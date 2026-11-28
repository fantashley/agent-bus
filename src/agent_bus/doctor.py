"""`agent-bus doctor`: a passive look at what a headless peer turn would run (issue #32,
stage 2A, agreed on agent-bus thread t-606fcd, P4).

It runs no agent turn and contacts nothing: no `codex mcp list`, no `copilot mcp list`, no
network.  Per participant it shows the executable a turn would launch and its version (the
cached `--version` probe `agents` uses too), the adapter recipe, and the isolation flags a turn
asks for.  Write and MCP isolation are reported as *inconclusive*: a requested flag isn't
evidence that the CLI enforces it.  Only a controlled live test with a positive control can
show that, and it ships later with `doctor --live` and the launch gate.  A check that fails
here (one a turn's own pre-launch checks would refuse) is reported separately.

Codex's MCP servers are read from its configuration files, never from the CLI.  When a
source Codex could load can't be accounted for (a file that can't be read, configuration
that outranks agent-bus's overrides and isn't interpreted), the inventory is marked
incomplete, and so ineligible for the future launch gate.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from . import peers
from .provenance import printable
from .config import Config
from .peers import (ADAPTERS, CODEX_FEATURES_OFF, PeerSetupError, _codex_overrides, _codex_split_profile,
                    _codex_value, _config_key, _repo_dirs, cli_identity)

INCONCLUSIVE = "inconclusive"
FAILED = "failed"
WAIVED = "waived"

# Why a requested flag isn't evidence.
WHY_INCONCLUSIVE = ("no controlled test with a positive control has shown the CLI "
                    "enforcing it for this launch configuration")

# Codex configuration files outside CODEX_HOME (codex 0.172.0's config loader).  The system
# config.toml is read like the user's.  managed_config.toml and requirements.toml are a
# managed layer and constraints that outrank agent-bus's `-c` overrides (they could turn a
# server or plugins back on), and doctor doesn't interpret them.
CODEX_SYSTEM_CONFIG = Path("/etc/codex/config.toml")
CODEX_MANAGED = (Path("/etc/codex/managed_config.toml"), Path("/etc/codex/requirements.toml"))
# macOS managed preferences (MDM) are another managed layer doctor can't read.
MACOS = sys.platform == "darwin"
# Codex 0.172.0 also loads a workspace-managed config bundle for a ChatGPT sign-in, from a
# cache or fetched at launch, and it can define MCP servers.  Ruling it out would mean reading
# the sign-in, which this account-free check doesn't, so it is always unaccounted for.
CODEX_CLOUD_CONFIG = ("workspace-managed cloud config (loaded for a ChatGPT sign-in, possibly fetched at launch) "
                      "can't be ruled out without reading the sign-in")


def _regular_file(f: Path, unaccounted: list[str], label: str) -> bool:
    """Whether ``f`` is a regular file to read; anything else there, or a path that can't be
    checked, is a source that isn't accounted for."""
    import stat
    try:
        st = f.stat()
    except FileNotFoundError:
        return False
    except OSError as e:
        unaccounted.append(f"{label} {f} can't be checked ({type(e).__name__})")
        return False
    if not stat.S_ISREG(st.st_mode):
        unaccounted.append(f"{label} {f} isn't a regular file")
        return False
    return True


def codex_home(ad: peers.CodexAdapter) -> str | None:
    """The CODEX_HOME a turn of ``ad``'s participant runs with, as its launch environment gives
    it (its configured CODEX_HOME or env, else the worker's); None when that environment has
    neither CODEX_HOME nor HOME."""
    if ad.home:
        return str(ad.home)
    home = ad.config.effective_env(ad.participant, "CODEX_HOME")
    if home:
        return home
    user = ad.config.effective_env(ad.participant, "HOME")
    return os.path.join(user, ".codex") if user else None


