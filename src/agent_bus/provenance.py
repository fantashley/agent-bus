"""Where a human action came from (#108).

The CLI commands that act as the human user record, with each action, the process that
ran them: its parent chain (executable names and a short argv), the terminal it was
started from, whether the #84 guard was overridden, and which agent-harness environment
markers were set.  ``agent-bus history`` and ``tail`` show it, and flag what looks like
an agent rather than the human.

This makes a spoof visible, not impossible.  Any process running as the same user can
write ``bus.db`` directly, provenance included, or run the CLI from a process chain that
looks clean.  The README's "Trust boundary" section says exactly where the gap is.
"""

from __future__ import annotations

import os
import re
from typing import Callable

from . import procinfo

VERSION = 1
# Agent harnesses set these in the shells their tools run commands in (#84).  Session
# markers only, never config or auth variables a human may export in their own shell too.
HARNESS_ENV = ("AI_AGENT", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CODEX_THREAD_ID", "CODEX_SANDBOX", "CODEX_CI")
# How much of the parent chain is kept, and of each ancestor's argv.
MAX_DEPTH = 16
MAX_ARGS = 4
MAX_LINE = 120
# A parent that adopts orphans: a process whose parent this is was detached from its own.
REAPERS = frozenset({"systemd", "init", "launchd"})
# Control characters (C0, DEL, C1): a forged record must not drive the human's terminal.
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def printable(text: object) -> str:
    return _CONTROL.sub("?", str(text))


# Arguments kept as they are: subcommands agents and terminals run under.  Any other operand
# (a value, a shell command, a URL, a PIN) could hold a credential and becomes "*".
SAFE_WORDS = frozenset({"app-server", "exec", "run", "server", "serve", "start", "daemon", "resume", "attach",
                        "new", "mcp", "chat", "agent", "login", "--"})
# Only these read as paths: a token with a "/" in it (base64, a URL) is not one.
PATH_PREFIXES = ("/", "./", "../", "~/")
_BASENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,23}")  # a script's name, not a long token
_EXE = re.compile(r"[A-Za-z0-9._+-]{1,40}")
_LONG_FLAG = re.compile(r"--[A-Za-z0-9][A-Za-z0-9_-]{0,30}")
_SHORT_FLAG = re.compile(r"-[A-Za-z0-9]")


def _short_arg(a: str, prev: str | None = None) -> str:
    """One argument, redacted before it is stored (#108): a long flag keeps its name but not
    an ``=value``; a short flag is kept alone, ``-*`` with anything attached; a path (an
    explicit one: see PATH_PREFIXES) keeps only its last component if that is short; a flag's
    separate value is always ``*``; a known subcommand is kept; anything else becomes ``*``."""
    if a in SAFE_WORDS:
        return a
    if prev is not None and prev.startswith("-") and prev != "--" and "=" not in prev and not a.startswith("-"):
        return "*"  # a flag's value, whatever it looks like (a token can start with "/")
    name, eq, _ = a.partition("=")
    if _LONG_FLAG.fullmatch(name):
        return name + ("=*" if eq else "")
    if a.startswith("-"):
        return a if _SHORT_FLAG.fullmatch(a) else "-*"
    if a.startswith(PATH_PREFIXES) and not any(c.isspace() for c in a) and ":" not in a:
        base = os.path.basename(a.rstrip("/"))  # no directories: they say little and leak paths
        return ".../" + base if _BASENAME.fullmatch(base) else ".../*"
    return "*"


def short_argv(args: list[str] | None) -> str | None:
    """An ancestor's argv for display: its executable's name and the first few arguments,
    each redacted (see _short_arg), on one line."""
    if not args:
        return None
    exe = procinfo.exe_name(args[0])
    parts = [exe if _EXE.fullmatch(exe) else "*"]
    parts += [_short_arg(args[i], args[i - 1] if i > 1 else None) for i in range(1, min(len(args), 1 + MAX_ARGS))]
    if len(args) > 1 + MAX_ARGS:
        parts.append("...")
    line = printable(" ".join(parts))
    return line if len(line) <= MAX_LINE else line[:MAX_LINE - 3] + "..."


def controls(fd: int) -> bool:
    """Whether ``fd`` is this process's controlling terminal: ``tcgetpgrp`` succeeds only then
    (not after ``setsid``, nor on another pty a descriptor was pointed at, #108)."""
    try:
        os.tcgetpgrp(fd)
        return True
    except OSError:
        return False


def terminal() -> tuple[str | None, bool]:
    """The terminal on stdin, stdout or stderr (the first found), and whether it is this
    process's controlling terminal."""
    for fd in (0, 1, 2):
        try:
            if os.isatty(fd):
                name = os.ttyname(fd)
                # A pty's master side answers tcgetpgrp too, for its slave's session.
                return name, controls(fd) and not name.endswith("ptmx")
        except OSError:
            pass
    return None, False


def ancestry(pid: int, ancestors_of: Callable[[int], list[int]] = procinfo.ancestors,
             argv_of: Callable[[int], list[str] | None] = procinfo.argv) -> tuple[list[dict], bool]:
    """``pid``'s parent chain, nearest first, as ``{pid, argv, harness}`` entries (``harness``
    only on an agent harness, read from the full argv), and whether it was cut short."""
    chain = ancestors_of(pid)
    out = []
    for p in chain[:MAX_DEPTH]:
        args = argv_of(p)
        entry: dict = {"pid": p, "argv": short_argv(args)}
        found = procinfo.harness_in_argv(args or [])
        if found:
            entry["harness"] = found
        out.append(entry)
    return out, len(chain) > MAX_DEPTH


