"""#71: severity that decides what blocks, a follow-up budget, and same-head pushback."""
import json

import pytest

from agent_bus.db import HUMAN, BusError
from agent_bus.peers import TurnResult
from agent_bus.service import Service, thread_summary
from agent_bus.worker import Worker, verdict_problem
from test_review_rounds import BOTH, calls, git_repo, run_job


def set_config(env, **kw):
    conf = env["tmp"] / "config" / "agent-bus" / "config.json"
    conf.write_text(json.dumps({**json.loads(conf.read_text()), **kw}))


def test_pushback_on_the_same_head_reruns_the_blocking_reviewer(env, no_spawn, monkeypatch):
    bus, repo = env["bus"], env["repo"]
    base, commit = git_repo(repo)
    h1 = commit("one\n")
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review(None, str(repo), base, h1, reviewers=BOTH)["thread"]["id"]
    run_job(bus, tid)  # codex asks for changes
    notice = claude.inbox(thread=tid)["messages"][-1]["body"]
    assert "push back" in notice and "[high, blocking]" in bus.last_verdict(tid, "codex")["body"]

    # No evidence yet: refused, and the error says how to push back.
    with pytest.raises(BusError) as e:
        claude.request_review(None, str(repo), base, h1, prev_head_commit_id=h1, reviewers=BOTH)
    assert e.value.code == "CHANGES_REQUESTED" and "send" in str(e.value)
    assert len(bus.jobs(tid)) == 1

    claude.send("PUSHBACK: the typo is intended, see the spec", thread=tid)
    res = claude.request_review(None, str(repo), base, h1, prev_head_commit_id=h1, reviewers=BOTH)
    assert res["job"] and bus.thread(tid)["refs"]["reviewer"] == "codex"
    assert res["review"]["reviewers"]["codex"]["state"] == "reviewing"
    assert res["thread"]["follow_ups"] == 1 and "max_rounds" not in res["thread"]
    # An exact repeat is idempotent.
    again = claude.request_review(None, str(repo), base, h1, prev_head_commit_id=h1, reviewers=BOTH)
    assert again["job"]["id"] == res["job"]["id"] and len(bus.jobs(tid)) == 2

    monkeypatch.setenv("FAKE_APPROVE", "codex,antigravity")
    run_job(bus, tid)
    codex2 = calls(env)[1]
    assert codex2["agent"] == "codex" and codex2["argv"][:3] == ["exec", "resume", "--json"]
    assert "has not changed the code" in codex2["prompt"] and "PUSHBACK" in codex2["prompt"]
    assert "```diff" not in codex2["prompt"]
    st = bus.review_standings(tid)
    assert st["complete"] and bus.thread(tid)["status"] == "resolved"
    assert st["follow_ups"] == 1 and st["passes"] == 3
    assert bus.thread(tid)["max_rounds"] == 1  # reviews no longer bump the discussion cap


def test_pushback_keeps_other_approvals_and_is_allowed_once(env, no_spawn, monkeypatch):
    bus, repo = env["bus"], env["repo"]
    base, commit = git_repo(repo)
    h1 = commit("one\n")
    monkeypatch.setenv("FAKE_APPROVE", "codex")
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review(None, str(repo), base, h1, reviewers=BOTH)["thread"]["id"]
    run_job(bus, tid)  # codex approves, antigravity asks for changes
    st = bus.review_standings(tid)
    assert st["approved"] == ["codex"] and st["changes_requested"] == ["antigravity"]

    res = claude.request_review(None, str(repo), base, h1, prev_head_commit_id=h1, reviewers=BOTH,
                                focus="PUSHBACK-FOCUS: this is fine because of X")
    assert res["review"]["reviewers"]["codex"]["state"] == "approved"
    assert res["review"]["reviewers"]["antigravity"]["state"] == "reviewing"
    run_job(bus, tid)  # antigravity still asks for changes
    agents = [c["agent"] for c in calls(env)]
    assert agents == ["codex", "antigravity", "antigravity"]
    assert "PUSHBACK-FOCUS" in calls(env)[2]["prompt"]
    st = bus.review_standings(tid)
    assert st["approved"] == ["codex"] and st["changes_requested"] == ["antigravity"]

    claude.send("more evidence", thread=tid)
    with pytest.raises(BusError) as e:
        claude.request_review(None, str(repo), base, h1, prev_head_commit_id=h1, reviewers=BOTH)
    assert e.value.code == "CHANGES_REQUESTED" and "already reconsidered" in str(e.value)

    # A new head is a new revision, which allows a pushback again.
    h2 = commit("two\n")
    claude.request_review(None, str(repo), base, h2, prev_head_commit_id=h1, reviewers=BOTH)
    assert "pushbacks" not in bus.thread(tid)["refs"]


