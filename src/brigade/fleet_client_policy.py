"""Bounded Fleet Hub /policy client. Tokens are never returned or logged."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Mapping

from . import fleet_client as _client

FleetClientError = _client.FleetClientError
POLICY_TIMEOUT_SECONDS = 2.5
MAX_POLICY_RESPONSE_BYTES = 512 * 1024
CLIENT_ERROR_CODES = frozenset(
    {
        "auth-failed",
        "policy-unsupported",
        "revision-conflict",
        "network",
        "invalid-request",
        "enrollment-required",
        "seat-unresolved",
        "seat-disabled",
        "training-disallowed",
        "ambiguous-seat",
        "retired",
        "policy-blocked",
        "quota-forbidden",
    }
)


class FleetPolicyClientError(FleetClientError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _scrub(value: str) -> str:
    from .fleet_hub_policy_api import scrub_text

    return scrub_text(value)


def _classify(status: int, payload: Any, fallback: str) -> FleetPolicyClientError:
    code = None
    message = fallback
    if isinstance(payload, Mapping):
        error = payload.get("error")
        if isinstance(error, Mapping):
            raw_code = error.get("code")
            if isinstance(raw_code, str) and raw_code in CLIENT_ERROR_CODES:
                code = raw_code
                raw_message = error.get("message")
                if isinstance(raw_message, str) and raw_message:
                    message = raw_message
    if code is None:
        if status in {401, 403}:
            code = "auth-failed"
        elif status == 409:
            code = "revision-conflict"
        elif status == 404:
            code = "policy-unsupported"
        elif status >= 500 or status in {408, 429}:
            code = "network"
        else:
            code = "invalid-request"
        message = fallback
    return FleetPolicyClientError(code, _scrub(message))


def _bounded_json(response: Any) -> Any:
    raw = response.read(MAX_POLICY_RESPONSE_BYTES + 1)
    if len(raw) > MAX_POLICY_RESPONSE_BYTES:
        raise FleetPolicyClientError("network", "fleet hub policy response exceeded the size limit")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FleetPolicyClientError("network", "fleet hub policy response was not valid JSON") from exc


def _request(
    method: str,
    path: str,
    *,
    token: str,
    hub_url: str,
    body: Mapping[str, Any] | None = None,
    timeout: float = POLICY_TIMEOUT_SECONDS,
) -> tuple[int, Any]:
    _client._require_encrypted_or_loopback_hub(hub_url)
    headers = {"Authorization": f"Bearer {token}"}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(hub_url.rstrip("/") + path, data=data, headers=headers, method=method)

    def _run() -> tuple[int, Any]:
        try:
            with _client._hub_open(request, timeout=timeout) as response:
                return response.status, _bounded_json(response)
        except urllib.error.HTTPError as exc:
            try:
                payload = _bounded_json(exc)
            except FleetPolicyClientError:
                payload = {}
            return exc.code, payload

    try:
        return _client._run_with_deadline(_run, timeout=timeout)
    except TimeoutError as exc:
        raise FleetPolicyClientError("network", str(exc)) from exc
    except FleetPolicyClientError:
        raise
    except urllib.error.URLError as exc:
        raise FleetPolicyClientError("network", f"fleet hub policy request failed: {exc}") from exc
    except OSError as exc:
        raise FleetPolicyClientError("network", f"fleet hub policy request failed: {exc}") from exc


def _hub_and_token(*, admin: bool) -> tuple[str, str]:
    settings = _client.load_fleet_settings()
    hub = settings["hub_url"]
    if not hub:
        raise FleetPolicyClientError("network", "no fleet hub configured (~/.brigade/fleet.toml [fleet] hub_url)")
    if admin:
        token = settings["admin_token"]
        if not token:
            raise FleetPolicyClientError(
                "auth-failed",
                "no fleet admin token configured (~/.brigade/fleet.toml [fleet] token_file or BRIGADE_FLEET_TOKEN)",
            )
        return hub, token
    config = _client.load_fleet_config()
    token = config["token"]
    if not token:
        raise FleetPolicyClientError("auth-failed", "no fleet node or admin token configured")
    return hub, token


def _checked(status: int, payload: Any) -> dict[str, Any]:
    if status == 200 and isinstance(payload, dict):
        return payload
    raise _classify(status, payload, f"hub returned HTTP {status}")


def show_policy() -> dict[str, Any]:
    hub, token = _hub_and_token(admin=False)
    status, payload = _request("GET", "/policy", token=token, hub_url=hub)
    return _checked(status, payload)


def history_policy(*, limit: int = 50) -> dict[str, Any]:
    hub, token = _hub_and_token(admin=False)
    status, payload = _request("GET", f"/policy?history=1&limit={int(limit)}", token=token, hub_url=hub)
    return _checked(status, payload)


def post_policy(body: Mapping[str, Any], *, admin: bool) -> dict[str, Any]:
    hub, token = _hub_and_token(admin=admin)
    status, payload = _request("POST", "/policy", token=token, hub_url=hub, body=body)
    return _checked(status, payload)


def preview_policy(document: Mapping[str, Any], *, expected_version: int, reason: str) -> dict[str, Any]:
    return post_policy(
        {"action": "preview", "document": document, "expected_version": expected_version, "reason": reason},
        admin=True,
    )


def save_policy(document: Mapping[str, Any], *, expected_version: int, reason: str) -> dict[str, Any]:
    return post_policy(
        {"action": "save", "document": document, "expected_version": expected_version, "reason": reason},
        admin=True,
    )


def rollback_policy(*, revision: int, expected_version: int, reason: str) -> dict[str, Any]:
    return post_policy(
        {"action": "rollback", "revision": revision, "expected_version": expected_version, "reason": reason},
        admin=True,
    )


def resolve_policy(
    *,
    consumer: str,
    repo: str | None,
    session_id: str,
    origin: str,
    overrides: Mapping[str, Any] | None = None,
    override_reason: str | None = None,
    decision_id: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "action": "resolve",
        "consumer": consumer,
        "repo_identity": repo,
        "session_id": session_id,
        "origin": origin,
    }
    if overrides:
        body["overrides"] = dict(overrides)
    if override_reason:
        body["override_reason"] = override_reason
    if decision_id:
        body["decision_id"] = decision_id
    return post_policy(body, admin=False)


def prepare_session(
    *,
    consumer: str,
    repo: str | None,
    session_id: str,
    origin: str,
    provider: str,
    model: str,
    instance_id: str,
    reasoning: str | None = None,
    overrides: Mapping[str, Any] | None = None,
    override_reason: str | None = None,
    decision_id: str | None = None,
    delegation_id: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "action": "prepare",
        "consumer": consumer,
        "repo_identity": repo,
        "session_id": session_id,
        "origin": origin,
        "provider": provider,
        "model": model,
        "instance_id": instance_id,
    }
    if reasoning:
        body["reasoning"] = reasoning
    if overrides:
        body["overrides"] = dict(overrides)
    if override_reason:
        body["override_reason"] = override_reason
    if decision_id:
        body["decision_id"] = decision_id
    if delegation_id:
        body["delegation_id"] = delegation_id
    return post_policy(body, admin=False)


def create_delegation(
    *,
    decision_id: str,
    source_revision: str,
    parent_request_id: str,
    parent_metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "action": "delegation-create",
        "decision_id": decision_id,
        "source_revision": source_revision,
        "parent_request_id": parent_request_id,
    }
    if parent_metadata is not None:
        body["parent_metadata"] = dict(parent_metadata)
    return post_policy(body, admin=False)


def show_delegation(*, delegation_id: str) -> dict[str, Any]:
    body: dict[str, Any] = {
        "action": "delegation-show",
        "delegation_id": delegation_id,
    }
    return post_policy(body, admin=False)


def acknowledge_session(
    *,
    session_id: str,
    consumer: str,
    repo: str | None,
    version: int,
    digest: str,
    status: str | None = None,
    reason: str | None = None,
    context_hash: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "action": "acknowledge",
        "session_id": session_id,
        "consumer": consumer,
        "repo_identity": repo,
        "version": version,
        "digest": digest,
    }
    if status:
        body["status"] = status
    if reason:
        body["reason"] = reason
    if context_hash:
        body["context_hash"] = context_hash
    return post_policy(body, admin=False)


def route_work(
    *,
    consumer: str,
    repo: str | None,
    session_id: str,
    origin: str,
    workload: str,
    machine: str | None = None,
    seat: str | None = None,
    override_reason: str | None = None,
    work_id: str | None = None,
    decision_id: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "action": "route",
        "consumer": consumer,
        "repo_identity": repo,
        "session_id": session_id,
        "origin": origin,
        "workload": workload,
    }
    if machine:
        body["machine"] = machine
    if seat:
        body["seat"] = seat
    if override_reason:
        body["override_reason"] = override_reason
    if work_id:
        body["work_id"] = work_id
    if decision_id:
        body["decision_id"] = decision_id
    return post_policy(body, admin=False)


def renew_reservation(*, reservation_id: str, decision_id: str, session_id: str) -> dict[str, Any]:
    return post_policy(
        {
            "action": "reservation-renew",
            "reservation_id": reservation_id,
            "decision_id": decision_id,
            "session_id": session_id,
        },
        admin=False,
    )


def release_reservation(*, reservation_id: str, decision_id: str, session_id: str) -> dict[str, Any]:
    return post_policy(
        {
            "action": "reservation-release",
            "reservation_id": reservation_id,
            "decision_id": decision_id,
            "session_id": session_id,
        },
        admin=False,
    )


def observe_telemetry(body: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(body)
    payload["action"] = "telemetry-observe"
    return post_policy(payload, admin=False)


def ingest_quota(body: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(body)
    payload["action"] = "quota-ingest"
    return post_policy(payload, admin=False)


def ingest_inventory(body: Mapping[str, Any], *, admin: bool = False) -> dict[str, Any]:
    payload = dict(body)
    payload["action"] = "inventory-ingest"
    return post_policy(payload, admin=admin)


def status_policy() -> dict[str, Any]:
    hub, token = _hub_and_token(admin=False)
    status, payload = _request("GET", "/policy/status", token=token, hub_url=hub)
    return _checked(status, payload)


def status_inventory() -> dict[str, Any]:
    hub, token = _hub_and_token(admin=False)
    status, payload = _request("GET", "/policy/inventory", token=token, hub_url=hub)
    return _checked(status, payload)
