"""#181: in a repo listed in self_hosted_reviews (or self_hosted_prompts), a review job runs
from the head's own build, under the installed worker's lease; the installed build runs it,
with the reason, when the head's can't (no flake, another schema, a failed or slow build, an
old head, a worker that dies before any turn)."""
import json
import os
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from agent_bus import cli, headbuild, worker
from agent_bus import config as cfg
from agent_bus.db import Bus
from agent_bus.service import Service
from agent_bus.snapshot import Snapshot

from test_head_prompts import INSTALLED, NEW_LINE, changed, write_config
from test_review_rounds import calls, git_repo, run_job

SRC = Path(__file__).resolve().parent.parent / "src"
DB_SOURCE = (SRC / "agent_bus" / "db.py").read_text()


@pytest.fixture(autouse=True)
def installed_bin(monkeypatch, tmp_path):
    """The installed agent-bus, one executable (codex-8): the hand-off needs one, and a Nix
    build's check phase has none on PATH."""
    monkeypatch.setenv("AGENT_BUS_BIN", str(fake_build(tmp_path / "installed") / "bin" / "agent-bus"))


# ------------------------------------------------------------ the schema check


def test_the_schema_fingerprint_sees_schema_and_migrations_but_not_comments():  # (and codex-2's __init__)
    mine = headbuild.installed_fingerprint()
    assert headbuild.schema_fingerprint(DB_SOURCE) == mine
    assert headbuild.schema_fingerprint(DB_SOURCE.replace("    def _migrate(self) -> None:\n",
                                                          "    def _migrate(self) -> None:  # note\n")) == mine
    assert headbuild.schema_fingerprint(DB_SOURCE.replace("    pid INTEGER,\n", "    pid INTEGER, ppid INTEGER,\n",
                                                          1)) != mine
    assert headbuild.schema_fingerprint(DB_SOURCE.replace('ADD COLUMN purpose TEXT"', 'ADD COLUMN purpose BLOB"')) != mine
    assert headbuild.schema_fingerprint(DB_SOURCE.replace(
        '("harness", "TEXT"))', '("harness", "TEXT"), ("x", "TEXT"))')) != mine
    assert headbuild.schema_fingerprint(DB_SOURCE.replace(  # codex-2: the wrapper and the guard count too
        "        self._conn = conn\n", "        self._conn = conn\n        conn.execute('CREATE TABLE x (a)')\n")) != mine
    assert headbuild.schema_fingerprint(DB_SOURCE.replace('"SQLITE_CREATE_", "SQLITE_DROP_"', '"SQLITE_DROP_"')) != mine
    assert headbuild.schema_fingerprint(DB_SOURCE.replace(
        "        if migrate:\n", "        self.conn.execute('CREATE TABLE IF NOT EXISTS x (a)')\n        if migrate:\n")) != mine
    with pytest.raises(headbuild.Unusable, match="has no _migrate"):
        headbuild.schema_fingerprint(DB_SOURCE.replace("def _migrate(", "def _migrate_v2("))
    with pytest.raises(headbuild.Unusable, match="not valid Python"):
        headbuild.schema_fingerprint("def (")


def snap_of(tmp_path, files: dict[str, str], mode="full") -> Snapshot:
    for name, text in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text)
    return Snapshot(head="0" * 40, path=str(tmp_path), mode=mode, files=sorted(files))


def test_what_keeps_the_heads_build_from_running(tmp_path):
    ok = {"flake.nix": "{}", headbuild.DB_PATH: DB_SOURCE}
    headbuild.check(snap_of(tmp_path / "ok", ok))
    cases = [
        (snap_of(tmp_path / "a", ok, mode="sparse"), "no full read-only copy of the head"),
        (Snapshot(head="0" * 40, reason="snapshots are turned off"), "snapshots are turned off"),
        (snap_of(tmp_path / "b", {headbuild.DB_PATH: DB_SOURCE}), "it has no flake.nix"),
        (snap_of(tmp_path / "c", {"flake.nix": "{}"}), f"it has no {headbuild.DB_PATH}"),
        (snap_of(tmp_path / "d", {**ok, headbuild.DB_PATH: DB_SOURCE.replace("blind INTEGER", "blinded INTEGER")}),
         "it changes the database schema or its migrations"),
    ]
    for snap, why in cases:
        with pytest.raises(headbuild.Unusable, match=why):
            headbuild.check(snap)


def test_a_head_builds_worker_never_creates_or_migrates_the_database(env, tmp_path):
    with pytest.raises(RuntimeError, match="creates none"):
        Bus(tmp_path / "none.db", migrate=False)
    assert not (tmp_path / "none.db").exists()
    Bus(tmp_path / "old.db").close()
    conn = sqlite3.connect(tmp_path / "old.db")
    conn.execute("ALTER TABLE events DROP COLUMN purpose")  # as from before #161
    conn.close()
    with pytest.raises(RuntimeError, match="needs a migration"):
        Bus(tmp_path / "old.db", migrate=False)
    cols = {r[1] for r in sqlite3.connect(tmp_path / "old.db").execute("PRAGMA table_info(events)")}
    assert "purpose" not in cols  # untouched
    Bus(env["bus"].path, migrate=False).close()  # an up-to-date one opens


