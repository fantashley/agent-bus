"""`agent-bus doctor` (issue #32 stage 2A, agent-bus thread t-606fcd, P4): a passive local
inspection that runs no agent turn and contacts nothing."""
import json

import pytest

from agent_bus import autoresume, cli, doctor, peers
from agent_bus import config as cfg
from agent_bus.peers import ADAPTERS, adapter


@pytest.fixture(autouse=True)
def no_machine_codex_config(tmp_path, monkeypatch):
    """Never this machine's /etc/codex files, nor its platform."""
    monkeypatch.setattr(doctor, "MACOS", False)
    monkeypatch.setattr(doctor, "CODEX_SYSTEM_CONFIG", tmp_path / "etc-codex" / "config.toml")
    monkeypatch.setattr(doctor, "CODEX_MANAGED", (tmp_path / "etc-codex" / "managed_config.toml",
                                                  tmp_path / "etc-codex" / "requirements.toml"))


@pytest.fixture
def passive(monkeypatch):
    """Fail the test if doctor lists MCP servers through a CLI, starts a turn or sweeps."""
    def refuse(*a, **k):
        raise AssertionError("doctor must stay passive")
    monkeypatch.setattr(peers.CodexAdapter, "mcp_servers", refuse)
    monkeypatch.setattr(peers.CopilotAdapter, "mcp_servers", refuse)
    monkeypatch.setattr(peers.Adapter, "run", refuse)
    monkeypatch.setattr(autoresume, "sweep_quietly", refuse)
    monkeypatch.setattr(autoresume, "sweep", refuse)


@pytest.fixture
def no_cloud(monkeypatch):
    """Model the config files alone: the cloud bundle doctor can never rule out is set aside."""
    monkeypatch.setattr(doctor, "CODEX_CLOUD_CONFIG", None)


def set_config(env, **extra):
    path = env["tmp"] / "config" / "agent-bus" / "config.json"
    conf = json.loads(path.read_text())
    for key, value in extra.items():
        if key == "peers":
            for agent, settings in value.items():
                conf["peers"][agent].update(settings)
        else:
            conf[key] = value
    path.write_text(json.dumps(conf))


def run(capsys, *args):
    rc = cli.main(["doctor", *args])
    return rc, capsys.readouterr().out


def codex_home(env):
    home = env["tmp"] / "codex"
    home.mkdir(exist_ok=True)
    return home


def test_doctor_reports_each_agent_without_turns_or_listings(env, passive, capsys):
    rc, out = run(capsys, "--repo", str(env["repo"]))
    assert rc == 0
    assert "passive: no agent turns, no network" in out
    for agent, recipe in (("claude", "6"), ("codex", "5"), ("antigravity", "13"), ("copilot", "3")):
        assert f"fake-{agent} 0.0.0" in out and f"recipe       {agent}/{recipe}" in out
    assert 'INCONCLUSIVE: --ignore-rules, -c sandbox_mode="read-only"' in out
    assert "INCONCLUSIVE: --permission-mode dontAsk" in out
    assert "INCONCLUSIVE: --strict-mcp-config" in out
    assert "FAILED" not in out
    # No turn ran: the fake CLIs log every turn, and `--version` isn't one.
    assert not env["log"].exists() or not env["log"].read_text().strip()
    assert env["bus"]._all("SELECT id FROM jobs") == []


def test_doctor_json(env, passive, capsys):
    rc, out = run(capsys, "--repo", str(env["repo"]), "--json")
    rep = json.loads(out)
    assert rc == 0 and rep["passive"] is True and rep["checkout"] == str(env["repo"]) and rep["failed"] == 0
    codex = rep["agents"]["codex"]
    assert codex["version"] == "fake-codex 0.0.0" and codex["recipe"] == "codex/5"
    assert codex["executable"]["path"].endswith("fake-codex")
    for a in rep["agents"].values():
        assert a["write_isolation"]["status"] == "inconclusive"
        assert a["mcp_isolation"]["status"] == "inconclusive"
        assert a["failed_checks"] == []
    inv = codex["mcp_isolation"]["inventory"]
    # Codex's workspace-managed cloud config can't be ruled out passively, so never complete.
    assert not inv["complete"] and not inv["gate_eligible"] and inv["servers"] == []
    assert inv["unaccounted"] == [doctor.CODEX_CLOUD_CONFIG]


