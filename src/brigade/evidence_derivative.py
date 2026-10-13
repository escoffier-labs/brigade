"""Local export of minimized derivatives from portable evidence packages."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import attestation_input, attestation_receipt, evidence_package, evidence_redaction, localio

SCHEMA = "brigade.evidence_package_derivative.v1"
SCHEMA_VERSION = 1
POLICY_VERSION = "brigade.evidence-derivative.minimum.v1"
_RECEIPT_FIELDS = frozenset(
    {
        "run_id",
        "producer_run_id",
        "schema_version",
        "status",
        "started_at",
        "finished_at",
        "baseline_commit",
        "tree_fingerprint",
        "changes_patch_sha256",
        "exit_code",
    }
)
_SUPPORTED = frozenset({"manifest.json", *(name for name, _, _ in evidence_package.PACKAGE_FILES)})


class DerivativeExportError(ValueError):
    """The source cannot safely produce a minimized derivative."""


@dataclass(frozen=True)
class DerivativeReport:
    removed_fields: int
    redaction_occurrences: int
    excluded_artifacts: int
    derivative_sha256: str


def _read(path: Path) -> bytes:
    if not path.is_file() or path.is_symlink():
        raise DerivativeExportError(f"source artifact is not a regular file: {path.name}")
    if path.stat().st_size > evidence_package.MAX_ENTRY_BYTES:
        raise DerivativeExportError(f"source artifact exceeds size limit: {path.name}")
    return path.read_bytes()


def _json_object(data: bytes, name: str) -> dict[str, Any]:
    try:
        value = attestation_input.strict_json_loads(data)
    except attestation_input.AttestationInputError as exc:
        raise DerivativeExportError(f"source {name} is malformed") from exc
    if not isinstance(value, dict):
        raise DerivativeExportError(f"source {name} is not an object")
    return value


def _safe_text(value: object) -> tuple[str, int]:
    if not isinstance(value, str):
        raise DerivativeExportError("allowlisted metadata has an invalid type")
    verdict = evidence_redaction.apply_origin_redaction(value, origin="unknown")
    if verdict.status == "error":
        raise DerivativeExportError("metadata redaction failed")
    return verdict.persisted_text, verdict.count


def _minimize_receipt(receipt: dict[str, Any]) -> tuple[dict[str, Any], int, int]:
    kept: dict[str, Any] = {}
    redaction_occurrences = 0
    for key in sorted(_RECEIPT_FIELDS & receipt.keys()):
        value = receipt[key]
        if value is None or isinstance(value, (int, bool)):
            kept[key] = value
        elif isinstance(value, str):
            kept[key], count = _safe_text(value)
            redaction_occurrences += count
        else:
            raise DerivativeExportError(f"allowlisted receipt field has an invalid type: {key}")
    return kept, len(set(receipt) - _RECEIPT_FIELDS), redaction_occurrences


def _validate_manifest_entries(manifest: dict[str, Any], raw: dict[str, bytes]) -> None:
    entries = manifest.get("entries")
    if not isinstance(entries, list):
        raise DerivativeExportError("source manifest entries are malformed")
    declared: set[str] = set()
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or set(entry) != {"path", "sha256", "bytes", "media_type", "kind"}
            or not isinstance(entry.get("path"), str)
        ):
            raise DerivativeExportError("source manifest entry is malformed")
        name = entry["path"]
        if name not in _SUPPORTED or name == "manifest.json":
            raise DerivativeExportError("source manifest declares an unsupported artifact class")
        data = raw.get(name)
        if (
            name in declared
            or data is None
            or type(entry.get("bytes")) is not int
            or entry["bytes"] != len(data)
            or entry.get("sha256") != hashlib.sha256(data).hexdigest()
        ):
            raise DerivativeExportError(f"source artifact digest mismatch: {name}")
        declared.add(name)
    if declared != set(raw) - {"manifest.json"} or manifest.get("entries_sha256") != localio.canonical_json_digest(
        entries
    ):
        raise DerivativeExportError("source package contents do not match its manifest")


def export_derivative(
    *,
    source: Path,
    out: Path,
    recipient: str,
    authority: str,
    lawful_basis: str,
    necessity: str,
) -> DerivativeReport:
    """Create one local, integrity-only derivative without changing its source."""
    source = source.expanduser()
    out = out.expanduser()
    if source.is_symlink() or not source.is_dir():
        raise DerivativeExportError("source package is not a directory")
    if out.is_symlink():
        raise DerivativeExportError("derivative output cannot be a symlink")
    source = source.resolve()
    out = out.parent.resolve() / out.name
    if out.exists():
        raise DerivativeExportError("derivative output already exists")
    try:
        out.relative_to(source)
    except ValueError:
        pass
    else:
        raise DerivativeExportError("derivative output cannot be inside its source package")

    names = sorted(path.name for path in source.iterdir())
    if "manifest.json" not in names or "receipt.json" not in names:
        raise DerivativeExportError("source is not a portable evidence package")
    known_names = [name for name in names if name in _SUPPORTED]
    raw = {name: _read(source / name) for name in known_names}
    manifest = _json_object(raw["manifest.json"], "manifest")
    if manifest.get("schema") != evidence_package.SCHEMA:
        raise DerivativeExportError("unsupported source package schema")
    _validate_manifest_entries(manifest, raw)
    receipt = _json_object(raw["receipt.json"], "receipt")
    try:
        receipt_snapshot = attestation_receipt.snapshot_stored_receipt(receipt)
    except attestation_receipt.ReceiptDigestError as exc:
        raise DerivativeExportError("source receipt digest is invalid") from exc
    source_metadata = manifest.get("source")
    if not isinstance(source_metadata, dict) or source_metadata.get("receipt_sha256") != receipt_snapshot.digest:
        raise DerivativeExportError("source manifest receipt digest does not match receipt")
    patch_data = raw.get("changes.patch")
    patch_digest = hashlib.sha256(patch_data).hexdigest() if patch_data is not None else None
    if (
        not isinstance(patch_digest, str)
        or receipt.get("changes_patch_sha256") != patch_digest
        or source_metadata.get("changes_patch_sha256") != patch_digest
    ):
        raise DerivativeExportError("source patch digest does not match receipt and manifest")
    minimized, removed, redaction_occurrences = _minimize_receipt(receipt)
    excluded = len(names) - len(known_names)
    safe_authorization: dict[str, Any] = {}
    for key, value in (
        ("recipient", recipient),
        ("authority", authority),
        ("lawful_basis", lawful_basis),
        ("necessity", necessity),
    ):
        if not isinstance(value, str):
            raise DerivativeExportError("allowlisted metadata has an invalid type")
        if not value.strip():
            raise DerivativeExportError(f"authorization metadata cannot be blank: {key}")
        safe_authorization[key], redacted = _safe_text(value)
        redaction_occurrences += redacted
    safe_authorization.update(
        {
            "organization_supplied": True,
        }
    )

    source_artifacts = [
        {"path": name, "sha256": hashlib.sha256(raw[name]).hexdigest(), "bytes": len(raw[name])} for name in known_names
    ]
    payload = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "classification": "minimized-derivative",
        "created_at": localio.utc_now_iso_z(),
        "authorization": safe_authorization,
        "source_artifacts": source_artifacts,
        "evidence": {"receipt_metadata": minimized},
        "transformation": {
            "policy_version": POLICY_VERSION,
            "removed_fields": removed,
            "redaction_occurrences": redaction_occurrences,
            "excluded_artifacts": excluded,
            "unknown_artifact_policy": "excluded",
        },
        "decisions": {
            "export": "authorized-local-copy",
            "source_quarantine": "unchanged",
            "source_cleanup": "not-authorized",
            "source_hold": "unchanged",
        },
        "verification": {
            "source_signed_objects": "not-included",
            "source_verification": "verify-originals-separately-by-recorded-digest",
            "derivative": "integrity-only",
        },
        "limitations": ["not-original-signed-object", "no-recipient-authorization-validation", "local-export-only"],
    }
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
    staging = out.parent / f".{out.name}.staging-{secrets.token_hex(4)}"
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        staging.mkdir(parents=True, mode=0o700)
        localio.write_bytes_atomic(staging / "derivative.json", encoded)
        os.replace(staging, out)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return DerivativeReport(removed, redaction_occurrences, excluded, hashlib.sha256(encoded).hexdigest())
