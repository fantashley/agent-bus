"""The ``agent-bus`` command: MCP server, worker, and the human's CLI."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import textwrap
import time

from . import config as cfg
from . import hooks, rex
from .db import HUMAN, SYSTEM, Bus, BusError
from .service import Service, present, thread_summary

COLORS = {"claude": "\x1b[38;5;173m", "codex": "\x1b[38;5;75m", "antigravity": "\x1b[38;5;114m",
          HUMAN: "\x1b[1;38;5;220m", SYSTEM: "\x1b[2m"}
RESET = "\x1b[0m"


def _color(who: str, text: str, enabled: bool) -> str:
    return f"{COLORS.get(who, '')}{text}{RESET}" if enabled else text


def format_message(m: dict, color: bool) -> str:
    when = time.strftime("%H:%M:%S", time.localtime(m["created_at"]))
    who = "human" if m["sender"] == HUMAN else ("bus" if m["sender"] == SYSTEM else m["sender"])
    tag = m["kind"]
    if m["data"].get("proposal_id"):
        tag += " " + m["data"]["proposal_id"]
    if m["kind"] == "vote":
        tag += f" {m['data'].get('stance')} {m['data'].get('on')}"
    if m["kind"] == "review_verdict":
        tag += " " + str(m["data"].get("verdict"))
    to = f" → {m['recipient']}" if m["recipient"] else ""
    head = _color(m["sender"], f"[{when}] #{m['id']} {who}{to} ({tag}, r{m['round'] or '-'})", color)
    body = textwrap.indent(m["body"].rstrip(), "    ")
    return f"{head}\n{body}"


def thread_status(bus: Bus, thread: str) -> tuple[str, str | None, str]:
    """Map bus state to (OSC 7501 state, blocked kind, message)."""
    t = bus.thread(thread)
    job = bus.active_job(thread)
    if t["status"] == "resolved":
        return "done", None, f"{thread}: resolved" + (f" on {t['final_proposal']}" if t["final_proposal"] else "")
    if t["status"] == "needs_human":
        return "blocked", "question", f"{thread}: needs human"
    if t["status"] == "cancelled":
        return "idle", None, f"{thread}: cancelled"
    if job:
        return "working", None, f"{thread}: round {t['round']}/{t['max_rounds']}"
    # Waiting on an interactive session that has mail here: only the human can nudge it (#20).
    waiting = hooks.mail_waiting(bus, thread)
    if waiting:
        return "blocked", "question", "; ".join(f"{w['agent']} has mail on {thread}" for w in waiting)
    return "idle", None, f"{thread}: waiting"


def peer_status(bus: Bus, thread: str, peer: str) -> tuple[str, str | None, str]:
    t = bus.thread(thread)
    if t["status"] == "cancelled":
        return "idle", None, f"{peer}: cancelled"
    if t["status"] == "needs_human":
        return "blocked", "question", f"{peer}: needs human"
    if bus.peer_busy(thread, peer) and bus.active_job(thread):
        return "working", None, f"{peer}: thinking (round {t['round']})"
    st = bus.standings(thread).get(peer)
    if t["status"] == "resolved":
        if t["kind"] == "review":
            return "done", None, f"{peer}: review posted"
        return "done", None, f"{peer}: agreed on {t['final_proposal']}"
    if st:
        return "idle", None, f"{peer}: {st['stance']} {st['proposal_id']}"
    return "idle", None, f"{peer}: waiting"


def cmd_tail(bus: Bus, args: argparse.Namespace) -> int:
    color = rex.stdout_is_tty() and not args.no_color
    # OSC 7501 status is a Rex feature: only emit it inside Rex.
    status = (not args.no_status) and rex.stdout_is_tty() and bool(os.environ.get("REX_SESSION"))
    notify = args.notify and rex.stdout_is_tty()
    notices = hooks.MailNotices()
    last_msg = last_ev = 0
    last_status = None
    t = bus.thread(args.thread)
    print(_color(SYSTEM, f"agent-bus {t['id']} · {t['kind']} · {', '.join(t['participants'])}", color))
    print(_color(SYSTEM, f"goal: {t['goal']}", color))
    if args.peer:
        print(_color(SYSTEM, f"viewing {args.peer} (output only; intervene with `agent-bus reply`)", color))
    try:
        while True:
            for m in bus.history(args.thread, after_id=last_msg)["messages"]:
                last_msg = m["id"]
                if args.peer and m["sender"] not in (args.peer, HUMAN, SYSTEM):
                    continue
                print(format_message(m, color), flush=True)
            if args.peer or args.events:
                for e in bus.events(args.thread, after_id=last_ev, agent=args.peer):
                    last_ev = e["id"]
                    if e["kind"] in ("text", "tool", "thinking", "turn_start", "turn_end", "error"):
                        who = e["agent"] or "worker"
                        print(_color(SYSTEM, f"  · {who} {e['kind']}: {e['text']}", color), flush=True)
            st = peer_status(bus, args.thread, args.peer) if args.peer else thread_status(bus, args.thread)
            if notify and not args.peer:
                for w in notices.new(hooks.mail_waiting(bus, args.thread)):
                    sys.stdout.write(rex.notify("agent-bus", hooks.describe_mail(w)))
                    sys.stdout.flush()
            if status and st != last_status:
                ident = f"bus.{args.peer}" if args.peer else None
                sys.stdout.write(rex.osc7501(st[0], msg=st[2], kind=st[1], id=ident))
                sys.stdout.flush()
                last_status = st
            if args.once:
                return 0
            time.sleep(1.0)
    except KeyboardInterrupt:
        return 0


def cmd_stream(bus: Bus, args: argparse.Namespace) -> int:
    """One line per notable event: verdicts, finals, status changes, failed jobs.

    Built for notification hooks (each stdout line is one event).  Starts from now
    unless --from-start; with --threads it exits once all of them are closed.
    """
    kinds = set(args.kinds.split(","))
    wanted = set(args.threads or [])
    row = bus._one("SELECT COALESCE(MAX(id), 0) AS m FROM messages")
    last_msg = 0 if args.from_start else row["m"]
    status: dict[str, str] = {}
    # Failures that predate this stream are history; anything that fails after it starts,
    # including a dead worker this stream's own reap() discovers, is reported.
    preexisting = {r["id"] for r in bus._all("SELECT id FROM jobs WHERE status='failed'")}
    reported: set[str] = set()
    first = True
    mail = "mail" in kinds
    notices = hooks.MailNotices()
    one_line = lambda text, n: " ".join(str(text).split())[:n]  # noqa: E731

    def drain() -> None:
        nonlocal last_msg
        for r in bus._all("SELECT id FROM messages WHERE id>? ORDER BY id", last_msg):
            m = bus.message(r["id"])
            last_msg = m["id"]
            if (wanted and m["thread_id"] not in wanted) or m["kind"] not in kinds:
                continue
            who = "human" if m["sender"] == HUMAN else m["sender"]
            extra = m["data"].get("verdict") or m["data"].get("proposal_id") or ""
            print(one_line(f"{m['kind'].upper()} {m['thread_id']} {who} {extra}: {m['body']}", 300), flush=True)

    def failures() -> None:
        for tid in (wanted or [t["id"] for t in bus.threads(include_closed=True, limit=200)]):
            for j in bus.jobs(tid):
                if j["status"] == "failed" and j["id"] not in preexisting and j["id"] not in reported:
                    reported.add(j["id"])
                    print(one_line(f"JOB_FAILED {tid} {j['id']}: {j['error'] or ''}", 300), flush=True)

    def settled(tid: str) -> bool:
        """Closed, or open with no worker left and its last job failed (nothing more will come)."""
        if bus.active_job(tid):
            return False
        if status.get(tid) in ("resolved", "needs_human", "cancelled"):
            return True
        js = bus.jobs(tid)
        return bool(js) and js[-1]["status"] == "failed"

    try:
        while True:
            bus.reap()
            drain()
            for t in bus.threads(include_closed=True, limit=200):
                if wanted and t["id"] not in wanted:
                    continue
                if status.get(t["id"]) != t["status"] and not first and t["status"] != "open":
                    print(one_line(f"THREAD {t['id']} {t['status']}: {t['goal']}", 200), flush=True)
                status[t["id"]] = t["status"]
            if mail:
                # Only watched threads count, before a notice is recorded.
                for w in notices.new(hooks.mail_waiting(bus, wanted or None)):
                    print(one_line(f"MAIL {hooks.describe_mail(w)}", 300), flush=True)
            failures()
            first = False
            if wanted and all(settled(t) for t in wanted):
                # Settling may itself have reaped a job, and a verdict may have landed just
                # before the thread closed: report both before exiting.
                failures()
                drain()
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


def cmd_ask(bus: Bus, args: argparse.Namespace) -> int:
    svc = Service(bus, HUMAN)
    peers = ([p.strip() for p in args.with_.split(",")] if args.with_.strip() else []) if args.with_ is not None else None
    settings: dict = {}
    for item in args.settings:
        key, sep, value = item.partition("=")
        agent, dot, field_ = key.partition(".")
        if not sep or not dot or field_ not in ("model", "effort"):
            raise ValueError(f"--set expects AGENT.model=VALUE or AGENT.effort=VALUE, got {item!r}")
        settings.setdefault(agent, {})[field_] = value
    res = svc.start_discussion(args.prompt, peers=peers, repo=args.repo, max_rounds=args.rounds,
                               deadline_minutes=args.deadline, watch=args.watch, peer_settings=settings or None)
    tid = res["thread"]["id"]
    print(f"started {tid} with {', '.join(res['thread']['participants'])} (job {res['job']['id']})")
    from . import quota
    for agent, b in (res.get("skipped_peers") or {}).items():
        print(f"left out {agent}: {quota.describe(agent, {**b, 'scope': b['scope'] or ''})}")
    if res.get("watch"):
        print(f"rex viewers: {json.dumps(res['watch'])}")
    print(f"follow with: agent-bus tail {tid}")
    if args.wait:
        while bus.thread(tid)["status"] == "open" and bus.active_job(tid):
            time.sleep(2)
        _print_history(bus, tid, color=rex.stdout_is_tty())
    return 0


def _print_history(bus: Bus, thread: str, color: bool) -> None:
    h = bus.history(thread)
    t = h["thread"]
    print(_color(SYSTEM, f"{t['id']} [{t['status']}] {t['kind']} r{t['round']}/{t['max_rounds']}: {t['goal']}",
                 color))
    for m in h["messages"]:
        print(format_message(m, color))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="agent-bus", description="Local message bus for coding agents.")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("mcp", help="run the stdio MCP server for one agent")
    s.add_argument("--agent", required=True, choices=cfg.AGENTS)
    s.add_argument("--role")

    s = sub.add_parser("hook", help="(called by client hooks) print a fixed unread-mail hint at turn boundaries")
    s.add_argument("--client", required=True)
    s.add_argument("--event", required=True)

    s = sub.add_parser("setup-codex", help="register agent-bus with Codex, forwarding Rex variables")
    s.add_argument("--bin", help="agent-bus executable Codex should run (default: this one)")
    grp = s.add_mutually_exclusive_group()
    grp.add_argument("--hooks", action="store_true",
                     help="register agent-bus fixed-hint hooks in Codex's hooks.json")
    grp.add_argument("--no-hooks", action="store_true",
                     help="remove agent-bus hooks from Codex's hooks.json")
    s.add_argument("--hooks-only", action="store_true",
                   help="only update Codex's hooks.json; leave config.toml untouched")
    s.add_argument("--hooks-bin", help="stable agent-bus path for hooks.json (default: --bin)")

    s = sub.add_parser("run-job", help="(internal) run a queued job")
    s.add_argument("job")

    s = sub.add_parser("ask", help="start a discussion between agents")
    s.add_argument("prompt")
    s.add_argument("--with", dest="with_", help="comma-separated agents (default: every available agent)")
    s.add_argument("--repo", help="allow-listed repo the agents may read")
    s.add_argument("--rounds", type=int, help="max rounds (default from config, 4)")
    s.add_argument("--deadline", type=int, help="deadline in minutes (default from config, 30)")
    s.add_argument("--watch", action="store_true", help="open Rex viewer panes")
    s.add_argument("--set", dest="settings", action="append", default=[], metavar="AGENT.KEY=VALUE",
                   help="per-peer model/effort for this discussion, e.g. codex.effort=high (repeatable)")
    s.add_argument("--wait", action="store_true", help="block until the worker finishes, then print the thread")

    s = sub.add_parser("agents", help="list the agents available on this machine, and why")
    s.add_argument("--json", action="store_true")
    s.add_argument("--clear-quota", metavar="AGENT", choices=cfg.AGENTS,
                   help="forget that AGENT is out of usage (e.g. after upgrading a plan)")

    s = sub.add_parser("threads", help="list threads")
    s.add_argument("--all", action="store_true", help="include resolved and cancelled threads")
    s.add_argument("--json", action="store_true")

    s = sub.add_parser("history", help="print a thread")
    s.add_argument("thread")
    s.add_argument("--json", action="store_true")

    s = sub.add_parser("tail", help="follow a thread (Rex-friendly: emits OSC 7501 status)")
    s.add_argument("thread")
    s.add_argument("--peer", choices=cfg.AGENTS, help="show one peer's progress events")
    s.add_argument("--events", action="store_true", help="show every peer's progress events")
    s.add_argument("--once", action="store_true", help="print and exit")
    s.add_argument("--no-status", action="store_true", help="don't emit OSC 7501 status")
    s.add_argument("--no-color", action="store_true")
    s.add_argument("--notify", action="store_true",
                   help="desktop notification (OSC 777/9) when an interactive session has mail on this thread")

    s = sub.add_parser("stream", help="print one line per verdict/final/status change (for notifications)")
    s.add_argument("threads", nargs="*", help="only these threads; exit once they're all closed")
    s.add_argument("--kinds", default="review_verdict,final,mail",
                   help="message kinds to report, plus `mail` for interactive sessions with unread mail "
                        "(default: review_verdict,final,mail)")
    s.add_argument("--from-start", action="store_true", help="also report existing messages")
    s.add_argument("--interval", type=float, default=2.0)

    s = sub.add_parser("reply", help="post to a thread as the human user")
    s.add_argument("thread")
    s.add_argument("body")
    s.add_argument("--to", choices=cfg.AGENTS)

    s = sub.add_parser("cancel", help="cancel a thread and kill its worker")
    s.add_argument("thread")

    s = sub.add_parser("resume", help="lift a thread's limits and continue it")
    s.add_argument("thread")
    s.add_argument("--rounds", type=int, default=2, help="extra rounds to allow (default 2)")

    s = sub.add_parser("rex-check", help="report what Rex shows about an agent session's block (read-only)")
    s.add_argument("--session", help="session id (default: the latest session of each agent)")
    s.add_argument("--block", help="Rex block to check instead of a session's")
    s.add_argument("--agent", choices=cfg.AGENTS, help="agent expected in --block; alone, only that agent's latest session")
    s.add_argument("--json", action="store_true")

    s = sub.add_parser("watch", help="open Rex viewer panes for a thread")
    s.add_argument("thread")
    s.add_argument("--no-peers", action="store_true", help="only the thread watcher")

    args = p.parse_args(argv)

    if args.cmd == "mcp":
        from . import mcp_server
        mcp_server.main(args.agent, args.role)
        return 0
    if args.cmd == "hook":
        from . import hooks
        try:
            payload = sys.stdin.read() if not sys.stdin.isatty() else ""
        except (OSError, ValueError):
            payload = ""
        out = hooks.run(args.client, args.event, payload)
        if out:
            print(out)
        return 0
    if args.cmd == "setup-codex":
        from . import codex_setup
        if args.hooks_bin and not args.hooks:
            p.error("--hooks-bin requires --hooks")
        if args.hooks_only and not (args.hooks or args.no_hooks):
            p.error("--hooks-only requires --hooks or --no-hooks")
        need_exe = (not args.hooks_only) or (args.hooks and not args.hooks_bin)
        exe = None
        if need_exe:
            me = cfg.self_command()
            if not args.bin and len(me) != 1:
                print("agent-bus: setup-codex: can't tell where agent-bus is installed; pass --bin", file=sys.stderr)
                return 1
            exe = os.path.abspath(args.bin or me[0])
        try:
            if not args.hooks_only:
                assert exe is not None
                print(f"codex: agent-bus entry {codex_setup.ensure(exe)}")
            if args.hooks:
                hooks_exe = os.path.abspath(args.hooks_bin) if args.hooks_bin else exe
                assert hooks_exe is not None
                state = codex_setup.ensure_hooks(hooks_exe, enable=True)
                note = " (trust them once in Codex's /hooks prompt)" if state == "updated" else ""
                print(f"codex: agent-bus hooks {state}{note}")
            elif args.no_hooks:
                print(f"codex: agent-bus hooks {codex_setup.ensure_hooks(enable=False)}")
        except (codex_setup.SetupError, OSError, UnicodeError, subprocess.TimeoutExpired) as e:
            print(f"agent-bus: setup-codex: {e}", file=sys.stderr)
            return 1
        return 0
    if args.cmd == "run-job":
        from . import worker
        return worker.main(args.job)

    bus = Bus()
    try:
        return _dispatch(bus, args)
    except BusError as e:
        print(f"agent-bus: {e.code}: {e.message}", file=sys.stderr)
        return 2
    except ValueError as e:
        print(f"agent-bus: {e}", file=sys.stderr)
        return 2
    finally:
        bus.close()


def _dispatch(bus: Bus, args: argparse.Namespace) -> int:
    svc = Service(bus, HUMAN)
    color = rex.stdout_is_tty()
    if args.cmd == "ask":
        return cmd_ask(bus, args)
    if args.cmd == "tail":
        return cmd_tail(bus, args)
    if args.cmd == "stream":
        return cmd_stream(bus, args)
    if args.cmd == "agents":
        from . import quota
        conf = cfg.Config.load()
        if args.clear_quota:
            n = bus.conn.execute("DELETE FROM quota_blocks WHERE agent=?", (args.clear_quota,)).rowcount
            print(f"cleared {n} out-of-usage block(s) for {args.clear_quota}")
            return 0
        avail = conf.availability()
        blocks: dict[str, list] = {}
        for b in bus.quota_blocks():
            blocks.setdefault(b["agent"], []).append(b)
        if args.json:
            for a, v in avail.items():
                if a in blocks:
                    v["out_of_usage"] = [quota.summary(b) for b in blocks[a]]
            print(json.dumps(avail, indent=2))
            return 0
        source = "config `agents`" if conf.agents is not None else "detected on PATH"
        print(f"agents ({source}):")
        for a, v in avail.items():
            print(f"  {'yes' if v['available'] else 'no ':<4} {a:<12} {v['reason']}")
            for b in blocks.get(a, []):
                when = time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(b["until"]))
                print(f"       out of usage{' for model ' + b['scope'] if b['scope'] else ''} until {when}"
                      f"{' (estimated)' if b['estimated'] else ''}: {b['reason'] or 'usage limit reached'}")
        return 0
    if args.cmd == "threads":
        ts = bus.threads(include_closed=args.all)
        if args.json:
            print(json.dumps([thread_summary(t) for t in ts], indent=2))
            return 0
        if not ts:
            print("no threads" if args.all else "no open threads (use --all to include closed ones)")
            return 0
        rows = []
        for t in ts:
            job = bus.active_job(t["id"])
            rows.append((t["id"], t["status"], t["kind"], f"{t['round']}/{t['max_rounds']}",
                         job["status"] if job else "-", ",".join(t["participants"]), t["goal"]))
        head = ("THREAD", "STATUS", "KIND", "ROUND", "WORKER", "PARTICIPANTS")
        widths = [max(len(head[i]), *(len(r[i]) for r in rows)) for i in range(5)]
        line = lambda cols: "  ".join(c.ljust(w) for c, w in zip(cols, widths)) + "  " + cols[5]
        print(line(head))
        indent = " " * (widths[0] + 2)
        for r in rows:
            print(line(r))
            print(indent + " ".join(r[6].split())[:100])
        return 0
    if args.cmd == "history":
        if args.json:
            h = bus.history(args.thread)
            print(json.dumps({"thread": thread_summary(h["thread"]),
                              "messages": [present(m) for m in h["messages"]]}, indent=2))
        else:
            _print_history(bus, args.thread, color)
        return 0
    if args.cmd == "reply":
        svc.send(args.body, thread=args.thread, to=args.to)
        print(f"posted to {args.thread}")
        return 0
    if args.cmd == "cancel":
        res = svc.cancel(args.thread)
        print(f"cancelled {args.thread}" + (f"; signalled worker {res['signalled']}" if res["signalled"] else ""))
        return 0
    if args.cmd == "resume":
        res = svc.resume(args.thread, extra_rounds=args.rounds)
        t = res["thread"]
        print(f"resumed {t['id']}: status {t['status']}, max rounds {t['max_rounds']}"
              + (f", job {res['job']['id']}" if res.get("job") else ""))
        return 0
    if args.cmd == "rex-check":
        from . import rexcheck
        if args.session or args.block:
            reports = [rexcheck.check(bus, session_id=args.session, block=args.block, agent=args.agent)]
        else:  # --agent alone narrows the latest sessions to that agent
            reports = [rexcheck.check(bus, session_id=s) for s in rexcheck.latest_sessions(bus, args.agent)]
        if args.json:
            print(json.dumps(reports, indent=2))
        else:
            print("\n".join(rexcheck.format_report(r) for r in reports) or "no sessions registered")
        return 0 if reports and all(r["ok"] for r in reports) else 1
    if args.cmd == "watch":
        t = bus.thread(args.thread)
        peers = [] if args.no_peers else [p for p in t["participants"] if p != t["initiator"]]
        print(json.dumps(rex.open_viewers(args.thread, peers)))
        return 0
    raise AssertionError(args.cmd)


if __name__ == "__main__":
    sys.exit(main())
