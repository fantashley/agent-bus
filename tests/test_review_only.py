"""#145: finishing a review without every approval, review-only reviews, debriefing a cancelled
thread, pre-existing findings; and #146's leftovers."""
import json

import pytest

from agent_bus import cli, evidence as ev, mcp_server
from agent_bus.db import HUMAN, BusError
from agent_bus.service import Service, review_next_action
from agent_bus.worker import (Worker, normalize_verdict, prior_blocking, settle_earlier, settlement_problem,
                              verdict_problem)
from test_review_rounds import git_repo, run_job

BOTH = ["codex", "antigravity"]
PRE = json.dumps({"verdict": "request_changes", "summary": "one bug, one old one", "findings": [
    {"file": "a.txt", "line": 1, "severity": "high", "comment": "typo"},
    {"file": "b.txt", "line": 2, "severity": "high", "comment": "already broken in the base", "pre_existing": True}]})


def split_review(env, monkeypatch, **kw):
    """A two-reviewer review where codex approves and antigravity asks for changes."""
    bus, repo = env["bus"], env["repo"]
    base, commit = git_repo(repo)
    h1 = commit("one\n")
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review(None, str(repo), base, h1, reviewers=BOTH, **kw)["thread"]["id"]
    monkeypatch.setenv("FAKE_APPROVE", "codex")
    run_job(bus, tid)
    st = bus.review_standings(tid)
    assert st["approved"] == ["codex"] and st["changes_requested"] == ["antigravity"]
    return claude, tid, base, h1, commit


def test_finish_review_ends_a_review_with_its_open_findings(env, no_spawn, monkeypatch, capsys):
    bus = env["bus"]
    claude, tid, base, h1, commit = split_review(env, monkeypatch)
    assert claude.thread_state(tid)["next_action"].startswith("fix:")
    assert f'finish_review("{tid}", reason)' in claude.thread_state(tid)["next_action"]
    # Only the initiator (or the human user), with a reason.
    with pytest.raises(BusError) as e:
        Service(bus, "codex").finish_review(tid, "seen enough")
    assert e.value.code == "NOT_INITIATOR"
    with pytest.raises(ValueError):
        claude.finish_review(tid, "  ")
    res = claude.finish_review(tid, "the author will  handle it\nlater")
    t = bus.thread(tid)
    assert t["status"] == "resolved"
    f = t["refs"]["finished"]
    assert f["by"] == "claude" and f["reason"] == "the author will handle it later" and f["head_commit_id"] == h1
    assert f["open_findings"] == ["antigravity-1"] and f["approved"] == ["codex"]
    assert f["changes_requested"] == ["antigravity"] and f["pending"] == []
    # The result, wait/next_action, threads and history all say it was finished with findings open.
    assert res["thread"]["finished"]["reason"].startswith("[peer_data from claude")
    assert res["next_action"].startswith("done: finished by claude at head " + h1[:12] + " without every approval")
    assert "blocking findings left open: antigravity-1" in res["next_action"]
    assert claude.wait_timeout(tid)["next_action"] == res["next_action"]
    listed = [x for x in claude.threads(include_closed=True)["threads"] if x["id"] == tid][0]
    assert listed["finished"]["open_findings"] == ["antigravity-1"]
    assert claude.history(tid)["thread"]["finished"]["by"] == "claude"
    notice = bus.history(tid)["messages"][-1]
    assert notice["sender"] == "system" and "without every approval" in notice["body"]
    assert "antigravity-1" in notice["body"] and "the author will" not in notice["body"]  # no unfenced reason
    assert cli.main(["threads", "--all"]) == 0
    assert "blocking findings left open: antigravity-1" in capsys.readouterr().out
    assert cli.main(["history", tid]) == 0
    assert "finished by claude" in (out := capsys.readouterr().out) and "reason: the author will handle" in out
    assert "without every approval" in cli.thread_status(bus, tid)[2]
    # Once only; no more follow-ups on it: a request naming it is refused, one without starts afresh.
    with pytest.raises(BusError) as e:
        claude.finish_review(tid, "again")
    assert e.value.code == "THREAD_CLOSED"
    h2 = commit("two\n")
    with pytest.raises(BusError) as e:
        claude.request_review(None, str(env["repo"]), base, h2, prev_head_commit_id=h1, reviewers=BOTH, thread=tid)
    assert e.value.code == "THREAD_CLOSED"
    new = claude.request_review(None, str(env["repo"]), base, h2, prev_head_commit_id=h1, reviewers=BOTH)
    assert new["thread"]["id"] != tid
    # And its peers can be debriefed.
    d = claude.debrief(tid)
    assert d["peers"] == BOTH


