"""Fleet Hub message transport and durable offline-send spool."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import logging
import os
import re
import stat
from time import monotonic
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from . import fleet_client
from .fleet_client_message_credentials import (
    FleetMessageHubError,
    MessageAuthority,
    MessageAuthorityError,
    MessageNotConfiguredError,
    message_scope,
)
from . import fleet_hub_messages as _message_limits

_LOG = logging.getLogger("brigade.fleet")
MESSAGE_RESPONSE_LIMIT = 8 * 1024 * 1024
MESSAGE_BATCH_SIZE = 100
MESSAGE_DRAIN_DEADLINE_SECONDS = 12.0
_NOT_PROVIDED = object()


class _UnauthorizedError(FleetMessageHubError):
    """A sender authentication failure cannot safely queue work in its scoped spool."""


def _spool_path(node_id: str, token: str, *, hub_url: str) -> Path:
    # The versioned filename proves origin; older token-only files are never adopted.
    return fleet_client.spool_path(node_id).with_name(f"messages-v1-{message_scope(hub_url, token)}.jsonl")


def has_pending(authority: MessageAuthority) -> bool:
    """Inspect only the captured authority's queue metadata, without a lock or settings read."""
    path = _spool_path(authority.node_id, authority.token, hub_url=authority.hub)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        raise fleet_client.FleetClientError("fleet message spool could not be inspected") from None
    return stat.S_ISREG(info.st_mode) and info.st_size > 0


def _warn_inactive_spools(node_id: str, active_path: Path) -> None:
    directory = fleet_client.spool_path(node_id).parent
    prefix = "messages-"
    try:
        if any(
            path.is_file() and not path.is_symlink() and path.stat().st_size
            for path in directory.glob("*.messages.jsonl")
        ):
            _LOG.warning("legacy fleet message spool has no credential scope or proven endpoint; leaving it untouched")
        stale_count = sum(
            1
            for path in directory.iterdir()
            if path != active_path
            and path.name.startswith(prefix)
            and path.name.endswith(".jsonl")
            and path.is_file()
            and not path.is_symlink()
            and path.stat().st_size > 0
        )
    except OSError:
        return
    if stale_count:
        _LOG.warning(
            "%d fleet message spool(s) for other credentials or unproven endpoints remain queued and were not replayed",
            stale_count,
        )


def _request(hub: str, token: str, body: dict[str, Any], *, timeout: float | None = None) -> tuple[int, dict[str, Any]]:
    return _request_with_timeout(hub, token, body, fleet_client.FLUSH_TIMEOUT_SECONDS if timeout is None else timeout)


