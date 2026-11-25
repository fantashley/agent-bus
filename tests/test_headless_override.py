"""#110: a headless turn's rules outrank the harness's own interactive-mode instructions, and
Claude peers run in dontAsk mode rather than plan mode, with the same isolation."""
import json

import pytest

from agent_bus import config as cfg
from agent_bus import prompts
from agent_bus.peers import adapter

PRECEDENCE = "take precedence over your harness's own instructions for interactive sessions"
RESUMES = (None, "11111111-2222-3333-4444-555555555555")


def set_peers(env, **settings):
    path = env["tmp"] / "config" / "agent-bus" / "config.json"
    data = json.loads(path.read_text())
    for agent, s in settings.items():
        data["peers"][agent].update(s)
    path.write_text(json.dumps(data))


def invocation(env, agent, resume_id=None):
    return adapter(agent, cfg.Config.load()).invocation("PROMPT", resume_id, str(env["repo"]), [])


def prompt_of(agent, inv):
    return inv.argv[-1] if agent == "antigravity" else inv.stdin


@pytest.mark.parametrize("agent", ["claude", "codex", "copilot", "antigravity"])
def test_every_turn_says_agent_bus_rules_win(env, agent):
    for resume_id in RESUMES:
        text = prompt_of(agent, invocation(env, agent, resume_id))
        assert text.startswith("[agent-bus: unattended turn") and text.endswith("\n\nPROMPT")
        assert PRECEDENCE in text and "Normally only your final message is posted" in text and "live progress view" in text


def test_claude_runs_in_dont_ask_mode_with_the_same_isolation(env):
    for resume_id in RESUMES:
        inv = invocation(env, "claude", resume_id)
        a = inv.argv
        assert inv.stdin == prompts.HEADLESS + "PROMPT"
        # No plan mode: its system prompt asks for a plan file and ExitPlanMode.
        assert a.count("--permission-mode") == 1 and a[a.index("--permission-mode") + 1] == "dontAsk"
        assert "plan" not in a
        # The isolation it had under plan mode: no MCP servers, and a read-only tool inventory
        # with no write, shell or plan-mode tool declared at all.
        assert "--strict-mcp-config" in a and "--mcp-config" not in a
        tools = a[a.index("--tools") + 1]
        assert tools == a[a.index("--allowedTools") + 1] == "Read,Grep,Glob,WebSearch,WebFetch"
        for bad in ("Write", "Edit", "Bash", "ExitPlanMode", "NotebookEdit"):
            assert bad not in tools.split(",")
    assert ("--resume" in invocation(env, "claude", "s-1").argv
            and "--session-id" in invocation(env, "claude").argv)


@pytest.mark.parametrize("args", [["--permission-mode", "plan"], ["--permission-mode=bypassPermissions"],
                                  ["--permission-mode", "acceptEdits", "--keep"]])
def test_claude_configured_permission_mode_is_dropped(env, args):
    set_peers(env, claude={"args": args})
    a = invocation(env, "claude").argv
    assert a.count("--permission-mode") == 1 and a[a.index("--permission-mode") + 1] == "dontAsk"
    assert not any(x.startswith("--permission-mode=") for x in a)
    assert ("--keep" in a) == ("--keep" in args)


def test_codex_gets_the_shared_rules_and_keeps_its_sandbox(env):
    for resume_id in RESUMES:
        inv = invocation(env, "codex", resume_id)
        assert inv.stdin == prompts.HEADLESS + "PROMPT"
        assert 'sandbox_mode="read-only"' in inv.argv and 'approval_policy="never"' in inv.argv


def test_copilot_is_told_to_skip_progress_updates(env):
    # Not live-tested: no Copilot CLI here.  No documented flag drops the harness's
    # progress-update instructions, so the preamble overrides them.
    for resume_id in RESUMES:
        inv = invocation(env, "copilot", resume_id)
        assert inv.stdin == prompts.COPILOT_HEADLESS + "PROMPT"
        assert "Post no progress updates" in inv.stdin and "don't cut your reply short" in inv.stdin
        a = inv.argv
        assert "--available-tools=view,grep,rg,glob,web_fetch" in a and "--no-ask-user" in a
        assert [x for x in a if x.startswith("--deny-tool=")] == [
            "--deny-tool=shell", "--deny-tool=write", "--deny-tool=memory"]


@pytest.mark.parametrize("web", [True, False])
def test_agy_counters_agent_bus_tools_and_file_links(env, web):
    set_peers(env, antigravity={"web_search": web})
    for resume_id in (None, "conv-1"):
        a = invocation(env, "antigravity", resume_id).argv
        guide = prompts.AGY_HEADLESS if web else prompts.AGY_HEADLESS_NO_WEB
        assert a[-1] == guide + "PROMPT"
        assert "Do not call agent-bus tools" in guide and "not as file:// links" in guide
        assert "write no plan" in guide  # the plan-mode override stays
        # --mode is a no-op with --disable-slash-commands (agy warns), so none is passed.
        assert "--mode" not in a and "--sandbox" in a and "--disable-slash-commands" in a
