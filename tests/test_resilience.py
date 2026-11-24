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
    assert starts and all("codex-cli 1.0.0 · recipe codex/3" in s for s in starts)


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


def test_whoami_flags_a_server_older_than_path(env, no_spawn, monkeypatch, tmp_path):
    """A server started before an upgrade runs (and spawns workers from) another build (#81)."""
    import asyncio
    import sys
    from agent_bus import mcp_server

    def install(name):
        exe = tmp_path / name / "bin" / "agent-bus"
        exe.parent.mkdir(parents=True)
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)
        return exe

    old, new = install("old"), install("new")
    profile = tmp_path / "profile" / "bin"
    profile.mkdir(parents=True)
    link = profile / "agent-bus"
    link.symlink_to(old)
    monkeypatch.setenv("PATH", f"{profile}:{os.environ['PATH']}")
    server = mcp_server.build("claude", bus=env["bus"])

    def whoami():
        return json.loads(asyncio.run(server.call_tool("whoami", {})).content[0].text)

    def start(argv0):
        monkeypatch.setattr(sys, "argv", [str(argv0), "mcp"])
        monkeypatch.setattr(cfg, "_LAUNCHED_FROM", cfg._launched_from())

    # Started through the profile link at the current build: not stale.
    start(link)
    assert "stale_build" not in whoami()
    # An upgrade moves the link; this server still runs, and spawns workers from, the old build.
    link.unlink()
    link.symlink_to(new)
    stale = whoami()["stale_build"]
    assert stale["running"] == os.path.realpath(old) and stale["on_path"] == os.path.realpath(new)
    assert "/reload-plugins" in stale["hint"]
    monkeypatch.delenv("AGENT_BUS_BIN", raising=False)
    assert cfg.self_command() == [os.path.realpath(old)]
    # Once the old build is gone, workers start from the agent-bus on PATH instead.
    old.unlink()
    assert cfg.self_command() == [str(link)]
    # Not an installed agent-bus script (python -m, tests): nothing to compare.
    start("pytest")
    assert "stale_build" not in whoami()


def test_whoami_flags_a_stale_nix_wrapped_server(env, no_spawn, monkeypatch, tmp_path):
    """#89: under Nix's wrapProgram, argv[0] is bin/.agent-bus-wrapped and the wrapper puts
    the build's own bin directory first on PATH.  Neither may hide an upgrade."""
    import asyncio
    import sys
    from agent_bus import mcp_server

    def install(name):
        bin_dir = tmp_path / name / "bin"
        bin_dir.mkdir(parents=True)
        for exe in ("agent-bus", ".agent-bus-wrapped"):
            (bin_dir / exe).write_text("#!/bin/sh\n")
            (bin_dir / exe).chmod(0o755)
        return bin_dir

    old, new = install("old"), install("new")
    profile = tmp_path / "profile" / "bin"
    profile.mkdir(parents=True)
    link = profile / "agent-bus"
    link.symlink_to(old / "agent-bus")
    monkeypatch.setenv("PATH", f"{old}:{profile}:{os.environ['PATH']}")  # as the old wrapper left it
    monkeypatch.setattr(sys, "argv", [str(old / ".agent-bus-wrapped"), "mcp"])
    monkeypatch.setattr(cfg, "_LAUNCHED_FROM", cfg._launched_from())
    assert cfg._LAUNCHED_FROM == os.path.realpath(old / "agent-bus")
    server = mcp_server.build("claude", bus=env["bus"])

    def whoami():
        return json.loads(asyncio.run(server.call_tool("whoami", {})).content[0].text)

    assert "stale_build" not in whoami()
    link.unlink()
    link.symlink_to(new / "agent-bus")
    stale = whoami()["stale_build"]
    assert stale["running"] == os.path.realpath(old / "agent-bus")
    assert stale["on_path"] == os.path.realpath(new / "agent-bus")
    monkeypatch.delenv("AGENT_BUS_BIN", raising=False)
    assert cfg.self_command() == [os.path.realpath(old / "agent-bus")]  # the wrapper, not the wrapped

    # An unwrapped launch takes PATH as it is: its own directory first is not stale.
    monkeypatch.setenv("PATH", f"{old}:{new}:{os.environ['PATH']}")
    monkeypatch.setattr(sys, "argv", [str(old / "agent-bus"), "mcp"])
    monkeypatch.setattr(cfg, "_LAUNCHED_FROM", cfg._launched_from())
    assert "stale_build" not in whoami()


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


# --------------------- agy's headless denials are incomplete turns, not drift (t-606fcd)


