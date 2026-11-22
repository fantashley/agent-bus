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
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import config as cfg
from .config import Config
from .db import new_uuid

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
    usage: dict | None = None
    # The CLI exited normally but its output lacked what agent-bus needs (a reply, a
    # session id): its format probably changed (issue #32, PEER_FORMAT_CHANGED).
    format_changed: bool = False
    # Set only on a failed turn that the CLI said was out of usage (issue #26, agreed on
    # agent-bus thread t-3e260b): {"resets_at": epoch | None, "source": "structured" |
    # "heuristic"}.
    quota: dict | None = None


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
    rate_limit: dict | None = None  # Claude's latest rate_limit_event info
    unknown: dict = field(default_factory=dict)  # event type -> count, for types the adapter doesn't know
    stray: int = 0  # non-JSON stdout lines (diagnostics), counted only


Emit = Callable[[str, str], None]  # (kind, text)


class Adapter:
    agent: str
    default_command: str
    # Bump when the invocation or what the parser relies on changes, so a version's
    # recorded evidence is tied to the recipe it was gathered with (issue #32).
    recipe: str = "1"
    # Stream event types this adapter understands.  Anything else is counted and
    # reported, never fatal on its own: CLIs add event types often.
    known_events: frozenset = frozenset()

    def event_type(self, obj: dict) -> str | None:
        t = obj.get("type")
        return t if isinstance(t, str) else None

    def __init__(self, config: Config, settings: dict | None = None):
        self.config = config
        self.command = config.peer_command(self.agent, self.default_command)
        self.extra = config.peer_args(self.agent)
        self.settings = settings or {}  # validated {model, effort}; see Config.peer_settings

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

    # Provider wording for "out of usage", matched only against a turn that already failed.
    # Kept narrow on purpose: a bare 429, "overloaded", or RESOURCE_EXHAUSTED without
    # quota wording stays an ordinary error.
    quota_patterns: tuple[str, ...] = ()

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
        st = StreamState(external_id=inv.external_id)
        # Any agent-bus MCP server the peer CLI launches sees this and refuses every tool,
        # so a headless peer can't post, start jobs, or read blind proposals.
        env = {**os.environ, "AGENT_BUS_PEER": peer_tag}
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
        rc = proc.wait()
        killer_done.set()
        if not isinstance(st.external_id, str) or not st.external_id.strip():
            st.external_id = None  # only a non-empty string can be pinned and resumed
        if not isinstance(st.final, str):
            st.final = None
        st.text_parts = [p for p in st.text_parts if isinstance(p, str)]
        if st.stray:
            emit("format", f"{st.stray} non-JSON stdout line(s) from {self.agent} (not shown)")
        if st.unknown:
            emit("format", f"unrecognised {self.agent} stream events: "
                 + ", ".join(f"{k}×{n}" for k, n in sorted(st.unknown.items())[:10]))
        text = st.final if st.final is not None else "".join(st.text_parts)
        if timed_out:
            return TurnResult(ok=False, text=text, external_id=st.external_id, error="turn timed out",
                              timed_out=True)
        if rc != 0 or st.error:
            err = st.error or f"exit {rc}: {''.join(stderr_tail)[-800:]}"
            try:
                # err already ends with the stderr tail unless the stream reported its own error.
                quota = self.classify(st, err + "\n" + "".join(stderr_tail)[-800:] if st.error else err)
            except Exception:  # noqa: BLE001 - classification is best-effort
                quota = None
            return TurnResult(ok=False, text=text, external_id=st.external_id, error=err, usage=st.usage,
                              quota=quota)
        # Exited normally: the reply and a session id to resume are both required.
        missing = [what for what, ok in (("a reply", text.strip()), ("a session id", st.external_id)) if not ok]
        if missing:
            return TurnResult(ok=False, text=text, external_id=st.external_id, usage=st.usage, format_changed=True,
                              error=f"PEER_FORMAT_CHANGED: {self.agent} exited normally without "
                                    + " or ".join(missing))
        return TurnResult(ok=True, text=text, external_id=st.external_id, usage=st.usage)


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


