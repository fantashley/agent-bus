"""#73: request_review checks the pinned commits before it creates anything."""
import hashlib
import shutil
import subprocess

import pytest

from agent_bus import worker
from agent_bus.db import BusError
from agent_bus.service import Service
from test_git import git_repo, no_jj  # noqa: F401  (no_jj is a fixture)

BOTH = ["codex", "antigravity"]
MISSING = "0123456789abcdef0123456789abcdef01234567"


def nothing_created(bus):
    return not bus._all("SELECT id FROM threads") and not bus._all("SELECT id FROM jobs")


def two_commits(repo):
    sh = git_repo(repo)
    base = sh("git", "rev-parse", "HEAD")
    (repo / "a.txt").write_text("change\n")
    sh("git", "commit", "-qam", "change")
    return sh, base, sh("git", "rev-parse", "HEAD")


@pytest.fixture
def claude(env):
    bus = env["bus"]
    return Service(bus, "claude", bus.register_session("claude"))


@pytest.mark.parametrize("which", ["base", "head"])
@pytest.mark.parametrize("reviewers", [None, BOTH])
def test_missing_commit_is_refused_before_anything_is_created(env, no_spawn, no_jj, claude, which, reviewers):
    _, base, head = two_commits(env["repo"])
    base, head = (MISSING, head) if which == "base" else (base, MISSING)
    with pytest.raises(BusError) as e:
        if reviewers:
            claude.request_review(None, str(env["repo"]), base, head, reviewers=reviewers)
        else:
            claude.request_review("codex", str(env["repo"]), base, head)
    assert e.value.code == "REVISION_NOT_FOUND"
    assert f"no commit {MISSING} ({which}_commit_id)" in e.value.message
    assert "jj log --no-graph -r <rev> -T commit_id" in e.value.message and "jj git import" in e.value.message
    assert nothing_created(env["bus"]) and no_spawn == []


def test_short_ids_resolve_to_full_ones(env, no_spawn, no_jj, claude):
    sh, base, head = two_commits(env["repo"])
    res = claude.request_review("codex", str(env["repo"]), base[:7].upper(), head[:10], change_id="short")
    refs = env["bus"].thread(res["thread"]["id"])["refs"]
    assert (refs["base_commit_id"], refs["head_commit_id"], refs["heads"]) == (base, head, [head])
    # The same request with full ids is the same review, and a follow-up finds it by a prefix.
    again = claude.request_review("codex", str(env["repo"]), base, head, change_id="short")
    assert again["thread"]["id"] == res["thread"]["id"] and again["job"]["deduplicated"]
    (env["repo"] / "a.txt").write_text("fixed\n")
    sh("git", "commit", "-qam", "fix")
    head2 = sh("git", "rev-parse", "HEAD")
    env["bus"].conn.execute("UPDATE jobs SET status='done'")
    nxt = claude.request_review("codex", str(env["repo"]), base, head2[:8], prev_head_commit_id=head[:8])
    assert nxt["thread"]["id"] == res["thread"]["id"]
    assert env["bus"].thread(res["thread"]["id"])["refs"]["prev_head_commit_id"] == head


@pytest.mark.parametrize("bad", ["abc123", "g" * 40, "a" * 41, "a" * 40 + "\n", 12345])
def test_malformed_ids_say_what_is_allowed(env, no_spawn, claude, bad):
    with pytest.raises(ValueError, match="at least 7") as e:
        claude.request_review("codex", str(env["repo"]), "a" * 40, bad)
    assert "never extend a short id by hand" in str(e.value)


def test_prefixes_need_a_repository(env, no_spawn, claude, monkeypatch):
    # env["repo"] is a plain directory (unless the temp dir is inside some repo): there's
    # nothing to resolve a prefix in.
    monkeypatch.setattr(worker, "commit_tools", lambda repo: [])
    with pytest.raises(ValueError, match="40 hex digits"):
        claude.request_review("codex", str(env["repo"]), "a" * 40, "b" * 7)
    assert nothing_created(env["bus"])


def test_tree_id_is_not_a_commit(env, no_spawn, no_jj, claude):
    sh, base, _ = two_commits(env["repo"])
    tree = sh("git", "rev-parse", "HEAD^{tree}")
    with pytest.raises(BusError) as e:
        claude.request_review("codex", str(env["repo"]), base, tree)
    assert e.value.code == "REVISION_NOT_FOUND" and nothing_created(env["bus"])


def test_missing_previous_head_is_refused(env, no_spawn, no_jj, claude):
    _, base, head = two_commits(env["repo"])
    with pytest.raises(BusError) as e:
        claude.request_review("codex", str(env["repo"]), base, head, prev_head_commit_id=MISSING)
    assert e.value.code == "REVISION_NOT_FOUND" and "without prev_head_commit_id" in e.value.message
    assert nothing_created(env["bus"])


