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
from . import autoresume, quota
from . import rex, unvotable, worker
from .db import HUMAN, SYSTEM, Bus, BusError, deadline_message, is_quiet, overdue, past_deadline, review_progress, wrap_peer

# A pinned commit id: the full 40 hex digits, or a prefix of at least 7 that request_review
# resolves to one commit in the repo (#73).  Upper case is accepted and lowered.
COMMIT_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
FULL_ID_HINT = ("get full ids with `jj log --no-graph -r <rev> -T commit_id` or `git rev-parse <rev>`, "
                "and never extend a short id by hand")
# A label for the change under review: a jj change id, a git branch name, or e.g. "PR #12".
CHANGE_RE = re.compile(r"[A-Za-z0-9#][A-Za-z0-9 ._/#:+-]{0,99}")
# The longest testing note a review request may carry (#93).
MAX_TESTING_NOTE = 4000
BLIND_NOTE = ("Round 1 is blind: other participants' views stay hidden until you post "
              "your own with send(kind=\"proposal\").")


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
    data = {k: v for k, v in m["data"].items() if k not in ("discussion", "session", "quiet")}
    if data:
        out["data"] = data
    return out


def thread_summary(t: dict, goal: bool = True) -> dict:
    """A thread as agents see it.  ``goal=False`` leaves out the goal: replies to a thread's
    own moves (send, wait, inbox) don't repeat it, since it can be long (#74)."""
    keys = ("id", "kind", "goal", "participants", "initiator", "status", "round", "max_rounds")
    out = {k: t[k] for k in keys}
    if t["kind"] == "review":
        # A review has no round cap; it reports passes and follow-ups instead (#71).
        del out["max_rounds"]
        out.update(review_progress(t))
    if t["initiator"] not in (HUMAN, SYSTEM, None):
        # An agent wrote the goal; label it so it isn't read as an instruction.
        out["goal"] = wrap_peer(t["initiator"], t["goal"])
    if not goal:
        del out["goal"]
    if t["deadline"]:
        out["deadline"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t["deadline"]))
    if t["final_proposal"]:
        out["final_proposal"] = t["final_proposal"]
    return out


