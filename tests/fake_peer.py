#!/usr/bin/env python3
"""Stand-in for claude / codex / agy that speaks each CLI's streaming JSON.

Behaviour is driven by env vars so tests can script outcomes:
  FAKE_AGENT      claude | codex | antigravity (which output format to emit)
  FAKE_BEHAVIOUR  consensus (default) | stubborn | garbage | review
  FAKE_EXHAUSTED  comma-separated agents that fail every turn as out of usage, in their
                  CLI's own shape (FAKE_EXHAUSTED_LATER: the same, but only after round 1;
                  FAKE_EXHAUSTED_REPAIR: only on a "reply again with only the JSON" repair turn)
  FAKE_BEHAVIOUR=nojson  reply without any JSON, to every prompt but a repair
  FAKE_BEHAVIOUR=silent  exit 0 after the session start, with no reply (format drift);
                  FAKE_SILENT_REPAIR=agents does the same only on a repair turn, and
                  FAKE_STRAY adds a non-JSON stdout line; FAKE_SILENT=agents is silent for just
                  those agents, and FAKE_SILENT_LATER=agents only after round 1
  FAKE_BEHAVIOUR=newevents  also emit event types no adapter knows
  FAKE_BEHAVIOUR=denied  (antigravity) agy's headless auto-denial of run_command: exit 0, no
                  reply, denied_actions in the result.  denied_reply: the same, then a reply;
                  denied_blank: a whitespace-only response; unfinished: the tool never reaches an end
                  state; toolerror: ERROR, no denial
  FAKE_MCP_LIST   what `codex mcp list --json` prints
  FAKE_APPROVE    comma-separated agents whose review verdicts approve (others request changes)
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

if argv == ["--version"]:  # agent-bus probes this once per binary (issue #32); not a turn
    print(os.environ.get("FAKE_VERSION", f"fake-{agent} 0.0.0"))
    sys.exit(0)

if agent == "codex":
    # Like the real CLI: a quoted key in a -c override names a different server, which
    # then has no transport.
    for i, a in enumerate(argv[:-1]):
        if a == "-c" and argv[i + 1].startswith("mcp_servers.") and '"' in argv[i + 1].split("=", 1)[0]:
            print('Error loading config.toml: invalid transport in `mcp_servers."x"`', file=sys.stderr)
            sys.exit(1)

    rest = list(argv)
    while len(rest) >= 2 and rest[0] in ("-c", "--profile"):
        rest = rest[2:]
    if rest[:2] == ["mcp", "list"]:
        print(os.environ.get("FAKE_MCP_LIST") or json.dumps(
            [{"name": "agent-bus", "enabled": True}, {"name": "github", "enabled": True},
             {"name": "off", "enabled": False}]))
        sys.exit(0)

if behaviour == "hang":
    import time
    time.sleep(60)  # never reads stdin, never writes stdout

if agent == "antigravity":
    prompt = argv[argv.index("--print") + 1]
else:
    prompt = sys.stdin.read()

log = os.environ.get("FAKE_LOG")
if log:
    with open(log, "a") as f:
        f.write(json.dumps({"agent": agent, "argv": argv, "prompt": prompt,
                            "peer_env": os.environ.get("AGENT_BUS_PEER")}) + "\n")

repair = "Reply again with only" in prompt
if behaviour == "nojson" and not repair:
    reply = "Thinking out loud, no JSON."
elif (behaviour == "review" or "Diff from base to head" in prompt or "Interdiff" in prompt) \
        and agent in os.environ.get("FAKE_APPROVE", "").split(","):
    reply = 'All good.\n{"verdict": "approve", "summary": "fine", "findings": []}'
elif behaviour == "review" or "Diff from base to head" in prompt or "Interdiff" in prompt:
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
resumed = sid is not None
sid = sid or str(uuid.uuid4())


def session_turns():
    """This turn's number in its session (from the log), for CLIs that report running totals."""
    if not resumed or not log:
        return 1
    n = 0
    for line in open(log):
        c = json.loads(line)
        if c["agent"] == agent and sid in c["argv"]:
            n += 1
    return 1 + n  # the first turn ran without the id in its argv

def out(obj):
    print(json.dumps(obj), flush=True)


def listed(var):
    return agent in [a for a in os.environ.get(var, "").split(",") if a]


if (listed("FAKE_EXHAUSTED") or (listed("FAKE_EXHAUSTED_LATER") and "round 1, which is blind" not in prompt)
        or (listed("FAKE_EXHAUSTED_REPAIR") and repair)):
    if agent == "claude":
        out({"type": "system", "subtype": "init", "session_id": sid})
        out({"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "resetsAt": 4102444800,
                                                              "rateLimitType": "five_hour", "isUsingOverage": False},
             "session_id": sid})
        out({"type": "result", "subtype": "success", "is_error": True,
             "result": "You've hit your limit · resets 3pm", "session_id": sid})
    elif agent == "codex":
        out({"type": "thread.started", "thread_id": sid})
        out({"type": "error", "message": "You've hit your usage limit. Upgrade to Pro or try again in 2 hours 5 minutes."})
        out({"type": "turn.failed", "error": {"message": "You've hit your usage limit. Upgrade to Pro or try again in 2 hours 5 minutes."}})
    else:
        out({"event": "init", "conversation_id": sid})
        # Verbatim from agy 1.5.13 (2026-11-22).
        out({"event": "result", "result": {"conversation_id": sid, "status": "ERROR",
                                           "error": "Individual quota reached. Please upgrade your subscription "
                                                    "to increase your limits. Resets in 2h26m14s."}})
    sys.exit(1)

