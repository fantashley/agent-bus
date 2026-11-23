"""Resuming threads automatically once the usage limit that parked them resets (issue #26)."""
import asyncio
import json
import secrets
import threading
import time

import pytest

from agent_bus import autoresume, cli, mcp_server
from agent_bus import config as cfg
from agent_bus.db import HUMAN, SYSTEM, Bus
from agent_bus.service import Service
from agent_bus.worker import Worker

BASE = "a" * 40


def run_job(bus, tid):
    assert Worker(bus, bus.jobs(tid)[-1]["id"]).run() == 0


def set_conf(**kw):
    path = cfg.config_path()
    path.write_text(json.dumps({**json.loads(path.read_text()), **kw}))


def parked_review(env, monkeypatch, reviewer="codex"):
    """A review parked because its reviewer is out of usage."""
    bus = env["bus"]
    bus.clear_quota(reviewer)  # an earlier park's block would refuse the request
    monkeypatch.setenv("FAKE_EXHAUSTED", reviewer)
    claude = Service(bus, "claude", bus.register_session("claude"))
    head = secrets.token_hex(20)  # a new change each time, not a repeat of an earlier request
    tid = claude.request_review(reviewer, str(env["repo"]), BASE, head)["thread"]["id"]
    monkeypatch.setattr(Worker, "review_diff", lambda self, refs, interdiff: ("diff", "Diff from base to head:"))
    run_job(bus, tid)
    monkeypatch.delenv("FAKE_EXHAUSTED")
    assert bus.thread(tid)["status"] == "needs_human"
    return tid


def expire(bus, agent):
    bus.conn.execute("UPDATE quota_blocks SET until=? WHERE agent=?", (time.time() - 1, agent))
    bus.conn.execute("UPDATE auto_resumes SET due=?", (time.time() - 1,))


def test_a_review_parked_by_quota_resumes_once_the_limit_resets(env, no_spawn, monkeypatch):
    bus = env["bus"]
    tid = parked_review(env, monkeypatch)
    [row] = autoresume.pending(bus)
    assert row["thread_id"] == tid and json.loads(row["agents"]) == ["codex"]
    assert abs(row["due"] - bus.quota_block("codex")["until"]) < 1  # "try again in 2 hours 5 minutes"
    assert env["waiters"] == [1]
    assert "will resume this automatically once codex has usage again" in bus.history(tid)["messages"][-1]["body"]
    assert autoresume.sweep(bus) == []  # still blocked
    expire(bus, "codex")
    assert autoresume.sweep(bus) == [tid]
    t = bus.thread(tid)
    assert t["status"] == "open" and t["refs"]["auto_resumes"] == 1 and not autoresume.pending(bus)
    assert "Resumed automatically: the usage limit that stopped codex has reset (automatic resume 1 of 3)" \
        in bus.history(tid)["messages"][-1]["body"]
    assert bus.jobs(tid)[-1]["params"]["mode"] == "retry" and len(no_spawn) == 2
    run_job(bus, tid)
    assert bus.thread(tid)["status"] == "resolved"  # the verdict came in
    assert autoresume.sweep(bus) == []  # nothing left to do


def test_a_block_that_was_extended_is_waited_for(env, no_spawn, monkeypatch):
    bus = env["bus"]
    tid = parked_review(env, monkeypatch)
    later = time.time() + 900
    bus.conn.execute("UPDATE auto_resumes SET due=?", (time.time() - 1,))
    bus.conn.execute("UPDATE quota_blocks SET until=?", (later,))
    assert autoresume.sweep(bus) == []
    assert bus.thread(tid)["status"] == "needs_human"
    assert autoresume.pending(bus)[0]["due"] == pytest.approx(later)


def test_concurrent_sweeps_resume_once(env, no_spawn, monkeypatch):
    bus = env["bus"]
    tid = parked_review(env, monkeypatch)
    expire(bus, "codex")
    results, buses = [], [Bus() for _ in range(4)]
    go = threading.Barrier(len(buses))

    def sweep(b):
        go.wait()
        results.extend(autoresume.sweep(b))
    threads = [threading.Thread(target=sweep, args=(b,)) for b in buses]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for b in buses:
        b.close()
    assert results == [tid]
    assert len([j for j in bus.jobs(tid) if j["params"].get("mode") == "retry"]) == 1
    assert sum("Resumed automatically" in m["body"] for m in bus.history(tid)["messages"]) == 1


