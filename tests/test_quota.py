"""Agents running out of usage (issue #26, agreed on agent-bus thread t-3e260b, P4)."""
import contextlib
import json
import time

import pytest

from agent_bus import config as cfg
from agent_bus.db import HUMAN, SYSTEM, BusError
from agent_bus.peers import StreamState, adapter
from agent_bus.service import Service
from agent_bus.worker import Worker

HEAD = "b" * 40
BASE = "a" * 40


def run_job(bus, tid):
    job = bus.jobs(tid)[-1]
    assert Worker(bus, job["id"]).run() == 0
    return bus.job(job["id"])


def calls(env, agent=None):
    if not env["log"].exists():
        return []
    out = [json.loads(line) for line in env["log"].read_text().splitlines()]
    return [c for c in out if agent is None or c["agent"] == agent]


def turn(env, agent):
    ad = adapter(agent, cfg.Config.load())
    return ad.run("hello", str(env["repo"]), 30, lambda k, t: None)


# ------------------------------------------------------------------ detection


def test_usage_is_parsed_from_each_cli(env):
    assert turn(env, "claude").usage == {"input": 10, "output": 5, "cache_read": 100, "thinking": 3}
    assert turn(env, "codex").usage == {"input": 20, "output": 6, "cache_read": 200, "thinking": 4}
    assert turn(env, "antigravity").usage == {"input": 30, "output": 7, "cache_read": 0, "thinking": 5}


def test_out_of_usage_is_classified_per_cli(env, monkeypatch):
    monkeypatch.setenv("FAKE_EXHAUSTED", "claude,codex,antigravity")
    before = time.time()
    claude = turn(env, "claude")
    assert not claude.ok and claude.quota["source"] == "structured"
    assert claude.quota["resets_at"] == 4102444800.0
    codex = turn(env, "codex")
    assert codex.quota["source"] == "heuristic"
    assert before + 7500 <= codex.quota["resets_at"] <= time.time() + 7500
    agy = turn(env, "antigravity")  # the real agy 1.5.13 wording, "Resets in 2h26m14s"
    assert agy.quota["source"] == "heuristic"
    assert before + 8774 <= agy.quota["resets_at"] <= time.time() + 8774


def test_successful_turns_and_generic_errors_are_not_usage(env):
    conf = cfg.Config.load()
    # A bare 429, an overload, or RESOURCE_EXHAUSTED without quota wording stays an ordinary error.
    for agent, err in (("codex", "stream error: 429 Too Many Requests"),
                       ("claude", "API Error: 529 Overloaded"),
                       ("antigravity", "status ERROR: RESOURCE_EXHAUSTED"),
                       ("claude", "Prompt is too long")):
        assert adapter(agent, conf).classify(StreamState(), err) is None
    # Claude's structured status counts only when it rejected the request without overage.
    st = StreamState(rate_limit={"status": "rejected", "isUsingOverage": True, "resetsAt": 1})
    assert adapter("claude", conf).classify(st, "error") is None
    assert turn(env, "claude").quota is None  # ok turn with status "allowed"


# ---------------------------------------------------------------------- cache


def test_blocks_expire_and_are_scoped(env):
    bus = env["bus"]
    now = time.time()
    bus.set_quota_block("antigravity", now + 600, "heuristic", "quota", scope="gemini-x")
    assert bus.quota_block("antigravity", "gemini-x")
    assert bus.quota_block("antigravity", "other-model") is None
    assert bus.quota_block("antigravity") is None
    bus.set_quota_block("codex", now - 1, "heuristic", "old")
    assert bus.quota_block("codex") is None
    assert [b["agent"] for b in bus.quota_blocks()] == ["antigravity"]  # expired one pruned
    # Newer evidence for the same scope replaces the old.
    bus.set_quota_block("antigravity", now + 60, "structured", "newer", scope="gemini-x")
    assert bus.quota_block("antigravity", "gemini-x")["reason"] == "newer"
    assert bus.clear_quota("antigravity", "gemini-x") == 1


# --------------------------------------------------------------------- review


