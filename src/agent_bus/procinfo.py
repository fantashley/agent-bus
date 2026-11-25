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
import re
import subprocess
import threading
from typing import Callable

CODEX_SHARED_DAEMON = "codex_shared_daemon"


def reap_later(child: subprocess.Popen) -> None:
    """Wait for a detached ``child`` on a daemon thread, so a long-running parent (an
    ``agent-bus mcp`` server) doesn't keep it as a zombie once it exits (#126).  A short-lived
    parent just exits, leaving the child to init."""
    threading.Thread(target=child.wait, name=f"reap-{child.pid}", daemon=True).start()


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


def alive(pid: int) -> bool:
    """Whether ``pid`` is a running process (not gone, not a zombie).  A pid that can't
    be read counts as alive: callers use this to decide whether something is safe to
    take over, so an unknown answer must not look like "exited"."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:  # e.g. EPERM: it exists, owned by someone else
        return True
    info = proc(pid)
    return info is None or info[1] != "Z"


def start_time(pid: int) -> str | None:
    """An opaque token for when a process started, so a reused pid isn't mistaken for it."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            return "linux:" + f.read().rsplit(")", 1)[1].split()[19]  # field 22, starttime
    except (OSError, IndexError):
        pass
    try:  # macOS and other systems without /proc
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
        return "ps:" + " ".join(out.stdout.split()) if out.returncode == 0 and out.stdout.strip() else None
    except (OSError, subprocess.TimeoutExpired):
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


# Agent harnesses whose shell tools could run the human's CLI commands (#84): the
# executable names they run as, and the npm packages that ship them as scripts.
HARNESS_EXES = frozenset({"claude", "claude-code", "codex", "agy", "copilot"})
HARNESS_PACKAGES = ("@anthropic-ai/claude-code", "@openai/codex", "@github/copilot")
SCRIPT_RUNTIME = re.compile(r"(node|nodejs|bun|deno|python[0-9.]*)")


def exe_name(path: str) -> str:
    """An executable's name without its directory, a login shell's leading ``-`` or a Nix
    wrapper's decoration (``.claude-wrapped``, ``.claude-unwrapped`` -> ``claude``)."""
    name = os.path.basename(path).lstrip("-")
    if name.startswith(".") and name.endswith(("-wrapped", "-unwrapped")):
        name = name[1:].rsplit("-", 1)[0]
    return name


def harness_in_argv(args: list[str]) -> str | None:
    """The agent harness a process's argv runs, if any: an exact executable name, or the
    script a runtime such as node was given.  Never a substring match on the rest of argv."""
    if not args:
        return None
    name = exe_name(args[0])
    if name in HARNESS_EXES:
        return name
    if SCRIPT_RUNTIME.fullmatch(name):
        script = next((a for a in args[1:] if not a.startswith("-")), None)
        if script:
            if exe_name(script) in HARNESS_EXES:
                return exe_name(script)
            for pkg in HARNESS_PACKAGES:
                if f"/{pkg}/" in script.replace("\\", "/"):
                    return pkg
    return None


def harness_ancestor(pid: int, ancestors_of: Callable[[int], list[int]] = ancestors,
                     argv_of: Callable[[int], list[str] | None] = argv) -> str | None:
    """The first agent harness above ``pid``, as ``"<name> (pid N)"``, or None.  An
    ancestor whose argv can't be read is skipped."""
    for p in ancestors_of(pid):
        found = harness_in_argv(argv_of(p) or [])
        if found:
            return f"{found} (pid {p})"
    return None