def test_the_hand_off_is_read_once_and_only_for_its_job(monkeypatch):
    monkeypatch.setenv(headbuild.ENV, "j-1 host:1:ab " + "a" * 40)
    assert headbuild.take_handoff("j-2") is None and headbuild.ENV not in os.environ
    monkeypatch.setenv(headbuild.ENV, "j-1 host:1:ab " + "a" * 40)
    assert headbuild.take_handoff("j-1") == ("host:1:ab", "a" * 40)
    assert headbuild.ENV not in os.environ  # nothing the worker starts sees it
    env = headbuild.child_env("j-1 o h")
    assert env[headbuild.ENV] == "j-1 o h" and headbuild.ENV not in headbuild.child_env()


def test_self_host_info_speaks_the_protocol(capsys, monkeypatch):
    monkeypatch.setenv("AGENT_BUS_DB", "/nonexistent/dir/bus.db")  # never opened
    assert cli.main(["self-host-info"]) == 0
    assert json.loads(capsys.readouterr().out) == {"protocol": headbuild.PROTOCOL}


# ------------------------------------------------------------ the build and its cache


def fake_build(tmp_path, script: str | None = None) -> Path:
    """A head's build: a bin/agent-bus running this checkout's agent-bus, or ``script``."""
    out = tmp_path / "fake-build"
    (out / "bin").mkdir(parents=True, exist_ok=True)
    exe = out / "bin" / "agent-bus"
    exe.write_text(script or f"#!/bin/sh\nPYTHONPATH={SRC} exec {sys.executable} -m agent_bus \"$@\"\n")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    pkg = out / "lib" / "python3" / "site-packages" / "agent_bus"  # what check_package reads (codex-2)
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "db.py").write_text(DB_SOURCE)
    return out


@pytest.fixture
def nix(monkeypatch, tmp_path):
    """`nix build` replaced: it links the out-link to a fake build (``state["out"]``), or
    raises ``state["fail"]``; ``state["builds"]`` counts the builds."""
    state = {"builds": 0, "out": None, "fail": None, "links": []}

    def build(nix_, src, out_link, timeout, cancelled):
        state["builds"] += 1
        state["links"].append(out_link)
        assert os.path.isfile(os.path.join(src, "flake.nix"))
        if state["fail"]:
            raise headbuild.Unusable(state["fail"])
        os.symlink(state["out"] or fake_build(tmp_path), out_link)

    monkeypatch.setattr(headbuild, "find_nix", lambda: "/fake/nix")
    monkeypatch.setattr(headbuild, "nix_build", build)
    return state


def test_builds_are_cached_per_head_and_pruned(env, nix, tmp_path):
    snap = snap_of(tmp_path / "snap", {"flake.nix": "{}"})
    said = []
    out, use = headbuild.build(snap, "a" * 40, 60, lambda: False, building=lambda: said.append(1))
    assert os.access(os.path.join(out, "bin", "agent-bus"), os.X_OK)
    assert nix["links"] == [headbuild.cache_dir() / ("a" * 40)]  # built straight to its GC root (codex-4)
    out2, use2 = headbuild.build(snap, "a" * 40, 60, lambda: False)
    assert out2 == out and nix["builds"] == 1 and said == [1]
    old = headbuild.cache_dir() / ("a" * 40)
    stale = time.time() - headbuild.KEEP_S - 60
    os.utime(old, (stale, stale), follow_symlinks=False)
    headbuild.build(snap, "b" * 40, 60, lambda: False)
    assert old.is_symlink()  # in use: kept
    os.close(use), os.close(use2)
    os.utime(old, (stale, stale), follow_symlinks=False)
    headbuild.build(snap, "b" * 40, 60, lambda: False)
    assert not old.is_symlink() and (headbuild.cache_dir() / ("b" * 40)).is_symlink()
    assert (headbuild.cache_dir() / f".{'a' * 40}.lock").exists()  # lock files stay (codex-5)
    nix["fail"] = "its build failed (boom)"
    with pytest.raises(headbuild.Unusable, match="boom"):
        headbuild.build(snap, "c" * 40, 60, lambda: False)
    assert not (headbuild.cache_dir() / ("c" * 40)).exists()


def test_a_build_that_runs_too_long_is_stopped(tmp_path):
    slow = tmp_path / "nix"
    slow.write_text("#!/bin/sh\nsleep 60\n")
    slow.chmod(0o755)
    started = time.monotonic()
    with pytest.raises(headbuild.Unusable, match="its build took over 1 s"):
        headbuild.nix_build(str(slow), str(tmp_path), tmp_path / "out", 1, lambda: False)
    assert time.monotonic() - started < 15
    with pytest.raises(headbuild.Unusable, match="cancelled"):
        headbuild.nix_build(str(slow), str(tmp_path), tmp_path / "out", 30, lambda: True)
    failing = tmp_path / "nix-fail"
    failing.write_text("#!/bin/sh\n(echo 'building...'; echo 'error: flake has no default package';"
                       " echo '       last 25 log lines:'; echo \"For full logs, run 'nix log /nix/store/x.drv'.\") >&2\n"
                       "exit 1\n")
    failing.chmod(0o755)
    with pytest.raises(headbuild.Unusable, match=r"its build failed \(error: flake has no default package\)"):
        headbuild.nix_build(str(failing), str(tmp_path), tmp_path / "out", 30, lambda: False)


def test_only_the_packages_out_output_is_built(tmp_path):
    """codex-11 (#185): one out-link, the cache entry, whatever outputs the package installs."""
    args = tmp_path / "args"
    nix = tmp_path / "nix"
    nix.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" > {args}\n")
    nix.chmod(0o755)
    headbuild.nix_build(str(nix), "/snap", tmp_path / "out", 30, lambda: False)
    assert args.read_text().splitlines() == ["build", "--no-write-lock-file", "--no-update-lock-file",
                                             "--out-link", str(tmp_path / "out"), "path:/snap#default^out"]


