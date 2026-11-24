"""The ``agent-bus`` command: the human's CLI, plus the MCP server, worker and hooks
entry points that client configs start (left out of the top-level help, #84)."""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import textwrap
import time

from . import config as cfg
from . import hooks, rex
from .db import HUMAN, SYSTEM, Bus, BusError, format_progress, is_quiet
from .service import Service, present, thread_summary
from .worker import format_review

COLORS = {"claude": "\x1b[38;5;173m", "codex": "\x1b[38;5;75m", "antigravity": "\x1b[38;5;114m",
          "copilot": "\x1b[38;5;141m",
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
    if not job and t["kind"] == "review" and (t["refs"] or {}).get("reviewers"):
        st = bus.review_standings(thread)
        if st["changes_requested"]:
            return "idle", None, f"{thread}: changes requested by {', '.join(st['changes_requested'])}"
    if job:
        if t["kind"] == "review":
            return "working", None, f"{thread}: {(t['refs'] or {}).get('reviewer')} reviewing (pass {t['round']})"
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
    from . import worker
    try:
        while True:
            bus.expire_discussion(args.thread)  # parked once its deadline passes (#74)
            worker.relaunch_stalled(bus)  # its job's worker may never have started (#64)
            for m in bus.history(args.thread, after_id=last_msg)["messages"]:
                last_msg = m["id"]
                if args.peer and m["sender"] not in (args.peer, HUMAN, SYSTEM):
                    continue
                print(format_message(m, color), flush=True)
            if args.peer or args.events:
                for e in bus.events(args.thread, after_id=last_ev, agent=args.peer):
                    last_ev = e["id"]
                    if e["kind"] in ("text", "tool", "thinking", "turn_start", "turn_end", "error", "format",
                                       "relaunch", "api_retry", "repair"):
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
        from . import autoresume
        while True:
            # The lazy sweep runs here, after the snapshots above, not before this command:
            # a job it fails (out of launch attempts, #64) is reported like any other.
            autoresume.sweep_quietly(bus)
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
        from . import worker
        while bus.thread(tid)["status"] == "open" and bus.active_job(tid):
            time.sleep(2)
            worker.relaunch_stalled(bus)  # its worker may never have started (#64)
        _print_history(bus, tid, color=rex.stdout_is_tty())
    return 0


def needs_you(bus: Bus, ts: list[dict]) -> dict[str, str]:
    """Why each of ``ts`` waits for the human, and what they can do, by thread id (#84).

    A thread parked as needs_human says why from its state where that is recorded (a
    planned automatic resume, the review's follow-up budget), otherwise from the bus's
    latest notice on it, leaving out routine ones and a later debrief's.  An open thread
    with nothing running needs the human when its last job failed, or when an idle
    interactive session has mail there: only a nudge wakes it (#20)."""
    from . import autoresume
    planned = {r["thread_id"]: r for r in autoresume.pending(bus)}
    bus.reap()  # once, rather than in an active_job() per thread
    active = {r["thread_id"] for r in bus._all("SELECT thread_id FROM jobs WHERE status IN ('queued','running')")}
    out = {}
    for t in ts:
        tid = t["id"]
        if t["status"] == "needs_human":
            if tid in planned:
                agents = ", ".join(json.loads(planned[tid]["agents"]))
                due = time.localtime(planned[tid]["due"])
                when = time.strftime("%H:%M" if due[:3] == time.localtime()[:3] else "%a %d %b %H:%M", due)
                out[tid] = (f"waiting for {agents} to have usage again; resumes by itself about {when} "
                            f"(or `agent-bus resume` / `agent-bus cancel` {tid})")
                continue
            if (t["refs"] or {}).get("budget_parked"):
                out[tid] = ("the review still asks for changes after its follow-up budget: settle the findings, "
                            f"`agent-bus resume {tid}` to allow more follow-ups, or `agent-bus cancel {tid}`")
                continue
            notices = (bus.message(r["id"]) for r in bus._all(
                "SELECT id FROM messages WHERE thread_id=? AND sender=? AND kind='status' ORDER BY id DESC",
                tid, SYSTEM))
            # A debrief's notices are tagged; older builds' aren't, but fall inside its job's run.
            debriefs = [(j["created_at"], j["updated_at"] if j["status"] not in ("queued", "running") else math.inf)
                        for j in bus.jobs(tid) if j["kind"] == "debrief"]
            why = next((m["body"] for m in notices if not is_quiet(m) and not m["data"].get("debrief")
                        and not any(a <= m["created_at"] <= b for a, b in debriefs)), "")
            out[tid] = (f"{_one_line(why) or 'parked for the human user'} "
                        f"(`agent-bus resume {tid}` to retry, `agent-bus cancel {tid}` to dismiss)")
        elif t["status"] == "open" and tid not in active:
            jobs = [j for j in bus.jobs(tid) if j["kind"] != "debrief"]
            if jobs and jobs[-1]["status"] == "failed":
                out[tid] = (f"its last job failed: {_one_line(jobs[-1]['error'] or 'no reason recorded')} "
                            f"(`agent-bus resume {tid}` to retry, `agent-bus cancel {tid}` to dismiss)")
    idle = [t["id"] for t in ts if t["status"] == "open" and t["id"] not in active]
    if idle:
        for w in hooks.mail_waiting(bus, set(idle)):
            for tid in w["threads"]:
                note = f"{w['agent']} has unread mail here in an idle session ({w['session']}); nudge it"
                out[tid] = f"{out[tid]}; {note}" if tid in out else note
    return out


def _one_line(text: str, n: int = 160) -> str:
    """A notice's first line, whitespace collapsed, at most ``n`` characters."""
    line = " ".join((text.strip().splitlines() or [""])[0].split())
    return line if len(line) <= n else line[:n - 3] + "..."


def _print_history(bus: Bus, thread: str, color: bool) -> None:
    h = bus.history(thread)
    t = h["thread"]
    rounds = format_progress(t) if t["kind"] == "review" else f"r{t['round']}/{t['max_rounds']}"
    print(_color(SYSTEM, f"{t['id']} [{t['status']}] {t['kind']} {rounds}: {t['goal']}", color))
    if t["kind"] == "review" and (t["refs"] or {}).get("reviewers"):
        st = bus.review_standings(thread)
        print(_color(SYSTEM, f"required reviewers at head {str(st['head_commit_id'])[:12]}"
                     + (" (complete)" if st["complete"] else "") + ":\n" + format_review(st), color))
    for m in h["messages"]:
        print(format_message(m, color))


def add_mcp_args(s: argparse.ArgumentParser) -> None:
    """The `agent-bus mcp` options (shared with setup-codex, which reads them back)."""
    s.add_argument("--agent", required=True, choices=cfg.HARNESSES, help="the client's harness")
    s.add_argument("--participant", help="speak as this configured participant on that harness "
                                         "(default: the harness's own participant)")
    s.add_argument("--role")


class _ArgError(Exception):
    pass


class _QuietParser(argparse.ArgumentParser):
    def error(self, message: str):  # raise instead of printing and exiting
        raise _ArgError(message)


def parse_mcp_args(args: list[str]) -> argparse.Namespace | None:
    """``args`` (after ``mcp``) parsed exactly as `agent-bus mcp` would, or None if it
    would refuse them."""
    p = _QuietParser(prog="agent-bus mcp", add_help=False)
    add_mcp_args(p)
    try:
        return p.parse_args(args)
    except _ArgError:
        return None


# Commands that act on threads: the lazy sweep runs after them for those threads.
ACTING = ("cancel", "resume", "reply", "debrief")

EPILOG = """\
common tasks:
  agent-bus ask "SQLite or JSON for a small CLI?" --wait
                                   ask your agents and wait for their answer
  agent-bus threads                see what's open, and what needs you
  agent-bus tail t-1a2b3c          follow a discussion or review live
  agent-bus reply t-1a2b3c "Keep it simple"
                                   weigh in
  agent-bus resume t-1a2b3c        unstick a thread (out of rounds, failed)
  agent-bus cancel t-1a2b3c t-4d5e6f
                                   dismiss threads you no longer need

Commands for wiring up clients (mcp, hook, setup-codex, setup-copilot, run-job,
auto-resume, stream, rex-check) are left out of this list: see "Wiring up
clients" in the README.
"""

# The CLI's internal entry points, started by client configs, hooks and the worker.  A
# headless peer may still reach them: `mcp` refuses its calls itself, `hook` stays quiet.
INTERNAL = ("mcp", "hook", "run-job")
# Commands that act as the human user: posting with trust "human", or changing a thread
# or an agent's state on their behalf (also `agents --clear-quota`, see acts_as_human).
HUMAN_ONLY = ("ask", "reply", "resume", "cancel", "debrief")
# Set in the shells agent harnesses run commands in.  Session markers only, never config
# or auth variables a human may export in their own shell too.
HARNESS_ENV = ("AI_AGENT", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CODEX_THREAD_ID", "CODEX_SANDBOX", "CODEX_CI")


def acts_as_human(args: argparse.Namespace) -> bool:
    return args.cmd in HUMAN_ONLY or (args.cmd == "agents" and bool(args.clear_quota))


def agent_shell() -> str | None:
    """Why this process looks like an agent harness's shell tool ran it, or None."""
    for var in HARNESS_ENV:
        if os.environ.get(var):
            return f"${var} is set"
    found = harness_ancestor()
    return f"it runs under {found}" if found else None


def harness_ancestor() -> str | None:
    from . import procinfo
    return procinfo.harness_ancestor(os.getpid())


def guard(args: argparse.Namespace) -> str | None:
    """Keep agents off the human's commands (#84, agreed on agent-bus thread t-fc8b3b).

    A seatbelt against an agent posting as the human by mistake or because some text told
    it to, not a security boundary: any process running as this user can unset the
    markers, detach from its harness, or write the database directly.  The README says
    so, and how a human typing inside an agent's shell gets past it.
    """
    if os.environ.get("AGENT_BUS_PEER") and args.cmd not in INTERNAL:
        # history and tail would show a peer the blind proposals its MCP tools hide.
        return ("PEER_READ_ONLY: you are running as a headless agent-bus peer; your reply is posted for you. "
                "The agent-bus CLI is disabled in this session.")
    if acts_as_human(args) and os.environ.get("AGENT_BUS_ALLOW_HUMAN") != "1":
        why = agent_shell()
        if why:
            return (f"HUMAN_ONLY: `agent-bus {args.cmd}` acts as the human user, and this looks like an agent's "
                    f"shell ({why}). Agents: use the agent-bus MCP tools, which speak as you. Humans: run it "
                    "from your own terminal.")
    return None


def _targets(args: argparse.Namespace) -> list[str]:
    """The threads an ACTING command acts on (cancel takes several)."""
    return args.thread if isinstance(args.thread, list) else [args.thread]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="agent-bus", formatter_class=argparse.RawDescriptionHelpFormatter, epilog=EPILOG,
        description="Let your coding agents (Claude Code, Codex, Antigravity, Copilot CLI) talk to\n"
                    "each other, and step in when they need you.")
    # Only the human's commands are listed (#84).  The rest are started by client configs,
    # hooks, the worker and Rex: they keep their names, but have no help line here.
    sub = p.add_subparsers(dest="cmd", required=True, metavar="COMMAND", title="commands")

    s = sub.add_parser("ask", help="ask your agents a question and let them agree on an answer",
                       description="Start a discussion: each agent answers on its own first, then they vote "
                                   "until they agree or run out of rounds.")
    s.add_argument("prompt", help="the question")
    s.add_argument("--with", dest="with_", help="comma-separated agents (default: every available agent)")
    s.add_argument("--repo", help="an allow-listed repo the agents may read")
    s.add_argument("--rounds", type=int, help="max rounds (default from config, 4)")
    s.add_argument("--deadline", type=int, help="deadline in minutes (default from config, 30)")
    s.add_argument("--watch", action="store_true", help="inside Rex: open panes following each agent")
    s.add_argument("--set", dest="settings", action="append", default=[], metavar="AGENT.KEY=VALUE",
                   help="per-agent model/effort for this discussion, e.g. codex.effort=high (repeatable)")
    s.add_argument("--wait", action="store_true", help="wait for the outcome, then print the thread")

    s = sub.add_parser("threads", help="what's going on, and which threads need you",
                       description="Open discussions and reviews, the ones waiting for you first, with why "
                                   "they need you and what you can do about it.")
    s.add_argument("--all", action="store_true", help="include resolved and cancelled threads")
    s.add_argument("--json", action="store_true")

    s = sub.add_parser("history", help="read a thread from the start")
    s.add_argument("thread")
    s.add_argument("--json", action="store_true")

    s = sub.add_parser("tail", help="follow a thread live",
                       description="Print a thread's messages as they arrive.  Inside Rex the pane also shows "
                                   "the thread's state (working, done, needs you).")
    s.add_argument("thread")
    s.add_argument("--peer", help="also show what this agent is doing (tools, thinking)")
    s.add_argument("--events", action="store_true", help="also show what every agent is doing")
    s.add_argument("--once", action="store_true", help="print what's there now and exit")
    s.add_argument("--no-status", action="store_true", help="don't report the thread's state to Rex (OSC 7501)")
    s.add_argument("--no-color", action="store_true")
    s.add_argument("--notify", action="store_true",
                   help="desktop notification when an idle agent session has mail on this thread")

    s = sub.add_parser("reply", help="say something on a thread as yourself",
                       description="Post to a thread as the human user.  Agents see it as coming from you.")
    s.add_argument("thread")
    s.add_argument("body")
    s.add_argument("--to", help="the agent to address")

    s = sub.add_parser("resume", help="unstick a thread: give it more rounds or retry what failed")
    s.add_argument("thread")
    s.add_argument("--rounds", type=int, default=2, help="extra rounds to allow (default 2)")

    s = sub.add_parser("cancel", help="stop threads and dismiss them",
                       description="Cancel threads, stopping any agent still working on them.  Use it to "
                                   "dismiss threads you no longer need, including ones stuck waiting for you.")
    s.add_argument("thread", nargs="+")

    s = sub.add_parser("debrief", help="ask the agents of a finished thread what got in their way")
    s.add_argument("thread")
    s.add_argument("--question", help="what to ask (default: friction with prompts, tools, sandbox, diffs, "
                                      "verdict format)")
    s.add_argument("--peer", action="append", dest="peers", help="an agent to ask (repeatable; default: all)")

    s = sub.add_parser("agents", help="which agents you have, and whether they're out of usage")
    s.add_argument("--json", action="store_true")
    s.add_argument("--clear-quota", metavar="AGENT",
                   help="forget that AGENT is out of usage (e.g. after upgrading a plan); this lifts it for "
                        "every participant sharing its harness and account")

    s = sub.add_parser("usage", help="how many tokens your agents' turns used, per agent and per thread")
    s.add_argument("--hours", type=float, default=24, help="how far back to look (default 24)")
    s.add_argument("--thread", help="only this thread")
    s.add_argument("--agent", help="only this agent")
    s.add_argument("--limit", type=int, default=20, help="most threads to list (default 20)")
    s.add_argument("--json", action="store_true")

    s = sub.add_parser("watch", help="inside Rex: open panes following a thread and each agent on it")
    s.add_argument("thread")
    s.add_argument("--no-peers", action="store_true", help="only the thread, not each agent")

    # ---- Not listed: client wiring, the worker, and integrations (see the README's
    # "Wiring up clients").  Keep these names stable: configs, hooks and agents use them.
    add_mcp_args(sub.add_parser("mcp", description="Run the stdio MCP server for one agent (started by the "
                                                   "client's MCP config)."))

    s = sub.add_parser("hook", description="Print a fixed unread-mail hint at turn boundaries (called by "
                                           "client hooks).")
    s.add_argument("--client", required=True)
    s.add_argument("--event", required=True)

    s = sub.add_parser("setup-codex", description="Register agent-bus with Codex, forwarding Rex variables "
                                                  "(run by the Home Manager module).")
    s.add_argument("--bin", help="agent-bus executable Codex should run (default: this one)")
    grp = s.add_mutually_exclusive_group()
    grp.add_argument("--hooks", action="store_true",
                     help="register agent-bus fixed-hint hooks in Codex's hooks.json")
    grp.add_argument("--no-hooks", action="store_true",
                     help="remove agent-bus hooks from Codex's hooks.json")
    s.add_argument("--hooks-only", action="store_true",
                   help="only update Codex's hooks.json; leave config.toml untouched")
    s.add_argument("--hooks-bin", help="stable agent-bus path for hooks.json (default: --bin)")

    s = sub.add_parser("setup-copilot", description="Register agent-bus with GitHub Copilot CLI (run by the "
                                                    "Home Manager module).")
    s.add_argument("--bin", help="agent-bus executable Copilot should run (default: this one)")

    s = sub.add_parser("run-job", description="Run a queued job (started by the MCP server or CLI).")
    s.add_argument("job")

    s = sub.add_parser("auto-resume", description="Resume threads parked by a usage limit that has reset. "
                                                  "Runs by itself; this shows what's planned.")
    s.add_argument("--wait", action="store_true", help="(used internally) keep sweeping until none is planned")

    s = sub.add_parser("stream", description="Print one line per verdict, final answer, status change, failed "
                                             "job or idle session with mail, for notification hooks.")
    s.add_argument("threads", nargs="*", help="only these threads; exit once they're all closed")
    s.add_argument("--kinds", default="review_verdict,final,mail",
                   help="message kinds to report, plus `mail` for interactive sessions with unread mail "
                        "(default: review_verdict,final,mail)")
    s.add_argument("--from-start", action="store_true", help="also report existing messages")
    s.add_argument("--interval", type=float, default=2.0)

    s = sub.add_parser("rex-check", description="Report what Rex shows about an agent session's block "
                                                "(read-only diagnostics).")
    s.add_argument("--session", help="session id (default: the latest session of each agent)")
    s.add_argument("--block", help="Rex block to check instead of a session's")
    s.add_argument("--agent", help="harness expected in --block; alone, only that participant's (or "
                                   "harness's) latest session")
    s.add_argument("--json", action="store_true")

    args = p.parse_args(argv)
    refused = guard(args)
    if refused:
        print(f"agent-bus: {refused}", file=sys.stderr)
        return 2

    if args.cmd == "mcp":
        from . import mcp_server
        mcp_server.main(args.agent, args.role, participant=args.participant)
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
    if args.cmd == "setup-copilot":
        from . import copilot_setup
        me = cfg.self_command()
        if not args.bin and len(me) != 1:
            print("agent-bus: setup-copilot: can't tell where agent-bus is installed; pass --bin", file=sys.stderr)
            return 1
        try:
            print(f"copilot: agent-bus entry {copilot_setup.ensure(os.path.abspath(args.bin or me[0]))}")
        except (copilot_setup.SetupError, OSError) as e:
            print(f"agent-bus: setup-copilot: {e}", file=sys.stderr)
            return 1
        return 0
    if args.cmd == "run-job":
        from . import worker
        return worker.main(args.job)

    bus = Bus()
    done = False
    try:
        rc = _dispatch(bus, args)
        done = True
        return rc
    except BusError as e:
        print(f"agent-bus: {e.code}: {e.message}", file=sys.stderr)
        return 2
    except ValueError as e:
        print(f"agent-bus: {e}", file=sys.stderr)
        return 2
    finally:
        if args.cmd in ACTING:
            # After a command acting on a thread (which _dispatch's sweep left out), sweep
            # that thread too: resuming one whose job never started relaunches it (#64).  A
            # command that failed leaves its threads out: `cancel a typo` must not resume a.
            from . import autoresume
            autoresume.sweep_quietly(bus, exclude=frozenset() if done else frozenset(_targets(args)))
        bus.close()


def _dispatch(bus: Bus, args: argparse.Namespace) -> int:
    from . import autoresume
    if args.cmd == "auto-resume":
        if args.wait:
            return autoresume.wait(bus)
        for tid in autoresume.sweep(bus):
            print(f"resumed {tid}")
        planned = autoresume.pending(bus)
        for r in planned:
            print(f"{r['thread_id']}: resumes after {', '.join(json.loads(r['agents']))} "
                  f"{'has' if len(json.loads(r['agents'])) == 1 else 'have'} usage again "
                  f"(about {time.strftime('%Y-%m-%d %H:%M %Z', time.localtime(r['due']))})")
        if not planned:
            print("no automatic resumes planned")
        return 0
    # A thread whose usage limit has reset resumes now, except one this command changes
    # (cancel, resume, reply): the explicit action goes first.
    acting = args.cmd in ACTING
    if args.cmd != "stream":  # which sweeps in its own loop
        autoresume.sweep_quietly(bus, exclude=frozenset(_targets(args) if acting else ()))
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
            subj = quota.subject(conf, conf.participant(args.clear_quota).name)
            with bus.tx():
                n = bus.conn.execute("DELETE FROM quota_blocks WHERE agent=?", (subj,)).rowcount
                bus.due_now(quota.sharing(conf, subj))
            print(f"cleared {n} out-of-usage block(s) for {args.clear_quota}"
                  + (f" ({subj}, shared by {', '.join(quota.sharing(conf, subj))})"
                     if quota.sharing(conf, subj) != [args.clear_quota] else ""))
            for tid in autoresume.sweep(bus):  # threads that waited only on it go now
                print(f"resumed {tid}")
            return 0
        avail = conf.availability()
        from .peers import cli_version, web_search_report
        for a, v in avail.items():
            harness = conf.harness_of(a)
            if harness != a:
                v["harness"] = harness
            # What its usage blocks and readings are kept under, and where Codex's come from.
            if conf.account(a):
                v["account"] = conf.account(a)
            if conf.codex_home(a):
                v["codex_home"] = conf.codex_home(a)
            if v["available"]:
                v["version"] = cli_version(bus, harness, conf.peer_command(a))
                v["web_search"] = web_search_report(a, conf)
        # Keyed by harness and account: listed under every participant they apply to.
        blocks = quota.by_participant(conf, bus.quota_blocks())
        limits = quota.by_participant(conf, bus.usage_limits())
        if args.json:
            for a, v in avail.items():
                if a in blocks:
                    v["out_of_usage"] = [quota.summary(b) for b in blocks[a]]
                if a in limits:
                    v["usage_limits"] = [quota.limits_summary(w, conf.usage_warn_at) for w in limits[a]]
                    v["near_limit"] = any(w["used"] >= conf.usage_warn_at for w in limits[a])
            print(json.dumps(avail, indent=2))
            return 0
        source = "config `agents`" if conf.agents is not None else "detected on PATH"
        print(f"agents ({source}):")
        for a, v in avail.items():
            print(f"  {'yes' if v['available'] else 'no ':<4} {a:<12} {v['reason']}"
                  + (f" ({v['version']})" if v.get("version") else "")
                  + (f" [{', '.join(f'{k} {v[k]}' for k in ('harness', 'account') if v.get(k))}]"
                     if v.get("harness") or v.get("account") else ""))
            if v.get("codex_home"):
                print(f"       CODEX_HOME {v['codex_home']}")
            if v.get("web_search"):
                print(f"       web search as a peer: {'on, ' if v['web_search']['enabled'] else ''}"
                      f"{v['web_search']['note']}")
            if limits.get(a):
                near = any(w["used"] >= conf.usage_warn_at for w in limits[a])
                print(f"       usage limits{' (NEAR THE LIMIT)' if near else ''}: "
                      + "; ".join(map(quota.describe_window, limits[a])))
            if conf.bedrock(a):
                print(f"       Amazon Bedrock: AWS_PROFILE {conf.aws_profile(a) or '(default)'}, "
                      f"AWS_REGION {conf.aws_region(a) or '(default)'}")
            for b in blocks.get(a, []):
                when = time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(b["until"]))
                if quota.is_auth(b):
                    print(f"       AUTH_REQUIRED: {quota.describe(a, b)}; to fix it, {quota.auth_hint(conf, a)}")
                    continue
                print(f"       {'throttled' if b['source'] == 'throttle' else 'out of usage'}{' for model ' + b['scope'] if b['scope'] else ''} until {when}"
                      f"{' (estimated)' if b['estimated'] else ''}: {b['reason'] or 'usage limit reached'}")
        return 0
    if args.cmd == "usage":
        from . import usage
        if not 0 < args.hours <= bus.USAGE_KEEP_S / 3600 or args.limit < 0:  # rejects nan too
            raise ValueError(f"--hours must be more than 0 and at most {bus.USAGE_KEEP_S // 3600} (the history "
                             "kept), and --limit not negative")
        if args.thread:
            bus.thread(args.thread)  # NO_SUCH_THREAD for a typo, not an empty report
        rep = usage.report(bus, hours=args.hours, thread=args.thread, agent=args.agent, limit=args.limit)
        print(json.dumps(rep, indent=2) if args.json else usage.format_report(rep))
        return 0
    if args.cmd == "threads":
        ts = bus.threads(include_closed=args.all)
        # Threads that need the human are listed however many newer ones there are.
        seen = {t["id"] for t in ts}
        older = [bus.thread(r["id"]) for r in bus._all(
            "SELECT id FROM threads WHERE status IN ('open','needs_human') ORDER BY updated_at DESC")
            if r["id"] not in seen]
        attention = needs_you(bus, ts + older)
        ts += [t for t in older if t["id"] in attention]
        # The threads waiting for the human come first (#84); otherwise most recent first.
        ts.sort(key=lambda t: t["id"] not in attention)
        if args.json:
            print(json.dumps([{**thread_summary(t), "needs_you": attention.get(t["id"]),
                               **({"review": bus.review_standings(t["id"])} if t["kind"] == "review" else {})}
                              for t in ts], indent=2))
            return 0
        if not ts:
            print("no threads" if args.all else "no open threads (use --all to include closed ones)")
            return 0
        rows = []
        for t in ts:
            job = bus.active_job(t["id"])
            # Reviews: pass and follow-ups against the budget (#71), e.g. "pass5,fu2/3".
            rounds = (format_progress(t).replace("pass ", "pass").replace(", follow-ups ", ",fu")
                      .replace(", budget warning", ",warn").replace(", waiting for human", ",human")
                      if t["kind"] == "review" else f"{t['round']}/{t['max_rounds']}")
            rows.append((t["id"], t["status"], t["kind"], rounds,
                         job["status"] if job else "-", ",".join(t["participants"]), t["goal"]))
        if attention:
            n = len(attention)
            print(f"{n} thread{'' if n == 1 else 's'} need{'s' if n == 1 else ''} you; "
                  "`agent-bus history <thread>` for the details\n")
        head = ("THREAD", "STATUS", "KIND", "ROUND", "WORKER", "PARTICIPANTS")
        widths = [max(len(head[i]), *(len(r[i]) for r in rows)) for i in range(5)]
        line = lambda cols: "  ".join(c.ljust(w) for c, w in zip(cols, widths)) + "  " + cols[5]
        print(line(head))
        indent = " " * (widths[0] + 2)
        for r in rows:
            print(line(r))
            print(indent + " ".join(r[6].split())[:100])
            if r[2] == "review" and (bus.thread(r[0])["refs"] or {}).get("reviewers"):
                st = bus.review_standings(r[0])
                print(indent + "; ".join(f"{who} {v['state'].replace('_', ' ')}"
                                         for who, v in st["reviewers"].items()))
            if r[0] in attention:
                print(_color(HUMAN, f"{indent}needs you: {attention[r[0]]}", color))
        return 0
    if args.cmd == "history":
        if args.json:
            h = bus.history(args.thread)
            print(json.dumps({"thread": thread_summary(h["thread"]),
                              **({"review": bus.review_standings(args.thread)}
                                 if h["thread"]["kind"] == "review" else {}),
                              "messages": [present(m) for m in h["messages"]]}, indent=2))
        else:
            _print_history(bus, args.thread, color)
        return 0
    if args.cmd == "reply":
        svc.send(args.body, thread=args.thread, to=args.to)
        print(f"posted to {args.thread}")
        return 0
    if args.cmd == "cancel":
        targets = list(dict.fromkeys(args.thread))
        for tid in targets:  # all or nothing: a typo cancels none of them
            bus.thread(tid)
        for tid in targets:
            res = svc.cancel(tid)
            print(f"cancelled {tid}" + (f"; signalled worker {res['signalled']}" if res["signalled"] else ""))
        return 0
    if args.cmd == "debrief":
        res = svc.debrief(args.thread, question=args.question, peers=args.peers)
        for p, why in (res.get("skipped_peers") or {}).items():
            print(f"skipped {p}: {why}")
        print(f"debriefing {', '.join(res['peers'])} on {args.thread} (job {res['job']['id']}); "
              f"follow it with `agent-bus tail {args.thread}`")
        return 0
    if args.cmd == "resume":
        res = svc.resume(args.thread, extra_rounds=args.rounds)
        t = res["thread"]
        limit = (f"follow-ups {t['follow_ups']}, limit {t['followup_limit']}" if t["kind"] == "review"
                 else f"max rounds {t['max_rounds']}")
        print(f"resumed {t['id']}: status {t['status']}, {limit}"
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
