"""#101: follow-up framing by the last verdict, stable finding ids, caveats on agree votes,
long answers in a fenced body block, and the smaller reviewer-friction fixes."""
import json
import os
import subprocess
import time

import pytest

from agent_bus import cli, prompts
from agent_bus.db import HUMAN, BusError
from agent_bus.peers import extract_reply, fenced_body
from agent_bus.service import Service
from agent_bus.worker import (Worker, diff_overview, normalize_verdict, settle_earlier, settlement_problem,
                               verdict_problem)
from test_review_rounds import BOTH, calls, git_repo, run_job


def verdict(v: str, findings=(), earlier=(), summary="s", prose="Prose.") -> str:
    obj = {"verdict": v, "summary": summary, "findings": list(findings)}
    if earlier:
        obj["earlier"] = list(earlier)
    return prose + "\n" + json.dumps(obj)


HIGH = {"file": "a.txt", "line": 1, "severity": "high", "comment": "typo"}


# ------------------------------------------------------------- follow-up framing (item 1)

def test_an_approving_reviewer_gets_the_approved_framing_not_converge(env, no_spawn, monkeypatch):
    """The PR #104 case: codex approved (with a low note), antigravity asked for changes. On the
    next head codex is told it approved and has nothing of its own to settle."""
    bus, repo = env["bus"], env["repo"]
    base, commit = git_repo(repo)
    h1 = commit("one\n")
    monkeypatch.setenv("FAKE_REPLY_CODEX", verdict("approve", [{**HIGH, "severity": "low"}]))
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review(None, str(repo), base, h1, reviewers=BOTH)["thread"]["id"]
    run_job(bus, tid)
    assert bus.review_standings(tid)["changes_requested"] == ["antigravity"]

    h2 = commit("two\n")
    monkeypatch.setenv("FAKE_APPROVE", "codex,antigravity")
    monkeypatch.delenv("FAKE_REPLY_CODEX")
    claude.request_review(None, str(repo), base, h2, prev_head_commit_id=h1, reviewers=BOTH)
    run_job(bus, tid)
    codex2, agy2 = calls(env)[2:4]
    assert codex2["agent"] == "codex" and agy2["agent"] == "antigravity"
    assert f"on a change you approved at {h1}" in codex2["prompt"]
    assert "other reviewers asked for changes" in codex2["prompt"]
    assert "fixed, withdrawn, or still blocking" not in codex2["prompt"]
    assert "The same rules apply as before" in codex2["prompt"] and "say which are" not in codex2["prompt"]
    assert "settle the findings listed at the top" in agy2["prompt"]
    assert "you approved at" not in agy2["prompt"]


def test_framing_follows_the_last_verdict(env):
    bus = env["bus"]
    tid = Service(bus, HUMAN).start_discussion("Q?", peers=["codex"])["thread"]["id"]
    w = Worker(bus, bus.jobs(tid)[-1]["id"])
    approved = {"data": {"verdict": "approve", "head_commit_id": "1" * 40}}
    asked = {"data": {"verdict": "request_changes", "head_commit_id": "1" * 40}}
    assert w.followup_framing(None, None, False, "") == ""
    alone = w.followup_framing(approved, None, False, "IDS")
    assert "you approved at " + "1" * 40 in alone and "the author changed it again" in alone and "IDS" not in alone
    assert "other reviewers asked for changes" in w.followup_framing(approved, "2" * 40, True, "")
    assert w.followup_framing(asked, None, False, "IDS") == prompts.CONVERGE + "IDS"


# ----------------------------------------------------------------- finding ids (item 2)