def test_the_probe_refuses_an_old_or_other_build(tmp_path):
    headbuild.probe(str(fake_build(tmp_path / "new")))
    old = fake_build(tmp_path / "old", "#!/bin/sh\necho \"agent-bus: invalid choice: '$1'\" >&2\nexit 2\n")
    with pytest.raises(headbuild.Unusable, match="doesn't speak the hand-off protocol.*invalid choice"):
        headbuild.probe(str(old))
    other = fake_build(tmp_path / "other", "#!/bin/sh\necho '{\"protocol\": 1}'\n")  # before #185
    with pytest.raises(headbuild.Unusable, match="speaks hand-off protocol 1, not 2"):
        headbuild.probe(str(other))
    failed = fake_build(tmp_path / "failed", "#!/bin/sh\necho '{\"protocol\": 2}'\nexit 1\n")  # codex-6
    with pytest.raises(headbuild.Unusable, match="doesn't speak the hand-off protocol"):
        headbuild.probe(str(failed))


# ------------------------------------------------------------ a review job


def agent_bus_repo(repo, head_prompts: str, head_db: str = DB_SOURCE, head_files: dict | None = None):
    """A git repo shaped like agent-bus: a flake, db.py and prompts.py; the head changes
    prompts.py (and db.py, if ``head_db`` differs), and writes ``head_files``."""
    git_repo(repo)
    genv = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
                GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")
    git = lambda *a: subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True,  # noqa: E731
                                    text=True, env=genv).stdout.strip()
    (repo / "src" / "agent_bus").mkdir(parents=True)
    (repo / "flake.nix").write_text("{ outputs = _: { }; }\n")
    (repo / headbuild.DB_PATH).write_text(DB_SOURCE)
    (repo / "src/agent_bus/prompts.py").write_text(INSTALLED)
    git("add", ".")
    git("commit", "-qm", "agent-bus")
    base = git("rev-parse", "HEAD")
    (repo / "src/agent_bus/prompts.py").write_text(head_prompts)
    (repo / headbuild.DB_PATH).write_text(head_db)
    for path, text in (head_files or {}).items():
        if text is None:
            (repo / path).unlink()
        else:
            (repo / path).write_text(text)
    git("add", "-A")
    git("commit", "-qm", "change it")
    return base, git("rev-parse", "HEAD")


def review(env, head_db: str = DB_SOURCE, reviewers=("codex",)):
    bus, repo = env["bus"], env["repo"]
    base, head = agent_bus_repo(repo, changed(), head_db)
    claude = Service(bus, "claude", bus.register_session("claude"))
    if len(reviewers) == 1:
        tid = claude.request_review(reviewers[0], str(repo), base, head, change_id="PR-1")["thread"]["id"]
    else:
        tid = claude.request_review(None, str(repo), base, head, change_id="PR-1",
                                    reviewers=list(reviewers))["thread"]["id"]
    job = run_job(bus, tid)
    events = [(e["kind"], e["text"]) for e in bus.events(tid) if e["kind"] in ("prompts", "error")]
    return job, events, head, tid


def test_an_opted_in_review_runs_on_the_heads_build(env, no_spawn, nix, monkeypatch):
    """The issue's acceptance case: the head's own build (here, a separate process of this
    checkout's worker) runs the whole job, every reviewer's pass, under the installed
    worker's lease, and says so in each prompt and event."""
    monkeypatch.setenv("FAKE_APPROVE", "codex,antigravity")
    write_config(env, self_hosted_reviews=[str(env["repo"])])
    job, events, head, tid = review(env, reviewers=("codex", "antigravity"))
    assert job["status"] == "done" and job["pid"] == os.getpid()  # the supervisor stays the job's process
    assert env["bus"].thread(tid)["status"] == "resolved"
    h = head[:12]
    texts = [t for _, t in events]
    builds = [e["text"] for e in env["bus"].events(tid) if e["kind"] == "head_build"]
    assert builds[0] == f"building head {h} with nix"
    assert builds[1].startswith(f"head {h}'s build ready after ") and builds[1].endswith(" s")
    assert len(builds) == 2
    (start, h1, a), (end, h2, b) = [e["purpose"].split(" ") for e in env["bus"].events(tid, kind="head_build")]
    assert (start, h1, end, h2) == ("start", head, "end", head) and a == b  # #198: one worker's pair
    assert texts[0].startswith(f"head {h}'s own build runs this review job (")
    assert [t.split(" (agent-bus")[0] for t in texts[1:]] == [f"head {h}'s own build runs this pass"] * 2
    prompts_seen = [c["prompt"] for c in calls(env)]
    assert len(prompts_seen) == 2
    for p in prompts_seen:
        assert f"head {h}'s own build (agent-bus" in p.replace("\n", " ")
        assert "the change takes effect only once it is installed" not in p
        assert "(Prompts:" not in p and "(Build:" not in p
    assert nix["builds"] == 1  # the second reviewer's pass is the same job


