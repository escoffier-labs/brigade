"""Credential-bound Fleet Hub tools over the shared MCP transports."""

from __future__ import annotations

import argparse
from time import monotonic, sleep
from typing import Any

from . import fleet_client, fleet_client_messages as messages, mcp_server
from .fleet_hub_messages import MAX_SQLITE_INTEGER, MAX_WAIT_MS
from .untrusted import scan_untrusted, wrap_untrusted

DEFAULT_PORT = 8933
_SAFE_ERROR_CODES = frozenset(
    {
        "hub_unavailable",
        "message_recipient_not_permitted",
        "message_sender_recipient_quota",
        "message_recipient_quota",
        "message_sender_quota",
        "message_recipient_inactive",
        "message_sender_inactive",
        "message_timestamp_future",
        "token_file_invalid",
        "admin_token_refused",
        "spool_conflict",
        "client_id_invalid",
    }
)


def tool_specs() -> list[dict[str, Any]]:
    text = {"type": "string", "minLength": 1}
    boolean = {"type": "boolean"}
    limit = {"type": "integer", "minimum": 1, "maximum": 100, "default": 100}
    definitions = [
        (
            "fleet_who",
            "List interactive sessions and permitted active recipients.",
            {"filter": {"type": "string"}, "all": boolean},
            [],
        ),
        (
            "fleet_send",
            "Send a message, durably spooling when the hub is offline.",
            {
                "to": text,
                "body": text,
                "reply_to": text,
                "kind": {"type": "string", "enum": ["message", "question", "completion"]},
                "thread": text,
            },
            ["to", "body"],
        ),
        (
            "fleet_inbox",
            "Read inbox messages without acknowledging them; optionally wait or count unread mail without bodies.",
            {
                "wait_secs": {"type": "integer", "minimum": 0, "maximum": 120, "default": 0},
                "unacked_only": {"type": "boolean", "default": True},
                "limit": limit,
                "after": text,
                "count_only": {
                    "type": "boolean",
                    "default": False,
                    "description": "Return only unread_count for the whole inbox, ignoring wait, cursor and page options.",
                },
            },
            [],
        ),
        (
            "fleet_ack",
            "Acknowledge inbox messages owned by this credential.",
            {"ids": {"type": "array", "items": text, "minItems": 1, "maxItems": 100}},
            ["ids"],
        ),
        (
            "fleet_reply",
            "Reply to an inbox message, durably spooling when offline.",
            {"id": text, "body": text},
            ["id", "body"],
        ),
        (
            "fleet_sent",
            "Read one page of sent messages and their acknowledgement status.",
            {"limit": limit, "after": text},
            [],
        ),
        (
            "fleet_claims",
            "List repository claims without changing ownership; truncated is true when the list was cut.",
            {
                "all": boolean,
                "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 200},
            },
            [],
        ),
    ]
    return [
        {
            "name": name,
            "description": description,
            "inputSchema": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        }
        for name, description, properties, required in definitions
    ]


class _InvalidArguments(ValueError):
    pass


def _validate(value: Any, schema: dict[str, Any]) -> None:
    kind = schema["type"]
    valid = {
        "string": isinstance(value, str),
        "boolean": isinstance(value, bool),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "array": isinstance(value, list),
    }[kind]
    if not valid:
        raise _InvalidArguments("Invalid tool arguments: incorrect field type.")
    if kind == "integer" and not schema.get("minimum", value) <= value <= schema.get("maximum", value):
        raise _InvalidArguments("Invalid tool arguments: integer outside the permitted range.")
    if kind == "string" and len(value) < schema.get("minLength", 0):
        raise _InvalidArguments("Invalid tool arguments: a required string is empty.")
    if "enum" in schema and value not in schema["enum"]:
        raise _InvalidArguments("Invalid tool arguments: unsupported value.")
    if kind == "array":
        if not schema["minItems"] <= len(value) <= schema["maxItems"]:
            raise _InvalidArguments("Invalid tool arguments: provide 1 to 100 message IDs.")
        for item in value:
            _validate(item, schema["items"])


