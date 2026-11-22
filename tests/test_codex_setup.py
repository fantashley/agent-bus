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
