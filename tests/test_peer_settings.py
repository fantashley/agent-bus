"""#10: per-agent model and reasoning effort for headless peers."""
import json

import pytest

from agent_bus import config as cfg
from agent_bus.db import HUMAN
from agent_bus.peers import adapter
from agent_bus.service import Service
from agent_bus.worker import Worker


def set_peers(env, **settings):
    path = env["tmp"] / "config" / "agent-bus" / "config.json"
    data = json.loads(path.read_text())
    for agent, s in settings.items():
        data["peers"][agent].update(s)
    path.write_text(json.dumps(data))


def calls(env):
    return [json.loads(l) for l in env["log"].read_text().splitlines()] if env["log"].exists() else []


def run_job(bus, tid):
    Worker(bus, bus.jobs(tid)[-1]["id"]).run()


def test_each_cli_gets_its_own_flags(env):
    conf = cfg.Config.load()
    s = {"model": "m-1", "effort": "high"}
    assert adapter("claude", conf, s).setting_args() == ["--model", "m-1", "--effort", "high"]
    assert adapter("codex", conf, s).setting_args() == ["-m", "m-1", "-c", 'model_reasoning_effort="high"']
    assert adapter("antigravity", conf, s).setting_args() == ["--model", "m-1", "--effort", "high"]
    assert adapter("codex", conf).setting_args() == []


def test_config_settings_reach_first_and_resumed_turns(env, no_spawn, monkeypatch):
    set_peers(env, codex={"effort": "xhigh"}, claude={"model": "sonnet"})
    # Round 1 runs in parallel, so codex's proposal may be P1; claude agreeing with it
    # first in round 2 would then end the thread before codex's resumed turn (#58).
    monkeypatch.setenv("FAKE_BEHAVIOUR", "stubborn")
    bus = env["bus"]
    tid = Service(bus, HUMAN).start_discussion("Q?", peers=["claude", "codex"], max_rounds=2)["thread"]["id"]
    run_job(bus, tid)
    cs = calls(env)
    codex = [c["argv"] for c in cs if c["agent"] == "codex"]
    claude = [c["argv"] for c in cs if c["agent"] == "claude"]
    assert codex and all('model_reasoning_effort="xhigh"' in a for a in codex)
    assert any("resume" in a for a in codex)  # resumed turns carry the settings too
    assert claude and all("--model" in a and "sonnet" in a and "--effort" not in a for a in claude)
    starts = [e["text"] for e in bus.events(tid) if e["kind"] == "turn_start" and e["agent"] == "codex"]
    assert starts and all("effort xhigh" in t for t in starts)


def test_discussion_override_is_per_thread(env, no_spawn):
    set_peers(env, codex={"effort": "low"})
    bus = env["bus"]
    svc = Service(bus, HUMAN)
    tid = svc.start_discussion("Q?", peers=["claude", "codex"], max_rounds=1,
                               peer_settings={"codex": {"effort": "high", "model": "gpt-x"}})["thread"]["id"]
    run_job(bus, tid)
    argv = [c["argv"] for c in calls(env) if c["agent"] == "codex"][0]
    assert 'model_reasoning_effort="high"' in argv and argv[argv.index("-m") + 1] == "gpt-x"


def test_review_override_kept_for_followups(env, no_spawn, monkeypatch):
    bus = env["bus"]
    claude = Service(bus, "claude", bus.register_session("claude"))
    monkeypatch.setattr(Worker, "review_diff", lambda self, refs, interdiff: ("diff --git a/x b/x\n", "Diff:"))
    r1 = claude.request_review("codex", str(env["repo"]), "a" * 40, "b" * 40, effort="high")
    tid = r1["thread"]["id"]
    assert bus.thread(tid)["refs"]["peer_settings"] == {"codex": {"effort": "high"}}
    run_job(bus, tid)
    claude.request_review("codex", str(env["repo"]), "a" * 40, "c" * 40, prev_head_commit_id="b" * 40)
    assert bus.thread(tid)["refs"]["peer_settings"] == {"codex": {"effort": "high"}}
    run_job(bus, tid)
    claude.request_review("codex", str(env["repo"]), "a" * 40, "d" * 40, prev_head_commit_id="c" * 40,
                          model="gpt-y")
    assert bus.thread(tid)["refs"]["peer_settings"] == {"codex": {"effort": "high", "model": "gpt-y"}}


