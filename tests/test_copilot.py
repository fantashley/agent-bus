"""#2: GitHub Copilot CLI as a fourth harness: a read-only headless adapter, MCP isolation,
usage, out-of-usage detection, setup-copilot, and Copilot as reviewer and discussion peer."""
import json
import os
import shutil
import subprocess

import pytest

from agent_bus import cli, copilot_setup
from agent_bus import config as cfg
from agent_bus.db import HUMAN
from agent_bus.peers import CopilotAdapter, adapter
from agent_bus.service import Service
from agent_bus.worker import Worker


def set_peers(env, **settings):
    path = env["tmp"] / "config" / "agent-bus" / "config.json"
    data = json.loads(path.read_text())
    for agent, s in settings.items():
        data["peers"][agent].update(s)
    path.write_text(json.dumps(data))


def argv(env, resume_id=None):
    return adapter("copilot", cfg.Config.load()).invocation("PROMPT", resume_id, str(env["repo"]), []).argv


def run(env, resume_id=None):
    events = []
    res = adapter("copilot", cfg.Config.load()).run("round 1, which is blind", str(env["repo"]), 30,
                                                    lambda k, t: events.append((k, t)), resume_id=resume_id)
    return res, events


def calls(env):
    return [json.loads(line) for line in env["log"].read_text().splitlines()] if env["log"].exists() else []


def value(args, flag):
    return [a.split("=", 1)[1] for a in args if a.startswith(flag + "=")]


# ------------------------------------------------------------------ the invocation


def test_registry_has_copilot():
    assert "copilot" in cfg.AGENTS and cfg.HARNESS_INFO["copilot"]["cli"] == "copilot"
    assert cfg.check_peer_settings("copilot", {"effort": "none"}) == {"effort": "none"}
    with pytest.raises(ValueError, match="effort"):
        cfg.check_peer_settings("copilot", {"effort": "turbo"})


def test_turn_is_read_only_with_every_mcp_server_off(env):
    for resume_id in (None, "11111111-2222-3333-4444-555555555555"):
        a = argv(env, resume_id)
        assert a[1:3] == ["-p", ""]  # the prompt goes on stdin
        assert value(a, "--available-tools") == ["view,grep,rg,glob,web_fetch"]
        assert value(a, "--deny-tool") == ["shell", "write", "memory"]
        assert "--disable-builtin-mcps" in a and "--no-auto-update" in a and "--sync-session=off" in a
        assert "--allow-all-urls" in a  # web_fetch is denied in -p mode without a URL rule
        disabled = [a[i + 1] for i, x in enumerate(a[:-1]) if x == "--disable-mcp-server"]
        assert disabled == ["agent-bus", "computer-use"]  # agent-bus too: peers never post
        for bad in ("--allow-all", "--yolo", "--allow-all-tools", "bash"):
            assert bad not in a
    new, resumed = argv(env), argv(env, "s-1")
    assert "--session-id" in new and "--resume" not in new
    assert resumed[resumed.index("--resume") + 1] == "s-1" and "--session-id" not in resumed


def test_web_search_off_drops_web_fetch_and_urls(env):
    set_peers(env, copilot={"web_search": False})
    a = argv(env)
    assert value(a, "--available-tools") == ["view,grep,rg,glob"] and "--allow-all-urls" not in a