def agy_run(env, monkeypatch, behaviour, seen=None):
    monkeypatch.setenv("FAKE_BEHAVIOUR", behaviour)
    return adapter("antigravity", cfg.Config.load()).run(
        "hi", str(env["repo"]), 30, (lambda k, t: seen.append((k, t))) if seen is not None else (lambda k, t: None))


def test_agy_denied_command_is_an_incomplete_turn_not_drift(env, monkeypatch):
    seen = []
    res = agy_run(env, monkeypatch, "denied", seen)
    assert not res.ok and not res.format_changed and res.incomplete
    assert res.error.startswith("PEER_TURN_INCOMPLETE: antigravity")
    assert "auto-denied 1 action(s)" in res.error and "give: command;" in res.error and "Bash" not in res.error
    assert "failed tool(s): run_command" in res.error
    assert "jj log" not in res.error  # names only, never arguments
    assert res.external_id and res.usage == {"input": 12732, "output": 652, "cache_read": 0, "thinking": 366}
    assert not [t for k, t in seen if k == "format"]  # every event was understood


def test_agy_tool_left_without_an_end_state_is_incomplete(env, monkeypatch):
    res = agy_run(env, monkeypatch, "unfinished")
    assert res.incomplete and not res.format_changed
    assert "unfinished: run_command" in res.error and "denied" not in res.error


def test_agy_unexplained_missing_reply_is_still_drift(env, monkeypatch):
    from agent_bus.peers import StreamState
    for behaviour in ("silent", "toolerror"):  # no tools at all; a tool ERROR with no denial
        res = agy_run(env, monkeypatch, behaviour)
        assert res.format_changed and not res.incomplete and res.error.startswith("PEER_FORMAT_CHANGED"), behaviour
    # A tool that looks unfinished among events we don't understand may just be unparsed.
    ad = adapter("antigravity", cfg.Config.load())
    st = StreamState(pending={2: "run_command"}, unknown={"brand_new_event": 1}, final="")
    assert ad.incomplete(st) is None
    # Without a recognised result carrying an explicitly blank response, nothing explains it:
    # a missing result, or a response field whose type changed, may hide a new schema.
    for final in (None, "answer"):
        assert ad.incomplete(StreamState(pending={2: "run_command"}, final=final)) is None
        assert ad.incomplete(StreamState(denied=["command"], final=final)) is None
    assert ad.incomplete(StreamState(pending={2: "run_command"}, final=" \n"))


def test_agy_denial_notice_carries_plain_names_only(env):
    from agent_bus.peers import StreamState
    ad = adapter("antigravity", cfg.Config.load())
    st = StreamState()
    ad.parse_line({"event": "step_update", "step_update": {
        "step_type": "tool", "step_index": 1, "state": "ERROR", "tool_name": "run_command --rm"}}, st, lambda k, t: None)
    ad.parse_line({"event": "result", "result": {"status": "SUCCESS", "response": "", "denied_actions": [
        {"action": {"cmd": "rm -rf secret"}, "display_name": "Bash"},  # not a name: dropped
        {"action": "command", "display_name": "db-prod.key"},          # free-form label: never shown
        {"action": "rm -rf secret"},                                   # not a plain name: "?"
    ]}}, st, lambda k, t: None)
    why = ad.incomplete(st)
    assert "rm" not in why and "secret" not in why and "db-prod" not in why
    assert "auto-denied 2 action(s)" in why and "give: command, ?;" in why
    assert "failed tool(s): ?" in why


def test_agy_tool_state_tracking_tolerates_partial_frames(env):
    from agent_bus.peers import StreamState
    ad = adapter("antigravity", cfg.Config.load())
    st = StreamState()
    step = lambda **kw: ad.parse_line({"event": "step_update", "step_update": {  # noqa: E731
        "step_type": "tool", "step_index": 2, **kw}}, st, lambda k, t: None)
    step(state="ACTIVE", tool_name="run_command")
    step()  # a frame with no state leaves the tool pending
    assert st.pending == {2: "run_command"}
    step(state="ERROR")  # the end frame omits the name: the ACTIVE frame's name is kept
    assert st.pending == {} and st.failed_tools == {"run_command"}


def test_agy_whitespace_only_denial_reaches_no_repair(env, no_spawn, monkeypatch):
    bus = env["bus"]
    res = agy_run(env, monkeypatch, "denied_blank")
    assert res.incomplete and res.text == ""
    tid = Service(bus, HUMAN).start_discussion("q", peers=["codex", "antigravity"], max_rounds=2)["thread"]["id"]
    run_job(bus, tid)
    msgs = bus.history(tid)["messages"]
    assert not [m for m in msgs if m["sender"] == "antigravity"]  # no repaired proposal or vote
    assert [m for m in msgs if m["sender"] == SYSTEM
            and m["body"].startswith("antigravity could not take its turn: PEER_TURN_INCOMPLETE")]
    agy = calls(env, "antigravity")
    assert agy and not any("did not end with the JSON object" in c["prompt"] for c in agy)