def _request_with_timeout(hub: str, token: str, body: dict[str, Any], timeout: float) -> tuple[int, dict[str, Any]]:
    fleet_client._require_encrypted_or_loopback_hub(hub)
    wire_body = {key: value for key, value in body.items() if not key.startswith("_")}
    request = urllib.request.Request(
        hub.rstrip("/") + "/messages",
        data=json.dumps(wire_body).encode("utf-8"),
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
            raise FleetMessageHubError(
                f"fleet hub messages failed: {type(exc).__name__}", code="hub_unavailable"
            ) from exc
        if not isinstance(payload, dict):
            raise fleet_client.FleetClientError("fleet hub messages returned an invalid response")
        return status, payload

    try:
        return fleet_client._run_with_deadline(request_blocking, timeout=timeout)
    except FleetMessageHubError:
        raise
    except fleet_client.FleetClientError:
        raise FleetMessageHubError("fleet hub request failed", code="hub_unavailable") from None
    except Exception as exc:
        raise FleetMessageHubError(f"fleet hub messages failed: {type(exc).__name__}", code="hub_unavailable") from exc


def _safe_log_text(value: Any) -> str:
    text = str(value)
    if text.isprintable():
        return text
    return "".join(character if character.isprintable() else ascii(character)[1:-1] for character in text)


def _spool_exists_locked(path: Path, dir_fd: int | None) -> bool:
    """Inspect the spool through its locked directory descriptor when available."""
    if dir_fd is None:  # pragma: no cover - Windows
        return path.exists() and not path.is_symlink()
    try:
        info = os.stat(path.name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise fleet_client.FleetClientError("fleet message spool could not be inspected") from exc
    if not stat.S_ISREG(info.st_mode):
        raise fleet_client.FleetClientError("fleet message could not be persisted: spool is not a regular file")
    return True


def _request_timeout(timeout: float | None) -> float | None:
    if timeout is None:
        return None
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise fleet_client.FleetClientError("message request timeout must be a positive number of seconds")
    return float(timeout)


def _authority(
    authority: MessageAuthority | None, token_file: str | None, expected_scope: str | None
) -> MessageAuthority:
    # Public entries capture only when their caller has not already captured.
    authority = MessageAuthority.capture(token_file) if authority is None else authority
    if expected_scope is not None and not hmac.compare_digest(authority.scope, expected_scope):
        raise MessageAuthorityError("message credential changed", code="token_scope_changed")
    authority.validate()
    return authority


def _transport(
    authority: MessageAuthority, body: dict[str, Any], *, timeout: float | None = None
) -> tuple[int, dict[str, Any]]:
    authority.validate()
    fleet_client._require_encrypted_or_loopback_hub(authority.hub)
    try:
        if timeout is None:
            return _request(authority.hub, authority.token, body)
        return _request(authority.hub, authority.token, body, timeout=timeout)
    except MessageAuthorityError:
        raise
    except FleetMessageHubError as error:
        authority.validate()
        raise FleetMessageHubError(
            _rejection(error.status, {"code": error.code}) if error.status is not None else "fleet hub request failed",
            status=error.status,
            code=error.code,
        ) from None
    except (fleet_client.FleetClientError, OSError, ValueError):
        authority.validate()
        raise FleetMessageHubError("fleet hub request failed", code="hub_unavailable") from None


def _rejection(status: int, payload: dict[str, Any]) -> str:
    """Closed protocol descriptions, never arbitrary upstream error text."""
    descriptions = {
        "message_sender_inactive": "sender is not an active hub principal",
        "message_not_found": "message not found",
        "principal_enrollment_changed": "principal generation changed",
        "principal_enrollment_revoked": "principal generation changed",
        "paging_cursor_expired": "paging cursor expired; restart from the first page",
    }
    code = payload.get("code")
    if isinstance(code, str) and code in descriptions:
        return descriptions[code]
    # Older Hubs omit codes for these fixed protocol errors.
    legacy = {"message principal already exists", "invalid label", "principal generation changed"}
    error = payload.get("error")
    if isinstance(error, str) and error in legacy:
        return next(text for text in legacy if text == error)
    if status == 401:
        return "unauthorized"
    if status == 404:
        return "message not found"
    if status == 409:
        return "message request conflict"
    if 300 <= status < 400:
        return "fleet hub request redirected"
    return "fleet hub message rejected"


def _online(body: dict[str, Any], *, authority: MessageAuthority, timeout: float | None = None) -> dict[str, Any]:
    try:
        status, payload = _transport(authority, body, timeout=_request_timeout(timeout))
    finally:
        authority.validate()
    if (
        status == 400
        and body.get("action") in {"who", "claims"}
        and ("limit" in body or "cursor" in body)
        and payload.get("code") is None
        and payload.get("error") == "invalid station read arguments"
    ):
        # Older hubs reject paging keys before reading either directory.
        # The retry carries no paging keys, which also bounds it to one.
        if "cursor" in body:
            # An unpaged retry would return page one as if it were this continuation,
            # duplicating rows when the hub was rolled back mid-walk. Restart instead.
            raise FleetMessageHubError(
                _rejection(400, {"code": "paging_cursor_expired"}), status=400, code="paging_cursor_expired"
            )
        legacy = _online(
            {key: value for key, value in body.items() if key != "limit"},
            authority=authority,
            timeout=timeout,
        )
        if legacy.get("truncated") is True:
            raise FleetMessageHubError(
                "Fleet hub does not support directory continuation.", code="paging_continuation_unsupported"
            )
        return legacy
    if not 200 <= status < 300:
        raise FleetMessageHubError(
            _rejection(status, payload),
            status=status,
            code=payload.get("code"),
        )
    return payload


def _queued_retry(path: Path, message: dict[str, Any], *, dir_fd: int | None) -> dict[str, Any] | None:
    def payload(entry: dict[str, Any]) -> dict[str, Any]:
        # Enqueue time and resolved recipient hints are local delivery state.
        return {
            key: value
            for key, value in entry.items()
            if not key.startswith("_")
            and key != "enqueued_at"
            and value is not None
            and not (key == "kind" and value == "message")
        }

    matching = [
        entry
        for entry in fleet_client._read_spool(path, dir_fd=dir_fd, strict=True)
        if entry.get("client_id") == message["client_id"]
    ]
    if any(payload(entry) != payload(message) for entry in matching):
        raise FleetMessageHubError("client_id is already queued for a different message", code="spool_conflict")
    return {"queued": True, "client_id": message["client_id"]} if matching else None


def _queue_locked(path: Path, message: dict[str, Any], *, dir_fd: int | None = None) -> dict[str, Any]:
    existing = _queued_retry(path, message, dir_fd=dir_fd)
    if existing is not None:
        return existing
    if not fleet_client._spool_append_locked(path, message, dir_fd=dir_fd, evict_oldest=False):
        try:
            pending = fleet_client._read_spool(path, dir_fd=dir_fd, strict=True)
        except fleet_client.FleetClientError as exc:
            raise fleet_client.FleetClientError(
                f"fleet message persistence is ambiguous for client_id {message['client_id']}; "
                "do not resubmit until the local spool can be checked"
            ) from exc
        if any(entry.get("client_id") == message["client_id"] for entry in pending):
            raise fleet_client.FleetClientError(
                f"fleet message persistence is ambiguous for client_id {message['client_id']}; "
                "do not resubmit until the local spool is flushed"
            )
        raise fleet_client.FleetClientError("fleet message could not be persisted to the local spool")
    return {"queued": True, "client_id": message["client_id"]}


def _require_spool_capacity(path: Path, message: dict[str, Any], *, dir_fd: int | None) -> None:
    try:
        info = os.stat(path.name, dir_fd=dir_fd, follow_symlinks=False) if dir_fd is not None else path.lstat()
    except FileNotFoundError:
        current = 0
    except OSError as exc:
        raise fleet_client.FleetClientError("fleet message spool capacity could not be checked") from exc
    else:
        if not stat.S_ISREG(info.st_mode):
            raise fleet_client.FleetClientError("fleet message spool is not a regular file")
        current = info.st_size
    if current + len((fleet_client._encode(message) + "\n").encode("utf-8")) > fleet_client.MAX_SPOOL_BYTES:
        raise fleet_client.FleetClientError("fleet message spool is full; refusing delivery without retry capacity")


def _response_confirms(payload: dict[str, Any], message: dict[str, Any]) -> bool:
    response_message = payload.get("message")
    return isinstance(response_message, dict) and response_message.get("client_id") == message.get("client_id")


def _remove_queued_locked(path: Path, message: dict[str, Any], *, dir_fd: int | None) -> None:
    queued = fleet_client._read_spool(path, dir_fd=dir_fd, strict=True)
    for index, entry in enumerate(queued):
        if entry == message:
            del queued[index]
            fleet_client._write_spool_atomic(path, queued, dir_fd=dir_fd)
            break


def _send_ordered(
    message: dict[str, Any],
    authority: MessageAuthority,
    *,
    lock_timeout: float | None = None,
    warn_inactive: bool = True,
) -> dict[str, Any]:
    fleet_client._validate_spool_lock_timeout(lock_timeout)
    hub, token, node_id = authority.hub, authority.token, authority.node_id
    fleet_client._require_encrypted_or_loopback_hub(hub)
    path = _spool_path(node_id, token, hub_url=hub)
    if warn_inactive:
        _warn_inactive_spools(node_id, path)
    try:
        with fleet_client._spool_lock(path, timeout=lock_timeout) as dir_fd:
            authority.validate()
            if _spool_exists_locked(path, dir_fd):
                existing = _queued_retry(path, message, dir_fd=dir_fd)
                if existing is not None:
                    return existing
                drain_started = monotonic()
                batch_limit = MESSAGE_BATCH_SIZE - 1 if message.get("action") == "reply" else MESSAGE_BATCH_SIZE
                _delivered, globally_blocked = _drain_locked(path, authority, max_messages=batch_limit, dir_fd=dir_fd)
                pending = fleet_client._read_spool(path, dir_fd=dir_fd, strict=True)
                recipient = message.get("_recipient") or message.get("to")
                if any(queued.get("action") == "reply" and _message_recipient(queued) is None for queued in pending):
                    return _queue_locked(path, message, dir_fd=dir_fd)
                if pending and recipient is None and message.get("action") == "reply":
                    remaining = MESSAGE_DRAIN_DEADLINE_SECONDS - (monotonic() - drain_started)
                    if remaining <= 0:
                        return _queue_locked(path, message, dir_fd=dir_fd)
                    try:
                        recipient = _reply_recipient(
                            authority,
                            int(message["id"]),
                            timeout=min(fleet_client.FLUSH_TIMEOUT_SECONDS, remaining),
                        )
                    except (_UnauthorizedError, MessageAuthorityError):
                        raise
                    except (KeyError, TypeError, ValueError, fleet_client.FleetClientError):
                        return _queue_locked(path, message, dir_fd=dir_fd)
                    if recipient is None:
                        raise fleet_client.FleetClientError("fleet hub did not return the replied-to message")
                    message["_recipient"] = recipient
                if any(_message_recipient(item) == recipient for item in pending) or globally_blocked:
                    return _queue_locked(path, message, dir_fd=dir_fd)
            authority.validate()
            _require_spool_capacity(path, message, dir_fd=dir_fd)
            queued_result = _queue_locked(path, message, dir_fd=dir_fd)
            try:
                status, payload = _transport(authority, message)
            except MessageAuthorityError:
                raise
            except fleet_client.FleetClientError:
                authority.validate()
                return queued_result
            # 401 is surfaced because a rotated token cannot flush this token-scoped spool.
            if status in (408, 425, 429) or status >= 500:
                return queued_result
            if not 200 <= status < 300:
                if 300 <= status < 400:
                    return queued_result
                if status == 400 and payload.get("code") == "message_timestamp_future":
                    return queued_result
                if status == 400 and payload.get("code") == "message_recipient_inactive":
                    return queued_result
                _remove_queued_locked(path, message, dir_fd=dir_fd)
                authority.validate()
                raise FleetMessageHubError(
                    _rejection(status, payload),
                    status=status,
                    code=payload.get("code"),
                )
            if not _response_confirms(payload, message):
                _LOG.warning("fleet message response did not confirm the submitted request; queueing it")
                return queued_result
            try:
                _remove_queued_locked(path, message, dir_fd=dir_fd)
            except (fleet_client.FleetClientError, OSError) as exc:
                _LOG.warning("confirmed fleet message remains in local spool: %s", type(exc).__name__)
            return payload
    except (KeyboardInterrupt, SystemExit):
        try:
            with fleet_client._spool_lock(path, timeout=lock_timeout) as dir_fd:
                already_queued = any(
                    entry.get("client_id") == message["client_id"]
                    for entry in fleet_client._read_spool(path, dir_fd=dir_fd, strict=True)
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


def _client_id(value: str | None) -> str:
    if value is None:
        return uuid4().hex
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value) is None:
        raise FleetMessageHubError(
            "client_id must contain 1 to 64 letters, digits, underscores, or hyphens", code="client_id_invalid"
        )
    return value


def _wire_metadata(**values: str | None) -> dict[str, str]:
    raw = {key: value for key, value in values.items() if value is not None}
    try:
        _message_limits._metadata(raw)
    except _message_limits.MessageError as exc:
        raise fleet_client.FleetClientError(str(exc)) from exc
    return raw


def sent(
    *,
    after: int = 0,
    ids: list[int] | None = None,
    token_file: str | None = None,
    authority: MessageAuthority | None = None,
    expected_scope: str | None = None,
) -> dict[str, Any]:
    _validate_sqlite_integer(after, "after cursor", allow_zero=True)
    authority = _authority(authority, token_file, expected_scope)
    body: dict[str, Any] = {"action": "sent"}
    if ids is not None:
        if after != 0 or not isinstance(ids, list) or not 1 <= len(ids) <= 100:
            raise fleet_client.FleetClientError("sent accepts either after or 1 to 100 ids")
        for message_id in ids:
            _validate_sqlite_integer(message_id, "message id")
        body["ids"] = ids
    else:
        body["after"] = after
    payload = _validate_page(_online(body, authority=authority), after=after)
    if ids is not None and any(message["id"] not in ids for message in payload["messages"]):
        raise fleet_client.FleetClientError("fleet hub returned an invalid message page")
    return payload


def flush_credential(
    token_file: str | None = None,
    *,
    lock_timeout: float | None = None,
    authority: MessageAuthority | None = None,
    expected_scope: str | None = None,
    warn_inactive: bool = True,
) -> int:
    fleet_client._validate_spool_lock_timeout(lock_timeout)
    authority = _authority(authority, token_file, expected_scope)
    return flush(authority.node_id, authority=authority, lock_timeout=lock_timeout, warn_inactive=warn_inactive)


def send(
    *,
    recipient: str,
    subject: str,
    body: str,
    reference: str | None,
    priority: str,
    thread: str | None = None,
    kind: str | None = None,
    sender_label: str | None = None,
    client_id: str | None = None,
    token_file: str | None = None,
    authority: MessageAuthority | None = None,
    expected_scope: str | None = None,
    reply_to: int | None = None,
    lock_timeout: float | None = None,
    warn_inactive: bool = True,
) -> dict[str, Any]:
    fleet_client._validate_spool_lock_timeout(lock_timeout)
    authority = _authority(authority, token_file, expected_scope)
    try:
        recipient = _message_limits._text(recipient, "to", 256) or ""
        subject = _message_limits._text(subject, "subject", _message_limits.SUBJECT_LIMIT) or ""
        body = _message_limits._text(body, "body", _message_limits.BODY_LIMIT) or ""
        reference = _message_limits._text(reference, "ref", _message_limits.REF_LIMIT, required=False)
    except _message_limits.MessageError as exc:
        raise fleet_client.FleetClientError(str(exc)) from exc
    if not isinstance(priority, str) or priority not in _message_limits.PRIORITIES:
        raise fleet_client.FleetClientError("priority must be low, normal, high, or urgent")
    message: dict[str, Any] = {
        "action": "send",
        "to": recipient,
        "subject": subject,
        "body": body,
        "ref": reference,
        "priority": priority,
        "client_id": _client_id(client_id),
        "enqueued_at": datetime.now(timezone.utc).isoformat(),
    }
    if reply_to is not None:
        _validate_sqlite_integer(reply_to, "reply_to")
        message["reply_to"] = reply_to
    message.update(_wire_metadata(thread=thread, kind=kind, sender_label=sender_label))
    result = _send_ordered(message, authority, lock_timeout=lock_timeout, warn_inactive=warn_inactive)
    authority.validate()
    return result


def identity(
    *,
    peers: list[str] | None = None,
    token_file: str | None = None,
    timeout: float | None = None,
    authority: MessageAuthority | None = None,
    expected_scope: str | None = None,
) -> dict[str, Any]:
    """Read the Hub-authenticated active principal, without sending or flushing messages."""
    request: dict[str, Any] = {"action": "identity"}
    if peers is not None:
        peers = _identity_peers(peers)
        request["peers"] = peers
    authority = _authority(authority, token_file, expected_scope)
    payload = _online(request, authority=authority, timeout=timeout)
    if peers is not None:
        _validate_identity_authorization(payload, peers)
        return payload
    principal = payload.get("principal")
    if (
        set(payload) != {"principal"}
        or not isinstance(principal, str)
        or not re.fullmatch(
            r"agent:[a-z][a-z0-9._-]{0,62}|[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}", principal
        )
    ):
        raise fleet_client.FleetClientError("fleet hub returned an invalid message identity")
    return payload


def _identity_agent(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"agent:[a-z][a-z0-9._-]{0,62}", value) is not None


def _identity_peers(value: object) -> list[str]:
    if (
        not isinstance(value, list)
        or not 1 <= len(value) <= 16
        or any(not _identity_agent(peer) for peer in value)
        or len(set(value)) != len(value)
    ):
        raise fleet_client.FleetClientError("invalid message identity peers")
    return list(value)


def _identity_epoch(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{16,128}", value) is not None


def _validate_identity_authorization(payload: dict[str, Any], peers: list[str]) -> None:
    error = "fleet hub returned an invalid message identity"
    records = payload.get("peers")
    if (
        set(payload) != {"schema", "principal", "enrollment_epoch", "grant_epoch", "peers"}
        or payload.get("schema") != "brigade.message_authorization.v1"
        or not _identity_agent(payload.get("principal"))
        or payload["principal"] in peers
        or not _identity_epoch(payload.get("enrollment_epoch"))
        or not _identity_epoch(payload.get("grant_epoch"))
        or not isinstance(records, list)
        or len(records) != len(peers)
    ):
        raise fleet_client.FleetClientError(error)
    for peer, record in zip(peers, records, strict=True):
        if (
            not isinstance(record, dict)
            or set(record) != {"principal", "authorized", "enrollment_epoch", "grant_epoch"}
            or record.get("principal") != peer
            or type(record.get("authorized")) is not bool
        ):
            raise fleet_client.FleetClientError(error)
        if record["authorized"]:
            valid = _identity_epoch(record["enrollment_epoch"]) and _identity_epoch(record["grant_epoch"])
        else:
            valid = record["enrollment_epoch"] is None and record["grant_epoch"] is None
        if not valid:
            raise fleet_client.FleetClientError(error)


def unread_count(
    *,
    token_file: str | None = None,
    timeout: float | None = None,
    authority: MessageAuthority | None = None,
    expected_scope: str | None = None,
) -> dict[str, Any]:
    """Read this credential's total unacknowledged inbox depth without bodies."""
    authority = _authority(authority, token_file, expected_scope)
    payload = _online({"action": "count"}, authority=authority, timeout=timeout)
    count = payload.get("unread_count")
    if not _message_limits._sqlite_integer(count, allow_zero=True):
        raise fleet_client.FleetClientError("fleet hub returned an invalid unread count")
    return {"unread_count": count}


def list_messages(
    *,
    after: int = 0,
    unacked: bool = False,
    wait_ms: int = 0,
    token_file: str | None = None,
    timeout: float | None = None,
    authority: MessageAuthority | None = None,
    expected_scope: str | None = None,
) -> dict[str, Any]:
    _validate_sqlite_integer(after, "after cursor", allow_zero=True)
    try:
        wait_ms = _message_limits._validate_wait_ms(wait_ms)
    except _message_limits.MessageError as exc:
        raise fleet_client.FleetClientError(str(exc)) from exc
    authority = _authority(authority, token_file, expected_scope)
    body: dict[str, Any] = {"action": "list", "after": after, "unacked": unacked}
    effective_timeout = _request_timeout(timeout)
    if wait_ms > 0:
        body["wait_ms"] = wait_ms
        min_timeout = wait_ms / 1000.0 + fleet_client.FLUSH_TIMEOUT_SECONDS
        effective_timeout = min_timeout if effective_timeout is None else max(effective_timeout, min_timeout)
    payload = _online(body, authority=authority, timeout=effective_timeout)
    return _validate_page(payload, after=after)


def _validate_page(payload: dict[str, Any], *, after: int) -> dict[str, Any]:
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
        or (isinstance(messages, list) and len(messages) > 100)
        or isinstance(next_cursor, bool)
        or not isinstance(next_cursor, int)
        or next_cursor > _message_limits.MAX_SQLITE_INTEGER
        or next_cursor != previous_id
    ):
        raise fleet_client.FleetClientError("fleet hub returned an invalid message page")
    return payload


def ack(
    message_id: int,
    *,
    token_file: str | None = None,
    timeout: float | None = None,
    authority: MessageAuthority | None = None,
    expected_scope: str | None = None,
) -> dict[str, Any]:
    _validate_sqlite_integer(message_id, "message id")
    authority = _authority(authority, token_file, expected_scope)
    payload = _online(
        {"action": "ack", "id": message_id},
        authority=authority,
        timeout=timeout,
    )
    message = payload.get("message")
    response_id = message.get("id") if isinstance(message, dict) else None
    if isinstance(response_id, bool) or not isinstance(response_id, int) or response_id != message_id:
        raise fleet_client.FleetClientError("fleet hub acknowledgement response did not confirm the requested message")
    return payload


def reply(
    message_id: int,
    *,
    body: str,
    subject: str | None,
    reference: str | None,
    priority: str,
    thread: str | None = None,
    kind: str | None = None,
    sender_label: str | None = None,
    client_id: str | None = None,
    token_file: str | None = None,
    authority: MessageAuthority | None = None,
    expected_scope: str | None = None,
    lock_timeout: float | None = None,
) -> dict[str, Any]:
    authority = _authority(authority, token_file, expected_scope)
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
        "client_id": _client_id(client_id),
        "enqueued_at": datetime.now(timezone.utc).isoformat(),
    }
    message.update(_wire_metadata(thread=thread, kind=kind, sender_label=sender_label))
    result = _send_ordered(message, authority, lock_timeout=lock_timeout)
    authority.validate()
    return result


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
            raise FleetMessageHubError(
                f"fleet hub principal management failed: {type(exc).__name__}", code="hub_unavailable"
            ) from exc
        if not isinstance(payload, dict):
            raise fleet_client.FleetClientError("fleet hub principal management returned an invalid response")
        return status, payload

    try:
        return fleet_client._run_with_deadline(request_blocking, timeout=fleet_client.FLUSH_TIMEOUT_SECONDS)
    except FleetMessageHubError:
        raise
    except fleet_client.FleetClientError:
        raise FleetMessageHubError("fleet hub request failed", code="hub_unavailable") from None
    except Exception as exc:
        raise FleetMessageHubError(
            f"fleet hub principal management failed: {type(exc).__name__}", code="hub_unavailable"
        ) from exc


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
        raise fleet_client.FleetClientError(_rejection(status, payload))
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
    may_send_to: list[str] | None = None,
    _defer_finalize: bool = False,
    _deferred_request_ids: list[str] | None = None,
    _deferred_paths: list[Path] | None = None,
) -> dict[str, Any]:
    if _defer_finalize and _deferred_request_ids is None:
        raise fleet_client.FleetClientError("deferred principal enrollment requires a request ID holder")
    settings = dict(fleet_client.load_fleet_settings())
    if not settings["hub_url"]:
        raise fleet_client.FleetClientError("no fleet hub configured (~/.brigade/fleet.toml [fleet] hub_url)")
    if not settings["admin_token"]:
        raise fleet_client.FleetClientError("no fleet admin token configured")
    fleet_client._require_encrypted_or_loopback_hub(settings["hub_url"])
    body: dict[str, Any] = {"action": action, "name": name}
    pending_path: Path | None = None
    revoke_pending_request_id: str | None = None
    if action == "add":
        generation = _principal_enrollment_generation(settings["hub_url"], settings["admin_token"], name)
        body.update(
            _pending_principal_request(
                name,
                settings["admin_token"],
                label,
                expected_enrollment_id=generation,
                may_send_to=may_send_to,
                hub_url=settings["hub_url"],
            )
        )
        if not body.get("may_send_to"):
            body.pop("may_send_to", None)
        body["token"] = hmac.new(
            settings["admin_token"].encode("utf-8"),
            f"brigade-message-principal:{name}:{body['request_id']}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        pending_path = _principal_pending_path(name, settings["admin_token"], hub_url=settings["hub_url"])
    elif action == "revoke":
        pending_path = _principal_revocation_path(name, settings["admin_token"], hub_url=settings["hub_url"])
        pending = _pending_principal_revocation_request(name, settings["admin_token"], hub_url=settings["hub_url"])
        if pending is None:
            generation = _principal_enrollment_generation(settings["hub_url"], settings["admin_token"], name)
            pending = _pending_principal_revocation_request(
                name, settings["admin_token"], expected_enrollment_id=generation, hub_url=settings["hub_url"]
            )
        if pending is None:
            raise fleet_client.FleetClientError("could not preserve pending principal revocation request")
        body["expected_enrollment_id"] = pending["expected_enrollment_id"]
        revoke_pending_request_id = pending["request_id"]
    elif action == "grants":
        from .fleet_hub_message_grants import normalize_patterns

        try:
            body["may_send_to"] = normalize_patterns(may_send_to)
        except _message_limits.MessageError as exc:
            raise fleet_client.FleetClientError(str(exc)) from exc
    elif label is not None:
        body["label"] = label
    status, payload = _principal_admin_request(settings["hub_url"], settings["admin_token"], body)
    if not 200 <= status < 300:
        if (
            action == "add"
            and pending_path is not None
            and status == 409
            and payload.get("code")
            in ("principal_enrollment_revoked", "principal_enrollment_changed", "principal_enrollment_grants_changed")
        ):
            _clear_principal_request(pending_path, request_id=body["request_id"])
        elif (
            action == "revoke"
            and pending_path is not None
            and status == 409
            and payload.get("code") == "principal_enrollment_changed"
            and revoke_pending_request_id is not None
        ):
            _clear_principal_request(pending_path, request_id=revoke_pending_request_id)
        elif pending_path is not None and 400 <= status < 500 and status not in (408, 409, 425, 429):
            request_id = body.get("request_id") if action == "add" else revoke_pending_request_id
            if isinstance(request_id, str):
                _clear_principal_request(pending_path, request_id=request_id)
        message = _rejection(status, payload)
        if action == "add" and payload.get("code") in ("principal_enrollment_revoked", "principal_enrollment_changed"):
            message += "; run principals add again to request a new enrollment generation"
        raise FleetMessageHubError(message, status=status, code=payload.get("code"))
    if action == "add" and pending_path is not None:
        principal = payload.get("principal")
        returned_token = principal.get("token") if isinstance(principal, dict) else None
        if (
            not isinstance(principal, dict)
            or principal.get("principal_id") != f"agent:{name}"
            or principal.get("name") != name
            or principal.get("enrollment_id") != body["request_id"]
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
            if _deferred_paths is not None:
                _deferred_paths.append(pending_path)
        else:
            _clear_principal_request(pending_path, request_id=body["request_id"])
    elif action == "revoke":
        principal = payload.get("principal")
        if (
            not isinstance(principal, dict)
            or principal.get("principal_id") != f"agent:{name}"
            or principal.get("name") != name
            or principal.get("enrollment_id") != body["expected_enrollment_id"]
        ):
            raise fleet_client.FleetClientError("fleet hub did not confirm the revoked principal generation")
        if revoke_pending_request_id is not None and pending_path is not None:
            _clear_principal_request(pending_path, request_id=revoke_pending_request_id)
        add_path = _principal_pending_path(name, settings["admin_token"], hub_url=settings["hub_url"])
        add_request_id = _pending_principal_request_id(add_path, name, enrollment_id=body["expected_enrollment_id"])
        if add_request_id is not None:
            _clear_principal_request(add_path, request_id=add_request_id)
    return payload


def set_principal_grants(name: str, patterns: list[str]) -> dict[str, Any]:
    return manage_principal("grants", name=name, may_send_to=patterns)


def show_principal(name: str) -> dict[str, Any]:
    return manage_principal("get", name=name)


def finalize_principal_add(name: str, *, request_id: str, pending_path: Path | None = None) -> None:
    """Clear enrollment retry state after the caller has safely delivered the token."""
    settings = fleet_client.load_fleet_settings()
    if not settings["admin_token"]:
        raise fleet_client.FleetClientError("no fleet admin token configured")
    current_path = _principal_pending_path(name, settings["admin_token"], hub_url=settings["hub_url"])
    if pending_path is not None and current_path != pending_path:
        raise FleetMessageHubError("principal credential changed", code="token_scope_changed")
    _clear_principal_request(current_path if pending_path is None else pending_path, request_id=request_id)


def _principal_pending_path(name: str, admin_token: str, *, hub_url: str) -> Path:
    token_scope = "v1-" + message_scope(hub_url, admin_token)
    name_scope = hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]
    return fleet_client.brigade_home() / fleet_client.SPOOL_DIRNAME / f"principal-{name_scope}-{token_scope}.jsonl"


def _principal_revocation_path(name: str, admin_token: str, *, hub_url: str) -> Path:
    token_scope = "v1-" + message_scope(hub_url, admin_token)
    name_scope = hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]
    return (
        fleet_client.brigade_home() / fleet_client.SPOOL_DIRNAME / f"principal-{name_scope}-{token_scope}.revoke.jsonl"
    )


