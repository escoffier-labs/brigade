"""Closed t3-dispatch tool contracts: names, limits, errors, and argument parsers."""

from __future__ import annotations

import json
from functools import partial
import re
from typing import Any, Callable

from ..hub_messages import HUB_CODES, INBOX_TOOLS

PACK_ID = "t3-dispatch"
DEFAULT_BIND = "127.0.0.1:8778"
SERVICE_NAME = "grokbot-t3-dispatch"
TOOLS = INBOX_TOOLS | frozenset(
    {
        "t3_approve",
        "t3_answer",
        "t3_capacity",
        "t3_followup",
        "t3_interrupt",
        "t3_recent",
        "t3_status",
        "t3_submit",
        "t3_wait",
    }
)
TERMINAL_STATES = frozenset({"succeeded", "failed", "interrupted"})
# Provisional marker while a follow-up to a finished request is in flight; only that follow-up may replace it.
REOPENING_STATE = "reopening"
REOPENING_TTL_SECONDS = 600
NOTIFY_STATES = TERMINAL_STATES | frozenset({"needs_attention", "unknown"})
APPROVAL_DECISIONS = frozenset({"accept", "acceptForSession", "decline", "cancel"})
INTERACTION_MODES = frozenset({"default", "plan"})
HOST_AUTO = "auto"
MAX_REQUEST_BYTES = 65_536
PUBLIC_RESULT_LIMIT = 131_072
MAX_ANSWERS_BYTES = 8_192
WAIT_DEFAULT_SECONDS = 30
WAIT_MAX_SECONDS = 60
RECENT_DEFAULT = 10
RECENT_MAX = 20
TITLE_MAX = 120
FLEET_TITLE_MAX = 120
CALLER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,47}$")
HANDLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")
ERROR_MESSAGES = {
    "invalid_request": "Tool input failed validation",
    "hub-not-configured": "Fleet Hub messaging is not configured",
    "hub-error": "Fleet Hub messaging refused the request or is unavailable",
    "dispatch-disabled": "Dispatch is turned off",
    "repository-not-allowed": "Repository is not on the dispatch allowlist",
    "host-not-allowed": "Host is not on the dispatch allowlist",
    "seat-not-allowed": "Seat is not on the dispatch allowlist",
    "prompt-too-large": "Prompt exceeds the dispatch size limit",
    "open-limit": "Too many dispatched requests are still open",
    "daily-limit": "Daily dispatch limit reached",
    "unknown-request": "Request was not launched through this connector",
    "base-ref-unresolved": "Base ref did not resolve to a commit",
    "no-eligible-host": "No allowlisted host has free capacity",
    "fleet-error": "t3-fleet refused the request",
    "submit-unknown": "Submit outcome is unknown; do not resubmit. Ask the operator to inspect t3-fleet and recover",
    "unavailable": "Dispatch is unavailable",
    "unsupported-platform": "T3 dispatch is unsupported on Windows. POSIX owner-only storage is required",
}


_FIELD_RE = re.compile(r"[^A-Za-z0-9_.:-]")
FIELD_MAX = 64


def _bounded_field(value: object) -> str | None:
    """Keep upstream codes and reasons short and plain so error results stay inside the public limit."""
    if not isinstance(value, str) or not value:
        return None
    return _FIELD_RE.sub("-", value)[:FIELD_MAX]


class T3DispatchError(ValueError):
    """Stable public failure. Messages never include prompts, paths, or secrets."""

    def __init__(
        self,
        code: str,
        *,
        hub_code: str | None = None,
        fleet_code: str | None = None,
        fleet_reason: str | None = None,
        request_id: str | None = None,
    ):
        if code not in ERROR_MESSAGES:
            code = "unavailable"
        self.code = code
        self.hub_code = hub_code if isinstance(hub_code, str) and hub_code in HUB_CODES else None
        self.fleet_code = _bounded_field(fleet_code)
        self.fleet_reason = _bounded_field(fleet_reason)
        self.request_id = request_id if isinstance(request_id, str) and HANDLE_RE.fullmatch(request_id) else None
        super().__init__(ERROR_MESSAGES[code])

    def public_error(self) -> dict[str, dict[str, str]]:
        error = {"code": self.code, "message": ERROR_MESSAGES[self.code]}
        if self.hub_code is not None:
            error["hub_code"] = self.hub_code
        if self.fleet_code is not None:
            error["fleet_code"] = self.fleet_code
        if self.fleet_reason is not None:
            error["fleet_reason"] = self.fleet_reason
        if self.request_id is not None:
            error["request_id"] = self.request_id
        return {"error": error}

    def __repr__(self) -> str:
        return f"T3DispatchError({self.code!r})"


def _invalid() -> T3DispatchError:
    return T3DispatchError("invalid_request")


def _object(raw: object, required: frozenset[str], optional: frozenset[str] = frozenset()) -> dict[str, Any]:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise _invalid()
    keys = set(raw)
    if not required <= keys or keys - required - optional:
        raise _invalid()
    if any(raw[key] is None for key in required):
        raise _invalid()
    return {key: value for key, value in raw.items() if value is not None}


