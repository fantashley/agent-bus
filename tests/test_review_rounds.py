"""#47: several required reviewers, one per round, all approving the same head."""
import json
import os
import shutil
import subprocess

import pytest

from agent_bus.db import HUMAN, BusError
from agent_bus.service import Service
from agent_bus.worker import Worker

BOTH = ["codex", "antigravity"]


def calls(env):
    return [json.loads(l) for l in env["log"].read_text().splitlines()] if env["log"].exists() else []


def run_job(bus, tid):
    job = bus.jobs(tid)[-1]
    Worker(bus, job["id"]).run()
    return bus.job(job["id"])


def git_repo(repo):
    if not shutil.which("git"):
        pytest.skip("git not installed")
    genv = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
                GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")
    sh = lambda *a: subprocess.run(a, cwd=repo, check=True, capture_output=True, text=True, env=genv).stdout.strip()  # noqa: E731
    sh("git", "init", "-q")
    (repo / "a.txt").write_text("start\n")
    sh("git", "add", "a.txt")
    sh("git", "commit", "-qm", "base")

    def commit(text):
        (repo / "a.txt").write_text(text)
        sh("git", "commit", "-qam", text.strip())
        return sh("git", "rev-parse", "HEAD")
    return sh("git", "rev-parse", "HEAD"), commit


def test_two_required_reviewers_acceptance(env, no_spawn, monkeypatch):
    """The issue's acceptance case: the first reviewer finds an issue, the author fixes it,
    and the next reviewer sees the updated diff and the earlier finding.  The change is
    ready once both approvals are valid for the same head."""
    bus, repo = env["bus"], env["repo"]
    base, commit = git_repo(repo)
    h1 = commit("helo world\n")
    claude = Service(bus, "claude", bus.register_session("claude"))
    res = claude.request_review(None, str(repo), base, h1, change_id="PR #47", reviewers=BOTH)
    tid = res["thread"]["id"]
    assert res["review"]["pending"] == BOTH and bus.thread(tid)["refs"]["reviewer"] == "codex"

    # Round 1: codex only, and it asks for changes.  Nobody else runs.
    run_job(bus, tid)
    assert [c["agent"] for c in calls(env)] == ["codex"]
    st = bus.review_standings(tid)
    assert st["changes_requested"] == ["codex"] and st["pending"] == ["antigravity"] and not st["complete"]
    assert bus.thread(tid)["status"] == "open"
    notice = claude.inbox(thread=tid)["messages"][-1]
    assert notice["from"] == "system" and "prev_head_commit_id=" + h1 in notice["body"]
    assert "goes to antigravity" in notice["body"]

    # The author fixes it, says how, and asks for the next round on the new head.
    h2 = commit("hello world\n")
    claude.send("FIX-NOTE: fixed the typo codex found", thread=tid)
    res = claude.request_review(None, str(repo), base, h2, prev_head_commit_id=h1, reviewers=BOTH)
    assert res["thread"]["id"] == tid and res["review"]["reviewers"]["antigravity"]["state"] == "reviewing"
    assert res["review"]["reviewers"]["codex"]["state"] == "stale"
    monkeypatch.setenv("FAKE_APPROVE", "codex,antigravity")
    run_job(bus, tid)

    agy, codex2 = calls(env)[1:]
    assert agy["agent"] == "antigravity" and codex2["agent"] == "codex"  # one at a time, in rotation
    # antigravity: the updated full diff, codex's finding (with its head) and the author's note.
    assert "Diff from base to head" in agy["prompt"] and "+hello world" in agy["prompt"]
    assert "helo" not in agy["prompt"].split("```diff")[1]
    assert "typo" in agy["prompt"] and f"on head {h1[:12]}" in agy["prompt"] and "FIX-NOTE" in agy["prompt"]
    assert "Required reviewers: codex, antigravity" in agy["prompt"]
    # codex resumes its own conversation with the interdiff since the head it last saw,
    # plus antigravity's verdict and the note.
    assert codex2["argv"][:3] == ["exec", "resume", "--json"]
    assert "Interdiff" in codex2["prompt"] and "-helo world" in codex2["prompt"]
    assert "FIX-NOTE" in codex2["prompt"] and "from antigravity" in codex2["prompt"]

    st = bus.review_standings(tid)
    assert st["complete"] and st["approved"] == BOTH and st["head_commit_id"] == h2
    assert bus.thread(tid)["status"] == "resolved"
    verdicts = [m for m in bus.history(tid)["messages"] if m["kind"] == "review_verdict"]
    assert [(m["sender"], m["data"]["verdict"], m["data"]["head_commit_id"]) for m in verdicts] == [
        ("codex", "request_changes", h1), ("antigravity", "approve", h2), ("codex", "approve", h2)]
    assert "Review complete" in claude.inbox(thread=tid)["messages"][-1]["body"]


