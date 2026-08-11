"""Versioned ``brigade.work-run`` archive schema, validator, and import/export.

Publishes archive wire compatibility separately from the checked-in JSON Schema
artifact (ACP-style): ``schemas/work-run.v1.schema.json`` describes the v1
document shape for tooling, while ``schema`` + ``schema_version`` on
``work-run.json`` govern import/export acceptance.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from . import __version__, localio, receipt_schema, run_checkpoint, worker_events

WORK_RUN_ARCHIVE_SCHEMA = "brigade.work-run"
WORK_RUN_ARCHIVE_SCHEMA_VERSION = 1
WORK_RUN_MANIFEST_NAME = "work-run.json"
WORK_RUN_PAYLOAD_DIR = "payload"
WORK_RUN_FORMAT = "directory"

# Nested receipt / document roles recognized when present under payload/.
_KNOWN_RECEIPT_FILES: dict[str, tuple[str, int]] = {
    "run.json": (receipt_schema.RUN_RECEIPT_SCHEMA, receipt_schema.RUN_RECEIPT_SCHEMA_VERSION),
    "roster.json": (receipt_schema.ROSTER_SNAPSHOT_SCHEMA, receipt_schema.ROSTER_SNAPSHOT_SCHEMA_VERSION),
    "plan.json": (receipt_schema.RUN_PLAN_SCHEMA, receipt_schema.RUN_PLAN_SCHEMA_VERSION),
    "worker-results.json": (receipt_schema.WORKER_RESULTS_SCHEMA, receipt_schema.WORKER_RESULTS_SCHEMA_VERSION),
    "synthesis.json": (receipt_schema.SYNTHESIS_SCHEMA, receipt_schema.SYNTHESIS_SCHEMA_VERSION),
}

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_RUN_ID_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{8}$")
_MEDIA_JSON = "application/json"
_MEDIA_JSONL = "application/x-ndjson"
_MEDIA_TEXT = "text/plain"
_MEDIA_PATCH = "text/x-diff"
_MEDIA_OCTET = "application/octet-stream"

_FILE_ROLES = frozenset(
    {
        "receipt",
        "journal",
        "checkpoint-reference",
        "artifact",
        "support",
        "other",
    }
)
_PRIVACY_CLASSES = frozenset({"public", "private", "redacted"})
_JOURNAL_AUTHORITY = frozenset({"none", "present", "authoritative"})
_PRIVATE_CHECKPOINT_POLICY = "strip_to_artifact_reference"
_RESUME_POLICY = "not_supported_v1"

_REQUIRED_MANIFEST_KEYS = frozenset(
    {
        "schema",
        "schema_version",
        "run_id",
        "exported_at",
        "exporter_brigade_version",
        "format",
        "payload_dir",
        "compatibility",
        "files",
    }
)
_OPTIONAL_MANIFEST_KEYS = frozenset({"run", "source_run_dir", "schema_artifact"})
_ALLOWED_MANIFEST_KEYS = _REQUIRED_MANIFEST_KEYS | _OPTIONAL_MANIFEST_KEYS

_REQUIRED_FILE_KEYS = frozenset({"path", "sha256", "byte_size", "role"})
_OPTIONAL_FILE_KEYS = frozenset(
    {
        "media_type",
        "nested_schema",
        "nested_schema_version",
        "privacy_class",
    }
)
_ALLOWED_FILE_KEYS = _REQUIRED_FILE_KEYS | _OPTIONAL_FILE_KEYS

_REQUIRED_COMPAT_KEYS = frozenset(
    {
        "min_reader_schema_version",
        "max_reader_schema_version",
        "journal_authority",
        "resume_supported",
        "private_checkpoint_bodies",
        "unsupported_archive_version",
        "nested_receipt_unknown_keys",
        "symlinks_and_special_files",
    }
)
_OPTIONAL_COMPAT_KEYS = frozenset({"schema_artifact_note"})
_ALLOWED_COMPAT_KEYS = _REQUIRED_COMPAT_KEYS | _OPTIONAL_COMPAT_KEYS


class WorkRunArchiveError(ValueError):
    """Archive schema, integrity, or compatibility failure."""

    def __init__(self, message: str, *, category: str = "schema") -> None:
        super().__init__(message)
        self.category = category


def schema_artifact_relative_path() -> str:
    """Return the repo-relative path of the published v1 JSON Schema artifact."""
    return "schemas/work-run.v1.schema.json"


def compatibility_defaults(*, journal_authority: str) -> dict[str, Any]:
    """Return the closed v1 compatibility object written into every manifest."""
    if journal_authority not in _JOURNAL_AUTHORITY:
        raise WorkRunArchiveError(f"invalid journal_authority: {journal_authority!r}", category="compatibility")
    return {
        "journal_authority": journal_authority,
        "max_reader_schema_version": WORK_RUN_ARCHIVE_SCHEMA_VERSION,
        "min_reader_schema_version": WORK_RUN_ARCHIVE_SCHEMA_VERSION,
        "nested_receipt_unknown_keys": "ignore",
        "private_checkpoint_bodies": _PRIVATE_CHECKPOINT_POLICY,
        "resume_supported": False,
        "schema_artifact_note": (
            "schemas/work-run.v1.schema.json versions the published JSON Schema "
            "artifact; archive wire acceptance uses schema + schema_version"
        ),
        "symlinks_and_special_files": "refuse",
        "unsupported_archive_version": "refuse",
    }


def validate_manifest(payload: Any) -> dict[str, Any]:
    """Validate a ``work-run.json`` object and return a normalized copy."""
    if not isinstance(payload, dict):
        raise WorkRunArchiveError("work-run manifest must be an object")
    unknown = sorted(set(payload) - _ALLOWED_MANIFEST_KEYS)
    if unknown:
        raise WorkRunArchiveError(f"unknown manifest field: {unknown[0]}")
    missing = sorted(_REQUIRED_MANIFEST_KEYS - set(payload))
    if missing:
        raise WorkRunArchiveError(f"missing required manifest field: {missing[0]}")

    if payload.get("schema") != WORK_RUN_ARCHIVE_SCHEMA:
        raise WorkRunArchiveError(
            f"unsupported archive schema {payload.get('schema')!r}; expected {WORK_RUN_ARCHIVE_SCHEMA!r}",
            category="compatibility",
        )
    schema_version = payload.get("schema_version")
    if type(schema_version) is not int or schema_version != WORK_RUN_ARCHIVE_SCHEMA_VERSION:
        raise WorkRunArchiveError(
            f"unsupported archive schema_version {schema_version!r}; "
            f"this Brigade supports {WORK_RUN_ARCHIVE_SCHEMA_VERSION}",
            category="compatibility",
        )
    run_id = _require_string(payload.get("run_id"), "run_id")
    if not _RUN_ID_RE.fullmatch(run_id):
        raise WorkRunArchiveError("run_id must match YYYYMMDD-HHMMSS-<8 hex>")
    exported_at = _require_string(payload.get("exported_at"), "exported_at")
    exporter = _require_string(payload.get("exporter_brigade_version"), "exporter_brigade_version")
    if payload.get("format") != WORK_RUN_FORMAT:
        raise WorkRunArchiveError(f"format must be {WORK_RUN_FORMAT!r}")
    payload_dir = _require_string(payload.get("payload_dir"), "payload_dir")
    if payload_dir != WORK_RUN_PAYLOAD_DIR:
        raise WorkRunArchiveError(f"payload_dir must be {WORK_RUN_PAYLOAD_DIR!r}")

    compatibility = _validate_compatibility(payload.get("compatibility"))
    files = _validate_files(payload.get("files"))
    run_summary = payload.get("run")
    normalized_run: dict[str, Any] | None = None
    if run_summary is not None:
        normalized_run = _validate_run_summary(run_summary)

    result: dict[str, Any] = {
        "schema": WORK_RUN_ARCHIVE_SCHEMA,
        "schema_version": WORK_RUN_ARCHIVE_SCHEMA_VERSION,
        "run_id": run_id,
        "exported_at": exported_at,
        "exporter_brigade_version": exporter,
        "format": WORK_RUN_FORMAT,
        "payload_dir": WORK_RUN_PAYLOAD_DIR,
        "compatibility": compatibility,
        "files": files,
    }
    if normalized_run is not None:
        result["run"] = normalized_run
    source_run_dir = payload.get("source_run_dir")
    if source_run_dir is not None:
        result["source_run_dir"] = _require_string(source_run_dir, "source_run_dir")
    schema_artifact = payload.get("schema_artifact")
    if schema_artifact is not None:
        result["schema_artifact"] = _require_string(schema_artifact, "schema_artifact")
    return result


def validate_archive(archive_dir: Path) -> dict[str, Any]:
    """Validate an on-disk archive (manifest + payload digests + export privacy)."""
    archive_dir = _normalize_user_path(archive_dir)
    if not archive_dir.is_dir():
        raise WorkRunArchiveError(f"archive directory not found: {archive_dir}", category="io")

    manifest_path = archive_dir / WORK_RUN_MANIFEST_NAME
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise WorkRunArchiveError(f"missing {WORK_RUN_MANIFEST_NAME}", category="io")
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkRunArchiveError(f"{WORK_RUN_MANIFEST_NAME} is not valid JSON: {exc}", category="io") from exc
    manifest = validate_manifest(raw)

    payload_root = archive_dir / WORK_RUN_PAYLOAD_DIR
    if not payload_root.is_dir() or payload_root.is_symlink():
        raise WorkRunArchiveError(f"missing payload directory: {WORK_RUN_PAYLOAD_DIR}", category="io")

    run_checkpoint.assert_export_tree_has_no_checkpoint_bodies(payload_root)
    try:
        worker_events.assert_export_tree_has_no_raw_worker_streams(payload_root)
    except worker_events.WorkerEventError as exc:
        raise WorkRunArchiveError(str(exc), category="export-privacy") from exc

    declared = {entry["path"]: entry for entry in manifest["files"]}
    on_disk = _collect_payload_files(payload_root)
    missing = sorted(set(declared) - set(on_disk))
    if missing:
        raise WorkRunArchiveError(f"payload missing declared file: {missing[0]}", category="integrity")
    unexpected = sorted(set(on_disk) - set(declared))
    if unexpected:
        raise WorkRunArchiveError(f"payload has undeclared file: {unexpected[0]}", category="integrity")

    for rel, entry in sorted(declared.items()):
        path = payload_root / rel
        if path.is_symlink() or not path.is_file():
            raise WorkRunArchiveError(f"payload path is not a regular file: {rel}", category="integrity")
        raw_bytes = path.read_bytes()
        digest = hashlib.sha256(raw_bytes).hexdigest()
        if digest != entry["sha256"]:
            raise WorkRunArchiveError(f"sha256 mismatch for {rel}", category="integrity")
        if len(raw_bytes) != entry["byte_size"]:
            raise WorkRunArchiveError(f"byte_size mismatch for {rel}", category="integrity")
        if entry["role"] == "checkpoint-reference":
            try:
                parsed = json.loads(raw_bytes.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise WorkRunArchiveError(
                    f"checkpoint-reference is not JSON: {rel}",
                    category="export-privacy",
                ) from exc
            if not isinstance(parsed, dict) or not run_checkpoint.is_checkpoint_artifact_reference(parsed):
                raise WorkRunArchiveError(
                    f"checkpoint body crossed an export boundary: {rel}",
                    category="export-privacy",
                )
        if worker_events.is_scrubbed_worker_event_stream_rel(rel):
            if entry.get("media_type") != worker_events.SCRUBBED_MEDIA_TYPE:
                raise WorkRunArchiveError(
                    f"scrubbed worker stream media_type mismatch: {rel}",
                    category="export-privacy",
                )
            if entry.get("privacy_class") != "redacted":
                raise WorkRunArchiveError(
                    f"scrubbed worker stream privacy_class must be redacted: {rel}",
                    category="export-privacy",
                )
            if entry.get("role") != "artifact":
                raise WorkRunArchiveError(
                    f"scrubbed worker stream role must be artifact: {rel}",
                    category="export-privacy",
                )
            if entry.get("nested_schema") != worker_events.STREAM_SCHEMA:
                raise WorkRunArchiveError(
                    f"scrubbed worker stream nested_schema mismatch: {rel}",
                    category="export-privacy",
                )
        if worker_events.is_raw_worker_event_stream_rel(rel):
            raise WorkRunArchiveError(
                f"raw worker stream crossed an export boundary: {rel}",
                category="export-privacy",
            )

    run_json = payload_root / "run.json"
    if not run_json.is_file():
        raise WorkRunArchiveError("payload must include run.json", category="schema")
    return manifest


def build_manifest_for_payload(
    payload_root: Path,
    *,
    run_id: str,
    exported_at: str | None = None,
    source_run_dir: str | None = None,
    exporter_brigade_version: str | None = None,
) -> dict[str, Any]:
    """Build a validated manifest for an already privacy-normalized payload tree."""
    payload_root = payload_root.expanduser().resolve()
    if not payload_root.is_dir():
        raise WorkRunArchiveError(f"payload directory not found: {payload_root}", category="io")
    run_checkpoint.assert_export_tree_has_no_checkpoint_bodies(payload_root)
    try:
        worker_events.assert_export_tree_has_no_raw_worker_streams(payload_root)
    except worker_events.WorkerEventError as exc:
        raise WorkRunArchiveError(str(exc), category="export-privacy") from exc

    files: list[dict[str, Any]] = []
    for rel in _collect_payload_files(payload_root):
        path = payload_root / rel
        raw = path.read_bytes()
        files.append(_file_entry_for_bytes(rel, raw))

    run_summary = _run_summary_from_payload(payload_root)
    journal_authority = _journal_authority_from_payload(payload_root, run_summary)
    manifest: dict[str, Any] = {
        "schema": WORK_RUN_ARCHIVE_SCHEMA,
        "schema_version": WORK_RUN_ARCHIVE_SCHEMA_VERSION,
        "run_id": run_id,
        "exported_at": exported_at or localio.utc_now_iso_z(),
        "exporter_brigade_version": exporter_brigade_version or __version__,
        "format": WORK_RUN_FORMAT,
        "payload_dir": WORK_RUN_PAYLOAD_DIR,
        "compatibility": compatibility_defaults(journal_authority=journal_authority),
        "files": files,
        "schema_artifact": schema_artifact_relative_path(),
    }
    if run_summary is not None:
        manifest["run"] = run_summary
    if source_run_dir is not None:
        manifest["source_run_dir"] = source_run_dir
    return validate_manifest(manifest)


def export_run(
    run_dir: Path,
    destination: Path,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Export a run directory into a versioned ``brigade.work-run`` archive.

    The source run directory is left unchanged. Private recovery-checkpoint
    bodies are stripped on the export copy only (#636). Raw worker event
    streams are replaced with scrubbed sidecars or refused (#592).
    """
    run_dir = _normalize_user_path(run_dir)
    dest_input = destination.expanduser()
    _reject_symlink_final_component(dest_input, label="destination")
    destination = dest_input.resolve()
    if not run_dir.is_dir():
        raise WorkRunArchiveError(f"run directory not found: {run_dir}", category="io")
    if not (run_dir / "run.json").is_file():
        raise WorkRunArchiveError(f"run directory has no run.json: {run_dir}", category="schema")
    if destination.exists():
        if not force:
            raise WorkRunArchiveError(f"destination already exists: {destination}", category="io")
        if not destination.is_dir():
            raise WorkRunArchiveError(f"destination is not a replaceable directory: {destination}", category="io")
        shutil.rmtree(destination)

    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.parent / f".{destination.name}.staging-{os.getpid()}"
    while staging.exists():
        staging = destination.parent / (
            f".{destination.name}.staging-{os.getpid()}-{hashlib.sha256(os.urandom(8)).hexdigest()[:6]}"
        )
    try:
        payload_root = staging / WORK_RUN_PAYLOAD_DIR
        _copy_run_tree_refuse_special(run_dir, payload_root)
        # Omit crashed checkpoint temp bodies; they cannot become truthful refs.
        cp_dir = payload_root / "events" / run_checkpoint.CHECKPOINT_DIR_NAME
        if cp_dir.is_dir():
            for temp_path in sorted(cp_dir.glob(".checkpoint.*.tmp")):
                if temp_path.is_file() and not temp_path.is_symlink():
                    temp_path.unlink()
        run_checkpoint.strip_checkpoint_bodies_for_export(payload_root)
        run_checkpoint.assert_export_tree_has_no_checkpoint_bodies(payload_root)
        try:
            worker_events.project_worker_streams_for_export(payload_root)
        except worker_events.WorkerEventError as exc:
            raise WorkRunArchiveError(str(exc), category="export-privacy") from exc

        run_id = run_dir.name
        if not _RUN_ID_RE.fullmatch(run_id):
            # Allow non-canonical directory names when run.json carries run_id.
            run_meta = localio.read_json_dict(payload_root / "run.json") or {}
            candidate = run_meta.get("run_id")
            if isinstance(candidate, str) and _RUN_ID_RE.fullmatch(candidate):
                run_id = candidate
            else:
                raise WorkRunArchiveError(
                    f"run directory name is not a run id and run.json has no run_id: {run_dir.name}",
                    category="schema",
                )

        manifest = build_manifest_for_payload(
            payload_root,
            run_id=run_id,
            source_run_dir=str(run_dir),
        )
        localio.write_json(staging / WORK_RUN_MANIFEST_NAME, manifest)
        os.rename(staging, destination)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    validated = validate_archive(destination)
    return {
        "status": "exported",
        "archive_dir": str(destination),
        "run_id": validated["run_id"],
        "schema": validated["schema"],
        "schema_version": validated["schema_version"],
        "file_count": len(validated["files"]),
        "compatibility": validated["compatibility"],
    }


