"""Private before-images and compare-and-swap decisions for memory care."""

from __future__ import annotations

import os
import re
import shutil
import threading
import uuid
from dataclasses import asdict, dataclass
from functools import wraps
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence, Callable, ParamSpec, TypeVar

from . import memory_hub_store as store
from .memory_paths import read_only_pattern, relative_path
from .projection import kernel

_CARE_HELD = threading.local()

P = ParamSpec("P")
R = TypeVar("R")


def serialized(fn: Callable[P, R]) -> Callable[P, R]:
    """Hold the same reentrant, cross-process lock for an entire care operation."""

    @wraps(fn)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        from .inbox_lock import held_file_lock, InboxLockTimeout

        if kwargs.get("dry_run"):
            return fn(*args, **kwargs)
        target = Path(args[0] if args else kwargs["target"]).resolve()  # type: ignore[arg-type]
        # Hand the resolved target on, so a relative --target never reaches a kernel plan.
        if args:
            args = (target, *args[1:])  # type: ignore[assignment]
        else:
            kwargs["target"] = target
        lock = safe_path(target, ".brigade/memory-care/care.lock")
        lock.parent.mkdir(parents=True, exist_ok=True)
        held: set[Path] = getattr(_CARE_HELD, "targets", set())
        if target in held:
            return fn(*args, **kwargs)
        try:
            with held_file_lock(lock, deadline_seconds=1):
                _CARE_HELD.targets = held | {target}
                try:
                    return fn(*args, **kwargs)
                finally:
                    _CARE_HELD.targets = held
        except InboxLockTimeout as exc:
            raise JournalConflict("memory care busy") from exc

    return wrapped


JOURNAL_REL = ".brigade/memory-care/journal"


class JournalConflict(ValueError):
    """A path no longer has the expected bytes."""


class ReadOnlyPath(ValueError):
    """The workspace protects a path."""


@dataclass(frozen=True)
class Change:
    path: str
    before: bytes | None
    after: bytes | None
    mode: int | None = None


@dataclass(frozen=True)
class Decision:
    decision_id: str
    kind: str
    reason: str
    card_id: str | None
    created_at: str
    paths: tuple[str, ...]
    after_sha256: dict[str, str | None]
    hub: dict[str, Any] | None
    undoes: str | None
    undone_by: str | None


def safe_path(target: Path, rel: str) -> Path:
    relative_path(rel, field="path")
    if "\\" in rel:
        raise JournalConflict("path must use POSIX separators")
    path = target / rel
    for part in (path, *path.parents):
        if part == target.parent:
            break
        if part.is_symlink():
            raise JournalConflict(f"symlink path: {rel}")
    return path


def _directory(target: Path, decision_id: str) -> Path:
    if not re.fullmatch(r"[0-9]{8}T[0-9]{6}Z-[a-z]+-[0-9a-f]{8}", decision_id):
        raise JournalConflict("invalid decision id")
    return safe_path(target, f"{JOURNAL_REL}/{decision_id}")


def _payload(target: Path, decision_id: str) -> dict[str, Any]:
    value = store.read_json(_directory(target, decision_id) / "decision.json")
    if not isinstance(value, dict):
        raise JournalConflict("decision not found")
    return value


def load(target: Path, decision_id: str) -> Decision:
    p = _payload(target, decision_id)
    return Decision(**{**{k: p[k] for k in Decision.__dataclass_fields__}, "paths": tuple(p["paths"])})


def annotate(target: Path, decision_id: str, **fields: Any) -> Decision:
    payload = _payload(target, decision_id)
    store.write_json(_directory(target, decision_id) / "decision.json", {**payload, **fields})
    return load(target, decision_id)


def update_hub(target: Path, decision_id: str, hub: dict[str, Any]) -> Decision:
    return annotate(target, decision_id, hub=hub)


def mark_undone(target: Path, decision_id: str, undone_by: str) -> Decision:
    return annotate(target, decision_id, undone_by=undone_by)


def before_bytes(target: Path, decision: Decision, path: str) -> bytes | None:
    index = decision.paths.index(path)
    p = _payload(target, decision.decision_id)
    if p["before_absent"][index]:
        return None
    return (_directory(target, decision.decision_id) / "before" / f"{index}.bin").read_bytes()


def current(target: Path, path: str) -> bytes | None:
    p = safe_path(target, path)
    return p.read_bytes() if p.exists() else None


