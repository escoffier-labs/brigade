"""Stage 1 compatibility seam for plan parse, plan, and dispatch."""
# ruff: noqa: F401

from __future__ import annotations

import copy
import inspect
import json
import os
import re
import signal
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from functools import partial, wraps
from json import JSONDecoder
from pathlib import Path
from typing import Any, Callable, ContextManager, Iterator, Mapping, Sequence
from uuid import uuid4

from .. import agents
from .. import codex_appserver
from .. import context_eval
from .. import evidence_brief as evidence_brief_mod
from .. import graphtrail_delta
from .. import causal_receipt
from .. import localio
from .. import message_envelope
from .. import proc, receipt_schema, runguard
from .. import run_control
from .. import run_checkpoint
from .. import run_budget
from .. import run_events
from .. import run_journal
from .. import run_lifecycle
from .. import run_projector
from .. import run_shadow
from .. import seat_health
from .. import seat_health_policy
from .. import verification_contract
from ..result_integrity import validate_final_output
from ..run_receipts import (
    agent_result_from_worker as _agent_result_from_worker,
    agent_result_payload as _agent_result_payload,
    assignment_payload as _assignment_payload,
    worker_payload as _worker_payload,
    write_agent_logs as _write_agent_logs,
    write_worker_logs as _write_worker_logs,
)
from ..run_transport import Assignment, WorkerResult, dag_cycle_members
from ..roster import Agent, Roster, is_cli_allowed, read_only_capability_error, timeout_for, workers
from ..route_catalog import RouteBrief, route_brief, uncovered_stages, unknown_covers
from ..route_policy import (
    RoutePolicyDecision,
    direct_worker_skill_ids,
    planner_skill_policy_section,
    validate_plan_skill_bindings,
    worker_skill_policy_constraint,
)

from . import briefs, run_io, prompts, artifacts
from . import orchestrator as _orchestrator_mod


