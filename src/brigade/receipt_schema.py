"""Schema version constants and SIEM-oriented receipt serialization helpers."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

VERIFY_RECEIPT_SCHEMA_VERSION = 2

# Stable env key: orchestrator run identity exported into worker environments
# via run/transport (#499). Receipt producers may stamp the value as optional
# ``producer_run_id``; omit the field when unset for legacy compatibility.
BRIGADE_RUN_ID_ENV = "BRIGADE_RUN_ID"


def producer_run_id_from_env(environ: Mapping[str, str] | None = None) -> str | None:
    """Return the orchestrator run id from ``BRIGADE_RUN_ID``, or None when absent."""
    source = environ if environ is not None else os.environ
    value = source.get(BRIGADE_RUN_ID_ENV, "")
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def stamp_optional_producer_run_id(
    payload: dict[str, Any],
    *,
    producer_run_id: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Stamp optional ``producer_run_id`` when present; omit when absent."""
    value = producer_run_id if producer_run_id is not None else producer_run_id_from_env(environ)
    if isinstance(value, str):
        cleaned = value.strip()
        if cleaned:
            payload["producer_run_id"] = cleaned
    return payload


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
RUN_EVENT_SCHEMA_VERSION = 2

CAUSAL_RECEIPT_SCHEMA = "brigade.causal_receipt.v1"
CAUSAL_RECEIPT_SCHEMA_VERSION = 1
CAUSAL_PARENT_MANIFEST_SCHEMA = "brigade.causal_parent_manifest.v1"
CAUSAL_PARENT_MANIFEST_SCHEMA_VERSION = 1

WORK_RUN_ARCHIVE_SCHEMA = "brigade.work-run"
WORK_RUN_ARCHIVE_SCHEMA_VERSION = 1

RESEARCH_SIDECAR_SCHEMA = "brigade.research.v1"
RESEARCH_SIDECAR_VERSION = 1
RESEARCH_SOURCES_SCHEMA = "brigade.research.sources.v1"
RESEARCH_SOURCES_VERSION = 1
RESEARCH_FINDINGS_SCHEMA = "brigade.research.findings.v1"
RESEARCH_FINDINGS_VERSION = 1
RESEARCH_CITATION_AUDIT_SCHEMA = "brigade.research.citation-audit.v1"
RESEARCH_CITATION_AUDIT_VERSION = 1
RESEARCH_CLAIM_AUDIT_SCHEMA = "brigade.research.claim-audit.v1"
RESEARCH_CLAIM_AUDIT_VERSION = 1


def stamp_research_sidecar(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {**payload, "schema": RESEARCH_SIDECAR_SCHEMA, "schema_version": RESEARCH_SIDECAR_VERSION}


def stamp_research_sources(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {**payload, "schema": RESEARCH_SOURCES_SCHEMA, "schema_version": RESEARCH_SOURCES_VERSION}


def stamp_research_findings(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {**payload, "schema": RESEARCH_FINDINGS_SCHEMA, "schema_version": RESEARCH_FINDINGS_VERSION}


def stamp_research_citation_audit(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        **payload,
        "schema": RESEARCH_CITATION_AUDIT_SCHEMA,
        "schema_version": RESEARCH_CITATION_AUDIT_VERSION,
    }


def stamp_research_claim_audit(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        **payload,
        "schema": RESEARCH_CLAIM_AUDIT_SCHEMA,
        "schema_version": RESEARCH_CLAIM_AUDIT_VERSION,
    }


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


def run_plan_document(
    assignments: list[dict[str, object]],
    *,
    run_id: str | None = None,
) -> dict[str, object]:
    from . import causal_receipt

    doc: dict[str, object] = {
        "schema": RUN_PLAN_SCHEMA,
        "schema_version": RUN_PLAN_SCHEMA_VERSION,
        "assignments": assignments,
    }
    if isinstance(run_id, str) and run_id:
        causal_receipt.stamp_causal_receipt(doc, causal_receipt.recorded_plan(run_id=run_id))
    return doc


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
    run_id: str | None = None,
    worker_results: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    from . import causal_receipt

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
    if isinstance(run_id, str) and run_id:
        results = worker_results if isinstance(worker_results, list) else []
        causal_receipt.stamp_causal_receipt(
            doc,
            causal_receipt.recorded_synthesis(
                run_id=run_id,
                worker_parents=causal_receipt.synthesis_worker_parents(run_id, results),
            ),
        )
    return doc