def _message_id(value: str, *, allow_zero: bool = False) -> int:
    if (
        not value.isascii()
        or not value.isdecimal()
        or len(value) > 19
        or not (0 if allow_zero else 1) <= int(value) <= MAX_SQLITE_INTEGER
    ):
        raise _InvalidArguments(
            "Invalid tool arguments: IDs and cursors must be decimal strings within SQLite's range."
        )
    return int(value)


# Message fields the hub authenticates, assigns or validates against a fixed
# set. Every other string in a message record is sender-chosen (subject, ref,
# thread, sender_label, client_id, and any field added later), so it goes inside
# the fence with the body instead of being named one at a time.
_MESSAGE_STRUCTURAL_FIELDS = frozenset(
    {"id", "sender", "recipient", "acked_by", "priority", "kind", "reply_to", "created_at", "acked_at"}
)
_FENCE_ORDER = ("subject", "ref", "thread", "sender_label", "client_id")


def _frame(value: Any) -> Any:
    """Frame every sender-chosen message field in one data block with the body."""
    if isinstance(value, list):
        return [_frame(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: _frame(item) for key, item in value.items()}
    if isinstance(value.get("body"), str):
        peer_keys = [key for key in value if key != "body" and key not in _MESSAGE_STRUCTURAL_FIELDS]
        peer_keys.sort(key=lambda key: (_FENCE_ORDER.index(key) if key in _FENCE_ORDER else len(_FENCE_ORDER), key))
        # Every sender-chosen field leaves the top level; text goes into the fence.
        peer_values = [(key, result.pop(key)) for key in peer_keys]
        lines = [f"{key}: {item}" for key, item in peer_values if isinstance(item, str)]
        text = "\n".join([*lines, "body:", value["body"]])
        result["body"] = wrap_untrusted(text, source_kind="peer-message")
        signal = scan_untrusted(text)
        # Excerpts would repeat peer instructions outside the fence.
        result["scan"] = {"flagged": signal.flagged, "count": signal.count}
    return result


# Directory and claim records carry text that nodes choose (repo_label, branch,
# claim target, harness, session ids). Only fields the hub authenticates or
# assigns pass through unchanged; every other string is fenced, whatever it
# looks like, so a node cannot place instructions outside the fence.
_STRUCTURAL_FIELDS = frozenset(
    {"node_id", "principal_id", "owner_node", "state", "expired", "truncated", "total", "oversized"}
)


def _structural(key: str) -> bool:
    return key in _STRUCTURAL_FIELDS or key.endswith("_at")


def _frame_directory(value: Any, key: str | None = None) -> Any:
    if isinstance(value, list):
        return [_frame_directory(item, key) for item in value]
    if isinstance(value, dict):
        return {name: _frame_directory(item, name) for name, item in value.items()}
    if isinstance(value, str) and value and not (key is not None and _structural(key)):
        return {
            "untrusted": wrap_untrusted(value, source_kind="peer-message"),
            "flagged": scan_untrusted(value).flagged,
        }
    return value


def poll_inbox(
    *,
    wait_secs: int,
    unacked_only: bool,
    authority: messages.MessageAuthority,
    after: int = 0,
    limit: int | None = None,
) -> dict[str, Any]:
    """Long-poll until the deadline, with bounded backoff for old or unavailable hubs."""
    deadline = monotonic() + wait_secs

    def read(unacked: bool, *, wait_ms: int = 0) -> dict[str, Any]:
        remaining = deadline - monotonic()
        return messages.list_messages(
            after=after,
            unacked=unacked,
            wait_ms=wait_ms,
            authority=authority,
            timeout=min(fleet_client.FLUSH_TIMEOUT_SECONDS, max(remaining, 2.0)) if wait_secs else None,
        )

    history = read(unacked_only)
    if not wait_secs:
        return history
    # Decide on the rows the caller will see after its limit, not the whole page.
    visible = history["messages"][:limit] if limit is not None else history["messages"]
    if any(row.get("acked_at") is None for row in visible):
        return history
    if any(row.get("acked_at") is None for row in history["messages"]):
        # Unread mail sits past the caller's limit: return the unread page instead,
        # or the first page if that read fails, as the wait loop does.
        try:
            return read(True)
        except fleet_client.FleetClientError:
            return history
    last_good = history
    while (remaining := deadline - monotonic()) > 0:
        chunk = int(min(remaining, MAX_WAIT_MS / 1000) * 1000)
        if chunk == 0:
            break
        started = monotonic()
        try:
            page = read(True, wait_ms=chunk)
        except fleet_client.FleetClientError:
            break
        if page["messages"]:
            return page
        if unacked_only:
            last_good = page
        # Older hubs ignore wait_ms; do not spin on their immediate empty replies.
        if monotonic() - started < chunk / 2000:
            break

    delay = 1.0
    while (remaining := deadline - monotonic()) > 0:
        sleep(min(delay, remaining))
        if monotonic() >= deadline:
            break
        try:
            page = read(True)
        except fleet_client.FleetClientError:
            return last_good
        if page["messages"]:
            return page
        if unacked_only:
            last_good = page
        delay = min(delay * 2, 5)
    return history


def _page_limit(payload: dict[str, Any], limit: int) -> dict[str, Any]:
    rows = payload["messages"][:limit]
    return {**payload, "messages": rows, "next_cursor": str(rows[-1]["id"] if rows else payload["next_cursor"])}


def _tool_error(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, messages.FleetMessageHubError):
        status = exc.status if isinstance(exc.status, int) and 400 <= exc.status <= 599 else None
        code = exc.code if isinstance(exc.code, str) and exc.code in _SAFE_ERROR_CODES else "hub_rejected"
        transient = (
            status in (408, 425, 429)
            or (status is not None and status >= 500)
            or code
            in {
                "hub_unavailable",
                "message_recipient_inactive",
                "message_timestamp_future",
            }
        )
        return {
            "error": "Fleet hub operation failed.",
            "code": code,
            "status": status,
            "transient": transient,
            "permanent": not transient,
        }
    return {"error": "Fleet operation failed. Check hub connectivity and credential permissions."}


class FleetMcp:
    def __init__(self, *, token_file: str | None = None, events: bool = False) -> None:
        self.token_file = token_file
        self.events = events

    def callbacks(self) -> dict[str, Any]:
        callbacks: dict[str, Any] = {
            "server_name": "brigade-fleet",
            "list_resources": lambda: [],
            "read_resource": lambda _uri: (None, None),
            "list_tools": tool_specs,
            "call_tool": self.call,
        }
        if self.events:
            from .fleet_mcp_events import FleetEvents

            callbacks["extension"] = FleetEvents(self._authority).extension()
        return callbacks

    def call(self, name: str, arguments: dict[str, Any]) -> tuple[object, bool]:
        try:
            specs = {spec["name"]: spec["inputSchema"] for spec in tool_specs()}
            if name not in specs:
                raise _InvalidArguments("Unknown fleet tool.")
            schema = specs[name]
            if set(arguments) - schema["properties"].keys() or set(schema["required"]) - arguments.keys():
                raise _InvalidArguments("Invalid tool arguments: unsupported or missing fields.")
            for key, value in arguments.items():
                _validate(value, schema["properties"][key])
            # Validate every ID before acknowledging any of a batch.
            ids = [_message_id(item) for item in arguments.get("ids", [])]
            message_id = _message_id(arguments["id"]) if "id" in arguments else None
            reply_to = _message_id(arguments["reply_to"]) if "reply_to" in arguments else None
            if "after" in arguments:
                _message_id(arguments["after"], allow_zero=True)
            payload = self._call(name, arguments, ids, message_id, reply_to)
            return _frame(payload), False
        except _InvalidArguments as exc:
            return {"error": str(exc)}, True
        except Exception as exc:
            # Upstream failures may contain URLs, credentials, or attacker-controlled text.
            return _tool_error(exc), True

    def _authority(self) -> messages.MessageAuthority:
        # Same capture the public message functions use for a token file.
        return messages._authority(None, self.token_file, None)

    def _call(
        self, name: str, args: dict[str, Any], ids: list[int], message_id: int | None, reply_to: int | None
    ) -> dict[str, Any]:
        # One captured credential for the whole tool call, as the message layer requires.
        authority = self._authority()
        if name == "fleet_who":
            return _frame_directory(messages._online({"action": "who", **args}, authority=authority))
        if name == "fleet_claims":
            return _frame_directory(messages._online({"action": "claims", **args}, authority=authority))
        if name == "fleet_send":
            result = messages.send(
                recipient=args["to"],
                body=args["body"],
                subject="Fleet message",
                reference=None,
                priority="normal",
                kind=args.get("kind"),
                thread=args.get("thread"),
                reply_to=reply_to,
                authority=authority,
            )
        elif name == "fleet_reply":
            assert message_id is not None
            result = messages.reply(
                message_id,
                body=args["body"],
                subject=None,
                reference=None,
                priority="normal",
                authority=authority,
            )
        elif name == "fleet_ack":
            results = []
            acked = []
            for item in ids:
                try:
                    message = messages.ack(item, authority=authority)["message"]
                except Exception as exc:
                    results.append({"id": str(item), "ok": False, **_tool_error(exc)})
                else:
                    acked.append(message)
                    results.append({"id": str(item), "ok": True, "message": message})
            return {"messages": acked, "results": results}
        elif name == "fleet_sent":
            return _page_limit(
                messages.sent(after=int(args.get("after", "0")), authority=authority), args.get("limit", 100)
            )
        elif args.get("count_only", False):
            return messages.unread_count(authority=authority)
        else:
            return _page_limit(
                poll_inbox(
                    wait_secs=args.get("wait_secs", 0),
                    unacked_only=args.get("unacked_only", True),
                    authority=authority,
                    after=int(args.get("after", "0")),
                    limit=args.get("limit", 100),
                ),
                args.get("limit", 100),
            )
        if reply_to is not None and not result.get("queued") and result.get("message", {}).get("reply_to") != reply_to:
            result = {**result, "reply_to_ignored": True}
        return {**result, "delivery": "spooled" if result.get("queued") else "delivered"}


def serve_mcp(
    *,
    http: bool = False,
    host: str | None = None,
    port: int | None = None,
    token_file: str | None = None,
    events: bool = False,
) -> int:
    callbacks = FleetMcp(token_file=token_file, events=events).callbacks()
    if http:
        return mcp_server.run_http_from_env(host=host, port=port, default_port=DEFAULT_PORT, **callbacks)
    if host is not None or port is not None:
        import sys

        print("error: --host and --port require --http", file=sys.stderr)
        return 2
    return mcp_server.serve_stdio(**callbacks)


def register(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("mcp", help="Serve Fleet Hub messaging, presence and claims over MCP.")
    transport = parser.add_mutually_exclusive_group()
    transport.add_argument("--http", action="store_true", help="Serve loopback HTTP with BRIGADE_MCP_HTTP_TOKEN.")
    transport.add_argument("--stdio", action="store_true", help="Serve stdio (the default).")
    parser.add_argument("--host", default=None, help="With --http: loopback address (default 127.0.0.1).")
    parser.add_argument("--port", type=int, default=None, help=f"With --http: port (default {DEFAULT_PORT}).")
    parser.add_argument(
        "--principal-token-file", help="Protected message principal token file (same as msg --token-file)."
    )
    parser.add_argument(
        "--events",
        action="store_true",
        help="Serve MCP Events (events/list, events/subscribe, events/unsubscribe); the hub must enable them too.",
    )
    parser.set_defaults(
        func=lambda args: serve_mcp(
            http=args.http,
            host=args.host,
            port=args.port,
            token_file=args.principal_token_file,
            events=args.events,
        )
    )
