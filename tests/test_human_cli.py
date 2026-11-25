"""The human's CLI apart from the plumbing, and the guard on its commands (#84)."""

import re

import pytest

from agent_bus import cli, procinfo
from agent_bus.db import SYSTEM

HUMAN_CMDS = ("ask", "threads", "history", "tail", "reply", "resume", "cancel", "finish", "debrief", "agents", "usage",
              "watch")
PLUMBING = ("mcp", "hook", "setup-codex", "setup-copilot", "run-job", "auto-resume", "stream", "rex-check")


def _help(capsys, *argv) -> str:
    with pytest.raises(SystemExit) as e:
        cli.main([*argv, "--help"])
    assert e.value.code == 0
    return capsys.readouterr().out


def test_top_level_help_lists_only_the_humans_commands(capsys):
    out = _help(capsys)
    listed = out.split("commands:", 1)[1].split("common tasks:", 1)[0]
    names = {m.group(1) for m in re.finditer(r"^    (\S+)  ", listed, re.M)}
    assert names == set(HUMAN_CMDS)
    assert "common tasks:" in out and "agent-bus cancel t-1a2b3c t-4d5e6f" in out
    assert max(map(len, out.split("common tasks:", 1)[1].splitlines())) <= 80
    assert "Wiring up clients" in " ".join(out.split())
    assert "OSC" not in listed and "called by" not in listed  # what you get, not how


@pytest.mark.parametrize("cmd", PLUMBING)
def test_plumbing_keeps_its_name_and_help(capsys, cmd):
    assert "usage: agent-bus " + cmd in _help(capsys, cmd)


def test_agents_documented_invocations_still_parse(env, capsys):
    t = env["bus"].create_thread("discussion", "g", ["claude", "codex"], "claude")
    env["bus"].set_thread(t["id"], status="resolved")  # so stream exits
    assert cli.main(["tail", t["id"], "--events", "--once"]) == 0
    assert cli.main(["stream", t["id"], "--from-start", "--interval", "0"]) == 0


# ------------------------------------------------------------------ the guard


def _marker(monkeypatch, var="CLAUDECODE"):
    monkeypatch.setenv(var, "1")


@pytest.mark.parametrize("var", cli.HARNESS_ENV)
def test_reply_refused_in_an_agent_shell(env, capsys, monkeypatch, var):
    bus = env["bus"]
    t = bus.create_thread("discussion", "g", ["claude", "codex"], "claude")
    before = len(bus.history(t["id"])["messages"])
    _marker(monkeypatch, var)
    assert cli.main(["reply", t["id"], "I am the human"]) == 2
    err = capsys.readouterr().err
    assert "HUMAN_ONLY" in err and f"${var}" in err and "MCP tools" in err
    assert "AGENT_BUS_ALLOW_HUMAN" not in err  # the override isn't handed to the agent
    assert len(bus.history(t["id"])["messages"]) == before


def test_harness_ancestor_refuses_too(env, capsys, monkeypatch):
    t = env["bus"].create_thread("discussion", "g", ["claude", "codex"], "claude")
    monkeypatch.setattr(cli, "harness_ancestor", lambda: "codex (pid 42)")
    assert cli.main(["cancel", t["id"]]) == 2
    assert "codex (pid 42)" in capsys.readouterr().err
    assert env["bus"].thread(t["id"])["status"] == "open"


@pytest.mark.parametrize("argv", [["ask", "q"], ["resume", "t-x"], ["cancel", "t-x"], ["debrief", "t-x"],
                                  ["agents", "--clear-quota", "codex"]])
def test_every_human_command_is_guarded(env, capsys, monkeypatch, argv):
    _marker(monkeypatch)
    assert cli.main(argv) == 2
    assert "HUMAN_ONLY" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [["threads"], ["usage"], ["agents"], ["auto-resume"]])
def test_reading_stays_open_in_an_agent_shell(env, capsys, monkeypatch, argv):
    _marker(monkeypatch)
    assert cli.main(argv) == 0


def test_override_lets_a_human_in_an_agent_shell_through(env, capsys, monkeypatch):
    t = env["bus"].create_thread("discussion", "g", ["claude", "codex"], "claude")
    _marker(monkeypatch)
    monkeypatch.setenv("AGENT_BUS_ALLOW_HUMAN", "1")
    assert cli.main(["reply", t["id"], "really me"]) == 0
    assert env["bus"].history(t["id"])["messages"][-1]["trust"] == "human"


