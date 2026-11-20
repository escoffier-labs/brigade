"""Dispose one batch task checkout, and recover an interrupted collection.

Two explicit commands live here, and neither of them launches anything.

``runs tasks dispose`` removes one per-task worktree, but only after proving
that the work it holds survives somewhere else, and records that decision as a
closed journal fact (issue #2050). The rule is one sentence: deleting a worktree
must never lose the only copy of work.

* The caller workspace's run lock is held for the whole command, and each task's
  own run lock from the first check through removal and recording, so there is
  no check-then-act gap between a proof and its deletion.
* The checkout is reidentified from the journal binding first: the same
  device/inode, the same recomputed path and branch, the same Git common
  directory. A checkout that is not provably this task's own is refused.
* A retained-copy proof has to hold: an integration ref that still holds the
  recorded integration commit, a digest-matched artifact sidecar whose delivery
  is ``retained`` with the candidate still on the task branch, or a
  ``no-changes`` delivery with an unchanged checkout and the branch at base.
* Ignored files are never collected, so removal would delete them. A checkout
  that still holds any refuses; ``--discard`` is the caller's explicit decision
  to lose them.
* Removal goes through ``git worktree remove`` without ``--force``. A checkout
  Git refuses to remove is left exactly as it is; Brigade never deletes a
  worktree directory itself, and ``--discard`` is the only path that forces it.
* The ``task.disposed`` fact is appended as soon as the removal succeeded and
  while the task lock is still held, before the optional branch delete runs. A
  failed branch delete therefore leaves a recorded disposal and reports its own
  refusal, and a later ``--delete-branch`` run retries only that delete. A crash
  between removal and the fact is repaired by running the command again: the
  checkout is gone and the binding is present, so the missing fact and the
  stranded reservation lease are recorded and released instead of failing
  forever.
* A dry run takes no lock, reads the lock state without mutating it, and reports
  the refusals a read-only check can see, including a checkout Git will refuse
  to remove because it holds uncommitted changes.

``runs tasks collect`` runs the artifact collection a crash between worker exit
and collection skipped. It revalidates the same binding and receipt evidence,
then either appends the observation the batch would have written, or a
``task.artifact-collected`` fact when the observation already exists but named
only a receipt.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Sequence

from . import (
    run_batch_artifacts,
    run_batch_integrate,
    run_batch_workspace,
    run_journal,
    run_lifecycle,
    run_task_journal,
    run_tasks,
    runguard,
)

DISPOSE_SCHEMA = "brigade.runs_tasks_dispose.v1"
COLLECT_SCHEMA = "brigade.runs_tasks_collect.v1"

# Closed disposal modes: the retained-copy proof that allowed the removal, or
# ``discarded`` when the caller decided to dispose without one. Reported
# statuses are closed too; ``planned`` is the dry run, which changes nothing.
MODE_INTEGRATED = "integrated"
MODE_RETAINED = "retained"
MODE_NO_CHANGES = "no-changes"
MODE_DISCARDED = "discarded"
DISPOSAL_MODES = frozenset({MODE_INTEGRATED, MODE_RETAINED, MODE_NO_CHANGES, MODE_DISCARDED})
BRANCH_KEPT = "kept"
BRANCH_DELETED = "deleted"
STATUS_DISPOSED = "disposed"
STATUS_ALREADY = "already-disposed"
STATUS_PLANNED = "planned"
STATUS_REFUSED = "refused"
STATUS_COLLECTED = "collected"
STATUS_ALREADY_COLLECTED = "already-collected"

# Closed refusal codes. Callers branch on these, never on the detail text.
NO_TASK_JOURNAL = "no-task-journal"
JOURNAL_UNREADABLE = "journal-unreadable"
UNKNOWN_RUN = "unknown-run"
UNKNOWN_TASK = "unknown-task"
DUPLICATE_TASK = "duplicate-task"
TARGET_BUSY = "target-busy"
TASK_BUSY = "task-busy"
NOT_TERMINAL = "not-terminal"
NOT_ATTEMPTED = "not-attempted"
NO_RECEIPT = "no-receipt"
WORKSPACE_INVALID = "workspace-invalid"
WORKSPACE_UNAVAILABLE = "workspace-unavailable"
NOT_RETAINED = "not-retained"
BRANCH_MOVED = "branch-moved"
IGNORED_FILES = "ignored-files"
REMOVE_REFUSED = "remove-refused"
ARTIFACT_MISMATCH = "artifact-mismatch"
COLLECTION_FAILED = "collection-failed"
GIT_FAILED = "git-failed"
ERROR_CODES = frozenset(
    "no-task-journal journal-unreadable unknown-run unknown-task duplicate-task target-busy task-busy "
    "not-terminal not-attempted no-receipt workspace-invalid workspace-unavailable not-retained "
    "branch-moved ignored-files remove-refused artifact-mismatch collection-failed git-failed".split()
)

_GIT_TIMEOUT = 60.0
_GIT_WORKTREE_TIMEOUT = 120.0
_SHA_RE = re.compile(r"[0-9a-f]{40}", re.ASCII)


class DisposeError(RuntimeError):
    """One closed refusal; the task's checkout and journal state are unchanged."""

    def __init__(self, code: str, detail: str, *, task_id: str | None = None):
        if code not in ERROR_CODES:
            raise ValueError(f"unknown dispose error code {code!r}")
        self.code = code
        self.detail = detail
        self.task_id = task_id
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class _Checkout:
    """One task's reserved checkout; ``binding`` is ``None`` once the path is gone.

    A missing path is the state a crash between removal and its fact leaves
    behind, so it is a repair to record rather than a refusal.
    """

    path: Path
    branch: str
    base_sha: str
    binding: run_batch_workspace.Binding | None


