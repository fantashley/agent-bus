"""Token usage history: what headless turns used, per agent and per thread (issue #26).

The worker records each CLI run's tokens in the ``turn_usage`` table (Bus.record_usage);
``agent-bus usage`` reports them from here.
"""

from __future__ import annotations

import time

from .db import USAGE_KEYS, Bus, BusError

LABELS = {"input": "INPUT", "output": "OUTPUT", "cache_read": "CACHE READ", "cache_write": "CACHE WRITE",
          "thinking": "THINKING"}


def _totals(rows: list[dict]) -> dict:
    """Turns, counter sums, and how many turns reported no share of their own."""
    out = {"turns": len(rows), "unreported_turns": 0, "partial_turns": 0, **{k: 0 for k in USAGE_KEYS}}
    for r in rows:
        if all(r[k] is None for k in USAGE_KEYS):
            out["unreported_turns"] += 1
        out["partial_turns"] += r["partial"]
        for k in USAGE_KEYS:
            out[k] += r[k] or 0
    return out


def report(bus: Bus, hours: float = 24, thread: str | None = None, agent: str | None = None,
           limit: int = 20) -> dict:
    """Recent usage per agent, and per thread (most recently used first, at most ``limit``)."""
    since = time.time() - hours * 3600
    # A turn marked started that reported nothing (cancelled, or its worker died) isn't one.
    rows = [r for r in bus.usage_rows(since, thread_id=thread, agent=agent)
            if r["session_total"] or any(r[k] is not None for k in USAGE_KEYS)]
    by_agent: dict[str, list] = {}
    by_thread: dict[str, list] = {}
    for r in rows:
        by_agent.setdefault(r["agent"], []).append(r)
        by_thread.setdefault(r["thread_id"], []).append(r)
    threads = []
    # A started turn's row id is taken when it starts, so recency is the latest completion time.
    last = {tid: max(r["created_at"] for r in rs) for tid, rs in by_thread.items()}
    for tid, rs in sorted(by_thread.items(), key=lambda kv: -last[kv[0]])[:limit]:
        try:
            t = bus.thread(tid)
            info = {"kind": t["kind"], "status": t["status"], "goal": " ".join(t["goal"].split())[:200]}
        except BusError:  # the thread row is gone; its usage still counts
            info = {"kind": None, "status": None, "goal": None}
        agents: dict[str, list] = {}
        for r in rs:
            agents.setdefault(r["agent"], []).append(r)
        threads.append({"thread": tid, **info, "last_turn_at": last[tid],
                        "agents": {a: _totals(v) for a, v in sorted(agents.items())}, "total": _totals(rs)})
    return {"since": since, "hours": hours, "agents": {a: _totals(v) for a, v in sorted(by_agent.items())},
            "threads": threads, "more_threads": max(0, len(by_thread) - limit)}


def _n(v: int) -> str:
    return f"{v:,}"


def format_report(rep: dict) -> str:
    when = time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(rep["since"]))
    lines = [f"token usage of headless turns in the last {rep['hours']:g}h (since {when})"]
    if not rep["agents"]:
        return lines[0] + ": none recorded"
    head = ("AGENT", "TURNS", *LABELS.values())
    table = [head] + [(a, _n(v["turns"]), *(_n(v[k]) for k in USAGE_KEYS)) for a, v in rep["agents"].items()]
    widths = [max(len(r[i]) for r in table) for i in range(len(head))]
    lines += ["", "by agent:"]
    lines += ["  " + "  ".join(c.ljust(w) if i == 0 else c.rjust(w) for i, (c, w) in enumerate(zip(r, widths)))
              for r in table]
    unreported = {a: v["unreported_turns"] for a, v in rep["agents"].items() if v["unreported_turns"]}
    if unreported:
        lines.append("  (turns with no count of their own, only a session total: "
                     + ", ".join(f"{a} {n}" for a, n in unreported.items()) + ")")
    partial = {a: v["partial_turns"] for a, v in rep["agents"].items() if v["partial_turns"]}
    if partial:
        lines.append("  (turns counted only in part, having stopped before the CLI's final report: "
                     + ", ".join(f"{a} {n}" for a, n in partial.items()) + ")")
    lines += ["", "by thread (most recent first):"]
    for t in rep["threads"]:
        desc = f"{t['kind']} {t['status']}: {t['goal'][:80]}" if t["kind"] else "(thread no longer exists)"
        lines.append(f"  {t['thread']}  {desc}")
        for a, v in t["agents"].items():
            counts = " ".join(f"{LABELS[k].lower()} {_n(v[k])}" for k in USAGE_KEYS if v[k])
            lines.append(f"    {a:<12} {v['turns']} turn{'s' if v['turns'] != 1 else ''}"
                         + (f"  {counts}" if counts else ""))
    if rep["more_threads"]:
        lines.append(f"  … and {rep['more_threads']} more thread(s) (use --limit)")
    return "\n".join(lines)
