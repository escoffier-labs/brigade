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

from .. import agents
from .. import codex_appserver
from .. import context_eval
from .. import evidence_brief as evidence_brief_mod
from .. import fleet_client
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

from . import briefs, run_io, planning, prompts, artifacts, model_admission


@dataclass(frozen=True)
class FleetModelPolicyResolution:
    roster: Roster
    receipt: dict[str, object]
    error: str | None = None


def _model_lease_lifecycle(function: Callable[..., int]) -> Callable[..., int]:
    """Freeze one startup policy snapshot without reserving unused seats."""

    def wrapped(task: str, roster: Roster, *args: Any, **kwargs: Any) -> int:
        snapshot = fleet_client.load_model_policy_snapshot()
        kwargs["model_policy_snapshot"] = snapshot
        return function(task, roster, *args, **kwargs)

    return wrapped


def resolve_fleet_model_policy(
    roster: Roster,
    *,
    worker: str | None = None,
    model_override: str | None = None,
    snapshot: Mapping[str, Any] | None = None,
) -> FleetModelPolicyResolution:
    """Resolve the frozen Fleet Hub model policy for this run."""
    from ..aboyeur_model_policy import resolve_fleet_model_policy as _resolve

    return _resolve(roster, worker=worker, model_override=model_override, snapshot=snapshot)


_roster_with_admission = model_admission.roster_payload


@contextmanager
def terminal_sigterm_handler(
    output_dir: Path | None,
    *,
    seat: str | None,
    before_record: Callable[[], None] | None = None,
    should_record: Callable[[], bool] | None = None,
) -> Iterator[None]:
    """Terminalize SIGTERM while preserving the conventional 128+signal exit."""

    if output_dir is None or threading.current_thread() is not threading.main_thread():
        yield
        return

    output_dir = output_dir.expanduser()
    previous = signal.getsignal(signal.SIGTERM)

    def handle_sigterm(signum: int, frame: object) -> None:  # noqa: ARG001
        if should_record is not None and not should_record():
            raise SystemExit(128 + signum)
        if before_record is not None:
            before_record()
        run_path = output_dir / "run.json"
        if run_path.is_file():
            active_seats: tuple[str, ...] = ()
            active_seat = seat
            try:
                payload = json.loads(run_path.read_text())
            except (OSError, json.JSONDecodeError):
                payload = None
            if isinstance(payload, dict):
                stored_active = payload.get("active_seats")
                if isinstance(stored_active, list):
                    active_seats = tuple(value for value in stored_active if isinstance(value, str) and value)
                    if len(active_seats) == 1:
                        active_seat = active_seats[0]
                    elif active_seats:
                        active_seat = None
                if not active_seats:
                    phase_owner = payload.get("phase_owner")
                    if isinstance(phase_owner, str) and phase_owner:
                        active_seat = phase_owner
                    else:
                        stored_worker = payload.get("worker")
                        if isinstance(stored_worker, str) and stored_worker:
                            active_seat = stored_worker
            artifacts.record_run_termination(
                output_dir,
                status="canceled",
                failure_phase=None,
                failure_kind="signal",
                detail="run terminated by SIGTERM",
                seat=active_seat,
                active_seats=active_seats,
            )
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, handle_sigterm)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def _terminalize_run_lifecycle(function: Callable[..., int]) -> Callable[..., int]:
    """Ensure every artifact-producing run leaves a terminal receipt on escape."""

    @wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> int:
        raw_output_dir = kwargs.get("output_dir")
        output_dir = raw_output_dir.expanduser() if isinstance(raw_output_dir, Path) else None
        raw_roster = args[1] if len(args) > 1 else kwargs.get("roster")
        raw_worker = kwargs.get("worker")
        seat = (
            raw_worker
            if isinstance(raw_worker, str) and raw_worker
            else raw_roster.orchestrator
            if isinstance(raw_roster, Roster)
            else None
        )

        def terminate_existing(
            *, status: str, failure_kind: str, detail: str, failure_phase: str | None = None
        ) -> None:
            if output_dir is None or not (output_dir / "run.json").is_file():
                return
            artifacts.record_run_termination(
                output_dir,
                status=status,
                failure_phase=failure_phase,
                failure_kind=failure_kind,
                detail=detail,
                seat=seat,
            )

        try:
            with terminal_sigterm_handler(output_dir, seat=seat):
                return function(*args, **kwargs)
        except runguard.RetainRunLockError:
            raise
        except KeyboardInterrupt:
            terminate_existing(
                status="canceled",
                failure_kind="keyboard-interrupt",
                detail="run canceled by user",
            )
            raise
        except TimeoutError as exc:
            terminate_existing(
                status="timeout",
                failure_kind="timeout",
                detail=run_io._one_line(str(exc)) or "run timed out",
            )
            raise
        except Exception as exc:
            terminate_existing(
                status="failed",
                failure_kind="unexpected-error",
                detail=f"{type(exc).__name__}: {run_io._one_line(str(exc)) or 'unexpected run failure'}",
            )
            raise

    return wrapped


@dataclass(frozen=True)
class OrchestratorHealthRoutingDecision:
    roster: Roster
    warning: str | None = None
    abort_detail: str | None = None
    abort_failure_phase: str = "preflight"
    abort_failure_kind: str = "unclassified"
    abort_seat: str | None = None
    receipt: dict[str, object] | None = None


def _seat_health_result_for(
    results: tuple[seat_health.SeatHealthResult, ...] | None,
    seat: str,
) -> seat_health.SeatHealthResult | None:
    if results is None:
        return None
    return next((result for result in results if result.seat == seat), None)


def _seat_health_typed_cause(result: seat_health.SeatHealthResult) -> str:
    if result.failure is not None:
        return result.failure.failure_class.value
    failed = next((check for check in result.checks if check.status == "failed"), None)
    if failed is not None and failed.cause_code:
        return failed.cause_code
    return "unclassified"


def resolve_orchestrator_health_routing(
    roster: Roster,
    results: tuple[seat_health.SeatHealthResult, ...] | None,
    *,
    pinned_seats: frozenset[str] = frozenset(),
    worker_seat: str | None = None,
) -> OrchestratorHealthRoutingDecision:
    """Route unhealthy orchestrator and worker seats to declared fallbacks.

    ``worker_seat`` marks a direct-worker run (``--worker``): only the selected
    worker and its declared fallbacks are health-gated, so an unused
    orchestrator can neither reroute nor abort the run.
    """
    if results is None:
        return OrchestratorHealthRoutingDecision(roster=roster)

    referenced = {name for agent in roster.agents.values() for name in agent.fallback}
    # Roots are the orchestrator plus seats not referenced as fallbacks by any other role.
    roots = [name for name in roster.agents if name == roster.orchestrator or name not in referenced]
    direct_worker = worker_seat is not None
    if direct_worker and worker_seat in roster.agents:
        # A pinned worker never dispatches through the orchestrator, so only the
        # worker seat chain is health-gated.
        roots = [worker_seat]
    effective_agents = dict(roster.agents)
    effective_orchestrator = roster.orchestrator
    decisions: list[dict[str, object]] = []
    warnings: list[str] = []
    abort: tuple[str, seat_health.SeatHealthResult, list[dict[str, str]]] | None = None

    for requested in roots:
        health_result = _seat_health_result_for(results, requested)
        if health_result is None or health_result.status != "unhealthy":
            continue
        typed_cause = _seat_health_typed_cause(health_result)
        rejected: list[dict[str, str]] = []
        selected: str | None = None
        for fallback_name in roster.agents[requested].fallback:
            fallback_result = _seat_health_result_for(results, fallback_name)
            if fallback_result is not None and fallback_result.status == "healthy":
                selected = fallback_name
                break
            status = fallback_result.status if fallback_result is not None else "missing"
            cause = _seat_health_typed_cause(fallback_result) if fallback_result is not None else "missing"
            rejected.append({"seat": fallback_name, "status": status, "cause": cause})

        decision: dict[str, object] = {
            "requested_seat": requested,
            "effective_seat": selected,
            "outcome": "fallback" if selected is not None else "skip",
            "typed_cause": typed_cause,
            "rejected_fallbacks": rejected,
        }
        decisions.append(decision)
        if selected is not None:
            if requested == roster.orchestrator:
                effective_orchestrator = selected
                warnings.append(
                    f"warning: orchestrator {requested} is unhealthy [{typed_cause}]; "
                    f"using declared fallback {selected}"
                )
            else:
                fallback = roster.agents[selected]
                requested_agent = roster.agents[requested]
                effective_agents[requested] = replace(
                    fallback,
                    name=requested,
                    role=requested_agent.role,
                    purpose=requested_agent.purpose,
                    fallback=(),
                )
                warnings.append(
                    f"warning: seat {requested} is unhealthy [{typed_cause}]; using declared fallback {selected}"
                )
        elif requested == roster.orchestrator or requested in pinned_seats:
            abort = (requested, health_result, rejected)
            decision["outcome"] = "abort"
        else:
            effective_agents.pop(requested, None)
            warnings.append(f"warning: skipping unhealthy seat {requested} [{typed_cause}]; no healthy fallback")

    if not decisions:
        return OrchestratorHealthRoutingDecision(roster=roster)

    effective = replace(
        roster,
        agents=effective_agents,
        orchestrator=effective_orchestrator,
        seat_routing=tuple(decisions),
    )
    receipt: dict[str, object] = {"schema": "brigade.seat_routing.v1", "decisions": decisions}
    if direct_worker:
        receipt["run_mode"] = "direct-worker"
    orchestrator_decision = next(
        (decision for decision in decisions if decision["requested_seat"] == roster.orchestrator), None
    )
    if orchestrator_decision is not None:
        # Preserve the original v1 summary for existing artifact consumers.
        receipt.update(
            {
                "requested_orchestrator": roster.orchestrator,
                "effective_orchestrator": orchestrator_decision["effective_seat"],
                "outcome": orchestrator_decision["outcome"],
                "typed_cause": orchestrator_decision["typed_cause"],
                "rejected_fallbacks": orchestrator_decision["rejected_fallbacks"],
            }
        )

    if abort is None:
        return OrchestratorHealthRoutingDecision(
            roster=effective,
            warning="\n".join(warnings) or None,
            receipt=receipt,
        )

    requested, unhealthy_result, rejected = abort
    typed_cause = _seat_health_typed_cause(unhealthy_result)
    if rejected:
        rejected_detail = "; rejected fallbacks: " + ", ".join(
            f"{entry['seat']} ({entry['status']}[{entry['cause']}])" for entry in rejected
        )
    else:
        rejected_detail = "; no declared fallbacks"
    if requested == roster.orchestrator:
        detail = f"orchestrator {requested} is unhealthy [{typed_cause}]{rejected_detail}"
    else:
        detail = f"seat {requested} is unhealthy [{typed_cause}]{rejected_detail}"
    failure = unhealthy_result.failure
    return OrchestratorHealthRoutingDecision(
        roster=effective,
        warning="\n".join(warnings) or None,
        abort_detail=detail,
        abort_failure_phase=failure.phase.value if failure is not None else "preflight",
        abort_failure_kind=failure.failure_class.value if failure is not None else "unclassified",
        abort_seat=requested,
        receipt=receipt,
    )