def test_mcp_calls_and_cli_commands_sweep(env, no_spawn, monkeypatch, capsys):
    bus = env["bus"]
    tid = parked_review(env, monkeypatch)
    assert cli.main(["auto-resume"]) == 0
    assert f"{tid}: resumes after codex has usage again" in capsys.readouterr().out
    expire(bus, "codex")
    server = mcp_server.build("antigravity", bus=bus)
    asyncio.run(server.call_tool("threads", {}))
    assert bus.thread(tid)["status"] == "open"

    tid2 = parked_review(env, monkeypatch, reviewer="antigravity")
    expire(bus, "antigravity")
    assert cli.main(["threads"]) == 0
    assert bus.thread(tid2)["status"] == "open"
    assert cli.main(["auto-resume"]) == 0
    assert "no automatic resumes planned" in capsys.readouterr().out


def test_turned_off_or_out_of_attempts_leaves_it_to_the_human(env, no_spawn, monkeypatch):
    bus = env["bus"]
    set_conf(auto_resume_after_quota=False)
    tid = parked_review(env, monkeypatch)
    assert not autoresume.pending(bus) and env["waiters"] == []
    set_conf(auto_resume_after_quota=True, auto_resume_max=1)
    tid2 = parked_review(env, monkeypatch)
    expire(bus, "codex")
    assert autoresume.sweep(bus) == [tid2]
    monkeypatch.setenv("FAKE_EXHAUSTED", "codex")
    run_job(bus, tid2)  # out of usage again
    assert bus.thread(tid2)["status"] == "needs_human" and not autoresume.pending(bus)  # 1 of 1 used
    monkeypatch.delenv("FAKE_EXHAUSTED")
    # A human resume restarts the count.
    Service(bus, HUMAN).resume(tid2)
    assert bus.thread(tid2)["refs"]["auto_resumes"] == 0
    assert bus.thread(tid)["status"] == "needs_human"


def test_human_resume_or_cancel_replaces_the_plan(env, no_spawn, monkeypatch):
    bus = env["bus"]
    tid = parked_review(env, monkeypatch)
    Service(bus, HUMAN).resume(tid)
    assert not autoresume.pending(bus)
    tid2 = parked_review(env, monkeypatch)
    Service(bus, HUMAN).cancel(tid2)
    assert not autoresume.pending(bus)
    # A plan whose thread moved on meanwhile is dropped, not acted on.
    tid3 = parked_review(env, monkeypatch)
    bus.set_thread(tid3, status="resolved")
    expire(bus, "codex")
    assert autoresume.sweep(bus) == [] and not autoresume.pending(bus)


def test_an_agent_no_longer_available_is_reported(env, no_spawn, monkeypatch):
    bus = env["bus"]
    tid = parked_review(env, monkeypatch)
    set_conf(agents=["claude", "antigravity"])
    expire(bus, "codex")
    assert autoresume.sweep(bus) == []
    assert bus.thread(tid)["status"] == "needs_human" and not autoresume.pending(bus)
    last = bus.history(tid)["messages"][-1]
    assert last["sender"] == SYSTEM and "Could not resume this automatically" in last["body"]


def test_a_discussion_stalled_by_quota_resumes(env, no_spawn, monkeypatch):
    bus = env["bus"]
    monkeypatch.setenv("FAKE_EXHAUSTED", "codex")
    tid = Service(bus, HUMAN).start_discussion("Which DB?", peers=["claude", "codex"])["thread"]["id"]
    run_job(bus, tid)
    monkeypatch.delenv("FAKE_EXHAUSTED")
    assert bus.thread(tid)["status"] == "needs_human"
    assert [json.loads(r["agents"]) for r in autoresume.pending(bus)] == [["codex"]]
    expire(bus, "codex")
    assert autoresume.sweep(bus) == [tid]
    assert bus.jobs(tid)[-1]["params"] == {"mode": "start_later"}
    run_job(bus, tid)
    assert bus.thread(tid)["status"] == "resolved"


def test_a_discussion_stalled_by_format_drift_is_not_resumed(env, no_spawn, monkeypatch):
    bus = env["bus"]
    monkeypatch.setenv("FAKE_SILENT", "codex")
    tid = Service(bus, HUMAN).start_discussion("Which DB?", peers=["claude", "codex"])["thread"]["id"]
    run_job(bus, tid)
    assert bus.thread(tid)["status"] == "needs_human" and not autoresume.pending(bus)


