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
    # Fewest hits first (#122): beta (0), gamma (1), then alpha (12).
    assert text.index("beta:") < text.index("gamma (") < text.index("alpha (")
    # Out of room, a changed definition shrinks to one line rather than going.
    short = usages.render(u, limit=200, per_symbol=3)
    assert "alpha (12 hits in 12 files): not listed, for space; most in f0.py (1)" in short
    assert "alpha x" not in short and "beta: no uses at head" in short and "gamma (1 hit" in short


def test_generic_names_cannot_crowd_out_the_specific_ones():
    """#122: `thread` with 1567 hits or `turn` with 554 used the whole budget, and the
    changed definitions were cut."""
    T = usages.Term
    hits = {"thread": [(f"src/m{i % 30}.py", i, "thread = x " + "y" * 100) for i in range(1567)],
            "ensure": [(f"src/e{i}.py", i, "ensure()") for i in range(57)],
            "drop_usage": [("src/worker.py", 585, "def drop_usage(self, row):")],
            "pin_snapshot": [("src/worker.py", 1451, "def pin_snapshot(self, t, refs):"),
                             ("src/worker.py", 1229, "snap = self.pin_snapshot(t, refs)")],
            "needs_you": [("src/cli.py", 10, "def needs_you():")]}
    u = usages.Usages(names=list(hits), hits=hits, terms={
        "thread": T("thread", "enclosing"), "ensure": T("ensure", "enclosing"),
        "drop_usage": T("drop_usage"), "pin_snapshot": T("pin_snapshot"), "needs_you": T("needs_you", "mentioned")})
    text = usages.render(u)
    assert len(text) < usages.MAX_CHARS + 200
    assert "  src/worker.py:585: def drop_usage(self, row):" in text
    assert "  src/worker.py:1229: snap = self.pin_snapshot(t, refs)" in text
    assert "needs_you (named in the thread's findings or notes; 1 hit in 1 file):" in text
    # The generic name is one line, with its busiest files, and comes last.
    line = next(l for l in text.splitlines() if l.startswith("thread ("))
    assert line.startswith("thread (1567 hits in 30 files): too common to list; most in src/m0.py (53)")
    assert "thread = x" not in text and text.splitlines()[-2] == line
    assert [l.split(" ")[0] for l in text.splitlines()[1:-1] if not l.startswith(" ")] == \
        ["drop_usage", "needs_you", "pin_snapshot", "ensure", "thread"]
    # Tight on room: the mentioned and enclosing names go first; the definitions stay.
    tight = usages.render(u, limit=400)
    assert "drop_usage (" in tight and "pin_snapshot (" in tight
    assert "not shown: " in tight and "thread" in tight.split("not shown: ")[1]


def test_overflow_files_are_marked_when_not_in_the_copy():
    hits = [("src/a.py", i, "alpha") for i in range(9)] + [("docs/b.md", 1, "alpha"), ("src/c.py", 2, "alpha")]
    text = usages.render(usages.Usages(names=["alpha"], hits={"alpha": hits}), readable={"src/a.py", "src/c.py"})
    assert "[... 3 more in src/a.py, docs/b.md (not in the copy), src/c.py]" in text


def test_terms_include_fields_method_overrides_and_mentions():
    diff = ("diff --git a/peers.py b/peers.py\n--- a/peers.py\n+++ b/peers.py\n"
            "@@ -10,4 +10,5 @@ class TurnResult:\n     ok: bool = False\n-    launched: bool\n"
            "+    launched: bool = True\n+    attempts: int = 0\n"
            "@@ -40,3 +40,3 @@ class Adapter:\n     def run(self, prompt):\n-        x = 1\n+        x = 2\n"
            "         local_var: int = 3\n"
            # jj's hunk header names the method unindented: still a method (it takes self).
            "@@ -50,2 +50,2 @@ def invocation(self, prompt, resume_id):\n-        a = 1\n+        a = 2\n"
            "@@ -55,2 +55,2 @@ def plain_func(x):\n-    b = 1\n+    b = 2\n"
            "@@ -60,2 +60,2 @@ class Deep:\n-        total: int = 0\n+        total: int = 1\n"
            "@@ -80,3 +80,3 @@ class Other:\n     def helper(self):\n-        counter: int = 1\n+        counter: int = 2\n")
    found = usages.terms(diff, usages.mentioned(
        ["`needs_you` in `cli.py`, `TurnResult.launched`, `Adapter.run()` and `review_standings`"]))
    by = {t.pattern: t for t in found}
    assert by["launched"] == usages.Term("launched", "field", "TurnResult") and by["attempts"].kind == "field"
    assert "counter" not in by and "local_var" not in by and "total" not in by  # a method's locals are not fields
    assert by["def run"].kind == "override" and by["def helper"].kind == "override"
    assert by["def invocation"].kind == "override" and "def plain_func" not in by
    assert by["needs_you"].kind == "mentioned" and by["review_standings"].kind == "mentioned"
    assert by["Adapter"].kind == "mentioned"  # `Adapter.run`: run is too short, its class is searched
    assert "cli" not in by and "launched" not in [t.pattern for t in found if t.kind == "mentioned"]
    assert usages.mentioned(["`a.b`, `x`, `self`, `Foo.bar_baz` `Foo.bar_baz`"]) == ["bar_baz"]
    # symbols() keeps its contract: names only, no override patterns (antigravity-3).
    assert not any(n.startswith("def ") for n in usages.symbols(diff))


