"""#196: a resumed follow-up gets a long file's outline again only where it changed since the
outline its session was given; a fresh prompt keeps the whole outline.  #202: the discussion
usages block says the definitions come at the end."""
import json
import os
import subprocess
from pathlib import Path

from agent_bus import prompts, usages
from agent_bus.service import Service

from agent_bus.worker import Worker

from test_prompt_polish_2 import _long
from test_review_rounds import calls, git_repo
from test_usage_defs import change, commit


def _outline(text):
    return usages.py_scopes(text).outline


def test_the_delta_lists_moved_runs_resized_new_and_removed_definitions():
    old = (("a", 1, 2, ()), ("Box", 4, 9, (("open", 5, 6), ("shut", 8, 9))), ("b", 11, 12, ()), ("c", 14, 15, ()))
    assert usages._outline_delta(old, old) == ""
    # Three lines added in Box.open: it and Box grow, everything after moves down 3.
    new = (("a", 1, 2, ()), ("Box", 4, 12, (("open", 5, 9), ("shut", 11, 12))), ("b", 14, 15, ()), ("c", 17, 18, ()))
    assert usages._outline_delta(old, new) == "Box 4-12; Box.open 5-9; Box.shut..c +3"
    # b removed, d added: a run stops at either.
    new = (("a", 1, 2, ()), ("Box", 4, 9, (("open", 5, 6), ("shut", 8, 9))), ("c", 11, 12, ()), ("d", 14, 20, ()))
    assert usages._outline_delta(old, new) == "b (removed); c -3; d 14-20 (new)"
    # codex-2: a name twice (a property's getter and setter) would be ambiguous: no delta.
    dup = (("Box", 1, 9, (("x", 2, 4), ("x", 5, 7))),)
    assert usages._outline_delta(dup, (("Box", 1, 10, (("x", 2, 4), ("x", 6, 8))),)) == ""


def test_a_delta_that_isnt_cheaper_or_doesnt_fit_gives_the_outline_and_notices_count(monkeypatch):
    """codex-4, codex-5: the delta's label and legend count; the notices are in the budget."""
    sc = usages.PyScopes({}, [None], {}, {}, outline=(("a", 1, 2, ()), ("b", 4, 5, ())), chars=50_000)
    moved = usages.PyScopes({}, [None], {}, {}, outline=(("a", 1, 2, ()), ("b", 5, 6, ())), chars=50_000)
    diff = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n"
    lines, _ = usages.outline(diff, {("+", "x.py"): moved}, seen={"x.py": ("h" * 40, sc.outline)})
    assert lines[1] == "  x.py: a 1-2; b 5-6" and usages.OUTLINE_DELTA not in lines[0]
    # Many unchanged files: their notice is counted, so the total stays within OUTLINE_CHARS.
    files = {f"src/{'d' * 30}{i}.py": sc for i in range(40)}
    diff = "".join(f"diff --git a/{p} b/{p}\n--- a/{p}\n+++ b/{p}\n@@ -1 +1 @@\n-a\n+b\n" for p in files)
    monkeypatch.setattr(usages, "OUTLINE_CHARS", 1_500)
    seen = {p: ("h" * 40, sc.outline) for p in list(files)[:30]}
    lines, done = usages.outline(diff, {("+", p): s for p, s in files.items()}, seen=seen)
    assert sum(len(x) + 1 for x in lines) <= 1_500 and len(done) >= 30
    assert lines[-1] == "Same as the outline you were given, so not repeated: 30 files (no room to name them)"
    assert lines[-2].startswith("  src/") and len(lines) == 12
    # codex-8: the new outlines get room first; the unchanged files are named in what is left.
    monkeypatch.setattr(usages, "OUTLINE_CHARS", 8_000)
    tests = {f"tests/test_{'t' * 30}{i}.py": sc for i in range(30)}
    big = usages.PyScopes({}, [None], {}, {}, outline=tuple((f"f{i}", i * 3 + 1, i * 3 + 2, ()) for i in range(160)),
                          chars=50_000)
    allf = {**tests, "src/one.py": big, "src/two.py": big}
    diff = "".join(f"diff --git a/{p} b/{p}\n--- a/{p}\n+++ b/{p}\n@@ -1 +1 @@\n-a\n+b\n" for p in allf)
    lines, _ = usages.outline(diff, {("+", p): s for p, s in allf.items()},
                              seen={p: ("h" * 40, sc.outline) for p in tests})
    assert [x.split(":")[0] for x in lines[1:3]] == ["  src/one.py", "  src/two.py"]
    assert all("more definitions" not in x for x in lines) and sum(len(x) + 1 for x in lines) <= 8_000
    assert lines[3].startswith("Same as the outline you were given, so not repeated: tests/")
    assert lines[3].endswith(f" (at head {'h' * 12})")
    # antigravity-2: unchanged files from several heads stay within the cap, whatever it is.
    for cap in range(1_000, 2_400, 7):
        monkeypatch.setattr(usages, "OUTLINE_CHARS", cap)
        lines, _ = usages.outline(diff, {("+", p): s for p, s in allf.items()},
                                  seen={p: (f"{i % 4:040x}", sc.outline) for i, p in enumerate(tests)})
        assert sum(len(x) + 1 for x in lines) <= cap
        assert sum(x.startswith("Same as the outline") for x in lines) == 1
    # codex-8 again: two unchanged files from two heads take no room from the new outlines; with
    # just enough room for them all, everything is shown in full.
    two = dict(list(tests.items())[:2], **{"src/one.py": big, "src/two.py": big})
    diff = "".join(f"diff --git a/{p} b/{p}\n--- a/{p}\n+++ b/{p}\n@@ -1 +1 @@\n-a\n+b\n" for p in two)
    seen = {p: (f"{i:040x}", sc.outline) for i, p in enumerate(list(tests)[:2])}
    monkeypatch.setattr(usages, "OUTLINE_CHARS", 100_000)
    lines, _ = usages.outline(diff, {("+", p): s for p, s in two.items()}, seen=seen)
    need = sum(len(x) + 1 for x in lines)
    monkeypatch.setattr(usages, "OUTLINE_CHARS", need + usages.COMPACT - 150)
    tight, _ = usages.outline(diff, {("+", p): s for p, s in two.items()}, seen=seen)
    assert tight == lines and "more definitions" not in "".join(tight)


