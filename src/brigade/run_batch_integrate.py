"""Integrate selected batch task candidates into one new, explicit local ref.

Worker completion and integration are separate facts. A retained candidate says
only that a worker produced a commit on its own task branch. This module turns
an explicit, ordered selection of those candidates into a new branch, and does
nothing else: it never checks out, merges, pushes, or publishes, and it never
touches the caller's branch, index or working tree.

The merge is quarantined. Repository config and a tracked ``.gitattributes`` can
name a custom merge driver, and a worker can write either. Every merge therefore
runs in a temporary bare repository under the batch run directory, with its own
empty config, so any attribute naming a custom driver falls back to Git's
builtin text merge. The quarantine reads and writes the caller repository's
object store directly through ``GIT_OBJECT_DIRECTORY``, so the final commit and
its new trees are already in place when the merge finishes. Nothing is
transferred: no ``git fetch``, no URL, no remote and no transport helper, so no
``url.*.insteadOf`` rewrite or ``core.sshCommand`` in the caller config can run,
and nothing in this path can run a hook or a driver.

Every commit this module writes is stamped with the pinned base commit's
timestamp, so the same selection always produces the same commit. That is what
makes a crash between the ref and the record repairable instead of permanent.

The result is recorded before it is published to the journal: one exclusive
``brigade.batch_integration.v1`` sidecar, then one ``task.integrated`` fact per
task, in the caller's given order. A crash between them is repaired by running
the same command again, because the record and the ref are the authority and the
missing facts are a deterministic suffix.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Sequence

from . import (
    localio,
    run_batch_artifacts,
    run_batch_workspace,
    run_journal,
    run_lifecycle,
    run_task_journal,
    run_tasks,
    runguard,
)

RECORD_SCHEMA = "brigade.batch_integration.v1"
CLI_SCHEMA = "brigade.runs_tasks_integrate.v1"

REF_PREFIX = "refs/heads/brigade/integrate/"
RECORD_PREFIX = "batch-integration-"
QUARANTINE_PREFIX = "q-"
# Where quarantine repositories are created. None means the system temporary
# directory: a run directory path is already long enough that Git's own files
# inside it pass the Windows path limit.
QUARANTINE_ROOT: Path | None = None

STATUS_CREATED = "created"
STATUS_CHECKED = "checked"
STATUS_ALREADY = "already-integrated"

# Closed refusal codes. Callers branch on these, never on the detail text.
NO_TASK_JOURNAL = "no-task-journal"
JOURNAL_UNREADABLE = "journal-unreadable"
UNKNOWN_RUN = "unknown-run"
UNKNOWN_TASK = "unknown-task"
DUPLICATE_TASK = "duplicate-task"
TASK_NOT_TERMINAL = "task-not-terminal"
ARTIFACT_MISMATCH = "artifact-mismatch"
NO_CANDIDATE = "no-candidate"
CANDIDATE_MOVED = "candidate-moved"
BASE_DIVERGED = "base-diverged"
BASE_MISMATCH = "base-mismatch"
TARGET_BUSY = "target-busy"
GIT_TOO_OLD = "git-too-old"
CONFLICT = "conflict"
REF_EXISTS = "ref-exists"
REF_CONFLICT = "ref-conflict"
RECORD_CONFLICT = "record-conflict"
GIT_FAILED = "git-failed"
ERROR_CODES = frozenset(
    {
        NO_TASK_JOURNAL,
        JOURNAL_UNREADABLE,
        UNKNOWN_RUN,
        UNKNOWN_TASK,
        DUPLICATE_TASK,
        TASK_NOT_TERMINAL,
        ARTIFACT_MISMATCH,
        NO_CANDIDATE,
        CANDIDATE_MOVED,
        BASE_DIVERGED,
        BASE_MISMATCH,
        TARGET_BUSY,
        GIT_TOO_OLD,
        CONFLICT,
        REF_EXISTS,
        REF_CONFLICT,
        RECORD_CONFLICT,
        GIT_FAILED,
    }
)

# ``merge-tree --write-tree`` raises the floor to 2.38.
MINIMUM_GIT = (2, 38)
MAX_CONFLICT_PATHS = 50
MAX_PATH_CHARS = 160
_MAX_READ_BYTES = 1024 * 1024
_GIT_TIMEOUT = 60.0
_ZERO_SHA = "0" * 40
_SHA_RE = re.compile(r"[0-9a-f]{40}", re.ASCII)
_VERSION_RE = re.compile(r"git version (\d+)\.(\d+)", re.ASCII)
# The quarantine repository's whole configuration. An empty config is the point:
# no merge driver, no hook path, no filter, no alias can be named in it.
QUARANTINE_CONFIG = "[core]\n\trepositoryformatversion = 0\n\tbare = true\n"


class IntegrationError(RuntimeError):
    """One closed integration refusal; nothing was written to the caller checkout."""

    def __init__(
        self,
        code: str,
        detail: str,
        *,
        task_ids: Sequence[str] = (),
        paths: Sequence[str] = (),
    ):
        if code not in ERROR_CODES:
            raise ValueError(f"unknown integration error code {code!r}")
        self.code = code
        self.detail = detail
        self.task_ids = tuple(task_ids)
        self.paths = tuple(paths)
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class Plan:
    """One fully validated integration selection, before any merge happens."""

    run_dir: Path
    workspace: Path
    run_id: str
    request_id: str
    base_sha: str
    task_ids: tuple[str, ...]
    candidate_commits: tuple[str, ...]
    commit_date: str

    @property
    def integration_id(self) -> str:
        return integration_id(self.run_id, self.task_ids, self.base_sha)

    @property
    def ref(self) -> str:
        return ref_name(self.integration_id)

    @property
    def candidates(self) -> dict[str, str]:
        return dict(zip(self.task_ids, self.candidate_commits, strict=True))


def integration_id(run_id: str, task_ids: Sequence[str], base_sha: str) -> str:
    """The 16-hex identity of one ordered selection against one pinned base."""
    material = run_id + "\0" + "\0".join(task_ids) + "\0" + base_sha
    return hashlib.sha256(material.encode("utf-8", "surrogatepass")).hexdigest()[:16]


def ref_name(identity: str) -> str:
    """The only ref an integration may create: a new branch, never a checked-out one."""
    if not run_task_journal.integration_id(identity):
        raise IntegrationError(UNKNOWN_RUN, "integration identity is invalid")
    return f"{REF_PREFIX}{identity}"


def record_name(identity: str) -> str:
    """The run-directory sidecar that records one integration."""
    if not run_task_journal.integration_id(identity):
        raise IntegrationError(UNKNOWN_RUN, "integration identity is invalid")
    return f"{RECORD_PREFIX}{identity}.json"


def _git(
    args: list[str],
    *,
    cwd: Path,
    timeout: float = _GIT_TIMEOUT,
    extra_env: dict[str, str] | None = None,
):
    """Run one Git command through the hardened runner, refusing a closed failure."""
    try:
        return run_batch_workspace.git(args, cwd=cwd, timeout=timeout, extra_env=extra_env)
    except OSError as exc:
        raise IntegrationError(GIT_FAILED, "git could not be started") from exc


def _git_line(args: list[str], *, cwd: Path) -> str:
    result = _git(args, cwd=cwd)
    if result.code != 0:
        raise IntegrationError(GIT_FAILED, f"git {args[0]} failed")
    return result.stdout.strip()


def _bounded_bytes(path: Path) -> bytes | None:
    """Read a regular, bounded file without following a symlinked final component."""
    try:
        fd = run_journal._open_nofollow(path, os.O_RDONLY)
    except (OSError, run_journal.RunJournalError):
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_READ_BYTES:
            return None
        chunks: list[bytes] = []
        remaining = info.st_size
        while remaining > 0:
            chunk = os.read(fd, min(_MAX_READ_BYTES, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
    except OSError:
        return None
    finally:
        os.close(fd)


def _ordered(task_ids: Sequence[str]) -> tuple[str, ...]:
    """The selection exactly as given: unique, valid, and never reordered."""
    if not task_ids:
        raise IntegrationError(UNKNOWN_TASK, "name at least one task to integrate")
    seen: set[str] = set()
    for task_id in task_ids:
        if not run_task_journal.task_id(task_id):
            raise IntegrationError(UNKNOWN_TASK, "a task id is not a valid task identity")
        if task_id in seen:
            raise IntegrationError(DUPLICATE_TASK, f"task {task_id} is named more than once", task_ids=[task_id])
        seen.add(task_id)
    return tuple(task_ids)


def _request(run_dir: Path) -> dict[str, str]:
    """The batch request's pinned base and request identity, from its marker.

    Read exactly like a task sidecar: one bounded regular file whose final
    component is never a symlink, so a marker cannot be repointed at something
    unbounded or at another file.
    """
    raw = _bounded_bytes(run_dir / "batch-request.json")
    if raw is None:
        raise IntegrationError(UNKNOWN_RUN, "run directory has no readable batch request marker")
    try:
        request = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IntegrationError(UNKNOWN_RUN, "batch request marker is not valid JSON") from exc
    if not isinstance(request, dict):
        raise IntegrationError(UNKNOWN_RUN, "batch request marker is not a request")
    request_id = request.get("request_id")
    base_sha = request.get("base_sha")
    if not run_task_journal.token(request_id):
        raise IntegrationError(UNKNOWN_RUN, "batch request identity is invalid")
    if not isinstance(base_sha, str) or _SHA_RE.fullmatch(base_sha) is None:
        raise IntegrationError(UNKNOWN_RUN, "batch request has no pinned base revision")
    return {"request_id": str(request_id), "base_sha": base_sha}


def _states(run_dir: Path) -> dict[str, run_tasks.TaskResult]:
    """Every durable task result, or a closed journal refusal."""
    if not run_journal.bound_is_file(run_lifecycle._journal_path(run_dir)):
        raise IntegrationError(NO_TASK_JOURNAL, f"no task journal in run {run_dir.name}")
    try:
        results = run_tasks.read_tasks(run_dir)
    except (run_tasks.TaskJournalError, run_journal.RunJournalError, OSError, ValueError) as exc:
        raise IntegrationError(JOURNAL_UNREADABLE, f"task journal in run {run_dir.name} is not readable") from exc
    return {result.handle.spec.task_id: result for result in results}


def _check_git(workspace: Path) -> None:
    """Refuse a Git too old to write a merge tree."""
    result = _git(["version"], cwd=workspace)
    if result.code != 0:
        raise IntegrationError(GIT_FAILED, "git version could not be read")
    match = _VERSION_RE.search(result.stdout)
    if match is None:
        raise IntegrationError(GIT_FAILED, "git version could not be read")
    version = (int(match.group(1)), int(match.group(2)))
    if version < MINIMUM_GIT:
        raise IntegrationError(
            GIT_TOO_OLD, f"merge-tree --write-tree needs git {MINIMUM_GIT[0]}.{MINIMUM_GIT[1]} or newer"
        )


def _artifact_digest(state: run_tasks.TaskResult) -> str | None:
    """The sidecar digest this task's journal records for its retained commit.

    A commit artifact can be named by the terminal observation, which is how a
    normal collection records it, or by a later ``task.artifact-collected``
    fact, which is how a recovered collection records one. The latest record
    wins; if both exist they must name the same sidecar bytes.
    """
    digests: list[str] = []
    if state.artifact_kind == "commit" and state.artifact_digest is not None:
        digests.append(state.artifact_digest)
    if state.collected is not None:
        digests.append(state.collected.artifact_digest)
    if not digests:
        return None
    if len(set(digests)) > 1:
        raise IntegrationError(
            ARTIFACT_MISMATCH,
            f"task {state.handle.spec.task_id} records two different artifacts",
            task_ids=[state.handle.spec.task_id],
        )
    return digests[-1]


def _artifact_candidate(
    run_dir: Path,
    run_id: str,
    state: run_tasks.TaskResult,
    base_sha: str,
) -> str:
    """The retained candidate commit this task's recorded artifact names."""
    task_id = state.handle.spec.task_id
    if state.status not in run_task_journal.TERMINAL or state.result_id is None:
        raise IntegrationError(TASK_NOT_TERMINAL, f"task {task_id} is not terminal", task_ids=[task_id])
    if state.handle.attempt_id is None:
        raise IntegrationError(ARTIFACT_MISMATCH, f"task {task_id} has no commit artifact", task_ids=[task_id])
    artifact_digest = _artifact_digest(state)
    if artifact_digest is None:
        raise IntegrationError(ARTIFACT_MISMATCH, f"task {task_id} has no commit artifact", task_ids=[task_id])
    name = run_batch_artifacts.sidecar_name(task_id)
    path = run_dir / name
    raw = _bounded_bytes(path)
    if raw is None:
        raise IntegrationError(
            ARTIFACT_MISMATCH, f"task {task_id} artifact is missing or unreadable", task_ids=[task_id]
        )
    if hashlib.sha256(raw).hexdigest() != artifact_digest:
        raise IntegrationError(
            ARTIFACT_MISMATCH, f"task {task_id} artifact does not match its digest", task_ids=[task_id]
        )
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IntegrationError(
            ARTIFACT_MISMATCH, f"task {task_id} artifact is not valid JSON", task_ids=[task_id]
        ) from exc
    if not isinstance(document, dict) or document.get("schema") != run_batch_artifacts.SCHEMA:
        raise IntegrationError(ARTIFACT_MISMATCH, f"task {task_id} artifact has an unknown schema", task_ids=[task_id])
    if document.get("task_id") != task_id:
        raise IntegrationError(ARTIFACT_MISMATCH, f"task {task_id} artifact names another task", task_ids=[task_id])
    if document.get("branch") != run_batch_workspace.branch_name(run_id, task_id):
        raise IntegrationError(ARTIFACT_MISMATCH, f"task {task_id} artifact names another branch", task_ids=[task_id])
    if document.get("base_sha") != base_sha:
        raise IntegrationError(BASE_MISMATCH, f"task {task_id} was collected against another base", task_ids=[task_id])
    if document.get("delivery") != run_batch_artifacts.DELIVERY_RETAINED:
        raise IntegrationError(NO_CANDIDATE, f"task {task_id} retained no candidate commit", task_ids=[task_id])
    candidate = document.get("candidate_commit")
    if not isinstance(candidate, str) or _SHA_RE.fullmatch(candidate) is None:
        raise IntegrationError(NO_CANDIDATE, f"task {task_id} retained no candidate commit", task_ids=[task_id])
    if state.collected is not None and state.collected.candidate_commit != candidate:
        raise IntegrationError(
            ARTIFACT_MISMATCH, f"task {task_id} recovered artifact names another candidate", task_ids=[task_id]
        )
    return candidate


