import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from io import BytesIO
from types import SimpleNamespace

import pytest

from brigade import (
    code_cmd,
    code_graph_background,
    code_graph_delta,
    component_bins,
    engine_paths,
    inbox_lock,
    localio,
    nt_process,
    proc,
    proc_group,
    runguard,
    work_cmd,
)


def test_work_verify_detached_worker_survives_and_releases_pipes(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
    (repo / ".gitignore").write_text(".brigade/\n")
    subprocess.run(["git", "add", ".gitignore"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.name=Test User", "-c", "user.email=test@example.invalid", "commit", "-qm", "init"],
        cwd=repo,
        check=True,
    )
    script = tmp_path / "engine.py"
    started, release = tmp_path / "started", tmp_path / "release"
    script.write_text(
        "import sqlite3, sys, time\n"
        "from pathlib import Path\n"
        f"assert Path(sys.argv[-1]) == Path({str(repo)!r})\n"
        f"Path({str(started)!r}).touch(exist_ok=False)\n"
        "limit = time.monotonic() + 90\n"
        f"while not Path({str(release)!r}).exists():\n"
        "    if time.monotonic() > limit: raise TimeoutError()\n"
        "    time.sleep(0.02)\n"
        "with sqlite3.connect(sys.argv[2]) as db:\n"
        "    db.execute('CREATE TABLE meta (key TEXT, value TEXT)')\n"
        "    db.execute(\"INSERT INTO meta VALUES ('synced_at', '1')\")\n"
        "print('detached worker stdout')\n"
        "print('detached worker stderr', file=sys.stderr)\n"
    )
    engine = tmp_path / ("engine.cmd" if os.name == "nt" else "engine")
    if os.name == "nt":
        engine.write_text(f'@echo off\n"{sys.executable}" "{script}" %*\n')
    else:
        engine.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(script))} "$@"\n')
        engine.chmod(0o755)
    monkeypatch.setenv("BRIGADE_CODE_GRAPH_BIN", str(engine))
    db = repo / ".brigade/code-graph/code-graph.db"
    reservation = db.parent / "verify-sync.json"
    argv = [
        sys.executable,
        "-m",
        "brigade",
        "work",
        "verify",
        "run",
        "--target",
        str(repo),
        "--argv-json",
        json.dumps([sys.executable, "-V"]),
        "--json",
        "--no-reuse",
    ]
    if os.name == "nt" and os.environ.get("BRIGADE_WINDOWS_TEST_PROGRESS"):
        # The affected-tests driver contains the entire test in a job that
        # forbids breakaway. Keep the detached worker in that outer job while
        # testing the actual console detach, lock and verify-parent exit.
        argv[1:3] = [
            "-c",
            "from brigade import cli, proc; "
            "flags = proc.detached_launch_kwargs; "
            "proc.detached_launch_kwargs = lambda **kw: flags(job_breakaway=False); "
            "cli.process_main()",
        ]
    deadline = time.monotonic() + 90

    def wait_until(predicate):
        while not predicate():
            assert (reason := code_graph_background.last_exit_reason(repo)) is None, reason
            assert time.monotonic() < deadline, "detached graph refresh did not reach the expected state"
            time.sleep(0.02)

    try:
        result = subprocess.run(argv, capture_output=True, text=True, check=True, timeout=40)
        first = json.loads(result.stdout)["code_graph_delta"]
        assert first["status"] == "sync_deferred"
        assert first["background_refresh"]["status"] in ({"started", "busy"} if os.name == "nt" else {"started"})
        wait_until(started.exists)
        assert not release.exists() and not reservation.exists()
        result = subprocess.run(argv, capture_output=True, text=True, check=True, timeout=40)
        assert json.loads(result.stdout)["code_graph_delta"]["background_refresh"]["status"] == "busy"
        assert "detached worker stdout" not in result.stdout and "detached worker stderr" not in result.stderr
    finally:
        release.touch()
    wait_until(
        lambda: code_graph_background.index_ready(db) and code_graph_background.status_or_start(repo, db) is None
    )
    assert not reservation.exists()


