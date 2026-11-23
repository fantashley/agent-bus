"""Relaunching queued jobs whose detached worker never started (issue #64)."""
import sqlite3
import threading
import time

import pytest

from agent_bus import autoresume, cli, worker
from agent_bus.db import HUMAN, Bus
from agent_bus.service import Service

BASE, HEAD = "a" * 40, "b" * 40


def age(bus, jid):
    """Let the job's latest launch run out of grace without its worker claiming it."""
    bus.conn.execute("UPDATE jobs SET created_at=created_at-?, launched_at=launched_at-? WHERE id=?",
                     (bus.QUEUE_GRACE_S + 1, bus.QUEUE_GRACE_S + 1, jid))


def crashed(monkeypatch, dispatch):
    """Run ``dispatch`` as if its process died after committing the job, before spawning
    its worker.  Returns (dispatch's result, the job id)."""
    real = worker.launch
    monkeypatch.setattr(worker, "launch", lambda *a, **k: True)
    try:
        res = dispatch()
    finally:
        monkeypatch.setattr(worker, "launch", real)
    return res, res["job"]["id"]


def claude(bus):
    return Service(bus, "claude", bus.register_session("claude"))


DISPATCHES = {
    "discussion": lambda env: Service(env["bus"], HUMAN).start_discussion("Q?", peers=["codex"]),
    "review": lambda env: claude(env["bus"]).request_review("codex", str(env["repo"]), BASE, HEAD),
    "review set": lambda env: claude(env["bus"]).request_review(None, str(env["repo"]), BASE, HEAD,
                                                                 reviewers=["codex", "antigravity"]),
}


@pytest.mark.parametrize("path", sorted(DISPATCHES))
def test_a_job_whose_launcher_crashed_is_relaunched(env, no_spawn, monkeypatch, path):
    bus = env["bus"]
    _, jid = crashed(monkeypatch, lambda: DISPATCHES[path](env))
    assert no_spawn == []
    assert worker.relaunch_stalled(bus) == []  # still within its grace period
    age(bus, jid)
    assert bus.reap() == [] and bus.job(jid)["status"] == "queued"  # reaping waits for the relaunches
    assert worker.relaunch_stalled(bus) == [jid]
    j = bus.job(jid)
    assert no_spawn == [jid] and j["status"] == "queued" and j["launches"] == 2
    assert any(e["kind"] == "relaunch" for e in bus.events(j["thread_id"]))


def test_a_manual_resume_that_crashed_is_relaunched(env, no_spawn, monkeypatch):
    bus = env["bus"]
    human = Service(bus, HUMAN)
    tid = human.start_discussion("Q?", peers=["codex"])["thread"]["id"]
    bus.conn.execute("UPDATE jobs SET status='done'")
    bus.set_thread(tid, status="needs_human")
    _, jid = crashed(monkeypatch, lambda: human.resume(tid))
    age(bus, jid)
    assert worker.relaunch_stalled(bus) == [jid] and no_spawn[-1] == jid


def test_an_automatic_resume_that_crashed_is_relaunched(env, no_spawn, monkeypatch):
    """The sweeper died between committing the resume and starting its worker."""
    bus = env["bus"]
    human = Service(bus, HUMAN)
    tid = human.start_discussion("Q?", peers=["codex"])["thread"]["id"]
    bus.conn.execute("UPDATE jobs SET status='done'")
    bus.set_thread(tid, status="needs_human")
    t = bus.thread(tid)
    with bus.tx():
        autoresume.schedule(bus, human.config, t, ["codex"])
    monkeypatch.setattr(autoresume, "launch", lambda *a: None)
    assert autoresume.sweep(bus) == [tid]
    jid = bus.jobs(tid)[-1]["id"]
    age(bus, jid)
    autoresume.sweep_quietly(bus)  # e.g. the next MCP call
    assert no_spawn[-1] == jid and bus.job(jid)["launches"] == 2


def test_cli_and_mcp_sweeps_relaunch(env, no_spawn, monkeypatch, capsys):
    bus = env["bus"]
    _, jid = crashed(monkeypatch, lambda: DISPATCHES["discussion"](env))
    age(bus, jid)
    assert cli.main(["threads"]) == 0
    assert no_spawn == [jid]


