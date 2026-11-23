"""Warnings when an agent is close to its usage limit (issue #26)."""
import asyncio
import json
import shutil
import time
from pathlib import Path

import pytest

from agent_bus import cli, mcp_server
from agent_bus import config as cfg
from agent_bus.db import HUMAN
from agent_bus.peers import StreamState, adapter, codex_rate_limits, codex_rollout
from agent_bus.service import Service
from agent_bus.worker import Worker

from test_fixtures import replay

DATA = Path(__file__).parent / "data"


def run_job(bus, tid):
    assert Worker(bus, bus.jobs(tid)[-1]["id"]).run() == 0


# --------------------------------------------------------------- what the CLIs report


def test_claude_windows_from_a_real_stream(env):
    res = replay(env, "claude", DATA / "streams" / "claude-start.jsonl").run(
        "p", str(env["repo"]), 30, lambda k, t: None)
    # Scrubbed: utilization 0.5 and resetsAt 4102444800 stand in for the account's values.
    assert [{k: v for k, v in w.items() if k != "observed_at"} for w in res.limits] == [
        {"window": "five_hour", "used": 0.5, "minutes": None, "resets_at": 4102444800.0},
        {"window": "seven_day", "used": 0.5, "minutes": None, "resets_at": 4102444800.0}]


def test_codex_windows_from_a_real_rollout_file(env, tmp_path):
    home = tmp_path / "codex"
    sid = "01a206bb-e09e-7650-9150-88aa6efbf5f4"
    day = home / "sessions" / "2026" / "11" / "23"
    day.mkdir(parents=True)
    shutil.copy(DATA / "rollouts" / "codex-token-count.jsonl", day / f"rollout-2026-11-23T04-48-25-{sid}.jsonl")
    assert codex_rollout(sid, home) == day / f"rollout-2026-11-23T04-48-25-{sid}.jsonl"
    assert codex_rollout("../../etc", home) is None and codex_rollout("feed", home) is None
    rl, seen = codex_rate_limits(codex_rollout(sid, home))
    assert rl["primary"] == {"used_percent": 50.0, "window_minutes": 10080, "resets_at": 4102444800}
    assert seen == 1795409363.004  # the last token_count's own timestamp, 2026-11-23T04:49:23.004Z
    ad = adapter("codex", cfg.Config.load())
    assert ad.usage_limits(StreamState(external_id=sid)) == [
        {"window": "primary", "used": 0.5, "minutes": 10080, "resets_at": 4102444800.0, "observed_at": seen}]
    # A resumed turn that wrote no reading (it failed first) gets none: what's in the file
    # is earlier turns'.
    st = StreamState(external_id=sid, resumed=True)
    ad.before_run(st, sid)
    assert ad.usage_limits(st) is None
    # Nor without a snapshot from before it ran (e.g. the stat failed).
    assert ad.usage_limits(StreamState(external_id=sid, resumed=True)) is None
    path = codex_rollout(sid, home)
    lines = path.read_text().splitlines(keepends=True)
    path.write_text(lines[0])
    st = StreamState(external_id=sid, resumed=True)
    ad.before_run(st, sid)
    with open(path, "a") as f:
        f.write(lines[1])  # this turn's reading
    assert ad.usage_limits(st)[0]["observed_at"] == seen
    assert ad.usage_limits(StreamState(external_id="00000000-0000-4000-8000-00000000dead")) is None


def test_codex_rollout_reading_is_tolerant(tmp_path):
    f = tmp_path / "r.jsonl"
    real = (DATA / "rollouts" / "codex-token-count.jsonl").read_text()
    # A cut first line, junk, and a later token_count without rate limits.
    f.write_text('cut"token_count" {' + "\n" + real + '{"payload": {"type": "token_count", "rate_limits": null}}\n')
    assert codex_rate_limits(f)[0]["primary"]["used_percent"] == 50.0
    f.write_text("not json\n")
    assert codex_rate_limits(f) is None


def test_a_later_record_with_no_windows_keeps_the_reading(tmp_path):
    f = tmp_path / "r.jsonl"
    real = (DATA / "rollouts" / "codex-token-count.jsonl").read_text()
    empty = {"type": "event_msg", "payload": {"type": "token_count", "rate_limits": {"primary": None, "secondary": None}}}
    f.write_text(real + json.dumps(empty) + "\n")
    assert codex_rate_limits(f, 0)[0]["primary"]["used_percent"] == 50.0


def test_claude_readings_are_stamped_when_they_arrive(env):
    from agent_bus.peers import adapter as make
    st = StreamState()
    ad = make("claude", cfg.Config.load())
    before = time.time()
    ad.parse_line({"type": "rate_limit_event", "rate_limit_info": {"unifiedWindows": {
        "five_hour": {"utilization": 0.9}}}}, st, lambda k, t: None)
    assert before <= ad.usage_limits(st)[0]["observed_at"] <= time.time()


