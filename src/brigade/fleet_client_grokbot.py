"""Private Fleet Hub client for authoritative Grok Bot queue operations.

The listener on the canonical memory owner host is the only caller that
holds a fleet node token.
Cloud workers never see this module's credentials. Request bodies and
returned projections are metadata-only.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Iterator

from . import fleet_client as _client


FleetClientError = _client.FleetClientError
GROKBOT_TIMEOUT_SECONDS = _client.CLOUD_TIMEOUT_SECONDS
MAX_GROKBOT_RESPONSE_BYTES = _client.MAX_CLOUD_RESPONSE_BYTES
_SAFE_JOB_FIELDS = frozenset(
    {
        "job_id",
        "role",
        "repository",
        "label",
        "task_digest",
        "state",
        "item_revision",
        "sequence",
        "created_at",
        "updated_at",
        "queued_at",
        "timeout_seconds",
        "artifact_kind",
        "private_snapshot_id",
        "claimed_at",
        "lease_expires_at",
        "cancel_requested_at",
        "artifact_ref",
        "artifact_digest",
        "artifact_size",
        "owner_node",
        "claimant_node",
        "claimant_worker",
        "queue_id",
        "lease_generation",
        "harness",
    }
)
_OK_KEYS = {
    "enqueue": "enqueued",
    "list": "jobs",
    "status": "job",
    "whoami": "actor_kind",
    "claim": "claimed",
    "start": "started",
    "renew": "renewed",
    "complete": "completed",
    "fail": "failed",
    "cancel": "canceled",
    "expire": "expired",
    "ack-cancel": "acknowledged",
}
_LISTENER_TOKEN: ContextVar[str | None] = ContextVar("grokbot_listener_token", default=None)
GROKBOT_LISTENER_ACTORS = frozenset({"feed", "control", "operator", "implementation-worker", "repository-scout"})
GROKBOT_WORKER_ROLES = frozenset({"implementation-worker", "repository-scout"})
_BOUNDED_REFUSAL_REASONS = frozenset(
    {
        "invalid-request",
        "invalid-artifact",
        "invalid-state",
        "refused",
        "revision-conflict",
        "operation-mismatch",
        "lease-conflict",
        "idempotency-conflict",
        "auth-failed",
        "missing-digest",
        "digest-mismatch",
    }
)


@dataclass(frozen=True)
class GrokbotHubDecision:
    """Outcome of one authoritative hub queue operation."""

    granted: bool
    reason: str
    job: dict[str, Any] | None = None
    jobs: list[dict[str, Any]] | None = None
    idempotent: bool = False
    detail: str | None = None


def load_fleet_config() -> dict[str, str]:
    return _client.load_fleet_config()


def hub_configured() -> bool:
    """True when a hub URL is configured. Config read failures fail closed."""
    try:
        return bool(load_fleet_config().get("hub_url"))
    except Exception:
        return True


def enqueue(**fields: Any) -> GrokbotHubDecision:
    return _op("enqueue", **fields)


def list_jobs(*, role: str | None = None, include_all: bool = False, **fields: Any) -> GrokbotHubDecision:
    return _op("list", role=role, include_all=include_all, **fields)


def status(job_id: str, **fields: Any) -> GrokbotHubDecision:
    return _op("status", job_id=job_id, **fields)


def whoami(**fields: Any) -> GrokbotHubDecision:
    return _op("whoami", **fields)


def enroll_actor(*, node_id: str, queue_owner_node_id: str, queue_id: str, actor_kind: str) -> dict[str, object]:
    """Enroll one existing fleet node as one enabled, role-matched Grok Bot actor.

    This is an admin-only control-plane action. It deliberately returns only
    the hub's safe enrollment acknowledgement, never either bearer token.
    """
    if actor_kind not in GROKBOT_LISTENER_ACTORS:
        raise FleetClientError("Grok Bot actor kind is invalid")
    body = {
        "action": "enroll-actor",
        "enroll_node_id": node_id,
        "queue_owner_node_id": queue_owner_node_id,
        "queue_id": queue_id,
        "actor_kind": actor_kind,
        "enabled": True,
    }
    if actor_kind in GROKBOT_WORKER_ROLES:
        body["role"] = actor_kind
    payload = _client._admin_request("/grokbot", body, what="Grok Bot actor enrollment")
    if payload.get("enrolled") is not True or payload.get("node_id") != node_id:
        raise FleetClientError("fleet hub Grok Bot actor enrollment returned an invalid response")
    return {"enrolled": True, "node_id": node_id}


def current_listener_token() -> str | None:
    return _LISTENER_TOKEN.get()


@contextmanager
def listener_identity(token: str | None) -> Iterator[None]:
    """Bind one listener node token for hub calls without changing host identity."""
    handle = _LISTENER_TOKEN.set(token)
    try:
        yield
    finally:
        _LISTENER_TOKEN.reset(handle)


def claim(job_id: str, *, lease_id: str, **fields: Any) -> GrokbotHubDecision:
    return _op("claim", job_id=job_id, lease_id=lease_id, **fields)


def start(job_id: str, *, lease_id: str, **fields: Any) -> GrokbotHubDecision:
    return _op("start", job_id=job_id, lease_id=lease_id, **fields)


def renew(job_id: str, *, lease_id: str, **fields: Any) -> GrokbotHubDecision:
    return _op("renew", job_id=job_id, lease_id=lease_id, **fields)


def complete(job_id: str, *, lease_id: str, artifact: dict[str, Any], **fields: Any) -> GrokbotHubDecision:
    return _op("complete", job_id=job_id, lease_id=lease_id, artifact=artifact, **fields)


def fail(job_id: str, *, lease_id: str, **fields: Any) -> GrokbotHubDecision:
    return _op("fail", job_id=job_id, lease_id=lease_id, **fields)


def cancel(job_id: str, **fields: Any) -> GrokbotHubDecision:
    return _op("cancel", job_id=job_id, **fields)


def expire(job_id: str, **fields: Any) -> GrokbotHubDecision:
    return _op("expire", job_id=job_id, **fields)


def ack_cancel(job_id: str, *, lease_id: str, **fields: Any) -> GrokbotHubDecision:
    return _op("ack-cancel", job_id=job_id, lease_id=lease_id, **fields)


def _op(action: str, **fields: Any) -> GrokbotHubDecision:
    try:
        config = load_fleet_config()
        fields.pop("hub_url", None)
        fields.pop("token", None)
        fields.pop("base_path", None)
        hub = config["hub_url"]
        token = current_listener_token() or config["token"]
        if not hub:
            return GrokbotHubDecision(False, "no-hub")
        if current_listener_token() is None:
            node = _client.resolve_node_id()
            if not _client._node_id_is_claimable(node):
                return GrokbotHubDecision(False, "no-identity", detail=node)
        fields.pop("node_id", None)
        fields.pop("holder", None)
        fields.pop("worker", None)
        body = {"action": action}
        body.update({key: value for key, value in fields.items() if value is not None})
        status, payload = _client._run_with_deadline(
            lambda: _post_grokbot_blocking(hub, token, body, timeout=GROKBOT_TIMEOUT_SECONDS),
            timeout=GROKBOT_TIMEOUT_SECONDS,
        )
    except Exception:
        return GrokbotHubDecision(False, "hub-unavailable")
    payload = payload if isinstance(payload, dict) else {}
    job = _safe_job(payload.get("job"))
    jobs: list[dict[str, Any]] | None = None
    raw_jobs = payload.get("jobs")
    if isinstance(raw_jobs, list):
        jobs = []
        for item in raw_jobs:
            safe = _safe_job(item)
            if safe is not None:
                jobs.append(safe)
    if status in (401, 403):
        return GrokbotHubDecision(False, "auth-failed", job=job, jobs=jobs)
    ok_key = _OK_KEYS[action]
    if action == "list" and status == 200 and jobs is not None:
        return GrokbotHubDecision(True, "ok", jobs=jobs)
    if action == "status" and status == 200 and job is not None:
        return GrokbotHubDecision(True, "ok", job=job)
    if action == "whoami" and status == 200 and isinstance(payload.get("actor_kind"), str):
        return GrokbotHubDecision(
            True,
            "ok",
            job={"actor_kind": payload.get("actor_kind"), "role": payload.get("role")},
        )
    if status == 200 and payload.get(ok_key) is True:
        return GrokbotHubDecision(True, "ok", job=job, jobs=jobs, idempotent=bool(payload.get("idempotent")))
    if status == 200 and action == "expire" and payload.get(ok_key) is False and job is not None:
        return GrokbotHubDecision(True, "ok", job=job)
    if status in (200, 400, 409):
        error = payload.get("error")
        reason = _bounded_refusal(error, status)
        return GrokbotHubDecision(False, reason, job=job, jobs=jobs)
    return GrokbotHubDecision(False, "hub-unavailable", job=job, jobs=jobs)


def _bounded_refusal(error: object, status: int) -> str:
    if isinstance(error, str) and error in _BOUNDED_REFUSAL_REASONS:
        return error
    if status == 400:
        if isinstance(error, str) and "artifact" in error:
            return "invalid-artifact"
        return "invalid-request"
    if isinstance(error, str) and error:
        return "refused"
    return "refused"


def _post_grokbot_blocking(hub_url: str, token: str, body: dict[str, Any], *, timeout: float) -> tuple[int, Any]:
    request = urllib.request.Request(
        hub_url.rstrip("/") + "/grokbot",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    try:
        with _client._hub_open(request, timeout=timeout) as response:
            return response.status, _bounded_json(response)
    except urllib.error.HTTPError as exc:
        if exc.code in (400, 401, 403, 409):
            try:
                return exc.code, _bounded_json(exc)
            except FleetClientError:
                return exc.code, {}
        raise


def _bounded_json(response: Any) -> Any:
    raw = response.read(MAX_GROKBOT_RESPONSE_BYTES + 1)
    if len(raw) > MAX_GROKBOT_RESPONSE_BYTES:
        raise FleetClientError("fleet hub grokbot response exceeded the size limit")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FleetClientError("fleet hub grokbot response was not valid JSON") from exc


def _safe_job(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    return {key: value for key, value in raw.items() if key in _SAFE_JOB_FIELDS}