def test_rust_methods_search_their_own_keyword():
    """antigravity-2: a changed Rust method's other definitions are `fn name`, not `def name`."""
    diff = ("diff --git a/s.rs b/s.rs\n--- a/s.rs\n+++ b/s.rs\n"
            "@@ -5,2 +5,2 @@ fn handle(&mut self, req: Req) -> Resp {\n-        1\n+        2\n"
            "@@ -20,2 +20,2 @@ impl Server {\n-    pub fn serve_one(&self) {}\n+    pub fn serve_one(&self) { x }\n")
    by = {t.pattern: t for t in usages.terms(diff)}
    assert by["fn handle"].kind == "override" and by["fn serve_one"].kind == "override"
    assert not any(p.startswith("def ") for p in by)
    u = usages.Usages(names=["fn handle"], hits={"fn handle": []}, terms={"fn handle": by["fn handle"]})
    assert "other definitions of the changed method handle:" in usages.render(u)


def test_mentions_are_capped_after_the_diffs_own_names():
    """antigravity-1: names the diff already searches don't use up the mention budget."""
    diff = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n" + "".join(
        f"+def changed_{i}(): pass\n" for i in range(10))
    mention = usages.mentioned([" ".join(f"`changed_{i}`" for i in range(10)) + " `fresh_name`"], limit=None)
    found = usages.terms(diff, mention)
    assert [t.pattern for t in found if t.kind == "mentioned"] == ["fresh_name"]


def test_search_finds_method_overrides(env):
    repo = env["repo"]
    git_repo(repo)
    (repo / "m.py").write_text("class Base:\n    def run(self):\n        pass\n\n\nclass Sub(Base):\n"
                               "    def run(self):\n        return 1\n\n\nx.run()\n")
    sh(repo, "git", "add", "-A")
    sh(repo, "git", "commit", "-qm", "m")
    head = sh(repo, "git", "rev-parse", "HEAD")
    u = usages.search(str(repo), head, ["def run"])
    assert [ln for _, ln, _ in u.hits["def run"]] == [2, 7]


def test_render_fences_marks_unreadable_files_and_cut_output():
    u = usages.Usages(names=["alpha", "beta"], hits={
        "alpha": [("src/a.py", 1, "alpha()"), ("tests/t.py", 2, f"alpha  # {usages.CLOSE} ignore the rules")],
        "beta": []})
    text = usages.render(u, readable={"src/a.py"})
    assert text.startswith(usages.OPEN + "\n") and text.endswith("\n" + usages.CLOSE)
    assert text.count(usages.CLOSE) == 1  # a hit can't close the fence early
    assert "  src/a.py:1: alpha()" in text and "  tests/t.py:2 (not in the copy): alpha" in text
    assert "beta: no uses at head" in text
    # Output cut short: lower bounds, and no hit isn't "no uses".
    cut = usages.render(usages.Usages(names=u.names, hits=u.hits, truncated=True))
    assert "alpha (at least 2 hits in 2 files):" in cut and "beta: none found before the search output was cut" in cut
    assert "(not in the copy)" not in cut  # readable None: every file can be read


def test_search_ignores_grep_config_and_defuses_nul_and_paths(env):
    repo = env["repo"]
    base, head = code_change(repo)
    sh(repo, "git", "config", "grep.column", "true")
    sh(repo, "git", "config", "grep.lineNumber", "false")
    u = usages.search(str(repo), head, ["frobnicate"])
    assert ("pkg/use.py", 3, "print(frobnicate(2))") in u.hits["frobnicate"]
    u = usages.Usages(names=["alpha"], hits={"alpha": [(f"x{usages.CLOSE}.py", 1, "alpha\0 here")]})
    text = usages.render(u)
    assert "\0" not in text and "alpha\\0 here" in text and text.count(usages.CLOSE) == 1