def test_a_head_that_changes_the_schema_is_reviewed_by_the_installed_build(env, no_spawn, nix):
    write_config(env, self_hosted_reviews=[str(env["repo"])])
    job, events, head, _ = review(env, head_db=DB_SOURCE.replace("blind INTEGER", "blinded INTEGER"))
    assert job["status"] == "done" and nix["builds"] == 0
    h = head[:12]
    why = "it changes the database schema or its migrations"
    assert events[0][1].startswith(f"installed build runs this review: head {h}'s build can't ({why}")
    assert events[1][1].startswith(f"installed build (head {h}'s can't: {why}")
    assert events[1][1].endswith(f"review prompts from head {h}'s src/agent_bus/prompts.py")  # #178 still
    p = calls(env)[-1]["prompt"]
    assert f"head {h}'s own build isn't running this review: {why}" in p.replace("\n", " ")
    assert NEW_LINE in p.split("```diff")[0] + p.split("```\n")[-1]


def test_a_failed_build_falls_back_with_its_reason(env, no_spawn, nix):
    write_config(env, self_hosted_reviews=[str(env["repo"])])
    nix["fail"] = "its build failed (error: builder failed)"
    job, events, head, tid = review(env)
    assert job["status"] == "done" and nix["builds"] == 1
    builds = [e["text"] for e in env["bus"].events(tid) if e["kind"] == "head_build"]
    assert builds[0] == f"building head {head[:12]} with nix"
    assert builds[1].startswith(f"head {head[:12]}'s build failed after ")
    assert "its build failed (error: builder failed)" in events[0][1]
    assert "its build failed (error: builder failed)" in calls(env)[-1]["prompt"].replace("\n", " ")


def test_a_head_worker_that_dies_before_any_turn_falls_back(env, no_spawn, nix, tmp_path):
    write_config(env, self_hosted_reviews=[str(env["repo"])])
    nix["out"] = fake_build(tmp_path / "broken", "#!/bin/sh\n[ \"$1\" = self-host-info ] && "
                            "echo '{\"protocol\": 2}' && exit 0\necho 'ImportError: no module' >&2\nexit 3\n")
    job, events, head, _ = review(env)
    assert job["status"] == "done"
    assert "its worker exited (code 3) before any turn: ImportError: no module" in events[1][1]
    assert len(calls(env)) == 1  # the installed build's pass


def test_a_head_worker_that_dies_mid_job_fails_it(env, no_spawn, nix, tmp_path):
    write_config(env, self_hosted_reviews=[str(env["repo"])])
    turn = ("from agent_bus.db import Bus; import sys; j=sys.argv[1]; b=Bus(); "
            "b.add_event(b.job(j)['thread_id'], 'turn_start', 'codex', job_id=j)")
    nix["out"] = fake_build(tmp_path / "crash", f"#!/bin/sh\n[ \"$1\" = self-host-info ] && "
                            f"echo '{{\"protocol\": 2}}' && exit 0\nPYTHONPATH={SRC} {sys.executable} -c \"{turn}\" "
                            f"\"$2\"\necho 'Segmentation fault' >&2\nexit 139\n")
    job, events, head, _ = review(env)
    assert job["status"] == "failed" and calls(env) == []
    why = f"head {head[:12]}'s build's worker exited (code 139) without finishing the job: Segmentation fault"
    assert job["error"] == why
    assert events[-1][0] == "error" and events[-1][1].startswith(why + " (log: ")


def test_the_older_prompts_key_never_runs_the_heads_build(env, no_spawn, nix):
    """codex-1: self_hosted_prompts (#178) keeps meaning the head's prompt text only."""
    write_config(env, self_hosted_prompts=[str(env["repo"])])
    job, events, head, _ = review(env)
    assert job["status"] == "done" and nix["builds"] == 0
    assert events == [("prompts", f"review prompts from head {head[:12]}'s src/agent_bus/prompts.py")]
    assert "(Build:" not in calls(env)[-1]["prompt"]


def test_a_build_packaging_another_schema_falls_back(env, no_spawn, nix, tmp_path):
    """codex-2: the packaged db.py is checked too, not only the source it was built from."""
    write_config(env, self_hosted_reviews=[str(env["repo"])])
    out = fake_build(tmp_path / "other-db")
    (out / "lib/python3/site-packages/agent_bus/db.py").write_text(DB_SOURCE.replace("blind INTEGER", "x INTEGER"))
    nix["out"] = out
    job, events, head, _ = review(env)
    assert job["status"] == "done" and len(calls(env)) == 1
    assert "its build's database schema or migrations differ" in events[0][1]


def test_a_head_worker_that_posts_then_dies_fails_the_job(env, no_spawn, nix, tmp_path):
    """codex-10: the head's worker's own posts (a review-request notice) mean it started."""
    write_config(env, self_hosted_reviews=[str(env["repo"])])
    post = ("from agent_bus.db import Bus; import sys; j=sys.argv[1]; b=Bus(); "
            "b.post(b.job(j)['thread_id'], 'system', 'Review requested from codex', kind='status', "
            "session_id='job:'+j)")
    nix["out"] = fake_build(tmp_path / "posted", f"#!/bin/sh\n[ \"$1\" = self-host-info ] && "
                            f"echo '{{\"protocol\": 2}}' && exit 0\nPYTHONPATH={SRC} {sys.executable} -c \"{post}\" "
                            f"\"$2\"\nexit 1\n")
    job, events, head, _ = review(env)
    assert job["status"] == "failed" and calls(env) == []
    assert "exited (code 1) without finishing the job" in job["error"]


def test_without_the_opt_in_nothing_changes(env, no_spawn, nix):
    job, events, _, _ = review(env)
    assert job["status"] == "done" and events == [] and nix["builds"] == 0
    assert "(Build:" not in calls(env)[-1]["prompt"]


