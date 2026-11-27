"""#14: plain git repos work without jj."""
import os
import shutil
import subprocess

import pytest

from agent_bus import config as cfg
from agent_bus.service import Service
from agent_bus.worker import Worker

GIT = shutil.which("git")


def git_repo(repo):
    if not GIT:
        pytest.skip("git not installed")
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")
    sh = lambda *a: subprocess.run(a, cwd=repo, check=True, capture_output=True, text=True, env=env).stdout.strip()  # noqa: E731
    sh("git", "init", "-q")
    (repo / "a.txt").write_text("hello\n")
    sh("git", "add", "a.txt")
    sh("git", "commit", "-qm", "base")
    return sh


@pytest.fixture
def no_jj(tmp_path, monkeypatch):
    """PATH with git but no jj, as on a machine that doesn't use Jujutsu."""
    if not GIT:
        pytest.skip("git not installed")
    bindir = tmp_path / "gitonly"
    bindir.mkdir()
    (bindir / "git").symlink_to(GIT)
    monkeypatch.setenv("PATH", str(bindir))
    assert shutil.which("jj") is None


def test_vcs_detection(env, tmp_path, monkeypatch):
    plain = tmp_path / "plain"
    plain.mkdir()
    git_repo(plain)
    (plain / "sub").mkdir()
    assert cfg.vcs(plain / "sub") == "git" and cfg.repo_root(plain / "sub") == str(plain.resolve())
    nowhere = tmp_path / "nowhere"
    in_some_repo = any((d / ".git").exists() for d in (nowhere, *nowhere.parents))
    assert cfg.vcs(nowhere) == ("git" if in_some_repo else None)
    colocated = tmp_path / "co"
    colocated.mkdir()
    git_repo(colocated)
    (colocated / ".jj").mkdir()
    monkeypatch.setattr(cfg.shutil, "which", lambda name: "/bin/jj" if name == "jj" else None)
    assert cfg.vcs(colocated) == "jj"
    monkeypatch.setattr(cfg.shutil, "which", lambda name: None)
    assert cfg.vcs(colocated) == "git"  # jj not installed: fall back to the colocated git repo

    # Nearest repository boundary wins: a plain git repo nested inside a jj repo stays git.
    outer_jj = tmp_path / "outer-jj"
    (outer_jj / ".jj").mkdir(parents=True)
    nested_git = outer_jj / "nested-git"
    nested_git.mkdir()
    git_repo(nested_git)
    (nested_git / "deep").mkdir()
    monkeypatch.setattr(cfg.shutil, "which", lambda name: "/bin/jj" if name == "jj" else None)
    assert cfg.vcs(nested_git / "deep") == "git"
    assert cfg.repo_root(nested_git / "deep") == str(nested_git.resolve())

    # A non-colocated jj repo nested inside a git repo does not bleed out to the parent git repo when jj is absent.
    nested_jj = plain / "nested-jj"
    (nested_jj / ".jj").mkdir(parents=True)
    monkeypatch.setattr(cfg.shutil, "which", lambda name: None)
    assert cfg.vcs(nested_jj) is None
    # A non-bare repo's .git directory is not mistaken for a bare repository.
    assert not cfg._is_bare_git(plain / ".git")
    assert cfg.vcs(plain / ".git" / "objects") == "git"


def test_colocated_repo_falls_back_to_git_for_unimported_commit(env, no_spawn):
    if shutil.which("jj") is None:
        pytest.skip("jj not installed")
    repo = env["repo"]
    sh = git_repo(repo)
    sh("jj", "git", "init", "--colocate")
    base = sh("git", "rev-parse", "HEAD")
    # Make a plain git commit without running jj afterward so jj hasn't imported it.
    (repo / "a.txt").write_text("unimported git commit\n")
    sh("git", "commit", "-qam", "git-only")
    head = sh("git", "rev-parse", "HEAD")
    bus = env["bus"]
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review("codex", str(repo), base, head, change_id="git-commit")["thread"]["id"]
    Worker(bus, bus.jobs(tid)[-1]["id"]).run()
    import json
    first = [json.loads(l) for l in env["log"].read_text().splitlines()][0]
    assert "+unimported git commit" in first["prompt"]


