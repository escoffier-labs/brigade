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
- ``prepare_lifecycle_journal`` (called by ``aboyeur._write_json`` before
  every ``run.json`` write, and by ``run_checkpoint.write_checkpoint``) idempotently creates
  the journal (0o700 directory, 0o600 file) the first time the matching run
  lock is held AND the run carries a pending ``lifecycle_journal_requested``
  marker. Journal existence is the durable activated fact after that point.
  ``record_lifecycle_transition`` no longer creates the journal; it requires
  the journal to already exist and skips (returns ``None``) when it does not.
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
import signal
import stat
import threading
import time
from contextlib import contextmanager
from collections.abc import Mapping
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from brigade import run_checkpoint, run_events, run_journal, runguard

_FLAG_ENV = "BRIGADE_LIFECYCLE_JOURNAL"
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_JOURNAL_NAME = "lifecycle.jsonl"
_REQUEST_FIELD = "lifecycle_journal_requested"
_IDEMPOTENCY_PREFIX = "lifecycle"
_IO_CATEGORY = "lifecycle journal I/O failure"
_CANONICALIZATION_CATEGORY = "lifecycle journal canonicalization failure"
_CHAIN_CATEGORY = "lifecycle journal is not derivable"
_MAX_OWNER_APPEND_ATTEMPTS = 4

# Map of current run.json status values to the allowlisted run_event.v1
# event_type that records that transition. Statuses without a mapping are
# skipped: no event is appended and the run.json snapshot refresh proceeds
# unchanged.
STATUS_EVENT_TYPE: dict[str, str] = {
    "started": "run.created",
    "planning": "run.planning.started",
    "dispatching": "run.dispatching.started",
    "result-processing": "run.result-processing.started",
    "synthesizing": "run.synthesis.started",
    "handoff": "run.synthesis.completed",
    "ok": "run.completed",
    "failed": "run.failed",
    "canceled": "run.interrupted",
    "timeout": "run.failed",
    "dry-run": "run.completed",
    "incomplete": "run.failed",
    "artifact-collection": "run.artifact_collection.started",
    "paused": "run.paused",
    "running": "run.resumed",
}

_CHECKPOINT_EVENT_PAIR_LOCK = threading.Lock()
_CHECKPOINT_EVENT_PAIR_STATE = threading.local()
_DISPATCH_FACT_TYPES = frozenset(
    {
        "run.dispatch.requested",
        "run.dispatch.observed",
        "run.dispatch.completed",
        "run.dispatch.failed",
    }
)
APPROVAL_REFERENCE_REQUIRED_FIELDS = frozenset(
    {
        "approval_id",
        "source",
        "fingerprint",
        "source_fingerprint",
        "contract_fingerprint",
        "evidence_fingerprint",
    }
)
APPROVAL_REFERENCE_OPTIONAL_FIELDS = frozenset(
    {
        "decision_state",
        "decided_at",
        "consuming_run_id",
    }
)
APPROVAL_REFERENCE_FIELDS = APPROVAL_REFERENCE_REQUIRED_FIELDS | APPROVAL_REFERENCE_OPTIONAL_FIELDS


class LifecycleJournalError(RuntimeError):
    """Bounded lifecycle-journal failure; run.json must not advance past it."""