@pytest.mark.parametrize("argv", [["history", "t-x"], ["tail", "t-x", "--once"], ["threads"], ["stream"],
                                  ["reply", "t-x", "hi"]])
def test_headless_peer_gets_no_cli_even_with_the_override(env, capsys, monkeypatch, argv):
    monkeypatch.setenv("AGENT_BUS_PEER", "1")
    monkeypatch.setenv("AGENT_BUS_ALLOW_HUMAN", "1")
    assert cli.main(argv) == 2
    assert "PEER_READ_ONLY" in capsys.readouterr().err


def test_headless_peer_still_reaches_the_hook(env, capsys, monkeypatch):
    monkeypatch.setenv("AGENT_BUS_PEER", "1")
    monkeypatch.setattr("sys.stdin", open("/dev/null"))
    assert cli.main(["hook", "--client", "claude", "--event", "prompt"]) == 0


@pytest.mark.parametrize("argv,found", [
    (["claude", "--continue"], "claude"),
    (["/nix/store/x/bin/.claude-unwrapped"], "claude"),
    (["/nix/store/x/bin/.codex-wrapped", "app-server"], "codex"),
    (["-agy"], "agy"),
    (["node", "/usr/lib/node_modules/@github/copilot/index.js"], "@github/copilot"),
    (["node", "--no-warnings", "/opt/bin/codex"], "codex"),
    (["python3.12", "/usr/bin/copilot"], "copilot"),
    (["bash", "-c", "claude"], None),  # never a substring of the rest of argv
    (["vim", "codex.md"], None),
    (["claudette"], None),
    ([], None),
])
def test_harness_in_argv(argv, found):
    assert procinfo.harness_in_argv(argv) == found


def test_harness_ancestor_skips_unreadable():
    argvs = {10: None, 11: ["zsh"], 12: [".codex-wrapped"]}
    assert procinfo.harness_ancestor(1, ancestors_of=lambda pid: [10, 11, 12], argv_of=argvs.get) == \
        "codex (pid 12)"
    assert procinfo.harness_ancestor(1, ancestors_of=lambda pid: [10, 11], argv_of=argvs.get) is None


# ------------------------------------------------------------------ triage


