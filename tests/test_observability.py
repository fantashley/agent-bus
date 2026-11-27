"""#161: why each turn ran (turn_start purpose), failed tool calls in the event stream, a
per-pass usage view, and the verdict format's version in the review prompt."""
import json
from pathlib import Path

from agent_bus import cli, prompts, usage
from agent_bus.db import HUMAN, Bus
from agent_bus.peers import StreamState, adapter
from agent_bus import config as cfg
from agent_bus.service import Service

from test_fixtures import replay
from test_review_rounds import BOTH, calls, git_repo, run_job

STREAMS = Path(__file__).parent / "data" / "streams"


def starts(bus, tid):
    return [(e["agent"], e["purpose"], e["text"].split(" · ")[0]) for e in bus.events(tid) if e["kind"] == "turn_start"]


def test_turn_purposes_and_the_per_pass_usage_view(env, no_spawn, monkeypatch, capsys):
    """codex asks for changes; after the fix antigravity approves, and codex approves only
    once a repair turn asks it again: one pass each, codex's second confirm-only, with two turns."""
    bus, repo = env["bus"], env["repo"]
    base, commit = git_repo(repo)
    h1 = commit("helo world\n")
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review(None, str(repo), base, h1, change_id="PR-1", reviewers=BOTH)["thread"]["id"]
    run_job(bus, tid)
    h2 = commit("hello world\n")
    claude.request_review(None, str(repo), base, h2, prev_head_commit_id=h1, reviewers=BOTH)
    monkeypatch.setenv("FAKE_APPROVE", "antigravity")
    monkeypatch.setenv("FAKE_REPLY_CODEX", "Looks fine, no JSON here.")
    monkeypatch.setenv("FAKE_REPAIR_CODEX", json.dumps({"verdict": "approve", "summary": "fixed", "findings": [],
                                                        "earlier": [{"id": "codex-1", "status": "fixed"}]}))
    run_job(bus, tid)
    assert [c["agent"] for c in calls(env)] == ["codex", "antigravity", "codex", "codex"]
    assert starts(bus, tid) == [("codex", "pass", "pass 1"), ("antigravity", "pass", "pass 1"),
                                ("codex", "pass", "pass 2 (resumed)"), ("codex", "repair", "pass 2 repair (resumed)")]

    rep = usage.report(bus, hours=1, thread=tid)
    got = [(p["agent"], p["kind"], p["verdict"], p["purposes"], p["turns"]) for p in rep["passes"]]
    assert got == [("codex", "first pass", "request_changes", ["pass"], 1),
                   ("antigravity", "first pass", "approve", ["pass"], 1),
                   ("codex", "confirm-only", "approve", ["pass", "repair"], 2)]
    assert rep["passes"][0]["head"] == h1[:12] and rep["passes"][2]["head"] == h2[:12]
    assert rep["passes"][2]["uncached_input"] > 0 and rep["passes"][2]["output"] > 0
    assert "passes" not in usage.report(bus, hours=1)  # only for one thread

    assert cli.main(["usage", "--thread", tid]) == 0
    out = capsys.readouterr().out
    assert "by pass (oldest first):" in out
    line = next(l for l in out.splitlines() if "confirm-only" in l)
    assert line.split()[:4] == ["codex", "confirm-only", "2", "(repair)"] and line.endswith(f"approve at {h2[:12]}")


def test_pass_kinds():
    high = {"id": "codex-2", "severity": "high", "file": "a", "line": 1, "comment": "x"}
    assert usage._pass_kind({"findings": [high]}, None) == "first pass"
    assert usage._pass_kind({"findings": [high]}, {"codex-1"}) == "new blocker"
    assert usage._pass_kind({"findings": [high]}, {"codex-2"}) == "still blocking"  # carried with its id
    assert usage._pass_kind({"findings": [], "earlier": [{"id": "codex-1", "status": "still_blocking"}]},
                            {"codex-1"}) == "still blocking"
    assert usage._pass_kind({"findings": [{**high, "severity": "low"}, {**high, "pre_existing": True}]},
                            {"codex-1"}) == "confirm-only"
    # Against what the previous verdict left open, as the worker charges the budget (codex-2):
    # a pre-existing finding's id made blocking later is a new blocker.
    before = {"verdict": "request_changes", "findings": [{**high, "pre_existing": True}]}
    assert usage._prior(before) == set()
    assert usage._pass_kind({"findings": [high]}, usage._prior(before)) == "new blocker"
    assert usage._prior({"verdict": "request_changes", "findings": [high]}) == {"codex-2"}
    assert usage._prior({"verdict": "approve", "findings": [high]}) == set()


