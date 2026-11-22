"""Issue #32 stage 2, PR A (agent-bus thread t-71c7ce, P2): sentinel MCP server,
isolation evidence store, fingerprints and the account-free doctor."""
import io
import json
import subprocess
import sys

from agent_bus import config as cfg
from agent_bus import isolation, sentinel


def test_sentinel_logs_each_stage_and_echoes_the_nonce(tmp_path):
    log = tmp_path / "sentinel.log"
    reqs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": sentinel.TOOL, "arguments": {}}},
            {"jsonrpc": "2.0", "id": 4, "method": "resources/list"},
            {"jsonrpc": "2.0", "id": 5, "method": "ping"},
            {"jsonrpc": "2.0", "method": "tools/list"}]  # a notification: no reply, even for a known method
    out = io.StringIO()
    sentinel.serve(str(log), "N0NCE", stdin=io.StringIO("".join(json.dumps(r) + "\n" for r in reqs)), stdout=out)
    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [r["id"] for r in replies] == [1, 2, 3, 4, 5]  # notifications get no reply
    assert replies[4]["result"] == {}
    assert replies[1]["result"]["tools"][0]["name"] == sentinel.TOOL
    assert replies[2]["result"]["content"][0]["text"] == "N0NCE"
    assert replies[3]["error"]["code"] == -32601
    assert [e["event"] for e in sentinel.events(str(log))] == ["start", "initialize", "handshake", "call", "exit"]


def test_sentinel_runs_as_a_cli_command(tmp_path):
    log = tmp_path / "s.log"
    p = subprocess.run([sys.executable, "-m", "agent_bus", "sentinel-mcp", "--log", str(log), "--nonce", "x"],
                       input=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}) + "\n",
                       capture_output=True, text=True, timeout=30,
                       env={"PYTHONPATH": str(__import__("pathlib").Path(__file__).parent.parent / "src"),
                            "PATH": "/usr/bin:/bin"})
    assert p.returncode == 0 and json.loads(p.stdout.splitlines()[0])["result"]["serverInfo"]
    # initialize alone isn't a completed handshake: the client never confirmed it
    assert [e["event"] for e in sentinel.events(str(log))] == ["start", "initialize", "exit"]


def test_evidence_matches_only_the_exact_key(env):
    bus = env["bus"]
    bus.set_isolation_evidence("codex", "id1", "1", "fp1", "write", "verified", "denied")
    assert bus.isolation_evidence("codex", "id1", "1", "fp1")["write"]["outcome"] == "verified"
    for key in (("id2", "1", "fp1"), ("id1", "2", "fp1"), ("id1", "1", "fp2")):
        assert bus.isolation_evidence("codex", *key) == {}
    bus.set_isolation_evidence("codex", "id1", "1", "fp1", "write", "inconclusive", "no attempt seen")
    assert bus.isolation_evidence("codex", "id1", "1", "fp1")["write"]["outcome"] == "inconclusive"
    try:
        bus.set_isolation_evidence("codex", "id1", "1", "fp1", "write", "probably-fine")
        raise AssertionError("bad outcome accepted")
    except ValueError:
        pass


def set_args(env, agent, args=None, **extra):
    conf_path = env["tmp"] / "config" / "agent-bus" / "config.json"
    conf = json.loads(conf_path.read_text())
    conf["peers"][agent] = {**conf["peers"][agent], **({"args": args} if args is not None else {}), **extra}
    conf_path.write_text(json.dumps(conf))
    return cfg.Config.load()


def test_fingerprint_changes_with_args_and_mcp_sources(env, monkeypatch):
    c = cfg.Config.load()
    base, _ = isolation.fingerprint("codex", c, str(env["repo"]))
    assert base and isolation.fingerprint("codex", c, str(env["repo"]))[0] == base  # stable
    monkeypatch.setenv("FAKE_MCP_LIST", json.dumps([{"name": "github", "enabled": True}]))
    assert isolation.fingerprint("codex", c, str(env["repo"]))[0] != base
    monkeypatch.delenv("FAKE_MCP_LIST")
    c2 = set_args(env, "codex", ["-c", "model_verbosity=low"])
    assert isolation.fingerprint("codex", c2, str(env["repo"]))[0] != base
    monkeypatch.setenv("FAKE_MCP_LIST", '[{"enabled": true}]')  # unreadable: nothing can match
    fp, problems = isolation.fingerprint("codex", c2, str(env["repo"]))
    assert fp is None and problems


def test_doctor_reports_without_turns(env, capsys):
    from agent_bus import cli
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    for agent in cfg.AGENTS:
        assert f"{agent}: fake-{agent} 0.0.0 · recipe {agent}/1" in out
    assert "FAIL" not in out
    assert "isolation evidence, write: none" in out
    assert not env["log"].exists() or "doctor" not in env["log"].read_text()  # no model turn ran


def test_doctor_catches_an_override_that_weakens_codex(env, capsys):
    from agent_bus import cli
    set_args(env, "codex", ["-c", 'sandbox_mode="workspace-write"'])
    assert cli.main(["doctor"]) == 1
    assert "FAIL launch preflight: peers.codex.args may not set `sandbox_mode`" in capsys.readouterr().out


