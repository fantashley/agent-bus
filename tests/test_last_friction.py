"""#135: usage-search ranking (production names before test-only ones, no definitions inside
string literals) and the first-pass nudge to report every blocking finding at once.  The
range-diff on rebased follow-ups is tested in test_rebase_interdiff.py."""
from agent_bus import usages
from agent_bus.service import Service
from test_review_rounds import BOTH, calls, git_repo, run_job


def _file(path: str, body: str, header: str = "") -> str:
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1,3 +1,3 @@{header}\n{body}"


def test_names_changed_only_in_tests_rank_after_production_ones():
    """The #136 review case: test helpers took the room at top priority, so changed production
    methods were left out."""
    diff = (_file("tests/test_wait.py", "+def call(x): pass\n+def bodies(m): pass\n+    s.client_session.go\n")
            + _file("src/pkg/db.py", "-        x = 1\n+        x = 2\n", " def unread(self, thread):")
            + _file("src/pkg/db.py", "+    def replay(self): pass\n")
            + _file("tests/conftest.py", "+def shared_helper(): pass\n+def replay(): pass\n"))
    found = usages.terms(diff)
    by = {t.pattern: t for t in found}
    # Touched outside tests: their usual kind, even if a test touches them too.
    assert by["replay"] == usages.Term("replay", "defined") and by["unread"] == usages.Term("unread", "enclosing")
    assert by["bodies"] == usages.Term("bodies", "defined", test=True) and by["shared_helper"].test
    names = [t.pattern for t in found if t.kind not in ("override", "read")]
    assert names == ["replay", "unread", "call", "bodies", "shared_helper"]
    assert usages.symbols(diff) == names
    # The cap drops the test-only names first.
    assert usages.symbols(diff, limit=2) == ["replay", "unread"]
    # Production methods' other definitions first, and production reads before test ones.
    assert [t.pattern for t in found if t.kind == "override"][:2] == ["def unread", "def replay"]
    assert not by["def unread"].test
    assert "client_session" in [t.pattern for t in found if t.kind == "read"]
    reads = [t.pattern for t in usages.terms(_file("tests/t.py", "+    a.from_test\n")
                                             + _file("src/m.py", "+    b.from_code\n")) if t.kind == "read"]
    assert reads == ["from_code", "from_test"]


def test_test_paths():
    for p in ("tests/test_x.py", "pkg/tests/util.py", "test/a.go", "src/__tests__/a.js", "conftest.py",
              "src/test_api.py", "server_test.go", "web/app.spec.ts", "web/app.test.js", "spec/models/user_spec.rb"):
        assert usages.is_test(p), p
    for p in ("src/agent_bus/usages.py", "src/testing.py", "src/contest.py", "docs/tests.md", "attest/x.py"):
        assert not usages.is_test(p), p


def test_test_only_names_render_after_production_ones_and_say_so():
    T = usages.Term
    hits = {"helper_one": [("tests/t.py", 1, "def helper_one(): pass")],
            "unread": [("src/db.py", i, "unread()") for i in range(5)]}
    u = usages.Usages(names=["helper_one", "unread"], hits=hits,
                      terms={"helper_one": T("helper_one", test=True), "unread": T("unread", "enclosing")})
    text = usages.render(u)
    assert "helper_one (only in tests; 1 hit in 1 file)" in text
    # Kept with room to spare, it still comes after the production name, though it has fewer
    # hits (#142 debrief).
    assert text.index("unread (5 hits") < text.index("helper_one (")
    # With room for one, the production name keeps it, though it has more hits.
    tight = usages.render(u, limit=len(usages.render(usages.Usages(names=["unread"], hits=hits))))
    assert "unread (5 hits" in tight and "not shown: helper_one" in tight
    # Every test-only term, whatever its kind, gets room after production reads and mentions
    # (codex-1, antigravity-1), even with fewer hits and an earlier place in the list.
    one = [("tests/t.py", 1, "x")]
    for kind in ("defined", "override", "read"):
        hits = {"test_side": one, "runtime_flag": [("src/c.py", i, "runtime_flag = 1") for i in range(5)],
                "mentioned_one": [("src/d.py", i, "mentioned_one()") for i in range(5)]}
        terms = {"test_side": T("test_side", kind, test=True), "runtime_flag": T("runtime_flag", "read"),
                 "mentioned_one": T("mentioned_one", "mentioned")}
        assert usages.priority(terms["test_side"]) > usages.priority(terms["runtime_flag"])
        both = usages.Usages(names=["runtime_flag", "mentioned_one"], hits=hits, terms=terms)
        u = usages.Usages(names=list(hits), hits=hits, terms=terms)
        room = len(usages.render(both)) - len(usages.OPEN) - len(usages.CLOSE) - 1  # the two blocks, each with its newline
        tight = usages.render(u, limit=room)
        assert "runtime_flag (" in tight and "mentioned_one (" in tight and "not shown: test_side" in tight, kind
    u = usages.Usages(names=["def helper_method"], hits={"def helper_method": one},
                      terms={"def helper_method": T("def helper_method", "override", test=True)})
    assert "changed method helper_method: overrides, or the one it overrides, only in tests;" in usages.render(u)
    # Reads from test files are marked too.
    reads = {t.pattern: t for t in usages.terms(_file("tests/t.py", "+    a.from_test\n")) if t.kind == "read"}
    assert reads["from_test"].test


