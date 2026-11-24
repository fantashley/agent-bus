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
PARTICIPANT_KEYS = frozenset({"harness", "model", "effort", "command", "args", "web_search", "allow_mcp", "account",
                              "codex_home", "env"})
# The account a participant's usage counts against, when it isn't the harness's default
# one (a wrapper command or args that sign in elsewhere).  Scopes usage blocks (#26, #43).
_ACCOUNT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@+-]{0,63}")

# Up to 256 characters: a Bedrock inference-profile ARN with a long model id runs past 100
# (arn:aws:bedrock:<region>:<account>:inference-profile/global.anthropic.…-v1:0), #36.
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@\[\]-]{0,255}")

# A peer's own environment variables (``env`` in a peers or participants entry, #36): non-secret
# settings such as AWS_PROFILE, AWS_REGION and CLAUDE_CODE_USE_BEDROCK.  config.json is not a
# place for credentials (Home Manager puts it in the Nix store), so obvious secrets are refused:
# this catches common mistakes, it is not a guarantee.
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SECRET_NAMES = frozenset({
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_SECURITY_TOKEN",
    "AWS_BEARER_TOKEN_BEDROCK", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
    "OPENAI_API_KEY", "CODEX_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"})
_SECRET_NAME = re.compile(r"(?:^|_)(?:SECRET|PASSWORD|PASSWD|TOKEN|API_?KEY|ACCESS_KEY|PRIVATE_KEY)(?:_|$)",
                          re.IGNORECASE)
_SECRET_VALUE = re.compile(r"(?:AKIA|ASIA)[A-Z0-9]{12,}|sk-[A-Za-z0-9_-]{16,}|AIza[0-9A-Za-z_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}"
                           r"|-----BEGIN ")
# A configured value that is safe to quote in a notice (an AWS profile name).
ENV_SAFE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@+-]{0,63}")
_AWS_REGION = re.compile(r"[a-z]{2}(?:-[a-z]+)+-\d+")
# Set by agent-bus itself, or by a launch setting of its own.  PATH too: agent-bus finds and
# fingerprints the CLI on its own PATH, so the turn must launch the same one (use `command`).
_RESERVED_ENV = re.compile(r"AGENT_BUS_.*|CODEX_HOME|PATH")


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
    if "env" in entry:
        _check_env(where, entry["env"])
    if "codex_home" in entry:
        home = entry["codex_home"]
        if harness != "codex":
            raise ValueError(f"{where}.codex_home applies only to the codex harness")
        if not (isinstance(home, str) and home and os.path.isabs(os.path.expanduser(home))):
            raise ValueError(f"{where}.codex_home must be an absolute path (or start with ~), got {home!r}")


def _check_env(where: str, env: object) -> None:
    """A launch setting's ``env``: names and string values, no secrets.  Errors name the
    variable, never its value."""
    if not isinstance(env, dict):
        raise ValueError(f"{where}.env must be an object of variable names to string values")
    for name, value in env.items():
        if not isinstance(name, str) or not _ENV_NAME.fullmatch(name):
            raise ValueError(f"{where}.env: invalid variable name {name!r}")
        if _RESERVED_ENV.fullmatch(name):
            hint = {"CODEX_HOME": " (use codex_home)", "PATH": " (set command to the CLI's path)"}.get(name, "")
            raise ValueError(f"{where}.env.{name} is set by agent-bus itself{hint}")
        if name.upper() in _SECRET_NAMES or _SECRET_NAME.search(name):
            raise ValueError(f"{where}.env.{name} looks like a credential: config.json must not hold secrets. "
                             "Point at a profile instead (AWS_PROFILE, an SSO session or credential_process)")
        if not isinstance(value, str) or "\0" in value:
            raise ValueError(f"{where}.env.{name} must be a string")
        if _SECRET_VALUE.search(value):
            raise ValueError(f"{where}.env.{name} looks like it holds a credential: config.json must not hold "
                             "secrets")
        if name in ("AWS_REGION", "AWS_DEFAULT_REGION") and not _AWS_REGION.fullmatch(value):
            raise ValueError(f"{where}.env.{name} is not an AWS region name like us-east-1")


def _truthy(value: str | None) -> bool:
    """How Claude Code reads a switch such as CLAUDE_CODE_USE_BEDROCK."""
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def _home(path: str | None) -> str | None:
    """A configured ``codex_home`` as Codex is given it: only ``~`` expanded, since a lexical
    cleanup of ``..`` could pick another directory than a symlinked path really is."""
    return os.path.expanduser(path) if path else None


# Claude Code's own config directory holds its first-party login.
_CREDENTIAL_FILES = ("AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE", "CLAUDE_CONFIG_DIR", "HOME")


