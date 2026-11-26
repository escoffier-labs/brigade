"""Plan and run fresh verification for a locked batch task checkout."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from time import monotonic

from . import localio, run_batch_workspace
from .work_cmd import verification, verify_required, verify_runtime


class Skip(StrEnum):
    SHARED_CHECKOUT = "shared-checkout"
    UNCONTAINED_CANCEL = "uncontained-cancel"
    REMOTE_OR_CLOUD = "remote-or-cloud"
    NOT_LAUNCHED = "not-launched"
    WORKTREE_UNLOCKED = "worktree-unlocked"
    CI_REQUIRED = "ci-required"


def verify(binding: run_batch_workspace.Binding, workspace: Path, run_dir: Path, attempt_id: str) -> dict[str, object]:
    """Return only the closed batch projection of a coordinator-owned receipt."""
    summary = {"attempt_id": attempt_id, "status": "failed"}
    outcome: dict[str, object] = {"verification": summary}
    target = binding.path
    env = verify_required._planner_env()
    try:
        run_batch_workspace.validate(binding, workspace, pristine=False, allow_head_moved=True)
        deadline = monotonic() + verify_required.PLANNER_TIMEOUT_SECONDS
        tree, head = localio.tree_fingerprint_with_head(target, git_env=env, deadline=deadline)
        if tree is None or head is None:
            return outcome
        plan, _, _ = verify_required.plan_snapshot(target, binding.base_sha, tree, deadline=deadline)
        if plan["mode"] == "full":
            return {"verification_skipped": Skip.CI_REQUIRED.value}
        if plan["mode"] != "selective" or localio.tree_fingerprint_with_head(
            target, git_env=env, deadline=deadline
        ) != (tree, head):
            return outcome
        # Use the same runner as work verify run in fresh mode. Read the
        # returned receipt directly, with all writes outside the worktree.
        with verify_runtime._verify_cancellation_signals():
            receipt, rc = verification._run_verify_commands(
                target,
                [plan["run_argv"]],
                900,
                code_graph_timeout=0,
                batch_receipt=verify_runtime.BatchReceipt(run_dir / "batch-verification", attempt_id, env),
            )
        run_batch_workspace.validate(binding, workspace, pristine=False, allow_head_moved=True)
        after_tree, after_head = localio.tree_fingerprint_with_head(target, git_env=env)
        digests = receipt.get("digests", {})
        digest = digests.get("receipt_sha256")
        if (
            receipt.get("producer_attempt_id") != attempt_id
            or receipt.get("producer_run_id") != run_dir.name
            or receipt.get("tree_fingerprint") != tree
            or receipt.get("tree_fingerprint_head") != head
            or after_tree is None
            or receipt.get("tree_fingerprint_after") != after_tree
            or not isinstance(digest, str)
        ):
            return outcome
        summary["receipt_sha256"] = digest
        if rc == 0 and receipt.get("status") == "completed" and (after_tree, after_head) == (tree, head):
            summary["status"] = "completed"
    except (OSError, RuntimeError, ValueError):
        # Planning, an invalidated worktree or verifier I/O can fail without
        # a usable receipt. The worker still gets its terminal observation.
        return outcome
    return outcome