def test_failed_pushback_keeps_the_request_for_changes(env, no_spawn, monkeypatch):
    bus, repo = env["bus"], env["repo"]
    base, commit = git_repo(repo)
    h1 = commit("one\n")
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review(None, str(repo), base, h1, reviewers=BOTH)["thread"]["id"]
    run_job(bus, tid)
    claude.send("evidence", thread=tid)
    first = claude.request_review(None, str(repo), base, h1, prev_head_commit_id=h1, reviewers=BOTH)
    monkeypatch.setenv("FAKE_EXHAUSTED", "codex")
    run_job(bus, tid)
    assert bus.thread(tid)["status"] == "needs_human"
    assert bus.review_standings(tid)["changes_requested"] == ["codex"]
    # Repeating it is still the same request, not a second pushback.
    again = claude.request_review(None, str(repo), base, h1, prev_head_commit_id=h1, reviewers=BOTH)
    assert again["job"]["id"] == first["job"]["id"]


def test_follow_up_budget_warns_then_waits_for_the_human(env, no_spawn):
    set_config(env, review_followup_budget=1)
    bus, repo = env["bus"], env["repo"]
    base, commit = git_repo(repo)
    h1 = commit("one\n")
    claude = Service(bus, "claude", bus.register_session("claude"))
    res = claude.request_review(None, str(repo), base, h1, reviewers=BOTH)
    tid = res["thread"]["id"]
    assert res["thread"]["followup_budget"] == 1 and res["thread"]["followup_limit"] == 2
    run_job(bus, tid)
    assert "run long" not in claude.inbox(thread=tid)["messages"][-1]["body"]

    h2 = commit("two\n")
    claude.request_review(None, str(repo), base, h2, prev_head_commit_id=h1, reviewers=BOTH)
    run_job(bus, tid)  # antigravity asks for changes: the soft budget is spent
    t = bus.thread(tid)
    assert t["status"] == "open" and thread_summary(t)["budget_warning"]
    assert "run long" in claude.inbox(thread=tid)["messages"][-1]["body"]

    h3 = commit("three\n")
    claude.request_review(None, str(repo), base, h3, prev_head_commit_id=h2, reviewers=BOTH)
    run_job(bus, tid)  # codex, past the budget, still asks for changes: the human decides
    codex2 = calls(env)[-1]
    assert codex2["agent"] == "codex" and "This review has run long" in codex2["prompt"]
    assert "fixed, withdrawn, or still blocking" in codex2["prompt"]
    t = bus.thread(tid)
    assert t["status"] == "needs_human" and thread_summary(t)["waiting_for_human"]
    assert "waits for the human user" in claude.inbox(thread=tid)["messages"][-1]["body"]

    h4 = commit("four\n")
    with pytest.raises(BusError) as e:
        claude.request_review(None, str(repo), base, h4, prev_head_commit_id=h3, reviewers=BOTH)
    assert e.value.code == "REVIEW_LIMIT_REACHED"

    res = Service(bus, HUMAN).resume(tid)
    assert not res.get("job") and res["thread"]["status"] == "open"
    assert res["thread"]["followup_limit"] == 3 and not res["thread"]["waiting_for_human"]
    res = claude.request_review(None, str(repo), base, h4, prev_head_commit_id=h3, reviewers=BOTH)
    assert res["job"] and res["thread"]["follow_ups"] == 3


