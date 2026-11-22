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
        assert argv[1] == "--autostart=false"
        argv = argv[argv.index("block") - 1:]  # drop global flags
        if argv[1:3] == ["block", "inspect"]:
            if not inspect_ok:
                return subprocess.CompletedProcess(argv, 1, "", "Error: block not found")
            return subprocess.CompletedProcess(argv, 0, json.dumps({"block": {"block_id": BLOCK, "label": "codex"}}), "")
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
    # A sub-task record never stands in for the client's own status.
    r = rexcheck.check(env["bus"], block=BLOCK, agent="codex", run=fake_rex([rec(id="task")], 100))
    assert not r["ok"] and results(r)["app"] == "fail"


def test_newest_record_by_parsed_time(env):
    older = {**rec(state="working"), "updated_at": "2026-11-22T03:00:00Z"}
    newer = {**rec(state="idle"), "updated_at": "2026-11-22T03:00:00.500Z"}  # sorts lower as a string
    r = rexcheck.check(env["bus"], block=BLOCK, agent="codex", run=fake_rex([older, newer], 100))
    assert next(c["detail"] for c in r["checks"] if c["check"] == "state").startswith("idle")


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
    monkeypatch.setattr(rexcheck, "_ancestors", lambda pid: [7, 100])  # client starts servers via a helper
    r = rexcheck.check(bus, session_id=sid, run=fake_rex([rec()], 100))
    assert r["ok"] and results(r)["client"] == "pass" and results(r)["mcp_server"] == "pass"
    monkeypatch.setattr(rexcheck, "_ancestors", lambda pid: [42])
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


def test_malformed_rex_output_fails_closed(env):
    def run(argv, **kw):
        if "inspect" in argv:
            return subprocess.CompletedProcess(argv, 1, "", "\n")  # whitespace-only stderr
        raise AssertionError(argv)
    r = rexcheck.check(env["bus"], block=BLOCK, agent="codex", run=run)
    assert not r["ok"] and results(r)["block"] == "fail" and results(r)["input_ready"] == "unavailable"

    def shapes(argv, **kw):
        if "inspect" in argv:
            return subprocess.CompletedProcess(argv, 0, json.dumps({"block": {"block_id": BLOCK}}), "")
        if "program_status" in argv:
            return subprocess.CompletedProcess(argv, 0, json.dumps({"records": "nope"}), "")
        return subprocess.CompletedProcess(argv, 0, "[]", "")
    r = rexcheck.check(env["bus"], block=BLOCK, agent="codex", run=shapes)
    assert not r["ok"] and results(r)["app"] == "fail"

    odd = {**rec(), "owner": "x", "state": None}
    r = rexcheck.check(env["bus"], block=BLOCK, agent="codex", run=fake_rex([odd], 100))
    assert not r["ok"] and results(r)["owner"] == "fail" and results(r)["state"] == "fail"


def test_input_ready_reported_even_on_early_failure(env):
    bus = env["bus"]
    sid = bus.register_session("codex", env={})
    r = rexcheck.check(bus, session_id=sid, run=fake_rex([], 1))
    assert r["checks"][-1] == {"check": "input_ready", "result": "unavailable", "detail": rexcheck.INPUT_READY}


def test_session_agent_mismatch_is_an_error(env):
    import pytest
    sid = env["bus"].register_session("codex", env={})
    with pytest.raises(ValueError):
        rexcheck.check(env["bus"], session_id=sid, agent="claude")


def test_zombie_is_not_alive(monkeypatch):
    monkeypatch.setattr(rexcheck, "_proc", lambda pid: (1, "Z"))
    assert not rexcheck._alive(os.getpid())
    monkeypatch.setattr(rexcheck, "_proc", lambda pid: (1, "S"))
    assert rexcheck._alive(os.getpid())


def test_latest_sessions_prefers_live(env):
    bus = env["bus"]
    live = bus.register_session("codex", env={})
    dead = bus.register_session("codex", env={})
    bus.conn.execute("UPDATE sessions SET pid=? WHERE id=?", (2 ** 22 + 12345, dead))
    bus.touch_session(dead)
    assert rexcheck.latest_sessions(bus) == [live]
    assert rexcheck.latest_sessions(bus, "claude") == []


def test_cli_exit_codes_and_json(env, monkeypatch, capsys):
    from agent_bus import cli
    assert cli.main(["rex-check"]) == 1  # no sessions
    assert "no sessions" in capsys.readouterr().out
    monkeypatch.setattr(rexcheck.subprocess, "run", fake_rex([rec()], 100))
    assert cli.main(["rex-check", "--block", BLOCK, "--agent", "codex", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out[0]["ok"] and out[0]["checks"][-1]["check"] == "input_ready"
    assert cli.main(["rex-check", "--block", BLOCK]) == 2  # --block needs --agent


def test_malformed_block_inspect_fails_even_with_good_status(env):
    good = fake_rex([rec()], 100)
    for bad in ({}, {"block": "weird"}, {"block": {"label": "x"}}):
        def run(argv, bad=bad, **kw):
            if "inspect" in argv:
                return subprocess.CompletedProcess(argv, 0, json.dumps(bad), "")
            return good(argv, **kw)
        r = rexcheck.check(env["bus"], block=BLOCK, agent="codex", run=run)
        assert not r["ok"] and results(r)["block"] == "fail", bad


def test_session_server_is_used_and_never_autostarted(env):
    bus = env["bus"]
    sid = bus.register_session("codex", env={"REX_SESSION": "s", "REX_BLOCK": BLOCK, "REX_SERVER": "nuc"})
    run = fake_rex([rec()], 100)
    rexcheck.check(bus, session_id=sid, run=run)
    assert run.calls and all(c[1:4] == ["--autostart=false", "-S", "nuc"] for c in run.calls)
    run = fake_rex([rec()], 100)
    rexcheck.check(bus, block=BLOCK, agent="codex", run=run)
    assert all("-S" not in c for c in run.calls)