def test_finish_review_waits_for_a_running_pass_and_the_human_can_finish(env, no_spawn, monkeypatch, capsys):
    bus, repo = env["bus"], env["repo"]
    base, commit = git_repo(repo)
    h1 = commit("one\n")
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review("codex", str(repo), base, h1)["thread"]["id"]
    with pytest.raises(BusError) as e:
        claude.finish_review(tid, "done")
    assert e.value.code == "THREAD_BUSY"
    run_job(bus, tid)  # changes requested
    assert cli.main(["finish", tid, "--reason", "not needed any more"]) == 0
    assert "blocking findings left open: codex-1" in capsys.readouterr().out
    f = bus.thread(tid)["refs"]["finished"]
    assert f["by"] == HUMAN and f["reason"] == "not needed any more"
    assert any(a["command"] for a in bus.human_actions(tid))
    # A finished single-reviewer review isn't continued either: a follow-up starts a new one.
    h2 = commit("two\n")
    assert claude.request_review("codex", str(repo), base, h2, prev_head_commit_id=h1)["thread"]["id"] != tid


def test_finishing_a_review_whose_approvals_are_all_in_resolves_it_as_approved(env, no_spawn, monkeypatch):
    bus, repo = env["bus"], env["repo"]
    base, commit = git_repo(repo)
    h1 = commit("one\n")
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review("codex", str(repo), base, h1)["thread"]["id"]
    monkeypatch.setenv("FAKE_APPROVE", "codex")
    monkeypatch.setattr(Worker, "finish_thread", lambda self, status: (_ for _ in ()).throw(RuntimeError("died")))
    with pytest.raises(RuntimeError):
        run_job(bus, tid)
    bus.conn.execute("UPDATE jobs SET status='failed' WHERE id=?", (bus.jobs(tid)[-1]["id"],))
    res = claude.finish_review(tid, "all in")
    assert bus.thread(tid)["status"] == "resolved" and "finished" not in bus.thread(tid)["refs"]
    assert res["next_action"] == f"done: codex approved head {h1}"


def test_finish_review_is_an_mcp_tool(env):
    server = mcp_server.build("claude", bus=env["bus"])
    import asyncio
    tools = {t.name: t for t in asyncio.run(server.list_tools())}
    assert set(tools["finish_review"].input_schema["properties"]) == {"thread", "reason"}
    assert "review_only" in tools["request_review"].input_schema["properties"]


# ------------------------------------------------------------------ review-only


