"""Fleet Hub message transport and durable offline-send spool."""

from __future__ import annotations

import hashlib
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
    if status >= 400:
        raise fleet_client.FleetClientError(str(payload.get("error") or f"fleet hub returned HTTP {status}"))
    return payload


def _queue_locked(path: Path, message: dict[str, Any], *, dir_fd: int | None = None) -> dict[str, Any]:
    if not fleet_client._spool_append_locked(path, message, dir_fd=dir_fd):
        raise fleet_client.FleetClientError("fleet message could not be persisted to the local spool")
    return {"queued": True, "client_id": message["client_id"]}


def _send_ordered(message: dict[str, Any], hub: str, token: str, node_id: str) -> dict[str, Any]:
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
            if status in (408, 429) or status >= 500:
                return _queue_locked(path, message, dir_fd=dir_fd)
            if not 200 <= status < 300:
                if 300 <= status < 400:
                    return _queue_locked(path, message, dir_fd=dir_fd)
                raise fleet_client.FleetClientError(str(payload.get("error") or f"fleet hub returned HTTP {status}"))
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
    return _online({"action": "list", "after": after, "unacked": unacked})


def ack(message_id: int) -> dict[str, Any]:
    _validate_sqlite_integer(message_id, "message id")
    return _online({"action": "ack", "id": message_id})


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


def manage_principal(action: str, *, name: str, label: str | None = None) -> dict[str, Any]:
    settings = fleet_client.load_fleet_settings()
    if not settings["hub_url"]:
        raise fleet_client.FleetClientError("no fleet hub configured (~/.brigade/fleet.toml [fleet] hub_url)")
    if not settings["admin_token"]:
        raise fleet_client.FleetClientError("no fleet admin token configured")
    body: dict[str, Any] = {"action": action, "name": name}
    if label is not None:
        body["label"] = label
    request = urllib.request.Request(
        settings["hub_url"].rstrip("/") + "/message-principals",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {settings['admin_token']}"},
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
        status, payload = fleet_client._run_with_deadline(request_blocking, timeout=fleet_client.FLUSH_TIMEOUT_SECONDS)
    except fleet_client.FleetClientError:
        raise
    except Exception as exc:
        raise fleet_client.FleetClientError(f"fleet hub principal management failed: {type(exc).__name__}") from exc
    if status >= 400:
        raise fleet_client.FleetClientError(str(payload.get("error") or f"fleet hub returned HTTP {status}"))
    return payload


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
        if 300 <= status < 400 or status >= 500 or status in (401, 403, 408, 429):
            fleet_client._write_spool_atomic(path, messages[index:], dir_fd=dir_fd)
            return delivered
        if not 200 <= status < 300 and status >= 400:
            _LOG.warning(
                "fleet message spool %s: dropping rejected message: %s", path, _safe_log_text(payload.get("error"))
            )
        elif 200 <= status < 300:
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