def test_findings_get_stable_ids_and_follow_ups_settle_them(env, no_spawn, monkeypatch):
    bus, repo = env["bus"], env["repo"]
    base, commit = git_repo(repo)
    h1 = commit("one\n")
    claude = Service(bus, "claude", bus.register_session("claude"))
    monkeypatch.setenv("FAKE_REPLY_CODEX", verdict("request_changes", [HIGH, {**HIGH, "file": "b.txt",
                                                                             "severity": "medium"}]))
    tid = claude.request_review("codex", str(repo), base, h1)["thread"]["id"]
    run_job(bus, tid)
    v1 = bus.last_verdict(tid, "codex")
    assert [f["id"] for f in v1["data"]["findings"]] == ["codex-1", "codex-2"]
    assert "- [high, blocking] codex-1 a.txt:1: typo" in v1["body"]

    # The follow-up lists both by id; the reviewer fixes one and keeps the other blocking
    # without repeating it, and raises a new one: it is carried over and the new one is codex-3.
    h2 = commit("two\n")
    monkeypatch.setenv("FAKE_REPLY_CODEX", verdict(
        "request_changes", [{"file": "c.txt", "line": 3, "severity": "medium", "comment": "new"}],
        earlier=[{"id": "codex-1", "status": "fixed", "note": "spelled right"},
                 {"id": "codex-2", "status": "still_blocking"},
                 {"id": "other-9", "status": "fixed"}]))
    claude.send("codex-1: fixed the typo", thread=tid)
    claude.request_review("codex", str(repo), base, h2, prev_head_commit_id=h1)
    run_job(bus, tid)
    p = calls(env)[-1]["prompt"]
    assert "- codex-1 [high] a.txt:1: typo" in p and "- codex-2 [medium] b.txt:1: typo" in p
    # The resumed confirmation pass leads with them (#159), before the interdiff.
    assert p.index("Your open blocking findings") < p.index("- codex-1 [high]") < p.index("```diff")
    # One complete follow-up verdict example (#126, #159): valid JSON, with "earlier" and a
    # finding listed again by its "id", using this reviewer's own ids.
    examples = [json.loads(line) for line in p.splitlines() if line.startswith('{"verdict"')]
    (still,) = [e for e in examples if e.get("earlier")]
    assert still["verdict"] == "request_changes" and [e["id"] for e in still["earlier"]] == ["codex-1", "codex-2"]
    assert still["findings"][0]["id"] == "codex-1" and "id" not in still["findings"][1]
    prior = {"codex-1": {"severity": "high"}, "codex-2": {"severity": "medium"}}
    v = settle_earlier(normalize_verdict(still), prior)
    assert verdict_problem(v) is None and settlement_problem(v, prior) is None
    v2 = bus.last_verdict(tid, "codex")
    assert [(f["id"], f["file"]) for f in v2["data"]["findings"]] == [("codex-3", "c.txt"), ("codex-2", "b.txt")]
    assert [e["id"] for e in v2["data"]["earlier"]] == ["codex-1", "codex-2"]  # not other-9
    assert "Earlier findings:\n- codex-1 fixed: spelled right\n- codex-2 still blocking" in v2["body"]
    assert "Findings:\n- [medium, blocking] codex-3 c.txt:3: new" in v2["body"]

    # Re-raising codex-2 by id keeps it, at its new severity; approving needs it settled.
    h3 = commit("three\n")
    monkeypatch.setenv("FAKE_REPLY_CODEX", verdict(
        "approve", [{**HIGH, "id": "codex-2", "severity": "low"}, {**HIGH, "id": "codex-1", "severity": "low"}],
        earlier=[{"id": "codex-2", "status": "withdrawn", "note": "minor"},
                 {"id": "codex-3", "status": "fixed"}]))
    claude.request_review("codex", str(repo), base, h3, prev_head_commit_id=h2)
    run_job(bus, tid)
    p = calls(env)[-1]["prompt"]
    assert "codex-3 [medium]" in p and "codex-2 [medium]" in p and "codex-1 [" not in p
    v3 = bus.last_verdict(tid, "codex")
    # codex-1 was settled already: not an earlier blocking finding now, so it gets a new id.
    assert [f["id"] for f in v3["data"]["findings"]] == ["codex-2", "codex-4"]
    assert bus.thread(tid)["status"] == "resolved"


