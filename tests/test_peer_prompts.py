"""#93: what headless peers are shown, as the peers themselves reported it."""
import json
import shutil
import string
import subprocess

import pytest

from agent_bus import prompts
from agent_bus.db import HUMAN
from agent_bus.peers import extract_json
from agent_bus.service import Service
from agent_bus.worker import Worker, diff_overview, normalize_verdict, repair_reason, verdict_problem

from test_review_rounds import BOTH, calls, git_repo, run_job


def no_doubled_braces(prompt: str) -> None:
    assert "{{" not in prompt and "}}" not in prompt, prompt


def test_every_template_renders_without_doubled_braces():
    """A .format() template's {{ }} only turn into braces once formatted: used raw, the peer
    sees them doubled (ROUND1 was).  Render each the way the worker does."""
    for name in dir(prompts):
        value = getattr(prompts, name)
        if not name.isupper() or not isinstance(value, str):
            continue
        try:  # as the worker uses it: formatted if it is a template, else raw
            fields = {f for _, f, _, _ in string.Formatter().parse(value) if f}
            rendered = value.format(**{f: "x" for f in fields}) if fields else value
        except (ValueError, KeyError, IndexError):
            rendered = value  # single braces that aren't fields: a raw string
        no_doubled_braces(rendered)


def test_discussion_and_review_prompts_never_show_doubled_braces(env, no_spawn, monkeypatch):
    bus, repo = env["bus"], env["repo"]
    tid = Service(bus, HUMAN).start_discussion("Which DB?", peers=["codex", "antigravity"])["thread"]["id"]
    run_job(bus, tid)
    base, commit = git_repo(repo)
    head = commit("one\n")
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review(None, str(repo), base, head, reviewers=BOTH)["thread"]["id"]
    run_job(bus, tid)
    # And the repair turns of both.
    monkeypatch.setenv("FAKE_BEHAVIOUR", "nojson")
    tid = Service(bus, HUMAN).start_discussion("Again?", peers=["codex", "antigravity"])["thread"]["id"]
    run_job(bus, tid)
    tid = claude.request_review("antigravity", str(repo), base, commit("two\n"))["thread"]["id"]
    run_job(bus, tid)
    seen = calls(env)
    assert any("round 1, which is blind" in c["prompt"] for c in seen)
    assert any("Reply again with only" in c["prompt"] for c in seen)
    assert any("Diff from base to head" in c["prompt"] for c in seen)
    for c in seen:
        no_doubled_braces(c["prompt"])
    r1 = next(c["prompt"] for c in seen if "round 1, which is blind" in c["prompt"])
    assert '{"action": "propose", "proposal":' in r1 and "Only its fields are posted" in r1


def test_verdict_example_is_valid_json_and_a_valid_verdict():
    example = next(l for l in prompts.VERDICT_FORMAT.splitlines() if l.startswith("{"))
    v = normalize_verdict(json.loads(example))
    assert v and verdict_problem(v) is None
    assert "Only the JSON" in prompts.VERDICT_FORMAT and "|" not in prompts.VERDICT_FORMAT
    for template in (prompts.REPAIR, prompts.REPAIR_BLIND):
        for line in template.splitlines():
            if line.startswith("{"):
                json.loads(line)


def test_multi_line_verdict_after_prose_is_parsed():
    reply = ('Here is my review.\n\n```json\n{\n  "verdict": "request_changes",\n  "summary": "a {brace} in text",\n'
             '  "findings": [\n    {"file": "a.py", "line": 3, "severity": "high", "comment": "bug"}\n  ]\n}\n```\n')
    v = normalize_verdict(extract_json(reply))
    assert v["verdict"] == "request_changes" and v["findings"][0]["line"] == 3


def test_repair_reasons():
    assert repair_reason(None, None) == "no JSON object in the reply"
    assert "verdict" in repair_reason({"verdict": "lgtm"}, None)
    assert "findings" in repair_reason({"verdict": "approve", "findings": {}}, None)
    assert repair_reason({}, "every finding needs a severity").startswith("inconsistent verdict")


def test_repair_turns_log_why(env, no_spawn, monkeypatch):
    bus, repo = env["bus"], env["repo"]
    monkeypatch.setenv("FAKE_BEHAVIOUR", "nojson")
    base, commit = git_repo(repo)
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review("codex", str(repo), base, commit("one\n"))["thread"]["id"]
    run_job(bus, tid)
    events = [(e["kind"], e["text"]) for e in bus.events(tid)]
    assert ("repair", "asking again for the verdict: no JSON object in the reply") in events
    tid = Service(bus, HUMAN).start_discussion("Q?", peers=["codex", "antigravity"])["thread"]["id"]
    run_job(bus, tid)
    assert ("repair", "asking again for the move: no JSON object in the reply") in [
        (e["kind"], e["text"]) for e in bus.events(tid)]


