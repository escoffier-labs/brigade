"""Tests for portable evidence package export and verification."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from brigade import attestation_receipt, evidence_package, localio


def _stamp_receipt(receipt: dict[str, object]) -> dict[str, object]:
    digests = receipt.setdefault("digests", {})
    assert isinstance(digests, dict)
    digests["algorithm"] = "sha256"
    digests["receipt_sha256"] = localio.canonical_json_digest(receipt, exclude_keys={"digests"})
    return receipt


def _fabricated_run_dir(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    run_id = "20260905-120000-work-verify-pkg"
    run_dir = tmp_path / ".brigade" / "work" / "verify-runs" / run_id
    run_dir.mkdir(parents=True)

    receipt = {
        "run_id": run_id,
        "path": str(run_dir),
        "schema_version": 2,
        "status": "completed",
        "started_at": "2026-09-05T12:00:00Z",
        "target": str(tmp_path),
        "commands": [],
        "baseline_commit": "abc123",
        "tree_fingerprint": "1" * 40,
        "changes_patch_sha256": "2" * 64,
        "producer_run_id": "20260905-110000-work-run-pkg",
        "digests": {"algorithm": "sha256"},
    }
    _stamp_receipt(receipt)
    (run_dir / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")

    (run_dir / "changes.patch").write_text("diff --git a/file.txt b/file.txt\n", encoding="utf-8")
    (run_dir / "attestation.json").write_text(json.dumps({"_type": "Statement"}), encoding="utf-8")
    (run_dir / "attestation.sigstore.json").write_text(json.dumps({"mediaType": "bundle"}), encoding="utf-8")
    (run_dir / "summary.md").write_text("# Summary\n", encoding="utf-8")
    # This file is not part of the allowlist and must not be exported.
    (run_dir / "command-1-stdout.log").write_text("secret log\n", encoding="utf-8")

    return run_dir, receipt


def test_export_and_verify_round_trip_exits_0(tmp_path: Path, capsys) -> None:
    run_dir, receipt = _fabricated_run_dir(tmp_path)
    out_dir = tmp_path / "exported-pkg"

    assert (
        evidence_package.export_package(
            target=tmp_path,
            run_id=receipt["run_id"],
            out_str=str(out_dir),
            force=False,
            json_output=False,
        )
        == 0
    )

    captured = capsys.readouterr()
    assert "evidence package exported" in captured.out
    assert str(receipt["run_id"]) in captured.out

    assert out_dir.is_dir()
    assert (out_dir / "manifest.json").is_file()
    assert (out_dir / "receipt.json").is_file()
    assert (out_dir / "changes.patch").is_file()
    assert (out_dir / "attestation.json").is_file()
    assert (out_dir / "attestation.sigstore.json").is_file()
    assert (out_dir / "summary.md").is_file()
    assert not (out_dir / "command-1-stdout.log").exists()

    assert (
        evidence_package.verify_package(
            directory=out_dir,
            json_output=False,
        )
        == 0
    )

    captured = capsys.readouterr()
    assert "verification ok" in captured.out


def test_manifest_excludes_absolute_paths_and_target_and_self_digest(tmp_path: Path) -> None:
    run_dir, receipt = _fabricated_run_dir(tmp_path)
    out_dir = tmp_path / "exported-pkg"
    evidence_package.export_package(
        target=tmp_path,
        run_id=receipt["run_id"],
        out_str=str(out_dir),
    )

    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema"] == evidence_package.SCHEMA
    assert "manifest_sha256" not in manifest
    manifest_text = json.dumps(manifest, sort_keys=True)
    assert str(tmp_path) not in manifest_text
    assert str(out_dir) not in manifest_text
    assert str(run_dir) not in manifest_text
    assert "manifest.json" not in manifest_text

    source = manifest["source"]
    assert source["verify_run_id"] == receipt["run_id"]
    assert source["producer_run_id"] == receipt["producer_run_id"]
    assert source["tree_fingerprint"] == receipt["tree_fingerprint"]
    assert source["baseline_commit"] == receipt["baseline_commit"]
    assert source["changes_patch_sha256"] == receipt["changes_patch_sha256"]
    assert "receipt_sha256" in source


def test_manifest_entries_sorted_and_exclude_planted_log(tmp_path: Path) -> None:
    run_dir, receipt = _fabricated_run_dir(tmp_path)
    out_dir = tmp_path / "exported-pkg"
    evidence_package.export_package(
        target=tmp_path,
        run_id=receipt["run_id"],
        out_str=str(out_dir),
    )

    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    paths = [entry["path"] for entry in manifest["entries"]]
    assert paths == sorted(paths)
    assert "command-1-stdout.log" not in paths
    assert "receipt.json" in paths


def test_verify_reports_missing_mismatch_and_extra_with_exit_1(tmp_path: Path) -> None:
    run_dir, receipt = _fabricated_run_dir(tmp_path)
    out_dir = tmp_path / "exported-pkg"
    evidence_package.export_package(
        target=tmp_path,
        run_id=receipt["run_id"],
        out_str=str(out_dir),
    )

    # Remove a file to report missing.
    (out_dir / "changes.patch").unlink()
    # Corrupt a file to report mismatch.
    (out_dir / "summary.md").write_text("tampered", encoding="utf-8")
    # Add an extra file.
    (out_dir / "extra.txt").write_text("extra", encoding="utf-8")

    assert (
        evidence_package.verify_package(
            directory=out_dir,
            json_output=True,
        )
        == 1
    )

    assert not (out_dir / "changes.patch").exists()


def test_verify_json_reports_states(tmp_path: Path, capsys) -> None:
    run_dir, receipt = _fabricated_run_dir(tmp_path)
    out_dir = tmp_path / "exported-pkg"
    evidence_package.export_package(
        target=tmp_path,
        run_id=receipt["run_id"],
        out_str=str(out_dir),
    )
    (out_dir / "changes.patch").unlink()
    (out_dir / "summary.md").write_text("tampered", encoding="utf-8")
    (out_dir / "extra.txt").write_text("extra", encoding="utf-8")

    capsys.readouterr()  # drain export output
    assert evidence_package.verify_package(directory=out_dir, json_output=True) == 1
    out = capsys.readouterr().out
    result = json.loads(out)
    assert result["schema"] == evidence_package.VERIFY_SCHEMA
    states = {item["path"]: item["state"] for item in result["entries"]}
    assert states["changes.patch"] == "missing"
    assert states["summary.md"] == "mismatch"
    assert result["extras"] == ["extra.txt"]
    assert result["manifest_entries"] == "match"
    assert result["receipt_digest"] == "match"
    assert result["ok"] is False

    # Keys are sorted.
    assert list(result.keys()) == sorted(result.keys())


def test_refuses_existing_out_without_force(tmp_path: Path, capsys, monkeypatch) -> None:
    run_dir, receipt = _fabricated_run_dir(tmp_path)
    out_dir = tmp_path / "exported-pkg"
    out_dir.mkdir()
    (out_dir / "placeholder.txt").write_text("keep", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert (
        evidence_package.export_package(
            target=tmp_path,
            run_id=receipt["run_id"],
            out_str="exported-pkg",
            force=False,
        )
        == 1
    )
    assert (out_dir / "placeholder.txt").exists()
    err = capsys.readouterr().err
    assert "already exists" in err
    assert "exported-pkg" in err
    assert str(tmp_path) not in err


def test_missing_receipt_prints_filename_not_path(tmp_path: Path, capsys, monkeypatch) -> None:
    run_dir, receipt = _fabricated_run_dir(tmp_path)
    out_dir = tmp_path / "exported-pkg"

    original_load = attestation_receipt.load_selected_receipt

    def load_and_remove_receipt(target: Path, run_id: str) -> attestation_receipt.SelectedReceipt:
        selected = original_load(target, run_id)
        (selected.directory / "receipt.json").unlink()
        return selected

    monkeypatch.setattr(attestation_receipt, "load_selected_receipt", load_and_remove_receipt)

    assert (
        evidence_package.export_package(
            target=tmp_path,
            run_id=receipt["run_id"],
            out_str=str(out_dir),
        )
        == 1
    )
    err = capsys.readouterr().err
    assert err.startswith("error: cannot export evidence package: ")
    assert "required receipt file missing" in err
    assert "receipt.json" in err
    assert str(tmp_path) not in err


def test_export_catches_unreadable_source_file(tmp_path: Path, capsys) -> None:
    run_dir, receipt = _fabricated_run_dir(tmp_path)
    (run_dir / "changes.patch").unlink()
    (run_dir / "changes.patch").mkdir()
    out_dir = tmp_path / "exported-pkg"

    assert (
        evidence_package.export_package(
            target=tmp_path,
            run_id=receipt["run_id"],
            out_str=str(out_dir),
        )
        == 1
    )
    err = capsys.readouterr().err
    assert err.startswith("error: cannot export evidence package: ")
    assert "changes.patch" in err
    assert "Traceback" not in err


def test_replaces_existing_out_with_force(tmp_path: Path, capsys) -> None:
    run_dir, receipt = _fabricated_run_dir(tmp_path)
    out_dir = tmp_path / "exported-pkg"
    out_dir.mkdir()
    (out_dir / "placeholder.txt").write_text("old", encoding="utf-8")

    assert (
        evidence_package.export_package(
            target=tmp_path,
            run_id=receipt["run_id"],
            out_str=str(out_dir),
            force=True,
        )
        == 0
    )
    assert not (out_dir / "placeholder.txt").exists()
    assert (out_dir / "manifest.json").is_file()
    # Replaced backup directory should have been removed.
    assert not any(p.name.startswith(f"{out_dir.name}.replaced-") for p in tmp_path.iterdir())


def test_staging_failure_leaves_no_staging_or_out(tmp_path: Path, monkeypatch) -> None:
    run_dir, receipt = _fabricated_run_dir(tmp_path)
    out_dir = tmp_path / "exported-pkg"
    original_write = localio.write_bytes_atomic
    calls = [0]

    def failing_write(path: Path, data: bytes) -> None:
        calls[0] += 1
        if calls[0] == 2:
            raise OSError("simulated staging failure")
        original_write(path, data)

    monkeypatch.setattr(localio, "write_bytes_atomic", failing_write)

    with pytest.raises(OSError, match="simulated staging failure"):
        evidence_package.export_package(
            target=tmp_path,
            run_id=receipt["run_id"],
            out_str=str(out_dir),
        )

    assert not out_dir.exists()
    assert not any(p.name.startswith(f"{out_dir.name}.staging-") for p in tmp_path.iterdir())


def test_out_inside_verify_runs_is_refused(tmp_path: Path, capsys) -> None:
    run_dir, receipt = _fabricated_run_dir(tmp_path)
    bad_out = tmp_path / ".brigade" / "work" / "verify-runs" / "pkg-out"

    assert (
        evidence_package.export_package(
            target=tmp_path,
            run_id=receipt["run_id"],
            out_str=str(bad_out),
        )
        == 1
    )
    assert "verify-runs directory" in capsys.readouterr().err


def test_verify_rejects_unsafe_entry_paths(tmp_path: Path, capsys) -> None:
    run_dir, receipt = _fabricated_run_dir(tmp_path)
    out_dir = tmp_path / "exported-pkg"
    evidence_package.export_package(
        target=tmp_path,
        run_id=receipt["run_id"],
        out_str=str(out_dir),
    )

    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    manifest["entries"].append(
        {
            "path": "../escape.patch",
            "sha256": "0" * 64,
            "bytes": 4,
            "media_type": "text/plain",
            "kind": "patch",
        }
    )
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")

    assert evidence_package.verify_package(directory=out_dir, json_output=False) == 1
    assert "unsafe" in capsys.readouterr().err


@pytest.mark.parametrize("unsafe_path", [".", "manifest.json"])
def test_verify_rejects_dot_and_manifest_entry_paths(tmp_path: Path, capsys, unsafe_path: str) -> None:
    run_dir, receipt = _fabricated_run_dir(tmp_path)
    out_dir = tmp_path / "exported-pkg"
    evidence_package.export_package(
        target=tmp_path,
        run_id=receipt["run_id"],
        out_str=str(out_dir),
    )

    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    manifest["entries"].append(
        {
            "path": unsafe_path,
            "sha256": "0" * 64,
            "bytes": 4,
            "media_type": "text/plain",
            "kind": "patch",
        }
    )
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")

    assert evidence_package.verify_package(directory=out_dir, json_output=False) == 1
    assert "unsafe" in capsys.readouterr().err


def test_verify_rejects_separator_in_entry_path(tmp_path: Path, capsys) -> None:
    run_dir, receipt = _fabricated_run_dir(tmp_path)
    out_dir = tmp_path / "exported-pkg"
    evidence_package.export_package(
        target=tmp_path,
        run_id=receipt["run_id"],
        out_str=str(out_dir),
    )

    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    manifest["entries"].append(
        {
            "path": "sub/receipt.json",
            "sha256": "0" * 64,
            "bytes": 4,
            "media_type": "application/json",
            "kind": "receipt",
        }
    )
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")

    assert evidence_package.verify_package(directory=out_dir, json_output=False) == 1
    assert "unsafe" in capsys.readouterr().err


def test_verify_manifest_sha256_matches_file_bytes(tmp_path: Path, capsys) -> None:
    run_dir, receipt = _fabricated_run_dir(tmp_path)
    out_dir = tmp_path / "exported-pkg"
    evidence_package.export_package(
        target=tmp_path,
        run_id=receipt["run_id"],
        out_str=str(out_dir),
    )

    capsys.readouterr()  # drain export output
    expected_sha256 = hashlib.sha256((out_dir / "manifest.json").read_bytes()).hexdigest()
    assert evidence_package.verify_package(directory=out_dir, json_output=True) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["manifest_sha256"] == expected_sha256


def test_verify_rejects_symlinked_manifest(tmp_path: Path, capsys) -> None:
    run_dir, receipt = _fabricated_run_dir(tmp_path)
    out_dir = tmp_path / "exported-pkg"
    evidence_package.export_package(
        target=tmp_path,
        run_id=receipt["run_id"],
        out_str=str(out_dir),
    )

    real_manifest = out_dir / "manifest.json"
    manifest_bytes = real_manifest.read_bytes()
    real_manifest.unlink()
    symlink_target = tmp_path / "manifest-symlink-target.json"
    symlink_target.write_bytes(manifest_bytes)
    (out_dir / "manifest.json").symlink_to(symlink_target)

    assert evidence_package.verify_package(directory=out_dir, json_output=False) == 1
    err = capsys.readouterr().err
    assert "manifest.json is a symlink" in err


def test_tampered_receipt_reports_receipt_digest_mismatch(tmp_path: Path, capsys) -> None:
    run_dir, receipt = _fabricated_run_dir(tmp_path)
    out_dir = tmp_path / "exported-pkg"
    evidence_package.export_package(
        target=tmp_path,
        run_id=receipt["run_id"],
        out_str=str(out_dir),
    )

    receipt_data = json.loads((out_dir / "receipt.json").read_text(encoding="utf-8"))
    receipt_data["status"] = "failed"
    (out_dir / "receipt.json").write_text(json.dumps(receipt_data), encoding="utf-8")

    capsys.readouterr()  # drain export output
    assert evidence_package.verify_package(directory=out_dir, json_output=True) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["receipt_digest"] == "mismatch"


def test_ambiguous_prefix_maps_to_exit_1(tmp_path: Path, capsys) -> None:
    root = tmp_path / ".brigade" / "work" / "verify-runs"
    root.mkdir(parents=True)
    a = {
        "run_id": "20260905-120000-work-verify-abc",
        "started_at": "2026-09-02T12:00:00Z",
        "tree_fingerprint": "1" * 40,
        "changes_patch_sha256": "2" * 64,
        "status": "completed",
        "target": str(tmp_path),
        "commands": [],
        "digests": {"algorithm": "sha256"},
    }
    _stamp_receipt(a)
    (root / a["run_id"]).mkdir()
    (root / a["run_id"] / "receipt.json").write_text(json.dumps(a), encoding="utf-8")
    b = dict(a)
    b["run_id"] = "20260905-120000-work-verify-abcd"
    b["started_at"] = "2026-09-02T12:00:01Z"
    b["digests"] = {"algorithm": "sha256"}
    _stamp_receipt(b)
    (root / b["run_id"]).mkdir()
    (root / b["run_id"] / "receipt.json").write_text(json.dumps(b), encoding="utf-8")

    assert (
        evidence_package.export_package(
            target=tmp_path,
            run_id="20260905-120000-work-verify-a",
            out_str=str(tmp_path / "out"),
        )
        == 1
    )
    assert "verification run id is ambiguous" in capsys.readouterr().err


def test_unknown_run_id_maps_to_exit_1(tmp_path: Path, capsys) -> None:
    run_dir, receipt = _fabricated_run_dir(tmp_path)
    assert (
        evidence_package.export_package(
            target=tmp_path,
            run_id="does-not-exist",
            out_str=str(tmp_path / "out"),
        )
        == 1
    )
    assert "verification run not found" in capsys.readouterr().err


def test_export_json_output_sorted_schema(tmp_path: Path, capsys) -> None:
    run_dir, receipt = _fabricated_run_dir(tmp_path)
    out_dir = tmp_path / "exported-pkg"
    assert (
        evidence_package.export_package(
            target=tmp_path,
            run_id=receipt["run_id"],
            out_str=str(out_dir),
            json_output=True,
        )
        == 0
    )
    out = capsys.readouterr().out
    result = json.loads(out)
    assert result["schema"] == evidence_package.EXPORT_SCHEMA
    assert list(result.keys()) == sorted(result.keys())
    assert result["entries"] == 5


def test_verify_package_refuses_oversized_entry_bytes(tmp_path: Path, capsys) -> None:
    run_dir, receipt = _fabricated_run_dir(tmp_path)
    out_dir = tmp_path / "exported-pkg"
    evidence_package.export_package(
        target=tmp_path,
        run_id=receipt["run_id"],
        out_str=str(out_dir),
    )
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    manifest["entries"][0]["bytes"] = 64 * 1024 * 1024 + 1
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")

    assert evidence_package.verify_package(directory=out_dir, json_output=False) == 1
    assert "exceeds size limit" in capsys.readouterr().err


def test_verify_package_receipt_missing_reports_invalid_digest(tmp_path: Path, capsys) -> None:
    run_dir, receipt = _fabricated_run_dir(tmp_path)
    out_dir = tmp_path / "exported-pkg"
    evidence_package.export_package(
        target=tmp_path,
        run_id=receipt["run_id"],
        out_str=str(out_dir),
    )
    (out_dir / "receipt.json").unlink()

    capsys.readouterr()  # drain export output
    assert evidence_package.verify_package(directory=out_dir, json_output=True) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["receipt_digest"] == "invalid"
    assert result["ok"] is False
