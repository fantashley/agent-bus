"""Headless discussion peers that can no longer vote (issues #39 and #40).

A peer can't take another turn in a discussion, until something outside it changes, when:

- ``quota``: its agent is out of usage (see quota.py; the block is shared and expires), or
- ``format``: its CLI exited normally with output agent-bus can't use (PEER_FORMAT_CHANGED).
  That is recorded per discussion and lasts until `agent-bus resume` clears it.

Either way its recorded proposals and votes stand, and the same rules decide what the rest of
the discussion can still do (first agreed for quota on agent-bus thread t-3e260b, P4, and
extended to format drift on t-606fcd, P4).  Ordinary or transient turn failures, including a
turn the CLI stopped partway through (PEER_TURN_INCOMPLETE), are not covered: those peers are
asked again.

Shared by the service (admission) and the worker (rounds).  Each reason is a dict with
``why`` ("quota" or "format") and the block or drift row it came from.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import config as cfg
from . import quota

CODES = {"quota": "USAGE_EXHAUSTED", "format": "PEER_FORMAT_CHANGED"}


def peers(bus, config: cfg.Config, t: dict) -> dict[str, dict]:
    """{peer: reason} for this discussion's headless peers that can't vote again.  Out of
    usage wins over format drift: it says when the peer may be back."""
    out = {p: {"why": "format", **d} for p, d in bus.peer_drift(t["id"]).items()
           if p in t["participants"] and p != t["initiator"]}
    out.update({p: {"why": "quota", "block": b} for p, b in quota.blocked_peers(bus, config, t).items()})
    return out


@dataclass
class Standing:
    """What the peers that can't vote again leave possible, from one read."""
    stuck: dict[str, dict]       # {peer: reason}
    stalemate: bool              # no path to unanimity while they're stuck
    everyone: bool               # no headless peer can take another turn
    backs: str | None            # the one proposal every stuck peer agrees with, if any


def assess(bus, config: cfg.Config, t: dict) -> Standing:
    """Stalemate: a peer that can't vote again backs nothing, or such peers back different
    proposals.  One that already agrees with something the others can still converge on
    doesn't stop the discussion.  When no headless peer can move but it isn't a stalemate,
    another round can't change anything, yet the initiator can still settle it by voting
    on the proposal they all back (issue #39)."""
    if t["status"] != "open":
        return Standing({}, False, False, None)
    stuck = peers(bus, config, t)
    if not stuck:
        return Standing({}, False, False, None)
    st = bus.standings(t["id"])
    backed = {st.get(p, {}).get("proposal_id") if st.get(p, {}).get("stance") == "agree" else None
              for p in stuck}
    stalemate = None in backed or len(backed) > 1
    headless = [p for p in t["participants"] if p != t["initiator"]]
    return Standing(stuck, stalemate, all(p in stuck for p in headless),
                    None if stalemate else next(iter(backed)))


def stalemate(bus, config: cfg.Config, t: dict) -> dict[str, dict]:
    """The peers that leave no path to unanimity ({} if there is still one), so a caller
    can decide and explain from the same snapshot."""
    s = assess(bus, config, t)
    return s.stuck if s.stalemate else {}


def code(stuck: dict[str, dict]) -> str:
    """The error code for refusing a request because of ``stuck``: USAGE_EXHAUSTED or
    PEER_FORMAT_CHANGED when there is one reason, PEERS_CANNOT_VOTE when they're mixed."""
    whys = {r["why"] for r in stuck.values()}
    return CODES[whys.pop()] if len(whys) == 1 else "PEERS_CANNOT_VOTE"


def describe(peer: str, reason: dict, thread: str) -> str:
    """Fixed wording, plus the CLI version the drift notice also names."""
    if reason["why"] == "quota":
        return quota.message(peer, "peer", reason["block"])
    return (f"peer {peer} ({reason['version']}) exited normally {reason['reason']}, so its output format has "
            f"probably changed. Run `agent-bus resume {thread}` once it's fixed.")


def explain(stuck: dict[str, dict], thread: str) -> str:
    return " ".join(describe(p, r, thread) for p, r in stuck.items())
