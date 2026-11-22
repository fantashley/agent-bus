"""Operations shared by the MCP server and the CLI.

A :class:`Service` acts on behalf of one identity: an agent session (MCP) or
the human user (CLI, ``agent="human"``).  Results are plain dicts ready to serialise.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any

from . import config as cfg
from . import rex, worker
from .db import HUMAN, SYSTEM, Bus, BusError, wrap_peer

COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
CHANGE_RE = re.compile(r"^[a-z0-9]{1,64}$")


def present(m: dict) -> dict:
    """A message as agents see it: peer text fenced, trust explicit."""
    body = m["body"]
    if m["trust"] == "peer_data" and m["sender"] != SYSTEM:
        body = wrap_peer(m["sender"], body)
    out = {"id": m["id"], "thread": m["thread_id"], "from": "human" if m["sender"] == HUMAN else m["sender"],
           "kind": m["kind"], "trust": m["trust"], "round": m["round"], "body": body,
           "at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(m["created_at"]))}
    if m["recipient"]:
        out["to"] = m["recipient"]
    data = {k: v for k, v in m["data"].items() if k != "discussion"}
    if data:
        out["data"] = data
    return out


def thread_summary(t: dict) -> dict:
    keys = ("id", "kind", "goal", "participants", "initiator", "status", "round", "max_rounds")
    out = {k: t[k] for k in keys}
    if t["initiator"] not in (HUMAN, SYSTEM, None):
        # An agent wrote the goal; label it so it isn't read as an instruction.
        out["goal"] = wrap_peer(t["initiator"], t["goal"])
    if t["deadline"]:
        out["deadline"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t["deadline"]))
    if t["final_proposal"]:
        out["final_proposal"] = t["final_proposal"]
    return out


class Service:
    def __init__(self, bus: Bus, agent: str, session_id: str | None = None, config: cfg.Config | None = None):
        self.bus = bus
        self.agent = agent
        self.session_id = session_id
        self.config = config or cfg.Config.load()

    @property
    def subscriber(self) -> str:
        return self.session_id or self.agent

    # ------------------------------------------------------------------ messaging

    def send(self, body: str, thread: str | None = None, to: str | None = None, kind: str = "message",
             on: str | None = None, stance: str | None = None, reasons: str | None = None,
             refs: dict | None = None) -> dict:
        if not body or not body.strip():
            raise ValueError("body is empty")
        if to is not None and to not in cfg.AGENTS:
            raise ValueError(f"`to` must be one of {', '.join(cfg.AGENTS)}")
        if kind not in ("message", "proposal", "vote", "status"):
            raise ValueError("kind must be message, proposal, vote or status")
        if thread is None:
            if not to:
                raise ValueError("give `thread`, or `to` to start a new chat thread")
            t = self.bus.create_thread("chat", body.strip().splitlines()[0][:120], [self.agent, to],
                                       initiator=self.agent, initiator_session=self.session_id,
                                       max_messages=self.config.max_messages)
            thread = t["id"]
        data: dict[str, Any] = {}
        if refs:
            data["refs"] = refs
        if kind == "vote":
            data.update(on=on, stance=stance, reasons=reasons or body)
        m = self.bus.post(thread, self.agent, body, kind=kind, session_id=self.session_id, recipient=to,
                          data=data)
        t = self.bus.thread(thread)
        res = {"posted": present(m), "thread": thread_summary(t)}
        if t["kind"] == "discussion" and t["status"] == "open":
            res["next"] = "Call wait(thread) for the other participants' moves."
        return res

    def reply(self, message_id: int, body: str) -> dict:
        orig = self.bus.message(message_id)
        to = orig["sender"] if orig["sender"] in cfg.AGENTS and orig["sender"] != self.agent else None
        return self.send(body, thread=orig["thread_id"], to=to)

    def inbox(self, thread: str | None = None, limit: int = 50) -> dict:
        msgs = self.bus.unread(self.subscriber, self.agent, thread_id=thread, limit=max(1, min(limit, 200)))
        res: dict[str, Any] = {"messages": [present(m) for m in msgs]}
        if thread:
            t = self.bus.thread(thread)
            res["thread"] = thread_summary(t)
            if self.bus._blind(t, self.agent):
                res["blind"] = ("Round 1 is blind: other participants' views stay hidden until you post "
                                "your own with send(kind=\"proposal\").")
        return res

    def history(self, thread: str, limit: int = 200) -> dict:
        h = self.bus.history(thread, viewer=None if self.agent == HUMAN else self.agent, limit=limit)
        res = {"thread": thread_summary(h["thread"]), "messages": [present(m) for m in h["messages"]]}
        if h["blind"]:
            res["blind"] = "Round 1 is blind until you post your own proposal."
        return res

    def threads(self, include_closed: bool = False) -> dict:
        who = None if self.agent == HUMAN else self.agent
        return {"threads": [thread_summary(t) for t in self.bus.threads(include_closed, agent_type=who)]}

    def thread_state(self, thread: str) -> dict:
        t = self.bus.thread(thread)
        res: dict[str, Any] = {"thread": thread_summary(t)}
        if t["kind"] == "discussion":
            res["standings"] = self.bus.standings(thread)
            job = self.bus.active_job(thread)
            res["worker"] = job["status"] if job else "idle"
        return res

    # ------------------------------------------------------------- coordination

    def start_discussion(self, question: str, peers: list[str] | None = None, repo: str | None = None,
                         context_refs: list[str] | None = None, max_rounds: int | None = None,
                         deadline_minutes: int | None = None, watch: bool = False) -> dict:
        if not question or not question.strip():
            raise ValueError("question is empty")
        if peers is None:
            peers = [a for a in cfg.AGENTS if a != self.agent]
        for p in peers:
            if p not in cfg.AGENTS:
                raise ValueError(f"unknown peer {p!r}; expected one of {', '.join(cfg.AGENTS)}")
            if p == self.agent:
                raise ValueError("you are already a participant; list only the other agents as peers")
        if not peers:
            raise ValueError("a discussion needs at least one peer")
        repo_path = self._allowed_repo(repo) if repo else None
        rounds = max(1, min(int(max_rounds or self.config.max_rounds), 10))
        minutes = max(1, min(int(deadline_minutes or self.config.deadline_minutes), 240))
        participants = list(peers) if self.agent == HUMAN else [self.agent, *peers]
        t = self.bus.create_thread(
            "discussion", question.strip(), participants, initiator=self.agent,
            initiator_session=self.session_id, max_rounds=rounds,
            max_messages=max(self.config.max_messages, rounds * len(participants) * 3),
            deadline=time.time() + minutes * 60, repo=repo_path,
            refs={"context_refs": context_refs} if context_refs else None)
        if self.agent == HUMAN:
            self.bus.post(t["id"], HUMAN, question.strip())
        else:
            # The question is the initiator's own text: it stays peer_data, never system.
            self.bus.post(t["id"], self.agent, question.strip(), session_id=self.session_id)
        self.bus.post(t["id"], SYSTEM, f"Discussion started by {'the human user' if self.agent == HUMAN else self.agent} "
                      f"with {', '.join(participants)}; up to {rounds} rounds.", kind="status")
        job = self._dispatch(t["id"], "discussion", {"mode": "start"}, f"{t['id']}:start")
        res: dict[str, Any] = {"thread": thread_summary(self.bus.thread(t["id"])), "job": job}
        if self.agent != HUMAN:
            res["next"] = ("Round 1 is blind. Post your own initial view now with "
                           f"send(thread=\"{t['id']}\", kind=\"proposal\", body=...) before reading anything, "
                           "then loop wait(thread) and vote or revise until the thread is resolved or "
                           "needs_human. If your turn has to end, the outcome will be in your inbox.")
        if watch:
            # An agent's viewers go next to its own recorded block, not wherever its MCP
            # server's environment points (under Codex's shared daemon that's another window).
            sess = self.bus.session(self.session_id) if self.session_id else None
            res["watch"] = rex.open_viewers(t["id"], peers, location=sess)
        return res

    def advance_discussion(self, thread: str) -> dict:
        t = self.bus.thread(thread)
        if t["kind"] != "discussion":
            raise ValueError(f"{thread} is not a discussion")
        if self.agent not in (t["initiator"], HUMAN):
            raise BusError("NOT_INITIATOR", "only the initiator (or the human user) can advance a discussion")
        if t["status"] != "open":
            raise BusError("THREAD_CLOSED", f"thread is {t['status']}")
        if t["round"] >= t["max_rounds"]:
            raise BusError("THREAD_LIMIT_REACHED",
                           f"all {t['max_rounds']} rounds are used; run `agent-bus resume {thread}`")
        job = self._dispatch(thread, "discussion", {"mode": "advance"}, f"{thread}:advance:{t['round'] + 1}")
        return {"thread": thread_summary(t), "job": job}

    def request_review(self, reviewer: str, repo: str, base_commit_id: str, head_commit_id: str,
                       change_id: str | None = None, prev_head_commit_id: str | None = None,
                       focus: str | None = None) -> dict:
        if reviewer not in cfg.AGENTS or reviewer == self.agent:
            raise ValueError("reviewer must be one of the other agents")
        for name, value in (("base_commit_id", base_commit_id), ("head_commit_id", head_commit_id),
                            ("prev_head_commit_id", prev_head_commit_id)):
            if value is not None and not COMMIT_RE.match(value):
                raise ValueError(f"{name} must be a full 40-character commit id")
        if change_id is not None and not CHANGE_RE.match(change_id):
            raise ValueError("change_id must be a jj change id (lowercase letters and digits)")
        repo_path = self._allowed_repo(repo)
        refs = {"reviewer": reviewer, "repo": repo_path, "change_id": change_id,
                "base_commit_id": base_commit_id, "head_commit_id": head_commit_id,
                "prev_head_commit_id": prev_head_commit_id, "focus": focus,
                "heads": [head_commit_id]}  # every head this thread has been asked to review
        # The requester is part of the key: two agents asking for the same review each get
        # their own thread, so each verdict reaches the agent that asked.
        key = (f"review:{self.agent}:{repo_path}:{reviewer}:"
               f"{prev_head_commit_id or base_commit_id}:{head_commit_id}")
        self.bus.reap()
        # Follow-up pass: same thread, so the worker resumes the reviewer's conversation.
        # The lineage lookup and every busy/stale decision and write happen in one IMMEDIATE
        # transaction, so concurrent follow-ups serialize and a loser changes nothing.
        with self.bus.tx():
            previous = (self._previous_review(reviewer, repo_path, prev_head_commit_id)
                        if prev_head_commit_id else None)
            if previous:
                t = previous
                current = (t["refs"] or {}).get("head_commit_id")
                active = self.bus._one("SELECT * FROM jobs WHERE thread_id=? AND status IN ('queued','running')",
                                       t["id"])
                same = self.bus._one("SELECT * FROM jobs WHERE thread_id=? AND dedupe_key=?"
                                     " AND status IN ('queued','running','done') ORDER BY created_at DESC LIMIT 1",
                                     t["id"], key)
                if same:
                    # This exact request (same prev head -> head) is queued, running or done:
                    # idempotent, whatever happened to the thread since.
                    job_row, created = self.bus._job(same), False
                elif active:
                    raise BusError("REVIEW_BUSY", f"{reviewer} is still on a previous pass ({t['id']}); "
                                   "wait for that verdict, then request this head again")
                elif current != prev_head_commit_id:
                    raise BusError("REVIEW_STALE", f"{t['id']} has moved on to head {str(current)[:12]}; "
                                   "use that as prev_head_commit_id")
                else:
                    if focus:
                        self.bus.post(t["id"], self.agent, focus, session_id=self.session_id, recipient=reviewer)
                    refs["heads"] = [*self._heads(t), head_commit_id]
                    self.bus.set_thread(t["id"], refs=json.dumps(refs), status="open", round=t["round"] + 1,
                                        max_rounds=t["round"] + 1,
                                        goal=f"Review {change_id or head_commit_id[:12]} in "
                                             f"{os.path.basename(repo_path)}")
                    job_row, created = self.bus.create_job(t["id"], "review", {"mode": "followup"}, key)
        if previous:
            if created:
                worker.spawn(job_row["id"])
            job = self._job_info(job_row, created)
        else:
            goal = f"Review {change_id or head_commit_id[:12]} in {os.path.basename(repo_path)}"
            t = self.bus.create_thread("review", goal, [self.agent, reviewer], initiator=self.agent,
                                       initiator_session=self.session_id, max_rounds=1,
                                       max_messages=self.config.max_messages, repo=repo_path, refs=refs)
            t["round"] = 1
            self.bus.set_thread(t["id"], round=1)
            job = self._dispatch(t["id"], "review", {"mode": "first"}, key)
            if job.get("deduplicated"):
                self.bus.set_thread(t["id"], status="cancelled")
                t = self.bus.thread(job["thread"])
        t = self.bus.thread(t["id"])
        return {"thread": thread_summary(t), "job": job,
                "next": f"Call wait(\"{t['id']}\") for the verdict, or check inbox later."}

    @staticmethod
    def _heads(t: dict) -> list[str]:
        refs = t["refs"] or {}
        return list(refs.get("heads") or [refs.get("head_commit_id")])

    def _previous_review(self, reviewer: str, repo: str, prev_head: str) -> dict | None:
        """The requester's review thread with this reviewer that has reviewed ``prev_head``
        at any point (not only as its current head), so a request based on a head the
        thread has since moved past is reported as stale instead of starting a new thread."""
        for r in self.bus._all("SELECT id FROM threads WHERE kind='review' AND initiator=? AND repo=?"
                               " ORDER BY created_at DESC", self.agent, repo):
            t = self.bus.thread(r["id"])
            if (t["refs"] or {}).get("reviewer") == reviewer and t["status"] != "cancelled" \
                    and prev_head in self._heads(t):
                return t
        return None

    def resume(self, thread: str, extra_rounds: int = 2) -> dict:
        """Human only: lift the turn cap and give a stalled discussion more rounds."""
        if self.agent != HUMAN:
            raise BusError("HUMAN_ONLY", "only the human user can resume a thread")
        t = self.bus.thread(thread)
        if t["status"] in ("resolved",):
            raise BusError("THREAD_CLOSED", "thread is already resolved")
        fields: dict[str, Any] = {"status": "open"}
        count = self.bus._one("SELECT COUNT(*) AS n FROM messages WHERE thread_id=? AND sender NOT IN (?,?)",
                              thread, HUMAN, SYSTEM)["n"]
        fields["max_messages"] = max(t["max_messages"], count + self.config.max_messages)
        if t["kind"] == "discussion":
            fields["max_rounds"] = max(t["max_rounds"], t["round"]) + extra_rounds
            fields["deadline"] = time.time() + self.config.deadline_minutes * 60
        self.bus.set_thread(thread, **fields)
        self.bus.post(thread, SYSTEM, f"Resumed by the human user with {extra_rounds} more round(s)."
                      if t["kind"] == "discussion" else "Resumed by the human user.", kind="status")
        res: dict[str, Any] = {"thread": thread_summary(self.bus.thread(thread))}
        if t["kind"] == "discussion":
            res["job"] = self._dispatch(thread, "discussion", {"mode": "start_later"},
                                        f"{thread}:resume:{time.time():.0f}")
        elif t["kind"] == "review" and not self.bus.active_job(thread) and not self._has_verdict_for_head(t):
            res["job"] = self._dispatch(thread, "review", {"mode": "retry"}, f"{thread}:retry:{time.time():.0f}")
        return res

    def _has_verdict_for_head(self, t: dict) -> bool:
        head = (t["refs"] or {}).get("head_commit_id")
        return any(m["kind"] == "review_verdict" and m["data"].get("head_commit_id") == head
                   for m in self.bus.history(t["id"])["messages"])

    def cancel(self, thread: str) -> dict:
        if self.agent != HUMAN:
            raise BusError("HUMAN_ONLY", "only the human user can cancel a thread")
        pids = self.bus.cancel_thread(thread)
        killed = []
        for pid in pids:
            try:
                os.killpg(pid, 15)
                killed.append(pid)
            except (ProcessLookupError, PermissionError):
                pass
        return {"thread": thread_summary(self.bus.thread(thread)), "signalled": killed}

    # ------------------------------------------------------------------ helpers

    def _allowed_repo(self, repo: str) -> str:
        if not os.path.isdir(os.path.expanduser(repo)):
            raise ValueError(f"repo {repo!r} is not a directory")
        root = cfg.repo_root(repo)
        if not self.config.repo_allowed(root):
            raise BusError("REPO_NOT_ALLOWED",
                           f"{root} is not in the agent-bus repo allowlist ({cfg.config_path()})")
        return root

    def _dispatch(self, thread: str, kind: str, params: dict, dedupe_key: str) -> dict:
        job, created = self.bus.create_job(thread, kind, params, dedupe_key)
        if created:
            worker.spawn(job["id"])
        return self._job_info(job, created)

    @staticmethod
    def _job_info(job: dict, created: bool) -> dict:
        return {"id": job["id"], "thread": job["thread_id"], "status": job["status"],
                **({} if created else {"deduplicated": True})}
