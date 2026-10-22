"""Local dispatch intents, exclusive ownership, and digest-chained receipts."""

from __future__ import annotations

import hashlib
import json
import os
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from .. import localio, runguard, worker_failure
from ..inbox_lock import InboxLockTimeout, held_file_lock_descriptor

TERMINAL = frozenset({"completed", "failed", "canceled", "orphaned", "lost_race", "unknown"})
ACTIVE = frozenset({"planned", "claimed", "workspace_ready", "launching", "running"})
MAX_EVENT_BYTES = 64 * 1024 * 1024
MAX_EVENT_LINE_BYTES = 1024 * 1024
MAX_EVENTS = 100_000
_DISPATCH_LOCK_METADATA_PREFIX = b"BRIGADE-DISPATCH-LOCK-V1:"
_DISPATCH_LOCK_METADATA_LIMIT = 128


@dataclass(frozen=True)
class _EventHead:
    sequence: int
    digest: str | None
    identity: tuple[int, int, int, int, int] | None

    @property
    def size(self) -> int:
        return self.identity[2] if self.identity is not None else 0


# At most 32 small heads per active lease. No payloads or cross-lease trust.
_EVENT_HEADS: ContextVar[tuple[Path, OrderedDict[Path, _EventHead]] | None] = ContextVar(
    "dispatch_event_heads", default=None
)


def _identity(stat: os.stat_result) -> tuple[int, int, int, int, int]:
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _file_identity(path: Path) -> tuple[int, int, int, int, int] | None:
    try:
        return _identity(path.stat())
    except FileNotFoundError:
        return None


def _lease_heads(directory: Path) -> OrderedDict[Path, _EventHead] | None:
    lease = _EVENT_HEADS.get()
    return lease[1] if lease is not None and directory.is_relative_to(lease[0]) else None


def _event_head(directory: Path) -> _EventHead:
    path = directory / "events.jsonl"
    heads = _lease_heads(directory)
    identity = _file_identity(path)
    cached = heads.pop(directory, None) if heads is not None else None
    if heads is not None and cached is not None and cached.identity == identity:
        heads[directory] = cached
        return cached
    rows = events(directory, repair_tail=True, last_only=True)
    validated = _file_identity(path)
    if validated != identity:
        # Repair can truncate the tail. Validate the repaired file under a
        # stable identity before retaining a head, including concurrent edits.
        rows = events(directory, repair_tail=True, last_only=True)
        if _file_identity(path) != validated:
            raise ValueError("dispatch event file changed during validation")
    return _EventHead(rows[-1]["sequence"] if rows else 0, rows[-1]["digest"] if rows else None, validated)


def _canonical(value: dict[str, Any]) -> bytes:
    # Dispatch uses JSON booleans in terminal taxonomy. Lifecycle canonical
    # encoding excludes them, so retain its byte format with normal JSON types.
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def root(target: Path) -> Path:
    return target / ".brigade/dispatch"


@contextmanager
def dispatch_lock(target: Path) -> Iterator[None]:
    path = root(target) / "dispatch.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with held_file_lock_descriptor(path, deadline_seconds=0) as held:
            held.verify()
            # Keep byte zero as the Windows lock byte. Invalidate any previous
            # owner's hint before publishing a complete framed record at byte 1.
            os.ftruncate(held.fd, 1)
            os.lseek(held.fd, 0, os.SEEK_SET)
            _write_dispatch_lock_bytes(held.fd, b"\0")
            os.lseek(held.fd, 1, os.SEEK_SET)
            record = _DISPATCH_LOCK_METADATA_PREFIX + str(os.getpid()).encode("ascii") + b"\n"
            _write_dispatch_lock_bytes(held.fd, record)
            os.ftruncate(held.fd, 1 + len(record))
            token = _EVENT_HEADS.set((root(target).resolve(), OrderedDict()))
            try:
                yield
            finally:
                _EVENT_HEADS.reset(token)
    except InboxLockTimeout as exc:
        holder = _dispatch_lock_holder(path)
        raise ValueError(f"dispatch.lock is held; last recorded owner pid {holder}") from exc