@pytest.mark.parametrize("agent,settings", [
    ("codex", {"effort": "max"}),            # not a Codex value
    ("claude", {"effort": "minimal"}),       # not a Claude value
    ("claude", {"model": "x; rm -rf /"}),    # unsafe model name
    ("claude", {"temperature": 1}),          # unknown key
])
def test_bad_settings_are_refused(env, no_spawn, agent, settings):
    with pytest.raises(ValueError):
        cfg.check_peer_settings(agent, settings)
    with pytest.raises(ValueError):
        Service(env["bus"], HUMAN).start_discussion("Q?", peers=["claude", "codex"],
                                                    peer_settings={agent: settings})


def test_bad_config_value_fails_on_load(env):
    set_peers(env, codex={"effort": "max"})
    with pytest.raises(ValueError, match="effort"):
        cfg.Config.load()


def test_override_for_a_non_peer_is_refused(env, no_spawn):
    with pytest.raises(ValueError, match="isn't a peer"):
        Service(env["bus"], HUMAN).start_discussion("Q?", peers=["claude"], peer_settings={"codex": {}})


def test_ask_set_flag(env, no_spawn, capsys):
    from agent_bus import cli
    assert cli.main(["ask", "Q?", "--with", "claude,codex", "--set", "codex.effort=high"]) == 0
    tid = capsys.readouterr().out.split()[1]
    assert env["bus"].thread(tid)["refs"]["peer_settings"] == {"codex": {"effort": "high"}}
    assert cli.main(["ask", "Q?", "--set", "codex.speed=fast"]) == 2
    assert "--set expects" in capsys.readouterr().err
    assert cli.main(["ask", "Q?", "--with", "codex", "--set", "codex.effort=max"]) == 2
    assert "effort" in capsys.readouterr().err


def test_settings_strip_conflicting_peer_args(env):
    set_peers(
        env,
        claude={"args": ["--model", "old", "--model=old2", "--effort", "low", "--effort=low2", "--keep", "1"]},
        antigravity={"args": ["--model", "old", "--effort=low", "--keep", "1"]},
        codex={"args": [
            "-m", "old1", "-m=old2", "-mold3", "--model", "old4", "--model=old5",
            "-c", 'model="old6"', "--config", "model=old7", "--config=model=old8", "-c=model=old9",
            "-c", 'model_reasoning_effort="low"', "--config", "model_reasoning_effort=low",
            "--config=model_reasoning_effort=low", "-c=model_reasoning_effort=low",
            "-c", "other=1", "--profile", "p", "--", "--model", "positional",
        ]},
    )
    conf = cfg.Config.load()

    # Only model set: effort flags in args stay; model flags are stripped.
    cl_m = adapter("claude", conf, {"model": "opus"})
    assert cl_m.extra_args() == ["--effort", "low", "--effort=low2", "--keep", "1"]
    inv_cl = cl_m.invocation("hi", None, str(env["repo"]), [])
    assert inv_cl.argv.count("--model") == 1 and inv_cl.argv[inv_cl.argv.index("--model") + 1] == "opus"

    # Both model and effort set on antigravity.
    ag = adapter("antigravity", conf, {"model": "m-new", "effort": "high"})
    assert ag.extra_args() == ["--keep", "1"]
    inv_ag = ag.invocation("hi", None, str(env["repo"]), [])
    assert inv_ag.argv.count("--model") == 1 and inv_ag.argv.count("--effort") == 1

    # Codex: only effort set preserves model flags and strips only model_reasoning_effort overrides.
    cx_e = adapter("codex", conf, {"effort": "high"})
    extra_e = cx_e.extra_args()
    assert "-m" in extra_e and "--model" in extra_e and 'model="old6"' in extra_e
    assert not any("model_reasoning_effort" in a for a in extra_e)

    # Codex: both model and effort set strip every conflicting form, keep unrelated args and anything after `--`.
    cx_both = adapter("codex", conf, {"model": "gpt-new", "effort": "high"})
    assert cx_both.extra_args() == ["-c", "other=1", "--profile", "p", "--", "--model", "positional"]
    inv_cx = cx_both.invocation("hi", None, str(env["repo"]), [])
    assert inv_cx.argv.count("-m") == 1 and inv_cx.argv[inv_cx.argv.index("-m") + 1] == "gpt-new"
    assert 'model_reasoning_effort="high"' in inv_cx.argv
    assert inv_cx.argv[-3:] == ["--model", "positional", "-"]

    # Empty settings leave args untouched.
    assert adapter("codex", conf).extra_args() == conf.peer_args("codex")
