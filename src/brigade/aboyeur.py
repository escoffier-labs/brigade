"""Bounded cross-model orchestration for `brigade run`."""
from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from json import JSONDecoder
from pathlib import Path
from uuid import uuid4

from . import agents
from .roster import Agent, Roster, is_cli_allowed, workers


@dataclass(frozen=True)
class Assignment:
    worker: str
    task: str


@dataclass(frozen=True)
class WorkerResult:
    worker: str
    task: str
    text: str
    ok: bool
    detail: str = ""


def build_plan_prompt(task: str, roster: Roster, corrective_note: str | None = None) -> str:
    worker_lines = "\n".join(
        f"- {agent.name}: cli={agent.cli}; role={agent.role}" for agent in workers(roster)
    )
    if not worker_lines:
        worker_lines = "- no workers configured"

    note = f"\nCorrection needed: {corrective_note}\n" if corrective_note else ""
    return (
        "You are the Brigade aboyeur. Split the user's task across the available workers.\n"
        "Return exactly one JSON object, with no prose outside JSON:\n"
        '{"assignments":[{"worker":"<worker-name>","task":"<specific sub-task>"}]}\n'
        f"{note}\n"
        f"User task:\n{task}\n\n"
        f"Available workers, excluding you:\n{worker_lines}\n\n"
        f"Rules:\n- Use at most {roster.max_workers} assignments.\n"
        "- Assign only listed workers.\n"
        "- Use zero assignments only if no worker is useful."
    )


def _extract_json(text: str) -> object:
    stripped = text.strip()
    fenced = _extract_fenced_json(stripped)
    if fenced is not None:
        return json.loads(fenced)
    return _loads_first_json_object(stripped)


def _extract_fenced_json(text: str) -> str | None:
    lines = text.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.strip().startswith("```"):
            start = index + 1
            break
    if start is None:
        return None

    for end in range(start, len(lines)):
        if lines[end].strip().startswith("```"):
            return "\n".join(lines[start:end]).strip()
    return None


def _loads_first_json_object(text: str) -> object:
    decoder = JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        return value
    return json.loads(text)


def make_run_dir(base: Path, now: datetime | None = None) -> Path:
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%d-%H%M%S")
    return base / f"{stamp}-{uuid4().hex[:8]}"


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")