@contextmanager
def checkpoint_event_pair() -> Iterator[None]:
    """Serialize one checkpoint/event pair and defer SIGTERM across its gap.

    The lock is separate from ``run_journal``'s append lock. It spans the
    checkpoint and paired event at the process level, so worker threads and
    interruption writers cannot interleave pair members. SIGTERM is blocked
    in the entering thread before it waits for the lock and restored to the
    exact prior mask only after the pair lock is released. That ordering lets
    a pending main-thread handler enter the writer normally instead of
    reentering the protected region while its lock is still held.
    """
    if getattr(_CHECKPOINT_EVENT_PAIR_STATE, "active", False):
        raise LifecycleJournalError(run_events._bound("checkpoint/event pair reentry is not allowed"))

    previous_mask: set[int | signal.Signals] | None = None
    if hasattr(signal, "pthread_sigmask"):
        try:
            previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
        except (OSError, ValueError) as exc:
            raise LifecycleJournalError(run_events._bound("checkpoint/event pair signal mask failed")) from exc

    acquired = False
    restore_error: BaseException | None = None
    try:
        # Mark the whole wait-and-hold interval active. On platforms where
        # SIGTERM cannot be masked, a same-thread signal handler that runs
        # while lock acquisition is waiting must fail bounded instead of
        # trying to acquire this non-reentrant lock again.
        _CHECKPOINT_EVENT_PAIR_STATE.active = True
        _CHECKPOINT_EVENT_PAIR_LOCK.acquire()
        acquired = True
        yield
    finally:
        _CHECKPOINT_EVENT_PAIR_STATE.active = False
        if acquired:
            _CHECKPOINT_EVENT_PAIR_LOCK.release()
        if previous_mask is not None:
            try:
                signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
            except (OSError, ValueError) as exc:
                restore_error = exc
        if restore_error is not None:
            raise LifecycleJournalError(
                run_events._bound("checkpoint/event pair signal mask restoration failed")
            ) from restore_error


def is_lifecycle_journaling_enabled() -> bool:
    """True when the opt-in lifecycle journal flag is set in the environment."""
    return os.environ.get(_FLAG_ENV, "").strip().lower() in _TRUTHY


def _run_id_from_dir(run_dir: Path) -> str:
    return run_dir.expanduser().resolve().name


def _journal_path(run_dir: Path) -> Path:
    return run_dir / "events" / _JOURNAL_NAME


def _dispatch_journal_path(run_dir: Path) -> Path | None:
    """Return the active regular journal, or ``None`` only for true legacy.

    Dispatch is the last boundary before an external worker invocation. A
    missing, renamed, or substituted journal after durable lifecycle or
    authority enrollment must fail closed there. A snapshot is considered
    truly unenrolled only when it is a regular JSON object and carries none
    of the durable request or projection metadata fields.
    """
    journal_path = _journal_path(run_dir)
    try:
        journal_mode = journal_path.lstat().st_mode
    except FileNotFoundError:
        journal_mode = None
    except OSError as exc:
        raise LifecycleJournalError(run_events._bound("lifecycle journal enrollment check failed")) from exc
    if journal_mode is not None:
        if not stat.S_ISREG(journal_mode):
            raise LifecycleJournalError(run_events._bound("enrolled lifecycle journal is not a regular file"))
        return journal_path

    run_json = run_dir / "run.json"
    try:
        run_mode = run_json.lstat().st_mode
    except FileNotFoundError as exc:
        raise LifecycleJournalError(run_events._bound("dispatch run receipt is missing")) from exc
    except OSError as exc:
        raise LifecycleJournalError(run_events._bound("dispatch run receipt enrollment check failed")) from exc
    if not stat.S_ISREG(run_mode):
        raise LifecycleJournalError(run_events._bound("dispatch run receipt is not a regular file"))
    try:
        meta = json.loads(run_json.read_bytes())
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise LifecycleJournalError(run_events._bound("dispatch run receipt enrollment is unreadable")) from exc
    if not isinstance(meta, dict):
        raise LifecycleJournalError(run_events._bound("dispatch run receipt enrollment is unreadable"))
    enrollment_fields = {
        _REQUEST_FIELD,
        "run_journal_authority_requested",
        "projector_version",
        "journal_present",
        "journal_last_sequence",
        "journal_last_event_digest",
    }
    if any(field in meta for field in enrollment_fields):
        raise LifecycleJournalError(run_events._bound("enrolled lifecycle journal is missing"))
    return None


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


