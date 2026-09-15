"""Shared selected-artifact and current-policy approval verification stages."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, NoReturn

from . import approval, approval_v2, attestation, run_journal


@dataclass(frozen=True)
class ValidatedApprovalArtifact:
    """The authenticated, selected approval statement and event bindings."""

    version: Literal[1, 2]
    statement: Mapping[str, Any]
    predicate: Mapping[str, Any]
    tree_fingerprint: str
    v1_receipt_subjects: frozenset[tuple[str, str]] = frozenset()
    v1_producer_keyids: frozenset[str] = frozenset()
    v2_bindings: approval_v2.SignedBindings | None = None


def _invalid_v1(message: str) -> NoReturn:
    raise approval.ApprovalError(message)


def _v1_signed_subjects(statement: Mapping[str, Any], *, strict: bool) -> tuple[str, frozenset[tuple[str, str]]] | None:
    signed = approval._signed_subjects(statement)
    if signed is None:
        return None
    if not strict:
        return signed[0], frozenset(signed[1])
    subjects = statement.get("subject")
    if not isinstance(subjects, list):
        return None
    for subject in subjects:
        if not isinstance(subject, Mapping) or set(subject) != {"name", "digest"}:
            return None
        name = subject.get("name")
        digest = subject.get("digest")
        expected_digest_keys = {"gitTree"} if name == "git:tree" else {"sha256"}
        if not isinstance(digest, Mapping) or set(digest) != expected_digest_keys:
            return None
    return signed[0], frozenset(signed[1])


def _validate_v1_artifact(
    *,
    target: Path,
    run_dir: Path,
    event: run_journal.RunEvent,
    decision_value: str | None,
    envelope: Mapping[str, Any],
    statement: Mapping[str, Any],
    statement_bytes: bytes,
    strict: bool,
) -> ValidatedApprovalArtifact:
    predicate = statement.get("predicate")
    if not isinstance(predicate, Mapping):
        _invalid_v1("approval predicate is invalid")
    approver, run_ref, policy = predicate.get("approver"), predicate.get("run"), predicate.get("policy")
    if not isinstance(approver, Mapping) or not isinstance(run_ref, Mapping) or not isinstance(policy, Mapping):
        _invalid_v1("approval predicate references are invalid")
    recorded_producer_keyids = approval._recorded_producer_keyids(predicate)
    signed = _v1_signed_subjects(statement, strict=strict)
    if recorded_producer_keyids is None:
        _invalid_v1("approval producer identities are invalid")
    if signed is None:
        _invalid_v1("approval subjects are invalid")
    tree_fingerprint, receipt_subjects = signed
    principal = approver.get("principal")
    signature = attestation.verify_attestation(
        envelope,
        allowed_signers_path=attestation.default_allowed_signers_path(target),
        target=target,
        principal=principal if isinstance(principal, str) else None,
        expected_predicate_type=approval.HUMAN_APPROVAL_PREDICATE_TYPE,
    )
    policy_digest = policy.get("digest")
    chain_head = run_ref.get("journalChainHead")
    valid = all(
        (
            signature.status == attestation.STATUS_SIGNED_OK,
            signature.principal == principal,
            signature.keyid == approver.get("keyid"),
            statement.get("_type") == attestation.IN_TOTO_STATEMENT_TYPE,
            statement.get("predicateType") == approval.HUMAN_APPROVAL_PREDICATE_TYPE,
            run_ref.get("id") == run_dir.name,
            isinstance(chain_head, Mapping) and chain_head.get("sha256") == event.previous_digest,
            predicate.get("schemaVersion") == 1,
            predicate.get("decision") == decision_value == event.payload.get("decision"),
            predicate.get("scope") == event.payload.get("scope"),
            principal == event.payload.get("approver_principal"),
            approver.get("keyid") == event.payload.get("approver_keyid"),
            predicate.get("nonce") == event.payload.get("nonce"),
            predicate.get("expiresAt") == event.payload.get("expires_at"),
            event.payload.get("producer_keyids") == sorted(recorded_producer_keyids),
            event.payload.get("subject_tree") == tree_fingerprint,
            hashlib.sha256(statement_bytes).hexdigest() == event.payload.get("statement_sha256"),
            policy.get("name") == approval.SOD_POLICY_NAME,
            isinstance(policy_digest, Mapping) and policy_digest.get("sha256") == approval.SOD_POLICY_SHA256,
            predicate.get("reasonCode") in approval.REASON_CODES,
            isinstance(predicate.get("reason"), str) and len(predicate["reason"]) <= 500,
            approval._parse_timestamp(predicate.get("decidedAt")) is not None,
            approval._parse_timestamp(predicate.get("expiresAt")) is not None,
            not strict or predicate.get("decidedAt") == event.payload.get("decided_at") == event.recorded_at,
        )
    )
    if not valid:
        _invalid_v1("approval signature or subjects do not match selected event")
    return ValidatedApprovalArtifact(
        1,
        statement,
        predicate,
        tree_fingerprint,
        v1_receipt_subjects=receipt_subjects,
        v1_producer_keyids=frozenset(recorded_producer_keyids),
    )


def _validate_v2_artifact(
    *,
    target: Path,
    run_dir: Path,
    event: run_journal.RunEvent,
    decision_value: str | None,
    envelope: Mapping[str, Any],
    statement: Mapping[str, Any],
    statement_bytes: bytes,
) -> ValidatedApprovalArtifact:
    predicate = statement.get("predicate")
    if not isinstance(predicate, Mapping):
        raise approval_v2.ApprovalV2Error("approval v2 predicate is invalid")
    approver, run_ref, policy = predicate.get("approver"), predicate.get("run"), predicate.get("policy")
    if not isinstance(approver, Mapping) or not isinstance(run_ref, Mapping) or not isinstance(policy, Mapping):
        raise approval_v2.ApprovalV2Error("approval v2 references are invalid")
    signed = approval_v2.parse_signed_bindings(statement)
    principal = approver.get("principal")
    signature = attestation.verify_attestation(
        envelope,
        allowed_signers_path=attestation.default_allowed_signers_path(target),
        target=target,
        principal=principal if isinstance(principal, str) else None,
        expected_predicate_type=approval.HUMAN_APPROVAL_V2_PREDICATE_TYPE,
    )
    policy_digest = policy.get("digest")
    chain_head = run_ref.get("journalChainHead")
    nonce = predicate.get("nonce")
    if not isinstance(nonce, str) or not approval_v2._HEX32_RE.fullmatch(nonce):
        raise approval_v2.ApprovalV2Error("approval v2 nonce is invalid")
    valid = all(
        (
            signature.status == attestation.STATUS_SIGNED_OK,
            signature.principal == principal,
            signature.keyid == approver.get("keyid"),
            statement.get("_type") == attestation.IN_TOTO_STATEMENT_TYPE,
            statement.get("predicateType") == approval.HUMAN_APPROVAL_V2_PREDICATE_TYPE,
            run_ref.get("id") == run_dir.name,
            isinstance(chain_head, Mapping) and chain_head.get("sha256") == event.previous_digest,
            predicate.get("schemaVersion") == 2,
            predicate.get("decision") == decision_value == event.payload.get("decision"),
            predicate.get("scope") == event.payload.get("scope"),
            principal == event.payload.get("approver_principal"),
            approver.get("keyid") == event.payload.get("approver_keyid"),
            predicate.get("nonce") == event.payload.get("nonce"),
            predicate.get("decidedAt") == event.payload.get("decided_at") == event.recorded_at,
            predicate.get("expiresAt") == event.payload.get("expires_at"),
            event.payload.get("producer_keyids") == sorted(signed.evidence.producer_keyids),
            event.payload.get("subject_tree") == signed.tree_fingerprint,
            hashlib.sha256(statement_bytes).hexdigest() == event.payload.get("statement_sha256"),
            policy.get("name") == approval_v2.SOD_POLICY_NAME,
            isinstance(policy_digest, Mapping) and policy_digest.get("sha256") == approval_v2.SOD_POLICY_SHA256,
            predicate.get("reasonCode") in approval.REASON_CODES,
            isinstance(predicate.get("reasonSha256"), str)
            and bool(approval._HEX64_RE.fullmatch(predicate["reasonSha256"])),
            approval._parse_timestamp(predicate.get("decidedAt")) is not None,
            approval._parse_timestamp(predicate.get("expiresAt")) is not None,
        )
    )
    if not valid:
        raise approval_v2.ApprovalV2Error("approval v2 signature or bindings are invalid")
    return ValidatedApprovalArtifact(2, statement, predicate, signed.tree_fingerprint, v2_bindings=signed)


def validate_approval_artifact(
    *,
    target: Path,
    run_dir: Path,
    report: run_journal.JournalReport,
    event: run_journal.RunEvent,
    decision_value: str | None,
    envelope: Mapping[str, Any],
    statement: Mapping[str, Any],
    statement_bytes: bytes,
    strict_v1: bool = False,
) -> ValidatedApprovalArtifact:
    """Validate one selected signed statement without consulting current state."""
    del report
    if statement.get("predicateType") == approval.HUMAN_APPROVAL_V2_PREDICATE_TYPE:
        return _validate_v2_artifact(
            target=target,
            run_dir=run_dir,
            event=event,
            decision_value=decision_value,
            envelope=envelope,
            statement=statement,
            statement_bytes=statement_bytes,
        )
    return _validate_v1_artifact(
        target=target,
        run_dir=run_dir,
        event=event,
        decision_value=decision_value,
        envelope=envelope,
        statement=statement,
        statement_bytes=statement_bytes,
        strict=strict_v1,
    )


def _invalid_current(
    run_id: str,
    decision_value: str | None,
    prior_approvals: tuple[dict[str, str | None], ...],
    *,
    live_tree: str | None = None,
    binding: str,
) -> approval.ApprovalVerification:
    return approval.ApprovalVerification(
        run_id,
        "APPROVAL-INVALID",
        decision_value,
        None,
        live_tree=live_tree,
        prior_approvals=prior_approvals,
        binding=binding,
    )


def evaluate_current_approval(
    *,
    target: Path,
    run_dir: Path,
    events: Sequence[run_journal.RunEvent],
    event: run_journal.RunEvent,
    decision_value: str | None,
    prior_approvals: tuple[dict[str, str | None], ...],
    artifact: ValidatedApprovalArtifact,
    run_meta: Mapping[str, Any],
    context: approval.ApprovalVerificationContext,
    now: datetime | None,
) -> approval.ApprovalVerification:
    """Evaluate freshness and SoD using an already validated selected artifact."""
    run_id = run_dir.name
    live_tree = context.live_tree
    comparison_tree = live_tree if live_tree is not None else run_meta.get("tree_fingerprint")
    binding = "test-result" if artifact.version == 2 else "receipt"
    if not isinstance(comparison_tree, str) or not comparison_tree:
        return _invalid_current(run_id, decision_value, prior_approvals, live_tree=live_tree, binding=binding)
    instant = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    reason_stale = False
    if artifact.version == 1:
        receipts = approval.collect_verify_receipts(
            target, run_id, tree_fingerprint=artifact.tree_fingerprint, context=context
        )
        current_subjects = {(f"verify:{receipt.run_id}", receipt.receipt_sha256) for receipt in receipts}
        missing_subjects = artifact.v1_receipt_subjects - current_subjects
        signed_receipts = [
            receipt
            for receipt in receipts
            if (f"verify:{receipt.run_id}", receipt.receipt_sha256) in artifact.v1_receipt_subjects
        ]
        sod = approval.evaluate_sod(
            target=target,
            statement=artifact.statement,
            run_meta=run_meta,
            receipts=signed_receipts,
            events=events,
            approval_sequence=event.sequence,
            now=instant,
            recorded_producer_keyids=set(artifact.v1_producer_keyids),
            context=context,
        )
        stale = comparison_tree != artifact.tree_fingerprint or bool(missing_subjects)
    else:
        assert artifact.v2_bindings is not None
        try:
            current_requester = approval_v2.verify_recorded_request(target, run_dir, events)
        except (approval_v2.ApprovalV2Error, OSError):
            return _invalid_current(run_id, decision_value, prior_approvals, binding=binding)
        evidence_stale = False
        if artifact.v2_bindings.evidence.test_results:
            try:
                current_evidence = approval_v2.collect_test_result_evidence(target, run_id, artifact.tree_fingerprint)
            except (approval_v2.ApprovalV2Error, OSError):
                evidence_stale, current_evidence = True, None
            if current_evidence != artifact.v2_bindings.evidence:
                evidence_stale = True
        elif decision_value == "allow":
            return _invalid_current(run_id, decision_value, prior_approvals, binding=binding)
        requester_stale = current_requester != artifact.v2_bindings.requester
        local_approval = run_meta.get("approval")
        local_reason = local_approval.get("reason") if isinstance(local_approval, Mapping) else None
        reason_stale = not isinstance(local_reason, str) or approval_v2.reason_sha256(
            artifact.predicate["nonce"], local_reason
        ) != artifact.predicate.get("reasonSha256")
        sod = approval_v2.evaluate_sod(
            statement=artifact.statement,
            evidence=artifact.v2_bindings.evidence,
            requester=artifact.v2_bindings.requester,
            events=events,
            approval_sequence=event.sequence,
            workspace_keyid=context.workspace_keyid,
            now=instant,
        )
        stale = comparison_tree != artifact.tree_fingerprint or evidence_stale or requester_stale or reason_stale
    expires_at = approval._parse_timestamp(artifact.predicate.get("expiresAt"))
    detail = "APPROVAL-STALE" if stale else None
    if any(check["status"] == "failed" and check["id"] != "approval-not-expired" for check in sod["checks"]):
        status = "SOD-VIOLATION"
    elif decision_value == "allow" and expires_at is not None and expires_at <= instant:
        status = "APPROVAL-EXPIRED"
    elif reason_stale or (decision_value == "allow" and stale):
        status = "APPROVAL-STALE"
    elif artifact.version == 2 and sod["result"] == "INDETERMINATE":
        status = "SOD-INDETERMINATE"
    elif decision_value == "allow":
        status = "APPROVED"
    elif decision_value == "deny":
        status = "DENIED"
    elif decision_value == "hold":
        status = "HELD"
    else:
        status = "APPROVAL-INVALID"
    return approval.ApprovalVerification(
        run_id,
        status,
        decision_value,
        sod,
        live_tree=live_tree,
        prior_approvals=prior_approvals,
        detail=detail,
        binding=binding,
    )


def verify_selected_approval(
    *,
    target: Path,
    run_dir: Path,
    report: run_journal.JournalReport,
    event: run_journal.RunEvent,
    decision_value: str | None,
    prior_approvals: tuple[dict[str, str | None], ...],
    envelope: Mapping[str, Any],
    statement: Mapping[str, Any],
    statement_bytes: bytes,
    run_meta: Mapping[str, Any],
    context: approval.ApprovalVerificationContext,
    now: datetime | None,
    strict_v1: bool = False,
) -> approval.ApprovalVerification:
    """Verify a caller-selected approval artifact through both stages."""
    try:
        artifact = validate_approval_artifact(
            target=target,
            run_dir=run_dir,
            report=report,
            event=event,
            decision_value=decision_value,
            envelope=envelope,
            statement=statement,
            statement_bytes=statement_bytes,
            strict_v1=strict_v1,
        )
        return evaluate_current_approval(
            target=target,
            run_dir=run_dir,
            events=report.events,
            event=event,
            decision_value=decision_value,
            prior_approvals=prior_approvals,
            artifact=artifact,
            run_meta=run_meta,
            context=context,
            now=now,
        )
    except (approval.ApprovalError, approval_v2.ApprovalV2Error, OSError):
        binding = (
            "test-result" if statement.get("predicateType") == approval.HUMAN_APPROVAL_V2_PREDICATE_TYPE else "receipt"
        )
        return _invalid_current(run_dir.name, decision_value, prior_approvals, binding=binding)


def verify_v1_approval(
    *,
    target: Path,
    run_dir: Path,
    report: run_journal.JournalReport,
    event: run_journal.RunEvent,
    decision_value: str | None,
    prior_approvals: tuple[dict[str, str | None], ...],
    envelope: Mapping[str, Any],
    statement: Mapping[str, Any],
    statement_bytes: bytes,
    run_meta: Mapping[str, Any],
    context: approval.ApprovalVerificationContext,
    now: datetime | None,
) -> approval.ApprovalVerification:
    """Standalone compatibility wrapper for legacy v1 approval verification."""
    return verify_selected_approval(
        target=target,
        run_dir=run_dir,
        report=report,
        event=event,
        decision_value=decision_value,
        prior_approvals=prior_approvals,
        envelope=envelope,
        statement=statement,
        statement_bytes=statement_bytes,
        run_meta=run_meta,
        context=context,
        now=now,
    )


def verify_v2_approval(
    *,
    target: Path,
    run_dir: Path,
    report: run_journal.JournalReport,
    event: run_journal.RunEvent,
    decision_value: str | None,
    prior_approvals: tuple[dict[str, str | None], ...],
    envelope: Mapping[str, Any],
    statement: Mapping[str, Any],
    statement_bytes: bytes,
    run_meta: Mapping[str, Any],
    context: approval.ApprovalVerificationContext,
    now: datetime | None,
) -> approval.ApprovalVerification:
    """Standalone compatibility wrapper for v2 approval verification."""
    return verify_selected_approval(
        target=target,
        run_dir=run_dir,
        report=report,
        event=event,
        decision_value=decision_value,
        prior_approvals=prior_approvals,
        envelope=envelope,
        statement=statement,
        statement_bytes=statement_bytes,
        run_meta=run_meta,
        context=context,
        now=now,
    )