def parse_plan(text: str, roster: Roster) -> list[Assignment]:
    try:
        payload = _extract_json(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"plan is not valid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise ValueError("plan JSON must be an object")
    raw_assignments = payload.get("assignments")
    if not isinstance(raw_assignments, list):
        raise ValueError("plan JSON needs an assignments list")
    if len(raw_assignments) > roster.max_workers:
        raise ValueError(f"plan has {len(raw_assignments)} assignments, limit is {roster.max_workers}")

    assignments: list[Assignment] = []
    seen: set[tuple[str, str]] = set()
    for item in raw_assignments:
        if not isinstance(item, dict):
            raise ValueError("each assignment must be an object")
        worker = item.get("worker")
        subtask = item.get("task")
        if not isinstance(worker, str) or not worker.strip():
            raise ValueError("assignment.worker must be a non-empty string")
        if worker not in roster.agents:
            raise ValueError(f"assignment references unknown worker: {worker!r}")
        if worker == roster.orchestrator:
            raise ValueError("assignment cannot target the orchestrator")
        if not isinstance(subtask, str) or not subtask.strip():
            raise ValueError("assignment.task must be a non-empty string")
        assignment = Assignment(worker=worker.strip(), task=subtask.strip())
        key = (assignment.worker, assignment.task)
        if key not in seen:
            assignments.append(assignment)
            seen.add(key)
    return assignments


def _run_orchestrator(roster: Roster, prompt: str, cwd: Path | None = None) -> agents.AgentResult:
    orchestrator = roster.agents[roster.orchestrator]
    if not is_cli_allowed(orchestrator.cli, roster):
        return agents.AgentResult(
            text="",
            ok=False,
            detail=f"{orchestrator.cli} is not allowed by limits.allow_models",
        )
    return agents.run_agent(orchestrator.cli, prompt, cwd=cwd)


def plan(task: str, roster: Roster, cwd: Path | None = None) -> list[Assignment]:
    first = _run_orchestrator(roster, build_plan_prompt(task, roster), cwd=cwd)
    if not first.ok:
        raise RuntimeError(f"orchestrator failed during plan: {first.detail}")
    try:
        return parse_plan(first.text, roster)
    except ValueError as exc:
        second = _run_orchestrator(roster, build_plan_prompt(task, roster, corrective_note=str(exc)), cwd=cwd)
        if not second.ok:
            raise RuntimeError(f"orchestrator failed during plan correction: {second.detail}") from exc
        try:
            return parse_plan(second.text, roster)
        except ValueError as second_exc:
            raise RuntimeError(f"orchestrator returned an invalid plan: {second_exc}") from second_exc


def _worker_prompt(agent: Agent, assignment: Assignment) -> str:
    return (
        f"You are Brigade worker {agent.name}.\n"
        f"Role:\n{agent.role}\n\n"
        f"Sub-task:\n{assignment.task}\n\n"
        "Return a concise, complete result for the orchestrator to synthesize."
    )


def dispatch(assignments: list[Assignment], roster: Roster, cwd: Path | None = None) -> list[WorkerResult]:
    def run_one(assignment: Assignment) -> WorkerResult:
        agent = roster.agents[assignment.worker]
        if not is_cli_allowed(agent.cli, roster):
            return WorkerResult(
                worker=assignment.worker,
                task=assignment.task,
                text="",
                ok=False,
                detail=f"{agent.cli} is not allowed by limits.allow_models",
            )
        result = agents.run_agent(agent.cli, _worker_prompt(agent, assignment), cwd=cwd)
        return WorkerResult(
            worker=assignment.worker,
            task=assignment.task,
            text=result.text,
            ok=result.ok,
            detail=result.detail,
        )

    if not assignments:
        return []

    results_by_index: dict[int, WorkerResult] = {}
    max_workers = min(roster.max_workers, len(assignments))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_index = {
            executor.submit(run_one, assignment): index
            for index, assignment in enumerate(assignments)
        }
        for future in as_completed(future_to_index):
            index = future_to_index[future]
            try:
                results_by_index[index] = future.result()
            except Exception as exc:  # pragma: no cover - defensive boundary
                assignment = assignments[index]
                results_by_index[index] = WorkerResult(
                    worker=assignment.worker,
                    task=assignment.task,
                    text="",
                    ok=False,
                    detail=str(exc)[:200],
                )

    return [results_by_index[index] for index in range(len(assignments))]


def build_synth_prompt(task: str, results: list[WorkerResult]) -> str:
    if results:
        rendered = "\n\n".join(
            "\n".join(
                [
                    f"Worker: {result.worker}",
                    f"Sub-task: {result.task}",
                    f"Status: {'ok' if result.ok else 'failed'}",
                    f"Detail: {result.detail}" if result.detail else "Detail:",
                    "Output:",
                    result.text or "(no output)",
                ]
            )
            for result in results
        )
    else:
        rendered = "(No workers were assigned.)"

    return (
        "You are the Brigade orchestrator. Synthesize the final answer for the user.\n"
        "Account for worker failures if any are present. Do not include implementation chatter.\n\n"
        f"Original task:\n{task}\n\n"
        f"Worker results:\n{rendered}\n"
    )


def _print_plan(assignments: list[Assignment]) -> None:
    print("plan:")
    if not assignments:
        print("  (no worker assignments)")
        return
    for assignment in assignments:
        print(f"  -> {assignment.worker}: {assignment.task}")


def _print_worker_status(results: list[WorkerResult]) -> None:
    print("workers:")
    if not results:
        print("  (none)")
        return
    for result in results:
        marker = "ok" if result.ok else "failed"
        detail = f": {result.detail}" if result.detail else ""
        print(f"  [{marker}] {result.worker}{detail}")


def _assignment_payload(assignments: list[Assignment]) -> list[dict[str, str]]:
    return [
        {"worker": assignment.worker, "task": assignment.task}
        for assignment in assignments
    ]


def _worker_payload(results: list[WorkerResult]) -> list[dict[str, object]]:
    return [
        {
            "worker": result.worker,
            "task": result.task,
            "ok": result.ok,
            "detail": result.detail,
            "text": result.text,
        }
        for result in results
    ]


def run(
    task: str,
    roster: Roster,
    *,
    dry_run: bool = False,
    show_plan: bool = False,
    verbose: bool = False,
    cwd: Path | None = None,
    output_dir: Path | None = None,
) -> int:
    cwd = cwd.expanduser().resolve() if cwd is not None else None
    output_dir = output_dir.expanduser() if output_dir is not None else None
    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        _write_json(
            output_dir / "run.json",
            {
                "task": task,
                "cwd": str(cwd) if cwd is not None else None,
                "orchestrator": roster.orchestrator,
                "dry_run": dry_run,
                "status": "started",
            },
        )

    try:
        assignments = plan(task, roster, cwd=cwd)
    except RuntimeError as exc:
        if output_dir is not None:
            _write_json(
                output_dir / "run.json",
                {
                    "task": task,
                    "cwd": str(cwd) if cwd is not None else None,
                    "orchestrator": roster.orchestrator,
                    "dry_run": dry_run,
                    "status": "failed",
                    "error": str(exc),
                },
            )
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if output_dir is not None:
        _write_json(output_dir / "plan.json", {"assignments": _assignment_payload(assignments)})

    if dry_run:
        payload = {"assignments": _assignment_payload(assignments)}
        if output_dir is not None:
            _write_json(
                output_dir / "run.json",
                {
                    "task": task,
                    "cwd": str(cwd) if cwd is not None else None,
                    "orchestrator": roster.orchestrator,
                    "dry_run": dry_run,
                    "status": "dry-run",
                    "artifacts": str(output_dir),
                },
            )
        print(json.dumps(payload, indent=2))
        return 0

    if show_plan or verbose:
        _print_plan(assignments)

    worker_results = dispatch(assignments, roster, cwd=cwd)
    if output_dir is not None:
        _write_json(output_dir / "worker-results.json", {"results": _worker_payload(worker_results)})
    if verbose:
        _print_worker_status(worker_results)
        print("synthesis:")
        print(f"  -> {roster.orchestrator}")

    final = _run_orchestrator(roster, build_synth_prompt(task, worker_results), cwd=cwd)
    if not final.ok:
        if output_dir is not None:
            _write_json(
                output_dir / "run.json",
                {
                    "task": task,
                    "cwd": str(cwd) if cwd is not None else None,
                    "orchestrator": roster.orchestrator,
                    "dry_run": dry_run,
                    "status": "failed",
                    "error": final.detail,
                    "artifacts": str(output_dir),
                },
            )
        print(f"error: orchestrator failed during synthesis: {final.detail}", file=sys.stderr)
        return 2
    if output_dir is not None:
        (output_dir / "final.txt").write_text(final.text + "\n")
        _write_json(
            output_dir / "run.json",
            {
                "task": task,
                "cwd": str(cwd) if cwd is not None else None,
                "orchestrator": roster.orchestrator,
                "dry_run": dry_run,
                "status": "ok",
                "artifacts": str(output_dir),
            },
        )
    print(final.text)
    return 0
