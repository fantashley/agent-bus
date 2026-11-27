"""#199: when the worker of a several-reviewer review's first request dies mid-pass (its job
failed or cancelled, the thread still open), repeating the request retries the pass on the
same thread once per reviewer per base..head, charged to the same `retried` marker as
#154's initiator retry; after that only the human user's `agent-bus resume` does."""
import time

import pytest

from agent_bus import worker
from agent_bus.db import HUMAN, SYSTEM, BusError
from agent_bus.service import Service
from agent_bus.worker import Worker

from test_git import MISSING, no_jj  # noqa: F401 (a fixture)
from test_initiator_retry import fail, recover
from test_review_rounds import git_repo, run_job

BOTH = ["codex", "antigravity"]


def start(env, monkeypatch, approve="codex,antigravity"):
    bus, repo = env["bus"], env["repo"]
    monkeypatch.setenv("FAKE_APPROVE", approve)
    base, commit = git_repo(repo)
    head = commit("one\n")
    claude = Service(bus, "claude", bus.register_session("claude"))
    request = lambda: claude.request_review(None, str(repo), base, head, reviewers=BOTH)  # noqa: E731
    tid = request()["thread"]["id"]
    return bus, claude, request, tid, base, head


def die(bus, tid):
    """The thread's running or queued job's worker dies holding its lease: the next call reaps it."""
    jid = bus.jobs(tid)[-1]["id"]
    bus.claim_job(jid, "dead", lease_s=-1)
    assert bus.active_job(tid) is None and bus.job(jid)["status"] == "failed"


def crash_at(monkeypatch, target, name):
    """The worker dies at ``target.name`` (once): its job ends failed, the thread stays as it was."""
    real = getattr(target, name)
    hit = []

    def boom(*a, **kw):
        if not hit:
            hit.append(1)
            raise RuntimeError("worker died")
        return real(*a, **kw)
    monkeypatch.setattr(target, name, boom)


def run_crashing(bus, tid):
    with pytest.raises(RuntimeError, match="worker died"):
        run_job(bus, tid)
    assert bus.jobs(tid)[-1]["status"] == "failed"


def test_a_dead_workers_pass_is_retried_once_then_only_resume(env, no_spawn, monkeypatch):
    bus, claude, request, tid, base, head = start(env, monkeypatch)
    die(bus, tid)
    res = request()
    assert res["thread"]["id"] == tid and res["job"]["status"] == "queued"
    t, job = bus.thread(tid), bus.jobs(tid)[-1]
    assert t["status"] == "open" and t["refs"]["retried"] == [f"codex@{base}..{head}"]
    assert job["params"] == {"mode": "retry", "round": t["round"]}
    assert job["dedupe_key"] == worker.retry_key(t, "codex")
    # A queued (or running, or done) retry is idempotent: the repeat spends nothing more.
    again = request()
    assert again["job"]["id"] == job["id"] and bus.thread(tid)["refs"]["retried"] == t["refs"]["retried"]
    die(bus, tid)
    jobs = len(bus.jobs(tid))
    with pytest.raises(BusError) as e:
        request()
    assert e.value.code == "REVIEW_FAILED" and f"agent-bus resume {tid}" in e.value.message
    assert "codex's one retry at this head is used" in e.value.message
    assert len(bus.jobs(tid)) == jobs and bus.thread(tid)["refs"]["retried"] == t["refs"]["retried"]
    # The human's resume still retries it.
    Service(bus, HUMAN).resume(tid)
    run_job(bus, tid)
    assert bus.thread(tid)["status"] == "resolved"


def test_an_initiator_retry_spends_the_same_retry(env, no_spawn, monkeypatch):
    bus, claude, request, tid, base, head = start(env, monkeypatch)
    fail(monkeypatch, "codex")
    run_job(bus, tid)  # parked with the retry offer (#154)
    recover(monkeypatch, "codex")
    claude.request_review("codex", str(env["repo"]), base, head, prev_head_commit_id=head, thread=tid)
    die(bus, tid)
    with pytest.raises(BusError) as e:
        request()
    assert e.value.code == "REVIEW_FAILED" and f"agent-bus resume {tid}" in e.value.message


def test_a_quota_refusal_does_not_spend_the_retry(env, no_spawn, monkeypatch):
    bus, claude, request, tid, base, head = start(env, monkeypatch)
    die(bus, tid)
    bus.set_quota_block("codex", time.time() + 600, "heuristic", "quota")
    with pytest.raises(BusError) as e:
        request()
    assert e.value.code == "USAGE_EXHAUSTED"
    assert "retried" not in bus.thread(tid)["refs"] and bus.jobs(tid)[-1]["status"] == "failed"
    bus.clear_quota("codex")
    assert request()["job"]["status"] == "queued"
    assert bus.thread(tid)["refs"]["retried"] == [f"codex@{base}..{head}"]


