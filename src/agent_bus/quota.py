"""Agents running out of usage (issue #26, agreed on agent-bus thread t-3e260b, P4).

Shared by the service (admission) and the worker (turns).  Blocks live in the
``quota_blocks`` table; see Bus.set_quota_block / quota_block.

Blocks and usage-limit readings belong to what actually runs out (#43): the harness and
the account it signs in with, which is the ``subject`` stored in their ``agent`` column
(the harness name alone for its default account, so rows from before named participants
keep their meaning).  A model-specific limit adds the model as the block's scope.
Participants sharing a harness and account share its blocks; one with its own
``account`` doesn't.

Thread notices and error messages use fixed wording only.  A block's ``reason``
is chosen by agent-bus, never copied from a CLI's error output, because these
texts are posted outside any peer_data fence; the raw error stays in the
originating job's progress events.
"""

from __future__ import annotations

import shlex
import time

from . import config as cfg

# Scope for a model-specific block when no model was chosen: the CLI's own default.
# Never '' (that means account-wide).
DEFAULT_MODEL = "(default)"

REASONS = {
    "structured": "{agent}'s CLI reported its usage limit was reached",
    "heuristic": "{agent}'s CLI reported a usage limit in its error",
    # Amazon Bedrock (#36): a request rate limit that outlasted the CLI's own retries, and
    # credentials the CLI couldn't load or that have expired.
    "throttle": "{agent}'s model provider throttled its requests",
    "auth": "{agent}'s CLI could not load or refresh its credentials",
}

# How long a throttled model is left alone when the provider gave no retry time: the CLI
# has already retried with backoff, so this only spaces out turns (#36).
THROTTLE_COOLDOWN_S = 60
# Expired or missing credentials don't come back by themselves: blocked until the human
# clears it (`agent-bus resume`, --clear-quota) or a turn succeeds.  Capped like any block.
AUTH_BLOCK_S = 7 * 86400


# The flags each adapter treats as choosing a model (and strips from extra args when a
# model setting overrides them): keep in step with peers.Adapter.extra_args.
MODEL_FLAGS = {"codex": ("-m", "--model")}


def effective_model(config: cfg.Config, agent: str, settings: dict) -> str | None:
    """The model participant ``agent``'s turn runs: the per-thread or config setting (which
    replaces any model flag in the extra args), else the last model flag the configured
    extra args pass, else ANTHROPIC_MODEL (Claude) or COPILOT_MODEL (Copilot) in its
    configured env, else None (the CLI's default)."""
    if settings.get("model"):
        return settings["model"]
    flags = MODEL_FLAGS.get(config.harness_of(agent), ("--model",))
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
    if found is None and config.harness_of(agent) == "claude":
        # Claude Code's model variable, when agent-bus sets it for the participant (#36).
        found = config.peer_env(agent).get("ANTHROPIC_MODEL") or None
    elif found is None and config.harness_of(agent) == "copilot":
        # Copilot's model variable, below --model in its precedence (#2).
        found = config.peer_env(agent).get("COPILOT_MODEL") or None
    return found


def model_key(model: str | None) -> str:
    return model or DEFAULT_MODEL


def subject(config: cfg.Config, agent: str) -> str:
    """Whose usage participant ``agent`` spends: its harness, plus ``@account`` when the
    participant names an account other than the harness's default."""
    account = config.account(agent)
    harness = config.harness_of(agent)
    return f"{harness}@{account}" if account else harness


def sharing(config: cfg.Config, subj: str) -> list[str]:
    """The participants whose usage counts against ``subj``."""
    return [p for p in config.participant_map() if subject(config, p) == subj]


def model_scope(config: cfg.Config, agent: str, model: str | None) -> str:
    """The key of a model-specific block for ``agent`` running ``model``.  On Amazon Bedrock
    limits are per model and region (#36): ``<model>@<region>``."""
    if config.bedrock(agent):
        # An alias names whatever the participant's ANTHROPIC_DEFAULT_<ALIAS>_MODEL says.
        alias = (model or "").split("[", 1)[0].lower()
        if alias in ("haiku", "sonnet", "opus"):
            model = config.peer_env(agent).get(f"ANTHROPIC_DEFAULT_{alias.upper()}_MODEL") or model
        return f"{model_key(model)}@{config.aws_region(agent) or DEFAULT_MODEL}"
    return model_key(model)


def scope_for(config: cfg.Config, agent: str, model: str | None, source: str = "heuristic",
              per_model: bool = False) -> str:
    """Claude and Codex limits are account-wide; an Antigravity quota may be per model, and
    a Bedrock throttle or quota is per model and region.  Copilot runs many vendors' models
    and has both kinds (#2): its adapter says ``per_model`` for "the rate limit for this
    model", and its session, weekly and plan limits stay account-wide.  Credentials are
    account-wide."""
    if source == "auth":
        return ""
    if per_model or config.harness_of(agent) == "antigravity" or config.bedrock(agent):
        return model_scope(config, agent, model)
    return ""


def block_for(bus, config: cfg.Config, agent: str, settings: dict) -> dict | None:
    """The active block that stops participant ``agent`` running with ``settings``."""
    return bus.quota_block(subject(config, agent), model_scope(config, agent, effective_model(config, agent, settings)))