def test_new_head_makes_approvals_stale(env, no_spawn, monkeypatch):
    bus, repo = env["bus"], env["repo"]
    base, commit = git_repo(repo)
    h1 = commit("one\n")
    monkeypatch.setenv("FAKE_APPROVE", "codex,antigravity")
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review(None, str(repo), base, h1, reviewers=BOTH)["thread"]["id"]
    run_job(bus, tid)
    assert bus.review_standings(tid)["complete"] and bus.thread(tid)["status"] == "resolved"
    # Asking again for the same head has nothing left to run.
    again = claude.request_review(None, str(repo), base, h1, prev_head_commit_id=h1, reviewers=BOTH)
    assert again["job"] is None and again["review"]["complete"]

    h2 = commit("two\n")
    res = claude.request_review(None, str(repo), base, h2, prev_head_commit_id=h1, reviewers=BOTH)
    st = res["review"]
    assert not st["complete"] and st["approved"] == [] and st["pending"] == BOTH
    assert st["reviewers"]["codex"] == {"state": "reviewing", "verdict": "approve", "head_commit_id": h1,
                                        "base_commit_id": base}
    assert st["reviewers"]["antigravity"]["state"] == "stale"
    assert bus.thread(tid)["status"] == "open"
    run_job(bus, tid)
    st = bus.review_standings(tid)
    assert st["complete"] and st["head_commit_id"] == h2
    assert [c["agent"] for c in calls(env)] == ["codex", "antigravity", "codex", "antigravity"]


def test_followups_are_refused_while_busy_or_stale(env, no_spawn):
    bus = env["bus"]
    sha, h1, h2, h3 = "a" * 40, "b" * 40, "c" * 40, "d" * 40
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review(None, str(env["repo"]), sha, h1, reviewers=BOTH)["thread"]["id"]
    with pytest.raises(BusError) as e:
        claude.request_review(None, str(env["repo"]), sha, h2, prev_head_commit_id=h1, reviewers=BOTH)
    assert e.value.code == "REVIEW_BUSY"
    assert bus.thread(tid)["refs"]["head_commit_id"] == h1  # the refusal changed nothing
    bus.conn.execute("UPDATE jobs SET status='done'")
    claude.request_review(None, str(env["repo"]), sha, h2, prev_head_commit_id=h1, reviewers=BOTH)
    bus.conn.execute("UPDATE jobs SET status='done'")
    with pytest.raises(BusError) as e:
        claude.request_review(None, str(env["repo"]), sha, h3, prev_head_commit_id=h1, reviewers=BOTH)
    assert e.value.code == "REVIEW_STALE"
    # The same reviewers in another order still name the same review.
    res = claude.request_review(None, str(env["repo"]), sha, h3, prev_head_commit_id=h2,
                                reviewers=["antigravity", "codex"])
    assert res["thread"]["id"] == tid and bus.thread(tid)["refs"]["reviewers"] == BOTH


@pytest.mark.parametrize("kwargs,match", [
    ({"reviewer": "codex", "reviewers": BOTH}, "not both"),
    ({"reviewers": ["codex", "codex"]}, "twice"),
    ({"reviewers": ["codex", "claude"]}, "other agents"),
    ({"reviewers": []}, "non-empty"),
    ({"reviewers": BOTH, "effort": "high"}, "peer_settings"),
    ({"reviewers": BOTH, "peer_settings": {"claude": {"effort": "high"}}}, "isn't one of the reviewers"),
    ({"reviewer": "codex", "peer_settings": {"codex": {"effort": "high"}}}, "goes with `reviewers`"),
])
def test_reviewers_are_validated(env, no_spawn, kwargs, match):
    bus = env["bus"]
    claude = Service(bus, "claude", bus.register_session("claude"))
    kwargs = {"reviewer": None, **kwargs}
    with pytest.raises(ValueError, match=match):
        claude.request_review(repo=str(env["repo"]), base_commit_id="a" * 40, head_commit_id="b" * 40, **kwargs)