def test_doctor_flags_agy_servers_and_the_unsafe_waiver(env, capsys, monkeypatch):
    from agent_bus import cli
    (env["tmp"] / "agy" / "mcp_config.json").write_text(json.dumps({"mcpServers": {"github": {"command": "x"}}}))
    assert cli.main(["doctor"]) == 1
    assert "github" in capsys.readouterr().out
    set_args(env, "antigravity", allow_mcp=True)
    cli.main(["doctor", "--json"])
    rep = json.loads(capsys.readouterr().out)
    mcp = [p for p in rep["antigravity"]["preflight"] if p["check"].startswith("MCP")][0]
    assert not mcp["ok"] and "UNSAFE waiver" in mcp["detail"]


# ------------------------------------------------- review fixes (t-f67b8b pass 1)

import pytest  # noqa: E402

from agent_bus.peers import PeerSetupError, adapter, agy_mcp_servers, agy_workspaces, trusted_agent_bus_entry  # noqa: E402

SERVER = {"mcpServers": {"sentinel": {"command": "true"}}}


def put(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else (json.dumps(data) if not isinstance(data, str)
                                                            else data).encode())


@pytest.mark.parametrize("layout", ["entry-is-the-plugin", "nested-plugin", "relative-to-repo-root"])
def test_agy_discovery_is_a_superset(env, layout):
    ws = env["tmp"] / "ws"
    (ws / ".git").mkdir(parents=True)
    agents = ws / "sub" / ".agents"
    if layout == "entry-is-the-plugin":
        put(agents / "plugins.json", {"entries": [{"path": "../plug"}]})
        put(ws / "sub" / "plug" / "mcp_config.json", SERVER)
    elif layout == "nested-plugin":
        put(agents / "plugins.json", {"entries": [{"path": "vendor", "include_only": ["group/x"]}]})
        put(ws / "sub" / "vendor" / "group" / "x" / "mcp_config.json", SERVER)
    else:  # agy falls back to the project root for relative paths
        put(agents / "plugins.json", {"entries": [{"path": "shared/plugins"}]})
        put(ws / "shared" / "plugins" / "s" / "mcp_config.json", SERVER)
    assert [n for n, _w, _s in agy_mcp_servers(str(ws / "sub"), [])] == ["sentinel"]


@pytest.mark.parametrize("content,why", [('["not", "an", "object"]', "not a JSON object"),
                                         ({"mcpServers": []}, "not an object"),
                                         (b"\xff\xfe broken", "")])
def test_agy_malformed_documents_fail_closed(env, content, why):
    ws = env["tmp"] / "ws"
    put(ws / ".agents" / "mcp_config.json", content)
    with pytest.raises(PeerSetupError) as e:
        agy_mcp_servers(str(ws), [])
    assert why in str(e.value)


def test_agy_falsey_manifest_fields_fail_closed(env):
    ws = env["tmp"] / "ws"
    put(ws / ".agents" / "plugins.json", {"entries": {}})
    with pytest.raises(PeerSetupError):
        agy_mcp_servers(str(ws), [])


def test_agy_checks_add_dirs_from_configured_args_in_the_peer_cwd(env):
    conf_path = env["tmp"] / "config" / "agent-bus" / "config.json"
    conf = json.loads(conf_path.read_text())
    conf["peers"]["antigravity"]["args"] = ["--add-dir", "extra"]
    conf_path.write_text(json.dumps(conf))
    repo = env["repo"]
    put(repo / "extra" / ".agents" / "mcp_config.json", SERVER)
    assert agy_workspaces(["agy", "--add-dir", "extra", "--add-dir=/abs"], str(repo)) == [
        str(repo), str(repo / "extra"), "/abs"]
    with pytest.raises(PeerSetupError) as e:
        adapter("antigravity", cfg.Config.load()).invocation("hi", None, str(repo), [])
    assert "sentinel" in str(e.value)


def test_relative_agent_bus_command_is_not_trusted():
    assert not trusted_agent_bus_entry("agent-bus", {"command": "agent-bus", "args": ["mcp", "--agent", "antigravity"]})


@pytest.mark.parametrize("args,bad", [
    (["--dangerously-bypass-approvals-and-sandbox"], "writes: read-only sandbox"),
    (["--full-auto"], "writes: read-only sandbox"),
    (["--sandbox=workspace-write"], "writes: read-only sandbox"),
    (["-c=sandbox_mode=danger-full-access"], "writes: read-only sandbox"),
    (["--config=approval_policy=on-request"], "writes: approval_policy never"),
    (["-c", "mcp_servers.github.enabled=true"], "MCP: every listed server disabled"),
])
def test_doctor_sees_codex_overrides_in_every_form(env, capsys, args, bad):
    """Configured args that change isolation are refused at launch, in every spelling;
    doctor reports the refusal (and still resolves the effective settings itself)."""
    from agent_bus import cli
    set_args(env, "codex", args)
    assert cli.main(["doctor"]) == 1
    assert "FAIL launch preflight: peers.codex.args may not" in capsys.readouterr().out


