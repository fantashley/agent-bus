"""Agents running out of usage (issue #26, agreed on agent-bus thread t-3e260b, P4).

Shared by the service (admission) and the worker (turns).  Blocks live in the
``quota_blocks`` table; see Bus.set_quota_block / quota_block.

Thread notices and error messages use fixed wording only.  A block's ``reason``
is chosen by agent-bus, never copied from a CLI's error output, because these
texts are posted outside any peer_data fence; the raw error stays in the
originating job's progress events.
"""

from __future__ import annotations

import time

from . import config as cfg

# Scope for a model-specific block when no model was chosen: the CLI's own default.
# Never '' (that means account-wide).
DEFAULT_MODEL = "(default)"

REASONS = {
    "structured": "{agent}'s CLI reported its usage limit was reached",
    "heuristic": "{agent}'s CLI reported a usage limit in its error",
}


# The flags each adapter treats as choosing a model (and strips from extra args when a
# model setting overrides them): keep in step with peers.Adapter.extra_args.
MODEL_FLAGS = {"codex": ("-m", "--model")}


def effective_model(config: cfg.Config, agent: str, settings: dict) -> str | None:
    """The model a peer turn runs: the per-thread or config setting (which replaces any
    model flag in the extra args), else the last model flag the configured extra args
    pass, else None (the CLI's default)."""
    if settings.get("model"):
        return settings["model"]
    flags = MODEL_FLAGS.get(agent, ("--model",))
    args, found = config.peer_args(agent), None
    for i, a in enumerate(args):
        if a == "--":
            break
        if a in flags and i + 1 < len(args):
            found = args[i + 1]
        elif any(a.startswith(f + "=") for f in flags):
            found = a.split("=", 1)[1]
        elif any(len(f) == 2 and a.startswith(f) and len(a) > 2 for f in flags):
            found = a[2:]
    return found


def model_key(model: str | None) -> str:
    return model or DEFAULT_MODEL


def scope_for(agent: str, model: str | None) -> str:
    """Claude and Codex limits are account-wide; an Antigravity quota may be per model."""
    return model_key(model) if agent == "antigravity" else ""


def reason(agent: str, source: str) -> str:
    return REASONS.get(source, REASONS["heuristic"]).format(agent=agent)


def summary(block: dict) -> dict:
    return {"until": block["until"], "estimated": bool(block["estimated"]), "source": block["source"],
            "reason": block["reason"], "scope": block["scope"] or None}


def when(until: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(until))


def describe(agent: str, block: dict) -> str:
    model = f" for model {block['scope']}" if block["scope"] else ""
    est = " (an estimate: no reset time was given)" if block["estimated"] else ""
    return f"{agent} is out of usage{model} until about {when(block['until'])}{est}"


def message(agent: str, role: str, block: dict) -> str:
    return (f"{role} {describe(agent, block)}. Pick another agent, or retry after that; the human can clear "
            f"it with `agent-bus agents --clear-quota {agent}`.")


def peer_model(config: cfg.Config, t: dict, agent: str) -> str | None:
    overrides = (t["refs"] or {}).get("peer_settings") or {}
    return effective_model(config, agent, config.peer_settings(agent, overrides.get(agent)))


def blocked_peers(bus, config: cfg.Config, t: dict) -> dict[str, dict]:
    """{peer: block} for this discussion's headless peers that are out of usage."""
    out = {}
    for p in t["participants"]:
        if p == t["initiator"]:
            continue
        b = bus.quota_block(p, model_key(peer_model(config, t, p)))
        if b:
            out[p] = b
    return out
