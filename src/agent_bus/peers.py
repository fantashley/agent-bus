"""Headless adapters for the three agent CLIs.

Each adapter builds a read-only command line for a first turn or a resumed
turn, and parses the CLI's streaming JSON into progress events, the final
response text and the external session id used to resume next round.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
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


Emit = Callable[[str, str], None]  # (kind, text)


class Adapter:
    agent: str
    default_command: str

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
                st.text_parts.append(line + "\n")
                continue
            if isinstance(obj, dict):
                try:
                    self.parse_line(obj, st, emit)
                except Exception as e:  # never let a format change kill the worker
                    emit("error", f"unparsed event: {e}")
        rc = proc.wait()
        killer_done.set()
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
        return TurnResult(ok=True, text=text, external_id=st.external_id, usage=st.usage)


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

_CODEX_KEY = re.compile(r"[A-Za-z0-9_-]+")


class CodexAdapter(Adapter):
    agent = "codex"
    default_command = cfg.AGENT_INFO["codex"]["cli"]

    def mcp_servers(self, cwd: str) -> list[str]:
        """Live MCP servers as codex would load them in ``cwd`` (project config included).

        Raises if they can't be listed: without the list we can't switch them off,
        and running a peer with unknown integrations live is not read-only.
        """
        overrides = [a for i, a in enumerate(self.extra) if i > 0 and self.extra[i - 1] == "-c"]
        argv = [self.command, *sum((["-c", o] for o in overrides), []), "mcp", "list", "--json"]
        try:
            out = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=30)
            if out.returncode != 0:
                raise PeerSetupError(f"`codex mcp list` failed: {out.stderr.strip()[:300]}")
            data = json.loads(out.stdout)
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as e:
            raise PeerSetupError(f"could not list codex MCP servers to disable them: {e}") from e
        if not isinstance(data, list):
            raise PeerSetupError("unexpected `codex mcp list --json` output")
        # Only servers that exist: overriding a missing one creates a half-defined entry
        # that codex rejects ("invalid transport").  agent-bus itself is also covered by
        # the AGENT_BUS_PEER guard even if it isn't listed here.
        return sorted({d["name"] for d in data
                       if isinstance(d, dict) and isinstance(d.get("name"), str) and d.get("enabled", True)})

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
    default_command = cfg.AGENT_INFO["antigravity"]["cli"]

    quota_patterns = _AGY_QUOTA

    def check_isolation(self) -> None:
        """agy has no per-session switch for MCP servers, so refuse to run a peer while
        any server other than agent-bus (which refuses peers itself) is configured,
        unless config sets peers.antigravity.allow_mcp."""
        if self.config.peers.get(self.agent, {}).get("allow_mcp"):
            return
        base = Path(os.environ.get("AGY_CONFIG_DIR") or Path.home() / ".gemini" / "config")
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

    def invocation(self, prompt, resume_id, cwd, add_dirs):
        self.check_isolation()
        argv = [self.command, "--mode", "plan", "--sandbox", "--output-format", "stream-json"]
        for d in add_dirs:
            argv += ["--add-dir", d]
        if resume_id:
            argv += ["--conversation", resume_id]
        argv += self.setting_args()
        argv += self.extra_args()
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