def test_settlement_problems():
    from agent_bus.worker import settlement_problem
    prior = {"codex-1": {**HIGH, "id": "codex-1"}, "codex-2": {**HIGH, "id": "codex-2", "severity": "medium"}}

    def problem(**v):
        return settlement_problem(settle_earlier(normalize_verdict({"verdict": "approve", "findings": [], **v}),
                                                 prior), prior)
    assert "codex-1, codex-2 have no entry" in problem()
    assert problem(verdict="request_changes", findings=[{**HIGH, "file": "new"}]) is None  # still blocks
    assert "codex-2 has no entry" in problem(earlier=[{"id": "codex-1", "status": "fixed"}])
    # Listed again by id counts as settled (a downgrade to low, here).
    assert problem(earlier=[{"id": "codex-1", "status": "fixed"}],
                   findings=[{**HIGH, "id": "codex-2", "severity": "low"}]) is None
    assert "still_blocking but listed as low" in problem(
        earlier=[{"id": "codex-1", "status": "still_blocking"}, {"id": "codex-2", "status": "fixed"}],
        findings=[{**HIGH, "id": "codex-1", "severity": "low"}])
    assert "fixed but still listed as high" in problem(
        earlier=[{"id": "codex-1", "status": "fixed"}, {"id": "codex-2", "status": "fixed"}],
        findings=[{**HIGH, "id": "codex-1"}])
    assert settlement_problem(normalize_verdict({"verdict": "approve", "findings": []}), {}) is None


def test_an_unsettled_follow_up_gets_a_repair_turn(env, no_spawn, monkeypatch):
    bus, repo = env["bus"], env["repo"]
    base, commit = git_repo(repo)
    h1 = commit("one\n")
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review("codex", str(repo), base, h1)["thread"]["id"]
    run_job(bus, tid)  # codex-1, high
    h2 = commit("two\n")
    monkeypatch.setenv("FAKE_REPLY_CODEX", verdict("approve"))  # says nothing about codex-1
    monkeypatch.setenv("FAKE_APPROVE", "codex")  # the repair answers with an approval, still unsettled
    claude.request_review("codex", str(repo), base, h2, prev_head_commit_id=h1)
    run_job(bus, tid)
    repair = calls(env)[-1]["prompt"]
    assert "Reply again with only" in repair and "codex-1 has no entry" in repair
    assert bus.thread(tid)["status"] == "needs_human" and bus.last_verdict(tid, "codex")["data"]["verdict"] != "approve"


def test_an_unsaid_blocker_is_carried_until_settled(env, no_spawn, monkeypatch):
    """request_changes that settles codex-1, raises codex-3 and says nothing of codex-2 keeps
    codex-2 open; the next pass lists it, and an approval that leaves it unsaid is refused."""
    bus, repo = env["bus"], env["repo"]
    base, commit = git_repo(repo)
    h1 = commit("one\n")
    claude = Service(bus, "claude", bus.register_session("claude"))
    monkeypatch.setenv("FAKE_REPLY_CODEX", verdict("request_changes", [HIGH, {**HIGH, "file": "b.txt"}]))
    tid = claude.request_review("codex", str(repo), base, h1)["thread"]["id"]
    run_job(bus, tid)
    h2 = commit("two\n")
    monkeypatch.setenv("FAKE_REPLY_CODEX", verdict("request_changes", [{**HIGH, "file": "c.txt"}],
                                                   earlier=[{"id": "codex-1", "status": "fixed"}]))
    claude.request_review("codex", str(repo), base, h2, prev_head_commit_id=h1)
    run_job(bus, tid)
    v2 = bus.last_verdict(tid, "codex")
    assert [(f["id"], f["file"]) for f in v2["data"]["findings"]] == [("codex-3", "c.txt"), ("codex-2", "b.txt")]
    h3 = commit("three\n")
    monkeypatch.setenv("FAKE_REPLY_CODEX", verdict("approve", earlier=[{"id": "codex-3", "status": "fixed"}]))
    monkeypatch.setenv("FAKE_REPAIR_CODEX", verdict("approve", earlier=[{"id": "codex-2", "status": "fixed"},
                                                                        {"id": "codex-3", "status": "fixed"}],
                                                    prose=""))
    claude.request_review("codex", str(repo), base, h3, prev_head_commit_id=h2)
    run_job(bus, tid)
    assert "codex-2 [high] b.txt:1" in calls(env)[-2]["prompt"] and "codex-2 has no entry" in calls(env)[-1]["prompt"]
    v3 = bus.last_verdict(tid, "codex")
    assert v3["data"]["verdict"] == "approve" and [e["id"] for e in v3["data"]["earlier"]] == ["codex-2", "codex-3"]
    assert bus.thread(tid)["status"] == "resolved"