@dataclass(frozen=True)
class _Plan:
    """One fully checked disposal, before anything is removed or recorded."""

    task_id: str
    path: Path
    branch: str
    binding: run_batch_workspace.Binding | None
    mode: str
    candidate_commit: str | None
    integration_commit: str | None
    branch_kept: str
    integration: run_tasks.TaskIntegration | None


@dataclass(frozen=True)
class _Proof:
    """The retained-copy proof for one task, and the integration entry it used.

    The entry is returned rather than re-derived from ``integrations[-1]``: a
    newer integration may already have lost its ref, while an older one still
    holds the work this proof just found.
    """

    mode: str
    candidate_commit: str | None
    integration_commit: str | None
    integration: run_tasks.TaskIntegration | None


def _git(args: list[str], *, cwd: Path, timeout: float = _GIT_TIMEOUT, stdin: bytes | None = None):
    """Run one Git command through the hardened runner, or refuse closed."""
    try:
        return run_batch_workspace.git(args, cwd=cwd, timeout=timeout, stdin=stdin)
    except OSError as exc:
        raise DisposeError(GIT_FAILED, "git could not be started") from exc


def _line(args: list[str], *, cwd: Path) -> str | None:
    """One Git answer, or ``None`` when Git refuses the question."""
    result = _git(args, cwd=cwd)
    return result.stdout.strip() if result.code == 0 else None


def _ref(workspace: Path, name: str) -> str | None:
    """The commit a ref holds, or ``None`` when it does not exist."""
    return _line(["rev-parse", "--verify", "--quiet", f"{name}^{{commit}}"], cwd=workspace)


def _states(run_dir: Path) -> dict[str, run_tasks.TaskResult]:
    """Every durable task result, or a closed journal refusal."""
    if not run_journal.bound_is_file(run_lifecycle._journal_path(run_dir)):
        raise DisposeError(NO_TASK_JOURNAL, f"no task journal in run {run_dir.name}")
    try:
        results = run_tasks.read_tasks(run_dir)
    except (run_tasks.TaskJournalError, run_journal.RunJournalError, OSError, ValueError) as exc:
        raise DisposeError(JOURNAL_UNREADABLE, f"task journal in run {run_dir.name} is not readable") from exc
    return {result.handle.spec.task_id: result for result in results}


def _workspace(run_dir: Path, workspace: Path | None) -> Path:
    """The caller workspace, mapped onto this command's closed refusal codes."""
    try:
        return run_batch_integrate._workspace(run_dir, workspace)
    except run_batch_integrate.IntegrationError as exc:
        raise DisposeError(exc.code, exc.detail) from exc


def _base_sha(run_dir: Path) -> str:
    """The batch request's pinned base, mapped onto this command's codes."""
    try:
        return run_batch_integrate._request(run_dir)["base_sha"]
    except run_batch_integrate.IntegrationError as exc:
        raise DisposeError(exc.code, exc.detail) from exc


def _sidecar(run_dir: Path, task_id: str, name: str) -> tuple[dict[str, Any] | None, str | None]:
    """One run-directory sidecar, read bounded: its document and its SHA-256."""
    raw = run_batch_integrate._bounded_bytes(run_dir / name)
    if raw is None:
        return None, None
    digest = hashlib.sha256(raw).hexdigest()
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, digest
    return (document if isinstance(document, dict) else None), digest


def _receipt(run_dir: Path, task_id: str) -> tuple[dict[str, Any] | None, str | None]:
    """The durable worker receipt the interrupted collection would have written."""
    digest = hashlib.sha256(task_id.encode("utf-8")).hexdigest()
    return _sidecar(run_dir, task_id, f"batch-result-{digest}.json")


def _artifact_sidecar(run_dir: Path, task_id: str) -> tuple[dict[str, Any] | None, str | None]:
    """The artifact sidecar a collection writes for one task."""
    return _sidecar(run_dir, task_id, run_batch_artifacts.sidecar_name(task_id))


