"""Git worktree guardrails for `brigade run`."""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import shutil
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic as _monotonic
from time import sleep as _sleep
from uuid import uuid4

from . import localio, proc

_NONTERMINAL_RUN_STATUSES = frozenset(
    {
        "started",
        "planning",
        "dispatching",
        "result-processing",
        "synthesizing",
        "handoff",
        "artifact-collection",
        "running",
    }
)


class _RunLockClock:
    monotonic = staticmethod(_monotonic)
    sleep = staticmethod(_sleep)


# Keep lock-clock patches local instead of replacing the process-wide time module.
time = _RunLockClock()


class RunGuardError(RuntimeError):
    """Base error for run guard failures."""


class RetainRunLockError(RunGuardError):
    """A terminal receipt could not be written, so stale recovery still needs the lock."""


class DirtyWorktreeError(RunGuardError):
    def __init__(self, paths: list[str]):
        self.paths = paths
        shown = ", ".join(paths[:8])
        if len(paths) > 8:
            shown += f", ... ({len(paths)} total)"
        super().__init__(
            f"dirty worktree: {shown}. Commit, stash, clean the tree, or pass --allow-dirty to run anyway."
        )


class RunLockError(RunGuardError):
    pass


@dataclass(frozen=True)
class PatchSummary:
    path: Path
    changed: bool
    tracked_count: int
    untracked_count: int


@dataclass(frozen=True)
class _LockOwnership:
    owner_token: str
    device: int
    inode: int


def _git(cwd: Path, *args: str, timeout: float = 30.0) -> proc.Result:
    return proc.run(["git", *args], cwd=cwd, timeout=timeout)


def is_git_worktree(cwd: Path) -> bool:
    result = _git(cwd, "rev-parse", "--is-inside-work-tree")
    return result.code == 0 and result.stdout.strip() == "true"


def git_root(cwd: Path) -> Path:
    result = _git(cwd, "rev-parse", "--show-toplevel")
    if result.code != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        suffix = f": {detail}" if detail else ""
        raise RunGuardError(f"not a git worktree: {cwd}{suffix}")
    return Path(result.stdout.strip()).resolve()


def dirty_paths(cwd: Path) -> list[str]:
    result = _git(cwd, "status", "--porcelain=v1")
    if result.code != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        suffix = f": {detail}" if detail else ""
        raise RunGuardError(f"not a git worktree: {cwd}{suffix}")
    paths: list[str] = []
    for line in result.stdout.splitlines():
        if not line:
            continue
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        paths.append(path)
    return sorted(paths)


def require_clean_worktree(cwd: Path) -> list[str]:
    # Brigade's own state (run lock, run artifacts) is not user work in progress.
    paths = [path for path in dirty_paths(cwd) if not path.startswith(".brigade/")]
    if paths:
        raise DirtyWorktreeError(paths)
    return paths


def lock_path(cwd: Path) -> Path:
    base = git_root(cwd) if is_git_worktree(cwd) else cwd.resolve()
    return base / ".brigade" / "run.lock"


def resolve_run_lock_workspace(
    run_meta: dict[str, object],
    run_dir: Path,
    *,
    fallback: Path | None = None,
) -> Path | None:
    raw_workspace = run_meta.get("lock_workspace")
    if isinstance(raw_workspace, str) and raw_workspace:
        return Path(raw_workspace).expanduser().resolve()
    run_dir = run_dir.expanduser().resolve()
    if run_dir.parent.name == "runs" and run_dir.parent.parent.name == ".brigade":
        return run_dir.parent.parent.parent
    if fallback is not None:
        return fallback.expanduser().resolve()
    raw_cwd = run_meta.get("cwd")
    if isinstance(raw_cwd, str) and raw_cwd:
        return Path(raw_cwd).expanduser().resolve()
    return None


