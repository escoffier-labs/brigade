"""Stage 1 compatibility seam for run receipts and artifact records."""
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
from typing import Any, Callable, Iterator, Mapping, Sequence
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

from . import briefs, run_io, planning, prompts
from . import orchestrator as _orchestrator_mod


def set_artifact_patch_ref(output_dir: Path, patch_ref: str = "changes.patch") -> None:
    for filename in ("worker-results.json", "synthesis.json"):
        path = output_dir / filename
        try:
            payload = json.loads(path.read_text())
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise runguard.RunGuardError(
                f"failed to read {filename} while recording artifact patch reference: {exc}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise runguard.RunGuardError(
                f"failed to parse {filename} while recording artifact patch reference: {exc}"
            ) from exc
        if not isinstance(payload, dict) or "ground_truth" not in payload:
            raise runguard.RunGuardError(f"{filename} is missing ground_truth while recording artifact patch reference")
        payload["ground_truth"] = prompts._with_patch_ref(payload.get("ground_truth"), patch_ref)
        if filename == "worker-results.json":
            receipt_schema.stamp_worker_results_document(payload)
        else:
            receipt_schema.stamp_synthesis_document(payload)
        try:
            run_io.write_sidecar_revision(output_dir, filename, payload)
        except OSError as exc:
            raise runguard.RunGuardError(f"failed to record artifact patch reference in {filename}: {exc}") from exc


