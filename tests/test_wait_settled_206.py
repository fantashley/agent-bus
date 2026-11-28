"""#208: `wait` returns at once when nothing can arrive on the thread.  #206: a resumed
several-reviewer job's self-hosted preflight checks the reviewer it hands the head to; the
`tail` status line says when the head is building; head-build attempts pair by explicit
phase and ignore an older worker's late start."""
import time

import pytest

from agent_bus import cli, worker
from agent_bus.db import HUMAN, head_build_attempt
from agent_bus.service import Service
from agent_bus.worker import Worker

from test_dead_worker_retry import BOTH, crash_at, run_crashing
from test_debrief import reviewed
from test_git import git_repo, no_jj  # noqa: F401 (a fixture)
from test_head_build import requested
from test_head_prompts import write_config
from test_review_rounds import run_job
from test_wait import call


def timed_wait(server, tid, timeout_s=3):
    start = time.monotonic()
    res = call(server, "wait", {"thread": tid, "timeout_s": timeout_s})
    return res, time.monotonic() - start


def test_wait_returns_at_once_when_nothing_can_arrive(env, no_spawn):
    from agent_bus import mcp_server
    bus, repo = env["bus"], env["repo"]
    sh = git_repo(repo)
    base = sh("git", "rev-parse", "HEAD")
    (repo / "a.txt").write_text("one\n")
    sh("git", "commit", "-qam", "one")
    head = sh("git", "rev-parse", "HEAD")
    server = mcp_server.build("claude", bus=bus)
    tid = call(server, "request_review", {"reviewer": "codex", "repo": str(repo), "base_commit_id": base,
                                          "head_commit_id": head})["thread"]["id"]
    # A job is queued: the wait sits out its timeout.
    res, took = timed_wait(server, tid, 2)
    assert took >= 1.5 and res["status"] == "timeout" and res["next_action"].startswith("wait:")
    run_job(bus, tid)  # codex asks for changes: the author's turn (fix:)
    res, took = timed_wait(server, tid)
    assert "messages" in res  # the verdict itself, as ever
    res, took = timed_wait(server, tid)
    assert took < 1 and res["status"] == "timeout" and res["next_action"].startswith("fix:")
    assert res["thread"]["status"] == "open" and "messages" not in res


def test_a_running_wait_returns_once_the_job_ends_settled(env, no_spawn):
    """codex-1 (#209): the verdict can be read while its job is still closing; a wait made
    then returns once the job ends, not after its whole timeout."""
    import asyncio

    from agent_bus import mcp_server
    from test_wait import acall
    svc, tid, _ = reviewed(env, "codex")
    bus = env["bus"]
    server = mcp_server.build("claude", bus=bus)
    jid = bus.jobs(tid)[-1]["id"]
    bus.conn.execute("UPDATE jobs SET status='running', lease_expires=? WHERE id=?", (time.time() + 60, jid))

    async def main():
        async def finish():
            await asyncio.sleep(1.5)
            bus.conn.execute("UPDATE jobs SET status='done' WHERE id=?", (jid,))
        start = time.monotonic()
        res, _ = await asyncio.gather(acall(server, "wait", {"thread": tid, "timeout_s": 10}), finish())
        return res, time.monotonic() - start
    res, took = asyncio.run(main())
    assert 1.4 < took < 4 and res["thread"]["status"] == "resolved"


@pytest.mark.parametrize("when", ["before", "during"])
def test_a_dead_debriefs_expired_lease_does_not_hold_the_wait(env, no_spawn, when):
    """codex-4 (#209): a running job whose lease has expired (its worker died) is reaped by
    the settled check, whether the lease expired before the wait or during it."""
    from agent_bus import mcp_server
    svc, tid, _ = reviewed(env, "codex")
    bus = env["bus"]
    svc.debrief(tid)
    jid = bus.jobs(tid)[-1]["id"]
    server = mcp_server.build("claude", bus=bus)
    call(server, "inbox", {"thread": tid})  # the thread's mail so far, read
    expires = time.time() + (-1 if when == "before" else 1.5)
    bus.conn.execute("UPDATE jobs SET status='running', lease_owner='dead', lease_expires=? WHERE id=?",
                     (expires, jid))
    res, took = timed_wait(server, tid, 10)
    assert (took < 1 if when == "before" else 1.4 < took < 4) and bus.job(jid)["status"] == "failed"
    assert res["thread"]["status"] == "resolved"


def test_wait_on_a_finished_thread_waits_only_for_a_running_debrief(env, no_spawn):
    from agent_bus import mcp_server
    svc, tid, _ = reviewed(env, "codex")
    server = mcp_server.build("claude", bus=env["bus"])
    assert svc.settled(tid)  # resolved, nothing running: nothing more can arrive
    res, took = timed_wait(server, tid)
    assert took < 1 and res["thread"]["status"] == "resolved"
    svc.debrief(tid)  # queued (never spawned here): its replies arrive on this thread
    assert not svc.settled(tid)
    res, took = timed_wait(server, tid, 2)
    assert took >= 1.5 and res["progress"]["job"]["status"] == "queued"


