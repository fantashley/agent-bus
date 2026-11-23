"""#46: headless peers search the web, with everything else still read-only."""
import json

import pytest

from agent_bus import cli
from agent_bus import config as cfg
from agent_bus.peers import StreamState, adapter
from agent_bus.prompts import AGY_HEADLESS, AGY_HEADLESS_NO_WEB


def set_peers(env, **settings):
    path = env["tmp"] / "config" / "agent-bus" / "config.json"
    data = json.loads(path.read_text())
    for agent, s in settings.items():
        data["peers"][agent].update(s)
    path.write_text(json.dumps(data))


def argv(env, agent, resume_id=None):
    return adapter(agent, cfg.Config.load()).invocation("PROMPT", resume_id, str(env["repo"]), []).argv


def configs(args):
    return [args[i + 1] for i, a in enumerate(args[:-1]) if a == "-c"]


def test_web_search_is_on_by_default_and_must_be_a_bool(env):
    conf = cfg.Config.load()
    assert all(conf.web_search(a) for a in cfg.AGENTS)
    set_peers(env, codex={"web_search": False})
    assert cfg.Config.load().web_search("codex") is False
    set_peers(env, codex={"web_search": "no"})
    with pytest.raises(ValueError, match="peers.codex.web_search"):
        cfg.Config.load()


def test_claude_tool_inventory_follows_the_switch(env):
    set_peers(env, claude={"args": ["--tools", "Bash", "--tools=Bash", "--keep"]})
    for resume_id in (None, "s-1"):
        on = argv(env, "claude", resume_id)
        # --tools is the whole built-in set (no Bash, CronCreate…), and a configured one is dropped.
        assert on.count("--tools") == 1 and "--tools=Bash" not in on and "Bash" not in on and "--keep" in on
        assert on[on.index("--tools") + 1] == on[on.index("--allowedTools") + 1] == "Read,Grep,Glob,WebSearch,WebFetch"
        assert "--disallowedTools" not in on
    set_peers(env, claude={"web_search": False})
    off = argv(env, "claude")
    assert off[off.index("--tools") + 1] == off[off.index("--allowedTools") + 1] == "Read,Grep,Glob"
    assert off[off.index("--disallowedTools") + 1] == "WebSearch,WebFetch"


def test_codex_apps_always_off_and_web_search_switchable(env):
    for resume_id in (None, "thread-1"):
        cs = configs(argv(env, "codex", resume_id))
        assert "features.apps=false" in cs  # its codex_apps MCP server isn't in `codex mcp list`
        assert 'web_search="cached"' in cs  # pinned: ~/.codex/config.toml can't drop it silently
    set_peers(env, codex={"web_search": False})
    for resume_id in (None, "thread-1"):
        cs = configs(argv(env, "codex", resume_id))
        assert 'web_search="disabled"' in cs and 'web_search="cached"' not in cs


def test_codex_configured_args_cannot_reenable_apps_or_web_search_when_off(env):
    extra = ["-c", "features.apps=true", "--enable", "apps", "--enable=apps", "-c", "features={apps=true}",
             "-c", 'web_search="live"', "--enable", "other", "-c", "keep=1"]
    set_peers(env, codex={"args": extra})
    a = argv(env, "codex")
    cs = configs(a)
    assert "features.apps=true" not in cs and "apps" not in a and "--enable=apps" not in a
    # A features table is kept for its other entries; the later features.apps=false wins (a
    # later -c overrides, and a dotted key merges into a table: codex 0.171.0).
    assert cs.index("features={apps=true}") < cs.index("features.apps=false")
    assert 'web_search="live"' in cs and "keep=1" in cs  # on: the user's own web_search choice stands
    assert 'web_search="cached"' not in cs
    assert a[a.index("--enable") + 1] == "other"
    set_peers(env, codex={"args": extra, "web_search": False})
    cs = configs(argv(env, "codex"))
    assert 'web_search="live"' not in cs and cs[-1] == 'web_search="disabled"'


def test_codex_mcp_listing_sees_every_config_spelling_and_profile(env, monkeypatch):
    log = env["tmp"] / "mcp-argv.json"
    set_peers(env, codex={"args": ["--config", "a=1", "--config=b=2", "-cc=3", "-c=d=4", "-c", "e=5",
                                   "-p", "prof", "--profile=prof2", "--", "-c", "after=1"]})
    import subprocess
    real = subprocess.run

    def spy(cmd, *a, **kw):
        if "mcp" in cmd:
            log.write_text(json.dumps(cmd))
        return real(cmd, *a, **kw)
    monkeypatch.setattr(subprocess, "run", spy)
    adapter("codex", cfg.Config.load()).invocation("P", None, str(env["repo"]), [])
    listed = json.loads(log.read_text())
    assert listed[1:-3] == ["--profile", "prof2", "-c", "a=1", "-c", "b=2", "-c", "c=3", "-c", "d=4", "-c", "e=5"]
    assert listed[-3:] == ["mcp", "list", "--json"]


