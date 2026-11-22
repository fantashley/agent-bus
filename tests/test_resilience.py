"""Stage 1 of issue #32 (decided on agent-bus thread t-35d692, P3): versions per turn,
PEER_FORMAT_CHANGED, unknown-event summaries, strict Codex MCP enumeration."""
import json
import os
import time

import pytest

from agent_bus import config as cfg
from agent_bus.db import HUMAN, SYSTEM
from agent_bus.peers import PeerSetupError, adapter, cli_identity, cli_version
from agent_bus.service import Service
from agent_bus.worker import Worker

BASE, HEAD = "a" * 40, "b" * 40


def run_job(bus, tid):
    job = bus.jobs(tid)[-1]
    assert Worker(bus, job["id"]).run() == 0
    return bus.job(job["id"])


def calls(env, agent):
    if not env["log"].exists():
        return []
    return [c for c in map(json.loads, env["log"].read_text().splitlines()) if c["agent"] == agent]


def events(bus, tid, kind):
    return [e["text"] for e in bus._all("SELECT * FROM events WHERE thread_id=? AND kind=? ORDER BY id", tid, kind)]


def test_version_is_probed_once_per_binary_and_recorded_per_turn(env, no_spawn, monkeypatch):
    bus = env["bus"]
    command = cfg.Config.load().peer_command("codex")
    monkeypatch.setenv("FAKE_VERSION", "codex-cli 9.9.9")
    assert cli_version(bus, "codex", command) == "codex-cli 9.9.9"
    monkeypatch.setenv("FAKE_VERSION", "codex-cli 1.0.0")
    assert cli_version(bus, "codex", command) == "codex-cli 9.9.9"  # cached for this binary
    # A rebuilt binary is a new identity: probed again.
    os.utime(os.path.realpath(cli_identity(command)[0]), ns=(time.time_ns(), time.time_ns() + 10**9))
    assert cli_version(bus, "codex", command) == "codex-cli 1.0.0"
    assert cli_version(bus, "codex", "/nonexistent/codex") == "unknown"

    tid = Service(bus, HUMAN).start_discussion("q", peers=["codex"])["thread"]["id"]
    run_job(bus, tid)
    starts = events(bus, tid, "turn_start")
    assert starts and all("codex-cli 1.0.0 · recipe codex/1" in s for s in starts)


def test_exit_zero_without_a_reply_is_format_drift(env, no_spawn, monkeypatch):
    bus = env["bus"]
    monkeypatch.setenv("FAKE_BEHAVIOUR", "silent")
    res = adapter("claude", cfg.Config.load()).run("hi", str(env["repo"]), 30, lambda k, t: None)
    assert not res.ok and res.format_changed and res.error.startswith("PEER_FORMAT_CHANGED")
    assert "without a reply" in res.error

    tid = Service(bus, HUMAN).start_discussion("q", peers=["claude", "codex"])["thread"]["id"]
    run_job(bus, tid)
    msgs = bus.history(tid)["messages"]
    notices = [m["body"] for m in msgs if m["sender"] == SYSTEM and m["body"].startswith("PEER_FORMAT_CHANGED")]
    assert len(notices) == 2  # one per peer, deduplicated across rounds
    assert any("claude (fake-claude 0.0.0)" in n for n in notices)
    assert not [m for m in msgs if m["sender"] in ("claude", "codex")]  # nothing half-posted


def test_format_drift_in_review_needs_human_without_a_fresh_retry(env, no_spawn, monkeypatch):
    bus = env["bus"]
    monkeypatch.setattr("agent_bus.worker.Worker.review_diff", lambda self, refs, interdiff: ("diff", "Diff"))
    me = Service(bus, "claude", bus.register_session("claude", env={}))
    tid = me.request_review("codex", str(env["repo"]), BASE, HEAD)["thread"]["id"]
    run_job(bus, tid)  # a normal first pass pins a session
    sid = bus.peer_session(tid, "codex")
    monkeypatch.setenv("FAKE_BEHAVIOUR", "silent")
    me.request_review("codex", str(env["repo"]), BASE, "c" * 40, prev_head_commit_id=HEAD)
    before = len(calls(env, "codex"))
    run_job(bus, tid)
    assert bus.thread(tid)["status"] == "needs_human"
    assert len(calls(env, "codex")) == before + 1  # the resumed attempt only, no fresh session
    assert bus.peer_session(tid, "codex") == sid  # the pinned session survives


def test_unknown_events_are_summarised_not_fatal(env, monkeypatch):
    monkeypatch.setenv("FAKE_BEHAVIOUR", "newevents")
    for agent in cfg.AGENTS:
        seen = []
        res = adapter(agent, cfg.Config.load()).run("hi", str(env["repo"]), 30,
                                                    lambda k, t: seen.append((k, t)))
        assert res.ok, res.error
        assert ("format", f"unrecognised {agent} stream events: brand_new_event×2") in seen


