"""Acceptance tests for minimized derivative evidence packages."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from brigade import cli, evidence_derivative, evidence_package, localio


def _stamp_receipt(receipt: dict[str, object]) -> None:
    receipt["digests"] = {
        "algorithm": "sha256",
        "receipt_sha256": localio.canonical_json_digest(receipt, exclude_keys={"digests"}),
    }


def _fabricated_run_dir(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    run_id = "20261013-120000-work-verify-derivative"
    run_dir = tmp_path / ".brigade" / "work" / "verify-runs" / run_id
    run_dir.mkdir(parents=True)
    receipt: dict[str, object] = {
        "run_id": run_id,
        "schema_version": 2,
        "status": "completed",
        "started_at": "2026-10-13T12:00:00Z",
        "target": str(tmp_path),
        "commands": [],
    }
    _stamp_receipt(receipt)
    (run_dir / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
    (run_dir / "changes.patch").write_text("private patch fixture-derivative-value", encoding="utf-8")
    (run_dir / "summary.md").write_text("private summary fixture-derivative-value", encoding="utf-8")
    return run_dir, receipt


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
