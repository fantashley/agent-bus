"""#174, #175: which definitions a change touches.  In a Python file they are read from the
file parsed with ast (a local helper counts as a change to the function it sits in, on
either side of the diff); elsewhere a keyword counts only at the start of a statement's
code, and prose files and comment lines define nothing."""
import os
import subprocess
from pathlib import Path

from agent_bus import usages
from agent_bus.worker import Diff

DATA = Path(__file__).parent / "data" / "usages"
PATH = "src/agent_bus/usages.py"


def sh(repo, *argv, stdin=None):
    genv = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
                GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")
    return subprocess.run(argv, cwd=repo, check=True, capture_output=True, text=True, env=genv,
                          input=stdin).stdout


def commit(repo, files):
    """Commit ``files`` (path -> text, None to delete) in ``repo``: its id."""
    if not (repo / ".git").exists():
        sh(repo, "git", "init", "-q")
    for p, text in files.items():
        f = repo / p
        if text is None:
            f.unlink()
        else:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text)
    sh(repo, "git", "add", "-A")
    sh(repo, "git", "commit", "-qm", "c", "--allow-empty")
    return sh(repo, "git", "rev-parse", "HEAD").strip()


def change(tmp_path, before, after):
    """A real git diff between two commits holding ``before`` and ``after`` (path -> text):
    (repo, head, diff)."""
    repo = tmp_path / "r"
    repo.mkdir(parents=True, exist_ok=True)
    base = commit(repo, before)
    head = commit(repo, after)
    return str(repo), head, sh(repo, "git", "diff", base, head)


def kinds(repo, head, diff):
    """The terms for ``diff`` with its Python files parsed: pattern -> kind."""
    return {t.pattern: t.kind for t in usages.terms(diff, scopes=usages.pinned_python(repo, head, diff))}


def test_the_nested_helper_from_160_counts_as_a_change_to_render(tmp_path):
    """The real #160 diff that added `def listing(k)` inside `usages.render` (#174)."""
    diff = (DATA / "usages-160.diff").read_text()
    head_text = (DATA / "usages-160.py").read_text()
    repo = tmp_path / "r"
    repo.mkdir()
    head = commit(repo, {PATH: head_text})
    scopes = usages.pinned_python(str(repo), head, diff)
    assert set(scopes) == {("+", PATH), ("-", PATH)}
    # The old side rebuilt from the head and the hunks is the base's blob itself.
    (f,) = usages._files(diff)
    old = usages.old_text(usages._lines(head_text), f.hunks)
    oid = sh(repo, "git", "hash-object", "--stdin", stdin="\n".join(old) + "\n").strip()
    assert oid.startswith(diff.split("\n")[1].split()[1].split("..")[0])
    got = kinds(str(repo), head, diff)
    assert "listing" not in got and "def listing" not in got
    assert got["render"] == "enclosing" and got["SAMPLE"] == "defined"
    # Without the parsed file it is matched as before: the helper is a top-priority name.
    assert {t.pattern: t.kind for t in usages.terms(diff)}["listing"] == "defined"


def test_signature_edit_and_deletion_of_the_helper(tmp_path):
    """codex-4 on #173: the removed line of a signature edit, and a deleted helper."""
    text = (DATA / "usages-160.py").read_text()
    sig = text.replace("def listing(k: int) -> str:", "def listing(k: int, extra: int = 0) -> str:")
    repo, head, diff = change(tmp_path, {PATH: text}, {PATH: sig})
    assert "-        def listing(k: int)" in diff
    got = kinds(repo, head, diff)
    assert got == {"render": "enclosing"}
    start = text.index("        def listing(")
    end = text.index('            return "\\n".join(lines)\n', start) + len('            return "\\n".join(lines)\n')
    repo, head, diff = change(tmp_path, {PATH: sig}, {PATH: text[:start] + text[end:]})
    # The helper's own calls are gone (#196): where those functions are still used is searched too,
    # but not a builtin type's method (`dict.fromkeys`, #196 again).
    assert kinds(repo, head, diff) == {"render": "enclosing", "_clean": "removed", "_defuse": "removed",
                                       "mark": "removed"}


