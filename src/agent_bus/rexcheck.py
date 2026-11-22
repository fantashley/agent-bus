"""``agent-bus rex-check``: what Rex can and can't tell us about an agent's block.

v1.1 decision (thread t-eda8ca, Claude/Codex/Antigravity): agent-bus never
types into a terminal.  Rex reports whether an agent says it is idle, but an
idle agent with a half-typed draft looks exactly like one with an empty input
box, and a check followed by a separate write could always race a human typing.
So this command only reports.  Each fact is checked separately, anything
missing or unreadable fails, and input readiness is always reported as
unavailable, never inferred.

Read-only: it calls ``rex block inspect`` and ``rex block call`` with the
``program_status`` and ``process`` methods, which don't change the block.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime
from typing import Callable

from .db import Bus

# OSC 7501 app names each client reports (measured on Rex with all three).
APPS = {"claude": "claude-code", "codex": "codex", "antigravity": "antigravity"}

PASS, FAIL, INFO, SKIP, UNAVAILABLE = "pass", "fail", "info", "skip", "unavailable"

Runner = Callable[..., subprocess.CompletedProcess]


def _rex(run: Runner, *args: str) -> dict:
    out = run(["rex", *args], capture_output=True, text=True, timeout=15)
    if out.returncode != 0:
        raise RuntimeError((out.stderr or out.stdout).strip().splitlines()[0][:200] if (out.stderr or out.stdout)
                           else f"exit {out.returncode}")
    return json.loads(out.stdout)


def _parent_pid(pid: int, run: Runner) -> int | None:
    try:
        with open(f"/proc/{pid}/stat") as f:
            # Field 4, after the parenthesised command name (which may contain spaces).
            return int(f.read().rsplit(")", 1)[1].split()[1])
    except (OSError, IndexError, ValueError):
        pass
    try:
        out = run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
        return int(out.stdout.strip()) if out.returncode == 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _age(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        return time.time() - datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def check(bus: Bus, session_id: str | None = None, block: str | None = None, agent: str | None = None,
          run: Runner = subprocess.run) -> dict:
    """Report on one session (or one block, given the agent expected in it)."""
    checks: list[dict] = []

    def add(name: str, result: str, detail: str) -> None:
        checks.append({"check": name, "result": result, "detail": detail})

    session = None
    if session_id:
        session = bus.session(session_id)
        if not session:
            raise ValueError(f"no session {session_id}")
        agent = session["agent_type"]
        block = block or session["rex_block"]
        if session["rex_session"] and session["rex_block"]:
            add("rex_env", PASS, "REX_SESSION and REX_BLOCK were recorded when the session registered")
        else:
            hint = (" (Codex needs env_vars on its agent-bus entry: run `agent-bus setup-codex`, then restart codex)"
                    if agent == "codex" else "")
            add("rex_env", FAIL, "the session registered without REX_SESSION/REX_BLOCK: not started inside Rex, "
                "or its client didn't pass them to the MCP server" + hint)
    if agent not in APPS:
        raise ValueError("pass --agent (claude, codex or antigravity) with --block")

    client_pid = None
    if session:
        if _alive(session["pid"]):
            client_pid = _parent_pid(session["pid"], run)
            add("mcp_server", PASS, f"server pid {session['pid']} is running (client pid {client_pid or 'unknown'})")
        else:
            add("mcp_server", FAIL, f"server pid {session['pid']} is not running; the session has ended")

    report = {"session": session_id, "agent": agent, "block": block, "checks": checks}
    if not block:
        add("block", FAIL, "no Rex block to check")
        return _finish(report)
    try:
        info = _rex(run, "block", "inspect", block, "--json")
        label = (info.get("block") or {}).get("label")
        add("block", PASS, f"block exists" + (f" (label {label!r})" if label else ""))
    except (RuntimeError, OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as e:
        add("block", FAIL, f"can't inspect block: {e}")
        return _finish(report)

    try:
        records = _rex(run, "block", "call", "program_status", "--block", block).get("records") or []
        proc = _rex(run, "block", "call", "process", "--block", block)
    except (RuntimeError, OSError, subprocess.TimeoutExpired, json.JSONDecodeError, AttributeError) as e:
        add("app", FAIL, f"can't read the block's status or process: {e}")
        return _finish(report)

    app = APPS[agent]
    mine = [r for r in records if isinstance(r, dict) and r.get("app") == app]
    # A client's own status has no id; ids are for sub-tasks it reports alongside.
    main = [r for r in mine if not r.get("id")] or mine
    rec = max(main, key=lambda r: r.get("updated_at") or "", default=None)
    if not rec:
        others = sorted({r.get("app") for r in records if isinstance(r, dict) and r.get("app")})
        add("app", FAIL, f"no OSC 7501 status from {app}" + (f" (only {', '.join(others)})" if others else ""))
    else:
        add("app", PASS, f"{app} reports status in this block")

    fg = (proc.get("foreground") or {}) if isinstance(proc, dict) else {}
    owner = ((rec or {}).get("owner") or {}).get("pid")
    if rec and owner and owner == fg.get("pid"):
        add("owner", PASS, f"status owner pid {owner} is the foreground process ({fg.get('name')})")
    elif rec:
        add("owner", FAIL, f"status owner pid {owner} is not the foreground process "
            f"(pid {fg.get('pid')}, {fg.get('name')}); the status may be stale")
    if session and rec:
        if client_pid and owner == client_pid:
            add("client", PASS, "the status comes from this session's own client")
        else:
            add("client", FAIL, f"status owner pid {owner} isn't this session's client (pid {client_pid}); "
                "the session isn't running in this block")

    if rec:
        age = _age(rec.get("updated_at"))
        add("state", INFO, f"{rec.get('state')}" + (f" (last reported {int(age)}s ago)" if age is not None else ""))
    add("input_ready", UNAVAILABLE, "no client or Rex signal says whether the input box is empty "
        "(an idle agent with a draft reports idle too); agent-bus never types into terminals")
    return _finish(report)


def _finish(report: dict) -> dict:
    report["ok"] = not any(c["result"] == FAIL for c in report["checks"])
    return report


def latest_sessions(bus: Bus) -> list[str]:
    """The most recent session of each agent type."""
    rows = bus._all("SELECT id, agent_type FROM sessions ORDER BY last_seen DESC")
    seen, out = set(), []
    for r in rows:
        if r["agent_type"] not in seen:
            seen.add(r["agent_type"])
            out.append(r["id"])
    return out


def format_report(r: dict) -> str:
    head = f"{r['session'] or '-'} {r['agent']}" + (f" in {r['block']}" if r["block"] else "")
    lines = [head]
    for c in r["checks"]:
        lines.append(f"  {c['result']:<11} {c['check']:<12} {c['detail']}")
    lines.append("  ok" if r["ok"] else "  FAILED")
    return "\n".join(lines)
