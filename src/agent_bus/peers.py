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


Emit = Callable[[str, str], None]  # (kind, text)


class Adapter:
    agent: str
    default_command: str

    def __init__(self, config: Config):
        self.config = config
        self.command = config.peer_command(self.agent, self.default_command)
        self.extra = config.peer_args(self.agent)

    def invocation(self, prompt: str, resume_id: str | None, cwd: str, add_dirs: list[str]) -> Invocation:
        raise NotImplementedError

    def parse_line(self, obj: dict, st: StreamState, emit: Emit) -> None:
        raise NotImplementedError

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
            return TurnResult(ok=False, text=text, external_id=st.external_id, error=err)
        return TurnResult(ok=True, text=text, external_id=st.external_id)


def _killpg(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _short(text: str, n: int = 240) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


class ClaudeAdapter(Adapter):
    agent = "claude"
    default_command = "claude"
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
        sid = resume_id
        if resume_id:
            argv += ["--resume", resume_id]
        else:
            sid = new_uuid()
            argv += ["--session-id", sid]
        argv += self.extra
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
        elif typ == "result":
            if obj.get("is_error"):
                st.error = _short(str(obj.get("result") or obj.get("subtype") or "error"), 800)
            if isinstance(obj.get("result"), str):
                st.final = obj["result"]


_CODEX_KEY = re.compile(r"[A-Za-z0-9_-]+")


class CodexAdapter(Adapter):
    agent = "codex"
    default_command = "codex"

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
        if resume_id:
            argv = [self.command, "exec", "resume", *common, *self.extra, resume_id, "-"]
        else:
            argv = [self.command, "exec", *common, "-C", cwd, *self.extra, "-"]
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
        elif typ in ("turn.failed", "error"):
            err = obj.get("error") or obj.get("message") or obj
            st.error = _short(json.dumps(err) if not isinstance(err, str) else err, 800)


class AntigravityAdapter(Adapter):
    agent = "antigravity"
    default_command = "agy"

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
        argv += self.extra
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
            if su.get("step_type") == "agent_response" and su.get("text_delta"):
                st.text_parts.append(su["text_delta"])
            elif su.get("state") == "ACTIVE" and su.get("step_type") not in (None, "user_input", "agent_response"):
                emit("tool", _short(str(su.get("step_type"))))
            if su.get("step_type") == "agent_response" and su.get("state") == "DONE":
                emit("text", _short("".join(st.text_parts)[-400:]))
        elif ev == "result":
            res = obj.get("result") or {}
            st.external_id = res.get("conversation_id") or st.external_id
            if res.get("status") not in (None, "SUCCESS"):
                st.error = _short(f"status {res.get('status')}: {res.get('error') or ''}", 800)
            if isinstance(res.get("response"), str):
                st.final = res["response"]


ADAPTERS = {a.agent: a for a in (ClaudeAdapter, CodexAdapter, AntigravityAdapter)}


def adapter(agent: str, config: Config) -> Adapter:
    try:
        return ADAPTERS[agent](config)
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
