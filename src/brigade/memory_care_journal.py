"""Private before-images and compare-and-swap decisions for memory care."""

from __future__ import annotations

import os
import re
import shutil
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from . import memory_hub_store as store
from .memory_paths import read_only_pattern, relative_path
from .projection import kernel

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
        p = _payload(target, directory.name)
        if p.get("status") != "committed":
            continue
        d = load(target, directory.name)
        if since is None or datetime.fromisoformat(d.created_at) >= since:
            decisions.append(d)
    return decisions


def revert(target: Path, decision_id: str, *, reason: str) -> Decision:
    d = load(target, decision_id)
    if d.undone_by:
        raise JournalConflict("decision already undone")
    changes = []
    for path in d.paths:
        live = current(target, path)
        if (None if live is None else kernel.content_digest(live)) != d.after_sha256[path]:
            raise JournalConflict(f"{path} changed after decision")
        changes.append(Change(path, live, before_bytes(target, d, path)))
    undo = commit(target, kind="undo", reason=reason, card_id=d.card_id, changes=changes, undoes=d.decision_id)
    mark_undone(target, d.decision_id, undo.decision_id)
    return undo
