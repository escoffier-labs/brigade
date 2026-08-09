"""Schema version constants and SIEM-oriented receipt serialization helpers."""

from __future__ import annotations

VERIFY_RECEIPT_SCHEMA_VERSION = 2
WORK_CLOSEOUT_SCHEMA_VERSION = 1
RUN_RECEIPT_SCHEMA_VERSION = 1
OUTCOME_RECORD_SCHEMA_VERSION = 1
OUTCOME_DECISION_SCHEMA_VERSION = 1

RUN_RECEIPT_SCHEMA = "brigade.run.v1"

ROSTER_SNAPSHOT_SCHEMA = "brigade.roster_snapshot.v1"
ROSTER_SNAPSHOT_SCHEMA_VERSION = 1
RUN_PLAN_SCHEMA = "brigade.run_plan.v1"
RUN_PLAN_SCHEMA_VERSION = 1
WORKER_RESULTS_SCHEMA = "brigade.worker_results.v1"
WORKER_RESULTS_SCHEMA_VERSION = 1
SYNTHESIS_SCHEMA = "brigade.synthesis.v1"
SYNTHESIS_SCHEMA_VERSION = 1

RUN_EVENT_SCHEMA = "brigade.run_event.v1"
RUN_EVENT_SCHEMA_VERSION = 1

WORK_RUN_ARCHIVE_SCHEMA = "brigade.work-run"
WORK_RUN_ARCHIVE_SCHEMA_VERSION = 1


def stamp_run_receipt(payload: dict[str, object]) -> dict[str, object]:
    payload.setdefault("schema", RUN_RECEIPT_SCHEMA)
    payload.setdefault("schema_version", RUN_RECEIPT_SCHEMA_VERSION)
    return payload


def stamp_worker_results_document(payload: dict[str, object]) -> dict[str, object]:
    payload.setdefault("schema", WORKER_RESULTS_SCHEMA)
    payload.setdefault("schema_version", WORKER_RESULTS_SCHEMA_VERSION)
    return payload


def stamp_synthesis_document(payload: dict[str, object]) -> dict[str, object]:
    payload.setdefault("schema", SYNTHESIS_SCHEMA)
    payload.setdefault("schema_version", SYNTHESIS_SCHEMA_VERSION)
    return payload


def run_plan_document(assignments: list[dict[str, object]]) -> dict[str, object]:
    return {
        "schema": RUN_PLAN_SCHEMA,
        "schema_version": RUN_PLAN_SCHEMA_VERSION,
        "assignments": assignments,
    }


def worker_results_document(
    results: list[dict[str, object]],
    *,
    ground_truth: dict[str, object] | None = None,
) -> dict[str, object]:
    doc: dict[str, object] = {
        "schema": WORKER_RESULTS_SCHEMA,
        "schema_version": WORKER_RESULTS_SCHEMA_VERSION,
        "results": results,
    }
    if ground_truth is not None:
        doc["ground_truth"] = ground_truth
    return doc


def synthesis_document(
    *,
    result: dict[str, object],
    orchestrator: str | None = None,
    worker: str | None = None,
    mode: str | None = None,
    ground_truth: dict[str, object] | None = None,
) -> dict[str, object]:
    doc: dict[str, object] = {
        "schema": SYNTHESIS_SCHEMA,
        "schema_version": SYNTHESIS_SCHEMA_VERSION,
        "result": result,
    }
    if mode is not None:
        doc["mode"] = mode
    if worker is not None:
        doc["worker"] = worker
    if orchestrator is not None:
        doc["orchestrator"] = orchestrator
    elif mode == "direct-worker":
        doc["orchestrator"] = None
    if ground_truth is not None:
        doc["ground_truth"] = ground_truth
    return doc