def pending_dispatch_requests(events: list[run_journal.RunEvent]) -> list[tuple[str, int | None]]:
    """Return dispatch requests without a terminal observation.

    A completed, failed, or observed fact closes the matching seat/attempt.
    Missing or malformed legacy payloads remain explicit unknown
    at-least-once work instead of being silently discarded.
    """
    pending: list[tuple[str, int | None]] = []
    for event in events:
        if event.event_type == "run.dispatch.requested":
            seat = event.payload.get("seat")
            attempt = event.payload.get("attempt")
            # Historical aggregate dispatch status events carried only a
            # detail field. They do not identify a worker action and must not
            # manufacture perpetual recovery work for every legacy run.
            if seat is None and attempt is None:
                continue
            valid_seat = seat if isinstance(seat, str) and seat else "unknown"
            valid_attempt = (
                attempt if isinstance(attempt, int) and not isinstance(attempt, bool) and attempt > 0 else None
            )
            pending.append((valid_seat, valid_attempt))
            continue
        if event.event_type not in {"run.dispatch.observed", "run.dispatch.completed", "run.dispatch.failed"}:
            continue
        seat = event.payload.get("seat")
        attempt = event.payload.get("attempt")
        if (
            not isinstance(seat, str)
            or not seat
            or isinstance(attempt, bool)
            or not isinstance(attempt, int)
            or attempt < 1
        ):
            continue
        pending = [item for item in pending if item != (seat, attempt)]
    return pending


def next_dispatch_attempt(run_dir: Path, seat: str) -> int:
    """Return the stable attempt id for the next reservation of ``seat``.

    Reuses an open pending ``run.dispatch.requested`` for the seat when one
    exists so crash/retry before observed/completed/failed keeps the same
    budget reservation identity. Otherwise allocates ``max(prior)+1``.
    Missing or inactive journals start at attempt 1.
    """
    if not isinstance(seat, str) or not seat:
        raise LifecycleJournalError(run_events._bound("dispatch fact seat must be non-empty"))
    run_dir = Path(run_dir).expanduser().resolve()
    journal_path = _dispatch_journal_path(run_dir)
    if journal_path is None:
        return 1
    try:
        report = run_journal.read_journal_bounded(journal_path)
    except (OSError, run_journal.RunJournalError):
        return 1
    if report.partial_tail is not None or report.chain_errors:
        raise LifecycleJournalError(run_events._bound(_CHAIN_CATEGORY))
    for pending_seat, pending_attempt in pending_dispatch_requests(report.events):
        if pending_seat == seat and pending_attempt is not None:
            return pending_attempt
    attempts: list[int] = []
    for prior_event in report.events:
        if prior_event.payload.get("seat") != seat:
            continue
        candidate = prior_event.payload.get("attempt")
        if isinstance(candidate, int) and not isinstance(candidate, bool) and candidate > 0:
            attempts.append(candidate)
    return max(attempts, default=0) + 1