def test_no_room_for_the_verdict_refuses_without_spending(env, no_spawn, monkeypatch):
    bus, claude, request, tid, base, head = start(env, monkeypatch)
    die(bus, tid)
    bus.set_thread(tid, max_messages=bus.thread(tid)["agent_messages"])
    with pytest.raises(BusError) as e:
        request()
    assert e.value.code == "REVIEW_FAILED" and "no room left for its verdict" in e.value.message
    assert f"agent-bus resume {tid}" in e.value.message and "retried" not in bus.thread(tid)["refs"]


def test_a_thread_with_every_approval_in_is_resolved_not_retried(env, no_spawn, monkeypatch):
    bus, claude, request, tid, base, head = start(env, monkeypatch)
    crash_at(monkeypatch, Worker, "review_outcome")  # died after the last approval
    run_crashing(bus, tid)
    assert bus.thread(tid)["status"] == "open" and bus.review_standings(tid)["complete"]
    jobs = len(bus.jobs(tid))
    res = request()
    assert res["job"] is None and len(bus.jobs(tid)) == jobs
    t = bus.thread(tid)
    assert t["status"] == "resolved" and "retried" not in t["refs"]
    assert "Review complete" in [m for m in bus.history(tid)["messages"] if m["sender"] == SYSTEM][-1]["body"]


def test_a_worker_that_died_after_an_approval_charges_the_next_reviewer(env, no_spawn, monkeypatch):
    bus, claude, request, tid, base, head = start(env, monkeypatch)
    crash_at(monkeypatch, worker, "handoff")  # codex approved; antigravity never started
    run_crashing(bus, tid)
    assert bus.thread(tid)["refs"]["reviewer"] == "codex"
    # The same reviewer report_missing's notice checks (codex-1).
    assert worker.dead_pass_retry(bus, bus.thread(tid)) == ("antigravity", None)
    request()
    assert bus.thread(tid)["refs"]["retried"] == [f"antigravity@{base}..{head}"]
    assert bus.jobs(tid)[-1]["dedupe_key"] == worker.retry_key(bus.thread(tid), "antigravity")
    before = len([m for m in bus.history(tid)["messages"] if m["kind"] == "review_verdict"])
    run_job(bus, tid)
    verdicts = [m["sender"] for m in bus.history(tid)["messages"] if m["kind"] == "review_verdict"][before:]
    assert verdicts == ["antigravity"] and bus.thread(tid)["status"] == "resolved"


def test_the_missing_commit_notice_offers_the_repeat_only_while_the_retry_is_unused(
        env, no_spawn, unchecked_commits, no_jj):
    bus, repo = env["bus"], env["repo"]
    sh = git_repo(repo)[0]
    claude = Service(bus, "claude", bus.register_session("claude"))
    request = lambda: claude.request_review(None, str(repo), sh, MISSING, reviewers=BOTH)  # noqa: E731
    tid = request()["thread"]["id"]
    Worker(bus, bus.jobs(tid)[-1]["id"]).run()
    notice = lambda: [m["body"] for m in bus.history(tid)["messages"] if "Review not started" in m["body"]][-1]  # noqa: E731
    assert "request this review again" in notice()
    assert request()["thread"]["id"] == tid  # the one retry
    Worker(bus, bus.jobs(tid)[-1]["id"]).run()
    assert bus.thread(tid)["status"] == "needs_human"
    assert "request this review again" not in notice() and f"agent-bus resume {tid}" in notice()
    with pytest.raises(BusError) as e:
        request()
    assert e.value.code == "REVIEW_FAILED"


@pytest.mark.parametrize("why", ["quota", "format"])
def test_a_human_only_park_ends_its_job_with_the_park(env, no_spawn, monkeypatch, why):
    """A worker that dies right after a quota or format-drift park leaves the job done, not
    failed, so repeating the request can't retry past the human's resume."""
    monkeypatch.setenv(*{"quota": ("FAKE_EXHAUSTED", "codex"), "format": ("FAKE_BEHAVIOUR", "silent")}[why])
    bus, claude, request, tid, base, head = start(env, monkeypatch)
    crash_at(monkeypatch, Worker, "after_park")
    with pytest.raises(RuntimeError, match="worker died"):
        run_job(bus, tid)
    assert bus.thread(tid)["status"] == "needs_human" and bus.jobs(tid)[-1]["status"] == "done"
    if why == "quota":
        bus.clear_quota("codex")
    jobs = len(bus.jobs(tid))
    request()  # idempotent: the park stands
    assert len(bus.jobs(tid)) == jobs and bus.thread(tid)["status"] == "needs_human"
    assert "retried" not in bus.thread(tid)["refs"]


