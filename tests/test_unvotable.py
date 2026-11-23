"""Peers that can no longer vote in a discussion: format drift reuses the out-of-usage rules
(issues #39 and #40; agreed on agent-bus thread t-606fcd, P4)."""
import json
import time

import pytest

from agent_bus import config as cfg
from agent_bus import unvotable
from agent_bus.db import HUMAN, SYSTEM, BusError
from agent_bus.service import Service
from agent_bus.worker import Worker


def run_job(bus, tid):
    job = bus.jobs(tid)[-1]
    assert Worker(bus, job["id"]).run() == 0
    return bus.job(job["id"])


def calls(env, agent):
    if not env["log"].exists():
        return []
    return [c for c in map(json.loads, env["log"].read_text().splitlines()) if c["agent"] == agent]


def system(bus, tid):
    return [m["body"] for m in bus.history(tid)["messages"] if m["sender"] == SYSTEM]


def advance_setup(bus, peers):
    """An agent-initiated discussion after round 1, with no job running."""
    me = Service(bus, "claude", bus.register_session("claude", env={}))
    tid = me.start_discussion("q", peers=list(peers), max_rounds=4)["thread"]["id"]
    for j in bus.jobs(tid):
        bus.conn.execute("UPDATE jobs SET status='done' WHERE id=?", (j["id"],))
    bus.set_thread(tid, round=1, blind=0)
    return me, tid


def test_drift_in_round_one_without_a_proposal_needs_human_at_once(env, no_spawn, monkeypatch):
    bus = env["bus"]
    monkeypatch.setenv("FAKE_SILENT", "codex")
    tid = Service(bus, HUMAN).start_discussion("q", peers=["claude", "codex"], max_rounds=4)["thread"]["id"]
    run_job(bus, tid)
    t = bus.thread(tid)
    assert t["status"] == "needs_human" and t["round"] == 1  # no rounds spent on it
    assert len(calls(env, "codex")) == 1
    assert set(bus.peer_drift(tid)) == {"codex"}
    bodies = system(bus, tid)
    assert len([b for b in bodies if b.startswith("PEER_FORMAT_CHANGED: codex")]) == 1
    assert "No consensus is possible while codex can't vote again" in bodies[-1]
    assert "no position from: codex" in bodies[-1]


def test_drift_later_keeps_the_peers_vote_and_is_not_rerun(env, no_spawn, monkeypatch):
    bus = env["bus"]
    monkeypatch.setenv("FAKE_SILENT_LATER", "codex")
    tid = Service(bus, HUMAN).start_discussion("q", peers=["claude", "codex", "antigravity"],
                                               max_rounds=4)["thread"]["id"]
    run_job(bus, tid)
    t = bus.thread(tid)
    # Round 1, then the one turn that drifted; later rounds and later jobs skip it.
    assert len(calls(env, "codex")) == 2
    standing = bus.standings(tid)["codex"]
    assert standing["stance"] == "agree"  # its own proposal still counts
    if t["status"] == "resolved":
        assert t["final_proposal"] == standing["proposal_id"]
    else:
        assert t["status"] == "needs_human"
    assert len([b for b in system(bus, tid) if b.startswith("PEER_FORMAT_CHANGED")]) == 1


def test_advance_with_a_drifted_peer_that_backs_nothing_is_refused(env, no_spawn):
    bus = env["bus"]
    me, tid = advance_setup(bus, ["codex", "antigravity"])
    bus.post(tid, "antigravity", "agy plan", kind="proposal")
    bus.set_peer_drift(tid, "codex", "fake-codex 0.0.0", "without a reply")
    with pytest.raises(BusError) as e:
        me.advance_discussion(tid)
    assert e.value.code == "PEER_FORMAT_CHANGED"
    assert "no consensus is possible" in e.value.message and f"agent-bus resume {tid}" in e.value.message
    assert "fake-codex 0.0.0" in e.value.message


def test_drifted_peers_backing_different_proposals_is_a_stalemate(env, no_spawn):
    bus = env["bus"]
    tid = Service(bus, HUMAN).start_discussion("q", peers=["claude", "codex", "antigravity"])["thread"]["id"]
    bus.set_thread(tid, round=2, blind=0)
    bus.post(tid, "codex", "codex plan", kind="proposal")
    bus.post(tid, "antigravity", "agy plan", kind="proposal")
    for a in ("codex", "antigravity"):
        bus.set_peer_drift(tid, a, "v", "without a reply")
    assert set(unvotable.stalemate(bus, cfg.Config.load(), bus.thread(tid))) == {"codex", "antigravity"}
    bus.post(tid, "antigravity", "v", kind="vote", data={"on": "P1", "stance": "agree", "reasons": "r"})
    assert unvotable.stalemate(bus, cfg.Config.load(), bus.thread(tid)) == {}  # both back P1 now


def test_mixed_reasons_on_one_proposal_leave_the_initiator_to_settle_it(env, no_spawn):
    bus = env["bus"]
    me, tid = advance_setup(bus, ["codex", "antigravity"])
    bus.post(tid, "codex", "codex plan", kind="proposal")  # P1
    bus.post(tid, "antigravity", "agree", kind="vote", data={"on": "P1", "stance": "agree", "reasons": "r"})
    me.send("claude plan", thread=tid, kind="proposal")  # P2
    bus.set_peer_drift(tid, "codex", "fake-codex 0.0.0", "without a reply")
    bus.set_quota_block("antigravity", time.time() + 600, "heuristic", "q")
    with pytest.raises(BusError) as e:
        me.advance_discussion(tid)
    assert e.value.code == "PEERS_CANNOT_VOTE"
    assert "Every peer backs P1" in e.value.message
    assert "antigravity is out of usage" in e.value.message and "peer codex (fake-codex 0.0.0)" in e.value.message
    assert bus.thread(tid)["round"] == 1
    me.send("ok", thread=tid, kind="vote", on="P1", stance="agree")
    assert bus.thread(tid)["status"] == "resolved"