@pytest.mark.parametrize("launcher_alive", [True, False])
def test_worker_claims_launcher_reservation_with_different_pid(tmp_path, monkeypatch, launcher_alive):
    lock = code_graph_background._lock_path(tmp_path)
    reservation = lock.with_suffix(".json")
    claim = {"pid": 12345, "created": time.time(), "started": None, "token": "claim-token"}
    localio.write_json(reservation, claim)
    monkeypatch.setattr(os, "getpid", lambda: 54321)
    monkeypatch.setattr(proc_group, "process_start_time", lambda pid: None)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: pid == 12345 and launcher_alive)
    assert code_graph_background._reservation_live(claim) is launcher_alive

    calls = []

    def sync(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, wait=lambda **kwargs: 0)

    monkeypatch.setattr(subprocess, "run", sync)
    monkeypatch.setattr(subprocess, "Popen", sync)
    code_graph_background._worker(str(lock), "engine", "db", str(tmp_path), claim["token"])
    assert calls == [["engine", "--db", "db", "sync", str(tmp_path)]]
    assert not reservation.exists()
    assert not lock.with_suffix(".exit.json").exists()


@pytest.mark.parametrize(
    ("holder", "token", "previous_exit"),
    [(None, "superseded", None), ("replacement", "superseded", "current failure"), ("holder", "", None)],
)
def test_worker_without_claim_preserves_latest_exit(tmp_path, monkeypatch, holder, token, previous_exit):
    lock = code_graph_background._lock_path(tmp_path)
    reservation = lock.with_suffix(".json")
    claim = {"token": holder} if holder else None
    if claim:
        localio.write_json(reservation, claim)
    if previous_exit:
        localio.write_json(lock.with_suffix(".exit.json"), {"reason": previous_exit})
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: pytest.fail("unowned refresh ran"))

    code_graph_background._worker(str(lock), "engine", "db", str(tmp_path), token)

    assert localio.read_json_dict(reservation) == claim
    expected = {"reason": previous_exit} if previous_exit else None
    assert localio.read_json_dict(lock.with_suffix(".exit.json")) == expected


def test_worker_lock_timeout_preserves_latest_exit(tmp_path, monkeypatch):
    lock = code_graph_background._lock_path(tmp_path)
    previous_exit = {"reason": "current failure"}
    localio.write_json(lock.with_suffix(".exit.json"), previous_exit)

    def busy(*args, **kwargs):
        raise inbox_lock.InboxLockTimeout("busy")

    monkeypatch.setattr(inbox_lock, "held_file_lock", busy)
    code_graph_background._worker(str(lock), "engine", "db", str(tmp_path), "claim-token")
    assert localio.read_json_dict(lock.with_suffix(".exit.json")) == previous_exit


