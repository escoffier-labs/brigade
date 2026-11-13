"""Closed Fleet Steward public contracts and stable errors."""

from __future__ import annotations

import re
from typing import Any, Mapping

IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$")
ERROR_MESSAGES = {
    "invalid_request": "Tool input failed validation",
    "denied": "Fleet request was denied",
    "not_found": "Fleet resource was not found",
    "unavailable": "Fleet observation is unavailable",
    "secure-owner-write-unavailable": "Fleet observation is unavailable",
    "timeout": "Fleet observation timed out",
    "protocol_error": "Fleet observation failed",
}
TIERS = frozenset(
    {
        "infrastructure",
        "protected-status-only",
        "appliance-read-only",
        "indirect-only",
    }
)


class FleetError(Exception):
    """Stable public Fleet failure. Messages never include paths or child output."""

    def __init__(self, code: str, message: str | None = None):
        self.code = code
        self.message = message if message is not None else ERROR_MESSAGES[code]
        super().__init__(self.message)

    def public_error(self) -> dict[str, dict[str, str]]:
        return {"error": {"code": self.code, "message": ERROR_MESSAGES[self.code]}}

    def __repr__(self) -> str:
        return f"FleetError({self.code!r})"


def parse_identifier(value: object, *, maximum: int = 64) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= maximum or not IDENTIFIER_RE.fullmatch(value):
        raise FleetError("invalid_request", ERROR_MESSAGES["invalid_request"])
    return value


def is_offset_datetime(value: object) -> bool:
    return isinstance(value, str) and DATETIME_RE.fullmatch(value) is not None


def omit_undefined(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item is not None}
