"""Tests for the versioned brigade.work-run archive schema (#487, #592)."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest

from brigade import cli, run_checkpoint, worker_events, work_run_archive
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


def test_validate_refuses_archive_root_symlink(tmp_path: Path):
    real_archive = tmp_path / "real-archive"
    work_run_archive.export_run(_seed_run(tmp_path / "runs" / RUN_ID), real_archive)
    archive_link = tmp_path / "archive-link"
    archive_link.symlink_to(real_archive)
    with pytest.raises(WorkRunArchiveError, match="symlink"):
        work_run_archive.validate_archive(archive_link)


def test_export_refuses_run_root_symlink(tmp_path: Path):
    real_run = _seed_run(tmp_path / "runs" / RUN_ID)
    run_link = tmp_path / "run-link"
    run_link.symlink_to(real_run)
    with pytest.raises(WorkRunArchiveError, match="symlink"):
        work_run_archive.export_run(run_link, tmp_path / "archive")


def test_force_export_refuses_destination_symlink_and_preserves_target(tmp_path: Path):
    run_dir = _seed_run(tmp_path / "runs" / RUN_ID)
    victim = tmp_path / "victim-dir"
    victim.mkdir()
    marker = victim / "keep-me.txt"
    marker.write_text("precious\n", encoding="utf-8")

    dest_link = tmp_path / "dest-link"
    dest_link.symlink_to(victim, target_is_directory=True)

    with pytest.raises(WorkRunArchiveError, match="symlink"):
        work_run_archive.export_run(run_dir, dest_link, force=True)

    assert marker.read_text(encoding="utf-8") == "precious\n"
    assert victim.is_dir()


def test_import_refuses_archive_root_symlink(tmp_path: Path):
    real_archive = tmp_path / "real-archive"
    work_run_archive.export_run(_seed_run(tmp_path / "runs" / RUN_ID), real_archive)
    archive_link = tmp_path / "archive-link"
    archive_link.symlink_to(real_archive)
    with pytest.raises(WorkRunArchiveError, match="symlink"):
        work_run_archive.import_archive(archive_link, runs_dir=tmp_path / "imported-runs")


def test_import_refuses_dangling_destination_run_symlink(tmp_path: Path):
    archive = tmp_path / "archive"
    work_run_archive.export_run(_seed_run(tmp_path / "runs" / RUN_ID), archive)
    runs_dir = tmp_path / "imported-runs"
    runs_dir.mkdir()
    (runs_dir / RUN_ID).symlink_to(tmp_path / "missing-target")
    with pytest.raises(WorkRunArchiveError, match="symlink"):
        work_run_archive.import_archive(archive, runs_dir=runs_dir)


# -- Issue #592: worker-event export/archive boundary -------------------------

_FIXTURES = Path(__file__).resolve().parents[1] / "src" / "brigade" / "fixtures"
_GOLDEN = json.loads((_FIXTURES / "worker-events-appserver.v1.golden.json").read_text(encoding="utf-8"))


def _golden_case(name: str) -> dict:
    for case in _GOLDEN["cases"]:
        if case["name"] == name:
            return case
    raise KeyError(name)


def _write_worker_stream(run_dir: Path, worker: str, events: list[dict]) -> Path:
    events_dir = run_dir / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    path = events_dir / f"{worker}.jsonl"
    path.write_text("".join(json.dumps(event, sort_keys=True) + "\n" for event in events), encoding="utf-8")
    return path


def test_export_projects_worker_stream_to_scrubbed_sidecar_not_public_support(tmp_path: Path):
    run_dir = _seed_run(tmp_path / "runs" / RUN_ID)
    raw_events = list(_GOLDEN["stream"]["raw_events"])
    raw_path = _write_worker_stream(run_dir, "coder", raw_events)
    raw_text = raw_path.read_text(encoding="utf-8")
    assert "SECRET" in raw_text or "sk-" in raw_text or "/home/" in raw_text or "Bearer" in raw_text

    archive = tmp_path / "archive"
    work_run_archive.export_run(run_dir, archive)

    # Source remains local-only raw / unclassified.
    assert raw_path.is_file()
    info = worker_events.inspect_stream_file(raw_path)
    assert info.status == worker_events.STATUS_UNCLASSIFIED
    assert info.artifact_class == worker_events.UNCLASSIFIED_ARTIFACT_CLASS
    local = worker_events.load_stream_for_consumer(
        raw_path,
        consumer="run_resume",
        policy=worker_events.POLICY_LOCAL_ONLY,
    )
    assert isinstance(local, list) and len(local) == len(raw_events)

    # Archive must not ship the raw NDJSON as public support data.
    assert not (archive / "payload" / "events" / "coder.jsonl").exists()
    scrubbed_path = archive / "payload" / "events" / "coder.scrubbed.json"
    assert scrubbed_path.is_file()
    scrubbed = json.loads(scrubbed_path.read_text(encoding="utf-8"))
    assert scrubbed["schema"] == worker_events.STREAM_SCHEMA
    assert scrubbed["media_type"] == worker_events.SCRUBBED_MEDIA_TYPE
    assert scrubbed["artifact_class"] == worker_events.SCRUBBED_ARTIFACT_CLASS
    assert scrubbed["event_count"] == len(raw_events)
    assert [event["method"] for event in scrubbed["events"]] == [event["method"] for event in raw_events]
    for raw, projected in zip(raw_events, scrubbed["events"], strict=True):
        assert projected["source_digest"] == worker_events.source_digest_for_event(raw)
        assert projected["schema"] == worker_events.SCHEMA
        assert not worker_events.scrubbed_projection_omits_sensitive_material(projected)

    blob = scrubbed_path.read_text(encoding="utf-8")
    assert "sk-" not in blob
    assert "Bearer " not in blob
    assert "/home/" not in blob
    assert "SECRET" not in blob

    entry = next(
        item
        for item in json.loads((archive / "work-run.json").read_text(encoding="utf-8"))["files"]
        if item["path"] == "events/coder.scrubbed.json"
    )
    assert entry["role"] == "artifact"
    assert entry["media_type"] == worker_events.SCRUBBED_MEDIA_TYPE
    assert entry["privacy_class"] == "redacted"
    assert entry["nested_schema"] == worker_events.STREAM_SCHEMA
    assert entry["nested_schema_version"] == worker_events.STREAM_SCHEMA_VERSION
    assert entry["sha256"] == hashlib.sha256(scrubbed_path.read_bytes()).hexdigest()

    with pytest.raises(WorkRunArchiveError, match="raw worker stream"):
        work_run_archive._classify_path("events/coder.jsonl", b"{}\n")


def test_export_refuses_unknown_method_with_bounded_diagnostic(tmp_path: Path):
    run_dir = _seed_run(tmp_path / "runs" / RUN_ID)
    _write_worker_stream(
        run_dir,
        "coder",
        [
            _golden_case("turn_started_public")["raw"],
            {
                "jsonrpc": "2.0",
                "method": "provider/undocumentedHook",
                "params": {"threadId": "thread-x", "secret": "sk-export-must-refuse-01234567"},
            },
        ],
    )
    with pytest.raises(WorkRunArchiveError) as excinfo:
        work_run_archive.export_run(run_dir, tmp_path / "archive")
    assert excinfo.value.category == "export-privacy"
    assert len(str(excinfo.value)) <= worker_events.MAX_DIAGNOSTIC_LEN
    assert "provider/undocumentedHook" in str(excinfo.value) or "unknown" in str(excinfo.value).lower()
    assert not (tmp_path / "archive").exists()
    # Source untouched.
    assert (run_dir / "events" / "coder.jsonl").is_file()


@pytest.mark.parametrize(
    "case_name,forbidden_snippets",
    [
        (
            "auto_declined_with_secrets",
            [
                "sk-live-EXAMPLESECRETVALUE99",
                "Bearer ",
                "Cookie",
                "OPENAI_API_KEY",
                "/home/example-user",
                "raw user prompt",
            ],
        ),
        (
            "turn_completed_with_provider_error",
            ["provider dump", "sk-live-EXAMPLESECRETVALUE99", "Bearer "],
        ),
        (
            "item_completed_agent_message_private",
            ["sk-live-EXAMPLESECRETVALUE99", "Secret plan", "exfiltrate"],
        ),
        (
            "item_completed_commandExecution_adversarial",
            [
                "/home/example-user",
                "sk-live-EXAMPLESECRETVALUE99",
                "Bearer ",
            ],
        ),
    ],
)
def test_export_adversarial_cases_omit_private_tokens_paths_and_errors(
    tmp_path: Path,
    case_name: str,
    forbidden_snippets: list[str],
):
    run_dir = _seed_run(tmp_path / "runs" / RUN_ID)
    case = _golden_case(case_name)
    _write_worker_stream(run_dir, "worker-a", [case["raw"]])
    archive = tmp_path / "archive"
    work_run_archive.export_run(run_dir, archive)
    scrubbed_path = archive / "payload" / "events" / "worker-a.scrubbed.json"
    blob = scrubbed_path.read_text(encoding="utf-8")
    for snippet in forbidden_snippets:
        assert snippet not in blob, snippet
    scrubbed = json.loads(blob)
    assert scrubbed["events"][0]["source_digest"] == case["source_digest"]
    assert scrubbed["events"][0]["method"] == case["raw"]["method"]


def test_export_refuses_unknown_field_and_cross_method_contamination(tmp_path: Path):
    run_dir = _seed_run(tmp_path / "runs" / RUN_ID)
    started = deepcopy(_golden_case("turn_started_public")["raw"])
    started["params"]["undocumentedLeak"] = "sk-must-not-export-aaaaaaaa"
    other = {
        "jsonrpc": "2.0",
        "method": "item/fileChange/requestApproval#auto-declined",
        "params": {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "itemId": "file-1",
            "availableDecisions": ["accept"],
            "command": "cat /home/demo/.ssh/id_rsa",
        },
    }
    _write_worker_stream(run_dir, "coder", [started, other])
    with pytest.raises(WorkRunArchiveError) as excinfo:
        work_run_archive.export_run(run_dir, tmp_path / "archive")
    assert excinfo.value.category == "export-privacy"
    assert len(str(excinfo.value)) <= worker_events.MAX_DIAGNOSTIC_LEN
    assert not (tmp_path / "archive").exists()


def test_validate_archive_refuses_raw_worker_stream_smuggled_as_public_support(tmp_path: Path):
    run_dir = _seed_run(tmp_path / "runs" / RUN_ID)
    archive = tmp_path / "archive"
    work_run_archive.export_run(run_dir, archive)

    smuggled = archive / "payload" / "events" / "coder.jsonl"
    smuggled.parent.mkdir(parents=True, exist_ok=True)
    smuggled.write_text(
        json.dumps(_golden_case("auto_declined_with_secrets")["raw"], sort_keys=True) + "\n",
        encoding="utf-8",
    )
    # Rebuild manifest as a naive exporter that classifies *.jsonl as public support.
    raw = smuggled.read_bytes()
    manifest = json.loads((archive / "work-run.json").read_text(encoding="utf-8"))
    manifest["files"].append(
        {
            "path": "events/coder.jsonl",
            "sha256": hashlib.sha256(raw).hexdigest(),
            "byte_size": len(raw),
            "role": "support",
            "media_type": "application/x-ndjson",
            "privacy_class": "public",
        }
    )
    manifest["files"].sort(key=lambda row: row["path"])
    (archive / "work-run.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(WorkRunArchiveError) as excinfo:
        work_run_archive.validate_archive(archive)
    assert excinfo.value.category == "export-privacy"
    assert "raw worker stream" in str(excinfo.value)


def test_validate_archive_refuses_misclassified_scrubbed_sidecar(tmp_path: Path):
    run_dir = _seed_run(tmp_path / "runs" / RUN_ID)
    _write_worker_stream(run_dir, "coder", [_golden_case("turn_started_public")["raw"]])
    archive = tmp_path / "archive"
    work_run_archive.export_run(run_dir, archive)

    manifest = json.loads((archive / "work-run.json").read_text(encoding="utf-8"))
    for entry in manifest["files"]:
        if entry["path"] == "events/coder.scrubbed.json":
            entry["privacy_class"] = "public"
            entry["role"] = "support"
            entry["media_type"] = "application/json"
    (archive / "work-run.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(WorkRunArchiveError) as excinfo:
        work_run_archive.validate_archive(archive)
    assert excinfo.value.category == "export-privacy"


def test_imported_scrubbed_sidecar_is_admissible_scrubbed_only(tmp_path: Path):
    run_dir = _seed_run(tmp_path / "runs" / RUN_ID)
    _write_worker_stream(run_dir, "coder", list(_GOLDEN["stream"]["raw_events"]))
    archive = tmp_path / "archive"
    work_run_archive.export_run(run_dir, archive)
    imported = work_run_archive.import_archive(archive, runs_dir=tmp_path / "imported-runs")
    scrubbed = Path(imported["run_dir"]) / "events" / "coder.scrubbed.json"
    loaded = worker_events.load_stream_for_consumer(scrubbed, consumer="run_audit")
    assert loaded["artifact_class"] == worker_events.SCRUBBED_ARTIFACT_CLASS
    assert loaded["event_count"] == len(_GOLDEN["stream"]["raw_events"])
    with pytest.raises(worker_events.WorkerEventPolicyError):
        worker_events.load_stream_for_consumer(
            run_dir / "events" / "coder.jsonl",
            consumer="run_audit",
        )
