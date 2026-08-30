"""Cloud and model-policy transport for :mod:`brigade.fleet_client`.

The public client keeps these names as compatibility re-exports.  This module
holds the independently evolving cloud and model admission protocol so the
event spool and repository-claim client remain easier to audit.
"""

import importlib
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from . import fleet_client as _client


FleetClientError = _client.FleetClientError
MAX_CLOUD_RESPONSE_BYTES = _client.MAX_CLOUD_RESPONSE_BYTES
CLOUD_TIMEOUT_SECONDS = _client.CLOUD_TIMEOUT_SECONDS


def _hub_open(request: urllib.request.Request, *, timeout: float):
    return _client._hub_open(request, timeout=timeout)


def _node_id_is_claimable(node_id: str) -> bool:
    return _client._node_id_is_claimable(node_id)


def _run_with_deadline(fn: Any, *, timeout: float) -> Any:
    return _client._run_with_deadline(fn, timeout=timeout)


def load_fleet_config() -> dict[str, str]:
    return _client.load_fleet_config()


def load_fleet_settings() -> dict[str, str]:
    return _client.load_fleet_settings()


def resolve_node_id(base_path: Path | None = None) -> str:
    return _client.resolve_node_id(base_path)


@dataclass(frozen=True)
class CloudDecision:
    """Outcome of one cloud-lease operation.

    ``holder`` is returned only to the local caller that supplied or minted
    it. The hub never returns holder tokens, and ``lease`` is restricted to
    the public lease fields before this value leaves the transport layer.
    """

    granted: bool
    reason: str
    lease: dict[str, Any] | None = None
    detail: str | None = None
    holder: str | None = None


@dataclass(frozen=True)
class ModelLeaseDecision:
    granted: bool
    reason: str
    lease_id: str | None = None
    holder: str | None = None


_CLOUD_OK_KEYS = {"admit": "admitted", "bind": "bound", "renew": "renewed", "release": "released"}
_CLOUD_LEASE_FIELDS = frozenset(
    {
        "lease_id",
        "provider",
        "provider_task_id",
        "repo",
        "label",
        "owner_node",
        "owner_conductor",
        "state",
        "admitted_at",
        "renewed_at",
        "ttl_seconds",
        "expires_at",
        "artifact_ref",
        "released_at",
        "expired",
    }
)
_MODEL_POLICY_FIELDS = frozenset(
    {
        "provider",
        "model",
        "seat",
        "enabled",
        "limit",
        "notes",
        "reasoning",
        "brigade_cli",
        "t3_instance_id",
        "t3_service_tier",
    }
)


def _bounded_json_response(response: Any, *, limit: int = MAX_CLOUD_RESPONSE_BYTES) -> Any:
    """Decode one small hub response without retaining arbitrary bodies."""
    raw = response.read(limit + 1)
    if len(raw) > limit:
        raise FleetClientError("fleet hub cloud response exceeded the size limit")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FleetClientError("fleet hub cloud response was not valid JSON") from exc


