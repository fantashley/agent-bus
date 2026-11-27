"""#154 item 1: a review pass that gave no usable verdict (a turn timeout, a crash, an
unusable reply) still parks the thread for the human user, but its initiator may retry it
once per reviewer per base..head by requesting the same head again."""
import pytest

from agent_bus.db import HUMAN, SYSTEM, BusError
from agent_bus.service import Service

from test_review_rounds import git_repo, run_job

BOTH = ["codex", "antigravity"]


def fail(monkeypatch, agent):
    """``agent``'s next pass gives no usable verdict, its repair turn included."""
    monkeypatch.setenv(f"FAKE_REPLY_{agent.upper()}", "Ran out of time before a verdict.")
    monkeypatch.setenv(f"FAKE_REPAIR_{agent.upper()}", "Still no verdict.")


def recover(monkeypatch, agent):
    monkeypatch.delenv(f"FAKE_REPLY_{agent.upper()}")
    monkeypatch.delenv(f"FAKE_REPAIR_{agent.upper()}")


def notices(bus, tid):
    return [m["body"] for m in bus.history(tid)["messages"] if m["sender"] == SYSTEM]


def test_a_failed_pass_offers_one_retry_that_reruns_it(env, no_spawn, monkeypatch):
    bus, repo = env["bus"], env["repo"]
    monkeypatch.setenv("FAKE_APPROVE", "codex")
    fail(monkeypatch, "codex")
    base, commit = git_repo(repo)
    head = commit("one\n")
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review("codex", str(repo), base, head)["thread"]["id"]
    job = run_job(bus, tid)
    t = bus.thread(tid)
    assert t["status"] == "needs_human" and job["status"] == "done"
    assert t["refs"]["retryable"]["reviewer"] == "codex"
    call = (f"call request_review with reviewer=\"codex\" (only that reviewer), thread=\"{tid}\", repo=\"{repo}\", "
            f"base_commit_id={base}, head_commit_id={head} and prev_head_commit_id={head}")
    assert call in notices(bus, tid)[-1] and "codex did not return a usable verdict" in notices(bus, tid)[-1]
    nxt = claude.thread_state(tid)["next_action"]
    assert nxt.startswith(f"retry: codex's pass at head {head} gave no usable verdict") and call in nxt
    # The retry: the same head again, not a follow-up (the budget doesn't count it).
    # Without `thread`, a same-head request is refused, and says how to retry (codex-1).
    with pytest.raises(BusError) as e:
        claude.request_review("codex", str(repo), base, head, prev_head_commit_id=head)
    assert e.value.code == "REVIEW_FAILED" and call in e.value.message
    recover(monkeypatch, "codex")
    res = claude.request_review("codex", str(repo), base, head, prev_head_commit_id=head, thread=tid)
    assert res["thread"]["id"] == tid and res["job"]["status"] == "queued"
    t = bus.thread(tid)
    assert t["status"] == "open" and t["refs"]["follow_ups"] == 0 and "retryable" not in t["refs"]
    assert bus.jobs(tid)[-1]["params"]["mode"] == "retry"
    run_job(bus, tid)
    assert bus.thread(tid)["status"] == "resolved"
    assert claude.thread_state(tid)["next_action"].startswith("done:")
    # Repeating the retry call reopens nothing and spends nothing (codex-3).
    with pytest.raises(BusError) as e:
        claude.request_review("codex", str(repo), base, head, prev_head_commit_id=head, thread=tid)
    assert e.value.code == "REVIEW_FAILED"
    assert bus.thread(tid)["status"] == "resolved" and bus.thread(tid)["refs"]["follow_ups"] == 0


def test_a_second_failure_at_the_same_head_waits_for_the_human(env, no_spawn, monkeypatch):
    bus, repo = env["bus"], env["repo"]
    fail(monkeypatch, "codex")
    base, commit = git_repo(repo)
    head = commit("one\n")
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review("codex", str(repo), base, head)["thread"]["id"]
    run_job(bus, tid)
    claude.request_review("codex", str(repo), base, head, prev_head_commit_id=head, thread=tid)
    run_job(bus, tid)
    t = bus.thread(tid)
    assert t["status"] == "needs_human" and "retryable" not in t["refs"]
    assert "the requester's one retry at this head is used" in notices(bus, tid)[-1]
    assert claude.thread_state(tid)["next_action"].startswith("stop:")
    # Neither the retry call nor a plain follow-up of the same head reruns it now.
    for extra in ({}, {"thread": tid}):
        with pytest.raises(BusError) as e:
            claude.request_review("codex", str(repo), base, head, prev_head_commit_id=head, **extra)
        assert e.value.code == "REVIEW_FAILED" and f"agent-bus resume {tid}" in e.value.message
    # A new head is new work, with its own retry.
    h2 = commit("two\n")
    claude.request_review("codex", str(repo), base, h2, prev_head_commit_id=head)
    run_job(bus, tid)
    assert bus.thread(tid)["refs"]["retryable"]["reviewer"] == "codex"


