"""#46: headless peers search the web, with everything else still read-only."""
import json
from pathlib import Path

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


def agy_agent(a):
    """The --agent a turn runs as, and its definition's frontmatter lines (in an --add-dir)."""
    name = a[a.index("--agent") + 1]
    dirs = [a[i + 1] for i, x in enumerate(a) if x == "--add-dir"]
    found = [Path(d) / ".agents" / "agents" / f"{name}.md" for d in dirs]
    found = [f for f in found if f.exists()]
    assert len(found) == 1
    return name, found[0].read_text().split("---")[1].strip().splitlines()


def test_agy_denies_commands_and_writes_and_guides_by_switch(env):
    a = argv(env, "antigravity")
    settings = json.loads(a[a.index("--settings") + 1])
    assert settings == {"permissions": {"deny": ["command(*)", "write_file(*)", "execute_url(*)"]}}
    assert a[-1] == AGY_HEADLESS + "PROMPT" and "search_web" in AGY_HEADLESS
    set_peers(env, antigravity={"web_search": False, "args": ["--settings", "{}", "--settings={}", "--x"]})
    a = argv(env, "antigravity", "conv-1")
    assert a.count("--settings") == 1 and "{}" not in a and "--settings={}" not in a and "--x" in a
    assert a[-1] == AGY_HEADLESS_NO_WEB + "PROMPT" and "do not use search_web" in AGY_HEADLESS_NO_WEB


def test_agy_runs_as_an_agent_whose_tools_are_the_whole_inventory(env):
    # A custom agent's `tools` drops invoke_subagent, swarm, schedule, run_workflow… and,
    # with web search off, search_web, which no deny rule removes (agy 1.5.13, issue #54).
    for resume_id in (None, "conv-1"):
        name, front = agy_agent(argv(env, "antigravity", resume_id))
        assert "mainAgent: true" in front and "subagent: false" in front
        assert "tools: [read_file, search_web]" in front
    # agy parses flags Go-style: one dash works as well as two.
    set_peers(env, antigravity={"web_search": False, "args": ["--agent", "mine", "--agent=mine", "-agent", "mine2",
                                                              "-agent=mine3", "-settings", "{}", "-settings={}", "--x"]})
    a = argv(env, "antigravity")
    off, front = agy_agent(a)
    assert off != name and "tools: [read_file]" in front
    assert a.count("--agent") == 1 and a.count("--settings") == 1 and "--x" in a
    for gone in ("mine", "--agent=mine", "-agent", "mine2", "-agent=mine3", "-settings", "{}", "-settings={}"):
        assert gone not in a, gone
    assert "manage_task" not in front[-1] and not any("subagent" in t for t in front[-1].split(","))


def test_codex_overrides_consume_their_value(env):
    from agent_bus.peers import _codex_overrides
    # A value is never read as a flag: `--` doesn't end the scan, and `-c…` isn't read twice.
    assert _codex_overrides(["-c", "--", "-c", 'web_search="disabled"']) == ["--", 'web_search="disabled"']
    assert _codex_overrides(["--config", "-cx=1", "-cy=2"]) == ["-cx=1", "y=2"]
    assert _codex_overrides(["-c", "a=1", "--", "-c", "b=2"]) == ["a=1"]
    set_peers(env, codex={"args": ["-c", "--", "-c", 'web_search="disabled"', "-p", "prof", "--enable", "apps"]})
    assert adapter("codex", cfg.Config.load()).web_search is False
    a = argv(env, "codex")
    # Nor does any other walker stop at it: the profile moves, apps is dropped, and our
    # overrides go after the whole slice, not between a -c and its value.
    assert a[1:4] == ["--profile", "prof", "exec"] and "apps" not in a
    cs = configs(a)
    assert cs[:2] == ["--", 'sandbox_mode="read-only"'] and cs[-1] == 'web_search="disabled"'


