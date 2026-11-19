"""Persisted incremental index for dispatch failure counts and exclusions.

Every ``brigade work dispatch`` tick plans by folding the local intent receipts
(one JSON file per dispatch attempt) in ``(created_at, intent_id)`` order, and
the pre-index code read and parsed the whole history twice per call: once in
``failure_counts`` and again for the active and completed exclusions. A
long-running watch therefore planned in time proportional to its own history.

This module keeps that fold next to the receipts and advances it incrementally.
The index stores:

* the journal cursor: how many receipts it has folded, the byte total of those
  receipts, and a digest of the last folded receipt, which together fingerprint
  the prefix the index covers;
* one compact identity row per folded receipt (path, order key, size,
  ``mtime_ns``, inode, task) so added, rewritten, and removed receipts are
  detectable without reading them;
* the derived per-task fold state: the failure count, the last active receipt,
  and the last completed receipt.

A call folds only the receipts whose identity changed, and re-derives only the
tasks those receipts belong to. Dispatch receipts are rewritten in place as an
intent advances through its states, so an index that only appended new files
would serve stale exclusions; the identity rows are what keep a rewritten
receipt honest, and per-task re-derivation keeps the cost proportional to that
task's own attempts instead of the full history.

Invalidation:

* missing, unreadable, or schema-mismatched index: rebuild from the receipts;
* journal shorter than the cursor, or an indexed receipt removed: rebuild;
* reset receipts changed (they erase history for a task): rebuild;
* a receipt's order key or task moved, or a receipt appeared behind the
  cursor: rebuild, because the fold order is no longer append-only;
* a receipt identity changed in place: re-fold only its task;
* nothing changed: serve the persisted fold without reading any receipt.

``exclusions`` additionally re-evaluates the completed-but-unlanded arm against
the live task ledger on every call, so a ledger change (a release or a landed
task) never needs a history rescan. Every completed receipt is kept, not just the
newest: a later completion with a different claim must not hide an earlier one
whose claim the ledger still holds.

Only the dispatcher that holds the dispatch lease writes the index. A dry-run
plan or ``dispatch status`` reads an existing index and otherwise computes in
memory. A write that cannot land (read-only directory, full disk) warns and
still returns the computed fold.
"""

from __future__ import annotations

import json
import os
import tempfile
import warnings
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable

from .. import localio, worker_failure
from . import dispatch_state

SCHEMA = "brigade.work_dispatch_exclusion_index.v1"
INDEX_NAME = "exclusion-index.json"
BREAKER_THRESHOLD = 3

# (size, mtime_ns, inode). The inode changes on the atomic replace every receipt
# writer uses, so a rewrite is detected even when timestamps are coarse.
_Identity = tuple[int, int, int]


@dataclass(frozen=True)
class _Record:
    """One folded receipt: where it lives, how it sorts, and its last identity."""

    path: str
    created_at: str
    intent_id: str
    size: int
    mtime_ns: int
    inode: int
    task_id: str

    @property
    def key(self) -> tuple[str, str]:
        return self.created_at, self.intent_id

    @property
    def identity(self) -> _Identity:
        return self.size, self.mtime_ns, self.inode


@dataclass
class _TaskState:
    """The fold result for one task, kept small enough to persist per task."""

    # None means no receipt ever assigned a failure count, so the task must not
    # appear in ``failure_counts`` at all. Zero is a real value: a completion
    # cleared the count.
    count: int | None = None
    active: tuple[str, str] | None = None
    # Every completed receipt that can still match the live ledger, as
    # (created_at, intent_id, claim_id, item_revision), oldest first.
    completed: list[tuple[str, str, str, int]] = field(default_factory=list)


@dataclass(frozen=True)
class _Index:
    records: list[_Record]
    tasks: dict[str, _TaskState]
    resets_digest: str
    tail: str | None
    bytes: int


def state_path(target: Path) -> Path:
    return dispatch_state.root(target) / INDEX_NAME


def _read_record(path: Path) -> dict[str, Any]:
    return dispatch_state.read(path)


def _key(row: dict[str, Any]) -> tuple[str, str]:
    return row["created_at"], row["intent_id"]


def _digest(row: dict[str, Any]) -> str:
    return localio.canonical_json_digest(row)


def _record(path: str, row: dict[str, Any], identity: _Identity) -> _Record:
    return _Record(
        path=path,
        created_at=row["created_at"],
        intent_id=row["intent_id"],
        size=identity[0],
        mtime_ns=identity[1],
        inode=identity[2],
        task_id=row["task_id"],
    )