def test_one_listed_reviewer_is_a_plain_review_and_lineages_stay_apart(env, no_spawn):
    bus = env["bus"]
    sha, h1, h2 = "a" * 40, "b" * 40, "c" * 40
    claude = Service(bus, "claude", bus.register_session("claude"))
    one = claude.request_review(None, str(env["repo"]), sha, h1, reviewers=["codex"],
                                peer_settings={"codex": {"effort": "high"}})["thread"]
    refs = bus.thread(one["id"])["refs"]
    assert "reviewers" not in refs and refs["reviewer"] == "codex"
    assert refs["peer_settings"] == {"codex": {"effort": "high"}}
    bus.conn.execute("UPDATE jobs SET status='done'")
    both = claude.request_review(None, str(env["repo"]), sha, h1, reviewers=BOTH)["thread"]
    bus.conn.execute("UPDATE jobs SET status='done'")
    # A single-reviewer follow-up continues the single thread, never the multi-reviewer one.
    assert claude.request_review("codex", str(env["repo"]), sha, h2, prev_head_commit_id=h1)["thread"]["id"] \
        == one["id"]
    assert claude.request_review(None, str(env["repo"]), sha, h2, prev_head_commit_id=h1,
                                 reviewers=BOTH)["thread"]["id"] == both["id"]


def test_out_of_usage_second_reviewer_parks_and_resume_continues(env, no_spawn, monkeypatch):
    bus, repo = env["bus"], env["repo"]
    base, commit = git_repo(repo)
    h1 = commit("one\n")
    monkeypatch.setenv("FAKE_APPROVE", "codex,antigravity")
    monkeypatch.setenv("FAKE_EXHAUSTED", "antigravity")
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review(None, str(repo), base, h1, reviewers=BOTH)["thread"]["id"]
    run_job(bus, tid)
    t = bus.thread(tid)
    assert t["status"] == "needs_human" and t["refs"]["reviewer"] == "antigravity"
    st = bus.review_standings(tid)
    assert st["approved"] == ["codex"] and st["pending"] == ["antigravity"]
    monkeypatch.delenv("FAKE_EXHAUSTED")
    res = Service(bus, HUMAN).resume(tid)
    assert res.get("job")
    run_job(bus, tid)
    assert bus.review_standings(tid)["complete"] and bus.thread(tid)["status"] == "resolved"
    assert [c["agent"] for c in calls(env)].count("codex") == 1  # its approval still counts