def test_owned_windows_worker_keeps_lock_when_sync_exceeds_budget(tmp_path, monkeypatch, capsys, graph_sync_platform):
    graph_sync_platform("win32")
    lock = code_graph_background._lock_path(tmp_path)
    db = lock.parent / "code-graph.db"
    localio.write_json(lock.with_suffix(".json"), {"token": "claim-token"})
    localio.write_json(lock.with_suffix(".exit.json"), {"lock_refused_db": str(db), "reason": "old refusal"})
    waits = []

    def wait(*, timeout=None):
        waits.append(timeout)
        if timeout is not None:
            assert code_graph_background.last_engine_lock_refusal(tmp_path, db) is None
            raise subprocess.TimeoutExpired("engine", timeout)
        status = code_graph_background.status_or_start(tmp_path, db, "engine")
        assert status["status"] == "busy" and "still running" in status["reason"]
        assert status["engine_pid"] == 12345 and status["started_at"] <= time.time()
        assert status["elapsed_seconds"] >= 0
        assert code_graph_background.last_exit_reason(tmp_path) is None
        assert code_cmd.run("doctor", ["--target", str(tmp_path)]) == 0
        assert "engine PID 12345" in capsys.readouterr().err
        return 0

    def start_engine(argv, **kwargs):
        assert kwargs["creationflags"] == subprocess.CREATE_NO_WINDOW
        assert kwargs["stderr"] == subprocess.PIPE
        return SimpleNamespace(wait=wait, pid=12345, stderr=BytesIO())

    monkeypatch.setattr(proc, "run", lambda *args, **kwargs: proc.Result(0, "", ""))
    monkeypatch.setattr(component_bins, "resolve", lambda name: "engine")
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(subprocess, "Popen", start_engine)
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: pytest.fail("killable worker sync"))
    monkeypatch.setattr(code_graph_background, "_ignored_for_refresh", lambda *args: True)
    monkeypatch.setattr(proc, "spawn_detached", lambda *args, **kwargs: pytest.fail("duplicate writer"))

    code_graph_background._worker(str(lock), "engine", str(db), str(tmp_path), "claim-token")

    assert waits == [120, None]
    assert code_graph_background.status_or_start(tmp_path, db) is None
    assert not lock.with_suffix(".exit.json").exists()


@pytest.mark.parametrize(
    ("created", "mtime", "actual_start", "alive", "busy"),
    [
        (1010, 1000, 7, True, False),  # Future timestamp disagrees with its file.
        (1061, 1061, 7, True, False),  # Even matching metadata has a bounded window.
        (939, 939, 7, True, False),  # Expired.
        (980, 980, 8, True, False),  # Reused PID.
        (980, 980, None, True, True),  # Creation-time query unavailable.
        (980, 980, 7, False, False),  # Exited launcher with a readable handle.
        (1010, 1010, 7, True, True),  # Clock rolled back with the same live launcher.
        (1010, 1010, None, True, False),  # A PID alone cannot explain future time.
    ],
)
def test_windows_observes_startup_reservation_without_relaunch(
    tmp_path, monkeypatch, created, mtime, actual_start, alive, busy
):
    monkeypatch.setattr(code_graph_background, "sys", SimpleNamespace(platform="win32", executable=sys.executable))
    monkeypatch.setattr(code_graph_background, "time", SimpleNamespace(time=lambda: 1000))
    monkeypatch.setattr(proc_group, "process_start_time", lambda pid: None)
    monkeypatch.setattr(nt_process, "process_creation_time", lambda pid: actual_start, raising=False)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: pid == 12345 and alive)
    lock = code_graph_background._lock_path(tmp_path)
    reservation = lock.with_suffix(".json")
    claim = {"pid": 12345, "created": created, "started": 7, "token": "first"}
    localio.write_json(reservation, claim)
    os.utime(reservation, (mtime, mtime))
    lock.touch()

    if busy:
        monkeypatch.setattr(proc, "spawn_detached", lambda *args, **kwargs: pytest.fail("duplicate launch"))
    db = lock.parent / "code-graph.db"
    result = code_graph_background.status_or_start(tmp_path, db, "engine" if busy else None)
    assert result == ({"status": "busy", "reason": "background sync starting"} if busy else None)
    assert localio.read_json_dict(reservation) == claim


@pytest.mark.skipif(sys.platform != "win32", reason="requires Windows process handles")
def test_windows_reads_creation_time_from_a_real_process_handle():
    started = nt_process.process_creation_time(os.getpid())
    assert started is not None and started > 0
    assert nt_process.process_creation_time(os.getpid()) == started


