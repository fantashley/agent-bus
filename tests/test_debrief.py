"""#91: debrief a finished thread's peers in their own resumed sessions."""
import asyncio
import json
import subprocess

import pytest

from agent_bus import cli, mcp_server
from agent_bus.db import HUMAN, BusError
from agent_bus.service import Service
from agent_bus.worker import Worker
from test_git import git_repo


def run_job(bus, tid):
    job = bus.jobs(tid)[-1]
    assert Worker(bus, job["id"]).run() == 0
    return bus.job(job["id"])


def calls(env):
    if not env["log"].exists():
        return []
    return [json.loads(line) for line in env["log"].read_text().splitlines()]


def without_prompt(argv):
    """The argv minus the prompt itself (agy takes it as --print's value; the others on stdin),
    and minus a review's snapshot directory (#111), which is gone once the thread is finished."""
    if "--print" in argv:
        i = argv.index("--print")
        argv = argv[:i] + argv[i + 2:]
    out = []
    for i, a in enumerate(argv):
        if a == "--add-dir" and i + 1 < len(argv) and "/snapshots/" in argv[i + 1]:
            continue
        if i and argv[i - 1] == "--add-dir" and "/snapshots/" in a:
            continue
        out.append(a)
    return out


def reviewed(env, reviewer):
    """A resolved review thread of ``reviewer``: a first pass and a resumed follow-up pass."""
    repo = env["repo"]
    sh = git_repo(repo)
    base = sh("git", "rev-parse", "HEAD")
    (repo / "a.txt").write_text("helo\n")
    sh("git", "commit", "-qam", "one")
    head1 = sh("git", "rev-parse", "HEAD")
    (repo / "a.txt").write_text("hello again\n")
    sh("git", "commit", "-qam", "two")
    head2 = sh("git", "rev-parse", "HEAD")
    bus = env["bus"]
    author = "codex" if reviewer == "claude" else "claude"
    svc = Service(bus, author, bus.register_session(author))
    tid = svc.request_review(reviewer, str(repo), base, head1)["thread"]["id"]
    run_job(bus, tid)
    svc.send("fixed it", thread=tid)
    assert svc.request_review(reviewer, str(repo), base, head2, prev_head_commit_id=head1)["thread"]["id"] == tid
    run_job(bus, tid)
    assert bus.thread(tid)["status"] == "resolved"
    return svc, tid, (base, head1, head2)


@pytest.mark.parametrize("reviewer", ["claude", "codex", "antigravity"])
def test_debrief_resumes_the_review_session_with_the_same_hardening(env, no_spawn, reviewer):
    svc, tid, (base, head1, head2) = reviewed(env, reviewer)
    review_job = env["bus"].jobs(tid)[-1]
    bus = env["bus"]
    before = (bus.thread(tid), bus.review_standings(tid), bus.peer_session(tid, reviewer))
    res = svc.debrief(tid)
    assert res["peers"] == [reviewer] and res["job"]["id"] in no_spawn
    run_job(bus, tid)
    mine = [c for c in calls(env) if c["agent"] == reviewer]
    assert len(mine) == 3  # first pass, resumed follow-up, debrief: never a fresh session
    review, debrief = mine[1], mine[2]
    # Exactly the follow-up review turn's argv (resume flag and session included), env and
    # CODEX_HOME: only the prompt differs.
    assert without_prompt(debrief["argv"]) == without_prompt(review["argv"])
    assert debrief["codex_home"] == review["codex_home"] and debrief["peer_env"] and review["peer_env"]
    resume_flag = {"claude": "--resume", "codex": "resume", "antigravity": "--conversation"}[reviewer]
    assert resume_flag in debrief["argv"] and before[2] in debrief["argv"]
    hardening = {"claude": ["--permission-mode", "dontAsk", "--strict-mcp-config"],
                 "codex": ['sandbox_mode="read-only"', 'approval_policy="never"', "mcp_servers.github.enabled=false"],
                 "antigravity": ["--mode", "plan", "--sandbox", "--agent"]}[reviewer]
    assert all(h in debrief["argv"] for h in hardening)
    assert "agent-bus debrief of thread" in debrief["prompt"] and "friction" in debrief["prompt"]
    # Posted as peer data to the author; status, verdicts and session untouched.
    msgs = bus.history(tid)["messages"]
    d = [m for m in msgs if m["kind"] == "debrief"]
    assert len(d) == 1 and d[0]["sender"] == reviewer and d[0]["trust"] == "peer_data"
    assert d[0]["recipient"] == svc.agent and "the prompts were clear" in d[0]["body"]
    after = bus.thread(tid)
    assert (after["status"], after["round"], after["refs"]) == (before[0]["status"], before[0]["round"],
                                                               before[0]["refs"])
    assert bus.review_standings(tid) == before[1] and bus.peer_session(tid, reviewer) == before[2]
    # Its tokens are accounted like any turn's.
    job = bus.jobs(tid)[-1]
    assert job["kind"] == "debrief" and job["status"] == "done"
    assert bus._one("SELECT COUNT(*) AS n FROM turn_usage WHERE job_id=?", job["id"])["n"] == 1
    # One per peer.
    with pytest.raises(BusError) as e:
        svc.debrief(tid)
    assert e.value.code == "DEBRIEF_USED"
    # The author reads it with wait/inbox, fenced.
    got = [m for m in svc.inbox(thread=tid)["messages"] if m["kind"] == "debrief"]
    assert got and got[0]["body"].startswith("[peer_data from")
    # A repeat of the last review request still finds that review pass, not the debrief.
    again = svc.request_review(reviewer, str(env["repo"]), base, head2, prev_head_commit_id=head1)
    assert again["job"]["id"] == review_job["id"] and again["job"].get("deduplicated")
    assert bus.review_standings(tid) == before[1]  # nobody "reviewing" during a debrief either