def _write_dispatch_lock_bytes(fd: int, content: bytes) -> None:
    """Write all bytes or propagate the underlying failure."""
    offset = 0
    while offset < len(content):
        written = os.write(fd, content[offset:])
        if written <= 0:
            raise OSError("short write while updating dispatch lock metadata")
        offset += written


def _dispatch_lock_holder(path: Path) -> str:
    """Read a bounded, complete last-recorded PID hint outside locked byte zero."""
    if not path.is_file():
        return "unknown"
    try:
        with path.open("rb") as lock_file:
            lock_file.seek(1)
            metadata = lock_file.read(_DISPATCH_LOCK_METADATA_LIMIT)
        if len(metadata) >= _DISPATCH_LOCK_METADATA_LIMIT:
            return "unknown"
        if not metadata.startswith(_DISPATCH_LOCK_METADATA_PREFIX) or not metadata.endswith(b"\n"):
            return "unknown"
        holder = metadata[len(_DISPATCH_LOCK_METADATA_PREFIX) : -1].decode("ascii")
    except (OSError, UnicodeDecodeError):
        return "unknown"
    return holder if holder.isdecimal() and int(holder) > 0 else "unknown"


def sessions(target: Path) -> list[Path]:
    return sorted(
        (path.parent for path in root(target).glob("dispatch-*/session.json")),
        key=lambda directory: read(directory / "session.json")["created_at"],
    )


def intents(target: Path) -> list[tuple[Path, dict[str, Any]]]:
    rows = [(path, read(path)) for path in root(target).glob("dispatch-*/intents/*.json")]
    return sorted(rows, key=lambda row: (row[1]["created_at"], row[1]["intent_id"]))


def read(path: Path) -> dict[str, Any]:
    value = localio.read_json_dict(path)
    if value is None:
        raise ValueError(f"unreadable dispatch receipt: {path}")
    return value


def process_identity(pid: int) -> tuple[str | None, str | None]:
    """Linux start ticks distinguish PID reuse and detect unreaped zombies."""
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return fields[0], fields[19]
    except (OSError, IndexError):
        return None, None


def child_alive(intent: dict[str, Any]) -> bool:
    launch = localio.read_json_dict(Path(intent["run_dir"]) / "dispatch-launch.json") if intent.get("run_dir") else None
    pid = launch.get("pid") if launch else intent.get("pid")
    expected = launch.get("process_token") if launch else intent.get("process_token")
    if type(pid) is not int or pid <= 0:
        pid, expected = intent.get("pid"), intent.get("process_token")
    if type(pid) is not int or pid <= 0:
        return False
    state, token = process_identity(pid)
    if state == "Z" or (expected is not None and token is not None and expected != token):
        return False
    return runguard._pid_is_active(pid)


def supervised(intent: dict[str, Any]) -> bool:
    return intent["state"] not in TERMINAL or (intent["state"] == "unknown" and child_alive(intent))


def live_children(target: Path) -> int:
    return sum(row["state"] in {"launching", "running", "unknown"} and child_alive(row) for _, row in intents(target))


def events(directory: Path, *, repair_tail: bool = False, last_only: bool = False) -> list[dict[str, Any]]:
    """Validate bounded records, ignoring an incomplete tail for readers.

    Only a lease holder may request repair. Readers leave a concurrent writer's
    partial append untouched. Append needs only the last validated envelope.
    """
    path = directory / "events.jsonl"
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    previous = None
    size = 0
    sequence = 0
    with path.open("r+b" if repair_tail else "rb") as stream:
        if os.fstat(stream.fileno()).st_size > MAX_EVENT_BYTES:
            raise ValueError("dispatch event byte bound exceeded")
        while line := stream.readline(MAX_EVENT_LINE_BYTES + 1):
            if size + len(line) > MAX_EVENT_BYTES or len(line) > MAX_EVENT_LINE_BYTES:
                raise ValueError("dispatch event byte bound exceeded")
            if not line.endswith(b"\n"):
                if repair_tail:
                    stream.truncate(size)
                    stream.flush()
                    os.fsync(stream.fileno())
                break
            sequence += 1
            if sequence > MAX_EVENTS:
                raise ValueError("dispatch event count bound exceeded")
            try:
                row = json.loads(line)
                digest = row.pop("digest")
                if row["previous_digest"] != previous or row["sequence"] != sequence:
                    raise ValueError("dispatch event chain is broken")
                if hashlib.sha256(_canonical(row)).hexdigest() != digest:
                    raise ValueError("dispatch event digest mismatch")
            except (KeyError, TypeError, AttributeError, UnicodeError) as exc:
                raise ValueError("invalid dispatch event envelope") from exc
            row["digest"] = digest
            if last_only:
                rows[:] = [row]
            else:
                rows.append(row)
            previous = digest
            size += len(line)
    return rows