def _write_seat_routing_receipt(output_dir: Path, receipt: dict[str, object]) -> None:
    try:
        run_io._write_json(output_dir / "seat-routing.json", receipt)
    except OSError as exc:
        print(f"error: seat routing receipt failed: {exc}", file=sys.stderr)


def _write_run_seat_health_receipt(
    output_dir: Path,
    roster: Roster,
    *,
    cwd: Path | None = None,
    probe: seat_health.SeatHealthProbe | None = None,
    require_hard_isolation: bool = False,
    sandbox: str | None = None,
) -> tuple[seat_health.SeatHealthResult, ...] | None:
    """Probe declared seats and write seat-health.json beside run.json.

    Returns probe results on success, including when the receipt write fails.
    Returns ``None`` when the probe itself raised so routing treats the run as
    observation-only.  ``allow_model_smoke`` stays off: paying for a model call
    per seat on every run is a routing decision that does not belong to
    admission.
    """
    active_probe = probe or seat_health.SeatHealthProbe(collect_executable_version=False)
    probe_failed = False
    try:
        results = active_probe.probe_roster(
            roster,
            workspace=cwd,
            allow_model_smoke=False,
            require_hard_isolation=require_hard_isolation,
            sandbox=sandbox,
        )
    except Exception as exc:
        probe_failed = True
        results = seat_health.exception_results_for_probe_failure(roster, exc)
    try:
        seat_health.write_seat_health_receipt(
            output_dir / "seat-health.json",
            results,
            run_id=output_dir.name,
        )
    except OSError as exc:
        print(f"error: seat health receipt failed: {exc}", file=sys.stderr)
    if probe_failed:
        return None
    return results