def test_graph_engine_rejects_a_second_writer_before_sync(tmp_path):
    engine = shutil.which("brigade-code-graph")
    if engine is None:
        pytest.skip("requires the native code graph engine")
    db = tmp_path / "graph.db"
    (tmp_path / "source.py").write_text("def first():\n    pass\n")
    command = [engine, "--db", str(db), "sync", str(tmp_path)]
    subprocess.run(command, capture_output=True, check=True, timeout=30)
    lock = db.with_suffix(".db.lock")
    lock.write_text(f"{os.getpid()}\n")
    (tmp_path / "source.py").write_text("def second():\n    pass\n")

    refused = subprocess.run(command, capture_output=True, text=True, check=False, timeout=30)

    assert refused.returncode != 0
    assert "another sync is already running" in refused.stderr
    with closing(sqlite3.connect(db)) as connection:
        symbols = connection.execute("SELECT name FROM symbols").fetchall()
    assert ("first",) in symbols
    assert ("second",) not in symbols


@pytest.mark.parametrize("has_old_exit", [False, True])
def test_ready_graph_ignores_old_exit_and_observes_without_git(
    tmp_path, monkeypatch, has_old_exit, graph_sync_platform
):
    graph_sync_platform("linux")
    lock = code_graph_background._lock_path(tmp_path)
    db = lock.parent / "code-graph.db"
    reservation = lock.with_suffix(".json")
    old_claim = {"pid": 12345, "created": 0, "started": None}
    localio.write_json(reservation, old_claim)
    lock.touch()
    if has_old_exit:
        localio.write_json(lock.with_suffix(".exit.json"), {"reason": "previous failure"})
    with closing(sqlite3.connect(db)) as connection, connection:
        connection.execute("CREATE TABLE meta (key TEXT, value TEXT)")
        connection.execute("INSERT INTO meta VALUES ('synced_at', '1')")

    monkeypatch.setattr(component_bins, "resolve", lambda name: str(tmp_path / "nonexistent-engine"))
    monkeypatch.setattr(code_graph_background, "_ignored_for_refresh", lambda *args: pytest.fail("observed via git"))
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    before = code_graph_delta.capture_before(tmp_path, run_dir, defer_sync=True)
    delta = code_graph_delta.capture_after_and_diff(tmp_path, run_dir, before, sync_timeout=0.5)

    assert delta["status"] == "sync_failed"
    assert "background_refresh_last_exit" not in delta
    assert localio.read_json_dict(reservation) == old_claim
    assert lock.with_suffix(".exit.json").exists() is has_old_exit


def test_dead_launcher_replacement_and_late_worker_stay_silent(tmp_path, monkeypatch):
    lock = code_graph_background._lock_path(tmp_path)
    db = lock.parent / "code-graph.db"
    reservation = lock.with_suffix(".json")
    localio.write_json(reservation, {"pid": 12345, "created": time.time(), "started": None, "token": "old"})
    lock.touch()
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: False)
    monkeypatch.setattr(code_graph_background, "_ignored_for_refresh", lambda *args: True)
    monkeypatch.setattr(
        proc, "spawn_detached", lambda *args, **kwargs: (SimpleNamespace(pid=54321, wait=lambda: 0), "detached")
    )

    assert code_graph_background.status_or_start(tmp_path, db, "engine") == {"status": "started"}
    replacement = localio.read_json_dict(reservation)
    assert replacement["pid"] == 54321
    assert not lock.with_suffix(".exit.json").exists()

    calls = []

    def sync(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, wait=lambda **kwargs: 0)

    monkeypatch.setattr(subprocess, "run", sync)
    monkeypatch.setattr(subprocess, "Popen", sync)
    code_graph_background._worker(str(lock), "engine", str(db), str(tmp_path), "old")
    assert localio.read_json_dict(reservation) == replacement
    assert not lock.with_suffix(".exit.json").exists()
    code_graph_background._worker(str(lock), "engine", str(db), str(tmp_path), replacement["token"])
    assert calls == [["engine", "--db", str(db), "sync", str(tmp_path)]]
    assert not reservation.exists()
    assert not lock.with_suffix(".exit.json").exists()


