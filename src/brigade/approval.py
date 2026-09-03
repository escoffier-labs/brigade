"""Signed human approval statements for completed Brigade run artifacts."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import attestation, localio, run_events, run_journal, run_projector

HUMAN_APPROVAL_PREDICATE_TYPE = "https://brigade.dev/attestation/human-approval/v1"
SOD_POLICY_NAME = "brigade.sod.v1"
SOD_POLICY_TEXT = """brigade.sod.v1
approver.kind must be human.
approver keyid must differ from every verify receipt signing key.
approver principal and keyid must differ from the requester when known.
approver keyid must differ from the workspace attestation key when present.
approval must precede every merge or ship stage event.
an allow decision must be unexpired at verification time.
"""
SOD_POLICY_SHA256 = hashlib.sha256(SOD_POLICY_TEXT.encode("utf-8")).hexdigest()

DECISIONS = frozenset({"allow", "deny", "hold"})
SCOPES = frozenset({"run", "merge"})
REASON_CODES = frozenset(
    {
        "reviewed-diff",
        "reviewed-tests",
        "policy-exception",
        "rejected-change",
        "needs-rework",
        "other",
    }
)
APPROVAL_STATUSES = frozenset(
    {
        "APPROVED",
        "DENIED",
        "HELD",
        "UNAPPROVED",
        "APPROVAL-INVALID",
        "APPROVAL-STALE",
        "SOD-VIOLATION",
        "APPROVAL-EXPIRED",
    }
)
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_HEX32_RE = re.compile(r"^[0-9a-f]{32}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_DURATION_RE = re.compile(r"^(\d+)([smhd])$")
_DURATION_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_APPROVAL_EVENT_TYPE = "approval"


class ApprovalError(RuntimeError):
    """Approval input or recorded evidence is invalid."""


@dataclass(frozen=True)
class VerifyReceiptEvidence:
    run_id: str
    receipt_sha256: str
    signing_keyids: frozenset[str]
    producer_keyid_unavailable: bool = False


@dataclass
class ApprovalVerificationContext:
    """Per-verification facts and cached receipt queries."""

    target: Path
    live_tree: str | None
    live_tree_state: str
    workspace_keyid: str | None
    workspace_keyid_state: str
    receipts: dict[tuple[str, str | None], list[VerifyReceiptEvidence]]


@dataclass(frozen=True)
class ApprovalVerification:
    run_id: str
    status: str
    decision: str | None
    sod: dict[str, Any] | None
    live_tree: str | None = None
    prior_approvals: tuple[dict[str, str | None], ...] = ()
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "decision": self.decision,
            "sod": self.sod,
            "live_tree": self.live_tree if self.live_tree is not None else "unavailable",
            "prior_approvals": list(self.prior_approvals),
            "detail": self.detail,
        }


def parse_duration(value: str) -> timedelta:
    """Parse a bounded CLI duration such as 30m, 24h, or 2d."""
    match = _DURATION_RE.fullmatch(value.strip())
    if match is None:
        raise ApprovalError("--expires-in must look like 30m, 24h, 2d, or 90s")
    seconds = int(match.group(1)) * _DURATION_SECONDS[match.group(2)]
    if seconds > 365 * 86400:
        raise ApprovalError("--expires-in must not exceed 365d")
    return timedelta(seconds=seconds)


def _utc_timestamp(value: datetime) -> str:
    return run_events.format_recorded_at(value.astimezone(timezone.utc))


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _load_json_object(path: Path, *, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ApprovalError(f"{label} is missing or not a regular file")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApprovalError(f"{label} is not readable JSON") from exc
    if not isinstance(payload, dict):
        raise ApprovalError(f"{label} must contain a JSON object")
    return payload


def _resolve_run_dir(target: Path, run_id: str) -> Path:
    target = target.expanduser().resolve()
    if not target.is_dir():
        raise ApprovalError(f"--target is not a directory: {target}")
    if not _RUN_ID_RE.fullmatch(run_id) or run_id in {".", ".."}:
        raise ApprovalError("run id must contain only letters, digits, dot, underscore, or hyphen")
    runs_root = (target / ".brigade" / "runs").resolve()
    run_dir = (runs_root / run_id).resolve()
    if run_dir.parent != runs_root or run_dir.name != run_id or not run_dir.is_dir() or run_dir.is_symlink():
        raise ApprovalError(f"run directory not found: {run_id}")
    return run_dir


def _read_journal(run_dir: Path) -> run_journal.JournalReport:
    path = run_dir / "events" / "lifecycle.jsonl"
    try:
        report = run_journal.read_journal_bounded(path)
    except (OSError, run_journal.RunJournalError) as exc:
        raise ApprovalError("lifecycle journal is not readable") from exc
    if report.partial_tail is not None or report.chain_errors:
        raise ApprovalError("lifecycle journal is corrupt")
    return report


def _read_journal_report(run_dir: Path) -> run_journal.JournalReport:
    try:
        return run_journal.read_journal_bounded(run_dir / "events" / "lifecycle.jsonl")
    except (OSError, run_journal.RunJournalError) as exc:
        raise ApprovalError("lifecycle journal is not readable") from exc


def _verification_context(target: Path) -> ApprovalVerificationContext:
    live_tree = localio.tree_fingerprint(target)
    try:
        workspace_key_path = attestation.resolve_signing_key_path(target)
        workspace_keyid = attestation.get_key_fingerprint(workspace_key_path) if workspace_key_path.is_file() else None
    except attestation.AttestationError:
        workspace_keyid = None
    return ApprovalVerificationContext(
        target=target,
        live_tree=live_tree,
        live_tree_state="computed" if live_tree is not None else "unavailable",
        workspace_keyid=workspace_keyid,
        workspace_keyid_state="computed" if workspace_keyid is not None else "unavailable",
        receipts={},
    )


def collect_verify_receipts(
    target: Path,
    producer_run_id: str,
    *,
    tree_fingerprint: str | None = None,
    context: ApprovalVerificationContext | None = None,
) -> list[VerifyReceiptEvidence]:
    """Collect receipt subjects attributed to one orchestration run."""
    cache_key = (producer_run_id, tree_fingerprint)
    if context is not None and cache_key in context.receipts:
        return list(context.receipts[cache_key])
    root = target / ".brigade" / "work" / "verify-runs"
    evidence: list[VerifyReceiptEvidence] = []
    for receipt_path in sorted(root.glob("*/receipt.json")):
        if receipt_path.parent.is_symlink():
            continue
        try:
            receipt = _load_json_object(receipt_path, label="verify receipt")
        except ApprovalError:
            continue
        if receipt.get("producer_run_id") != producer_run_id:
            continue
        receipt_tree = receipt.get("tree_fingerprint")
        if tree_fingerprint is not None and receipt_tree != tree_fingerprint:
            continue
        verify_run_id = receipt.get("run_id")
        digests = receipt.get("digests")
        receipt_sha256 = digests.get("receipt_sha256") if isinstance(digests, Mapping) else None
        if not isinstance(verify_run_id, str) or not _RUN_ID_RE.fullmatch(verify_run_id):
            raise ApprovalError("matching verify receipt has an invalid run_id")
        if not isinstance(receipt_sha256, str) or not _HEX64_RE.fullmatch(receipt_sha256):
            raise ApprovalError("matching verify receipt has no valid receipt_sha256")
        keyids: set[str] = set()
        hmac_keyid = digests.get("key_id") if isinstance(digests, Mapping) else None
        if isinstance(hmac_keyid, str) and hmac_keyid:
            keyids.add(hmac_keyid)
        attestation_path = receipt_path.parent / "attestation.json"
        attestation_unparseable = False
        if attestation_path.exists():
            verified_attestation = attestation.verify_attestation(attestation_path, target=target)
            if verified_attestation.status == attestation.STATUS_SIGNED_OK and verified_attestation.keyid:
                keyids.add(verified_attestation.keyid)
            else:
                attestation_unparseable = True
        evidence.append(
            VerifyReceiptEvidence(
                run_id=verify_run_id,
                receipt_sha256=receipt_sha256,
                signing_keyids=frozenset(keyids),
                producer_keyid_unavailable=attestation_unparseable,
            )
        )
    if context is not None:
        context.receipts[cache_key] = list(evidence)
    return evidence


def _subjects(tree_fingerprint: str, receipts: Sequence[VerifyReceiptEvidence]) -> list[dict[str, Any]]:
    return [
        {"name": "git:tree", "digest": {"gitTree": tree_fingerprint}},
        *[{"name": f"verify:{receipt.run_id}", "digest": {"sha256": receipt.receipt_sha256}} for receipt in receipts],
    ]


def _is_merge_or_ship_event(event: run_journal.RunEvent) -> bool:
    event_type = event.event_type.lower()
    if event_type in {"merge", "ship", "run.merge", "run.ship"}:
        return True
    if event_type.startswith(("merge.", "ship.", "run.merge.", "run.ship.")):
        return True
    stage = event.payload.get("stage")
    return isinstance(stage, str) and stage.lower() in {"merge", "ship"}


def evaluate_sod(
    *,
    target: Path,
    statement: Mapping[str, Any],
    run_meta: Mapping[str, Any],
    receipts: Sequence[VerifyReceiptEvidence],
    events: Sequence[run_journal.RunEvent],
    now: datetime,
    recorded_producer_keyids: set[str] | None = None,
    context: ApprovalVerificationContext | None = None,
) -> dict[str, Any]:
    """Evaluate the five brigade.sod.v1 rules over recorded evidence."""
    predicate = statement.get("predicate")
    predicate = predicate if isinstance(predicate, Mapping) else {}
    approver = predicate.get("approver")
    approver = approver if isinstance(approver, Mapping) else {}
    principal = approver.get("principal")
    keyid = approver.get("keyid")
    decision = predicate.get("decision")
    decided_at = _parse_timestamp(predicate.get("decidedAt"))
    expires_at = _parse_timestamp(predicate.get("expiresAt"))

    checks: list[dict[str, str]] = []
    checks.append(
        {
            "id": "approver-is-human",
            "status": "passed" if approver.get("kind") == "human" else "failed",
            "detail": "declared",
        }
    )
    producer_keyids = {item for receipt in receipts for item in receipt.signing_keyids}
    producer_keyids.update(recorded_producer_keyids or set())
    unavailable_receipts = [receipt.run_id for receipt in receipts if receipt.producer_keyid_unavailable]
    if unavailable_receipts:
        checks.append(
            {
                "id": "producer-identity-unverifiable",
                "status": "failed",
                "detail": f"receipt {unavailable_receipts[0]}",
            }
        )
    checks.append(
        {
            "id": "approver-not-producer",
            "status": "passed"
            if isinstance(keyid, str) and keyid not in producer_keyids and not unavailable_receipts
            else "failed",
        }
    )
    requester = run_meta.get("requester_principal")
    requester_keyid = run_meta.get("requester_keyid")
    if isinstance(requester, str) and requester:
        checks.append(
            {
                "id": "approver-not-requester",
                "status": "passed"
                if isinstance(principal, str)
                and principal != requester
                and (not isinstance(requester_keyid, str) or keyid != requester_keyid)
                else "failed",
            }
        )
    else:
        checks.append({"id": "requester-unknown", "status": "passed"})

    workspace_keyid = context.workspace_keyid if context is not None else _verification_context(target).workspace_keyid
    checks.append(
        {
            "id": "approver-key-not-workspace-key",
            "status": "passed" if workspace_keyid is None or keyid != workspace_keyid else "failed",
        }
    )

    late = decided_at is None
    if decided_at is not None:
        for event in events:
            if not _is_merge_or_ship_event(event):
                continue
            stage_time = _parse_timestamp(event.recorded_at)
            if stage_time is None or decided_at >= stage_time:
                late = True
                break
    checks.append({"id": "approval-before-merge-ship", "status": "failed" if late else "passed"})

    expired = decision == "allow" and (expires_at is None or expires_at <= now.astimezone(timezone.utc))
    checks.append({"id": "approval-not-expired", "status": "failed" if expired else "passed"})
    result = "PASSED" if all(check["status"] == "passed" for check in checks) else "FAILED"
    return {"result": result, "checks": checks}


def build_statement(
    *,
    run_id: str,
    journal_chain_head: str,
    tree_fingerprint: str,
    receipts: Sequence[VerifyReceiptEvidence],
    decision: str,
    scope: str,
    principal: str,
    keyid: str,
    decided_at: str,
    expires_at: str,
    nonce: str,
    reason_code: str,
    reason: str,
) -> dict[str, Any]:
    """Build the canonical human-approval in-toto Statement."""
    return {
        "_type": attestation.IN_TOTO_STATEMENT_TYPE,
        "subject": _subjects(tree_fingerprint, receipts),
        "predicateType": HUMAN_APPROVAL_PREDICATE_TYPE,
        "predicate": {
            "schemaVersion": 1,
            "run": {"id": run_id, "journalChainHead": {"sha256": journal_chain_head}},
            "decision": decision,
            "scope": scope,
            "approver": {"principal": principal, "keyid": keyid, "kind": "human"},
            "decidedAt": decided_at,
            "expiresAt": expires_at,
            "nonce": nonce,
            "reasonCode": reason_code,
            "reason": reason,
            "policy": {"name": SOD_POLICY_NAME, "digest": {"sha256": SOD_POLICY_SHA256}},
            "producers": [
                {"receipt": receipt.run_id, "keyid": producer_keyid}
                for receipt in receipts
                for producer_keyid in sorted(receipt.signing_keyids)
            ],
        },
    }


def _discover_principal(statement: dict[str, Any], key_path: Path, target: Path) -> str:
    probe = attestation.create_envelope(statement, key_path)
    result = attestation.verify_attestation(
        probe,
        allowed_signers_path=attestation.default_allowed_signers_path(target),
        target=target,
        expected_predicate_type=HUMAN_APPROVAL_PREDICATE_TYPE,
    )
    if result.status != attestation.STATUS_SIGNED_OK or not result.principal:
        raise ApprovalError("signing key is not trusted by the target allowed_signers policy")
    return result.principal


def _validate_reason(reason: str) -> str:
    if len(reason) > 500:
        raise ApprovalError("--reason must not exceed 500 characters")
    if any(ord(char) < 32 and char not in {"\t"} for char in reason):
        raise ApprovalError("--reason must be plain text without line breaks or control characters")
    for token in reason.split():
        candidate = token.strip("()[]{}<>,;:\"'")
        if Path(candidate).is_absolute() or re.match(r"^[A-Za-z]:[\\/]", candidate):
            raise ApprovalError("--reason must not contain absolute paths")
    return reason


def record_approval(
    *,
    target: Path,
    run_id: str,
    decision: str,
    scope: str = "run",
    reason_code: str = "other",
    reason: str = "",
    key: Path | None = None,
    principal: str | None = None,
    expires_in: str = "24h",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Sign, append, and project one approval decision."""
    if decision not in DECISIONS:
        raise ApprovalError("decision must be allow, deny, or hold")
    if scope not in SCOPES:
        raise ApprovalError("scope must be run or merge")
    if reason_code not in REASON_CODES:
        raise ApprovalError("reason code is not recognized")
    reason = _validate_reason(reason)
    target = target.expanduser().resolve()
    run_dir = _resolve_run_dir(target, run_id)
    run_meta = _load_json_object(run_dir / "run.json", label="run.json")
    tree_fingerprint = run_meta.get("tree_fingerprint")
    if not isinstance(tree_fingerprint, str) or not tree_fingerprint:
        raise ApprovalError("run.json has no final tree_fingerprint")
    report = _read_journal(run_dir)
    if not report.events:
        raise ApprovalError("lifecycle journal has no chain head")
    chain_head = report.events[-1].event_digest
    receipts = collect_verify_receipts(target, run_id, tree_fingerprint=tree_fingerprint)
    if decision == "allow" and not receipts:
        raise ApprovalError("allow requires at least one verify receipt")

    instant = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    decided_at = _utc_timestamp(instant)
    expires_at = _utc_timestamp(instant + parse_duration(expires_in))
    nonce = secrets.token_hex(16)
    if any(event.payload.get("nonce") == nonce for event in report.events if event.event_type == _APPROVAL_EVENT_TYPE):
        raise ApprovalError("approval nonce was already used for this run")
    run_projector.project_run_snapshot(run_meta, report.events, journal_present=True)
    key_path = attestation.resolve_signing_key_path(target, key_file=key)
    keyid = attestation.get_key_fingerprint(key_path)
    tentative_principal = principal or "principal-pending-verification"
    statement = build_statement(
        run_id=run_id,
        journal_chain_head=chain_head,
        tree_fingerprint=tree_fingerprint,
        receipts=receipts,
        decision=decision,
        scope=scope,
        principal=tentative_principal,
        keyid=keyid,
        decided_at=decided_at,
        expires_at=expires_at,
        nonce=nonce,
        reason_code=reason_code,
        reason=reason,
    )
    resolved_principal = principal or _discover_principal(statement, key_path, target)
    if resolved_principal != tentative_principal:
        statement = build_statement(
            run_id=run_id,
            journal_chain_head=chain_head,
            tree_fingerprint=tree_fingerprint,
            receipts=receipts,
            decision=decision,
            scope=scope,
            principal=resolved_principal,
            keyid=keyid,
            decided_at=decided_at,
            expires_at=expires_at,
            nonce=nonce,
            reason_code=reason_code,
            reason=reason,
        )
    envelope = attestation.create_envelope(statement, key_path)
    signature_check = attestation.verify_attestation(
        envelope,
        allowed_signers_path=attestation.default_allowed_signers_path(target),
        target=target,
        principal=resolved_principal,
        expected_predicate_type=HUMAN_APPROVAL_PREDICATE_TYPE,
    )
    if signature_check.status != attestation.STATUS_SIGNED_OK:
        raise ApprovalError("signing key and principal are not trusted by the target allowed_signers policy")

    sod = evaluate_sod(
        target=target,
        statement=statement,
        run_meta=run_meta,
        receipts=receipts,
        events=report.events,
        now=instant,
    )
    statement_sha256 = hashlib.sha256(attestation.canonical_statement_bytes(statement)).hexdigest()
    relative_path = Path("approvals") / f"{nonce}.json"
    event = run_journal.append_event(
        run_dir / "events" / "lifecycle.jsonl",
        run_id=run_id,
        event_type=_APPROVAL_EVENT_TYPE,
        payload={
            "decision": decision,
            "scope": scope,
            "approver_principal": resolved_principal,
            "approver_keyid": keyid,
            "subject_tree": tree_fingerprint,
            "nonce": nonce,
            "expires_at": expires_at,
            "statement_sha256": statement_sha256,
            "attestation_path": relative_path.as_posix(),
            "producer_keyids": sorted({keyid for receipt in receipts for keyid in receipt.signing_keyids}),
        },
        idempotency_key=f"approval:{nonce}",
        expected_previous_sequence=report.events[-1].sequence,
        recorded_at=decided_at,
    )
    attestation.write_attestation_file(envelope, run_dir / relative_path)
    approval_projection = {
        "decision": decision,
        "scope": scope,
        "approver_principal": resolved_principal,
        "decided_at": decided_at,
        "expires_at": expires_at,
        "nonce": nonce,
        "statement_sha256": statement_sha256,
        "approver_keyid": keyid,
        "subject_tree": tree_fingerprint,
        "attestation_path": relative_path.as_posix(),
        "sod": sod,
    }
    updated = dict(run_meta)
    updated["approval"] = approval_projection
    after = _read_journal(run_dir)
    projection = run_projector.project_run_snapshot(updated, after.events, journal_present=True)
    localio.write_bytes_atomic(run_dir / "run.json", projection.to_bytes())
    return {"event": event.to_dict(), "approval": approval_projection}