def test_configured_args_cannot_widen_the_turn(env):
    set_peers(env, copilot={"args": [
        "--yolo", "--allow-all", "--available-tools", "bash", "apply_patch", "--allow-tool=shell",
        "--additional-mcp-config", "@x.json", "--enable-mcp-server", "github", "--plugin-dir", "/p",
        "--agent", "task", "--mode", "autopilot", "--allow-url", "evil.example", "--allow-all-urls",
        "-Cdir", "--resume", "--trust-folder", "--config-dir", "/elsewhere", "--add-dir", "/extra", "--effort", "low", "--keep-me", "--model", "gpt-x", "--reasoning-effort", "low"]})
    set_peers(env, copilot={"web_search": False})
    a = argv(env)
    for bad in ("--yolo", "--allow-all", "bash", "apply_patch", "--allow-tool=shell", "@x.json", "github", "/p",
                "task", "autopilot", "evil.example", "--allow-all-urls", "-Cdir", "--trust-folder", "/elsewhere", "--add-dir", "/extra"):
        assert bad not in a
    assert a.count("--resume") == 0 and value(a, "--available-tools") == ["view,grep,rg,glob"]
    assert "--keep-me" in a and a[a.index("--model") + 1] == "gpt-x"  # model and effort pass through
    set_peers(env, copilot={"model": "gpt-y", "effort": "high"})
    conf = cfg.Config.load()
    a = adapter("copilot", conf, settings=conf.peer_settings("copilot")).invocation(
        "PROMPT", None, str(env["repo"]), []).argv
    assert a.count("--model") == 1 and a[a.index("--model") + 1] == "gpt-y"
    assert a.count("--reasoning-effort") == 1 and a[a.index("--reasoning-effort") + 1] == "high"
    assert "--effort" not in a  # the alias can't override the configured effort


def test_permission_env_never_reaches_the_cli(env, monkeypatch):
    for name in CopilotAdapter.drop_env:
        monkeypatch.setenv(name, "true")
    seen = {}
    real = subprocess.Popen

    def spy(args, **kw):
        if "-p" in args:
            seen.update(kw["env"])
        return real(args, **kw)

    monkeypatch.setattr(subprocess, "Popen", spy)
    res, _ = run(env)
    assert res.ok and "GITHUB_COPILOT_PROMPT_MODE_REPO_HOOKS" in CopilotAdapter.drop_env
    assert not [n for n in CopilotAdapter.drop_env if n in seen]
    assert seen["AGENT_BUS_PEER"]


@pytest.mark.parametrize("name", CopilotAdapter.drop_env)
def test_permission_env_is_refused_in_config(env, name):
    set_peers(env, copilot={"env": {name: "true"}})
    with pytest.raises(ValueError, match=name):
        cfg.Config.load()


def test_mcp_list_failure_fails_closed(env, monkeypatch):
    monkeypatch.setenv("FAKE_COPILOT_MCP_FAIL", "1")
    res, _ = run(env)
    assert not res.ok and "copilot mcp list" in res.error
    assert not [c for c in calls(env) if c["agent"] == "copilot"]  # the turn never started


def test_mcp_servers_are_listed_with_the_participants_env(env):
    # A configured env can point copilot at another config, so the listing must see it too.
    other = json.dumps({"mcpServers": {"elsewhere": {"type": "local", "command": "x", "enabled": True}}})
    set_peers(env, copilot={"env": {"FAKE_COPILOT_MCP_LIST": other}})
    a = argv(env)
    assert [a[i + 1] for i, x in enumerate(a[:-1]) if x == "--disable-mcp-server"] == ["elsewhere"]


def test_a_live_mcp_server_stops_the_turn(env, monkeypatch):
    monkeypatch.setenv("FAKE_COPILOT_LIVE_MCP", "sneaky")
    res, _ = run(env)
    assert not res.ok and "PEER_SETUP" in res.error and "sneaky" in res.error and not res.text


# ------------------------------------------------------------------ the stream


def test_reply_session_and_running_usage(env):
    res, events = run(env)
    assert res.ok and '"action": "propose"' in res.text and res.external_id
    assert res.usage == {"input": 30, "output": 8, "cache_read": 300, "cache_write": 0, "thinking": 2}
    assert ("tool", "view a.txt") in events
    assert CopilotAdapter.session_usage  # the checkpoint is the session's total
    again, _ = run(env, resume_id=res.external_id)
    assert again.ok and again.external_id == res.external_id and again.usage["input"] == 60


