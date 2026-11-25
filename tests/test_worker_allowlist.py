"""#99: the worker checks the repo allowlist, from its own freshly loaded config, for every job kind."""
import json

import pytest

from agent_bus import snapshot, usages
from agent_bus.db import HUMAN
from agent_bus.service import Service
from agent_bus.worker import Worker

from test_review_rounds import git_repo


def calls(env):
    return [json.loads(line) for line in env["log"].read_text().splitlines()] if env["log"].exists() else []


def set_repos(env, repos):
    conf = env["tmp"] / "config" / "agent-bus" / "config.json"
    data = json.loads(conf.read_text())
    data["repos"] = [str(r) for r in repos]
    conf.write_text(json.dumps(data))


@pytest.fixture
def watched(monkeypatch):
    """Record any review diff, snapshot (#116) or usage search (#117) the worker builds."""
    seen = []
    real_ensure, real_note, real_diff = snapshot.ensure, usages.note, Worker.review_diff
    monkeypatch.setattr(Worker, "review_diff", lambda self, *a, **k: seen.append("diff") or real_diff(self, *a, **k))
    monkeypatch.setattr(snapshot, "ensure", lambda *a, **k: seen.append("snapshot") or real_ensure(*a, **k))
    monkeypatch.setattr(usages, "note", lambda *a, **k: seen.append("usages") or real_note(*a, **k))
    return seen


def refused(env, tid, watched, who=None):
    """Remove the repo from the allowlist (a server's older config already queued the job),
    then run the job: refused before any peer turn, diff, snapshot or usage search."""
    bus = env["bus"]
    set_repos(env, [])
    n = len(calls(env))
    watched.clear()
    job = bus.jobs(tid)[-1]
    assert Worker(bus, job["id"]).run() == 1
    job = bus.job(job["id"])
    assert job["status"] == "failed" and job["error"].startswith("REPO_NOT_ALLOWED")
    assert len(calls(env)) == n and watched == []
    notice = bus.history(tid)["messages"][-1]
    assert notice["sender"] == "system" and "no longer in the agent-bus repo allowlist" in notice["body"]
    assert notice["recipient"] == who
    return notice


def test_first_review_pass(env, no_spawn, watched):
    bus, repo = env["bus"], env["repo"]
    base, commit = git_repo(repo)
    h1 = commit("one\n")
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review("codex", str(repo), base, h1)["thread"]["id"]
    notice = refused(env, tid, watched, who="claude")
    assert notice["body"].startswith("Review not started") and f"agent-bus resume {tid}" in notice["body"]
    assert bus.thread(tid)["status"] == "needs_human"
    # Allow-listed again: the resume runs the pass.
    set_repos(env, [repo])
    Service(bus, HUMAN).resume(tid)
    Worker(bus, bus.jobs(tid)[-1]["id"]).run()
    assert bus.last_verdict(tid, "codex") is not None and "snapshot" in watched


def test_review_follow_up(env, no_spawn, watched):
    bus, repo = env["bus"], env["repo"]
    base, commit = git_repo(repo)
    h1 = commit("one\n")
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review("codex", str(repo), base, h1)["thread"]["id"]
    Worker(bus, bus.jobs(tid)[-1]["id"]).run()
    assert "snapshot" in watched  # the allow-listed pass builds one
    claude.send("fixed it", thread=tid)
    h2 = commit("two\n")
    claude.request_review("codex", str(repo), base, h2, prev_head_commit_id=h1)
    assert bus.jobs(tid)[-1]["params"]["mode"] == "followup"
    refused(env, tid, watched, who="claude")
    assert bus.thread(tid)["status"] == "needs_human"


