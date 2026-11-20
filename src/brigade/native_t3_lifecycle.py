"""Governed native T3 observation, cancellation, acknowledgement and closeout.

These verbs complete the loop a T3 parent session drives around its own
``delegate_task``, ``task_status`` and ``task_cancel`` tools. Brigade never
calls T3. ``observe`` records a supplied status projection only after an
existing correlation matches its full handle digest. ``cancel`` records a
digests-only request and prints the native arguments. ``ack`` consumes one
exact terminal result. ``finalize`` is explicit and never automatic. ``ack``,
``finalize`` and ``status`` are host neutral and live in
``native_child_lifecycle``. This module binds them to the T3 host.

Task text and raw native payloads never enter a report, a refusal, a journal
payload or a command-line argument.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Mapping

from . import native_child_journal as journal_core
from . import native_child_lifecycle as lifecycle_core
from . import native_t3_dispatch as dispatch
from . import native_t3_enrollment as enrollment
from . import run_task_journal as journal
from .native_t3_host import HOST

OBSERVE_SCHEMA = "brigade.t3_observe.v1"
CANCEL_SCHEMA = "brigade.t3_cancel.v1"
ACK_SCHEMA = HOST.ack_schema
FINALIZE_SCHEMA = HOST.finalize_schema
STATUS_SCHEMA = HOST.status_schema
CANCEL_EVENT = HOST.cancel_event
CANCEL_DOMAIN = "brigade.t3.cancel-key.v1"
CANCEL_PREFIX = "brigade-t3-cancel:"
SEAT_DOMAIN = HOST.seat_digest_domain
HOLD_RESOLVED = "hold-resolved"


def _length_prefixed(value: str) -> bytes:
    raw = value.encode("utf-8")
    return len(raw).to_bytes(8, "big") + raw


def cancel_key(run_id: str, task_id: str, attempt_id: str, handle_digest: str) -> str:
    """Derive the domain-separated native cancel request identity."""
    if (
        not journal.token(run_id)
        or not journal.task_id(task_id)
        or not journal.digest(attempt_id)
        or not journal.digest(handle_digest)
    ):
        raise ValueError("invalid cancel key identity")
    material = b"".join(
        _length_prefixed(value) for value in (CANCEL_DOMAIN, run_id, task_id, attempt_id, handle_digest)
    )
    return CANCEL_PREFIX + hashlib.sha256(material).hexdigest()


def _read_meta(workspace: Path, run_dir: Path) -> dict[str, Any]:
    """Read the run receipt without the awaiting-parent restriction."""
    return journal_core.read_meta(HOST, workspace, run_dir)


def ack(*, workspace: Path, run_id: str, task_id: str, result_id: str) -> dict[str, Any]:
    """Acknowledge exactly one retained terminal result, idempotently."""
    return lifecycle_core.ack(
        HOST,
        workspace=workspace,
        run_id=run_id,
        task_id=task_id,
        result_id=result_id,
        owned_run=enrollment.owned_run,
    )


def finalize(*, workspace: Path, run_id: str) -> dict[str, Any]:
    """Close the run explicitly: completed only when every task is acked."""
    return lifecycle_core.finalize(HOST, workspace=workspace, run_id=run_id, owned_run=enrollment.owned_run)


def status(*, workspace: Path, run_id: str) -> dict[str, Any]:
    """Read-only fixed tokens for every task, without native details."""
    return lifecycle_core.status(
        HOST,
        workspace=workspace,
        run_id=run_id,
        correlated=lambda events, task_id: dispatch._correlation(events, task_id) is not None,
    )


def observe(*, workspace: Path, run_id: str, task_id: str, parent_thread: str, payload: object) -> dict[str, Any]:
    """Record one status projection for an already-correlated task."""
    workspace = workspace.expanduser().resolve()
    if not journal.provider_reference(parent_thread):
        raise dispatch._refuse("invalid-identity", "the parent thread identity is not valid")
    run_dir = dispatch._context(workspace, run_id, task_id)
    with enrollment.owned_run(workspace, run_dir):
        dispatch._run_meta(workspace, run_dir)
        events, verified, binding = dispatch._durable_claim(workspace, run_dir, task_id, parent_thread)
        held = dispatch._held(events, task_id)
        if held is not None:
            raise dispatch._refuse("task-held", f"the task is held ({held})")
        prior = dispatch._correlation(events, task_id)
        if prior is None:
            raise dispatch._refuse("no-correlation", "the task has no recorded native correlation")
        result = dispatch.parse_claimed_result(payload, verified=verified, binding=binding)
        dispatch._require_target(result, verified.retained.target)
        handle_digest = dispatch._handle_digest(result, binding.parent_thread)
        if handle_digest != prior.handle_digest or (
            prior.provider_task_id is not None and prior.provider_task_id != result.provider_task_id
        ):
            raise dispatch._refuse("handle-mismatch", "the status handle does not match the recorded correlation")
        observation = dispatch._classify(verified, binding, result)
        if observation.family == "refused":
            raise dispatch._refuse("native-mismatch", "the native projection does not correlate with this task")
        terminal = observation.family == "terminal-candidate"
        evidence = dispatch._evidence_digest(handle_digest, result, observation) if terminal else None
        updated = dispatch._observe(workspace, run_dir, verified, result, observation, evidence=evidence)
        return {
            "schema": OBSERVE_SCHEMA,
            "run_id": run_dir.name,
            "task_id": task_id,
            "attempt_id": binding.attempt_id,
            "family": observation.family,
            "reason": observation.reason,
            "status": updated.status,
            "result_id": updated.result_id if terminal else None,
            "replayed": updated == verified.result,
        }


def cancel(*, workspace: Path, run_id: str, task_id: str, parent_thread: str) -> dict[str, Any]:
    """Record one digests-only cancel request and print the native arguments.

    A cancel request never marks containment or terminal. While the task stays
    nonterminal an identical repeat returns the same arguments with no new
    fact. A terminal or held task refuses before any output.
    """
    workspace = workspace.expanduser().resolve()
    if not journal.provider_reference(parent_thread):
        raise dispatch._refuse("invalid-identity", "the parent thread identity is not valid")
    run_dir = dispatch._context(workspace, run_id, task_id)
    with enrollment.owned_run(workspace, run_dir):
        dispatch._run_meta(workspace, run_dir)
        events, verified, binding = dispatch._durable_claim(workspace, run_dir, task_id, parent_thread)
        held = dispatch._held(events, task_id)
        if held is not None:
            raise dispatch._refuse("task-held", f"the task is held ({held})")
        task = journal_core.find_task(journal_core.read_tasks(run_dir), task_id)
        if task.result_id is not None or task.status in journal.TERMINAL:
            raise dispatch._refuse("task-terminal", "the task is already terminal and cannot be cancelled")
        prior = dispatch._correlation(events, task_id)
        if prior is None:
            raise dispatch._refuse("no-correlation", "the task has no recorded native correlation")
        handle_digest = prior.handle_digest
        # The recorded provider handle is T3's task node id. A legacy digest-only
        # correlation has no provider id to print, so it keeps the old value.
        provider_task_id = prior.provider_task_id or task_id
        client_request_id = cancel_key(run_dir.name, task_id, binding.attempt_id, handle_digest)
        replay = dispatch._latest(events, CANCEL_EVENT, task_id) is not None
        if not replay:
            dispatch._append(
                workspace,
                run_dir,
                CANCEL_EVENT,
                {
                    "task_id": task_id,
                    "attempt_id": binding.attempt_id,
                    "handle_digest": handle_digest,
                    "cancel_key": client_request_id.removeprefix(CANCEL_PREFIX),
                },
                key=dispatch._key("cancel", task_id, binding.attempt_id, client_request_id),
            )
        return {
            "schema": CANCEL_SCHEMA,
            "run_id": run_dir.name,
            "task_id": task_id,
            "attempt_id": binding.attempt_id,
            "handle_digest": handle_digest,
            "task_cancel": {"taskId": provider_task_id, "clientRequestId": client_request_id},
            "replayed": replay,
        }


def _describe(report: Mapping[str, Any]) -> str:
    lines = [f"run {report['run_id']} status={report['status']}"]
    for task in report["tasks"]:
        lines.append(
            "task {task_id} state={state} held={held_reason} correlated={correlated} "
            "terminal={terminal} acked={acked} cancel-requested={cancel_requested} attempt={attempt} "
            "seat={seat_digest}".format(**task)
        )
    return "\n".join(lines)


def observe_cli(args: Any) -> int:
    return dispatch._emit(
        lambda: observe(
            workspace=args.target,
            run_id=args.run_id,
            task_id=args.task_id,
            parent_thread=args.parent_thread,
            payload=dispatch._read_native(args.native_json),
        )
    )


def cancel_cli(args: Any) -> int:
    return dispatch._emit(
        lambda: cancel(
            workspace=args.target,
            run_id=args.run_id,
            task_id=args.task_id,
            parent_thread=args.parent_thread,
        )
    )


def ack_cli(args: Any) -> int:
    return dispatch._emit(
        lambda: ack(workspace=args.target, run_id=args.run_id, task_id=args.task_id, result_id=args.result_id)
    )


def finalize_cli(args: Any) -> int:
    return dispatch._emit(lambda: finalize(workspace=args.target, run_id=args.run_id))


def status_cli(args: Any) -> int:
    try:
        report = status(workspace=args.target, run_id=args.run_id)
    except dispatch.DispatchError as exc:
        print(f"error [{exc.code}]: {dispatch._bound(str(exc))}", file=sys.stderr)
        return 2
    except enrollment.EnrollmentError as exc:
        print(f"error [{exc.code}]: {dispatch._bound(str(exc))}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, sort_keys=True))
    else:
        print(_describe(report))
    return 0
