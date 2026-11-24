"""The MCP `wait` tool (#72): quiet lifecycle notices, peer progress on timeout,
concurrent waits sharing one poller, replay by message id, and a reconnecting
client keeping its session."""

import asyncio
import json
import os
import subprocess
import sys
import time

from agent_bus import mcp_server, procinfo
from agent_bus.db import SYSTEM, is_quiet
from agent_bus.service import Service


def call(server, tool, args):
    res = asyncio.run(server.call_tool(tool, args))
    return json.loads(res.content[0].text)


async def acall(server, tool, args):
    res = await server.call_tool(tool, args)
    return json.loads(res.content[0].text)


def chat(bus):
    """A chat thread from codex to claude, and claude's MCP server."""
    server = mcp_server.build("claude", bus=bus)
    codex = Service(bus, "codex", bus.register_session("codex"))
    tid = codex.send("hello", to="claude")["thread"]["id"]
    return server, codex, tid


def dead_pid():
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def test_quiet_notices_do_not_end_a_wait(env, no_spawn):
    bus = env["bus"]
    server, codex, tid = chat(bus)
    assert call(server, "wait", {"thread": tid, "timeout_s": 1})["messages"][0]["body"].endswith("[/peer_data]")
    bus.post(tid, SYSTEM, "Review requested from codex at head 0123456789ab.", kind="status",
             data={"quiet": True})
    start = time.monotonic()
    res = call(server, "wait", {"thread": tid, "timeout_s": 2})
    assert time.monotonic() - start >= 1.5  # the notice alone didn't wake it
    assert res["status"] == "timeout"
    # ...but it is still delivered, once, without the internal flag.
    assert [m["body"] for m in res["messages"]] == ["Review requested from codex at head 0123456789ab."]
    assert "data" not in res["messages"][0]
    assert "goal" not in res["thread"]
    assert res["cursor"] == res["messages"][0]["id"]
    # A quiet notice followed by real mail comes back together, at once.
    bus.post(tid, SYSTEM, "Round 2 of 3: vote on a proposal or revise one.", kind="status", data={"quiet": True})
    codex.send("verdict soon", thread=tid)
    res = call(server, "wait", {"thread": tid, "timeout_s": 5})
    assert [m["from"] for m in res["messages"]] == ["system", "codex"]
    assert "status" not in res


def test_legacy_lifecycle_notices_are_quiet_and_others_are_not(env, no_spawn):
    bus = env["bus"]
    _, _, tid = chat(bus)

    def quiet(body, sender=SYSTEM, kind="status"):
        return is_quiet(bus.post(tid, sender, body, kind=kind))

    assert quiet("Review requested from codex at head 0123456789ab.")
    assert quiet("Review follow-up (retry) requested from antigravity at head 0123456789ab. The full diff ...")
    assert quiet("Round 2 of 4: vote on a proposal or revise one.")
    assert quiet("Discussion started by claude with claude, codex; up to 3 rounds.")
    assert not quiet("Waiting for claude (initiator) to post for round 2.")
    assert not quiet("codex could not take its turn: timeout")
    assert not quiet("Round 2 of 4: vote on a proposal or revise one.", kind="message")


def test_a_full_page_of_quiet_notices_still_ends_the_wait(env, no_spawn):
    bus = env["bus"]
    _, codex, tid = chat(bus)
    claude = Service(bus, "claude", bus.register_session("claude"))
    claude.inbox(thread=tid)
    for i in range(3):
        bus.post(tid, SYSTEM, f"Round {i + 2} of 9: vote on a proposal or revise one.", kind="status")
    codex.send("after the notices", thread=tid)
    assert claude.poll(tid, limit=4)["messages"][-1]["from"] == "codex"
    for i in range(3):
        bus.post(tid, SYSTEM, f"Round {i + 2} of 9: vote on a proposal or revise one.", kind="status")
    codex.send("hidden behind a full page", thread=tid)
    assert len(claude.poll(tid, limit=3)["messages"]) == 3  # a full page wakes it
    assert claude.poll(tid, limit=3)["messages"][0]["from"] == "codex"


