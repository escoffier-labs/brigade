"""Verify the parent checkout of an isolated run."""

from __future__ import annotations

from pathlib import Path

from .. import runguard


def parent_worktree_breach(
    workspace: Path | None,
    snapshot: runguard.PreRunSnapshot | None,
    output_dir: Path | None,
) -> str | None:
    if workspace is None or snapshot is None:
        return None
    # Branch and HEAD changes are classified by the run's drift checkpoints.
    if runguard.detect_branch_head_drift(workspace, snapshot) is not None:
        return None
    try:
        changed, untracked = runguard.changes_relative_to_snapshot(workspace, snapshot)
    except runguard.RunGuardError as exc:
        runguard.raise_if_retained_run_lock(exc)
        return f"isolation breach: could not verify canonical checkout: {exc}"
    if output_dir is not None:
        # Brigade writes receipts and logs here even when the directory is
        # untracked in the parent. Tracked edits are still checked.
        artifacts = output_dir.resolve()
        if artifacts != workspace and artifacts.is_relative_to(workspace):
            untracked = [path for path in untracked if not (workspace / path).is_relative_to(artifacts)]
    escaped = sorted(set(changed + untracked))
    if not escaped:
        return None
    shown = ", ".join(escaped[:10])
    if len(escaped) > 10:
        shown += f", and {len(escaped) - 10} more"
    return f"isolation breach: modified files outside assigned worktree: {shown}"