def test_out_of_usage_is_detected(env, monkeypatch):
    monkeypatch.setenv("FAKE_EXHAUSTED", "copilot")
    res, _ = run(env)
    assert not res.ok and res.quota and res.quota["source"] == "heuristic"


def test_silent_turn_is_format_drift(env, monkeypatch):
    monkeypatch.setenv("FAKE_SILENT", "copilot")
    res, _ = run(env)
    assert not res.ok and res.format_changed


# ------------------------------------------------------------------ as a peer


def git_repo(repo):
    if not shutil.which("git"):
        pytest.skip("git not installed")
    genv = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
                GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")
    sh = lambda *a: subprocess.run(a, cwd=repo, check=True, capture_output=True, text=True, env=genv).stdout.strip()  # noqa: E731
    sh("git", "init", "-q")
    (repo / "a.txt").write_text("start\n")
    sh("git", "add", "a.txt")
    sh("git", "commit", "-qm", "base")
    base = sh("git", "rev-parse", "HEAD")
    (repo / "a.txt").write_text("changed\n")
    sh("git", "commit", "-qam", "change")
    return base, sh("git", "rev-parse", "HEAD")


def test_claude_and_copilot_review_one_after_another(env, no_spawn, monkeypatch):
    bus, repo = env["bus"], env["repo"]
    base, head = git_repo(repo)
    monkeypatch.setenv("FAKE_APPROVE", "claude,copilot")
    codex = Service(bus, "codex", bus.register_session("codex"))
    tid = codex.request_review(None, str(repo), base, head, change_id="PR #1",
                               reviewers=["claude", "copilot"])["thread"]["id"]
    Worker(bus, bus.jobs(tid)[-1]["id"]).run()
    assert [c["agent"] for c in calls(env)] == ["claude", "copilot"]
    assert "Diff from base to head" in calls(env)[1]["prompt"]
    st = bus.review_standings(tid)
    assert st["complete"] and st["approved"] == ["claude", "copilot"]


def test_four_way_discussion_reaches_consensus(env, no_spawn):
    bus = env["bus"]
    tid = Service(bus, HUMAN).start_discussion("Q?", max_rounds=3)["thread"]["id"]
    assert bus.thread(tid)["participants"] == ["claude", "codex", "antigravity", "copilot"]
    Worker(bus, bus.jobs(tid)[-1]["id"]).run()
    assert bus.thread(tid)["status"] == "resolved"


# ------------------------------------------------------------------ setup-copilot


def test_setup_copilot_adds_once_and_keeps_other_servers(env, monkeypatch):
    home = env["tmp"] / "copilot-home"
    monkeypatch.setenv("COPILOT_HOME", str(home))
    path = home / "mcp-config.json"
    home.mkdir()
    path.write_text(json.dumps({"mcpServers": {"other": {"type": "http", "url": "https://x"}}, "extra": 1}))
    path.chmod(0o600)
    assert copilot_setup.ensure("/bin/agent-bus") == "added"
    data = json.loads(path.read_text())
    assert data["extra"] == 1 and data["mcpServers"]["other"] == {"type": "http", "url": "https://x"}
    assert data["mcpServers"]["agent-bus"] == {"type": "local", "command": "/bin/agent-bus",
                                               "args": ["mcp", "--agent", "copilot"], "tools": ["*"]}
    assert path.stat().st_mode & 0o777 == 0o600
    before = path.stat().st_mtime_ns
    assert copilot_setup.ensure("/bin/agent-bus") == "unchanged" and path.stat().st_mtime_ns == before
    assert copilot_setup.ensure("/nix/new/agent-bus") == "updated"


