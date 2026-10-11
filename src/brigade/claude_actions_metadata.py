"""Passive, bounded job and artifact metadata for an independently confirmed run.

Only the injected GET boundary performs IO. Jobs belong to an exact attempt,
whereas artifact metadata belongs to the run and cannot attest an attempt.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict
from typing import Any
from urllib.parse import urlsplit

from brigade.claude_actions_evidence import (
    MAX_PROVIDER_ID,
    ActionRunEvidence,
    ConfirmedWorkflow,
    _CONCLUSIONS,
    _STATUSES,
    _provider_id,
    _repository,
    _sha,
    _text,
    _timestamp,
)
from brigade.claude_actions_import import FixtureTransport, Limits, _Reader
from brigade.claude_review_import import GetTransport


def _url(value: object, host: str, paths: set[str]) -> str | None:
    text = _text(value, 2048)
    if text is None:
        return None
    try:
        parsed = urlsplit(text)
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or parsed.netloc != host
        or "?" in text
        or "#" in text
        or parsed.path.lower() not in paths
    ):
        return None
    return f"https://{host}{parsed.path.lower()}"


def _relation(head: str | None, current: str) -> str:
    if head is None:
        return "unknown"
    return "current-head" if head == current else "stale-head"


def _nonnegative(value: object) -> int | None:
    return value if type(value) is int and 0 <= value <= MAX_PROVIDER_ID else None


def _job(
    row: dict[str, Any],
    *,
    run: ActionRunEvidence,
    current: str,
    runners: Mapping[int, str],
) -> dict[str, Any]:
    repository, run_id, attempt = run.identity
    job_id = row["id"]
    head = _sha(row.get("head_sha"))
    status_text = _text(row.get("status"), 32)
    status = status_text if status_text in _STATUSES else "unknown"
    conclusion_text = _text(row.get("conclusion"), 32)
    conclusion = conclusion_text if status == "completed" and conclusion_text in _CONCLUSIONS else "unknown"
    runner = runners.get(job_id, "unknown")
    return {
        "identity_key": f"github-actions-job:{repository}:{run_id}:{attempt}:{job_id}",
        "job_id": job_id,
        "run_id": run_id,
        "run_attempt": attempt,
        "workflow_id": run.workflow.workflow_id,
        "definition_revision": run.workflow.revision,
        "head_sha": head,
        "head_relation": _relation(head, current),
        "status": status,
        "conclusion": conclusion,
        "created_at": _timestamp(row.get("created_at")),
        "started_at": _timestamp(row.get("started_at")),
        "completed_at": _timestamp(row.get("completed_at")),
        "updated_at": _timestamp(row.get("updated_at")),
        "api_url": _url(row.get("url"), "api.github.com", {f"/repos/{repository}/actions/jobs/{job_id}"}),
        "html_url": _url(
            row.get("html_url"),
            "github.com",
            {
                f"/{repository}/actions/runs/{run_id}/job/{job_id}",
                f"/{repository}/runs/{run_id}/jobs/{job_id}",
            },
        ),
        "runner_kind": runner,
        "runner_authority": "caller-confirmed-job-runner" if runner != "unknown" else "unknown",
        "local_node_id": None,
        "review_state": "unknown",
        "findings_state": "unknown",
        "merge_readiness": "unknown",
        "deletion_state": "unknown",
    }


def _artifact(row: dict[str, Any], *, run: ActionRunEvidence, current: str) -> dict[str, Any]:
    repository = run.workflow.repository
    artifact_id = row["id"]
    head = _sha(row["workflow_run"].get("head_sha"))
    expired = row.get("expired") if type(row.get("expired")) is bool else None
    return {
        "identity_key": f"github-actions-artifact:{repository}:{artifact_id}",
        "artifact_id": artifact_id,
        "run_id": run.run_id,
        "run_attempt": None,
        "attempt_association": "unknown",
        "definition_revision": None,
        "head_sha": head,
        "head_relation": _relation(head, current),
        "expired": expired,
        "availability": "expired" if expired is True else "present",
        "content_availability": "unknown",
        "deletion_state": "unknown",
        "size_in_bytes": _nonnegative(row.get("size_in_bytes")),
        "created_at": _timestamp(row.get("created_at")),
        "updated_at": _timestamp(row.get("updated_at")),
        "expires_at": _timestamp(row.get("expires_at")),
        "api_url": _url(row.get("url"), "api.github.com", {f"/repos/{repository}/actions/artifacts/{artifact_id}"}),
        "local_node_id": None,
    }


def collect_metadata(
    transport: GetTransport | None = None,
    *,
    run: ActionRunEvidence,
    current_head_sha: str,
    observed_at: str,
    limits: Limits | None = None,
    clock: Callable[[], float] = time.monotonic,
    runner_kinds: Mapping[int, str] | None = None,
) -> dict[str, Any]:
    """Collect one metadata snapshot, without discovery, downloads or persistence.

    The caller independently confirms the run attempt and its definition revision.
    Runner attestations are keyed by provider job ID, never inferred from names.
    The transport owns authorization, blocking timeouts and redirect prevention.
    """
    if (
        not isinstance(run, ActionRunEvidence)
        or not isinstance(run.workflow, ConfirmedWorkflow)
        or _repository(run.workflow.repository) is None
        or _repository(run.workflow.repository) != run.workflow.repository
        or _provider_id(run.workflow.workflow_id) is None
        or _sha(run.workflow.revision) is None
        or _sha(run.workflow.revision) != run.workflow.revision
        or _provider_id(run.run_id) is None
        or _provider_id(run.run_attempt) is None
        or run.provider != "github-actions"
        or (run.head_sha is not None and _sha(run.head_sha) is None)
    ):
        raise ValueError("metadata requires a confirmed workflow and bounded run attempt")
    current, observed = _sha(current_head_sha), _timestamp(observed_at)
    if current is None or observed is None:
        raise ValueError("full current head and observation time are required")
    bounds = limits if limits is not None else Limits()
    if not isinstance(bounds, Limits):
        raise ValueError("bounded read limits are required")
    bounds = Limits(**asdict(bounds))
    supplied = runner_kinds if runner_kinds is not None else {}
    if not isinstance(supplied, Mapping) or len(supplied) > bounds.max_objects:
        raise ValueError("bounded job runner attestations are required")
    runners: dict[int, str] = {}
    for job_id, kind in supplied.items():
        if (
            _provider_id(job_id) is None
            or not isinstance(kind, str)
            or kind
            not in (
                "github-hosted",
                "self-hosted",
                "unknown",
            )
        ):
            raise ValueError("runner attestations require bounded job IDs and explicit hosting kinds")
        runners[job_id] = kind
    if not callable(clock) or (transport is not None and not callable(getattr(transport, "get", None))):
        raise ValueError("metadata requires a GET transport and clock")
    reader = _Reader(transport if transport is not None else FixtureTransport(), bounds, clock)
    repository, run_id, attempt = run.identity
    base = f"/repos/{repository}/actions/runs/{run_id}"
    quarantined: list[dict[str, Any]] = []
    duplicates = 0

    def endpoint(kind: str, path: str) -> tuple[list[dict[str, Any]], str]:
        nonlocal duplicates
        rows: dict[int, dict[str, Any]] = {}
        origins: dict[int, dict[str, Any]] = {}
        conflicts: set[int] = set()
        rejected: set[int] = set()
        seen_pages: set[str] = set()
        total: int | None = None
        start = len(reader.sources)

        def quarantine(identity: int | None, reason: str, source: dict[str, Any]) -> None:
            quarantined.append(
                {
                    "resource": kind,
                    "provider_id": identity,
                    "reason": reason,
                    "source_url": source["source_url"],
                    "source_page": source["page"],
                    "observed_at": observed,
                }
            )

        for page in range(1, bounds.max_pages + 1):
            payload, source = reader.read(path, page=page)
            source["resource"] = kind
            if payload is None:
                break
            items, count = payload.get(kind), _nonnegative(payload.get("total_count"))
            if not isinstance(items, list) or count is None:
                reader.reason("malformed-payload", source)
                break
            if total is not None and count != total:
                reader.reason("inconsistent-total", source)
                break
            total = count
            if len(items) > bounds.per_page:
                reader.reason("oversized-page", source)
                break
            fingerprint = hashlib.sha256(json.dumps(items, sort_keys=True).encode()).hexdigest()
            if items and fingerprint in seen_pages:
                reader.reason("repeated-page", source)
                break
            seen_pages.add(fingerprint)
            for row in items:
                if not reader.object(source):
                    break
                if not isinstance(row, dict):
                    reader.reason("malformed-payload", source)
                    continue
                identity = _provider_id(row.get("id"))
                # Compare complete JSON before normalization so even changes in
                # redacted fields cannot silently replace an earlier identity.
                if identity is not None and identity in rows:
                    duplicates += 1
                    if json.dumps(rows[identity], sort_keys=True) != json.dumps(row, sort_keys=True):
                        if identity not in conflicts:
                            quarantine(identity, "conflicting-identity", source)
                        conflicts.add(identity)
                        reader.reason("conflicting-identity", source)
                    continue
                if identity is not None:
                    rows[identity] = row
                    origins[identity] = source
                head = _sha(row.get("head_sha"))
                known_head = _sha(run.head_sha)
                binding = row.get("workflow_run")
                valid = identity is not None
                if kind == "jobs":
                    valid = (
                        valid
                        and _provider_id(row.get("run_id")) == run_id
                        and ("run_attempt" not in row or _provider_id(row["run_attempt"]) == attempt)
                        and not (head is not None and known_head is not None and head != known_head)
                    )
                else:
                    head = _sha(binding.get("head_sha")) if isinstance(binding, dict) else None
                    valid = (
                        valid
                        and isinstance(binding, dict)
                        and _provider_id(binding.get("id")) == run_id
                        and not (head is not None and known_head is not None and head != known_head)
                    )
                if not valid:
                    if identity is not None:
                        rejected.add(identity)
                    quarantine(identity, "object-identity-mismatch", source)
                    reader.reason("object-identity-mismatch", source)
            if not source["complete"]:
                break
            if len(rows) >= total:
                if len(rows) != total:
                    reader.reason("inconsistent-total", source)
                break
            if len(items) < bounds.per_page:
                reader.reason("pagination-truncated", source)
                break
            if page == bounds.max_pages:
                reader.reason("page-budget", source)

        records: list[dict[str, Any]] = []
        for identity, row in rows.items():
            if identity in conflicts or identity in rejected:
                continue
            record = (
                _job(row, run=run, current=current, runners=runners)
                if kind == "jobs"
                else _artifact(row, run=run, current=current)
            )
            record.update(
                provider="github-actions",
                repository=repository,
                scope="run-attempt" if kind == "jobs" else "run",
                authority="caller-confirmed-definition-and-github-job"
                if kind == "jobs"
                else "github-artifact-metadata",
                source_url=origins[identity]["source_url"],
                source_page=origins[identity]["page"],
                revision=record["definition_revision"],
                observed_at=observed,
            )
            records.append(record)
        sources = reader.sources[start:]
        if all(source["complete"] for source in sources):
            state = "confirmed" if records else "absent-in-complete-scope"
        else:
            state = "unknown"
            for source in sources:
                status = source["http_status"]
                if status is not None and status != 200:
                    state = {403: "access-denied", 429: "rate-limited"}.get(status, "unavailable")
                    break
        return records, state

    jobs, jobs_state = endpoint("jobs", f"{base}/attempts/{attempt}/jobs")
    artifacts, artifacts_state = endpoint("artifacts", f"{base}/artifacts")
    return {
        "provider": "github-actions",
        "repository": repository,
        "workflow_id": run.workflow.workflow_id,
        "run_id": run_id,
        "run_attempt": attempt,
        "current_head_sha": current,
        "observed_at": observed,
        "jobs": jobs,
        "artifacts": artifacts,
        "jobs_state": jobs_state,
        "artifacts_state": artifacts_state,
        "current_head_job_identities": [job["identity_key"] for job in jobs if job["head_relation"] == "current-head"],
        "quarantined": quarantined,
        "duplicate_objects": duplicates,
        "complete": not reader.reasons,
        "absence": "unknown",
        "review_state": "unknown",
        "findings_state": "unknown",
        "merge_readiness": "unknown",
        "deletion_state": "unknown",
        "sources": reader.sources,
        "usage": reader.usage,
        "limits": asdict(bounds),
        "incomplete_reasons": reader.reasons,
    }
