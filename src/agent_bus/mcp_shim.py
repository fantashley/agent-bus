"""The restart shim behind ``agent-bus mcp`` (#139).

A client's MCP server used to keep running the build it started with until the client
restarted it (`/reload-plugins` in Claude Code, a restart elsewhere).  Now `agent-bus mcp`
is a small line proxy that owns the client's stdio for the whole connection and runs the
real server as a child.  When a client calls a tool while nothing else is in flight and
the `agent-bus` on PATH has become a different build, the shim starts that build as a new
child, replays the client's ``initialize`` handshake to it, and only then retires the old
child and hands it the call.  The bus session is registered once, by the shim, and every
child serves that same session, so read cursors, the threads it started and its hooks'
binding to the client carry over.

The shim itself stays on the build it started with, so its contract with later builds
must stay stable: the `mcp` arguments, ``AGENT_BUS_MCP_SESSION`` (set on the child: serve
this session, don't register one), and newline-delimited JSON-RPC on stdin/stdout.

Not used for headless peers' servers, for a launch that isn't an installed `agent-bus`
script (``python -m agent_bus``, tests), or with ``auto_restart_mcp`` false: those run the
server in-process, as before.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from . import config as cfg
from . import procinfo
from .db import Bus

SESSION_ENV = "AGENT_BUS_MCP_SESSION"
# How long a move may hold the call that triggered it, in all: inside clients' 60 s tool
# timeouts together with a 55 s wait.  A server starts in about half a second; a build that
# takes longer is tried again later.  (The variable is for tests on loaded machines.)
SWAP_DEADLINE_S = float(os.environ.get("AGENT_BUS_MCP_SWAP_DEADLINE_S") or 4.0)
RETRY_AFTER_TIMEOUT_S = 300.0
STOP_TIMEOUT_S = 10.0
# The one tool whose cancellation ends its handler at once (it's async): the SDK runs the
# others in threads that carry on after a cancellation and then send no reply, so a session
# with such a call cancelled stays on its server.
SETTLES_ON_CANCEL = frozenset({"wait"})
# The shim's own requests to a candidate child; their replies never reach the client.
INIT_ID = "agent-bus-shim-initialize"
LIST_ID = "agent-bus-shim-tools-list"


def resolve(harness: str, participant: str | None, config: cfg.Config) -> str:
    """The participant an interactive session of ``harness`` speaks as: ``participant``
    when pinned (it must run on that harness), else the harness's default participant."""
    if harness not in cfg.HARNESSES:
        raise SystemExit(f"--agent must be one of {', '.join(cfg.HARNESSES)}")
    if participant is None:
        return harness
    try:
        found = config.participant(participant)
    except ValueError as e:
        raise SystemExit(f"--participant: {e}") from None
    if found.harness != harness:
        raise SystemExit(f"--participant {participant} runs on the {found.harness} harness, not {harness}")
    return participant


def register(bus: Bus, harness: str, agent: str, role: str | None) -> str:
    """Register this process's bus session: its client is the process that started it."""
    cwd = os.getcwd()
    # Under Codex's shared app-server daemon, REX_* are the daemon's, not this window's (#6).
    rex_reason = (procinfo.CODEX_SHARED_DAEMON
                  if harness == "codex" and procinfo.shared_codex_daemon(os.getpid()) is not None else None)
    # The client that started this server, so its hooks can find this session (and only it).
    client = os.getppid()
    return bus.register_session(agent, cwd=cwd, repo=cfg.repo_root(cwd), role=role, rex_reason=rex_reason,
                                client_pid=client, client_start=procinfo.start_time(client), harness=harness)


def enabled() -> bool:
    """``auto_restart_mcp`` from the config file now, read on its own: a newer build's
    config may hold settings this (older) shim's schema would reject, and the server
    validates the rest."""
    try:
        data = json.loads(cfg.config_path().read_text())
    except FileNotFoundError:
        return True
    except (OSError, ValueError):
        return False
    value = data.get("auto_restart_mcp", True) if isinstance(data, dict) else True
    return value is True


def wanted() -> bool:
    """Whether `agent-bus mcp` should run as the restart shim (a config this build rejects:
    no, so the server reports it as before)."""
    if os.environ.get("AGENT_BUS_PEER") or os.environ.get(SESSION_ENV) or not cfg._LAUNCHED_FROM:
        return False
    try:
        return cfg.Config.load().auto_restart_mcp
    except Exception:
        return False