def test_background_exit_reason_drops_controls_and_is_bounded(tmp_path):
    lock = code_graph_background._lock_path(tmp_path)
    localio.write_json(lock.with_suffix(".exit.json"), {"reason": "\x1b" + "x" * 500 + "\r\n"})
    reason = code_graph_background.last_exit_reason(tmp_path)
    assert reason == "x" * 160


@pytest.fixture
def graph_sync_platform(monkeypatch):
    def select(platform):
        monkeypatch.setattr(
            code_graph_background,
            "may_run_killable_foreground_sync",
            lambda: platform != "win32",
        )

    return select


@pytest.mark.parametrize(
    ("platform", "detach_refused"),
    [("win32", False), ("win32", True), ("linux", False)],
)
def test_verify_existing_graph_sync_policy(
    tmp_path, monkeypatch, capsys, graph_sync_platform, platform, detach_refused
):
    graph_sync_platform(platform)
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text(".brigade/\n")
    db = engine_paths.code_graph_db(tmp_path).path
    db.parent.mkdir(parents=True)
    with closing(sqlite3.connect(db)) as connection, connection:
        connection.execute("CREATE TABLE meta (key TEXT, value TEXT)")
        connection.execute("INSERT INTO meta VALUES ('synced_at', '1')")

    monkeypatch.setattr(component_bins, "resolve", lambda name: "engine")
    launches = []

    def launch(*args, **kwargs):
        launches.append(args)
        if detach_refused:
            raise proc.DetachedLaunchError("detach refused")
        return SimpleNamespace(pid=12345, wait=lambda: 0), "detached"

    monkeypatch.setattr(proc, "spawn_detached", launch)
    monkeypatch.setattr(code_graph_background, "_process_start_time", lambda pid: None)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: pid == 12345)
    calls = []
    real_run = subprocess.run

    (tmp_path / "source.py").write_text("new source")
    with closing(sqlite3.connect(db)) as connection, connection:
        connection.execute("INSERT INTO meta VALUES ('indexed_source', 'old source')")
    if platform == "win32" and not detach_refused:
        previous = code_graph_background._lock_path(tmp_path).with_suffix(".exit.json")
        localio.write_json(previous, {"lock_refused_db": str(db.resolve()), "reason": "old refusal"})

    def run_engine(argv, *args, **kwargs):
        if isinstance(argv, list) and argv[:1] == ["engine"]:
            calls.append(argv[3])
            if argv[3] == "diff" and platform == "win32":
                for flag in ("--before", "--after"):
                    with closing(sqlite3.connect(argv[argv.index(flag) + 1])) as snapshot:
                        assert snapshot.execute("SELECT value FROM meta WHERE key = 'indexed_source'").fetchone() == (
                            "old source",
                        )
            return subprocess.CompletedProcess(argv, 0, stdout="{}", stderr="")
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run_engine)
    assert work_cmd.verify_run(target=tmp_path, commands=[[sys.executable, "-V"]], json_output=True, reuse=False) == 0
    receipt = json.loads(capsys.readouterr().out)["code_graph_delta"]

    assert receipt["status"] == "ok"
    if platform == "linux":
        assert calls == ["sync", "sync", "diff"]
        assert not launches
        assert not receipt["stale_graph_used"]
    else:
        assert calls == ["diff"]
        assert len(launches) == 1
        assert receipt["stale_graph_used"] and receipt["stale_after"]
        assert receipt["commands"]["before_sync"] is None
        assert receipt["commands"]["after_sync"] is None
        assert "graph_sync_reason" not in receipt
        if detach_refused:
            assert receipt["background_refresh"]["failure_kind"] == "detach_refused"


