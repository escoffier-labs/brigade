"""Tests for the versioned brigade.work-run archive schema (#487)."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest

from brigade import cli, run_checkpoint, work_run_archive
from brigade.work_run_archive import (
    WORK_RUN_ARCHIVE_SCHEMA,
    WORK_RUN_ARCHIVE_SCHEMA_VERSION,
    WorkRunArchiveError,
)


RUN_ID = "20260809-120000-abcdef12"


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _seed_run(run_dir: Path, *, with_checkpoint_body: bool = False) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    _write_json(
        run_dir / "run.json",
        {
            "schema": "brigade.run.v1",
            "schema_version": 1,
            "run_id": RUN_ID,
            "status": "ok",
            "started_at": "2026-08-09T12:00:00Z",
            "finished_at": "2026-08-09T12:01:00Z",
            "task": "demo task",
            "orchestrator": "orch",
            "dry_run": False,
            "read_only": True,
            "suspected_noop": False,
            "code_graph_brief": {},
            "drift_impact_brief": {},
            "evidence_brief": {},
            "brief_budget": {},
        },
    )
    _write_json(
        run_dir / "roster.json",
        {
            "schema": "brigade.roster_snapshot.v1",
            "schema_version": 1,
            "orchestrator": "orch",
            "agents": {},
        },
    )
    (run_dir / "final.txt").write_text("done\n", encoding="utf-8")
    if with_checkpoint_body:
        body = {
            "schema": "brigade.run.v1",
            "schema_version": 1,
            "status": "ok",
            "secret_marker": "private-body-must-not-export",
        }
        raw = json.dumps(body, indent=2, sort_keys=True).encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        cp_dir = run_dir / "events" / run_checkpoint.CHECKPOINT_DIR_NAME
        cp_dir.mkdir(parents=True, exist_ok=True)
        (cp_dir / f"{digest}.json").write_bytes(raw)
    return run_dir


def test_published_schema_artifact_matches_runtime_constants():
    schema_path = Path(__file__).parents[1] / "schemas" / "work-run.v1.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert schema["$id"].endswith("/work-run.v1.schema.json")
    assert schema["properties"]["schema"]["const"] == WORK_RUN_ARCHIVE_SCHEMA
    assert schema["properties"]["schema_version"]["const"] == WORK_RUN_ARCHIVE_SCHEMA_VERSION
    assert work_run_archive.schema_artifact_relative_path() == "schemas/work-run.v1.schema.json"


def test_export_import_round_trip_and_validate(tmp_path: Path):
    runs_dir = tmp_path / "runs"
    run_dir = _seed_run(runs_dir / RUN_ID)
    archive = tmp_path / "archive"

    exported = work_run_archive.export_run(run_dir, archive)
    assert exported["status"] == "exported"
    assert exported["schema"] == WORK_RUN_ARCHIVE_SCHEMA
    assert exported["schema_version"] == WORK_RUN_ARCHIVE_SCHEMA_VERSION
    assert (archive / "work-run.json").is_file()
    assert (archive / "payload" / "run.json").is_file()

    manifest = work_run_archive.validate_archive(archive)
    assert manifest["run_id"] == RUN_ID
    assert manifest["compatibility"]["resume_supported"] is False
    assert any(entry["path"] == "run.json" and entry["role"] == "receipt" for entry in manifest["files"])

    imported_runs = tmp_path / "imported-runs"
    result = work_run_archive.import_archive(archive, runs_dir=imported_runs)
    assert result["run_id"] == RUN_ID
    assert (imported_runs / RUN_ID / "run.json").is_file()
    assert (imported_runs / RUN_ID / "final.txt").read_text(encoding="utf-8") == "done\n"
    # Source tree unchanged.
    assert (run_dir / "run.json").is_file()


def test_export_strips_private_checkpoint_bodies(tmp_path: Path):
    run_dir = _seed_run(tmp_path / "runs" / RUN_ID, with_checkpoint_body=True)
    archive = tmp_path / "archive"
    work_run_archive.export_run(run_dir, archive)

    # Source still has the private body.
    source_cps = list((run_dir / "events" / run_checkpoint.CHECKPOINT_DIR_NAME).glob("*.json"))
    assert source_cps
    source_payload = json.loads(source_cps[0].read_text(encoding="utf-8"))
    assert source_payload.get("secret_marker") == "private-body-must-not-export"

    # Archive only carries the closed artifact reference.
    exported_cps = list((archive / "payload" / "events" / run_checkpoint.CHECKPOINT_DIR_NAME).glob("*.json"))
    assert len(exported_cps) == 1
    exported_payload = json.loads(exported_cps[0].read_text(encoding="utf-8"))
    assert run_checkpoint.is_checkpoint_artifact_reference(exported_payload)
    assert "secret_marker" not in exported_payload

    file_entry = next(
        entry
        for entry in json.loads((archive / "work-run.json").read_text(encoding="utf-8"))["files"]
        if entry["path"].startswith("events/recovery-checkpoints/")
    )
    assert file_entry["role"] == "checkpoint-reference"
    assert file_entry["privacy_class"] == "private"


def test_validate_manifest_refuses_unsupported_schema_version():
    payload = {
        "schema": WORK_RUN_ARCHIVE_SCHEMA,
        "schema_version": 99,
        "run_id": RUN_ID,
        "exported_at": "2026-08-09T12:00:00Z",
        "exporter_brigade_version": "0.26.0",
        "format": "directory",
        "payload_dir": "payload",
        "compatibility": work_run_archive.compatibility_defaults(journal_authority="none"),
        "files": [
            {
                "path": "run.json",
                "sha256": "a" * 64,
                "byte_size": 1,
                "role": "receipt",
            }
        ],
    }
    with pytest.raises(WorkRunArchiveError, match="unsupported archive schema_version"):
        work_run_archive.validate_manifest(payload)


def test_validate_archive_detects_digest_mismatch(tmp_path: Path):
    run_dir = _seed_run(tmp_path / "runs" / RUN_ID)
    archive = tmp_path / "archive"
    work_run_archive.export_run(run_dir, archive)
    (archive / "payload" / "final.txt").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(WorkRunArchiveError, match="sha256 mismatch"):
        work_run_archive.validate_archive(archive)


def test_validate_manifest_is_closed_to_unknown_keys():
    base = {
        "schema": WORK_RUN_ARCHIVE_SCHEMA,
        "schema_version": 1,
        "run_id": RUN_ID,
        "exported_at": "2026-08-09T12:00:00Z",
        "exporter_brigade_version": "0.26.0",
        "format": "directory",
        "payload_dir": "payload",
        "compatibility": work_run_archive.compatibility_defaults(journal_authority="none"),
        "files": [
            {
                "path": "run.json",
                "sha256": "b" * 64,
                "byte_size": 2,
                "role": "receipt",
            }
        ],
    }
    bad = deepcopy(base)
    bad["extra"] = True
    with pytest.raises(WorkRunArchiveError, match="unknown manifest field"):
        work_run_archive.validate_manifest(bad)


def test_cli_export_import_validate_archive(tmp_path: Path, capsys):
    runs_dir = tmp_path / "runs"
    _seed_run(runs_dir / RUN_ID)
    archive = tmp_path / "out-archive"
    imported = tmp_path / "imported"

    assert (
        cli.main(
            [
                "runs",
                "export",
                RUN_ID,
                "--cwd",
                str(tmp_path),
                "--runs-dir",
                str(runs_dir),
                "--output",
                str(archive),
                "--json",
            ]
        )
        == 0
    )
    exported = json.loads(capsys.readouterr().out)
    assert exported["run_id"] == RUN_ID

    assert cli.main(["runs", "validate-archive", str(archive), "--json"]) == 0
    validated = json.loads(capsys.readouterr().out)
    assert validated["status"] == "valid"

    assert (
        cli.main(
            [
                "runs",
                "import",
                str(archive),
                "--cwd",
                str(tmp_path),
                "--runs-dir",
                str(imported),
                "--json",
            ]
        )
        == 0
    )
    imported_payload = json.loads(capsys.readouterr().out)
    assert imported_payload["run_dir"] == str(imported / RUN_ID)
    assert imported_payload["resume_supported"] is False


def test_export_refuses_symlink_in_run_tree(tmp_path: Path):
    run_dir = _seed_run(tmp_path / "runs" / RUN_ID)
    (run_dir / "link.txt").symlink_to(run_dir / "final.txt")
    with pytest.raises(WorkRunArchiveError, match="symlink"):
        work_run_archive.export_run(run_dir, tmp_path / "archive")
