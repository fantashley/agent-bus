"""#196, #202: an outline of the long changed Python files and a usages block for a discussion,
for a peer that can't search; builtin types' methods are generic words."""
import subprocess

from agent_bus import prompts, snapshot, usages
from agent_bus.db import HUMAN
from agent_bus.service import Service

from test_review_rounds import calls, git_repo, run_job
from test_usage_defs import change


def _long(name: str, n: int = 100) -> str:
    """A Python file over usages.LONG_FILE characters: a function, a class with two methods,
    then filler functions."""
    head = (f"import os\n\n\ndef {name}(x):\n    return x\n\n\n@decorated\nclass Box:\n    size: int = 0\n\n"
            "    def open(self):\n        return 1\n\n    async def shut(self):\n        return 2\n")
    body = "".join(f"\n\ndef filler_{i}():\n    return '{'x' * 400}'\n" for i in range(n))
    return head + body


def test_a_long_changed_file_gets_an_outline_and_a_short_one_does_not(tmp_path):
    long = _long("first")
    assert len(long) > usages.LONG_FILE
    repo, head, diff = change(tmp_path, {"src/big.py": long, "src/small.py": "def tiny():\n    return 1\n"},
                              {"src/big.py": long.replace("return x\n", "return x + 1\n", 1),
                               "src/small.py": "def tiny():\n    return 2\n"})
    text, _, outside = usages.note(repo, head, diff)
    assert not outside
    out = text.split("Outline of the changed files too long to read whole")[1]
    line = next(x for x in out.split("\n") if x.startswith("  src/big.py: "))
    # Top-level definitions with their ranges (a decorator included), a class's methods in parentheses.
    assert line.startswith("  src/big.py: first 4-5; Box 8-16 (open 12-13, shut 15-16); filler_0 19-20; ")
    assert "small.py" not in out and text.endswith(usages.CLOSE)
    # With no copy of the head, it is marked as the other lines are.
    text, _, outside = usages.note(repo, head, diff, readable=frozenset())
    assert outside and "  src/big.py (not in the copy): first 4-5;" in text


def test_the_outline_is_capped_test_files_last(tmp_path, monkeypatch):
    files = {"tests/test_big.py": _long("t"), "src/a.py": _long("a"), "src/b.py": _long("b", 150)}
    repo, head, diff = change(tmp_path, files, {p: t.replace("return x\n", "return x + 1\n", 1)
                                                for p, t in files.items()})
    scopes = usages.pinned_python(repo, head, diff)
    lines, done = usages.outline(diff, scopes)
    assert done == ["src/a.py", "src/b.py", "tests/test_big.py"]
    assert all("more definitions" not in line for line in lines)
    # codex-4 of #211: a file with too many definitions for the room gets as many as fit, and a
    # count of the rest; the files with no room for any are named; all within OUTLINE_CHARS.
    head = len(lines[0]) + 1
    monkeypatch.setattr(usages, "OUTLINE_CHARS", head + len(lines[1]) + 1 + usages.COMPACT + 100)
    lines, done = usages.outline(diff, scopes)
    assert done == ["src/a.py", "src/b.py"] and sum(len(x) + 1 for x in lines) <= usages.OUTLINE_CHARS
    assert "more definitions" not in lines[1]
    assert lines[2].startswith("  src/b.py: b 4-5; Box 8-16 (open 12-13, shut 15-16); filler_0 19-20")
    assert lines[2].endswith(" [+149 more definitions]")
    assert lines[-1] == "[... no room for the outline of 1 more file: tests/test_big.py]"
    # Cut inside or just after a class's methods: the list is closed.
    monkeypatch.setattr(usages, "OUTLINE_CHARS", head + usages.COMPACT + 80)
    lines, done = usages.outline(diff, scopes)
    assert lines[1] == "  src/a.py: a 4-5; Box 8-16 (open 12-13, shut 15-16) [+100 more definitions]"
    assert lines[2] == "[... no room for the outline of 2 more files: src/b.py, tests/test_big.py]"
    monkeypatch.setattr(usages, "OUTLINE_CHARS", head + usages.COMPACT + 72)
    lines, done = usages.outline(diff, scopes)
    assert lines[1].startswith("  src/a.py: a 4-5; Box 8-16 (open 12-13) [+")


def test_builtin_type_methods_are_generic():
    # #207's debrief: a removed `.rstrip()` used a MAX_REMOVED slot.
    assert usages.mentioned(["`rstrip`, `lstrip`, `setdefault`, `fromkeys`, `drop_usage`"]) == ["drop_usage"]


def _discussion(env, question, evidence=None):
    bus = env["bus"]
    tid = Service(bus, HUMAN).start_discussion(question, peers=["codex", "antigravity"], repo=str(env["repo"]),
                                               evidence=evidence)["thread"]["id"]
    run_job(bus, tid)
    by = {c["agent"]: c["prompt"] for c in calls(env)}
    return by["antigravity"], by["codex"]


