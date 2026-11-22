"""Issue #6: Codex's shared app-server daemon hands MCP servers its own Rex location."""
import subprocess

import pytest

from agent_bus import procinfo, rex, rexcheck
from agent_bus.db import HUMAN
from agent_bus.service import Service

DAEMON = ["/home/u/.codex/packages/app-server-daemon/releases/0.171.0/bin/codex", "app-server",
          "--listen", "unix://", "--analytics-default-enabled", "--managed-daemon"]


@pytest.mark.parametrize("args,expected", [
    (DAEMON, True),
    (["/nix/store/x-codex/bin/.codex-wrapped", "app-server", "--managed-daemon"], True),
    (["codex", "app-server", "--listen", "ws://127.0.0.1:9"], True),
    (["codex", "app-server", "--listen=unix:///tmp/s"], True),
    (["codex", "app-server", "--listen", "stdio://"], False),
    (["codex", "app-server"], False),                    # a single-client stdio server
    (["codex", "--no-daemon", "--sandbox", "read-only"], False),
    (["codex", "exec", "--json", "-"], False),
    (["python", "codex-app-server", "--managed-daemon"], False),  # substring isn't enough
    (["/usr/bin/node", "app-server", "--managed-daemon"], False),
    ([], False),
])
def test_daemon_argv_matching(args, expected):
    assert procinfo.is_shared_codex_daemon(args) is expected


def test_daemon_found_through_helpers_and_unreadable_argv_is_not_evidence():
    tree = {10: ["sh", "-c", "agent-bus mcp"], 20: DAEMON, 30: ["systemd", "--user"]}
    assert procinfo.shared_codex_daemon(1, lambda pid: [10, 20, 30], tree.get) == 20
    assert procinfo.shared_codex_daemon(1, lambda pid: [10, 30], tree.get) is None
    assert procinfo.shared_codex_daemon(1, lambda pid: [99], lambda pid: None) is None


def test_ancestors_bounded_and_cycle_safe(monkeypatch):
    parents = {5: 6, 6: 7, 7: 5}
    monkeypatch.setattr(procinfo, "proc", lambda pid: (parents[pid], "S"))
    assert procinfo.ancestors(5) == [6, 7]
    monkeypatch.setattr(procinfo, "proc", lambda pid: (pid + 1, "S"))
    assert len(procinfo.ancestors(100, limit=4)) == 4


def test_registration_under_daemon_records_no_location(env):
    bus = env["bus"]
    loc = {"REX_SESSION": "session:old", "REX_BLOCK": "block:old", "REX_SERVER": "x"}
    sid = bus.register_session("codex", env=loc, rex_reason=procinfo.CODEX_SHARED_DAEMON)
    s = bus.session(sid)
    assert (s["rex_session"], s["rex_block"], s["rex_server"]) == (None, None, None)
    assert s["rex_reason"] == "codex_shared_daemon"


def test_migration_adds_rex_reason(env):
    bus = env["bus"]
    bus.conn.execute("ALTER TABLE sessions DROP COLUMN rex_reason")
    bus._migrate()
    assert "rex_reason" in {r["name"] for r in bus._all("PRAGMA table_info(sessions)")}


def test_mcp_server_detects_daemon_even_with_stale_env(env, monkeypatch):
    from agent_bus import mcp_server
    monkeypatch.setenv("REX_SESSION", "session:old")
    monkeypatch.setenv("REX_BLOCK", "block:old")
    monkeypatch.setattr(procinfo, "shared_codex_daemon", lambda pid: 4242)
    mcp_server.build("codex", bus=env["bus"])
    mcp_server.build("claude", bus=env["bus"])  # only Codex sessions are checked
    rows = {r["agent_type"]: dict(r) for r in env["bus"]._all("SELECT * FROM sessions")}
    assert rows["codex"]["rex_block"] is None and rows["codex"]["rex_reason"] == "codex_shared_daemon"
    assert rows["claude"]["rex_block"] == "block:old" and rows["claude"]["rex_reason"] is None


def no_rex(*a, **kw):
    raise AssertionError("rex must not be called")


def test_rex_check_reports_stored_reason(env):
    bus = env["bus"]
    sid = bus.register_session("codex", env={}, rex_reason=procinfo.CODEX_SHARED_DAEMON)
    r = rexcheck.check(bus, session_id=sid, run=no_rex)
    first = r["checks"][0]
    assert not r["ok"] and first["check"] == "rex_env" and "codex --no-daemon" in first["detail"]


def test_rex_check_diagnoses_rows_from_before_the_reason_existed(env, monkeypatch):
    bus = env["bus"]
    sid = bus.register_session("codex", env={"REX_SESSION": "session:old", "REX_BLOCK": "block:old"})
    monkeypatch.setattr(rexcheck.procinfo, "shared_codex_daemon", lambda pid, ancestors_of: 4242)

    def gone(argv, **kw):
        return subprocess.CompletedProcess(argv, 1, "", "Error: block target not found")

    r = rexcheck.check(bus, session_id=sid, run=gone)
    checks = {c["check"]: c for c in r["checks"]}
    assert not r["ok"] and "codex --no-daemon" in checks["rex_env"]["detail"]
    assert [c["check"] for c in r["checks"]].count("rex_env") == 1


def test_viewers_refused_for_daemon_session_despite_stale_env(env, monkeypatch, no_spawn):
    monkeypatch.setenv("REX_SESSION", "session:old")
    monkeypatch.setenv("REX_BLOCK", "block:old")
    monkeypatch.setattr(rex.shutil, "which", lambda name: "/bin/rex")
    monkeypatch.setattr(rex.subprocess, "run", no_rex)
    bus = env["bus"]
    sid = bus.register_session("codex", env={}, rex_reason=procinfo.CODEX_SHARED_DAEMON)
    res = Service(bus, "codex", sid).start_discussion("Q?", peers=["claude"], watch=True)
    assert res["watch"]["opened"] == [] and "--no-daemon" in res["watch"]["note"]


def test_viewers_anchor_on_recorded_block_not_env(env, monkeypatch, no_spawn):
    monkeypatch.setenv("REX_SESSION", "session:env")
    monkeypatch.setenv("REX_BLOCK", "block:env")
    monkeypatch.setattr(rex.shutil, "which", lambda name: "/bin/rex")
    calls = []
    monkeypatch.setattr(rex.subprocess, "run",
                        lambda argv, **kw: calls.append(argv) or subprocess.CompletedProcess(argv, 0, "{}", ""))
    bus = env["bus"]
    sid = bus.register_session("codex", env={"REX_SESSION": "session:mine", "REX_BLOCK": "block:mine"})
    Service(bus, "codex", sid).start_discussion("Q?", peers=["claude"], watch=True)
    assert calls and calls[0][calls[0].index("-b") + 1] == "block:mine"
    # The human's CLI has no session row and keeps using its own environment.
    calls.clear()
    Service(bus, HUMAN).start_discussion("Q?", peers=["claude"], watch=True)
    assert calls[0][calls[0].index("-b") + 1] == "block:env"
