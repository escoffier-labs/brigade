"""Bounded cross-model orchestration for `brigade run`."""

from __future__ import annotations

import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from json import JSONDecoder
from pathlib import Path
from uuid import uuid4

from . import agents
from .roster import Agent, Roster, is_cli_allowed, timeout_for, workers


@dataclass(frozen=True)
class Assignment:
    worker: str
    task: str
    stage: int = 1


@dataclass(frozen=True)
class WorkerResult:
    worker: str
    task: str
    text: str
    ok: bool
    detail: str = ""


def build_plan_prompt(
    task: str,
    roster: Roster,
    corrective_note: str | None = None,
    read_only: bool = False,
) -> str:
    worker_lines = "\n".join(f"- {agent.name}: cli={agent.cli}; role={agent.role}" for agent in workers(roster))
    if not worker_lines:
        worker_lines = "- no workers configured"

    note = f"\nCorrection needed: {corrective_note}\n" if corrective_note else ""
    policy = f"\n\n{_read_only_rules()}\n" if read_only else ""
    return (
        "You are the Brigade aboyeur. Split the user's task across the available workers.\n"
        "Return exactly one JSON object, with no prose outside JSON:\n"
        '{"assignments":[{"stage":1,"worker":"<worker-name>","task":"<specific sub-task>"}]}\n'
        f"{note}\n"
        f"User task:\n{task}\n\n"
        f"Available workers, excluding you:\n{worker_lines}\n\n"
        f"Rules:\n- Use at most {roster.max_workers} assignments per stage.\n"
        "- Stage must be a positive integer starting at stage 1.\n"
        "- Assignments in the same stage run in parallel; later stages receive earlier-stage worker results.\n"
        "- Omit stage only for backwards-compatible stage 1 assignments.\n"
        "- Assign only listed workers.\n"
        "- Use zero assignments only if no worker is useful."
        f"{policy}"
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


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:48] or "brigade-run"


def _safe_document_content(text: str) -> str:
    # The ingester treats `##` as handoff section boundaries, so keep routed
    # document content at ### or below.
    return re.sub(r"(?m)^##(?!#)", "###", text).strip()


