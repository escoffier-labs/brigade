"""Explicit batches adapt the existing run orchestrator without model planning."""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from . import agents, fleet_client, inbox_lock, localio, run_receipts, run_tasks, run_lifecycle, run_journal, runguard
from .run_batch import BatchError
from .run_transport import Assignment, WorkerResult
from .roster import Roster, read_only_cloud_mutation_error


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
        self.by_prompt = {f"Batch task_id: {t['task_id']}\n\n{t['text']}": t for t in plan["tasks"]}
        # Per-task read-only is enforced by the existing worker read-only path,
        # so a mixed batch keeps one dispatcher and its dependency context.
        self.read_only = {t["task_id"]: bool(t.get("read_only", False)) for t in plan["tasks"]}
        # Tasks refused at the launch point keep their journal state untouched.
        self.refused: set[str] = set()

    def read_only_for(self, assignment: Assignment) -> bool:
        return self.read_only[self.by_prompt[assignment.task]["task_id"]]

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
        run_tasks.accept_task_batch(
            self.run_dir, workspace=self.workspace, request_id=self.plan["request_id"], specs=specs
        )
        layers = {task_id: index for index, layer in enumerate(self.plan["layers"]) for task_id in layer}
        # Batch assignments intentionally omit run-level selected_skill_ids; the caller supplies each task.
        return [
            Assignment(worker=t["seat"], task=prompt, stage=layers[t["task_id"]])
            for prompt, t in self.by_prompt.items()
        ]

    def observe(self, result: WorkerResult) -> None:
        task = self.by_prompt[result.task]
        if task["task_id"] in self.refused:
            return
        current = next(r for r in run_tasks.read_tasks(self.run_dir) if r.handle.spec.task_id == task["task_id"])
        if current.result_id is not None:
            return
        # Skipped tasks retain queued state. They were never launched.
        if result.status == "skipped":
            return
        handle = run_tasks.prepare_attempt(self.run_dir, workspace=self.workspace, task_id=task["task_id"])
        path = self.run_dir / f"batch-result-{hashlib.sha256(task['task_id'].encode()).hexdigest()}.json"
        # Persist the sanitized receipt projection, never raw captures or diagnostics.
        localio.write_json(path, run_receipts.worker_payload_one(result))
        run_journal._fsync_directory(self.run_dir)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        run_tasks.observe_task(
            self.run_dir,
            workspace=self.workspace,
            handle=handle,
            observation=run_tasks.TaskObservation(
                status="completed" if result.ok else "failed",
                artifact_kind="receipt",
                artifact_digest=digest,
                containment="quiescent",
            ),
        )

    def dispatch(self, assignments: list[Assignment], roster: Roster, **kwargs: Any) -> list[WorkerResult]:
        from .aboyeur import planning

        _refuse_externally_controlled(self.run_dir)

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
            task_id = self.by_prompt[assignment.task]["task_id"]
            # Launch-point fence, independent of the request marker: a task the
            # journal shows as attempted is never invoked again. The request and
            # run locks serialize dispatchers, so this read cannot race a launch.
            current = next(r for r in run_tasks.read_tasks(self.run_dir) if r.handle.spec.task_id == task_id)
            if current.handle.attempt_id is not None or current.status != "queued":
                self.refused.add(task_id)
                raise BatchError("launch-refused", f"task {task_id} already has a recorded attempt; refusing relaunch")
            handle = run_tasks.prepare_attempt(self.run_dir, workspace=self.workspace, task_id=task_id)
            run_tasks.observe_task(
                self.run_dir,
                workspace=self.workspace,
                handle=handle,
                observation=run_tasks.TaskObservation(status="unknown"),
            )
            return original_prompt(agent, assignment, **prompt_kwargs)

        kwargs["build_prompt"] = build_prompt
        kwargs["on_result"] = self.observe
        kwargs["allow_reinvocation"] = False
        # Layers are ordered by stage. The DAG scheduler ignores stage, so it is never used here.
        kwargs["scheduler"] = "waves"
        kwargs["read_only_for"] = self.read_only_for
        return planning.dispatch(waves, roster, **kwargs)

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

    def finish(self, results: list[WorkerResult]) -> agents.AgentResult:
        for result in results:
            self.observe(result)
        # Use the planned-run finish path with no synthesis invocation. It
        # records incomplete workers and retains every result and artifact.
        return agents.AgentResult(
            text="\n".join(
                f"{self.by_prompt[r.task]['task_id']}: {'completed' if r.ok else r.status or 'failed'}" for r in results
            ),
            ok=all(r.ok for r in results),
        )


def stored_result(plan: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    tasks = {}
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
        tasks[task_id] = {
            "status": state.status if state else "unknown",
            "not_run": bool(state and state.status == "queued"),
            "result_id": state.result_id if state else None,
            "result_ref": str(path.relative_to(run_dir)) if path.is_file() else None,
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
    if not run_lifecycle._journal_path(run_dir).exists():
        return True
    try:
        return all(r.handle.attempt_id is None and r.status == "queued" for r in run_tasks.read_tasks(run_dir))
    except run_tasks.TaskJournalError:
        return False


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
            if any(task.get("isolation", "shared") == "worktree" for task in plan["tasks"]):
                # The request is accepted with its pinned base and recorded
                # isolation, then refused before any launch. Creating the
                # worktrees lands in a following slice.
                raise BatchError(
                    "isolation-worktree-not-implemented",
                    "batch worktree isolation execution is not implemented yet",
                )
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
            normal.batch_execution = BatchExecution(plan, run_dir, workspace)
            try:
                with contextlib.redirect_stdout(sys.stderr):
                    dispatch_rc = dispatch(normal)
            except (KeyboardInterrupt, TimeoutError):
                dispatch_rc = 1
    result = stored_result(plan, run_dir)
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        print(f"run {result['run_id']}")
        for task_id, task in result["tasks"].items():
            print(f"{task_id}: {task['status']} {task['result_ref'] or ''}".rstrip())
    if dispatch_rc:
        if any(t["status"] == "failed" for t in result["tasks"].values()):
            return 1
        return 1 if dispatch_rc in {3, 130} else dispatch_rc
    return (
        0 if result["run_status"] == "ok" and all(t["status"] == "completed" for t in result["tasks"].values()) else 1
    )
