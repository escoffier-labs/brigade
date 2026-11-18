"""Prepare, commit, and restore multi-file projection operations.

A planned operation either publishes every destination mutation or restores
every destination to its captured pre-operation state. The user-facing
guarantee is all-or-restored completion when the process can run recovery, not
simultaneous visibility across independent paths.
"""

from __future__ import annotations

import contextlib
import contextvars
import errno
import hashlib
import json
import os
import re
import shutil
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Literal, Mapping

from brigade import dirfd as dirfd_mod
from brigade import localio

SCHEMA_VERSION = 1
ABSENT = "absent"
JOURNAL_SCHEMA = "brigade.projection.journal.v1"
RECEIPT_SCHEMA = "brigade.projection.receipt.v1"
UNFINISHED_STATUSES = frozenset({"prepared", "committing", "restoring", "recovery-required"})
MutationKind = Literal["create", "replace", "remove"]

DEPENDENCY_DECISION: dict[str, Any] = {
    "implementation": "native",
    "runtime_package": None,
    "reason": "standard-library implementation satisfies the issue gate; no runtime dependency admitted",
}

_HOME_TOKEN_RE = re.compile(r"(Authorization\s*:\s*[^\s]+)|(\bBearer\s+[A-Za-z0-9._\-+=/]+)", re.IGNORECASE)


class ProjectionError(RuntimeError):
    """Base class for projection-kernel failures. Carries a bounded diagnostic."""

    def __init__(self, diagnostic: str) -> None:
        super().__init__(diagnostic)
        self.diagnostic = diagnostic


class PlanError(ProjectionError):
    """Plan is invalid and must not start prepare."""


class DriftError(ProjectionError):
    """Live destination state no longer matches the planned before-state."""


class OverlapBlockedError(ProjectionError):
    """A later mutating operation overlaps an unfinished recovery set."""


class DestinationChangedError(ProjectionError):
    """Recovery refused because a destination changed after the failed operation."""


class InjectedFailure(ProjectionError):
    """Test-only failure injected at a named mutation boundary."""


@dataclass(frozen=True)
class MutationSpec:
    destination: Path
    display_path: str
    mutation: MutationKind
    expected_before: str
    desired_after: str
    staged_bytes: bytes | None = None
    mode: int | None = None
    ownership: Mapping[str, Any] | None = None
    conflict: Mapping[str, Any] | None = None
    writer: Callable[[Path, bytes], None] | None = None
    remover: Callable[[Path], None] | None = None
    propagate_error: bool = False


@dataclass(frozen=True)
class Plan:
    operation_id: str
    projector: str
    source_fingerprint: str
    mutations: tuple[MutationSpec, ...]
    validators: tuple[Callable[..., None], ...] = ()


@dataclass(frozen=True)
class OperationSummary:
    operation_id: str
    status: str
    destinations: tuple[Path, ...]


@dataclass
class FailureInjector:
    """Fail at a named commit or restore boundary. Production callers pass None."""

    boundary: str | None = None
    restore_boundary: str | None = None
    error: BaseException | None = None
    crash_after: int | None = None

    def at(self, name: str) -> None:
        if name == self.boundary or name == self.restore_boundary:
            raise self.error or InjectedFailure(name)


@dataclass(frozen=True)
class Receipt:
    schema_version: int
    operation_id: str
    projector: str
    source_digest: str
    destinations: tuple[dict[str, Any], ...]
    validator_results: tuple[dict[str, Any], ...]
    mutation_counts: dict[str, int]
    terminal_state: str
    recovery_command: str | None

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "schema": RECEIPT_SCHEMA,
            "schema_version": self.schema_version,
            "operation_id": self.operation_id,
            "projector": self.projector,
            "source_digest": self.source_digest,
            "destinations": [dict(item) for item in self.destinations],
            "validator_results": [dict(item) for item in self.validator_results],
            "mutation_counts": dict(self.mutation_counts),
            "terminal_state": self.terminal_state,
            "recovery_command": self.recovery_command,
        }
        return json.loads(_redact_text(json.dumps(payload, sort_keys=True)))


def mutation(
    *,
    destination: Path,
    mutation: MutationKind,
    expected_before: str,
    desired_after: str,
    staged_bytes: bytes | None = None,
    display_path: str = "",
    mode: int | None = None,
    ownership: Mapping[str, Any] | None = None,
    conflict: Mapping[str, Any] | None = None,
    writer: Callable[[Path, bytes], None] | None = None,
    remover: Callable[[Path], None] | None = None,
    propagate_error: bool = False,
) -> MutationSpec:
    return MutationSpec(
        destination=Path(destination),
        display_path=display_path,
        mutation=mutation,
        expected_before=expected_before,
        desired_after=desired_after,
        staged_bytes=staged_bytes,
        mode=mode,
        ownership=ownership,
        conflict=conflict,
        writer=writer,
        remover=remover,
        propagate_error=propagate_error,
    )


def content_digest(data: bytes | None) -> str:
    if data is None:
        return ABSENT
    return hashlib.sha256(data).hexdigest()


def operations_root(target: Path) -> Path:
    return target.expanduser().resolve() / ".brigade" / "projection" / "operations"


def operation_dir(target: Path, operation_id: str) -> Path:
    if not operation_id or "/" in operation_id or "\\" in operation_id or ".." in operation_id:
        raise PlanError("unsafe operation id")
    return operations_root(target) / operation_id