def _checkout(state: run_tasks.TaskResult, run_dir: Path, workspace: Path) -> _Checkout:
    """Rebuild and validate one task's binding from the journal alone.

    The path and branch are recomputed from identities, never read back from a
    file, and the recorded digests have to match.
    """
    task_id = state.handle.spec.task_id
    recorded = state.workspace
    if recorded is None:
        raise DisposeError(WORKSPACE_INVALID, f"task {task_id} has no recorded worktree binding")
    run_id = run_dir.name
    path = run_batch_workspace.expected_path(workspace, run_id, task_id)
    branch = run_batch_workspace.branch_name(run_id, task_id)
    if run_batch_workspace._digest(branch) != recorded.branch_digest:
        raise DisposeError(WORKSPACE_INVALID, f"task {task_id} branch does not match its binding")
    if run_batch_workspace._digest(str(path)) != recorded.path_digest:
        raise DisposeError(WORKSPACE_INVALID, f"task {task_id} path does not match its binding")
    try:
        info = path.lstat()
    except FileNotFoundError:
        return _Checkout(path=path, branch=branch, base_sha=recorded.base_sha, binding=None)
    except OSError as exc:
        raise DisposeError(WORKSPACE_UNAVAILABLE, f"task {task_id} checkout could not be inspected") from exc
    binding = run_batch_workspace.Binding(
        task_id=task_id,
        run_id=run_id,
        base_sha=recorded.base_sha,
        branch=branch,
        path=path,
        dev=info.st_dev,
        ino=info.st_ino,
    )
    if run_batch_workspace.binding_fact(binding)["dev_ino_digest"] != recorded.dev_ino_digest:
        raise DisposeError(WORKSPACE_INVALID, f"task {task_id} checkout is not the reserved one")
    try:
        run_batch_workspace.validate(binding, workspace, pristine=False, allow_head_moved=True)
    except run_batch_workspace.WorkspaceError as exc:
        code = WORKSPACE_UNAVAILABLE if exc.code == run_batch_workspace.WORKSPACE_UNAVAILABLE else WORKSPACE_INVALID
        raise DisposeError(code, f"task {task_id} checkout {exc.code}: {exc.reason}") from exc
    return _Checkout(path=path, branch=branch, base_sha=recorded.base_sha, binding=binding)


def _portrait(checkout: _Checkout) -> list[str]:
    """Every tracked, untracked and ignored entry one checkout still holds."""
    result = _git(["status", "--porcelain", "--ignored", "--untracked-files=normal"], cwd=checkout.path)
    if result.code != 0:
        raise DisposeError(WORKSPACE_INVALID, f"task {checkout.branch} checkout status could not be read")
    return [line for line in result.stdout.splitlines() if line.strip()]


def _recorded_artifact_digest(state: run_tasks.TaskResult) -> str | None:
    """The artifact sidecar digest this task's journal records, if any.

    A commit artifact is named by the terminal observation, or by a later
    ``task.artifact-collected`` fact. Both name the same bytes, so a disagreement
    is a refusal rather than a choice.
    """
    digests: list[str] = []
    if state.artifact_kind == "commit" and state.artifact_digest is not None:
        digests.append(state.artifact_digest)
    if state.collected is not None:
        digests.append(state.collected.artifact_digest)
    if not digests:
        return None
    if len(set(digests)) > 1:
        raise DisposeError(
            ARTIFACT_MISMATCH,
            f"task {state.handle.spec.task_id} records two different artifacts",
            task_id=state.handle.spec.task_id,
        )
    return digests[-1]


def _artifact_record(run_dir: Path, state: run_tasks.TaskResult) -> dict[str, Any] | None:
    """The artifact sidecar matching the journal digest, or ``None`` when absent."""
    task_id = state.handle.spec.task_id
    digest = _recorded_artifact_digest(state)
    if digest is None:
        return None
    document, actual = _artifact_sidecar(run_dir, task_id)
    if document is None or actual != digest:
        return None
    if document.get("schema") != run_batch_artifacts.SCHEMA or document.get("task_id") != task_id:
        return None
    if state.collected is not None and document.get("candidate_commit") != state.collected.candidate_commit:
        raise DisposeError(
            ARTIFACT_MISMATCH, f"task {task_id} recovered artifact names another candidate", task_id=task_id
        )
    return document


def _no_changes_record(run_dir: Path, state: run_tasks.TaskResult, checkout: _Checkout) -> dict[str, Any] | None:
    """The sidecar of a ``no-changes`` delivery, tied to this attempt and base.

    A no-changes collection has no candidate, so its observation names the worker
    receipt instead of the artifact sidecar and there is no digest binding the
    two. The proof is therefore the receipt digest the journal recorded, plus a
    sidecar that agrees with this attempt, base and branch; the caller separately
    requires a clean checkout whose branch is still at that base.
    """
    task_id = state.handle.spec.task_id
    document, _digest = _artifact_sidecar(run_dir, task_id)
    if document is None or document.get("schema") != run_batch_artifacts.SCHEMA:
        return None
    if document.get("task_id") != task_id or document.get("candidate_commit") is not None:
        return None
    if document.get("delivery") != run_batch_artifacts.DELIVERY_NO_CHANGES:
        return None
    if document.get("attempt_id") != state.handle.attempt_id:
        return None
    if document.get("base_sha") != checkout.base_sha or document.get("branch") != checkout.branch:
        return None
    if state.artifact_kind != "receipt" or state.artifact_digest is None:
        return None
    _document, receipt_digest = _receipt(run_dir, task_id)
    return document if receipt_digest == state.artifact_digest else None


def _intact_integration(workspace: Path, state: run_tasks.TaskResult) -> run_tasks.TaskIntegration | None:
    """The newest integration entry whose ref still holds its recorded commit."""
    for entry in reversed(state.integrations):
        if _ref(workspace, run_batch_integrate.ref_name(entry.integration_id)) == entry.integration_commit:
            return entry
    return None


