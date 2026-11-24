"""#111: every reviewer reads files exactly as of the pinned head, from a read-only snapshot."""
import json
import os
import stat
import subprocess

import pytest

from agent_bus import config as cfg
from agent_bus import prompts, snapshot
from agent_bus.db import HUMAN
from agent_bus.peers import adapter
from agent_bus.service import Service
from agent_bus.worker import Worker

from test_review_rounds import git_repo


def sh(repo, *argv):
    genv = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
                GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")
    return subprocess.run(argv, cwd=repo, check=True, capture_output=True, text=True, env=genv).stdout.strip()


def calls(env):
    return [json.loads(line) for line in env["log"].read_text().splitlines()] if env["log"].exists() else []


def layout(repo):
    """A repo whose head differs from its working copy, with an ignored directory, a
    subdirectory, an executable, and symlinks in and out of the tree."""
    base, commit = git_repo(repo)
    (repo / ".gitignore").write_text("node_modules/\n")
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("print('head')\n")
    (repo / "src" / "other.py").write_text("x = 1\n")
    (repo / "docs").mkdir()
    (repo / "docs" / "guide.md").write_text("guide\n")
    (repo / "run.sh").write_text("#!/bin/sh\n")
    (repo / "run.sh").chmod(0o755)
    os.symlink("src/app.py", repo / "link-in")
    os.symlink("/etc/hostname", repo / "link-out")
    os.symlink("../../outside", repo / "src" / "link-up")
    sh(repo, "git", "add", "-A")
    sh(repo, "git", "commit", "-qm", "layout")
    head = sh(repo, "git", "rev-parse", "HEAD")
    # The working copy moves on, and has ignored and untracked files.
    (repo / "src" / "app.py").write_text("print('working copy')\n")
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "dep.js").write_text("x\n")
    (repo / "untracked.txt").write_text("u\n")
    return base, head


def test_snapshot_is_exactly_the_head_and_read_only(env):
    repo = env["repo"]
    base, head = layout(repo)
    snap = snapshot.ensure(str(repo), "t-abc", base, head, 10**9)
    root = snapshot.root() / "t-abc" / head
    assert snap.mode == "full" and snap.path == str(root)
    assert (root / "src" / "app.py").read_text() == "print('head')\n"
    assert not (root / "node_modules").exists() and not (root / "untracked.txt").exists()
    assert os.readlink(root / "link-in") == "src/app.py"  # in the tree: kept
    assert not os.path.lexists(root / "link-out") and not os.path.lexists(root / "src" / "link-up")
    assert snap.skipped == 2
    assert os.access(root / "run.sh", os.X_OK) and not os.access(root / "src" / "app.py", os.X_OK)
    for p in (root, root / "src", root / "src" / "app.py"):
        assert not stat.S_IMODE(os.stat(p).st_mode) & 0o222, p
    with pytest.raises(PermissionError):
        (root / "src" / "new.txt").write_text("no")
    assert "src/app.py" in snap.files and "link-in" in snap.files and "link-out" not in snap.files
    assert snap.changed == [".gitignore", "docs/guide.md", "link-in", "link-out", "run.sh", "src/app.py",
                            "src/link-up", "src/other.py"]
    assert snap.tree_files == len(snap.files) + 2
    # Outside the repository, under agent-bus's own state.
    assert str(cfg.state_dir()) in snap.path and str(repo) not in snap.path


def test_snapshot_is_reused_then_replaced_for_a_new_head(env, monkeypatch):
    repo = env["repo"]
    base, commit = git_repo(repo)
    h1 = commit("one\n")
    first = snapshot.ensure(str(repo), "t-abc", base, h1, 10**9)
    with monkeypatch.context() as m:
        m.setattr(snapshot, "_write", lambda *a: pytest.fail("rebuilt a reused snapshot"))
        again = snapshot.ensure(str(repo), "t-abc", base, h1, 10**9)
    assert again.path == first.path and again.files == first.files and again.mode == "full"
    h2 = commit("two\n")
    second = snapshot.ensure(str(repo), "t-abc", base, h2, 10**9)
    assert (snapshot.root() / "t-abc" / h2 / "a.txt").read_text() == "two\n"
    assert sorted(p.name for p in (snapshot.root() / "t-abc").iterdir()) == sorted([h2, f"{h2}.json"])
    assert not os.path.exists(first.path) and second.path != first.path


