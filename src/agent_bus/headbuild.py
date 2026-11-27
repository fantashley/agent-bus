"""A review job run by the head's own build, for a repository that opts in (#181).

In a repository listed in ``self_hosted_reviews`` (never the older, prompts-only
``self_hosted_prompts``), the installed worker claims a review job as usual and then hands
the whole job to the agent-bus build of the head under review: it builds the head's flake from its pinned read-only snapshot
(``nix build path:<snapshot>#default^out``, cached per head commit), checks that build speaks this
hand-off protocol, and runs its ``agent-bus run-job`` as a child under the installed worker's
lease.  The installed worker stays the supervisor: it keeps the lease alive, stays the job's
recorded process (so ``agent-bus cancel`` reaches both), and reports a child that dies.

The shared database must never be migrated by the head: if the head's ``db.py`` defines a
different schema or migrations (compared with ``ast``, never run), the installed build runs
the job.  So it does when the head can't be built in time, isn't an agent-bus flake, or its
build doesn't speak ``PROTOCOL``, each with the reason in the prompt and in a ``prompts`` event.

The hand-off contract (protocol 2), between the installed build and the head's:
- ``<head build>/bin/agent-bus self-host-info`` prints ``{"protocol": 2}`` without opening the
  database or the config.
- ``<head build>/bin/agent-bus run-job <job>`` with ``AGENT_BUS_HANDOFF="<job> <owner> <head>"``
  in its environment runs that job under the supervisor's lease ``owner`` instead of claiming
  it, opens the database without creating or migrating anything, and leaves a job it fails
  on an unexpected error running, for the supervisor to report.
- Every message it posts for the job carries ``job:<job>`` as its session (#185): the
  supervisor counts only those, and the job's ``turn_start`` events, as the head's worker
  having started.  (Protocol 1's workers posted their notices unattributed.)
"""

from __future__ import annotations

import ast
import fcntl
import hashlib
import json
import os
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable

from . import config as cfg

PROTOCOL = 2
ENV = "AGENT_BUS_HANDOFF"
DB_PATH = "src/agent_bus/db.py"
MAX_BYTES = 512 * 1024
# What a database's shape depends on: the tables and indexes every open creates, the table
# only a migration creates, the migrations themselves, the constructor that runs them, the
# connection wrapper it builds first and the guard a head's worker opens it with (any edit
# to one counts).
SCHEMA_NAMES = ("SCHEMA", "CLI_VERSION_TIMEOUTS", "_SCHEMA_ACTIONS", "_no_schema_changes", "_LockedConn")
BUS_NAMES = ("SESSION_COLUMNS", "__init__", "_needs_migration", "_migrate")
PROBE_TIMEOUT_S = 30
KEEP_S = 7 * 24 * 3600  # an unused head build's cache entry (a GC root) is dropped after this


class Unusable(Exception):
    """Why the head's build can't run this job: a short reason, for the prompt and an event."""


# ------------------------------------------------------------ the database's shape