def test_a_needs_human_or_open_discussion_thread_still_waits(env, no_spawn):
    bus = env["bus"]
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.start_discussion("which?", peers=["codex"])["thread"]["id"]
    assert not claude.settled(tid)  # its job is queued
    bus.conn.execute("UPDATE jobs SET status='done' WHERE thread_id=?", (tid,))
    assert not claude.settled(tid)  # an open discussion: another participant may still post
    bus.transition(tid, "needs_human")
    assert not claude.settled(tid)  # the human may resume it


# ------------------------------------------------------------------ #206


def test_head_build_attempt_pairs_by_explicit_phase():
    def ev(purpose=None, text="", agent=None):
        ev.n += 1
        return {"kind": "head_build", "agent": agent, "purpose": purpose, "text": text, "created_at": ev.n}
    ev.n = 0
    h = "a" * 40
    # antigravity-1: only an end closes an attempt, and the owner is everything after the head.
    a = head_build_attempt([ev(f"start {h} host:1 x"), ev(f"other {h} host:1 x")])
    assert a["owner"] == "host:1 x" and a["end"] is None
    end = ev(f"end {h} host:1 x")
    assert head_build_attempt([ev(f"start {h} host:1 x"), end])["end"] == end["created_at"]
    # codex-5: an older worker's late, ownerless start doesn't replace the holder's open attempt...
    late = ev(text=f"building head {h[:12]} with nix")
    a = head_build_attempt([ev(f"start {h} B"), late], holder="B")
    assert a["owner"] == "B" and a["end"] is None
    assert head_build_attempt([ev(f"start {h} B"), late, ev(f"end {h} B")], holder="B")["end"] is not None
    # ...but does replace an attempt whose worker lost the job, or one that ended.
    assert head_build_attempt([ev(f"start {h} A"), late], holder="B")["owner"] is None
    # codex-2 (#209): nor the holder's ended one; with no attempt of the holder's, it does.
    a = head_build_attempt([ev(f"start {h} B"), ev(f"end {h} B"), late], holder="B")
    assert a["owner"] == "B" and a["end"] is not None
    assert head_build_attempt([ev(f"start {h} A"), ev(f"end {h} A"), late], holder="B")["end"] is None


def test_the_status_line_says_while_the_head_builds(env, no_spawn, monkeypatch):
    write_config(env, self_hosted_reviews=[str(env["repo"])])
    res, h = requested(env)
    bus, tid, jid = env["bus"], res["thread"]["id"], res["job"]["id"]
    assert cli.thread_status(bus, tid)[2] == f"{tid}: codex reviewing (pass 1)"
    assert bus.claim_job(jid, "A", lease_s=60)
    bus.add_event(tid, "head_build", f"building head {h[:12]} with nix", job_id=jid, purpose=f"start {h} A")
    seen = []
    real = cli.thread_status
    monkeypatch.setattr(cli, "thread_status", lambda b, t, builds=None: seen.append(builds) or real(b, t, builds))
    assert cli.main(["tail", tid, "--once", "--no-color"]) == 0
    builds = seen[-1]
    assert [e["purpose"] for e in builds[jid]] == [f"start {h} A"]
    assert real(bus, tid, builds)[2] == f"{tid}: building head {h[:12]} with nix (pass 1)"
    # Taken over by another worker: that attempt stopped with it.
    bus.conn.execute("UPDATE jobs SET lease_owner='B' WHERE id=?", (jid,))
    assert real(bus, tid, builds)[2] == f"{tid}: codex reviewing (pass 1)"
    bus.conn.execute("UPDATE jobs SET lease_owner='A' WHERE id=?", (jid,))
    bus.add_event(tid, "head_build", f"head {h[:12]}'s build ready after 2 s", job_id=jid, purpose=f"end {h} A")
    assert cli.main(["tail", tid, "--once", "--no-color"]) == 0
    assert real(bus, tid, seen[-1])[2] == f"{tid}: codex reviewing (pass 1)"