MODULE = '''\
from typing import TYPE_CHECKING


class Worker:
    @staticmethod
    def testing_note(t: dict, refs: dict) -> str:
        """The author's note on how this head was tested."""
        return refs.get("testing") or ""

    @staticmethod
    def review_notes(t: dict, refs: dict, notes: str) -> str:
        return notes

    def reading_note(
        self, refs: dict, listing: bool = True,
    ) -> str:
        def shown_path(p):
            return p
        return shown_path(refs["repo"])


def outer_function(
    a: int,
    b: int,
) -> None:
    def inner_helper(x):
        def deep(y):
            return y
        return deep(x)
    return inner_helper(a + b)


if TYPE_CHECKING:
    def compat_fn():
        return 1


def removed_fn():
    return 2


def load_defaults():
    return {}
'''


def test_scopes_come_from_the_file_not_the_hunk(tmp_path):
    """The layouts that sank the hunk-indentation heuristic on #173."""
    after = (MODULE
             .replace("        return notes\n", "        return notes.strip()\n")  # antigravity-3
             .replace("        def shown_path(p):\n            return p\n",
                      "        def shown_path(p, q=None):\n            return p or q\n")  # in a method
             .replace("        def deep(y):\n            return y\n",
                      "        def deep(y, z=0):\n            return y + z\n")  # codex-10, antigravity-6
             .replace("    def compat_fn():\n        return 1\n", "    def compat_fn():\n        return 3\n")
             .replace("def removed_fn():\n    return 2\n\n\n", ""))  # antigravity-2
    repo, head, diff = change(tmp_path, {"m.py": MODULE}, {"m.py": after})
    got = kinds(repo, head, diff)
    assert "review_notes" in got and "def review_notes" in got  # a sibling method, not a local helper
    assert got["reading_note"] == "enclosing" and got["def reading_note"] == "override"
    assert got["outer_function"] == "enclosing" and got["compat_fn"] == "enclosing"
    assert got["removed_fn"] == "defined"
    assert not {"shown_path", "inner_helper", "deep", "def shown_path", "def deep"} & set(got)


def test_a_function_replaced_by_a_method(tmp_path):
    """codex-5 on #173: the removed and added sides are placed in their own files."""
    before = "class Settings:\n    path: str = ''\n\n\ndef load_defaults():\n    return {}\n"
    after = ("class Settings:\n    path: str = ''\n\n    def reload_settings(self):\n        def merge(a):\n"
             "            return a\n        return merge({})\n")
    repo, head, diff = change(tmp_path, {"s.py": before}, {"s.py": after})
    got = kinds(repo, head, diff)
    assert got["load_defaults"] == "defined" and got["reload_settings"] == "defined"
    assert got["def reload_settings"] == "override" and "merge" not in got


def test_a_deleted_file_and_a_new_one(tmp_path):
    new = "def brand_new():\n    def local_one():\n        pass\n    return local_one\n"
    repo, head, diff = change(tmp_path, {"gone.py": MODULE}, {"gone.py": None, "new.py": new})
    assert "+++ /dev/null" in diff and "--- /dev/null" in diff
    got = kinds(repo, head, diff)
    assert {"outer_function", "compat_fn", "removed_fn", "Worker", "brand_new"} <= set(got)
    assert not {"inner_helper", "deep", "shown_path", "local_one"} & set(got)


def test_fields_constants_and_docstrings_come_from_the_file(tmp_path):
    before = ('"""Docs."""\nLIMITS = {\n    "a": 1,\n}\n\n\nclass Turn:\n    launched: bool = False\n\n\n'
              'def run_turn(x):\n    """Run x."""\n    return x\n')
    after = ('"""Docs: a function defined inside another one.\n\ndef fake_name(): pass\n"""\nLIMITS = {\n'
             '    "a": 2,\n}\n\n\nclass Turn:\n    launched: bool = True\n\n\n'
             'def run_turn(x):\n    """Run x: the class of things that are defined inside."""\n    return x\n')
    repo, head, diff = change(tmp_path, {"t.py": before}, {"t.py": after})
    got = kinds(repo, head, diff)
    assert got == {"LIMITS": "defined", "launched": "field", "run_turn": "enclosing"}


