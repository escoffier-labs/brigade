"""Read-only hub projections retained for Task 5."""

from __future__ import annotations


from typing import Any


from .grokbot_job_validation import (
    WORKER_LABEL_RE,
    GrokbotJobError,
)


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


def _hub_jobs(*, role: str | None = None, include_all: bool = False) -> list[dict[str, Any]]:
    from . import fleet_client_grokbot

    decision = fleet_client_grokbot.list_jobs(role=role, include_all=include_all)
    if not decision.granted or decision.jobs is None:
        raise GrokbotJobError(decision.reason, action="list")
    return [_hub_projection_job(job) for job in decision.jobs]


def _default_queue_ttl_seconds() -> int:
    from .grokbot_jobs import DEFAULT_QUEUE_TTL_SECONDS

    return DEFAULT_QUEUE_TTL_SECONDS


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
        "worker_label",
        "lease_generation",
        "queue_id",
    ):
        if key in job and job[key] is not None:
            projection[key] = job[key]
    _drop_unsafe_worker_label(projection)
    return projection


def _drop_unsafe_worker_label(projection: dict[str, Any]) -> None:
    """Hold a hub-supplied label to the same pattern the write path enforces.

    A hub row is untrusted on the read path. ``_safe_job`` filters keys but
    never values, and the operator CLI interpolates the label into one line per
    job, so a newline-bearing or unbounded label would forge output. Drop any
    value that would not have been accepted on claim (#1501).
    """
    label = projection.get("worker_label")
    if label is None:
        return
    if not isinstance(label, str) or not WORKER_LABEL_RE.fullmatch(label):
        del projection["worker_label"]
