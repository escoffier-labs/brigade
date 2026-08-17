"""Inspect Brigade run artifact directories."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

_NONTERMINAL_STATUSES = frozenset(
    {
        "started",
        "planning",
        "dispatching",
        "result-processing",
        "synthesizing",
        "handoff",
        "artifact-collection",
        "running",
    }
)
_SUCCESS_STATUSES = frozenset({"ok", "dry-run"})
_APPROVAL_REFERENCE_FIELDS = (
    "approval_id",
    "source",
    "fingerprint",
    "source_fingerprint",
    "contract_fingerprint",
    "evidence_fingerprint",
)
_APPROVAL_PAUSE_REQUEST_FIELD = "brigade_approval_pause_request"
_APPROVAL_PAUSE_REQUEST_SCHEMA = "brigade.approval_pause_request.v2"
_APPROVAL_PAUSE_REQUEST_FIELDS = frozenset(
    {
        "schema",
        "run_id",
        "run_dir_fingerprint",
        "owner_fingerprint",
        "correlation_fingerprint",
        "requester_worker",
        "requester_thread_id",
        "requester_turn_id",
        "approval_reference",
    }
)
_APPROVAL_CORRELATION_ENV = "BRIGADE_APPROVAL_CORRELATION"
_APPROVAL_RESUME_TOKEN_ENV = "BRIGADE_APPROVAL_RESUME_TOKEN"
_APPROVAL_MARKER_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_REQUESTER_COORDINATE_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class ApprovalResumeError(RuntimeError):
    """An approval-paused run cannot safely resume."""


class _ApprovalPauseReference(dict[str, str]):
    """Closed approval reference with its in-memory requester correlation."""

    requester_worker: str
    requester_thread_id: str
    requester_turn_id: str

    def __init__(
        self,
        reference: Mapping[str, str],
        *,
        requester_worker: str,
        requester_thread_id: str,
        requester_turn_id: str,
    ) -> None:
        super().__init__(reference)
        self.requester_worker = requester_worker
        self.requester_thread_id = requester_thread_id
        self.requester_turn_id = requester_turn_id


@dataclass(frozen=True)
class _ApprovalAuthorization:
    status: str
    decided_at: str | None
    consumed_by: str | None
    claimed_at: str | None = None


@dataclass(frozen=True)
class _ApprovalReservation:
    decided_at: str
    reserved_at: str
    token: str


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path.name} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return payload


def _read_text(path: Path) -> str | None:
    if not path.exists():
        return None
    return path.read_text().strip()


def _runs_root(cwd: Path, runs_dir: Path | None) -> tuple[Path | None, str | None]:
    cwd = cwd.expanduser().resolve()
    if not cwd.is_dir():
        return None, f"error: --cwd is not a directory: {cwd}"
    root = runs_dir.expanduser() if runs_dir is not None else cwd / ".brigade" / "runs"
    if not root.is_dir():
        return None, f"error: runs directory not found: {root}"
    return root, None


def _resolve_run_dir(run: str | Path, *, cwd: Path, runs_dir: Path | None = None) -> tuple[Path | None, str | None]:
    raw = Path(run).expanduser()
    if raw.is_absolute() or len(raw.parts) > 1 or raw.exists():
        run_dir = raw.resolve()
        if not run_dir.is_dir():
            return None, f"error: run directory not found: {run_dir}"
        return run_dir, None

    root, error = _runs_root(cwd, runs_dir)
    if error is not None:
        return None, error
    assert root is not None
    if str(run) == "latest":
        runs, skipped = _collect_runs(root)
        if skipped:
            print(f"skipped {skipped} invalid run director{'y' if skipped == 1 else 'ies'}", file=sys.stderr)
        if not runs:
            return None, f"error: no runs found in {root}"
        return runs[0][0].resolve(), None
    run_dir = (root / raw).resolve()
    if not run_dir.is_dir():
        return None, f"error: run directory not found: {run_dir}"
    return run_dir, None


def _line(label: str, value: object | None) -> None:
    if value not in (None, ""):
        print(f"{label}: {value}")


def _lineage(meta: Mapping[str, Any]) -> Mapping[str, Any] | None:
    lineage = meta.get("lineage")
    return lineage if isinstance(lineage, Mapping) else None


def _print_lineage(meta: Mapping[str, Any]) -> None:
    lineage = _lineage(meta)
    if lineage is None:
        return
    print("lineage:")
    _line("  kind", lineage.get("kind"))
    _line("  parent run", lineage.get("parent_run_id"))
    _line("  branch point", lineage.get("branch_point_event_id"))
    shared_prefix = lineage.get("shared_prefix")
    if not isinstance(shared_prefix, Mapping):
        return
    sequence = shared_prefix.get("event_sequence")
    digest = shared_prefix.get("event_digest")
    if isinstance(sequence, int) and isinstance(digest, str) and digest:
        print(f"  shared prefix: seq={sequence} digest={digest[:12]}")


def _print_roster(roster: dict[str, Any] | None) -> None:
    if not roster:
        return
    agents = roster.get("agents")
    print("roster:")
    _line("  orchestrator", roster.get("orchestrator"))
    _line("  max_workers", roster.get("max_workers"))
    _line("  timeout_seconds", roster.get("timeout_seconds"))
    allow_models = roster.get("allow_models")
    if isinstance(allow_models, list) and allow_models:
        print(f"  allow_models: {', '.join(str(item) for item in allow_models)}")
    if isinstance(agents, dict):
        for name, agent in agents.items():
            if not isinstance(agent, dict):
                continue
            marker = " (orchestrator)" if name == roster.get("orchestrator") else ""
            timeout = agent.get("timeout_seconds")
            timeout_text = f"; timeout={timeout:g}s" if isinstance(timeout, (int, float)) else ""
            print(f"  - {name}: {agent.get('cli', 'unknown')}{marker}{timeout_text}")


def _print_plan(plan: dict[str, Any] | None) -> None:
    assignments = plan.get("assignments") if plan else None
    if not isinstance(assignments, list):
        return
    print("plan:")
    if not assignments:
        print("  (no worker assignments)")
        return
    for assignment in assignments:
        if not isinstance(assignment, dict):
            continue
        print(f"  -> {assignment.get('worker', 'unknown')}: {assignment.get('task', '')}")


def _print_workers(worker_results: dict[str, Any] | None) -> None:
    results = worker_results.get("results") if worker_results else None
    if not isinstance(results, list):
        return
    print("workers:")
    if not results:
        print("  (none)")
        return
    for result in results:
        if not isinstance(result, dict):
            continue
        marker = "ok" if result.get("ok") else "failed"
        detail = f": {result.get('detail')}" if result.get("detail") else ""
        print(f"  [{marker}] {result.get('worker', 'unknown')}{detail}")


def _print_ground_truth(worker_results: dict[str, Any] | None) -> None:
    ground_truth = worker_results.get("ground_truth") if worker_results else None
    if not isinstance(ground_truth, dict):
        return
    if ground_truth.get("available") is not True:
        reason = ground_truth.get("reason")
        suffix = f" ({reason})" if isinstance(reason, str) and reason else ""
        print(f"ground truth: unavailable{suffix}")
        return
    print("ground truth:")
    changed = [item for item in ground_truth.get("changed_files") or [] if isinstance(item, str)]
    untracked = [item for item in ground_truth.get("untracked_files") or [] if isinstance(item, str)]
    print(f"  changed_files: {len(changed)}" + (f" ({', '.join(changed[:8])})" if changed else ""))
    if untracked:
        print(f"  untracked_files: {len(untracked)} ({', '.join(untracked[:8])})")
    diffstat = ground_truth.get("diffstat")
    if isinstance(diffstat, str) and diffstat.strip():
        for line in diffstat.strip().splitlines():
            print(f"  {line.strip()}")
    patch_ref = ground_truth.get("patch_ref")
    if isinstance(patch_ref, str) and patch_ref:
        print(f"  patch_ref: {patch_ref}")
    for receipt in ground_truth.get("verify_receipts") or []:
        if not isinstance(receipt, dict):
            continue
        print(f"  verify: {receipt.get('run_id', 'unknown')} {receipt.get('status', 'unknown')}")
        for command in receipt.get("commands") or []:
            if isinstance(command, dict):
                print(f"    - {command.get('command', '?')} exit={command.get('exit_code')}")


def _print_synthesis(synthesis: dict[str, Any] | None) -> None:
    if not synthesis:
        return
    result = synthesis.get("result")
    print("synthesis:")
    if isinstance(result, dict):
        marker = "ok" if result.get("ok") else "failed"
        detail = f": {result.get('detail')}" if result.get("detail") else ""
        print(f"  [{marker}] {synthesis.get('orchestrator', 'orchestrator')}{detail}")
    else:
        print(f"  {synthesis.get('orchestrator', 'orchestrator')}")


def _print_final(final_text: str | None) -> None:
    if final_text is None:
        return
    print("final:")
    if not final_text:
        print("  (empty)")
        return
    for line in final_text.splitlines():
        print(f"  {line}")


def _duration_text(value: object) -> str:
    if not isinstance(value, (int, float)):
        return "unknown duration"
    return f"{value:g}s"


def _artifact_signature(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _is_terminal(meta: dict[str, Any]) -> bool:
    status = meta.get("status")
    if meta.get("finished_at"):
        return True
    return isinstance(status, str) and status not in _NONTERMINAL_STATUSES


def _approval_resume_state(meta: Mapping[str, Any]) -> str | None:
    reference = meta.get("approval_reference")
    if not isinstance(reference, Mapping):
        return None
    state = reference.get("decision_state")
    if meta.get("status") == "paused" and state is None:
        return "pending"
    if meta.get("status") == "running" and isinstance(state, str) and state:
        return str(state)
    return None


def _is_intentional_approval_pause(meta: Mapping[str, Any]) -> bool:
    state = _approval_resume_state(meta)
    return state is not None and state != "consumed"


def _approval_needs_reconciliation(meta: Mapping[str, Any]) -> bool:
    return _approval_resume_state(meta) == "consumed"


def _approval_reference_from_record(source: str, record: Mapping[str, Any]) -> dict[str, str]:
    """Build the closed reference shape from an existing approval record."""
    approval_id: object
    source_fingerprint: object
    contract_fingerprint: object
    evidence_fingerprint: object
    fingerprint_fn: Callable[[Any], str]
    if source == "daily":
        from .daily_cmd import config as daily_config

        approval_id = record.get("approval_id")
        source_fingerprint = record.get("source_fingerprint")
        contract_fingerprint = record.get("config_fingerprint")
        evidence_fingerprint = daily_config._fingerprint(record.get("evidence_refs", []))
        fingerprint_fn = daily_config._fingerprint
    elif source == "tool":
        from .tools_cmd import helpers as tool_helpers

        approval_id = record.get("id")
        source_fingerprint = record.get("source_fingerprint") or tool_helpers._stable_hash({"source": None})
        contract_fingerprint = record.get("contract_fingerprint")
        evidence_fingerprint = record.get("call_fingerprint")
        fingerprint_fn = tool_helpers._stable_hash
    else:
        raise ApprovalResumeError(f"unsupported approval source: {source}")
    components = {
        "approval_id": approval_id,
        "source": source,
        "source_fingerprint": source_fingerprint,
        "contract_fingerprint": contract_fingerprint,
        "evidence_fingerprint": evidence_fingerprint,
    }
    if any(not isinstance(value, str) or not value for value in components.values()):
        raise ApprovalResumeError(f"{source} approval record is missing required fingerprints")
    reference = {
        **components,
        "fingerprint": fingerprint_fn(components),
    }
    return {key: str(value) for key, value in reference.items()}


def _reference_matches_record(reference: Mapping[str, str], record: Mapping[str, Any]) -> None:
    actual = _approval_reference_from_record(reference["source"], record)
    if any(reference.get(field) != actual[field] for field in _APPROVAL_REFERENCE_FIELDS):
        raise ApprovalResumeError("approval fingerprints changed after the run paused")


def _request_digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _active_run_request_binding(workspace: Path) -> tuple[Path, str] | None:
    """Return the live run and lock token visible to a child process."""
    from . import runguard

    workspace = workspace.expanduser().resolve()
    owner = runguard._read_lock_owner(runguard.lock_path(workspace))
    if owner is None:
        return None
    raw_run_dir = owner.get("run_dir")
    owner_token = owner.get("owner_token")
    if not isinstance(raw_run_dir, str) or not raw_run_dir:
        return None
    if not isinstance(owner_token, str) or not owner_token:
        return None
    try:
        run_dir = Path(raw_run_dir).expanduser().resolve()
    except (OSError, RuntimeError):
        return None
    if runguard.run_lock_state(workspace, run_dir) != "live":
        return None
    return run_dir, owner_token


def _approval_marker_from_env(name: str) -> str | None:
    value = os.environ.get(name)
    if not isinstance(value, str) or not _APPROVAL_MARKER_RE.fullmatch(value):
        return None
    return value


def _attach_approval_pause_request(workspace: Path, source: str, record: dict[str, Any]) -> bool:
    """Attach a bounded request to an existing approval artifact.

    The child never appends to the parent journal. The active owner later
    validates this request against its exact lock acquisition and records the
    pause itself.
    """
    binding = _active_run_request_binding(workspace)
    correlation_marker = _approval_marker_from_env(_APPROVAL_CORRELATION_ENV)
    if binding is None or correlation_marker is None:
        return False
    run_dir, owner_token = binding
    reference = _approval_reference_from_record(source, record)
    record[_APPROVAL_PAUSE_REQUEST_FIELD] = {
        "schema": _APPROVAL_PAUSE_REQUEST_SCHEMA,
        "run_id": run_dir.name,
        "run_dir_fingerprint": _request_digest(str(run_dir)),
        "owner_fingerprint": _request_digest(owner_token),
        "correlation_fingerprint": _request_digest(correlation_marker),
        "requester_worker": None,
        "requester_thread_id": None,
        "requester_turn_id": None,
        "approval_reference": reference,
    }
    return True


def _request_matches_active_binding(
    request: Mapping[str, Any],
    *,
    run_dir: Path,
    owner_token: str,
) -> bool:
    return (
        set(request) == _APPROVAL_PAUSE_REQUEST_FIELDS
        and request.get("schema") == _APPROVAL_PAUSE_REQUEST_SCHEMA
        and request.get("run_id") == run_dir.name
        and request.get("run_dir_fingerprint") == _request_digest(str(run_dir))
        and request.get("owner_fingerprint") == _request_digest(owner_token)
    )


def _unbound_request_for_update(
    workspace: Path,
    request: object,
    *,
    source: str,
    record: Mapping[str, Any],
    run_dir: Path,
    owner_token: str,
    correlation_fingerprint: str,
) -> dict[str, Any]:
    """Revalidate one scanned request under its source-store lock."""
    current_binding = _active_run_request_binding(workspace)
    if current_binding != (run_dir, owner_token):
        raise ApprovalResumeError("approval pause active binding changed before requester update")
    if (
        not isinstance(request, dict)
        or not _request_matches_active_binding(request, run_dir=run_dir, owner_token=owner_token)
        or request.get("correlation_fingerprint") != correlation_fingerprint
    ):
        raise ApprovalResumeError("approval pause request changed before requester update")
    raw_reference = request.get("approval_reference")
    if not isinstance(raw_reference, Mapping):
        raise ApprovalResumeError("approval pause request lost its approval reference")
    from . import run_lifecycle

    try:
        request_reference = run_lifecycle.normalize_approval_reference(raw_reference)
    except run_lifecycle.LifecycleJournalError as exc:
        raise ApprovalResumeError("approval pause request reference changed before requester update") from exc
    if request_reference != _approval_reference_from_record(source, record):
        raise ApprovalResumeError("approval pause request reference changed before requester update")
    coordinate_fields = ("requester_worker", "requester_thread_id", "requester_turn_id")
    if any(request.get(field) is not None for field in coordinate_fields):
        raise ApprovalResumeError("approval pause request is already bound")
    return request


def _bind_approval_pause_request(
    workspace: Path,
    *,
    correlation_marker: str,
    worker: str,
    thread_id: str,
    turn_id: str,
) -> None:
    """Atomically bind one child request to its completed command coordinates."""
    coordinates = (worker, thread_id, turn_id)
    if not _APPROVAL_MARKER_RE.fullmatch(correlation_marker) or any(
        not _REQUESTER_COORDINATE_RE.fullmatch(value) for value in coordinates
    ):
        raise ApprovalResumeError("approval pause requester binding is invalid")
    binding = _active_run_request_binding(workspace)
    if binding is None:
        raise ApprovalResumeError("approval pause requester binding requires an active run")
    run_dir, owner_token = binding
    correlation_fingerprint = _request_digest(correlation_marker)
    from .daily_cmd import approvals as daily_approvals
    from .tools_cmd import calls as tool_calls

    candidates: list[tuple[str, str]] = []
    daily_records, _ = daily_approvals._read_approvals(workspace)
    for daily_record in daily_records:
        request = daily_record.get(_APPROVAL_PAUSE_REQUEST_FIELD)
        if (
            isinstance(request, Mapping)
            and _request_matches_active_binding(request, run_dir=run_dir, owner_token=owner_token)
            and request.get("correlation_fingerprint") == correlation_fingerprint
        ):
            candidates.append(("daily", str(daily_record.get("approval_id") or "")))
    for tool_record in tool_calls._read_calls(workspace):
        request = tool_record.get(_APPROVAL_PAUSE_REQUEST_FIELD)
        if (
            isinstance(request, Mapping)
            and _request_matches_active_binding(request, run_dir=run_dir, owner_token=owner_token)
            and request.get("correlation_fingerprint") == correlation_fingerprint
        ):
            candidates.append(("tool", str(tool_record.get("id") or "")))
    if len(candidates) != 1 or not candidates[0][1]:
        raise ApprovalResumeError("approval pause correlation did not match exactly one request")
    source, approval_id = candidates[0]
    coordinate_fields = ("requester_worker", "requester_thread_id", "requester_turn_id")
    if source == "daily":
        with daily_approvals._approval_store_lock(workspace, approval_id):
            bound_daily_record = daily_approvals._find_approval(workspace, approval_id)
            if bound_daily_record is None:
                raise ApprovalResumeError("daily approval disappeared during requester binding")
            request = _unbound_request_for_update(
                workspace,
                bound_daily_record.get(_APPROVAL_PAUSE_REQUEST_FIELD),
                source=source,
                record=bound_daily_record,
                run_dir=run_dir,
                owner_token=owner_token,
                correlation_fingerprint=correlation_fingerprint,
            )
            request.update(dict(zip(coordinate_fields, coordinates, strict=True)))
            daily_approvals._write_approval_unlocked(workspace, bound_daily_record)
        return
    with tool_calls._calls_store_lock(workspace):
        bound_tool_record, calls, error = tool_calls._resolve_call(workspace, approval_id)
        if bound_tool_record is None:
            raise ApprovalResumeError(error or "tool approval disappeared during requester binding")
        request = _unbound_request_for_update(
            workspace,
            bound_tool_record.get(_APPROVAL_PAUSE_REQUEST_FIELD),
            source=source,
            record=bound_tool_record,
            run_dir=run_dir,
            owner_token=owner_token,
            correlation_fingerprint=correlation_fingerprint,
        )
        request.update(dict(zip(coordinate_fields, coordinates, strict=True)))
        tool_calls._write_calls_unlocked(workspace, calls)


def _request_reference_for_owned_run(
    request: object,
    *,
    source: str,
    record: Mapping[str, Any],
    run_dir: Path,
    owner_token: str,
) -> _ApprovalPauseReference | None:
    from . import run_lifecycle

    if not isinstance(request, Mapping):
        return None
    if request.get("run_id") != run_dir.name:
        return None
    if set(request) != _APPROVAL_PAUSE_REQUEST_FIELDS:
        raise ApprovalResumeError("approval pause request has an invalid shape")
    if request.get("schema") != _APPROVAL_PAUSE_REQUEST_SCHEMA:
        raise ApprovalResumeError("approval pause request has an unsupported schema")
    if request.get("run_dir_fingerprint") != _request_digest(str(run_dir)):
        raise ApprovalResumeError("approval pause request targets a different run directory")
    if request.get("owner_fingerprint") != _request_digest(owner_token):
        raise ApprovalResumeError("approval pause request targets a different lock acquisition")
    correlation_fingerprint = request.get("correlation_fingerprint")
    if not isinstance(correlation_fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", correlation_fingerprint):
        raise ApprovalResumeError("approval pause request has an invalid correlation fingerprint")
    requester_worker = request.get("requester_worker")
    requester_thread_id = request.get("requester_thread_id")
    requester_turn_id = request.get("requester_turn_id")
    if any(
        not isinstance(value, str) or not _REQUESTER_COORDINATE_RE.fullmatch(value)
        for value in (requester_worker, requester_thread_id, requester_turn_id)
    ):
        raise ApprovalResumeError("approval pause request is not bound to a valid requester")
    raw_reference = request.get("approval_reference")
    if not isinstance(raw_reference, Mapping):
        raise ApprovalResumeError("approval pause request has no approval reference")
    try:
        reference = run_lifecycle.normalize_approval_reference(raw_reference)
    except run_lifecycle.LifecycleJournalError as exc:
        raise ApprovalResumeError(f"approval pause request reference is invalid: {exc}") from exc
    if reference["source"] != source:
        raise ApprovalResumeError("approval pause request source does not match its artifact")
    _reference_matches_record(reference, record)
    assert isinstance(requester_worker, str)
    assert isinstance(requester_thread_id, str)
    assert isinstance(requester_turn_id, str)
    return _ApprovalPauseReference(
        reference,
        requester_worker=requester_worker,
        requester_thread_id=requester_thread_id,
        requester_turn_id=requester_turn_id,
    )


def _approval_pause_reference_for_owned_run(workspace: Path, run_dir: Path) -> _ApprovalPauseReference | None:
    """Resolve one child request while the current process owns the run lock."""
    from . import runguard
    from .daily_cmd import approvals as daily_approvals
    from .tools_cmd import calls as tool_calls

    workspace = workspace.expanduser().resolve()
    run_dir = run_dir.expanduser().resolve()
    if not runguard.is_active_run_owner(workspace, run_dir):
        raise ApprovalResumeError("approval pause request requires the active run owner")
    owner = runguard._read_lock_owner(runguard.lock_path(workspace))
    owner_token = owner.get("owner_token") if owner is not None else None
    if not isinstance(owner_token, str) or not owner_token:
        raise ApprovalResumeError("active run lock has no owner token")

    candidates: list[_ApprovalPauseReference] = []
    daily_records, _ = daily_approvals._read_approvals(workspace)
    for record in daily_records:
        reference = _request_reference_for_owned_run(
            record.get(_APPROVAL_PAUSE_REQUEST_FIELD),
            source="daily",
            record=record,
            run_dir=run_dir,
            owner_token=owner_token,
        )
        if reference is not None:
            candidates.append(reference)
    for record in tool_calls._read_calls(workspace):
        reference = _request_reference_for_owned_run(
            record.get(_APPROVAL_PAUSE_REQUEST_FIELD),
            source="tool",
            record=record,
            run_dir=run_dir,
            owner_token=owner_token,
        )
        if reference is not None:
            candidates.append(reference)
    if not candidates:
        return None
    unique = {
        (
            json.dumps(reference, sort_keys=True),
            reference.requester_worker,
            reference.requester_thread_id,
            reference.requester_turn_id,
        )
        for reference in candidates
    }
    if len(unique) != 1:
        raise ApprovalResumeError("multiple approval pause requests target the active run")
    return candidates[0]


def _daily_authorization(
    workspace: Path,
    reference: Mapping[str, str],
    *,
    run_id: str,
) -> _ApprovalAuthorization:
    from .daily_cmd import approvals as daily_approvals
    from .daily_cmd import config as daily_config

    approval = daily_approvals._find_approval(workspace, reference["approval_id"])
    if not isinstance(approval, dict):
        raise ApprovalResumeError(f"daily approval not found: {reference['approval_id']}")
    _reference_matches_record(reference, approval)
    status = str(approval.get("status") or "")
    decided_at = approval.get("reviewed_at")
    decided_text = decided_at if isinstance(decided_at, str) and decided_at else None
    consumed = approval.get("consumed_run_id")
    consumed_by = consumed if isinstance(consumed, str) and consumed else None
    claim = approval.get("approval_claim")
    if isinstance(claim, Mapping):
        claim_run_id = claim.get("run_id")
        claim_state = claim.get("state")
        claim_time = claim.get("redeemed_at") if claim_state == "redeemed" else claim.get("reserved_at")
        return _ApprovalAuthorization(
            status=str(claim_state or ""),
            decided_at=decided_text,
            consumed_by=claim_run_id if isinstance(claim_run_id, str) else None,
            claimed_at=claim_time if isinstance(claim_time, str) else None,
        )
    if status == "approved":
        config, _ = daily_config._load_config(workspace)
        blockers = daily_approvals._approval_blockers(workspace, approval, config)
        if blockers:
            raise ApprovalResumeError(f"daily approval is stale or blocked: {blockers[0]}")
        return _ApprovalAuthorization(
            status=status,
            decided_at=decided_text,
            consumed_by=None,
        )
    if status == "consumed" and consumed_by == run_id:
        config, _ = daily_config._load_config(workspace)
        revalidation = dict(approval)
        revalidation["status"] = "approved"
        revalidation["consumed_run_id"] = None
        blockers = daily_approvals._approval_blockers(workspace, revalidation, config)
        if blockers:
            raise ApprovalResumeError(f"daily approval is stale or blocked: {blockers[0]}")
    return _ApprovalAuthorization(
        status=status,
        decided_at=decided_text,
        consumed_by=consumed_by,
    )


def _tool_authorization(
    workspace: Path,
    reference: Mapping[str, str],
    *,
    run_id: str,
) -> _ApprovalAuthorization:
    from .tools_cmd import calls as tool_calls

    call, _calls, error = tool_calls._resolve_call(workspace, reference["approval_id"])
    if call is None:
        raise ApprovalResumeError(error or f"tool approval not found: {reference['approval_id']}")
    _reference_matches_record(reference, call)
    status = str(call.get("status") or "")
    decided_at = call.get("reviewed_at")
    decided_text = decided_at if isinstance(decided_at, str) and decided_at else None
    stored_run_id = call.get("run_id")
    consumed_by = stored_run_id if isinstance(stored_run_id, str) and stored_run_id else None
    claim = call.get("approval_claim")
    if isinstance(claim, Mapping):
        claim_run_id = claim.get("run_id")
        claim_state = claim.get("state")
        claim_time = claim.get("redeemed_at") if claim_state == "redeemed" else claim.get("reserved_at")
        return _ApprovalAuthorization(
            status=str(claim_state or ""),
            decided_at=decided_text,
            consumed_by=claim_run_id if isinstance(claim_run_id, str) else None,
            claimed_at=claim_time if isinstance(claim_time, str) else None,
        )
    if status == "approved":
        blockers = tool_calls._call_run_blockers(workspace, call)
        if blockers:
            raise ApprovalResumeError(f"tool approval is stale or blocked: {blockers[0]}")

        return _ApprovalAuthorization(
            status=status,
            decided_at=decided_text,
            consumed_by=None,
        )
    if status == "running":
        if consumed_by == run_id:
            revalidation = dict(call)
            revalidation["status"] = "approved"
            revalidation["run_id"] = None
            revalidation["started_at"] = None
            revalidation["completed_at"] = None
            blockers = tool_calls._call_run_blockers(workspace, revalidation)
            if blockers:
                raise ApprovalResumeError(f"tool approval is stale or blocked: {blockers[0]}")
        status = "consumed"
    return _ApprovalAuthorization(
        status=status,
        decided_at=decided_text,
        consumed_by=consumed_by,
    )


def _load_approval_authorization(
    workspace: Path,
    reference: Mapping[str, str],
    *,
    run_id: str,
) -> _ApprovalAuthorization:
    source = reference["source"]
    if source == "daily":
        return _daily_authorization(workspace, reference, run_id=run_id)
    if source == "tool":
        return _tool_authorization(workspace, reference, run_id=run_id)
    raise ApprovalResumeError(f"unsupported approval source: {source}")


def _reserve_approval(
    workspace: Path,
    reference: Mapping[str, str],
    *,
    run_id: str,
) -> _ApprovalReservation:
    token = secrets.token_urlsafe(32)
    token_fingerprint = _request_digest(token)
    source = reference["source"]
    if source == "daily":
        from .daily_cmd import approvals as daily_approvals

        try:
            claim = daily_approvals.reserve_for_run(
                workspace,
                reference["approval_id"],
                reference,
                run_id,
                token_fingerprint,
            )
        except daily_approvals.ApprovalClaimError as exc:
            raise ApprovalResumeError(str(exc)) from exc
        return _ApprovalReservation(
            decided_at=claim.decided_at,
            reserved_at=claim.claimed_at,
            token=token,
        )
    elif source == "tool":
        from .tools_cmd import calls as tool_calls

        try:
            tool_claim = tool_calls.reserve_for_run(
                workspace,
                reference["approval_id"],
                reference,
                run_id,
                token_fingerprint,
            )
        except tool_calls.CallClaimError as exc:
            raise ApprovalResumeError(str(exc)) from exc
        return _ApprovalReservation(
            decided_at=tool_claim.decided_at,
            reserved_at=tool_claim.claimed_at,
            token=token,
        )
    raise ApprovalResumeError(f"unsupported approval source: {source}")


def _record_approval_decision(
    run_dir: Path,
    workspace: Path,
    reference: Mapping[str, str],
    authorization: _ApprovalAuthorization,
) -> None:
    from . import run_lifecycle

    if authorization.decided_at is None:
        raise ApprovalResumeError("approval decision has no review timestamp")
    event_decision = "granted" if authorization.status == "approved" else authorization.status
    run_lifecycle.record_lifecycle_event(
        run_dir,
        event_type=f"approval.{event_decision}",
        payload={
            "approval_id": reference["approval_id"],
            "decided_at": authorization.decided_at,
            "decision_state": authorization.status,
        },
        idempotency_key=run_lifecycle.approval_idempotency_key(
            reference["approval_id"],
            event_decision,
        ),
        workspace=workspace,
    )


def _refresh_paused_snapshot(
    run_dir: Path,
    meta: Mapping[str, Any],
    reference: Mapping[str, str],
    *,
    decision_state: str = "pending",
    decided_at: str | None = None,
) -> None:
    from . import aboyeur, receipt_schema

    refreshed = dict(meta)
    refreshed["status"] = "running"
    refreshed_reference = {
        **reference,
        "decision_state": decision_state,
    }
    if decided_at is not None:
        refreshed_reference["decided_at"] = decided_at
    refreshed["approval_reference"] = refreshed_reference
    aboyeur._write_json(run_dir / "run.json", receipt_schema.stamp_run_receipt(refreshed))


def _refresh_resumed_snapshot(
    run_dir: Path,
    meta: Mapping[str, Any],
    reference: Mapping[str, str],
    *,
    decided_at: str | None,
    claimed_at: str,
) -> None:
    from . import aboyeur, receipt_schema

    refreshed = dict(meta)
    refreshed["status"] = "running"
    refreshed["status_started_at"] = claimed_at
    refreshed.pop("finished_at", None)
    refreshed.pop("duration_seconds", None)
    resumed_reference = {
        **reference,
        "decision_state": "consumed",
        "consuming_run_id": run_dir.name,
    }
    if decided_at is not None:
        resumed_reference["decided_at"] = decided_at
    refreshed["approval_reference"] = resumed_reference
    aboyeur._write_json(run_dir / "run.json", receipt_schema.stamp_run_receipt(refreshed))


def _commit_redeemed_approval_facts(
    run_dir: Path,
    workspace: Path,
    reference: Mapping[str, str],
    *,
    decided_at: str,
    claimed_at: str,
) -> None:
    """Commit outcome facts while the caller retains the source-store lock."""
    from . import run_lifecycle

    run_id = run_dir.name
    run_lifecycle.record_lifecycle_event(
        run_dir,
        event_type="approval.consumed",
        payload={
            "approval_id": reference["approval_id"],
            "consuming_run_id": run_id,
        },
        idempotency_key=run_lifecycle.approval_idempotency_key(
            reference["approval_id"],
            "consumed",
            scope=run_id,
        ),
        workspace=workspace,
    )
    run_lifecycle.record_lifecycle_event(
        run_dir,
        event_type="run.resumed",
        payload={"approval_id": reference["approval_id"]},
        idempotency_key=run_lifecycle.approval_idempotency_key(
            reference["approval_id"],
            "resumed",
            scope=run_id,
        ),
        workspace=workspace,
    )
    meta = _read_json(run_dir / "run.json")
    if meta is None:
        raise ApprovalResumeError("approval run receipt disappeared after resumed action")
    _refresh_resumed_snapshot(
        run_dir,
        meta,
        reference,
        decided_at=decided_at,
        claimed_at=claimed_at,
    )


def _finalize_redeemed_approval(
    run_dir: Path,
    workspace: Path,
    reference: Mapping[str, str],
) -> None:
    """Validate and commit outcome facts in one source-store transaction."""
    run_id = run_dir.name
    source = reference["source"]
    if source == "daily":
        from .daily_cmd import approvals as daily_approvals

        try:
            with daily_approvals.redeemed_reconciliation_guard(
                workspace,
                reference["approval_id"],
                reference,
                run_id,
            ) as claim:
                _commit_redeemed_approval_facts(
                    run_dir,
                    workspace,
                    reference,
                    decided_at=claim.decided_at,
                    claimed_at=claim.claimed_at,
                )
        except daily_approvals.ApprovalClaimError as exc:
            raise ApprovalResumeError(str(exc)) from exc
        return
    if source == "tool":
        from .tools_cmd import calls as tool_calls

        try:
            with tool_calls.redeemed_reconciliation_guard(
                workspace,
                reference["approval_id"],
                reference,
                run_id,
            ) as claim:
                _commit_redeemed_approval_facts(
                    run_dir,
                    workspace,
                    reference,
                    decided_at=claim.decided_at,
                    claimed_at=claim.claimed_at,
                )
        except tool_calls.CallClaimError as exc:
            raise ApprovalResumeError(str(exc)) from exc
        return
    raise ApprovalResumeError(f"unsupported approval source: {source}")


def resume(run_dir: Path) -> int:
    """Resume a legacy run or consume an approval before a paused continuation."""
    from . import run_lifecycle, run_resume, runguard

    run_dir = run_dir.expanduser().resolve()
    try:
        meta = _read_json(run_dir / "run.json")
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if meta is None or _approval_resume_state(meta) is None:
        return run_resume.resume(run_dir)
    raw_reference = meta.get("approval_reference")
    if not isinstance(raw_reference, Mapping):
        print("error: approval-paused run has no approval reference", file=sys.stderr)
        return 2
    try:
        reference = run_lifecycle.normalize_approval_reference(raw_reference)
    except run_lifecycle.LifecycleJournalError as exc:
        print(f"error: invalid approval reference: {exc}", file=sys.stderr)
        return 2
    workspace = runguard.resolve_run_lock_workspace(meta, run_dir)
    if workspace is None:
        print("error: run artifact has no workspace cwd; cannot acquire resume lock", file=sys.stderr)
        return 2
    state = runguard.run_lock_state(workspace, run_dir)
    if state != "absent":
        print(
            f"error: approval-paused run requires an absent lock before resume; found {state}",
            file=sys.stderr,
        )
        return 2
    try:
        with runguard.run_lock(workspace, run_dir=run_dir):
            current = _read_json(run_dir / "run.json")
            if current is None or _approval_resume_state(current) is None:
                raise ApprovalResumeError("approval-paused run changed before resume lock acquisition")
            current_raw_reference = current.get("approval_reference")
            if not isinstance(current_raw_reference, Mapping):
                raise ApprovalResumeError("approval-paused run lost its approval reference")
            current_reference = run_lifecycle.normalize_approval_reference(current_raw_reference)
            if current_reference != reference:
                raise ApprovalResumeError("approval reference changed before resume lock acquisition")
            current_decision_state = _approval_resume_state(current)
            if current_decision_state == "rejected":
                raise ApprovalResumeError("approval is rejected")
            authorization = _load_approval_authorization(
                workspace,
                reference,
                run_id=run_dir.name,
            )
            if authorization.status in {"rejected", "held"}:
                _record_approval_decision(run_dir, workspace, reference, authorization)
                _refresh_paused_snapshot(
                    run_dir,
                    current,
                    reference,
                    decision_state=authorization.status,
                    decided_at=authorization.decided_at,
                )
                raise ApprovalResumeError(f"approval is {authorization.status}")
            if authorization.status == "redeemed":
                _finalize_redeemed_approval(run_dir, workspace, reference)
                return 0
            if authorization.status in {"consumed", "reserved"}:
                if authorization.consumed_by != run_dir.name:
                    owner = authorization.consumed_by or "another run"
                    raise ApprovalResumeError(f"approval was already consumed by {owner}")
            elif authorization.status != "approved":
                raise ApprovalResumeError(f"approval is {authorization.status or 'pending'}")
            reservation = _reserve_approval(
                workspace,
                reference,
                run_id=run_dir.name,
            )
            _record_approval_decision(
                run_dir,
                workspace,
                reference,
                _ApprovalAuthorization(
                    status="approved",
                    decided_at=reservation.decided_at,
                    consumed_by=run_dir.name,
                ),
            )
            _refresh_paused_snapshot(
                run_dir,
                current,
                reference,
                decision_state="approved",
                decided_at=reservation.decided_at,
            )
            return run_resume._resume_locked(
                run_dir,
                approval_resume=True,
                approval_token=reservation.token,
            )
    except (
        ApprovalResumeError,
        ValueError,
        OSError,
        runguard.RunGuardError,
        run_lifecycle.LifecycleJournalError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def _failure_fields(meta: dict[str, Any]) -> tuple[object, object, object]:
    failure = meta.get("failure")
    failure_payload = failure if isinstance(failure, dict) else {}
    return (
        meta.get("failure_phase") or failure_payload.get("phase"),
        failure_payload.get("kind"),
        failure_payload.get("detail"),
    )


def _print_failure(meta: dict[str, Any]) -> None:
    phase, kind, detail = _failure_fields(meta)
    _line("failure phase", phase)
    _line("failure kind", kind)
    _line("failure detail", detail)


def _watch_return_code(status: object) -> int:
    if status in _SUCCESS_STATUSES:
        return 0
    return 1


def _emit_json(payload: dict[str, object]) -> None:
    print(json.dumps(payload, sort_keys=True))


def _emit_run(meta: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        phase, kind, detail = _failure_fields(meta)
        payload = {
            "type": "run",
            "status": meta.get("status"),
            "task": meta.get("task"),
            "started_at": meta.get("started_at"),
            "finished_at": meta.get("finished_at"),
            "duration_seconds": meta.get("duration_seconds"),
            "failure_phase": phase,
            "failure_kind": kind,
            "failure_detail": detail,
        }
        _emit_json({key: value for key, value in payload.items() if value is not None})
        return
    _line("status", meta.get("status"))
    _line("task", meta.get("task"))
    _print_failure(meta)


def _emit_plan(plan_payload: dict[str, Any], *, json_output: bool) -> None:
    assignments = plan_payload.get("assignments")
    if not isinstance(assignments, list):
        return
    if json_output:
        _emit_json({"type": "plan", "assignments": assignments})
        return
    print("plan:")
    if not assignments:
        print("  (no worker assignments)")
        return
    for assignment in assignments:
        if not isinstance(assignment, dict):
            continue
        stage = assignment.get("stage", 1)
        print(f"  stage {stage} -> {assignment.get('worker', 'unknown')}: {assignment.get('task', '')}")


def _event_item_type(event: dict[str, Any]) -> str:
    params = event.get("params")
    if not isinstance(params, dict):
        return ""
    item = params.get("item")
    if isinstance(item, dict) and isinstance(item.get("type"), str):
        return item["type"]
    turn = params.get("turn")
    if isinstance(turn, dict):
        return "turn"
    return ""


def _emit_event(worker: str, event: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        _emit_json({"type": "event", "worker": worker, "event": event})
        return
    method = event.get("method", "unknown")
    item_type = _event_item_type(event)
    suffix = f" {item_type}" if item_type else ""
    print(f"event: {worker} {method}{suffix}")


def _emit_workers(worker_results: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        _emit_json({"type": "workers", "results": worker_results.get("results") or []})
        return
    _print_workers(worker_results)


def _emit_synthesis(synthesis: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        _emit_json(
            {
                "type": "synthesis",
                "orchestrator": synthesis.get("orchestrator"),
                "result": synthesis.get("result"),
            }
        )
        return
    _print_synthesis(synthesis)


def _emit_final(final_text: str, *, json_output: bool) -> None:
    if json_output:
        _emit_json({"type": "final", "text": final_text})
        return
    _print_final(final_text)


def _emit_summary(run_dir: Path, meta: dict[str, Any], *, json_output: bool) -> None:
    status = str(meta.get("status") or "unknown")
    duration = meta.get("duration_seconds")
    if json_output:
        payload: dict[str, object] = {"type": "summary", "run": str(run_dir), "status": status}
        if isinstance(duration, (int, float)):
            payload["duration_seconds"] = duration
        phase, _, _ = _failure_fields(meta)
        if phase == "stale-lock-recovery":
            recovery_status = _lock_recovery_status(run_dir, meta)
            payload["failure_phase"] = phase
            payload["inspect_command"] = f"brigade runs show {run_dir}"
            payload["recover_status"] = recovery_status
            payload["resume_available"] = _resume_available(run_dir)
        _emit_json(payload)
        return
    print(f"summary: {status} in {_duration_text(duration)}")
    _print_terminal_guidance(run_dir, meta)


def _tail_events(run_dir: Path, offsets: dict[Path, int], *, json_output: bool) -> None:
    events_dir = run_dir / "events"
    if not events_dir.is_dir():
        return
    for path in sorted(events_dir.glob("*.jsonl")):
        try:
            size = path.stat().st_size
        except OSError:
            continue
        offset = offsets.get(path, 0)
        if size < offset:
            offset = 0
        try:
            with path.open() as fh:
                fh.seek(offset)
                for line in fh:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        event = json.loads(stripped)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(event, dict):
                        _emit_event(path.stem, event, json_output=json_output)
                offsets[path] = fh.tell()
        except OSError:
            continue


def _poll_watch_artifacts(
    run_dir: Path,
    signatures: dict[str, str],
    event_offsets: dict[Path, int],
    *,
    json_output: bool,
) -> tuple[dict[str, Any] | None, int | None]:
    try:
        run_meta = _read_json(run_dir / "run.json")
        if run_meta is None:
            print(f"error: run.json not found in {run_dir}", file=sys.stderr)
            return None, 2
        run_sig = _artifact_signature(run_meta)
        if signatures.get("run") != run_sig:
            _emit_run(run_meta, json_output=json_output)
            signatures["run"] = run_sig

        plan = _read_json(run_dir / "plan.json")
        if plan is not None:
            plan_sig = _artifact_signature(plan)
            if signatures.get("plan") != plan_sig:
                _emit_plan(plan, json_output=json_output)
                signatures["plan"] = plan_sig

        _tail_events(run_dir, event_offsets, json_output=json_output)

        worker_results = _read_json(run_dir / "worker-results.json")
        if worker_results is not None:
            workers_sig = _artifact_signature(worker_results)
            if signatures.get("workers") != workers_sig:
                _emit_workers(worker_results, json_output=json_output)
                signatures["workers"] = workers_sig

        synthesis = _read_json(run_dir / "synthesis.json")
        if synthesis is not None:
            synthesis_sig = _artifact_signature(synthesis)
            if signatures.get("synthesis") != synthesis_sig:
                _emit_synthesis(synthesis, json_output=json_output)
                signatures["synthesis"] = synthesis_sig

        final_text = _read_text(run_dir / "final.txt")
        if final_text is not None and signatures.get("final") != final_text:
            _emit_final(final_text, json_output=json_output)
            signatures["final"] = final_text
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None, 2
    return run_meta, None


def _short(text: object, limit: int = 72) -> str:
    rendered = " ".join(str(text or "").split())
    if len(rendered) <= limit:
        return rendered
    return rendered[: limit - 3].rstrip() + "..."


def _run_sort_key(item: tuple[Path, dict[str, Any]]) -> str:
    path, meta = item
    value = meta.get("started_at")
    return str(value) if value else path.name


def _collect_runs(root: Path) -> tuple[list[tuple[Path, dict[str, Any]]], int]:
    runs: list[tuple[Path, dict[str, Any]]] = []
    skipped = 0
    for child in root.iterdir():
        if not child.is_dir():
            continue
        try:
            meta = _read_json(child / "run.json")
        except ValueError:
            skipped += 1
            continue
        if meta is None:
            skipped += 1
            continue
        runs.append((child, meta))
    runs.sort(key=_run_sort_key, reverse=True)
    return runs, skipped


def _positive_timeout(value: object) -> float | None:
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    return None


def _run_timeout_seconds(run_dir: Path, meta: dict[str, Any]) -> float | None:
    try:
        roster = _read_json(run_dir / "roster.json")
    except (OSError, ValueError):
        return None
    if roster is None:
        return None
    default_timeout = _positive_timeout(roster.get("timeout_seconds"))
    agents = roster.get("agents")
    if not isinstance(agents, dict):
        return default_timeout

    status = meta.get("status")
    seats: list[str] = []
    if status in {"planning", "result-processing", "synthesizing", "handoff"}:
        orchestrator = roster.get("orchestrator")
        if isinstance(orchestrator, str):
            seats.append(orchestrator)
    elif isinstance(meta.get("worker"), str):
        seats.append(meta["worker"])
    elif status == "dispatching" and isinstance(meta.get("active_seats"), list):
        seats.extend(seat for seat in meta["active_seats"] if isinstance(seat, str))
    elif status == "started":
        orchestrator = roster.get("orchestrator")
        if isinstance(orchestrator, str):
            seats.append(orchestrator)
    else:
        try:
            plan = _read_json(run_dir / "plan.json")
        except (OSError, ValueError):
            plan = None
        if plan is not None:
            assignments = plan.get("assignments")
            if isinstance(assignments, list):
                seats.extend(
                    item["worker"]
                    for item in assignments
                    if isinstance(item, dict) and isinstance(item.get("worker"), str)
                )

    timeouts: list[float] = []
    for seat in seats:
        agent = agents.get(seat)
        seat_timeout = _positive_timeout(agent.get("timeout_seconds")) if isinstance(agent, dict) else None
        if seat_timeout is not None:
            timeouts.append(seat_timeout)
        elif default_timeout is not None:
            timeouts.append(default_timeout)
    return max(timeouts) if timeouts else default_timeout


def _stale_timeout(run_dir: Path, meta: dict[str, Any]) -> float | None:
    if _is_terminal(meta):
        return None
    timeout = _run_timeout_seconds(run_dir, meta)
    started_at = meta.get("status_started_at", meta.get("started_at"))
    if timeout is None or not isinstance(started_at, str):
        return None
    try:
        started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return timeout if time.time() - started.timestamp() > timeout else None


def list_runs(*, cwd: Path, runs_dir: Path | None = None, limit: int = 10) -> int:
    if limit < 1:
        print("error: --limit must be a positive integer", file=sys.stderr)
        return 2

    cwd = cwd.expanduser().resolve()
    if not cwd.is_dir():
        print(f"error: --cwd is not a directory: {cwd}", file=sys.stderr)
        return 2
    root = runs_dir.expanduser() if runs_dir is not None else cwd / ".brigade" / "runs"
    if not root.is_dir():
        print(f"error: runs directory not found: {root}", file=sys.stderr)
        return 2

    runs, skipped = _collect_runs(root)
    for path, meta in runs[:limit]:
        status = meta.get("status", "unknown")
        stale_timeout = _stale_timeout(path, meta)
        status_text = f"{status}; stale > {stale_timeout:g}s timeout" if stale_timeout is not None else str(status)
        started = meta.get("started_at", path.name)
        duration = meta.get("duration_seconds")
        duration_text = f" {duration:g}s" if isinstance(duration, (int, float)) else ""
        mode = " read-only" if meta.get("read_only") else ""
        if meta.get("dry_run"):
            mode += " dry-run"
        print(f"{started} [{status_text}]{duration_text}{mode} {path}")
        task = _short(meta.get("task"))
        if task:
            print(f"  {task}")
    if not runs:
        print(f"no runs found in {root}")
    if skipped:
        print(f"skipped {skipped} invalid run director{'y' if skipped == 1 else 'ies'}", file=sys.stderr)
    return 0


def _read_verified_parent_journal(run_dir: Path) -> tuple[list[Any] | None, str | None]:
    from . import run_journal, run_lifecycle

    journal_path = run_lifecycle._journal_path(run_dir)
    if not journal_path.is_file():
        return None, "parent run has no lifecycle journal"
    try:
        report = run_journal.read_journal_bounded(journal_path)
    except run_journal.RunJournalError as exc:
        return None, f"could not read parent lifecycle journal: {exc.diagnostic}"
    if report.partial_tail is not None:
        return None, "parent lifecycle journal has a partial trailing record"
    if report.chain_errors:
        return None, f"parent lifecycle journal is corrupt: {report.chain_errors[0]}"
    return report.events, None


def _supported_branch_snapshot(parent_run_dir: Path, branch_event: Any) -> tuple[dict[str, Any] | None, str | None]:
    from . import run_checkpoint, run_projector

    events, error = _read_verified_parent_journal(parent_run_dir)
    if error is not None:
        return None, error
    assert events is not None
    prefix = [event for event in events if event.sequence <= branch_event.sequence]
    checkpoints = [event for event in prefix if event.event_type == run_checkpoint.CHECKPOINT_EVENT_TYPE]
    checkpoint = checkpoints[-1] if checkpoints else None
    if checkpoint is None:
        return None, "unsupported branch point event: no verified checkpoint covers it"
    if branch_event.event_type == run_checkpoint.CHECKPOINT_EVENT_TYPE:
        return None, (
            "unsupported branch point event: branch from the checkpoint-covered "
            "lifecycle event, not the checkpoint itself"
        )
    paired_event_type = checkpoint.payload.get("paired_event_type")
    if (
        branch_event.sequence != checkpoint.sequence + 1
        or branch_event.event_type != paired_event_type
        or run_checkpoint._paired_event_derived_status(branch_event) is None
    ):
        return None, "unsupported branch point event: not a checkpoint-covered lifecycle state"
    try:
        checkpoint_bytes = run_checkpoint.validate_checkpoint(parent_run_dir, checkpoint)
        checkpoint_obj = run_checkpoint._parse_checkpoint_object(checkpoint_bytes)
        if run_checkpoint._paired_event_derived_status(branch_event) != checkpoint_obj.get("status"):
            return None, "unsupported branch point event: checkpoint status does not match the paired event"
        projection = run_projector.project_run_snapshot(checkpoint_obj, prefix, journal_present=True)
    except (run_checkpoint.CheckpointError, run_projector.ProjectionError) as exc:
        return None, f"unsupported branch point event: {exc}"
    return projection.snapshot, None


def _child_status_followup(status: str) -> tuple[str, dict[str, Any]] | None:
    from . import run_lifecycle

    if status == "started":
        return None
    event_type = run_lifecycle.STATUS_EVENT_TYPE.get(status)
    if event_type is None:
        return None
    if event_type in {"run.completed", "run.failed", "run.interrupted"}:
        return event_type, {"status": status, "detail": "branched child snapshot"}
    return event_type, {"detail": "branched child snapshot"}


def _absolute_path_under(value: object, parent: Path) -> bool:
    if not isinstance(value, str) or not value:
        return False
    path = Path(value)
    if not path.is_absolute():
        return False
    try:
        resolved = path.resolve()
        parent_resolved = parent.resolve()
    except OSError:
        return False
    return resolved == parent_resolved or parent_resolved in resolved.parents


def _rewrite_parent_owned_path(value: object, *, parent_run_dir: Path, child_run_dir: Path) -> str:
    parent_resolved = parent_run_dir.resolve()
    resolved = Path(str(value)).resolve()
    if resolved == parent_resolved:
        return str(child_run_dir)
    return str(child_run_dir / resolved.relative_to(parent_resolved))


def _child_receipt_from_parent_snapshot(
    snapshot: Mapping[str, Any],
    *,
    workspace: Path,
    parent_run_id: str,
    parent_run_dir: Path,
    child_run_dir: Path,
    branch_event: Any,
) -> dict[str, Any]:
    child = dict(snapshot)
    child["schema"] = child.get("schema") or "brigade.run.v1"
    child["schema_version"] = child.get("schema_version") or 1
    child["cwd"] = str(workspace)
    child["lock_workspace"] = str(workspace)
    created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    child["started_at"] = created_at
    child["status_started_at"] = created_at
    child.pop("projector_version", None)
    child.pop("journal_present", None)
    child.pop("journal_last_sequence", None)
    child.pop("journal_last_event_digest", None)
    child.pop("finished_at", None)
    child.pop("duration_seconds", None)
    child.pop("resumed_at", None)
    child.pop("recovery_history", None)
    child.pop("recovery_preserved_artifact", None)
    child.pop("control_transport", None)
    child.pop("control_socket", None)
    child.pop("active_stage", None)
    child.pop("active_seats", None)
    child.pop("phase_owner", None)
    child.pop("error", None)
    child.pop("failure", None)
    child.pop("failure_phase", None)
    child.pop("failure_kind", None)
    child.pop("worker_failure_summary", None)
    artifacts = child.get("artifacts")
    if _absolute_path_under(artifacts, parent_run_dir):
        child["artifacts"] = _rewrite_parent_owned_path(
            artifacts, parent_run_dir=parent_run_dir, child_run_dir=child_run_dir
        )
    if _absolute_path_under(child.get("handoff"), parent_run_dir):
        child.pop("handoff", None)
    child["lineage"] = {
        "kind": "child",
        "parent_run_id": parent_run_id,
        "branch_point_event_id": branch_event.event_id,
        "shared_prefix": {
            "event_sequence": branch_event.sequence,
            "event_digest": branch_event.event_digest,
            "previous_digest": branch_event.previous_digest,
        },
    }
    return child


def child(parent_run: str | Path, event_id: str, *, cwd: Path, runs_dir: Path | None = None) -> int:
    from . import (
        aboyeur,
        localio,
        receipt_schema,
        run_checkpoint,
        run_journal,
        run_lifecycle,
        run_projector,
        runguard,
    )

    parent_run_dir, error = _resolve_run_dir(parent_run, cwd=cwd, runs_dir=runs_dir)
    if error is not None:
        print(error, file=sys.stderr)
        return 2
    assert parent_run_dir is not None
    if not isinstance(event_id, str) or not event_id.strip():
        print("error: event_id must not be empty", file=sys.stderr)
        return 2
    try:
        parent_meta = _read_json(parent_run_dir / "run.json")
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if parent_meta is None:
        print(f"error: run.json not found in {parent_run_dir}", file=sys.stderr)
        return 2
    events, journal_error = _read_verified_parent_journal(parent_run_dir)
    if journal_error is not None:
        print(f"error: {journal_error}", file=sys.stderr)
        return 2
    assert events is not None
    branch_event = next((event for event in events if event.event_id == event_id), None)
    if branch_event is None:
        print(f"error: unknown branch point event: {event_id}", file=sys.stderr)
        return 2
    snapshot, snapshot_error = _supported_branch_snapshot(parent_run_dir, branch_event)
    if snapshot_error is not None:
        print(f"error: {snapshot_error}", file=sys.stderr)
        return 2
    assert snapshot is not None
    workspace = runguard.resolve_run_lock_workspace(parent_meta, parent_run_dir, fallback=cwd)
    if workspace is None:
        print("error: parent run has no lock workspace", file=sys.stderr)
        return 2
    runs_root = parent_run_dir.parent
    child_run_dir = aboyeur.make_run_dir(runs_root)
    child_meta = _child_receipt_from_parent_snapshot(
        snapshot,
        workspace=workspace,
        parent_run_id=parent_run_dir.name,
        parent_run_dir=parent_run_dir,
        child_run_dir=child_run_dir,
        branch_event=branch_event,
    )
    create_errors = (
        FileExistsError,
        OSError,
        ValueError,
        runguard.RunGuardError,
        run_lifecycle.LifecycleJournalError,
        run_projector.ProjectionError,
        run_checkpoint.CheckpointError,
        run_journal.RunJournalError,
    )
    try:
        target_status = str(child_meta.get("status") or "started")
        followup = _child_status_followup(target_status)
        if target_status != "started" and followup is None:
            print(f"error: unsupported branch point status: {target_status}", file=sys.stderr)
            return 2
        child_bootstrap = dict(child_meta)
        child_bootstrap["status"] = "started"
        with runguard.run_lock(workspace, run_dir=child_run_dir):
            try:
                child_run_dir.mkdir(parents=True, exist_ok=False)
                localio.write_json(child_run_dir / "run.json", receipt_schema.stamp_run_receipt(child_bootstrap))
                run_lifecycle.prepare_lifecycle_journal(child_run_dir, workspace=workspace)
                run_lifecycle.record_lifecycle_event(
                    child_run_dir,
                    event_type="run.created",
                    payload={"status": "started"},
                    idempotency_key=f"runs-child:{parent_run_dir.name}:{branch_event.event_id}",
                    workspace=workspace,
                )
                if followup is not None:
                    localio.write_json(child_run_dir / "run.json", receipt_schema.stamp_run_receipt(dict(child_meta)))
                    event_type, payload = followup
                    run_lifecycle.record_lifecycle_event(
                        child_run_dir,
                        event_type=event_type,
                        payload=payload,
                        idempotency_key=f"runs-child:{branch_event.event_id}:{event_type}",
                        workspace=workspace,
                    )
                report = run_journal.read_journal_bounded(run_lifecycle._journal_path(child_run_dir))
                if report.partial_tail is not None:
                    raise run_journal.RunJournalError("child lifecycle journal has a partial trailing record")
                if report.chain_errors:
                    raise run_journal.RunJournalError(report.chain_errors[0])
                projection = run_projector.project_run_snapshot(child_bootstrap, report.events, journal_present=True)
                localio.write_json(
                    child_run_dir / "run.json", receipt_schema.stamp_run_receipt(dict(projection.snapshot))
                )
            except create_errors:
                if child_run_dir.exists():
                    shutil.rmtree(child_run_dir, ignore_errors=True)
                raise
    except create_errors as exc:
        print(f"error: could not create child run: {exc}", file=sys.stderr)
        return 2
    print(f"child: {child_run_dir}")
    return 0


def show_latest(*, cwd: Path, runs_dir: Path | None = None) -> int:
    cwd = cwd.expanduser().resolve()
    if not cwd.is_dir():
        print(f"error: --cwd is not a directory: {cwd}", file=sys.stderr)
        return 2
    root = runs_dir.expanduser() if runs_dir is not None else cwd / ".brigade" / "runs"
    if not root.is_dir():
        print(f"error: runs directory not found: {root}", file=sys.stderr)
        return 2

    runs, skipped = _collect_runs(root)
    if skipped:
        print(f"skipped {skipped} invalid run director{'y' if skipped == 1 else 'ies'}", file=sys.stderr)
    if not runs:
        print(f"error: no runs found in {root}", file=sys.stderr)
        return 1
    return show(runs[0][0])


def _resume_available(run_dir: Path) -> bool:
    try:
        worker_results = _read_json(run_dir / "worker-results.json")
    except ValueError:
        return False
    results = worker_results.get("results") if worker_results else None
    if not isinstance(results, list):
        return False
    return any(
        isinstance(result, dict)
        and isinstance(result.get("thread_id"), str)
        and bool(result["thread_id"])
        and not result.get("ok")
        and result.get("status") in {"interrupted", "failed"}
        for result in results
    )


def _print_recovery_guidance(run_dir: Path) -> None:
    if _resume_available(run_dir):
        print(f"resume: brigade runs resume {run_dir}")
    else:
        print("resume: unavailable (no resumable app-server worker thread)")


def _lock_workspace(run_dir: Path, run_meta: dict[str, Any], *, fallback: Path | None = None) -> Path | None:
    from . import runguard

    return runguard.resolve_run_lock_workspace(run_meta, run_dir, fallback=fallback)


def _lock_recovery_status(run_dir: Path, run_meta: dict[str, Any]) -> str:
    workspace = _lock_workspace(run_dir, run_meta)
    if workspace is None:
        return "unknown"
    from . import runguard

    return runguard.run_recovery_status(workspace, run_dir)


def _concurrent_terminal_meta(run_dir: Path) -> dict[str, Any] | None:
    """Return the terminal run.json meta if a concurrent recovery already finished it.

    Used in the "run lock not found for run" fallback: another process claimed and
    terminalized the run while this recovery was racing for the lock. Uses
    ``_read_run_json_state`` so invalid UTF-8, deep JSON nesting (RecursionError),
    or a raw OSError stay bounded (the caller returns rc=2) instead of escaping as
    raw exceptions from ``_read_json``.
    """
    parseable, meta, _error, _oserror = _read_run_json_state(run_dir)
    if parseable and meta is not None and _is_terminal(meta):
        return meta
    return None


def _print_terminal_guidance(run_dir: Path, run_meta: dict[str, Any]) -> None:
    phase, _, _ = _failure_fields(run_meta)
    if phase != "stale-lock-recovery":
        return
    print(f"inspect: brigade runs show {run_dir}")
    recovery_status = _lock_recovery_status(run_dir, run_meta)
    if recovery_status == "cleared":
        print("recover: completed (stale lock cleared)")
    elif recovery_status == "required":
        print("recover: required (stale lock remains)")
    else:
        print("recover: unknown (workspace or lock metadata unavailable)")
    _print_recovery_guidance(run_dir)


def _journal_active(run_dir: Path) -> bool:
    return (run_dir / "events" / "lifecycle.jsonl").is_file()


def _print_pending_dispatch_recovery(run_dir: Path) -> None:
    """Surface unobserved external dispatches as at-least-once recovery work."""
    from . import run_journal, run_lifecycle

    try:
        events = run_journal.read_journal_bounded(run_lifecycle._journal_path(run_dir)).events
    except (OSError, run_journal.RunJournalError):
        return
    for seat, attempt in run_lifecycle.pending_dispatch_requests(events):
        attempt_text = str(attempt) if attempt is not None else "unknown"
        print(f"dispatch recovery: at-least-once work required (seat={seat}, attempt={attempt_text})")


def _read_run_json_state(run_dir: Path) -> tuple[bool, dict[str, Any] | None, str | None, bool]:
    """Return (parseable, run_meta, read_error, read_oserror) for run.json.

    ``parseable`` is True only when run.json exists and parses to a dict;
    ``run_meta`` is that dict or None; ``read_error`` is a bounded message
    describing why run.json could not be read (missing or corrupt).
    When ``read_oserror`` is True a raw ``OSError`` prevented reading
    run.json and recovery must fail closed before claiming the lock.
    """
    run_json = run_dir / "run.json"
    if not run_json.exists():
        return False, None, f"run.json not found in {run_dir}", False
    try:
        text = run_json.read_text(encoding="utf-8")
    except OSError:
        return False, None, "could not read run.json", True
    except UnicodeDecodeError as exc:
        return False, None, f"run.json is not valid UTF-8: {exc}", False
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        return False, None, f"run.json is not valid JSON: {exc}", False
    except RecursionError:
        return False, None, "run.json JSON nesting too deep", False
    if not isinstance(payload, dict):
        return False, None, "run.json must contain a JSON object", False
    return True, payload, None, False


def _state_refusal_message(state: str, workspace: Path) -> str:
    from . import runguard

    lock = runguard.lock_path(workspace)
    if state == "live":
        return f"error: run owner process is still active: {lock}"
    if state == "foreign":
        return f"error: run lock belongs to a different run: {lock}"
    return f"error: run lock is invalid: {lock}"


def _recover_terminal(run_dir: Path, run_meta: dict[str, Any], cwd: Path) -> int:
    from . import runguard

    phase, _, _ = _failure_fields(run_meta)
    if phase == "stale-lock-recovery":
        workspace = _lock_workspace(run_dir, run_meta, fallback=cwd)
        if workspace is None:
            print(f"error: recovered run artifact has no workspace cwd: {run_dir}", file=sys.stderr)
            return 2
        # An activated journal requires an explicit before_terminalize callback
        # before runguard will clear the claimed lock; the run is already
        # terminal so a no-op callback is sufficient (no run.json restoration).
        callback: Any = None
        if _journal_active(run_dir):
            callback = lambda owner: None  # noqa: E731  (intentional no-op)
        try:
            runguard.recover_stale_run(workspace, run_dir, required=False, before_terminalize=callback)
        except runguard.RunLockError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    print(f"already terminal: {run_dir} [{run_meta.get('status', 'unknown')}]")
    _print_recovery_guidance(run_dir)
    return 0


def _recover_from_checkpoint(
    run_dir: Path,
    workspace: Path,
    parseable: bool,
    run_meta: dict[str, Any] | None,
    read_error: str | None,
) -> int:
    from . import run_checkpoint, runguard

    # runguard wraps every before_terminalize callback exception into a
    # RunLockError after restoring the claimed lock, so a CheckpointError raised
    # inside the callback can never be caught directly here. Preserve the
    # original bounded CheckpointError in this holder so the RunLockError
    # handler below can surface the specific checkpoint reason instead of the
    # generic runguard "before_terminalize callback failed" wrapper.
    checkpoint_failure: run_checkpoint.CheckpointError | None = None

    def before_terminalize(owner: dict[str, object] | None) -> None:
        nonlocal checkpoint_failure
        try:
            run_checkpoint.recover_from_checkpoint(run_dir, run_meta if parseable else None)
        except run_checkpoint.CheckpointError as exc:
            checkpoint_failure = exc
            raise

    try:
        runguard.recover_stale_run(workspace, run_dir, before_terminalize=before_terminalize)
    except runguard.RunLockError as exc:
        if checkpoint_failure is not None:
            print(f"error: {checkpoint_failure}", file=sys.stderr)
            return 2
        if str(exc).startswith("run lock not found for run:"):
            concurrent_meta = _concurrent_terminal_meta(run_dir)
            if concurrent_meta is not None:
                print(f"already terminal: {run_dir} [{concurrent_meta.get('status', 'unknown')}]")
                _print_recovery_guidance(run_dir)
                return 0
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"recovered: {run_dir}")
    _print_recovery_guidance(run_dir)
    return 0


def _recover_legacy(
    run_dir: Path,
    workspace: Path,
    parseable: bool,
    run_meta: dict[str, Any] | None,
    read_error: str | None,
) -> int:
    from . import runguard

    try:
        runguard.recover_stale_run(workspace, run_dir)
    except runguard.RunLockError as exc:
        if str(exc).startswith("run lock not found for run:"):
            concurrent_meta = _concurrent_terminal_meta(run_dir)
            if concurrent_meta is not None:
                print(f"already terminal: {run_dir} [{concurrent_meta.get('status', 'unknown')}]")
                _print_recovery_guidance(run_dir)
                return 0
            detail = read_error if not parseable else str(exc)
        else:
            detail = str(exc)
        print(f"error: {detail}", file=sys.stderr)
        return 2
    if not parseable:
        try:
            recovered_meta = _read_json(run_dir / "run.json")
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if recovered_meta is None:
            print(f"error: run.json not found in {run_dir} after recovery", file=sys.stderr)
            return 2
    print(f"recovered: {run_dir}")
    _print_recovery_guidance(run_dir)
    return 0


def recover(run: str | Path, *, cwd: Path, runs_dir: Path | None = None) -> int:
    run_dir, error = _resolve_run_dir(run, cwd=cwd, runs_dir=runs_dir)
    if error is not None:
        print(error, file=sys.stderr)
        return 2
    assert run_dir is not None

    parseable, run_meta, read_error, read_oserror = _read_run_json_state(run_dir)
    if read_oserror:
        print(f"error: {read_error}", file=sys.stderr)
        return 2

    # Terminal runs keep the legacy behavior: tolerate foreign locks and only
    # clear a matching stale lock when the failure phase is stale-lock-recovery.
    if parseable and run_meta is not None and _is_terminal(run_meta):
        return _recover_terminal(run_dir, run_meta, cwd)

    # Entry gate for recovery (nonterminal or missing/corrupt run.json). Refuse
    # live current/foreign/invalid locks without claiming or mutating anything.
    workspace_meta: dict[str, Any] = run_meta if parseable and run_meta is not None else {}
    workspace = _lock_workspace(run_dir, workspace_meta, fallback=cwd)
    if workspace is None:
        print(f"error: run artifact has no workspace cwd: {run_dir}", file=sys.stderr)
        return 2

    from . import runguard

    state = runguard.run_lock_state(workspace, run_dir)
    if parseable and run_meta is not None and _approval_resume_state(run_meta) is not None:
        if state == "live":
            print(_state_refusal_message(state, workspace), file=sys.stderr)
            return 2
        if _approval_needs_reconciliation(run_meta):
            print(
                f"error: approval resume requires reconciliation; run `brigade runs resume {run_dir}`",
                file=sys.stderr,
            )
        else:
            print(
                f"error: approval wait is intentional; run `brigade runs resume {run_dir}` after review",
                file=sys.stderr,
            )
        return 2
    if state in {"foreign", "invalid"}:
        # A foreign or invalid current lock must not block recovery of a
        # matching persistent .stale claim: runguard.recover_stale_run skips
        # the live lock without mutating it and recovers only the matching
        # claim. When no matching claim exists, keep the existing refusal
        # and no-mutation behavior. Live (active-owner) locks always refuse
        # so same-run ownership stays intact.
        if not runguard.has_matching_stale_claim(workspace, run_dir):
            print(_state_refusal_message(state, workspace), file=sys.stderr)
            return 2
    elif state == "live":
        print(_state_refusal_message(state, workspace), file=sys.stderr)
        return 2

    if _journal_active(run_dir):
        result = _recover_from_checkpoint(run_dir, workspace, parseable, run_meta, read_error)
        if result == 0:
            _print_pending_dispatch_recovery(run_dir)
        return result
    return _recover_legacy(run_dir, workspace, parseable, run_meta, read_error)


def redact(
    run: str | Path,
    *,
    cwd: Path,
    runs_dir: Path | None = None,
    sequence_start: int | None = None,
    sequence_end: int | None = None,
    reason: str | None = None,
    operator_confirmed: bool = False,
    cleanup_operation: str | None = None,
) -> int:
    """Run the explicit operator procedure for lifecycle journal redaction."""
    run_dir, error = _resolve_run_dir(run, cwd=cwd, runs_dir=runs_dir)
    if error is not None:
        print(error, file=sys.stderr)
        return 2
    assert run_dir is not None

    from . import run_redaction

    try:
        if cleanup_operation is not None:
            if sequence_start is not None or sequence_end is not None or reason is not None:
                raise run_redaction.RedactionError("cleanup cannot include a sequence range or reason code")
            report = run_redaction.cleanup_redaction_quarantine(
                run_dir,
                operation_id=cleanup_operation,
                operator_confirmed=operator_confirmed,
            )
            print(f"redaction cleanup: {report.operation_id}")
            print("quarantine: removed")
            return 0
        if sequence_start is None or sequence_end is None or reason is None:
            raise run_redaction.RedactionError("redaction requires a sequence range and reason code")
        report = run_redaction.redact_journal(
            run_dir,
            sequence_start=sequence_start,
            sequence_end=sequence_end,
            reason=reason,
            operator_confirmed=operator_confirmed,
        )
    except run_redaction.RedactionError as exc:
        print(f"error: redaction failed: {exc.diagnostic}", file=sys.stderr)
        return 2
    print(f"redaction: {report.operation_id}")
    print(f"sequences: {report.sequence_start}-{report.sequence_end}")
    print(f"record: {report.record_path}")
    if report.cleaned:
        print("quarantine: removed")
    else:
        print(f"quarantine: retained at {report.quarantine_path}")
    return 0


_TERMINAL_LIFECYCLE_EVENT_TYPES = frozenset(
    {
        "run.completed",
        "run.failed",
        "run.interrupted",
    }
)


def _lifecycle_event_record(event: Any) -> dict[str, object]:
    from . import run_event_cursor

    payload = event.to_dict()
    payload["cursor"] = run_event_cursor.encode(
        run_event_cursor.DecodedCursor(
            schema=run_event_cursor.SUPPORTED_SCHEMA,
            run_id=event.run_id,
            sequence=event.sequence,
            digest=event.event_digest,
        )
    )
    return payload


def _events_return_code(event: Any) -> int:
    if event.event_type == "run.completed":
        status = event.payload.get("status") if isinstance(event.payload, Mapping) else None
        return 0 if status in _SUCCESS_STATUSES else 1
    return 1


def events(
    run: str | Path,
    *,
    cwd: Path,
    runs_dir: Path | None = None,
    after: str | None = None,
    follow: bool = False,
    interval: float = 1.0,
) -> int:
    """Emit verified lifecycle journal records as newline-delimited JSON (issue #604)."""
    from . import run_event_cursor, run_journal, run_lifecycle

    if interval < 0:
        print("error: --interval must be non-negative", file=sys.stderr)
        return 2
    run_dir, error = _resolve_run_dir(run, cwd=cwd, runs_dir=runs_dir)
    if error is not None:
        print(error, file=sys.stderr)
        return 2
    assert run_dir is not None

    journal_path = run_lifecycle._journal_path(run_dir)
    if not journal_path.is_file():
        print("error: legacy-no-journal", file=sys.stderr)
        return 2

    expected_run_id = run_lifecycle._run_id_from_dir(run_dir)
    after_sequence = 0
    last_digest: str | None = None
    if after is not None:
        try:
            cursor = run_event_cursor.decode(after)
        except run_event_cursor.CursorError as exc:
            print(f"error: {exc.code}: {exc}", file=sys.stderr)
            return 2
        if cursor.run_id != expected_run_id:
            print("error: cursor_run_id_mismatch: cursor run_id does not match the run", file=sys.stderr)
            return 2
        try:
            report = run_journal.read_journal_bounded(journal_path)
        except run_journal.RunJournalError as exc:
            print(f"error: {exc.diagnostic}", file=sys.stderr)
            return 2
        if report.partial_tail is not None:
            print("error: lifecycle journal ends in a partial line", file=sys.stderr)
            return 2
        if report.chain_errors:
            print(f"error: {report.chain_errors[0]}", file=sys.stderr)
            return 2
        match = next((event for event in report.events if event.sequence == cursor.sequence), None)
        if match is None:
            print("error: cursor_sequence_mismatch: cursor sequence is not in the journal", file=sys.stderr)
            return 2
        try:
            run_event_cursor.validate(
                cursor,
                expected_run_id=expected_run_id,
                event_sequence=match.sequence,
                event_digest=match.event_digest,
            )
        except run_event_cursor.CursorError as exc:
            print(f"error: {exc.code}: {exc}", file=sys.stderr)
            return 2
        after_sequence = cursor.sequence
        last_digest = cursor.digest

    last_emitted = after_sequence
    while True:
        try:
            report = run_journal.read_journal_bounded(journal_path)
        except run_journal.RunJournalError as exc:
            print(f"error: {exc.diagnostic}", file=sys.stderr)
            return 2
        if report.partial_tail is not None:
            print("error: lifecycle journal ends in a partial line", file=sys.stderr)
            return 2
        if report.chain_errors:
            print(f"error: {report.chain_errors[0]}", file=sys.stderr)
            return 2

        if last_emitted:
            retained = next((event for event in report.events if event.sequence == last_emitted), None)
            if retained is None or retained.event_digest != last_digest:
                print("error: cursor continuity changed in an emitted journal prefix", file=sys.stderr)
                return 2

        terminal_event = None
        first_suffix = True
        for event in report.events:
            if event.sequence <= last_emitted:
                continue
            if first_suffix and last_digest is not None and event.previous_digest != last_digest:
                print("error: cursor continuity failed for journal suffix", file=sys.stderr)
                return 2
            _emit_json(_lifecycle_event_record(event))
            last_emitted = event.sequence
            last_digest = event.event_digest
            first_suffix = False
            if terminal_event is None and event.event_type in _TERMINAL_LIFECYCLE_EVENT_TYPES:
                terminal_event = event

        if terminal_event is not None:
            return _events_return_code(terminal_event)
        if not follow:
            return 0
        time.sleep(interval)


def watch(
    run: str | Path,
    *,
    cwd: Path,
    runs_dir: Path | None = None,
    json_output: bool = False,
    interval: float = 1.0,
) -> int:
    if interval < 0:
        print("error: --interval must be non-negative", file=sys.stderr)
        return 2
    run_dir, error = _resolve_run_dir(run, cwd=cwd, runs_dir=runs_dir)
    if error is not None:
        print(error, file=sys.stderr)
        return 2
    assert run_dir is not None
    if json_output:
        _emit_json({"type": "watch", "run": str(run_dir)})
    else:
        print(f"watching: {run_dir}")

    signatures: dict[str, str] = {}
    event_offsets: dict[Path, int] = {}
    summary_emitted = False
    while True:
        run_meta, rc = _poll_watch_artifacts(
            run_dir,
            signatures,
            event_offsets,
            json_output=json_output,
        )
        if rc is not None:
            return rc
        assert run_meta is not None
        if run_meta.get("status") == "artifact-collection":
            workspace = _lock_workspace(run_dir, run_meta, fallback=cwd)
            if workspace is not None:
                from . import runguard

                try:
                    if runguard.recover_stale_run(workspace, run_dir, required=False):
                        continue
                    refreshed = _read_json(run_dir / "run.json")
                    if refreshed is not None and _is_terminal(refreshed):
                        continue
                    print(
                        "error: artifact-collection run has no matching recoverable lock",
                        file=sys.stderr,
                    )
                    return 2
                except runguard.RunLockError as exc:
                    detail = str(exc)
                    if "owner process is still active" not in detail and "recovery is still active" not in detail:
                        print(f"error: artifact-collection recovery failed: {detail}", file=sys.stderr)
                        return 2
        approval_state = _approval_resume_state(run_meta)
        if approval_state is not None:
            workspace = _lock_workspace(run_dir, run_meta, fallback=cwd)
            if workspace is None:
                print(f"error: approval run has no workspace cwd: {run_dir}", file=sys.stderr)
                return 2
            from . import runguard

            lock_state = runguard.run_lock_state(workspace, run_dir)
            if lock_state == "live":
                time.sleep(interval)
                continue
            if _approval_needs_reconciliation(run_meta):
                print(
                    f"error: approval resume requires reconciliation; run `brigade runs resume {run_dir}`",
                    file=sys.stderr,
                )
                return 2
            if lock_state != "absent":
                print(
                    f"error: approval wait has unexpected run lock state {lock_state}",
                    file=sys.stderr,
                )
                return 2
            if not summary_emitted:
                _emit_summary(run_dir, run_meta, json_output=json_output)
                summary_emitted = True
            return _watch_return_code(run_meta.get("status"))
        if _is_terminal(run_meta):
            if not summary_emitted:
                _emit_summary(run_dir, run_meta, json_output=json_output)
                summary_emitted = True
            return _watch_return_code(run_meta.get("status"))
        time.sleep(interval)


def audit(
    run: str | Path,
    *,
    cwd: Path,
    runs_dir: Path | None = None,
    json_output: bool = False,
) -> int:
    """Offline coordinator decision audit (#595). Thin wrapper over run_audit."""
    run_dir, error = _resolve_run_dir(run, cwd=cwd, runs_dir=runs_dir)
    if error is not None:
        print(error, file=sys.stderr)
        return 2
    assert run_dir is not None

    from . import run_audit

    report = run_audit.audit_run(run_dir)
    receipt = report.to_receipt()
    if json_output:
        print(json.dumps(receipt, indent=2, sort_keys=True))
    else:
        print(f"audit: {report.result}")
        print(f"run: {report.source_run_id}")
        print(f"projector_version: {report.projector_version}")
        print(f"code_revision: {report.code_revision}")
        if report.not_auditable_reason:
            print(f"not_auditable: {report.not_auditable_reason}")
        if report.first_divergence is not None:
            print(f"divergence_class: {report.first_divergence.divergence_class}")
            print(f"divergence: {report.first_divergence.detail}")
        transport = report.transport_coverage.get("transport")
        if transport is not None:
            print(f"transport: {transport}")
        coverage = report.transport_coverage.get("coverage")
        if isinstance(coverage, dict) and isinstance(coverage.get("note"), str):
            print(f"coverage: {coverage['note']}")
    if report.result == run_audit.RESULT_MATCH:
        return 0
    if report.result == run_audit.RESULT_NOT_AUDITABLE:
        return 2
    return 1


def show(run_dir: Path) -> int:
    run_dir = run_dir.expanduser()
    if not run_dir.is_dir():
        print(f"error: run directory not found: {run_dir}", file=sys.stderr)
        return 2

    try:
        run_meta = _read_json(run_dir / "run.json")
        if run_meta is None:
            print(f"error: run.json not found in {run_dir}", file=sys.stderr)
            return 2
        roster = _read_json(run_dir / "roster.json")
        plan = _read_json(run_dir / "plan.json")
        worker_results = _read_json(run_dir / "worker-results.json")
        synthesis = _read_json(run_dir / "synthesis.json")
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"run: {run_dir}")
    _line("status", run_meta.get("status"))
    _line("task", run_meta.get("task"))
    _line("cwd", run_meta.get("cwd"))
    mode = "read-only" if run_meta.get("read_only") else "normal"
    if run_meta.get("dry_run"):
        mode = f"{mode}, dry-run"
    print(f"mode: {mode}")
    _line("started", run_meta.get("started_at"))
    _line("finished", run_meta.get("finished_at"))
    duration = run_meta.get("duration_seconds")
    if isinstance(duration, (int, float)):
        print(f"duration: {duration:g}s")
    _line("artifacts", run_meta.get("artifacts"))
    _line("handoff", run_meta.get("handoff"))
    _line("error", run_meta.get("error"))
    _print_failure(run_meta)
    if run_meta.get("suspected_noop") is True:
        print("warning: suspected no-op run; ok workers produced no non-.brigade file changes.")

    _print_roster(roster)
    _print_plan(plan)
    _print_workers(worker_results)
    _print_ground_truth(worker_results)
    _print_synthesis(synthesis)
    _print_final(_read_text(run_dir / "final.txt"))
    _print_lineage(run_meta)
    _print_terminal_guidance(run_dir, run_meta)
    if _is_terminal(run_meta):
        return _watch_return_code(run_meta.get("status"))
    return 0