def test_setup_copilot_keeps_a_participant_pin_and_user_keys(env, monkeypatch):
    home = env["tmp"] / "copilot-home"
    monkeypatch.setenv("COPILOT_HOME", str(home))
    home.mkdir()
    pinned = ["mcp", "--agent", "copilot", "--participant", "copilot-work"]
    (home / "mcp-config.json").write_text(json.dumps({"mcpServers": {"agent-bus": {
        "type": "local", "command": "/old", "args": pinned, "env": {"X": "1"}}}}))
    assert copilot_setup.ensure("/new") == "updated"
    entry = json.loads((home / "mcp-config.json").read_text())["mcpServers"]["agent-bus"]
    assert entry["args"] == pinned and entry["env"] == {"X": "1"} and entry["command"] == "/new"


@pytest.mark.parametrize("text", ["[]", "{not json", '{"mcpServers": []}'])
def test_setup_copilot_refuses_a_file_it_does_not_understand(env, monkeypatch, text):
    home = env["tmp"] / "copilot-home"
    monkeypatch.setenv("COPILOT_HOME", str(home))
    home.mkdir()
    (home / "mcp-config.json").write_text(text)
    with pytest.raises(copilot_setup.SetupError):
        copilot_setup.ensure("/bin/agent-bus")
    assert (home / "mcp-config.json").read_text() == text


def test_setup_copilot_cli(env, monkeypatch, capsys):
    monkeypatch.setenv("COPILOT_HOME", str(env["tmp"] / "ch"))
    assert cli.main(["setup-copilot", "--bin", "/bin/agent-bus"]) == 0
    assert "copilot: agent-bus entry added" in capsys.readouterr().out


# ------------------------------------------------------------------ a harness for many models


def test_named_participants_run_different_models_on_the_copilot_harness(env):
    path = env["tmp"] / "config" / "agent-bus" / "config.json"
    data = json.loads(path.read_text())
    data["participants"] = {"copilot-gpt": {"harness": "copilot", "model": "gpt-6.2-sol"},
                            "copilot-claude": {"harness": "copilot", "model": "claude-opus-5.5", "effort": "max"}}
    path.write_text(json.dumps(data))
    conf = cfg.Config.load()
    for name, model in (("copilot-gpt", "gpt-6.2-sol"), ("copilot-claude", "claude-opus-5.5")):
        a = adapter(name, conf, settings=conf.peer_settings(name))
        assert isinstance(a, CopilotAdapter) and a.participant == name
        args = a.invocation("P", None, str(env["repo"]), []).argv
        assert args[args.index("--model") + 1] == model
    assert "--reasoning-effort" in adapter("copilot-claude", conf, settings=conf.peer_settings("copilot-claude")) \
        .invocation("P", None, str(env["repo"]), []).argv


def test_only_the_per_model_rate_limit_is_scoped_to_the_model(env):
    from agent_bus import quota
    from agent_bus.peers import StreamState
    a, conf = adapter("copilot", cfg.Config.load()), cfg.Config.load()
    one = a.classify(StreamState(), "You've hit the rate limit for this model. Switch models with /model.")
    weekly = a.classify(StreamState(), "You've reached your weekly rate limit. Wait, or switch to Auto.")
    assert one["scope"] == "model" and "scope" not in weekly
    assert quota.scope_for(conf, "copilot", "gpt-x", "heuristic", per_model=True) == "gpt-x"
    assert quota.scope_for(conf, "copilot", "gpt-x", "heuristic") == ""


def test_copilot_model_in_env_names_the_quota_scope(env):
    # COPILOT_MODEL picks the model below --model: two participants on one account that
    # choose models this way get separate per-model blocks.
    from agent_bus import quota
    set_peers(env, copilot={"env": {"COPILOT_MODEL": "gpt-a"}})
    conf = cfg.Config.load()
    assert quota.effective_model(conf, "copilot", {}) == "gpt-a"
    assert quota.effective_model(conf, "copilot", {"model": "gpt-b"}) == "gpt-b"
    set_peers(env, copilot={"env": {"COPILOT_MODEL": "gpt-a"}, "args": ["--model", "gpt-c"]})
    assert quota.effective_model(cfg.Config.load(), "copilot", {}) == "gpt-c"
