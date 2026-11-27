"""#191: where the functions the changed lines call are defined; #194: line numbers in a listed
name's tail."""
from agent_bus import usages

from test_usage_defs import change

CONFIG = '''def vcs(path):
    """The function vcs ... see class Snapshot."""
    return "git"


class Config:
    def _allowed_repo(self, repo):
        return repo
'''


def test_callees_are_the_names_added_lines_call():
    diff = ("diff --git a/src/s.py b/src/s.py\n--- a/src/s.py\n+++ b/src/s.py\n@@ -1,1 +1,9 @@\n"
            " import os\n"
            "+def ensure_copy(repo):\n"
            "+    if cfg.vcs(repo) != \"jj\": os.replace(a, b)\n"
            "+    path = self._allowed_repo(repo)  # comment_call(x)\n"
            "+    log(\"not_a_call(x)\", len(path), sorted(path), d.get(k))\n"
            "+    # commented_out(x)\n"
            "+    return ensure_copy(path) or Snapshot(path)\n"
            "+    obj.__init__(x)\n"
            "+    log(x)\n"
            "diff --git a/tests/test_s.py b/tests/test_s.py\n--- a/tests/test_s.py\n+++ b/tests/test_s.py\n"
            "@@ -1,0 +1,2 @@\n+make_repo(tmp)\n+log(y)\n"
            "diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n@@ -1,0 +1,1 @@\n+prose_call(x)\n")
    # Production calls first, in order, then test-only ones, then the diff's own definitions;
    # builtins, a stdlib module's functions, generic words, dunders, strings, comments and
    # prose left out.
    assert usages.callees(diff) == ["vcs", "_allowed_repo", "log", "Snapshot", "make_repo", "ensure_copy"]


def test_definitions_are_found_at_head_outside_tests_first(tmp_path):
    repo, head, _ = change(tmp_path, {}, {
        "src/config.py": CONFIG,
        "src/snap.py": "class Snapshot:\n    pass\n",
        "tests/test_x.py": "def vcs(p):\n    return None\n",
        "docs/a.md": "def vcs(path) is described here\n",
        "tests/data/x.diff": "+def vcs(path):\n",
        "web/app.js": "function render_list(x) {}\n// function vcs(x)\n",
    })
    found, cut = usages.definitions(repo, head, ["vcs", "_allowed_repo", "missing_name", "Snapshot", "render_list"])
    assert not cut
    assert found == {"vcs": [("src/config.py", 1), ("tests/test_x.py", 1)],
                     "_allowed_repo": [("src/config.py", 7)],
                     "Snapshot": [("src/snap.py", 1)],
                     "render_list": [("web/app.js", 1)]}
    assert usages.definitions(repo, head, []) == ({}, False)
    assert usages.definitions(str(tmp_path), head, ["vcs"]) == ({}, False)  # not a repository


def test_note_lists_where_the_called_functions_are_defined(tmp_path):
    repo, head, diff = change(tmp_path, {"src/config.py": CONFIG, "src/snap.py": "def ensure(repo):\n    pass\n"}, {
        "src/config.py": CONFIG,
        "src/snap.py": "def ensure(repo):\n    if cfg.vcs(repo) == 'jj':\n        cfg._allowed_repo(repo)\n",
    })
    text, _, outside = usages.note(repo, head, diff, readable={"src/snap.py"})
    defs = text[text.index("Where what the changed lines call is defined:"):]
    assert "  vcs: src/config.py (not in the copy):1\n" in defs
    assert "  _allowed_repo: src/config.py (not in the copy):7\n" in defs
    assert outside
    # The change's own names are listed with their uses, not here.
    assert "  ensure:" not in defs


def test_called_definitions_are_capped():
    defs = {f"name_{i}": [(f"src/m{j}.py", j) for j in range(5)] for i in range(30)}
    text = usages.render(usages.Usages(), defs=defs)
    section = text[text.index("Where what the changed lines call is defined:"):]
    assert "  name_0: src/m0.py:0, src/m1.py:1, src/m2.py:2 [+2 more]" in section
    assert sum(len(line) + 1 for line in section.split("\n") if line.startswith("  ")) <= usages.DEF_CHARS
    assert section.count("\n  name_") <= usages.MAX_CALLEES and "more names not shown]" in section


def test_a_listed_names_tail_gives_line_numbers():
    # #194: "[... 6 more in worker.py]" left a reviewer with no search guessing line ranges.
    hits = [("src/worker.py", ln, "x = alpha") for ln in (10, 20, 30, 40)] + [("tests/test_a.py", 5, "alpha")]
    text = usages.render(usages.Usages(names=["alpha"], hits={"alpha": hits}), per_symbol=2)
    assert "  [... 3 more at src/worker.py:30,40; tests/test_a.py:5]" in text