def cli_version(bus, agent: str, command: str, cwd: str | None = None) -> str:
    """The CLI's own ``--version`` line, probed once per binary identity and cached in
    the bus (issue #32).  "unknown" when it can't be found or doesn't say.  A miss is
    probed under a per-binary file lock, so concurrent jobs don't race to different
    answers."""
    ident = cli_identity(command, cwd)
    if ident is None:
        return "unknown"
    launch, key = ident
    cached = bus.cli_version(key)
    if cached is not None:
        return cached
    import fcntl
    import hashlib
    lock_dir = Path(bus.path).parent / "locks"  # next to the cache itself
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock = lock_dir / f"version-{hashlib.sha256(key.encode()).hexdigest()[:16]}.lock"
    with open(lock, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        cached = bus.cli_version(key)  # another process may have probed while we waited
        if cached is not None:
            return cached
        version = "unknown"
        try:
            out = subprocess.run([launch, "--version"], cwd=cwd, capture_output=True, text=True, timeout=10,
                                 stdin=subprocess.DEVNULL, env={**os.environ, "AGENT_BUS_PEER": "version-probe"})
            line = next((ln.strip() for ln in (out.stdout or out.stderr).splitlines() if ln.strip()), "")
            if out.returncode == 0 and line:
                version = "".join(ch for ch in line if ch.isprintable())[:80]
        except (OSError, subprocess.TimeoutExpired):
            pass
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


def format_usage(u: dict | None) -> str:
    if not u:
        return ""
    names = (("input", "in"), ("output", "out"), ("cache_read", "cache"), ("thinking", "thinking"))
    return " · tokens " + " ".join(f"{label} {u[k]}" for k, label in names if k in u)


def _short(text: str, n: int = 240) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


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
    known_events = frozenset({"system", "assistant", "user", "result", "rate_limit_event", "stream_event"})
    default_command = cfg.AGENT_INFO["claude"]["cli"]
    # Read-only tools only. No Bash at all: `jj`/`git` accept flags (--tool, --output,
    # --ext-diff) that run commands or write files, and reviews get the diff in the prompt.
    allowed_tools = ["Read", "Grep", "Glob", "WebSearch", "WebFetch"]

    def invocation(self, prompt, resume_id, cwd, add_dirs):
        # --strict-mcp-config with no --mcp-config loads no MCP servers at all.
        argv = [self.command, "-p", "--output-format", "stream-json", "--verbose",
                "--permission-mode", "plan", "--strict-mcp-config",
                "--allowedTools", ",".join(self.allowed_tools)]
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
        if obj.get("session_id"):
            st.external_id = obj["session_id"]
        if typ == "assistant":
            for block in obj.get("message", {}).get("content", []) or []:
                if block.get("type") == "text" and block.get("text"):
                    st.text_parts.append(block["text"])
                    emit("text", _short(block["text"]))
                elif block.get("type") == "tool_use":
                    inp = block.get("input") or {}
                    hint = inp.get("command") or inp.get("file_path") or inp.get("pattern") or inp.get("query") or ""
                    emit("tool", _short(f"{block.get('name')} {hint}".strip()))
        elif typ == "rate_limit_event" and isinstance(obj.get("rate_limit_info"), dict):
            st.rate_limit = obj["rate_limit_info"]
        elif typ == "result":
            st.usage = usage_from(obj.get("usage"), CLAUDE_USAGE)
            thinking = (obj.get("usage") or {}).get("output_tokens_details") if isinstance(obj.get("usage"), dict) else None
            if isinstance(thinking, dict) and _num(thinking.get("thinking_tokens")) is not None:
                st.usage = {**(st.usage or {}), "thinking": thinking["thinking_tokens"]}
            if obj.get("is_error"):
                st.error = _short(str(obj.get("result") or obj.get("subtype") or "error"), 800)
            if isinstance(obj.get("result"), str):
                st.final = obj["result"]

    quota_patterns = (r"usage limit reached", r"hit your (?:usage )?limit", r"out of (?:extra )?usage")

    def classify(self, st, error):
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
        return super().classify(st, error)


CLAUDE_USAGE = {"input": "input_tokens", "output": "output_tokens", "cache_read": "cache_read_input_tokens",
                "cache_write": "cache_creation_input_tokens"}
CODEX_USAGE = {"input": "input_tokens", "output": "output_tokens", "cache_read": "cached_input_tokens",
               "thinking": "reasoning_output_tokens"}
AGY_USAGE = {"input": "input_tokens", "output": "output_tokens", "cache_read": "cache_read_tokens",
             "thinking": "thinking_tokens"}

# "Individual quota reached. ... Resets in 2h26m14s." seen live from agy 1.5.13 on 2026-11-22.
_AGY_QUOTA = (r"quota reached", r"quota (?:exceeded|exhausted|has been exhausted)", r"exhausted (?:your|the) quota",
              r"(?:out of|insufficient|no remaining) (?:ai )?credits", r"usage limit (?:reached|exceeded)")

def codex_config_args(args: list[str]) -> list[str]:
    """Every option in ``args`` that changes codex's configuration, in order, in a
    canonical split form: -c/--config overrides (split or joined) and -p/--profile.
    The MCP listing gets exactly these, so it sees the servers the turn will (#32)."""
    out: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            break
        for flags, canon in ((("-c", "--config"), "-c"), (("-p", "--profile"), "--profile")):
            if a in flags and i + 1 < len(args):
                out += [canon, args[i + 1]]
                i += 1
                break
            joined = next((a[len(f) + 1:] for f in flags if f.startswith("--") and a.startswith(f + "=")), None)
            for short in ("-c", "-p"):
                if joined is None and flags[0] == short and a.startswith(short) and len(a) > 2:
                    joined = a[2:].removeprefix("=")  # -cX and -c=X
            if joined is not None:
                out += [canon, joined]
                break
        i += 1
    return out


_CODEX_KEY = re.compile(r"[A-Za-z0-9_-]+")


class CodexAdapter(Adapter):
    agent = "codex"
    known_events = frozenset({"thread.started", "turn.started", "item.started", "item.updated", "item.completed",
                              "turn.completed", "turn.failed", "error"})
    default_command = cfg.AGENT_INFO["codex"]["cli"]

    def mcp_list(self, cwd: str) -> list[dict]:
        """`codex mcp list --json` as this launch would see it, validated, read once
        per adapter and cwd so the launch and doctor use one snapshot (#32).

        Raises if it can't be listed: without the list we can't switch servers off, and
        running a peer with unknown integrations live is not read-only."""
        cache = self.__dict__.setdefault("_mcp_cache", {})
        if cwd in cache:
            return cache[cwd]
        argv = [self.command, *codex_config_args(self.extra), "mcp", "list", "--json"]
        try:
            out = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=30)
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
        self.check_project_servers(cwd, {d["name"] for d in data})
        cache[cwd] = data
        return data

    def mcp_servers(self, cwd: str) -> list[str]:
        """Enabled MCP servers to switch off.  Only servers that exist: overriding a
        missing one creates a half-defined entry codex rejects ("invalid transport").
        agent-bus itself is also covered by the AGENT_BUS_PEER guard."""
        return sorted({d["name"] for d in self.mcp_list(cwd) if d.get("enabled", True)})

    @staticmethod
    def check_project_servers(cwd: str, listed: set[str]) -> None:
        """Project `.codex/config.toml` files above ``cwd`` can add MCP servers that a
        trusted exec loads even if `mcp list` didn't report them.  Any enabled one the
        listing missed fails closed: it would stay live without a disable override."""
        import tomllib
        d = Path(cwd).resolve()
        for parent in [d, *d.parents]:
            f = parent / ".codex" / "config.toml"
            try:
                text = f.read_text(encoding="utf-8")
            except FileNotFoundError:
                continue
            except (OSError, ValueError) as e:
                raise PeerSetupError(f"can't read {f} to check Codex's project MCP servers: {e}") from e
            try:
                servers = tomllib.loads(text).get("mcp_servers", {})
            except tomllib.TOMLDecodeError as e:
                raise PeerSetupError(f"can't parse {f} to check Codex's project MCP servers: {e}") from e
            if not isinstance(servers, dict):
                raise PeerSetupError(f"unexpected `mcp_servers` in {f}")
            missed = sorted(n for n, spec in servers.items()
                            if n not in listed and not (isinstance(spec, dict) and spec.get("enabled") is False))
            if missed:
                raise PeerSetupError(f"{f} defines MCP servers ({', '.join(missed)}) that `codex mcp list` didn't "
                                     "report, so a peer can't switch them off; remove them or mark them enabled = false")

    # Configured args that could change the turn's isolation in a way the MCP listing
    # and the generated overrides can't account for: refused outright (#32).
    UNSAFE_FLAGS = ("-s", "--sandbox", "-a", "--ask-for-approval", "--dangerously-bypass-approvals-and-sandbox",
                    "--yolo", "--full-auto", "-p", "--profile", "-C", "--cd", "--add-dir", "--enable", "--disable")
    UNSAFE_KEYS = ("mcp_servers", "sandbox_mode", "sandbox_workspace_write", "approval_policy", "profile",
                   "profiles", "projects", "features", "default_permissions", "permissions")

    def check_extra_args(self) -> None:
        cfg_args = codex_config_args(self.extra)
        for i in range(0, len(cfg_args), 2):
            flag, value = cfg_args[i], cfg_args[i + 1]
            key = value.split("=", 1)[0].strip().strip('"').strip("'")
            root = key.split(".", 1)[0]
            if flag == "--profile" or root in self.UNSAFE_KEYS:
                raise PeerSetupError(f"peers.codex.args may not set `{key if flag == '-c' else 'profile'}`: it "
                                     "changes the peer's sandbox, approvals or MCP servers")
        for a in self.extra:
            if a == "--":
                break
            if any(a == f or (f.startswith("--") and a.startswith(f + "="))
                   or (len(f) == 2 and a.startswith(f) and len(a) > 2 and f not in ("-c",))
                   for f in self.UNSAFE_FLAGS):
                raise PeerSetupError(f"peers.codex.args may not use `{a.split('=', 1)[0]}`: it changes the "
                                     "peer's sandbox, approvals, profile or working directories")

    def setting_args(self) -> list[str]:
        args = []
        if self.settings.get("model"):
            args += ["-m", self.settings["model"]]
        if self.settings.get("effort"):
            args += ["-c", f'model_reasoning_effort="{self.settings["effort"]}"']
        return args

    def extra_args(self) -> list[str]:
        flags, keys = set(), set()
        if self.settings.get("model"):
            flags |= {"-m", "--model"}
            keys.add("model")
        if self.settings.get("effort"):
            keys.add("model_reasoning_effort")
        return _strip_flags(self.extra, flags, keys) if (flags or keys) else list(self.extra)

    def invocation(self, prompt, resume_id, cwd, add_dirs):
        self.check_extra_args()
        common = ["--json", "--skip-git-repo-check", "-c", 'sandbox_mode="read-only"',
                  "-c", 'approval_policy="never"']
        # The read-only sandbox doesn't cover MCP side effects, so switch every server off.
        for name in self.mcp_servers(cwd):
            # `-c` takes a dotted path with bare keys: a quoted key names a *different*
            # server ("invalid transport"), and a dot can't be expressed at all.  A
            # server we can't switch off fails the turn rather than staying live.
            if not _CODEX_KEY.fullmatch(name):
                raise PeerSetupError(f"can't switch off codex MCP server {name!r} for a read-only turn "
                                     "(its name can't be written in a `-c` override)")
            common += ["-c", f"mcp_servers.{name}.enabled=false"]
        common += self.setting_args()
        extra = self.extra_args()
        if resume_id:
            argv = [self.command, "exec", "resume", *common, *extra, resume_id, "-"]
        else:
            argv = [self.command, "exec", *common, "-C", cwd, *extra, "-"]
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
            elif itype in ("web_search", "mcp_tool_call", "file_change") and typ == "item.started":
                emit("tool", _short(f"{itype} {item.get('query') or item.get('tool') or ''}"))
        elif typ == "turn.completed":
            st.usage = usage_from(obj.get("usage"), CODEX_USAGE)
        elif typ in ("turn.failed", "error"):
            err = obj.get("error") or obj.get("message") or obj
            st.error = _short(json.dumps(err) if not isinstance(err, str) else err, 800)

    # Codex's own usage-limit and quota errors (codex-rs protocol error.rs).
    quota_patterns = (r"you've hit your usage limit", r"usage_limit_(?:reached|exceeded)", r"insufficient_quota",
                      r"quota exceeded\. check your plan")