def _proof(run_dir: Path, workspace: Path, state: run_tasks.TaskResult, checkout: _Checkout) -> _Proof:
    """The retained-copy proof for one task, or a ``not-retained`` refusal."""
    task_id = state.handle.spec.task_id
    entry = _intact_integration(workspace, state)
    if entry is not None:
        return _Proof(MODE_INTEGRATED, entry.candidate_commit, entry.integration_commit, entry)
    record = _artifact_record(run_dir, state)
    if record is None:
        record = _no_changes_record(run_dir, state, checkout)
    if record is not None:
        delivery, candidate = record.get("delivery"), record.get("candidate_commit")
        if delivery == run_batch_artifacts.DELIVERY_RETAINED and (
            isinstance(candidate, str) and _SHA_RE.fullmatch(candidate) is not None
        ):
            return _Proof(MODE_RETAINED, candidate, None, None)
        if delivery == run_batch_artifacts.DELIVERY_NO_CHANGES:
            return _Proof(MODE_NO_CHANGES, None, None, None)
    raise DisposeError(
        NOT_RETAINED,
        f"task {task_id} has no retained copy: no live integration ref, no matching retained "
        "artifact and no no-changes delivery. Use --discard to dispose of it anyway",
        task_id=task_id,
    )


def _check_branch(
    workspace: Path,
    checkout: _Checkout,
    *,
    mode: str,
    candidate: str | None,
    delete_branch: bool,
    task_id: str,
) -> None:
    """The task branch still holds exactly the revision the proof recorded."""
    expected = candidate if candidate is not None else checkout.base_sha
    tip = _ref(workspace, f"refs/heads/{checkout.branch}")
    if tip == expected:
        return
    if tip is None and delete_branch and mode == MODE_INTEGRATED:
        # This command already deleted the branch and crashed before recording
        # it. The integration proof still holds, so the repair is safe.
        return
    raise DisposeError(
        BRANCH_MOVED, f"task {task_id} branch tip is {tip or 'absent'}, expected {expected}", task_id=task_id
    )


def _plan(
    checkout: _Checkout,
    task_id: str,
    mode: str,
    candidate: str | None,
    integration_commit: str | None,
    branch_kept: str,
    integration: run_tasks.TaskIntegration | None = None,
) -> _Plan:
    return _Plan(
        task_id=task_id,
        path=checkout.path,
        branch=checkout.branch,
        binding=checkout.binding,
        mode=mode,
        candidate_commit=candidate,
        integration_commit=integration_commit,
        branch_kept=branch_kept,
        integration=integration,
    )


def _plan_task(
    run_dir: Path,
    workspace: Path,
    state: run_tasks.TaskResult,
    base_sha: str,
    *,
    discard: bool,
    delete_branch: bool,
) -> _Plan:
    """Every read-only check for one task, in the documented order."""
    task_id = state.handle.spec.task_id
    if state.status not in run_task_journal.TERMINAL:
        raise DisposeError(NOT_TERMINAL, f"task {task_id} is not terminal in the run journal", task_id=task_id)
    checkout = _checkout(state, run_dir, workspace)
    if checkout.base_sha != base_sha:
        raise DisposeError(WORKSPACE_INVALID, f"task {task_id} was bound to another base", task_id=task_id)
    portrait = _portrait(checkout) if checkout.binding is not None else []
    if discard:
        # The caller's explicit disposal decision: no proof, no cleanup check,
        # and Git's own --force is allowed below.
        return _plan(checkout, task_id, MODE_DISCARDED, None, None, BRANCH_KEPT)
    proof = _proof(run_dir, workspace, state, checkout)
    mode, candidate, integration_commit = proof.mode, proof.candidate_commit, proof.integration_commit
    _check_branch(workspace, checkout, mode=mode, candidate=candidate, delete_branch=delete_branch, task_id=task_id)
    if mode == MODE_NO_CHANGES and any(not line.startswith("!!") for line in portrait):
        # Nothing was collected because nothing changed, so anything here now
        # arrived after collection and exists nowhere else.
        raise DisposeError(NOT_RETAINED, f"task {task_id} checkout still holds uncommitted changes", task_id=task_id)
    if any(line.startswith("!!") for line in portrait):
        raise DisposeError(
            IGNORED_FILES,
            f"task {task_id} checkout still holds ignored files, which are never collected; "
            "use --discard to dispose of them anyway",
            task_id=task_id,
        )
    if any(not line.startswith("!!") for line in portrait):
        # A retained or integrated copy is the branch, never the working tree.
        # Anything uncommitted here arrived after collection, and Git itself
        # refuses to remove a dirty checkout without --force.
        raise DisposeError(
            REMOVE_REFUSED,
            f"task {task_id} checkout still holds uncommitted changes, which exist nowhere else; "
            "commit them, or use --discard to lose them",
            task_id=task_id,
        )
    branch_kept = BRANCH_KEPT
    if delete_branch and mode == MODE_INTEGRATED:
        assert integration_commit is not None and proof.integration is not None
        # The integration commit must still be reachable from the ref of the
        # entry this proof matched: that ref, not the task branch, is then the
        # copy that holds the work. The integration record proves the commit
        # was built from this candidate.
        ref = run_batch_integrate.ref_name(proof.integration.integration_id)
        if _git(["merge-base", "--is-ancestor", integration_commit, ref], cwd=workspace).code != 0:
            raise DisposeError(
                NOT_RETAINED,
                f"task {task_id} integration commit is not reachable from {ref}",
                task_id=task_id,
            )
        branch_kept = BRANCH_DELETED
    return _plan(checkout, task_id, mode, candidate, integration_commit, branch_kept, proof.integration)