def test_threads_puts_the_ones_needing_you_first_and_says_why(env, capsys):
    bus = env["bus"]
    parked = bus.create_thread("review", "check PR", ["claude", "codex"], "claude")
    bus.post(parked["id"], SYSTEM, "Review not started: repo does not contain abc.\nmore detail", kind="status")
    bus.post(parked["id"], SYSTEM, "Round 2 of 3: routine", kind="status", data={"quiet": True})
    bus.set_thread(parked["id"], status="needs_human")
    newer = bus.create_thread("discussion", "fresh", ["claude", "codex"], "claude")
    bus.create_job(newer["id"], "discussion", {}, "k")
    assert cli.main(["threads"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("1 thread needs you")
    assert out.index(parked["id"]) < out.index(newer["id"])
    note = next(ln for ln in out.splitlines() if "needs you:" in ln)
    assert "Review not started: repo does not contain abc." in note and "more detail" not in note
    assert f"agent-bus cancel {parked['id']}" in note
    import json
    assert cli.main(["threads", "--json"]) == 0
    rows = {r["id"]: r for r in json.loads(capsys.readouterr().out)}
    assert rows[parked["id"]]["needs_you"].startswith("Review not started")
    assert rows[newer["id"]]["needs_you"] is None


def test_cancel_dismisses_several_threads(env, capsys):
    bus = env["bus"]
    ids = [bus.create_thread("discussion", f"g{i}", ["claude", "codex"], "claude")["id"] for i in range(2)]
    bus.set_thread(ids[0], status="needs_human")
    assert cli.main(["cancel", *ids]) == 0
    assert all(bus.thread(i)["status"] == "cancelled" for i in ids)
    assert capsys.readouterr().out.count("cancelled") == 2


def _needs(capsys, tid):
    import json
    assert cli.main(["threads", "--json"]) == 0
    return {r["id"]: r for r in json.loads(capsys.readouterr().out)}[tid]["needs_you"]


def test_triage_reads_state_before_notices(env, capsys):
    bus = env["bus"]
    t = bus.create_thread("review", "r", ["claude", "codex"], "claude")
    bus.post(t["id"], SYSTEM, "Verdict summary first\nThis review still asks for changes ...", kind="status")
    bus.set_thread(t["id"], status="needs_human", refs='{"budget_parked": true}')
    assert _needs(capsys, t["id"]).startswith("the review still asks for changes after its follow-up budget")
    d = bus.create_thread("discussion", "d", ["claude", "codex"], "claude")
    bus.set_thread(d["id"], status="needs_human")
    bus.conn.execute("INSERT INTO auto_resumes (thread_id, agents, due, park_id, guard, created_at) "
                     "VALUES (?,?,?,?,?,?)", (d["id"], '["codex"]', 2e9, 1, "g", 0))
    assert _needs(capsys, d["id"]).startswith("waiting for codex to have usage again")


def test_triage_skips_a_later_debriefs_notice(env, capsys):
    bus = env["bus"]
    t = bus.create_thread("discussion", "d", ["claude", "codex"], "claude")
    bus.post(t["id"], SYSTEM, "No consensus (deadline reached). Needs the human user.", kind="status")
    bus.set_thread(t["id"], status="needs_human")
    bus.post(t["id"], SYSTEM, "codex gave no debrief: timed out", kind="status", data={"debrief": True})
    assert _needs(capsys, t["id"]).startswith("No consensus (deadline reached)")


def test_open_thread_whose_last_job_failed_needs_you(env, capsys):
    bus = env["bus"]
    t = bus.create_thread("review", "r", ["claude", "codex"], "claude")
    job, _ = bus.create_job(t["id"], "review", {}, "k")
    bus.conn.execute("UPDATE jobs SET status='failed', error='worker never started' WHERE id=?", (job["id"],))
    assert _needs(capsys, t["id"]).startswith("its last job failed: worker never started")


def test_parked_threads_are_listed_past_the_limit(env, capsys):
    bus = env["bus"]
    old = bus.create_thread("review", "old", ["claude", "codex"], "claude")
    bus.set_thread(old["id"], status="needs_human")
    for i in range(55):
        bus.create_thread("discussion", f"g{i}", ["claude", "codex"], "claude")
    assert _needs(capsys, old["id"]).startswith("parked for the human user")


def test_cancel_with_a_typo_cancels_none(env, capsys):
    bus = env["bus"]
    t = bus.create_thread("discussion", "g", ["claude", "codex"], "claude")
    assert cli.main(["cancel", t["id"], "t-nope"]) == 2
    assert "NO_SUCH_THREAD" in capsys.readouterr().err
    assert bus.thread(t["id"])["status"] == "open"


def test_failed_cancel_never_sweeps_its_threads(env, capsys, monkeypatch):
    from agent_bus import autoresume
    excluded = []
    monkeypatch.setattr(autoresume, "sweep_quietly", lambda bus, exclude=frozenset(): excluded.append(exclude))
    t = env["bus"].create_thread("discussion", "g", ["claude", "codex"], "claude")
    assert cli.main(["cancel", t["id"], "t-nope"]) == 2
    assert all(t["id"] in ex for ex in excluded) and excluded


def test_older_open_thread_with_a_failed_job_is_listed(env, capsys):
    bus = env["bus"]
    old = bus.create_thread("review", "old", ["claude", "codex"], "claude")
    job, _ = bus.create_job(old["id"], "review", {}, "k")
    bus.conn.execute("UPDATE jobs SET status='failed', error='boom' WHERE id=?", (job["id"],))
    for i in range(55):
        bus.create_thread("discussion", f"g{i}", ["claude", "codex"], "claude")
    assert _needs(capsys, old["id"]).startswith("its last job failed: boom")


def test_cancel_names_a_thread_once(env, capsys):
    t = env["bus"].create_thread("discussion", "g", ["claude", "codex"], "claude")
    assert cli.main(["cancel", t["id"], t["id"]]) == 0
    assert capsys.readouterr().out.count("cancelled") == 1


def test_triage_skips_an_untagged_debrief_notice_from_an_older_build(env, capsys):
    bus = env["bus"]
    t = bus.create_thread("discussion", "d", ["claude", "codex"], "claude")
    bus.post(t["id"], SYSTEM, "No consensus (4 rounds used). Needs the human user.", kind="status")
    bus.set_thread(t["id"], status="needs_human")
    job, _ = bus.create_job(t["id"], "debrief", {}, "db")
    bus.post(t["id"], SYSTEM, "codex gave no debrief: timed out", kind="status")  # no data.debrief
    bus.conn.execute("UPDATE jobs SET status='failed', updated_at=? WHERE id=?", (1e12, job["id"]))
    assert _needs(capsys, t["id"]).startswith("No consensus (4 rounds used)")
