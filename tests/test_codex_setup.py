import json
import subprocess
import tomllib

import pytest

from agent_bus import codex_setup as cs

CONFIG = '''model = "x"

[mcp_servers.other]
command = "/bin/true"
env_vars = ["KEEP"]

[mcp_servers.agent-bus]
command = "/old/agent-bus"
args = ["mcp", "--agent", "codex"]

[profiles.p]
model = "y"
'''


def test_add_env_vars_only_touches_agent_bus_table():
    out = cs.add_env_vars(CONFIG)
    d = tomllib.loads(out)
    assert d["mcp_servers"]["agent-bus"]["env_vars"] == cs.ENV_VARS
    assert d["mcp_servers"]["other"]["env_vars"] == ["KEEP"]
    assert d["profiles"]["p"]["model"] == "y"
    # Idempotent: a second pass replaces rather than duplicates the key.
    assert cs.add_env_vars(out) == out


def test_add_env_vars_quoted_header_and_missing_table():
    quoted = CONFIG.replace("[mcp_servers.agent-bus]", '[mcp_servers."agent-bus"]')
    assert tomllib.loads(cs.add_env_vars(quoted))["mcp_servers"]["agent-bus"]["env_vars"] == cs.ENV_VARS
    with pytest.raises(cs.SetupError):
        cs.add_env_vars('model = "x"\n')


def test_add_env_vars_refuses_multiline_value():
    text = CONFIG.replace('args = ["mcp", "--agent", "codex"]\n',
                          'args = ["mcp", "--agent", "codex"]\nenv_vars = [\n  "A",\n]\n')
    with pytest.raises(cs.SetupError):
        cs.add_env_vars(text)


class FakeCodex:
    """Simulates `codex mcp get/add/remove` over a real config.toml, like the CLI:
    `add` rewrites the entry without env_vars."""

    def __init__(self, path):
        self.path = path
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append(argv[1:])
        d = tomllib.loads(self.path.read_text())
        servers = d.get("mcp_servers", {})
        verb = argv[2]
        if verb == "get":
            e = servers.get("agent-bus")
            if not e:
                return subprocess.CompletedProcess(argv, 1, "", "not found")
            t = {"type": "stdio", "command": e["command"], "args": e.get("args", []),
                 "env_vars": e.get("env_vars")}
            return subprocess.CompletedProcess(argv, 0, json.dumps({"name": "agent-bus", "transport": t}), "")
        text = self.path.read_text()
        start = text.find("[mcp_servers.agent-bus]")
        if verb == "remove" and start >= 0:
            nxt = text.find("\n[", start + 1)
            text = text[:start] + (text[nxt + 1:] if nxt >= 0 else "")
        if verb == "add":
            cmd = argv[argv.index("--") + 1:]
            text += f'\n[mcp_servers.agent-bus]\ncommand = "{cmd[0]}"\nargs = {json.dumps(cmd[1:])}\n'
        self.path.write_text(text)
        return subprocess.CompletedProcess(argv, 0, "", "")


@pytest.fixture
def codex_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    p = tmp_path / "config.toml"
    p.write_text(CONFIG)
    return p


def test_ensure_replaces_stale_entry_then_is_a_no_op(codex_home):
    fake = FakeCodex(codex_home)
    assert cs.ensure("/new/agent-bus", codex="codex", run=fake) == "updated"
    d = tomllib.loads(codex_home.read_text())
    assert d["mcp_servers"]["agent-bus"] == {"command": "/new/agent-bus", "args": ["mcp", "--agent", "codex"],
                                             "env_vars": cs.ENV_VARS}
    assert d["mcp_servers"]["other"]["env_vars"] == ["KEEP"]
    fake.calls.clear()
    assert cs.ensure("/new/agent-bus", codex="codex", run=fake) == "unchanged"
    assert fake.calls == [["mcp", "get", "agent-bus", "--json"]]


def test_ensure_only_adds_env_vars_when_command_matches(codex_home):
    fake = FakeCodex(codex_home)
    assert cs.ensure("/old/agent-bus", codex="codex", run=fake) == "updated"
    assert not any(c[1] in ("add", "remove") for c in fake.calls)


