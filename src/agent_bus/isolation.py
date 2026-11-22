"""Isolation evidence and the account-free doctor (issue #32 stage 2, agent-bus
thread t-71c7ce, P2).

A peer is only as isolated as its CLI, flags and configuration make it, and all
three change.  Evidence is recorded per (agent, executable identity, adapter
recipe, config fingerprint) and per capability ("write", "mcp"), with an outcome
of verified, failed or inconclusive.  Any change to the key means no evidence.

This module only reads, fingerprints and reports.  The live canaries that earn
"verified" (scripted local model endpoints, same-source positive controls) and
the launch gate that requires it come later, together, per P2.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any

from . import config as cfg
from .peers import (PeerSetupError, adapter, agy_mcp_servers, agy_workspaces, cli_identity, cli_version,
                    codex_config_args, trusted_agent_bus_entry)

CAPABILITIES = ("write", "mcp")
OUTCOMES = ("verified", "failed", "inconclusive")

# What can earn evidence today, per adapter.  None: no live recipe yet (P2: a
# scripted local model endpoint for Claude and Codex; none known for Antigravity).
RECIPES: dict[str, str | None] = {"claude": None, "codex": None, "antigravity": None}
NO_RECIPE = {
    "claude": "no live canary recipe yet (planned: scripted Anthropic Messages endpoint)",
    "codex": "no live canary recipe yet (planned: scripted Responses endpoint)",
    "antigravity": "no local model endpoint known, so isolation can't be shown live yet",
}


def _digest(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:16]


CONFIG_ENV = ("HOME", "XDG_CONFIG_HOME", "CODEX_HOME", "CLAUDE_CONFIG_DIR", "AGY_CONFIG_DIR")


def fingerprint(agent: str, config: cfg.Config, cwd: str | None = None, ad=None) -> tuple[str | None, list[str]]:
    """(fingerprint, problems) of everything this agent's isolation depends on besides
    the binary and recipe: its extra args, and the MCP sources its preflight reads.
    None when a source can't be read (then nothing can match)."""
    ad = ad or adapter(agent, config)
    inputs: dict[str, Any] = {"args": config.peer_args(agent), "command": ad.command,
                              "allow_mcp": bool(config.peers.get(agent, {}).get("allow_mcp")),
                              # Where each CLI reads its configuration from.
                              "env": {k: os.environ.get(k) for k in CONFIG_ENV}}
    problems: list[str] = []
    try:
        if agent == "codex":
            inputs["mcp"] = sorted(json.dumps(d, sort_keys=True) for d in ad.mcp_list(cwd or "."))
        elif agent == "antigravity":
            spaces = agy_workspaces(config.peer_args(agent), cwd or ".")
            inputs["workspaces"] = spaces
            inputs["mcp"] = sorted((n, w, _digest(s)) for n, w, s in agy_mcp_servers(spaces[0], spaces[1:]))
    except PeerSetupError as e:
        problems.append(str(e))
        return None, problems
    return _digest(inputs), problems


def preflight(agent: str, config: cfg.Config, cwd: str, ad=None) -> list[dict]:
    """Checks that need no model turn: [{check, ok, detail}]."""
    out: list[dict] = []
    ad = ad or adapter(agent, config)
    try:
        argv = ad.invocation("doctor", None, cwd, []).argv
    except PeerSetupError as e:
        return [{"check": "launch preflight", "ok": False, "detail": str(e)}]
    out.append({"check": "launch preflight", "ok": True, "detail": "passes"})

    def has(*flags: str) -> bool:
        return any(argv[i:i + len(flags)] == list(flags) for i in range(len(argv)))

    if agent == "claude":
        out.append({"check": "MCP: --strict-mcp-config with no --mcp-config", "ok": has("--strict-mcp-config")
                    and "--mcp-config" not in argv, "detail": "loads no MCP servers"})
        out.append({"check": "writes: --permission-mode plan", "ok": has("--permission-mode", "plan"),
                    "detail": "tools that act need approval, which a headless turn can't give"})
    elif agent == "codex":
        servers = ad.mcp_servers(cwd)
        cfg_args = codex_config_args(argv[1:])
        still_on = [s for s in servers
                    if _last_setting(cfg_args, (), f"mcp_servers.{s}.enabled") not in ("false",)]
        out.append({"check": "MCP: every listed server disabled", "ok": not still_on,
                    "detail": f"{len(servers) - len(still_on)}/{len(servers)} listed servers switched off"
                    + (f"; still on: {', '.join(still_on)}" if still_on else "")})
        bypass = [a for a in argv if a in CODEX_BYPASS or any(a.startswith(f + "=") for f in CODEX_BYPASS)]
        sandbox = effective_codex_setting(argv, ("--sandbox", "-s"), "sandbox_mode")
        approval = effective_codex_setting(argv, ("--ask-for-approval", "-a"), "approval_policy")
        out.append({"check": "writes: read-only sandbox",
                    "ok": not bypass and sandbox == "read-only",
                    "detail": (f"overridden by {', '.join(bypass)}" if bypass
                               else f"effective sandbox: {sandbox or 'unknown'}")})
        out.append({"check": "writes: approval_policy never", "ok": not bypass and approval == "never",
                    "detail": f"effective approval policy: {approval or 'unknown'}"})
    elif agent == "antigravity":
        allow = bool(config.peers.get(agent, {}).get("allow_mcp"))
        # Its own scan of the launch's effective workspaces, even when allow_mcp makes
        # the launch skip it.
        spaces = agy_workspaces(argv, cwd)
        extra = [n for n, _w, s in agy_mcp_servers(spaces[0], spaces[1:]) if not trusted_agent_bus_entry(n, s)]
        out.append({"check": "MCP: no servers besides agent-bus", "ok": not extra,
                    "detail": "UNSAFE waiver: allow_mcp is set" if allow and extra
                    else ", ".join(extra) or "none configured"})
        out.append({"check": "writes: --mode plan --sandbox", "ok": has("--mode", "plan") and "--sandbox" in argv,
                    "detail": "plan mode, terminal sandbox"})
    return out


