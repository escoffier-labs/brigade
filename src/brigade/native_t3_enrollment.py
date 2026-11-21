"""Governed native T3 enrollment: durable custody, never a launch.

A T3 Code parent session drives this verb sequence around its own native
delegate tool. Brigade never calls T3 and records no acceptance here. This
module validates a whole batch, binds each task's seat to a declared native
target, retains the exact delegation input as a digest-addressed asset, and
publishes reference-only enrollment facts.

Owner-only, descriptor-bound custody of the run directory and its assets is
host neutral and lives in ``native_child_custody``. This module supplies the T3
target declaration, the delegation input encoding and the verbs.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping

from . import native_child_admission, native_child_custody, roster as roster_mod
from . import native_child_enrollment as core
from . import run_batch, run_journal, run_lifecycle, runguard
from . import run_task_journal as journal
from . import run_tasks
from .native_child_host import NativeChildCustodyError as EnrollmentError
from .native_t3_contract import INHERIT_MODE, NativeIntentTemplate, NativeTarget
from .native_t3_host import HOST
from .native_t3_structural import TARGET_MODE_INVALID, TARGET_REMOTE_UNSUPPORTED, UNSUPPORTED, structural_gap

SCHEMA = "brigade.t3_enrollment.v1"
INPUT_SCHEMA = "brigade.t3_delegation_input.v1"
RUN_KIND = HOST.run_kind
AWAITING_NATIVE_PARENT = runguard.EXTERNALLY_CONTROLLED_STATUS
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
_TARGET_FIELDS = frozenset({*_TARGET_REQUIRED, "options", "runtime_mode", "interaction_mode", "max_live"})
_OPTION_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]*", re.ASCII)


class UnsupportedTasksError(EnrollmentError):
    """A batch holding tasks T3 structurally cannot run.

    Every entry names one affected task id and its closed structural token,
    never task text. The whole batch is refused, so nothing is enrolled for the
    tasks that would otherwise have been fine.
    """

    def __init__(self, entries: tuple[tuple[str, str], ...]) -> None:
        self.entries = tuple(entries)
        named = ", ".join(f"{task_id} {token}" for task_id, token in self.entries)
        super().__init__(UNSUPPORTED, f"tasks T3 cannot run: {named}")


@contextmanager
def owned_run(workspace: Path, run_dir: Path) -> Iterator[None]:
    """Hold the workspace run lock for exactly one enrollment transaction."""
    with native_child_custody.owned_run(workspace, run_dir):
        yield


def _load_roster(workspace: Path, roster_path: Path | None) -> roster_mod.Roster:
    return native_child_admission.load_roster(workspace, roster_path)


def _nt_private() -> Any | None:
    """The Windows owner-only helper when this platform can actually bind it."""
    return native_child_custody.nt_private(os.name)


def _require_supported_platform() -> None:
    """Owner-only custody needs POSIX modes or the Windows NT private helper."""
    if not native_child_custody.supported_platform(os.name):
        raise EnrollmentError(
            "unsupported-platform",
            "native T3 enrollment needs owner-only file custody that this platform does not provide",
        )


def write_delegation_asset(run_dir: Path, name: str, content: bytes) -> None:
    """Publish one owner-only asset atomically, refusing to replace a file."""
    _require_supported_platform()
    native_child_custody.write_asset(run_dir, name, content)


def _publish_assets(plan: EnrollmentPlan) -> None:
    native_child_custody.publish_assets(plan.run_dir, [(entry.asset_path, entry.content) for entry in plan.entries])


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


asset_name = core.asset_name


def delegation_input_bytes(*, task_id: str, seat: str, text: str, target: NativeTarget) -> bytes:
    return core.input_bytes(INPUT_SCHEMA, task_id=task_id, seat=seat, text=text, target=target)


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


def _declared_target(loaded: roster_mod.Roster, seat: str, *, source_revision: str) -> NativeTarget:
    """Resolve one declaration without applying the structural-gap classifier.

    Field, option, model and isolation validity stay declaration refusals. The
    mode, locality and driver rules are structural, and the caller applies them
    through ``native_t3_structural.structural_gap`` so one closed decision
    covers submission and claim.
    """
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
    runtime_mode = _target_string(binding, "runtime_mode", default=INHERIT_MODE)
    interaction_mode = _target_string(binding, "interaction_mode", default=INHERIT_MODE)
    try:
        target = NativeTarget(
            **{key: _target_string(binding, key) for key in _TARGET_STRING_FIELDS},
            launch_model=model,
            options=tuple(options),
            runtime_mode=runtime_mode,
            interaction_mode=interaction_mode,
            isolation=_target_string(binding, "isolation"),
            source_revision=source_revision,
        )
    except ValueError:
        raise EnrollmentError("invalid-target", f"seat {seat!r} T3 target binding is invalid") from None
    if target.isolation != "worktree":
        raise EnrollmentError("target-isolation", f"seat {seat!r} T3 target must use worktree isolation")
    return target


def target_for_seat(loaded: roster_mod.Roster, seat: str, *, source_revision: str) -> NativeTarget:
    """Map one roster seat to its declared native target; declarations grant nothing.

    A mode or locality gap raises under its own token, which is also the
    structural token the caller reports. A driver gap stays resolvable here so
    admission, rather than declaration parsing, decides it.
    """
    target = _declared_target(loaded, seat, source_revision=source_revision)
    gap = structural_gap(target)
    if gap == TARGET_MODE_INVALID:
        raise EnrollmentError(gap, f"seat {seat!r} T3 target modes must be T3 literals or inherit")
    if gap == TARGET_REMOTE_UNSUPPORTED:
        raise EnrollmentError(
            gap,
            f"seat {seat!r} T3 target must be local because cross-machine governed targets are unsupported",
        )
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


_clean_revision = core.clean_revision


def plan_enrollment(*, workspace: Path, batch_path: Path, roster_path: Path | None = None) -> EnrollmentPlan:
    """Validate every refusal before any run state is written."""
    payload, canonical = core.validate_batch(batch_path)
    source_revision = _clean_revision(workspace)
    plan = run_batch.plan_batch(payload, base_sha=source_revision)
    loaded = _load_roster(workspace, roster_path)
    targets: dict[str, NativeTarget] = {}
    gaps: dict[str, str] = {}
    for task in canonical["tasks"]:
        seat = task["seat"]
        if seat not in loaded.agents:
            raise EnrollmentError("unknown-seat", f"seat {seat!r} is not in the roster")
        if seat in targets or seat in gaps:
            continue
        target = _declared_target(loaded, seat, source_revision=source_revision)
        gap = structural_gap(target)
        if gap is None:
            targets[seat] = target
        else:
            gaps[seat] = gap
    if gaps:
        # Classify the whole batch before writing anything, and name only the
        # affected caller-chosen task ids with their structural tokens.
        raise UnsupportedTasksError(
            tuple((task["task_id"], gaps[task["seat"]]) for task in canonical["tasks"] if task["seat"] in gaps)
        )
    run_id, run_dir = core.run_location(HOST, workspace, canonical["request_id"])
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
    _require_supported_platform()
    workspace = target.expanduser().resolve()
    plan = plan_enrollment(workspace=workspace, batch_path=batch_path, roster_path=roster_path)
    try:
        with owned_run(workspace, plan.run_dir):
            core.initialize_run(HOST, workspace, plan.run_dir, "Native T3 enrollment")
            core.check_marker(
                plan.run_dir / _MARKER_NAME,
                schema=SCHEMA,
                request_id=plan.request_id,
                fingerprint=plan.fingerprint,
                source_revision=plan.source_revision,
            )
            _publish_assets(plan)
            assets = tuple(
                core.EnrollmentAsset(
                    run_tasks.TaskSpec(entry.task_id, entry.seat, entry.template.payload_digest),
                    entry.asset_path,
                    entry.content,
                    entry.template.template_digest,
                )
                for entry in plan.entries
            )
            core.record_enrollment(workspace, plan.run_dir, plan.request_id, assets)
    except journal.TaskJournalError as exc:
        raise EnrollmentError("journal-refused", "the task journal refused enrollment") from exc
    except (run_lifecycle.LifecycleJournalError, run_journal.RunJournalError) as exc:
        raise EnrollmentError("journal-refused", "the run journal refused enrollment") from exc
    except OSError as exc:
        raise EnrollmentError("io-refused", "run state could not be written") from exc
    return report(plan)


def read_delegation_input(run_dir: Path, *, task_id: str) -> DelegationInput:
    """Read one retained asset back, refusing any digest or custody mismatch."""
    _require_supported_platform()
    result = next(
        (item for item in run_tasks.read_tasks(run_dir) if item.handle.spec.task_id == task_id),
        None,
    )
    if result is None:
        raise EnrollmentError("not-enrolled", "task has no native enrollment record")
    seat, text, raw_target = core.read_input(INPUT_SCHEMA, run_dir, result)
    # A retained target is read back exactly as enrolled, including a legacy
    # `app-server` mode. New declarations are classified at open and a claim
    # re-applies the same closed structural decision in admission.evaluate.
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
    except UnsupportedTasksError as exc:
        # One fixed line per affected task, naming only its caller-chosen id
        # and the closed structural token. Task text never enters a refusal.
        for task_id, token in exc.entries:
            print(f"error [{exc.code}]: task {task_id} {token}", file=sys.stderr)
        return 2
    except EnrollmentError as exc:
        print(f"error [{exc.code}]: {_bound(str(exc))}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0
