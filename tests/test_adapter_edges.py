"""Edges of a turn's lifecycle: an isolation refusal that the deadline overtakes and a
process group that outlives its leader (#120), and a fresh-session retry whose CLI never
launched (#129)."""

import json
import os
import subprocess
import sys
import time

import pytest

from agent_bus import config as cfg
from agent_bus import peers
from agent_bus.db import HUMAN
from agent_bus.peers import Adapter, CodexAdapter, Invocation, PeerSetupError, TurnResult
from agent_bus.service import Service
from agent_bus.worker import Worker

# A CLI that starts a child, in its process group, which ignores SIGTERM, holds a FIFO open
# for writing (EOF on it means the child is dead, even as an unreaped zombie) and doesn't
# hold the adapter's pipes.  The leader then prints one stream event and sleeps; with
# `lead-stays` it ignores SIGTERM too.
FAKE_CLI = r"""
import json, signal, subprocess, sys, time
fifo, mode = sys.argv[1], sys.argv[2]
subprocess.Popen([sys.executable, "-c",
    "import signal, sys, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
    "f = open(sys.argv[1], 'w'); f.write('up'); f.flush(); time.sleep(120)", fifo],
    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(0.5)  # the child is up
if mode == "lead-stays":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
print(json.dumps({"type": "start"}), flush=True)
time.sleep(120)
"""


class Child:
    """The fake CLI's child, watched through its FIFO."""

    def __init__(self, path):
        os.mkfifo(path)
        self.fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        self.seen = b""

    def dead(self) -> bool:
        while True:
            try:
                chunk = os.read(self.fd, 64)
            except BlockingIOError:  # still held open: the child lives
                return False
            if not chunk:
                return bool(self.seen)  # EOF after it was up: dead (before: not started yet)
            self.seen += chunk

    def dead_within(self, seconds: float) -> bool:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self.dead():
                return True
            time.sleep(0.02)
        return self.dead()


class FakeCliAdapter(Adapter):
    """Runs FAKE_CLI; with ``abort`` it refuses the turn on its first event, like copilot's
    MCP check (#100)."""
    agent = "codex"
    default_command = "codex"
    known_events = frozenset({"start"})

    def __init__(self, config, script, fifo, mode, abort):
        super().__init__(config)
        self.script, self.fifo, self.mode, self.abort = script, fifo, mode, abort

    def invocation(self, prompt, resume_id, cwd, add_dirs):
        return Invocation([sys.executable, str(self.script), str(self.fifo), self.mode], external_id="s-1")

    def parse_line(self, obj, st, emit):
        if self.abort:
            st.abort = "PEER_SETUP: a server went live"


@pytest.fixture
def fake_cli(env):
    script = env["tmp"] / "fake_cli.py"
    script.write_text(FAKE_CLI)
    fifo = env["tmp"] / "child.alive"
    child = Child(fifo)
    try:
        yield lambda mode, abort: FakeCliAdapter(cfg.Config.load(), script, fifo, mode, abort), child
    finally:
        os.close(child.fd)


def test_a_refusal_wins_over_a_deadline_that_passes_while_the_cli_is_stopped(env, monkeypatch, fake_cli):
    # The leader ignores SIGTERM, so _killpg waits out its grace period, and the turn's
    # deadline passes meanwhile: the watchdog marks it timed out.  It's still the isolation
    # refusal, so the worker neither retries it nor pins the aborted session (#120).
    monkeypatch.setattr(peers, "KILL_GRACE_S", 2.5)
    make, child = fake_cli
    res = make("lead-stays", abort=True).run("P", str(env["repo"]), 1.0, lambda *a: None)
    assert not res.ok and res.refused and not res.timed_out
    assert res.error.startswith("PEER_SETUP") and not res.text
    assert child.dead_within(0.5)


def test_an_abort_kills_a_child_that_ignores_sigterm_after_the_leader_exits(env, monkeypatch, fake_cli):
    # The leader dies on SIGTERM at once; its child, in the same group, ignores it and is
    # killed when the grace period ends, before the turn returns (#120).
    monkeypatch.setattr(peers, "KILL_GRACE_S", 0.5)
    make, child = fake_cli
    res = make("lead-exits", abort=True).run("P", str(env["repo"]), 30.0, lambda *a: None)
    assert res.refused
    assert child.dead_within(0.5)


def test_a_timed_out_turn_returns_only_once_its_group_is_killed(env, monkeypatch, fake_cli):
    # The watchdog stops a turn that ran out of time.  The leader exits on SIGTERM, and the
    # child doesn't hold the pipes, so the turn's own wait returns at once; the turn still
    # returns only after the watchdog's SIGKILL, or a worker exiting next would leave the
    # child running (#120).
    monkeypatch.setattr(peers, "KILL_GRACE_S", 2.0)
    make, child = fake_cli
    res = make("lead-exits", abort=False).run("P", str(env["repo"]), 1.0, lambda *a: None)
    assert res.timed_out and not res.refused
    assert child.dead_within(0.3)