def test_python_calls_come_from_the_file_parsed_at_head(tmp_path):
    # codex-1: an f-string's call counts, a string's text doesn't, nor does a comment's "def".
    before = {"src/c.py": CONFIG, "src/s.py": "def ensure(repo):\n    pass\n"}
    after = {"src/c.py": CONFIG, "src/s.py": (
        "def ensure(repo):\n"
        "    print(f'{cfg.vcs(repo)}')\n"
        "    doc = '''\n"
        "    cfg.in_a_string(repo)\n"
        "    '''\n"
        "    answer = cfg._allowed_repo(repo)  # see def _allowed_repo below\n"
        "    return compile(x) or cfg.compile(y)\n")}
    repo, head, diff = change(tmp_path, before, after)
    called = usages.callees(diff, usages.pinned_python(repo, head, diff))
    assert called == ["vcs", "_allowed_repo", "compile"]  # cfg.compile, not the builtin
    # Without the parsed file (it can't be read), the lines' code is matched.
    assert "in_a_string" in usages.callees(diff) and "_allowed_repo" in usages.callees(diff)


def test_a_called_name_the_diff_also_defines_is_kept_last():
    # codex-2: a new Proxy.resolve_ref delegating to provider.resolve_ref still looks it up.
    diff = ("diff --git a/src/p.js b/src/p.js\n--- a/src/p.js\n+++ b/src/p.js\n@@ -1,0 +1,3 @@\n"
            "+function resolve_ref(x) { return provider.resolve_ref(x); }\n"
            "+const ready = cfg.from(x); // function hidden_call(y)\n"
            "+const big = cfg.compile(x);\n")
    assert usages.callees(diff) == ["from", "compile", "resolve_ref"]


def test_definition_shaped_text_is_not_a_definition(tmp_path):
    # codex-3: a def in a test's triple-quoted fixture, and one in a trailing JS comment.
    repo, head, _ = change(tmp_path, {}, {
        "tests/test_f.py": 'MODULE = """\ndef removed_fn():\n    pass\n"""\n',
        "web/a.js": "const ready = true; // function resolveItem() {}\n",
        "src/real.py": "class Outer:\n    def removed_fn(self):\n        def resolveItem():\n            pass\n",
    })
    found, _ = usages.definitions(repo, head, ["removed_fn", "resolveItem"])
    assert found == {"removed_fn": [("src/real.py", 2)], "resolveItem": [("src/real.py", 3)]}


def test_a_cut_definition_search_is_said(tmp_path, monkeypatch):
    # codex-4: output cut before any definition was read still tells the reviewer.
    repo, head, _ = change(tmp_path, {}, {"a/big.js": "function vcs() {}" * 50 + "\n", "src/c.py": CONFIG})
    monkeypatch.setattr(usages, "MAX_OUTPUT", 100)
    found, cut = usages.definitions(repo, head, ["vcs", "_allowed_repo"])
    assert cut and found == {}
    text = usages.render(usages.Usages(), defs=found, defs_cut=cut)
    assert "Where what the changed lines call is defined:" in text and "output was cut short" in text


def test_a_tail_names_every_file_before_more_lines_of_one():
    # codex-5: 65 hits in one file used to crowd the other two files out of the tail.
    hits = [("src/a.py", i, "x") for i in range(65)] + [("src/b.py", 1, "x"), ("src/c.py", 2, "x")]
    line = usages._lines_at(hits, lambda p: p)
    assert "src/b.py:1" in line and "src/c.py:2" in line and line.startswith("src/a.py:0,1,2,")
    assert len(line) <= usages.COMPACT
    # codex-5 (round 3): test files' first lines too, before more production lines.
    hits = [("src/a.py", i, "x") for i in range(65)] + [("tests/b.py", 1, "x"), ("tests/c.py", 2, "x")]
    line = usages._lines_at(hits, lambda p: p)
    assert line.startswith("src/a.py:0,1,") and "; tests/b.py:1; tests/c.py:2 [+" in line


def test_a_python_blob_two_paths_share_is_parsed_once(tmp_path, monkeypatch):
    # codex-7: identical copies were each parsed again.
    repo, head, _ = change(tmp_path, {}, {f"src/copy{i}.py": CONFIG for i in range(5)})
    parses = []
    real = usages.py_scopes
    monkeypatch.setattr(usages, "py_scopes", lambda text: parses.append(1) or real(text))
    found, _ = usages.definitions(repo, head, ["vcs"])
    assert len(found["vcs"]) == 5 and len(parses) == 1


def test_a_long_added_line_is_scanned_quickly():
    # codex-6: each call used to rescan its whole line (about 10 s at 144 KB).
    import time
    line = "cfg.commit_item();" * 8000
    diff = f"diff --git a/a.js b/a.js\n--- a/a.js\n+++ b/a.js\n@@ -1,0 +1,1 @@\n+{line}\n"
    start = time.monotonic()
    assert usages.callees(diff) == ["commit_item"]
    assert time.monotonic() - start < 2