class EventCapacityError(ValueError):
    """A valid log has insufficient room for another complete envelope."""

    def __init__(self, message: str, directory: Path | None = None) -> None:
        super().__init__(message)
        self.directory = directory


def event_room(directory: Path, *, reserve_events: int) -> bool:
    """Reserve worst-case legal envelopes without consuming or raising limits."""
    head = _event_head(directory.resolve())
    return (
        head.sequence + reserve_events <= MAX_EVENTS
        and head.size + reserve_events * MAX_EVENT_LINE_BYTES <= MAX_EVENT_BYTES
    )


def event(directory: Path, name: str, payload: dict[str, Any]) -> None:
    directory = directory.resolve()
    head = _event_head(directory)
    row: dict[str, Any] = {
        "sequence": head.sequence + 1,
        "event_type": name,
        "recorded_at": localio.utc_now_iso(),
        "previous_digest": head.digest,
        "payload": payload,
    }
    if row["sequence"] > MAX_EVENTS:
        raise EventCapacityError("dispatch event count bound exceeded", directory)
    row["digest"] = hashlib.sha256(_canonical(row)).hexdigest()
    line = _canonical(row) + b"\n"
    if len(line) > MAX_EVENT_LINE_BYTES or head.size + len(line) > MAX_EVENT_BYTES:
        raise EventCapacityError("dispatch event byte bound exceeded", directory)
    path = directory / "events.jsonl"
    heads = _lease_heads(directory)
    if heads is not None:
        heads.pop(directory, None)
    with path.open("ab") as stream:
        before = _identity(os.fstat(stream.fileno()))
        if (head.identity is not None and before != head.identity) or (head.identity is None and before[2] != 0):
            raise ValueError("dispatch event file changed before append")
        stream.write(line)
        stream.flush()
        os.fsync(stream.fileno())
        identity = _identity(os.fstat(stream.fileno()))
    if identity[2] != head.size + len(line) or _file_identity(path) != identity:
        raise ValueError("dispatch event file changed during append")
    if heads is not None:
        heads[directory] = _EventHead(row["sequence"], row["digest"], identity)
        if len(heads) > 32:
            heads.popitem(last=False)


def transition(path: Path, intent: dict[str, Any], state: str, **updates: Any) -> None:
    next_intent = dict(intent)
    next_intent.update(updates, state=state, state_seq=intent.get("state_seq", 0) + 1, updated_at=localio.utc_now_iso())
    # The receipt is a write-ahead transition. If replacement is interrupted,
    # recovery repeats the idempotent side effect from the older snapshot.
    event(
        path.parent.parent,
        "work.dispatch.intent",
        {
            **{
                key: next_intent.get(key)
                for key in ("intent_id", "task_id", "claim_id", "run_id", "branch", "terminal")
            },
            "state": state,
            "state_seq": next_intent["state_seq"],
        },
    )
    localio.write_json(path, next_intent)
    intent.update(next_intent)


def _resets(target: Path) -> dict[str, str]:
    resets: dict[str, str] = {}
    for path in (root(target) / "resets").glob("*.json"):
        row = read(path)
        task_id = row["task_id"]
        resets[task_id] = max(resets.get(task_id, ""), row["created_at"])
    return resets


def failure_counts(target: Path) -> dict[str, int]:
    resets = _resets(target)
    counts: dict[str, int] = {}
    for _, row in intents(target):
        task_id = row["task_id"]
        if row["created_at"] <= resets.get(task_id, ""):
            continue
        if row["state"] == "completed":
            counts[task_id] = 0
        elif row["state"] == "failed":
            terminal = row.get("terminal") or {}
            if (
                terminal.get("infrastructure") is not True
                and terminal.get("failure_kind") not in worker_failure.RUN_POLICY_TERMINAL_FAILURE_KINDS
            ):
                counts[task_id] = counts.get(task_id, 0) + 1
    return counts


