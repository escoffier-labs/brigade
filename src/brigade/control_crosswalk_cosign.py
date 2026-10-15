"""Cosign verification adapter for signed verify-run results."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Any

from . import attestation, attestation_input, attestation_receipt, control_readiness, cosign_attestation, receipt_schema


def binding_candidates(obs: control_readiness.ArtifactObservation, candidates: dict[str, object]) -> dict[str, object]:
    if receipt_claims_untrusted(obs):
        return {}
    return candidates


def receipt_claims_untrusted(obs: control_readiness.ArtifactObservation) -> bool:
    """Return whether receipt fields failed integrity or signed-payload checks."""
    if obs.proposed == "unavailable" and obs.relpath.endswith("attestation.sigstore.json"):
        return True
    if (
        obs.proposed == "invalid"
        and "schema_missing" in obs.reasons
        and obs.relpath.endswith("receipt.json")
        and obs.dimensions.get("integrity", {}).get("status") != "passed"
    ):
        return True
    if (
        obs.proposed == "invalid"
        and "schema_missing" in obs.reasons
        and obs.relpath.endswith("attestation.sigstore.json")
        and obs.dimensions.get("signature", {}).get("status") != "passed"
    ):
        return True
    if obs.proposed == "invalid" and any(
        reason in {"invalid_json", "media_type_mismatch", "dsse_signatures_missing", "symlink_refused"}
        for reason in obs.reasons
    ):
        return True
    return obs.proposed == "rejected" and any(
        reason in {"receipt_digest_mismatch", "signature_mismatch"} for reason in obs.reasons
    )


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
    except attestation_receipt.ReceiptDigestMismatchError:
        return _observation(
            relpath,
            "rejected",
            integrity=("failed", "receipt_digest_mismatch"),
            subject="unknown",
            reason="receipt_digest_mismatch",
        )
    except attestation_receipt.ReceiptDigestError:
        return control_readiness.ArtifactObservation(
            relpath, "discovered", "invalid", control_readiness.dims(), ["schema_missing"]
        )
    try:
        statement = attestation.build_statement(snapshot)
    except attestation.AttestationExportError:
        return control_readiness.ArtifactObservation(
            relpath, "discovered", "invalid", control_readiness.dims(), ["schema_missing"]
        )
    try:
        bundle = attestation_input.read_json_object(bundle_path)
        cosign_attestation.validate_bundle(bundle, statement)
    except (OSError, attestation_input.AttestationInputError):
        return _observation(
            relpath,
            "rejected",
            integrity=("failed", "signature_mismatch"),
            signature=("failed", "signature_mismatch"),
            authorization="unknown",
            reason="signature_mismatch",
        )
    except cosign_attestation.CosignBundlePayloadMismatchError:
        return _observation(
            relpath,
            "rejected",
            integrity=("failed", "signature_mismatch"),
            signature=("failed", "signature_mismatch"),
            authorization="unknown",
            reason="signature_mismatch",
        )
    except cosign_attestation.CosignAttestationError:
        return _observation(relpath, "invalid", reason="schema_missing")

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
            subject="unknown",
            reason="signature_mismatch",
            source_digest=digest,
        )
    if snapshot.receipt.get("run_id") != bundle_path.parent.name:
        return _observation(
            relpath,
            "rejected",
            integrity="passed",
            signature="passed",
            subject=("failed", "run_binding_mismatch"),
            reason="run_binding_mismatch",
            source_digest=digest,
        )
    if (
        type(snapshot.receipt.get("schema_version")) is not int
        or snapshot.receipt.get("schema_version") != receipt_schema.VERIFY_RECEIPT_SCHEMA_VERSION
    ):
        return _observation(
            relpath,
            "invalid",
            integrity="passed",
            signature="passed",
            authorization="unknown",
            subject="passed",
            reason="schema_missing",
            source_digest=digest,
        )

    commands = snapshot.receipt.get("commands")
    planned = snapshot.receipt.get("planned_commands")
    verified_trust = {
        "integrity": "passed",
        "signature": "passed",
        "authorization": "unknown",
        "subject": "passed",
        "source_digest": digest,
    }
    receipt_status = snapshot.receipt.get("status")
    if not isinstance(receipt_status, str) or receipt_status not in {"completed", "failed", "rejected", "canceled"}:
        return _observation(relpath, "incomplete", **verified_trust, reason="status_not_terminal")
    if receipt_status == "canceled":
        return _observation(relpath, "incomplete", **verified_trust, reason="status_canceled")
    if not isinstance(commands, list) or not commands:
        return _observation(relpath, "incomplete", **verified_trust, reason="empty_commands")
    if not isinstance(planned, list) or not planned or not all(isinstance(item, str) and item for item in planned):
        return _observation(relpath, "incomplete", **verified_trust, reason="planned_commands_missing")
    if len(commands) != len(planned) or any(
        not isinstance(command, dict) or command.get("command") != expected
        for command, expected in zip(commands, planned, strict=False)
    ):
        return _observation(relpath, "incomplete", **verified_trust, reason="planned_commands_mismatch")
    terminal_command_statuses = {"completed"}
    if receipt_status in {"failed", "rejected"}:
        terminal_command_statuses.update({"failed", "timed_out", "rejected"})
    if any(
        not isinstance(command.get("status"), str)
        or command.get("status") not in terminal_command_statuses
        or (command.get("exit_code") is None and command.get("status") != "timed_out")
        or (command.get("exit_code") is not None and type(command.get("exit_code")) is not int)
        for command in commands
    ):
        return _observation(relpath, "incomplete", **verified_trust, reason="command_not_terminal")

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
            authorization="unknown",
            subject="passed",
            reason=reason,
            source_digest=digest,
        )
    return _observation(
        relpath,
        "validated",
        integrity="passed",
        signature="passed",
        authorization="unknown",
        subject="passed",
        source_digest=digest,
    )