def _credential_key(env: dict) -> tuple:
    """What picks a Claude participant's credentials, as configured: Bedrock or not, which
    AWS profile, the AWS files that define it, and Claude Code's config directory."""
    return (_truthy(env.get("CLAUDE_CODE_USE_BEDROCK")), env.get("AWS_PROFILE") or None,
            *(env.get(k) or None for k in _CREDENTIAL_FILES))


def _same_home(path: str | None) -> str | None:
    """Where a ``codex_home`` really is, for telling whether two name the same home."""
    return os.path.realpath(os.path.expanduser(path)) if path else None


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
    "review_followup_budget": 3,
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
    # Author follow-up requests on a review thread before agent-bus warns that the review is
    # running long (#71); a review still blocked after twice as many waits for the human.
    review_followup_budget: int = 3
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
        b = conf.review_followup_budget
        if isinstance(b, bool) or not isinstance(b, int) or b < 1:
            raise ValueError("review_followup_budget must be a whole number, 1 or more")
        conf._repo_entries()  # reject a malformed repos entry when loading
        if not isinstance(conf.peers, dict):
            raise ValueError("config `peers` must be an object keyed by harness")
        for agent, entry in conf.peers.items():  # reject a bad model/effort when loading
            if agent in HARNESS_INFO and isinstance(entry, dict):
                _check_launch(f"peers.{agent}", agent, entry, agent)
        named = conf._resolve_participants()
        conf._check_codex_homes(named)
        conf._check_claude_credentials(named)
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
        merged = {**base, **own}
        if isinstance(base.get("env"), dict) or isinstance(own.get("env"), dict):
            # Variable by variable: a participant adds to (or overrides) its harness's env.
            merged["env"] = {**(base.get("env") or {}), **(own.get("env") or {})}
        return Participant(name, harness, merged)

    def _check_codex_homes(self, named: dict[str, Participant]) -> None:
        """Usage readings come from a participant's CODEX_HOME but are kept per harness and
        account (#66), so a home tells logins apart: a participant with another home than its
        harness's needs an account of its own, participants sharing such a home share an
        account, and no account label is used from two homes."""
        base = self.peers.get("codex") if isinstance(self.peers.get("codex"), dict) else {}
        # The harness's home as configured, not the worker's $CODEX_HOME: validation must not
        # depend on which process loads the config.
        default = _same_home(base.get("codex_home"))
        harness_account = base.get("account") or None
        home_of: dict[str | None, tuple[str, str | None]] = {}  # account -> (participant, home)
        account_of: dict[str, tuple[str, str | None]] = {}  # non-default home -> (participant, account)
        for name, p in named.items():
            if p.harness != "codex":
                continue
            home = _same_home(p.settings.get("codex_home"))
            home = None if home == default else home  # None: the harness's own home
            account = p.settings.get("account") or None
            # Its own label: one inherited from peers.codex names the harness's login, and may
            # have blocks stored under it already.
            if home is not None and (account is None or account == harness_account):
                raise ValueError(f"participants.{name}.codex_home is another Codex home than the codex harness's, "
                                 f"so it signs in to another account: set participants.{name}.account to a label "
                                 "of its own (usage limits are tracked per account)")
            if home is not None and home in account_of and account_of[home][1] != account:
                raise ValueError(f"participants {account_of[home][0]} and {name} share codex_home {home} but name "
                                 "different accounts")
            if account in home_of and home_of[account][1] != home:
                raise ValueError(f"participants {home_of[account][0]} and {name} use different Codex homes but the "
                                 f"same account {account or '(the default)'}: give each login its own account label")
            if home is not None:
                account_of.setdefault(home, (name, account))
            home_of.setdefault(account, (name, home))

    def _check_claude_credentials(self, named: dict[str, Participant]) -> None:
        """Like codex homes (#66): which provider and AWS profile a Claude participant uses
        tells logins apart (#36), and blocks (out of usage, expired credentials) are kept per
        harness and account.  So a participant whose configured CLAUDE_CODE_USE_BEDROCK or
        AWS_PROFILE differs from its harness's needs an account of its own, and one account
        label never names two credential selections."""
        base = self.peers.get("claude") if isinstance(self.peers.get("claude"), dict) else {}
        default = _credential_key(base.get("env") or {})
        harness_account = base.get("account") or None
        seen: dict[str | None, tuple[str, tuple]] = {}  # account -> (participant, credentials)
        for name, p in named.items():
            if p.harness != "claude":
                continue
            creds = _credential_key(p.settings.get("env") or {})
            account = p.settings.get("account") or None
            if creds != default and (account is None or account == harness_account):
                raise ValueError(f"participants.{name}.env selects other credentials than the claude harness's "
                                 f"(CLAUDE_CODE_USE_BEDROCK, AWS_PROFILE, AWS config files, CLAUDE_CONFIG_DIR or HOME), so set participants.{name}.account to a "
                                 "label of its own (usage limits and expired credentials are tracked per account)")
            if account in seen and seen[account][1] != creds:
                raise ValueError(f"participants {seen[account][0]} and {name} use different credentials "
                                 f"(CLAUDE_CODE_USE_BEDROCK, AWS_PROFILE, AWS config files, CLAUDE_CONFIG_DIR or HOME) but "
                                 "the same account "
                                 f"{account or '(the default)'}: give each its own account label")
            seen.setdefault(account, (name, creds))

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

    def codex_home(self, agent: str) -> str | None:
        """The CODEX_HOME ``agent``'s turns run with, if configured (#66); None leaves the
        child the worker's own (``$CODEX_HOME``, else ``~/.codex``)."""
        return _home(self.launch(agent).get("codex_home"))

    def peer_env(self, agent: str) -> dict[str, str]:
        """The environment variables ``agent``'s turns get on top of the worker's own (#36)."""
        return dict(self.launch(agent).get("env") or {})

    def effective_env(self, agent: str, name: str) -> str | None:
        """``name`` as ``agent``'s CLI is given it: its configured env, else the worker's.
        (A CLI may still set it from its own settings, which agent-bus doesn't read.)"""
        env = self.peer_env(agent)
        return env[name] if name in env else os.environ.get(name)

    def bedrock(self, agent: str) -> bool:
        """Whether ``agent`` runs Claude Code on Amazon Bedrock, as far as agent-bus can tell."""
        return self.harness_of(agent) == "claude" and _truthy(self.effective_env(agent, "CLAUDE_CODE_USE_BEDROCK"))

    def aws_region(self, agent: str) -> str | None:
        return self.effective_env(agent, "AWS_REGION") or self.effective_env(agent, "AWS_DEFAULT_REGION") or None

    def aws_profile(self, agent: str) -> str | None:
        return self.effective_env(agent, "AWS_PROFILE") or None

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


