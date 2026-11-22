"""Orca target enrollment and verification of the private task input."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from . import native_child_admission, native_child_custody, run_batch, run_journal, run_lifecycle, run_tasks
from . import native_child_enrollment as core
from . import native_child_journal as journal
from .native_child_host import NativeChildCustodyError
from .native_orca_host import HOST, NativeTarget, declared_target, structural_gap

SCHEMA = "brigade.orca_enrollment.v1"
INPUT_SCHEMA = "brigade.orca_delegation_input.v1"
_TEMPLATE_DOMAIN = "brigade.native-orca.template.v1"


def template_digest(run_id: str, spec: run_tasks.TaskSpec, target: NativeTarget) -> str:
    return journal.digest({"run_id": run_id, "spec": asdict(spec), "target": asdict(target)}, _TEMPLATE_DOMAIN)


def enroll(*, workspace: Path, batch_path: Path, roster_path: Path | None = None) -> dict[str, Any]:
    if not native_child_custody.supported_platform():
        raise journal.refuse("unsupported-platform", "native Orca enrollment needs owner-only file custody")
    workspace = workspace.expanduser().resolve()
    payload, canonical = core.validate_batch(batch_path)
    revision = core.clean_revision(workspace)
    plan = run_batch.plan_batch(payload, base_sha=revision)
    roster = native_child_admission.load_roster(workspace, roster_path)
    targets: dict[str, NativeTarget] = {}
    unsupported = []
    request_id = canonical["request_id"]
    run_id, run_dir = core.run_location(HOST, workspace, request_id)
    assets = []
    for task in canonical["tasks"]:
        task_id, seat, text = task["task_id"], task["seat"], task["text"]
        if seat not in roster.agents:
            raise journal.refuse("unknown-seat", f"seat {seat!r} is not in the roster")
        if seat not in targets:
            targets[seat] = declared_target(roster, seat, source_revision=revision)
        target = targets[seat]
        gap = structural_gap(target)
        if gap is not None:
            unsupported.append((task_id, gap))
            continue
        spec = run_tasks.TaskSpec(task_id, seat, hashlib.sha256(text.encode("utf-8")).hexdigest())
        content = core.input_bytes(INPUT_SCHEMA, task_id=task_id, seat=seat, text=text, target=target)
        assets.append(
            core.EnrollmentAsset(spec, core.asset_name(task_id), content, template_digest(run_id, spec, target))
        )
    if unsupported:
        raise core.UnsupportedTasksError(
            HOST.unsupported_code, "the batch contains unsupported Orca targets", tuple(unsupported)
        )
    try:
        with native_child_custody.owned_run(workspace, run_dir):
            core.initialize_run(HOST, workspace, run_dir, "Native Orca enrollment")
            core.check_marker(
                run_dir / "orca-enrollment.json",
                schema=SCHEMA,
                request_id=request_id,
                fingerprint=plan["fingerprint"],
                source_revision=revision,
            )
            native_child_custody.publish_assets(run_dir, [(asset.path, asset.content) for asset in assets])
            core.record_enrollment(workspace, run_dir, request_id, tuple(assets))
    except (run_tasks.TaskJournalError, run_lifecycle.LifecycleJournalError, run_journal.RunJournalError):
        raise journal.refuse("journal-refused", "the journal refused enrollment") from None
    except OSError:
        raise journal.refuse("io-refused", "run state could not be written") from None
    return {
        "schema": SCHEMA,
        "run_id": run_id,
        "tasks": [
            {
                "task_id": asset.spec.task_id,
                "seat": asset.spec.seat,
                "state": "enrolled",
                "orca_version": targets[asset.spec.seat].orca_version,
            }
            for asset in assets
        ],
    }


@dataclass(frozen=True)
class VerifiedTask:
    result: run_tasks.TaskResult
    target: NativeTarget
    template_digest: str


def verify_task(run_dir: Path, result: run_tasks.TaskResult) -> VerifiedTask:
    seat, _text, target_fields = core.read_input(INPUT_SCHEMA, run_dir, result)
    spec = result.handle.spec
    try:
        target = NativeTarget(**target_fields)
        expected = template_digest(run_dir.name, spec, target)
    except (TypeError, ValueError):
        raise journal.refuse("asset-invalid", "the retained Orca target is invalid") from None
    assert result.enrollment is not None
    if spec.seat != seat or result.enrollment.template_digest != expected:
        raise journal.refuse("target-changed", "the retained asset no longer matches its enrollment binding")
    return VerifiedTask(result, target, expected)


def verify_for_launch(workspace: Path, run_dir: Path) -> dict[str, VerifiedTask]:
    try:
        head = core.clean_revision(workspace)
    except NativeChildCustodyError as exc:
        raise journal.refuse(exc.code, str(exc)) from None
    verified = {}
    for result in journal.read_tasks(run_dir):
        task = verify_task(run_dir, result)
        if task.target.source_revision != head:
            raise journal.refuse("source-changed", "the workspace source revision changed since enrollment")
        verified[result.handle.spec.task_id] = task
    return verified
