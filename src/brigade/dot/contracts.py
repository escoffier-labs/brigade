from dataclasses import dataclass, field
import re
import unicodedata
from typing import Any, Callable

from ..hub_messages import MAX_MESSAGE_BYTES as MAX_MESSAGE_BYTES

PACK_ID = "dot"
SERVICE_NAME = "dot-t3"
SERVER_VERSION = "0.28.0"
DEFAULT_BIND = "127.0.0.1:8779"
MAX_PROMPT_BYTES = 24576
MAX_CARD_FILE_BYTES = 1024 * 1024
MAX_HANDOFF_BYTES = 16384


ERROR_MESSAGES = {
    "recipient_not_permitted": "The recipient is outside the dot's send grants. Nothing was sent or queued, "
    "and retrying will not help.",
    "recipient_quota": "The dot's send quota for this recipient is exhausted. Nothing was sent or queued; "
    "do not retry in a loop.",
    "hub_credential_invalid": "The bridge's Fleet Hub credential file is unusable. This is a local setup fault, "
    "not a hub outage. Nothing was sent or queued, and retrying will not help.",
    "client_id_conflict": "This client_id is already queued with different content. Nothing new was queued; "
    "use a new client_id for a different message.",
    "send_limit_reached": "This connector's hourly message_send limit is reached. Nothing was sent or queued; "
    "wait for the hour to roll over and do not retry in a loop.",
    "messaging_disabled": "Hub messaging is turned off for this connector. Nothing was sent or queued, "
    "and retrying will not help.",
}


class DotError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def require(condition: bool, code: str = "invalid_request") -> None:
    if not condition:
        raise DotError(code)


def _pattern(regex: str) -> Callable[[Any], str]:
    compiled = re.compile(regex)

    def validate(value: Any) -> str:
        require(isinstance(value, str) and compiled.fullmatch(value) is not None)
        return value

    return validate


def _text(
    *, max_chars: int | None = None, max_bytes: int | None = None, multiline: bool = False
) -> Callable[[Any], str]:
    def validate(value: Any) -> str:
        require(isinstance(value, str) and bool(value.strip()))
        if max_chars is not None:
            require(len(value) <= max_chars)
        if max_bytes is not None:
            try:
                require(len(value.encode("utf-8")) <= max_bytes)
            except UnicodeError:
                raise DotError("invalid_request") from None
        allowed = "\n\t\r" if multiline else ""
        require(not any(unicodedata.category(char) == "Cc" and char not in allowed for char in value))
        return value

    return validate


def _integer(minimum: int, maximum: int) -> Callable[[Any], int]:
    def validate(value: Any) -> int:
        require(type(value) is int and minimum <= value <= maximum)
        return value

    return validate


def _boolean(value: Any) -> bool:
    require(type(value) is bool)
    return value


def _choice(*options: str) -> Callable[[Any], str]:
    def validate(value: Any) -> str:
        require(isinstance(value, str) and value in options)
        return value

    return validate


def encoded_size(value: Any) -> int:
    import json

    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8"))


def answers_value(value: Any) -> dict[str, Any]:
    require(isinstance(value, dict) and 1 <= len(value) <= 32)
    require(all(isinstance(key, str) and 0 < len(key) <= 128 for key in value))
    try:
        require(encoded_size(value) <= 8192)
    except (TypeError, ValueError):
        raise DotError("invalid_request") from None
    return value


def task_report(value: Any) -> dict[str, Any]:
    from ..fleet_dot import DotReportError, parse_report

    require(isinstance(value, dict) and type(value.get("version")) is int and value["version"] == 2)
    try:
        parse_report(value)
    except DotReportError:
        raise DotError("invalid_request") from None
    return value


def message_ids(value: Any) -> list[int]:
    require(isinstance(value, list) and 1 <= len(value) <= 100)
    require(all(type(item) is int and 0 < item < 2**62 for item in value))
    require(len(set(value)) == len(value))
    return value


def status_message_ids(value: Any) -> list[int]:
    result = message_ids(value)
    require(len(result) <= 50)
    return result


