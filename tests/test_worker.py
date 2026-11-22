import json
import subprocess

import pytest

from agent_bus.db import HUMAN
from agent_bus.peers import extract_json
from agent_bus.service import Service
from agent_bus.worker import Worker


def run_job(bus, tid):
    job = bus.jobs(tid)[-1]
    assert Worker(bus, job["id"]).run() == 0
    return bus.job(job["id"])


def test_human_ask_reaches_consensus_with_all_three(env, no_spawn):
    bus = env["bus"]
    tid = Service(bus, HUMAN).start_discussion("Which DB?", peers=["claude", "codex", "antigravity"])["thread"]["id"]
    job = run_job(bus, tid)
    assert job["status"] == "done"
    t = bus.thread(tid)
    assert t["status"] == "resolved" and t["final_proposal"] == "P1"
    kinds = [m["kind"] for m in bus.history(tid)["messages"]]
    assert kinds.count("proposal") == 3 and "final" in kinds
    # Each peer resumed its own session in round 2.
    calls = [json.loads(l) for l in env["log"].read_text().splitlines()]
    # P1's author already agrees with it, so that peer may not need a round-2 turn;
    # whoever did take one must have resumed.
    second = {}
    for c in calls:
        second.setdefault(c["agent"], []).append(c)
    resumed = {a: cs[1]["argv"] for a, cs in second.items() if len(cs) > 1}
    assert len(resumed) >= 2
    checks = {"claude": lambda a: "--resume" in a,
              "codex": lambda a: a[:3] == ["exec", "resume", "--json"],
              "antigravity": lambda a: "--conversation" in a}
    for agent, argv in resumed.items():
        assert checks[agent](argv), (agent, argv)
    # Round 1 prompts contain no other agent's view.
    r1 = [c for c in calls if "round 1, which is blind" in c["prompt"]]
    assert len(r1) == 3 and not any("says use SQLite" in c["prompt"] for c in r1)
    # Progress events were recorded for viewers.
    assert {e["kind"] for e in bus.events(tid)} >= {"turn_start", "turn_end", "tool"}


def test_stubborn_peers_end_in_needs_human(env, no_spawn, monkeypatch):
    monkeypatch.setenv("FAKE_BEHAVIOUR", "stubborn")
    bus = env["bus"]
    tid = Service(bus, HUMAN).start_discussion("Q?", peers=["claude", "codex"], max_rounds=2)["thread"]["id"]
    run_job(bus, tid)
    t = bus.thread(tid)
    assert t["status"] == "needs_human" and t["round"] == 2
    last = bus.history(tid)["messages"][-1]
    assert "Needs the human user" in last["body"] and "objects" in last["body"]
    # The human resumes, which dispatches another worker.
    res = Service(bus, HUMAN).resume(tid, extra_rounds=1)
    assert res["thread"]["max_rounds"] == 3 and res["thread"]["status"] == "open"


def test_garbage_reply_posted_as_message(env, no_spawn, monkeypatch):
    monkeypatch.setenv("FAKE_BEHAVIOUR", "garbage")
    bus = env["bus"]
    tid = Service(bus, HUMAN).start_discussion("Q?", peers=["codex"], max_rounds=1)["thread"]["id"]
    run_job(bus, tid)
    msgs = bus.history(tid)["messages"]
    assert any(m["sender"] == "codex" and m["kind"] == "message" for m in msgs)
    assert bus.thread(tid)["status"] == "needs_human"


def test_absent_initiator_leaves_thread_awaiting_its_vote(env, no_spawn):
    bus = env["bus"]
    sid = bus.register_session("claude")
    claude = Service(bus, "claude", sid)
    tid = claude.start_discussion("Q?", peers=["codex", "antigravity"])["thread"]["id"]
    bus.conn.execute("UPDATE sessions SET last_seen=0 WHERE id=?", (sid,))  # turn ended
    run_job(bus, tid)
    t = bus.thread(tid)
    assert t["status"] == "open"
    msgs = claude.inbox(thread=tid)["messages"]
    assert "Waiting for claude" in msgs[-1]["body"]
    claude.send("ok", thread=tid, kind="vote", on="P1", stance="agree")
    assert bus.thread(tid)["status"] == "resolved"


def test_present_initiator_takes_part(env, no_spawn):
    bus = env["bus"]
    sid = bus.register_session("claude")
    claude = Service(bus, "claude", sid)
    tid = claude.start_discussion("Q?", peers=["codex"])["thread"]["id"]
    claude.send("claude proposal", thread=tid, kind="proposal")  # P1, posted before the peer
    run_job(bus, tid)
    # codex proposes P2 in round 1, then agrees with P1 in round 2 -> consensus.
    t = bus.thread(tid)
    assert t["status"] == "resolved" and t["final_proposal"] == "P1"


def test_review_round_trip(env, no_spawn):
    repo = env["repo"]
    if subprocess.run(["jj", "--version"], capture_output=True).returncode != 0:
        pytest.skip("jj not installed")
    sh = lambda *a: subprocess.run(a, cwd=repo, check=True, capture_output=True, text=True).stdout.strip()  # noqa: E731
    sh("jj", "git", "init")
    (repo / "a.txt").write_text("hello\n")
    sh("jj", "file", "track", "a.txt")
    sh("jj", "commit", "-m", "base")
    (repo / "a.txt").write_text("helo\n")
    head = sh("jj", "log", "--no-graph", "-r", "@", "-T", "commit_id")
    base = sh("jj", "log", "--no-graph", "-r", "@-", "-T", "commit_id")
    bus = env["bus"]
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review("codex", str(repo), base, head, focus="check spelling")["thread"]["id"]
    run_job(bus, tid)
    msgs = claude.inbox(thread=tid)["messages"]
    verdict = [m for m in msgs if m["kind"] == "review_verdict"][0]
    assert verdict["data"]["verdict"] == "request_changes"
    assert verdict["data"]["head_commit_id"] == head
    calls = [json.loads(l) for l in env["log"].read_text().splitlines()]
    assert "-helo" not in calls[0]["prompt"] and "+helo" in calls[0]["prompt"]


def test_extract_json():
    assert extract_json('x {"a": 1} y {"b": {"c": 2}}') == {"b": {"c": 2}}
    assert extract_json("```json\n{\"a\": 1}\n```") == {"a": 1}
    assert extract_json("nothing") is None
