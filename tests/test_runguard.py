from __future__ import annotations

import os

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


def test_run_lock_replaces_lock_with_dead_pid(tmp_path):
    repo = _repo(tmp_path)
    lock_path = runguard.lock_path(repo)
    lock_path.mkdir(parents=True)
    (lock_path / "pid").write_text("99999999\n")

    with runguard.run_lock(repo):
        assert (lock_path / "pid").read_text().strip() == str(os.getpid())

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
