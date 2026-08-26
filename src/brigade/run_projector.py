"""Pure run.json snapshot projector (issue #568, slice 3).

Derives a deterministic ``run.json`` snapshot from a base snapshot plus a
verified lifecycle event sequence. The projector owns exactly five derived
fields (``status``, ``projector_version``, ``journal_present``,
``journal_last_sequence``, ``journal_last_event_digest``) and preserves every
other current ``run.json`` field verbatim from the base snapshot through an
explicit ownership map. Unknown base fields and registered event types with
no status mapping fail closed with bounded typed errors.

Purity: no I/O, no clock, no environment, no mutable module state. Identical
inputs produce byte-identical output on every call and every host. Encoding
matches ``aboyeur._write_json`` byte for byte: sorted keys, two-space indent,
one trailing newline, UTF-8.

This slice does not write ``run.json`` and is not wired into any runtime
path. Standard library only. Brigade is zero-runtime-dependency.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from brigade import run_checkpoint, run_events, run_journal

PROJECTOR_VERSION: int = 6

# Field ownership over the run.json contract. Every current run.json key is
# in exactly one of these two sets; see the ownership inventory in
# docs/phase-568-slice-3-run-projector.md.
DERIVED_FIELDS: frozenset[str] = frozenset(
    {
        "status",
        "projector_version",
        "journal_present",
        "journal_last_sequence",
        "journal_last_event_digest",
    }
)

PRESERVED_FIELDS: frozenset[str] = frozenset(
    {
        # Identity and schema
        "schema",
        "schema_version",
        "kind",
        "task",
        "orchestrator",
        "roster",
        "worker",
        "scheduler",
        # Run configuration
        "dry_run",
        "read_only",
        "cwd",
        "lock_workspace",
        "codex_transport",
        "lifecycle_journal_requested",
        "run_journal_authority_requested",
        "approval_reference",
        "verification_contract",
        "run_budget",
        "run_budget_projection",
        # Research receipt fields mirrored onto run.json
        "category",
        "provenance",
        "lineage",
        "causal_receipt",
        # Timing
        "started_at",
        "status_started_at",
        "finished_at",
        "duration_seconds",
        "resumed_at",
        # Recovery (written by run_resume.py and runguard.py)
        "recovery_history",
        "recovery_preserved_artifact",
        # Briefs and routing telemetry
        "code_graph_brief",
        "drift_impact_brief",
        "evidence_brief",
        "brief_budget",
        "route",
        "skill_route_policy",
        "seat_routing",
        "retry_decisions",
        "health",
        "worker_failure_summary",
        "transport_routing",
        "pre_run_snapshot",
        "git",
        "code_graph_delta",
        "context_eval",
        "suspected_noop",
        # Control transport
        "control_transport",
        "control_socket",
        # Live progress
        "active_stage",
        "active_seats",
        "phase_owner",
        # Outcome and failure
        "error",
        "failure_phase",
        "failure_kind",
        "failure",
        "transport_warning",
        "artifact_collection",
        # Artifact references
        "artifacts",
        "handoff",
    }
)

OWNED_FIELDS: frozenset[str] = DERIVED_FIELDS | PRESERVED_FIELDS

# Static event_type -> run.json status mappings (payload-independent rows of
# the event-to-status table).
EVENT_STATUS: dict[str, str] = {
    "run.planning.started": "planning",
    "run.dispatching.started": "dispatching",
    "run.dispatch.requested": "dispatching",
    "run.dispatch.observed": "dispatching",
    "run.dispatch.completed": "dispatching",
    "run.dispatch.failed": "dispatching",
    "run.result-processing.started": "result-processing",
    "run.synthesis.started": "synthesizing",
    "run.synthesis.completed": "handoff",
    "run.artifact_collection.started": "artifact-collection",
    # Approval waits retain the documented previous-reader nonterminal status.
    # Current readers distinguish the wait through the approval reference.
    "run.paused": "running",
    "run.resumed": "running",
    # Research reopen recovery (no approval reference) restores running.
    "run.recovery.started": "running",
}


def _has_dispatch_identity(payload: Any) -> bool:
    return (
        isinstance(payload, Mapping)
        and isinstance(payload.get("seat"), str)
        and bool(payload["seat"])
        and isinstance(payload.get("attempt"), int)
        and not isinstance(payload["attempt"], bool)
        and payload["attempt"] > 0
    )


# Approval observations do not themselves change the compatibility status.
# ``run.paused`` and ``run.resumed`` own the status transition; the approval
# facts between them only advance the verified journal cursor.
_STATUS_NEUTRAL_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "approval.requested",
        "approval.granted",
        "approval.rejected",
        "approval.held",
        "approval.consumed",
        "run.redaction.recorded",
        # Live-control pairs advance the journal cursor only (issue #604).
        "control.requested",
        "control.observed",
        "control.failed",
        # Run-budget lifecycle facts are status-neutral; terminal policy is
        # applied by the coordinator via run.failed / run.interrupted (issue #593).
        "run_budget.threshold_reached",
        "run_budget.reservation_denied",
        "run_budget.exhausted",
        "run_budget.cancel_requested",
        "run_budget.cancelled",
        "run_budget.usage_reconciled",
    }
)

# Payload-driven rows: event_type -> (allowed payload status values, derived
# status). A None derived status means the payload status value is used
# directly as the derived status (run.failed: "failed" vs "timeout").
_PAYLOAD_STATUS_RULES: dict[str, tuple[frozenset[str], str | None]] = {
    "run.created": (frozenset({"started"}), "started"),
    "run.completed": (frozenset({"ok", "dry-run", "completed"}), None),
    "run.failed": (frozenset({"failed", "timeout", "incomplete"}), None),
    # Payload status is preserved so canceled (brigade run) and cancelled
    # (research) both project correctly without rewriting each other.
    "run.interrupted": (frozenset({"canceled", "cancelled"}), None),
}


class ProjectionError(RuntimeError):
    """Base class for projection failures. Carries a bounded ``diagnostic``."""

    def __init__(self, diagnostic: str) -> None:
        super().__init__(diagnostic)
        self.diagnostic = diagnostic


class UnknownSnapshotFieldError(ProjectionError):
    """Base snapshot carries a key outside ``OWNED_FIELDS``."""


class UnmappedEventTypeError(ProjectionError):
    """A validated event's type has no status mapping."""


