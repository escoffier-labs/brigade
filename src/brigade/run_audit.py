"""Offline coordinator decision audit from recorded lifecycle evidence (#595).

Read-only consumer of the per-run lifecycle journal and archived run artifacts.
Builds on ``run_journal.read_journal_bounded``, ``run_events.validate_event``,
and ``run_projector.project_run_snapshot``.

Hard safety boundary:
- Never writes to any run directory (journal, run.json, quarantine, or sidecars).
- Never constructs provider adapters or subprocess runners.
- Never appends lifecycle events or invents live fallback evidence.
- Reports contain safe summaries, fingerprints, and classified references only.

Standard library only. Brigade is zero-runtime-dependency.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from brigade import __version__ as BRIGADE_VERSION
from brigade import run_events, run_journal, run_projector
from brigade.run_events import CanonicalizationError, canonical_bytes

AUDIT_SCHEMA = "brigade.run_audit.v1"
AUDIT_SCHEMA_VERSION = 1
AUDIT_NORMALIZATION_VERSION = 1
WORKER_PROMPT_TEMPLATE_REVISION = "brigade.worker_prompt.v1"

JOURNAL_REL = Path("events") / "lifecycle.jsonl"
RUN_JSON_NAME = "run.json"
PLAN_JSON_NAME = "plan.json"
ROSTER_JSON_NAME = "roster.json"

RESULT_MATCH = "match"
RESULT_DIVERGE = "diverge"
RESULT_NOT_AUDITABLE = "not_auditable"
RESULT_ERROR = "error"

# Initial divergence classes from issue #595.
CLASS_LIFECYCLE_PROJECTION_DRIFT = "lifecycle_projection_drift"
CLASS_PROMPT_TEMPLATE_DRIFT = "prompt_template_drift"
CLASS_POLICY_OR_ROUTING_DRIFT = "policy_or_routing_drift"
CLASS_APPROVAL_STATE_DRIFT = "approval_state_drift"
CLASS_ADAPTER_IDENTITY_DRIFT = "adapter_identity_drift"
CLASS_MISSING_OR_CORRUPT_FIXTURE = "missing_or_corrupt_fixture"
CLASS_EXTRA_OR_MISSING_OPERATION = "extra_or_missing_coordinator_operation"
CLASS_ATTEMPTED_LIVE_SIDE_EFFECT = "attempted_live_side_effect"
CLASS_UNSUPPORTED_SCHEMA = "unsupported_schema_or_projector_version"

DIVERGENCE_CLASSES = frozenset(
    {
        CLASS_LIFECYCLE_PROJECTION_DRIFT,
        CLASS_PROMPT_TEMPLATE_DRIFT,
        CLASS_POLICY_OR_ROUTING_DRIFT,
        CLASS_APPROVAL_STATE_DRIFT,
        CLASS_ADAPTER_IDENTITY_DRIFT,
        CLASS_MISSING_OR_CORRUPT_FIXTURE,
        CLASS_EXTRA_OR_MISSING_OPERATION,
        CLASS_ATTEMPTED_LIVE_SIDE_EFFECT,
        CLASS_UNSUPPORTED_SCHEMA,
    }
)

# Byte comparison excludes fields that are expected to vary. Versioned with
# AUDIT_NORMALIZATION_VERSION; covered by golden fixtures.
NORMALIZATION_EXCLUSIONS: frozenset[str] = frozenset(
    {
        "recorded_at",
        "duration_seconds",
        "started_at",
        "status_started_at",
        "finished_at",
        "resumed_at",
        "decided_at",
        "cwd",
        "lock_workspace",
        "artifacts",
        "handoff",
        "control_socket",
        "recovery_preserved_artifact",
        "path",  # checkpoint absolute/relative path noise
    }
)

# First-slice audit coverage by transport. Structured app-server response
# fixture comparison is a follow-up (#592); unsupported transports must state
# coverage instead of claiming full replay.
TRANSPORT_COVERAGE: dict[str, dict[str, Any]] = {
    "app-server": {
        "coordinator_decisions": True,
        "lifecycle_projection": True,
        "composed_prompt_fingerprints": True,
        "provider_response_fixtures": False,
        "worker_event_streams": False,
        "note": "coordinator-only slice; provider response fixtures deferred",
    },
    "exec": {
        "coordinator_decisions": True,
        "lifecycle_projection": True,
        "composed_prompt_fingerprints": True,
        "provider_response_fixtures": False,
        "worker_event_streams": False,
        "note": "exec transport has no recorded worker stream; coordinator-only coverage",
    },
}

_COORDINATOR_EVENT_TYPES = frozenset(
    {
        "run.dispatch.requested",
        "run.dispatch.observed",
        "run.dispatch.completed",
        "run.dispatch.failed",
        "run.paused",
        "run.resumed",
        "approval.requested",
        "approval.granted",
        "approval.rejected",
        "approval.held",
        "approval.consumed",
    }
)

_APPROVAL_EVENT_TYPES = frozenset(
    {
        "approval.requested",
        "approval.granted",
        "approval.rejected",
        "approval.held",
        "approval.consumed",
    }
)

_DISPATCH_EVENT_TYPES = frozenset(
    {
        "run.dispatch.requested",
        "run.dispatch.observed",
        "run.dispatch.completed",
        "run.dispatch.failed",
    }
)


class AuditError(RuntimeError):
    """Bounded audit failure. Carries a ``diagnostic`` safe for reports."""

    def __init__(self, diagnostic: str) -> None:
        super().__init__(diagnostic)
        self.diagnostic = diagnostic


class AttemptedSideEffectError(AuditError):
    """Audit code attempted a forbidden live side effect."""


@dataclass(frozen=True)
class Divergence:
    """First (or only) coordinator decision divergence."""

    divergence_class: str
    sequence: int | None
    event_type: str | None
    expected: Any
    observed: Any
    detail: str


@dataclass
class AuditReport:
    """Bounded offline audit result. Safe for stdout / receipt persistence."""

    result: str
    source_run_id: str
    projector_version: int
    code_revision: str
    normalization_version: int
    audit_schema_version: int
    evidence_digests: dict[str, str] = field(default_factory=dict)
    transport_coverage: dict[str, Any] = field(default_factory=dict)
    normalized_events: list[dict[str, Any]] = field(default_factory=list)
    first_divergence: Divergence | None = None
    not_auditable_reason: str | None = None
    graphtrail_note: str = "GraphTrail deltas are timing-dependent and fail-open; absence is not coordinator divergence"

    def to_receipt(self) -> dict[str, Any]:
        """Receipt fields required by issue #595 acceptance criteria."""
        receipt: dict[str, Any] = {
            "schema": AUDIT_SCHEMA,
            "schema_version": AUDIT_SCHEMA_VERSION,
            "source_run": self.source_run_id,
            "projector_version": self.projector_version,
            "code_revision": self.code_revision,
            "normalization_version": self.normalization_version,
            "result": self.result,
            "first_divergence": None,
            "evidence_digests": dict(self.evidence_digests),
            "transport_coverage": dict(self.transport_coverage),
            "graphtrail_note": self.graphtrail_note,
        }
        if self.first_divergence is not None:
            receipt["first_divergence"] = {
                "class": self.first_divergence.divergence_class,
                "sequence": self.first_divergence.sequence,
                "event_type": self.first_divergence.event_type,
                "expected": self.first_divergence.expected,
                "observed": self.first_divergence.observed,
                "detail": self.first_divergence.detail,
            }
        if self.not_auditable_reason is not None:
            receipt["not_auditable_reason"] = self.not_auditable_reason
        return receipt

    def normalized_events_bytes(self) -> bytes:
        """Canonical bytes of the normalized coordinator event sequence."""
        return canonical_bytes(_canonicalize_for_fingerprint(self.normalized_events)) + b"\n"


