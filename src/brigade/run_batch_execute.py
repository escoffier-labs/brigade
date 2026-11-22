"""Explicit batches adapt the existing run orchestrator without model planning."""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import re
import sys
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from . import (
    agents,
    codex_appserver,
    fleet_client,
    inbox_lock,
    localio,
    proc,
    run_batch_artifacts,
    run_batch_launch,
    run_batch_workspace,
    run_control,
    run_journal,
    run_lifecycle,
    run_receipts,
    run_task_inbox,
    run_task_journal,
    run_tasks,
    runguard,
)
from .run_batch import BatchError
from .run_transport import TASK_CANCELLED_STATUS, Assignment, WorkerResult
from .roster import Roster, read_only_cloud_mutation_error


# Written once the batch is accepted into the lifecycle journal, so a deleted journal is detectable.
JOURNAL_SENTINEL = "batch-journal-enrolled.json"
_CANCEL_POLL_SECONDS = 0.5


@dataclass
class _TaskRuntime:
    lock: threading.Lock = field(default_factory=threading.Lock)
    handed_off: bool = False
    cancel_done: threading.Event | None = None
    attempt_id: str | None = None
    containment: str | None = None


def _reference(value: str | None) -> str | None:
    """Drop a provider value the journal would refuse, rather than lose the whole observation."""
    return value if run_task_journal.provider_reference(value) else None


def _read_only_enforcement_error(task_id: str, agent: Any, roster: Roster, *, read_only: bool) -> str | None:
    """Refuse a per-task read-only setting the seat cannot hard-enforce.

    The shared-writable layer rule lets a read-only task run beside a writer in
    one checkout, so a soft or prompt-only read-only mode is not enough. A
    read-only roster sandbox likewise cannot carry a writable task.
    """
    if read_only:
        enforcement = agents.read_only_enforcement(agent.cli or "", sandbox=roster.sandbox, transport=agent.transport)
        if enforcement != "hard":
            return (
                f"task {task_id} is read_only but seat {agent.name!r} enforces read-only as {enforcement!r} "
                f"under sandbox {roster.sandbox!r}; use isolation worktree, depends_on, or a hard read-only seat"
            )
    elif roster.sandbox == "read-only":
        return f"task {task_id} is writable but the roster sandbox is read-only"
    return None


def _refuse_externally_controlled(run_dir: Path) -> None:
    """A native T3 enrollment run has no generic launch authority."""
    from . import localio, runs_status

    meta = localio.read_json_dict(run_dir / "run.json")
    if meta is not None and runs_status.is_externally_controlled(meta):
        raise BatchError("native-enrolled-run", "run is externally controlled by a native T3 parent")