def test_a_pass_run_again_is_a_retry_and_flags_show_per_pass(env, no_spawn, monkeypatch, capsys):
    """codex-5: a pass whose turns gave no verdict, run again after `resume`, is a retry; and
    codex-4: a pass's counts say when they are short."""
    bus, repo = env["bus"], env["repo"]
    base, commit = git_repo(repo)
    h1 = commit("helo world\n")
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review("codex", str(repo), base, h1, change_id="PR-1")["thread"]["id"]
    monkeypatch.setenv("FAKE_REPLY_CODEX", "No JSON.")
    monkeypatch.setenv("FAKE_REPAIR_CODEX", "Still no JSON.")
    run_job(bus, tid)
    assert bus.thread(tid)["status"] == "needs_human"
    monkeypatch.delenv("FAKE_REPLY_CODEX")
    monkeypatch.delenv("FAKE_REPAIR_CODEX")
    Service(bus, HUMAN).resume(tid)
    run_job(bus, tid)
    assert [(p, label) for _, p, label in starts(bus, tid)] == [
        ("pass", "pass 1"), ("repair", "pass 1 repair (resumed)"), ("retry", "pass 1 retry")]
    bus.conn.execute("UPDATE turn_usage SET partial=1 WHERE id=(SELECT MAX(id) FROM turn_usage)")
    rep = usage.report(bus, hours=1, thread=tid)
    assert [(p["kind"], p["purposes"], p["partial_turns"]) for p in rep["passes"]] == [
        ("no verdict", ["pass", "repair"], 0), ("first pass", ["retry"], 1)]
    assert cli.main(["usage", "--thread", tid]) == 0
    out = capsys.readouterr().out
    assert "1 (retry)" in out and "1 counted in part" in out


def test_a_discussion_has_no_per_pass_view(env, no_spawn):
    bus = env["bus"]
    tid = Service(bus, HUMAN).start_discussion("Which DB?", peers=["claude", "codex"])["thread"]["id"]
    # codex-5: as if an earlier worker had started codex's round-1 turn and died.
    bus.add_event(tid, "turn_start", "round 1 · fake-codex 0.0.0", agent="codex", purpose="pass")
    run_job(bus, tid)
    got = {(e["agent"], e["purpose"]) for e in bus.events(tid) if e["kind"] == "turn_start"}
    assert ("codex", "retry") in got and ("claude", "pass") in got and ("claude", "retry") not in got
    assert "passes" not in usage.report(bus, hours=1, thread=tid)


def test_an_old_events_table_gets_the_purpose_column(env):
    path = env["tmp"] / "old.db"
    bus = Bus(path)
    bus.conn.execute("ALTER TABLE events DROP COLUMN purpose")
    bus.conn.execute("INSERT INTO events (thread_id, kind, text, created_at) VALUES ('t-x', 'turn_start', 'round 1', 1)")
    again = Bus(path)
    assert [e["purpose"] for e in again.events("t-x")] == [None]


# ------------------------------------------------------------ failed tool calls


def parse(agent, lines):
    ad = adapter(agent, cfg.Config.load())
    st, events = StreamState(), []
    for obj in lines:
        ad.parse_line(obj, st, lambda kind, text: events.append((kind, text)))
    return events


def fixture(name):
    return [json.loads(l) for l in (STREAMS / name).read_text().splitlines() if l.strip()]


def test_antigravity_tool_error_is_reported(env):
    """The real denied run_command stream: its ERROR step is a tool_failed event."""
    events = []
    ad = replay(env, "antigravity", STREAMS / "antigravity-denied-command.jsonl")
    ad.run("prompt", str(env["repo"]), 30, lambda kind, text: events.append((kind, text)))
    failed = [t for k, t in events if k == "tool_failed"]
    assert len(failed) == 1 and failed[0].startswith("run_command: permission check failed")


def test_codex_failed_command_is_reported(env):
    """codex-start's command, as if it had exited 1 (the item's own exit_code and status)."""
    lines = fixture("codex-start.jsonl")
    assert not [e for e in parse("codex", lines) if e[0] == "tool_failed"]
    done = next(o for o in lines if o["type"] == "item.completed" and o["item"]["type"] == "command_execution")
    done["item"].update(exit_code=1, status="failed")
    failed = [t for k, t in parse("codex", lines) if k == "tool_failed"]
    assert len(failed) == 1 and failed[0].startswith("shell exit 1: ") and "notes.txt" in failed[0]
    done["item"].update(exit_code=None, status="declined")
    assert [t.split(":")[0] for k, t in parse("codex", lines) if k == "tool_failed"] == ["shell declined"]