def test_preamble_offers_web_search_unless_it_is_off(env, no_spawn):
    t = {"id": "t-x", "participants": ["codex", "antigravity"], "initiator": HUMAN, "repo": "/r"}
    w = Worker.__new__(Worker)
    w.config = cfg.Config.load()
    for agent in cfg.AGENTS:  # every adapter can search headless now (issue #46)
        assert "You may read files in /r and search the web if that helps you answer." in w.preamble(agent, t)
    w.config.peers["antigravity"]["web_search"] = False
    agy = w.preamble("antigravity", t)
    assert "search the web" not in agy and "You may read files in /r if that helps you answer." in agy


def test_agy_reply_after_a_denial_is_a_normal_turn(env, monkeypatch):
    res = agy_run(env, monkeypatch, "denied_reply")
    assert res.ok and not res.incomplete and not res.format_changed and res.error is None
    assert '"action"' in res.text  # it answered from context after the denial


def test_agy_headless_guidance_on_every_turn(env, no_spawn, monkeypatch):
    from agent_bus.prompts import AGY_HEADLESS
    ad = adapter("antigravity", cfg.Config.load())
    assert ad.recipe == "4" and "run_command" in AGY_HEADLESS and "search_web" in AGY_HEADLESS
    assert "do not use search_web" not in AGY_HEADLESS.lower()  # no contradiction with the preamble
    for resume_id in (None, "conv-1"):
        argv = ad.invocation("PROMPT-BODY", resume_id, str(env["repo"]), []).argv
        assert argv[-1] == AGY_HEADLESS + "PROMPT-BODY"
        assert argv[argv.index("--mode") + 1] == "plan" and "--sandbox" in argv  # unchanged isolation
    # Long prompts still go through a file, and both the file and the pointer carry the guidance.
    scratch = env["tmp"] / "scratch"
    scratch.mkdir()
    big = "x" * 200_000
    argv = ad.invocation(big, "conv-1", str(env["repo"]), [str(scratch)]).argv
    assert argv[-1].startswith(AGY_HEADLESS) and "read_file" in argv[-1] and big not in argv[-1]
    (written,) = scratch.glob("prompt-*.md")
    assert written.read_text() == AGY_HEADLESS + big

    # Through the worker: first and resumed turns are guided and recorded as recipe 4; other peers aren't.
    monkeypatch.setenv("FAKE_BEHAVIOUR", "stubborn")  # every peer takes a round-2 turn
    tid = Service(env["bus"], HUMAN).start_discussion("q", peers=["codex", "antigravity"], max_rounds=2)["thread"]["id"]
    run_job(env["bus"], tid)
    agy = calls(env, "antigravity")
    assert len(agy) >= 2 and "--conversation" in agy[-1]["argv"]
    assert all(c["prompt"].startswith(AGY_HEADLESS) for c in agy)
    assert not any(AGY_HEADLESS.strip() in c["prompt"] for c in calls(env, "codex"))
    starts = [e["text"] for e in env["bus"]._all(
        "SELECT * FROM events WHERE thread_id=? AND kind='turn_start' AND agent='antigravity'", tid)]
    assert starts and all("recipe antigravity/4" in s for s in starts)


def test_incomplete_agy_discussion_turn_is_reported_without_a_drift_notice(env, no_spawn, monkeypatch):
    bus = env["bus"]
    monkeypatch.setenv("FAKE_BEHAVIOUR", "denied")  # codex replies normally
    tid = Service(bus, HUMAN).start_discussion("q", peers=["codex", "antigravity"], max_rounds=2)["thread"]["id"]
    run_job(bus, tid)
    msgs = bus.history(tid)["messages"]
    system = [m["body"] for m in msgs if m["sender"] == SYSTEM]
    assert not [b for b in system if "PEER_FORMAT_CHANGED" in b]
    assert [b for b in system if b.startswith("antigravity could not take its turn: PEER_TURN_INCOMPLETE")]
    assert not [m for m in msgs if m["sender"] == "antigravity"]  # nothing made to look like a reply
    assert bus.peer_session(tid, "antigravity") is None  # a first turn that stopped early isn't pinned