def safe_display_path(path: Path, *, target: Path, suggested: str = "") -> str:
    resolved = _physical(path)
    target_resolved = target.expanduser().resolve()
    try:
        return resolved.relative_to(target_resolved).as_posix()
    except ValueError:
        text = suggested or str(resolved)
    home = str(Path.home().expanduser().resolve())
    if text.startswith(home):
        text = "~" + text[len(home) :]
    return text.replace(home, "~")


def build_plan(
    *,
    operation_id: str,
    projector: str,
    source_fingerprint: str,
    mutations: Iterable[MutationSpec],
    target: Path,
    validators: Iterable[Callable[..., None]] = (),
) -> Plan:
    target = target.expanduser().resolve()
    specs = tuple(mutations)
    if not specs:
        raise PlanError("plan has no mutations")
    physical: list[tuple[MutationSpec, Path]] = []
    seen: dict[str, Path] = {}
    for spec in specs:
        requested = spec.destination if spec.destination.is_absolute() else target / spec.destination
        _reject_parent_symlink(requested)
        dest = _physical(requested)
        key = dest.as_posix()
        if key in seen:
            raise PlanError("duplicate destination")
        for existing in seen.values():
            if _parent_child(existing, dest):
                raise PlanError("parent-child destination collision")
        seen[key] = dest
        physical.append((spec, dest))
    normalized: list[MutationSpec] = []
    for spec, dest in physical:
        _reject_symlink_and_type(dest, spec.mutation)
        _validate_mutation_bytes(spec)
        live = _live_digest(dest)
        if live != spec.expected_before:
            raise PlanError("live state does not match expected before-state")
        display = safe_display_path(dest, target=target, suggested=spec.display_path)
        normalized.append(
            MutationSpec(
                destination=dest,
                display_path=display,
                mutation=spec.mutation,
                expected_before=spec.expected_before,
                desired_after=spec.desired_after,
                staged_bytes=spec.staged_bytes,
                mode=spec.mode,
                ownership=spec.ownership,
                conflict=spec.conflict,
                writer=spec.writer,
                remover=spec.remover,
                propagate_error=spec.propagate_error,
            )
        )
    return Plan(
        operation_id=operation_id,
        projector=projector,
        source_fingerprint=source_fingerprint,
        mutations=tuple(normalized),
        validators=tuple(validators),
    )


def execute(plan: Plan, *, target: Path, inject: FailureInjector | None = None) -> Receipt:
    if plan.projector in {"skills", "harness-profile"}:
        from ..skills_cmd.mutation import skill_mutation

        with skill_mutation(target):
            return _execute(plan, target=target, inject=inject)
    return _execute(plan, target=target, inject=inject)


def _execute(plan: Plan, *, target: Path, inject: FailureInjector | None = None) -> Receipt:
    target = target.expanduser().resolve()
    inject = inject or FailureInjector()
    for spec in plan.mutations:
        if _live_digest(spec.destination) != spec.expected_before:
            raise DriftError("destination drift between plan and prepare")
    _assert_no_overlap(target, {spec.destination for spec in plan.mutations}, exclude=plan.operation_id)
    op_dir = operation_dir(target, plan.operation_id)
    if op_dir.exists():
        if _read_journal(op_dir).get("status") == "superseded":
            raise ProjectionError("cannot reuse a superseded operation id")
        shutil.rmtree(op_dir)
    op_dir.mkdir(parents=True)
    try:
        os.chmod(op_dir, 0o700)
    except OSError:
        pass
    try:
        with _bind_held_parents(plan.mutations):
            return _prepare_and_commit(plan, target=target, op_dir=op_dir, inject=inject)
    except (PlanError, DriftError, OverlapBlockedError, DestinationChangedError):
        shutil.rmtree(op_dir, ignore_errors=True)
        raise
    except Exception:
        shutil.rmtree(op_dir, ignore_errors=True)
        raise


def recover(operation_id: str, *, target: Path, force: bool = False) -> Receipt:
    if operation_id.startswith(("skills-", "harness-profile-")):
        from ..skills_cmd.mutation import skill_mutation

        with skill_mutation(target):
            return _recover(operation_id, target=target, force=force)
    return _recover(operation_id, target=target, force=force)


def _recover(operation_id: str, *, target: Path, force: bool = False) -> Receipt:
    target = target.expanduser().resolve()
    op_dir = operation_dir(target, operation_id)
    journal = _read_journal(op_dir)
    status = str(journal.get("status") or "")
    if status == "committed":
        return _receipt_from_file(op_dir)
    if status in {"restored", "superseded"}:
        return _receipt_from_file(op_dir)
    mutations = _mutations_from_journal(journal)
    for spec in mutations:
        current = _recover_digest(spec.destination)
        allowed = {spec.expected_before, spec.desired_after}
        if current not in allowed and not force:
            raise DestinationChangedError(
                f"destination {spec.display_path} changed after the failed operation; pass --force to restore anyway"
            )
    with _bind_held_parents(mutations):
        _write_journal(op_dir, {**journal, "status": "restoring"})
        try:
            for spec in reversed(mutations):
                _restore_one(op_dir, spec)
        except OSError:
            receipt = _build_receipt(
                plan_from_journal(journal, mutations), terminal_state="recovery-required", validator_results=()
            )
            _persist_receipt(
                op_dir,
                receipt,
                journal_update={**journal, "status": "recovery-required"},
                keep_before=True,
            )
            return receipt
        receipt = _build_receipt(plan_from_journal(journal, mutations), terminal_state="restored", validator_results=())
        _persist_receipt(op_dir, receipt, journal_update={**journal, "status": "restored", "committed_indexes": []})
        return receipt


