"""#75: a follow-up after a rebase gets a rebase-aware interdiff, not main's changes."""
import json
import os
import shutil
import subprocess

import pytest

from agent_bus.service import Service
from agent_bus.worker import Worker

BOTH = ["codex", "antigravity"]


def _git_version():
    try:
        out = subprocess.run(["git", "version"], capture_output=True, text=True).stdout.split()[2]
        return tuple(int(x) for x in out.split(".")[:2])
    except (OSError, IndexError, ValueError):
        return (0, 0)


# `git merge-tree --merge-base` is git 2.40+; older ones always get the full diff.
pytestmark = pytest.mark.skipif(_git_version() < (2, 40), reason="needs git 2.40+")
FOLLOWUP = "The change you reviewed earlier in this conversation"


def calls(env):
    return [json.loads(l) for l in env["log"].read_text().splitlines()] if env["log"].exists() else []


def run_job(bus, tid):
    Worker(bus, bus.jobs(tid)[-1]["id"]).run()


class Repo:
    """A plain git repo with a main line and a one-commit change on top of it."""

    def __init__(self, path):
        if not shutil.which("git"):
            pytest.skip("git not installed")
        self.path = path
        self.env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
                        GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")
        self.git("init", "-q", "-b", "main")
        self.m1 = self.commit({"a.txt": "one\ntwo\nthree\n"}, "base")

    def git(self, *a):
        return subprocess.run(["git", *a], cwd=self.path, check=True, capture_output=True, text=True,
                              env=self.env).stdout.strip()

    def commit(self, files, msg, parent=None):
        """Commit ``files`` (None deletes) on top of ``parent`` (default: the current HEAD)."""
        if parent:
            self.git("checkout", "-q", "--detach", parent)
        for name, text in files.items():
            if text is None:
                self.git("rm", "-q", name)
            else:
                (self.path / name).write_text(text)
                self.git("add", name)
        self.git("commit", "-qm", msg)
        return self.git("rev-parse", "HEAD")

    def objects(self):
        return self.git("count-objects", "-v")