class AntigravityAdapter(Adapter):
    agent = "antigravity"
    known_events = frozenset({"init", "step_update", "result"})

    def event_type(self, obj):
        t = obj.get("event")
        return t if isinstance(t, str) else None
    default_command = cfg.AGENT_INFO["antigravity"]["cli"]

    quota_patterns = _AGY_QUOTA

    def check_isolation(self, cwd: str | None = None, add_dirs: list[str] | None = None) -> None:
        """agy has no per-session switch for MCP servers, so refuse to run a peer while
        any server other than agent-bus (which refuses peers itself) is configured,
        unless config sets peers.antigravity.allow_mcp.

        agy loads MCP servers from the global config root and from every workspace
        `.agents/` directory between the working directory and the project root
        (`mcp_config.json`, `plugins/*/`, and plugin dirs listed in `plugins.json`,
        which may inherit other such files).  Every one of those is checked, walking up
        to the filesystem root to be safe; anything unreadable fails closed (#32)."""
        if self.config.peers.get(self.agent, {}).get("allow_mcp"):
            return
        live = [f"{name} ({where})" for name, where, spec in agy_mcp_servers(cwd, add_dirs or [])
                if not trusted_agent_bus_entry(name, spec)]
        if live:
            raise PeerSetupError(
                f"Antigravity has MCP servers configured ({', '.join(sorted(live))}) that a headless peer "
                "can't be isolated from. Remove them, or set peers.antigravity.allow_mcp = true to accept that.")

    def invocation(self, prompt, resume_id, cwd, add_dirs):
        argv = [self.command, "--mode", "plan", "--sandbox", "--output-format", "stream-json"]
        for d in add_dirs:
            argv += ["--add-dir", d]
        if resume_id:
            argv += ["--conversation", resume_id]
        argv += self.setting_args()
        argv += self.extra_args()
        # Check the workspaces the final command line gives agy, configured extra args
        # included, resolved where agy will run.
        spaces = agy_workspaces(argv, cwd)
        self.check_isolation(spaces[0], spaces[1:])
        # agy takes the prompt as the value of --print; there is no stdin mode
        # for plain text, so very long prompts go through a file.
        if len(prompt.encode()) > MAX_ARG:
            path = os.path.join(add_dirs[0] if add_dirs else cwd, f"prompt-{new_uuid()}.md")
            with open(path, "w") as f:
                f.write(prompt)
            prompt = f"Your full instructions are in the file {path}. Read it and follow it exactly."
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
            if kind == "agent_response":
                if su.get("text_delta"):
                    st.text_parts.append(su["text_delta"])
                    st.step_text.setdefault(idx, []).append(su["text_delta"])
                if state == "DONE":
                    # agy reports how much it thought, not what: say that much, before the
                    # text it led to.
                    usage = su.get("usage") if isinstance(su.get("usage"), dict) else {}
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
            st.usage = usage_from(res.get("usage"), AGY_USAGE)
            if res.get("status") not in (None, "SUCCESS"):
                st.error = _short(f"status {res.get('status')}: {res.get('error') or ''}", 800)
            if isinstance(res.get("response"), str):
                st.final = res["response"]


