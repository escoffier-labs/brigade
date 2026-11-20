"""The governed launch claim for one native child task.

A claim reserves one dispatch unit, prepares the attempt, binds the finalized
intent by digest, consumes the launch permission and records the durable
dispatch request. Only a granted claim hands its host adapter the claimed
attempt, and only after every mutation committed. A claim that committed
without its durable request is a run-wide barrier that
``native_child_journal.barrier`` enforces on every later claim.

The sequence, and the order of its two locks, is host neutral. The adapter
supplies each step through ``ClaimOps`` and decides what a granted claim prints.
Adapters pass the step callables at call time, so a replaced step takes effect.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import run_budget, run_journal, run_lifecycle, run_tasks
from . import native_child_journal as journal_core
from .native_child_admission import UNAVAILABLE, Admission, HubAuthority
from .native_child_capacity import CapacityDecision, Occupancy
from .native_child_host import NativeChildCapacityError, NativeChildCustodyError, NativeChildHost
from .native_child_journal import refuse
from . import roster as roster_mod


@dataclass(frozen=True)
class ClaimOps:
    """The adapter-supplied steps of one claim, in the order the sequence uses them."""

    capacity_lock: Callable[[Path], AbstractContextManager[Any]]
    governed_occupancy: Callable[[Path], Occupancy]
    assess: Callable[[roster_mod.Roster, str, Occupancy], CapacityDecision]
    owned_run: Callable[[Path, Path], AbstractContextManager[Any]]
    load_roster: Callable[[Path], roster_mod.Roster]
    run_meta: Callable[[Path, Path], Mapping[str, Any]]
    read_events: Callable[[Path], Sequence[run_journal.RunEvent]]
    # Verified tasks keyed by task id. A verified task exposes ``result`` (the
    # task journal result) and ``target`` (the retained target the host admits).
    verify: Callable[[Path, Path], Mapping[str, Any]]
    evaluate: Callable[[roster_mod.Roster, str, Any, HubAuthority], Admission]
    batch_ref: Callable[[Sequence[run_journal.RunEvent], str, str], str]
    coordinator: Callable[[Path, Path, Mapping[str, Any]], run_budget.BudgetCoordinator]
    # Record the claim for one candidate attempt and return that attempt.
    record_claim: Callable[[Any, int, str, Admission, dict[str, Any]], int | None]


@dataclass(frozen=True)
class ClaimGrant:
    """A committed claim: the dispatch attempt and whatever the adapter recorded."""

    attempt: int
    recorded: dict[str, Any]


def record_claim(
    host: NativeChildHost,
    *,
    workspace: Path,
    run_dir: Path,
    task_id: str,
    attempt: int,
    build_intent: Callable[[run_tasks.TaskHandle], tuple[Any, str]],
    claim_payload: Callable[[str], Mapping[str, Any]],
    append: Callable[..., Any],
    record_requested: Callable[..., int | None],
    out: dict[str, Any],
) -> int | None:
    """Prepare, bind, claim, then record the requested fact, in that order.

    ``build_intent`` returns the adapter's finalized intent and its digest.
    ``claim_payload`` receives that digest and returns the adapter-specific part
    of the claim binding fact.
    """
    try:
        handle = run_tasks.prepare_attempt(run_dir, workspace=workspace, task_id=task_id, native_governed=True)
    except run_tasks.TaskJournalError as exc:
        raise refuse("claim-refused", f"the attempt was not prepared ({exc})") from None
    if handle.attempt_id is None:
        raise refuse("claim-refused", "the prepared attempt has no identity")
    intent, intent_digest = build_intent(handle)
    append(
        workspace,
        run_dir,
        journal_core.CLAIM_EVENT,
        {
            "task_id": task_id,
            "attempt_id": handle.attempt_id,
            "dispatch_attempt": attempt,
            **claim_payload(intent_digest),
        },
        key=journal_core.idempotency_key(host, "claimed", task_id, handle.attempt_id),
    )
    try:
        decision = run_tasks.claim_task_launch(
            run_dir,
            workspace=workspace,
            handle=handle,
            expected_version=journal_core.task_version(run_dir, task_id),
            seat=handle.spec.seat,
            dispatch_attempt=attempt,
            native_governed=True,
        )
    except run_tasks.TaskJournalError as exc:
        raise refuse("claim-refused", f"the launch claim was refused ({exc})") from None
    if decision.outcome != "granted":
        raise refuse("claim-refused", f"the launch claim was {decision.outcome}")
    out.update({"handle": handle, "intent": intent})
    return record_requested(run_dir, workspace, handle.spec.seat, attempt)


def run_claim(
    host: NativeChildHost,
    ops: ClaimOps,
    *,
    workspace: Path,
    run_dir: Path,
    task_id: str,
    hub: HubAuthority,
) -> ClaimGrant:
    """Reserve, prepare, bind, claim and request exactly once for one task.

    The Hub read is network I/O, so the caller reads it before this sequence and
    it stays outside the capacity lock. The roster is re-read inside the locks
    because an edit while waiting must not be evaluated stale.
    """
    try:
        with ops.capacity_lock(workspace):
            # Occupancy is read and trusted before the run lock. The capacity
            # lock serializes every claim that can add a unit, so this snapshot
            # stays valid through the existing claim path. Other verbs can only
            # free a unit, which keeps this snapshot conservative.
            occupancy = ops.governed_occupancy(workspace)
            if not occupancy.authoritative:
                raise refuse("capacity-unknown", "live governed native capacity cannot be verified")
            with ops.owned_run(workspace, run_dir):
                try:
                    roster = ops.load_roster(workspace)
                except NativeChildCustodyError as exc:
                    raise refuse(exc.code, str(exc)) from None
                meta = ops.run_meta(workspace, run_dir)
                events = ops.read_events(run_dir)
                journal_core.barrier(events)
                verified = ops.verify(workspace, run_dir)
                current = verified.get(task_id)
                if current is None:
                    raise refuse("not-enrolled", "the task has no native enrollment record")
                held = journal_core.held(events, task_id)
                if held is not None:
                    raise refuse("task-held", f"the task is held ({held})")
                if current.result.launch_claim is not None:
                    raise refuse("already-claimed", "the task already consumed its launch claim")
                seat = current.result.handle.spec.seat
                decision = ops.assess(roster, seat, occupancy)
                if not decision.granted:
                    raise refuse("capacity-denied", "no governed native capacity remains for this claim")
                evidence = ops.evaluate(roster, seat, current.target, hub)
                if evidence.reason in host.structural_gaps:
                    # A target the host structurally cannot run is decided by
                    # the closed classifier, never by message text. Every
                    # policy, capacity or authority refusal keeps its own code.
                    raise refuse(host.unsupported_code, f"seat admission refused this claim ({evidence.reason})")
                if evidence.code == UNAVAILABLE:
                    raise refuse(
                        "admission-unavailable", f"seat admission authority is unavailable ({evidence.reason})"
                    )
                if not evidence.admitted:
                    raise refuse("admission-denied", f"seat admission refused this claim ({evidence.reason})")
                batch_ref = ops.batch_ref(events, run_dir.name, task_id)
                budget = ops.coordinator(workspace, run_dir, meta)
                recorded: dict[str, Any] = {}
                try:
                    attempt = budget.reserve_and_record_dispatch(
                        seat=seat,
                        allocate_attempt=lambda: run_lifecycle.allocate_next_dispatch_attempt(run_dir, seat),
                        record_requested=lambda candidate: ops.record_claim(
                            current, candidate, batch_ref, evidence, recorded
                        ),
                        reclaim_pending=False,
                    )
                except run_budget.BudgetPolicyError as exc:
                    code = "capacity-denied" if not exc.exhausted else "budget-exhausted"
                    raise refuse(code, f"the run budget refused a dispatch unit ({exc.dimension})") from None
                except run_budget.BudgetError as exc:
                    raise refuse("budget-refused", exc.diagnostic) from None
                except (run_lifecycle.LifecycleJournalError, run_journal.RunJournalError, OSError):
                    raise refuse("journal-refused", "the dispatch transaction was refused") from None
                if type(attempt) is not int or "intent" not in recorded:
                    raise refuse("claim-refused", "the dispatch transaction did not complete")
                return ClaimGrant(attempt, recorded)
    except NativeChildCapacityError as exc:
        raise refuse(exc.code, str(exc)) from None