class BatchExecution:
    def __init__(self, plan: dict[str, Any], run_dir: Path, workspace: Path):
        self.plan = plan
        self.run_dir = run_dir
        self.workspace = workspace
        self.runtime = {task["task_id"]: _TaskRuntime() for task in plan["tasks"]}
        self.processes = proc.ProcessRegistry()
        self.turns: run_control.LiveTurnRegistry | None = None
        # The prepared handle and its journal version, captured at the launch point.
        self.prepared: dict[str, tuple[run_tasks.TaskHandle, str]] = {}
        # Per-task read-only is enforced by the existing worker read-only path,
        # so a mixed batch keeps one dispatcher and its dependency context.
        self.read_only = {t["task_id"]: bool(t.get("read_only", False)) for t in plan["tasks"]}
        # Tasks refused at the launch point keep their journal state untouched.
        self.refused: set[str] = set()
        # Declared prerequisites, and dependents never launched because one of
        # them was cancelled. Those stay queued and do not abort later waves.
        self.depends_on = {t["task_id"]: tuple(t.get("depends_on") or ()) for t in plan["tasks"]}
        self.prerequisite_cancelled: set[str] = set()
        # Per-task worktrees created and validated before any launch, by task ID.
        self.workspaces: dict[str, run_batch_workspace.Binding] = {}
        # Per-task launch refusals, as (code, reason), including every workspace
        # refusal. They are raised at the existing build_prompt fence, so nothing
        # launches for them, and reported in the CLI result.
        self.workspace_refusals: dict[str, tuple[str, str]] = {}
        # A whole-batch refusal raised during dispatch, so the CLI can report its code.
        self.refusal: tuple[str, str] | None = None
        self.writable_worktree = {
            t["task_id"]: t.get("isolation") == "worktree" and not t.get("read_only", False) for t in plan["tasks"]
        }

    def read_only_for(self, assignment: Assignment) -> bool:
        return self.read_only[str(assignment.task_id)]

    def cwd_for(self, assignment: Assignment) -> Path:
        """The directory one worker runs in: its task worktree, else the caller workspace."""
        binding = self.workspaces.get(str(assignment.task_id))
        return self.workspace if binding is None else binding.path

    def writable_worktree_for(self, assignment: Assignment) -> bool:
        """Only a writable worktree task owns a checkout Brigade created for it."""
        return self.writable_worktree.get(str(assignment.task_id), False)

    def _isolated(self) -> list[str]:
        return [t["task_id"] for t in self.plan["tasks"] if t.get("isolation") == "worktree"]

    def check_pinned_base(self) -> None:
        """Refuse the whole batch when the caller checkout no longer matches its pinned base."""
        from .fleet_t3_transport import SourceRevisionError, clean_source_revision

        expected = self.plan.get("base_sha")
        message = None
        try:
            current = clean_source_revision(self.workspace, ignore_prefixes=(".brigade/",))
        except SourceRevisionError as exc:
            message = f"worktree isolation needs the pinned base revision: {exc}"
        else:
            if current != expected:
                message = f"caller workspace source moved since acceptance: pinned {expected}, found {current}"
        if message is not None:
            self.refusal = ("source-changed", message)
            raise BatchError(*self.refusal)

    def _reuse_or_create(
        self, task_id: str, state: run_tasks.TaskResult | None, others: list[Path]
    ) -> run_batch_workspace.Binding:
        """Return the validated binding for one task, replaying a recorded one without repair."""
        base_sha = str(self.plan["base_sha"])
        run_id = self.run_dir.name
        path = run_batch_workspace.expected_path(self.workspace, run_id, task_id)
        branch = run_batch_workspace.branch_name(run_id, task_id)
        if state is not None and state.workspace is not None:
            # Crash replay: the path is recomputed from identities, never read back.
            try:
                info = path.lstat()
            except OSError as exc:
                raise run_batch_workspace.WorkspaceError(run_batch_workspace.WORKSPACE_INVALID, "missing") from exc
            binding = run_batch_workspace.Binding(
                task_id=task_id,
                run_id=run_id,
                base_sha=base_sha,
                branch=branch,
                path=path,
                dev=info.st_dev,
                ino=info.st_ino,
            )
            fact = run_batch_workspace.binding_fact(binding)
            recorded = run_tasks.TaskWorkspaceBinding(
                base_sha=fact["base_sha"],
                branch_digest=fact["branch_digest"],
                path_digest=fact["path_digest"],
                dev_ino_digest=fact["dev_ino_digest"],
            )
            if recorded != state.workspace:
                raise run_batch_workspace.WorkspaceError(run_batch_workspace.WORKSPACE_INVALID, "binding-digest")
            run_batch_workspace.validate(binding, self.workspace, pristine=True)
            return binding
        run_batch_workspace.check_disjoint(path, self.workspace, others)
        reservation = run_batch_workspace.reserve(path)
        binding = run_batch_workspace.create(
            self.workspace,
            base_sha,
            reservation,
            branch,
            task_id=task_id,
            run_id=run_id,
        )
        fact = run_batch_workspace.binding_fact(binding)
        run_tasks.record_workspace_binding(
            self.run_dir,
            workspace=self.workspace,
            task_id=task_id,
            binding=run_tasks.TaskWorkspaceBinding(
                base_sha=fact["base_sha"],
                branch_digest=fact["branch_digest"],
                path_digest=fact["path_digest"],
                dev_ino_digest=fact["dev_ino_digest"],
            ),
        )
        return binding

    def prepare_workspaces(self, stack: contextlib.ExitStack) -> None:
        """Reserve, create, validate and bind every isolated task before any launch.

        The base is checked once for the whole batch and refuses it outright. A
        single task's workspace refusal is retained and refuses only that task at
        the launch fence. Each checkout holds its run lock from creation through
        worker exit, so ``brigade runs prune-worktrees`` skips a live task
        worktree. Nothing is ever deleted, here or later.
        """
        isolated = self._isolated()
        if not isolated:
            return
        self.check_pinned_base()
        states = {r.handle.spec.task_id: r for r in run_tasks.read_tasks(self.run_dir)}
        paths = [run_batch_workspace.expected_path(self.workspace, self.run_dir.name, task_id) for task_id in isolated]
        for index, task_id in enumerate(isolated):
            state = states.get(task_id)
            if state is not None and (
                state.cancel_requested or state.handle.attempt_id is not None or state.status != "queued"
            ):
                # The existing relaunch fence owns this task. Never prepare a
                # checkout for it, including one cancelled before launch.
                continue
            others = [path for position, path in enumerate(paths) if position != index]
            try:
                binding = self._reuse_or_create(task_id, state, others)
            except run_batch_workspace.WorkspaceError as exc:
                self.workspace_refusals[task_id] = (exc.code, exc.reason)
                continue
            except run_tasks.TaskJournalError:
                self.workspace_refusals[task_id] = (run_batch_workspace.WORKSPACE_INVALID, "binding")
                continue
            try:
                # Claim mode takes over a dead owner's lock only when that owner's
                # recorded run_dir is this batch run, re-verified inside the atomic
                # claim (run_lock_state(path, self.run_dir) == "stale"). A live
                # owner, or a dead owner from a different run, still refuses. It
                # writes no stale-lock-recovery receipt and never terminalizes the
                # batch run.json, so a SIGKILLed dispatch replays instead of
                # refusing the task forever.
                stack.enter_context(runguard.run_lock(binding.path, run_dir=self.run_dir, stale_action="claim"))
            except (runguard.RunLockError, OSError):
                # A checkout another process already owns is refused, never adopted.
                self.workspace_refusals[task_id] = (run_batch_workspace.WORKSPACE_UNAVAILABLE, "run-lock")
                continue
            self.workspaces[task_id] = binding

    def _validate_at_launch(self, task_id: str) -> None:
        """Re-validate the task checkout once more immediately before its launch."""
        binding = self.workspaces.get(task_id)
        if binding is None:
            return
        try:
            run_batch_workspace.validate(binding, self.workspace, pristine=True)
        except run_batch_workspace.WorkspaceError as exc:
            self.workspace_refusals[task_id] = (exc.code, exc.reason)
            raise BatchError(exc.code, f"task {task_id} workspace {exc.code}: {exc.reason}") from exc

    def current(self, task_id: str) -> run_tasks.TaskResult:
        found = next((r for r in run_tasks.read_tasks(self.run_dir) if r.handle.spec.task_id == task_id), None)
        if found is None:
            raise BatchError("launch-refused", f"task {task_id} is missing from the lifecycle journal")
        return found

    def _drain_inbox(self) -> bool:
        """Journal pending cancel intent; the batch owns the run lock here.

        A refused inbox is reported and skipped: a malformed request must never
        turn into a failed dispatch, and it names no task the batch may launch.
        """
        try:
            return bool(run_task_inbox.drain(self.run_dir, self.workspace))
        except run_task_inbox.TaskInboxError as exc:
            print(f"warning: task cancel inbox refused: {exc.code}: {exc.detail}", file=sys.stderr)
            return False

    def _watch_cancels(self, stopped: threading.Event, *, terminate: bool = True) -> None:
        while not stopped.wait(_CANCEL_POLL_SECONDS):
            self._drain_inbox()
            if not terminate:
                # A remote route has no exact per-task cancel, so a running
                # task keeps its real result and the intent shows as too late.
                continue
            try:
                states = run_tasks.read_tasks(self.run_dir)
            except run_tasks.TaskJournalError as exc:
                print(f"warning: task cancel journal refused: {exc}", file=sys.stderr)
                continue
            # Both transactions have exited before we acquire runtime locks
            # or make any potentially blocking transport call.
            for state in states:
                if stopped.is_set():
                    return
                if not state.cancel_requested or state.launch_claim is None or state.result_id is not None:
                    continue
                task_id, attempt_id = state.handle.spec.task_id, state.handle.attempt_id
                runtime = self.runtime.get(task_id)
                if runtime is None or attempt_id is None:
                    continue
                with runtime.lock:
                    if runtime.handed_off or runtime.cancel_done is not None:
                        continue
                    done = runtime.cancel_done = threading.Event()
                    runtime.attempt_id = attempt_id
                try:
                    self.processes.cancel_task(task_id, attempt_id)
                    if self.turns is not None:
                        self.turns.interrupt(task_id=task_id, attempt_id=attempt_id, latch_task=True)
                finally:
                    done.set()

    def _cancel_containment(self, task_id: str, attempt_id: str, result: WorkerResult) -> str:
        outcomes = {outcome.result for outcome in self.processes.task_cancellation_outcomes(task_id, attempt_id)}
        if self.turns is not None:
            turn = self.turns.task_interrupt_outcome(task_id, attempt_id)
            if turn is not None:
                outcomes.add("interrupted" if turn == "still_active" and result.status == "interrupted" else turn)
        return next((s for s in ("error", "still_active", "interrupted") if s in outcomes), "unknown")

    def _record_cancelled(self, task_id: str) -> None:
        """Record the one terminal cancellation observation for a gated task.

        The attempt is the deterministic one the launch would have used, so the
        cancellation names the same assignment. Replay is a no-op once the
        terminal result exists.
        """
        state = self.current(task_id)
        if state.result_id is not None or not state.cancel_requested:
            return
        handle = run_tasks.prepare_attempt(self.run_dir, workspace=self.workspace, task_id=task_id)
        run_tasks.observe_task(
            self.run_dir,
            workspace=self.workspace,
            handle=handle,
            observation=run_tasks.TaskObservation(status="cancelled", containment="not-launched"),
        )

    def launch_recorder(self, agent: Any, assignment: Assignment | None, record_requested: Any) -> Any:
        """The claim-then-requested recorder the orchestrator runs inside the budget reservation."""
        task_id = assignment.task_id if assignment is not None else None
        # The owner drains before each claim, so an intent that arrived after
        # this task's attempt was prepared is journaled before the claim folds it.
        self._drain_inbox()
        return run_batch_launch.launch_recorder(
            run_dir=self.run_dir,
            workspace=self.workspace,
            assignment=assignment,
            seat=agent.name,
            prepared=self.prepared.get(task_id) if task_id is not None else None,
            record_requested=lambda attempt: record_requested("run.dispatch.requested", agent, attempt),
            on_refused=self.refused.add,
        )

    def refuse_unbudgeted(self, assignment: Assignment | None) -> Any:
        return run_batch_launch.refuse_unbudgeted(assignment, self.refused.add)

    def assignments(self, roster: Roster) -> list[Assignment]:
        from .aboyeur import artifacts

        for task in self.plan["tasks"]:
            read_only = self.read_only[task["task_id"]]
            error = artifacts._direct_worker_error(task["seat"], roster, read_only=read_only)
            agent = roster.agents.get(task["seat"])
            if error is None and read_only and agent is not None:
                # The same refusal a read-only run applies to a mutating cloud seat.
                error = read_only_cloud_mutation_error(agent)
            if error is None and agent is not None:
                error = _read_only_enforcement_error(task["task_id"], agent, roster, read_only=read_only)
            if error is not None:
                raise BatchError("invalid-seat", error)
        specs = tuple(
            run_tasks.TaskSpec(t["task_id"], t["seat"], hashlib.sha256(t["text"].encode("utf-8")).hexdigest())
            for t in self.plan["tasks"]
        )
        accepted = run_tasks.accept_task_batch(
            self.run_dir, workspace=self.workspace, request_id=self.plan["request_id"], specs=specs
        )
        # Records that the journal existed, so deleting it later cannot re-enroll the request.
        localio.write_json(self.run_dir / JOURNAL_SENTINEL, {"request_id": self.plan["request_id"]})
        run_journal._fsync_directory(self.run_dir)
        attempts = {m.handle.spec.task_id: run_tasks.planned_attempt_id(m.handle) for m in accepted.members}
        layers = {task_id: index for index, layer in enumerate(self.plan["layers"]) for task_id in layer}
        # Batch assignments intentionally omit run-level selected_skill_ids; the caller supplies each task.
        return [
            Assignment(
                worker=t["seat"],
                task=f"Batch task_id: {t['task_id']}\n\n{t['text']}",
                stage=layers[t["task_id"]],
                task_id=t["task_id"],
                attempt_id=attempts[t["task_id"]],
            )
            for t in self.plan["tasks"]
        ]

    def observe(self, result: WorkerResult) -> WorkerResult | None:
        if result.task_id is None:
            return None
        task_id = str(result.task_id)
        runtime = self.runtime[task_id]
        with runtime.lock:
            runtime.handed_off = True
            done, attempt_id = runtime.cancel_done, runtime.attempt_id
        containment = None
        if done is not None and attempt_id is not None:
            done.wait()
            if runtime.containment is None:
                runtime.containment = self._cancel_containment(task_id, attempt_id, result)
            containment = runtime.containment
            result = replace(result, ok=False, status=TASK_CANCELLED_STATUS)
        self._publish_result(result, task_id, containment)
        return result

    def _observe_for_dispatch(self, result: WorkerResult) -> WorkerResult | None:
        try:
            return self.observe(result)
        except Exception:
            task_id = result.task_id
            if task_id is None or self.runtime[task_id].containment is None:
                raise
            # Keep the scheduling decision on a write failure. Finish retries
            # observation and reports any error that persists.
            print(f"warning: task {task_id} cancellation observation failed", file=sys.stderr)
            return replace(result, ok=False, status=TASK_CANCELLED_STATUS)

    def _publish_result(self, result: WorkerResult, task_id: str, cancel_containment: str | None) -> None:
        if task_id in self.prerequisite_cancelled:
            # Never launched and never journaled: the task stays queued.
            return
        if task_id in self.refused or result.status == "refused":
            # A refusal is also where a cancel gate lands: record the terminal
            # cancellation once, then leave the result untouched.
            self._record_cancelled(task_id)
            return
        if self.current(task_id).result_id is not None:
            return
        # Skipped tasks retain queued state. They were never launched.
        if result.status == "skipped":
            return
        handle = run_tasks.prepare_attempt(self.run_dir, workspace=self.workspace, task_id=task_id)
        path = self.run_dir / f"batch-result-{hashlib.sha256(task_id.encode()).hexdigest()}.json"
        # Persist the sanitized receipt projection, never raw captures or diagnostics.
        localio.write_json(path, run_receipts.worker_payload_one(result))
        run_journal._fsync_directory(self.run_dir)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        artifact_kind, artifact_digest = "receipt", digest
        binding = self.workspaces.get(task_id)
        if binding is not None:
            # Collection runs inside the per-task run lock the batch holds from
            # creation through worker exit, so nothing else can be writing the
            # checkout while its candidate is committed and patched.
            artifact = run_batch_artifacts.collect(
                binding,
                self.workspace,
                self.run_dir,
                execution=run_batch_artifacts.execution_state(result),
                attempt_id=handle.attempt_id,
            )
            try:
                _, sidecar_digest = run_batch_artifacts.write_sidecar(self.run_dir, artifact)
            except OSError:
                # The terminal observation still stands on the receipt alone.
                artifact_digest = digest
            else:
                run_journal._fsync_directory(self.run_dir)
                if artifact.get("candidate_commit") is not None:
                    # A candidate exists: the artifact is the commit, and only
                    # then does the observation name the artifact sidecar.
                    artifact_kind, artifact_digest = "commit", sidecar_digest
        # An intent arriving after handoff can still be journaled before
        # publication, including while worktree artifact collection is running.
        self._drain_inbox()
        run_tasks.observe_task(
            self.run_dir,
            workspace=self.workspace,
            handle=handle,
            observation=run_tasks.TaskObservation(
                status="cancelled" if cancel_containment is not None else ("completed" if result.ok else "failed"),
                artifact_kind=artifact_kind,
                artifact_digest=artifact_digest,
                containment=cancel_containment or "quiescent",
                provider_task_id=_reference(result.provider_task_id),
                original_run_id=_reference(result.original_run_id),
                provider_result_id=_reference(result.provider_result_id),
            ),
        )

    def dispatch(self, assignments: list[Assignment], roster: Roster, **kwargs: Any) -> list[WorkerResult]:
        from .aboyeur import planning

        _refuse_externally_controlled(self.run_dir)
        # A remote route hands the task text to another machine, which has no
        # access to a checkout reserved on this one. Refuse before any launch.
        if kwargs.get("remote_transport") is not None and self._isolated():
            self.refusal = (
                "isolation-remote-unsupported",
                "worktree isolation cannot be combined with Fleet Hub remote routing; run the batch on one machine",
            )
            raise BatchError(*self.refusal)

        # Reuse the wave scheduler and its fail-fast behavior. No wave exceeds
        # the effective roster limit, even before transport admission is applied.
        waves: list[Assignment] = []
        stage = 0
        for layer in sorted({a.stage for a in assignments}):
            members = [a for a in assignments if a.stage == layer]
            for offset in range(0, len(members), roster.max_workers):
                waves.extend(replace(a, stage=stage) for a in members[offset : offset + roster.max_workers])
                stage += 1
        original_prompt = kwargs["build_prompt"]

        def build_prompt(agent, assignment, **prompt_kwargs):
            task_id = str(assignment.task_id)
            # Launch-point fence, independent of the request marker: a task the
            # journal shows as attempted is never invoked again. The request and
            # run locks serialize dispatchers, so this read cannot race a launch.
            try:
                refusal = self.workspace_refusals.get(task_id)
                if refusal is not None:
                    raise BatchError(refusal[0], f"task {task_id} workspace {refusal[0]}: {refusal[1]}")
                # An isolated task's checkout is re-validated immediately before
                # its launch, so a file added since creation refuses that task.
                self._validate_at_launch(task_id)
                blocked = sorted(
                    dep
                    for dep in self.depends_on.get(task_id, ())
                    if dep in self.prerequisite_cancelled or self._cancelled(dep)
                )
                if blocked:
                    self.prerequisite_cancelled.add(task_id)
                    raise BatchError("prerequisite-cancelled", f"task {task_id} depends on cancelled task {blocked[0]}")
                current = self.current(task_id)
                if current.cancel_requested:
                    # A journaled intent owns this launch: record the terminal
                    # cancellation on the prepared attempt and never invoke.
                    self._record_cancelled(task_id)
                    raise BatchError("cancelled-before-launch", f"task {task_id} was cancelled before it launched")
                if current.handle.attempt_id is not None or current.status != "queued":
                    raise BatchError(
                        "launch-refused", f"task {task_id} already has a recorded attempt; refusing relaunch"
                    )
                # Only the attempt is prepared here. The launch claim that follows in the budget
                # reservation needs this as the last task event and moves the task to unknown.
                handle = run_tasks.prepare_attempt(self.run_dir, workspace=self.workspace, task_id=task_id)
                self.prepared[task_id] = (handle, self.current(task_id).version or "")
            except BatchError as exc:
                # The wave executor folds this into a WorkerResult, so keep the
                # closed code here for the CLI result and its human report.
                self.refused.add(task_id)
                self.workspace_refusals.setdefault(task_id, (exc.code, str(exc)))
                raise
            except run_tasks.TaskJournalError:
                self.refused.add(task_id)
                raise
            return original_prompt(agent, assignment, **prompt_kwargs)

        kwargs["build_prompt"] = build_prompt
        kwargs["on_result"] = self._observe_for_dispatch
        kwargs["allow_reinvocation"] = False
        # Layers are ordered by stage. The DAG scheduler ignores stage, so it is never used here.
        kwargs["scheduler"] = "waves"
        kwargs["read_only_for"] = self.read_only_for
        kwargs["cwd_for"] = self.cwd_for
        kwargs["writable_worktree_for"] = self.writable_worktree_for
        self.processes = kwargs.get("process_registry") or self.processes
        self.turns = kwargs.get("control_registry")
        if self.turns is None and kwargs.get("appserver") is not None:
            self.turns = run_control.LiveTurnRegistry()
        kwargs["process_registry"] = self.processes
        kwargs["control_registry"] = self.turns
        with contextlib.ExitStack() as stack:
            # Resume/replay: an intent that survived a crash is journaled, and
            # the task is recorded as cancelled before any checkout or launch.
            self._drain_inbox()
            self.prepare_workspaces(stack)
            stopped = threading.Event()
            # Fleet remote dispatch cannot cancel one exact remote request, so a
            # remote-routed batch never claims live termination: the watcher
            # only journals intent, and `runs tasks cancel` reports
            # live-termination-unavailable because no live watcher is advertised.
            terminate = kwargs.get("remote_transport") is None
            ctx = contextvars.copy_context()
            watcher = threading.Thread(
                target=ctx.run,
                args=(self._watch_cancels, stopped),
                kwargs={"terminate": terminate},
                name="batch-task-cancel",
                daemon=True,
            )
            advertise = (
                run_task_inbox.live_watcher(self.run_dir, self.workspace) if terminate else contextlib.nullcontext()
            )
            with advertise:
                watcher.start()
                try:
                    return planning.dispatch(waves, roster, **kwargs)
                finally:
                    stopped.set()
                    watcher.join(timeout=self.processes.cancellation_timeout + codex_appserver._INTERRUPT_GRACE)
                    if watcher.is_alive():
                        raise runguard.RetainRunLockError("batch cancellation watcher did not stop")
                    self._drain_inbox()

    def resolve_model_policy(self, roster: Roster, **kwargs: Any) -> Any:
        from .aboyeur import orchestrator

        snapshot = kwargs.get("snapshot")
        if snapshot is None:
            snapshot = fleet_client.load_model_policy_snapshot()
        # Resolve each required seat against the same snapshot and source roster.
        # Accumulate Hub-derived seats before the final pass, which receipts all
        # admissions together for the runtime admission coordinator.
        for seat in sorted({t["seat"] for t in self.plan["tasks"]}):
            resolution = orchestrator.resolve_fleet_model_policy(roster, worker=seat, snapshot=snapshot)
            if resolution.error is not None:
                return resolution
            roster = replace(roster, agents={**roster.agents, **resolution.roster.agents})
        resolution = orchestrator.resolve_fleet_model_policy(roster, worker=kwargs["worker"], snapshot=snapshot)
        # Explicit seats have no health or invalid-final fallback. Failure must
        # remain attributed to the seat the caller selected.
        seats = {t["seat"] for t in self.plan["tasks"]}
        return replace(
            resolution,
            roster=replace(
                resolution.roster,
                agents={
                    name: replace(agent, fallback=(), invalid_final_fallback=None) if name in seats else agent
                    for name, agent in resolution.roster.agents.items()
                },
            ),
        )

    def resolve_health_routing(self, roster: Roster, results: Any, **kwargs: Any) -> Any:
        from .aboyeur import orchestrator

        for seat in sorted({t["seat"] for t in self.plan["tasks"]}):
            routing = orchestrator.resolve_orchestrator_health_routing(
                roster, results, pinned_seats=frozenset({seat}), worker_seat=seat
            )
            if routing.abort_detail is not None:
                return routing
        return orchestrator.OrchestratorHealthRoutingDecision(roster=roster)

    def _cancelled(self, task_id: str) -> bool:
        """True when this task's terminal state is the recorded cancellation."""
        try:
            state = self.current(task_id)
        except (BatchError, run_tasks.TaskJournalError):
            return False
        return state.status == "cancelled" and state.cancel_requested

    def finish(self, results: list[WorkerResult]) -> agents.AgentResult:
        failures: list[str] = []
        lines = []
        for result in results:
            try:
                self.observe(result)
            except Exception as exc:
                # Dispatch guards on_result failures, so finish is the last
                # chance to record that a result file or journal observation
                # could not be written. Keep it inside the batch summary path
                # instead of escaping as an unexpected run failure.
                failures.append(f"task {result.task_id} observation failed: {exc}")
            if self._cancelled(str(result.task_id)):
                # A recorded cancellation is a decided outcome, not a failure;
                # the sibling results keep their own status.
                lines.append(f"{result.task_id}: cancelled")
                continue
            if str(result.task_id) in self.prerequisite_cancelled:
                # Never launched because a prerequisite was cancelled; it stays queued.
                lines.append(f"{result.task_id}: prerequisite-cancelled")
                continue
            lines.append(f"{result.task_id}: {'completed' if result.ok else result.status or 'failed'}")
        # Use the planned-run finish path with no synthesis invocation. It
        # records incomplete workers and retains every result and artifact.
        ok = (
            all(
                r.ok or self._cancelled(str(r.task_id)) or str(r.task_id) in self.prerequisite_cancelled
                for r in results
            )
            and not failures
        )
        detail = "; ".join(failures)
        if detail:
            lines.append(detail)
        return agents.AgentResult(
            text="\n".join(lines),
            ok=ok,
            detail=detail,
            failure_phase="result-processing" if failures else None,
            failure_kind="observation-write-failed" if failures else None,
        )