def test_snapshot_rebuilds_after_a_crash(env):
    repo = env["repo"]
    base, commit = git_repo(repo)
    head = commit("one\n")
    tdir = snapshot.root() / "t-abc"
    (tdir / ".tmp-leftover" / "sub").mkdir(parents=True)
    (tdir / ".tmp-leftover").chmod(0o555)
    (tdir / head).mkdir()  # a snapshot directory without its record: not trusted
    snap = snapshot.ensure(str(repo), "t-abc", base, head, 10**9)
    assert snap.mode == "full" and (tdir / head / "a.txt").read_text() == "one\n"
    assert not (tdir / ".tmp-leftover").exists()


def test_over_the_cap_a_sparse_snapshot_then_none(env):
    repo = env["repo"]
    base, _ = git_repo(repo)
    (repo / "big").mkdir()
    (repo / "big" / "blob.bin").write_bytes(b"x" * 5000)
    (repo / "src").mkdir()
    (repo / "src" / "a.py").write_text("a\n")
    sh(repo, "git", "add", "-A")
    sh(repo, "git", "commit", "-qm", "big")
    mid = sh(repo, "git", "rev-parse", "HEAD")
    (repo / "src" / "a.py").write_text("changed\n")
    (repo / "src" / "b.py").write_text("b\n")
    sh(repo, "git", "add", "-A")
    sh(repo, "git", "commit", "-qm", "change")
    head = sh(repo, "git", "rev-parse", "HEAD")
    snap = snapshot.ensure(str(repo), "t-abc", mid, head, 1000)
    assert snap.mode == "sparse" and snap.files == ["src/a.py", "src/b.py"] and snap.tree_files == 4
    assert not (snapshot.root() / "t-abc" / head / "big").exists()
    note = prompts.SNAPSHOT_SPARSE.format(n=2, total=4)
    assert "2 of the head's 4 files" in note
    snap = snapshot.ensure(str(repo), "t-def", mid, head, 5)
    assert snap.path is None and snap.mode == "over_cap" and "size cap" in snap.reason
    snap = snapshot.ensure(str(repo), "t-ghi", mid, head, 0)
    assert snap.path is None and "turned off" in snap.reason
    assert not (snapshot.root() / "t-ghi").exists()


def test_bad_ids_and_unreadable_repos_give_no_snapshot(env, tmp_path):
    repo = env["repo"]
    base, commit = git_repo(repo)
    head = commit("one\n")
    assert snapshot.ensure(str(repo), "../x", base, head, 10**9).path is None
    assert snapshot.ensure(str(repo), "t-abc", base, "../" + head, 10**9).path is None
    snap = snapshot.ensure(str(repo), "t-abc", base, "f" * 40, 10**9)
    assert snap.path is None and snap.mode == "failed" and "could not read" in snap.reason
    plain = tmp_path / "plain"
    plain.mkdir()
    assert snapshot.ensure(str(plain), "t-abc", base, head, 10**9).path is None


def test_listing_is_capped_and_keeps_the_changed_directories(env):
    files = [f"vendor/pkg{i}/file{j}.go" for i in range(40) for j in range(10)] + ["README.md", "src/app.py",
                                                                                   "src/util.py"]
    snap = snapshot.Snapshot(head="h", path="/x", mode="full", files=sorted(files), changed=["src/app.py"])
    full = snapshot.listing(snap, limit=10**6)
    assert full.splitlines() == sorted(files)
    short = snapshot.listing(snap, limit=2000)
    assert len(short) < 2400
    lines = short.splitlines()
    assert lines[:3] == ["README.md", "src/app.py", "src/util.py"]
    assert any("more files not listed" in line for line in lines)
    assert "vendor/pkg0/ (10 files)" in short and "src/ (2 files)" in short


# ------------------------------------------------------------------ the review turns


