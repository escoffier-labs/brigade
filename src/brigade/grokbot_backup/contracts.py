"""Closed Backup Steward public contracts and stable errors."""

from __future__ import annotations

import re
from typing import Any, Mapping

PACK_ID = "backup-steward"
DEFAULT_BIND = "127.0.0.1:8772"
TOOLS = frozenset(
    {
        "backup_overview",
        "backup_target_status",
        "backup_restore_readiness",
        "backup_operation_status",
        "backup_propose_action",
        "backup_execute_action",
    }
)
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$")
REVISION_RE = re.compile(r"^[0-9a-f]{64}$")
ERROR_MESSAGES = {
    "invalid_request": "Tool input failed validation",
    "denied": "Backup request was denied",
    "not_found": "Backup resource was not found",
    "unavailable": "Backup observation is unavailable",
    "timeout": "Backup observation timed out",
    "protocol_error": "Backup observation failed",
}
STATUSES = frozenset({"ok", "partial", "unavailable", "denied", "invalid"})
HEALTH_CLASSES = frozenset({"healthy", "degraded", "unhealthy", "unknown"})
LOCK_CLASSES = frozenset({"active_operation", "stale_lock", "unknown_lock", "none"})
OPERATION_STATES = frozenset({"queued", "running", "succeeded", "failed", "cancelled"})
ACTION_IDS = frozenset({"run-backup", "run-integrity-check", "run-restore-rehearsal"})
FINDING_KINDS = frozenset(
    {
        "snapshot-stale",
        "scheduler-unhealthy",
        "integrity-failed",
        "restore-evidence-stale",
        "active-operation",
        "stale-lock",
        "repository-sweep-failed",
        "capacity-warning",
    }
)
SEVERITY_CLASSES = frozenset({"info", "warning", "critical", "unknown"})


class BackupError(Exception):
    """Stable public Backup failure. Messages never include paths or child output."""

    def __init__(self, code: str, message: str | None = None):
        self.code = code
        self.message = message if message is not None else ERROR_MESSAGES[code]
        super().__init__(self.message)

    def public_error(self) -> dict[str, dict[str, str]]:
        return {"error": {"code": self.code, "message": ERROR_MESSAGES[self.code]}}

    def __repr__(self) -> str:
        return f"BackupError({self.code!r})"


def parse_identifier(value: object, *, maximum: int = 64) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= maximum or not IDENTIFIER_RE.fullmatch(value):
        raise BackupError("invalid_request", ERROR_MESSAGES["invalid_request"])
    return value


def parse_action_id(value: object) -> str:
    action_id = parse_identifier(value)
    if action_id not in ACTION_IDS:
        raise BackupError("invalid_request", ERROR_MESSAGES["invalid_request"])
    return action_id


def is_offset_datetime(value: object) -> bool:
    return isinstance(value, str) and DATETIME_RE.fullmatch(value) is not None


def parse_overview_input(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw:
        raise BackupError("invalid_request", ERROR_MESSAGES["invalid_request"])
    return {}


def parse_target_input(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict) or set(raw) != {"target_alias"}:
        raise BackupError("invalid_request", ERROR_MESSAGES["invalid_request"])
    return {"target_alias": parse_identifier(raw.get("target_alias"))}


def parse_operation_input(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict) or set(raw) != {"operation_id"}:
        raise BackupError("invalid_request", ERROR_MESSAGES["invalid_request"])
    return {"operation_id": parse_identifier(raw.get("operation_id"))}


def parse_propose_input(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict) or set(raw) != {"target_alias", "action_id", "finding_id"}:
        raise BackupError("invalid_request", ERROR_MESSAGES["invalid_request"])
    return {
        "target_alias": parse_identifier(raw.get("target_alias")),
        "action_id": parse_action_id(raw.get("action_id")),
        "finding_id": parse_identifier(raw.get("finding_id")),
    }


def parse_execute_input(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict) or set(raw) != {"proposal_id"}:
        raise BackupError("invalid_request", ERROR_MESSAGES["invalid_request"])
    return {"proposal_id": parse_identifier(raw.get("proposal_id"))}


def omit_undefined(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item is not None}