def _require_no_legacy_principal_requests(name: str, admin_token: str, active: Path) -> None:
    name_scope = hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]
    token_scope = hashlib.sha256(admin_token.encode("utf-8")).hexdigest()
    for candidate in active.parent.glob(f"*principal-{name_scope}-{token_scope}.*"):
        if candidate.is_symlink() or (candidate.is_file() and candidate.stat().st_size):
            raise fleet_client.FleetClientError("legacy principal retry has unproven endpoint scope; left untouched")


def _pending_principal_revocation_request(
    name: str,
    admin_token: str,
    *,
    expected_enrollment_id: str | None | object = _NOT_PROVIDED,
    hub_url: str,
) -> dict[str, Any] | None:
    """Read or persist a revoke's enrollment generation across ambiguous retries."""
    path = _principal_revocation_path(name, admin_token, hub_url=hub_url)
    _require_no_legacy_principal_requests(name, admin_token, path)
    with fleet_client._spool_lock(path) as dir_fd:
        pending = fleet_client._read_spool(path, dir_fd=dir_fd, strict=True)
        for item in pending:
            request_id = item.get("request_id")
            generation = item.get("expected_enrollment_id")
            if item.get("name") != name or not isinstance(request_id, str) or not request_id:
                continue
            if generation is not None and not isinstance(generation, str):
                raise fleet_client.FleetClientError("pending principal revocation has an invalid generation")
            return {"name": name, "request_id": request_id, "expected_enrollment_id": generation}
        if expected_enrollment_id is _NOT_PROVIDED:
            return None
        if expected_enrollment_id is not None and not isinstance(expected_enrollment_id, str):
            raise fleet_client.FleetClientError("fleet hub returned an invalid principal enrollment generation")
        request = {
            "name": name,
            "request_id": uuid4().hex,
            "expected_enrollment_id": expected_enrollment_id,
        }
        fleet_client._write_spool_atomic(path, [request], dir_fd=dir_fd)
        return request