def test_waiter_sweeps_until_nothing_is_planned(env, no_spawn, monkeypatch):
    bus = env["bus"]
    tid = parked_review(env, monkeypatch)
    bus.conn.execute("UPDATE quota_blocks SET until=?", (time.time() + 1.5,))
    bus.conn.execute("UPDATE auto_resumes SET due=?", (time.time() + 1.5,))
    assert autoresume.wait(bus, until=time.time() + 20) == 0  # returns once nothing is planned
    assert bus.thread(tid)["status"] == "open"


@pytest.mark.parametrize("bad", [{"auto_resume_after_quota": "yes"}, {"auto_resume_max": -1},
                                 {"auto_resume_max": 1.5}])
def test_config_is_validated(env, bad):
    set_conf(**bad)
    with pytest.raises(ValueError, match="auto_resume"):
        cfg.Config.load()


# ------------------------------------------------------------- review fixes (t-d44c64)


def test_not_while_the_parking_job_still_runs(env, no_spawn, monkeypatch):
    bus = env["bus"]
    tid = parked_review(env, monkeypatch)
    job = bus.jobs(tid)[-1]["id"]
    bus.conn.execute("UPDATE jobs SET status='running', lease_owner='w', lease_expires=? WHERE id=?",
                     (time.time() + 60, job))
    expire(bus, "codex")
    assert autoresume.sweep(bus) == [] and autoresume.pending(bus)[0]["due"] > time.time()
    bus.conn.execute("UPDATE jobs SET status='done' WHERE id=?", (job,))
    bus.conn.execute("UPDATE auto_resumes SET due=?", (time.time() - 1,))
    assert autoresume.sweep(bus) == [tid]


def test_the_worker_starts_after_the_resume_commits(env, monkeypatch):
    bus = env["bus"]
    from agent_bus import worker
    seen = []
    monkeypatch.setattr(worker, "spawn", lambda jid: seen.append(jid) or 0)
    tid = parked_review(env, monkeypatch)
    seen.clear()

    def spawn(jid):  # what a freshly started worker would see
        other = Bus()
        try:
            seen.append((other.job(jid)["status"], other.thread(tid)["status"]))
        finally:
            other.close()
        return 0
    monkeypatch.setattr(worker, "spawn", spawn)
    expire(bus, "codex")
    assert autoresume.sweep(bus) == [tid]
    assert seen == [("queued", "open")]


def test_a_plan_for_an_earlier_pass_is_dropped(env, no_spawn, monkeypatch):
    bus = env["bus"]
    tid = parked_review(env, monkeypatch)
    t = bus.thread(tid)
    bus.set_thread(tid, round=t["round"] + 1)  # e.g. a follow-up moved it on, then it parked otherwise
    expire(bus, "codex")
    assert autoresume.sweep(bus) == [] and not autoresume.pending(bus)
    assert bus.thread(tid)["status"] == "needs_human"


def test_giving_up_only_drops_the_park_that_failed(env, no_spawn, monkeypatch):
    bus = env["bus"]
    tid = parked_review(env, monkeypatch)
    autoresume.give_up(bus, tid, "an-older-park", "boom")
    assert autoresume.pending(bus) and "boom" not in bus.history(tid)["messages"][-1]["body"]
    park = autoresume.pending(bus)[0]["park_id"]
    autoresume.give_up(bus, tid, park, "boom")
    autoresume.give_up(bus, tid, park, "boom")
    assert not autoresume.pending(bus)
    assert sum("boom" in m["body"] for m in bus.history(tid)["messages"]) == 1


def test_a_lowered_cap_applies_to_plans_already_made(env, no_spawn, monkeypatch):
    bus = env["bus"]
    tid = parked_review(env, monkeypatch)
    set_conf(auto_resume_max=0)
    expire(bus, "codex")
    assert autoresume.sweep(bus) == [] and not autoresume.pending(bus)
    assert bus.thread(tid)["status"] == "needs_human"


def test_a_repo_no_longer_allowed_is_not_resumed(env, no_spawn, monkeypatch):
    bus = env["bus"]
    tid = parked_review(env, monkeypatch)
    set_conf(repos=[])
    expire(bus, "codex")
    assert autoresume.sweep(bus) == []
    assert bus.thread(tid)["status"] == "needs_human"
    assert "REPO_NOT_ALLOWED" in bus.history(tid)["messages"][-1]["body"]