def _verify_candidate(workspace: Path, run_id: str, task_id: str, candidate: str, base_sha: str) -> None:
    """The task branch still holds the recorded candidate, which descends from base."""
    branch = run_batch_workspace.branch_name(run_id, task_id)
    tip = _git(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}"], cwd=workspace)
    if tip.code != 0 or tip.stdout.strip() != candidate:
        raise IntegrationError(
            CANDIDATE_MOVED, f"task {task_id} branch no longer holds its candidate", task_ids=[task_id]
        )
    ancestor = _git(["merge-base", "--is-ancestor", base_sha, candidate], cwd=workspace)
    if ancestor.code == 1:
        raise IntegrationError(
            BASE_DIVERGED, f"task {task_id} candidate does not descend from the base", task_ids=[task_id]
        )
    if ancestor.code != 0:
        raise IntegrationError(GIT_FAILED, "candidate ancestry could not be read")


def _plan(run_dir: Path, workspace: Path, request: dict[str, str], task_ids: tuple[str, ...]) -> Plan:
    _check_git(workspace)
    states = _states(run_dir)
    candidates: list[str] = []
    for task_id in task_ids:
        state = states.get(task_id)
        if state is None:
            raise IntegrationError(
                UNKNOWN_TASK, f"task {task_id} is not accepted in run {run_dir.name}", task_ids=[task_id]
            )
        candidate = _artifact_candidate(run_dir, run_dir.name, state, request["base_sha"])
        _verify_candidate(workspace, run_dir.name, task_id, candidate, request["base_sha"])
        candidates.append(candidate)
    return Plan(
        run_dir=run_dir,
        workspace=workspace,
        run_id=run_dir.name,
        request_id=request["request_id"],
        base_sha=request["base_sha"],
        task_ids=task_ids,
        candidate_commits=tuple(candidates),
        commit_date=_commit_date(workspace, request["base_sha"]),
    )