def test_codex_final_overrides_stay_before_a_double_dash(env):
    set_peers(env, codex={"args": ["-c", "x=1", "--", "positional"], "web_search": False})
    a = argv(env, "codex")
    cut = a.index("--")
    assert a[cut:] == ["--", "positional", "-"]
    assert 'web_search="disabled"' in a[:cut] and "features.apps=false" in a[:cut]


def test_agy_denies_commands_and_writes_and_guides_by_switch(env):
    a = argv(env, "antigravity")
    settings = json.loads(a[a.index("--settings") + 1])
    assert settings == {"permissions": {"deny": ["command(*)", "write_file(*)", "execute_url(*)"]}}
    assert a[-1] == AGY_HEADLESS + "PROMPT" and "search_web" in AGY_HEADLESS
    set_peers(env, antigravity={"web_search": False, "args": ["--settings", "{}", "--settings={}", "--x"]})
    a = argv(env, "antigravity", "conv-1")
    assert a.count("--settings") == 1 and "{}" not in a and "--settings={}" not in a and "--x" in a
    assert a[-1] == AGY_HEADLESS_NO_WEB + "PROMPT" and "do not use search_web" in AGY_HEADLESS_NO_WEB


def test_codex_web_search_progress_names_the_query_or_page(env):
    ad = adapter("codex", cfg.Config.load())
    st, events = StreamState(), []
    emit = lambda kind, text: events.append((kind, text))  # noqa: E731
    ad.parse_line({"type": "item.started", "item": {"type": "web_search", "query": "", "action": {"type": "other"}}},
                  st, emit)
    assert events == []  # the started item says nothing yet
    ad.parse_line({"type": "item.completed", "item": {"type": "web_search", "query": "jj install",
                                                      "action": {"type": "search"}}}, st, emit)
    ad.parse_line({"type": "item.completed", "item": {"type": "web_search", "query": "",
                                                      "action": {"type": "open_page", "url": "https://x.test/"}}},
                  st, emit)
    assert events == [("tool", "web_search jj install"), ("tool", "web_search https://x.test/")]


def test_agents_reports_web_search(env, capsys):
    set_peers(env, codex={"web_search": False}, antigravity={"web_search": False})
    assert cli.main(["agents", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["claude"]["web_search"]["enabled"] is True
    assert out["codex"]["web_search"] == {"enabled": False, "off_enforced": True, "tools": "search and fetch pages",
                                          "note": "off"}
    agy = out["antigravity"]["web_search"]
    assert agy["enabled"] is False and agy["off_enforced"] is False and "instruction only" in agy["note"]
    assert cli.main(["agents"]) == 0
    text = capsys.readouterr().out
    assert "web search as a peer: on, search and fetch pages" in text
    assert "web search as a peer: off by instruction only" in text


def test_codex_mcp_off_switches_come_after_configured_server_tables(env):
    # A later server table would replace an earlier `enabled=false` (codex 0.171.0).
    set_peers(env, codex={"args": ["-c", 'mcp_servers.github={command="gh-mcp"}']})
    cs = configs(argv(env, "codex"))
    assert cs.index('mcp_servers.github={command="gh-mcp"}') < cs.index("mcp_servers.github.enabled=false")


def test_codex_profile_goes_before_exec_on_both_launches(env):
    set_peers(env, codex={"args": ["-p", "old", "--profile", "prof", "-c", "x=1"]})
    for resume_id in (None, "thread-1"):
        a = argv(env, "codex", resume_id)
        assert a[1:4] == ["--profile", "prof", "exec"]  # `exec resume` rejects --profile
        assert "-p" not in a and "old" not in a and a.count("--profile") == 1


def test_codex_args_that_disable_web_search_turn_it_off_everywhere(env, capsys):
    from agent_bus.db import HUMAN
    from agent_bus.worker import Worker
    # Read as codex reads it: TOML, comments included (codex 0.171.0 disables search for this).
    set_peers(env, codex={"args": ["-c", 'web_search="disabled" # privacy']})
    assert adapter("codex", cfg.Config.load()).web_search is False
    set_peers(env, codex={"args": ["-c", "web_search=disabled"]})  # not TOML: the raw text
    assert adapter("codex", cfg.Config.load()).web_search is False
    for value in ('\n"disabled"', '"disabled'):  # trimmed first; an unmatched quote is stripped
        set_peers(env, codex={"args": ["-c", f"web_search={value}"]})
        assert adapter("codex", cfg.Config.load()).web_search is False, value
    set_peers(env, codex={"args": ["-c", 'web_search="disabled"', "-c", 'web_search="live"']})
    assert adapter("codex", cfg.Config.load()).web_search is True  # the last one wins
    set_peers(env, codex={"args": ["-c", "web_search='disabled'"]})
    cs = configs(argv(env, "codex"))
    assert cs[-1] == 'web_search="disabled"' and "web_search='disabled'" not in cs
    w = Worker.__new__(Worker)
    w.config = cfg.Config.load()
    t = {"id": "t-x", "participants": ["codex"], "initiator": HUMAN, "repo": "/r"}
    assert "search the web" not in w.preamble("codex", t)
    assert cli.main(["agents", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["codex"]["web_search"]["enabled"] is False
