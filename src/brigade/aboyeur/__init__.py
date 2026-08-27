"""Bounded cross-model orchestration for `brigade run`.

Attributes remain live views of their owning seam. Assignment is forwarded so
monkeypatching ``brigade.aboyeur`` changes the callable seen by all
module-reference callers.
"""
# ruff: noqa: F401

from __future__ import annotations

import importlib as _importlib
import sys as _sys
from types import ModuleType
from typing import TYPE_CHECKING
from typing import Any as _Any

_MODULE_NAMES = (
    "briefs",
    "run_io",
    "planning",
    "prompts",
    "artifacts",
    "orchestrator",
)

# These were imports on the original aboyeur module. Every seam preserves the
# corresponding module global, so facade monkeypatches must be broadcast to
# every seam that binds it.
_SHARED_IMPORT_NAMES = (
    "copy",
    "inspect",
    "json",
    "os",
    "re",
    "signal",
    "sys",
    "threading",
    "time",
    "contextmanager",
    "dataclass",
    "replace",
    "datetime",
    "timezone",
    "partial",
    "wraps",
    "JSONDecoder",
    "Path",
    "Any",
    "Callable",
    "Iterator",
    "Mapping",
    "Sequence",
    "uuid4",
    "agents",
    "codex_appserver",
    "context_eval",
    "evidence_brief_mod",
    "graphtrail_delta",
    "causal_receipt",
    "localio",
    "message_envelope",
    "proc",
    "receipt_schema",
    "runguard",
    "run_control",
    "run_checkpoint",
    "run_budget",
    "run_events",
    "run_journal",
    "run_lifecycle",
    "run_projector",
    "run_shadow",
    "seat_health",
    "seat_health_policy",
    "verification_contract",
    "validate_final_output",
    "_agent_result_from_worker",
    "_agent_result_payload",
    "_assignment_payload",
    "_worker_payload",
    "_write_agent_logs",
    "_write_worker_logs",
    "Assignment",
    "WorkerResult",
    "dag_cycle_members",
    "Agent",
    "Roster",
    "is_cli_allowed",
    "read_only_capability_error",
    "timeout_for",
    "workers",
    "RouteBrief",
    "route_brief",
    "uncovered_stages",
    "unknown_covers",
    "RoutePolicyDecision",
    "direct_worker_skill_ids",
    "planner_skill_policy_section",
    "validate_plan_skill_bindings",
    "worker_skill_policy_constraint",
)
if TYPE_CHECKING:
    from .briefs import (
        BRIEF_BUDGET_BYTES,
        BriefSet,
        CODE_GRAPH_HEADING,
        CODE_GRAPH_LIMIT,
        CodeGraphBrief,
        DRIFT_IMPACT_HEADING,
        DRIFT_IMPACT_LIMIT,
        DriftImpactBrief,
        EvidenceBrief,
        NO_PLAN_FILE_RULE,
        _brief_bytes,
        _brief_order,
        _drift_report_excerpt,
        _drift_symbol_candidates,
        _graphtrail_bin,
        _latest_drift_report,
        _pending_drift_entries,
        _prepend_brief,
        _prepend_code_graph,
        _prepend_optional_briefs,
        _read_json_dict,
        _safe_watch_name,
        _truncate_brief_text,
        _truncate_on_line_boundary,
        _upstream_drift_reports_dir,
        _upstream_drift_state_path,
        arbitrate_briefs,
        build_plan_prompt,
        code_graph_brief,
        drift_impact_brief,
    )
    from .run_io import (
        _AUTHORITY_REQUEST_FIELD,
        _PROJECTION_METADATA_FIELDS,
        _authoritative_prior_decision,
        _ensure_directory_durable,
        _fsync_directory,
        _genuine_journal_ahead,
        _one_line,
        _payload_requests_authority,
        _project_authority_candidate,
        _resolve_authority_state,
        _revision_contains,
        _safe_document_content,
        _slug,
        _supports_directory_fsync,
        _utc_iso,
        _write_json,
        _write_json_inner,
        _write_new_revision,
        make_run_dir,
        write_run_handoff,
        write_sidecar_revision,
    )
    from .planning import (
        PLAN_JSON_ONLY_RULE,
        _admitted_result_output,
        _apply_candidate_set_gate,
        _call_with_process_registry,
        _candidate_set_replan_note,
        _contains_event_marker,
        _coverage_missing,
        _emit_plan_request,
        _extract_fenced_json,
        _extract_json,
        _loads_first_json_object,
        _orchestrator_failure_detail,
        _orchestrator_harness,
        _orchestrator_hides_write_tools,
        _parse_gated_plan,
        _read_only_rules,
        _record_plan_attempt,
        _redact_event_marker,
        _render_prior_results,
        _result_from_seat,
        _run_codex_appserver_worker,
        _run_orchestrator,
        _unknown_covers,
        _validate_plan_dependencies,
        _worker_event_writer,
        _worker_prompt,
        dispatch,
        parse_plan,
        plan,
    )
    from .prompts import (
        NOOP_DETAIL,
        _ROUTE_CHANGED_PATHS_CAP,
        _code_graph_delta_skip,
        _context_eval_fact,
        _context_eval_for_run,
        _git_stdout,
        _ground_truth_facts,
        _ground_truth_str_list,
        _initial_code_graph_delta,
        _is_brigade_path,
        _mark_noop_worker_results,
        _non_brigade_paths,
        _parse_iso_datetime,
        _print_plan,
        _print_worker_status,
        _receipt_command_payload,
        _receipt_git_snapshot,
        _route_changed_paths,
        _suspected_noop,
        _verify_receipt_payload,
        _verify_receipts_since,
        _with_patch_ref,
        build_ground_truth,
        build_synth_prompt,
    )
    from .artifacts import (
        _build_budget_coordinator,
        _direct_worker_error,
        _initial_verification_budget,
        _observed_budget_cancellation,
        _roster_payload,
        _roster_resolution_payload,
        _run_payload,
        record_approval_pause,
        record_artifact_collection,
        record_dispatch_stage,
        record_result_processing,
        record_run_start,
        record_run_termination,
        set_artifact_patch_ref,
        update_run_receipt,
        write_approval_resume_handoff,
    )
    from .orchestrator import (
        OrchestratorHealthRoutingDecision,
        WorkerResult,
        _seat_health_result_for,
        _seat_health_typed_cause,
        _terminalize_run_lifecycle,
        _worker_payload,
        _write_run_seat_health_receipt,
        _write_seat_routing_receipt,
        resolve_orchestrator_health_routing,
        run,
        terminal_sigterm_handler,
    )