def test_a_repair_that_relists_a_finding_is_not_overridden(env, no_spawn, monkeypatch):
    """The first reply approves with codex-1 still_blocking; the repair lists codex-1 as low and
    gives no "earlier": the first reply's still_blocking is not merged back over it."""
    bus, repo = env["bus"], env["repo"]
    base, commit = git_repo(repo)
    h1 = commit("one\n")
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review("codex", str(repo), base, h1)["thread"]["id"]
    run_job(bus, tid)  # codex-1, high
    h2 = commit("two\n")
    monkeypatch.setenv("FAKE_REPLY_CODEX", verdict("approve", earlier=[{"id": "codex-1", "status": "still_blocking"}],
                                                   summary="first"))
    monkeypatch.setenv("FAKE_REPAIR_CODEX", '{"verdict": "approve", "findings": [{"id": "codex-1", "file": "a.txt", '
                                            '"line": 1, "severity": "low", "comment": "minor now"}]}')
    claude.request_review("codex", str(repo), base, h2, prev_head_commit_id=h1)
    run_job(bus, tid)
    v = bus.last_verdict(tid, "codex")
    assert v["data"]["verdict"] == "approve" and v["data"]["earlier"] == []
    assert v["data"]["findings"][0]["id"] == "codex-1" and "first" in v["body"]
    assert bus.thread(tid)["status"] == "resolved"


def test_still_blocking_on_an_approval_is_inconsistent():
    prior = {"codex-1": {**HIGH, "id": "codex-1"}}
    v = settle_earlier(normalize_verdict({"verdict": "approve", "summary": "ok", "findings": [],
                                          "earlier": [{"id": "codex-1", "status": "still_blocking"}]}), prior)
    from agent_bus.worker import verdict_problem
    assert v["findings"][0]["id"] == "codex-1" and "blocks approval" in verdict_problem(v)
    # Junk in "earlier" is dropped, not fatal.
    v = normalize_verdict({"verdict": "approve", "findings": [], "earlier": [1, {"id": "x"}, {"id": "codex-1",
                                                                                         "status": "gone"}]})
    assert v["earlier"] == []


# ------------------------------------------------------------- caveats on votes (item 3)

def test_agree_votes_carry_caveats(env, no_spawn):
    bus = env["bus"]
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.start_discussion("Which DB?", peers=["codex"])["thread"]["id"]
    claude.send("Use SQLite", thread=tid, kind="proposal")
    res = claude.send(thread=tid, kind="vote", on="P1", stance="agree", reasons="simple",
                      caveats="add a backup job later")
    m = bus.message(res["posted"]["id"])
    assert m["body"] == "simple\n\nCaveats (non-blocking): add a backup job later"
    assert m["data"]["caveats"] == "add a backup job later"
    with pytest.raises(BusError) as e:
        claude.send(thread=tid, kind="vote", on="P1", stance="object", reasons="no", caveats="x")
    assert e.value.code == "BAD_VOTE"
    with pytest.raises(BusError):
        claude.send("hi", thread=tid, caveats="x")