def test_followup_keeps_the_debrief_reset(env, no_spawn):
    svc, tid, (base, head1, head2) = reviewed(env, "codex")
    bus = env["bus"]
    svc.debrief(tid)
    run_job(bus, tid)
    bus.transition(tid, "needs_human", frm=("resolved",))  # say it had been parked
    Service(bus, HUMAN).resume(tid)
    after = bus.thread(tid)["refs"]["debrief_after"]
    (env["repo"] / "a.txt").write_text("third\n")
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qam", "three"],
                   cwd=env["repo"], check=True)
    head3 = subprocess.run(["git", "rev-parse", "HEAD"], cwd=env["repo"], check=True, capture_output=True,
                           text=True).stdout.strip()
    svc.request_review("codex", str(env["repo"]), base, head3, prev_head_commit_id=head2)
    assert bus.thread(tid)["refs"]["debrief_after"] == after
    run_job(bus, tid)
    assert svc.debrief(tid)["peers"] == ["codex"]  # the earlier answer no longer counts


def test_debrief_refuses_a_repo_no_longer_allowed(env, no_spawn):
    svc, tid, _ = reviewed(env, "codex")
    conf = env["tmp"] / "config" / "agent-bus" / "config.json"
    data = json.loads(conf.read_text())
    data["repos"] = []
    conf.write_text(json.dumps(data))
    with pytest.raises(BusError) as e:
        Service(env["bus"], svc.agent, svc.session_id).debrief(tid)
    assert e.value.code == "REPO_NOT_ALLOWED"
    # A server still running with the old config queues it; the worker, with the new one, refuses.
    bus = env["bus"]
    n = len(calls(env))
    svc.debrief(tid)
    run_job(bus, tid)
    assert len(calls(env)) == n and not [m for m in bus.history(tid)["messages"] if m["kind"] == "debrief"]
    assert "no longer in the agent-bus repo allowlist" in bus.history(tid)["messages"][-1]["body"]


def stubborn(env, monkeypatch, peers=("codex", "antigravity")):
    monkeypatch.setenv("FAKE_BEHAVIOUR", "stubborn")
    bus = env["bus"]
    tid = Service(bus, HUMAN).start_discussion("Q?", peers=list(peers), max_rounds=1)["thread"]["id"]
    run_job(bus, tid)
    # One peer's proposal is the consensus already; two stubborn ones need the human.
    assert bus.thread(tid)["status"] == ("needs_human" if len(peers) > 1 else "resolved")
    monkeypatch.delenv("FAKE_BEHAVIOUR")
    return tid


def test_discussion_debrief_one_per_peer_until_a_human_resume(env, no_spawn, monkeypatch):
    bus = env["bus"]
    tid = stubborn(env, monkeypatch)
    human = Service(bus, HUMAN)
    n = len(calls(env))
    human.debrief(tid, question="Was the vote format clear?")
    run_job(bus, tid)
    new = calls(env)[n:]
    assert sorted(c["agent"] for c in new) == ["antigravity", "codex"]
    assert all("Was the vote format clear?" in c["prompt"] and "[peer_data from" not in c["prompt"] for c in new)
    t = bus.thread(tid)
    assert t["status"] == "needs_human" and bus.standings(tid)  # votes and status unchanged
    assert sorted(m["sender"] for m in bus.history(tid)["messages"] if m["kind"] == "debrief") == \
        ["antigravity", "codex"]
    with pytest.raises(BusError) as e:
        human.debrief(tid)
    assert e.value.code == "DEBRIEF_USED"
    # The human resumes it: a running thread can't be debriefed, a finished one can again.
    human.resume(tid, extra_rounds=1)
    with pytest.raises(BusError) as e:
        human.debrief(tid)
    assert e.value.code == "THREAD_NOT_FINISHED"
    monkeypatch.setenv("FAKE_BEHAVIOUR", "stubborn")
    run_job(bus, tid)
    monkeypatch.delenv("FAKE_BEHAVIOUR")
    assert bus.thread(tid)["status"] == "needs_human"
    assert human.debrief(tid, peers=["codex"])["peers"] == ["codex"]


