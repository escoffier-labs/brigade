"""Collect one batch task worktree's result into an exact, retained artifact.

Execution and delivery are separate facts. A worker finishing says only that it
finished. This module turns what it left in its reserved checkout into a
recorded result without deleting, resetting, cleaning or checking anything out:
an uncommitted worker's leftovers are committed by Brigade itself with its own
fixed identity and no hooks, the resulting candidate is bounded by a binary
patch from the pinned base, and a sanitized sidecar records both.

Ignored files are counted and left alone: they were never part of the review
surface, so they are never committed and never patched. A refusal records why
and changes nothing, leaving the checkout exactly as the worker left it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Any

from . import localio, run_batch_workspace, run_journal

SCHEMA = "brigade.batch_task_artifact.v1"

# Closed delivery states. Callers branch on these, never on the reason text.
DELIVERY_RETAINED = "retained"
DELIVERY_NO_CHANGES = "no-changes"
DELIVERY_BASE_DIVERGED = "base-diverged"
DELIVERY_COLLECTION_FAILED = "collection-failed"
DELIVERY_STATES = frozenset(
    {DELIVERY_RETAINED, DELIVERY_NO_CHANGES, DELIVERY_BASE_DIVERGED, DELIVERY_COLLECTION_FAILED}
)

EXECUTION_COMPLETED = "completed"
EXECUTION_FAILED = "failed"
EXECUTION_STATES = frozenset({EXECUTION_COMPLETED, EXECUTION_FAILED})

# Above this the commit stays the retained copy and no patch is written.
PATCH_MAX_BYTES = 32 * 1024 * 1024
COMMIT_MESSAGE_PREFIX = "brigade batch task "
_REASON_PATCH_OVERSIZE = "patch-oversize"
_SIDECAR_CHUNK_BYTES = 1024 * 1024
_SIDECAR_MAX_BYTES = 1024 * 1024
_COLLECT_TIMEOUT = 120.0
_SHA_RE = re.compile(r"[0-9a-f]{40}", re.ASCII)
_SUMMARY_FIELDS = ("delivery", "candidate_commit", "patch_ref", "patch_sha256")


def _task_digest(task_id: str) -> str:
    return hashlib.sha256(task_id.encode("utf-8", "surrogatepass")).hexdigest()


def sidecar_name(task_id: str) -> str:
    """The artifact sidecar's file name, keyed by the task identity."""
    return f"batch-artifact-{_task_digest(task_id)}.json"


def patch_name(task_id: str) -> str:
    """The bounded patch's file name, keyed by the task identity."""
    return f"batch-artifact-{_task_digest(task_id)}.patch"


def execution_state(result: Any) -> str:
    """The task's execution fact, derived from the worker result alone."""
    return EXECUTION_COMPLETED if getattr(result, "ok", False) else EXECUTION_FAILED


def _record(binding: run_batch_workspace.Binding, attempt_id: str | None, execution: str) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "task_id": binding.task_id,
        "attempt_id": attempt_id,
        "execution": execution if execution in EXECUTION_STATES else EXECUTION_FAILED,
        "delivery": DELIVERY_COLLECTION_FAILED,
        "reason": None,
        "base_sha": binding.base_sha,
        "branch": binding.branch,
        "candidate_commit": None,
        "patch_ref": None,
        "patch_sha256": None,
        "patch_bytes": None,
        "changed_paths": 0,
        "ignored_left": 0,
    }


def _git_line(checkout: Path, *args: str, reason: str) -> str:
    """One Git answer from a task checkout, or a closed collection refusal."""
    try:
        result = run_batch_workspace.git(list(args), cwd=checkout, timeout=_COLLECT_TIMEOUT)
    except OSError as exc:
        raise run_batch_workspace.WorkspaceError(run_batch_workspace.WORKSPACE_UNAVAILABLE, reason) from exc
    if result.code != 0:
        raise run_batch_workspace.WorkspaceError(run_batch_workspace.WORKSPACE_UNAVAILABLE, reason)
    return result.stdout.strip()


def _split_status(text: str) -> tuple[int, int]:
    """Count ignored entries and everything else in one porcelain status."""
    ignored = changed = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        if line.startswith("!!"):
            ignored += 1
        else:
            changed += 1
    return changed, ignored


def _write_patch(checkout: Path, binding: run_batch_workspace.Binding, candidate: str, run_dir: Path) -> dict[str, Any]:
    """Write the bounded patch, created exclusively with mode ``0600``.

    An oversize diff removes its own partial file and leaves the candidate
    commit as the retained copy. A pre-existing entry is never replaced.
    """
    name = patch_name(binding.task_id)
    path = run_dir / name
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    except OSError as exc:
        raise run_batch_workspace.WorkspaceError(run_batch_workspace.WORKSPACE_UNAVAILABLE, "patch-exists") from exc
    try:
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            try:
                result = run_batch_workspace.git(
                    ["diff", "--binary", "--no-color", "--no-ext-diff", binding.base_sha, candidate],
                    cwd=checkout,
                    timeout=_COLLECT_TIMEOUT,
                    stdout_file=handle,
                )
            except OSError as exc:
                raise run_batch_workspace.WorkspaceError(run_batch_workspace.WORKSPACE_UNAVAILABLE, "git-diff") from exc
            if result.code != 0:
                raise run_batch_workspace.WorkspaceError(run_batch_workspace.WORKSPACE_UNAVAILABLE, "git-diff")
            handle.flush()
            os.fsync(handle.fileno())
        size = path.stat().st_size
        if size > PATCH_MAX_BYTES:
            path.unlink()
            return {"patch_ref": None, "patch_sha256": None, "patch_bytes": None, "reason": _REASON_PATCH_OVERSIZE}
        digest = localio.file_sha256(path)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        try:
            path.unlink()
        except OSError:
            pass
        raise
    return {"patch_ref": name, "patch_sha256": digest, "patch_bytes": size, "reason": None}