def record_dispatch_fact(
    run_dir: Path,
    *,
    workspace: Path | None,
    event_type: str,
    seat: str,
    attempt: int | None = None,
) -> run_journal.RunEvent | None:
    """Append one paired per-invocation dispatch fact under a process lock.

    Each worker transport call receives its own checkpoint-event pair. The
    lock covers both appends so wave and DAG workers cannot interleave a
    checkpoint with another seat's fact. The event payload deliberately holds
    only the selected seat and its monotonically allocated attempt identity.
    """
    if event_type not in _DISPATCH_FACT_TYPES:
        raise LifecycleJournalError(run_events._bound("invalid dispatch fact event type"))
    if not isinstance(seat, str) or not seat:
        raise LifecycleJournalError(run_events._bound("dispatch fact seat must be non-empty"))
    if attempt is None and event_type != "run.dispatch.requested":
        raise LifecycleJournalError(run_events._bound("only dispatch requested may allocate an attempt"))
    if attempt is not None and (isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1):
        raise LifecycleJournalError(run_events._bound("dispatch fact attempt must be a positive integer"))

    run_dir = Path(run_dir).expanduser().resolve()
    with checkpoint_event_pair():
        journal_path = _dispatch_journal_path(run_dir)
        if journal_path is None:
            return None
        if workspace is None or not runguard.is_active_run_owner(workspace, run_dir):
            raise LifecycleJournalError("lifecycle journal append requires the active run lock for this run")
        try:
            # Import lazily to avoid the aboyeur -> lifecycle module cycle.
            # These are the same authority gates used by run.json status
            # writers, now applied before every dispatch pair.
            from brigade import aboyeur, run_shadow

            snapshot = (run_dir / "run.json").read_bytes()
            try:
                snapshot_obj = json.loads(snapshot)
            except (ValueError, UnicodeDecodeError) as exc:
                raise LifecycleJournalError(run_events._bound("dispatch run receipt is not valid JSON")) from exc
            if not isinstance(snapshot_obj, dict):
                raise LifecycleJournalError(run_events._bound("dispatch run receipt is not a JSON object"))
            authority_state = aboyeur._resolve_authority_state(run_dir)
            report = run_journal.read_journal_bounded(journal_path)
            if report.partial_tail is not None or report.chain_errors:
                raise run_journal.ChainIntegrityError(run_events._bound(_CHAIN_CATEGORY))
            if attempt is None:
                attempts: list[int] = []
                for prior_event in report.events:
                    candidate = prior_event.payload.get("attempt")
                    if (
                        prior_event.payload.get("seat") == seat
                        and isinstance(candidate, int)
                        and not isinstance(candidate, bool)
                    ):
                        attempts.append(candidate)
                attempt = max(attempts, default=0) + 1
            assert attempt is not None
            payload = {"seat": seat, "attempt": attempt, "detail": event_type.rsplit(".", 1)[-1]}
            pairing_key = run_checkpoint.dispatch_pairing_key(event_type, seat, attempt)
            key_digest = hashlib.sha256(
                run_events.canonical_bytes(
                    {"event_type": event_type, "seat": seat, "attempt": attempt, "pairing_key": pairing_key}
                )
            ).hexdigest()
            idempotency_key = f"dispatch:{key_digest[:32]}"
            if authority_state == "authoritative":
                recovered = _recover_authoritative_dispatch_checkpoint(
                    run_dir,
                    journal_path=journal_path,
                    journal_report=report,
                    snapshot=snapshot,
                    snapshot_obj=snapshot_obj,
                    event_type=event_type,
                    pairing_key=pairing_key,
                    payload=payload,
                    idempotency_key=idempotency_key,
                )
                if recovered is not None:
                    return recovered
                # The shared prior gate catches up exactly one verified
                # checkpoint/event pair before any new dispatch or aggregate
                # status pair can advance the journal again.
                aboyeur._authoritative_prior_decision(run_dir, snapshot_obj)
            checkpoint = run_checkpoint.write_checkpoint(
                run_dir,
                snapshot,
                workspace=workspace,
                paired_event_type=event_type,
                body_kind=(
                    run_checkpoint._BODY_KIND_BASE_STRIPPED
                    if snapshot_obj.get("run_journal_authority_requested") is True
                    else None
                ),
                pairing_key=pairing_key,
            )
            if checkpoint is None:
                raise LifecycleJournalError(run_events._bound("enrolled lifecycle journal checkpoint was not recorded"))
            report = run_journal.read_journal_bounded(journal_path)
            if report.partial_tail is not None or report.chain_errors:
                raise run_journal.ChainIntegrityError(run_events._bound(_CHAIN_CATEGORY))
            event = _append_owner_event(
                journal_path,
                run_id=_run_id_from_dir(run_dir),
                event_type=event_type,
                payload=payload,
                idempotency_key=idempotency_key,
            )
            run_shadow.record_shadow_comparison(run_dir, snapshot_obj)
            return event
        except run_journal.RunJournalError as exc:
            raise _bound_journal_failure(exc) from exc
        except run_events.CanonicalizationError as exc:
            raise _bound_journal_failure(exc) from exc
        except OSError as exc:
            raise _bound_journal_failure(exc) from exc