def _remove(workspace: Path, plan: _Plan, *, discard: bool) -> None:
    """Let Git remove the checkout; never delete a worktree directory directly."""
    args = ["worktree", "remove"]
    if discard:
        args.append("--force")
    args.append(str(plan.path))
    result = _git(args, cwd=workspace, timeout=_GIT_WORKTREE_TIMEOUT)
    if result.code != 0 or plan.path.exists():
        raise DisposeError(
            REMOVE_REFUSED,
            f"git refused to remove task {plan.task_id}'s checkout; nothing was changed",
            task_id=plan.task_id,
        )


def _delete_branch(workspace: Path, plan: _Plan) -> None:
    """Delete only this task's branch, with a compare-and-swap on the candidate."""
    assert plan.candidate_commit is not None
    ref = f"refs/heads/{plan.branch}"
    if _ref(workspace, ref) is None:
        # Already deleted by an earlier run of this command.
        return
    integration = plan.integration
    if integration is None:
        raise DisposeError(
            NOT_RETAINED, f"task {plan.task_id} has no integration to keep its work", task_id=plan.task_id
        )
    # One atomic transaction: the integration ref that holds this task's work
    # must still be at its recorded commit when the task branch goes. The
    # integration commit is a squash, so it does not keep the candidate by
    # ancestry, and a separate check would leave a window to lose both.
    transaction = (
        f"verify {run_batch_integrate.ref_name(integration.integration_id)} {integration.integration_commit}\n"
        f"delete {ref} {plan.candidate_commit}\n"
    ).encode("ascii")
    result = _git(["update-ref", "--stdin"], cwd=workspace, stdin=transaction)
    if (
        result.code != 0
        and _ref(workspace, run_batch_integrate.ref_name(integration.integration_id)) != integration.integration_commit
    ):
        raise DisposeError(
            NOT_RETAINED,
            f"task {plan.task_id} integration ref moved; the branch was kept",
            task_id=plan.task_id,
        )
    current = _ref(workspace, ref)
    if current is None:
        # The delete succeeded, or Git removed the ref and then reported a
        # failure. Both leave the branch gone, which is the requested outcome.
        return
    if current != plan.candidate_commit:
        raise DisposeError(
            BRANCH_MOVED,
            f"task {plan.task_id} branch moved from {plan.candidate_commit} to {current}",
            task_id=plan.task_id,
        )
    raise DisposeError(
        GIT_FAILED, f"task {plan.task_id} branch could not be deleted (git exit {result.code})", task_id=plan.task_id
    )


def _append(run_dir: Path, workspace: Path, event_type: str, payload: dict[str, Any]) -> None:
    """Publish one checkpoint-paired task fact under the held run lock."""
    try:
        with run_task_journal.transaction(run_dir, workspace=workspace, writing=True) as (run_dir, _events):
            run_task_journal.append(run_dir, workspace=workspace, event_type=event_type, payload=payload)
    except (
        run_task_journal.TaskJournalError,
        run_journal.RunJournalError,
        run_lifecycle.LifecycleJournalError,
        OSError,
    ) as exc:
        raise DisposeError(JOURNAL_UNREADABLE, f"{event_type} could not be recorded") from exc


def _release_lease(path: Path) -> None:
    """Release the empty reservation lease ``run_batch_workspace.reserve`` made.

    Only an empty directory is removed. A live lock, a pending stale claim or a
    recovery gate keeps the lease in place, and none of them is deleted here.
    """
    lease = runguard.worktree_lock_dir(path)
    try:
        if lease.is_dir():
            lease.rmdir()
    except OSError:
        pass


@contextlib.contextmanager
def _run_lock(workspace: Path, run_dir: Path, *, code: str, detail: str) -> Iterator[None]:
    """Hold one checkout's run lock, or refuse with this command's closed code.

    ``stale_action="claim"`` takes over a dead owner's lock only when that
    owner's recorded run_dir is this exact batch run, re-verified inside the
    atomic claim. That is what makes a crashed dispose or collect repairable: it
    held this lock when it died. A live owner, or a dead owner of another run,
    still refuses.
    """
    stack = contextlib.ExitStack()
    try:
        stack.enter_context(runguard.run_lock(workspace, run_dir=run_dir, stale_action="claim"))
    except (runguard.RunLockError, OSError) as exc:
        raise DisposeError(code, detail) from exc
    with stack:
        yield


def _row(
    task_id: str,
    status: str,
    *,
    mode: str | None = None,
    branch_kept: str | None = None,
    refusal: DisposeError | None = None,
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "status": status,
        "mode": mode,
        "branch_kept": branch_kept,
        "refusal": None if refusal is None else {"code": refusal.code, "detail": refusal.detail},
    }


def _retry_branch_delete(run_dir: Path, workspace: Path, state: run_tasks.TaskResult) -> str:
    """Delete the branch of an already-disposed integrated task, or refuse.

    Only the optional branch delete is retried: the disposal is already recorded,
    so this repeats exactly the compare-and-swap on the candidate and the
    reachability proof the first run would have made, and nothing else.
    """
    task_id = state.handle.spec.task_id
    checkout = _checkout(state, run_dir, workspace)
    if _ref(workspace, f"refs/heads/{checkout.branch}") is None:
        # The earlier run deleted it, or a crash left it deleted.
        return BRANCH_DELETED
    entry = _intact_integration(workspace, state)
    if entry is None:
        raise DisposeError(
            NOT_RETAINED,
            f"task {task_id} has no integration ref that still holds its recorded commit; keeping the branch",
            task_id=task_id,
        )
    ref = run_batch_integrate.ref_name(entry.integration_id)
    if _git(["merge-base", "--is-ancestor", entry.integration_commit, ref], cwd=workspace).code != 0:
        raise DisposeError(
            NOT_RETAINED, f"task {task_id} integration commit is not reachable from {ref}", task_id=task_id
        )
    plan = _plan(
        checkout,
        task_id,
        MODE_INTEGRATED,
        entry.candidate_commit,
        entry.integration_commit,
        BRANCH_DELETED,
        integration=entry,
    )
    _delete_branch(workspace, plan)
    return BRANCH_DELETED


