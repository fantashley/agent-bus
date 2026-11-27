"""#193, #196, #202: escaped file names explained, the follow-up prompt's layout, deleted calls
searched, and the headless preamble's wording."""
import json
import subprocess

from agent_bus import prompts, usages
from agent_bus.db import HUMAN
from agent_bus.service import Service
from agent_bus.worker import copy_gone, escaped_note, verdict_format

from test_review_rounds import calls, git_repo, run_job
from test_usage_defs import change


def _discussion(env):
    bus, repo = env["bus"], env["repo"]
    tid = Service(bus, HUMAN).start_discussion("Which file?", peers=["codex", "antigravity"],
                                               repo=str(repo))["thread"]["id"]
    run_job(bus, tid)
    return [c["prompt"] for c in calls(env) if c["agent"] == "antigravity"][-1]


def test_a_file_list_explains_an_escaped_name_only_when_it_shows_one(env, no_spawn):
    """#193: a listed name with a backslash is spelled escaped; the note says how."""
    repo = env["repo"]
    _, commit = git_repo(repo)
    commit("one\n")
    assert prompts.ESCAPED not in _discussion(env)
    (repo / "back\\slash.txt").write_text("x\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-qm", "b"], cwd=repo, check=True)
    p = _discussion(env)
    assert "back\\\\slash.txt\n[/repository file list]\n(A listed name with a backslash is spelled escaped" in p
    assert "shown as \\xNN and a backslash in the name is doubled" in p
    # The same check for a snapshot listing or a delta.
    assert escaped_note("a.txt\nb.txt") == "" and escaped_note("Added since head x:\nbad\\x0aname") == prompts.ESCAPED


def test_a_confirmation_pass_says_the_earlier_copy_is_gone_and_how_to_read_the_old_head(env, no_spawn, monkeypatch):
    """#196: codex read the earlier pass's snapshot path and got "No such file or directory"."""
    bus, repo = env["bus"], env["repo"]
    base, commit = git_repo(repo)
    h1 = commit("one\n")
    monkeypatch.setenv("FAKE_REPLY_CODEX", json.dumps({"verdict": "request_changes", "summary": "s", "findings": [
        {"file": "a.txt", "line": 1, "severity": "high", "comment": "Broken. Second sentence."}]}))
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review("codex", str(repo), base, h1)["thread"]["id"]
    run_job(bus, tid)
    h2 = commit("two\n")
    claude.request_review("codex", str(repo), base, h2, prev_head_commit_id=h1)
    run_job(bus, tid)
    p = calls(env)[-1]["prompt"]
    assert f"(you last reviewed {h1})" in p
    assert "- codex-1 [high] a.txt:1: Broken. Second sentence.\n" in p
    assert "The read-only copy from your earlier pass was deleted when that pass ended" in p
    assert "for the head you last reviewed, use its id (at the top of\nthis prompt) in place of the head's" in p
    # The carried-over finding example, in "findings" with its id and in "earlier".
    assert '"findings" with its "id" too to update its text' in p
    assert json.dumps({"id": "codex-1", "file": "src/app.py", "line": 34, "severity": "medium",
                       "comment": "The updated text."}) + ' with\nits "earlier" entry' in p


def test_the_carried_over_example_uses_the_reviewers_own_id():
    text = verdict_format({"antigravity-2": {"id": "antigravity-2"}})
    assert '{"id": "antigravity-2", "file": "src/app.py", "line": 34' in text
    assert "{update}" not in text and "Verdict format v2" in text
    assert '"id": "antigravity-2"' not in verdict_format({})