def test_ensure_restores_config_when_codex_disagrees(codex_home):
    fake = FakeCodex(codex_home)

    def stubborn(argv, **kw):  # codex that never reports env_vars back
        out = fake(argv, **kw)
        if argv[2] == "get" and out.returncode == 0:
            data = json.loads(out.stdout)
            data["transport"]["env_vars"] = None
            out = subprocess.CompletedProcess(argv, 0, json.dumps(data), "")
        return out

    with pytest.raises(cs.SetupError):
        cs.ensure("/old/agent-bus", codex="codex", run=stubborn)
    assert codex_home.read_text() == CONFIG


def test_add_env_vars_edge_cases():
    # Header at EOF without a trailing newline.
    out = cs.add_env_vars('[mcp_servers.agent-bus]')
    assert tomllib.loads(out)["mcp_servers"]["agent-bus"]["env_vars"] == cs.ENV_VARS
    # A quoted key is replaced, not duplicated.
    text = CONFIG.replace('args = ["mcp", "--agent", "codex"]\n',
                          'args = ["mcp", "--agent", "codex"]\n"env_vars" = ["OLD"]\n')
    assert tomllib.loads(cs.add_env_vars(text))["mcp_servers"]["agent-bus"]["env_vars"] == cs.ENV_VARS
    # A multi-line value with "]" in a comment on its first line is refused.
    text = CONFIG.replace('args = ["mcp", "--agent", "codex"]\n',
                          'args = ["mcp", "--agent", "codex"]\nenv_vars = [ # ]\n  "A",\n]\n')
    with pytest.raises(cs.SetupError):
        cs.add_env_vars(text)
    # Invalid TOML is a SetupError, not a traceback.
    with pytest.raises(cs.SetupError):
        cs.add_env_vars("[mcp_servers.agent-bus\n")


def test_add_env_vars_keeps_env_subtable():
    text = CONFIG.replace("[profiles.p]", '[mcp_servers.agent-bus.env]\nFOO = "1"\n\n[profiles.p]')
    d = tomllib.loads(cs.add_env_vars(text))["mcp_servers"]["agent-bus"]
    assert d["env_vars"] == cs.ENV_VARS and d["env"] == {"FOO": "1"}


