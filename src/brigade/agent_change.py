"""Agent-change evidence index emitter (issue #1404, slice 2)."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import (
    agent_request,
    approval,
    approval_v2,
    attestation,
    attestation_input,
    localio,
    run_journal,
)
from .agent_change_refs import (
    AgentChangeError,
    _HEX40_OR_64_RE,
    _build_test_result_references,
    _decode_envelope_payload,
    _extract_baseline,
    _extract_tree,
    _load_json_object,
    _predicate_version,
    _verify_reference_envelope,
)

AGENT_CHANGE_PREDICATE_TYPE = "https://brigade.dev/attestation/agent-change/v1"
AGENT_CHANGE_POLICY_SCHEMA = "brigade.agent_change_policy.v1"

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_APPROVAL_NONCE_RE = re.compile(r"^[0-9a-f]{32}$")
_APPROVAL_FILENAME_RE = re.compile(r"^[0-9a-f]{32}\.json$")

_POLICY_REQUIRED_KEYS = frozenset(
    {
        "schema",
        "schema_version",
        "project_scope",
        "required_references",
        "allowed_profiles",
        "policy_version",
    }
)


def default_policy_path(target: Path) -> Path:
    return target.expanduser().resolve() / ".brigade" / "attestation" / "agent-change-policy.json"


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _policy_digest(policy: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json_bytes(policy)).hexdigest()


def _validate_policy(policy: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(policy, dict):
        raise AgentChangeError("agent-change policy is not a JSON object")
    if set(policy) != _POLICY_REQUIRED_KEYS:
        raise AgentChangeError("agent-change policy has an invalid key set")
    if policy.get("schema") != AGENT_CHANGE_POLICY_SCHEMA:
        raise AgentChangeError("agent-change policy schema is invalid")
    if policy.get("schema_version") != 1:
        raise AgentChangeError("agent-change policy schema_version is invalid")
    scope = policy.get("project_scope")
    if not isinstance(scope, str) or not scope:
        raise AgentChangeError("agent-change policy project_scope is invalid")
    required = policy.get("required_references")
    if not isinstance(required, list):
        raise AgentChangeError("agent-change policy required_references is invalid")
    allowed_kinds = {"agent-request", "test-result", "human-approval"}
    for idx, item in enumerate(required):
        if not isinstance(item, dict) or "kind" not in item:
            raise AgentChangeError(f"agent-change policy required_references[{idx}] is invalid")
        kind = item.get("kind")
        if kind not in allowed_kinds:
            raise AgentChangeError(f"agent-change policy required reference kind {kind!r} is invalid")
        if "min_count" in item and not isinstance(item.get("min_count"), int):
            raise AgentChangeError(f"agent-change policy required_references[{idx}].min_count is invalid")
    allowed_profiles = policy.get("allowed_profiles")
    if not isinstance(allowed_profiles, list) or not allowed_profiles:
        raise AgentChangeError("agent-change policy allowed_profiles is invalid")
    for profile in allowed_profiles:
        if not isinstance(profile, str) or not profile:
            raise AgentChangeError("agent-change policy allowed_profiles entry is invalid")
    policy_version = policy.get("policy_version")
    if not isinstance(policy_version, int) or isinstance(policy_version, bool):
        raise AgentChangeError("agent-change policy policy_version is invalid")
    return dict(policy)


def _load_policy(policy_path: Path) -> dict[str, Any]:
    data = _load_json_object(policy_path, "agent-change policy")
    return _validate_policy(data)


def init_policy(target: Path, *, force: bool = False) -> Path:
    """Write a default agent-change policy file with a fresh UUID scope."""
    target = target.expanduser().resolve()
    if not target.is_dir():
        raise AgentChangeError(f"--target is not a directory: {target}")
    policy_path = default_policy_path(target)
    if policy_path.exists() and not force:
        raise FileExistsError(f"agent-change policy already exists: {policy_path}")
    policy = {
        "schema": AGENT_CHANGE_POLICY_SCHEMA,
        "schema_version": 1,
        "project_scope": secrets.token_hex(16),
        "required_references": [
            {"kind": "agent-request"},
            {"kind": "test-result", "min_count": 1},
            {"kind": "human-approval"},
        ],
        "allowed_profiles": [attestation.ATTESTATION_PROFILE],
        "policy_version": 1,
    }
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    attestation.write_attestation_file(policy, policy_path, force=force)
    return policy_path


def _resolve_run_dir(target: Path, run_id: str) -> Path:
    if not _RUN_ID_RE.fullmatch(run_id) or run_id in {".", ".."}:
        raise AgentChangeError("run id must contain only letters, digits, dot, underscore, or hyphen")
    runs_root = (target / ".brigade" / "runs").resolve()
    run_dir = (runs_root / run_id).resolve()
    if run_dir.parent != runs_root or run_dir.name != run_id or not run_dir.is_dir() or run_dir.is_symlink():
        raise AgentChangeError(f"run directory not found: {run_id}")
    return run_dir


def _read_run_json(run_dir: Path) -> dict[str, Any]:
    return _load_json_object(run_dir / "run.json", "run.json")


def _read_roster(run_dir: Path) -> dict[str, Any]:
    roster_path = run_dir / "roster.json"
    if not roster_path.is_file() or roster_path.is_symlink():
        return {}
    try:
        return attestation_input.read_json_object(roster_path)
    except (OSError, attestation_input.AttestationInputError):
        return {}


def _seat_declared_kind(seat: str, roster: Mapping[str, Any] | None) -> str | None:
    if roster is None:
        return None
    agents = roster.get("agents")
    if not isinstance(agents, dict):
        return None
    row = agents.get(seat)
    if not isinstance(row, dict):
        return None
    return row.get("role") if isinstance(row.get("role"), str) else None


def _participants_from_run(run_meta: Mapping[str, Any], roster: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    seat_names: list[str] = []
    orchestrator = run_meta.get("orchestrator")
    if isinstance(orchestrator, str) and orchestrator:
        seat_names.append(orchestrator)
    worker = run_meta.get("worker")
    if isinstance(worker, str) and worker and worker not in seat_names:
        seat_names.append(worker)
    active_seats = run_meta.get("active_seats")
    if isinstance(active_seats, list):
        for seat in active_seats:
            if isinstance(seat, str) and seat and seat not in seat_names:
                seat_names.append(seat)

    participants: list[dict[str, Any]] = []
    for seat in seat_names:
        declared_kind = _seat_declared_kind(seat, roster)
        harness = None
        model_declared = None
        if declared_kind in {"orchestrator", "worker"} or declared_kind is None:
            agents = roster.get("agents") if isinstance(roster, dict) and isinstance(roster.get("agents"), dict) else {}
            row = agents.get(seat) if isinstance(agents, dict) else None
            if isinstance(row, dict):
                harness = row.get("cli") if isinstance(row.get("cli"), str) else None
                model_declared = row.get("model") if isinstance(row.get("model"), str) else None
        participants.append(
            {
                "seat": seat,
                "harness": harness,
                "modelDeclared": model_declared,
                "source": "roster_snapshot",
                "providerObserved": {"status": "unknown"},
            }
        )
    return participants


def _read_lifecycle_report(run_dir: Path) -> run_journal.JournalReport | None:
    path = run_dir / "events" / "lifecycle.jsonl"
    if not path.is_file() or path.is_symlink():
        return None
    try:
        report = run_journal.read_journal_bounded(path)
    except (OSError, run_journal.RunJournalError):
        return None
    if report.partial_tail is not None or report.chain_errors:
        return None
    return report


def _journal_chain_head(events: Sequence[run_journal.RunEvent]) -> dict[str, Any]:
    if not events:
        return {"status": "unavailable"}
    return {"sha256": events[-1].event_digest}


def _latest_approval_event(events: Sequence[run_journal.RunEvent]) -> run_journal.RunEvent | None:
    return next((event for event in reversed(events) if event.event_type == "approval"), None)


def _request_event(run_dir: Path, events: Sequence[run_journal.RunEvent] | None = None) -> run_journal.RunEvent | None:
    if events is None:
        report = _read_lifecycle_report(run_dir)
        if report is None:
            return None
        events = report.events
    request_events = [event for event in events if event.event_type == "request.signed"]
    if len(request_events) != 1:
        return None
    return request_events[0]


def _request_event_for_nonce(run_dir: Path, nonce: str) -> run_journal.RunEvent | None:
    event = _request_event(run_dir)
    if event is None or event.payload.get("nonce") != nonce:
        return None
    return event


def _build_request_reference(
    run_dir: Path,
    target: Path,
    required_kinds: set[str],
    events: Sequence[run_journal.RunEvent],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any]]:
    event = _request_event(run_dir, events)
    if event is None:
        missing = None
        if "agent-request" in required_kinds:
            missing = {"kind": "agent-request", "reason": "required reference absent"}
        return None, missing, {"status": "absent"}
    payload = event.payload
    nonce = payload.get("nonce")
    attestation_path = payload.get("attestation_path")
    task_sha256 = payload.get("task_sha256")
    if (
        not isinstance(nonce, str)
        or not _APPROVAL_NONCE_RE.fullmatch(nonce)
        or not isinstance(attestation_path, str)
        or attestation_path != f"requests/{nonce}.json"
    ):
        missing = None
        if "agent-request" in required_kinds:
            missing = {"kind": "agent-request", "reason": "signed request event is invalid"}
        return None, missing, {"status": "absent"}
    requests_dir = run_dir / "requests"
    if requests_dir.is_symlink() or not requests_dir.is_dir():
        missing = None
        if "agent-request" in required_kinds:
            missing = {"kind": "agent-request", "reason": "signed request envelope is missing"}
        return None, missing, {"status": "absent"}
    request_file = run_dir / attestation_path
    if request_file.is_symlink() or not request_file.is_file():
        missing = None
        if "agent-request" in required_kinds:
            missing = {"kind": "agent-request", "reason": "signed request envelope is missing"}
        return None, missing, {"status": "absent"}
    try:
        envelope = attestation_input.read_json_object(request_file)
    except (OSError, attestation_input.AttestationInputError):
        missing = None
        if "agent-request" in required_kinds:
            missing = {"kind": "agent-request", "reason": "signed request envelope is not readable JSON"}
        return None, missing, {"status": "absent"}

    result, statement, payload_bytes = _verify_reference_envelope(
        envelope,
        target,
        agent_request.AGENT_REQUEST_PREDICATE_TYPE,
        "signed request",
    )
    subject_baseline = _extract_baseline(statement) if statement is not None else None
    envelope_nonce = None
    if statement is not None:
        pred = statement.get("predicate")
        if isinstance(pred, dict):
            envelope_nonce = pred.get("nonce")
    reference_nonce = (
        envelope_nonce if isinstance(envelope_nonce, str) and _APPROVAL_NONCE_RE.fullmatch(envelope_nonce) else nonce
    )
    verified = result.status == attestation.STATUS_SIGNED_OK
    keyids = sorted({result.keyid}) if verified and isinstance(result.keyid, str) else []
    reference: dict[str, Any] = {
        "kind": "agent-request",
        "predicateType": agent_request.AGENT_REQUEST_PREDICATE_TYPE,
        "predicateVersion": _predicate_version(statement, agent_request.AGENT_REQUEST_PREDICATE_TYPE)
        if statement is not None
        else None,
        "profile": attestation.ATTESTATION_PROFILE,
        "subjectBaseline": subject_baseline,
        "payloadSha256": hashlib.sha256(payload_bytes).hexdigest() if payload_bytes is not None else None,
        "envelopeSha256": localio.canonical_json_digest(envelope),
        "signerKeyids": keyids,
        "verified": verified,
        "locator": f".brigade/runs/{run_dir.name}/requests/{nonce}.json",
        "nonce": reference_nonce,
        "taskSha256": task_sha256 if isinstance(task_sha256, str) else None,
    }
    if not verified:
        reference["reason"] = result.status.lower().replace("_", "-")
    if reference["predicateVersion"] is None:
        del reference["predicateVersion"]
    if subject_baseline is None:
        del reference["subjectBaseline"]
    request_field: dict[str, Any] = {"nonce": nonce}
    if isinstance(task_sha256, str):
        request_field["taskSha256"] = task_sha256
    return reference, None, request_field


def _approval_predicate_type_from_statement(statement: Mapping[str, Any]) -> str | None:
    predicate_type = statement.get("predicateType")
    if predicate_type in {approval.HUMAN_APPROVAL_PREDICATE_TYPE, approval_v2.HUMAN_APPROVAL_PREDICATE_TYPE}:
        return predicate_type
    return None


def _build_approval_reference(
    run_dir: Path,
    target: Path,
    approval_path: Path,
    latest_approval_nonce: str | None,
) -> dict[str, Any]:
    label = f"approval {approval_path.name}"
    nonce = approval_path.stem
    try:
        envelope = attestation_input.read_json_object(approval_path)
    except (OSError, attestation_input.AttestationInputError):
        return {
            "kind": "human-approval",
            "predicateType": None,
            "predicateVersion": None,
            "profile": attestation.ATTESTATION_PROFILE,
            "subjectTree": None,
            "payloadSha256": None,
            "envelopeSha256": None,
            "signerKeyids": [],
            "verified": False,
            "locator": f".brigade/runs/{run_dir.name}/approvals/{approval_path.name}",
            "nonce": nonce,
            "reason": "malformed-payload",
        }

    statement, payload_bytes, reason = _decode_envelope_payload(envelope, label)
    if statement is None or payload_bytes is None:
        return {
            "kind": "human-approval",
            "predicateType": None,
            "predicateVersion": None,
            "profile": attestation.ATTESTATION_PROFILE,
            "subjectTree": None,
            "payloadSha256": None,
            "envelopeSha256": None,
            "signerKeyids": [],
            "verified": False,
            "locator": f".brigade/runs/{run_dir.name}/approvals/{approval_path.name}",
            "nonce": nonce,
            "reason": reason,
        }

    predicate_type = _approval_predicate_type_from_statement(statement)
    if predicate_type is None:
        return {
            "kind": "human-approval",
            "predicateType": statement.get("predicateType"),
            "predicateVersion": None,
            "profile": attestation.ATTESTATION_PROFILE,
            "subjectTree": None,
            "payloadSha256": hashlib.sha256(payload_bytes).hexdigest(),
            "envelopeSha256": localio.canonical_json_digest(envelope),
            "signerKeyids": [],
            "verified": False,
            "locator": f".brigade/runs/{run_dir.name}/approvals/{approval_path.name}",
            "nonce": nonce,
            "reason": "unsupported-predicate",
        }

    result, statement2, payload_bytes2 = _verify_reference_envelope(
        envelope,
        target,
        predicate_type,
        label,
    )
    if payload_bytes2 is not None:
        payload_bytes = payload_bytes2
        statement = statement2
    verified = result.status == attestation.STATUS_SIGNED_OK
    subject_tree = _extract_tree(statement) if statement is not None else None
    keyids = sorted({result.keyid}) if verified and isinstance(result.keyid, str) else []

    nonce = approval_path.stem
    journal_bound = latest_approval_nonce is not None and nonce == latest_approval_nonce

    reference: dict[str, Any] = {
        "kind": "human-approval",
        "predicateType": predicate_type,
        "predicateVersion": _predicate_version(statement, predicate_type),
        "profile": attestation.ATTESTATION_PROFILE,
        "subjectTree": subject_tree,
        "payloadSha256": hashlib.sha256(payload_bytes).hexdigest(),
        "envelopeSha256": localio.canonical_json_digest(envelope),
        "signerKeyids": keyids,
        "verified": verified,
        "locator": f".brigade/runs/{run_dir.name}/approvals/{approval_path.name}",
        "journalBound": journal_bound,
    }
    if not verified:
        reference["reason"] = result.status.lower().replace("_", "-")
    if reference["predicateVersion"] is None:
        del reference["predicateVersion"]
    return reference


def _build_approval_references(
    run_dir: Path,
    target: Path,
    events: Sequence[run_journal.RunEvent],
) -> list[dict[str, Any]]:
    approvals_dir = run_dir / "approvals"
    if approvals_dir.is_symlink():
        raise AgentChangeError("approval directory must not contain symlinks")
    if not approvals_dir.is_dir():
        return []
    latest = _latest_approval_event(events)
    latest_nonce = latest.payload.get("nonce") if latest is not None and isinstance(latest.payload, dict) else None
    references: list[dict[str, Any]] = []
    for path in sorted(approvals_dir.iterdir()):
        if path.is_symlink():
            raise AgentChangeError("approval directory must not contain symlinks")
        if not path.is_file() or not _APPROVAL_FILENAME_RE.fullmatch(path.name):
            continue
        references.append(_build_approval_reference(run_dir, target, path, latest_nonce))
    return references


def _deduplicate_references(references: list[dict[str, Any]]) -> list[dict[str, Any]]:
    key_to_refs: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    nondedup: list[dict[str, Any]] = []
    for ref in references:
        payload_sha256 = ref.get("payloadSha256")
        if isinstance(payload_sha256, str) and payload_sha256:
            subject = ref.get("subjectTree") or ref.get("subjectBaseline") or ""
            key = (ref["kind"], payload_sha256, subject)
            key_to_refs.setdefault(key, []).append(ref)
        else:
            nondedup.append(ref)

    deduped: list[dict[str, Any]] = []
    for key in sorted(key_to_refs):
        group = key_to_refs[key]
        group.sort(key=lambda r: r["locator"])
        base = dict(group[0])
        if len(group) > 1:
            base["locators"] = [r["locator"] for r in group]
        deduped.append(base)
    combined = deduped + sorted(nondedup, key=lambda r: (r["kind"], r.get("locator") or ""))
    combined.sort(key=lambda r: (r["kind"], r.get("payloadSha256") or r.get("locator") or ""))
    return combined


def _collect_references(
    target: Path,
    run_id: str,
    run_dir: Path,
    run_meta: Mapping[str, Any],
    final_tree: str,
    policy: Mapping[str, Any],
    events: Sequence[run_journal.RunEvent],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    required_kinds: set[str] = set()
    for item in policy.get("required_references", []):
        if isinstance(item, dict) and isinstance(item.get("kind"), str):
            required_kinds.add(item["kind"])
    request_ref, request_missing, request_field = _build_request_reference(run_dir, target, required_kinds, events)
    test_refs, other_tree, identity = _build_test_result_references(target, run_id, final_tree)
    approval_refs = _build_approval_references(run_dir, target, events)

    references: list[dict[str, Any]] = []
    if request_ref is not None:
        references.append(request_ref)
    references.extend(test_refs)
    references.extend(approval_refs)
    references = _deduplicate_references(references)

    missing: list[dict[str, Any]] = []
    if request_missing is not None:
        missing.append(request_missing)
    present_kinds = {r.get("kind") for r in references if isinstance(r.get("kind"), str)}
    missing_kinds = required_kinds - present_kinds
    for kind in sorted(missing_kinds, key=str):
        if not any(m.get("kind") == kind for m in missing if isinstance(m, dict)):
            missing.append({"kind": kind, "reason": "required reference absent"})
    return references, other_tree, missing, identity, request_field


def _required_set_satisfied(
    policy: Mapping[str, Any],
    references: Sequence[Mapping[str, Any]],
    missing: Sequence[Mapping[str, Any]],
) -> bool:
    required = policy.get("required_references", [])
    if not isinstance(required, list):
        return False
    by_kind: dict[str, int] = {}
    for ref in references:
        if ref.get("verified"):
            kind = ref.get("kind")
            if isinstance(kind, str):
                by_kind[kind] = by_kind.get(kind, 0) + 1
    for item in required:
        if not isinstance(item, dict):
            return False
        kind = item.get("kind")
        if not isinstance(kind, str):
            return False
        min_count = item.get("min_count", 1)
        if not isinstance(min_count, int) or isinstance(min_count, bool) or min_count < 1:
            min_count = 1
        if by_kind.get(kind, 0) < min_count:
            return False
    missing_kinds = {m.get("kind") for m in missing if isinstance(m.get("kind"), str)}
    for item in required:
        kind = item.get("kind")
        if kind in missing_kinds:
            return False
    return True


def build_statement(
    target: Path,
    run_id: str,
    *,
    policy_path: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the canonical agent-change index statement for a run."""
    target = target.expanduser().resolve()
    if not target.is_dir():
        raise AgentChangeError(f"--target is not a directory: {target}")
    policy_path = policy_path.expanduser().resolve() if policy_path is not None else default_policy_path(target)
    if not policy_path.is_file() or policy_path.is_symlink():
        raise AgentChangeError("agent-change policy file is missing; run 'brigade receipts agent-change-policy init'")
    policy = _load_policy(policy_path)
    policy_digest = _policy_digest(policy)

    run_dir = _resolve_run_dir(target, run_id)
    run_meta = _read_run_json(run_dir)
    tree_fingerprint = run_meta.get("tree_fingerprint")
    if not isinstance(tree_fingerprint, str) or not _HEX40_OR_64_RE.fullmatch(tree_fingerprint):
        raise AgentChangeError("run.json has no final tree_fingerprint")

    roster = _read_roster(run_dir)
    participants = _participants_from_run(run_meta, roster)
    report = _read_lifecycle_report(run_dir)
    events = report.events if report is not None else []
    journal_head = _journal_chain_head(events)
    references, other_tree, missing, identity, request_field = _collect_references(
        target, run_id, run_dir, run_meta, tree_fingerprint, policy, events
    )
    complete = _required_set_satisfied(policy, references, missing) and not missing

    baseline = identity.get("baseline", {"status": "unknown"})
    patch = identity.get("patch", {"status": "unknown"})

    instant = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(microsecond=0)
    emitted_at = instant.isoformat().replace("+00:00", "Z")

    return {
        "_type": attestation.IN_TOTO_STATEMENT_TYPE,
        "subject": [
            {"name": "git:tree", "digest": {"gitTree": tree_fingerprint}},
        ],
        "predicateType": AGENT_CHANGE_PREDICATE_TYPE,
        "predicate": {
            "schemaVersion": 1,
            "run": {
                "id": run_id,
                "journalChainHead": journal_head,
                "orchestratorSeat": run_meta.get("orchestrator")
                if isinstance(run_meta.get("orchestrator"), str)
                else None,
                "workerSeats": [
                    p["seat"] for p in participants if p.get("seat") and p["seat"] != run_meta.get("orchestrator")
                ],
            },
            "participants": participants,
            "baseline": baseline,
            "patch": patch,
            "emittedAt": emitted_at,
            "nonce": secrets.token_hex(16),
            "policy": {
                "name": AGENT_CHANGE_POLICY_SCHEMA,
                "digest": {"sha256": policy_digest},
            },
            "project": {"scope": policy["project_scope"]},
            "signerIndependence": "shared-workspace-key",
            "request": request_field,
            "references": references,
            "missing": missing,
            "otherTreeReceipts": other_tree,
            "complete": complete,
        },
    }