def _pid_is_active(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        if exc.errno == errno.ESRCH or getattr(exc, "winerror", None) == 87:
            return False
        return True
    return True


def _lock_is_stale(path: Path) -> bool:
    if path.exists() and not path.is_dir():
        raise RunLockError(f"malformed run lock is not a directory: {path}")
    recorded_pids: list[int] = []
    try:
        recorded_pids.append(int((path / "pid").read_text().strip()))
    except (FileNotFoundError, NotADirectoryError, ValueError):
        pass
    except OSError as exc:
        raise RunLockError(f"could not inspect run lock {path}: {exc}") from exc
    owner = _read_lock_owner(path)
    owner_pid = owner.get("pid") if owner is not None else None
    if isinstance(owner_pid, int):
        recorded_pids.append(owner_pid)
    return not any(_pid_is_active(pid) for pid in set(recorded_pids))


def _lock_owner_payload(*, owner_token: str, run_dir: Path | None) -> dict[str, object]:
    return {
        "schema": "brigade.run_lock.v1",
        "owner_token": owner_token,
        "pid": os.getpid(),
        "run_dir": str(run_dir.expanduser().resolve()) if run_dir is not None else None,
        "acquired_at": datetime.now(timezone.utc).isoformat(),
    }


def _read_lock_owner(path: Path) -> dict[str, object] | None:
    try:
        payload = json.loads((path / "owner.json").read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _publish_lock(path: Path, *, run_dir: Path | None) -> _LockOwnership:
    owner_token = uuid4().hex
    candidate = path.with_name(f".{path.name}.{owner_token}.tmp")
    candidate.mkdir()
    try:
        (candidate / "pid").write_text(f"{os.getpid()}\n")
        (candidate / "owner.json").write_text(json.dumps(_lock_owner_payload(owner_token=owner_token, run_dir=run_dir)))
        try:
            candidate.rename(path)
        except OSError:
            if path.exists():
                raise FileExistsError(path) from None
            raise
    finally:
        if candidate.exists():
            shutil.rmtree(candidate, ignore_errors=True)
    published = path.stat()
    return _LockOwnership(owner_token=owner_token, device=published.st_dev, inode=published.st_ino)


def _recovery_claim_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.recovering-{os.getpid()}-{uuid4().hex}.stale")


def _stale_claims(path: Path) -> list[Path]:
    return sorted(path.parent.glob(f".{path.name}.*.stale"))


def _recovery_claim_pid(path: Path, claimed: Path) -> int | None:
    prefix = f".{path.name}.recovering-"
    if not claimed.name.startswith(prefix) or not claimed.name.endswith(".stale"):
        return None
    raw = claimed.name[len(prefix) : -len(".stale")].split("-", 1)[0]
    try:
        return int(raw)
    except ValueError:
        return None


def _owner_with_workspace(owner: dict[str, object] | None, path: Path) -> dict[str, object] | None:
    if owner is None:
        return None
    enriched = dict(owner)
    enriched["_lock_workspace"] = str(path.parent.parent.resolve())
    return enriched


def _claim_stale_lock(path: Path) -> tuple[Path, dict[str, object] | None] | None:
    if not _lock_is_stale(path):
        return None
    claimed = _recovery_claim_path(path)
    try:
        path.rename(claimed)
    except FileNotFoundError:
        return None
    if not _lock_is_stale(claimed):
        if not path.exists():
            claimed.rename(path)
        return None
    return claimed, _owner_with_workspace(_read_lock_owner(claimed), path)


def _claim_existing_stale(path: Path, stale: Path) -> tuple[Path, dict[str, object] | None] | None:
    recovery_pid = _recovery_claim_pid(path, stale)
    if recovery_pid is not None and _pid_is_active(recovery_pid):
        raise RunLockError(f"stale run lock recovery is still active: {stale}")
    claimed = _recovery_claim_path(path)
    try:
        stale.rename(claimed)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RunLockError(f"could not claim stale run lock {stale}: {exc}") from exc
    if not _lock_is_stale(claimed):
        if not stale.exists():
            claimed.rename(stale)
        raise RunLockError(f"stale run lock owner process is still active: {stale}")
    return claimed, _owner_with_workspace(_read_lock_owner(claimed), path)


def _recover_run_artifact(owner: dict[str, object] | None) -> str:
    if owner is None:
        return "unattributable"
    raw_run_dir = owner.get("run_dir")
    owner_pid = owner.get("pid")
    if not isinstance(raw_run_dir, str) or not raw_run_dir or not isinstance(owner_pid, int):
        return "unattributable"
    run_json = Path(raw_run_dir).expanduser().resolve() / "run.json"
    prior_status = "artifact-unavailable"
    try:
        payload = json.loads(run_json.read_text())
    except FileNotFoundError:
        payload = {"schema": "brigade.run.v1", "artifacts": raw_run_dir}
    except json.JSONDecodeError:
        backup = run_json.with_name(f"run.json.corrupt-{uuid4().hex}")
        try:
            run_json.rename(backup)
        except OSError:
            return "write-failed"
        payload = {
            "schema": "brigade.run.v1",
            "artifacts": raw_run_dir,
            "recovery_preserved_artifact": str(backup),
        }
    except OSError:
        return "write-failed"
    if not isinstance(payload, dict):
        backup = run_json.with_name(f"run.json.corrupt-{uuid4().hex}")
        try:
            run_json.rename(backup)
        except OSError:
            return "write-failed"
        payload = {
            "schema": "brigade.run.v1",
            "artifacts": raw_run_dir,
            "recovery_preserved_artifact": str(backup),
        }
    else:
        status = payload.get("status")
        if isinstance(status, str) and status:
            prior_status = status
            if status not in _NONTERMINAL_RUN_STATUSES:
                return "terminal"
    recovered_at = datetime.now(timezone.utc).isoformat()
    detail = f"run owner process {owner_pid} is no longer active"
    failure_attribution: dict[str, object] = {}
    stored_status = payload.get("status")
    active_seats = payload.get("active_seats")
    phase_owner = payload.get("phase_owner")
    if stored_status == "dispatching" and isinstance(active_seats, list):
        seats = [seat for seat in active_seats if isinstance(seat, str) and seat]
        if len(seats) == 1:
            failure_attribution["seat"] = seats[0]
        elif seats:
            failure_attribution["seats"] = seats
    if not failure_attribution and isinstance(phase_owner, str) and phase_owner:
        failure_attribution["seat"] = phase_owner
    if not failure_attribution:
        worker = payload.get("worker")
        orchestrator = payload.get("orchestrator")
        if isinstance(worker, str) and worker:
            failure_attribution["seat"] = worker
        elif isinstance(orchestrator, str) and orchestrator:
            failure_attribution["seat"] = orchestrator
    workspace = owner.get("_lock_workspace")
    if isinstance(workspace, str) and workspace:
        payload.setdefault("cwd", workspace)
        payload.setdefault("lock_workspace", workspace)
    acquired_at = owner.get("acquired_at")
    if isinstance(acquired_at, str) and acquired_at:
        payload.setdefault("started_at", acquired_at)
    payload.update(
        {
            "status": "failed",
            "status_started_at": recovered_at,
            "finished_at": recovered_at,
            "error": detail,
            "failure_phase": "stale-lock-recovery",
            "failure": {
                "phase": "stale-lock-recovery",
                "kind": "owner-process-exited",
                "detail": detail,
                "owner_pid": owner_pid,
                "prior_status": prior_status,
                "recovered_at": recovered_at,
                **failure_attribution,
            },
        }
    )
    try:
        localio.write_json(run_json, payload)
    except OSError:
        return "write-failed"
    return "recovered"


def _restore_claimed_lock(path: Path, claimed: Path) -> Path:
    if path.exists():
        return claimed
    try:
        claimed.rename(path)
    except OSError:
        return claimed
    return path


def _owner_matches_run(owner: dict[str, object] | None, run_dir: Path) -> bool:
    if owner is None:
        return False
    recorded = owner.get("run_dir")
    return isinstance(recorded, str) and Path(recorded).expanduser().resolve() == run_dir


def _quarantine_unattributable(path: Path, claimed: Path) -> None:
    quarantined = path.with_name(f".{path.name}.{uuid4().hex}.orphaned")
    try:
        claimed.rename(quarantined)
    except OSError:
        pass


def _finish_claimed_recovery(path: Path, claimed: Path, owner: dict[str, object] | None) -> str:
    recovery = _recover_run_artifact(owner)
    if recovery == "write-failed":
        retained = _restore_claimed_lock(path, claimed)
        raise RunLockError(f"could not preserve the stale run failure; lock retained at {retained}")
    if recovery == "unattributable":
        _quarantine_unattributable(path, claimed)
        return recovery
    shutil.rmtree(claimed, ignore_errors=True)
    return recovery


def _recover_pending_claims(path: Path, *, run_dir: Path | None = None, required: bool = False) -> bool:
    recovered = False
    for stale in _stale_claims(path):
        owner = _read_lock_owner(stale)
        if run_dir is not None and not _owner_matches_run(owner, run_dir):
            continue
        claimed_owner = _claim_existing_stale(path, stale)
        if claimed_owner is None:
            continue
        claimed, owner = claimed_owner
        _finish_claimed_recovery(path, claimed, owner)
        recovered = True
    if required and not recovered:
        raise RunLockError(f"run lock not found for run: {run_dir}")
    return recovered


def recover_stale_run(cwd: Path, run_dir: Path, *, required: bool = True) -> bool:
    run_dir = run_dir.expanduser().resolve()
    path = lock_path(cwd)
    if path.exists() and not path.is_dir():
        raise RunLockError(f"malformed run lock is not a directory: {path}")
    if path.is_dir():
        owner = _read_lock_owner(path)
        if owner is None:
            raise RunLockError(f"run lock has no owner metadata: {path}")
        if _owner_matches_run(owner, run_dir):
            stale = _claim_stale_lock(path)
            if stale is None:
                if path.exists():
                    raise RunLockError(f"run owner process is still active: {path}")
            else:
                claimed, claimed_owner = stale
                if claimed_owner is None or claimed_owner.get("owner_token") != owner.get("owner_token"):
                    retained = _restore_claimed_lock(path, claimed)
                    raise RunLockError(f"run lock owner changed during recovery; lock retained at {retained}")
                _finish_claimed_recovery(path, claimed, claimed_owner)
                return True
        elif required:
            raise RunLockError(f"run lock belongs to a different run: {path}")
    return _recover_pending_claims(path, run_dir=run_dir, required=required)


def run_recovery_status(cwd: Path, run_dir: Path) -> str:
    cwd = cwd.expanduser().resolve()
    run_dir = run_dir.expanduser().resolve()
    if not cwd.is_dir():
        return "unknown"
    path = lock_path(cwd)
    if path.exists() and not path.is_dir():
        return "unknown"
    candidates = ([path] if path.is_dir() else []) + _stale_claims(path)
    saw_unreadable = False
    for candidate in candidates:
        owner = _read_lock_owner(candidate)
        if owner is None:
            saw_unreadable = True
        elif _owner_matches_run(owner, run_dir):
            return "required"
    return "unknown" if saw_unreadable else "cleared"


def _acquire_lock(path: Path, *, run_dir: Path | None = None) -> _LockOwnership:
    for _ in range(8):
        try:
            ownership = _publish_lock(path, run_dir=run_dir)
        except FileExistsError:
            if path.exists() and not path.is_dir():
                raise RunLockError(f"malformed run lock is not a directory: {path}") from None
            stale = _claim_stale_lock(path)
            if stale is None:
                if not path.exists():
                    continue
                raise RunLockError(
                    f"another brigade run appears active: {path}. Remove the lock only if no run is active."
                ) from None
            claimed, owner = stale
            _finish_claimed_recovery(path, claimed, owner)
            continue
        pending = _stale_claims(path)
        if not pending:
            return ownership
        _release_lock(path, ownership)
        _recover_pending_claims(path)
    raise RunLockError(f"could not acquire run lock: {path}")


def _release_lock(path: Path, ownership: _LockOwnership) -> None:
    try:
        visible = path.stat()
    except OSError:
        return
    if visible.st_dev != ownership.device or visible.st_ino != ownership.inode:
        return
    release_path = path.with_name(f".{path.name}.{ownership.owner_token}.release")
    try:
        path.rename(release_path)
    except FileNotFoundError:
        return
    try:
        released = release_path.stat()
    except OSError:
        return
    if released.st_dev == ownership.device and released.st_ino == ownership.inode:
        shutil.rmtree(release_path, ignore_errors=True)
        return
    if not path.exists():
        release_path.rename(path)


@contextlib.contextmanager
def run_lock(
    cwd: Path,
    *,
    run_dir: Path | None = None,
    wait_seconds: float = 0.0,
    poll_interval: float = 0.1,
):
    if wait_seconds < 0:
        raise ValueError("run lock wait_seconds must be non-negative")
    if poll_interval <= 0:
        raise ValueError("run lock poll_interval must be positive")
    path = lock_path(cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + wait_seconds
    ownership: _LockOwnership | None = None
    while True:
        try:
            ownership = _acquire_lock(path, run_dir=run_dir)
            break
        except RunLockError as exc:
            if wait_seconds == 0:
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RunLockError(f"timed out after {wait_seconds:g}s waiting for run lock: {path}") from exc
            time.sleep(min(poll_interval, remaining))
    retain_lock = False
    try:
        yield path
    except RetainRunLockError:
        retain_lock = True
        raise
    finally:
        if ownership is not None and not retain_lock:
            _release_lock(path, ownership)


def create_detached_worktree(repo: Path, worktree_path: Path) -> Path:
    root = git_root(repo)
    worktree_path = worktree_path.expanduser().resolve()
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    result = _git(root, "worktree", "add", "--detach", str(worktree_path), "HEAD", timeout=120.0)
    if result.code != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RunGuardError(f"failed to create detached worktree: {detail}")
    return worktree_path


def remove_worktree(repo: Path, worktree_path: Path) -> None:
    if not worktree_path.exists():
        return
    root = git_root(repo)
    result = _git(root, "worktree", "remove", "--force", str(worktree_path), timeout=120.0)
    if result.code != 0:
        shutil.rmtree(worktree_path, ignore_errors=True)


def is_primary_checkout(cwd: Path) -> bool:
    """True when cwd is the main checkout rather than a linked git worktree.

    In a primary checkout `--git-dir` and `--git-common-dir` resolve to the
    same ``<toplevel>/.git`` directory; a linked worktree's ``--git-dir`` lives
    under ``<common>/.git/worktrees/<name>`` and so differs from the common dir.
    Returns False when cwd is not inside a work tree at all.
    """
    git_dir = _git(cwd, "rev-parse", "--git-dir")
    common_dir = _git(cwd, "rev-parse", "--git-common-dir")
    if git_dir.code != 0 or common_dir.code != 0:
        return False
    raw_git = git_dir.stdout.strip()
    raw_common = common_dir.stdout.strip()
    try:
        git_path = (Path(raw_git) if Path(raw_git).is_absolute() else (cwd / raw_git)).resolve()
        common_path = (Path(raw_common) if Path(raw_common).is_absolute() else (cwd / raw_common)).resolve()
        return git_path == common_path
    except OSError:
        return False


@dataclass(frozen=True)
class PreRunSnapshot:
    """Pre-run git state used to attribute only worker changes to the worker.

    Captures branch, HEAD, and a content-sensitive fingerprint for every
    tracked file already dirty and every untracked file already present, so
    ground truth can compare final state to the baseline and attribute only
    the paths the worker actually touched (a further edit to a file that was
    already dirty is detected, while a baseline-dirty file the worker left
    alone is excluded). Fingerprints encode content, filesystem type, and
    mode, so deletions and type changes are detected too. Raw file contents
    and secrets are never persisted: only a one-way digest per path is stored.
    A branch/HEAD drift check can fail the run if the ref moves out from under
    the worker.
    """

    branch: str
    head: str
    tracked_dirty: tuple[tuple[str, str], ...]
    untracked: tuple[tuple[str, str], ...]

    @property
    def tracked_dirty_paths(self) -> tuple[str, ...]:
        return tuple(path for path, _ in self.tracked_dirty)

    @property
    def untracked_paths(self) -> tuple[str, ...]:
        return tuple(path for path, _ in self.untracked)


def _tracked_dirty_paths(cwd: Path) -> list[str]:
    result = _git(cwd, "diff", "--name-only", "HEAD")
    if result.code != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RunGuardError(f"failed to list tracked dirty files: {detail}")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _path_fingerprint(cwd: Path, relpath: str) -> str:
    """Content- and type-sensitive fingerprint for a working-tree path.

    Encodes filesystem type (regular file, symlink, directory, special, or
    missing), mode, and content digest so a further edit, a deletion, or a
    type/mode change to a baseline-dirty or untracked file is detected while an
    untouched baseline file compares equal. Only a one-way digest is returned;
    raw file contents are read transiently to hash and never persisted.
    """
    full = cwd / relpath
    try:
        st = full.lstat()
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unreadable"
    mode = st.st_mode & 0o7777
    if stat.S_ISLNK(st.st_mode):
        try:
            target = os.readlink(full)
        except OSError:
            return f"link:{mode}:unreadable"
        return f"link:{mode}:{target}"
    if stat.S_ISDIR(st.st_mode):
        try:
            entries = sorted(os.listdir(full))
        except OSError:
            return f"dir:{mode}:unreadable"
        return f"dir:{mode}:{','.join(entries)}"
    if not stat.S_ISREG(st.st_mode):
        return f"special:{mode}"
    h = hashlib.sha256()
    try:
        with full.open("rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                h.update(chunk)
    except OSError:
        return f"file:{mode}:unreadable"
    return f"file:{mode}:{h.hexdigest()}"


def _fingerprint_map(cwd: Path, paths: list[str]) -> tuple[tuple[str, str], ...]:
    return tuple((path, _path_fingerprint(cwd, path)) for path in sorted(set(paths)))


def capture_pre_run_snapshot(cwd: Path | None) -> PreRunSnapshot | None:
    """Capture pre-run git state, or None when cwd is not a git work tree.

    Raises RunGuardError when the tree is a git work tree but git state cannot
    be read, so preflight fails loudly instead of silently disabling safety.
    """
    if cwd is None or not is_git_worktree(cwd):
        return None
    head = _git(cwd, "rev-parse", "HEAD")
    branch = _git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
    # A repo with no commits has no HEAD; treat that as a non-snapshotable tree
    # so ground truth falls back to its unavailable path rather than crashing.
    if head.code != 0 or branch.code != 0:
        return None
    tracked_dirty_paths = _tracked_dirty_paths(cwd)
    untracked_paths = _untracked_files(cwd)
    return PreRunSnapshot(
        head=head.stdout.strip(),
        branch=branch.stdout.strip(),
        tracked_dirty=_fingerprint_map(cwd, tracked_dirty_paths),
        untracked=_fingerprint_map(cwd, untracked_paths),
    )


def snapshot_payload(snapshot: PreRunSnapshot | None) -> dict[str, object] | None:
    if snapshot is None:
        return None
    return {
        "schema": "brigade.pre_run_snapshot.v1",
        "branch": snapshot.branch,
        "head": snapshot.head,
        "tracked_dirty_files": list(snapshot.tracked_dirty_paths),
        # Content-sensitive fingerprints per path so the persisted snapshot can
        # audit and reproduce the worker-change comparison: a reviewer can re-run
        # the baseline-vs-final fingerprint diff without recapturing the tree.
        # Only one-way digests are stored; raw file contents are never persisted.
        "tracked_dirty_fingerprints": dict(snapshot.tracked_dirty),
        "untracked_files": list(snapshot.untracked_paths),
        "untracked_fingerprints": dict(snapshot.untracked),
    }


def detect_branch_head_drift(cwd: Path | None, snapshot: PreRunSnapshot | None) -> str | None:
    """Return a human detail when branch or HEAD moved since the snapshot."""
    if snapshot is None or cwd is None:
        return None
    head = _git(cwd, "rev-parse", "HEAD")
    branch = _git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
    if head.code != 0 or branch.code != 0:
        detail = head.stderr.strip() or branch.stderr.strip() or "git rev-parse failed"
        return f"could not re-read git state after run: {detail}"
    current_head = head.stdout.strip()
    current_branch = branch.stdout.strip()
    if current_head != snapshot.head and current_branch != snapshot.branch:
        return (
            f"git state drifted during run: branch {snapshot.branch!r} -> {current_branch!r}, "
            f"HEAD {snapshot.head[:12]} -> {current_head[:12]}"
        )
    if current_head != snapshot.head:
        return f"git HEAD drifted during run: {snapshot.head[:12]} -> {current_head[:12]}"
    if current_branch != snapshot.branch:
        return f"git branch drifted during run: {snapshot.branch!r} -> {current_branch!r}"
    return None


def changes_relative_to_snapshot(cwd: Path | None, snapshot: PreRunSnapshot | None) -> tuple[list[str], list[str]]:
    """Changed/untracked files attributable to the worker, relative to snapshot.

    Compares final working-tree state to the content-sensitive baseline so a
    further edit, deletion, or type/mode change to a file that was already
    dirty or untracked before the run is detected, while a baseline file the
    worker left alone is excluded. Newly dirtied tracked files and new
    untracked files are attributed to the worker. The union of baseline and
    final dirty/untracked paths is covered, so a baseline-dirty tracked file
    the worker restored to HEAD (no longer listed by `git diff --name-only
    HEAD`) is still detected as a content change. Returns ([], []) when no
    snapshot is available.

    Fails closed: if a final git query fails, RunGuardError is raised with a
    precise reason instead of returning an empty result the caller would treat
    as an available clean run.
    """
    if snapshot is None or cwd is None:
        return [], []
    try:
        current_tracked = _tracked_dirty_paths(cwd)
    except RunGuardError as exc:
        raise RunGuardError(f"could not re-read tracked dirty files after run: {exc}") from exc
    try:
        current_untracked = _untracked_files(cwd)
    except RunGuardError as exc:
        raise RunGuardError(f"could not re-read untracked files after run: {exc}") from exc
    baseline_tracked = dict(snapshot.tracked_dirty)
    baseline_untracked = dict(snapshot.untracked)

    changed: list[str] = []
    current_tracked_set = set(current_tracked)
    for path in current_tracked:
        baseline = baseline_tracked.get(path)
        if baseline is None:
            # Newly dirtied by the worker.
            changed.append(path)
            continue
        # Already dirty at baseline: attribute only if the worker touched it
        # (content, deletion, or type/mode change).
        if _path_fingerprint(cwd, path) != baseline:
            changed.append(path)
    # A baseline-dirty tracked file the worker restored to HEAD is no longer
    # listed by `git diff --name-only HEAD`, so it is absent from
    # current_tracked. Compare its final fingerprint to the baseline and
    # attribute the restore (a content change back to HEAD) to the worker. This
    # covers the union of baseline and final dirty tracked paths, not just the
    # final set.
    for path, baseline in baseline_tracked.items():
        if path in current_tracked_set:
            continue
        if _path_fingerprint(cwd, path) != baseline:
            changed.append(path)

    untracked: list[str] = []
    current_untracked_set = set(current_untracked)
    for path in current_untracked:
        baseline = baseline_untracked.get(path)
        if baseline is None:
            # New untracked file created by the worker.
            untracked.append(path)
            continue
        # Already untracked at baseline: attribute only if the worker mutated it.
        if _path_fingerprint(cwd, path) != baseline:
            untracked.append(path)

    # A baseline untracked file the worker deleted is no longer listed by git,
    # so compare final state explicitly and report it as an untracked-path
    # change attributable to the worker.
    for path, baseline in baseline_untracked.items():
        if path in current_untracked_set:
            continue
        if _path_fingerprint(cwd, path) != baseline:
            untracked.append(path)

    return changed, untracked


def _tracked_diff(cwd: Path) -> tuple[str, int]:
    result = _git(cwd, "diff", "--binary", "HEAD")
    if result.code != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RunGuardError(f"failed to collect tracked diff: {detail}")
    count = 0
    names = _git(cwd, "diff", "--name-only", "HEAD")
    if names.code == 0:
        count = len([line for line in names.stdout.splitlines() if line.strip()])
    return result.stdout, count


def _untracked_files(cwd: Path) -> list[str]:
    result = _git(cwd, "ls-files", "--others", "--exclude-standard")
    if result.code != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RunGuardError(f"failed to list untracked files: {detail}")
    return sorted(line for line in result.stdout.splitlines() if line.strip())


def _untracked_diff(cwd: Path, relpath: str) -> str:
    result = proc.run(["git", "diff", "--no-index", "--binary", "--", "/dev/null", relpath], cwd=cwd)
    if result.code not in {0, 1}:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RunGuardError(f"failed to collect untracked diff for {relpath}: {detail}")
    return result.stdout


def collect_changes_patch(cwd: Path, patch_path: Path) -> PatchSummary:
    patch_path = patch_path.expanduser().resolve()
    patch_path.parent.mkdir(parents=True, exist_ok=True)
    tracked_patch, tracked_count = _tracked_diff(cwd)
    untracked = _untracked_files(cwd)
    pieces = [tracked_patch] if tracked_patch else []
    pieces.extend(_untracked_diff(cwd, path) for path in untracked)
    # Diff bytes are newline-sensitive: a hunk whose last line is a blank
    # context line ends in " \n", and trimming it corrupts the patch
    # (issue #124). Keep each piece verbatim, only guaranteeing the single
    # trailing newline git already emits.
    content = "".join(piece if piece.endswith("\n") else piece + "\n" for piece in pieces if piece)
    patch_path.write_text(content)
    return PatchSummary(
        path=patch_path,
        changed=bool(content),
        tracked_count=tracked_count,
        untracked_count=len(untracked),
    )


def verify_changes_patch(cwd: Path, patch_path: Path) -> bool:
    """Check the written patch is one git can actually apply.

    The worktree still holds the changes the patch describes, so a valid
    patch must reverse-apply cleanly against it. An empty patch is valid.
    """
    patch_path = patch_path.expanduser().resolve()
    try:
        if patch_path.stat().st_size == 0:
            return True
    except FileNotFoundError:
        return False
    result = proc.run(["git", "apply", "--check", "--reverse", str(patch_path)], cwd=cwd)
    return result.code == 0
