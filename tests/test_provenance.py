"""#108: the human's commands record where they came from, and history/tail show it."""

import json
import sqlite3

import pytest

from agent_bus import cli, prompts, provenance
from agent_bus.db import HUMAN, SYSTEM, Bus
from agent_bus.service import Service, present

REAL_ANCESTRY = provenance.ancestry  # conftest swaps these for a clean terminal in every test
REAL_CONTROLS = provenance.controls
REAL_TERMINAL = provenance.terminal


def clean(**over):
    rec = {"v": 1, "command": "reply", "pid": 4200, "ancestry": [{"pid": 4120, "argv": "zsh"},
                                                                {"pid": 4100, "argv": "terminal"}],
           "tty": "/dev/pts/0", "controlling_tty": True, "guard_overridden": False, "harness_env": []}
    rec.update(over)
    return rec


def thread(bus):
    return bus.create_thread("discussion", "g", ["claude", "codex"], "claude")["id"]


def history_json(capsys, tid):
    assert cli.main(["history", tid, "--json"]) == 0
    return json.loads(capsys.readouterr().out)


# ------------------------------------------------------------------ collecting


def test_short_argv_keeps_names_not_paths_or_values():
    assert provenance.short_argv(["/usr/bin/zsh", "-l"]) == "zsh -l"
    assert provenance.short_argv(["node", "/opt/lib/node_modules/x/cli.js", "--resume"]) == \
        "node .../cli.js --resume"
    assert provenance.short_argv(["python3", *[f"arg{i}" for i in range(10)]]) == "python3 * * * * ..."
    assert provenance.short_argv(None) is None and provenance.short_argv([]) is None