def commit(
    target: Path,
    *,
    kind: str,
    reason: str,
    card_id: str | None,
    changes: Sequence[Change],
    hub: dict[str, Any] | None = None,
    undoes: str | None = None,
    applied: bool = False,
) -> Decision:
    from .memory_cmd import _config_or_default

    if not changes or not re.fullmatch("[a-z]+", kind):
        raise JournalConflict("empty changes or invalid kind")
    config = _config_or_default(target)
    for change in changes:
        safe_path(target, change.path)
        pattern = read_only_pattern(change.path, config.read_only_paths)
        if pattern:
            raise ReadOnlyPath(f"{change.path}: read_only_paths pattern {pattern}")
    stamp = datetime.now(timezone.utc)
    decision_id = f"{stamp:%Y%m%dT%H%M%SZ}-{kind}-{uuid.uuid4().hex[:8]}"
    directory = _directory(target, decision_id)
    store.private_dir(directory / "before")
    decision = Decision(
        decision_id,
        kind,
        reason,
        card_id,
        stamp.isoformat(),
        tuple(c.path for c in changes),
        {c.path: None if c.after is None else kernel.content_digest(c.after) for c in changes},
        hub,
        undoes,
        None,
    )
    for n, change in enumerate(changes):
        if change.before is not None:
            path = directory / "before" / f"{n}.bin"
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(change.before)
                handle.flush()
                os.fsync(handle.fileno())
    payload = {**asdict(decision), "status": "pending", "before_absent": [c.before is None for c in changes]}
    store.write_json(directory / "decision.json", payload)
    store.fsync_dir(directory / "before")
    store.fsync_dir(directory)
    try:
        if applied:
            if any(current(target, c.path) != c.after for c in changes):
                raise JournalConflict("applied bytes differ")
        else:
            mutations = [
                kernel.mutation(
                    destination=safe_path(target, c.path),
                    mutation="remove" if c.after is None else ("create" if c.before is None else "replace"),
                    expected_before=kernel.content_digest(c.before),
                    desired_after=kernel.content_digest(c.after),
                    staged_bytes=c.after,
                )
                for c in changes
            ]
            plan = kernel.build_plan(
                operation_id=decision_id,
                projector="memory-care",
                source_fingerprint=decision_id,
                mutations=mutations,
                target=target,
            )
            receipt = kernel.execute(plan, target=target)
            if receipt.terminal_state != "committed":
                raise JournalConflict(f"kernel {receipt.terminal_state}")
    except (ValueError, OSError, kernel.ProjectionError) as exc:
        shutil.rmtree(directory)
        raise JournalConflict(str(exc)) from exc
    store.write_json(directory / "decision.json", {**payload, "status": "committed"})
    return decision


def entries(target: Path, *, since: datetime | None = None) -> list[Decision]:
    root = target / JOURNAL_REL
    if not root.exists():
        return []
    decisions = []
    for directory in sorted(root.iterdir()):
        if not (directory / "decision.json").exists():
            continue
        p = _payload(target, directory.name)
        if p.get("status") != "committed" and not p.get("restorable"):
            continue
        d = load(target, directory.name)
        if since is None or datetime.fromisoformat(d.created_at) >= since:
            decisions.append(d)
    return decisions


def revert(target: Path, decision_id: str, *, reason: str) -> Decision:
    from contextlib import nullcontext
    from .ingest import _document_lock

    from .memory_inbox_append import DOCUMENT_WRITERS

    documents = any(item["writer"] in DOCUMENT_WRITERS for item in _payload(target, decision_id).get("appends", []))
    with _document_lock(target) if documents else nullcontext():
        return _revert_locked(target, decision_id, reason=reason)


def _revert_locked(target: Path, decision_id: str, *, reason: str) -> Decision:
    d = load(target, decision_id)
    if d.kind == "undo":
        raise JournalConflict("cannot undo an undo decision")
    if d.undone_by and _payload(target, d.undone_by).get("phase") != "abandoned":
        raise JournalConflict("decision already undone")
    from .memory_inbox_append import undo_changes, retractions

    append_payload = _payload(target, decision_id)
    changes = undo_changes(target, append_payload)
    document_undo = bool(changes)
    for path in d.paths:
        live = current(target, path)
        if (None if live is None else kernel.content_digest(live)) != d.after_sha256[path]:
            if d.kind == "inbox" and path.startswith("memory/cards/"):
                raise JournalConflict(
                    "card-changed-by-later-decision: card changed by a later decision. Undo the later decision first."
                )
            raise JournalConflict(f"{path} changed after decision")
        changes.append(Change(path, live, before_bytes(target, d, path)))
    if not append_payload.get("appends"):
        undo = commit(target, kind="undo", reason=reason, card_id=d.card_id, changes=changes, undoes=d.decision_id)
        mark_undone(target, d.decision_id, undo.decision_id)
        return undo
    undo = commit_phased(
        target,
        kind="undo",
        reason=reason,
        card_id=d.card_id,
        changes=changes,
        undoes=d.decision_id,
        prepared_paths=[],
        hub={},
        metadata={"appends": retractions(append_payload), "document_undo": document_undo},
    )
    return undo


