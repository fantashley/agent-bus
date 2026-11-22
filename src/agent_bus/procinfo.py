"""Process inspection: parents, argv, and Codex's shared app-server daemon.

By default the Codex TUI connects to a long-running ``codex app-server
--managed-daemon`` (observed with Codex 0.171.0), and that daemon, not the TUI,
starts MCP servers.  They inherit the daemon's environment, so REX_SESSION and
REX_BLOCK describe the Rex block the daemon happened to start in, not the
window the user is typing in.  agent-bus detects that and doesn't record a Rex
location for such sessions (issue #6, agreed on agent-bus thread t-536092).

Detection is best-effort.  If argv can't be read, or Codex changes how it runs
the daemon, a stale location can still be recorded; rex-check's owner/client
checks remain the backstop for that.
"""

from __future__ import annotations

import os
import subprocess
from typing import Callable

CODEX_SHARED_DAEMON = "codex_shared_daemon"


def proc(pid: int) -> tuple[int, str] | None:
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


def ancestors(pid: int, limit: int = 32) -> list[int]:
    """Parent, grandparent, ... of ``pid``, stopping at init, on a cycle, or after ``limit``."""
    out, seen = [], {pid}
    while len(out) < limit:
        info = proc(pid)
        if not info or info[0] <= 1 or info[0] in seen:
            break
        pid = info[0]
        seen.add(pid)
        out.append(pid)
    return out


def argv(pid: int) -> list[str] | None:
    """A process's argument vector, or None if it can't be read."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            raw = f.read()
        if raw:
            return [a.decode(errors="replace") for a in raw.rstrip(b"\0").split(b"\0")]
    except OSError:
        pass
    try:  # without /proc, ps joins argv with spaces; good enough for flag matching
        out = subprocess.run(["ps", "-o", "args=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
        return out.stdout.split() if out.returncode == 0 and out.stdout.strip() else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def is_shared_codex_daemon(args: list[str]) -> bool:
    """A Codex executable running ``app-server`` as a server for several clients:
    ``--managed-daemon``, or ``--listen`` on anything but stdio."""
    if not args or "codex" not in os.path.basename(args[0]).lower():
        return False
    rest = args[1:]
    if "app-server" not in rest:
        return False
    if "--managed-daemon" in rest:
        return True
    for i, a in enumerate(rest):
        target = a.split("=", 1)[1] if a.startswith("--listen=") else (
            rest[i + 1] if a == "--listen" and i + 1 < len(rest) else None)
        if target is not None and not target.startswith("stdio"):
            return True
    return False


def shared_codex_daemon(pid: int, ancestors_of: Callable[[int], list[int]] = ancestors,
                        argv_of: Callable[[int], list[str] | None] = argv) -> int | None:
    """The pid of a shared Codex app-server daemon above ``pid``, if there is one."""
    for p in ancestors_of(pid):
        args = argv_of(p)
        if args and is_shared_codex_daemon(args):
            return p
    return None