@pytest.mark.parametrize("listing", [
    '[{"enabled": true}]',                       # no name
    '[{"name": "", "enabled": true}]',
    '[{"name": "x", "enabled": "yes"}]',
    '["github"]',
])
def test_malformed_codex_mcp_entries_fail_closed(env, monkeypatch, listing):
    monkeypatch.setenv("FAKE_MCP_LIST", listing)
    with pytest.raises(PeerSetupError):
        adapter("codex", cfg.Config.load()).mcp_servers(str(env["repo"]))
    res = adapter("codex", cfg.Config.load()).run("hi", str(env["repo"]), 30, lambda k, t: None)
    assert not res.ok and "codex mcp list" in res.error


def test_agents_and_whoami_show_versions(env, no_spawn, capsys):
    import asyncio
    from agent_bus import cli, mcp_server
    assert cli.main(["agents"]) == 0
    assert "(fake-codex 0.0.0)" in capsys.readouterr().out
    server = mcp_server.build("claude", bus=env["bus"])
    payload = json.loads(asyncio.run(server.call_tool("whoami", {})).content[0].text)
    assert payload["versions"]["codex"] == "fake-codex 0.0.0" and "claude" not in payload["versions"]


# ------------------------------------------------- review fixes (t-a3faf1 pass 1)


def test_stray_stdout_is_not_a_reply(env, monkeypatch):
    monkeypatch.setenv("FAKE_BEHAVIOUR", "silent")
    monkeypatch.setenv("FAKE_STRAY", "1")
    seen = []
    res = adapter("codex", cfg.Config.load()).run("hi", str(env["repo"]), 30, lambda k, t: seen.append((k, t)))
    assert res.format_changed and "without a reply" in res.error
    assert any(k == "format" and "non-JSON stdout" in t for k, t in seen)


def test_format_drift_during_a_repair_posts_nothing(env, no_spawn, monkeypatch):
    bus = env["bus"]
    monkeypatch.setenv("FAKE_BEHAVIOUR", "nojson")
    monkeypatch.setenv("FAKE_SILENT_REPAIR", "codex")
    monkeypatch.setattr("agent_bus.worker.Worker.review_diff", lambda self, refs, interdiff: ("diff", "Diff"))
    me = Service(bus, "claude", bus.register_session("claude", env={}))
    tid = me.request_review("codex", str(env["repo"]), BASE, HEAD)["thread"]["id"]
    run_job(bus, tid)
    assert bus.thread(tid)["status"] == "needs_human"
    assert not [m for m in bus.history(tid)["messages"] if m["sender"] == "codex"]

    tid = Service(bus, HUMAN).start_discussion("q", peers=["codex"], max_rounds=1)["thread"]["id"]
    run_job(bus, tid)
    assert not [m for m in bus.history(tid)["messages"] if m["sender"] == "codex"]  # not even the malformed reply


def test_bad_mcp_entry_error_names_only_index_and_field(env, monkeypatch):
    monkeypatch.setenv("FAKE_MCP_LIST", '[{"name": "ok"}, {"command": "x", "env": {"TOKEN": "s3cret"}}]')
    with pytest.raises(PeerSetupError) as e:
        adapter("codex", cfg.Config.load()).mcp_servers(str(env["repo"]))
    assert "entry #1" in str(e.value) and "name" in str(e.value) and "s3cret" not in str(e.value)


def test_version_probe_resolves_relative_commands_in_the_turn_cwd(env, monkeypatch):
    bus = env["bus"]
    real = cfg.Config.load().peer_command("codex")
    work = env["tmp"] / "work"
    (work / "bin").mkdir(parents=True)
    (work / "bin" / "codex").symlink_to(real)
    assert cli_version(bus, "codex", "bin/codex", cwd=str(work)) == "fake-codex 0.0.0"
    assert cli_version(bus, "codex", "bin/codex", cwd=str(env["tmp"])) == "unknown"
    monkeypatch.setenv("PATH", "bin" + os.pathsep + os.environ["PATH"])  # a relative PATH entry
    assert os.path.realpath(cli_identity("codex", cwd=str(work))[0]) == os.path.realpath(real)


def test_non_string_session_id_is_not_pinned(env):
    import stat
    import sys
    script = env["tmp"] / "int-session-codex"
    script.write_text(f"#!{sys.executable}\nimport json, sys\n"
                      "if 'list' in sys.argv: print('[]'); sys.exit(0)\n"
                      "print(json.dumps({'type': 'thread.started', 'thread_id': 12345}))\n"
                      "print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'hi'}}))\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    conf_path = env["tmp"] / "config" / "agent-bus" / "config.json"
    conf = json.loads(conf_path.read_text())
    conf["peers"]["codex"] = {"command": str(script)}
    conf_path.write_text(json.dumps(conf))
    res = adapter("codex", cfg.Config.load()).run("hi", str(env["repo"]), 30, lambda k, t: None)
    assert res.format_changed and "without a session id" in res.error and res.external_id is None


def test_aliases_of_one_binary_are_probed_separately(env, no_spawn):
    bus = env["bus"]
    real = cfg.Config.load().peer_command("codex")
    a, b = env["tmp"] / "alias-a", env["tmp"] / "alias-b"
    a.symlink_to(real)
    b.symlink_to(real)
    assert cli_identity(str(a))[1] != cli_identity(str(b))[1]
    cli_version(bus, "codex", str(a))
    cli_version(bus, "codex", str(b))
    assert len(bus._all("SELECT * FROM cli_versions")) == 2



# ------------------------------------------- Antigravity workspace MCP sources (#32)


def agy_check(env, cwd, add_dirs=()):
    ad = adapter("antigravity", cfg.Config.load())
    ad.check_isolation(str(cwd), list(add_dirs))


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data) if not isinstance(data, str) else data)