def _dispose_one(
    run_dir: Path,
    workspace: Path,
    state: run_tasks.TaskResult,
    base_sha: str,
    *,
    discard: bool,
    delete_branch: bool,
    confirm: bool,
) -> dict[str, Any]:
    """Plan, and when confirmed perform, one task's disposal."""
    task_id = state.handle.spec.task_id
    path = run_batch_workspace.expected_path(workspace, run_dir.name, task_id)
    if state.disposal is not None:
        if not confirm:
            return _row(task_id, STATUS_ALREADY, mode=state.disposal.mode, branch_kept=state.disposal.branch_kept)
        # A crash between the fact and the lease release leaves an empty
        # reservation behind. Take the task lock so no live worker owns it, then
        # release the lease and retry only the optional branch delete.
        refusal: DisposeError | None = None
        branch_kept = state.disposal.branch_kept
        with _run_lock(path, run_dir, code=TASK_BUSY, detail=f"another caller holds task {task_id}'s run lock"):
            if delete_branch and state.disposal.mode == MODE_INTEGRATED:
                try:
                    branch_kept = _retry_branch_delete(run_dir, workspace, state)
                except DisposeError as exc:
                    refusal = exc
        _release_lease(path)
        if refusal is not None:
            # The disposal stands; only the requested branch delete was refused.
            return _row(
                task_id,
                STATUS_REFUSED,
                mode=state.disposal.mode,
                branch_kept=state.disposal.branch_kept,
                refusal=refusal,
            )
        return _row(task_id, STATUS_ALREADY, mode=state.disposal.mode, branch_kept=branch_kept)
    if not confirm:
        plan = _plan_task(run_dir, workspace, state, base_sha, discard=discard, delete_branch=delete_branch)
        return _row(task_id, STATUS_PLANNED, mode=plan.mode, branch_kept=plan.branch_kept)
    branch_kept = BRANCH_KEPT
    branch_error: DisposeError | None = None
    with _run_lock(path, run_dir, code=TASK_BUSY, detail=f"another caller holds task {task_id}'s run lock"):
        plan = _plan_task(run_dir, workspace, state, base_sha, discard=discard, delete_branch=delete_branch)
        if plan.binding is not None:
            _remove(workspace, plan, discard=discard)
        # Removal is irreversible, so the disposal is recorded as soon as it
        # succeeded: a failed branch delete below must not leave the checkout
        # gone with no recorded reason and no way to re-run.
        if plan.branch_kept == BRANCH_DELETED:
            try:
                _delete_branch(workspace, plan)
            except DisposeError as exc:
                branch_error = exc
            else:
                branch_kept = BRANCH_DELETED
        _append(
            run_dir,
            workspace,
            "task.disposed",
            {"task_id": task_id, "mode": plan.mode, "branch_kept": branch_kept},
        )
    # Released after the lock, so the lease directory is empty by the time it is
    # removed. A crash before this line leaves it for the next run to release.
    _release_lease(path)
    if branch_error is not None:
        # The disposal stands; only the requested branch delete is refused.
        raise branch_error
    return _row(task_id, STATUS_DISPOSED, mode=plan.mode, branch_kept=branch_kept)


def _refuse_busy_workspace(workspace: Path, run_dir: Path) -> None:
    """A dry run reads the workspace lock state without taking or changing it.

    Only a state whose owner is gone and which this exact run could claim is
    planned against; a live, foreign or unreadable lock refuses with the same
    code ``--confirm`` would report, and none of them is mutated here.
    """
    state = runguard.run_lock_state(workspace, run_dir)
    if state in {"live", "foreign", "invalid"}:
        raise DisposeError(
            TARGET_BUSY,
            f"this workspace's run lock is {state}; a dry run does not take it",
        )