def _pending_principal_request(
    name: str,
    admin_token: str,
    label: str | None,
    *,
    expected_enrollment_id: str | None = None,
    may_send_to: list[str] | None = None,
    hub_url: str,
) -> dict[str, Any]:
    path = _principal_pending_path(name, admin_token, hub_url=hub_url)
    with fleet_client._spool_lock(path) as dir_fd:
        pending = fleet_client._read_spool(path, dir_fd=dir_fd, strict=True)
    # Unknown-origin retries must never acquire authority from current configuration.
    # Keep old files intact and refuse a new operation until they are reconciled.
    _require_no_legacy_principal_requests(name, admin_token, path)
    # An interrupted migration can leave a copy of an already finalized request.
    # Even origin-bearing filenames cannot prove that this history is pending.
    if any(path.parent.glob(f"*.{path.name}")):
        raise fleet_client.FleetClientError("node-prefixed principal retry requires reconciliation; left untouched")
    if not pending:
        with fleet_client._spool_lock(path) as dir_fd:
            pending = fleet_client._read_spool(path, dir_fd=dir_fd, strict=True)
            if not pending:
                new_request: dict[str, Any] = {
                    "name": name,
                    "request_id": uuid4().hex,
                    "expected_enrollment_id": expected_enrollment_id,
                }
                from .fleet_hub_message_grants import normalize_patterns

                try:
                    new_request["may_send_to"] = normalize_patterns(may_send_to if may_send_to is not None else [])
                except _message_limits.MessageError as exc:
                    raise fleet_client.FleetClientError(str(exc)) from exc
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
            pending_request["may_send_to"] = item.get("may_send_to", [])
            if may_send_to is not None and sorted(set(may_send_to)) != pending_request["may_send_to"]:
                _LOG.warning(
                    "pending enrollment retains its original send grants. Use principals grants after enrollment"
                )
            return pending_request
    raise fleet_client.FleetClientError("could not preserve pending principal enrollment request")