def schema_fingerprint(source: str) -> str:
    """A hash of the schema and migrations ``db.py``'s ``source`` defines (comments and
    layout don't count).  Raises Unusable when they can't be found."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError) as e:
        raise Unusable(f"its {DB_PATH} is not valid Python") from e
    found: dict[str, str] = {}

    def note(node: ast.stmt, names: tuple) -> None:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names:
            found[node.name] = ast.dump(node)
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) \
                and node.targets[0].id in names:
            found[node.targets[0].id] = ast.dump(node.value)

    for node in tree.body:
        note(node, SCHEMA_NAMES)
        if isinstance(node, ast.ClassDef) and node.name == "Bus":
            for item in node.body:
                note(item, BUS_NAMES)
    missing = [n for n in (*SCHEMA_NAMES, *BUS_NAMES) if n not in found]
    if missing:
        raise Unusable(f"its {DB_PATH} has no {', '.join(missing)}")
    text = "\n".join(f"{n}={found[n]}" for n in (*SCHEMA_NAMES, *BUS_NAMES))
    return hashlib.sha256(text.encode()).hexdigest()


def installed_fingerprint() -> str:
    from . import db
    return schema_fingerprint(Path(db.__file__).read_text())


def _read(snap, path: str) -> str:
    if path not in snap.files:
        raise Unusable(f"it has no {path}")
    try:
        fd = os.open(os.path.join(snap.path, path), os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as f:
            if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                raise Unusable(f"its {path} is not a regular file")
            data = f.read(MAX_BYTES + 1)
    except OSError as e:
        raise Unusable(f"its {path} could not be read") from e
    if len(data) > MAX_BYTES:
        raise Unusable(f"its {path} is over {MAX_BYTES // 1024} KiB")
    return data.decode("utf-8", "replace")


def check_package(out: str) -> None:
    """Raise Unusable unless the build at ``out`` packages a db.py with the installed
    schema and migrations: what runs, not only the source it was built from."""
    found = sorted(Path(out).glob("lib/python*/site-packages/agent_bus/db.py"))
    if len(found) != 1:
        raise Unusable("its build has no single agent_bus/db.py")
    try:
        source = found[0].read_text(errors="replace")
    except OSError as e:
        raise Unusable(f"its build's agent_bus/db.py could not be read ({e})") from None
    if schema_fingerprint(source) != installed_fingerprint():
        raise Unusable("its build's database schema or migrations differ from the installed build's")


def check(snap) -> None:
    """Raise Unusable unless the head's full read-only copy ``snap`` is an agent-bus flake
    whose database schema and migrations are the installed build's."""
    if snap is None or not snap.path or snap.mode != "full":
        why = snap.reason if snap is not None and snap.reason else f"it is {getattr(snap, 'mode', 'missing')}"
        raise Unusable(f"there is no full read-only copy of the head ({why})")
    if "flake.nix" not in snap.files:
        raise Unusable("it has no flake.nix")
    if schema_fingerprint(_read(snap, DB_PATH)) != installed_fingerprint():
        raise Unusable("it changes the database schema or its migrations, and the shared database is "
                       "never migrated by a head")


# ------------------------------------------------------------ predicted at request time


SELF_PATH = "src/agent_bus/headbuild.py"
PREDICTED = ("flake.nix", SELF_PATH, DB_PATH)
PREDICT_TIMEOUT_S = 5  # in all: the prediction is optional, and request_review waits on it