def test_several_reviewers_retry_only_the_one_that_failed(env, no_spawn, monkeypatch):
    bus, repo = env["bus"], env["repo"]
    monkeypatch.setenv("FAKE_APPROVE", "codex,antigravity")
    fail(monkeypatch, "antigravity")
    base, commit = git_repo(repo)
    head = commit("one\n")
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review(None, str(repo), base, head, reviewers=BOTH)["thread"]["id"]
    run_job(bus, tid)
    assert bus.thread(tid)["status"] == "needs_human"
    nxt = claude.thread_state(tid)["next_action"]
    assert nxt.startswith("retry: antigravity's pass") and f'reviewer="antigravity" (only that reviewer), thread="{tid}"' in nxt
    recover(monkeypatch, "antigravity")
    res = claude.request_review("antigravity", str(repo), base, head, prev_head_commit_id=head, thread=tid)
    assert res["thread"]["id"] == tid and bus.thread(tid)["refs"]["follow_ups"] == 0
    before = len([m for m in bus.history(tid)["messages"] if m["kind"] == "review_verdict"])
    run_job(bus, tid)
    verdicts = [m for m in bus.history(tid)["messages"] if m["kind"] == "review_verdict"][before:]
    assert [m["sender"] for m in verdicts] == ["antigravity"]  # codex's approval stands
    assert bus.thread(tid)["status"] == "resolved"



def test_a_human_resume_replaces_the_offer(env, no_spawn, monkeypatch):
    bus, repo = env["bus"], env["repo"]
    fail(monkeypatch, "codex")
    base, commit = git_repo(repo)
    head = commit("one\n")
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review("codex", str(repo), base, head)["thread"]["id"]
    run_job(bus, tid)
    Service(bus, HUMAN).resume(tid)
    t = bus.thread(tid)
    assert t["status"] == "open" and "retryable" not in t["refs"]
    assert not claude.thread_state(tid)["next_action"].startswith("retry:")


def test_the_retry_call_names_its_thread(env, no_spawn, monkeypatch):
    """codex-1: another review of the same head (here a review-only one) doesn't take the retry."""
    bus, repo = env["bus"], env["repo"]
    base, commit = git_repo(repo)
    head = commit("one\n")
    claude = Service(bus, "claude", bus.register_session("claude"))
    other = claude.request_review("codex", str(repo), base, head, review_only=True)["thread"]["id"]
    run_job(bus, other)  # changes requested
    fail(monkeypatch, "codex")
    tid = claude.request_review("codex", str(repo), base, head, review_only=False)["thread"]["id"]
    assert tid != other
    run_job(bus, tid)
    recover(monkeypatch, "codex")
    res = claude.request_review("codex", str(repo), base, head, prev_head_commit_id=head, thread=tid)
    assert res["thread"]["id"] == tid and bus.jobs(tid)[-1]["params"]["mode"] == "retry"
    assert len(bus.jobs(other)) == 1
    # `thread` with one reviewer names only that reviewer's review.
    with pytest.raises(ValueError, match="goes with `reviewers`"):
        claude.request_review("antigravity", str(repo), base, head, prev_head_commit_id=head, thread=tid)


def test_a_spent_retry_stays_spent_after_a_pushback(env, no_spawn, monkeypatch):
    """codex-2: the retry, a request for changes, then a pushback that fails too: no second offer."""
    bus, repo = env["bus"], env["repo"]
    fail(monkeypatch, "codex")
    base, commit = git_repo(repo)
    head = commit("one\n")
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review("codex", str(repo), base, head)["thread"]["id"]
    run_job(bus, tid)
    recover(monkeypatch, "codex")
    claude.request_review("codex", str(repo), base, head, prev_head_commit_id=head, thread=tid)
    run_job(bus, tid)
    assert bus.review_standings(tid)["changes_requested"] == ["codex"]
    claude.send("It is not a typo: see the spec.", thread=tid)
    claude.request_review("codex", str(repo), base, head, prev_head_commit_id=head)  # the pushback
    fail(monkeypatch, "codex")
    run_job(bus, tid)
    t = bus.thread(tid)
    assert t["status"] == "needs_human" and "retryable" not in t["refs"]
    assert claude.thread_state(tid)["next_action"].startswith("stop:")