def test_headless_agree_vote_caveats_and_a_proposal_in_a_body_block(env, no_spawn, monkeypatch):
    bus = env["bus"]
    long = 'Use "SQLite":\n\n```yaml\nkey: "quoted"\n```\n{"not": "the reply"}'
    monkeypatch.setenv("FAKE_REPLY_CODEX",
                       f'Thinking.\n~~~~body\n{long}\n~~~~\n{{"action": "propose", "reasons": "simple"}}')
    tid = Service(bus, HUMAN).start_discussion("Which DB?", peers=["codex", "antigravity"])["thread"]["id"]
    w = Worker(bus, bus.jobs(tid)[-1]["id"])
    assert bus.claim_job(w.job["id"], w.owner)
    w.round_one(bus.thread(tid))
    props = {p["sender"]: p for p in bus.proposals(tid)}
    assert props["codex"]["body"] == long + "\n\nReasons: simple"
    assert "~~~~" not in props["codex"]["data"]["discussion"]

    bus.set_thread(tid, blind=0)
    monkeypatch.setenv("FAKE_REPLY_CODEX", '{"action": "vote", "on": "P2", "stance": "agree", "reasons": "fine", '
                                           '"caveats": "watch the WAL size"}')
    monkeypatch.setenv("FAKE_REPLY_ANTIGRAVITY", '~~~body\nLong reasons\nover lines\n~~~\n'
                                                 '{"action": "vote", "on": "P1", "stance": "agree"}')
    w.later_round()
    votes = {m["sender"]: m for m in bus.history(tid)["messages"] if m["kind"] == "vote"}
    assert votes["codex"]["body"] == "fine\n\nCaveats (non-blocking): watch the WAL size"
    assert votes["codex"]["data"]["caveats"] == "watch the WAL size"
    assert votes["antigravity"]["body"] == "Long reasons\nover lines"
    assert "Caveats (non-blocking): watch the WAL size" in cli.format_message(votes["codex"], False)


# ------------------------------------------------------------ long answers (item 4)

@pytest.mark.parametrize("text, body, rest", [
    ('~~~~body\nA ``` block:\n```\ncode\n```\n~~~~\n{"a": 1}', "A ``` block:\n```\ncode\n```", '{"a": 1}'),
    ('```body\nx\n````\n{"a": 1}', "x", '{"a": 1}'),  # a longer closing fence closes it
    ('before {"a": 0}\n~~~body\nunclosed {"a": 2}', None, 'before {"a": 0}'),  # cut, JSON and all
    ('no block {"a": 3}', None, 'no block {"a": 3}'),
    # A body marker quoted in an ordinary code block is not one.
    ('```text\n~~~~body\n```\n{"a": 6}', None, '```text\n~~~~body\n```\n{"a": 6}'),
    ('```json\n{"a": 7}\n```', None, '```json\n{"a": 7}\n```'),
    ('~~~body\nfirst {"a": 4}\n~~~\n~~~body\nsecond\n~~~\n{}', "second", '{}'),  # the last; both left out
    ('~~~body\nB\n~~~\n{"a": 5}\n~~~body\ncut short', None, '{"a": 5}'),  # truncated: no body
])
def test_fenced_body(text, body, rest):
    assert fenced_body(text) == (body, rest)


def test_extract_reply_keeps_the_old_format_and_ignores_json_in_the_body():
    assert extract_reply('{"verdict": "approve", "summary": "inline"}', "summary")["summary"] == "inline"
    # The JSON field wins over a body block; an empty one is filled from it.
    assert extract_reply('~~~body\nB\n~~~\n{"summary": "J"}', "summary")["summary"] == "J"
    assert extract_reply('~~~body\nB\n~~~\n{"summary": " "}', "summary")["summary"] == "B"
    # JSON inside the body is never taken for the reply's own object.
    assert extract_reply('~~~body\n{"verdict": "approve"}\n~~~\nno json', "summary") is None
    assert extract_reply('~~~body\nB\n~~~\n{"action": "vote"}', lambda o: "reasons")["reasons"] == "B"
    # A reply cut off inside its body block never yields the example verdict it quotes.
    assert extract_reply('~~~~body\nFor example {"verdict": "approve", "findings": []} is', "summary") is None
    # A non-string action with a body block is just an invalid move, not a crash.
    from agent_bus.worker import Worker as W, move_field
    for bad in ([], {"x": 1}):
        a = extract_reply('~~~body\nB\n~~~\n' + json.dumps({"action": bad}), move_field)
        assert a == {"action": bad} and not W.valid_action(a)