def _launched_from() -> str | None:
    """The resolved path of the installed `agent-bus` script this process started as, if any."""
    argv0 = sys.argv[0] if sys.argv else ""
    name = os.path.basename(argv0)
    if name == ".agent-bus-wrapped":
        # Nix's wrapProgram: bin/agent-bus sets PATH and execs bin/.agent-bus-wrapped, which
        # is what argv[0] names (#89).  The wrapper is the installed script.
        argv0 = os.path.join(os.path.dirname(argv0), "agent-bus")
    elif not name.startswith("agent-bus"):
        return None
    return os.path.realpath(argv0) if os.access(argv0, os.X_OK) else None


def _on_path() -> str | None:
    """The `agent-bus` a new shell would run, resolved.  When started through a Nix wrapper,
    skips this build's own bin directory: the wrapper put it first on this process's PATH,
    which would hide an upgrade (#89).  Any other launch takes PATH as it is."""
    wrapped = bool(sys.argv) and os.path.basename(sys.argv[0]) == ".agent-bus-wrapped"
    own = os.path.dirname(_LAUNCHED_FROM) if _LAUNCHED_FROM and wrapped else None
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if not d or (own and os.path.realpath(d) == own):
            continue
        exe = os.path.join(d, "agent-bus")
        if os.path.isfile(exe) and os.access(exe, os.X_OK):
            return os.path.realpath(exe)
    return None


# Resolved once at startup: a link argv[0] went through (a profile's bin/agent-bus) may move
# to another build while this process keeps running the one it started with.
_LAUNCHED_FROM = _launched_from()


def self_command() -> list[str]:
    """How to re-invoke this program (used to spawn workers and viewers)."""
    env = os.environ.get("AGENT_BUS_BIN")
    if env:
        return [env]
    # The build this process started as, while it's still installed (cleanup can remove it).
    if _LAUNCHED_FROM and os.access(_LAUNCHED_FROM, os.X_OK):
        return [_LAUNCHED_FROM]
    found = shutil.which("agent-bus")
    if found:
        return [found]
    return [sys.executable, "-m", "agent_bus"]


def stale_build() -> dict | None:
    """When this process runs a different agent-bus install than the `agent-bus` on PATH.

    After an upgrade, a long-lived MCP server keeps running the build it started with (and
    spawns its workers from that build) until its client restarts it.  Compares the path this
    process started from, resolved at startup, with PATH's `agent-bus` resolved now; None when
    they match or either can't be told (not started as an installed `agent-bus` script, or
    none on PATH).
    """
    on_path = _on_path() if _LAUNCHED_FROM else None
    if not on_path or _LAUNCHED_FROM == on_path:
        return None
    return {"running": _LAUNCHED_FROM, "on_path": on_path,
            "hint": "This agent-bus MCP server is a different build from the agent-bus on PATH "
                    "(likely started before an upgrade), and the review workers it starts use it "
                    "too. Restart it: /reload-plugins in Claude Code, or restart the client."}
