"""#117: follow-ups from peer debriefs.  A reviewer without search (Antigravity) is shown where
the change's names are used, a shell reviewer's read commands number the lines and say to
read a little at a time, every wait on a review thread says where the review stands, and
turn_start labels count each reviewer's own passes."""
import asyncio
import json
import os
import subprocess

from agent_bus import mcp_server, prompts, usages
from agent_bus.service import Service, review_next_action

from test_review_rounds import BOTH, calls, git_repo, run_job


def sh(repo, *argv):
    genv = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
                GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")
    return subprocess.run(argv, cwd=repo, check=True, capture_output=True, text=True, env=genv).stdout.strip()


def code_change(repo):
    """A base with a helper, its caller and its test; a head that renames the helper and
    drops an old one that a forgotten caller still uses."""
    base, _ = git_repo(repo)
    (repo / "pkg").mkdir()
    (repo / "pkg" / "core.py").write_text(
        "MAX_WIDGETS = 3\n\n\ndef frobnicate(x):\n    return x + 1\n\n\ndef legacy_frob(x):\n    return x\n")
    (repo / "pkg" / "use.py").write_text("from pkg.core import frobnicate\n\nprint(frobnicate(2))\n")
    (repo / "old.py").write_text("from pkg.core import legacy_frob\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_core.py").write_text("from pkg.core import frobnicate\n\n\ndef test_it():\n"
                                                  "    assert frobnicate(1) == 2\n")
    sh(repo, "git", "add", "-A")
    sh(repo, "git", "commit", "-qm", "code")
    base = sh(repo, "git", "rev-parse", "HEAD")
    (repo / "pkg" / "core.py").write_text("MAX_WIDGETS = 4\n\n\ndef frobnicate(x):\n    return x + 2\n")
    sh(repo, "git", "commit", "-qam", "change")
    return base, sh(repo, "git", "rev-parse", "HEAD")


def test_symbols_are_definitions_constants_and_enclosing_functions():
    diff = ("diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
            "@@ -1,3 +1,3 @@ class Widget:\n-def old_name(x):\n+def new_name(x):\n+    self.data = 1\n"
            "@@ -10,2 +10,2 @@ def render_page(req):\n-    x = 1\n+    x = 2\n+LIMIT_BYTES: int = 5\n"
            "+    def test_helper(): pass\n+fn parse_args() {}\n+func (s *Server) ServeHTTP(w) {}\n"
            "+def __init__(self): pass\n+x == FOO\n"
            # No function in the header (the hunk starts at the top): the definition above the change.
            "diff --git a/b.py b/b.py\n--- a/b.py\n+++ b/b.py\n@@ -1,3 +1,3 @@\n def compute_total(a):\n"
            "-    return a\n+    return a + 1\n")
    names = usages.symbols(diff)
    assert names == ["old_name", "new_name", "LIMIT_BYTES", "parse_args", "ServeHTTP", "render_page", "compute_total"]
    assert len(usages.symbols(diff, limit=2)) == 2


def test_search_finds_uses_at_the_head_not_the_working_copy(env):
    repo = env["repo"]
    base, head = code_change(repo)
    (repo / "pkg" / "extra.py").write_text("frobnicate  # working copy only\n")  # untracked: not searched
    u = usages.search(str(repo), head, ["frobnicate", "legacy_frob", "MAX_WIDGETS"])
    assert u is not None and not u.truncated
    assert {p for p, _, _ in u.hits["frobnicate"]} == {"pkg/core.py", "pkg/use.py", "tests/test_core.py"}
    assert ("pkg/use.py", 3, "print(frobnicate(2))") in u.hits["frobnicate"]
    # A removed definition whose caller was forgotten: its one remaining use shows.
    assert u.hits["legacy_frob"] == [("old.py", 1, "from pkg.core import legacy_frob")]
    text = usages.render(u)
    assert "frobnicate (5 hits in 3 files):" in text and "  pkg/use.py:3: print(frobnicate(2))" in text
    assert "legacy_frob (1 hit in 1 file):" in text
    # Unreadable repository: no search rather than an error.
    assert usages.search(str(env["tmp"]), head, ["frobnicate"]) is None


def test_render_caps_hits_per_name_and_in_all():
    u = usages.Usages(names=["alpha", "beta", "gamma"], hits={
        "alpha": [(f"f{i}.py", i, "alpha " + "x" * 300) for i in range(12)], "beta": [], "gamma": [("g.py", 1, "gamma")]})
    text = usages.render(u, per_symbol=3)
    assert text.count("alpha x") == 3 and "[... 9 more in f3.py, f4.py" in text and "and 3 more]" in text
    assert "beta: no uses at head" in text and " [...]" in text
    short = usages.render(u, limit=len(text.split("\nbeta")[0]) + 5, per_symbol=3)
    assert "[... 2 more names not shown: beta, gamma]" in short and "gamma (" not in short


def test_agy_review_shows_usages_and_codex_gets_numbered_reads(env, no_spawn, monkeypatch):
    bus, repo = env["bus"], env["repo"]
    monkeypatch.setenv("FAKE_APPROVE", "codex,antigravity")
    base, head = code_change(repo)
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review(None, str(repo), base, head, reviewers=BOTH)["thread"]["id"]
    run_job(bus, tid)
    codex, agy = calls(env)[:2]
    # antigravity: the search agent-bus ran for it, with paths and lines to read around.
    a = agy["prompt"]
    assert agy["agent"] == "antigravity" and f"agent-bus searched head {head} for you" in a
    assert "  pkg/use.py:3: print(frobnicate(2))" in a and "  old.py:1: from pkg.core import legacy_frob" in a
    assert "tests/test_core.py:" in a and "MAX_WIDGETS (" in a
    assert "StartLine and EndLine" in a  # both in the guidance and the usages note
    assert "nl -ba" not in a
    assert any(e["kind"] == "usages" for e in bus.events(tid))
    # codex has a shell and its own search: numbered, copyable reads and a hint to read a
    # little at a time, but no usages section.
    p = codex["prompt"]
    assert codex["agent"] == "codex" and "agent-bus searched head" not in p
    assert f"git -C {repo} --no-replace-objects show {head}:<path> | nl -ba" in p
    snap = [e["text"] for e in bus.events(tid) if e["kind"] == "snapshot"][0].rsplit("at ", 1)[1]
    assert f"nl -ba {snap}/<path>" in p
    assert prompts.READ_TIP_SHELL.strip() in p