def _identities(target: Path) -> dict[str, _Identity]:
    """Map every receipt, relative to the dispatch root, to its identity.

    ``os.scandir`` is used instead of ``Path.glob`` because a planning call runs
    this over the whole receipt tree on every tick, and the path construction and
    directory checks in ``glob`` dominate a 10,000-receipt history.
    """
    result: dict[str, _Identity] = {}
    try:
        sessions = list(os.scandir(dispatch_state.root(target)))
    except OSError:
        return result
    for session in sessions:
        if not session.name.startswith("dispatch-") or not session.is_dir():
            continue
        try:
            entries = list(os.scandir(os.path.join(session.path, "intents")))
        except OSError:
            continue
        for entry in entries:
            if not entry.name.endswith(".json"):
                continue
            try:
                stat = entry.stat()
            except OSError:
                continue
            result[f"{session.name}/intents/{entry.name}"] = (stat.st_size, stat.st_mtime_ns, stat.st_ino)
    return result


def _remember_completed(state: _TaskState, entry: tuple[str, str, str, int]) -> None:
    """Keep one completed receipt per (claim id, item revision), newest wins.

    The full scan evaluates every completed receipt against the live ledger, so a
    later completion with a different claim cannot replace an earlier one whose
    claim the ledger still holds. Two receipts with the same claim and revision
    make the same decision, so the newest key can stand for both.
    """
    state.completed = [item for item in state.completed if (item[2], item[3]) != (entry[2], entry[3])]
    state.completed.append(entry)


def _fold_onto(state: _TaskState, rows: Iterable[dict[str, Any]], resets: dict[str, str]) -> _TaskState:
    """Apply the pre-index fold rules for the given rows, in order."""
    for row in rows:
        reset = resets.get(row["task_id"], "")
        created_at = row["created_at"]
        state_name = row["state"]
        if created_at > reset:
            if state_name == "completed":
                state.count = 0
                revision = row.get("claimed_item_revision", row["task_item_revision"] + 1)
                _remember_completed(state, (created_at, row["intent_id"], row["claim_id"], revision))
            elif state_name == "failed":
                terminal = row.get("terminal") or {}
                if (
                    terminal.get("infrastructure") is not True
                    and terminal.get("failure_kind") not in worker_failure.RUN_POLICY_TERMINAL_FAILURE_KINDS
                ):
                    state.count = (state.count or 0) + 1
        if state_name in dispatch_state.ACTIVE or state_name == "unknown":
            state.active = (created_at, row["intent_id"])
    return state


def _decode_record(value: Any) -> _Record | None:
    if not isinstance(value, list) or len(value) != 7:
        return None
    path, created_at, intent_id, size, mtime_ns, inode, task_id = value
    if not all(isinstance(item, str) for item in (path, created_at, intent_id, task_id)):
        return None
    if any(type(item) is not int or item < 0 for item in (size, mtime_ns, inode)):
        return None
    return _Record(
        path=path,
        created_at=created_at,
        intent_id=intent_id,
        size=size,
        mtime_ns=mtime_ns,
        inode=inode,
        task_id=task_id,
    )


def _decode_task(value: Any) -> _TaskState | None:
    if not isinstance(value, dict):
        return None
    count = value.get("count")
    active = value.get("active")
    completed = value.get("completed")
    if count is not None and (type(count) is not int or count < 0):
        return None
    if active is not None and not (
        isinstance(active, list) and len(active) == 2 and all(isinstance(item, str) for item in active)
    ):
        return None
    if not isinstance(completed, list):
        return None
    entries: list[tuple[str, str, str, int]] = []
    for entry in completed:
        if not (
            isinstance(entry, list)
            and len(entry) == 4
            and isinstance(entry[0], str)
            and isinstance(entry[1], str)
            and isinstance(entry[2], str)
            and type(entry[3]) is int
        ):
            return None
        entries.append((entry[0], entry[1], entry[2], entry[3]))
    return _TaskState(
        count=count,
        active=(active[0], active[1]) if active is not None else None,
        completed=entries,
    )


def _decode(raw: dict[str, Any] | None) -> _Index | None:
    """Return a self-consistent index, or None when it is missing or corrupt."""
    if not isinstance(raw, dict) or raw.get("schema") != SCHEMA:
        return None
    try:
        journal = raw["journal"]
        resets_digest = raw["resets"]
        entries = raw["records"]
        task_rows = raw["tasks"]
        if not isinstance(journal, dict) or not isinstance(task_rows, dict):
            return None
        offset = journal["offset"]
        total = journal["bytes"]
        tail = journal.get("tail")
    except (KeyError, TypeError, AttributeError):
        return None
    if type(offset) is not int or type(total) is not int or offset < 0 or total < 0:
        return None
    if not isinstance(resets_digest, str) or not (tail is None or isinstance(tail, str)):
        return None
    if not isinstance(entries, list):
        return None
    records = [_decode_record(entry) for entry in entries]
    if any(record is None for record in records):
        return None
    decoded: list[_Record] = [record for record in records if record is not None]
    tasks: dict[str, _TaskState] = {}
    for task_id, value in task_rows.items():
        if not isinstance(task_id, str):
            return None
        state = _decode_task(value)
        if state is None:
            return None
        tasks[task_id] = state
    if offset != len(decoded) or total != sum(record.size for record in decoded):
        return None
    keys = [record.key for record in decoded]
    if any(earlier > later for earlier, later in zip(keys, keys[1:], strict=False)):
        return None
    if (tail is None) != (not decoded):
        return None
    return _Index(records=decoded, tasks=tasks, resets_digest=resets_digest, tail=tail, bytes=total)


