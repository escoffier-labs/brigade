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
from collections.abc import Callable
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
_REASON_PATCH_TIMEOUT = "patch-timeout"
_REASON_ARTIFACT_CONFLICT = "artifact-conflict"
_SIDECAR_CHUNK_BYTES = 1024 * 1024
_SIDECAR_MAX_BYTES = 1024 * 1024
_COLLECT_TIMEOUT = 120.0
# The patch is the base-to-candidate binary diff; nothing on this command line
# may consult a driver, a pager, or an external diff program.
_DIFF_ARGS = ("diff", "--binary", "--no-color", "--no-textconv", "--no-ext-diff")
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


def _unlink_partial(path: Path) -> None:
    """Remove only the partial patch this call created exclusively."""
    try:
        path.unlink()
    except OSError:
        pass


def _patch_writer(fd: int | None, digest: Any, state: dict[str, Any]) -> Callable[[bytes], bool]:
    """A bounded ``git diff`` consumer: write when given a handle, digest, and stop past the cap."""

    def consume(chunk: bytes) -> bool:
        if state["bytes"] + len(chunk) > PATCH_MAX_BYTES:
            state["oversize"] = True
            return False
        if fd is not None:
            view = memoryview(chunk)
            while view:
                view = view[os.write(fd, view) :]
        digest.update(chunk)
        state["bytes"] += len(chunk)
        return True

    return consume


def _measure_diff(checkout: Path, binding: run_batch_workspace.Binding, candidate: str) -> tuple[str, int] | None:
    """This diff's digest and size without writing it, or ``None`` when unbounded."""
    digest = hashlib.sha256()
    state: dict[str, Any] = {"bytes": 0, "oversize": False}
    result = run_batch_workspace.git_stream(
        [*_DIFF_ARGS, binding.base_sha, candidate],
        cwd=checkout,
        on_chunk=_patch_writer(None, digest, state),
        timeout=_COLLECT_TIMEOUT,
    )
    if result.timed_out or result.stopped or result.code != 0:
        return None
    return digest.hexdigest(), state["bytes"]


def _write_patch(checkout: Path, binding: run_batch_workspace.Binding, candidate: str, run_dir: Path) -> dict[str, Any]:
    """Stream the bounded patch into an exclusively created mode ``0600`` file.

    Git's stdout is read in bounded chunks: as soon as the next chunk would pass
    ``PATCH_MAX_BYTES`` the command is stopped and reaped, so an oversize diff is
    never written to disk in full. An oversize or timed-out diff removes its own
    partial file and leaves the candidate commit as the retained copy. A
    pre-existing entry is never replaced.
    """
    name = patch_name(binding.task_id)
    path = run_dir / name
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    # O_NOFOLLOW is absent on Windows; where it exists it refuses a symlinked name.
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        # A crash between writing the patch and recording its sidecar leaves the
        # patch behind. The same diff is re-adopted; anything else conflicts and
        # is left exactly as it is.
        measured = _measure_diff(checkout, binding, candidate)
        try:
            matches = (
                measured is not None and path.stat().st_size == measured[1] and localio.file_sha256(path) == measured[0]
            )
        except OSError:
            matches = False
        if not matches or measured is None:
            raise run_batch_workspace.WorkspaceError(
                run_batch_workspace.WORKSPACE_UNAVAILABLE, _REASON_ARTIFACT_CONFLICT
            ) from None
        return {"patch_ref": name, "patch_sha256": measured[0], "patch_bytes": measured[1], "reason": None}
    except OSError as exc:
        raise run_batch_workspace.WorkspaceError(run_batch_workspace.WORKSPACE_UNAVAILABLE, "patch-open") from exc
    digest = hashlib.sha256()
    state: dict[str, Any] = {"bytes": 0, "oversize": False}
    reason: str | None = None
    failed = False
    try:
        try:
            result = run_batch_workspace.git_stream(
                [*_DIFF_ARGS, binding.base_sha, candidate],
                cwd=checkout,
                on_chunk=_patch_writer(fd, digest, state),
                timeout=_COLLECT_TIMEOUT,
            )
        except OSError as exc:
            raise run_batch_workspace.WorkspaceError(run_batch_workspace.WORKSPACE_UNAVAILABLE, "git-diff") from exc
        if result.timed_out:
            reason = _REASON_PATCH_TIMEOUT
        elif state["oversize"]:
            reason = _REASON_PATCH_OVERSIZE
        elif result.code != 0:
            raise run_batch_workspace.WorkspaceError(run_batch_workspace.WORKSPACE_UNAVAILABLE, "git-diff")
        else:
            os.fsync(fd)
    except BaseException:
        failed = True
        raise
    finally:
        # The handle is closed before any unlink, because Windows refuses to
        # remove a file that is still open.
        try:
            os.close(fd)
        except OSError:
            pass
        if failed or reason is not None:
            _unlink_partial(path)
    if reason is not None:
        return {"patch_ref": None, "patch_sha256": None, "patch_bytes": None, "reason": reason}
    size = state["bytes"]
    # The digest comes from the bytes that were streamed, never from reopening
    # a path another process could have swapped.
    return {"patch_ref": name, "patch_sha256": digest.hexdigest(), "patch_bytes": size, "reason": None}


