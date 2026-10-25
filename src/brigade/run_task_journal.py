"""Bounded reference-only task facts in the existing lifecycle journal.

No task database or execution transport lives here. Task mutations share the
owner's checkpoint/journal lock order and checkpoint-before-fact protocol.
"""

from __future__ import annotations

import hashlib
import json
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping

from brigade import run_checkpoint, run_events, run_journal, run_lifecycle, runguard

TASK_FIELDS = {event_type: run_events.EVENT_TYPES[event_type] for event_type in run_events._TASK_EVENT_TYPES}
STATES = frozenset(
    {
        "queued",
        "running",
        "waiting-for-input",
        "waiting-for-children",
        "unknown",
        "completed",
        "failed",
        "interrupted",
        "cancelled",
    }
)
TERMINAL = frozenset({"completed", "failed", "interrupted", "cancelled"})
CONTAINMENT = frozenset({"unknown", "active", "quiescent"})
ARTIFACT_KINDS = frozenset({"commit", "patch", "receipt", "unavailable"})
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]*", re.ASCII)
_PROVIDER_REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:%~+/@=-]*", re.ASCII)
_PROVIDER_FIELDS = frozenset({"provider_task_id", "original_run_id", "provider_result_id"})
_DIGEST_FIELDS = frozenset({"payload_digest", "spec_digest", "attempt_id", "result_id", "artifact_digest"})


class TaskJournalError(RuntimeError):
    """A bounded task refusal; no transport action has been performed."""


def token(value: object) -> bool:
    return isinstance(value, str) and len(value) <= run_events.MAX_PAYLOAD_STR_LEN and bool(_TOKEN.fullmatch(value))


def task_id(value: object) -> bool:
    return isinstance(value, str) and len(value) <= 128 and token(value)


def provider_reference(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) <= run_events.MAX_PAYLOAD_STR_LEN
        and bool(_PROVIDER_REFERENCE.fullmatch(value))
    )


def digest(value: object) -> bool:
    return isinstance(value, str) and bool(run_events._HEX64.fullmatch(value))


def validate_payload(event_type: str, payload: Mapping[str, Any]) -> None:
    """Validate exact closed task shapes, also used by envelope readers."""
    fields = TASK_FIELDS[event_type]
    if set(payload) != fields:
        raise run_events.CanonicalizationError("task payload fields do not match closed schema")
    optional = (
        {"provider_task_id", "original_run_id", "provider_result_id", "result_id", "artifact_kind", "artifact_digest"}
        if event_type == "task.observed"
        else set()
    )
    for key, value in payload.items():
        if key in optional and value is None:
            continue
        if key in _DIGEST_FIELDS:
            valid = digest(value)
        elif key == "task_id":
            valid = task_id(value)
        elif key in _PROVIDER_FIELDS:
            valid = provider_reference(value)
        elif key == "status":
            valid = isinstance(value, str) and value in STATES
        elif key == "containment":
            valid = isinstance(value, str) and value in CONTAINMENT
        elif key == "artifact_kind":
            valid = isinstance(value, str) and value in ARTIFACT_KINDS
        else:
            valid = token(value)
        if not valid:
            raise run_events.CanonicalizationError("invalid task reference field")
    if event_type == "task.observed":
        terminal = payload["status"] in TERMINAL
        if terminal != (payload["result_id"] is not None):
            raise run_events.CanonicalizationError("task result identity requires terminal status")
        kind, artifact = payload["artifact_kind"], payload["artifact_digest"]
        if terminal:
            if kind is None or (kind == "unavailable") != (artifact is None):
                raise run_events.CanonicalizationError("invalid classified task artifact")
        elif kind is not None or artifact is not None:
            raise run_events.CanonicalizationError("nonterminal task cannot publish an artifact")


