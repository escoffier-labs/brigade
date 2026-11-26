import json
import os
import shlex

import pytest

from brigade import proc
from brigade import runguard
from brigade.native_child_custody import EnrollmentError, owned_run


def _repo(tmp_path):
    repo = tmp_path / "repo with spaces"
    repo.mkdir()
    for args in (
        ("init",),
        ("config", "user.email", "test@example.invalid"),
        ("config", "user.name", "Test User"),
    ):
        result = proc.run(["git", *args], cwd=repo)
        assert result.code == 0, result.stderr
    (repo / "tracked.txt").write_text("base\n")
    result = proc.run(["git", "add", "tracked.txt"], cwd=repo)
    assert result.code == 0, result.stderr
    result = proc.run(["git", "commit", "-m", "initial"], cwd=repo)
    assert result.code == 0, result.stderr
    return repo


def _write_owner(lock, *, pid, run_dir):
    lock.mkdir(parents=True)
    (lock / "pid").write_text(f"{pid}\n")
    (lock / "owner.json").write_text(
        json.dumps(
            {
                "schema": "brigade.run_lock.v1",
                "owner_token": "test-owner",
                "pid": pid,
                "run_dir": str(run_dir.resolve()),
                "acquired_at": "2026-09-02T00:00:00+00:00",
            }
        )
    )


@pytest.mark.parametrize("native", [False, True])
def test_claim_wait_refuses_stale_foreign_lock_without_moving_it(tmp_path, monkeypatch, native):
    repo = _repo(tmp_path)
    run_dir = repo / ".brigade" / "runs" / "current"
    foreign_run = run_dir.with_name("foreign")
    lock = runguard.lock_path(repo)
    _write_owner(lock, pid=43210, run_dir=foreign_run)
    identity = lock.stat().st_ino
    files = {path.name: path.read_bytes() for path in lock.iterdir()}
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: pid != 43210)
    acquire = runguard._acquire_lock
    rename = type(lock).rename
    attempts = 0
    moves = []

    def count_acquire(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        assert attempts < 5
        return acquire(*args, **kwargs)

    def record_rename(path, target):
        if path == lock:
            moves.append(target)
        return rename(path, target)

    monkeypatch.setattr(runguard, "_acquire_lock", count_acquire)
    monkeypatch.setattr(type(lock), "rename", record_rename)

    if native:
        with pytest.raises(EnrollmentError, match="stale run lock does not belong to this run") as exc:
            with owned_run(repo, run_dir, wait_seconds=1):
                pytest.fail("acquired another run's lock")
        assert exc.value.code == "run-owner"
    else:
        with pytest.raises(runguard.RunLockError, match="stale run lock does not belong to this run"):
            with runguard.run_lock(repo, run_dir=run_dir, wait_seconds=1, stale_action="claim"):
                pytest.fail("acquired another run's lock")

    assert attempts == 1
    assert moves == []
    assert lock.stat().st_ino == identity
    assert {path.name: path.read_bytes() for path in lock.iterdir()} == files
    assert not list(lock.parent.glob(f".{lock.name}.*.stale"))


def test_run_lock_wait_fails_immediately_for_retained_claim_with_recovery_command(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = repo / ".brigade" / "runs" / "abandoned-run"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps({"status": "dispatching"}))
    lifecycle = run_dir / "events" / "lifecycle.jsonl"
    lifecycle.parent.mkdir()
    lifecycle.write_text("{}\n")
    lock_path = runguard.lock_path(repo)
    retained = lock_path.with_name(f".{lock_path.name}.retained-dead.stale")
    _write_owner(retained, pid=43210, run_dir=run_dir)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: pid == os.getpid())
    monotonic = iter((0.0, 0.0, 60.1))
    monkeypatch.setattr(runguard.time, "monotonic", lambda: next(monotonic))
    sleeps = []
    monkeypatch.setattr(runguard.time, "sleep", sleeps.append)

    with pytest.raises(runguard.RunLockError) as exc:
        with runguard.run_lock(repo, wait_seconds=60.0, poll_interval=0.1):
            pass

    message = str(exc.value)
    retained_claims = list(lock_path.parent.glob(f".{lock_path.name}.retained-*.stale"))
    assert len(retained_claims) == 1
    assert str(retained_claims[0]) in message
    assert str(run_dir.resolve()) in message
    assert "43210" in message
    expected = ["brigade", "runs", "recover", "--cwd", str(repo.resolve()), "abandoned-run"]
    command = message.split("recover with: ", 1)[1]
    assert shlex.split(command) == expected
    assert sleeps == []


def test_run_lock_wait_timeout_reports_live_owner_details(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = repo / ".brigade" / "runs" / "live-run"
    run_dir.mkdir(parents=True)
    _write_owner(runguard.lock_path(repo), pid=43210, run_dir=run_dir)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: True)
    monotonic = iter((10.0, 10.0, 10.25))
    sleeps = []
    monkeypatch.setattr(runguard.time, "monotonic", lambda: next(monotonic))
    monkeypatch.setattr(runguard.time, "sleep", sleeps.append)

    with pytest.raises(runguard.RunLockError) as exc:
        with runguard.run_lock(repo, wait_seconds=0.2, poll_interval=0.05):
            pass

    message = str(exc.value)
    assert "timed out after 0.2s waiting for run lock" in message
    assert "owner pid 43210" in message
    assert str(run_dir.resolve()) in message
    assert "pid alive at last check: yes" in message
    assert sleeps == [0.05]