def test_exhausted_reviewer_needs_human_without_retries(env, no_spawn, monkeypatch):
    bus = env["bus"]
    monkeypatch.setenv("FAKE_EXHAUSTED", "codex")
    me = Service(bus, "claude", bus.register_session("claude", env={}))
    monkeypatch.setattr("agent_bus.worker.Worker.review_diff", lambda self, refs, interdiff: ("diff", "Diff"))
    tid = me.request_review("codex", str(env["repo"]), BASE, HEAD)["thread"]["id"]
    run_job(bus, tid)
    t = bus.thread(tid)
    assert t["status"] == "needs_human"
    assert len(calls(env, "codex")) == 1  # no fresh session, no JSON repair turn
    assert bus.delivered(tid, "codex") == 0  # nothing was delivered
    msgs = bus.history(tid)["messages"]
    notices = [m for m in msgs if m["sender"] == SYSTEM and "out of usage" in m["body"]]
    assert len(notices) == 1 and f"agent-bus resume {tid}" in notices[0]["body"]
    assert not [m for m in msgs if m["sender"] == "codex"]  # no partial verdict
    block = bus.quota_block("codex")
    assert block and not block["estimated"] and block["source"] == "heuristic"

    # The next request fails fast: no thread, no job.
    jobs = len(no_spawn)
    with pytest.raises(BusError) as e:
        me.request_review("codex", str(env["repo"]), BASE, "c" * 40)
    assert e.value.code == "USAGE_EXHAUSTED" and "until about" in e.value.message
    assert len(no_spawn) == jobs

    # The human's resume clears the cached block and retries.
    res = Service(bus, HUMAN).resume(tid)
    assert "job" in res and bus.quota_block("codex") is None
    assert "Cleared out-of-usage state for codex" in bus.history(tid)["messages"][-1]["body"]


def test_reset_far_in_the_future_is_clamped(env, no_spawn, monkeypatch):
    bus = env["bus"]
    monkeypatch.setenv("FAKE_EXHAUSTED", "claude")
    monkeypatch.setattr("agent_bus.worker.Worker.review_diff", lambda self, refs, interdiff: ("diff", "Diff"))
    me = Service(bus, "codex", bus.register_session("codex", env={}))
    tid = me.request_review("claude", str(env["repo"]), BASE, HEAD)["thread"]["id"]
    run_job(bus, tid)
    block = bus.quota_block("claude")
    assert block["source"] == "structured"
    assert block["until"] <= time.time() + 7 * 86400  # Claude said year 2100


# ----------------------------------------------------------------- discussion


def test_default_roster_skips_blocked_peers_and_explicit_ones_fail(env, no_spawn):
    bus = env["bus"]
    bus.set_quota_block("codex", time.time() + 600, "structured", "Claude five_hour limit reached")
    human = Service(bus, HUMAN)
    res = human.start_discussion("Which DB?")
    assert set(res["skipped_peers"]) == {"codex"}
    assert res["thread"]["participants"] == ["claude", "antigravity"]
    opening = [m for m in bus.history(res["thread"]["id"])["messages"] if m["sender"] == SYSTEM]
    assert "Left out: peer codex" in opening[0]["body"]
    with pytest.raises(BusError) as e:
        human.start_discussion("Which DB?", peers=["claude", "codex"])
    assert e.value.code == "USAGE_EXHAUSTED"
    for a in ("claude", "antigravity"):
        bus.set_quota_block(a, time.time() + 600, "heuristic", "quota", estimated=True)
    with pytest.raises(BusError) as e:
        human.start_discussion("Which DB?")
    assert e.value.code == "USAGE_EXHAUSTED" and "an estimate" in e.value.message


def test_peer_out_of_usage_from_the_start_ends_needs_human_after_round_one(env, no_spawn, monkeypatch):
    bus = env["bus"]
    monkeypatch.setenv("FAKE_EXHAUSTED", "codex")
    tid = Service(bus, HUMAN).start_discussion("Which DB?", peers=["claude", "codex"])["thread"]["id"]
    run_job(bus, tid)
    t = bus.thread(tid)
    assert t["status"] == "needs_human" and t["round"] == 1  # no rounds spent chasing it
    assert len(calls(env, "codex")) == 1
    assert t["participants"] == ["claude", "codex"]  # never silently dropped
    body = bus.history(tid)["messages"][-1]["body"]
    assert "No consensus is possible" in body
    assert not bus.quota_block("codex")["estimated"]  # "try again in 2 hours 5 minutes"