def test_removed_lines_are_classified_by_the_old_path():
    """codex-2: a rename between src/ and tests/ classifies each side by its own path."""
    def rename(a, b, body):
        return (f"diff --git a/{a} b/{b}\nsimilarity index 50%\nrename from {a}\nrename to {b}\n"
                f"--- a/{a}\n+++ b/{b}\n@@ -1,2 +1,2 @@\n{body}")
    found = {t.pattern: t for t in usages.terms(rename("src/helpers.py", "tests/helpers.py",
                                                       "-def from_code(): pass\n+def into_test(): pass\n"))}
    assert not found["from_code"].test and found["into_test"].test
    found = {t.pattern: t for t in usages.terms(rename("tests/helpers.py", "src/helpers.py",
                                                       "-def from_test(): pass\n+def into_code(): pass\n"))}
    assert found["from_test"].test and not found["into_code"].test
    # Quoted (non-ASCII) paths are decoded the way git writes them.
    q = ('diff --git "a/caf\\303\\251/conftest.py" "b/caf\\303\\251/conftest.py"\n'
         '--- "a/caf\\303\\251/conftest.py"\n+++ "b/caf\\303\\251/conftest.py"\n@@ -1 +1 @@\n+def quoted_helper(): pass\n')
    assert usages.terms(q)[0] == usages.Term("quoted_helper", test=True)
    # A path with a space ends in a tab on the ---/+++ lines (antigravity-2).
    sp = ("diff --git a/my dir/conftest.py b/my dir/conftest.py\n--- a/my dir/conftest.py\t\n"
          "+++ b/my dir/conftest.py\t\n@@ -1 +1 @@\n+def spaced_helper(): pass\n")
    assert usages.terms(sp)[0] == usages.Term("spaced_helper", test=True)
    # Hunk content that looks like a file header is content (codex-7): `++ "\777"` added, and
    # `-- a/tests/x.py` removed, neither changes the path or stops the scan.
    body = '+++ "\\777"\n--- a/tests/x.py\n+def after_header_like(): pass\n'
    found = usages.terms(_file("src/m.py", body))
    assert found[0] == usages.Term("after_header_like")
    assert usages.terms('diff --git a/x.py b/x.py\n--- "a/\\777"\n+++ b/x.py\n@@ -1 +1 @@\n+def bad_quote(): pass\n')


def test_definitions_inside_string_literals_define_nothing():
    """A fake diff in a test (`"+def key_0(): pass"`) is not a definition (#136 review)."""
    body = ('+    diff = "+def key_0(): pass\\n" + \'+def key_1(): pass\'\n'
            '+    doc = """+def key_2(): pass\n'  # a multi-line string: from its quote to the line end
            '+    md = `def key_3`\n'
            '+def real_one(x): return "def key_4"\n'
            "+    escaped = 'it\\'s def key_5'\n")
    names = usages.symbols(_file("tests/test_a.py", body))
    assert names == ["real_one"]
    # codex-3: a triple-quoted literal holding a lone quote, and a hunk header's string.
    triple = _file("src/a.py", '+EXAMPLE = """A quote " then def fake_name(): pass"""\n'
                               "+B = '''it's def fake_two'''; def after_it(): pass\n")
    assert usages.symbols(triple) == ["EXAMPLE", "after_it"]
    hdr2 = _file("src/w.py", "-    x = 1\n+    x = 2\n", ' EXAMPLE = "+def fake_name(): pass"')
    assert usages.symbols(hdr2) == []
    # Rust's lifetimes are not quotes.
    rs = _file("src/s.rs", "+impl<'a> Iter<'a> { fn next_item(&mut self) {} }\n+let c = 'x'; fn after_char() {}\n")
    assert usages.symbols(rs) == ["next_item", "after_char"]
    # A hunk header's function is found the same way.
    hdr = _file("src/w.py", "-    x = 1\n+    x = 2\n", ' def opening(refs): s = "def fake_name"')
    assert usages.symbols(hdr) == ["opening"]


def test_first_pass_asks_for_every_blocking_finding_at_once(env, no_spawn, monkeypatch):
    bus, repo = env["bus"], env["repo"]
    base, commit = git_repo(repo)
    h1 = commit("one\n")
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review(None, str(repo), base, h1, reviewers=BOTH)["thread"]["id"]
    run_job(bus, tid)
    nudge = "report every blocking finding you can find in this one\npass"
    (first,) = calls(env)  # codex asks for changes (the fake peer's default)
    assert first["agent"] == "codex" and nudge in first["prompt"]
    h2 = commit("two\n")
    monkeypatch.setenv("FAKE_APPROVE", "codex,antigravity")
    claude.request_review(None, str(repo), base, h2, prev_head_commit_id=h1, reviewers=BOTH)
    run_job(bus, tid)
    by = {c["agent"]: c["prompt"] for c in calls(env)[1:]}
    assert nudge not in by["codex"] and "settle the findings listed at the top" in by["codex"]  # it converges
    assert nudge in by["antigravity"]  # its own first pass
