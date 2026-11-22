"""Paths and user configuration.

Configuration lives in ``$XDG_CONFIG_HOME/agent-bus/config.json`` (Home Manager
writes it); every key is optional.  ``AGENT_BUS_CONFIG`` and ``AGENT_BUS_DB``
override the locations, which the tests rely on.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

# The agents agent-bus knows how to drive, as data: the CLI each one runs as, and
# the app name it reports in Rex's OSC 7501 program status.  Which of them a
# machine actually has is decided per machine (see Config.availability).
AGENT_INFO = {
    "claude": {"cli": "claude", "app": "claude-code", "name": "Claude Code",
               "efforts": ("low", "medium", "high", "xhigh", "max")},
    "codex": {"cli": "codex", "app": "codex", "name": "Codex",
              "efforts": ("minimal", "low", "medium", "high", "xhigh")},
    "antigravity": {"cli": "agy", "app": "antigravity", "name": "Antigravity",
                    "efforts": ("low", "medium", "high", "xhigh", "max")},
}

_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@\[\]-]{0,99}")


def check_peer_settings(agent: str, settings: dict | None) -> dict:
    """Validate a {model, effort} mapping for ``agent``; returns only the keys that are set.
    Model names are restricted to a safe character set, and effort to the values that
    agent's CLI accepts."""
    if settings is None:
        return {}
    if not isinstance(settings, dict):
        raise ValueError(f"settings for {agent} must be an object with model and/or effort")
    extra = set(settings) - {"model", "effort"}
    if extra:
        raise ValueError(f"unknown setting(s) for {agent}: {', '.join(sorted(extra))} (expected model, effort)")
    out = {}
    model, effort = settings.get("model"), settings.get("effort")
    if model is not None:
        if not isinstance(model, str) or not _MODEL.fullmatch(model):
            raise ValueError(f"invalid model name for {agent}: {model!r}")
        out["model"] = model
    if effort is not None:
        allowed = AGENT_INFO[agent]["efforts"]
        if effort not in allowed:
            raise ValueError(f"invalid effort for {agent}: {effort!r}; expected one of {', '.join(allowed)}")
        out["effort"] = effort
    return out
AGENTS = tuple(AGENT_INFO)

DEFAULTS = {
    "repos": [],
    "max_rounds": 4,
    "deadline_minutes": 30,
    "turn_timeout_s": 600,
    "initiator_grace_s": 180,
    "max_messages": 60,
    "max_diff_bytes": 200_000,
    "peers": {},
    "agents": None,
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
    # Agents available on this machine.  None means detect them (see availability).
    agents: list | None = None

    @classmethod
    def load(cls) -> "Config":
        data = dict(DEFAULTS)
        path = config_path()
        if path.exists():
            data.update(json.loads(path.read_text()))
        known = {k: data[k] for k in DEFAULTS}
        conf = cls(**known)
        for agent, entry in (conf.peers or {}).items():  # reject a bad model/effort when loading
            if agent in AGENT_INFO and isinstance(entry, dict):
                check_peer_settings(agent, {k: entry.get(k) for k in ("model", "effort")})
        return conf

    def peer_command(self, agent: str, default: str | None = None) -> str:
        return os.path.expanduser(self.peers.get(agent, {}).get("command", default or AGENT_INFO[agent]["cli"]))

    def availability(self) -> dict[str, dict]:
        """For each known agent: whether this machine has it, and why.

        With ``agents`` set in the config, that list is authoritative.  Otherwise an
        agent is available when its CLI (``peers.<agent>.command``, or the default
        name on PATH) can be found.  Being signed in isn't checked.
        """
        out = {}
        if self.agents is not None:
            if not isinstance(self.agents, list):
                raise ValueError(f"config `agents` must be a list of agent names, got {type(self.agents).__name__}")
            unknown = [a for a in self.agents if not isinstance(a, str) or a not in AGENT_INFO]
            if unknown:
                raise ValueError(f"unknown agent(s) in config `agents`: {', '.join(map(str, unknown))}; "
                                 f"known: {', '.join(AGENTS)}")
            for a in AGENTS:
                on = a in self.agents
                out[a] = {"available": on, "reason": "listed in config `agents`" if on
                          else "not listed in config `agents`"}
            return out
        for a in AGENTS:
            cmd = self.peer_command(a)
            found = shutil.which(cmd)
            out[a] = {"available": bool(found),
                      "reason": f"found {found}" if found else f"`{cmd}` not found on PATH"}
        return out

    def available_agents(self) -> list[str]:
        return [a for a, v in self.availability().items() if v["available"]]

    def require_available(self, agent: str, role: str = "agent") -> None:
        info = self.availability().get(agent)
        if info is None:
            raise ValueError(f"unknown {role} {agent!r}; expected one of {', '.join(AGENTS)}")
        if not info["available"]:
            from .db import BusError
            raise BusError("AGENT_UNAVAILABLE", f"{agent} isn't available on this machine ({info['reason']}); "
                           f"available: {', '.join(self.available_agents()) or 'none'}")

    def peer_settings(self, agent: str, override: dict | None = None) -> dict:
        """Model and effort for ``agent``'s headless turns: config ``peers.<agent>``,
        then a per-thread override on top.  Empty means the CLI's own defaults."""
        base = self.peers.get(agent, {})
        merged = {k: base[k] for k in ("model", "effort") if base.get(k) is not None}
        merged.update(check_peer_settings(agent, override))
        return check_peer_settings(agent, merged)

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