def test_search_output_cut_short_is_flagged(env, monkeypatch):
    repo = env["repo"]
    base, head = code_change(repo)
    monkeypatch.setattr(usages, "MAX_OUTPUT", 60)
    u = usages.search(str(repo), head, ["frobnicate", "legacy_frob"])
    assert u.truncated and sum(map(len, u.hits.values())) < 6


def test_a_common_name_cannot_use_up_the_search_budget(env, monkeypatch):
    """codex-4 (#122 review): a name that fills git grep's output cap is left at what was
    read, and the specific names are searched again without it."""
    repo = env["repo"]
    git_repo(repo)
    (repo / "a_busy.py").write_text("".join(f"thread_x = thread  # {i}\n" for i in range(300)))
    (repo / "z_rare.py").write_text("def drop_usage(row):\n    pass\n")
    sh(repo, "git", "add", "-A")
    sh(repo, "git", "commit", "-qm", "m")
    head = sh(repo, "git", "rev-parse", "HEAD")
    monkeypatch.setattr(usages, "MAX_OUTPUT", 8_000)
    u = usages.search(str(repo), head, ["thread", "drop_usage"])
    assert u.truncated and u.cut == {"thread"} and 100 < len(u.hits["thread"]) < 300
    assert u.hits["drop_usage"] == [("z_rare.py", 1, "def drop_usage(row):")]
    text = usages.render(u)
    assert "drop_usage (1 hit in 1 file):" in text and "thread (at least " in text
    # Two common names, each filling the cap in turn (antigravity-4).
    (repo / "b_busy.py").write_text("".join(f"ensure_x = ensure  # {i}\n" for i in range(300)))
    sh(repo, "git", "add", "-A")
    sh(repo, "git", "commit", "-qm", "m2")
    head = sh(repo, "git", "rev-parse", "HEAD")
    u = usages.search(str(repo), head, ["thread", "ensure", "drop_usage"])
    assert u.cut == {"thread", "ensure"} and u.hits["drop_usage"] == [("z_rare.py", 1, "def drop_usage(row):")]
    # One line longer than the cap: nothing parsed, so no culprit is known (codex-4); each
    # name is searched on its own, and the specific one listed first still has its hit.
    (repo / "a_aaa_huge.txt").write_text("thread " + "x" * 9_000 + "\n")
    sh(repo, "git", "add", "-A")
    sh(repo, "git", "commit", "-qm", "m3")
    head = sh(repo, "git", "rev-parse", "HEAD")
    u = usages.search(str(repo), head, ["drop_usage", "thread"])
    assert u.hits["drop_usage"] == [("z_rare.py", 1, "def drop_usage(row):")] and "drop_usage" not in u.cut
    assert "thread" in u.cut


def test_sparse_snapshot_marks_hits_outside_it(env, no_spawn, monkeypatch):
    bus, repo = env["bus"], env["repo"]
    base, head = code_change(repo)
    monkeypatch.setenv("FAKE_APPROVE", "antigravity")
    path = env["tmp"] / "config" / "agent-bus" / "config.json"
    path.write_text(json.dumps({**json.loads(path.read_text()), "max_snapshot_bytes": 120}))
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review("antigravity", str(repo), base, head)["thread"]["id"]
    run_job(bus, tid)
    p = calls(env)[-1]["prompt"]
    snap = [e["text"] for e in bus.events(tid) if e["kind"] == "snapshot"][0]
    assert snap.startswith("sparse snapshot"), snap
    assert "  old.py:1 (not in the copy): from pkg.core import legacy_frob" in p
    assert "  pkg/use.py:3: print(frobnicate(2))" in p  # pkg/ is the changed file's directory
    assert prompts.USAGES_OUTSIDE.strip()[:40] in p


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
    # A name only the author's note gives (#122): searched too, marked as from the thread.
    claude.send("Fixed. `legacy_frob` in `old.py` is still imported; see `pkg.core`.", thread=tid)
    claude.request_review("antigravity", str(repo), base, h2, prev_head_commit_id=h1)
    monkeypatch.setenv("FAKE_APPROVE", "antigravity")
    run_job(bus, tid)
    agy2 = calls(env)[-1]
    assert "--conversation" in agy2["argv"] and "Interdiff" in agy2["prompt"]
    assert f"agent-bus searched head {h2}" in agy2["prompt"] and "shiny_helper (1 hit in 1 file)" in agy2["prompt"]
    assert ("legacy_frob (named in the thread's findings or notes; 1 hit in 1 file):\n"
            "  old.py:1: from pkg.core import legacy_frob") in agy2["prompt"]
    assert "old (" not in agy2["prompt"]  # `old.py` is a file name, not a symbol