SERVER = {"mcpServers": {"sentinel": {"command": "true"}}}


@pytest.mark.parametrize("layout", ["agents-mcp", "agents-plugins-dir", "plugins-json-entry", "inherits",
                                    "parent-dir", "add-dir", "global-plugins-json"])
def test_agy_preflight_sees_workspace_and_global_plugin_sources(env, layout):
    ws = env["tmp"] / "ws"
    sub = ws / "pkg" / "deep"
    sub.mkdir(parents=True)
    cwd, add = ws, []
    if layout == "agents-mcp":
        write(ws / ".agents" / "mcp_config.json", SERVER)
    elif layout == "agents-plugins-dir":
        write(ws / ".agents" / "plugins" / "s" / "mcp_config.json", SERVER)
    elif layout == "plugins-json-entry":
        write(ws / ".agents" / "plugins.json", {"entries": [{"path": "vendor"}]})
        write(ws / "vendor" / "s" / "mcp_config.json", SERVER)
    elif layout == "inherits":
        write(ws / ".agents" / "plugins.json", {"inherits": [{"path": str(ws / "shared.json")}]})
        write(ws / "shared.json", {"entries": [{"path": str(ws / "vendor")}]})
        write(ws / "vendor" / "s" / "mcp_config.json", SERVER)
    elif layout == "parent-dir":  # started in a subdirectory: the repo's .agents still applies
        write(ws / ".agents" / "mcp_config.json", SERVER)
        cwd = sub
    elif layout == "add-dir":
        other = env["tmp"] / "other"
        write(other / ".agents" / "mcp_config.json", SERVER)
        add = [other]
    else:
        write(env["tmp"] / "agy" / "plugins.json", {"entries": [{"path": str(ws / "vendor")}]})
        write(ws / "vendor" / "s" / "mcp_config.json", SERVER)
    with pytest.raises(PeerSetupError) as e:
        agy_check(env, cwd, add)
    assert "sentinel" in str(e.value)


def test_agy_preflight_allows_clean_and_disabled_and_fails_closed_on_garbage(env):
    ws = env["tmp"] / "ws"
    ws.mkdir()
    agy_check(env, ws)  # nothing configured
    write(ws / ".agents" / "mcp_config.json", {"mcpServers": {"s": {"command": "x", "disabled": True}}})
    agy_check(env, ws)  # disabled servers don't count
    write(ws / ".agents" / "plugins.json", "{not json")
    with pytest.raises(PeerSetupError):
        agy_check(env, ws)
    write(ws / ".agents" / "plugins.json", {"entries": ["just-a-string"]})
    with pytest.raises(PeerSetupError):
        agy_check(env, ws)


def test_agy_exempts_only_the_real_agent_bus_registration(env, monkeypatch):
    from agent_bus import peers
    real = env["tmp"] / "bin" / "agent-bus"
    real.write_text("#!/bin/sh\n")
    real.chmod(0o755)
    monkeypatch.setattr(peers.shutil, "which", lambda cmd, path=None: str(real) if cmd == "agent-bus" else None)
    ok = {"command": str(real), "args": ["mcp", "--agent", "antigravity"]}
    assert peers.trusted_agent_bus_entry("agent-bus", ok)
    impostor = env["tmp"] / "evil" / "agent-bus"
    impostor.parent.mkdir()
    impostor.write_text("#!/bin/sh\n")
    for spec in ({**ok, "command": str(impostor)},                    # same name, different binary
                 {**ok, "args": ["mcp", "--agent", "claude"]},        # wrong identity
                 {**ok, "env": {"X": "1"}},                           # extra transport keys
                 {"url": "https://example.invalid/mcp"},
                 "not-an-object"):
        assert not peers.trusted_agent_bus_entry("agent-bus", spec), spec
    assert not peers.trusted_agent_bus_entry("other", ok)
    ws = env["tmp"] / "ws"
    write(ws / ".agents" / "mcp_config.json", {"mcpServers": {"agent-bus": {**ok, "command": str(impostor)}}})
    with pytest.raises(PeerSetupError) as e:
        agy_check(env, ws)
    assert "agent-bus" in str(e.value)
