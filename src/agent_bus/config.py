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

# The CLI harnesses agent-bus knows how to drive, as data: the CLI each one runs as,
# and the app name it reports in Rex's OSC 7501 program status.  Adapters, CLI discovery
# and versioning, hooks and isolation recipes belong to a harness.  Which of them a
# machine actually has is decided per machine (see Config.availability).
HARNESS_INFO = {
    "claude": {"cli": "claude", "app": "claude-code", "name": "Claude Code",
               "efforts": ("low", "medium", "high", "xhigh", "max")},
    "codex": {"cli": "codex", "app": "codex", "name": "Codex",
              "efforts": ("minimal", "low", "medium", "high", "xhigh")},
    "antigravity": {"cli": "agy", "app": "antigravity", "name": "Antigravity",
                    "efforts": ("low", "medium", "high", "xhigh", "max")},
}
HARNESSES = tuple(HARNESS_INFO)
# Before named participants (#43) a harness was also the only identity; kept as aliases.
AGENT_INFO = HARNESS_INFO
# The default participants: one per harness, with the harness's own name.  Configured
# participants (``participants`` in config.json) come on top; see Config.participants.
AGENTS = HARNESSES

# A participant's name is its identity: sender, mailbox, roster entry, reviewer.
PARTICIPANT_NAME = re.compile(r"[a-z][a-z0-9-]{0,31}")
RESERVED_NAMES = frozenset({"human", "system"})
# What a participant entry may set.  Launch settings (everything but ``harness``) default
# to the harness's own ``peers.<harness>`` entry.
PARTICIPANT_KEYS = frozenset({"harness", "model", "effort", "command", "args", "web_search", "allow_mcp", "account"})
# The account a participant's usage counts against, when it isn't the harness's default
# one (a wrapper command or args that sign in elsewhere).  Scopes usage blocks (#26, #43).
_ACCOUNT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@+-]{0,63}")

_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@\[\]-]{0,99}")


@dataclass(frozen=True)
class Participant:
    """A named, separately addressable agent: an identity on the bus, run by a harness
    with its own launch settings (#43)."""
    name: str
    harness: str
    # Launch settings: the harness's ``peers`` entry with the participant's own on top.
    settings: dict = field(default_factory=dict, compare=False, hash=False)

    @property
    def default(self) -> bool:
        """One of the built-in participants named after their harness."""
        return self.name == self.harness


def check_peer_settings(harness: str, settings: dict | None, who: str | None = None) -> dict:
    """Validate a {model, effort} mapping for a participant run by ``harness`` (named
    ``who`` in errors; the harness by default); returns only the keys that are set.
    Model names are restricted to a safe character set, and effort to the values that
    harness's CLI accepts."""
    who = who or harness
    if settings is None:
        return {}
    if not isinstance(settings, dict):
        raise ValueError(f"settings for {who} must be an object with model and/or effort")
    extra = set(settings) - {"model", "effort"}
    if extra:
        raise ValueError(f"unknown setting(s) for {who}: {', '.join(sorted(extra))} (expected model, effort)")
    out = {}
    model, effort = settings.get("model"), settings.get("effort")
    if model is not None:
        if not isinstance(model, str) or not _MODEL.fullmatch(model):
            raise ValueError(f"invalid model name for {who}: {model!r}")
        out["model"] = model
    if effort is not None:
        allowed = HARNESS_INFO[harness]["efforts"]
        if effort not in allowed:
            raise ValueError(f"invalid effort for {who}: {effort!r}; expected one of {', '.join(allowed)}")
        out["effort"] = effort
    return out


def _check_launch(where: str, harness: str, entry: dict, who: str) -> None:
    """Reject a bad launch setting in a ``peers`` or ``participants`` entry when loading."""
    check_peer_settings(harness, {k: entry.get(k) for k in ("model", "effort")}, who)
    for key in ("web_search", "allow_mcp"):
        if not isinstance(entry.get(key, True), bool):
            raise ValueError(f"{where}.{key} must be true or false")
    if "command" in entry and not (isinstance(entry["command"], str) and entry["command"]):
        raise ValueError(f"{where}.command must be a non-empty string")
    if "args" in entry and not (isinstance(entry["args"], list) and all(isinstance(a, str) for a in entry["args"])):
        raise ValueError(f"{where}.args must be a list of strings")
    account = entry.get("account")
    if account is not None and not (isinstance(account, str) and _ACCOUNT.fullmatch(account)):
        raise ValueError(f"{where}.account must be a short label (letters, digits and ._@+-), got {account!r}")