def _clear_principal_request(path: Path, *, request_id: str) -> bool:
    with fleet_client._spool_lock(path) as dir_fd:
        pending = fleet_client._read_spool(path, dir_fd=dir_fd, strict=True)
        remaining = [item for item in pending if item.get("request_id") != request_id]
        if len(remaining) == len(pending):
            return False
        fleet_client._write_spool_atomic(path, remaining, dir_fd=dir_fd)
        return True


def _pending_principal_request_id(path: Path, name: str, *, enrollment_id: str | None) -> str | None:
    with fleet_client._spool_lock(path) as dir_fd:
        pending = fleet_client._read_spool(path, dir_fd=dir_fd, strict=True)
    for item in pending:
        request_id = item.get("request_id")
        if item.get("name") == name and isinstance(request_id, str) and request_id and request_id == enrollment_id:
            return request_id
    return None


def flush(
    node_id: str,
    *,
    hub_url: str | None = None,
    token: str | None = None,
    lock_timeout: float | None = None,
    authority: MessageAuthority | None = None,
    warn_inactive: bool = True,
) -> int:
    fleet_client._validate_spool_lock_timeout(lock_timeout)
    if authority is None:
        try:
            authority = MessageAuthority.capture(node_credential=True)
        except MessageNotConfiguredError:
            return 0
        if (hub_url is not None and hub_url.rstrip("/") != authority.hub.rstrip("/")) or (
            token is not None and not hmac.compare_digest(token, authority.token)
        ):
            raise MessageAuthorityError("message credential changed", code="token_scope_changed")
    authority.validate()
    path = _spool_path(node_id, authority.token, hub_url=authority.hub)
    if warn_inactive:
        _warn_inactive_spools(node_id, path)
    if not path.is_file():
        return 0
    with fleet_client._spool_lock(path, timeout=lock_timeout) as dir_fd:
        authority.validate()
        result = _flush_locked(path, authority, dir_fd=dir_fd)
    authority.validate()
    return result