if (behaviour == "silent" or (repair and listed("FAKE_SILENT_REPAIR")) or listed("FAKE_SILENT")
        or (listed("FAKE_SILENT_LATER") and "round 1, which is blind" not in prompt)):
    if agent == "claude":
        out({"type": "system", "subtype": "init", "session_id": sid})
    elif agent == "codex":
        out({"type": "thread.started", "thread_id": sid})
    else:
        out({"event": "init", "conversation_id": sid})
    if os.environ.get("FAKE_STRAY"):
        print("warning: something unstructured", flush=True)
    sys.exit(0)
if agent == "antigravity" and repair and os.environ.get("FAKE_AGY_DENIED_REPAIR"):
    behaviour = "denied"
if agent == "antigravity" and behaviour in ("denied", "denied_reply", "denied_blank", "unfinished", "toolerror"):
    # The headless denial agy 1.5.13 sends in plan mode with --sandbox (live probe,
    # 2026-11-23): run_command goes ACTIVE then ERROR, the result is SUCCESS with an empty
    # response and lists denied_actions, exit 0.  Variants: denied_reply also answers from
    # context; denied_blank's response is whitespace only; unfinished leaves the tool with
    # no end state; toolerror has no denial.
    su = lambda **kw: out({"event": "step_update", "step_update": {"conversation_id": sid, **kw}})  # noqa: E731
    tool = {"step_type": "tool", "tool_name": "run_command",
            "tool_info": {"name": "run_command", "parameters": {"CommandLine": "jj log"}}}
    out({"event": "init", "conversation_id": sid, "init": {"permission_mode": "request-review"}})
    su(step_index=0, state="DONE", step_type="user_input")
    su(step_index=1, state="DONE", step_type="agent_response",
       usage={"input_tokens": 12732, "output_tokens": 652, "thinking_tokens": 366, "cache_read_tokens": 0})
    su(step_index=2, state="ACTIVE", **tool)
    if behaviour != "unfinished":
        su(step_index=2, state="ERROR", **tool)
    response = "  \n" if behaviour == "denied_blank" else ""
    if behaviour == "denied_reply":
        response = reply
        su(step_index=3, state="ACTIVE", step_type="agent_response", text_delta=reply)
        su(step_index=3, state="DONE", step_type="agent_response")
    res = {"conversation_id": sid, "status": "SUCCESS", "response": response,
           "usage": {"input_tokens": 12732, "output_tokens": 652, "thinking_tokens": 366, "cache_read_tokens": 0}}
    if behaviour.startswith("denied"):
        res["denied_actions"] = [{"action": "command", "display_name": "Bash"}]
    out({"event": "result", "result": res})
    sys.exit(0)
if behaviour == "newevents":
    out({"type": "brand_new_event"} if agent != "antigravity" else {"event": "brand_new_event"})
    out({"type": "brand_new_event"} if agent != "antigravity" else {"event": "brand_new_event"})

if agent == "claude":
    out({"type": "system", "subtype": "init", "session_id": sid})
    out({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "x"}}]}, "session_id": sid})
    out({"type": "assistant", "message": {"content": [{"type": "text", "text": reply}]}, "session_id": sid})
    out({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "resetsAt": 4102444800,
                                                          "rateLimitType": "five_hour"}, "session_id": sid})
    out({"type": "result", "subtype": "success", "is_error": False, "result": reply, "session_id": sid,
         "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 100,
                   "output_tokens_details": {"thinking_tokens": 3}}})
elif agent == "codex":
    out({"type": "thread.started", "thread_id": sid})
    out({"type": "item.started", "item": {"type": "command_execution", "command": "ls"}})
    out({"type": "item.completed", "item": {"type": "agent_message", "text": reply}})
    n = session_turns()  # like codex 0.171.0: the session's running total
    out({"type": "turn.completed", "usage": {"input_tokens": 20 * n, "cached_input_tokens": 200 * n,
                                             "output_tokens": 6 * n, "reasoning_output_tokens": 4 * n}})
else:
    out({"event": "init", "conversation_id": sid})
    out({"event": "step_update", "step_update": {"conversation_id": sid, "state": "ACTIVE", "step_type": "agent_response", "text_delta": reply}})
    step = {"input_tokens": 30, "output_tokens": 7, "thinking_tokens": 5, "cache_read_tokens": 0}
    out({"event": "step_update", "step_update": {"conversation_id": sid, "state": "DONE", "step_type": "agent_response",
                                                 "usage": step}})
    n = session_turns()  # like agy 1.5.13: result.usage is the conversation's running total
    out({"event": "result", "result": {"conversation_id": sid, "status": "SUCCESS", "response": reply,
                                       "num_turns": n, "usage": {k: v * n for k, v in step.items()}}})
