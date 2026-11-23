"""Register agent-bus with GitHub Copilot CLI (issue #2).

Copilot keeps its own MCP servers in ``$COPILOT_HOME/mcp-config.json``
(``~/.copilot`` by default), a plain JSON file with a top-level ``mcpServers``
object that ``copilot mcp add`` also writes.  Only the ``agent-bus`` entry is
added or fixed; every other server and key is left as it is, and a file that
isn't a JSON object is refused rather than rewritten.  No write happens when the
entry already matches.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

NAME = "agent-bus"
# A participant pin a user added to the entry (`--participant NAME`, #43): kept as it is.
_PIN = re.compile(r"[a-z][a-z0-9-]{0,31}")  # config.PARTICIPANT_NAME
# Names that can never be a participant on the copilot harness.
_NOT_COPILOT = frozenset({"human", "system", "claude", "codex", "antigravity"})


class SetupError(Exception):
    pass


def copilot_home() -> Path:
    return Path(os.environ.get("COPILOT_HOME") or Path.home() / ".copilot")


def config_path() -> Path:
    return copilot_home() / "mcp-config.json"


def pin(have: dict | None) -> list[str]:
    """The existing entry's args when they pin Copilot to a participant, as `agent-bus mcp`
    itself parses them; [] otherwise."""
    from .cli import parse_mcp_args  # cli imports this module lazily
    args = (have or {}).get("args") if isinstance(have, dict) else None
    if not isinstance(args, list) or not args or args[0] != "mcp" or not all(isinstance(a, str) for a in args):
        return []
    ns = parse_mcp_args(args[1:])
    if ns is None or ns.agent != "copilot" or ns.participant is None:
        return []
    if not _PIN.fullmatch(ns.participant) or ns.participant in _NOT_COPILOT:
        return []
    return list(args)


def wanted(bin_path: str, have: dict | None = None) -> dict:
    """The entry agent-bus wants: the user's other keys on it (env, cwd, ...) kept."""
    base = dict(have) if isinstance(have, dict) else {}
    return {**base, "type": "local", "command": bin_path, "args": pin(have) or ["mcp", "--agent", "copilot"],
            "tools": ["*"]}


def ensure(bin_path: str, path: Path | None = None) -> str:
    """Add or fix the agent-bus entry: "added", "updated" or "unchanged"."""
    path = path or config_path()
    data: dict = {}
    if path.exists():
        try:
            text = path.read_text()
            data = json.loads(text) if text.strip() else {}
        except (OSError, UnicodeError, json.JSONDecodeError) as e:
            raise SetupError(f"can't read {path}: {e}") from e
        if not isinstance(data, dict):
            raise SetupError(f"{path} is not a JSON object; leaving it alone")
    servers = data.get("mcpServers", {})
    if not isinstance(servers, dict):
        raise SetupError(f"`mcpServers` in {path} is not an object; leaving it alone")
    have = servers.get(NAME)
    want = wanted(bin_path, have)
    if have == want:
        return "unchanged"
    data = {**data, "mcpServers": {**servers, NAME: want}}
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    mode = (path.stat().st_mode & 0o777) if path.exists() else 0o600
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.chmod(tmp, mode)
        os.replace(tmp, path)  # atomic: Copilot reads the old file or the new one whole
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return "added" if have is None else "updated"
