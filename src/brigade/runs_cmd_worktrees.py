"""Worktree helpers for the `brigade runs` command family."""

from __future__ import annotations

import json
import sys
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import handoff_worktrees, runguard
from .runguard_gate import _recovery_gate_path

_PRUNE_WORKTREES_SCHEMA = "brigade.runs-prune-worktrees.v1"


def _emit_json(payload: dict[str, object]) -> None:
    print(json.dumps(payload, sort_keys=True))


def _prune_lease(path: Path) -> tuple[str, Path | None]:
    lock = runguard.lock_path(path)
    if any(lock.parent.glob(f".{lock.name}.*.stale")):
        return "retained", None
    owner = runguard.read_lock_owner(lock) or {}
    raw_run_dir = owner.get("run_dir")
    run_dir = Path(raw_run_dir) if isinstance(raw_run_dir, str) and raw_run_dir else None
    state = runguard.run_lock_state(path, run_dir or path)
    if state == "stale" and runguard.inspect_run_lock_reconcile(path) == "live":
        return "live", None
    return state, run_dir


def _recover_prunable_run(path: Path, run_dir: Path) -> None:
    from . import run_checkpoint, run_control_journal, runs_cmd, runs_status

    if not (run_dir / "events" / "lifecycle.jsonl").is_file():
        runguard.recover_stale_run(path, run_dir)
        return

    parseable, meta, error, _ = runs_cmd._read_run_json_state(run_dir)
    if not parseable or meta is None:
        raise runguard.RunGuardError(f"recover the run before pruning: {error}")
    if (
        runguard.resolve_run_lock_workspace(meta, run_dir) != path
        or runs_status.is_externally_controlled(meta)
        or runs_cmd._approval_resume_state(meta) is not None
    ):
        raise runguard.RunGuardError("recover or resume the run before pruning its worktree")

    def before_terminalize(owner: dict[str, object] | None) -> None:
        if not runs_status.is_terminal(meta):
            run_checkpoint.recover_from_checkpoint(run_dir, meta)

    try:
        run_control_journal.hold_journal_for_recovery(
            run_dir, lambda: runguard.recover_stale_run(path, run_dir, before_terminalize=before_terminalize)
        )
    except run_control_journal.ControlJournalError as exc:
        raise runguard.RunGuardError(str(exc)) from exc


def _remove_prunable_worktree(repo_root: Path, path: Path) -> None:
    state, run_dir = _prune_lease(path)
    if state == "stale" and run_dir is not None:
        if run_dir != path:
            _recover_prunable_run(path, run_dir)
    elif state != "absent":
        raise runguard.RunGuardError("run lock is present")

    # The maintenance lock is outside the checkout and refuses to take over
    # an unrelated dead owner that acquired the lease after classification.
    with runguard.run_lock(path, run_dir=path, stale_action="claim") as lock:
        if any(lock.parent.glob(f".{lock.name}.*.stale")):
            raise runguard.RunGuardError("run lock is retained for recovery")
        if (
            handoff_worktrees.git_common_dir(path) != handoff_worktrees.git_common_dir(repo_root)
            or not runguard.worktree_is_clean(path)
            or not runguard.worktree_head_is_branch_backed(path)
        ):
            raise runguard.RunGuardError("worktree changed since classification")
        runguard.remove_worktree(repo_root, path)
        if path.exists():
            raise runguard.RunGuardError("kept: worktree still exists (pending handoffs or git refusal)")
        # With the checkout gone and the maintenance lock still held, there
        # is no surviving stale owner that needs this gate.
        try:
            _recovery_gate_path(lock).unlink(missing_ok=True)
        except OSError as exc:
            print(f"warning: could not remove worktree recovery gate at {lock.parent}: {exc}", file=sys.stderr)
    with suppress(OSError):
        lock.parent.rmdir()


def _old_empty_dir_mtime(path: Path, threshold: datetime) -> datetime | None:
    try:
        if path.is_symlink() or not path.is_dir():
            return None
        mtime = runguard.worktree_mtime(path)
        if mtime <= threshold and next(path.iterdir(), None) is None:
            return mtime
    except OSError:
        pass
    return None


def _prune_abandoned_setup(
    root: Path, prefix: str, threshold: datetime, now: datetime, *, apply: bool
) -> list[dict[str, Any]]:
    """Classify aged, empty setup paths left before the lease was published."""
    leases = root / ".run-locks"
    entries: list[dict[str, Any]] = []
    for base, other in ((root, leases), (leases, root)):
        if base.is_symlink() or not base.is_dir():
            continue
        for path in sorted(base.iterdir()):
            if not path.name.startswith(prefix):
                continue
            mtime = _old_empty_dir_mtime(path, threshold)
            if mtime is None:
                continue
            peer = other / path.name
            if peer.is_symlink() or (peer.exists() and _old_empty_dir_mtime(peer, threshold) is None):
                continue
            age = (now - mtime).total_seconds() / 86400
            entry: dict[str, Any] = {
                "path": str(path),
                "kind": "incomplete-setup",
                "removable": True,
                "reasons": [],
                "age_days": round(age, 2),
                "clean": True,
                "branch_backed": False,
            }
            if apply:
                try:
                    if _old_empty_dir_mtime(path, threshold) != mtime:
                        raise OSError("directory changed during pruning")
                    path.rmdir()
                except OSError as exc:
                    entry["removable"] = False
                    entry["reasons"].append(f"removal failed: {exc}")
            entries.append(entry)
    return entries