def test_single_reviewer_pushback_and_budget(env, no_spawn):
    set_config(env, review_followup_budget=1)
    bus, repo = env["bus"], env["repo"]
    base, commit = git_repo(repo)
    h1 = commit("one\n")
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review("codex", str(repo), base, h1)["thread"]["id"]
    run_job(bus, tid)
    with pytest.raises(BusError) as e:
        claude.request_review("codex", str(repo), base, h1, prev_head_commit_id=h1)
    assert e.value.code == "CHANGES_REQUESTED"
    res = claude.request_review("codex", str(repo), base, h1, prev_head_commit_id=h1, focus="it is intended")
    assert res["thread"]["id"] == tid and res["thread"]["follow_ups"] == 1
    assert bus.jobs(tid)[-1]["params"]["mode"] == "pushback"
    run_job(bus, tid)
    assert "has not changed the code" in calls(env)[-1]["prompt"]
    assert bus.thread(tid)["status"] == "resolved"
    # A retry of that request (no newer note) is idempotent; a second pushback is refused.
    assert claude.request_review("codex", str(repo), base, h1, prev_head_commit_id=h1,
                                 focus="it is intended")["job"]["id"] == bus.jobs(tid)[-1]["id"]
    claude.send("more evidence", thread=tid)
    with pytest.raises(BusError) as e:
        claude.request_review("codex", str(repo), base, h1, prev_head_commit_id=h1)
    assert "already reconsidered" in str(e.value)

    h2 = commit("two\n")
    claude.request_review("codex", str(repo), base, h2, prev_head_commit_id=h1)
    run_job(bus, tid)  # the second follow-up still asks for changes: past the limit of 2
    t = bus.thread(tid)
    assert t["status"] == "needs_human" and t["refs"]["budget_parked"]
    h3 = commit("three\n")
    with pytest.raises(BusError) as e:
        claude.request_review("codex", str(repo), base, h3, prev_head_commit_id=h2)
    assert e.value.code == "REVIEW_LIMIT_REACHED"


@pytest.mark.parametrize("verdict, problem", [
    ({"verdict": "request_changes", "findings": [{"severity": "low"}]}, "needs at least one high or medium"),
    ({"verdict": "request_changes", "findings": []}, "needs at least one high or medium"),
    ({"verdict": "approve", "findings": [{"severity": "medium"}]}, "blocks approval"),
    ({"verdict": "approve", "findings": [{"severity": "low"}]}, None),
    ({"verdict": "request_changes", "findings": [{"severity": "high"}, {"severity": "low"}]}, None),
    ({"verdict": "approve", "findings": [{"severity": "?"}]}, "needs a severity"),
])
def test_verdict_must_match_its_severities(verdict, problem):
    found = verdict_problem(verdict)
    assert (found is None) if problem is None else (problem in found)


def test_inconsistent_verdict_gets_one_repair_turn(env, no_spawn, monkeypatch):
    bus = env["bus"]
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review("codex", str(env["repo"]), "a" * 40, "b" * 40)["thread"]["id"]
    monkeypatch.setattr(Worker, "review_diff", lambda self, refs, interdiff: ("+x", "Diff:"))
    low = '{"verdict": "request_changes", "summary": "s", "findings": [{"file": "f", "severity": "low", "comment": "nit"}]}'
    fixed = '{"verdict": "approve", "summary": "s", "findings": [{"file": "f", "severity": "low", "comment": "nit"}]}'
    asks = []

    def turn(self, agent, prompt, resume=True, fresh_prompt=None):
        asks.append(prompt)
        return TurnResult(ok=True, text=low if len(asks) == 1 else fixed, external_id="s1")
    monkeypatch.setattr(Worker, "turn", turn)
    run_job(bus, tid)
    assert len(asks) == 2 and "inconsistent" in asks[1]
    assert "Severity, and what blocks approval" in asks[0]
    v = bus.last_verdict(tid, "codex")
    assert v["data"]["verdict"] == "approve" and "[low, non-blocking]" in v["body"]

    # Still inconsistent after the repair: the human decides.
    tid2 = claude.request_review("antigravity", str(env["repo"]), "a" * 40, "b" * 40)["thread"]["id"]
    monkeypatch.setattr(Worker, "turn", lambda self, *a, **k: TurnResult(ok=True, text=low, external_id="s2"))
    run_job(bus, tid2)
    assert bus.thread(tid2)["status"] == "needs_human" and bus.last_verdict(tid2, "antigravity") is None
    assert "inconsistent verdict" in bus.history(tid2)["messages"][-1]["body"]