def test_base_must_be_an_ancestor_of_head(env, no_spawn, no_jj, claude):
    sh, base, head = two_commits(env["repo"])
    sh("git", "checkout", "-q", "-b", "side", base)
    (env["repo"] / "b.txt").write_text("side\n")
    sh("git", "add", "b.txt")
    sh("git", "commit", "-qm", "side")
    side = sh("git", "rev-parse", "HEAD")
    with pytest.raises(BusError) as e:
        claude.request_review("codex", str(env["repo"]), side, head)
    assert e.value.code == "REVISION_NOT_ANCESTOR" and "git merge-base" in e.value.message
    assert nothing_created(env["bus"])
    claude.request_review("codex", str(env["repo"]), head, head)  # a commit is its own ancestor


def test_ambiguous_prefix_lists_the_matches(env, no_spawn, no_jj, claude, monkeypatch):
    _, base, head = two_commits(env["repo"])
    monkeypatch.setattr(worker, "resolve_commit", lambda repo, tools, rev: (
        ("ambiguous", ["abcdef1" + "0" * 33, "abcdef1" + "1" * 33]) if rev == "abcdef1" else ("found", [rev])))
    with pytest.raises(BusError) as e:
        claude.request_review("codex", str(env["repo"]), base, "abcdef1")
    assert e.value.code == "REVISION_AMBIGUOUS" and "abcdef100000, abcdef111111" in e.value.message
    assert nothing_created(env["bus"])


def test_unknown_probe_lets_a_full_id_through_but_not_a_prefix(env, no_spawn, no_jj, claude, monkeypatch):
    _, base, head = two_commits(env["repo"])
    monkeypatch.setattr(worker, "resolve_commit", lambda repo, tools, rev: ("unknown", []))
    with pytest.raises(BusError, match="pass the full id"):
        claude.request_review("codex", str(env["repo"]), base, head[:12])
    assert claude.request_review("codex", str(env["repo"]), base, head)["thread"]["id"]


# The resolver itself, with ambiguity a 7-digit prefix would need thousands of commits for.

def many_commits(repo, n=5):
    """``n`` commits, newest first, plus two branches whose commits share a 4-digit prefix."""
    sh = git_repo(repo)
    for i in range(n):
        (repo / "a.txt").write_text(f"{i}\n")
        sh("git", "commit", "-qam", str(i))
    ids = sh("git", "log", "--format=%H").split()
    tree, seen = sh("git", "rev-parse", "HEAD^{tree}"), {}
    for i in range(100000):  # hash commit objects until two prefixes collide (a few hundred)
        body = (f"tree {tree}\nparent {ids[0]}\nauthor t <t@example.com> 0 +0000\n"
                f"committer t <t@example.com> 0 +0000\n\n{i}\n").encode()
        sha = hashlib.sha1(b"commit %d\0" % len(body) + body).hexdigest()
        if sha[:4] in seen:
            break
        seen[sha[:4]] = body
    for k, b in enumerate((seen[sha[:4]], body)):
        made = subprocess.run(["git", "hash-object", "-t", "commit", "-w", "--stdin"], cwd=repo, input=b,
                              check=True, capture_output=True).stdout.decode().strip()
        sh("git", "branch", f"twin{k}", made)
    return sh, ids, sha[:4]


def test_resolver_in_plain_git(env, no_jj):
    sh, ids, dup = many_commits(env["repo"])
    repo = str(env["repo"])
    assert worker.resolve_commit(repo, ["git"], ids[0][:7]) == ("found", [ids[0]])
    state, found = worker.resolve_commit(repo, ["git"], dup)
    assert state == "ambiguous" and len(found) > 1 and all(i.startswith(dup) for i in found)
    assert worker.resolve_commit(repo, ["git"], MISSING) == ("missing", [])
    # A branch named like the prefix doesn't shadow the commit (git rev-parse would take it).
    sh("git", "branch", ids[0][:7], ids[-1])
    assert worker.resolve_commit(repo, ["git"], ids[0][:7]) == ("found", [ids[0]])