def trusted_agent_bus_entry(name: str, spec: object) -> bool:
    """Only agent-bus's own registration is exempt, recognised by what it runs, not by
    its name: exactly {command, args}, args `mcp --agent antigravity`, and an absolute
    command that resolves to the agent-bus binary installed here (on PATH or running
    now).  That server refuses every call from a peer (AGENT_BUS_PEER)."""
    if name != "agent-bus" or not isinstance(spec, dict) or set(spec) - {"command", "args", "disabled"}:
        return False
    command = spec.get("command")
    if spec.get("args") != ["mcp", "--agent", "antigravity"] or not isinstance(command, str):
        return False
    if not os.path.isabs(command):  # a relative command resolves wherever agy runs it
        return False
    trusted = installed_agent_bus()
    return trusted is not None and os.path.realpath(command) == trusted


def installed_agent_bus() -> str | None:
    """The real path of the agent-bus installation in use: the running executable when
    it is agent-bus itself (workers and the CLI run that way), else the one on PATH."""
    running = sys.argv[0] if os.path.basename(sys.argv[0]) == "agent-bus" else None
    for candidate in (running, shutil.which("agent-bus")):
        if candidate and os.path.isabs(candidate):
            return os.path.realpath(candidate)
    return None


def agy_workspaces(argv: list[str], cwd: str) -> list[str]:
    """The workspace roots a launch gets: its cwd plus every --add-dir in the final
    argv (worker-supplied and configured extra args alike), resolved in that cwd."""
    out = [cwd]
    for i, a in enumerate(argv):
        if a == "--add-dir" and i + 1 < len(argv):
            out.append(argv[i + 1])
        elif a.startswith("--add-dir="):
            out.append(a.split("=", 1)[1])
    return [d if os.path.isabs(d) else os.path.join(cwd, d) for d in out]