def test_builds_off_keeps_the_literal_prompts_and_says_why(env, no_spawn, nix):
    """codex-9: an opted-in review with builds off says the installed build runs it, and why."""
    write_config(env, self_hosted_reviews=[str(env["repo"])], self_hosted_build_timeout_s=0)
    job, events, head, _ = review(env)
    assert job["status"] == "done" and nix["builds"] == 0
    h, off = head[:12], "head builds are off (self_hosted_build_timeout_s is 0)"
    assert events == [("prompts", f"installed build runs this review: head {h}'s build can't ({off})"),
                      ("prompts", f"installed build (head {h}'s can't: {off}); "
                                  f"review prompts from head {h}'s src/agent_bus/prompts.py")]
    p = calls(env)[-1]["prompt"].replace("\n", " ")
    assert f"head {h}'s own build isn't running this review: {off}." in p
    assert NEW_LINE in calls(env)[-1]["prompt"].split("```diff")[0] + calls(env)[-1]["prompt"].split("```\n")[-1]


def test_a_cache_it_cant_write_falls_back(env, no_spawn, nix):
    """codex-6: an I/O error making the build is a reason, not a failed review."""
    write_config(env, self_hosted_reviews=[str(env["repo"])])
    headbuild.cache_dir().parent.mkdir(parents=True, exist_ok=True)
    headbuild.cache_dir().write_text("not a directory")
    job, events, head, _ = review(env)
    assert job["status"] == "done" and nix["builds"] == 0
    assert "its build could not be made or found (" in events[0][1]


def test_an_author_note_during_a_startup_crash_still_falls_back(env, no_spawn, nix, tmp_path):
    """codex-7: only the job's own turns mean the head's worker started."""
    write_config(env, self_hosted_reviews=[str(env["repo"])])
    note = ("from agent_bus.db import Bus; import sys; j=sys.argv[1]; b=Bus(); "
            "b.post(b.job(j)['thread_id'], 'claude', 'a note from the author')")
    nix["out"] = fake_build(tmp_path / "broken", f"#!/bin/sh\n[ \"$1\" = self-host-info ] && "
                            f"echo '{{\"protocol\": 2}}' && exit 0\nPYTHONPATH={SRC} {sys.executable} -c \"{note}\" "
                            f"\"$2\"\nexit 1\n")
    job, events, head, _ = review(env)
    assert job["status"] == "done" and len(calls(env)) == 1
    assert "its worker exited (code 1) before any turn" in events[1][1]


@pytest.mark.parametrize("sender, session", [("codex", "a-codex-session"), ("system", None)])
def test_anyone_elses_note_during_a_startup_crash_still_falls_back(env, no_spawn, nix, tmp_path, sender, session):
    """codex-12 (#185): a note from another participant (or a notice the job didn't post)
    isn't the head's worker's progress: only a message attributed to the job is."""
    write_config(env, self_hosted_reviews=[str(env["repo"])])
    note = ("from agent_bus.db import Bus; import sys; j=sys.argv[1]; b=Bus(); "
            f"b.post(b.job(j)['thread_id'], {sender!r}, 'a note', session_id={session!r})")
    nix["out"] = fake_build(tmp_path / "broken", f"#!/bin/sh\n[ \"$1\" = self-host-info ] && "
                            f"echo '{{\"protocol\": 2}}' && exit 0\nPYTHONPATH={SRC} {sys.executable} -c \"{note}\" "
                            f"\"$2\"\nexit 1\n")
    job, events, head, _ = review(env)
    assert job["status"] == "done" and len(calls(env)) == 1
    assert "its worker exited (code 1) before any turn" in events[1][1]


def test_a_worker_from_before_job_attribution_is_never_handed_the_job(env, no_spawn, nix, tmp_path):
    """codex-1 (#185): a protocol 1 head posted its notices unattributed, so a crash after one
    would read as a startup crash and the installed build would run the job again: such a
    head is refused before the hand-off."""
    write_config(env, self_hosted_reviews=[str(env["repo"])])
    ran = tmp_path / "ran"
    nix["out"] = fake_build(tmp_path / "legacy", "#!/bin/sh\n[ \"$1\" = self-host-info ] && "
                            f"echo '{{\"protocol\": 1}}' && exit 0\ntouch {ran}\nexit 1\n")
    job, events, head, _ = review(env)
    assert job["status"] == "done" and len(calls(env)) == 1 and not ran.exists()
    assert "it speaks hand-off protocol 1, not 2" in events[0][1]


def test_a_workers_posts_carry_its_job(env, no_spawn, nix, monkeypatch):
    """codex-12 (#185): every message a review job's worker posts (notices and verdicts)
    says which job posted it."""
    monkeypatch.setenv("FAKE_APPROVE", "codex,antigravity")
    write_config(env, self_hosted_reviews=[str(env["repo"])])
    job, _, _, tid = review(env, reviewers=("codex", "antigravity"))
    bus = env["bus"]
    mine = [m for m in bus.history(tid)["messages"] if m["session_id"] == f"job:{job['id']}"]
    assert {m["kind"] for m in mine} >= {"review_verdict", "status"}
    others = [m for m in bus.history(tid)["messages"] if m["session_id"] != f"job:{job['id']}"]
    assert all(m["sender"] == "claude" or m["created_at"] < job["created_at"] for m in others), others