_OWNERS = {
    "BRIEF_BUDGET_BYTES": "briefs",
    "BriefSet": "briefs",
    "CODE_GRAPH_HEADING": "briefs",
    "CODE_GRAPH_LIMIT": "briefs",
    "CodeGraphBrief": "briefs",
    "DRIFT_IMPACT_HEADING": "briefs",
    "DRIFT_IMPACT_LIMIT": "briefs",
    "DriftImpactBrief": "briefs",
    "EvidenceBrief": "briefs",
    "NOOP_DETAIL": "prompts",
    "NO_PLAN_FILE_RULE": "briefs",
    "OrchestratorHealthRoutingDecision": "orchestrator",
    "PLAN_JSON_ONLY_RULE": "planning",
    "_AUTHORITY_REQUEST_FIELD": "run_io",
    "_PROJECTION_METADATA_FIELDS": "run_io",
    "_ROUTE_CHANGED_PATHS_CAP": "prompts",
    "_admitted_result_output": "planning",
    "_apply_candidate_set_gate": "planning",
    "_authoritative_prior_decision": "run_io",
    "_brief_bytes": "briefs",
    "_brief_order": "briefs",
    "_build_budget_coordinator": "artifacts",
    "_call_with_process_registry": "planning",
    "_candidate_set_replan_note": "planning",
    "_code_graph_delta_skip": "prompts",
    "_contains_event_marker": "planning",
    "_context_eval_fact": "prompts",
    "_context_eval_for_run": "prompts",
    "_coverage_missing": "planning",
    "_direct_worker_error": "artifacts",
    "_drift_report_excerpt": "briefs",
    "_drift_symbol_candidates": "briefs",
    "_emit_plan_request": "planning",
    "_ensure_directory_durable": "run_io",
    "_extract_fenced_json": "planning",
    "_extract_json": "planning",
    "_fsync_directory": "run_io",
    "_genuine_journal_ahead": "run_io",
    "_git_stdout": "prompts",
    "_graphtrail_bin": "briefs",
    "_ground_truth_facts": "prompts",
    "_ground_truth_str_list": "prompts",
    "_initial_code_graph_delta": "prompts",
    "_initial_verification_budget": "artifacts",
    "_is_brigade_path": "prompts",
    "_latest_drift_report": "briefs",
    "_loads_first_json_object": "planning",
    "_mark_noop_worker_results": "prompts",
    "_non_brigade_paths": "prompts",
    "_observed_budget_cancellation": "artifacts",
    "_one_line": "run_io",
    "_orchestrator_failure_detail": "planning",
    "_orchestrator_harness": "planning",
    "_orchestrator_hides_write_tools": "planning",
    "_parse_gated_plan": "planning",
    "_parse_iso_datetime": "prompts",
    "_payload_requests_authority": "run_io",
    "_pending_drift_entries": "briefs",
    "_prepend_brief": "briefs",
    "_prepend_code_graph": "briefs",
    "_prepend_optional_briefs": "briefs",
    "_print_plan": "prompts",
    "_print_worker_status": "prompts",
    "_project_authority_candidate": "run_io",
    "_read_json_dict": "briefs",
    "_read_only_rules": "planning",
    "_receipt_command_payload": "prompts",
    "_receipt_git_snapshot": "prompts",
    "_record_plan_attempt": "planning",
    "_redact_event_marker": "planning",
    "_render_prior_results": "planning",
    "_resolve_authority_state": "run_io",
    "_result_from_seat": "planning",
    "_revision_contains": "run_io",
    "_roster_payload": "artifacts",
    "_roster_resolution_payload": "artifacts",
    "_route_changed_paths": "prompts",
    "_run_codex_appserver_worker": "planning",
    "_run_orchestrator": "planning",
    "_run_payload": "artifacts",
    "_safe_document_content": "run_io",
    "_safe_watch_name": "briefs",
    "_seat_health_result_for": "orchestrator",
    "_seat_health_typed_cause": "orchestrator",
    "_slug": "run_io",
    "_supports_directory_fsync": "run_io",
    "_suspected_noop": "prompts",
    "_terminalize_run_lifecycle": "orchestrator",
    "_truncate_brief_text": "briefs",
    "_truncate_on_line_boundary": "briefs",
    "_unknown_covers": "planning",
    "_upstream_drift_reports_dir": "briefs",
    "_upstream_drift_state_path": "briefs",
    "_utc_iso": "run_io",
    "_validate_plan_dependencies": "planning",
    "_verify_receipt_payload": "prompts",
    "_verify_receipts_since": "prompts",
    "_with_patch_ref": "prompts",
    "_worker_event_writer": "planning",
    "_worker_prompt": "planning",
    "_write_json": "run_io",
    "_write_json_inner": "run_io",
    "_write_new_revision": "run_io",
    "_write_run_seat_health_receipt": "orchestrator",
    "_write_seat_routing_receipt": "orchestrator",
    "arbitrate_briefs": "briefs",
    "build_ground_truth": "prompts",
    "build_plan_prompt": "briefs",
    "build_synth_prompt": "prompts",
    "code_graph_brief": "briefs",
    "dispatch": "planning",
    "drift_impact_brief": "briefs",
    "make_run_dir": "run_io",
    "parse_plan": "planning",
    "plan": "planning",
    "record_approval_pause": "artifacts",
    "record_artifact_collection": "artifacts",
    "record_dispatch_stage": "artifacts",
    "record_result_processing": "artifacts",
    "record_run_start": "artifacts",
    "record_run_termination": "artifacts",
    "resolve_orchestrator_health_routing": "orchestrator",
    "run": "orchestrator",
    "set_artifact_patch_ref": "artifacts",
    "terminal_sigterm_handler": "orchestrator",
    "update_run_receipt": "artifacts",
    "write_approval_resume_handoff": "artifacts",
    "write_run_handoff": "run_io",
    "write_sidecar_revision": "run_io",
}