def test_a_discussion_shows_where_its_backticked_names_appear(env, no_spawn):
    """#202: a discussion peer with no search gets the usages of the names in backticks in the
    question and the evidence, at the checked-out commit."""
    repo = env["repo"]
    git_repo(repo)
    (repo / "src").mkdir()
    (repo / "src" / "mod.py").write_text("def drop_usage(x):\n    return x\n\n\ndef caller():\n    return drop_usage(1)\n"
                                         "\n\ndef from_evidence():\n    return 2\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-qm", "m"], cwd=repo, check=True)
    rev = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    agy, codex = _discussion(env, "Should `drop_usage()` go?",
                             evidence=[{"title": "Note", "text": "See `from_evidence`, and `cfg.from()`."}])
    assert f"agent-bus searched commit {rev[:12]} (the repository's checked-out commit" in agy
    assert "drop_usage (2 hits in 1 file):\n  src/mod.py:1: def drop_usage(x):\n  src/mod.py:6:" in agy
    assert "from_evidence (1 hit in 1 file):\n  src/mod.py:9: def from_evidence():" in agy
    assert "named in the thread's findings" not in agy and "\nfrom (" not in agy
    # Where they are defined too (codex-3 of #211).
    assert "Where these names are defined:\n  drop_usage: src/mod.py:1\n  from_evidence: src/mod.py:9\n" in agy
    # Codex can search: no such block.
    assert "agent-bus searched commit" not in codex
    # Nothing in backticks: none.
    agy, _ = _discussion(env, "Which file?")
    assert "agent-bus searched commit" not in agy


def _repo_with_a_name(env):
    repo = env["repo"]
    git_repo(repo)
    (repo / "m.py").write_text("def drop_usage():\n    return 1\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-qm", "m"], cwd=repo, check=True)
    return repo


def _usage_events(env):
    return [r["text"] for r in env["bus"]._all("SELECT text FROM events WHERE kind = 'usages'")]


def test_a_partial_clone_gets_no_discussion_search(env, no_spawn):
    """codex-1 of #211: git grep would fetch a partial clone's missing blobs."""
    repo = _repo_with_a_name(env)
    agy, _ = _discussion(env, "Should `drop_usage()` go?")
    assert "agent-bus searched commit" in agy
    # Any promisor or filter setting, whatever its value (codex-1 of #211).
    subprocess.run(["git", "config", "remote.origin.partialclonefilter", "blob:none"], cwd=repo, check=True)
    agy, _ = _discussion(env, "Should `drop_usage()` go?")
    assert "agent-bus searched commit" not in agy and "file list]" in agy
    assert any(e.startswith("no usage search for the discussion: a partial clone") for e in _usage_events(env))


def test_a_slow_discussion_search_does_not_hold_up_the_turns(env, no_spawn, monkeypatch):
    """codex-2 and codex-8 of #211: every peer's first turn waits for the search, so its git
    calls get little time in all, and each is killed when that is up."""
    import time

    from agent_bus import worker
    repo = _repo_with_a_name(env)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    with usages.time_limit(0):
        assert usages.search(str(repo), head, ["drop_usage"]) is None
    assert usages.search(str(repo), head, ["drop_usage"]).hits["drop_usage"]  # no limit outside
    # A git call that runs past the limit is killed.
    real = subprocess.Popen
    with monkeypatch.context() as m:
        m.setattr(usages.subprocess, "Popen", lambda *a, **k: real(
            ["sleep", "30"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL))
        start = time.monotonic()
        with usages.time_limit(0.3):
            assert usages.search(str(repo), head, ["drop_usage"]) is None
        assert time.monotonic() - start < 5
    # The checks before the search are within the limit too (codex-9 of #211).
    calls = []
    real_check = snapshot.partial_clone
    monkeypatch.setattr(snapshot, "partial_clone", lambda repo, timeout=10: calls.append(timeout)
                        or real_check(repo, timeout))
    monkeypatch.setattr(worker, "DISCUSSION_SEARCH_S", 0.0)
    agy, codex = _discussion(env, "Should `drop_usage()` go?")
    assert "agent-bus searched commit" not in agy and codex and calls and calls[0] <= 0
    assert ("no usage search for the discussion: a partial clone, or its git configuration could not be read "
            "in time") in _usage_events(env)
    monkeypatch.setattr(worker, "DISCUSSION_SEARCH_S", 5.0)
    agy, _ = _discussion(env, "Should `drop_usage()` go?")
    assert "agent-bus searched commit" in agy and 0 < calls[-1] <= 5


def test_a_qualified_name_is_defined_by_its_last_part_its_qualifiers_file_first(env, no_spawn):
    """codex-3 of #211: `snapshot.ensure()` is searched as written, and its definition found."""
    repo = env["repo"]
    git_repo(repo)
    (repo / "a.py").write_text("def ensure():\n    return 0\n")
    (repo / "snapshot.py").write_text("def ensure():\n    return 1\n")
    (repo / "b.py").write_text("import snapshot\nsnapshot.ensure()\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-qm", "m"], cwd=repo, check=True)
    agy, _ = _discussion(env, "Where is `snapshot.ensure()`?")
    assert "snapshot.ensure (1 hit in 1 file):\n  b.py:2: snapshot.ensure()" in agy
    assert "Where these names are defined:\n  ensure: snapshot.py:1, a.py:1\n" in agy


def test_resumed_follow_ups_keep_a_changed_severity_in_full():
    """codex-5 of #211: under self_hosted_prompts, a head whose SEVERITY differs from the
    installed one gets it in full, not the installed brief."""
    from agent_bus import headprompts
    from agent_bus.worker import severity_brief
    assert severity_brief() == prompts.SEVERITY_BRIEF
    with headprompts.using(headprompts.Active(texts={"SEVERITY": "Changed rules."})):
        assert severity_brief() == "Changed rules."
    with headprompts.using(headprompts.Active(texts={"SEVERITY": prompts.SEVERITY})):
        assert severity_brief() == prompts.SEVERITY_BRIEF