def test_review_summary_in_a_body_block(env, no_spawn, monkeypatch):
    bus, repo = env["bus"], env["repo"]
    base, commit = git_repo(repo)
    h1 = commit("one\n")
    summary = 'Query: `SELECT "x" FROM t`\nYAML: a: "b"\n\n```sql\nSELECT 1;\n```'
    monkeypatch.setenv("FAKE_REPLY_CODEX", f'~~~~body\n{summary}\n~~~~\n{{"verdict": "approve", "findings": []}}')
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review("codex", str(repo), base, h1)["thread"]["id"]
    run_job(bus, tid)
    assert bus.last_verdict(tid, "codex")["body"] == f"Verdict: approve\n\n{summary}"
    p = calls(env)[0]["prompt"]
    assert "~~~~body" in p and 'leave "summary" out of the JSON' in p


def test_every_prompt_that_asks_for_json_explains_the_body_block():
    for text in (prompts.ROUND1, prompts.LATER, prompts.VERDICT_FORMAT):
        assert "~~~~body" in text
    assert "caveats" in prompts.LATER and "caveats" in prompts.REPAIR


# ---------------------------------------------------------------- smaller ones (item 6)

def test_round_two_recap_leaves_out_the_opening_question(env, no_spawn):
    bus = env["bus"]
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.start_discussion("QUESTION-TEXT: which DB?", peers=["codex", "antigravity"])["thread"]["id"]
    claude.send("claude's own view", thread=tid, kind="proposal")
    w = Worker(bus, bus.jobs(tid)[-1]["id"])
    assert bus.claim_job(w.job["id"], w.owner)
    w.round_one(bus.thread(tid))
    bus.set_thread(tid, blind=0, round=2)
    resumed, fresh, _ = w.later_prompt("codex")
    assert "QUESTION-TEXT" not in resumed and "claude's own view" in resumed
    assert fresh.count("QUESTION-TEXT") == 1  # asked once, not again in the transcript


def test_tail_header_names_the_roles(env, no_spawn):
    bus = env["bus"]
    t = bus.thread(Service(bus, HUMAN).start_discussion("Q?", peers=["codex"])["thread"]["id"])
    assert cli.thread_header(t) == f"agent-bus {t['id']} · discussion · started by the human user · peer: codex"
    claude = Service(bus, "claude", bus.register_session("claude"))
    t = bus.thread(claude.start_discussion("Q?", peers=["codex", "antigravity"])["thread"]["id"])
    assert cli.thread_header(t).endswith("started by claude · peers: codex, antigravity")
    t = {"id": "t-1", "kind": "review", "initiator": "claude", "participants": ["claude", "codex"], "refs": {}}
    assert cli.thread_header(t) == "agent-bus t-1 · review · requested by claude · reviewer: codex"


def test_overview_escapes_control_characters_in_paths():
    diff = ('diff --git "a/two\\nlines.txt" "b/two\\nlines.txt"\nnew file mode 100644\n--- /dev/null\n'
            '+++ "b/two\\nlines.txt"\n@@ -0,0 +1 @@\n+x\n'
            'diff --git "a/bell\\a" "b/bell\\a"\n--- "a/bell\\a"\n+++ "b/bell\\a"\n@@ -1 +1 @@\n-a\n+b\n')
    assert diff_overview(diff) == "2 files, +2 -1\n  A two\\nlines.txt  +1 -0\n  M bell\\x07  +1 -1"


def test_commit_log_never_runs_the_signature_verifier(env, no_spawn, monkeypatch, tmp_path):
    """A signed commit and log.showSignature: git log would run gpg.program on it (checked
    below, so the fixture proves something), but the review's commit log doesn't."""
    bus, repo = env["bus"], env["repo"]
    monkeypatch.setenv("FAKE_APPROVE", "codex")
    base, commit = git_repo(repo)
    commit("one\n")
    marker = tmp_path / "verifier-ran"
    verifier = tmp_path / "fake-gpg"
    verifier.write_text(f"#!/bin/sh\ntouch {marker}\nexit 1\n")
    verifier.chmod(0o755)
    git = lambda *a, **kw: subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True,  # noqa: E731
                                          text=True, **kw).stdout.strip()
    tree, parent = git("rev-parse", "HEAD^{tree}"), git("rev-parse", "HEAD")
    obj = (f"tree {tree}\nparent {parent}\nauthor t <t@example.com> 1700000000 +0000\n"
           f"committer t <t@example.com> 1700000000 +0000\n"
           "gpgsig -----BEGIN PGP SIGNATURE-----\n \n iQEzBAABCAAdFiEE\n -----END PGP SIGNATURE-----\n\nsigned\n")
    head = git("hash-object", "-t", "commit", "-w", "--stdin", input=obj)
    git("update-ref", "refs/heads/signed", head)
    git("config", "log.showSignature", "true")
    git("config", "gpg.program", str(verifier))
    git("log", "-1", head)
    assert marker.exists(), "the fixture's signature should make a plain git log run the verifier"
    os.unlink(marker)

    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review("codex", str(repo), base, head)["thread"]["id"]
    run_job(bus, tid)
    assert f"{head[:12]} signed" in calls(env)[0]["prompt"]
    assert not marker.exists()


