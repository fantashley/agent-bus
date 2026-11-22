"""Register agent-bus with Codex, forwarding Rex's location variables.

``codex mcp add`` can't set ``env_vars``, and Codex starts MCP servers with a
filtered environment, so without it the server never sees REX_SESSION or
REX_BLOCK and can't record which Rex block a Codex session is in.  Re-running
``codex mcp add`` also drops ``env_vars``.  So: add the entry with the CLI, then
put ``env_vars`` into that one table, and check the result through ``codex mcp
get`` so a format we don't understand fails loudly instead of being half-applied.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import tomllib
from pathlib import Path
from typing import Callable

NAME = "agent-bus"
ENV_VARS = ["REX_SESSION", "REX_BLOCK", "REX_SERVER"]

Runner = Callable[..., subprocess.CompletedProcess]


class SetupError(Exception):
    pass


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def wanted(bin_path: str) -> dict:
    return {"command": bin_path, "args": ["mcp", "--agent", "codex"], "env_vars": ENV_VARS}


def current(codex: str, run: Runner = subprocess.run) -> dict | None:
    out = run([codex, "mcp", "get", NAME, "--json"], capture_output=True, text=True, timeout=30)
    if out.returncode != 0:
        return None
    try:
        t = json.loads(out.stdout).get("transport") or {}
    except (json.JSONDecodeError, AttributeError):
        return None
    return {"command": t.get("command"), "args": t.get("args") or [], "env_vars": t.get("env_vars") or []}


_HEADER = re.compile(r'^\s*\[\s*mcp_servers\s*\.\s*(?:"agent-bus"|\'agent-bus\'|agent-bus)\s*\]\s*(?:#.*)?$')


def add_env_vars(text: str, names: list[str] = ENV_VARS) -> str:
    """Return ``text`` with ``env_vars`` set in the ``[mcp_servers.agent-bus]`` table.

    Only that table changes; the result must parse and every other key must be
    unchanged, or this raises.
    """
    before = tomllib.loads(text)
    lines = text.splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines) if _HEADER.match(line)), None)
    if start is None:
        raise SetupError("no [mcp_servers.agent-bus] table in Codex's config.toml")
    end = next((i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("[")), len(lines))
    body = [line for line in lines[start + 1:end] if not re.match(r"\s*env_vars\s*=", line)]
    if any(re.match(r"\s*env_vars\s*=", line) and "]" not in line for line in lines[start + 1:end]):
        raise SetupError("env_vars in Codex's config.toml spans lines; not editing it")
    new_line = "env_vars = " + json.dumps(names) + "\n"
    out = "".join(lines[:start + 1] + [new_line] + body + lines[end:])
    after = tomllib.loads(out)
    entry = after.get("mcp_servers", {}).get(NAME, {})
    if entry.get("env_vars") != names:
        raise SetupError("could not set env_vars in Codex's config.toml")
    # Nothing else may change.
    strip = lambda d: {**d, "mcp_servers": {k: v for k, v in d.get("mcp_servers", {}).items() if k != NAME}}
    old_entry = {k: v for k, v in before.get("mcp_servers", {}).get(NAME, {}).items() if k != "env_vars"}
    new_entry = {k: v for k, v in entry.items() if k != "env_vars"}
    if strip(before) != strip(after) or old_entry != new_entry:
        raise SetupError("editing Codex's config.toml would change other settings; not writing it")
    return out


def _write_atomic(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".config.toml.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp, path.stat().st_mode & 0o777)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def ensure(bin_path: str, codex: str | None = None, run: Runner = subprocess.run) -> str:
    """Make Codex's agent-bus entry match; return "unchanged" or "updated"."""
    codex = codex or shutil.which("codex")
    if not codex:
        raise SetupError("codex is not on PATH")
    want = wanted(bin_path)
    have = current(codex, run)
    if have == want:
        return "unchanged"
    if have is None or have["command"] != want["command"] or have["args"] != want["args"]:
        if have is not None:
            _check(run([codex, "mcp", "remove", NAME], capture_output=True, text=True, timeout=30), "remove")
        _check(run([codex, "mcp", "add", NAME, "--", bin_path, "mcp", "--agent", "codex"],
                   capture_output=True, text=True, timeout=30), "add")
    path = codex_home() / "config.toml"
    original = path.read_text()
    _write_atomic(path, add_env_vars(original))
    if current(codex, run) != want:
        _write_atomic(path, original)
        raise SetupError("Codex didn't read back the agent-bus entry as written; config.toml restored")
    return "updated"


def _check(out: subprocess.CompletedProcess, what: str) -> None:
    if out.returncode != 0:
        raise SetupError(f"`codex mcp {what}` failed: {(out.stderr or out.stdout).strip()[:300]}")
