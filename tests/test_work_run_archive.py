"""Tests for the versioned brigade.work-run archive schema (#487, #592)."""

from __future__ import annotations

import hashlib
import json
import shutil
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


def test_export_strips_crashed_checkpoint_temp(tmp_path: Path):
    """#646/#654: a crashed ``.checkpoint.*.tmp`` holding private bytes must not
    reach the archive payload, and the source run tree must keep it."""
    run_dir = _seed_run(tmp_path / "runs" / RUN_ID, with_checkpoint_body=True)
    cp_dir = run_dir / "events" / run_checkpoint.CHECKPOINT_DIR_NAME
    crashed = cp_dir / ".checkpoint.xyz789.tmp"
    crashed.write_bytes(b'{"schema": "brigade.run.v1", "secret_marker": "crashed-temp-must-not-export')
    archive = tmp_path / "archive"
    work_run_archive.export_run(run_dir, archive)

    assert crashed.exists()
    payload_cp = archive / "payload" / "events" / run_checkpoint.CHECKPOINT_DIR_NAME
    assert not (payload_cp / crashed.name).exists()
    assert not list(payload_cp.glob(".checkpoint.*.tmp"))
    archive_bytes = b"".join(p.read_bytes() for p in archive.rglob("*") if p.is_file())
    assert b"crashed-temp-must-not-export" not in archive_bytes
    run_checkpoint.assert_export_tree_has_no_checkpoint_bodies(archive / "payload")


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


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("relationship", ["same", "ancestor", "descendant"])
def test_export_refuses_overlapping_trees_without_mutation(tmp_path: Path, force: bool, relationship: str, capsys):
    run_dir = _seed_run(tmp_path / "runs" / RUN_ID)
    sibling = _seed_run(tmp_path / "runs" / "20260809-120001-abcdef12")
    destination = {
        "same": run_dir,
        "ancestor": run_dir.parent,
        "descendant": run_dir / "new" / "archive",
    }[relationship]
    before = {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    args = ["runs", "export", str(run_dir), "--output", str(destination), "--json"]
    if force:
        args.append("--force")

    assert cli.main(args) == 2
    assert "directories overlap" in capsys.readouterr().err
    assert {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before
    assert (sibling / "run.json").is_file()
    assert not (run_dir / "new").exists()
    assert not list(tmp_path.rglob("*.staging-*"))


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("relationship", ["same", "ancestor", "descendant"])
def test_import_refuses_overlapping_trees_without_mutation(tmp_path: Path, force: bool, relationship: str, capsys):
    run_dir = _seed_run(tmp_path / "source" / RUN_ID)
    destination = tmp_path / "imported" / RUN_ID
    archive = {
        "same": destination,
        "ancestor": destination / "saved-archive",
        "descendant": tmp_path / "archive",
    }[relationship]
    work_run_archive.export_run(run_dir, archive)
    runs_dir = archive / "new" / "runs" if relationship == "descendant" else destination.parent
    before = {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    args = ["runs", "import", str(archive), "--runs-dir", str(runs_dir), "--json"]
    if force:
        args.append("--force")

    assert cli.main(args) == 2
    assert "directories overlap" in capsys.readouterr().err
    assert {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before
    assert not (archive / "new").exists()
    assert not list(tmp_path.rglob("*.import-*"))
    work_run_archive.validate_archive(archive)


@pytest.mark.parametrize("operation", ["export", "import"])
def test_archive_overlap_through_symlinked_parent_is_refused(tmp_path: Path, operation: str):
    run_dir = _seed_run(tmp_path / "runs" / RUN_ID)
    alias = tmp_path / "alias"
    alias.symlink_to(run_dir.parent, target_is_directory=True)
    if operation == "export":
        with pytest.raises(WorkRunArchiveError, match="directories overlap"):
            work_run_archive.export_run(run_dir, alias / RUN_ID, force=True)
        assert (run_dir / "final.txt").read_text(encoding="utf-8") == "done\n"
    else:
        archive = run_dir.parent / "imports" / RUN_ID
        work_run_archive.export_run(run_dir, archive)
        with pytest.raises(WorkRunArchiveError, match="directories overlap"):
            work_run_archive.import_archive(archive, runs_dir=alias / "imports", force=True)
        work_run_archive.validate_archive(archive)


def test_force_archive_replacement_allows_disjoint_sibling_trees(tmp_path: Path):
    run_dir = _seed_run(tmp_path / "runs" / RUN_ID)
    archive = run_dir.with_name(f"{RUN_ID}-archive")
    work_run_archive.export_run(run_dir, archive)
    work_run_archive.export_run(run_dir, archive, force=True)
    runs_dir = tmp_path / "imported"
    work_run_archive.import_archive(archive, runs_dir=runs_dir)
    work_run_archive.import_archive(archive, runs_dir=runs_dir, force=True)
    assert (runs_dir / RUN_ID / "final.txt").read_text(encoding="utf-8") == "done\n"
    assert (run_dir / "final.txt").read_text(encoding="utf-8") == "done\n"
    work_run_archive.validate_archive(archive)


@pytest.mark.parametrize("operation", ["export", "import"])
@pytest.mark.parametrize("relationship", ["same", "ancestor", "descendant"])
def test_archive_overlap_with_case_preserving_resolution_is_refused(
    tmp_path: Path, monkeypatch, operation: str, relationship: str
):
    real_root = tmp_path / "real"
    real_root.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real_root, target_is_directory=True)
    if operation == "export":
        source = _seed_run(real_root / "runs" / RUN_ID)
        input_path = {
            "same": alias / "runs" / RUN_ID,
            "ancestor": alias / "runs",
            "descendant": alias / "runs" / RUN_ID / "new" / "archive",
        }[relationship]
    else:
        run_dir = _seed_run(tmp_path / "source" / RUN_ID)
        source = {
            "same": real_root / "runs" / RUN_ID,
            "ancestor": real_root / "runs" / RUN_ID / "saved-archive",
            "descendant": real_root / "archive",
        }[relationship]
        work_run_archive.export_run(run_dir, source)
        input_path = alias / "archive" / "new" / "runs" if relationship == "descendant" else alias / "runs"
    before = {path.relative_to(real_root): path.read_bytes() for path in real_root.rglob("*") if path.is_file()}
    original_resolve = Path.resolve

    def preserve_alias(path: Path, *args, **kwargs):
        # Simulate case-preserving POSIX resolve with a real directory alias;
        # stat() still follows the alias to the actual filesystem identity.
        return path.absolute() if path == input_path else original_resolve(path, *args, **kwargs)

    def unexpected_copy(*args, **kwargs):
        pytest.fail("overlapping paths reached the copy step")

    monkeypatch.setattr(Path, "resolve", preserve_alias)
    monkeypatch.setattr(work_run_archive, "_copy_run_tree_refuse_special", unexpected_copy)
    with pytest.raises(WorkRunArchiveError, match="directories overlap"):
        if operation == "export":
            work_run_archive.export_run(source, input_path, force=True)
        else:
            work_run_archive.import_archive(source, runs_dir=input_path, force=True)
    assert {path.relative_to(real_root): path.read_bytes() for path in real_root.rglob("*") if path.is_file()} == before
    assert not list(real_root.rglob("*.staging-*"))
    assert not list(real_root.rglob("*.import-*"))
    assert not list(real_root.rglob("new"))


@pytest.mark.parametrize("operation", ["export", "import"])
def test_archive_path_stat_error_refuses_before_replacement(tmp_path: Path, monkeypatch, operation: str):
    run_dir = _seed_run(tmp_path / "source" / RUN_ID)
    archive = tmp_path / "archive"
    work_run_archive.export_run(run_dir, archive)
    destination = archive if operation == "export" else _seed_run(tmp_path / "runs" / RUN_ID)
    before = {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    original_stat = Path.stat

    def denied_stat(path: Path, *args, **kwargs):
        if path == destination:
            raise PermissionError("cannot inspect test destination")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", denied_stat)
    with pytest.raises(WorkRunArchiveError, match="cannot inspect source and destination") as exc:
        if operation == "export":
            work_run_archive.export_run(run_dir, destination, force=True)
        else:
            work_run_archive.import_archive(archive, runs_dir=destination.parent, force=True)
    assert exc.value.category == "io"
    monkeypatch.setattr(Path, "stat", original_stat)
    assert {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before


# -- Issue #592: worker-event export/archive boundary -------------------------

_FIXTURES = Path(__file__).resolve().parents[1] / "src" / "brigade" / "fixtures"
_GOLDEN = json.loads((_FIXTURES / "worker-events-appserver.v1.golden.json").read_text(encoding="utf-8"))


def _golden_case(name: str) -> dict:
    for case in _GOLDEN["cases"]:
        if case["name"] == name:
            return case
    raise KeyError(name)


def _write_worker_stream(
    run_dir: Path,
    worker: str,
    events: list[dict],
    *,
    nested_under: str | None = None,
) -> Path:
    events_dir = run_dir / "events"
    if nested_under is not None:
        events_dir = events_dir / nested_under
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
                "sk-live-EXAMPLESECRETVALUE99",  # content-guard: allow bare-token-prefix
                "Bearer ",
                "Cookie",
                "OPENAI_API_KEY",
                "/home/example-user",
                "raw user prompt",
            ],
        ),
        (
            "turn_completed_with_provider_error",
            ["provider dump", "sk-live-EXAMPLESECRETVALUE99", "Bearer "],  # content-guard: allow bare-token-prefix
        ),
        (
            "item_completed_agent_message_private",
            ["sk-live-EXAMPLESECRETVALUE99", "Secret plan", "exfiltrate"],  # content-guard: allow bare-token-prefix
        ),
        (
            "item_completed_commandExecution_adversarial",
            [
                "/home/example-user",
                "sk-live-EXAMPLESECRETVALUE99",  # content-guard: allow bare-token-prefix
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


def test_export_discovers_nested_worker_stream_and_writes_scrubbed_sidecar(tmp_path: Path):
    run_dir = _seed_run(tmp_path / "runs" / RUN_ID)
    case = _golden_case("auto_declined_with_secrets")
    raw_path = _write_worker_stream(run_dir, "coder", [case["raw"]], nested_under="nested")
    raw_text = raw_path.read_text(encoding="utf-8")
    assert "Bearer " in raw_text
    assert "/home/example-user" in raw_text

    archive = tmp_path / "archive"
    work_run_archive.export_run(run_dir, archive)

    # Source nested raw remains local-only / unclassified.
    assert raw_path.is_file()
    assert worker_events.inspect_stream_file(raw_path).status == worker_events.STATUS_UNCLASSIFIED

    assert not (archive / "payload" / "events" / "nested" / "coder.jsonl").exists()
    scrubbed_path = archive / "payload" / "events" / "nested" / "coder.scrubbed.json"
    assert scrubbed_path.is_file()
    blob = scrubbed_path.read_text(encoding="utf-8")
    for snippet in (
        "Bearer ",
        "sk-live-EXAMPLESECRETVALUE99",  # content-guard: allow bare-token-prefix
        "/home/example-user",
        "OPENAI_API_KEY",
        "raw user prompt",
    ):
        assert snippet not in blob, snippet
    scrubbed = json.loads(blob)
    assert scrubbed["events"][0]["source_digest"] == case["source_digest"]
    assert scrubbed["events"][0]["method"] == case["raw"]["method"]

    entry = next(
        item
        for item in json.loads((archive / "work-run.json").read_text(encoding="utf-8"))["files"]
        if item["path"] == "events/nested/coder.scrubbed.json"
    )
    assert entry["role"] == "artifact"
    assert entry["media_type"] == worker_events.SCRUBBED_MEDIA_TYPE
    assert entry["privacy_class"] == "redacted"
    assert entry["nested_schema"] == worker_events.STREAM_SCHEMA


def test_export_refuses_nested_unknown_method_with_secret_marker(tmp_path: Path):
    run_dir = _seed_run(tmp_path / "runs" / RUN_ID)
    _write_worker_stream(
        run_dir,
        "coder",
        [
            {
                "jsonrpc": "2.0",
                "method": "provider/undocumentedHook",
                "params": {
                    "Authorization": "Bearer sk-live-EXAMPLESECRETVALUE99",
                    "cwd": "/home/example-user/project",
                },
            }
        ],
        nested_under="nested",
    )
    with pytest.raises(WorkRunArchiveError) as excinfo:
        work_run_archive.export_run(run_dir, tmp_path / "archive")
    assert excinfo.value.category == "export-privacy"
    message = str(excinfo.value)
    assert len(message) <= worker_events.MAX_DIAGNOSTIC_LEN
    assert "events/nested/coder.jsonl" in message or "nested/coder.jsonl" in message
    assert "unknown" in message.lower() or "provider/undocumentedHook" in message
    assert not (tmp_path / "archive").exists()
    assert (run_dir / "events" / "nested" / "coder.jsonl").is_file()


def test_validate_and_import_refuse_nested_raw_worker_stream_smuggling(tmp_path: Path):
    run_dir = _seed_run(tmp_path / "runs" / RUN_ID)
    archive = tmp_path / "archive"
    work_run_archive.export_run(run_dir, archive)

    smuggled = archive / "payload" / "events" / "nested" / "coder.jsonl"
    smuggled.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "jsonrpc": "2.0",
        "method": "provider/undocumentedHook",
        "params": {
            "Authorization": "Bearer sk-live-EXAMPLESECRETVALUE99",
            "cwd": "/home/example-user/project",
            "prompt": "private nested content",
        },
    }
    smuggled.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    raw = smuggled.read_bytes()
    manifest = json.loads((archive / "work-run.json").read_text(encoding="utf-8"))
    manifest["files"].append(
        {
            "path": "events/nested/coder.jsonl",
            "sha256": hashlib.sha256(raw).hexdigest(),
            "byte_size": len(raw),
            "role": "support",
            "media_type": "application/x-ndjson",
            "privacy_class": "public",
        }
    )
    manifest["files"].sort(key=lambda row: row["path"])
    (archive / "work-run.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(WorkRunArchiveError) as validate_exc:
        work_run_archive.validate_archive(archive)
    assert validate_exc.value.category == "export-privacy"
    assert "raw worker stream" in str(validate_exc.value)
    assert "nested" in str(validate_exc.value)

    with pytest.raises(WorkRunArchiveError) as import_exc:
        work_run_archive.import_archive(archive, runs_dir=tmp_path / "imported-runs")
    assert import_exc.value.category == "export-privacy"
    assert "raw worker stream" in str(import_exc.value)


def test_list_worker_event_streams_is_recursive_under_events(tmp_path: Path):
    events = tmp_path / "events"
    (events / "nested" / "deep").mkdir(parents=True)
    (events / "lifecycle.jsonl").write_text("{}\n", encoding="utf-8")
    (events / "coder.jsonl").write_text("{}\n", encoding="utf-8")
    (events / "nested" / "deep" / "helper.jsonl").write_text("{}\n", encoding="utf-8")
    found = [path.relative_to(events).as_posix() for path in worker_events.list_worker_event_streams(events)]
    assert found == ["coder.jsonl", "nested/deep/helper.jsonl"]
    assert worker_events.is_raw_worker_event_stream_rel("events/nested/deep/helper.jsonl")
    assert worker_events.is_scrubbed_worker_event_stream_rel("events/nested/deep/helper.scrubbed.json")
    assert not worker_events.is_raw_worker_event_stream_rel("events/lifecycle.jsonl")


def _valid_lifecycle_line(*, run_id: str = RUN_ID) -> bytes:
    from brigade import run_events

    envelope = run_events.build_event(
        run_id=run_id,
        sequence=1,
        event_type="run.created",
        payload={"status": "started"},
        idempotency_key="create-1",
        recorded_at="2026-08-09T12:00:00.000000Z",
        previous_digest=None,
    )
    return run_events.canonical_bytes(envelope) + b"\n"


def _smuggle_lifecycle_worker_envelope(archive: Path) -> bytes:
    smuggle = {
        "jsonrpc": "2.0",
        "method": "turn/started",
        "params": {
            "Authorization": "Bearer sk-live-EXAMPLESECRETVALUE99",
            "prompt": "private lifecycle smuggle",
            "cwd": "/home/example-user/project",
        },
    }
    life = archive / "payload" / "events" / "lifecycle.jsonl"
    life.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(smuggle, sort_keys=True) + "\n").encode("utf-8")
    life.write_bytes(raw)
    manifest = json.loads((archive / "work-run.json").read_text(encoding="utf-8"))
    manifest["files"] = [entry for entry in manifest["files"] if entry["path"] != "events/lifecycle.jsonl"]
    manifest["files"].append(
        {
            "path": "events/lifecycle.jsonl",
            "sha256": hashlib.sha256(raw).hexdigest(),
            "byte_size": len(raw),
            "role": "journal",
            "media_type": "application/x-ndjson",
            "privacy_class": "public",
            "nested_schema": "brigade.run_event.v1",
            "nested_schema_version": 1,
        }
    )
    manifest["files"].sort(key=lambda row: row["path"])
    (archive / "work-run.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return raw


def test_validate_archive_refuses_lifecycle_jsonl_worker_envelope_smuggling(tmp_path: Path):
    run_dir = _seed_run(tmp_path / "runs" / RUN_ID)
    archive = tmp_path / "archive"
    work_run_archive.export_run(run_dir, archive)
    raw = _smuggle_lifecycle_worker_envelope(archive)
    assert b"Bearer sk-live-EXAMPLESECRETVALUE99" in raw
    assert b"private lifecycle smuggle" in raw

    with pytest.raises(WorkRunArchiveError) as excinfo:
        work_run_archive.validate_archive(archive)
    assert excinfo.value.category == "export-privacy"
    message = str(excinfo.value)
    assert "lifecycle.jsonl" in message
    assert "worker" in message.lower() or "json-rpc" in message.lower() or "notification" in message.lower()
    assert len(message) <= worker_events.MAX_DIAGNOSTIC_LEN


def test_import_archive_refuses_lifecycle_jsonl_worker_envelope_smuggling(tmp_path: Path):
    run_dir = _seed_run(tmp_path / "runs" / RUN_ID)
    archive = tmp_path / "archive"
    work_run_archive.export_run(run_dir, archive)
    _smuggle_lifecycle_worker_envelope(archive)

    with pytest.raises(WorkRunArchiveError) as excinfo:
        work_run_archive.import_archive(archive, runs_dir=tmp_path / "imported-runs")
    assert excinfo.value.category == "export-privacy"
    assert "lifecycle.jsonl" in str(excinfo.value)
    assert not (tmp_path / "imported-runs" / RUN_ID).exists()


def test_validate_and_import_accept_authentic_lifecycle_journal(tmp_path: Path):
    run_dir = _seed_run(tmp_path / "runs" / RUN_ID)
    life = run_dir / "events" / "lifecycle.jsonl"
    life.parent.mkdir(parents=True, exist_ok=True)
    life.write_bytes(_valid_lifecycle_line())

    archive = tmp_path / "archive"
    work_run_archive.export_run(run_dir, archive)
    entry = next(
        item
        for item in json.loads((archive / "work-run.json").read_text(encoding="utf-8"))["files"]
        if item["path"] == "events/lifecycle.jsonl"
    )
    assert entry["role"] == "journal"
    assert entry["privacy_class"] == "public"
    assert entry["nested_schema"] == "brigade.run_event.v1"

    manifest = work_run_archive.validate_archive(archive)
    assert any(item["path"] == "events/lifecycle.jsonl" for item in manifest["files"])

    imported = work_run_archive.import_archive(archive, runs_dir=tmp_path / "imported-runs")
    imported_life = Path(imported["run_dir"]) / "events" / "lifecycle.jsonl"
    assert imported_life.is_file()
    assert b'"schema":"brigade.run_event.v1"' in imported_life.read_bytes()
    assert b"Bearer " not in imported_life.read_bytes()


def test_export_refuses_source_lifecycle_worker_envelope(tmp_path: Path):
    run_dir = _seed_run(tmp_path / "runs" / RUN_ID)
    life = run_dir / "events" / "lifecycle.jsonl"
    life.parent.mkdir(parents=True, exist_ok=True)
    life.write_text(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "item/completed",
                "params": {
                    "Authorization": "Bearer sk-live-EXAMPLESECRETVALUE99",
                    "prompt": "source lifecycle smuggle",
                },
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(WorkRunArchiveError) as excinfo:
        work_run_archive.export_run(run_dir, tmp_path / "archive")
    assert excinfo.value.category == "export-privacy"
    assert "lifecycle.jsonl" in str(excinfo.value)
    assert not (tmp_path / "archive").exists()
    assert life.is_file()


def _archive_bytes(root: Path) -> dict[Path, bytes]:
    return {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def _replacement_case(tmp_path: Path):
    original = _seed_run(tmp_path / "original" / RUN_ID)
    archive = tmp_path / "archive"
    work_run_archive.export_run(original, archive)
    before = _archive_bytes(archive)
    replacement = _seed_run(tmp_path / "replacement" / RUN_ID)
    (replacement / "final.txt").write_text("replacement artifact\n", encoding="utf-8")
    return original, replacement, archive, before


def _assert_no_export_temps(archive: Path) -> None:
    assert not list(archive.parent.glob(f".{archive.name}.staging-*"))
    assert not list(archive.parent.glob(f".{archive.name}.backup-*"))


def _inject_promotion_rename(monkeypatch, rename) -> None:
    real_promote = work_run_archive._promote_export

    def promote(staging, destination, *, force):
        # dirfd capability checks compare os.rename's identity before staging
        # is ready, so inject the failure only within the promotion operation.
        with monkeypatch.context() as scoped:
            scoped.setattr(work_run_archive.os, "rename", rename)
            return real_promote(staging, destination, force=force)

    monkeypatch.setattr(work_run_archive, "_promote_export", promote)


@pytest.mark.parametrize("invalid", ["run-id", "symlink"])
def test_cli_refused_replacement_preserves_only_remaining_archive(tmp_path: Path, capsys, invalid: str):
    original, replacement, archive, before = _replacement_case(tmp_path)
    shutil.rmtree(original)
    if invalid == "run-id":
        replacement = replacement.rename(replacement.with_name("unnamed-run"))
        (replacement / "run.json").write_text('{"status": "ok"}\n', encoding="utf-8")
    else:
        (replacement / "link.txt").symlink_to(replacement / "final.txt")

    assert cli.main(["runs", "export", str(replacement), "--output", str(archive), "--force", "--json"]) == (
        1 if invalid == "run-id" else 2
    )
    assert "error:" in capsys.readouterr().err
    assert _archive_bytes(archive) == before
    work_run_archive.validate_archive(archive)
    _assert_no_export_temps(archive)


@pytest.mark.parametrize(
    "phase",
    [
        "copy",
        "manifest",
        "validation",
        "backup-create",
        "backup-move",
        "promotion",
        "backup-interrupt",
        "promotion-interrupt",
    ],
)
def test_export_replacement_fault_preserves_previous_archive(tmp_path: Path, monkeypatch, phase: str):
    _, replacement, archive, before = _replacement_case(tmp_path)
    real_rename = work_run_archive.os.rename
    real_validate = work_run_archive.validate_archive

    def fail_build(*args, **kwargs):
        if phase == "copy":
            raise OSError("copy injected failure")
        if phase == "backup-create":
            raise OSError("backup-create injected failure")
        raise WorkRunArchiveError("manifest injected failure")

    def validate(path):
        if Path(path).name.startswith(".archive.staging-"):
            raise WorkRunArchiveError("validation injected failure", category="integrity")
        return real_validate(path)

    def rename(source, destination):
        source = Path(source)
        if source == archive and phase == "backup-move":
            raise OSError("backup-move injected failure")
        if source == archive and phase == "backup-interrupt":
            real_rename(source, destination)
            raise KeyboardInterrupt("backup-interrupt injected failure")
        if source.name.startswith(".archive.staging-"):
            if phase == "promotion":
                raise OSError("promotion injected failure")
            if phase == "promotion-interrupt":
                raise KeyboardInterrupt("promotion-interrupt injected failure")
        return real_rename(source, destination)

    if phase == "copy":
        monkeypatch.setattr(work_run_archive, "_copy_run_tree_refuse_special", fail_build)
    elif phase == "manifest":
        monkeypatch.setattr(work_run_archive, "build_manifest_for_payload", fail_build)
    elif phase == "validation":
        monkeypatch.setattr(work_run_archive, "validate_archive", validate)
    elif phase == "backup-create":
        monkeypatch.setattr(work_run_archive.tempfile, "mkdtemp", fail_build)
    else:
        _inject_promotion_rename(monkeypatch, rename)
    expected = KeyboardInterrupt if phase.endswith("interrupt") else OSError if phase == "copy" else WorkRunArchiveError
    with pytest.raises(expected, match="injected failure"):
        work_run_archive.export_run(replacement, archive, force=True)
    assert _archive_bytes(archive) == before
    real_validate(archive)
    _assert_no_export_temps(archive)


def test_cli_promotion_failure_restores_previous_archive(tmp_path: Path, monkeypatch, capsys):
    _, replacement, archive, before = _replacement_case(tmp_path)
    real_rename = work_run_archive.os.rename

    def rename(source, destination):
        if Path(source).name.startswith(".archive.staging-"):
            raise OSError("promotion injected failure")
        return real_rename(source, destination)

    _inject_promotion_rename(monkeypatch, rename)
    assert cli.main(["runs", "export", str(replacement), "--output", str(archive), "--force", "--json"]) == 2
    assert "promotion injected failure" in capsys.readouterr().err
    assert _archive_bytes(archive) == before
    _assert_no_export_temps(archive)


def test_empty_backup_cleanup_does_not_mask_move_failure(tmp_path: Path, monkeypatch):
    _, replacement, archive, before = _replacement_case(tmp_path)
    real_rename = work_run_archive.os.rename
    real_rmdir = Path.rmdir

    def rename(source, destination):
        if Path(source) == archive:
            raise OSError("backup-move injected failure")
        return real_rename(source, destination)

    def rmdir(path):
        if path.name.startswith(".archive.backup-"):
            raise OSError("empty-container injected failure")
        return real_rmdir(path)

    _inject_promotion_rename(monkeypatch, rename)
    monkeypatch.setattr(Path, "rmdir", rmdir)
    with pytest.raises(WorkRunArchiveError, match="backup-move injected failure") as exc:
        work_run_archive.export_run(replacement, archive, force=True)
    assert "empty-container" not in str(exc.value)
    assert _archive_bytes(archive) == before
    containers = list(tmp_path.glob(".archive.backup-*"))
    assert len(containers) == 1 and not list(containers[0].iterdir())
    assert not list(tmp_path.glob(".archive.staging-*"))


@pytest.mark.parametrize("blocker", ["rename", "directory", "symlink", "dangling"])
def test_replacement_failed_restore_keeps_backup(tmp_path: Path, monkeypatch, blocker: str):
    _, replacement, archive, before = _replacement_case(tmp_path)
    victim = tmp_path / "other-archive"
    victim.mkdir()
    (victim / "keep.txt").write_text("other writer\n", encoding="utf-8")
    real_rename = work_run_archive.os.rename

    def rename(source, destination):
        source = Path(source)
        if source.name.startswith(".archive.staging-"):
            if blocker == "directory":
                archive.mkdir()
                (archive / "keep.txt").write_text("other writer\n", encoding="utf-8")
            elif blocker in {"symlink", "dangling"}:
                archive.symlink_to(victim if blocker == "symlink" else tmp_path / "missing", target_is_directory=True)
            raise OSError("promotion injected failure")
        if source.parent.name.startswith(".archive.backup-"):
            raise OSError("restore injected failure")
        return real_rename(source, destination)

    _inject_promotion_rename(monkeypatch, rename)
    with pytest.raises(WorkRunArchiveError, match="restoration failed") as exc:
        work_run_archive.export_run(replacement, archive, force=True)
    backups = list(tmp_path.glob(".archive.backup-*/archive"))
    assert len(backups) == 1
    assert str(backups[0]) in str(exc.value)
    assert "promotion injected failure" in str(exc.value)
    assert _archive_bytes(backups[0]) == before
    work_run_archive.validate_archive(backups[0])
    assert (victim / "keep.txt").read_text(encoding="utf-8") == "other writer\n"
    if blocker == "directory":
        assert (archive / "keep.txt").read_text(encoding="utf-8") == "other writer\n"
    elif blocker in {"symlink", "dangling"}:
        assert archive.is_symlink()
    assert not list(tmp_path.glob(".archive.staging-*"))


def test_successful_replacement_cleanup_failure_reports_installed_archive(tmp_path: Path, monkeypatch):
    _, replacement, archive, before = _replacement_case(tmp_path)
    real_rmtree = work_run_archive.shutil.rmtree

    def rmtree(path, *args, **kwargs):
        if Path(path).name.startswith(".archive.backup-"):
            raise OSError("cleanup injected failure")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(work_run_archive.shutil, "rmtree", rmtree)
    with pytest.raises(WorkRunArchiveError, match="archive installed") as exc:
        work_run_archive.export_run(replacement, archive, force=True)
    backups = list(tmp_path.glob(".archive.backup-*/archive"))
    assert len(backups) == 1
    assert str(backups[0]) in str(exc.value)
    assert _archive_bytes(backups[0]) == before
    work_run_archive.validate_archive(archive)
    assert (archive / "payload" / "final.txt").read_text(encoding="utf-8") == "replacement artifact\n"


@pytest.mark.parametrize("kind", ["file", "directory", "symlink", "dangling"])
def test_export_rechecks_destination_before_promotion(tmp_path: Path, monkeypatch, kind: str):
    replacement = _seed_run(tmp_path / "runs" / RUN_ID)
    archive = tmp_path / "archive"
    victim = tmp_path / "victim"
    victim.mkdir()
    marker = victim / "keep.txt"
    marker.write_text("untouched\n", encoding="utf-8")
    real_validate = work_run_archive.validate_archive

    def validate(path):
        result = real_validate(path)
        if kind == "file":
            archive.write_text("other writer\n", encoding="utf-8")
        elif kind == "directory":
            archive.mkdir()
        else:
            archive.symlink_to(victim if kind == "symlink" else tmp_path / "missing", target_is_directory=True)
        return result

    monkeypatch.setattr(work_run_archive, "validate_archive", validate)
    with pytest.raises(WorkRunArchiveError):
        work_run_archive.export_run(replacement, archive, force=kind != "directory")
    assert marker.read_text(encoding="utf-8") == "untouched\n"
    if kind == "file":
        assert archive.read_text(encoding="utf-8") == "other writer\n"
    elif kind == "directory":
        assert archive.is_dir() and not list(archive.iterdir())
    else:
        assert archive.is_symlink()
    _assert_no_export_temps(archive)


def test_forced_replacement_success_and_nonforced_refusal(tmp_path: Path, capsys):
    _, replacement, archive, before = _replacement_case(tmp_path)
    assert cli.main(["runs", "export", str(replacement), "--output", str(archive), "--json"]) == 2
    assert "already exists" in capsys.readouterr().err
    assert _archive_bytes(archive) == before
    assert cli.main(["runs", "export", str(replacement), "--output", str(archive), "--force", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "exported"
    work_run_archive.validate_archive(archive)
    assert (archive / "payload" / "final.txt").read_text(encoding="utf-8") == "replacement artifact\n"
    _assert_no_export_temps(archive)
