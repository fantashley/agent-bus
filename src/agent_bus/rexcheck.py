"""``agent-bus rex-check``: what Rex can and can't tell us about an agent's block.

v1.1 decision (thread t-eda8ca, Claude/Codex/Antigravity): agent-bus never
types into a terminal.  Rex reports whether an agent says it is idle, but an
idle agent with a half-typed draft looks exactly like one with an empty input
box, and a check followed by a separate write could always race a human typing.
So this command only reports.  Each fact is checked separately, anything
missing, malformed or unreadable fails, and input readiness is always reported
as unavailable, never inferred.

Read-only: it calls ``rex block inspect`` and ``rex block call`` with the
``program_status`` and ``process`` methods, which don't change the block, with
``--autostart=false`` so it never starts a Rex server, and ``-S`` with the
server a session recorded (REX_SERVER), if any.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime
from typing import Any, Callable

from .db import Bus

# OSC 7501 app names each client reports (measured on Rex with all three).
APPS = {"claude": "claude-code", "codex": "codex", "antigravity": "antigravity"}

PASS, FAIL, INFO, UNAVAILABLE = "pass", "fail", "info", "unavailable"

INPUT_READY = ("no client or Rex signal says whether the input box is empty (an idle agent with a "
               "draft reports idle too); agent-bus never types into terminals")

Runner = Callable[..., subprocess.CompletedProcess]


class RexError(Exception):
    pass


def _rex(run: Runner, server: str | None, *args: str) -> dict:
    # Never start a server (rex autostarts a missing local one by default), and ask the
    # server the session recorded, not whichever one this shell would pick.
    argv = ["rex", "--autostart=false", *(["-S", server] if server else []), *args]
    try:
        out = run(argv, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RexError(str(e)) from e
    if out.returncode != 0:
        text = " ".join(((out.stderr or "") + " " + (out.stdout or "")).split())
        raise RexError(text[:200] or f"exit {out.returncode}")
    try:
        data = json.loads(out.stdout)
    except json.JSONDecodeError as e:
        raise RexError(f"unreadable output from rex {' '.join(args[:3])}") from e
    if not isinstance(data, dict):
        raise RexError(f"unexpected output from rex {' '.join(args[:3])}")
    return data


def _proc(pid: int) -> tuple[int, str] | None:
    """(parent pid, state letter) of a process, or None if it can't be read."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            # Fields after the parenthesised command name (which may contain spaces).
            rest = f.read().rsplit(")", 1)[1].split()
            return int(rest[1]), rest[0]
    except (OSError, IndexError, ValueError):
        pass
    try:  # macOS and other systems without /proc
        out = subprocess.run(["ps", "-o", "ppid=,stat=", "-p", str(pid)], capture_output=True, text=True,
                             timeout=5)
        ppid, stat = out.stdout.split()
        return int(ppid), stat[:1]
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def _alive(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    info = _proc(pid)
    return info is None or info[1] != "Z"  # a zombie has exited


def _ancestors(pid: int, limit: int = 32) -> list[int]:
    """Parent, grandparent, ... of ``pid`` (stopping at init), so a client that starts its
    MCP servers through a helper process still counts as the session's client."""
    out, seen = [], {pid}
    while len(out) < limit:
        info = _proc(pid)
        if not info or info[0] <= 1 or info[0] in seen:
            break
        pid = info[0]
        seen.add(pid)
        out.append(pid)
    return out


def _epoch(ts: Any) -> float | None:
    if not isinstance(ts, str) or not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _pick_record(records: Any, app: str) -> dict | None:
    """The client's own status record: no ``id`` (ids are sub-tasks it reports alongside),
    newest by parsed time. Sub-task records never stand in for it."""
    if not isinstance(records, list):
        raise RexError("unexpected program_status output")
    own = [r for r in records if isinstance(r, dict) and r.get("app") == app and not r.get("id")]
    return max(own, key=lambda r: _epoch(r.get("updated_at")) or float("-inf"), default=None)


def check(bus: Bus, session_id: str | None = None, block: str | None = None, agent: str | None = None,
          run: Runner | None = None) -> dict:
    """Report on one session (or one block, given the agent expected in it)."""
    run = run or subprocess.run
    checks: list[dict] = []

    def add(name: str, result: str, detail: str) -> None:
        checks.append({"check": name, "result": result, "detail": detail})

    session = None
    server = None
    if session_id:
        session = bus.session(session_id)
        if not session:
            raise ValueError(f"no session {session_id}")
        if agent and agent != session["agent_type"]:
            raise ValueError(f"session {session_id} is a {session['agent_type']} session, not {agent}")
        agent = session["agent_type"]
        block = block or session["rex_block"]
        server = session["rex_server"]
        if session["rex_session"] and session["rex_block"]:
            add("rex_env", PASS, "REX_SESSION and REX_BLOCK were recorded when the session registered")
        else:
            hint = (" (Codex needs env_vars on its agent-bus entry: run `agent-bus setup-codex`, then restart codex)"
                    if agent == "codex" else "")
            add("rex_env", FAIL, "the session registered without REX_SESSION/REX_BLOCK: not started inside Rex, "
                "or its client didn't pass them to the MCP server" + hint)
    if agent not in APPS:
        raise ValueError("pass --agent (claude, codex or antigravity) with --block")
    report = {"session": session_id, "agent": agent, "block": block, "checks": checks}

    client_pids: list[int] = []
    if session:
        if _alive(session["pid"]):
            client_pids = _ancestors(session["pid"])
            add("mcp_server", PASS, f"server pid {session['pid']} is running")
        else:
            add("mcp_server", FAIL, f"server pid {session['pid']} is not running; the session has ended")

    if not block:
        add("block", FAIL, "no Rex block to check")
        return _finish(report)
    try:
        info = _rex(run, server, "block", "inspect", block, "--json")
        meta = info.get("block")
        if not isinstance(meta, dict) or not isinstance(meta.get("block_id"), str) or not meta["block_id"]:
            raise RexError("rex block inspect returned no block id")
        label = meta.get("label")
        add("block", PASS, f"block exists ({meta['block_id']}"
            + (f", label {label!r})" if isinstance(label, str) and label else ")"))
        block = meta["block_id"]
    except RexError as e:
        add("block", FAIL, f"can't inspect block: {e}")
        return _finish(report)

    app = APPS[agent]
    try:
        rec = _pick_record(_rex(run, server, "block", "call", "program_status", "--block", block).get("records"), app)
        proc = _rex(run, server, "block", "call", "process", "--block", block)
    except RexError as e:
        add("app", FAIL, f"can't read the block's status or process: {e}")
        return _finish(report)

    if not rec:
        add("app", FAIL, f"no OSC 7501 status of its own from {app} in this block")
        return _finish(report)
    add("app", PASS, f"{app} reports status in this block")

    fg = proc.get("foreground") if isinstance(proc.get("foreground"), dict) else {}
    owner_info = rec.get("owner") if isinstance(rec.get("owner"), dict) else {}
    owner = owner_info.get("pid")
    if isinstance(owner, int) and owner == fg.get("pid"):
        add("owner", PASS, f"status owner pid {owner} is the foreground process ({fg.get('name')})")
    else:
        add("owner", FAIL, f"status owner pid {owner} is not the foreground process "
            f"(pid {fg.get('pid')}, {fg.get('name')}); the status may be stale")
    if session and session_alive(checks):
        if isinstance(owner, int) and owner in client_pids:
            add("client", PASS, f"the status comes from this session's own client (pid {owner})")
        else:
            add("client", FAIL, f"status owner pid {owner} isn't a parent of this session's server "
                f"(parents: {', '.join(map(str, client_pids)) or 'unknown'}); the session isn't running in this block")

    state = rec.get("state")
    if isinstance(state, str) and state:
        age = _epoch(rec.get("updated_at"))
        add("state", INFO, state + (f" (last reported {int(time.time() - age)}s ago)" if age else ""))
    else:
        add("state", FAIL, "the status record has no state")
    return _finish(report)


def session_alive(checks: list[dict]) -> bool:
    return any(c["check"] == "mcp_server" and c["result"] == PASS for c in checks)


def _finish(report: dict) -> dict:
    # Always present, whatever else failed, so the report's shape doesn't vary.
    report["checks"].append({"check": "input_ready", "result": UNAVAILABLE, "detail": INPUT_READY})
    report["ok"] = not any(c["result"] == FAIL for c in report["checks"])
    return report


def latest_sessions(bus: Bus, agent: str | None = None) -> list[str]:
    """The most recent live session of each agent type (or the most recent one, if none is live)."""
    rows = bus._all("SELECT id, agent_type, pid FROM sessions ORDER BY last_seen DESC, rowid DESC")
    live: dict[str, str] = {}
    latest: dict[str, str] = {}
    for r in rows:
        if agent and r["agent_type"] != agent:
            continue
        latest.setdefault(r["agent_type"], r["id"])
        if r["agent_type"] not in live and _alive(r["pid"]):
            live[r["agent_type"]] = r["id"]
    return [live.get(a, sid) for a, sid in latest.items()]


def format_report(r: dict) -> str:
    head = f"{r['session'] or '-'} {r['agent']}" + (f" in {r['block']}" if r["block"] else "")
    lines = [head]
    for c in r["checks"]:
        lines.append(f"  {c['result']:<11} {c['check']:<12} {c['detail']}")
    lines.append("  ok" if r["ok"] else "  FAILED")
    return "\n".join(lines)