def last_seen(session_id: str) -> float | None:
    """The session's last_seen in the bus, read-only; None if it can't be read."""
    import sqlite3
    uri = Path(cfg.db_path()).absolute().as_uri() + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=1)
        try:
            row = conn.execute("SELECT last_seen FROM sessions WHERE id=?", (session_id,)).fetchone()
        finally:
            conn.close()
        return float(row[0]) if row and row[0] is not None else None
    except (sqlite3.Error, TypeError, ValueError):
        return None


def without_wrapper_dirs(path: str, added: set[str]) -> str:
    """``path`` without what this build's Nix wrapper added, for a wrapper that didn't record
    the caller's PATH: another build's wrapper adds its own, and this build's bin or git/jj
    must not win over them.  The wrapper prepends and appends, so only the runs of its
    directories at either end are its; the same directory elsewhere came from the client
    and stays."""
    parts = path.split(os.pathsep)
    if not added:
        return path
    while parts and (not parts[0] or os.path.realpath(parts[0]) in added):
        parts.pop(0)
    while parts and (not parts[-1] or os.path.realpath(parts[-1]) in added):
        parts.pop()
    return os.pathsep.join(parts)


def _log(text: str) -> None:
    print(f"agent-bus mcp: {text}", file=sys.stderr, flush=True)


def _parse(line: bytes) -> list[dict]:
    """The JSON-RPC messages on a line (a batch is a list), or [] if it isn't JSON."""
    try:
        msg = json.loads(line)
    except ValueError:
        return []
    items = msg if isinstance(msg, list) else [msg]
    return [m for m in items if isinstance(m, dict)]


def _key(rid: Any) -> str:
    """A request id as the SDK matches it (a cancellation may name 7 as "7")."""
    return str(rid)


def _is_request(m: dict) -> bool:
    return "method" in m and m.get("id") is not None


def _is_response(m: dict) -> bool:
    return "method" not in m and m.get("id") is not None and ("result" in m or "error" in m)