class EventChainError(ProjectionError):
    """Envelope invalid, sequence gap/duplicate, digest-link break, mixed run_id."""


class EventPayloadError(ProjectionError):
    """A payload status rule failed for a payload-driven event type."""


class ProjectionInputError(ProjectionError):
    """An input argument violates the projector contract."""


class SnapshotEncodingError(ProjectionError):
    """Projected values cannot be encoded by the run.json JSON contract."""


def _bound(msg: str) -> str:
    limit = run_events.MAX_DIAGNOSTIC_LEN
    if len(msg) <= limit:
        return msg
    return msg[: limit - 1] + "…"


@dataclass(frozen=True)
class RunProjection:
    """Result of a successful projection."""

    snapshot: dict[str, Any]  # complete projected run.json object
    status: str  # derived status (equals snapshot["status"])
    journal_present: bool
    last_sequence: int  # 0 when events is empty
    last_event_digest: str | None  # None when events is empty

    def to_bytes(self) -> bytes:
        """Encode the snapshot with the existing run.json byte contract."""
        return encode_snapshot_bytes(self.snapshot)


def encode_snapshot_bytes(snapshot: Mapping[str, Any]) -> bytes:
    """Encode a snapshot as ``json.dumps(indent=2, sort_keys=True) + newline``, UTF-8.

    This matches ``aboyeur._write_json`` byte for byte. Encoding failures are
    wrapped as a bounded ``SnapshotEncodingError`` whose diagnostic carries a
    category only -- never the raw value or the serializer message.
    """
    try:
        return (json.dumps(snapshot, indent=2, sort_keys=True) + "\n").encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise SnapshotEncodingError(_bound("snapshot cannot be encoded as run.json JSON bytes")) from exc