def test_bare_git_repo_detection_and_review(env, no_spawn, no_jj, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    sh = git_repo(src)
    base = sh("git", "rev-parse", "HEAD")
    (src / "a.txt").write_text("bare change\n")
    sh("git", "commit", "-qam", "change")
    head = sh("git", "rev-parse", "HEAD")
    bare = env["repo"] / "bare.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(bare)], check=True)

    assert cfg.vcs(bare) == "git"
    assert cfg.repo_root(bare) == str(bare.resolve())
    assert cfg.repo_root(bare / "objects") == str(bare.resolve())

    bus = env["bus"]
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review("codex", str(bare), base, head, change_id="bare-branch")["thread"]["id"]
    Worker(bus, bus.jobs(tid)[-1]["id"]).run()
    import json
    first = [json.loads(l) for l in env["log"].read_text().splitlines()][0]
    assert "+bare change" in first["prompt"]


def test_review_round_trip_in_plain_git_without_jj(env, no_spawn, no_jj):
    repo = env["repo"]
    sh = git_repo(repo)
    base = sh("git", "rev-parse", "HEAD")
    (repo / "a.txt").write_text("helo\n")
    sh("git", "commit", "-qam", "typo")
    head1 = sh("git", "rev-parse", "HEAD")
    bus = env["bus"]
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review("codex", str(repo), base, head1, change_id="feature/typo-fix")["thread"]["id"]
    Worker(bus, bus.jobs(tid)[-1]["id"]).run()
    verdicts = [m for m in bus.history(tid)["messages"] if m["kind"] == "review_verdict"]
    assert verdicts and verdicts[-1]["data"]["head_commit_id"] == head1
    import json
    first = [json.loads(l) for l in env["log"].read_text().splitlines()][0]
    assert "+helo" in first["prompt"]
    # A follow-up pass gets the interdiff from git too.
    (repo / "a.txt").write_text("hello\n")
    sh("git", "commit", "-qam", "fix")
    head2 = sh("git", "rev-parse", "HEAD")
    claude.request_review("codex", str(repo), base, head2, prev_head_commit_id=head1, change_id="PR #12")
    Worker(bus, bus.jobs(tid)[-1]["id"]).run()
    second = [json.loads(l) for l in env["log"].read_text().splitlines()][-1]
    assert "Interdiff" in second["prompt"] and "-helo" in second["prompt"]


@pytest.mark.parametrize("label,ok", [
    ("qpvuntsm", True), ("feature/typo-fix", True), ("PR #12", True), ("v1.2+build", True),
    (" leading", False), ("trailing ", False), ("fix (review)", False), ("two\nlines", False), ("x" * 101, False), ("$(rm)", False),
])
def test_change_labels(env, no_spawn, label, ok):
    bus = env["bus"]
    claude = Service(bus, "claude", bus.register_session("claude"))
    call = lambda: claude.request_review("codex", str(env["repo"]), "a" * 40, "b" * 40, change_id=label)  # noqa: E731
    if ok:
        call()
    else:
        with pytest.raises(ValueError, match="label"):
            call()


# #42: a well-formed commit id the repository doesn't have is reported on the thread. Since #73
# request_review refuses one up front, so these tests skip that check (unchecked_commits):
# the worker still catches a commit that is gone by the time the review runs.

MISSING = "0123456789abcdef0123456789abcdef01234567"


def missing_report(env, tid):
    bus = env["bus"]
    t = bus.thread(tid)
    msgs = bus.history(tid)["messages"]
    assert t["status"] == "needs_human"
    head = t["refs"]["head_commit_id"]
    assert not [m for m in msgs if m["kind"] == "review_verdict" and m["data"]["head_commit_id"] == head]
    assert bus.jobs(tid)[-1]["status"] == "failed"
    assert not env["log"].exists() or len(env["log"].read_text().splitlines()) == env.get("calls", 0)
    report = [m for m in msgs if m["sender"] == "system" and "Review not started" in m["body"]]
    assert len(report) == 1 and report[0]["recipient"] == "claude"
    assert f"agent-bus resume {tid}" in report[0]["body"]
    return report[0]["body"]


@pytest.mark.parametrize("which", ["base", "head"])
def test_missing_commit_in_plain_git_needs_human(env, no_spawn, unchecked_commits, no_jj, which):
    sh = git_repo(env["repo"])
    real = sh("git", "rev-parse", "HEAD")
    base, head = (MISSING, real) if which == "base" else (real, MISSING)
    bus = env["bus"]
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review("codex", str(env["repo"]), base, head)["thread"]["id"]
    assert Worker(bus, bus.jobs(tid)[-1]["id"]).run() == 1
    body = missing_report(env, tid)
    assert f"the {which} commit {MISSING}" in body and real not in body
    assert "request this review again" in body
    # The failed job doesn't block requesting the same review again.
    again = claude.request_review("codex", str(env["repo"]), base, head)
    assert not again["job"].get("deduplicated")
    # #179 (antigravity-4): the commits are checked before any snapshot is made.
    assert not [e for e in bus.events(tid) if e["kind"] in ("snapshot", "prompts")]


def test_missing_commit_in_jj_repo_needs_human(env, no_spawn, unchecked_commits):
    if shutil.which("jj") is None:
        pytest.skip("jj not installed")
    repo = env["repo"]
    sh = git_repo(repo)
    sh("jj", "git", "init", "--colocate")
    base = sh("git", "rev-parse", "HEAD")
    bus = env["bus"]
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review("codex", str(repo), base, MISSING)["thread"]["id"]
    Worker(bus, bus.jobs(tid)[-1]["id"]).run()
    assert f"the head commit {MISSING}" in missing_report(env, tid)


def test_missing_previous_head_on_followup_needs_human(env, no_spawn, unchecked_commits, no_jj):
    repo = env["repo"]
    sh = git_repo(repo)
    base = sh("git", "rev-parse", "HEAD")
    (repo / "a.txt").write_text("helo\n")
    sh("git", "commit", "-qam", "typo")
    head1 = sh("git", "rev-parse", "HEAD")
    bus = env["bus"]
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review("codex", str(repo), base, head1)["thread"]["id"]
    Worker(bus, bus.jobs(tid)[-1]["id"]).run()
    assert bus.thread(tid)["status"] == "open"  # changes requested: waits for the fix (#143)
    env["calls"] = len(env["log"].read_text().splitlines())
    # Rewrite the reviewed head away entirely, as after an amend plus gc.
    (repo / "a.txt").write_text("hi\n")
    sh("git", "commit", "-q", "--amend", "-am", "fixed")
    head2 = sh("git", "rev-parse", "HEAD")
    sh("git", "reflog", "expire", "--expire=now", "--all")
    sh("git", "gc", "-q", "--prune=now")
    seen = len([e for e in bus.events(tid) if e["kind"] in ("snapshot", "prompts")])
    claude.request_review("codex", str(repo), base, head2, prev_head_commit_id=head1)
    Worker(bus, bus.jobs(tid)[-1]["id"]).run()
    body = missing_report(env, tid)
    assert f"the previous head commit {head1}" in body and head2 not in body
    # #183: the interdiff's previous head is checked before this pass makes a snapshot.
    assert len([e for e in bus.events(tid) if e["kind"] in ("snapshot", "prompts")]) == seen
    # A follow-up's refs already moved to head2, so only `resume` retries this pass.
    assert "ask the human user to run `agent-bus resume" in body and "request this review again" not in body
    assert "without prev_head_commit_id" in body


def test_missing_head_is_not_read_as_a_path(env, no_spawn, unchecked_commits, no_jj):
    repo = env["repo"]
    sh = git_repo(repo)
    base = sh("git", "rev-parse", "HEAD")
    (repo / f"{MISSING}^{{commit}}").write_text("decoy\n")
    bus = env["bus"]
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review("codex", str(repo), base, MISSING)["thread"]["id"]
    Worker(bus, bus.jobs(tid)[-1]["id"]).run()
    assert f"the head commit {MISSING}" in missing_report(env, tid)


def test_tree_id_is_not_reviewed_as_a_commit(env, no_spawn, unchecked_commits, no_jj):
    sh = git_repo(env["repo"])
    base = sh("git", "rev-parse", "HEAD")
    tree = sh("git", "rev-parse", "HEAD^{tree}")
    bus = env["bus"]
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review("codex", str(env["repo"]), base, tree)["thread"]["id"]
    Worker(bus, bus.jobs(tid)[-1]["id"]).run()
    assert f"the head commit {tree}" in missing_report(env, tid)


def test_git_probe_error_is_not_reported_as_missing(env, no_spawn, unchecked_commits, no_jj, monkeypatch):
    sh = git_repo(env["repo"])
    real = sh("git", "rev-parse", "HEAD")
    bus = env["bus"]
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review("codex", str(env["repo"]), real, MISSING)["thread"]["id"]
    real_run = subprocess.run
    # A fatal git error (exit 128, e.g. bad config) is not proof the commit is missing.
    monkeypatch.setattr("agent_bus.worker.subprocess.run", lambda argv, **kw: real_run(
        ["git", "-c", "=x", *argv[1:]] if argv[:3] == ["git", "--no-replace-objects", "rev-parse"] else argv, **kw))
    with pytest.raises(RuntimeError, match="could not compute diff"):
        Worker(bus, bus.jobs(tid)[-1]["id"]).run()
    assert bus.thread(tid)["status"] == "open"


def test_unrelated_diff_failure_is_not_reported_as_missing(env, no_spawn, no_jj, monkeypatch):
    sh = git_repo(env["repo"])
    real = sh("git", "rev-parse", "HEAD")
    bus = env["bus"]
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review("codex", str(env["repo"]), real, real)["thread"]["id"]
    real_run = subprocess.run
    monkeypatch.setattr("agent_bus.worker.subprocess.run", lambda argv, **kw: real_run(
        ["git", "diff", "--no-such-flag"] if argv[:3] == ["git", "--no-replace-objects", "diff"] else argv, **kw))
    with pytest.raises(RuntimeError, match="could not compute diff"):
        Worker(bus, bus.jobs(tid)[-1]["id"]).run()
    assert bus.thread(tid)["status"] == "open"
    assert not [m for m in bus.history(tid)["messages"] if "Review not started" in m["body"]]
