"""Prepare, commit, and restore multi-file projection operations.

A planned operation either publishes every destination mutation or restores
every destination to its captured pre-operation state. The user-facing
guarantee is all-or-restored completion when the process can run recovery, not
simultaneous visibility across independent paths.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, Mapping

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
    target = target.expanduser().resolve()
    inject = inject or FailureInjector()
    for spec in plan.mutations:
        if _live_digest(spec.destination) != spec.expected_before:
            raise DriftError("destination drift between plan and prepare")
    _assert_no_overlap(target, {spec.destination for spec in plan.mutations}, exclude=plan.operation_id)
    op_dir = operation_dir(target, plan.operation_id)
    if op_dir.exists():
        shutil.rmtree(op_dir)
    op_dir.mkdir(parents=True)
    try:
        os.chmod(op_dir, 0o700)
    except OSError:
        pass
    try:
        return _prepare_and_commit(plan, target=target, op_dir=op_dir, inject=inject)
    except (PlanError, DriftError, OverlapBlockedError, DestinationChangedError):
        shutil.rmtree(op_dir, ignore_errors=True)
        raise
    except Exception:
        shutil.rmtree(op_dir, ignore_errors=True)
        raise


def recover(operation_id: str, *, target: Path, force: bool = False) -> Receipt:
    target = target.expanduser().resolve()
    op_dir = operation_dir(target, operation_id)
    journal = _read_journal(op_dir)
    status = str(journal.get("status") or "")
    if status == "committed":
        return _receipt_from_file(op_dir)
    if status == "restored":
        return _receipt_from_file(op_dir)
    mutations = _mutations_from_journal(journal)
    for spec in mutations:
        current = _recover_digest(spec.destination)
        allowed = {spec.expected_before, spec.desired_after}
        if current not in allowed and not force:
            raise DestinationChangedError(
                f"destination {spec.display_path} changed after the failed operation; pass --force to restore anyway"
            )
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
            _apply_mutation(spec)
            committed.append(index)
            _write_journal(op_dir, {**journal, "committed_indexes": committed})
            inject.at(f"commit:{index}:after")
        receipt = _build_receipt(plan, terminal_state="committed", validator_results=validator_results)
        _persist_receipt(
            op_dir, receipt, journal_update={**journal, "status": "committed", "committed_indexes": committed}
        )
        return receipt
    except (InjectedFailure, OSError):
        return _restore_after_failure(
            plan,
            op_dir=op_dir,
            ordered=ordered,
            committed=committed,
            journal=journal,
            inject=inject,
            validator_results=validator_results,
        )


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


def _apply_mutation(spec: MutationSpec) -> None:
    if spec.mutation == "remove":
        spec.destination.unlink()
        return
    if spec.staged_bytes is None:
        raise PlanError("staged bytes required")
    spec.destination.parent.mkdir(parents=True, exist_ok=True)
    localio.write_bytes_atomic(spec.destination, spec.staged_bytes)
    if spec.mode is not None:
        os.chmod(spec.destination, spec.mode)


def _restore_one(op_dir: Path, spec: MutationSpec, index: int | None = None) -> None:
    if index is None:
        index = _index_for(op_dir, spec)
    absent_marker = op_dir / "before" / f"{index}.absent"
    data_path = op_dir / "before" / f"{index}.bin"
    meta_path = op_dir / "before" / f"{index}.meta.json"
    if absent_marker.is_file() or spec.expected_before == ABSENT:
        if spec.destination.exists() or spec.destination.is_symlink():
            spec.destination.unlink()
        return
    payload = data_path.read_bytes()
    spec.destination.parent.mkdir(parents=True, exist_ok=True)
    localio.write_bytes_atomic(spec.destination, payload)
    meta = localio.read_json_dict(meta_path) or {}
    mode = meta.get("mode")
    if isinstance(mode, int):
        os.chmod(spec.destination, mode)


def _index_for(op_dir: Path, spec: MutationSpec) -> int:
    journal = _read_journal(op_dir)
    for item in journal.get("mutations", []):
        if Path(item["destination"]) == spec.destination:
            return int(item["index"])
    raise ProjectionError("before-image index missing")


def _capture_before(before_dir: Path, index: int, spec: MutationSpec) -> None:
    meta: dict[str, Any] = {"mode": None, "absent": spec.expected_before == ABSENT}
    if spec.expected_before == ABSENT:
        (before_dir / f"{index}.absent").write_text("absent\n", encoding="utf-8")
    else:
        (before_dir / f"{index}.bin").write_bytes(spec.destination.read_bytes())
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
