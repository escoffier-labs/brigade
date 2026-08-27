"""Run-artifact lifecycle writes used by :mod:`brigade.aboyeur`.

These operations are deliberately isolated from planning and dispatch: they
coordinate journal/checkpoint projections and must stay durable when worker
execution terminates unexpectedly.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from . import receipt_schema, runguard, run_checkpoint, run_lifecycle
from . import aboyeur as _aboyeur


def _one_line(text: str) -> str:
    return _aboyeur._one_line(text)


def _parse_iso_datetime(value: object) -> datetime | None:
    return _aboyeur._parse_iso_datetime(value)


def _utc_iso(value: datetime) -> str:
    return _aboyeur._utc_iso(value)


def _with_patch_ref(ground_truth: object, patch_ref: str) -> object:
    return _aboyeur._with_patch_ref(ground_truth, patch_ref)


def _write_json(path: Path, payload: object) -> None:
    _aboyeur._write_json(path, payload)


def write_sidecar_revision(run_dir: Path, filename: str, payload: object) -> None:
    _aboyeur.write_sidecar_revision(run_dir, filename, payload)


def set_artifact_patch_ref(output_dir: Path, patch_ref: str = "changes.patch") -> None:
    for filename in ("worker-results.json", "synthesis.json"):
        path = output_dir / filename
        try:
            payload = json.loads(path.read_text())
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise runguard.RunGuardError(
                f"failed to read {filename} while recording artifact patch reference: {exc}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise runguard.RunGuardError(
                f"failed to parse {filename} while recording artifact patch reference: {exc}"
            ) from exc
        if not isinstance(payload, dict) or "ground_truth" not in payload:
            raise runguard.RunGuardError(f"{filename} is missing ground_truth while recording artifact patch reference")
        payload["ground_truth"] = _with_patch_ref(payload.get("ground_truth"), patch_ref)
        if filename == "worker-results.json":
            receipt_schema.stamp_worker_results_document(payload)
        else:
            receipt_schema.stamp_synthesis_document(payload)
        try:
            write_sidecar_revision(output_dir, filename, payload)
        except OSError as exc:
            raise runguard.RunGuardError(f"failed to record artifact patch reference in {filename}: {exc}") from exc


def record_artifact_collection(
    output_dir: Path,
    *,
    status: str,
    patch_ref: str | None = None,
    changed: bool | None = None,
    tracked_count: int | None = None,
    untracked_count: int | None = None,
    worktree: Path | None = None,
    failure_phase: str | None = None,
    failure_kind: str | None = None,
    detail: str | None = None,
) -> None:
    run_path = output_dir / "run.json"
    try:
        payload = json.loads(run_path.read_text())
    except OSError as exc:
        raise runguard.RetainRunLockError(f"failed to read run receipt during artifact collection: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise runguard.RetainRunLockError(f"run receipt is invalid during artifact collection: {exc}") from exc
    if not isinstance(payload, dict):
        raise runguard.RetainRunLockError("run receipt must contain an object during artifact collection")

    collection: dict[str, object] = {"status": status}
    if patch_ref is not None:
        collection["patch_ref"] = patch_ref
    if changed is not None:
        collection["changed"] = changed
    if tracked_count is not None:
        collection["tracked_count"] = tracked_count
    if untracked_count is not None:
        collection["untracked_count"] = untracked_count
    if worktree is not None:
        collection["worktree"] = str(worktree)

    if status == "failed":
        bounded_detail = _one_line(detail or "artifact collection failed")[:2000]
        phase = failure_phase or "artifact-collection"
        artifact_failure = {
            "phase": phase,
            "kind": failure_kind or "unknown",
            "detail": bounded_detail,
        }
        collection["failure"] = artifact_failure
        primary_status = payload.get("status")
        primary_terminal = isinstance(primary_status, str) and primary_status not in {
            "started",
            "planning",
            "dispatching",
            "result-processing",
            "synthesizing",
            "handoff",
            "artifact-collection",
            "running",
        }
        if not primary_terminal:
            payload["status"] = "failed"
            payload["error"] = bounded_detail
            payload["failure_phase"] = phase
            payload["failure"] = artifact_failure
            finished_at = datetime.now(timezone.utc)
            payload["status_started_at"] = _utc_iso(finished_at)
            payload["finished_at"] = _utc_iso(finished_at)
            started_at = payload.get("started_at")
            if isinstance(started_at, str):
                started = _parse_iso_datetime(started_at)
                if started is not None:
                    payload["duration_seconds"] = max(
                        0.0,
                        round((finished_at - started).total_seconds(), 3),
                    )
    elif status == "ok" and payload.get("status") == "artifact-collection":
        finished_at = datetime.now(timezone.utc)
        payload["status"] = "ok"
        payload["status_started_at"] = _utc_iso(finished_at)
        payload["finished_at"] = _utc_iso(finished_at)
        started_at = payload.get("started_at")
        if isinstance(started_at, str):
            started = _parse_iso_datetime(started_at)
            if started is not None:
                payload["duration_seconds"] = max(
                    0.0,
                    round((finished_at - started).total_seconds(), 3),
                )
    payload["artifact_collection"] = collection
    try:
        _write_json(run_path, receipt_schema.stamp_run_receipt(payload))
    except (OSError, run_lifecycle.LifecycleJournalError, run_checkpoint.CheckpointError) as exc:
        raise runguard.RetainRunLockError(f"failed to update run receipt after artifact collection: {exc}") from exc


def record_run_termination(
    output_dir: Path,
    *,
    status: str,
    failure_phase: str | None,
    failure_kind: str,
    detail: str,
    seat: str | None = None,
    active_seats: tuple[str, ...] = (),
) -> None:
    """Terminalize a run that escaped its normal result path."""

    run_path = output_dir / "run.json"
    try:
        payload = json.loads(run_path.read_text())
    except OSError as exc:
        raise runguard.RetainRunLockError(f"failed to read run receipt during termination: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise runguard.RetainRunLockError(f"run receipt is invalid during termination: {exc}") from exc
    if not isinstance(payload, dict):
        raise runguard.RetainRunLockError("run receipt must contain an object during termination")
    if payload.get("finished_at"):
        return

    stored_status = payload.get("status")
    phase_owner = payload.get("phase_owner")
    if stored_status == "result-processing" and isinstance(phase_owner, str) and phase_owner:
        seat = phase_owner
        active_seats = ()
    phase_by_status = {
        "started": "startup",
        "planning": "planning",
        "dispatching": "dispatch",
        "result-processing": "result-processing",
        "synthesizing": "synthesis",
        "handoff": "handoff",
        "artifact-collection": "artifact-collection",
    }
    phase = failure_phase or (phase_by_status.get(stored_status, "run") if isinstance(stored_status, str) else "run")
    bounded_detail = _one_line(detail or failure_kind)[:2000]
    failure: dict[str, object] = {
        "phase": phase,
        "kind": failure_kind,
        "detail": bounded_detail,
    }
    if seat:
        failure["seat"] = seat
    elif active_seats:
        failure["seats"] = list(active_seats)
    finished_at = datetime.now(timezone.utc)
    payload.update(
        {
            "status": status,
            "status_started_at": _utc_iso(finished_at),
            "error": bounded_detail,
            "failure_phase": phase,
            "failure": failure,
            "finished_at": _utc_iso(finished_at),
        }
    )
    started_at = payload.get("started_at")
    if isinstance(started_at, str):
        started = _parse_iso_datetime(started_at)
        if started is not None:
            payload["duration_seconds"] = max(
                0.0,
                round((finished_at - started).total_seconds(), 3),
            )
    try:
        _write_json(run_path, receipt_schema.stamp_run_receipt(payload))
    except (OSError, run_lifecycle.LifecycleJournalError, run_checkpoint.CheckpointError) as exc:
        raise runguard.RetainRunLockError(f"failed to write terminal run receipt: {exc}") from exc