def _rebuild(target: Path, current: dict[str, _Identity], resets: dict[str, str], resets_digest: str) -> _Index:
    """Fold the whole journal from scratch."""
    base = dispatch_state.root(target)
    pairs: list[tuple[str, dict[str, Any]]] = []
    # Iterate in the glob order the receipt reader uses, so equal order keys keep
    # the same relative order as a full scan.
    for path in current:
        pairs.append((path, _read_record(base / path)))
    pairs.sort(key=lambda pair: _key(pair[1]))
    tasks: dict[str, _TaskState] = {}
    records: list[_Record] = []
    for path, row in pairs:
        records.append(_record(path, row, current[path]))
        _fold_onto(tasks.setdefault(row["task_id"], _TaskState()), [row], resets)
    return _Index(
        records=records,
        tasks=tasks,
        resets_digest=resets_digest,
        tail=_digest(pairs[-1][1]) if pairs else None,
        bytes=sum(record.size for record in records),
    )


def _derive_task(
    target: Path,
    task_id: str,
    indexed: list[_Record],
    added: list[_Record],
    cache: dict[str, dict[str, Any]],
    resets: dict[str, str],
) -> _TaskState:
    """Re-fold one task from its own receipts after one of them was rewritten."""
    base = dispatch_state.root(target)
    records = [record for record in indexed + added if record.task_id == task_id]
    records.sort(key=lambda record: record.key)
    rows: list[dict[str, Any]] = []
    for record in records:
        row = cache.get(record.path)
        if row is None:
            row = _read_record(base / record.path)
        rows.append(row)
    return _fold_onto(_TaskState(), rows, resets)


def _advance(
    target: Path,
    index: _Index,
    current: dict[str, _Identity],
    resets: dict[str, str],
    resets_digest: str,
) -> tuple[_Index | None, bool]:
    """Fold receipt deltas, or report that the index cannot be advanced."""
    indexed = {record.path: record for record in index.records}
    if len(indexed) != len(index.records):
        return None, False
    new_paths = [path for path in current if path not in indexed]
    rewritten = [record for record in index.records if current[record.path] != record.identity]
    if not new_paths and not rewritten:
        return index, False
    base = dispatch_state.root(target)
    cache: dict[str, dict[str, Any]] = {}
    tail_path = index.records[-1].path if index.records else None
    effective: list[_Record] = []
    for record in rewritten:
        row = _read_record(base / record.path)
        cache[record.path] = row
        if _key(row) != record.key or row["task_id"] != record.task_id:
            # The receipt moved in the fold order or changed owner.
            return None, False
        if record.path == tail_path and _digest(row) == index.tail:
            # The stored fingerprint still matches the rewritten bytes, so only
            # the timestamp moved and the fold is unchanged.
            continue
        effective.append(record)
    added: list[_Record] = []
    for path in new_paths:
        row = _read_record(base / path)
        cache[path] = row
        added.append(_record(path, row, current[path]))
    added.sort(key=lambda record: record.key)
    cursor = index.records[-1].key if index.records else None
    for record in added:
        if cursor is not None and record.key <= cursor:
            # A receipt behind the cursor changes the prefix order.
            return None, False
        cursor = record.key
    tasks = dict(index.tasks)
    rewritten_tasks = {record.task_id for record in effective}
    appended_tasks = {record.task_id for record in added} - rewritten_tasks
    for task_id in rewritten_tasks:
        tasks[task_id] = _derive_task(target, task_id, index.records, added, cache, resets)
    for task_id in appended_tasks:
        tasks[task_id] = _fold_onto(
            tasks.get(task_id, _TaskState()),
            [cache[record.path] for record in added if record.task_id == task_id],
            resets,
        )
    identities = {record.path: current[record.path] for record in rewritten}
    records = [
        replace(
            record,
            size=identities[record.path][0],
            mtime_ns=identities[record.path][1],
            inode=identities[record.path][2],
        )
        if record.path in identities
        else record
        for record in index.records
    ]
    records.extend(added)
    tail = index.tail
    if records and records[-1].path in cache:
        tail = _digest(cache[records[-1].path])
    return (
        _Index(
            records=records,
            tasks=tasks,
            resets_digest=resets_digest,
            tail=tail,
            bytes=sum(record.size for record in records),
        ),
        True,
    )