@_terminalize_run_lifecycle
@_model_lease_lifecycle
def run(
    task: str,
    roster: Roster,
    *,
    dry_run: bool = False,
    show_plan: bool = False,
    verbose: bool = False,
    cwd: Path | None = None,
    lock_workspace: Path | None = None,
    output_dir: Path | None = None,
    handoff_inbox: Path | None = None,
    read_only: bool = False,
    sandbox_read_only: bool | None = None,
    sandbox: str | None = None,
    code_graph_enabled: bool = True,
    evidence_enabled: bool = True,
    code_graph: briefs.CodeGraphBrief | None = None,
    drift_impact: briefs.DriftImpactBrief | None = None,
    evidence: briefs.EvidenceBrief | None = None,
    codex_transport: str | None = None,
    route_enabled: bool = True,
    route_approvals: tuple[str, ...] = (),
    route_template: str | None = None,
    route_overrides: tuple[str, ...] = (),
    worker: str | None = None,
    model_override: str | None = None,
    allow_shadow: bool = False,
    authorized_writable_worktree: bool = False,
    fail_fast: bool = True,
    scheduler: str = "waves",
    defer_artifact_collection: bool = False,
    verification_contract_payload: Mapping[str, Any] | None = None,
    run_budget_payload: Mapping[str, Any] | None = None,
    model_policy_snapshot: Mapping[str, Any] | None = None,
) -> int:
    if run_budget_payload is not None:
        run_budget_payload = run_budget.validate_explicit_declaration(run_budget_payload)
    started_at = datetime.now(timezone.utc)
    process_registry = proc.ProcessRegistry()
    transport_for_payload = codex_transport or roster.codex_transport
    cwd = cwd.expanduser().resolve() if cwd is not None else None
    lock_workspace = lock_workspace.expanduser().resolve() if lock_workspace is not None else cwd
    output_dir = output_dir.expanduser() if output_dir is not None else None
    handoff_inbox = handoff_inbox.expanduser() if handoff_inbox is not None else None
    direct_worker = worker is not None
    admission_coordinator = model_admission.RuntimeAdmissionCoordinator.for_run(output_dir)
    # Judge the command-line sandbox override rather than the static declaration.
    effective_sandbox = sandbox if sandbox is not None else ("read-only" if read_only else None)
    durable_enrollment_expected = False
    # Keep requested and effective scheduler state distinct until dispatch.
    scheduler_resolution: dict[str, object] = {
        "requested": scheduler,
        "used": None,
        "fallback_reason": None,
    }
    health_summary_payload: dict[str, object] | None = None
    transport_routing_payload: dict[str, object] | None = None
    quarantine_state = seat_health_policy.SeatQuarantineState()
    active_health_probe = seat_health.SeatHealthProbe(collect_executable_version=False)
    model_policy = resolve_fleet_model_policy(
        roster,
        worker=worker,
        model_override=model_override,
        snapshot=model_policy_snapshot,
    )
    roster = model_policy.roster
    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        run_io._write_json(output_dir / "model-policy.json", model_policy.receipt)
        run_path = output_dir / "run.json"
        if roster.seat_routing and run_path.is_file():
            artifacts.update_run_receipt(
                output_dir,
                seat_routing=[dict(decision) for decision in roster.seat_routing],
            )
        run_io._write_json(output_dir / "roster.json", _roster_with_admission(roster, model_policy.receipt))
    if model_policy.error is not None:
        policy_error = model_policy.error
        if output_dir is not None and not (output_dir / "run.json").is_file():
            artifacts.record_run_start(
                output_dir,
                task=task,
                cwd=cwd,
                roster=roster,
                read_only=read_only,
                worker=worker,
                dry_run=dry_run,
                lock_workspace=lock_workspace,
                codex_transport=transport_for_payload,
                started_at=started_at,
                scheduler=scheduler,
                verification_contract_payload=verification_contract_payload,
                run_budget_payload=run_budget_payload,
                handoff_inbox=handoff_inbox,
            )
        if output_dir is not None and (output_dir / "run.json").is_file():
            artifacts.record_run_termination(
                output_dir,
                status="failed",
                failure_phase="preflight",
                failure_kind="fleet-model-policy",
                detail=policy_error,
                seat=worker or roster.orchestrator,
            )
        print(f"error: {policy_error}", file=sys.stderr)
        return 2

    @contextmanager
    def lease_model_agent(agent: Agent) -> Iterator[str | None]:
        nonlocal roster
        if model_policy.receipt.get("state") != "authoritative":
            yield None
            return
        runtime = admission_coordinator.admit(agent.name, model_policy.receipt)
        if runtime.target is not None:
            roster = admission_coordinator.attach_target(roster, agent.name, runtime.target)
        if runtime.records and output_dir is not None:
            model_admission.persist(output_dir, roster, model_policy.receipt)
        if runtime.error is not None:
            yield f"fleet model policy denied seat {agent.name!r}: {runtime.error}"
            return
        decision = fleet_client.acquire_model_lease(
            agent.name,
            agents.model_policy_provider(agent.cli or ""),
            agents.model_policy_model(agent.cli or "", agent.model),
        )
        if not decision.granted:
            yield f"fleet model policy denied seat {agent.name!r}: {decision.reason}"
            return
        try:
            yield None
        finally:
            if decision.lease_id is not None and decision.holder is not None:
                fleet_client.release_model_lease(decision.lease_id, holder=decision.holder)

    def scheduler_resolved(used: str, fallback_reason: str | None) -> None:
        scheduler_resolution["used"] = used
        scheduler_resolution["fallback_reason"] = fallback_reason

    def _payload(**kwargs: Any) -> dict[str, object]:
        if "handoff_inbox" not in kwargs and handoff_inbox is not None:
            kwargs["handoff_inbox"] = handoff_inbox
        if "skill_route_policy" not in kwargs and skill_policy is not None:
            kwargs["skill_route_policy"] = skill_policy
        if "retry_decisions" not in kwargs and quarantine_state.retry_decisions:
            kwargs["retry_decisions"] = [dict(entry) for entry in quarantine_state.retry_decisions]
        if health_summary_payload is not None and "health" not in kwargs:
            kwargs["health"] = health_summary_payload
        if transport_routing_payload is not None and "transport_routing" not in kwargs:
            kwargs["transport_routing"] = transport_routing_payload
        if output_dir is not None and (
            "lifecycle_journal_requested" not in kwargs or "run_journal_authority_requested" not in kwargs
        ):
            run_path = output_dir / "run.json"
            try:
                run_info = os.lstat(run_path)
            except FileNotFoundError as exc:
                if durable_enrollment_expected:
                    raise runguard.RetainRunLockError("refusing to overwrite unknown durable enrollment state") from exc
                run_info = None
            except OSError as exc:
                if durable_enrollment_expected:
                    raise runguard.RetainRunLockError("refusing to overwrite unknown durable enrollment state") from exc
                run_info = None
            if run_info is not None and (not os.path.isfile(run_path) or os.path.islink(run_path)):
                if durable_enrollment_expected:
                    raise runguard.RetainRunLockError("refusing to overwrite unknown durable enrollment state")
            elif run_info is not None:
                try:
                    existing = json.loads(run_path.read_text())
                except (OSError, ValueError, RecursionError) as exc:
                    if durable_enrollment_expected:
                        raise runguard.RetainRunLockError(
                            "refusing to overwrite unknown durable enrollment state"
                        ) from exc
                    existing = None
                if not isinstance(existing, dict) and durable_enrollment_expected:
                    raise runguard.RetainRunLockError("refusing to overwrite unknown durable enrollment state")
                if isinstance(existing, dict):
                    if (
                        "lifecycle_journal_requested" not in kwargs
                        and existing.get("lifecycle_journal_requested") is True
                    ):
                        kwargs["lifecycle_journal_requested"] = True
                    if (
                        "run_journal_authority_requested" not in kwargs
                        and existing.get("run_journal_authority_requested") is True
                    ):
                        kwargs["run_journal_authority_requested"] = True
                    if "health" not in kwargs and isinstance(existing.get("health"), dict):
                        kwargs["health"] = dict(existing["health"])
                    if "transport_routing" not in kwargs and isinstance(existing.get("transport_routing"), dict):
                        kwargs["transport_routing"] = dict(existing["transport_routing"])
                    if "verification_contract_payload" not in kwargs and isinstance(
                        existing.get("verification_contract"), dict
                    ):
                        kwargs["verification_contract_payload"] = dict(existing["verification_contract"])
                    if "run_budget_payload" not in kwargs and isinstance(existing.get("run_budget"), dict):
                        kwargs["run_budget_payload"] = dict(existing["run_budget"])
                    if "kind" not in kwargs and isinstance(existing.get("kind"), str) and existing["kind"].strip():
                        kwargs["kind"] = existing["kind"].strip()
                    if "handoff_inbox" not in kwargs and isinstance(existing.get("handoff_inbox"), str):
                        kwargs["handoff_inbox"] = existing["handoff_inbox"]
                    if "causal_receipt_payload" not in kwargs and isinstance(existing.get("causal_receipt"), dict):
                        kwargs["causal_receipt_payload"] = dict(existing["causal_receipt"])
                    existing_retry = existing.get("retry_decisions")
                    if isinstance(existing_retry, list):
                        prior = [dict(entry) for entry in existing_retry if isinstance(entry, dict)]
                        fresh = [
                            dict(entry) for entry in (kwargs.get("retry_decisions") or []) if isinstance(entry, dict)
                        ]
                        seen = {json.dumps(entry, sort_keys=True) for entry in prior}
                        kwargs["retry_decisions"] = prior + [
                            entry for entry in fresh if json.dumps(entry, sort_keys=True) not in seen
                        ]
        return artifacts._run_payload(
            lock_workspace=lock_workspace,
            pre_run_snapshot=pre_run_snapshot_payload,
            scheduler=dict(scheduler_resolution),
            **kwargs,
        )

    skill_policy: RoutePolicyDecision | None = None

    # Capture pre-run git state before any worker touches the tree so ground
    # truth can attribute only the worker's changes and a drift check can fail
    # the run if branch or HEAD moves out from under it. The in-memory snapshot
    # is captured now (before dispatch); the persisted copy is written once
    # output_dir exists. A git work tree whose state cannot be read is a
    # preflight failure (not a silent downgrade): the run refuses to start
    # rather than running without isolation attribution.
    try:
        pre_run_snapshot = runguard.capture_pre_run_snapshot(cwd)
        # Branch/HEAD drift is checked against the resolved lock workspace, which
        # may be a separate canonical checkout from the assigned cwd worktree.
        # The cwd snapshot stays the ground-truth baseline for worker change
        # attribution; a separate drift snapshot is captured from lock_workspace
        # before dispatch when it differs, so movement of the canonical checkout
        # is detected even though the assigned worktree never moved. When they
        # are the same path, reuse the cwd snapshot to preserve explicit
        # canonical-write behavior.
        if lock_workspace != cwd:
            drift_snapshot = runguard.capture_pre_run_snapshot(lock_workspace)
        else:
            drift_snapshot = pre_run_snapshot
    except runguard.RunGuardError as exc:
        print(f"error: pre-run snapshot failed: {exc}", file=sys.stderr)
        return 2
    pre_run_snapshot_payload = runguard.snapshot_payload(pre_run_snapshot)

    def _drift_failure_rc() -> int | None:
        """Centralized branch/HEAD drift check across every return path.

        Returns 2 (and records a run-isolation failure) when branch or HEAD
        moved since the drift snapshot taken from the lock workspace, else
        None so the caller can proceed. Call this after planning, before any
        dry-run return, after worker dispatch, after synthesis, and
        immediately before finalization.
        """
        detail = runguard.detect_branch_head_drift(lock_workspace, drift_snapshot)
        if detail is None:
            return None
        if output_dir is not None:
            artifacts.record_run_termination(
                output_dir,
                status="failed",
                failure_phase="run-isolation",
                failure_kind="branch-head-drift",
                detail=detail,
                seat=roster.orchestrator,
            )
        print(f"error: {detail}", file=sys.stderr)
        return 2

    def _run_isolation_check() -> str | None:
        """Detect canonical-checkout escapes after dispatch without per-worker attribution."""

        if lock_workspace == cwd or drift_snapshot is None:
            return None
        # Preserve the existing branch/HEAD drift classification. Working-tree
        # mutations are what this post-dispatch check adds; committed or
        # branch-moving interference remains caught by the run-level drift
        # checkpoints.
        if runguard.detect_branch_head_drift(lock_workspace, drift_snapshot) is not None:
            return None
        try:
            changed, untracked = runguard.changes_relative_to_snapshot(lock_workspace, drift_snapshot)
        except runguard.RunGuardError as exc:
            return f"isolation breach: could not verify canonical checkout: {exc}"
        escaped = sorted(set(changed + untracked))
        if not escaped:
            return None
        shown = ", ".join(escaped[:10])
        if len(escaped) > 10:
            shown += f", and {len(escaped) - 10} more"
        return f"isolation breach: modified files outside assigned worktree: {shown}"

    if worker is not None:
        worker_error = artifacts._direct_worker_error(worker, roster, read_only=read_only)
        if worker_error is not None:
            print(f"error: {worker_error}", file=sys.stderr)
            return 2
    if output_dir is not None:
        durable_enrollment_expected = artifacts.record_run_start(
            output_dir,
            task=task,
            cwd=cwd,
            roster=roster,
            read_only=read_only,
            worker=worker,
            dry_run=dry_run,
            lock_workspace=lock_workspace,
            codex_transport=transport_for_payload,
            started_at=started_at,
            scheduler=scheduler,
            verification_contract_payload=verification_contract_payload,
            run_budget_payload=run_budget_payload,
            handoff_inbox=handoff_inbox,
        )
        run_io._write_json(output_dir / "roster.json", _roster_with_admission(roster, model_policy.receipt))
    if code_graph is None:
        code_graph = briefs.code_graph_brief(cwd, task) if code_graph_enabled else briefs.CodeGraphBrief(attached=False)
    if drift_impact is None:
        drift_impact = briefs.drift_impact_brief(cwd) if code_graph_enabled else briefs.DriftImpactBrief(attached=False)
    if evidence is None:
        evidence = (
            evidence_brief_mod.evidence_brief(cwd, task)
            if code_graph_enabled and evidence_enabled
            else briefs.EvidenceBrief(attached=False)
        )
    brief_set = briefs.arbitrate_briefs(task, code_graph=code_graph, drift_impact=drift_impact, evidence=evidence)
    code_graph = brief_set.code_graph
    drift_impact = brief_set.drift_impact
    evidence = brief_set.evidence
    route = (
        route_brief(
            task,
            template=route_template,
            changed_paths=prompts._route_changed_paths(cwd),
            approvals=route_approvals,
            overrides=route_overrides,
        )
        if route_enabled
        else None
    )
    code_graph_delta = prompts._initial_code_graph_delta(
        code_graph_enabled=code_graph_enabled,
        dry_run=dry_run,
        read_only=read_only,
        cwd=cwd,
    )
    code_graph_delta_before: dict[str, object] | None = None
    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        if pre_run_snapshot_payload is not None:
            run_io._write_json(output_dir / "pre-run-snapshot.json", pre_run_snapshot_payload)
        if code_graph_delta is None and cwd is not None:
            code_graph_delta_before = graphtrail_delta.capture_before(cwd, output_dir)
            code_graph_delta = code_graph_delta_before
        run_io._write_json(output_dir / "roster.json", _roster_with_admission(roster, model_policy.receipt))
        run_io._write_json(
            output_dir / "run.json",
            _payload(
                task=task,
                cwd=cwd,
                roster=roster,
                dry_run=dry_run,
                read_only=read_only,
                status="started",
                started_at=started_at,
                output_dir=output_dir,
                code_graph=code_graph,
                drift_impact=drift_impact,
                evidence=evidence,
                brief_set=brief_set,
                codex_transport=transport_for_payload,
                route=route,
                code_graph_delta=code_graph_delta,
                worker=worker,
            ),
        )
        # After the initial run receipt, before planning: a probe failure stays receipted.
        health_results = _write_run_seat_health_receipt(
            output_dir,
            roster,
            cwd=cwd,
            probe=active_health_probe,
            require_hard_isolation=read_only,
            sandbox=effective_sandbox,
        )
        routing = resolve_orchestrator_health_routing(
            roster,
            health_results,
            pinned_seats=frozenset({worker}) if worker is not None else frozenset(),
            worker_seat=worker,
        )
        roster = routing.roster
        health_summary_payload = seat_health_policy.seat_health_summary(
            health_results,
            routing_decisions=roster.seat_routing,
        )
        # Record the effective sandbox the preflight judged. The run receipt's
        # field inventory is closed, so the override lives under the preserved
        # health payload rather than as a new top-level run.json key.
        if isinstance(health_summary_payload, dict):
            health_summary_payload["effective_sandbox"] = effective_sandbox
        if routing.warning is not None:
            print(routing.warning, file=sys.stderr)
        if worker is not None and roster.seat_routing:
            for decision in roster.seat_routing:
                if decision.get("requested_seat") != worker or decision.get("outcome") != "fallback":
                    continue
                effective = decision.get("effective_seat")
                cause = decision.get("typed_cause") or "unclassified"
                if isinstance(effective, str):
                    print(
                        seat_health_policy.format_worker_seat_resolution(
                            requested=worker,
                            effective=effective,
                            typed_cause=str(cause),
                        ),
                        file=sys.stderr,
                    )
                break
        if routing.receipt is not None:
            routing_receipt = dict(routing.receipt)
            routing_receipt["run_id"] = output_dir.name
            _write_seat_routing_receipt(output_dir, routing_receipt)
        if routing.abort_detail is not None:
            if roster.seat_routing or health_summary_payload is not None:
                run_path = output_dir / "run.json"
                try:
                    run_payload = json.loads(run_path.read_text())
                except (OSError, json.JSONDecodeError) as exc:
                    raise runguard.RetainRunLockError(
                        f"failed to read run receipt before seat-routing abort: {exc}"
                    ) from exc
                if isinstance(run_payload, dict):
                    if roster.seat_routing:
                        run_payload["seat_routing"] = [dict(decision) for decision in roster.seat_routing]
                    if health_summary_payload is not None:
                        run_payload["health"] = dict(health_summary_payload)
                    run_io._write_json(run_path, receipt_schema.stamp_run_receipt(run_payload))
            artifacts.record_run_termination(
                output_dir,
                status="failed",
                failure_phase=routing.abort_failure_phase,
                failure_kind=routing.abort_failure_kind,
                detail=routing.abort_detail,
                seat=routing.abort_seat or roster.orchestrator,
            )
            print(f"error: {routing.abort_detail}", file=sys.stderr)
            return 2

    if cwd is not None and route is not None and route.attached:
        from ..route_policy import decide_route_skills, route_policy_extensions_from_decision
        from ..route_receipts import write_route_decision

        skill_policy = decide_route_skills(
            cwd,
            route_brief=route,
            runs_dir=output_dir.parent if output_dir is not None else None,
            now=started_at,
            allow_shadow=allow_shadow if direct_worker else True,
        )
        if output_dir is not None and skill_policy.policy_applied:
            write_route_decision(
                output_dir,
                roster,
                runs_dir=output_dir.parent,
                policy_extensions=route_policy_extensions_from_decision(skill_policy),
            )
            run_io._write_json(
                output_dir / "run.json",
                _payload(
                    task=task,
                    cwd=cwd,
                    roster=roster,
                    dry_run=dry_run,
                    read_only=read_only,
                    status="started",
                    started_at=started_at,
                    output_dir=output_dir,
                    code_graph=code_graph,
                    drift_impact=drift_impact,
                    evidence=evidence,
                    brief_set=brief_set,
                    codex_transport=transport_for_payload,
                    route=route,
                    code_graph_delta=code_graph_delta,
                    worker=worker,
                ),
            )

    control_socket = None
    control_transport = None
    plan_attempts: list[dict[str, object]] | None = [] if output_dir is not None else None
    if worker is not None:
        selected_skill_ids, bind_error = direct_worker_skill_ids(
            skill_policy,
            allow_shadow=allow_shadow,
        )
        if bind_error is not None:
            print(f"error: {bind_error}", file=sys.stderr)
            return 2
        assignments = [
            Assignment(
                worker=worker,
                task=task,
                stage=1,
                selected_skill_ids=selected_skill_ids,
            )
        ]
    else:
        if output_dir is not None:
            run_io._write_json(
                output_dir / "run.json",
                _payload(
                    task=task,
                    cwd=cwd,
                    roster=roster,
                    dry_run=dry_run,
                    read_only=read_only,
                    status="planning",
                    started_at=started_at,
                    output_dir=output_dir,
                    code_graph=code_graph,
                    drift_impact=drift_impact,
                    evidence=evidence,
                    brief_set=brief_set,
                    codex_transport=transport_for_payload,
                    route=route,
                    code_graph_delta=code_graph_delta,
                    worker=worker,
                ),
            )
        try:
            assignments = planning._call_with_process_registry(
                planning.plan,
                task,
                roster,
                cwd=cwd,
                read_only=read_only,
                sandbox_read_only=sandbox_read_only,
                sandbox=sandbox,
                attempts=plan_attempts,
                code_graph=code_graph,
                drift_impact=drift_impact,
                evidence=evidence,
                route=route,
                skill_policy=skill_policy,
                codex_transport=transport_for_payload,
                process_registry=process_registry,
                output_dir=output_dir,
                model_lease=lease_model_agent,
            )
        except RuntimeError as exc:
            final_attempt = plan_attempts[-1] if plan_attempts else None
            timed_out = isinstance(final_attempt, dict) and final_attempt.get("timed_out") is True
            if isinstance(final_attempt, dict) and isinstance(final_attempt.get("failure_kind"), str):
                failure_kind = final_attempt["failure_kind"]
            elif isinstance(final_attempt, dict) and isinstance(final_attempt.get("parse_error"), str):
                failure_kind = "invalid-plan"
            else:
                failure_kind = "orchestrator-error"
            if timed_out:
                failure_kind = "timeout"
            if output_dir is not None:
                finished_at: datetime | None = datetime.now(timezone.utc)
                run_io._write_json(output_dir / "plan-attempts.json", {"attempts": plan_attempts or []})
                failure_phase = "planning"
                if isinstance(final_attempt, dict):
                    attempt_phase = final_attempt.get("failure_phase")
                    attempt_kind = final_attempt.get("failure_kind")
                    if attempt_phase == "preflight" and attempt_kind == "provider-config":
                        failure_phase = "preflight"
                run_io._write_json(
                    output_dir / "run.json",
                    _payload(
                        task=task,
                        cwd=cwd,
                        roster=roster,
                        dry_run=dry_run,
                        read_only=read_only,
                        status="timeout" if timed_out else "failed",
                        started_at=started_at,
                        finished_at=finished_at,
                        output_dir=output_dir,
                        error=str(exc),
                        failure_phase=failure_phase,
                        failure_kind=failure_kind,
                        failure_seat=roster.orchestrator,
                        code_graph=code_graph,
                        drift_impact=drift_impact,
                        evidence=evidence,
                        brief_set=brief_set,
                        codex_transport=transport_for_payload,
                        route=route,
                        control_socket=control_socket,
                        control_transport=control_transport,
                        code_graph_delta=code_graph_delta,
                        worker=worker,
                    ),
                )
            print(f"error: {exc}", file=sys.stderr)
            return 2

    # Drift checkpoint: after planning (and before any dry-run return). A
    # concurrent commit or branch switch during planning must fail the run
    # instead of letting a dry-run return a plan built on stale state.
    drift_rc = _drift_failure_rc()
    if drift_rc is not None:
        return drift_rc

    if output_dir is not None:
        attempts_payload: dict[str, object] = {"attempts": plan_attempts or []}
        if direct_worker:
            attempts_payload["mode"] = "direct-worker"
        run_io._write_json(output_dir / "plan-attempts.json", attempts_payload)
        plan_doc = receipt_schema.run_plan_document(
            _assignment_payload(assignments),
            run_id=output_dir.name,
        )
        contract_for_plan = verification_contract_payload
        if contract_for_plan is None:
            try:
                run_raw = json.loads((output_dir / "run.json").read_text())
            except (OSError, json.JSONDecodeError):
                run_raw = None
            if isinstance(run_raw, dict) and isinstance(run_raw.get("verification_contract"), dict):
                contract_for_plan = dict(run_raw["verification_contract"])
        if isinstance(contract_for_plan, Mapping):
            plan_doc["verification_contract"] = dict(contract_for_plan)
        budget_for_plan = run_budget_payload
        if budget_for_plan is None:
            try:
                run_raw = json.loads((output_dir / "run.json").read_text())
            except (OSError, json.JSONDecodeError):
                run_raw = None
            if isinstance(run_raw, dict) and isinstance(run_raw.get("run_budget"), dict):
                budget_for_plan = dict(run_raw["run_budget"])
        if isinstance(budget_for_plan, Mapping):
            plan_doc["run_budget"] = dict(budget_for_plan)
        run_io._write_json(output_dir / "plan.json", plan_doc)

    if cwd is not None and output_dir is not None:
        from .. import candidate_set as candidate_set_mod

        assignments, _candidate_decision, candidate_failure = planning._apply_candidate_set_gate(
            cwd=cwd,
            output_dir=output_dir,
            assignments=assignments,
            roster=roster,
            task=task,
            read_only=read_only,
            sandbox_read_only=sandbox_read_only,
            sandbox=sandbox,
            code_graph=code_graph,
            drift_impact=drift_impact,
            evidence=evidence,
            route=route,
            skill_policy=skill_policy,
            transport_for_payload=transport_for_payload,
            process_registry=process_registry,
            plan_attempts=plan_attempts,
            allow_replan=not dry_run and not direct_worker,
            model_lease=lease_model_agent,
        )
        plan_doc = receipt_schema.run_plan_document(
            _assignment_payload(assignments),
            run_id=output_dir.name,
        )
        try:
            prior_plan = json.loads((output_dir / "plan.json").read_text())
        except (OSError, json.JSONDecodeError):
            prior_plan = None
        if isinstance(prior_plan, dict) and isinstance(prior_plan.get("verification_contract"), dict):
            plan_doc["verification_contract"] = dict(prior_plan["verification_contract"])
        elif isinstance(verification_contract_payload, Mapping):
            plan_doc["verification_contract"] = dict(verification_contract_payload)
        if isinstance(prior_plan, dict) and isinstance(prior_plan.get("run_budget"), dict):
            plan_doc["run_budget"] = dict(prior_plan["run_budget"])
        elif isinstance(run_budget_payload, Mapping):
            plan_doc["run_budget"] = dict(run_budget_payload)
        run_io._write_json(output_dir / "plan.json", plan_doc)
        if plan_attempts is not None:
            attempts_payload = {"attempts": plan_attempts or []}
            if direct_worker:
                attempts_payload["mode"] = "direct-worker"
            run_io._write_json(output_dir / "plan-attempts.json", attempts_payload)
        if candidate_failure is not None and not dry_run:
            artifacts.record_run_termination(
                output_dir,
                status="failed",
                failure_phase=candidate_set_mod.FAILURE_PHASE,
                failure_kind=candidate_set_mod.FAILURE_KIND,
                detail=candidate_failure,
                seat=roster.orchestrator,
            )
            print(f"error: {candidate_failure}", file=sys.stderr)
            return 2

    if dry_run:
        payload = {"assignments": _assignment_payload(assignments)}
        if output_dir is not None:
            finished_at = datetime.now(timezone.utc)
            run_io._write_json(
                output_dir / "run.json",
                _payload(
                    task=task,
                    cwd=cwd,
                    roster=roster,
                    dry_run=dry_run,
                    read_only=read_only,
                    status="dry-run",
                    started_at=started_at,
                    finished_at=finished_at,
                    output_dir=output_dir,
                    code_graph=code_graph,
                    drift_impact=drift_impact,
                    evidence=evidence,
                    brief_set=brief_set,
                    codex_transport=transport_for_payload,
                    route=route,
                    code_graph_delta=code_graph_delta,
                    worker=worker,
                ),
            )
        print(json.dumps(payload, indent=2))
        return 0

    if show_plan or verbose:
        prompts._print_plan(assignments)

    effective_transport = codex_transport or roster.codex_transport
    has_codex_workers = any(
        (roster.agents.get(a.worker) is not None and roster.agents[a.worker].cli == "codex") for a in assignments
    )
    if effective_transport == "app-server" and not has_codex_workers:
        effective_transport = "exec"
    elif effective_transport == "app-server" and output_dir is not None:
        control_socket = output_dir / "control.sock"
    transport_for_payload = effective_transport
    if output_dir is not None:
        run_io._write_json(
            output_dir / "run.json",
            _payload(
                task=task,
                cwd=cwd,
                roster=roster,
                dry_run=dry_run,
                read_only=read_only,
                status="dispatching",
                started_at=started_at,
                output_dir=output_dir,
                code_graph=code_graph,
                drift_impact=drift_impact,
                evidence=evidence,
                brief_set=brief_set,
                codex_transport=transport_for_payload,
                route=route,
                control_socket=control_socket,
                control_transport=control_transport,
                code_graph_delta=code_graph_delta,
                worker=worker,
            ),
        )
    appserver = None
    control_registry = None
    control_server = None

    def close_appserver() -> None:
        nonlocal appserver
        server = appserver
        appserver = None
        close = getattr(server, "close", None)
        if callable(close):
            close()

    def close_control_server() -> None:
        nonlocal control_server
        server = control_server
        control_server = None
        close = getattr(server, "close", None)
        if callable(close):
            close()

    def close_server_resources() -> None:
        try:
            close_control_server()
        finally:
            close_appserver()

    try:
        if effective_transport == "app-server":
            try:
                # BRIGADE_RUN_ID is run-scoped process identity, not per-seat env.
                # AppServer already accepts process env; seat.env stays forbidden
                # on the shared app-server session (roster + dispatch force direct).
                appserver_kwargs: dict[str, Any] = {"cwd": cwd}
                if output_dir is not None:
                    appserver_params = inspect.signature(codex_appserver.AppServer).parameters.values()
                    accepts_env = any(
                        parameter.name == "env" or parameter.kind is inspect.Parameter.VAR_KEYWORD
                        for parameter in appserver_params
                    )
                    if accepts_env:
                        appserver_kwargs["env"] = {receipt_schema.BRIGADE_RUN_ID_ENV: output_dir.name}
                appserver = planning._call_with_process_registry(
                    codex_appserver.AppServer,
                    process_registry=process_registry,
                    **appserver_kwargs,
                )
                appserver.start()
            except codex_appserver.AppServerError as exc:
                close_appserver()
                transport_routing_payload = seat_health_policy.transport_fallback_decision(
                    requested_transport="app-server",
                    effective_transport="exec",
                    cause="transport-unavailable",
                    detail=str(exc),
                )
                if output_dir is not None:
                    try:
                        run_io._write_json(output_dir / "transport-routing.json", dict(transport_routing_payload))
                    except OSError as write_exc:
                        print(f"error: transport routing receipt failed: {write_exc}", file=sys.stderr)
                print(
                    f"warning: codex app-server unavailable ({exc}); falling back to exec [transport-unavailable]",
                    file=sys.stderr,
                )
                effective_transport = "exec"
                control_socket = None
            if appserver is not None and output_dir is not None:
                control_registry = run_control.LiveTurnRegistry()
                control_server = run_control.ControlServer(output_dir / "control.sock", control_registry)
                try:
                    control_transport = control_server.start()
                except run_control.ControlError as exc:
                    close_control_server()
                    print(f"warning: run control unavailable ({exc})", file=sys.stderr)
                    control_registry = None
                    control_transport = None
                    control_socket = None
        transport_for_payload = effective_transport
        if output_dir is not None:
            run_io._write_json(
                output_dir / "run.json",
                _payload(
                    task=task,
                    cwd=cwd,
                    roster=roster,
                    dry_run=dry_run,
                    read_only=read_only,
                    status="dispatching",
                    started_at=started_at,
                    output_dir=output_dir,
                    code_graph=code_graph,
                    drift_impact=drift_impact,
                    evidence=evidence,
                    brief_set=brief_set,
                    codex_transport=transport_for_payload,
                    route=route,
                    control_socket=control_socket,
                    control_transport=control_transport,
                    code_graph_delta=code_graph_delta,
                    worker=worker,
                ),
            )

        active_stage = min(assignment.stage for assignment in assignments) if assignments else 1
        active_seats = tuple(assignment.worker for assignment in assignments if assignment.stage == active_stage)
    except BaseException:
        close_server_resources()
        raise

    budget_coordinator: run_budget.BudgetCoordinator | None = None

    def _budget_cancel_fn() -> run_budget.CancellationReport:
        """Record observed per-seat interruption state without raw transport data."""
        return artifacts._observed_budget_cancellation(process_registry, control_registry, active_seats)

    def _append_budget_event(event_type: str, payload: Mapping[str, Any], idempotency_key: str) -> Any:
        assert output_dir is not None
        return run_lifecycle.record_lifecycle_event(
            output_dir,
            event_type=event_type,
            payload=dict(payload),
            idempotency_key=idempotency_key,
            workspace=lock_workspace,
        )

    if output_dir is not None and lock_workspace is not None:
        try:
            budget_coordinator = artifacts._build_budget_coordinator(
                output_dir,
                started_at=started_at,
                append_event=_append_budget_event,
            )
        except run_budget.BudgetCompatibilityError as exc:
            # Unknown schema/dimension must stop recovery/dispatch with a bounded diagnostic.
            active_seat = active_seats[0] if len(active_seats) == 1 else None
            artifacts.record_run_termination(
                output_dir,
                status="failed",
                failure_phase="dispatch",
                failure_kind="unexpected-error",
                detail=f"run budget compatibility error: {exc.diagnostic}",
                seat=active_seat,
                active_seats=active_seats,
            )
            close_server_resources()
            return 1

    def stage_started(stage: int, seats: tuple[str, ...]) -> None:
        nonlocal active_stage, active_seats
        active_stage = stage
        active_seats = seats
        if output_dir is not None:
            artifacts.record_dispatch_stage(output_dir, stage=stage, seats=seats)

    def dispatch_interrupted() -> None:
        if output_dir is None:
            return
        active_seat = active_seats[0] if len(active_seats) == 1 else None
        # Live operator cancellation is a policy terminal (#593), not a worker
        # FailureClass and not infrastructure-neutral under #580.
        if budget_coordinator is not None:
            try:
                budget_coordinator.request_cancel(
                    request_id="opcancel:live-ctrl-c",
                    reason_class="operator_cancel",
                    transport_capability="mixed",
                    dimension="wall_clock_seconds",
                    cancel_fn=_budget_cancel_fn,
                )
            except (run_budget.BudgetError, run_lifecycle.LifecycleJournalError, OSError) as exc:
                print(f"warning: budget cancel receipt failed: {exc}", file=sys.stderr)
        artifacts.record_run_termination(
            output_dir,
            status="canceled",
            failure_phase="dispatch",
            failure_kind=run_budget.POLICY_KIND_OPERATOR_CANCELLED,
            detail="run canceled by operator",
            seat=active_seat,
            active_seats=active_seats,
        )

    def dispatch_fact(event_type: str, agent: Agent, attempt: int | None = None) -> int | None:
        if output_dir is None:
            return None
        try:
            event = run_lifecycle.record_dispatch_fact(
                output_dir,
                workspace=lock_workspace,
                event_type=event_type,
                seat=agent.name,
                attempt=attempt,
            )
        except (OSError, run_lifecycle.LifecycleJournalError, run_checkpoint.CheckpointError) as exc:
            raise runguard.RetainRunLockError(f"failed to record dispatch lifecycle fact: {exc}") from exc
        if event is None:
            return None
        recorded_attempt = event.payload.get("attempt")
        return recorded_attempt if isinstance(recorded_attempt, int) else None

    def dispatch_requested(agent: Agent) -> int | None:
        # Enforce wall-clock / worker-dispatch ceilings before new work starts (#593).
        # Attempt allocation and reservation share one lock; retries reclaim an
        # open pending identity while concurrent same-seat callers mint anew.
        if budget_coordinator is None or output_dir is None:
            return dispatch_fact("run.dispatch.requested", agent)
        try:
            return budget_coordinator.reserve_and_record_dispatch(
                seat=agent.name,
                allocate_attempt=lambda: run_lifecycle.allocate_next_dispatch_attempt(output_dir, agent.name),
                record_requested=lambda attempt: dispatch_fact("run.dispatch.requested", agent, attempt),
            )
        except run_budget.BudgetPolicyError as exc:
            if exc.exhausted:
                try:
                    budget_coordinator.request_cancel(
                        request_id=f"budgetcancel:{exc.dimension}",
                        reason_class="budget_cancel",
                        transport_capability="mixed",
                        dimension=exc.dimension,
                        cancel_fn=_budget_cancel_fn,
                    )
                except (run_budget.BudgetError, run_lifecycle.LifecycleJournalError, OSError) as cancel_exc:
                    print(f"warning: budget cancel receipt failed: {cancel_exc}", file=sys.stderr)
            raise
        except (OSError, run_lifecycle.LifecycleJournalError, run_checkpoint.CheckpointError) as exc:
            # Mirror dispatch_fact fail-closed translation so enrollment gaps
            # before the durable requested write retain the run lock.
            raise runguard.RetainRunLockError(f"failed to record dispatch lifecycle fact: {exc}") from exc

    def dispatch_observed(agent: Agent, attempt: int) -> None:
        dispatch_fact("run.dispatch.observed", agent, attempt)

    def dispatch_completed(agent: Agent, attempt: int) -> None:
        dispatch_fact("run.dispatch.completed", agent, attempt)

    def dispatch_failed(agent: Agent, attempt: int) -> None:
        dispatch_fact("run.dispatch.failed", agent, attempt)

    def reprobe_seat_for_retry(agent: Agent) -> bool:
        active_health_probe.invalidate(seat=agent.name)
        try:
            result = active_health_probe.probe(
                agent,
                roster,
                workspace=cwd,
                allow_model_smoke=False,
                require_hard_isolation=read_only,
            )
        except Exception:
            return False
        if output_dir is not None:
            try:
                seat_health.write_seat_health_receipt(
                    output_dir / "seat-health.json",
                    (result,),
                    run_id=output_dir.name,
                )
            except OSError as exc:
                print(f"error: seat health receipt failed: {exc}", file=sys.stderr)
        return result.status == "healthy"

    def persist_failed_attempt(result: WorkerResult) -> None:
        if output_dir is None:
            return
        try:
            run_io.write_sidecar_revision(
                output_dir,
                "worker-results.json",
                receipt_schema.worker_results_document(_worker_payload([result])),
            )
        except OSError as exc:
            print(f"error: worker attempt receipt failed: {exc}", file=sys.stderr)

    active_seat = active_seats[0] if len(active_seats) == 1 else None
    worker_prompt_builder = partial(
        planning._worker_prompt,
        skill_policy=skill_policy,
        run_id=output_dir.name if output_dir is not None else message_envelope.IN_MEMORY_RUN_ID,
        to_seat=roster.orchestrator,
    )
    try:
        try:
            worker_results = planning._call_with_process_registry(
                planning.dispatch,
                assignments,
                roster,
                cwd=cwd,
                read_only=read_only,
                sandbox_read_only=sandbox_read_only,
                sandbox=sandbox,
                direct=direct_worker,
                code_graph=code_graph,
                drift_impact=drift_impact,
                evidence=evidence,
                appserver=appserver,
                control_registry=control_registry,
                events_dir=(output_dir / "events") if (output_dir is not None and appserver is not None) else None,
                verbose=verbose,
                authorized_writable_worktree=authorized_writable_worktree,
                fail_fast=fail_fast,
                scheduler=scheduler,
                route_dependencies=dict(route.dependencies) if route is not None and route.attached else None,
                route_held=dict(route.held) if route is not None and route.attached else None,
                on_stage_start=stage_started,
                on_interrupt=dispatch_interrupted,
                on_scheduler_resolved=scheduler_resolved,
                on_dispatch_requested=dispatch_requested,
                on_dispatch_observed=dispatch_observed,
                on_dispatch_completed=dispatch_completed,
                on_dispatch_failed=dispatch_failed,
                process_registry=process_registry,
                build_prompt=worker_prompt_builder,
                quarantine_state=quarantine_state,
                reprobe_seat=reprobe_seat_for_retry,
                on_failed_attempt_persisted=persist_failed_attempt,
                run_id=output_dir.name if output_dir is not None else None,
                output_dir=output_dir,
                model_lease=lease_model_agent,
            )
        except runguard.RetainRunLockError:
            raise
        except run_budget.BudgetPolicyError as exc:
            active_seat = active_seats[0] if len(active_seats) == 1 else None
            status, kind = run_budget.terminal_status_for_policy("budget_exhausted")
            # Reservation denial without full exhaustion still terminalizes the
            # run: new work cannot start under the declared ceiling.
            if output_dir is not None:
                artifacts.record_run_termination(
                    output_dir,
                    status=status,
                    failure_phase="dispatch",
                    failure_kind=kind,
                    detail=run_io._one_line(str(exc)) or "run budget reservation denied",
                    seat=active_seat,
                    active_seats=active_seats,
                )
            return 1
        except KeyboardInterrupt:
            active_seat = active_seats[0] if len(active_seats) == 1 else None
            if output_dir is not None:
                artifacts.record_run_termination(
                    output_dir,
                    status="canceled",
                    failure_phase="dispatch",
                    failure_kind=run_budget.POLICY_KIND_OPERATOR_CANCELLED,
                    detail="run canceled by operator",
                    seat=active_seat,
                    active_seats=active_seats,
                )
            raise
        except TimeoutError as exc:
            active_seat = active_seats[0] if len(active_seats) == 1 else None
            detail = run_io._one_line(str(exc)) or "worker dispatch timed out"
            if output_dir is not None:
                artifacts.record_run_termination(
                    output_dir,
                    status="timeout",
                    failure_phase="dispatch",
                    failure_kind="timeout",
                    detail=detail,
                    seat=active_seat,
                    active_seats=active_seats,
                )
            raise
        except Exception as exc:
            active_seat = active_seats[0] if len(active_seats) == 1 else None
            detail = f"{type(exc).__name__}: {run_io._one_line(str(exc)) or 'unexpected dispatch failure'}"
            if output_dir is not None:
                artifacts.record_run_termination(
                    output_dir,
                    status="failed",
                    failure_phase="dispatch",
                    failure_kind="unexpected-error",
                    detail=detail,
                    seat=active_seat,
                    active_seats=active_seats,
                )
            raise
    finally:
        close_server_resources()
    if (
        output_dir is not None
        and lock_workspace is not None
        and runguard.is_active_run_owner(lock_workspace, output_dir)
    ):
        from .. import runs_cmd

        approval_reference = runs_cmd._approval_pause_reference_for_owned_run(
            lock_workspace,
            output_dir,
        )
        if approval_reference is not None:
            artifacts.write_approval_resume_handoff(
                output_dir,
                worker_results,
                requester_worker=approval_reference.requester_worker,
                requester_thread_id=approval_reference.requester_thread_id,
            )
            artifacts.record_approval_pause(output_dir, approval_reference)
            return 0
    # Drift checkpoint: after worker dispatch. A concurrent commit or branch
    # switch during dispatch must fail the run before ground truth attributes
    # the foreign state to the worker.
    drift_rc = _drift_failure_rc()
    if drift_rc is not None:
        return drift_rc
    if output_dir is not None:
        artifacts.record_result_processing(output_dir)
    if output_dir is not None and code_graph_delta_before is not None and cwd is not None:
        code_graph_delta = graphtrail_delta.capture_after_and_diff(cwd, output_dir, code_graph_delta_before)
    context_eval_payload = prompts._context_eval_for_run(code_graph, code_graph_delta)
    ground_truth = prompts.build_ground_truth(cwd, started_at, pre_run_snapshot=pre_run_snapshot)
    if code_graph_delta is not None:
        ground_truth["code_graph_delta"] = code_graph_delta
    if context_eval_payload is not None:
        ground_truth["context_eval"] = context_eval_payload
    suspected_noop = prompts._suspected_noop(
        ground_truth=ground_truth,
        worker_results=worker_results,
        dry_run=dry_run,
        read_only=read_only,
        sandbox_read_only=sandbox_read_only,
        sandbox=sandbox,
    )
    ground_truth["suspected_noop"] = suspected_noop
    worker_results = prompts._mark_noop_worker_results(worker_results, suspected_noop)
    if output_dir is not None:
        worker_results = _write_worker_logs(output_dir, worker_results)
        run_io.write_sidecar_revision(
            output_dir,
            "worker-results.json",
            receipt_schema.worker_results_document(
                _worker_payload(worker_results),
                ground_truth=ground_truth,
            ),
        )
    isolation_detail = _run_isolation_check()
    if isolation_detail is not None:
        if output_dir is not None:
            artifacts.record_run_termination(
                output_dir,
                status="failed",
                failure_phase="run-isolation",
                failure_kind="isolation-breach",
                detail=isolation_detail,
                seat=roster.orchestrator,
            )
        print(f"error: {isolation_detail}", file=sys.stderr)
        return 2
    if verbose:
        prompts._print_worker_status(worker_results)
        if not direct_worker:
            print("synthesis:")
            print(f"  -> {roster.orchestrator}")

    synth_captured = None
    if direct_worker:
        direct_result = (
            worker_results[0]
            if worker_results
            else WorkerResult(
                worker=worker or "",
                task=task,
                text="",
                ok=False,
                detail="direct worker produced no result",
            )
        )
        final = _agent_result_from_worker(direct_result)
    else:
        if output_dir is not None:
            run_io._write_json(
                output_dir / "run.json",
                _payload(
                    task=task,
                    cwd=cwd,
                    roster=roster,
                    dry_run=dry_run,
                    read_only=read_only,
                    status="synthesizing",
                    started_at=started_at,
                    output_dir=output_dir,
                    code_graph=code_graph,
                    drift_impact=drift_impact,
                    evidence=evidence,
                    brief_set=brief_set,
                    codex_transport=transport_for_payload,
                    route=route,
                    control_socket=control_socket,
                    control_transport=control_transport,
                    code_graph_delta=code_graph_delta,
                    worker=worker,
                ),
            )
        synthesis_started = time.monotonic()
        synth_prompt = prompts.build_synth_prompt(
            task,
            worker_results,
            read_only=read_only,
            ground_truth=ground_truth,
            code_graph=code_graph,
            drift_impact=drift_impact,
            evidence=evidence,
            run_id=output_dir.name if output_dir is not None else message_envelope.IN_MEMORY_RUN_ID,
            to_seat=roster.orchestrator,
        )
        synth_request = message_envelope.emit(
            synth_prompt,
            kind="synthesis-request",
            producer="aboyeur.build_synth_prompt",
            from_seat=message_envelope.BRIGADE_SEAT,
            to_seat=roster.orchestrator,
            run_dir=output_dir,
            run_id=output_dir.name if output_dir is not None else None,
            assignment_id=message_envelope.default_assignment_id("synthesis-request"),
            session_harness=planning._orchestrator_harness(roster),
        )
        synth_captured = None
        if not synth_request.delivered:
            final = agents.AgentResult(
                text="",
                ok=False,
                detail=synth_request.reason or "synthesis-request rejected by provenance gate",
                failure_phase="synthesis",
                failure_kind="unclassified",
            )
        else:
            final = planning._call_with_process_registry(
                planning._run_orchestrator,
                roster,
                synth_prompt,
                cwd=cwd,
                read_only=read_only,
                sandbox_read_only=sandbox_read_only,
                sandbox=sandbox,
                codex_transport=transport_for_payload,
                process_registry=process_registry,
                model_lease=lease_model_agent,
            )
            synth_captured = message_envelope.emit(
                final.text,
                kind="synthesis-result",
                producer="aboyeur.run",
                from_seat=roster.orchestrator,
                to_seat=message_envelope.BRIGADE_SEAT,
                run_dir=output_dir,
                run_id=output_dir.name if output_dir is not None else None,
                assignment_id=message_envelope.default_assignment_id("synthesis-result"),
                session_harness=planning._orchestrator_harness(roster),
            )
            if not synth_captured.delivered:
                final = replace(
                    final,
                    text="",
                    ok=False,
                    detail=synth_captured.reason or "synthesis-result rejected by provenance gate",
                    failure_phase=final.failure_phase or "synthesis",
                    failure_kind=final.failure_kind or "unclassified",
                )
        final = replace(final, duration_seconds=max(0.0, round(time.monotonic() - synthesis_started, 3)))
    # Drift checkpoint: after synthesis. A concurrent commit or branch switch
    # during synthesis must fail the run before the synthesized answer is
    # finalized on top of drifted state.
    drift_rc = _drift_failure_rc()
    if drift_rc is not None:
        return drift_rc
    if output_dir is not None:
        if not direct_worker:
            final = _write_agent_logs(output_dir, "synthesis", final)
        synthesis_payload = (
            receipt_schema.synthesis_document(
                mode="direct-worker",
                worker=worker,
                result=_agent_result_payload(final),
                ground_truth=ground_truth,
                run_id=output_dir.name,
                worker_results=_worker_payload(worker_results),
            )
            if direct_worker
            else receipt_schema.synthesis_document(
                orchestrator=roster.orchestrator,
                result=_agent_result_payload(final),
                ground_truth=ground_truth,
                run_id=output_dir.name,
                worker_results=_worker_payload(worker_results),
            )
        )
        if synth_captured is not None:
            synthesis_payload["provenance"] = synth_captured.envelope
        causal_receipt.write_synthesis_lineage_artifacts(
            output_dir,
            synthesis_payload,
            worker_results=_worker_payload(worker_results),
            write=run_io.write_sidecar_revision,
        )
    if not final.ok:
        if output_dir is not None:
            finished_at = datetime.now(timezone.utc)
            interrupted = final.status == "interrupted"
            if direct_worker:
                (output_dir / "final.txt").write_text(final.text + "\n")
            run_io._write_json(
                output_dir / "run.json",
                _payload(
                    task=task,
                    cwd=cwd,
                    roster=roster,
                    dry_run=dry_run,
                    read_only=read_only,
                    status="timeout" if final.timed_out else "canceled" if interrupted else "failed",
                    started_at=started_at,
                    finished_at=finished_at,
                    output_dir=output_dir,
                    error=final.detail,
                    failure_phase=(
                        final.failure_phase
                        or ("inference" if final.timed_out else "dispatch" if direct_worker else "synthesis")
                    ),
                    failure_kind=(
                        "timeout"
                        if final.timed_out
                        else final.failure_kind or ("interrupted" if interrupted else "agent-error")
                    ),
                    failure_seat=worker if direct_worker else roster.orchestrator,
                    code_graph=code_graph,
                    drift_impact=drift_impact,
                    evidence=evidence,
                    brief_set=brief_set,
                    codex_transport=transport_for_payload,
                    route=route,
                    code_graph_delta=code_graph_delta,
                    context_eval_payload=context_eval_payload,
                    suspected_noop=suspected_noop,
                    worker=worker,
                    transport_warning=final.transport_warning,
                ),
            )
        if direct_worker:
            print(f"error: worker failed: {final.detail}", file=sys.stderr)
            if final.text:
                print(final.text)
        else:
            print(f"error: orchestrator failed during synthesis: {final.detail}", file=sys.stderr)
        return 2
    # Drift checkpoint: immediately before finalization. The final ok receipt
    # must not be written on top of state a concurrent commit moved out from
    # under the run.
    drift_rc = _drift_failure_rc()
    if drift_rc is not None:
        return drift_rc
    workers_ok = direct_worker or all(result.ok for result in worker_results)
    failed_seats = [result.worker for result in worker_results if not result.ok]
    if output_dir is not None:
        pending_handoff = handoff_inbox is not None
        if not workers_ok:
            final_status = "incomplete"
            finished_at = datetime.now(timezone.utc)
            run_status = "incomplete"
        else:
            final_status = "artifact-collection" if defer_artifact_collection else "ok"
            finished_at = None if defer_artifact_collection or pending_handoff else datetime.now(timezone.utc)
            run_status = "handoff" if pending_handoff else final_status
        (output_dir / "final.txt").write_text(final.text + "\n")
        run_io._write_json(
            output_dir / "run.json",
            _payload(
                task=task,
                cwd=cwd,
                roster=roster,
                dry_run=dry_run,
                read_only=read_only,
                status=run_status,
                started_at=started_at,
                finished_at=finished_at,
                output_dir=output_dir,
                error=(
                    f"{len(failed_seats)} worker(s) failed or were skipped: {', '.join(failed_seats)}"
                    if not workers_ok
                    else None
                ),
                failure_phase="workers" if not workers_ok else None,
                failure_kind="worker-failure" if not workers_ok else None,
                failure_seat=",".join(failed_seats) if not workers_ok else None,
                code_graph=code_graph,
                drift_impact=drift_impact,
                evidence=evidence,
                brief_set=brief_set,
                codex_transport=transport_for_payload,
                route=route,
                control_socket=control_socket,
                control_transport=control_transport,
                code_graph_delta=code_graph_delta,
                context_eval_payload=context_eval_payload,
                suspected_noop=suspected_noop,
                worker=worker,
                transport_warning=direct_result.transport_warning if direct_worker else None,
                worker_failure_summary=(
                    seat_health_policy.worker_failure_summary(worker_results) if not workers_ok else None
                ),
            ),
        )
    if handoff_inbox is not None and workers_ok:
        try:
            handoff = run_io.write_run_handoff(
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
                run_io._write_json(
                    output_dir / "run.json",
                    _payload(
                        task=task,
                        cwd=cwd,
                        roster=roster,
                        dry_run=dry_run,
                        read_only=read_only,
                        status="failed",
                        started_at=started_at,
                        finished_at=finished_at,
                        output_dir=output_dir,
                        error=detail,
                        failure_phase="handoff",
                        failure_kind="handoff-write-error",
                        failure_seat=roster.orchestrator,
                        code_graph=code_graph,
                        drift_impact=drift_impact,
                        evidence=evidence,
                        brief_set=brief_set,
                        codex_transport=transport_for_payload,
                        route=route,
                        control_socket=control_socket,
                        control_transport=control_transport,
                        code_graph_delta=code_graph_delta,
                        context_eval_payload=context_eval_payload,
                        suspected_noop=suspected_noop,
                        worker=worker,
                    ),
                )
            print(f"error: {detail}", file=sys.stderr)
            print(final.text)
            return 2
        print(f"handoff: {handoff}", file=sys.stderr)
        if output_dir is not None:
            finished_at = None if defer_artifact_collection else datetime.now(timezone.utc)
            run_io._write_json(
                output_dir / "run.json",
                _payload(
                    task=task,
                    cwd=cwd,
                    roster=roster,
                    dry_run=dry_run,
                    read_only=read_only,
                    status="artifact-collection" if defer_artifact_collection else "ok",
                    started_at=started_at,
                    finished_at=finished_at,
                    output_dir=output_dir,
                    handoff_path=handoff,
                    code_graph=code_graph,
                    drift_impact=drift_impact,
                    evidence=evidence,
                    brief_set=brief_set,
                    codex_transport=transport_for_payload,
                    route=route,
                    control_socket=control_socket,
                    control_transport=control_transport,
                    code_graph_delta=code_graph_delta,
                    context_eval_payload=context_eval_payload,
                    suspected_noop=suspected_noop,
                    worker=worker,
                ),
            )
    reroute_summary = seat_health_policy.format_reroute_summary(roster.seat_routing)
    if reroute_summary is not None:
        print(reroute_summary, file=sys.stderr)
    if not workers_ok:
        print(
            seat_health_policy.format_incomplete_warning(
                worker_results,
                routing_decisions=roster.seat_routing,
            ),
            file=sys.stderr,
        )
        print(final.text)
        return 3
    print(final.text)
    return 0
