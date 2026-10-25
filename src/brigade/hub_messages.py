"""Shared Fleet Hub message transport, projections, and idempotent sends."""

from __future__ import annotations

import builtins
from contextlib import contextmanager
import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Callable, Iterator, Mapping

from . import fleet_client, fleet_client_messages
from .fleet_client import FleetClientError
from .fleet_client_message_credentials import FleetMessageHubError, MessageAuthority

MAX_MESSAGE_BYTES = 8192
INBOX_TOOLS = frozenset({"inbox_list", "inbox_ack", "inbox_reply", "message_send", "message_status"})
# Only protocol codes may cross the public boundary, never arbitrary upstream text.
HUB_CODES = frozenset(
    {
        "message_recipient_not_permitted",
        "message_sender_recipient_quota",
        "message_recipient_quota",
        "message_sender_quota",
        "message_recipient_inactive",
        "message_recipient_unknown",
        "message_not_found",
        "message_timestamp_future",
        "token_file_invalid",
        "admin_token_refused",
        "spool_conflict",
        "client_id_invalid",
        "invalid_argument",
        "hub_unavailable",
    }
)


class HubMessageError(Exception):
    def __init__(self, code: str, hub_code: str | None = None):
        self.code = code
        self.hub_code = hub_code if isinstance(hub_code, str) and hub_code in HUB_CODES else None
        super().__init__(code)


def call_message(action: str, *args: Any, token_file: str | None, **kwargs: Any) -> Any:
    try:
        value = getattr(fleet_client_messages, action)(*args, token_file=token_file, **kwargs)
    except FleetMessageHubError as error:
        hub_code = error.code if isinstance(error.code, str) else None
        code = {
            401: "hub_unauthorized",
            403: "hub_forbidden",
            404: "message_not_found",
            429: "hub_quota",
            400: "invalid_request",
            409: "invalid_request",
        }.get(error.status or 0, "hub_unavailable")
        code = {
            "message_recipient_not_permitted": "recipient_not_permitted",
            "message_sender_recipient_quota": "recipient_quota",
        }.get(hub_code or "", code)
        if error.status is None:
            code = LOCAL_CLI_CODES.get(hub_code or "", code)
        raise HubMessageError(code, hub_code) from None
    except (FleetClientError, OSError, ValueError):
        raise HubMessageError("hub_unavailable") from None
    if action != "flush_credential" and not isinstance(value, dict):
        raise HubMessageError("hub_unavailable")
    return value


def inbox_page(value: dict[str, Any], *, after: int, recipient: str | None = None) -> tuple[list[dict[str, Any]], int]:
    rows, cursor = value.get("messages"), value.get("next_cursor")
    if not isinstance(rows, list) or type(cursor) is not int:
        raise HubMessageError("hub_unavailable")
    previous = after
    for row in rows:
        if not isinstance(row, dict) or type(row.get("id")) is not int or row["id"] <= previous:
            raise HubMessageError("hub_inconsistent")
        previous = row["id"]
        if recipient is not None and row.get("recipient") != recipient:
            raise HubMessageError("hub_inconsistent")
        if not all(isinstance(row.get(key), str) for key in ("sender", "body", "created_at")):
            raise HubMessageError("hub_inconsistent")
    if cursor != previous:
        raise HubMessageError("hub_inconsistent")
    return rows, cursor


LOCAL_CLI_CODES = {
    "token_file_invalid": "hub_credential_invalid",
    "admin_token_refused": "hub_credential_invalid",
    "token_scope_changed": "hub_credential_changed",
    "spool_conflict": "client_id_conflict",
    "client_id_invalid": "invalid_request",
    "invalid_argument": "invalid_request",
}


def sender_name(row: dict[str, Any], node_id: str) -> str:
    label = row.get("sender_label")
    sender = row["sender"]
    return label if label and sender == node_id else f"{label}@{sender}" if label else sender


def legacy_message(row: dict[str, Any], node_id: str) -> dict[str, Any]:
    body = row["body"]
    raw = body.encode("utf-8")
    if len(raw) > MAX_MESSAGE_BYTES:
        suffix = "\n[truncated; hub message " + str(row["id"]) + "]"
        body = raw[: MAX_MESSAGE_BYTES - len(suffix.encode())].decode("utf-8", errors="ignore") + suffix
    return dict(
        id=row["id"],
        created_at=row["created_at"],
        sender=sender_name(row, node_id),
        thread=row.get("thread"),
        body=body,
        acked_at=row.get("acked_at"),
        priority="urgent" if row.get("priority") in ("high", "urgent") else "normal",
        subject=row.get("subject"),
        ref=row.get("ref"),
        kind=row.get("kind") or "message",
    )


def truncate_subject(subject: str) -> str:
    return subject.encode("utf-8")[:200].decode("utf-8", errors="ignore")


def default_subject(body: str, kind: str, sender: str) -> str:
    return truncate_subject(next((line.strip() for line in body.splitlines() if line.strip()), f"{kind} from {sender}"))


