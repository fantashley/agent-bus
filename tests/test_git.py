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
    (" leading", False), ("trailing ", False), ("two\nlines", False), ("x" * 101, False), ("$(rm)", False),
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
