import json
import os
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from agent_bus import config as cfg  # noqa: E402
from agent_bus.db import Bus  # noqa: E402

FAKE = Path(__file__).resolve().parent / "fake_peer.py"


@pytest.fixture(autouse=True)
def no_real_state(tmp_path_factory, monkeypatch):
    """Never the real state and config dirs, even in a test without ``env``: a worker's
    sweep there removed a running review's snapshot (#164)."""
    home = tmp_path_factory.mktemp("xdg")
    monkeypatch.setenv("XDG_STATE_HOME", str(home / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "config"))
    monkeypatch.delenv("AGENT_BUS_DB", raising=False)
    monkeypatch.delenv("AGENT_BUS_CONFIG", raising=False)


@pytest.fixture(autouse=True)
def not_in_agent_shell(monkeypatch):
    """Tests run the human's CLI commands, often from inside an agent's shell: hide the
    harness markers the CLI's guard looks for (#84).  Guard tests put them back."""
    from agent_bus import cli
    for var in (*cli.HARNESS_ENV, "AGENT_BUS_ALLOW_HUMAN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(cli, "harness_ancestor", lambda: None)
    # The provenance the human's commands record (#108): a plain terminal, never the test
    # run's own process chain (which may be an agent's, and holds this machine's paths).
    from agent_bus import provenance
    monkeypatch.setattr(provenance, "ancestry", lambda pid: (CLEAN_CHAIN, False))
    monkeypatch.setattr(provenance, "terminal", lambda: ("/dev/pts/0", True))


# A human's terminal as tests' provenance records see it: a shell under a terminal emulator.
CLEAN_CHAIN = [{"pid": 4120, "argv": "zsh"}, {"pid": 4100, "argv": "terminal"}]


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated state/config dirs with fake peer CLIs wired in."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("AGENT_BUS_DB", raising=False)
    monkeypatch.delenv("AGENT_BUS_CONFIG", raising=False)
    monkeypatch.delenv("REX_SESSION", raising=False)
    monkeypatch.delenv("AGENT_BUS_PEER", raising=False)
    (tmp_path / "agy").mkdir()
    monkeypatch.setenv("AGY_CONFIG_DIR", str(tmp_path / "agy"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))  # rollout files (usage limits)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    peers = {}
    for agent in cfg.AGENTS:
        script = bindir / f"fake-{agent}"
        script.write_text(f"#!/bin/sh\nFAKE_AGENT={agent} exec {sys.executable} {FAKE} \"$@\"\n")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        peers[agent] = {"command": str(script)}
    repo = tmp_path / "repo"
    repo.mkdir()
    conf = tmp_path / "config" / "agent-bus" / "config.json"
    conf.parent.mkdir(parents=True)
    conf.write_text(json.dumps({"peers": peers, "repos": [str(repo)], "initiator_grace_s": 3,
                                "turn_timeout_s": 60}))
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "fake.log"))
    # The detached auto-resume waiter: recorded, never started by tests.
    from agent_bus import autoresume
    waiters = []
    monkeypatch.setattr(autoresume, "spawn_waiter", lambda: waiters.append(1))
    bus = Bus()
    yield {"waiters": waiters, "tmp": tmp_path, "bus": bus, "repo": repo, "log": tmp_path / "fake.log"}
    bus.close()


@pytest.fixture
def no_spawn(monkeypatch):
    """Record dispatched jobs instead of spawning detached workers."""
    spawned = []
    from agent_bus import worker
    monkeypatch.setattr(worker, "spawn", lambda jid: spawned.append(jid) or 0)
    return spawned


@pytest.fixture
def unchecked_commits(monkeypatch):
    """request_review's up-front commit check (#73) gets no definite answer, so a full id the
    repo doesn't have is accepted: tests of the worker's own missing-commit backstop (#42),
    which still catches a commit that is gone by the time the review runs."""
    from agent_bus import worker
    monkeypatch.setattr(worker, "resolve_commit", lambda repo, tools, rev: ("unknown", []))
    monkeypatch.setattr(worker, "is_ancestor", lambda repo, tools, base, head: None)