def test_a_cancelled_thread_is_not_retried(env, no_spawn, monkeypatch):
    bus, claude, request, tid, base, head = start(env, monkeypatch)
    Service(bus, HUMAN).cancel(tid)
    assert request()["thread"]["id"] != tid


def test_the_missing_commit_notice_offers_no_repeat_without_room_for_the_verdict(
        env, no_spawn, unchecked_commits, no_jj):
    bus, repo = env["bus"], env["repo"]
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review(None, str(repo), git_repo(repo)[0], MISSING, reviewers=BOTH)["thread"]["id"]
    bus.set_thread(tid, max_messages=bus.thread(tid)["agent_messages"])
    Worker(bus, bus.jobs(tid)[-1]["id"]).run()
    body = [m["body"] for m in bus.history(tid)["messages"] if "Review not started" in m["body"]][-1]
    assert "request this review again" not in body and f"agent-bus resume {tid}" in body


def test_a_park_whose_worker_died_before_ending_its_job_stays_human_only(env, no_spawn, monkeypatch):
    """codex-2: an older build's park (out of usage, say) whose worker died before marking its
    job done leaves needs_human and a failed job; a repeat of the request doesn't reopen it."""
    bus, claude, request, tid, base, head = start(env, monkeypatch)
    bus.transition(tid, "needs_human")
    die(bus, tid)
    jobs = len(bus.jobs(tid))
    with pytest.raises(BusError) as e:
        request()
    assert e.value.code == "REVIEW_FAILED" and "waits for the human user" in e.value.message
    assert f"agent-bus resume {tid}" in e.value.message
    assert len(bus.jobs(tid)) == jobs and "retried" not in bus.thread(tid)["refs"]


def test_a_missing_commit_park_is_recognised_by_its_jobs_error(env, no_spawn, monkeypatch):
    """codex-3: the repeat that a missing commit's notice offers works on a park any build made
    (by its job's error, recorded the same way since #50); any other failed job of a parked
    thread stays human-only."""
    bus, claude, request, tid, base, head = start(env, monkeypatch)
    jid = bus.jobs(tid)[-1]["id"]
    bus.claim_job(jid, "old", lease_s=60)
    bus.transition(tid, "needs_human")
    assert bus.finish_job(jid, "failed", "old", str(worker.MissingRevision(str(env["repo"]), [("head", head)])))
    refs = bus.thread(tid)["refs"]
    assert worker.MissingRevision.failed(bus.job(jid), refs)
    assert request()["job"]["status"] == "queued"
    assert bus.thread(tid)["status"] == "open" and bus.thread(tid)["refs"]["retried"] == [f"codex@{base}..{head}"]
    assert not worker.MissingRevision.failed({"status": "failed", "error": "worker died (lease expired or never claimed)"}, refs)
    assert not worker.MissingRevision.failed({"status": "failed", "error": "RuntimeError('could not compute diff')"}, refs)


def test_a_missing_commit_park_matches_the_whole_error():
    """#204: the error must be report_missing's, whole, for this thread's repo and pinned ids
    (the format older builds wrote too), not merely contain one."""
    repo, base, head, old = "/src/my repo", "b" * 40, "c" * 40, "a" * 40
    refs = {"repo": repo, "base_commit_id": base, "head_commit_id": head, "prev_head_commit_id": old,
            "heads": [old, head]}
    failed = lambda error, status="failed": worker.MissingRevision.failed({"status": status, "error": error}, refs)  # noqa: E731
    for missing in ([("head", head)], [("base", base)], [("base", base), ("head", head)],
                    [("previous head", old)], [("previous head", old), ("head", head)]):
        assert failed(str(worker.MissingRevision(repo, missing)))
    # An older build's error, written the same way since #50.
    assert failed(f"{repo} has no commit {base} (base), {head} (head)")
    assert not failed(str(worker.MissingRevision(repo, [("head", head)])), status="done")
    for error in (
            "REPO_NOT_ALLOWED: /x has no commit a (head) is not allowed",  # codex-4's example
            f"REPO_NOT_ALLOWED: {repo} has no commit {head} (head) is not allowed",
            f"x{repo} has no commit {head} (head)",
            f"/other has no commit {head} (head)",
            f"{repo} has no commit {head} (head) and more",
            f"{repo} has no commit {base} (head)",  # not the pinned head
            f"{repo} has no commit {'d' * 40} (previous head)",  # never a head of this thread
            f"{repo} has no commit {head} (head), {base} (base)",  # not report_missing's order
            f"{repo} has no commit ",
            ""):
        assert not failed(error), error
    assert not worker.MissingRevision.failed(None, refs)
    assert not worker.MissingRevision.failed({"status": "failed", "error": f"None has no commit {head} (head)"}, {})
