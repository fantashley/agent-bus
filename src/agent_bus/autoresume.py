"""Resuming a thread automatically once the usage limit that parked it resets (issue #26).

A review or discussion that went to ``needs_human`` only because a peer or reviewer ran out
of usage gets a row in ``auto_resumes`` (written in the same transaction that parks it).
Once every block it waited on has expired, the next sweep resumes it as `agent-bus resume`
would, at most once per park: the row is claimed and deleted in the transaction that
reopens the thread, and the resume job's dedupe key is the park's own id, so concurrent
sweeps (the waiter process, MCP calls, CLI commands) can't resume it twice.

Sweeps run from a detached waiter (`agent-bus auto-resume --wait`, started when a thread is
parked) and, lazily, on every agent-bus MCP call and CLI command, which also covers a
waiter lost to a reboot.  ``auto_resume_after_quota`` (default on) turns it off, and
``auto_resume_max`` (default 3) bounds automatic resumes per thread until the human resumes
it themselves.
"""

from __future__ import annotations

import fcntl
import json
import subprocess
import time
from pathlib import Path

from . import config as cfg
from . import quota
from .db import SYSTEM, Bus, BusError

# A waiter sweeps at least this often, in case a block was cleared early.
RECHECK_S = 600


def blocks_for(bus: Bus, config: cfg.Config, t: dict, agents: list[str]) -> dict[str, dict]:
    """The active blocks that stop ``agents`` on thread ``t`` (with its model settings)."""
    out = {}
    for a in agents:
        b = quota.thread_block(bus, config, t, a)
        if b:
            out[a] = b
    return out


def schedule(bus: Bus, config: cfg.Config, t: dict, agents: list[str]) -> dict | None:
    """Plan an automatic resume of ``t``, which is being parked because ``agents`` are out of
    usage.  Call inside the transaction that parks it.  Returns {"due", "agents", "attempt"}
    or None when it won't be resumed automatically (turned off, or out of attempts)."""
    if not config.auto_resume_after_quota or not agents:
        return None
    done = int((t["refs"] or {}).get("auto_resumes") or 0)
    if done >= config.auto_resume_max:
        return None
    blocks = blocks_for(bus, config, t, agents)
    due = max([b["until"] for b in blocks.values()] or [time.time()])
    with bus.tx():
        bus.conn.execute(
            "INSERT INTO auto_resumes (thread_id, agents, due, park_id, guard, created_at) VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(thread_id) DO UPDATE SET agents=excluded.agents, due=excluded.due,"
            " park_id=excluded.park_id, guard=excluded.guard, created_at=excluded.created_at",
            (t["id"], json.dumps(sorted(agents)), due, f"{t['id']}:{time.time():.6f}", guard(t), time.time()))
    return {"due": due, "agents": sorted(agents), "attempt": done + 1}


def guard(t: dict) -> str:
    """What the parked thread was doing: a plan applies only while this hasn't changed (a
    review follow-up, say, moves the round and head on, and parks for its own reasons)."""
    refs = t["refs"] or {}
    return json.dumps([t["round"], refs.get("head_commit_id"), refs.get("revision"), refs.get("reviewer")])


def notice(plan: dict, config: cfg.Config) -> str:
    return (f"agent-bus will resume this automatically once {', '.join(plan['agents'])} "
            f"{'has' if len(plan['agents']) == 1 else 'have'} usage again (about {quota.when(plan['due'])}; "
            f"automatic resume {plan['attempt']} of {config.auto_resume_max}). To stop that, cancel the thread "
            "or set auto_resume_after_quota to false.")


def forget(bus: Bus, thread_id: str) -> None:
    """Drop a planned automatic resume (the human resumed or cancelled the thread)."""
    bus.conn.execute("DELETE FROM auto_resumes WHERE thread_id=?", (thread_id,))


def pending(bus: Bus) -> list[dict]:
    return [dict(r) for r in bus._all("SELECT * FROM auto_resumes ORDER BY due")]


def sweep(bus: Bus, config: cfg.Config | None = None, now: float | None = None,
          exclude: frozenset = frozenset()) -> list[str]:
    """Resume every parked thread whose blocks have all expired (but not those in
    ``exclude``: a command acting on that thread itself goes first).  Returns their ids."""
    now = time.time() if now is None else now
    if bus._one("SELECT 1 FROM auto_resumes WHERE due<=? LIMIT 1", now) is None:
        return []  # the common case: one indexed read
    config = config or cfg.Config.load()
    out = []
    for r in bus._all("SELECT * FROM auto_resumes WHERE due<=? ORDER BY due", now):
        if r["thread_id"] in exclude:
            continue
        try:
            job = resume_one(bus, config, r["thread_id"], now)
        except (BusError, ValueError) as e:  # e.g. AGENT_UNAVAILABLE, a checkout that's gone
            give_up(bus, r["thread_id"], r["park_id"], str(e))
            continue
        except Exception:  # noqa: BLE001 - try this one again next time; go on with the rest
            continue
        if job is not None:
            out.append(r["thread_id"])
            if job.get("created"):
                launch(bus, r["thread_id"], job)
    return out


