#!/usr/bin/env python3
"""Stand-in for claude / codex / agy that speaks each CLI's streaming JSON.

Behaviour is driven by env vars so tests can script outcomes:
  FAKE_AGENT      claude | codex | antigravity (which output format to emit)
  FAKE_BEHAVIOUR  consensus (default) | stubborn | garbage | review
Round-1 prompts get a proposal; later prompts agree with the lowest proposal id
mentioned (or object, for `stubborn`).
"""
import json
import os
import re
import sys
import uuid

agent = os.environ["FAKE_AGENT"]
behaviour = os.environ.get("FAKE_BEHAVIOUR", "consensus")
argv = sys.argv[1:]

if agent == "antigravity":
    prompt = argv[argv.index("--print") + 1]
else:
    prompt = sys.stdin.read()

log = os.environ.get("FAKE_LOG")
if log:
    with open(log, "a") as f:
        f.write(json.dumps({"agent": agent, "argv": argv, "prompt": prompt}) + "\n")

if behaviour == "review" or "Diff from base to head" in prompt or "Interdiff" in prompt:
    reply = 'Looks fine.\n{"verdict": "request_changes", "summary": "one bug", "findings": [{"file": "a.txt", "line": 1, "severity": "high", "comment": "typo"}]}'
elif behaviour == "garbage":
    reply = "I refuse to emit JSON."
elif "round 1, which is blind" in prompt:
    reply = f'My view.\n{{"action": "propose", "proposal": "{agent} says use SQLite", "reasons": "simple"}}'
elif "did not end with the JSON" in prompt:
    reply = '{"action": "vote", "on": "P1", "stance": "agree", "reasons": "fine"}'
else:
    ids = sorted(set(re.findall(r"\bP(\d+)\b", prompt)), key=int)
    on = f"P{ids[0]}" if ids else "P1"
    stance = "object" if behaviour == "stubborn" else "agree"
    reply = f'Thoughts.\n{{"action": "vote", "on": "{on}", "stance": "{stance}", "reasons": "{agent} {stance}s"}}'

sid = None
for flag in ("--session-id", "--resume", "--conversation"):
    if flag in argv:
        sid = argv[argv.index(flag) + 1]
if agent == "codex" and "resume" in argv:
    sid = argv[-2]
sid = sid or str(uuid.uuid4())

def out(obj):
    print(json.dumps(obj), flush=True)

if agent == "claude":
    out({"type": "system", "subtype": "init", "session_id": sid})
    out({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "x"}}]}, "session_id": sid})
    out({"type": "assistant", "message": {"content": [{"type": "text", "text": reply}]}, "session_id": sid})
    out({"type": "result", "subtype": "success", "is_error": False, "result": reply, "session_id": sid})
elif agent == "codex":
    out({"type": "thread.started", "thread_id": sid})
    out({"type": "item.started", "item": {"type": "command_execution", "command": "ls"}})
    out({"type": "item.completed", "item": {"type": "agent_message", "text": reply}})
    out({"type": "turn.completed"})
else:
    out({"event": "init", "conversation_id": sid})
    out({"event": "step_update", "step_update": {"conversation_id": sid, "state": "ACTIVE", "step_type": "agent_response", "text_delta": reply}})
    out({"event": "step_update", "step_update": {"conversation_id": sid, "state": "DONE", "step_type": "agent_response"}})
    out({"event": "result", "result": {"conversation_id": sid, "status": "SUCCESS", "response": reply}})