@pytest.mark.parametrize("unready_file", [False, True])
def test_windows_first_build_keeps_running_after_detach_refusal(
    tmp_path, monkeypatch, graph_sync_platform, unready_file
):
    graph_sync_platform("win32")
    db = engine_paths.code_graph_db(tmp_path).path
    if unready_file:
        db.parent.mkdir(parents=True)
        db.touch()
    monkeypatch.setattr(component_bins, "resolve", lambda name: "engine")
    monkeypatch.setattr(code_graph_background, "_ignored_for_refresh", lambda *args: True)
    monkeypatch.setattr(code_graph_background, "_process_start_time", lambda pid: None)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: pid == 12345)

    def refuse(*args, **kwargs):
        raise proc.DetachedLaunchError("detach refused")

    monkeypatch.setattr(proc, "spawn_detached", refuse)
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: pytest.fail("killable first sync"))
    launches, waits, reapers = [], [], []

    def wait(*, timeout=None):
        waits.append(timeout)
        raise subprocess.TimeoutExpired("worker", timeout)

    def start_worker(argv, **kwargs):
        launches.append((argv, kwargs))
        return SimpleNamespace(pid=12345, wait=wait)

    def reaper(*, target, daemon):
        return SimpleNamespace(start=lambda: reapers.append(target))

    monkeypatch.setattr(subprocess, "Popen", start_worker)
    monkeypatch.setattr(code_graph_background, "Thread", reaper)
    monkeypatch.setattr(
        proc,
        "detached_launch_kwargs",
        lambda *, job_breakaway: {"creationflags": 8 if job_breakaway else 4},
    )
    run_dir = tmp_path / "run"
    before = code_graph_delta.capture_before(tmp_path, run_dir, defer_sync=True, snapshot_timeout=10)
    receipt = code_graph_delta.capture_after_and_diff(tmp_path, run_dir, before, sync_timeout=0.5)

    assert waits == [10]
    assert reapers == [wait]
    assert len(launches) == 1 and launches[0][1]["creationflags"] == 4
    assert receipt["status"] == "sync_deferred"
    assert before["background_refresh"]["status"] == "busy"
    assert receipt["background_refresh"]["in_progress"] is True
    assert receipt["foreground_fallback"]["reason"] == "detached launch refused"
    assert code_graph_background.status_or_start(tmp_path, db, "engine")["status"] == "busy"


def test_windows_engine_lock_refusal_is_reported_in_receipt_and_doctor(
    tmp_path, monkeypatch, capsys, graph_sync_platform
):
    graph_sync_platform("win32")
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    lock = code_graph_background._lock_path(tmp_path)
    db = engine_paths.code_graph_db(tmp_path).path
    engine_lock = db.with_name(db.name + ".lock")
    localio.write_json(lock.with_suffix(".json"), {"token": "claim-token"})
    engine_lock.write_text("12345\n")
    stderr = b"noise\n" * 4000 + b"Error: another sync is already running for this database\n"

    def refuse_sync(argv, **kwargs):
        return SimpleNamespace(wait=lambda **kw: 1, stderr=BytesIO(stderr))

    monkeypatch.setattr(subprocess, "Popen", refuse_sync)
    monkeypatch.setattr(subprocess, "run", lambda argv, **kwargs: subprocess.CompletedProcess(argv, 1))
    code_graph_background._worker(str(lock), "engine", str(db), str(tmp_path), "claim-token")
    monkeypatch.setattr(component_bins, "resolve", lambda name: "engine")
    monkeypatch.setattr(code_graph_background, "_ignored_for_refresh", lambda *args: True)
    monkeypatch.setattr(
        proc,
        "spawn_detached",
        lambda *args, **kwargs: (
            SimpleNamespace(pid=12345, wait=lambda: 0),
            "detached",
        ),
    )
    monkeypatch.setattr(code_graph_background, "_process_start_time", lambda pid: None)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: False)
    run_dir = tmp_path / "run"
    before = code_graph_delta.capture_before(tmp_path, run_dir, defer_sync=True)
    receipt = code_graph_delta.capture_after_and_diff(tmp_path, run_dir, before, sync_timeout=0.5)

    assert receipt["graph_sync_reason"] == "engine_lock_refused"
    assert "another sync" in receipt["background_refresh_last_exit"]

    monkeypatch.setattr(proc, "run", lambda *args, **kwargs: proc.Result(0, "STALE\n", ""))
    assert code_cmd.run("doctor", ["--target", str(tmp_path), "--db", str(db)]) == 0
    doctor = capsys.readouterr()
    assert doctor.out == "STALE\n"
    assert str(engine_lock) in doctor.err
    assert "Confirm no code-graph engine process is running" in doctor.err
    assert "delete" in doctor.err and "one hour" in doctor.err
    assert engine_lock.is_file()