class Child:
    """One server process of some build, serving the shim's session."""

    def __init__(self, exe: str, args: list[str], env: dict[str, str], on_line, on_eof):
        self.exe = exe
        self.proc = subprocess.Popen([exe, "mcp", *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env)
        self.replies: queue.Queue = queue.Queue()  # replies to the shim's own requests
        self.private: set[str] = set()  # the shim's own request ids not answered yet
        self.eof = False
        self._lock = threading.Lock()
        self._on_line, self._on_eof = on_line, on_eof
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        for line in iter(self.proc.stdout.readline, b""):
            msgs = _parse(line)
            if len(msgs) == 1 and _is_response(msgs[0]) and msgs[0]["id"] in self.private:
                self.private.discard(msgs[0]["id"])
                self.replies.put(msgs[0])
                continue
            self._on_line(self, line, msgs)
        self.eof = True
        self.replies.put(None)
        self._on_eof(self)

    def write(self, line: bytes) -> None:
        with self._lock:
            self.proc.stdin.write(line if line.endswith(b"\n") else line + b"\n")
            self.proc.stdin.flush()

    def request(self, rid: str, method: str, params: Any, deadline: float) -> dict:
        """Send the shim's own request and wait for its reply until ``deadline``."""
        self.private.add(rid)
        self.write(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}).encode())
        while True:
            reply = self.replies.get(timeout=max(0.0, deadline - time.monotonic()))
            if reply is None:
                raise RuntimeError("it exited")
            if reply["id"] == rid:
                if "error" in reply:
                    raise RuntimeError(f"{method} failed: {reply['error']}")
                if not isinstance(reply.get("result"), dict):
                    raise RuntimeError(f"{method} returned no result")
                return reply["result"]

    def stop(self) -> None:
        """End it the way a client does (close stdin), then harder if it doesn't exit."""
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(STOP_TIMEOUT_S)
            return
        except subprocess.TimeoutExpired:
            self.proc.terminate()
        try:
            self.proc.wait(STOP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()

    def kill(self) -> None:
        self.proc.kill()
        self.proc.wait()


class Shim:
    def __init__(self, args: list[str], session_id: str, exe: str,
                 stdin=None, stdout=None):
        self.args, self.session_id = args, session_id
        self.stdin = stdin or sys.stdin.buffer
        self.stdout = stdout or sys.stdout.buffer
        self.lock = threading.Lock()      # the state below
        self.out_lock = threading.Lock()  # writes to the client
        # Client requests the child hasn't answered: id -> (method, tool name for a tools/call).
        self.in_flight: dict[str, tuple[str, str | None]] = {}
        self.cancelled: set[str] = set()         # cancelled ones whose handler may still run
        self.server_pending: set[str] = set()    # the child's requests the client hasn't answered
        self.init: dict | None = None            # the client's initialize request
        self.initialized: bytes | None = None    # and its notifications/initialized line
        self.protocol: str | None = None         # the protocol version the client agreed
        self.tools: list | None = None           # the tool list the client last saw
        # A build that wouldn't start, and when to try it again (never, unless it timed
        # out): retried sooner only once the installed build changes.
        self.failed: tuple[str, float] | None = None
        self.closing = False
        self.child = self._spawn(exe)

    # ------------------------------------------------------------------ children

    def _spawn(self, exe: str) -> Child:
        env = dict(os.environ)
        env[SESSION_ENV] = self.session_id
        # The caller's PATH from before this build's Nix wrapper changed it, when the wrapper
        # recorded it: another build's wrapper adds its own bin and git/jj.
        caller = os.environ.get("AGENT_BUS_CALLER_PATH")
        env["PATH"] = caller if caller is not None else without_wrapper_dirs(env.get("PATH", ""),
                                                                             cfg.wrapper_path_dirs())
        return Child(exe, self.args, env, self._from_child, self._child_gone)

    def _from_child(self, child: Child, line: bytes, msgs: list[dict]) -> None:
        with self.lock:
            if child is not self.child:
                return  # a candidate before it takes over, or a retired child
            for m in msgs:
                if _is_response(m):
                    key = _key(m["id"])
                    method = (self.in_flight.pop(key, None) or (None, None))[0]
                    self.cancelled.discard(key)
                    result = m.get("result")
                    if method == "initialize" and isinstance(result, dict):
                        # Tell the client the tool list can change, for after an upgrade.
                        caps = result.setdefault("capabilities", {})
                        tools = caps.get("tools")
                        caps["tools"] = {**(tools if isinstance(tools, dict) else {}), "listChanged": True}
                        self.protocol = result.get("protocolVersion")
                        line = json.dumps(m).encode() + b"\n"
                    elif method == "tools/list" and isinstance(result, dict) and isinstance(result.get("tools"), list):
                        self.tools = result["tools"]
                elif _is_request(m):
                    self.server_pending.add(_key(m["id"]))
            self._to_client(line)

    def _child_gone(self, child: Child) -> None:
        with self.lock:
            current = child is self.child and not self.closing
        if current:
            # As before the shim: the server's end is the connection's end.
            code = child.proc.wait()
            _log(f"the server exited ({code})")
            os._exit(code or 1)

    def _to_client(self, line: bytes) -> None:
        with self.out_lock:
            self.stdout.write(line if line.endswith(b"\n") else line + b"\n")
            self.stdout.flush()

    # ------------------------------------------------------------------ client side

    def run(self) -> None:
        for line in iter(self.stdin.readline, b""):
            self._from_client(line)
        with self.lock:
            self.closing = True
        self.child.stop()

    def _from_client(self, line: bytes) -> None:
        for m in _parse(line):
            method = m.get("method")
            if method == "initialize" and _is_request(m):
                self.init = m
            elif method == "notifications/initialized":
                self.initialized = line
            elif method == "notifications/cancelled":
                key = _key((m.get("params") or {}).get("requestId"))
                with self.lock:
                    call = self.in_flight.pop(key, None)
                    if call is not None and call[1] not in SETTLES_ON_CANCEL:
                        self.cancelled.add(key)
            if _is_request(m):
                if method == "tools/call":
                    self._maybe_swap()
                tool = (m.get("params") or {}).get("name") if method == "tools/call" else None
                with self.lock:
                    self.in_flight[_key(m["id"])] = (method, tool if isinstance(tool, str) else None)
            elif _is_response(m):
                with self.lock:
                    self.server_pending.discard(_key(m["id"]))
        try:
            self.child.write(line)
        except OSError:
            pass  # the child is gone: its reader ends the connection

    # ------------------------------------------------------------------ upgrades

    def _busy(self) -> bool:
        with self.lock:
            return bool(self.in_flight or self.cancelled or self.server_pending)

    def _probe(self, cand: Child, before: float, deadline: float) -> list:
        """Hand the candidate the client's handshake and check it can take over: same
        protocol, a tool list, and the shim's session taken up (not one of its own)."""
        result = cand.request(INIT_ID, "initialize", self.init.get("params"), deadline)
        if self.protocol and result.get("protocolVersion") != self.protocol:
            raise RuntimeError(f"it speaks MCP {result.get('protocolVersion')}, the client {self.protocol}")
        cand.write(self.initialized)
        tools = cand.request(LIST_ID, "tools/list", {}, deadline).get("tools")
        if not isinstance(tools, list):
            raise RuntimeError("tools/list returned no tools")
        # A build that serves SESSION_ENV touches the session at startup; the old child is
        # idle, so a later last_seen is the candidate's.  One from before the shim registers
        # a session of its own instead.
        now = last_seen(self.session_id)
        if now is None or now <= before:
            raise RuntimeError("it didn't take up this session (a build from before the restart shim?)")
        return tools

    def _maybe_swap(self) -> None:
        """Before forwarding a tool call: move onto the installed build if it changed and
        nothing is in flight.  Never resends or drops a message: the call is forwarded
        once, to whichever child serves after this.  Only for a client that opened the
        session with ``initialize`` (a client on MCP's newer handshake-free protocol stays
        on its build, as before)."""
        if self.init is None or self.initialized is None or self._busy():
            return
        target = cfg.installed()
        if not target or os.path.islink(target):  # mid-switch: the profile link is being replaced
            return
        if self.failed and self.failed[0] != target:
            self.failed = None  # the install changed since: a later fix of that build is tried
        if target == self.child.exe or (self.failed and time.monotonic() < self.failed[1]) or not enabled():
            return
        deadline = time.monotonic() + SWAP_DEADLINE_S
        before = last_seen(self.session_id)
        if before is None:
            return  # can't read the bus now, so can't tell whether a candidate takes the session up
        try:
            cand = self._spawn(target)
        except OSError as e:
            _log(f"can't start the installed build {target}: {e}")
            self.failed = (target, float("inf"))
            return
        # The whole probe runs on a thread so that one deadline bounds every step of it
        # (a candidate that never reads its stdin blocks a write); killing the candidate
        # unblocks the thread.
        box: dict[str, Any] = {}

        def probe() -> None:
            try:
                box["tools"] = self._probe(cand, before, deadline)
            except BaseException as e:  # noqa: BLE001 - reported below
                box["error"] = e

        thread = threading.Thread(target=probe, daemon=True)
        thread.start()
        thread.join(max(0.0, deadline - time.monotonic()) + 0.2)
        timed_out = thread.is_alive()  # before reading box: the thread may finish meanwhile
        error = box.get("error")
        if timed_out or isinstance(error, queue.Empty):
            _log(f"staying on {self.child.exe} for now: the installed build {target} didn't start "
                 f"within {SWAP_DEADLINE_S:g} s")
            self.failed = (target, time.monotonic() + RETRY_AFTER_TIMEOUT_S)
            cand.kill()
            return
        with self.lock:
            # Checked under the lock its reader takes at EOF, so a candidate that exits now
            # is either seen here or, once promoted, ends the connection like any server.
            if error is None and (cand.eof or cand.proc.poll() is not None):
                error = RuntimeError("it exited")
            if error is None:
                old, self.child = self.child, cand
        if error is not None:
            _log(f"staying on {self.child.exe}: the installed build {target} didn't start: {error}")
            self.failed = (target, float("inf"))
            cand.kill()
            return
        tools = box["tools"]
        # Off the call's path: the old child ends the way a client ends it.
        threading.Thread(target=old.stop, daemon=True).start()
        _log(f"moved from {old.exe} to the installed build {target}")
        changed = self.tools is not None and tools != self.tools
        self.tools = tools
        if changed:
            self._to_client(json.dumps({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}).encode())


def main(harness: str, role: str | None, participant: str | None) -> int:
    agent = resolve(harness, participant, cfg.Config.load())
    bus = Bus()
    try:
        session_id = register(bus, harness, agent, role)
    finally:
        bus.close()
    args = ["--agent", harness] + (["--participant", participant] if participant else []) + \
        (["--role", role] if role else [])
    try:
        Shim(args, session_id, cfg._LAUNCHED_FROM).run()
    except KeyboardInterrupt:
        pass
    return 0
