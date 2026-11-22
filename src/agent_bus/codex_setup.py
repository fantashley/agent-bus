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


_ENV_KEY = re.compile(r"""\s*(?:env_vars|"env_vars"|'env_vars')\s*=""")


def _parse(text: str, what: str) -> dict:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise SetupError(f"{what} is not valid TOML ({e}); not editing it") from e


def add_env_vars(text: str, names: list[str] = ENV_VARS) -> str:
    """Return ``text`` with ``env_vars`` set in the ``[mcp_servers.agent-bus]`` table.

    Only that table changes; the result must parse and every other key must be
    unchanged, or this raises :class:`SetupError`.
    """
    before = _parse(text, "Codex's config.toml")
    lines = text.splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines) if _HEADER.match(line)), None)
    if start is None:
        raise SetupError("no [mcp_servers.agent-bus] table in Codex's config.toml")
    end = next((i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("[")), len(lines))
    old = [line for line in lines[start + 1:end] if _ENV_KEY.match(line)]
    for line in old:
        try:  # a value spread over several lines doesn't parse on its own
            tomllib.loads(line)
        except tomllib.TOMLDecodeError:
            raise SetupError("env_vars in Codex's config.toml spans lines; not editing it") from None
    body = [line for line in lines[start + 1:end] if not _ENV_KEY.match(line)]
    header = lines[start] if lines[start].endswith("\n") else lines[start] + "\n"
    new_line = "env_vars = " + json.dumps(names) + "\n"
    out = "".join(lines[:start] + [header, new_line] + body + lines[end:])
    after = _parse(out, "the edited config.toml")
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
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, path.stat().st_mode & 0o777)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _matches(have: dict | None, want: dict) -> bool:
    """Same command and args, and every Rex variable forwarded (others may be too)."""
    return (have is not None and have["command"] == want["command"] and have["args"] == want["args"]
            and set(want["env_vars"]) <= set(have["env_vars"]))


def ensure(bin_path: str, codex: str | None = None, run: Runner = subprocess.run) -> str:
    """Make Codex's agent-bus entry match; return "unchanged" or "updated".

    Variables the entry already forwards are kept; the Rex ones are added.  On any
    failure after the first change, config.toml is put back exactly as it was.
    """
    codex = codex or shutil.which("codex")
    if not codex:
        raise SetupError("codex is not on PATH")
    want = wanted(bin_path)
    have = current(codex, run)
    if _matches(have, want):
        return "unchanged"
    keep = [v for v in (have or {}).get("env_vars", []) if v not in ENV_VARS]
    names = keep + ENV_VARS
    path = codex_home() / "config.toml"
    snapshot = path.read_text(encoding="utf-8") if path.exists() else None
    try:
        if have is None or have["command"] != want["command"] or have["args"] != want["args"]:
            if have is not None:
                _check(run([codex, "mcp", "remove", NAME], capture_output=True, text=True, timeout=30), "remove")
            _check(run([codex, "mcp", "add", NAME, "--", bin_path, "mcp", "--agent", "codex"],
                       capture_output=True, text=True, timeout=30), "add")
        _write_atomic(path, add_env_vars(path.read_text(encoding="utf-8"), names))
        if not _matches(current(codex, run), want):
            raise SetupError("Codex didn't read back the agent-bus entry as written")
    except BaseException as e:
        if snapshot is None:
            path.unlink(missing_ok=True)
        else:
            _write_atomic(path, snapshot)
        if isinstance(e, SetupError):
            raise SetupError(f"{e}; config.toml restored") from e
        raise
    return "updated"


def _check(out: subprocess.CompletedProcess, what: str) -> None:
    if out.returncode != 0:
        raise SetupError(f"`codex mcp {what}` failed: {(out.stderr or out.stdout).strip()[:300]}")