def _commit_date(workspace: Path, base_sha: str) -> str:
    """The pinned base commit's timestamp, the fixed stamp of every commit here.

    Git stamps a commit with the current time by default, so two runs over the
    same inputs would produce two different commits and a crash between the ref
    and the record could never be repaired. The pinned base is immutable, so its
    timestamp makes the result a function of the selection alone.
    """
    result = _git(["show", "-s", "--format=%ct", base_sha], cwd=workspace)
    seconds = result.stdout.strip()
    if result.code != 0 or not seconds.isdigit():
        raise IntegrationError(GIT_FAILED, "pinned base timestamp could not be read")
    return f"{seconds} +0000"


def _objects_dir(workspace: Path) -> Path:
    """The caller repository's shared object directory, the quarantine's object store."""
    common = _git_line(["rev-parse", "--git-common-dir"], cwd=workspace)
    path = Path(common)
    if not path.is_absolute():
        path = workspace / path
    try:
        resolved = path.resolve()
    except OSError as exc:
        raise IntegrationError(GIT_FAILED, "repository object directory is unreadable") from exc
    objects = resolved / "objects"
    if not objects.is_dir():
        raise IntegrationError(GIT_FAILED, "repository object directory is unreadable")
    return objects


@contextlib.contextmanager
def _quarantine(run_dir: Path, plan: Plan) -> Iterator[Path]:
    """A private bare repository for every merge, removed afterwards.

    Its configuration is written by Brigade and holds nothing else, so no merge
    driver, hook path, filter or attribute helper can be named by the caller
    repository or a tracked ``.gitattributes``. Merges read and write the
    caller's object store through ``GIT_OBJECT_DIRECTORY``, so nothing is
    transferred back over any Git transport afterwards.
    """
    root = QUARANTINE_ROOT if QUARANTINE_ROOT is not None else Path(tempfile.gettempdir())
    # mkdtemp creates a fresh 0700 directory and never follows or reuses an entry.
    path = Path(tempfile.mkdtemp(dir=str(root), prefix=f"{QUARANTINE_PREFIX}{plan.integration_id[:8]}-"))
    try:
        init = _git(["init", "--bare", "-q", "--template=", str(path)], cwd=run_dir)
        if init.code != 0:
            raise IntegrationError(GIT_FAILED, "quarantine repository could not be created")
        (path / "config").write_text(QUARANTINE_CONFIG, encoding="utf-8")
        yield path
    except BaseException:
        # A cleanup failure must never replace the refusal that caused it.
        shutil.rmtree(path, ignore_errors=True)
        with contextlib.suppress(OSError, run_journal.RunJournalError):
            run_journal._fsync_directory(path.parent)
        raise
    else:
        shutil.rmtree(path, ignore_errors=True)
        try:
            run_journal._fsync_directory(path.parent)
        except (OSError, run_journal.RunJournalError) as exc:
            raise IntegrationError(GIT_FAILED, "quarantine removal could not be recorded") from exc