def exclusions(target: Path) -> dict[str, str]:
    from . import claiming
    from .ledger import locking

    result = {task_id: "circuit-breaker" for task_id, count in failure_counts(target).items() if count >= 3}
    tasks = {task["id"]: task for task in locking._read_task_ledger(target)["tasks"]}
    resets = _resets(target)
    for _, row in intents(target):
        task_id = row["task_id"]
        if row["state"] in ACTIVE or row["state"] == "unknown":
            result[task_id] = "active-intent"
        elif row["state"] == "completed" and row["created_at"] > resets.get(task_id, ""):
            task = tasks.get(task_id, {})
            claim = claiming.claim_record(task)
            # Claiming increments the planned revision once. Older receipts
            # predate the explicit claimed revision field.
            revision = row.get("claimed_item_revision", row["task_item_revision"] + 1)
            if (
                task.get("status") == "in_progress"
                and claim is not None
                and claim["claim_id"] == row["claim_id"]
                and task.get("item_revision") == revision
            ):
                result[task_id] = "completed-unlanded"
    return result


def status(*, target: Path, dispatch_id: str | None = None, json_output: bool = False) -> int:
    directories = sessions(target)
    if dispatch_id:
        matches = (
            directories[-1:] if dispatch_id == "latest" else [p for p in directories if p.name.startswith(dispatch_id)]
        )
        if len(matches) != 1:
            raise ValueError(f"dispatch id not found or ambiguous: {dispatch_id}")
        directories = matches
    rows: list[dict[str, Any]] = []
    for directory in directories:
        rows.append(
            {
                "session": read(directory / "session.json"),
                "plan": read(directory / "plan.json"),
                "intents": [read(p) for p in sorted((directory / "intents").glob("*.json"))],
                "events": events(directory),
            }
        )
    payload: dict[str, Any] = {
        "sessions": rows,
        "tripped_tasks": sorted(key for key, count in failure_counts(target).items() if count >= 3),
    }
    if json_output or dispatch_id is not None:
        print(json.dumps(payload, indent=2))
    else:
        for row in rows:
            print(
                f"{row['session']['dispatch_id']}: " + ", ".join(f"{i['task_id']}={i['state']}" for i in row["intents"])
            )
        if payload["tripped_tasks"]:
            print("tripped: " + ", ".join(payload["tripped_tasks"]))
    return 0


def reset(*, target: Path, task_id: str, reason: str, json_output: bool = False, release_unknown: bool = False) -> int:
    from .ledger import locking

    if not reason.strip():
        raise ValueError("--reason must be nonempty")
    with dispatch_lock(target):
        tasks = locking._read_task_ledger(target)["tasks"]
        matches = [task["id"] for task in tasks if task["id"] == task_id] or [
            task["id"] for task in tasks if task["id"].startswith(task_id)
        ]
        if len(matches) != 1:
            raise ValueError(f"task id not found or ambiguous: {task_id}")
        unknown = (
            [(path, row) for path, row in intents(target) if row["task_id"] == matches[0] and row["state"] == "unknown"]
            if release_unknown
            else []
        )
        if any(child_alive(row) for _, row in unknown):
            raise ValueError("cannot release an unknown intent while its child is alive")
        receipt = {
            "schema": "brigade.work_dispatch_reset.v1",
            "task_id": matches[0],
            "reason": reason.strip(),
            "created_at": localio.utc_now_iso(),
            "released_unknown": [row["intent_id"] for _, row in unknown],
        }
        path = root(target) / "resets" / f"{uuid4().hex}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        localio.write_json_exclusive(path, receipt)
        for intent_path, row in unknown:
            transition(intent_path, row, "canceled", recovery={"reset_receipt": str(path), "reason": reason.strip()})
            locking._release_tasks(target, task_ids=[row["task_id"]], claim_ids=[row["claim_id"]])
    print(json.dumps(receipt) if json_output else f"reset: {matches[0]} ({reason.strip()})")
    return 0
