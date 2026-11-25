"""Token usage history per agent and thread (issue #26)."""
import json
import time
from pathlib import Path

import pytest

from agent_bus import cli, usage
from agent_bus import config as cfg
from agent_bus.db import HUMAN
from agent_bus.db import turn_share
from agent_bus.service import Service
from agent_bus.worker import Worker

from test_fixtures import replay

STREAMS = Path(__file__).parent / "data" / "streams"


def run_job(bus, tid):
    job = bus.jobs(tid)[-1]
    assert Worker(bus, job["id"]).run() == 0


def rows(bus, tid=None):
    return bus.usage_rows(0, thread_id=tid)


def counts(r):
    return {k: r[k] for k in ("input", "output", "cache_read", "cache_write", "thinking") if r[k] is not None}


# ------------------------------------------------------------- what each CLI reports


def test_resumed_antigravity_turn_counts_only_its_own_steps(env):
    # agy 1.5.13's result.usage on a resumed conversation is its running total (num_turns 2:
    # 17494 in); the turn's own model steps used 2689.
    res = replay(env, "antigravity", STREAMS / "antigravity-resume.jsonl").run(
        "p", str(env["repo"]), 30, lambda k, t: None, resume_id="00000000-0000-4000-8000-000000000001")
    assert res.usage == {"input": 2689, "output": 48, "cache_read": 11284, "thinking": 44}


def test_first_antigravity_turn_sums_its_steps(env):
    res = replay(env, "antigravity", STREAMS / "antigravity-start.jsonl").run(
        "p", str(env["repo"]), 30, lambda k, t: None)
    assert res.usage == {"input": 14805, "output": 661, "cache_read": 10167, "thinking": 578}  # = result.usage


def test_codex_reports_a_running_total(env):
    ad = replay(env, "codex", STREAMS / "codex-resume.jsonl")
    assert ad.session_usage
    res = ad.run("p", str(env["repo"]), 30, lambda k, t: None, resume_id="00000000-0000-4000-8000-000000000001")
    # Includes codex-start's 35031: the session's total, not the turn's.
    # codex's input_tokens (54180) counts its cached input (45184) too: "input" is the rest (#101).
    assert res.usage["input"] == 54180 - 45184 and res.usage["cache_read"] == 45184 and res.usage["cache_write"] == 0


def test_turn_share():
    assert turn_share({"input": 40, "output": 9}, {"input": 25, "output": 4}) == {"input": 15, "output": 5}
    assert turn_share({"input": 40}, None) is None
    assert turn_share({"input": 10}, {"input": 25}) is None  # not the same session's total
    assert turn_share({"output": 3}, {"input": 25}) is None


# ------------------------------------------------------------------- recording


def worker_turns(bus, agents, n=2):
    """``n`` turns for each agent on a fresh discussion: the first in a new session, the
    rest resuming it (as later rounds do)."""
    tid = Service(bus, HUMAN).start_discussion("Which DB?", peers=list(agents))["thread"]["id"]
    w = Worker(bus, bus.jobs(tid)[-1]["id"])
    assert bus.claim_job(w.job["id"], w.owner)
    for i in range(n):
        for a in agents:
            assert w.turn(a, "Which DB?", resume=i > 0).ok
    return tid


def test_each_turn_is_recorded(env, no_spawn):
    bus = env["bus"]
    tid = worker_turns(bus, ("claude", "codex", "antigravity"))
    rs = rows(bus, tid)
    by = {a: [r for r in rs if r["agent"] == a] for a in ("claude", "codex", "antigravity")}
    assert all(len(v) == 2 for v in by.values())
    assert all(r["ok"] == 1 and r["session"] and r["job_id"] for r in rs)
    assert by["claude"][0]["session"] == by["claude"][1]["session"]
    assert [counts(r) for r in by["claude"]] == [{"input": 10, "output": 5, "cache_read": 100, "thinking": 3}] * 2
    # The resumed codex turn reported 40 in so far; its own share is 20.
    assert [counts(r) for r in by["codex"]] == [{"input": 20, "output": 6, "cache_read": 200, "thinking": 4}] * 2
    assert json.loads(by["codex"][1]["split_total"])["input"] == 40
    assert by["claude"][0]["split_total"] is None
    assert [counts(r) for r in by["antigravity"]] == [{"input": 30, "output": 7, "cache_read": 0, "thinking": 5}] * 2
    ends = [e["text"] for e in bus.events(tid) if e["kind"] == "turn_end" and e["agent"] == "codex"]
    assert ends == ["ok · tokens in 220 (200 cached, 20 uncached) out 6 thinking 4"] * 2