def test_review_only_waits_for_the_authors_next_head(env, no_spawn, monkeypatch):
    bus = env["bus"]
    claude, tid, base, h1, commit = split_review(env, monkeypatch, review_only=True)
    assert claude.thread_state(tid)["thread"]["review_only"] is True
    nxt = claude.thread_state(tid)["next_action"]
    assert nxt.startswith("await_head: antigravity asked for changes on head " + h1)
    assert f"prev_head_commit_id={h1}" in nxt and f'finish_review("{tid}", reason)' in nxt and "fix:" not in nxt
    notice = [m for m in bus.history(tid)["messages"] if m["sender"] == "system"][-1]
    assert "review-only" in notice["body"] and "finish_review" in notice["body"]
    assert "Address the findings" not in notice["body"]
    # The flag can't change on a follow-up; leaving it out keeps it.
    h2 = commit("two\n")
    with pytest.raises(ValueError) as e:
        claude.request_review(None, str(env["repo"]), base, h2, prev_head_commit_id=h1, reviewers=BOTH,
                              review_only=False)
    assert "review_only can't change" in str(e.value)
    claude.send("The author says: fixed the typo", thread=tid)
    res = claude.request_review(None, str(env["repo"]), base, h2, prev_head_commit_id=h1, reviewers=BOTH)
    assert res["thread"]["id"] == tid and bus.thread(tid)["refs"]["review_only"] is True
    monkeypatch.setenv("FAKE_APPROVE", "codex,antigravity")
    run_job(bus, tid)
    assert bus.thread(tid)["status"] == "resolved"
    assert claude.thread_state(tid)["next_action"] == f"done: every required reviewer approved head {h2}"


def test_single_reviewer_review_only_keeps_its_flag(env, no_spawn):
    bus, repo = env["bus"], env["repo"]
    base, commit = git_repo(repo)
    h1 = commit("one\n")
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review("codex", str(repo), base, h1, review_only=True)["thread"]["id"]
    run_job(bus, tid)
    assert claude.thread_state(tid)["next_action"].startswith("await_head: codex asked for changes")
    # A repeat of the first request that leaves the flag out is still the same review.
    assert claude.request_review("codex", str(repo), base, h1)["thread"]["id"] == tid
    h2 = commit("two\n")
    with pytest.raises(ValueError):
        claude.request_review("codex", str(repo), base, h2, prev_head_commit_id=h1, review_only=False)
    claude.request_review("codex", str(repo), base, h2, prev_head_commit_id=h1)
    assert bus.thread(tid)["refs"]["review_only"] is True and bus.thread(tid)["refs"]["head_commit_id"] == h2
    with pytest.raises(ValueError):
        claude.request_review("codex", str(repo), base, h1, review_only="yes")
    # A fresh request of the other kind is another review, not a refusal.
    assert claude.request_review("codex", str(repo), base, h1, review_only=False)["thread"]["id"] != tid


def test_next_action_without_a_thread_id_still_renders():
    st = {"head_commit_id": "h", "reviewers": {"codex": {"state": "changes_requested"}}, "complete": False,
          "changes_requested": ["codex"], "pending": [], "follow_ups": 0, "followup_limit": 6}
    assert review_next_action({"status": "open", "refs": {"review_only": True}}, st, None).startswith("await_head:")
    stop = review_next_action({"status": "open", "refs": {"review_only": True}}, {**st, "follow_ups": 6}, None)
    assert stop.startswith("stop:") and "finish_review" in stop


# ------------------------------------------------------------------ debriefs


def test_a_cancelled_review_can_be_debriefed_by_the_peers_that_took_a_turn(env, no_spawn, monkeypatch):
    bus = env["bus"]
    claude, tid, *_ = split_review(env, monkeypatch)
    Service(bus, HUMAN).cancel(tid)
    res = claude.debrief(tid)
    assert res["peers"] == BOTH
    run_job(bus, tid)
    d = [m for m in bus.history(tid)["messages"] if m["kind"] == "debrief"]
    assert sorted(m["sender"] for m in d) == sorted(BOTH) and bus.thread(tid)["status"] == "cancelled"
    assert bus.jobs(tid)[-1]["status"] == "done"


def test_a_cancelled_thread_without_finished_turns_says_why(env, no_spawn):
    bus, repo = env["bus"], env["repo"]
    base, commit = git_repo(repo)
    h1 = commit("one\n")
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review(None, str(repo), base, h1, reviewers=BOTH)["thread"]["id"]
    run_job(bus, tid)  # codex asks for changes; antigravity never ran
    Service(bus, HUMAN).cancel(tid)
    with pytest.raises(BusError) as e:
        claude.debrief(tid, peers=["antigravity"])
    assert e.value.code == "NO_SESSION"
    res = claude.debrief(tid)
    assert res["peers"] == ["codex"] and "antigravity" in res["skipped_peers"]
    h2 = commit("two\n")
    other = claude.request_review("codex", str(repo), base, h2)["thread"]["id"]
    Service(bus, HUMAN).cancel(other)
    with pytest.raises(BusError) as e:
        claude.debrief(other)
    assert e.value.code == "THREAD_NOT_FINISHED" and "no peer finished a turn" in str(e.value)