def review_next_action(t: dict, st: dict, job: dict | None, last_failed: bool = False,
                       pushback_pending: bool = False) -> str:
    """A one-line hint for a review's initiator (#117): keep waiting, fix, done, or stop.
    ``st`` is the thread's review_standings, ``job`` its active job (or None),
    ``last_failed`` whether its latest review job failed with nothing running (a worker that
    died, say), and ``pushback_pending`` whether the current pass is a pushback still due
    its verdict."""
    head = st.get("head_commit_id") or ""
    if job is not None and job["kind"] == "debrief":
        return "wait: debrief replies are on their way"
    if t["status"] == "cancelled":
        return "stop: the review was cancelled"
    if t["status"] == "needs_human":
        return ("stop: the thread waits for the human user; read the latest system notice and tell them "
                "what it says (agent-bus resume is theirs to run)")
    if st["complete"]:
        several = len(st["reviewers"]) > 1
        return f"done: {'every required reviewer' if several else next(iter(st['reviewers']))} approved head {head}"
    # A failed pass with no verdict to act on: a pushback's earlier request for changes still
    # shows, but only a resume reruns that pass.
    if last_failed and (pushback_pending or not st["changes_requested"]):
        return (f"stop: the last review pass failed and nothing is running; tell the human user, who can run "
                f"`agent-bus resume {t['id']}` to retry it")
    # A request for changes on the current head ends the pass, even while its worker is
    # still closing the job: nobody else reviews this head until there is a fix.  Past the
    # follow-up limit the worker parks the thread (or died before it could): a new request
    # would fail with REVIEW_LIMIT_REACHED.
    if st["changes_requested"] and st.get("follow_ups", 0) >= st.get("followup_limit", float("inf")):
        return ("stop: the review still asks for changes after its follow-up limit (REVIEW_LIMIT_REACHED); "
                "tell the human user")
    if st["changes_requested"]:
        return (f"fix: {', '.join(st['changes_requested'])} asked for changes on head {head}. Address the blocking "
                "findings, send a note on each, then request_review again with the new head and "
                f"prev_head_commit_id={head} (or send evidence and request the same head to push back)")
    if job is not None:
        who = [r for r, v in st["reviewers"].items() if v["state"] == "reviewing"] or st["pending"]
        return f"wait: {', '.join(who) or 'a reviewer'} is reviewing head {head}"
    if st["pending"]:
        return f"wait: {', '.join(st['pending'])} still to review head {head}"
    return "wait"


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

    def send(self, body: str | None = None, thread: str | None = None, to: str | None = None,
             kind: str = "message", on: str | None = None, stance: str | None = None,
             reasons: str | None = None, refs: dict | None = None, caveats: str | None = None) -> dict:
        reasons = reasons.strip() if reasons and reasons.strip() else None
        caveats = caveats.strip() if caveats and caveats.strip() else None
        if caveats and (kind != "vote" or stance != "agree"):
            # Non-blocking concerns that come with an agreement (#101); an objection's
            # concerns are its reasons.
            raise BusError("BAD_VOTE", "caveats go only on an agree vote; put an objection's concerns in reasons")
        if kind == "vote":
            # A vote's on/stance/reasons say everything; a body is optional (#74).  The
            # reasons go into the body too, so every rendering of the vote shows them.
            text = body.strip() if body and body.strip() else ""
            if not text:
                body = reasons or f"{stance} on {on}"
            elif reasons and reasons not in text:
                body = f"{text}\n\nReasons: {reasons}"
            if caveats:  # shown under the vote wherever it is rendered
                body += f"\n\nCaveats (non-blocking): {caveats}"
        if not body or not body.strip():
            raise ValueError("body is empty")
        if to is not None:
            if not self.config.is_participant(to):
                raise ValueError(f"`to` must be one of {', '.join(self.config.participant_map())}")
            self.config.require_available(to, "recipient")
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
            if caveats:
                data["caveats"] = caveats
        m = self.bus.post(thread, self.agent, body, kind=kind, session_id=self.session_id, recipient=to,
                          data=data)
        t = self.bus.thread(thread)
        res = {"posted": present(m), "thread": thread_summary(t, goal=False)}
        if kind == "vote" and t["status"] == "open" and not self.bus._blind(t, self.agent):
            note = self._superseded(thread, on)
            if note:
                res["superseded"] = note
        if t["kind"] == "discussion" and t["status"] == "open":
            res["next"] = "Call wait(thread) for the other participants' moves."
        return res

    def _superseded(self, thread: str, on: str) -> str | None:
        """A warning when a vote went to a proposal that newer ones have since followed:
        proposals arrive concurrently, so one may land just before a vote on an older one."""
        props = self.bus.proposals(thread)
        target = next((p for p in props if p["data"].get("proposal_id") == on), None)
        newer = [p for p in props if target and p["id"] > target["id"]]
        if not newer:
            return None
        return (f"{on} is not the latest proposal: {self._list_proposals(newer)} came after it. Your vote "
                "counts only for " + on + "; vote on a newer one if that is what you back.")

    @staticmethod
    def _list_proposals(props: list[dict]) -> str:
        return ", ".join(f"{p['data']['proposal_id']} (by {p['sender']})" for p in props)

    def _newer_than_my_stance(self, t: dict) -> str | None:
        """A hint for a participant whose latest vote predates proposals from others: they
        may want to vote on the newer one (proposals and votes cross in flight)."""
        if t["kind"] != "discussion" or t["status"] != "open" or self.agent not in t["participants"]:
            return None
        mine = self.bus.standings(t["id"]).get(self.agent)
        if not mine:
            return None
        newer = [p for p in self.bus.proposals(t["id"])
                 if p["id"] > mine["message_id"] and p["sender"] != self.agent]
        if not newer:
            return None
        return (f"Proposals posted since your latest stance ({mine['stance']} on {mine['proposal_id']}): "
                f"{self._list_proposals(newer)}. Your stance still counts only for {mine['proposal_id']}.")

    def reply(self, message_id: int, body: str) -> dict:
        orig = self.bus.message(message_id)
        sender = orig["sender"]
        if sender not in (HUMAN, SYSTEM, self.agent) and not self.config.is_participant(sender):
            # A participant since removed from the config: a reply to it must not become a
            # message to the whole thread.
            raise ValueError(f"can't reply to {sender}: it is no longer a configured participant")
        to = sender if sender not in (HUMAN, SYSTEM, self.agent) else None
        return self.send(body, thread=orig["thread_id"], to=to)

    def inbox(self, thread: str | None = None, limit: int = 50) -> dict:
        msgs = self.bus.unread(self.subscriber, self.agent, thread_id=thread, limit=max(1, min(limit, 200)))
        return self._batch(thread, msgs)

    def _batch(self, thread: str | None, msgs: list[dict]) -> dict:
        res: dict[str, Any] = {"messages": [present(m) for m in msgs]}
        if thread:
            t = self.bus.thread(thread)
            res["thread"] = thread_summary(t, goal=False)
            if t["kind"] == "review":
                res.update(self.review_state(t))  # every wake-up says where the review stands (#117)
            # The last message in this batch (or, with none, the last one read on the
            # thread): pass it to wait(after_message_id=...) to replay anything after it,
            # e.g. a reply lost when the connection dropped, or the next page (#72).
            res["cursor"] = msgs[-1]["id"] if msgs else self.bus.cursor(self.subscriber, thread)
            if self.bus._blind(t, self.agent):
                res["blind"] = BLIND_NOTE
            else:
                hint = self._newer_than_my_stance(t)
                if hint:
                    res["newer_proposals"] = hint
        return res

    # ------------------------------------------------------------------ waiting (#72)

    def poll(self, thread: str, after_message_id: int | None = None, limit: int = 50,
             wake_quiet: bool = False) -> dict | None:
        """One look for ``wait``: the unread messages on ``thread``, marked read, when they
        should end the wait; otherwise None, leaving them unread.  Routine lifecycle
        notices (db.is_quiet) don't end it by themselves unless ``wake_quiet``; a full
        page does, so quiet notices can never hide what comes after them.
        ``after_message_id`` replays everything after that message instead (Bus.replay)."""
        self.bus.expire_discussion(thread)  # its notice is then among the messages (#74)
        if after_message_id is not None:
            msgs = self.bus.replay(self.subscriber, self.agent, thread, int(after_message_id), limit=limit)
            return self._batch(thread, msgs) if msgs else None
        msgs = self.bus.unread(self.subscriber, self.agent, thread_id=thread, limit=limit, advance=False)
        if not msgs or (not wake_quiet and len(msgs) < limit and all(is_quiet(m) for m in msgs)):
            return None
        self.bus.mark_read(self.subscriber, thread, msgs[-1]["id"])
        return self._batch(thread, msgs)

    def wait_timeout(self, thread: str, deliver: bool = True) -> dict:
        """What ``wait`` returns when nothing woke it: any quiet notices still unread, the
        thread's state and the progress of its current job.  ``deliver=False`` leaves
        unread messages for a concurrent wait that is still running."""
        delivered = self.poll(thread, wake_quiet=True) if deliver else None
        res: dict[str, Any] = {"status": "timeout", **self.thread_state(thread)}
        res["thread"].pop("goal", None)  # the caller has it already; it can be long
        if delivered:
            res["messages"] = delivered["messages"]
        res["cursor"] = delivered["cursor"] if delivered else self.bus.cursor(self.subscriber, thread)
        progress = self.progress(thread)
        if progress:
            res["progress"] = progress
        return res

    def progress(self, thread: str) -> dict | None:
        """How the thread's latest job is going, from its progress events: per peer that
        has taken a turn in it, the turn's state, elapsed time, latest activity and counts.
        Metadata only: event text can be a peer's draft, which the caller must not see
        before it is posted (and never unfenced)."""
        jobs = self.bus.jobs(thread)
        if not jobs:
            return None
        job = jobs[-1]
        now = time.time()
        # Not the job's error: it can quote what a peer was about to post.
        info: dict[str, Any] = {"status": job["status"], "elapsed_s": round(now - job["created_at"])}
        turns: dict[str, dict] = {}
        for e in self.bus.events(thread, job_id=job["id"]):
            agent, kind = e["agent"], e["kind"]
            if not agent:
                continue
            if kind == "turn_start":
                turns[agent] = {"state": "working", "start": e["created_at"], "end": None, "last": e,
                                "tool_calls": 0, "api_retries": 0, "errors": 0}
                continue
            turn = turns.get(agent)
            if turn is None:
                continue
            turn["last"] = e
            if kind == "turn_end":
                turn["state"] = "failed" if (e["text"] or "").startswith("failed") else "done"
                turn["end"] = e["created_at"]
            elif kind == "tool":
                turn["tool_calls"] += 1
            elif kind == "api_retry":
                turn["api_retries"] += 1
            elif kind == "error":
                turn["errors"] += 1
        peers: dict[str, dict] = {}
        for agent, turn in turns.items():
            state = turn["state"]
            if state == "working" and job["status"] not in ("queued", "running"):
                state = "stopped"  # its worker ended mid-turn
            out = {"state": state, "turn_elapsed_s": round((turn["end"] or now) - turn["start"]),
                   "last_event": turn["last"]["kind"], "last_event_age_s": round(now - turn["last"]["created_at"]),
                   "tool_calls": turn["tool_calls"]}
            out.update({k: turn[k] for k in ("api_retries", "errors") if turn[k]})
            peers[agent] = out
        return {"job": info, "peers": peers}

    def history(self, thread: str, limit: int = 200) -> dict:
        self.bus.expire_discussion(thread)
        h = self.bus.history(thread, viewer=None if self.agent == HUMAN else self.agent, limit=limit)
        res = {"thread": thread_summary(h["thread"]), "messages": [present(m) for m in h["messages"]]}
        if h["thread"]["kind"] == "review":
            res.update(self.review_state(h["thread"]))
        if h["blind"]:
            res["blind"] = "Round 1 is blind until you post your own proposal."
        return res

    def threads(self, include_closed: bool = False) -> dict:
        who = None if self.agent == HUMAN else self.agent
        self.bus.expire_discussions()
        return {"threads": [thread_summary(t) for t in self.bus.threads(include_closed, agent_type=who)]}

    def thread_state(self, thread: str) -> dict:
        self.bus.expire_discussion(thread)
        t = self.bus.thread(thread)
        res: dict[str, Any] = {"thread": thread_summary(t)}
        if t["kind"] == "discussion":
            if self.bus._blind(t, self.agent):
                res["blind"] = BLIND_NOTE  # standings would show who has proposed what
            else:
                res["standings"] = self.bus.standings(thread)
                hint = self._newer_than_my_stance(t)
                if hint:
                    res["newer_proposals"] = hint
            job = self.bus.active_job(thread)
            res["worker"] = job["status"] if job else "idle"
        elif t["kind"] == "review":
            res.update(self.review_state(t))
        return res

    def review_state(self, t: dict) -> dict:
        """A review thread's standings and what its initiator should do next (#117)."""
        job = self.bus.active_job(t["id"])  # reaps first: a dead worker's reviewer is no longer "reviewing"
        st = self.bus.review_standings(t["id"])
        jobs = [j for j in self.bus.jobs(t["id"]) if j["kind"] != "debrief"] if job is None else []
        failed = t["status"] == "open" and bool(jobs) and jobs[-1]["status"] == "failed"
        return {"review": st, "next_action": review_next_action(t, st, job, failed,
                                                                failed and self._pushback_pending(t))}

    # ------------------------------------------------------------- coordination

    def _quota_block(self, agent: str, override: dict | None = None) -> dict | None:
        return quota.block_for(self.bus, self.config, agent, self.config.peer_settings(agent, override))

    def _quota_check(self, agent: str, role: str, override: dict | None = None) -> None:
        """Fail fast, before any job is queued, when ``agent`` is known to be out of usage."""
        block = self._quota_block(agent, override)
        if block:
            raise BusError(quota.code(block), quota.message(agent, role, block, self.config))

    def start_discussion(self, question: str, peers: list[str] | None = None, repo: str | None = None,
                         context_refs: list[str] | None = None, max_rounds: int | None = None,
                         deadline_minutes: int | None = None, watch: bool = False,
                         peer_settings: dict | None = None) -> dict:
        """``peer_settings``: optional per-peer model/effort for this discussion's headless
        turns, e.g. {"codex": {"effort": "high"}}, on top of the config's."""
        if not question or not question.strip():
            raise ValueError("question is empty")
        defaulted = peers is None
        if defaulted:
            peers = [a for a in self.config.available_agents() if a != self.agent]
        for p in peers:
            self.config.participant(p, "peer")  # unknown names fail here
            if p == self.agent:
                raise ValueError("you are already a participant; list only the other agents as peers")
            self.config.require_available(p, "peer")
        settings = {}
        for agent, value in (peer_settings or {}).items():
            if agent not in peers:
                raise ValueError(f"peer_settings names {agent!r}, which isn't a peer in this discussion")
            settings[agent] = self.config.check_settings(agent, value)
        # Out of usage: left out of a default roster (and reported), refused when named.
        skipped: dict[str, dict] = {}
        for p in list(peers):
            block = self._quota_block(p, settings.get(p))
            if block and defaulted:
                skipped[p] = block
                peers.remove(p)
                settings.pop(p, None)
            elif block:
                raise BusError(quota.code(block), quota.message(p, "peer", block, self.config))
        if not peers:
            if skipped:
                codes = {quota.code(b) for b in skipped.values()}
                raise BusError(next(iter(codes)) if len(codes) == 1 else "PEERS_CANNOT_VOTE",
                               ("no peer has usage left: " if codes == {"USAGE_EXHAUSTED"}
                                else "no peer can take a turn: ")
                               + " ".join(quota.message(p, "peer", b, self.config) for p, b in skipped.items()))
            if defaulted:
                which = "no agent" if self.agent == HUMAN else "no other agent"
                raise ValueError(f"a discussion needs at least one peer ({which} is available on this machine)")
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
            refs={k: v for k, v in (("context_refs", context_refs), ("peer_settings", settings)) if v} or None)
        if self.agent == HUMAN:
            self.bus.post(t["id"], HUMAN, question.strip())
        else:
            # The question is the initiator's own text: it stays peer_data, never system.
            self.bus.post(t["id"], self.agent, question.strip(), session_id=self.session_id)
        self.bus.post(t["id"], SYSTEM, f"Discussion started by {'the human user' if self.agent == HUMAN else self.agent} "
                      f"with {', '.join(participants)}; up to {rounds} rounds."
                      + "".join(f" Left out: {quota.message(p, 'peer', b, self.config)}" for p, b in skipped.items()),
                      kind="status", data={"quiet": True})
        job = self._dispatch(t["id"], "discussion", {"mode": "start"}, f"{t['id']}:start")
        res: dict[str, Any] = {"thread": thread_summary(self.bus.thread(t["id"])), "job": job}
        if skipped:
            res["skipped_peers"] = {p: quota.summary(b) for p, b in skipped.items()}
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
        expired = self.bus.expire_discussion(thread)
        t = self.bus.thread(thread)
        if expired or past_deadline(t):
            raise BusError("DEADLINE_REACHED", deadline_message(t, parked=expired, refused="can't be advanced"))
        if t["status"] != "open":
            raise BusError("THREAD_CLOSED", f"thread is {t['status']}")
        if t["round"] >= t["max_rounds"]:
            raise BusError("THREAD_LIMIT_REACHED",
                           f"all {t['max_rounds']} rounds are used; run `agent-bus resume {thread}`")
        for p in t["participants"]:
            if p != t["initiator"]:
                self.config.require_available(p, "peer")
        # Peers that can't vote again (out of usage, format drift) keep their recorded
        # votes; refuse only when another round can't help.
        s = unvotable.assess(self.bus, self.config, t)
        if s.stalemate:
            raise BusError(unvotable.code(s.stuck), "no consensus is possible until these peers can vote again: "
                           + unvotable.explain(s.stuck, thread, self.config))
        if s.everyone:
            # No headless peer can move, so a round would only be spent (issue #39); the
            # initiator can still settle it by voting on the proposal they all back.
            how = (f"{t['initiator']} (initiator) can settle it by voting on it" if self.agent == HUMAN else
                   f"vote on it with send(thread=\"{thread}\", kind=\"vote\", on=\"{s.backs}\", "
                   "stance=\"agree\"|\"object\")")
            raise BusError(unvotable.code(s.stuck), "no peer can take another round: "
                           + unvotable.explain(s.stuck, thread, self.config)
                           + f" Every peer backs {s.backs}: {how}, or retry once they can vote again.")
        # Checked again where the job is queued: a sweep may have parked the thread meanwhile.
        self._launch_later = []
        try:
            with self.bus.tx():
                now = self.bus.thread(thread)
                if past_deadline(now):
                    raise BusError("DEADLINE_REACHED", deadline_message(now, parked=False,
                                                                        refused="can't be advanced"))
                if now["status"] != "open" or now["round"] != t["round"]:
                    raise BusError("THREAD_CLOSED" if now["status"] != "open" else "THREAD_BUSY",
                                   f"thread {thread} changed meanwhile ({now['status']}, round {now['round']})")
                job = self._dispatch(thread, "discussion", {"mode": "advance"}, f"{thread}:advance:{t['round'] + 1}")
            launches = self._launch_later
        finally:
            self._launch_later = None
        for j in launches:
            if j["created"]:
                worker.launch(self.bus, j["id"])
        return {"thread": thread_summary(t, goal=False), "job": job}

    def request_review(self, reviewer: str | None, repo: str, base_commit_id: str, head_commit_id: str,
                       change_id: str | None = None, prev_head_commit_id: str | None = None,
                       focus: str | None = None, model: str | None = None, effort: str | None = None,
                       reviewers: list[str] | None = None, peer_settings: dict | None = None,
                       thread: str | None = None, testing: str | None = None) -> dict:
        """``model``/``effort`` override the config for this reviewer on this thread; a
        follow-up pass keeps the thread's settings unless it gives new ones.

        ``testing`` is the author's note on how this head was tested (#93): shown to the
        reviewers of this head, labelled as the author's unchecked claim.

        ``reviewers`` (instead of ``reviewer``) names several required reviewers: they review
        one at a time on one thread, and the change is ready once all approve the same head
        (#47).  Their model/effort go in ``peer_settings``, e.g. {"codex": {"effort": "high"}}."""
        if testing is not None and (not isinstance(testing, str) or len(testing) > MAX_TESTING_NOTE):
            raise ValueError(f"testing must be a note of at most {MAX_TESTING_NOTE} characters")
        testing = (testing.strip() or None) if testing is not None else None
        if reviewers is not None:
            if reviewer is not None:
                raise ValueError("give `reviewer` or `reviewers`, not both")
            if model or effort:
                raise ValueError("with `reviewers`, set each reviewer's model/effort in peer_settings")
            if not isinstance(reviewers, list) or not reviewers:
                raise ValueError("reviewers must be a non-empty list of agents")
            if len(reviewers) == 1:  # one required reviewer: the plain review
                reviewer = reviewers[0]
                if thread is not None:
                    raise ValueError("`thread` is for reviews by several reviewers; a single reviewer's "
                                     "review is found by its heads")
                extra = set(peer_settings or {}) - {reviewer}
                if extra:
                    raise ValueError(f"peer_settings names {sorted(extra)[0]!r}, which isn't a reviewer here")
                if self.config.is_participant(reviewer):  # unknown agents are refused just below
                    given = self.config.check_settings(reviewer, (peer_settings or {}).get(reviewer, {}))
                    model, effort = given.get("model"), given.get("effort")
            else:
                return self._request_reviews(reviewers, repo, base_commit_id, head_commit_id, change_id,
                                             prev_head_commit_id, focus, peer_settings, thread, testing)
        elif peer_settings:
            raise ValueError("peer_settings goes with `reviewers`; use model/effort with a single `reviewer`")
        if thread is not None:
            raise ValueError("`thread` goes with `reviewers`; a single reviewer's review is found by its heads")
        if not self.config.is_participant(reviewer) or reviewer == self.agent:
            raise ValueError("reviewer must be one of the other agents")
        override = self.config.check_settings(reviewer, {"model": model, "effort": effort})
        self.config.require_available(reviewer, "reviewer")
        self._check_review_ids(base_commit_id, head_commit_id, prev_head_commit_id, change_id)
        repo_path = self._allowed_repo(repo)
        base_commit_id, head_commit_id, prev_head_commit_id = self._resolve_review_ids(
            repo_path, base_commit_id, head_commit_id, prev_head_commit_id)
        request = [base_commit_id, prev_head_commit_id, head_commit_id]
        refs = {"reviewer": reviewer, "repo": repo_path, "change_id": change_id,
                "base_commit_id": base_commit_id, "head_commit_id": head_commit_id,
                "prev_head_commit_id": prev_head_commit_id, "focus": focus, "testing": testing,
                "heads": [head_commit_id],  # every head this thread has been asked to review
                **self._budget_refs()}
        if override:
            refs["peer_settings"] = {reviewer: override}
        self.bus.reap()
        # Follow-up pass: same thread, so the worker resumes the reviewer's conversation.
        # The lineage lookup and every busy/stale decision and write happen in one IMMEDIATE
        # transaction, so concurrent follow-ups serialize and a loser changes nothing.
        with self.bus.tx():
            previous = (self._previous_review(reviewer, repo_path, prev_head_commit_id, request)
                        if prev_head_commit_id else None)
            if previous:
                t = previous
                old = t["refs"] or {}
                current = old.get("head_commit_id")
                active = self.bus._one("SELECT * FROM jobs WHERE thread_id=? AND status IN ('queued','running')",
                                       t["id"])
                # Idempotent only as a repeat of the request that pinned the thread's current
                # revision (#59); an older request is new work, or stale.
                repeat = self._pinned_request(t) == request
                same = self._revision_pass(t) if repeat else None
                if same and same["status"] == "done" and prev_head_commit_id == head_commit_id:
                    # Evidence since this pass asked for changes makes the same-head request a
                    # new pushback (#71), not a retry of the one that pinned this pass: a note
                    # since the verdict, or a focus if it hasn't been pushed back on yet.
                    mine = self.bus.last_verdict(t["id"], reviewer)
                    fresh = (old.get("pushbacks") or {}).get(reviewer) != current
                    if self._single_pushback(t, mine, base_commit_id, head_commit_id) and (
                            self._note_since(t, mine["id"]) is not None or (fresh and focus and focus.strip())):
                        repeat, same = False, None
                if repeat and same is None:
                    # Pinned to this request with no pass for it (a thread #59 left without
                    # one): queue that pass now, or report the one already running.
                    self._quota_check(reviewer, "reviewer", (old.get("peer_settings") or {}).get(reviewer))
                    autoresume.forget(self.bus, t["id"])
                    # Its reviewer's session may never have seen the previous head: full diff.
                    self.bus.set_thread(t["id"], status="open", refs=json.dumps({**old, "full_diff": True}))
                    job_row, created = self.bus.create_job(t["id"], "review",
                                                           {"mode": "followup", "round": t["round"]},
                                                           f"review:{t['id']}:{t['round']}")
                elif same and same["status"] in ("queued", "running", "done"):
                    job_row, created = same, False
                elif same:
                    raise BusError("REVIEW_FAILED", f"the pass for this request on {t['id']} {same['status']} "
                                   f"({same.get('error') or 'see the thread'}); ask the human user to run "
                                   f"`agent-bus resume {t['id']}` to retry it")
                elif active:
                    raise BusError("REVIEW_BUSY", f"{reviewer} is still on a previous pass ({t['id']}); "
                                   "wait for that verdict, then request this head again")
                elif current != prev_head_commit_id:
                    raise BusError("REVIEW_STALE", f"{t['id']} has moved on to head {str(current)[:12]}; "
                                   "use that as prev_head_commit_id")
                else:
                    self._check_budget_park(t)
                    # The same head again after a request for changes is a pushback (#71).
                    mine = self.bus.last_verdict(t["id"], reviewer)
                    pushback = self._single_pushback(t, mine, base_commit_id, head_commit_id)
                    if testing is None and (old.get("base_commit_id"), current) == (base_commit_id, head_commit_id):
                        refs["testing"] = old.get("testing")  # the same head keeps its testing note
                    if pushback:
                        self._check_pushback(t, reviewer, mine, focus, (old.get("pushbacks") or {}).get(reviewer)
                                             == current)
                        refs["pushbacks"] = {reviewer: current}
                        refs["pushback_round"] = t["round"] + 1
                    for k in ("followup_budget", "followup_limit"):
                        if old.get(k):
                            refs[k] = old[k]
                    refs["follow_ups"] = int(old.get("follow_ups") or 0) + 1
                    kept = (old.get("peer_settings") or {}).get(reviewer, {})
                    # The follow-up runs with the thread's settings plus any new ones.
                    self._quota_check(reviewer, "reviewer", {**kept, **override})
                    if focus:
                        self.bus.post(t["id"], self.agent, focus, session_id=self.session_id, recipient=reviewer)
                    refs["heads"] = [*self._heads(t), head_commit_id]
                    if kept or override:
                        refs["peer_settings"] = {reviewer: {**kept, **override}}
                    for k in ("auto_resumes", "debrief_after"):  # these still count for this thread
                        if old.get(k):
                            refs[k] = old[k]
                    autoresume.forget(self.bus, t["id"])  # this pass replaces the parked one
                    self.bus.set_thread(t["id"], status="open", round=t["round"] + 1,
                                        goal=f"Review {change_id or head_commit_id[:12]} in "
                                             f"{os.path.basename(repo_path)}")
                    # Scoped to the thread and round: create_job's global dedupe lookup must
                    # never hand back another thread's job (#59) or an earlier round's.
                    job_row, created = self.bus.create_job(t["id"], "review",
                                                           {"mode": "pushback" if pushback else "followup",
                                                            "round": t["round"] + 1},
                                                           f"review:{t['id']}:{t['round'] + 1}")
                    self.bus.set_thread(t["id"], refs=json.dumps(refs))
            else:
                t, job_row, created = self._fresh_review(reviewer, repo_path, refs, override, request)
        if created:
            worker.launch(self.bus, job_row["id"])
        job = self._job_info(job_row, created)
        t = self.bus.thread(t["id"])
        return {"thread": thread_summary(t), "job": job, "review": self.bus.review_standings(t["id"]),
                "next": f"Call wait(\"{t['id']}\") for the verdict, or check inbox later."}

    def _fresh_review(self, reviewer: str, repo_path: str, refs: dict, override: dict,
                      request: list) -> tuple[dict, dict, bool]:
        """Start a single-reviewer thread, or return the one this exact request started while it
        is still pinned to this request and its pass hasn't failed (a failed one is retried on a
        new thread).  Runs inside the caller's transaction."""
        base, prev, head = request
        # The requester is part of the key: two agents asking for the same review each get
        # their own thread, so each verdict reaches the agent that asked.  Follow-up passes
        # use thread-scoped keys, so this only ever finds fresh reviews (#59).
        key = f"review:{self.agent}:{repo_path}:{reviewer}:{prev or base}:{head}"
        for row in self.bus._all("SELECT thread_id FROM jobs WHERE dedupe_key=? GROUP BY thread_id"
                                 " ORDER BY MAX(created_at) DESC", key):
            t = self.bus.thread(row["thread_id"])
            if t["status"] == "cancelled" or self._pinned_request(t) != request:
                continue  # moved on to another diff: new work
            latest = self._revision_pass(t)
            if latest and latest["status"] in ("queued", "running", "done"):
                return t, latest, False  # idempotent, including a pass `agent-bus resume` restarted
        self._quota_check(reviewer, "reviewer", override)
        goal = f"Review {refs['change_id'] or head[:12]} in {os.path.basename(repo_path)}"
        t = self.bus.create_thread("review", goal, [self.agent, reviewer], initiator=self.agent,
                                   initiator_session=self.session_id, max_rounds=1,
                                   max_messages=self.config.max_messages, repo=repo_path, refs=refs)
        self.bus.set_thread(t["id"], round=1)
        # No dedupe inside create_job: its lookup can't tell whether a thread has moved on.
        job_row, created = self.bus.create_job(t["id"], "review", {"mode": "first", "round": 1}, None)
        self.bus.conn.execute("UPDATE jobs SET dedupe_key=? WHERE id=?", (key, job_row["id"]))
        return t, job_row, created

    def _budget_refs(self) -> dict:
        """A new review's follow-up count and budget (#71): a warning once the author has made
        ``review_followup_budget`` follow-up requests, the human after twice as many."""
        b = self.config.review_followup_budget
        return {"follow_ups": 0, "followup_budget": b, "followup_limit": 2 * b}

    def _over_budget(self, t: dict, resuming: bool = False) -> bool:
        """Whether a review is held for the human by its follow-up limit (#71): parked for
        that, or past the limit with someone still asking for changes (a worker can die
        between posting that verdict and parking the thread).  When ``resuming``, a pushback
        pass still due its verdict isn't: the resume reruns it within the limit."""
        if t["kind"] != "review":
            return False
        refs = t["refs"] or {}
        if t["status"] == "needs_human" and refs.get("budget_parked"):
            return True
        p = review_progress(t)
        if p["follow_ups"] < p["followup_limit"]:
            return False
        if resuming and self._pushback_pending(t):
            return False  # a pushback still to give its verdict: let it finish (a quota resume retries it)
        return bool(self.bus.review_standings(t["id"])["changes_requested"])

    def _pushback_pending(self, t: dict) -> bool:
        """Whether the current pass is a pushback still due its verdict (#71)."""
        return (t["refs"] or {}).get("pushback_round") == t["round"] and not any(
            m["round"] == t["round"] for m in self._verdicts(t))

    def _verdicts(self, t: dict) -> list[dict]:
        return [m for m in self.bus.history(t["id"])["messages"] if m["kind"] == "review_verdict"]

    def _check_budget_park(self, t: dict) -> None:
        """Refuse a follow-up on a review held for the human by its follow-up limit."""
        if self._over_budget(t):
            p = review_progress(t)
            if self._pushback_pending(t):
                # A resume reruns that pushback at the same limit; it doesn't raise it (#87).
                raise BusError("REVIEW_LIMIT_REACHED", f"{t['id']} is at its limit of {p['followup_limit']} "
                               "follow-up requests and its pushback pass is still due its verdict: wait for "
                               f"that verdict (if the pass stopped, ask the human user to run `agent-bus resume "
                               f"{t['id']}` to rerun it). If it still asks for changes, the review waits for the "
                               "human user before another revision")
            raise BusError("REVIEW_LIMIT_REACHED", f"{t['id']} still had changes requested after "
                           f"{p['follow_ups']} follow-up requests, so it waits for the human user: ask them to "
                           f"settle the open findings or run `agent-bus resume {t['id']}` to allow more "
                           "follow-ups, then request review again")

    @staticmethod
    def _single_pushback(t: dict, mine: dict | None, base: str, head: str) -> bool:
        """Whether a single-reviewer request is a same-head pushback (#71): the reviewer's
        verdict in the current pass asked for changes on this very base and head."""
        refs = t["refs"] or {}
        return (mine is not None and mine["round"] == t["round"] and mine["data"].get("verdict") == "request_changes"
                and head == refs.get("head_commit_id") == mine["data"].get("head_commit_id")
                and base == refs.get("base_commit_id") == mine["data"].get("base_commit_id", base))

    def _note_since(self, t: dict, after: int) -> int | None:
        """The requester's (or the human user's) newest note on ``t`` after message ``after``."""
        row = self.bus._one("SELECT MAX(id) AS id FROM messages WHERE thread_id=? AND kind='message'"
                            " AND sender IN (?,?) AND id>?", t["id"], self.agent, HUMAN, after)
        return row["id"] if row else None

    def _new_pushback(self, t: dict, focus: str | None) -> bool:
        """Whether a repeat of the same-head request that pinned ``t``'s revision is a new
        pushback (#71) rather than a retry: someone asks for changes and the author has sent
        a note since that verdict, or gives a focus for a reviewer not yet pushed back on."""
        standing = self.bus.review_standings(t["id"])
        if not standing["changes_requested"]:
            return False
        target = standing["changes_requested"][0]
        verdict = self.bus.last_verdict(t["id"], target)
        if (t["refs"] or {}).get("pushback_round") == t["round"] and verdict["round"] < t["round"]:
            return False  # the pushback pass this request started gave no verdict: a retry
        fresh = target not in ((t["refs"] or {}).get("pushbacks") or {})
        return self._note_since(t, verdict["id"]) is not None or bool(fresh and focus and focus.strip())

    def _check_pushback(self, t: dict, reviewer: str, verdict: dict, focus: str | None, used: bool) -> None:
        """A same-head re-request after ``reviewer`` asked for changes (#71) needs the author's
        evidence (a note sent since that verdict, or ``focus``), once per reviewer and head."""
        head = str(verdict["data"].get("head_commit_id"))[:12]
        if used:
            raise BusError("CHANGES_REQUESTED", f"{reviewer} already reconsidered its findings at head {head} "
                           "after a pushback and still asks for changes; push a fix, or ask the human user")
        if not (focus and focus.strip()) and self._note_since(t, verdict["id"]) is None:
            raise BusError("CHANGES_REQUESTED", f"{reviewer} asked for changes at head {head}; push a fix and "
                           "request review of the new head, or, to push back on a finding, `send` a note "
                           "here with your evidence and request the same head again")

    @staticmethod
    def _pinned_request(t: dict) -> list:
        """The request that pinned a single-reviewer thread's current revision: its refs
        hold that request's base, previous head and head (threads from before #59 too)."""
        refs = t["refs"] or {}
        return [refs.get("base_commit_id"), refs.get("prev_head_commit_id"), refs.get("head_commit_id")]

    def _revision_pass(self, t: dict) -> dict | None:
        """The newest job of a single-reviewer thread's current revision (its first or
        follow-up pass, or a resume's retry of it), or None if that revision has none.

        Jobs carry the round they ran for.  Older ones don't, and #59 could advance a thread
        without creating its follow-up job, leaving it open past its first round with no
        active job (a pass that ran would have resolved or parked it).  Only there does an
        older job have to show it ran for the current round: by a message it posted in that
        round, or by its old-style key, which named the request (unless the thread has made
        that same move before, so the key can't tell the rounds apart)."""
        # Review jobs only: a debrief (#91) is no pass of the review.
        row = self.bus._one("SELECT * FROM jobs WHERE thread_id=? AND kind='review' ORDER BY created_at DESC,"
                            " rowid DESC LIMIT 1", t["id"])
        if row is None:
            return None
        newest = self.bus._job(row)
        if "round" in newest["params"]:
            return newest if newest["params"]["round"] == t["round"] else None
        if newest["status"] in ("queued", "running") or t["status"] != "open" or t["round"] == 1:
            return newest
        refs = t["refs"] or {}
        heads, move = self._heads(t), (refs.get("prev_head_commit_id"), refs.get("head_commit_id"))
        old_key = f"review:{t['initiator']}:{t['repo']}:{refs.get('reviewer')}:{move[0]}:{move[1]}"
        if newest["params"].get("mode") == "followup" and newest["dedupe_key"] == old_key \
                and list(zip(heads, heads[1:])).count(move) == 1:
            return newest
        posted = self.bus._one("SELECT 1 FROM messages WHERE thread_id=? AND session_id=? AND round=?",
                               t["id"], f"job:{newest['id']}", t["round"])
        return newest if posted else None

    @staticmethod
    def _check_review_ids(base: str, head: str, prev: str | None, change_id: str | None) -> None:
        for name, value in (("base_commit_id", base), ("head_commit_id", head), ("prev_head_commit_id", prev)):
            if value is not None and not (isinstance(value, str) and COMMIT_RE.fullmatch(value)):
                raise ValueError(f"{name} must be a commit id: all 40 hex digits, or an unambiguous prefix of "
                                 f"at least 7 (got {str(value)[:50]!r}); {FULL_ID_HINT}")
        if change_id is not None and (not CHANGE_RE.fullmatch(change_id) or change_id != change_id.strip()):
            raise ValueError("change_id must be a short label: letters, digits, space and ._/#:+- only (no "
                             "parentheses or newlines), starting with a letter, digit or #, at most 100 "
                             f"characters, e.g. a jj change id (qpvuntsm), a branch (feature/x) or \"PR #12\" "
                             f"(got {change_id[:50]!r})")

    @staticmethod
    def _resolve_review_ids(repo: str, base: str, head: str, prev: str | None) -> tuple[str, str, str | None]:
        """Check a review's pinned commits in ``repo`` before anything is created (#73): resolve
        each id (or unambiguous prefix) to its full commit id, refuse one the repo doesn't have,
        and refuse a base that isn't an ancestor of the head, whose diff would also show
        unrelated changes.  A probe that gives no definite answer lets a full id through: the
        worker checks again and reports a missing commit on the thread."""
        tools = worker.commit_tools(repo)
        given = (("base_commit_id", base), ("head_commit_id", head), ("prev_head_commit_id", prev))
        if not tools:  # not a jj or git repo: nothing to resolve in, and the worker reports it
            for name, value in given:
                if value is not None and len(value) != 40:
                    raise ValueError(f"{name} must be all 40 hex digits: {repo} is not a jj or git repository, "
                                     "so a prefix can't be resolved")
            return base.lower(), head.lower(), prev and prev.lower()
        out: list[str | None] = []
        for name, value in given:
            if value is None:
                out.append(None)
                continue
            value = value.lower()
            state, ids = worker.resolve_commit(repo, tools, value)
            if state == "found":
                out.append(ids[0])
            elif state == "ambiguous":
                listed = f" ({', '.join(i[:12] for i in ids[:5])}{', ...' if len(ids) > 5 else ''})" if ids else ""
                raise BusError("REVISION_AMBIGUOUS", f"{name} {value} matches more than one commit in "
                               f"{repo}{listed}; pass a longer prefix or the full id: {FULL_ID_HINT}")
            elif state == "missing":
                gone = (" If the previous head is gone for good (amended and garbage-collected), request a "
                        "fresh review without prev_head_commit_id." if name == "prev_head_commit_id" else "")
                raise BusError("REVISION_NOT_FOUND", f"{repo} has no commit {value} ({name}); nothing was "
                               f"created. Check the id: {FULL_ID_HINT}. If the commit exists elsewhere, make it "
                               "available in this repository (push or fetch it; in a jj repo whose commit was "
                               f"made with git, `jj git import`), then request again.{gone}")
            elif len(value) == 40:
                out.append(value)
            else:
                raise BusError("REVISION_NOT_FOUND", f"could not resolve {name} {value} in {repo}: pass the "
                               f"full id ({FULL_ID_HINT})")
        base, head, prev = out
        if worker.is_ancestor(repo, tools, base, head) is False:
            raise BusError("REVISION_NOT_ANCESTOR", f"base {base[:12]} is not an ancestor of head {head[:12]} in "
                           f"{repo}, so the diff would also show changes that aren't part of this change; "
                           "nothing was created. Rebase the change onto that base, or pass the commit it is "
                           "based on as base_commit_id (`jj log --no-graph -r 'fork_point(<base> | <head>)' "
                           "-T commit_id` or `git merge-base <base> <head>`).")
        return base, head, prev

    def _request_reviews(self, reviewers: list[str], repo: str, base_commit_id: str, head_commit_id: str,
                         change_id: str | None, prev_head_commit_id: str | None, focus: str | None,
                         peer_settings: dict | None, thread: str | None = None,
                         testing: str | None = None) -> dict:
        """Several required reviewers on one thread, one per round (#47).

        The first request starts the first reviewer in the list.  Each approval hands the
        same head to the next reviewer without a valid approval; a request for changes
        waits for the author.  A follow-up (``prev_head_commit_id`` = the thread's current
        head) pins a new revision: a new head or base makes every earlier approval stale.
        It starts the reviewer after the last round's one, which gets the full diff and the
        earlier verdicts and replies or, if it has reviewed before, resumes its own
        conversation with the interdiff from the head it last reviewed (rebase-aware if the
        base has moved since, #75).

        Every decision, write and the reply's snapshot happen in one IMMEDIATE transaction."""
        order: list[str] = []
        for r in reviewers:
            if not self.config.is_participant(r) or r == self.agent:
                raise ValueError(f"each reviewer must be one of the other agents (got {r!r})")
            if r in order:
                raise ValueError(f"{r} is listed twice in reviewers")
            order.append(r)
        settings = {}
        for agent, value in (peer_settings or {}).items():
            if agent not in order:
                raise ValueError(f"peer_settings names {agent!r}, which isn't one of the reviewers")
            settings[agent] = self.config.check_settings(agent, value)
        for r in order:
            self.config.require_available(r, "reviewer")
        self._check_review_ids(base_commit_id, head_commit_id, prev_head_commit_id, change_id)
        if thread is not None and prev_head_commit_id is None:
            raise ValueError("`thread` names the review to continue; it needs prev_head_commit_id")
        repo_path = self._allowed_repo(repo)
        base_commit_id, head_commit_id, prev_head_commit_id = self._resolve_review_ids(
            repo_path, base_commit_id, head_commit_id, prev_head_commit_id)
        goal = f"Review {change_id or head_commit_id[:12]} in {os.path.basename(repo_path)} by {', '.join(order)}"
        request = [base_commit_id, prev_head_commit_id, head_commit_id]
        self.bus.reap()
        with self.bus.tx():
            if prev_head_commit_id:
                t = self._previous_review_set(order, repo_path, prev_head_commit_id, base_commit_id, head_commit_id,
                                              change_id, thread)
                if t:
                    job_row, created = self._followup_reviews(t, order, request, settings, focus, goal, change_id,
                                                              testing)
                else:  # no lineage: a fresh review that happens to name a previous head
                    t, job_row, created = self._fresh_reviews(order, repo_path, request, settings, focus, goal,
                                                              change_id, testing)
            else:
                t, job_row, created = self._fresh_reviews(order, repo_path, request, settings, focus, goal,
                                                          change_id, testing)
            t = self.bus.thread(t["id"])
            st = self.bus.review_standings(t["id"])
        if created:
            worker.launch(self.bus, job_row["id"])
        job = self._job_info(job_row, created) if job_row else None
        if job is None:
            nxt = f"Every required reviewer has already approved head {head_commit_id[:12]}; nothing to run."
        else:
            nxt = (f"Call wait(\"{t['id']}\"): {t['refs']['reviewer']} reviews first; each approval hands the "
                   "head to the next reviewer, and a request for changes waits for your fix.")
        return {"thread": thread_summary(t), "job": job, "review": st, "next": nxt}

    def _fresh_reviews(self, order: list[str], repo_path: str, request: list, settings: dict,
                       focus: str | None, goal: str, change_id: str | None,
                       testing: str | None = None) -> tuple[dict, dict, bool]:
        """Start a several-reviewer thread, or return the one this exact request started while
        it is still pinned to this diff (retrying its pass if that failed)."""
        base, prev, head = request
        key = f"reviews:{self.agent}:{repo_path}:{','.join(order)}:{prev or base}:{head}"
        for row in self.bus._all("SELECT thread_id FROM jobs WHERE dedupe_key=? GROUP BY thread_id"
                                 " ORDER BY MAX(created_at) DESC", key):
            t = self.bus.thread(row["thread_id"])
            if (t["status"] == "cancelled" or (t["refs"] or {}).get("request") != request
                    or (t["refs"] or {}).get("change_id") != change_id):
                continue  # moved on to another diff, or another change's review: new work
            latest = self._revision_job(t)
            if latest and latest["status"] in ("queued", "running", "done"):
                return t, latest, False  # idempotent, including a pass `agent-bus resume` restarted
            # Its pass failed or was cancelled: requesting it again retries it on the same thread.
            if self.bus.active_job(t["id"]):  # a debrief (#91): its job would stand in for the retry
                raise BusError("REVIEW_BUSY", f"{t['id']} has a debrief running; request this review again "
                               "once it has finished")
            reviewer = t["refs"]["reviewer"]
            self._quota_check(reviewer, "reviewer", ((t["refs"].get("peer_settings") or {}).get(reviewer)))
            autoresume.forget(self.bus, t["id"])  # this retry replaces any planned one
            self.bus.set_thread(t["id"], status="open")
            job_row, created = self.bus.create_job(t["id"], "review", {"mode": "retry"},
                                                   f"reviews:{t['id']}:retry:{time.time():.6f}")
            return t, job_row, created
        first = order[0]
        self._quota_check(first, "reviewer", settings.get(first))
        refs: dict[str, Any] = {"reviewers": order, "reviewer": first, "repo": repo_path, "change_id": change_id,
                                "base_commit_id": base, "head_commit_id": head, "prev_head_commit_id": None,
                                "focus": focus, "opening_focus": focus, "testing": testing, "heads": [head],
                                "revision": 0,
                                "request": request, **self._budget_refs()}
        if settings:
            refs["peer_settings"] = settings
        t = self.bus.create_thread("review", goal, [self.agent, *order], initiator=self.agent,
                                   initiator_session=self.session_id, max_rounds=1,
                                   max_messages=self.config.max_messages, repo=repo_path, refs=refs)
        self.bus.set_thread(t["id"], round=1)
        # No dedupe inside create_job: its lookup can't tell whether a thread has moved on.
        job_row, created = self.bus.create_job(t["id"], "review", {"mode": "first"}, None)
        self.bus.conn.execute("UPDATE jobs SET dedupe_key=? WHERE id=?", (key, job_row["id"]))
        refs["request_job"] = job_row["id"]
        self.bus.set_thread(t["id"], refs=json.dumps(refs))
        return t, job_row, created

    def _followup_reviews(self, t: dict, order: list[str], request: list, settings: dict,
                          focus: str | None, goal: str, change_id: str | None,
                          testing: str | None = None) -> tuple[dict | None, bool]:
        base, prev, head = request
        old = t["refs"]
        current = old.get("head_commit_id")
        active = self.bus._one("SELECT * FROM jobs WHERE thread_id=? AND status IN ('queued','running')", t["id"])
        # Idempotent only as a repeat of the request that pinned the thread's current
        # revision; an older request (say, going back to an earlier base) is new work.
        repeat = old.get("request") == request
        if repeat and old.get("request_job") is None:
            return None, False  # it pinned a revision with nothing to run
        same = self._revision_job(t) if repeat else None
        if same and same["status"] == "done" and prev == head and self._new_pushback(t, focus):
            # The same head again, after its pass asked for changes and the author sent
            # evidence: a pushback (#71), not a repeat of the request that pinned it.
            same = None
        if same and same["status"] in ("queued", "running", "done"):
            return same, False
        if same and self.bus.review_standings(t["id"])["complete"]:
            return self._finish_complete(t, head), False  # every approval is in; it only needs resolving
        if same:
            raise BusError("REVIEW_FAILED", f"the pass for this request on {t['id']} {same['status']} "
                           f"({same.get('error') or 'see the thread'}); ask the human user to run "
                           f"`agent-bus resume {t['id']}` to retry it")
        if active:
            raise BusError("REVIEW_BUSY", f"{old.get('reviewer')} is still reviewing ({t['id']}); "
                           "wait for that verdict, then request this head again")
        if current != prev:
            raise BusError("REVIEW_STALE", f"{t['id']} has moved on to head {str(current)[:12]}; "
                           "use that as prev_head_commit_id")
        self._check_budget_park(t)
        standing = self.bus.review_standings(t["id"])
        # A new base or head is a new revision: every earlier verdict goes stale, even one
        # given on this same base..head before (A -> B -> A).
        new_revision = (base, head) != (old.get("base_commit_id"), current)
        pushback = None
        if not new_revision and standing["changes_requested"]:
            # The same head after a request for changes: a pushback (#71).  The first reviewer
            # that asked for changes reconsiders; every approval of this revision stands.
            pushback = standing["changes_requested"][0]
            verdict = self.bus.last_verdict(t["id"], pushback)
            self._check_pushback(t, pushback, verdict, focus, pushback in (old.get("pushbacks") or {}))
        kept = old.get("peer_settings") or {}
        merged = {a: {**kept.get(a, {}), **settings.get(a, {})} for a in order if kept.get(a) or settings.get(a)}
        heads = self._heads(t)
        revision = old.get("revision", 0) + new_revision
        # A testing note describes a head: a pushback on the same one keeps it unless it brings its own.
        testing = old.get("testing") if testing is None and not new_revision else testing
        refs = {**old, "base_commit_id": base, "head_commit_id": head, "focus": focus, "testing": testing,
                "prev_head_commit_id": None,
                "change_id": change_id if change_id is not None else old.get("change_id"),
                "revision": revision, "request": request,
                "heads": heads if head in heads else [*heads, head]}
        refs.pop("peer_settings", None)
        if new_revision:
            refs.pop("pushbacks", None)  # one pushback per reviewer per revision
        if merged:
            refs["peer_settings"] = merged
        # Pin the new revision first: the standings (and so the next reviewer) are judged
        # against it.  Any refusal below rolls this back with the transaction.
        self.bus.set_thread(t["id"], refs=json.dumps(refs))
        nxt = pushback or self.bus.next_reviewer(t["id"], after=old.get("reviewer"))
        job_row, created = None, False
        if nxt is None:
            # Everyone has approved this revision (so base and head are unchanged): leave the
            # thread pinned to the request that got it there, and resolve it if a worker died
            # before doing so.
            self.bus.set_thread(t["id"], refs=json.dumps(old))
            return self._finish_complete(t, head), False
        self._quota_check(nxt, "reviewer", merged.get(nxt))
        if focus:
            self.bus.post(t["id"], self.agent, focus, session_id=self.session_id)
        # Its own last head, for the interdiff (rebase-aware if that verdict was on another
        # base, #75); the worker falls back to the full diff when it can't send one.
        last = self.bus.review_standings(t["id"])["reviewers"][nxt]
        refs.update(reviewer=nxt, prev_head_commit_id=last["head_commit_id"])
        if new_revision or pushback:  # an author follow-up: it counts against the budget (#71)
            refs["follow_ups"] = int(old.get("follow_ups") or 0) + 1
        if pushback:
            refs["pushbacks"] = {**(old.get("pushbacks") or {}), pushback: True}
            refs["pushback_round"] = t["round"] + 1
        autoresume.forget(self.bus, t["id"])  # this pass replaces the parked one
        self.bus.set_thread(t["id"], status="open", round=t["round"] + 1, goal=goal)
        # Scoped to the thread and round: create_job's global dedupe lookup must never
        # hand back another thread's job or an earlier round's.
        job_row, created = self.bus.create_job(t["id"], "review", {"mode": "pushback" if pushback else "followup"},
                                               f"reviews:{t['id']}:{t['round'] + 1}")
        # The job this revision's request started (None: nothing to run), so a repeat
        # reports on this revision's work, never an older revision's.
        refs["request_job"] = job_row["id"] if job_row else None
        self.bus.set_thread(t["id"], refs=json.dumps(refs))
        return job_row, created

    def _finish_complete(self, t: dict, head: str) -> None:
        """Resolve a review whose approvals are all in (a worker died before it could), with
        the completion notice.  Returns None: there's no job to report."""
        if self.bus.transition(t["id"], "resolved", frm=("open", "needs_human")):
            self.bus.post(t["id"], SYSTEM, f"Review complete: every required reviewer approved head "
                          f"{head[:12]}.\n{worker.format_review(self.bus.review_standings(t['id']))}",
                          kind="status", recipient=None if t["initiator"] == HUMAN else t["initiator"])
        return None

    def _revision_job(self, t: dict) -> dict | None:
        """The newest job since the one that pinned the thread's current revision (a resume
        retry counts), or None if there's none."""
        pin = self.bus._one("SELECT created_at FROM jobs WHERE id=?", (t["refs"] or {}).get("request_job"))
        if pin is None:
            return None
        row = self.bus._one("SELECT * FROM jobs WHERE thread_id=? AND kind='review' AND created_at>=?"
                            " ORDER BY created_at DESC, rowid DESC LIMIT 1", t["id"], pin["created_at"])
        return self.bus._job(row) if row else None

    def _previous_review_set(self, reviewers: list[str], repo: str, prev_head: str, base: str, head: str,
                             change_id: str | None, thread: str | None) -> dict | None:
        """The requester's several-reviewer thread (same reviewers, same repo) to continue.

        ``thread`` names it outright.  Otherwise, among the threads that have seen
        ``prev_head`` (with a ``change_id``: those with that label, else unlabelled ones):
        the one this exact request pinned (a repeat), else the only one.  No guessing
        between several: that is REVIEW_AMBIGUOUS, settled by ``thread`` or ``change_id``."""
        if thread is not None:
            t = self.bus.thread(thread)
            refs = t["refs"] or {}
            if (t["kind"] != "review" or t["initiator"] != self.agent or t["repo"] != repo
                    or set(refs.get("reviewers") or []) != set(reviewers) or t["status"] == "cancelled"):
                raise ValueError(f"{thread} is not your review of this repo by {', '.join(reviewers)}")
            if prev_head not in self._heads(t):
                raise BusError("REVIEW_STALE", f"{thread} has never been at head {prev_head[:12]}")
            return t
        found = []
        for r in self.bus._all("SELECT id FROM threads WHERE kind='review' AND initiator=? AND repo=?"
                               " ORDER BY created_at DESC", self.agent, repo):
            t = self.bus.thread(r["id"])
            refs = t["refs"] or {}
            if set(refs.get("reviewers") or []) == set(reviewers) and t["status"] != "cancelled" \
                    and prev_head in self._heads(t):
                found.append(t)
        if change_id is not None:
            # The label is the strongest signal: threads with it, else unlabelled ones (the
            # request adds the label), never threads labelled otherwise.
            found = ([t for t in found if t["refs"].get("change_id") == change_id]
                     or [t for t in found if t["refs"].get("change_id") is None])
        exact = [t for t in found if t["refs"].get("request") == [base, prev_head, head]]
        if exact:
            found = exact
        if len(found) > 1:
            raise BusError("REVIEW_AMBIGUOUS", "several of your reviews by these reviewers have been at head "
                           f"{prev_head[:12]} ({', '.join(t['id'] for t in found)}); pass `thread` (or a "
                           "distinguishing change_id) to say which one to continue")
        return found[0] if found else None

    @staticmethod
    def _heads(t: dict) -> list[str]:
        refs = t["refs"] or {}
        return list(refs.get("heads") or [refs.get("head_commit_id")])

    def _previous_review(self, reviewer: str, repo: str, prev_head: str, request: list) -> dict | None:
        """The requester's review thread with this reviewer to continue: the one this exact
        request pinned (a repeat, even if a newer thread has since seen ``prev_head``), else
        the newest that has reviewed ``prev_head`` at any point (not only as its current
        head), so a request based on a head the thread has since moved past is reported as
        stale instead of starting a new thread."""
        mine = []
        for r in self.bus._all("SELECT id FROM threads WHERE kind='review' AND initiator=? AND repo=?"
                               " ORDER BY created_at DESC", self.agent, repo):
            t = self.bus.thread(r["id"])
            if (t["refs"] or {}).get("reviewer") == reviewer and not (t["refs"] or {}).get("reviewers") \
                    and t["status"] != "cancelled":
                mine.append(t)
        exact = [t for t in mine if self._pinned_request(t) == request]
        if exact:
            # A fresh review that named this previous head without any lineage is pinned to
            # this request but never saw prev_head: its repeat is the fresh path's.
            return exact[0] if prev_head in self._heads(exact[0]) else None
        return next((t for t in mine if prev_head in self._heads(t)), None)

    def debrief(self, thread: str, question: str | None = None, peers: list[str] | None = None) -> dict:
        """Ask a finished review's or discussion's headless peers for feedback, each in its own
        resumed session on the thread, with the same hardening and usage accounting as any turn
        (#91).  Their answers come back as ``debrief`` messages; the thread's status and
        verdicts never change.  One per peer per thread until the human user resumes it."""
        self.bus.expire_discussion(thread)  # an overdue discussion is finished (#74)
        t = self.bus.thread(thread)
        if t["kind"] not in ("review", "discussion"):
            raise ValueError(f"{thread} is a {t['kind']} thread; only reviews and discussions can be debriefed")
        if self.agent not in (t["initiator"], HUMAN):
            raise BusError("NOT_INITIATOR", "only the thread's initiator (or the human user) can debrief it")
        if question is not None and not question.strip():
            raise ValueError("question is empty; leave it out for the default question")
        headless = [p for p in t["participants"] if p != t["initiator"]]
        named = peers is not None
        if named:
            if not peers:
                raise ValueError("peers is empty; leave it out to debrief every peer")
            for p in peers:
                if p not in headless:
                    raise ValueError(f"{p!r} is not a headless peer on {thread} ({', '.join(headless)})")
            if len(set(peers)) != len(peers):
                raise ValueError("a peer is listed twice")
        if t["repo"]:
            self._allowed_repo(t["repo"])  # still allow-listed: the peers run there
        self.bus.reap()
        with self.bus.tx():
            t = self.bus.thread(thread)  # decided again inside the transaction
            if t["status"] not in ("resolved", "needs_human"):
                raise BusError("THREAD_NOT_FINISHED", f"{thread} is {t['status']}; only a resolved or needs_human "
                               "thread can be debriefed")
            if self.bus.active_job(thread):
                raise BusError("THREAD_BUSY", f"{thread} has a job running; wait for it to finish")
            done = self._debriefed(t)
            chosen, skipped = [], {}
            for p in (peers if named else headless):
                why = None
                if p in done:
                    why = ("DEBRIEF_USED", f"{p} has already given its debrief on {thread} (one per peer, until the "
                           "human user resumes the thread)")
                elif self.bus.peer_session(thread, p) is None:
                    why = ("NO_SESSION", f"{p} has no stored session on {thread} to resume, and a fresh session "
                           "would not remember the thread")
                else:
                    try:
                        self.config.require_available(p, "peer")
                    except ValueError as e:  # no longer configured
                        why = ("AGENT_UNAVAILABLE", str(e))
                    except BusError as e:
                        why = (e.code, e.message)
                    if why is None:
                        block = self._quota_block(p, ((t["refs"] or {}).get("peer_settings") or {}).get(p))
                        if block:
                            why = (quota.code(block), quota.message(p, "peer", block, self.config))
                if why and named:
                    raise BusError(*why)
                if why:
                    skipped[p] = why
                else:
                    chosen.append(p)
            if not chosen:
                codes = {c for c, _ in skipped.values()}
                raise BusError(next(iter(codes)) if len(codes) == 1 else "DEBRIEF_UNAVAILABLE",
                               "no peer can be debriefed: " + " ".join(m + "." for _, m in skipped.values()))
            params = {"peers": chosen, "question": question.strip() if question else None, "asker": self.agent}
            job, created = self.bus.create_job(thread, "debrief", params, None)
        if created:
            worker.launch(self.bus, job["id"])
        res: dict[str, Any] = {"thread": thread_summary(self.bus.thread(thread)), "job": self._job_info(job, created),
                               "peers": chosen}
        if skipped:
            res["skipped_peers"] = {p: m for p, (_, m) in skipped.items()}
        res["next"] = (f"Call wait(\"{thread}\") for the replies (kind \"debrief\"); they don't change the "
                       "thread's status or verdicts.")
        return res

    def _debriefed(self, t: dict) -> set[str]:
        """Peers whose debrief on ``t`` already counts: an answer since the human user last
        resumed the thread.  A debrief that got no answer can be asked for again."""
        after = int((t["refs"] or {}).get("debrief_after") or 0)
        return {r["sender"] for r in self.bus._all("SELECT DISTINCT sender FROM messages WHERE thread_id=?"
                                                   " AND kind='debrief' AND id>?", t["id"], after)}

    def resume(self, thread: str, extra_rounds: int = 2) -> dict:
        """Human only: lift the turn cap and give a stalled discussion more rounds."""
        if self.agent != HUMAN:
            raise BusError("HUMAN_ONLY", "only the human user can resume a thread")
        # One transaction from reading the thread to reopening it, so an automatic resume
        # (or its worker) can't change it in between; the worker starts after the commit.
        self._launch_later = []
        try:
            with self.bus.tx():
                res = self._resume(thread, extra_rounds)
            launches = self._launch_later
        finally:
            self._launch_later = None
        for job in launches:
            if job["created"]:
                worker.launch(self.bus, job["id"])
        return res

    def _resume(self, thread: str, extra_rounds: int) -> dict:
        t = self.bus.thread(thread)
        if t["status"] in ("resolved",):
            raise BusError("THREAD_CLOSED", "thread is already resolved")
        job = self.bus.active_job(thread)
        if job and job["kind"] == "debrief":
            # Its job would stand in for the resumed thread's own (one job per thread).
            raise BusError("THREAD_BUSY", f"a debrief of {thread} is running; resume it once that has finished")
        if job and job["status"] == "running" and t["kind"] == "discussion" and (overdue(t) or t["status"] != "open"):
            # A live worker is finishing past the deadline, or has parked the thread and not
            # yet exited: a resume now would let its turn or notice land in the new window,
            # and the new job would be deduplicated against the old one (#74).  A queued job
            # that never started is what a resume relaunches, so that one doesn't block.
            raise BusError("THREAD_BUSY", f"{thread}'s worker is still finishing; resume it once that job "
                           "has ended")
        if job and job["status"] == "queued" and t["kind"] == "discussion" and t["deadline"] \
                and t["deadline"] <= time.time():
            # A job that never started before the deadline: its mode (an advance runs at most
            # one round) shouldn't stand in for the resume's own job (#74).
            self.bus.conn.execute("UPDATE jobs SET status='cancelled', error='the discussion deadline passed "
                                  "before it started', updated_at=? WHERE id=? AND status='queued'",
                                  (time.time(), job["id"]))
        # Each peer may be debriefed once more after this resume (#91).
        t = {**t, "refs": {**(t["refs"] or {}), "debrief_after": self.bus.last_message_id(thread)}}
        overrides = (t["refs"] or {}).get("peer_settings") or {}
        peers = self._resume_peers(t)
        # The human is retrying now: forget cached out-of-usage blocks for these peers, so
        # a stale or wrong one doesn't stop the retry.  A real limit is found again.
        cleared = [p for p in peers if quota.clear_for(self.bus, self.config, p, quota.effective_model(
            self.config, p, self.config.peer_settings(p, overrides.get(p))))]
        # And the format drift that kept peers out of this discussion's rounds: the human
        # resumes once the adapter handles the new format, and a real drift is found again.
        drifted = self.bus.clear_peer_drift(thread)
        # This resume replaces any automatic one, and restarts the count of those.
        autoresume.forget(self.bus, thread)
        note = ((f"Resumed by the human user with {extra_rounds} more round(s)."
                 if t["kind"] == "discussion" else "Resumed by the human user.")
                + (f" It allows {self.config.review_followup_budget} more follow-up request(s); request review "
                   "again to continue." if self._over_budget(t, resuming=True) else "")
                + (f" Cleared out-of-usage state for {', '.join(cleared)}." if cleared else "")
                + (f" Cleared format-drift state for {', '.join(drifted)}." if drifted else ""))
        return self._reopen(t, extra_rounds, note, auto_resumes=0, key=f"{time.time():.0f}")

    def auto_resume(self, thread: str, park_id: str, agents: list[str], extra_rounds: int = 2) -> dict:
        """Resume a thread parked only because ``agents`` ran out of usage, now that their
        blocks have expired (see autoresume.resume_one, which calls this inside the
        transaction that claims the planned resume)."""
        t = self.bus.thread(thread)
        self._resume_peers(t)  # each must still be available
        if t["repo"]:
            self._allowed_repo(t["repo"])  # and its repo still allow-listed
        n = int((t["refs"] or {}).get("auto_resumes") or 0) + 1
        note = (f"Resumed automatically: the usage limit that stopped {', '.join(agents)} has reset "
                f"(automatic resume {n} of {self.config.auto_resume_max}"
                + (f", with {extra_rounds} more round(s))." if t["kind"] == "discussion" else ")."))
        # The caller starts the worker once its transaction has committed the job.
        self._launch_later = []
        try:
            self._reopen(t, extra_rounds, note, auto_resumes=n, key=f"auto:{park_id}")
            return self._launch_later[0] if self._launch_later else {}
        finally:
            self._launch_later = None

    def _resume_peers(self, t: dict) -> list[str]:
        """The headless peers or reviewers a resume of ``t`` would run (each must be available)."""
        if t["kind"] == "discussion":
            peers = [p for p in t["participants"] if p != t["initiator"]]
        elif (t["refs"] or {}).get("reviewers"):
            # A complete review only needs resolving, which runs no reviewer.
            peers = [] if self.bus.review_standings(t["id"])["complete"] else list(t["refs"]["reviewers"])
        else:
            peers = [r for r in [(t["refs"] or {}).get("reviewer")] if r]
        for p in peers:
            self.config.require_available(p, "peer" if t["kind"] == "discussion" else "reviewer")
        return peers

    def _reopen(self, t: dict, extra_rounds: int, note: str, auto_resumes: int, key: str) -> dict:
        thread = t["id"]
        fields: dict[str, Any] = {"status": "open"}
        count = self.bus.agent_messages(thread)
        fields["max_messages"] = max(t["max_messages"], count + self.config.max_messages)
        if t["kind"] == "discussion":
            fields["max_rounds"] = max(t["max_rounds"], t["round"]) + extra_rounds
            fields["deadline"] = time.time() + self.config.deadline_minutes * 60
        parked = self._over_budget(t, resuming=True)
        if t["refs"] is not None or auto_resumes:
            refs = {**(t["refs"] or {}), "auto_resumes": auto_resumes}
            if parked:
                # Over its follow-up limit (#71): allow another budget's worth of follow-ups,
                # then wait for the author's next request (no reviewer reruns on the same head).
                refs.pop("budget_parked", None)
                refs["followup_limit"] = review_progress(t)["follow_ups"] + self.config.review_followup_budget
            if t["kind"] == "review" and not refs.get("reviewers") and self._revision_pass(t) is None:
                # A round #59 left without a pass: its reviewer's session may never have seen
                # the previous head, so the retry gets the full diff.
                refs["full_diff"] = True
            fields["refs"] = json.dumps(refs)
        self.bus.set_thread(thread, **fields)
        res: dict[str, Any] = {}
        if t["kind"] == "discussion":
            res["job"] = self._dispatch(thread, "discussion", {"mode": "start_later"}, f"{thread}:resume:{key}")
        elif t["kind"] == "review" and not parked and not self.bus.active_job(thread) \
                and not self._review_settled(t):
            res["job"] = self._dispatch(thread, "review", {"mode": "retry", "round": t["round"]},
                                        f"{thread}:retry:{key}")
        # After the job lookups, whose reaping may post an older job's notice: this one is last.
        self.bus.post(thread, SYSTEM, note, kind="status")
        res["thread"] = thread_summary(self.bus.thread(thread))
        return res

    def _review_settled(self, t: dict) -> bool:
        """Nothing for a retry to do: the single pass has its verdict on the current head."""
        if (t["refs"] or {}).get("reviewers"):
            # Always worth a job: it runs a missing round, hands an approval on, or (if a
            # worker died before doing so) resolves a complete review and posts the outcome.
            return False
        return self._has_verdict_for_head(t)

    def _has_verdict_for_head(self, t: dict) -> bool:
        """A verdict on the current head in the current round (a thread can return to a head
        an earlier round reviewed; that verdict isn't this round's)."""
        head = (t["refs"] or {}).get("head_commit_id")
        return any(m["kind"] == "review_verdict" and m["data"].get("head_commit_id") == head
                   and m["round"] == t["round"] for m in self.bus.history(t["id"])["messages"])

    def cancel(self, thread: str) -> dict:
        if self.agent != HUMAN:
            raise BusError("HUMAN_ONLY", "only the human user can cancel a thread")
        pids = self.bus.cancel_thread(thread)
        autoresume.forget(self.bus, thread)
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

    # Set while a caller will start the worker itself, after its transaction commits.
    _launch_later: list | None = None

    def _dispatch(self, thread: str, kind: str, params: dict, dedupe_key: str) -> dict:
        job, created = self.bus.create_job(thread, kind, params, dedupe_key)
        if self._launch_later is not None:
            self._launch_later.append({"id": job["id"], "created": created})
        elif created:
            worker.launch(self.bus, job["id"])
        return self._job_info(job, created)

    @staticmethod
    def _job_info(job: dict, created: bool) -> dict:
        return {"id": job["id"], "thread": job["thread_id"], "status": job["status"],
                **({} if created else {"deduplicated": True})}