def _collect(
    binding: run_batch_workspace.Binding,
    repo_root: Path,
    run_dir: Path,
    record: dict[str, Any],
) -> None:
    """Fill ``record`` from the checkout in place, refusing closed reasons only."""
    # HEAD is allowed to have moved: the worker may have committed on its branch.
    # Everything else still has to be this task's own, bound checkout.
    run_batch_workspace.validate(binding, repo_root, pristine=False, allow_head_moved=True)
    checkout = binding.path
    status = _git_line(checkout, "status", "--porcelain", "--ignored", reason="status-unreadable")
    changed, ignored = _split_status(status)
    record["ignored_left"] = ignored
    if changed:
        # add -A honours the checkout's own ignore rules, so an ignored file the
        # worker left behind stays uncommitted and out of the candidate.
        _git_line(checkout, "add", "-A", reason="git-add")
        _git_line(
            checkout,
            "commit",
            "--no-verify",
            "-m",
            f"{COMMIT_MESSAGE_PREFIX}{binding.task_id}",
            reason="git-commit",
        )
    candidate = _git_line(checkout, "rev-parse", "HEAD", reason="git-head")
    if _SHA_RE.fullmatch(candidate) is None:
        raise run_batch_workspace.WorkspaceError(run_batch_workspace.WORKSPACE_INVALID, "git-head")
    if candidate == binding.base_sha:
        # Nothing was committed and nothing is left over: there is no candidate.
        record["delivery"] = DELIVERY_NO_CHANGES
        return
    record["candidate_commit"] = candidate
    record["changed_paths"] = _changed_paths(checkout, binding.base_sha, candidate)
    try:
        ancestor = run_batch_workspace.git(
            ["merge-base", "--is-ancestor", binding.base_sha, candidate],
            cwd=checkout,
            timeout=_COLLECT_TIMEOUT,
        )
    except OSError as exc:
        raise run_batch_workspace.WorkspaceError(run_batch_workspace.WORKSPACE_UNAVAILABLE, "git-merge-base") from exc
    if ancestor.code == 1:
        # The candidate does not descend from the pinned base, so no patch can
        # be applied to a base checkout. The commit is still recorded.
        record["delivery"] = DELIVERY_BASE_DIVERGED
        return
    if ancestor.code != 0:
        raise run_batch_workspace.WorkspaceError(run_batch_workspace.WORKSPACE_UNAVAILABLE, "git-merge-base")
    record["delivery"] = DELIVERY_RETAINED
    record.update(_write_patch(checkout, binding, candidate, run_dir))


def _changed_paths(checkout: Path, base_sha: str, candidate: str) -> int:
    try:
        result = run_batch_workspace.git(
            ["diff", "--name-only", "--no-color", "--no-ext-diff", base_sha, candidate],
            cwd=checkout,
            timeout=_COLLECT_TIMEOUT,
        )
    except OSError:
        return 0
    if result.code != 0:
        return 0
    return sum(1 for line in result.stdout.splitlines() if line.strip())


def collect(
    binding: run_batch_workspace.Binding,
    repo_root: Path,
    run_dir: Path,
    *,
    execution: str,
    attempt_id: str | None,
) -> dict[str, Any]:
    """Collect one terminal task result into its artifact record.

    Never raises: a refusal is a ``collection-failed`` delivery with a closed
    reason token, so a worker's terminal observation is still recorded. Any
    candidate already determined before the failure stays recorded.
    """
    record = _record(binding, attempt_id, execution)
    try:
        _collect(binding, repo_root, run_dir, record)
    except run_batch_workspace.WorkspaceError as exc:
        record["delivery"] = DELIVERY_COLLECTION_FAILED
        record["reason"] = exc.reason
    except OSError:
        record["delivery"] = DELIVERY_COLLECTION_FAILED
        record["reason"] = "git"
    return record


def write_sidecar(run_dir: Path, record: dict[str, Any]) -> tuple[str, str]:
    """Persist the sanitized sidecar and return its file name and SHA-256."""
    path = run_dir / sidecar_name(str(record["task_id"]))
    localio.write_json(path, record)
    return path.name, localio.file_sha256(path)


def _bounded_bytes(path: Path) -> bytes | None:
    """Read a regular, bounded sidecar without following a symlinked final component."""
    try:
        fd = run_journal._open_nofollow(path, os.O_RDONLY)
    except (OSError, run_journal.RunJournalError):
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _SIDECAR_MAX_BYTES:
            return None
        chunks: list[bytes] = []
        remaining = info.st_size
        while remaining > 0:
            chunk = os.read(fd, min(_SIDECAR_CHUNK_BYTES, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
    except OSError:
        return None
    finally:
        os.close(fd)


def read(run_dir: Path, task_id: str) -> dict[str, Any] | None:
    """The sanitized artifact record for one task, or ``None`` when absent or unreadable."""
    raw = _bounded_bytes(run_dir / sidecar_name(task_id))
    if raw is None:
        return None
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return document if isinstance(document, dict) and document.get("schema") == SCHEMA else None


def summary(record: dict[str, Any] | None) -> dict[str, Any] | None:
    """The additive ``artifact`` member of one batch result entry."""
    if record is None:
        return None
    projected = {field: record.get(field) for field in _SUMMARY_FIELDS}
    if projected["delivery"] not in DELIVERY_STATES:
        return None
    for field in ("candidate_commit", "patch_ref", "patch_sha256"):
        if projected[field] is not None and not isinstance(projected[field], str):
            return None
    return projected