def test_a_cancelled_job_stops_a_hung_head_worker(env, no_spawn, nix, tmp_path, monkeypatch):
    """codex-3: a cancel (or a lost lease) stops the head's worker even if it ignores it."""
    monkeypatch.setattr(worker, "LEASE_S", 3)  # heartbeat every second
    write_config(env, self_hosted_reviews=[str(env["repo"])])
    nix["out"] = fake_build(tmp_path / "hung", "#!/bin/sh\n[ \"$1\" = self-host-info ] && "
                            "echo '{\"protocol\": 2}' && exit 0\nexec sleep 300\n")
    bus = env["bus"]
    base, head = agent_bus_repo(env["repo"], changed())
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review("codex", str(env["repo"]), base, head, change_id="PR-1")["thread"]["id"]
    threading.Timer(3, lambda: Bus().cancel_thread(tid)).start()
    started = time.monotonic()
    job = run_job(bus, tid)
    assert job["status"] == "cancelled" and time.monotonic() - started < 20


def test_the_head_worker_never_changes_the_databases_shape(env):
    """codex-2: a Bus opened without migrations refuses DDL, so a head that slipped one past
    the fingerprint still can't apply it."""
    b = Bus(env["bus"].path, migrate=False)
    for ddl in ("CREATE TABLE x (a)", "CREATE INDEX x ON events(kind)", "ALTER TABLE events ADD COLUMN z",
                "DROP TABLE cursors", "CREATE TRIGGER t AFTER INSERT ON events BEGIN SELECT 1; END"):
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            b.conn.execute(ddl)
    b.conn.execute("CREATE TEMP TABLE scratch (a)")  # its own connection's
    b.add_event("t-x", "note", "rows are fine")
    b.close()


def test_the_handoff_needs_one_installed_executable(env, monkeypatch):
    """codex-8: what the head's worker starts must be the installed build."""
    monkeypatch.delenv("AGENT_BUS_BIN", raising=False)
    monkeypatch.setattr(cfg, "_LAUNCHED_FROM", None)
    monkeypatch.setattr(cfg.shutil, "which", lambda name: None)
    with pytest.raises(headbuild.Unusable, match="isn't one executable"):
        headbuild.child_env("j-1 o h")


def test_the_head_worker_stops_once_its_supervisors_lease_runs_out(env, no_spawn):
    """antigravity-1: the head's worker runs no heartbeat; a lease nobody renews stops it."""
    bus = env["bus"]
    base, head = agent_bus_repo(env["repo"], changed())
    claude = Service(bus, "claude", bus.register_session("claude"))
    tid = claude.request_review("codex", str(env["repo"]), base, head, change_id="PR-1")["thread"]["id"]
    jid = bus.jobs(tid)[-1]["id"]
    assert bus.claim_job(jid, "host:1:ab", 60)
    w = worker.Worker(bus, jid, handoff=("host:1:ab", head))
    assert not w.cancelled()
    bus.conn.execute("UPDATE jobs SET lease_expires=? WHERE id=?", (time.time() - 1, jid))
    assert w.cancelled() and w._lost.is_set()


def test_the_head_worker_gets_the_callers_path_without_the_installed_wrappers(monkeypatch, tmp_path):
    """antigravity-2: a worker spawned from a wrapped agent-bus records a wrapped caller PATH."""
    wrapped_bin, git = str(tmp_path / "installed-bin"), str(tmp_path / "git-bin")
    monkeypatch.setattr(cfg, "wrapper_path_dirs", lambda: {wrapped_bin, git})
    monkeypatch.setenv("AGENT_BUS_CALLER_PATH", os.pathsep.join([wrapped_bin, "/usr/bin", "/bin", git]))
    assert headbuild.child_env()["PATH"] == os.pathsep.join(["/usr/bin", "/bin"])
    monkeypatch.delenv("AGENT_BUS_CALLER_PATH")
    monkeypatch.setenv("PATH", os.pathsep.join([wrapped_bin, "/usr/bin", git]))
    assert headbuild.child_env()["PATH"] == "/usr/bin"


def test_a_build_without_agent_bus_keeps_no_gc_root(env, nix, tmp_path):
    """antigravity-5."""
    (tmp_path / "empty-build").mkdir()
    nix["out"] = tmp_path / "empty-build"
    with pytest.raises(headbuild.Unusable, match="has no bin/agent-bus"):
        headbuild.build(snap_of(tmp_path / "snap", {"flake.nix": "{}"}), "d" * 40, 60, lambda: False)
    assert not (headbuild.cache_dir() / ("d" * 40)).is_symlink()


# ------------------------------------------------------------ #188: said up front, and while it builds


def requested(env, reviewers=("codex",), **head) -> tuple[dict, str]:
    base, h = agent_bus_repo(env["repo"], changed(), **head)
    claude = Service(env["bus"], "claude", env["bus"].register_session("claude"))
    if len(reviewers) == 1:
        return claude.request_review(reviewers[0], str(env["repo"]), base, h, change_id="PR-1"), h
    return claude.request_review(None, str(env["repo"]), base, h, change_id="PR-1", reviewers=list(reviewers)), h


@pytest.mark.parametrize("reviewers", [("codex",), ("codex", "antigravity")])
@pytest.mark.parametrize("head, why", [
    ({"head_db": DB_SOURCE.replace("blind INTEGER", "blinded INTEGER")},
     "it changes the database schema or its migrations"),
    ({"head_files": {headbuild.SELF_PATH: "import ast\n\nPROTOCOL = 3\n"}},
     f"its {headbuild.SELF_PATH} sets hand-off protocol 3, the installed build's is {headbuild.PROTOCOL}"),
    ({"head_files": {"flake.nix": None}}, "it has no flake.nix"),
])
def test_the_request_says_when_the_installed_build_will_run_it(env, no_spawn, head, why, reviewers):
    write_config(env, self_hosted_reviews=[str(env["repo"])])
    res, h = requested(env, reviewers, **head)
    assert res["build"].startswith(f"Expect the installed build to run this review: head {h[:12]}'s own build "
                                   f"can't ({why}).")