def _quarantine_env(plan: Plan) -> dict[str, str]:
    """The quarantine's object store and the fixed stamp for every commit it makes.

    Both are layered on after the hardened environment, which drops an ambient
    ``GIT_OBJECT_DIRECTORY`` and any commit dates the caller exported.
    """
    return {
        "GIT_OBJECT_DIRECTORY": str(_objects_dir(plan.workspace)),
        "GIT_AUTHOR_DATE": plan.commit_date,
        "GIT_COMMITTER_DATE": plan.commit_date,
    }


def _merge(
    quarantine: Path,
    current: str,
    candidate: str,
    task_ids: Sequence[str],
    extra_env: dict[str, str],
) -> str:
    """Merge one candidate into the running tree and return the new tree.

    Output is parsed bounded: the first NUL-separated field is the tree, the
    rest are conflicted paths, and at most ``MAX_CONFLICT_PATHS`` are reported.
    A conflict is reported before the output is trusted, because a path that is
    not valid UTF-8 is still a conflict and must not read as a Git failure.
    """
    result = _git(
        [
            "-c",
            "merge.renormalize=false",
            "merge-tree",
            "--write-tree",
            "--no-messages",
            "-z",
            "--name-only",
            current,
            candidate,
        ],
        cwd=quarantine,
        extra_env=extra_env,
    )
    fields = [field for field in result.stdout.split("\0") if field]
    if result.code == 1:
        paths = [field[:MAX_PATH_CHARS] for field in fields[1 : MAX_CONFLICT_PATHS + 1]]
        shown = ", ".join(paths) if paths else "unknown paths"
        raise IntegrationError(
            CONFLICT,
            f"tasks {', '.join(task_ids)} conflict on: {shown}",
            task_ids=list(task_ids),
            paths=paths,
        )
    if result.decode_failed:
        raise IntegrationError(GIT_FAILED, "merge output could not be read")
    if result.code != 0:
        raise IntegrationError(GIT_FAILED, "merge-tree failed")
    tree = fields[0] if fields else ""
    if _SHA_RE.fullmatch(tree) is None:
        raise IntegrationError(GIT_FAILED, "merge-tree returned no tree")
    return tree