def test_peer_running_out_later_keeps_its_votes_and_is_not_rerun(env, no_spawn, monkeypatch):
    bus = env["bus"]
    monkeypatch.setenv("FAKE_EXHAUSTED_LATER", "codex")
    tid = Service(bus, HUMAN).start_discussion("Which DB?", peers=["claude", "codex", "antigravity"],
                                               max_rounds=4)["thread"]["id"]
    run_job(bus, tid)
    t = bus.thread(tid)
    codex_turns = calls(env, "codex")
    # Round 1 plus the one turn that hit the limit; later rounds don't launch it again,
    # and the failed resumed turn didn't fall back to a fresh session.
    assert len(codex_turns) == 2
    # Codex already backs its own proposal, so the others can still converge on it.
    standing = bus.standings(tid)["codex"]
    assert standing["stance"] == "agree"
    assert t["round"] >= 2
    if t["status"] == "resolved":
        assert t["final_proposal"] == standing["proposal_id"]
    else:
        assert t["status"] == "needs_human"
    notices = [m for m in bus.history(tid)["messages"] if m["sender"] == SYSTEM and "out of usage" in m["body"]]
    assert len(notices) == 1


def test_successful_turn_clears_a_block_and_turn_end_reports_tokens(env, no_spawn, monkeypatch):
    bus = env["bus"]
    tid = Service(bus, HUMAN).start_discussion("Which DB?", peers=["claude"])["thread"]["id"]
    # A block another worker recorded while this turn was already running: the
    # successful turn is newer evidence, so it lifts the block.
    real = bus.quota_block
    monkeypatch.setattr(bus, "quota_block", lambda agent, model=None: None if agent == "claude" else real(agent, model))
    bus.set_quota_block("claude", time.time() + 600, "heuristic", "x", estimated=True)
    run_job(bus, tid)
    assert bus.thread(tid)["status"] == "resolved"
    assert not bus._all("SELECT * FROM quota_blocks WHERE agent='claude'")
    ends = bus._all("SELECT * FROM events WHERE thread_id=? AND kind='turn_end'", tid)
    assert ends and "tokens in 10 out 5 cache 100 thinking 3" in ends[0]["text"]


# ------------------------------------------------- review fixes (t-cf74ff pass 1)


def test_running_out_during_a_repair_turn_posts_nothing(env, no_spawn, monkeypatch):
    bus = env["bus"]
    monkeypatch.setenv("FAKE_BEHAVIOUR", "nojson")
    monkeypatch.setenv("FAKE_EXHAUSTED_REPAIR", "codex")
    monkeypatch.setattr("agent_bus.worker.Worker.review_diff", lambda self, refs, interdiff: ("diff", "Diff"))
    me = Service(bus, "claude", bus.register_session("claude", env={}))
    tid = me.request_review("codex", str(env["repo"]), BASE, HEAD)["thread"]["id"]
    run_job(bus, tid)
    assert bus.thread(tid)["status"] == "needs_human"
    msgs = bus.history(tid)["messages"]
    assert not [m for m in msgs if m["sender"] == "codex"]
    assert not [m for m in msgs if "did not return a usable verdict" in m["body"]]
    assert bus.quota_block("codex")

    # The same in a discussion: no proposal, and not the malformed first reply either.
    bus.clear_quota("codex")
    tid = Service(bus, HUMAN).start_discussion("Which DB?", peers=["claude", "codex"])["thread"]["id"]
    run_job(bus, tid)
    assert not [m for m in bus.history(tid)["messages"] if m["sender"] == "codex"]


def test_quota_failure_keeps_the_pinned_session(env, no_spawn, monkeypatch):
    bus = env["bus"]
    monkeypatch.setenv("FAKE_EXHAUSTED_LATER", "codex")
    tid = Service(bus, HUMAN).start_discussion("Which DB?", peers=["claude", "codex"])["thread"]["id"]
    run_job(bus, tid)
    first = [c for c in calls(env, "codex")]
    assert len(first) == 2
    resumed_with = first[1]["argv"][-2]  # codex exec resume <id> <prompt>
    assert bus.peer_session(tid, "codex") == resumed_with  # not cleared, not replaced