def test_drift_after_advance_is_queued_spends_no_round(env, no_spawn):
    bus = env["bus"]
    me, tid = advance_setup(bus, ["codex"])
    bus.post(tid, "codex", "codex plan", kind="proposal")  # P1
    me.send("claude plan", thread=tid, kind="proposal")  # P2
    me.advance_discussion(tid)
    bus.set_peer_drift(tid, "codex", "fake-codex 0.0.0", "without a reply")  # recorded meanwhile
    run_job(bus, tid)
    t = bus.thread(tid)
    assert t["status"] == "open" and t["round"] == 1
    assert calls(env, "codex") == []
    bodies = system(bus, tid)
    assert any(b.startswith("PEER_FORMAT_CHANGED: codex (fake-codex 0.0.0)") for b in bodies)
    assert "All peers agree on P1. Waiting for claude" in bodies[-1]


def test_resume_clears_drift_and_reruns_the_peer(env, no_spawn, monkeypatch):
    bus = env["bus"]
    monkeypatch.setenv("FAKE_SILENT", "codex")
    tid = Service(bus, HUMAN).start_discussion("q", peers=["claude", "codex"], max_rounds=2)["thread"]["id"]
    run_job(bus, tid)
    assert bus.thread(tid)["status"] == "needs_human" and bus.peer_drift(tid)
    monkeypatch.delenv("FAKE_SILENT")  # the adapter handles the new format now
    Service(bus, HUMAN).resume(tid)
    assert bus.peer_drift(tid) == {}
    assert "Cleared format-drift state for codex." in system(bus, tid)[-1]
    run_job(bus, tid)
    assert len(calls(env, "codex")) == 2


def test_drift_in_one_discussion_does_not_skip_the_peer_elsewhere(env, no_spawn, monkeypatch):
    bus = env["bus"]
    tid = Service(bus, HUMAN).start_discussion("q", peers=["claude", "codex"])["thread"]["id"]
    other = Service(bus, HUMAN).start_discussion("q2", peers=["claude", "codex"])["thread"]["id"]
    bus.set_peer_drift(other, "codex", "v", "without a reply")
    run_job(bus, tid)
    assert calls(env, "codex") and bus.thread(tid)["status"] == "resolved"


def test_incomplete_turns_are_not_drift_and_are_asked_again(env, no_spawn, monkeypatch):
    bus = env["bus"]
    monkeypatch.setenv("FAKE_BEHAVIOUR", "denied")  # antigravity stops partway; codex replies
    tid = Service(bus, HUMAN).start_discussion("q", peers=["codex", "antigravity"], max_rounds=2)["thread"]["id"]
    run_job(bus, tid)
    assert bus.peer_drift(tid) == {}
    assert len(calls(env, "antigravity")) == 2  # asked again in round 2


def test_review_drift_is_not_recorded_so_the_next_pass_tries_again(env, no_spawn, monkeypatch):
    bus = env["bus"]
    monkeypatch.setattr("agent_bus.worker.Worker.review_diff", lambda self, refs, interdiff: ("diff", "Diff"))
    monkeypatch.setenv("FAKE_BEHAVIOUR", "silent")
    me = Service(bus, "claude", bus.register_session("claude", env={}))
    tid = me.request_review("codex", str(env["repo"]), "a" * 40, "b" * 40)["thread"]["id"]
    run_job(bus, tid)
    assert bus.thread(tid)["status"] == "needs_human" and bus.peer_drift(tid) == {}


def test_resume_gives_recovered_peers_a_round_despite_an_absent_initiator(env, no_spawn):
    bus = env["bus"]
    me, tid = advance_setup(bus, ["codex", "antigravity"])
    me.send("claude plan", thread=tid, kind="proposal")  # P1, the initiator's
    bus.post(tid, "codex", "codex plan", kind="proposal")  # P2
    bus.post(tid, "antigravity", "agree", kind="vote", data={"on": "P2", "stance": "agree", "reasons": "r"})
    bus.set_peer_drift(tid, "codex", "v", "without a reply")
    bus.conn.execute("UPDATE sessions SET last_seen=0")  # the initiator is absent
    Service(bus, HUMAN).resume(tid)
    run_job(bus, tid)  # the fake peers agree with the lowest proposal id: P1
    assert calls(env, "codex") and calls(env, "antigravity")
    assert bus.thread(tid)["status"] == "resolved" and bus.thread(tid)["final_proposal"] == "P1"


def test_a_round_is_not_taken_on_a_thread_closed_meanwhile(env, no_spawn, monkeypatch):
    import contextlib
    bus = env["bus"]
    me, tid = advance_setup(bus, ["codex"])
    me.advance_discussion(tid)
    w = Worker(bus, bus.jobs(tid)[-1]["id"])
    monkeypatch.setattr(w, "fenced", contextlib.nullcontext)  # no lease needed for this check
    bus.transition(tid, "resolved")  # e.g. the initiator's final vote landed after the done() check
    assert w.later_round() is False
    assert bus.thread(tid)["round"] == 1 and calls(env, "codex") == []