def protocol_of(source: str) -> int | None:
    """The hand-off protocol a head's ``headbuild.py`` ``source`` defines: its one top-level
    binding of ``PROTOCOL``, a plain ``PROTOCOL = <int>``, read with ``ast`` (never run).
    None if there's no such literal, or anything else at the top level binds it too."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return None
    found = []
    for node in tree.body:
        targets = (node.targets if isinstance(node, ast.Assign) else
                   [node.target] if isinstance(node, (ast.AnnAssign, ast.AugAssign)) else [])
        if any(isinstance(n, ast.Name) and n.id == "PROTOCOL" for t in targets for n in ast.walk(t)):
            found.append(node)
    if len(found) != 1 or not isinstance(found[0], ast.Assign) or len(found[0].targets) != 1 \
            or not isinstance(found[0].targets[0], ast.Name):
        return None
    value = found[0].value
    return value.value if isinstance(value, ast.Constant) and type(value.value) is int else None


def _head_files(repo: str, head: str) -> dict[str, str | None] | None:
    """What git says about ``PREDICTED`` at ``head``, each path on its own: None for one the
    commit doesn't have, its text for headbuild.py and db.py ("" for flake.nix, never read),
    and no entry for one it can't tell about (not fetched in a partial clone, not a regular
    file, over MAX_BYTES).  None if the commit's tree can't be read.  All of it within
    PREDICT_TIMEOUT_S, with no lazy fetching."""
    deadline = time.monotonic() + PREDICT_TIMEOUT_S

    def run(argv: list[str], env: dict | None = None, stdin: bytes | None = None) -> bytes | None:
        left = deadline - time.monotonic()
        if left <= 0:
            return None
        try:
            proc = subprocess.run(argv, cwd=repo, env=env, input=stdin, capture_output=True, timeout=left)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return proc.stdout if proc.returncode == 0 else None

    # snapshot._git_env's choice, under this deadline: its `jj git root` may take a minute.
    env = dict(os.environ, GIT_NO_REPLACE_OBJECTS="1", GIT_NO_LAZY_FETCH="1")
    if not (os.path.exists(os.path.join(repo, ".git")) or cfg.vcs(repo) == "git"):
        if cfg.vcs(repo) != "jj":
            return None
        root = run(["jj", "git", "root", "--ignore-working-copy"])
        if not root or not root.strip():
            return None
        env["GIT_DIR"] = root.decode("utf-8", "replace").strip()
    git = lambda *argv, stdin=None: run(["git", "--no-replace-objects", *argv], env, stdin)  # noqa: E731

    # Membership from the commit's trees (antigravity-1): in a partial clone a blob can be
    # missing locally although the commit has it, which only means "can't tell".
    out = git("ls-tree", "-r", "-z", "--full-tree", f"{head}^{{tree}}", "--", *PREDICTED)
    if out is None:
        return None
    entries: dict[str, tuple[str, str]] = {}
    for rec in out.split(b"\0"):
        meta, _, path = rec.decode("utf-8", "replace").partition("\t")
        parts = meta.split()
        if len(parts) == 3:
            entries[path] = (parts[0], parts[2])
    files: dict[str, str | None] = {p: None for p in PREDICTED if p not in entries}
    if "flake.nix" in entries:
        files["flake.nix"] = ""
    for path in (SELF_PATH, DB_PATH):
        if path not in entries or entries[path][0] not in ("100644", "100755"):
            continue
        oid = entries[path][1]
        rec = git("cat-file", "--batch-check=%(objecttype) %(objectsize)", stdin=f"{oid}\n".encode())
        parts = rec.decode().split() if rec is not None else []
        if len(parts) != 2 or parts[0] != "blob" or not parts[1].isdigit() or int(parts[1]) > MAX_BYTES:
            continue  # not fetched here (missing), or too big to read
        data = git("cat-file", "blob", oid)
        if data is not None:
            files[path] = data.decode("utf-8", "replace")
    return files


def predict(repo: str, head: str) -> str | None:
    """Why the head's own build won't run a review of ``head`` in ``repo``, as far as its
    files at that commit tell (#188): no flake.nix, another hand-off protocol, or another
    database schema.  The files are read from git and parsed with ``ast``, never run.  None
    when nothing rules the head's build out (or git can't say, or a file can't be parsed):
    its build and checks can still fail.  Never raises."""
    try:
        files = _head_files(repo, head)
        if files is None:
            return None
        if "flake.nix" in files and files["flake.nix"] is None:
            return "it has no flake.nix"
        if files.get(SELF_PATH) is not None:
            theirs = protocol_of(files[SELF_PATH])
            if theirs is not None and theirs != PROTOCOL:
                return f"its {SELF_PATH} sets hand-off protocol {theirs}, the installed build's is {PROTOCOL}"
        if DB_PATH not in files:
            return None
        if files[DB_PATH] is None:
            return f"it has no {DB_PATH}"
        try:
            if schema_fingerprint(files[DB_PATH]) != installed_fingerprint():
                return "it changes the database schema or its migrations"
        except Unusable as e:
            return str(e)
        return None
    except Exception:  # noqa: BLE001 - optional: a parser or formatting limit only means "can't tell"
        return None


# ------------------------------------------------------------ building it


def cache_dir() -> Path:
    return cfg.state_dir() / "head-builds"


def _ready(link: Path) -> str | None:
    """The store path ``link`` points to, when it holds an executable bin/agent-bus."""
    if not link.is_symlink():
        return None
    out = os.path.realpath(link)
    return out if os.access(os.path.join(out, "bin", "agent-bus"), os.X_OK) else None


def find_nix() -> str | None:
    return shutil.which("nix")


def _lock(path: Path, op: int, timeout: float, cancelled: Callable[[], bool], what: str) -> int:
    """``path`` opened and flocked with ``op``, waiting up to ``timeout`` seconds.  Raises
    Unusable on the timeout or ``cancelled()``."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, op | fcntl.LOCK_NB)
                fd, held = None, fd
                return held
            except BlockingIOError:
                if cancelled():
                    raise Unusable("the review was cancelled") from None
                if time.monotonic() >= deadline:
                    raise Unusable(f"{what} took over {int(timeout)} s") from None
                time.sleep(0.5)
    finally:
        if fd is not None:
            os.close(fd)


def build(snap, head: str, timeout: float, cancelled: Callable[[], bool],
          building: Callable[[], None] = lambda: None) -> tuple[str, int]:
    """The head's build (its store path), from the cache or built now within ``timeout``
    seconds, and a shared hold on its cache entry: no prune removes it (its GC root) until
    the caller closes that descriptor.  One build per head at a time; ``building()`` is
    called once when the head isn't in the cache yet, before waiting for another build of
    it or starting one.  Raises Unusable (also when ``cancelled()``)."""
    nix = find_nix()
    if not nix:
        raise Unusable("nix is not on PATH")
    top = cache_dir()
    top.mkdir(parents=True, exist_ok=True, mode=0o700)
    link = top / head
    deadline = time.monotonic() + timeout
    said = not _ready(link)
    if said:
        building()  # before the lock: another worker's build of it may take minutes (#188)
    fd = _lock(top / f".{head}.lock", fcntl.LOCK_EX, timeout, cancelled, "another build of it")
    try:
        out = _ready(link)
        if out:
            os.utime(link, follow_symlinks=False)
        else:
            if not said:
                building()  # pruned while this waited for the lock
            # Straight to the cache entry, never renamed: nix's own GC root points at this path.
            nix_build(nix, snap.path, link, deadline - time.monotonic(), cancelled)
            out = _ready(link)
            if not out:
                if link.is_symlink():
                    link.unlink()  # not a build worth keeping a GC root for
                raise Unusable("its build has no bin/agent-bus")
        use = _lock(top / f".{head}.use", fcntl.LOCK_SH, 10, cancelled, "a prune of it")
    finally:
        os.close(fd)
    prune(keep=head)
    return out, use


def nix_build(nix: str, src: str, out_link: Path, timeout: float, cancelled: Callable[[], bool]) -> None:
    """``nix build`` the flake at ``src`` (never writing its lock file) to ``out_link``, in
    its own process group, stopped on ``timeout`` or ``cancelled()``.  Raises Unusable.

    Only the package's ``out`` output (#185): one built with several outputs to install
    (``meta.outputsToInstall``) would also get suffixed links (``out_link-dist``), GC roots
    the cache's entry, refresh and prune know nothing about."""
    if timeout <= 0:
        raise Unusable("there was no time left to build it")
    with tempfile.TemporaryFile() as err:
        proc = subprocess.Popen([nix, "build", "--no-write-lock-file", "--no-update-lock-file",
                                 "--out-link", str(out_link), f"path:{src}#default^out"],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err,
                                start_new_session=True)
        end = time.monotonic() + timeout
        why = None
        while proc.poll() is None:
            if cancelled():
                why = "the review was cancelled"
            elif time.monotonic() >= end:
                why = f"its build took over {int(timeout)} s"
            if why:
                stop(proc)
                raise Unusable(why)
            time.sleep(0.5)
        if proc.returncode != 0:
            err.seek(0)
            raise Unusable(f"its build failed ({error_line(err.read()[-65536:])})")


def stop(proc: subprocess.Popen, grace: float = 10) -> None:
    """SIGTERM ``proc``'s process group, then SIGKILL it after ``grace`` seconds."""
    for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 5)):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            proc.wait(wait)
            return
        except subprocess.TimeoutExpired:
            continue