class _FacadeModule(ModuleType):
    def __getattr__(self, name: str) -> _Any:
        owner = _OWNERS.get(name)
        if owner is not None:
            return getattr(_sys.modules[f"{self.__name__}.{owner}"], name)
        shared_modules = _SHARED_IMPORTS.get(name)
        if shared_modules:
            return getattr(_sys.modules[f"{self.__name__}.{shared_modules[0]}"], name)
        raise AttributeError(f"module {self.__name__!r} has no attribute {name!r}")

    def __setattr__(self, name: str, value: _Any) -> None:
        owner = _OWNERS.get(name)
        if owner is not None:
            setattr(_sys.modules[f"{self.__name__}.{owner}"], name, value)
            return
        shared_modules = _SHARED_IMPORTS.get(name)
        if shared_modules:
            for module_name in shared_modules:
                setattr(_sys.modules[f"{self.__name__}.{module_name}"], name, value)
            return
        super().__setattr__(name, value)

    def __delattr__(self, name: str) -> None:
        owner = _OWNERS.get(name)
        if owner is not None:
            delattr(_sys.modules[f"{self.__name__}.{owner}"], name)
            return
        shared_modules = _SHARED_IMPORTS.get(name)
        if shared_modules:
            for module_name in shared_modules:
                delattr(_sys.modules[f"{self.__name__}.{module_name}"], name)
            return
        super().__delattr__(name)


for _module_name in _MODULE_NAMES:
    _importlib.import_module(f"{__name__}.{_module_name}")

_SHARED_IMPORTS: dict[str, tuple[str, ...]] = {
    name: tuple(module_name for module_name in _MODULE_NAMES if name in vars(_sys.modules[f"{__name__}.{module_name}"]))
    for name in _SHARED_IMPORT_NAMES
}

_sys.modules[__name__].__class__ = _FacadeModule