DEFAULTS = {
    "repos": [],
    "max_rounds": 4,
    "deadline_minutes": 30,
    "turn_timeout_s": 600,
    "initiator_grace_s": 180,
    "quota_cooldown_s": 900,
    "usage_warn_at": 0.8,
    "auto_resume_after_quota": True,
    "auto_resume_max": 3,
    "max_messages": 60,
    "max_diff_bytes": 200_000,
    "peers": {},
    "participants": {},
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
    # Allow-listed repos: a path, or {"path": ..., "web_search": false} to keep peers off the
    # web while they work in it (issue #54).
    repos: list = field(default_factory=list)
    max_rounds: int = 4
    deadline_minutes: int = 30
    turn_timeout_s: int = 600
    initiator_grace_s: int = 180
    # How long an out-of-usage agent is skipped when its CLI gave no reset time (#26).
    quota_cooldown_s: int = 900
    # Warn that an agent is close to its usage limit once a window is this full (#26).
    usage_warn_at: float = 0.8
    # Resume a thread parked by a usage limit once it resets, at most this many times (#26).
    auto_resume_after_quota: bool = True
    auto_resume_max: int = 3
    max_messages: int = 60
    max_diff_bytes: int = 200_000
    # Per-agent overrides: {"claude": {"command": "/path/claude", "args": [...]}}
    peers: dict = field(default_factory=dict)
    # Named participants (#43): {"codex-mini": {"harness": "codex", "model": "...", ...}}.
    # The built-in names (claude, codex, antigravity) are always participants too.
    participants: dict = field(default_factory=dict)
    # Participants available on this machine.  None means detect them (see availability).
    agents: list | None = None

    @classmethod
    def load(cls) -> "Config":
        data = dict(DEFAULTS)
        path = config_path()
        if path.exists():
            data.update(json.loads(path.read_text()))
        known = {k: data[k] for k in DEFAULTS}
        conf = cls(**known)
        w = conf.usage_warn_at
        if isinstance(w, bool) or not isinstance(w, (int, float)) or not 0 < w <= 1:
            raise ValueError("usage_warn_at must be a fraction above 0 and at most 1, e.g. 0.8")
        if not isinstance(conf.auto_resume_after_quota, bool):
            raise ValueError("auto_resume_after_quota must be true or false")
        if isinstance(conf.auto_resume_max, bool) or not isinstance(conf.auto_resume_max, int) \
                or conf.auto_resume_max < 0:
            raise ValueError("auto_resume_max must be a whole number, 0 or more")
        conf._repo_entries()  # reject a malformed repos entry when loading
        if not isinstance(conf.peers, dict):
            raise ValueError("config `peers` must be an object keyed by harness")
        for agent, entry in conf.peers.items():  # reject a bad model/effort when loading
            if agent in HARNESS_INFO and isinstance(entry, dict):
                _check_launch(f"peers.{agent}", agent, entry, agent)
        named = conf._resolve_participants()
        for key in conf.peers:
            if key not in HARNESS_INFO and key in named:
                raise ValueError(f"peers.{key}: `peers` is keyed by harness; set {key}'s launch settings "
                                 f"under participants.{key}")
        return conf

    def _resolve_participants(self) -> dict[str, Participant]:
        """Every participant: the built-in one per harness, then the configured ones
        (sorted by name).  Validated as a whole, so a bad entry fails when loading."""
        raw = self.participants if self.participants is not None else {}
        if not isinstance(raw, dict):
            raise ValueError("config `participants` must be an object keyed by participant name")
        out: dict[str, Participant] = {}
        for h in HARNESSES:
            entry = raw[h] if h in raw else {}
            out[h] = self._participant(h, entry)
        for name in sorted(raw):
            if name not in out:
                out[name] = self._participant(name, raw[name])
        return out

    def _participant(self, name: object, entry: object) -> Participant:
        where = f"participants.{name}"
        if not isinstance(name, str) or not PARTICIPANT_NAME.fullmatch(name) or name in RESERVED_NAMES:
            raise ValueError(f"invalid participant name {name!r}: use 1-32 lowercase letters, digits and "
                             "hyphens, starting with a letter (not human or system)")
        if not isinstance(entry, dict):
            raise ValueError(f"{where} must be an object")
        extra = set(entry) - PARTICIPANT_KEYS
        if extra:
            raise ValueError(f"unknown key(s) in {where}: {', '.join(sorted(extra))} "
                             f"(expected {', '.join(sorted(PARTICIPANT_KEYS))})")
        harness = entry.get("harness", name if name in HARNESS_INFO else None)
        if not isinstance(harness, str) or harness not in HARNESS_INFO:
            raise ValueError(f"{where}.harness must be one of {', '.join(HARNESSES)}, got {harness!r}")
        if name in HARNESS_INFO and harness != name:
            raise ValueError(f"participant {name!r} is the {name} harness's own name, so its harness must be {name}")
        if "allow_mcp" in entry and harness != "antigravity":
            raise ValueError(f"{where}.allow_mcp applies only to the antigravity harness")
        _check_launch(where, harness, entry, name)
        base = self.peers.get(harness) if isinstance(self.peers.get(harness), dict) else {}
        own = {k: v for k, v in entry.items() if k != "harness"}
        return Participant(name, harness, {**base, **own})

    def participant_map(self) -> dict[str, Participant]:
        # Resolved on every call (it's a handful of entries): tests and callers may change
        # ``peers`` after loading.
        return self._resolve_participants()

    def participant(self, name: str, role: str = "participant") -> Participant:
        """The participant called ``name``; ValueError for a name nobody configured."""
        found = self.participant_map().get(name) if isinstance(name, str) else None
        if found is None:
            raise ValueError(f"unknown {role} {name!r}; expected one of {', '.join(self.participant_map())}")
        return found

    def is_participant(self, name: object) -> bool:
        return isinstance(name, str) and name in self.participant_map()

    def harness_of(self, name: str) -> str:
        return self.participant(name).harness

    def launch(self, name: str) -> dict:
        """``name``'s launch settings (command, args, model, effort, web_search, ...)."""
        return self.participant(name).settings

    def peer_command(self, agent: str, default: str | None = None) -> str:
        p = self.participant(agent)
        return os.path.expanduser(p.settings.get("command", default or HARNESS_INFO[p.harness]["cli"]))

    def availability(self) -> dict[str, dict]:
        """For each participant: whether this machine has it, and why.

        With ``agents`` set in the config, that list of participant names is
        authoritative.  Otherwise a participant is available when its harness's CLI
        (its ``command``, the harness's ``peers.<harness>.command``, or the default name
        on PATH) can be found.  Being signed in isn't checked.
        """
        out = {}
        names = self.participant_map()
        if self.agents is not None:
            if not isinstance(self.agents, list):
                raise ValueError(f"config `agents` must be a list of participant names, got {type(self.agents).__name__}")
            unknown = [a for a in self.agents if not isinstance(a, str) or a not in names]
            if unknown:
                raise ValueError(f"unknown participant(s) in config `agents`: {', '.join(map(str, unknown))}; "
                                 f"known: {', '.join(names)}")
            for a in names:
                on = a in self.agents
                out[a] = {"available": on, "reason": "listed in config `agents`" if on
                          else "not listed in config `agents`"}
            return out
        for a in names:
            cmd = self.peer_command(a)
            found = shutil.which(cmd)
            out[a] = {"available": bool(found),
                      "reason": f"found {found}" if found else f"`{cmd}` not found on PATH"}
        return out

    def available_agents(self) -> list[str]:
        return [a for a, v in self.availability().items() if v["available"]]

    def require_available(self, agent: str, role: str = "agent") -> None:
        info = self.availability().get(agent) if isinstance(agent, str) else None
        if info is None:
            raise ValueError(f"unknown {role} {agent!r}; expected one of {', '.join(self.participant_map())}")
        if not info["available"]:
            from .db import BusError
            raise BusError("AGENT_UNAVAILABLE", f"{agent} isn't available on this machine ({info['reason']}); "
                           f"available: {', '.join(self.available_agents()) or 'none'}")

    def check_settings(self, agent: str, settings: dict | None) -> dict:
        """check_peer_settings for participant ``agent`` (by its harness's rules)."""
        return check_peer_settings(self.harness_of(agent), settings, agent)

    def peer_settings(self, agent: str, override: dict | None = None) -> dict:
        """Model and effort for ``agent``'s headless turns: its participant entry (on top of
        config ``peers.<harness>``), then a per-thread override on top.  Empty means the
        CLI's own defaults."""
        base = self.launch(agent)
        merged = {k: base[k] for k in ("model", "effort") if base.get(k) is not None}
        merged.update(self.check_settings(agent, override))
        return self.check_settings(agent, merged)

    def web_search(self, agent: str, repo: str | os.PathLike | None = None) -> bool:
        """Whether ``agent``'s headless turns may search the web (issue #46): on unless
        config sets ``web_search = false`` for it (or for its harness in ``peers``), or for
        an allow-listed ``repos`` entry that covers ``repo`` (issue #54)."""
        if repo is not None and not self.repo_web_search(repo):
            return False
        return self.launch(agent).get("web_search", True) is not False

    def peer_args(self, agent: str) -> list[str]:
        return list(self.launch(agent).get("args", []))

    def account(self, agent: str) -> str | None:
        """The account label ``agent``'s usage counts against, if configured."""
        return self.launch(agent).get("account") or None

    def _repo_entries(self) -> list[dict]:
        """``repos`` as {"path", "web_search"?} entries: a bare string is a path."""
        if not isinstance(self.repos, list):
            raise ValueError("config `repos` must be a list of paths or {path, web_search} objects")
        out = []
        for i, entry in enumerate(self.repos):
            if isinstance(entry, str):
                entry = {"path": entry}
            where = f"repos[{i}]"
            if not isinstance(entry, dict):
                raise ValueError(f"{where} must be a path or an object with `path` (and `web_search`)")
            extra = set(entry) - {"path", "web_search"}
            if extra:
                raise ValueError(f"unknown key(s) in {where}: {', '.join(sorted(extra))} (expected path, web_search)")
            if not isinstance(entry.get("path"), str) or not entry["path"]:
                raise ValueError(f"{where}.path must be a non-empty string")
            if not isinstance(entry.get("web_search", True), bool):
                raise ValueError(f"{where}.web_search must be true or false")
            out.append(entry)
        return out

    def _covering(self, repo: str | os.PathLike) -> list[dict]:
        """The ``repos`` entries that ``repo`` is, or is inside."""
        real = os.path.realpath(os.path.expanduser(str(repo)))
        out = []
        for entry in self._repo_entries():
            root = os.path.realpath(os.path.expanduser(entry["path"]))
            if real == root or real.startswith(root.rstrip(os.sep) + os.sep):
                out.append(entry)
        return out

    def repo_allowed(self, repo: str | os.PathLike) -> bool:
        return bool(self._covering(repo))

    def repo_web_search(self, repo: str | os.PathLike) -> bool:
        """False when any allow-listed entry covering ``repo`` turns peer web search off:
        an entry can only turn it off, so the most restrictive one wins (issue #54)."""
        return all(e.get("web_search", True) for e in self._covering(repo))

    def web_search_off_repos(self) -> list[str]:
        """The allow-listed paths whose peers may not search the web, as configured."""
        return [e["path"] for e in self._repo_entries() if e.get("web_search") is False]


def _is_bare_git(d: Path) -> bool:
    return (d.name != ".git" and (d / "HEAD").is_file() and (d / "objects").is_dir()
            and ((d / "refs").is_dir() or (d / "reftable").is_dir()))


def vcs(path: str | os.PathLike) -> str | None:
    """Which tool manages the nearest repo containing ``path``: "jj" when its
    root has a ``.jj`` directory and jj is installed (colocated jj+git repos
    count), "git" for a git repo (including bare repos, or a colocated repo
    without jj installed), None outside any repo.  Decided from the filesystem,
    so a plain git repo never spawns jj."""
    p = Path(os.path.realpath(os.path.expanduser(str(path))))
    for d in (p, *p.parents):
        has_jj = (d / ".jj").is_dir()
        has_git = (d / ".git").exists() or _is_bare_git(d)
        if has_jj or has_git:
            if has_jj and shutil.which("jj"):
                return "jj"
            return "git" if has_git else None
    return None


def repo_root(path: str | os.PathLike) -> str:
    """Return the jj or git root containing ``path``, or ``path`` itself."""
    path = os.path.realpath(os.path.expanduser(str(path)))
    kind = vcs(path)
    if kind == "jj":
        cmds = (["jj", "root", "--ignore-working-copy"],)
    elif kind == "git":
        cmds = (["git", "rev-parse", "--show-toplevel"],
                ["git", "rev-parse", "--is-bare-repository", "--absolute-git-dir"])
    else:
        return path
    for cmd in cmds:
        try:
            out = subprocess.run(cmd, cwd=path, capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if out.returncode == 0 and out.stdout.strip():
            lines = out.stdout.strip().splitlines()
            if "--is-bare-repository" in cmd:
                if len(lines) == 2 and lines[0] == "true":
                    return lines[1]
            else:
                return lines[0]
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