def test_limits_and_unparsable_files_fall_back(tmp_path):
    text = (DATA / "usages-160.py").read_text()
    sig = text.replace("def listing(k: int) -> str:", "def listing(k: int, extra: int = 0) -> str:")
    repo, head, diff = change(tmp_path, {PATH: text, "bad.py": "def f(:\n"}, {PATH: sig, "bad.py": "def g(:\n"})
    # Over the budget, the file isn't read at all (sizes are checked first).
    assert usages.pinned_python(repo, head, diff, budget=1000) == {}
    scopes = usages.pinned_python(repo, head, diff)
    assert ("+", PATH) in scopes and ("+", "bad.py") not in scopes
    assert usages.MAX_SOURCE < 10 ** 7
    # A diff that doesn't fit the head (another head, a cut hunk) gives no old side.
    (f,) = [f for f in usages._files(diff) if f.new == PATH]
    assert usages.old_text(usages._lines(text), f.hunks) is None
    assert usages.old_text(usages._lines(sig), f.hunks) is not None
    # antigravity-4: a deletion past the end of a shorter head.
    assert usages.old_text(["a"], [(3, 1, 2, 0, ["x"], [])]) is None


def test_note_rebuilds_from_the_full_diff_of_a_capped_one(tmp_path):
    text = (DATA / "usages-160.py").read_text()
    sig = text.replace("def listing(k: int) -> str:", "def listing(k: int, extra: int = 0) -> str:")
    repo, head, diff = change(tmp_path, {PATH: text}, {PATH: sig})
    # codex-8: the shown diff is cut right after the changed lines, so its hunk doesn't fit the
    # head: only the full diff (raw) rebuilds the old side.
    cut = diff[:diff.index("\n", diff.index("+        def listing(")) + 1]
    assert usages.old_text(usages._lines(sig), usages._files(cut)[0].hunks) is None
    hits, _, _ = usages.note(repo, head, Diff(cut, "", raw=diff))
    assert "render (" in hits and "listing" not in hits
    hits, _, _ = usages.note(repo, head, cut)
    assert "listing" in hits  # without it, the removed line is matched as before


def test_crlf_bom_and_blank_context_lines(tmp_path):
    """codex-1, codex-2: lines as the worker reads the diff (text mode: universal newlines), a
    BOM, and blank context lines that diff.suppressBlankEmpty leaves without their space."""
    before = MODULE.replace("def inner_helper(x):", "def inner_helper(x, y=0):")
    for name, enc in (("crlf.py", lambda t: t.replace("\n", "\r\n")), ("bom.py", lambda t: "\ufeff" + t)):
        repo = tmp_path / name
        repo.mkdir()
        base = commit(repo, {name: enc(MODULE)})
        head = commit(repo, {name: enc(before)})
        diff = sh(repo, "git", "diff", base, head)
        assert kinds(str(repo), head, diff) == {"outer_function": "enclosing"}
    renamed = MODULE.replace("def load_defaults():", "def load_settings():")
    repo = tmp_path / "blank"
    repo.mkdir()
    base = commit(repo, {"m.py": MODULE})
    head = commit(repo, {"m.py": renamed})
    diff = sh(repo, "git", "-c", "diff.suppressBlankEmpty=true", "diff", base, head)
    assert "\n\n-def load_defaults" in diff
    assert kinds(str(repo), head, diff) == {"load_defaults": "defined", "load_settings": "defined"}
    assert {t.pattern for t in usages.terms(diff)} == {"load_defaults", "load_settings"}


def test_type_aliases_shared_lines_and_old_side_limits(tmp_path, monkeypatch):
    """codex-5, codex-6."""
    import sys
    new = sys.version_info >= (3, 12)
    before = ("FIRST = 1; SECOND = 2\nclass Turn:\n    first_field: int = 1; second_field: int = 2\n"
              + ("type OldPayload = list[int]\ntype FirstAlias = int; type SecondAlias = str\n" if new else ""))
    after = ("FIRST = 3; SECOND = 2\nclass Turn:\n    first_field: int = 3; second_field: int = 2\n"
             + ("type NewPayload = dict[str, int]\ntype FirstAlias = bool; type SecondAlias = str\n" if new else ""))
    repo, head, diff = change(tmp_path, {"t.py": before}, {"t.py": after})
    got = kinds(repo, head, diff)
    assert {"FIRST", "SECOND"} <= set(got)
    assert got["first_field"] == got["second_field"] == "field"  # antigravity-2
    if new:
        assert {"OldPayload", "NewPayload", "FirstAlias", "SecondAlias"} <= set(got)
    # A deleted file's old side counts against the limits before it is parsed.
    repo, head, diff = change(tmp_path / "d", {"gone.py": MODULE}, {"gone.py": None})
    assert ("-", "gone.py") in usages.pinned_python(repo, head, diff)
    assert usages.pinned_python(repo, head, diff, budget=len(MODULE) - 1) == {}
    monkeypatch.setattr(usages, "MAX_SOURCE", len(MODULE) - 1)
    assert usages.pinned_python(repo, head, diff) == {}