def test_doctor_only_one_agent_and_unknown_names(env, passive, capsys):
    rc, out = run(capsys, "--agent", "codex", "--repo", str(env["repo"]), "--json")
    assert rc == 0 and list(json.loads(out)["agents"]) == ["codex"]
    assert cli.main(["doctor", "--agent", "nobody"]) == 2


def test_doctor_lists_unavailable_agents(env, passive, capsys):
    set_config(env, agents=["claude"])
    rep = json.loads(run(capsys, "--json", "--repo", str(env["repo"]))[1])
    assert rep["agents"]["claude"]["available"] is True
    assert rep["agents"]["codex"] == {"available": False, "reason": "not listed in config `agents`"}


def test_codex_inventory_reads_every_config_layer(env, passive, no_cloud, capsys):
    (codex_home(env) / "config.toml").write_text(
        '[mcp_servers.github]\ncommand = "gh-mcp"\n'
        '[mcp_servers.docs]\nurl = "https://example.invalid/mcp"\nenabled = false\n')
    doctor.CODEX_SYSTEM_CONFIG.parent.mkdir(parents=True)
    doctor.CODEX_SYSTEM_CONFIG.write_text('[mcp_servers.fleet]\ncommand = "fleet"\n')
    (env["repo"] / ".git").mkdir()
    (env["repo"] / ".codex").mkdir()
    # A project file loads only in a trusted project: listed, but it decides nothing.
    (env["repo"] / ".codex" / "config.toml").write_text('[mcp_servers.github]\nenabled = false\n'
                                                         '[mcp_servers.local]\ncommand = "local"\n'
                                                         '[mcp_servers.off]\ncommand = "x"\nenabled = false\n'
                                                         '[mcp_servers."has.dot"]\ncommand = "x"\n')
    set_config(env, peers={"codex": {"args": ["-c", 'mcp_servers.extra.command="x"',
                                              "-c", "mcp_servers.fleet.enabled=false"]}})
    rc, out = run(capsys, "--json", "--repo", str(env["repo"]))
    inv = json.loads(out)["agents"]["codex"]["mcp_isolation"]["inventory"]
    assert rc == 0 and inv["complete"] and inv["gate_eligible"] and inv["profile"] is None, inv["unaccounted"]
    got = {s["name"]: (s["sources"], s["definite"], s["nameable"]) for s in inv["servers"]}
    assert got == {
        "docs": (["user config"], True, True),
        "extra": (["configured args"], True, True),
        "fleet": (["system config", "configured args"], True, True),
        "github": (["user config", "project config"], True, True),
        "has.dot": (["project config"], False, False),
        "local": (["project config"], False, True),
        "off": (["project config"], False, True),
    }
    assert {s["source"] for s in inv["sources"]} == {"system config", "user config", "project config", "plugins",
                                                     "apps"}
    out = run(capsys, "--repo", str(env["repo"]), "--agent", "codex")[1]
    assert "mcp servers  from config files (complete):" in out and "docs (user config)" in out
    assert "local (project config; loads only if the project is trusted)" in out
    assert "has.dot (project config; loads only if the project is trusted; its name can't be written" in out


def test_codex_home_follows_the_participants_home(env, passive, no_cloud, capsys, monkeypatch):
    monkeypatch.delenv("CODEX_HOME")
    other = env["tmp"] / "other-home"
    (other / ".codex").mkdir(parents=True)
    (other / ".codex" / "config.toml").write_text('[mcp_servers.theirs]\ncommand = "x"\n')
    set_config(env, peers={"codex": {"env": {"HOME": str(other)}}})
    inv = json.loads(run(capsys, "--json", "--repo", str(env["repo"]), "--agent", "codex")[1])[
        "agents"]["codex"]["mcp_isolation"]["inventory"]
    assert inv["codex_home"] == str(other / ".codex") and [s["name"] for s in inv["servers"]] == ["theirs"]


def fifo(path):
    import os
    path.parent.mkdir(parents=True, exist_ok=True)
    os.mkfifo(path)