def test_model_from_extra_args_and_default_model_scope(env, no_spawn, monkeypatch):
    bus = env["bus"]
    conf_path = env["tmp"] / "config" / "agent-bus" / "config.json"
    conf = json.loads(conf_path.read_text())
    conf["peers"]["antigravity"]["args"] = ["--model", "gemini-x"]
    conf_path.write_text(json.dumps(conf))
    from agent_bus import quota
    c = cfg.Config.load()
    assert quota.effective_model(c, "antigravity", {}) == "gemini-x"
    assert quota.effective_model(c, "antigravity", {"model": "other"}) == "other"
    # A block on the CLI's default model doesn't stop an explicitly chosen one.
    bus.set_quota_block("antigravity", time.time() + 600, "heuristic", "q", scope=quota.DEFAULT_MODEL)
    assert bus.quota_block("antigravity", quota.model_key(None))
    assert bus.quota_block("antigravity", quota.model_key("gemini-x")) is None
    human = Service(bus, HUMAN)
    human.start_discussion("q", peers=["antigravity"])  # runs gemini-x via args: allowed
    bus.set_quota_block("antigravity", time.time() + 600, "heuristic", "q", scope="gemini-x")
    with pytest.raises(BusError):
        human.start_discussion("q", peers=["antigravity"])


def test_review_followup_is_checked_against_its_inherited_model(env, no_spawn, monkeypatch):
    bus = env["bus"]
    monkeypatch.setattr("agent_bus.worker.Worker.review_diff", lambda self, refs, interdiff: ("diff", "Diff"))
    me = Service(bus, "claude", bus.register_session("claude", env={}))
    tid = me.request_review("antigravity", str(env["repo"]), BASE, HEAD, model="gemini-x")["thread"]["id"]
    run_job(bus, tid)
    bus.set_quota_block("antigravity", time.time() + 600, "heuristic", "q", scope="gemini-x")
    with pytest.raises(BusError) as e:  # the follow-up would run gemini-x again
        me.request_review("antigravity", str(env["repo"]), BASE, "c" * 40, prev_head_commit_id=HEAD)
    assert e.value.code == "USAGE_EXHAUSTED"
    # Nothing about the thread moved.
    assert bus.thread(tid)["refs"]["head_commit_id"] == HEAD


def test_advance_refuses_only_a_stalemate(env, no_spawn, monkeypatch):
    bus = env["bus"]
    me = Service(bus, "claude", bus.register_session("claude", env={}))
    tid = me.start_discussion("q", peers=["codex", "antigravity"], max_rounds=4)["thread"]["id"]
    bus.set_thread(tid, round=1, blind=0)
    bus.post(tid, "codex", "codex plan", kind="proposal")
    bus.set_quota_block("codex", time.time() + 600, "heuristic", "q")
    me.advance_discussion(tid)  # codex backs its own P1, which the others could still adopt
    bus.post(tid, "antigravity", "agy plan", kind="proposal")
    bus.set_quota_block("antigravity", time.time() + 600, "heuristic", "q")
    for j in bus.jobs(tid):
        bus.conn.execute("UPDATE jobs SET status='done' WHERE id=?", (j["id"],))
    with pytest.raises(BusError) as e:  # two blocked peers backing different proposals
        me.advance_discussion(tid)
    assert e.value.code == "USAGE_EXHAUSTED"