def test_incomplete_agy_review_keeps_its_session_without_a_fresh_retry(env, no_spawn, monkeypatch):
    bus = env["bus"]
    monkeypatch.setattr("agent_bus.worker.Worker.review_diff", lambda self, refs, interdiff: ("diff", "Diff"))
    me = Service(bus, "claude", bus.register_session("claude", env={}))
    tid = me.request_review("antigravity", str(env["repo"]), BASE, HEAD)["thread"]["id"]
    run_job(bus, tid)  # a normal first pass pins a session
    sid = bus.peer_session(tid, "antigravity")
    assert sid
    monkeypatch.setenv("FAKE_BEHAVIOUR", "denied")
    me.request_review("antigravity", str(env["repo"]), BASE, "c" * 40, prev_head_commit_id=HEAD)
    before = len(calls(env, "antigravity"))
    run_job(bus, tid)
    assert bus.thread(tid)["status"] == "needs_human"
    assert len(calls(env, "antigravity")) == before + 1  # a fresh session would stop the same way
    assert bus.peer_session(tid, "antigravity") == sid
    bodies = [m["body"] for m in bus.history(tid)["messages"] if m["sender"] == SYSTEM]
    assert any("PEER_TURN_INCOMPLETE" in b for b in bodies)
    assert not any("PEER_FORMAT_CHANGED" in b for b in bodies)


def test_incomplete_agy_repair_reports_the_denial(env, no_spawn, monkeypatch):
    bus = env["bus"]
    monkeypatch.setenv("FAKE_BEHAVIOUR", "nojson")
    monkeypatch.setenv("FAKE_AGY_DENIED_REPAIR", "1")
    monkeypatch.setattr("agent_bus.worker.Worker.review_diff", lambda self, refs, interdiff: ("diff", "Diff"))
    me = Service(bus, "claude", bus.register_session("claude", env={}))
    tid = me.request_review("antigravity", str(env["repo"]), BASE, HEAD)["thread"]["id"]
    run_job(bus, tid)
    msgs = bus.history(tid)["messages"]
    assert bus.thread(tid)["status"] == "needs_human"
    assert any("PEER_TURN_INCOMPLETE" in m["body"] for m in msgs if m["sender"] == SYSTEM)
    assert not any(m["kind"] == "review_verdict" for m in msgs)


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