def _normalize_events(events: Sequence[run_journal.RunEvent | Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Normalize typed RunEvent instances and raw mappings to envelope mappings."""
    normalized: list[Mapping[str, Any]] = []
    for item in events:
        if isinstance(item, run_journal.RunEvent):
            normalized.append(item.to_dict())
        elif isinstance(item, Mapping):
            normalized.append(item)
        else:
            raise EventChainError(_bound("event chain item is neither a RunEvent nor a mapping"))
    return normalized


def _verify_chain(envelopes: list[Mapping[str, Any]]) -> None:
    """Fail closed on any invalid envelope, sequence break, or run_id mix.

    Every envelope must pass ``run_events.validate_event``; the first event
    must have ``sequence == 1`` and a null ``previous_digest``; each later
    event must continue the sequence and link its ``previous_digest`` to the
    prior ``event_digest``; all events share one ``run_id``.
    """
    run_id: Any = None
    expected_sequence = 1
    previous_digest: Any = None
    for position, env in enumerate(envelopes, start=1):
        errors = run_events.validate_event(env)
        if errors:
            raise EventChainError(_bound(f"event chain envelope invalid at input position {position}"))
        sequence = env["sequence"]
        if run_id is None:
            run_id = env["run_id"]
        elif env["run_id"] != run_id:
            raise EventChainError(_bound(f"event chain mixes run_id values at sequence {sequence}"))
        if sequence != expected_sequence:
            raise EventChainError(_bound(f"event chain sequence break: expected {expected_sequence}, got {sequence}"))
        if sequence == 1:
            if env["previous_digest"] is not None:
                raise EventChainError(_bound("event chain sequence 1 previous_digest must be null"))
        elif env["previous_digest"] != previous_digest:
            raise EventChainError(
                _bound(f"event chain previous_digest does not link to prior event_digest at sequence {sequence}")
            )
        previous_digest = env["event_digest"]
        expected_sequence = sequence + 1


def _derive_status(envelopes: list[Mapping[str, Any]], base_status: Any) -> str:
    """Derive the run.json status from the event sequence.

    Every event must have a status mapping: static rows come from
    ``EVENT_STATUS``; payload-driven rows validate the payload ``status``
    against the allowed set. Registered types with no mapping raise
    ``UnmappedEventTypeError``; a missing or disallowed payload status raises
    ``EventPayloadError``. An empty sequence preserves the base status.
    """
    if not envelopes:
        if not isinstance(base_status, str):
            raise ProjectionInputError(_bound("base snapshot status must be a string when the event sequence is empty"))
        return base_status
    if not isinstance(base_status, str):
        raise ProjectionInputError(_bound("base snapshot status must be a string when projecting events"))
    status = base_status
    for env in envelopes:
        event_type = env["event_type"]
        sequence = env["sequence"]
        if event_type == run_checkpoint.CHECKPOINT_EVENT_TYPE:
            continue
        if event_type in _STATUS_NEUTRAL_EVENT_TYPES:
            continue
        # Slice-9 per-worker facts are status-neutral: a worker terminal
        # result must not advance the aggregate run while other seats remain
        # active. Older aggregate envelopes lacked an identity and retain
        # their original status semantics below.
        if event_type.startswith("run.dispatch.") and _has_dispatch_identity(env["payload"]):
            continue
        if event_type == "run.dispatch.completed" and not _has_dispatch_identity(env["payload"]):
            status = "result-processing"
            continue
        if event_type in EVENT_STATUS:
            status = EVENT_STATUS[event_type]
            continue
        rule = _PAYLOAD_STATUS_RULES.get(event_type)
        if rule is None:
            raise UnmappedEventTypeError(
                _bound(f"event type {event_type!r} at sequence {sequence} has no status mapping")
            )
        allowed, derived = rule
        payload = env["payload"]
        payload_status = payload.get("status")
        if payload_status not in allowed:
            raise EventPayloadError(
                _bound(f"event type {event_type!r} at sequence {sequence} requires payload status in {sorted(allowed)}")
            )
        status = derived if derived is not None else payload_status
    return status


def project_run_snapshot(
    base_snapshot: Mapping[str, Any],
    events: Sequence[run_journal.RunEvent | Mapping[str, Any]],
    *,
    journal_present: bool,
) -> RunProjection:
    """Project a run.json snapshot from a base snapshot plus a verified event sequence.

    Pure: no I/O, no clock, no environment. ``journal_present`` must be a real
    boolean supplied by the caller from the journal path check; it is never
    inferred from ``events``. Base snapshot keys must be a subset of
    ``OWNED_FIELDS``; derived-field keys present in the base are ignored and
    recomputed, so re-projecting a projected snapshot is idempotent. Every
    preserved field present in the base is deep-copied into the result. The
    result is verified to encode under the run.json byte contract before it
    is returned.
    """
    if not isinstance(journal_present, bool):
        raise ProjectionInputError(_bound("journal_present must be a boolean"))
    if not isinstance(base_snapshot, Mapping):
        raise ProjectionInputError(_bound("base_snapshot must be a mapping"))
    approval_reference = base_snapshot.get("approval_reference")
    if approval_reference is not None:
        if not isinstance(approval_reference, Mapping):
            raise ProjectionInputError(_bound("base snapshot approval_reference must be a mapping"))
        decision_state = approval_reference.get("decision_state")
        if decision_state is not None and decision_state not in run_events.APPROVAL_DECISION_STATES:
            raise ProjectionInputError(_bound("base snapshot approval_reference has invalid decision_state"))

    unknown = sorted(set(base_snapshot.keys()) - OWNED_FIELDS)
    if unknown:
        shown = ", ".join(unknown[:8])
        raise UnknownSnapshotFieldError(_bound(f"base snapshot carries unknown fields: {shown}"))

    envelopes = _normalize_events(events)
    _verify_chain(envelopes)

    status = _derive_status(envelopes, base_snapshot.get("status"))

    snapshot: dict[str, Any] = {}
    for field_name in PRESERVED_FIELDS:
        if field_name in base_snapshot:
            snapshot[field_name] = copy.deepcopy(base_snapshot[field_name])
    if "kind" not in base_snapshot:
        snapshot["kind"] = "work"

    last_sequence = 0
    last_event_digest: str | None = None
    if envelopes:
        final = envelopes[-1]
        last_sequence = final["sequence"]
        last_event_digest = final["event_digest"]

    snapshot["status"] = status
    snapshot["projector_version"] = PROJECTOR_VERSION
    snapshot["journal_present"] = journal_present
    snapshot["journal_last_sequence"] = last_sequence
    snapshot["journal_last_event_digest"] = last_event_digest

    # Verify the result encodes under the run.json byte contract before
    # returning; failures surface as a bounded SnapshotEncodingError.
    encode_snapshot_bytes(snapshot)

    return RunProjection(
        snapshot=snapshot,
        status=status,
        journal_present=journal_present,
        last_sequence=last_sequence,
        last_event_digest=last_event_digest,
    )