def give_up(bus: Bus, thread_id: str, park_id: str, why: str) -> None:
    """Drop the plan of this park (not a newer one), with one notice."""
    with bus.tx():
        gone = bus._all("DELETE FROM auto_resumes WHERE thread_id=? AND park_id=? RETURNING thread_id",
                        thread_id, park_id)
        if gone:
            bus.post(thread_id, SYSTEM, f"Could not resume this automatically: {why}. Run "
                     f"`agent-bus resume {thread_id}` to retry.", kind="status")


def launch(bus: Bus, thread_id: str, job: dict) -> None:
    """Start the worker for a resume's job, now that its transaction has committed.  Only
    the sweep that claimed the plan gets here, so there is one launch per resume.  Like
    every other job agent-bus starts, one whose worker never starts (its launcher died
    first, or it couldn't be spawned) is relaunched by later sweeps, a bounded number of
    times (worker.relaunch_stalled, #64); if it never starts, the thread is told so."""
    from . import worker
    worker.launch(bus, job["id"])


def resume_one(bus: Bus, config: cfg.Config, thread_id: str, now: float) -> dict | None:
    """Claim and carry out one due plan in a single transaction.  Returns the resume's job
    ({"id", "created"}; {} with nothing to run), for the caller to launch once it has
    committed, or None if the thread wasn't resumed."""
    from .service import Service  # service imports this module
    with bus.tx():
        row = bus._one("SELECT * FROM auto_resumes WHERE thread_id=? AND due<=?", thread_id, now)
        if row is None:
            return None  # another sweep got here first
        t = bus.thread(thread_id)
        done = int((t["refs"] or {}).get("auto_resumes") or 0)
        if (t["status"] != "needs_human" or not config.auto_resume_after_quota or row["guard"] != guard(t)
                or done >= config.auto_resume_max):
            forget(bus, thread_id)  # moved on, turned off, or out of attempts (the cap may have dropped)
            return None
        still = blocks_for(bus, config, t, json.loads(row["agents"]))
        if still or bus.active_job(thread_id):
            # Blocked again meanwhile, or for longer; or the job that parked it hasn't
            # finished yet: wait for that.
            due = max([b["until"] for b in still.values()] + [now + 30])
            bus.conn.execute("UPDATE auto_resumes SET due=? WHERE thread_id=?", (due, thread_id))
            return None
        forget(bus, thread_id)
        # Reopens the thread and queues its job inside this transaction: the plan is gone
        # only if the resume happened.  The worker starts after the commit.
        return Service(bus, SYSTEM, config=config).auto_resume(thread_id, row["park_id"],
                                                               json.loads(row["agents"]))


def sweep_quietly(bus: Bus, exclude: frozenset = frozenset()) -> None:
    """A lazy sweep from an MCP call or CLI command: never fails the caller.  It also
    relaunches any job whose worker never started (#64)."""
    from . import worker
    for step in (sweep, worker.relaunch_stalled):
        try:
            step(bus, exclude=exclude)
        except Exception:  # noqa: BLE001
            pass


def spawn_waiter() -> None:
    """Start a detached `agent-bus auto-resume --wait`, which sweeps as blocks expire."""
    logs = cfg.state_dir() / "logs"
    logs.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(logs / "auto-resume.log", "ab") as log:
        subprocess.Popen([*cfg.self_command(), "auto-resume", "--wait"], stdin=subprocess.DEVNULL, stdout=log,
                         stderr=subprocess.STDOUT, start_new_session=True, cwd=str(cfg.state_dir()))


def wait(bus: Bus, until: float | None = None) -> int:
    """Sweep whenever the next planned resume falls due, or a queued job's worker would
    need relaunching (a resume's included, #64), until neither is left (or ``until``, for
    tests).  One waiter per database at a time (a lock file next to it): another one
    started meanwhile exits straight away, and leaves the plans to this one."""
    lock_path = Path(f"{bus.path}.auto-resume.lock")
    with open(lock_path, "a") as lock:
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return 0  # another waiter has it
            while until is None or time.time() < until:
                sweep_quietly(bus)
                bus.reap()  # a queued job out of launch attempts is failed, and a resume's says so
                due = [t for t in (bus._one("SELECT MIN(due) AS due FROM auto_resumes")["due"], bus.next_stall())
                       if t is not None]
                if not due:
                    break
                nap = min(max(min(due) - time.time(), 1), RECHECK_S)
                time.sleep(nap if until is None else min(nap, max(until - time.time(), 0)))
            fcntl.flock(lock, fcntl.LOCK_UN)
            # A park that came in just as this one found nothing left may have had its own
            # waiter turned away by the lock: look once more after letting go.
            if (until is not None and time.time() >= until) or bus._one("SELECT 1 FROM auto_resumes LIMIT 1") is None:
                return 0
