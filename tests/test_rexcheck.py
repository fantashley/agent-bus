import json
import os
import subprocess

from agent_bus import rexcheck

BLOCK = "block:1"


def fake_rex(records, fg_pid, inspect_ok=True):
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        assert argv[0] == "rex" and "write" not in argv and "send" not in argv and "send-key" not in argv
        if argv[1:3] == ["block", "inspect"]:
            if not inspect_ok:
                return subprocess.CompletedProcess(argv, 1, "", "Error: block not found")
            return subprocess.CompletedProcess(argv, 0, json.dumps({"block": {"label": "codex"}}), "")
        method = argv[3]
        if method == "program_status":
            return subprocess.CompletedProcess(argv, 0, json.dumps({"records": records}), "")
        if method == "process":
            return subprocess.CompletedProcess(argv, 0, json.dumps({"foreground": {"pid": fg_pid, "name": "codex"}}), "")
        raise AssertionError(argv)

    run.calls = calls
    return run


def rec(app="codex", state="idle", pid=100, id=None):
    r = {"app": app, "state": state, "updated_at": "2026-11-22T03:00:00Z", "owner": {"pid": pid}}
    if id:
        r["id"] = id
    return r


def results(report):
    return {c["check"]: c["result"] for c in report["checks"]}


def test_block_all_good_still_reports_input_unavailable(env):
    r = rexcheck.check(env["bus"], block=BLOCK, agent="codex", run=fake_rex([rec()], 100))
    assert r["ok"]
    assert results(r) == {"block": "pass", "app": "pass", "owner": "pass", "state": "info",
                          "input_ready": "unavailable"}


def test_stale_owner_and_wrong_app_fail(env):
    r = rexcheck.check(env["bus"], block=BLOCK, agent="codex", run=fake_rex([rec()], 999))
    assert not r["ok"] and results(r)["owner"] == "fail"
    r = rexcheck.check(env["bus"], block=BLOCK, agent="claude", run=fake_rex([rec()], 100))
    assert not r["ok"] and results(r)["app"] == "fail"


def test_prefers_client_record_over_subtask_records(env):
    records = [rec(pid=5, id="task", state="working"), rec(pid=100)]
    r = rexcheck.check(env["bus"], block=BLOCK, agent="codex", run=fake_rex(records, 100))
    assert r["ok"] and "idle" in next(c["detail"] for c in r["checks"] if c["check"] == "state")


def test_missing_block_fails_closed(env):
    r = rexcheck.check(env["bus"], block=BLOCK, agent="codex", run=fake_rex([], 1, inspect_ok=False))
    assert not r["ok"] and results(r)["block"] == "fail"


def test_session_without_rex_env_fails_with_codex_hint(env):
    bus = env["bus"]
    sid = bus.register_session("codex", env={})
    r = rexcheck.check(bus, session_id=sid, run=fake_rex([rec()], 100))
    assert not r["ok"] and results(r)["rex_env"] == "fail"
    assert "setup-codex" in r["checks"][0]["detail"]


def test_session_client_must_own_the_status(env, monkeypatch):
    bus = env["bus"]
    sid = bus.register_session("codex", env={"REX_SESSION": "session:1", "REX_BLOCK": BLOCK})
    monkeypatch.setattr(rexcheck, "_parent_pid", lambda pid, run: 100)
    r = rexcheck.check(bus, session_id=sid, run=fake_rex([rec()], 100))
    assert r["ok"] and results(r)["client"] == "pass" and results(r)["mcp_server"] == "pass"
    monkeypatch.setattr(rexcheck, "_parent_pid", lambda pid, run: 42)
    r = rexcheck.check(bus, session_id=sid, run=fake_rex([rec()], 100))
    assert not r["ok"] and results(r)["client"] == "fail"


def test_ended_session_fails(env, monkeypatch):
    bus = env["bus"]
    sid = bus.register_session("codex", env={"REX_SESSION": "s", "REX_BLOCK": BLOCK})
    bus.conn.execute("UPDATE sessions SET pid=? WHERE id=?", (2 ** 22 + 12345, sid))
    r = rexcheck.check(bus, session_id=sid, run=fake_rex([rec()], 100))
    assert not r["ok"] and results(r)["mcp_server"] == "fail"


def test_latest_sessions_one_per_agent(env):
    bus = env["bus"]
    a = bus.register_session("codex", env={})
    bus.register_session("claude", env={})
    b = bus.register_session("codex", env={})
    bus.touch_session(b)
    got = rexcheck.latest_sessions(bus)
    assert b in got and a not in got and len(got) == 2