def _stat_kind(p: Path) -> str | None:
    """"dir", "file", "other" or None (missing), distinguishing an unreadable path
    (PeerSetupError) from a missing one: Path.exists() hides permission errors."""
    import stat as st
    try:
        mode = os.stat(p).st_mode
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as e:
        raise PeerSetupError(f"can't check {p} for Antigravity customizations: {e}") from e
    return "dir" if st.S_ISDIR(mode) else "file" if st.S_ISREG(mode) else "other"


def _resolve(p: Path) -> Path:
    try:
        return p.resolve()
    except (OSError, RuntimeError) as e:
        raise PeerSetupError(f"can't resolve {p} for Antigravity customizations: {e}") from e


def agy_roots(cwd: str | None, add_dirs: list[str]) -> list[Path]:
    """agy customization roots a run in ``cwd`` (plus ``add_dirs`` workspaces) loads:
    the global config dir; each `.agents` dir from every workspace up to /; and any
    personal customization dir a repository's `.gemini/config.json` selects
    (`personal_customization_dir`), with its `.agents` (#32)."""
    roots = [Path(os.environ.get("AGY_CONFIG_DIR") or Path.home() / ".gemini" / "config")]

    def add(p: Path) -> None:
        if p not in roots:
            roots.append(p)

    for start in [cwd, *add_dirs]:
        if not start:
            continue
        d = _resolve(Path(start if os.path.isabs(start) else os.path.join(cwd or ".", start)))
        for parent in [d, *d.parents]:
            if _stat_kind(parent / ".agents") == "dir":
                add(parent / ".agents")
            conf = parent / ".gemini" / "config.json"
            if _stat_kind(conf) is not None:
                try:
                    data = json.loads(conf.read_text(encoding="utf-8") or "{}")
                except (OSError, ValueError) as e:
                    raise PeerSetupError(f"can't read {conf} to check Antigravity customizations: {e}") from e
                personal = data.get("personal_customization_dir") if isinstance(data, dict) else None
                if personal is not None:
                    if not isinstance(personal, str) or not personal:
                        raise PeerSetupError(f"unexpected personal_customization_dir in {conf}")
                    pd = Path(os.path.expanduser(personal))
                    pd = pd if pd.is_absolute() else parent / pd
                    add(pd)
                    add(pd / ".agents")
    return roots


