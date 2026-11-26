"""#143: a single-reviewer review stays open after a request for changes, waiting for the
author, and resolves only on approval, like a review with several reviewers."""
import pytest

from agent_bus import cli, snapshot
from agent_bus.db import HUMAN, BusError
from agent_bus.service import Service

from test_review_rounds import git_repo, run_job


def test_request_changes_keeps_the_thread_open_until_approval(env, no_spawn, monkeypatch, capsys):
    bus, repo = env["bus"], env["repo"]
    base, commit = git_repo(repo)
    h1 = commit("one\n")
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review("codex", str(repo), base, h1)["thread"]["id"]
    run_job(bus, tid)  # the fake reviewer asks for changes
    t = bus.thread(tid)
    assert t["status"] == "open"
    # wait/inbox: the verdict, with what to do next.
    res = claude.inbox(thread=tid)
    assert res["messages"][-1]["kind"] == "review_verdict"
    assert res["next_action"].startswith("fix: codex asked for changes on head " + h1)
    assert res["review"]["changes_requested"] == ["codex"]
    # Listed as unfinished, and shown as changes requested, not done.
    assert tid in [x["id"] for x in claude.threads()["threads"]]
    assert cli.thread_status(bus, tid) == ("idle", None, f"{tid}: changes requested by codex")
    assert cli.peer_status(bus, tid, "codex") == ("idle", None, "codex: changes requested")
    # A stream of it has nothing more to wait for until the author acts.
    assert cli.main(["stream", tid, "--from-start", "--interval", "0"]) == 0
    assert "REVIEW_VERDICT" in capsys.readouterr().out
    # Not finished, so not debriefable yet.
    with pytest.raises(BusError) as e:
        claude.debrief(tid)
    assert e.value.code == "THREAD_NOT_FINISHED"

    # The fix is approved: resolved, and done.
    h2 = commit("two\n")
    claude.send("codex-1: fixed", thread=tid)
    claude.request_review("codex", str(repo), base, h2, prev_head_commit_id=h1)
    # Queued on the new head: the pane doesn't show the old head's verdict (codex-1).
    assert "changes requested" not in cli.peer_status(bus, tid, "codex")[2]
    monkeypatch.setenv("FAKE_APPROVE", "codex")
    run_job(bus, tid)
    assert bus.thread(tid)["status"] == "resolved"
    assert claude.inbox(thread=tid)["next_action"] == f"done: codex approved head {h2}"
    assert cli.thread_status(bus, tid)[0] == "done"
    assert claude.debrief(tid)["peers"] == ["codex"]


def test_a_human_resume_of_a_review_waiting_for_its_fix_runs_nothing(env, no_spawn):
    bus, repo = env["bus"], env["repo"]
    base, commit = git_repo(repo)
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review("codex", str(repo), base, commit("one\n"))["thread"]["id"]
    run_job(bus, tid)
    n = len(bus.jobs(tid))
    res = Service(bus, HUMAN).resume(tid)
    assert not res.get("job") and res["thread"]["status"] == "open" and len(bus.jobs(tid)) == n


def test_snapshot_of_a_review_waiting_for_its_fix_is_swept(env, no_spawn):
    """The thread stays open, maybe for good: the snapshot goes once no job runs (the next pass
    makes it again)."""
    bus, repo = env["bus"], env["repo"]
    base, commit = git_repo(repo)
    h1 = commit("one\n")
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review("codex", str(repo), base, h1)["thread"]["id"]
    snap = snapshot.ensure(str(repo), tid, base, h1, 10**9)
    assert snap.path
    snapshot.release(snap.hold)  # as the worker that made it does when its job ends (#166)
    snapshot.sweep(bus)
    assert (snapshot.root() / tid).exists()  # its job is still queued
    run_job(bus, tid)  # the worker sweeps after its job
    assert bus.thread(tid)["status"] == "open" and not (snapshot.root() / tid).exists()


def test_resume_resolves_an_approved_review_whose_worker_died(env, no_spawn, monkeypatch):
    """The worker posted the approval but exited before resolving the thread: a resume
    resolves it without rerunning the reviewer."""
    bus, repo = env["bus"], env["repo"]
    base, commit = git_repo(repo)
    h1 = commit("one\n")
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review("codex", str(repo), base, h1)["thread"]["id"]
    monkeypatch.setenv("FAKE_APPROVE", "codex")
    from agent_bus.worker import Worker

    def dies(self, status):
        raise RuntimeError("worker died")
    monkeypatch.setattr(Worker, "finish_thread", dies)  # after posting the approval
    with pytest.raises(RuntimeError):
        run_job(bus, tid)
    _reaped(bus, tid)
    assert bus.thread(tid)["status"] == "open" and claude.inbox(thread=tid)["next_action"].startswith("done:")
    n = len(bus.jobs(tid))
    res = Service(bus, HUMAN).resume(tid)
    assert not res.get("job") and len(bus.jobs(tid)) == n
    assert bus.thread(tid)["status"] == "resolved"
    assert "Review complete" in bus.history(tid)["messages"][-1]["body"]
    # Repeating the request is idempotent: no second review, no REVIEW_FAILED (codex-6).
    again = claude.request_review("codex", str(repo), base, h1)
    assert again["thread"]["id"] == tid and len(bus.jobs(tid)) == n and len(bus.threads(True)) == 1
    assert bus.jobs(tid)[-1]["status"] == "failed"  # the failed attempt stays in the history


def test_a_repeated_follow_up_after_a_recovered_approval_is_idempotent(env, no_spawn, monkeypatch):
    bus, repo = env["bus"], env["repo"]
    base, commit = git_repo(repo)
    h1 = commit("one\n")
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review("codex", str(repo), base, h1)["thread"]["id"]
    run_job(bus, tid)  # changes requested
    h2 = commit("two\n")
    claude.request_review("codex", str(repo), base, h2, prev_head_commit_id=h1)
    monkeypatch.setenv("FAKE_APPROVE", "codex")
    from agent_bus.worker import Worker

    def dies(self, status):
        raise RuntimeError("worker died")
    monkeypatch.setattr(Worker, "finish_thread", dies)
    with pytest.raises(RuntimeError):
        run_job(bus, tid)
    _reaped(bus, tid)
    Service(bus, HUMAN).resume(tid)
    assert bus.thread(tid)["status"] == "resolved"
    n = len(bus.jobs(tid))
    again = claude.request_review("codex", str(repo), base, h2, prev_head_commit_id=h1)
    assert again["thread"]["id"] == tid and len(bus.jobs(tid)) == n


def _reaped(bus, tid):
    """What reaping a dead worker leaves: its job failed."""
    bus.conn.execute("UPDATE jobs SET status='failed', error='worker died' WHERE id=?", (bus.jobs(tid)[-1]["id"],))
    assert not bus.active_job(tid)
