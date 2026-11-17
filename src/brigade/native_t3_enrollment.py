"""Governed native T3 enrollment: durable custody, never a launch.

A T3 Code parent session drives this verb sequence around its own native
delegate tool. Brigade never calls T3 and records no acceptance here. This
module validates a whole batch, binds each task's seat to a declared native
target, retains the exact delegation input as a digest-addressed asset, and
publishes reference-only enrollment facts.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping

from . import localio, roster as roster_mod, run_batch, run_journal, run_lifecycle, runguard
from . import run_task_journal as journal
from . import run_tasks
from .native_t3_contract import NativeIntentTemplate, NativeTarget

SCHEMA = "brigade.t3_enrollment.v1"
INPUT_SCHEMA = "brigade.t3_delegation_input.v1"
RUN_KIND = "native-t3"
AWAITING_NATIVE_PARENT = "awaiting-native-parent"
REQUEST_KEY_PREFIX = "brigade-t3:"
REQUEST_KEY_DOMAIN = "brigade.t3.request-key.v1"
ENROLLED_STATE = "enrolled"
_MARKER_NAME = "t3-enrollment.json"
_MAX_DIAGNOSTIC = 240
_TARGET_STRING_FIELDS = (
    "origin_environment",
    "origin_thread",
    "target_environment",
    "target_project",
    "provider_instance",
    "driver",
)
_TARGET_REQUIRED = (*_TARGET_STRING_FIELDS, "model", "isolation")
_TARGET_FIELDS = frozenset({*_TARGET_REQUIRED, "options", "runtime_mode", "interaction_mode"})
_OPTION_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]*", re.ASCII)


class EnrollmentError(RuntimeError):
    """A bounded enrollment refusal published before any native invocation."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def _bound(message: str) -> str:
    text = " ".join(str(message).split())
    return text if len(text) <= _MAX_DIAGNOSTIC else text[: _MAX_DIAGNOSTIC - 1] + "."


def awaiting_native_parent(meta: Mapping[str, object]) -> bool:
    """True for a run whose next verb belongs to an external native parent."""
    return meta.get("status") == AWAITING_NATIVE_PARENT


def _length_prefixed(value: str) -> bytes:
    raw = value.encode("utf-8")
    return len(raw).to_bytes(8, "big") + raw


def request_key(run_id: str, task_id: str, attempt_id: str) -> str:
    """Derive the domain-separated native client request key for one attempt.

    The encoding is length-prefixed so no pair of adjacent fields can collide.
    This slice only unit-tests the derivation. No caller submits it yet.
    """
    if not journal.token(run_id) or not journal.task_id(task_id) or not journal.digest(attempt_id):
        raise ValueError("invalid request key identity")
    material = b"".join(_length_prefixed(value) for value in (REQUEST_KEY_DOMAIN, run_id, task_id, attempt_id))
    return REQUEST_KEY_PREFIX + hashlib.sha256(material).hexdigest()


def asset_name(task_id: str) -> str:
    """Return the run-local asset name for one task, without task text."""
    if not journal.task_id(task_id):
        raise ValueError("invalid task identity")
    return "native-input-" + hashlib.sha256(task_id.encode("utf-8")).hexdigest() + ".json"


