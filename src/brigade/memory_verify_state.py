"""`.brigade/memory/verify/state.json`: the verification job's private state.

The file follows the rules of the reflect `schedule.json`: owner-only, read
through descriptors without following links, written by an exclusive temporary
file and an atomic rename, and guarded by its own lock file. It is limited to
8 MiB. Anything oversized, malformed or unsafe fails closed with one fixed
reason that names the file and carries none of its bytes. Recovery is to
inspect and remove the file, as for `schedule.json`.

The file holds key digests, days, revisions, verdict codes, proposal ids and
read counts. It never holds card ids, card text, titles or evidence. Its pause
counter is separate from the reflect job's.
"""

from __future__ import annotations

import contextlib
import os
import re
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from . import memory_hub_client as hubc
from . import memory_proposal_io as io
from .inbox_lock import InboxLockTimeout, held_file_lock

ROOT = ".brigade/memory/verify"
STATE = f"{ROOT}/state.json"
LOCK = f"{ROOT}/state.lock"
SCHEMA = "brigade.memory-verify-state.v1"
MAX_BYTES = 8 * 1024 * 1024
FAILURES = 3
MAX_SCOPES = hubc.READ_SCOPES_MAX
MAX_SCOPE_CARDS = 50_000
STATE_ERROR = "invalid verify state. Inspect and remove .brigade/memory/verify/state.json to recover"
LOCK_ERROR = "verify state is locked by another run"

KEY = re.compile(r"[0-9a-f]{32}")
DIGEST = re.compile(r"[0-9a-f]{64}")
CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")
TOP_KEYS = frozenset({"schema", "consecutive_failures", "paused", "last_status", "binding", "scopes"})
SCOPE_KEYS = frozenset({"since_day", "never_read", "counts_ms", "cards"})
CARD_KEYS = frozenset({"seen", "proposed", "verified", "revision", "evidence", "verdict", "pending", "reads"})

assert io.MAX_STATE_BYTES == MAX_BYTES  # The bounded reader and writer enforce this limit.


class VerifyStateError(ValueError):
    """Fixed text only. Never formatted with state contents or paths from the file."""

    def __init__(self, reason: str = STATE_ERROR) -> None:
        super().__init__(reason)


def fresh() -> dict[str, Any]:
    return {"schema": SCHEMA, "consecutive_failures": 0, "paused": False, "scopes": {}}


def _count(value: object) -> bool:
    return type(value) is int and value >= 0


def _card(entry: object) -> bool:
    if not isinstance(entry, dict) or set(entry) - CARD_KEYS or not {"seen", "proposed"} <= set(entry):
        return False
    if not all(_count(entry.get(name)) for name in ("seen", "proposed")):
        return False
    if any(name in entry and not _count(entry[name]) for name in ("verified", "revision")):
        return False
    if "evidence" in entry and not (isinstance(entry["evidence"], str) and DIGEST.fullmatch(entry["evidence"])):
        return False
    if "verdict" in entry and not (isinstance(entry["verdict"], str) and CODE.fullmatch(entry["verdict"])):
        return False
    if "pending" in entry and not (isinstance(entry["pending"], str) and hubc.event_id_ms(entry["pending"])):
        return False
    # A present key must hold a value: an explicit null is as invalid as a wrong type.
    if "reads" not in entry:
        return True
    reads = entry["reads"]
    return isinstance(reads, list) and len(reads) == 3 and all(_count(value) for value in reads)


