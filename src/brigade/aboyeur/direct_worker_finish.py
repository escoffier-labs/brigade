"""Direct-worker finish and terminal failure-status mapping for `brigade run`.

The orchestrator finish path and resume salvage path must agree on how a timed-out,
interrupted, or failed worker/orchestrator result is typed onto run.json. Helpers
here are the single mapping; they must not import ``orchestrator`` at module level.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .. import agents
from .. import localio
from .. import receipt_schema
from .. import seat_health_policy
from ..roster import Roster
from ..run_receipts import (
    agent_result_payload as _agent_result_payload,
    worker_payload as _worker_payload,
)
from ..run_transport import WorkerResult

JsonWriter = Callable[[Path, object], None]
PayloadBuilder = Callable[..., dict[str, object]]


def _final_tree_base(base: Mapping[str, Any]) -> dict[str, Any]:
    """Return terminal receipt fields with the final non-evidence Git tree when available."""
    result = dict(base)
    if result.get("dry_run") is True:
        return result
    cwd = result.get("cwd")
    if not isinstance(cwd, Path):
        return result
    tree_fingerprint = localio.tree_fingerprint(cwd)
    if tree_fingerprint is not None:
        result["tree_fingerprint"] = tree_fingerprint
    return result


def terminal_run_status(final: agents.AgentResult) -> str:
    """Map a failed worker/orchestrator result the way the orchestrator finish path does."""
    if final.timed_out:
        return "timeout"
    if final.status == "interrupted":
        return "canceled"
    return "failed"


def terminal_failure_kind(final: agents.AgentResult) -> str:
    if final.timed_out:
        return "timeout"
    if final.failure_kind:
        return final.failure_kind
    if final.status == "interrupted":
        return "interrupted"
    return "agent-error"


def terminal_failure_phase(final: agents.AgentResult, *, direct_worker: bool) -> str:
    if final.failure_phase:
        return final.failure_phase
    if final.timed_out:
        return "inference"
    return "dispatch" if direct_worker else "synthesis"


def missing_direct_worker_result(*, worker: str, task: str) -> WorkerResult:
    return WorkerResult(
        worker=worker or "",
        task=task,
        text="",
        ok=False,
        detail="direct worker produced no result",
    )


def report_terminal_failure(final: agents.AgentResult, *, direct_worker: bool) -> None:
    if direct_worker:
        print(f"error: worker failed: {final.detail}", file=sys.stderr)
        if final.text:
            print(final.text)
        return
    print(f"error: orchestrator failed during synthesis: {final.detail}", file=sys.stderr)


def synthesis_document_payload(
    *,
    direct_worker: bool,
    worker: str | None,
    roster: Roster,
    final: agents.AgentResult,
    ground_truth: Mapping[str, Any],
    run_id: str,
    worker_results: list[WorkerResult],
    synth_captured: Any | None,
) -> dict[str, object]:
    result = _agent_result_payload(final)
    workers = _worker_payload(worker_results)
    truth = dict(ground_truth)
    if direct_worker:
        payload = receipt_schema.synthesis_document(
            mode="direct-worker",
            worker=worker,
            result=result,
            ground_truth=truth,
            run_id=run_id,
            worker_results=workers,
        )
    else:
        payload = receipt_schema.synthesis_document(
            orchestrator=roster.orchestrator,
            result=result,
            ground_truth=truth,
            run_id=run_id,
            worker_results=workers,
        )
    if synth_captured is not None:
        payload["provenance"] = synth_captured.envelope
    return payload


def write_failed_run_receipt(
    output_dir: Path,
    *,
    write_json: JsonWriter,
    payload: PayloadBuilder,
    final: agents.AgentResult,
    direct_worker: bool,
    worker: str | None,
    roster: Roster,
    base: Mapping[str, Any],
) -> None:
    terminal_base = _final_tree_base(base)
    if direct_worker:
        (output_dir / "final.txt").write_text(final.text + "\n")
    write_json(
        output_dir / "run.json",
        payload(
            **terminal_base,
            status=terminal_run_status(final),
            finished_at=datetime.now(timezone.utc),
            error=final.detail,
            failure_phase=terminal_failure_phase(final, direct_worker=direct_worker),
            failure_kind=terminal_failure_kind(final),
            failure_seat=worker if direct_worker else roster.orchestrator,
            transport_warning=final.transport_warning,
        ),
    )


def write_completed_run_receipt(
    output_dir: Path,
    *,
    write_json: JsonWriter,
    payload: PayloadBuilder,
    final: agents.AgentResult,
    worker_results: list[WorkerResult],
    direct_worker: bool,
    workers_ok: bool,
    defer_artifact_collection: bool,
    pending_handoff: bool,
    transport_warning: dict[str, object] | None,
    base: Mapping[str, Any],
    extra: Mapping[str, Any],
) -> None:
    terminal_base = _final_tree_base(base)
    failed_seats = [result.worker for result in worker_results if not result.ok]
    finished_at: datetime | None
    if not workers_ok:
        run_status = "incomplete"
        finished_at = datetime.now(timezone.utc)
    else:
        final_status = "artifact-collection" if defer_artifact_collection else "ok"
        finished_at = None if defer_artifact_collection or pending_handoff else datetime.now(timezone.utc)
        run_status = "handoff" if pending_handoff else final_status
    (output_dir / "final.txt").write_text(final.text + "\n")
    write_json(
        output_dir / "run.json",
        payload(
            **terminal_base,
            **extra,
            status=run_status,
            finished_at=finished_at,
            error=(
                f"{len(failed_seats)} worker(s) failed or were skipped: {', '.join(failed_seats)}"
                if not workers_ok
                else None
            ),
            failure_phase="workers" if not workers_ok else None,
            failure_kind="worker-failure" if not workers_ok else None,
            failure_seat=",".join(failed_seats) if not workers_ok else None,
            transport_warning=transport_warning,
            worker_failure_summary=(
                seat_health_policy.worker_failure_summary(worker_results) if not workers_ok else None
            ),
        ),
    )


def write_handoff_failure_receipt(
    output_dir: Path,
    *,
    write_json: JsonWriter,
    payload: PayloadBuilder,
    detail: str,
    roster: Roster,
    base: Mapping[str, Any],
    extra: Mapping[str, Any],
) -> None:
    terminal_base = _final_tree_base(base)
    write_json(
        output_dir / "run.json",
        payload(
            **terminal_base,
            **extra,
            status="failed",
            finished_at=datetime.now(timezone.utc),
            error=detail,
            failure_phase="handoff",
            failure_kind="handoff-write-error",
            failure_seat=roster.orchestrator,
        ),
    )


def write_ok_after_handoff_receipt(
    output_dir: Path,
    *,
    write_json: JsonWriter,
    payload: PayloadBuilder,
    handoff: Path,
    defer_artifact_collection: bool,
    base: Mapping[str, Any],
    extra: Mapping[str, Any],
) -> None:
    terminal_base = _final_tree_base(base)
    finished_at = None if defer_artifact_collection else datetime.now(timezone.utc)
    write_json(
        output_dir / "run.json",
        payload(
            **terminal_base,
            **extra,
            status="artifact-collection" if defer_artifact_collection else "ok",
            finished_at=finished_at,
            handoff_path=handoff,
        ),
    )
