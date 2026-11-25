"""Read-only snapshots of a review's pinned head (#111).

A reviewer reads files from the repository's working copy, which may have moved on from the
commit under review.  Peers with a shell get git/jj commands for the pinned commits (#93,
#76), but Antigravity has only read_file and Copilot only view/grep/glob.  So before a review
turn the worker writes the head's tree into a directory of agent-bus's own state, which every
peer can read (an added directory, or Codex's read-only sandbox, which reads everywhere):

    <state>/snapshots/<thread id>/<head commit id>/   the files, read-only
    <state>/snapshots/<thread id>/<head commit id>.json   what is in it

The files come from ``git cat-file --batch``: exactly the commit's blobs, with no checkout
filters, export-subst or other conversions (``git archive`` applies those, and a filter is an
external command), and without untracked or ignored files such as node_modules.  A snapshot
is reused by the pass's later turns on the same head, replaced when the head moves, and
removed once no job runs on the thread (``sweep``, which every worker also runs when it
starts, so a crashed worker's snapshots don't stay).  A tree over the size cap gets a sparse
snapshot: the changed files and the other files in their directories; if even that is over,
no snapshot, and the prompt says files on disk may not match the head.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable

from . import config as cfg

# The most files a snapshot holds (the byte cap is config: max_snapshot_bytes).
MAX_FILES = 50_000
# Commit and thread ids name directories, so only plain ones do.
_ID = re.compile(r"[0-9A-Za-z_-]{1,128}")
# How long a turn waits for another worker's build of its thread's snapshot.
LOCK_WAIT_S = 60
# How long the git commands that read the tree may take.
GIT_TIMEOUT_S = 300


@dataclass
class Snapshot:
    """A pinned head's snapshot, or why there is none (``path`` None)."""
    head: str
    path: str | None = None
    # "full", "sparse" (the tree is over the cap: changed files and their directories only),
    # "over_cap" (even that is over: no snapshot) or "failed".
    mode: str = "failed"
    files: list[str] = field(default_factory=list)  # every path it holds, sorted
    changed: list[str] = field(default_factory=list)  # the change's files that exist at head
    tree_files: int = 0  # files in the head's whole tree
    skipped: int = 0  # symlinks out of the tree and submodules, left out
    reason: str = ""


def root() -> Path:
    """Per bus database: workers on another database (AGENT_BUS_DB) sharing the state
    directory never see, or sweep, this one's snapshots."""
    db = os.path.realpath(cfg.db_path())
    return cfg.state_dir() / "snapshots" / hashlib.sha256(os.fsencode(db)).hexdigest()[:16]