def test_thread_not_finished_says_how_a_discussion_finishes(env, no_spawn):
    bus = env["bus"]
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.start_discussion("SQLite or JSON?", peers=["codex"])["thread"]["id"]
    with pytest.raises(BusError) as e:
        claude.debrief(tid)
    assert e.value.code == "THREAD_NOT_FINISHED" and "agrees on a proposal" in str(e.value)


# ------------------------------------------------------------------ pre-existing findings


def test_pre_existing_findings_never_block():
    v = normalize_verdict({"verdict": "approve", "summary": "ok", "findings": [
        {"file": "b", "line": 1, "severity": "high", "comment": "old", "pre_existing": True},
        {"file": "c", "line": 2, "severity": "low", "comment": "x", "pre_existing": "yes"}]})
    assert v["findings"][0]["pre_existing"] is True and "pre_existing" not in v["findings"][1]
    assert verdict_problem(v) is None
    v["verdict"] = "request_changes"
    assert "pre_existing" in verdict_problem(v)
    mine = {"data": {"verdict": "request_changes", "findings": [
        {"id": "codex-1", "severity": "high", "file": "a"},
        {"id": "codex-2", "severity": "high", "file": "b", "pre_existing": True}]}}
    assert list(prior_blocking(mine)) == ["codex-1"]
    # Re-listed as pre-existing, an earlier blocker can't be called still blocking.
    prior = prior_blocking(mine)
    again = settle_earlier(normalize_verdict({"verdict": "request_changes", "findings": [
        {"id": "codex-1", "file": "a", "severity": "high", "comment": "old after all", "pre_existing": True},
        {"file": "d", "severity": "high", "comment": "new"}],
        "earlier": [{"id": "codex-1", "status": "still_blocking"}]}), prior)
    assert "pre_existing" in settlement_problem(again, prior)


def test_pre_existing_findings_are_listed_apart_and_left_out_of_finish(env, no_spawn, monkeypatch):
    bus, repo = env["bus"], env["repo"]
    base, commit = git_repo(repo)
    h1 = commit("one\n")
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review("codex", str(repo), base, h1)["thread"]["id"]
    monkeypatch.setenv("FAKE_REPLY_CODEX", PRE)
    run_job(bus, tid)
    v = [m for m in bus.history(tid)["messages"] if m["kind"] == "review_verdict"][-1]
    assert [f["id"] for f in v["data"]["findings"]] == ["codex-1", "codex-2"]
    assert v["data"]["findings"][1]["pre_existing"] is True
    body = v["body"]
    assert "[high, blocking] codex-1" in body
    assert body.index("Pre-existing") < body.index("[high, pre-existing] codex-2")
    assert claude.finish_review(tid, "enough")["thread"]["finished"]["open_findings"] == ["codex-1"]
    first = [c for c in json.loads("[" + ",".join(env["log"].read_text().splitlines()) + "]")
             if c["agent"] == "codex"][0]
    assert '"pre_existing": true' in first["prompt"]


# ------------------------------------------------------------------ #146