def test_concurrent_waits_share_one_result(env, no_spawn):
    bus = env["bus"]
    server, codex, tid = chat(bus)
    call(server, "inbox", {"thread": tid})

    async def go():
        async def post_later():
            await asyncio.sleep(1.2)
            codex.send("news", thread=tid)
        start = time.monotonic()
        a, b, _ = await asyncio.gather(acall(server, "wait", {"thread": tid, "timeout_s": 10}),
                                       acall(server, "wait", {"thread": tid, "timeout_s": 10}), post_later())
        return a, b, time.monotonic() - start

    a, b, took = asyncio.run(go())
    assert took < 5
    assert a["messages"] == b["messages"] and a["messages"][0]["body"].startswith("[peer_data from codex")
    assert [r.get("joined", False) for r in (a, b)] == [False, True]
    # The shared batch was read once; a later wait starts afresh.
    res = call(server, "wait", {"thread": tid, "timeout_s": 1})
    assert res["status"] == "timeout" and "messages" not in res and "joined" not in res


def test_a_timed_out_wait_leaves_a_longer_one_waiting(env, no_spawn):
    bus = env["bus"]
    server, codex, tid = chat(bus)
    call(server, "inbox", {"thread": tid})

    async def go():
        async def post_later():
            await asyncio.sleep(2.5)
            codex.send("late news", thread=tid)
        return await asyncio.gather(acall(server, "wait", {"thread": tid, "timeout_s": 1}),
                                    acall(server, "wait", {"thread": tid, "timeout_s": 10}), post_later())

    short, long, _ = asyncio.run(go())
    assert short["status"] == "timeout"
    assert long["messages"][0]["body"].startswith("[peer_data from codex") and long["joined"]


def test_timeout_reports_peer_progress_without_event_text(env, no_spawn):
    bus = env["bus"]
    server, _, tid = chat(bus)
    old, _ = bus.create_job(tid, "review", {}, None)
    bus.add_event(tid, "turn_start", "round 1", agent="codex", job_id=old["id"])
    job, _ = bus.create_job(tid, "review", {}, None)
    bus.add_event(tid, "turn_start", "round 2", agent="codex", job_id=job["id"])
    bus.add_event(tid, "tool", "read_file secret-plan.md", agent="codex", job_id=job["id"])
    bus.add_event(tid, "api_retry", "529 overloaded", agent="codex", job_id=job["id"])
    bus.add_event(tid, "text", "my draft verdict", agent="codex", job_id=job["id"])
    bus.add_event(tid, "turn_start", "round 2", agent="antigravity", job_id=job["id"])
    bus.add_event(tid, "turn_end", "failed: timeout", agent="antigravity", job_id=job["id"])
    call(server, "inbox", {"thread": tid})
    res = call(server, "wait", {"thread": tid, "timeout_s": 1})
    p = res["progress"]
    assert p["job"]["status"] == "queued"
    codex = p["peers"]["codex"]
    assert codex["state"] == "working" and codex["last_event"] == "text"
    assert codex["tool_calls"] == 1 and codex["api_retries"] == 1
    assert p["peers"]["antigravity"]["state"] == "failed"
    dumped = json.dumps(res)
    assert "secret-plan" not in dumped and "draft verdict" not in dumped and "overloaded" not in dumped
    bus.conn.execute("UPDATE jobs SET status='failed', error='could not post: my unposted proposal' WHERE id=?",
                     (job["id"],))
    p = call(server, "wait", {"thread": tid, "timeout_s": 1})["progress"]
    assert p["job"] == {"status": "failed", "elapsed_s": p["job"]["elapsed_s"]}  # an error can quote a draft
    assert p["peers"]["codex"]["state"] == "stopped"


def test_blind_initiator_sees_no_standings_on_timeout(env, no_spawn):
    bus = env["bus"]
    server = mcp_server.build("claude", bus=bus)
    tid = call(server, "start_discussion", {"question": "Q?", "peers": ["codex"]})["thread"]["id"]
    res = call(server, "wait", {"thread": tid, "timeout_s": 1})
    assert res["status"] == "timeout" and "standings" not in res and "blind" in res
    # The "Discussion started" notice is quiet: delivered on timeout, didn't end the wait.
    assert res["messages"][0]["body"].startswith("Discussion started by claude")
    call(server, "send", {"thread": tid, "kind": "proposal", "body": "mine"})
    res = call(server, "wait", {"thread": tid, "timeout_s": 1})
    assert "standings" in res and "blind" not in res