def validate(state: object) -> dict[str, Any]:
    """Return `state` when it has exactly the documented shape, or raise VerifyStateError."""
    if not isinstance(state, dict) or set(state) - TOP_KEYS or state.get("schema") != SCHEMA:
        raise VerifyStateError()
    count, paused = state.get("consecutive_failures"), state.get("paused")
    if type(count) is not int or count < 0 or type(paused) is not bool or paused != (count >= FAILURES):
        raise VerifyStateError()
    if "last_status" in state and state["last_status"] not in ("completed", "failed"):
        raise VerifyStateError()
    if "binding" in state and not (isinstance(state["binding"], str) and DIGEST.fullmatch(state["binding"])):
        raise VerifyStateError()
    scopes = state.get("scopes")
    if not isinstance(scopes, dict) or len(scopes) > MAX_SCOPES:
        raise VerifyStateError()
    for name, table in scopes.items():
        if not hubc.valid_scope(name) or not isinstance(table, dict) or set(table) - SCOPE_KEYS:
            raise VerifyStateError()
        # since_day and never_read are documented as nullable: null means not counted yet or unknown.
        for field in ("since_day", "never_read"):
            if table.get(field) is not None and not _count(table[field]):
                raise VerifyStateError()
        # When the cached read counts were observed, in ms since the epoch. Never null.
        if "counts_ms" in table and not _count(table["counts_ms"]):
            raise VerifyStateError()
        cards = table.get("cards", {})
        if not isinstance(cards, dict) or len(cards) > MAX_SCOPE_CARDS:
            raise VerifyStateError()
        if not all(isinstance(key, str) and KEY.fullmatch(key) and _card(entry) for key, entry in cards.items()):
            raise VerifyStateError()
    return state


def load(target: Path) -> dict[str, Any]:
    """The current state, or a fresh one when the file is absent. Fails closed on anything else."""
    try:
        state = io.read_object(target / STATE, private=True)
    except FileNotFoundError:
        return fresh()
    except (OSError, ValueError, UnicodeError, RecursionError):
        raise VerifyStateError() from None
    return validate(state)


def save(target: Path, state: dict[str, Any]) -> None:
    """Validate, then replace the file atomically. A state over 8 MiB is refused and the old file stays."""
    data = io.json_bytes(validate(state))
    if len(data) > MAX_BYTES:
        raise VerifyStateError()
    path = target / STATE
    temporary = path.with_name(f".{uuid.uuid4().hex}.tmp")
    try:
        io.write_exclusive(temporary, data)
        with io.directory(path.parent, private=True) as parent:
            try:
                os.replace(temporary.name, path.name, src_dir_fd=parent, dst_dir_fd=parent)
                os.fsync(parent)
            finally:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(temporary.name, dir_fd=parent)
    except (OSError, ValueError):
        raise VerifyStateError() from None


@contextlib.contextmanager
def locked(target: Path, *, deadline_seconds: float = 1.0) -> Iterator[None]:
    """Hold the verify state lock. Another holder makes this fail with a fixed reason."""
    lock = target / LOCK
    with contextlib.ExitStack() as stack:
        try:
            io.protect_file(lock)
            stack.enter_context(held_file_lock(lock, deadline_seconds=deadline_seconds))
        except InboxLockTimeout:
            raise VerifyStateError(LOCK_ERROR) from None
        except (OSError, ValueError):
            raise VerifyStateError() from None
        yield


def record_run(state: dict[str, Any], status: str) -> dict[str, Any]:
    """Count a finished run. Three consecutive failures pause the scheduled job; a success clears it."""
    if status not in ("completed", "failed"):
        raise ValueError("run status must be completed or failed")
    count = state["consecutive_failures"] + 1 if status == "failed" else 0
    state.update(consecutive_failures=count, paused=count >= FAILURES, last_status=status)
    return state


def summary(target: Path) -> dict[str, Any]:
    """Pause state for status output. A bad file reads as paused with the fixed reason."""
    try:
        state = load(target)
    except VerifyStateError as exc:
        return {"paused": True, "state_error": str(exc)}
    report: dict[str, Any] = {key: state[key] for key in ("consecutive_failures", "paused")}
    if "last_status" in state:
        report["last_status"] = state["last_status"]
    report["never_read"] = {name: table.get("never_read") for name, table in sorted(state["scopes"].items())}
    report["tracked_cards"] = sum(len(table.get("cards", {})) for table in state["scopes"].values())
    return report