def card_name(value: Any) -> str:
    require(isinstance(value, str))
    if value.endswith(".md"):
        value = value[:-3]
    require(re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", value) is not None and ".." not in value)
    return value


def handoff_content(value: Any) -> str:
    _text(max_bytes=MAX_HANDOFF_BYTES, multiline=True)(value)
    require(value.startswith("# Memory Handoff\n"))
    return value


def _required(validator: Callable[[Any], Any]) -> "Field":
    return Field(validator, required=True)


def _optional(validator: Callable[[Any], Any], default: Any = None) -> "Field":
    return Field(validator, default=default)


@dataclass(frozen=True)
class Field:
    validator: Callable[[Any], Any]
    required: bool = False
    default: Any = None


@dataclass(frozen=True)
class ToolSchema:
    fields: dict[str, Field] = field(default_factory=dict)
    gated: bool = False
    read_only: bool = True


HOST = _pattern(r"[a-z0-9][a-z0-9-]{0,31}")
SUBMIT_HOST = _pattern(r"auto|[a-z0-9][a-z0-9-]{0,31}")
REPOSITORY = _pattern(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
SEAT = _pattern(r"[a-z0-9][a-z0-9_-]{0,63}")
WORKLOAD = _pattern(r"[a-z0-9][a-z0-9-]{0,63}")
REVISION = _pattern(r"[0-9a-f]{40}")
REQUEST_ID = _pattern(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}")
CONTROL_ID = _pattern(r"[A-Za-z0-9][A-Za-z0-9._:/%-]{0,511}")
THREAD = _pattern(r"[A-Za-z0-9][A-Za-z0-9._:/#-]{0,127}")
SLUG = _pattern(r"[a-z0-9][a-z0-9-]{2,63}")
TITLE = _text(max_chars=200)
REASON = _text(max_chars=300)
PROMPT = _text(max_bytes=MAX_PROMPT_BYTES, multiline=True)
QUERY = _text(max_chars=256)
MESSAGE_BODY = _text(max_bytes=MAX_MESSAGE_BYTES, multiline=True)
CONSUMER = _choice("brigade-run", "t3-fleet")
WORK_ID = _pattern(r"wl-[0-9a-f]{6,64}")
WORK_STATUS = _pattern(r"[a-z][a-z_-]{0,31}")
BURN_CURSOR = _pattern(r"[A-Za-z0-9_-]{1,2048}={0,2}")

TOOL_SCHEMAS = {
    "dot_t3_capabilities": ToolSchema(),
    "fleet_capacity": ToolSchema({"host": _optional(HOST)}),
    "fleet_doctor": ToolSchema({"host": _optional(HOST), "repository": _optional(REPOSITORY)}),
    "fleet_submit": ToolSchema(
        {
            "repository": _required(REPOSITORY),
            "title": _required(TITLE),
            "prompt": _required(PROMPT),
            "host": _optional(SUBMIT_HOST, "auto"),
            "seat": _optional(SEAT),
            "workload": _optional(WORKLOAD),
            "override_reason": _optional(REASON),
            "source_revision": _optional(REVISION),
            "allow_queue": _optional(_boolean, False),
            "read_only": _optional(_boolean, False),
        },
        gated=True,
        read_only=False,
    ),
    "fleet_status": ToolSchema({"request_id": _required(REQUEST_ID), "include_output": _optional(_boolean, False)}),
    "fleet_wait": ToolSchema(
        {
            "request_id": _required(REQUEST_ID),
            "timeout_seconds": _optional(_integer(1, 60), 30),
            "include_output": _optional(_boolean, False),
        }
    ),
    "fleet_followup": ToolSchema(
        {"request_id": _required(REQUEST_ID), "message": _required(PROMPT), "read_only": _optional(_boolean, False)},
        gated=True,
        read_only=False,
    ),
    "fleet_answer": ToolSchema(
        {"request_id": _required(REQUEST_ID), "input_id": _required(CONTROL_ID), "answers": _required(answers_value)},
        gated=True,
        read_only=False,
    ),
    "fleet_approve": ToolSchema(
        {
            "request_id": _required(REQUEST_ID),
            "approval_id": _required(CONTROL_ID),
            "decision": _required(_choice("accept", "acceptForSession", "decline", "cancel")),
        },
        gated=True,
        read_only=False,
    ),
    "fleet_interrupt": ToolSchema({"request_id": _required(REQUEST_ID)}, gated=True, read_only=False),
    "fleet_recent": ToolSchema({"limit": _optional(_integer(1, 50), 20)}),
    "fleet_overview": ToolSchema({"include_terminal": _optional(_boolean, False)}),
    "memory_search": ToolSchema({"query": _required(QUERY), "limit": _optional(_integer(1, 20), 10)}),
    "memory_card": ToolSchema({"name": _required(card_name), "offset": _optional(_integer(0, MAX_CARD_FILE_BYTES), 0)}),
    "handoff_write": ToolSchema({"slug": _required(SLUG), "content": _required(handoff_content)}, read_only=False),
    "inbox_list": ToolSchema({"unacked_only": _optional(_boolean, True), "limit": _optional(_integer(1, 50), 20)}),
    "inbox_ack": ToolSchema({"ids": _required(message_ids)}, read_only=False),
    "message_send": ToolSchema(
        {
            "to": _required(_pattern(r"agent:[a-z][a-z0-9._-]{0,62}|[A-Za-z0-9._-]{1,128}")),
            "subject": _required(_text(max_bytes=200)),
            "body": _required(MESSAGE_BODY),
            "ref": _optional(THREAD),
            "priority": _optional(_choice("low", "normal", "high", "urgent"), "normal"),
            "thread": _optional(THREAD),
            "kind": _optional(_choice("message", "question", "completion"), "message"),
            "client_id": _optional(_pattern(r"[A-Za-z0-9_-]{1,64}")),
        },
        gated=True,
        read_only=False,
    ),
    "message_status": ToolSchema({"ids": _required(status_message_ids)}),
    "fleet_policy": ToolSchema({"since_hash": _optional(_pattern(r"[0-9a-f]{64}"))}),
    "skill_search": ToolSchema({"query": _required(QUERY), "limit": _optional(_integer(1, 50), 20)}),
    "skill_get": ToolSchema(
        {
            "name": _required(SLUG),
            "resource": _optional(_pattern(r"(?:SKILL\.md|[A-Za-z0-9_-]+(?:/[A-Za-z0-9_.-]+)+)"), "SKILL.md"),
            "offset": _optional(_integer(0, 1048576), 0),
        }
    ),
    "inbox_reply": ToolSchema(
        {"msg_id": _required(_integer(1, 2**63 - 1)), "body": _required(MESSAGE_BODY)}, read_only=False
    ),
    "fleet_report_session": ToolSchema(
        {"report": _required(task_report), "publish": _optional(_boolean, False)}, gated=True, read_only=False
    ),
    "fleet_sessions": ToolSchema(
        {"include_ended": _optional(_boolean, False), "limit": _optional(_integer(1, 100), 50)}
    ),
    "fleet_work_list": ToolSchema(
        {
            "source": _optional(_choice("github", "brigade", "native")),
            "status": _optional(WORK_STATUS),
            "limit": _optional(_integer(1, 50), 25),
        }
    ),
    "fleet_work_show": ToolSchema({"work_id": _required(WORK_ID)}),
    "fleet_work_next": ToolSchema({"consumer": _optional(CONSUMER), "workload": _optional(WORKLOAD)}),
    "fleet_work_burn": ToolSchema({"cursor": _optional(BURN_CURSOR), "limit": _optional(_integer(1, 50), 20)}),
    "fleet_models": ToolSchema({"consumer": _optional(CONSUMER, "brigade-run")}),
    "fleet_cloud": ToolSchema(
        {"include_released": _optional(_boolean, False), "limit": _optional(_integer(1, 100), 50)}
    ),
    "fleet_hub_policy": ToolSchema(),
    "fleet_digest": ToolSchema(),
}

TOOLS = frozenset(TOOL_SCHEMAS)
# Tools that start or steer work on operator machines, gated by dispatch_enabled.
DISPATCH_GATED_TOOLS = frozenset(
    {"fleet_submit", "fleet_followup", "fleet_answer", "fleet_approve", "fleet_interrupt", "fleet_report_session"}
)
# Hub messaging has its own gate, so a profile can send messages without dispatch.
MESSAGING_GATED_TOOLS = frozenset({"message_send"})
GATED_TOOLS = DISPATCH_GATED_TOOLS | MESSAGING_GATED_TOOLS

# Capability tools owned by a connector profile other than Dot. Dot's TOOL_SCHEMAS stays fixed.
PROFILE_TOOL_SCHEMAS = {"duet_capabilities": ToolSchema()}
ALL_TOOL_SCHEMAS = {**TOOL_SCHEMAS, **PROFILE_TOOL_SCHEMAS}