def supersede(operation_id: str, *, target: Path) -> Receipt:
    """Retire an unfinished operation without touching destinations or before-images.

    Callers must hold a lock that excludes the operation's writer.
    The caller owns reconciliation of the partial result. Recovery, including
    forced recovery, must never replay a superseded operation.
    """
    target = target.expanduser().resolve()
    op_dir = operation_dir(target, operation_id)
    journal = _read_journal(op_dir)
    status = str(journal.get("status") or "")
    if status == "superseded":
        return _receipt_from_file(op_dir)
    if status not in UNFINISHED_STATUSES:
        raise ProjectionError("only unfinished operations can be superseded")
    receipt = _build_receipt(
        plan_from_journal(journal, _mutations_from_journal(journal)),
        terminal_state="superseded",
        validator_results=(),
    )
    _persist_receipt(op_dir, receipt, journal_update={**journal, "status": "superseded"}, keep_before=True)
    return receipt


def unfinished_operations(target: Path) -> list[OperationSummary]:
    root = operations_root(target)
    if not root.is_dir():
        return []
    found: list[OperationSummary] = []
    for child in sorted(root.iterdir()):
        journal_path = child / "journal.json"
        if not journal_path.is_file():
            continue
        journal = localio.read_json_dict(journal_path) or {}
        status = str(journal.get("status") or "")
        if status not in UNFINISHED_STATUSES:
            continue
        dests = tuple(Path(item["destination"]) for item in journal.get("mutations", []) if item.get("destination"))
        found.append(OperationSummary(operation_id=child.name, status=status, destinations=dests))
    return found


def write_crash_fixture(plan: Plan, *, target: Path, committed_count: int) -> None:
    """Leave a committing journal after N successful mutations, as if the process crashed."""
    execute(plan, target=target, inject=FailureInjector(crash_after=committed_count))


def _prepare_and_commit(
    plan: Plan,
    *,
    target: Path,
    op_dir: Path,
    inject: FailureInjector,
) -> Receipt:
    before_dir = op_dir / "before"
    staged_dir = op_dir / "staged"
    before_dir.mkdir()
    staged_dir.mkdir()
    ordered = tuple(sorted(plan.mutations, key=lambda spec: spec.destination.as_posix()))
    for index, spec in enumerate(ordered):
        _capture_before(before_dir, index, spec)
        _stage_bytes(staged_dir, index, spec)
    validator_results = _run_validators(plan, ordered)
    journal = _journal_payload(plan, ordered, status="prepared", committed_indexes=[])
    _write_journal(op_dir, journal)
    committed: list[int] = []
    crash_receipt: Receipt | None = None
    try:
        journal = {**journal, "status": "committing"}
        _write_journal(op_dir, journal)
        for index, spec in enumerate(ordered):
            if inject.crash_after is not None and index >= inject.crash_after:
                crash_receipt = _build_receipt(
                    plan, terminal_state="recovery-required", validator_results=validator_results
                )
                _persist_receipt(
                    op_dir,
                    crash_receipt,
                    journal_update={**journal, "status": "committing", "committed_indexes": committed},
                    keep_before=True,
                )
                return crash_receipt
            inject.at(f"commit:{index}:before")
            # The digest checks at plan and prepare time are older than this
            # write by now (before-images, validators, earlier mutations). Check
            # the destination once more immediately before the change, so a
            # file edited in the meantime is never overwritten or removed.
            _assert_unchanged_since_plan(spec)
            _apply_mutation(spec)
            committed.append(index)
            _write_journal(op_dir, {**journal, "committed_indexes": committed})
            inject.at(f"commit:{index}:after")
        receipt = _build_receipt(plan, terminal_state="committed", validator_results=validator_results)
        _persist_receipt(
            op_dir, receipt, journal_update={**journal, "status": "committed", "committed_indexes": committed}
        )
        return receipt
    except (InjectedFailure, DriftError, OSError) as error:
        receipt = _restore_after_failure(
            plan,
            op_dir=op_dir,
            ordered=ordered,
            committed=committed,
            journal=journal,
            inject=inject,
            validator_results=validator_results,
        )
        if receipt.terminal_state == "restored" and any(spec.propagate_error for spec in ordered):
            raise error
        return receipt


def _restore_after_failure(
    plan: Plan,
    *,
    op_dir: Path,
    ordered: tuple[MutationSpec, ...],
    committed: list[int],
    journal: dict[str, Any],
    inject: FailureInjector,
    validator_results: tuple[dict[str, Any], ...],
) -> Receipt:
    _write_journal(op_dir, {**journal, "status": "restoring", "committed_indexes": committed})
    try:
        for index in reversed(committed):
            inject.at(f"restore:{index}:before")
            _restore_one(op_dir, ordered[index], index=index)
            inject.at(f"restore:{index}:after")
    except (InjectedFailure, OSError):
        receipt = _build_receipt(plan, terminal_state="recovery-required", validator_results=validator_results)
        _persist_receipt(
            op_dir,
            receipt,
            journal_update={**journal, "status": "recovery-required", "committed_indexes": committed},
            keep_before=True,
        )
        return receipt
    receipt = _build_receipt(plan, terminal_state="restored", validator_results=validator_results)
    _persist_receipt(
        op_dir,
        receipt,
        journal_update={**journal, "status": "restored", "committed_indexes": []},
        keep_before=True,
    )
    return receipt