def test_codex_rollout_offset_skips_earlier_records(tmp_path):
    f = tmp_path / "r.jsonl"
    real = (DATA / "rollouts" / "codex-token-count.jsonl").read_text().splitlines()[-1]
    f.write_text(real + "\n")
    size = f.stat().st_size
    with open(f, "a") as out:
        out.write(json.dumps({"type": "response_item", "payload": {"output": "x" * 3_000_000}}) + "\n")
    assert codex_rate_limits(f, 0)[0]["primary"]["used_percent"] == 50.0
    assert codex_rate_limits(f, size) is None


def test_an_older_reading_does_not_replace_a_newer_one(env):
    bus = env["bus"]
    now = time.time()
    bus.set_usage_limits("codex", [{"window": "primary", "used": 0.9, "resets_at": now + 60, "observed_at": now - 10}])
    bus.set_usage_limits("codex", [{"window": "primary", "used": 0.1, "resets_at": now + 60, "observed_at": now - 20}])
    assert bus.usage_limits("codex")[0]["used"] == 0.9


def test_malformed_windows_are_dropped(env):
    conf = cfg.Config.load()
    st = StreamState(rate_limit={"unifiedWindows": {
        "five_hour": {"utilization": float("nan")}, "seven_day": {"utilization": True},
        "bad name!": {"utilization": 0.9}, "ok": {"utilization": 0.9, "resetsAt": "soon"}, "x": "y"}})
    assert adapter("claude", conf).usage_limits(st) == [
        {"window": "ok", "used": 0.9, "minutes": None, "resets_at": None, "observed_at": None}]
    assert adapter("claude", conf).usage_limits(StreamState()) is None
    assert adapter("antigravity", conf).usage_limits(StreamState()) is None  # agy reports none


# ----------------------------------------------------------------------- storage


def test_readings_are_kept_until_their_window_resets(env):
    bus = env["bus"]
    now = time.time()
    bus.set_usage_limits("claude", [{"window": "five_hour", "used": 0.9, "resets_at": now + 60},
                                    {"window": "seven_day", "used": 0.2, "resets_at": now - 1},
                                    {"window": "other", "used": 0.95}])
    assert [w["window"] for w in bus.usage_limits("claude")] == ["five_hour", "other"]
    bus.conn.execute("UPDATE usage_limits SET observed_at=observed_at-?", (bus.LIMIT_FRESH_S + 1,))
    assert [w["window"] for w in bus.usage_limits()] == ["five_hour"]  # no reset time: stale now


def test_usage_warn_at_is_validated(env):
    path = cfg.config_path()
    conf = json.loads(path.read_text())
    for bad in (0, 1.5, "high", True):
        path.write_text(json.dumps({**conf, "usage_warn_at": bad}))
        with pytest.raises(ValueError, match="usage_warn_at"):
            cfg.Config.load()


# ------------------------------------------------------------------- warnings


def set_warn(env, value):
    path = cfg.config_path()
    path.write_text(json.dumps({**json.loads(path.read_text()), "usage_warn_at": value}))


def test_turns_record_windows_and_warn_at_turn_start(env, no_spawn, monkeypatch):
    bus = env["bus"]
    monkeypatch.setenv("FAKE_CLAUDE_USED", "0.85")
    monkeypatch.setenv("FAKE_CODEX_USED", "40")
    tid = Service(bus, HUMAN).start_discussion("Which DB?", peers=["claude", "codex"])["thread"]["id"]
    run_job(bus, tid)
    by = {(w["agent"], w["window"]): w["used"] for w in bus.usage_limits()}
    assert by == {("claude", "five_hour"): 0.85, ("claude", "seven_day"): 0.25, ("codex", "primary"): 0.4}
    starts = {}
    for e in bus.events(tid):
        if e["kind"] == "turn_start":
            starts.setdefault(e["agent"], []).append(e["text"])
    # The first turn had no reading yet; later ones warn about claude only.
    assert "near its usage limit" not in starts["claude"][0]
    assert any("near its usage limit: five_hour 85% used, resets" in t for t in starts["claude"][1:])
    assert not any("near its usage limit" in t for t in starts["codex"])


def test_agents_and_whoami_show_near_limit(env, capsys):
    bus = env["bus"]
    now = time.time()
    bus.set_usage_limits("codex", [{"window": "primary", "used": 0.92, "minutes": 10080, "resets_at": now + 3600}])
    bus.set_usage_limits("claude", [{"window": "five_hour", "used": 0.3, "resets_at": now + 3600}])
    assert cli.main(["agents"]) == 0
    out = capsys.readouterr().out
    assert "usage limits (NEAR THE LIMIT): primary (7d) 92% used, resets" in out
    assert "usage limits: five_hour 30% used" in out
    assert cli.main(["agents", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["codex"]["near_limit"] is True and data["claude"]["near_limit"] is False
    assert data["codex"]["usage_limits"][0]["minutes"] == 10080
    assert "usage_limits" not in data["antigravity"]
    set_warn(env, 0.25)
    assert cli.main(["agents", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["claude"]["near_limit"] is True

    set_warn(env, 0.8)
    server = mcp_server.build("claude", bus=bus)
    me = json.loads(asyncio.run(server.call_tool("whoami", {})).content[0].text)
    assert [(w["agent"], w["window"]) for w in me["near_limit"]] == [("codex", "primary")]