def stored_result(
    plan: dict[str, Any], run_dir: Path, refusals: dict[str, tuple[str, str]] | None = None
) -> dict[str, Any]:
    tasks = {}
    refusals = refusals or {}
    journal_path = run_lifecycle._journal_path(run_dir)
    # A crash before journal enrollment is still an accepted request and must
    # never be interpreted as permission to launch a replacement run.
    states = {}
    if journal_path.exists():
        try:
            states = {
                r.handle.spec.task_id: r
                for r in run_tasks.read_task_batch(run_dir, request_id=plan["request_id"]).members
            }
        except run_tasks.TaskJournalError:
            pass
    for task in plan["tasks"]:
        task_id = task["task_id"]
        state = states.get(task_id)
        path = run_dir / f"batch-result-{hashlib.sha256(task_id.encode()).hexdigest()}.json"
        refusal = refusals.get(task_id)
        worktree = task.get("isolation") == "worktree"
        artifact = run_batch_artifacts.summary(run_batch_artifacts.read(run_dir, task_id)) if worktree else None
        tasks[task_id] = {
            "status": state.status if state else "unknown",
            "not_run": bool(state and state.status == "queued"),
            "result_id": state.result_id if state else None,
            "result_ref": str(path.relative_to(run_dir)) if path.is_file() else None,
            "workspace": "worktree" if worktree else "shared",
            "artifact": artifact,
            "refusal": {"code": refusal[0], "reason": refusal[1]} if refusal is not None else None,
        }
    return {
        "schema": "brigade.run_batch_result.v1",
        "run_id": run_dir.name,
        "request_id": plan["request_id"],
        "run_status": (localio.read_json_dict(run_dir / "run.json") or {}).get("status", "unknown"),
        "tasks": tasks,
    }