def _verified(run_dir: Path) -> list[run_journal.RunEvent]:
    path = run_lifecycle._journal_path(run_dir)
    if not run_journal.bound_is_file(path):
        raise TaskJournalError("task lifecycle journal is not active")
    report = run_journal.read_journal_bounded(path)
    if report.partial_tail is not None:
        raise TaskJournalError("task journal partial tail")
    if report.chain_errors:
        raise TaskJournalError("task journal chain error or unsupported reader")
    events = list(report.events)
    if any(e.run_id != run_dir.name for e in events):
        raise TaskJournalError("task journal run identity mismatch")
    checkpoints = [e for e in events if e.event_type == run_checkpoint.CHECKPOINT_EVENT_TYPE]
    if not checkpoints:
        raise TaskJournalError("task journal has no recovery checkpoint")
    # A later unrelated pair cannot erase a checkpoint-only task crash.
    for index, checkpoint in enumerate(events):
        if checkpoint.event_type != run_checkpoint.CHECKPOINT_EVENT_TYPE:
            continue
        paired_type = checkpoint.payload.get("paired_event_type")
        if paired_type not in TASK_FIELDS:
            continue
        key = checkpoint.payload.get("pairing_key")
        if key is None:
            raise TaskJournalError("task checkpoint refused: pairing")
        following = events[index + 1] if index + 1 < len(events) else None
        if (
            following is None
            or following.sequence != checkpoint.sequence + 1
            or following.event_type != paired_type
            or following.request_digest != key
        ):
            raise TaskJournalError("task checkpoint refused: incomplete-pair")
    latest = checkpoints[-1]
    body = json.loads(run_checkpoint.validate_checkpoint(run_dir, latest))
    run_checkpoint._verify_coverage(events, latest, body)
    if events:
        run_journal.sync_journal_head(
            path, expected_sequence=events[-1].sequence, expected_event_digest=events[-1].event_digest
        )
    return events


@contextmanager
def transaction(
    run_dir: Path, *, workspace: Path | None = None, writing: bool = False
) -> Iterator[tuple[Path, list[run_journal.RunEvent]]]:
    """Verify and fold under the same locks as lifecycle publication."""
    try:
        run_dir = run_journal.normalize_run_dir(run_dir)
        with run_lifecycle.checkpoint_event_pair(), run_journal.journal_mutation(run_lifecycle._journal_path(run_dir)):
            if writing and (workspace is None or not runguard.is_active_run_owner(workspace, run_dir)):
                raise TaskJournalError("task mutation requires active run owner")
            yield run_dir, _verified(run_dir)
    except TaskJournalError:
        raise
    except run_checkpoint.CheckpointError as exc:
        raise TaskJournalError(run_events._bound(f"task checkpoint refused: {exc.category}")) from exc
    except (
        OSError,
        ValueError,
        run_journal.RunJournalError,
        run_lifecycle.LifecycleJournalError,
        run_events.CanonicalizationError,
    ) as exc:
        # Underlying OS/provider details must not enter the public diagnostic.
        raise TaskJournalError("task journal operation refused or durability indeterminate") from exc


def append(run_dir: Path, *, workspace: Path, event_type: str, payload: Mapping[str, Any]) -> run_journal.RunEvent:
    """Publish a checkpoint-paired task fact; no direct journal append shortcut."""
    identity: dict[str, Any] = {"event_type": event_type, "payload": payload}
    if event_type == "task.observed":
        # Bind real state transitions to their prior task fact. A status can
        # recur after a wait; suppressing it for the whole run loses progress.
        events = run_journal.read_journal_bounded(run_lifecycle._journal_path(run_dir)).events
        identity["prior_task_fact"] = next(
            (
                e.event_digest
                for e in reversed(events)
                if e.event_type in TASK_FIELDS and e.payload.get("task_id") == payload["task_id"]
            ),
            None,
        )
    key = "task:" + hashlib.sha256(run_events.canonical_bytes(identity)).hexdigest()
    return run_lifecycle._record_lifecycle_event_paired(
        run_dir, workspace=workspace, event_type=event_type, payload=payload, idempotency_key=key
    )
