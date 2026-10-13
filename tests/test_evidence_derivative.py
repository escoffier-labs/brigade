"""Acceptance tests for minimized derivative evidence packages."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

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
        assert str(exc) == "source receipt digest is invalid"
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

    def replace_before_open(root_fd: int, name: str, flags: int, mode: int = 0o600) -> int:
        nonlocal replaced
        if name == "changes.patch" and not replaced:
            replacement = tmp_path / "replacement.patch"
            replacement.write_bytes(b"replacement")
            replacement.replace(source / name)
            replaced = True
        return original_open(root_fd, name, flags, mode)

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
            1,
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

    def add_artifact_after_inventory(root_fd: int, name: str, flags: int, mode: int = 0o600) -> int:
        nonlocal added
        if name == "changes.patch" and not added:
            (source / "attestation.json").write_text("late artifact", encoding="utf-8")
            added = True
        return original_open(root_fd, name, flags, mode)

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


def test_derivative_rejects_package_missing_patch_artifact(tmp_path: Path) -> None:
    _, receipt = _fabricated_run_dir(tmp_path)
    source = tmp_path / "source"
    assert evidence_package.export_package(target=tmp_path, run_id=str(receipt["run_id"]), out_str=str(source)) == 0
    (source / "changes.patch").unlink()
    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    manifest["entries"] = [entry for entry in manifest["entries"] if entry["path"] != "changes.patch"]
    manifest["entries_sha256"] = localio.canonical_json_digest(manifest["entries"])
    (source / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

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
        raise AssertionError("derivative accepted a package without its patch artifact")


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


def test_derivative_rejects_manifest_over_portable_size_limit(tmp_path: Path) -> None:
    _, receipt = _fabricated_run_dir(tmp_path)
    source = tmp_path / "source"
    assert evidence_package.export_package(target=tmp_path, run_id=str(receipt["run_id"]), out_str=str(source)) == 0
    (source / "manifest.json").write_bytes(
        b" " * (evidence_derivative.evidence_package_snapshot._MANIFEST_MAX_BYTES + 1)
    )

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
        assert str(exc) == "source manifest is malformed"
    else:
        raise AssertionError("derivative accepted a manifest over the portable size limit")


def test_derivative_does_not_replace_output_directory_created_during_publish(tmp_path: Path, monkeypatch) -> None:
    _, receipt = _fabricated_run_dir(tmp_path)
    source = tmp_path / "source"
    assert evidence_package.export_package(target=tmp_path, run_id=str(receipt["run_id"]), out_str=str(source)) == 0
    out = tmp_path / "derivative"
    mkdir = Path.mkdir

    def race_mkdir(path: Path, *args, **kwargs) -> None:
        if path == out:
            mkdir(path, *args, **kwargs)
            raise FileExistsError(path)
        mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", race_mkdir)
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


def test_derivative_wraps_unsupported_publication_link_errors(tmp_path: Path, monkeypatch) -> None:
    _, receipt = _fabricated_run_dir(tmp_path)
    source = tmp_path / "source"
    assert evidence_package.export_package(target=tmp_path, run_id=str(receipt["run_id"]), out_str=str(source)) == 0

    def unsupported_link(*_args, **_kwargs):
        raise OSError("hard links are unavailable")

    monkeypatch.setattr(evidence_derivative.os, "link", unsupported_link)
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
        recipient='contact operator@contoso.example api_key="sk-test-abcdefghijklmnopqrstuv"',
        authority="approved-case-7",
        lawful_basis="contract",
        necessity="review",
    )

    raw = (out / "derivative.json").read_text(encoding="utf-8")
    transformation = json.loads(raw)["transformation"]
    assert report.removed_fields == transformation["removed_fields"] == 3
    assert report.redaction_occurrences == transformation["redaction_occurrences"] == 2
    assert "operator@contoso.example" not in raw
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