def test_codex_sandbox_approval_and_risky_features_come_last(env):
    # Configured args can't loosen the sandbox, approvals or the switched-off features (#54):
    # a later -c wins, and sandbox flags (which beat -c) are dropped.
    extra = ["-c", 'sandbox_mode="danger-full-access"', "-c", 'approval_policy="on-request"',
             "-s", "workspace-write", "--sandbox=danger-full-access", "-sdanger-full-access",
             "--dangerously-bypass-approvals-and-sandbox", "--approve-for-me", "--worktree", "--trust", "--yolo", "--not-so-yolo",
             "-c", "features.goals=true", "--enable", "image_generation", "--enable=tool_suggest", "--keep",
             "-c", "--dangerously-bypass-approvals-and-sandbox"]
    set_peers(env, codex={"args": extra})
    for resume_id in (None, "thread-1"):
        a = argv(env, "codex", resume_id)
        cs = configs(a)
        for flag in ("-s", "--sandbox=danger-full-access", "-sdanger-full-access", "--approve-for-me", "--worktree", "--trust", "--yolo", "--not-so-yolo",
                     "--enable", "--enable=tool_suggest", "workspace-write", "features.goals=true"):
            assert flag not in a, flag
        assert "--keep" in a
        # A -c value is a value, not the flag it looks like: it's kept, as a (bogus) override.
        assert cs.count("--dangerously-bypass-approvals-and-sandbox") == 1
        assert a.count("--dangerously-bypass-approvals-and-sandbox") == 1
        tail = cs[cs.index('sandbox_mode="read-only"'):]
        assert tail[:6] == ['sandbox_mode="read-only"', 'approval_policy="never"', "features.apps=false",
                            "features.goals=false", "features.image_generation=false", "features.tool_suggest=false"]
        assert cs.index('sandbox_mode="danger-full-access"') < cs.index('sandbox_mode="read-only"')
        assert cs.index('approval_policy="on-request"') < cs.index('approval_policy="never"')


def test_per_repo_switch_turns_web_search_off(env, capsys):
    from agent_bus.db import HUMAN
    from agent_bus.worker import Worker
    path = env["tmp"] / "config" / "agent-bus" / "config.json"
    data = json.loads(path.read_text())
    repo = str(env["repo"])
    other = env["tmp"] / "other"
    other.mkdir()
    for bad, msg in (([{"path": repo, "web_search": "no"}], r"repos\[0\].web_search"),
                     ([{"web_search": False}], r"repos\[0\].path"), ([{"path": repo, "x": 1}], "unknown key"),
                     ([3], r"repos\[0\] must be"), ("~/src", "must be a list")):
        path.write_text(json.dumps({**data, "repos": bad}))
        with pytest.raises(ValueError, match=msg):
            cfg.Config.load()
    # Entries can only turn it off, so any one covering the repo does (here the parent).
    path.write_text(json.dumps({**data, "repos": [{"path": str(env["tmp"]), "web_search": False}, repo,
                                                  {"path": str(other), "web_search": True}]}))
    conf = cfg.Config.load()
    assert conf.repo_allowed(repo) and conf.repo_allowed(other) and not conf.repo_allowed("/elsewhere")
    assert conf.web_search("codex") is True and conf.web_search("codex", "/elsewhere") is True
    assert conf.web_search("codex", repo) is False and conf.web_search("codex", str(env["repo"] / "sub")) is False
    for agent in cfg.AGENTS:
        assert adapter(agent, conf).web_search is True
        assert adapter(agent, conf, repo=repo).web_search is False
    assert configs(adapter("codex", conf, repo=repo).invocation("P", None, repo, []).argv)[-1] == 'web_search="disabled"'
    _, front = agy_agent(adapter("antigravity", conf, repo=repo).invocation("P", None, repo, []).argv)
    assert "tools: [read_file]" in front
    w = Worker.__new__(Worker)
    w.config = conf
    t = {"id": "t-x", "participants": ["codex"], "initiator": HUMAN}
    assert "search the web" not in w.preamble("codex", {**t, "repo": repo})
    assert "search the web" in w.preamble("codex", {**t, "repo": None})
    assert cli.main(["agents", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)["codex"]["web_search"]
    assert report["enabled"] is True and report["off_in_repos"] == [str(env["tmp"])]
    assert cli.main(["agents"]) == 0
    assert f"off in {env['tmp']}" in capsys.readouterr().out


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
                                          "note": "off", "off_in_repos": []}
    agy = out["antigravity"]["web_search"]
    assert agy["enabled"] is False and agy["off_enforced"] is True and agy["note"] == "off"  # its agent has no search_web
    assert cli.main(["agents"]) == 0
    text = capsys.readouterr().out
    assert "web search as a peer: on, search and fetch pages" in text
    assert "web search as a peer: off" in text and "instruction only" not in text


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