def test_review_prompt_leads_with_files_commits_and_testing(env, no_spawn, monkeypatch):
    bus, repo = env["bus"], env["repo"]
    monkeypatch.setenv("FAKE_APPROVE", "codex,antigravity")
    base, commit = git_repo(repo)
    (repo / "new.txt").write_text("x\ny\n")
    import subprocess
    subprocess.run(["git", "add", "new.txt"], cwd=repo, check=True)
    head = commit("changed\n")
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review(None, str(repo), base, head, reviewers=BOTH,
                                testing="pytest -q: 400 passed")["thread"]["id"]
    assert bus.thread(tid)["refs"]["testing"] == "pytest -q: 400 passed"
    run_job(bus, tid)
    codex = calls(env)[0]
    p = codex["prompt"]
    assert codex["agent"] == "codex"
    files = p.index("Changed files in this change:")
    assert files < p.index("```diff")
    assert "2 files, +3 -1" in p and "  A new.txt  +2 -0" in p and "  M a.txt  +1 -1" in p
    assert f"{head[:12]} changed" in p and "Commit messages, base..head" in p
    assert "Testing done, as claude reports it" in p and "pytest -q: 400 passed" in p
    assert f"Diff from base to head (base {base[:12]} to head {head[:12]}), the whole change, because this is your first pass" in p
    assert "time limit of about 1 minute." in p
    # codex has a shell: copyable read-only commands for the pinned head.
    assert f"git -C {repo} --no-replace-objects show {head}:<path>" in p
    assert "jj -R" not in p  # a plain git repo: no jj commands (#76)
    # antigravity has only read_file: it reads the pinned head's snapshot (#111).
    agy = calls(env)[1]
    assert agy["agent"] == "antigravity" and "git -C" not in agy["prompt"]
    assert f"are exactly head {head}" in agy["prompt"] and "may not match the head commit" not in agy["prompt"]
    for c in calls(env):
        no_doubled_braces(c["prompt"])


def test_review_prompt_gives_a_shell_reviewer_read_only_jj_commands_in_a_jj_repo(env, no_spawn, monkeypatch):
    """#76: in a read-only sandbox a jj command without --ignore-working-copy fails on its
    snapshot and git import locks, and @ is the workspace's own commit, not the head."""
    if shutil.which("jj") is None:
        pytest.skip("jj not installed")
    monkeypatch.setenv("FAKE_APPROVE", "codex,antigravity")
    bus, repo = env["bus"], env["repo"]
    base, commit = git_repo(repo)
    head = commit("one\n")
    subprocess.run(["jj", "git", "init", "--colocate"], cwd=repo, check=True, capture_output=True)
    claude = Service(bus, "claude", bus.register_session("claude"))
    run_job(bus, claude.request_review(None, str(repo), base, head, reviewers=BOTH)["thread"]["id"])
    codex, agy = calls(env)[:2]
    p = codex["prompt"]
    assert f"git -C {repo} --no-replace-objects show {head}:<path>" in p
    assert f"""jj -R {repo} --ignore-working-copy file show -r 'commit_id("{head}")' 'root:"<path>"'""" in p
    assert f"""--ignore-working-copy diff --git --from 'commit_id("{base}")' --to 'commit_id("{head}")'""" in p
    assert f"""jj -R {repo} --ignore-working-copy log -r 'commit_id("{base}")..commit_id("{head}")'""" in p
    assert "never @" in p
    assert agy["agent"] == "antigravity" and "jj -R" not in agy["prompt"] and "git -C" not in agy["prompt"]
    no_doubled_braces(p)