def test_notices_use_fixed_wording(env, no_spawn, monkeypatch):
    bus = env["bus"]
    monkeypatch.setenv("FAKE_EXHAUSTED", "codex")
    monkeypatch.setattr("agent_bus.worker.Worker.review_diff", lambda self, refs, interdiff: ("diff", "Diff"))
    me = Service(bus, "claude", bus.register_session("claude", env={}))
    tid = me.request_review("codex", str(env["repo"]), BASE, HEAD)["thread"]["id"]
    run_job(bus, tid)
    system = " ".join(m["body"] for m in bus.history(tid)["messages"] if m["sender"] == SYSTEM)
    assert "Upgrade to Pro" not in system and "codex's CLI reported a usage limit" not in system
    assert "codex is out of usage until about" in system
    assert bus.quota_block("codex")["reason"] == "codex's CLI reported a usage limit in its error"
    # The raw CLI error is still in the progress events.
    ends = bus._all("SELECT text FROM events WHERE thread_id=? AND kind='turn_end'", tid)
    assert any("Upgrade to Pro" in e["text"] for e in ends)


# ---------------------------------------------- review fixes (t-ef88cb pass 1)


def test_reset_from_text_units():
    from agent_bus.peers import _reset_from_text
    assert _reset_from_text("try again in 500ms", now=0) == 0.5  # not 500 minutes
    assert _reset_from_text("try again in 2 hours, and 5 minutes", now=0) == 7500
    assert _reset_from_text("try again in 1 day 2 hrs", now=0) == 93600
    assert _reset_from_text("try again in 3 fortnights", now=0) is None
    assert _reset_from_text("try again at 3:05 PM", now=0) is None
    assert _reset_from_text("Claude AI usage limit reached|1736280000") == 1736280000


def stance(bus, tid, agent, kind, on=None, stance="agree"):
    if kind == "proposal":
        bus.post(tid, agent, f"{agent} plan", kind="proposal")
    else:
        bus.post(tid, agent, "v", kind="vote", data={"on": on, "stance": stance, "reasons": "r"})


@pytest.mark.parametrize("setup,expected", [
    ([("codex", "proposal")], False),                         # backs its own P1: others can join
    ([("claude", "proposal"), ("codex", "vote", "P1", "object")], True),  # backs nothing
    ([], True),                                               # never posted
])
def test_stalemate_rule_is_deterministic(env, no_spawn, setup, expected):
    from agent_bus import unvotable
    bus = env["bus"]
    tid = Service(bus, HUMAN).start_discussion("q", peers=["claude", "codex", "antigravity"])["thread"]["id"]
    bus.set_thread(tid, round=2, blind=0)
    for s in setup:
        stance(bus, tid, *s)
    bus.set_quota_block("codex", time.time() + 600, "heuristic", "q")
    assert bool(unvotable.stalemate(bus, cfg.Config.load(), bus.thread(tid))) is expected


def test_two_blocked_peers_backing_different_proposals_is_a_stalemate(env, no_spawn):
    from agent_bus import unvotable
    bus = env["bus"]
    tid = Service(bus, HUMAN).start_discussion("q", peers=["claude", "codex", "antigravity"])["thread"]["id"]
    bus.set_thread(tid, round=2, blind=0)
    stance(bus, tid, "codex", "proposal")
    stance(bus, tid, "antigravity", "proposal")
    for a in ("codex", "antigravity"):
        bus.set_quota_block(a, time.time() + 600, "heuristic", "q")
    assert unvotable.stalemate(bus, cfg.Config.load(), bus.thread(tid))


def test_all_peers_blocked_on_one_proposal_hands_over_without_spinning(env, no_spawn, monkeypatch):
    bus = env["bus"]
    sid = bus.register_session("claude", env={})
    me = Service(bus, "claude", sid)
    tid = me.start_discussion("q", peers=["codex"], max_rounds=4)["thread"]["id"]
    me.send("my own plan", thread=tid, kind="proposal")  # P1: a competing initiator position
    bus.conn.execute("UPDATE sessions SET last_seen=0 WHERE id=?", (sid,))  # then absent: no grace waits
    monkeypatch.setenv("FAKE_EXHAUSTED_LATER", "codex")
    run_job(bus, tid)  # round 1: codex proposes P2; round 2: codex runs out of usage
    t = bus.thread(tid)
    st = bus.standings(tid)["codex"]
    assert (st["proposal_id"], st["stance"]) == ("P2", "agree")
    assert bus.quota_block("codex")  # the quota path really ran, in round 2
    assert t["status"] == "open" and t["round"] == 2  # no round 3 for a peer that can't move
    assert len(calls(env, "codex")) == 2
    last = bus.history(tid)["messages"][-1]
    assert last["recipient"] == "claude" and "All peers agree on P2" in last["body"]