def _commit(quarantine: Path, tree: str, parent: str, message: str, extra_env: dict[str, str]) -> str:
    """A synthetic commit with the fixed Brigade identity, dates and no hooks."""
    result = _git(["commit-tree", tree, "-p", parent, "-m", message], cwd=quarantine, extra_env=extra_env)
    commit = result.stdout.strip()
    if result.code != 0 or _SHA_RE.fullmatch(commit) is None:
        raise IntegrationError(GIT_FAILED, "commit-tree failed")
    return commit


def _merge_all(plan: Plan, quarantine: Path, extra_env: dict[str, str]) -> str:
    """Merge every candidate in the caller's order; the last tree is the result."""
    current = plan.base_sha
    tree = ""
    for index, (task_id, candidate) in enumerate(zip(plan.task_ids, plan.candidate_commits, strict=True)):
        tree = _merge(quarantine, current, candidate, plan.task_ids[: index + 1], extra_env)
        current = _commit(quarantine, tree, current, f"brigade: integrate batch task {task_id}", extra_env)
    return tree


def _require_present(plan: Plan, commit: str) -> None:
    """The merged commit must already be in the caller repository's object store.

    Quarantined merges write there directly, so there is no transfer step at
    all: no URL is resolved and no transport helper, credential helper or
    ``url.*.insteadOf`` rewrite from the caller's config is consulted.
    """
    present = _git(["cat-file", "-e", f"{commit}^{{commit}}"], cwd=plan.workspace)
    if present.code != 0:
        raise IntegrationError(GIT_FAILED, "integration commit is missing from the object store")


