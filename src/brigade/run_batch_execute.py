"""Explicit batches adapt the existing run orchestrator without model planning."""

from __future__ import annotations

import contextlib
import hashlib
import json
import sys
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import localio, run_tasks, run_lifecycle, run_journal
from .run_batch import BatchError
from .run_transport import Assignment, WorkerResult
from .roster import Roster


def direct_assignments(
    worker: str, task: str, skill_ids: tuple[str, ...], execution: BatchExecution | None, roster: Roster
) -> list[Assignment]:
    """Keep the normal direct-worker assignment unchanged."""
    if execution is None:
        return [Assignment(worker=worker, task=task, stage=1, selected_skill_ids=skill_ids)]
    return execution.assignments(roster)


class BatchExecution:
    def __init__(self, plan: dict[str, Any], run_dir: Path, workspace: Path):
        self.plan = plan
        self.run_dir = run_dir
        self.workspace = workspace
        self.by_prompt = {f"Batch task_id: {t['task_id']}\n\n{t['text']}": t for t in plan["tasks"]}

    def assignments(self, roster: Roster) -> list[Assignment]:
        from .aboyeur import artifacts

        for task in self.plan["tasks"]:
            error = artifacts._direct_worker_error(task["seat"], roster, read_only=False)
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
        return [
            Assignment(worker=t["seat"], task=prompt, stage=layers[t["task_id"]])
            for prompt, t in self.by_prompt.items()
        ]

    def observe(self, result: WorkerResult) -> None:
        task = self.by_prompt[result.task]
        current = next(r for r in run_tasks.read_tasks(self.run_dir) if r.handle.spec.task_id == task["task_id"])
        if current.result_id is not None:
            return
        handle = run_tasks.prepare_attempt(self.run_dir, workspace=self.workspace, task_id=task["task_id"])
        # Skipped tasks retain queued state. They were never launched.
        if result.status == "skipped":
            return
        path = self.run_dir / f"batch-result-{hashlib.sha256(task['task_id'].encode()).hexdigest()}.json"
        localio.write_json(path, asdict(result))
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
            handle = run_tasks.prepare_attempt(
                self.run_dir, workspace=self.workspace, task_id=self.by_prompt[assignment.task]["task_id"]
            )
            run_tasks.observe_task(
                self.run_dir,
                workspace=self.workspace,
                handle=handle,
                observation=run_tasks.TaskObservation(status="unknown"),
            )
            return original_prompt(agent, assignment, **prompt_kwargs)

        kwargs["build_prompt"] = build_prompt
        kwargs["on_result"] = self.observe
        return planning.dispatch(waves, roster, **kwargs)

    def finish(self, results: list[WorkerResult]) -> int:
        from .aboyeur import artifacts

        for result in results:
            self.observe(result)
        ok = len(results) == len(self.plan["tasks"]) and all(r.ok for r in results)
        artifacts.update_run_receipt(
            self.run_dir, status="ok" if ok else "incomplete", finished_at=datetime.now(timezone.utc).isoformat()
        )
        return 0 if ok else 1


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


def execute_cli(args: Any, plan: dict[str, Any]) -> int:
    from . import cli
    from .cli.run import dispatch

    if plan["synthesis"] is not None:
        raise BatchError("synthesis-not-implemented", "batch synthesis execution is not implemented")
    workspace = args.cwd.expanduser().resolve()
    run_dir = workspace / ".brigade" / "runs" / ("batch-" + hashlib.sha256(plan["request_id"].encode()).hexdigest())
    # Atomic directory creation is the request-level launch fence. The marker
    # stays inside existing run storage, including after interruption.
    try:
        run_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        marker = localio.read_json_dict(run_dir / "batch-request.json")
        if marker is None:
            raise BatchError(
                "request-unknown", "request reservation has no readable fingerprint; refusing launch"
            ) from None
        if marker.get("fingerprint") != plan["fingerprint"]:
            raise BatchError("request-conflict", "request_id already belongs to a different batch") from None
    else:
        localio.write_json(run_dir / "batch-request.json", plan)
        run_journal._fsync_directory(run_dir)
        run_journal._fsync_directory(run_dir.parent)
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
        if args.roster is not None:
            argv.extend(["--roster", str(args.roster)])
        normal = cli._build_parser_for(argv).parse_args(argv)
        normal.batch_execution = BatchExecution(plan, run_dir, workspace)
        try:
            with contextlib.redirect_stdout(sys.stderr):
                dispatch(normal)
        except (KeyboardInterrupt, TimeoutError):
            pass
    result = stored_result(plan, run_dir)
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        print(f"run {result['run_id']}")
        for task_id, task in result["tasks"].items():
            print(f"{task_id}: {task['status']} {task['result_ref'] or ''}".rstrip())
    return (
        0 if result["run_status"] == "ok" and all(t["status"] == "completed" for t in result["tasks"].values()) else 1
    )
