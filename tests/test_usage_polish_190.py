"""#190: usage search polish deferred from #189."""
import shutil
import subprocess

import pytest

from agent_bus import snapshot, usages

from test_review_rounds import git_repo


def test_a_common_names_first_lines_come_from_its_busiest_files_outside_tests():
    # antigravity-3: the sample used to spend its room on the first paths (README.md, docs/).
    hits = ([("README.md", 1, "x"), ("docs/a.md", 2, "x")]
            + [("src/busy.py", i, "x") for i in range(10, 13)]
            + [("tests/test_busier.py", i, "x") for i in range(20, 30)])
    line = usages._lines_at(hits, lambda p: p, busiest=True)
    assert line == "src/busy.py:10,11,12; README.md:1; docs/a.md:2; tests/test_busier.py:20,21,22,23,24,25,26,27,28,29"
    # Without it, path order (outside tests first), as for a name that is listed by line.
    assert usages._lines_at(hits, lambda p: p).startswith("README.md:1; docs/a.md:2; src/busy.py:10")
    many = {"common": [(f"docs/d{i}.md", i, "x") for i in range(60)] + [("src/m.py", i, "x") for i in range(50)]}
    text = usages.render(usages.Usages(names=["common"], hits=many, terms={"common": usages.Term("common")}))
    assert "; first at src/m.py:0,1,2," in text


def test_language_keywords_are_not_searched_as_mentioned_names():
    # Debrief of #189: a backticked `break` was searched (44 hits).
    assert usages.mentioned(["`break` here, `continue` there, `await x`, `lambda`, `drop_usage()`"]) == ["drop_usage"]
    # codex-1: only a bare keyword; other names, a qualified one and the diff's own are kept.
    # #194: and a keyword as a dotted mention's last part, which _KEYWORDS must not cover; but
    # #196's debrief: such a mention (`cfg.from()`, 1,194 hits) is skipped, as is a builtin's name.
    assert (usages.mentioned(["`static.ensure()`, `router.switch()`, `switch`, `break`, `obj.from`, `cfg.compile()`"])
            == ["static.ensure", "switch"])
    diff = ("diff --git a/src/r.py b/src/r.py\n--- a/src/r.py\n+++ b/src/r.py\n@@ -1,0 +1,3 @@\n"
            "+class Router:\n+    static: int = 0\n+    def switch(self): return options[\"from\"]\n")
    found = {t.pattern for t in usages.terms(diff)}
    assert {"switch", "static", "from"} <= found  # a key read that is a keyword too (#194)


def test_a_backslash_in_a_file_name_is_told_apart_from_an_escaped_line_feed():
    # codex-8: a line feed and the literal text backslash-x0a were both shown as \x0a.
    assert snapshot.escaped("a\nb") == "a\\x0ab"
    assert snapshot.escaped("a\\x0ab") == "a\\\\x0ab"
    assert snapshot.escaped("a\udcffb") == "a\\xffb"


def test_a_jj_repository_is_listed_without_reading_git_head(env, monkeypatch):
    # antigravity-4: rev-parse ran in a jj repo too, and without GIT_NO_LAZY_FETCH.
    if shutil.which("jj") is None:
        pytest.skip("jj not installed")
    repo = env["repo"]
    _, commit = git_repo(repo)
    commit("one\n")
    subprocess.run(["jj", "git", "init", "--colocate"], cwd=repo, check=True, capture_output=True)
    seen = []
    real = snapshot._git

    def spy(r, e, *argv):
        seen.append((argv[0], e.get("GIT_NO_LAZY_FETCH")))
        return real(r, e, *argv)
    monkeypatch.setattr(snapshot, "_git", spy)
    assert snapshot.tracked(str(repo)) is not None
    assert seen == [("ls-tree", "1")]
    seen.clear()
    shutil.rmtree(repo / ".jj")
    assert snapshot.tracked(str(repo)) is not None
    assert seen == [("rev-parse", "1"), ("ls-tree", "1")]
