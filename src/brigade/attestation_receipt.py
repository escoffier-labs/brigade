"""Bounded receipt snapshots for attestation export and local re-derivation."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import attestation_input, localio

MAX_RECEIPT_DIRECTORY_ENTRIES = 4096
MAX_RECEIPT_SCAN_BYTES = 32 * 1024 * 1024
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ReceiptDigestError(ValueError):
    """Raised when receipt digest evidence is invalid."""


class ReceiptSelectionError(ValueError):
    """Raised when a bounded verify receipt selection cannot be made."""


@dataclass(frozen=True)
class ReceiptSnapshot(Mapping[str, Any]):
    """One validated receipt value and its writer-compatible digest."""

    receipt: dict[str, Any]
    digest: str

    def __getitem__(self, key: str) -> Any:
        return self.receipt[key]

    def __iter__(self):
        return iter(self.receipt)

    def __len__(self) -> int:
        return len(self.receipt)


@dataclass(frozen=True)
class SelectedReceipt:
    """A receipt snapshot with separate filesystem placement metadata."""

    snapshot: ReceiptSnapshot
    directory: Path


def snapshot_receipt(receipt: Mapping[str, Any]) -> ReceiptSnapshot:
    """Snapshot a receipt and validate any stored writer digest evidence."""
    snapshot = attestation_input.validate_json_value(receipt)
    if not isinstance(snapshot, dict):
        raise ReceiptDigestError("receipt must be a JSON object")

    digest = localio.canonical_json_digest(snapshot, exclude_keys={"digests"})
    if "digests" not in snapshot:
        return ReceiptSnapshot(receipt=snapshot, digest=digest)

    digests = snapshot["digests"]
    if not isinstance(digests, dict):
        raise ReceiptDigestError("receipt digests must be an object")

    if "algorithm" in digests and digests["algorithm"] != "sha256":
        raise ReceiptDigestError("receipt digest algorithm must be sha256")
    if "receipt_sha256" not in digests:
        return ReceiptSnapshot(receipt=snapshot, digest=digest)

    stored_digest = digests["receipt_sha256"]
    if digests.get("algorithm") != "sha256":
        raise ReceiptDigestError("receipt digest algorithm must be sha256")
    if not isinstance(stored_digest, str) or not _SHA256_RE.fullmatch(stored_digest):
        raise ReceiptDigestError("receipt digest must be a lowercase SHA-256 hex string")
    if stored_digest != digest:
        raise ReceiptDigestError("receipt digest does not match receipt content")
    return ReceiptSnapshot(receipt=snapshot, digest=digest)


def snapshot_stored_receipt(receipt: Mapping[str, Any]) -> ReceiptSnapshot:
    """Snapshot a receipt and require a stored writer digest."""
    snapshot = snapshot_receipt(receipt)
    if not isinstance(snapshot.receipt.get("digests"), Mapping) or "receipt_sha256" not in snapshot.receipt["digests"]:
        raise ReceiptDigestError("receipt has no stored digest")
    return snapshot


def load_selected_receipt(target: Path, run_id: str) -> SelectedReceipt:
    """Strictly load one verify receipt without placing filesystem data in it."""
    root = target / ".brigade" / "work" / "verify-runs"
    if not root.is_dir():
        raise ReceiptSelectionError(f"verification run not found: {run_id}")

    if run_id != "latest":
        direct_directory = root / run_id
        if direct_directory.exists():
            return SelectedReceipt(
                snapshot=snapshot_receipt(_read_receipt(direct_directory / "receipt.json", 0)[0]),
                directory=direct_directory,
            )

    selected_receipt: dict[str, Any] | None = None
    selected_directory: Path | None = None
    selected_order: tuple[str, str] | None = None
    scanned_bytes = 0
    entry_count = 0
    try:
        entries = root.iterdir()
        for directory in entries:
            entry_count += 1
            if entry_count > MAX_RECEIPT_DIRECTORY_ENTRIES:
                raise ReceiptSelectionError("verification receipt scan exceeds directory entry limit")
            if not directory.is_dir():
                continue
            receipt, scanned_bytes = _read_receipt(directory / "receipt.json", scanned_bytes)
            candidate_run_id = receipt.get("run_id")
            if not isinstance(candidate_run_id, str):
                continue
            if run_id != "latest" and not candidate_run_id.startswith(run_id):
                continue
            if run_id != "latest" and selected_order is not None:
                raise ReceiptSelectionError(f"verification run id is ambiguous: {run_id}")
            order = (str(receipt.get("started_at") or candidate_run_id), candidate_run_id)
            if selected_order is None or order > selected_order:
                selected_receipt = receipt
                selected_directory = directory
                selected_order = order
    except OSError as exc:
        raise ReceiptSelectionError("verification receipt scan failed") from exc

    if selected_receipt is None or selected_directory is None:
        raise ReceiptSelectionError(f"verification run not found: {run_id}")
    return SelectedReceipt(snapshot=snapshot_receipt(selected_receipt), directory=selected_directory)


def _read_receipt(path: Path, scanned_bytes: int) -> tuple[dict[str, Any], int]:
    try:
        data = attestation_input.read_bounded_file(path, max_bytes=attestation_input.MAX_JSON_BYTES)
    except attestation_input.AttestationInputError as exc:
        if "exceeds byte limit" in str(exc):
            raise ReceiptSelectionError("verification receipt exceeds byte limit") from exc
        raise ReceiptSelectionError("verification receipt is invalid") from exc
    except OSError as exc:
        raise ReceiptSelectionError("verification receipt is not readable") from exc
    scanned_bytes += len(data)
    if scanned_bytes > MAX_RECEIPT_SCAN_BYTES:
        raise ReceiptSelectionError("verification receipt scan exceeds byte limit")
    try:
        value = attestation_input.strict_json_loads(data, max_bytes=attestation_input.MAX_JSON_BYTES)
    except attestation_input.AttestationInputError as exc:
        raise ReceiptSelectionError("verification receipt is invalid") from exc
    if not isinstance(value, dict):
        raise ReceiptSelectionError("verification receipt is invalid")
    return value, scanned_bytes