def advance_setup(bus, peers=("codex",)):
    """An agent-initiated discussion after round 1: each peer backs its own proposal and
    the initiator backs a competing one."""
    me = Service(bus, "claude", bus.register_session("claude", env={}))
    tid = me.start_discussion("q", peers=list(peers), max_rounds=4)["thread"]["id"]
    for j in bus.jobs(tid):
        bus.conn.execute("UPDATE jobs SET status='done' WHERE id=?", (j["id"],))
    bus.set_thread(tid, round=1, blind=0)
    return me, tid


def test_advance_is_refused_when_every_peer_is_blocked(env, no_spawn):
    bus = env["bus"]
    me, tid = advance_setup(bus)
    bus.post(tid, "codex", "codex plan", kind="proposal")  # P1
    me.send("claude plan", thread=tid, kind="proposal")  # P2
    bus.set_quota_block("codex", time.time() + 600, "heuristic", "q")
    jobs = len(bus.jobs(tid))
    with pytest.raises(BusError) as e:
        me.advance_discussion(tid)
    assert e.value.code == "USAGE_EXHAUSTED"
    assert "no peer can take another round" in e.value.message and "Every peer backs P1" in e.value.message
    assert len(bus.jobs(tid)) == jobs and bus.thread(tid)["round"] == 1  # nothing queued, no round spent
    with pytest.raises(BusError) as e:  # the human is told who can settle it
        Service(bus, HUMAN).advance_discussion(tid)
    assert "claude (initiator) can settle it" in e.value.message
    me.send("ok", thread=tid, kind="vote", on="P1", stance="agree")  # the initiator can still settle it
    assert bus.thread(tid)["status"] == "resolved"


def test_block_after_advance_is_queued_spends_no_round(env, no_spawn):
    bus = env["bus"]
    me, tid = advance_setup(bus)
    bus.post(tid, "codex", "codex plan", kind="proposal")  # P1
    me.send("claude plan", thread=tid, kind="proposal")  # P2
    me.advance_discussion(tid)
    bus.set_quota_block("codex", time.time() + 600, "heuristic", "q")  # arrives after admission
    run_job(bus, tid)
    t = bus.thread(tid)
    assert t["status"] == "open" and t["round"] == 1
    assert calls(env, "codex") == []
    bodies = [m["body"] for m in bus.history(tid)["messages"] if m["sender"] == SYSTEM]
    assert any("codex is out of usage" in b for b in bodies)
    assert "All peers agree on P1. Waiting for claude" in bodies[-1]
    # Once codex has usage again, the same advance runs: the first one spent no round.
    bus.clear_quota("codex")
    assert not me.advance_discussion(tid)["job"].get("deduplicated")
    run_job(bus, tid)
    assert bus.thread(tid)["round"] == 2 and len(calls(env, "codex")) == 1


def test_block_between_the_check_and_the_round_spends_no_round(env, no_spawn, monkeypatch):
    from agent_bus import unvotable
    bus = env["bus"]
    me, tid = advance_setup(bus)
    bus.post(tid, "codex", "codex plan", kind="proposal")  # P1
    me.send("claude plan", thread=tid, kind="proposal")  # P2
    me.advance_discussion(tid)
    real = unvotable.assess

    def assess(*a):  # another worker records a block right after the first check
        s = real(*a)
        bus.set_quota_block("codex", time.time() + 600, "heuristic", "q")
        return s
    monkeypatch.setattr(unvotable, "assess", assess)
    run_job(bus, tid)
    assert bus.thread(tid)["round"] == 1 and calls(env, "codex") == []


