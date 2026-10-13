"""CLI for the Fleet Hub principal message queue."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .. import fleet_hub_messages


def register(fleet_sub: argparse._SubParsersAction) -> None:
    parser = fleet_sub.add_parser("msg", help="Send and manage durable Fleet Hub messages.")
    sub = parser.add_subparsers(dest="msg_command", metavar="<msg-command>")
    sub.required = True
    send = sub.add_parser("send", help="Send a message to an enrolled node principal.")
    send.add_argument("--to", required=True, help="Recipient node principal.")
    send.add_argument("--subject", required=True)
    send.add_argument("--body-file", required=True, type=Path, help="Read the message body from this file.")
    send.add_argument("--ref", help="Optional issue, PR, or run reference.")
    send.add_argument("--priority", choices=("low", "normal", "high", "urgent"), default="normal")
    send.add_argument("--json", action="store_true")
    send.set_defaults(func=_send)

    listing = sub.add_parser("list", help="Read this principal's message inbox.")
    listing.add_argument("--unacked", action="store_true")
    listing.add_argument("--after", type=int, default=0, help="Return messages after this cursor.")
    listing.add_argument("--json", action="store_true")
    listing.set_defaults(func=_list)

    ack = sub.add_parser("ack", help="Acknowledge one inbox message.")
    ack.add_argument("id", type=int)
    ack.add_argument("--json", action="store_true")
    ack.set_defaults(func=_ack)

    reply = sub.add_parser("reply", help="Reply to one inbox message.")
    reply.add_argument("id", type=int)
    reply.add_argument("--body-file", required=True, type=Path)
    reply.add_argument("--subject")
    reply.add_argument("--ref")
    reply.add_argument("--priority", choices=("low", "normal", "high", "urgent"), default="normal")
    reply.add_argument("--json", action="store_true")
    reply.set_defaults(func=_reply)

    principals = sub.add_parser("principals", help="Enroll or revoke named external agent principals (admin token).")
    principal_sub = principals.add_subparsers(dest="principal_command", metavar="<principal-command>")
    principal_sub.required = True
    add = principal_sub.add_parser("add", help="Enroll a named external agent and print its one-time token.")
    add.add_argument("name")
    add.add_argument("--label")
    add.add_argument("--json", action="store_true")
    add.set_defaults(func=_principal_add)
    revoke = principal_sub.add_parser("revoke", help="Revoke a named external agent token.")
    revoke.add_argument("name")
    revoke.add_argument("--json", action="store_true")
    revoke.set_defaults(func=_principal_revoke)


def _read_body(path: Path) -> str:
    try:
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
        ),
    )


def _list(args: argparse.Namespace) -> int:
    from .. import fleet_client_messages

    return _result(args, lambda: fleet_client_messages.list_messages(after=args.after, unacked=args.unacked))


def _ack(args: argparse.Namespace) -> int:
    from .. import fleet_client_messages

    return _result(args, lambda: fleet_client_messages.ack(args.id))


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
        ),
    )


def _principal_add(args: argparse.Namespace) -> int:
    from .. import fleet_client_messages

    deferred_request_ids: list[str] = []

    return _result(
        args,
        lambda: fleet_client_messages.manage_principal(
            "add",
            name=args.name,
            label=args.label,
            _defer_finalize=True,
            _deferred_request_ids=deferred_request_ids,
        ),
        after_output=lambda: fleet_client_messages.finalize_principal_add(
            args.name, request_id=deferred_request_ids[-1]
        ),
    )


def _principal_revoke(args: argparse.Namespace) -> int:
    from .. import fleet_client_messages

    return _result(args, lambda: fleet_client_messages.manage_principal("revoke", name=args.name))