def _json_bytes(payload: dict[str, Any]) -> bytes:
    import json

    return (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode()


def _execute_phase(target: Path, decision_id: str, phase: str, changes: Sequence[Change]) -> None:
    mutations = [
        kernel.mutation(
            destination=safe_path(target, c.path),
            mutation="remove" if c.after is None else ("create" if c.before is None else "replace"),
            expected_before=kernel.content_digest(c.before),
            desired_after=kernel.content_digest(c.after),
            staged_bytes=c.after,
            mode=(
                0o600
                if c.path.startswith(JOURNAL_REL + "/")
                else (
                    c.mode
                    if c.mode is not None
                    else (safe_path(target, c.path).stat().st_mode & 0o777 if c.before is not None else None)
                )
            ),
        )
        for c in changes
    ]
    plan = kernel.build_plan(
        operation_id=f"care-{decision_id}-{phase}",
        projector="memory-care",
        source_fingerprint=decision_id,
        mutations=mutations,
        target=target,
    )
    receipt = kernel.execute(plan, target=target)
    if receipt.terminal_state != "committed":
        raise JournalConflict(f"kernel {receipt.terminal_state}")


def commit_phased(
    target: Path,
    *,
    kind: str,
    reason: str,
    card_id: str | None,
    changes: Sequence[Change],
    prepared_paths: Sequence[str],
    hub: dict[str, Any],
    undoes: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> Decision:
    """Publish the safety copy and recovery recipe before any removal."""
    import base64

    from .memory_cmd import _config_or_default

    config = _config_or_default(target)
    for c in changes:
        safe_path(target, c.path)
        if read_only_pattern(c.path, config.read_only_paths):
            raise ReadOnlyPath(c.path)
        if current(target, c.path) != c.before:
            raise JournalConflict(f"{c.path}: changed before preparation")
    stamp = datetime.now(timezone.utc)
    decision_id = f"{stamp:%Y%m%dT%H%M%SZ}-{kind}-{uuid.uuid4().hex[:8]}"
    directory = _directory(target, decision_id)
    store.private_dir(directory / "before")
    d = Decision(
        decision_id,
        kind,
        reason,
        card_id,
        stamp.isoformat(),
        tuple(c.path for c in changes),
        {c.path: None if c.after is None else kernel.content_digest(c.after) for c in changes},
        hub,
        undoes,
        None,
    )
    payload = {
        **asdict(d),
        **(metadata or {}),
        "status": "prepared",
        "phase": "prepared",
        "before_absent": [c.before is None for c in changes],
        "prepared_paths": list(prepared_paths),
        "modes": {
            c.path: (safe_path(target, c.path).stat().st_mode & 0o777 if c.before is not None else c.mode)
            for c in changes
        },
        "after_bytes": [None if c.after is None else base64.b64encode(c.after).decode("ascii") for c in changes],
    }
    preparation = [Change(f"{JOURNAL_REL}/{decision_id}/decision.json", None, _json_bytes(payload))]
    # Immutable evidence is durable before the reversible plan. Recovery must
    # never roll back publication of these saved bytes.
    for n, c in enumerate(changes):
        if c.before is not None:
            fd = os.open(directory / "before" / f"{n}.bin", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(c.before)
                handle.flush()
                os.fsync(handle.fileno())
    store.fsync_dir(directory / "before")
    preparation.extend(c for c in changes if c.path in prepared_paths)
    _execute_phase(target, decision_id, "prepare", preparation)
    return finish_prepared(target, decision_id)


def index_paths(payload: dict[str, Any]) -> set[str]:
    """Index identity survives removal of the final saved link row."""
    return (
        set(payload.get("index_paths", []))
        | set(payload.get("index_before", {}))
        | {row["path"] for row in payload.get("index_lines", [])}
    )


def _restorable(target: Path, payload: dict[str, Any]) -> bool:
    return payload.get("kind") == "archive" and any(
        current(target, path) is not None
        and kernel.content_digest(current(target, path)) == payload["after_sha256"][path]
        for path in payload.get("prepared_paths", [])
    )


def _repair_indexes(target: Path, decision_id: str) -> None:
    """Retry safe link restoration after releasing a superseded overlap guard."""
    import base64

    from .memory_cmd import _config_or_default

    p = _payload(target, decision_id)
    changes = []
    d = load(target, decision_id)
    config = _config_or_default(target)
    for path in p.get("repair_indexes", []):
        live = current(target, path)
        if read_only_pattern(path, config.read_only_paths) or kernel.content_digest(live) != p["after_sha256"][path]:
            continue
        saved = p.get("index_before", {}).get(path)
        before = base64.b64decode(saved) if saved is not None else before_bytes(target, d, path)
        if live != before:
            changes.append(Change(path, live, before))
    if p.get("kind") == "archive" and not p.get("restorable"):
        originals = [path for path in d.paths if path not in index_paths(p) and p["after_sha256"][path] is None]
        if originals and all(current(target, path) is not None for path in originals):
            saved = [before_bytes(target, d, path) for path in originals]
            for path in p.get("prepared_paths", []):
                live = current(target, path)
                if (
                    live is not None
                    and kernel.content_digest(live) == p["after_sha256"][path]
                    and live in saved
                    and not read_only_pattern(path, config.read_only_paths)
                ):
                    # These exact bytes remain in the immutable decision before-images.
                    changes.append(Change(path, live, None))
    if changes:
        _execute_phase(target, decision_id, "repair", changes)
    annotate(target, decision_id, repair_indexes=[])


def finish_prepared(target: Path, decision_id: str) -> Decision:
    import base64

    from .memory_cmd import _config_or_default

    d = load(target, decision_id)
    p = _payload(target, decision_id)
    config = _config_or_default(target)
    changes = [
        Change(
            path,
            before_bytes(target, d, path),
            None if data is None else base64.b64decode(data),
            p.get("modes", {}).get(path),
        )
        for path, data in zip(d.paths, p["after_bytes"], strict=True)
    ]
    if d.kind == "archive":
        # Rebase link removal on the live index, keeping unrelated edits.
        indices = index_paths(p)
        p["index_paths"] = sorted(indices)
        rebased = []
        for c in changes:
            live = current(target, c.path)
            if c.path in indices and live is not None and live != c.before:
                lines = live.decode("utf-8").splitlines(keepends=True)
                rows = [row for row in p["index_lines"] if row["path"] == c.path]
                removed = {row["line"] for row in rows}
                after = "".join(line for line in lines if line not in removed).encode("utf-8")
                p["index_lines"] = [row for row in p["index_lines"] if row["path"] != c.path] + [
                    {
                        "path": c.path,
                        "line": line,
                        "number": n,
                        "neighbor": lines[n + 1] if n + 1 < len(lines) else None,
                    }
                    for n, line in enumerate(lines)
                    if line in removed
                ]
                p.setdefault("index_before", {})[c.path] = base64.b64encode(live).decode("ascii")
                c = Change(c.path, live, after, c.mode)
                p["after_sha256"][c.path] = kernel.content_digest(after)
            rebased.append(c)
        changes = rebased
    prepared = set(p["prepared_paths"])
    drift = [c.path for c in changes if current(target, c.path) != (c.after if c.path in prepared else c.before)]
    drift.extend(
        path
        for path, digest in p.get("append_guards", {}).items()
        if kernel.content_digest(current(target, path)) != digest
    )
    # Routed documents are written after finish, so they are not among the changes.
    from .memory_inbox_append import DOCUMENT_WRITERS

    destinations = [c.path for c in changes] + [
        a["path"] for a in p.get("appends", []) if a.get("writer") in DOCUMENT_WRITERS
    ]
    protected = any(read_only_pattern(path, config.read_only_paths) for path in destinations)
    if drift or protected:
        # Discard only our unchanged safety copy. Never overwrite an edited card or archive.
        final = {
            **p,
            "phase": "abandoned",
            "status": "abandoned",
            "abandoned_reason": ("read-only paths changed" if protected else f"changed: {', '.join(drift)}"),
        }
        original_missing = any(
            c.before is not None and c.after is None and current(target, c.path) is None for c in changes
        )
        final["restorable"] = _restorable(target, p)
        pending = (
            []
            if protected or d.kind == "undo" or original_missing
            else [Change(c.path, c.after, c.before) for c in changes if c.path in prepared and c.path not in drift]
        )
        if any(c.path in prepared and c.after is None for c in pending):
            final["restorable"] = False
    else:
        final = {**p, "phase": "undone" if d.kind == "undo" else "committed", "status": "committed"}
        pending = [c for c in changes if c.path not in prepared]
        if d.undoes:
            predecessor = _payload(target, d.undoes)
            rel = f"{JOURNAL_REL}/{d.undoes}/decision.json"
            pending.append(
                Change(
                    rel, current(target, rel), _json_bytes({**predecessor, "phase": "undone", "undone_by": decision_id})
                )
            )
    rel = f"{JOURNAL_REL}/{decision_id}/decision.json"
    pending.append(Change(rel, current(target, rel), _json_bytes(final)))
    _execute_phase(target, decision_id, "finish", pending)
    from .memory_inbox_append import publish

    publish(target, decision_id)
    return load(target, decision_id)


@serialized
def resolve_prepared(target: Path) -> list[dict[str, Any]]:
    from contextlib import nullcontext
    from .ingest import _document_lock

    needs_lock = False
    for op in kernel.unfinished_operations(target):
        match = re.fullmatch(r"care-([0-9]{8}T[0-9]{6}Z-undo-[0-9a-f]{8})-(prepare|finish|repair)", op.operation_id)
        if match:
            try:
                needs_lock = needs_lock or bool(_payload(target, match[1]).get("document_undo"))
            except JournalConflict:
                # An unreadable unfinished undo may contain a document mutation.
                needs_lock = True
    from .memory_care_pending import load as pending_index

    needs_lock = needs_lock or any(row["document"] for row in pending_index(target).values())
    with _document_lock(target) if needs_lock else nullcontext():
        return _resolve_prepared_locked(target)


def _resolve_prepared_locked(target: Path) -> list[dict[str, Any]]:
    """Recover our interrupted kernel plans before interpreting decision phases."""
    resolved = []
    for operation in kernel.unfinished_operations(target):
        match = re.fullmatch(
            r"care-([0-9]{8}T[0-9]{6}Z-[a-z]+-[0-9a-f]{8})-(prepare|finish|repair)", operation.operation_id
        )
        if not match:
            continue
        decision_id = match.group(1)
        try:
            p = _payload(target, decision_id)
        except JournalConflict:
            p = {}
        if p.get("phase") == "abandoned" and match.group(2) != "repair":
            kernel.supersede(operation.operation_id, target=target)
            _repair_indexes(target, decision_id)
            resolved.append(
                {
                    "decision_id": decision_id,
                    "phase": "abandoned",
                    "reason": p.get("abandoned_reason", ""),
                    "committed_destinations": p.get("committed_destinations", []),
                }
            )
            continue
        try:
            receipt = kernel.recover(operation.operation_id, target=target)
            if receipt.terminal_state != "restored":
                raise JournalConflict(f"kernel {receipt.terminal_state}")
        except (JournalConflict, kernel.ProjectionError):
            if not p:
                raise
            op = store.read_json(kernel.operation_dir(target, operation.operation_id) / "journal.json")
            committed = [op["mutations"][n]["display_path"] for n in op.get("committed_indexes", [])]
            reason = "kernel recovery could not complete safely. Operation superseded, before-images retained"
            annotate(
                target,
                decision_id,
                phase="abandoned",
                status="abandoned",
                abandoned_reason=reason,
                restorable=_restorable(target, p)
                and any(
                    current(target, path) is None and p["after_sha256"][path] is None
                    for path in p["paths"]
                    if path not in index_paths(p)
                ),
                committed_destinations=committed,
                repair_indexes=[] if match.group(2) == "repair" else sorted(index_paths(p)),
            )
            kernel.supersede(operation.operation_id, target=target)
            _repair_indexes(target, decision_id)
            resolved.append(
                {
                    "decision_id": decision_id,
                    "phase": "abandoned",
                    "reason": reason,
                    "committed_destinations": committed,
                }
            )
        else:
            if match.group(2) == "repair":
                _repair_indexes(target, decision_id)
    from .memory_care_pending import load as pending_index

    for decision_id, row in pending_index(target).items():
        if row["repair"]:
            _repair_indexes(target, decision_id)
        if row["append"]:
            from .ingest import AppendRefused
            from .memory_inbox_append import publish

            try:
                publish(target, decision_id)
            except AppendRefused as exc:
                # Recorded on the decision and the handoff is back in the review inbox: not a recovery failure.
                resolved.append({"decision_id": decision_id, "phase": "append-refused", "reason": exc.reason})
                continue
        if row["prepared"]:
            finish_prepared(target, decision_id)
            final = _payload(target, decision_id)
            resolved.append(
                {"decision_id": decision_id, "phase": final["phase"], "reason": final.get("abandoned_reason", "")}
            )
    pending_index(target)
    return resolved