def _final_commit(plan: Plan, quarantine: Path, tree: str, extra_env: dict[str, str]) -> str:
    """The integration commit, parented on the pinned base."""
    message = f"brigade: integrate batch {plan.request_id} tasks {' '.join(plan.task_ids)}"
    return _commit(quarantine, tree, plan.base_sha, message, extra_env)


def _ref_commit(plan: Plan) -> str | None:
    """The commit the result ref holds now, or ``None`` when it is absent."""
    current = _git(["rev-parse", "--verify", "--quiet", plan.ref], cwd=plan.workspace)
    return current.stdout.strip() if current.code == 0 else None


def _create_ref(plan: Plan, commit: str) -> None:
    """Create the result branch with an absent-ref compare-and-swap.

    Integration commits are deterministic, so a ref that already holds this
    exact commit is this selection's own ref: either a previous run created it
    and crashed before recording, or a concurrent identical run won the race.
    Both continue into verification and repair. Any other commit owns the name
    and is refused.
    """
    cas = _git(["update-ref", plan.ref, commit, _ZERO_SHA], cwd=plan.workspace)
    if cas.code == 0:
        return
    current = _ref_commit(plan)
    if current == commit:
        return
    if current is not None:
        raise IntegrationError(REF_CONFLICT, f"{plan.ref} already exists at another commit")
    raise IntegrationError(GIT_FAILED, "integration ref could not be created")


def _record(plan: Plan, commit: str) -> dict[str, Any]:
    return {
        "schema": RECORD_SCHEMA,
        "status": STATUS_CREATED,
        "integration_id": plan.integration_id,
        "run_id": plan.run_id,
        "request_id": plan.request_id,
        "base_sha": plan.base_sha,
        "task_ids": list(plan.task_ids),
        "candidate_commits": [
            {"task_id": task_id, "candidate_commit": candidate}
            for task_id, candidate in zip(plan.task_ids, plan.candidate_commits, strict=True)
        ],
        "integration_commit": commit,
        "ref": plan.ref,
    }


def _read_record(plan: Plan) -> dict[str, Any] | None:
    """The existing integration record, or ``None`` when none was written.

    An entry at the recorded name that is missing, unreadable, symlinked,
    oversized or not this schema refuses instead of being replaced.
    """
    path = plan.run_dir / record_name(plan.integration_id)
    raw = _bounded_bytes(path)
    if raw is None:
        if os.path.lexists(path):
            raise IntegrationError(RECORD_CONFLICT, f"{path.name} exists but is not a readable record")
        return None
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IntegrationError(RECORD_CONFLICT, f"{path.name} is not valid JSON") from exc
    if not isinstance(document, dict):
        raise IntegrationError(RECORD_CONFLICT, f"{path.name} is not an integration record")
    return document


def _verify_record(plan: Plan, record: dict[str, Any]) -> str:
    """The record must describe exactly this selection, and nothing else."""
    commit = record.get("integration_commit")
    if not isinstance(commit, str) or _SHA_RE.fullmatch(commit) is None:
        raise IntegrationError(RECORD_CONFLICT, f"integration record for {plan.integration_id} names no commit")
    if record != _record(plan, commit):
        raise IntegrationError(RECORD_CONFLICT, f"integration record for {plan.integration_id} does not match")
    return commit


def _present(plan: Plan, commit: str) -> set[str]:
    """Task ids whose ``task.integrated`` fact for this integration is durable."""
    try:
        results = run_tasks.read_tasks(plan.run_dir)
    except (run_tasks.TaskJournalError, run_journal.RunJournalError, OSError, ValueError) as exc:
        raise IntegrationError(JOURNAL_UNREADABLE, f"task journal in run {plan.run_id} is not readable") from exc
    by_id = {result.handle.spec.task_id: result for result in results}
    candidates = plan.candidates
    present: set[str] = set()
    for task_id in plan.task_ids:
        state = by_id.get(task_id)
        entries = () if state is None else state.integrations
        for entry in entries:
            if entry.integration_id != plan.integration_id:
                continue
            if (entry.candidate_commit, entry.integration_commit) != (candidates[task_id], commit):
                raise IntegrationError(RECORD_CONFLICT, f"task {task_id} records a different integration")
            present.add(task_id)
    return present