def test_a_resumed_note_shows_only_what_changed_in_the_outlines(tmp_path):
    before = {f"src/f{i}.py": _long(f"first{i}") for i in range(2)}
    mid = {p: t.replace("return x\n", "return x + 1\n", 1) for p, t in before.items()}
    repo, h1, diff1 = change(tmp_path, before, mid)
    known = {}
    text, _, _ = usages.note(repo, h1, diff1, known=known)
    assert known == {"src/f0.py": h1, "src/f1.py": h1}
    assert "  src/f0.py: first0 4-5; Box 8-16 (open 12-13, shut 15-16); filler_0 19-20; " in text
    # The next head: f0's first function gains two lines, f1 changes inside a line.
    after = {"src/f0.py": mid["src/f0.py"].replace("return x + 1\n", "y = 1\n    z = 2\n    return x + y\n", 1),
             "src/f1.py": mid["src/f1.py"].replace("return x + 1\n", "return x + 2\n", 1)}
    h2 = commit(Path(repo), after)
    diff2 = subprocess.run(["git", "diff", h1, h2], cwd=repo, capture_output=True, text=True).stdout
    full, _, _ = usages.note(repo, h2, diff2)
    known = {}
    text, _, _ = usages.note(repo, h2, diff2, seen={"src/f0.py": h1, "src/f1.py": h1}, known=known)
    assert known == {"src/f0.py": h2, "src/f1.py": h2}
    assert f"  src/f0.py (changes since your outline at head {h1[:12]}): first0 4-7; Box..filler_99 +2\n" in text
    assert f"Same as the outline you were given, so not repeated: src/f1.py (at head {h1[:12]})\n" in text
    assert usages.OUTLINE_DELTA in text and "filler_5 " not in text
    assert len(text) < len(full) - 2_000
    # A file the session wasn't given, or whose earlier head can't be read, gets it whole.
    text, _, _ = usages.note(repo, h2, diff2, seen={"src/f0.py": "0" * 40})
    assert text == full


def _sh(repo, *argv):
    genv = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
                GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")
    return subprocess.run(argv, cwd=repo, check=True, capture_output=True, text=True, env=genv).stdout.strip()


