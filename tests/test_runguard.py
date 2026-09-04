from __future__ import annotations

import errno
import json
import math
import os
import shutil
import threading
import time as stdlib_time
from pathlib import Path

import pytest

from brigade import proc
from brigade import runguard


def _git(repo, *args):
    result = proc.run(["git", *args], cwd=repo)
    assert result.code == 0, result.stderr
    return result


def _repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test User")
    (repo / "tracked.txt").write_text("base\n")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "initial")
    return repo


def test_dirty_paths_reports_modified_and_untracked_files(tmp_path):
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_text("changed\n")
    (repo / "new.txt").write_text("new\n")

    assert runguard.dirty_paths(repo) == ["new.txt", "tracked.txt"]


def test_dirty_paths_rejects_non_git_directory(tmp_path):
    with pytest.raises(runguard.RunGuardError, match="not a git worktree"):
        runguard.dirty_paths(tmp_path)


def test_require_clean_worktree_allows_clean_repo(tmp_path):
    repo = _repo(tmp_path)

    assert runguard.require_clean_worktree(repo) == []


def test_require_clean_worktree_blocks_dirty_repo(tmp_path):
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_text("changed\n")

    with pytest.raises(runguard.DirtyWorktreeError) as exc:
        runguard.require_clean_worktree(repo)

    assert exc.value.paths == ["tracked.txt"]
    assert "Commit, stash, or clean the tree" in str(exc.value)
    assert "--allow-dirty" not in str(exc.value)


def test_run_lock_rejects_lock_held_by_live_process(tmp_path):
    repo = _repo(tmp_path)
    lock_path = runguard.lock_path(repo)
    lock_path.mkdir(parents=True)
    (lock_path / "pid").write_text(f"{os.getpid()}\n")

    with pytest.raises(runguard.RunLockError, match="another brigade run appears active"):
        with runguard.run_lock(repo):
            pass


def test_run_lock_reports_regular_file_lock_as_typed_error_and_preserves_it(tmp_path):
    repo = _repo(tmp_path)
    lock_path = runguard.lock_path(repo)
    lock_path.parent.mkdir(parents=True)
    lock_path.write_text("malformed lock\n")

    with pytest.raises(runguard.RunLockError, match="malformed run lock"):
        with runguard.run_lock(repo):
            pass

    assert lock_path.is_file()
    assert lock_path.read_text() == "malformed lock\n"


def test_acquire_lock_retries_when_release_removes_lock_during_type_probe(tmp_path, monkeypatch):
    lock_path = tmp_path / "run.lock"
    lock_path.mkdir()
    released_path = tmp_path / "released.lock"
    original_publish = runguard._publish_lock
    original_lstat = Path.lstat
    publish_calls = 0

    def publish_after_release(path, *, run_dir=None):
        nonlocal publish_calls
        publish_calls += 1
        if publish_calls == 1:
            raise FileExistsError(path)
        return original_publish(path, run_dir=run_dir)

    def release_during_type_probe(self, *args, **kwargs):
        if self == lock_path and publish_calls == 1:
            lock_path.rename(released_path)
            released_path.rmdir()
            raise FileNotFoundError(self)
        return original_lstat(self, *args, **kwargs)

    monkeypatch.setattr(runguard, "_publish_lock", publish_after_release)
    monkeypatch.setattr(Path, "lstat", release_during_type_probe)

    ownership = runguard._acquire_lock(lock_path)

    assert publish_calls == 2
    assert lock_path.is_dir()
    runguard._release_lock(lock_path, ownership)
    assert not lock_path.exists()


def test_run_lock_handles_windows_missing_process_error_as_stale(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    lock_path = runguard.lock_path(repo)
    lock_path.mkdir(parents=True)
    (lock_path / "pid").write_text("43210\n")
    missing_process = OSError("invalid process parameter")
    missing_process.winerror = 87
    monkeypatch.setattr(runguard.os, "kill", lambda *args: (_ for _ in ()).throw(missing_process))

    with runguard.run_lock(repo):
        assert (lock_path / "pid").read_text().strip() == str(os.getpid())

    assert not lock_path.exists()


def test_run_lock_publishes_complete_owner_metadata(tmp_path):
    repo = _repo(tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    lock_path = runguard.lock_path(repo)

    with runguard.run_lock(repo, run_dir=run_dir):
        owner = json.loads((lock_path / "owner.json").read_text())
        assert owner["schema"] == "brigade.run_lock.v1"
        assert owner["pid"] == os.getpid()
        assert owner["run_dir"] == str(run_dir.resolve())
        assert isinstance(owner["owner_token"], str) and owner["owner_token"]
        assert isinstance(owner["acquired_at"], str) and owner["acquired_at"]
        assert (lock_path / "pid").read_text().strip() == str(os.getpid())


def test_run_lock_is_retained_when_terminal_receipt_cannot_be_written(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "run.json").write_text(json.dumps({"status": "artifact-collection"}))
    lock_path = runguard.lock_path(repo)

    with pytest.raises(runguard.RetainRunLockError, match="receipt disk full"):
        with runguard.run_lock(repo, run_dir=run_dir):
            raise runguard.RetainRunLockError("receipt disk full")

    assert lock_path.is_dir()
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: False)
    assert runguard.recover_stale_run(repo, run_dir) is True
    assert not lock_path.exists()
    recovered = json.loads((run_dir / "run.json").read_text())
    assert recovered["status"] == "failed"
    assert recovered["failure"]["prior_status"] == "artifact-collection"


def test_recover_stale_dispatching_run_attributes_active_seat(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "status": "dispatching",
                "active_seats": ["coder"],
                "phase_owner": "chef",
                "worker": "fallback-worker",
                "orchestrator": "fallback-chef",
            }
        )
    )

    result = runguard._recover_run_artifact({"run_dir": str(run_dir), "pid": 4321})

    assert result == "recovered"
    recovered = json.loads((run_dir / "run.json").read_text())
    assert recovered["failure"]["seat"] == "coder"


def test_run_lock_release_does_not_delete_replacement_owner(tmp_path):
    repo = _repo(tmp_path)
    lock_path = runguard.lock_path(repo)

    with runguard.run_lock(repo):
        replacement = lock_path.with_name("replacement.lock")
        replacement.mkdir()
        (replacement / "pid").write_text(f"{os.getpid()}\n")
        (replacement / "owner.json").write_text(
            json.dumps(
                {
                    "schema": "brigade.run_lock.v1",
                    "owner_token": "replacement-owner",
                    "pid": os.getpid(),
                    "run_dir": None,
                    "acquired_at": "2026-07-16T00:00:00+00:00",
                }
            )
        )
        runguard.shutil.rmtree(lock_path)
        replacement.rename(lock_path)

    assert lock_path.is_dir()
    assert json.loads((lock_path / "owner.json").read_text())["owner_token"] == "replacement-owner"


def test_run_lock_allows_only_one_concurrent_owner(tmp_path):
    repo = _repo(tmp_path)
    start = threading.Barrier(3)
    loser_finished = threading.Event()
    results = []

    def contend(name):
        start.wait()
        try:
            with runguard.run_lock(repo):
                results.append((name, "acquired"))
                assert loser_finished.wait(timeout=2.0)
        except runguard.RunLockError:
            results.append((name, "locked"))
            loser_finished.set()

    threads = [threading.Thread(target=contend, args=(name,)) for name in ("one", "two")]
    for thread in threads:
        thread.start()
    start.wait()
    for thread in threads:
        thread.join(timeout=3.0)

    assert not any(thread.is_alive() for thread in threads)
    assert sorted(result for _, result in results) == ["acquired", "locked"]
    assert not runguard.lock_path(repo).exists()


