"""Acceptance tests for minimized derivative evidence packages."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from brigade import cli, evidence_derivative, evidence_package, localio


def _stamp_receipt(receipt: dict[str, object]) -> None:
    receipt["digests"] = {
        "algorithm": "sha256",
        "receipt_sha256": localio.canonical_json_digest(receipt, exclude_keys={"digests"}),
    }


def _refresh_package_entry(source: Path, name: str, data: bytes) -> None:
    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    entry = next(item for item in manifest["entries"] if item["path"] == name)
    entry["bytes"] = len(data)
    entry["sha256"] = hashlib.sha256(data).hexdigest()
    manifest["entries_sha256"] = localio.canonical_json_digest(manifest["entries"])
    (source / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _fabricated_run_dir(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    run_id = "20261013-120000-work-verify-derivative"
    run_dir = tmp_path / ".brigade" / "work" / "verify-runs" / run_id
    run_dir.mkdir(parents=True)
    receipt: dict[str, object] = {
        "run_id": run_id,
        "schema_version": 2,
        "status": "completed",
        "started_at": "2026-10-13T12:00:00Z",
        "completed_at": "2026-10-13T12:01:00Z",
        "target": str(tmp_path),
        "commands": [],
    }
    patch = "private patch fixture-derivative-value"
    receipt["changes_patch_sha256"] = hashlib.sha256(patch.encode()).hexdigest()
    _stamp_receipt(receipt)
    (run_dir / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
    (run_dir / "changes.patch").write_text(patch, encoding="utf-8")
    (run_dir / "summary.md").write_text("private summary fixture-derivative-value", encoding="utf-8")
    return run_dir, receipt


def test_source_inventory_stops_at_entry_limit_before_sorting(monkeypatch) -> None:
    requested_limits: list[int] = []

    def child_names(_root_fd: int, limit: int) -> list[str]:
        requested_limits.append(limit)
        return [f"unsupported-{index:04d}" for index in range(limit)]

    monkeypatch.setattr(evidence_derivative.dirfd, "child_names", child_names)
    try:
        evidence_derivative._source_inventory(0)
    except evidence_derivative.DerivativeExportError as exc:
        assert str(exc) == "source package inventory exceeds entry limit"
    else:
        raise AssertionError("oversized source inventory was accepted")

    assert requested_limits == [evidence_derivative._MAX_SOURCE_INVENTORY_ENTRIES + 1]


def test_derivative_minimizes_payloads_and_records_original_provenance(tmp_path: Path) -> None:
    run_dir, receipt = _fabricated_run_dir(tmp_path)
    sentinel = "fixture-derivative-value"
    receipt["environment"] = {"TOKEN": sentinel}
    receipt["path"] = f"/private/{sentinel}"
    _stamp_receipt(receipt)
    (run_dir / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
    source = tmp_path / "source"
    assert evidence_package.export_package(target=tmp_path, run_id=str(receipt["run_id"]), out_str=str(source)) == 0
    originals = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in source.iterdir() if path.is_file()
    }

    out = tmp_path / "derivative"
    report = evidence_derivative.export_derivative(
        source=source,
        out=out,
        recipient="recipient-42",
        authority="approved-case-7",
        lawful_basis="contract",
        necessity="verification-result review",
    )

    raw = (out / "derivative.json").read_text(encoding="utf-8")
    derivative = json.loads(raw)
    assert sentinel not in raw
    assert str(tmp_path) not in raw
    assert derivative["schema"] == "brigade.evidence_package_derivative.v1"
    assert derivative["classification"] == "minimized-derivative"
    assert derivative["transformation"]["policy_version"] == evidence_derivative.POLICY_VERSION
    assert derivative["transformation"]["removed_fields"] > 0
    assert derivative["verification"]["source_signed_objects"] == "not-included"
    assert derivative["verification"]["derivative"] == "integrity-only"
    assert {item["path"]: item["sha256"] for item in derivative["source_artifacts"]} == originals
    assert report.removed_fields == derivative["transformation"]["removed_fields"]
    assert [path.name for path in out.iterdir()] == ["derivative.json"]
    assert {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in source.iterdir() if path.is_file()
    } == originals


def test_derivative_excludes_inventory_logs_worker_checkpoint_and_prior_exports(tmp_path: Path) -> None:
    _, receipt = _fabricated_run_dir(tmp_path)
    source = tmp_path / "source"
    assert evidence_package.export_package(target=tmp_path, run_id=str(receipt["run_id"]), out_str=str(source)) == 0
    planted = "private-unsupported-artifact-value"
    for name in (
        "inventory.json",
        "logs.jsonl",
        "worker-events.json",
        "checkpoint.json",
        "previous-export.json",
    ):
        (source / name).write_text(f"{name}: {planted}", encoding="utf-8")

    out = tmp_path / "derivative"
    report = evidence_derivative.export_derivative(
        source=source,
        out=out,
        recipient="recipient-42",
        authority="approved-case-7",
        lawful_basis="contract",
        necessity="review",
    )

    raw = (out / "derivative.json").read_text(encoding="utf-8")
    assert planted not in raw
    assert report.excluded_artifacts == 5
    assert json.loads(raw)["transformation"]["excluded_artifacts"] == 5


@pytest.mark.parametrize("schema_version", [1, 2])
def test_derivative_accepts_receipt_without_stored_digest(tmp_path: Path, schema_version: int) -> None:
    run_dir, receipt = _fabricated_run_dir(tmp_path)
    receipt["schema_version"] = schema_version
    receipt.pop("digests")
    (run_dir / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
    source = tmp_path / "source"
    assert evidence_package.export_package(target=tmp_path, run_id=str(receipt["run_id"]), out_str=str(source)) == 0

    out = tmp_path / "derivative"
    evidence_derivative.export_derivative(
        source=source,
        out=out,
        recipient="recipient-42",
        authority="approved-case-7",
        lawful_basis="contract",
        necessity="review",
    )
    derivative = json.loads((out / "derivative.json").read_text(encoding="utf-8"))
    assert derivative["evidence"]["receipt_metadata"]["schema_version"] == schema_version
    assert derivative["evidence"]["receipt_metadata"]["run_id"] == receipt["run_id"]


def test_derivative_rejects_receipt_with_invalid_stored_digest(tmp_path: Path) -> None:
    _, receipt = _fabricated_run_dir(tmp_path)
    source = tmp_path / "source"
    assert evidence_package.export_package(target=tmp_path, run_id=str(receipt["run_id"]), out_str=str(source)) == 0
    altered = json.loads((source / "receipt.json").read_text(encoding="utf-8"))
    altered["status"] = "rewritten"
    receipt_bytes = json.dumps(altered, indent=2, sort_keys=True).encode() + b"\n"
    (source / "receipt.json").write_bytes(receipt_bytes)
    _refresh_package_entry(source, "receipt.json", receipt_bytes)

    try:
        evidence_derivative.export_derivative(
            source=source,
            out=tmp_path / "derivative",
            recipient="recipient-42",
            authority="approved-case-7",
            lawful_basis="contract",
            necessity="review",
        )
    except evidence_derivative.DerivativeExportError as exc:
        assert str(exc) == "source receipt digest does not match receipt content"
    else:
        raise AssertionError("derivative accepted a receipt with an invalid stored digest")


def test_derivative_rejects_receipt_rewritten_with_recomputed_package_hashes(tmp_path: Path) -> None:
    _, receipt = _fabricated_run_dir(tmp_path)
    source = tmp_path / "source"
    assert evidence_package.export_package(target=tmp_path, run_id=str(receipt["run_id"]), out_str=str(source)) == 0
    altered = json.loads((source / "receipt.json").read_text(encoding="utf-8"))
    altered["status"] = "rewritten"
    _stamp_receipt(altered)
    receipt_bytes = json.dumps(altered, indent=2, sort_keys=True).encode() + b"\n"
    (source / "receipt.json").write_bytes(receipt_bytes)
    _refresh_package_entry(source, "receipt.json", receipt_bytes)

    try:
        evidence_derivative.export_derivative(
            source=source,
            out=tmp_path / "derivative",
            recipient="recipient-42",
            authority="approved-case-7",
            lawful_basis="contract",
            necessity="review",
        )
    except evidence_derivative.DerivativeExportError as exc:
        assert str(exc) == "source manifest receipt digest does not match receipt"
    else:
        raise AssertionError("derivative accepted a rewritten receipt with recomputed package hashes")


@pytest.mark.parametrize(
    ("field", "value"),
    [("started_at", "not-a-timestamp"), ("completed_at", "2026-10-13T12:01:00")],
)
def test_derivative_rejects_receipt_with_invalid_timestamps(tmp_path: Path, field: str, value: str) -> None:
    _, receipt = _fabricated_run_dir(tmp_path)
    source = tmp_path / "source"
    assert evidence_package.export_package(target=tmp_path, run_id=str(receipt["run_id"]), out_str=str(source)) == 0
    altered = json.loads((source / "receipt.json").read_text(encoding="utf-8"))
    altered[field] = value
    _stamp_receipt(altered)
    receipt_bytes = json.dumps(altered, indent=2, sort_keys=True).encode() + b"\n"
    (source / "receipt.json").write_bytes(receipt_bytes)
    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    manifest["source"]["receipt_sha256"] = evidence_derivative.attestation_receipt.snapshot_stored_receipt(
        altered
    ).digest
    (source / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _refresh_package_entry(source, "receipt.json", receipt_bytes)

    with pytest.raises(evidence_derivative.DerivativeExportError, match=f"source receipt field is invalid: {field}"):
        evidence_derivative.export_derivative(
            source=source,
            out=tmp_path / "derivative",
            recipient="recipient-42",
            authority="approved-case-7",
            lawful_basis="contract",
            necessity="review",
        )


def test_derivative_rejects_receipt_with_stale_manifest_source_identities(tmp_path: Path) -> None:
    identity_fields = {
        "producer_run_id": "20261013-120001-work-verify-altered",
        "tree_fingerprint": "f" * 40,
        "baseline_commit": "a" * 40,
    }
    for field, altered_value in identity_fields.items():
        target = tmp_path / field
        target.mkdir()
        _, receipt = _fabricated_run_dir(target)
        source = target / "source"
        assert evidence_package.export_package(target=target, run_id=str(receipt["run_id"]), out_str=str(source)) == 0

        altered = json.loads((source / "receipt.json").read_text(encoding="utf-8"))
        altered[field] = altered_value
        _stamp_receipt(altered)
        receipt_bytes = json.dumps(altered, indent=2, sort_keys=True).encode() + b"\n"
        (source / "receipt.json").write_bytes(receipt_bytes)
        manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
        manifest["source"]["receipt_sha256"] = evidence_derivative.attestation_receipt.snapshot_stored_receipt(
            altered
        ).digest
        (source / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        _refresh_package_entry(source, "receipt.json", receipt_bytes)

        try:
            evidence_derivative.export_derivative(
                source=source,
                out=target / "derivative",
                recipient="recipient-42",
                authority="approved-case-7",
                lawful_basis="contract",
                necessity="review",
            )
        except evidence_derivative.DerivativeExportError as exc:
            assert str(exc) == "source manifest identities do not match receipt"
        else:
            raise AssertionError(f"derivative accepted a stale {field} manifest identity")


def test_derivative_rejects_source_artifact_replaced_after_initial_stat(tmp_path: Path, monkeypatch) -> None:
    _, receipt = _fabricated_run_dir(tmp_path)
    source = tmp_path / "source"
    assert evidence_package.export_package(target=tmp_path, run_id=str(receipt["run_id"]), out_str=str(source)) == 0
    original_open = evidence_derivative.dirfd.open_child_file
    replaced = False

    def replace_before_open(root_fd: int, name: str, flags: int, mode: int = 0o600, **kwargs) -> int:
        nonlocal replaced
        if name == "changes.patch" and not replaced:
            replacement = tmp_path / "replacement.patch"
            replacement.write_bytes(b"replacement")
            replacement.replace(source / name)
            replaced = True
        return original_open(root_fd, name, flags, mode, **kwargs)

    monkeypatch.setattr(evidence_derivative.dirfd, "open_child_file", replace_before_open)
    try:
        evidence_derivative.export_derivative(
            source=source,
            out=tmp_path / "derivative",
            recipient="recipient-42",
            authority="approved-case-7",
            lawful_basis="contract",
            necessity="review",
        )
    except evidence_derivative.DerivativeExportError as exc:
        assert str(exc) == "source artifact changed while reading: changes.patch"
    else:
        raise AssertionError("derivative accepted an artifact replaced after its initial stat")


def test_derivative_rejects_unsupported_portable_package_schema_versions(tmp_path: Path) -> None:
    for target_name, artifact_name, field, value, expected_error in (
        (
            "manifest-version",
            "manifest.json",
            "schema_version",
            99,
            "source manifest does not satisfy the portable package schema",
        ),
        (
            "receipt-version",
            "receipt.json",
            "schema_version",
            99,
            "source receipt does not satisfy the portable package schema",
        ),
    ):
        target = tmp_path / target_name
        target.mkdir()
        _, receipt = _fabricated_run_dir(target)
        source = target / "source"
        assert evidence_package.export_package(target=target, run_id=str(receipt["run_id"]), out_str=str(source)) == 0

        artifact = json.loads((source / artifact_name).read_text(encoding="utf-8"))
        artifact[field] = value
        if artifact_name == "receipt.json":
            _stamp_receipt(artifact)
            receipt_bytes = json.dumps(artifact, indent=2, sort_keys=True).encode() + b"\n"
            (source / artifact_name).write_bytes(receipt_bytes)
            manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
            manifest["source"]["receipt_sha256"] = evidence_derivative.attestation_receipt.snapshot_stored_receipt(
                artifact
            ).digest
            (source / "manifest.json").write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            _refresh_package_entry(source, artifact_name, receipt_bytes)
        else:
            (source / artifact_name).write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        try:
            evidence_derivative.export_derivative(
                source=source,
                out=target / "derivative",
                recipient="recipient-42",
                authority="approved-case-7",
                lawful_basis="contract",
                necessity="review",
            )
        except evidence_derivative.DerivativeExportError as exc:
            assert str(exc) == expected_error
        else:
            raise AssertionError(f"derivative accepted unsupported {target_name} schema version")


def test_derivative_rejects_source_inventory_added_during_export(tmp_path: Path, monkeypatch) -> None:
    _, receipt = _fabricated_run_dir(tmp_path)
    source = tmp_path / "source"
    assert evidence_package.export_package(target=tmp_path, run_id=str(receipt["run_id"]), out_str=str(source)) == 0
    original_open = evidence_derivative.dirfd.open_child_file
    added = False

    def add_artifact_after_inventory(root_fd: int, name: str, flags: int, mode: int = 0o600, **kwargs) -> int:
        nonlocal added
        if name == "changes.patch" and not added:
            (source / "attestation.json").write_text("late artifact", encoding="utf-8")
            added = True
        return original_open(root_fd, name, flags, mode, **kwargs)

    monkeypatch.setattr(evidence_derivative.dirfd, "open_child_file", add_artifact_after_inventory)
    try:
        evidence_derivative.export_derivative(
            source=source,
            out=tmp_path / "derivative",
            recipient="recipient-42",
            authority="approved-case-7",
            lawful_basis="contract",
            necessity="review",
        )
    except evidence_derivative.DerivativeExportError as exc:
        assert str(exc) == "source package inventory changed while reading"
    else:
        raise AssertionError("derivative accepted an artifact added after its initial inventory")


def test_derivative_rejects_artifact_rewritten_after_read_before_publish(tmp_path: Path, monkeypatch) -> None:
    _, receipt = _fabricated_run_dir(tmp_path)
    source = tmp_path / "source"
    assert evidence_package.export_package(target=tmp_path, run_id=str(receipt["run_id"]), out_str=str(source)) == 0
    patch_path = source / "changes.patch"
    original_minimize = evidence_derivative._minimize_receipt

    def rewrite_patch_after_read(value: dict[str, Any]) -> tuple[dict[str, Any], int, int]:
        result = original_minimize(value)
        patch_path.write_bytes(patch_path.read_bytes() + b"changed")
        return result

    monkeypatch.setattr(evidence_derivative, "_minimize_receipt", rewrite_patch_after_read)
    try:
        evidence_derivative.export_derivative(
            source=source,
            out=tmp_path / "derivative",
            recipient="recipient-42",
            authority="approved-case-7",
            lawful_basis="contract",
            necessity="review",
        )
    except evidence_derivative.DerivativeExportError as exc:
        assert str(exc) == "source artifact changed while reading: changes.patch"
    else:
        raise AssertionError("derivative accepted an artifact rewritten after its read")

    assert not (tmp_path / "derivative").exists()


def test_derivative_rejects_patch_rewritten_with_recomputed_package_hashes(tmp_path: Path) -> None:
    _, receipt = _fabricated_run_dir(tmp_path)
    source = tmp_path / "source"
    assert evidence_package.export_package(target=tmp_path, run_id=str(receipt["run_id"]), out_str=str(source)) == 0
    patch_bytes = b"rewritten patch bytes"
    (source / "changes.patch").write_bytes(patch_bytes)
    _refresh_package_entry(source, "changes.patch", patch_bytes)

    try:
        evidence_derivative.export_derivative(
            source=source,
            out=tmp_path / "derivative",
            recipient="recipient-42",
            authority="approved-case-7",
            lawful_basis="contract",
            necessity="review",
        )
    except evidence_derivative.DerivativeExportError as exc:
        assert str(exc) == "source patch digest does not match receipt and manifest"
    else:
        raise AssertionError("derivative accepted a rewritten patch with recomputed package hashes")


def test_derivative_records_omitted_patch_artifact(tmp_path: Path) -> None:
    _, receipt = _fabricated_run_dir(tmp_path)
    source = tmp_path / "source"
    assert evidence_package.export_package(target=tmp_path, run_id=str(receipt["run_id"]), out_str=str(source)) == 0
    (source / "changes.patch").unlink()
    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    manifest["entries"] = [entry for entry in manifest["entries"] if entry["path"] != "changes.patch"]
    manifest["entries_sha256"] = localio.canonical_json_digest(manifest["entries"])
    (source / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    out = tmp_path / "derivative"
    evidence_derivative.export_derivative(
        source=source,
        out=out,
        recipient="recipient-42",
        authority="approved-case-7",
        lawful_basis="contract",
        necessity="review",
    )
    derivative = json.loads((out / "derivative.json").read_text(encoding="utf-8"))
    assert "source-patch-omitted" in derivative["limitations"]
    assert derivative["evidence"]["receipt_metadata"]["changes_patch_sha256"] == receipt["changes_patch_sha256"]
    assert "changes.patch" not in {artifact["path"] for artifact in derivative["source_artifacts"]}


def test_derivative_accepts_package_without_patch_when_digest_is_unavailable(tmp_path: Path) -> None:
    run_dir, receipt = _fabricated_run_dir(tmp_path)
    receipt["changes_patch_sha256"] = None
    _stamp_receipt(receipt)
    (run_dir / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
    (run_dir / "changes.patch").unlink()
    source = tmp_path / "source"
    assert evidence_package.export_package(target=tmp_path, run_id=str(receipt["run_id"]), out_str=str(source)) == 0

    out = tmp_path / "derivative"
    evidence_derivative.export_derivative(
        source=source,
        out=out,
        recipient="recipient-42",
        authority="approved-case-7",
        lawful_basis="contract",
        necessity="review",
    )

    derivative = json.loads((out / "derivative.json").read_text(encoding="utf-8"))
    assert "changes.patch" not in {artifact["path"] for artifact in derivative["source_artifacts"]}
    assert derivative["evidence"]["receipt_metadata"]["status"] == "completed"
    assert derivative["evidence"]["receipt_metadata"]["completed_at"] == "2026-10-13T12:01:00Z"


def test_derivative_rejects_duplicate_json_properties(tmp_path: Path) -> None:
    for name in ("manifest.json", "receipt.json"):
        target = tmp_path / name.replace(".", "-")
        _, receipt = _fabricated_run_dir(target)
        source = target / "source"
        assert evidence_package.export_package(target=target, run_id=str(receipt["run_id"]), out_str=str(source)) == 0
        malformed = b'{"schema":"brigade.evidence_package.v1","schema":"brigade.evidence_package.v1"}'
        if name == "receipt.json":
            malformed = b'{"run_id":"first","run_id":"second"}'
            (source / name).write_bytes(malformed)
            _refresh_package_entry(source, name, malformed)
        else:
            (source / name).write_bytes(malformed)

        try:
            evidence_derivative.export_derivative(
                source=source,
                out=target / "derivative",
                recipient="recipient-42",
                authority="approved-case-7",
                lawful_basis="contract",
                necessity="review",
            )
        except evidence_derivative.DerivativeExportError as exc:
            assert str(exc) == f"source {name.removesuffix('.json')} is malformed"
        else:
            raise AssertionError(f"derivative accepted duplicate properties in {name}")


@pytest.mark.parametrize(
    ("name", "limit"),
    [
        ("manifest.json", evidence_derivative.evidence_package_snapshot._MANIFEST_MAX_BYTES),
        ("receipt.json", evidence_derivative.attestation_input.MAX_JSON_BYTES),
    ],
)
def test_derivative_rejects_oversized_json_before_opening(tmp_path: Path, monkeypatch, name: str, limit: int) -> None:
    _, receipt = _fabricated_run_dir(tmp_path)
    source = tmp_path / "source"
    assert evidence_package.export_package(target=tmp_path, run_id=str(receipt["run_id"]), out_str=str(source)) == 0
    (source / name).write_bytes(b" " * (limit + 1))
    opened: list[str] = []
    open_child_file = evidence_derivative.dirfd.open_child_file

    def observe_open(parent: int, child: str, flags: int, mode: int = 0o600, **kwargs) -> int:
        opened.append(child)
        return open_child_file(parent, child, flags, mode, **kwargs)

    monkeypatch.setattr(evidence_derivative.dirfd, "open_child_file", observe_open)

    try:
        evidence_derivative.export_derivative(
            source=source,
            out=tmp_path / "derivative",
            recipient="recipient-42",
            authority="approved-case-7",
            lawful_basis="contract",
            necessity="review",
        )
    except evidence_derivative.DerivativeExportError as exc:
        assert str(exc) == f"source artifact exceeds size limit: {name}"
    else:
        raise AssertionError("derivative accepted a manifest over the portable size limit")
    assert name not in opened
    assert not (tmp_path / "derivative").exists()


def test_derivative_does_not_replace_output_directory_created_during_publish(tmp_path: Path, monkeypatch) -> None:
    _, receipt = _fabricated_run_dir(tmp_path)
    source = tmp_path / "source"
    assert evidence_package.export_package(target=tmp_path, run_id=str(receipt["run_id"]), out_str=str(source)) == 0
    out = tmp_path / "derivative"
    mkdir_child = evidence_derivative.dirfd.mkdir_child

    def race_mkdir_child(parent: int, name: str) -> None:
        mkdir_child(parent, name)
        if name == out.name:
            raise FileExistsError(name)

    monkeypatch.setattr(evidence_derivative.dirfd, "mkdir_child", race_mkdir_child)
    try:
        evidence_derivative.export_derivative(
            source=source,
            out=out,
            recipient="recipient-42",
            authority="approved-case-7",
            lawful_basis="contract",
            necessity="review",
        )
    except evidence_derivative.DerivativeExportError as exc:
        assert str(exc) == "derivative output could not be published"
    else:
        raise AssertionError("derivative replaced an output directory created during publication")
    assert out.is_dir()
    assert list(out.iterdir()) == []


def test_derivative_preserves_concurrent_temporary_file_on_name_collision(tmp_path: Path, monkeypatch) -> None:
    _, receipt = _fabricated_run_dir(tmp_path)
    source = tmp_path / "source"
    assert evidence_package.export_package(target=tmp_path, run_id=str(receipt["run_id"]), out_str=str(source)) == 0
    monkeypatch.setattr(evidence_derivative.secrets, "token_hex", lambda _size: "collision")
    open_child_file = evidence_derivative.dirfd.open_child_file
    concurrent_data = b"owned by another export"

    def collide_on_temp(parent: int, name: str, flags: int, mode: int = 0o600, **kwargs) -> int:
        if name == ".derivative.json.tmp-collision":
            competitor = open_child_file(parent, name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
            os.write(competitor, concurrent_data)
            os.close(competitor)
        return open_child_file(parent, name, flags, mode, **kwargs)

    monkeypatch.setattr(evidence_derivative.dirfd, "open_child_file", collide_on_temp)

    try:
        evidence_derivative.export_derivative(
            source=source,
            out=tmp_path / "derivative",
            recipient="recipient-42",
            authority="approved-case-7",
            lawful_basis="contract",
            necessity="review",
        )
    except evidence_derivative.DerivativeExportError as exc:
        assert str(exc) == "derivative output could not be published"
    else:
        raise AssertionError("derivative accepted an already occupied temporary name")

    assert (tmp_path / "derivative" / ".derivative.json.tmp-collision").read_bytes() == concurrent_data


def test_derivative_preserves_concurrent_file_after_publish_link_collision(tmp_path: Path, monkeypatch) -> None:
    _, receipt = _fabricated_run_dir(tmp_path)
    source = tmp_path / "source"
    assert evidence_package.export_package(target=tmp_path, run_id=str(receipt["run_id"]), out_str=str(source)) == 0
    out = tmp_path / "derivative"
    concurrent_data = b"created by another process"

    link_child = evidence_derivative.dirfd.link_child
    replace_children = evidence_derivative.dirfd.replace_children

    def concurrent_link(parent: int, source: str, destination: str, *, replace: bool = True) -> None:
        if destination == "derivative.json":
            (out / destination).write_bytes(concurrent_data)
            raise FileExistsError(destination)
        if evidence_derivative.dirfd.posix_available():
            link_child(parent, source, destination)
        else:
            replace_children(parent, source, destination, replace=replace)

    monkeypatch.setattr(evidence_derivative.dirfd, "link_child", concurrent_link)
    monkeypatch.setattr(evidence_derivative.dirfd, "replace_children", concurrent_link)
    try:
        evidence_derivative.export_derivative(
            source=source,
            out=out,
            recipient="recipient-42",
            authority="approved-case-7",
            lawful_basis="contract",
            necessity="review",
        )
    except evidence_derivative.DerivativeExportError as exc:
        assert str(exc) == "derivative output could not be published"
    else:
        raise AssertionError("derivative accepted a concurrent output file")

    assert (out / "derivative.json").read_bytes() == concurrent_data


def test_derivative_fails_and_cleans_publication_when_temp_unlink_fails(tmp_path: Path, monkeypatch) -> None:
    if not evidence_derivative.dirfd.posix_available():
        pytest.skip("POSIX descriptor-relative hard-link publication required")
    _, receipt = _fabricated_run_dir(tmp_path)
    source = tmp_path / "source"
    assert evidence_package.export_package(target=tmp_path, run_id=str(receipt["run_id"]), out_str=str(source)) == 0
    out = tmp_path / "derivative"
    temp_name = ".derivative.json.tmp-unlink-once"
    monkeypatch.setattr(evidence_derivative.secrets, "token_hex", lambda _size: "unlink-once")
    unlink_child = evidence_derivative.dirfd.unlink_child
    failure_pending = True

    def fail_temp_unlink_once(parent: int, name: str) -> None:
        nonlocal failure_pending
        if name == temp_name and failure_pending:
            failure_pending = False
            raise PermissionError("temporary publication cleanup failed")
        unlink_child(parent, name)

    monkeypatch.setattr(evidence_derivative.dirfd, "unlink_child", fail_temp_unlink_once)
    try:
        evidence_derivative.export_derivative(
            source=source,
            out=out,
            recipient="recipient-42",
            authority="approved-case-7",
            lawful_basis="contract",
            necessity="review",
        )
    except evidence_derivative.DerivativeExportError as exc:
        assert str(exc) == "derivative output could not be published"
    else:
        raise AssertionError("derivative accepted a leftover publication temporary file")

    assert not out.exists()
    assert not list(tmp_path.glob(".derivative.staging-*"))


def test_derivative_rejects_output_parent_replaced_with_source(tmp_path: Path, monkeypatch) -> None:
    _, receipt = _fabricated_run_dir(tmp_path)
    source = tmp_path / "source"
    assert evidence_package.export_package(target=tmp_path, run_id=str(receipt["run_id"]), out_str=str(source)) == 0
    source_entries = {path.name for path in source.iterdir()}
    out_parent = tmp_path / "destination"
    out_parent.mkdir()
    displaced_parent = tmp_path / "destination-before-race"
    original_verify = evidence_derivative._verify_source_inventory

    def replace_parent_after_verification(*args) -> None:
        original_verify(*args)
        out_parent.rename(displaced_parent)
        source.rename(out_parent)

    monkeypatch.setattr(evidence_derivative, "_verify_source_inventory", replace_parent_after_verification)
    try:
        try:
            evidence_derivative.export_derivative(
                source=source,
                out=out_parent / "derivative",
                recipient="recipient-42",
                authority="approved-case-7",
                lawful_basis="contract",
                necessity="review",
            )
        except evidence_derivative.DerivativeExportError as exc:
            assert str(exc) == "derivative output parent changed during publication"
        else:
            raise AssertionError("derivative export followed a replaced output parent into the source package")

        assert {path.name for path in out_parent.iterdir()} == source_entries
    finally:
        if out_parent.exists() and not source.exists():
            out_parent.rename(source)
        if displaced_parent.exists() and not out_parent.exists():
            displaced_parent.rename(out_parent)


def test_derivative_rejects_symlinked_output_ancestor_after_resolution(tmp_path: Path, monkeypatch) -> None:
    _, receipt = _fabricated_run_dir(tmp_path)
    source = tmp_path / "source"
    assert evidence_package.export_package(target=tmp_path, run_id=str(receipt["run_id"]), out_str=str(source)) == 0
    source_entries = {path.name for path in source.iterdir()}
    ancestor = tmp_path / "destination"
    ancestor.mkdir()
    displaced = tmp_path / "destination-before-race"
    out_parent = ancestor / "nested"
    open_chain = evidence_derivative.dirfd.open_directory_chain
    swapped = False

    def swap_ancestor(path: Path, *, create: bool = False) -> int:
        nonlocal swapped
        if path == out_parent and not swapped:
            swapped = True
            ancestor.rename(displaced)
            try:
                ancestor.symlink_to(source, target_is_directory=True)
            except OSError as exc:
                displaced.rename(ancestor)
                pytest.skip(f"directory symlinks unavailable: {exc}")
        return open_chain(path, create=create)

    monkeypatch.setattr(evidence_derivative.dirfd, "open_directory_chain", swap_ancestor)
    try:
        try:
            evidence_derivative.export_derivative(
                source=source,
                out=out_parent / "derivative",
                recipient="recipient-42",
                authority="approved-case-7",
                lawful_basis="contract",
                necessity="review",
            )
        except evidence_derivative.DerivativeExportError as exc:
            assert str(exc) == "derivative output could not be published"
        else:
            raise AssertionError("derivative followed a replaced output ancestor into its source package")

        assert {path.name for path in source.iterdir()} == source_entries
        assert not (source / "derivative").exists()
    finally:
        if ancestor.is_symlink():
            ancestor.unlink()
        if displaced.exists() and not ancestor.exists():
            displaced.rename(ancestor)


def test_derivative_wraps_unsupported_publication_link_errors(tmp_path: Path, monkeypatch) -> None:
    _, receipt = _fabricated_run_dir(tmp_path)
    source = tmp_path / "source"
    assert evidence_package.export_package(target=tmp_path, run_id=str(receipt["run_id"]), out_str=str(source)) == 0

    def unsupported_publish(*_args, **_kwargs):
        raise OSError("no-clobber publication is unavailable")

    monkeypatch.setattr(evidence_derivative.dirfd, "link_child", unsupported_publish)
    monkeypatch.setattr(evidence_derivative.dirfd, "replace_children", unsupported_publish)
    try:
        evidence_derivative.export_derivative(
            source=source,
            out=tmp_path / "derivative",
            recipient="recipient-42",
            authority="approved-case-7",
            lawful_basis="contract",
            necessity="review",
        )
    except evidence_derivative.DerivativeExportError as exc:
        assert str(exc) == "derivative output could not be published"
    else:
        raise AssertionError("derivative exposed an unsupported hard-link error")

    assert not (tmp_path / "derivative").exists()


@pytest.mark.parametrize(
    ("key", "value"),
    [("status", True), ("started_at", 7), ("completed_at", False), ("exit_code", True)],
)
def test_derivative_rejects_invalid_copied_receipt_field_types(tmp_path: Path, key: str, value: object) -> None:
    _, receipt = _fabricated_run_dir(tmp_path)
    source = tmp_path / "source"
    assert evidence_package.export_package(target=tmp_path, run_id=str(receipt["run_id"]), out_str=str(source)) == 0
    altered = json.loads((source / "receipt.json").read_text(encoding="utf-8"))
    altered[key] = value
    _stamp_receipt(altered)
    receipt_bytes = json.dumps(altered, indent=2, sort_keys=True).encode() + b"\n"
    (source / "receipt.json").write_bytes(receipt_bytes)
    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    manifest["source"]["receipt_sha256"] = altered["digests"]["receipt_sha256"]
    (source / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _refresh_package_entry(source, "receipt.json", receipt_bytes)

    try:
        evidence_derivative.export_derivative(
            source=source,
            out=tmp_path / "derivative",
            recipient="recipient-42",
            authority="approved-case-7",
            lawful_basis="contract",
            necessity="review",
        )
    except evidence_derivative.DerivativeExportError as exc:
        assert str(exc) == f"source receipt field is invalid: {key}"
    else:
        raise AssertionError(f"derivative accepted invalid receipt field type for {key}")
    assert not list(tmp_path.glob(".derivative.staging-*"))


def test_derivative_rejects_blank_authorization_metadata(tmp_path: Path) -> None:
    _, receipt = _fabricated_run_dir(tmp_path)
    source = tmp_path / "source"
    assert evidence_package.export_package(target=tmp_path, run_id=str(receipt["run_id"]), out_str=str(source)) == 0

    for key in ("recipient", "authority", "lawful_basis", "necessity"):
        authorization = {
            "recipient": "recipient-42",
            "authority": "approved-case-7",
            "lawful_basis": "contract",
            "necessity": "review",
        }
        authorization[key] = "  "
        try:
            evidence_derivative.export_derivative(source=source, out=tmp_path / f"derivative-{key}", **authorization)
        except evidence_derivative.DerivativeExportError as exc:
            assert str(exc) == f"authorization metadata cannot be blank: {key}"
        else:
            raise AssertionError(f"derivative accepted blank {key} authorization metadata")


def test_derivative_counts_redaction_matches_separately_from_removed_fields(tmp_path: Path) -> None:
    _, receipt = _fabricated_run_dir(tmp_path)
    source = tmp_path / "source"
    assert evidence_package.export_package(target=tmp_path, run_id=str(receipt["run_id"]), out_str=str(source)) == 0
    out = tmp_path / "derivative"

    report = evidence_derivative.export_derivative(
        source=source,
        out=out,
        # Synthetic input. content-guard: allow email content-guard: allow api-key-assignment
        recipient='contact operator@contoso.example api_key="sk-test-abcdefghijklmnopqrstuv"',
        authority="approved-case-7",
        lawful_basis="contract",
        necessity="review",
    )

    raw = (out / "derivative.json").read_text(encoding="utf-8")
    transformation = json.loads(raw)["transformation"]
    assert report.removed_fields == transformation["removed_fields"] == 3
    assert report.redaction_occurrences == transformation["redaction_occurrences"] == 2
    assert "operator@contoso.example" not in raw  # content-guard: allow email
    assert "sk-test-abcdefghijklmnopqrstuv" not in raw


def test_derivative_refuses_export_inside_source_package(tmp_path: Path) -> None:
    _, receipt = _fabricated_run_dir(tmp_path)
    source = tmp_path / "source"
    assert evidence_package.export_package(target=tmp_path, run_id=str(receipt["run_id"]), out_str=str(source)) == 0

    try:
        evidence_derivative.export_derivative(
            source=source,
            out=source / "derivative",
            recipient="recipient-42",
            authority="approved-case-7",
            lawful_basis="contract",
            necessity="review",
        )
    except evidence_derivative.DerivativeExportError as exc:
        assert str(exc) == "derivative output cannot be inside its source package"
    else:
        raise AssertionError("derivative export inside source package was accepted")


def test_derivative_export_cli_writes_local_json(tmp_path: Path, capsys) -> None:
    _, receipt = _fabricated_run_dir(tmp_path)
    source = tmp_path / "source"
    out = tmp_path / "nested" / "derivative"
    assert evidence_package.export_package(target=tmp_path, run_id=str(receipt["run_id"]), out_str=str(source)) == 0

    assert (
        cli.main(
            [
                "receipts",
                "export",
                "derivative",
                "--source",
                str(source),
                "--out",
                str(out),
                "--recipient",
                "recipient-42",
                "--authority",
                "approved-case-7",
                "--lawful-basis",
                "contract",
                "--necessity",
                "verification-result review",
            ]
        )
        == 0
    )
    assert (out / "derivative.json").is_file()
    assert "evidence derivative exported" in capsys.readouterr().out


def test_derivative_export_cli_rejects_blank_authority(tmp_path: Path, capsys) -> None:
    _, receipt = _fabricated_run_dir(tmp_path)
    source = tmp_path / "source"
    out = tmp_path / "derivative"
    assert evidence_package.export_package(target=tmp_path, run_id=str(receipt["run_id"]), out_str=str(source)) == 0

    assert (
        cli.main(
            [
                "receipts",
                "export",
                "derivative",
                "--source",
                str(source),
                "--out",
                str(out),
                "--recipient",
                "recipient-42",
                "--authority",
                "",
                "--lawful-basis",
                "contract",
                "--necessity",
                "review",
            ]
        )
        == 1
    )
    assert not out.exists()
    assert "authorization metadata cannot be blank: authority" in capsys.readouterr().err


def test_derivative_reads_deny_concurrent_windows_writers(tmp_path: Path, monkeypatch) -> None:
    _, receipt = _fabricated_run_dir(tmp_path)
    source = tmp_path / "source"
    assert evidence_package.export_package(target=tmp_path, run_id=str(receipt["run_id"]), out_str=str(source)) == 0
    original_open = evidence_derivative.dirfd.open_child_file
    read_sharing: list[bool] = []

    def observe_read(parent, name, flags, mode=0o600, *, share_write=True):
        if not flags & (os.O_WRONLY | os.O_RDWR):
            read_sharing.append(share_write)
        return original_open(parent, name, flags, mode, share_write=share_write)

    monkeypatch.setattr(evidence_derivative.dirfd, "open_child_file", observe_read)
    evidence_derivative.export_derivative(
        source=source,
        out=tmp_path / "derivative",
        recipient="recipient-42",
        authority="approved-case-7",
        lawful_basis="contract",
        necessity="review",
    )
    assert read_sharing and not any(read_sharing)


def test_derivative_cli_handles_unavailable_directory_operations(tmp_path: Path, monkeypatch, capsys) -> None:
    source = tmp_path / "source"
    source.mkdir()

    def unavailable(*args, **kwargs):
        raise NotImplementedError("directory operations unavailable")

    monkeypatch.setattr(evidence_derivative.dirfd, "open_directory_nofollow", unavailable)
    result = cli.main(
        [
            "receipts",
            "export",
            "derivative",
            "--source",
            str(source),
            "--out",
            str(tmp_path / "out"),
            "--recipient",
            "recipient-42",
            "--authority",
            "approved-case-7",
            "--lawful-basis",
            "contract",
            "--necessity",
            "review",
        ]
    )
    assert result == 1
    assert "error: cannot export evidence derivative: directory operations unavailable" in capsys.readouterr().err


@pytest.mark.parametrize("competitor_content", [False, True])
def test_derivative_refuses_output_directory_swapped_before_open(
    tmp_path: Path, monkeypatch, competitor_content
) -> None:
    _, receipt = _fabricated_run_dir(tmp_path)
    source = tmp_path / "source"
    assert evidence_package.export_package(target=tmp_path, run_id=str(receipt["run_id"]), out_str=str(source)) == 0
    out = tmp_path / "derivative"
    original_open = evidence_derivative.dirfd.open_child_directory
    swapped = False

    def swap_before_open(parent, name, **kwargs):
        nonlocal swapped
        if name == out.name and not swapped:
            out.rename(tmp_path / "created-directory")
            out.mkdir()
            if competitor_content:
                (out / "competitor.txt").write_text("competing export", encoding="utf-8")
            swapped = True
        return original_open(parent, name, **kwargs)

    monkeypatch.setattr(evidence_derivative.dirfd, "open_child_directory", swap_before_open)
    with pytest.raises(evidence_derivative.DerivativeExportError, match="output directory changed"):
        evidence_derivative.export_derivative(
            source=source,
            out=out,
            recipient="recipient-42",
            authority="approved-case-7",
            lawful_basis="contract",
            necessity="review",
        )
    assert out.is_dir()
    assert {path.name for path in out.iterdir()} == ({"competitor.txt"} if competitor_content else set())
    if competitor_content:
        assert (out / "competitor.txt").read_text(encoding="utf-8") == "competing export"


@pytest.mark.skipif(not evidence_derivative.dirfd.posix_available(), reason="POSIX filename rules")
@pytest.mark.parametrize("component", ["2026-10-15T12:00", "parent\\child"])
def test_derivative_publishes_beneath_valid_posix_component(tmp_path: Path, component: str) -> None:
    _, receipt = _fabricated_run_dir(tmp_path)
    source = tmp_path / "source"
    assert evidence_package.export_package(target=tmp_path, run_id=str(receipt["run_id"]), out_str=str(source)) == 0
    out = tmp_path / component / "derivative"
    evidence_derivative.export_derivative(
        source=source,
        out=out,
        recipient="recipient-42",
        authority="approved-case-7",
        lawful_basis="contract",
        necessity="review",
    )
    assert (out / "derivative.json").is_file()


@pytest.mark.skipif(not evidence_derivative.dirfd.posix_available(), reason="POSIX allows concurrent file writes")
@pytest.mark.parametrize("phase", ["temporary-fsync", "published-fsync"])
def test_derivative_rejects_in_place_rewrite_after_fsync(tmp_path: Path, monkeypatch, phase: str) -> None:
    _, receipt = _fabricated_run_dir(tmp_path)
    source = tmp_path / "source"
    assert evidence_package.export_package(target=tmp_path, run_id=str(receipt["run_id"]), out_str=str(source)) == 0
    out = tmp_path / "derivative"
    fsync = os.fsync
    fsync_directory = evidence_derivative.dirfd.fsync_directory
    rewritten = False

    def rewrite(path: Path) -> None:
        nonlocal rewritten
        original = path.read_bytes()
        with path.open("r+b") as writer:
            writer.write(b"[" + original[1:])
        rewritten = True

    def rewrite_after_temp_fsync(descriptor: int) -> None:
        fsync(descriptor)
        if not rewritten:
            temporary = list(out.glob(".derivative.json.tmp-*"))
            if temporary:
                rewrite(temporary[0])

    def rewrite_after_published_fsync(descriptor: int) -> None:
        fsync_directory(descriptor)
        rewrite(out / "derivative.json")

    if phase == "temporary-fsync":
        monkeypatch.setattr(evidence_derivative.os, "fsync", rewrite_after_temp_fsync)
    else:
        monkeypatch.setattr(evidence_derivative.dirfd, "fsync_directory", rewrite_after_published_fsync)

    with pytest.raises(evidence_derivative.DerivativeExportError, match="output file changed"):
        evidence_derivative.export_derivative(
            source=source,
            out=out,
            recipient="recipient-42",
            authority="approved-case-7",
            lawful_basis="contract",
            necessity="review",
        )
    assert rewritten
    assert not out.exists()


def test_derivative_publication_denies_concurrent_windows_writers(tmp_path: Path, monkeypatch) -> None:
    _, receipt = _fabricated_run_dir(tmp_path)
    source = tmp_path / "source"
    assert evidence_package.export_package(target=tmp_path, run_id=str(receipt["run_id"]), out_str=str(source)) == 0
    original_open = evidence_derivative.dirfd.open_child_file
    publication_sharing: list[bool] = []

    def observe_open(parent, name, flags, mode=0o600, *, share_write=True):
        if flags & os.O_CREAT:
            publication_sharing.append(share_write)
        return original_open(parent, name, flags, mode, share_write=share_write)

    monkeypatch.setattr(evidence_derivative.dirfd, "open_child_file", observe_open)
    out = tmp_path / "derivative"
    report = evidence_derivative.export_derivative(
        source=source,
        out=out,
        recipient="recipient-42",
        authority="approved-case-7",
        lawful_basis="contract",
        necessity="review",
    )
    assert publication_sharing and not any(publication_sharing)
    assert report.derivative_sha256 == hashlib.sha256((out / "derivative.json").read_bytes()).hexdigest()


def test_derivative_rejects_foreign_output_entry_before_temporary_open(tmp_path: Path, monkeypatch) -> None:
    _, receipt = _fabricated_run_dir(tmp_path)
    source = tmp_path / "source"
    assert evidence_package.export_package(target=tmp_path, run_id=str(receipt["run_id"]), out_str=str(source)) == 0
    out = tmp_path / "derivative"
    original_open = evidence_derivative.dirfd.open_child_file
    competing_content = b"competing export"

    def add_foreign_entry(parent, name, flags, mode=0o600, **kwargs):
        if flags & os.O_CREAT:
            (out / "competitor.txt").write_bytes(competing_content)
        return original_open(parent, name, flags, mode, **kwargs)

    monkeypatch.setattr(evidence_derivative.dirfd, "open_child_file", add_foreign_entry)
    with pytest.raises(evidence_derivative.DerivativeExportError, match="output directory contains unexpected entries"):
        evidence_derivative.export_derivative(
            source=source,
            out=out,
            recipient="recipient-42",
            authority="approved-case-7",
            lawful_basis="contract",
            necessity="review",
        )
    assert (out / "competitor.txt").read_bytes() == competing_content
    assert {path.name for path in out.iterdir()} == {"competitor.txt"}


@pytest.mark.parametrize("which", ["source", "out"])
def test_derivative_reports_unresolvable_home_paths_as_export_errors(tmp_path: Path, which: str) -> None:
    missing_user = Path("~brigade-no-such-user-1700/package")
    paths = {"source": tmp_path, "out": tmp_path / "derivative"}
    paths[which] = missing_user

    with pytest.raises(evidence_derivative.DerivativeExportError, match="could not be resolved safely"):
        evidence_derivative.export_derivative(
            source=paths["source"],
            out=paths["out"],
            recipient="recipient-42",
            authority="approved-case-7",
            lawful_basis="contract",
            necessity="review",
        )


def test_derivative_rejects_output_inside_source_by_directory_identity(tmp_path: Path, monkeypatch) -> None:
    _, receipt = _fabricated_run_dir(tmp_path)
    source = tmp_path / "source"
    assert evidence_package.export_package(target=tmp_path, run_id=str(receipt["run_id"]), out_str=str(source)) == 0
    alias = tmp_path / "alias"
    alias.mkdir()
    real_stat = os.stat
    source_info = real_stat(source)

    def aliasing_stat(path, *args, **kwargs):
        # Emulate a case-insensitive name or bind mount: a different path text for the source directory.
        if Path(path) == alias:
            return source_info
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(evidence_derivative.os, "stat", aliasing_stat)
    before = sorted(entry.name for entry in source.iterdir())

    with pytest.raises(evidence_derivative.DerivativeExportError, match="cannot be inside its source package"):
        evidence_derivative.export_derivative(
            source=source,
            out=alias / "derivative",
            recipient="recipient-42",
            authority="approved-case-7",
            lawful_basis="contract",
            necessity="review",
        )
    assert sorted(entry.name for entry in source.iterdir()) == before