def test_review_progress_shows_passes_not_max_rounds(env, no_spawn, capsys):
    from agent_bus import cli
    bus, repo = env["bus"], env["repo"]
    base, commit = git_repo(repo)
    h1 = commit("one\n")
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review(None, str(repo), base, h1, reviewers=BOTH)["thread"]["id"]
    run_job(bus, tid)
    h2 = commit("two\n")
    claude.request_review(None, str(repo), base, h2, prev_head_commit_id=h1, reviewers=BOTH)
    assert cli.main(["history", tid]) == 0
    out = capsys.readouterr().out
    assert "review pass 2, follow-ups 1/3:" in out and "r2/" not in out
    assert cli.main(["threads"]) == 0
    assert "pass2,fu1/3" in capsys.readouterr().out


def test_hard_stop_holds_even_if_the_worker_died_before_parking(env, no_spawn):
    set_config(env, review_followup_budget=1)
    bus, repo = env["bus"], env["repo"]
    base, commit = git_repo(repo)
    h1 = commit("one\n")
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review(None, str(repo), base, h1, reviewers=BOTH)["thread"]["id"]
    run_job(bus, tid)
    prev = h1
    for text in ("two\n", "three\n"):
        h = commit(text)
        claude.request_review(None, str(repo), base, h, prev_head_commit_id=prev, reviewers=BOTH)
        run_job(bus, tid)
        prev = h
    # As if the worker died after the verdict, before it parked the thread.
    refs = {k: v for k, v in bus.thread(tid)["refs"].items() if k != "budget_parked"}
    bus.set_thread(tid, status="open", refs=json.dumps(refs))
    h4 = commit("four\n")
    with pytest.raises(BusError) as e:
        claude.request_review(None, str(repo), base, h4, prev_head_commit_id=prev, reviewers=BOTH)
    assert e.value.code == "REVIEW_LIMIT_REACHED"
    # The human's resume lifts it, with no reviewer rerun on the same head.
    res = Service(bus, HUMAN).resume(tid)
    assert not res.get("job") and res["thread"]["followup_limit"] == 3
    assert claude.request_review(None, str(repo), base, h4, prev_head_commit_id=prev, reviewers=BOTH)["job"]


def test_single_reviewer_resume_lifts_an_unparked_over_limit_review(env, no_spawn):
    set_config(env, review_followup_budget=1)
    bus, repo = env["bus"], env["repo"]
    base, commit = git_repo(repo)
    prev = commit("one\n")
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review("codex", str(repo), base, prev)["thread"]["id"]
    run_job(bus, tid)
    for text in ("two\n", "three\n"):
        h = commit(text)
        claude.request_review("codex", str(repo), base, h, prev_head_commit_id=prev)
        run_job(bus, tid)
        prev = h
    refs = {k: v for k, v in bus.thread(tid)["refs"].items() if k != "budget_parked"}
    bus.set_thread(tid, status="open", refs=json.dumps(refs))  # the worker died before parking
    h4 = commit("four\n")
    with pytest.raises(BusError):
        claude.request_review("codex", str(repo), base, h4, prev_head_commit_id=prev)
    Service(bus, HUMAN).resume(tid)
    assert claude.request_review("codex", str(repo), base, h4, prev_head_commit_id=prev)["job"]


def test_single_reviewer_failed_pushback_keeps_the_request_for_changes(env, no_spawn, monkeypatch):
    bus, repo = env["bus"], env["repo"]
    base, commit = git_repo(repo)
    h1 = commit("one\n")
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review("codex", str(repo), base, h1)["thread"]["id"]
    run_job(bus, tid)
    claude.request_review("codex", str(repo), base, h1, prev_head_commit_id=h1, focus="evidence")
    assert bus.review_standings(tid)["reviewers"]["codex"]["state"] == "reviewing"
    monkeypatch.setenv("FAKE_EXHAUSTED", "codex")
    run_job(bus, tid)
    assert bus.thread(tid)["status"] == "needs_human"
    assert bus.review_standings(tid)["changes_requested"] == ["codex"]


def test_single_reviewer_note_after_approval_is_not_a_pushback(env, no_spawn, monkeypatch):
    bus, repo = env["bus"], env["repo"]
    base, commit = git_repo(repo)
    h1 = commit("one\n")
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review("codex", str(repo), base, h1)["thread"]["id"]
    run_job(bus, tid)
    first = claude.request_review("codex", str(repo), base, h1, prev_head_commit_id=h1, focus="evidence")
    monkeypatch.setenv("FAKE_APPROVE", "codex")
    run_job(bus, tid)  # the pushback convinced it
    claude.send("thanks", thread=tid)
    again = claude.request_review("codex", str(repo), base, h1, prev_head_commit_id=h1)
    assert again["job"]["id"] == first["job"]["id"] and again["thread"]["follow_ups"] == 1