def test_first_time_reviewer_on_a_later_head_gets_the_whole_change(env, no_spawn, monkeypatch):
    """antigravity reported getting only an interdiff on its first pass at a later head: it
    gets the full diff, and the prompt now says why."""
    bus, repo = env["bus"], env["repo"]
    base, commit = git_repo(repo)
    h1 = commit("helo\n")
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review(None, str(repo), base, h1, reviewers=BOTH)["thread"]["id"]
    run_job(bus, tid)  # codex asks for changes
    h2 = commit("hello\n")
    claude.request_review(None, str(repo), base, h2, prev_head_commit_id=h1, reviewers=BOTH)
    monkeypatch.setenv("FAKE_APPROVE", "codex,antigravity")
    run_job(bus, tid)
    agy, codex2 = calls(env)[1:3]
    assert agy["agent"] == "antigravity" and "--conversation" not in agy["argv"]
    assert "Interdiff" not in agy["prompt"]
    assert "the whole change, because this is your first pass on this change" in agy["prompt"]
    # codex resumes with a labelled interdiff and the files it touches.
    assert (f"Interdiff since the head you last reviewed (old head {h1[:12]} to new head {h2[:12]}, "
            f"same base {base[:12]})") in codex2["prompt"]
    assert "Changed files in this interdiff:" in codex2["prompt"]


def test_diff_overview_counts_and_statuses():
    diff = ("diff --git a/a.py b/a.py\nindex 1..2 100644\n--- a/a.py\n+++ b/a.py\n@@ -1 +1,2 @@\n-x\n+y\n+++z\n"
            "diff --git a/old.txt b/new.txt\nsimilarity index 100%\nrename from old.txt\nrename to new.txt\n"
            "diff --git a/gone b/gone\ndeleted file mode 100644\n--- a/gone\n+++ /dev/null\n@@ -1 +0,0 @@\n-bye\n"
            "diff --git a/img.png b/img.png\nnew file mode 100644\nBinary files /dev/null and b/img.png differ\n")
    assert diff_overview(diff) == ("4 files, +2 -2\n  M a.py  +2 -1\n  R new.txt (from old.txt)  +0 -0\n"
                                   "  D gone  +0 -1\n  A img.png  binary")
    assert diff_overview("(empty diff)") == ""


def test_truncated_diff_still_lists_every_file(env):
    bus = env["bus"]
    tid = Service(bus, HUMAN).start_discussion("Q?", peers=["codex"])["thread"]["id"]
    w = Worker(bus, bus.jobs(tid)[-1]["id"])
    w.config.max_diff_bytes = 200
    diff = "".join(f"diff --git a/f{i} b/f{i}\n--- a/f{i}\n+++ b/f{i}\n@@ -1 +1 @@\n-a\n+{'b' * 50}\n" for i in range(5))
    capped = w.cap_diff(diff)
    assert "f4" not in capped
    t = bus.thread(tid)
    overview = w.review_overview({**t, "initiator": "claude"},
                                 {"repo": str(env["repo"]), "base_commit_id": "0" * 40, "head_commit_id": "1" * 40},
                                 capped, "change")
    assert "5 files, +5 -5" in overview and "  M f4  +1 -1" in overview


@pytest.mark.parametrize("web", [True, False])
def test_agy_turns_disable_slash_commands_and_say_read_file_only(env, web):
    from agent_bus import config as cfg
    from agent_bus.peers import adapter
    conf = cfg.Config.load()
    conf.peers.setdefault("antigravity", {})["web_search"] = web
    inv = adapter("antigravity", conf).invocation("PROMPT", None, str(env["repo"]), [])
    assert "--disable-slash-commands" in inv.argv
    preamble = inv.argv[-1]
    assert "There is no directory listing or search" in preamble and "write no plan" in preamble


def test_diff_overview_reads_quoted_paths():
    diff = ('diff --git "a/caf\\303\\251.txt" "b/caf\\303\\251.txt"\nnew file mode 100644\n--- /dev/null\n'
            '+++ "b/caf\\303\\251.txt"\n@@ -0,0 +1 @@\n+x\n'
            'diff --git a/with space.txt b/with space.txt\n--- a/with space.txt\t\n+++ b/with space.txt\t\n'
            '@@ -1 +1 @@\n-a\n+b\n')
    assert diff_overview(diff) == "2 files, +2 -1\n  A café.txt  +1 -0\n  M with space.txt  +1 -1"


def test_overview_ignores_repo_diff_and_log_config(env, no_spawn, monkeypatch):
    """The reviewed repo's config can't drop files from the overview (diff.noprefix), break the
    commit log (a UTF-16 log encoding) or run a signature verifier (log.showSignature)."""
    import subprocess
    bus, repo = env["bus"], env["repo"]
    monkeypatch.setenv("FAKE_APPROVE", "codex")
    base, commit = git_repo(repo)
    (repo / "café.txt").write_text("x\n")
    subprocess.run(["git", "add", "café.txt"], cwd=repo, check=True)
    head = commit("changed\n")
    for k, v in (("diff.noprefix", "true"), ("diff.mnemonicPrefix", "true"), ("i18n.logOutputEncoding", "UTF-16"),
                 ("log.showSignature", "true"), ("gpg.program", str(env["tmp"] / "no-such-verifier"))):
        subprocess.run(["git", "config", k, v], cwd=repo, check=True)
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review("codex", str(repo), base, head)["thread"]["id"]
    run_job(bus, tid)
    p = calls(env)[0]["prompt"]
    assert "2 files, +2 -1" in p and "  A café.txt  +1 -0" in p and f"{head[:12]} changed" in p
    assert bus.thread(tid)["status"] == "resolved"


