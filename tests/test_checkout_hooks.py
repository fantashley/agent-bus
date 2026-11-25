"""#98: a checkout under review can't run its own hooks or project config in a peer turn."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from agent_bus import config as cfg
from agent_bus.peers import CopilotAdapter, PeerSetupError, adapter
from agent_bus.service import Service
from agent_bus.worker import Worker


def set_peers(env, **settings):
    path = env["tmp"] / "config" / "agent-bus" / "config.json"
    data = json.loads(path.read_text())
    for agent, s in settings.items():
        data["peers"][agent].update(s)
    path.write_text(json.dumps(data))


def invocation(env, agent, resume_id=None, add_dirs=()):
    return adapter(agent, cfg.Config.load()).invocation("PROMPT", resume_id, str(env["repo"]), list(add_dirs))


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data))


def flag_values(argv, flag):
    return [argv[i + 1] for i, a in enumerate(argv[:-1]) if a == flag]


# ------------------------------------------------------------------ Claude Code


def test_claude_loads_only_the_users_settings(env):
    # -p skips the trust dialog: the checkout's .claude/settings*.json ran their hooks and
    # apiKeyHelper, and their env redirected the API.  Only user settings load now.
    set_peers(env, claude={"args": ["--setting-sources", "user,project,local", "--setting-sources=project",
                                    "--verbose"]})
    for resume_id in (None, "s-1"):
        a = invocation(env, "claude", resume_id).argv
        assert flag_values(a, "--setting-sources") == ["user"]
        assert not [x for x in a if x.startswith("--setting-sources=")]
        assert "--verbose" in a
    assert adapter("claude", cfg.Config.load()).recipe == "6"


# ------------------------------------------------------------------ Codex


def test_codex_ignores_rules_and_turns_hooks_off(env):
    # A trusted project's .codex/rules can allow a command outside the read-only sandbox, and
    # its .codex/hooks.json or [hooks] table would run commands outside the turn's tools.
    set_peers(env, codex={"args": ["--dangerously-bypass-hook-trust", "--ignore-rules", "--enable", "hooks",
                                   "-c", "features.hooks=true", "--enable=hooks"]})
    for resume_id in (None, "11111111-2222-3333-4444-555555555555"):
        a = invocation(env, "codex", resume_id).argv
        assert a.count("--ignore-rules") == 1  # codex rejects the flag twice
        assert "--dangerously-bypass-hook-trust" not in a
        assert "hooks" not in a and "--enable=hooks" not in a
        configs = flag_values(a, "-c")
        assert configs[-1 - configs[::-1].index("features.hooks=false")] == "features.hooks=false"
        assert "features.hooks=true" not in configs
    assert adapter("codex", cfg.Config.load()).recipe == "5"


def test_codex_turns_plugins_off_in_the_turn_and_its_mcp_listing(env, monkeypatch):
    # A trusted project's config can add a local marketplace and enable a plugin from it, and
    # the plugin's MCP servers then start; `-c mcp_servers.<name>.enabled=false` can't switch
    # one off (codex 0.172.0: "invalid transport").  So plugins are off, and the listing that
    # decides which servers to switch off runs with them off too (#124).
    import subprocess
    set_peers(env, codex={"args": ["--enable", "plugins", "-c", "features.plugins=true", "--enable=plugins"]})
    listed = []
    real = subprocess.run

    def spy(cmd, *a, **kw):
        if "mcp" in cmd:
            listed.append(cmd)
        return real(cmd, *a, **kw)
    monkeypatch.setattr(subprocess, "run", spy)
    for resume_id in (None, "11111111-2222-3333-4444-555555555555"):
        a = invocation(env, "codex", resume_id).argv
        assert "plugins" not in a and "--enable=plugins" not in a
        configs = flag_values(a, "-c")
        assert "features.plugins=true" not in configs
        assert [c for c in configs if c.startswith("features.plugins")] == ["features.plugins=false"]
    assert listed and all("features.plugins=false" in cmd and "features.plugins=true" not in cmd for cmd in listed)


@pytest.mark.parametrize("key, value", [("sqlite_home", '"/elsewhere"'), ("log_dir", '"/elsewhere"'),
                                        ("js_repl_node_path", '"./node"'), ("js_repl_node_module_dirs", '["m"]')])
def test_codex_refuses_project_config_that_moves_its_state(env, key, value):
    # A trusted project's config.toml can set these (codex 0.172.0 created its databases in
    # the directory a project's sqlite_home and log_dir named); a -c can't untrust the project.
    (env["repo"] / ".git").mkdir()
    sub = env["repo"] / "pkg"
    sub.mkdir()
    write(env["repo"] / ".codex" / "config.toml", f'model = "x"\n{key} = {value}\n')
    for cwd in (env["repo"], sub):  # the repository root's config applies from a subdirectory too
        with pytest.raises(PeerSetupError, match=f"PEER_SETUP: .*{key}"):
            adapter("codex", cfg.Config.load()).invocation("P", None, str(cwd), [])


def test_codex_other_project_config_is_fine_and_broken_config_fails_closed(env):
    write(env["repo"] / ".codex" / "config.toml", 'developer_instructions = "be brief"\nnotify = ["x"]\n')
    assert "--ignore-rules" in invocation(env, "codex").argv
    write(env["repo"] / ".codex" / "config.toml", "not = [toml")
    with pytest.raises(PeerSetupError, match="config.toml"):
        invocation(env, "codex")


# ------------------------------------------------------------------ Antigravity


AGY_HOOK = {"probe": {"SessionStart": [{"type": "command", "command": "touch x"}]}}
AGY_MCP = {"mcpServers": {"probe": {"command": "sh", "args": ["-c", "touch x"]}}}


@pytest.mark.parametrize("rel, content", [
    (".agents/hooks.json", AGY_HOOK),
    ("_agents/hooks.json", AGY_HOOK),
    (".agent/mcp_config.json", AGY_MCP),
    ("_agent/plugins/p1/hooks.json", AGY_HOOK),
    (".agents/plugins/p1/mcp_config.json", AGY_MCP),
    (".agents/plugins.json", {"plugins": [{"path": "tools/p"}]}),
])
def test_agy_refuses_a_workspace_with_its_own_hooks_or_servers(env, rel, content):
    # agy loads them from every workspace directory, in print mode too, whatever the folder
    # trust list says, and --settings has no key that turns them off (agy 1.5.13).
    write(env["repo"] / rel, content)
    with pytest.raises(PeerSetupError, match="PEER_SETUP: this workspace defines hooks or MCP servers") as e:
        invocation(env, "antigravity")
    assert rel.split("/")[-1] in str(e.value)


def test_agy_checks_added_directories_and_the_repo_root(env, tmp_path):
    snap = tmp_path / "snapshot"
    write(snap / ".agents" / "hooks.json", AGY_HOOK)
    with pytest.raises(PeerSetupError, match="snapshot"):
        invocation(env, "antigravity", add_dirs=[str(snap)])
    # From a subdirectory of a repository, the root's customizations load too.
    (env["repo"] / ".git").mkdir()
    write(env["repo"] / ".agents" / "mcp_config.json", AGY_MCP)
    sub = env["repo"] / "pkg"
    sub.mkdir()
    with pytest.raises(PeerSetupError, match="mcp_config.json"):
        adapter("antigravity", cfg.Config.load()).invocation("P", None, str(sub), [])


@pytest.mark.parametrize("args", [["--add-dir", "{d}"], ["--add-dir={d}"], ["-add-dir", "{d}"], ["-add-dir={d}"]])
def test_agy_checks_directories_in_the_configured_args(env, tmp_path, args):
    extra = tmp_path / "extra"
    write(extra / ".agents" / "hooks.json", AGY_HOOK)
    set_peers(env, antigravity={"args": [a.format(d=extra) for a in args]})
    with pytest.raises(PeerSetupError, match="extra"):
        invocation(env, "antigravity")


@pytest.mark.parametrize("rel", ["extra", "../outside", "pkg/../extra"])
def test_agy_configured_relative_dirs_are_relative_to_the_turn(env, rel, monkeypatch, tmp_path):
    # The worker runs elsewhere (its state directory); agy runs in the turn's directory.
    target = os.path.normpath(env["repo"] / rel)
    write(Path(target) / ".agents" / "hooks.json", AGY_HOOK)
    (env["repo"] / "pkg").mkdir(exist_ok=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    set_peers(env, antigravity={"args": ["--add-dir", rel]})
    with pytest.raises(PeerSetupError, match="hooks.json"):
        invocation(env, "antigravity")


def test_repo_dirs_walk_normalized_and_resolved_paths(tmp_path):
    from agent_bus.peers import _repo_dirs
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "a" / "b").mkdir(parents=True)
    assert _repo_dirs(str(repo / "a" / "b" / ".." / "b")) == [repo / "a" / "b", repo / "a", repo]
    link = tmp_path / "link"
    link.symlink_to(repo / "a")
    assert repo in _repo_dirs(str(link))  # through the symlink's target


def test_agy_switched_off_or_empty_customizations_are_fine(env):
    write(env["repo"] / ".agents" / "hooks.json", {"probe": {"enabled": False, **AGY_HOOK["probe"]}})
    write(env["repo"] / ".agents" / "mcp_config.json", "")  # agy leaves it empty
    write(env["repo"] / ".agent" / "mcp_config.json", {"mcpServers": {"probe": {"command": "x", "disabled": True}}})
    write(env["repo"] / ".agents" / "plugins.json", {})
    write(env["repo"] / ".agents" / "rules" / "style.md", "# rules are only text")
    write(env["repo"] / ".agents" / "skills" / "s" / "SKILL.md", "---\nname: s\n---\n")
    a = invocation(env, "antigravity").argv
    assert "--print" in a and adapter("antigravity", cfg.Config.load()).recipe == "12"


@pytest.mark.parametrize("content", ["{not json", "[1]"])
def test_agy_unreadable_customizations_fail_closed(env, content):
    write(env["repo"] / ".agents" / "hooks.json", content)
    with pytest.raises(PeerSetupError, match="hooks.json"):
        invocation(env, "antigravity")


def test_agy_review_of_a_head_with_hooks_is_refused_before_launch(env, no_spawn):
    # The review's snapshot of the head holds the head's own .agents, and goes in with --add-dir.
    if not shutil.which("git"):
        pytest.skip("git not installed")
    bus, repo = env["bus"], env["repo"]
    genv = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
                GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")
    sh = lambda *a: subprocess.run(a, cwd=repo, check=True, capture_output=True, text=True, env=genv).stdout.strip()  # noqa: E731
    sh("git", "init", "-q")
    (repo / "a.txt").write_text("start\n")
    sh("git", "add", "a.txt")
    sh("git", "commit", "-qm", "base")
    base = sh("git", "rev-parse", "HEAD")
    write(repo / ".agents" / "hooks.json", AGY_HOOK)
    sh("git", "add", ".")
    sh("git", "commit", "-qm", "hooks")
    head = sh("git", "rev-parse", "HEAD")
    sh("git", "checkout", "-q", base)  # the working copy has no hooks: only the snapshot does
    codex = Service(bus, "codex", bus.register_session("codex"))
    tid = codex.request_review("antigravity", str(repo), base, head)["thread"]["id"]
    Worker(bus, bus.jobs(tid)[-1]["id"]).run()
    notice = [m["body"] for m in bus.history(tid)["messages"] if "PEER_SETUP" in m["body"]]
    assert notice and "hooks.json" in notice[0]
    log = env["log"]
    launched = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    assert not [c for c in launched if c["agent"] == "antigravity"]


# ------------------------------------------------------------------ Copilot CLI


def test_copilot_switches_off_an_added_directorys_mcp_servers(env, tmp_path):
    # `copilot mcp list` runs in the turn's directory, so it can't see a snapshot's .mcp.json.
    snap = tmp_path / "snapshot"
    write(snap / ".mcp.json", {"mcpServers": {"head-server": {"command": "x"}, "agent-bus": {"command": "y"}}})
    write(snap / ".github" / "mcp.json", {"mcpServers": {"gh-server": {"command": "x"}}})
    a = invocation(env, "copilot", add_dirs=[str(snap)]).argv
    off = flag_values(a, "--disable-mcp-server")
    assert off == ["agent-bus", "computer-use", "head-server", "gh-server"]  # each once
    assert adapter("copilot", cfg.Config.load()).recipe == "3"


@pytest.mark.parametrize("content, off", [
    ({"bare-server": {"command": "x"}, "$schema": "https://example.com/s.json"}, ["bare-server"]),
    ({"servers": {"vs-server": {"command": "x"}}}, ["vs-server"]),
    ({"mcpServers": {"a": {}}, "servers": {"b": {}}, "c": {"command": "x"}}, ["a", "b", "c"]),
    # A bare server may be named like a wrapper: its own fields say which it is.
    # (when a wrapper-named key holds a non-object, both readings are switched off).
    ({"servers": {"command": "x", "args": ["y"]}, "other": {"url": "https://example.com"}}, ["servers", "other"]),
    ({"mcpServers": {"type": "http", "url": "https://example.com", "headers": {"a": "b"}}},
     ["headers", "mcpServers"]),
    # A valid wrapped server next to an invalid sibling still loads in Copilot.
    ({"mcpServers": {"env": {"command": "sh"}, "broken": "x"}}, ["env", "mcpServers"]),
    ({"mcpServers": {"tools": {"command": "sh"}, "url": "https://example.com"}}, ["tools", "mcpServers"]),
    # ...and a wrapped server may be named like a field: a wrapper holds only objects.
    ({"mcpServers": {"tools": {"command": "sh"}, "env": {"url": "https://example.com"}}}, ["tools", "env"]),
    ({"servers": {"command": {"command": "sh"}}}, ["command"]),
])
def test_copilot_reads_every_mcp_file_shape(env, tmp_path, content, off):
    # A bare object (server names at the top level) and a "servers" wrapper load too.
    snap = tmp_path / "snapshot"
    write(snap / ".mcp.json", content)
    a = invocation(env, "copilot", add_dirs=[str(snap)]).argv
    assert flag_values(a, "--disable-mcp-server") == ["agent-bus", "computer-use", *off]


@pytest.mark.parametrize("content", ["{not json", "[1]", {"mcpServers": []}, {"servers": "x"},
                                     {"mcpServers": {"-x": {}}}])
def test_copilot_unreadable_added_mcp_config_fails_closed(env, tmp_path, content):
    snap = tmp_path / "snapshot"
    write(snap / ".mcp.json", content)
    with pytest.raises(PeerSetupError, match=".mcp.json"):
        invocation(env, "copilot", add_dirs=[str(snap)])


def test_copilot_extension_opt_in_never_reaches_the_cli(env):
    # Project extensions run as processes of their own, outside the turn's tools.
    assert "GITHUB_COPILOT_PROMPT_MODE_EXTENSIONS" in CopilotAdapter.drop_env
    set_peers(env, copilot={"env": {"GITHUB_COPILOT_PROMPT_MODE_EXTENSIONS": "true"}})
    with pytest.raises(ValueError, match="extensions"):
        cfg.Config.load()


def test_copilot_refuses_a_checkout_with_language_servers(env, tmp_path):
    # A trusted folder starts .github/lsp.json's servers in the background; disableAllHooks
    # doesn't cover them.
    write(env["repo"] / ".github" / "copilot" / "settings.json", {"disableAllHooks": True})
    write(env["repo"] / ".github" / "lsp.json", {"lspServers": {"x": {"command": "touch"}}})
    with pytest.raises(PeerSetupError, match="language servers.*lsp.json"):
        invocation(env, "copilot")
    write(env["repo"] / ".github" / "lsp.json", {})
    invocation(env, "copilot")
    snap = tmp_path / "snapshot"
    write(snap / ".github" / "lsp.json", {"lspServers": {"x": {"command": "touch"}}})
    with pytest.raises(PeerSetupError, match="snapshot"):
        invocation(env, "copilot", add_dirs=[str(snap)])


def test_copilot_refuses_an_added_directorys_hooks(env, tmp_path):
    snap = tmp_path / "snapshot"
    write(snap / ".claude" / "settings.json", {"hooks": {"SessionStart": [{"hooks": [{"command": "x"}]}]}})
    with pytest.raises(PeerSetupError, match="settings.json"):
        invocation(env, "copilot", add_dirs=[str(snap)])
