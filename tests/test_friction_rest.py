"""#126 follow-ups: a new session of the same client starts where its others read to, the
MCP server reaps the workers it spawns, follow-ups quote earlier blockers in full, the
usage note shows pre-split cache writes, and names the diff only reads are searched."""

import asyncio
import json
import os
import re
import time

from agent_bus import cli, mcp_server, procinfo, usages, worker
from agent_bus.service import Service


def call(server, tool, args):
    res = asyncio.run(server.call_tool(tool, args))
    return json.loads(res.content[0].text)


# ------------------------------------------------------------------ read cursors


def bodies(res):
    """The messages' text, without the peer_data fence."""
    return [m["body"].split("\n")[1] if m["body"].startswith("[peer_data") else m["body"]
            for m in res["messages"]]


def client_session(bus, agent="claude", **kw):
    """A session of this test process's client (alive, so never reattached to)."""
    me = os.getpid()
    return bus.register_session(agent, client_pid=me, client_start=procinfo.start_time(me), **kw)


def test_a_new_session_of_the_same_client_does_not_replay_what_it_read(env, no_spawn):
    bus = env["bus"]
    codex = Service(bus, "codex", bus.register_session("codex"))
    tid = codex.send("one", to="claude")["thread"]["id"]
    codex.send("two", thread=tid)
    first = Service(bus, "claude", client_session(bus))
    assert bodies(first.inbox(tid)) == ["one", "two"]
    # The client starts another MCP server (a subagent's), which sends and then waits.
    second = Service(bus, "claude", client_session(bus))
    assert second.subscriber != first.subscriber
    assert second.poll(tid) is None and second.inbox(tid)["messages"] == []
    second.send("my note", thread=tid)
    codex.send("three", thread=tid)
    assert bodies(second.poll(tid)) == ["three"]
    # From there on each session keeps its own cursor.
    assert bodies(first.inbox(tid)) == ["three"]
    codex.send("four", thread=tid)
    assert bodies(second.inbox(tid)) == ["four"]
    assert bodies(first.inbox(tid)) == ["four"]
    # after_message_id still replays what was read.
    assert bodies(second.poll(tid, after_message_id=0)) == ["one", "two", "three", "four"]


def test_an_inherited_cursor_is_kept_from_first_access(env, no_spawn):
    """codex-1: once a session has looked, its position is its own; and a paged replay
    from before the inherited position never moves it back."""
    bus = env["bus"]
    codex = Service(bus, "codex", bus.register_session("codex"))
    tid = codex.send("one", to="claude")["thread"]["id"]
    first = Service(bus, "claude", client_session(bus))
    second = Service(bus, "claude", client_session(bus))
    assert bodies(first.inbox(tid)) == ["one"]
    assert second.inbox(tid)["messages"] == []  # inherits: nothing unread
    codex.send("two", thread=tid)
    assert bodies(first.inbox(tid)) == ["two"]
    assert bodies(second.inbox(tid)) == ["two"]  # not skipped because first read it
    third = Service(bus, "claude", client_session(bus))
    assert bodies(third.poll(tid, after_message_id=0, limit=1)) == ["one"]
    codex.send("three", thread=tid)
    assert bodies(third.inbox(tid)) == ["three"]  # not "two" again


def test_other_clients_and_shared_daemons_start_from_the_top(env, no_spawn):
    bus = env["bus"]
    codex = Service(bus, "codex", bus.register_session("codex"))
    tid = codex.send("one", to="claude")["thread"]["id"]
    assert Service(bus, "claude", client_session(bus)).inbox(tid)["messages"]
    # Another client, an unverified one, or another participant: nothing inherited.
    other = bus.register_session("claude", client_pid=1, client_start="linux:1")
    assert bodies(Service(bus, "claude", other).inbox(tid)) == ["one"]
    assert Service(bus, "claude", bus.register_session("claude")).inbox(tid)["messages"]
    # A shared Codex daemon serves several windows: its sessions never share a position.
    a = Service(bus, "codex", client_session(bus, "codex", rex_reason=procinfo.CODEX_SHARED_DAEMON))
    b = Service(bus, "codex", client_session(bus, "codex", rex_reason=procinfo.CODEX_SHARED_DAEMON))
    claude = Service(bus, "claude", bus.register_session("claude"))
    claude.send("hi codex", thread=tid)
    assert a.inbox(tid)["messages"] and bodies(b.inbox(tid)) == ["hi codex"]


def test_wait_through_a_second_mcp_server_returns_only_unread(env, no_spawn):
    """The report in #126: wait after a send, from a new server of the same client,
    returned the whole thread."""
    bus = env["bus"]
    first = mcp_server.build("claude", bus=bus)
    codex = Service(bus, "codex", bus.register_session("codex"))
    tid = codex.send("long verdict", to="claude")["thread"]["id"]
    assert [m["from"] for m in call(first, "wait", {"thread": tid, "timeout_s": 1})["messages"]] == ["codex"]
    second = mcp_server.build("claude", bus=bus)
    call(second, "send", {"thread": tid, "body": "addressed"})
    codex.send("thanks", thread=tid)
    res = call(second, "wait", {"thread": tid, "timeout_s": 2})
    assert ["thanks" in m["body"] for m in res["messages"]] == [True]


