"""Paths and user configuration.

Configuration lives in ``$XDG_CONFIG_HOME/agent-bus/config.json`` (Home Manager
writes it); every key is optional.  ``AGENT_BUS_CONFIG`` and ``AGENT_BUS_DB``
override the locations, which the tests rely on.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

AGENTS = ("claude", "codex", "antigravity")

DEFAULTS = {
    "repos": [],
    "max_rounds": 4,
    "deadline_minutes": 30,
    "turn_timeout_s": 600,
    "initiator_grace_s": 180,
    "max_messages": 60,
    "max_diff_bytes": 200_000,
    "peers": {},
}


def _xdg(var: str, fallback: str) -> Path:
    value = os.environ.get(var)
    return Path(value) if value else Path.home() / fallback


def state_dir() -> Path:
    return _xdg("XDG_STATE_HOME", ".local/state") / "agent-bus"


def config_path() -> Path:
    env = os.environ.get("AGENT_BUS_CONFIG")
    if env:
        return Path(env)
    return _xdg("XDG_CONFIG_HOME", ".config") / "agent-bus" / "config.json"


def db_path() -> Path:
    env = os.environ.get("AGENT_BUS_DB")
    return Path(env) if env else state_dir() / "bus.db"


@dataclass
class Config:
    repos: list[str] = field(default_factory=list)
    max_rounds: int = 4
    deadline_minutes: int = 30
    turn_timeout_s: int = 600
    initiator_grace_s: int = 180
    max_messages: int = 60
    max_diff_bytes: int = 200_000
    # Per-agent overrides: {"claude": {"command": "/path/claude", "args": [...]}}
    peers: dict = field(default_factory=dict)

    @classmethod
    def load(cls) -> "Config":
        data = dict(DEFAULTS)
        path = config_path()
        if path.exists():
            data.update(json.loads(path.read_text()))
        known = {k: data[k] for k in DEFAULTS}
        return cls(**known)

    def peer_command(self, agent: str, default: str) -> str:
        return self.peers.get(agent, {}).get("command", default)

    def peer_args(self, agent: str) -> list[str]:
        return list(self.peers.get(agent, {}).get("args", []))

    def repo_allowed(self, repo: str | os.PathLike) -> bool:
        real = os.path.realpath(repo)
        for allowed in self.repos:
            root = os.path.realpath(os.path.expanduser(allowed))
            if real == root or real.startswith(root.rstrip(os.sep) + os.sep):
                return True
        return False


def repo_root(path: str | os.PathLike) -> str:
    """Return the jj (or git) root containing ``path``, or ``path`` itself."""
    path = os.path.realpath(os.path.expanduser(str(path)))
    for cmd in (["jj", "root", "--ignore-working-copy"], ["git", "rev-parse", "--show-toplevel"]):
        try:
            out = subprocess.run(cmd, cwd=path, capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    return path


def self_command() -> list[str]:
    """How to re-invoke this program (used to spawn workers and viewers)."""
    env = os.environ.get("AGENT_BUS_BIN")
    if env:
        return [env]
    argv0 = sys.argv[0] if sys.argv else ""
    if os.path.basename(argv0).startswith("agent-bus") and os.access(argv0, os.X_OK):
        return [os.path.abspath(argv0)]
    found = shutil.which("agent-bus")
    if found:
        return [found]
    return [sys.executable, "-m", "agent_bus"]