@contextmanager
def _locked(thread_id: str, wait: float | None = None, cancelled: Callable[[], bool] | None = None):
    """Holds the thread's snapshot lock: making or reusing a snapshot, and a sweep's decision
    to remove it with the removal itself, never interleave.  Yields False if someone else
    still holds it after ``wait`` seconds (0: a sweep skips a snapshot being built; a stuck
    old worker's build never blocks a retry's turn for long), or once ``cancelled()`` says
    the job waiting for it was cancelled, even if it got the lock (#113)."""
    locks = root() / ".locks"
    locks.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(locks / f"{thread_id}.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + (LOCK_WAIT_S if wait is None else wait)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline or (cancelled is not None and cancelled()):
                    yield False
                    return
                time.sleep(0.2)
        if cancelled is not None and cancelled():  # cancelled while it waited, or just as it got it
            yield False
            return
        yield True
    finally:
        os.close(fd)


def _git_env(repo: str) -> dict[str, str] | None:
    """The environment git reads ``repo``'s objects with, or None if it can't: a git repo or a
    colocated jj one as it is, a jj repo that isn't colocated through its backing git store.
    Replacement objects (refs/replace) are ignored, so the bytes are the pinned ids' own."""
    env = dict(os.environ, GIT_NO_REPLACE_OBJECTS="1")
    if os.path.exists(os.path.join(repo, ".git")) or cfg.vcs(repo) == "git":
        return env
    if cfg.vcs(repo) != "jj":
        return None
    try:
        out = subprocess.run(["jj", "git", "root", "--ignore-working-copy"], cwd=repo, capture_output=True,
                             text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    env["GIT_DIR"] = out.stdout.strip()
    return env


def _git(repo: str, env: dict, *argv: str) -> bytes | None:
    try:
        out = subprocess.run(["git", *argv], cwd=repo, env=env, capture_output=True, timeout=GIT_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout if out.returncode == 0 else None


@dataclass
class _Entry:
    mode: str
    kind: str
    oid: str
    size: int
    path: str


def _tree(repo: str, env: dict, head: str) -> list[_Entry] | None:
    out = _git(repo, env, "ls-tree", "-r", "-l", "-z", "--full-tree", f"{head}^{{tree}}")
    if out is None:
        return None
    entries = []
    for rec in out.split(b"\0"):
        if not rec:
            continue
        meta, _, path = rec.partition(b"\t")
        parts = meta.split()
        if len(parts) != 4:
            return None
        mode, kind, oid, size = (p.decode() for p in parts)
        entries.append(_Entry(mode, kind, oid, int(size) if size.isdigit() else 0, os.fsdecode(path)))
    return entries


def _safe(path: str) -> bool:
    parts = path.split("/")
    return bool(path) and not path.startswith("/") and all(
        p not in ("", ".", "..") and p.lower() != ".git" and "\0" not in p for p in parts)


def _parent(path: str) -> str:
    return str(PurePosixPath(path).parent) if "/" in path else ""


def _inside(path: str, target: str) -> bool:
    """Whether symlink ``path`` -> ``target`` stays inside the snapshot."""
    if not target or target.startswith("/") or "\0" in target:
        return False
    norm = os.path.normpath(os.path.join(_parent(path), target))
    return not (norm == ".." or norm.startswith("../") or norm.startswith("/"))


def ensure(repo: str, thread_id: str, base: str, head: str, max_bytes: int,
           cancelled: Callable[[], bool] | None = None) -> Snapshot:
    """The snapshot of ``head`` for ``thread_id``, made now or reused.  Never raises for a
    repository problem: a Snapshot without a path says why there is none.  ``cancelled``
    stops a wait for another worker's lock early."""
    snap = Snapshot(head=head)
    if not (_ID.fullmatch(head) and _ID.fullmatch(thread_id) and _ID.fullmatch(base)):
        snap.reason = "the base, head or thread id can't name a directory"
        return snap
    if max_bytes <= 0:
        snap.mode, snap.reason = "over_cap", "snapshots are turned off (max_snapshot_bytes is 0)"
        return snap
    with _locked(thread_id, cancelled=cancelled) as mine:
        if not mine:
            snap.reason = ("the review was cancelled" if cancelled is not None and cancelled()
                           else "another worker is still writing it")
            return snap
        return _ensure(repo, thread_id, base, head, max_bytes, snap)


def _ensure(repo: str, thread_id: str, base: str, head: str, max_bytes: int, snap: Snapshot) -> Snapshot:
    tdir = root() / thread_id
    final, meta = tdir / head, tdir / f"{head}.json"
    if final.is_dir() and meta.is_file():
        try:
            data = json.loads(meta.read_text())
            # Reused only for the same base (a sparse one's directories, and the changed
            # files, depend on it) and while it still fits the cap.
            # A sparse one only while the whole tree is still over the cap.
            if (data["base"] == base and data["bytes"] <= max_bytes
                    and (data["mode"] == "full" or data["tree_bytes"] > max_bytes
                         or data["tree_files"] > MAX_FILES)):
                return Snapshot(head=head, path=str(final), mode=data["mode"], files=data["files"],
                                changed=data["changed"], tree_files=data["tree_files"], skipped=data["skipped"])
        except (OSError, ValueError, KeyError, TypeError):
            pass  # rebuilt below
    tdir.mkdir(parents=True, exist_ok=True, mode=0o700)
    # One head per thread: an earlier head's snapshot, or a crashed build's leftovers, go.
    for p in tdir.iterdir():
        remove(p)
    env = _git_env(repo)
    entries = _tree(repo, env, head) if env is not None else None
    if entries is None:
        snap.reason = "git could not read the head commit's tree"
        return snap
    blobs = [e for e in entries if e.kind == "blob" and _safe(e.path)]
    snap.skipped = len(entries) - len(blobs)
    snap.tree_files = len(blobs)
    paths = {e.path for e in blobs}
    names = _git(repo, env, "diff", "--name-only", "-z", "--no-renames", "--no-ext-diff", "--no-textconv",
                 f"{base}^{{commit}}", f"{head}^{{commit}}", "--")
    touched = [os.fsdecode(n) for n in (names or b"").split(b"\0") if n]
    snap.changed = sorted(p for p in touched if p in paths)

    def fits(chosen: list[_Entry]) -> bool:
        return len(chosen) <= MAX_FILES and sum(e.size for e in chosen) <= max_bytes

    chosen, snap.mode = blobs, "full"
    if not fits(chosen):
        dirs = {_parent(p) for p in touched}  # a deleted file's directory too
        chosen, snap.mode = [e for e in blobs if _parent(e.path) in dirs], "sparse"
        if names is None or not chosen or not fits(chosen):
            snap.mode = "over_cap"
            snap.reason = (f"the head's tree ({len(blobs)} files) is over agent-bus's snapshot size cap"
                           + ("" if names is None else ", even for the changed files' directories alone"))
            return snap
    tmp = tdir / f".tmp-{uuid.uuid4().hex}"
    try:
        snap.skipped += _write(repo, env, tmp, chosen)
        snap.files = sorted(e.path for e in chosen if (tmp / e.path).is_symlink() or (tmp / e.path).exists())
        _read_only(tmp)
        data = {"mode": snap.mode, "files": snap.files, "changed": snap.changed, "tree_files": snap.tree_files,
                "skipped": snap.skipped, "base": base, "bytes": sum(e.size for e in chosen),
                "tree_bytes": sum(e.size for e in blobs)}
        part = tdir / f".tmp-{uuid.uuid4().hex}.json"
        part.write_text(json.dumps(data))
        os.replace(part, meta)
        os.rename(tmp, final)
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        remove(tmp)
        snap.mode, snap.reason = "failed", f"the snapshot could not be written: {e}"
        return snap
    snap.path = str(final)
    return snap


def _write(repo: str, env: dict, dest: Path, chosen: list[_Entry]) -> int:
    """Write ``chosen`` blobs under ``dest`` (new) exactly as committed; returns how many
    symlinks were left out.  Regular files first, symlinks last, so nothing is ever written
    through a symlink; one that points out of the snapshot is left out."""
    dest.mkdir(mode=0o700)
    links = []
    proc = subprocess.Popen(["git", "cat-file", "--batch"], cwd=repo, env=env, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def feed() -> None:
        try:
            for e in chosen:
                proc.stdin.write(e.oid.encode() + b"\n")
            proc.stdin.close()
        except (OSError, ValueError):
            pass

    feeder = threading.Thread(target=feed, daemon=True)
    feeder.start()
    timer = threading.Timer(GIT_TIMEOUT_S, proc.kill)
    timer.start()
    try:
        for e in chosen:
            header = proc.stdout.readline().split()
            if len(header) != 3 or header[1] != b"blob":
                raise ValueError(f"git cat-file gave no blob for {e.path}")
            size = int(header[2])
            body = proc.stdout.read(size)
            proc.stdout.read(1)  # the newline after each object
            if len(body) != size:
                raise ValueError(f"git cat-file stopped short at {e.path}")
            if e.mode == "120000":
                links.append((e.path, os.fsdecode(body)))
                continue
            target = dest / e.path
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(body)
                os.fchmod(f.fileno(), 0o555 if e.mode == "100755" else 0o444)
    finally:
        timer.cancel()
        proc.kill()
        proc.wait()
        feeder.join(5)
    skipped = 0
    made: list[Path] = []
    for path, target in links:
        if not _inside(path, target):
            skipped += 1
            continue
        link = dest / path
        link.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.symlink(target, link)
        made.append(link)
    # The text check can't see through other links (a/.. after a link a -> x/y is x, not the
    # top), so check each one as the filesystem resolves it, and again after a removal.
    top = os.path.realpath(dest)
    while True:
        out = [link for link in made if os.path.commonpath([top, os.path.realpath(link)]) != top]
        if not out:
            return skipped
        for link in out:
            link.unlink()
            made.remove(link)
            skipped += 1


def _read_only(top: Path) -> None:
    for d, _, _ in list(os.walk(top)):  # listed first: chmod needs only search access after
        os.chmod(d, 0o555)


def remove(path: Path) -> None:
    """Remove a snapshot (read-only directories included), or anything else under root()."""
    try:
        if path.is_symlink() or not path.is_dir():
            path.unlink(missing_ok=True)
            return
        for d, _, _ in os.walk(path):
            os.chmod(d, 0o700)
        shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass


def stale(bus, thread_id: str) -> bool:
    """A thread's snapshots are no longer needed: the thread is gone, or no job of it is
    queued or running.  A review waiting for the author's fix stays open (#143), maybe for
    good; its next pass makes the snapshot again (a new head needs a new one anyway)."""
    from .db import BusError
    try:
        bus.thread(thread_id)
    except BusError:
        return True
    return bus.active_job(thread_id) is None


def sweep(bus, only: str | None = None) -> None:
    """Remove the snapshots of every thread that no longer needs them (or of ``only``)."""
    top = root()
    if not top.is_dir():
        return
    for p in top.iterdir():
        if p.name.startswith(".") or (only is not None and p.name != only):
            continue
        with _locked(p.name, wait=0) as mine:  # the decision and the removal, together
            if mine and stale(bus, p.name):  # a held lock: being built for a live job
                remove(p)


# The most files a resumed follow-up lists as added to or removed from the copy (#126).
MAX_DELTA = 200


def delta(repo: str, old_head: str, snap: Snapshot, baseline: bool) -> tuple[list[str], list[str]] | None:
    """The files a full snapshot of ``snap.head`` has that one of ``old_head`` didn't, and the
    ones it lost (#126), for a resumed follow-up whose session was given the old head's whole
    list (``baseline``: see complete()); or None when that can't be told exactly enough, so
    the whole list is shown: no baseline, a sparse copy now, the old list too long to have
    been listed whole, a symlink changed or deleted, a type change, a git error, or over
    MAX_DELTA files."""
    if not baseline or snap.mode != "full" or not snap.path or not _ID.fullmatch(old_head):
        return None
    env = _git_env(repo)
    out = _git(repo, env, "diff", "--raw", "-z", "--no-renames", "--no-abbrev", "--no-ext-diff",
               f"{old_head}^{{commit}}", f"{snap.head}^{{commit}}", "--") if env is not None else None
    if out is None:
        return None
    have = set(snap.files)
    recs = out.split(b"\0")
    added, removed = [], []
    # Records: ":oldmode newmode oldsha newsha status", then the path.
    for meta, path in zip(recs[0::2], recs[1::2]):
        fields = meta.split()
        if len(fields) != 5:
            return None
        old_mode, new_mode, status = fields[0].lstrip(b":"), fields[1], fields[4]
        name = os.fsdecode(path)
        if b"120000" in (old_mode, new_mode):
            # A symlink changed: whether it is in the copy can change too (codex-7); a deleted
            # one was in the old copy only if its target was in the tree then (#134); and an
            # added one can change where other, unchanged symlinks resolve.
            return None
        if status == b"A" and name in have:  # not a submodule, which the copy leaves out
            added.append(name)
        elif status == b"D" and _safe(name) and old_mode != b"160000":  # a submodule never was in it
            removed.append(name)
        elif status not in (b"A", b"D", b"M"):
            return None  # a type change, or anything else: can't tell (antigravity-5)
    if len(added) + len(removed) > MAX_DELTA:
        return None
    # The earlier prompt listed the old copy whole only if its list fit (see listing).
    esc = lambda p: p.encode("utf-8", "surrogateescape").decode("utf-8", "backslashreplace")  # noqa: E731
    old = (have - set(added)) | set(removed)
    if len("\n".join(map(esc, old))) > MAX_LISTING:  # measured as listing() shows them
        return None
    return sorted(map(esc, added)), sorted(map(esc, removed))


def complete(snap: Snapshot) -> bool:
    """Whether a prompt listing ``snap`` lists every file of the head's tree: a full copy
    whose list fits MAX_LISTING (listing() shows it whole).  A baseline for delta()."""
    if not snap.path or snap.mode != "full":
        return False
    esc = (p.encode("utf-8", "surrogateescape").decode("utf-8", "backslashreplace") for p in snap.files)
    return len("\n".join(esc)) <= MAX_LISTING


# How much of a snapshot's file list a prompt shows a peer that can't list directories.
MAX_LISTING = 12_000


def listing(snap: Snapshot, limit: int = MAX_LISTING) -> str:
    """The snapshot's files, one per line, capped at ``limit`` characters.  When they don't
    all fit, the changed files' directories and the top level come first, then a count of
    files per directory, so a peer that can't list directories still sees the layout."""
    # A non-UTF-8 name (kept as surrogates for the filesystem) is shown escaped.
    files = [p.encode("utf-8", "surrogateescape").decode("utf-8", "backslashreplace") for p in snap.files]
    snap = Snapshot(head=snap.head, files=files, changed=[
        p.encode("utf-8", "surrogateescape").decode("utf-8", "backslashreplace") for p in snap.changed])
    text = "\n".join(files)
    if len(text) <= limit:
        return text
    dirs = {_parent(p) for p in snap.changed} | {""}
    first = [p for p in files if _parent(p) in dirs]
    rest = [p for p in files if _parent(p) not in dirs]
    counts: dict[str, int] = {}
    for p in files:
        d = _parent(p)
        if d:
            counts[d] = counts.get(d, 0) + 1
    summary = [f"{d}/ ({n} files)" for d, n in sorted(counts.items())]
    budget = limit // 2
    shown, used = [], 0
    for p in first + rest:
        if used + len(p) + 1 > budget:
            break
        shown.append(p)
        used += len(p) + 1
    out = "\n".join(shown) + f"\n[... {len(files) - len(shown)} more files not listed ...]"
    dir_text, used = [], 0
    for line in summary:
        if used + len(line) + 1 > limit - budget:
            dir_text.append(f"[... {len(summary) - len(dir_text)} more directories not listed ...]")
            break
        dir_text.append(line)
        used += len(line) + 1
    if dir_text:
        out += "\nDirectories, with the number of files directly in each:\n" + "\n".join(dir_text)
    return out
