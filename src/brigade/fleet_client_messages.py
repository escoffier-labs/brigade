"""Fleet Hub message transport and durable offline-send spool."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from time import monotonic
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from . import fleet_client
from . import fleet_hub_messages as _message_limits

_LOG = logging.getLogger("brigade.fleet")
MESSAGE_RESPONSE_LIMIT = 8 * 1024 * 1024
MESSAGE_BATCH_SIZE = 100
MESSAGE_DRAIN_DEADLINE_SECONDS = 12.0


def _spool_path(node_id: str, token: str | None = None) -> Path:
    if token is None:
        token = fleet_client.load_fleet_settings()["node_token"]
    # Scope queued requests to their sender without persisting the bearer token.
    token_scope = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return fleet_client.spool_path(node_id).with_name(f"{node_id}.messages-{token_scope}.jsonl")


def _warn_inactive_spools(node_id: str, active_path: Path) -> None:
    directory = fleet_client.spool_path(node_id).parent
    legacy_path = directory / f"{node_id}.messages.jsonl"
    if legacy_path.is_file() and not legacy_path.is_symlink():
        _LOG.warning("legacy fleet message spool %s has no credential scope; leaving it untouched", legacy_path)
    prefix = f"{node_id}.messages-"
    try:
        stale_count = sum(
            1
            for path in directory.iterdir()
            if path != active_path
            and path.name.startswith(prefix)
            and path.name.endswith(".jsonl")
            and path.is_file()
            and not path.is_symlink()
        )
    except OSError:
        return
    if stale_count:
        _LOG.warning(
            "%d fleet message spool(s) for other credentials remain queued and were not replayed",
            stale_count,
        )


def _request(hub: str, token: str, body: dict[str, Any], *, timeout: float | None = None) -> tuple[int, dict[str, Any]]:
    return _request_with_timeout(hub, token, body, fleet_client.FLUSH_TIMEOUT_SECONDS if timeout is None else timeout)


def _request_with_timeout(hub: str, token: str, body: dict[str, Any], timeout: float) -> tuple[int, dict[str, Any]]:
    fleet_client._require_encrypted_or_loopback_hub(hub)
    request = urllib.request.Request(
        hub.rstrip("/") + "/messages",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )

    def request_blocking() -> tuple[int, dict[str, Any]]:
        try:
            with fleet_client._hub_open(request, timeout=timeout) as response:
                status = response.status
                payload = json.loads(fleet_client._read_bounded_response(response, limit=MESSAGE_RESPONSE_LIMIT))
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read(MESSAGE_RESPONSE_LIMIT + 1))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                payload = {"error": f"HTTP {exc.code}"}
            status = exc.code
        except Exception as exc:
            raise fleet_client.FleetClientError(f"fleet hub messages failed: {type(exc).__name__}") from exc
        if not isinstance(payload, dict):
            raise fleet_client.FleetClientError("fleet hub messages returned an invalid response")
        return status, payload

    try:
        return fleet_client._run_with_deadline(request_blocking, timeout=timeout)
    except fleet_client.FleetClientError:
        raise
    except Exception as exc:
        raise fleet_client.FleetClientError(f"fleet hub messages failed: {type(exc).__name__}") from exc


def _safe_log_text(value: Any) -> str:
    text = str(value)
    if text.isprintable():
        return text
    return "".join(character if character.isprintable() else ascii(character)[1:-1] for character in text)


def _settings() -> tuple[str, str, str]:
    settings = fleet_client.load_fleet_settings()
    if not settings["hub_url"]:
        raise fleet_client.FleetClientError("no fleet hub configured (~/.brigade/fleet.toml [fleet] hub_url)")
    if not settings["node_token"]:
        raise fleet_client.FleetClientError("no fleet node token configured")
    return settings["hub_url"], settings["node_token"], fleet_client.resolve_node_id()


def _online(body: dict[str, Any]) -> dict[str, Any]:
    hub, token, _node_id = _settings()
    status, payload = _request(hub, token, body)
    if not 200 <= status < 300:
        raise fleet_client.FleetClientError(str(payload.get("error") or f"fleet hub returned HTTP {status}"))
    return payload


def _queue_locked(path: Path, message: dict[str, Any], *, dir_fd: int | None = None) -> dict[str, Any]:
    if not fleet_client._spool_append_locked(path, message, dir_fd=dir_fd):
        raise fleet_client.FleetClientError("fleet message could not be persisted to the local spool")
    return {"queued": True, "client_id": message["client_id"]}


def _response_confirms(payload: dict[str, Any], message: dict[str, Any]) -> bool:
    response_message = payload.get("message")
    return isinstance(response_message, dict) and response_message.get("client_id") == message.get("client_id")


def _send_ordered(message: dict[str, Any], hub: str, token: str, node_id: str) -> dict[str, Any]:
    fleet_client._require_encrypted_or_loopback_hub(hub)
    path = _spool_path(node_id, token)
    _warn_inactive_spools(node_id, path)
    try:
        with fleet_client._spool_lock(path) as dir_fd:
            if path.exists() and not path.is_symlink():
                _flush_locked(path, hub, token, max_messages=MESSAGE_BATCH_SIZE, dir_fd=dir_fd)
                if fleet_client._read_spool(path, dir_fd=dir_fd):
                    return _queue_locked(path, message, dir_fd=dir_fd)
            try:
                status, payload = _request(hub, token, message)
            except fleet_client.FleetClientError:
                return _queue_locked(path, message, dir_fd=dir_fd)
            if status in (408, 425, 429) or status >= 500:
                return _queue_locked(path, message, dir_fd=dir_fd)
            if not 200 <= status < 300:
                if 300 <= status < 400:
                    return _queue_locked(path, message, dir_fd=dir_fd)
                if status == 400 and payload.get("code") == "message_timestamp_future":
                    return _queue_locked(path, message, dir_fd=dir_fd)
                if status == 400 and payload.get("code") == "message_recipient_inactive":
                    return _queue_locked(path, message, dir_fd=dir_fd)
                raise fleet_client.FleetClientError(str(payload.get("error") or f"fleet hub returned HTTP {status}"))
            if not _response_confirms(payload, message):
                _LOG.warning("fleet message response did not confirm the submitted request; queueing it")
                return _queue_locked(path, message, dir_fd=dir_fd)
            return payload
    except (KeyboardInterrupt, SystemExit):
        try:
            with fleet_client._spool_lock(path) as dir_fd:
                already_queued = any(
                    entry.get("client_id") == message["client_id"]
                    for entry in fleet_client._read_spool(path, dir_fd=dir_fd)
                )
                if not already_queued:
                    _queue_locked(path, message, dir_fd=dir_fd)
        except BaseException as exc:
            _LOG.warning("interrupted fleet message could not be persisted to the spool: %s", type(exc).__name__)
        raise


def _validate_sqlite_integer(value: Any, field: str, *, allow_zero: bool = False) -> None:
    minimum = 0 if allow_zero else 1
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or value > _message_limits.MAX_SQLITE_INTEGER
    ):
        qualifier = "a non-negative" if allow_zero else "a positive"
        raise fleet_client.FleetClientError(f"{field} must be {qualifier} integer within SQLite's range")


def send(*, recipient: str, subject: str, body: str, reference: str | None, priority: str) -> dict[str, Any]:
    hub, token, node_id = _settings()
    try:
        recipient = _message_limits._text(recipient, "to", 256) or ""
        subject = _message_limits._text(subject, "subject", _message_limits.SUBJECT_LIMIT) or ""
        body = _message_limits._text(body, "body", _message_limits.BODY_LIMIT) or ""
        reference = _message_limits._text(reference, "ref", _message_limits.REF_LIMIT, required=False)
    except _message_limits.MessageError as exc:
        raise fleet_client.FleetClientError(str(exc)) from exc
    if not isinstance(priority, str) or priority not in _message_limits.PRIORITIES:
        raise fleet_client.FleetClientError("priority must be low, normal, high, or urgent")
    message = {
        "action": "send",
        "to": recipient,
        "subject": subject,
        "body": body,
        "ref": reference,
        "priority": priority,
        "client_id": uuid4().hex,
        "enqueued_at": datetime.now(timezone.utc).isoformat(),
    }
    return _send_ordered(message, hub, token, node_id)


def list_messages(*, after: int = 0, unacked: bool = False) -> dict[str, Any]:
    _validate_sqlite_integer(after, "after cursor", allow_zero=True)
    payload = _online({"action": "list", "after": after, "unacked": unacked})
    messages = payload.get("messages")
    next_cursor = payload.get("next_cursor")
    previous_id = after
    valid_ids = isinstance(messages, list)
    if isinstance(messages, list):
        for message in messages:
            message_id = message.get("id") if isinstance(message, dict) else None
            if (
                isinstance(message_id, bool)
                or not isinstance(message_id, int)
                or message_id <= previous_id
                or message_id > _message_limits.MAX_SQLITE_INTEGER
            ):
                valid_ids = False
                break
            previous_id = message_id
    if (
        not valid_ids
        or isinstance(next_cursor, bool)
        or not isinstance(next_cursor, int)
        or next_cursor > _message_limits.MAX_SQLITE_INTEGER
        or next_cursor != previous_id
    ):
        raise fleet_client.FleetClientError("fleet hub returned an invalid message page")
    return payload


def ack(message_id: int) -> dict[str, Any]:
    _validate_sqlite_integer(message_id, "message id")
    payload = _online({"action": "ack", "id": message_id})
    message = payload.get("message")
    response_id = message.get("id") if isinstance(message, dict) else None
    if isinstance(response_id, bool) or not isinstance(response_id, int) or response_id != message_id:
        raise fleet_client.FleetClientError("fleet hub acknowledgement response did not confirm the requested message")
    return payload


def reply(message_id: int, *, body: str, subject: str | None, reference: str | None, priority: str) -> dict[str, Any]:
    hub, token, node_id = _settings()
    _validate_sqlite_integer(message_id, "message id")
    try:
        body = _message_limits._text(body, "body", _message_limits.BODY_LIMIT) or ""
        if subject is not None:
            subject = _message_limits._text(subject, "subject", _message_limits.SUBJECT_LIMIT)
        reference = _message_limits._text(reference, "ref", _message_limits.REF_LIMIT, required=False)
    except _message_limits.MessageError as exc:
        raise fleet_client.FleetClientError(str(exc)) from exc
    if not isinstance(priority, str) or priority not in _message_limits.PRIORITIES:
        raise fleet_client.FleetClientError("priority must be low, normal, high, or urgent")
    message = {
        "action": "reply",
        "id": message_id,
        "body": body,
        "subject": subject,
        "ref": reference,
        "priority": priority,
        "client_id": uuid4().hex,
        "enqueued_at": datetime.now(timezone.utc).isoformat(),
    }
    return _send_ordered(message, hub, token, node_id)


def _principal_admin_request(hub: str, admin_token: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(
        hub.rstrip("/") + "/message-principals",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {admin_token}"},
        method="POST",
    )

    def request_blocking() -> tuple[int, dict[str, Any]]:
        try:
            with fleet_client._hub_open(request, timeout=fleet_client.FLUSH_TIMEOUT_SECONDS) as response:
                status = response.status
                payload = json.loads(fleet_client._read_bounded_response(response, limit=64 * 1024))
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read(64 * 1024 + 1))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                payload = {"error": f"HTTP {exc.code}"}
            status = exc.code
        except Exception as exc:
            raise fleet_client.FleetClientError(f"fleet hub principal management failed: {type(exc).__name__}") from exc
        if not isinstance(payload, dict):
            raise fleet_client.FleetClientError("fleet hub principal management returned an invalid response")
        return status, payload

    try:
        return fleet_client._run_with_deadline(request_blocking, timeout=fleet_client.FLUSH_TIMEOUT_SECONDS)
    except fleet_client.FleetClientError:
        raise
    except Exception as exc:
        raise fleet_client.FleetClientError(f"fleet hub principal management failed: {type(exc).__name__}") from exc


def _principal_enrollment_generation(hub: str, admin_token: str, name: str) -> str | None:
    status, payload = _principal_admin_request(hub, admin_token, {"action": "get", "name": name})
    if status == 404:
        return None
    principal = payload.get("principal")
    if (
        status != 200
        or not isinstance(principal, dict)
        or principal.get("principal_id") != f"agent:{name}"
        or principal.get("name") != name
    ):
        raise fleet_client.FleetClientError(
            str(payload.get("error") or "fleet hub did not confirm the principal generation")
        )
    request_id = principal.get("enrollment_id")
    if request_id is not None and (
        not isinstance(request_id, str)
        or not 16 <= len(request_id) <= 128
        or not request_id.isascii()
        or any(not (character.isalnum() or character in "_-") for character in request_id)
    ):
        raise fleet_client.FleetClientError("fleet hub returned an invalid principal enrollment generation")
    return request_id


def manage_principal(
    action: str,
    *,
    name: str,
    label: str | None = None,
    _defer_finalize: bool = False,
    _deferred_request_ids: list[str] | None = None,
) -> dict[str, Any]:
    if _defer_finalize and _deferred_request_ids is None:
        raise fleet_client.FleetClientError("deferred principal enrollment requires a request ID holder")
    settings = fleet_client.load_fleet_settings()
    if not settings["hub_url"]:
        raise fleet_client.FleetClientError("no fleet hub configured (~/.brigade/fleet.toml [fleet] hub_url)")
    if not settings["admin_token"]:
        raise fleet_client.FleetClientError("no fleet admin token configured")
    fleet_client._require_encrypted_or_loopback_hub(settings["hub_url"])
    body: dict[str, Any] = {"action": action, "name": name}
    pending_path: Path | None = None
    revoke_pending_request_id: str | None = None
    generation = _principal_enrollment_generation(settings["hub_url"], settings["admin_token"], name)
    if action == "add":
        body.update(_pending_principal_request(name, settings["admin_token"], label, expected_enrollment_id=generation))
        body["token"] = hmac.new(
            settings["admin_token"].encode("utf-8"),
            f"brigade-message-principal:{name}:{body['request_id']}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        pending_path = _principal_pending_path(name, settings["admin_token"])
    elif action == "revoke":
        body["expected_enrollment_id"] = generation
        pending_path = _principal_pending_path(name, settings["admin_token"])
        revoke_pending_request_id = _pending_principal_request_id(pending_path, name)
    elif label is not None:
        body["label"] = label
    status, payload = _principal_admin_request(settings["hub_url"], settings["admin_token"], body)
    if not 200 <= status < 300:
        if (
            action == "add"
            and pending_path is not None
            and status == 409
            and payload.get("code") in ("principal_enrollment_revoked", "principal_enrollment_changed")
        ):
            _clear_principal_request(pending_path, request_id=body["request_id"])
        elif pending_path is not None and 400 <= status < 500 and status not in (408, 409, 425, 429):
            if action == "add":
                _clear_principal_request(pending_path, request_id=body["request_id"])
        message = str(payload.get("error") or f"fleet hub returned HTTP {status}")
        if action == "add" and payload.get("code") in ("principal_enrollment_revoked", "principal_enrollment_changed"):
            message += "; run principals add again to request a new enrollment generation"
        raise fleet_client.FleetClientError(message)
    if action == "add" and pending_path is not None:
        principal = payload.get("principal")
        returned_token = principal.get("token") if isinstance(principal, dict) else None
        if (
            not isinstance(principal, dict)
            or principal.get("principal_id") != f"agent:{name}"
            or principal.get("name") != name
            or not isinstance(returned_token, str)
            or len(returned_token) != len(body["token"])
            or not returned_token.isascii()
            or any(char not in "0123456789abcdef" for char in returned_token)
            or not hmac.compare_digest(returned_token, body["token"])
        ):
            raise fleet_client.FleetClientError("fleet hub did not confirm the enrolled principal and token")
        if _defer_finalize:
            if _deferred_request_ids is not None:
                _deferred_request_ids.append(body["request_id"])
        else:
            _clear_principal_request(pending_path, request_id=body["request_id"])
    elif action == "revoke":
        principal = payload.get("principal")
        if (
            not isinstance(principal, dict)
            or principal.get("principal_id") != f"agent:{name}"
            or principal.get("name") != name
        ):
            raise fleet_client.FleetClientError("fleet hub did not confirm the revoked principal")
        if revoke_pending_request_id is not None and pending_path is not None:
            _clear_principal_request(pending_path, request_id=revoke_pending_request_id)
    return payload


def finalize_principal_add(name: str, *, request_id: str) -> None:
    """Clear enrollment retry state after the caller has safely delivered the token."""
    settings = fleet_client.load_fleet_settings()
    if not settings["admin_token"]:
        raise fleet_client.FleetClientError("no fleet admin token configured")
    _clear_principal_request(_principal_pending_path(name, settings["admin_token"]), request_id=request_id)


def _principal_pending_path(name: str, admin_token: str) -> Path:
    token_scope = hashlib.sha256(admin_token.encode("utf-8")).hexdigest()
    name_scope = hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]
    return fleet_client.brigade_home() / fleet_client.SPOOL_DIRNAME / f"principal-{name_scope}-{token_scope}.jsonl"


def _pending_principal_request(
    name: str,
    admin_token: str,
    label: str | None,
    *,
    expected_enrollment_id: str | None = None,
) -> dict[str, Any]:
    path = _principal_pending_path(name, admin_token)
    with fleet_client._spool_lock(path) as dir_fd:
        pending = fleet_client._read_spool(path, dir_fd=dir_fd)
    if not pending:
        # Migrate requests written before retry state stopped depending on the
        # mutable node identity. Never nest spool locks: they share a process lock.
        name_scope = hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]
        token_scope = hashlib.sha256(admin_token.encode("utf-8")).hexdigest()
        for legacy_path in sorted(path.parent.glob(f"*.principal-{name_scope}-{token_scope}.jsonl")):
            if legacy_path == path or legacy_path.is_symlink() or not legacy_path.is_file():
                continue
            with fleet_client._spool_lock(legacy_path) as legacy_fd:
                legacy_pending = fleet_client._read_spool(legacy_path, dir_fd=legacy_fd)
            if legacy_pending:
                legacy_request_id = next(
                    (
                        item.get("request_id")
                        for item in legacy_pending
                        if item.get("name") == name and isinstance(item.get("request_id"), str)
                    ),
                    None,
                )
                with fleet_client._spool_lock(path) as dir_fd:
                    pending = fleet_client._read_spool(path, dir_fd=dir_fd)
                    if not pending:
                        pending = legacy_pending
                        fleet_client._write_spool_atomic(path, pending, dir_fd=dir_fd)
                if pending == legacy_pending and isinstance(legacy_request_id, str):
                    _clear_principal_request(legacy_path, request_id=legacy_request_id)
                break
    if not pending:
        with fleet_client._spool_lock(path) as dir_fd:
            pending = fleet_client._read_spool(path, dir_fd=dir_fd)
            if not pending:
                new_request: dict[str, Any] = {
                    "name": name,
                    "request_id": uuid4().hex,
                    "expected_enrollment_id": expected_enrollment_id,
                }
                if label is not None:
                    new_request["label"] = label
                fleet_client._write_spool_atomic(path, [new_request], dir_fd=dir_fd)
                return new_request
    for item in pending:
        request_id = item.get("request_id")
        if item.get("name") == name and isinstance(request_id, str) and request_id:
            pending_request: dict[str, Any] = {"name": name, "request_id": request_id}
            expected = item.get("expected_enrollment_id")
            if expected is None or isinstance(expected, str):
                pending_request["expected_enrollment_id"] = expected
            if isinstance(item.get("label"), str):
                pending_request["label"] = item["label"]
            return pending_request
    raise fleet_client.FleetClientError("could not preserve pending principal enrollment request")


def _pending_principal_request_id(path: Path, name: str) -> str | None:
    with fleet_client._spool_lock(path) as dir_fd:
        pending = fleet_client._read_spool(path, dir_fd=dir_fd)
    for item in pending:
        request_id = item.get("request_id")
        if item.get("name") == name and isinstance(request_id, str) and request_id:
            return request_id
    return None


def _clear_principal_request(path: Path, *, request_id: str) -> bool:
    with fleet_client._spool_lock(path) as dir_fd:
        pending = fleet_client._read_spool(path, dir_fd=dir_fd)
        remaining = [item for item in pending if item.get("request_id") != request_id]
        if len(remaining) == len(pending):
            return False
        fleet_client._write_spool_atomic(path, remaining, dir_fd=dir_fd)
        return True


def flush(node_id: str, *, hub_url: str | None = None, token: str | None = None) -> int:
    config = fleet_client.load_fleet_config()
    hub = hub_url or config["hub_url"]
    auth = token or fleet_client.load_fleet_settings()["node_token"]
    path = _spool_path(node_id, auth)
    _warn_inactive_spools(node_id, path)
    if not hub or not path.is_file():
        return 0
    with fleet_client._spool_lock(path) as dir_fd:
        return _flush_locked(path, hub, auth, dir_fd=dir_fd)


def _flush_locked(
    path: Path, hub: str, token: str, *, max_messages: int | None = None, dir_fd: int | None = None
) -> int:
    messages = fleet_client._read_spool(path, dir_fd=dir_fd)
    deadline = monotonic() + MESSAGE_DRAIN_DEADLINE_SECONDS
    delivered = 0
    index = 0
    for message in messages:
        if max_messages is not None and index >= max_messages:
            fleet_client._write_spool_atomic(path, messages[index:], dir_fd=dir_fd)
            return delivered
        if _expired(message):
            _LOG.warning("fleet message spool %s: dropping expired offline message", path)
            index += 1
            continue
        remaining = deadline - monotonic()
        if remaining <= 0:
            fleet_client._write_spool_atomic(path, messages[index:], dir_fd=dir_fd)
            return delivered
        try:
            timeout = min(fleet_client.FLUSH_TIMEOUT_SECONDS, remaining)
            if timeout == fleet_client.FLUSH_TIMEOUT_SECONDS:
                status, payload = _request(hub, token, message)
            else:
                status, payload = _request(hub, token, message, timeout=timeout)
        except fleet_client.FleetClientError:
            fleet_client._write_spool_atomic(path, messages[index:], dir_fd=dir_fd)
            return delivered
        if (
            300 <= status < 400
            or status >= 500
            or status in (401, 408, 425, 429)
            or (status == 400 and payload.get("code") in ("message_timestamp_future", "message_recipient_inactive"))
        ):
            fleet_client._write_spool_atomic(path, messages[index:], dir_fd=dir_fd)
            return delivered
        if not 200 <= status < 300 and status >= 400:
            _LOG.warning(
                "fleet message spool %s: dropping rejected message: %s", path, _safe_log_text(payload.get("error"))
            )
        elif 200 <= status < 300:
            if not _response_confirms(payload, message):
                _LOG.warning("fleet message spool %s: response did not confirm the queued message", path)
                fleet_client._write_spool_atomic(path, messages[index:], dir_fd=dir_fd)
                return delivered
            delivered += 1
        else:
            fleet_client._write_spool_atomic(path, messages[index:], dir_fd=dir_fd)
            return delivered
        index += 1
    fleet_client._write_spool_atomic(path, [], dir_fd=dir_fd)
    return delivered


def _expired(message: dict[str, Any]) -> bool:
    value = message.get("enqueued_at")
    if not isinstance(value, str):
        return True
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return True
    if stamp.tzinfo is None:
        return True
    return stamp < datetime.now(timezone.utc) - timedelta(days=_message_limits.RETENTION_DAYS)