def test_debrief_tries_a_peer_with_recorded_drift(env, no_spawn, monkeypatch):
    bus = env["bus"]
    tid = stubborn(env, monkeypatch, peers=("codex",))
    bus.set_peer_drift(tid, "codex", "fake-codex 0.0.0", "without a reply")
    Service(bus, HUMAN).debrief(tid)
    run_job(bus, tid)
    assert [m["sender"] for m in bus.history(tid)["messages"] if m["kind"] == "debrief"] == ["codex"]


def test_debrief_refusals(env, no_spawn, monkeypatch):
    bus = env["bus"]
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.start_discussion("Q?", peers=["codex"])["thread"]["id"]
    with pytest.raises(BusError) as e:  # still running
        claude.debrief(tid)
    assert e.value.code == "THREAD_NOT_FINISHED"
    bus.transition(tid, "resolved")
    with pytest.raises(BusError) as e:  # its job is still queued
        claude.debrief(tid)
    assert e.value.code == "THREAD_BUSY"
    bus.conn.execute("UPDATE jobs SET status='done' WHERE thread_id=?", (tid,))
    bus.set_peer_session(tid, "codex", "sess-1")
    with pytest.raises(BusError) as e:  # not the initiator
        Service(bus, "antigravity", bus.register_session("antigravity")).debrief(tid)
    assert e.value.code == "NOT_INITIATOR"
    with pytest.raises(ValueError):
        claude.debrief(tid, peers=["claude"])  # the initiator isn't a headless peer
    chat = claude.send("hi", to="codex")["thread"]["id"]
    with pytest.raises(ValueError):
        claude.debrief(chat)
    # A peer with no stored session: refused, never a fresh session.
    bus.set_peer_session(tid, "codex", None)
    with pytest.raises(BusError) as e:
        claude.debrief(tid)
    assert e.value.code == "NO_SESSION"
    assert not [j for j in bus.jobs(tid) if j["kind"] == "debrief"]


def test_debrief_skips_peers_without_a_session(env, no_spawn, monkeypatch):
    bus = env["bus"]
    tid = stubborn(env, monkeypatch)
    bus.set_peer_session(tid, "antigravity", None)
    res = Service(bus, HUMAN).debrief(tid)
    assert res["peers"] == ["codex"] and "antigravity" in res["skipped_peers"]
    with pytest.raises(BusError) as e:  # named outright, it is an error
        Service(bus, HUMAN).debrief(tid, peers=["antigravity"])
    assert e.value.code in ("NO_SESSION", "THREAD_BUSY")


def test_failed_resume_is_reported_not_replaced_by_a_fresh_session(env, no_spawn, monkeypatch):
    bus = env["bus"]
    tid = stubborn(env, monkeypatch)
    sessions = {p: bus.peer_session(tid, p) for p in ("codex", "antigravity")}
    monkeypatch.setenv("FAKE_RESUME_FAILS", "codex")
    n = len(calls(env))
    Service(bus, HUMAN).debrief(tid)
    run_job(bus, tid)
    new = calls(env)[n:]
    assert sorted(c["agent"] for c in new) == ["antigravity", "codex"]  # codex tried once, resumed
    assert sessions["codex"] in [c for c in new if c["agent"] == "codex"][0]["argv"]
    msgs = bus.history(tid)["messages"]
    assert [m["sender"] for m in msgs if m["kind"] == "debrief"] == ["antigravity"]
    notice = [m for m in msgs if m["sender"] == "system" and "codex gave no debrief" in m["body"]]
    assert notice and "No fresh session was started" in notice[0]["body"]
    assert bus.thread(tid)["status"] == "needs_human"
    assert {p: bus.peer_session(tid, p) for p in sessions} == sessions
    # It gave no answer, so it can be asked again; antigravity has had its turn.
    monkeypatch.delenv("FAKE_RESUME_FAILS")
    assert Service(bus, HUMAN).debrief(tid)["peers"] == ["codex"]