class MessageOperations:
    """Both connector clients use the same send/reply/status behavior."""

    def __init__(
        self,
        *,
        token_file: str,
        recipients: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self.token_file = token_file
        self._recipients = recipients or {}
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @property
    def recipients(self) -> Mapping[str, str]:
        return self._recipients

    def _created_at(self) -> str:
        moment = self.clock()
        self._require(isinstance(moment, datetime) and moment.utcoffset() is not None, "clock_unavailable")
        return moment.astimezone(timezone.utc).isoformat()

    @staticmethod
    def _require(condition: bool, code: str) -> None:
        if not condition:
            raise HubMessageError(code)

    def _call(self, action: str, *args: Any, **kwargs: Any) -> Any:
        return call_message(action, *args, token_file=self.token_file, **kwargs)

    def capture(self) -> MessageAuthority:
        try:
            return MessageAuthority.capture(self.token_file)
        except FleetClientError as error:
            raise HubMessageError("hub_credential_invalid", getattr(error, "code", None)) from None

    def require_authority(self, authority: MessageAuthority) -> None:
        try:
            authority.validate()
        except FleetClientError as error:
            code = (
                "hub_credential_changed"
                if getattr(error, "code", None) == "token_scope_changed"
                else "hub_credential_invalid"
            )
            raise HubMessageError(code, getattr(error, "code", None)) from None

    @contextmanager
    def _operation_scope(self) -> Iterator[MessageAuthority]:
        authority = self.capture()
        try:
            yield authority
        finally:
            self.require_authority(authority)

    def _sent_message(self, value: dict[str, Any], *, agent: bool) -> dict[str, Any] | None:
        if value.get("queued") is True:
            return None
        row = value.get("message")
        self._require(
            isinstance(row, dict)
            and type(row.get("id")) is int
            and row["id"] > 0
            and isinstance(row.get("created_at"), str),
            "hub_unavailable",
        )
        return row

    def reply(self, msg_id: int, body: str) -> dict[str, Any]:
        with self._operation_scope() as authority:
            cid = "mr-" + hashlib.sha256(f"{msg_id}\n{body}".encode()).hexdigest()[:40]
            value = self._call(
                "reply",
                msg_id,
                body=body,
                subject=None,
                reference=None,
                priority="normal",
                thread=None,
                kind="message",
                sender_label=None,
                client_id=cid,
                authority=authority,
            )
            row = self._sent_message(value, agent=True)
            result: dict[str, Any] = dict(
                id=row["id"] if row else None,
                msg_id=msg_id,
                created_at=row["created_at"] if row else self._created_at(),
                body=body,
            )
            if row is None:
                result["queued"] = True
            return result

    def send(
        self,
        to: str,
        subject: str,
        body: str,
        ref: str | None = None,
        priority: str = "normal",
        thread: str | None = None,
        kind: str = "message",
        client_id: str | None = None,
        *,
        lock_timeout: float | None = None,
        warn_inactive: bool = True,
    ) -> dict[str, Any]:
        fleet_client._validate_spool_lock_timeout(lock_timeout)
        with self._operation_scope() as authority:
            # Node names from hub_recipients resolve to node ids; grants name node ids.
            recipient = dict(self.recipients).get(to, to)
            cid = (
                client_id
                or "ms-"
                + hashlib.sha256(
                    json.dumps(
                        [recipient, subject, body, ref, priority, thread, kind, int(self.clock().timestamp()) // 600]
                    ).encode()
                ).hexdigest()[:40]
            )
            send_options: dict[str, Any] = {}
            if lock_timeout is not None:
                send_options["lock_timeout"] = lock_timeout
            if not warn_inactive:
                send_options["warn_inactive"] = False
            row = self._sent_message(
                self._call(
                    "send",
                    recipient=recipient,
                    subject=subject,
                    body=body,
                    reference=ref,
                    priority=priority,
                    thread=thread,
                    kind=kind,
                    sender_label=None,
                    client_id=cid,
                    authority=authority,
                    **send_options,
                ),
                agent=True,
            )
            return dict(
                id=row["id"] if row else None,
                to=to,
                recipient=recipient,
                created_at=row["created_at"] if row else self._created_at(),
                client_id=cid,
                queued=row is None,
            )

    def status(self, ids: builtins.list[int]) -> dict[str, Any]:
        with self._operation_scope() as authority:
            value = self._call("sent", ids=ids, authority=authority)
            self._require(isinstance(value.get("messages"), list), "hub_unavailable")
            self._require(
                all(
                    isinstance(row, dict) and isinstance(row.get("recipient"), (str, type(None)))
                    for row in value["messages"]
                ),
                "hub_unavailable",
            )
            keys = ("id", "subject", "created_at", "acked_at", "acked_by", "reply_to")
            names = {node: name for name, node in self.recipients.items()}
            return dict(
                messages=[
                    dict(to=names.get(row.get("recipient"), row.get("recipient")), **{k: row.get(k) for k in keys})
                    for row in value["messages"]
                ]
            )