def test_pushback_at_the_limit_that_hits_quota_is_retried_not_lifted(env, no_spawn, monkeypatch):
    set_config(env, review_followup_budget=1)
    bus, repo = env["bus"], env["repo"]
    base, commit = git_repo(repo)
    h1 = commit("one\n")
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review("codex", str(repo), base, h1)["thread"]["id"]
    run_job(bus, tid)
    h2 = commit("two\n")
    claude.request_review("codex", str(repo), base, h2, prev_head_commit_id=h1)
    run_job(bus, tid)
    claude.request_review("codex", str(repo), base, h2, prev_head_commit_id=h2, focus="evidence")
    assert bus.thread(tid)["refs"]["follow_ups"] == 2  # at the limit
    monkeypatch.setenv("FAKE_EXHAUSTED", "codex")
    run_job(bus, tid)
    assert bus.thread(tid)["status"] == "needs_human"
    with pytest.raises(BusError) as e:  # #87: wait for (or resume) that pushback, not "allow more"
        claude.request_review("codex", str(repo), base, commit("three\n"), prev_head_commit_id=h2)
    assert e.value.code == "REVIEW_LIMIT_REACHED" and "still due its verdict" in str(e.value)
    monkeypatch.delenv("FAKE_EXHAUSTED")
    res = Service(bus, HUMAN).resume(tid)
    assert res.get("job") and res["thread"]["followup_limit"] == 2  # the pushback reruns as it was


def test_unfinished_pushback_at_the_limit_still_blocks_a_new_head(env, no_spawn, monkeypatch):
    set_config(env, review_followup_budget=1)
    bus, repo = env["bus"], env["repo"]
    base, commit = git_repo(repo)
    h1 = commit("one\n")
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review(None, str(repo), base, h1, reviewers=BOTH)["thread"]["id"]
    run_job(bus, tid)
    h2 = commit("two\n")
    claude.request_review(None, str(repo), base, h2, prev_head_commit_id=h1, reviewers=BOTH)
    run_job(bus, tid)  # antigravity blocks
    claude.request_review(None, str(repo), base, h2, prev_head_commit_id=h2, reviewers=BOTH, focus="evidence")
    monkeypatch.setenv("FAKE_EXHAUSTED", "antigravity")
    run_job(bus, tid)  # the pushback hits a usage limit, at the follow-up limit
    with pytest.raises(BusError) as e:
        claude.request_review(None, str(repo), base, commit("three\n"), prev_head_commit_id=h2, reviewers=BOTH)
    assert e.value.code == "REVIEW_LIMIT_REACHED"
    # #87: a resume reruns that pushback at the same limit, so the error says to wait for it.
    assert "pushback pass is still due its verdict" in str(e.value)
    assert "allow more follow-ups" not in str(e.value)


def status_bodies(bus, tid):
    return [m["body"] for m in bus.history(tid)["messages"] if m["kind"] == "status"]