def collect(command: str, env: dict | os._Environ | None = None, pid: int | None = None,
            ancestry_of: Callable[[int], tuple[list[dict], bool]] | None = None,
            terminal_of: Callable[[], tuple[str | None, bool]] | None = None) -> dict:
    """The provenance record of one human action, run as ``command`` by this process."""
    env = os.environ if env is None else env
    pid = os.getpid() if pid is None else pid
    chain, truncated = (ancestry_of or ancestry)(pid)
    name, ctty = (terminal_of or terminal)()
    rec = {
        "v": VERSION,
        "command": command,
        "pid": pid,
        "ancestry": chain,
        "tty": name,
        "controlling_tty": bool(name) and ctty,
        "guard_overridden": env.get("AGENT_BUS_ALLOW_HUMAN") == "1",
        "harness_env": [v for v in HARNESS_ENV if env.get(v)],
    }
    if truncated:
        rec["ancestry_truncated"] = True
    return rec


def well_formed(rec: object) -> bool:
    """Whether a stored record has the shape the views read (anything may write one)."""
    if not isinstance(rec, dict):
        return False
    chain, env = rec.get("ancestry"), rec.get("harness_env", [])
    return (isinstance(chain, list) and all(isinstance(a, dict) and isinstance(a.get("argv"), (str, type(None)))
                                            and isinstance(a.get("harness"), (str, type(None))) for a in chain)
            and isinstance(env, list) and all(isinstance(v, str) for v in env)
            and isinstance(rec.get("tty"), (str, type(None))))


def flags(rec: dict | None, records: int = 1) -> list[tuple[str, str]]:
    """Why a human action's provenance looks like something other than the human at their
    own terminal, as ``(short code, detail)`` pairs; empty when nothing does.  Codes carry
    no pids or paths (peers may see them); details are for the human.

    ``rec`` None is a row written without the CLI recording it: an agent-bus from before
    #108, or something writing ``bus.db`` directly.  That is never shown as clean."""
    if rec is None:
        return [("none recorded", "no provenance recorded: not posted through this agent-bus CLI "
                 "(an older version, or written straight into bus.db)")]
    if not well_formed(rec):
        return [("unreadable", "provenance record unreadable")]
    out = []
    if records > 1:
        out.append(("several records", f"{records} provenance records for one action"))
    if rec.get("guard_overridden"):
        out.append(("guard overridden", "AGENT_BUS_ALLOW_HUMAN=1 overrode the agent-shell guard"))
    if rec.get("harness_env"):
        names = ", ".join(str(v) for v in rec["harness_env"])
        out.append(("harness env", f"agent harness environment set: {names}"))
    harnesses = [f"{a['harness']} (pid {a.get('pid')})" for a in rec["ancestry"] if a.get("harness")]
    if harnesses:
        out.append(("agent in ancestry", f"agent harness in its parent chain: {', '.join(harnesses)}"))
    if not rec.get("tty"):
        out.append(("no tty", "not run from a terminal (no tty on stdin, stdout or stderr)"))
    elif not rec.get("controlling_tty"):
        out.append(("no controlling tty", f"{rec['tty']} is not its controlling terminal (setsid, or a redirected descriptor)"))
    first = rec["ancestry"][0] if rec["ancestry"] else None
    if first is None:
        out.append(("detached", "no parent chain recorded (detached, or unreadable)"))
    elif procinfo.exe_name(str(first.get("argv") or "").split(" ")[0]) in REAPERS:
        out.append(("detached", f"its parent is {first['argv'].split(' ')[0]} (detached from what started it)"))
    return out


def peer_note(rec: dict | None, records: int = 1) -> str:
    """The marker peers get on a flagged human message: short codes only, never the
    ancestry itself.  Empty when nothing is flagged."""
    codes = list(dict.fromkeys(c for c, _ in flags(rec, records)))
    if not codes:
        return ""
    return f"agent-bus flagged where this came from ({', '.join(codes)}): it may not be from the human user"


def chain_line(rec: dict, depth: int = 6) -> str:
    """The parent chain on one line, nearest first: ``zsh[4120] < tmux[900] < ...``."""
    chain = [a for a in rec.get("ancestry") or [] if isinstance(a, dict)]
    parts = [f"{a.get('argv') or '?'}[{a.get('pid')}]" for a in chain[:depth]]
    more = len(chain) - depth
    if more > 0 or rec.get("ancestry_truncated"):
        parts.append(f"+{max(more, 0)} more" + (", cut short" if rec.get("ancestry_truncated") else ""))
    return " < ".join(parts) or "(none)"


def describe(rec: dict | None, records: int = 1) -> tuple[list[str], list[str]]:
    """``(warnings, info)`` lines for the human's views: the flags' details, then where it
    came from."""
    warn = [printable(d) for _, d in flags(rec, records)]
    info = []
    if well_formed(rec):
        info.append(f"from: {chain_line(rec)}")
        info.append(f"tty: {rec.get('tty') or 'none'}"
                    + ("" if not rec.get("tty") or rec.get("controlling_tty") else " (not controlling)")
                    + f"; pid {rec.get('pid')}; `agent-bus {rec.get('command')}`")
    return warn, [printable(x) for x in info]


def summary(rec: dict | None, records: int = 1) -> dict:
    """The provenance of a human action for ``--json`` output."""
    fl = flags(rec, records)
    return {"recorded": rec is not None, "flagged": bool(fl), "flags": [c for c, _ in fl],
            "details": [d for _, d in fl], "record": rec}