@pytest.mark.parametrize("argv,want", [
    (["bash", "-c", "source /opt/x/env.sh && curl -H 'Authorization: x'"], "bash -c *"),
    (["tool", "--token=abc123", "run"], "tool --token=* run"),
    (["tool", "--api-key", "abc123", "run"], "tool --api-key * run"),
    (["tool", "--api_key", "abc123"], "tool --api_key *"),
    (["tool", "--output", "file.txt"], "tool --output *"),
    (["tool", "--output=/opt/out.txt", "./run.sh"], "tool --output=* .../run.sh"),
    (["pinentry-wrapper", "4821"], "pinentry-wrapper *"),
    (["tool", "-pShortSecret"], "tool -*"),
    (["tool", "https://user:pw@example.com/x"], "tool *"),
    (["tool", "ghp_" + "a" * 36], "tool *"),
    (["tool", "--config=/opt/secret/dir/conf.json"], "tool --config=*"),
    (["codex", "app-server", "--listen", "stdio://"], "codex app-server --listen *"),
    (["tool", "--secret-key", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"], "tool --secret-key *"),
    (["node", "./bin/cli.js", "--", "x"], "node .../cli.js -- *"),
    (["tool", "--secret-key", "/" + "Ab3+" * 8], "tool --secret-key *"),
    (["tool", "/" + "Ab3x" * 8], "tool .../*"),
    (["tool", "-k", "/opt/x/key.pem", "-v"], "tool -k * -v"),
])
def test_short_argv_redacts_what_could_hold_a_credential(argv, want):
    assert provenance.short_argv(argv) == want


# The README's "What the redaction keeps" rules, one case each (#132).
@pytest.mark.parametrize("argv,want", [
    (["tool", "--mode", "exec"], "tool --mode exec"),  # 1: an allowlisted word, even after a flag
    (["tool", "-o", "--", "x"], "tool -o -- *"),  # 1: "--" too
    (["tool", "-o", "value"], "tool -o *"),  # 2: a flag's value
    (["tool", "-o", "-v"], "tool -o -v"),  # 2: unless it starts with "-"
    (["tool", "--a", "--b", "--c=x"], "tool --a --b --c=*"),  # 3: long flags, even after a flag
    (["tool", "--" + "x" * 32], "tool -*"),  # 3/4: too long for a long flag
    (["tool", "-v", "-abc"], "tool -v -*"),  # 4
    (["tool", "~/" + "a" * 24], "tool .../" + "a" * 24),  # 5: a component of 24 characters
    (["tool", "~/" + "a" * 25], "tool .../*"),  # 5: over 24
    (["tool", "/opt/a b"], "tool *"),  # 5: whitespace: not a path
    (["tool", "plain"], "tool *"),  # 6
    (["x" * 41], "*"),  # an executable name over 40 characters
])
def test_short_argv_keeps_exactly_what_the_readme_says(argv, want):
    assert provenance.short_argv(argv) == want


def test_ancestry_marks_harnesses_from_the_full_argv_and_cuts_long_chains():
    argvs = {10: ["bash"], 11: ["node", "/opt/lib/node_modules/@openai/codex/bin/codex.js"], 12: None}
    chain, cut = REAL_ANCESTRY(1, ancestors_of=lambda pid: [10, 11, 12], argv_of=argvs.get)
    assert chain == [{"pid": 10, "argv": "bash"}, {"pid": 11, "argv": "node .../codex.js", "harness": "@openai/codex"},
                     {"pid": 12, "argv": None}]
    assert not cut
    chain, cut = REAL_ANCESTRY(1, ancestors_of=lambda pid: list(range(100, 140)), argv_of=lambda p: ["sh"])
    assert len(chain) == provenance.MAX_DEPTH and cut


def test_collect_records_tty_override_and_harness_markers():
    rec = provenance.collect("reply", env={"CLAUDECODE": "1", "AGENT_BUS_ALLOW_HUMAN": "1", "HOME": "/x"},
                             pid=7, ancestry_of=lambda pid: ([{"pid": 6, "argv": "zsh"}], False),
                             terminal_of=lambda: (None, False))
    assert rec == {"v": 1, "command": "reply", "pid": 7, "ancestry": [{"pid": 6, "argv": "zsh"}], "tty": None,
                   "controlling_tty": False, "guard_overridden": True, "harness_env": ["CLAUDECODE"]}
    rec = provenance.collect("x", env={}, pid=7, ancestry_of=lambda pid: ([], True),
                             terminal_of=lambda: ("/dev/pts/1", False))
    assert rec["ancestry_truncated"] is True and rec["controlling_tty"] is False  # another pty than its own


# ------------------------------------------------------------------ flagging


def codes(rec, records=1):
    return [c for c, _ in provenance.flags(rec, records)]


def test_a_human_at_their_own_terminal_is_not_flagged():
    assert codes(clean()) == []
    assert provenance.peer_note(clean()) == ""


@pytest.mark.parametrize("over,code", [
    ({"guard_overridden": True}, "guard overridden"),
    ({"harness_env": ["CODEX_THREAD_ID"]}, "harness env"),
    ({"ancestry": [{"pid": 5, "argv": "bash"}, {"pid": 4, "argv": "claude", "harness": "claude"}]},
     "agent in ancestry"),
    ({"tty": None}, "no tty"),
    ({"controlling_tty": False}, "no controlling tty"),
    ({"ancestry": []}, "detached"),
    ({"ancestry": [{"pid": 900, "argv": "systemd --user"}]}, "detached"),
])
def test_what_gets_flagged(over, code):
    assert codes(clean(**over)) == [code]


def test_missing_unreadable_and_duplicate_records_are_never_clean():
    assert codes(None) == ["none recorded"]
    assert "no provenance recorded" in provenance.flags(None)[0][1]
    assert codes({}) == ["unreadable"] and codes({"ancestry": "zsh"}) == ["unreadable"]
    for bad in ({"harness_env": 3}, {"harness_env": [1]}, {"tty": 5}, {"ancestry": [3]},
                {"ancestry": [{"pid": 1, "argv": ["zsh"]}]}, {"ancestry": [{"pid": 1, "argv": "x", "harness": 2}]}):
        assert codes(clean(**bad)) == ["unreadable"]
        assert provenance.describe(clean(**bad))[1] == []
        provenance.peer_note(clean(**bad))
    assert codes(clean(), records=2) == ["several records"]


def test_peer_note_has_codes_but_no_pids_or_paths():
    rec = clean(tty=None, ancestry=[{"pid": 4321, "argv": "claude --resume", "harness": "claude"}])
    note = provenance.peer_note(rec)
    assert "no tty" in note and "agent in ancestry" in note
    assert "4321" not in note and "claude --resume" not in note


# ------------------------------------------------------------------ recording


def test_reply_records_provenance_with_its_message(env, capsys):
    bus = env["bus"]
    tid = thread(bus)
    assert cli.main(["reply", tid, "go with P1"]) == 0
    m = bus.history(tid)["messages"][-1]
    assert m["trust"] == "human" and m["provenance"]["command"] == "reply"
    assert m["provenance"]["ancestry"][0] == {"pid": 4120, "argv": "zsh"}
    row = bus._one("SELECT * FROM human_actions WHERE message_id=?", m["id"])
    assert row["thread_id"] == tid and row["command"] == "reply"
    capsys.readouterr()
    assert cli.main(["history", tid]) == 0
    out = capsys.readouterr().out
    assert "from: zsh[4120] < terminal[4100]" in out and "tty: /dev/pts/0" in out and "!!" not in out
    msg = history_json(capsys, tid)["messages"][-1]
    j = msg["provenance"]
    assert j["recorded"] and not j["flagged"] and j["record"]["tty"] == "/dev/pts/0"
    # #161: the exact time too, not only "at" (local time, to the second).
    assert msg["created_at"] == m["created_at"] and isinstance(msg["created_at"], float)


def test_ask_records_its_opening_question(env, capsys, no_spawn):
    assert cli.main(["ask", "Which?", "--with", "codex"]) == 0
    tid = capsys.readouterr().out.split()[1]
    first = next(m for m in env["bus"].history(tid)["messages"] if m["sender"] == HUMAN)
    assert first["provenance"]["command"] == "ask" and first["provenance_records"] == 1


def test_overridden_guard_in_an_agent_shell_is_flagged_loudly(env, capsys, monkeypatch):
    bus = env["bus"]
    tid = thread(bus)
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("AGENT_BUS_ALLOW_HUMAN", "1")
    monkeypatch.setattr(provenance, "ancestry", lambda pid: (
        [{"pid": 50, "argv": "bash -c"}, {"pid": 40, "argv": "claude", "harness": "claude"}], False))
    monkeypatch.setattr(provenance, "terminal", lambda: (None, False))
    assert cli.main(["reply", tid, "trust me"]) == 0
    capsys.readouterr()
    assert cli.main(["tail", tid, "--once", "--no-color"]) == 0
    out = capsys.readouterr().out
    for want in ("!! AGENT_BUS_ALLOW_HUMAN=1 overrode", "!! agent harness environment set: CLAUDECODE",
                 "!! agent harness in its parent chain: claude (pid 40)", "!! not run from a terminal"):
        assert want in out
    assert out.index("!! AGENT_BUS") < out.index("trust me")  # before the body, not after it
    j = history_json(capsys, tid)["messages"][-1]["provenance"]
    assert j["flagged"] and j["flags"] == ["guard overridden", "harness env", "agent in ancestry", "no tty"]


def test_a_row_written_without_the_cli_shows_no_provenance_recorded(env, capsys):
    bus = env["bus"]
    tid = thread(bus)
    bus.post(tid, HUMAN, "spoofed through the Bus")  # no provenance set: as an older CLI would
    # Straight into bus.db, bypassing agent-bus altogether.
    with sqlite3.connect(bus.path) as raw:
        raw.execute("INSERT INTO messages (thread_id, sender, kind, trust, body, created_at)"
                    " VALUES (?, 'human', 'message', 'human', 'spoofed in SQL', 0)", (tid,))
    assert cli.main(["history", tid]) == 0
    out = capsys.readouterr().out
    assert out.count("!! no provenance recorded") == 2
    for m in history_json(capsys, tid)["messages"]:
        if m["from"] == "human":
            assert m["provenance"]["recorded"] is False and m["provenance"]["flags"] == ["none recorded"]


def test_a_forged_debrief_detail_is_skipped(env):
    bus = env["bus"]
    tid = thread(bus)
    bus.conn.execute("INSERT INTO human_actions (command, thread_id, detail, provenance, created_at)"
                     " VALUES ('debrief', ?, 'not json', '{}', 0)", (tid,))
    assert bus._provenance(job_id="j-1") == (None, 0)


def test_a_forged_unreadable_record_is_flagged(env, capsys):
    bus = env["bus"]
    tid = thread(bus)
    mid = bus.post(tid, HUMAN, "hi")["id"]
    bus.conn.execute("INSERT INTO human_actions (command, thread_id, message_id, provenance, created_at)"
                     " VALUES ('reply', ?, ?, 'not json', 0)", (tid, mid))
    assert history_json(capsys, tid)["messages"][-1]["provenance"]["flags"] == ["unreadable"]


def test_a_forged_record_cannot_drive_the_terminal():
    rec = clean(tty="/dev/pts/0\x1b[2J", ancestry=[{"pid": 1, "argv": "zsh\x1b]0;x\x07", "harness": "x\x1b[31m"}])
    warn, info = provenance.describe(rec)
    assert warn and info and not any("\x1b" in x or "\x07" in x for x in warn + info)


def test_a_second_record_for_one_message_is_flagged(env, capsys):
    bus = env["bus"]
    tid = thread(bus)
    assert cli.main(["reply", tid, "hi"]) == 0
    mid = bus.history(tid)["messages"][-1]["id"]
    bus.conn.execute("INSERT INTO human_actions (command, thread_id, message_id, provenance, created_at)"
                     " VALUES ('reply', ?, ?, ?, 0)", (tid, mid, json.dumps(clean())))
    capsys.readouterr()
    assert history_json(capsys, tid)["messages"][-1]["provenance"]["flags"] == ["several records"]


def test_cancel_and_resume_are_recorded_as_actions(env, capsys, no_spawn):
    bus = env["bus"]
    tid = thread(bus)
    bus.set_thread(tid, status="needs_human")
    assert cli.main(["resume", tid]) == 0
    assert cli.main(["cancel", tid]) == 0
    acts = bus.human_actions(tid)
    assert [a["command"] for a in acts] == ["resume", "cancel"] and acts[0]["detail"] == {"rounds": 2}
    capsys.readouterr()
    assert cli.main(["history", tid]) == 0
    out = capsys.readouterr().out
    assert "human action: resume (rounds 2)" in out and "human action: cancel" in out
    assert out.index("human action: resume") < out.index("human action: cancel")
    j = history_json(capsys, tid)
    assert [a["command"] for a in j["human_actions"]] == ["resume", "cancel"]
    assert not j["human_actions"][0]["provenance"]["flagged"]
    assert [a["created_at"] for a in j["human_actions"]] == [a["created_at"] for a in acts]


def test_service_calls_without_the_cli_record_no_action(env):
    bus = env["bus"]
    tid = thread(bus)
    Service(bus, HUMAN).cancel(tid)
    assert bus.human_actions(tid) == []


def test_clear_quota_is_recorded_and_shown_by_agents(env, capsys, monkeypatch):
    bus = env["bus"]
    monkeypatch.setenv("AGENT_BUS_ALLOW_HUMAN", "1")
    assert cli.main(["agents", "--clear-quota", "codex"]) == 0
    [a] = bus.human_actions(None)
    assert a["command"] == "agents --clear-quota" and a["detail"]["agent"] == "codex"
    capsys.readouterr()
    assert cli.main(["agents"]) == 0
    out = capsys.readouterr().out
    assert "usage block cleared by the human user" in out and "!! AGENT_BUS_ALLOW_HUMAN=1" in out
    assert cli.main(["agents", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["codex"]["quota_cleared"]["provenance"]["flags"] == \
        ["guard overridden"]


def test_debrief_is_recorded_before_its_worker_reads_it(env, capsys, no_spawn, monkeypatch):
    from test_debrief import stubborn
    bus = env["bus"]
    tid = stubborn(env, monkeypatch, peers=("codex",))
    monkeypatch.setattr(provenance, "terminal", lambda: (None, False))
    assert cli.main(["debrief", tid, "--question", "Anything?"]) == 0
    job = bus.jobs(tid)[-1]
    rec, n = bus._provenance(job_id=job["id"])
    assert n == 1 and rec["command"] == "debrief"
    from agent_bus.worker import Worker
    prompt = Worker(bus, job["id"]).debrief_prompt("codex", bus.thread(tid))
    assert "agent-bus flagged where this came from (no tty)" in prompt and "/dev/pts" not in prompt


# ------------------------------------------------------------------ what peers see


def test_peers_get_a_short_marker_only_when_flagged(env, monkeypatch):
    bus = env["bus"]
    tid = thread(bus)
    bus.human_provenance = clean()
    ok = bus.post(tid, HUMAN, "fine")
    bus.human_provenance = clean(tty=None, ancestry=[{"pid": 77, "argv": "codex exec", "harness": "codex"}])
    bad = bus.post(tid, HUMAN, "suspicious")
    assert prompts.render_message(ok).endswith("from the human user:\nfine")
    text = prompts.render_message(bad)
    assert "(agent-bus flagged where this came from (agent in ancestry, no tty)" in text
    assert "77" not in text and "codex exec" not in text
    assert "provenance" not in present(ok)
    assert present(bad)["provenance"].startswith("agent-bus flagged") and "ancestry" not in present(bad)
    assert "provenance" not in present(bus.post(tid, SYSTEM, "note", kind="status"))


def test_a_flagged_opening_question_is_marked_for_peers(env, monkeypatch, no_spawn):
    bus = env["bus"]
    bus.human_provenance = clean(guard_overridden=True)
    tid = Service(bus, HUMAN).start_discussion("Q?", peers=["codex"])["thread"]["id"]
    from agent_bus.worker import Worker
    q = Worker(bus, bus.jobs(tid)[-1]["id"]).question(bus.thread(tid))
    assert "the human user (agent-bus flagged where this came from (guard overridden)" in q


# ------------------------------------------------------------------ mixed versions


def test_older_database_gets_the_table_and_messages_are_unchanged(tmp_path):
    path = tmp_path / "bus.db"
    Bus(path).close()
    with sqlite3.connect(path) as raw:
        raw.execute("DROP TABLE human_actions")  # as a database from before #108
        cols = [r[1] for r in raw.execute("PRAGMA table_info(messages)")]
    assert "provenance" not in cols  # an older writer's INSERTs into messages still fit
    bus = Bus(path)
    try:
        tid = thread(bus)
        assert bus.post(tid, HUMAN, "hi")["provenance"] is None
        assert bus.human_actions(tid, with_messages=True) == []
    finally:
        bus.close()


def test_a_pty_that_is_not_ours_is_not_the_controlling_terminal():
    import os
    master, slave = os.openpty()  # a terminal, but never this process's controlling one
    try:
        assert os.isatty(slave) and REAL_CONTROLS(slave) is False
    finally:
        os.close(master)
        os.close(slave)
    assert REAL_CONTROLS(-1) is False


def test_a_pty_master_on_stdin_is_not_a_controlling_terminal(monkeypatch):
    import os
    monkeypatch.setattr(os, "isatty", lambda fd: fd == 0)
    monkeypatch.setattr(os, "ttyname", lambda fd: "/dev/ptmx")
    monkeypatch.setattr(provenance, "controls", lambda fd: True)  # as tcgetpgrp answers on a master
    assert REAL_TERMINAL() == ("/dev/ptmx", False)


def test_the_goal_marker_is_the_opening_questions_not_a_later_reply(env, no_spawn):
    from agent_bus.service import thread_summary
    bus = env["bus"]
    tid = bus.create_thread("discussion", "Q?", ["codex"], HUMAN)["id"]
    bus.human_provenance = clean()
    bus.post(tid, HUMAN, "an early reply")  # before any opening question: never stands in for it
    assert "none recorded" in thread_summary(bus.thread(tid))["goal_provenance"]


def test_cancel_and_its_record_commit_together(env, monkeypatch):
    bus = env["bus"]
    tid = thread(bus)

    def boom(self, *a, **k):
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(Bus, "record_human_action", boom)
    with pytest.raises(sqlite3.OperationalError):
        cli.main(["cancel", tid])
    assert bus.thread(tid)["status"] == "open"
    assert not any("Cancelled" in m["body"] for m in bus.history(tid)["messages"])


@pytest.mark.parametrize("rec,note", [(clean(), None), (clean(tty=None), "no tty"), (None, "none recorded")])
def test_the_goal_carries_the_opening_questions_marker(env, no_spawn, rec, note):
    from agent_bus.service import thread_summary
    bus = env["bus"]
    bus.human_provenance = rec
    tid = Service(bus, HUMAN).start_discussion("Q?", peers=["codex"])["thread"]["id"]
    out = thread_summary(bus.thread(tid))
    if note is None:
        assert "goal_provenance" not in out
    else:
        assert note in out["goal_provenance"] and out["goal"] == "Q?"
    assert "goal_provenance" not in thread_summary(bus.thread(tid), goal=False)
    assert "goal_provenance" not in Service(bus, HUMAN).wait_timeout(tid, deliver=False)["thread"]