def test_two_reviewer_follow_ups_count_through_new_heads_and_a_pushback(env, no_spawn, monkeypatch):
    """#89: the shape of a real two-reviewer review (several new heads, a handoff after an
    approval, a pushback), with the default budget of 3 and limit of 6."""
    bus, repo = env["bus"], env["repo"]
    base, commit = git_repo(repo)
    claude = Service(bus, "claude", bus.register_session("claude"))
    heads = [commit("one\n")]
    res = claude.request_review(None, str(repo), base, heads[0], reviewers=BOTH, change_id="PR-1")
    tid = res["thread"]["id"]
    assert (res["thread"]["follow_ups"], res["thread"]["followup_budget"], res["thread"]["followup_limit"]) == (0, 3, 6)
    run_job(bus, tid)  # codex asks for changes
    assert status_bodies(bus, tid)[0].startswith(f"Review requested from codex at head {heads[0][:12]}")

    def new_head(text, approve=""):
        monkeypatch.setenv("FAKE_APPROVE", approve)
        heads.append(commit(text))
        out = claude.request_review(None, str(repo), base, heads[-1], prev_head_commit_id=heads[-2],
                                    reviewers=BOTH, thread=tid, change_id="PR-1")
        run_job(bus, tid)
        return out

    # A new head goes to antigravity, which has never reviewed: still the author's follow-up.
    assert new_head("two\n")["thread"]["follow_ups"] == 1
    assert f"Review follow-up requested from antigravity at head {heads[-1][:12]}." in status_bodies(bus, tid)
    assert new_head("three\n")["thread"]["follow_ups"] == 2
    assert not thread_summary(bus.thread(tid))["budget_warning"]
    assert "run long" not in claude.inbox(thread=tid)["messages"][-1]["body"]
    # antigravity approves and hands the head to codex: the handoff is free.
    assert new_head("four\n", approve="antigravity")["thread"]["follow_ups"] == 3
    t = bus.thread(tid)
    assert t["round"] == 5 and thread_summary(t)["follow_ups"] == 3
    assert thread_summary(t)["budget_warning"] and t["status"] == "open"
    notice = claude.inbox(thread=tid)["messages"][-1]["body"]
    assert notice.startswith("codex requested changes") and "run long: 3 follow-up requests" in notice

    # A pushback counts too.
    claude.send("PUSHBACK: intended", thread=tid)
    res = claude.request_review(None, str(repo), base, heads[-1], prev_head_commit_id=heads[-1],
                                reviewers=BOTH, thread=tid)
    assert res["thread"]["follow_ups"] == 4 and bus.jobs(tid)[-1]["params"]["mode"] == "pushback"
    run_job(bus, tid)
    assert f"Review pushback requested from codex at head {heads[-1][:12]}." in status_bodies(bus, tid)
    assert bus.review_standings(tid)["approved"] == ["antigravity"]

    assert new_head("five\n")["thread"]["follow_ups"] == 5
    assert bus.thread(tid)["status"] == "open"
    new_head("six\n")  # the sixth follow-up still asks for changes: the human decides
    t = bus.thread(tid)
    assert thread_summary(t)["follow_ups"] == 6 and t["status"] == "needs_human" and t["refs"]["budget_parked"]
    assert thread_summary(t)["waiting_for_human"]
    assert "still asks for changes after 6 follow-up requests" in claude.inbox(thread=tid)["messages"][-1]["body"]
    with pytest.raises(BusError) as e:
        claude.request_review(None, str(repo), base, commit("seven\n"), prev_head_commit_id=heads[-1],
                              reviewers=BOTH, thread=tid)
    assert e.value.code == "REVIEW_LIMIT_REACHED" and "after 6 follow-up requests" in str(e.value)
    assert "to allow more follow-ups" in str(e.value)


def test_single_reviewer_follow_ups_count_to_the_default_limit(env, no_spawn):
    """#89, the single-reviewer path: new heads and a pushback each count, with the default
    budget of 3 (a warning) and limit of 6 (the human)."""
    bus, repo = env["bus"], env["repo"]
    base, commit = git_repo(repo)
    claude = Service(bus, "claude", bus.register_session("claude"))
    prev = commit("one\n")
    tid = claude.request_review("codex", str(repo), base, prev)["thread"]["id"]
    run_job(bus, tid)
    for n in range(1, 7):
        if n == 4:  # the fourth is a pushback on the same head
            res = claude.request_review("codex", str(repo), base, prev, prev_head_commit_id=prev, focus="intended")
            assert bus.jobs(tid)[-1]["params"]["mode"] == "pushback"
        else:
            h = commit(f"{n}\n")
            res = claude.request_review("codex", str(repo), base, h, prev_head_commit_id=prev)
            prev = h
        assert res["thread"]["id"] == tid and res["thread"]["follow_ups"] == n
        run_job(bus, tid)
        t = bus.thread(tid)
        assert thread_summary(t)["budget_warning"] == (n >= 3)
        assert t["status"] == ("needs_human" if n == 6 else "resolved")
    assert f"Review follow-up requested from codex at head {prev[:12]}." in status_bodies(bus, tid)
    assert "still asks for changes after 6 follow-up requests" in claude.inbox(thread=tid)["messages"][-1]["body"]
    with pytest.raises(BusError) as e:
        claude.request_review("codex", str(repo), base, commit("seven\n"), prev_head_commit_id=prev)
    assert e.value.code == "REVIEW_LIMIT_REACHED" and "to allow more follow-ups" in str(e.value)