def clear_for(bus, config: cfg.Config, agent: str, model: str | None) -> int:
    """Lift the block on ``agent``'s account (and on its model): newer evidence of usage,
    or the human retrying.  Threads waiting on any participant sharing it are due now."""
    subj = subject(config, agent)
    return bus.clear_quota(subj, model_scope(config, agent, model), waiting=sharing(config, subj))


def is_auth(block: dict | None) -> bool:
    return bool(block) and block.get("source") == "auth"


def code(block: dict) -> str:
    """The error code for refusing a request because of ``block``."""
    return "AUTH_REQUIRED" if is_auth(block) else "USAGE_EXHAUSTED"


def auth_hint(config: cfg.Config, agent: str) -> str:
    """What the human runs to sign ``agent`` in again: fixed wording, plus the AWS profile
    from configuration (never anything from the CLI's output)."""
    if config.is_participant(agent) and config.bedrock(agent):
        profile = config.aws_profile(agent)
        if profile and cfg.ENV_SAFE.fullmatch(profile):
            # With the peer's own HOME and AWS config file, if it has them: that's where the
            # profile is defined and where the SSO token is cached.
            env = config.peer_env(agent)
            prefix = "".join(f"{k}={shlex.quote(env[k])} " for k in ("HOME", "AWS_CONFIG_FILE") if env.get(k))
            return f"run `{prefix}aws sso login --profile {profile}` on this machine"
        return "refresh its AWS credentials on this machine (e.g. `aws sso login --profile <profile>`)"
    return "sign its CLI in again on this machine"


def reason(agent: str, source: str) -> str:
    return REASONS.get(source, REASONS["heuristic"]).format(agent=agent)


def summary(block: dict) -> dict:
    return {"until": block["until"], "estimated": bool(block["estimated"]), "source": block["source"],
            "reason": block["reason"], "scope": block["scope"] or None}


def when(until: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(until))


def describe(agent: str, block: dict) -> str:
    if is_auth(block):
        return f"{agent} can't authenticate: its CLI could not load or refresh its credentials"
    model = f" for model {block['scope']}" if block["scope"] else ""
    if block.get("source") == "throttle":
        return f"{agent} is throttled by its model provider{model} until about {when(block['until'])}"
    est = " (an estimate: no reset time was given)" if block["estimated"] else ""
    return f"{agent} is out of usage{model} until about {when(block['until'])}{est}"


def message(agent: str, role: str, block: dict, config: cfg.Config | None = None) -> str:
    if is_auth(block):
        hint = auth_hint(config, agent) if config is not None else "sign its CLI in again on this machine"
        return (f"{role} {describe(agent, block)}. To fix it, {hint}; then retry, or clear it with "
                f"`agent-bus agents --clear-quota {agent}`.")
    return (f"{role} {describe(agent, block)}. Pick another agent, or retry after that; the human can clear "
            f"it with `agent-bus agents --clear-quota {agent}`.")


def thread_settings(config: cfg.Config, t: dict, agent: str) -> dict:
    overrides = (t["refs"] or {}).get("peer_settings") or {}
    return config.peer_settings(agent, overrides.get(agent))


def peer_model(config: cfg.Config, t: dict, agent: str) -> str | None:
    return effective_model(config, agent, thread_settings(config, t, agent))


def thread_block(bus, config: cfg.Config, t: dict, agent: str) -> dict | None:
    """The block that stops ``agent`` on thread ``t`` (with its model settings), if any.
    A participant no longer configured has no known account: nothing to report."""
    if not config.is_participant(agent):
        return None
    return block_for(bus, config, agent, thread_settings(config, t, agent))


def blocked_peers(bus, config: cfg.Config, t: dict) -> dict[str, dict]:
    """{peer: block} for this discussion's headless peers that are out of usage."""
    out = {}
    for p in t["participants"]:
        if p == t["initiator"]:
            continue
        b = thread_block(bus, config, t, p)
        if b:
            out[p] = b
    return out


def by_participant(config: cfg.Config, rows: list[dict]) -> dict[str, list[dict]]:
    """Blocks or usage-limit readings (keyed by subject) listed under each participant
    whose usage they describe."""
    subjects = {p: subject(config, p) for p in config.participant_map()}
    return {p: found for p, subj in subjects.items() if (found := [r for r in rows if r["agent"] == subj])}


# ------------------------------------------------------------ near the limit


def window_name(w: dict) -> str:
    """A usage-limit window as people say it: "five_hour", or "primary (7d)"."""
    m = w.get("minutes")
    if not m:
        return w["window"]
    span = f"{m // 1440}d" if m % 1440 == 0 else f"{m // 60}h" if m % 60 == 0 else f"{m}m"
    return f"{w['window']} ({span})"


def describe_window(w: dict) -> str:
    resets = f", resets {when(w['resets_at'])}" if w.get("resets_at") else ""
    return f"{window_name(w)} {w['used']:.0%} used{resets}"


def near_limit(bus, config: cfg.Config, agent: str) -> list[dict]:
    """The usage-limit windows of ``agent``'s account at or above ``usage_warn_at``, from
    the last readings its CLI gave (Claude and Codex; Antigravity reports none)."""
    return [w for w in bus.usage_limits(subject(config, agent)) if w["used"] >= config.usage_warn_at]


def limits_summary(w: dict, warn_at: float) -> dict:
    return {"window": w["window"], "used": round(w["used"], 4), "minutes": w["minutes"],
            "resets_at": w["resets_at"], "observed_at": w["observed_at"], "near_limit": w["used"] >= warn_at}