def test_claude_tool_error_result_is_reported(env):
    """claude-start's Read, as if its result had come back as an error."""
    lines = fixture("claude-start.jsonl")
    assert not [e for e in parse("claude", lines) if e[0] == "tool_failed"]
    res = next(o for o in lines if o["type"] == "user")
    block = res["message"]["content"][0]
    block.update(is_error=True, content="<tool_use_error>File does not exist.</tool_use_error>")
    assert [t for k, t in parse("claude", lines) if k == "tool_failed"] == [
        "Read: <tool_use_error>File does not exist.</tool_use_error>"]


def test_copilot_failed_tool_is_reported(env):
    lines = [{"type": "tool.execution_start", "data": {"toolCallId": "c1", "toolName": "bash", "arguments": {}}},
             {"type": "tool.execution_complete", "data": {"toolCallId": "c1", "success": False,
                                                         "error": {"message": "exit status 2"}}},
             {"type": "tool.execution_complete", "data": {"toolCallId": "c2", "success": True}}]
    assert [e for e in parse("copilot", lines) if e[0] == "tool_failed"] == [("tool_failed", "bash: exit status 2")]


def test_codex_collab_tool_failure_is_reported(env):
    """codex-7: codex 0.172.0 streams its collaboration tools as collab_tool_call items."""
    item = {"id": "item_3", "type": "collab_tool_call", "tool": "spawn_agent", "status": "in_progress"}
    events = parse("codex", [{"type": "item.started", "item": item},
                             {"type": "item.completed", "item": {**item, "status": "failed"}}])
    assert events == [("tool", "collab_tool_call spawn_agent"), ("tool_failed", "collab_tool_call spawn_agent failed")]


def test_events_only_shows_failed_tools(env, capsys):
    bus = env["bus"]
    tid = Service(bus, "claude", bus.register_session("claude")).send(to="codex", body="hi")["thread"]["id"]
    bus.add_event(tid, "tool", "shell false", agent="codex")
    bus.add_event(tid, "tool_failed", "shell exit 1: false", agent="codex")
    bus.add_event(tid, "tool_failed", "Bash: line one\nline two \x1b[31mred", agent="claude")
    assert cli.main(["tail", tid, "--events-only", "--once", "--no-color"]) == 0
    out = capsys.readouterr().out
    assert "codex tool: shell false" in out and "codex tool_failed: shell exit 1: false" in out
    # codex-6: one line per event, with no raw control characters.
    assert "  · claude tool_failed: Bash: line one\\nline two \\x1b[31mred\n" in out and "\x1b" not in out


def test_tail_shows_every_event_kind(env, capsys):
    """#179: no hard-coded list of kinds, so `snapshot` and kinds added later are shown;
    --events-only leaves out only the peers' streamed text and thinking."""
    bus = env["bus"]
    tid = Service(bus, "claude", bus.register_session("claude")).send(to="codex", body="hi")["thread"]["id"]
    bus.add_event(tid, "snapshot", "full snapshot of head abc")
    bus.add_event(tid, "some_future_kind", "NEW-KIND")
    bus.add_event(tid, "text", "PEER-TEXT", agent="codex")
    bus.add_event(tid, "thinking", "PEER-THINKING", agent="codex")
    assert cli.main(["tail", tid, "--events", "--once", "--no-color", "--no-status"]) == 0
    out = capsys.readouterr().out
    assert "worker snapshot: full snapshot of head abc" in out and "worker some_future_kind: NEW-KIND" in out
    assert "codex text: PEER-TEXT" in out and "codex thinking: PEER-THINKING" in out
    assert cli.main(["tail", tid, "--events-only", "--once", "--no-color", "--no-status"]) == 0
    only = capsys.readouterr().out
    assert "worker snapshot:" in only and "worker some_future_kind: NEW-KIND" in only
    assert "PEER-TEXT" not in only and "PEER-THINKING" not in only


# ------------------------------------------------------------ the verdict format label


def test_review_prompt_names_the_verdict_format_and_build(env, no_spawn):
    bus, repo = env["bus"], env["repo"]
    base, commit = git_repo(repo)
    h1 = commit("helo world\n")
    claude = Service(bus, "claude", bus.register_session("claude"))
    run_job(bus, claude.request_review("codex", str(repo), base, h1, change_id="PR-1")["thread"]["id"])
    prompt = calls(env)[0]["prompt"]
    assert f"Verdict format v{prompts.VERDICT_FORMAT_VERSION}," in prompt and "agent-bus 0." in prompt