@pytest.mark.parametrize("argv,expect", [
    # A dedicated option beats any config value, whatever the order (codex 0.171.0).
    (["--sandbox", "workspace-write", "-c", 'sandbox_mode="read-only"'], "workspace-write"),
    (["-c", 'sandbox_mode="read-only"', "-sworkspace-write"], "workspace-write"),
    (["-c", 'sandbox_mode="read-only"', "--config=sandbox_mode=danger-full-access"], "danger-full-access"),
    (["-c", 'sandbox_mode="read-only"'], "read-only"),
])
def test_effective_codex_sandbox_precedence(argv, expect):
    assert isolation.effective_codex_setting(argv, ("--sandbox", "-s"), "sandbox_mode") == expect


def test_doctor_fails_when_a_configured_cli_is_missing(env, capsys):
    from agent_bus import cli
    set_args(env, "claude", command=str(env["tmp"] / "nope" / "claude"))
    conf_path = env["tmp"] / "config" / "agent-bus" / "config.json"
    conf = json.loads(conf_path.read_text())
    conf["agents"] = ["claude", "codex"]  # listed, so it counts as available whatever PATH says
    conf_path.write_text(json.dumps(conf))
    assert cli.main(["doctor"]) == 1
    assert "FAIL CLI found" in capsys.readouterr().out


# ------------------------------------------------- review fixes (t-f67b8b pass 2)


def test_agy_personal_customization_dir_is_a_root(env):
    ws, personal = env["tmp"] / "ws", env["tmp"] / "me"
    put(ws / ".gemini" / "config.json", {"personal_customization_dir": str(personal)})
    put(personal / ".agents" / "mcp_config.json", SERVER)
    assert [n for n, _w, _s in agy_mcp_servers(str(ws), [])] == ["sentinel"]


def test_agy_dot_dirs_other_than_git_are_scanned(env):
    ws = env["tmp"] / "ws"
    put(ws / ".agents" / "plugins" / ".github" / "mcp_config.json", SERVER)
    put(ws / ".agents" / "plugins" / "big" / "payload" / "a" / "b" / "c" / "d" / "e" / "x.txt", "x")
    assert [n for n, _w, _s in agy_mcp_servers(str(ws), [])] == ["sentinel"]  # and no depth failure


def test_agy_unreadable_paths_fail_closed(env):
    import os
    ws = env["tmp"] / "ws"
    locked = ws / ".agents" / "plugins"
    locked.mkdir(parents=True)
    os.chmod(locked, 0)
    try:
        if os.access(locked, os.R_OK):  # e.g. running as root
            pytest.skip("can't make a directory unreadable here")
        with pytest.raises(PeerSetupError):
            agy_mcp_servers(str(ws), [])
    finally:
        os.chmod(locked, 0o755)


def test_doctor_scans_configured_add_dirs_even_with_allow_mcp(env, capsys):
    from agent_bus import cli
    put(env["repo"] / "extra" / ".agents" / "mcp_config.json", SERVER)
    set_args(env, "antigravity", ["--add-dir", str(env["repo"] / "extra")], allow_mcp=True)
    assert cli.main(["doctor"]) == 1
    assert "UNSAFE waiver" in capsys.readouterr().out


def test_codex_project_servers_missed_by_the_listing_fail_closed(env):
    put(env["repo"] / ".codex" / "config.toml", '[mcp_servers.local]\ncommand = "x"\n')
    with pytest.raises(PeerSetupError) as e:
        adapter("codex", cfg.Config.load()).invocation("hi", None, str(env["repo"]), [])
    assert "local" in str(e.value)
    put(env["repo"] / ".codex" / "config.toml", '[mcp_servers.local]\ncommand = "x"\nenabled = false\n')
    adapter("codex", cfg.Config.load()).invocation("hi", None, str(env["repo"]), [])


@pytest.mark.parametrize("args", [["--profile", "fast"], ["-pfast"], ["-c", "mcp_servers={}"],
                                  ["-c", 'projects."/x".trust_level="trusted"'], ["--add-dir", "/tmp"]])
def test_codex_unsafe_configured_args_are_refused(env, args):
    set_args(env, "codex", args)
    with pytest.raises(PeerSetupError):
        adapter("codex", cfg.Config.load()).invocation("hi", None, str(env["repo"]), [])


def test_agent_bus_exemption_follows_the_running_installation(env, monkeypatch):
    from agent_bus import peers
    mine = env["tmp"] / "inst" / "agent-bus"
    other = env["tmp"] / "other" / "agent-bus"
    for p in (mine, other):
        put(p, "#!/bin/sh\n")
    monkeypatch.setattr(peers.sys, "argv", [str(mine), "run-job"])
    monkeypatch.setattr(peers.shutil, "which", lambda *a, **k: str(other))  # PATH points elsewhere
    spec = {"args": ["mcp", "--agent", "antigravity"]}
    assert trusted_agent_bus_entry("agent-bus", {**spec, "command": str(mine)})
    assert not trusted_agent_bus_entry("agent-bus", {**spec, "command": str(other)})
