"""A fake MCP server for isolation canaries (issue #32, agent-bus thread t-71c7ce, P2).

``agent-bus sentinel-mcp --log PATH --nonce N`` speaks just enough MCP over stdio
(newline-delimited JSON-RPC) to be loaded and called: ``initialize``, ``ping``,
``tools/list`` and one tool, ``sentinel_echo``, which returns the nonce.  It records
each stage separately in the log ("start", "initialize", "handshake" once the client
sends notifications/initialized, "call"), so a canary can tell them apart.
Notifications are never answered.  It never touches anything but its own log.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import IO

TOOL = "sentinel_echo"


def log(path: str, event: str, **fields) -> None:
    with open(path, "a") as f:
        f.write(json.dumps({"event": event, "pid": os.getpid(), "at": time.time(), **fields}) + "\n")


def events(path: str) -> list[dict]:
    try:
        with open(path) as f:
            return [json.loads(line) for line in f if line.strip()]
    except (OSError, ValueError):
        return []


def serve(log_path: str, nonce: str, stdin: IO[str] = sys.stdin, stdout: IO[str] = sys.stdout) -> int:
    log(log_path, "start")

    def reply(msg_id, result=None, error=None) -> None:
        body = {"jsonrpc": "2.0", "id": msg_id}
        body.update({"error": error} if error else {"result": result})
        stdout.write(json.dumps(body) + "\n")
        stdout.flush()

    for line in stdin:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if not isinstance(msg, dict):
            continue
        method, msg_id = msg.get("method"), msg.get("id")
        if "id" not in msg:  # a notification: never answered
            if method == "notifications/initialized":
                log(log_path, "handshake")  # the client confirmed: the handshake is complete
            continue
        if method == "ping":
            reply(msg_id, {})
        elif method == "initialize":
            log(log_path, "initialize")
            reply(msg_id, {"protocolVersion": (msg.get("params") or {}).get("protocolVersion", "2025-06-18"),
                           "capabilities": {"tools": {}},
                           "serverInfo": {"name": "agent-bus-sentinel", "version": "1"}})
        elif method == "tools/list":
            reply(msg_id, {"tools": [{"name": TOOL, "description": "Returns the canary nonce.",
                                      "inputSchema": {"type": "object", "properties": {}}}]})
        elif method == "tools/call":
            name = (msg.get("params") or {}).get("name")
            log(log_path, "call", tool=name)
            if name == TOOL:
                reply(msg_id, {"content": [{"type": "text", "text": nonce}]})
            else:
                reply(msg_id, error={"code": -32602, "message": f"unknown tool {name!r}"})
        else:
            reply(msg_id, error={"code": -32601, "message": f"method not found: {method}"})
    log(log_path, "exit")
    return 0