def _load(
    target: Path,
    current: dict[str, _Identity],
    resets: dict[str, str],
    resets_digest: str,
) -> tuple[_Index | None, bool]:
    """Load and advance the persisted index, or return None to rebuild."""
    index = _decode(localio.read_json_dict(state_path(target)))
    if index is None or index.resets_digest != resets_digest:
        return None, False
    indexed = {record.path: record for record in index.records}
    if len(indexed) != len(index.records):
        return None, False
    if len(current) < len(index.records) or set(indexed) - set(current):
        # The journal is shorter than the cursor, or an indexed receipt is gone.
        return None, False
    return _advance(target, index, current, resets, resets_digest)


def _write(target: Path, index: _Index) -> None:
    """Publish the index atomically without waiting on durability.

    The index is derived state, not a receipt: a crash that loses the newest
    cursor advance only costs the next call a re-fold of the receipts it has not
    folded yet. Skipping the fsync keeps a per-tick write off the durability
    path, which is most of the cost of writing a 10,000-receipt index.
    """
    path = state_path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(_render(index))
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _render(index: _Index) -> bytes:
    payload = {
        "schema": SCHEMA,
        "resets": index.resets_digest,
        "journal": {"offset": len(index.records), "bytes": index.bytes, "tail": index.tail},
        "records": [
            [
                record.path,
                record.created_at,
                record.intent_id,
                record.size,
                record.mtime_ns,
                record.inode,
                record.task_id,
            ]
            for record in index.records
        ],
        "tasks": {
            task_id: {
                "count": state.count,
                "active": list(state.active) if state.active is not None else None,
                "completed": [list(entry) for entry in state.completed],
            }
            for task_id, state in index.tasks.items()
        },
    }
    return (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")


def _persist(target: Path, index: _Index) -> None:
    """Publish the index, warning instead of failing when the write cannot land.

    A read-only dispatch directory, a full disk, or something occupying the index
    path must not turn planning into an error: the computed fold is already in
    hand and the next call simply rebuilds.
    """
    try:
        _write(target, index)
    except OSError as exc:
        warnings.warn(f"dispatch exclusion index not written: {exc}", stacklevel=3)


def _snapshot(target: Path) -> tuple[_Index, dict[str, str]]:
    resets = dispatch_state._resets(target)
    resets_digest = localio.canonical_json_digest(resets)
    current = _identities(target)
    # Only the dispatcher that holds the lease writes the index. Dry-run plans and
    # `dispatch status` read it when it is already there and otherwise compute in
    # memory, so a read-only caller never mutates dispatch state.
    persist = dispatch_state.holds_dispatch_lock()
    index, dirty = _load(target, current, resets, resets_digest)
    if index is None:
        index = _rebuild(target, current, resets, resets_digest)
        if persist and (index.records or state_path(target).exists()):
            _persist(target, index)
        return index, resets
    if dirty and persist and index.records:
        _persist(target, index)
    return index, resets


def failure_counts(target: Path) -> dict[str, int]:
    """Return the per-task failure counts the pre-index full scan produced."""
    index, _ = _snapshot(target)
    return {task_id: state.count for task_id, state in index.tasks.items() if state.count is not None}


def exclusions(target: Path) -> dict[str, str]:
    """Return the dispatch exclusions for the current receipts and task ledger."""
    index, resets = _snapshot(target)
    return _evaluate(target, index, resets)


def _evaluate(target: Path, index: _Index, resets: dict[str, str]) -> dict[str, str]:
    from . import claiming
    from .ledger import locking

    tasks = {task["id"]: task for task in locking._read_task_ledger(target)["tasks"]}
    result: dict[str, str] = {}
    for task_id, state in index.tasks.items():
        reason: str | None = None
        if state.count is not None and state.count >= BREAKER_THRESHOLD:
            reason = "circuit-breaker"
        best: tuple[str, str] = ("", "")
        if state.active is not None and state.active > best:
            best, reason = state.active, "active-intent"
        if state.completed:
            task = tasks.get(task_id, {})
            claim = claiming.claim_record(task)
            reset = resets.get(task_id, "")
            completed_at: tuple[str, str] | None = None
            for created_at, intent_id, claim_id, revision in state.completed:
                if created_at <= reset:
                    continue
                if (
                    task.get("status") == "in_progress"
                    and claim is not None
                    and claim["claim_id"] == claim_id
                    and task.get("item_revision") == revision
                ):
                    if completed_at is None or (created_at, intent_id) > completed_at:
                        completed_at = (created_at, intent_id)
            if completed_at is not None and completed_at > best:
                reason = "completed-unlanded"
        if reason is not None:
            result[task_id] = reason
    return result