@pytest.mark.parametrize("setup, why", [
    (lambda env: (codex_home(env) / "config.toml").write_text("[mcp_servers\n"), "can't be read"),
    (lambda env: (codex_home(env) / "config.toml").write_text('mcp_servers = "x"\n'), "isn't a table"),
    (lambda env: (codex_home(env) / "config.toml").write_text("[mcp_servers]\nx = 1\n"), "'x' in the user config"),
    (lambda env: (codex_home(env) / "config.toml").write_text('[mcp_servers.x]\nenabled = "no"\n'),
     "isn't true/false"),
    (lambda env: (doctor.CODEX_MANAGED[0].parent.mkdir(parents=True), doctor.CODEX_MANAGED[0].write_text("")),
     "isn't interpreted"),
    (lambda env: (doctor.CODEX_MANAGED[1].parent.mkdir(parents=True), doctor.CODEX_MANAGED[1].write_text("")),
     "isn't interpreted"),
    (lambda env: set_config(env, peers={"codex": {"args": ["-c", 'mcp_servers."a.b".command="x"']}}),
     "key doctor doesn't read"),
    (lambda env: set_config(env, peers={"codex": {"args": ["-c", '"profile"="work"']}}), "key doctor doesn't read"),
    (lambda env: set_config(env, peers={"codex": {"args": ["-c", 'project_root_markers.0=".hg"']}}),
     "sets project_root_markers"),
    (lambda env: set_config(env, peers={"codex": {"args": ["-c", "mcp_servers.x..enabled=false"]}}),
     "key doctor doesn't read"),
    (lambda env: set_config(env, peers={"codex": {"args": ["-c", "mcp_servers.x.enabled.foo=false"]}}),
     "doesn't model"),
    (lambda env: (codex_home(env) / "config.toml").write_text('[mcp_servers.x]\nenabled = false\n'),
     "exactly one of command or url"),
    (lambda env: (codex_home(env) / "config.toml").write_text('[mcp_servers.x]\ncommand = "a"\nurl = "b"\n'),
     "exactly one of command or url"),
    (lambda env: (codex_home(env) / "config.toml").write_text('[mcp_servers.x]\ncommand = ""\n'),
     "isn't a non-empty string"),
    (lambda env: ((env["repo"] / ".codex").mkdir(),
                  (env["repo"] / ".codex" / "config.toml").write_text('[mcp_servers.x]\nenabled = false\n')),
     "exactly one of command or url"),
    (lambda env: ((codex_home(env) / "config.toml").write_text('[mcp_servers.x]\ncommand = "a"\n'),
                  (env["repo"] / ".codex").mkdir(),
                  (env["repo"] / ".codex" / "config.toml").write_text('[mcp_servers.x]\nurl = "b"\n')),
     "exactly one of command or url"),
    (lambda env: set_config(env, peers={"codex": {"args": ["-c", 'mcp_servers.x={command="x",enabled=false}']}}),
     "doesn't model"),
    (lambda env: set_config(env, peers={"codex": {"args": ["--profile", "work"]}}), "profile 'work'"),
    (lambda env: set_config(env, peers={"codex": {"args": ["-c", "project_root_markers=[]"]}}),
     "project_root_markers"),
    (lambda env: (codex_home(env) / "config.toml").write_text('profile = 1979-05-27\n'), "profile '1979-05-27'"),
    (lambda env: (codex_home(env) / "config.toml").write_text('project_root_markers = [".hg"]\n'),
     "project_root_markers"),
    (lambda env: (codex_home(env) / "config.toml").write_text('[mcp_servers.x]\nenabled = true\n'),
     "exactly one of command or url"),
    (lambda env: fifo(codex_home(env) / "config.toml"), "isn't a regular file"),
    (lambda env: fifo(doctor.CODEX_MANAGED[0]), "isn't a regular file"),
    (lambda env: set_config(env, peers={"codex": {"args": ["-c", 'profiles.p.mcp_servers.x.command="x"']}}),
     "doesn't model"),
])
def test_codex_inventory_is_incomplete_when_a_source_is_unaccounted_for(env, passive, no_cloud, capsys, setup, why):
    setup(env)
    rc, out = run(capsys, "--json", "--repo", str(env["repo"]), "--agent", "codex")
    inv = json.loads(out)["agents"]["codex"]["mcp_isolation"]["inventory"]
    assert not inv["complete"] and not inv["gate_eligible"]
    assert any(why in u for u in inv["unaccounted"]), inv["unaccounted"]
    assert rc == 0  # incomplete is ineligible for the future gate, not a failed check
    assert "INCOMPLETE, not eligible for the launch gate" in run(capsys, "--repo", str(env["repo"]),
                                                                 "--agent", "codex")[1]