def test_after_message_id_replays_and_continues(env, no_spawn):
    bus = env["bus"]
    server, codex, tid = chat(bus)
    first = call(server, "wait", {"thread": tid, "timeout_s": 1})
    seen = first["cursor"]
    codex.send("second", thread=tid)
    lost = call(server, "wait", {"thread": tid, "timeout_s": 1})  # say this reply never arrived
    assert lost["cursor"] > seen
    replay = call(server, "wait", {"thread": tid, "timeout_s": 1, "after_message_id": seen})
    assert [m["id"] for m in replay["messages"]] == [m["id"] for m in lost["messages"]]
    # A fresh session told what the caller has seen doesn't deliver it again.
    other = Service(bus, "claude", bus.register_session("claude"))
    assert other.poll(tid, after_message_id=replay["cursor"]) is None
    assert other.inbox(thread=tid)["messages"] == []
    codex.send("third", thread=tid)
    assert [m["body"].splitlines()[1] for m in other.inbox(thread=tid)["messages"]] == ["third"]


def test_after_message_id_never_moves_the_cursor_past_the_thread(env, no_spawn):
    bus = env["bus"]
    _, codex, tid = chat(bus)
    claude = Service(bus, "claude", bus.register_session("claude"))
    assert claude.poll(tid, after_message_id=10**9) is None
    codex.send("new", thread=tid)
    assert len(claude.inbox(thread=tid)["messages"]) == 1


def test_a_reconnecting_client_keeps_its_session(env, no_spawn, monkeypatch):
    bus = env["bus"]
    client, start = os.getppid(), procinfo.start_time(os.getppid())
    sid = bus.register_session("claude", client_pid=client, client_start=start)
    codex = Service(bus, "codex", bus.register_session("codex"))
    tid = codex.send("hello", to="claude")["thread"]["id"]
    assert len(Service(bus, "claude", sid).inbox(thread=tid)["messages"]) == 1
    # While the first server runs (it's this process), a second one is a separate session.
    other = bus.register_session("claude", client_pid=client, client_start=start)
    assert other != sid
    bus.conn.execute("DELETE FROM sessions WHERE id=?", (other,))
    # Its server gone, the same client's next server carries on as that session.
    bus.conn.execute("UPDATE sessions SET pid=?, last_seen=0 WHERE id=?", (dead_pid(), sid))
    again = bus.register_session("claude", client_pid=client, client_start=start, cwd="/elsewhere")
    assert again == sid
    s = bus.session(sid)
    assert s["pid"] == os.getpid() and s["cwd"] == "/elsewhere" and bus.agent_present("claude", sid)
    assert Service(bus, "claude", again).inbox(thread=tid)["messages"] == []  # nothing redelivered
    # Not for another participant, another client, or a shared daemon's sessions.
    bus.conn.execute("UPDATE sessions SET pid=? WHERE id=?", (dead_pid(), sid))
    assert bus.register_session("codex", client_pid=client, client_start=start) != sid
    assert bus.register_session("claude", client_pid=client, client_start="other") != sid
    assert bus.register_session("claude", client_pid=client, client_start=start,
                                rex_reason=procinfo.CODEX_SHARED_DAEMON) != sid


def test_ambiguous_predecessors_start_a_new_session(env, no_spawn):
    bus = env["bus"]
    client, start = os.getppid(), procinfo.start_time(os.getppid())
    a = bus.register_session("claude", client_pid=client, client_start=start)
    b = bus.register_session("claude", client_pid=client, client_start=start)
    bus.conn.execute("UPDATE sessions SET pid=? WHERE id IN (?,?)", (dead_pid(), a, b))
    assert bus.register_session("claude", client_pid=client, client_start=start) not in (a, b)


def test_alive():
    assert procinfo.alive(os.getpid())
    assert not procinfo.alive(dead_pid())


def test_a_replay_pages_by_its_own_cursor(env, no_spawn):
    bus = env["bus"]
    _, codex, tid = chat(bus)
    claude = Service(bus, "claude", bus.register_session("claude"))
    for i in range(4):
        codex.send(f"m{i}", thread=tid)
    everything = claude.inbox(thread=tid)["messages"]  # say this reply was lost
    page = claude.poll(tid, after_message_id=0, limit=2)
    assert [m["id"] for m in page["messages"]] == [m["id"] for m in everything[:2]]
    assert page["cursor"] == everything[1]["id"]  # not the stored cursor, which is further on
    rest = claude.poll(tid, after_message_id=page["cursor"], limit=50)
    assert [m["id"] for m in rest["messages"]] == [m["id"] for m in everything[2:]]