def _recover_authoritative_dispatch_checkpoint(
    run_dir: Path,
    *,
    journal_path: Path,
    journal_report: run_journal.JournalReport,
    snapshot: bytes,
    snapshot_obj: dict[str, object],
    event_type: str,
    pairing_key: str,
    payload: dict[str, object],
    idempotency_key: str,
) -> run_journal.RunEvent | None:
    """Finish only the exact dispatch fact left after its durable checkpoint.

    A checkpoint-only tail is a narrow crash window: the original dispatch
    request identity is recoverable, but a generic authority catch-up would
    treat it as ordinary journal-ahead evidence. Every field below binds the
    missing fact to this run's current base-stripped receipt. Any divergence
    raises before a new checkpoint, retry, shadow write, or run.json write.
    """
    from brigade import run_shadow

    readiness = run_shadow.check_projection_readiness(run_dir)
    if readiness.reasons != (run_shadow.REASON_JOURNAL_AHEAD,):
        return None
    if not journal_report.events or journal_report.events[-1].event_type != run_checkpoint.CHECKPOINT_EVENT_TYPE:
        return None

    checkpoint = journal_report.events[-1]
    artifact_path = run_shadow.shadow_artifact_path(run_dir)
    try:
        artifact = json.loads(artifact_path.read_text())
    except (OSError, ValueError) as exc:
        raise LifecycleJournalError(
            run_events._bound("authoritative dispatch checkpoint recovery evidence is unreadable")
        ) from exc
    if not isinstance(artifact, dict):
        raise LifecycleJournalError(run_events._bound("authoritative dispatch checkpoint recovery evidence is invalid"))
    baseline = run_shadow._verify_stale_baseline(
        run_dir,
        run_dir.name,
        artifact.get("last_compared_sequence"),
        artifact.get("last_compared_event_digest"),
    )
    if baseline is None or checkpoint.sequence != baseline[0] + 1:
        raise LifecycleJournalError(run_events._bound("authoritative dispatch checkpoint recovery is not exact"))
    if run_checkpoint._writer_canonical_bytes(snapshot_obj) != snapshot:
        raise LifecycleJournalError(run_events._bound("authoritative dispatch checkpoint recovery is not exact"))
    try:
        expected_bytes = run_checkpoint._strip_journal_metadata_from_base(snapshot)
        checkpoint_bytes = run_checkpoint.validate_checkpoint(run_dir, checkpoint)
    except (run_checkpoint.CheckpointError, RecursionError) as exc:
        raise LifecycleJournalError(
            run_events._bound("authoritative dispatch checkpoint recovery is not exact")
        ) from exc

    expected_payload = run_checkpoint._checkpoint_payload(
        expected_bytes,
        paired_event_type=event_type,
        body_kind=run_checkpoint._BODY_KIND_BASE_STRIPPED,
        pairing_key=pairing_key,
    )
    expected_checkpoint_key = run_checkpoint._checkpoint_idempotency_key(
        expected_payload["sha256"],
        paired_event_type=event_type,
        body_kind=run_checkpoint._BODY_KIND_BASE_STRIPPED,
        pairing_key=pairing_key,
    )
    if (
        checkpoint.run_id != run_dir.name
        or checkpoint.event_type != run_checkpoint.CHECKPOINT_EVENT_TYPE
        or checkpoint.payload != expected_payload
        or checkpoint.idempotency_key != expected_checkpoint_key
        or checkpoint_bytes != expected_bytes
    ):
        raise LifecycleJournalError(run_events._bound("authoritative dispatch checkpoint recovery is not exact"))
    event = run_journal.append_event(
        journal_path,
        run_id=run_dir.name,
        event_type=event_type,
        payload=payload,
        idempotency_key=idempotency_key,
        expected_previous_sequence=checkpoint.sequence,
    )
    run_shadow.record_shadow_comparison(run_dir, snapshot_obj)
    return event