@dataclass
class _HeldParent:
    fd: int
    chain: tuple[tuple[int, int], ...]
    remaining: tuple[str, ...]
    filename: str


_HELD_PARENTS: contextvars.ContextVar[dict[str, _HeldParent] | None] = contextvars.ContextVar(
    "brigade_projection_held_parents", default=None
)


@contextlib.contextmanager
def _bind_held_parents(mutations: tuple[MutationSpec, ...]) -> Iterator[None]:
    held = _hold_parents(mutations)
    token = _HELD_PARENTS.set(held)
    try:
        yield
    finally:
        _HELD_PARENTS.reset(token)
        _close_held(held)


def _parent_containment_available() -> bool:
    supports: set[Any] = getattr(os, "supports_dir_fd", set())
    # os.replace takes src_dir_fd/dst_dir_fd and is therefore absent from
    # supports_dir_fd, which only records the dir_fd keyword.
    posix = (
        os.name == "posix"
        and hasattr(os, "O_NOFOLLOW")
        and hasattr(os, "O_DIRECTORY")
        and os.open in supports
        and os.mkdir in supports
        and os.unlink in supports
    )
    # Windows holds the same parent through handle-relative NT operations, so
    # the quarantine-then-verify step has a handle to rename through there too.
    return posix or _nt_handles()


def _nt_handles() -> bool:
    """Whether held parents are Windows handles rather than POSIX directory descriptors."""
    return dirfd_mod.nt_available()


def _directory_flags() -> int:
    try:
        return dirfd_mod.directory_flags()
    except OSError as exc:
        raise PlanError("unsafe symlink destination") from exc


def _dir_identity(fd: int) -> tuple[int, int]:
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode):
        raise OSError("projection parent is not a directory")
    return (info.st_dev, info.st_ino)


def _is_unsafe_parent_oserror(exc: OSError) -> bool:
    if exc.errno in {errno.ELOOP, errno.ENOTDIR} or dirfd_mod.is_reparse_point_refusal(exc):
        return True
    text = str(exc).lower()
    return "symlink" in text or "swapped" in text


def _hold_parents(mutations: tuple[MutationSpec, ...]) -> dict[str, _HeldParent]:
    if not _parent_containment_available():
        for spec in mutations:
            _reject_parent_symlink(spec.destination)
        return {}
    held: dict[str, _HeldParent] = {}
    try:
        for spec in mutations:
            held[spec.destination.as_posix()] = _walk_existing_parent(spec.destination)
        return held
    except OSError as exc:
        _close_held(held)
        if _is_unsafe_parent_oserror(exc):
            raise PlanError("unsafe symlink destination") from exc
        raise
    except Exception:
        _close_held(held)
        raise


def _open_walk_root(anchor: str) -> int:
    if _nt_handles():
        return dirfd_mod.open_directory_nofollow(Path(anchor), writable=False)
    return os.open(anchor, dirfd_mod.directory_flags(nofollow=False))


def _open_walk_child(parent: int, component: str, flags: int) -> int:
    if _nt_handles():
        return dirfd_mod.open_child_directory(parent, component, writable=False)
    return os.open(component, flags, dir_fd=parent)


def _walk_existing_parent(destination: Path, *, mutable: bool = True) -> _HeldParent:
    """Open the deepest existing parent of ``destination`` one component at a time without following links.

    ``mutable`` asks for a parent that can be renamed into and created under,
    which on Windows means reopening the final handle with write rights.
    """
    if not destination.is_absolute():
        raise PlanError("destination must be absolute")
    filename = destination.name
    if not filename or filename in {".", ".."}:
        raise PlanError("unsafe destination name")
    parts = destination.parent.parts
    if not parts:
        raise PlanError("unsafe destination path")
    flags = 0 if _nt_handles() else _directory_flags()
    fd = _open_walk_root(parts[0])
    remaining: list[str] = []
    missing = False
    try:
        chain = [_dir_identity(fd)]
        for component in parts[1:]:
            if component in {"", ".", ".."}:
                raise PlanError("unsafe destination path")
            if missing:
                remaining.append(component)
                continue
            try:
                child = _open_walk_child(fd, component, flags)
            except FileNotFoundError:
                missing = True
                remaining.append(component)
                continue
            except OSError as exc:
                if _is_unsafe_parent_oserror(exc):
                    raise OSError("refusing swapped projection parent") from exc
                raise
            os.close(fd)
            fd = child
            chain.append(_dir_identity(fd))
        if mutable and _nt_handles():
            writable = dirfd_mod.reopen_directory_writable(fd)
            os.close(fd)
            fd = writable
        return _HeldParent(fd=fd, chain=tuple(chain), remaining=tuple(remaining), filename=filename)
    except Exception:
        os.close(fd)
        raise


def _assert_held_parent_current(destination: Path, held: _HeldParent) -> None:
    again = _walk_existing_parent(destination, mutable=False)
    try:
        if len(again.chain) < len(held.chain) or again.chain[: len(held.chain)] != held.chain:
            raise OSError("refusing swapped projection parent")
        created = again.chain[len(held.chain) :]
        if len(created) + len(again.remaining) != len(held.remaining):
            raise OSError("refusing swapped projection parent")
        if again.remaining != held.remaining[len(created) :]:
            raise OSError("refusing swapped projection parent")
        current = os.fstat(held.fd)
        if (current.st_dev, current.st_ino) != held.chain[-1]:
            raise OSError("refusing swapped projection parent")
    finally:
        os.close(again.fd)


