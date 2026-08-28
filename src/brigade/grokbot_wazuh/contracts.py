"""Closed Wazuh triage public contracts and stable errors."""

from __future__ import annotations

import re
from typing import Any, Mapping

PACK_ID = "wazuh-triage"
DEFAULT_BIND = "127.0.0.1:8774"
PRODUCER = "wazuh"
TOOLS = frozenset(
    {
        "wazuh_action_status",
        "wazuh_alert_status",
        "wazuh_classify",
        "wazuh_incident_bundle",
        "wazuh_ingest",
        "wazuh_propose_remediation",
    }
)
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
OPAQUE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")
DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$")
ERROR_MESSAGES = {
    "invalid_request": "Tool input failed validation",
    "denied": "Wazuh request was denied",
    "not_found": "Wazuh resource was not found",
    "unavailable": "Wazuh observation is unavailable",
    "timeout": "Wazuh observation timed out",
    "protocol_error": "Wazuh observation failed",
}
STATUSES = frozenset({"ok", "partial", "unavailable", "denied", "invalid"})
CATEGORIES = frozenset({"suppress", "watch", "escalate"})
SEVERITIES = frozenset({"info", "low", "medium", "warning", "high", "critical", "unknown"})
PROPOSAL_STATES = frozenset({"proposed", "expired", "denied"})
ALERT_KEYS = frozenset(
    {
        "agent_id",
        "decoder",
        "detail",
        "rule_description",
        "rule_groups",
        "rule_id",
        "rule_level",
        "timestamp",
    }
)
FORBIDDEN_ALERT_KEYS = frozenset(
    {
        "api_key",
        "authorization",
        "bearer",
        "command",
        "credential",
        "password",
        "path",
        "private_key",
        "secret",
        "token",
    }
)
INGEST_KEYS = frozenset({"alerts"})
FINDING_KEYS = frozenset(
    {
        "body",
        "content_digest",
        "finding_id",
        "observed_at",
        "producer",
        "revision",
        "severity",
        "source_digest",
        "source_ref",
        "title",
    }
)
MAX_ALERTS = 50
MAX_DETAIL_BYTES = 16_384
MAX_DESCRIPTION_CHARS = 200
MAX_GROUPS = 8
MAX_GROUP_CHARS = 64
MAX_IDENTIFIER = 64
MAX_RULE_ID = 32
MAX_FINDING_ID = 128
PATH_VALUE_RE = re.compile(r"(?:[A-Za-z]:[\\/]|/)")
COMMAND_VALUE_RE = re.compile(r"\b(?:curl|ssh|bash|wget|powershell|cmd)\b", re.IGNORECASE)


class WazuhError(Exception):
    """Stable public Wazuh failure. Messages never include paths or child output."""

    def __init__(self, code: str, message: str | None = None):
        self.code = code
        self.message = message if message is not None else ERROR_MESSAGES[code]
        super().__init__(self.message)

    def public_error(self) -> dict[str, dict[str, str]]:
        return {"error": {"code": self.code, "message": ERROR_MESSAGES[self.code]}}

    def __repr__(self) -> str:
        return f"WazuhError({self.code!r})"


def parse_identifier(value: object, *, maximum: int = MAX_IDENTIFIER) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= maximum or not IDENTIFIER_RE.fullmatch(value):
        raise WazuhError("invalid_request", ERROR_MESSAGES["invalid_request"])
    return value


def parse_finding_id(value: object) -> str:
    return parse_identifier(value, maximum=MAX_FINDING_ID)


def parse_opaque_id(value: object) -> str:
    if not isinstance(value, str) or not OPAQUE_ID_RE.fullmatch(value):
        raise WazuhError("invalid_request", ERROR_MESSAGES["invalid_request"])
    return value


def parse_fingerprint(value: object) -> str:
    if not isinstance(value, str) or not FINGERPRINT_RE.fullmatch(value):
        raise WazuhError("invalid_request", ERROR_MESSAGES["invalid_request"])
    return value


def is_offset_datetime(value: object) -> bool:
    return isinstance(value, str) and DATETIME_RE.fullmatch(value) is not None