def test_a_discussion_records_usage(env, no_spawn):
    bus = env["bus"]
    tid = Service(bus, HUMAN).start_discussion("Which DB?", peers=["claude", "codex"])["thread"]["id"]
    run_job(bus, tid)
    assert {r["agent"] for r in rows(bus, tid)} == {"claude", "codex"}


def test_resumed_session_without_an_earlier_total(env, no_spawn):
    """A session whose earlier total was never recorded: its share isn't known, so the
    row keeps only the running total and turn_end says that's what it is."""
    bus = env["bus"]
    tid = worker_turns(bus, ("codex",), n=1)
    bus.conn.execute("UPDATE turn_usage SET session='elsewhere'")
    w = Worker(bus, bus.jobs(tid)[-1]["id"])
    w.owner = bus.job(w.job["id"])["lease_owner"]
    assert w.turn("codex", "Which DB?").ok
    first, last = rows(bus, tid)
    assert counts(first)["input"] == 20
    assert not counts(last) and json.loads(last["split_total"])["input"] == 40
    ends = [e["text"] for e in bus.events(tid) if e["kind"] == "turn_end"]
    assert "session tokens so far in 440 (400 cached, 40 uncached)" in ends[-1]


def session_turns(bus):
    def turn(total, resumed=True, record=True, close=False):
        row = bus.begin_usage("t-x", "codex", session="s" if resumed else None)
        if close:
            bus.close_usage(row)
        if record:
            return rec(row, total, resumed)
        return row

    def rec(row, total, resumed=True):
        return bus.record_usage("t-x", "codex", None, ok=True, session="s", session_total=total,
                                resumed=resumed, row=row)
    return turn, rec


def test_a_share_needs_the_previous_turns_total(env):
    """A difference across a turn with no total of its own (it reported nothing or its
    write failed) may cover more than this turn: it gets no share, only its total."""
    bus = env["bus"]
    turn, rec = session_turns(bus)
    assert turn({"input": 100, "output": 20}, resumed=False) == {"input": 100, "output": 20}
    assert turn({"input": 140, "output": 22}) == {"input": 40, "output": 2}
    turn(None, record=False, close=True)  # started, reported nothing
    assert turn({"input": 170, "output": 30}) is None
    assert turn({"input": 180, "output": 31}) == {"input": 10, "output": 1}
    # A counter the baseline lacks has no baseline; once both have it, shares resume.
    assert turn({"input": 190, "output": 33, "thinking": 4}) is None
    assert turn({"input": 200, "output": 34, "thinking": 6}) == {"input": 10, "output": 1, "thinking": 2}
    assert sum(r["input"] or 0 for r in rows(bus)) <= 200


def test_overlapping_workers_leave_shares_unknown(env):
    """Turns of one session from workers that overlapped (one lost its lease): neither
    their order nor the next turn's baseline can be trusted."""
    bus = env["bus"]
    turn, rec = session_turns(bus)
    turn({"input": 100}, resumed=False)
    # A starts, B starts, B runs and reports, then A runs and reports.
    a, b = turn(None, record=False), turn(None, record=False)
    assert rec(b, {"input": 130}) is None
    assert rec(a, {"input": 150}) is None
    assert turn({"input": 170}) is None  # B's 130 is behind A's 150
    assert turn({"input": 180}) == {"input": 10}
    # A started before B and may still report: B and the next turn can't trust their baseline.
    a = turn(None, record=False)
    assert turn({"input": 200}) is None
    assert turn({"input": 220}) is None
    assert rec(a, {"input": 205}) is None
    assert turn({"input": 230}) is None  # A ended after 220 was reported: it may have run after it
    assert turn({"input": 240}) == {"input": 10}
    # A stalls, B reports, then A ends without a total: C can't trust B's baseline.
    a = turn(None, record=False)
    turn({"input": 260})
    bus.close_usage(a)
    assert turn({"input": 280}) is None
    assert turn({"input": 290}) == {"input": 10}


