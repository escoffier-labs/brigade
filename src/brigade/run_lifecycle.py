"""Opt-in lifecycle journaling integration for brigade run status transitions.

Appends ``brigade.run_event.v1`` envelopes to the per-run lifecycle journal
BEFORE each existing ``run.json`` status snapshot refresh, gated by an opt-in
feature flag (``BRIGADE_LIFECYCLE_JOURNAL``) that is **per-run and durable**.

Activation model
----------------
- Flag off: byte-identical behavior, no journal, no event, no request field.
- New run started with the flag on: the pre-lock bootstrap writes
  ``lifecycle_journal_requested: true`` into ``run.json`` but never creates
  ``events/lifecycle.jsonl``. The request field is the durable opt-in marker
  until journaling activates.
- Legacy ``run.json`` with no request field stays snapshot-only even if the
  environment flag is later enabled.
- Once the matching run lock is held, the first mapped-status write with a
  pending request creates the journal (0o700 directory, 0o600 file) and
  appends ``run.created``. Journal existence is the durable activated fact
  after that point.
- Once the journal exists, later transitions remain journaled even if a
  resumed process lacks the environment flag.

Lock ownership
--------------
Every append happens under the active run lock. ``record_run_start`` is
written once before ``cli/run.py`` acquires ``runguard.run_lock`` (the
pre-lock bootstrap, which records the request but never creates the journal)
and again inside the held lock through ``aboyeur.run``. Ownership is verified
with ``runguard.is_active_run_owner`` against the workspace identified by the
run receipt payload (``lock_workspace`` or ``cwd``, resolved by
``runguard.resolve_run_lock_workspace``), never derived from the run
directory layout, so custom ``--output-dir`` runs verify against the lock
they actually hold. Once journaling is active, a mapped-status write without
the matching active lock fails closed as ``LifecycleJournalError`` and the
caller's ``run.json`` snapshot does not advance.

Transition identity
-------------------
Events record status *transitions*, not detail refreshes. The prior canonical
``run.json`` snapshot is read and hashed before appending. When the prior
status equals the target status and the journal already holds a committed
event, nothing is appended even if error/detail fields changed. A real
transition uses an idempotency key derived from a fixed prefix plus the prior
snapshot digest and target event request: a crash/retry after the journal fsync
but before the ``run.json`` replacement sees the unchanged prior snapshot,
derives the same key, and ``append_event`` returns the committed event
without a second append. A later legitimate recurrence
(dispatching -> result-processing -> dispatching, including across an
unmapped intermediate status) has a different prior snapshot digest and
appends. The journal tail payload is never used to dedupe.

Bounded failure
---------------
Any ``run_journal`` typed error surfaces its already-bounded ``diagnostic``;
raw ``OSError`` and ``run_events.CanonicalizationError`` from snapshot
reads, ensure/read/append, or key derivation are wrapped as
``LifecycleJournalError`` with a generic bounded category (the raw text can
carry a path or private value) and raised before the caller writes
``run.json``.

Privacy exclusions are structural: payloads are built from the closed per-type
allowlist only, so no prompts, model output, tool arguments, credentials,
provider bodies, stack traces, or raw ``run.json`` error strings can enter a
payload.

Standard library only. Brigade is zero-runtime-dependency.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from brigade import run_events, run_journal, runguard

_FLAG_ENV = "BRIGADE_LIFECYCLE_JOURNAL"
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_JOURNAL_NAME = "lifecycle.jsonl"
_REQUEST_FIELD = "lifecycle_journal_requested"
_IDEMPOTENCY_PREFIX = "lifecycle"
_IO_CATEGORY = "lifecycle journal I/O failure"
_CANONICALIZATION_CATEGORY = "lifecycle journal canonicalization failure"

# Map of current run.json status values to the allowlisted run_event.v1
# event_type that records that transition. Statuses without a mapping
# (e.g. "dry-run", "incomplete", "artifact-collection") are skipped: no event
# is appended and the run.json snapshot refresh proceeds unchanged. This keeps
# the integration to the current status transitions and the existing event
# registry -- no new schemas.
STATUS_EVENT_TYPE: dict[str, str] = {
    "started": "run.created",
    "planning": "run.planning.started",
    "dispatching": "run.dispatch.requested",
    "result-processing": "run.dispatch.completed",
    "synthesizing": "run.synthesis.started",
    "handoff": "run.synthesis.completed",
    "ok": "run.completed",
    "failed": "run.failed",
    "canceled": "run.interrupted",
    "timeout": "run.failed",
}


class LifecycleJournalError(RuntimeError):
    """Bounded lifecycle-journal failure; run.json must not advance past it."""


def is_lifecycle_journaling_enabled() -> bool:
    """True when the opt-in lifecycle journal flag is set in the environment."""
    return os.environ.get(_FLAG_ENV, "").strip().lower() in _TRUTHY


def _run_id_from_dir(run_dir: Path) -> str:
    return run_dir.expanduser().resolve().name


def _journal_path(run_dir: Path) -> Path:
    return run_dir / "events" / _JOURNAL_NAME


def _journal_requested(
    run_dir: Path,
    *,
    incoming: Mapping[str, Any] | None,
) -> bool:
    """True when this run has durably requested lifecycle journaling."""
    run_json = run_dir / "run.json"
    if run_json.is_file():
        try:
            meta = json.loads(run_json.read_bytes())
        except (OSError, ValueError, UnicodeDecodeError):
            return False
        return isinstance(meta, dict) and meta.get(_REQUEST_FIELD) is True
    if incoming is not None and incoming.get(_REQUEST_FIELD) is True:
        return True
    return False


def _run_snapshot_state(run_dir: Path) -> tuple[str | None, str | None]:
    """Return (status, sha256 digest) of the current canonical run.json snapshot.

    The digest binds the exact prior snapshot bytes; the status drives
    transition detection. Returns (None, None) when run.json is absent. A
    present-but-unparseable snapshot still yields its digest with a None
    status. Other read failures propagate as OSError so the caller fails
    closed with a bounded category.
    """
    try:
        raw = (run_dir / "run.json").read_bytes()
    except FileNotFoundError:
        return None, None
    digest = hashlib.sha256(raw).hexdigest()
    status: str | None = None
    try:
        meta = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        meta = None
    if isinstance(meta, dict):
        candidate = meta.get("status")
        if isinstance(candidate, str) and candidate:
            status = candidate
    return status, digest


def _allowlisted_payload(event_type: str, *, status: str) -> dict[str, Any]:
    """Build a payload carrying only keys in the closed per-type allowlist."""
    allowed = run_events.EVENT_TYPES[event_type]
    payload: dict[str, Any] = {}
    if "status" in allowed:
        payload["status"] = status
    if "detail" in allowed:
        text = status
        if len(text) > run_events.MAX_PAYLOAD_STR_LEN:
            text = text[: run_events.MAX_PAYLOAD_STR_LEN]
        payload["detail"] = text
    return payload


def record_lifecycle_transition(
    run_dir: Path,
    *,
    status: str,
    workspace: Path | None = None,
    incoming_snapshot: Mapping[str, Any] | None = None,
) -> run_journal.RunEvent | None:
    """Append one lifecycle event for a run status transition.

    Called BEFORE the existing ``run.json`` snapshot refresh. Returns the
    appended (or replayed) ``RunEvent`` when journaling is active and the
    status transition is recorded; ``None`` when the status is unmapped, the
    run is not activated, journaling is only requested but the matching lock
    is not yet held, or the call only recorded the per-run request. Raises
    ``LifecycleJournalError`` on any bounded journal failure, or when
    journaling is active but the caller does not hold the matching active run
    lock, so the caller does not write ``run.json`` past an uncommitted or
    unowned transition.

    ``workspace`` identifies the run lock to verify ownership against; pass
    the workspace from the run receipt payload (``lock_workspace`` or
    ``cwd``), as resolved by ``runguard.resolve_run_lock_workspace``.
    """
    run_dir = Path(run_dir).expanduser().resolve()
    event_type = STATUS_EVENT_TYPE.get(status)
    if event_type is None:
        # Unmapped statuses are not lifecycle transitions: no event, no
        # activation, no ownership gate; the snapshot refresh proceeds.
        return None
    run_id = _run_id_from_dir(run_dir)
    if not run_events._RUN_ID_RE.match(run_id):
        raise LifecycleJournalError(run_events._bound(f"invalid run_id from run_dir: {run_id!r}"))
    journal_path = _journal_path(run_dir)
    requested = _journal_requested(run_dir, incoming=incoming_snapshot)
    if not journal_path.is_file():
        if not requested:
            return None
        if workspace is None or not runguard.is_active_run_owner(workspace, run_dir):
            return None
        try:
            run_journal.ensure_journal(journal_path)
        except run_journal.RunJournalError as exc:
            raise LifecycleJournalError(run_events._bound(exc.diagnostic)) from exc
        except run_events.CanonicalizationError as exc:
            raise LifecycleJournalError(_CANONICALIZATION_CATEGORY) from exc
        except OSError as exc:
            raise LifecycleJournalError(f"{_IO_CATEGORY} ({type(exc).__name__})") from exc

    # The journal exists: journaling is active for this run, with or without
    # the env flag. Every append must come from the process holding the
    # matching active run lock; anything else fails closed so run.json never
    # advances past an uncommitted or unowned transition.
    if workspace is None or not runguard.is_active_run_owner(workspace, run_dir):
        raise LifecycleJournalError("lifecycle journal append requires the active run lock for this run")

    try:
        run_journal.ensure_journal(journal_path)
        report = run_journal.read_journal(journal_path)
        if report.partial_tail is not None or report.chain_errors:
            raise run_journal.ChainIntegrityError(run_events._bound("lifecycle journal is not derivable"))
        prior_status, prior_digest = _run_snapshot_state(run_dir)
        payload = _allowlisted_payload(event_type, status=status)
        # Status transitions, not detail refreshes: a same-status refresh
        # appends nothing once any event is committed, even when error/detail
        # fields changed.
        if prior_status == status and report.events:
            return report.events[-1]
        # Real transition: the idempotency key binds the prior snapshot digest
        # to the target event request. A crash/retry after the journal fsync
        # but before the run.json replacement sees the unchanged prior
        # snapshot, derives the same key, and append_event returns the
        # committed event; a later genuine recurrence has a different prior
        # snapshot digest and appends. The fixed prefix keeps the key within
        # the 128-char envelope bound regardless of run directory name length.
        key_digest = hashlib.sha256(
            run_events.canonical_bytes(
                {
                    "event_type": event_type,
                    "payload": payload,
                    "prior_snapshot_digest": prior_digest,
                }
            )
        ).hexdigest()
        idempotency_key = f"{_IDEMPOTENCY_PREFIX}:{key_digest[:32]}"
        return run_journal.append_event(
            journal_path,
            run_id=run_id,
            event_type=event_type,
            payload=payload,
            idempotency_key=idempotency_key,
            expected_previous_sequence=report.events[-1].sequence if report.events else 0,
        )
    except run_journal.RunJournalError as exc:
        raise LifecycleJournalError(run_events._bound(exc.diagnostic)) from exc
    except run_events.CanonicalizationError as exc:
        raise LifecycleJournalError(_CANONICALIZATION_CATEGORY) from exc
    except OSError as exc:
        raise LifecycleJournalError(f"{_IO_CATEGORY} ({type(exc).__name__})") from exc


__all__ = [
    "LifecycleJournalError",
    "STATUS_EVENT_TYPE",
    "is_lifecycle_journaling_enabled",
    "record_lifecycle_transition",
]
