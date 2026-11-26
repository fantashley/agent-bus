"""#139: `agent-bus mcp` moves onto the installed build at the next idle tool call, over one
real stdio connection, with no client reload."""

import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from agent_bus import config as cfg
from agent_bus import mcp_shim
from agent_bus import service

SRC = str(Path(__file__).resolve().parent.parent / "src")


def make_build(root: Path, name: str, log: Path, extra_tool: bool = False, broken: bool = False,
               hang: bool = False, pre_shim: bool = False) -> Path:
    """A fake installed build: bin/agent-bus running this checkout, plus a `which_build`
    tool naming it (and an `extra` tool when the catalog should differ).  `broken` exits at
    once, `hang` never answers, `pre_shim` ignores the shim's session like builds before it."""
    exe = root / name / "bin" / "agent-bus"
    exe.parent.mkdir(parents=True, exist_ok=True)
    script = (f"#!{sys.executable}\nimport os, sys\nsys.path.insert(0, {SRC!r})\n"
              f"with open({str(log)!r}, 'a') as f:\n"
              f"    f.write({name!r} + ' ' + str(os.getpid()) + ' ' + os.environ.get('AGENT_BUS_MCP_SESSION', '-') + '\\n')\n")
    if broken:
        script += "sys.exit(3)\n"
    if hang:
        script += "import time\ntime.sleep(60)\n"
    script += ("from agent_bus import mcp_server\n_build = mcp_server.build\n"
               "def build(*a, **k):\n    server = _build(*a, **k)\n"
               f"    @server.tool()\n    def which_build() -> str:\n        return {name!r}\n")
    if extra_tool:
        script += "    @server.tool()\n    def extra() -> str:\n        return 'extra'\n"
    script += "    return server\nmcp_server.build = build\n"
    if pre_shim:  # serves in-process and registers its own session, as builds before #139 did
        script += "os.environ.pop('AGENT_BUS_MCP_SESSION', None)\nmcp_server.main('claude')\n"
    else:
        script += "from agent_bus.cli import main\nsys.exit(main())\n"
    exe.write_text(script)
    exe.chmod(0o755)
    return exe