def test_the_request_says_the_heads_build_will_be_tried(env, no_spawn):
    write_config(env, self_hosted_reviews=[str(env["repo"])])
    same = {"head_files": {headbuild.SELF_PATH: f"PROTOCOL = {headbuild.PROTOCOL}\n"}}
    res, h = requested(env, **same)
    assert res["build"].startswith(f"Head {h[:12]}'s own build will run this review once nix has built it")
    assert "the installed build runs it instead" in res["build"]


@pytest.mark.parametrize("key", [None, "self_hosted_prompts"])
def test_the_request_says_nothing_about_builds_elsewhere(env, no_spawn, key):
    if key:
        write_config(env, **{key: [str(env["repo"])]})
    assert "build" not in requested(env)[0]


def test_the_request_says_when_builds_are_off(env, no_spawn):
    write_config(env, self_hosted_reviews=[str(env["repo"])], self_hosted_build_timeout_s=0)
    res, h = requested(env)
    assert "head builds are off (self_hosted_build_timeout_s is 0)" in res["build"]


def test_the_head_is_never_run_to_predict_its_build(env, no_spawn, tmp_path):
    """Only literals are read: a PROTOCOL computed by code says nothing either way."""
    write_config(env, self_hosted_reviews=[str(env["repo"])])
    ran = tmp_path / "ran"
    res, h = requested(env, head_files={headbuild.SELF_PATH: f"import os\nPROTOCOL = os.system('touch {ran}')\n"})
    assert res["build"].startswith(f"Head {h[:12]}'s own build will run") and not ran.exists()


def test_wait_and_tail_show_the_heads_build(env, no_spawn, capsys):
    write_config(env, self_hosted_reviews=[str(env["repo"])])
    res, h = requested(env)
    bus, tid, jid = env["bus"], res["thread"]["id"], res["job"]["id"]
    claude = Service(bus, "claude", bus.register_session("claude"))
    assert "head_build" not in claude.progress(tid)
    bus.add_event(tid, "head_build", f"building head {h[:12]} with nix", job_id=jid)
    p = claude.progress(tid)
    assert p["head_build"]["state"] == "building" and p["head_build"]["head"] == h[:12] and p["peers"] == {}
    assert cli.main(["tail", tid, "--once", "--no-color"]) == 0
    assert f"worker head_build: building head {h[:12]} with nix" in capsys.readouterr().out
    bus.add_event(tid, "head_build", f"head {h[:12]}'s build ready after 3 s", job_id=jid)
    assert claude.progress(tid)["head_build"]["state"] == "done"


def test_head_build_progress_pairs_each_attempt(env, no_spawn, capsys, monkeypatch):
    """#198: a start opens an attempt and only the same worker's end closes it; an attempt by
    a worker that no longer holds the job shows as stopped; the head is the event's, not the
    thread's current one.  Older builds' events (no purpose) pair by their text."""
    write_config(env, self_hosted_reviews=[str(env["repo"])])
    res, h = requested(env)
    bus, tid, jid = env["bus"], res["thread"]["id"], res["job"]["id"]
    claude = Service(bus, "claude", bus.register_session("claude"))
    other = "f" * 40
    progress = lambda: claude.progress(tid)["head_build"]  # noqa: E731
    bus.add_event(tid, "head_build", f"building head {h[:12]} with nix", job_id=jid)  # an older build's
    assert progress()["state"] == "building" and progress()["head"] == h[:12]
    assert bus.claim_job(jid, "A", lease_s=60)
    bus.add_event(tid, "head_build", f"building head {other[:12]} with nix", job_id=jid, purpose=f"start {other} A")
    assert progress()["state"] == "building" and progress()["head"] == other[:12]
    # B takes the job over: A's unfinished attempt stopped with it, and A's late end closes nothing.
    bus.conn.execute("UPDATE jobs SET lease_owner='B' WHERE id=?", (jid,))
    assert progress()["state"] == "stopped"
    bus.add_event(tid, "head_build", f"building head {h[:12]} with nix", job_id=jid, purpose=f"start {h} B")
    bus.add_event(tid, "head_build", f"head {h[:12]}'s build ready after 9 s", job_id=jid, purpose=f"end {h} A")
    assert progress()["state"] == "building" and progress()["head"] == h[:12]
    bus.add_event(tid, "head_build", f"head {h[:12]}'s build ready after 1 s", job_id=jid, purpose=f"end {h} B")
    assert progress()["state"] == "done"
    # A plain tail reads only head_build events, up to the thread's latest event (#198).
    seen = []
    real = Bus.events
    monkeypatch.setattr(Bus, "events", lambda self, *a, **kw: seen.append(kw) or real(self, *a, **kw))
    bus.add_event(tid, "tool", "ls", agent="codex", job_id=jid)
    assert cli.main(["tail", tid, "--once", "--no-color"]) == 0
    out = capsys.readouterr().out
    assert [kw.get("kind") for kw in seen] == ["head_build"] and seen[0]["upto_id"] == bus.last_event_id(tid)
    assert f"worker head_build: building head {other[:12]} with nix" in out and "codex tool" not in out


