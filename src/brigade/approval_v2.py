"""Portable Test Result and requester bindings for human approval v2."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import agent_request, attestation, localio, run_journal

HUMAN_APPROVAL_PREDICATE_TYPE = "https://brigade.dev/attestation/human-approval/v2"
SOD_POLICY_NAME = "brigade.sod.v2"
SOD_POLICY_TEXT = """brigade.sod.v2
approver.kind must be human.
approver keyid must differ from every verified Test Result producer key.
requester identity must be verified from the signed request evidence.
approver principal and keyid must differ from the verified requester.
approver keyid must differ from the workspace attestation key when present.
approval must precede every merge or ship stage event.
an allow decision must be unexpired at verification time.
"""
SOD_POLICY_SHA256 = hashlib.sha256(SOD_POLICY_TEXT.encode("utf-8")).hexdigest()

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_HEX32_RE = re.compile(r"^[0-9a-f]{32}$")
_HEX40_OR_64_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_KEY_ID_RE = re.compile(r"^[A-Za-z0-9:+/=._-]{1,200}$")


class ApprovalV2Error(RuntimeError):
    """Approval v2 evidence is absent, malformed, or inconsistent."""


@dataclass(frozen=True)
class TestResultEvidence:
    verify_run_id: str
    payload_sha256: str
    envelope_sha256: str
    profile: str
    signer_keyid: str
    producer_keyids: tuple[str, ...]

    def descriptor(self) -> dict[str, Any]:
        return {
            "verifyRunId": self.verify_run_id,
            "payloadSha256": self.payload_sha256,
            "envelopeSha256": self.envelope_sha256,
            "profile": self.profile,
            "signerKeyid": self.signer_keyid,
            "producerKeyids": list(self.producer_keyids),
        }


@dataclass(frozen=True)
class ApprovalEvidence:
    baseline_commit: str | None
    changes_patch_sha256: str | None
    test_results: tuple[TestResultEvidence, ...]

    @property
    def producer_keyids(self) -> frozenset[str]:
        return frozenset(keyid for item in self.test_results for keyid in item.producer_keyids)


@dataclass(frozen=True)
class RequesterIdentity:
    principal: str
    keyid: str
    baseline_commit: str
    statement_sha256: str
    envelope_sha256: str

    def binding(self) -> dict[str, str]:
        return {
            "status": "verified",
            "principal": self.principal,
            "keyid": self.keyid,
            "baselineCommit": self.baseline_commit,
            "statementSha256": self.statement_sha256,
            "envelopeSha256": self.envelope_sha256,
        }


@dataclass(frozen=True)
class SignedBindings:
    tree_fingerprint: str
    evidence: ApprovalEvidence
    requester: RequesterIdentity | None


def _load_json_object(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ApprovalV2Error(f"{label} is missing or not a regular file")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApprovalV2Error(f"{label} is not readable JSON") from exc
    if not isinstance(payload, dict):
        raise ApprovalV2Error(f"{label} must contain a JSON object")
    return payload


def _decode_payload(envelope: Mapping[str, Any], label: str) -> tuple[dict[str, Any], bytes]:
    payload = envelope.get("payload")
    if not isinstance(payload, str):
        raise ApprovalV2Error(f"{label} has no payload")
    try:
        payload_bytes = base64.b64decode(payload, validate=True)
        statement = json.loads(payload_bytes.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApprovalV2Error(f"{label} payload is invalid") from exc
    if not isinstance(statement, dict) or attestation.canonical_statement_bytes(statement) != payload_bytes:
        raise ApprovalV2Error(f"{label} payload is not canonical JSON")
    return statement, payload_bytes


def _safe_evidence_file(
    run_dir: Path,
    relative_path: str,
    expected_path: str,
    label: str,
) -> Path:
    if relative_path != expected_path:
        raise ApprovalV2Error(f"{label} path is invalid")
    candidate = run_dir / relative_path
    try:
        resolved_run_dir = run_dir.resolve()
        resolved = candidate.resolve()
    except (OSError, RuntimeError) as exc:
        raise ApprovalV2Error(f"{label} path is invalid") from exc
    if (
        resolved.parent.parent != resolved_run_dir
        or candidate.is_symlink()
        or candidate.parent.is_symlink()
        or not resolved.is_file()
    ):
        raise ApprovalV2Error(f"{label} is missing or not a regular file")
    return resolved


def verify_recorded_request(
    target: Path,
    run_dir: Path,
    events: Sequence[run_journal.RunEvent],
) -> RequesterIdentity | None:
    """Reverify the sole request.signed event and return its bound identity."""
    request_events = [event for event in events if event.event_type == "request.signed"]
    if not request_events:
        return None
    if len(request_events) != 1:
        raise ApprovalV2Error("signed request evidence is ambiguous")
    event = request_events[0]
    payload = event.payload
    nonce = payload.get("nonce")
    relative_path = payload.get("attestation_path")
    if not isinstance(nonce, str) or not _HEX32_RE.fullmatch(nonce) or not isinstance(relative_path, str):
        raise ApprovalV2Error("signed request event is invalid")
    path = _safe_evidence_file(run_dir, relative_path, f"requests/{nonce}.json", "signed request")
    envelope = _load_json_object(path, "signed request")
    statement, statement_bytes = _decode_payload(envelope, "signed request")
    principal = payload.get("requester_principal")
    keyid = payload.get("requester_keyid")
    baseline_commit = payload.get("baseline_commit")
    statement_sha256 = payload.get("statement_sha256")
    if (
        not isinstance(principal, str)
        or not principal
        or not isinstance(keyid, str)
        or not _KEY_ID_RE.fullmatch(keyid)
        or not isinstance(baseline_commit, str)
        or not _HEX40_OR_64_RE.fullmatch(baseline_commit)
        or not isinstance(statement_sha256, str)
        or not _HEX64_RE.fullmatch(statement_sha256)
    ):
        raise ApprovalV2Error("signed request event identity is invalid")
    signature = attestation.verify_attestation(
        envelope,
        allowed_signers_path=attestation.default_allowed_signers_path(target),
        principal=principal,
        target=target,
        expected_predicate_type=agent_request.AGENT_REQUEST_PREDICATE_TYPE,
    )
    predicate = statement.get("predicate")
    expected_subject = [{"name": "git:baseline", "digest": {"gitCommit": baseline_commit}}]
    valid = all(
        (
            signature.status == attestation.STATUS_SIGNED_OK,
            signature.principal == principal,
            signature.keyid == keyid,
            statement.get("_type") == attestation.IN_TOTO_STATEMENT_TYPE,
            statement.get("predicateType") == agent_request.AGENT_REQUEST_PREDICATE_TYPE,
            statement.get("subject") == expected_subject,
            isinstance(predicate, Mapping),
            isinstance(predicate, Mapping) and predicate.get("schemaVersion") == 1,
            isinstance(predicate, Mapping) and predicate.get("run") == {"id": run_dir.name},
            isinstance(predicate, Mapping) and predicate.get("taskSha256") == payload.get("task_sha256"),
            isinstance(predicate, Mapping) and _parse_timestamp(predicate.get("requestedAt")) is not None,
            isinstance(predicate, Mapping) and predicate.get("nonce") == nonce,
            hashlib.sha256(statement_bytes).hexdigest() == statement_sha256,
        )
    )
    if not valid:
        raise ApprovalV2Error("signed request evidence did not reverify")
    return RequesterIdentity(
        principal=principal,
        keyid=keyid,
        baseline_commit=baseline_commit,
        statement_sha256=statement_sha256,
        envelope_sha256=localio.canonical_json_digest(envelope),
    )


def _matching_receipt_paths(target: Path) -> list[Path]:
    root = target / ".brigade" / "work" / "verify-runs"
    if not root.exists():
        return []
    if root.is_symlink() or not root.is_dir():
        raise ApprovalV2Error("verify receipt root is not a regular directory")
    paths: list[Path] = []
    for child in sorted(root.iterdir()):
        if child.is_symlink():
            raise ApprovalV2Error("verify receipt directory must not be a symlink")
        if child.is_dir() and (child / "receipt.json").exists():
            paths.append(child / "receipt.json")
    return paths


def collect_test_result_evidence(
    target: Path,
    producer_run_id: str,
    final_tree: str,
) -> ApprovalEvidence:
    """Require every matching receipt to have an exact re-derived PASSED envelope."""
    if not _HEX40_OR_64_RE.fullmatch(final_tree):
        raise ApprovalV2Error("run final tree fingerprint is invalid")
    results: list[TestResultEvidence] = []
    baselines: set[str] = set()
    patch_digests: set[str] = set()
    for receipt_path in _matching_receipt_paths(target):
        receipt = _load_json_object(receipt_path, "verify receipt")
        if receipt.get("producer_run_id") != producer_run_id:
            continue
        verify_run_id = receipt.get("run_id")
        baseline_commit = receipt.get("baseline_commit")
        tree_fingerprint = receipt.get("tree_fingerprint")
        patch_sha256 = receipt.get("changes_patch_sha256")
        digests = receipt.get("digests")
        receipt_sha256 = digests.get("receipt_sha256") if isinstance(digests, Mapping) else None
        patch_path = receipt_path.parent / "changes.patch"
        if (
            not isinstance(verify_run_id, str)
            or not _RUN_ID_RE.fullmatch(verify_run_id)
            or verify_run_id != receipt_path.parent.name
            or not isinstance(baseline_commit, str)
            or not _HEX40_OR_64_RE.fullmatch(baseline_commit)
            or tree_fingerprint != final_tree
            or not isinstance(patch_sha256, str)
            or not _HEX64_RE.fullmatch(patch_sha256)
            or not isinstance(receipt_sha256, str)
            or not _HEX64_RE.fullmatch(receipt_sha256)
            or receipt_sha256 != localio.canonical_json_digest(receipt, exclude_keys={"digests"})
            or patch_path.is_symlink()
            or not patch_path.is_file()
            or localio.file_sha256(patch_path) != patch_sha256
        ):
            raise ApprovalV2Error("matching verify receipt identity or digest is invalid")
        attestation_path = receipt_path.parent / "attestation.json"
        envelope = _load_json_object(attestation_path, "Test Result envelope")
        statement, payload_bytes = _decode_payload(envelope, "Test Result envelope")
        verified = attestation.verify_attestation(
            envelope,
            target=target,
            expected_predicate_type=attestation.IN_TOTO_TEST_RESULT_PREDICATE_TYPE,
            require_receipt=True,
        )
        predicate = statement.get("predicate")
        brigade_meta = envelope.get("brigade")
        signer_keyid = verified.keyid
        if (
            verified.status != attestation.STATUS_SIGNED_OK
            or verified.rederived is not True
            or verified.run_id != verify_run_id
            or not isinstance(signer_keyid, str)
            or not _KEY_ID_RE.fullmatch(signer_keyid)
            or not isinstance(predicate, Mapping)
            or predicate.get("result") != "PASSED"
            or not isinstance(brigade_meta, Mapping)
            or brigade_meta.get("profile") != attestation.ATTESTATION_PROFILE
        ):
            raise ApprovalV2Error(
                f"matching verify receipt {verify_run_id} requires a re-derived SIGNED-OK PASSED Test Result"
            )
        producer_keyids = {signer_keyid}
        receipt_keyid = digests.get("key_id") if isinstance(digests, Mapping) else None
        if isinstance(receipt_keyid, str) and receipt_keyid:
            if not _KEY_ID_RE.fullmatch(receipt_keyid):
                raise ApprovalV2Error("matching verify receipt producer key id is invalid")
            producer_keyids.add(receipt_keyid)
        results.append(
            TestResultEvidence(
                verify_run_id=verify_run_id,
                payload_sha256=hashlib.sha256(payload_bytes).hexdigest(),
                envelope_sha256=localio.canonical_json_digest(envelope),
                profile=attestation.ATTESTATION_PROFILE,
                signer_keyid=signer_keyid,
                producer_keyids=tuple(sorted(producer_keyids)),
            )
        )
        baselines.add(baseline_commit)
        patch_digests.add(patch_sha256)
    if not results:
        raise ApprovalV2Error("allow requires at least one re-derived SIGNED-OK Test Result envelope")
    if len(baselines) != 1:
        raise ApprovalV2Error("matching verify receipts do not agree on baseline commit")
    if len(patch_digests) != 1:
        raise ApprovalV2Error("matching verify receipts do not agree on patch digest")
    ordered = tuple(sorted(results, key=lambda item: (item.payload_sha256, item.verify_run_id)))
    if len({item.payload_sha256 for item in ordered}) != len(ordered):
        raise ApprovalV2Error("matching verify receipts do not have unique Test Result payloads")
    return ApprovalEvidence(next(iter(baselines)), next(iter(patch_digests)), ordered)


def empty_evidence() -> ApprovalEvidence:
    return ApprovalEvidence(None, None, ())


def _subjects(tree_fingerprint: str, evidence: ApprovalEvidence) -> list[dict[str, Any]]:
    subjects: list[dict[str, Any]] = [{"name": "git:tree", "digest": {"gitTree": tree_fingerprint}}]
    if evidence.changes_patch_sha256 is not None:
        subjects.append({"name": "changes.patch", "digest": {"sha256": evidence.changes_patch_sha256}})
    subjects.extend(
        {
            "name": f"test-result:{item.verify_run_id}",
            "digest": {"sha256": item.payload_sha256},
        }
        for item in evidence.test_results
    )
    return subjects


def _evidence_predicate(evidence: ApprovalEvidence) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "testResultPayloadSha256": [item.payload_sha256 for item in evidence.test_results],
        "envelopes": [item.descriptor() for item in evidence.test_results],
    }
    if evidence.test_results:
        payload["baselineCommit"] = evidence.baseline_commit
        payload["changesPatchSha256"] = evidence.changes_patch_sha256
    return payload


def build_statement(
    *,
    run_id: str,
    journal_chain_head: str,
    tree_fingerprint: str,
    evidence: ApprovalEvidence,
    requester: RequesterIdentity | None,
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
    """Build the canonical human-approval/v2 statement."""
    if not _HEX40_OR_64_RE.fullmatch(tree_fingerprint):
        raise ApprovalV2Error("run final tree fingerprint is invalid")
    return {
        "_type": attestation.IN_TOTO_STATEMENT_TYPE,
        "subject": _subjects(tree_fingerprint, evidence),
        "predicateType": HUMAN_APPROVAL_PREDICATE_TYPE,
        "predicate": {
            "schemaVersion": 2,
            "run": {"id": run_id, "journalChainHead": {"sha256": journal_chain_head}},
            "decision": decision,
            "scope": scope,
            "approver": {"principal": principal, "keyid": keyid, "kind": "human"},
            "requester": requester.binding() if requester is not None else {"status": "unknown"},
            "decidedAt": decided_at,
            "expiresAt": expires_at,
            "nonce": nonce,
            "reasonCode": reason_code,
            "reasonSha256": hashlib.sha256(reason.encode("utf-8")).hexdigest(),
            "evidence": _evidence_predicate(evidence),
            "policy": {"name": SOD_POLICY_NAME, "digest": {"sha256": SOD_POLICY_SHA256}},
        },
    }


def _parse_requester(value: object) -> RequesterIdentity | None:
    if value == {"status": "unknown"}:
        return None
    if not isinstance(value, Mapping) or set(value) != {
        "status",
        "principal",
        "keyid",
        "baselineCommit",
        "statementSha256",
        "envelopeSha256",
    }:
        raise ApprovalV2Error("approval requester binding is invalid")
    if value.get("status") != "verified":
        raise ApprovalV2Error("approval requester binding is invalid")
    principal = value.get("principal")
    keyid = value.get("keyid")
    baseline_commit = value.get("baselineCommit")
    statement_sha256 = value.get("statementSha256")
    envelope_sha256 = value.get("envelopeSha256")
    if (
        not isinstance(principal, str)
        or not principal
        or not isinstance(keyid, str)
        or not _KEY_ID_RE.fullmatch(keyid)
        or not isinstance(baseline_commit, str)
        or not _HEX40_OR_64_RE.fullmatch(baseline_commit)
        or not isinstance(statement_sha256, str)
        or not _HEX64_RE.fullmatch(statement_sha256)
        or not isinstance(envelope_sha256, str)
        or not _HEX64_RE.fullmatch(envelope_sha256)
    ):
        raise ApprovalV2Error("approval requester binding is invalid")
    return RequesterIdentity(principal, keyid, baseline_commit, statement_sha256, envelope_sha256)


def parse_signed_bindings(statement: Mapping[str, Any]) -> SignedBindings:
    """Validate v2's closed binding shapes and reproduce their subjects."""
    predicate = statement.get("predicate")
    subjects = statement.get("subject")
    if not isinstance(predicate, Mapping) or not isinstance(subjects, list):
        raise ApprovalV2Error("approval v2 statement is invalid")
    if not subjects or not isinstance(subjects[0], Mapping):
        raise ApprovalV2Error("approval v2 subjects are invalid")
    tree_digest = subjects[0].get("digest")
    tree = tree_digest.get("gitTree") if isinstance(tree_digest, Mapping) else None
    if subjects[0].get("name") != "git:tree" or not isinstance(tree, str) or not _HEX40_OR_64_RE.fullmatch(tree):
        raise ApprovalV2Error("approval v2 tree subject is invalid")
    evidence_raw = predicate.get("evidence")
    if not isinstance(evidence_raw, Mapping):
        raise ApprovalV2Error("approval v2 evidence binding is invalid")
    payload_digests = evidence_raw.get("testResultPayloadSha256")
    envelopes = evidence_raw.get("envelopes")
    if not isinstance(payload_digests, list) or not isinstance(envelopes, list):
        raise ApprovalV2Error("approval v2 evidence binding is invalid")
    results: list[TestResultEvidence] = []
    for raw in envelopes:
        if not isinstance(raw, Mapping) or set(raw) != {
            "verifyRunId",
            "payloadSha256",
            "envelopeSha256",
            "profile",
            "signerKeyid",
            "producerKeyids",
        }:
            raise ApprovalV2Error("approval v2 envelope binding is invalid")
        verify_run_id = raw.get("verifyRunId")
        payload_sha256 = raw.get("payloadSha256")
        envelope_sha256 = raw.get("envelopeSha256")
        profile = raw.get("profile")
        signer_keyid = raw.get("signerKeyid")
        producer_keyids = raw.get("producerKeyids")
        if not isinstance(producer_keyids, list) or any(
            not isinstance(item, str) or not _KEY_ID_RE.fullmatch(item) for item in producer_keyids
        ):
            raise ApprovalV2Error("approval v2 envelope binding is invalid")
        producer_keyid_values = tuple(str(item) for item in producer_keyids)
        if (
            not isinstance(verify_run_id, str)
            or not _RUN_ID_RE.fullmatch(verify_run_id)
            or not isinstance(payload_sha256, str)
            or not _HEX64_RE.fullmatch(payload_sha256)
            or not isinstance(envelope_sha256, str)
            or not _HEX64_RE.fullmatch(envelope_sha256)
            or profile != attestation.ATTESTATION_PROFILE
            or not isinstance(signer_keyid, str)
            or not _KEY_ID_RE.fullmatch(signer_keyid)
            or producer_keyids != sorted(set(producer_keyid_values))
            or signer_keyid not in producer_keyid_values
        ):
            raise ApprovalV2Error("approval v2 envelope binding is invalid")
        results.append(
            TestResultEvidence(
                verify_run_id,
                payload_sha256,
                envelope_sha256,
                attestation.ATTESTATION_PROFILE,
                signer_keyid,
                producer_keyid_values,
            )
        )
    ordered = tuple(sorted(results, key=lambda item: (item.payload_sha256, item.verify_run_id)))
    if tuple(results) != ordered or payload_digests != [item.payload_sha256 for item in ordered]:
        raise ApprovalV2Error("approval v2 Test Result payload digest set is not canonical")
    if len(set(payload_digests)) != len(payload_digests):
        raise ApprovalV2Error("approval v2 Test Result payload digest set contains duplicates")
    baseline = evidence_raw.get("baselineCommit")
    patch = evidence_raw.get("changesPatchSha256")
    if ordered:
        if (
            set(evidence_raw)
            != {
                "baselineCommit",
                "changesPatchSha256",
                "testResultPayloadSha256",
                "envelopes",
            }
            or not isinstance(baseline, str)
            or not _HEX40_OR_64_RE.fullmatch(baseline)
            or not isinstance(patch, str)
            or not _HEX64_RE.fullmatch(patch)
        ):
            raise ApprovalV2Error("approval v2 receipt identity binding is invalid")
        evidence = ApprovalEvidence(baseline, patch, ordered)
    else:
        if set(evidence_raw) != {"testResultPayloadSha256", "envelopes"}:
            raise ApprovalV2Error("approval v2 empty evidence binding is invalid")
        evidence = empty_evidence()
    if subjects != _subjects(tree, evidence):
        raise ApprovalV2Error("approval v2 subjects do not match its evidence binding")
    return SignedBindings(tree, evidence, _parse_requester(predicate.get("requester")))


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
    statement: Mapping[str, Any],
    evidence: ApprovalEvidence,
    requester: RequesterIdentity | None,
    events: Sequence[run_journal.RunEvent],
    workspace_keyid: str | None,
    now: datetime,
) -> dict[str, Any]:
    """Evaluate brigade.sod.v2 using only verified, statement-bound identities."""
    predicate = statement.get("predicate")
    predicate = predicate if isinstance(predicate, Mapping) else {}
    approver = predicate.get("approver")
    approver = approver if isinstance(approver, Mapping) else {}
    principal = approver.get("principal")
    keyid = approver.get("keyid")
    decision = predicate.get("decision")
    decided_at = _parse_timestamp(predicate.get("decidedAt"))
    expires_at = _parse_timestamp(predicate.get("expiresAt"))
    checks: list[dict[str, str]] = [
        {
            "id": "approver-is-human",
            "status": "passed" if approver.get("kind") == "human" else "failed",
            "detail": "declared",
        },
        {
            "id": "approver-not-producer",
            "status": "passed" if isinstance(keyid, str) and keyid not in evidence.producer_keyids else "failed",
        },
    ]
    if requester is None:
        checks.append(
            {
                "id": "approver-not-requester",
                "status": "indeterminate",
                "detail": "requester identity unavailable",
            }
        )
    else:
        checks.append(
            {
                "id": "approver-not-requester",
                "status": "passed" if principal != requester.principal and keyid != requester.keyid else "failed",
            }
        )
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
    statuses = {check["status"] for check in checks}
    if "failed" in statuses:
        result = "FAILED"
    elif "indeterminate" in statuses:
        result = "INDETERMINATE"
    else:
        result = "PASSED"
    return {"result": result, "checks": checks}