# The corrective turn is the last chance before the run dies on an unparsable
# plan, so it restates the output contract instead of only naming the parse error.
PLAN_JSON_ONLY_RULE = (
    "Reply with the JSON plan object and nothing else: no prose, no preamble, no explanation, "
    "no tool-failure or hook commentary, nothing before or after the object. "
    'If no worker is useful, reply with exactly {"assignments": []}.'
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


def _read_only_rules() -> str:
    return (
        "READ-ONLY MODE:\n"
        "- Do not modify files.\n"
        "- Do not install packages, change configuration, commit, push, or call external write APIs.\n"
        "- You may inspect, reason, summarize, and recommend exact next steps.\n"
        "- If a task appears to require changes, describe the proposed changes instead of making them."
    )


def parse_plan(
    text: str,
    roster: Roster,
    *,
    read_only: bool = False,
    skill_policy: RoutePolicyDecision | None = None,
) -> list[Assignment]:
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
        if read_only:
            capability_error = read_only_capability_error(roster.agents[worker])
            if capability_error is not None:
                raise ValueError(capability_error)
        if not isinstance(subtask, str) or not subtask.strip():
            raise ValueError("assignment.task must be a non-empty string")
        raw_covers = item.get("covers", [])
        if not isinstance(raw_covers, list) or any(not isinstance(c, str) or not c.strip() for c in raw_covers):
            raise ValueError("assignment.covers must be a list of non-empty strings")
        covers = tuple(dict.fromkeys(c.strip() for c in raw_covers))
        raw_selected = item.get("selected_skill_ids", [])
        if raw_selected is None:
            raw_selected = []
        if not isinstance(raw_selected, list) or any(
            not isinstance(skill_id, str) or not skill_id.strip() for skill_id in raw_selected
        ):
            raise ValueError("assignment.selected_skill_ids must be a list of non-empty strings when set")
        selected_skill_ids = tuple(dict.fromkeys(skill_id.strip() for skill_id in raw_selected))
        raw_domain = item.get("domain")
        domain: str | None = None
        if raw_domain is not None:
            if not isinstance(raw_domain, str) or not raw_domain.strip():
                raise ValueError("assignment.domain must be a non-empty string when set")
            domain = raw_domain.strip()
        raw_capabilities = item.get("capabilities", [])
        if raw_capabilities is None:
            raw_capabilities = []
        if not isinstance(raw_capabilities, list) or any(
            not isinstance(capability, str) or not capability.strip() for capability in raw_capabilities
        ):
            raise ValueError("assignment.capabilities must be a list of non-empty strings when set")
        capabilities = tuple(dict.fromkeys(capability.strip() for capability in raw_capabilities))
        raw_max_risk = item.get("max_risk_class")
        max_risk_class: str | None = None
        if raw_max_risk is not None:
            if not isinstance(raw_max_risk, str) or not raw_max_risk.strip():
                raise ValueError("assignment.max_risk_class must be a non-empty string when set")
            max_risk_class = raw_max_risk.strip()
            from ..tools_cmd.constants import RISK_CLASSES

            if max_risk_class not in RISK_CLASSES:
                raise ValueError("assignment.max_risk_class must be one of: " + ", ".join(RISK_CLASSES))
        assignment = Assignment(
            worker=worker,
            task=subtask.strip(),
            stage=stage,
            covers=covers,
            selected_skill_ids=selected_skill_ids,
            domain=domain,
            capabilities=capabilities,
            max_risk_class=max_risk_class,
        )
        key = (assignment.stage, assignment.worker, assignment.task)
        if key not in seen:
            assignments.append(assignment)
            seen.add(key)
            stage_counts[assignment.stage] = stage_counts.get(assignment.stage, 0) + 1
        elif covers or selected_skill_ids or domain or capabilities or max_risk_class:
            # Duplicates merge their covers instead of dropping them, so a plan
            # that tags the same assignment twice still counts as covering both.
            for index, existing in enumerate(assignments):
                if (existing.stage, existing.worker, existing.task) == key:
                    merged = tuple(dict.fromkeys(existing.covers + covers))
                    merged_skills = tuple(dict.fromkeys(existing.selected_skill_ids + selected_skill_ids))
                    merged_caps = tuple(dict.fromkeys(existing.capabilities + capabilities))
                    merged_domain = domain or existing.domain
                    merged_risk = max_risk_class or existing.max_risk_class
                    assignments[index] = replace(
                        existing,
                        covers=merged,
                        selected_skill_ids=merged_skills,
                        domain=merged_domain,
                        capabilities=merged_caps,
                        max_risk_class=merged_risk,
                    )
                    break

    for stage, count in stage_counts.items():
        if count > roster.max_workers:
            raise ValueError(f"plan has {count} assignments in stage {stage}, limit is {roster.max_workers}")
    validate_plan_skill_bindings(assignments, skill_policy)
    return sorted(assignments, key=lambda assignment: assignment.stage)


def _record_plan_attempt(
    attempts: list[dict[str, object]] | None,
    *,
    stage: str,
    result: agents.AgentResult,
    parsed: bool = False,
    parse_error: str | None = None,
    coverage_missing: list[str] | None = None,
    unknown_covers: list[str] | None = None,
) -> None:
    if attempts is None:
        return
    payload: dict[str, object] = {
        "stage": stage,
        "ok": result.ok,
        "parsed": parsed,
        "detail": result.detail,
        "text": result.text,
        "timed_out": result.timed_out,
        "status": result.status,
    }
    if result.failure_phase is not None:
        payload["failure_phase"] = result.failure_phase
    if result.failure_kind is not None:
        payload["failure_kind"] = result.failure_kind
    if parse_error is not None:
        payload["parse_error"] = parse_error
    if coverage_missing:
        payload["coverage_missing"] = list(coverage_missing)
    if unknown_covers:
        payload["unknown_covers"] = list(unknown_covers)
    attempts.append(payload)


def _orchestrator_failure_detail(
    result: agents.AgentResult,
    *,
    seat: str,
    cli: str | None,
    transport: str,
) -> str:
    if cli != "codex":
        return result.detail or ""
    # Prefer stderr: it still carries the raw codex banner after run_agent maps detail.
    source = result.stderr or result.detail or ""
    mapped = agents.codex_stdin_hang_detail(source, seat=seat, transport=transport)
    if mapped is not None:
        return mapped
    if "blocked waiting for stdin" in (result.detail or "").lower():
        return (
            agents.codex_stdin_hang_detail(
                "Reading additional input from stdin",
                seat=seat,
                transport=transport,
            )
            or result.detail
        )
    return result.detail or ""


def resolve_run_sandbox(
    *,
    sandbox: str | None = None,
    roster_sandbox: str | None = None,
    read_only: bool = False,
    health: object | None = None,
) -> str | None:
    """Resolve the sandbox every write-capable stage for a run must inherit.

    Precedence: persisted ``health.effective_sandbox``, the explicit argument,
    the roster declaration, then the read-only default. A recorded ``None`` in
    health still falls through so a later explicit or roster value is not lost.
    """
    if isinstance(health, dict):
        stored = health.get("effective_sandbox")
        if isinstance(stored, str) and stored:
            return stored
    if sandbox is not None:
        return sandbox
    if roster_sandbox is not None:
        return roster_sandbox
    return "read-only" if read_only else None


def _run_orchestrator(
    roster: Roster,
    prompt: str,
    cwd: Path | None = None,
    read_only: bool = False,
    sandbox_read_only: bool | None = None,
    sandbox: str | None = None,
    codex_transport: str | None = None,
    process_registry: proc.ProcessRegistry | None = None,
    model_lease: Callable[[Agent], ContextManager[str | None]] | None = None,
) -> agents.AgentResult:
    orchestrator = roster.agents[roster.orchestrator]
    transport = codex_transport or roster.codex_transport
    if orchestrator.cli is None or not is_cli_allowed(orchestrator.cli, roster):
        return agents.AgentResult(
            text="",
            ok=False,
            detail=f"{orchestrator.cli} is not allowed by limits.allow_models",
        )
    if orchestrator.cli.startswith("codex-cloud:"):
        # A cloud task returns a status report plus diff, not a plan or a
        # synthesis; the orchestrator seat must be a conversational CLI.
        return agents.AgentResult(
            text="",
            ok=False,
            detail="codex-cloud seats are workers only; pick a local CLI for the orchestrator",
        )
    cloudflare_detail = agents.cloudflare_ai_gateway_preflight_detail(orchestrator.model)
    if cloudflare_detail is not None:
        return agents.AgentResult(
            text="",
            ok=False,
            detail=cloudflare_detail,
            failure_phase="preflight",
            failure_kind="provider-config",
        )
    effective_read_only = read_only if sandbox_read_only is None else sandbox_read_only
    # Do not invent a sandbox kwarg from read_only alone: legacy run_agent
    # doubles omit that parameter, and prompt-level read-only already
    # travels as read_only=. Roster or an explicit flag still propagate.
    effective_sandbox = resolve_run_sandbox(
        sandbox=sandbox,
        roster_sandbox=roster.sandbox,
        read_only=False,
    )
    kwargs: dict[str, object] = {
        "timeout": timeout_for(orchestrator, roster),
        "cwd": cwd,
        "read_only": effective_read_only,
    }
    if effective_sandbox is not None:
        kwargs["sandbox"] = effective_sandbox
    if orchestrator.model is not None:
        kwargs["model"] = orchestrator.model
    if orchestrator.reasoning is not None:
        kwargs["reasoning"] = orchestrator.reasoning
    if orchestrator.env is not None:
        kwargs["env"] = dict(orchestrator.env)
    if orchestrator.command is not None:
        kwargs["command"] = orchestrator.command
    if model_lease is None:
        result = _call_with_process_registry(
            agents.run_agent, orchestrator.cli, prompt, process_registry=process_registry, **kwargs
        )
    else:
        with model_lease(orchestrator) as lease_error:
            if lease_error is not None:
                return agents.AgentResult(
                    text="",
                    ok=False,
                    detail=lease_error,
                    failure_phase="preflight",
                    failure_kind="fleet-model-policy",
                )
            result = _call_with_process_registry(
                agents.run_agent, orchestrator.cli, prompt, process_registry=process_registry, **kwargs
            )
    if not result.ok and orchestrator.cli == "codex":
        detail = _orchestrator_failure_detail(
            result,
            seat=roster.orchestrator,
            cli=orchestrator.cli,
            transport=transport,
        )
        if detail != result.detail:
            return agents.AgentResult(
                text=result.text,
                ok=False,
                detail=detail,
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=result.exit_code,
                timed_out=result.timed_out,
                requested_model=result.requested_model,
                reasoning=result.reasoning,
                transport=result.transport,
            )
    return result


def _call_with_process_registry(
    function: Callable[..., Any],
    *args: Any,
    process_registry: proc.ProcessRegistry | None,
    **kwargs: Any,
) -> Any:
    parameters = inspect.signature(function).parameters.values()
    accepts_registry = any(
        parameter.name == "process_registry" or parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )
    if accepts_registry:
        kwargs["process_registry"] = process_registry
    return function(*args, **kwargs)


def _coverage_missing(route: RouteBrief | None, assignments: list[Assignment]) -> list[str]:
    if route is None or not route.attached or not route.route:
        return []
    return uncovered_stages(route, assignments)


def _unknown_covers(route: RouteBrief | None, assignments: list[Assignment]) -> list[str]:
    if route is None or not route.attached or not route.route:
        return []
    return unknown_covers(route, assignments)


def _validate_plan_dependencies(route: RouteBrief | None, assignments: list[Assignment]) -> None:
    if route is None or not route.attached:
        return
    members = dag_cycle_members(assignments, route.dependencies)
    if members:
        raise ValueError(f"plan has a dependency cycle involving: {', '.join(members)}")


def _orchestrator_hides_write_tools(
    roster: Roster,
    *,
    read_only: bool,
    sandbox_read_only: bool | None,
    sandbox: str | None,
) -> bool:
    """True when the orchestrator seat launches without any file-write tool."""
    orchestrator = roster.agents.get(roster.orchestrator)
    if orchestrator is None or orchestrator.cli is None:
        return False
    # _run_orchestrator resolves read-only the same way before building argv.
    effective_read_only = read_only if sandbox_read_only is None else sandbox_read_only
    return agents.hides_write_tools(orchestrator.cli, read_only=effective_read_only, sandbox=sandbox)


def _orchestrator_harness(roster: Roster) -> str | None:
    agent = roster.agents.get(roster.orchestrator)
    return agent.cli if agent is not None else None


def _emit_plan_request(
    prompt: str,
    roster: Roster,
    *,
    output_dir: Path | None,
) -> str:
    delivery = message_envelope.emit(
        prompt,
        kind="plan-request",
        producer="aboyeur.build_plan_prompt",
        from_seat=message_envelope.BRIGADE_SEAT,
        to_seat=roster.orchestrator,
        run_dir=output_dir,
        run_id=output_dir.name if output_dir is not None else None,
        assignment_id=message_envelope.default_assignment_id("plan-request"),
        session_harness=_orchestrator_harness(roster),
    )
    if not delivery.delivered:
        raise RuntimeError(delivery.reason or "plan-request rejected by provenance gate")
    return prompt


def _parse_gated_plan(
    text: str,
    roster: Roster,
    *,
    output_dir: Path | None,
    read_only: bool = False,
    skill_policy: RoutePolicyDecision | None = None,
) -> list[Assignment]:
    delivery = message_envelope.emit(
        text,
        kind="plan-result",
        producer="aboyeur.plan",
        from_seat=roster.orchestrator,
        to_seat=message_envelope.BRIGADE_SEAT,
        run_dir=output_dir,
        run_id=output_dir.name if output_dir is not None else None,
        assignment_id=message_envelope.default_assignment_id("plan-result"),
        session_harness=_orchestrator_harness(roster),
    )
    if not delivery.delivered:
        raise ValueError(delivery.reason or "plan-result rejected by provenance gate")
    return parse_plan(text, roster, read_only=read_only, skill_policy=skill_policy)


def _result_from_seat(result: WorkerResult) -> str:
    """Seat that actually produced the terminal text (fallback-aware)."""

    for attempt in result.attempts:
        if attempt.selected and attempt.worker:
            return attempt.worker
    return result.worker


def _admitted_result_output(
    result: WorkerResult,
    *,
    run_id: str | None = None,
    to_seat: str | None = None,
) -> str:
    env = result.provenance
    if not isinstance(env, dict):
        _legacy, display = message_envelope.synthesize_legacy_message_provenance()
        return display
    binding = message_envelope.message_binding(env)
    expected_message_id = binding["message_id"] if binding is not None else env.get("item_id")
    admission = message_envelope.admit_message(
        result.text,
        env,
        kind="worker-result",
        producer="run_transport.dispatch",
        run_id=run_id or message_envelope.IN_MEMORY_RUN_ID,
        message_id=expected_message_id if isinstance(expected_message_id, str) else None,
        assignment_id=message_envelope.assignment_id_for(result.worker, result.task),
        from_seat=_result_from_seat(result),
        to_seat=to_seat,
    )
    if not admission.delivered:
        return admission.display or admission.reason or message_envelope.LEGACY_MESSAGE_DISPLAY
    return message_envelope.wrap_message_body(result.text, env)


def plan(
    task: str,
    roster: Roster,
    cwd: Path | None = None,
    read_only: bool = False,
    sandbox_read_only: bool | None = None,
    sandbox: str | None = None,
    attempts: list[dict[str, object]] | None = None,
    code_graph: briefs.CodeGraphBrief | None = None,
    drift_impact: briefs.DriftImpactBrief | None = None,
    evidence: briefs.EvidenceBrief | None = None,
    route: RouteBrief | None = None,
    skill_policy: RoutePolicyDecision | None = None,
    codex_transport: str | None = None,
    process_registry: proc.ProcessRegistry | None = None,
    output_dir: Path | None = None,
    model_lease: Callable[[Agent], ContextManager[str | None]] | None = None,
) -> list[Assignment]:
    transport = codex_transport or roster.codex_transport
    no_file_writes = _orchestrator_hides_write_tools(
        roster,
        read_only=read_only,
        sandbox_read_only=sandbox_read_only,
        sandbox=sandbox,
    )
    first = _call_with_process_registry(
        _run_orchestrator,
        roster,
        _emit_plan_request(
            briefs.build_plan_prompt(
                task,
                roster,
                read_only=read_only,
                code_graph=code_graph,
                drift_impact=drift_impact,
                evidence=evidence,
                route=route,
                no_file_writes=no_file_writes,
                skill_policy=skill_policy,
            ),
            roster,
            output_dir=output_dir,
        ),
        cwd=cwd,
        read_only=read_only,
        sandbox_read_only=sandbox_read_only,
        sandbox=sandbox,
        codex_transport=transport,
        process_registry=process_registry,
        model_lease=model_lease,
    )
    if not first.ok:
        _record_plan_attempt(attempts, stage="initial", result=first)
        raise RuntimeError(f"orchestrator failed during plan: {first.detail}")
    try:
        assignments = _parse_gated_plan(
            first.text,
            roster,
            output_dir=output_dir,
            read_only=read_only,
            skill_policy=skill_policy,
        )
        _validate_plan_dependencies(route, assignments)
        _record_plan_attempt(
            attempts,
            stage="initial",
            result=first,
            parsed=True,
            coverage_missing=_coverage_missing(route, assignments),
            unknown_covers=_unknown_covers(route, assignments),
        )
    except ValueError as exc:
        _record_plan_attempt(attempts, stage="initial", result=first, parse_error=str(exc))
        # Schema-force the retry: the parse error alone left a seat whose final
        # message had been hijacked by hooks with nothing to correct toward (#518).
        second = _call_with_process_registry(
            _run_orchestrator,
            roster,
            _emit_plan_request(
                briefs.build_plan_prompt(
                    task,
                    roster,
                    corrective_note=f"{exc} {PLAN_JSON_ONLY_RULE}",
                    read_only=read_only,
                    code_graph=code_graph,
                    drift_impact=drift_impact,
                    evidence=evidence,
                    route=route,
                    no_file_writes=no_file_writes,
                    skill_policy=skill_policy,
                ),
                roster,
                output_dir=output_dir,
            ),
            cwd=cwd,
            read_only=read_only,
            sandbox_read_only=sandbox_read_only,
            sandbox=sandbox,
            codex_transport=transport,
            process_registry=process_registry,
            model_lease=model_lease,
        )
        if not second.ok:
            _record_plan_attempt(attempts, stage="correction", result=second)
            raise RuntimeError(f"orchestrator failed during plan correction: {second.detail}") from exc
        try:
            assignments = _parse_gated_plan(
                second.text,
                roster,
                output_dir=output_dir,
                read_only=read_only,
                skill_policy=skill_policy,
            )
            _validate_plan_dependencies(route, assignments)
            _record_plan_attempt(
                attempts,
                stage="correction",
                result=second,
                parsed=True,
                coverage_missing=_coverage_missing(route, assignments),
                unknown_covers=_unknown_covers(route, assignments),
            )
        except ValueError as second_exc:
            _record_plan_attempt(
                attempts,
                stage="correction",
                result=second,
                parse_error=str(second_exc),
            )
            raise RuntimeError(f"orchestrator returned an invalid plan: {second_exc}") from second_exc

    missing = _coverage_missing(route, assignments)
    if not missing:
        return assignments
    # The plan parses but skips required route stages: one corrective retry, then
    # keep whichever plan covers more. Advisory, never fatal - a deterministic
    # constraint must not brick a run the orchestrator can finish.
    revised_result = _call_with_process_registry(
        _run_orchestrator,
        roster,
        _emit_plan_request(
            briefs.build_plan_prompt(
                task,
                roster,
                corrective_note=(
                    "the plan does not cover required route stages: "
                    + ", ".join(missing)
                    + '. Add or tag assignments with "covers" so every required stage is covered.'
                ),
                read_only=read_only,
                code_graph=code_graph,
                drift_impact=drift_impact,
                evidence=evidence,
                route=route,
                no_file_writes=no_file_writes,
                skill_policy=skill_policy,
            ),
            roster,
            output_dir=output_dir,
        ),
        cwd=cwd,
        read_only=read_only,
        sandbox_read_only=sandbox_read_only,
        sandbox=sandbox,
        codex_transport=transport,
        process_registry=process_registry,
        model_lease=model_lease,
    )
    if not revised_result.ok:
        _record_plan_attempt(attempts, stage="coverage-correction", result=revised_result)
        return assignments
    try:
        revised = _parse_gated_plan(
            revised_result.text,
            roster,
            output_dir=output_dir,
            read_only=read_only,
            skill_policy=skill_policy,
        )
        _validate_plan_dependencies(route, revised)
    except ValueError as exc:
        _record_plan_attempt(attempts, stage="coverage-correction", result=revised_result, parse_error=str(exc))
        return assignments
    revised_missing = _coverage_missing(route, revised)
    _record_plan_attempt(
        attempts,
        stage="coverage-correction",
        result=revised_result,
        parsed=True,
        coverage_missing=revised_missing,
        unknown_covers=_unknown_covers(route, revised),
    )
    # A revision that covers strictly more wins; anything else (including an
    # empty plan) keeps the original. Never trade assignments away for tags.
    if revised and len(revised_missing) < len(missing):
        return revised
    return assignments


def _render_prior_results(
    results: list[WorkerResult],
    *,
    run_id: str | None = None,
    to_seat: str | None = None,
) -> str:
    return "\n\n".join(
        "\n".join(
            [
                f"Worker: {result.worker}",
                f"Sub-task: {result.task}",
                f"Status: {'ok' if result.ok else 'failed'}",
                f"Detail: {result.detail}" if result.detail else "Detail:",
                "Output:",
                _admitted_result_output(result, run_id=run_id, to_seat=to_seat) or "(no output)",
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
    direct: bool = False,
    code_graph: briefs.CodeGraphBrief | None = None,
    drift_impact: briefs.DriftImpactBrief | None = None,
    evidence: briefs.EvidenceBrief | None = None,
    skill_policy: RoutePolicyDecision | None = None,
    run_id: str | None = None,
    to_seat: str | None = None,
) -> str:
    prior_context = ""
    if prior_results:
        prior_context = (
            f"\n\nEarlier-stage context:\n{_render_prior_results(prior_results, run_id=run_id, to_seat=to_seat)}"
        )
    policy = f"\n\n{_read_only_rules()}" if read_only else ""
    scope_policy = worker_skill_policy_constraint(skill_policy, assignment)
    tool_gate = ""
    if assignment.admissible_tool_ids:
        tool_ids = ", ".join(assignment.admissible_tool_ids)
        tool_gate = (
            "\n\nAdmissible portable tools for this step (CandidateSetGate): "
            f"{tool_ids}. Use only these catalog tool ids; do not improvise tools "
            "outside the admissible set."
        )
    elif assignment.domain or assignment.capabilities or assignment.max_risk_class:
        tool_gate = (
            "\n\nCandidateSetGate found no admissible portable tools for this step's "
            "declared domain/capability/risk requirements. Do not invent tools."
        )
    return_instruction = (
        "Return a concise, complete final user-visible result."
        if direct
        else "Return a concise, complete result for the orchestrator to synthesize."
    )
    prompt = (
        f"You are Brigade worker {agent.name}.\n"
        f"Role:\n{agent.role}\n\n"
        f"Sub-task:\n{assignment.task}\n\n"
        f"{return_instruction}"
        f"{prior_context}"
        f"{scope_policy}"
        f"{tool_gate}"
        f"{policy}"
    )
    return briefs._prepend_optional_briefs(prompt, code_graph=code_graph, drift_impact=drift_impact, evidence=evidence)


def _contains_event_marker(value: Any, marker: str) -> bool:
    if isinstance(value, str):
        return marker in value
    if isinstance(value, list):
        return any(_contains_event_marker(item, marker) for item in value)
    if isinstance(value, dict):
        return any(_contains_event_marker(item, marker) for item in value.values())
    return False


def _redact_event_marker(value: Any, marker: str) -> Any:
    if isinstance(value, str):
        return value.replace(marker, "[redacted]")
    if isinstance(value, list):
        return [_redact_event_marker(item, marker) for item in value]
    if isinstance(value, dict):
        return {key: _redact_event_marker(item, marker) for key, item in value.items()}
    return value


def _worker_event_writer(
    events_dir: Path | None,
    worker: str,
    *,
    verbose: bool = False,
    workspace: Path | None = None,
    correlation_marker: str | None = None,
):
    """Append lifecycle notifications to events/<worker>.jsonl; optionally narrate."""
    if events_dir is None and not verbose:
        return None
    path = None
    if events_dir is not None:
        events_dir.mkdir(parents=True, exist_ok=True)
        path = events_dir / f"{run_io._slug(worker)}.jsonl"

    def on_event(msg: dict) -> None:
        safe_msg = msg
        if correlation_marker is not None:
            params = msg.get("params") if isinstance(msg, dict) else None
            item = params.get("item") if isinstance(params, dict) else None
            if (
                msg.get("method") == "item/completed"
                and isinstance(item, dict)
                and item.get("type") == "commandExecution"
                and _contains_event_marker(msg, correlation_marker)
                and workspace is not None
            ):
                thread_id = params.get("threadId") if isinstance(params, dict) else None
                turn_id = params.get("turnId") if isinstance(params, dict) else None
                if isinstance(thread_id, str) and isinstance(turn_id, str):
                    from .. import runs_cmd

                    try:
                        runs_cmd._bind_approval_pause_request(
                            workspace,
                            correlation_marker=correlation_marker,
                            worker=worker,
                            thread_id=thread_id,
                            turn_id=turn_id,
                        )
                    except runs_cmd.ApprovalResumeError:
                        pass
            safe_msg = _redact_event_marker(msg, correlation_marker)
        if path is not None:
            with path.open("a") as fh:
                fh.write(proc.bound_text(json.dumps(safe_msg, default=str), proc.MAX_CAPTURE_BYTES) + "\n")
        if verbose and safe_msg.get("method") == "item/completed":
            item = (safe_msg.get("params") or {}).get("item") or {}
            print(f"worker {worker}: {item.get('type', 'item')} completed", file=sys.stderr)

    return on_event


def _run_codex_appserver_worker(
    appserver,
    agent: Agent,
    worker: str,
    prompt: str,
    *,
    timeout: float,
    cwd: Path | None,
    read_only: bool,
    sandbox: str | None,
    registry: run_control.LiveTurnRegistry | None,
    on_event=None,
) -> agents.AgentResult:
    effective_sandbox = sandbox if sandbox is not None else ("read-only" if read_only else None)
    active_turn_id: str | None = None
    try:
        thread = appserver.start_thread(cwd=cwd, model=agent.model, sandbox=effective_sandbox)

        def on_turn_start(turn_id: str) -> None:
            nonlocal active_turn_id
            active_turn_id = turn_id
            if registry is not None:
                registry.register(worker, thread, turn_id)

        try:
            turn_kwargs = {"timeout": timeout, "on_event": on_event, "on_turn_start": on_turn_start}
            if agent.reasoning is not None:
                turn_kwargs["effort"] = agent.reasoning
            turn = thread.run_turn(prompt, **turn_kwargs)
        except TypeError as exc:
            if "on_turn_start" not in str(exc):
                raise
            fallback_kwargs = {"timeout": timeout, "on_event": on_event}
            if agent.reasoning is not None:
                fallback_kwargs["effort"] = agent.reasoning
            turn = thread.run_turn(prompt, **fallback_kwargs)
    except codex_appserver.AppServerError as exc:
        return agents.AgentResult(
            text="",
            ok=False,
            detail=str(exc)[:200],
            status="failed",
            transport="codex-app-server",
            requested_model=agent.model,
            reasoning=agent.reasoning,
        )
    finally:
        if registry is not None and active_turn_id is not None:
            registry.unregister(worker, active_turn_id)
    text = turn.text.strip()
    if getattr(turn, "output_limit_exceeded", False) or len(text.encode("utf-8")) > proc.MAX_CAPTURE_BYTES:
        return agents.AgentResult(
            text=proc.bound_text(text),
            ok=False,
            detail=f"combined output exceeded {proc.MAX_CAPTURE_BYTES} byte limit"[:200],
            failure_phase="harness",
            failure_kind="output-limit",
            thread_id=turn.thread_id,
            status=turn.status,
            transport="codex-app-server",
            requested_model=agent.model,
            reasoning=agent.reasoning,
        )
    if not turn.ok:
        timed_out = bool(getattr(turn, "timed_out", False))
        failure_kind = "timeout" if timed_out else "interrupted" if turn.status == "interrupted" else None
        return agents.AgentResult(
            text=text,
            ok=False,
            detail=(turn.detail or f"turn {turn.status}")[:200],
            failure_kind=failure_kind,
            thread_id=turn.thread_id,
            status=turn.status,
            timed_out=timed_out,
            transport="codex-app-server",
            requested_model=agent.model,
            reasoning=agent.reasoning,
        )
    if not text:
        return agents.AgentResult(
            text="",
            ok=False,
            detail="empty output",
            thread_id=turn.thread_id,
            status=turn.status,
            transport="codex-app-server",
            requested_model=agent.model,
            reasoning=agent.reasoning,
        )
    output_failure = validate_final_output(text)
    if output_failure is not None:
        return agents.AgentResult(
            text=text,
            ok=False,
            detail=output_failure.detail,
            failure_phase="output-validation",
            failure_kind=output_failure.kind,
            thread_id=turn.thread_id,
            status=turn.status,
            transport="codex-app-server",
            requested_model=agent.model,
            reasoning=agent.reasoning,
        )
    return agents.AgentResult(
        text=text,
        ok=True,
        thread_id=turn.thread_id,
        status=turn.status,
        transport="codex-app-server",
        requested_model=agent.model,
        reasoning=agent.reasoning,
    )


def _candidate_set_replan_note(decision: Any) -> str:
    from .. import candidate_set as candidate_set_mod

    detail = candidate_set_mod.empty_required_failure_detail(decision)
    return (
        f"{detail}. Revise assignment domain, capabilities, and/or max_risk_class "
        "so at least one .brigade/tools.toml entry is admissible, or drop tool "
        "requirements when no portable tool is needed."
    )


def _apply_candidate_set_gate(
    *,
    cwd: Path,
    output_dir: Path,
    assignments: list[Assignment],
    roster: Roster,
    task: str,
    read_only: bool,
    sandbox_read_only: bool | None,
    sandbox: str | None,
    code_graph: Any,
    drift_impact: Any,
    evidence: Any,
    route: RouteBrief | None,
    skill_policy: Any,
    transport_for_payload: str | None,
    process_registry: Any,
    plan_attempts: list[dict[str, object]] | None,
    allow_replan: bool,
    model_lease: Callable[[Agent], ContextManager[str | None]] | None,
) -> tuple[list[Assignment], Any, str | None]:
    """Filter tools per assignment, write receipt, optionally replan once on empty sets.

    Returns ``(assignments, decision, failure_detail)``. ``failure_detail`` is set when
    a required step still has no admissible tool after the bounded replan.
    """
    from .. import candidate_set as candidate_set_mod

    decision = candidate_set_mod.evaluate_assignments(
        cwd,
        assignments,
        run_id=output_dir.name,
    )
    candidate_set_mod.write_candidate_set_receipt(output_dir, decision)
    if not decision.has_empty_required_steps:
        return candidate_set_mod.apply_admissible_tools(assignments, decision), decision, None

    if allow_replan:
        note = _candidate_set_replan_note(decision)
        if plan_attempts is not None:
            plan_attempts.append(
                {
                    "stage": "candidate-set-gate",
                    "ok": False,
                    "parsed": True,
                    "detail": note,
                    "failure_phase": candidate_set_mod.FAILURE_PHASE,
                    "failure_kind": candidate_set_mod.FAILURE_KIND,
                    "empty_required_steps": [
                        {
                            "stage": step.stage,
                            "worker": step.worker,
                            "task": step.task,
                            "requirements": step.requirements.payload(),
                        }
                        for step in decision.empty_required_steps
                    ],
                }
            )
        no_file_writes = _orchestrator_hides_write_tools(
            roster,
            read_only=read_only,
            sandbox_read_only=sandbox_read_only,
            sandbox=sandbox,
        )
        revised_result = _call_with_process_registry(
            _run_orchestrator,
            roster,
            _emit_plan_request(
                briefs.build_plan_prompt(
                    task,
                    roster,
                    corrective_note=note,
                    read_only=read_only,
                    code_graph=code_graph,
                    drift_impact=drift_impact,
                    evidence=evidence,
                    route=route,
                    no_file_writes=no_file_writes,
                    skill_policy=skill_policy,
                ),
                roster,
                output_dir=output_dir,
            ),
            cwd=cwd,
            read_only=read_only,
            sandbox_read_only=sandbox_read_only,
            sandbox=sandbox,
            codex_transport=transport_for_payload,
            process_registry=process_registry,
            model_lease=model_lease,
        )
        if revised_result.ok:
            try:
                revised = _parse_gated_plan(
                    revised_result.text,
                    roster,
                    output_dir=output_dir,
                    read_only=read_only,
                    skill_policy=skill_policy,
                )
                _validate_plan_dependencies(route, revised)
                _record_plan_attempt(
                    plan_attempts,
                    stage="candidate-set-correction",
                    result=revised_result,
                    parsed=True,
                    coverage_missing=_coverage_missing(route, revised),
                    unknown_covers=_unknown_covers(route, revised),
                )
                assignments = revised
                decision = candidate_set_mod.evaluate_assignments(
                    cwd,
                    assignments,
                    run_id=output_dir.name,
                )
                candidate_set_mod.write_candidate_set_receipt(output_dir, decision)
                run_io._write_json(
                    output_dir / "plan.json",
                    receipt_schema.run_plan_document(_assignment_payload(assignments)),
                )
            except ValueError as exc:
                _record_plan_attempt(
                    plan_attempts,
                    stage="candidate-set-correction",
                    result=revised_result,
                    parse_error=str(exc),
                )
        else:
            _record_plan_attempt(plan_attempts, stage="candidate-set-correction", result=revised_result)

    if decision.has_empty_required_steps:
        return (
            candidate_set_mod.apply_admissible_tools(assignments, decision),
            decision,
            candidate_set_mod.empty_required_failure_detail(decision),
        )
    return candidate_set_mod.apply_admissible_tools(assignments, decision), decision, None


def dispatch(
    assignments: list[Assignment],
    roster: Roster,
    cwd: Path | None = None,
    read_only: bool = False,
    sandbox_read_only: bool | None = None,
    sandbox: str | None = None,
    direct: bool = False,
    code_graph: briefs.CodeGraphBrief | None = None,
    drift_impact: briefs.DriftImpactBrief | None = None,
    evidence: briefs.EvidenceBrief | None = None,
    appserver=None,
    control_registry: run_control.LiveTurnRegistry | None = None,
    events_dir: Path | None = None,
    verbose: bool = False,
    authorized_writable_worktree: bool = False,
    fail_fast: bool = True,
    scheduler: str = "waves",
    route_dependencies: dict[str, tuple[str, ...]] | None = None,
    route_held: dict[str, list[str]] | None = None,
    on_stage_start: Callable[[int, tuple[str, ...]], None] | None = None,
    on_interrupt: Callable[[], None] | None = None,
    on_scheduler_resolved: Callable[[str, str | None], None] | None = None,
    on_dispatch_requested: Callable[[Agent], int | None] | None = None,
    on_dispatch_observed: Callable[[Agent, int], None] | None = None,
    on_dispatch_completed: Callable[[Agent, int], None] | None = None,
    on_dispatch_failed: Callable[[Agent, int], None] | None = None,
    process_registry: proc.ProcessRegistry | None = None,
    build_prompt: Callable[..., str] | None = None,
    quarantine_state: seat_health_policy.SeatQuarantineState | None = None,
    reprobe_seat: Callable[[Agent], bool] | None = None,
    on_failed_attempt_persisted: Callable[[WorkerResult], None] | None = None,
    run_id: str | None = None,
    output_dir: Path | None = None,
    model_lease: Callable[[Agent], ContextManager[str | None]] | None = None,
) -> list[WorkerResult]:
    from .. import run_transport

    bound_prompt = build_prompt
    if bound_prompt is None:
        bound_prompt = partial(
            _worker_prompt,
            run_id=run_id or (output_dir.name if output_dir is not None else message_envelope.IN_MEMORY_RUN_ID),
            to_seat=roster.orchestrator,
        )
    return run_transport.dispatch(
        assignments,
        roster,
        build_prompt=bound_prompt,
        run_appserver_worker=_run_codex_appserver_worker,
        event_writer=_worker_event_writer,
        cwd=cwd,
        read_only=read_only,
        sandbox_read_only=sandbox_read_only,
        sandbox=sandbox,
        direct=direct,
        code_graph=code_graph,
        drift_impact=drift_impact,
        evidence=evidence,
        appserver=appserver,
        control_registry=control_registry,
        events_dir=events_dir,
        verbose=verbose,
        authorized_writable_worktree=authorized_writable_worktree,
        fail_fast=fail_fast,
        scheduler=scheduler,
        route_dependencies=route_dependencies,
        route_held=route_held,
        on_stage_start=on_stage_start,
        on_interrupt=on_interrupt,
        on_scheduler_resolved=on_scheduler_resolved,
        on_dispatch_requested=on_dispatch_requested,
        on_dispatch_observed=on_dispatch_observed,
        on_dispatch_completed=on_dispatch_completed,
        on_dispatch_failed=on_dispatch_failed,
        process_registry=process_registry,
        quarantine_state=quarantine_state,
        reprobe_seat=reprobe_seat,
        on_failed_attempt_persisted=on_failed_attempt_persisted,
        run_id=run_id,
        output_dir=output_dir,
        model_lease=model_lease,
    )