def dispose(
    run_dir: Path,
    task_ids: Sequence[str],
    *,
    discard: Sequence[str] = (),
    delete_branch: bool = False,
    confirm: bool = False,
    workspace: Path | None = None,
) -> dict[str, Any]:
    """Dispose of the named tasks' worktrees, or report what would happen.

    One task's refusal never stops another: every task is checked and acted on in
    the order given, and a refused task is left exactly as it was.
    """
    try:
        run_dir = run_journal.normalize_run_dir(Path(run_dir))
    except (run_journal.RunJournalError, OSError, ValueError) as exc:
        raise DisposeError(UNKNOWN_RUN, "run directory is not a run") from exc
    workspace = _workspace(run_dir, workspace)
    try:
        ordered = run_batch_integrate._ordered(task_ids)
    except run_batch_integrate.IntegrationError as exc:
        raise DisposeError(exc.code, exc.detail) from exc
    discarded = tuple(discard)
    for task_id in discarded:
        if task_id not in ordered:
            raise DisposeError(
                UNKNOWN_TASK, f"task {task_id} is named by --discard but not by this command", task_id=task_id
            )
    payload: dict[str, Any] = {
        "schema": DISPOSE_SCHEMA,
        "run_id": run_dir.name,
        "confirm": confirm,
        "delete_branch": delete_branch,
        "discard": list(discarded),
        "tasks": [],
        "refusal": None,
    }
    with contextlib.ExitStack() as stack:
        if confirm:
            stack.enter_context(
                _run_lock(workspace, run_dir, code=TARGET_BUSY, detail="another caller holds this workspace's run lock")
            )
        else:
            _refuse_busy_workspace(workspace, run_dir)
        base_sha = _base_sha(run_dir)
        states = _states(run_dir)
        for task_id in ordered:
            state = states.get(task_id)
            if state is None:
                payload["tasks"].append(
                    _row(
                        task_id,
                        STATUS_REFUSED,
                        refusal=DisposeError(
                            UNKNOWN_TASK, f"task {task_id} is not accepted in run {run_dir.name}", task_id=task_id
                        ),
                    )
                )
                continue
            try:
                row = _dispose_one(
                    run_dir,
                    workspace,
                    state,
                    base_sha,
                    discard=task_id in discarded,
                    delete_branch=delete_branch,
                    confirm=confirm,
                )
            except DisposeError as exc:
                row = _row(task_id, STATUS_REFUSED, refusal=exc)
            payload["tasks"].append(row)
    return payload


def collect(run_dir: Path, task_id: str, *, workspace: Path | None = None) -> dict[str, Any]:
    """Run the artifact collection a crashed batch never reached. Never launches."""
    try:
        run_dir = run_journal.normalize_run_dir(Path(run_dir))
    except (run_journal.RunJournalError, OSError, ValueError) as exc:
        raise DisposeError(UNKNOWN_RUN, "run directory is not a run") from exc
    workspace = _workspace(run_dir, workspace)
    try:
        run_batch_integrate._ordered((task_id,))
    except run_batch_integrate.IntegrationError as exc:
        raise DisposeError(exc.code, exc.detail) from exc
    payload: dict[str, Any] = {
        "schema": COLLECT_SCHEMA,
        "run_id": run_dir.name,
        "task_id": task_id,
        "status": None,
        "delivery": None,
        "candidate_commit": None,
        "artifact_digest": None,
        "refusal": None,
    }
    with _run_lock(workspace, run_dir, code=TARGET_BUSY, detail="another caller holds this workspace's run lock"):
        state = _states(run_dir).get(task_id)
        if state is None:
            raise DisposeError(UNKNOWN_TASK, f"task {task_id} is not accepted in run {run_dir.name}")
        if state.handle.attempt_id is None:
            raise DisposeError(NOT_ATTEMPTED, f"task {task_id} has no recorded attempt to collect")
        if state.workspace is None:
            raise DisposeError(WORKSPACE_INVALID, f"task {task_id} has no recorded worktree binding")
        base_sha = _base_sha(run_dir)
        path = run_batch_workspace.expected_path(workspace, run_dir.name, task_id)
        with _run_lock(path, run_dir, code=TASK_BUSY, detail=f"another caller holds task {task_id}'s run lock"):
            payload.update(_collect_one(run_dir, workspace, state, base_sha))
    return payload