def _classify_worktree_for_removal(
    path: Path,
    threshold: datetime,
    older_than_days: int,
) -> dict[str, Any]:
    reasons: list[str] = []
    clean = False
    try:
        clean = runguard.worktree_is_clean(path)
    except runguard.RunGuardError as exc:
        reasons.append(f"unreadable worktree state: {exc}")
    if not clean and not reasons:
        reasons.append("dirty")

    branch_backed = False
    try:
        branch_backed = runguard.worktree_head_is_branch_backed(path)
    except runguard.RunGuardError as exc:
        reasons.append(f"unreadable HEAD state: {exc}")
    if not branch_backed and not any(r.startswith("unreadable HEAD state") for r in reasons):
        reasons.append("detached HEAD with unreachable commits")

    try:
        mtime = runguard.worktree_mtime(path)
        age_seconds = (datetime.now(timezone.utc) - mtime).total_seconds()
        age_days = age_seconds / 86400
        old_enough = age_seconds >= 0 and mtime <= threshold
    except OSError as exc:
        age_days = 0.0
        old_enough = False
        reasons.append(f"unreadable directory age: {exc}")
    if not old_enough and not any(r.startswith("unreadable directory age") for r in reasons):
        reasons.append(f"younger than {older_than_days} days")

    # The lease may live beside the checkout, so Git's clean verdict alone
    # cannot distinguish a disposable tree from a live or retained run.
    state, run_dir = _prune_lease(path)
    has_lease = state != "absent" and not (state == "stale" and run_dir is not None)
    if has_lease:
        reasons.append("run lock is present")

    return {
        "path": str(path),
        "removable": clean and branch_backed and old_enough and not has_lease,
        "reasons": reasons,
        "age_days": round(age_days, 2),
        "clean": clean,
        "branch_backed": branch_backed,
    }


def prune_worktrees(
    cwd: Path,
    *,
    older_than_days: int = 14,
    apply: bool = False,
    json_output: bool = False,
) -> int:
    """List or remove Brigade-created worktrees that are safe to delete.

    A worktree is removable only when it is clean, its HEAD is reachable from
    a branch, and it is older than the threshold. Non-Brigade worktrees are
    never touched. Empty directories from interrupted setup must also age
    past the stale-lease grace period, even with a zero-day threshold.
    """
    target = cwd.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    if older_than_days < 0:
        print(f"error: --older-than must be non-negative: {older_than_days}", file=sys.stderr)
        return 2
    try:
        repo_root = runguard.git_root(target)
    except runguard.RunGuardError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    root = runguard.brigade_worktree_root()
    if not root.is_dir():
        if json_output:
            _emit_json(
                {
                    "schema": _PRUNE_WORKTREES_SCHEMA,
                    "target": str(repo_root),
                    "older_than_days": older_than_days,
                    "apply": apply,
                    "removed": [],
                    "worktrees": [],
                }
            )
        else:
            print(f"no Brigade worktrees found under {root}")
        return 0

    now = datetime.now(timezone.utc)
    threshold = now - timedelta(days=older_than_days)
    repo_common = handoff_worktrees.git_common_dir(repo_root)
    setup_threshold = min(threshold, now - runguard.STALE_LOCK_RECONCILE_MAX_AGE)
    entries = _prune_abandoned_setup(root, f"{repo_root.name}-", setup_threshold, now, apply=apply)
    setup_paths = {Path(entry["path"]) for entry in entries}
    removed = [Path(entry["path"]) for entry in entries if apply and entry["removable"]]
    for path in runguard.brigade_worktrees_for_repo(repo_root):
        if path in setup_paths:
            continue
        common = handoff_worktrees.git_common_dir(path)
        if common is not None and common != repo_common:
            continue
        classification = _classify_worktree_for_removal(path, threshold, older_than_days)
        if classification["removable"]:
            if apply:
                try:
                    _remove_prunable_worktree(repo_root, path)
                    removed.append(path)
                except (runguard.RunGuardError, OSError) as exc:
                    classification["removable"] = False
                    classification["reasons"].append(f"removal failed: {exc}")
        entries.append(classification)

    if json_output:
        _emit_json(
            {
                "schema": _PRUNE_WORKTREES_SCHEMA,
                "target": str(repo_root),
                "older_than_days": older_than_days,
                "apply": apply,
                "removed": [str(p) for p in removed],
                "worktrees": entries,
            }
        )
        return 0

    if not entries:
        print(f"no Brigade worktrees found for {repo_root}")
        return 0

    for item in entries:
        path_str = str(item["path"])
        age_days = float(item["age_days"])
        if item["removable"]:
            if apply:
                print(f"removed: {path_str} ({age_days:g} days old)")
            else:
                print(f"removable: {path_str} ({age_days:g} days old)")
        else:
            print(f"kept: {path_str} ({', '.join(item['reasons'])})")
    return 0