def test_ensure_fresh_install(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    p = tmp_path / "config.toml"
    p.write_text("")
    fake = FakeCodex(p)
    assert cs.ensure("/bin/agent-bus", codex="codex", run=fake) == "updated"
    assert tomllib.loads(p.read_text())["mcp_servers"]["agent-bus"]["env_vars"] == cs.ENV_VARS


def test_cli_setup_codex_errors_exit_nonzero(tmp_path, monkeypatch, capsys):
    from agent_bus import cli
    monkeypatch.setattr(cs.shutil, "which", lambda name: None)
    assert cli.main(["setup-codex", "--bin", "/bin/agent-bus"]) == 1
    assert "codex is not on PATH" in capsys.readouterr().err


def test_ensure_keeps_other_forwarded_variables(codex_home):
    codex_home.write_text(CONFIG.replace('args = ["mcp", "--agent", "codex"]\n',
                                         'args = ["mcp", "--agent", "codex"]\nenv_vars = ["AGENT_BUS_DB"]\n'))
    fake = FakeCodex(codex_home)
    assert cs.ensure("/old/agent-bus", codex="codex", run=fake) == "updated"
    got = tomllib.loads(codex_home.read_text())["mcp_servers"]["agent-bus"]["env_vars"]
    assert got == ["AGENT_BUS_DB", *cs.ENV_VARS]
    assert cs.ensure("/old/agent-bus", codex="codex", run=fake) == "unchanged"
    # A stale command is re-added by codex (which drops env_vars); they're carried over.
    assert cs.ensure("/new/agent-bus", codex="codex", run=fake) == "updated"
    got = tomllib.loads(codex_home.read_text())["mcp_servers"]["agent-bus"]
    assert got["command"] == "/new/agent-bus" and got["env_vars"] == ["AGENT_BUS_DB", *cs.ENV_VARS]


def test_ensure_restores_pre_setup_config_after_stale_command_and_failed_check(codex_home):
    fake = FakeCodex(codex_home)

    def stubborn(argv, **kw):
        out = fake(argv, **kw)
        if argv[2] == "get" and out.returncode == 0:
            data = json.loads(out.stdout)
            data["transport"]["env_vars"] = None
            out = subprocess.CompletedProcess(argv, 0, json.dumps(data), "")
        return out

    with pytest.raises(cs.SetupError):
        cs.ensure("/new/agent-bus", codex="codex", run=stubborn)  # remove + add, then fails
    assert codex_home.read_text() == CONFIG


def test_ensure_hooks_creates_updates_in_place_and_removes_cleanly(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    hooks_path = tmp_path / "hooks.json"

    # Disabling when hooks.json doesn't exist is a no-op and doesn't create the file.
    assert cs.ensure_hooks(enable=False) == "unchanged"
    assert not hooks_path.exists()

    # Enabling creates only UserPromptSubmit and Stop entries, and is idempotent.
    assert cs.ensure_hooks("/profile/bin/agent-bus", enable=True) == "updated"
    data = json.loads(hooks_path.read_text())
    assert data == {
        "hooks": {
            "UserPromptSubmit": [{"hooks": [{"type": "command",
                                             "command": "/profile/bin/agent-bus hook --client codex --event prompt",
                                             "timeout": 5}]}],
            "Stop": [{"hooks": [{"type": "command",
                                 "command": "/profile/bin/agent-bus hook --client codex --event stop",
                                 "timeout": 5}]}],
        }
    }
    assert cs.ensure_hooks("/profile/bin/agent-bus", enable=True) == "unchanged"

    # Disabling removes agent-bus's entries cleanly.
    assert cs.ensure_hooks(enable=False) == "updated"
    assert json.loads(hooks_path.read_text()) == {"hooks": {}}
    assert cs.ensure_hooks(enable=False) == "unchanged"


def test_ensure_hooks_preserves_unrelated_hooks_and_indices(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    hooks_path = tmp_path / "hooks.json"
    existing = {
        "description": "user hooks",
        "hooks": {
            "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "/usr/bin/my-hook", "timeout": 10}]}],
            "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "/usr/bin/audit"}]}],
        },
    }
    hooks_path.write_text(json.dumps(existing))

    # Appends after user's existing UserPromptSubmit hook so index 0 stays untouched.
    assert cs.ensure_hooks("/old/agent-bus", enable=True) == "updated"
    d = json.loads(hooks_path.read_text())
    assert d["description"] == "user hooks"
    assert d["hooks"]["PreToolUse"] == existing["hooks"]["PreToolUse"]
    assert d["hooks"]["UserPromptSubmit"][0] == existing["hooks"]["UserPromptSubmit"][0]
    assert d["hooks"]["UserPromptSubmit"][1]["hooks"][0]["command"].startswith("/old/agent-bus ")

    # Updating bin path replaces in place at index 1 (and handles spaces in path).
    assert cs.ensure_hooks("/my profile/bin/agent-bus", enable=True) == "updated"
    d = json.loads(hooks_path.read_text())
    assert len(d["hooks"]["UserPromptSubmit"]) == 2
    assert d["hooks"]["UserPromptSubmit"][0] == existing["hooks"]["UserPromptSubmit"][0]
    assert d["hooks"]["UserPromptSubmit"][1]["hooks"][0]["command"] == (
        "'/my profile/bin/agent-bus' hook --client codex --event prompt"
    )
    assert cs.ensure_hooks("/my profile/bin/agent-bus", enable=True) == "unchanged"

    # Disabling removes only agent-bus entries and leaves user hooks intact.
    assert cs.ensure_hooks(enable=False) == "updated"
    assert json.loads(hooks_path.read_text()) == existing


@pytest.mark.parametrize("bad", ["not json", "[]", '{"hooks": []}', '{"hooks": {"Stop": {}}}'])
def test_ensure_hooks_refuses_malformed_file(tmp_path, monkeypatch, bad):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    hooks_path = tmp_path / "hooks.json"
    hooks_path.write_text(bad)
    with pytest.raises(cs.SetupError):
        cs.ensure_hooks("/profile/bin/agent-bus", enable=True)
    assert hooks_path.read_text() == bad


def test_cli_setup_codex_hooks_flags(codex_home, monkeypatch, capsys):
    from agent_bus import cli
    fake = FakeCodex(codex_home)
    orig = cs.ensure
    monkeypatch.setattr(cs, "ensure", lambda exe: orig(exe, codex="codex", run=fake))
    assert cli.main(["setup-codex", "--bin", "/store/agent-bus", "--hooks",
                     "--hooks-bin", "/profile/bin/agent-bus"]) == 0
    out = capsys.readouterr().out
    assert "agent-bus hooks updated" in out and "/hooks" in out
    hooks_path = codex_home.parent / "hooks.json"
    assert "/profile/bin/agent-bus hook --client codex --event prompt" in hooks_path.read_text()
    assert cli.main(["setup-codex", "--bin", "/store/agent-bus", "--no-hooks"]) == 0
    assert "agent-bus hooks updated" in capsys.readouterr().out
    assert json.loads(hooks_path.read_text()) == {"hooks": {}}
    # --hooks-only updates hooks.json without touching config.toml or needing codex on PATH.
    assert cli.main(["setup-codex", "--hooks-only", "--hooks", "--hooks-bin", "/profile/bin/agent-bus"]) == 0
    assert "agent-bus hooks updated" in capsys.readouterr().out
    assert cli.main(["setup-codex", "--hooks-only", "--no-hooks"]) == 0
    assert "agent-bus hooks updated" in capsys.readouterr().out
    assert json.loads(hooks_path.read_text()) == {"hooks": {}}
    with pytest.raises(SystemExit) as e:
        cli.main(["setup-codex", "--bin", "/store/agent-bus", "--hooks-bin", "/profile/bin/agent-bus"])
    assert e.value.code == 2
    with pytest.raises(SystemExit) as e:
        cli.main(["setup-codex", "--hooks-only"])
    assert e.value.code == 2


def test_ensure_hooks_preserves_indices_when_unrelated_hooks_follow(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    hooks_path = tmp_path / "hooks.json"
    other = {"hooks": [{"type": "command", "command": "/usr/bin/other", "timeout": 10}]}
    # Agent-bus is at index 0, followed by an unrelated user hook at index 1 (and a duplicate at index 1 -> 2).
    initial = {
        "hooks": {
            "UserPromptSubmit": [
                {"hooks": [{"type": "command", "command": "/profile/bin/agent-bus hook --client codex --event prompt",
                            "timeout": 15}]},
                {"hooks": [{"type": "command", "command": "/old/agent-bus hook --client codex --event prompt",
                            "timeout": 5}]},
                other,
            ],
        }
    }
    hooks_path.write_text(json.dumps(initial))

    # Deduplicating on enable keeps index 0 (and its custom timeout), leaves a placeholder at index 1,
    # and keeps `other` at index 2 so its Codex trust key doesn't shift.
    assert cs.ensure_hooks("/profile/bin/agent-bus", enable=True) == "updated"
    d = json.loads(hooks_path.read_text())
    assert len(d["hooks"]["UserPromptSubmit"]) == 3
    assert d["hooks"]["UserPromptSubmit"][0]["hooks"][0]["timeout"] == 15
    assert d["hooks"]["UserPromptSubmit"][1] == {"hooks": []}
    assert d["hooks"]["UserPromptSubmit"][2] == other
    assert cs.ensure_hooks("/profile/bin/agent-bus", enable=True) == "unchanged"

    # Disabling leaves a placeholder at index 0 so `other` stays at index 2.
    assert cs.ensure_hooks(enable=False) == "updated"
    d = json.loads(hooks_path.read_text())
    assert d["hooks"]["UserPromptSubmit"] == [{"hooks": []}, {"hooks": []}, other]

    # Re-enabling reuses slot 0 and keeps `other` at index 2.
    assert cs.ensure_hooks("/profile/bin/agent-bus", enable=True) == "updated"
    d = json.loads(hooks_path.read_text())
    assert d["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"].startswith("/profile/bin/agent-bus ")
    assert d["hooks"]["UserPromptSubmit"][2] == other


def test_ensure_hooks_refuses_to_shift_handler_in_shared_group(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    hooks_path = tmp_path / "hooks.json"
    hooks_path.write_text(json.dumps({
        "hooks": {
            "Stop": [{"hooks": [
                {"type": "command", "command": "/profile/bin/agent-bus hook --client codex --event stop", "timeout": 5},
                {"type": "command", "command": "/usr/bin/after", "timeout": 5},
            ]}]
        }
    }))
    with pytest.raises(cs.SetupError, match="precedes an unrelated hook"):
        cs.ensure_hooks(enable=False)


def test_ensure_hooks_supports_custom_executable_names(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    hooks_path = tmp_path / "hooks.json"
    custom = "/profile/bin/agent-bus-custom"
    assert cs.ensure_hooks(custom, enable=True) == "updated"
    assert cs.ensure_hooks(custom, enable=True) == "unchanged"
    d = json.loads(hooks_path.read_text())
    assert len(d["hooks"]["UserPromptSubmit"]) == 1
    assert d["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"] == f"{custom} hook --client codex --event prompt"
    assert cs.ensure_hooks(enable=False) == "updated"
    assert json.loads(hooks_path.read_text()) == {"hooks": {}}