def _collect_one(
    run_dir: Path,
    workspace: Path,
    state: run_tasks.TaskResult,
    base_sha: str,
) -> dict[str, Any]:
    """Revalidate the evidence, then record the collection the crash skipped."""
    task_id = state.handle.spec.task_id
    attempt_id = state.handle.attempt_id
    assert attempt_id is not None
    checkout = _checkout(state, run_dir, workspace)
    if checkout.binding is None:
        raise DisposeError(
            WORKSPACE_INVALID, f"task {task_id} checkout is gone; there is nothing left to collect", task_id=task_id
        )
    if checkout.base_sha != base_sha:
        raise DisposeError(WORKSPACE_INVALID, f"task {task_id} was bound to another base", task_id=task_id)
    document, receipt_digest = _receipt(run_dir, task_id)
    if state.artifact_kind == "receipt" and state.artifact_digest is not None:
        if receipt_digest != state.artifact_digest:
            raise DisposeError(
                ARTIFACT_MISMATCH,
                f"task {task_id} receipt does not match the digest its observation recorded",
                task_id=task_id,
            )
    recorded = _recorded_artifact_digest(state)
    if state.collected is not None or recorded is not None:
        # A collection is already recorded. Verify it is still on disk exactly as
        # recorded and write nothing.
        sidecar, actual = _artifact_sidecar(run_dir, task_id)
        digest = recorded if recorded is not None else (state.collected.artifact_digest if state.collected else None)
        if sidecar is None or digest is None or actual != digest:
            raise DisposeError(
                ARTIFACT_MISMATCH,
                f"task {task_id} artifact does not match the digest its journal records",
                task_id=task_id,
            )
        return {
            "status": STATUS_ALREADY_COLLECTED,
            "delivery": state.collected.delivery if state.collected is not None else sidecar.get("delivery"),
            "candidate_commit": (
                state.collected.candidate_commit if state.collected is not None else sidecar.get("candidate_commit")
            ),
            "artifact_digest": digest,
        }
    if state.result_id is not None:
        # A no-changes delivery has no candidate, so its observation names the
        # worker receipt and never the sidecar. A matching sidecar is still the
        # batch's own completed collection, not one this command should repeat.
        unchanged = _no_changes_record(run_dir, state, checkout)
        if unchanged is not None:
            return {
                "status": STATUS_ALREADY_COLLECTED,
                "delivery": unchanged["delivery"],
                "candidate_commit": None,
                "artifact_digest": _artifact_sidecar(run_dir, task_id)[1],
            }
    execution, status = (
        (run_batch_artifacts.EXECUTION_COMPLETED, "completed")
        if document is not None and document.get("ok") is True
        else (run_batch_artifacts.EXECUTION_FAILED, "failed")
    )
    if state.result_id is None and (document is None or receipt_digest is None):
        raise DisposeError(NO_RECEIPT, f"task {task_id} has no durable worker receipt to collect from", task_id=task_id)
    artifact = run_batch_artifacts.collect(
        checkout.binding, workspace, run_dir, execution=execution, attempt_id=attempt_id
    )
    if artifact.get("delivery") == run_batch_artifacts.DELIVERY_COLLECTION_FAILED:
        # A failed collection is not an artifact: record nothing, so the same
        # command can be retried once the checkout is readable again.
        reason = artifact.get("reason") or "unknown"
        raise DisposeError(
            COLLECTION_FAILED, f"task {task_id} collection failed: {reason}; nothing was recorded", task_id=task_id
        )
    try:
        _, sidecar_digest = run_batch_artifacts.write_sidecar(run_dir, artifact)
        run_journal._fsync_directory(run_dir)
    except (OSError, run_journal.RunJournalError) as exc:
        raise DisposeError(
            ARTIFACT_MISMATCH,
            f"task {task_id} artifact sidecar could not be written, or already holds different bytes",
            task_id=task_id,
        ) from exc
    if state.result_id is None:
        # No observation yet: the durable receipt is the execution fact, and this
        # is exactly the observation the batch would have written.
        artifact_kind, artifact_digest = "receipt", receipt_digest
        if artifact.get("candidate_commit") is not None:
            artifact_kind, artifact_digest = "commit", sidecar_digest
        try:
            run_tasks.observe_task(
                run_dir,
                workspace=workspace,
                handle=state.handle,
                observation=run_tasks.TaskObservation(
                    status=status,
                    artifact_kind=artifact_kind,
                    artifact_digest=artifact_digest,
                    containment="quiescent",
                ),
            )
        except (
            run_tasks.TaskJournalError,
            run_journal.RunJournalError,
            run_lifecycle.LifecycleJournalError,
            OSError,
        ) as exc:
            raise DisposeError(JOURNAL_UNREADABLE, "the recovered observation could not be recorded") from exc
        return {
            "status": STATUS_COLLECTED,
            "delivery": artifact.get("delivery"),
            "candidate_commit": artifact.get("candidate_commit"),
            "artifact_digest": artifact_digest,
        }
    # A receipt-only observation: collection never ran, or it failed before it
    # could be recorded. Record it as a closed fact; never rewrite the
    # observation itself.
    candidate = artifact.get("candidate_commit")
    if not isinstance(candidate, str) or _SHA_RE.fullmatch(candidate) is None:
        candidate = None
    delivery = artifact.get("delivery")
    _append(
        run_dir,
        workspace,
        "task.artifact-collected",
        {
            "task_id": task_id,
            "attempt_id": attempt_id,
            "artifact_digest": sidecar_digest,
            "candidate_commit": candidate,
            "delivery": delivery,
        },
    )
    return {
        "status": STATUS_COLLECTED,
        "delivery": delivery,
        "candidate_commit": candidate,
        "artifact_digest": sidecar_digest,
    }


def refusal_payload(error: DisposeError, *, run_id: str | None = None, confirm: bool = False) -> dict[str, Any]:
    """The closed refusal projection for ``--json``."""
    return {
        "schema": DISPOSE_SCHEMA,
        "run_id": run_id,
        "confirm": confirm,
        "tasks": [],
        "refusal": {"code": error.code, "detail": error.detail, "task_id": error.task_id},
    }


def render(payload: dict[str, Any]) -> str:
    """Human output: what happened, or what would happen without ``--confirm``."""
    refusal = payload.get("refusal")
    if refusal is not None:
        return f"error: {refusal['code']}: {refusal['detail']}"
    if payload.get("schema") == COLLECT_SCHEMA:
        lines = [f"run: {payload['run_id']}", f"task {payload['task_id']}: {payload['status']}"]
        for label, field in (
            ("delivery", "delivery"),
            ("candidate", "candidate_commit"),
            ("artifact sha256", "artifact_digest"),
        ):
            if payload[field] is not None:
                lines.append(f"{label}: {payload[field]}")
        lines.append("Brigade did not launch, merge, push or publish anything.")
        return "\n".join(lines)
    lines = [f"run: {payload['run_id']}"]
    for row in payload["tasks"]:
        if row["refusal"] is not None:
            lines.append(f"task {row['task_id']}: refused {row['refusal']['code']} ({row['refusal']['detail']})")
            continue
        detail = f"task {row['task_id']}: {row['status']}"
        if row["mode"] is not None:
            detail += f" ({row['mode']})"
        if row["branch_kept"] is not None:
            detail += f", branch {row['branch_kept']}"
        lines.append(detail)
    if not payload["confirm"]:
        lines.append("dry run: --confirm is required to remove any worktree")
    else:
        lines.append("Brigade did not merge, push or publish anything.")
    return "\n".join(lines)