def _flush_locked(
    path: Path, authority: MessageAuthority, *, max_messages: int | None = None, dir_fd: int | None = None
) -> int:
    return _drain_locked(path, authority, max_messages=max_messages, dir_fd=dir_fd)[0]


def _message_recipient(message: dict[str, Any]) -> str | None:
    recipient = message.get("_recipient") or message.get("to")
    return recipient if isinstance(recipient, str) else None


def _reply_recipient(authority: MessageAuthority, message_id: int, *, timeout: float | None = None) -> str | None:
    _validate_sqlite_integer(message_id, "message id")
    body = {"action": "list", "after": message_id - 1, "unacked": False}
    try:
        status, payload = _transport(authority, body, timeout=timeout)
    finally:
        authority.validate()
    messages = payload.get("messages")
    if status == 401 or (status == 403 and payload.get("code") == "message_sender_inactive"):
        raise _UnauthorizedError(_rejection(status, payload), status=status, code=payload.get("code"))
    if status != 200 or not isinstance(messages, list):
        raise fleet_client.FleetClientError("fleet hub could not resolve the reply recipient")
    previous_id = message_id - 1
    recipient = None
    for item in messages:
        candidate_id = item.get("id") if isinstance(item, dict) else None
        if (
            isinstance(candidate_id, bool)
            or not isinstance(candidate_id, int)
            or candidate_id <= previous_id
            or candidate_id > _message_limits.MAX_SQLITE_INTEGER
        ):
            raise fleet_client.FleetClientError("fleet hub returned an invalid reply-recipient page")
        previous_id = candidate_id
        if candidate_id == message_id:
            sender = item.get("sender")
            if not isinstance(sender, str) or not sender:
                raise fleet_client.FleetClientError("fleet hub returned an invalid reply-recipient record")
            recipient = sender
    next_cursor = payload.get("next_cursor")
    if isinstance(next_cursor, bool) or not isinstance(next_cursor, int) or next_cursor != previous_id:
        raise fleet_client.FleetClientError("fleet hub returned an invalid reply-recipient cursor")
    return recipient