def test_resolver_in_colocated_jj(env):
    if shutil.which("jj") is None:
        pytest.skip("jj not installed")
    sh, ids, dup = many_commits(env["repo"])
    sh("jj", "git", "init", "--colocate")
    repo = str(env["repo"])
    tools = worker.commit_tools(repo)
    assert tools == ["jj", "git"]
    assert worker.resolve_commit(repo, ["jj"], ids[0][:7]) == ("found", [ids[0]])
    assert worker.resolve_commit(repo, ["jj"], dup)[0] == "ambiguous"
    assert worker.resolve_commit(repo, ["jj"], MISSING) == ("missing", [])
    assert worker.resolve_commit(repo, tools, MISSING) == ("missing", [])
    # A plain git commit jj hasn't imported yet is found through git.
    (env["repo"] / "a.txt").write_text("git only\n")
    sh("git", "commit", "-qam", "git only")
    git_only = sh("git", "rev-parse", "HEAD")
    assert worker.resolve_commit(repo, ["jj"], git_only[:9]) == ("missing", [])
    assert worker.resolve_commit(repo, tools, git_only[:9]) == ("found", [git_only])
    assert worker.is_ancestor(repo, tools, ids[-1], git_only) is True
    assert worker.is_ancestor(repo, tools, git_only, ids[-1]) is False
    # jj's own answer, as in a jj repo without a .git.
    assert worker.is_ancestor(repo, ["jj"], ids[-1], ids[0]) is True
    assert worker.is_ancestor(repo, ["jj"], ids[0], ids[-1]) is False


def test_request_in_colocated_jj_with_an_unimported_git_commit(env, no_spawn, claude):
    if shutil.which("jj") is None:
        pytest.skip("jj not installed")
    sh, base, _ = two_commits(env["repo"])
    sh("jj", "git", "init", "--colocate")
    (env["repo"] / "a.txt").write_text("unimported\n")
    sh("git", "commit", "-qam", "unimported")
    head = sh("git", "rev-parse", "HEAD")
    res = claude.request_review("codex", str(env["repo"]), base[:7], head[:7])
    assert env["bus"].thread(res["thread"]["id"])["refs"]["head_commit_id"] == head


def test_colocated_prefix_is_ambiguous_across_jj_and_git(env):
    """jj has one commit with the prefix, git also has one jj hasn't imported: ambiguous,
    not jj's match (which could pin the wrong head)."""
    if shutil.which("jj") is None:
        pytest.skip("jj not installed")
    sh, _, dup = many_commits(env["repo"])
    sh("git", "branch", "-D", "twin1")  # its commit stays in git's object store
    sh("jj", "git", "init", "--colocate")
    repo = str(env["repo"])
    assert worker.resolve_commit(repo, ["jj"], dup)[0] == "found"
    state, ids = worker.resolve_commit(repo, worker.commit_tools(repo), dup)
    assert state == "ambiguous" and len(ids) == 2


def test_jj_probes_ignore_color_config(env, tmp_path, monkeypatch):
    if shutil.which("jj") is None:
        pytest.skip("jj not installed")
    sh, ids, _ = many_commits(env["repo"])
    sh("jj", "git", "init", "--colocate")
    conf = tmp_path / "jj-color.toml"
    conf.write_text('[ui]\ncolor = "always"\n')
    monkeypatch.setenv("JJ_CONFIG", str(conf))
    repo = str(env["repo"])
    assert worker.resolve_commit(repo, ["jj"], ids[0][:7]) == ("found", [ids[0]])
    assert worker.is_ancestor(repo, ["jj"], ids[-1], ids[0]) is True


def test_shallow_history_is_not_proof_of_a_non_ancestor(env, no_jj):
    sh, ids, _ = many_commits(env["repo"])
    repo = str(env["repo"])
    assert worker.is_ancestor(repo, ["git"], ids[-1], ids[0]) is True
    # Cut the history at a middle commit: the oldest one is still there, but git can't see the path.
    (env["repo"] / ".git" / "shallow").write_text(ids[1] + "\n")
    assert sh("git", "rev-parse", "--is-shallow-repository") == "true"
    assert worker.is_ancestor(repo, ["git"], ids[-1], ids[0]) is None


def test_colocated_ancestry_falls_back_to_jj(env):
    """Git can't answer for jj's synthetic root commit; jj can."""
    if shutil.which("jj") is None:
        pytest.skip("jj not installed")
    sh, ids, _ = many_commits(env["repo"])
    sh("jj", "git", "init", "--colocate")
    repo, root = str(env["repo"]), "0" * 40
    assert worker.is_ancestor(repo, ["jj", "git"], ids[-1], root) is False
    assert worker.is_ancestor(repo, ["jj", "git"], root, ids[-1]) is True


def test_prefix_needs_every_tool_to_answer(env, monkeypatch):
    """A tool that can't answer may hold a second commit with the prefix: unknown, not jj's match."""
    if shutil.which("jj") is None:
        pytest.skip("jj not installed")
    sh, ids, _ = many_commits(env["repo"])
    sh("jj", "git", "init", "--colocate")
    repo = str(env["repo"])
    real = worker._resolve_with
    monkeypatch.setattr(worker, "_resolve_with", lambda r, tool, rev: ("unknown", []) if tool == "git"
                        else real(r, tool, rev))
    assert worker.resolve_commit(repo, ["jj", "git"], ids[0][:7]) == ("unknown", [])
    assert worker.resolve_commit(repo, ["jj", "git"], ids[0]) == ("found", [ids[0]])