def test_cli_command_on_a_thread_leaves_its_job_alone(env, no_spawn, monkeypatch, capsys):
    bus = env["bus"]
    _, jid = crashed(monkeypatch, lambda: DISPATCHES["discussion"](env))
    age(bus, jid)
    assert cli.main(["cancel", bus.job(jid)["thread_id"]]) == 0
    assert no_spawn == [] and bus.job(jid)["status"] == "cancelled"


def test_a_claimed_job_is_not_relaunched(env, no_spawn, monkeypatch):
    bus = env["bus"]
    _, jid = crashed(monkeypatch, lambda: DISPATCHES["discussion"](env))
    assert bus.claim_job(jid, "alive")
    age(bus, jid)
    assert worker.relaunch_stalled(bus) == [] and no_spawn == []


def test_gives_up_after_the_launch_limit(env, no_spawn, monkeypatch):
    """Each worker exits before claiming its job: relaunched until the limit, then failed."""
    bus = env["bus"]
    _, jid = crashed(monkeypatch, lambda: DISPATCHES["discussion"](env))
    for attempt in range(2, bus.MAX_LAUNCHES + 1):
        age(bus, jid)
        assert worker.relaunch_stalled(bus) == [jid] and bus.job(jid)["launches"] == attempt
    age(bus, jid)
    assert worker.relaunch_stalled(bus) == []  # and the job out of attempts is failed
    assert len(no_spawn) == bus.MAX_LAUNCHES - 1  # plus the launch that crashed
    j = bus.job(jid)
    assert j["status"] == "failed" and "never claimed" in j["error"]


def test_a_failed_spawn_writes_off_only_its_own_attempt(env, monkeypatch):
    bus = env["bus"]
    _, jid = crashed(monkeypatch, lambda: DISPATCHES["discussion"](env))
    age(bus, jid)
    attempt = bus.reserve_relaunch(jid)
    assert attempt == 2
    age(bus, jid)
    assert bus.reserve_relaunch(jid) == 3  # a later attempt, meanwhile
    assert not bus.launch_failed(jid, attempt, "stale")  # attempt 2's failure changes nothing
    assert bus.job(jid)["status"] == "queued" and bus.stalled_jobs() == []
    assert bus.launch_failed(jid, 3, "could not start its worker: gone")  # the last one fails the job
    assert bus.job(jid)["status"] == "failed" and "gone" in bus.job(jid)["error"]


def test_concurrent_launchers_relaunch_once(env, monkeypatch):
    bus = env["bus"]
    _, jid = crashed(monkeypatch, lambda: DISPATCHES["discussion"](env))
    age(bus, jid)
    spawned, lock = [], threading.Lock()

    def record(j):
        with lock:
            spawned.append(j)
        return 0
    monkeypatch.setattr(worker, "spawn", record)
    n = 8
    buses = [Bus(bus.path) for _ in range(n)]
    gate = threading.Barrier(n)

    def sweeper(b):
        assert b.stalled_jobs()  # every launcher sees the job stalled...
        gate.wait()
        worker.relaunch_stalled(b)
    threads = [threading.Thread(target=sweeper, args=(b,)) for b in buses]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for b in buses:
        b.close()
    assert spawned == [jid] and bus.job(jid)["launches"] == 2  # ...and exactly one relaunches it


def test_the_waiter_relaunches_a_stalled_job(env, no_spawn, monkeypatch):
    bus = env["bus"]
    _, jid = crashed(monkeypatch, lambda: DISPATCHES["discussion"](env))
    age(bus, jid)
    autoresume.wait(bus, until=time.time() + 0.5)
    assert no_spawn == [jid]


def test_old_database_jobs_get_launch_attempts(env, tmp_path, no_spawn):
    path = tmp_path / "pre64.db"
    Bus(path).close()
    c = sqlite3.connect(path)  # rebuild jobs as it was before #64
    c.executescript("""
      DROP TABLE jobs;
      CREATE TABLE jobs (id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, kind TEXT NOT NULL, params TEXT NOT NULL,
        dedupe_key TEXT, status TEXT NOT NULL, lease_owner TEXT, lease_expires REAL, pid INTEGER, error TEXT,
        created_at REAL NOT NULL, updated_at REAL NOT NULL);
      INSERT INTO jobs VALUES ('j-old','t-x','review','{}','k','queued',NULL,NULL,NULL,NULL,0,0);
    """)
    c.close()
    bus = Bus(path)
    try:
        j = bus.job("j-old")
        assert j["launches"] == 1 and j["launched_at"] is None
        assert bus.reap() == []  # not failed outright...
        assert worker.relaunch_stalled(bus) == ["j-old"] and bus.job("j-old")["launches"] == 2  # ...relaunched
    finally:
        bus.close()


