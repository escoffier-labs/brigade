"""Per-task batch worktree reservation, creation, validation and binding facts."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from brigade import run_batch_execute, run_batch_workspace as workspace, runguard
from brigade import run_tasks as tasks
from tests import test_run_tasks as journal_support

accepted = journal_support.accepted
task_run = journal_support.task_run

_GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
}


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=False, env={**os.environ, **_GIT_ENV}
    )
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _init_repo(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    (path / "README.md").write_text("base\n")
    (path / ".gitignore").write_text("ignored/\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "base")
    return _git(path, "rev-parse", "HEAD")


def _files(path: Path) -> dict[str, bytes]:
    return {str(c.relative_to(path)): c.read_bytes() for c in sorted(path.rglob("*")) if c.is_file()}


@dataclass(frozen=True)
class Sandbox:
    repo: Path
    base_sha: str
    cache_root: Path

    def path_for(self, task_id: str, run_id: str = "run-one") -> Path:
        return workspace.expected_path(self.repo, run_id, task_id)

    def bind(self, task_id: str, run_id: str = "run-one") -> workspace.Binding:
        reservation = workspace.reserve(self.path_for(task_id, run_id))
        return workspace.create(
            self.repo,
            self.base_sha,
            reservation,
            workspace.branch_name(run_id, task_id),
            task_id=task_id,
            run_id=run_id,
        )


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    repo = tmp_path / "repo"
    base_sha = _init_repo(repo)
    assert workspace.cache_root() == (home / ".cache" / "brigade" / "worktrees").resolve()
    return Sandbox(repo=repo, base_sha=base_sha, cache_root=workspace.cache_root())


@pytest.mark.parametrize(
    "task_id", ["../x", "a/b", "../..", "x" * 300, "ünïcode-任务", ".hidden", "..", "Leading Space"]
)
def test_hostile_task_ids_stay_direct_children_of_the_cache_root(sandbox, task_id):
    path = sandbox.path_for(task_id)
    assert path.parent == sandbox.cache_root
    assert path.resolve().parent == sandbox.cache_root
    assert path.name.startswith("repo-") and "-batch-" in path.name
    assert len(path.name) <= len("repo-12345678-batch-") + workspace.SLUG_LIMIT + 1 + workspace.DIGEST_SUFFIX_LIMIT
    assert workspace.expected_path(sandbox.repo, "run-one", task_id) == path
    assert workspace.expected_path(sandbox.repo, "run-two", task_id) != path


def test_overlap_refuses_a_repo_root_above_the_cache_root(sandbox, monkeypatch):
    inside = sandbox.repo / "home"
    inside.mkdir()
    monkeypatch.setenv("HOME", str(inside))
    path = workspace.expected_path(sandbox.repo, "run-one", "one")
    assert path.parent != sandbox.cache_root
    with pytest.raises(workspace.WorkspaceError) as refused:
        workspace.check_disjoint(path, sandbox.repo)
    assert refused.value.code == workspace.WORKSPACE_OVERLAP


def test_overlap_refuses_a_repo_root_below_the_candidate(sandbox):
    candidate = sandbox.path_for("one")
    nested_repo = candidate / "nested"
    nested_repo.mkdir(parents=True)
    with pytest.raises(workspace.WorkspaceError) as refused:
        workspace.check_disjoint(candidate, nested_repo)
    assert (refused.value.code, refused.value.reason) == (workspace.WORKSPACE_OVERLAP, "repo-root")


def test_overlap_refuses_a_registered_worktree_containing_the_cache_root(sandbox):
    home = sandbox.cache_root.parents[2]
    home.rmdir()
    _git(sandbox.repo, "worktree", "add", "-b", "home-branch", str(home), "main")
    assert home in workspace.registered_worktree_roots(sandbox.repo)
    path = workspace.expected_path(sandbox.repo, "run-one", "one")
    with pytest.raises(workspace.WorkspaceError) as refused:
        workspace.check_disjoint(path, sandbox.repo)
    assert (refused.value.code, refused.value.reason) == (workspace.WORKSPACE_OVERLAP, "registered-worktree")


def test_overlap_refuses_another_task_path_and_any_other_parent(sandbox):
    first = sandbox.path_for("one")
    workspace.check_disjoint(first, sandbox.repo, [sandbox.path_for("two")])
    with pytest.raises(workspace.WorkspaceError) as refused:
        workspace.check_disjoint(first, sandbox.repo, [first])
    assert (refused.value.code, refused.value.reason) == (workspace.WORKSPACE_OVERLAP, "other-task")
    with pytest.raises(workspace.WorkspaceError) as outside:
        workspace.check_disjoint(sandbox.repo / "sibling", sandbox.repo)
    assert (outside.value.code, outside.value.reason) == (workspace.WORKSPACE_OVERLAP, "outside-cache-root")


def test_reserve_refuses_an_existing_path_without_touching_it(sandbox):
    path = sandbox.path_for("one")
    path.mkdir(parents=True)
    (path / "keep.txt").write_text("precious\n")
    before = _files(path)
    with pytest.raises(workspace.WorkspaceError) as refused:
        workspace.reserve(path)
    assert (refused.value.code, refused.value.reason) == (workspace.WORKSPACE_EXISTS, "path")
    assert _files(path) == before


def test_reserve_refuses_an_existing_reservation_without_touching_it(sandbox):
    path = sandbox.path_for("one")
    reservation = runguard.worktree_lock_dir(path)
    reservation.mkdir(parents=True)
    (reservation / "owner.txt").write_text("held\n")
    before = _files(reservation)
    with pytest.raises(workspace.WorkspaceError) as refused:
        workspace.reserve(path)
    assert (refused.value.code, refused.value.reason) == (workspace.WORKSPACE_EXISTS, "reservation")
    assert _files(reservation) == before
    assert not path.exists()


def test_validate_refuses_a_symlink_at_the_expected_path(sandbox):
    path = sandbox.path_for("one")
    elsewhere = path.parent / "elsewhere"
    elsewhere.mkdir(parents=True)
    path.symlink_to(elsewhere, target_is_directory=True)
    info = path.lstat()
    binding = workspace.Binding(
        task_id="one",
        run_id="run-one",
        base_sha=sandbox.base_sha,
        branch=workspace.branch_name("run-one", "one"),
        path=path,
        dev=info.st_dev,
        ino=info.st_ino,
    )
    with pytest.raises(workspace.WorkspaceError) as refused:
        workspace.validate(binding, sandbox.repo)
    assert (refused.value.code, refused.value.reason) == (workspace.WORKSPACE_INVALID, "symlink")
    assert path.is_symlink()


def test_create_then_validate_succeeds_and_binds_digests_only(sandbox):
    binding = sandbox.bind("one")
    assert binding.path == sandbox.path_for("one")
    assert binding.branch == workspace.branch_name("run-one", "one")
    assert binding.branch.startswith(workspace.BRANCH_PREFIX)
    workspace.validate(binding, sandbox.repo)
    fact = workspace.binding_fact(binding)
    assert set(fact) == {"task_id", "base_sha", "branch_digest", "path_digest", "dev_ino_digest"}
    assert fact["task_id"] == "one" and fact["base_sha"] == sandbox.base_sha
    for field in ("branch_digest", "path_digest", "dev_ino_digest"):
        assert len(fact[field]) == 64 and set(fact[field]) <= set("0123456789abcdef")
    assert str(binding.path) not in fact.values() and binding.branch not in fact.values()


def test_create_refuses_a_branch_or_path_it_does_not_own(sandbox):
    path = sandbox.path_for("one")
    reservation = workspace.reserve(path)
    with pytest.raises(workspace.WorkspaceError) as wrong_branch:
        workspace.create(
            sandbox.repo, sandbox.base_sha, reservation, "brigade/batch/other", task_id="one", run_id="run-one"
        )
    assert (wrong_branch.value.code, wrong_branch.value.reason) == (workspace.WORKSPACE_INVALID, "branch")
    assert not (path / ".git").exists()
    other = workspace.reserve(sandbox.path_for("two"))
    with pytest.raises(workspace.WorkspaceError) as wrong_path:
        workspace.create(
            sandbox.repo,
            sandbox.base_sha,
            other,
            workspace.branch_name("run-one", "two"),
            task_id="one",
            run_id="run-one",
        )
    assert (wrong_path.value.code, wrong_path.value.reason) == (workspace.WORKSPACE_OVERLAP, "unexpected-path")
    assert not (other.path / ".git").exists()


@pytest.mark.parametrize("leftover", ["tracked", "untracked", "ignored"])
def test_validate_refuses_every_kind_of_leftover_file(sandbox, leftover):
    binding = sandbox.bind("one")
    if leftover == "tracked":
        (binding.path / "README.md").write_text("edited in the worker\n")
    elif leftover == "untracked":
        (binding.path / "scratch.txt").write_text("untracked\n")
    else:
        (binding.path / "ignored").mkdir()
        (binding.path / "ignored" / "artifact.bin").write_bytes(b"ignored but present\n")
    with pytest.raises(workspace.WorkspaceError) as refused:
        workspace.validate(binding, sandbox.repo)
    assert (refused.value.code, refused.value.reason) == (workspace.WORKSPACE_INVALID, "dirty")
    workspace.validate(binding, sandbox.repo, pristine=False)


def test_validate_refuses_a_different_repository_at_the_same_commit(sandbox, tmp_path):
    binding = sandbox.bind("one")
    inode = binding.path.lstat().st_ino
    other = tmp_path / "other"
    _init_repo(other)
    for child in list(binding.path.iterdir()):
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
    for child in list(other.iterdir()):
        shutil.move(str(child), str(binding.path / child.name))
    assert binding.path.lstat().st_ino == inode
    assert _git(binding.path, "rev-parse", "HEAD") == sandbox.base_sha
    with pytest.raises(workspace.WorkspaceError) as refused:
        workspace.validate(binding, sandbox.repo)
    assert (refused.value.code, refused.value.reason) == (workspace.WORKSPACE_INVALID, "foreign-common-dir")


@pytest.mark.parametrize("change", ["branch", "detached", "head", "inode"])
def test_validate_refuses_a_swapped_or_moved_checkout(sandbox, change):
    binding = sandbox.bind("one")
    if change == "branch":
        _git(binding.path, "switch", "-q", "-c", "brigade/batch/other-branch")
        expected = "branch"
    elif change == "detached":
        _git(binding.path, "checkout", "-q", "--detach", "HEAD")
        expected = "branch"
    elif change == "head":
        (binding.path / "README.md").write_text("a worker commit\n")
        _git(binding.path, "add", "-A")
        _git(binding.path, "commit", "-q", "-m", "worker")
        expected = "head"
    else:
        binding.path.rename(binding.path.with_name(binding.path.name + "-moved"))
        binding.path.mkdir()
        expected = "identity-changed"
    with pytest.raises(workspace.WorkspaceError) as refused:
        workspace.validate(binding, sandbox.repo)
    assert (refused.value.code, refused.value.reason) == (workspace.WORKSPACE_INVALID, expected)
    assert binding.path.is_dir()


# --- Journal facts -------------------------------------------------------


def _binding(*, path_digest: str = "a" * 64) -> tasks.TaskWorkspaceBinding:
    return tasks.TaskWorkspaceBinding(
        base_sha="b" * 40, branch_digest="c" * 64, path_digest=path_digest, dev_ino_digest="d" * 64
    )


def test_workspace_bound_fact_is_written_at_schema_version_three(task_run):
    from brigade import run_journal, run_lifecycle

    run_dir, workspace_dir = task_run
    accepted(task_run)
    binding = _binding()
    result = tasks.record_workspace_binding(run_dir, workspace=workspace_dir, task_id="one", binding=binding)
    assert result.workspace == binding and result.status == "queued"
    assert result.handle.attempt_id is None and result.attempted is False
    events = run_journal.read_journal_bounded(run_lifecycle._journal_path(run_dir)).events
    bound = [event for event in events if event.event_type == "task.workspace-bound"]
    assert [event.schema_version for event in bound] == [3]
    assert bound[0].payload == {"task_id": "one", **vars(binding)}
    assert all(event.schema_version == 2 for event in events if event.event_type != "task.workspace-bound")
    assert tasks.read_tasks(run_dir)[0].workspace == binding
    # A binding is custody, not a launch: the relaunch fence still sees nothing attempted.
    assert run_batch_execute.retryable_before_launch(run_dir) is True


def test_workspace_bound_replay_is_a_noop_and_a_conflict_refuses(task_run):
    from brigade import run_lifecycle

    run_dir, workspace_dir = task_run
    accepted(task_run)
    binding = _binding()
    tasks.record_workspace_binding(run_dir, workspace=workspace_dir, task_id="one", binding=binding)
    journal_path = run_lifecycle._journal_path(run_dir)
    before = journal_path.read_bytes()
    replayed = tasks.record_workspace_binding(run_dir, workspace=workspace_dir, task_id="one", binding=binding)
    assert replayed.workspace == binding
    assert journal_path.read_bytes() == before
    with pytest.raises(tasks.TaskJournalError, match="workspace binding conflict"):
        tasks.record_workspace_binding(
            run_dir, workspace=workspace_dir, task_id="one", binding=_binding(path_digest="e" * 64)
        )
    assert journal_path.read_bytes() == before


def test_bound_then_prepared_then_claim_is_accepted(task_run):
    run_dir, workspace_dir = task_run
    accepted(task_run)
    binding = _binding()
    tasks.record_workspace_binding(run_dir, workspace=workspace_dir, task_id="one", binding=binding)
    handle = tasks.prepare_attempt(run_dir, workspace=workspace_dir, task_id="one")
    version = tasks.read_tasks(run_dir)[0].version
    decision = tasks.claim_task_launch(
        run_dir, workspace=workspace_dir, handle=handle, expected_version=version, seat="worker", dispatch_attempt=1
    )
    assert decision.outcome == "granted"
    assert tasks.read_tasks(run_dir)[0].workspace == binding


def test_workspace_binding_refuses_after_an_attempt(task_run):
    run_dir, workspace_dir = task_run
    accepted(task_run)
    tasks.prepare_attempt(run_dir, workspace=workspace_dir, task_id="one")
    with pytest.raises(tasks.TaskJournalError, match="pristine queued task history"):
        tasks.record_workspace_binding(run_dir, workspace=workspace_dir, task_id="one", binding=_binding())


def test_equal_repo_names_in_different_places_get_different_paths(sandbox, tmp_path):
    other = tmp_path / "elsewhere" / "repo"
    other.mkdir(parents=True)
    assert workspace.expected_path(other, "run-one", "one") != sandbox.path_for("one")


def test_reserve_refuses_a_dangling_symlink_without_following_it(sandbox, tmp_path):
    path = sandbox.path_for("one")
    outside = tmp_path / "outside"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(outside)
    with pytest.raises(workspace.WorkspaceError) as refused:
        workspace.reserve(path)
    assert refused.value.code == workspace.WORKSPACE_EXISTS
    assert not outside.exists()
    assert not (outside.parent / ".run-locks").exists()


def test_reserve_refusal_leaves_no_lease_behind(sandbox):
    path = sandbox.path_for("one")
    path.mkdir(parents=True)
    with pytest.raises(workspace.WorkspaceError):
        workspace.reserve(path)
    path.rmdir()
    # The refused attempt did not strand a lease that blocks the next one.
    assert workspace.reserve(path).path == path


def test_validate_refuses_when_git_status_fails(sandbox, monkeypatch):
    binding = sandbox.bind("one")
    real = workspace._git

    def failing_status(args, *, cwd, timeout=workspace._GIT_TIMEOUT):
        if args[:1] == ["status"]:
            return workspace.proc.Result(code=128, stdout="", stderr="fatal")
        return real(args, cwd=cwd, timeout=timeout)

    monkeypatch.setattr(workspace, "_git", failing_status)
    with pytest.raises(workspace.WorkspaceError) as refused:
        workspace.validate(binding, sandbox.repo)
    assert (refused.value.code, refused.value.reason) == (workspace.WORKSPACE_INVALID, "status-unreadable")


def test_validate_does_not_run_a_worktree_fsmonitor_or_inherit_git_dir(sandbox, tmp_path, monkeypatch):
    binding = sandbox.bind("one")
    marker = tmp_path / "fsmonitor-ran"
    _git(sandbox.repo, "config", "core.fsmonitor", f"touch {marker}; false")
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "nowhere"))
    workspace.validate(binding, sandbox.repo)
    assert not marker.exists()