class Client:
    """A bare MCP client speaking newline-delimited JSON-RPC over the server's stdio."""

    def __init__(self, cmd: list[str], env: dict):
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env)
        self.msgs: queue.Queue = queue.Queue()
        self.notes: list[dict] = []
        self.responses: dict = {}
        self.next_id = 0
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in iter(self.proc.stdout.readline, b""):
            self.msgs.put(json.loads(line))
        self.msgs.put(None)

    def send(self, msg: dict):
        self.proc.stdin.write(json.dumps(msg).encode() + b"\n")
        self.proc.stdin.flush()

    def request(self, method: str, params: dict | None = None) -> int:
        self.next_id += 1
        self.send({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params or {}})
        return self.next_id

    def response(self, rid: int, timeout: float = 60) -> dict:
        while rid not in self.responses:
            m = self.msgs.get(timeout=timeout)
            assert m is not None, "the server closed the connection"
            if "id" in m and "method" not in m:
                self.responses[m["id"]] = m
            else:
                self.notes.append(m)
        return self.responses.pop(rid)

    def initialize(self) -> dict:
        res = self.response(self.request("initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "test-client", "version": "1"}}))
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return res["result"]

    def call(self, name: str, **args):
        return self.result(self.request("tools/call", {"name": name, "arguments": args}))

    def result(self, rid: int):
        res = self.response(rid)["result"]
        assert not res.get("isError"), res
        text = res["content"][0]["text"]
        try:
            return json.loads(text)
        except ValueError:
            return text

    def close(self):
        self.proc.stdin.close()
        return self.proc.wait(30)


@pytest.fixture
def installs(env, monkeypatch):
    """A profile whose bin/agent-bus links to the "old" build, first on PATH."""
    tmp = env["tmp"]
    log = tmp / "builds.log"
    profile = tmp / "profile" / "bin"
    profile.mkdir(parents=True)
    link = profile / "agent-bus"
    link.symlink_to(make_build(tmp, "old", log))
    path = f"{profile}:{os.environ['PATH']}"
    monkeypatch.setenv("PATH", path)

    def switch(target: Path):
        # Atomically, as a profile switch does.
        tmp_link = link.with_name("agent-bus.new")
        tmp_link.symlink_to(target)
        os.replace(tmp_link, link)

    def started(children_only=True):
        """The processes the builds started: by default the servers (the shim's children)."""
        lines = [line.split() for line in log.read_text().splitlines()] if log.exists() else []
        return [b for b in lines if b[2] != "-"] if children_only else lines

    # Generous, so a loaded test machine doesn't turn a slow start into "stay on the old build".
    monkeypatch.setenv("AGENT_BUS_MCP_SWAP_DEADLINE_S", "30")

    def connect():
        client = Client([str(link), "mcp", "--agent", "claude"], dict(os.environ, PATH=path))
        info = client.initialize()
        return client, info

    return {"tmp": tmp, "log": log, "link": link, "switch": switch, "started": started, "connect": connect}


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_next_idle_call_runs_the_installed_build(env, installs):
    """The acceptance: after the profile moves to a new build, the open session's next tool
    call runs it, once, on the same bus session with its read cursor."""
    client, info = installs["connect"]()
    assert info["capabilities"]["tools"]["listChanged"] is True  # set by the shim
    me = client.call("whoami")
    assert client.call("which_build") == "old"
    assert "stale_build" not in me
    [(name, old_pid, sid)] = installs["started"]()
    assert name == "old" and sid == me["session_id"]  # the child serves the shim's session
    session = env["bus"].session(me["session_id"])
    assert session["pid"] == client.proc.pid  # the shim's, so the row outlives a swap
    assert session["client_pid"] == os.getpid()  # the real client, for hooks

    # A thread with something read, so the cursor can be checked across the swap.
    tid = client.call("send", to="codex", body="before")["thread"]["id"]
    codex = service.Service(env["bus"], agent="codex")
    codex.send(body="read before", thread=tid)
    assert ["read before" in m["body"] for m in client.call("inbox", thread=tid)["messages"]] == [True]

    installs["switch"](make_build(installs["tmp"], "new", installs["log"]))
    # The first call after the upgrade changes state: it runs exactly once, on the new build.
    sent = client.call("send", thread=tid, body="after the upgrade")
    assert [b[0] for b in installs["started"]()] == ["old", "new"]
    import time
    for _ in range(100):  # retired in the background, off the call's path
        if not alive(int(old_pid)):
            break
        time.sleep(0.1)
    assert not alive(int(old_pid))
    rows = env["bus"]._all("SELECT body FROM messages WHERE thread_id=? AND sender='claude'", tid)
    assert [r["body"] for r in rows] == ["before", "after the upgrade"]
    assert "after the upgrade" in sent["posted"]["body"]
    assert client.call("which_build") == "new"
    after = client.call("whoami")
    assert after["session_id"] == me["session_id"] and "stale_build" not in after
    # Cursor kept: what was read before isn't unread again.
    codex.send(body="read after", thread=tid)
    assert ["read after" in m["body"] for m in client.call("inbox", thread=tid)["messages"]] == [True]
    # Same tool list: nothing to announce.
    assert not [n for n in client.notes if n.get("method") == "notifications/tools/list_changed"]
    assert client.close() == 0


def test_no_swap_while_a_call_is_in_flight(env, installs):
    """A call that arrives while a wait runs goes to the current build; the next idle one
    moves."""
    client, _ = installs["connect"]()
    me = client.call("whoami")
    tid = client.call("send", to="codex", body="hi")["thread"]["id"]
    client.call("wait", thread=tid, timeout_s=1)  # nothing left unread: the next wait blocks
    waiting = client.request("tools/call", {"name": "wait", "arguments": {"thread": tid, "timeout_s": 8}})
    installs["switch"](make_build(installs["tmp"], "new", installs["log"]))
    during = client.call("whoami")
    assert during["session_id"] == me["session_id"]
    assert "moves onto the installed build by itself" in during["stale_build"]["hint"]
    assert [b[0] for b in installs["started"]()] == ["old"]
    assert client.result(waiting)["status"] == "timeout"  # the wait ran to its end on the old build
    assert client.call("which_build") == "new"
    client.close()


def test_a_cancelled_wait_does_not_hold_the_move(env, installs):
    """wait is async: a cancellation ends it, so the next call moves (the cancellation may
    name the request id as a string)."""
    client, _ = installs["connect"]()
    tid = client.call("send", to="codex", body="hi")["thread"]["id"]
    client.call("wait", thread=tid, timeout_s=1)
    waiting = client.request("tools/call", {"name": "wait", "arguments": {"thread": tid, "timeout_s": 30}})
    client.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": str(waiting)}})
    installs["switch"](make_build(installs["tmp"], "new", installs["log"]))
    assert client.call("which_build") == "new"
    client.close()


def bare_shim():
    shim = mcp_shim.Shim.__new__(mcp_shim.Shim)
    shim.lock = threading.Lock()
    shim.in_flight, shim.server_pending, shim.cancelled = {}, set(), set()
    shim.init = shim.initialized = None
    shim.child = type("C", (), {"write": lambda self, line: None})()
    return shim


def test_a_cancelled_synchronous_call_keeps_the_session_on_its_server():
    """Other tools run on in a thread after a cancellation and get no reply: until one
    comes, the session isn't moved off the server that may still be running it."""
    shim = bare_shim()
    for rid, tool in ((7, "send"), (8, "wait")):
        shim._from_client(json.dumps({"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                                      "params": {"name": tool, "arguments": {}}}).encode())
    for rid in ("7", 8):
        shim._from_client(json.dumps({"jsonrpc": "2.0", "method": "notifications/cancelled",
                                      "params": {"requestId": rid}}).encode())
    assert shim.in_flight == {} and shim.cancelled == {"7"} and shim._busy()
    import io
    shim.out_lock, shim.stdout = threading.Lock(), io.BytesIO()
    reply = {"jsonrpc": "2.0", "id": 7, "result": {"content": []}}  # a late reply settles it
    shim._from_child(shim.child, json.dumps(reply).encode(), [reply])
    assert not shim._busy()


def test_a_client_id_like_the_shims_own_is_answered(env, installs):
    client, _ = installs["connect"]()
    for rid in (mcp_shim.INIT_ID, mcp_shim.LIST_ID):
        client.send({"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                     "params": {"name": "which_build", "arguments": {}}})
        assert client.response(rid, timeout=30)["result"]["content"][0]["text"] == "old"
    client.close()


def test_a_changed_tool_list_is_announced(env, installs):
    client, _ = installs["connect"]()
    before = client.response(client.request("tools/list"))["result"]["tools"]
    assert "extra" not in {t["name"] for t in before}
    installs["switch"](make_build(installs["tmp"], "new", installs["log"], extra_tool=True))
    assert client.call("which_build") == "new"
    assert [n for n in client.notes if n.get("method") == "notifications/tools/list_changed"]
    after = client.response(client.request("tools/list"))["result"]["tools"]
    assert "extra" in {t["name"] for t in after}
    client.close()


def test_a_broken_build_keeps_the_old_one(env, installs):
    client, _ = installs["connect"]()
    broken = make_build(installs["tmp"], "broken", installs["log"], broken=True)
    installs["switch"](broken)
    assert client.call("which_build") == "old"
    assert client.call("which_build") == "old"
    assert [b[0] for b in installs["started"]()] == ["old", "broken"]  # tried once, not on every call
    # A rollback forgets the failure: the same build, fixed, is tried again.
    installs["switch"](installs["tmp"] / "old" / "bin" / "agent-bus")
    assert client.call("which_build") == "old"
    make_build(installs["tmp"], "broken", installs["log"])
    installs["switch"](broken)
    assert client.call("which_build") == "broken"
    client.close()


def test_a_hung_build_does_not_hold_the_call_long(env, installs, monkeypatch):
    import time
    monkeypatch.setenv("AGENT_BUS_MCP_SWAP_DEADLINE_S", "2")
    client, _ = installs["connect"]()
    installs["switch"](make_build(installs["tmp"], "hung", installs["log"], hang=True))
    start = time.monotonic()
    assert client.call("which_build") == "old"
    assert time.monotonic() - start < 2 + 3
    start = time.monotonic()
    assert client.call("which_build") == "old"
    assert time.monotonic() - start < 2  # not retried on every call
    client.close()


def test_a_build_from_before_the_shim_is_not_moved_to(env, installs):
    """A rollback to a build that ignores the shim's session would register another one,
    with the shim as its client: the session stays on the current build instead."""
    client, _ = installs["connect"]()
    me = client.call("whoami")
    installs["switch"](make_build(installs["tmp"], "pre", installs["log"], pre_shim=True))
    assert client.call("which_build") == "old"
    assert client.call("whoami")["session_id"] == me["session_id"]
    client.close()


def test_missing_shim_session_fails_startup(env, monkeypatch):
    from agent_bus import mcp_server
    monkeypatch.setenv(mcp_shim.SESSION_ENV, "s-missing")
    with pytest.raises(SystemExit, match="s-missing"):
        mcp_server.build("claude", bus=env["bus"])


def test_installed_path_from_the_mcp_entry(env, installs, monkeypatch):
    """Claude's plugin entry runs the profile's agent-bus by absolute path and names it in
    AGENT_BUS_INSTALLED: upgrades are seen even when the client's PATH lacks the profile."""
    link = installs["link"]
    path = os.environ["PATH"].split(":", 1)[1]  # without the profile
    client = Client([str(link), "mcp", "--agent", "claude"],
                    dict(os.environ, PATH=path, AGENT_BUS_INSTALLED=str(link)))
    client.initialize()
    assert client.call("which_build") == "old"
    installs["switch"](make_build(installs["tmp"], "new", installs["log"]))
    assert client.call("which_build") == "new"
    client.close()


def test_enabled_reads_only_its_own_key(env):
    conf = cfg.config_path()
    base = json.loads(conf.read_text())
    conf.write_text(json.dumps({**base, "a_setting_from_a_newer_build": {"x": 1}, "peers": "not this schema"}))
    assert mcp_shim.enabled() is True
    conf.write_text(json.dumps({**base, "auto_restart_mcp": False}))
    assert mcp_shim.enabled() is False


def test_children_drop_the_wrappers_path_entries(monkeypatch, tmp_path):
    bin_dir = tmp_path / "store" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "agent-bus").write_text(
        "#! /bin/bash -e\n"
        "PATH='/nix/store/aaa-python3.14-dep/bin'$PATH\n"
        f"PATH='{bin_dir}'$PATH\n"
        "if [[ $PATH != *':''/nix/store/bbb-git-2/bin'':'* ]]; then\n"
        "    PATH=$PATH'/nix/store/bbb-git-2/bin'\n"
        "fi\n"
        "PATH=$PATH':''/nix/store/ccc-git-3/bin:/nix/store/ddd-jujutsu-1/bin'\n")
    monkeypatch.setattr(cfg, "_LAUNCHED_FROM", str(bin_dir / "agent-bus"))
    monkeypatch.setattr(sys, "orig_argv", ["python3", str(bin_dir / ".agent-bus-wrapped"), "mcp"])
    added = cfg.wrapper_path_dirs()
    assert str(bin_dir) in added and "/nix/store/bbb-git-2/bin" in added
    assert "/nix/store/aaa-python3.14-dep/bin" in added
    assert {"/nix/store/ccc-git-3/bin", "/nix/store/ddd-jujutsu-1/bin"} <= added  # makeBinPath's ':'
    # Only the wrapper's runs at either end go; the client's own copy of a directory stays.
    path = ":".join([str(bin_dir), "/nix/store/aaa-python3.14-dep/bin", "/usr/bin",
                     "/nix/store/ddd-jujutsu-1/bin", "/bin", "/nix/store/bbb-git-2/bin",
                     "/nix/store/ccc-git-3/bin"])
    assert mcp_shim.without_wrapper_dirs(path, added) == "/usr/bin:/nix/store/ddd-jujutsu-1/bin:/bin"


def test_opt_out_runs_the_server_in_process(env, installs):
    conf = cfg.config_path()
    conf.write_text(json.dumps({**json.loads(conf.read_text()), "auto_restart_mcp": False}))
    client, info = installs["connect"]()
    assert info["capabilities"]["tools"].get("listChanged") is not True
    me = client.call("whoami")
    [(name, pid, sid)] = installs["started"](children_only=False)
    assert sid == "-" and int(pid) == client.proc.pid  # no shim, no child
    installs["switch"](make_build(installs["tmp"], "new", installs["log"]))
    stale = client.call("whoami")["stale_build"]
    assert "/reload-plugins" in stale["hint"] and stale["running"] != stale["on_path"]
    assert client.call("which_build") == "old"
    assert me["session_id"]
    client.close()


def test_bad_opt_out_value_is_refused(env):
    conf = cfg.config_path()
    conf.write_text(json.dumps({"auto_restart_mcp": "no"}))
    with pytest.raises(ValueError, match="auto_restart_mcp"):
        cfg.Config.load()
    assert mcp_shim.enabled() is False


def test_shim_is_skipped_for_peers_and_uninstalled_launches(env, monkeypatch):
    monkeypatch.setattr(cfg, "_LAUNCHED_FROM", "/some/build/bin/agent-bus")
    assert mcp_shim.wanted()
    monkeypatch.setenv("AGENT_BUS_PEER", "j-1")
    assert not mcp_shim.wanted()
    monkeypatch.delenv("AGENT_BUS_PEER")
    monkeypatch.setenv(mcp_shim.SESSION_ENV, "s-1")  # already the shim's child
    assert not mcp_shim.wanted()
    monkeypatch.delenv(mcp_shim.SESSION_ENV)
    monkeypatch.setattr(cfg, "_LAUNCHED_FROM", None)  # python -m agent_bus, tests
    assert not mcp_shim.wanted()


def test_wrapped_build_sees_upgrades_when_argv0_was_rewritten(monkeypatch, tmp_path):
    """Nix's wrapped script sets sys.argv[0] to bin/agent-bus; the interpreter's original
    argv still names bin/.agent-bus-wrapped, and the build's own bin dir (first on the
    wrapper's PATH) must not hide the profile's newer build."""
    def install(name):
        bin_dir = tmp_path / name / "bin"
        bin_dir.mkdir(parents=True)
        for exe in ("agent-bus", ".agent-bus-wrapped"):
            (bin_dir / exe).write_text("#!/bin/sh\n")
            (bin_dir / exe).chmod(0o755)
        return bin_dir

    old, new = install("old"), install("new")
    profile = tmp_path / "profile" / "bin"
    profile.mkdir(parents=True)
    (profile / "agent-bus").symlink_to(new / "agent-bus")
    monkeypatch.setenv("PATH", f"{old}:{profile}")
    monkeypatch.setattr(sys, "argv", [str(old / "agent-bus"), "mcp"])
    monkeypatch.setattr(sys, "orig_argv", ["/usr/bin/python3", str(old / ".agent-bus-wrapped"), "mcp"])
    monkeypatch.setattr(cfg, "_LAUNCHED_FROM", cfg._launched_from())
    assert cfg.own_wrapped_bin() == os.path.realpath(old)
    assert cfg.stale_build()["on_path"] == os.path.realpath(new / "agent-bus")


def test_children_get_the_callers_path_when_the_wrapper_recorded_it(env, monkeypatch, tmp_path):
    """The Nix wrapper exports the caller's PATH before changing it: each child starts with
    exactly that, so a caller-supplied jj survives a move wherever it sits."""
    seen = {}

    class Fake:
        def __init__(self, exe, args, env, on_line, on_eof):
            seen.update(env)

    monkeypatch.setattr(mcp_shim, "Child", Fake)
    monkeypatch.setenv("PATH", "/nix/store/x-agent-bus/bin:/nix/store/jj/bin:/usr/bin:/nix/store/git/bin")
    monkeypatch.setenv("AGENT_BUS_CALLER_PATH", "/nix/store/jj/bin:/usr/bin")
    mcp_shim.Shim(["--agent", "claude"], "s-1", "/x/agent-bus")
    assert seen["PATH"] == "/nix/store/jj/bin:/usr/bin" and seen[mcp_shim.SESSION_ENV] == "s-1"

