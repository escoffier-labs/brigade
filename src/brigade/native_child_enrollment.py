"""Shared preparation and private custody for native-child batch enrollment."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from . import localio, native_child_capacity, native_child_custody, run_batch, run_journal, run_lifecycle, run_tasks
from . import runguard
from .source_revision import SourceRevisionError, clean_source_revision
from .native_child_host import NativeChildCustodyError as EnrollmentError
from .native_child_host import NativeChildHost
from .run_task_journal import NATIVE_ENROLLMENT_DESCRIPTOR_VERSION, task_id


class UnsupportedTasksError(EnrollmentError):
    """A batch-wide structural refusal with one bounded token per affected task."""

    def __init__(self, code: str, message: str, entries: tuple[tuple[str, str], ...]) -> None:
        self.entries = entries
        super().__init__(code, message)


@dataclass(frozen=True)
class EnrollmentAsset:
    spec: run_tasks.TaskSpec
    path: str
    content: bytes
    template_digest: str


def clean_revision(workspace: Path) -> str:
    try:
        return clean_source_revision(workspace, ignore_prefixes=(".brigade/",))
    except SourceRevisionError as exc:
        code = "source-dirty" if exc.code == "source-dirty" else "source-unavailable"
        raise EnrollmentError(code, "the workspace source revision is not usable") from exc


def validate_batch(batch_path: Path) -> tuple[object, dict[str, Any]]:
    payload = run_batch.load_batch(batch_path)
    canonical = run_batch.validate_batch(payload)
    for task in canonical["tasks"]:
        if task["isolation"] != "worktree":
            raise EnrollmentError("isolation-required", f"task {task['task_id']} must declare worktree isolation")
    if canonical["synthesis"] is not None:
        raise EnrollmentError("synthesis-unsupported", "batch synthesis is not part of native enrollment")
    return payload, canonical


def run_location(host: NativeChildHost, workspace: Path, request_id: str) -> tuple[str, Path]:
    run_id = host.run_prefix + hashlib.sha256(request_id.encode("utf-8")).hexdigest()
    return run_id, native_child_capacity.runs_root(workspace) / run_id


def asset_name(identity: str) -> str:
    if not task_id(identity):
        raise ValueError("invalid task identity")
    return "native-input-" + hashlib.sha256(identity.encode("utf-8")).hexdigest() + ".json"


def input_bytes(schema: str, *, task_id: str, seat: str, text: str, target: Any) -> bytes:
    payload = {"schema": schema, "task_id": task_id, "seat": seat, "text": text, "target": asdict(target)}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def read_input(schema: str, run_dir: Path, result: run_tasks.TaskResult) -> tuple[str, str, dict[str, Any]]:
    enrollment = result.enrollment
    if enrollment is None:
        raise EnrollmentError("not-enrolled", "task has no native enrollment record")
    raw = native_child_custody.read_asset(run_dir, enrollment.asset_path)
    if hashlib.sha256(raw).hexdigest() != enrollment.asset_digest:
        raise EnrollmentError("asset-digest", "retained delegation input failed digest verification")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise EnrollmentError("asset-invalid", "retained delegation input is not valid JSON") from None
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != schema
        or payload.get("task_id") != result.handle.spec.task_id
    ):
        raise EnrollmentError("asset-invalid", "retained delegation input is not this task's input")
    seat, text, target = payload.get("seat"), payload.get("text"), payload.get("target")
    if type(seat) is not str or type(text) is not str or not isinstance(target, dict):
        raise EnrollmentError("asset-invalid", "retained delegation input is incomplete")
    return seat, text, target


def initialize_run(host: NativeChildHost, workspace: Path, run_dir: Path, description: str) -> None:
    from . import aboyeur

    native_child_custody.create_run_dir(run_dir)
    run_json = run_dir / "run.json"
    if not run_json.is_file():
        aboyeur._write_json(
            run_json,
            {
                "schema": "brigade.run.v1",
                "kind": host.run_kind,
                "status": runguard.EXTERNALLY_CONTROLLED_STATUS,
                "task": description,
                "started_at": localio.utc_now_iso(),
                "lock_workspace": str(workspace),
                "lifecycle_journal_requested": True,
            },
        )
    run_lifecycle.prepare_lifecycle_journal(run_dir, workspace=workspace)


def check_marker(path: Path, *, schema: str, request_id: str, fingerprint: str, source_revision: str) -> None:
    expected = {
        "schema": schema,
        "request_id": request_id,
        "fingerprint": fingerprint,
        "source_revision": source_revision,
    }
    if not path.is_file():
        localio.write_json(path, expected)
        run_journal._fsync_directory(path.parent)
        return
    if localio.read_json_dict(path) != expected:
        raise EnrollmentError("request-conflict", "request_id already belongs to a different enrollment")


def record_enrollment(workspace: Path, run_dir: Path, request_id: str, assets: tuple[EnrollmentAsset, ...]) -> None:
    run_tasks.accept_task_batch(
        run_dir, workspace=workspace, request_id=request_id, specs=tuple(asset.spec for asset in assets)
    )
    for asset in assets:
        run_tasks.record_native_enrollment(
            run_dir,
            workspace=workspace,
            task_id=asset.spec.task_id,
            enrollment=run_tasks.TaskEnrollment(
                descriptor_version=NATIVE_ENROLLMENT_DESCRIPTOR_VERSION,
                asset_path=asset.path,
                asset_digest=hashlib.sha256(asset.content).hexdigest(),
                template_digest=asset.template_digest,
            ),
        )