def _replayed(
    existing: dict[str, Any],
    binding: run_batch_workspace.Binding,
    attempt_id: str | None,
    run_dir: Path,
) -> bool:
    """Whether an artifact already written for this exact attempt still holds.

    A second collection for the same attempt must not refuse or overwrite. The
    sidecar is reused only while it still describes the disk: same attempt, base
    and branch, a candidate equal to the branch's current ``HEAD``, and a
    recorded patch whose size and digest match.
    """
    if existing.get("attempt_id") != attempt_id or existing.get("base_sha") != binding.base_sha:
        return False
    if existing.get("branch") != binding.branch:
        return False
    try:
        head = _git_line(binding.path, "rev-parse", "HEAD", reason="git-head")
    except run_batch_workspace.WorkspaceError:
        return False
    candidate = existing.get("candidate_commit")
    if head != (candidate if candidate is not None else binding.base_sha):
        return False
    recorded = existing.get("patch_ref")
    if recorded is None:
        return existing.get("patch_sha256") is None and existing.get("patch_bytes") is None
    if recorded != patch_name(binding.task_id):
        return False
    path = run_dir / recorded
    try:
        return path.stat().st_size == existing.get("patch_bytes") and localio.file_sha256(path) == existing.get(
            "patch_sha256"
        )
    except OSError:
        return False


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
    status = _git_line(
        checkout,
        "status",
        "--porcelain",
        "--ignored",
        "--untracked-files=normal",
        reason="status-unreadable",
    )
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
            "--no-gpg-sign",
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
            ["diff", "--name-only", "--no-color", "--no-textconv", "--no-ext-diff", base_sha, candidate],
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
    existing = read(run_dir, binding.task_id)
    if existing is not None:
        if _replayed(existing, binding, attempt_id, run_dir):
            # A second collection for the same attempt reuses what was already
            # written instead of refusing or overwriting it.
            return existing
        record["reason"] = _REASON_ARTIFACT_CONFLICT
        return record
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
    """Persist the sanitized sidecar and return its file name and SHA-256.

    An existing sidecar is reused only when it already holds exactly this
    record, which a same-attempt replay produces. Anything else raises
    ``OSError`` and leaves the existing artifact untouched.
    """
    path = run_dir / sidecar_name(str(record["task_id"]))
    expected = (json.dumps(record, indent=2, sort_keys=True) + "\n").encode("utf-8")
    try:
        localio.write_json_exclusive(path, record)
    except FileExistsError:
        if _bounded_bytes(path) != expected:
            raise OSError(f"artifact sidecar already exists: {path.name}") from None
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