def test_cancel_goes_before_the_cli_sweep(env, no_spawn, monkeypatch):
    bus = env["bus"]
    tid = parked_review(env, monkeypatch)
    expire(bus, "codex")
    jobs = len(bus.jobs(tid))
    assert cli.main(["cancel", tid]) == 0
    assert bus.thread(tid)["status"] == "cancelled" and len(bus.jobs(tid)) == jobs
    assert not autoresume.pending(bus)


def test_a_drifted_peer_with_a_quota_block_is_not_resumed(env, no_spawn, monkeypatch):
    bus = env["bus"]
    monkeypatch.setenv("FAKE_SILENT", "codex")
    tid = Service(bus, HUMAN).start_discussion("Which DB?", peers=["claude", "codex"])["thread"]["id"]
    # Another job finds codex out of usage too: its quota reason hides the drift.
    real = bus.peer_drift

    def drift_then_block(thread_id):
        out = real(thread_id)
        if out and not bus.quota_block("codex"):
            bus.set_quota_block("codex", time.time() + 600, "heuristic", "quota")
        return out
    monkeypatch.setattr(bus, "peer_drift", drift_then_block)
    run_job(bus, tid)
    assert bus.quota_block("codex") and real(tid)  # both reasons are on record
    assert bus.thread(tid)["status"] == "needs_human" and not autoresume.pending(bus)


def test_a_followup_keeps_the_count_and_replaces_the_plan(env, no_spawn, monkeypatch):
    bus = env["bus"]
    tid = parked_review(env, monkeypatch)
    t = bus.thread(tid)
    bus.set_thread(tid, refs=json.dumps({**t["refs"], "auto_resumes": 2}))
    bus.clear_quota("codex")  # the human cleared it, say
    claude = Service(bus, "claude", bus.register_session("claude"))
    head = t["refs"]["head_commit_id"]
    res = claude.request_review("codex", str(env["repo"]), BASE, "c" * 40, prev_head_commit_id=head)
    assert res["thread"]["id"] == tid
    assert bus.thread(tid)["refs"]["auto_resumes"] == 2 and not autoresume.pending(bus)


# ------------------------------------------------------------- review fixes (t-d44c64, pass 2)


def test_a_known_launch_failure_fails_the_job_at_once(env, monkeypatch):
    bus = env["bus"]
    from agent_bus import worker
    monkeypatch.setattr(worker, "spawn", lambda jid: 0)
    tid = parked_review(env, monkeypatch)
    expire(bus, "codex")

    def boom(jid):
        raise OSError("no such file")
    monkeypatch.setattr(worker, "spawn", boom)
    assert autoresume.sweep(bus) == [tid]
    assert bus.jobs(tid)[-1]["status"] == "failed" and not autoresume.pending(bus)
    assert "its worker could not be started" in bus.history(tid)["messages"][-1]["body"]
    monkeypatch.setattr(worker, "spawn", lambda jid: 0)
    assert Service(bus, HUMAN).resume(tid).get("job")  # the retry the notice suggests works now


def test_a_missing_checkout_gives_up_and_the_sweep_goes_on(env, no_spawn, monkeypatch):
    bus = env["bus"]
    tid = parked_review(env, monkeypatch)
    tid2 = parked_review(env, monkeypatch, reviewer="antigravity")
    bus.set_thread(tid, repo=str(env["tmp"] / "gone"))
    expire(bus, "codex")
    expire(bus, "antigravity")
    assert autoresume.sweep(bus) == [tid2]
    assert "Could not resume this automatically" in bus.history(tid)["messages"][-1]["body"]


def test_reading_a_thread_does_not_hold_back_its_resume(env, no_spawn, monkeypatch, capsys):
    bus = env["bus"]
    tid = parked_review(env, monkeypatch)
    expire(bus, "codex")
    assert cli.main(["history", tid]) == 0
    assert bus.thread(tid)["status"] == "open"


def test_a_resume_whose_worker_never_started_says_so_when_reaped(env, monkeypatch):
    """The sweeper died between committing the resume and starting its worker."""
    bus = env["bus"]
    from agent_bus import worker
    monkeypatch.setattr(worker, "spawn", lambda jid: 0)
    tid = parked_review(env, monkeypatch)
    expire(bus, "codex")
    monkeypatch.setattr(autoresume, "launch", lambda *a: None)
    assert autoresume.sweep(bus) == [tid]
    bus.conn.execute("UPDATE jobs SET created_at=created_at-? WHERE status='queued'", (bus.QUEUE_GRACE_S + 1,))
    bus.reap()
    bus.reap()
    notes = [m["body"] for m in bus.history(tid)["messages"] if "worker never started" in m["body"]]
    assert notes == [f"The automatic resume's worker never started. Run `agent-bus resume {tid}` to retry."]