def retryable_before_launch(run_dir: Path) -> bool:
    """Fail closed on damaged journals or any evidence of a possible launch."""
    # A result file is written only after a worker returns, so its presence is
    # evidence of a possible launch even when the lifecycle journal is gone.
    # A preflight refusal writes none, so its retry stays allowed.
    if any(run_dir.glob("batch-result-*.json")):
        return False
    if not run_lifecycle._journal_path(run_dir).exists():
        return True
    try:
        return all(r.handle.attempt_id is None and r.status == "queued" for r in run_tasks.read_tasks(run_dir))
    except run_tasks.TaskJournalError:
        return False


def journal_lost(run_dir: Path) -> bool:
    """True when a request that was accepted has lost or rewound its journal.

    Without a journal ``retryable_before_launch`` reads as "nothing launched", so
    deleting it would re-enroll every task. Acceptance leaves durable evidence
    outside the journal (the sentinel, and batch results once a worker returns),
    and any of it means the journal must still hold the accepted batch. A request
    that never reached acceptance has none of it and keeps its preflight retry.
    A journal rewound to a valid prefix that still contains the accepted batch
    is not detectable here: at-most-once assumes the journal is append-only.
    """
    accepted = (run_dir / JOURNAL_SENTINEL).exists() or any(run_dir.glob("batch-result-*.json"))
    if not run_lifecycle._journal_path(run_dir).exists():
        return accepted
    try:
        tasks = run_tasks.read_tasks(run_dir)
    except run_tasks.TaskJournalError:
        return True
    return accepted and not tasks