def error_line(data: bytes) -> str:
    """What nix said went wrong: its first ``error:`` line (the rest is the builder's log
    tail and where the full log is), else its last line."""
    for ln in data.decode("utf-8", "replace").splitlines():
        if ln.strip().startswith("error:"):
            return last_line(ln.encode())
    return last_line(data)


def last_line(data: bytes) -> str:
    """The last non-empty line of ``data``, shortened: what a failed process said last."""
    lines = [ln.strip() for ln in data.decode("utf-8", "replace").splitlines() if ln.strip()]
    text = lines[-1] if lines else "no output"
    return text if len(text) <= 300 else text[:297] + "..."


def prune(keep: str) -> None:
    """Drop the cache entries (GC roots) of head builds unused for KEEP_S, except ``keep``
    and any being built or used right now (their locks are held).  Best effort."""
    top = cache_dir()
    try:
        entries = [p for p in top.iterdir() if not p.name.startswith(".") and p.name != keep]
    except OSError:
        return
    for p in entries:
        fds = []
        try:
            for kind in ("lock", "use"):
                fds.append(os.open(top / f".{p.name}.{kind}", os.O_RDWR | os.O_CREAT, 0o600))
                fcntl.flock(fds[-1], fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Checked under the locks: a cache hit refreshes it under the build lock.
            if p.is_symlink() and time.time() - p.lstat().st_mtime >= KEEP_S:
                p.unlink()
        except OSError:
            pass
        finally:
            for fd in fds:
                os.close(fd)


# ------------------------------------------------------------ handing the job over


def child_env(handoff: str | None = None) -> dict[str, str]:
    """The head build's worker's environment: the caller's PATH from before this build's
    Nix wrapper (the head's wrapper adds its own), the installed build for anything it
    starts (a waiter, a relaunched job), and the hand-off ("<job> <owner> <head>")."""
    from .mcp_shim import without_wrapper_dirs
    env = dict(os.environ)
    caller = env.get("AGENT_BUS_CALLER_PATH")
    # Stripped either way: a worker spawned from a wrapped agent-bus records an already
    # wrapped PATH as its caller's.
    env["PATH"] = without_wrapper_dirs(caller if caller is not None else env.get("PATH", ""),
                                       cfg.wrapper_path_dirs())
    me = cfg.self_command()
    if len(me) != 1:
        raise Unusable("the installed agent-bus isn't one executable the head's worker could start "
                       "things with")
    env["AGENT_BUS_BIN"] = me[0]
    env.pop(ENV, None)
    if handoff:
        env[ENV] = handoff
    return env


def probe(out: str) -> None:
    """Raise Unusable unless the build at ``out`` speaks hand-off ``PROTOCOL``."""
    cfg.state_dir().mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        res = subprocess.run([os.path.join(out, "bin", "agent-bus"), "self-host-info"],
                             env=child_env(), stdin=subprocess.DEVNULL,
                             capture_output=True, timeout=PROBE_TIMEOUT_S, cwd=str(cfg.state_dir()))
    except subprocess.TimeoutExpired:
        raise Unusable(f"its `agent-bus self-host-info` took over {PROBE_TIMEOUT_S} s") from None
    except OSError as e:
        raise Unusable(f"its agent-bus could not be started ({e})") from None
    try:
        got = json.loads(res.stdout.decode("utf-8", "replace").strip().splitlines()[-1])
        protocol = got.get("protocol")
        if res.returncode != 0:
            raise ValueError(res.returncode)
    except (ValueError, IndexError, AttributeError):
        raise Unusable("it doesn't speak the hand-off protocol (its `agent-bus self-host-info` failed: "
                       f"{last_line(res.stderr[-8192:] or res.stdout[-8192:])})") from None
    if protocol != PROTOCOL:
        raise Unusable(f"it speaks hand-off protocol {protocol!r}, not {PROTOCOL}")


def take_handoff(job_id: str) -> tuple[str, str] | None:
    """The (lease owner, head commit) this process was handed ``job_id`` with, if any.  The
    variable is removed, so nothing this worker starts sees it."""
    raw = os.environ.pop(ENV, None)
    parts = (raw or "").split()
    if len(parts) != 3 or parts[0] != job_id:
        return None
    return parts[1], parts[2]


def info() -> str:
    return json.dumps({"protocol": PROTOCOL})