def test_a_baseline_that_ended_after_this_turn_started_is_not_used(env):
    """A and B overlap and A finishes first: B's total may not include A's tokens."""
    bus = env["bus"]
    turn, rec = session_turns(bus)
    turn({"input": 100}, resumed=False)
    a = turn(None, record=False)
    b = turn(None, record=False)
    assert rec(a, {"input": 120}) is None  # B started after it
    assert rec(b, {"input": 125}) is None  # A ended after B started
    assert turn({"input": 135}) is None  # A ended after B (the baseline) started
    assert turn({"input": 140}) == {"input": 5}


def test_resumed_antigravity_without_num_turns_or_steps_has_no_count(env, tmp_path):
    lines = [{"event": "init", "conversation_id": "c"},
             {"event": "result", "result": {"conversation_id": "c", "status": "SUCCESS", "response": "ok",
                                            "usage": {"input_tokens": 999}}}]
    fx = tmp_path / "resumed.jsonl"
    fx.write_text("".join(json.dumps(x) + "\n" for x in lines))
    ad = replay(env, "antigravity", fx)
    assert ad.run("p", str(env["repo"]), 30, lambda k, t: None, resume_id="c").usage is None
    assert ad.run("p", str(env["repo"]), 30, lambda k, t: None).usage == {"input": 999}


def test_a_turn_that_never_ended_keeps_later_shares_unknown(env):
    """Its worker died, and its CLI may still be running: no later baseline covers it."""
    bus = env["bus"]
    turn, rec = session_turns(bus)
    turn({"input": 100}, resumed=False)
    turn(None, record=False)
    assert turn({"input": 130}) is None
    assert turn({"input": 140}) is None


def test_a_failed_completion_write_leaves_a_closed_gap(env, no_spawn, monkeypatch):
    bus = env["bus"]
    tid = worker_turns(bus, ("codex",), n=1)
    w = Worker(bus, bus.jobs(tid)[-1]["id"])
    w.owner = bus.job(w.job["id"])["lease_owner"]
    real = bus.record_usage

    def fail(*a, **kw):
        raise RuntimeError("disk full")
    monkeypatch.setattr(bus, "record_usage", fail)
    assert w.turn("codex", "Which DB?").ok
    monkeypatch.setattr(bus, "record_usage", real)
    assert w.turn("codex", "Which DB?").ok  # right after the gap: no share
    assert w.turn("codex", "Which DB?").ok
    shares = [counts(r) for r in rows(bus, tid)]
    assert shares[1] == {} and shares[2] == {} and shares[3]["input"] == 20


def test_sessions_are_pruned_whole(env):
    bus = env["bus"]
    turn, rec = session_turns(bus)
    turn({"input": 100}, resumed=False)
    turn({"input": 120})
    old = time.time() - bus.USAGE_KEEP_S - 1
    bus.conn.execute("UPDATE turn_usage SET created_at=?", (old,))
    turn(None, record=False)  # a turn of it still open: the session stays
    bus.conn.execute("UPDATE turn_usage SET created_at=?", (old,))
    bus.record_usage("t-y", "claude", {"input": 1}, ok=True)  # prunes
    assert len([r for r in rows(bus) if r["agent"] == "codex"]) == 3
    bus.conn.execute("UPDATE turn_usage SET done=1")
    bus.record_usage("t-y", "claude", {"input": 1}, ok=True)
    assert [r["agent"] for r in rows(bus)] == ["claude", "claude"]
    # A later resume of the dropped session has no baseline.
    assert turn({"input": 150}) is None


def test_a_baseline_must_start_after_earlier_turns_ended(env):
    """B captured its total, then A spent tokens and ended, then B's write landed: B's
    total may not cover A's tokens, so C can't use it."""
    bus = env["bus"]
    turn, rec = session_turns(bus)
    turn({"input": 100}, resumed=False)
    a = turn(None, record=False)
    b = turn(None, record=False)
    bus.close_usage(a)
    rec(b, {"input": 130})
    assert turn({"input": 170}) is None
    assert turn({"input": 180}) == {"input": 10}