def test_cli_shows_who_approved_and_who_is_pending(env, no_spawn, capsys):
    bus = env["bus"]
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review(None, str(env["repo"]), "a" * 40, "b" * 40, reviewers=BOTH)["thread"]["id"]
    bus.conn.execute("UPDATE jobs SET status='done'")
    bus.post(tid, "codex", "Verdict: approve", kind="review_verdict",
             data={"verdict": "approve", "head_commit_id": "b" * 40, "revision": 0})
    from agent_bus.cli import main
    assert main(["history", tid]) == 0
    out = capsys.readouterr().out
    assert "- codex: approved" in out and "- antigravity: pending" in out
    assert main(["threads"]) == 0
    assert "codex approved; antigravity pending" in capsys.readouterr().out
    assert main(["history", tid, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["review"]["approved"] == ["codex"]


def test_mcp_tool_takes_reviewers(env, no_spawn):
    import asyncio
    from agent_bus import mcp_server
    server = mcp_server.build("claude", bus=env["bus"])
    res = asyncio.run(server.call_tool("request_review", {
        "repo": str(env["repo"]), "base_commit_id": "a" * 40, "head_commit_id": "b" * 40, "reviewers": BOTH}))
    text = json.dumps(res, default=str)
    assert "BAD_REQUEST" not in text and "codex reviews first" in text
    t = env["bus"].threads()[0]
    assert t["refs"]["reviewers"] == BOTH and t["participants"] == ["claude", *BOTH]


def test_new_base_for_the_same_head_needs_new_approvals(env, no_spawn, monkeypatch):
    bus, repo = env["bus"], env["repo"]
    b1, commit = git_repo(repo)
    b2 = commit("middle\n")
    h = commit("end\n")
    monkeypatch.setenv("FAKE_APPROVE", "codex,antigravity")
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review(None, str(repo), b2, h, reviewers=BOTH)["thread"]["id"]
    run_job(bus, tid)
    assert bus.review_standings(tid)["complete"]
    res = claude.request_review(None, str(repo), b1, h, prev_head_commit_id=h, reviewers=BOTH)
    assert res["job"] and not res["review"]["complete"] and res["review"]["pending"] == BOTH
    assert bus.thread(tid)["refs"]["prev_head_commit_id"] is None  # full diff, not an empty interdiff
    run_job(bus, tid)
    later = calls(env)[2:]
    assert [c["agent"] for c in later] == ["codex", "antigravity"]
    assert all("Diff from base to head" in c["prompt"] and "-start" in c["prompt"] for c in later)
    st = bus.review_standings(tid)
    assert st["complete"] and all(r["base_commit_id"] == b1 for r in st["reviewers"].values())


def test_followup_job_never_borrows_another_threads_job(env, no_spawn):
    bus = env["bus"]
    p, h = "a" * 40, "b" * 40
    claude = Service(bus, "claude", bus.register_session("claude"))
    a = claude.request_review(None, str(env["repo"]), "c" * 40, p, reviewers=BOTH)["thread"]["id"]
    bus.conn.execute("UPDATE jobs SET status='done'")
    b = claude.request_review(None, str(env["repo"]), p, h, reviewers=BOTH)  # fresh review of p..h
    assert b["thread"]["id"] != a
    bus.conn.execute("UPDATE jobs SET status='done'")
    res = claude.request_review(None, str(env["repo"]), "c" * 40, h, prev_head_commit_id=p, reviewers=BOTH)
    assert res["thread"]["id"] == a and res["job"]["thread"] == a and not res["job"].get("deduplicated")


def test_same_head_after_changes_requested_is_refused(env, no_spawn):
    bus, repo = env["bus"], env["repo"]
    base, commit = git_repo(repo)
    h1 = commit("one\n")
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review(None, str(repo), base, h1, reviewers=BOTH)["thread"]["id"]
    run_job(bus, tid)
    with pytest.raises(BusError) as e:
        claude.request_review(None, str(repo), base, h1, prev_head_commit_id=h1, reviewers=BOTH)
    assert e.value.code == "CHANGES_REQUESTED"
    assert bus.thread(tid)["refs"]["reviewer"] == "codex" and len(bus.jobs(tid)) == 1


def test_resume_finishes_a_review_whose_worker_died_after_the_last_approval(env, no_spawn):
    bus = env["bus"]
    base, head = "a" * 40, "b" * 40
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review(None, str(env["repo"]), base, head, reviewers=BOTH)["thread"]["id"]
    for who in BOTH:
        bus.post(tid, who, "Verdict: approve", kind="review_verdict",
                 data={"verdict": "approve", "head_commit_id": head, "base_commit_id": base, "revision": 0})
    bus.claim_job(bus.jobs(tid)[0]["id"], "dead", lease_s=-1)  # died before resolving
    assert bus.review_standings(tid)["complete"] and bus.thread(tid)["status"] == "open"
    # Resolving runs no reviewer, so it doesn't need them to be available any more.
    conf = env["tmp"] / "config" / "agent-bus" / "config.json"
    conf.write_text(json.dumps({**json.loads(conf.read_text()), "agents": ["claude"]}))
    assert Service(bus, HUMAN).resume(tid).get("job")
    run_job(bus, tid)
    assert bus.thread(tid)["status"] == "resolved"
    assert "Review complete" in claude.inbox(thread=tid)["messages"][-1]["body"]
    assert calls(env) == []  # nobody reviewed again


def test_missing_new_head_for_a_first_time_reviewer_points_to_resume(env, no_spawn, monkeypatch):
    bus, repo = env["bus"], env["repo"]
    base, commit = git_repo(repo)
    h1 = commit("one\n")
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review(None, str(repo), base, h1, reviewers=BOTH)["thread"]["id"]
    run_job(bus, tid)  # codex asks for changes
    missing = "f" * 40
    claude.request_review(None, str(repo), base, missing, prev_head_commit_id=h1, reviewers=BOTH)
    assert bus.thread(tid)["refs"]["reviewer"] == "antigravity"
    run_job(bus, tid)
    body = [m for m in bus.history(tid)["messages"] if "Review not started" in m["body"]][-1]["body"]
    assert f"agent-bus resume {tid}" in body and "without prev_head_commit_id" in body


def test_returning_to_an_earlier_base_needs_fresh_approvals(env, no_spawn, monkeypatch):
    """A..H approved by both, B..H rejected, back to A..H: the old approvals stay stale."""
    bus, repo = env["bus"], env["repo"]
    a, commit = git_repo(repo)
    b = commit("middle\n")
    h = commit("end\n")
    monkeypatch.setenv("FAKE_APPROVE", "codex,antigravity")
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review(None, str(repo), a, h, reviewers=BOTH)["thread"]["id"]
    run_job(bus, tid)
    monkeypatch.setenv("FAKE_APPROVE", "")
    claude.request_review(None, str(repo), b, h, prev_head_commit_id=h, reviewers=BOTH)
    run_job(bus, tid)
    assert bus.review_standings(tid)["changes_requested"] == ["codex"]
    # Repeating the request for B is idempotent, not a new round.
    again = claude.request_review(None, str(repo), b, h, prev_head_commit_id=h, reviewers=BOTH)
    assert again["job"]["deduplicated"]
    monkeypatch.setenv("FAKE_APPROVE", "codex,antigravity")
    res = claude.request_review(None, str(repo), a, h, prev_head_commit_id=h, reviewers=BOTH)
    assert res["job"] and not res["job"].get("deduplicated")
    assert res["review"]["approved"] == [] and res["review"]["pending"] == BOTH
    run_job(bus, tid)
    assert [c["agent"] for c in calls(env)] == ["codex", "antigravity", "codex", "antigravity", "codex"]
    st = bus.review_standings(tid)
    assert st["complete"] and bus.thread(tid)["refs"]["base_commit_id"] == a


def test_repeating_a_failed_followup_reports_that_failure(env, no_spawn, monkeypatch):
    bus, repo = env["bus"], env["repo"]
    base, commit = git_repo(repo)
    h1 = commit("one\n")
    monkeypatch.setenv("FAKE_APPROVE", "codex,antigravity")
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review(None, str(repo), base, h1, reviewers=BOTH)["thread"]["id"]
    run_job(bus, tid)
    missing = "f" * 40
    claude.request_review(None, str(repo), base, missing, prev_head_commit_id=h1, reviewers=BOTH)
    assert run_job(bus, tid)["status"] == "failed"
    with pytest.raises(BusError) as e:
        claude.request_review(None, str(repo), base, missing, prev_head_commit_id=h1, reviewers=BOTH)
    assert e.value.code == "REVIEW_FAILED" and f"agent-bus resume {tid}" in e.value.message


def test_fresh_request_is_not_deduplicated_onto_a_thread_that_moved(env, no_spawn, monkeypatch):
    """A..H complete, then the same thread moves to B..H: asking for A..H afresh starts a
    new review instead of returning the old job with B..H's standings."""
    bus, repo = env["bus"], env["repo"]
    a, commit = git_repo(repo)
    b = commit("middle\n")
    h = commit("end\n")
    monkeypatch.setenv("FAKE_APPROVE", "codex,antigravity")
    claude = Service(bus, "claude", bus.register_session("claude"))
    first = claude.request_review(None, str(repo), a, h, reviewers=BOTH)
    tid = first["thread"]["id"]
    run_job(bus, tid)
    # While still pinned to A..H, the identical request is idempotent.
    same = claude.request_review(None, str(repo), a, h, reviewers=BOTH)
    assert same["thread"]["id"] == tid and same["job"]["deduplicated"]
    claude.request_review(None, str(repo), b, h, prev_head_commit_id=h, reviewers=BOTH)
    run_job(bus, tid)
    res = claude.request_review(None, str(repo), a, h, reviewers=BOTH)
    assert res["thread"]["id"] != tid and not res["job"].get("deduplicated")
    assert res["review"]["pending"] == BOTH and bus.thread(res["thread"]["id"])["refs"]["base_commit_id"] == a


def test_repeating_a_first_request_follows_its_resumed_or_failed_pass(env, no_spawn):
    bus = env["bus"]
    base, head = "a" * 40, "b" * 40
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review(None, str(env["repo"]), base, head, reviewers=BOTH)["thread"]["id"]
    bus.conn.execute("UPDATE jobs SET status='failed'")
    # Failed: asking again retries on the same thread, not a second one.
    retry = claude.request_review(None, str(env["repo"]), base, head, reviewers=BOTH)
    assert retry["thread"]["id"] == tid and not retry["job"].get("deduplicated")
    bus.conn.execute("UPDATE jobs SET status='failed'")
    bus.transition(tid, "needs_human")
    resumed = Service(bus, HUMAN).resume(tid)["job"]
    # A pass the human resumed is the one this request refers to.
    again = claude.request_review(None, str(env["repo"]), base, head, reviewers=BOTH)
    assert again["thread"]["id"] == tid and again["job"]["id"] == resumed["id"] and again["job"]["deduplicated"]
    assert len([t for t in bus.threads(include_closed=True) if t["kind"] == "review"]) == 1


def test_ambiguous_followup_needs_a_thread(env, no_spawn):
    bus = env["bus"]
    a, b, h, h2 = "a" * 40, "c" * 40, "b" * 40, "d" * 40
    claude = Service(bus, "claude", bus.register_session("claude"))
    one = claude.request_review(None, str(env["repo"]), a, h, reviewers=BOTH, change_id="PR #1")["thread"]["id"]
    bus.conn.execute("UPDATE jobs SET status='done'")
    two = claude.request_review(None, str(env["repo"]), b, h, reviewers=BOTH, change_id="PR #2")["thread"]["id"]
    bus.conn.execute("UPDATE jobs SET status='done'")
    # Both have been at H: no guessing, not even by base.
    with pytest.raises(BusError) as e:
        claude.request_review(None, str(env["repo"]), b, h2, prev_head_commit_id=h, reviewers=BOTH)
    assert e.value.code == "REVIEW_AMBIGUOUS" and one in e.value.message and two in e.value.message
    # The label settles it, even against the other thread's base (it is never relabelled).
    res = claude.request_review(None, str(env["repo"]), b, h2, prev_head_commit_id=h, reviewers=BOTH,
                                change_id="PR #1")
    assert res["thread"]["id"] == one and bus.thread(two)["refs"]["change_id"] == "PR #2"
    bus.conn.execute("UPDATE jobs SET status='done'")
    # So does `thread`.
    res = claude.request_review(None, str(env["repo"]), a, h2, prev_head_commit_id=h, reviewers=BOTH, thread=two)
    assert res["thread"]["id"] == two
    with pytest.raises(ValueError, match="needs prev_head_commit_id"):
        claude.request_review(None, str(env["repo"]), a, h2, reviewers=BOTH, thread=two)
    with pytest.raises(ValueError, match="goes with `reviewers`"):
        claude.request_review("codex", str(env["repo"]), a, h2, prev_head_commit_id=h, thread=two)


def test_exact_repeat_is_not_taken_for_another_labelled_review(env, no_spawn):
    """PR #1 went H -> H2 on base B; PR #2 is still at H.  Asking for B..H2 for PR #2 is
    new work on PR #2, not a repeat of PR #1's request."""
    bus = env["bus"]
    a, b, h, h2 = "a" * 40, "c" * 40, "b" * 40, "d" * 40
    claude = Service(bus, "claude", bus.register_session("claude"))
    one = claude.request_review(None, str(env["repo"]), a, h, reviewers=BOTH, change_id="PR #1")["thread"]["id"]
    bus.conn.execute("UPDATE jobs SET status='done'")
    claude.request_review(None, str(env["repo"]), b, h2, prev_head_commit_id=h, reviewers=BOTH, change_id="PR #1")
    bus.conn.execute("UPDATE jobs SET status='done'")
    two = claude.request_review(None, str(env["repo"]), a, h, reviewers=BOTH, change_id="PR #2")["thread"]["id"]
    bus.conn.execute("UPDATE jobs SET status='done'")
    res = claude.request_review(None, str(env["repo"]), b, h2, prev_head_commit_id=h, reviewers=BOTH,
                                change_id="PR #2")
    assert res["thread"]["id"] == two and not res["job"].get("deduplicated")
    assert bus.thread(one)["refs"]["change_id"] == "PR #1"


def test_exact_repeat_finds_its_thread_after_another_reached_that_head(env, no_spawn):
    """T1: A..H then B..H2.  T2: A..H afresh.  Repeating T1's B..H2 request is idempotent on
    T1, never a new round on T2."""
    bus = env["bus"]
    a, b, h, h2 = "a" * 40, "c" * 40, "b" * 40, "d" * 40
    claude = Service(bus, "claude", bus.register_session("claude"))
    t1 = claude.request_review(None, str(env["repo"]), a, h, reviewers=BOTH)["thread"]["id"]
    bus.conn.execute("UPDATE jobs SET status='done'")
    claude.request_review(None, str(env["repo"]), b, h2, prev_head_commit_id=h, reviewers=BOTH)
    bus.conn.execute("UPDATE jobs SET status='done'")
    t2 = claude.request_review(None, str(env["repo"]), a, h, reviewers=BOTH)["thread"]["id"]
    bus.conn.execute("UPDATE jobs SET status='done'")
    jobs_t2 = len(bus.jobs(t2))
    res = claude.request_review(None, str(env["repo"]), b, h2, prev_head_commit_id=h, reviewers=BOTH)
    assert res["thread"]["id"] == t1 and res["job"]["deduplicated"] and len(bus.jobs(t2)) == jobs_t2
    # A new request whose base fits T1 but whose previous head is only T2's current one conflicts.
    with pytest.raises(BusError) as e:
        claude.request_review(None, str(env["repo"]), b, "e" * 40, prev_head_commit_id=h, reviewers=BOTH)
    assert e.value.code == "REVIEW_AMBIGUOUS"


def test_followup_keeps_a_new_change_label(env, no_spawn):
    bus = env["bus"]
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review(None, str(env["repo"]), "a" * 40, "b" * 40, reviewers=BOTH)["thread"]["id"]
    bus.conn.execute("UPDATE jobs SET status='done'")
    claude.request_review(None, str(env["repo"]), "a" * 40, "c" * 40, prev_head_commit_id="b" * 40,
                          reviewers=BOTH, change_id="PR #12")
    assert bus.thread(tid)["refs"]["change_id"] == "PR #12"


def test_labelled_request_prefers_its_label_over_an_unlabelled_exact_match(env, no_spawn):
    bus = env["bus"]
    a, b, h, h2 = "a" * 40, "c" * 40, "b" * 40, "d" * 40
    claude = Service(bus, "claude", bus.register_session("claude"))
    one = claude.request_review(None, str(env["repo"]), a, h, reviewers=BOTH)["thread"]["id"]  # unlabelled
    bus.conn.execute("UPDATE jobs SET status='done'")
    claude.request_review(None, str(env["repo"]), b, h2, prev_head_commit_id=h, reviewers=BOTH, thread=one)
    bus.conn.execute("UPDATE jobs SET status='done'")
    two = claude.request_review(None, str(env["repo"]), a, h, reviewers=BOTH, change_id="PR #2")["thread"]["id"]
    assert two != one  # a fresh request only reuses a review with the same label
    bus.conn.execute("UPDATE jobs SET status='done'")
    res = claude.request_review(None, str(env["repo"]), b, h2, prev_head_commit_id=h, reviewers=BOTH,
                                change_id="PR #2")
    assert res["thread"]["id"] == two and not res["job"].get("deduplicated")
    assert bus.thread(one)["refs"]["change_id"] is None


def test_fresh_request_with_another_label_never_reuses_a_pinned_review(env, no_spawn):
    bus = env["bus"]
    a, h = "a" * 40, "b" * 40
    claude = Service(bus, "claude", bus.register_session("claude"))
    one = claude.request_review(None, str(env["repo"]), a, h, reviewers=BOTH, change_id="PR #1")["thread"]["id"]
    bus.conn.execute("UPDATE jobs SET status='done'")
    # Still pinned to A..H, so only the label tells these requests apart.
    assert claude.request_review(None, str(env["repo"]), a, h, reviewers=BOTH,
                                 change_id="PR #1")["thread"]["id"] == one
    res = claude.request_review(None, str(env["repo"]), a, h, reviewers=BOTH, change_id="PR #2")
    assert res["thread"]["id"] != one and not res["job"].get("deduplicated")
    unlabelled = claude.request_review(None, str(env["repo"]), a, h, reviewers=BOTH)
    assert unlabelled["thread"]["id"] not in (one, res["thread"]["id"])


def test_initial_focus_reaches_every_reviewer(env, no_spawn, monkeypatch):
    bus, repo = env["bus"], env["repo"]
    base, commit = git_repo(repo)
    h1 = commit("one\n")
    monkeypatch.setenv("FAKE_APPROVE", "codex,antigravity")
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review(None, str(repo), base, h1, reviewers=BOTH, focus="FOCUS-NOTE")["thread"]["id"]
    run_job(bus, tid)
    assert all("FOCUS-NOTE" in c["prompt"] for c in calls(env)) and len(calls(env)) == 2


def test_missing_base_on_a_base_only_followup_points_to_resume(env, no_spawn, monkeypatch):
    bus, repo = env["bus"], env["repo"]
    base, commit = git_repo(repo)
    h = commit("one\n")
    monkeypatch.setenv("FAKE_APPROVE", "codex,antigravity")
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review(None, str(repo), base, h, reviewers=BOTH)["thread"]["id"]
    run_job(bus, tid)
    claude.request_review(None, str(repo), "f" * 40, h, prev_head_commit_id=h, reviewers=BOTH)
    assert bus.thread(tid)["refs"]["heads"] == [h]
    run_job(bus, tid)
    body = [m for m in bus.history(tid)["messages"] if "Review not started" in m["body"]][-1]["body"]
    assert f"agent-bus resume {tid}" in body


def test_cli_status_prefers_terminal_states(env, no_spawn):
    from agent_bus.cli import thread_status
    bus = env["bus"]
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review(None, str(env["repo"]), "a" * 40, "b" * 40, reviewers=BOTH)["thread"]["id"]
    bus.conn.execute("UPDATE jobs SET status='done'")
    bus.post(tid, "codex", "Verdict: request_changes", kind="review_verdict",
             data={"verdict": "request_changes", "head_commit_id": "b" * 40, "revision": 0})
    assert "changes requested by codex" in thread_status(bus, tid)[2]
    bus.transition(tid, "needs_human")
    assert thread_status(bus, tid)[:2] == ("blocked", "question")
    Service(bus, HUMAN).cancel(tid)
    assert thread_status(bus, tid)[2].endswith("cancelled")


def test_approved_same_head_request_keeps_the_pin_and_resolves(env, no_spawn):
    bus = env["bus"]
    base, head = "a" * 40, "b" * 40
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review(None, str(env["repo"]), base, head, reviewers=BOTH)["thread"]["id"]
    for who in BOTH:
        bus.post(tid, who, "Verdict: approve", kind="review_verdict",
                 data={"verdict": "approve", "head_commit_id": head, "base_commit_id": base, "revision": 0})
    bus.conn.execute("UPDATE jobs SET status='done'")  # the worker died before resolving
    before = bus.thread(tid)["refs"]
    res = claude.request_review(None, str(env["repo"]), base, head, prev_head_commit_id=head, reviewers=BOTH)
    assert res["job"] is None and res["thread"]["status"] == "resolved"
    assert bus.thread(tid)["refs"] == before
    # The original request is still a repeat of what's pinned.
    again = claude.request_review(None, str(env["repo"]), base, head, reviewers=BOTH)
    assert again["thread"]["id"] == tid and again["job"]["deduplicated"]


def test_single_listed_reviewer_settings_are_checked(env, no_spawn):
    bus = env["bus"]
    claude = Service(bus, "claude", bus.register_session("claude"))
    with pytest.raises(ValueError):
        claude.request_review(None, str(env["repo"]), "a" * 40, "b" * 40, reviewers=["codex"],
                              peer_settings={"codex": {"bogus": 1}})
    with pytest.raises(ValueError, match="several reviewers"):
        claude.request_review(None, str(env["repo"]), "a" * 40, "b" * 40, prev_head_commit_id="a" * 40,
                              reviewers=["codex"], thread="t-x")


def test_wait_timeout_reaps_a_dead_reviewer(env, no_spawn):
    bus = env["bus"]
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review(None, str(env["repo"]), "a" * 40, "b" * 40, reviewers=BOTH)["thread"]["id"]
    assert claude.thread_state(tid)["review"]["reviewers"]["codex"]["state"] == "reviewing"
    bus.claim_job(bus.jobs(tid)[0]["id"], "dead", lease_s=-1)
    assert claude.thread_state(tid)["review"]["reviewers"]["codex"]["state"] == "pending"


def test_opening_focus_spends_no_message(env, no_spawn, monkeypatch):
    bus, repo = env["bus"], env["repo"]
    base, commit = git_repo(repo)
    h1 = commit("one\n")
    monkeypatch.setenv("FAKE_APPROVE", "codex,antigravity")
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review(None, str(repo), base, h1, reviewers=BOTH, focus="FOCUS-NOTE")["thread"]["id"]
    bus.set_thread(tid, max_messages=2)  # exactly the two verdicts
    run_job(bus, tid)
    assert bus.review_standings(tid)["complete"]
    assert all(m["body"] != "FOCUS-NOTE" for m in bus.history(tid)["messages"])
    assert all("FOCUS-NOTE" in c["prompt"] for c in calls(env))


def test_exact_base_only_repeat_resolves_a_fully_approved_thread(env, no_spawn):
    bus = env["bus"]
    b1, b2, h = "a" * 40, "c" * 40, "b" * 40
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review(None, str(env["repo"]), b1, h, reviewers=BOTH)["thread"]["id"]
    bus.conn.execute("UPDATE jobs SET status='done'")
    claude.request_review(None, str(env["repo"]), b2, h, prev_head_commit_id=h, reviewers=BOTH)
    for who in BOTH:
        bus.post(tid, who, "Verdict: approve", kind="review_verdict",
                 data={"verdict": "approve", "head_commit_id": h, "base_commit_id": b2, "revision": 1})
    bus.claim_job(bus.jobs(tid)[-1]["id"], "dead", lease_s=-1)  # died before resolving
    res = claude.request_review(None, str(env["repo"]), b2, h, prev_head_commit_id=h, reviewers=BOTH)
    assert res["job"] is None and res["thread"]["status"] == "resolved" and res["review"]["complete"]


@pytest.mark.parametrize("bad", [False, [], "high"])
def test_one_listed_reviewer_rejects_malformed_settings(env, no_spawn, bad):
    bus = env["bus"]
    claude = Service(bus, "claude", bus.register_session("claude"))
    with pytest.raises(ValueError):
        claude.request_review(None, str(env["repo"]), "a" * 40, "b" * 40, reviewers=["codex"],
                              peer_settings={"codex": bad})
