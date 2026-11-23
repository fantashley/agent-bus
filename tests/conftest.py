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
