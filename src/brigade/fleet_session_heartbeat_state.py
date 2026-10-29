"""Private last-published snapshots beside hook state, independent of task state."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, TypeVar

from .claude_hooks import anchored_io
from .fleet_session_presence import DEFAULT_TTL_SECONDS, SessionSnapshot
from .fleet_session_process import ProcessTable

SUFFIX = ".presence"
MAX_RECORD_BYTES = 65536
LOCK_SECONDS = 0.05
# Allow four default presence windows after expiry before collecting unowned
# records. Quiet, long-running sessions are retained by process proof, not age.
OBSOLETE_GRACE_SECONDS = 4 * DEFAULT_TTL_SECONDS
T = TypeVar("T")


def record_name(harness: str, session_id: str) -> str:
    return hashlib.sha256(f"{harness}\0{session_id}".encode()).hexdigest() + SUFFIX


def publish_tracked(
    snapshot: SessionSnapshot, send: Callable[[], T], *, successful: Callable[[T], bool], ended: bool = False
) -> T:
    """Serialize the actual write and saved payload against timer renewals.

    Version 2 records are published with session-root attribution. Version 1
    could contain tool-attributed secondary checkouts and must never renew.

    Refuse a contended publication rather than sending metadata that cannot be
    saved while an older heartbeat is in flight. Hook callers already fail open.
    """
    if snapshot.harness not in {"claude", "codex"} or not snapshot.checkout_path:
        return send()
    proof = None if ended else ProcessTable().owner(snapshot.harness)
    target = Path(snapshot.checkout_path)
    name = record_name(snapshot.harness, snapshot.session_id)
    with anchored_io.session_directory(target, create=True) as anchor:
        with anchor.held_lock(name + ".lock", deadline_seconds=2.25 if ended else LOCK_SECONDS):
            record = {"version": 2, "enabled": False, "process": proof, "snapshot": asdict(snapshot)}
            # Disable the previous payload before sending. A failed write,
            # process crash, or failed state save cannot renew stale metadata.
            anchor.write_json(name, record)
            result = send()
            if not ended and successful(result):
                record["enabled"] = proof is not None
                anchor.write_json(name, record)
            return result


def read_record(anchor: anchored_io.SessionDirectory, name: str) -> dict[str, Any] | None:
    raw = anchor.read_bytes(name, MAX_RECORD_BYTES + 1)
    if len(raw) > MAX_RECORD_BYTES:
        return None
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        return None
    if not isinstance(value, dict) or value.get("version") != 2 or type(value.get("enabled")) is not bool:
        return None
    return value


def snapshot_from_record(record: dict[str, Any], target: Path, name: str) -> SessionSnapshot | None:
    raw = record.get("snapshot")
    if not isinstance(raw, dict) or raw.get("harness") not in {"claude", "codex"}:
        return None
    try:
        snapshot = SessionSnapshot(**{**raw, "dirty_paths": tuple(raw["dirty_paths"])})
        if (
            snapshot.checkout_path != str(target)
            or snapshot.cloud_context is not None
            or record_name(snapshot.harness, snapshot.session_id) != name
        ):
            return None
        return snapshot
    except (TypeError, KeyError, ValueError):
        return None
