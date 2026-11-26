"""The restart shim behind ``agent-bus mcp`` (#139).

A client's MCP server used to keep running the build it started with until the client
restarted it (`/reload-plugins` in Claude Code, a restart elsewhere).  Now `agent-bus mcp`
is a small line proxy that owns the client's stdio for the whole connection and runs the
real server as a child.  When a client calls a tool while nothing else is in flight and
the `agent-bus` on PATH has become a different build, the shim starts that build as a new
child, replays the client's ``initialize`` handshake to it, and only then retires the old
child and hands it the call (on MCP's handshake-free 2026-07-28 protocol, which Claude Code
speaks, there is no handshake: the shim asks it ``server/discover`` and re-opens the client's
``subscriptions/listen`` streams on it).  The bus session is registered once, by the shim, and every
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
# Set on the child too: the shim's log file, which the stale_build hint names (a shim from
# before the log has none, so a child it started points at the client's MCP log instead).
LOG_ENV = "AGENT_BUS_MCP_SHIM_LOG"
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
DISCOVER_ID = "agent-bus-shim-discover"
# MCP 2026-07-28 (handshake-free): every request carries the client's version, info and
# capabilities in its _meta, and server notifications ride `subscriptions/listen` requests,
# which stay open for the whole connection and are tagged with the listen request's id.
MODERN_META_PREFIX = "io.modelcontextprotocol/"
PROTOCOL_META = "io.modelcontextprotocol/protocolVersion"
SUBSCRIPTION_META = "io.modelcontextprotocol/subscriptionId"
LISTEN = "subscriptions/listen"
LISTEN_ACK = "notifications/subscriptions/acknowledged"
LOG_MAX_BYTES = 256 * 1024


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


_log_file: Path | None = None  # the session's shim log, once the shim runs


def _log(text: str) -> bool:
    """To stderr (which some clients don't keep) and to the session's shim log, kept to
    about LOG_MAX_BYTES: one older file is kept beside it.  Whether the log took it."""
    print(f"agent-bus mcp: {text}", file=sys.stderr, flush=True)
    if _log_file is None:
        return False
    try:
        _log_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)  # as the workers' logs
        if _log_file.exists() and _log_file.stat().st_size > LOG_MAX_BYTES:
            os.replace(_log_file, _log_file.with_name(_log_file.name + ".1"))
        fd = os.open(_log_file, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} pid {os.getpid()}: {text}\n")
        return True
    except OSError:
        return False


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


def _modern_meta(m: dict) -> dict | None:
    """The 2026-07-28 envelope (protocol version, client info and capabilities) a request
    carries in its _meta, or None for a request without one."""
    params = m.get("params")
    meta = params.get("_meta") if isinstance(params, dict) else None
    if not isinstance(meta, dict) or not isinstance(meta.get(PROTOCOL_META), str):
        return None
    return {k: v for k, v in meta.items() if k.startswith(MODERN_META_PREFIX)}


def _stream_of(m: dict) -> str | None:
    """The listen stream a server frame belongs to (its id, subscription tag or the request
    it cancels), as a key."""
    params = m.get("params") if isinstance(m.get("params"), dict) else {}
    meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
    for rid in (m.get("id") if "method" not in m else None, meta.get(SUBSCRIPTION_META), params.get("requestId")):
        if rid is not None:
            return _key(rid)
    return None


def _covers(new: dict, old: dict) -> bool:
    """Whether a listen stream honoring ``new`` delivers everything one honoring ``old`` did."""
    for k, v in old.items():
        if isinstance(v, list):
            if not set(map(json.dumps, v)) <= set(map(json.dumps, new.get(k) or [])):
                return False
        elif v and not new.get(k):
            return False
    return True


def _listen_ack(m: dict) -> Any:
    """The listen request id a subscriptions-acknowledged notification answers, or None."""
    if m.get("method") != LISTEN_ACK:
        return None
    meta = (m.get("params") or {}).get("_meta") if isinstance(m.get("params"), dict) else None
    return meta.get(SUBSCRIPTION_META) if isinstance(meta, dict) else None


class Mismatch(RuntimeError):
    """A candidate that can't serve the client's current streams or envelope: tried again
    once those change, not only when the installed build does.  ``probed`` is what it was
    probed against (Shim._streams, #168)."""

    def __init__(self, message: str, probed: str):
        super().__init__(message)
        self.probed = probed


class ListenRefused(RuntimeError):
    """A candidate ended a re-opened listen stream at once (an error or a final result)."""


def _fingerprint(meta: dict | None, listens) -> str:
    """The client's envelope and listen streams (each one's request and what the server
    honored): what a Mismatch depends on (#168)."""
    streams = sorted([key, v["request"], v["honored"]] for key, v in listens)
    return json.dumps([meta, streams], sort_keys=True, default=str)


class Child:
    """One server process of some build, serving the shim's session."""

    def __init__(self, exe: str, args: list[str], env: dict[str, str], on_line, on_eof):
        self.exe = exe
        self.proc = subprocess.Popen([exe, "mcp", *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env)
        self.replies: queue.Queue = queue.Queue()  # replies to the shim's own requests
        self.private: set[str] = set()  # the shim's own request ids not answered yet
        self.acks: set[str] = set()     # client listen streams re-opened here, not acknowledged yet
        self.eof = False
        self._lock = threading.Lock()
        self._on_line, self._on_eof = on_line, on_eof
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        for line in iter(self.proc.stdout.readline, b""):
            msgs = _parse(line)
            if len(msgs) == 1 and _is_response(msgs[0]) and _key(msgs[0]["id"]) in self.private:
                self.private.discard(_key(msgs[0]["id"]))
                self.acks.discard(_key(msgs[0]["id"]))
                self.replies.put(msgs[0])
                continue
            sub = _listen_ack(msgs[0]) if len(msgs) == 1 else None
            if sub is not None and _key(sub) in self.acks:
                # The ack of a listen stream the shim re-opened here: the client has its own.
                self.acks.discard(_key(sub))
                self.private.discard(_key(sub))  # from now on, the stream's frames are the client's
                self.replies.put({"id": sub, "ack": (msgs[0].get("params") or {}).get("notifications") or {}})
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
        self.private.add(_key(rid))
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

    def listen(self, request: dict, deadline: float) -> dict:
        """Re-open one of the client's listen streams here, under the client's own request id
        (its frames are tagged with it), and wait for the acknowledgement that opens it: the
        notifications it honors."""
        sub = request["id"]
        self.acks.add(_key(sub))
        self.private.add(_key(sub))  # a refusal comes back as a reply
        self.write(json.dumps(request).encode())
        while True:
            reply = self.replies.get(timeout=max(0.0, deadline - time.monotonic()))
            if reply is None:
                raise RuntimeError("it exited")
            if _key(reply["id"]) == _key(sub):
                if "ack" in reply:
                    return reply["ack"]
                raise ListenRefused(f"{LISTEN} ended at once: {reply.get('error') or reply.get('result')}")

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
        # A 2026-07-28 client (no initialize): the envelope of its latest request the server
        # accepted (and of those in flight), and its listen streams, which stay open rather
        # than being calls in flight: id -> {"request", "honored"} (None until acknowledged).
        self.meta: dict | None = None
        self.pending_meta: dict[str, dict] = {}
        self.listens: dict[str, dict] = {}
        # During a move: the candidate, and what it sent before taking over (passed on then).
        self.candidate: Child | None = None
        self.held: list[tuple[bytes, list[dict]]] = []
        self.replayed: dict[str, Any] = {}       # the listen streams re-opened on it (key -> id)
        self.last_skip: str | None = None        # why the last move didn't happen, as logged
        # A build that wouldn't start, and when to try it again (never, unless it timed
        # out): retried sooner only once the installed build changes.
        # A Mismatch also keeps the envelope and listen streams it was probed against (a
        # change retries it).
        self.failed: tuple[str, float] | None = None
        self.failed_with: str | None = None
        self.closing = False
        self.child = self._spawn(exe)

    # ------------------------------------------------------------------ children

    def _spawn(self, exe: str) -> Child:
        env = dict(os.environ)
        env[SESSION_ENV] = self.session_id
        if _log_file is not None:
            env[LOG_ENV] = str(_log_file)
        # The caller's PATH from before this build's Nix wrapper changed it, when the wrapper
        # recorded it: another build's wrapper adds its own bin and git/jj.
        caller = os.environ.get("AGENT_BUS_CALLER_PATH")
        env["PATH"] = caller if caller is not None else without_wrapper_dirs(env.get("PATH", ""),
                                                                             cfg.wrapper_path_dirs())
        return Child(exe, self.args, env, self._from_child, self._child_gone)

    def _from_child(self, child: Child, line: bytes, msgs: list[dict]) -> None:
        with self.lock:
            if child is self.candidate:
                self.held.append((line, msgs))  # its listen streams' frames, for after it takes over
                return
            if child is not self.child:
                return  # a failed candidate, or a retired child
            self._from_server(line, msgs)

    def _from_server(self, line: bytes, msgs: list[dict]) -> None:
        """Pass on what the serving child sent, noting what it settles (under the lock)."""
        for m in msgs:
            sub = _listen_ack(m)
            if sub is not None and _key(sub) in self.listens:
                listen = self.listens[_key(sub)]
                listen["honored"] = (m.get("params") or {}).get("notifications") or {}
                self.meta = _modern_meta(listen["request"]) or self.meta
            elif m.get("method") == "notifications/cancelled":  # the server ended a stream
                self.listens.pop(_key((m.get("params") or {}).get("requestId")), None)
            if _is_response(m):
                key = _key(m["id"])
                self.listens.pop(key, None)  # a listen stream the server ended
                method = (self.in_flight.pop(key, None) or (None, None))[0]
                self.cancelled.discard(key)
                meta = self.pending_meta.pop(key, None)
                if meta is not None and "result" in m:
                    self.meta = meta  # an envelope this server accepted
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
                    self.listens.pop(key, None)
                    self.pending_meta.pop(key, None)
                    call = self.in_flight.pop(key, None)
                    if call is not None and call[1] not in SETTLES_ON_CANCEL:
                        self.cancelled.add(key)
            if _is_request(m):
                if method == "tools/call":
                    self._maybe_swap()
                tool = (m.get("params") or {}).get("name") if method == "tools/call" else None
                meta = _modern_meta(m)
                with self.lock:
                    if method == LISTEN:
                        # Open for the whole connection, not a call in flight: moved with the session.
                        self.listens[_key(m["id"])] = {"request": m, "honored": None}
                    else:
                        self.in_flight[_key(m["id"])] = (method, tool if isinstance(tool, str) else None)
                        if meta is not None:
                            self.pending_meta[_key(m["id"])] = meta
            elif _is_response(m):
                with self.lock:
                    self.server_pending.discard(_key(m["id"]))
        try:
            self.child.write(line)
        except OSError:
            pass  # the child is gone: its reader ends the connection

    # ------------------------------------------------------------------ upgrades

    def _busy(self) -> bool:
        return self._busy_reason() is not None

    def _busy_reason(self) -> str | None:
        with self.lock:
            parts = [f"{what} {sorted(ids)}" for what, ids in (
                ("client requests in flight", [f"{k} ({m}{' ' + t if t else ''})" for k, (m, t) in self.in_flight.items()]),
                ("cancelled calls not settled", self.cancelled),
                ("server requests unanswered", self.server_pending),
                ("listen streams not acknowledged", [k for k, v in self.listens.items() if v["honored"] is None]))
                if ids]
        return "; ".join(parts) or None

    def _probe(self, cand: Child, before: float, deadline: float) -> list:
        """Hand the candidate the client's handshake (or, on 2026-07-28, ask it what it
        supports and re-open the client's listen streams on it) and check it can take over:
        same protocol, a tool list, and the shim's session taken up (not one of its own)."""
        with self.lock:  # what it is probed against (#168): a Mismatch is kept against it
            meta = self.meta or {}
            probed = _fingerprint(self.meta, self.listens.items())
        if self.init is not None:
            result = cand.request(INIT_ID, "initialize", self.init.get("params"), deadline)
            if self.protocol and result.get("protocolVersion") != self.protocol:
                raise RuntimeError(f"it speaks MCP {result.get('protocolVersion')}, the client {self.protocol}")
            cand.write(self.initialized)
            params: dict = {}
        else:
            params = {"_meta": meta}
            result = cand.request(DISCOVER_ID, "server/discover", params, deadline)
            versions = result.get("supportedVersions")
            if not isinstance(versions, list) or meta.get(PROTOCOL_META) not in versions:
                raise Mismatch(f"it speaks MCP {versions}, the client {meta.get(PROTOCOL_META)}", probed)
        tools = cand.request(LIST_ID, "tools/list", params, deadline).get("tools")
        if not isinstance(tools, list):
            raise RuntimeError("tools/list returned no tools")
        # The streams as they are now: one the serving child ended meanwhile isn't replayed.
        with self.lock:
            listens = list(self.listens.items())
            probed = _fingerprint(self.meta, listens)
        self.replayed = {key: listen["request"]["id"] for key, listen in listens}
        for key, listen in listens:
            try:
                honored = cand.listen(listen["request"], deadline)
            except ListenRefused as e:
                # Maybe a stream the serving child ended during the replay: tried again once
                # the streams change, like any stream it can't serve.
                raise Mismatch(f"{e} (listen stream {key})", probed) from e
            if not _covers(honored, listen["honored"] or {}):
                raise Mismatch(f"it honors {honored} on listen stream {key}, the current build "
                               f"{listen['honored']}", probed)
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
        once, to whichever child serves after this.  When it doesn't move, the shim log
        says why (once per reason)."""
        target = cfg.installed()
        if not target:
            return
        self._forget_failure(target)
        if target == self.child.exe:
            self.last_skip = None
            return
        before = None
        if os.path.islink(target):  # mid-switch: the profile link is being replaced
            why = f"the installed {target} is mid-switch"
        elif self.init is None and self.meta is None:
            why = "the client opened the connection with neither initialize nor a 2026-07-28 request"
        elif self.init is not None and self.initialized is None:
            why = "the client hasn't sent notifications/initialized"
        elif self.failed and time.monotonic() < self.failed[1]:
            why = ("it didn't start before; tried again once the installed build changes"
                   if self.failed[1] == float("inf")
                   else "it was slow to start before; tried again after " +
                   time.strftime("%H:%M:%S", time.localtime(time.time() + self.failed[1] - time.monotonic())))
        elif not enabled():
            why = "auto_restart_mcp is off"
        elif (busy := self._busy_reason()) is not None:
            why = f"busy: {busy}"
        elif (before := last_seen(self.session_id)) is None:
            why = "can't read the session's last_seen from the bus"  # so can't tell whether a candidate takes it up
        else:
            why = None
        if why is not None:
            if why != self.last_skip:  # once per reason, not on every call
                _log(f"not moving to the installed build {target} yet: {why}")
                self.last_skip = why
            return
        self.last_skip = None
        deadline = time.monotonic() + SWAP_DEADLINE_S
        try:
            with self.lock:
                self.candidate = cand = self._spawn(target)
        except OSError as e:
            _log(f"can't start the installed build {target}: {e}")
            self._fail(target, float("inf"))
            return
        self.replayed = {}
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
            self._fail(target, time.monotonic() + RETRY_AFTER_TIMEOUT_S)
            self._drop_candidate(cand)
            return
        with self.lock:
            # Checked under the lock its reader takes at EOF, so a candidate that exits now
            # is either seen here or, once promoted, ends the connection like any server.
            if error is None and (cand.eof or cand.proc.poll() is not None):
                error = RuntimeError("it exited")
            if error is None:
                old, self.child, self.candidate = self.child, cand, None
                # A stream the old build ended after the candidate re-opened it (the client
                # has its end): closed on the candidate too, and nothing more is sent on it.
                gone = {key: rid for key, rid in self.replayed.items() if key not in self.listens}
                # What it sent on the other listen streams meanwhile goes to the client first.
                held, self.held = self.held, []
                for line, msgs in held:
                    if not any(_stream_of(m) in gone for m in msgs):
                        self._from_server(line, msgs)
        if error is not None:
            _log(f"staying on {self.child.exe}: the installed build {target} didn't start: {error}")
            self._fail(target, float("inf"), error)
            self._drop_candidate(cand)
            return
        for rid in gone.values():
            try:
                cand.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/cancelled",
                                       "params": {"requestId": rid}}).encode())
            except OSError:
                pass
        tools = box["tools"]
        # Off the call's path: the old child ends the way a client ends it.
        threading.Thread(target=old.stop, daemon=True).start()
        _log(f"moved from {old.exe} to the installed build {target}")
        changed = self.tools is not None and tools != self.tools
        self.tools = tools
        if changed and self.init is not None:
            self._to_client(json.dumps({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}).encode())
        elif changed:  # 2026-07-28: on each listen stream that asked for it, tagged with its id
            with self.lock:
                listens = list(self.listens.values())
            for listen in listens:
                if (listen["honored"] or {}).get("toolsListChanged"):  # as the server agreed
                    self._to_client(json.dumps({"jsonrpc": "2.0", "method": "notifications/tools/list_changed",
                                                "params": {"_meta": {SUBSCRIPTION_META: listen["request"]["id"]}}}).encode())

    def _streams(self) -> str:
        """The client's envelope and listen streams now (see _fingerprint).  Called with
        self.lock held."""
        return _fingerprint(self.meta, self.listens.items())

    def _forget_failure(self, target: str) -> None:
        """Forget a failed build once the install changed (a later fix of it is tried), or,
        for one that couldn't serve the client's streams, once those changed since its probe:
        a stream ended, a new or re-filtered one, or another envelope (#168)."""
        if not self.failed:
            return
        changed = self.failed[0] != target
        if not changed and self.failed_with is not None:
            with self.lock:
                changed = self._streams() != self.failed_with
        if changed:
            self.failed = self.failed_with = None

    def _fail(self, target: str, until: float, error: BaseException | None = None) -> None:
        self.failed = (target, until)
        self.failed_with = error.probed if isinstance(error, Mismatch) else None

    def _drop_candidate(self, cand: Child) -> None:
        with self.lock:
            self.candidate, self.held = None, []
        cand.kill()


def main(harness: str, role: str | None, participant: str | None) -> int:
    global _log_file
    agent = resolve(harness, participant, cfg.Config.load())
    bus = Bus()
    try:
        session_id = register(bus, harness, agent, role)
    finally:
        bus.close()
    _log_file = cfg.shim_log_path(session_id)
    if not _log(f"session {session_id} for {harness}, serving {cfg._LAUNCHED_FROM}"):
        _log_file = None  # can't keep one: the hint points at the client's MCP log instead
    args = ["--agent", harness] + (["--participant", participant] if participant else []) + \
        (["--role", role] if role else [])
    try:
        Shim(args, session_id, cfg._LAUNCHED_FROM).run()
    except KeyboardInterrupt:
        pass
    return 0