def test_a_worker_that_lost_the_job_records_no_build_event(env, no_spawn, nix, monkeypatch):
    """#198 (codex-1): head_build events are fenced by the job's lease."""
    write_config(env, self_hosted_reviews=[str(env["repo"])])
    real = headbuild.build

    def taken_over(snap, head, *a, building, **kw):
        env["bus"].conn.execute("UPDATE jobs SET lease_owner='other'")
        return real(snap, head, *a, building=building, **kw)

    monkeypatch.setattr(headbuild, "build", taken_over)
    job, _, _, tid = review(env)
    assert not [e for e in env["bus"].events(tid) if e["kind"] == "head_build"]
    assert job["lease_owner"] == "other" and nix["builds"] == 0  # it stopped before building


@pytest.mark.parametrize("source, want", [
    ("PROTOCOL = 3\n", 3),
    ("PROTOCOL = 3\nPROTOCOL: int = 2\n", None),  # codex-1: rebound, so it can't tell
    ("PROTOCOL = 3\nPROTOCOL += 1\n", None),
    ("PROTOCOL = '3'\n", None),
    ("PROTOCOL = 1 + 2\n", None),
    ("PROTOCOL[0] = 3\n", None),  # antigravity-4: not a binding of the name
    ("(PROTOCOL,) = (3,)\n", None),
    ("def f(:\n", None),
])
def test_the_heads_protocol_is_one_int_literal(source, want):
    assert headbuild.protocol_of(source) == want


@pytest.mark.parametrize("files", [
    {headbuild.SELF_PATH: "PROTOCOL = 0x" + "f" * 20000 + "\n"},  # codex-2: too big to print
    {headbuild.DB_PATH: "X = " + "(" * 5000 + "1" + ")" * 5000 + "\n"},  # codex-2: too deep to parse
])
def test_a_head_it_cant_parse_never_fails_the_request(env, no_spawn, files):
    write_config(env, self_hosted_reviews=[str(env["repo"])])
    res, h = requested(env, head_files=files)
    assert res["job"]["status"] == "queued"
    assert res["build"].startswith(f"Head {h[:12]}'s own build will run") or "Expect the installed" in res["build"]


def test_a_head_with_none_of_agent_bus_files_has_no_flake(env, no_spawn):
    """codex-3: a commit without any of the files is a known miss, not an unreadable tree."""
    write_config(env, self_hosted_reviews=[str(env["repo"])])
    gone = {"flake.nix": None, headbuild.DB_PATH: None, "src/agent_bus/prompts.py": None}
    res, h = requested(env, head_files=gone)
    assert "own build can't (it has no flake.nix)" in res["build"]
    assert headbuild.predict(str(env["repo"]), "e" * 40) is None  # a commit git doesn't have: can't tell


def test_the_note_reads_the_config_the_worker_will(env, no_spawn):
    """codex-5: a config edited after this server started counts."""
    claude = Service(env["bus"], "claude", env["bus"].register_session("claude"))
    write_config(env, self_hosted_reviews=[str(env["repo"])], self_hosted_build_timeout_s=0)
    base, h = agent_bus_repo(env["repo"], changed())
    claude.config = cfg.Config.load()
    write_config(env, self_hosted_build_timeout_s=600)
    res = claude.request_review("codex", str(env["repo"]), base, h, change_id="PR-1")
    assert res["build"].startswith(f"Head {h[:12]}'s own build will run")


def test_a_wait_for_another_build_of_the_head_shows_as_building(env, nix, tmp_path):
    """codex-6: building() is called before the per-head lock, so a second worker waiting on
    the first's build shows it too."""
    import fcntl
    snap = snap_of(tmp_path / "snap", {"flake.nix": "{}"})
    headbuild.cache_dir().mkdir(parents=True, exist_ok=True)
    held = os.open(headbuild.cache_dir() / f".{'a' * 40}.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(held, fcntl.LOCK_EX)
    said = []
    with pytest.raises(headbuild.Unusable, match="another build of it took over"):
        headbuild.build(snap, "a" * 40, 0.6, lambda: False, building=lambda: said.append(1))
    os.close(held)
    assert said == [1]


def test_a_blob_this_partial_clone_lacks_is_unknown_not_missing(env, no_spawn, tmp_path):
    """antigravity-1: in a blobless clone the head's files aren't fetched; that says nothing."""
    base, h = agent_bus_repo(env["repo"], changed())
    subprocess.run(["git", "config", "uploadpack.allowFilter", "true"], cwd=env["repo"], check=True)
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", "--no-checkout", "--filter=blob:none", f"file://{env['repo']}", str(clone)],
                   check=True, capture_output=True)
    assert headbuild.predict(str(clone), h) is None
    assert headbuild._head_files(str(clone), h) == {"flake.nix": "", headbuild.SELF_PATH: None}
    assert headbuild._head_files(str(env["repo"]), h)["flake.nix"] == ""


def test_a_missing_flake_counts_even_when_db_py_cant_be_read(env, no_spawn):
    """codex-9: each file on its own: an over-size db.py doesn't hide a missing flake.nix."""
    write_config(env, self_hosted_reviews=[str(env["repo"])])
    big = DB_SOURCE + "#" * (headbuild.MAX_BYTES + 1) + "\n"
    res, h = requested(env, head_db=big, head_files={"flake.nix": None})
    assert "own build can't (it has no flake.nix)" in res["build"]


def test_the_prediction_has_one_short_deadline(env, monkeypatch):
    """codex-4: past PREDICT_TIMEOUT_S in all, it can't tell."""
    base, h = agent_bus_repo(env["repo"], changed())
    monkeypatch.setattr(headbuild, "PREDICT_TIMEOUT_S", 0)
    assert headbuild._head_files(str(env["repo"]), h) is None
