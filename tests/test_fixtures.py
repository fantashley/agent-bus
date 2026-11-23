"""Replay scrubbed real CLI streams through the production adapters (issue #32)."""
import json
import stat
import sys
from pathlib import Path

import pytest

from agent_bus import config as cfg
from agent_bus.peers import adapter

DATA = Path(__file__).parent / "data" / "streams"
MANIFEST = json.loads((DATA / "MANIFEST.json").read_text())["fixtures"]


def replay(env, agent, fixture):
    """A fake CLI that prints the fixture, wired in as this agent's command."""
    script = env["tmp"] / f"replay-{agent}"
    # Codex lists its MCP servers before each turn: answer with none.
    script.write_text(f"#!{sys.executable}\nimport sys\n"
                      f"if 'mcp' in sys.argv and 'list' in sys.argv:\n    print('[]'); sys.exit(0)\n"
                      f"sys.stdout.write(open({str(fixture)!r}).read())\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    conf_path = env["tmp"] / "config" / "agent-bus" / "config.json"
    conf = json.loads(conf_path.read_text())
    conf["peers"][agent] = {"command": str(script)}
    conf_path.write_text(json.dumps(conf))
    return adapter(agent, cfg.Config.load())


@pytest.mark.parametrize("fx", MANIFEST, ids=[f["file"] for f in MANIFEST])
def test_real_stream_parses(env, fx):
    events = []
    ad = replay(env, fx["agent"], DATA / fx["file"])
    res = ad.run("prompt", str(env["repo"]), 30, lambda kind, text: events.append((kind, text)),
                 resume_id="00000000-0000-4000-8000-000000000001" if "resume" in fx["file"] else None)
    assert res.ok, res.error
    assert res.external_id, "no session id: resume would break"
    if "final" in fx:
        assert res.text.strip() == fx["final"]
    for word in fx.get("final_contains", []):
        assert word in res.text
    assert res.usage and res.usage.get("input") is not None
    # The tool calls the capture is about show up as progress (issue #46).
    for tool in fx.get("tools", []):
        assert any(k == "tool" and t.startswith(tool + " ") for k, t in events), (tool, events)
    # Every event type in a real capture is one the adapter knows.
    assert not [t for k, t in events if k == "format"], events


def test_manifest_lists_every_fixture():
    listed = {f["file"] for f in MANIFEST}
    on_disk = {p.name for p in DATA.glob("*.jsonl")}
    assert listed == on_disk
    for f in MANIFEST:
        assert {"agent", "cli", "captured", "scenario"} <= set(f)


LEAKS = [
    (r"/home/(?!user\b)[\w.-]+", "a real home directory"),
    (r"/Users/(?!user\b)[\w.-]+", "a real macOS home directory"),
    (r"-home-(?!user\b)[\w.]+", "an encoded home directory"),
    (r"/tmp/claude-\d|-tmp-claude-\d|scratchpad", "a scratch directory"),
    # The scrubber leaves a bare /tmp/scratch; anything glued on means a fragment of a
    # streamed path was scrubbed alone and the rest of it survived.
    (r"/tmp/scratch(?=[^\s\"'`)\]/]|/)", "a partly scrubbed path"),
    (r"\b(?:msg|toolu|req|call)_(?:bdrk_)?(?!fixture)[A-Za-z0-9]{6,}", "an unscrubbed message or tool id"),
    (r"\b(?!00000000-0000-4000-8000-)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
     "an unscrubbed UUID"),
]


def texts(path):
    """Every key and value in a fixture, plus streamed fragments reassembled per step
    (Antigravity's text_delta, Claude's stream_event deltas), so a path split across
    frames is checked whole.  Independent of who runs the tests."""
    out, streams = [], {}

    def walk(v):
        if isinstance(v, dict):
            for k, x in v.items():
                out.append(str(k))
                if k == "credential_expires_at":  # account data: only the synthetic value
                    assert x == "2099-01-01T00:00:00.000Z", (path.name, "a real credential expiry")
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
        elif v is not None:
            out.append(str(v))

    for line in path.read_text().splitlines():
        obj = json.loads(line)
        walk(obj)
        su = obj.get("step_update") or {}
        if isinstance(su.get("text_delta"), str):
            streams.setdefault(("agy", su.get("step_index")), []).append(su["text_delta"])
        delta = ((obj.get("event") or {}).get("delta") or {}) if obj.get("type") == "stream_event" else {}
        if isinstance(delta.get("text"), str):
            streams.setdefault(("claude", (obj.get("event") or {}).get("index")), []).append(delta["text"])
    return out + ["".join(parts) for parts in streams.values()]


@pytest.mark.parametrize("fx", MANIFEST, ids=[f["file"] for f in MANIFEST])
def test_fixtures_leak_nothing_even_when_reassembled(fx):
    import re
    for text in texts(DATA / fx["file"]):
        for pattern, what in LEAKS:
            assert not re.search(pattern, text), (fx["file"], what, text[:120])


def test_the_leak_guard_catches_a_path_split_across_frames(tmp_path):
    import re
    f = tmp_path / "x.jsonl"
    f.write_text("\n".join(json.dumps({"event": "step_update", "step_update": {"step_index": 3, "text_delta": t}})
                           for t in ("see file:///ho", "me/alice/src/notes.txt")) + "\n")
    joined = texts(f)
    assert any(re.search(LEAKS[0][0], t) for t in joined)


def test_claude_init_events_list_only_placeholder_tooling():
    """A capture's init event lists the machine's own commands, skills and plugins: only the
    scrubber's fixed placeholders may be committed."""
    sys.path.insert(0, str(DATA))
    from scrub import INIT_PLACEHOLDERS
    for path in [*DATA.glob("claude-*.jsonl"), *(DATA.parent / "bedrock").glob("claude-*.jsonl")]:
        for line in path.read_text().splitlines():
            obj = json.loads(line)
            if obj.get("type") == "system" and obj.get("subtype") == "init":
                for key, value in INIT_PLACEHOLDERS.items():
                    assert obj.get(key, value) == value, (path.name, key)