def record_artifact_collection(
    output_dir: Path,
    *,
    status: str,
    patch_ref: str | None = None,
    changed: bool | None = None,
    tracked_count: int | None = None,
    untracked_count: int | None = None,
    worktree: Path | None = None,
    failure_phase: str | None = None,
    failure_kind: str | None = None,
    detail: str | None = None,
) -> None:
    run_path = output_dir / "run.json"
    try:
        payload = json.loads(run_path.read_text())
    except OSError as exc:
        raise runguard.RetainRunLockError(f"failed to read run receipt during artifact collection: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise runguard.RetainRunLockError(f"run receipt is invalid during artifact collection: {exc}") from exc
    if not isinstance(payload, dict):
        raise runguard.RetainRunLockError("run receipt must contain an object during artifact collection")

    collection: dict[str, object] = {"status": status}
    if patch_ref is not None:
        collection["patch_ref"] = patch_ref
    if changed is not None:
        collection["changed"] = changed
    if tracked_count is not None:
        collection["tracked_count"] = tracked_count
    if untracked_count is not None:
        collection["untracked_count"] = untracked_count
    if worktree is not None:
        collection["worktree"] = str(worktree)

    if status == "failed":
        bounded_detail = run_io._one_line(detail or "artifact collection failed")[:2000]
        phase = failure_phase or "artifact-collection"
        artifact_failure = {
            "phase": phase,
            "kind": failure_kind or "unknown",
            "detail": bounded_detail,
        }
        collection["failure"] = artifact_failure
        primary_status = payload.get("status")
        primary_terminal = isinstance(primary_status, str) and primary_status not in {
            "started",
            "planning",
            "dispatching",
            "result-processing",
            "synthesizing",
            "handoff",
            "artifact-collection",
            "running",
        }
        if not primary_terminal:
            payload["status"] = "failed"
            payload["error"] = bounded_detail
            payload["failure_phase"] = phase
            payload["failure"] = artifact_failure
            finished_at = datetime.now(timezone.utc)
            payload["status_started_at"] = run_io._utc_iso(finished_at)
            payload["finished_at"] = run_io._utc_iso(finished_at)
            started_at = payload.get("started_at")
            if isinstance(started_at, str):
                started = prompts._parse_iso_datetime(started_at)
                if started is not None:
                    payload["duration_seconds"] = max(
                        0.0,
                        round((finished_at - started).total_seconds(), 3),
                    )
    elif status == "ok" and payload.get("status") == "artifact-collection":
        finished_at = datetime.now(timezone.utc)
        payload["status"] = "ok"
        payload["status_started_at"] = run_io._utc_iso(finished_at)
        payload["finished_at"] = run_io._utc_iso(finished_at)
        started_at = payload.get("started_at")
        if isinstance(started_at, str):
            started = prompts._parse_iso_datetime(started_at)
            if started is not None:
                payload["duration_seconds"] = max(
                    0.0,
                    round((finished_at - started).total_seconds(), 3),
                )
    payload["artifact_collection"] = collection
    try:
        run_io._write_json(run_path, receipt_schema.stamp_run_receipt(payload))
    except (OSError, run_lifecycle.LifecycleJournalError, run_checkpoint.CheckpointError) as exc:
        raise runguard.RetainRunLockError(f"failed to update run receipt after artifact collection: {exc}") from exc


def record_run_termination(
    output_dir: Path,
    *,
    status: str,
    failure_phase: str | None,
    failure_kind: str,
    detail: str,
    seat: str | None = None,
    active_seats: tuple[str, ...] = (),
) -> None:
    """Terminalize a run that escaped its normal result path."""

    run_path = output_dir / "run.json"
    try:
        payload = json.loads(run_path.read_text())
    except OSError as exc:
        raise runguard.RetainRunLockError(f"failed to read run receipt during termination: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise runguard.RetainRunLockError(f"run receipt is invalid during termination: {exc}") from exc
    if not isinstance(payload, dict):
        raise runguard.RetainRunLockError("run receipt must contain an object during termination")
    if payload.get("finished_at"):
        return

    stored_status = payload.get("status")
    phase_owner = payload.get("phase_owner")
    if stored_status == "result-processing" and isinstance(phase_owner, str) and phase_owner:
        seat = phase_owner
        active_seats = ()
    phase = failure_phase or {
        "started": "startup",
        "planning": "planning",
        "dispatching": "dispatch",
        "result-processing": "result-processing",
        "synthesizing": "synthesis",
        "handoff": "handoff",
        "artifact-collection": "artifact-collection",
    }.get(stored_status if isinstance(stored_status, str) else "", "run")
    bounded_detail = run_io._one_line(detail or failure_kind)[:2000]
    failure: dict[str, object] = {
        "phase": phase,
        "kind": failure_kind,
        "detail": bounded_detail,
    }
    if seat:
        failure["seat"] = seat
    elif active_seats:
        failure["seats"] = list(active_seats)
    finished_at = datetime.now(timezone.utc)
    payload.update(
        {
            "status": status,
            "status_started_at": run_io._utc_iso(finished_at),
            "error": bounded_detail,
            "failure_phase": phase,
            "failure": failure,
            "finished_at": run_io._utc_iso(finished_at),
        }
    )
    started_at = payload.get("started_at")
    if isinstance(started_at, str):
        started = prompts._parse_iso_datetime(started_at)
        if started is not None:
            payload["duration_seconds"] = max(
                0.0,
                round((finished_at - started).total_seconds(), 3),
            )
    try:
        run_io._write_json(run_path, receipt_schema.stamp_run_receipt(payload))
    except (OSError, run_lifecycle.LifecycleJournalError, run_checkpoint.CheckpointError) as exc:
        raise runguard.RetainRunLockError(f"failed to write terminal run receipt: {exc}") from exc


def _initial_verification_budget(container: Mapping[str, Any]) -> tuple[bool, Mapping[str, Any] | None]:
    """Return a present, validated verification budget from an initial run artifact."""
    if "verification_contract" not in container:
        return False, None
    contract = container["verification_contract"]
    if not isinstance(contract, Mapping):
        raise run_budget.BudgetCompatibilityError(
            "verification_contract must be an object",
            code="schema_incompatible",
        )
    schema = contract.get("schema")
    if schema is not None and schema != verification_contract.VERIFICATION_CONTRACT_SCHEMA:
        raise run_budget.BudgetCompatibilityError(
            run_events._bound(f"unsupported verification_contract schema {schema!r}"),
            code="schema_incompatible",
        )
    version = contract.get("schema_version")
    if version is not None and (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version != verification_contract.VERIFICATION_CONTRACT_SCHEMA_VERSION
    ):
        raise run_budget.BudgetCompatibilityError(
            run_events._bound(f"unsupported verification_contract schema_version {version!r}"),
            code="schema_incompatible",
        )
    if "budget" not in contract:
        return False, None
    budget = contract["budget"]
    if not isinstance(budget, Mapping):
        raise run_budget.BudgetCompatibilityError(
            "verification_contract budget must be an object",
            code="schema_incompatible",
        )
    return True, budget


def _build_budget_coordinator(
    output_dir: Path,
    *,
    started_at: datetime,
    append_event: Callable[[str, Mapping[str, Any], str], Any],
) -> run_budget.BudgetCoordinator | None:
    """Build a budget coordinator when the run journal is active.

    Declaration sources (first match wins):
    1. ``run.json`` ``run_budget`` object
    2. ``run.json`` / plan ``verification_contract.budget`` (#500 bridge)
    3. Empty declaration (no enforceable ceilings; observed reconcile still
       works). Ordinary undeclared ``brigade run`` / dogfood / model-trial
       paths stay unbounded for backward compatibility.

    Returns ``None`` when the lifecycle journal is not enrolled so budget events
    are not appended into a missing journal.
    """
    run_path = output_dir / "run.json"
    try:
        raw = json.loads(run_path.read_text())
    except (OSError, json.JSONDecodeError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    if raw.get("lifecycle_journal_requested") is not True and not (output_dir / "events" / "lifecycle.jsonl").is_file():
        return None

    declaration: run_budget.RunBudgetDeclaration
    if "run_budget" in raw:
        declaration = run_budget.declaration_from_persisted_artifact(raw["run_budget"])
    else:
        has_budget, budget = _initial_verification_budget(raw)
        if not has_budget:
            # Also accept a sibling plan.json contract when present.
            plan_path = output_dir / "plan.json"
            try:
                plan_raw = json.loads(plan_path.read_text())
            except (OSError, json.JSONDecodeError):
                plan_raw = None
            if isinstance(plan_raw, dict):
                has_budget, budget = _initial_verification_budget(plan_raw)
        declaration = run_budget.declaration_from_verification_budget(budget)

    coordinator = run_budget.BudgetCoordinator(
        declaration=declaration,
        append_event=append_event,
    )
    coordinator.set_started_at(started_at)
    journal_path = output_dir / "events" / "lifecycle.jsonl"
    if journal_path.is_file():
        try:
            report = run_journal.read_journal_bounded(journal_path)
            if report.partial_tail is None and not report.chain_errors:
                coordinator.reload(report.events)
        except (OSError, run_journal.RunJournalError, run_events.CanonicalizationError):
            # Leave empty projection; append path will fail closed if the journal is broken.
            pass
    return coordinator


def _observed_budget_cancellation(
    process_registry: proc.ProcessRegistry,
    control_registry: run_control.LiveTurnRegistry | None,
    active_seats: tuple[str, ...],
) -> run_budget.CancellationReport:
    """Cancel both transports and preserve only observed per-seat state."""
    outcomes: list[run_budget.CancelOutcome] = []
    active: list[str] = []
    represented: set[str] = set()
    if control_registry is not None:
        interrupted, still_active = control_registry.interrupt_observed(active_seats)
        outcomes.extend(run_budget.CancelOutcome(seat, "interrupt", result) for seat, result in interrupted)
        represented.update(seat for seat, _result in interrupted)
        represented.update(still_active)
        active.extend(still_active)

    try:
        observed_processes = process_registry.cancel()
    except Exception:
        unobserved = tuple(seat for seat in active_seats if seat not in represented)
        outcomes.extend(run_budget.CancelOutcome(seat, "process_cancel", "error") for seat in unobserved)
        active.extend(unobserved)
        return run_budget.CancellationReport(
            outcomes=tuple(outcomes),
            active_seats=tuple(dict.fromkeys(active)),
        )

    process_results: dict[str, str] = {}
    priority = {"interrupted": 0, "still_active": 1, "error": 2}
    for process in observed_processes:
        seat = process.seat or "process"
        existing = process_results.get(seat)
        if existing is None or priority[process.result] > priority[existing]:
            process_results[seat] = process.result
    for seat, result in process_results.items():
        outcomes.append(run_budget.CancelOutcome(seat, "process_cancel", result))
        represented.add(seat)
        if result in {"still_active", "error"}:
            active.append(seat)

    for seat in active_seats:
        if seat not in represented:
            outcomes.append(run_budget.CancelOutcome(seat, "none", "unsupported"))
    return run_budget.CancellationReport(
        outcomes=tuple(outcomes),
        active_seats=tuple(dict.fromkeys(active)),
    )


def record_approval_pause(output_dir: Path, approval_reference: Mapping[str, Any]) -> None:
    """Commit an approval wait and refresh its compatibility snapshot.

    The caller must hold the matching normal run lock. Successful return is
    intentionally non-exceptional so the surrounding ``run_lock`` context
    releases ownership and the process can exit cleanly.
    """
    run_dir = output_dir.expanduser().resolve()
    run_path = run_dir / "run.json"
    try:
        raw = json.loads(run_path.read_text())
    except OSError as exc:
        raise runguard.RetainRunLockError(f"failed to read run receipt before approval pause: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise runguard.RetainRunLockError(f"run receipt is invalid before approval pause: {exc}") from exc
    if not isinstance(raw, dict):
        raise runguard.RetainRunLockError("run receipt must contain an object before approval pause")
    try:
        reference = run_lifecycle.normalize_approval_reference(approval_reference)
        workspace = runguard.resolve_run_lock_workspace(raw, run_dir)
        if workspace is None or not runguard.is_active_run_owner(workspace, run_dir):
            raise run_lifecycle.LifecycleJournalError("approval pause requires the active run lock for this run")
        run_lifecycle.prepare_lifecycle_journal(
            run_dir,
            workspace=workspace,
            incoming_snapshot=raw,
        )
        run_lifecycle.record_lifecycle_event(
            run_dir,
            event_type="approval.requested",
            payload={
                "approval_id": reference["approval_id"],
                "source": reference["source"],
                "contract_fingerprint": reference["contract_fingerprint"],
            },
            idempotency_key=run_lifecycle.approval_idempotency_key(reference["approval_id"], "requested"),
            workspace=workspace,
        )
        raw["status"] = "running"
        raw["status_started_at"] = run_io._utc_iso(datetime.now(timezone.utc))
        raw["approval_reference"] = {
            **reference,
            "decision_state": "pending",
        }
        raw.pop("finished_at", None)
        raw.pop("duration_seconds", None)
        run_io._write_json(run_path, receipt_schema.stamp_run_receipt(raw))
    except (OSError, run_lifecycle.LifecycleJournalError, run_checkpoint.CheckpointError) as exc:
        raise runguard.RetainRunLockError(f"failed to record approval pause: {exc}") from exc


def write_approval_resume_handoff(
    output_dir: Path,
    worker_results: list[WorkerResult],
    *,
    requester_worker: str,
    requester_thread_id: str,
) -> None:
    """Persist only the closed worker coordinates needed by approval resume."""
    matching = [
        result
        for result in worker_results
        if isinstance(result.thread_id, str)
        and bool(result.thread_id)
        and result.worker == requester_worker
        and result.thread_id == requester_thread_id
    ]
    if len(matching) != 1:
        raise runguard.RetainRunLockError("approval pause requester did not match exactly one worker thread")
    requester = matching[0]
    resumable = [
        {
            "worker": requester.worker,
            "task": requester.task,
            "ok": False,
            "thread_id": requester.thread_id,
            "status": "interrupted",
        }
    ]
    try:
        run_io.write_sidecar_revision(
            output_dir,
            "worker-results.json",
            receipt_schema.worker_results_document(resumable),
        )
    except (OSError, TypeError, ValueError) as exc:
        raise runguard.RetainRunLockError("failed to persist approval resume handoff") from exc


def record_dispatch_stage(output_dir: Path, *, stage: int, seats: tuple[str, ...]) -> None:
    """Record the currently executing dispatch stage without terminalizing it."""

    run_path = output_dir / "run.json"
    try:
        payload = json.loads(run_path.read_text())
    except OSError as exc:
        raise runguard.RetainRunLockError(f"failed to read run receipt during stage transition: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise runguard.RetainRunLockError(f"run receipt is invalid during stage transition: {exc}") from exc
    if not isinstance(payload, dict):
        raise runguard.RetainRunLockError("run receipt must contain an object during stage transition")
    if payload.get("finished_at"):
        return
    payload.update(
        {
            "status": "dispatching",
            "status_started_at": run_io._utc_iso(datetime.now(timezone.utc)),
            "active_stage": stage,
            "active_seats": list(seats),
        }
    )
    try:
        run_io._write_json(run_path, receipt_schema.stamp_run_receipt(payload))
    except (OSError, run_lifecycle.LifecycleJournalError, run_checkpoint.CheckpointError) as exc:
        raise runguard.RetainRunLockError(f"failed to write dispatch stage receipt: {exc}") from exc


def record_result_processing(output_dir: Path, *, seat: str | None = None) -> None:
    """Record aggregate post-dispatch result processing and clear worker ownership."""

    run_path = output_dir / "run.json"
    try:
        payload = json.loads(run_path.read_text())
    except OSError as exc:
        raise runguard.RetainRunLockError(f"failed to read run receipt during result processing: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise runguard.RetainRunLockError(f"run receipt is invalid during result processing: {exc}") from exc
    if not isinstance(payload, dict):
        raise runguard.RetainRunLockError("run receipt must contain an object during result processing")
    if payload.get("finished_at"):
        return
    payload.update({"status": "result-processing", "status_started_at": run_io._utc_iso(datetime.now(timezone.utc))})
    if seat is None:
        payload.pop("phase_owner", None)
    else:
        payload["phase_owner"] = seat
    payload.pop("active_stage", None)
    payload.pop("active_seats", None)
    try:
        run_io._write_json(run_path, receipt_schema.stamp_run_receipt(payload))
    except (OSError, run_lifecycle.LifecycleJournalError, run_checkpoint.CheckpointError) as exc:
        raise runguard.RetainRunLockError(f"failed to record result-processing phase: {exc}") from exc


def _roster_resolution_payload(roster: Roster) -> dict[str, object] | None:
    if roster.resolution is None:
        return None
    return {
        "path": str(roster.resolution.path),
        "source": roster.resolution.source,
        "shadowed": [str(path) for path in roster.resolution.shadowed],
    }


def _roster_payload(roster: Roster) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": receipt_schema.ROSTER_SNAPSHOT_SCHEMA,
        "schema_version": receipt_schema.ROSTER_SNAPSHOT_SCHEMA_VERSION,
        "orchestrator": roster.orchestrator,
        "max_workers": roster.max_workers,
        "timeout_seconds": roster.timeout_seconds,
        "allow_models": list(roster.allow_models),
        "sandbox": roster.sandbox,
        "agents": {
            name: {
                "cli": agent.cli,
                "command": list(agent.command) if agent.command is not None else None,
                "model": agent.model,
                "reasoning": agent.reasoning,
                "transport": agent.transport,
                "transport_version": agent.transport_version,
                "role": agent.role,
                "timeout_seconds": agent.timeout_seconds,
                "invalid_final_fallback": agent.invalid_final_fallback,
                "read_only_capable": agent.read_only_capable,
                "cloud_safe_mode": agent.cloud_safe_mode,
                # env tables hold names and references only, never secret
                # values (enforced at roster load), so persisting them for
                # resume is safe.
                "env": dict(agent.env) if agent.env is not None else None,
                "capabilities": list(agent.capabilities),
            }
            for name, agent in roster.agents.items()
        },
    }
    if resolution := _roster_resolution_payload(roster):
        payload["resolution"] = resolution
    return payload


def roster_payload_with_admission(
    roster: Roster,
    receipt: Mapping[str, Any],
) -> dict[str, object]:
    payload = _roster_payload(roster)
    admission = receipt.get("model_admission")
    if isinstance(admission, dict):
        payload["model_admission"] = dict(admission)
    admissions = receipt.get("admissions")
    if isinstance(admissions, list):
        payload["admissions"] = [dict(item) for item in admissions if isinstance(item, dict)]
    return payload


def _run_payload(
    *,
    task: str,
    cwd: Path | None,
    lock_workspace: Path | None,
    roster: Roster,
    dry_run: bool,
    read_only: bool,
    status: str,
    started_at: datetime,
    finished_at: datetime | None = None,
    output_dir: Path | None = None,
    handoff_path: Path | None = None,
    error: str | None = None,
    failure_phase: str | None = None,
    failure_kind: str | None = None,
    failure_seat: str | None = None,
    transport_warning: dict[str, object] | None = None,
    code_graph: briefs.CodeGraphBrief | None = None,
    drift_impact: briefs.DriftImpactBrief | None = None,
    evidence: briefs.EvidenceBrief | None = None,
    brief_set: briefs.BriefSet | None = None,
    codex_transport: str | None = None,
    control_socket: Path | None = None,
    control_transport: run_control.ControlTransport | None = None,
    code_graph_delta: dict[str, object] | None = None,
    context_eval_payload: dict[str, object] | None = None,
    suspected_noop: bool = False,
    route: RouteBrief | None = None,
    skill_route_policy: RoutePolicyDecision | None = None,
    worker: str | None = None,
    include_git: bool = True,
    pre_run_snapshot: dict[str, object] | None = None,
    scheduler: dict[str, object] | None = None,
    lifecycle_journal_requested: bool | None = None,
    run_journal_authority_requested: bool | None = None,
    health: dict[str, object] | None = None,
    worker_failure_summary: dict[str, object] | None = None,
    transport_routing: dict[str, object] | None = None,
    retry_decisions: Sequence[Mapping[str, Any]] | None = None,
    verification_contract_payload: Mapping[str, Any] | None = None,
    run_budget_payload: Mapping[str, Any] | None = None,
    kind: str = "work",
    causal_receipt_payload: Mapping[str, Any] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": receipt_schema.RUN_RECEIPT_SCHEMA,
        "schema_version": receipt_schema.RUN_RECEIPT_SCHEMA_VERSION,
        "kind": kind,
        "task": task,
        "orchestrator": roster.orchestrator,
        "dry_run": dry_run,
        "read_only": read_only,
        "status": status,
        "started_at": run_io._utc_iso(started_at),
        "status_started_at": run_io._utc_iso(started_at if status == "started" else datetime.now(timezone.utc)),
        "suspected_noop": suspected_noop,
        "code_graph_brief": {
            "attached": bool(code_graph.attached) if code_graph is not None else False,
            "bytes": code_graph.bytes if code_graph is not None else 0,
        },
        "drift_impact_brief": {
            "attached": bool(drift_impact.attached) if drift_impact is not None else False,
            "bytes": drift_impact.bytes if drift_impact is not None else 0,
            "pending_count": drift_impact.pending_count if drift_impact is not None else 0,
        },
        "evidence_brief": {
            "attached": bool(evidence.attached) if evidence is not None else False,
            "bytes": evidence.bytes if evidence is not None else 0,
        },
        "brief_budget": {
            "bytes": brief_set.budget_bytes if brief_set is not None else briefs.BRIEF_BUDGET_BYTES,
            "attached": list(brief_set.attached) if brief_set is not None else [],
        },
    }
    if cwd is not None:
        payload["cwd"] = str(cwd)
    if scheduler is not None:
        payload["scheduler"] = scheduler
    if lifecycle_journal_requested:
        payload["lifecycle_journal_requested"] = True
    if run_journal_authority_requested:
        payload["run_journal_authority_requested"] = True
    if resolution := _roster_resolution_payload(roster):
        payload["roster"] = resolution
    if roster.seat_routing:
        payload["seat_routing"] = [dict(decision) for decision in roster.seat_routing]
    if retry_decisions:
        payload["retry_decisions"] = [dict(entry) for entry in retry_decisions]
    if lock_workspace is not None:
        payload["lock_workspace"] = str(lock_workspace)
    if route is not None:
        payload["route"] = route.payload()
    if skill_route_policy is not None and skill_route_policy.policy_applied:
        from ..route_policy import route_policy_extensions_from_decision

        payload["skill_route_policy"] = route_policy_extensions_from_decision(skill_route_policy)
    if worker is not None:
        payload["worker"] = worker
    if include_git:
        git = prompts._receipt_git_snapshot(cwd)
        if git is not None:
            payload["git"] = git
    if pre_run_snapshot is not None:
        payload["pre_run_snapshot"] = pre_run_snapshot
    if code_graph_delta is not None:
        payload["code_graph_delta"] = code_graph_delta
    if context_eval_payload is not None:
        payload["context_eval"] = context_eval_payload
    if finished_at is not None:
        payload["finished_at"] = run_io._utc_iso(finished_at)
        payload["duration_seconds"] = max(0.0, round((finished_at - started_at).total_seconds(), 3))
    if output_dir is not None:
        payload["artifacts"] = str(output_dir)
    if handoff_path is not None:
        payload["handoff"] = str(handoff_path)
    if error is not None:
        payload["error"] = error
        if failure_phase is not None or failure_kind is not None:
            payload["failure_phase"] = failure_phase or "unknown"
            if failure_kind is not None:
                payload["failure_kind"] = failure_kind
            failure_payload: dict[str, object] = {
                "phase": failure_phase or "unknown",
                "kind": failure_kind or "unknown",
                "detail": error,
            }
            if failure_seat is not None:
                failure_payload["seat"] = failure_seat
            payload["failure"] = failure_payload
    if transport_warning is not None:
        payload["transport_warning"] = dict(transport_warning)
    if codex_transport is not None:
        payload["codex_transport"] = codex_transport
    if control_transport is not None:
        payload["control_transport"] = control_transport.to_metadata()
        if control_transport.kind == "unix" and control_transport.path:
            payload["control_socket"] = control_transport.path
    elif control_socket is not None:
        payload["control_socket"] = str(control_socket)
    if verification_contract_payload is not None:
        payload["verification_contract"] = dict(verification_contract_payload)
    if run_budget_payload is not None:
        payload["run_budget"] = dict(run_budget_payload)
    if isinstance(causal_receipt_payload, Mapping):
        payload["causal_receipt"] = dict(causal_receipt_payload)
    return seat_health_policy.apply_health_fields(
        payload,
        health=health,
        worker_failures=worker_failure_summary,
        transport_routing=transport_routing,
    )


def _direct_worker_error(worker: str, roster: Roster, *, read_only: bool = False) -> str | None:
    agent = roster.agents.get(worker)
    if agent is None:
        return f"unknown worker: {worker}"
    if worker == roster.orchestrator:
        return f"--worker cannot target orchestrator seat: {worker}"
    if read_only:
        capability_error = read_only_capability_error(agent)
        if capability_error is not None:
            return capability_error
    if agent.cli is None:
        return f"worker has no CLI adapter: {worker}"
    if not is_cli_allowed(agent.cli, roster):
        return f"{agent.cli} is not allowed by limits.allow_models"
    return None


def record_run_start(
    output_dir: Path,
    *,
    task: str,
    cwd: Path | None,
    roster: Roster,
    read_only: bool,
    worker: str | None = None,
    dry_run: bool = False,
    lock_workspace: Path | None = None,
    codex_transport: str | None = None,
    started_at: datetime | None = None,
    scheduler: str | None = None,
    verification_contract_payload: Mapping[str, Any] | None = None,
    run_budget_payload: Mapping[str, Any] | None = None,
    kind: str = "work",
) -> bool:
    """Write the minimal typed receipt needed before optional or blocking work.

    The requested scheduler is recorded here so a run that dies before dispatch
    still says what it was asked to do; ``used`` stays None until dispatch
    resolves it. ``record_run_termination`` merges into this payload, so the
    field survives a startup failure.
    """

    if run_budget_payload is not None:
        run_budget_payload = run_budget.validate_explicit_declaration(run_budget_payload)
    output_dir = output_dir.expanduser().resolve()
    started_at = started_at or datetime.now(timezone.utc)
    output_dir.mkdir(parents=True, exist_ok=True)
    run_json = output_dir / "run.json"
    run_json_exists = run_json.is_file()
    existing_lifecycle_requested = False
    existing_authority_requested = False
    existing_verification_contract: dict[str, Any] | None = None
    existing_run_budget: dict[str, Any] | None = None
    existing_kind: str | None = None
    existing_causal_receipt: dict[str, Any] | None = None
    existing_retry_decisions: list[dict[str, Any]] | None = None
    if run_json_exists:
        try:
            existing = json.loads(run_json.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError) as exc:
            raise runguard.RetainRunLockError(
                "existing run.json is unreadable as JSON; refusing to overwrite unknown durable enrollment state"
            ) from exc
        except OSError as exc:
            raise runguard.RetainRunLockError(
                "failed to read existing run.json; refusing to overwrite unknown durable enrollment state"
            ) from exc
        if not isinstance(existing, dict):
            raise runguard.RetainRunLockError(
                "existing run.json is not a JSON object; refusing to overwrite unknown durable enrollment state"
            )
        if existing.get("lifecycle_journal_requested") is True:
            existing_lifecycle_requested = True
        if existing.get(run_io._AUTHORITY_REQUEST_FIELD) is True:
            existing_authority_requested = True
        if isinstance(existing.get("verification_contract"), dict):
            existing_verification_contract = dict(existing["verification_contract"])
        if isinstance(existing.get("run_budget"), dict):
            existing_run_budget = dict(existing["run_budget"])
        if isinstance(existing.get("kind"), str) and existing["kind"].strip():
            existing_kind = existing["kind"].strip()
        if isinstance(existing.get("causal_receipt"), dict):
            existing_causal_receipt = dict(existing["causal_receipt"])
        existing_retry = existing.get("retry_decisions")
        if isinstance(existing_retry, list):
            existing_retry_decisions = [dict(entry) for entry in existing_retry if isinstance(entry, dict)]
    # Every new run carries both durable request fields. Existing runs enroll
    # only from their stored run.json fields, so legacy snapshot-only runs stay
    # untouched. An authority request implies lifecycle journaling even when an
    # older or repaired receipt retained only the authority field.
    new_run = not run_json_exists
    lifecycle_requested = existing_lifecycle_requested or existing_authority_requested or new_run
    authority_requested = existing_authority_requested or new_run
    contract_payload = (
        dict(verification_contract_payload)
        if isinstance(verification_contract_payload, Mapping)
        else existing_verification_contract
    )
    budget_payload = dict(run_budget_payload) if isinstance(run_budget_payload, Mapping) else existing_run_budget
    receipt_kind = kind if new_run or kind != "work" else (existing_kind or kind)
    # The first run.json write activates the lifecycle journal and publishes a
    # recovery checkpoint BEFORE the atomic run.json replacement. If that final
    # replacement fails, durable journal/checkpoint state already exists without
    # a run.json, so ``_terminalize_run_lifecycle.terminate_existing`` cannot
    # terminalize (run.json is absent) and the lock must be retained for
    # ``brigade runs recover``. Translate the bounded lifecycle write failures
    # to ``RetainRunLockError`` so ``runguard.run_lock`` keeps the lock instead
    # of releasing it and orphaning the recovery state.
    try:
        run_io._write_json(
            output_dir / "run.json",
            _run_payload(
                task=task,
                cwd=cwd,
                lock_workspace=lock_workspace if lock_workspace is not None else cwd,
                roster=roster,
                dry_run=dry_run,
                read_only=read_only,
                status="started",
                started_at=started_at,
                output_dir=output_dir,
                code_graph=briefs.CodeGraphBrief(attached=False),
                drift_impact=briefs.DriftImpactBrief(attached=False),
                evidence=briefs.EvidenceBrief(attached=False),
                codex_transport=codex_transport or roster.codex_transport,
                worker=worker,
                include_git=False,
                scheduler=(
                    {"requested": scheduler, "used": None, "fallback_reason": None} if scheduler is not None else None
                ),
                lifecycle_journal_requested=True if lifecycle_requested else None,
                run_journal_authority_requested=True if authority_requested else None,
                verification_contract_payload=contract_payload,
                run_budget_payload=budget_payload,
                kind=receipt_kind,
                causal_receipt_payload=(
                    existing_causal_receipt
                    if existing_causal_receipt is not None
                    else (causal_receipt.recorded_run(run_id=output_dir.name) if new_run else None)
                ),
                retry_decisions=existing_retry_decisions,
            ),
        )
    except (OSError, run_lifecycle.LifecycleJournalError, run_checkpoint.CheckpointError) as exc:
        raise runguard.RetainRunLockError(f"failed to write initial run receipt: {exc}") from exc
    run_io._write_json(output_dir / "roster.json", _roster_payload(roster))
    return lifecycle_requested or authority_requested


def update_run_receipt(output_dir: Path, **fields: object) -> dict[str, object]:
    path = output_dir.expanduser().resolve() / "run.json"
    current = briefs._read_json_dict(path)
    if current is None:
        raise FileNotFoundError(path)
    current.update(fields)
    run_io._write_json(path, current)
    return current