@contextlib.contextmanager
def request_lock(run_dir: Path):
    try:
        with inbox_lock.held_file_lock(run_dir / "batch-dispatch.lock", deadline_seconds=1.0):
            yield
    except inbox_lock.InboxLockTimeout as exc:
        raise BatchError("request-busy", "another caller is dispatching this batch request") from exc


def execute_cli(args: Any, plan: dict[str, Any]) -> int:
    from . import cli
    from .cli.run import dispatch

    if plan["synthesis"] is not None:
        raise BatchError("synthesis-not-implemented", "batch synthesis execution is not implemented")
    workspace = (args.cwd or Path.cwd()).expanduser().resolve()
    run_dir = (
        runguard.lock_path(workspace).parent
        / "runs"
        / ("batch-" + hashlib.sha256(plan["request_id"].encode()).hexdigest())
    )
    _refuse_externally_controlled(run_dir)
    try:
        run_dir.mkdir(parents=True, exist_ok=False)
        run_journal._fsync_directory(run_dir.parent)
    except FileExistsError:
        pass
    dispatch_rc = None
    execution: BatchExecution | None = None
    # Serialize retry eligibility with dispatch, including preflight. The
    # workspace run lock remains the authority for executing worker processes.
    with request_lock(run_dir):
        marker_path = run_dir / "batch-request.json"
        marker = localio.read_json_dict(marker_path)
        if marker_path.exists() and (
            marker is None or re.fullmatch(r"[0-9a-f]{64}", str(marker.get("fingerprint"))) is None
        ):
            raise BatchError("request-unknown", "request reservation has no readable fingerprint; refusing launch")
        if marker is not None and marker.get("fingerprint") != plan["fingerprint"]:
            raise BatchError("request-conflict", "request_id already belongs to a different batch")
        # Checked whatever the marker says: deleting the marker too must not
        # turn a lost journal back into a fresh request.
        if journal_lost(run_dir):
            raise BatchError(
                "request-unknown",
                "request journal is missing or unreadable; refusing launch. Use a new request_id",
            )
        # The journal, not the marker, records launches. Read it even when the
        # marker is missing, so a removed marker cannot authorize a relaunch.
        if not retryable_before_launch(run_dir):
            if marker is None:
                raise BatchError(
                    "request-unknown",
                    "journal shows a possible launch but the request marker is missing; refusing launch",
                )
        else:
            localio.write_json(marker_path, plan)
            run_journal._fsync_directory(run_dir)
            execution = BatchExecution(plan, run_dir, workspace)
            # The base is checked once before the run starts, so a caller that
            # moved HEAD is refused with its own code and no run artifacts.
            if plan.get("base_sha") is not None:
                execution.check_pinned_base()
            argv = [
                "run",
                f"Explicit batch {plan['request_id']}",
                "--worker",
                plan["tasks"][0]["seat"],
                "--cwd",
                str(workspace),
                "--output-dir",
                str(run_dir),
                "--no-route",
            ]
            # The run-level flag stays the single truth for an all-read-only
            # batch. A mixed batch relies on the per-task read-only resolver.
            if all(task.get("read_only", False) for task in plan["tasks"]):
                argv.append("--read-only")
            if args.roster is not None:
                argv.extend(["--roster", str(args.roster)])
            normal = cli._build_parser_for(argv).parse_args(argv)
            normal.batch_execution = execution
            try:
                with contextlib.redirect_stdout(sys.stderr):
                    dispatch_rc = dispatch(normal)
            except (KeyboardInterrupt, TimeoutError):
                dispatch_rc = 1
            # A refusal raised inside dispatch still belongs to the request, so
            # report its closed code instead of a generic run failure.
            if execution.refusal is not None:
                raise BatchError(*execution.refusal)
    result = stored_result(plan, run_dir, execution.workspace_refusals if execution is not None else None)
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        print(f"run {result['run_id']}")
        for task_id, task in result["tasks"].items():
            print(f"{task_id}: {task['status']} {task['result_ref'] or ''}".rstrip())
            if task["refusal"] is not None:
                print(f"task {task_id}: {task['refusal']['code']} ({task['refusal']['reason']})", file=sys.stderr)
    if dispatch_rc:
        if any(t["status"] == "failed" for t in result["tasks"].values()):
            return 1
        return 1 if dispatch_rc in {3, 130} else dispatch_rc
    return (
        0 if result["run_status"] == "ok" and all(t["status"] == "completed" for t in result["tasks"].values()) else 1
    )