def normalize_approval_reference(reference: Mapping[str, Any]) -> dict[str, str]:
    """Validate and copy the closed, reference-only approval snapshot shape."""
    if not isinstance(reference, Mapping):
        raise LifecycleJournalError("approval reference must be an object")
    unknown = sorted(set(reference) - APPROVAL_REFERENCE_FIELDS)
    if unknown:
        raise LifecycleJournalError(run_events._bound(f"approval reference has unknown fields: {unknown[:8]}"))
    missing = sorted(APPROVAL_REFERENCE_REQUIRED_FIELDS - set(reference))
    if missing:
        raise LifecycleJournalError(run_events._bound(f"approval reference is missing fields: {missing}"))
    normalized: dict[str, str] = {}
    for key in APPROVAL_REFERENCE_FIELDS:
        if key not in reference:
            continue
        value = reference[key]
        if not isinstance(value, str) or not value:
            raise LifecycleJournalError(run_events._bound(f"approval reference field {key!r} must be non-empty text"))
        if len(value) > run_events.MAX_PAYLOAD_STR_LEN:
            raise LifecycleJournalError(
                run_events._bound(f"approval reference field {key!r} exceeds {run_events.MAX_PAYLOAD_STR_LEN} chars")
            )
        if key == "decision_state" and value not in run_events.APPROVAL_DECISION_STATES:
            raise LifecycleJournalError(run_events._bound(f"approval reference has invalid decision_state {value!r}"))
        normalized[key] = value
    return normalized


def approval_idempotency_key(approval_id: str, fact: str, *, scope: str | None = None) -> str:
    """Return a bounded key without copying approval identifiers into metadata."""
    digest = hashlib.sha256(
        run_events.canonical_bytes(
            {
                "approval_id": approval_id,
                "fact": fact,
                "scope": scope,
            }
        )
    ).hexdigest()
    return f"approval:{fact}:{digest[:32]}"


def _approval_reference(incoming_snapshot: Mapping[str, Any] | None) -> dict[str, str] | None:
    if incoming_snapshot is None or "approval_reference" not in incoming_snapshot:
        return None
    raw = incoming_snapshot.get("approval_reference")
    if not isinstance(raw, Mapping):
        raise LifecycleJournalError("approval reference must be an object")
    return normalize_approval_reference(raw)