def test_codex_inventory_with_a_relative_codex_home(env, passive, no_cloud, capsys, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", "codex")
    inv = json.loads(run(capsys, "--json", "--repo", str(env["repo"]), "--agent", "codex")[1])[
        "agents"]["codex"]["mcp_isolation"]["inventory"]
    assert not inv["complete"] and "relative" in inv["unaccounted"][0]


def test_codex_inventory_on_macos_is_incomplete(env, passive, no_cloud, capsys, monkeypatch):
    monkeypatch.setattr(doctor, "MACOS", True)
    inv = json.loads(run(capsys, "--json", "--repo", str(env["repo"]), "--agent", "codex")[1])[
        "agents"]["codex"]["mcp_isolation"]["inventory"]
    assert not inv["complete"] and "MDM" in inv["unaccounted"][0]


def test_codex_inventory_survives_an_unreadable_directory(env, passive, no_cloud, capsys, monkeypatch):
    real = doctor.Path.stat

    def stat(self, *a, **k):
        if self.name == "config.toml" and "codex" in str(self):
            raise PermissionError(13, "denied")
        return real(self, *a, **k)
    monkeypatch.setattr(doctor.Path, "stat", stat)
    inv = json.loads(run(capsys, "--json", "--repo", str(env["repo"]), "--agent", "codex")[1])[
        "agents"]["codex"]["mcp_isolation"]["inventory"]
    assert not inv["complete"] and any("can't be checked (PermissionError)" in u for u in inv["unaccounted"])


def test_codex_server_that_cant_be_switched_off_is_flagged_not_failed(env, passive, capsys):
    """Whether it's enabled (and so refused at launch) isn't modelled: it is flagged only."""
    (codex_home(env) / "config.toml").write_text('[mcp_servers."has.dot"]\ncommand = "x"\n')
    rc, out = run(capsys, "--json", "--repo", str(env["repo"]), "--agent", "codex")
    codex = json.loads(out)["agents"]["codex"]
    assert rc == 0 and codex["failed_checks"] == [] and codex["mcp_isolation"]["status"] == "inconclusive"
    assert codex["mcp_isolation"]["inventory"]["servers"][0]["nameable"] is False


def test_codex_checkout_config_that_writes_outside_the_sandbox_fails(env, passive, capsys):
    (env["repo"] / ".codex").mkdir()
    (env["repo"] / ".codex" / "config.toml").write_text('log_dir = "/tmp/x"\n')
    rc, out = run(capsys, "--repo", str(env["repo"]), "--agent", "codex")
    assert rc == 1 and "FAILED       write: checkout's Codex config: PEER_SETUP" in out
    assert "write        FAILED:" in out and "mcp          INCONCLUSIVE:" in out


def test_antigravity_configured_mcp_server_is_a_known_failed_check(env, passive, capsys):
    (env["tmp"] / "agy" / "mcp_config.json").write_text(json.dumps({"mcpServers": {"github": {"command": "x"}}}))
    rc, out = run(capsys, "--json", "--repo", str(env["repo"]))
    rep = json.loads(out)
    agy = rep["agents"]["antigravity"]
    assert rc == 1 and rep["failed"] == 1
    assert agy["mcp_isolation"]["status"] == "failed" and agy["write_isolation"]["status"] == "inconclusive"
    assert "github" in agy["failed_checks"][0]["detail"]


def test_antigravity_allow_mcp_is_reported_as_waived(env, passive, capsys):
    (env["tmp"] / "agy" / "mcp_config.json").write_text(json.dumps({"mcpServers": {"github": {"command": "x"}}}))
    set_config(env, peers={"antigravity": {"allow_mcp": True}})
    rc, out = run(capsys, "--json", "--repo", str(env["repo"]), "--agent", "antigravity")
    agy = json.loads(out)["agents"]["antigravity"]
    assert rc == 0 and agy["mcp_isolation"]["status"] == "waived" and "unsafe" in agy["mcp_isolation"]["detail"]


def test_copilot_checkout_hooks_are_a_known_failed_check(env, passive, capsys):
    (env["repo"] / ".github" / "hooks").mkdir(parents=True)
    (env["repo"] / ".github" / "hooks" / "h.json").write_text(json.dumps({"hooks": {"sessionStart": [{"x": 1}]}}))
    rc, out = run(capsys, "--json", "--repo", str(env["repo"]), "--agent", "copilot")
    cop = json.loads(out)["agents"]["copilot"]
    assert rc == 1 and cop["write_isolation"]["status"] == "failed"
    assert "copilot mcp list" in cop["mcp_isolation"]["note"]


@pytest.mark.parametrize("web", [True, False])
@pytest.mark.parametrize("harness", sorted(ADAPTERS))
def test_requested_flags_are_the_ones_a_turn_passes(env, harness, web):
    """What doctor shows must be what a turn's own invocation passes."""
    set_config(env, peers={harness: {"web_search": web}})
    ad = adapter(harness, cfg.Config.load())
    argv = " ".join(ad.invocation("hi", None, str(env["repo"]), []).argv)
    req = ad.isolation_request()
    for flag in req["write"] + req["mcp"]:
        if "<name>" in flag:  # one per server; the fake listings name some
            flag = flag.split("<name>")[0]
        assert flag in argv, (harness, flag)


def test_text_report_escapes_terminal_controls(env, passive, capsys):
    (codex_home(env) / "config.toml").write_text('[mcp_servers."evil\\u001b[2Jx\\nFAILED fake"]\ncommand = "x"\n')
    out = run(capsys, "--repo", str(env["repo"]), "--agent", "codex")[1]
    assert "\x1b" not in out and "evil?[2Jx?FAILED fake" in out


def test_version_is_not_probed_through_the_checkout(env, passive, capsys, monkeypatch):
    """A CLI found through a relative PATH entry is the checkout's program: doctor never runs it."""
    import shutil
    (env["repo"] / "bin").mkdir()
    shutil.copy(env["tmp"] / "bin" / "fake-codex", env["repo"] / "bin" / "codex")
    monkeypatch.setenv("PATH", "bin:" + __import__("os").environ["PATH"])
    set_config(env, peers={"codex": {"command": "codex"}})
    called = []
    monkeypatch.setattr(peers, "probe_version", lambda *a, **k: called.append(a) or "probed")
    codex = json.loads(run(capsys, "--json", "--repo", str(env["repo"]), "--agent", "codex")[1])["agents"]["codex"]
    assert codex["available"] and codex["executable"]["path"] == str(env["repo"] / "bin" / "codex")
    assert codex["version"].startswith("unknown (not probed") and called == []


def test_claude_mcp_config_in_args_is_reported_as_waived(env, passive, capsys):
    set_config(env, peers={"claude": {"args": ["--mcp-config", '{"mcpServers": {"secret": {}}}']}})
    rc, out = run(capsys, "--json", "--repo", str(env["repo"]), "--agent", "claude")
    claude = json.loads(out)["agents"]["claude"]
    assert claude["mcp_isolation"]["status"] == "waived" and "secret" not in out


def test_antigravity_configured_add_dir_is_checked(env, passive, capsys):
    extra = env["tmp"] / "extra"
    (extra / ".agents").mkdir(parents=True)
    (extra / ".agents" / "mcp_config.json").write_text(json.dumps({"mcpServers": {"x": {"command": "x"}}}))
    set_config(env, peers={"antigravity": {"args": ["--add-dir", str(extra)]}})
    rc, out = run(capsys, "--json", "--repo", str(env["repo"]), "--agent", "antigravity")
    assert rc == 1 and "workspace" in json.loads(out)["agents"]["antigravity"]["failed_checks"][0]["detail"]


def test_antigravity_mcp_servers_of_the_wrong_shape_are_a_failed_check(env, passive, capsys):
    (env["tmp"] / "agy" / "mcp_config.json").write_text(json.dumps({"mcpServers": ["x"]}))
    rc, out = run(capsys, "--json", "--repo", str(env["repo"]), "--agent", "antigravity")
    assert rc == 1 and "isn't an object" in json.loads(out)["agents"]["antigravity"]["failed_checks"][0]["detail"]


def test_project_transport_doesnt_complete_a_user_server(env, passive, no_cloud, capsys):
    (codex_home(env) / "config.toml").write_text('[mcp_servers.x]\nenabled = true\n')
    (env["repo"] / ".codex").mkdir()
    (env["repo"] / ".codex" / "config.toml").write_text('[mcp_servers.x]\ncommand = "x"\n')
    inv = json.loads(run(capsys, "--json", "--repo", str(env["repo"]), "--agent", "codex")[1])[
        "agents"]["codex"]["mcp_isolation"]["inventory"]
    assert not inv["complete"] and "exactly one of command or url" in inv["unaccounted"][0]


def test_version_is_probed_from_the_root_and_not_cached(env, passive, capsys, monkeypatch):
    seen, real = [], peers.probe_version
    monkeypatch.setattr(peers, "probe_version", lambda launch, cwd: seen.append(cwd) or "from-root 1.0")
    out = run(capsys, "--json", "--repo", str(env["repo"]), "--agent", "codex")[1]
    assert seen == ["/"] and json.loads(out)["agents"]["codex"]["version"] == "from-root 1.0"
    ident = peers.cli_identity(cfg.Config.load().peer_command("codex"), str(env["repo"]))
    assert env["bus"].cli_version(ident[1]) is None  # the turns' cache is left alone
    monkeypatch.setattr(peers, "probe_version", real)
    assert peers.cli_version(env["bus"], "codex", ident[0]) == "fake-codex 0.0.0"
    seen.clear()
    monkeypatch.setattr(peers, "probe_version", lambda launch, cwd: seen.append(cwd) or "x")
    out = run(capsys, "--json", "--repo", str(env["repo"]), "--agent", "codex")[1]
    assert seen == [] and json.loads(out)["agents"]["codex"]["version"] == "fake-codex 0.0.0"  # cached


def test_repo_must_be_a_directory(env, passive, capsys):
    assert cli.main(["doctor", "--repo", str(env["tmp"] / "missing")]) == 2
    assert "not a directory" in capsys.readouterr().err


def test_a_checkout_check_that_cant_read_its_files_is_a_failed_check(env, passive, capsys, monkeypatch):
    def denied(cwd):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(peers, "_codex_project_check", denied)
    rc, out = run(capsys, "--json", "--repo", str(env["repo"]), "--agent", "codex")
    codex = json.loads(out)["agents"]["codex"]
    assert rc == 1 and "can't be checked (PermissionError" in codex["failed_checks"][0]["detail"]


def test_project_files_are_trusted_one_by_one(env, passive, no_cloud, capsys):
    """A trusted root whose server lacks a transport isn't saved by a child's file, which may not load."""
    (env["repo"] / ".git").mkdir()
    (env["repo"] / ".codex").mkdir()
    (env["repo"] / ".codex" / "config.toml").write_text('[mcp_servers.x]\nenabled = false\n')
    (env["repo"] / "sub" / ".codex").mkdir(parents=True)
    (env["repo"] / "sub" / ".codex" / "config.toml").write_text('[mcp_servers.x]\ncommand = "x"\n')
    inv = json.loads(run(capsys, "--json", "--repo", str(env["repo"] / "sub"), "--agent", "codex")[1])[
        "agents"]["codex"]["mcp_isolation"]["inventory"]
    assert not inv["complete"] and "exactly one of command or url" in inv["unaccounted"][0]
    (env["repo"] / ".codex" / "config.toml").write_text('[mcp_servers.x]\ncommand = "x"\n')
    inv = json.loads(run(capsys, "--json", "--repo", str(env["repo"] / "sub"), "--agent", "codex")[1])[
        "agents"]["codex"]["mcp_isolation"]["inventory"]
    assert inv["complete"], inv["unaccounted"]


def test_checkout_discovery_errors_are_unaccounted(env, passive, no_cloud, capsys, monkeypatch):
    def denied(start):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(doctor, "_repo_dirs", denied)
    inv = json.loads(run(capsys, "--json", "--repo", str(env["repo"]), "--agent", "codex")[1])[
        "agents"]["codex"]["mcp_isolation"]["inventory"]
    assert not inv["complete"] and "can't be found (PermissionError)" in inv["unaccounted"][0]