def _match(value: object, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise _invalid()
    return value


def _handle(value: object) -> str:
    return _match(value, HANDLE_RE)


def _name(value: object) -> str:
    return _match(value, NAME_RE)


def _caller(value: object) -> str:
    return _match(value, CALLER_RE)


def _ref(value: object) -> str:
    ref = _match(value, REF_RE)
    if ".." in ref or ref.endswith("/") or ref.endswith(".lock") or "//" in ref:
        raise _invalid()
    return ref


def _encodable(value: str) -> str:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise _invalid() from exc
    return value


def _text(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise _invalid()
    return _encodable(value)


def _title(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > TITLE_MAX:
        raise _invalid()
    if any(ch in value for ch in "\r\n\x00"):
        raise _invalid()
    return _encodable(value)


def _bounded_int(value: object, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise _invalid()
    return value


def parse_capacity(raw: object) -> dict[str, Any]:
    _object(raw, frozenset())
    return {}


def parse_submit(raw: object) -> dict[str, Any]:
    data = _object(
        raw,
        frozenset({"repository", "prompt", "title", "caller"}),
        frozenset({"host", "seat", "base_ref", "interaction_mode"}),
    )
    parsed: dict[str, Any] = {
        "repository": _name(data["repository"]),
        "prompt": _text(data["prompt"]),
        "title": _title(data["title"]),
        "caller": _caller(data["caller"]),
        "host": HOST_AUTO,
        "seat": None,
        "base_ref": None,
        "interaction_mode": "default",
    }
    if "host" in data:
        parsed["host"] = HOST_AUTO if data["host"] == HOST_AUTO else _name(data["host"])
    if "seat" in data:
        parsed["seat"] = _name(data["seat"])
    if "base_ref" in data:
        parsed["base_ref"] = _ref(data["base_ref"])
    if "interaction_mode" in data:
        if data["interaction_mode"] not in INTERACTION_MODES:
            raise _invalid()
        parsed["interaction_mode"] = data["interaction_mode"]
    return parsed


def parse_request_only(raw: object) -> dict[str, Any]:
    data = _object(raw, frozenset({"request_id"}))
    return {"request_id": _handle(data["request_id"])}


def parse_wait(raw: object) -> dict[str, Any]:
    data = _object(raw, frozenset({"request_id"}), frozenset({"timeout_s"}))
    timeout = data.get("timeout_s", WAIT_DEFAULT_SECONDS)
    return {"request_id": _handle(data["request_id"]), "timeout_s": _bounded_int(timeout, 1, WAIT_MAX_SECONDS)}


def parse_followup(raw: object) -> dict[str, Any]:
    data = _object(raw, frozenset({"request_id", "message"}))
    return {"request_id": _handle(data["request_id"]), "message": _text(data["message"])}


def parse_answer(raw: object) -> dict[str, Any]:
    data = _object(raw, frozenset({"request_id", "input_id", "answers"}))
    answers = data["answers"]
    if not isinstance(answers, dict) or not answers:
        raise _invalid()
    for key, value in answers.items():
        _handle(key)
        if isinstance(value, str):
            continue
        if isinstance(value, list) and value and all(isinstance(item, str) for item in value):
            continue
        raise _invalid()
    if len(json.dumps(answers, separators=(",", ":")).encode("utf-8")) > MAX_ANSWERS_BYTES:
        raise _invalid()
    return {"request_id": _handle(data["request_id"]), "input_id": _handle(data["input_id"]), "answers": answers}


def parse_approve(raw: object) -> dict[str, Any]:
    data = _object(raw, frozenset({"request_id", "approval_id", "decision"}))
    if data["decision"] not in APPROVAL_DECISIONS:
        raise _invalid()
    return {
        "request_id": _handle(data["request_id"]),
        "approval_id": _handle(data["approval_id"]),
        "decision": data["decision"],
    }


def parse_recent(raw: object) -> dict[str, Any]:
    data = _object(raw, frozenset(), frozenset({"caller", "limit"}))
    caller = _caller(data["caller"]) if "caller" in data else None
    limit = _bounded_int(data.get("limit", RECENT_DEFAULT), 1, RECENT_MAX)
    return {"caller": caller, "limit": limit}


PARSERS: dict[str, Callable[[object], dict[str, Any]]] = {
    "t3_approve": parse_approve,
    "t3_answer": parse_answer,
    "t3_capacity": parse_capacity,
    "t3_followup": parse_followup,
    "t3_interrupt": parse_request_only,
    "t3_recent": parse_recent,
    "t3_status": parse_request_only,
    "t3_submit": parse_submit,
    "t3_wait": parse_wait,
}


def parse_inbox(name: str, raw: object) -> dict[str, Any]:
    # Reuse Dot's authoritative field validators and defaults, including its byte cap.
    from ..dot.contracts import DotError, TOOL_SCHEMAS

    schema = TOOL_SCHEMAS[name]
    data = _object(
        raw, frozenset(key for key, field in schema.fields.items() if field.required), frozenset(schema.fields)
    )
    try:
        return {
            key: field.validator(data[key]) if key in data else field.default for key, field in schema.fields.items()
        }
    except DotError:
        raise _invalid() from None


for _inbox_name in INBOX_TOOLS:
    PARSERS[_inbox_name] = partial(parse_inbox, _inbox_name)