def _materialize_parent(held: _HeldParent) -> tuple[int, list[int]]:
    flags = 0 if _nt_handles() else _directory_flags()
    fd = held.fd
    extra: list[int] = []
    try:
        for component in held.remaining:
            try:
                dirfd_mod.mkdir_child(fd, component, 0o755)
            except FileExistsError:
                pass
            try:
                child = (
                    dirfd_mod.open_child_directory(fd, component)
                    if _nt_handles()
                    else os.open(component, flags, dir_fd=fd)
                )
            except OSError as exc:
                if _is_unsafe_parent_oserror(exc):
                    raise OSError("refusing swapped projection parent") from exc
                raise
            extra.append(child)
            fd = child
            _dir_identity(fd)
        return fd, extra
    except Exception:
        _close_descriptors(extra)
        raise


def _current_held_parent(spec: MutationSpec) -> _HeldParent | None:
    held = _HELD_PARENTS.get()
    if not held:
        return None
    return held.get(spec.destination.as_posix())


def _materialize_held_parent(spec: MutationSpec, held: _HeldParent | None) -> tuple[int | None, list[int]]:
    if held is None:
        _reject_parent_symlink(spec.destination)
        return None, []
    _assert_held_parent_current(spec.destination, held)
    return _materialize_parent(held)


def _publish_bytes_via_parent(parent_fd: int, name: str, data: bytes) -> None:
    temporary_name = f".{name}.{uuid.uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_BINARY", 0)
    descriptor = dirfd_mod.open_child_file(parent_fd, temporary_name, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        dirfd_mod.replace_children(parent_fd, temporary_name, name)
    except BaseException:
        try:
            dirfd_mod.unlink_child(parent_fd, temporary_name)
        except FileNotFoundError:
            pass
        raise
    finally:
        if descriptor != -1:
            os.close(descriptor)


def _unlink_via_parent(parent_fd: int, name: str) -> None:
    dirfd_mod.unlink_child(parent_fd, name)


def _apply_mode(spec: MutationSpec, parent_fd: int | None, held: _HeldParent | None, mode: int) -> None:
    if parent_fd is not None and held is not None and not _nt_handles():
        _chmod_via_parent(parent_fd, held.filename, mode)
    else:
        # Windows has no permission bits to set through a handle; the pathname chmod only
        # toggles the read-only attribute, as it always did there.
        os.chmod(spec.destination, mode)


def _chmod_via_parent(parent_fd: int, name: str, mode: int) -> None:
    descriptor = os.open(
        name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0), dir_fd=parent_fd
    )
    try:
        os.fchmod(descriptor, mode)
    finally:
        os.close(descriptor)


def _close_descriptors(descriptors: list[int]) -> None:
    for descriptor in descriptors:
        try:
            os.close(descriptor)
        except OSError:
            pass


def _close_held(held: Mapping[str, _HeldParent]) -> None:
    _close_descriptors([item.fd for item in held.values()])


def _path_via_held_parent(parent_fd: int, filename: str) -> Path:
    """Return a path that opens through the held parent descriptor, not a followable pathname."""
    if not filename or filename in {".", ".."} or "/" in filename or "\\" in filename:
        raise OSError("unsafe destination name")
    for base in (Path("/proc/self/fd"), Path("/dev/fd")):
        candidate = base / str(parent_fd)
        if candidate.exists():
            return candidate / filename
    raise OSError("held parent descriptor path is unavailable")


def _assert_regular_on_held_parent(parent_fd: int, name: str) -> None:
    try:
        info = dirfd_mod.stat_child(parent_fd, name)
    except OSError as exc:
        raise OSError("refusing swapped projection parent") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise OSError("refusing swapped projection parent")


def _assert_absent_on_held_parent(parent_fd: int, name: str) -> None:
    try:
        dirfd_mod.stat_child(parent_fd, name)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise OSError("refusing swapped projection parent") from exc
    raise OSError("refusing swapped projection parent")


def _apply_writer(spec: MutationSpec, *, held: _HeldParent | None, parent_fd: int | None) -> None:
    writer = spec.writer
    if writer is None or spec.staged_bytes is None:
        raise PlanError("staged bytes required")
    if parent_fd is None or held is None:
        writer(spec.destination, spec.staged_bytes)
        return
    _assert_held_parent_current(spec.destination, held)
    # There is no pathname that opens through a Windows handle, so a writer there
    # gets the destination path and the held handle keeps its parent in place.
    writer(
        spec.destination if _nt_handles() else _path_via_held_parent(parent_fd, held.filename),
        spec.staged_bytes,
    )
    _assert_regular_on_held_parent(parent_fd, held.filename)
    _assert_held_parent_current(spec.destination, held)


def _apply_remover(spec: MutationSpec, *, held: _HeldParent | None, parent_fd: int | None) -> None:
    remover = spec.remover
    if remover is None:
        raise PlanError("remover required")
    if parent_fd is None or held is None:
        remover(spec.destination)
        return
    _assert_held_parent_current(spec.destination, held)
    remover(spec.destination if _nt_handles() else _path_via_held_parent(parent_fd, held.filename))
    _assert_absent_on_held_parent(parent_fd, held.filename)
    _assert_held_parent_current(spec.destination, held)