# ------------------------------------------------------------------ reaping


def test_spawned_workers_are_reaped(env, monkeypatch):
    monkeypatch.setattr(worker.cfg, "self_command", lambda: ["true"])
    pid = worker.spawn("j-reap")
    deadline = time.time() + 10
    while time.time() < deadline:
        state = procinfo.proc(pid)
        if state is None:
            break  # gone: reaped, not left as a zombie
        time.sleep(0.05)
    assert procinfo.proc(pid) is None


# ------------------------------------------------------------------ follow-up prompts


def test_earlier_blockers_are_quoted_in_full_within_a_cap():
    long = "word " * 200 + "the end."
    mine = {"data": {"head_commit_id": "a" * 40}}
    prior = {"codex-1": {"id": "codex-1", "file": "a.py", "line": 3, "severity": "high", "comment": long}}
    note = worker.earlier_findings_note("codex", mine, prior)
    assert "the end." in note and "…" not in note
    many = {f"codex-{i}": {"id": f"codex-{i}", "file": "a.py", "severity": "medium", "comment": "x" * 3999 + "Z"}
            for i in range(6)}
    note = worker.earlier_findings_note("codex", mine, many)
    # 12,000 characters in all: two whole, the third cut to leave 300 for each of the rest.
    assert note.count("Z") == 2 and note.count("… (cut short)") == 4
    assert sum(map(len, re.findall("x{50,}", note))) == worker.EARLIER_TEXT_CAP - 2
    assert all(f"codex-{i}" in note for i in range(6))
    # Short ones later reserve only their own length, so nothing fitting is cut (codex-5).
    mixed = {f"codex-{i}": {"id": f"codex-{i}", "file": "a.py", "severity": "medium",
                            "comment": ("x" * 3899 + "Z") if i < 3 else "s" * 100} for i in range(6)}
    note = worker.earlier_findings_note("codex", mine, mixed)
    assert note.count("Z") == 3 and "cut short" not in note
    # Too many for 300 each: every id still listed, the text shared out within the cap (codex-3).
    lots = {f"codex-{i}": {"id": f"codex-{i}", "file": "a.py", "severity": "medium", "comment": "y" * 400}
            for i in range(100)}
    note = worker.earlier_findings_note("codex", mine, lots)
    assert sum(map(len, re.findall("y{20,}", note))) <= worker.EARLIER_TEXT_CAP and all(f"codex-{i} " in note for i in range(100))


# ------------------------------------------------------------------ usage note


def test_usage_note_shows_pre_split_cache_writes(env, capsys):
    bus = env["bus"]
    bus.conn.execute("INSERT INTO turn_usage (thread_id, agent, ok, input, cache_read, cache_write, started_at,"
                     " created_at) VALUES ('t-1', 'claude', 1, 10, 20, 30, ?, ?)", (time.time(), time.time()))
    assert cli.main(["usage"]) == 0
    assert "claude 1 turn, input 10 cache read 20 cache write 30" in capsys.readouterr().out
    bus.conn.execute("UPDATE turn_usage SET cache_write=0")
    assert cli.main(["usage"]) == 0
    assert "claude 1 turn, input 10 cache read 20)" in capsys.readouterr().out


# ------------------------------------------------------------------ names only read


def test_terms_include_keys_and_attributes_the_added_lines_read():
    diff = ("diff --git a/w.py b/w.py\n--- a/w.py\n+++ b/w.py\n"
            "@@ -1,3 +1,4 @@ def opening(refs):\n"
            "+    focus = refs.get(\"opening_focus\") or refs['fallback_text']\n"
            "+    if t.launched and getattr(t, \"retry_count\", 0):\n"
            "+        t.record_launch(focus)\n"
            "+    # comment.mentions_this\n"
            "+    labels = [\"display_label\"]\n"
            "-    old = refs.get(\"removed_key\")\n")
    found = usages.terms(diff)
    reads = [t.pattern for t in found if t.kind == "read"]
    assert reads == ["opening_focus", "fallback_text", "retry_count", "launched"]  # keys, then attributes
    assert "display_label" not in reads  # a list literal, not a key (codex-4)
    assert "record_launch" not in reads and "mentions_this" not in reads and "removed_key" not in reads
    assert "opening" in [t.pattern for t in found if t.kind == "enclosing"]
    assert "opening_focus" not in usages.symbols(diff)  # symbols(): the diff's own names only
    u = usages.Usages(names=["opening_focus"], hits={"opening_focus": [("s.py", 4, "refs['opening_focus'] = x")]},
                      terms={"opening_focus": usages.Term("opening_focus", "read")})
    assert "opening_focus (a key or attribute the changed lines read: where it is set; 1 hit" in usages.render(u)
    # Capped, and never the diff's own names.
    many = "".join(f"+    x = cfg.get(\"key_{i}\")\n" for i in range(10))
    diff2 = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n+def key_0(): pass\n" + many
    reads2 = [t.pattern for t in usages.terms(diff2) if t.kind == "read"]
    assert len(reads2) == usages.MAX_READ and "key_0" not in reads2
