"""Headless adapters for the three agent CLIs.

Each adapter builds a read-only command line for a first turn or a resumed
turn, and parses the CLI's streaming JSON into progress events, the final
response text and the external session id used to resume next round.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import config as cfg
from .config import Config
from .db import new_uuid
from .prompts import AGY_HEADLESS, AGY_HEADLESS_NO_WEB

# argv entries above this size go through stdin or a file instead.
MAX_ARG = 100_000


class PeerSetupError(Exception):
    """The peer can't be started safely (e.g. its integrations can't be isolated)."""


@dataclass
class TurnResult:
    ok: bool
    text: str = ""
    external_id: str | None = None
    error: str | None = None
    timed_out: bool = False
    # Token counts from the CLI's own terminal usage report; a counter it didn't give is absent.
    # For an adapter with ``session_usage`` they are the session's running total, not this
    # turn's (the worker works out the turn's share).
    usage: dict | None = None
    # The CLI exited normally but its output lacked what agent-bus needs (a reply, a
    # session id): its format probably changed (issue #32, PEER_FORMAT_CHANGED).
    format_changed: bool = False
    # Set only on a failed turn that the CLI said was out of usage (issue #26, agreed on
    # agent-bus thread t-3e260b): {"resets_at": epoch | None, "source": "structured" |
    # "heuristic"}.
    quota: dict | None = None
    # The CLI's stream explains why a normally exited turn produced no reply.
    incomplete: bool = False
    # Set by the worker for a ``session_usage`` CLI: the session's running total, while
    # ``usage`` becomes this turn's share of it (None when that isn't known).
    session_usage: dict | None = None
    # The account's usage-limit windows as the CLI reported them with this turn (see
    # Adapter.usage_limits); None when it reported none.
    limits: list[dict] | None = None
    # ``usage`` counts only the steps reported before the turn stopped without its final report.
    partial_usage: bool = False
    # The provider refused the model or region (a wrong model id or inference profile, a
    # region without it, no access to it; #36): retrying or repairing can't help.
    config_error: bool = False


@dataclass
class Invocation:
    argv: list[str]
    stdin: str | None = None
    external_id: str | None = None  # known up front (claude --session-id)


@dataclass
class StreamState:
    text_parts: list[str] = field(default_factory=list)
    final: str | None = None
    external_id: str | None = None
    error: str | None = None
    step_text: dict = field(default_factory=dict)  # per-step text, for clients that stream deltas
    usage: dict | None = None
    step_usage: dict | None = None  # Antigravity: this run's steps' token counts, summed
    resumed: bool = False  # this run resumed an existing session
    rollout: tuple | None = None  # Codex: (rollout file, its size) before a resumed turn ran
    rate_limit: dict | None = None  # Claude's latest rate_limit_event info
    rate_limit_at: float | None = None  # when it arrived
    unknown: dict = field(default_factory=dict)  # event type -> count, for types the adapter doesn't know
    stray: int = 0  # non-JSON stdout lines (diagnostics), counted only
    pending: dict = field(default_factory=dict)  # step index -> tool name, for tool steps with no end state
    failed_tools: set = field(default_factory=set)  # names of tool steps that ended in an error
    denied: list = field(default_factory=list)  # actions the CLI reported denying (sanitised labels)
    api_error: str | None = None  # Claude: the last API error's own type ("model_not_found", ...)
    api_status: int | None = None  # and its HTTP status, if it gave one
    # Set by an adapter's parser when the stream shows the turn can't run safely (say, an MCP
    # server it couldn't switch off went live): the CLI is stopped at once and the turn fails.
    abort: str | None = None


Emit = Callable[[str, str], None]  # (kind, text)


class Adapter:
    # The harness this adapter drives (#43).  Each turn runs one participant on it:
    # ``self.participant``, whose launch settings it uses.
    agent: str
    default_command: str
    # Bump when the invocation or what the parser relies on changes, so a version's
    # recorded evidence is tied to the recipe it was gathered with (issue #32).
    recipe: str = "1"
    # Stream event types this adapter understands.  Anything else is counted and
    # reported, never fatal on its own: CLIs add event types often.
    known_events: frozenset = frozenset()
    # What web access a headless turn gets when web search is on (issue #46), and whether
    # turning it off (peers.<agent>.web_search = false) is enforced by the CLI or only asked
    # of the model.  Searches run on the vendor's side, not from this machine.
    web_tools: str = "search and fetch pages"
    web_off_enforced: bool = True
    # The CLI's usage report is the whole session's running total, so a resumed turn's own
    # share is the difference from the session's previous report (issue #26).
    session_usage: bool = False
    # How the discussion preamble describes the web access a turn gets (issue #46).
    web_note: str = " and search the web"
    # Variables removed from the CLI's environment: ones that would widen what a headless
    # turn may do, whatever its flags say.
    drop_env: tuple[str, ...] = ()
    # The headless turn can run read-only shell commands (git), so a review prompt can give
    # it commands to read the pinned head (#93).
    shell: bool = False

    def event_type(self, obj: dict) -> str | None:
        t = obj.get("type")
        return t if isinstance(t, str) else None

    def __init__(self, config: Config, settings: dict | None = None, participant: str | None = None,
                 repo: str | None = None):
        self.config = config
        self.participant = participant or self.agent
        if config.harness_of(self.participant) != self.agent:
            raise ValueError(f"participant {self.participant!r} runs on {config.harness_of(self.participant)}, "
                             f"not {self.agent}")
        self.command = config.peer_command(self.participant, self.default_command)
        self.extra = config.peer_args(self.participant)
        self.settings = settings or {}  # validated {model, effort}; see Config.peer_settings
        # Off for the participant, or for the allow-listed repo the turn runs in (#46, #54).
        self.web_search = config.web_search(self.participant, repo)

    def launch_fingerprint(self, cwd: str | None = None) -> str | None:
        """What isolation evidence for a turn of this participant must match (#32, #43):
        the harness and its recipe, the executable identity, and the launch settings
        that change how the CLI is isolated (its args, MCP and web settings).  Two
        participants on one harness share evidence only when these all match; model and
        effort don't change isolation, so they aren't part of it.  None when the
        executable can't be found: such a launch can't be matched to any evidence."""
        import hashlib
        ident = cli_identity(self.command, cwd)
        if ident is None:
            return None
        launch = self.config.launch(self.participant)
        material = {"harness": self.agent, "recipe": self.recipe, "executable": ident[1],
                    "args": self.extra, "web_search": self.web_search,
                    "allow_mcp": bool(launch.get("allow_mcp"))}
        # Another Codex home is another config.toml (other MCP servers, profiles): only when
        # set, so a participant without one keeps the evidence it had (#66).
        if launch.get("codex_home"):
            material["codex_home"] = self.config.codex_home(self.participant)
        # Its own environment can point the CLI at another config (CLAUDE_CONFIG_DIR, ...): only
        # when set, likewise (#36).
        if self.config.peer_env(self.participant):
            material["env"] = self.config.peer_env(self.participant)
        return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()

    def child_env(self) -> dict[str, str]:
        """Environment variables the participant's launch settings give its CLI: its
        configured ``env`` (#36), on top of the worker's own."""
        return self.config.peer_env(self.participant)

    def setting_args(self) -> list[str]:
        """This CLI's flags for the configured model and effort (none: its defaults)."""
        args = []
        if self.settings.get("model"):
            args += ["--model", self.settings["model"]]
        if self.settings.get("effort"):
            args += ["--effort", self.settings["effort"]]
        return args

    def extra_args(self) -> list[str]:
        """``self.extra`` with flags overridden by explicit ``self.settings`` stripped."""
        drop = set()
        if self.settings.get("model"):
            drop.add("--model")
        if self.settings.get("effort"):
            drop.add("--effort")
        return _strip_flags(self.extra, drop) if drop else list(self.extra)

    def invocation(self, prompt: str, resume_id: str | None, cwd: str, add_dirs: list[str]) -> Invocation:
        raise NotImplementedError

    def parse_line(self, obj: dict, st: StreamState, emit: Emit) -> None:
        raise NotImplementedError

    def before_run(self, st: StreamState, resume_id: str | None) -> None:
        """Note anything usage_limits needs from before the CLI runs."""

    def usage_limits(self, st: StreamState) -> list[dict] | None:
        """How much of each of the account's usage-limit windows is used, as this turn's
        CLI reported it without spending a turn of its own (issue #26): a list of
        {"window", "used" (a fraction), "minutes" (the window's length, if given),
        "resets_at" (epoch, if given), "observed_at" (when the CLI took the reading, if
        it says; else now)}, or None when the CLI reports none.  Only readings from this
        turn count: an older one may be behind a newer one from another session."""
        return None

    def incomplete(self, st: StreamState) -> str | None:
        """Why a normally-exited turn without a reply stopped partway, when the stream
        itself shows it; None leaves it to the format-drift check."""
        return None

    # Provider wording for "out of usage", matched only against a turn that already failed.
    # Kept narrow on purpose: a bare 429, "overloaded", or RESOURCE_EXHAUSTED without
    # quota wording stays an ordinary error.
    quota_patterns: tuple[str, ...] = ()

    def misconfigured(self, st: StreamState, error: str) -> bool:
        """Whether a failed turn was refused because of the model or region it asked for."""
        return False

    def classify(self, st: StreamState, error: str) -> dict | None:
        """Whether a failed turn ran out of usage: structured evidence first, then text."""
        for pat in self.quota_patterns:
            if re.search(pat, error, re.IGNORECASE):
                # The raw error stays in the job's progress events, never in the block.
                return {"resets_at": _reset_from_text(error), "source": "heuristic"}
        return None

    def run(self, prompt: str, cwd: str, timeout: float, emit: Emit, resume_id: str | None = None,
            add_dirs: list[str] | None = None, cancelled: Callable[[], bool] = lambda: False,
            peer_tag: str = "1") -> TurnResult:
        try:
            inv = self.invocation(prompt, resume_id, cwd, add_dirs or [])
        except PeerSetupError as e:
            return TurnResult(ok=False, error=str(e))
        st = StreamState(external_id=inv.external_id, resumed=bool(resume_id))
        try:
            self.before_run(st, resume_id)
        except OSError:
            pass  # only the usage-limit reading depends on it
        # Any agent-bus MCP server the peer CLI launches sees this and refuses every tool,
        # so a headless peer can't post, start jobs, or read blind proposals.
        # drop_env goes last: a participant's configured env can't put one of them back.
        env = {k: v for k, v in {**os.environ, **self.child_env()}.items() if k not in self.drop_env}
        env["AGENT_BUS_PEER"] = peer_tag
        try:
            proc = subprocess.Popen(
                inv.argv, cwd=cwd, stdin=subprocess.PIPE if inv.stdin is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True, env=env)
        except OSError as e:
            return TurnResult(ok=False, error=f"could not start {inv.argv[0]}: {e}")

        # Supervision starts before any I/O, so a peer that never reads stdin or never
        # writes stdout still hits the timeout and cancellation.
        timed_out = False
        killer_done = threading.Event()

        def watchdog() -> None:
            nonlocal timed_out
            end = time.time() + timeout
            while not killer_done.wait(1.0):
                if time.time() > end or cancelled():
                    timed_out = time.time() > end
                    _killpg(proc)
                    return

        threading.Thread(target=watchdog, daemon=True).start()

        stderr_tail: list[str] = []

        def pump_stderr() -> None:
            assert proc.stderr
            for line in proc.stderr:
                stderr_tail.append(line)
                del stderr_tail[:-40]

        threading.Thread(target=pump_stderr, daemon=True).start()

        def feed_stdin() -> None:
            assert proc.stdin
            try:
                proc.stdin.write(inv.stdin or "")
                proc.stdin.close()
            except (BrokenPipeError, OSError, ValueError):
                pass

        if inv.stdin is not None:
            threading.Thread(target=feed_stdin, daemon=True).start()
        assert proc.stdout
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                # Unstructured stdout is diagnostics, never the reply (issue #32).
                st.stray += 1  # counted only: a wrapper's output may hold secrets
                continue
            if isinstance(obj, dict):
                typ = self.event_type(obj)
                if typ not in self.known_events:
                    key = (typ or "(none)")[:40]
                    st.unknown[key] = st.unknown.get(key, 0) + 1
                try:
                    self.parse_line(obj, st, emit)
                except Exception as e:  # never let a format change kill the worker
                    emit("error", f"unparsed event: {e}")
                if st.abort and proc.poll() is None:
                    st.error = st.error or st.abort
                    _killpg(proc)
        rc = proc.wait()
        killer_done.set()
        if not isinstance(st.external_id, str) or not st.external_id.strip():
            st.external_id = None  # only a non-empty string can be pinned and resumed
        if not isinstance(st.final, str):
            st.final = None
        st.text_parts = [p for p in st.text_parts if isinstance(p, str)]
        if st.stray:
            emit("format", f"{st.stray} non-JSON stdout line(s) from {self.participant} (not shown)")
        if st.unknown:
            emit("format", f"unrecognised {self.participant} stream events: "
                 + ", ".join(f"{k}×{n}" for k, n in sorted(st.unknown.items())[:10]))
        text = st.final if st.final is not None else "".join(st.text_parts)
        # Steps that finished before a turn failed, timed out or ended without its result
        # still used their tokens: kept, marked as a partial count.
        partial = st.usage is None and st.step_usage is not None
        st.usage = st.usage or st.step_usage
        try:
            limits = self.usage_limits(st)
        except Exception as e:  # noqa: BLE001 - the windows are a hint; never fail a turn on them
            emit("format", f"could not read {self.participant}'s usage limits: {e!r}"[:300])
            limits = None
        if timed_out:
            return TurnResult(ok=False, text=text, external_id=st.external_id, error="turn timed out",
                              timed_out=True, usage=st.usage, partial_usage=partial, limits=limits)
        if st.abort:
            return TurnResult(ok=False, text="", external_id=st.external_id, error=st.abort, usage=st.usage,
                              partial_usage=partial, limits=limits)
        if rc != 0 or st.error:
            err = st.error or f"exit {rc}: {''.join(stderr_tail)[-800:]}"
            # err already ends with the stderr tail unless the stream reported its own error.
            full = err + "\n" + "".join(stderr_tail)[-800:] if st.error else err
            try:
                quota = self.classify(st, full)
            except Exception:  # noqa: BLE001 - classification is best-effort
                quota = None
            try:
                config_error = not quota and self.misconfigured(st, full)
            except Exception:  # noqa: BLE001
                config_error = False
            return TurnResult(ok=False, text=text, external_id=st.external_id, error=err, usage=st.usage,
                              partial_usage=partial, limits=limits, quota=quota, config_error=config_error)
        # Exited normally: the reply and a session id to resume are both required.
        missing = [what for what, ok in (("a reply", text.strip()), ("a session id", st.external_id)) if not ok]
        # Only the reply missing: the stream itself may show why the turn stopped early.  A
        # missing session id, or a stopped turn nothing explains, stays drift.
        why = self.incomplete(st) if missing == ["a reply"] else None
        if why:
            # No reply text at all: a whitespace-only response must not reach JSON repair.
            return TurnResult(ok=False, text="", external_id=st.external_id, usage=st.usage,
                              partial_usage=partial, limits=limits, incomplete=True, error=f"PEER_TURN_INCOMPLETE: {self.participant} {why}")
        if missing:
            return TurnResult(ok=False, text=text, external_id=st.external_id, usage=st.usage,
                              partial_usage=partial, limits=limits, format_changed=True, error=f"PEER_FORMAT_CHANGED: {self.participant} exited normally without "
                                    + " or ".join(missing))
        return TurnResult(ok=True, text=text, external_id=st.external_id, usage=st.usage, partial_usage=partial, limits=limits)


def cli_identity(command: str, cwd: str | None = None) -> tuple[str, str] | None:
    """(path to launch, identity) of the CLI a turn in ``cwd`` would run.  The identity
    is the resolved real path plus its size and mtime, so a rebuilt or upgraded binary
    is a new identity.  A
    relative command, or a relative PATH entry, is resolved against ``cwd`` as the
    turn's own launch would.  None if it can't be found."""
    base = cwd or os.getcwd()
    if os.path.sep in command:
        found = command if os.path.isabs(command) else os.path.join(base, command)
        found = found if os.access(found, os.X_OK) else None
    else:
        path = os.pathsep.join(e if os.path.isabs(e) else os.path.join(base, e or ".")
                               for e in os.environ.get("PATH", os.defpath).split(os.pathsep))
        found = shutil.which(command, path=path)
    if not found:
        return None
    # Fingerprint the resolved binary, but keep the path the turn would launch: a
    # wrapper may dispatch on its own name or find files relative to it.
    real = os.path.realpath(found)
    try:
        st = os.stat(real)
    except OSError:
        return None
    # Both paths are in the identity: aliases of one binary that dispatch on their
    # name are probed separately.
    return found, f"{found}->{real}:{st.st_size}:{st.st_mtime_ns}"


# A `--version` probe that runs longer than this is abandoned as "unknown" ...
VERSION_PROBE_TIMEOUT_S = 10
# ... and the same binary identity reports "unknown" without probing for this long
# before one caller tries again (#41: a slow cold start must not stick forever).
VERSION_TIMEOUT_COOLDOWN_S = 60


def cli_version(bus, agent: str, command: str, cwd: str | None = None) -> str:
    """The CLI's own ``--version`` line, probed once per binary identity and cached in
    the bus (issue #32).  "unknown" when it can't be found or doesn't say.  A miss is
    probed under a per-binary file lock, so concurrent jobs share one probe and its
    answer.  A probe that times out isn't cached: the identity answers "unknown" at
    once for a cooldown, then the next caller probes again (issue #41)."""
    ident = cli_identity(command, cwd)
    if ident is None:
        return "unknown"
    launch, key = ident

    def settled() -> str | None:
        cached = bus.cli_version(key)
        if cached is not None:
            return cached
        retry_after = bus.cli_version_retry_after(key)
        return "unknown" if retry_after is not None and time.time() < retry_after else None

    found = settled()
    if found is not None:
        return found
    import fcntl
    import hashlib
    lock_dir = Path(bus.path).parent / "locks"  # next to the cache itself
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock = lock_dir / f"version-{hashlib.sha256(key.encode()).hexdigest()[:16]}.lock"
    with open(lock, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        # Another caller may have probed, or timed out, while we waited for the lock.
        found = settled()
        if found is not None:
            return found
        version = "unknown"
        try:
            # errors="replace": a stray undecodable byte (say, a wrapper's diagnostic on
            # stderr) must not stop the turn this probe runs ahead of.  Its own session, so
            # a timeout kills the whole group: a grandchild still holding the pipes (a shim
            # launching the real binary) can't keep this probe, and the lock, waiting (#52).
            proc = subprocess.Popen([launch, "--version"], cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, errors="replace", stdin=subprocess.DEVNULL, start_new_session=True,
                                    env={**os.environ, "AGENT_BUS_PEER": "version-probe"})
        except OSError:
            proc = None
        if proc is not None:
            with proc:
                try:
                    stdout, stderr = proc.communicate(timeout=VERSION_PROBE_TIMEOUT_S)
                except BaseException as e:  # a timeout, a pipe error, or an interrupt
                    # SIGKILL straight away: the wrapper may already be gone, so waiting on
                    # it (as _killpg does) wouldn't escalate for a descendant ignoring SIGTERM.
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    if not isinstance(e, (subprocess.TimeoutExpired, OSError)):
                        raise
                    if isinstance(e, subprocess.TimeoutExpired):
                        bus.set_cli_version_timeout(key, agent, launch, time.time() + VERSION_TIMEOUT_COOLDOWN_S)
                        return "unknown"
                    stdout = stderr = ""
            text = stdout if (stdout or "").strip() else stderr
            line = next((ln.strip() for ln in (text or "").splitlines() if ln.strip()), "")
            if proc.returncode == 0 and line:
                version = "".join(ch for ch in line if ch.isprintable())[:80]
        bus.set_cli_version(key, agent, launch, version)
        return version


def _killpg(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


_UNITS = {}
for _names, _secs in ((("d", "day", "days"), 86400), (("h", "hr", "hrs", "hour", "hours"), 3600),
                      (("m", "min", "mins", "minute", "minutes"), 60), (("s", "sec", "secs", "second", "seconds"), 1),
                      (("ms", "millisecond", "milliseconds"), 0.001)):
    _UNITS.update(dict.fromkeys(_names, _secs))


def _reset_from_text(text: str, now: float | None = None) -> float | None:
    """A reset time the error text states unambiguously: a Unix timestamp after a
    pipe (Claude's "usage limit reached|1736280000") or a relative "try again in 2 hours,
    and 5 minutes".  Wall-clock times like "at 3:05 PM" have no date or zone: not trusted.
    An unknown unit makes the whole duration untrusted."""
    now = time.time() if now is None else now
    m = re.search(r"\|\s*(\d{10})\b", text)
    if m:
        return float(m.group(1))
    term = r"\d+\s*[a-z]+"
    m = re.search(rf"(?:try again|resets?|available again) in ({term}(?:\s*(?:,\s*and|,|and)?\s*{term})*)",
                  text, re.IGNORECASE)
    if m:
        total = 0.0
        for n, unit in re.findall(r"(\d+)\s*([a-z]+)", m.group(1), re.IGNORECASE):
            if unit.lower() == "and":
                continue
            secs = _UNITS.get(unit.lower())
            if secs is None:
                return None
            total += int(n) * secs
        return now + total if total else None
    return None


def _num(v: object) -> int | None:
    return v if type(v) is int and v >= 0 else None


def usage_from(raw: object, keys: dict[str, str]) -> dict | None:
    """Token counters renamed to agent-bus's names ({ours: theirs}); absent ones skipped."""
    if not isinstance(raw, dict):
        return None
    out = {ours: _num(raw.get(theirs)) for ours, theirs in keys.items()}
    out = {k: v for k, v in out.items() if v is not None}
    return out or None


def format_usage(u: dict | None, label: str = "tokens") -> str:
    if not u:
        return ""
    names = (("input", "in"), ("output", "out"), ("cache_read", "cache"), ("thinking", "thinking"))
    return f" · {label} " + " ".join(f"{name} {u[k]}" for k, name in names if k in u)


def _limit(window: object, used: object, minutes: object = None, resets_at: object = None) -> dict | None:
    """One usage-limit window, checked: a short name, a finite fraction used, and the
    optional length and reset time when they're well formed."""
    if not isinstance(window, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", window):
        return None
    if isinstance(used, bool) or not isinstance(used, (int, float)) or not 0 <= used <= 10:
        return None  # also rejects nan
    ok = lambda v: not isinstance(v, bool) and isinstance(v, (int, float)) and 0 < v < 1e11  # noqa: E731
    return {"window": window, "used": float(used), "minutes": int(minutes) if ok(minutes) else None,
            "resets_at": float(resets_at) if ok(resets_at) else None}


def codex_rollout(thread_id: str, home: Path | None = None) -> Path | None:
    """The rollout file Codex keeps for a session, $CODEX_HOME/sessions/Y/M/D/rollout-…-<id>.jsonl
    (codex 0.171.0; a resumed session appends to the file of the day it started)."""
    if not re.fullmatch(r"[0-9a-fA-F-]{8,64}", thread_id or ""):
        return None
    home = home or Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    found = sorted((home / "sessions").glob(f"*/*/*/rollout-*-{thread_id}.jsonl"))
    return found[-1] if found else None


def _iso_time(v: object) -> float | None:
    """Epoch seconds from an ISO 8601 UTC time like codex's "2026-11-23T04:48:28.191Z"."""
    if not isinstance(v, str):
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def codex_rate_limits(path: Path, offset: int = 0) -> tuple[dict, float] | None:
    """(rate_limits, when) of the last token_count event in a Codex rollout file, among
    the records written after byte ``offset`` (the file's size before this turn ran)."""
    found = None
    with open(path, "rb") as f:
        f.seek(offset)
        for raw in f:
            if b'"token_count"' not in raw:
                continue
            try:
                obj = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue  # e.g. a record still being written
            payload = obj.get("payload") if isinstance(obj, dict) else None
            rl = payload.get("rate_limits") if isinstance(payload, dict) else None
            # One with no window at all says nothing: keep an earlier reading of this turn.
            if isinstance(payload, dict) and payload.get("type") == "token_count" and isinstance(rl, dict) \
                    and any(isinstance(rl.get(k), dict) for k in ("primary", "secondary")):
                t = _iso_time(obj.get("timestamp"))
                found = (payload["rate_limits"], t if t is not None else time.time())
    return found


def _short(text: str, n: int = 240) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _drop_codex_feature(args: list[str], feature: str) -> list[str]:
    """Remove ``--enable <feature>`` / ``--enable=<feature>`` from a codex argv slice."""
    out: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            out.extend(args[i:])
            break
        if a in ("-c", "--config"):  # its value is a value, whatever it looks like
            out.extend(args[i : i + 2])
            i += 2
            continue
        if a == "--enable" and i + 1 < len(args) and args[i + 1] == feature:
            i += 2
            continue
        if a == f"--enable={feature}":
            i += 1
            continue
        out.append(a)
        i += 1
    return out


def _codex_overrides(args: list[str]) -> list[str]:
    """The values of every ``-c``/``--config`` override in a codex argv slice.  A separate
    value is consumed with its flag, so one like ``--`` or ``-cx=1`` is never read as a flag."""
    out: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            break
        if a in ("-c", "--config"):
            if i + 1 < len(args):
                out.append(args[i + 1])
            i += 2
            continue
        if a.startswith("--config="):
            out.append(a.split("=", 1)[1])
        elif a.startswith("-c") and len(a) > 2 and not a.startswith("--"):
            out.append(a[3:] if a.startswith("-c=") else a[2:])
        i += 1
    return out


def _codex_options_end(args: list[str]) -> int:
    """Where a codex argv slice's options end: its first ``--`` that isn't a ``-c`` value."""
    i = 0
    while i < len(args):
        if args[i] == "--":
            return i
        i += 2 if args[i] in ("-c", "--config") else 1
    return len(args)


def _drop_bool_flags(args: list[str], flags: set[str]) -> list[str]:
    """Remove valueless ``flags`` from an argv slice, up to a ``--``.  A ``-c``/``--config``
    value is skipped with its flag, so it is never mistaken for one."""
    out: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            out.extend(args[i:])
            break
        if a in ("-c", "--config"):
            out.extend(args[i : i + 2])
            i += 2
            continue
        if a not in flags:
            out.append(a)
        i += 1
    return out


def _codex_split_profile(args: list[str]) -> tuple[list[str], list[str]]:
    """(``--profile`` args, everything else) from a codex argv slice: a profile is a
    top-level option that ``exec resume`` doesn't accept."""
    profile: list[str] = []
    rest: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            rest.extend(args[i:])
            break
        if a in ("-c", "--config"):
            rest.extend(args[i : i + 2])
            i += 2
            continue
        if a in ("-p", "--profile") and i + 1 < len(args):
            profile, i = ["--profile", args[i + 1]], i + 2
            continue
        if a.startswith("--profile=") or (a.startswith("-p") and len(a) > 2 and not a.startswith("--")):
            val = a.split("=", 1)[1] if a.startswith("--profile=") or a.startswith("-p=") else a[2:]
            profile, i = ["--profile", val], i + 1
            continue
        rest.append(a)
        i += 1
    return profile, rest  # the last profile given wins, as in codex


def _codex_config_args(args: list[str]) -> list[str]:
    """The config overrides and profile in a codex argv slice, as ``-c``/``--profile`` args."""
    return _codex_split_profile(args)[0] + [x for o in _codex_overrides(args) for x in ("-c", o)]


def _codex_value(override: str) -> object:
    """An override's value as codex 0.171.0 reads it (utils/cli config_override.rs): trimmed,
    then TOML (comments and all), else the text with surrounding quote characters stripped."""
    import tomllib
    raw = override.split("=", 1)[1].strip()
    try:
        return tomllib.loads(f"v = {raw}")["v"]
    except tomllib.TOMLDecodeError:
        return raw.strip("\"'")


def _config_key(override: str) -> str:
    return override.split("=", 1)[0].strip() if "=" in override else ""


def _strip_flags(args: list[str], flags: set[str], config_keys: set[str] = frozenset()) -> list[str]:
    """Remove options in ``flags`` (``--flag val``, ``--flag=val``, short ``-xval``)
    and ``-c``/``--config`` overrides for ``config_keys`` from an argv slice."""
    short = {f for f in flags if len(f) == 2 and f.startswith("-") and f[1] != "-"}
    out: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            out.extend(args[i:])
            break
        if a in flags:
            i += 2
            continue
        if any(a.startswith(f + "=") for f in flags):
            i += 1
            continue
        if any(a.startswith(f) and len(a) > 2 for f in short):
            i += 1
            continue
        if config_keys:
            if a in ("-c", "--config"):
                if i + 1 < len(args) and _config_key(args[i + 1]) in config_keys:
                    i += 2
                    continue
                out.extend(args[i : i + 2])
                i += 2
                continue
            if a.startswith("--config=") and _config_key(a.split("=", 1)[1]) in config_keys:
                i += 1
                continue
            if a.startswith("-c") and len(a) > 2 and not a.startswith("--"):
                rest = a[3:] if a.startswith("-c=") else a[2:]
                if _config_key(rest) in config_keys:
                    i += 1
                    continue
        out.append(a)
        i += 1
    return out


class ClaudeAdapter(Adapter):
    agent = "claude"
    # 2: --tools limits the built-in tool inventory, not just what is pre-approved (issue #46).
    # 3: one --settings of agent-bus's own: awsAuthRefresh off, and the configured env (#36).
    recipe = "3"
    known_events = frozenset({"system", "assistant", "user", "result", "rate_limit_event", "stream_event"})
    default_command = cfg.AGENT_INFO["claude"]["cli"]
    # Read-only tools only. No Bash at all: `jj`/`git` accept flags (--tool, --output,
    # --ext-diff) that run commands or write files, and reviews get the diff in the prompt.
    allowed_tools = ["Read", "Grep", "Glob", "WebSearch", "WebFetch"]
    web_tool_names = ("WebSearch", "WebFetch")

    def invocation(self, prompt, resume_id, cwd, add_dirs):
        # --strict-mcp-config with no --mcp-config loads no MCP servers at all.  --tools is
        # the session's whole built-in tool set (without it Bash, CronCreate and the rest stay
        # declared, only unapproved); --allowedTools pre-approves the same list.
        tools = ",".join(t for t in self.allowed_tools if self.web_search or t not in self.web_tool_names)
        argv = [self.command, "-p", "--output-format", "stream-json", "--verbose",
                "--permission-mode", "plan", "--strict-mcp-config", "--tools", tools,
                "--allowedTools", tools]
        if not self.web_search:  # removed from the session, not just left unapproved
            argv += ["--disallowedTools", ",".join(self.web_tool_names)]
        for d in add_dirs:
            argv += ["--add-dir", d]
        argv += self.setting_args()
        sid = resume_id
        if resume_id:
            argv += ["--resume", resume_id]
        else:
            sid = new_uuid()
            argv += ["--session-id", sid]
        argv += ["--settings", json.dumps(self.flag_settings(), sort_keys=True)]
        # Ours is the whole inventory, and the only flag settings (a second --settings could
        # turn the credential refresh back on).
        argv += _strip_flags(self.extra_args(), {"--tools", "--settings"})
        return Invocation(argv=argv, stdin=prompt, external_id=sid)

    def flag_settings(self) -> dict:
        """Settings passed with --settings, which outrank the user's and project's own
        (Claude Code 2.1.330, live probe 2026-11-23; only managed settings outrank them):
        - ``awsAuthRefresh`` off: with expired AWS credentials Claude Code runs it even in
          print mode, and it is typically an interactive `aws sso login` (#36);
        - the configured env, since a settings file's ``env`` beats the process env, so
          AWS_REGION or AWS_PROFILE from ~/.claude/settings.json would win otherwise."""
        out: dict = {"awsAuthRefresh": ""}
        env = self.config.peer_env(self.participant)
        if env:
            out["env"] = env
        return out

    def parse_line(self, obj, st, emit):
        typ = obj.get("type")
        if obj.get("session_id"):
            st.external_id = obj["session_id"]
        if typ == "assistant":
            if isinstance(obj.get("error"), str):  # an API error the CLI turned into a message
                st.api_error = obj["error"]
                st.api_status = _num(obj.get("api_error_status"))
            for block in obj.get("message", {}).get("content", []) or []:
                if block.get("type") == "text" and block.get("text"):
                    st.text_parts.append(block["text"])
                    emit("text", _short(block["text"]))
                elif block.get("type") == "tool_use":
                    inp = block.get("input") or {}
                    hint = (inp.get("command") or inp.get("file_path") or inp.get("pattern") or inp.get("query")
                            or inp.get("url") or "")
                    emit("tool", _short(f"{block.get('name')} {hint}".strip()))
        elif typ == "system" and obj.get("subtype") == "api_retry":
            # The CLI retrying a failed API call itself: worth seeing when a turn hangs (a
            # region that doesn't resolve retries 10 times).  Its own fields only, checked.
            kind = obj.get("error") if isinstance(obj.get("error"), str) and _WORD.fullmatch(obj["error"]) else "?"
            emit("api_retry", f"api retry {_num(obj.get('attempt'))}/{_num(obj.get('max_retries'))}: {kind}")
        elif typ == "rate_limit_event" and isinstance(obj.get("rate_limit_info"), dict):
            st.rate_limit = obj["rate_limit_info"]
            st.rate_limit_at = time.time()  # when the reading arrived, not when the turn ends
        elif typ == "result":
            if obj.get("is_error") and _num(obj.get("api_error_status")) is not None:
                st.api_status = obj["api_error_status"]
            st.usage = usage_from(obj.get("usage"), CLAUDE_USAGE)
            thinking = (obj.get("usage") or {}).get("output_tokens_details") if isinstance(obj.get("usage"), dict) else None
            if isinstance(thinking, dict) and _num(thinking.get("thinking_tokens")) is not None:
                st.usage = {**(st.usage or {}), "thinking": thinking["thinking_tokens"]}
            if obj.get("is_error"):
                st.error = _short(str(obj.get("result") or obj.get("subtype") or "error"), 800)
            if isinstance(obj.get("result"), str):
                st.final = obj["result"]

    quota_patterns = (r"usage limit reached", r"hit your (?:usage )?limit", r"out of (?:extra )?usage")

    def usage_limits(self, st):
        # rate_limit_event.rate_limit_info.unifiedWindows, e.g. {"five_hour": {"utilization":
        # 0.58, "resetsAt": 1795423800, "status": "allowed"}, "seven_day": {...}}: Claude Code
        # 2.1.330, live probe 2026-11-23 (tests/data/streams/claude-*.jsonl, scrubbed).
        windows = (st.rate_limit or {}).get("unifiedWindows")
        if not isinstance(windows, dict):
            return None
        out = [_limit(name, w.get("utilization"), resets_at=w.get("resetsAt"))
               for name, w in windows.items() if isinstance(w, dict)]
        return [{**w, "observed_at": st.rate_limit_at} for w in out if w] or None

    def classify(self, st, error):
        # Credentials (#36): Claude Code's own error type, else the AWS SDK's wording.  First:
        # an earlier step's "allowed" rate-limit status says nothing about them.
        if st.api_error in _CLAUDE_AUTH_ERRORS or any(re.search(p, error) for p in _AWS_AUTH):
            return {"resets_at": None, "source": "auth"}
        # Structured: the effective rate-limit status says this request was rejected, and
        # no overage covers it.  Seen live on Claude Code 2.1.x as status allowed/rejected.
        info = st.rate_limit
        if info is not None:
            # The structured status is authoritative: "allowed", or covered by overage,
            # means this failure wasn't the usage limit, whatever the error text says.
            if info.get("status") == "rejected" and not info.get("isUsingOverage"):
                reset = info.get("resetsAt")
                return {"resets_at": float(reset) if isinstance(reset, (int, float)) and reset > 0 else None,
                        "source": "structured"}
            return None
        if self.config.bedrock(self.participant):
            # Bedrock sends no rate_limit_event (live probe 2026-11-23); its limits come as
            # errors once the CLI's own retries are spent.  Wording from the AWS docs.
            if any(re.search(p, error, re.IGNORECASE) for p in _BEDROCK_QUOTA):
                return {"resets_at": _reset_from_text(error), "source": "heuristic"}
            if st.api_status == 429 or any(re.search(p, error) for p in _BEDROCK_THROTTLE):
                return {"resets_at": _reset_from_text(error), "source": "throttle"}
        return super().classify(st, error)

    def misconfigured(self, st, error):
        # A wrong model id or inference profile, a region without it, or no access to it:
        # Bedrock answers all of them alike (live probes 2026-11-23, tests/data/bedrock).
        return st.api_error == "model_not_found" or any(re.search(p, error) for p in _BEDROCK_CONFIG)


_WORD = re.compile(r"[a-z0-9_]{1,40}")
# Claude Code's API error types for credentials it couldn't load or that were refused.  Seen
# live: cloud_credential_error (missing SSO token, 2.1.330).
_CLAUDE_AUTH_ERRORS = frozenset({"cloud_credential_error", "authentication_failed"})
# AWS SDK credential failures (the first one seen live, the rest as AWS documents them).
# AccessDeniedException is not one: the credentials worked, the model isn't allowed.
_AWS_AUTH = (r"Could not load AWS credentials", r"SSO session .{0,80}(?:not found|expired|invalid)",
             r"\bExpiredToken(?:Exception)?\b", r"security token included in the request is (?:expired|invalid)",
             r"\bUnrecognizedClientException\b", r"\bInvalidSignatureException\b")
# Bedrock's account quotas (ServiceQuotaExceededException; "Too many tokens per day" for the
# daily token quota): a usage block for that model and region.
_BEDROCK_QUOTA = (r"\bServiceQuotaExceededException\b", r"too many tokens per day")
# Request-rate throttling (ThrottlingException, HTTP 429): a short cooldown.
_BEDROCK_THROTTLE = (r"\bThrottlingException\b", r"\bToo many requests\b", r"\b429 Too many")
# The model or region refused: Bedrock's ValidationException wording (seen live) and an
# AccessDeniedException for a model the account or region has no access to (documented).
_BEDROCK_CONFIG = (r"provided model identifier is invalid", r"with on-demand throughput isn.t supported",
                   r"\bAccessDeniedException\b", r"(?:don't|do not) have access to the model")

CLAUDE_USAGE = {"input": "input_tokens", "output": "output_tokens", "cache_read": "cache_read_input_tokens",
                "cache_write": "cache_creation_input_tokens"}
CODEX_USAGE = {"input": "input_tokens", "output": "output_tokens", "cache_read": "cached_input_tokens",
               "cache_write": "cache_write_input_tokens", "thinking": "reasoning_output_tokens"}
AGY_USAGE = {"input": "input_tokens", "output": "output_tokens", "cache_read": "cache_read_tokens",
             "thinking": "thinking_tokens"}

# "Individual quota reached. ... Resets in 2h26m14s." seen live from agy 1.5.13 on 2026-11-22.
_AGY_QUOTA = (r"quota reached", r"quota (?:exceeded|exhausted|has been exhausted)", r"exhausted (?:your|the) quota",
              r"(?:out of|insufficient|no remaining) (?:ai )?credits", r"usage limit (?:reached|exceeded)")

_CODEX_KEY = re.compile(r"[A-Za-z0-9_-]+")


# Codex features switched off for every headless turn, after the configured args so they
# can't turn one back on (codex 0.171.0, live probes 2026-11-23):
# - apps: a codex_apps MCP server that `codex mcp list` doesn't show and the read-only sandbox
#   doesn't cover (plugin management, site publishing and more; issue #46).
# - goals: create_goal/update_goal keep a goal on the thread that outlives the turn.
# - image_generation: image_gen makes an image and saves it outside the sandbox's reach.
# - tool_suggest: request_plugin_install, which installs a plugin into ~/.codex.  It wasn't
#   declared in the probes once apps was off, so this is belt and braces.
# Left on: the collaboration tools (spawn_agent, send_message, ...).  No switch removes them
# (features.multi_agent and multi_agent_v2 = false don't: the model's catalog entry picks
# multi-agent v2), and a spawned agent is a thread of the same process with the same config:
# a probe's sub-agent got `Read-only file system` for `touch`.  They can't act outside the
# sandbox; they only cost tokens.
CODEX_FEATURES_OFF = ("apps", "goals", "image_generation", "tool_suggest")
# Flags that pick a sandbox or approval mode themselves, so they'd beat our `-c` overrides,
# and ones that change the repo or codex's own state outside the sandbox: --worktree creates
# a managed git worktree, and --trust saves trust for the project (MCP commands included).
# --yolo and --not-so-yolo are hidden aliases codex exec 0.171.0 accepts; it doesn't take
# abbreviated long flags (`--dangerously` is rejected).
_CODEX_SANDBOX_FLAGS = {"-s", "--sandbox"}
_CODEX_SANDBOX_BOOL_FLAGS = {"--dangerously-bypass-approvals-and-sandbox", "--yolo", "--not-so-yolo",
                             "--approve-for-me", "--worktree", "--trust"}


class CodexAdapter(Adapter):
    agent = "codex"
    # 2: the `apps` feature (its codex_apps MCP server) is switched off, and web search can be
    # (issue #46).
    # 3: goals, image_generation and tool_suggest off too; the sandbox and approval overrides
    # come after the configured args, whose own sandbox flags are dropped (issue #54).
    recipe = "3"
    known_events = frozenset({"thread.started", "turn.started", "item.started", "item.updated", "item.completed",
                              "turn.completed", "turn.failed", "error"})
    default_command = cfg.AGENT_INFO["codex"]["cli"]
    # `turn.completed.usage` after `exec resume` counts every turn of the session so far
    # (codex 0.171.0, live probe 2026-11-23; tests/data/streams/codex-resume.jsonl too).
    session_usage = True
    # Commands run in codex's read-only sandbox.
    shell = True

    def mcp_servers(self, cwd: str) -> list[str]:
        """Live MCP servers as codex would load them in ``cwd`` (project config included).

        Raises if they can't be listed: without the list we can't switch them off,
        and running a peer with unknown integrations live is not read-only.
        """
        # Listed with the same config the turn runs with: every override and profile spelling.
        argv = [self.command, *_codex_config_args(self.extra_args()), "mcp", "list", "--json"]
        try:
            # With the turn's CODEX_HOME too: another home has its own config.toml (#66).
            out = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=30,
                                 env={**os.environ, **self.child_env()})
            if out.returncode != 0:
                raise PeerSetupError(f"`codex mcp list` failed: {out.stderr.strip()[:300]}")
            data = json.loads(out.stdout)
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as e:
            raise PeerSetupError(f"could not list codex MCP servers to disable them: {e}") from e
        if not isinstance(data, list):
            raise PeerSetupError("unexpected `codex mcp list --json` output")
        # Every entry must be well formed: a server we can't name is one we can't switch
        # off, so a malformed entry fails the turn instead of being skipped (issue #32).
        for i, d in enumerate(data):
            # Name the entry and field only: a record can carry env vars or credential URLs.
            bad = ("not an object" if not isinstance(d, dict)
                   else "missing or empty `name`" if not isinstance(d.get("name"), str) or not d["name"]
                   else "`enabled` is not true/false" if not isinstance(d.get("enabled", True), bool) else None)
            if bad:
                raise PeerSetupError(f"unexpected entry #{i} in `codex mcp list --json`: {bad}")
        # Only servers that exist: overriding a missing one creates a half-defined entry
        # that codex rejects ("invalid transport").  agent-bus itself is also covered by
        # the AGENT_BUS_PEER guard even if it isn't listed here.
        return sorted({d["name"] for d in data
                       if isinstance(d, dict) and isinstance(d.get("name"), str) and d.get("enabled", True)})

    def __init__(self, config: Config, settings: dict | None = None, participant: str | None = None,
                 repo: str | None = None):
        super().__init__(config, settings, participant, repo)
        # A web_search="disabled" in peers.codex.args turns search off for the preamble and
        # the report too, so they say what the turn will really have.
        modes = [_codex_value(o) for o in _codex_overrides(self.extra) if _config_key(o) == "web_search"]
        if modes and modes[-1] == "disabled":
            self.web_search = False
        # The participant's own CODEX_HOME (#66): the turn runs with it, and its usage-limit
        # readings come from that home's rollout files.  None: the worker's own.
        home = config.codex_home(self.participant)
        self.home = Path(home) if home else None

    def child_env(self) -> dict[str, str]:
        env = super().child_env()
        return {**env, "CODEX_HOME": str(self.home)} if self.home else env

    def setting_args(self) -> list[str]:
        args = []
        if self.settings.get("model"):
            args += ["-m", self.settings["model"]]
        if self.settings.get("effort"):
            args += ["-c", f'model_reasoning_effort="{self.settings["effort"]}"']
        return args

    def extra_args(self) -> list[str]:
        # These features stay off: a features table is kept, and the final -c features.X=false
        # wins.  So do the sandbox and approval modes: flags that set them are dropped.
        flags, keys = set(_CODEX_SANDBOX_FLAGS), {f"features.{f}" for f in CODEX_FEATURES_OFF}
        if self.settings.get("model"):
            flags |= {"-m", "--model"}
            keys.add("model")
        if self.settings.get("effort"):
            keys.add("model_reasoning_effort")
        if not self.web_search:
            keys.add("web_search")
        out = _drop_bool_flags(_strip_flags(self.extra, flags, keys), _CODEX_SANDBOX_BOOL_FLAGS)
        for feature in CODEX_FEATURES_OFF:
            out = _drop_codex_feature(out, feature)
        return out

    def invocation(self, prompt, resume_id, cwd, add_dirs):
        # `apps` (on by default in 0.171.0) adds a codex_apps MCP server that `codex mcp list`
        # doesn't show and that the read-only sandbox doesn't cover: live probe 2026-11-23, it
        # offered plugin management, site publishing and more.  Web search is codex's hosted
        # tool (search and open_page, run by OpenAI), on by default ("cached" in a read-only
        # sandbox); off sets web_search="disabled" (issue #46).
        common = ["--json", "--skip-git-repo-check"]
        common += self.setting_args()
        extra = self.extra_args()
        # After the configured args, since a later -c wins (a features table, or a server
        # table, merges with it or would replace an earlier dotted key).
        last = ["-c", 'sandbox_mode="read-only"', "-c", 'approval_policy="never"']
        for feature in CODEX_FEATURES_OFF:
            last += ["-c", f"features.{feature}=false"]
        # The read-only sandbox doesn't cover MCP side effects, so switch every server off.
        for name in self.mcp_servers(cwd):
            # `-c` takes a dotted path with bare keys: a quoted key names a *different*
            # server ("invalid transport"), and a dot can't be expressed at all.  A
            # server we can't switch off fails the turn rather than staying live.
            if not _CODEX_KEY.fullmatch(name):
                raise PeerSetupError(f"can't switch off codex MCP server {name!r} for a read-only turn "
                                     "(its name can't be written in a `-c` override)")
            last += ["-c", f"mcp_servers.{name}.enabled=false"]
        # The mode is pinned: agent-bus's switch, not ~/.codex/config.toml, decides, so the
        # preamble never offers a tool codex dropped.  A mode in peers.codex.args is kept while
        # search is on (one that disables it turns self.web_search off).
        if not self.web_search:
            last += ["-c", 'web_search="disabled"']
        elif not any(_config_key(o) == "web_search" for o in _codex_overrides(extra)):
            last += ["-c", 'web_search="cached"']
        # ...but before a `--`, after which everything is positional.
        cut = _codex_options_end(extra)
        extra = extra[:cut] + last + extra[cut:]
        # A profile goes before `exec`: `exec resume` rejects --profile (codex 0.171.0).
        profile, extra = _codex_split_profile(extra)
        if resume_id:
            argv = [self.command, *profile, "exec", "resume", *common, *extra, resume_id, "-"]
        else:
            argv = [self.command, *profile, "exec", *common, "-C", cwd, *extra, "-"]
        return Invocation(argv=argv, stdin=prompt, external_id=resume_id)

    def parse_line(self, obj, st, emit):
        typ = obj.get("type")
        if typ == "thread.started" and obj.get("thread_id"):
            st.external_id = obj["thread_id"]
        elif typ in ("item.started", "item.completed"):
            item = obj.get("item") or {}
            itype = item.get("type")
            if itype == "agent_message" and typ == "item.completed":
                st.final = item.get("text", "")
                emit("text", _short(st.final))
            elif itype == "reasoning" and typ == "item.completed" and item.get("text"):
                emit("text", _short(item["text"]))
            elif itype == "command_execution" and typ == "item.started":
                emit("tool", _short(f"shell {item.get('command', '')}"))
            elif itype == "web_search" and typ == "item.completed":
                # The started item has an empty query (codex 0.171.0); the completed one says
                # what was searched or opened.
                action = item.get("action") if isinstance(item.get("action"), dict) else {}
                emit("tool", _short(f"web_search {item.get('query') or action.get('url') or ''}".strip()))
            elif itype in ("mcp_tool_call", "file_change") and typ == "item.started":
                emit("tool", _short(f"{itype} {item.get('query') or item.get('tool') or ''}"))
        elif typ == "turn.completed":
            st.usage = usage_from(obj.get("usage"), CODEX_USAGE)
        elif typ in ("turn.failed", "error"):
            err = obj.get("error") or obj.get("message") or obj
            st.error = _short(json.dumps(err) if not isinstance(err, str) else err, 800)

    def usage_limits(self, st):
        # `codex exec --json` streams no rate-limit info, but the session's rollout file has
        # it: each token_count event carries rate_limits.primary/secondary = {used_percent,
        # window_minutes, resets_at} (codex 0.171.0, live probe 2026-11-23;
        # tests/data/rollouts/codex-token-count.jsonl, scrubbed).
        path = codex_rollout(st.external_id, self.home) if st.external_id else None
        if not path:
            return None
        # Only what this turn appended: a resumed session's file already holds earlier
        # turns', so without a matching snapshot from before it ran there's no telling.
        if st.resumed:
            if not st.rollout or st.rollout[0] != path:
                return None
            offset = st.rollout[1]
        else:
            offset = 0  # a new session's file is new
        found = codex_rate_limits(path, offset)
        if not found:
            return None
        rl, seen = found
        out = []
        for name in ("primary", "secondary"):
            w = rl.get(name)
            if isinstance(w, dict):
                used = w.get("used_percent")
                out.append(_limit(name, used / 100 if isinstance(used, (int, float)) and not isinstance(used, bool)
                                  else None, w.get("window_minutes"), w.get("resets_at")))
        return [{**w, "observed_at": seen} for w in out if w] or None

    def before_run(self, st, resume_id):
        path = codex_rollout(resume_id, self.home) if resume_id else None
        if path:
            st.rollout = (path, path.stat().st_size)

    # Codex's own usage-limit and quota errors (codex-rs protocol error.rs).
    quota_patterns = (r"you've hit your usage limit", r"usage_limit_(?:reached|exceeded)", r"insufficient_quota",
                      r"quota exceeded\. check your plan")


class AntigravityAdapter(Adapter):
    agent = "antigravity"
    # 2: headless tool guidance in every prompt; denied actions reported as an incomplete turn.
    # 3: deny rules for commands and file writes; the guidance offers web search (issue #46).
    # 4: a custom main agent whose tool list is the whole inventory (issue #54).
    # 5: --disable-slash-commands (issue #93).
    recipe = "5"
    # agy 1.5.13 in plan mode declares search_web (Google-grounded search, run server side) but
    # no page fetch.  `search_web(*)` and `read_url(*)` deny rules are ignored, but a custom
    # agent's `tools` list removes it (live probes, 2026-11-23): see peer_agent.
    web_tools = "search only (plan mode has no page fetch)"
    # The tools a headless turn's agent gets.  Plan mode on its own also declares write_file,
    # edit_file, run_command, ask_question, define_subagent, invoke_subagent,
    # manage_subagents, send_message, schedule, run_workflow and swarm (agy 1.5.13, live
    # probes 2026-11-23).  The deny rules stop the writes and commands, and a sub-agent
    # inherits them (a probe's sub-agent got "Matches user-configured deny rule" for
    # write_file), but no rule covers the orchestration tools, and they act outside the turn:
    # invoke_subagent with a "branch" workspace created a branch and a git worktree in the
    # reviewed repo (under ~/src too); swarm workers get working copies the same way;
    # schedule sets timers and cron jobs (IsDaemon ones keep firing after the task);
    # run_workflow runs a Python orchestration script.  An agent's `tools` list is the whole
    # inventory apart from manage_task (background tasks of this conversation, none of which
    # can start without run_command) and the MCP tools (agent-bus only, see check_isolation).
    # agy 1.5.13's registry also has list_dir, grep_search and find_by_name (an agent may list
    # them), but no mode declares them to the model, plan mode or not (live probes 2026-11-24,
    # issue #93); search would need run_command, which stays denied.
    agent_tools = ("read_file",)
    web_tool_names = ("search_web",)
    # Session-only permission rules (Deny wins over any Allow or Ask, in agy's own settings
    # too).  Without them a command is auto-denied as needing approval and the turn ends with
    # no reply; with them it is a tool error the model answers past.  write_file also covers
    # edit_file, and plan mode allows workspace writes under the system temp directory without
    # asking (live probes, agy 1.5.13, 2026-11-23).  execute_url is the browser subagent.
    deny_rules = ("command(*)", "write_file(*)", "execute_url(*)")
    known_events = frozenset({"init", "step_update", "result"})

    def event_type(self, obj):
        t = obj.get("event")
        return t if isinstance(t, str) else None
    default_command = cfg.AGENT_INFO["antigravity"]["cli"]

    quota_patterns = _AGY_QUOTA

    def check_isolation(self) -> None:
        """agy has no per-session switch for MCP servers, so refuse to run a peer while
        any server other than agent-bus (which refuses peers itself) is configured,
        unless config sets peers.antigravity.allow_mcp."""
        if self.config.launch(self.participant).get("allow_mcp"):
            return
        base = self.config.peers.get(self.agent) if isinstance(self.config.peers.get(self.agent), dict) else {}
        if (self.command != os.path.expanduser(base.get("command", self.default_command))
                or self.extra != list(base.get("args", []))
                or self.config.peer_env(self.participant) != (base.get("env") or {})):
            # A participant with its own command or args (say, a wrapper or flag for another
            # account) may run agy with another config directory, which this check can't see (#43).
            raise PeerSetupError(
                f"{self.participant} runs Antigravity with its own command, args or env, so agent-bus can't check "
                f"which MCP servers it loads; set participants.{self.participant}.allow_mcp = true to run it "
                "anyway")
        # Where the child will look: its own env first (#36).  A relative directory would be
        # resolved from the turn's working directory, which this check can't stand in for.
        # The child doesn't expand ~ in them, so neither does this check.
        conf_dir = self.config.effective_env(self.participant, "AGY_CONFIG_DIR")
        home = self.config.effective_env(self.participant, "HOME")
        home = str(Path.home()) if home is None else home
        if (conf_dir and not os.path.isabs(conf_dir)) or not os.path.isabs(home):
            raise PeerSetupError("AGY_CONFIG_DIR and HOME must be absolute paths for agent-bus to check "
                                 "Antigravity's MCP servers")
        base = Path(conf_dir) if conf_dir else Path(home) / ".gemini" / "config"
        files = [base / "mcp_config.json", *sorted(base.glob("plugins/*/mcp_config.json"))]
        live = []
        for f in files:
            if not f.exists():
                continue
            try:
                text = f.read_text().strip()
                servers = (json.loads(text).get("mcpServers") or {}) if text else {}  # agy leaves it empty
            except (OSError, json.JSONDecodeError, AttributeError) as e:
                raise PeerSetupError(f"can't read {f} to check Antigravity's MCP servers: {e}") from e
            live += [n for n, spec in servers.items()
                     if n != "agent-bus" and not (isinstance(spec, dict) and spec.get("disabled"))]
        if live:
            raise PeerSetupError(
                f"Antigravity has MCP servers configured ({', '.join(sorted(live))}) that a headless peer "
                "can't be isolated from. Remove them, or set peers.antigravity.allow_mcp = true to accept that.")

    def peer_agent(self) -> tuple[str, str]:
        """(name, directory) of the custom agent a headless turn runs as: plan mode with only
        ``agent_tools`` (and search_web while web search is on).  agy finds an agent in the
        ``.agents/agents`` of a workspace directory, so the directory goes in with --add-dir.
        It holds nothing but agent-bus's own definitions.  A same-named agent in the reviewed
        repo's .agents doesn't win over it (live probe, agy 1.5.13), and an unknown --agent
        name fails the turn ("unknown agent") rather than falling back to the default."""
        tools = list(self.agent_tools) + (list(self.web_tool_names) if self.web_search else [])
        name = f"agent-bus-peer-{'web' if self.web_search else 'noweb'}"
        base = cfg.state_dir() / "antigravity-agent"
        agents = base / ".agents" / "agents"
        agents.mkdir(parents=True, exist_ok=True, mode=0o700)
        text = (f"---\nname: {name}\ndescription: agent-bus headless read-only peer\nmainAgent: true\n"
                f"subagent: false\ntools: [{', '.join(tools)}]\n---\n"
                "You are a headless, read-only peer in an agent-bus discussion or review. Follow the "
                "instructions in the user's message.\n")
        path = agents / f"{name}.md"
        if not path.exists() or path.read_text() != text:
            tmp = agents / f".{name}.{new_uuid()}.tmp"
            tmp.write_text(text)
            os.replace(tmp, path)  # atomic: a concurrent turn reads the old or new file whole
        return name, str(base)

    def invocation(self, prompt, resume_id, cwd, add_dirs):
        self.check_isolation()
        name, agent_dir = self.peer_agent()
        # --disable-slash-commands: in print mode, no slash command or skill expands from the
        # prompt (peer text included), and plan mode leaves out its /plan meta-prompt, which
        # asks for plan and walkthrough artifacts and user approval (live probe, agy 1.5.13,
        # 2026-11-24, issue #93).
        argv = [self.command, "--mode", "plan", "--sandbox", "--disable-slash-commands", "--output-format", "stream-json",
                "--settings", json.dumps({"permissions": {"deny": list(self.deny_rules)}}),
                "--agent", name, "--add-dir", agent_dir]
        for d in add_dirs:
            argv += ["--add-dir", d]
        if resume_id:
            argv += ["--conversation", resume_id]
        argv += self.setting_args()
        # Our --settings carries the deny rules, and our --agent the tool list: one in the
        # configured args could replace them.  agy parses flags Go-style, so a single dash works
        # as well as two (`-agent x`, `-settings=...`).
        argv += _strip_flags(self.extra_args(), {"--settings", "-settings", "--agent", "-agent"})
        # Every turn, resumed or not, says which tools work unattended (recipe 2).
        guide = AGY_HEADLESS if self.web_search else AGY_HEADLESS_NO_WEB
        prompt = guide + prompt
        # agy takes the prompt as the value of --print; there is no stdin mode
        # for plain text, so very long prompts go through a file.
        if len(prompt.encode()) > MAX_ARG:
            path = os.path.join(add_dirs[0] if add_dirs else cwd, f"prompt-{new_uuid()}.md")
            with open(path, "w") as f:
                f.write(prompt)
            prompt = (guide + f"Your full instructions are in the file {path}. "
                      "Read it with read_file and follow it exactly.")
        argv += ["--print", prompt]
        return Invocation(argv=argv, external_id=resume_id)

    def parse_line(self, obj, st, emit):
        ev = obj.get("event")
        if ev == "init":
            st.external_id = obj.get("conversation_id") or st.external_id
        elif ev == "step_update":
            su = obj.get("step_update") or {}
            st.external_id = su.get("conversation_id") or st.external_id
            kind, state, idx = su.get("step_type"), su.get("state"), su.get("step_index")
            if kind == "tool" and isinstance(idx, int):
                # A tool step ends DONE (fixtures) or ERROR (a denied run_command, agy 1.5.13
                # probe 2026-11-23).  Only one with no end state is still pending at exit.
                # A frame with no state changes nothing; an end frame that omits the tool's
                # name keeps the one its ACTIVE frame gave.
                info = su.get("tool_info") if isinstance(su.get("tool_info"), dict) else {}
                name = su.get("tool_name") or info.get("name")
                if state == "ACTIVE":
                    st.pending[idx] = name or st.pending.get(idx) or "tool"
                elif state:
                    prev = st.pending.pop(idx, None)
                    if state == "ERROR":
                        st.failed_tools.add(_safe_name(name or prev or "tool"))
            if kind == "agent_response":
                if su.get("text_delta"):
                    st.text_parts.append(su["text_delta"])
                    st.step_text.setdefault(idx, []).append(su["text_delta"])
                if state == "DONE":
                    # agy reports how much it thought, not what: say that much, before the
                    # text it led to.
                    usage = su.get("usage") if isinstance(su.get("usage"), dict) else {}
                    step = usage_from(usage, AGY_USAGE)
                    if step:
                        st.step_usage = {k: (st.step_usage or {}).get(k, 0) + step.get(k, 0)
                                         for k in {*(st.step_usage or {}), *step}}
                    if usage.get("thinking_tokens"):
                        secs = su.get("duration_seconds")
                        emit("thinking", f"{usage['thinking_tokens']} thinking tokens"
                             + (f" over {secs:.0f}s" if isinstance(secs, (int, float)) else ""))
                    text = "".join(st.step_text.pop(idx, []))
                    if text.strip():
                        emit("text", _short(text))
            elif state == "ACTIVE" and kind not in (None, "user_input"):
                # A long-running step can send several ACTIVE frames; report it once.
                key = ("active", idx)
                if idx is not None and key in st.step_text:
                    return
                st.step_text[key] = True
                if kind == "tool":
                    info = su.get("tool_info") if isinstance(su.get("tool_info"), dict) else {}
                    name = su.get("tool_name") or info.get("name") or "tool"
                    emit("tool", _short(f"{name} {_agy_hint(info.get('parameters'))}".strip()))
                else:
                    emit("tool", _short(str(kind)))
        elif ev == "result":
            res = obj.get("result") or {}
            st.external_id = res.get("conversation_id") or st.external_id
            # result.usage counts every turn of the conversation (num_turns says how many;
            # agy 1.5.13, live probe 2026-11-23), so this turn's share is the sum of its own
            # model steps.  Without step counts the total is this turn's only on a first turn
            # (num_turns may be missing, e.g. from an error result, so resuming decides).
            # A failed result's steps may not all have reported: leave it to run() to keep
            # the step sum as a partial count.
            whole = (usage_from(res.get("usage"), AGY_USAGE)
                     if not st.resumed and res.get("num_turns") in (None, 1) else None)
            st.usage = (st.step_usage or whole) if res.get("status") in (None, "SUCCESS") else whole
            if res.get("status") not in (None, "SUCCESS"):
                st.error = _short(f"status {res.get('status')}: {res.get('error') or ''}", 800)
            if isinstance(res.get("response"), str):
                st.final = res["response"]
            # Actions agy auto-denied because a headless turn can't ask for approval.
            denied = res.get("denied_actions")
            for d in denied if isinstance(denied, list) else []:
                # The action type only: display_name is free-form text from the CLI.
                if isinstance(d, dict) and isinstance(d.get("action"), str) and d["action"]:
                    st.denied.append(_safe_name(d["action"]))

    def incomplete(self, st):
        # agy 1.5.13 in plan mode with --sandbox (live probe, 2026-11-23): a terminal command
        # needs approval, so headless it is auto-denied.  The run_command step ends ERROR,
        # the result is SUCCESS with an empty response and lists denied_actions, and agy
        # exits 0.  Names only in the diagnostic, never arguments.
        # Only a recognised result with an explicitly blank response counts: anything else
        # (no result, a response of another type) may be a changed schema, so it stays drift.
        if not isinstance(st.final, str) or st.final.strip():
            return None
        tools = f"; failed tool(s): {', '.join(sorted(st.failed_tools))}" if st.failed_tools else ""
        if st.denied:
            return (f"ended its turn without a reply after it auto-denied {len(st.denied)} action(s) that need "
                    f"approval a headless turn can't give: {', '.join(st.denied)}{tools}")
        # Every event understood, but a tool call never reached an end state.
        if st.pending and not st.unknown:
            names = sorted({_safe_name(n) for n in st.pending.values()})
            return f"ended its turn without a reply while tool call(s) were unfinished: {', '.join(names)}{tools}"
        return None  # nothing in the stream explains it: format drift


# Copilot's read-only built-in tools (copilot 1.0.108, live probes 2026-11-23): file viewing,
# search and globbing.  `--available-tools` makes these the session's whole tool set, so the
# model never sees bash, apply_patch, task (sub-agents), skill, sql, the dynamic workflows or
# any MCP server's tools.  `grep` is the name its bundled read-only agents use and `rg` the one
# the main session declared; a name the build doesn't have is ignored.
COPILOT_TOOLS = ("view", "grep", "rg", "glob")
# Copilot has no web search tool; web_fetch fetches a page from this machine.  In -p mode a
# fetch that no URL rule allows is denied, so with search on the turn also gets
# --allow-all-urls (the same reach Claude's WebFetch has).
COPILOT_WEB_TOOLS = ("web_fetch",)
# Deny rules win over every allow, --allow-all-tools and allow-all settings included.
COPILOT_DENY = ("shell", "write", "memory")
# Options a configured `peers.copilot.args` may not pass: they widen the tool set, permissions,
# MCP servers, paths or URLs (the URL ones are ours to give when web search is on), pick the
# session, mode or output format, or send the session somewhere.  Valued and valueless alike:
# each is dropped with the values after it (several take a variadic or optional value).
_COPILOT_DROP = {
    "-p", "--prompt", "-i", "--interactive", "--fleet", "--acp", "--connect",
    "--available-tools", "--excluded-tools", "--allow-tool", "--allow-all-tools", "--allow-all-paths",
    "--allow-all-urls", "--allow-url", "--allow-all", "--yolo", "--assisted-approval", "--trust-folder",
    "--additional-mcp-config", "--enable-mcp-server", "--mcp-github-auth", "--plugin-dir", "--agent",
    "--enable-all-github-mcp-tools", "--add-github-mcp-toolset", "--add-github-mcp-tool",
    "--allow-all-mcp-server-instructions", "--enable-memory",
    "--mode", "--autopilot", "--plan", "--max-autopilot-continues",
    "-r", "--resume", "--continue", "--session-id", "-n", "--name", "-C",
    "--output-format", "--stream", "-s", "--silent", "--share", "--share-gist", "--sync-session",
    "--usage-output-file", "--log-dir",
    # Extra directories are the worker's to give (add_dirs): copilot also loads skills and
    # agents from them as trusted configuration.
    "--add-dir",
    # Another config directory has other MCP servers, which `copilot mcp list` (run without
    # these args) wouldn't see: set COPILOT_HOME in the participant's env instead.
    "--config-dir",
}
# Environment variables equivalent to permission flags (`copilot help environment`), and
# the opt-ins that load the checkout's own hooks and MCP servers in -p mode (off by default
# there).
_COPILOT_DROP_ENV = ("COPILOT_ALLOW_ALL", "COPILOT_ASSISTED_APPROVAL",
                     "GITHUB_COPILOT_PROMPT_MODE_REPO_HOOKS", "GITHUB_COPILOT_PROMPT_MODE_WORKSPACE_MCP")
COPILOT_USAGE = {"input": "input", "output": "output", "cache_read": "cache_read", "cache_write": "cache_write"}
# Copilot's own usage-limit messages (`copilot help limits` and `help billing`, 1.0.108):
# "You've hit the rate limit for this model.", "You've hit your session rate limit.",
# "You've reached your weekly rate limit.", "You've hit your rate limit.", and the plan's
# "Plan: 100% used, resets <date>".  Not yet seen live.  Only the first is per model.
_COPILOT_MODEL_QUOTA = r"hit the rate limit for this model"
_COPILOT_QUOTA = (_COPILOT_MODEL_QUOTA, r"hit your (?:session )?rate limit", r"reached your (?:weekly|session) rate limit",
                  r"plan: 100% used", r"usage limit (?:reached|exceeded)")


def _copilot_strip(args: list[str], drop: set[str]) -> list[str]:
    """Remove ``drop`` options from a copilot argv slice, with their ``=value`` or the
    non-option tokens after them (copilot's variadic and optional values), up to a ``--``."""
    out: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            out.extend(args[i:])
            break
        flag = a.split("=", 1)[0] if a.startswith("--") else a
        short = len(a) > 2 and a[0] == "-" and a[1] != "-" and a[:2] in drop  # -Cdir, -rID
        if flag in drop or short:
            i += 1
            if "=" not in a and not short:
                while i < len(args) and not args[i].startswith("-"):
                    i += 1
            continue
        out.append(a)
        i += 1
    return out


def _copilot_repo_hooks_check(cwd: str) -> None:
    """Refuse a checkout that defines repository hooks Copilot could run.

    Copilot loads a repository's hooks in -p mode when the folder is trusted, and nothing
    on its command line or in its environment switches them off (hooks reference, 1.0.108).
    A hook runs commands at session start, before and outside the turn's read-only tool
    set, and sees the review prompt.  agent-bus can't tell whether the user trusted the
    folder, so any repository hook fails the turn (#98 covers the other harnesses).
    Copilot's own repo-wide off switch, ``disableAllHooks`` in .github/copilot/settings.json,
    is honoured as Copilot resolves it: settings.local.json, applied after it, wins."""
    roots = [Path(cwd)]
    for d in (Path(cwd), *Path(cwd).parents):  # the repository root, where Copilot looks
        if (d / ".git").exists() or (d / ".jj").exists():
            if d != roots[0]:
                roots.append(d)
            break

    def load(f: Path):
        try:
            data = json.loads(f.read_text())
        except (OSError, UnicodeError, ValueError) as e:
            raise PeerSetupError(f"PEER_SETUP: can't read {f} to check it for Copilot hooks ({e}); "
                                 "refusing to run the turn") from e
        return data if isinstance(data, dict) else {}

    found = []
    for root in roots:
        off = None
        for name in ("settings.json", "settings.local.json"):  # the local file is applied last
            f = root / ".github" / "copilot" / name
            if f.is_file() and "disableAllHooks" in load(f):
                off = load(f)["disableAllHooks"]
        if off is True:
            continue
        hooks_dir = root / ".github" / "hooks"
        try:
            hook_files = sorted(p for p in hooks_dir.iterdir() if p.suffix == ".json") if hooks_dir.is_dir() else []
        except OSError as e:
            raise PeerSetupError(f"PEER_SETUP: can't list {hooks_dir} to check it for Copilot hooks ({e}); "
                                 "refusing to run the turn") from e
        for f in hook_files:
            data = load(f)
            if data.get("hooks") and data.get("disableAllHooks") is not True:
                found.append(f)
        for rel in (".github/copilot/settings.json", ".github/copilot/settings.local.json",
                    ".claude/settings.json", ".claude/settings.local.json"):
            f = root / rel
            if f.is_file() and load(f).get("hooks"):
                found.append(f)
    if found:
        raise PeerSetupError(
            "PEER_SETUP: this checkout defines hooks Copilot can run outside a read-only turn ("
            + ", ".join(str(f) for f in found) + "); refusing to run the turn. Remove them, or set "
            "disableAllHooks in .github/copilot/settings.json.")


class CopilotAdapter(Adapter):
    """GitHub Copilot CLI (`copilot -p`, 1.0.108; issue #2)."""
    agent = "copilot"
    recipe = "1"
    default_command = cfg.AGENT_INFO["copilot"]["cli"]
    # Every event type seen in live probes of 1.0.108 (2026-11-23), plus the error event its
    # code emits.
    known_events = frozenset({
        "session.start", "session.resume", "session.info", "session.mcp_servers_loaded", "session.tools_updated",
        "session.usage_checkpoint", "session.error", "session.warning", "session.indexed_search",
        "instruction.update", "user.message",
        "assistant.turn_start", "assistant.turn_end", "assistant.message_start", "assistant.message_delta",
        "assistant.message", "assistant.tool_call_delta", "assistant.reasoning", "assistant.reasoning_delta",
        "assistant.idle", "model.call_start", "model.call_finished", "model.call_final_result",
        "tool.execution_start", "tool.execution_complete", "result"})
    web_tools = "fetch pages (no search tool)"
    web_note = " and fetch web pages"
    # session.usage_checkpoint counts the whole session: a resumed probe's turn reported
    # requests 4 / cost 2 after a first turn's 2 / 1.
    session_usage = True
    drop_env = _COPILOT_DROP_ENV
    quota_patterns = _COPILOT_QUOTA

    def classify(self, st, error):
        found = super().classify(st, error)
        # A harness for many vendors' models: one model's rate limit leaves the others usable.
        if found and re.search(_COPILOT_MODEL_QUOTA, error, re.IGNORECASE):
            found["scope"] = "model"
        return found

    def setting_args(self) -> list[str]:
        args = []
        if self.settings.get("model"):
            args += ["--model", self.settings["model"]]
        if self.settings.get("effort"):
            args += ["--reasoning-effort", self.settings["effort"]]
        return args

    def extra_args(self) -> list[str]:
        drop = set(_COPILOT_DROP)
        if self.settings.get("model"):
            drop.add("--model")
        if self.settings.get("effort"):
            drop |= {"--reasoning-effort", "--effort"}  # one option, two spellings
        return _copilot_strip(self.extra, drop)

    def mcp_servers(self, cwd: str) -> list[str]:
        """Every MCP server copilot would load in ``cwd`` (user, workspace, plugin and
        built-in), so each can be switched off.  Raises if they can't be listed."""
        argv = [self.command, "mcp", "list", "--json"]
        # With the turn's own environment: a participant's configured env (#36) can point
        # copilot at another config directory, and so at other MCP servers.
        env = {k: v for k, v in {**os.environ, **self.child_env()}.items() if k not in self.drop_env}
        env["AGENT_BUS_PEER"] = "mcp-list"
        try:
            out = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=60,
                                 stdin=subprocess.DEVNULL, env=env)
            if out.returncode != 0:
                raise PeerSetupError(f"`copilot mcp list` failed: {out.stderr.strip()[:300]}")
            data = json.loads(out.stdout)
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as e:
            raise PeerSetupError(f"could not list copilot MCP servers to disable them: {e}") from e
        servers = data.get("mcpServers") if isinstance(data, dict) else None
        if not isinstance(servers, dict):
            raise PeerSetupError("unexpected `copilot mcp list --json` output")
        bad = [n for n in servers if not (isinstance(n, str) and n and not n.startswith("-"))]
        if bad:
            raise PeerSetupError("`copilot mcp list --json` names a server that can't be switched off")
        return sorted(servers)

    def invocation(self, prompt, resume_id, cwd, add_dirs):
        _copilot_repo_hooks_check(cwd)
        tools = list(COPILOT_TOOLS) + (list(COPILOT_WEB_TOOLS) if self.web_search else [])
        # The prompt is read from stdin when -p is empty (live probe).  --no-auto-update runs
        # the binary agent-bus fingerprinted, not a newer copy it downloaded earlier.
        argv = [self.command, "-p", "", "--output-format", "json", "--stream", "on", "--no-ask-user",
                "--no-auto-update", "--sync-session=off", "--disable-builtin-mcps",
                f"--available-tools={','.join(tools)}"]
        for rule in COPILOT_DENY:
            argv.append(f"--deny-tool={rule}")
        if self.web_search:
            argv.append("--allow-all-urls")
        for name in self.mcp_servers(cwd):
            argv += ["--disable-mcp-server", name]
        for d in add_dirs:
            argv += ["--add-dir", d]
        argv += self.setting_args()
        sid = resume_id
        if resume_id:
            argv += ["--resume", resume_id]
        else:
            sid = new_uuid()
            argv += ["--session-id", sid]
        argv += self.extra_args()
        return Invocation(argv=argv, stdin=prompt, external_id=sid)

    def parse_line(self, obj, st, emit):
        typ = obj.get("type")
        data = obj.get("data") if isinstance(obj.get("data"), dict) else {}
        if typ == "session.start" and isinstance(data.get("sessionId"), str):
            st.external_id = data["sessionId"]
        elif typ == "session.mcp_servers_loaded":
            # Fail closed: every server should come up disabled.  One that is live (or in a
            # state we don't know) wasn't switched off, so the turn stops before it runs.
            servers = data.get("servers") if isinstance(data.get("servers"), list) else []
            live = sorted({_safe_name(s.get("name")) for s in servers
                           if isinstance(s, dict) and s.get("status") != "disabled"})
            if live:
                st.abort = (f"PEER_SETUP: copilot loaded MCP server(s) agent-bus didn't switch off "
                            f"({', '.join(live)}); refusing to run the turn")
        elif typ == "assistant.message":
            content = data.get("content")
            if isinstance(content, str) and content.strip():
                st.final = content  # the last message with text is the reply
                emit("text", _short(content))
        elif typ == "tool.execution_start":
            args = data.get("arguments") if isinstance(data.get("arguments"), dict) else {}
            hint = next((args[k] for k in ("path", "pattern", "query", "url") if isinstance(args.get(k), str)), "")
            emit("tool", _short(f"{_safe_name(data.get('toolName'))} {hint}".strip()))
        elif typ == "session.usage_checkpoint":
            st.usage = _copilot_usage(data)
        elif typ == "session.error":
            st.error = _short(str(data.get("message") or data.get("errorType") or "error"), 800)
        elif typ == "result":
            if isinstance(obj.get("sessionId"), str):
                st.external_id = obj["sessionId"]
            code = obj.get("exitCode")
            if isinstance(code, int) and code != 0 and not st.error:
                st.error = f"copilot reported exit code {code}"


def _copilot_usage(data: dict) -> dict | None:
    """The session's token counts so far, summed over its models.  ``tokenDetails`` splits
    input from cache reads and writes (``usage.inputTokens`` is their sum); thinking is
    ``usage.reasoningTokens``."""
    snap = data.get("accountingSnapshot")
    models = snap.get("modelMetrics") if isinstance(snap, dict) else None
    if not isinstance(models, dict):
        return None
    total: dict = {}
    for m in models.values():
        if not isinstance(m, dict):
            continue
        details = m.get("tokenDetails") if isinstance(m.get("tokenDetails"), dict) else {}
        counts = usage_from({k: (v or {}).get("tokenCount") for k, v in details.items() if isinstance(v, dict)},
                            COPILOT_USAGE) or {}
        thinking = _num((m.get("usage") or {}).get("reasoningTokens")) if isinstance(m.get("usage"), dict) else None
        if thinking is not None:
            counts["thinking"] = thinking
        for k, v in counts.items():
            total[k] = total.get(k, 0) + v
    return total or None


_NAME = re.compile(r"[A-Za-z0-9_.:-]{1,40}")


def _safe_name(value: object) -> str:
    """A CLI-supplied identifier fit for a thread notice: a plain name as given, else "?".
    Never filtered down from free-form text, which could keep fragments of arguments."""
    return value if isinstance(value, str) and _NAME.fullmatch(value) else "?"


_AGY_HINT_KEYS = ("commandline", "command", "path", "absolutepath", "file", "directorypath", "searchdirectory",
                  "pattern", "query", "searchpath", "url")


def _agy_hint(params) -> str:
    """The most telling argument of an agy tool call.  Most parameter names are
    PascalCase, some lowercase (search_web's ``query``), so match case-insensitively."""
    if not isinstance(params, dict):
        return ""
    lowered = {str(k).lower(): v for k, v in params.items()}
    for key in _AGY_HINT_KEYS:
        val = lowered.get(key)
        if isinstance(val, str) and val:
            return val
    return ""


ADAPTERS = {a.agent: a for a in (ClaudeAdapter, CodexAdapter, AntigravityAdapter, CopilotAdapter)}


def adapter(agent: str, config: Config, settings: dict | None = None, repo: str | None = None) -> Adapter:
    """The adapter for a turn of participant ``agent``: its harness's, with its launch settings
    (and the web search switch of the allow-listed ``repo`` it runs in, if any)."""
    harness = config.participant(agent).harness
    return ADAPTERS[harness](config, settings, participant=agent, repo=repo)


def web_search_report(agent: str, config: Config) -> dict:
    """What a headless turn of ``agent`` gets for web search, for `agent-bus agents` (#46):
    on or off for the participant, and the allow-listed repos that turn it off (#54)."""
    cls = ADAPTERS[config.harness_of(agent)]
    on = adapter(agent, config).web_search  # the same resolution the turn and preamble use
    off_in = config.web_search_off_repos() if on else []
    note = cls.web_tools if on else ("off" if cls.web_off_enforced
                                     else "off by instruction only: the CLI has no switch for it")
    if off_in:
        note += f"; off in {', '.join(off_in)}"
    return {"enabled": on, "off_enforced": cls.web_off_enforced, "tools": cls.web_tools, "note": note,
            "off_in_repos": off_in}


def extract_json(text: str) -> dict | None:
    """Find the last JSON object in a model's reply (bare, fenced, or inline)."""
    text = text.strip()
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    dec = json.JSONDecoder()
    found = None
    i = text.find("{")
    while i != -1:
        try:
            obj, end = dec.raw_decode(text, i)
        except json.JSONDecodeError:
            i = text.find("{", i + 1)
            continue
        if isinstance(obj, dict):
            found = obj
        i = text.find("{", end)
    return found