def test_killpg_returns_at_once_when_the_whole_group_exits(env, monkeypatch):
    monkeypatch.setattr(peers, "KILL_GRACE_S", 5.0)
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    start = time.monotonic()
    peers._killpg(proc)
    assert time.monotonic() - start < 2 and proc.returncode is not None


def test_killpg_on_a_group_that_is_already_gone(env):
    proc = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    proc.wait()
    peers._killpg(proc)  # no error


# ------------------------------------------------------------------ #129


def test_run_says_whether_the_cli_launched(env, monkeypatch):
    ad = CodexAdapter(cfg.Config.load())

    def refuse(*a, **k):
        raise PeerSetupError("PEER_SETUP: no")

    monkeypatch.setattr(ad, "invocation", refuse)
    res = ad.run("P", str(env["repo"]), 30.0, lambda *a: None)
    assert res.refused and not res.launched
    monkeypatch.setattr(ad, "invocation", lambda *a, **k: Invocation([str(env["tmp"] / "no-such-cli")]))
    res = ad.run("P", str(env["repo"]), 30.0, lambda *a: None)
    assert not res.ok and not res.launched and "could not start" in res.error
    assert TurnResult(ok=False).launched  # what every other result says


def turning_worker(bus):
    tid = Service(bus, HUMAN).start_discussion("Q?", peers=["codex"], max_rounds=3)["thread"]["id"]
    w = Worker(bus, bus.jobs(tid)[-1]["id"])
    assert bus.claim_job(w.job["id"], w.owner)
    assert w.turn("codex", "Q?").ok
    return tid, w


def calls(env):
    return [json.loads(line) for line in env["log"].read_text().splitlines()]


def test_a_fresh_retry_refused_before_launch_keeps_the_stored_session(env, no_spawn, monkeypatch):
    """A resume fails, and the fresh retry's setup is refused (#129): no fresh session ran, so
    the stored one stays for a later resume or debrief, and the retry leaves no usage row."""
    bus = env["bus"]
    tid, w = turning_worker(bus)
    stored = bus.peer_session(tid, "codex")
    assert stored
    monkeypatch.setenv("FAKE_RESUME_FAILS", "codex")
    invocation = CodexAdapter.invocation

    def refuse_fresh(self, prompt, resume_id, cwd, add_dirs):
        if resume_id is None:
            raise PeerSetupError("PEER_SETUP: refused")
        return invocation(self, prompt, resume_id, cwd, add_dirs)

    monkeypatch.setattr(CodexAdapter, "invocation", refuse_fresh)
    before = len(calls(env))
    res = w.turn("codex", "Q?", resume=True)
    assert not res.ok and res.refused and not res.launched
    assert len(calls(env)) == before + 1  # the failed resume only
    assert bus.peer_session(tid, "codex") == stored
    rs = bus.usage_rows(0, thread_id=tid)
    assert len(rs) == 2 and all(r["done"] for r in rs)  # the first turn's and the failed resume's


def test_a_fresh_retry_that_cannot_start_keeps_the_stored_session(env, no_spawn, monkeypatch):
    """The fresh retry's subprocess can't be created (#129): the stored session stays."""
    bus = env["bus"]
    tid, w = turning_worker(bus)
    stored = bus.peer_session(tid, "codex")
    monkeypatch.setenv("FAKE_RESUME_FAILS", "codex")
    invocation = CodexAdapter.invocation

    def missing_fresh(self, prompt, resume_id, cwd, add_dirs):
        inv = invocation(self, prompt, resume_id, cwd, add_dirs)
        if resume_id is None:
            inv.argv[0] = str(env["tmp"] / "no-such-cli")
        return inv

    monkeypatch.setattr(CodexAdapter, "invocation", missing_fresh)
    res = w.turn("codex", "Q?", resume=True)
    assert not res.ok and not res.launched and "could not start" in res.error
    assert bus.peer_session(tid, "codex") == stored


def test_a_resume_that_cannot_start_leaves_no_usage_row(env, no_spawn, monkeypatch):
    """A resumed turn whose CLI never started, nor its fresh retry, drops their started usage
    rows (#129), so the session's next resumed turn still gets its own share."""
    bus = env["bus"]
    tid, w = turning_worker(bus)
    invocation = CodexAdapter.invocation

    def missing(self, prompt, resume_id, cwd, add_dirs):
        inv = invocation(self, prompt, resume_id, cwd, add_dirs)
        inv.argv[0] = str(env["tmp"] / "no-such-cli")
        return inv

    monkeypatch.setattr(CodexAdapter, "invocation", missing)
    res = w.turn("codex", "Q?", resume=True)
    assert not res.ok and not res.launched
    assert len(bus.usage_rows(0, thread_id=tid)) == 1
    monkeypatch.setattr(CodexAdapter, "invocation", invocation)
    assert w.turn("codex", "Q?", resume=True).ok
    first, last = bus.usage_rows(0, thread_id=tid)
    assert last["session"] == first["session"] and last["input"] == 20  # its share, not the total