def test_antigravity_error_result_after_steps_is_partial(env, tmp_path):
    lines = [{"event": "init", "conversation_id": "c"},
             {"event": "step_update", "step_update": {"conversation_id": "c", "step_index": 1, "state": "DONE",
                                                      "step_type": "agent_response",
                                                      "usage": {"input_tokens": 9, "output_tokens": 2}}},
             {"event": "result", "result": {"conversation_id": "c", "status": "ERROR", "error": "boom"}}]
    fx = tmp_path / "error.jsonl"
    fx.write_text("".join(json.dumps(x) + "\n" for x in lines))
    res = replay(env, "antigravity", fx).run("p", str(env["repo"]), 30, lambda k, t: None)
    assert not res.ok and res.usage == {"input": 9, "output": 2} and res.partial_usage


def test_started_rows_are_timed_at_completion(env):
    bus = env["bus"]
    row = bus.begin_usage("t-x", "codex")
    bus.conn.execute("UPDATE turn_usage SET created_at=created_at-3600")
    bus.record_usage("t-x", "codex", None, ok=True, session="s", session_total={"input": 5}, row=row)
    assert usage.report(bus, hours=0.05)["agents"]["codex"]["input"] == 5


def test_antigravity_keeps_finished_steps_usage_when_the_turn_fails(env, tmp_path):
    lines = [{"event": "init", "conversation_id": "c"},
             {"event": "step_update", "step_update": {"conversation_id": "c", "step_index": 1, "state": "DONE",
                                                      "step_type": "agent_response",
                                                      "usage": {"input_tokens": 9, "output_tokens": 2}}}]
    fx = tmp_path / "partial.jsonl"
    fx.write_text("".join(json.dumps(x) + "\n" for x in lines))
    res = replay(env, "antigravity", fx).run("p", str(env["repo"]), 30, lambda k, t: None)
    assert not res.ok and res.usage == {"input": 9, "output": 2} and res.partial_usage
    bus = env["bus"]
    bus.record_usage("t-x", "antigravity", res.usage, ok=False, partial=res.partial_usage)
    assert usage.report(bus)["agents"]["antigravity"]["partial_turns"] == 1
    full = replay(env, "antigravity", STREAMS / "antigravity-start.jsonl").run("p", str(env["repo"]), 30,
                                                                               lambda k, t: None)
    assert not full.partial_usage


def test_failed_turns_without_usage_record_nothing(env, no_spawn, monkeypatch):
    monkeypatch.setenv("FAKE_EXHAUSTED", "codex")
    bus = env["bus"]
    tid = Service(bus, HUMAN).start_discussion("Which DB?", peers=["claude", "codex"])["thread"]["id"]
    run_job(bus, tid)
    assert set(usage.report(bus, thread=tid)["agents"]) == {"claude"}
    # Its turns stay only as started rows, so a later total isn't split across them.
    assert all(r["split_total"] is None and r["input"] is None for r in rows(bus, tid) if r["agent"] == "codex")


def test_old_usage_is_pruned(env):
    bus = env["bus"]
    bus.record_usage("t-x", "claude", {"input": 1}, ok=True)
    bus.conn.execute("UPDATE turn_usage SET created_at=?", (time.time() - bus.USAGE_KEEP_S - 1,))
    bus.record_usage("t-x", "claude", {"input": 2}, ok=True)
    assert [r["input"] for r in rows(bus)] == [2]


# ------------------------------------------------------------------------ report


