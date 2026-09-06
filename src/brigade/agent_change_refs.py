"""Reference-building helpers for the agent-change evidence index emitter."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import (
    attestation,
    attestation_input,
    attestation_receipt,
    localio,
)

_VERIFY_RUN_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_HEX40_OR_64_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


class AgentChangeError(RuntimeError):
    """Agent-change index construction or export failed."""


def _load_json_object(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise AgentChangeError(f"{label} is missing or not a regular file")
    try:
        return attestation_input.read_json_object(path)
    except (OSError, attestation_input.AttestationInputError) as exc:
        raise AgentChangeError(f"{label} is not readable JSON") from exc


def _decode_envelope_payload(
    envelope: Mapping[str, Any], label: str
) -> tuple[dict[str, Any] | None, bytes | None, str | None]:
    payload_b64 = envelope.get("payload")
    if not isinstance(payload_b64, str):
        return None, None, "malformed-payload"
    try:
        payload_bytes = attestation_input.decode_dsse_base64(
            payload_b64,
            label=f"{label} payload",
            max_bytes=attestation_input.MAX_PAYLOAD_BYTES,
        )
    except attestation_input.AttestationInputError:
        return None, None, "malformed-payload"
    try:
        statement = attestation_input.strict_json_loads(
            payload_bytes,
            max_bytes=attestation_input.MAX_PAYLOAD_BYTES,
        )
    except attestation_input.AttestationInputError:
        return None, None, "malformed-payload"
    if not isinstance(statement, dict):
        return None, None, "malformed-payload"
    return statement, payload_bytes, None


def _extract_tree(statement: Mapping[str, Any]) -> str | None:
    subjects = statement.get("subject")
    if not isinstance(subjects, list):
        return None
    for subject in subjects:
        if not isinstance(subject, dict):
            continue
        if subject.get("name") == "git:tree":
            digest = subject.get("digest")
            if isinstance(digest, dict):
                value = digest.get("gitTree")
                if isinstance(value, str) and _HEX40_OR_64_RE.fullmatch(value):
                    return value
    return None


def _extract_baseline(statement: Mapping[str, Any]) -> str | None:
    subjects = statement.get("subject")
    if not isinstance(subjects, list):
        return None
    for subject in subjects:
        if not isinstance(subject, dict):
            continue
        if subject.get("name") == "git:baseline":
            digest = subject.get("digest")
            if isinstance(digest, dict):
                value = digest.get("gitCommit")
                if isinstance(value, str) and _HEX40_OR_64_RE.fullmatch(value):
                    return value
    return None


def _predicate_version(statement: Mapping[str, Any] | None, predicate_type: str) -> str | None:
    if statement is None:
        return None
    if predicate_type == attestation.IN_TOTO_TEST_RESULT_PREDICATE_TYPE:
        parts = predicate_type.rsplit("/", 1)
        return parts[-1] if len(parts) == 2 else None
    predicate = statement.get("predicate")
    if isinstance(predicate, dict):
        version = predicate.get("schemaVersion")
        if isinstance(version, int) and not isinstance(version, bool):
            return str(version)
    return None


def _verify_reference_envelope(
    envelope: Mapping[str, Any],
    target: Path,
    expected_predicate_type: str,
    label: str,
    require_receipt: bool = False,
    receipt: Mapping[str, Any] | None = None,
) -> tuple[attestation.AttestationVerifyResult, dict[str, Any] | None, bytes | None]:
    statement, payload_bytes, _ = _decode_envelope_payload(envelope, label)
    if statement is None or payload_bytes is None:
        return (
            attestation.AttestationVerifyResult(status=attestation.STATUS_UNVERIFIABLE_SIGNATURE),
            None,
            None,
        )
    if statement.get("predicateType") != expected_predicate_type:
        return (
            attestation.AttestationVerifyResult(status=attestation.STATUS_UNVERIFIABLE_SIGNATURE),
            statement,
            payload_bytes,
        )
    result = attestation.verify_attestation(
        envelope,
        allowed_signers_path=attestation.default_allowed_signers_path(target),
        target=target,
        expected_predicate_type=expected_predicate_type,
        require_receipt=require_receipt,
        receipt=receipt,
    )
    return result, statement, payload_bytes


def _build_test_result_references(
    target: Path,
    run_id: str,
    final_tree: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    references: list[dict[str, Any]] = []
    other_tree: list[dict[str, Any]] = []
    baselines: set[str] = set()
    patches: set[str] = set()
    root = target / ".brigade" / "work" / "verify-runs"
    if not root.is_dir() or root.is_symlink():
        return [], [], {"status": "unknown"}
    entries: list[Path] = []
    count = 0
    for child in root.iterdir():
        count += 1
        if count > attestation_receipt.MAX_RECEIPT_DIRECTORY_ENTRIES:
            raise AgentChangeError("verify receipt directory scan exceeds entry limit")
        if child.is_symlink():
            raise AgentChangeError("verify receipt directory must not be a symlink")
        if not child.is_dir():
            continue
        name = child.name
        if not _VERIFY_RUN_ID_RE.fullmatch(name) or name in {".", ".."}:
            continue
        entries.append(child)
    entries.sort(key=lambda p: p.name)

    for verify_dir in entries:
        receipt_path = verify_dir / "receipt.json"
        attestation_path = verify_dir / "attestation.json"
        if not receipt_path.is_file() or receipt_path.is_symlink():
            continue
        try:
            receipt = attestation_input.read_json_object(receipt_path)
        except (OSError, attestation_input.AttestationInputError):
            continue
        if receipt.get("producer_run_id") != run_id:
            continue
        try:
            snapshot = attestation_receipt.snapshot_stored_receipt(receipt)
        except attestation_receipt.ReceiptDigestError:
            references.append(
                {
                    "kind": "test-result",
                    "predicateType": attestation.IN_TOTO_TEST_RESULT_PREDICATE_TYPE,
                    "predicateVersion": "v0.1",
                    "profile": attestation.ATTESTATION_PROFILE,
                    "subjectTree": None,
                    "payloadSha256": None,
                    "envelopeSha256": None,
                    "signerKeyids": [],
                    "verified": False,
                    "locator": f".brigade/work/verify-runs/{verify_dir.name}/attestation.json",
                    "reason": "receipt-digest-invalid",
                }
            )
            continue
        receipt = snapshot.receipt
        tree = receipt.get("tree_fingerprint")
        if tree != final_tree:
            other_tree.append(
                {
                    "verifyRunId": verify_dir.name,
                    "tree": tree if isinstance(tree, str) else None,
                }
            )
            continue
        baseline = receipt.get("baseline_commit")
        patch = receipt.get("changes_patch_sha256")
        if isinstance(baseline, str) and _HEX40_OR_64_RE.fullmatch(baseline):
            baselines.add(baseline)
        if isinstance(patch, str) and _HEX64_RE.fullmatch(patch):
            patches.add(patch)

        if not attestation_path.is_file() or attestation_path.is_symlink():
            references.append(
                {
                    "kind": "test-result",
                    "predicateType": attestation.IN_TOTO_TEST_RESULT_PREDICATE_TYPE,
                    "predicateVersion": "v0.1",
                    "profile": attestation.ATTESTATION_PROFILE,
                    "subjectTree": tree,
                    "payloadSha256": None,
                    "envelopeSha256": None,
                    "signerKeyids": [],
                    "verified": False,
                    "locator": f".brigade/work/verify-runs/{verify_dir.name}/attestation.json",
                    "reason": "attestation envelope missing",
                }
            )
            continue
        try:
            envelope = attestation_input.read_json_object(attestation_path)
        except (OSError, attestation_input.AttestationInputError):
            references.append(
                {
                    "kind": "test-result",
                    "predicateType": attestation.IN_TOTO_TEST_RESULT_PREDICATE_TYPE,
                    "predicateVersion": "v0.1",
                    "profile": attestation.ATTESTATION_PROFILE,
                    "subjectTree": tree,
                    "payloadSha256": None,
                    "envelopeSha256": None,
                    "signerKeyids": [],
                    "verified": False,
                    "locator": f".brigade/work/verify-runs/{verify_dir.name}/attestation.json",
                    "reason": "attestation envelope is not readable JSON",
                }
            )
            continue

        result, statement, payload_bytes = _verify_reference_envelope(
            envelope,
            target,
            attestation.IN_TOTO_TEST_RESULT_PREDICATE_TYPE,
            "Test Result",
            require_receipt=True,
            receipt=snapshot.receipt,
        )
        verified = result.status == attestation.STATUS_SIGNED_OK
        subject_tree = _extract_tree(statement) if statement is not None else None
        predicate = statement.get("predicate") if statement is not None else None
        result_value = None
        if isinstance(predicate, dict):
            result_value = predicate.get("result")
        keyids = sorted({result.keyid}) if verified and isinstance(result.keyid, str) else []
        reference: dict[str, Any] = {
            "kind": "test-result",
            "predicateType": attestation.IN_TOTO_TEST_RESULT_PREDICATE_TYPE,
            "predicateVersion": "v0.1",
            "profile": attestation.ATTESTATION_PROFILE,
            "subjectTree": subject_tree,
            "payloadSha256": hashlib.sha256(payload_bytes).hexdigest() if payload_bytes is not None else None,
            "envelopeSha256": localio.canonical_json_digest(envelope),
            "signerKeyids": keyids,
            "verified": verified,
            "locator": f".brigade/work/verify-runs/{verify_dir.name}/attestation.json",
            "rederived": result.rederived if verified else False,
            "result": result_value,
        }
        if not verified:
            reason = result.status.lower().replace("_", "-")
            if result.status == attestation.STATUS_SUBJECT_MISMATCH:
                reason = "rederivation-failed"
            reference["reason"] = reason
        references.append(reference)

    baseline_out: dict[str, Any]
    patch_out: dict[str, Any]
    if len(baselines) == 1:
        baseline_out = {"gitCommit": baselines.pop()}
    elif baselines:
        baseline_out = {"status": "conflicted"}
    else:
        baseline_out = {"status": "unknown"}
    if len(patches) == 1:
        patch_out = {"sha256": patches.pop()}
    elif patches:
        patch_out = {"status": "conflicted"}
    else:
        patch_out = {"status": "unknown"}
    return references, other_tree, {"baseline": baseline_out, "patch": patch_out}