def test_every_reviewer_reads_the_snapshot(env, no_spawn, monkeypatch):
    """Claude, agy and Copilot get the snapshot as an added directory, Codex reads it in its
    read-only sandbox; each prompt names it as exactly the head, and only agy's lists its files.
    It is there while the turns run, and gone once the thread is resolved."""
    bus, repo = env["bus"], env["repo"]
    base, head = layout(repo)
    reviewers = ["claude", "antigravity", "copilot", "codex"]
    monkeypatch.setenv("FAKE_APPROVE", ",".join(reviewers))
    seen = []
    real_turn = Worker.turn

    def turn(self, agent, prompt, **kw):
        path = (kw.get("read_dirs") or [None])[0]
        seen.append((agent, path, path and open(os.path.join(path, "src", "app.py")).read()))
        return real_turn(self, agent, prompt, **kw)

    monkeypatch.setattr(Worker, "turn", turn)
    tid = Service(bus, HUMAN).request_review(None, str(repo), base, head, reviewers=reviewers)["thread"]["id"]
    Worker(bus, bus.jobs(tid)[-1]["id"]).run()
    assert bus.review_standings(tid)["complete"]
    path = str(snapshot.root() / tid / head)
    assert seen == [(a, path, "print('head')\n") for a in reviewers]
    by = {c["agent"]: c for c in calls(env)}
    for agent in ("claude", "antigravity", "copilot"):
        a = by[agent]["argv"]
        assert path in a and a[a.index(path) - 1] == "--add-dir", agent
    assert path not in by["codex"]["argv"]  # its read-only sandbox reads everywhere
    for agent, c in by.items():
        assert f"Files under\n{path}\nare exactly head {head}" in c["prompt"], agent
        assert ("Files in the copy (" in c["prompt"]) == (agent == "antigravity"), agent
    assert "\nsrc/other.py\n" in by["antigravity"]["prompt"] and "dep.js" not in by["antigravity"]["prompt"]
    assert f"git -C {repo} --no-replace-objects log" in by["codex"]["prompt"] and "For history and the other pinned commits" in by["codex"]["prompt"]
    # The thread is resolved: its snapshot is gone.
    assert not (snapshot.root() / tid).exists()
    kinds = [e["text"] for e in bus.events(tid) if e["kind"] == "snapshot"]
    assert kinds and kinds[0].startswith(f"full snapshot of head {head[:12]}")


def test_over_cap_review_says_there_is_no_snapshot(env, no_spawn, monkeypatch):
    bus, repo = env["bus"], env["repo"]
    path = env["tmp"] / "config" / "agent-bus" / "config.json"
    path.write_text(json.dumps({**json.loads(path.read_text()), "max_snapshot_bytes": 0}))
    base, commit = git_repo(repo)
    head = commit("one\n")
    tid = Service(bus, HUMAN).request_review("antigravity", str(repo), base, head)["thread"]["id"]
    Worker(bus, bus.jobs(tid)[-1]["id"]).run()
    p = calls(env)[0]["prompt"]
    assert "made no read-only copy of the head commit: snapshots are turned off" in p
    assert "may not match the head commit" in p
    assert not any("/snapshots/" in a for a in calls(env)[0]["argv"])


def test_follow_up_names_the_new_heads_snapshot(env, no_spawn, monkeypatch):
    bus, repo = env["bus"], env["repo"]
    base, commit = git_repo(repo)
    h1 = commit("one\n")
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review("antigravity", str(repo), base, h1)["thread"]["id"]
    Worker(bus, bus.jobs(tid)[-1]["id"]).run()
    h2 = commit("two\n")
    claude.send("fixed", thread=tid)
    claude.request_review("antigravity", str(repo), base, h2, prev_head_commit_id=h1)
    Worker(bus, bus.jobs(tid)[-1]["id"]).run()
    second = calls(env)[-1]
    assert "--conversation" in second["argv"]  # a resumed follow-up
    assert f"are exactly head {h2}" in second["prompt"] and str(snapshot.root() / tid / h2) in second["argv"]


# ------------------------------------------------------------------ garbage collection


def test_sweep_removes_finished_threads_and_strays(env, no_spawn):
    bus, repo = env["bus"], env["repo"]
    base, commit = git_repo(repo)
    head = commit("one\n")
    open_tid = Service(bus, HUMAN).request_review("codex", str(repo), base, head)["thread"]["id"]
    done_tid = Service(bus, HUMAN).start_discussion("Q?", peers=["codex"])["thread"]["id"]
    for tid in (open_tid, done_tid, "t-gone00"):
        assert snapshot.ensure(str(repo), tid, base, head, 10**9).path
    bus.cancel_thread(done_tid)
    snapshot.sweep(bus)
    left = {p.name for p in snapshot.root().iterdir() if not p.name.startswith(".")}
    assert left == {open_tid}  # still open, with a queued job
    bus.transition(open_tid, "resolved")
    snapshot.sweep(bus)  # resolved, but its job is still queued: kept
    assert (snapshot.root() / open_tid).exists()