def test_agy_followup_searches_the_interdiff(env, no_spawn, monkeypatch):
    bus, repo = env["bus"], env["repo"]
    base, h1 = code_change(repo)
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review("antigravity", str(repo), base, h1)["thread"]["id"]
    run_job(bus, tid)  # asks for changes
    (repo / "pkg" / "use.py").write_text("from pkg.core import frobnicate\n\n\ndef shiny_helper():\n"
                                        "    return frobnicate(3)\n")
    sh(repo, "git", "commit", "-qam", "fix")
    h2 = sh(repo, "git", "rev-parse", "HEAD")
    claude.request_review("antigravity", str(repo), base, h2, prev_head_commit_id=h1)
    monkeypatch.setenv("FAKE_APPROVE", "antigravity")
    run_job(bus, tid)
    agy2 = calls(env)[-1]
    assert "--conversation" in agy2["argv"] and "Interdiff" in agy2["prompt"]
    assert f"agent-bus searched head {h2}" in agy2["prompt"] and "shiny_helper (1 hit in 1 file)" in agy2["prompt"]


def call(server, tool, args):
    res = asyncio.run(server.call_tool(tool, args))
    return json.loads(res.content[0].text)


def test_every_wait_on_a_review_says_where_it_stands(env, no_spawn, monkeypatch):
    bus, repo = env["bus"], env["repo"]
    base, commit = git_repo(repo)
    h1 = commit("helo\n")
    server = mcp_server.build("claude", bus=bus)
    sid = call(server, "whoami", {}).get("session_id")
    claude = Service(bus, "claude", sid or bus.register_session("claude"))
    tid = claude.request_review(None, str(repo), base, h1, reviewers=BOTH)["thread"]["id"]
    run_job(bus, tid)  # codex asks for changes; antigravity never runs
    res = call(server, "wait", {"thread": tid, "timeout_s": 5})
    assert res.get("status") != "timeout" and any(m["kind"] == "review_verdict" for m in res["messages"])
    assert res["review"]["changes_requested"] == ["codex"] and res["review"]["pending"] == ["antigravity"]
    assert res["thread"]["status"] == "open"
    assert res["next_action"].startswith("fix: codex asked for changes on head " + h1)
    assert f"prev_head_commit_id={h1}" in res["next_action"]
    # A timeout says the same.
    res = call(server, "wait", {"thread": tid, "timeout_s": 1})
    assert res["status"] == "timeout" and res["next_action"].startswith("fix:")
    # The fix is under review: keep waiting.
    h2 = commit("hello\n")
    claude.request_review(None, str(repo), base, h2, prev_head_commit_id=h1, reviewers=BOTH)
    res = call(server, "wait", {"thread": tid, "timeout_s": 1})
    assert res["next_action"] == f"wait: antigravity is reviewing head {h2}"
    monkeypatch.setenv("FAKE_APPROVE", "codex,antigravity")
    run_job(bus, tid)
    res = call(server, "wait", {"thread": tid, "timeout_s": 5})
    assert res["review"]["complete"] and res["next_action"] == f"done: every required reviewer approved head {h2}"
    assert call(server, "history", {"thread": tid})["next_action"].startswith("done:")


def test_next_action_for_the_other_states():
    st = {"head_commit_id": "h", "reviewers": {"codex": {"state": "pending"}}, "complete": False,
          "changes_requested": [], "pending": ["codex"]}
    t = {"status": "open"}
    assert review_next_action(t, st, None) == "wait: codex still to review head h"
    assert review_next_action(t, st, {"kind": "review"}) == "wait: codex is reviewing head h"
    assert review_next_action({"status": "needs_human"}, st, None).startswith("stop: the thread waits for the human")
    assert review_next_action({"status": "cancelled"}, st, None) == "stop: the review was cancelled"
    assert review_next_action({"status": "resolved"}, {**st, "complete": True}, {"kind": "debrief"}) == \
        "wait: debrief replies are on their way"
    assert review_next_action({"status": "resolved"}, {**st, "complete": True}, None) == "done: codex approved head h"


def test_turn_labels_count_each_reviewers_own_passes(env, no_spawn, monkeypatch):
    bus, repo = env["bus"], env["repo"]
    base, commit = git_repo(repo)
    h1 = commit("helo\n")
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review(None, str(repo), base, h1, reviewers=BOTH)["thread"]["id"]
    run_job(bus, tid)  # codex asks for changes
    h2 = commit("hello\n")
    claude.request_review(None, str(repo), base, h2, prev_head_commit_id=h1, reviewers=BOTH)
    monkeypatch.setenv("FAKE_APPROVE", "codex,antigravity")
    run_job(bus, tid)
    starts = [(e["agent"], e["text"].split(" · ")[0]) for e in bus.events(tid) if e["kind"] == "turn_start"]
    # antigravity's first turn is its pass 1, though the thread is in its second round.
    assert starts == [("codex", "pass 1"), ("antigravity", "pass 1"), ("codex", "pass 2 (resumed)")]
    assert bus.thread(tid)["round"] >= 2
