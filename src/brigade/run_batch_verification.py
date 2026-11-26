"""Plan and run fresh verification for a locked batch task checkout."""

from __future__ import annotations

import subprocess
import tempfile
from enum import StrEnum
from pathlib import Path
from time import monotonic

from . import run_batch_verify_sandbox, run_batch_verify_tree, run_batch_workspace
from .work_cmd import verification, verify_required, verify_runtime

_VERIFY_TIMEOUT = 900
_PASS_TRUST = "candidate-authored-tests"


class Skip(StrEnum):
    SHARED_CHECKOUT = "shared-checkout"
    UNCONTAINED_CANCEL = "uncontained-cancel"
    REMOTE_OR_CLOUD = "remote-or-cloud"
    NOT_LAUNCHED = "not-launched"
    WORKTREE_UNLOCKED = "worktree-unlocked"
    CI_REQUIRED = "ci-required"
    WORKSPACE_INVALID = "workspace-invalid"
    TREE_UNAVAILABLE = "tree-unavailable"
    TREE_CHANGED = "tree-changed"
    PLANNER_UNCONFIGURED = "planner-unconfigured"
    PLAN_UNAVAILABLE = "plan-unavailable"
    VERIFY_INFRA_MODIFIED = "verify-infra-modified"
    VERIFIER_UNAVAILABLE = "verifier-unavailable"
    SANDBOX_UNAVAILABLE = "sandbox-unavailable"
    CANDIDATE_SYMLINK_ESCAPE = "candidate-symlink-escape"
    CANDIDATE_IMPORT_MISMATCH = "candidate-import-mismatch"


def _skip(reason: Skip) -> dict[str, object]:
    return {"verification_skipped": reason.value}


def _fingerprint(target: Path, env: dict[str, str]) -> tuple[str | None, str | None]:
    try:
        return run_batch_verify_tree.fingerprint(
            target, env, deadline=monotonic() + verify_required.PLANNER_TIMEOUT_SECONDS
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return None, None


def _run_snapshot(
    source: Path,
    target: Path,
    run_dir: Path,
    attempt_id: str,
    base: str,
    identity: tuple[str, str],
    argv: list[str],
    runner: str | None,
    imports: list[str],
    env: dict[str, str],
) -> dict[str, object]:
    tree, head = identity
    run_batch_verify_tree.materialize(source, target, env, base, head, tree)
    if run_batch_verify_sandbox.has_escaping_symlinks(target.resolve()):
        return _skip(Skip.CANDIDATE_SYMLINK_ESCAPE)
    if _fingerprint(target, env) != identity:
        return _skip(Skip.TREE_UNAVAILABLE)
    if _fingerprint(source, env) != identity:
        return _skip(Skip.TREE_CHANGED)
    command = run_batch_verify_tree.command(source, target, base, argv, runner, env)
    binary = run_batch_verify_sandbox.bwrap()
    sandbox = run_batch_verify_sandbox.prepare(binary, target, env) if binary is not None else None
    if sandbox is None:
        return _skip(Skip.SANDBOX_UNAVAILABLE)
    probe = sandbox.check_imports(imports)
    if probe in (41, 42):
        return _skip(Skip.CANDIDATE_IMPORT_MISMATCH)
    if probe != 0:
        return _skip(Skip.SANDBOX_UNAVAILABLE)
    with verify_runtime._verify_cancellation_signals():
        receipt, rc = verification._run_verify_commands(
            target,
            [sandbox.command(command)],
            _VERIFY_TIMEOUT,
            code_graph_timeout=0,
            batch_receipt=verify_runtime.BatchReceipt(run_dir / "batch-verification", attempt_id, env),
        )
    after_tree, after_head = _fingerprint(target, env)
    summary = {"attempt_id": attempt_id, "status": "failed"}
    if after_tree is not None and (
        receipt.get("producer_attempt_id") == attempt_id
        and receipt.get("producer_run_id") == run_dir.name
        and receipt.get("tree_fingerprint") == tree
        and receipt.get("tree_fingerprint_head") == head
        and receipt.get("tree_fingerprint_after") == after_tree
        and isinstance(digest := receipt.get("digests", {}).get("receipt_sha256"), str)
    ):
        summary["receipt_sha256"] = digest
        if (
            rc == 0
            and receipt.get("status") == "completed"
            and (after_tree, after_head) == identity
            and _fingerprint(source, env) == identity
        ):
            summary["status"] = "completed"
            summary["trust"] = _PASS_TRUST
    return {"verification": summary}


def verify(binding: run_batch_workspace.Binding, workspace: Path, run_dir: Path, attempt_id: str) -> dict[str, object]:
    """Verify the candidate with the base planner and a coordinator-owned checkout."""
    try:
        run_batch_workspace.validate(binding, workspace, pristine=False, allow_head_moved=True)
    except (OSError, run_batch_workspace.WorkspaceError):
        return _skip(Skip.WORKSPACE_INVALID)
    source = binding.path
    base = binding.base_sha
    git_env = run_batch_verify_tree.git_environment()
    tree, head = _fingerprint(source, git_env)
    if tree is None or head is None:
        return _skip(Skip.TREE_UNAVAILABLE)
    try:
        deadline = monotonic() + verify_required.PLANNER_TIMEOUT_SECONDS
        config = verify_required._config_at(source, base, deadline)
        if config is None:
            return _skip(Skip.PLANNER_UNCONFIGURED)
        if config["run_argv"][0] == "{tests}":
            return _skip(Skip.VERIFIER_UNAVAILABLE)
        runner = run_batch_verify_tree.repository_runner(config["run_argv"][0])
        if run_batch_verify_tree.infrastructure_changed(source, git_env, base, tree, config["planner_files"], runner):
            return _skip(Skip.VERIFY_INFRA_MODIFIED)
        imports = run_batch_verify_sandbox.expected_imports(source, base, config.get("import_packages"))
        plan, _, _ = verify_required.plan_snapshot(source, base, tree, deadline=deadline)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        return _skip(Skip.PLAN_UNAVAILABLE)
    if plan["mode"] == "full":
        return _skip(Skip.CI_REQUIRED)
    if plan["mode"] != "selective":
        return _skip(Skip.PLAN_UNAVAILABLE)
    if _fingerprint(source, git_env) != (tree, head):
        return _skip(Skip.TREE_CHANGED)
    try:
        # Nothing from the worker filesystem is copied into this directory.
        with tempfile.TemporaryDirectory(prefix="batch-verify-", dir=run_dir) as scratch:
            env = run_batch_verify_tree.command_environment(Path(scratch), git_env)
            return _run_snapshot(
                source,
                Path(scratch) / "candidate",
                run_dir,
                attempt_id,
                base,
                (tree, head),
                plan["run_argv"],
                runner,
                imports,
                env,
            )
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        return _skip(Skip.VERIFIER_UNAVAILABLE)
