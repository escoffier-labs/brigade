import json
import os
import shlex
import sqlite3
import subprocess
import sys
import time
from contextlib import closing, contextmanager
from types import SimpleNamespace

import pytest

from brigade import code_graph_background, code_graph_delta, component_bins, inbox_lock, localio, proc, proc_group
from brigade import runguard


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
        assert json.loads(result.stdout)["code_graph_delta"]["background_refresh"]["status"] == "started"
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
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", sync)
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


def test_owned_windows_worker_hides_console_and_records_timeout_under_lock(tmp_path, monkeypatch):
    lock = code_graph_background._lock_path(tmp_path)
    localio.write_json(lock.with_suffix(".json"), {"token": "claim-token"})
    real_lock = inbox_lock.held_file_lock
    held = False

    @contextmanager
    def track_lock(*args, **kwargs):
        nonlocal held
        with real_lock(*args, **kwargs):
            held = True
            try:
                yield
            finally:
                held = False

    def timeout(argv, **kwargs):
        assert kwargs["creationflags"] == subprocess.CREATE_NO_WINDOW
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    def record_exit(path, reason):
        assert held
        assert path == lock and reason == "graph sync timed out"
        reasons.append(reason)

    reasons = []
    monkeypatch.setattr(inbox_lock, "held_file_lock", track_lock)
    monkeypatch.setattr(code_graph_background, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(subprocess, "run", timeout)
    monkeypatch.setattr(code_graph_background, "_record_exit", record_exit)
    code_graph_background._worker(str(lock), "engine", "db", str(tmp_path), "claim-token")
    assert reasons == ["graph sync timed out"]


@pytest.mark.parametrize(("age", "launcher_alive", "busy"), [(20, True, True), (20, False, False), (61, True, False)])
def test_windows_startup_window_survives_a_wall_clock_jump(monkeypatch, age, launcher_alive, busy):
    monkeypatch.setattr(code_graph_background, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(code_graph_background, "time", SimpleNamespace(time=lambda: 50, monotonic=lambda: 100))
    monkeypatch.setattr(proc_group, "process_start_time", lambda pid: None)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: pid == 12345 and launcher_alive)
    claim = {"pid": 12345, "created": 1000, "created_monotonic": 100 - age, "started": None}
    assert code_graph_background._reservation_live(claim) is busy


@pytest.mark.parametrize("has_old_exit", [False, True])
def test_ready_graph_ignores_old_exit_and_observes_without_git(tmp_path, monkeypatch, has_old_exit):
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


def test_replacing_stale_reservation_on_ready_index_does_not_record_an_exit(tmp_path, monkeypatch):
    lock = code_graph_background._lock_path(tmp_path)
    db = lock.parent / "code-graph.db"
    reservation = lock.with_suffix(".json")
    localio.write_json(reservation, {"pid": 12345, "created": 0, "started": None})
    lock.touch()
    monkeypatch.setattr(code_graph_background, "index_ready", lambda path: path == db)
    monkeypatch.setattr(code_graph_background, "_ignored_for_refresh", lambda *args: True)
    monkeypatch.setattr(
        proc, "spawn_detached", lambda *args, **kwargs: (SimpleNamespace(pid=54321, wait=lambda: 0), "detached")
    )

    assert code_graph_background.status_or_start(tmp_path, db, "engine") == {"status": "started"}
    assert localio.read_json_dict(reservation)["pid"] == 54321
    assert not lock.with_suffix(".exit.json").exists()


def test_background_exit_reason_drops_controls_and_is_bounded(tmp_path):
    lock = code_graph_background._lock_path(tmp_path)
    localio.write_json(lock.with_suffix(".exit.json"), {"reason": "\x1b" + "x" * 500 + "\r\n"})
    reason = code_graph_background.last_exit_reason(tmp_path)
    assert reason == "x" * 160
