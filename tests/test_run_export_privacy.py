"""Export privacy for ``brigade.work-run`` archives (#2046)."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from brigade import memory_recall_log, run_audit, run_export_projection, run_resume, work_run_archive
from brigade.work_run_archive import WorkRunArchiveError

RUN_ID = "20260809-120000-abcdef12"
TASK_MARKER = "PROMPT-MARKER-task"
PLAN_MARKER = "PROMPT-MARKER-plan"
WORKER_MARKER = "GENERATED-MARKER-worker"
SYNTH_MARKER = "GENERATED-MARKER-synthesis"
BATCH_MARKER = "PROMPT-MARKER-batch"
RESULT_MARKER = "GENERATED-MARKER-batch-result"
ABS_HOME = "/" + "home/alice"
DRIVE_PATH = "C:" + "\\" + "Users\\alice\\repo"
FILE_URI_PATH = "file://" + ABS_HOME + "/control.sock"


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


def test_export_never_includes_batch_task_artifact_patches(tmp_path: Path):
    """A candidate patch can carry untracked file contents, so it stays local (#2050)."""
    workspace, run_dir = _seed_privacy_run(tmp_path)
    digest = hashlib.sha256(b"t1").hexdigest()
    leaked = "LEAKED-UNTRACKED-CONTENT"
    patch = run_dir / f"batch-artifact-{digest}.patch"
    patch.write_text(
        "diff --git a/scratch b/scratch\nnew file mode 100644\n--- /dev/null\n+++ b/scratch\n+" + leaked + "\n",
        encoding="utf-8",
    )
    _write_json(
        run_dir / f"batch-artifact-{digest}.json",
        {
            "schema": "brigade.batch_task_artifact.v1",
            "task_id": "t1",
            "attempt_id": "attempt-1",
            "execution": "completed",
            "delivery": "retained",
            "reason": None,
            "base_sha": "a" * 40,
            "branch": "brigade/batch/t1-000000000000",
            "candidate_commit": "b" * 40,
            "patch_ref": patch.name,
            "patch_sha256": hashlib.sha256(patch.read_bytes()).hexdigest(),
            "patch_bytes": patch.stat().st_size,
            "changed_paths": 1,
            "ignored_left": 0,
        },
    )

    archive = tmp_path / "archive"
    exported = work_run_archive.export_run(run_dir, archive)

    manifest = json.loads((archive / "work-run.json").read_text(encoding="utf-8"))
    paths = [entry["path"] for entry in manifest["files"]]
    assert not [path for path in paths if "batch-artifact" in path]
    assert not list(archive.rglob("batch-artifact-*"))
    assert not (archive / "payload" / patch.name).exists()
    assert leaked.encode() not in _archive_bytes(archive)
    assert exported["dropped_file_count"] >= 2
    # The run still exports, and its result receipt stays.
    assert any(path.startswith("batch-result-") for path in paths)


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
    assert run_payload["cwd"]["digest"].startswith(run_export_projection.DIGEST_PREFIX)
    assert run_payload["lock_workspace"] == "."
    assert run_payload["artifacts"] == run_export_projection.RUN_DIR_TOKEN
    assert run_payload["handoff"] == run_export_projection.EXTERNAL_PATH_TOKEN
    assert run_payload["artifact_collection"]["worktree"]["digest"].startswith(run_export_projection.DIGEST_PREFIX)

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
    # A home path outside the inferred workspace maps to the external token. Inside it,
    # the relative path is digested. Either form is private.
    cwd = installed["cwd"]
    assert cwd == run_export_projection.EXTERNAL_PATH_TOKEN or cwd["digest"].startswith(
        run_export_projection.DIGEST_PREFIX
    )
    assert str(Path.home()) not in json.dumps(installed)


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


def test_generated_text_env_and_artifacts_are_not_exported(tmp_path: Path):
    _, run_dir = _seed_privacy_run(tmp_path)
    worker = json.loads((run_dir / "worker-results.json").read_text(encoding="utf-8"))
    worker["results"][0]["failure"] = {"message": "raw prompt"}
    worker["results"][0]["env_overrides"] = {"OPENAI_API_KEY": "sk-123"}
    _write_json(run_dir / "worker-results.json", worker)
    (run_dir / "summary.md").write_text("raw prompt\n", encoding="utf-8")
    (run_dir / "changes.patch").write_text("raw prompt\n", encoding="utf-8")

    archive = tmp_path / "archive"
    work_run_archive.export_run(run_dir, archive)

    raw = _archive_bytes(archive)
    assert b"raw prompt" not in raw
    assert b"sk-123" not in raw
    for name in ("final.txt", "summary.md", "changes.patch"):
        assert not (archive / "payload" / name).exists()


def test_export_refuses_prompt_list(tmp_path: Path):
    _, run_dir = _seed_privacy_run(tmp_path)
    payload = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    payload["task"] = ["prompt"]
    _write_json(run_dir / "run.json", payload)

    with pytest.raises(WorkRunArchiveError) as excinfo:
        work_run_archive.export_run(run_dir, tmp_path / "archive")
    assert excinfo.value.category == "export-privacy"


@pytest.mark.parametrize(
    "prompt", [TASK_MARKER, ["prompt"], {"digest": "hmac-sha256:" + "a" * 64 + " trailing", "chars": 1}]
)
def test_import_refuses_malformed_prompt_digest(tmp_path: Path, prompt):
    _, run_dir = _seed_privacy_run(tmp_path)
    archive = tmp_path / "archive"
    work_run_archive.export_run(run_dir, archive)
    payload = json.loads((archive / "payload" / "run.json").read_text(encoding="utf-8"))
    payload["task"] = prompt
    _rewrite_payload(archive, "run.json", payload)

    with pytest.raises(WorkRunArchiveError) as excinfo:
        work_run_archive.import_archive(archive, runs_dir=tmp_path / "imported")
    assert excinfo.value.category == "export-privacy"


@pytest.mark.parametrize("field", ["task", "source_run_dir", "schema_artifact"])
def test_manifest_rejects_private_fields(tmp_path: Path, field: str):
    _, run_dir = _seed_privacy_run(tmp_path)
    archive = tmp_path / "archive"
    work_run_archive.export_run(run_dir, archive)
    manifest = json.loads((archive / "work-run.json").read_text(encoding="utf-8"))
    if field == "task":
        manifest["run"]["task"] = TASK_MARKER
    elif field == "source_run_dir":
        manifest["source_run_dir"] = ABS_HOME + "/x"
    else:
        manifest["schema_artifact"] = ABS_HOME + "/x"
    _write_json(archive / "work-run.json", manifest)

    with pytest.raises(WorkRunArchiveError) as excinfo:
        work_run_archive.validate_archive(archive)
    assert excinfo.value.category == "export-privacy"


def test_drive_unc_file_uri_and_embedded_paths_are_mapped(tmp_path: Path):
    workspace, run_dir = _seed_privacy_run(tmp_path)
    payload = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    payload["cwd"] = DRIVE_PATH
    payload["handoff"] = "\\\\server\\share\\secret"
    payload["control_socket"] = FILE_URI_PATH
    payload["artifacts"] = "~other/alice/repo"
    payload["scheduler"] = {"requested": "waves", "used": None, "fallback_reason": "cwd=" + ABS_HOME + "/proj"}
    _write_json(run_dir / "run.json", payload)

    archive = tmp_path / "archive"
    work_run_archive.export_run(run_dir, archive)
    exported = json.loads((archive / "payload" / "run.json").read_text(encoding="utf-8"))
    token = run_export_projection.EXTERNAL_PATH_TOKEN
    assert exported["cwd"] == token
    assert exported["handoff"] == token
    assert exported["control_socket"] == token
    assert exported["artifacts"] == token
    assert exported["scheduler"]["fallback_reason"]["digest"].startswith(run_export_projection.DIGEST_PREFIX)
    raw = _archive_bytes(archive)
    for needle in (b"alice", b"C:\\\\", b"file://", b"\\\\server"):
        assert needle not in raw


def test_force_import_refusal_preserves_existing_run(tmp_path: Path):
    _, run_dir = _seed_privacy_run(tmp_path)
    archive = tmp_path / "archive"
    work_run_archive.export_run(run_dir, archive)
    payload = json.loads((archive / "payload" / "run.json").read_text(encoding="utf-8"))
    payload["task"] = TASK_MARKER
    _rewrite_payload(archive, "run.json", payload)

    imported = tmp_path / "imported"
    dest = imported / RUN_ID
    dest.mkdir(parents=True)
    (dest / "keep.txt").write_text("keep\n", encoding="utf-8")
    _write_json(dest / "run.json", {"schema": "brigade.run.v1", "schema_version": 1, "status": "ok"})

    with pytest.raises(WorkRunArchiveError):
        work_run_archive.import_archive(archive, runs_dir=imported, force=True)
    assert (dest / "keep.txt").read_text(encoding="utf-8") == "keep\n"
    assert json.loads((dest / "run.json").read_text(encoding="utf-8"))["status"] == "ok"


def test_shallow_run_path_never_uses_root(tmp_path: Path):
    shallow = tmp_path / "run"
    shallow.mkdir()
    assert run_export_projection.workspace_for_run(shallow) != Path("/")
    assert run_export_projection.workspace_for_run(shallow).is_dir()
    (tmp_path / ".brigade").mkdir()
    assert run_export_projection.workspace_for_run(shallow) == tmp_path
    assert run_export_projection.digest_key_target(shallow) == tmp_path
    assert run_export_projection.digest_key_target(shallow) != Path("/")


def test_legacy_receipts_without_schema_project(tmp_path: Path):
    _, run_dir = _seed_privacy_run(tmp_path)
    archive = tmp_path / "archive"
    work_run_archive.export_run(run_dir, archive)
    for rel in (
        "run.json",
        "roster.json",
        "plan.json",
        "worker-results.json",
        "synthesis.json",
        "batch-request.json",
    ):
        payload = json.loads((archive / "payload" / rel).read_text(encoding="utf-8"))
        payload.pop("schema", None)
        payload.pop("schema_version", None)
        _rewrite_payload(archive, rel, payload)
    manifest = json.loads((archive / "work-run.json").read_text(encoding="utf-8"))
    manifest["schema_version"] = 1
    manifest["compatibility"]["min_reader_schema_version"] = 1
    manifest["compatibility"]["max_reader_schema_version"] = 1
    _write_json(archive / "work-run.json", manifest)

    imported = work_run_archive.import_archive(archive, runs_dir=tmp_path / "imported")
    installed = json.loads((Path(imported["run_dir"]) / "run.json").read_text(encoding="utf-8"))
    assert installed["schema"] == "brigade.run.v1"
    assert installed["task"]["digest"].startswith(run_export_projection.DIGEST_PREFIX)


def test_import_refuses_non_json_artifact(tmp_path: Path):
    _, run_dir = _seed_privacy_run(tmp_path)
    archive = tmp_path / "archive"
    work_run_archive.export_run(run_dir, archive)
    extra = archive / "payload" / "final.txt"
    extra.write_text("generated text\n", encoding="utf-8")
    manifest = json.loads((archive / "work-run.json").read_text(encoding="utf-8"))
    manifest["files"].append(
        {
            "path": "final.txt",
            "sha256": hashlib.sha256(extra.read_bytes()).hexdigest(),
            "byte_size": len(extra.read_bytes()),
            "role": "artifact",
        }
    )
    manifest["files"].sort(key=lambda entry: entry["path"])
    _write_json(archive / "work-run.json", manifest)

    with pytest.raises(WorkRunArchiveError) as excinfo:
        work_run_archive.import_archive(archive, runs_dir=tmp_path / "imported")
    assert excinfo.value.category == "export-privacy"


def test_roster_agent_keys_become_digests(tmp_path: Path):
    _, run_dir = _seed_privacy_run(tmp_path)
    roster = json.loads((run_dir / "roster.json").read_text(encoding="utf-8"))
    roster["agents"]["../evil"] = roster["agents"]["chef"]
    _write_json(run_dir / "roster.json", roster)

    archive = tmp_path / "archive"
    work_run_archive.export_run(run_dir, archive)
    exported = json.loads((archive / "payload" / "roster.json").read_text(encoding="utf-8"))
    assert exported["agents"]
    assert all(key.startswith(run_export_projection.DIGEST_PREFIX) for key in exported["agents"])
    assert b"../evil" not in _archive_bytes(archive)


def test_import_refuses_unknown_batch_result_schema(tmp_path: Path):
    _, run_dir = _seed_privacy_run(tmp_path)
    archive = tmp_path / "archive"
    work_run_archive.export_run(run_dir, archive)
    batch = next((archive / "payload").glob("batch-result-*.json"))
    rel = batch.relative_to(archive / "payload").as_posix()
    payload = json.loads(batch.read_text(encoding="utf-8"))
    payload["schema"] = "brigade.unknown.v1"
    payload["schema_version"] = 1
    _rewrite_payload(archive, rel, payload)

    with pytest.raises(WorkRunArchiveError) as excinfo:
        work_run_archive.import_archive(archive, runs_dir=tmp_path / "imported")
    assert excinfo.value.category == "export-privacy"


def test_windows_workspace_path_maps_relative(monkeypatch):
    import pathlib

    monkeypatch.setattr(run_export_projection, "Path", pathlib.PureWindowsPath)
    ctx = run_export_projection._Context(
        workspace=pathlib.PureWindowsPath(r"D:\a\workspace"),
        run_dir=pathlib.PureWindowsPath(r"D:\a\workspace\.brigade\runs\id"),
        home=pathlib.PurePosixPath("/home/example"),  # content-guard: allow home-path
        key=b"k" * 32,
    )
    assert run_export_projection._map_path(r"D:\a\workspace\project", ctx) == "project"
    assert run_export_projection._map_path(r"D:\a\workspace\.brigade\runs\id\logs\x.log", ctx) == "<run-dir>/logs/x.log"
    assert run_export_projection._map_path(r"D:\other\project", ctx) == "<external-path>"
    assert run_export_projection._map_path(r"\\server\share\file", ctx) == "<external-path>"


def test_unknown_enum_status_is_tokenized_and_import_refuses(tmp_path: Path):
    _, run_dir = _seed_privacy_run(tmp_path)
    payload = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    payload["status"] = "api-key=<secret>"
    _write_json(run_dir / "run.json", payload)

    archive = tmp_path / "archive"
    work_run_archive.export_run(run_dir, archive)
    exported = json.loads((archive / "payload" / "run.json").read_text(encoding="utf-8"))
    assert exported["status"] == "<other>"
    assert b"api-key=<secret>" not in _archive_bytes(archive)

    clean = tmp_path / "clean"
    clean_run = _seed_privacy_run(tmp_path / "clean-src")[1]
    work_run_archive.export_run(clean_run, clean)
    tampered = json.loads((clean / "payload" / "run.json").read_text(encoding="utf-8"))
    tampered["status"] = "api-key=<secret>"
    _rewrite_payload(clean, "run.json", tampered)
    with pytest.raises(WorkRunArchiveError) as excinfo:
        work_run_archive.import_archive(clean, runs_dir=tmp_path / "imported")
    assert excinfo.value.category == "export-privacy"


def test_free_text_role_label_becomes_digest(tmp_path: Path):
    _, run_dir = _seed_privacy_run(tmp_path)
    roster = json.loads((run_dir / "roster.json").read_text(encoding="utf-8"))
    roster["agents"]["chef"]["role"] = "free text label"
    _write_json(run_dir / "roster.json", roster)

    archive = tmp_path / "archive"
    work_run_archive.export_run(run_dir, archive)
    exported = json.loads((archive / "payload" / "roster.json").read_text(encoding="utf-8"))
    assert all(
        agent["role"]["digest"].startswith(run_export_projection.DIGEST_PREFIX) for agent in exported["agents"].values()
    )
    assert b"free text label" not in _archive_bytes(archive)


@pytest.mark.skipif(os.name != "posix", reason="POSIX ownership and mode checks")
def test_search_log_key_rejects_loose_mode(tmp_path: Path, capsys):
    target = tmp_path / "target"
    path = memory_recall_log.key_path(target)
    path.parent.mkdir(parents=True)
    path.write_text("a" * 64 + "\n", encoding="ascii")
    path.chmod(0o644)
    assert memory_recall_log.load_or_create_key(target) is None
    err = capsys.readouterr().err
    assert str(path) in err
    assert "chmod 600" in err


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink semantics")
def test_search_log_key_rejects_symlink(tmp_path: Path, capsys):
    target = tmp_path / "target"
    path = memory_recall_log.key_path(target)
    path.parent.mkdir(parents=True)
    real = tmp_path / "real-key"
    real.write_text("b" * 64 + "\n", encoding="ascii")
    real.chmod(0o600)
    path.symlink_to(real)
    assert memory_recall_log.load_or_create_key(target) is None
    err = capsys.readouterr().err
    assert str(path) in err
    assert "chmod 600" in err


def test_journal_is_withheld_and_audit_reports_it(tmp_path: Path, monkeypatch):
    from tests import test_run_redaction

    monkeypatch.setattr(test_run_redaction, "RUN_ID", RUN_ID)
    run, _, events = test_run_redaction._authority_run(tmp_path)
    archive = tmp_path / "archive"
    work_run_archive.export_run(run, archive)
    assert not (archive / "payload" / "events" / "lifecycle.jsonl").exists()
    manifest = json.loads((archive / "work-run.json").read_text(encoding="utf-8"))
    assert manifest["journal_withheld"] == {
        "chain_head": events[-1].event_digest,
        "event_count": len(events),
    }
    assert manifest["compatibility"]["journal_authority"] == "withheld"

    imported = work_run_archive.import_archive(archive, runs_dir=tmp_path / "imported")
    audit = run_audit.audit_run(Path(imported["run_dir"]))
    assert audit.result == run_audit.RESULT_JOURNAL_WITHHELD
    assert "withheld" in audit.not_auditable_reason
    assert "legacy run without lifecycle journal" not in audit.not_auditable_reason


def test_import_refuses_injected_lifecycle_journal(tmp_path: Path, monkeypatch):
    from tests import test_run_redaction

    monkeypatch.setattr(test_run_redaction, "RUN_ID", RUN_ID)
    run, _, _ = test_run_redaction._authority_run(tmp_path)
    archive = tmp_path / "archive"
    work_run_archive.export_run(run, archive)
    injected = archive / "payload" / "events" / "lifecycle.jsonl"
    injected.parent.mkdir(parents=True, exist_ok=True)
    injected.write_bytes((run / "events" / "lifecycle.jsonl").read_bytes())

    with pytest.raises(WorkRunArchiveError) as excinfo:
        work_run_archive.import_archive(archive, runs_dir=tmp_path / "imported")
    assert excinfo.value.category == "export-privacy"


def test_v2_archive_without_journal_still_imports(tmp_path: Path):
    _, run_dir = _seed_privacy_run(tmp_path)
    archive = tmp_path / "archive"
    work_run_archive.export_run(run_dir, archive)
    manifest = json.loads((archive / "work-run.json").read_text(encoding="utf-8"))
    manifest["schema_version"] = 2
    manifest["compatibility"]["min_reader_schema_version"] = 2
    manifest["compatibility"]["max_reader_schema_version"] = 2
    manifest.pop("journal_withheld", None)
    manifest["compatibility"]["journal_authority"] = "none"
    _write_json(archive / "work-run.json", manifest)

    imported = work_run_archive.import_archive(archive, runs_dir=tmp_path / "imported")
    assert imported["schema_version"] == 2


def test_free_text_scalars_are_digested(tmp_path: Path):
    _, run_dir = _seed_privacy_run(tmp_path)
    run_meta = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    run_meta["requester_keyid"] = "api-key-secretvalue"
    _write_json(run_dir / "run.json", run_meta)
    roster = json.loads((run_dir / "roster.json").read_text(encoding="utf-8"))
    agent = roster["agents"].pop("chef")
    agent["model"] = "api-key-secretvalue"
    agent["cli"] = "api-key-secretvalue"
    roster["agents"]["api-key-secretvalue"] = agent
    _write_json(run_dir / "roster.json", roster)
    worker = json.loads((run_dir / "worker-results.json").read_text(encoding="utf-8"))
    worker["results"][0]["worker"] = "api-key-secretvalue"
    worker["results"][0]["stdout_log"] = "api-key-secretvalue"
    worker["results"][0]["requested_model"] = "api-key-secretvalue"
    worker["results"][0]["acpx_version"] = "1.2.3-secret"
    _write_json(run_dir / "worker-results.json", worker)

    archive = tmp_path / "archive"
    work_run_archive.export_run(run_dir, archive)
    run_payload = json.loads((archive / "payload" / "run.json").read_text(encoding="utf-8"))
    assert run_payload["requester_keyid"]["digest"].startswith(run_export_projection.DIGEST_PREFIX)
    roster_payload = json.loads((archive / "payload" / "roster.json").read_text(encoding="utf-8"))
    assert all(key.startswith(run_export_projection.DIGEST_PREFIX) for key in roster_payload["agents"])
    agent_payload = next(iter(roster_payload["agents"].values()))
    assert agent_payload["model"]["digest"].startswith(run_export_projection.DIGEST_PREFIX)
    assert agent_payload["cli"]["digest"].startswith(run_export_projection.DIGEST_PREFIX)
    worker_payload = json.loads((archive / "payload" / "worker-results.json").read_text(encoding="utf-8"))
    entry = worker_payload["results"][0]
    assert entry["worker"]["digest"].startswith(run_export_projection.DIGEST_PREFIX)
    assert entry["stdout_log"]["digest"].startswith(run_export_projection.DIGEST_PREFIX)
    assert entry["requested_model"]["digest"].startswith(run_export_projection.DIGEST_PREFIX)
    assert entry["acpx_version"]["digest"].startswith(run_export_projection.DIGEST_PREFIX)
    raw = _archive_bytes(archive)
    assert b"api-key-secretvalue" not in raw
    assert b"1.2.3-secret" not in raw


@pytest.mark.parametrize("field", ["requester_keyid", "stdout_log", "roster-key", "manifest"])
def test_import_refuses_raw_free_text_scalar(tmp_path: Path, field: str):
    _, run_dir = _seed_privacy_run(tmp_path)
    archive = tmp_path / "archive"
    work_run_archive.export_run(run_dir, archive)
    if field == "requester_keyid":
        payload = json.loads((archive / "payload" / "run.json").read_text(encoding="utf-8"))
        payload["requester_keyid"] = "api-key-secretvalue"
        _rewrite_payload(archive, "run.json", payload)
    elif field == "stdout_log":
        payload = json.loads((archive / "payload" / "worker-results.json").read_text(encoding="utf-8"))
        payload["results"][0]["stdout_log"] = "api-key-secretvalue"
        _rewrite_payload(archive, "worker-results.json", payload)
    elif field == "roster-key":
        payload = json.loads((archive / "payload" / "roster.json").read_text(encoding="utf-8"))
        payload["agents"]["api-key-secretvalue"] = {"cli": "codex"}
        _rewrite_payload(archive, "roster.json", payload)
    else:
        manifest = json.loads((archive / "work-run.json").read_text(encoding="utf-8"))
        manifest["run"]["status"] = "api-key-secretvalue"
        _write_json(archive / "work-run.json", manifest)

    with pytest.raises(WorkRunArchiveError) as excinfo:
        work_run_archive.import_archive(archive, runs_dir=tmp_path / "imported")
    assert excinfo.value.category == "export-privacy"


@pytest.mark.skipif(os.name != "posix", reason="POSIX ownership and mode checks")
def test_export_warns_when_key_is_refused(tmp_path: Path, capsys):
    workspace, run_dir = _seed_privacy_run(tmp_path)
    path = memory_recall_log.key_path(workspace)
    path.parent.mkdir(parents=True)
    path.write_text("a" * 64 + "\n", encoding="ascii")
    path.chmod(0o644)

    with pytest.raises(WorkRunArchiveError):
        work_run_archive.export_run(run_dir, tmp_path / "archive")
    err = capsys.readouterr().err
    assert str(path) in err
    assert "chmod 600" in err


@pytest.mark.parametrize("field", ["nested_schema", "path", "schema_artifact"])
def test_manifest_word_fields_refuse_free_text(tmp_path: Path, field: str):
    _, run_dir = _seed_privacy_run(tmp_path)
    archive = tmp_path / "archive"
    work_run_archive.export_run(run_dir, archive)
    manifest = json.loads((archive / "work-run.json").read_text(encoding="utf-8"))
    if field == "schema_artifact":
        manifest["schema_artifact"] = "api-key-secretvalue"
    else:
        entry = next(item for item in manifest["files"] if item["path"] == "run.json")
        entry[field] = "api-key-secretvalue"
    _write_json(archive / "work-run.json", manifest)

    with pytest.raises(WorkRunArchiveError):
        work_run_archive.validate_archive(archive)


def test_worker_stream_names_become_keyed_digests(tmp_path: Path):
    payload = tmp_path / "payload"
    stream = payload / "events" / "workers" / "api-key-secretvalue.scrubbed.json"
    stream.parent.mkdir(parents=True)
    stream.write_text("{}", encoding="utf-8")
    assert not run_export_projection.is_allowed_export_rel("events/workers/api-key-secretvalue.scrubbed.json")

    assert run_export_projection.rename_worker_streams(payload, b"k" * 32) == 1

    names = [path.relative_to(payload).as_posix() for path in payload.rglob("*") if path.is_file()]
    assert len(names) == 1
    assert "api-key-secretvalue" not in names[0]
    assert run_export_projection.is_allowed_export_rel(names[0])
    assert not (payload / "events" / "workers").exists()


def test_hex_named_stream_is_renamed_too(tmp_path: Path):
    payload = tmp_path / "payload"
    chosen = "a" * 64
    stream = payload / "events" / f"{chosen}.scrubbed.json"
    stream.parent.mkdir(parents=True)
    stream.write_text("{}", encoding="utf-8")

    assert run_export_projection.rename_worker_streams(payload, b"k" * 32) == 1

    names = [path.name for path in (payload / "events").iterdir()]
    assert names != [f"{chosen}.scrubbed.json"]
    assert run_export_projection.is_allowed_export_rel(f"events/{names[0]}")