def test_a_pushback_after_a_pass_that_failed_after_its_verdict_runs(env, no_spawn):
    """codex-7 (#146): a base-only follow-up asks for changes, then its worker fails; the
    author's same-head pushback with new evidence is a pushback, not that failed job."""
    bus, repo = env["bus"], env["repo"]
    base, commit = git_repo(repo)
    base2 = commit("middle\n")
    h1 = commit("one\n")
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review("codex", str(repo), base, h1)["thread"]["id"]
    run_job(bus, tid)
    claude.request_review("codex", str(repo), base2, h1, prev_head_commit_id=h1)  # base only
    failed = run_job(bus, tid)
    bus.conn.execute("UPDATE jobs SET status='failed', error='worker died' WHERE id=?", (failed["id"],))
    n = len(bus.jobs(tid))
    claude.send("codex-1 is not a bug: see the spec", thread=tid)
    res = claude.request_review("codex", str(repo), base2, h1, prev_head_commit_id=h1)
    assert not res["job"].get("deduplicated") and len(bus.jobs(tid)) == n + 1
    assert bus.jobs(tid)[-1]["params"]["mode"] == "pushback"


def test_evidence_unchanged_note_agrees_in_number():
    item = {"title": "a", "text": "b"}
    one = {"kind": "discussion", "initiator": "claude", "refs": {"evidence": [item]}}
    two = {"kind": "discussion", "initiator": "claude", "refs": {"evidence": [item, item]}}
    assert "the 1 item" in ev.unchanged(one) and "is the one earlier" in ev.unchanged(one)
    assert "the 2 items" in ev.unchanged(two) and "are the ones earlier" in ev.unchanged(two)


# ------------------------------------------------------------------ review round 1 (codex)


def test_a_finished_review_stays_finished_through_cancel_and_resume(env, no_spawn, monkeypatch):
    bus = env["bus"]
    claude, tid, *_ = split_review(env, monkeypatch)
    claude.finish_review(tid, "enough")
    claude.debrief(tid)
    human = Service(bus, HUMAN)
    human.cancel(tid)  # stops the debrief, not the finished state
    assert bus.thread(tid)["status"] == "resolved" and bus.jobs(tid)[-1]["status"] == "cancelled"
    with pytest.raises(BusError) as e:
        human.resume(tid)
    assert e.value.code == "THREAD_CLOSED"
    assert claude.thread_state(tid)["next_action"].startswith("done: finished by claude")


def test_finish_keeps_blockers_left_unsettled_at_an_earlier_head(env, no_spawn, monkeypatch):
    bus = env["bus"]
    claude, tid, base, h1, commit = split_review(env, monkeypatch)
    h2 = commit("two\n")
    monkeypatch.setenv("FAKE_APPROVE", "")
    claude.send("fixed antigravity-1", thread=tid)
    claude.request_review(None, str(env["repo"]), base, h2, prev_head_commit_id=h1, reviewers=BOTH)
    run_job(bus, tid)  # codex asks for changes at h2; antigravity never reaches it
    st = bus.review_standings(tid)
    assert st["changes_requested"] == ["codex"] and st["reviewers"]["antigravity"]["state"] == "stale"
    f = claude.finish_review(tid, "enough")["thread"]["finished"]
    assert f["open_findings"] == ["codex-1"]
    assert f["earlier_open_findings"] == {"antigravity-1": {"head_commit_id": h1, "base_commit_id": base}}
    assert (f"unsettled from an earlier revision: antigravity-1 (head {h1[:12]} on base {base[:12]})"
            in claude.thread_state(tid)["next_action"])


def test_a_human_finish_is_recorded_on_either_path_and_flagged_when_unsure(env, no_spawn, monkeypatch):
    bus, repo = env["bus"], env["repo"]
    base, commit = git_repo(repo)
    h1 = commit("one\n")
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review("codex", str(repo), base, h1)["thread"]["id"]
    monkeypatch.setenv("FAKE_APPROVE", "codex")
    monkeypatch.setattr(Worker, "finish_thread", lambda self, status: (_ for _ in ()).throw(RuntimeError("died")))
    with pytest.raises(RuntimeError):
        run_job(bus, tid)
    bus.conn.execute("UPDATE jobs SET status='failed' WHERE id=?", (bus.jobs(tid)[-1]["id"],))
    assert cli.main(["finish", tid, "--reason", "all in"]) == 0
    assert bus.thread(tid)["status"] == "resolved"
    assert [a["detail"] for a in bus.human_actions(tid)] == [{"finish": "all in"}]
    # A human finish with no provenance recorded is flagged to agents, like a human message.
    h2 = commit("two\n")
    other = claude.request_review("codex", str(repo), base, h2)["thread"]["id"]
    monkeypatch.undo()
    run_job(bus, other)
    bus.human_provenance = None
    f = Service(bus, HUMAN).finish_review(other, "unsure who")["thread"]["finished"]
    assert "may not be from the human user" in f["reason_provenance"]