def _quarantine_for_verify(spec: MutationSpec, held: _HeldParent | None, parent_fd: int | None) -> str | None:
    """Move the destination aside, then require the moved bytes to be the planned before-state.

    The digest check before a mutation and the mutation itself are separate
    pathname operations. Renaming the destination to a fresh unpredictable name
    through the held parent first means a later write can only reach the moved
    object, and this comparison sees it. Returns the quarantine name, or ``None``
    when nothing needs protecting: a created destination, a custom writer or
    remover (they merge into the live file and manage their own bytes), or a
    platform without a held parent descriptor.
    """
    if (
        parent_fd is None
        or held is None
        or spec.expected_before == ABSENT
        or spec.writer is not None
        or spec.remover is not None
    ):
        return None
    name = f".{held.filename}.{uuid.uuid4().hex}.quarantine"
    _quarantine_rename(parent_fd, held.filename, name)
    try:
        observed = _digest_via_parent(parent_fd, name)
    except OSError:
        observed = None
    if observed == spec.expected_before:
        return name
    try:
        _restore_quarantined(parent_fd, name, held.filename)
    except OSError as error:
        raise DriftError(f"destination drift between check and commit; moved aside as {name}") from error
    raise DriftError("destination drift between check and commit")


def _quarantine_rename(parent_fd: int, name: str, quarantine_name: str) -> None:
    try:
        dirfd_mod.rename_file_child(parent_fd, name, parent_fd, quarantine_name)
    except OSError as error:
        # A refused rename (an open handle without delete sharing, a link in the way) means the
        # destination cannot be moved out of reach, so the mutation is abandoned unverified.
        raise DriftError("destination drift between check and commit") from error


