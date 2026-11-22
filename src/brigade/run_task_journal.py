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
from typing import Any, Iterator, Mapping, Sequence

from brigade import run_checkpoint, run_events, run_journal, run_lifecycle, runguard

BATCH_DESCRIPTOR_VERSION = 1
MAX_BATCH_MEMBERS = 8
NATIVE_ENROLLMENT_DESCRIPTOR_VERSION = 1

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
# ``not-launched`` names the containment of a cancellation that stopped a task
# before any worker existed, so it is never confused with a quiescent worker.
CONTAINMENT = frozenset({"unknown", "active", "quiescent", "not-launched"})
# Closed cancel-intent reasons: who asked for the cancellation, never free text.
CANCEL_REASONS = frozenset({"operator", "parent", "budget"})
ARTIFACT_KINDS = frozenset({"commit", "patch", "receipt", "unavailable"})
# Closed disposal vocabulary: the retained-copy proof that justified removing a
# task worktree, and whether its task branch survived. Values are tokens, never
# booleans, because the canonical payload contract refuses booleans.
DISPOSAL_MODES = frozenset({"integrated", "retained", "no-changes", "discarded"})
BRANCH_STATES = frozenset({"kept", "deleted"})
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]*", re.ASCII)
_PROVIDER_REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:%~+/@=-]*", re.ASCII)
# A pinned base revision is already public; it is the only non-digest string a
# workspace binding carries.
_BASE_SHA = re.compile(r"[0-9a-f]{40}", re.ASCII)
_WORKSPACE_DIGEST_FIELDS = ("branch_digest", "path_digest", "dev_ino_digest")
# An integration identity is the first 16 hex digits of a SHA-256 over the
# ordered selection, so it is a bounded, already-public string.
_INTEGRATION_ID = re.compile(r"[0-9a-f]{16}", re.ASCII)
# Run-local delegation input assets are flat names derived from the task ID.
# The name is reloadable and never carries task text.
_NATIVE_ASSET_PATH = re.compile(r"native-input-[0-9a-f]{64}\.json", re.ASCII)
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


def base_sha(value: object) -> bool:
    return isinstance(value, str) and bool(_BASE_SHA.fullmatch(value))


def integration_id(value: object) -> bool:
    return isinstance(value, str) and bool(_INTEGRATION_ID.fullmatch(value))


def native_asset_path(value: object) -> bool:
    return isinstance(value, str) and bool(_NATIVE_ASSET_PATH.fullmatch(value))


def batch_membership_digest(*, descriptor_version: int, members: Sequence[Mapping[str, Any]]) -> str:
    return hashlib.sha256(
        run_events.canonical_bytes(
            {
                "domain": "brigade.task-batch.members.v1",
                "descriptor_version": descriptor_version,
                "members": list(members),
            }
        )
    ).hexdigest()


def batch_child_request_id(*, run_id: str, request_id: str, task_id: str) -> str:
    return hashlib.sha256(
        run_events.canonical_bytes(
            {"domain": "brigade.task-batch.child.v1", "run_id": run_id, "request_id": request_id, "task_id": task_id}
        )
    ).hexdigest()


def batch_reference(*, run_id: str, request_id: str, membership_digest: str) -> str:
    return hashlib.sha256(
        run_events.canonical_bytes(
            {
                "domain": "brigade.task-batch.ref.v1",
                "run_id": run_id,
                "request_id": request_id,
                "membership_digest": membership_digest,
            }
        )
    ).hexdigest()


def _validate_batch_payload(payload: Mapping[str, Any]) -> None:
    version, members = payload["descriptor_version"], payload["members"]
    if type(version) is not int or version != BATCH_DESCRIPTOR_VERSION:
        raise run_events.CanonicalizationError("unsupported task batch descriptor version")
    if not token(payload["request_id"]) or not digest(payload["membership_digest"]):
        raise run_events.CanonicalizationError("invalid task batch identity")
    if not isinstance(members, list) or not 1 <= len(members) <= MAX_BATCH_MEMBERS:
        raise run_events.CanonicalizationError("task batch requires 1 through 8 members")
    seen: set[str] = set()
    for row in members:
        if not isinstance(row, Mapping) or set(row) != {"task_id", "seat", "payload_digest"}:
            raise run_events.CanonicalizationError("task batch member fields do not match closed schema")
        if not task_id(row["task_id"]) or not token(row["seat"]) or not digest(row["payload_digest"]):
            raise run_events.CanonicalizationError("invalid task batch member")
        if row["task_id"] in seen:
            raise run_events.CanonicalizationError("duplicate task batch member")
        seen.add(row["task_id"])
    if payload["membership_digest"] != batch_membership_digest(descriptor_version=version, members=members):
        raise run_events.CanonicalizationError("task batch membership digest mismatch")


def _validate_native_enrollment_payload(payload: Mapping[str, Any]) -> None:
    """Closed enrollment shape: references and digests only, no payload text."""
    version = payload["descriptor_version"]
    if type(version) is not int or version != NATIVE_ENROLLMENT_DESCRIPTOR_VERSION:
        raise run_events.CanonicalizationError("unsupported native enrollment descriptor version")
    if (
        not task_id(payload["task_id"])
        or not native_asset_path(payload["asset_path"])
        or not digest(payload["asset_digest"])
        or not digest(payload["template_digest"])
    ):
        raise run_events.CanonicalizationError("invalid native enrollment reference")