def test_a_resumed_jobs_preflight_checks_the_reviewer_it_hands_to(env, no_spawn, monkeypatch, no_jj, unchecked_commits):
    """codex-4 (#205): codex approved head3, then its worker died before the handoff;
    antigravity's previous head (head2) is gone.  The resumed self-hosted job reports it
    before any snapshot, build or prompts event, as for the job's first reviewer (#201)."""
    bus, repo = env["bus"], env["repo"]
    sh = git_repo(repo)
    base = sh("git", "rev-parse", "HEAD")

    def amend(text):
        (repo / "a.txt").write_text(text)
        sh("git", "commit", "-q", "--amend", "-am", text.strip())
        return sh("git", "rev-parse", "HEAD")
    (repo / "a.txt").write_text("one\n")
    sh("git", "commit", "-qam", "one")
    head1 = sh("git", "rev-parse", "HEAD")
    sh("git", "tag", "keep", head1)  # codex's previous head stays
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review(None, str(repo), base, head1, reviewers=BOTH)["thread"]["id"]
    run_job(bus, tid)  # codex asks for changes at head1
    head2 = amend("two\n")
    claude.request_review(None, str(repo), base, head2, prev_head_commit_id=head1, reviewers=BOTH, thread=tid)
    run_job(bus, tid)  # antigravity's round: it asks for changes at head2
    assert bus.review_standings(tid)["changes_requested"] == ["antigravity"]
    head3 = amend("three\n")
    sh("git", "reflog", "expire", "--expire=now", "--all")
    sh("git", "gc", "-q", "--prune=now")  # head2 is gone
    claude.request_review(None, str(repo), base, head3, prev_head_commit_id=head2, reviewers=BOTH, thread=tid)
    monkeypatch.setenv("FAKE_APPROVE", "codex")
    crash_at(monkeypatch, worker, "handoff")  # codex approves head3; its worker dies
    run_crashing(bus, tid)
    t = bus.thread(tid)
    assert t["refs"]["reviewer"] == "codex" and bus.review_standings(tid)["reviewers"]["codex"]["state"] == "approved"
    w = Worker(bus, bus.jobs(tid)[-1]["id"])
    first_t, first = w.first_pass(t)
    assert first["reviewer"] == "antigravity" and first_t["round"] == t["round"] + 1
    write_config(env, self_hosted_reviews=[str(repo)])
    setup = ("snapshot", "prompts", "head_build")
    seen = len([e for e in bus.events(tid) if e["kind"] in setup])
    Service(bus, HUMAN).resume(tid)
    Worker(bus, bus.jobs(tid)[-1]["id"]).run()
    assert bus.thread(tid)["status"] == "needs_human" and bus.jobs(tid)[-1]["status"] == "failed"
    report = [m["body"] for m in bus.history(tid)["messages"] if "Review not started" in m["body"]][-1]
    assert f"the previous head commit {head2}" in report
    assert len([e for e in bus.events(tid) if e["kind"] in setup]) == seen


@pytest.mark.parametrize("state", ["none", "last"])
def test_first_pass_is_the_current_reviewers_unless_it_has_a_verdict(env, no_spawn, monkeypatch, state):
    bus, repo = env["bus"], env["repo"]
    sh = git_repo(repo)
    base = sh("git", "rev-parse", "HEAD")
    (repo / "a.txt").write_text("one\n")
    sh("git", "commit", "-qam", "one")
    head = sh("git", "rev-parse", "HEAD")
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review(None, str(repo), base, head, reviewers=BOTH)["thread"]["id"]
    w = Worker(bus, bus.jobs(tid)[-1]["id"])
    t = bus.thread(tid)
    if state == "none":
        assert w.first_pass(t) == (t, t["refs"])
        return
    monkeypatch.setenv("FAKE_APPROVE", "codex,antigravity")
    crash_at(monkeypatch, Worker, "review_outcome")  # every approval in: no pass left to run
    run_crashing(bus, tid)
    assert w.first_pass(bus.thread(tid)) is None


# ------------------------------------------------------------------ #210


def test_the_poller_checks_may_settle_without_another_sweep(env, no_spawn, monkeypatch):
    """codex-5 (#210): each tick's poll already touched the session and swept; may_settle is
    a plain read, so it isn't run through call() (a second sweep every second)."""
    from agent_bus import autoresume, mcp_server
    bus, repo = env["bus"], env["repo"]
    sh = git_repo(repo)
    base = sh("git", "rev-parse", "HEAD")
    (repo / "a.txt").write_text("one\n")
    sh("git", "commit", "-qam", "one")
    head = sh("git", "rev-parse", "HEAD")
    server = mcp_server.build("claude", bus=bus)
    tid = call(server, "request_review", {"reviewer": "codex", "repo": str(repo), "base_commit_id": base,
                                          "head_commit_id": head})["thread"]["id"]
    sweeps, checks = [], []
    real_sweep, real_check = autoresume.sweep_quietly, Service.may_settle
    monkeypatch.setattr(autoresume, "sweep_quietly", lambda *a, **k: sweeps.append(1) or real_sweep(*a, **k))
    monkeypatch.setattr(Service, "may_settle", lambda self, t: checks.append(1) or real_check(self, t))
    res, took = timed_wait(server, tid, 3)  # its job is queued: the wait sits out its timeout
    assert res["status"] == "timeout" and len(checks) >= 2
    assert len(sweeps) <= len(checks) + 2  # one per tick's poll (and the wait's own calls), not two


def test_head_build_attempt_skips_unmatched_legacy_text():
    """antigravity-2 (#210): an ownerless head_build event whose text is neither of an older
    build's (start or end) closes no open legacy attempt."""
    def ev(text):
        ev.n += 1
        return {"kind": "head_build", "agent": None, "purpose": None, "text": text, "created_at": ev.n}
    ev.n = 0
    h = "b" * 12
    a = head_build_attempt([ev(f"building head {h} with nix"), ev("something else entirely")])
    assert a == {"head": h, "owner": None, "start": 1, "end": None}
    end = ev(f"head {h}'s build ready after 3 s")
    assert head_build_attempt([ev(f"building head {h} with nix"), ev("odd"), end])["end"] == end["created_at"]
    assert head_build_attempt([ev("odd")]) is None