# Codex switches that take precedence over any sandbox or approval setting
# (codex 0.171.0 exec applies them first).  Any of them means not read-only.
CODEX_BYPASS = ("--dangerously-bypass-approvals-and-sandbox", "--yolo", "--full-auto")


def effective_codex_setting(argv: list[str], flags: tuple[str, ...], config_key: str) -> str | None:
    """Codex resolves a dedicated option (e.g. --sandbox) ahead of any config value,
    whatever the order (codex 0.171.0 permission_config): the last dedicated option
    wins if there is one, else the last config override."""
    dedicated = _last_setting(argv, flags, "\0no-config-key")
    return dedicated if dedicated is not None else _last_setting(argv, (), config_key)


def _last_setting(argv: list[str], flags: tuple[str, ...], config_key: str) -> str | None:
    """The effective value of a codex setting: the last of `--flag value`, `--flag=value`,
    `-Xvalue`, or a -c/--config override (split or joined), since later ones win."""
    value = None
    i = 0
    while i < len(argv):
        a = argv[i]
        nxt = argv[i + 1] if i + 1 < len(argv) else None
        override = None
        if a in flags and nxt is not None:
            value, i = nxt, i + 2
            continue
        for f in flags:
            if f.startswith("--") and a.startswith(f + "="):
                value = a.split("=", 1)[1]
            elif not f.startswith("--") and a.startswith(f) and len(a) > len(f):
                value = a[len(f):]
        if a in ("-c", "--config") and nxt is not None:
            override, i = nxt, i + 1
        elif a.startswith("--config="):
            override = a.split("=", 1)[1]
        elif a.startswith("-c") and len(a) > 2:
            override = a[2:].removeprefix("=")
        if override is not None and "=" in override and override.split("=", 1)[0].strip() == config_key:
            value = override.split("=", 1)[1].strip().strip('"').strip("'")
        i += 1
    return value


def report(bus, config: cfg.Config, cwd: str) -> dict[str, dict]:
    """Everything `agent-bus doctor` shows, per available agent.  No turns."""
    out: dict[str, dict] = {}
    for agent, avail in config.availability().items():
        entry: dict[str, Any] = {"available": avail["available"], "reason": avail["reason"]}
        out[agent] = entry
        if not avail["available"]:
            continue
        ad = adapter(agent, config)
        ident = cli_identity(ad.command, cwd)
        entry["launch"] = ident[0] if ident else None
        if not ident:
            entry.update(version="unknown", recipe=f"{agent}/{ad.recipe}", fingerprint=None, problems=[],
                         preflight=[{"check": "CLI found", "ok": False, "detail": f"`{ad.command}` not found"}],
                         evidence={c: {"outcome": "none", "reason": "CLI not found"} for c in CAPABILITIES})
            continue
        entry["version"] = cli_version(bus, agent, ad.command, cwd=cwd)
        entry["recipe"] = f"{agent}/{ad.recipe}"
        fp, problems = fingerprint(agent, config, cwd, ad)
        entry["fingerprint"] = fp
        entry["problems"] = problems
        try:
            entry["preflight"] = preflight(agent, config, cwd, ad)
        except PeerSetupError as e:  # a later check failing still lands in the report
            entry["preflight"] = [{"check": "preflight", "ok": False, "detail": str(e)}]
        found = bus.isolation_evidence(agent, ident[1], ad.recipe, fp) if ident and fp else {}
        entry["evidence"] = {
            cap: ({"outcome": found[cap]["outcome"], "reason": found[cap]["reason"],
                   "checked_at": found[cap]["checked_at"]} if cap in found
                  else {"outcome": "none", "reason": NO_RECIPE[agent] if RECIPES[agent] is None
                        else "run `agent-bus doctor --live --agent " + agent + "`"})
            for cap in CAPABILITIES}
    return out


def failing(rep: dict[str, dict]) -> bool:
    """The same criteria render() marks FAIL: a failed check or an unreadable config."""
    return any(not p["ok"] for e in rep.values() if e.get("available") for p in e.get("preflight", [])) \
        or any(e.get("problems") for e in rep.values() if e.get("available"))


def render(rep: dict[str, dict]) -> str:
    lines = []
    for agent, e in rep.items():
        if not e["available"]:
            lines.append(f"{agent}: not available ({e['reason']})")
            continue
        lines.append(f"{agent}: {e['version']} · recipe {e['recipe']} · {e['launch']}")
        for p in e["preflight"]:
            lines.append(f"  {'ok  ' if p['ok'] else 'FAIL'} {p['check']}: {p['detail']}")
        for problem in e["problems"]:
            lines.append(f"  FAIL config fingerprint: {problem}")
        for cap, ev in e["evidence"].items():
            when = (f" ({time.strftime('%Y-%m-%d %H:%M', time.localtime(ev['checked_at']))})"
                    if ev.get("checked_at") else "")
            lines.append(f"  isolation evidence, {cap}: {ev['outcome']}{when}: {ev['reason']}")
    return "\n".join(lines)
