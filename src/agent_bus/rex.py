"""Optional Rex integration: viewer panes and OSC 7501 program status.

Rex is a view layer only.  Viewers are output-only ``agent-bus tail``
processes; nothing here ever types into a block.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys

from . import config as cfg


def in_rex() -> bool:
    return bool(os.environ.get("REX_SESSION")) and shutil.which("rex") is not None


def osc7501(state: str, msg: str | None = None, id: str | None = None, title: str | None = None,
            kind: str | None = None, app: str = "agent-bus") -> str:
    pairs = [f"state={state}"]
    if id:
        pairs.append(f"id={id}")
    if kind and state == "blocked":
        pairs.append(f"kind={kind}")
    pairs.append(f"app={app}")
    if title:
        pairs.append("title=" + _b64(title, 192))
    if msg:
        pairs.append("msg=" + _b64(msg, 2048))
    return "\x1b]7501;" + ":".join(pairs) + "\x1b\\"


def notify(title: str, body: str) -> str:
    """A desktop notification for terminals that support OSC 777 (notify) or OSC 9."""
    clean = lambda text, n: "".join(ch if ch.isprintable() and ch not in ";\x07" else " "  # noqa: E731
                                    for ch in " ".join(text.split()))[:n]
    return f"\x1b]777;notify;{clean(title, 64)};{clean(body, 256)}\x07\x1b]9;{clean(body, 256)}\x07"


def _b64(text: str, limit: int) -> str:
    clean = "".join(ch if ch.isprintable() else " " for ch in " ".join(text.split()))
    data = clean.encode()[:limit].decode(errors="ignore").encode()
    return base64.b64encode(data).decode()


SHARED_DAEMON_NOTE = ("not opening viewers: this Codex session runs under Codex's shared app-server daemon, so its "
                      "Rex location is the daemon's, not this window's. Run `agent-bus watch {thread}` from a Rex "
                      "shell, or start Codex with `codex --no-daemon`")


def open_viewers(thread: str, peers: list[str] | None = None, location: dict | None = None) -> dict:
    """Open a labelled thread watcher split, plus one output-only viewer per peer.

    ``location`` is the calling session's recorded Rex location (rex_session,
    rex_block, rex_reason).  When given, it is used instead of this process's
    environment, which for a session under Codex's shared daemon is the daemon's.
    A session with a rex_reason gets no viewers at all, never a split of some other block.
    """
    if location is not None:
        if location.get("rex_reason"):
            return {"opened": [], "note": SHARED_DAEMON_NOTE.format(thread=thread)}
        if not (location.get("rex_session") and location.get("rex_block")) or shutil.which("rex") is None:
            return {"opened": [], "note": "this session has no recorded Rex location (or rex is not on PATH)"}
        anchor = location["rex_block"]
    elif not in_rex():
        return {"opened": [], "note": "not running inside Rex (REX_SESSION unset or rex not on PATH)"}
    else:
        anchor = os.environ.get("REX_BLOCK")
    me = cfg.self_command()
    opened, errors = [], []
    specs = [("Agent Bus " + thread, [*me, "tail", thread], None)]
    for p in peers or []:
        specs.append((f"bus:{thread}/{p}", [*me, "tail", thread, "--peer", p], p))
    for i, (label, cmd, _peer) in enumerate(specs):
        argv = ["rex", "split", "--split", "below" if i == 0 else "right", "--focus=false",
                "--label", label, "--shell", "none", "--json"]
        if anchor:
            argv += ["-b", anchor]
        if location and location.get("rex_server"):
            argv[1:1] = ["-S", location["rex_server"]]
        argv += ["--", *cmd]
        try:
            out = subprocess.run(argv, capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired) as e:
            errors.append(f"{label}: {e}")
            continue
        if out.returncode != 0:
            errors.append(f"{label}: {out.stderr.strip()[:200]}")
            continue
        opened.append(label)
        # Chain peer viewers off the previous viewer so the agent's own block isn't split again.
        anchor = _block_id(out.stdout) or anchor
    res: dict = {"opened": opened}
    if errors:
        res["errors"] = errors
    return res


def _block_id(output: str) -> str | None:
    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        return None
    if isinstance(data, dict):
        for key in ("block_id", "block", "id"):
            val = data.get(key)
            if isinstance(val, str):
                return val
            if isinstance(val, dict) and isinstance(val.get("id"), str):
                return val["id"]
    return None


def stdout_is_tty() -> bool:
    try:
        return sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False