def test_a_parked_pass_is_not_rerun_by_another_reviewers_pushback(env, no_spawn, monkeypatch):
    """codex-4: on a review-only review, a pushback for codex can't hand the parked head back to
    antigravity, whose pass failed twice."""
    bus, repo = env["bus"], env["repo"]
    fail(monkeypatch, "antigravity")
    base, commit = git_repo(repo)
    head = commit("one\n")
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review(None, str(repo), base, head, reviewers=BOTH, review_only=True)["thread"]["id"]
    run_job(bus, tid)  # codex asks for changes, antigravity gives no verdict
    claude.request_review("antigravity", str(repo), base, head, prev_head_commit_id=head, thread=tid)
    run_job(bus, tid)
    assert bus.thread(tid)["status"] == "needs_human" and claude.thread_state(tid)["next_action"].startswith("stop:")
    claude.send("Evidence for codex.", thread=tid)
    with pytest.raises(BusError) as e:
        claude.request_review(None, str(repo), base, head, prev_head_commit_id=head, reviewers=BOTH, thread=tid)
    assert e.value.code == "REVIEW_FAILED"


def test_no_offer_without_room_for_the_verdict(env, no_spawn, monkeypatch):
    """codex-5: a thread at its message cap needs the human's resume, which raises the cap."""
    bus, repo = env["bus"], env["repo"]
    fail(monkeypatch, "codex")
    base, commit = git_repo(repo)
    head = commit("one\n")
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review("codex", str(repo), base, head)["thread"]["id"]
    bus.set_thread(tid, max_messages=0)
    run_job(bus, tid)
    t = bus.thread(tid)
    assert t["status"] == "needs_human" and "retryable" not in t["refs"]
    assert "Run `agent-bus resume` to retry" in notices(bus, tid)[-1]


def test_an_offer_the_thread_has_no_room_for_is_withdrawn(env, no_spawn, monkeypatch):
    """antigravity-1: messages posted after the park filled the thread: next_action and the
    retry call agree that only a resume helps."""
    bus, repo = env["bus"], env["repo"]
    fail(monkeypatch, "codex")
    base, commit = git_repo(repo)
    head = commit("one\n")
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review("codex", str(repo), base, head)["thread"]["id"]
    run_job(bus, tid)
    assert claude.thread_state(tid)["next_action"].startswith("retry:")
    bus.set_thread(tid, max_messages=bus.thread(tid)["agent_messages"])
    assert claude.thread_state(tid)["next_action"].startswith("stop:")
    with pytest.raises(BusError) as e:
        claude.request_review("codex", str(repo), base, head, prev_head_commit_id=head, thread=tid)
    assert e.value.code == "REVIEW_FAILED" and "agent-bus resume" in e.value.message


def test_an_unavailable_reviewer_does_not_spend_the_retry(env, no_spawn, monkeypatch):
    """antigravity-3."""
    bus, repo = env["bus"], env["repo"]
    fail(monkeypatch, "codex")
    base, commit = git_repo(repo)
    head = commit("one\n")
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review("codex", str(repo), base, head)["thread"]["id"]
    run_job(bus, tid)

    def gone(agent, role="agent"):
        raise BusError("AGENT_UNAVAILABLE", f"{agent} isn't available")
    monkeypatch.setattr(claude.config, "require_available", gone)
    with pytest.raises(BusError) as e:
        claude.request_review("codex", str(repo), base, head, prev_head_commit_id=head, thread=tid)
    assert e.value.code == "AGENT_UNAVAILABLE"
    t = bus.thread(tid)
    assert t["status"] == "needs_human" and t["refs"]["retryable"] and "retried" not in t["refs"]


def test_a_retry_call_spends_only_its_reviewers_offer(env, no_spawn, monkeypatch):
    """codex-3: codex's retry passes and hands the head to antigravity, which fails too.
    Replaying codex's call (or the reviewers' list) doesn't spend antigravity's offer."""
    bus, repo = env["bus"], env["repo"]
    monkeypatch.setenv("FAKE_APPROVE", "codex,antigravity")
    fail(monkeypatch, "codex")
    base, commit = git_repo(repo)
    head = commit("one\n")
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review(None, str(repo), base, head, reviewers=BOTH)["thread"]["id"]
    run_job(bus, tid)
    recover(monkeypatch, "codex")
    fail(monkeypatch, "antigravity")
    claude.request_review("codex", str(repo), base, head, prev_head_commit_id=head, thread=tid)
    run_job(bus, tid)
    assert claude.thread_state(tid)["next_action"].startswith("retry: antigravity's pass")
    for call in (lambda: claude.request_review("codex", str(repo), base, head, prev_head_commit_id=head, thread=tid),
                 lambda: claude.request_review(None, str(repo), base, head, prev_head_commit_id=head,
                                               reviewers=BOTH, thread=tid)):
        with pytest.raises(BusError) as e:
            call()
        assert e.value.code == "REVIEW_FAILED"
    assert bus.thread(tid)["refs"]["retryable"]["reviewer"] == "antigravity"
    assert bus.thread(tid)["refs"]["follow_ups"] == 0