def test_usage_rows_from_before_the_split_are_kept_and_flagged(env, capsys):
    """Rows recorded before #101 (input_split 0) are never rewritten: the participant's
    harness then isn't known for sure.  The report counts them apart, and a session resumed
    across the upgrade gets no share against such a baseline, rather than a wrong one."""
    bus = env["bus"]
    # As a writer from before #101 leaves them (an old worker still running during the upgrade).
    for total in ({"input": 18033, "cache_read": 13952}, None):
        bus.conn.execute("INSERT INTO turn_usage (thread_id, agent, session, ok, input, cache_read, session_total,"
                         " started_at, created_at) VALUES ('t-1', 'codex', ?, 1, 18033, 13952, ?, 1, 1)",
                         ("s" if total else None, json.dumps(total) if total else None))
    bus.conn.execute("UPDATE turn_usage SET created_at=?", (time.time() - 60,))
    share = bus.record_usage("t-1", "codex", None, ok=True, session="s", resumed=True,
                             session_total={"input": 4181, "cache_read": 14852},
                             row=bus.begin_usage("t-1", "codex", session="s"))
    assert share is None
    rows = bus._all("SELECT * FROM turn_usage ORDER BY id")
    assert [r["input"] for r in rows[:2]] == [18033, 18033] and [r["input_split"] for r in rows] == [0, 0, 1]
    assert rows[2]["session_total"] is None and json.loads(rows[2]["split_total"])["input"] == 4181
    # The next turn has a like baseline again.
    bus.conn.execute("UPDATE turn_usage SET created_at=created_at-1, started_at=started_at-1")
    assert bus.record_usage("t-1", "codex", None, ok=True, session="s", resumed=True,
                            session_total={"input": 4281, "cache_read": 15852},
                            row=bus.begin_usage("t-1", "codex", session="s")) == {"input": 100, "cache_read": 1000}
    assert cli.main(["usage", "--json"]) == 0
    codex = json.loads(capsys.readouterr().out)["agents"]["codex"]
    assert codex["unsplit_turns"] == 2 and codex["unsplit_input"] == 2 * 18033
    assert codex["unsplit_cache_read"] == 2 * 13952
    # Only the split rows feed the split columns: 100 uncached and 1000 cached.
    assert (codex["input"], codex["cache_read"], codex["uncached_input"], codex["cached_input"],
            codex["input_total"]) == (100, 1000, 100, 1000, 1100)
    assert cli.main(["usage"]) == 0
    out = capsys.readouterr().out
    assert "split cached from uncached input" in out and "codex 2 turns, input 36,066 cache read 27,904" in out


def test_migration_marks_old_usage_rows(env):
    import sqlite3

    from agent_bus.db import Bus
    path = env["bus"].path
    env["bus"].record_usage("t-1", "codex", {"input": 9}, ok=True)
    env["bus"].close()
    con = sqlite3.connect(path)
    con.execute("ALTER TABLE turn_usage DROP COLUMN input_split")
    con.execute("ALTER TABLE turn_usage DROP COLUMN split_total")
    con.commit()
    con.close()
    bus = Bus(path)
    bus.record_usage("t-1", "codex", {"input": 9}, ok=True)
    assert [r["input_split"] for r in bus._all("SELECT input_split FROM turn_usage ORDER BY id")] == [0, 1]
    bus.close()
