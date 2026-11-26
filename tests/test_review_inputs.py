"""#161: usage search skips stdlib and generic names, and searches a generic name qualified as
the author wrote it; large new data files are summarized in a reviewer's diff."""
from agent_bus import usages
from agent_bus import config as cfg
from agent_bus.snapshot import Snapshot
from agent_bus.worker import DATA_HEAD, Worker, is_data_file, summarize_data_files


def by(diff, mention=None):
    return {t.pattern: t for t in usages.terms(diff, mention)}


def test_stdlib_and_generic_reads_are_dropped_before_the_cap():
    diff = ("diff --git a/l.py b/l.py\n--- a/l.py\n+++ b/l.py\n@@ -1 +1,4 @@\n"
            "+fcntl.flock(fd, fcntl.LOCK_SH | os.O_RDWR)\n+x = refs[\"repo\"] + self.queue.launch_count\n"
            + "".join(f"+y{i} = thing.generic_{i}\n" for i in range(3)))
    reads = [t.pattern for t in usages.terms(diff) if t.kind == "read"]
    assert "LOCK_SH" not in reads and "O_RDWR" not in reads and "repo" not in reads
    assert "launch_count" in reads and "generic_0" in reads  # codex-7: self.queue is no module


def test_mentions_keep_qualified_common_names():
    got = usages.mentioned(["`snapshot.ensure()` and `monkeypatch.undo()`, `os.path.join`, `Adapter.run()`, "
                            "`TurnResult.launched`, `repo`, `json.loads`, `fcntl.LOCK_SH`, `a.b`"], limit=None)
    assert got == ["snapshot.ensure", "monkeypatch.undo", "Adapter", "launched"]
    # codex-8: a short, skipped or qualified receiver keeps the call as written (its last two parts).
    assert usages.mentioned(["`db.close()`, `self.reset()`, `pkg.snapshot.ensure()`, `self.queue.launch_count`"],
                            limit=None) == ["db.close", "self.reset", "snapshot.ensure", "launch_count"]
    # codex-11: a receiver named like a less common stdlib module may be the project's own.
    assert usages.mentioned(["`queue.claim_next_job()`"], limit=None) == ["claim_next_job"]
    # antigravity-5: no builtins' methods, dunders or test names.
    assert usages.mentioned(["`str.split`, `self.__dict__`, `test_mod.test_case`, `dict.get()`"], limit=None) == []


def test_search_finds_qualified_names(env):
    from test_debrief_followups import git_repo, sh
    repo = env["repo"]
    git_repo(repo)
    (repo / "m.py").write_text("snapshot.ensure(a)\nensure(b)\nother.ensure(c)\n")
    sh(repo, "git", "add", "-A")
    sh(repo, "git", "commit", "-qm", "m")
    head = sh(repo, "git", "rev-parse", "HEAD")
    u = usages.search(str(repo), head, ["snapshot.ensure"])
    assert [ln for _, ln, _ in u.hits["snapshot.ensure"]] == [1]


# ------------------------------------------------------------ data files in the diff


def file_diff(path, n, new=True):
    head = f"diff --git a/{path} b/{path}\n" + ("new file mode 100644\n--- /dev/null\n" if new else f"--- a/{path}\n")
    return head + f"+++ b/{path}\n@@ -0,0 +1,{n} @@\n" + "".join(f"+  \"k{i}\": {i},\n" for i in range(n))


def snap(*files):
    return Snapshot(head="h" * 40, path="/copy", mode="sparse", files=sorted(files))


def test_large_new_data_files_are_summarized():
    code = ("diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n")
    big = file_diff("tests/data/streams/big.json", 385)
    small = file_diff("tests/data/small.json", 20)
    diff = code + big + small
    out = summarize_data_files(diff, snap("tests/data/streams/big.json", "tests/data/small.json", "src/a.py"))
    assert out.startswith(code) and out.endswith(small)
    section = out[len(code):-len(small)]
    assert "new file mode 100644" in section and "+++ b/tests/data/streams/big.json" in section
    assert ("[agent-bus: new data file tests/data/streams/big.json summarized: 385 lines" in section
            and "/copy/tests/data/streams/big.json]" in section)
    assert section.count("\n+  \"k") == DATA_HEAD and "[... 373 more added lines" in section
    # Code is never cut, however long; nor a file the copy lacks, nor a changed or deleted one (codex-1).
    long_code = file_diff("src/gen.py", 400)
    assert summarize_data_files(long_code, snap("src/gen.py")) == long_code
    assert summarize_data_files(big, snap()) == big
    changed = file_diff("tests/data/streams/big.json", 385, new=False)
    assert summarize_data_files(changed, snap("tests/data/streams/big.json")) == changed


def test_a_long_line_is_cut_in_the_preview():
    """codex-2: a minified fixture's one huge line doesn't fill the preview."""
    def one_line(n):
        return ("diff --git a/f.json b/f.json\nnew file mode 100644\n--- /dev/null\n+++ b/f.json\n@@ -0,0 +1 @@\n+"
                + "x" * n + "\n")
    out = summarize_data_files(one_line(30_000), snap("f.json"))
    assert len(out) < 1_000 and "[... line cut]" in out
    assert "more added lines" not in out and not out.endswith("\n\n")  # antigravity-4: all shown
    # codex-1: a line read_file can't return whole stays inline.
    assert summarize_data_files(one_line(50_000), snap("f.json")) == one_line(50_000)


def test_the_reviewers_diff_is_summarized_then_capped(env):
    """codex-9: the file list keeps every line; the summary frees room for later files."""
    w = Worker.__new__(Worker)
    w.config = cfg.Config.load()
    w.config.max_diff_bytes = 3_000
    code = "diff --git a/src/z.py b/src/z.py\n--- a/src/z.py\n+++ b/src/z.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"
    d = w.cap_diff(file_diff("tests/data/a.json", 385) + code)
    assert "+x = 2" not in d and "385" in d.overview  # cut before the code file
    short = w.summarize_data(d, snap("tests/data/a.json", "src/z.py"))
    assert "+x = 2" in short and short.overview == d.overview and "+385" in short.overview
    assert w.summarize_data(d, Snapshot(head="h" * 40)) is d  # no copy: nothing to point at


def test_data_file_paths():
    assert is_data_file("tests/data/x.jsonl") and is_data_file("flake.lock") and is_data_file("a/fixtures/b.golden")
    assert not is_data_file("src/agent_bus/worker.py") and not is_data_file("Makefile")
    # antigravity-1: code among fixtures is code, and a snapshots/ package isn't data.
    assert not is_data_file("tests/data/streams/scrub.py") and not is_data_file("src/app/snapshots/store.txt.py")
    assert not is_data_file("src/app/snapshots/notes")