def _allowlisted_payload(
    event_type: str,
    *,
    status: str,
    incoming_snapshot: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
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
    if event_type in {"run.paused", "run.resumed"}:
        reference = _approval_reference(incoming_snapshot)
        if reference is None:
            raise LifecycleJournalError(f"{event_type} requires an approval reference")
        payload["approval_id"] = reference["approval_id"]
        if event_type == "run.paused":
            payload["reason"] = "approval-required"
    return payload


def _bound_journal_failure(exc: BaseException) -> LifecycleJournalError:
    if isinstance(exc, run_journal.RunJournalError):
        return LifecycleJournalError(run_events._bound(exc.diagnostic))
    if isinstance(exc, run_events.CanonicalizationError):
        return LifecycleJournalError(_CANONICALIZATION_CATEGORY)
    if isinstance(exc, OSError):
        return LifecycleJournalError(f"{_IO_CATEGORY} ({type(exc).__name__})")
    return LifecycleJournalError(run_events._bound(str(exc)))


def _append_owner_event(
    journal_path: Path,
    *,
    run_id: str,
    event_type: str,
    payload: Mapping[str, Any],
    idempotency_key: str,
) -> run_journal.RunEvent:
    """Append an owner event, retrying only an externally-stale tail."""
    last_stale: run_journal.StaleSequenceError | None = None
    for attempt in range(_MAX_OWNER_APPEND_ATTEMPTS):
        report = run_journal.read_journal_bounded(journal_path)
        if report.partial_tail is not None or report.chain_errors:
            raise run_journal.ChainIntegrityError(run_events._bound(_CHAIN_CATEGORY))
        try:
            return run_journal.append_event(
                journal_path,
                run_id=run_id,
                event_type=event_type,
                payload=dict(payload),
                idempotency_key=idempotency_key,
                expected_previous_sequence=report.events[-1].sequence if report.events else 0,
            )
        except run_journal.StaleSequenceError as exc:
            last_stale = exc
            if attempt + 1 == _MAX_OWNER_APPEND_ATTEMPTS:
                break
            time.sleep(0.01 * 2**attempt)
    raise LifecycleJournalError("lifecycle_append_stale_exhausted") from last_stale


def prepare_lifecycle_journal(
    run_dir: Path,
    *,
    workspace: Path | None = None,
    incoming_snapshot: Mapping[str, Any] | None = None,
) -> None:
    """Idempotently activate the per-run lifecycle journal under the matching lock.

    Creates ``events/`` (0o700) and ``lifecycle.jsonl`` (0o600) only when ALL of:
    - the journal does not already exist (idempotent no-op otherwise),
    - the run has durably requested journaling (``lifecycle_journal_requested``
      in ``run.json`` or ``incoming_snapshot``), and
    - the caller holds the matching active run lock for ``run_dir`` (verified
      via ``runguard.is_active_run_owner`` against ``workspace``, never
      derived from the run directory layout).

    No-op when the journal already exists, when journaling was never requested,
    or when the matching lock is not held. Raises a bounded
    ``LifecycleJournalError`` (generic category; the raw ``OSError`` text can
    carry a path or private value) on any ``ensure_journal`` failure.
    """
    run_dir = Path(run_dir).expanduser().resolve()
    journal_path = _journal_path(run_dir)
    if journal_path.is_file():
        return
    if not _journal_requested(run_dir, incoming=incoming_snapshot):
        return
    if workspace is None or not runguard.is_active_run_owner(workspace, run_dir):
        return
    try:
        run_journal.ensure_journal(journal_path)
    except run_journal.RunJournalError as exc:
        raise _bound_journal_failure(exc) from exc
    except run_events.CanonicalizationError as exc:
        raise _bound_journal_failure(exc) from exc
    except OSError as exc:
        raise _bound_journal_failure(exc) from exc


def record_lifecycle_event(
    run_dir: Path,
    *,
    event_type: str,
    payload: Mapping[str, Any],
    idempotency_key: str,
    workspace: Path | None,
) -> run_journal.RunEvent:
    """Append or replay one checkpoint-paired fact under the active run lock."""
    run_dir = Path(run_dir).expanduser().resolve()
    run_id = _run_id_from_dir(run_dir)
    if not run_events._RUN_ID_RE.match(run_id):
        raise LifecycleJournalError(run_events._bound(f"invalid run_id from run_dir: {run_id!r}"))
    try:
        run_events.validate_event_request(
            event_type=event_type,
            payload=payload,
            idempotency_key=idempotency_key,
        )
    except run_events.CanonicalizationError as exc:
        raise _bound_journal_failure(exc) from exc
    with checkpoint_event_pair():
        journal_path = _journal_path(run_dir)
        if not journal_path.is_file():
            raise LifecycleJournalError("lifecycle journal is not active for this run")
        if workspace is None or not runguard.is_active_run_owner(workspace, run_dir):
            raise LifecycleJournalError("lifecycle journal append requires the active run lock for this run")
        try:
            existing = run_journal.lookup_idempotent_event(
                journal_path,
                event_type=event_type,
                payload=dict(payload),
                idempotency_key=idempotency_key,
            )
            if existing is not None:
                return existing
            # Keep every explicit fact recoverable under the same authoritative
            # prior gate used by status and per-worker dispatch pairs.
            from brigade import aboyeur, run_shadow

            snapshot = (run_dir / "run.json").read_bytes()
            try:
                snapshot_obj = json.loads(snapshot)
            except (ValueError, UnicodeDecodeError) as exc:
                raise LifecycleJournalError(run_events._bound("lifecycle run receipt is not valid JSON")) from exc
            if not isinstance(snapshot_obj, dict):
                raise LifecycleJournalError(run_events._bound("lifecycle run receipt is not a JSON object"))
            if aboyeur._resolve_authority_state(run_dir) == "authoritative":
                aboyeur._authoritative_prior_decision(run_dir, snapshot_obj)
            checkpoint = run_checkpoint.write_checkpoint(
                run_dir,
                snapshot,
                workspace=workspace,
                paired_event_type=event_type,
                body_kind=(
                    run_checkpoint._BODY_KIND_BASE_STRIPPED
                    if snapshot_obj.get("run_journal_authority_requested") is True
                    else None
                ),
            )
            if checkpoint is None:
                raise LifecycleJournalError(run_events._bound("enrolled lifecycle journal checkpoint was not recorded"))
            report = run_journal.read_journal_bounded(journal_path)
            if report.partial_tail is not None or report.chain_errors:
                raise run_journal.ChainIntegrityError(run_events._bound(_CHAIN_CATEGORY))
            event = _append_owner_event(
                journal_path,
                run_id=run_id,
                event_type=event_type,
                payload=dict(payload),
                idempotency_key=idempotency_key,
            )
            run_shadow.record_shadow_comparison(run_dir, snapshot_obj)
            return event
        except run_journal.RunJournalError as exc:
            raise _bound_journal_failure(exc) from exc
        except run_events.CanonicalizationError as exc:
            raise _bound_journal_failure(exc) from exc
        except OSError as exc:
            raise _bound_journal_failure(exc) from exc


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
    run is a legacy/no-request run that never opted in, or journaling is only
    requested but the matching lock is not yet held (the pre-lock bootstrap).
    Raises ``LifecycleJournalError`` when journaling is active but the caller
    does not hold the matching active run lock, and when journaling was
    requested and the matching lock is held but the journal is absent after
    preparation (a broken activation flow), so the caller never writes
    ``run.json`` past an unowned or uncommitted transition.

    This function does not create the journal; ``prepare_lifecycle_journal``
    (called by ``aboyeur._write_json`` and ``run_checkpoint.write_checkpoint``)
    owns activation. The skip rules (unmapped status, legacy/no-request,
    requested-but-not-yet-locked) are retained.

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
    if not journal_path.is_file():
        # Journal absent. Two legitimate skip paths remain: a legacy/no-request
        # run that never opted in, and a requested run whose activation could
        # not yet happen because the matching lock is not held (the pre-lock
        # bootstrap). When journaling WAS requested AND the matching lock IS
        # held, prepare_lifecycle_journal should already have activated the
        # journal; its absence after preparation is a broken flow that must
        # fail closed rather than silently advancing run.json.
        if _journal_requested(run_dir, incoming=incoming_snapshot) and (
            workspace is not None and runguard.is_active_run_owner(workspace, run_dir)
        ):
            raise LifecycleJournalError(run_events._bound("lifecycle journal requested but not activated"))
        return None

    # The journal exists: journaling is active for this run, with or without
    # the env flag. Every append must come from the process holding the
    # matching active run lock; anything else fails closed so run.json never
    # advances past an uncommitted or unowned transition.
    if workspace is None or not runguard.is_active_run_owner(workspace, run_dir):
        raise LifecycleJournalError("lifecycle journal append requires the active run lock for this run")

    try:
        report = run_journal.read_journal_bounded(journal_path)
        if report.partial_tail is not None or report.chain_errors:
            raise run_journal.ChainIntegrityError(run_events._bound(_CHAIN_CATEGORY))
        prior_status, prior_digest = _run_snapshot_state(run_dir)
        payload = _allowlisted_payload(
            event_type,
            status=status,
            incoming_snapshot=incoming_snapshot,
        )
        # Status transitions, not detail refreshes: a same-status refresh
        # appends nothing once a status event is already committed, even when
        # error/detail fields changed. Recovery checkpoint events are not
        # status events, so a first mapped-status write still appends its
        # status event after the checkpoint event.
        mapped_event_types = frozenset(STATUS_EVENT_TYPE.values())
        status_events = [e for e in report.events if e.event_type in mapped_event_types]
        if prior_status == status and status_events:
            return status_events[-1]
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
        return _append_owner_event(
            journal_path,
            run_id=run_id,
            event_type=event_type,
            payload=payload,
            idempotency_key=idempotency_key,
        )
    except run_journal.RunJournalError as exc:
        raise _bound_journal_failure(exc) from exc
    except run_events.CanonicalizationError as exc:
        raise _bound_journal_failure(exc) from exc
    except OSError as exc:
        raise _bound_journal_failure(exc) from exc


__all__ = [
    "LifecycleJournalError",
    "STATUS_EVENT_TYPE",
    "approval_idempotency_key",
    "checkpoint_event_pair",
    "is_lifecycle_journaling_enabled",
    "next_dispatch_attempt",
    "normalize_approval_reference",
    "pending_dispatch_requests",
    "prepare_lifecycle_journal",
    "record_dispatch_fact",
    "record_lifecycle_event",
    "record_lifecycle_transition",
]