def test_an_earlier_pre_existing_finding_keeps_its_id():
    v = normalize_verdict({"verdict": "approve", "findings": [
        {"id": "codex-2", "file": "b", "severity": "high", "comment": "still old", "pre_existing": True},
        {"id": "codex-9", "file": "c", "severity": "low", "comment": "unknown id"}]})
    v = settle_earlier(v, {}, {"codex-2"})
    assert [f["id"] for f in v["findings"]] == ["codex-2", None]


def test_repeating_a_failed_pushback_request_with_its_own_focus_is_a_retry(env, no_spawn):
    """codex-5: the focus that pinned the failed pass isn't new evidence for a pushback."""
    bus, repo = env["bus"], env["repo"]
    base, commit = git_repo(repo)
    base2 = commit("middle\n")
    h1 = commit("one\n")
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review("codex", str(repo), base, h1)["thread"]["id"]
    run_job(bus, tid)
    claude.request_review("codex", str(repo), base2, h1, prev_head_commit_id=h1, focus="rebased only")
    failed = run_job(bus, tid)
    bus.conn.execute("UPDATE jobs SET status='failed', error='worker died' WHERE id=?", (failed["id"],))
    again = claude.request_review("codex", str(repo), base2, h1, prev_head_commit_id=h1, focus="rebased only")
    assert again["job"]["id"] == failed["id"] and again["job"].get("deduplicated")
    pushed = claude.request_review("codex", str(repo), base2, h1, prev_head_commit_id=h1,
                                   focus="codex-1 is intended: see the spec")
    assert bus.jobs(tid)[-1]["params"]["mode"] == "pushback" and not pushed["job"].get("deduplicated")


def test_a_retry_of_a_failed_first_review_only_pass_keeps_its_mode(env, no_spawn):
    bus, repo = env["bus"], env["repo"]
    base, commit = git_repo(repo)
    h1 = commit("one\n")
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review("codex", str(repo), base, h1, review_only=True)["thread"]["id"]
    bus.conn.execute("UPDATE jobs SET status='failed', error='worker died' WHERE id=?", (bus.jobs(tid)[-1]["id"],))
    retry = claude.request_review("codex", str(repo), base, h1)["thread"]["id"]
    assert bus.thread(retry)["refs"]["review_only"] is True


def test_a_peer_note_is_not_a_finished_turn(env, no_spawn):
    bus, repo = env["bus"], env["repo"]
    base, commit = git_repo(repo)
    h1 = commit("one\n")
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review("codex", str(repo), base, h1)["thread"]["id"]
    Service(bus, "codex", bus.register_session("codex")).send("a note, not a turn", thread=tid)
    Service(bus, HUMAN).cancel(tid)
    with pytest.raises(BusError) as e:
        claude.debrief(tid)
    assert e.value.code == "THREAD_NOT_FINISHED"


# ------------------------------------------------------------------ review round 2 (antigravity)