def import_archive(
    archive_dir: Path,
    *,
    runs_dir: Path,
    force: bool = False,
) -> dict[str, Any]:
    """Validate an archive and copy its payload into ``runs_dir/<run_id>/``.

    v1 import is inspection-oriented: ``compatibility.resume_supported`` is
    always false. Journal-authoritative archives remain readable via
    ``brigade runs show`` / ``audit`` after import, but local resume/recover
    that needs private checkpoint bodies is out of scope for this envelope.
    """
    archive_dir = _normalize_user_path(archive_dir)
    runs_dir = runs_dir.expanduser().resolve()
    manifest = validate_archive(archive_dir)
    run_id = str(manifest["run_id"])
    dest = runs_dir / run_id
    _reject_symlink_final_component(dest, label="destination run")
    if dest.exists():
        if not force:
            raise WorkRunArchiveError(f"destination run already exists: {dest}", category="io")
        if not dest.is_dir():
            raise WorkRunArchiveError(f"destination is not a replaceable directory: {dest}", category="io")
        shutil.rmtree(dest)

    runs_dir.mkdir(parents=True, exist_ok=True)
    staging = runs_dir / f".{run_id}.import-{localio.utc_now().strftime('%H%M%S')}-{os.getpid()}"
    # Unique non-existing path: copytree creates the destination directory.
    while staging.exists():
        staging = (
            runs_dir
            / f".{run_id}.import-{localio.utc_now().strftime('%H%M%S')}-{os.getpid()}-{hashlib.sha256(os.urandom(8)).hexdigest()[:6]}"
        )
    try:
        _copy_run_tree_refuse_special(archive_dir / WORK_RUN_PAYLOAD_DIR, staging)
        run_checkpoint.assert_export_tree_has_no_checkpoint_bodies(staging)
        try:
            worker_events.assert_export_tree_has_no_raw_worker_streams(staging)
        except worker_events.WorkerEventError as exc:
            raise WorkRunArchiveError(str(exc), category="export-privacy") from exc
        # Re-check digests against the validated manifest after the copy.
        for entry in manifest["files"]:
            rel = str(entry["path"])
            path = staging / rel
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != entry["sha256"]:
                raise WorkRunArchiveError(f"import copy sha256 mismatch for {rel}", category="integrity")
        _reject_symlink_final_component(dest, label="destination run")
        os.rename(staging, dest)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    return {
        "status": "imported",
        "archive_dir": str(archive_dir),
        "run_dir": str(dest),
        "run_id": run_id,
        "schema": manifest["schema"],
        "schema_version": manifest["schema_version"],
        "file_count": len(manifest["files"]),
        "compatibility": manifest["compatibility"],
        "resume_supported": False,
    }