def _append_facts(plan: Plan, commit: str) -> list[str]:
    """Append only the missing facts, in order, so a crash leaves a repair-able suffix."""
    present = _present(plan, commit)
    missing = [task_id for task_id in plan.task_ids if task_id not in present]
    if not missing:
        return []
    candidates = plan.candidates
    try:
        with run_task_journal.transaction(plan.run_dir, workspace=plan.workspace, writing=True) as (run_dir, _events):
            for task_id in missing:
                run_task_journal.append(
                    run_dir,
                    workspace=plan.workspace,
                    event_type="task.integrated",
                    payload={
                        "task_id": task_id,
                        "integration_id": plan.integration_id,
                        "candidate_commit": candidates[task_id],
                        "integration_commit": commit,
                    },
                )
    except run_task_journal.TaskJournalError as exc:
        raise IntegrationError(JOURNAL_UNREADABLE, "integration facts could not be recorded") from exc
    return missing


def _verify_ref(plan: Plan, commit: str) -> None:
    """The recorded ref must still hold the recorded commit, or be created with CAS."""
    current = _ref_commit(plan)
    if current is not None:
        if current != commit:
            raise IntegrationError(REF_CONFLICT, f"{plan.ref} already exists at another commit")
        return
    cas = _git(["update-ref", plan.ref, commit, _ZERO_SHA], cwd=plan.workspace)
    if cas.code == 0:
        return
    if _ref_commit(plan) is not None:
        raise IntegrationError(REF_CONFLICT, f"{plan.ref} already exists at another commit")
    raise IntegrationError(GIT_FAILED, "integration ref could not be restored")


def _payload(plan: Plan, *, commit: str | None, status: str, check: bool) -> dict[str, Any]:
    return {
        "schema": CLI_SCHEMA,
        "run_id": plan.run_id,
        "request_id": plan.request_id,
        "check": check,
        "status": status,
        "base_sha": plan.base_sha,
        "integration_id": plan.integration_id,
        "ref": None if check else plan.ref,
        "integration_commit": commit,
        "task_ids": list(plan.task_ids),
        "candidate_commits": [
            {"task_id": task_id, "candidate_commit": candidate}
            for task_id, candidate in zip(plan.task_ids, plan.candidate_commits, strict=True)
        ],
        "refusal": None,
    }


def _ref_holds(plan: Plan, commit: str) -> bool:
    return _ref_commit(plan) == commit


def _write_record(plan: Plan, commit: str) -> None:
    """Record the integration before the ref is created.

    Writing the record first means a crash between the two leaves a recorded
    integration whose ref is still missing, which the record path repairs. The
    other order would leave a ref with no record, which only the deterministic
    commit and the compare-and-swap reconciliation can recognize.
    """
    try:
        localio.write_json_exclusive(plan.run_dir / record_name(plan.integration_id), _record(plan, commit))
    except FileExistsError:
        # A concurrent caller wrote the same record first.
        existing = _read_record(plan)
        if existing is None or _verify_record(plan, existing) != commit:
            raise IntegrationError(
                RECORD_CONFLICT, f"integration record for {plan.integration_id} does not match"
            ) from None
    except OSError as exc:
        raise IntegrationError(GIT_FAILED, "integration record could not be written") from exc
    try:
        run_journal._fsync_directory(plan.run_dir)
    except (OSError, run_journal.RunJournalError) as exc:
        raise IntegrationError(GIT_FAILED, "integration record could not be recorded") from exc


