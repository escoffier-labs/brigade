"""Export privacy for ``brigade.work-run`` archives (#2046)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from brigade import run_export_projection, run_resume, work_run_archive
from brigade.work_run_archive import WorkRunArchiveError

RUN_ID = "20260809-120000-abcdef12"
TASK_MARKER = "PROMPT-MARKER-task"
PLAN_MARKER = "PROMPT-MARKER-plan"
WORKER_MARKER = "GENERATED-MARKER-worker"
SYNTH_MARKER = "GENERATED-MARKER-synthesis"
BATCH_MARKER = "PROMPT-MARKER-batch"
RESULT_MARKER = "GENERATED-MARKER-batch-result"


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _seed_privacy_run(tmp_path: Path) -> tuple[Path, Path]:
    workspace = tmp_path / "workspace"
    run_dir = workspace / ".brigade" / "runs" / RUN_ID
    run_dir.mkdir(parents=True)
    _write_json(
        run_dir / "run.json",
        {
            "schema": "brigade.run.v1",
            "schema_version": 1,
            "run_id": RUN_ID,
            "status": "ok",
            "task": TASK_MARKER,
            "cwd": str(workspace / "project"),
            "lock_workspace": str(workspace),
            "artifacts": str(run_dir),
            "handoff": str(Path.home() / "private-handoff"),
            "handoff_inbox": str(run_dir / "inbox"),
            "control_socket": str(run_dir / "control.sock"),
            "artifact_collection": {
                "status": "ok",
                "worktree": str(workspace / "worktree"),
            },
            "started_at": "2026-08-09T12:00:00Z",
            "finished_at": "2026-08-09T12:01:00Z",
        },
    )
    _write_json(
        run_dir / "roster.json",
        {
            "schema": "brigade.roster_snapshot.v1",
            "schema_version": 1,
            "orchestrator": "chef",
            "agents": {
                "chef": {
                    "cli": "codex",
                    "command": [str(Path.home() / "bin" / "codex")],
                    "role": "plan",
                }
            },
        },
    )
    _write_json(
        run_dir / "plan.json",
        {
            "schema": "brigade.run_plan.v1",
            "schema_version": 1,
            "assignments": [{"stage": 1, "worker": "chef", "task": PLAN_MARKER}],
        },
    )
    _write_json(
        run_dir / "worker-results.json",
        {
            "schema": "brigade.worker_results.v1",
            "schema_version": 1,
            "results": [
                {
                    "worker": "chef",
                    "task": PLAN_MARKER,
                    "ok": True,
                    "detail": WORKER_MARKER,
                    "text": WORKER_MARKER,
                    "stdout_log": str(Path.home() / "worker.stdout.log"),
                }
            ],
            "ground_truth": {"patch_ref": "changes.patch"},
        },
    )
    _write_json(
        run_dir / "synthesis.json",
        {
            "schema": "brigade.synthesis.v1",
            "schema_version": 1,
            "result": {"ok": True, "text": SYNTH_MARKER, "detail": SYNTH_MARKER},
            "worker": "chef",
        },
    )
    _write_json(
        run_dir / "batch-request.json",
        {
            "schema": "brigade.run_batch.v1",
            "request_id": "batch-1",
            "base_sha": None,
            "fingerprint": "f" * 64,
            "layers": [["t1"]],
            "tasks": [
                {
                    "task_id": "t1",
                    "text": BATCH_MARKER,
                    "seat": "chef",
                    "depends_on": [],
                    "isolation": "shared",
                    "read_only": True,
                }
            ],
            "synthesis": {"text": BATCH_MARKER, "seat": "chef"},
        },
    )
    batch_name = f"batch-result-{hashlib.sha256(b't1').hexdigest()}.json"
    _write_json(
        run_dir / batch_name,
        {
            "worker": "chef",
            "task": BATCH_MARKER,
            "ok": True,
            "detail": RESULT_MARKER,
            "text": RESULT_MARKER,
        },
    )
    (run_dir / "final.txt").write_text("done\n", encoding="utf-8")
    (run_dir / "private-notes.txt").write_text(TASK_MARKER + "\n", encoding="utf-8")
    return workspace, run_dir


def _archive_bytes(archive: Path) -> bytes:
    return b"".join(path.read_bytes() for path in sorted(archive.rglob("*")) if path.is_file())


def _rewrite_payload(archive: Path, rel: str, payload: dict) -> None:
    path = archive / "payload" / rel
    _write_json(path, payload)
    raw = path.read_bytes()
    manifest = json.loads((archive / "work-run.json").read_text(encoding="utf-8"))
    for entry in manifest["files"]:
        if entry["path"] == rel:
            entry["sha256"] = hashlib.sha256(raw).hexdigest()
            entry["byte_size"] = len(raw)
            break
    else:
        raise AssertionError(f"missing manifest entry for {rel}")
    _write_json(archive / "work-run.json", manifest)


def test_export_drops_markers_paths_and_unknown_files(tmp_path: Path):
    workspace, run_dir = _seed_privacy_run(tmp_path)
    archive = tmp_path / "archive"
    exported = work_run_archive.export_run(run_dir, archive)

    raw = _archive_bytes(archive)
    for marker in (TASK_MARKER, PLAN_MARKER, WORKER_MARKER, SYNTH_MARKER, BATCH_MARKER, RESULT_MARKER):
        assert marker.encode() not in raw
    assert str(Path.home()).encode() not in raw
    assert not (archive / "payload" / "private-notes.txt").exists()
    assert exported["dropped_file_count"] >= 1

    run_payload = json.loads((archive / "payload" / "run.json").read_text(encoding="utf-8"))
    assert run_payload["task"]["digest"].startswith(run_export_projection.DIGEST_PREFIX)
    assert run_payload["cwd"] == "project"
    assert run_payload["lock_workspace"] == "."
    assert run_payload["artifacts"] == run_export_projection.RUN_DIR_TOKEN
    assert run_payload["handoff"] == run_export_projection.EXTERNAL_PATH_TOKEN
    assert run_payload["artifact_collection"]["worktree"] == "worktree"

    manifest = json.loads((archive / "work-run.json").read_text(encoding="utf-8"))
    assert "task" not in manifest["run"]
    assert manifest["run"]["task_digest"].startswith(run_export_projection.DIGEST_PREFIX)
    assert manifest["source_run_dir"] == run_export_projection.RUN_DIR_TOKEN

    imported = work_run_archive.import_archive(archive, runs_dir=tmp_path / "imported")
    installed = json.loads((Path(imported["run_dir"]) / "run.json").read_text(encoding="utf-8"))
    assert installed["task"]["digest"].startswith(run_export_projection.DIGEST_PREFIX)
    # Source run keeps the raw prompt for resume.
    assert json.loads((run_dir / "run.json").read_text(encoding="utf-8"))["task"] == TASK_MARKER


@pytest.mark.parametrize("kind", ["marker", "unknown-key", "unknown-file"])
def test_import_refuses_tampered_current_archive(tmp_path: Path, kind: str):
    _, run_dir = _seed_privacy_run(tmp_path)
    archive = tmp_path / "archive"
    work_run_archive.export_run(run_dir, archive)

    if kind == "marker":
        payload = json.loads((archive / "payload" / "run.json").read_text(encoding="utf-8"))
        payload["task"] = TASK_MARKER
        _rewrite_payload(archive, "run.json", payload)
    elif kind == "unknown-key":
        payload = json.loads((archive / "payload" / "run.json").read_text(encoding="utf-8"))
        payload["unexpected"] = "value"
        _rewrite_payload(archive, "run.json", payload)
    else:
        extra = archive / "payload" / "extra.json"
        _write_json(extra, {"marker": TASK_MARKER})
        raw = extra.read_bytes()
        manifest = json.loads((archive / "work-run.json").read_text(encoding="utf-8"))
        manifest["files"].append(
            {
                "path": "extra.json",
                "sha256": hashlib.sha256(raw).hexdigest(),
                "byte_size": len(raw),
                "role": "support",
            }
        )
        manifest["files"].sort(key=lambda entry: entry["path"])
        _write_json(archive / "work-run.json", manifest)

    with pytest.raises(WorkRunArchiveError) as excinfo:
        work_run_archive.import_archive(archive, runs_dir=tmp_path / "imported")
    assert excinfo.value.category == "export-privacy"


def test_legacy_archive_is_projected_before_install(tmp_path: Path):
    _, run_dir = _seed_privacy_run(tmp_path)
    archive = tmp_path / "archive"
    work_run_archive.export_run(run_dir, archive)

    legacy_run = json.loads((archive / "payload" / "run.json").read_text(encoding="utf-8"))
    legacy_run["task"] = TASK_MARKER
    legacy_run["unexpected"] = "legacy-field"
    legacy_run["cwd"] = str(Path.home() / "legacy-project")
    _rewrite_payload(archive, "run.json", legacy_run)

    manifest = json.loads((archive / "work-run.json").read_text(encoding="utf-8"))
    manifest["schema_version"] = 1
    manifest["compatibility"]["min_reader_schema_version"] = 1
    manifest["compatibility"]["max_reader_schema_version"] = 1
    _write_json(archive / "work-run.json", manifest)

    with pytest.raises(WorkRunArchiveError, match="unsupported archive schema_version"):
        work_run_archive.validate_archive(archive)

    imported = work_run_archive.import_archive(archive, runs_dir=tmp_path / "imported")
    assert imported["projected_legacy_payload"] is True
    installed_bytes = b"".join(path.read_bytes() for path in Path(imported["run_dir"]).rglob("*") if path.is_file())
    assert TASK_MARKER.encode() not in installed_bytes
    assert str(Path.home()).encode() not in installed_bytes
    installed = json.loads((Path(imported["run_dir"]) / "run.json").read_text(encoding="utf-8"))
    assert installed["task"]["digest"].startswith(run_export_projection.DIGEST_PREFIX)
    assert "unexpected" not in installed
    assert not installed["cwd"].startswith(("/", "~"))
    assert str(Path.home()) not in installed["cwd"]


def test_prompt_digest_is_keyed():
    first = run_export_projection.digest_prompt(b"a" * 32, "same prompt")
    second = run_export_projection.digest_prompt(b"b" * 32, "same prompt")
    assert first["digest"] != second["digest"]
    assert first["chars"] == len("same prompt")
    assert not first["digest"].startswith("sha256:")


def test_resume_still_reads_the_source_run_after_export(tmp_path: Path, capsys):
    workspace = tmp_path / "workspace"
    run_dir = workspace / ".brigade" / "runs" / RUN_ID
    run_dir.mkdir(parents=True)
    _write_json(
        run_dir / "run.json",
        {
            "schema": "brigade.run.v1",
            "schema_version": 1,
            "status": "failed",
            "task": TASK_MARKER,
            "cwd": str(workspace),
            "orchestrator": "chef",
            "read_only": True,
            "started_at": "2026-08-09T12:00:00Z",
        },
    )
    _write_json(
        run_dir / "roster.json",
        {
            "schema": "brigade.roster_snapshot.v1",
            "schema_version": 1,
            "orchestrator": "chef",
            "max_workers": 1,
            "timeout_seconds": 60.0,
            "allow_models": [],
            "sandbox": None,
            "agents": {"chef": {"cli": "codex", "model": None, "role": "plan", "timeout_seconds": None}},
        },
    )
    _write_json(
        run_dir / "worker-results.json",
        {
            "schema": "brigade.worker_results.v1",
            "schema_version": 1,
            "results": [{"worker": "chef", "task": "done", "ok": False, "detail": WORKER_MARKER}],
            "ground_truth": {},
        },
    )

    work_run_archive.export_run(run_dir, tmp_path / "archive")

    assert run_resume.resume(run_dir) == 2
    assert "no resumable workers" in capsys.readouterr().err
    assert json.loads((run_dir / "run.json").read_text(encoding="utf-8"))["task"] == TASK_MARKER