def _digest_via_parent(parent_fd: int, name: str) -> str:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0)
    # Denying writers while the bytes are read keeps the digest of one stable object on Windows.
    descriptor = dirfd_mod.open_child_file(parent_fd, name, flags, share_write=False)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("quarantined destination is not a regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            return content_digest(handle.read())
    finally:
        os.close(descriptor)


def _restore_quarantined(parent_fd: int, quarantine_name: str, name: str) -> None:
    """Put the moved object back, never over an entry that appeared since (link fails on an existing name)."""
    dirfd_mod.replace_children(parent_fd, quarantine_name, name, replace=False)


def _finish_quarantine(parent_fd: int, quarantined: str, name: str, *, published: bool) -> None:
    """Drop the verified moved copy after a publish, or put it back when the mutation failed."""
    try:
        if published:
            dirfd_mod.unlink_child(parent_fd, quarantined)
            return
        try:
            _restore_quarantined(parent_fd, quarantined, name)
        except FileExistsError:
            # The failed step still left new bytes at the name; the planned
            # before-image is the record of the original, so the moved copy goes.
            dirfd_mod.unlink_child(parent_fd, quarantined)
    except OSError:
        pass


def _apply_mutation(spec: MutationSpec) -> None:
    held = _current_held_parent(spec)
    parent_fd, extra = _materialize_held_parent(spec, held)
    try:
        quarantined = _quarantine_for_verify(spec, held, parent_fd)
        try:
            _apply_verified_mutation(spec, held=held, parent_fd=parent_fd, quarantined=quarantined is not None)
        except BaseException:
            if quarantined is not None and parent_fd is not None and held is not None:
                _finish_quarantine(parent_fd, quarantined, held.filename, published=False)
            raise
        if quarantined is not None and parent_fd is not None and held is not None:
            _finish_quarantine(parent_fd, quarantined, held.filename, published=True)
    finally:
        _close_descriptors(extra)


def _apply_verified_mutation(
    spec: MutationSpec, *, held: _HeldParent | None, parent_fd: int | None, quarantined: bool
) -> None:
    if spec.mutation == "remove":
        if spec.remover is not None:
            _apply_remover(spec, held=held, parent_fd=parent_fd)
        elif parent_fd is not None and held is not None:
            # A quarantined destination is already out of the way; deleting the
            # verified moved copy is the removal.
            if not quarantined:
                _unlink_via_parent(parent_fd, held.filename)
        else:
            spec.destination.unlink()
        return
    if spec.staged_bytes is None:
        raise PlanError("staged bytes required")
    if spec.writer is not None:
        _apply_writer(spec, held=held, parent_fd=parent_fd)
    elif parent_fd is not None and held is not None:
        _publish_bytes_via_parent(parent_fd, held.filename, spec.staged_bytes)
    else:
        spec.destination.parent.mkdir(parents=True, exist_ok=True)
        localio.write_bytes_atomic(spec.destination, spec.staged_bytes)
    if spec.mode is not None:
        _apply_mode(spec, parent_fd, held, spec.mode)


def _restore_one(op_dir: Path, spec: MutationSpec, index: int | None = None) -> None:
    if index is None:
        index = _index_for(op_dir, spec)
    absent_marker = op_dir / "before" / f"{index}.absent"
    data_path = op_dir / "before" / f"{index}.bin"
    meta_path = op_dir / "before" / f"{index}.meta.json"
    held = _current_held_parent(spec)
    parent_fd, extra = _materialize_held_parent(spec, held)
    try:
        if absent_marker.is_file() or spec.expected_before == ABSENT:
            if parent_fd is not None and held is not None:
                try:
                    _unlink_via_parent(parent_fd, held.filename)
                except FileNotFoundError:
                    pass
            elif spec.destination.exists() or spec.destination.is_symlink():
                spec.destination.unlink()
            return
        payload = data_path.read_bytes()
        if parent_fd is not None and held is not None:
            _publish_bytes_via_parent(parent_fd, held.filename, payload)
        else:
            spec.destination.parent.mkdir(parents=True, exist_ok=True)
            localio.write_bytes_atomic(spec.destination, payload)
        meta = localio.read_json_dict(meta_path) or {}
        mode = meta.get("mode")
        if isinstance(mode, int):
            _apply_mode(spec, parent_fd, held, mode)
    finally:
        _close_descriptors(extra)


def _index_for(op_dir: Path, spec: MutationSpec) -> int:
    journal = _read_journal(op_dir)
    for item in journal.get("mutations", []):
        if Path(item["destination"]) == spec.destination:
            return int(item["index"])
    raise ProjectionError("before-image index missing")


def _assert_unchanged_since_plan(spec: MutationSpec) -> None:
    """Raise ``DriftError`` unless the destination still holds the planned before-state."""
    try:
        live = _live_digest(spec.destination)
    except (PlanError, OSError) as error:
        raise DriftError("destination drift between prepare and commit") from error
    if live != spec.expected_before:
        raise DriftError("destination drift between prepare and commit")


def _capture_before(before_dir: Path, index: int, spec: MutationSpec) -> None:
    meta: dict[str, Any] = {"mode": None, "absent": spec.expected_before == ABSENT}
    if spec.expected_before == ABSENT:
        (before_dir / f"{index}.absent").write_text("absent\n", encoding="utf-8")
    else:
        before = spec.destination.read_bytes()
        # The before-image is what a failure restores: it must be the planned
        # bytes, never an edit made after the plan-time check.
        if content_digest(before) != spec.expected_before:
            raise DriftError("destination drift between plan and prepare")
        (before_dir / f"{index}.bin").write_bytes(before)
        meta["mode"] = spec.destination.stat().st_mode & 0o777
    localio.write_json(before_dir / f"{index}.meta.json", meta)


def _stage_bytes(staged_dir: Path, index: int, spec: MutationSpec) -> None:
    if spec.staged_bytes is None:
        (staged_dir / f"{index}.absent").write_text("absent\n", encoding="utf-8")
        return
    (staged_dir / f"{index}.bin").write_bytes(spec.staged_bytes)


def _run_validators(plan: Plan, ordered: tuple[MutationSpec, ...]) -> tuple[dict[str, Any], ...]:
    staged = {index: spec.staged_bytes for index, spec in enumerate(ordered)}
    results: list[dict[str, Any]] = []
    for validator in plan.validators:
        validator(plan, staged)
        results.append({"name": getattr(validator, "__name__", "validator"), "status": "ok"})
    return tuple(results)


def _journal_payload(
    plan: Plan,
    ordered: tuple[MutationSpec, ...],
    *,
    status: str,
    committed_indexes: list[int],
) -> dict[str, Any]:
    return {
        "schema": JOURNAL_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "operation_id": plan.operation_id,
        "projector": plan.projector,
        "source_fingerprint": plan.source_fingerprint,
        "status": status,
        "committed_indexes": list(committed_indexes),
        "mutations": [
            {
                "index": index,
                "destination": spec.destination.as_posix(),
                "display_path": spec.display_path,
                "mutation": spec.mutation,
                "expected_before": spec.expected_before,
                "desired_after": spec.desired_after,
                "mode": spec.mode,
            }
            for index, spec in enumerate(ordered)
        ],
    }


def _write_journal(op_dir: Path, payload: dict[str, Any]) -> None:
    localio.write_json(op_dir / "journal.json", payload)
    try:
        os.chmod(op_dir / "journal.json", 0o600)
    except OSError:
        pass


def _read_journal(op_dir: Path) -> dict[str, Any]:
    payload = localio.read_json_dict(op_dir / "journal.json")
    if payload is None:
        raise ProjectionError("projection journal missing or invalid")
    return payload


def _mutations_from_journal(journal: Mapping[str, Any]) -> tuple[MutationSpec, ...]:
    specs: list[MutationSpec] = []
    for item in journal.get("mutations", []):
        specs.append(
            MutationSpec(
                destination=Path(str(item["destination"])),
                display_path=str(item.get("display_path") or ""),
                mutation=item["mutation"],
                expected_before=str(item["expected_before"]),
                desired_after=str(item["desired_after"]),
                mode=item.get("mode") if isinstance(item.get("mode"), int) else None,
            )
        )
    return tuple(specs)


def plan_from_journal(journal: Mapping[str, Any], mutations: tuple[MutationSpec, ...]) -> Plan:
    return Plan(
        operation_id=str(journal["operation_id"]),
        projector=str(journal.get("projector") or "unknown"),
        source_fingerprint=str(journal.get("source_fingerprint") or ""),
        mutations=mutations,
    )


def _build_receipt(
    plan: Plan,
    *,
    terminal_state: str,
    validator_results: tuple[dict[str, Any], ...],
) -> Receipt:
    counts = {"create": 0, "replace": 0, "remove": 0}
    destinations: list[dict[str, Any]] = []
    for spec in plan.mutations:
        counts[spec.mutation] = counts.get(spec.mutation, 0) + 1
        destinations.append(
            {
                "display_path": spec.display_path,
                "mutation": spec.mutation,
                "before_digest": spec.expected_before,
                "after_digest": spec.desired_after,
            }
        )
    recovery_command = None
    if terminal_state == "recovery-required":
        recovery_command = f"brigade projection recover {plan.operation_id}"
    return Receipt(
        schema_version=SCHEMA_VERSION,
        operation_id=plan.operation_id,
        projector=plan.projector,
        source_digest=plan.source_fingerprint,
        destinations=tuple(destinations),
        validator_results=validator_results,
        mutation_counts=counts,
        terminal_state=terminal_state,
        recovery_command=recovery_command,
    )


def _persist_receipt(
    op_dir: Path,
    receipt: Receipt,
    *,
    journal_update: dict[str, Any],
    keep_before: bool = False,
) -> None:
    payload = receipt.to_dict()
    localio.write_json(op_dir / "receipt.json", payload)
    try:
        os.chmod(op_dir / "receipt.json", 0o600)
    except OSError:
        pass
    _write_journal(op_dir, journal_update)
    if receipt.terminal_state == "committed" and not keep_before:
        shutil.rmtree(op_dir / "before", ignore_errors=True)
        shutil.rmtree(op_dir / "staged", ignore_errors=True)


def _receipt_from_file(op_dir: Path) -> Receipt:
    payload = localio.read_json_dict(op_dir / "receipt.json")
    if payload is None:
        raise ProjectionError("projection receipt missing")
    return Receipt(
        schema_version=int(payload.get("schema_version") or SCHEMA_VERSION),
        operation_id=str(payload["operation_id"]),
        projector=str(payload.get("projector") or ""),
        source_digest=str(payload.get("source_digest") or ""),
        destinations=tuple(payload.get("destinations") or ()),
        validator_results=tuple(payload.get("validator_results") or ()),
        mutation_counts=dict(payload.get("mutation_counts") or {}),
        terminal_state=str(payload["terminal_state"]),
        recovery_command=payload.get("recovery_command"),
    )


def _assert_no_overlap(target: Path, destinations: set[Path], *, exclude: str) -> None:
    incoming = {_physical(path) for path in destinations}
    for item in unfinished_operations(target):
        if item.operation_id == exclude:
            continue
        existing = {_physical(path) for path in item.destinations}
        if incoming & existing:
            raise OverlapBlockedError(f"unfinished projection {item.operation_id} overlaps the planned destination set")


def _validate_mutation_bytes(spec: MutationSpec) -> None:
    if spec.mutation in {"create", "replace"}:
        if spec.staged_bytes is None:
            raise PlanError("staged bytes required")
        if content_digest(spec.staged_bytes) != spec.desired_after:
            raise PlanError("desired after-state digest does not match staged bytes")
    elif spec.mutation == "remove":
        if spec.desired_after != ABSENT:
            raise PlanError("remove must desire absence")
    else:
        raise PlanError("unsupported mutation type")


def _reject_symlink_and_type(path: Path, kind: MutationKind) -> None:
    try:
        status = os.lstat(path)
    except FileNotFoundError:
        if kind != "create":
            raise PlanError("destination is absent") from None
        _reject_parent_symlink(path)
        return
    if stat.S_ISLNK(status.st_mode):
        raise PlanError("unsafe symlink destination")
    if stat.S_ISDIR(status.st_mode) and kind != "replace":
        # Directory destinations are invalid as file mutations; parent-child
        # checks still run first when another dest lives underneath.
        raise PlanError("unsupported file type")
    if not stat.S_ISREG(status.st_mode) and not stat.S_ISDIR(status.st_mode):
        raise PlanError("unsupported file type")
    if stat.S_ISDIR(status.st_mode):
        return
    _reject_parent_symlink(path)


def _reject_parent_symlink(path: Path) -> None:
    parent = path.parent
    while parent != parent.parent:
        try:
            status = os.lstat(parent)
        except FileNotFoundError:
            parent = parent.parent
            continue
        if stat.S_ISLNK(status.st_mode):
            raise PlanError("unsafe symlink destination")
        parent = parent.parent


def _parent_child(left: Path, right: Path) -> bool:
    left_parts = left.parts
    right_parts = right.parts
    if left_parts == right_parts:
        return False
    return left_parts == right_parts[: len(left_parts)] or right_parts == left_parts[: len(right_parts)]


def _physical(path: Path) -> Path:
    path = path.expanduser()
    parent = path.parent.resolve()
    return parent / path.name


def _live_digest(path: Path) -> str:
    try:
        status = os.lstat(path)
    except FileNotFoundError:
        return ABSENT
    if stat.S_ISLNK(status.st_mode):
        raise PlanError("unsafe symlink destination")
    if not stat.S_ISREG(status.st_mode):
        raise PlanError("unsupported file type")
    return content_digest(path.read_bytes())


def _recover_digest(path: Path) -> str:
    try:
        status = os.lstat(path)
    except FileNotFoundError:
        return ABSENT
    if stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode):
        return f"unexpected:{status.st_mode}"
    return content_digest(path.read_bytes())


def _redact_text(text: str) -> str:
    home = str(Path.home().expanduser().resolve())
    redacted = text.replace(home, "~")
    if home != str(Path.home()):
        redacted = redacted.replace(str(Path.home()), "~")
    return _HOME_TOKEN_RE.sub("[redacted]", redacted)