def parse_ingest_input(raw: object) -> dict[str, list[dict[str, Any]]]:
    if not isinstance(raw, dict) or set(raw) != INGEST_KEYS:
        raise WazuhError("invalid_request", ERROR_MESSAGES["invalid_request"])
    alerts = raw.get("alerts")
    if not isinstance(alerts, list) or not alerts or len(alerts) > MAX_ALERTS:
        raise WazuhError("invalid_request", ERROR_MESSAGES["invalid_request"])
    return {"alerts": [_parse_alert(item) for item in alerts]}


def parse_alert_status_input(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw:
        raise WazuhError("invalid_request", ERROR_MESSAGES["invalid_request"])
    return {}


def parse_classify_input(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict) or set(raw) != {"fingerprint"}:
        raise WazuhError("invalid_request", ERROR_MESSAGES["invalid_request"])
    return {"fingerprint": parse_fingerprint(raw.get("fingerprint"))}


def parse_incident_input(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict) or set(raw) != {"scope"}:
        raise WazuhError("invalid_request", ERROR_MESSAGES["invalid_request"])
    return {"scope": parse_finding_id(raw.get("scope"))}


def parse_propose_input(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict) or set(raw) != {"finding_id"}:
        raise WazuhError("invalid_request", ERROR_MESSAGES["invalid_request"])
    return {"finding_id": parse_finding_id(raw.get("finding_id"))}


def parse_action_status_input(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict) or set(raw) != {"proposal_id"}:
        raise WazuhError("invalid_request", ERROR_MESSAGES["invalid_request"])
    return {"proposal_id": parse_opaque_id(raw.get("proposal_id"))}


def omit_undefined(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item is not None}


def _parse_alert(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise WazuhError("invalid_request", ERROR_MESSAGES["invalid_request"])
    extra = set(raw) - ALERT_KEYS
    if extra or ALERT_KEYS - set(raw) or extra & FORBIDDEN_ALERT_KEYS or set(raw) & FORBIDDEN_ALERT_KEYS:
        raise WazuhError("invalid_request", ERROR_MESSAGES["invalid_request"])
    rule_id = parse_identifier(raw.get("rule_id"), maximum=MAX_RULE_ID)
    level = raw.get("rule_level")
    if type(level) is not int or not 0 <= level <= 15:
        raise WazuhError("invalid_request", ERROR_MESSAGES["invalid_request"])
    description = _bounded_text(raw.get("rule_description"), maximum=MAX_DESCRIPTION_CHARS)
    _reject_command_or_path(description)
    groups = raw.get("rule_groups")
    if not isinstance(groups, list) or len(groups) > MAX_GROUPS:
        raise WazuhError("invalid_request", ERROR_MESSAGES["invalid_request"])
    parsed_groups = [parse_identifier(item, maximum=MAX_GROUP_CHARS) for item in groups]
    agent_id = parse_identifier(raw.get("agent_id"))
    decoder = parse_identifier(raw.get("decoder"))
    if not is_offset_datetime(raw.get("timestamp")):
        raise WazuhError("invalid_request", ERROR_MESSAGES["invalid_request"])
    detail = raw.get("detail")
    if not isinstance(detail, str) or "\x00" in detail or len(detail.encode("utf-8")) > MAX_DETAIL_BYTES:
        raise WazuhError("invalid_request", ERROR_MESSAGES["invalid_request"])
    return {
        "rule_id": rule_id,
        "rule_level": level,
        "rule_description": description,
        "rule_groups": parsed_groups,
        "agent_id": agent_id,
        "decoder": decoder,
        "timestamp": raw["timestamp"],
        "detail": detail,
    }


def _bounded_text(value: object, *, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or "\x00" in value:
        raise WazuhError("invalid_request", ERROR_MESSAGES["invalid_request"])
    return value


def _reject_command_or_path(value: str) -> None:
    if PATH_VALUE_RE.search(value) or COMMAND_VALUE_RE.search(value):
        raise WazuhError("invalid_request", ERROR_MESSAGES["invalid_request"])