def test_a_large_file_is_placed_in_linear_time(tmp_path):
    """codex-7: placing many changed lines doesn't scan every definition for each."""
    import time
    before = "".join(f"def func_{i}(x):\n    def local_{i}(y):\n        return y\n    return x\n\n\n"
                     for i in range(6000))
    after = before.replace("        return y\n", "        return y + 1\n")
    repo, head, diff = change(tmp_path, {"big.py": before}, {"big.py": after})
    scopes = usages.pinned_python(repo, head, diff)
    started = time.monotonic()
    found = usages.terms(diff, scopes=scopes)
    assert time.monotonic() - started < 5
    assert found and all(t.pattern.startswith("func_") for t in found)
    # codex-10: many statements on one line are indexed in linear time too.
    started = time.monotonic()
    one = usages.py_scopes("; ".join(f"VALUE_{i} = {i}" for i in range(40000)) + "\n")
    assert time.monotonic() - started < 5 and len(one.consts[1]) == 40000


def test_prose_and_comments_define_nothing():
    """#175: the prose that took four usage-search slots on #173.  Elsewhere than Python files
    and prose, definitions are matched as before, except on comment lines."""
    md = ("diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n@@ -1,1 +1,2 @@ ## Usage search\n"
          " text\n+A Python function defined inside another one (`self.queue.launch_count`) is not searched.\n")
    assert usages.terms(md) == []
    comments = ("diff --git a/a.js b/a.js\n--- a/a.js\n+++ b/a.js\n@@ -1,1 +1,7 @@ // a function defined here\n x\n"
                "+// a function defined inside another one\n+ * the struct defined below\n"
                "+# the function defined above\n+    *\n")
    assert usages.terms(comments) == []
    # codex-16: lines starting with a block comment, `#define` or `*/` match as before.
    base = ("diff --git a/b.js b/b.js\n--- a/b.js\n+++ b/b.js\n@@ -1,1 +1,5 @@\n x\n"
            "+/* one */ export function process_request() {}\n"
            "+/*! SPDX-License-Identifier: MIT */function routeRequest() {}\n"
            "+#define MAKE_TYPE(x) struct x##Type\n+*/ function afterBlock() {}\n")
    assert usages.symbols(base) == ["process_request", "routeRequest", "afterBlock"]
    # The cases the line matching handled before still match the same (codex-14, -15).
    c = ("diff --git a/s.h b/s.h\n--- a/s.h\n+++ b/s.h\n@@ -1,1 +1,9 @@\n x\n+typedef struct OpaqueState OpaqueState;\n"
         "+template <typename T, int N = (1 << 8)> class SmallBuffer;\n+template<class T>struct WideWord {\n"
         "+template <typename T = std::map<int, std::vector<int>>> class NestedCache;\n"
         "+export template <typename T> class ExportedCache;\n"
         "+template <class A> struct FirstPair; template <class B> struct SecondPair;\n"
         "+#[inline] pub fn render_preview() {}\n+template < limit; function afterLimit() {}\n")
    assert usages.symbols(c) == ["OpaqueState", "SmallBuffer", "WideWord", "NestedCache", "ExportedCache",
                                 "FirstPair", "SecondPair", "render_preview", "afterLimit"]
    rs = ("diff --git a/s.rs b/s.rs\n--- a/s.rs\n+++ b/s.rs\n@@ -1,1 +1,3 @@ trait ReviewAdapter {\n x\n"
          "+    fn invocation(&self) -> u32;\n+    pub(crate) unsafe fn raw_call(&self) {}\n")
    assert usages.symbols(rs) == ["invocation", "raw_call"]
    go = ("diff --git a/s.go b/s.go\n--- a/s.go\n+++ b/s.go\n@@ -1,1 +1,4 @@\n x\n"
          "+func (s *Server) ServeHTTP(w http.ResponseWriter) {}\n+type Handler func(int) error\n"
          "+type BufferPointer *[32]byte\n")
    assert usages.symbols(go) == ["ServeHTTP", "Handler", "BufferPointer"]