# A `-c` key doctor can read: a plain dotted path, no quoting, no empty segment.
_BARE_PATH = re.compile(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*")
# Top-level keys a `-c` override may set that change which MCP servers load, and that doctor
# doesn't model.
_CODEX_UNMODELLED = ("profile", "profiles", "project_root_markers", "features")


def _name(value: object) -> str:
    return value if isinstance(value, str) else repr(value)


def codex_mcp_inventory(ad: peers.CodexAdapter, repo: str) -> dict:
    """The MCP servers a Codex turn in ``repo`` could load, from configuration files only.

    Read: the system config, the user config in CODEX_HOME, each project ``.codex/config.toml``
    from the repository root down to ``repo``, and the participant's configured ``-c``
    overrides.  A project file loads only if Codex trusts the project, which doctor can't tell:
    its servers are listed as conditional.  Whether a server is enabled isn't modelled: a turn
    switches every listed server off by name.
    ``complete`` is False, with a reason in ``unaccounted``, for any source doctor can't
    account for: what it doesn't model is never taken as absent."""
    import tomllib
    home = codex_home(ad)
    sources: list[dict] = []
    unaccounted: list[str] = [CODEX_CLOUD_CONFIG] if CODEX_CLOUD_CONFIG else []
    if MACOS:
        unaccounted.append("macOS managed preferences (MDM) for Codex aren't read")
    files = [("system config", CODEX_SYSTEM_CONFIG)]
    if home is None:
        unaccounted.append("neither CODEX_HOME nor HOME is set for the turn, so its user config is unknown")
    elif not os.path.isabs(home):
        unaccounted.append(f"CODEX_HOME {home!r} is relative, so which config.toml loads depends on the turn's "
                           "directory")
    else:
        files.append(("user config", Path(home) / "config.toml"))
    try:
        files += [("project config", d / ".codex" / "config.toml") for d in reversed(_repo_dirs(repo))]
    except OSError as e:
        unaccounted.append(f"the checkout's project config files can't be found ({type(e).__name__})")
    layers: list[tuple[str, dict]] = []
    for label, f in files:
        if not _regular_file(f, unaccounted, label):
            continue
        try:
            data = tomllib.loads(f.read_text())
        except (OSError, UnicodeError, tomllib.TOMLDecodeError) as e:
            sources.append({"source": label, "path": str(f), "state": "unreadable"})
            unaccounted.append(f"{label} {f} can't be read ({type(e).__name__})")
            continue
        sources.append({"source": label, "path": str(f),
                        "state": "read (loads only in a trusted project)" if label == "project config" else "read"})
        layers.append((label, data))
    for f in CODEX_MANAGED:
        if _regular_file(f, unaccounted, "managed config"):
            sources.append({"source": "managed", "path": str(f), "state": "not interpreted"})
            unaccounted.append(f"{f} outranks agent-bus's overrides and isn't interpreted by doctor")

    servers: dict[str, dict] = {}

    def add(label: str, table: object) -> None:
        if not isinstance(table, dict):
            unaccounted.append(f"`mcp_servers` in the {label} isn't a table")
            return
        trusted = label != "project config"
        for name, spec in table.items():
            if not isinstance(spec, dict):
                unaccounted.append(f"MCP server {_name(name)!r} in the {label} isn't a table")
                continue
            entry = servers.setdefault(name, {"name": name, "sources": [], "trusted": set(), "project": [],
                                              "definite": False})
            if label not in entry["sources"]:
                entry["sources"].append(label)
            entry["definite"] |= trusted
            fields = {field for field in ("command", "url") if field in spec}
            for field in fields:
                if not (isinstance(spec[field], str) and spec[field]):
                    unaccounted.append(f"MCP server {_name(name)!r} in the {label}: `{field}` isn't a "
                                       "non-empty string")
            if trusted:
                entry["trusted"] |= fields
            else:  # each project file is trusted, or not, on its own
                entry["project"].append(fields)
            if "enabled" in spec and not isinstance(spec["enabled"], bool):
                unaccounted.append(f"MCP server {_name(name)!r} in the {label}: `enabled` isn't true/false")

    for label, data in layers:
        if "mcp_servers" in data:
            add(label, data["mcp_servers"])
        # Another project root than .git (doctor's) changes which project files load.
        if "project_root_markers" in data:
            unaccounted.append(f"the {label} sets project_root_markers, which doctor doesn't model")

    # A profile (codex 0.172.0 reads CODEX_HOME/<name>.config.toml for it) isn't modelled.
    overrides = _codex_overrides(ad.extra_args())
    profile_args = _codex_split_profile(ad.extra_args())[0]
    profile = profile_args[1] if profile_args else None
    for _label, data in layers:
        if "profile" in data and profile is None:
            profile = data["profile"]
    if profile is not None:
        profile = str(profile)  # JSON-safe, whatever TOML type it was
        unaccounted.append(f"profile {profile!r} is selected, and doctor doesn't read profiles")

    # Of the configured `-c` overrides only `mcp_servers.<name>.<enabled|command|url>` is
    # modelled.  Any other override that could bear on MCP servers, or whose key doctor can't
    # read as a plain dotted path, is unaccounted for.
    for o in overrides:
        key = _config_key(o)
        parts = key.split(".")
        if not _BARE_PATH.fullmatch(key):
            unaccounted.append(f"a configured `-c {key}=...` override has a key doctor doesn't read")
        elif parts[0] in _CODEX_UNMODELLED:
            unaccounted.append(f"a configured `-c {key}=...` override sets {parts[0]}, which doctor doesn't model")
        elif parts[0] == "mcp_servers":
            if len(parts) != 3 or parts[2] not in ("enabled", "command", "url"):
                unaccounted.append(f"a configured `-c {key}=...` override sets MCP servers in a form doctor doesn't "
                                   "model")
            else:
                add("configured args", {parts[1]: {parts[2]: _codex_value(o)}})

    # Codex rejects a server without exactly one transport, enabled or not, and any subset of
    # the project files may load.  So: one transport in the trusted layers that no project file
    # adds to, or (a project-only server) the same single transport in every project file.
    for s in servers.values():
        if s["definite"]:
            ok = len(s["trusted"]) == 1 and all(f <= s["trusted"] for f in s["project"])
        else:
            ok = all(len(f) == 1 for f in s["project"]) and len(set().union(*s["project"])) == 1
        if not ok:
            unaccounted.append(f"MCP server {_name(s['name'])!r} doesn't have exactly one of command or url "
                               "with every combination of trusted project files, so Codex could reject it")
    # Plugins and apps bring servers of their own; every turn switches both features off.
    for feature in ("plugins", "apps"):
        if feature in CODEX_FEATURES_OFF:
            sources.append({"source": feature, "path": None,
                            "state": f"off for peer turns (-c features.{feature}=false)"})
    # Whether each is enabled isn't modelled: every turn switches all of them off by name.
    listed = [{"name": _name(s["name"]), "sources": s["sources"], "definite": s["definite"],
               "nameable": bool(peers._CODEX_KEY.fullmatch(_name(s["name"])))}
              for s in sorted(servers.values(), key=lambda s: _name(s["name"]))]
    return {"codex_home": home, "profile": profile, "sources": sources, "servers": listed,
            "complete": not unaccounted, "unaccounted": unaccounted, "gate_eligible": not unaccounted}


def _check(failed: list[dict], capability: str, name: str, fn) -> None:
    """Run one of a turn's own passive pre-launch checks; a refusal is a known failed check."""
    try:
        fn()
    except PeerSetupError as e:
        failed.append({"capability": capability, "check": name, "detail": str(e)})
    except OSError as e:  # the turn's own check would fail on it too
        failed.append({"capability": capability, "check": name,
                       "detail": f"can't be checked ({type(e).__name__}: {e.strerror or e})"})


def version(bus, command: str, repo: str) -> str:
    """The CLI's version: the cached `--version` probe's answer, else a probe of doctor's own,
    run from "/" (no checkout file can steer a cwd-sensitive wrapper) and not cached (so a
    wrapper that answers differently from the checkout doesn't spoil the cache the turns
    use).  An executable found through the checkout (a relative command or PATH entry) isn't
    run at all."""
    here = cli_identity(command, repo)
    if here is None:
        return "unknown"
    cached = bus.cli_version(here[1])
    if cached is not None:
        return cached
    if here != cli_identity(command, "/"):
        return "unknown (not probed: the executable is found through the checkout)"
    found = peers.probe_version(here[0], "/")
    return "unknown (the probe timed out)" if found is None else found


def inspect(bus, conf: Config, participant: str, repo: str) -> dict:
    """What doctor reports for one available participant, a turn of which runs in ``repo``."""
    harness = conf.harness_of(participant)
    ad = ADAPTERS[harness](conf, participant=participant, repo=repo)
    ident = cli_identity(ad.command, repo)
    out: dict = {"harness": harness,
                 "executable": ({"path": ident[0], "resolved": os.path.realpath(ident[0])} if ident else None),
                 "version": version(bus, ad.command, repo),
                 "recipe": f"{harness}/{ad.recipe}"}
    req = ad.isolation_request()
    failed: list[dict] = []
    write = {"requested": req["write"], "status": INCONCLUSIVE, "detail": WHY_INCONCLUSIVE}
    mcp = {"requested": req["mcp"], "status": INCONCLUSIVE, "detail": WHY_INCONCLUSIVE}
    if harness == "claude":
        if any(a == "--mcp-config" or a.startswith("--mcp-config=") for a in ad.extra_args()):
            mcp.update(status=WAIVED, detail="the configured args pass --mcp-config: its servers load in peer turns "
                                             "despite --strict-mcp-config (unsafe)")
    elif harness == "codex":
        _check(failed, "write", "checkout's Codex config", lambda: peers._codex_project_check(repo))
        inv = codex_mcp_inventory(ad, repo)
        mcp["inventory"] = inv
    elif harness == "antigravity":
        if conf.launch(participant).get("allow_mcp"):
            mcp.update(status=WAIVED, detail=f"participants.{participant}.allow_mcp is set: its configured MCP "
                                             "servers stay live in peer turns (unsafe)")
        else:
            mcp["note"] = ("agy has no per-session MCP switch: a turn is refused while any MCP server other than "
                           "agent-bus is configured")
            _check(failed, "mcp", "Antigravity MCP servers", ad.check_isolation)
        _check(failed, "mcp", "checkout's Antigravity hooks and MCP servers",
               lambda: peers._agy_workspace_check(ad.workspace_dirs(repo)))
    elif harness == "copilot":
        mcp["note"] = "its servers are listed with `copilot mcp list` at launch, which doctor doesn't run"
        _check(failed, "write", "checkout's Copilot hooks", lambda: peers._copilot_repo_hooks_check(repo))
    for f in failed:
        cap = write if f["capability"] == "write" else mcp
        if cap["status"] == INCONCLUSIVE:
            cap["status"] = FAILED
    out.update(write_isolation=write, mcp_isolation=mcp, failed_checks=failed)
    return out


def report(bus, conf: Config, repo: str, only: str | None = None) -> dict:
    """The whole doctor report: every configured participant (or just ``only``).  Without a
    config `agents` list, a participant is available when its CLI is found as a turn in
    ``repo`` would find it."""
    avail = conf.availability()
    if conf.agents is None:
        for name, v in avail.items():
            found = cli_identity(conf.peer_command(name), repo)
            v.update(available=found is not None, reason=f"found {found[0]}" if found
                     else f"`{conf.peer_command(name)}` not found on PATH")
    if only is not None:
        conf.participant(only)  # ValueError for a name nobody configured
        avail = {only: avail[only]}
    agents = {}
    for name, v in avail.items():
        if not v["available"]:
            agents[name] = {"available": False, "reason": v["reason"]}
            continue
        agents[name] = {"available": True, **inspect(bus, conf, name, repo)}
    return {"checkout": repo, "passive": True, "agents": agents,
            "failed": sum(len(a.get("failed_checks", [])) for a in agents.values())}


def format_report(rep: dict) -> str:
    lines = [f"agent-bus doctor (passive: no agent turns, no network) · checkout {rep['checkout']}",
             f"Isolation is shown as the flags a peer turn asks for, and is {INCONCLUSIVE}: {WHY_INCONCLUSIVE}.",
             ""]
    for name, a in rep["agents"].items():
        if not a["available"]:
            lines += [f"{name}: not available ({a['reason']})", ""]
            continue
        harness = f" (harness {a['harness']})" if a["harness"] != name else ""
        lines.append(f"{name}{harness}")
        exe = a["executable"]
        if exe is None:
            lines.append("  executable   not found")
        else:
            lines.append(f"  executable   {exe['path']}" + (f" -> {exe['resolved']}" if exe["resolved"] != exe["path"]
                                                            else ""))
        lines.append(f"  version      {a['version']}")
        lines.append(f"  recipe       {a['recipe']}")
        for label, cap in (("write", a["write_isolation"]), ("mcp", a["mcp_isolation"])):
            lines.append(f"  {label:<12} {cap['status'].upper()}: "
                         + (", ".join(cap["requested"]) if cap["requested"] else "no flag"))
            for extra in (cap.get("note"), None if cap["detail"] == WHY_INCONCLUSIVE else cap["detail"]):
                if extra:
                    lines.append(f"               {extra}")
        inv = a["mcp_isolation"].get("inventory")
        if inv is not None:
            state = "complete" if inv["complete"] else "INCOMPLETE, not eligible for the launch gate"
            lines.append(f"  mcp servers  from config files ({state}):")
            if inv["servers"]:
                for s in inv["servers"]:
                    lines.append(f"               {s['name']} ({', '.join(s['sources'])}"
                                 + ("" if s["definite"] else "; loads only if the project is trusted")
                                 + ("" if s["nameable"] else "; its name can't be written in a -c override, so a turn is refused if it's enabled")
                                 + ")")
            else:
                lines.append("               none configured")
            for src in inv["sources"]:
                lines.append(f"               source: {src['source']}"
                             + (f" {src['path']}" if src["path"] else "") + f": {src['state']}")
            for why in inv["unaccounted"]:
                lines.append(f"               unaccounted: {why}")
        for f in a["failed_checks"]:
            lines.append(f"  FAILED       {f['capability']}: {f['check']}: {f['detail']}")
        lines.append("")
    # Names, paths and details can come from a checkout's own files: no terminal controls.
    return "\n".join(printable(line) for line in lines).rstrip() + "\n"