def test_a_resumed_follow_up_repeats_no_unchanged_outline_and_a_fresh_one_does(env, no_spawn, monkeypatch):
    bus, repo = env["bus"], env["repo"]
    git_repo(repo)
    (repo / "big.py").write_text(_long("first"))
    _sh(repo, "git", "add", "-A")
    _sh(repo, "git", "commit", "-qm", "big")
    base = _sh(repo, "git", "rev-parse", "HEAD")

    def edit(old, new):
        (repo / "big.py").write_text((repo / "big.py").read_text().replace(old, new, 1))
        _sh(repo, "git", "commit", "-qam", "e")
        return _sh(repo, "git", "rev-parse", "HEAD")
    h1 = edit("return x\n", "return x + 1\n")
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review("antigravity", str(repo), base, h1)["thread"]["id"]
    Worker(bus, bus.jobs(tid)[-1]["id"]).run()
    first = calls(env)[-1]["prompt"]
    assert "  big.py: first 4-5; Box 8-16 (open 12-13, shut 15-16); filler_0 19-20;" in first
    def record():
        rec = json.loads([e["text"] for e in bus.events(tid) if e["kind"] == "outline"][-1])
        assert rec["session"] == bus.peer_session(tid, "antigravity")
        return rec["heads"]
    assert record() == {h1: ["big.py"]}
    # A follow-up that edits a line in place: the outline is the same, so it is only named.
    h2 = edit("return 1\n", "return 3\n")
    claude.request_review("antigravity", str(repo), base, h2, prev_head_commit_id=h1)
    Worker(bus, bus.jobs(tid)[-1]["id"]).run()
    second = calls(env)[-1]
    assert "--conversation" in second["argv"]
    assert f"Same as the outline you were given, so not repeated: big.py (at head {h1[:12]})" in second["prompt"]
    assert "filler_0 19-20" not in second["prompt"]
    assert record() == {h2: ["big.py"]}
    # One that moves definitions: only the changes.
    h3 = edit("return 3\n", "a = 3\n        return a\n")
    claude.request_review("antigravity", str(repo), base, h3, prev_head_commit_id=h2)
    Worker(bus, bus.jobs(tid)[-1]["id"]).run()
    third = calls(env)[-1]["prompt"]
    assert f"  big.py (changes since your outline at head {h2[:12]}): Box 8-17; Box.open 12-14; Box.shut..filler_99 +1" in third
    # A session that can't be resumed starts fresh: the whole outline, and the record starts over.
    monkeypatch.setenv("FAKE_RESUME_FAILS", "antigravity")
    h4 = edit("return 2\n", "return 4\n")
    claude.request_review("antigravity", str(repo), base, h4, prev_head_commit_id=h3)
    Worker(bus, bus.jobs(tid)[-1]["id"]).run()
    fourth = calls(env)[-1]
    assert "--conversation" not in fourth["argv"]
    assert "  big.py: first 4-5; Box 8-17 (open 12-14, shut 16-17); filler_0 20-21;" in fourth["prompt"]
    assert "changes since your outline" not in fourth["prompt"] and "Same as the outline" not in fourth["prompt"]
    assert record() == {h4: ["big.py"]}
    # codex-1: a record from a session that is no longer the saved one counts for nothing.
    w = Worker(bus, bus.jobs(tid)[-1]["id"])
    assert w.outlined_baseline("antigravity") == {"big.py": h4}
    bus.set_peer_session(tid, "antigravity", "another-session")
    assert w.outlined_baseline("antigravity") == {}


def test_the_record_drops_files_rather_than_itself_when_too_long(env, no_spawn):
    """codex-6: past an event's 2,000 characters some files are left out, not every one."""
    bus = env["bus"]
    claude = Service(bus, "claude", bus.register_session("claude"))
    repo = env["repo"]
    base, commit_ = git_repo(repo)
    tid = claude.request_review("antigravity", str(repo), base, commit_("x\n"))["thread"]["id"]
    w = Worker(bus, bus.jobs(tid)[-1]["id"])
    w.outlines_given = {"fresh": {f"src/agent_bus/{'m' * 25}{i}.py": f"{i:040x}" for i in range(40)}}
    w.record_outlines({"reviewer": "antigravity"}, None, True, "s1")
    text = [e["text"] for e in bus.events(tid) if e["kind"] == "outline"][-1]
    rec = json.loads(text)
    assert len(text) <= 2000 and rec["session"] == "s1" and 10 < sum(map(len, rec["heads"].values())) < 40

def test_the_discussion_usages_say_the_definitions_come_last():
    assert "then, at the end, where they are defined" in prompts.DISCUSSION_USAGES