def _integrate(plan: Plan, *, check: bool) -> dict[str, Any]:
    record = _read_record(plan)
    if record is not None:
        commit = _verify_record(plan, record)
        if check:
            # A check never creates the ref or appends facts, even to finish a
            # recorded integration whose fact append crashed.
            complete = len(_present(plan, commit)) == len(plan.task_ids) and _ref_holds(plan, commit)
            return _payload(plan, commit=commit, status=STATUS_ALREADY if complete else STATUS_CHECKED, check=True)
        _verify_ref(plan, commit)
        appended = _append_facts(plan, commit)
        return _payload(plan, commit=commit, status=STATUS_CREATED if appended else STATUS_ALREADY, check=False)
    with _quarantine(plan.run_dir, plan) as quarantine:
        extra_env = _quarantine_env(plan)
        tree = _merge_all(plan, quarantine, extra_env)
        if check:
            return _payload(plan, commit=None, status=STATUS_CHECKED, check=True)
        commit = _final_commit(plan, quarantine, tree, extra_env)
        _require_present(plan, commit)
    existing = _ref_commit(plan)
    if existing is not None and existing != commit:
        # Read before the record so a foreign ref leaves no record behind.
        raise IntegrationError(REF_EXISTS, f"{plan.ref} already exists at {existing}")
    _write_record(plan, commit)
    if existing is None:
        _create_ref(plan, commit)
    _append_facts(plan, commit)
    return _payload(plan, commit=commit, status=STATUS_CREATED, check=False)


@contextlib.contextmanager
def _lock(workspace: Path, run_dir: Path) -> Iterator[None]:
    """Hold the caller workspace run lock for the whole command."""
    with contextlib.ExitStack() as stack:
        try:
            stack.enter_context(runguard.run_lock(workspace, run_dir=run_dir))
        except (runguard.RunLockError, OSError) as exc:
            raise IntegrationError(TARGET_BUSY, "another caller holds this workspace's run lock") from exc
        yield


def _workspace(run_dir: Path, workspace: Path | None) -> Path:
    """The caller workspace: an explicit path, or the run's own recorded lock workspace."""
    if workspace is not None:
        return Path(workspace).expanduser().resolve()
    meta = localio.read_json_dict(run_dir / "run.json") or {}
    resolved = runguard.resolve_run_lock_workspace(meta, run_dir)
    if resolved is None:
        raise IntegrationError(UNKNOWN_RUN, "caller workspace could not be resolved")
    return resolved


def integrate(
    run_dir: Path,
    task_ids: Sequence[str],
    *,
    workspace: Path | None = None,
    check: bool = False,
) -> dict[str, Any]:
    """Integrate the named tasks' candidates into a new local branch.

    Refuses with one closed code before writing anything, except for the run
    lock this command holds by design. ``check=True`` stops after the merges and
    writes neither a ref nor a record nor a fact.
    """
    try:
        run_dir = run_journal.normalize_run_dir(Path(run_dir))
    except (run_journal.RunJournalError, OSError, ValueError) as exc:
        raise IntegrationError(UNKNOWN_RUN, "run directory is not a run") from exc
    workspace = _workspace(run_dir, workspace)
    ordered = _ordered(task_ids)
    with _lock(workspace, run_dir):
        request = _request(run_dir)
        plan = _plan(run_dir, workspace, request, ordered)
        return _integrate(plan, check=check)


def refusal_payload(error: IntegrationError, *, run_id: str | None = None, check: bool = False) -> dict[str, Any]:
    """The closed refusal projection for ``--json``."""
    return {
        "schema": CLI_SCHEMA,
        "run_id": run_id,
        "check": check,
        "refusal": {
            "code": error.code,
            "detail": error.detail,
            "task_ids": list(error.task_ids),
            "paths": list(error.paths),
        },
    }


def render(payload: dict[str, Any]) -> str:
    """Human output: the result, and how to inspect or merge it yourself."""
    refusal = payload.get("refusal")
    if refusal is not None:
        return f"error: {refusal['code']}: {refusal['detail']}"
    lines = [
        f"run: {payload['run_id']}",
        f"base: {payload['base_sha']}",
        f"integration: {payload['integration_id']}",
        f"tasks ({len(payload['task_ids'])}): {', '.join(payload['task_ids'])}",
    ]
    for entry in payload["candidate_commits"]:
        lines.append(f"candidate {entry['task_id']}: {entry['candidate_commit']}")
    if payload["status"] == STATUS_CHECKED:
        if payload["integration_commit"] is None:
            lines.append("merges cleanly; --check wrote no ref, record or journal fact")
        else:
            lines.append("the integration is recorded but not complete; --check wrote nothing")
        return "\n".join(lines)
    ref = payload["ref"]
    if ref is not None:
        lines.insert(3, f"commit: {payload['integration_commit']}")
        lines.insert(3, f"ref: {ref}")
    if payload["status"] == STATUS_ALREADY:
        lines.append("already integrated; nothing was written")
    if ref is not None:
        lines.append(f"Inspect it with: git log {ref}")
        lines.append(f"Merge it yourself with: git merge {ref}")
        lines.append("Brigade did not merge, push or publish anything.")
    return "\n".join(lines)
