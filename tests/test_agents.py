"""#8: any subset of agents (registry as data, `agents` config, detection)."""
import json

import pytest

from agent_bus import config as cfg
from agent_bus.db import HUMAN, BusError
from agent_bus.service import Service


def set_config(env, **changes):
    path = env["tmp"] / "config" / "agent-bus" / "config.json"
    data = json.loads(path.read_text())
    data.update(changes)
    path.write_text(json.dumps(data))


def test_registry_is_the_single_source():
    assert cfg.AGENTS == tuple(cfg.AGENT_INFO)
    from agent_bus import peers, rexcheck
    assert set(peers.ADAPTERS) == set(cfg.AGENTS)
    assert rexcheck.APPS == {a: i["app"] for a, i in cfg.AGENT_INFO.items()}


def test_configured_subset_drives_defaults_and_refusals(env, no_spawn):
    set_config(env, agents=["claude", "codex"])
    bus = env["bus"]
    claude = Service(bus, "claude", bus.register_session("claude"))
    t = claude.start_discussion("Q?")["thread"]
    assert t["participants"] == ["claude", "codex"]
    human = Service(bus, HUMAN).start_discussion("Q?")["thread"]
    assert human["participants"] == ["claude", "codex"]
    for call in (lambda: claude.start_discussion("Q?", peers=["antigravity"]),
                 lambda: claude.send("hi", to="antigravity"),
                 lambda: claude.request_review("antigravity", str(env["repo"]), "a" * 40, "b" * 40)):
        with pytest.raises(BusError) as e:
            call()
        assert e.value.code == "AGENT_UNAVAILABLE" and "antigravity" in e.value.message


def test_unknown_agent_in_config_is_an_error(env):
    set_config(env, agents=["claude", "gemini"])
    with pytest.raises(ValueError, match="gemini"):
        cfg.Config.load().available_agents()


@pytest.mark.parametrize("bad", [{"claude": False}, "claude", 123, True, [123], [{}]])
def test_non_list_or_bad_agents_config_raises_value_error(env, bad):
    set_config(env, agents=bad)
    with pytest.raises(ValueError):
        cfg.Config.load().available_agents()


def test_peer_command_expands_tilde_consistently(env, monkeypatch):
    monkeypatch.setenv("HOME", str(env["tmp"]))
    set_config(env, peers={"claude": {"command": "~/bin/fake-claude"}})
    conf = cfg.Config.load()
    expected = str(env["tmp"] / "bin" / "fake-claude")
    assert conf.peer_command("claude") == expected
    from agent_bus import peers
    assert peers.adapter("claude", conf).command == expected
    avail = conf.availability()
    assert avail["claude"]["available"] and expected in avail["claude"]["reason"]


def test_detection_without_config(env):
    set_config(env, peers={"claude": {"command": str(env["tmp"] / "bin" / "fake-claude")},
                           "codex": {"command": str(env["tmp"] / "missing-codex")},
                           "antigravity": {"command": "definitely-not-on-path-agy"}})
    avail = cfg.Config.load().availability()
    assert avail["claude"]["available"] and "found" in avail["claude"]["reason"]
    assert not avail["codex"]["available"] and not avail["antigravity"]["available"]
    assert "not found" in avail["antigravity"]["reason"]


def test_empty_peers_message_depends_on_defaulting(env, no_spawn, capsys):
    from agent_bus import cli
    bus = env["bus"]
    set_config(env, agents=["claude", "codex"])
    claude = Service(bus, "claude", bus.register_session("claude"))
    with pytest.raises(ValueError) as e:
        claude.start_discussion("Q?", peers=[])
    assert str(e.value) == "a discussion needs at least one peer"
    set_config(env, agents=["claude"])
    claude = Service(bus, "claude", bus.register_session("claude"))
    with pytest.raises(ValueError, match="no other agent is available"):
        claude.start_discussion("Q?")
    set_config(env, agents=[])
    assert cli.main(["ask", "Q?"]) == 2
    assert "no agent is available" in capsys.readouterr().err


def test_reply_cli_checks_recipient_availability(env, no_spawn, capsys):
    from agent_bus import cli
    set_config(env, agents=["claude", "codex"])
    bus = env["bus"]
    tid = Service(bus, HUMAN).start_discussion("Q?")["thread"]["id"]
    before = len(bus.history(tid)["messages"])
    assert cli.main(["reply", tid, "hi", "--to", "antigravity"]) == 2
    assert "AGENT_UNAVAILABLE" in capsys.readouterr().err
    assert len(bus.history(tid)["messages"]) == before
    assert cli.main(["reply", tid, "hi", "--to", "codex"]) == 0
    assert len(bus.history(tid)["messages"]) == before + 1


def test_advance_resume_and_worker_check_availability(env, no_spawn):
    from agent_bus.worker import Worker
    bus = env["bus"]
    set_config(env, agents=["claude", "codex", "antigravity"])
    claude = Service(bus, "claude", bus.register_session("claude"))
    disc_id = claude.start_discussion("Q?", peers=["codex", "antigravity"])["thread"]["id"]
    rev_id = claude.request_review("antigravity", str(env["repo"]), "a" * 40, "b" * 40)["thread"]["id"]
    job_id = bus.jobs(rev_id)[0]["id"]
    bus.set_thread(disc_id, status="needs_human")
    bus.set_thread(rev_id, status="needs_human")

    # Exclude antigravity after the threads already exist.
    set_config(env, agents=["claude", "codex"])
    claude = Service(bus, "claude", bus.register_session("claude"))
    human = Service(bus, HUMAN)
    with pytest.raises(BusError) as e:
        human.resume(disc_id)
    assert e.value.code == "AGENT_UNAVAILABLE" and "antigravity" in e.value.message
    assert bus.thread(disc_id)["status"] == "needs_human"
    with pytest.raises(BusError) as e:
        human.resume(rev_id)
    assert e.value.code == "AGENT_UNAVAILABLE" and "antigravity" in e.value.message
    assert bus.thread(rev_id)["status"] == "needs_human"

    bus.set_thread(disc_id, status="open")
    with pytest.raises(BusError) as e:
        claude.advance_discussion(disc_id)
    assert e.value.code == "AGENT_UNAVAILABLE" and "antigravity" in e.value.message

    w = Worker(bus, job_id)
    assert bus.claim_job(job_id, w.owner, 60)
    with pytest.raises(BusError) as e:
        w.turn("antigravity", "hello")
    assert e.value.code == "AGENT_UNAVAILABLE" and "antigravity" in e.value.message
    assert not env["log"].exists()


def test_agents_cli_ask_default_and_whoami(env, no_spawn, capsys):
    from agent_bus import cli, mcp_server
    set_config(env, agents=["claude", "codex"])
    assert cli.main(["agents"]) == 0
    out = capsys.readouterr().out
    assert "config `agents`" in out and "no   antigravity" in out
    assert cli.main(["agents", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["codex"]["available"]

    assert cli.main(["ask", "Q?"]) == 0
    out = capsys.readouterr().out
    assert "with claude, codex" in out
    threads = env["bus"].threads()
    assert len(threads) == 1 and threads[0]["participants"] == ["claude", "codex"]

    server = mcp_server.build("claude", bus=env["bus"])
    import asyncio
    res = asyncio.run(server.call_tool("whoami", {}))
    payload = json.loads(res.content[0].text)
    assert payload["agent"] == "claude"
    assert payload["available_agents"] == ["codex"]
    assert payload["unavailable_agents"] == {"antigravity": "not listed in config `agents`",
                                             "copilot": "not listed in config `agents`"}