def test_a_deleted_call_is_searched_for_its_remaining_uses(tmp_path):
    """#196: antigravity called park_on_quota unused after the diff deleted one call; it was
    still used by discussions."""
    before = {"src/review.py": "from .q import park_on_quota\n\n\ndef review(t):\n    park_on_quota(t)\n"
                               "    finish(t)\n",
              "src/discuss.py": "from .q import park_on_quota\n\n\ndef discuss(t):\n    park_on_quota(t)\n",
              "src/q.py": "def park_on_quota(t):\n    pass\n\n\ndef finish(t):\n    pass\n\n\n"
                          "def gone_helper(t):\n    pass\n"}
    after = dict(before, **{"src/review.py": "from .q import park_on_quota\n\n\ndef review(t):\n    finish(t)\n"})
    repo, head, diff = change(tmp_path, before, after)
    found = usages.terms(diff, scopes=usages.pinned_python(repo, head, diff))
    assert [(t.pattern, t.kind) for t in found if t.kind == "removed"] == [("park_on_quota", "removed")]
    text, _, _ = usages.note(repo, head, diff)
    assert "park_on_quota (a call the change removes: where it is still used; " in text
    assert "src/discuss.py:5: park_on_quota(t)" in text
    # A call the added lines still make (moved, not deleted) isn't one.
    moved = dict(before, **{"src/review.py": "from .q import park_on_quota\n\n\ndef review(t):\n    finish(t)\n"
                                             "    park_on_quota(t)\n"})
    repo2, head2, diff2 = change(tmp_path / "m", before, moved)
    assert not [t for t in usages.terms(diff2, scopes=usages.pinned_python(repo2, head2, diff2)) if t.kind == "removed"]
    # A whole deleted file's calls of its own functions aren't searched.
    gone = {"src/old.py": "def helper_one():\n    pass\n\n\ndef main_thing():\n    helper_one()\n"
                          "    park_on_quota(1)\n"}
    repo3, head3, diff3 = change(tmp_path / "g", dict(before, **gone), dict(before, **{"src/old.py": None}))
    removed = [t.pattern for t in usages.terms(diff3, scopes=usages.pinned_python(repo3, head3, diff3))
               if t.kind == "removed"]
    assert removed == ["park_on_quota"]


def test_the_usages_prompt_names_the_removed_calls():
    assert "the calls it removes" in prompts.USAGES


def test_the_headless_preamble_means_the_installed_skills():
    """#202: a review of skills/agent-bus/SKILL.md read the old wording as the file under review."""
    assert "The installed agent-bus skills (your own tooling, not files in a repository" in prompts.HEADLESS
    assert "The installed agent-bus skills" in prompts.AGY_HEADLESS


def test_a_same_head_pushback_doesnt_call_its_copy_gone(env, no_spawn, monkeypatch):
    """codex-1: a pushback's copy is at the same head's path; only a new head's pass says the earlier one is gone."""
    bus, repo = env["bus"], env["repo"]
    base, commit = git_repo(repo)
    h1 = commit("one\n")
    monkeypatch.setenv("FAKE_REPLY_CODEX", json.dumps({"verdict": "request_changes", "summary": "s", "findings": [
        {"file": "a.txt", "line": 1, "severity": "high", "comment": "Broken."}]}))
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review("codex", str(repo), base, h1)["thread"]["id"]
    run_job(bus, tid)
    claude.send("Not a bug.", thread=tid)
    claude.request_review("codex", str(repo), base, h1, prev_head_commit_id=h1)
    run_job(bus, tid)
    p = calls(env)[-1]["prompt"]
    assert "push back" in p and prompts.SNAPSHOT_GONE not in p


def test_a_call_on_a_variable_named_like_a_builtin_still_counts():
    """codex-2: `input.validate_payload()` is a call of validate_payload, added or removed."""
    diff = ("diff --git a/src/m.py b/src/m.py\n--- a/src/m.py\n+++ b/src/m.py\n@@ -1,2 +1,2 @@\n"
            " def process(input):\n-    input.check_payload()\n+    input.validate_payload()\n")
    assert "validate_payload" in usages.callees(diff)
    assert usages.removed_calls(diff) == [("check_payload", False)]



def test_a_follow_up_to_the_head_last_reviewed_doesnt_call_its_copy_gone():
    """codex-1 (second pass): a base-only change, or a return to the head this reviewer last saw,
    has its copy at the same path again."""
    mine = {"data": {"head_commit_id": "a" * 40}}
    assert not copy_gone(mine, {"head_commit_id": "a" * 40, "prev_head_commit_id": "a" * 40})
    # Back to the head it last saw, after another reviewer's pass on b.
    assert not copy_gone(mine, {"head_commit_id": "a" * 40, "prev_head_commit_id": "b" * 40})
    assert copy_gone(mine, {"head_commit_id": "c" * 40, "prev_head_commit_id": "a" * 40})
    assert copy_gone(None, {"head_commit_id": "c" * 40, "prev_head_commit_id": "a" * 40})