def test_session_lost_after_queueing(env, no_spawn, monkeypatch):
    bus = env["bus"]
    tid = stubborn(env, monkeypatch, peers=("codex",))
    Service(bus, HUMAN).debrief(tid)
    bus.set_peer_session(tid, "codex", None)
    n = len(calls(env))
    run_job(bus, tid)
    assert calls(env)[n:] == []  # nothing ran
    assert any("codex gave no debrief" in m["body"] and "no stored session" in m["body"]
               for m in bus.history(tid)["messages"])


def test_out_of_usage_debrief_leaves_the_thread_alone(env, no_spawn, monkeypatch):
    bus = env["bus"]
    tid = stubborn(env, monkeypatch, peers=("codex",))
    monkeypatch.setenv("FAKE_EXHAUSTED", "codex")
    Service(bus, HUMAN).debrief(tid)
    run_job(bus, tid)
    assert bus.thread(tid)["status"] == "resolved"
    assert bus.quota_blocks()  # recorded, like any turn's
    assert not env["waiters"] and not bus._all("SELECT * FROM auto_resumes")
    notice = bus.history(tid)["messages"][-1]
    assert "request the debrief" in notice["body"] and "agent-bus resume" not in notice["body"]
    with pytest.raises(BusError) as e:  # now refused up front, like any request
        Service(bus, HUMAN).debrief(tid)
    assert e.value.code == "USAGE_EXHAUSTED"


def test_debrief_is_exempt_from_the_message_cap(env, no_spawn, monkeypatch):
    bus = env["bus"]
    tid = stubborn(env, monkeypatch, peers=("codex",))
    t = bus.thread(tid)
    bus.set_thread(tid, max_messages=t["agent_messages"])
    assert bus.thread(tid)["limit_reached"]
    Service(bus, HUMAN).debrief(tid)
    run_job(bus, tid)
    assert [m for m in bus.history(tid)["messages"] if m["kind"] == "debrief"]
    assert bus.thread(tid)["agent_messages"] == t["agent_messages"]


def test_resume_waits_for_a_running_debrief(env, no_spawn, monkeypatch):
    bus = env["bus"]
    tid = stubborn(env, monkeypatch)  # needs_human: a resume would reopen it
    Service(bus, HUMAN).debrief(tid)
    with pytest.raises(BusError) as e:
        Service(bus, HUMAN).resume(tid)
    assert e.value.code == "THREAD_BUSY"


def test_cli_and_mcp(env, no_spawn, monkeypatch, capsys):
    bus = env["bus"]
    tid = stubborn(env, monkeypatch, peers=("codex",))
    assert cli.main(["debrief", tid, "--question", "Anything?"]) == 0
    assert "debriefing codex" in capsys.readouterr().out
    job = bus.jobs(tid)[-1]
    assert job["kind"] == "debrief" and job["params"]["question"] == "Anything?"
    assert cli.main(["debrief", tid]) == 2  # its job is still running
    # MCP: an agent's own thread only.
    claude = Service(bus, "claude", bus.register_session("claude"))
    own = claude.start_discussion("Q?", peers=["codex"])["thread"]["id"]
    claude.send("mine", thread=own, kind="proposal")
    run_job(bus, own)
    assert bus.thread(own)["status"] == "resolved"
    server = mcp_server.build("claude", bus=bus)
    res = json.loads(asyncio.run(server.call_tool("debrief", {"thread": tid})).content[0].text)
    assert res["error"] == "NOT_INITIATOR"
    res = json.loads(asyncio.run(server.call_tool("debrief", {"thread": own})).content[0].text)
    assert res["peers"] == ["codex"]


def test_retrying_a_failed_review_waits_for_a_running_debrief(env, no_spawn):
    repo = env["repo"]
    sh = git_repo(repo)
    base = sh("git", "rev-parse", "HEAD")
    (repo / "a.txt").write_text("helo\n")
    sh("git", "commit", "-qam", "one")
    head = sh("git", "rev-parse", "HEAD")
    bus = env["bus"]
    svc = Service(bus, "claude", bus.register_session("claude"))
    tid = svc.request_review(None, str(repo), base, head, reviewers=["codex", "antigravity"])["thread"]["id"]
    run_job(bus, tid)
    # Say its pass then failed and parked the thread (a missing commit, for one).
    bus.conn.execute("UPDATE jobs SET status='failed' WHERE thread_id=?", (tid,))
    bus.transition(tid, "needs_human")
    svc.debrief(tid)
    with pytest.raises(BusError) as e:
        svc.request_review(None, str(repo), base, head, reviewers=["codex", "antigravity"])
    assert e.value.code == "REVIEW_BUSY"
    assert bus.thread(tid)["status"] == "needs_human"