def test_full_diff_and_interdiff_keep_their_own_file_lists(env):
    bus = env["bus"]
    tid = Service(bus, HUMAN).start_discussion("Q?", peers=["codex"])["thread"]["id"]
    w = Worker(bus, bus.jobs(tid)[-1]["id"])
    w.config.max_diff_bytes = 60
    big = "diff --git a/big b/big\n--- a/big\n+++ b/big\n@@ -1 +1 @@\n-" + "a" * 80 + "\n+b\n"
    full = w.cap_diff(big + "diff --git a/f1 b/f1\n--- a/f1\n+++ b/f1\n@@ -1 +1 @@\n-a\n+b\n")
    inter = w.cap_diff(big + "diff --git a/f2 b/f2\n--- a/f2\n+++ b/f2\n@@ -1 +1 @@\n-a\n+b\n")
    assert full == inter  # the same text as sent...
    refs = {"repo": str(env["repo"]), "base_commit_id": "0" * 40, "head_commit_id": "1" * 40}
    t = {**bus.thread(tid), "initiator": "claude"}
    assert "f1" in w.review_overview(t, refs, full, "change") and "f2" not in w.review_overview(t, refs, full, "change")
    assert "f2" in w.review_overview(t, refs, inter, "interdiff")  # ...but each keeps its own files


def test_pushback_pass_shows_a_new_testing_note(env, no_spawn):
    bus, repo = env["bus"], env["repo"]
    base, commit = git_repo(repo)
    head = commit("one\n")
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review("codex", str(repo), base, head)["thread"]["id"]
    run_job(bus, tid)  # changes requested
    claude.send("It is not a typo: see the spec.", thread=tid)
    claude.request_review("codex", str(repo), base, head, prev_head_commit_id=head, testing="TESTED-IT: 3 passed")
    run_job(bus, tid)
    last = calls(env)[-1]
    assert "has not changed the code" in last["prompt"] and "TESTED-IT: 3 passed" in last["prompt"]
    assert "--conversation" not in last["argv"] and last["argv"][:3] == ["exec", "resume", "--json"]


def test_tail_shows_repair_events(env, no_spawn, monkeypatch, capsys):
    from agent_bus import cli
    bus, repo = env["bus"], env["repo"]
    monkeypatch.setenv("FAKE_BEHAVIOUR", "nojson")
    base, commit = git_repo(repo)
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review("codex", str(repo), base, commit("one\n"))["thread"]["id"]
    run_job(bus, tid)
    capsys.readouterr()
    assert cli.main(["tail", tid, "--events", "--once", "--no-status", "--no-color"]) == 0
    assert "codex repair: asking again for the verdict: no JSON object in the reply" in capsys.readouterr().out


@pytest.mark.parametrize("reviewers", [None, ["codex", "antigravity"]])
def test_pushback_without_a_note_keeps_the_heads_testing_note(env, no_spawn, reviewers):
    bus, repo = env["bus"], env["repo"]
    base, commit = git_repo(repo)
    head = commit("one\n")
    claude = Service(bus, "claude", bus.register_session("claude"))
    who = None if reviewers else "codex"
    tid = claude.request_review(who, str(repo), base, head, reviewers=reviewers, testing="KEEP-ME")["thread"]["id"]
    run_job(bus, tid)  # codex asks for changes
    claude.send("Not a bug: see the spec.", thread=tid)
    claude.request_review(who, str(repo), base, head, prev_head_commit_id=head, reviewers=reviewers)
    assert bus.thread(tid)["refs"]["testing"] == "KEEP-ME"
    run_job(bus, tid)
    assert "KEEP-ME" in calls(env)[-1]["prompt"]
    head2 = commit("two\n")
    claude.request_review(who, str(repo), base, head2, prev_head_commit_id=head, reviewers=reviewers)
    assert bus.thread(tid)["refs"]["testing"] is None  # a new head: the old note no longer applies
