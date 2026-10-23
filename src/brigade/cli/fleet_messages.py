"""CLI for the Fleet Hub principal message queue.

JSON failures contain error, status, and code. Local failures have null status
and a non-null code: token_file_invalid (unsafe or invalid token file),
client_id_invalid (invalid retry key), spool_conflict (queued key reused with a
different payload), admin_token_refused (admin credential used for messaging),
or invalid_argument (other local validation, configuration, or filesystem error).
Transport failures use hub_unavailable with null status. Hub refusals preserve
the HTTP status and the hub's code, which may be null on older hubs.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .. import fleet_hub_messages


def register(fleet_sub: argparse._SubParsersAction) -> None:
    # The fleet MCP station is the agent-facing side of messaging; registering
    # it here keeps cli/fleet.py under the module size ceiling.
    from ..fleet_mcp import register as register_mcp

    register_mcp(fleet_sub)
    parser = fleet_sub.add_parser("msg", help="Send and manage durable Fleet Hub messages.")
    sub = parser.add_subparsers(dest="msg_command", metavar="<msg-command>")
    sub.required = True
    identity = sub.add_parser("identity", help="Read this credential's authenticated active Hub principal.")
    identity.add_argument("--json", action="store_true")
    _message_options(identity)
    identity.set_defaults(func=_identity)
    send = sub.add_parser("send", help="Send a message to an active node or agent principal.")
    send.add_argument("--to", required=True, help="Recipient node or agent principal.")
    send.add_argument("--subject", required=True)
    send.add_argument("--body-file", required=True, type=Path, help="Read the message body from this file.")
    send.add_argument("--ref", help="Optional issue, PR, or run reference.")
    send.add_argument("--priority", choices=("low", "normal", "high", "urgent"), default="normal")
    send.add_argument("--json", action="store_true")
    _message_options(send, metadata=True)
    send.set_defaults(func=_send)

    listing = sub.add_parser("list", help="Read this principal's message inbox.")
    listing.add_argument("--unacked", action="store_true")
    listing.add_argument("--after", type=int, default=0, help="Return messages after this cursor.")
    listing.add_argument(
        "--wait",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help="Wait up to SECONDS for matching messages (0 to 20 seconds; larger values are rejected).",
    )
    listing.add_argument("--json", action="store_true")
    _message_options(listing)
    listing.set_defaults(func=_list)

    count = sub.add_parser("count", help="Count this principal's unread inbox messages without fetching bodies.")
    count.add_argument("--json", action="store_true")
    _message_options(count)
    count.set_defaults(func=_count)

    ack = sub.add_parser("ack", help="Acknowledge one inbox message.")
    ack.add_argument("id", type=int)
    ack.add_argument("--json", action="store_true")
    _message_options(ack)
    ack.set_defaults(func=_ack)

    reply = sub.add_parser("reply", help="Reply to one inbox message.")
    reply.add_argument("id", type=int)
    reply.add_argument("--body-file", required=True, type=Path)
    reply.add_argument("--subject")
    reply.add_argument("--ref")
    reply.add_argument("--priority", choices=("low", "normal", "high", "urgent"), default="normal")
    reply.add_argument("--json", action="store_true")
    _message_options(reply, metadata=True)
    reply.set_defaults(func=_reply)

    sent = sub.add_parser("sent", help="Read this principal's sent messages and acknowledgement status.")
    filters = sent.add_mutually_exclusive_group(required=True)
    filters.add_argument("--after", type=int, default=0)
    filters.add_argument("--id", type=int, action="append", dest="ids")
    sent.add_argument("--json", action="store_true")
    _message_options(sent)
    sent.set_defaults(func=_sent)

    flush = sub.add_parser("flush", help="Flush queued messages for this invocation's credential.")
    flush.add_argument("--json", action="store_true")
    _message_options(flush)
    flush.set_defaults(func=_flush)

    principals = sub.add_parser("principals", help="Enroll or revoke named external agent principals (admin token).")
    principal_sub = principals.add_subparsers(dest="principal_command", metavar="<principal-command>")
    principal_sub.required = True
    add = principal_sub.add_parser(
        "add", help="Enroll a named external agent and print its token (shown once; store it privately)."
    )
    add.add_argument("name")
    add.add_argument("--label")
    add.add_argument("--may-send-to", nargs="+", action="extend")
    add.add_argument("--json", action="store_true")
    add.set_defaults(func=_principal_add)
    revoke = principal_sub.add_parser("revoke", help="Revoke a named external agent token.")
    revoke.add_argument("name")
    revoke.add_argument("--json", action="store_true")
    revoke.set_defaults(func=_principal_revoke)
    grants = principal_sub.add_parser("grants", help="Replace an external principal's send grants.")
    grants.add_argument("name")
    grant_options = grants.add_mutually_exclusive_group(required=True)
    grant_options.add_argument("--may-send-to", nargs="+", action="extend")
    grant_options.add_argument("--clear", action="store_true")
    grants.add_argument("--json", action="store_true")
    grants.set_defaults(func=_principal_grants)
    show = principal_sub.add_parser("show", help="Show an external principal's grants and revocation state.")
    show.add_argument("name")
    show.add_argument("--json", action="store_true")
    show.set_defaults(func=_principal_show)


def _message_options(parser: argparse.ArgumentParser, *, metadata: bool = False) -> None:
    parser.add_argument("--token-file", help="Use a protected message principal token file for this invocation.")
    if metadata:
        parser.add_argument("--thread")
        parser.add_argument("--kind", choices=("message", "question", "completion"))
        parser.add_argument("--sender-label", help="Display label for a node sender.")
        parser.add_argument("--client-id", help="Stable idempotency key for retries.")


def _token_kwargs(args: argparse.Namespace) -> dict[str, Any]:
    return {"token_file": args.token_file} if args.token_file is not None else {}


def _read_body(path: Path) -> str:
    try:
        if path == Path("-"):
            raw = sys.stdin.buffer.read(fleet_hub_messages.BODY_LIMIT + 1)
        else:
            with path.open("rb") as stream:
                raw = stream.read(fleet_hub_messages.BODY_LIMIT + 1)
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"cannot read message body file {path}: {exc}") from exc
    if len(raw) > fleet_hub_messages.BODY_LIMIT:
        raise ValueError(f"message body file exceeds {fleet_hub_messages.BODY_LIMIT} bytes: {path}")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"cannot read message body file {path}: {exc}") from exc


def _result(
    args: argparse.Namespace,
    operation: Callable[[], dict[str, Any]],
    *,
    after_output: Callable[[], None] | None = None,
) -> int:
    from .. import fleet_client

    try:
        payload = operation()
    except (fleet_client.FleetClientError, OSError, ValueError) as exc:
        from .fleet import _safe_table_cell

        if args.json:
            status = getattr(exc, "status", None)
            code = getattr(exc, "code", None)
            if status is None and code is None:
                code = "invalid_argument"
            print(
                json.dumps(
                    {"error": str(exc), "status": status, "code": code},
                    sort_keys=True,
                )
            )
        print(f"error: {_safe_table_cell(exc)}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        message = payload.get("message")
        if isinstance(message, dict):
            from .fleet import _safe_table_cell

            print(
                f"message {_safe_table_cell(message.get('id'))} "
                f"{_safe_table_cell(message.get('recipient'))} {_safe_table_cell(message.get('subject'))}"
            )
        elif payload.get("queued"):
            print(f"message queued for delivery ({payload.get('client_id')})")
        else:
            print(json.dumps(payload, sort_keys=True))
    if after_output is not None:
        sys.stdout.flush()
        try:
            after_output()
        except (fleet_client.FleetClientError, OSError, ValueError) as exc:
            from .fleet import _safe_table_cell

            print(f"error: {_safe_table_cell(exc)}", file=sys.stderr)
            return 1
    return 0


def _send(args: argparse.Namespace) -> int:
    from .. import fleet_client_messages

    return _result(
        args,
        lambda: fleet_client_messages.send(
            recipient=args.to,
            subject=args.subject,
            body=_read_body(args.body_file),
            reference=args.ref,
            priority=args.priority,
            thread=args.thread,
            kind=args.kind,
            sender_label=args.sender_label,
            client_id=args.client_id,
            **_token_kwargs(args),
        ),
    )


def _wait_ms(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError("wait must be a non-negative number of seconds")
    if value == 0:
        return 0
    if value > fleet_hub_messages.MAX_WAIT_MS / 1000:
        raise ValueError("wait must not exceed 20 seconds")
    return max(1, int(round(float(value) * 1000)))


def _identity(args: argparse.Namespace) -> int:
    from .. import fleet_client_messages

    return _result(args, lambda: fleet_client_messages.identity(**_token_kwargs(args)))


def _list(args: argparse.Namespace) -> int:
    from .. import fleet_client_messages

    def operation() -> dict[str, Any]:
        wait_ms = _wait_ms(getattr(args, "wait", 0.0))
        kwargs: dict[str, Any] = _token_kwargs(args)
        if wait_ms > 0:
            kwargs["wait_ms"] = wait_ms
        return fleet_client_messages.list_messages(after=args.after, unacked=args.unacked, **kwargs)

    return _result(args, operation)


def _count(args: argparse.Namespace) -> int:
    from .. import fleet_client_messages

    return _result(args, lambda: fleet_client_messages.unread_count(**_token_kwargs(args)))


def _ack(args: argparse.Namespace) -> int:
    from .. import fleet_client_messages

    return _result(args, lambda: fleet_client_messages.ack(args.id, **_token_kwargs(args)))


def _reply(args: argparse.Namespace) -> int:
    from .. import fleet_client_messages

    return _result(
        args,
        lambda: fleet_client_messages.reply(
            args.id,
            body=_read_body(args.body_file),
            subject=args.subject,
            reference=args.ref,
            priority=args.priority,
            thread=args.thread,
            kind=args.kind,
            sender_label=args.sender_label,
            client_id=args.client_id,
            **_token_kwargs(args),
        ),
    )


def _principal_add(args: argparse.Namespace) -> int:
    from .. import fleet_client_messages

    deferred_request_ids: list[str] = []
    deferred_paths: list[Path] = []

    return _result(
        args,
        lambda: fleet_client_messages.manage_principal(
            "add",
            name=args.name,
            label=args.label,
            may_send_to=getattr(args, "may_send_to", None),
            _defer_finalize=True,
            _deferred_request_ids=deferred_request_ids,
            _deferred_paths=deferred_paths,
        ),
        after_output=lambda: fleet_client_messages.finalize_principal_add(
            args.name, request_id=deferred_request_ids[-1], pending_path=deferred_paths[-1]
        ),
    )


def _principal_revoke(args: argparse.Namespace) -> int:
    from .. import fleet_client_messages

    return _result(args, lambda: fleet_client_messages.manage_principal("revoke", name=args.name))


def _sent(args: argparse.Namespace) -> int:
    from .. import fleet_client_messages

    return _result(args, lambda: fleet_client_messages.sent(after=args.after, ids=args.ids, **_token_kwargs(args)))


def _flush(args: argparse.Namespace) -> int:
    from .. import fleet_client_messages

    return _result(args, lambda: {"delivered": fleet_client_messages.flush_credential(**_token_kwargs(args))})


def _principal_grants(args: argparse.Namespace) -> int:
    from .. import fleet_client_messages

    return _result(
        args, lambda: fleet_client_messages.set_principal_grants(args.name, [] if args.clear else args.may_send_to)
    )


def _principal_show(args: argparse.Namespace) -> int:
    from .. import fleet_client_messages

    return _result(args, lambda: fleet_client_messages.show_principal(args.name))
