from __future__ import annotations

import json
import os
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
    assert "--allow-dirty" in str(exc.value)


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

    def paused_recover(owner):
        recovery_started.set()
        assert finish_recovery.wait(timeout=2.0)
        return original_recover(owner)

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

    real_git = runguard._git
    calls = {"n": 0}

    def flaky(cwd, *args, **kwargs):
        calls["n"] += 1
        # First call (tracked dirty paths) succeeds; second (ls-files) fails.
        if "ls-files" in args:
            return proc.Result(128, "", "fatal: loose object")
        return real_git(cwd, *args, **kwargs)

    monkeypatch.setattr(runguard, "_git", flaky)

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
