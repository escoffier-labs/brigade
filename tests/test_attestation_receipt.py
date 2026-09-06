"""Tests for verify-receipt snapshots used by attestation export."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from brigade import attestation_input, attestation_receipt, localio


def _receipt() -> dict[str, object]:
    return {
        "run_id": "20260905-120000-work-verify-receipt",
        "path": "/workspace/.brigade/work/verify-runs/20260905-120000-work-verify-receipt",
        "tree_fingerprint": "1" * 40,
        "changes_patch_sha256": "2" * 64,
        "metadata": {"digests": {"receipt_sha256": "content"}},
        "digests": {
            "algorithm": "sha256",
        },
    }


def _stamp(receipt: dict[str, object]) -> dict[str, object]:
    digests = receipt["digests"]
    assert isinstance(digests, dict)
    digests["receipt_sha256"] = localio.canonical_json_digest(receipt, exclude_keys={"digests"})
    return receipt


def test_snapshot_uses_writer_digest_and_retains_path_and_nested_digests_as_content() -> None:
    receipt = _stamp(_receipt())

    snapshot = attestation_receipt.snapshot_receipt(receipt)

    assert snapshot.digest == receipt["digests"]["receipt_sha256"]

    changed_path = dict(receipt)
    changed_path["path"] = "/workspace/moved/receipt"
    with pytest.raises(attestation_receipt.ReceiptDigestError, match="does not match"):
        attestation_receipt.snapshot_receipt(changed_path)

    changed_nested = dict(receipt)
    changed_metadata = dict(receipt["metadata"])
    changed_metadata["digests"] = {"receipt_sha256": "changed"}
    changed_nested["metadata"] = changed_metadata
    with pytest.raises(attestation_receipt.ReceiptDigestError, match="does not match"):
        attestation_receipt.snapshot_receipt(changed_nested)


def test_snapshot_computes_digest_when_receipt_has_no_stored_digest() -> None:
    receipt = _receipt()
    receipt.pop("digests")

    snapshot = attestation_receipt.snapshot_receipt(receipt)

    assert snapshot.digest == localio.canonical_json_digest(receipt, exclude_keys={"digests"})


def test_snapshot_stored_receipt_requires_receipt_sha256() -> None:
    receipt = _stamp(_receipt())
    assert attestation_receipt.snapshot_stored_receipt(receipt).digest == receipt["digests"]["receipt_sha256"]

    no_digests = _receipt()
    no_digests.pop("digests")
    with pytest.raises(attestation_receipt.ReceiptDigestError, match="receipt has no stored digest"):
        attestation_receipt.snapshot_stored_receipt(no_digests)

    missing_sha = _receipt()
    with pytest.raises(attestation_receipt.ReceiptDigestError, match="receipt has no stored digest"):
        attestation_receipt.snapshot_stored_receipt(missing_sha)


def test_selected_receipt_keeps_directory_metadata_outside_receipt_content(tmp_path: Path) -> None:
    receipt = _receipt()
    receipt.pop("path")
    _stamp(receipt)
    directory = tmp_path / ".brigade" / "work" / "verify-runs" / str(receipt["run_id"])
    directory.mkdir(parents=True)
    (directory / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")

    selected = attestation_receipt.load_selected_receipt(tmp_path, str(receipt["run_id"]))

    assert selected.directory == directory
    assert "path" not in selected.snapshot.receipt


def test_exact_selection_refuses_a_stale_receipt_without_prefix_fallback(tmp_path: Path) -> None:
    root = tmp_path / ".brigade" / "work" / "verify-runs"
    run_id = "20260905-120000-work-verify-stale"
    stale = _stamp(_receipt())
    stale["run_id"] = run_id
    stale["path"] = "/workspace/stale"
    stale_directory = root / run_id
    stale_directory.mkdir(parents=True)
    (stale_directory / "receipt.json").write_text(json.dumps(stale), encoding="utf-8")

    valid = _stamp(_receipt())
    valid["run_id"] = f"{run_id}-other"
    valid["path"] = "/workspace/other"
    valid["digests"]["receipt_sha256"] = localio.canonical_json_digest(valid, exclude_keys={"digests"})
    valid_directory = root / str(valid["run_id"])
    valid_directory.mkdir()
    (valid_directory / "receipt.json").write_text(json.dumps(valid), encoding="utf-8")

    with pytest.raises(attestation_receipt.ReceiptDigestError, match="does not match"):
        attestation_receipt.load_selected_receipt(tmp_path, run_id)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda receipt: receipt.__setitem__("digests", []), "digests must be an object"),
        (
            lambda receipt: receipt["digests"].__setitem__("algorithm", "sha512"),
            "algorithm must be sha256",
        ),
        (
            lambda receipt: receipt["digests"].__setitem__("receipt_sha256", "ABC"),
            "lowercase SHA-256 hex",
        ),
    ],
)
def test_snapshot_rejects_invalid_stored_digest_evidence(mutate, message: str) -> None:
    receipt = _stamp(_receipt())
    mutate(receipt)

    with pytest.raises(attestation_receipt.ReceiptDigestError, match=message):
        attestation_receipt.snapshot_receipt(receipt)


def test_prefix_selection_refuses_ambiguous_matches(tmp_path: Path) -> None:
    root = tmp_path / ".brigade" / "work" / "verify-runs"
    a = _receipt()
    a["run_id"] = "20260905-120000-work-verify-abc"
    a["started_at"] = "2026-09-02T12:00:00Z"
    a.pop("path", None)
    _stamp(a)
    dir_a = root / a["run_id"]
    dir_a.mkdir(parents=True)
    (dir_a / "receipt.json").write_text(json.dumps(a), encoding="utf-8")

    b = _receipt()
    b["run_id"] = "20260905-120000-work-verify-abcd"
    b["started_at"] = "2026-09-02T12:00:01Z"
    b.pop("path", None)
    _stamp(b)
    dir_b = root / b["run_id"]
    dir_b.mkdir(parents=True)
    (dir_b / "receipt.json").write_text(json.dumps(b), encoding="utf-8")

    with pytest.raises(
        attestation_receipt.ReceiptSelectionError,
        match="verification run id is ambiguous: 20260905-120000-work-verify-a",
    ):
        attestation_receipt.load_selected_receipt(tmp_path, "20260905-120000-work-verify-a")

    assert attestation_receipt.load_selected_receipt(tmp_path, a["run_id"]).directory == dir_a
    assert attestation_receipt.load_selected_receipt(tmp_path, b["run_id"]).directory == dir_b
    assert attestation_receipt.load_selected_receipt(tmp_path, "latest").directory == dir_b


def test_scan_budget_enforced_across_multiple_receipts(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / ".brigade" / "work" / "verify-runs"
    a = {
        "run_id": "20260905-120000-work-verify-abc",
        "started_at": "2026-09-02T12:00:00Z",
        "tree_fingerprint": "1" * 40,
        "changes_patch_sha256": "2" * 64,
        "digests": {"algorithm": "sha256"},
    }
    a["digests"]["receipt_sha256"] = localio.canonical_json_digest(a, exclude_keys={"digests"})
    dir_a = root / a["run_id"]
    dir_a.mkdir(parents=True)
    data_a = json.dumps(a).encode("utf-8")
    (dir_a / "receipt.json").write_bytes(data_a)

    b = dict(a)
    b["run_id"] = "20260905-120000-work-verify-abcd"
    b["started_at"] = "2026-09-02T12:00:01Z"
    b["digests"] = {"algorithm": "sha256"}
    b["digests"]["receipt_sha256"] = localio.canonical_json_digest(b, exclude_keys={"digests"})
    dir_b = root / b["run_id"]
    dir_b.mkdir(parents=True)
    data_b = json.dumps(b).encode("utf-8")
    (dir_b / "receipt.json").write_bytes(data_b)

    monkeypatch.setattr(attestation_receipt, "MAX_RECEIPT_SCAN_BYTES", len(data_a) + 1)

    with pytest.raises(attestation_receipt.ReceiptSelectionError, match="verification receipt scan exceeds byte limit"):
        attestation_receipt.load_selected_receipt(tmp_path, "latest")


def test_scan_budget_counts_bytes_read_not_stat_size(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / ".brigade" / "work" / "verify-runs"
    receipt = {
        "run_id": "20260905-120000-work-verify-abc",
        "started_at": "2026-09-02T12:00:00Z",
        "tree_fingerprint": "1" * 40,
        "changes_patch_sha256": "2" * 64,
        "digests": {"algorithm": "sha256"},
    }
    receipt["digests"]["receipt_sha256"] = localio.canonical_json_digest(receipt, exclude_keys={"digests"})
    directory = root / receipt["run_id"]
    directory.mkdir(parents=True)
    (directory / "receipt.json").write_bytes(b'{"run_id":"abc"}')

    monkeypatch.setattr(attestation_receipt, "MAX_RECEIPT_SCAN_BYTES", 100)

    original_read_bounded_file = attestation_input.read_bounded_file

    def _inflate(path: Path, *, max_bytes: int = attestation_input.MAX_JSON_BYTES) -> bytes:
        data = original_read_bounded_file(path, max_bytes=max_bytes)
        if path.name == "receipt.json":
            return data + b" " * 200
        return data

    monkeypatch.setattr(attestation_input, "read_bounded_file", _inflate)

    with pytest.raises(attestation_receipt.ReceiptSelectionError, match="verification receipt scan exceeds byte limit"):
        attestation_receipt.load_selected_receipt(tmp_path, "latest")