def _one_line(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def write_run_handoff(
    inbox: Path,
    *,
    task: str,
    cwd: Path | None,
    output_dir: Path | None,
    assignments: list[Assignment],
    worker_results: list[WorkerResult],
    final_text: str,
    read_only: bool = False,
    now: datetime | None = None,
) -> Path:
    timestamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d-%H%M")
    safe_task = _one_line(task)
    path = inbox / f"{timestamp}-brigade-run-{_slug(safe_task)}.md"
    worker_summary = (
        "\n".join(
            f"- {result.worker}: {'ok' if result.ok else 'failed'}"
            + (f" ({_one_line(result.detail)})" if result.detail else "")
            for result in worker_results
        )
        or "- no workers dispatched"
    )
    assignment_summary = (
        "\n".join(
            f"- stage {assignment.stage} -> {assignment.worker}: {_one_line(assignment.task)}"
            for assignment in assignments
        )
        or "- no worker assignments"
    )
    artifact_line = f"- artifacts: `{output_dir}`" if output_dir is not None else "- artifacts: none"
    cwd_line = f"- cwd: `{cwd}`" if cwd is not None else "- cwd: not set"
    mode_line = "- mode: read-only" if read_only else "- mode: normal"
    document_content = _safe_document_content(
        f"""### Brigade run: {_slug(safe_task)}
- task: {safe_task}
{artifact_line}
{cwd_line}
{mode_line}

Final answer:
{final_text}
"""
    )
    body = f"""# Memory Handoff

## Type

project-context

## Title

Brigade run completed: {_slug(safe_task)}

## Summary

Brigade completed a bounded plan-dispatch-synthesize run and produced a final answer. This handoff captures the task, assignments, worker status, artifact path, and final result for memory ingestion.

## Durable facts

- task: {safe_task}
{cwd_line}
{artifact_line}
{mode_line}
- orchestrated assignments:
{assignment_summary}
- worker status:
{worker_summary}

## Evidence

{artifact_line}
- final answer captured in this handoff

## Recommended memory action

no-card

## Target document

.learnings/LEARNINGS.md

## Suggested document content

{document_content}
"""
    inbox.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


def _read_only_rules() -> str:
    return (
        "READ-ONLY MODE:\n"
        "- Do not modify files.\n"
        "- Do not install packages, change configuration, commit, push, or call external write APIs.\n"
        "- You may inspect, reason, summarize, and recommend exact next steps.\n"
        "- If a task appears to require changes, describe the proposed changes instead of making them."
    )


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

    assignments: list[Assignment] = []
    seen: set[tuple[int, str, str]] = set()
    stage_counts: dict[int, int] = {}
    for item in raw_assignments:
        if not isinstance(item, dict):
            raise ValueError("each assignment must be an object")
        stage = item.get("stage", 1)
        if isinstance(stage, bool) or not isinstance(stage, int) or stage < 1:
            raise ValueError("assignment.stage must be a positive integer")
        raw_worker = item.get("worker")
        subtask = item.get("task")
        if not isinstance(raw_worker, str) or not raw_worker.strip():
            raise ValueError("assignment.worker must be a non-empty string")
        worker = raw_worker.strip()
        if worker not in roster.agents:
            raise ValueError(f"assignment references unknown worker: {worker!r}")
        if worker == roster.orchestrator:
            raise ValueError("assignment cannot target the orchestrator")
        if not isinstance(subtask, str) or not subtask.strip():
            raise ValueError("assignment.task must be a non-empty string")
        assignment = Assignment(worker=worker, task=subtask.strip(), stage=stage)
        key = (assignment.stage, assignment.worker, assignment.task)
        if key not in seen:
            assignments.append(assignment)
            seen.add(key)
            stage_counts[assignment.stage] = stage_counts.get(assignment.stage, 0) + 1

    for stage, count in stage_counts.items():
        if count > roster.max_workers:
            raise ValueError(f"plan has {count} assignments in stage {stage}, limit is {roster.max_workers}")
    return sorted(assignments, key=lambda assignment: assignment.stage)


def _record_plan_attempt(
    attempts: list[dict[str, object]] | None,
    *,
    stage: str,
    result: agents.AgentResult,
    parsed: bool = False,
    parse_error: str | None = None,
) -> None:
    if attempts is None:
        return
    payload: dict[str, object] = {
        "stage": stage,
        "ok": result.ok,
        "parsed": parsed,
        "detail": result.detail,
        "text": result.text,
    }
    if parse_error is not None:
        payload["parse_error"] = parse_error
    attempts.append(payload)


def _run_orchestrator(
    roster: Roster,
    prompt: str,
    cwd: Path | None = None,
    read_only: bool = False,
    sandbox_read_only: bool | None = None,
    sandbox: str | None = None,
) -> agents.AgentResult:
    orchestrator = roster.agents[roster.orchestrator]
    if not is_cli_allowed(orchestrator.cli, roster):
        return agents.AgentResult(
            text="",
            ok=False,
            detail=f"{orchestrator.cli} is not allowed by limits.allow_models",
        )
    kwargs: dict[str, object] = {
        "timeout": timeout_for(orchestrator, roster),
        "cwd": cwd,
        "read_only": read_only if sandbox_read_only is None else sandbox_read_only,
    }
    if sandbox is not None:
        kwargs["sandbox"] = sandbox
    if orchestrator.model is not None:
        kwargs["model"] = orchestrator.model
    return agents.run_agent(orchestrator.cli, prompt, **kwargs)


def plan(
    task: str,
    roster: Roster,
    cwd: Path | None = None,
    read_only: bool = False,
    sandbox_read_only: bool | None = None,
    sandbox: str | None = None,
    attempts: list[dict[str, object]] | None = None,
) -> list[Assignment]:
    first = _run_orchestrator(
        roster,
        build_plan_prompt(task, roster, read_only=read_only),
        cwd=cwd,
        read_only=read_only,
        sandbox_read_only=sandbox_read_only,
        sandbox=sandbox,
    )
    if not first.ok:
        _record_plan_attempt(attempts, stage="initial", result=first)
        raise RuntimeError(f"orchestrator failed during plan: {first.detail}")
    try:
        assignments = parse_plan(first.text, roster)
        _record_plan_attempt(attempts, stage="initial", result=first, parsed=True)
        return assignments
    except ValueError as exc:
        _record_plan_attempt(attempts, stage="initial", result=first, parse_error=str(exc))
        second = _run_orchestrator(
            roster,
            build_plan_prompt(task, roster, corrective_note=str(exc), read_only=read_only),
            cwd=cwd,
            read_only=read_only,
            sandbox_read_only=sandbox_read_only,
            sandbox=sandbox,
        )
        if not second.ok:
            _record_plan_attempt(attempts, stage="correction", result=second)
            raise RuntimeError(f"orchestrator failed during plan correction: {second.detail}") from exc
        try:
            assignments = parse_plan(second.text, roster)
            _record_plan_attempt(attempts, stage="correction", result=second, parsed=True)
            return assignments
        except ValueError as second_exc:
            _record_plan_attempt(
                attempts,
                stage="correction",
                result=second,
                parse_error=str(second_exc),
            )
            raise RuntimeError(f"orchestrator returned an invalid plan: {second_exc}") from second_exc


def _render_prior_results(results: list[WorkerResult]) -> str:
    return "\n\n".join(
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


def _worker_prompt(
    agent: Agent,
    assignment: Assignment,
    *,
    prior_results: list[WorkerResult] | None = None,
    read_only: bool = False,
) -> str:
    prior_context = ""
    if prior_results:
        prior_context = f"\n\nEarlier-stage context:\n{_render_prior_results(prior_results)}"
    policy = f"\n\n{_read_only_rules()}" if read_only else ""
    return (
        f"You are Brigade worker {agent.name}.\n"
        f"Role:\n{agent.role}\n\n"
        f"Sub-task:\n{assignment.task}\n\n"
        "Return a concise, complete result for the orchestrator to synthesize."
        f"{prior_context}"
        f"{policy}"
    )


def dispatch(
    assignments: list[Assignment],
    roster: Roster,
    cwd: Path | None = None,
    read_only: bool = False,
    sandbox_read_only: bool | None = None,
    sandbox: str | None = None,
) -> list[WorkerResult]:
    def run_one(assignment: Assignment, prior_results: list[WorkerResult]) -> WorkerResult:
        agent = roster.agents[assignment.worker]
        if not is_cli_allowed(agent.cli, roster):
            return WorkerResult(
                worker=assignment.worker,
                task=assignment.task,
                text="",
                ok=False,
                detail=f"{agent.cli} is not allowed by limits.allow_models",
            )
        kwargs: dict[str, object] = {
            "timeout": timeout_for(agent, roster),
            "cwd": cwd,
            "read_only": read_only if sandbox_read_only is None else sandbox_read_only,
        }
        if sandbox is not None:
            kwargs["sandbox"] = sandbox
        if agent.model is not None:
            kwargs["model"] = agent.model
        result = agents.run_agent(
            agent.cli,
            _worker_prompt(agent, assignment, prior_results=prior_results, read_only=read_only),
            **kwargs,
        )
        return WorkerResult(
            worker=assignment.worker,
            task=assignment.task,
            text=result.text,
            ok=result.ok,
            detail=result.detail,
        )

    if not assignments:
        return []

    all_results: list[WorkerResult] = []
    stages = sorted({assignment.stage for assignment in assignments})
    for stage in stages:
        stage_assignments = [assignment for assignment in assignments if assignment.stage == stage]
        stage_results_by_index: dict[int, WorkerResult] = {}
        prior_results = list(all_results)
        max_workers = min(roster.max_workers, len(stage_assignments))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_index = {
                executor.submit(run_one, assignment, prior_results): index
                for index, assignment in enumerate(stage_assignments)
            }
            for future in as_completed(future_to_index):
                index = future_to_index[future]
                try:
                    stage_results_by_index[index] = future.result()
                except Exception as exc:  # pragma: no cover - defensive boundary
                    assignment = stage_assignments[index]
                    stage_results_by_index[index] = WorkerResult(
                        worker=assignment.worker,
                        task=assignment.task,
                        text="",
                        ok=False,
                        detail=str(exc)[:200],
                    )
        all_results.extend(stage_results_by_index[index] for index in range(len(stage_assignments)))

    return all_results


def build_synth_prompt(task: str, results: list[WorkerResult], read_only: bool = False) -> str:
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

    policy = f"\n\n{_read_only_rules()}" if read_only else ""
    return (
        "You are the Brigade orchestrator. Synthesize the final answer for the user.\n"
        "Account for worker failures if any are present. Do not include implementation chatter.\n\n"
        f"Original task:\n{task}\n\n"
        f"Worker results:\n{rendered}\n"
        f"{policy}"
    )


def _print_plan(assignments: list[Assignment]) -> None:
    print("plan:")
    if not assignments:
        print("  (no worker assignments)")
        return
    stages = sorted({assignment.stage for assignment in assignments})
    if len(stages) == 1:
        for assignment in assignments:
            print(f"  -> {assignment.worker}: {assignment.task}")
        return
    for stage in stages:
        print(f"  stage {stage}:")
        for assignment in assignments:
            if assignment.stage == stage:
                print(f"    -> {assignment.worker}: {assignment.task}")


def _print_worker_status(results: list[WorkerResult]) -> None:
    print("workers:")
    if not results:
        print("  (none)")
        return
    for result in results:
        marker = "ok" if result.ok else "failed"
        detail = f": {result.detail}" if result.detail else ""
        print(f"  [{marker}] {result.worker}{detail}")


def _assignment_payload(assignments: list[Assignment]) -> list[dict[str, object]]:
    return [
        {"stage": assignment.stage, "worker": assignment.worker, "task": assignment.task} for assignment in assignments
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


def _agent_result_payload(result: agents.AgentResult) -> dict[str, object]:
    return {
        "ok": result.ok,
        "detail": result.detail,
        "text": result.text,
    }


def _roster_payload(roster: Roster) -> dict[str, object]:
    return {
        "orchestrator": roster.orchestrator,
        "max_workers": roster.max_workers,
        "timeout_seconds": roster.timeout_seconds,
        "allow_models": list(roster.allow_models),
        "agents": {
            name: {
                "cli": agent.cli,
                "model": agent.model,
                "role": agent.role,
                "timeout_seconds": agent.timeout_seconds,
            }
            for name, agent in roster.agents.items()
        },
    }


def _run_payload(
    *,
    task: str,
    cwd: Path | None,
    roster: Roster,
    dry_run: bool,
    read_only: bool,
    status: str,
    started_at: datetime,
    finished_at: datetime | None = None,
    output_dir: Path | None = None,
    handoff_path: Path | None = None,
    error: str | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "task": task,
        "cwd": str(cwd) if cwd is not None else None,
        "orchestrator": roster.orchestrator,
        "dry_run": dry_run,
        "read_only": read_only,
        "status": status,
        "started_at": _utc_iso(started_at),
    }
    if finished_at is not None:
        payload["finished_at"] = _utc_iso(finished_at)
        payload["duration_seconds"] = max(0.0, round((finished_at - started_at).total_seconds(), 3))
    if output_dir is not None:
        payload["artifacts"] = str(output_dir)
    if handoff_path is not None:
        payload["handoff"] = str(handoff_path)
    if error is not None:
        payload["error"] = error
    return payload


def run(
    task: str,
    roster: Roster,
    *,
    dry_run: bool = False,
    show_plan: bool = False,
    verbose: bool = False,
    cwd: Path | None = None,
    output_dir: Path | None = None,
    handoff_inbox: Path | None = None,
    read_only: bool = False,
    sandbox_read_only: bool | None = None,
    sandbox: str | None = None,
) -> int:
    started_at = datetime.now(timezone.utc)
    cwd = cwd.expanduser().resolve() if cwd is not None else None
    output_dir = output_dir.expanduser() if output_dir is not None else None
    handoff_inbox = handoff_inbox.expanduser() if handoff_inbox is not None else None
    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        _write_json(output_dir / "roster.json", _roster_payload(roster))
        _write_json(
            output_dir / "run.json",
            _run_payload(
                task=task,
                cwd=cwd,
                roster=roster,
                dry_run=dry_run,
                read_only=read_only,
                status="started",
                started_at=started_at,
                output_dir=output_dir,
            ),
        )

    plan_attempts: list[dict[str, object]] | None = [] if output_dir is not None else None
    try:
        assignments = plan(
            task,
            roster,
            cwd=cwd,
            read_only=read_only,
            sandbox_read_only=sandbox_read_only,
            sandbox=sandbox,
            attempts=plan_attempts,
        )
    except RuntimeError as exc:
        if output_dir is not None:
            finished_at = datetime.now(timezone.utc)
            _write_json(output_dir / "plan-attempts.json", {"attempts": plan_attempts or []})
            _write_json(
                output_dir / "run.json",
                _run_payload(
                    task=task,
                    cwd=cwd,
                    roster=roster,
                    dry_run=dry_run,
                    read_only=read_only,
                    status="failed",
                    started_at=started_at,
                    finished_at=finished_at,
                    output_dir=output_dir,
                    error=str(exc),
                ),
            )
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if output_dir is not None:
        _write_json(output_dir / "plan-attempts.json", {"attempts": plan_attempts or []})
        _write_json(output_dir / "plan.json", {"assignments": _assignment_payload(assignments)})

    if dry_run:
        payload = {"assignments": _assignment_payload(assignments)}
        if output_dir is not None:
            finished_at = datetime.now(timezone.utc)
            _write_json(
                output_dir / "run.json",
                _run_payload(
                    task=task,
                    cwd=cwd,
                    roster=roster,
                    dry_run=dry_run,
                    read_only=read_only,
                    status="dry-run",
                    started_at=started_at,
                    finished_at=finished_at,
                    output_dir=output_dir,
                ),
            )
        print(json.dumps(payload, indent=2))
        return 0

    if show_plan or verbose:
        _print_plan(assignments)

    worker_results = dispatch(
        assignments,
        roster,
        cwd=cwd,
        read_only=read_only,
        sandbox_read_only=sandbox_read_only,
        sandbox=sandbox,
    )
    if output_dir is not None:
        _write_json(output_dir / "worker-results.json", {"results": _worker_payload(worker_results)})
    if verbose:
        _print_worker_status(worker_results)
        print("synthesis:")
        print(f"  -> {roster.orchestrator}")

    final = _run_orchestrator(
        roster,
        build_synth_prompt(task, worker_results, read_only=read_only),
        cwd=cwd,
        read_only=read_only,
        sandbox_read_only=sandbox_read_only,
        sandbox=sandbox,
    )
    if output_dir is not None:
        _write_json(
            output_dir / "synthesis.json",
            {
                "orchestrator": roster.orchestrator,
                "result": _agent_result_payload(final),
            },
        )
    if not final.ok:
        if output_dir is not None:
            finished_at = datetime.now(timezone.utc)
            _write_json(
                output_dir / "run.json",
                _run_payload(
                    task=task,
                    cwd=cwd,
                    roster=roster,
                    dry_run=dry_run,
                    read_only=read_only,
                    status="failed",
                    started_at=started_at,
                    finished_at=finished_at,
                    output_dir=output_dir,
                    error=final.detail,
                ),
            )
        print(f"error: orchestrator failed during synthesis: {final.detail}", file=sys.stderr)
        return 2
    if output_dir is not None:
        finished_at = datetime.now(timezone.utc)
        (output_dir / "final.txt").write_text(final.text + "\n")
        _write_json(
            output_dir / "run.json",
            _run_payload(
                task=task,
                cwd=cwd,
                roster=roster,
                dry_run=dry_run,
                read_only=read_only,
                status="ok",
                started_at=started_at,
                finished_at=finished_at,
                output_dir=output_dir,
            ),
        )
    if handoff_inbox is not None:
        try:
            handoff = write_run_handoff(
                handoff_inbox,
                task=task,
                cwd=cwd,
                output_dir=output_dir,
                assignments=assignments,
                worker_results=worker_results,
                final_text=final.text,
                read_only=read_only,
            )
        except OSError as exc:
            detail = f"handoff failed: {exc}"
            if output_dir is not None:
                finished_at = datetime.now(timezone.utc)
                _write_json(
                    output_dir / "run.json",
                    _run_payload(
                        task=task,
                        cwd=cwd,
                        roster=roster,
                        dry_run=dry_run,
                        read_only=read_only,
                        status="handoff-failed",
                        started_at=started_at,
                        finished_at=finished_at,
                        output_dir=output_dir,
                        error=detail,
                    ),
                )
            print(f"error: {detail}", file=sys.stderr)
            print(final.text)
            return 2
        print(f"handoff: {handoff}", file=sys.stderr)
        if output_dir is not None:
            finished_at = datetime.now(timezone.utc)
            _write_json(
                output_dir / "run.json",
                _run_payload(
                    task=task,
                    cwd=cwd,
                    roster=roster,
                    dry_run=dry_run,
                    read_only=read_only,
                    status="ok",
                    started_at=started_at,
                    finished_at=finished_at,
                    output_dir=output_dir,
                    handoff_path=handoff,
                ),
            )
    print(final.text)
    return 0