def test_usage_command(env, no_spawn, capsys):
    bus = env["bus"]
    tid = worker_turns(bus, ("claude", "codex"))
    bus.record_usage("t-gone", "codex", None, ok=True, session_total={"input": 5}, resumed=True)
    old = time.time() - 48 * 3600
    bus.record_usage(tid, "claude", {"input": 1000}, ok=True)
    bus.conn.execute("UPDATE turn_usage SET created_at=? WHERE input=1000", (old,))

    assert cli.main(["usage", "--json"]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["agents"]["claude"] == {"turns": 2, "unreported_turns": 0, "partial_turns": 0, "unsplit_turns": 0,
                                       "unsplit_input": 0, "unsplit_cache_read": 0, "unsplit_cache_write": 0,
                                       "input": 20, "output": 10,
                                       "cache_read": 200, "cache_write": 0, "thinking": 6,
                                       "cached_input": 200, "uncached_input": 20, "input_total": 220}
    assert rep["agents"]["codex"]["turns"] == 3 and rep["agents"]["codex"]["unreported_turns"] == 1
    assert [t["thread"] for t in rep["threads"]] == ["t-gone", tid]  # most recent first
    assert rep["threads"][0]["kind"] is None
    assert rep["threads"][1]["agents"]["codex"]["input"] == 40 and rep["threads"][1]["total"]["turns"] == 4

    assert cli.main(["usage", "--hours", "2160", "--thread", tid, "--agent", "claude", "--json"]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["agents"]["claude"]["input"] == 1020 and list(rep["agents"]) == ["claude"]

    assert cli.main(["usage"]) == 0
    out = capsys.readouterr().out
    assert "by agent:" in out and "claude" in out and "(thread no longer exists)" in out
    assert "CACHED IN" in out and "UNCACHED IN" in out and "cached in 200 uncached in 20" in out
    assert "only a session total: codex 1" in out and f"{tid}  discussion open: Which DB?" in out
    assert cli.main(["usage", "--thread", "t-nope"]) == 2
    assert "NO_SUCH_THREAD" in capsys.readouterr().err


def test_threads_are_ordered_by_their_latest_completion(env):
    bus = env["bus"]
    row = bus.begin_usage("t-codex", "codex")  # starts first, finishes last
    bus.record_usage("t-claude", "claude", {"input": 1}, ok=True)
    bus.conn.execute("UPDATE turn_usage SET created_at=created_at-10")
    bus.record_usage("t-codex", "codex", None, ok=True, session="s", session_total={"input": 2}, row=row)
    rep = usage.report(bus, limit=1)
    assert [t["thread"] for t in rep["threads"]] == ["t-codex"] and rep["more_threads"] == 1


def test_usage_command_with_nothing_recorded(env, capsys):
    assert cli.main(["usage"]) == 0
    assert "none recorded" in capsys.readouterr().out


@pytest.mark.parametrize("args", [["--hours", "0"], ["--hours", "nan"], ["--hours", "inf"], ["--hours", "2161"],
                                  ["--limit", "-1"]])
def test_usage_command_rejects_bad_ranges(env, capsys, args):
    assert cli.main(["usage", *args]) == 2


def test_cached_and_uncached_input_mean_the_same_for_every_cli():
    """#101: each CLI's live usage report (claude 2.1.331, codex-cli 0.172.0, agy 1.5.13, one
    "reply ok" turn each).  Claude and agy report input other than cache reads and writes;
    codex's input_tokens counts its cached input too.  turn_end splits input the same way."""
    from agent_bus.peers import AGY_USAGE, CLAUDE_USAGE, codex_usage, format_usage, usage_from
    claude = usage_from({"input_tokens": 2, "cache_creation_input_tokens": 17135, "cache_read_input_tokens": 9333,
                         "output_tokens": 4}, CLAUDE_USAGE)
    assert format_usage(claude) == " · tokens in 26470 (9333 cached, 17137 uncached) out 4"
    codex = codex_usage({"input_tokens": 18033, "cached_input_tokens": 13952, "cache_write_input_tokens": 0,
                         "output_tokens": 5, "reasoning_output_tokens": 0})
    assert codex["input"] == 4081
    assert format_usage(codex) == " · tokens in 18033 (13952 cached, 4081 uncached) out 5 thinking 0"
    agy = usage_from({"input_tokens": 2420, "output_tokens": 37, "thinking_tokens": 35, "cache_read_tokens": 10297,
                      "total_tokens": 2457}, AGY_USAGE)
    assert format_usage(agy) == " · tokens in 12717 (10297 cached, 2420 uncached) out 37 thinking 35"
    assert format_usage({"output": 3}) == " · tokens out 3"
    assert codex_usage({"input_tokens": 5, "cached_input_tokens": 9})["input"] == 0  # never negative
