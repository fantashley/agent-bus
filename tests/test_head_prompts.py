"""#138: in a repo listed in self_hosted_prompts, a review pass takes its prompt text from
the head under review (read with ast, never imported); elsewhere nothing changes."""
import json
import os
import subprocess
from pathlib import Path

import pytest

from agent_bus import cli, headprompts, prompts
from agent_bus import config as cfg
from agent_bus.service import Service
from agent_bus.snapshot import Snapshot

from test_review_rounds import calls, git_repo, run_job

INSTALLED = Path(prompts.__file__).read_text()
OLD_LINE = "- low (non-blocking): style or naming, minor cleanup, speculative hardening, rare edge cases,"
NEW_LINE = "- low (non-blocking): HEAD-ONLY WORDING, style or naming, minor cleanup, rare edge cases,"


def changed(old: str = OLD_LINE, new: str = NEW_LINE, source: str = INSTALLED) -> str:
    assert source.count(old) == 1
    return source.replace(old, new)


# ------------------------------------------------------------ parsing without running it


def test_the_installed_prompts_parse_to_themselves():
    """Every allowlisted constant, CONVERGE's `literal + NEW_FINDINGS` included, reads back
    as the installed text: today's prompts.py is loadable as it stands."""
    texts = headprompts.parse(INSTALLED)
    assert set(texts) == set(headprompts.NAMES)
    assert all(texts[n] == getattr(prompts, n) for n in headprompts.NAMES)
    assert headprompts.parse(changed())["SEVERITY"] == prompts.SEVERITY.replace(OLD_LINE, NEW_LINE)


def test_the_verdict_format_and_preambles_are_never_taken_from_the_head():
    for name in ("VERDICT_FORMAT", "VERDICT_FOLLOWUP", "FORMAT_LABEL", "VERDICT_FORMAT_VERSION", "HEADLESS",
                 "AGY_HEADLESS", "PREAMBLE", "ROUND1", "DEBRIEF", "PROMPTS_HEAD", "PROMPTS_INSTALLED"):
        assert name not in headprompts.NAMES


@pytest.mark.parametrize("edit, why", [
    # Not a literal: a call, an f-string, a name from elsewhere.
    (("SEVERITY = \"\"\"\\", "SEVERITY = \"\".join([]) + \"\"\"\\"), "SEVERITY is missing or not a string literal"),
    (("FIRST_PASS = \"\"\"", "FIRST_PASS = f\"\"\""), "FIRST_PASS is missing or not a string literal"),
    (("CONFIRM = \"\"\"", "CONFIRM = __name__ + \"\"\""), "CONFIRM is missing or not a string literal"),
    # Bound again later, or somewhere a plain assignment isn't.
    (("def render_message", "SEVERITY = 'other'\n\n\ndef render_message"), "SEVERITY is missing"),
    (("from . import provenance", "from . import provenance as REVIEW"), "REVIEW is missing"),
    (("def render_message", "from .db import *\n\n\ndef render_message"), "a wildcard import"),  # antigravity-2
    (("def render_message", "X = (SNAPSHOT := 'x')\n\n\ndef render_message"), "SNAPSHOT is missing"),
    (("def render_message(m: dict)", "def render_message(m: dict, _=(USAGES := ''))"), "USAGES is missing"),
    (("def render_message", "def f():\n    global TIME_LIMIT\n\n\ndef render_message"), "TIME_LIMIT is missing"),
    (("def render_message", "class SEVERITY:\n    pass\n\n\ndef render_message"), "SEVERITY is missing"),
    # Any top-level statement other than imports, assignments and definitions (codex-4).
    (("def render_message", "SEVERITY += 'x'\n\n\ndef render_message"), "top-level AugAssign"),
    (("def render_message", "if True:\n    TIME_LIMIT = 'x {limit}'\n\n\ndef render_message"), "top-level If"),
    (("def render_message", "for SNAPSHOT in ():\n    pass\n\n\ndef render_message"), "top-level For"),
    (("def render_message", "del USAGES\n\n\ndef render_message"), "top-level Delete"),
    (("def render_message", "try:\n    pass\nexcept Exception as SEVERITY:\n    pass\n\n\ndef render_message"),
     "top-level Try"),
    (("def render_message", "match 1:\n    case SEVERITY:\n        pass\n\n\ndef render_message"), "top-level Match"),
    (("def render_message", "print('hi')\n\n\ndef render_message"), "top-level Expr"),
    # Text the worker or a peer CLI can't use (codex-3).
    (("SEVERITY = \"\"\"\\", "SEVERITY = ' '\n_OLD_SEVERITY = \"\"\"\\"), "SEVERITY is empty"),
    (("PURE_REBASE = \"\"\"", "PURE_REBASE = \"\\0\" + \"\"\""), "PURE_REBASE is empty, or has a NUL"),
    (("PURE_REBASE = \"\"\"", "PURE_REBASE = \"\\ud800\" + \"\"\""), "PURE_REBASE is empty, or has a NUL"),
    # Removed.
    (("PURE_REBASE = \"\"\"", "_PURE_REBASE = \"\"\""), "PURE_REBASE is missing"),
    # Fields the installed worker doesn't fill, or drops one it relies on.
    (("Change: {change_id}", "Change: {change_id} {extra}"), "REVIEW takes other fields"),
    (("{verdict_format}\n\"\"\"\n\n\n# A debrief", "\n\"\"\"\n\n\n# A debrief"), "REVIEW takes other fields"),
    (("Change: {change_id}", "Change: {change_id.__class__}"), "REVIEW takes other fields"),
    (("Change: {change_id}", "Change: {change_id} {diff}"), "REVIEW takes other fields"),  # (codex-2)
    (("Change: {change_id}", "Change: {change_id!r}"), "conversion or format spec"),
    (("Change: {change_id}", "Change: {change_id:>9}"), "conversion or format spec"),
    (("Change: {change_id}", "Change: {change_id"), "unbalanced braces"),
    (("Change: {change_id}", "Change: {change_id}}"), "unbalanced braces"),
    # Not Python.
    (("def render_message", "def render_message(:\n\n\ndef x"), "not valid Python"),
])
def test_what_makes_the_head_prompts_not_loadable(edit, why):
    with pytest.raises(headprompts.NotLoadable, match=why.replace("(", r"\(")):
        headprompts.parse(changed(*edit))