def test_review_pushback(env, no_spawn, watched):
    bus, repo = env["bus"], env["repo"]
    base, commit = git_repo(repo)
    h1 = commit("one\n")
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review(None, str(repo), base, h1, reviewers=["codex", "antigravity"])["thread"]["id"]
    Worker(bus, bus.jobs(tid)[-1]["id"]).run()  # codex asks for changes
    claude.send("PUSHBACK: intended", thread=tid)
    claude.request_review(None, str(repo), base, h1, prev_head_commit_id=h1, reviewers=["codex", "antigravity"])
    assert bus.jobs(tid)[-1]["params"]["mode"] == "pushback"
    refused(env, tid, watched, who="claude")
    assert bus.thread(tid)["status"] == "needs_human"


def test_discussion_start_and_advance(env, no_spawn, watched):
    bus, repo = env["bus"], env["repo"]
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.start_discussion("Which DB?", peers=["codex"], repo=str(repo))["thread"]["id"]
    claude.send("Postgres, for the joins.", thread=tid, kind="proposal")
    notice = refused(env, tid, watched, who="claude")
    assert notice["body"].startswith("Discussion turn not started")
    assert bus.thread(tid)["status"] == "needs_human" and bus.thread(tid)["blind"]
    # Allow-listed again, the resume runs the blind round 1 the refusal skipped: the peer
    # proposes without seeing the initiator's proposal.
    set_repos(env, [repo])
    Service(bus, HUMAN).resume(tid)
    assert bus.jobs(tid)[-1]["params"]["mode"] == "start_later"
    Worker(bus, bus.jobs(tid)[-1]["id"]).run()
    first = calls(env)[0]
    assert first["agent"] == "codex" and "Postgres" not in first["prompt"]
    assert any(m["sender"] == "codex" and m["kind"] == "proposal" and m["round"] == 1
               for m in bus.history(tid)["messages"])

    # An advance queued by a server that still allows the repo.
    set_repos(env, [repo])
    tid = claude.start_discussion("Which cache?", peers=["codex"], repo=str(repo))["thread"]["id"]
    bus.conn.execute("UPDATE jobs SET status='done' WHERE thread_id=?", (tid,))
    bus.set_thread(tid, blind=0)
    claude.advance_discussion(tid)
    assert bus.jobs(tid)[-1]["params"]["mode"] == "advance"
    refused(env, tid, watched, who="claude")
    assert bus.thread(tid)["status"] == "needs_human"


def test_human_discussion_notice_goes_to_everyone(env, no_spawn, watched):
    bus, repo = env["bus"], env["repo"]
    tid = Service(bus, HUMAN).start_discussion("Which DB?", peers=["codex"], repo=str(repo))["thread"]["id"]
    refused(env, tid, watched, who=None)
    assert bus.thread(tid)["status"] == "needs_human"


def test_a_job_without_a_repo_is_not_checked(env, no_spawn):
    bus = env["bus"]
    set_repos(env, [])
    tid = Service(bus, HUMAN).start_discussion("Which DB?", peers=["codex"])["thread"]["id"]
    assert Worker(bus, bus.jobs(tid)[-1]["id"]).run() == 0
    assert calls(env)


def test_a_resumed_round_one_runs_only_the_peers_that_owe_a_proposal(env, no_spawn):
    """A worker that died after some round-1 proposals were in (blind still set): the resume
    asks only the peers without one, and never replaces a proposal already made."""
    bus = env["bus"]
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.start_discussion("Which DB?", peers=["codex", "antigravity"])["thread"]["id"]
    claude.send("Postgres.", thread=tid, kind="proposal")
    bus.post(tid, "codex", "SQLite.", kind="proposal")
    bus.conn.execute("UPDATE jobs SET status='failed' WHERE thread_id=?", (tid,))  # reaped
    bus.set_thread(tid, status="needs_human")
    assert bus.thread(tid)["blind"]
    Service(bus, HUMAN).resume(tid)
    Worker(bus, bus.jobs(tid)[-1]["id"]).run()
    first = calls(env)[0]
    assert first["agent"] == "antigravity" and "Postgres" not in first["prompt"] and "SQLite" not in first["prompt"]
    round_one = [m["sender"] for m in bus.history(tid)["messages"] if m["kind"] == "proposal" and m["round"] == 1]
    assert sorted(round_one) == ["antigravity", "claude", "codex"]