def test_version_probe_survives_undecodable_output(env):
    """Codex PR #37 final pass: a wrapper that writes a byte the locale can't decode
    must not crash the probe (and with it the turn)."""
    import stat
    import sys
    script = env["tmp"] / "noisy-cli"
    script.write_text(f"#!{sys.executable}\nimport sys\n"
                      "sys.stderr.buffer.write(b'warn \\xff\\xfe\\n')\n"
                      "sys.stdout.buffer.write(b'noisy 1.2.3 \\xff\\n')\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    version = cli_version(env["bus"], "codex", str(script), cwd=str(env["tmp"]))
    assert version.startswith("noisy 1.2.3")


# ------------------------------------------------- Antigravity review of PR #37 (t-c0e2b7)


def test_whitespace_stdout_falls_back_to_stderr_for_the_version(env):
    import stat
    import sys
    script = env["tmp"] / "stderr-cli"
    script.write_text(f"#!{sys.executable}\nimport sys\nprint()\nprint('tool 4.5.6', file=sys.stderr)\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    assert cli_version(env["bus"], "codex", str(script), cwd=str(env["tmp"])) == "tool 4.5.6"


# ------------------------------------------------- version probe timeout cooldown (#41)


def _slow_cli(env, name="slow-cli"):
    """A CLI whose ``--version`` logs each run and hangs while ``<name>.slow`` exists."""
    import stat
    import sys
    script = env["tmp"] / name
    runs, slow = env["tmp"] / f"{name}.runs", env["tmp"] / f"{name}.slow"
    script.write_text(f"#!{sys.executable}\nimport os, time\n"
                      f"open({str(runs)!r}, 'a').write('x')\n"
                      f"while os.path.exists({str(slow)!r}):\n    time.sleep(0.05)\n"
                      "print('slow 2.0.0')\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    slow.touch()
    return str(script), (lambda: len(runs.read_text()) if runs.exists() else 0), slow


def _concurrently(n, fn):
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(n) as pool:
        return list(pool.map(lambda _: fn(), range(n)))


def test_timed_out_version_probe_cools_down_then_recovers(env, monkeypatch):
    from agent_bus import peers
    bus = env["bus"]
    monkeypatch.setattr(peers, "VERSION_PROBE_TIMEOUT_S", 0.5)
    script, runs, slow = _slow_cli(env)
    key = cli_identity(script)[1]

    assert cli_version(bus, "codex", script) == "unknown"
    assert runs() == 1
    # Not cached as the binary's answer: only a cooldown is recorded.
    assert bus.cli_version(key) is None
    assert bus.cli_version_retry_after(key) > time.time() + peers.VERSION_TIMEOUT_COOLDOWN_S - 5

    # During the cooldown: "unknown" at once, without another probe.
    slow.unlink()
    started = time.monotonic()
    assert cli_version(bus, "codex", script) == "unknown"
    assert time.monotonic() - started < 0.3 and runs() == 1

    # Once it expires, the next caller probes again and the answer is cached as usual.
    bus.set_cli_version_timeout(key, "codex", script, time.time() - 1)
    assert cli_version(bus, "codex", script) == "slow 2.0.0"
    assert runs() == 2
    assert bus.cli_version(key) == "slow 2.0.0" and bus.cli_version_retry_after(key) is None
    assert cli_version(bus, "codex", script) == "slow 2.0.0" and runs() == 2


def test_concurrent_callers_share_one_version_probe(env, monkeypatch):
    from agent_bus import peers
    bus = env["bus"]
    monkeypatch.setattr(peers, "VERSION_PROBE_TIMEOUT_S", 0.5)
    script, runs, slow = _slow_cli(env)
    key = cli_identity(script)[1]

    # Callers that arrive during a probe that times out wait for it, then share its
    # "unknown" rather than each queueing a probe of their own.
    assert _concurrently(4, lambda: cli_version(bus, "codex", script)) == ["unknown"] * 4
    assert runs() == 1

    # After the cooldown, concurrent callers share a single fresh probe.
    slow.unlink()
    bus.set_cli_version_timeout(key, "codex", script, time.time() - 1)
    assert _concurrently(4, lambda: cli_version(bus, "codex", script)) == ["slow 2.0.0"] * 4
    assert runs() == 2


def test_timed_out_version_probe_kills_grandchild_holding_its_pipes(env, monkeypatch):
    """#52: a wrapper whose grandchild inherits stdout (and ignores SIGTERM) must not hang
    the probe, and the per-binary lock, past its timeout: the whole group is killed."""
    import stat
    from agent_bus import peers
    monkeypatch.setattr(peers, "VERSION_PROBE_TIMEOUT_S", 0.5)
    script, fifo = env["tmp"] / "shim-cli", env["tmp"] / "shim-cli.alive"
    # The grandchild also holds a FIFO open: EOF on it means it is dead, even if its
    # PID lingers as an unreaped zombie.
    os.mkfifo(fifo)
    alive = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
    try:
        script.write_text("#!/bin/sh\n"
                          f"(trap '' TERM; echo up >&3; exec sleep 60) 3>'{fifo}' &\n"
                          "echo 'shim 1.0.0'\n")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)

        started = time.monotonic()
        assert cli_version(env["bus"], "codex", str(script)) == "unknown"
        assert time.monotonic() - started < 10
        seen, deadline = b"", time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                chunk = os.read(alive, 64)
            except BlockingIOError:  # still held open: the grandchild lives
                time.sleep(0.05)
                continue
            if not chunk:
                break
            seen += chunk
        else:
            pytest.fail("the probe's grandchild survived its timeout")
    finally:
        os.close(alive)
    assert seen == b"up\n"  # it did start, and hold the pipes, before being killed


def test_upgrade_drops_unknown_versions_cached_by_older_releases(env):
    """Codex review of PR #49: before #41 a timed-out probe was cached as "unknown" for
    good.  Opening such a database once makes those binaries eligible for a new probe."""
    from agent_bus.db import Bus
    bus = env["bus"]
    command = cfg.Config.load().peer_command("codex")
    key = cli_identity(command)[1]
    bus.set_cli_version(key, "codex", command, "unknown")
    bus.set_cli_version("other-binary", "claude", "/x/claude", "claude 1.0.0")
    bus.conn.execute("DROP TABLE cli_version_timeouts")  # as an older release left it
    upgraded = Bus(bus.path)
    try:
        assert upgraded.cli_version(key) is None
        assert upgraded.cli_version("other-binary") == "claude 1.0.0"
        assert cli_version(upgraded, "codex", command) == "fake-codex 0.0.0"
        # Only once: a definite "unknown" cached after the upgrade stays cached.
        upgraded.set_cli_version("gone", "codex", "/x/codex", "unknown")
    finally:
        upgraded.close()
    again = Bus(bus.path)
    try:
        assert again.cli_version("gone") == "unknown"
    finally:
        again.close()