def test_windows_explicit_sync_returns_with_live_engine(tmp_path, monkeypatch, capsys, graph_sync_platform):
    graph_sync_platform("win32")
    monkeypatch.setattr(component_bins, "resolve", lambda name: "engine")
    monkeypatch.setattr(proc, "run", lambda *args, **kwargs: pytest.fail("killable sync"))
    launched = []

    def start(argv, **kwargs):
        launched.append(argv)

        def wait(*, timeout=None):
            if timeout is not None:
                raise subprocess.TimeoutExpired(argv, timeout)
            return 0

        return SimpleNamespace(pid=12345, wait=wait)

    monkeypatch.setattr(subprocess, "Popen", start)
    assert code_cmd.run("sync", ["--target", str(tmp_path), "--force"]) == 124
    assert launched == [["engine", "sync", "--force"]]
    stderr = capsys.readouterr().err
    assert "still running" in stderr and "12345" in stderr and "brigade code doctor" in stderr


def test_windows_dependency_hook_sync_does_not_kill_writer(tmp_path, monkeypatch, graph_sync_platform):
    from brigade.claude_hooks import dependencies

    graph_sync_platform("win32")
    db = engine_paths.code_graph_db(tmp_path).path
    db.parent.mkdir(parents=True)
    db.touch()
    monkeypatch.setattr(component_bins, "resolve", lambda name: "engine")
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: pytest.fail("killable hook sync"))

    def wait(*, timeout=None):
        if timeout is not None:
            raise subprocess.TimeoutExpired("engine", timeout)
        return 0

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: SimpleNamespace(pid=12345, wait=wait))
    assert dependencies._dependents(tmp_path, "source.py") == (None, "sync_timed_out")


def test_windows_aboyeur_snapshot_defaults_defer_to_one_refresh(tmp_path, monkeypatch, graph_sync_platform):
    graph_sync_platform("win32")
    db = engine_paths.code_graph_db(tmp_path).path
    db.parent.mkdir(parents=True)
    with closing(sqlite3.connect(db)) as connection, connection:
        connection.execute("CREATE TABLE meta (key TEXT, value TEXT)")
        connection.execute("INSERT INTO meta VALUES ('synced_at', '1')")
    monkeypatch.setattr(component_bins, "resolve", lambda name: "engine")
    launches = []

    def refresh(target, database, binary=None, **kwargs):
        if binary is not None:
            launches.append(binary)
            return {"status": "started"}
        return {"status": "busy", "reason": "background sync lock held"}

    def run_engine(argv, **kwargs):
        assert argv[3] == "diff"
        return subprocess.CompletedProcess(argv, 0, stdout="{}", stderr="")

    monkeypatch.setattr(code_graph_background, "status_or_start", refresh)
    monkeypatch.setattr(subprocess, "run", run_engine)
    run_dir = tmp_path / "run"
    before = code_graph_delta.capture_before(tmp_path, run_dir)
    result = code_graph_delta.capture_after_and_diff(tmp_path, run_dir, before)
    assert result["status"] == "ok" and result["stale_graph_used"]
    assert launches == ["engine"]
    assert result["background_refresh"]["status"] == "busy"