def _validate_compatibility(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WorkRunArchiveError("compatibility must be an object")
    unknown = sorted(set(value) - _ALLOWED_COMPAT_KEYS)
    if unknown:
        raise WorkRunArchiveError(f"unknown compatibility field: {unknown[0]}")
    missing = sorted(_REQUIRED_COMPAT_KEYS - set(value))
    if missing:
        raise WorkRunArchiveError(f"missing compatibility field: {missing[0]}")

    min_v = value.get("min_reader_schema_version")
    max_v = value.get("max_reader_schema_version")
    if type(min_v) is not int or type(max_v) is not int:
        raise WorkRunArchiveError("compatibility reader schema versions must be integers")
    if min_v != WORK_RUN_ARCHIVE_SCHEMA_VERSION or max_v != WORK_RUN_ARCHIVE_SCHEMA_VERSION:
        raise WorkRunArchiveError(
            "compatibility reader window must equal supported archive schema_version",
            category="compatibility",
        )
    journal_authority = value.get("journal_authority")
    if journal_authority not in _JOURNAL_AUTHORITY:
        raise WorkRunArchiveError("compatibility.journal_authority is invalid")
    if value.get("resume_supported") is not False:
        raise WorkRunArchiveError("compatibility.resume_supported must be false in v1")
    if value.get("private_checkpoint_bodies") != _PRIVATE_CHECKPOINT_POLICY:
        raise WorkRunArchiveError("compatibility.private_checkpoint_bodies must strip private bodies")
    if value.get("unsupported_archive_version") != "refuse":
        raise WorkRunArchiveError("compatibility.unsupported_archive_version must be refuse")
    if value.get("nested_receipt_unknown_keys") != "ignore":
        raise WorkRunArchiveError("compatibility.nested_receipt_unknown_keys must be ignore")
    if value.get("symlinks_and_special_files") != "refuse":
        raise WorkRunArchiveError("compatibility.symlinks_and_special_files must be refuse")

    result = {key: value[key] for key in sorted(_REQUIRED_COMPAT_KEYS)}
    note = value.get("schema_artifact_note")
    if note is not None:
        result["schema_artifact_note"] = _require_string(note, "compatibility.schema_artifact_note")
    return result


def _validate_files(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise WorkRunArchiveError("files must be a non-empty array")
    seen: set[str] = set()
    files: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise WorkRunArchiveError(f"files[{index}] must be an object")
        unknown = sorted(set(item) - _ALLOWED_FILE_KEYS)
        if unknown:
            raise WorkRunArchiveError(f"files[{index}] unknown field: {unknown[0]}")
        missing = sorted(_REQUIRED_FILE_KEYS - set(item))
        if missing:
            raise WorkRunArchiveError(f"files[{index}] missing field: {missing[0]}")
        path = _require_relative_payload_path(item.get("path"), f"files[{index}].path")
        if path in seen:
            raise WorkRunArchiveError(f"duplicate files path: {path}")
        seen.add(path)
        sha = _require_string(item.get("sha256"), f"files[{index}].sha256")
        if not _HEX64.fullmatch(sha):
            raise WorkRunArchiveError(f"files[{index}].sha256 must be 64 lowercase hex")
        byte_size = item.get("byte_size")
        if isinstance(byte_size, bool) or not isinstance(byte_size, int) or byte_size < 0:
            raise WorkRunArchiveError(f"files[{index}].byte_size must be a non-negative integer")
        role = _require_string(item.get("role"), f"files[{index}].role")
        if role not in _FILE_ROLES:
            raise WorkRunArchiveError(f"files[{index}].role is invalid")
        entry: dict[str, Any] = {
            "path": path,
            "sha256": sha,
            "byte_size": byte_size,
            "role": role,
        }
        media_type = item.get("media_type")
        if media_type is not None:
            entry["media_type"] = _require_string(media_type, f"files[{index}].media_type")
        nested_schema = item.get("nested_schema")
        if nested_schema is not None:
            entry["nested_schema"] = _require_string(nested_schema, f"files[{index}].nested_schema")
        nested_version = item.get("nested_schema_version")
        if nested_version is not None:
            if type(nested_version) is not int or nested_version < 1:
                raise WorkRunArchiveError(f"files[{index}].nested_schema_version must be a positive integer")
            entry["nested_schema_version"] = nested_version
        privacy = item.get("privacy_class")
        if privacy is not None:
            if privacy not in _PRIVACY_CLASSES:
                raise WorkRunArchiveError(f"files[{index}].privacy_class is invalid")
            entry["privacy_class"] = privacy
        files.append(entry)
    files.sort(key=lambda row: str(row["path"]))
    if "run.json" not in seen:
        raise WorkRunArchiveError("files must declare run.json")
    return files


def _validate_run_summary(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WorkRunArchiveError("run summary must be an object")
    allowed = frozenset({"schema", "schema_version", "status", "started_at", "finished_at", "task"})
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise WorkRunArchiveError(f"unknown run summary field: {unknown[0]}")
    result: dict[str, Any] = {}
    schema = value.get("schema")
    if schema is not None:
        result["schema"] = _require_string(schema, "run.schema")
    schema_version = value.get("schema_version")
    if schema_version is not None:
        if type(schema_version) is not int or schema_version < 1:
            raise WorkRunArchiveError("run.schema_version must be a positive integer")
        result["schema_version"] = schema_version
    for key in ("status", "started_at", "finished_at", "task"):
        item = value.get(key)
        if item is not None:
            result[key] = _require_string(item, f"run.{key}")
    return result


def _file_entry_for_bytes(rel: str, raw: bytes) -> dict[str, Any]:
    role, media_type, privacy = _classify_path(rel, raw)
    entry: dict[str, Any] = {
        "path": rel,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "byte_size": len(raw),
        "role": role,
        "media_type": media_type,
        "privacy_class": privacy,
    }
    nested_schema, nested_version = _nested_schema_for(rel, raw)
    if nested_schema is not None:
        entry["nested_schema"] = nested_schema
    if nested_version is not None:
        entry["nested_schema_version"] = nested_version
    return entry


def _classify_path(rel: str, raw: bytes) -> tuple[str, str, str]:
    if rel in _KNOWN_RECEIPT_FILES:
        return "receipt", _MEDIA_JSON, "public"
    if rel == "events/lifecycle.jsonl":
        return "journal", _MEDIA_JSONL, "public"
    if worker_events.is_raw_worker_event_stream_rel(rel):
        # Raw streams are local-only. Export must project them first; classify
        # never treats them as public support data.
        raise WorkRunArchiveError(
            f"raw worker stream crossed an export boundary: {rel}",
            category="export-privacy",
        )
    if worker_events.is_scrubbed_worker_event_stream_rel(rel):
        return "artifact", worker_events.SCRUBBED_MEDIA_TYPE, "redacted"
    if rel.startswith(f"events/{run_checkpoint.CHECKPOINT_DIR_NAME}/") and rel.endswith(".json"):
        privacy = "private"
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            parsed = None
        if isinstance(parsed, dict) and run_checkpoint.is_checkpoint_artifact_reference(parsed):
            return "checkpoint-reference", _MEDIA_JSON, privacy
        return "other", _MEDIA_JSON, privacy
    if rel in {"final.txt", "summary.md", "detached.log"}:
        return "artifact", _MEDIA_TEXT, "public"
    if rel == "changes.patch":
        return "artifact", _MEDIA_PATCH, "public"
    if rel.endswith(".json"):
        return "support", _MEDIA_JSON, "public"
    if rel.endswith(".jsonl"):
        return "support", _MEDIA_JSONL, "public"
    return "other", _MEDIA_OCTET, "public"


def _nested_schema_for(rel: str, raw: bytes) -> tuple[str | None, int | None]:
    known = _KNOWN_RECEIPT_FILES.get(rel)
    if known is not None:
        schema_name, schema_version = known
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return schema_name, schema_version
        if isinstance(parsed, dict):
            nested = parsed.get("schema")
            version = parsed.get("schema_version")
            if isinstance(nested, str) and nested:
                schema_name = nested
            if type(version) is int and version >= 1:
                schema_version = version
        return schema_name, schema_version
    if worker_events.is_scrubbed_worker_event_stream_rel(rel):
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return worker_events.STREAM_SCHEMA, worker_events.STREAM_SCHEMA_VERSION
        if isinstance(parsed, dict):
            nested = parsed.get("schema")
            version = parsed.get("schema_version")
            nested_schema = worker_events.STREAM_SCHEMA
            nested_version = worker_events.STREAM_SCHEMA_VERSION
            if isinstance(nested, str) and nested:
                nested_schema = nested
            if type(version) is int and version >= 1:
                nested_version = version
            return nested_schema, nested_version
        return worker_events.STREAM_SCHEMA, worker_events.STREAM_SCHEMA_VERSION
    if rel.endswith(".json"):
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None, None
        if isinstance(parsed, dict):
            nested = parsed.get("schema")
            version = parsed.get("schema_version")
            json_schema = nested if isinstance(nested, str) and nested else None
            json_version = version if type(version) is int and version >= 1 else None
            return json_schema, json_version
    if rel == "events/lifecycle.jsonl":
        return receipt_schema.RUN_EVENT_SCHEMA, receipt_schema.RUN_EVENT_SCHEMA_VERSION
    return None, None


def _run_summary_from_payload(payload_root: Path) -> dict[str, Any] | None:
    meta = localio.read_json_dict(payload_root / "run.json")
    if meta is None:
        return None
    summary: dict[str, Any] = {}
    schema = meta.get("schema")
    if isinstance(schema, str) and schema:
        summary["schema"] = schema
    version = meta.get("schema_version")
    if type(version) is int and version >= 1:
        summary["schema_version"] = version
    for key in ("status", "started_at", "finished_at", "task"):
        value = meta.get(key)
        if isinstance(value, str) and value:
            summary[key] = value
    return summary or None


def _journal_authority_from_payload(payload_root: Path, run_summary: Mapping[str, Any] | None) -> str:
    journal = payload_root / "events" / "lifecycle.jsonl"
    if not journal.is_file():
        return "none"
    meta = localio.read_json_dict(payload_root / "run.json") or {}
    if meta.get("run_journal_authority_requested") is True or meta.get("journal_authority") == "authoritative":
        return "authoritative"
    return "present"


def _collect_payload_files(payload_root: Path) -> list[str]:
    files: list[str] = []
    for path in sorted(payload_root.rglob("*")):
        if path.is_dir() and not path.is_symlink():
            continue
        rel = path.relative_to(payload_root).as_posix()
        if path.is_symlink() or not path.is_file() or not stat.S_ISREG(path.stat().st_mode):
            raise WorkRunArchiveError(
                f"payload contains symlink or special file: {rel}",
                category="io",
            )
        files.append(rel)
    return files


def _copy_run_tree_refuse_special(source: Path, dest: Path) -> None:
    """Copy a run tree, refusing symlinks and non-regular files."""

    def _ignore(directory: str, names: list[str]) -> set[str]:
        ignored: set[str] = set()
        base = Path(directory)
        for name in names:
            path = base / name
            try:
                st = path.lstat()
            except OSError as exc:
                raise WorkRunArchiveError(f"cannot stat {path}: {exc}", category="io") from exc
            if stat.S_ISLNK(st.st_mode):
                raise WorkRunArchiveError(f"refusing symlink in run tree: {path}", category="io")
            if stat.S_ISDIR(st.st_mode):
                continue
            if not stat.S_ISREG(st.st_mode):
                raise WorkRunArchiveError(f"refusing special file in run tree: {path}", category="io")
        return ignored

    shutil.copytree(source, dest, symlinks=False, ignore=_ignore)


def _require_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise WorkRunArchiveError(f"{name} must be a non-empty string")
    return value


def _reject_symlink_final_component(path: Path, *, label: str = "path") -> None:
    """Reject a symlinked final path component before resolve() erases it."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise WorkRunArchiveError(f"cannot stat {label}: {path}", category="io") from exc
    if stat.S_ISLNK(st.st_mode):
        raise WorkRunArchiveError(f"refusing symlinked {label}: {path}", category="io")


def _normalize_user_path(path: Path) -> Path:
    """Expand ~, refuse a symlinked final component, then resolve parents."""
    expanded = path.expanduser()
    _reject_symlink_final_component(expanded)
    return expanded.resolve()


def _require_relative_payload_path(value: Any, name: str) -> str:
    path = _require_string(value, name)
    pure = PurePosixPath(path)
    if pure.is_absolute() or "\\" in path or ".." in pure.parts or path.startswith("./"):
        raise WorkRunArchiveError(f"{name} must be a relative payload path")
    if not path or path.endswith("/"):
        raise WorkRunArchiveError(f"{name} must be a relative payload path")
    return path


# -- CLI entrypoints ----------------------------------------------------------


def export_cli(
    run: str | Path,
    *,
    cwd: Path,
    runs_dir: Path | None,
    output: Path,
    force: bool = False,
    json_output: bool = False,
) -> int:
    """CLI wrapper for ``brigade runs export``."""
    import sys

    from . import runs_cmd

    run_dir, error = runs_cmd._resolve_run_dir(run, cwd=cwd, runs_dir=runs_dir)
    if error is not None:
        print(error, file=sys.stderr)
        return 2
    assert run_dir is not None
    try:
        payload = export_run(run_dir, output, force=force)
    except WorkRunArchiveError as exc:
        print(f"error: {exc.category}: {exc}", file=sys.stderr)
        return 2 if exc.category in {"io", "compatibility"} else 1
    except run_checkpoint.CheckpointError as exc:
        print(f"error: export-privacy: {exc}", file=sys.stderr)
        return 1
    except worker_events.WorkerEventError as exc:
        print(f"error: export-privacy: {exc}", file=sys.stderr)
        return 1
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"work-run archive: {payload['run_id']}")
    print(f"schema: {payload['schema']} v{payload['schema_version']}")
    print(f"path: {payload['archive_dir']}")
    print(f"files: {payload['file_count']}")
    return 0


def import_cli(
    archive: Path,
    *,
    cwd: Path,
    runs_dir: Path | None,
    force: bool = False,
    json_output: bool = False,
) -> int:
    """CLI wrapper for ``brigade runs import``."""
    import sys

    root = runs_dir.expanduser() if runs_dir is not None else cwd.expanduser().resolve() / ".brigade" / "runs"
    try:
        payload = import_archive(archive, runs_dir=root, force=force)
    except WorkRunArchiveError as exc:
        print(f"error: {exc.category}: {exc}", file=sys.stderr)
        return 2 if exc.category in {"io", "compatibility"} else 1
    except run_checkpoint.CheckpointError as exc:
        print(f"error: export-privacy: {exc}", file=sys.stderr)
        return 1
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"work-run import: {payload['run_id']}")
    print(f"schema: {payload['schema']} v{payload['schema_version']}")
    print(f"run_dir: {payload['run_dir']}")
    print(f"files: {payload['file_count']}")
    print("resume_supported: false")
    return 0


def validate_cli(archive: Path, *, json_output: bool = False) -> int:
    """CLI wrapper for ``brigade runs validate-archive``."""
    import sys

    try:
        manifest = validate_archive(archive)
    except WorkRunArchiveError as exc:
        print(f"error: {exc.category}: {exc}", file=sys.stderr)
        return 2 if exc.category in {"io", "compatibility"} else 1
    except run_checkpoint.CheckpointError as exc:
        print(f"error: export-privacy: {exc}", file=sys.stderr)
        return 1
    payload = {
        "status": "valid",
        "archive_dir": str(archive.expanduser().resolve()),
        "run_id": manifest["run_id"],
        "schema": manifest["schema"],
        "schema_version": manifest["schema_version"],
        "file_count": len(manifest["files"]),
        "compatibility": manifest["compatibility"],
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print("work-run archive: valid")
    print(f"run_id: {payload['run_id']}")
    print(f"schema: {payload['schema']} v{payload['schema_version']}")
    print(f"files: {payload['file_count']}")
    return 0
