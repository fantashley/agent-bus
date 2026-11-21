"""Operations shared by the MCP server and the CLI.

A :class:`Service` acts on behalf of one identity: an agent session (MCP) or
Ashley (CLI, ``agent="human"``).  Results are plain dicts ready to serialise.
"""

from __future__ import annotations

import os
import re
import time
from typing import Any

from . import config as cfg
from . import rex, worker
from .db import HUMAN, SYSTEM, Bus, BusError, wrap_peer

COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


def present(m: dict) -> dict:
    """A message as agents see it: peer text fenced, trust explicit."""
    body = m["body"]
    if m["trust"] == "peer_data" and m["sender"] != SYSTEM:
        body = wrap_peer(m["sender"], body)
    out = {"id": m["id"], "thread": m["thread_id"], "from": "Ashley (human)" if m["sender"] == HUMAN else m["sender"],
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
        self.bus.post(t["id"], SYSTEM, f"Discussion started by "
                      f"{'Ashley' if self.agent == HUMAN else self.agent}: {question.strip()}", kind="status")
        job = self._dispatch(t["id"], "discussion", {"mode": "start"}, f"{t['id']}:start")
        res: dict[str, Any] = {"thread": thread_summary(self.bus.thread(t["id"])), "job": job}
        if self.agent != HUMAN:
            res["next"] = ("Round 1 is blind. Post your own initial view now with "
                           f"send(thread=\"{t['id']}\", kind=\"proposal\", body=...) before reading anything, "
                           "then loop wait(thread) and vote or revise until the thread is resolved or "
                           "needs_human. If your turn has to end, the outcome will be in your inbox.")
        if watch:
            res["watch"] = rex.open_viewers(t["id"], peers)
        return res

    def advance_discussion(self, thread: str) -> dict:
        t = self.bus.thread(thread)
        if t["kind"] != "discussion":
            raise ValueError(f"{thread} is not a discussion")
        if self.agent not in (t["initiator"], HUMAN):
            raise BusError("NOT_INITIATOR", "only the initiator (or Ashley) can advance a discussion")
        if t["status"] != "open":
            raise BusError("THREAD_CLOSED", f"thread is {t['status']}")
        if t["round"] >= t["max_rounds"]:
            raise BusError("THREAD_LIMIT_REACHED",
                           f"all {t['max_rounds']} rounds are used; Ashley can `agent-bus resume {thread}`")
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
        repo_path = self._allowed_repo(repo)
        refs = {"reviewer": reviewer, "repo": repo_path, "change_id": change_id,
                "base_commit_id": base_commit_id, "head_commit_id": head_commit_id,
                "prev_head_commit_id": prev_head_commit_id, "focus": focus}
        goal = f"Review {change_id or head_commit_id[:12]} in {os.path.basename(repo_path)}"
        t = self.bus.create_thread("review", goal, [self.agent, reviewer], initiator=self.agent,
                                   initiator_session=self.session_id, max_rounds=1,
                                   max_messages=self.config.max_messages, repo=repo_path, refs=refs,
                                   deadline=time.time() + self.config.turn_timeout_s * 2)
        job = self._dispatch(t["id"], "review", {},
                             f"review:{repo_path}:{reviewer}:{prev_head_commit_id or base_commit_id}:{head_commit_id}")
        if job.get("deduplicated"):
            # The same review is already queued or done; drop the empty thread we just made.
            self.bus.set_thread(t["id"], status="cancelled")
            existing = self.bus.thread(job["thread"])
            return {"thread": thread_summary(existing), "job": job,
                    "next": f"Already requested. Call wait(\"{existing['id']}\") for the verdict."}
        return {"thread": thread_summary(t), "job": job,
                "next": f"Call wait(\"{t['id']}\") for the verdict, or check inbox later."}

    def resume(self, thread: str, extra_rounds: int = 2) -> dict:
        """Human only: lift the turn cap and give a stalled discussion more rounds."""
        if self.agent != HUMAN:
            raise BusError("HUMAN_ONLY", "only Ashley can resume a thread")
        t = self.bus.thread(thread)
        if t["status"] in ("resolved",):
            raise BusError("THREAD_CLOSED", "thread is already resolved")
        fields: dict[str, Any] = {"limit_reached": 0, "status": "open"}
        count = self.bus._one("SELECT COUNT(*) AS n FROM messages WHERE thread_id=? AND sender NOT IN (?,?)",
                              thread, HUMAN, SYSTEM)["n"]
        fields["max_messages"] = max(t["max_messages"], count + self.config.max_messages)
        if t["kind"] == "discussion":
            fields["max_rounds"] = max(t["max_rounds"], t["round"]) + extra_rounds
            fields["deadline"] = time.time() + self.config.deadline_minutes * 60
        self.bus.set_thread(thread, **fields)
        self.bus.post(thread, SYSTEM, f"Resumed by Ashley with {extra_rounds} more round(s).", kind="status")
        res: dict[str, Any] = {"thread": thread_summary(self.bus.thread(thread))}
        if t["kind"] == "discussion":
            res["job"] = self._dispatch(thread, "discussion", {"mode": "start_later"},
                                        f"{thread}:resume:{time.time():.0f}")
        return res

    def cancel(self, thread: str) -> dict:
        if self.agent != HUMAN:
            raise BusError("HUMAN_ONLY", "only Ashley can cancel a thread")
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
        return {"id": job["id"], "thread": job["thread_id"], "status": job["status"],
                **({} if created else {"deduplicated": True})}