def agy_mcp_servers(cwd: str | None, add_dirs: list[str]) -> list[tuple[str, str, object]]:
    """(server name, file, spec) for every enabled MCP server agy could load.

    A superset of agy's own discovery (issue #32).  For each root: its own
    mcp_config.json; each plugin under plugins/; and plugin dirs plugins.json selects,
    where an entry may be a plugin itself, a directory of plugins (one level), or name
    nested plugins through include_only.  A relative path is tried against every base
    agy might use.  Anything unreadable or malformed fails closed (PeerSetupError)."""
    found: list[tuple[str, str, object]] = []
    seen: set[Path] = set()

    def fail(f: Path, why: str) -> PeerSetupError:
        return PeerSetupError(f"can't check {f} for Antigravity MCP servers: {why}")

    def read_json(f: Path):
        try:
            text = f.read_text(encoding="utf-8").strip()
            return json.loads(text) if text else {}  # agy leaves some files empty
        except (OSError, ValueError) as e:  # ValueError covers JSON and UTF-8 decoding
            raise fail(f, str(e)) from e

    def servers_in(f: Path) -> None:
        if _stat_kind(f) is None:
            return
        f = _resolve(f)
        if f in seen:
            return
        seen.add(f)
        data = read_json(f)
        if not isinstance(data, dict):
            raise fail(f, "not a JSON object")
        servers = data.get("mcpServers", {})
        if not isinstance(servers, dict):
            raise fail(f, "`mcpServers` is not an object")
        found.extend((n, str(f), spec) for n, spec in servers.items()
                     if not (isinstance(spec, dict) and spec.get("disabled") is True))

    def plugin(d: Path) -> None:
        servers_in(d / "mcp_config.json")

    def plugin_dir(d: Path) -> None:
        """A directory that is a plugin, or holds plugins one level down."""
        kind = _stat_kind(d)
        if kind is None:
            return
        if kind == "file":
            if d.name == "mcp_config.json":
                servers_in(d)
            return
        plugin(d)
        try:
            children = sorted(c for c in d.iterdir() if c.name not in (".git", ".jj"))
        except OSError as e:
            raise fail(d, str(e)) from e
        for c in children:
            if _stat_kind(c) == "dir":
                plugin(c)

    def project_root(f: Path) -> Path:
        for p in [f.parent, *f.parents]:
            if _stat_kind(p / ".git") is not None or _stat_kind(p / ".jj") is not None:
                return p
        return f.parent

    def plugins_json(f: Path, depth: int = 0) -> None:
        if _stat_kind(f) is None:
            return
        f = _resolve(f)
        if f in seen:
            return
        if depth > 8:
            raise fail(f, "inherits too deeply")
        seen.add(f)
        data = read_json(f)
        if not isinstance(data, dict):
            raise fail(f, "not a JSON object")
        # agy resolves relative paths against the dir holding the dot-dir, falling back
        # to the project root; try every candidate.
        bases = {f.parent.parent if f.parent.name == ".agents" else f.parent, f.parent, project_root(f)}
        for key in ("entries", "inherits"):
            if key not in data:
                continue
            items = data[key]
            if not isinstance(items, list):
                raise fail(f, f"`{key}` is not a list")
            for item in items:
                path = item.get("path") if isinstance(item, dict) else None
                if not isinstance(path, str) or not path:
                    raise fail(f, f"an `{key}` item has no path")
                include = item.get("include_only", [])
                if not isinstance(include, list) or not all(isinstance(x, str) for x in include):
                    raise fail(f, "`include_only` is not a list of names")
                target = Path(os.path.expanduser(path))
                for t in ([target] if target.is_absolute() else [b / target for b in bases]):
                    if key == "inherits":
                        plugins_json(t, depth + 1)
                        continue
                    plugin_dir(t)
                    for name in include:  # nested selections, e.g. "group/plugin"
                        if "/" in name and ".." not in Path(name).parts:
                            plugin(t / name)

    for root in agy_roots(cwd, add_dirs):
        servers_in(root / "mcp_config.json")
        if _stat_kind(root / "plugins") == "dir":
            plugin_dir(root / "plugins")
        plugins_json(root / "plugins.json")
    return found


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


ADAPTERS = {a.agent: a for a in (ClaudeAdapter, CodexAdapter, AntigravityAdapter)}


def adapter(agent: str, config: Config, settings: dict | None = None) -> Adapter:
    try:
        return ADAPTERS[agent](config, settings)
    except KeyError:
        raise ValueError(f"unknown agent {agent!r}; expected one of {', '.join(ADAPTERS)}") from None


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