def test_a_growing_concatenation_is_capped():
    blowup = "".join(f"_B{i} = _B{i - 1} + _B{i - 1}\n" for i in range(1, 40))
    source = changed("PURE_REBASE = \"\"\"", "_B0 = 'xx'\n" + blowup + "PURE_REBASE = _B39 + \"\"\"")
    with pytest.raises(headprompts.NotLoadable, match="PURE_REBASE is missing"):
        headprompts.parse(source)


def test_a_long_flat_concatenation_falls_back_rather_than_failing():
    """codex-1: a left-deep + chain of short literals is nested too deeply to evaluate."""
    chain = " + ".join(["'a'"] * 5000)
    with pytest.raises(headprompts.NotLoadable, match="nested too deeply|PURE_REBASE is missing"):
        headprompts.parse(changed("PURE_REBASE = \"\"\"", f"PURE_REBASE = {chain} + \"\"\""))


def test_all_the_strings_together_are_capped(tmp_path):
    """codex-2: many distinct near-limit strings, even unused ones, can't pile up.  Built
    from one shared literal, so the file is under the 256 KiB file cap (#179, codex-7) and
    it is the 1 MiB total that refuses it."""
    big = f"_X = '{'x' * 100_000}'\n" + "".join(f"_U{i} = '{i}' + _X\n" for i in range(12))
    source = changed("PURE_REBASE = \"\"\"", big + "PURE_REBASE = \"\"\"")
    assert len(source.encode()) <= headprompts.MAX_BYTES
    target = tmp_path / headprompts.PATH
    target.parent.mkdir(parents=True)
    target.write_text(source)
    snap = Snapshot(head="h" * 40, path=str(tmp_path), files=[headprompts.PATH])
    with pytest.raises(headprompts.NotLoadable, match="add up to over 1024 KiB"):
        headprompts.load(snap)


def test_load_reads_only_a_regular_file_in_the_snapshot(tmp_path):
    snap_dir = tmp_path / "snap"
    target = snap_dir / headprompts.PATH
    target.parent.mkdir(parents=True)
    snap = Snapshot(head="h" * 40, path=str(snap_dir), files=[headprompts.PATH])
    with pytest.raises(headprompts.NotLoadable, match="no read-only copy"):
        headprompts.load(Snapshot(head="h" * 40, reason="off"))
    with pytest.raises(headprompts.NotLoadable, match="has no src/agent_bus/prompts.py"):
        headprompts.load(Snapshot(head="h" * 40, path=str(snap_dir), files=[]))
    outside = tmp_path / "outside.py"
    outside.write_text(INSTALLED)
    target.symlink_to(outside)
    with pytest.raises(headprompts.NotLoadable, match="could not be read"):
        headprompts.load(snap)
    target.unlink()
    target.write_bytes(b"x" * (headprompts.MAX_BYTES + 1))
    with pytest.raises(headprompts.NotLoadable, match="over 256 KiB"):
        headprompts.load(snap)
    target.write_bytes(b"\xff\xfe")
    with pytest.raises(headprompts.NotLoadable, match="not UTF-8"):
        headprompts.load(snap)
    target.write_text(changed())
    assert NEW_LINE in headprompts.load(snap)["SEVERITY"]


# ------------------------------------------------------------ config


def write_config(env, **extra):
    path = env["tmp"] / "config" / "agent-bus" / "config.json"
    path.write_text(json.dumps({**json.loads(path.read_text()), **extra}))