def _bound(msg: str) -> str:
    limit = run_events.MAX_DIAGNOSTIC_LEN
    if len(msg) <= limit:
        return msg
    return msg[: limit - 1] + "…"


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _digest_file(path: Path) -> str | None:
    """Return SHA-256 of a regular file, or None when absent. Never follows a symlink final component."""
    if not path.exists():
        return None
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise AuditError(_bound(f"cannot stat evidence: {path.name}")) from exc
    if stat.S_ISLNK(info.st_mode):
        raise AuditError(_bound(f"refusing symlinked evidence: {path.name}"))
    if not stat.S_ISREG(info.st_mode):
        raise AuditError(_bound(f"evidence is not a regular file: {path.name}"))
    return _sha256_hex(path.read_bytes())


def _read_json_object(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise AuditError(_bound(f"cannot stat evidence: {path.name}")) from exc
    if stat.S_ISLNK(info.st_mode):
        raise AuditError(_bound(f"refusing symlinked evidence: {path.name}"))
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AuditError(_bound(f"corrupt evidence: {path.name}")) from exc
    if not isinstance(payload, dict):
        raise AuditError(_bound(f"evidence is not a JSON object: {path.name}"))
    return payload


def _strip_exclusions(value: Any) -> Any:
    """Recursively drop normalization-excluded keys from mappings."""
    if isinstance(value, Mapping):
        return {key: _strip_exclusions(child) for key, child in value.items() if key not in NORMALIZATION_EXCLUSIONS}
    if isinstance(value, list):
        return [_strip_exclusions(item) for item in value]
    return value


def _canonicalize_for_fingerprint(value: Any) -> Any:
    """Convert values into the strict canonical form used by ``canonical_bytes``.

    Lifecycle envelopes forbid booleans; archived run.json / plan / roster
    payloads still carry them. Fingerprints map ``True``/``False`` to ``1``/``0``
    so audit digests stay deterministic without widening the journal contract.
    """
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, Mapping):
        return {str(key): _canonicalize_for_fingerprint(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_canonicalize_for_fingerprint(item) for item in value]
    if isinstance(value, tuple):
        return [_canonicalize_for_fingerprint(item) for item in value]
    return value


def _fingerprint(obj: Any) -> str:
    try:
        return _sha256_hex(canonical_bytes(_canonicalize_for_fingerprint(_strip_exclusions(obj))))
    except CanonicalizationError as exc:
        raise AuditError(_bound(f"value cannot be fingerprinted: {exc}")) from exc


def composed_prompt_fingerprint(
    *,
    worker: str,
    role: str,
    task: str,
    read_only: bool,
    skill_policy: Any,
    template_revision: str = WORKER_PROMPT_TEMPLATE_REVISION,
) -> str:
    """Fingerprint a coordinator-composed worker prompt without retaining prompt text.

    The material is the template revision plus the coordinator-owned inputs that
    enter ``aboyeur._worker_prompt``. Changing ``template_revision`` (or any
    input) changes the digest; the raw prompt never appears in audit output.
    """
    material = {
        "kind": "composed_worker_prompt",
        "template_revision": template_revision,
        "worker": worker,
        "role": role,
        "task": task,
        "read_only": 1 if read_only else 0,
        "skill_policy": _strip_exclusions(skill_policy),
    }
    return _fingerprint(material)


def _transport_coverage(transport: Any) -> dict[str, Any]:
    if not isinstance(transport, str) or not transport:
        return {
            "transport": None,
            "supported": False,
            "coverage": {
                "coordinator_decisions": False,
                "lifecycle_projection": True,
                "composed_prompt_fingerprints": False,
                "provider_response_fixtures": False,
                "worker_event_streams": False,
                "note": "transport identity missing; lifecycle projection only",
            },
        }
    known = TRANSPORT_COVERAGE.get(transport)
    if known is None:
        return {
            "transport": transport,
            "supported": False,
            "coverage": {
                "coordinator_decisions": True,
                "lifecycle_projection": True,
                "composed_prompt_fingerprints": False,
                "provider_response_fixtures": False,
                "worker_event_streams": False,
                "note": f"unsupported transport {transport!r}; coordinator routing/approval only",
            },
        }
    return {"transport": transport, "supported": True, "coverage": dict(known)}


def _base_snapshot_for_projection(run_meta: Mapping[str, Any]) -> dict[str, Any]:
    """Return a projector-safe base snapshot (derived fields omitted)."""
    base: dict[str, Any] = {}
    for key, value in run_meta.items():
        if key in run_projector.DERIVED_FIELDS:
            continue
        if key not in run_projector.OWNED_FIELDS:
            # Unknown keys fail closed at project time; surface as unsupported.
            continue
        base[key] = value
    if "status" not in base:
        base["status"] = "started"
    return base


def _unknown_run_fields(run_meta: Mapping[str, Any]) -> list[str]:
    return sorted(set(run_meta.keys()) - run_projector.OWNED_FIELDS)


def _role_for_worker(roster: Mapping[str, Any] | None, worker: str) -> str | None:
    if roster is None:
        return None
    agents = roster.get("agents")
    if not isinstance(agents, Mapping):
        return None
    agent = agents.get(worker)
    if not isinstance(agent, Mapping):
        return None
    role = agent.get("role")
    return role if isinstance(role, str) and role else None


def _plan_assignments(plan: Mapping[str, Any] | None) -> list[dict[str, str]]:
    if plan is None:
        return []
    raw = plan.get("assignments")
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        worker = item.get("worker")
        task = item.get("task")
        if isinstance(worker, str) and worker and isinstance(task, str):
            out.append({"worker": worker, "task": task})
    return out


def _normalize_lifecycle_event(event: run_journal.RunEvent) -> dict[str, Any] | None:
    """Normalize one coordinator-owned lifecycle event, or None if not coordinator-owned."""
    if event.event_type not in _COORDINATOR_EVENT_TYPES:
        return None
    payload = _strip_exclusions(event.payload)
    kind = "approval" if event.event_type in _APPROVAL_EVENT_TYPES else "dispatch"
    if event.event_type in {"run.paused", "run.resumed"}:
        kind = "approval_lifecycle"
    record = {
        "kind": kind,
        "sequence": event.sequence,
        "event_type": event.event_type,
        "payload": payload,
        "request_digest": event.request_digest,
        "event_digest": event.event_digest,
    }
    return record


def _build_normalized_events(
    *,
    events: Sequence[run_journal.RunEvent],
    run_meta: Mapping[str, Any],
    plan: Mapping[str, Any] | None,
    roster: Mapping[str, Any] | None,
    projection: run_projector.RunProjection,
    template_revision: str,
) -> list[dict[str, Any]]:
    """Build the ordered normalized coordinator decision event sequence."""
    normalized: list[dict[str, Any]] = []

    route_material = {
        "kind": "route_policy",
        "route": _strip_exclusions(run_meta.get("route")),
        "skill_route_policy": _strip_exclusions(run_meta.get("skill_route_policy")),
        "worker": run_meta.get("worker"),
        "orchestrator": run_meta.get("orchestrator"),
        "scheduler": _strip_exclusions(run_meta.get("scheduler")),
    }
    normalized.append(
        {
            "kind": "route_policy",
            "sequence": 0,
            "event_type": None,
            "fingerprint": _fingerprint(route_material),
            "values": route_material,
        }
    )

    adapter_material = {
        "kind": "adapter_identity",
        "codex_transport": run_meta.get("codex_transport"),
        "control_transport": _strip_exclusions(run_meta.get("control_transport")),
    }
    normalized.append(
        {
            "kind": "adapter_identity",
            "sequence": 0,
            "event_type": None,
            "fingerprint": _fingerprint(adapter_material),
            "values": adapter_material,
        }
    )

    read_only = bool(run_meta.get("read_only"))
    skill_policy = run_meta.get("skill_route_policy")
    for index, assignment in enumerate(_plan_assignments(plan), start=1):
        worker = assignment["worker"]
        role = _role_for_worker(roster, worker)
        if role is None:
            # Missing role evidence: still emit a decision marker so absence is visible.
            normalized.append(
                {
                    "kind": "composed_prompt",
                    "sequence": index,
                    "event_type": None,
                    "fingerprint": None,
                    "values": {
                        "worker": worker,
                        "role_present": 0,
                        "template_revision": template_revision,
                    },
                    "evidence": "missing_roster_role",
                }
            )
            continue
        digest = composed_prompt_fingerprint(
            worker=worker,
            role=role,
            task=assignment["task"],
            read_only=read_only,
            skill_policy=skill_policy,
            template_revision=template_revision,
        )
        normalized.append(
            {
                "kind": "composed_prompt",
                "sequence": index,
                "event_type": None,
                "fingerprint": digest,
                "values": {
                    "worker": worker,
                    "role_present": 1,
                    "template_revision": template_revision,
                    "task_fingerprint": _fingerprint(assignment["task"]),
                    "read_only": 1 if read_only else 0,
                    "skill_policy_fingerprint": _fingerprint(skill_policy) if skill_policy is not None else None,
                },
            }
        )

    for event in events:
        record = _normalize_lifecycle_event(event)
        if record is None:
            continue
        record["fingerprint"] = _fingerprint(
            {
                "event_type": record["event_type"],
                "payload": record["payload"],
                "request_digest": record["request_digest"],
            }
        )
        # Drop raw digests of private-adjacent payloads from the public values
        # surface; keep fingerprints + allowlisted payload keys only.
        public = {
            "kind": record["kind"],
            "sequence": record["sequence"],
            "event_type": record["event_type"],
            "fingerprint": record["fingerprint"],
            "payload": record["payload"],
        }
        normalized.append(public)

    projection_material = {
        "kind": "lifecycle_projection",
        "status": projection.status,
        "projector_version": projection.snapshot.get("projector_version"),
        "journal_present": 1 if projection.journal_present else 0,
        "journal_last_sequence": projection.last_sequence,
        "journal_last_event_digest": projection.last_event_digest,
    }
    normalized.append(
        {
            "kind": "lifecycle_projection",
            "sequence": projection.last_sequence,
            "event_type": None,
            "fingerprint": _fingerprint(projection_material),
            "values": projection_material,
        }
    )
    return normalized


def _compare_normalized(
    expected: Sequence[Mapping[str, Any]],
    observed: Sequence[Mapping[str, Any]],
) -> Divergence | None:
    """Return the first divergence between expected and observed normalized events."""
    exp_list = [dict(item) for item in expected]
    obs_list = [dict(item) for item in observed]
    limit = max(len(exp_list), len(obs_list))
    for index in range(limit):
        if index >= len(exp_list):
            item = obs_list[index]
            return Divergence(
                divergence_class=CLASS_EXTRA_OR_MISSING_OPERATION,
                sequence=item.get("sequence") if isinstance(item.get("sequence"), int) else None,
                event_type=item.get("event_type") if isinstance(item.get("event_type"), str) else None,
                expected=None,
                observed={"kind": item.get("kind"), "fingerprint": item.get("fingerprint")},
                detail=_bound(f"extra coordinator operation at normalized index {index}"),
            )
        if index >= len(obs_list):
            item = exp_list[index]
            return Divergence(
                divergence_class=CLASS_EXTRA_OR_MISSING_OPERATION,
                sequence=item.get("sequence") if isinstance(item.get("sequence"), int) else None,
                event_type=item.get("event_type") if isinstance(item.get("event_type"), str) else None,
                expected={"kind": item.get("kind"), "fingerprint": item.get("fingerprint")},
                observed=None,
                detail=_bound(f"missing coordinator operation at normalized index {index}"),
            )
        exp = exp_list[index]
        obs = obs_list[index]
        if exp == obs:
            continue
        divergence_class = _classify_pair_drift(exp, obs)
        return Divergence(
            divergence_class=divergence_class,
            sequence=obs.get("sequence") if isinstance(obs.get("sequence"), int) else None,
            event_type=obs.get("event_type") if isinstance(obs.get("event_type"), str) else None,
            expected={"kind": exp.get("kind"), "fingerprint": exp.get("fingerprint"), "values": exp.get("values")},
            observed={"kind": obs.get("kind"), "fingerprint": obs.get("fingerprint"), "values": obs.get("values")},
            detail=_bound(f"coordinator decision drift at normalized index {index} kind={obs.get('kind')!r}"),
        )
    return None


def _classify_pair_drift(expected: Mapping[str, Any], observed: Mapping[str, Any]) -> str:
    kind = observed.get("kind") or expected.get("kind")
    if kind == "composed_prompt":
        return CLASS_PROMPT_TEMPLATE_DRIFT
    if kind == "route_policy" or kind == "dispatch":
        return CLASS_POLICY_OR_ROUTING_DRIFT
    if kind in {"approval", "approval_lifecycle"}:
        return CLASS_APPROVAL_STATE_DRIFT
    if kind == "adapter_identity":
        return CLASS_ADAPTER_IDENTITY_DRIFT
    if kind == "lifecycle_projection":
        return CLASS_LIFECYCLE_PROJECTION_DRIFT
    return CLASS_POLICY_OR_ROUTING_DRIFT


def _projection_drift(
    run_meta: Mapping[str, Any],
    projection: run_projector.RunProjection,
) -> Divergence | None:
    """Compare projector-derived fields against the archived run.json values."""
    checks = (
        ("status", projection.status, run_meta.get("status")),
        ("projector_version", run_projector.PROJECTOR_VERSION, run_meta.get("projector_version")),
        ("journal_present", True, run_meta.get("journal_present")),
        ("journal_last_sequence", projection.last_sequence, run_meta.get("journal_last_sequence")),
        (
            "journal_last_event_digest",
            projection.last_event_digest,
            run_meta.get("journal_last_event_digest"),
        ),
    )
    # Legacy run.json may omit journal_* fields written only after authority
    # graduation. Absence of those fields is not projection drift when the
    # status itself matches the projected status; presence with a mismatch is.
    for name, expected, observed in checks:
        if name in {"projector_version", "journal_present", "journal_last_sequence", "journal_last_event_digest"}:
            if name not in run_meta:
                continue
        if expected != observed:
            return Divergence(
                divergence_class=CLASS_LIFECYCLE_PROJECTION_DRIFT,
                sequence=projection.last_sequence or None,
                event_type=None,
                expected={name: expected},
                observed={name: observed},
                detail=_bound(f"lifecycle projection drift on field {name}"),
            )
    return None


def _approval_state_drift(
    events: Sequence[run_journal.RunEvent],
    run_meta: Mapping[str, Any],
) -> Divergence | None:
    """Compare the last approval decision in the journal to run.json approval_reference."""
    reference = run_meta.get("approval_reference")
    last_approval: run_journal.RunEvent | None = None
    for event in events:
        if event.event_type in _APPROVAL_EVENT_TYPES:
            last_approval = event
    if last_approval is None:
        return None
    if not isinstance(reference, Mapping):
        return Divergence(
            divergence_class=CLASS_APPROVAL_STATE_DRIFT,
            sequence=last_approval.sequence,
            event_type=last_approval.event_type,
            expected={"approval_reference": "present"},
            observed={"approval_reference": None},
            detail=_bound("approval events present but run.json approval_reference missing"),
        )
    expected_id = last_approval.payload.get("approval_id")
    observed_id = reference.get("approval_id")
    if expected_id != observed_id:
        return Divergence(
            divergence_class=CLASS_APPROVAL_STATE_DRIFT,
            sequence=last_approval.sequence,
            event_type=last_approval.event_type,
            expected={"approval_id": expected_id},
            observed={"approval_id": observed_id},
            detail=_bound("approval_id drift between journal and run.json"),
        )
    # decision_state on granted/rejected/held events is authoritative when present.
    decision = last_approval.payload.get("decision_state")
    observed_decision = reference.get("decision_state")
    if isinstance(decision, str) and observed_decision is not None and decision != observed_decision:
        return Divergence(
            divergence_class=CLASS_APPROVAL_STATE_DRIFT,
            sequence=last_approval.sequence,
            event_type=last_approval.event_type,
            expected={"decision_state": decision},
            observed={"decision_state": observed_decision},
            detail=_bound("approval decision_state drift between journal and run.json"),
        )
    return None


def _routing_drift(
    events: Sequence[run_journal.RunEvent],
    plan: Mapping[str, Any] | None,
) -> Divergence | None:
    """Compare plan assignments to recorded dispatch.requested seats."""
    assignments = _plan_assignments(plan)
    if not assignments:
        return None
    planned_seats = [item["worker"] for item in assignments]
    requested: list[str] = []
    for event in events:
        if event.event_type != "run.dispatch.requested":
            continue
        seat = event.payload.get("seat")
        if isinstance(seat, str) and seat:
            requested.append(seat)
    if not requested:
        return None
    if requested != planned_seats[: len(requested)] and set(requested) != set(planned_seats):
        # Allow subset prefix (partial dispatch) when order matches.
        if requested != planned_seats[: len(requested)]:
            return Divergence(
                divergence_class=CLASS_POLICY_OR_ROUTING_DRIFT,
                sequence=next(
                    (e.sequence for e in events if e.event_type == "run.dispatch.requested"),
                    None,
                ),
                event_type="run.dispatch.requested",
                expected={"seats": planned_seats},
                observed={"seats": requested},
                detail=_bound("dispatch seat routing drift versus plan.json assignments"),
            )
    return None


def audit_run(
    run_dir: Path,
    *,
    expected_normalized_events: Sequence[Mapping[str, Any]] | None = None,
    code_revision: str | None = None,
    template_revision: str = WORKER_PROMPT_TEMPLATE_REVISION,
    supported_projector_version: int | None = None,
) -> AuditReport:
    """Audit coordinator-owned decisions for one archived run.

    Pure with respect to the run directory: reads only. ``expected_normalized_events``
    enables golden / regression comparison; when omitted, the audit checks
    internal consistency (projection, approval, routing) against archived
    artifacts and returns the normalized event sequence for byte-stable replay.
    """
    run_dir = Path(run_dir)
    revision = code_revision if code_revision is not None else BRIGADE_VERSION
    projector_floor = (
        supported_projector_version if supported_projector_version is not None else run_projector.PROJECTOR_VERSION
    )

    if not run_dir.is_dir():
        return AuditReport(
            result=RESULT_NOT_AUDITABLE,
            source_run_id=run_dir.name,
            projector_version=run_projector.PROJECTOR_VERSION,
            code_revision=revision,
            normalization_version=AUDIT_NORMALIZATION_VERSION,
            audit_schema_version=AUDIT_SCHEMA_VERSION,
            not_auditable_reason=_bound(f"run directory not found: {run_dir.name}"),
            first_divergence=Divergence(
                divergence_class=CLASS_MISSING_OR_CORRUPT_FIXTURE,
                sequence=None,
                event_type=None,
                expected={"run_dir": "directory"},
                observed=None,
                detail=_bound(f"run directory not found: {run_dir.name}"),
            ),
        )

    source_run_id = run_dir.name
    journal_path = run_dir / JOURNAL_REL
    run_json_path = run_dir / RUN_JSON_NAME

    try:
        run_meta = _read_json_object(run_json_path)
    except AuditError as exc:
        return _error_report(
            source_run_id,
            revision,
            CLASS_MISSING_OR_CORRUPT_FIXTURE,
            exc.diagnostic,
            artifact=RUN_JSON_NAME,
        )

    if run_meta is None:
        return AuditReport(
            result=RESULT_NOT_AUDITABLE,
            source_run_id=source_run_id,
            projector_version=run_projector.PROJECTOR_VERSION,
            code_revision=revision,
            normalization_version=AUDIT_NORMALIZATION_VERSION,
            audit_schema_version=AUDIT_SCHEMA_VERSION,
            not_auditable_reason=_bound("legacy run missing run.json"),
            first_divergence=Divergence(
                divergence_class=CLASS_MISSING_OR_CORRUPT_FIXTURE,
                sequence=None,
                event_type=None,
                expected={"artifact": RUN_JSON_NAME},
                observed=None,
                detail=_bound(f"missing evidence artifact: {RUN_JSON_NAME}"),
            ),
        )

    schema = run_meta.get("schema")
    schema_version = run_meta.get("schema_version")
    if schema != "brigade.run.v1" or schema_version != 1:
        return AuditReport(
            result=RESULT_NOT_AUDITABLE,
            source_run_id=source_run_id,
            projector_version=run_projector.PROJECTOR_VERSION,
            code_revision=revision,
            normalization_version=AUDIT_NORMALIZATION_VERSION,
            audit_schema_version=AUDIT_SCHEMA_VERSION,
            not_auditable_reason=_bound(f"unsupported run schema {schema!r} version {schema_version!r}"),
            first_divergence=Divergence(
                divergence_class=CLASS_UNSUPPORTED_SCHEMA,
                sequence=None,
                event_type=None,
                expected={"schema": "brigade.run.v1", "schema_version": 1},
                observed={"schema": schema, "schema_version": schema_version},
                detail=_bound("unsupported run.json schema or version"),
            ),
            transport_coverage=_transport_coverage(run_meta.get("codex_transport")),
        )

    if not journal_path.exists():
        return AuditReport(
            result=RESULT_NOT_AUDITABLE,
            source_run_id=source_run_id,
            projector_version=run_projector.PROJECTOR_VERSION,
            code_revision=revision,
            normalization_version=AUDIT_NORMALIZATION_VERSION,
            audit_schema_version=AUDIT_SCHEMA_VERSION,
            not_auditable_reason=_bound("legacy run without lifecycle journal"),
            first_divergence=Divergence(
                divergence_class=CLASS_MISSING_OR_CORRUPT_FIXTURE,
                sequence=None,
                event_type=None,
                expected={"artifact": str(JOURNAL_REL)},
                observed=None,
                detail=_bound(f"missing evidence artifact: {JOURNAL_REL}"),
            ),
            transport_coverage=_transport_coverage(run_meta.get("codex_transport")),
            evidence_digests=_safe_digests(run_dir, journal_present=False),
        )

    try:
        report = run_journal.read_journal_bounded(journal_path)
    except run_journal.RunJournalError as exc:
        return _error_report(
            source_run_id,
            revision,
            CLASS_MISSING_OR_CORRUPT_FIXTURE,
            exc.diagnostic,
            artifact=str(JOURNAL_REL),
            transport=_transport_coverage(run_meta.get("codex_transport")),
        )

    if report.partial_tail is not None:
        return _error_report(
            source_run_id,
            revision,
            CLASS_MISSING_OR_CORRUPT_FIXTURE,
            _bound("lifecycle journal ends in a partial line"),
            artifact=str(JOURNAL_REL),
            transport=_transport_coverage(run_meta.get("codex_transport")),
        )
    if report.chain_errors:
        return _error_report(
            source_run_id,
            revision,
            CLASS_MISSING_OR_CORRUPT_FIXTURE,
            report.chain_errors[0],
            artifact=str(JOURNAL_REL),
            sequence=1,
            transport=_transport_coverage(run_meta.get("codex_transport")),
        )
    if not report.events:
        return AuditReport(
            result=RESULT_NOT_AUDITABLE,
            source_run_id=source_run_id,
            projector_version=run_projector.PROJECTOR_VERSION,
            code_revision=revision,
            normalization_version=AUDIT_NORMALIZATION_VERSION,
            audit_schema_version=AUDIT_SCHEMA_VERSION,
            not_auditable_reason=_bound("lifecycle journal is empty"),
            first_divergence=Divergence(
                divergence_class=CLASS_MISSING_OR_CORRUPT_FIXTURE,
                sequence=None,
                event_type=None,
                expected={"events": "non-empty"},
                observed={"events": 0},
                detail=_bound(f"empty evidence artifact: {JOURNAL_REL}"),
            ),
            transport_coverage=_transport_coverage(run_meta.get("codex_transport")),
            evidence_digests=_safe_digests(run_dir, journal_present=True),
        )

    # Fail closed on any envelope the journal reader accepted but validate_event rejects.
    for event in report.events:
        errors = run_events.validate_event(event.to_dict())
        if errors:
            return _error_report(
                source_run_id,
                revision,
                CLASS_MISSING_OR_CORRUPT_FIXTURE,
                _bound(f"event sequence {event.sequence} failed validation: {errors[0]}"),
                artifact=str(JOURNAL_REL),
                sequence=event.sequence,
                event_type=event.event_type,
                transport=_transport_coverage(run_meta.get("codex_transport")),
            )

    source_run_id = report.events[0].run_id
    unknown = _unknown_run_fields(run_meta)
    if unknown:
        return AuditReport(
            result=RESULT_NOT_AUDITABLE,
            source_run_id=source_run_id,
            projector_version=run_projector.PROJECTOR_VERSION,
            code_revision=revision,
            normalization_version=AUDIT_NORMALIZATION_VERSION,
            audit_schema_version=AUDIT_SCHEMA_VERSION,
            not_auditable_reason=_bound(f"unsupported run.json fields for projector: {', '.join(unknown[:8])}"),
            first_divergence=Divergence(
                divergence_class=CLASS_UNSUPPORTED_SCHEMA,
                sequence=None,
                event_type=None,
                expected={"owned_fields": "subset"},
                observed={"unknown_fields": unknown[:8]},
                detail=_bound("run.json carries fields outside projector ownership map"),
            ),
            transport_coverage=_transport_coverage(run_meta.get("codex_transport")),
            evidence_digests=_safe_digests(run_dir, journal_present=True),
        )

    recorded_projector = run_meta.get("projector_version")
    if isinstance(recorded_projector, int) and not isinstance(recorded_projector, bool):
        if recorded_projector != projector_floor:
            return AuditReport(
                result=RESULT_NOT_AUDITABLE,
                source_run_id=source_run_id,
                projector_version=run_projector.PROJECTOR_VERSION,
                code_revision=revision,
                normalization_version=AUDIT_NORMALIZATION_VERSION,
                audit_schema_version=AUDIT_SCHEMA_VERSION,
                not_auditable_reason=_bound(
                    f"unsupported projector version {recorded_projector}; audit supports {projector_floor}"
                ),
                first_divergence=Divergence(
                    divergence_class=CLASS_UNSUPPORTED_SCHEMA,
                    sequence=None,
                    event_type=None,
                    expected={"projector_version": projector_floor},
                    observed={"projector_version": recorded_projector},
                    detail=_bound("unsupported projector version for audit"),
                ),
                transport_coverage=_transport_coverage(run_meta.get("codex_transport")),
                evidence_digests=_safe_digests(run_dir, journal_present=True),
            )

    try:
        plan = _read_json_object(run_dir / PLAN_JSON_NAME)
        roster = _read_json_object(run_dir / ROSTER_JSON_NAME)
    except AuditError as exc:
        return _error_report(
            source_run_id,
            revision,
            CLASS_MISSING_OR_CORRUPT_FIXTURE,
            exc.diagnostic,
            artifact=exc.diagnostic,
            transport=_transport_coverage(run_meta.get("codex_transport")),
        )

    try:
        projection = run_projector.project_run_snapshot(
            _base_snapshot_for_projection(run_meta),
            report.events,
            journal_present=True,
        )
    except run_projector.ProjectionError as exc:
        return _error_report(
            source_run_id,
            revision,
            CLASS_LIFECYCLE_PROJECTION_DRIFT,
            exc.diagnostic,
            artifact=str(JOURNAL_REL),
            transport=_transport_coverage(run_meta.get("codex_transport")),
        )

    try:
        normalized = _build_normalized_events(
            events=report.events,
            run_meta=run_meta,
            plan=plan,
            roster=roster,
            projection=projection,
            template_revision=template_revision,
        )
    except AuditError as exc:
        return _error_report(
            source_run_id,
            revision,
            CLASS_MISSING_OR_CORRUPT_FIXTURE,
            exc.diagnostic,
            transport=_transport_coverage(run_meta.get("codex_transport")),
        )

    evidence_digests = _safe_digests(run_dir, journal_present=True)
    evidence_digests["normalized_events"] = _sha256_hex(canonical_bytes(_canonicalize_for_fingerprint(normalized)))
    coverage = _transport_coverage(run_meta.get("codex_transport"))

    divergence: Divergence | None = None
    if expected_normalized_events is not None:
        divergence = _compare_normalized(expected_normalized_events, normalized)
    else:
        divergence = _projection_drift(run_meta, projection)
        if divergence is None:
            divergence = _approval_state_drift(report.events, run_meta)
        if divergence is None:
            divergence = _routing_drift(report.events, plan)

    result = RESULT_MATCH if divergence is None else RESULT_DIVERGE
    return AuditReport(
        result=result,
        source_run_id=source_run_id,
        projector_version=run_projector.PROJECTOR_VERSION,
        code_revision=revision,
        normalization_version=AUDIT_NORMALIZATION_VERSION,
        audit_schema_version=AUDIT_SCHEMA_VERSION,
        evidence_digests=evidence_digests,
        transport_coverage=coverage,
        normalized_events=normalized,
        first_divergence=divergence,
    )


def _safe_digests(run_dir: Path, *, journal_present: bool) -> dict[str, str]:
    digests: dict[str, str] = {}
    for name in (RUN_JSON_NAME, PLAN_JSON_NAME, ROSTER_JSON_NAME):
        digest = _digest_file(run_dir / name)
        if digest is not None:
            digests[name] = digest
    if journal_present:
        digest = _digest_file(run_dir / JOURNAL_REL)
        if digest is not None:
            digests[str(JOURNAL_REL)] = digest
    return digests


def _error_report(
    source_run_id: str,
    revision: str,
    divergence_class: str,
    detail: str,
    *,
    artifact: str | None = None,
    sequence: int | None = None,
    event_type: str | None = None,
    transport: dict[str, Any] | None = None,
) -> AuditReport:
    expected: Any = {"artifact": artifact} if artifact is not None else None
    return AuditReport(
        result=RESULT_ERROR if divergence_class == CLASS_MISSING_OR_CORRUPT_FIXTURE else RESULT_DIVERGE,
        source_run_id=source_run_id,
        projector_version=run_projector.PROJECTOR_VERSION,
        code_revision=revision,
        normalization_version=AUDIT_NORMALIZATION_VERSION,
        audit_schema_version=AUDIT_SCHEMA_VERSION,
        transport_coverage=transport or {},
        first_divergence=Divergence(
            divergence_class=divergence_class,
            sequence=sequence,
            event_type=event_type,
            expected=expected,
            observed=None,
            detail=_bound(detail),
        ),
        not_auditable_reason=_bound(detail)
        if divergence_class in {CLASS_MISSING_OR_CORRUPT_FIXTURE, CLASS_UNSUPPORTED_SCHEMA}
        else None,
    )


def forbid_live_side_effects() -> None:
    """Raise if this module ever grows live side-effect imports.

    Called by the sentinel test and available for defensive callers. The audit
    path must remain offline: no subprocess, socket, urllib, or AppServer.
    """
    banned = ("subprocess", "socket", "urllib", "http.client", "codex_appserver")
    source = Path(__file__).read_text(encoding="utf-8")
    for name in banned:
        # Match import forms only; comments mentioning the ban are allowed.
        if f"import {name}" in source or f"from {name}" in source:
            raise AttemptedSideEffectError(_bound(f"audit module imports forbidden module {name}"))