def _validate_workspace_binding_payload(payload: Mapping[str, Any]) -> None:
    """Closed binding shape: one public base revision and three digests."""
    if not task_id(payload["task_id"]):
        raise run_events.CanonicalizationError("invalid task workspace binding identity")
    if not base_sha(payload["base_sha"]):
        raise run_events.CanonicalizationError("invalid task workspace binding base revision")
    if any(not digest(payload[field]) for field in _WORKSPACE_DIGEST_FIELDS):
        raise run_events.CanonicalizationError("invalid task workspace binding digest")


def _validate_integration_payload(payload: Mapping[str, Any]) -> None:
    """Closed integration shape: one identity and the two commit revisions."""
    if not task_id(payload["task_id"]):
        raise run_events.CanonicalizationError("invalid task integration identity")
    if not integration_id(payload["integration_id"]):
        raise run_events.CanonicalizationError("invalid task integration reference")
    if not base_sha(payload["candidate_commit"]) or not base_sha(payload["integration_commit"]):
        raise run_events.CanonicalizationError("invalid task integration revision")


def _validate_disposal_payload(payload: Mapping[str, Any]) -> None:
    """Closed disposal shape: one identity, one mode and one branch verdict."""
    if not task_id(payload["task_id"]):
        raise run_events.CanonicalizationError("invalid task disposal identity")
    if payload["mode"] not in DISPOSAL_MODES:
        raise run_events.CanonicalizationError("invalid task disposal mode")
    if payload["branch_kept"] not in BRANCH_STATES:
        raise run_events.CanonicalizationError("invalid task disposal branch state")


def _validate_cancel_requested_payload(payload: Mapping[str, Any]) -> None:
    """Closed cancel-intent shape: one identity and one bounded reason token."""
    if not task_id(payload["task_id"]):
        raise run_events.CanonicalizationError("invalid task cancel-request identity")
    if payload["reason"] not in CANCEL_REASONS:
        raise run_events.CanonicalizationError("invalid task cancel-request reason")


def _validate_collected_payload(payload: Mapping[str, Any]) -> None:
    """Closed recovered-collection shape: one sidecar digest and its delivery.

    The candidate revision and the delivery are the same closed values the
    artifact sidecar already holds, so this fact can stand in for the
    observation when a collection finished after the worker's receipt.
    """
    from brigade import run_batch_artifacts

    if not task_id(payload["task_id"]):
        raise run_events.CanonicalizationError("invalid task artifact collection identity")
    if not digest(payload["attempt_id"]) or not digest(payload["artifact_digest"]):
        raise run_events.CanonicalizationError("invalid task artifact collection reference")
    if payload["candidate_commit"] is not None and not base_sha(payload["candidate_commit"]):
        raise run_events.CanonicalizationError("invalid task artifact collection revision")
    if payload["delivery"] not in run_batch_artifacts.DELIVERY_STATES:
        raise run_events.CanonicalizationError("invalid task artifact collection delivery")


def validate_payload(event_type: str, payload: Mapping[str, Any]) -> None:
    """Validate exact closed task shapes, also used by envelope readers."""
    fields = TASK_FIELDS[event_type]
    if set(payload) != fields:
        raise run_events.CanonicalizationError("task payload fields do not match closed schema")
    if event_type == "task.batch-accepted":
        _validate_batch_payload(payload)
        return
    if event_type == "task.native-enrolled":
        _validate_native_enrollment_payload(payload)
        return
    if event_type == "task.workspace-bound":
        _validate_workspace_binding_payload(payload)
        return
    if event_type == "task.integrated":
        _validate_integration_payload(payload)
        return
    if event_type == "task.disposed":
        _validate_disposal_payload(payload)
        return
    if event_type == "task.cancel-requested":
        _validate_cancel_requested_payload(payload)
        return
    if event_type == "task.artifact-collected":
        _validate_collected_payload(payload)
        return
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
        elif key == "dispatch_attempt":
            valid = type(value) is int and value > 0
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


def validate_pairs(events: Sequence[run_journal.RunEvent]) -> None:
    """Require both halves of every task checkpoint/fact pair in history."""
    for index, event in enumerate(events):
        if event.event_type in TASK_FIELDS:
            preceding = events[index - 1] if index else None
            if (
                preceding is None
                or preceding.event_type != run_checkpoint.CHECKPOINT_EVENT_TYPE
                or preceding.run_id != event.run_id
                or preceding.sequence + 1 != event.sequence
                or preceding.payload.get("paired_event_type") != event.event_type
                or preceding.payload.get("pairing_key") != event.request_digest
            ):
                raise TaskJournalError("task checkpoint refused: incomplete-pair")
        if event.event_type != run_checkpoint.CHECKPOINT_EVENT_TYPE:
            continue
        paired_type = event.payload.get("paired_event_type")
        if paired_type not in TASK_FIELDS:
            continue
        key = event.payload.get("pairing_key")
        if key is None:
            raise TaskJournalError("task checkpoint refused: pairing")
        following = events[index + 1] if index + 1 < len(events) else None
        if (
            following is None
            or following.run_id != event.run_id
            or following.sequence != event.sequence + 1
            or following.event_type != paired_type
            or following.request_digest != key
        ):
            raise TaskJournalError("task checkpoint refused: incomplete-pair")


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
    validate_pairs(events)
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