def test_whoami_and_clear_quota_cli(env, no_spawn, capsys):
    import asyncio
    from agent_bus import cli, mcp_server
    bus = env["bus"]
    bus.set_quota_block("codex", time.time() + 600, "structured", "codex's CLI reported its usage limit was reached")
    server = mcp_server.build("claude", bus=bus)
    payload = json.loads(asyncio.run(server.call_tool("whoami", {})).content[0].text)
    assert [b["agent"] for b in payload["out_of_usage"]] == ["codex"]
    assert cli.main(["agents"]) == 0
    assert "out of usage until" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(["agents", "--clear-quota", "nobody"])  # argparse rejects it, no NameError
    capsys.readouterr()
    assert cli.main(["agents", "--clear-quota", "codex"]) == 0
    assert "cleared 1" in capsys.readouterr().out
    assert bus.quota_block("codex") is None


def test_ask_prints_skipped_peers(env, no_spawn, capsys):
    from agent_bus import cli
    env["bus"].set_quota_block("codex", time.time() + 600, "heuristic", "q")
    assert cli.main(["ask", "Q?"]) == 0
    assert "left out codex: codex is out of usage" in capsys.readouterr().out


# ------------------------------------------------- review fixes (t-cf74ff pass 2)


def test_zero_cooldown_still_notices_and_needs_human(env, no_spawn, monkeypatch):
    bus = env["bus"]
    conf_path = env["tmp"] / "config" / "agent-bus" / "config.json"
    conf = json.loads(conf_path.read_text())
    conf["quota_cooldown_s"] = 0
    conf_path.write_text(json.dumps(conf))
    monkeypatch.setenv("FAKE_EXHAUSTED", "codex")
    monkeypatch.setattr("agent_bus.peers._reset_from_text", lambda text, now=None: None)  # no reset: 0s estimate
    monkeypatch.setattr("agent_bus.worker.Worker.review_diff", lambda self, refs, interdiff: ("diff", "Diff"))
    me = Service(bus, "claude", bus.register_session("claude", env={}))
    tid = me.request_review("codex", str(env["repo"]), BASE, HEAD)["thread"]["id"]
    run_job(bus, tid)
    assert bus.thread(tid)["status"] == "needs_human"
    assert any("codex is out of usage" in m["body"] for m in bus.history(tid)["messages"])


def test_model_flags_match_what_each_adapter_strips(env):
    from agent_bus import quota
    conf_path = env["tmp"] / "config" / "agent-bus" / "config.json"
    conf = json.loads(conf_path.read_text())
    conf["peers"]["codex"]["args"] = ["-m", "gpt-a", "-mgpt-b"]
    conf["peers"]["antigravity"]["args"] = ["-m", "not-a-model-flag"]
    conf_path.write_text(json.dumps(conf))
    c = cfg.Config.load()
    assert quota.effective_model(c, "codex", {}) == "gpt-b"  # the last one wins, like the CLI
    assert quota.effective_model(c, "antigravity", {}) is None  # agy has no -m


def test_stalemate_from_another_jobs_block_names_the_peer(env, no_spawn):
    bus = env["bus"]
    tid = Service(bus, HUMAN).start_discussion("q", peers=["claude", "codex"])["thread"]["id"]
    bus.set_quota_block("codex", time.time() + 600, "structured", "x")  # recorded elsewhere
    run_job(bus, tid)
    t = bus.thread(tid)
    assert t["status"] == "needs_human"
    assert len(calls(env, "codex")) == 0
    notices = [m["body"] for m in bus.history(tid)["messages"] if "codex is out of usage" in m["body"]]
    assert len(notices) == 1 and f"agent-bus resume {tid}" in notices[0]


def test_stalemate_cleared_before_closing_leaves_the_discussion_open(env, no_spawn, monkeypatch):
    from agent_bus import unvotable
    bus = env["bus"]
    tid = Service(bus, HUMAN).start_discussion("q", peers=["claude", "codex"])["thread"]["id"]
    w = Worker(bus, bus.jobs(tid)[-1]["id"])
    monkeypatch.setattr(w, "fenced", contextlib.nullcontext)  # no lease needed for this check
    # The quick check sees a stalemate; by the time the transaction re-reads, the block is gone.
    real = unvotable.stalemate
    quick = iter([{"codex": {}}])
    monkeypatch.setattr(unvotable, "stalemate", lambda *a: next(quick, None) or real(*a))
    assert w.close_on_stalemate() is False
    assert bus.thread(tid)["status"] == "open"