def test_follow_ups_pick_the_review_of_their_mode(env, no_spawn):
    """antigravity-1: reviews of both modes for one diff; a follow-up naming its mode continues
    that one, without REVIEW_AMBIGUOUS or a refusal."""
    bus, repo = env["bus"], env["repo"]
    base, commit = git_repo(repo)
    h1 = commit("one\n")
    claude = Service(bus, "claude", bus.register_session("claude"))
    t1 = claude.request_review("codex", str(repo), base, h1, review_only=True)["thread"]["id"]
    run_job(bus, t1)
    t2 = claude.request_review("codex", str(repo), base, h1, review_only=False)["thread"]["id"]
    run_job(bus, t2)
    h2 = commit("two\n")
    assert claude.request_review("codex", str(repo), base, h2, prev_head_commit_id=h1,
                                 review_only=True)["thread"]["id"] == t1
    m1 = claude.request_review(None, str(repo), base, h1, reviewers=BOTH, review_only=True)["thread"]["id"]
    run_job(bus, m1)
    m2 = claude.request_review(None, str(repo), base, h1, reviewers=BOTH, review_only=False)["thread"]["id"]
    run_job(bus, m2)
    assert claude.request_review(None, str(repo), base, h2, prev_head_commit_id=h1, reviewers=BOTH,
                                 review_only=False)["thread"]["id"] == m2


def test_a_retry_without_the_flag_keeps_to_the_newest_reviews_mode(env, no_spawn):
    """antigravity-2: an omitted flag takes the newest matching review's mode, so an older
    review of the other mode is neither returned nor copied."""
    bus, repo = env["bus"], env["repo"]
    base, commit = git_repo(repo)
    h1 = commit("one\n")
    claude = Service(bus, "claude", bus.register_session("claude"))
    t1 = claude.request_review("codex", str(repo), base, h1, review_only=True)["thread"]["id"]
    run_job(bus, t1)  # done
    t2 = claude.request_review("codex", str(repo), base, h1, review_only=False)["thread"]["id"]
    bus.conn.execute("UPDATE jobs SET status='failed', error='worker died' WHERE id=?", (bus.jobs(t2)[-1]["id"],))
    retry = claude.request_review("codex", str(repo), base, h1)["thread"]["id"]
    assert retry not in (t1, t2) and not bus.thread(retry)["refs"].get("review_only")


# ------------------------------------------------------------------ review round 3 (codex)


def test_mode_preference_only_among_threads_that_can_take_the_request(env, no_spawn):
    """codex-10: an unrelated thread of the named mode doesn't hide the only lineage, and with
    both modes at the previous head an omitted flag is ambiguous."""
    bus, repo = env["bus"], env["repo"]
    base, commit = git_repo(repo)
    h1 = commit("one\n")
    claude = Service(bus, "claude", bus.register_session("claude"))
    t1 = claude.request_review("codex", str(repo), base, h1, review_only=True)["thread"]["id"]
    run_job(bus, t1)
    hx = commit("unrelated\n")
    run_job(bus, claude.request_review("codex", str(repo), base, hx)["thread"]["id"])
    h2 = commit("two\n")
    with pytest.raises(ValueError) as e:  # the only lineage is review-only: a mode flip is refused
        claude.request_review("codex", str(repo), base, h2, prev_head_commit_id=h1, review_only=False)
    assert "review_only can't change" in str(e.value)
    t2 = claude.request_review("codex", str(repo), base, h1, review_only=False)["thread"]["id"]
    run_job(bus, t2)
    with pytest.raises(BusError) as e:
        claude.request_review("codex", str(repo), base, h2, prev_head_commit_id=h1)
    assert e.value.code == "REVIEW_AMBIGUOUS" and "review_only" in str(e.value)
    # Both modes advance to the same next head: an exact repeat of the other mode doesn't hide
    # the lineage of the named one.
    assert claude.request_review("codex", str(repo), base, h2, prev_head_commit_id=h1,
                                 review_only=False)["thread"]["id"] == t2
    run_job(bus, t2)
    assert claude.request_review("codex", str(repo), base, h2, prev_head_commit_id=h1,
                                 review_only=True)["thread"]["id"] == t1


def test_history_prints_a_finish_reason_without_terminal_controls(env, no_spawn, monkeypatch, capsys):
    claude, tid, *_ = split_review(env, monkeypatch)
    claude.finish_review(tid, "done \x1b[2Jcleared")
    assert cli.main(["history", tid]) == 0
    out = capsys.readouterr().out
    assert "\x1b[2J" not in out and "cleared" in out