def test_a_worker_sweeps_stale_snapshots_when_it_starts(env, no_spawn):
    bus, repo = env["bus"], env["repo"]
    base, commit = git_repo(repo)
    head = commit("one\n")
    assert snapshot.ensure(str(repo), "t-crash0", base, head, 10**9).path  # a crashed worker's
    tid = Service(bus, HUMAN).start_discussion("Q?", peers=["codex"])["thread"]["id"]
    Worker(bus, bus.jobs(tid)[-1]["id"]).run()
    assert not (snapshot.root() / "t-crash0").exists()


def test_config_rejects_a_bad_cap(env):
    path = env["tmp"] / "config" / "agent-bus" / "config.json"
    data = json.loads(path.read_text())
    for bad in (-1, "1G", True):
        path.write_text(json.dumps({**data, "max_snapshot_bytes": bad}))
        with pytest.raises(ValueError, match="max_snapshot_bytes"):
            cfg.Config.load()


def test_adapters_pass_read_dirs_as_added_directories(env):
    conf = cfg.Config.load()
    for agent in ("claude", "antigravity", "copilot"):
        argv = adapter(agent, conf).invocation("P", None, str(env["repo"]), ["/scratch", "/snap"]).argv
        i = argv.index("/snap")
        assert argv[i - 1] == "--add-dir" and argv.index("/scratch") < i, agent
    assert adapter("antigravity", conf).lists_files is False
    assert all(adapter(a, conf).lists_files for a in ("claude", "codex", "copilot"))


def test_copilot_refuses_a_snapshot_with_hooks(env, tmp_path):
    from agent_bus.peers import PeerSetupError
    snap = tmp_path / "snap"
    (snap / ".github" / "hooks").mkdir(parents=True)
    (snap / ".github" / "hooks" / "h.json").write_text(json.dumps({"hooks": {"sessionStart": [{"bash": "x"}]}}))
    with pytest.raises(PeerSetupError, match="hooks"):
        adapter("copilot", cfg.Config.load()).invocation("P", None, str(env["repo"]), [str(snap)])


def test_a_symlink_chain_that_resolves_outside_is_left_out(env):
    """`a/b/up -> ../..` is the top, so `a/b/esc -> up/..` passes a text check (a/b) but
    resolves above the top."""
    repo = env["repo"]
    base, _ = git_repo(repo)
    (repo / "a" / "b").mkdir(parents=True)
    (repo / "a" / "b" / "f").write_text("f\n")
    os.symlink("../..", repo / "a" / "b" / "up")
    os.symlink("up/..", repo / "a" / "b" / "esc")
    sh(repo, "git", "add", "-A")
    sh(repo, "git", "commit", "-qm", "links")
    head = sh(repo, "git", "rev-parse", "HEAD")
    assert snapshot._inside("a/b/esc", "up/..")  # the text check alone lets it through
    snap = snapshot.ensure(str(repo), "t-abc", base, head, 10**9)
    top = snapshot.root() / "t-abc" / head
    assert os.readlink(top / "a" / "b" / "up") == "../.." and not os.path.lexists(top / "a" / "b" / "esc")
    assert "a/b/esc" not in snap.files and snap.skipped == 1


def test_replacement_objects_are_ignored(env):
    repo = env["repo"]
    base, commit = git_repo(repo)
    head = commit("pinned\n")
    blob = sh(repo, "git", "rev-parse", f"{head}:a.txt")
    (repo / "other.txt").write_text("replaced\n")
    other = sh(repo, "git", "hash-object", "-w", "other.txt")
    sh(repo, "git", "replace", blob, other)
    assert sh(repo, "git", "show", f"{head}:a.txt") == "replaced"  # git itself honours it
    snapshot.ensure(str(repo), "t-abc", base, head, 10**9)
    assert (snapshot.root() / "t-abc" / head / "a.txt").read_text() == "pinned\n"


def test_reuse_needs_the_same_base_and_a_cap_it_still_fits(env, monkeypatch):
    repo = env["repo"]
    base, commit = git_repo(repo)
    mid = commit("one\n")
    head = commit("two\n")
    snapshot.ensure(str(repo), "t-abc", base, head, 10**9)
    built = []
    real = snapshot._write
    monkeypatch.setattr(snapshot, "_write", lambda *a: built.append(1) or real(*a))
    snapshot.ensure(str(repo), "t-abc", base, head, 10**9)
    assert built == []
    snap = snapshot.ensure(str(repo), "t-abc", mid, head, 10**9)  # rebased onto another base
    assert built == [1] and snap.path
    snap = snapshot.ensure(str(repo), "t-abc", mid, head, 2)  # the cap lowered under it
    assert built == [1] and snap.path is None and snap.mode == "over_cap"