def test_cli_resume_relaunches_its_threads_stalled_job(env, no_spawn, monkeypatch, capsys):
    """The explicit resume goes first (it finds the queued job active), then the sweep."""
    bus = env["bus"]
    _, jid = crashed(monkeypatch, lambda: DISPATCHES["discussion"](env))
    tid = bus.job(jid)["thread_id"]
    bus.set_thread(tid, status="needs_human")
    age(bus, jid)
    assert cli.main(["resume", tid]) == 0
    assert no_spawn == [jid] and bus.thread(tid)["status"] == "open"


def test_a_lazy_sweep_fails_a_job_out_of_attempts(env, no_spawn, monkeypatch, capsys):
    bus = env["bus"]
    _, jid = crashed(monkeypatch, lambda: DISPATCHES["discussion"](env))
    bus.conn.execute("UPDATE jobs SET launches=? WHERE id=?", (bus.MAX_LAUNCHES, jid))
    age(bus, jid)
    autoresume.sweep_quietly(bus)
    assert bus.job(jid)["status"] == "failed" and no_spawn == []


def test_ask_wait_relaunches_until_it_gives_up(env, no_spawn, monkeypatch, capsys):
    """Every worker exits before claiming its job: `ask --wait` doesn't poll forever."""
    bus = env["bus"]
    monkeypatch.setattr(cli.time, "sleep", lambda s: bus.conn.execute(
        "UPDATE jobs SET created_at=created_at-?, launched_at=launched_at-? WHERE status='queued'",
        (bus.QUEUE_GRACE_S + 1, bus.QUEUE_GRACE_S + 1)))
    assert cli.main(["ask", "--with", "codex", "--wait", "Q?"]) == 0
    (job,) = bus.jobs(bus.threads(include_closed=True)[0]["id"])
    assert job["status"] == "failed" and len(no_spawn) == bus.MAX_LAUNCHES


def test_stream_drives_a_watched_stalled_job(env, no_spawn, monkeypatch, capsys):
    bus = env["bus"]
    _, jid = crashed(monkeypatch, lambda: DISPATCHES["discussion"](env))
    tid = bus.job(jid)["thread_id"]
    bus.conn.execute("UPDATE jobs SET launches=? WHERE id=?", (bus.MAX_LAUNCHES - 1, jid))
    age(bus, jid)
    monkeypatch.setattr(cli.time, "sleep", lambda s: age(bus, jid))
    assert cli.main(["stream", tid, "--interval", "0"]) == 0
    assert no_spawn == [jid] and "JOB_FAILED" in capsys.readouterr().out


def test_stream_reports_a_job_out_of_attempts_when_it_starts(env, no_spawn, monkeypatch, capsys):
    bus = env["bus"]
    _, jid = crashed(monkeypatch, lambda: DISPATCHES["discussion"](env))
    bus.conn.execute("UPDATE jobs SET launches=? WHERE id=?", (bus.MAX_LAUNCHES, jid))
    age(bus, jid)
    assert cli.main(["stream", bus.job(jid)["thread_id"], "--interval", "0"]) == 0
    assert "JOB_FAILED" in capsys.readouterr().out


def test_cancel_cancels_a_job_out_of_attempts_cleanly(env, no_spawn, monkeypatch, capsys):
    bus = env["bus"]
    _, jid = crashed(monkeypatch, lambda: DISPATCHES["discussion"](env))
    bus.conn.execute("UPDATE jobs SET launches=? WHERE id=?", (bus.MAX_LAUNCHES, jid))
    age(bus, jid)
    assert cli.main(["cancel", bus.job(jid)["thread_id"]]) == 0
    assert bus.job(jid)["status"] == "cancelled"


def test_tail_relaunches_a_stalled_job(env, no_spawn, monkeypatch, capsys):
    bus = env["bus"]
    _, jid = crashed(monkeypatch, lambda: DISPATCHES["discussion"](env))
    monkeypatch.setattr(autoresume, "sweep_quietly", lambda *a, **k: None)  # only tail's own loop
    age(bus, jid)
    assert cli.main(["tail", bus.job(jid)["thread_id"], "--once"]) == 0
    assert no_spawn == [jid]
