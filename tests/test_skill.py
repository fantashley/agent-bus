"""The agent-bus skill (skills/agent-bus/SKILL.md) is the one how-to agents get: it must
cover every MCP tool and every error code an agent can be handed (#107)."""

import asyncio
import re
from pathlib import Path

from agent_bus import mcp_server, unvotable

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skills" / "agent-bus" / "SKILL.md"
SRC = ROOT / "src" / "agent_bus"
# Raised only inside the worker, never returned to an agent's tool call.
INTERNAL = {"LEASE_LOST", "NO_SUCH_JOB"}


def skill_text() -> str:
    return SKILL.read_text()


def frontmatter(text: str) -> dict[str, str]:
    assert text.startswith("---\n"), "SKILL.md must start with YAML frontmatter"
    block = text[4:text.index("\n---\n", 4)]
    return dict(line.split(": ", 1) for line in block.splitlines())


def test_frontmatter_names_the_skill_and_says_when_to_load_it():
    meta = frontmatter(skill_text())
    assert meta["name"] == "agent-bus"
    desc = meta["description"]
    # Clients cap descriptions at 1024 characters (the Agent Skills format).
    assert len(desc) <= 1024
    for trigger in ("start_discussion", "request_review", "debrief", "peer_data"):
        assert trigger in desc


def test_skill_mentions_every_mcp_tool(env):
    server = mcp_server.build("claude", bus=env["bus"])
    tools = [t.name for t in asyncio.run(server.list_tools())]
    assert "request_review" in tools
    text = skill_text()
    missing = [t for t in tools if f"`{t}`" not in text]
    assert not missing, f"SKILL.md doesn't mention {missing}"


def error_codes() -> set[str]:
    codes = set(unvotable.CODES.values()) | {"PEERS_CANNOT_VOTE", "DEBRIEF_UNAVAILABLE"}
    for path in SRC.glob("*.py"):
        src = path.read_text()
        codes |= set(re.findall(r'BusError\(\s*"([A-Z][A-Z_]+)"', src))
        codes |= set(re.findall(r'why = \("([A-Z][A-Z_]+)"', src))
        codes |= set(re.findall(r'"error": "([A-Z][A-Z_]+)"', src))
    return codes - INTERNAL


def test_skill_covers_every_error_code():
    codes = error_codes()
    assert {"THREAD_LIMIT_REACHED", "REVIEW_LIMIT_REACHED", "USAGE_EXHAUSTED", "AUTH_REQUIRED",
            "NO_SESSION", "PEER_READ_ONLY", "BAD_REQUEST"} <= codes
    rows = {m.group(1) for m in re.finditer(r"^\| `([A-Z][A-Z_]+)` \|", skill_text(), re.M)}
    assert not codes - rows, f"SKILL.md's error table is missing {sorted(codes - rows)}"
    assert not rows - codes, f"SKILL.md lists codes the code never returns: {sorted(rows - codes)}"


def test_skill_has_the_initiator_guidance():
    text = skill_text()
    for phrase in ("Round 1 is blind", "reasons=", "prev_head_commit_id", "reviewers=[",
                   "acceptance criteria", "`testing`", "Paste evidence", "Name the files to read",
                   "Keep the requested output short", "DEADLINE_REACHED", "headless agent-bus peer"):
        assert phrase in text, phrase


def test_mcp_instructions_keep_the_core_rules():
    text = mcp_server.INSTRUCTIONS
    for phrase in ("peer_data", "headless peer", "must not call agent-bus tools", "start_discussion",
                   "request_review", "debrief", "Don't ping-pong", "THREAD_LIMIT_REACHED",
                   "REVIEW_LIMIT_REACHED", "stop and tell the user", "`agent-bus` skill"):
        assert phrase in text, phrase