def test_listing_escapes_non_utf8_names(env):
    name = os.fsdecode(b"caf\xe9.txt")
    snap = snapshot.Snapshot(head="h", path="/x", mode="full", files=[name, "ok.txt"], changed=[name])
    text = snapshot.listing(snap)
    text.encode()  # no surrogates left: the prompt encodes strictly
    assert "caf\\xe9.txt" in text and "ok.txt" in text


def test_snapshots_are_kept_apart_per_database(env, monkeypatch):
    first = snapshot.root()
    monkeypatch.setenv("AGENT_BUS_DB", str(env["tmp"] / "other.db"))
    assert snapshot.root() != first and snapshot.root().parent == first.parent


def test_sweep_decides_and_removes_under_the_threads_lock(env, monkeypatch):
    repo = env["repo"]
    base, commit = git_repo(repo)
    head = commit("one\n")
    snapshot.ensure(str(repo), "t-gone00", base, head, 10**9)
    held = []
    real = snapshot._locked

    def locked(tid, wait=None):
        held.append(tid)
        return real(tid, wait)

    monkeypatch.setattr(snapshot, "_locked", locked)
    monkeypatch.setattr(snapshot, "stale", lambda bus, tid: held == [tid])
    snapshot.sweep(env["bus"])
    assert held == ["t-gone00"] and not (snapshot.root() / "t-gone00").exists()


def test_sweep_skips_a_snapshot_being_built(env):
    repo = env["repo"]
    base, commit = git_repo(repo)
    head = commit("one\n")
    snapshot.ensure(str(repo), "t-gone00", base, head, 10**9)
    with snapshot._locked("t-gone00") as mine:  # another worker building it
        assert mine
        snapshot.sweep(env["bus"])  # returns at once rather than waiting
        assert (snapshot.root() / "t-gone00").exists()
    snapshot.sweep(env["bus"])
    assert not (snapshot.root() / "t-gone00").exists()


def test_a_raised_cap_upgrades_a_sparse_snapshot(env):
    repo = env["repo"]
    base, _ = git_repo(repo)
    (repo / "big").mkdir()
    (repo / "big" / "blob.bin").write_bytes(b"x" * 5000)
    (repo / "src").mkdir()
    (repo / "src" / "a.py").write_text("a\n")
    sh(repo, "git", "add", "-A")
    sh(repo, "git", "commit", "-qm", "big")
    head = sh(repo, "git", "rev-parse", "HEAD")
    assert snapshot.ensure(str(repo), "t-abc", base, head, 5005).mode == "sparse"
    assert snapshot.ensure(str(repo), "t-abc", base, head, 5005).mode == "sparse"  # reused
    assert snapshot.ensure(str(repo), "t-abc", base, head, 10**9).mode == "full"


def test_a_held_lock_gives_no_snapshot_rather_than_a_hang(env, monkeypatch):
    repo = env["repo"]
    base, commit = git_repo(repo)
    head = commit("one\n")
    monkeypatch.setattr(snapshot, "LOCK_WAIT_S", 0.3)
    with snapshot._locked("t-abc"):
        snap = snapshot.ensure(str(repo), "t-abc", base, head, 10**9)
    assert snap.path is None and "still writing" in snap.reason
    assert snapshot.ensure(str(repo), "t-abc", base, head, 10**9).path


def test_a_sparse_snapshot_over_the_file_cap_is_reused(env, monkeypatch):
    repo = env["repo"]
    base, _ = git_repo(repo)
    (repo / "src").mkdir()
    (repo / "src" / "a.py").write_text("a\n")
    (repo / "other").mkdir()
    (repo / "other" / "b.py").write_text("b\n")
    sh(repo, "git", "add", "-A")
    sh(repo, "git", "commit", "-qm", "two dirs")
    head = sh(repo, "git", "rev-parse", "HEAD")
    monkeypatch.setattr(snapshot, "MAX_FILES", 2)  # 3 files: over the file cap, not the bytes
    assert snapshot.ensure(str(repo), "t-abc", base, head, 10**9).mode == "sparse"
    monkeypatch.setattr(snapshot, "_write", lambda *a: pytest.fail("rebuilt a reusable sparse snapshot"))
    assert snapshot.ensure(str(repo), "t-abc", base, head, 10**9).mode == "sparse"