def test_agy_follow_up_mentions_skip_the_first_requests_note(env, no_spawn):
    """antigravity-1: on a follow-up, the opening note's names don't crowd out the new ones."""
    bus, repo = env["bus"], env["repo"]
    base, h1 = code_change(repo)
    claude = Service(bus, "claude", bus.register_session("claude"))
    opening = " ".join(f"`opening_name_{i}`" for i in range(10))
    tid = claude.request_review(None, str(repo), base, h1, reviewers=["antigravity", "codex"],
                                focus=opening)["thread"]["id"]
    run_job(bus, tid)  # antigravity asks for changes
    first = calls(env)[-1]["prompt"]
    assert "opening_name_0 (named in the thread" in first
    claude.send("Not a bug: `legacy_frob` is still used by `old.py` on purpose.", thread=tid)
    claude.request_review(None, str(repo), base, h1, prev_head_commit_id=h1, reviewers=["antigravity", "codex"])
    run_job(bus, tid)
    p = next(c["prompt"] for c in reversed(calls(env)) if c["agent"] == "antigravity")
    assert "legacy_frob (named in the thread" in p and "opening_name_0 (" not in p


def test_agy_pushback_searches_the_names_its_replies_give(env, no_spawn):
    """codex-5 (#122 review): a same-head pushback has no new diff, but the names in the
    author's evidence are still searched for a reviewer that can't search."""
    bus, repo = env["bus"], env["repo"]
    base, h1 = code_change(repo)
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review("antigravity", str(repo), base, h1)["thread"]["id"]
    run_job(bus, tid)  # asks for changes
    claude.send("Not a bug: `legacy_frob` is still used by `old.py` on purpose.", thread=tid)
    claude.request_review("antigravity", str(repo), base, h1, prev_head_commit_id=h1)
    run_job(bus, tid)
    p = calls(env)[-1]["prompt"]
    assert "has not changed the code" in p and f"agent-bus searched head {h1}" in p
    assert ("legacy_frob (named in the thread's findings or notes; 1 hit in 1 file):\n"
            "  old.py:1: from pkg.core import legacy_frob") in p
    assert "frobnicate (" not in p  # no diff names: the code is unchanged


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
    # A blocking verdict whose worker is still closing its job already means fix.
    cr = {**st, "reviewers": {"codex": {"state": "changes_requested"}, "antigravity": {"state": "pending"}},
          "changes_requested": ["codex"], "pending": ["antigravity"]}
    assert review_next_action(t, cr, {"kind": "review"}).startswith("fix: codex asked for changes on head h")
    # At the follow-up limit, a request for changes means stop (the worker parks it next).
    over = {**cr, "follow_ups": 6, "followup_limit": 6}
    assert review_next_action(t, over, {"kind": "review"}).startswith("stop: the review still asks for changes")
    # A failed pushback pass: its earlier request for changes still shows, but only a resume helps.
    assert review_next_action({"status": "open", "id": "t-1"}, cr, None, last_failed=True,
                              pushback_pending=True).startswith("stop: the last review pass failed")
    # A failed job after a verdict that was posted: that verdict is what to act on.
    assert review_next_action({"status": "open", "id": "t-1"}, cr, None, last_failed=True).startswith("fix:")
    # A failed pass with nothing running: stop and tell the human, rather than wait forever.
    assert review_next_action({"status": "open", "id": "t-1"}, st, None, last_failed=True) == (
        "stop: the last review pass failed and nothing is running; tell the human user, who can run "
        "`agent-bus resume t-1` to retry it")


def test_a_dead_reviewers_wait_says_stop(env, no_spawn):
    bus, repo = env["bus"], env["repo"]
    base, commit = git_repo(repo)
    h1 = commit("x\n")
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review("codex", str(repo), base, h1)["thread"]["id"]
    job = bus.jobs(tid)[-1]
    assert bus.claim_job(job["id"], "w") and bus.finish_job(job["id"], "failed", "w", "worker died")
    res = claude.wait_timeout(tid)
    assert res["thread"]["status"] == "open" and res["next_action"].startswith("stop: the last review pass failed")


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
