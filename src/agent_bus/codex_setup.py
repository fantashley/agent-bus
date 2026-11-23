"""Register agent-bus with Codex, forwarding Rex's location variables.

``codex mcp add`` can't set ``env_vars``, and Codex starts MCP servers with a
filtered environment, so without it the server never sees REX_SESSION or
REX_BLOCK and can't record which Rex block a Codex session is in.  Re-running
``codex mcp add`` also drops ``env_vars``.  So: add the entry with the CLI, then
put ``env_vars`` into that one table, and check the result through ``codex mcp
get`` so a format we don't understand fails loudly instead of being half-applied.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import tomllib
from pathlib import Path
from typing import Callable

NAME = "agent-bus"
ENV_VARS = ["REX_SESSION", "REX_BLOCK", "REX_SERVER"]
HOOK_EVENTS = (("UserPromptSubmit", "prompt"), ("Stop", "stop"))
HOOK_TIMEOUT_S = 5

Runner = Callable[..., subprocess.CompletedProcess]


class SetupError(Exception):
    pass


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


# A participant pin a user added to the entry (`--participant NAME`, #43): kept as it is.
_PIN = re.compile(r"[a-z][a-z0-9-]{0,31}")  # config.PARTICIPANT_NAME


# Names that can never be a participant on the codex harness.
_NOT_CODEX = frozenset({"human", "system", "claude", "antigravity", "copilot"})


def pin(have: dict | None) -> list[str]:
    """The existing entry's args when they register Codex pinned to a participant (#43),
    as `agent-bus mcp` itself parses them (abbreviations, ``=`` forms, any order); []
    otherwise."""
    from .cli import parse_mcp_args  # cli imports this module lazily
    args = list((have or {}).get("args") or [])
    if not args or args[0] != "mcp" or not all(isinstance(a, str) for a in args):
        return []
    ns = parse_mcp_args(args[1:])
    if ns is None or ns.agent != "codex" or ns.participant is None:
        return []
    if not _PIN.fullmatch(ns.participant) or ns.participant in _NOT_CODEX:
        return []
    return args


def wanted(bin_path: str, have: dict | None = None) -> dict:
    """The entry agent-bus wants, keeping a participant pin ``have`` already carries."""
    return {"command": bin_path, "args": pin(have) or ["mcp", "--agent", "codex"], "env_vars": ENV_VARS}


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


def _write_atomic(path: Path, text: str, default_mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        mode = (path.stat().st_mode & 0o777) if path.exists() else default_mode
        os.chmod(tmp, mode)
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
    have = current(codex, run)
    want = wanted(bin_path, have)
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
            _check(run([codex, "mcp", "add", NAME, "--", bin_path, *want["args"]],
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


def hook_handler(bin_path: str, event: str) -> dict:
    return {"type": "command", "command": f"{shlex.quote(bin_path)} hook --client codex --event {event}",
            "timeout": HOOK_TIMEOUT_S}


def _is_agent_bus_hook(handler: object, event: str) -> bool:
    if not isinstance(handler, dict) or handler.get("type") != "command":
        return False
    cmd = handler.get("command")
    if not isinstance(cmd, str):
        return False
    try:
        tokens = shlex.split(cmd)
    except ValueError:
        return False
    return (len(tokens) == 6 and bool(tokens[0])
            and tokens[1:] == ["hook", "--client", "codex", "--event", event])


def _merge_handler(existing: dict, want: dict) -> dict:
    return {**existing, "type": want["type"], "command": want["command"],
            "timeout": existing.get("timeout", want["timeout"])}


def _strip_group_handlers(group: dict, drop_indices: set[int], hook_event: str) -> dict:
    handlers = group["hooks"]
    kept = [h for i, h in enumerate(handlers) if i not in drop_indices]
    if kept:
        last_kept = max(i for i in range(len(handlers)) if i not in drop_indices)
        if any(i < last_kept for i in drop_indices):
            raise SetupError(f"agent-bus hook in `hooks.{hook_event}` precedes an unrelated hook in the same "
                             "group; remove it manually so Codex's hook trust indices do not shift")
        return {**group, "hooks": kept}
    return {"hooks": []}


def edit_hooks(data: dict, bin_path: str | None, *, enable: bool) -> tuple[dict, bool]:
    """Return ``(updated_data, changed)`` for Codex's ``hooks.json``.

    Only agent-bus's own ``UserPromptSubmit`` and ``Stop`` entries are added,
    updated in place, or removed.  Unrelated events, matcher groups, hook entries
    and their group/handler indices (which Codex keys hook trust on) are preserved:
    removing an agent-bus group ahead of later groups leaves an empty ``{"hooks": []}``
    placeholder instead of shifting later groups down, and re-enabling reuses it.
    """
    if not isinstance(data, dict):
        raise SetupError("Codex's hooks.json root must be a JSON object")
    if "hooks" in data and not isinstance(data["hooks"], dict):
        raise SetupError("`hooks` in Codex's hooks.json must be a JSON object")
    out = copy.deepcopy(data)
    if not enable and "hooks" not in out:
        return out, False
    hooks = out.setdefault("hooks", {})
    for hook_event, bus_event in HOOK_EVENTS:
        if hook_event in hooks and not isinstance(hooks[hook_event], list):
            raise SetupError(f"`hooks.{hook_event}` in Codex's hooks.json must be a list")
        groups = hooks.get(hook_event)
        if enable:
            if not bin_path:
                raise SetupError("bin_path is required to enable Codex hooks")
            want = hook_handler(bin_path, bus_event)
            if groups is None:
                groups = []
                hooks[hook_event] = groups
            found = False
            new_groups = []
            emptied_indices: set[int] = set()
            for gi, g in enumerate(groups):
                if isinstance(g, dict) and isinstance(g.get("hooks"), list):
                    drop: set[int] = set()
                    updated_handlers = list(g["hooks"])
                    for hi, h in enumerate(updated_handlers):
                        if _is_agent_bus_hook(h, bus_event):
                            if not found:
                                updated_handlers[hi] = _merge_handler(h, want)
                                found = True
                            else:
                                drop.add(hi)
                    g = {**g, "hooks": updated_handlers}
                    if drop:
                        g = _strip_group_handlers(g, drop, hook_event)
                        if g == {"hooks": []}:
                            emptied_indices.add(gi)
                    new_groups.append(g)
                else:
                    new_groups.append(g)
            # Trim only trailing groups we emptied ourselves (never shifts earlier groups).
            while new_groups and (len(new_groups) - 1) in emptied_indices:
                emptied_indices.remove(len(new_groups) - 1)
                new_groups.pop()
            if not found:
                # Reuse an empty placeholder group if present, else append at the end.
                slot = next((i for i, g in enumerate(new_groups) if g == {"hooks": []}), None)
                if slot is not None:
                    new_groups[slot] = {"hooks": [want]}
                else:
                    new_groups.append({"hooks": [want]})
            hooks[hook_event] = new_groups
        elif groups is not None:
            new_groups = []
            emptied_indices = set()
            removed_any = False
            for gi, g in enumerate(groups):
                if isinstance(g, dict) and isinstance(g.get("hooks"), list):
                    drop = {hi for hi, h in enumerate(g["hooks"]) if _is_agent_bus_hook(h, bus_event)}
                    if drop:
                        removed_any = True
                        g = _strip_group_handlers(g, drop, hook_event)
                        if g == {"hooks": []}:
                            emptied_indices.add(gi)
                new_groups.append(g)
            if removed_any:
                while new_groups and (len(new_groups) - 1) in emptied_indices:
                    emptied_indices.remove(len(new_groups) - 1)
                    new_groups.pop()
                if new_groups:
                    hooks[hook_event] = new_groups
                else:
                    del hooks[hook_event]
    return out, out != data


def ensure_hooks(bin_path: str | None = None, *, enable: bool) -> str:
    """Register or remove only agent-bus's entries in ``$CODEX_HOME/hooks.json``."""
    path = codex_home() / "hooks.json"
    if not path.exists():
        if not enable:
            return "unchanged"
        data: dict = {}
    else:
        try:
            text = path.read_text(encoding="utf-8")
            data = json.loads(text) if text.strip() else {}
        except json.JSONDecodeError as e:
            raise SetupError(f"Codex's hooks.json is not valid JSON ({e}); not editing it") from e
    updated, changed = edit_hooks(data, bin_path, enable=enable)
    if not changed:
        return "unchanged"
    _write_atomic(path, json.dumps(updated, indent=2) + "\n")
    return "updated"
