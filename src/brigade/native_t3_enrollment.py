"""Governed native T3 enrollment: durable custody, never a launch.

A T3 Code parent session drives this verb sequence around its own native
delegate tool. Brigade never calls T3 and records no acceptance here. This
module validates a whole batch, binds each task's seat to a declared native
target, retains the exact delegation input as a digest-addressed asset, and
publishes reference-only enrollment facts.

Custody is descriptor-bound. The run directory is bound before any asset read,
each asset is created with exclusive no-follow creation in owner-only mode, and
publish replay plus readback require a single-link regular file with mode 0600.
Windows binds a protected owner-only DACL at creation through
``work_cmd.nt_private`` and verifies it on the held handle, because NTFS has no
POSIX mode bits. Statements about administrators, backup and restore rights stay
outside this discretionary access boundary.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import stat
import sys
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping

from . import dirfd, localio, roster as roster_mod, run_batch, run_dirfd, run_journal, run_lifecycle, runguard
from . import run_task_journal as journal
from . import run_tasks
from .native_t3_contract import (
    INHERIT_MODE,
    INTERACTION_MODES,
    RUNTIME_MODES,
    NativeIntentTemplate,
    NativeTarget,
)

SCHEMA = "brigade.t3_enrollment.v1"
INPUT_SCHEMA = "brigade.t3_delegation_input.v1"
RUN_KIND = "native-t3"
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
    runtime_mode = _target_string(binding, "runtime_mode", default=INHERIT_MODE)
    interaction_mode = _target_string(binding, "interaction_mode", default=INHERIT_MODE)
    if runtime_mode not in RUNTIME_MODES | {INHERIT_MODE}:
        raise EnrollmentError(
            "target-mode-invalid",
            f"seat {seat!r} T3 target runtime_mode must be a T3 runtime literal or inherit",
        )
    if interaction_mode not in INTERACTION_MODES | {INHERIT_MODE}:
        raise EnrollmentError(
            "target-mode-invalid",
            f"seat {seat!r} T3 target interaction_mode must be a T3 interaction literal or inherit",
        )
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
    if target.target_environment != target.origin_environment:
        raise EnrollmentError(
            "target-remote-unsupported",
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
    payload = run_batch.load_batch(batch_path)
    canonical = run_batch.validate_batch(payload)
    for task in canonical["tasks"]:
        if task["isolation"] != "worktree":
            raise EnrollmentError("isolation-required", f"task {task['task_id']} must declare worktree isolation")
    if canonical["synthesis"] is not None:
        raise EnrollmentError("synthesis-unsupported", "batch synthesis is not part of native enrollment")
    source_revision = _clean_revision(workspace)
    plan = run_batch.plan_batch(payload, base_sha=source_revision)
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

    _create_run_dir(plan.run_dir)
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


def _nt_private() -> Any | None:
    """The Windows owner-only helper when this platform can actually bind it."""
    if os.name != "nt" or not dirfd.nt_available():
        return None
    try:
        from .work_cmd import nt_private

        # Bind both lazy API namespaces now, so a missing DLL or symbol refuses
        # before any lock or run state is written.
        nt_private._security_api()
        nt_private.nt._require_api()
    except (ImportError, OSError, AttributeError):
        return None
    return nt_private


def _windows_private_helper() -> Any:
    helper = _nt_private()
    if helper is None:
        raise EnrollmentError(
            "unsupported-platform",
            "native T3 enrollment needs owner-only file custody that this platform does not provide",
        )
    return helper


def _create_windows_run_dir(run_dir: Path) -> None:
    """Create or reopen the run directory under a protected owner-only DACL."""
    helper = _windows_private_helper()
    try:
        parent = dirfd.open_directory_nofollow(run_dir.parent)
    except OSError:
        raise EnrollmentError("run-dir", "the run directory is not bindable") from None
    try:
        try:
            descriptor = helper.open_child_directory(parent, run_dir.name, create=True)
        except FileExistsError:
            # An existing object is inspected, never repaired. Verification is
            # part of the reopen, so an inherited or widened DACL refuses here.
            try:
                descriptor = helper.open_child_directory(parent, run_dir.name)
            except OSError:
                raise EnrollmentError("run-dir", "the run directory custody is not owner-only") from None
        except OSError:
            raise EnrollmentError("run-dir", "the run directory could not be created owner-only") from None
        try:
            try:
                helper.verify_fd(descriptor, directory=True)
            except OSError:
                raise EnrollmentError("run-dir", "the run directory custody is not owner-only") from None
            if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise EnrollmentError("run-dir", "the run directory is not a directory")
        finally:
            os.close(descriptor)
    finally:
        os.close(parent)
    run_journal._fsync_directory(run_dir.parent)


def _create_run_dir(run_dir: Path) -> None:
    """Create the run directory owner-only, refusing a symlink at the name."""
    run_dir.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        _create_windows_run_dir(run_dir)
        return
    run_dir.mkdir(mode=0o700, exist_ok=True)
    try:
        descriptor = dirfd.open_directory_nofollow(run_dir)
    except OSError:
        raise EnrollmentError("run-dir", "the run directory is not bindable") from None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode):
            raise EnrollmentError("run-dir", "the run directory is not a directory")
        try:
            os.fchmod(descriptor, 0o700)
        except (AttributeError, OSError):
            # Windows has no POSIX mode bits. Creation mode stays owner-only.
            pass
    finally:
        os.close(descriptor)
    run_journal._fsync_directory(run_dir.parent)


def _require_supported_platform() -> None:
    """Owner-only custody needs POSIX modes or the Windows NT private helper."""
    if os.name == "nt" and _nt_private() is None:
        raise EnrollmentError(
            "unsupported-platform",
            "native T3 enrollment needs owner-only file custody that this platform does not provide",
        )


def _private_asset(info: os.stat_result) -> bool:
    """Owner-only single-link regular file: the custody shape we can trust."""
    if os.name == "nt":
        # NTFS has no POSIX mode bits. The owner-only DACL is verified on the
        # held handle; this shape check still refuses links and non-files.
        return stat.S_ISREG(info.st_mode) and info.st_nlink == 1
    return stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and stat.S_IMODE(info.st_mode) == 0o600


def _require_private_asset(info: os.stat_result) -> None:
    if not _private_asset(info):
        raise EnrollmentError("asset-custody", "delegation input asset is not a private regular file")


def _discard_held_asset(descriptor: int, name: str) -> None:
    """Mark the opened asset for deletion without re-resolving its name.

    The handle carries DELETE, so the mark applies to the exact object that was
    verified. A swapped directory entry cannot redirect it. When the mark fails,
    the empty object stays for inspection, which is the fail-closed alternative
    to an identity-free unlink.
    """
    try:
        from .work_cmd import nt_dirfd

        api = nt_dirfd._require_api()
        disposition = api.FILE_DISPOSITION_INFORMATION()
        disposition.DeleteFile = True
        nt_dirfd._nt_set_info(
            api,
            nt_dirfd._handle_from_fd(descriptor),
            ctypes.byref(disposition),
            ctypes.sizeof(disposition),
            nt_dirfd._FileDispositionInformation,
            name=name,
        )
    except (OSError, AttributeError):
        return


def _create_windows_asset(bound: run_dirfd.BoundRunDir, name: str, content: bytes) -> None:
    """Create one asset, verify its DACL, then write and flush the bytes."""
    helper = _windows_private_helper()
    try:
        descriptor = helper.open_file(bound.dir_fd(), name, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    except FileExistsError:
        raise EnrollmentError("asset-conflict", "delegation input asset already exists") from None
    except PermissionError:
        # open_file verified the new object, refused it and closed its handle.
        # Without a held identity the object is left for inspection, not unlinked.
        raise EnrollmentError("asset-custody", "delegation input asset custody was not established") from None
    except OSError:
        raise EnrollmentError("asset-write", "delegation input asset could not be created") from None
    complete = False
    try:
        try:
            helper.verify_fd(descriptor, directory=False)
        except OSError:
            raise EnrollmentError("asset-custody", "delegation input asset custody was not established") from None
        if not _private_asset(os.fstat(descriptor)):
            raise EnrollmentError("asset-custody", "delegation input asset custody was not established")
        try:
            with os.fdopen(os.dup(descriptor), "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:
            raise EnrollmentError("asset-write", "delegation input asset could not be written") from None
        complete = True
    finally:
        if not complete:
            _discard_held_asset(descriptor, name)
        os.close(descriptor)
    dirfd.fsync_directory(bound.dir_fd())


def _create_asset(bound: run_dirfd.BoundRunDir, name: str, content: bytes) -> None:
    """Publish one asset with exclusive no-follow creation and owner-only custody."""
    if os.name == "nt":
        _create_windows_asset(bound, name, content)
        return
    try:
        descriptor = bound.open_file((), name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise EnrollmentError("asset-conflict", "delegation input asset already exists") from None
    except OSError:
        raise EnrollmentError("asset-write", "delegation input asset could not be created") from None
    info: os.stat_result | None = None
    try:
        try:
            os.fchmod(descriptor, 0o600)
        except (AttributeError, OSError):
            pass
        try:
            with os.fdopen(os.dup(descriptor), "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:
            raise EnrollmentError("asset-write", "delegation input asset could not be written") from None
        info = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if info is None or not _private_asset(info):
        try:
            dirfd.unlink_child(bound.dir_fd(), name)
        except OSError:
            pass
        raise EnrollmentError("asset-custody", "delegation input asset custody was not established")
    dirfd.fsync_directory(bound.dir_fd())


def write_delegation_asset(run_dir: Path, name: str, content: bytes) -> None:
    """Publish one owner-only asset atomically, refusing to replace a file."""
    _require_supported_platform()
    if not journal.native_asset_path(name):
        raise EnrollmentError("asset-name", "delegation input asset name is invalid")
    try:
        with run_dirfd.bound_run_dir(run_dir) as bound:
            _create_asset(bound, name, content)
            if not bound.still_bound():
                raise EnrollmentError("asset-custody", "run directory changed during asset publish")
    except OSError:
        raise EnrollmentError("asset-custody", "run directory could not be bound") from None


def _read_windows_asset(bound: run_dirfd.BoundRunDir, name: str, *, missing_ok: bool) -> bytes | None:
    """Open the asset once, verify custody on that handle, then read it."""
    helper = _windows_private_helper()
    try:
        descriptor = helper.open_file(bound.dir_fd(), name, os.O_RDONLY)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise EnrollmentError("asset-missing", "retained delegation input is missing") from None
    except OSError:
        raise EnrollmentError("asset-custody", "delegation input asset is not a private regular file") from None
    try:
        try:
            info = os.fstat(descriptor)
        except OSError:
            raise EnrollmentError("asset-custody", "delegation input asset custody was not established") from None
        if not _private_asset(info):
            raise EnrollmentError("asset-custody", "delegation input asset is not a private regular file")
        try:
            helper.verify_fd(descriptor, directory=False)
        except OSError:
            raise EnrollmentError("asset-custody", "delegation input asset custody was not established") from None
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > run_dirfd.MAX_READ_BYTES:
                raise EnrollmentError("asset-custody", "delegation input asset exceeds the read limit")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _existing_asset(bound: run_dirfd.BoundRunDir, name: str, content: bytes) -> bool:
    """True when an already published asset matches; refuses unsafe custody."""
    if os.name == "nt":
        existing = _read_windows_asset(bound, name, missing_ok=True)
        if existing is None:
            return False
        if existing != content:
            raise EnrollmentError("asset-conflict", "retained delegation input already differs")
        return True
    try:
        info = bound.stat((), name)
    except FileNotFoundError:
        return False
    _require_private_asset(info)
    existing = bound.read_bytes(name)
    if existing is None:
        raise EnrollmentError("asset-missing", "retained delegation input disappeared")
    if existing != content:
        raise EnrollmentError("asset-conflict", "retained delegation input already differs")
    return True


def _publish_assets(plan: EnrollmentPlan) -> None:
    try:
        with run_dirfd.bound_run_dir(plan.run_dir) as bound:
            for entry in plan.entries:
                if not _existing_asset(bound, entry.asset_path, entry.content):
                    _create_asset(bound, entry.asset_path, entry.content)
            if not bound.still_bound():
                raise EnrollmentError("asset-custody", "run directory changed during enrollment")
    except OSError:
        raise EnrollmentError("asset-custody", "run directory could not be bound") from None


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
    _require_supported_platform()
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
    """Read one retained asset back, refusing any digest or custody mismatch."""
    _require_supported_platform()
    result = next(
        (item for item in run_tasks.read_tasks(run_dir) if item.handle.spec.task_id == task_id),
        None,
    )
    if result is None or result.enrollment is None:
        raise EnrollmentError("not-enrolled", "task has no native enrollment record")
    name = result.enrollment.asset_path
    try:
        with run_dirfd.bound_run_dir(run_dir) as bound:
            if os.name == "nt":
                read = _read_windows_asset(bound, name, missing_ok=False)
                if read is None:
                    raise EnrollmentError("asset-missing", "retained delegation input is missing")
                raw = read
            else:
                try:
                    info = bound.stat((), name)
                except FileNotFoundError:
                    raise EnrollmentError("asset-missing", "retained delegation input is missing") from None
                _require_private_asset(info)
                existing = bound.read_bytes(name)
                if existing is None:
                    raise EnrollmentError("asset-missing", "retained delegation input is missing")
                raw = existing
            if not bound.still_bound():
                raise EnrollmentError("asset-custody", "run directory changed during readback")
    except OSError:
        raise EnrollmentError("asset-custody", "run directory could not be bound") from None
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
    runtime_mode = raw_target.get("runtime_mode")
    interaction_mode = raw_target.get("interaction_mode")
    if (
        type(runtime_mode) is not str
        or runtime_mode not in RUNTIME_MODES | {INHERIT_MODE}
        or type(interaction_mode) is not str
        or interaction_mode not in INTERACTION_MODES | {INHERIT_MODE}
    ):
        raise EnrollmentError(
            "target-mode-invalid",
            "retained delegation input target uses a mode T3 does not accept",
        )
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
