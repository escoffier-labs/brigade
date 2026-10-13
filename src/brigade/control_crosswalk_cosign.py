"""Cosign verification adapter for signed verify-run results."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Any

from . import attestation, attestation_input, attestation_receipt, control_readiness, cosign_attestation, receipt_schema


def _observation(
    relpath: str,
    outcome: str,
    *,
    integrity: Any = "unknown",
    signature: Any = "unknown",
    authorization: Any = "unknown",
    subject: Any = "unknown",
    reason: str | None = None,
    source_digest: str | None = None,
) -> control_readiness.ArtifactObservation:
    reasons = [reason] if reason else []
    return control_readiness.ArtifactObservation(
        relpath,
        "claim_validated",
        outcome,
        control_readiness.dims(
            integrity=integrity,
            signature=signature,
            authorization=authorization,
            subject=subject,
            population="passed",
        ),
        reasons,
        source_digest_sha256=source_digest,
    )


def _unavailable(relpath: str, reason: str) -> control_readiness.ArtifactObservation:
    return _observation(
        relpath,
        "unavailable",
        integrity=("unavailable", reason),
        signature=("unavailable", reason),
        authorization=("unavailable", reason),
        subject="unknown",
        reason=reason,
    )


def verify_cosign_bundle(_target: Path, bundle_path: Path) -> control_readiness.ArtifactObservation:
    """Verify a Test Result bundle against its receipt and configured public key."""
    relpath = bundle_path.name
    receipt_path = bundle_path.parent / "receipt.json"
    try:
        receipt = attestation_input.read_json_object(receipt_path)
    except (OSError, attestation_input.AttestationInputError):
        return control_readiness.ArtifactObservation(
            relpath, "discovered", "invalid", control_readiness.dims(), ["schema_missing"]
        )
    try:
        snapshot = attestation_receipt.snapshot_receipt(receipt)
    except attestation_input.AttestationInputError:
        return control_readiness.ArtifactObservation(
            relpath, "discovered", "invalid", control_readiness.dims(), ["schema_missing"]
        )
    except attestation_receipt.ReceiptDigestError:
        return _observation(
            relpath,
            "rejected",
            integrity=("failed", "receipt_digest_mismatch"),
            subject="unknown",
            reason="receipt_digest_mismatch",
        )
    if (
        type(snapshot.receipt.get("schema_version")) is not int
        or snapshot.receipt.get("schema_version") != receipt_schema.VERIFY_RECEIPT_SCHEMA_VERSION
    ):
        return control_readiness.ArtifactObservation(
            relpath, "discovered", "invalid", control_readiness.dims(), ["schema_missing"]
        )
    if snapshot.receipt.get("run_id") != bundle_path.parent.name:
        return _observation(
            relpath,
            "rejected",
            integrity="unknown",
            subject=("failed", "run_binding_mismatch"),
            reason="run_binding_mismatch",
            source_digest=hashlib.sha256(bundle_path.read_bytes()).hexdigest(),
        )

    try:
        statement = attestation.build_statement(snapshot)
        bundle = attestation_input.read_json_object(bundle_path)
        cosign_attestation.validate_bundle(bundle, statement)
    except (OSError, attestation_input.AttestationInputError, attestation.AttestationExportError):
        return _observation(
            relpath,
            "rejected",
            integrity=("failed", "signature_mismatch"),
            signature=("failed", "signature_mismatch"),
            authorization="unknown",
            reason="signature_mismatch",
        )
    except cosign_attestation.CosignAttestationError:
        return _observation(
            relpath,
            "rejected",
            integrity=("failed", "signature_mismatch"),
            signature=("failed", "signature_mismatch"),
            authorization="unknown",
            reason="signature_mismatch",
        )

    key_path = _target / ".brigade" / "attestation" / "cosign.pub"
    if not key_path.is_file():
        return _unavailable(relpath, "verifier_tool_unavailable")
    try:
        binary, _version = cosign_attestation.require_safe_cosign()
    except cosign_attestation.CosignAttestationError:
        return _unavailable(relpath, "verifier_tool_unavailable")

    tree_fingerprint = snapshot.receipt.get("tree_fingerprint")
    if not isinstance(tree_fingerprint, str) or not tree_fingerprint:
        return control_readiness.ArtifactObservation(
            relpath, "discovered", "invalid", control_readiness.dims(), ["schema_missing"]
        )
    command = [
        binary,
        "verify-blob-attestation",
        "--bundle",
        str(bundle_path),
        "--key",
        str(key_path),
        "--insecure-ignore-tlog=true",
        f"--type={attestation.IN_TOTO_TEST_RESULT_PREDICATE_TYPE}",
        f"--digest={tree_fingerprint}",
        "--digestAlg=gitTree",
    ]
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return _unavailable(relpath, "verifier_tool_unavailable")
    digest = hashlib.sha256(bundle_path.read_bytes()).hexdigest()
    if result.returncode != 0:
        return _observation(
            relpath,
            "rejected",
            integrity=("failed", "signature_mismatch"),
            signature=("failed", "signature_mismatch"),
            subject="passed",
            reason="signature_mismatch",
            source_digest=digest,
        )

    commands = snapshot.receipt.get("commands")
    planned = snapshot.receipt.get("planned_commands")
    if not isinstance(commands, list) or not commands:
        return _observation(relpath, "incomplete", integrity="passed", reason="empty_commands")
    if not isinstance(planned, list) or not planned or not all(isinstance(item, str) and item for item in planned):
        return _observation(relpath, "incomplete", integrity="passed", reason="planned_commands_missing")
    if len(commands) != len(planned) or any(
        not isinstance(command, dict) or command.get("command") != expected
        for command, expected in zip(commands, planned, strict=False)
    ):
        return _observation(relpath, "incomplete", integrity="passed", reason="planned_commands_mismatch")
    receipt_status = snapshot.receipt.get("status")
    if not isinstance(receipt_status, str) or receipt_status not in {"completed", "failed", "rejected"}:
        return _observation(relpath, "incomplete", integrity="passed", reason="status_not_terminal")
    terminal_command_statuses = {"completed"}
    if receipt_status in {"failed", "rejected"}:
        terminal_command_statuses.update({"failed", "timed_out", "rejected"})
    if any(
        type(command.get("exit_code")) is not int or command.get("status") not in terminal_command_statuses
        for command in commands
    ):
        return _observation(relpath, "incomplete", integrity="passed", reason="command_not_terminal")

    if receipt_status != "completed" or any(command.get("exit_code") != 0 for command in commands):
        reason = (
            "status_rejected"
            if receipt_status == "rejected"
            else ("status_failed" if receipt_status == "failed" else "exit_code_nonzero")
        )
        return _observation(
            relpath,
            "failed",
            integrity="passed",
            signature="passed",
            authorization="passed",
            subject="passed",
            reason=reason,
            source_digest=digest,
        )
    return _observation(
        relpath,
        "validated",
        integrity="passed",
        signature="passed",
        authorization="passed",
        subject="passed",
        source_digest=digest,
    )