def approve_cli(
    *,
    target: Path,
    run_id: str,
    decision: str,
    scope: str,
    reason_code: str,
    reason: str,
    key: Path | None,
    principal: str | None,
    expires_in: str,
) -> int:
    try:
        result = record_approval(
            target=target,
            run_id=run_id,
            decision=decision,
            scope=scope,
            reason_code=reason_code,
            reason=reason,
            key=key,
            principal=principal,
            expires_in=expires_in,
        )
    except run_projector.ProjectionError as exc:
        print(f"error: {exc.diagnostic}", file=sys.stderr)
        return 1
    except (ApprovalError, attestation.AttestationError, FileNotFoundError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    sod = result["approval"]["sod"]
    print(f"approval: {decision}")
    print(f"sod: {sod['result']}")
    for check in sod["checks"]:
        print(f"- {check['id']}: {check['status']}")
    return 0 if sod["result"] == "PASSED" else 3


def _decode_statement(envelope: Mapping[str, Any]) -> tuple[dict[str, Any] | None, bytes | None]:
    payload = envelope.get("payload")
    if not isinstance(payload, str):
        return None, None
    try:
        payload_bytes = base64.b64decode(payload, validate=True)
        statement = json.loads(payload_bytes.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None, None
    return (statement, payload_bytes) if isinstance(statement, dict) else (None, None)


def _latest_approval_event(events: Sequence[run_journal.RunEvent]) -> run_journal.RunEvent | None:
    return next((event for event in reversed(events) if event.event_type == _APPROVAL_EVENT_TYPE), None)


def _prior_approvals(events: Sequence[run_journal.RunEvent]) -> tuple[dict[str, str | None], ...]:
    approvals = [event for event in events if event.event_type == _APPROVAL_EVENT_TYPE]
    return tuple(
        {
            "decision": event.payload.get("decision") if isinstance(event.payload.get("decision"), str) else None,
            "principal": (
                event.payload.get("approver_principal")
                if isinstance(event.payload.get("approver_principal"), str)
                else None
            ),
            "decided_at": event.recorded_at,
        }
        for event in approvals[:-1]
    )


def _signed_subjects(statement: Mapping[str, Any]) -> tuple[str, set[tuple[str, str]]] | None:
    raw_subjects = statement.get("subject")
    if not isinstance(raw_subjects, list):
        return None
    tree: str | None = None
    receipts: set[tuple[str, str]] = set()
    for subject in raw_subjects:
        if not isinstance(subject, Mapping):
            return None
        name = subject.get("name")
        digest = subject.get("digest")
        if not isinstance(name, str) or not isinstance(digest, Mapping):
            return None
        if name == "git:tree":
            value = digest.get("gitTree")
            if tree is not None or not isinstance(value, str) or not value:
                return None
            tree = value
        elif name.startswith("verify:"):
            value = digest.get("sha256")
            if not isinstance(value, str) or not _HEX64_RE.fullmatch(value):
                return None
            subject_key = (name, value)
            if subject_key in receipts:
                return None
            receipts.add(subject_key)
        else:
            return None
    return (tree, receipts) if tree is not None else None


def _recorded_producer_keyids(predicate: Mapping[str, Any]) -> set[str] | None:
    producers = predicate.get("producers")
    if not isinstance(producers, list):
        return set()
    keyids: set[str] = set()
    for producer in producers:
        if not isinstance(producer, Mapping):
            return None
        receipt = producer.get("receipt")
        keyid = producer.get("keyid")
        if not isinstance(receipt, str) or not _RUN_ID_RE.fullmatch(receipt) or not isinstance(keyid, str) or not keyid:
            return None
        keyids.add(keyid)
    return keyids


def verify_run_approval(
    target: Path,
    run_dir: Path,
    *,
    now: datetime | None = None,
    context: ApprovalVerificationContext | None = None,
) -> ApprovalVerification:
    """Verify the latest signed approval event for one run directory."""
    run_id = run_dir.name
    try:
        report = _read_journal_report(run_dir)
    except ApprovalError:
        return ApprovalVerification(run_id, "APPROVAL-INVALID", None, None)
    event = _latest_approval_event(report.events)
    if event is None:
        return ApprovalVerification(run_id, "UNAPPROVED", None, None)
    decision = event.payload.get("decision")
    decision_value = decision if isinstance(decision, str) else None
    prior_approvals = _prior_approvals(report.events)
    if report.partial_tail is not None or report.chain_errors:
        return ApprovalVerification(run_id, "APPROVAL-INVALID", decision_value, None, prior_approvals=prior_approvals)
    try:
        context = context or _verification_context(target)
        run_meta = _load_json_object(run_dir / "run.json", label="run.json")
        nonce = event.payload.get("nonce")
        rel = event.payload.get("attestation_path")
        if not isinstance(nonce, str) or not _HEX32_RE.fullmatch(nonce):
            raise ApprovalError("approval event nonce is invalid")
        expected_rel = f"approvals/{nonce}.json"
        if rel != expected_rel:
            raise ApprovalError("approval attestation path is invalid")
        envelope = _load_json_object(run_dir / expected_rel, label="approval attestation")
        statement, statement_bytes = _decode_statement(envelope)
        if statement is None or statement_bytes is None:
            raise ApprovalError("approval statement is invalid")
        predicate = statement.get("predicate")
        if not isinstance(predicate, Mapping):
            raise ApprovalError("approval predicate is invalid")
        approver = predicate.get("approver")
        run_ref = predicate.get("run")
        policy = predicate.get("policy")
        if not isinstance(approver, Mapping) or not isinstance(run_ref, Mapping) or not isinstance(policy, Mapping):
            raise ApprovalError("approval predicate references are invalid")
        recorded_producer_keyids = _recorded_producer_keyids(predicate)
        if recorded_producer_keyids is None:
            raise ApprovalError("approval producer identities are invalid")
        signed = _signed_subjects(statement)
        if signed is None:
            raise ApprovalError("approval subjects are invalid")
        approved_tree, signed_receipt_subjects = signed
        live_tree = context.live_tree
        comparison_tree = live_tree if live_tree is not None else run_meta.get("tree_fingerprint")
        if not isinstance(comparison_tree, str) or not comparison_tree:
            raise ApprovalError("run.json has no final tree_fingerprint")
        principal = approver.get("principal")
        signature = attestation.verify_attestation(
            envelope,
            allowed_signers_path=attestation.default_allowed_signers_path(target),
            target=target,
            principal=principal if isinstance(principal, str) else None,
            expected_predicate_type=HUMAN_APPROVAL_PREDICATE_TYPE,
        )
        receipts = collect_verify_receipts(target, run_id, tree_fingerprint=approved_tree, context=context)
        current_receipt_subjects = {(f"verify:{receipt.run_id}", receipt.receipt_sha256) for receipt in receipts}
        missing_subjects = signed_receipt_subjects - current_receipt_subjects
        signed_receipts = [
            receipt
            for receipt in receipts
            if (f"verify:{receipt.run_id}", receipt.receipt_sha256) in signed_receipt_subjects
        ]
        policy_digest = policy.get("digest")
        chain_head = run_ref.get("journalChainHead")
        valid = all(
            (
                signature.status == attestation.STATUS_SIGNED_OK,
                signature.principal == principal,
                signature.keyid == approver.get("keyid"),
                statement.get("_type") == attestation.IN_TOTO_STATEMENT_TYPE,
                statement.get("predicateType") == HUMAN_APPROVAL_PREDICATE_TYPE,
                run_ref.get("id") == run_id,
                isinstance(chain_head, Mapping) and chain_head.get("sha256") == event.previous_digest,
                predicate.get("schemaVersion") == 1,
                predicate.get("decision") == decision,
                predicate.get("scope") == event.payload.get("scope"),
                principal == event.payload.get("approver_principal"),
                approver.get("keyid") == event.payload.get("approver_keyid"),
                predicate.get("nonce") == nonce,
                predicate.get("expiresAt") == event.payload.get("expires_at"),
                event.payload.get("producer_keyids") == sorted(recorded_producer_keyids),
                event.payload.get("subject_tree") == approved_tree,
                hashlib.sha256(statement_bytes).hexdigest() == event.payload.get("statement_sha256"),
                policy.get("name") == SOD_POLICY_NAME,
                isinstance(policy_digest, Mapping) and policy_digest.get("sha256") == SOD_POLICY_SHA256,
                predicate.get("reasonCode") in REASON_CODES,
                isinstance(predicate.get("reason"), str) and len(predicate["reason"]) <= 500,
                _parse_timestamp(predicate.get("decidedAt")) is not None,
                _parse_timestamp(predicate.get("expiresAt")) is not None,
            )
        )
        if not valid:
            raise ApprovalError("approval signature or subjects do not match current evidence")
    except (ApprovalError, OSError):
        return ApprovalVerification(run_id, "APPROVAL-INVALID", decision_value, None, prior_approvals=prior_approvals)

    instant = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    sod = evaluate_sod(
        target=target,
        statement=statement,
        run_meta=run_meta,
        receipts=signed_receipts,
        events=report.events,
        now=instant,
        recorded_producer_keyids=recorded_producer_keyids,
        context=context,
    )
    expires_at = _parse_timestamp(predicate.get("expiresAt"))
    stale = comparison_tree != approved_tree or bool(missing_subjects)
    detail = "APPROVAL-STALE" if stale else None
    if any(check["status"] == "failed" and check["id"] != "approval-not-expired" for check in sod["checks"]):
        status = "SOD-VIOLATION"
    elif decision == "allow" and expires_at is not None and expires_at <= instant:
        status = "APPROVAL-EXPIRED"
    elif decision == "allow" and stale:
        status = "APPROVAL-STALE"
    elif decision == "allow":
        status = "APPROVED"
    elif decision == "deny":
        status = "DENIED"
    elif decision == "hold":
        status = "HELD"
    else:
        status = "APPROVAL-INVALID"
    return ApprovalVerification(
        run_id,
        status,
        decision_value,
        sod,
        live_tree=live_tree,
        prior_approvals=prior_approvals,
        detail=detail,
    )


def verify_approvals(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    runs_root = target / ".brigade" / "runs"
    context = _verification_context(target)
    results = (
        [
            verify_run_approval(target, run_dir, context=context).to_dict()
            for run_dir in sorted(runs_root.iterdir())
            if run_dir.is_dir() and not run_dir.is_symlink() and (run_dir / "run.json").is_file()
        ]
        if runs_root.is_dir()
        else []
    )
    counts = {status: 0 for status in sorted(APPROVAL_STATUSES)}
    for item in results:
        counts[str(item["status"])] += 1
    return {"runs": results, "summary": counts}


def verify_receipts_with_approvals(*, target: Path, json_output: bool = False) -> int:
    """Run existing receipt verification and add signed approval results."""
    from . import receipts_cmd

    target = target.expanduser().resolve()
    approvals = verify_approvals(target)
    approval_failed = any(item["status"] in {"SOD-VIOLATION", "APPROVAL-INVALID"} for item in approvals["runs"])
    if json_output:
        payload = receipts_cmd.verify_payload(target)
        payload["approvals"] = approvals
        summary = payload["summary"]
        receipts_failed = int(summary["mismatch"]) + int(summary["missing"])
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 1 if receipts_failed or approval_failed else 0

    receipts_rc = receipts_cmd.verify(target=target, json_output=False)
    print("approvals:")
    for item in approvals["runs"]:
        decision = item.get("decision")
        status = str(item["status"])
        decision_note = f" ({decision})" if status == "APPROVAL-STALE" and isinstance(decision, str) else ""
        detail = f" [{item['detail']}]" if isinstance(item.get("detail"), str) and status != "APPROVAL-STALE" else ""
        print(f"- {status}{decision_note} {item['run_id']}{detail}")
        for prior in item.get("prior_approvals", []):
            if isinstance(prior, Mapping):
                print(f"  prior: {prior.get('decision')} {prior.get('principal')} {prior.get('decided_at')}")
    return 1 if receipts_rc != 0 or approval_failed else 0


def audit_projection(run_meta: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return the approval decision and recorded SoD block for run audit reports."""
    approval = run_meta.get("approval")
    if not isinstance(approval, Mapping):
        return None
    decision = approval.get("decision")
    sod = approval.get("sod")
    if not isinstance(decision, str) or not isinstance(sod, Mapping):
        return None
    return {"decision": decision, "sod": dict(sod)}


def print_run_projection(run_dir: Path) -> None:
    """Print the approval summary projected into run.json, when present."""
    run_meta = localio.read_json_dict(run_dir / "run.json")
    approval = run_meta.get("approval") if run_meta is not None else None
    if not isinstance(approval, Mapping):
        return
    decision = approval.get("decision")
    sod = approval.get("sod")
    if isinstance(decision, str):
        print(f"approval: {decision}")
    if isinstance(sod, Mapping) and isinstance(sod.get("result"), str):
        print(f"approval sod: {sod['result']}")