def _post_cloud_blocking(hub_url: str, token: str, body: dict[str, Any], *, timeout: float) -> tuple[int, Any]:
    """POST one cloud operation, returning only a bounded decoded payload."""
    request = urllib.request.Request(
        hub_url.rstrip("/") + "/cloud",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    try:
        with _hub_open(request, timeout=timeout) as response:
            return response.status, _bounded_json_response(response)
    except urllib.error.HTTPError as exc:
        if exc.code in (400, 401, 403, 409):
            try:
                return exc.code, _bounded_json_response(exc)
            except FleetClientError:
                return exc.code, {}
        raise


def _post_model_policy_blocking(hub_url: str, token: str, body: dict[str, Any], *, timeout: float) -> tuple[int, Any]:
    """POST /models mutation, returning only a bounded decoded payload."""
    request = urllib.request.Request(
        hub_url.rstrip("/") + "/models",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    try:
        with _hub_open(request, timeout=timeout) as response:
            return response.status, _bounded_json_response(response)
    except urllib.error.HTTPError as exc:
        if exc.code in (400, 401, 403, 409):
            try:
                return exc.code, _bounded_json_response(exc)
            except FleetClientError:
                return exc.code, {}
        raise


def _get_cloud_blocking(hub_url: str, path: str, token: str, *, timeout: float) -> Any:
    request = urllib.request.Request(
        hub_url.rstrip("/") + path,
        headers={"Authorization": f"Bearer {token}"},
    )
    with _hub_open(request, timeout=timeout) as response:
        if response.status != 200:
            raise FleetClientError(f"fleet hub cloud request failed: HTTP {response.status}")
        return _bounded_json_response(response)


def _get_models_blocking(hub_url: str, path: str, token: str, *, timeout: float) -> Any:
    """GET /models with the roster cap. Cloud endpoints stay at 64 KiB."""
    roster_limit = 1024 * 1024
    request = urllib.request.Request(
        hub_url.rstrip("/") + path,
        headers={"Authorization": f"Bearer {token}"},
    )
    with _hub_open(request, timeout=timeout) as response:
        if response.status != 200:
            raise FleetClientError(f"fleet hub model roster request failed: HTTP {response.status}")
        return _bounded_json_response(response, limit=roster_limit)


def _safe_cloud_lease(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    return {key: value for key, value in raw.items() if key in _CLOUD_LEASE_FIELDS}


def _safe_model_policy(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    return {key: value for key, value in raw.items() if key in _MODEL_POLICY_FIELDS}


def _existing_seat(snapshot: Mapping[str, Any], seat: str) -> dict[str, Any] | None:
    raw_seats = snapshot.get("seats")
    if not isinstance(raw_seats, list):
        return None
    for item in raw_seats:
        if isinstance(item, dict) and item.get("seat") == seat:
            return item
    return None


def _preserved_field(
    value: str | None,
    existing: Mapping[str, Any] | None,
    key: str,
    *,
    default: str = "",
) -> str:
    if value is not None:
        return value
    if existing is None:
        return default
    current = existing.get(key)
    if isinstance(current, str) and current:
        return current
    bindings = existing.get("bindings")
    if isinstance(bindings, dict):
        bound = bindings.get(key)
        if isinstance(bound, str) and bound:
            return bound
    return default


def _normalize_cloud_prompt_hash(value: str | None) -> str | None:
    """Strip the cloud-tracker ``sha256:`` prefix before hub admission."""
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    return stripped.removeprefix("sha256:")


def _cloud_op(
    action: str,
    *,
    provider: str | None = None,
    lease_id: str | None = None,
    holder: str | None = None,
    node_id: str | None = None,
    hub_url: str | None = None,
    token: str | None = None,
    base_path: Path | None = None,
    **fields: Any,
) -> CloudDecision:
    """One bounded cloud operation that fails closed on every uncertainty."""
    lease = lease_id or uuid4().hex
    fence = holder or uuid4().hex
    try:
        config = load_fleet_config()
        hub = hub_url or config["hub_url"]
        if not hub:
            return CloudDecision(False, "no-hub", holder=fence)
        node = node_id or resolve_node_id(base_path)
        if not _node_id_is_claimable(node):
            return CloudDecision(False, "no-identity", detail=node, holder=fence)
        body: dict[str, Any] = {"action": action, "lease_id": lease, "node_id": node, "holder": fence}
        if provider is not None:
            body["provider"] = provider
        if "prompt_hash" in fields:
            fields = dict(fields)
            fields["prompt_hash"] = _normalize_cloud_prompt_hash(fields.get("prompt_hash"))
        body.update(fields)
        status, payload = _run_with_deadline(
            lambda: _client._post_cloud_blocking(hub, token or config["token"], body, timeout=CLOUD_TIMEOUT_SECONDS),
            timeout=CLOUD_TIMEOUT_SECONDS,
        )
    except Exception:
        return CloudDecision(False, "hub-unavailable", holder=fence)
    payload = payload if isinstance(payload, dict) else {}
    safe_lease = _safe_cloud_lease(payload.get("lease"))
    if status in (401, 403):
        return CloudDecision(False, "auth-failed", holder=fence)
    if status == 200 and payload.get(_CLOUD_OK_KEYS[action]) is True:
        # The client minted the lease id and sent it in this request. Keep that
        # request identity authoritative even if an older or malformed hub
        # omits it from the public lease projection. Callers must always be able
        # to bind, renew, or release a successful lease.
        safe_lease = dict(safe_lease or {})
        safe_lease["lease_id"] = lease
        return CloudDecision(True, "ok", lease=safe_lease, holder=fence)
    if status in (200, 409):
        return CloudDecision(False, "refused", lease=safe_lease, holder=fence)
    return CloudDecision(False, "hub-unavailable", holder=fence)


def admit_cloud(
    provider: str,
    *,
    repo: str | None = None,
    label: str | None = None,
    prompt_hash: str | None = None,
    conductor: str | None = None,
    ttl_seconds: int = 300,
    **kwargs: Any,
) -> CloudDecision:
    return _cloud_op(
        "admit",
        provider=provider,
        repo=repo,
        label=label,
        prompt_hash=prompt_hash,
        conductor=conductor,
        ttl_seconds=ttl_seconds,
        **kwargs,
    )


def bind_cloud(
    lease_id: str, provider_task_id: str, *, artifact_ref: str | None = None, **kwargs: Any
) -> CloudDecision:
    return _cloud_op("bind", lease_id=lease_id, provider_task_id=provider_task_id, artifact_ref=artifact_ref, **kwargs)


def renew_cloud(lease_id: str, *, ttl_seconds: int = 900, **kwargs: Any) -> CloudDecision:
    return _cloud_op("renew", lease_id=lease_id, ttl_seconds=ttl_seconds, **kwargs)


def release_cloud(lease_id: str, *, state: str = "released", **kwargs: Any) -> CloudDecision:
    return _cloud_op("release", lease_id=lease_id, state=state, **kwargs)


def fetch_cloud(*, hub_url: str | None = None, include_all: bool = False) -> dict[str, Any]:
    """Return the hub's sanitized cloud snapshot, never holder capabilities."""
    config = load_fleet_config()
    hub = hub_url or config["hub_url"]
    if not hub:
        raise FleetClientError("no fleet hub configured (~/.brigade/fleet.toml [fleet] hub_url)")
    try:
        payload = _run_with_deadline(
            lambda: _get_cloud_blocking(
                hub, "/cloud?all=1" if include_all else "/cloud", config["token"], timeout=CLOUD_TIMEOUT_SECONDS
            ),
            timeout=CLOUD_TIMEOUT_SECONDS,
        )
    except FleetClientError:
        raise
    except Exception as exc:
        raise FleetClientError("fleet hub cloud read failed") from exc
    if not isinstance(payload, dict):
        return {"leases": [], "policy": {}}
    leases = payload.get("leases")
    policy = payload.get("policy")
    return {
        "leases": [safe for item in leases if (safe := _safe_cloud_lease(item)) is not None]
        if isinstance(leases, list)
        else [],
        "policy": policy if isinstance(policy, dict) else {},
    }


def fetch_model_policy(*, hub_url: str | None = None) -> list[dict[str, Any]]:
    """Return the bounded, sanitized model-policy rows from the hub."""
    snapshot = load_model_policy_snapshot(hub_url=hub_url)
    state = snapshot["state"]
    if state == "unconfigured":
        raise FleetClientError("no fleet hub configured (~/.brigade/fleet.toml [fleet] hub_url)")
    if state == "auth-failed":
        raise FleetClientError("fleet hub model policy read rejected this node's credentials")
    if state != "authoritative":
        raise FleetClientError("fleet hub model policy read failed")
    return list(snapshot["models"])


def load_model_policy_snapshot(*, hub_url: str | None = None) -> dict[str, Any]:
    """Classify one bounded model-policy read for run admission.

    A missing hub preserves standalone Brigade behavior. A configured hub
    lazily fetches the validated versioned roster (LKG permitted). Auth and
    transport failures stay classified for existing callers. A successful
    read is authoritative even when the registry is empty.
    """
    config = load_fleet_config()
    hub = hub_url or config["hub_url"]
    if not hub:
        return {"state": "unconfigured", "models": []}
    admission = importlib.import_module("brigade.fleet_model_admission")

    try:
        decision = admission.fetch_versioned_roster(allow_lkg=True, hub_url=hub)
    except Exception:
        return {"state": "unavailable", "models": []}
    if not decision.ok:
        if decision.reason == "auth-failed":
            return {"state": "auth-failed", "models": []}
        if decision.reason in {"unsupported-schema", "node-token-required", "admin-token-not-cacheable"}:
            return {"state": decision.reason, "models": []}
        return {"state": "unavailable", "models": []}
    payload = decision.payload if isinstance(decision.payload, dict) else {}
    seats = [dict(item) for item in payload.get("seats") or [] if isinstance(item, dict)]
    safe_models = [safe for item in seats if (safe := _safe_model_policy(item)) is not None]
    revision = payload.get("revision", payload.get("roster_revision"))
    source = payload.get("source")
    if not isinstance(source, str) or not source:
        source = decision.reason if decision.reason in {"hub", "lkg"} else "hub"
    return {
        "schema": payload.get("schema"),
        "state": "authoritative",
        "source": source,
        "roster_revision": revision,
        "revision": revision,
        "roster_digest": payload.get("roster_digest"),
        "expires_at": payload.get("expires_at"),
        "seats": seats,
        "consumer_defaults": payload.get("consumer_defaults"),
        "retired_models": payload.get("retired_models"),
        "models": safe_models,
    }


def set_model_policy(
    provider: str,
    model: str,
    seat: str,
    *,
    enabled: bool,
    limit: int | None = None,
    notes: str | None = None,
    reasoning: str | None = None,
    brigade_cli: str | None = None,
    t3_instance_id: str | None = None,
    t3_service_tier: str | None = None,
    expected_revision: int | None = None,
) -> dict[str, Any]:
    """Set one seat's provider/model policy with the configured admin token."""
    try:
        settings = load_fleet_settings()
        hub = settings["hub_url"]
        if not hub:
            raise FleetClientError("no fleet hub configured (~/.brigade/fleet.toml [fleet] hub_url)")
        admin_token = settings["admin_token"]
        if not admin_token:
            raise FleetClientError(
                "no fleet admin token configured (~/.brigade/fleet.toml [fleet] token_file or BRIGADE_FLEET_TOKEN)"
            )
        snapshot = _run_with_deadline(
            lambda: _client._get_models_blocking(hub, "/models", admin_token, timeout=CLOUD_TIMEOUT_SECONDS),
            timeout=CLOUD_TIMEOUT_SECONDS,
        )
        if not isinstance(snapshot, dict) or type(snapshot.get("revision")) is not int:
            raise FleetClientError("fleet hub model policy revision is missing")
        revision = int(snapshot["revision"]) if expected_revision is None else expected_revision
        existing = _existing_seat(snapshot, seat)
        resolved_reasoning = _preserved_field(reasoning, existing, "reasoning", default="none")
        resolved_cli = _preserved_field(brigade_cli, existing, "brigade_cli")
        resolved_t3 = _preserved_field(t3_instance_id, existing, "t3_instance_id")
        resolved_tier = _preserved_field(t3_service_tier, existing, "t3_service_tier")
        body = {
            "action": "set",
            "provider": provider,
            "model": model,
            "seat": seat,
            "enabled": enabled,
            "limit": limit,
            "notes": notes,
            "reasoning": resolved_reasoning,
            "brigade_cli": resolved_cli,
            "t3_instance_id": resolved_t3,
            "t3_service_tier": resolved_tier,
            "expected_revision": revision,
        }
        status, payload = _run_with_deadline(
            lambda: _client._post_model_policy_blocking(hub, admin_token, body, timeout=CLOUD_TIMEOUT_SECONDS),
            timeout=CLOUD_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        raise FleetClientError("fleet hub model policy update failed") from exc
    payload = payload if isinstance(payload, dict) else {}
    if status == 200:
        policy = _safe_model_policy(payload.get("policy"))
        return policy if policy else {}
    detail = payload.get("error") if isinstance(payload.get("error"), str) else None
    raise FleetClientError(f"fleet hub model policy update refused: HTTP {status}{': ' + detail if detail else ''}")


def _model_lease_op(
    action: str,
    *,
    seat: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    lease_id: str | None = None,
    holder: str | None = None,
    ttl_seconds: int = 3600,
) -> ModelLeaseDecision:
    lease = lease_id or uuid4().hex
    fence = holder or uuid4().hex
    try:
        config = load_fleet_config()
        if not config["hub_url"]:
            return ModelLeaseDecision(True, "no-hub", lease, fence)
        node = resolve_node_id()
        if not _node_id_is_claimable(node):
            return ModelLeaseDecision(False, "no-identity", lease, fence)
        body: dict[str, Any] = {"action": action, "lease_id": lease, "node_id": node, "holder": fence}
        if action == "acquire":
            body.update(seat=seat, provider=provider, model=model, ttl_seconds=ttl_seconds)
        status, payload = _run_with_deadline(
            lambda: _post_model_policy_blocking(
                config["hub_url"], config["token"], body, timeout=CLOUD_TIMEOUT_SECONDS
            ),
            timeout=CLOUD_TIMEOUT_SECONDS,
        )
    except Exception:
        return ModelLeaseDecision(False, "hub-unavailable", lease, fence)
    if status in (401, 403):
        return ModelLeaseDecision(False, "auth-failed", lease, fence)
    key = "acquired" if action == "acquire" else "released"
    return ModelLeaseDecision(
        status == 200 and isinstance(payload, dict) and payload.get(key) is True,
        "ok" if status == 200 and isinstance(payload, dict) and payload.get(key) is True else "refused",
        lease,
        fence,
    )


def acquire_model_lease(
    seat: str,
    provider: str,
    model: str,
    *,
    lease_id: str | None = None,
    holder: str | None = None,
    ttl_seconds: int = 3600,
) -> ModelLeaseDecision:
    return _model_lease_op(
        "acquire", seat=seat, provider=provider, model=model, lease_id=lease_id, holder=holder, ttl_seconds=ttl_seconds
    )


def release_model_lease(lease_id: str, *, holder: str) -> ModelLeaseDecision:
    return _model_lease_op("release", lease_id=lease_id, holder=holder)