def test_self_hosted_prompts_config(env, tmp_path):
    assert cfg.Config.load().self_hosted(env["repo"]) is False  # off by default
    ws = tmp_path / "ws"
    # #181: self_hosted_reviews also takes the head's prompt text; only it runs the head's build
    # (codex-1: the older key never grants running the head's code).
    for key in ("self_hosted_prompts", "self_hosted_reviews"):
        write_config(env, **{key: [str(env["repo"]), str(ws)]})
        conf = cfg.Config.load()
        assert conf.self_hosted(env["repo"]) and conf.self_hosted(ws / "task")
        assert not conf.self_hosted(tmp_path) and not conf.self_hosted(str(env["repo"]) + "-other")
        assert conf.head_builds(env["repo"]) is (key == "self_hosted_reviews")
        for bad in ("~/src/agent-bus", [""], [1]):
            write_config(env, **{key: bad})
            with pytest.raises(ValueError, match=key):
                cfg.Config.load()
        write_config(env, **{key: []})
    write_config(env, self_hosted_reviews=[str(ws)], self_hosted_prompts=[str(env["repo"])])
    conf = cfg.Config.load()
    assert conf.self_hosted(env["repo"]) and conf.self_hosted(ws)
    write_config(env, self_hosted_reviews=[str(ws)], self_hosted_build_timeout_s=0)
    conf = cfg.Config.load()
    assert conf.self_hosted(ws) and not conf.head_builds(ws)  # builds off: prompts only (#138)
    for bad in (-1, True, "600", 1.5):
        write_config(env, self_hosted_build_timeout_s=bad)
        with pytest.raises(ValueError, match="self_hosted_build_timeout_s"):
            cfg.Config.load()
    write_config(env, self_hosted_build_timeout_s=0)


# ------------------------------------------------------------ a review with a real head


def prompts_repo(repo, head_source: str):
    """A git repo whose head changes src/agent_bus/prompts.py from the installed text."""
    base, commit = git_repo(repo)
    target = repo / headprompts.PATH
    target.parent.mkdir(parents=True)
    target.write_text(INSTALLED)
    genv = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
                GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")
    git = lambda *a: subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True,  # noqa: E731
                                    text=True, env=genv).stdout.strip()
    git("add", headprompts.PATH)
    git("commit", "-qm", "add prompts")
    base = git("rev-parse", "HEAD")
    target.write_text(head_source)
    git("commit", "-qam", "change a prompt")
    return base, git("rev-parse", "HEAD")


def review(env, head_source: str):
    bus, repo = env["bus"], env["repo"]
    base, head = prompts_repo(repo, head_source)
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review("codex", str(repo), base, head, change_id="PR-1")["thread"]["id"]
    run_job(bus, tid)
    env["tid"] = tid
    prompt = calls(env)[-1]["prompt"]
    events = [e["text"] for e in bus.events(tid) if e["kind"] == "prompts"]
    return prompt, events, head


def test_an_opted_in_review_shows_the_heads_changed_prompt(env, no_spawn, capsys):
    """The issue's acceptance case: the reviewer sees the head's SEVERITY text, and is told
    whose prompts these are; the verdict format stays the installed one."""
    write_config(env, self_hosted_prompts=[str(env["repo"])])
    prompt, events, head = review(env, changed())
    assert NEW_LINE in prompt.split("```diff")[0] + prompt.split("```\n")[-1]  # in the rules, not just the diff
    assert prompts.VERDICT_FORMAT in prompt
    assert f"this review's prompt text is head {head[:12]}'s, read from its src/agent_bus/prompts.py" in prompt
    assert events == [f"review prompts from head {head[:12]}'s src/agent_bus/prompts.py"]
    # antigravity-1: `agent-bus tail` shows it.
    for flag in ("--events", "--events-only"):
        assert cli.main(["tail", env["tid"], flag, "--once", "--no-color"]) == 0
        assert f"worker prompts: review prompts from head {head[:12]}'s" in capsys.readouterr().out


def test_without_the_opt_in_nothing_changes(env, no_spawn):
    prompt, events, _ = review(env, changed())
    rules = prompt.split("```diff")[0] + prompt.split("```\n")[-1]
    assert NEW_LINE not in rules and prompts.SEVERITY in rules
    assert "(Prompts:" not in prompt and events == []


def test_unloadable_head_prompts_fall_back_and_say_so(env, no_spawn):
    write_config(env, self_hosted_prompts=[str(env["repo"])])
    source = changed("SEVERITY = \"\"\"\\", "SEVERITY = \"\".join([]) + \"\"\"\\", changed())
    prompt, events, head = review(env, source)
    rules = prompt.split("```diff")[0] + prompt.split("```\n")[-1]
    assert NEW_LINE not in rules and prompts.SEVERITY in rules
    why = "SEVERITY is missing or not a string literal"
    assert f"but head {head[:12]}'s could not be loaded ({why}), so this review uses agent-bus" in prompt
    assert events == [f"installed review prompts: head {head[:12]}'s could not be loaded ({why})"]
