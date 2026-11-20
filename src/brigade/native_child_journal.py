"""Run journal access for governed native children.

Host-neutral reads and writes of the durable facts a governed native run keeps:
the run receipt, the lifecycle journal, the launch claim and its dispatch
request, the run-wide barrier, task holds and the per-invocation budget
coordinator. Task text and raw native payloads never enter a refusal, a journal
payload or an exception message.

A host adapter supplies the persisted spellings that name the host through
``NativeChildHost``. Every other persisted string here, including the
``task.native-*`` event names, is shared by all hosts.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import run_budget, run_events, run_journal, run_lifecycle, run_tasks, runguard
from . import run_task_journal as journal
from .native_child_host import NativeChildError, NativeChildHost

CLAIM_EVENT = "task.native-claimed"
HELD_EVENT = "task.native-held"
HOLD_RESOLVED_EVENT = "task.native-hold-resolved"
ABANDONED = "abandoned-unknown"
ACCEPT_CONFLICT = "accept-conflict"
HELD_RUN_STATUS = "held"
AWAITING_PARENT = runguard.EXTERNALLY_CONTROLLED_STATUS
MAX_DIAGNOSTIC = 240


def bound(message: str) -> str:
    text = " ".join(str(message).split())
    return text if len(text) <= MAX_DIAGNOSTIC else text[: MAX_DIAGNOSTIC - 1] + "."


def refuse(code: str, message: str) -> NativeChildError:
    return NativeChildError(code, bound(message))


def digest(material: Mapping[str, Any], domain: str) -> str:
    encoded = json.dumps(
        {"domain": domain, **material}, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def idempotency_key(host: NativeChildHost, kind: str, *identity: object) -> str:
    """Derive a fixed-length idempotency key from an identity tuple.

    Task ids reach 128 characters and may not be embedded beside a 64-hex
    digest inside the 128-character key bound. The digest of the whole
    identity keeps the key both bounded and collision-safe.
    """
    key = hashlib.sha256(
        run_events.canonical_bytes({"domain": host.idempotency_domain, "kind": kind, "identity": list(identity)})
    ).hexdigest()
    return f"native-{kind}:{key}"


@dataclass(frozen=True)
class ClaimBinding:
    """The durable claim intent for one task attempt, without task text."""

    task_id: str
    attempt_id: str
    dispatch_attempt: int
    parent_thread: str
    template_digest: str
    intent_digest: str

    def __post_init__(self) -> None:
        if (
            not journal.task_id(self.task_id)
            or not journal.digest(self.attempt_id)
            or type(self.dispatch_attempt) is not int
            or self.dispatch_attempt < 1
            or not journal.provider_reference(self.parent_thread)
            or not journal.digest(self.template_digest)
            or not journal.digest(self.intent_digest)
        ):
            raise ValueError("invalid native claim binding")


def run_dir(workspace: Path, run_id: str) -> Path:
    if not journal.token(run_id):
        raise refuse("invalid-run", "the run identity is not valid")
    try:
        runs = runguard.lock_path(workspace).parent / "runs"
    except (OSError, ValueError):
        raise refuse("run-missing", "no run matches that identity") from None
    path = runs / run_id
    if not path.is_dir():
        raise refuse("run-missing", "no run matches that identity")
    return path


def context(workspace: Path, run_id: str, task_id: str) -> Path:
    """Resolve the run directory. Run state is re-read inside the run lock."""
    if not journal.task_id(task_id):
        raise refuse("invalid-identity", "the task identity is not valid")
    return run_dir(workspace, run_id)


def run_meta(host: NativeChildHost, workspace: Path, directory: Path) -> dict[str, Any]:
    """Read the run receipt for a transition, refusing a held or finished run."""
    try:
        meta = json.loads(run_journal.bound_read_bytes(directory / "run.json"))
    except FileNotFoundError:
        raise refuse("run-missing", "no run matches that identity") from None
    except (OSError, ValueError, UnicodeDecodeError):
        raise refuse("run-state", "the run receipt is not readable") from None
    if not isinstance(meta, dict) or meta.get("kind") != host.run_kind:
        raise refuse("not-native-run", f"the run is not a {host.run_noun}")
    if meta.get("lock_workspace") != str(workspace):
        raise refuse("run-state", "the run belongs to a different workspace")
    status = meta.get("status")
    if status == HELD_RUN_STATUS:
        raise refuse("run-held", "the run is explicitly held and accepts no native transition")
    if status != AWAITING_PARENT:
        raise refuse("run-state", "the run state does not permit this native transition")
    return meta


def read_meta(host: NativeChildHost, workspace: Path, directory: Path) -> dict[str, Any]:
    """Read the run receipt without the awaiting-parent restriction.

    ``status`` and an idempotent ``ack`` stay readable after finalization, so
    they never route through the strict transition gate in ``run_meta``.
    """
    try:
        meta = json.loads(run_journal.bound_read_bytes(directory / "run.json"))
    except FileNotFoundError:
        raise refuse("run-missing", "no run matches that identity") from None
    except (OSError, ValueError, UnicodeDecodeError, run_journal.RunJournalError):
        raise refuse("run-state", "the run receipt is not readable") from None
    if not isinstance(meta, dict) or meta.get("kind") != host.run_kind:
        raise refuse("not-native-run", f"the run is not a {host.run_noun}")
    if meta.get("lock_workspace") != str(workspace):
        raise refuse("run-state", "the run belongs to a different workspace")
    return meta


def read_events(directory: Path) -> list[run_journal.RunEvent]:
    try:
        with journal.transaction(directory) as (_dir, events):
            return list(events)
    except journal.TaskJournalError as exc:
        raise refuse("journal-refused", f"the task journal refused the read ({exc})") from None


def read_tasks(directory: Path) -> tuple[run_tasks.TaskResult, ...]:
    try:
        return run_tasks.read_tasks(directory)
    except run_tasks.TaskJournalError as exc:
        raise refuse("journal-refused", f"the task journal refused the read ({exc})") from None


def find_task(results: tuple[run_tasks.TaskResult, ...], task_id: str) -> run_tasks.TaskResult:
    for result in results:
        if result.handle.spec.task_id == task_id:
            return result
    raise refuse("not-enrolled", "the task has no native enrollment record")


def latest(events: Sequence[run_journal.RunEvent], event_type: str, task_id: str) -> Mapping[str, Any] | None:
    for event in reversed(events):
        if event.event_type == event_type and event.payload.get("task_id") == task_id:
            return event.payload
    return None


def claim_binding(events: Sequence[run_journal.RunEvent], task_id: str) -> ClaimBinding | None:
    payload = latest(events, CLAIM_EVENT, task_id)
    if payload is None:
        return None
    try:
        return ClaimBinding(
            task_id=payload["task_id"],
            attempt_id=payload["attempt_id"],
            dispatch_attempt=payload["dispatch_attempt"],
            parent_thread=payload["parent_thread"],
            template_digest=payload["template_digest"],
            intent_digest=payload["intent_digest"],
        )
    except (KeyError, ValueError):
        raise refuse("journal-refused", "the claim binding is not usable") from None


def held(events: Sequence[run_journal.RunEvent], task_id: str) -> str | None:
    """Return the active hold reason, folding durable resolutions in order.

    A resolved accept-conflict hold stays resolved. An abandoned attempt has no
    resolution event and stays held forever.
    """
    for event in reversed(events):
        if event.event_type not in {HELD_EVENT, HOLD_RESOLVED_EVENT}:
            continue
        payload = event.payload
        if payload.get("task_id") != task_id:
            continue
        reason = payload.get("reason")
        if reason not in {ACCEPT_CONFLICT, ABANDONED}:
            raise refuse("journal-refused", "the task hold record is not usable")
        if event.event_type == HOLD_RESOLVED_EVENT:
            if reason != ACCEPT_CONFLICT:
                raise refuse("journal-refused", "the task hold resolution is not usable")
            return None
        return str(reason)
    return None


def requested_attempts(events: Sequence[run_journal.RunEvent]) -> set[tuple[Any, Any]]:
    """Every (seat, attempt) whose dispatch request is durable."""
    return {
        (event.payload["seat"], event.payload["attempt"])
        for event in events
        if event.event_type == "run.dispatch.requested"
    }


def barrier(events: Sequence[run_journal.RunEvent]) -> None:
    """Refuse all new claims while any durable claim is unresolved.

    Two states block the run. A launch claim without its dispatch request was
    authorized but never recorded. A claim binding without a launch claim died
    between the two facts and never consumed launch permission. Neither state
    may let a sibling reuse the dispatch identity.
    """
    requested = requested_attempts(events)
    launched = {
        (event.payload["task_id"], event.payload["attempt_id"])
        for event in events
        if event.event_type == "task.launch-claimed"
    }
    abandoned = {
        event.payload["task_id"]
        for event in events
        if event.event_type == HELD_EVENT and event.payload.get("reason") == ABANDONED
    }
    for event in events:
        if event.event_type == "task.launch-claimed":
            if (event.payload["seat"], event.payload["dispatch_attempt"]) not in requested:
                raise refuse(
                    "run-barrier",
                    "a durable launch claim has no matching dispatch request, so the run holds every new claim",
                )
        elif event.event_type == CLAIM_EVENT:
            identity = (event.payload["task_id"], event.payload["attempt_id"])
            if identity not in launched and identity[0] not in abandoned:
                raise refuse(
                    "run-barrier",
                    "a retained claim binding has no launch claim, so the run holds every new claim",
                )


def batch_ref(events: Sequence[run_journal.RunEvent], run_id: str, task_id: str) -> str:
    for event in events:
        if event.event_type == "task.batch-accepted" and any(
            row.get("task_id") == task_id for row in event.payload["members"]
        ):
            return journal.batch_reference(
                run_id=run_id,
                request_id=event.payload["request_id"],
                membership_digest=event.payload["membership_digest"],
            )
    raise refuse("not-batch", "the task has no retained batch binding")


def append_fact(
    workspace: Path, directory: Path, event_type: str, payload: Mapping[str, Any], *, key: str
) -> run_journal.RunEvent:
    try:
        return run_lifecycle.record_lifecycle_event(
            directory, event_type=event_type, payload=payload, idempotency_key=key, workspace=workspace
        )
    except (run_lifecycle.LifecycleJournalError, run_journal.RunJournalError, OSError, ValueError):
        raise refuse("journal-refused", "the run journal refused the native fact") from None


def record_held(
    host: NativeChildHost,
    workspace: Path,
    directory: Path,
    task_id: str,
    attempt_id: str,
    reason: str,
    evidence: str,
    *,
    append: Callable[..., Any] = append_fact,
) -> None:
    if not journal.digest(evidence):
        raise refuse("journal-refused", "the hold evidence is not usable")
    append(
        workspace,
        directory,
        HELD_EVENT,
        {"task_id": task_id, "attempt_id": attempt_id, "reason": reason, "evidence_digest": evidence},
        key=idempotency_key(host, "held", task_id, attempt_id, reason, evidence),
    )


def record_dispatch_requested(directory: Path, workspace: Path, seat: str, attempt: int) -> int:
    """The durable requested fact after the launch claim. A crash here holds the run."""
    try:
        event = run_lifecycle.record_dispatch_fact(
            directory, workspace=workspace, event_type="run.dispatch.requested", seat=seat, attempt=attempt
        )
    except (run_lifecycle.LifecycleJournalError, run_journal.RunJournalError, OSError):
        raise refuse("journal-refused", "the dispatch request was not recorded") from None
    if event is None or event.payload.get("attempt") != attempt:
        raise refuse("dispatch-binding", "the dispatch request did not keep the claimed attempt")
    return attempt


def close_lost_request(
    workspace: Path,
    directory: Path,
    events: Sequence[run_journal.RunEvent],
    result: run_tasks.TaskResult,
    *,
    record_requested: Callable[..., int] = record_dispatch_requested,
) -> None:
    """Record a missing dispatch request so an abandoned lost reply frees the barrier.

    The claim already consumed launch permission, so the unit stays charged and
    the task stays held and non-relaunchable.
    """
    claim = result.launch_claim
    if claim is None:
        return
    if (claim.seat, claim.dispatch_attempt) not in requested_attempts(events):
        record_requested(directory, workspace, claim.seat, claim.dispatch_attempt)


def coordinator(workspace: Path, directory: Path, meta: Mapping[str, Any]) -> run_budget.BudgetCoordinator:
    """Build one per-invocation coordinator from the durable run state."""
    try:
        declaration = run_budget.parse_declaration(meta.get("run_budget"))
    except run_budget.BudgetCompatibilityError as exc:
        raise refuse("budget-incompatible", exc.diagnostic) from None
    budget = run_budget.BudgetCoordinator(
        declaration=declaration,
        append_event=lambda event_type, payload, key: run_lifecycle.record_lifecycle_event(
            directory, event_type=event_type, payload=dict(payload), idempotency_key=key, workspace=workspace
        ),
    )
    started = meta.get("started_at")
    try:
        budget.set_started_at(
            datetime.fromisoformat(started.replace("Z", "+00:00")) if isinstance(started, str) else None
        )
    except ValueError:
        budget.set_started_at(None)
    try:
        report = run_journal.read_journal_bounded(run_lifecycle._journal_path(directory))
    except (OSError, run_journal.RunJournalError):
        raise refuse("journal-refused", "the run journal is not readable") from None
    if report.partial_tail is not None or report.chain_errors:
        raise refuse("journal-refused", "the run journal chain is not usable")
    budget.reload(report.events)
    return budget


def task_version(directory: Path, task_id: str) -> str:
    try:
        version = next(item.version for item in run_tasks.read_tasks(directory) if item.handle.spec.task_id == task_id)
    except StopIteration:
        raise refuse("not-enrolled", "the task has no native enrollment record") from None
    except run_tasks.TaskJournalError as exc:
        raise refuse("journal-refused", f"the task journal refused the read ({exc})") from None
    if version is None:
        raise refuse("journal-refused", "the task has no durable version")
    return version
