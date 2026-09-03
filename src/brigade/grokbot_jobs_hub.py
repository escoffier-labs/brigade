"""Hub-authority listing and report-pairing helpers for Grok Bot jobs.

Queue storage and lease mutations stay in ``grokbot_jobs``. This sibling lists
hub jobs, projects safe rows, and pairs local artifacts and snapshots by
``job_id``. ``grokbot_jobs`` re-exports every name so callers and tests keep
the same bindings.
"""

from __future__ import annotations

from typing import Any

from .grokbot_job_validation import (
    JOB_ID_RE,
    GrokbotJobError,
    validate_job_id as _validate_job_id,
)


def _read_hub_report_from_storage(storage: Any, job: dict[str, Any]) -> dict[str, Any]:
    """Verify one hub-listed report against its local artifact bytes."""
    from . import grokbot_jobs

    job_id = _validate_job_id(job["job_id"])
    expected = job.get("artifact_digest")
    if not isinstance(expected, str) or not expected:
        raise GrokbotJobError("missing-digest")
    data = grokbot_jobs._read_bytes_file(
        storage.artifacts, f"{job_id}.md", maximum=grokbot_jobs.MAX_REPORT_BYTES, missing_reason="report-missing"
    )
    return grokbot_jobs._verified_report(job_id, data, expected)


def _snapshots_by_job_id(storage: Any) -> dict[str, dict[str, Any]]:
    """Index private task snapshots by job_id for hub-authority pairing."""
    from . import grokbot_jobs

    if storage.snapshots is None:
        return {}
    index: dict[str, dict[str, Any]] = {}
    for name in grokbot_jobs._list_names(storage.snapshots, prefix="grokbot-", suffix=".json"):
        payload = grokbot_jobs._read_json_file(storage.snapshots, name, missing_ok=True)
        if not isinstance(payload, dict) or payload.get("schema") != grokbot_jobs.SNAPSHOT_SCHEMA:
            continue
        job_id = payload.get("job_id")
        name_id = name[: -len(".json")]
        if isinstance(job_id, str) and JOB_ID_RE.fullmatch(job_id) and job_id == name_id:
            index[job_id] = payload
    return index


def _hub_unavailable_row() -> dict[str, Any]:
    return {
        "job_id": "grokbot-hub-unavailable",
        "label": "Grok Bot hub unavailable",
        "task_hash": None,
        "state": "unavailable",
        "created_at": None,
        "updated_at": None,
        "queued_at": None,
        "artifact": {"kind": "report"},
        "degraded": True,
        "classification": "needs-investigation",
        "source": "grokbot-hub",
    }


def _hub_tracker_artifact(job: dict[str, Any], kind: object) -> dict[str, Any]:
    artifact: dict[str, Any] = {"kind": kind}
    if kind == "draft-pr" and job.get("artifact_ref"):
        artifact["url"] = job["artifact_ref"]
    elif kind == "branch":
        if job.get("artifact_ref"):
            artifact["branch"] = job["artifact_ref"]
        if job.get("artifact_digest"):
            artifact["commit"] = job["artifact_digest"]
    elif kind == "report":
        if job.get("artifact_digest"):
            artifact["sha256"] = job["artifact_digest"]
        if job.get("artifact_size") is not None:
            artifact["size"] = job["artifact_size"]
        if job.get("private_snapshot_id"):
            artifact["private_snapshot_id"] = job["private_snapshot_id"]
    else:
        return {}
    return artifact if len(artifact) > 1 else {}


def _hub_job(job_id: str) -> dict[str, Any]:
    return _hub_projection(_require_hub("status", job_id=job_id))


def _hub_jobs(*, role: str | None = None, include_all: bool = False) -> list[dict[str, Any]]:
    from . import fleet_client_grokbot

    decision = fleet_client_grokbot.list_jobs(role=role, include_all=include_all)
    if not decision.granted or decision.jobs is None:
        raise GrokbotJobError(decision.reason, action="list")
    return [_hub_projection_job(job) for job in decision.jobs]


def _require_hub(action: str, job_id: str | None = None, **fields: Any) -> Any:
    from . import fleet_client_grokbot

    operation = getattr(fleet_client_grokbot, action)
    decision = operation(job_id, **fields) if job_id is not None else operation(**fields)
    if not decision.granted:
        raise GrokbotJobError(decision.reason, action=action)
    return decision


def _default_queue_ttl_seconds() -> int:
    from .grokbot_jobs import DEFAULT_QUEUE_TTL_SECONDS

    return DEFAULT_QUEUE_TTL_SECONDS


def _hub_projection(decision: Any) -> dict[str, Any]:
    if decision.job is None:
        raise GrokbotJobError(decision.reason)
    return _hub_projection_job(decision.job)


def _hub_projection_job(job: dict[str, Any]) -> dict[str, Any]:
    digest = job.get("task_digest")
    projection = {
        "job_id": job["job_id"],
        "label": job.get("label"),
        "role": job.get("role"),
        "repository": job.get("repository"),
        "task_hash": f"sha256:{digest}"
        if isinstance(digest, str) and not str(digest).startswith("sha256:")
        else digest,
        "state": job["state"],
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
        "queued_at": job.get("queued_at"),
        "timeout_seconds": job.get("timeout_seconds"),
        "queue_ttl_seconds": job.get("queue_ttl_seconds", _default_queue_ttl_seconds()),
        "item_revision": job.get("item_revision"),
        "sequence": job.get("sequence"),
        "artifact": {"kind": job.get("artifact_kind")},
        "artifact_kind": job.get("artifact_kind"),
        "harness": "grokbot",
    }
    for key in (
        "claimed_at",
        "lease_expires_at",
        "cancel_requested_at",
        "private_snapshot_id",
        "artifact_ref",
        "artifact_digest",
        "artifact_size",
        "claimant_node",
        "claimant_worker",
        "lease_generation",
        "queue_id",
    ):
        if key in job and job[key] is not None:
            projection[key] = job[key]
    return projection