def _drain_locked(
    path: Path, authority: MessageAuthority, *, max_messages: int | None = None, dir_fd: int | None = None
) -> tuple[int, bool]:
    authority.validate()
    if path.name != f"messages-v1-{authority.scope}.jsonl":
        raise FleetMessageHubError("message spool origin does not match transport", code="token_scope_changed")
    messages = fleet_client._read_spool(path, dir_fd=dir_fd, strict=True)
    deadline = monotonic() + MESSAGE_DRAIN_DEADLINE_SECONDS
    delivered = 0
    attempted = 0
    retained: list[dict[str, Any]] = []
    blocked_recipients: set[str] = set()
    for index, message in enumerate(messages):
        authority.validate()
        if _expired(message):
            _LOG.warning("fleet message spool %s: dropping expired offline message", path)
            continue
        if max_messages is not None and attempted >= max_messages:
            retained.extend(messages[index:])
            fleet_client._write_spool_atomic(path, retained, dir_fd=dir_fd)
            return delivered, False
        remaining = deadline - monotonic()
        if remaining <= 0:
            retained.extend(messages[index:])
            fleet_client._write_spool_atomic(path, retained, dir_fd=dir_fd)
            return delivered, True
        recipient = _message_recipient(message)
        if recipient is None and blocked_recipients and message.get("action") == "reply":
            try:
                attempted += 1
                lookup_timeout = min(fleet_client.FLUSH_TIMEOUT_SECONDS, remaining)
                recipient = _reply_recipient(authority, int(message["id"]), timeout=lookup_timeout)
                if recipient is None:
                    _LOG.warning("fleet message spool %s: dropping reply because its parent expired", path)
                    continue
                message["_recipient"] = recipient
            except MessageAuthorityError:
                raise
            except _UnauthorizedError:
                retained.extend(messages[index:])
                fleet_client._write_spool_atomic(path, retained, dir_fd=dir_fd)
                raise
            except (KeyError, TypeError, ValueError, fleet_client.FleetClientError):
                retained.extend(messages[index:])
                fleet_client._write_spool_atomic(path, retained, dir_fd=dir_fd)
                return delivered, True
        if recipient in blocked_recipients:
            retained.append(message)
            continue
        if max_messages is not None and attempted >= max_messages:
            retained.extend(messages[index:])
            fleet_client._write_spool_atomic(path, retained, dir_fd=dir_fd)
            return delivered, False
        remaining = deadline - monotonic()
        if remaining <= 0:
            retained.extend(messages[index:])
            fleet_client._write_spool_atomic(path, retained, dir_fd=dir_fd)
            return delivered, True
        try:
            attempted += 1
            timeout = min(fleet_client.FLUSH_TIMEOUT_SECONDS, remaining)
            if timeout == fleet_client.FLUSH_TIMEOUT_SECONDS:
                status, payload = _transport(authority, message)
            else:
                status, payload = _transport(authority, message, timeout=timeout)
        except MessageAuthorityError:
            raise
        except fleet_client.FleetClientError:
            authority.validate()
            retained.extend(messages[index:])
            fleet_client._write_spool_atomic(path, retained, dir_fd=dir_fd)
            return delivered, True
        authority.validate()
        code = payload.get("code")
        if status == 401 or (status == 403 and code == "message_sender_inactive"):
            retained.extend(messages[index:])
            fleet_client._write_spool_atomic(path, retained, dir_fd=dir_fd)
            raise _UnauthorizedError(
                _rejection(status, payload),
                status=status,
                code=payload.get("code"),
            )
        if status == 429 and code in ("message_recipient_quota", "message_sender_recipient_quota"):
            rejected_recipient = payload.get("recipient") or recipient
            if isinstance(rejected_recipient, str):
                message["_recipient"] = rejected_recipient
                blocked_recipients.add(rejected_recipient)
            retained.append(message)
            continue
        if status == 400 and code == "message_recipient_inactive":
            rejected_recipient = payload.get("recipient") or recipient
            if isinstance(rejected_recipient, str):
                message["_recipient"] = rejected_recipient
                blocked_recipients.add(rejected_recipient)
            retained.append(message)
            continue
        if (
            300 <= status < 400
            or status >= 500
            or status in (408, 425, 429)
            or (status == 400 and code == "message_timestamp_future")
        ):
            retained.extend(messages[index:])
            fleet_client._write_spool_atomic(path, retained, dir_fd=dir_fd)
            return delivered, True
        if not 200 <= status < 300 and status >= 400:
            _LOG.warning(
                "fleet message spool %s: dropping rejected message: %s",
                path,
                _safe_log_text(
                    payload.get("code")
                    if payload.get("code") in ("invalid_argument", "message_recipient_unknown", "message_not_found")
                    else "hub_rejected"
                ),
            )
        elif 200 <= status < 300:
            if not _response_confirms(payload, message):
                _LOG.warning("fleet message spool %s: response did not confirm the queued message", path)
                retained.extend(messages[index:])
                fleet_client._write_spool_atomic(path, retained, dir_fd=dir_fd)
                return delivered, True
            delivered += 1
        else:
            retained.extend(messages[index:])
            fleet_client._write_spool_atomic(path, retained, dir_fd=dir_fd)
            return delivered, True
    fleet_client._write_spool_atomic(path, retained, dir_fd=dir_fd)
    authority.validate()
    return delivered, False


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