def test_a_blind_replay_does_not_skip_a_hidden_proposal(env, no_spawn):
    bus = env["bus"]
    server = mcp_server.build("claude", bus=bus)
    tid = call(server, "start_discussion", {"question": "Q?", "peers": ["codex"]})["thread"]["id"]
    hidden = bus.post(tid, "codex", "codex's view", kind="proposal")
    notice = bus.post(tid, SYSTEM, "Waiting for claude (initiator) to post for round 1.", kind="status")
    res = call(server, "wait", {"thread": tid, "timeout_s": 1, "after_message_id": hidden["id"]})
    # Still blind: the replay stops at the hidden proposal, like any read.
    assert hidden["id"] not in [m["id"] for m in res.get("messages", [])]
    assert notice["id"] not in [m["id"] for m in res.get("messages", [])]
    assert res["cursor"] < hidden["id"]
    # The bus itself is bounded too, for any reader.
    assert all(m["id"] < hidden["id"] for m in bus.replay("someone", "claude", tid, after=notice["id"]))
    assert bus.cursor("someone", tid) < hidden["id"]
    call(server, "send", {"thread": tid, "kind": "proposal", "body": "mine"})
    later = call(server, "wait", {"thread": tid, "timeout_s": 1, "after_message_id": res["cursor"]})
    assert hidden["id"] in [m["id"] for m in later["messages"]]


def test_a_shorter_concurrent_wait_leaves_unread_mail_to_the_longer_one(env, no_spawn):
    bus = env["bus"]
    server, codex, tid = chat(bus)
    call(server, "inbox", {"thread": tid})

    async def go():
        async def post_soon():
            await asyncio.sleep(0.2)
            bus.post(tid, SYSTEM, "Round 2 of 3: vote on a proposal or revise one.", kind="status",
                     data={"quiet": True})
        return await asyncio.gather(acall(server, "wait", {"thread": tid, "timeout_s": 1}),
                                    acall(server, "wait", {"thread": tid, "timeout_s": 3}), post_soon())

    short, long, _ = asyncio.run(go())
    assert short["status"] == "timeout" and "messages" not in short
    assert long["status"] == "timeout" and [m["body"][:7] for m in long["messages"]] == ["Round 2"]


def test_a_blind_replay_honours_a_checkpoint_before_any_hidden_message(env, no_spawn):
    bus = env["bus"]
    server = mcp_server.build("claude", bus=bus)
    tid = call(server, "start_discussion", {"question": "Q?", "peers": ["codex"]})["thread"]["id"]
    seen = call(server, "wait", {"thread": tid, "timeout_s": 1})["cursor"]
    fresh = Service(bus, "claude", bus.register_session("claude"))  # cursor 0, still blind
    assert fresh.poll(tid, after_message_id=seen, wake_quiet=True) is None  # nothing again
    notice = bus.post(tid, SYSTEM, "Waiting for claude (initiator) to post for round 1.", kind="status")
    assert [m["id"] for m in fresh.poll(tid, after_message_id=seen)["messages"]] == [notice["id"]]


def test_a_fresh_server_given_a_checkpoint_does_not_redeliver_through_wait(env, no_spawn):
    bus = env["bus"]
    server = mcp_server.build("claude", bus=bus)
    tid = call(server, "start_discussion", {"question": "Q?", "peers": ["codex"]})["thread"]["id"]
    seen = call(server, "wait", {"thread": tid, "timeout_s": 1})["cursor"]
    bus.post(tid, "codex", "codex's view", kind="proposal")  # hidden: claude is still blind
    again = mcp_server.build("claude", bus=bus)  # a new session, cursor 0
    res = call(again, "wait", {"thread": tid, "timeout_s": 1, "after_message_id": seen})
    assert res["status"] == "timeout" and "messages" not in res and res["cursor"] == seen


def test_a_thread_blind_again_after_resume_still_bounds_a_replay(env, no_spawn):
    bus = env["bus"]
    server = mcp_server.build("claude", bus=bus)
    tid = call(server, "start_discussion", {"question": "Q?", "peers": ["codex"]})["thread"]["id"]
    hidden = bus.post(tid, "codex", "codex's view", kind="proposal")
    bus.set_thread(tid, status="cancelled")
    later = bus.post(tid, SYSTEM, "Cancelled.", kind="status")
    claude = Service(bus, "claude", bus.register_session("claude"))
    bus.set_thread(tid, status="open")  # resumed before the worker clears blind: blind again
    assert claude.poll(tid, after_message_id=later["id"]) is None
    assert bus.cursor(claude.subscriber, tid) < hidden["id"]
    call(server, "send", {"thread": tid, "kind": "proposal", "body": "mine"})
    assert hidden["id"] in [m["id"] for m in claude.inbox(thread=tid)["messages"]]
