"""Closed Fleet Steward public contracts and stable errors."""

from __future__ import annotations

import re
from typing import Any, Mapping

PACK_ID = "fleet-steward"
DEFAULT_BIND = "127.0.0.1:8771"
TOOLS = frozenset(
    {
        "fleet_overview",
        "host_status",
        "incident_bundle",
        "propose_remediation",
        "service_health",
        "execute_remediation",
    }
)
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
OPAQUE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_WAZUH_FINDING_ID = 128
DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$")
FORBIDDEN_REMEDIATION_INPUT = frozenset(
    {
        "address",
        "command",
        "credential",
        "environment",
        "hostname",
        "password",
        "path",
        "shell",
        "token",
        "username",
    }
)
WAZUH_PROPOSAL_FIELDS = frozenset(
    {
        "action_id",
        "blast_radius",
        "finding_revision",
        "maintenance_window_id",
        "rollback_id",
        "service_id",
        "target_alias",
        "verification_id",
        "wazuh_fingerprint",
    }
)
ERROR_MESSAGES = {
    "invalid_request": "Tool input failed validation",
    "denied": "Fleet request was denied",
    "not_found": "Fleet resource was not found",
    "unavailable": "Fleet observation is unavailable",
    "timeout": "Fleet observation timed out",
    "protocol_error": "Fleet observation failed",
}
STATUSES = frozenset({"ok", "partial", "unavailable", "denied", "invalid"})
TIERS = frozenset(
    {
        "infrastructure",
        "protected-status-only",
        "appliance-read-only",
        "indirect-only",
    }
)
HEALTH_CLASSES = frozenset({"healthy", "degraded", "unhealthy", "unknown"})
REACHABILITY = frozenset({"reachable", "unreachable", "unknown"})
SEVERITY_CLASSES = frozenset({"info", "warning", "critical", "unknown"})


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


def parse_wazuh_finding_id(value: object) -> str:
    return parse_identifier(value, maximum=MAX_WAZUH_FINDING_ID)


def parse_opaque_id(value: object) -> str:
    if not isinstance(value, str) or not OPAQUE_ID_RE.fullmatch(value):
        raise FleetError("invalid_request", ERROR_MESSAGES["invalid_request"])
    return value


def parse_fingerprint(value: object) -> str:
    if not isinstance(value, str) or not FINGERPRINT_RE.fullmatch(value):
        raise FleetError("invalid_request", ERROR_MESSAGES["invalid_request"])
    return value


def is_offset_datetime(value: object) -> bool:
    return isinstance(value, str) and DATETIME_RE.fullmatch(value) is not None


def parse_overview_input(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw:
        raise FleetError("invalid_request", ERROR_MESSAGES["invalid_request"])
    return {}


def parse_host_status_input(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict) or set(raw) != {"alias"}:
        raise FleetError("invalid_request", ERROR_MESSAGES["invalid_request"])
    return {"alias": parse_identifier(raw.get("alias"))}


def parse_service_health_input(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict) or set(raw) != {"service_id"}:
        raise FleetError("invalid_request", ERROR_MESSAGES["invalid_request"])
    return {"service_id": parse_identifier(raw.get("service_id"))}


def parse_incident_input(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict) or set(raw) != {"scope"}:
        raise FleetError("invalid_request", ERROR_MESSAGES["invalid_request"])
    return {"scope": parse_identifier(raw.get("scope"))}


def parse_propose_input(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict) or set(raw) != {"finding_id"} or set(raw) & FORBIDDEN_REMEDIATION_INPUT:
        raise FleetError("invalid_request", ERROR_MESSAGES["invalid_request"])
    return {"finding_id": parse_wazuh_finding_id(raw.get("finding_id"))}


def parse_execute_input(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict) or set(raw) != {"proposal_id"} or set(raw) & FORBIDDEN_REMEDIATION_INPUT:
        raise FleetError("invalid_request", ERROR_MESSAGES["invalid_request"])
    return {"proposal_id": parse_opaque_id(raw.get("proposal_id"))}


def omit_undefined(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item is not None}