def test_run_lock_retries_when_concurrent_stale_claim_removes_visible_lock(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    lock_path = runguard.lock_path(repo)
    lock_path.mkdir(parents=True)
    (lock_path / "pid").write_text("99999999\n")
    original_claim = runguard._claim_stale_lock
    calls = 0

    def concurrent_claim(path):
        nonlocal calls
        calls += 1
        if calls == 1:
            runguard.shutil.rmtree(path)
            return None
        return original_claim(path)

    monkeypatch.setattr(runguard, "_claim_stale_lock", concurrent_claim)

    with runguard.run_lock(repo):
        assert lock_path.is_dir()

    assert calls == 1


def test_run_lock_waits_until_live_lock_clears(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    lock_path = runguard.lock_path(repo)
    lock_path.mkdir(parents=True)
    (lock_path / "pid").write_text(f"{os.getpid()}\n")
    sleeps = []

    def release_lock(delay):
        sleeps.append(delay)
        (lock_path / "pid").unlink()
        lock_path.rmdir()

    monkeypatch.setattr(runguard.time, "sleep", release_lock)

    with runguard.run_lock(repo, wait_seconds=1.0, poll_interval=0.05):
        assert (lock_path / "pid").read_text().strip() == str(os.getpid())

    assert sleeps == [0.05]
    assert not lock_path.exists()


def test_run_lock_wait_timeout_is_bounded(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    lock_path = runguard.lock_path(repo)
    lock_path.mkdir(parents=True)
    (lock_path / "pid").write_text(f"{os.getpid()}\n")
    monotonic = iter((10.0, 10.0, 10.25))
    sleeps = []
    monkeypatch.setattr(runguard.time, "monotonic", lambda: next(monotonic))
    monkeypatch.setattr(runguard.time, "sleep", sleeps.append)

    with pytest.raises(runguard.RunLockError, match=r"timed out after 0.2s waiting for run lock"):
        with runguard.run_lock(repo, wait_seconds=0.2, poll_interval=0.05):
            pass

    assert sleeps == [0.05]
    assert lock_path.is_dir()


def test_run_lock_timeout_clock_is_isolated_from_process_clock(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    lock_path = runguard.lock_path(repo)
    lock_path.mkdir(parents=True)
    (lock_path / "pid").write_text(f"{os.getpid()}\n")
    monotonic = iter((10.0, 10.0, 10.25))
    sleeps = []
    monkeypatch.setattr(runguard.time, "monotonic", lambda: next(monotonic))
    monkeypatch.setattr(runguard.time, "sleep", sleeps.append)

    stdlib_time.monotonic()
    with pytest.raises(runguard.RunLockError, match=r"timed out after 0.2s waiting for run lock"):
        with runguard.run_lock(repo, wait_seconds=0.2, poll_interval=0.05):
            pass

    assert sleeps == [0.05]
    assert lock_path.is_dir()


def test_run_lock_fair_queue_long_holder_multiple_waiters_proceed_in_order(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    order: list[str] = []
    holder_release = threading.Event()
    holder_acquired = threading.Event()
    enrollment_signals = [threading.Event() for _ in range(3)]
    enroll_index = 0
    enroll_lock = threading.Lock()
    original_enroll = runguard._enroll_wait_ticket

    def counting_enroll(queue_dir):
        nonlocal enroll_index
        ticket = original_enroll(queue_dir)
        with enroll_lock:
            enrollment_signals[enroll_index].set()
            enroll_index += 1
        return ticket

    monkeypatch.setattr(runguard, "_enroll_wait_ticket", counting_enroll)

    def holder() -> None:
        with runguard.run_lock(repo):
            order.append("holder-start")
            holder_acquired.set()
            assert holder_release.wait(timeout=5)
            order.append("holder-end")

    def waiter(name: str) -> None:
        with runguard.run_lock(repo, wait_seconds=math.inf, poll_interval=0.02):
            order.append(name)

    holder_thread = threading.Thread(target=holder)
    holder_thread.start()
    assert holder_acquired.wait(timeout=5)

    waiter_threads: list[threading.Thread] = []
    for index in range(3):
        thread = threading.Thread(target=waiter, args=(f"waiter-{index}",))
        thread.start()
        assert enrollment_signals[index].wait(timeout=5)
        waiter_threads.append(thread)

    holder_release.set()

    for thread in waiter_threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    holder_thread.join(timeout=10)
    assert not holder_thread.is_alive()

    assert order == ["holder-start", "holder-end", "waiter-0", "waiter-1", "waiter-2"]


def test_wait_to_acquire_lock_retries_when_lock_clears_after_failed_acquire(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    lock_path = runguard.lock_path(repo)
    lock_path.mkdir(parents=True)
    (lock_path / "pid").write_text(f"{os.getpid()}\n")
    original_acquire = runguard._acquire_lock
    acquire_calls = 0

    def acquire_then_clear(path, *, run_dir=None):
        nonlocal acquire_calls
        acquire_calls += 1
        if acquire_calls == 1:
            shutil.rmtree(lock_path)
            raise runguard.RunLockError("another brigade run appears active")
        return original_acquire(path, run_dir=run_dir)

    monkeypatch.setattr(runguard, "_acquire_lock", acquire_then_clear)

    with runguard.run_lock(repo, wait_seconds=1.0, poll_interval=0.05):
        assert acquire_calls == 2
        assert (lock_path / "pid").read_text().strip() == str(os.getpid())

    assert not lock_path.exists()


def test_wait_to_acquire_lock_still_surfaces_malformed_lock_after_failed_acquire(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    lock_path = runguard.lock_path(repo)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("malformed lock\n")

    def fail_acquire(path, *, run_dir=None):
        raise runguard.RunLockError("could not acquire run lock")

    monkeypatch.setattr(runguard, "_acquire_lock", fail_acquire)

    with pytest.raises(runguard.RunLockError, match="malformed run lock"):
        with runguard.run_lock(repo, wait_seconds=1.0, poll_interval=0.05):
            pass

    assert lock_path.is_file()
    assert lock_path.read_text() == "malformed lock\n"


def test_run_lock_wait_reclaims_stale_holder(tmp_path):
    repo = _repo(tmp_path)
    lock_path = runguard.lock_path(repo)
    lock_path.mkdir(parents=True)
    (lock_path / "pid").write_text("99999999\n")

    with runguard.run_lock(repo, wait_seconds=1.0, poll_interval=0.05):
        assert (lock_path / "pid").read_text().strip() == str(os.getpid())

    assert not lock_path.exists()


def test_run_lock_prunes_stale_wait_tickets_before_queue_head(tmp_path):
    repo = _repo(tmp_path)
    queue_dir = runguard._wait_queue_dir(runguard.lock_path(repo))
    queue_dir.mkdir(parents=True)
    dead_ticket = queue_dir / "00000000000000000001-00000001-dead.wait"
    dead_ticket.write_text(json.dumps({"pid": 99999999}))
    live_ticket = runguard._enroll_wait_ticket(queue_dir)

    assert runguard._is_wait_queue_head(live_ticket, queue_dir)
    assert not dead_ticket.exists()
    assert queue_dir.is_dir()


def test_run_lock_removes_empty_wait_queue_dir_when_last_ticket_leaves(tmp_path):
    repo = _repo(tmp_path)
    queue_dir = runguard._wait_queue_dir(runguard.lock_path(repo))
    ticket = runguard._enroll_wait_ticket(queue_dir)
    assert queue_dir.is_dir()

    runguard._leave_wait_ticket(ticket, queue_dir)
    assert not queue_dir.exists()


def test_run_lock_prune_removes_empty_wait_queue_dir(tmp_path):
    repo = _repo(tmp_path)
    queue_dir = runguard._wait_queue_dir(runguard.lock_path(repo))
    queue_dir.mkdir(parents=True)
    dead_ticket = queue_dir / "00000000000000000001-00000001-dead.wait"
    dead_ticket.write_text(json.dumps({"pid": 99999999}))

    runguard._prune_stale_wait_tickets(queue_dir)
    assert not dead_ticket.exists()
    assert not queue_dir.exists()


def test_wait_ticket_transient_read_error_is_not_pruned(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    queue_dir = runguard._wait_queue_dir(runguard.lock_path(repo))
    ticket = runguard._enroll_wait_ticket(queue_dir)
    original_read_text = Path.read_text
    calls = 0

    def flaky_read_text(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        if self == ticket and calls == 1:
            raise OSError(errno.EIO, "simulated transient read")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", flaky_read_text)

    runguard._prune_stale_wait_tickets(queue_dir)
    assert ticket.exists()
    assert runguard._wait_ticket_is_live(ticket)


def test_wait_ticket_enroll_survives_concurrent_prune_before_publish(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    queue_dir = runguard._wait_queue_dir(runguard.lock_path(repo))
    publish_ready = threading.Event()
    publish_gate = threading.Event()
    enrolling_files: list[Path] = []
    real_replace = os.replace

    def gated_replace(src, dst):
        if str(dst).endswith(".wait"):
            enrolling_files.append(Path(src))
            publish_ready.set()
            assert publish_gate.wait(timeout=5)
            listed = runguard._list_wait_tickets(queue_dir)
            assert listed == []
            runguard._prune_stale_wait_tickets(queue_dir)
            assert Path(src).exists()
            assert not Path(dst).exists()
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", gated_replace)

    enrolled: dict[str, object] = {}
    errors: list[BaseException] = []

    def enroll() -> None:
        try:
            ticket = runguard._enroll_wait_ticket(queue_dir)
        except BaseException as exc:
            errors.append(exc)
            return
        enrolled["ticket"] = ticket

    enroll_thread = threading.Thread(target=enroll)
    enroll_thread.start()
    assert publish_ready.wait(timeout=5)
    publish_gate.set()
    enroll_thread.join(timeout=5)
    assert not enroll_thread.is_alive()
    assert errors == []

    ticket = enrolled["ticket"]
    assert isinstance(ticket, Path)
    assert ticket.exists()
    assert runguard._wait_ticket_is_live(ticket)
    assert runguard._is_wait_queue_head(ticket, queue_dir)
    assert not any(queue_dir.glob("*.enrolling"))
    assert not enrolling_files or not enrolling_files[0].exists()

    runguard._leave_wait_ticket(ticket, queue_dir)


def test_wait_to_acquire_lock_timeout_before_immediate_retry_churn(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    lock_path = runguard.lock_path(repo)
    lock_path.mkdir(parents=True)
    (lock_path / "pid").write_text(f"{os.getpid()}\n")
    monotonic = iter((0.0, 0.0, 0.21))
    sleeps: list[float] = []
    monkeypatch.setattr(runguard.time, "monotonic", lambda: next(monotonic))
    monkeypatch.setattr(runguard.time, "sleep", sleeps.append)
    monkeypatch.setattr(runguard, "_is_wait_queue_head", lambda *_args, **_kwargs: True)

    def always_retry_acquire(*_args, **_kwargs):
        raise runguard.RunLockError("another brigade run appears active")

    monkeypatch.setattr(runguard, "_acquire_lock", always_retry_acquire)
    monkeypatch.setattr(runguard, "_wait_retry_after_acquire_error", lambda *_args, **_kwargs: True)

    with pytest.raises(runguard.RunLockError, match=r"timed out after 0.2s waiting for run lock"):
        with runguard.run_lock(repo, wait_seconds=0.2, poll_interval=0.05):
            pass

    assert sleeps == []


def test_wait_to_acquire_lock_retries_immediately_without_poll_sleep(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    lock_path = runguard.lock_path(repo)
    lock_path.mkdir(parents=True)
    (lock_path / "pid").write_text(f"{os.getpid()}\n")
    original_acquire = runguard._acquire_lock
    acquire_calls = 0
    sleeps = []
    monkeypatch.setattr(runguard.time, "sleep", sleeps.append)

    def acquire_then_clear(path, *, run_dir=None):
        nonlocal acquire_calls
        acquire_calls += 1
        if acquire_calls == 1:
            shutil.rmtree(lock_path)
            raise runguard.RunLockError("another brigade run appears active")
        return original_acquire(path, run_dir=run_dir)

    monkeypatch.setattr(runguard, "_acquire_lock", acquire_then_clear)

    with runguard.run_lock(repo, wait_seconds=1.0, poll_interval=0.05):
        assert acquire_calls == 2

    assert sleeps == []


def test_wait_to_acquire_lock_unbounded_immediate_retry_sleeps_between_retries(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    lock_path = runguard.lock_path(repo)
    lock_path.mkdir(parents=True)
    (lock_path / "pid").write_text(f"{os.getpid()}\n")
    original_acquire = runguard._acquire_lock
    acquire_calls = 0
    sleeps: list[float] = []
    monkeypatch.setattr(runguard.time, "sleep", sleeps.append)

    def fail_twice_then_succeed(path, *, run_dir=None):
        nonlocal acquire_calls
        acquire_calls += 1
        if acquire_calls <= 2:
            if lock_path.exists():
                shutil.rmtree(lock_path)
            raise runguard.RunLockError("another brigade run appears active")
        return original_acquire(path, run_dir=run_dir)

    monkeypatch.setattr(runguard, "_acquire_lock", fail_twice_then_succeed)

    with runguard.run_lock(repo, wait_seconds=math.inf, poll_interval=0.05):
        assert acquire_calls == 3

    assert sleeps == [0.05, 0.05]


def test_run_lock_replaces_lock_with_dead_pid(tmp_path):
    repo = _repo(tmp_path)
    lock_path = runguard.lock_path(repo)
    lock_path.mkdir(parents=True)
    (lock_path / "pid").write_text("99999999\n")

    with runguard.run_lock(repo):
        assert (lock_path / "pid").read_text().strip() == str(os.getpid())

    assert not lock_path.exists()


@pytest.mark.parametrize("pid_text", [None, "not-a-pid\n"])
def test_run_lock_preserves_live_owner_when_pid_sidecar_is_missing_or_corrupt(tmp_path, pid_text):
    repo = _repo(tmp_path)
    abandoned_run = tmp_path / "active-run"
    abandoned_run.mkdir()
    (abandoned_run / "run.json").write_text(json.dumps({"status": "dispatching"}))
    lock_path = runguard.lock_path(repo)
    lock_path.mkdir(parents=True)
    if pid_text is not None:
        (lock_path / "pid").write_text(pid_text)
    (lock_path / "owner.json").write_text(
        json.dumps({"owner_token": "live-owner", "pid": os.getpid(), "run_dir": str(abandoned_run.resolve())})
    )

    with pytest.raises(runguard.RunLockError, match="another brigade run appears active"):
        with runguard.run_lock(repo, run_dir=tmp_path / "new-run"):
            pass

    assert lock_path.is_dir()
    assert json.loads((abandoned_run / "run.json").read_text())["status"] == "dispatching"


@pytest.mark.parametrize("prior_status", ["result-processing", "artifact-collection"])
def test_run_lock_recovers_dead_owner_run_to_typed_terminal_state(tmp_path, prior_status):
    repo = _repo(tmp_path)
    abandoned_run = tmp_path / "abandoned-run"
    abandoned_run.mkdir()
    (abandoned_run / "run.json").write_text(
        json.dumps({"schema": "brigade.run.v1", "status": prior_status, "task": "inspect"})
    )
    lock_path = runguard.lock_path(repo)
    lock_path.mkdir(parents=True)
    (lock_path / "pid").write_text("99999999\n")
    (lock_path / "owner.json").write_text(
        json.dumps(
            {
                "schema": "brigade.run_lock.v1",
                "owner_token": "dead-owner",
                "pid": 99999999,
                "run_dir": str(abandoned_run.resolve()),
                "acquired_at": "2026-07-16T00:00:00+00:00",
            }
        )
    )

    with runguard.run_lock(repo, run_dir=tmp_path / "new-run"):
        recovered = json.loads((abandoned_run / "run.json").read_text())
        assert recovered["status"] == "failed"
        assert recovered["failure_phase"] == "stale-lock-recovery"
        assert recovered["failure"] == {
            "phase": "stale-lock-recovery",
            "kind": "owner-process-exited",
            "detail": "run owner process 99999999 is no longer active",
            "owner_pid": 99999999,
            "prior_status": prior_status,
            "recovered_at": recovered["failure"]["recovered_at"],
        }
        assert recovered["finished_at"] == recovered["failure"]["recovered_at"]
        assert recovered["task"] == "inspect"


def test_run_lock_keeps_stale_lock_when_failure_artifact_cannot_be_written(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    abandoned_run = tmp_path / "abandoned-run"
    abandoned_run.mkdir()
    (abandoned_run / "run.json").write_text(
        json.dumps({"schema": "brigade.run.v1", "status": "dispatching", "task": "inspect"})
    )
    lock_path = runguard.lock_path(repo)
    lock_path.mkdir(parents=True)
    (lock_path / "pid").write_text("99999999\n")
    (lock_path / "owner.json").write_text(
        json.dumps(
            {
                "schema": "brigade.run_lock.v1",
                "owner_token": "dead-owner",
                "pid": 99999999,
                "run_dir": str(abandoned_run.resolve()),
                "acquired_at": "2026-07-16T00:00:00+00:00",
            }
        )
    )
    monkeypatch.setattr(
        runguard.localio, "write_json", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full"))
    )

    with pytest.raises(runguard.RunLockError, match="could not preserve the stale run failure"):
        with runguard.run_lock(repo, run_dir=tmp_path / "new-run"):
            pass

    assert lock_path.is_dir()
    assert json.loads((lock_path / "owner.json").read_text())["owner_token"] == "dead-owner"
    assert json.loads((abandoned_run / "run.json").read_text())["status"] == "dispatching"


def test_run_lock_quarantines_unattributable_dead_owner_without_blocking_workspace(tmp_path):
    repo = _repo(tmp_path)
    lock_path = runguard.lock_path(repo)
    lock_path.mkdir(parents=True)
    (lock_path / "pid").write_text("99999999\n")
    (lock_path / "owner.json").write_text(
        json.dumps(
            {
                "schema": "brigade.run_lock.v1",
                "owner_token": "dead-owner",
                "pid": 99999999,
                "run_dir": None,
                "acquired_at": "2026-07-16T00:00:00+00:00",
            }
        )
    )

    with runguard.run_lock(repo, run_dir=tmp_path / "new-run"):
        assert lock_path.is_dir()

    assert not lock_path.exists()


@pytest.mark.parametrize("existing_run_json", [None, "not json"])
def test_run_lock_records_dead_owner_when_initial_run_json_is_unavailable(tmp_path, existing_run_json):
    repo = _repo(tmp_path)
    abandoned_run = tmp_path / "abandoned-run"
    abandoned_run.mkdir()
    if existing_run_json is not None:
        (abandoned_run / "run.json").write_text(existing_run_json)
    lock_path = runguard.lock_path(repo)
    lock_path.mkdir(parents=True)
    (lock_path / "pid").write_text("99999999\n")
    (lock_path / "owner.json").write_text(
        json.dumps(
            {
                "schema": "brigade.run_lock.v1",
                "owner_token": "dead-owner",
                "pid": 99999999,
                "run_dir": str(abandoned_run.resolve()),
                "acquired_at": "2026-07-16T00:00:00+00:00",
            }
        )
    )

    with runguard.run_lock(repo, run_dir=tmp_path / "new-run"):
        recovered = json.loads((abandoned_run / "run.json").read_text())
        assert recovered["status"] == "failed"
        assert recovered["failure"]["kind"] == "owner-process-exited"
        assert recovered["failure"]["prior_status"] == "artifact-unavailable"


def test_run_lock_finishes_abandoned_stale_claim_before_new_owner_enters(tmp_path):
    repo = _repo(tmp_path)
    abandoned_run = tmp_path / "abandoned-run"
    abandoned_run.mkdir()
    (abandoned_run / "run.json").write_text(
        json.dumps({"schema": "brigade.run.v1", "status": "dispatching", "task": "inspect"})
    )
    lock_path = runguard.lock_path(repo)
    claimed = lock_path.with_name(f".{lock_path.name}.crashed.stale")
    claimed.mkdir(parents=True)
    (claimed / "pid").write_text("99999999\n")
    (claimed / "owner.json").write_text(
        json.dumps(
            {
                "schema": "brigade.run_lock.v1",
                "owner_token": "dead-owner",
                "pid": 99999999,
                "run_dir": str(abandoned_run.resolve()),
                "acquired_at": "2026-07-16T00:00:00+00:00",
            }
        )
    )

    with runguard.run_lock(repo, run_dir=tmp_path / "new-run"):
        assert json.loads((abandoned_run / "run.json").read_text())["status"] == "failed"
    assert not claimed.exists()


def test_recover_stale_run_refuses_pending_claim_with_live_owner(tmp_path):
    repo = _repo(tmp_path)
    run_dir = tmp_path / "active-run"
    run_dir.mkdir()
    (run_dir / "run.json").write_text(json.dumps({"status": "dispatching"}))
    lock_path = runguard.lock_path(repo)
    claimed = lock_path.with_name(f".{lock_path.name}.recovering-99999999-dead.stale")
    claimed.mkdir(parents=True)
    (claimed / "pid").write_text(f"{os.getpid()}\n")
    (claimed / "owner.json").write_text(
        json.dumps({"owner_token": "live-owner", "pid": os.getpid(), "run_dir": str(run_dir.resolve())})
    )

    with pytest.raises(runguard.RunLockError, match="owner process is still active"):
        runguard.recover_stale_run(repo, run_dir)

    assert claimed.is_dir()
    assert json.loads((run_dir / "run.json").read_text())["status"] == "dispatching"


def test_recovery_preserves_non_object_run_json(tmp_path):
    repo = _repo(tmp_path)
    run_dir = tmp_path / "abandoned-run"
    run_dir.mkdir()
    (run_dir / "run.json").write_text("[]")
    lock_path = runguard.lock_path(repo)
    lock_path.mkdir(parents=True)
    (lock_path / "pid").write_text("99999999\n")
    (lock_path / "owner.json").write_text(
        json.dumps({"owner_token": "dead", "pid": 99999999, "run_dir": str(run_dir.resolve())})
    )

    assert runguard.recover_stale_run(repo, run_dir) is True
    recovered = json.loads((run_dir / "run.json").read_text())
    preserved = Path(recovered["recovery_preserved_artifact"])
    assert preserved.read_text() == "[]"


def test_run_lock_does_not_admit_new_owner_while_stale_recovery_is_in_progress(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    abandoned_run = tmp_path / "abandoned-run"
    abandoned_run.mkdir()
    (abandoned_run / "run.json").write_text(
        json.dumps({"schema": "brigade.run.v1", "status": "dispatching", "task": "inspect"})
    )
    lock_path = runguard.lock_path(repo)
    lock_path.mkdir(parents=True)
    (lock_path / "pid").write_text("99999999\n")
    (lock_path / "owner.json").write_text(
        json.dumps(
            {
                "schema": "brigade.run_lock.v1",
                "owner_token": "dead-owner",
                "pid": 99999999,
                "run_dir": str(abandoned_run.resolve()),
                "acquired_at": "2026-07-16T00:00:00+00:00",
            }
        )
    )
    recovery_started = threading.Event()
    finish_recovery = threading.Event()
    second_entered = threading.Event()
    original_recover = runguard._recover_run_artifact

    def paused_recover(owner, *, persist_recovery_provenance=False):
        recovery_started.set()
        assert finish_recovery.wait(timeout=2.0)
        return original_recover(owner, persist_recovery_provenance=persist_recovery_provenance)

    monkeypatch.setattr(runguard, "_recover_run_artifact", paused_recover)

    def first_owner():
        with runguard.run_lock(repo, run_dir=tmp_path / "first-new-run"):
            pass

    def second_owner():
        try:
            with runguard.run_lock(repo, run_dir=tmp_path / "second-new-run"):
                second_entered.set()
        except runguard.RunLockError:
            pass

    first = threading.Thread(target=first_owner)
    first.start()
    assert recovery_started.wait(timeout=2.0)
    second = threading.Thread(target=second_owner)
    second.start()
    second.join(timeout=2.0)

    assert not second.is_alive()
    assert not second_entered.is_set()
    finish_recovery.set()
    first.join(timeout=2.0)
    assert not first.is_alive()


@pytest.mark.parametrize("owner_json", [None, "not json"])
def test_run_lock_release_uses_published_directory_identity_when_owner_metadata_is_lost(tmp_path, owner_json):
    repo = _repo(tmp_path)
    lock_path = runguard.lock_path(repo)

    with runguard.run_lock(repo):
        (lock_path / "owner.json").unlink()
        if owner_json is not None:
            (lock_path / "owner.json").write_text(owner_json)

    assert not lock_path.exists()


def test_run_lock_treats_pidless_lock_as_stale(tmp_path):
    repo = _repo(tmp_path)
    lock_path = runguard.lock_path(repo)
    lock_path.mkdir(parents=True)

    with runguard.run_lock(repo):
        assert lock_path.is_dir()

    assert not lock_path.exists()


def test_run_lock_removes_lock_after_context(tmp_path):
    repo = _repo(tmp_path)
    lock_path = runguard.lock_path(repo)

    with runguard.run_lock(repo):
        assert lock_path.is_dir()

    assert not lock_path.exists()


def test_create_detached_worktree_checks_out_head_in_separate_directory(tmp_path):
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_text("dirty source\n")
    worktree_path = tmp_path / "worktree"

    created = runguard.create_detached_worktree(repo, worktree_path)

    assert created == worktree_path
    assert (worktree_path / "tracked.txt").read_text() == "base\n"
    assert runguard.git_root(worktree_path) == worktree_path
    assert proc.run(["git", "symbolic-ref", "-q", "HEAD"], cwd=worktree_path).code == 1


def test_collect_changes_patch_captures_modified_and_untracked_files(tmp_path):
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_text("changed\n")
    (repo / "new.txt").write_text("new\n")
    patch_path = tmp_path / "changes.patch"

    summary = runguard.collect_changes_patch(repo, patch_path)

    patch = patch_path.read_text()
    assert summary.changed is True
    assert summary.path == patch_path
    assert "tracked.txt" in patch
    assert "new.txt" in patch
    assert "diff --git a/new.txt b/new.txt" in patch
    assert "+changed" in patch
    assert "+new" in patch


def test_collect_changes_patch_writes_empty_patch_for_clean_repo(tmp_path):
    repo = _repo(tmp_path)
    patch_path = tmp_path / "changes.patch"

    summary = runguard.collect_changes_patch(repo, patch_path)

    assert summary.changed is False
    assert patch_path.read_text() == ""


def _assert_patch_applies_to_base(repo, patch_path):
    _git(repo, "stash", "--include-untracked")
    result = proc.run(["git", "apply", "--check", str(patch_path)], cwd=repo)
    assert result.code == 0, f"patch does not apply: {result.stderr}"


def test_collect_changes_patch_preserves_trailing_blank_context_line(tmp_path):
    # A diff whose last hunk line is a blank context line ends in " \n".
    # Trimming that trailing space shortens the hunk and git rejects the
    # patch with "corrupt patch at line N" (issue #124).
    repo = _repo(tmp_path)
    (repo / "blank_tail.txt").write_text("x\n\n")
    _git(repo, "add", "blank_tail.txt")
    _git(repo, "commit", "-m", "blank tail")
    (repo / "blank_tail.txt").write_text("CHANGED\n\n")
    patch_path = tmp_path / "changes.patch"

    summary = runguard.collect_changes_patch(repo, patch_path)

    assert summary.changed is True
    _assert_patch_applies_to_base(repo, patch_path)


def test_collect_changes_patch_survives_blank_context_between_pieces(tmp_path):
    # The tracked piece ends on a blank context line while an untracked
    # piece follows; per-piece trimming corrupts the boundary the same way.
    repo = _repo(tmp_path)
    (repo / "blank_tail.txt").write_text("x\n\n")
    _git(repo, "add", "blank_tail.txt")
    _git(repo, "commit", "-m", "blank tail")
    (repo / "blank_tail.txt").write_text("CHANGED\n\n")
    (repo / "new.txt").write_text("new\n")
    patch_path = tmp_path / "changes.patch"

    summary = runguard.collect_changes_patch(repo, patch_path)

    assert summary.changed is True
    assert summary.untracked_count == 1
    _assert_patch_applies_to_base(repo, patch_path)


def test_verify_changes_patch_accepts_valid_patch(tmp_path):
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_text("changed\n")
    (repo / "new.txt").write_text("new\n")
    patch_path = tmp_path / "changes.patch"
    runguard.collect_changes_patch(repo, patch_path)

    assert runguard.verify_changes_patch(repo, patch_path) is True


def test_verify_changes_patch_rejects_corrupt_patch(tmp_path):
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_text("changed\n")
    patch_path = tmp_path / "changes.patch"
    runguard.collect_changes_patch(repo, patch_path)
    # Simulate the historical truncation: drop the final line.
    lines = patch_path.read_text().splitlines(keepends=True)
    patch_path.write_text("".join(lines[:-1]))

    assert runguard.verify_changes_patch(repo, patch_path) is False


def test_verify_changes_patch_accepts_empty_patch(tmp_path):
    repo = _repo(tmp_path)
    patch_path = tmp_path / "changes.patch"
    runguard.collect_changes_patch(repo, patch_path)

    assert runguard.verify_changes_patch(repo, patch_path) is True


def test_is_primary_checkout_true_for_main_repo(tmp_path):
    repo = _repo(tmp_path)

    assert runguard.is_primary_checkout(repo) is True


def test_is_primary_checkout_false_for_linked_worktree(tmp_path):
    repo = _repo(tmp_path)
    linked = tmp_path / "linked"
    _git(repo, "worktree", "add", str(linked), "HEAD")

    assert runguard.is_primary_checkout(repo) is True
    assert runguard.is_primary_checkout(linked) is False


def test_is_primary_checkout_false_outside_git(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()

    assert runguard.is_primary_checkout(plain) is False


def test_capture_pre_run_snapshot_records_clean_state(tmp_path):
    repo = _repo(tmp_path)

    snapshot = runguard.capture_pre_run_snapshot(repo)

    assert snapshot is not None
    assert snapshot.tracked_dirty == ()
    assert snapshot.untracked == ()
    assert len(snapshot.head) == 40
    assert snapshot.branch in {"main", "master"}


def test_capture_pre_run_snapshot_records_dirty_state(tmp_path):
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_text("dirty\n")
    (repo / "new.txt").write_text("new\n")

    snapshot = runguard.capture_pre_run_snapshot(repo)

    assert snapshot is not None
    # Fingerprints are content-sensitive and not raw contents; only the path
    # sets are exposed for attribution comparison.
    assert snapshot.tracked_dirty_paths == ("tracked.txt",)
    assert snapshot.untracked_paths == ("new.txt",)
    assert all(isinstance(fp, str) for _, fp in snapshot.tracked_dirty)
    assert all(isinstance(fp, str) for _, fp in snapshot.untracked)


def test_capture_pre_run_snapshot_none_outside_git(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()

    assert runguard.capture_pre_run_snapshot(plain) is None
    assert runguard.capture_pre_run_snapshot(None) is None


def test_snapshot_payload_shape(tmp_path):
    repo = _repo(tmp_path)
    snapshot = runguard.capture_pre_run_snapshot(repo)

    payload = runguard.snapshot_payload(snapshot)

    assert payload == {
        "schema": "brigade.pre_run_snapshot.v1",
        "branch": snapshot.branch,
        "head": snapshot.head,
        "tracked_dirty_files": [],
        "tracked_dirty_fingerprints": {},
        "untracked_files": [],
        "untracked_fingerprints": {},
        "tracked_dirty_files_total": 0,
        "untracked_files_total": 0,
    }
    assert runguard.snapshot_payload(None) is None


def test_snapshot_payload_persists_content_sensitive_fingerprints(tmp_path):
    # Regression for finding 4: the persisted pre-run snapshot must carry
    # enough content-sensitive data to audit/reproduce the worker-change
    # comparison, not just path lists. Fingerprints are one-way digests and
    # never raw contents.
    repo = _repo(tmp_path)
    secret = "sk-super-secret-value-do-not-leak"
    (repo / "tracked.txt").write_text(secret + "\n")
    (repo / "new.txt").write_text("already here\n")
    snapshot = runguard.capture_pre_run_snapshot(repo)

    payload = runguard.snapshot_payload(snapshot)
    assert payload is not None
    assert payload["tracked_dirty_fingerprints"] == dict(snapshot.tracked_dirty)
    assert payload["untracked_fingerprints"] == dict(snapshot.untracked)
    assert payload["tracked_dirty_fingerprints"].keys() == {"tracked.txt"}
    assert payload["untracked_fingerprints"].keys() == {"new.txt"}
    # Persisted fingerprints are content-sensitive digests, not raw contents.
    blob = json.dumps(payload)
    assert secret not in blob
    assert all(secret not in fp for fp in payload["tracked_dirty_fingerprints"].values())


def test_untracked_files_streams_past_capture_cap(tmp_path):
    # Issue #1165: a >1 MiB newline-delimited untracked listing died at the
    # generic 1 MiB child-output cap and failed the pre-run snapshot before
    # dispatch. NUL-delimited streaming enumeration must capture it.
    # Paths stay short so the full Windows path remains well under MAX_PATH.
    repo = _repo(tmp_path)
    suffix = "x" * 64
    created: list[str] = []
    for d in range(100):
        rel_dir = f"d{d:02d}"
        (repo / rel_dir).mkdir()
        for f in range(150):
            rel = f"{rel_dir}/f{f:04d}{suffix}"
            (repo / rel).write_text("content\n")
            created.append(rel)
    listed_bytes = sum(len(rel.encode()) + 1 for rel in created)
    assert listed_bytes > proc.MAX_CAPTURE_BYTES

    paths = runguard._untracked_files(repo)

    assert len(paths) == len(created)
    assert sorted(created) == paths

    snapshot = runguard.capture_pre_run_snapshot(repo)
    assert snapshot is not None
    assert len(snapshot.untracked_paths) == len(created)


def test_untracked_files_keeps_newline_names_via_nul_delimiters(tmp_path):
    repo = _repo(tmp_path)
    (repo / "weird\nname.txt").write_text("x\n")

    paths = runguard._untracked_files(repo)

    assert paths == ["weird\nname.txt"]


def test_untracked_files_names_path_list_when_budget_exceeded(tmp_path):
    repo = _repo(tmp_path)
    (repo / ("long" * 20 + "-a.txt")).write_text("a\n")
    (repo / ("long" * 20 + "-b.txt")).write_text("b\n")

    with pytest.raises(runguard.RunGuardError, match=r"untracked-path list exceeded 32 byte enumeration limit"):
        runguard._untracked_files(repo, max_bytes=32)


def test_snapshot_payload_summarizes_large_path_sets(tmp_path):
    repo = _repo(tmp_path)
    count = runguard.SNAPSHOT_RECEIPT_PATH_CAP + 10
    for i in range(count):
        (repo / f"u{i:04d}.txt").write_text("x\n")
    snapshot = runguard.capture_pre_run_snapshot(repo)

    payload = runguard.snapshot_payload(snapshot)

    assert payload is not None
    assert payload["untracked_files_total"] == count
    assert len(payload["untracked_files"]) == runguard.SNAPSHOT_RECEIPT_PATH_CAP
    assert set(payload["untracked_fingerprints"]) == set(payload["untracked_files"])


def test_changes_relative_to_snapshot_attributes_only_worker_changes(tmp_path):
    repo = _repo(tmp_path)
    # Pre-existing dirty state (allowed only in a linked worktree in practice).
    (repo / "preexisting.txt").write_text("already dirty\n")
    (repo / "preexisting_untracked.txt").write_text("already here\n")
    snapshot = runguard.capture_pre_run_snapshot(repo)

    # Worker makes its own changes; leaves the pre-existing files alone.
    (repo / "tracked.txt").write_text("worker changed\n")
    (repo / "worker_new.txt").write_text("worker created\n")

    changed, untracked = runguard.changes_relative_to_snapshot(repo, snapshot)

    assert changed == ["tracked.txt"]
    assert untracked == ["worker_new.txt"]


def test_changes_relative_to_snapshot_clean_run_attributes_everything(tmp_path):
    repo = _repo(tmp_path)
    snapshot = runguard.capture_pre_run_snapshot(repo)  # clean

    (repo / "tracked.txt").write_text("worker changed\n")
    (repo / "worker_new.txt").write_text("worker created\n")

    changed, untracked = runguard.changes_relative_to_snapshot(repo, snapshot)

    assert changed == ["tracked.txt"]
    assert untracked == ["worker_new.txt"]


def test_changes_relative_to_snapshot_no_snapshot_returns_empty(tmp_path):
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_text("changed\n")

    changed, untracked = runguard.changes_relative_to_snapshot(repo, None)

    assert changed == []
    assert untracked == []


def test_changes_relative_to_snapshot_detects_predirty_tracked_mutation(tmp_path):
    # Regression for finding 1: path-set subtraction missed a further edit to a
    # file already dirty before the run. The baseline fingerprint must differ
    # from the final fingerprint so the worker's edit is detected.
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_text("already dirty\n")  # baseline tracked dirt
    snapshot = runguard.capture_pre_run_snapshot(repo)

    # Worker edits the already-dirty file further.
    (repo / "tracked.txt").write_text("already dirty then worker changed\n")

    changed, untracked = runguard.changes_relative_to_snapshot(repo, snapshot)

    assert changed == ["tracked.txt"]
    assert untracked == []


def test_changes_relative_to_snapshot_detects_preexisting_untracked_mutation(tmp_path):
    # Regression for finding 1: a pre-existing untracked file the worker
    # mutates must be detected (path-set subtraction dropped it before).
    repo = _repo(tmp_path)
    (repo / "untracked.txt").write_text("already here\n")  # baseline untracked
    snapshot = runguard.capture_pre_run_snapshot(repo)

    (repo / "untracked.txt").write_text("already here then worker changed\n")

    changed, untracked = runguard.changes_relative_to_snapshot(repo, snapshot)

    assert changed == []
    assert untracked == ["untracked.txt"]


def test_changes_relative_to_snapshot_excludes_unchanged_baseline_dirt(tmp_path):
    # Regression for finding 1: a baseline-dirty file the worker leaves alone
    # must be excluded from attribution.
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_text("already dirty\n")
    (repo / "untracked.txt").write_text("already here\n")
    snapshot = runguard.capture_pre_run_snapshot(repo)

    # Worker only creates a new file; leaves baseline dirt untouched.
    (repo / "worker_new.txt").write_text("worker created\n")

    changed, untracked = runguard.changes_relative_to_snapshot(repo, snapshot)

    assert changed == []
    assert untracked == ["worker_new.txt"]


def test_changes_relative_to_snapshot_detects_deletion_of_predirty_tracked(tmp_path):
    # Regression for finding 1: deleting a baseline-dirty tracked file is a
    # worker edit (deletion) and must be detected.
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_text("already dirty\n")
    snapshot = runguard.capture_pre_run_snapshot(repo)

    (repo / "tracked.txt").unlink()

    changed, untracked = runguard.changes_relative_to_snapshot(repo, snapshot)

    assert changed == ["tracked.txt"]
    assert untracked == []


def test_changes_relative_to_snapshot_detects_deletion_of_preexisting_untracked(tmp_path):
    # Regression for finding 1: deleting a baseline-untracked file is a worker
    # edit and must be detected even though git no longer lists the path.
    repo = _repo(tmp_path)
    (repo / "untracked.txt").write_text("already here\n")
    snapshot = runguard.capture_pre_run_snapshot(repo)

    (repo / "untracked.txt").unlink()

    changed, untracked = runguard.changes_relative_to_snapshot(repo, snapshot)

    assert changed == []
    assert untracked == ["untracked.txt"]


def test_changes_relative_to_snapshot_detects_type_change_of_predirty_tracked(tmp_path):
    # Regression for finding 1: a mode/type change (chmod +x) to a baseline-dirty
    # tracked file is a worker edit and must be detected.
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_text("already dirty\n")
    snapshot = runguard.capture_pre_run_snapshot(repo)

    (repo / "tracked.txt").chmod(0o755)

    changed, untracked = runguard.changes_relative_to_snapshot(repo, snapshot)

    assert changed == ["tracked.txt"]
    assert untracked == []


def test_changes_relative_to_snapshot_detects_predirty_tracked_restored_to_head(tmp_path):
    # Regression for finding 2: a baseline-dirty tracked file the worker restores
    # to HEAD is no longer listed by `git diff --name-only HEAD`, so it disappears
    # from current_tracked. The restore is a content change (dirty baseline ->
    # HEAD) and must be attributed to the worker by comparing the union of
    # baseline and final dirty tracked paths with content fingerprints.
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_text("already dirty\n")  # baseline tracked dirt
    snapshot = runguard.capture_pre_run_snapshot(repo)

    # Worker restores the file to HEAD content (no longer dirty).
    (repo / "tracked.txt").write_text("base\n")

    changed, untracked = runguard.changes_relative_to_snapshot(repo, snapshot)

    assert changed == ["tracked.txt"]
    assert untracked == []


def test_changes_relative_to_snapshot_excludes_predirty_tracked_unchanged(tmp_path):
    # Companion to finding 2: a baseline-dirty tracked file the worker leaves
    # dirty at the exact baseline content must NOT be attributed to the worker.
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_text("already dirty\n")
    snapshot = runguard.capture_pre_run_snapshot(repo)

    # Worker does not touch tracked.txt; only creates a new untracked file.
    (repo / "worker_new.txt").write_text("worker created\n")

    changed, untracked = runguard.changes_relative_to_snapshot(repo, snapshot)

    assert changed == []
    assert untracked == ["worker_new.txt"]


def test_changes_relative_to_snapshot_fails_closed_when_tracked_query_fails(tmp_path, monkeypatch):
    # Regression for finding 3: a final git query failure must not become an
    # available clean result. Fail closed with RunGuardError and a precise reason.
    repo = _repo(tmp_path)
    snapshot = runguard.capture_pre_run_snapshot(repo)

    def boom(cwd, *args, **kwargs):
        return proc.Result(128, "", "fatal: not a git object")

    monkeypatch.setattr(runguard, "_git", boom)

    with pytest.raises(runguard.RunGuardError, match="could not re-read tracked dirty files after run"):
        runguard.changes_relative_to_snapshot(repo, snapshot)


def test_changes_relative_to_snapshot_fails_closed_when_untracked_query_fails(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    snapshot = runguard.capture_pre_run_snapshot(repo)

    def flaky(args, **kwargs):
        # Enumeration streams via proc.run_delimited (issue #1165); simulate
        # git failing there instead of through the _git wrapper.
        assert "ls-files" in args
        return proc.DelimitedResult(128, [], "fatal: loose object")

    monkeypatch.setattr(runguard.proc, "run_delimited", flaky)

    with pytest.raises(runguard.RunGuardError, match="could not re-read untracked files after run"):
        runguard.changes_relative_to_snapshot(repo, snapshot)


def test_capture_pre_run_snapshot_fingerprint_excludes_raw_content(tmp_path):
    # Contract: only a one-way digest is persisted, never raw file contents.
    repo = _repo(tmp_path)
    secret = "sk-super-secret-value-do-not-leak"
    (repo / "tracked.txt").write_text(secret + "\n")
    snapshot = runguard.capture_pre_run_snapshot(repo)

    persisted = runguard.snapshot_payload(snapshot)
    assert persisted is not None
    assert "tracked_dirty_files" in persisted
    assert secret not in json.dumps(persisted)
    # The in-memory fingerprint is a digest, not the raw secret.
    assert all(secret not in fp for _, fp in snapshot.tracked_dirty)


def test_detect_branch_head_drift_clean_returns_none(tmp_path):
    repo = _repo(tmp_path)
    snapshot = runguard.capture_pre_run_snapshot(repo)

    assert runguard.detect_branch_head_drift(repo, snapshot) is None


def test_detect_branch_head_drift_on_head_move(tmp_path):
    repo = _repo(tmp_path)
    snapshot = runguard.capture_pre_run_snapshot(repo)

    # A concurrent commit moves HEAD out from under the worker.
    (repo / "concurrent.txt").write_text("x\n")
    _git(repo, "add", "concurrent.txt")
    _git(repo, "commit", "-m", "concurrent")

    detail = runguard.detect_branch_head_drift(repo, snapshot)

    assert detail is not None
    assert "HEAD drifted" in detail


def test_detect_branch_head_drift_on_branch_switch(tmp_path):
    repo = _repo(tmp_path)
    snapshot = runguard.capture_pre_run_snapshot(repo)

    _git(repo, "checkout", "-b", "other-branch")
    detail = runguard.detect_branch_head_drift(repo, snapshot)

    assert detail is not None
    assert "branch drifted" in detail


def test_detect_branch_head_drift_no_snapshot_returns_none(tmp_path):
    repo = _repo(tmp_path)

    assert runguard.detect_branch_head_drift(repo, None) is None
    assert runguard.detect_branch_head_drift(None, None) is None


# --- Slice 5 Task 4: run_lock_state and before_terminalize callback ---


def _write_lock_owner_metadata(lock_path, *, owner_token, pid, run_dir, acquired_at="2026-07-16T00:00:00+00:00"):
    lock_path.mkdir(parents=True, exist_ok=True)
    (lock_path / "pid").write_text(f"{pid}\n")
    (lock_path / "owner.json").write_text(
        json.dumps(
            {
                "schema": "brigade.run_lock.v1",
                "owner_token": owner_token,
                "pid": pid,
                "run_dir": str(run_dir.resolve()) if run_dir is not None else None,
                "acquired_at": acquired_at,
            }
        )
    )


def _activated_journal(run_dir):
    events = run_dir / "events"
    events.mkdir(parents=True, exist_ok=True)
    journal = events / "lifecycle.jsonl"
    journal.write_text("")
    return journal


def test_run_lock_state_absent(tmp_path):
    repo = _repo(tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    assert runguard.run_lock_state(repo, run_dir) == "absent"


def test_run_lock_state_live_for_current_process(tmp_path):
    repo = _repo(tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    lock_path = runguard.lock_path(repo)
    _write_lock_owner_metadata(lock_path, owner_token="live", pid=os.getpid(), run_dir=run_dir)
    assert runguard.run_lock_state(repo, run_dir) == "live"


def test_run_lock_state_live_for_foreign_pid(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    lock_path = runguard.lock_path(repo)
    _write_lock_owner_metadata(lock_path, owner_token="live", pid=777777, run_dir=run_dir)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: True)
    assert runguard.run_lock_state(repo, run_dir) == "live"


def test_run_lock_state_stale(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    lock_path = runguard.lock_path(repo)
    _write_lock_owner_metadata(lock_path, owner_token="dead", pid=99999999, run_dir=run_dir)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: False)
    assert runguard.run_lock_state(repo, run_dir) == "stale"


def test_run_lock_state_foreign(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    other_run = tmp_path / "other-run"
    other_run.mkdir()
    lock_path = runguard.lock_path(repo)
    _write_lock_owner_metadata(lock_path, owner_token="dead", pid=99999999, run_dir=other_run)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: False)
    assert runguard.run_lock_state(repo, run_dir) == "foreign"


def test_run_lock_state_live_when_owner_run_dir_mismatches_but_pid_is_live(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    other_run = tmp_path / "other-run"
    other_run.mkdir()
    lock_path = runguard.lock_path(repo)
    _write_lock_owner_metadata(lock_path, owner_token="live", pid=777777, run_dir=other_run)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: True)
    assert runguard.run_lock_state(repo, run_dir) == "live"


def test_run_lock_state_invalid_when_owner_metadata_missing(tmp_path):
    repo = _repo(tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    lock_path = runguard.lock_path(repo)
    lock_path.mkdir(parents=True)
    (lock_path / "pid").write_text("99999999\n")
    assert runguard.run_lock_state(repo, run_dir) == "invalid"


def test_run_lock_state_invalid_when_owner_metadata_unparseable(tmp_path):
    repo = _repo(tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    lock_path = runguard.lock_path(repo)
    lock_path.mkdir(parents=True)
    (lock_path / "pid").write_text("99999999\n")
    (lock_path / "owner.json").write_text("not json")
    assert runguard.run_lock_state(repo, run_dir) == "invalid"


def test_run_lock_state_invalid_when_owner_metadata_is_non_utf8(tmp_path):
    repo = _repo(tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    lock_path = runguard.lock_path(repo)
    lock_path.mkdir(parents=True)
    (lock_path / "pid").write_text("99999999\n")
    (lock_path / "owner.json").write_bytes(b"\xff")
    snapshot_paths = sorted(p.name for p in lock_path.iterdir())
    snapshot_owner_bytes = (lock_path / "owner.json").read_bytes()

    assert runguard.run_lock_state(repo, run_dir) == "invalid"

    assert sorted(p.name for p in lock_path.iterdir()) == snapshot_paths
    assert (lock_path / "owner.json").read_bytes() == snapshot_owner_bytes


def test_run_lock_state_invalid_when_owner_metadata_is_deeply_nested(tmp_path):
    repo = _repo(tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    lock_path = runguard.lock_path(repo)
    lock_path.mkdir(parents=True)
    (lock_path / "pid").write_text("99999999\n")
    # Deeply nested valid JSON sufficient for json.loads to raise RecursionError.
    depth = 10000
    nested = "null"
    for _ in range(depth):
        nested = f"[{nested}]"
    (lock_path / "owner.json").write_text(nested)
    snapshot_paths = sorted(p.name for p in lock_path.iterdir())
    snapshot_owner_bytes = (lock_path / "owner.json").read_bytes()

    assert runguard.run_lock_state(repo, run_dir) == "invalid"

    assert sorted(p.name for p in lock_path.iterdir()) == snapshot_paths
    assert (lock_path / "owner.json").read_bytes() == snapshot_owner_bytes


def test_run_lock_state_never_raises_and_never_mutates(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    lock_path = runguard.lock_path(repo)
    _write_lock_owner_metadata(lock_path, owner_token="dead", pid=99999999, run_dir=run_dir)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: False)

    def boom(*args, **kwargs):
        raise OSError("boom")

    monkeypatch.setattr(runguard.shutil, "rmtree", boom)
    monkeypatch.setattr(runguard, "_claim_stale_lock", boom)
    monkeypatch.setattr(runguard, "_claim_existing_stale", boom)
    monkeypatch.setattr(runguard, "_restore_claimed_lock", boom)

    assert runguard.run_lock_state(repo, run_dir) == "stale"
    assert lock_path.is_dir()
    assert json.loads((lock_path / "owner.json").read_text())["owner_token"] == "dead"


def test_run_lock_state_returns_invalid_when_inspection_helpers_raise(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    lock_path = runguard.lock_path(repo)
    _write_lock_owner_metadata(lock_path, owner_token="dead", pid=99999999, run_dir=run_dir)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: False)

    def boom(*args, **kwargs):
        raise ValueError("malformed owner run_dir resolution")

    monkeypatch.setattr(runguard, "_owner_matches_run", boom)

    assert runguard.run_lock_state(repo, run_dir) == "invalid"
    assert lock_path.is_dir()
    assert json.loads((lock_path / "owner.json").read_text())["owner_token"] == "dead"


def test_run_lock_state_returns_invalid_for_cyclic_owner_run_dir(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    cyclic = tmp_path / "cyclic"
    cyclic.symlink_to(cyclic, target_is_directory=True)
    lock_path = runguard.lock_path(repo)
    lock_path.mkdir(parents=True)
    (lock_path / "pid").write_text("99999999\n")
    (lock_path / "owner.json").write_text(
        json.dumps(
            {
                "schema": "brigade.run_lock.v1",
                "owner_token": "dead",
                "pid": 99999999,
                "run_dir": str(cyclic),
                "acquired_at": "2026-07-16T00:00:00+00:00",
            }
        )
    )
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: False)

    assert runguard.run_lock_state(repo, run_dir) == "invalid"
    assert lock_path.is_dir()
    assert json.loads((lock_path / "owner.json").read_text())["owner_token"] == "dead"


def test_run_lock_state_returns_invalid_for_cyclic_requested_run_dir(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    cyclic = tmp_path / "cyclic"
    cyclic.symlink_to(cyclic, target_is_directory=True)
    lock_path = runguard.lock_path(repo)
    lock_path.mkdir(parents=True)
    (lock_path / "pid").write_text("99999999\n")
    (lock_path / "owner.json").write_text(
        json.dumps(
            {
                "schema": "brigade.run_lock.v1",
                "owner_token": "dead",
                "pid": 99999999,
                "run_dir": str(cyclic),
                "acquired_at": "2026-07-16T00:00:00+00:00",
            }
        )
    )
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: False)

    snapshot_paths = sorted(p.name for p in lock_path.iterdir())
    snapshot_owner_bytes = (lock_path / "owner.json").read_bytes()

    assert runguard.run_lock_state(repo, cyclic) == "invalid"

    assert sorted(p.name for p in lock_path.iterdir()) == snapshot_paths
    assert (lock_path / "owner.json").read_bytes() == snapshot_owner_bytes


@pytest.mark.parametrize("exc", [OSError("resolve boom"), RuntimeError("resolve boom")])
def test_run_lock_state_never_raises_for_lock_path_resolution_errors(tmp_path, monkeypatch, exc):
    """``OSError``/``RuntimeError`` from ``lock_path`` resolution (``cwd.resolve``
    on a cyclic or vanished workspace, or ``expanduser`` with no HOME) must not
    escape the never-raises predicate; the lock normalizes to ``invalid`` so
    resume/watch refuse rather than treating the lock as absent."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    real_lock_path = runguard.lock_path

    def boom(cwd):
        if cwd == workspace:
            raise exc
        return real_lock_path(cwd)

    monkeypatch.setattr(runguard, "lock_path", boom)

    assert runguard.run_lock_state(workspace, run_dir) == "invalid"


def test_run_lock_state_never_raises_for_unresolvable_workspace(tmp_path, monkeypatch):
    """A real unresolvable workspace (cyclic symlink) must normalize to
    ``invalid`` rather than escape ``run_lock_state`` with ``OSError``."""
    cyclic = tmp_path / "cyclic"
    cyclic.symlink_to(cyclic, target_is_directory=True)
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    assert runguard.run_lock_state(cyclic, run_dir) == "invalid"


def test_run_lock_state_returns_invalid_for_lstat_oserror(tmp_path, monkeypatch):
    """The single-lstat probe must classify inspection errors as invalid."""
    repo = _repo(tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    lock_path = runguard.lock_path(repo)
    real_lstat = Path.lstat

    def lstat_raises_eacces(self, *args, **kwargs):
        if self == lock_path:
            raise PermissionError(13, "Permission denied")
        return real_lstat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", lstat_raises_eacces)

    assert runguard.run_lock_state(repo, run_dir) == "invalid"


def test_lock_is_stale_treats_vanished_path_as_stale_not_malformed(tmp_path, monkeypatch):
    """A lock path that vanishes between probes must not be reported malformed."""
    lock_path = tmp_path / "run.lock"
    lock_path.mkdir()
    (lock_path / "pid").write_text("99999999\n")
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: False)
    shutil.rmtree(lock_path)

    assert runguard._lock_is_stale(lock_path) is True


def test_lock_is_stale_still_rejects_non_directory_lock(tmp_path):
    lock_path = tmp_path / "run.lock"
    lock_path.write_text("not a directory\n")

    with pytest.raises(runguard.RunLockError, match="malformed run lock"):
        runguard._lock_is_stale(lock_path)

    assert lock_path.is_file()


def test_recover_stale_run_invokes_before_terminalize_after_token_check(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = tmp_path / "abandoned-run"
    run_dir.mkdir()
    (run_dir / "run.json").write_text(
        json.dumps({"schema": "brigade.run.v1", "status": "dispatching", "task": "inspect"})
    )
    lock_path = runguard.lock_path(repo)
    _write_lock_owner_metadata(lock_path, owner_token="dead-owner", pid=99999999, run_dir=run_dir)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: False)

    finish_calls = []
    callback_owners = []
    original_finish = runguard._finish_claimed_recovery

    def spy_finish(path, claimed, owner, *, persist_recovery_provenance=False):
        finish_calls.append(owner)
        return original_finish(path, claimed, owner, persist_recovery_provenance=persist_recovery_provenance)

    monkeypatch.setattr(runguard, "_finish_claimed_recovery", spy_finish)

    def before_terminalize(owner):
        callback_owners.append(owner)

    assert runguard.recover_stale_run(repo, run_dir, before_terminalize=before_terminalize) is True
    assert len(callback_owners) == 1
    assert callback_owners[0].get("owner_token") == "dead-owner"
    assert len(finish_calls) == 1
    assert finish_calls[0] is callback_owners[0]


def test_recover_stale_run_invokes_callback_for_existing_pending_claim(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = tmp_path / "abandoned-run"
    run_dir.mkdir()
    (run_dir / "run.json").write_text(
        json.dumps({"schema": "brigade.run.v1", "status": "dispatching", "task": "inspect"})
    )
    lock_path = runguard.lock_path(repo)
    claimed = lock_path.with_name(f".{lock_path.name}.crashed.stale")
    _write_lock_owner_metadata(claimed, owner_token="dead-owner", pid=99999999, run_dir=run_dir)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: False)

    callback_owners = []

    def before_terminalize(owner):
        callback_owners.append(owner)

    assert runguard.recover_stale_run(repo, run_dir, before_terminalize=before_terminalize) is True
    assert len(callback_owners) == 1
    assert callback_owners[0].get("owner_token") == "dead-owner"
    assert not claimed.exists()
    assert json.loads((run_dir / "run.json").read_text())["status"] == "failed"


def test_recover_stale_run_callback_error_restores_claimed_lock_and_raises(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = tmp_path / "abandoned-run"
    run_dir.mkdir()
    (run_dir / "run.json").write_text(
        json.dumps({"schema": "brigade.run.v1", "status": "dispatching", "task": "inspect"})
    )
    lock_path = runguard.lock_path(repo)
    _write_lock_owner_metadata(lock_path, owner_token="dead-owner", pid=99999999, run_dir=run_dir)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: False)

    restore_calls = []
    original_restore = runguard._restore_claimed_lock

    def spy_restore(target, claimed):
        restore_calls.append((target, claimed))
        return original_restore(target, claimed)

    monkeypatch.setattr(runguard, "_restore_claimed_lock", spy_restore)

    def before_terminalize(owner):
        raise ValueError("checkpoint validation failed")

    with pytest.raises(runguard.RunLockError, match="restored") as exc:
        runguard.recover_stale_run(repo, run_dir, before_terminalize=before_terminalize)

    assert len(restore_calls) == 1
    assert restore_calls[0][0] == lock_path
    assert str(lock_path) in str(exc.value)
    assert lock_path.is_dir()
    dangling = list(lock_path.parent.glob(f".{lock_path.name}.*.stale"))
    assert dangling == []
    assert json.loads((lock_path / "owner.json").read_text())["owner_token"] == "dead-owner"
    assert json.loads((run_dir / "run.json").read_text())["status"] == "dispatching"


def test_recover_stale_run_callback_error_for_pending_claim_restores_and_raises(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = tmp_path / "abandoned-run"
    run_dir.mkdir()
    (run_dir / "run.json").write_text(
        json.dumps({"schema": "brigade.run.v1", "status": "dispatching", "task": "inspect"})
    )
    lock_path = runguard.lock_path(repo)
    claimed = lock_path.with_name(f".{lock_path.name}.crashed.stale")
    _write_lock_owner_metadata(claimed, owner_token="dead-owner", pid=99999999, run_dir=run_dir)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: False)

    restore_calls = []
    original_restore = runguard._restore_claimed_lock

    def spy_restore(target, claimed_arg):
        restore_calls.append((target, claimed_arg))
        return original_restore(target, claimed_arg)

    monkeypatch.setattr(runguard, "_restore_claimed_lock", spy_restore)

    def before_terminalize(owner):
        raise ValueError("checkpoint validation failed")

    with pytest.raises(runguard.RunLockError, match="restored") as exc:
        runguard.recover_stale_run(repo, run_dir, before_terminalize=before_terminalize)

    assert len(restore_calls) == 1
    assert restore_calls[0][0] == lock_path
    assert str(lock_path) in str(exc.value)
    assert lock_path.is_dir()
    dangling = list(lock_path.parent.glob(f".{lock_path.name}.*.stale"))
    assert dangling == []
    assert json.loads((lock_path / "owner.json").read_text())["owner_token"] == "dead-owner"
    assert json.loads((run_dir / "run.json").read_text())["status"] == "dispatching"


def test_pending_claim_token_race_restores_and_raises(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = tmp_path / "abandoned-run"
    run_dir.mkdir()
    (run_dir / "run.json").write_text(
        json.dumps({"schema": "brigade.run.v1", "status": "dispatching", "task": "inspect"})
    )
    lock_path = runguard.lock_path(repo)
    stale = lock_path.with_name(f".{lock_path.name}.crashed.stale")
    _write_lock_owner_metadata(stale, owner_token="original-token", pid=99999999, run_dir=run_dir)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: False)

    original_claim = runguard._claim_existing_stale

    def racing_claim(path, stale_arg):
        result = original_claim(path, stale_arg)
        if result is None:
            return None
        claimed, owner = result
        raced = dict(owner)
        raced["owner_token"] = "raced-token"
        (claimed / "owner.json").write_text(json.dumps(raced))
        return claimed, raced

    monkeypatch.setattr(runguard, "_claim_existing_stale", racing_claim)

    callback_calls = []
    finish_calls = []
    original_finish = runguard._finish_claimed_recovery

    def spy_finish(*args, **kwargs):
        finish_calls.append(args)
        return original_finish(*args, **kwargs)

    monkeypatch.setattr(runguard, "_finish_claimed_recovery", spy_finish)

    def before_terminalize(owner):
        callback_calls.append(owner)

    with pytest.raises(runguard.RunLockError, match="owner changed during pending claim recovery") as exc:
        runguard.recover_stale_run(repo, run_dir, before_terminalize=before_terminalize)

    assert str(stale) in str(exc.value)
    assert callback_calls == []
    assert finish_calls == []
    assert stale.is_dir()
    dangling = [p for p in lock_path.parent.glob(f".{lock_path.name}.*.stale") if p != stale]
    assert dangling == []
    assert json.loads((stale / "owner.json").read_text())["owner_token"] == "raced-token"
    assert json.loads((run_dir / "run.json").read_text())["status"] == "dispatching"


def test_pending_activated_claim_without_callback_requires_explicit_recovery(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = tmp_path / "abandoned-run"
    run_dir.mkdir()
    (run_dir / "run.json").write_text(
        json.dumps({"schema": "brigade.run.v1", "status": "dispatching", "task": "inspect"})
    )
    _activated_journal(run_dir)
    lock_path = runguard.lock_path(repo)
    claimed = lock_path.with_name(f".{lock_path.name}.crashed.stale")
    _write_lock_owner_metadata(claimed, owner_token="dead-owner", pid=99999999, run_dir=run_dir)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: False)

    with pytest.raises(runguard.RunLockError, match="explicit"):
        runguard.recover_stale_run(repo, run_dir)

    retained = list(lock_path.parent.glob(f".{lock_path.name}.*.stale"))
    assert len(retained) == 1
    assert json.loads((run_dir / "run.json").read_text())["status"] == "dispatching"


def test_generic_lock_acquisition_retains_activated_pending_claim(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = tmp_path / "abandoned-run"
    run_dir.mkdir()
    (run_dir / "run.json").write_text(
        json.dumps({"schema": "brigade.run.v1", "status": "dispatching", "task": "inspect"})
    )
    _activated_journal(run_dir)
    lock_path = runguard.lock_path(repo)
    claimed = lock_path.with_name(f".{lock_path.name}.crashed.stale")
    _write_lock_owner_metadata(claimed, owner_token="dead-owner", pid=99999999, run_dir=run_dir)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: False)

    with pytest.raises(runguard.RunLockError):
        with runguard.run_lock(repo, run_dir=tmp_path / "new-run"):
            pass

    retained = list(lock_path.parent.glob(f".{lock_path.name}.*.stale"))
    assert len(retained) == 1
    assert json.loads((run_dir / "run.json").read_text())["status"] == "dispatching"


def test_recover_stale_run_without_callback_terminalizes_as_today(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = tmp_path / "abandoned-run"
    run_dir.mkdir()
    (run_dir / "run.json").write_text(
        json.dumps({"schema": "brigade.run.v1", "status": "dispatching", "task": "inspect"})
    )
    lock_path = runguard.lock_path(repo)
    _write_lock_owner_metadata(lock_path, owner_token="dead-owner", pid=99999999, run_dir=run_dir)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: False)
    monkeypatch.setattr(runguard.localio, "tree_fingerprint", lambda path: "f" * 40)

    assert runguard.recover_stale_run(repo, run_dir) is True
    assert not lock_path.exists()
    recovered = json.loads((run_dir / "run.json").read_text())
    assert recovered["status"] == "failed"
    assert recovered["tree_fingerprint"] == "f" * 40
    assert "lock_workspace" not in recovered.get("failure", {})
    assert "lock_acquired_at" not in recovered.get("failure", {})


def test_recover_stale_run_refuses_live_owner_current_pid(tmp_path):
    repo = _repo(tmp_path)
    run_dir = tmp_path / "active-run"
    run_dir.mkdir()
    (run_dir / "run.json").write_text(json.dumps({"status": "dispatching"}))
    lock_path = runguard.lock_path(repo)
    _write_lock_owner_metadata(lock_path, owner_token="live-owner", pid=os.getpid(), run_dir=run_dir)

    with pytest.raises(runguard.RunLockError, match="another brigade run appears active"):
        with runguard.run_lock(repo, run_dir=tmp_path / "new-run"):
            pass

    assert lock_path.is_dir()
    assert json.loads((lock_path / "owner.json").read_text())["owner_token"] == "live-owner"
    assert json.loads((run_dir / "run.json").read_text())["status"] == "dispatching"


def test_recover_stale_run_refuses_live_owner_foreign_pid(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = tmp_path / "active-run"
    run_dir.mkdir()
    (run_dir / "run.json").write_text(json.dumps({"status": "dispatching"}))
    lock_path = runguard.lock_path(repo)
    _write_lock_owner_metadata(lock_path, owner_token="live-owner", pid=777777, run_dir=run_dir)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: True)

    with pytest.raises(runguard.RunLockError, match="another brigade run appears active"):
        with runguard.run_lock(repo, run_dir=tmp_path / "new-run"):
            pass

    assert lock_path.is_dir()
    assert json.loads((lock_path / "owner.json").read_text())["owner_token"] == "live-owner"


def test_recover_run_artifact_persists_lock_recovery_provenance(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = tmp_path / "abandoned-run"
    run_dir.mkdir()
    (run_dir / "run.json").write_text(
        json.dumps({"schema": "brigade.run.v1", "status": "dispatching", "task": "inspect"})
    )
    _activated_journal(run_dir)
    lock_path = runguard.lock_path(repo)
    _write_lock_owner_metadata(lock_path, owner_token="dead-owner", pid=99999999, run_dir=run_dir)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: False)

    def before_terminalize(owner):
        return None

    assert runguard.recover_stale_run(repo, run_dir, before_terminalize=before_terminalize) is True

    recovered = json.loads((run_dir / "run.json").read_text())
    assert recovered["status"] == "failed"
    failure = recovered["failure"]
    assert failure["lock_workspace"] == str(repo.resolve())
    assert failure["lock_acquired_at"] == "2026-07-16T00:00:00+00:00"


def test_recover_run_artifact_persists_lock_recovery_provenance_omits_absent_owner_values(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = tmp_path / "abandoned-run"
    run_dir.mkdir()
    (run_dir / "run.json").write_text(
        json.dumps({"schema": "brigade.run.v1", "status": "dispatching", "task": "inspect"})
    )
    _activated_journal(run_dir)
    lock_path = runguard.lock_path(repo)
    lock_path.mkdir(parents=True)
    (lock_path / "pid").write_text("99999999\n")
    (lock_path / "owner.json").write_text(
        json.dumps(
            {
                "schema": "brigade.run_lock.v1",
                "owner_token": "dead-owner",
                "pid": 99999999,
                "run_dir": str(run_dir.resolve()),
            }
        )
    )
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: False)

    def before_terminalize(owner):
        return None

    assert runguard.recover_stale_run(repo, run_dir, before_terminalize=before_terminalize) is True

    recovered = json.loads((run_dir / "run.json").read_text())
    failure = recovered["failure"]
    assert "lock_workspace" in failure
    assert "lock_acquired_at" not in failure


def test_legacy_recover_run_artifact_does_not_add_recovery_provenance(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = tmp_path / "abandoned-run"
    run_dir.mkdir()
    (run_dir / "run.json").write_text(
        json.dumps({"schema": "brigade.run.v1", "status": "dispatching", "task": "inspect"})
    )
    lock_path = runguard.lock_path(repo)
    _write_lock_owner_metadata(lock_path, owner_token="dead-owner", pid=99999999, run_dir=run_dir)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: False)

    assert runguard.recover_stale_run(repo, run_dir) is True
    recovered = json.loads((run_dir / "run.json").read_text())
    assert recovered["status"] == "failed"
    failure = recovered["failure"]
    assert "lock_workspace" not in failure
    assert "lock_acquired_at" not in failure


def test_is_active_run_owner_remains_current_process_only(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = tmp_path / "active-run"
    run_dir.mkdir()
    lock_path = runguard.lock_path(repo)
    _write_lock_owner_metadata(lock_path, owner_token="live", pid=os.getpid(), run_dir=run_dir)
    assert runguard.is_active_run_owner(repo, run_dir) is True

    other_run = tmp_path / "other-run"
    other_run.mkdir()
    _write_lock_owner_metadata(lock_path, owner_token="live", pid=os.getpid(), run_dir=other_run)
    assert runguard.is_active_run_owner(repo, run_dir) is False

    foreign_pid = 777777
    _write_lock_owner_metadata(lock_path, owner_token="live", pid=foreign_pid, run_dir=run_dir)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: True)
    assert runguard.is_active_run_owner(repo, run_dir) is False


def _abandoned_run(tmp_path):
    """A repo plus a non-terminal run dir holding a dispatching run.json."""
    repo = _repo(tmp_path)
    run_dir = tmp_path / "abandoned-run"
    run_dir.mkdir()
    (run_dir / "run.json").write_text(
        json.dumps({"schema": "brigade.run.v1", "status": "dispatching", "task": "inspect"})
    )
    return repo, run_dir


def _matching_stale_claim(repo, run_dir, *, owner_token="dead-owner", pid=99999999):
    """Write a persistent ``.stale`` claim for ``run_dir`` next to the run lock."""
    lock = runguard.lock_path(repo)
    stale = lock.with_name(f".{lock.name}.crashed.stale")
    _write_lock_owner_metadata(stale, owner_token=owner_token, pid=pid, run_dir=run_dir)
    return stale


def test_recover_stale_run_continues_to_pending_claim_when_lock_vanishes_during_probe(tmp_path, monkeypatch):
    repo, run_dir = _abandoned_run(tmp_path)
    lock_path = runguard.lock_path(repo)
    lock_path.mkdir(parents=True)
    stale = _matching_stale_claim(repo, run_dir)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: pid != 99999999)
    original_lstat = Path.lstat
    vanished = False

    def vanish_after_successful_lstat(self, *args, **kwargs):
        nonlocal vanished
        result = original_lstat(self, *args, **kwargs)
        if self == lock_path and not vanished:
            shutil.rmtree(lock_path)
            vanished = True
        return result

    monkeypatch.setattr(Path, "lstat", vanish_after_successful_lstat)

    assert runguard.recover_stale_run(repo, run_dir) is True
    assert vanished is True
    assert not lock_path.exists()
    assert not stale.exists()
    assert json.loads((run_dir / "run.json").read_text())["status"] == "failed"


def test_recover_stale_run_uses_pending_claim_when_matching_lock_disappears_after_failed_claim(tmp_path, monkeypatch):
    repo, run_dir = _abandoned_run(tmp_path)
    lock_path = runguard.lock_path(repo)
    _write_lock_owner_metadata(lock_path, owner_token="visible-owner", pid=99999999, run_dir=run_dir)
    stale = _matching_stale_claim(repo, run_dir, owner_token="pending-owner")
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: False)

    def lose_visible_lock(path):
        assert path == lock_path
        shutil.rmtree(lock_path)
        return None

    monkeypatch.setattr(runguard, "_claim_stale_lock", lose_visible_lock)

    assert runguard.recover_stale_run(repo, run_dir) is True
    assert not lock_path.exists()
    assert not stale.exists()
    assert json.loads((run_dir / "run.json").read_text())["status"] == "failed"


def test_run_recovery_status_is_cleared_when_lock_vanishes_during_probe(tmp_path, monkeypatch):
    repo, run_dir = _abandoned_run(tmp_path)
    lock_path = runguard.lock_path(repo)
    lock_path.mkdir(parents=True)
    original_lstat = Path.lstat
    vanished = False

    def vanish_after_successful_lstat(self, *args, **kwargs):
        nonlocal vanished
        result = original_lstat(self, *args, **kwargs)
        if self == lock_path and not vanished:
            lock_path.rmdir()
            vanished = True
        return result

    monkeypatch.setattr(Path, "lstat", vanish_after_successful_lstat)

    assert runguard.run_recovery_status(repo, run_dir) == "cleared"
    assert vanished is True


def test_recover_stale_run_rejects_malformed_lock_as_typed_error(tmp_path):
    repo, run_dir = _abandoned_run(tmp_path)
    lock_path = runguard.lock_path(repo)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("malformed lock\n")

    with pytest.raises(runguard.RunLockError, match="malformed run lock is not a directory"):
        runguard.recover_stale_run(repo, run_dir)

    assert lock_path.read_text() == "malformed lock\n"
    assert json.loads((run_dir / "run.json").read_text())["status"] == "dispatching"


def test_recover_stale_run_fails_closed_when_lock_lstat_raises_oserror(tmp_path, monkeypatch):
    repo, run_dir = _abandoned_run(tmp_path)
    lock_path = runguard.lock_path(repo)
    lock_path.mkdir(parents=True)
    original_lstat = Path.lstat

    def fail_lock_stat(self, *args, **kwargs):
        if self == lock_path:
            raise OSError("probe failed")
        return original_lstat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", fail_lock_stat)

    with pytest.raises(runguard.RunLockError, match="could not inspect run lock.*probe failed"):
        runguard.recover_stale_run(repo, run_dir)

    assert json.loads((run_dir / "run.json").read_text())["status"] == "dispatching"


@pytest.mark.parametrize("probe_error", [OSError("probe failed"), None])
def test_run_recovery_status_is_unknown_for_uninspectable_lock(tmp_path, monkeypatch, probe_error):
    repo, run_dir = _abandoned_run(tmp_path)
    lock_path = runguard.lock_path(repo)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if probe_error is None:
        lock_path.write_text("malformed lock\n")
    else:
        original_lstat = Path.lstat

        def fail_lock_stat(self, *args, **kwargs):
            if self == lock_path:
                raise probe_error
            return original_lstat(self, *args, **kwargs)

        monkeypatch.setattr(Path, "lstat", fail_lock_stat)

    assert runguard.run_recovery_status(repo, run_dir) == "unknown"


def test_dangling_symlink_lock_fails_closed_across_recovery_status_and_state(tmp_path):
    repo, run_dir = _abandoned_run(tmp_path)
    lock_path = runguard.lock_path(repo)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.symlink_to(lock_path.parent / "missing-lock-target", target_is_directory=True)

    assert runguard.run_lock_state(repo, run_dir) == "invalid"
    assert runguard.run_recovery_status(repo, run_dir) == "unknown"
    with pytest.raises(runguard.RunLockError, match="malformed run lock is not a directory"):
        runguard.recover_stale_run(repo, run_dir)
    with pytest.raises(runguard.RunLockError, match="malformed run lock is not a directory"):
        runguard._lock_is_stale(lock_path)
    with pytest.raises(runguard.RunLockError, match="malformed run lock is not a directory"):
        with runguard.run_lock(repo):
            pass


def test_claim_is_activated_normalizes_runtime_error_from_expanduser(monkeypatch):
    """A ``RuntimeError`` from ``expanduser`` (no HOME for a ``~`` run_dir) must
    normalize to ``False``: the activation probe is best-effort and never raises."""
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.delenv("USERPROFILE", raising=False)
    owner = {"run_dir": "~nope-nope-nope-nope/somerun", "pid": 99999999}
    assert runguard._claim_is_activated(owner) is False


def test_claim_is_activated_normalizes_oserror_from_is_file(monkeypatch, tmp_path):
    """An ``OSError`` from ``is_file`` (a path component vanishing between
    resolve and stat) must normalize to ``False`` rather than escape the probe."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    real_is_file = Path.is_file

    def flaky_is_file(self, *args, **kwargs):
        if str(self).endswith("lifecycle.jsonl"):
            raise OSError("is_file boom")
        return real_is_file(self, *args, **kwargs)

    monkeypatch.setattr(Path, "is_file", flaky_is_file)
    owner = {"run_dir": str(run_dir), "pid": 99999999}
    assert runguard._claim_is_activated(owner) is False


def test_recover_stale_run_recovers_matching_stale_claim_despite_foreign_live_lock(tmp_path, monkeypatch):
    """A matching persistent ``.stale`` claim stays recoverable under a foreign
    live lock: the foreign lock is skipped without mutation and the claim is
    recovered via ``_recover_pending_claims``."""
    repo, run_dir = _abandoned_run(tmp_path)
    lock_path = runguard.lock_path(repo)

    # Foreign live lock: a different run owns the lock path with a live pid.
    foreign_run = tmp_path / "foreign-run"
    foreign_run.mkdir()
    _write_lock_owner_metadata(lock_path, owner_token="foreign", pid=os.getpid(), run_dir=foreign_run)
    stale = _matching_stale_claim(repo, run_dir)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: pid != 99999999)

    assert runguard.recover_stale_run(repo, run_dir) is True
    # The foreign live lock is untouched; the claim is cleared and the run terminalized.
    assert lock_path.is_dir()
    assert json.loads((lock_path / "owner.json").read_text())["owner_token"] == "foreign"
    assert not stale.exists()
    assert json.loads((run_dir / "run.json").read_text())["status"] == "failed"


def test_recover_stale_run_recovers_matching_stale_claim_despite_invalid_live_lock(tmp_path, monkeypatch):
    """A matching persistent ``.stale`` claim stays recoverable when the live
    lock is invalid (no owner metadata); the invalid lock is left unmutated."""
    repo, run_dir = _abandoned_run(tmp_path)
    lock_path = runguard.lock_path(repo)

    # Invalid live lock: directory with no owner.json.
    lock_path.mkdir(parents=True, exist_ok=True)
    (lock_path / "pid").write_text(f"{os.getpid()}\n")
    stale = _matching_stale_claim(repo, run_dir)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: pid != 99999999)

    assert runguard.recover_stale_run(repo, run_dir) is True
    assert lock_path.is_dir()
    assert not (lock_path / "owner.json").exists()
    assert not stale.exists()
    assert json.loads((run_dir / "run.json").read_text())["status"] == "failed"


def test_recover_stale_run_still_raises_when_foreign_lock_and_no_matching_claim(tmp_path, monkeypatch):
    """A foreign live lock with no matching ``.stale`` claim still raises, so the
    caller's concurrent-terminal fallback can surface a bounded error; the
    foreign lock is never mutated."""
    repo, run_dir = _abandoned_run(tmp_path)
    lock_path = runguard.lock_path(repo)

    foreign_run = tmp_path / "foreign-run"
    foreign_run.mkdir()
    _write_lock_owner_metadata(lock_path, owner_token="foreign", pid=os.getpid(), run_dir=foreign_run)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: True)

    with pytest.raises(runguard.RunLockError, match="run lock not found for run:"):
        runguard.recover_stale_run(repo, run_dir)
    assert lock_path.is_dir()
    assert json.loads((lock_path / "owner.json").read_text())["owner_token"] == "foreign"


@pytest.mark.parametrize("exc", [OSError("resolve boom"), RuntimeError("expanduser boom")])
def test_recover_pending_claims_skips_claim_when_owner_matches_run_raises(tmp_path, monkeypatch, exc):
    """When ``_owner_matches_run`` raises ``OSError``/``RuntimeError`` resolving
    the owner's recorded run_dir, ``_recover_pending_claims`` fails closed
    (skips the unverifiable claim) instead of letting the error escape the
    recovery gate. The matching claim is left in place and run.json is not
    terminalized."""
    repo, run_dir = _abandoned_run(tmp_path)
    stale = _matching_stale_claim(repo, run_dir)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: pid != 99999999)

    def boom(_owner, _run_dir):
        raise exc

    monkeypatch.setattr(runguard, "_owner_matches_run", boom)

    # required=False so the gate returns False instead of raising "run lock not
    # found"; the point is that no exception escapes and the claim is skipped.
    assert runguard._recover_pending_claims(runguard.lock_path(repo), run_dir=run_dir) is False
    assert stale.exists()
    assert json.loads((run_dir / "run.json").read_text())["status"] == "dispatching"


def test_has_matching_stale_claim_normalizes_oserror_from_stale_claims_glob(tmp_path, monkeypatch):
    """An ``OSError`` from the ``_stale_claims`` glob (a vanished or unreadable
    lock parent) must not escape the fail-closed predicate."""
    repo = _repo(tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _matching_stale_claim(repo, run_dir, owner_token="dead")
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: False)

    def boom(_path):
        raise OSError("glob boom")

    monkeypatch.setattr(runguard, "_stale_claims", boom)

    assert runguard.has_matching_stale_claim(repo, run_dir) is False


def test_has_matching_stale_claim_normalizes_runtime_error_from_owner_path_resolve(tmp_path, monkeypatch):
    """A ``RuntimeError`` from ``_owner_matches_run`` path resolution
    (``expanduser`` with no HOME) must not escape the fail-closed predicate."""
    repo = _repo(tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _matching_stale_claim(repo, run_dir, owner_token="dead")
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: False)

    def boom(_owner, _run_dir):
        raise RuntimeError("expanduser boom")

    monkeypatch.setattr(runguard, "_owner_matches_run", boom)

    assert runguard.has_matching_stale_claim(repo, run_dir) is False


@pytest.mark.parametrize("exc", [OSError("resolve boom"), RuntimeError("resolve boom")])
def test_has_matching_stale_claim_normalizes_lock_path_errors(tmp_path, monkeypatch, exc):
    """``OSError``/``RuntimeError`` from ``lock_path`` resolution must not
    escape the fail-closed predicate."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    real_lock_path = runguard.lock_path

    def boom(cwd):
        if cwd == workspace:
            raise exc
        return real_lock_path(cwd)

    monkeypatch.setattr(runguard, "lock_path", boom)

    assert runguard.has_matching_stale_claim(workspace, run_dir) is False


def test_has_matching_stale_claim_returns_false_for_foreign_stale_claim(tmp_path, monkeypatch):
    """A persistent ``.stale`` claim pointing at a different run returns
    ``False`` (fail closed)."""
    repo = _repo(tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    other_run = tmp_path / "other-run"
    other_run.mkdir()
    _matching_stale_claim(repo, other_run)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: False)

    assert runguard.has_matching_stale_claim(repo, run_dir) is False


def test_has_matching_stale_claim_never_mutates_lock_or_stale_claims(tmp_path, monkeypatch):
    """The predicate is read-only: neither the lock nor any claim is mutated."""
    repo = _repo(tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    stale = _matching_stale_claim(repo, run_dir)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: False)

    snapshot_paths = sorted(p.name for p in stale.parent.iterdir())
    stale_owner_bytes = (stale / "owner.json").read_bytes()

    runguard.has_matching_stale_claim(repo, run_dir)

    assert sorted(p.name for p in stale.parent.iterdir()) == snapshot_paths
    assert (stale / "owner.json").read_bytes() == stale_owner_bytes