def export_agent_change(
    target: Path,
    run_id: str,
    *,
    key: Path | None = None,
    policy: Path | None = None,
    out: str | None = None,
    force: bool = False,
    json_output: bool = False,
) -> int:
    """CLI handler for 'brigade receipts export agent-change'."""
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    if run_id != "latest" and (not _RUN_ID_RE.fullmatch(run_id) or run_id in {".", ".."}):
        print(
            "error: run id must be 'latest' or contain only letters, digits, dot, underscore, or hyphen",
            file=sys.stderr,
        )
        return 2
    if run_id == "latest":
        print("error: 'latest' is not supported for agent-change export", file=sys.stderr)
        return 2

    try:
        run_dir = _resolve_run_dir(target, run_id)
    except AgentChangeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    policy_path = policy.expanduser().resolve() if policy is not None else None
    key_path = attestation.resolve_signing_key_path(target, key_file=key)
    if not key_path.is_file():
        print(f"error: signing key not found: {key_path}", file=sys.stderr)
        return 1

    try:
        statement = build_statement(target, run_id, policy_path=policy_path)
    except AgentChangeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except FileExistsError as exc:
        print(f"error: {exc} (use --force to overwrite)", file=sys.stderr)
        return 1

    try:
        envelope = attestation.create_envelope(statement, key_path)
    except attestation.AttestationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if out == "-":
        print(json.dumps(envelope, indent=2, sort_keys=True))
        return 0 if statement["predicate"]["complete"] else 3

    if out is not None:
        out_path = Path(out).expanduser().resolve()
    else:
        out_path = run_dir / "agent-change.json"

    try:
        attestation.write_attestation_file(envelope, out_path, force=force)
    except FileExistsError as exc:
        print(f"error: {exc} (use --force to overwrite)", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"error: failed to write agent-change index: {exc}", file=sys.stderr)
        return 1

    try:
        rel_path = str(out_path.relative_to(target))
    except ValueError:
        rel_path = str(out_path)
    if json_output:
        print(
            json.dumps(
                {
                    "schema": "brigade.agent_change_export_result.v1",
                    "status": "complete" if statement["predicate"]["complete"] else "incomplete",
                    "path": rel_path,
                    "run_id": run_id,
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print(f"agent-change index: {rel_path}")
        if not statement["predicate"]["complete"]:
            print("warning: index is incomplete; required references are missing")

    return 0 if statement["predicate"]["complete"] else 3