def delegation_input_bytes(*, task_id: str, seat: str, text: str, target: NativeTarget) -> bytes:
    """Encode the exact retained delegation input for one task."""
    payload = {
        "schema": INPUT_SCHEMA,
        "task_id": task_id,
        "seat": seat,
        "text": text,
        "target": asdict(target),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


@dataclass(frozen=True)
class DelegationInput:
    """The retained delegation input, only after digest verification."""

    task_id: str
    seat: str
    text: str
    target: NativeTarget


def _target_string(binding: Mapping[str, object], key: str, *, default: str | None = None) -> str:
    if key not in binding:
        if default is None:
            raise EnrollmentError("invalid-target", f"T3 target field {key} is required")
        return default
    value = binding[key]
    if type(value) is not str or not value:
        raise EnrollmentError("invalid-target", f"T3 target field {key} must be non-empty text")
    return value


def target_for_seat(loaded: roster_mod.Roster, seat: str, *, source_revision: str) -> NativeTarget:
    """Map one roster seat to its declared native target; declarations grant nothing."""
    binding = loaded.t3_targets.get(seat)
    if binding is None:
        raise EnrollmentError("seat-unbound", f"seat {seat!r} has no declared T3 target")
    if set(binding) - _TARGET_FIELDS:
        raise EnrollmentError("invalid-target", f"seat {seat!r} T3 target has an undocumented field")
    if any(key not in binding for key in _TARGET_REQUIRED):
        raise EnrollmentError("invalid-target", f"seat {seat!r} T3 target is missing a required field")
    raw_options = binding.get("options", {})
    if not isinstance(raw_options, dict):
        raise EnrollmentError("invalid-target", f"seat {seat!r} T3 target options must be a table")
    options: list[tuple[str, str | bool]] = []
    for key, value in raw_options.items():
        if type(key) is not str or not _OPTION_KEY.fullmatch(key) or type(value) not in (str, bool):
            raise EnrollmentError("invalid-target", f"seat {seat!r} T3 target has an invalid option")
        options.append((key, value))
    model = binding["model"]
    if type(model) is not str or not model:
        raise EnrollmentError("invalid-target", f"seat {seat!r} T3 target model must be non-empty text")
    try:
        target = NativeTarget(
            **{key: _target_string(binding, key) for key in _TARGET_STRING_FIELDS},
            launch_model=model,
            options=tuple(options),
            runtime_mode=_target_string(binding, "runtime_mode", default="app-server"),
            interaction_mode=_target_string(binding, "interaction_mode", default="default"),
            isolation=_target_string(binding, "isolation"),
            source_revision=source_revision,
        )
    except ValueError:
        raise EnrollmentError("invalid-target", f"seat {seat!r} T3 target binding is invalid") from None
    if target.isolation != "worktree":
        raise EnrollmentError("target-isolation", f"seat {seat!r} T3 target must use worktree isolation")
    return target


@dataclass(frozen=True)
class EnrollmentTask:
    task_id: str
    seat: str
    target: NativeTarget
    template: NativeIntentTemplate
    asset_path: str
    asset_digest: str
    content: bytes


@dataclass(frozen=True)
class EnrollmentPlan:
    workspace: Path
    run_id: str
    request_id: str
    run_dir: Path
    fingerprint: str
    source_revision: str
    entries: tuple[EnrollmentTask, ...]


def _clean_revision(workspace: Path) -> str:
    from .fleet_t3_transport import SourceRevisionError, clean_source_revision

    try:
        return clean_source_revision(workspace, ignore_prefixes=(".brigade/",))
    except SourceRevisionError as exc:
        code = "source-dirty" if exc.code == "source-dirty" else "source-unavailable"
        raise EnrollmentError(code, "the workspace source revision is not usable") from exc


def _load_roster(workspace: Path, roster_path: Path | None) -> roster_mod.Roster:
    try:
        resolution = roster_mod.resolve_roster(workspace, roster_path)
        return roster_mod.load_roster(resolution.path, resolution=resolution)
    except FileNotFoundError:
        raise EnrollmentError("roster-missing", "no roster applies to the target") from None
    except (OSError, ValueError):
        raise EnrollmentError("roster-invalid", "the roster could not be read") from None


def plan_enrollment(*, workspace: Path, batch_path: Path, roster_path: Path | None = None) -> EnrollmentPlan:
    """Validate every refusal before any run state is written."""
    source_revision = _clean_revision(workspace)
    payload = run_batch.load_batch(batch_path)
    canonical = run_batch.validate_batch(payload)
    needs_base = any(task["isolation"] == "worktree" for task in canonical["tasks"])
    plan = run_batch.plan_batch(payload, base_sha=source_revision if needs_base else None)
    loaded = _load_roster(workspace, roster_path)
    targets: dict[str, NativeTarget] = {}
    for task in canonical["tasks"]:
        seat = task["seat"]
        if seat not in loaded.agents:
            raise EnrollmentError("unknown-seat", f"seat {seat!r} is not in the roster")
        if seat not in targets:
            targets[seat] = target_for_seat(loaded, seat, source_revision=source_revision)
    run_id = "t3-" + hashlib.sha256(canonical["request_id"].encode("utf-8")).hexdigest()
    run_dir = runguard.lock_path(workspace).parent / "runs" / run_id
    entries = []
    for task in canonical["tasks"]:
        target = targets[task["seat"]]
        payload_digest = hashlib.sha256(task["text"].encode("utf-8")).hexdigest()
        template = NativeIntentTemplate(
            run_id=run_id,
            task_id=task["task_id"],
            seat=task["seat"],
            target=target,
            payload_digest=payload_digest,
            source_revision=source_revision,
        )
        content = delegation_input_bytes(task_id=task["task_id"], seat=task["seat"], text=task["text"], target=target)
        entries.append(
            EnrollmentTask(
                task_id=task["task_id"],
                seat=task["seat"],
                target=target,
                template=template,
                asset_path=asset_name(task["task_id"]),
                asset_digest=hashlib.sha256(content).hexdigest(),
                content=content,
            )
        )
    return EnrollmentPlan(
        workspace=workspace,
        run_id=run_id,
        request_id=canonical["request_id"],
        run_dir=run_dir,
        fingerprint=plan["fingerprint"],
        source_revision=source_revision,
        entries=tuple(entries),
    )


@contextmanager
def owned_run(workspace: Path, run_dir: Path) -> Iterator[None]:
    """Hold the workspace run lock for exactly one enrollment transaction.

    Ownership is per invocation. No token is persisted and no long-lived owner
    process is imitated. Between verbs the run has no live lock holder, which
    is the normal externally controlled state rather than a stale run.
    """
    try:
        with runguard.run_lock(workspace, run_dir=run_dir, wait_seconds=0, stale_action="claim"):
            if not runguard.is_active_run_owner(workspace, run_dir):
                raise EnrollmentError("run-owner", "run ownership was not established")
            yield
    except runguard.RunLockError as exc:
        raise EnrollmentError("run-busy", "another lifecycle holds the workspace run lock") from exc
    except runguard.RunGuardError as exc:
        raise EnrollmentError("run-lock", "the workspace run lock is unavailable") from exc


def _initialize_run(plan: EnrollmentPlan) -> None:
    from . import aboyeur

    plan.run_dir.mkdir(parents=True, exist_ok=True)
    run_journal._fsync_directory(plan.run_dir.parent)
    run_json = plan.run_dir / "run.json"
    if not run_json.is_file():
        aboyeur._write_json(
            run_json,
            {
                "schema": "brigade.run.v1",
                "kind": RUN_KIND,
                "status": AWAITING_NATIVE_PARENT,
                "task": "Native T3 enrollment",
                "started_at": localio.utc_now_iso(),
                "lock_workspace": str(plan.workspace),
                "lifecycle_journal_requested": True,
            },
        )
    run_lifecycle.prepare_lifecycle_journal(plan.run_dir, workspace=plan.workspace)


def _check_marker(plan: EnrollmentPlan) -> None:
    path = plan.run_dir / _MARKER_NAME
    expected = {
        "schema": SCHEMA,
        "request_id": plan.request_id,
        "fingerprint": plan.fingerprint,
        "source_revision": plan.source_revision,
    }
    if not path.is_file():
        localio.write_json(path, expected)
        run_journal._fsync_directory(plan.run_dir)
        return
    if localio.read_json_dict(path) != expected:
        raise EnrollmentError("request-conflict", "request_id already belongs to a different enrollment")


def write_delegation_asset(run_dir: Path, name: str, content: bytes) -> None:
    """Publish one owner-only asset atomically, refusing to replace a file."""
    if not journal.native_asset_path(name):
        raise EnrollmentError("asset-name", "delegation input asset name is invalid")
    path = run_dir / name
    try:
        localio.write_text_exclusive(path, content.decode("utf-8"))
    except FileExistsError:
        raise EnrollmentError("asset-conflict", "delegation input asset already exists") from None
    except (OSError, UnicodeDecodeError):
        raise EnrollmentError("asset-write", "delegation input asset could not be written") from None
    try:
        os.chmod(path, 0o600)
    except OSError:
        raise EnrollmentError("asset-write", "delegation input asset permissions were not applied") from None
    run_journal._fsync_directory(run_dir)


def _publish_assets(plan: EnrollmentPlan) -> None:
    for entry in plan.entries:
        path = plan.run_dir / entry.asset_path
        try:
            existing = run_journal.bound_read_bytes(path)
        except FileNotFoundError:
            write_delegation_asset(plan.run_dir, entry.asset_path, entry.content)
        else:
            if existing != entry.content:
                raise EnrollmentError("asset-conflict", "retained delegation input already differs")


def _accept(plan: EnrollmentPlan) -> None:
    specs = tuple(
        run_tasks.TaskSpec(entry.task_id, entry.seat, entry.template.payload_digest) for entry in plan.entries
    )
    run_tasks.accept_task_batch(plan.run_dir, workspace=plan.workspace, request_id=plan.request_id, specs=specs)


def _record(plan: EnrollmentPlan) -> None:
    for entry in plan.entries:
        run_tasks.record_native_enrollment(
            plan.run_dir,
            workspace=plan.workspace,
            task_id=entry.task_id,
            enrollment=run_tasks.TaskEnrollment(
                descriptor_version=journal.NATIVE_ENROLLMENT_DESCRIPTOR_VERSION,
                asset_path=entry.asset_path,
                asset_digest=entry.asset_digest,
                template_digest=entry.template.template_digest,
            ),
        )


def report(plan: EnrollmentPlan) -> dict[str, Any]:
    """Return the bounded public enrollment report, never input content."""
    return {
        "schema": SCHEMA,
        "run_id": plan.run_id,
        "tasks": [
            {
                "task_id": entry.task_id,
                "seat": entry.seat,
                "state": ENROLLED_STATE,
                "target": {"provider_instance": entry.target.provider_instance, "model": entry.target.launch_model},
            }
            for entry in plan.entries
        ],
    }


def enroll(*, batch_path: Path, target: Path, roster_path: Path | None = None) -> dict[str, Any]:
    """Enroll one validated batch; a replay of the same request is idempotent."""
    workspace = target.expanduser().resolve()
    plan = plan_enrollment(workspace=workspace, batch_path=batch_path, roster_path=roster_path)
    try:
        with owned_run(workspace, plan.run_dir):
            _initialize_run(plan)
            _check_marker(plan)
            _publish_assets(plan)
            _accept(plan)
            _record(plan)
    except journal.TaskJournalError as exc:
        raise EnrollmentError("journal-refused", "the task journal refused enrollment") from exc
    except (run_lifecycle.LifecycleJournalError, run_journal.RunJournalError) as exc:
        raise EnrollmentError("journal-refused", "the run journal refused enrollment") from exc
    except OSError as exc:
        raise EnrollmentError("io-refused", "run state could not be written") from exc
    return report(plan)


def read_delegation_input(run_dir: Path, *, task_id: str) -> DelegationInput:
    """Read one retained asset back, refusing any digest mismatch."""
    result = next(
        (item for item in run_tasks.read_tasks(run_dir) if item.handle.spec.task_id == task_id),
        None,
    )
    if result is None or result.enrollment is None:
        raise EnrollmentError("not-enrolled", "task has no native enrollment record")
    path = run_dir / result.enrollment.asset_path
    if not run_journal.bound_is_file(path):
        raise EnrollmentError("asset-missing", "retained delegation input is missing")
    raw = run_journal.bound_read_bytes(path)
    if hashlib.sha256(raw).hexdigest() != result.enrollment.asset_digest:
        raise EnrollmentError("asset-digest", "retained delegation input failed digest verification")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise EnrollmentError("asset-invalid", "retained delegation input is not valid JSON") from None
    if not isinstance(payload, dict) or payload.get("schema") != INPUT_SCHEMA or payload.get("task_id") != task_id:
        raise EnrollmentError("asset-invalid", "retained delegation input is not this task's input")
    seat, text, raw_target = payload.get("seat"), payload.get("text"), payload.get("target")
    if type(seat) is not str or type(text) is not str or not isinstance(raw_target, dict):
        raise EnrollmentError("asset-invalid", "retained delegation input is incomplete")
    try:
        target = NativeTarget(
            **{key: raw_target[key] for key in (*_TARGET_STRING_FIELDS, "runtime_mode", "interaction_mode")},
            launch_model=raw_target["launch_model"],
            options=tuple(tuple(pair) for pair in raw_target["options"]),
            isolation=raw_target["isolation"],
            source_revision=raw_target["source_revision"],
        )
    except (KeyError, TypeError, ValueError):
        raise EnrollmentError("asset-invalid", "retained delegation input target is invalid") from None
    return DelegationInput(task_id=task_id, seat=seat, text=text, target=target)


def open_cli(args: Any) -> int:
    """Run ``brigade t3 open``: JSON on success, bounded refusals on stderr."""
    try:
        result = enroll(batch_path=args.batch_file, target=args.target, roster_path=args.roster)
    except run_batch.BatchError as exc:
        print(f"error [{exc.code}]: {_bound(str(exc))}", file=sys.stderr)
        return 2
    except EnrollmentError as exc:
        print(f"error [{exc.code}]: {_bound(str(exc))}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0
