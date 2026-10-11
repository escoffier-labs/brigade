"""Pure retention evaluation over caller-admitted captured observations.

Admission and source provenance belong to the caller. Digests identify supplied
evidence, not its authenticity. No storage, clock, restore or delete operation
occurs here. A supported prune prerequisite never authorizes deletion.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from . import evidence_ack_acceptance as ack, evidence_ack_protocol as wire


@dataclass(frozen=True, slots=True)
class RetentionSubject:
    association: ack.AckAssociation
    destination_reference: str
    object_version: str


@dataclass(frozen=True, slots=True)
class ObservationBinding:
    subject: RetentionSubject
    source_id: str
    source_class: str
    observed_at: str
    valid_from: str
    valid_until: str
    policy_revision: str
    hold_revision: str
    evidence_sha256: str


@dataclass(frozen=True, slots=True)
class RetentionDeclaration:
    binding: ObservationBinding
    starts_at: str
    period: str


@dataclass(frozen=True, slots=True)
class EnforcementObservation:
    binding: ObservationBinding
    state: str
    retain_until: str | None


@dataclass(frozen=True, slots=True)
class HoldObservation:
    binding: ObservationBinding
    state: str


@dataclass(frozen=True, slots=True)
class RecoveryObservation:
    """Captured archive or restore result, admitted from the existing adapters.

    'verified' requires adopter evidence of the actual operation and integrity
    reconciliation with this subject. A package digest alone is insufficient.
    A verified restore must identify the archive evidence it proves.
    """

    binding: ObservationBinding
    state: str
    archive_evidence_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class OperationAuthority:
    binding: ObservationBinding
    principal: str
    operation: str
    state: str


@dataclass(frozen=True, slots=True)
class ControlAssessment:
    kind: str
    state: str
    binding: ObservationBinding | None
    observed_state: str | None


@dataclass(frozen=True, slots=True)
class AuthorityAssessment:
    principal: str | None
    operation: str | None
    state: str
    binding: ObservationBinding | None


@dataclass(frozen=True, slots=True)
class RetentionAssessment:
    subject: RetentionSubject | None
    evaluated_at: str | None
    policy_revision: str | None
    hold_revision: str | None
    declaration: ControlAssessment
    enforcement: ControlAssessment
    hold: ControlAssessment
    archive: ControlAssessment
    restore: ControlAssessment
    authorities: tuple[AuthorityAssessment, ...]
    starts_at: str | None
    period: str | None
    declared_expires_at: str | None
    observed_retain_until: str | None
    expires_at: str | None
    retention_state: str
    prune_prerequisite: str
    reasons: tuple[str, ...]
    deletion_authorized: Literal[False] = False


def _stamp(value: datetime) -> str:
    # strftime's year width differs by platform for years below 1000.
    return value.isoformat(timespec="seconds") + "Z"


def calendar_expiry(starts_at: str, period: str) -> str:
    """Add positive integer ISO calendar years/months/days, then clamp the day.

    Years and months are combined before a single month-end clamp. Days are
    added afterwards. No universal period or minimum is imposed.
    """
    start = ack._time(starts_at)
    match = (
        re.fullmatch(r"P(?:(\d{1,9})Y)?(?:(\d{1,9})M)?(?:(\d{1,9})D)?", period, flags=re.ASCII)
        if type(period) is str and len(period) <= 32
        else None
    )
    if start is None or match is None:
        raise ValueError("invalid retention calendar")
    years, months, days = (int(value or 0) for value in match.groups())
    if not years + months + days:
        raise ValueError("invalid retention calendar")
    try:
        year, month = divmod(start.year * 12 + start.month - 1 + years * 12 + months, 12)
        if not 1 <= year <= 9999:
            raise ValueError
        expiry = start.replace(year=year, month=month + 1, day=min(start.day, calendar.monthrange(year, month + 1)[1]))
        return _stamp(expiry + timedelta(days=days))
    except (ValueError, OverflowError):
        raise ValueError("invalid retention calendar") from None


def _subject_valid(subject: object) -> bool:
    if type(subject) is not RetentionSubject or type(subject.association) is not ack.AckAssociation:
        return False
    association = subject.association
    try:
        wire._destination_reference(subject.destination_reference)
        wire._destination_id(association.destination_id)
        wire._nonce(association.nonce)
        wire._run_id(association.verify_run_id)
    except wire.AckWireError:
        return False
    return (
        ack._text(subject.object_version, 2048)
        and ack._text(association.audience)
        and all(
            ack._digest(value)
            for value in (
                association.request_id,
                association.envelope_id,
                association.manifest_sha256,
                association.receipt_sha256,
            )
        )
    )


def _binding_valid(binding: object) -> bool:
    return (
        type(binding) is ObservationBinding
        and _subject_valid(binding.subject)
        and all(
            ack._text(value)
            for value in (binding.source_id, binding.source_class, binding.policy_revision, binding.hold_revision)
        )
        and ack._digest(binding.evidence_sha256)
        and all(
            ack._time(value) is not None for value in (binding.observed_at, binding.valid_from, binding.valid_until)
        )
    )


def _ack_bound(accepted: ack.AckAcceptance, subject: RetentionSubject, now: datetime | None) -> bool:
    if (
        not _subject_valid(subject)
        or any(
            type(value) is not str
            for value in (
                accepted.state,
                accepted.signature,
                accepted.authorization,
                accepted.asserted_result,
                accepted.custody_authority,
                accepted.destination_reference,
                accepted.object_version,
            )
        )
        or accepted.state != "accepted"
        or type(accepted.reasons) is not tuple
        or accepted.reasons != ()
        or accepted.signature != "VALID"
        or accepted.authorization != "authorized"
        or accepted.asserted_result != "received"
        or type(accepted.association) is not ack.AckAssociation
        or accepted.association != subject.association
        or accepted.destination_reference != subject.destination_reference
        or accepted.object_version != subject.object_version
        or accepted.custody_authority != "independent-operating-evidence"
        or not ack._digest(accepted.policy_sha256)
        or not ack._text(accepted.policy_revision)
        or type(accepted.authority) is not tuple
        or not 1 <= len(accepted.authority) <= 2
        or any(type(item) is not ack.AuthorityObservation for item in accepted.authority)
    ):
        return False
    acknowledged, evaluated = ack._time(accepted.acknowledged_at), ack._time(accepted.evaluated_at)
    if acknowledged is None or evaluated is None or now is None or not acknowledged <= evaluated <= now:
        return False
    # Reconcile the exact custody evidence rather than trusting its summary label.
    kinds = tuple(item.kind for item in accepted.authority)
    if any(type(kind) is not str or kind not in {"custody", "time"} for kind in kinds) or len(set(kinds)) != len(kinds):
        return False
    custody = tuple(item for item in accepted.authority if item.kind == "custody")
    return len(custody) == 1 and all(
        type(item.association) is ack.AckAssociation
        and type(item.state) is str
        and type(item.source_class) is str
        and type(item.destination_reference) is str
        and type(item.acknowledged_at) is str
        and (item.kind != "custody" or type(item.object_version) is str)
        and item.association == subject.association
        and item.state == "supported"
        and ack._digest(item.evidence_sha256)
        and item.source_class
        == ("independent-operating-evidence" if item.kind == "custody" else "independent-time-evidence")
        and item.destination_reference == subject.destination_reference
        and item.acknowledged_at == accepted.acknowledged_at
        and (item.kind != "custody" or item.object_version == subject.object_version)
        for item in accepted.authority
    )


def _binding_state(
    binding: ObservationBinding,
    *,
    subject: RetentionSubject,
    policy_revision: str,
    hold_revision: str,
    now: datetime | None,
    acknowledged: datetime | None,
    source_class: str,
) -> str:
    if not _binding_valid(binding) or not _subject_valid(subject):
        return "indeterminate"
    if (
        binding.subject != subject
        or binding.policy_revision != policy_revision
        or binding.hold_revision != hold_revision
    ):
        return "mismatch"
    if binding.source_class != source_class or not ack._text(policy_revision) or not ack._text(hold_revision):
        return "indeterminate"
    observed, lower, upper = (
        ack._time(value) for value in (binding.observed_at, binding.valid_from, binding.valid_until)
    )
    if now is None or acknowledged is None:
        return "unavailable"
    assert observed is not None and lower is not None and upper is not None
    if not acknowledged <= observed <= now or not lower <= observed < upper:
        return "indeterminate"
    if now >= upper:
        return "stale"
    return "current"


def evaluate_retention(
    *,
    acknowledgement: ack.AckAcceptance,
    subject: RetentionSubject,
    policy_revision: str,
    hold_revision: str,
    evaluated_at: str | None,
    declaration: RetentionDeclaration | None,
    enforcement: EnforcementObservation | None,
    hold: HoldObservation | None,
    archive: RecoveryObservation | None,
    restore: RecoveryObservation | None,
    authorities: tuple[OperationAuthority, ...] = (),
) -> RetentionAssessment:
    """Assess a prospective prune prerequisite without granting deletion rights.

    The caller admits the acknowledgement and independently sourced controls,
    supplying the current policy/hold revisions and evaluation time. This does
    not reauthenticate the acknowledgement or discover current destination state.
    """
    items = (
        (declaration, RetentionDeclaration),
        (enforcement, EnforcementObservation),
        (hold, HoldObservation),
        (archive, RecoveryObservation),
        (restore, RecoveryObservation),
    )
    if (
        type(acknowledgement) is not ack.AckAcceptance
        or type(subject) is not RetentionSubject
        or any(item is not None and type(item) is not expected for item, expected in items)
        or type(authorities) is not tuple
        or len(authorities) > 128
        or any(type(item) is not OperationAuthority for item in authorities)
    ):
        raise ValueError("invalid retention input type")
    now, acknowledged = ack._time(evaluated_at), ack._time(acknowledgement.acknowledged_at)
    reasons: list[str] = []
    if not _ack_bound(acknowledgement, subject, now):
        reasons.append("acknowledgement-binding")
    if now is None:
        reasons.append("evaluation-time-unavailable" if evaluated_at is None else "evaluation-time-indeterminate")
    if not ack._text(policy_revision) or not ack._text(hold_revision):
        reasons.append("revision-indeterminate")

    def binding_state(binding: ObservationBinding, source_class: str = "independent-operating-evidence") -> str:
        return _binding_state(
            binding,
            subject=subject,
            policy_revision=policy_revision,
            hold_revision=hold_revision,
            now=now,
            acknowledged=acknowledged,
            source_class=source_class,
        )

    declared_expiry: str | None = None
    declaration_state = "unavailable"
    if declaration is not None:
        declaration_state = binding_state(declaration.binding, "declared")
        if declaration_state == "current":
            try:
                start = ack._time(declaration.starts_at)
                if now is None or start is None or start > now:
                    raise ValueError
                declared_expiry = calendar_expiry(declaration.starts_at, declaration.period)
                declaration_state = "declared"
            except ValueError:
                declaration_state = "indeterminate"
    declared = ControlAssessment(
        "declaration",
        declaration_state,
        declaration.binding if declaration is not None and _binding_valid(declaration.binding) else None,
        "declared" if declaration is not None else None,
    )

    def control(
        kind: str, item: EnforcementObservation | HoldObservation | RecoveryObservation | None, allowed: frozenset[str]
    ) -> ControlAssessment:
        if item is None:
            return ControlAssessment(kind, "unavailable", None, None)
        observed_state = item.state if type(item.state) is str and item.state in allowed else None
        state = binding_state(item.binding)
        if state == "current":
            state = observed_state if observed_state in allowed else "indeterminate"
        return ControlAssessment(kind, state, item.binding if _binding_valid(item.binding) else None, observed_state)

    unknown = frozenset({"unknown", "unavailable", "indeterminate", "failed"})
    enforced = control("enforcement", enforcement, unknown | {"enforced"})
    held = control("hold", hold, unknown | {"active", "unheld", "released"})
    archived = control("archive", archive, unknown | {"verified"})
    restored = control("restore", restore, unknown | {"verified"})
    if archived.state == restored.state == "verified":
        assert restore is not None and archived.binding is not None and restored.binding is not None
        link = restore.archive_evidence_sha256
        recovery_state = "verified"
        if not ack._digest(link):
            recovery_state = "indeterminate"
        elif (
            link != archived.binding.evidence_sha256
            or restored.binding.evidence_sha256 == archived.binding.evidence_sha256
            or restored.binding.observed_at < archived.binding.observed_at
        ):
            recovery_state = "mismatch"
        restored = ControlAssessment("restore", recovery_state, restored.binding, restored.observed_state)
    expiry = declared_expiry
    if enforced.state == "enforced":
        assert enforcement is not None
        measured = ack._time(enforcement.retain_until)
        minimum = ack._time(declared_expiry)
        if measured is None:
            enforced = ControlAssessment("enforcement", "indeterminate", enforced.binding, enforced.observed_state)
        elif minimum is not None:
            if measured < minimum:
                enforced = ControlAssessment("enforcement", "mismatch", enforced.binding, enforced.observed_state)
            else:
                expiry = enforcement.retain_until

    permissions: list[AuthorityAssessment] = []
    keys = [
        (item.principal, item.operation)
        for item in authorities
        if ack._text(item.principal) and ack._text(item.operation)
    ]
    for item in authorities:
        principal = item.principal if ack._text(item.principal) else None
        operation = (
            item.operation
            if type(item.operation) is str and item.operation in {"write", "hold", "restore", "delete"}
            else None
        )
        state = binding_state(item.binding)
        if (
            principal is None
            or operation is None
            or type(item.state) is not str
            or item.state not in {"allowed", "denied", "unknown", "unavailable", "indeterminate"}
            or not _binding_valid(item.binding)
        ):
            state = "indeterminate"
        elif state == "current" and keys.count((principal, operation)) > 1:
            state = "ambiguous"
        elif state == "current":
            state = item.state
        permissions.append(
            AuthorityAssessment(principal, operation, state, item.binding if _binding_valid(item.binding) else None)
        )

    expiry_time = ack._time(expiry)
    retention_state = (
        "indeterminate" if expiry_time is None or now is None else "expired" if now >= expiry_time else "retained"
    )
    for assessment, supported in (
        (declared, {"declared"}),
        (enforced, {"enforced"}),
        (held, {"unheld", "released"}),
        (archived, {"verified"}),
        (restored, {"verified"}),
    ):
        if assessment.state not in supported:
            reasons.append(assessment.kind + "-" + assessment.state)
    if retention_state != "expired":
        reasons.append("retention-" + retention_state)
    if any(item.state not in {"allowed", "denied"} for item in permissions):
        reasons.append("authority-not-established")
    for operation in ("restore", "delete"):
        candidates = tuple(item for item in permissions if item.operation == operation)
        if not any(item.state == "allowed" for item in candidates) or any(
            item.state not in {"allowed", "denied"} for item in candidates
        ):
            reasons.append(operation + "-authority")
    return RetentionAssessment(
        subject if _subject_valid(subject) else None,
        evaluated_at if now is not None else None,
        policy_revision if ack._text(policy_revision) else None,
        hold_revision if ack._text(hold_revision) else None,
        declared,
        enforced,
        held,
        archived,
        restored,
        tuple(permissions),
        declaration.starts_at if declaration is not None and declared_expiry is not None else None,
        declaration.period if declaration is not None and declared_expiry is not None else None,
        declared_expiry,
        enforcement.retain_until if enforcement is not None and enforced.state == "enforced" else None,
        expiry,
        retention_state,
        "blocked" if reasons else "supported",
        tuple(reasons),
    )