@pytest.fixture(autouse=True)
def no_user_git_config(monkeypatch):
    """The developer's own git config (say, a merge driver) mustn't change the outcome."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


@pytest.fixture
def repo(env):
    return Repo(env["repo"])


def reviewed(env, repo, files, reviewers=None):
    """A change (``files`` on main's first commit) that codex (or ``reviewers``) reviewed."""
    f1 = repo.commit(files, "change", parent=repo.m1)
    bus = env["bus"]
    claude = Service(bus, "claude", bus.register_session("claude"))
    kw = {"reviewers": reviewers} if reviewers else {}
    tid = claude.request_review(None if reviewers else "codex", str(repo.path), repo.m1, f1, **kw)["thread"]["id"]
    run_job(bus, tid)
    return bus, claude, tid, f1


def followup(env, claude, bus, tid, repo, base, head, prev, reviewers=None):
    kw = {"reviewers": reviewers} if reviewers else {}
    claude.request_review(None if reviewers else "codex", str(repo.path), base, head, prev_head_commit_id=prev, **kw)
    run_job(bus, tid)
    return calls(env)[-1]


def test_pure_rebase_says_so_and_leaves_out_mains_changes(env, no_spawn, repo):
    bus, claude, tid, f1 = reviewed(env, repo, {"b.txt": "FEATURE\n"})
    m2 = repo.commit({"c.txt": "MAIN-CHANGE\n"}, "main moves on", parent=repo.m1)
    f2 = repo.commit({"b.txt": "FEATURE\n"}, "change", parent=m2)
    before = repo.objects()
    last = followup(env, claude, bus, tid, repo, m2, f2, f1)
    assert last["argv"][:3] == ["exec", "resume", "--json"]  # the session is kept
    p = last["prompt"]
    assert FOLLOWUP in p and f"you last reviewed {f1}" in p
    assert f"base moved from {repo.m1} to {m2}" in p and "pure rebase" in p
    assert "Interdiff since the head you last reviewed (rebase-aware)" in p and "MAIN-CHANGE" not in p and "FEATURE" not in p
    assert repo.objects() == before  # the replay wrote nothing to the repository
    v = bus.last_verdict(tid, "codex")["data"]
    assert (v["head_commit_id"], v["base_commit_id"]) == (f2, m2)


def test_rebase_with_a_change_of_its_own_shows_only_that(env, no_spawn, repo):
    bus, claude, tid, f1 = reviewed(env, repo, {"b.txt": "FEATURE\n"})
    m2 = repo.commit({"a.txt": "one\ntwo\nthree\nMAIN-CHANGE\n"}, "main moves on", parent=repo.m1)
    f2 = repo.commit({"b.txt": "FEATURE-V2\n"}, "change", parent=m2)
    last = followup(env, claude, bus, tid, repo, m2, f2, f1)
    p = last["prompt"]
    assert last["argv"][:3] == ["exec", "resume", "--json"] and FOLLOWUP in p
    assert "Interdiff since the head you last reviewed (rebase-aware)" in p and f"base moved from {repo.m1}" in p
    assert "-FEATURE\n+FEATURE-V2" in p and "MAIN-CHANGE" not in p and "pure rebase" not in p


def assert_full_diff(env, bus, tid, last, why):
    assert last["argv"][:3] != ["exec", "resume", "--json"]
    assert FOLLOWUP not in last["prompt"] and "Diff from base to head" in last["prompt"]
    notice = [m for m in bus.history(tid)["messages"] if m["sender"] == "system" and "requested from" in m["body"]]
    assert f"Full diff: the base changed and {why}" in notice[-1]["body"]


def test_conflicting_rebase_falls_back_to_the_full_diff(env, no_spawn, repo):
    bus, claude, tid, f1 = reviewed(env, repo, {"a.txt": "one\nFEATURE\nthree\n"})
    m2 = repo.commit({"a.txt": "one\nMAIN-CHANGE\nthree\n"}, "main moves on", parent=repo.m1)
    f2 = repo.commit({"a.txt": "one\nMAIN-CHANGE FEATURE\nthree\n"}, "change, conflict resolved", parent=m2)
    last = followup(env, claude, bus, tid, repo, m2, f2, f1)
    assert_full_diff(env, bus, tid, last, "replaying the previous head onto the new base conflicts")
    assert f"Previous head (already reviewed): {f1}" in last["prompt"]


def test_missing_old_base_falls_back_to_the_full_diff(env, no_spawn, repo):
    bus, claude, tid, f1 = reviewed(env, repo, {"b.txt": "FEATURE\n"})
    gone = "0123456789abcdef0123456789abcdef01234567"
    m = bus.last_verdict(tid, "codex")  # as if that base had since been garbage-collected
    bus.conn.execute("UPDATE messages SET data=? WHERE id=?",
                     (json.dumps({**m["data"], "base_commit_id": gone}), m["id"]))
    m2 = repo.commit({"c.txt": "MAIN-CHANGE\n"}, "main moves on", parent=repo.m1)
    f2 = repo.commit({"b.txt": "FEATURE\n"}, "change", parent=m2)
    last = followup(env, claude, bus, tid, repo, m2, f2, f1)
    assert_full_diff(env, bus, tid, last, f"commit {gone} is not in the repository")


def test_interdiff_bigger_than_the_full_diff_falls_back(env, no_spawn, repo):
    big = "".join(f"line {i}\n" for i in range(500))
    bus, claude, tid, f1 = reviewed(env, repo, {"big.txt": big})
    m2 = repo.commit({"c.txt": "MAIN-CHANGE\n"}, "main moves on", parent=repo.m1)
    f2 = repo.commit({"small.txt": "small\n"}, "a much smaller change", parent=m2)
    last = followup(env, claude, bus, tid, repo, m2, f2, f1)
    assert_full_diff(env, bus, tid, last, "the rebase-aware interdiff is bigger than the full diff")
    assert "+small" in last["prompt"] and "line 499" not in last["prompt"]


def test_configured_merge_driver_falls_back_without_running_it(env, no_spawn, repo):
    bus, claude, tid, f1 = reviewed(env, repo, {"b.txt": "FEATURE\n"})
    repo.git("config", "merge.mine.driver", "touch DRIVER-RAN; false")
    m2 = repo.commit({".gitattributes": "* merge=mine\n", "b.txt": "MAIN\n"}, "main moves on", parent=repo.m1)
    f2 = repo.commit({"b.txt": "FEATURE\n"}, "change", parent=m2)
    last = followup(env, claude, bus, tid, repo, m2, f2, f1)
    assert_full_diff(env, bus, tid, last, "it has a custom merge driver configured")
    assert not (repo.path / "DRIVER-RAN").exists()


def test_several_reviewers_get_the_rebase_aware_interdiff(env, no_spawn, repo, monkeypatch):
    monkeypatch.setenv("FAKE_APPROVE", "codex,antigravity")
    bus, claude, tid, f1 = reviewed(env, repo, {"b.txt": "FEATURE\n"}, reviewers=BOTH)
    assert bus.review_standings(tid)["complete"]
    m2 = repo.commit({"c.txt": "MAIN-CHANGE\n"}, "main moves on", parent=repo.m1)
    f2 = repo.commit({"b.txt": "FEATURE\n"}, "change", parent=m2)
    res = claude.request_review(None, str(repo.path), m2, f2, prev_head_commit_id=f1, reviewers=BOTH)
    assert res["review"]["pending"] == BOTH  # a new base still needs new approvals
    run_job(bus, tid)
    later = calls(env)[2:]
    assert [c["agent"] for c in later] == BOTH
    for c in later:
        p = c["prompt"]
        assert FOLLOWUP in p and "pure rebase" in p and "MAIN-CHANGE" not in p, c["agent"]
    st = bus.review_standings(tid)
    assert st["complete"] and all(r["base_commit_id"] == m2 for r in st["reviewers"].values())


def test_several_reviewers_fall_back_on_a_conflict(env, no_spawn, repo, monkeypatch):
    monkeypatch.setenv("FAKE_APPROVE", "codex,antigravity")
    bus, claude, tid, f1 = reviewed(env, repo, {"a.txt": "one\nFEATURE\nthree\n"}, reviewers=BOTH)
    m2 = repo.commit({"a.txt": "one\nMAIN-CHANGE\nthree\n"}, "main moves on", parent=repo.m1)
    f2 = repo.commit({"a.txt": "one\nMAIN-CHANGE FEATURE\nthree\n"}, "change", parent=m2)
    claude.request_review(None, str(repo.path), m2, f2, prev_head_commit_id=f1, reviewers=BOTH)
    run_job(bus, tid)
    for c in calls(env)[2:]:
        assert FOLLOWUP not in c["prompt"] and "Diff from base to head" in c["prompt"]
    assert bus.review_standings(tid)["complete"]


@pytest.mark.parametrize("colocate", [True, False])
def test_jj_repo_gets_the_rebase_aware_interdiff(env, no_spawn, colocate):
    """jj repos too, colocated or not (git reads a non-colocated repo's backing store)."""
    if shutil.which("jj") is None or shutil.which("git") is None:
        pytest.skip("jj and git needed")
    path = env["repo"]
    sh = lambda *a: subprocess.run(a, cwd=path, check=True, capture_output=True, text=True).stdout.strip()  # noqa: E731
    sh("jj", "git", "init", "--colocate" if colocate else "--no-colocate")
    rev = lambda r: sh("jj", "log", "--no-graph", "-r", r, "-T", "commit_id")  # noqa: E731
    (path / "a.txt").write_text("hello\n")
    sh("jj", "file", "track", "a.txt")
    sh("jj", "commit", "-m", "base")
    (path / "b.txt").write_text("FEATURE\n")
    sh("jj", "file", "track", "b.txt")
    sh("jj", "describe", "-m", "change")
    m1, f1 = rev("@-"), rev("@")
    change = sh("jj", "log", "--no-graph", "-r", "@", "-T", "change_id")
    bus = env["bus"]
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review("codex", str(path), m1, f1)["thread"]["id"]
    run_job(bus, tid)
    sh("jj", "new", m1, "-m", "main moves on")
    (path / "c.txt").write_text("MAIN-CHANGE\n")
    sh("jj", "file", "track", "c.txt")
    m2 = rev("@")
    sh("jj", "rebase", "-r", f1, "-d", m2)
    f2 = rev(change)
    claude.request_review("codex", str(path), m2, f2, prev_head_commit_id=f1)
    run_job(bus, tid)
    p = calls(env)[-1]["prompt"]
    assert FOLLOWUP in p and "pure rebase" in p and "MAIN-CHANGE" not in p