def test_no_launch_failure_notice_for_a_cancelled_job(env, monkeypatch):
    bus = env["bus"]
    from agent_bus import worker
    monkeypatch.setattr(worker, "spawn", lambda jid: 0)
    tid = parked_review(env, monkeypatch)

    def cancelled_then_fails(jid):
        bus.conn.execute("UPDATE jobs SET status='cancelled' WHERE id=?", (jid,))
        raise OSError("gone")
    monkeypatch.setattr(worker, "spawn", cancelled_then_fails)
    expire(bus, "codex")
    assert autoresume.sweep(bus) == [tid]
    assert not any("could not be started" in m["body"] for m in bus.history(tid)["messages"])


def test_only_automatic_resume_jobs_get_the_reap_notice(env, no_spawn):
    bus = env["bus"]
    t = bus.create_thread("review", "g", ["claude", "codex"], "claude")
    bus.create_job(t["id"], "review", {"mode": "first"}, "review:/src/project:auto:backup:x")
    bus.conn.execute("UPDATE jobs SET created_at=created_at-?", (bus.QUEUE_GRACE_S + 1,))
    bus.reap()
    assert not any("never started" in m["body"] for m in bus.history(t["id"])["messages"])


def test_manual_resume_reaps_before_its_own_notice(env, monkeypatch):
    bus = env["bus"]
    from agent_bus import worker
    monkeypatch.setattr(worker, "spawn", lambda jid: 0)
    tid = parked_review(env, monkeypatch)
    expire(bus, "codex")
    monkeypatch.setattr(autoresume, "launch", lambda *a: None)
    assert autoresume.sweep(bus) == [tid]
    bus.conn.execute("UPDATE jobs SET created_at=created_at-? WHERE status='queued'", (bus.QUEUE_GRACE_S + 1,))
    bus.set_thread(tid, status="needs_human")
    Service(bus, HUMAN).resume(tid)
    bodies = [m["body"] for m in bus.history(tid)["messages"]]
    assert "never started" in bodies[-2] and bodies[-1].startswith("Resumed by the human user")


# ------------------------------------------------------------ review fixes (t-c958a2)


def test_an_mcp_call_acts_before_the_sweep(env, no_spawn, monkeypatch):
    bus = env["bus"]
    tid = parked_review(env, monkeypatch)
    expire(bus, "codex")
    server = mcp_server.build("claude", bus=bus)
    asyncio.run(server.call_tool("send", {"thread": tid, "body": "pushed a fix"}))
    bodies = [m["body"] for m in bus.history(tid)["messages"]]
    assert bodies.index("pushed a fix") < next(i for i, b in enumerate(bodies) if "Resumed automatically" in b)


def test_clearing_a_block_early_makes_its_threads_due(env, no_spawn, monkeypatch, capsys):
    bus = env["bus"]
    tid = parked_review(env, monkeypatch)
    assert autoresume.pending(bus)[0]["due"] > time.time() + 3600
    assert cli.main(["agents", "--clear-quota", "codex"]) == 0
    assert f"resumed {tid}" in capsys.readouterr().out
    assert bus.thread(tid)["status"] == "open"
    tid2 = parked_review(env, monkeypatch)
    bus.clear_quota("codex")  # e.g. a successful turn elsewhere lifted it
    assert autoresume.sweep(bus) == [tid2]


def test_one_waiter_at_a_time(env, no_spawn, monkeypatch):
    import fcntl
    bus = env["bus"]
    parked_review(env, monkeypatch)
    path = f"{bus.path}.auto-resume.lock"  # per database
    with open(path, "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        start = time.time()
        assert autoresume.wait(bus, until=time.time() + 30) == 0
        assert time.time() - start < 2  # turned away at once


def test_an_mcp_read_shows_the_thread_resumed(env, no_spawn, monkeypatch):
    bus = env["bus"]
    tid = parked_review(env, monkeypatch)
    expire(bus, "codex")
    server = mcp_server.build("claude", bus=bus)
    res = asyncio.run(server.call_tool("history", {"thread": tid}))
    assert '"status": "open"' in json.dumps(json.loads(res.content[0].text))

