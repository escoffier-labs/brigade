"""Closed n8n-operator public contracts and stable errors."""

from __future__ import annotations

import re
from typing import Any, Mapping

PACK_ID = "n8n-operator"
DEFAULT_BIND = "127.0.0.1:8775"
TOOLS = frozenset(
    {
        "n8n_overview",
        "n8n_workflow_status",
        "n8n_execution_bundle",
        "n8n_propose_action",
        "n8n_action_status",
        "n8n_execute_action",
    }
)
TOOL_INPUT_KEYS = {
    "n8n_overview": frozenset(),
    "n8n_workflow_status": frozenset({"workflow_id"}),
    "n8n_execution_bundle": frozenset({"execution_id"}),
    "n8n_propose_action": frozenset({"action_id", "target_id"}),
    "n8n_action_status": frozenset({"proposal_id"}),
    "n8n_execute_action": frozenset({"proposal_id"}),
}
ACTION_IDS = frozenset(
    {
        "deactivate-workflow",
        "archive-workflow",
        "unarchive-workflow",
        "cancel-execution",
    }
)
EXCLUDED_ACTION_IDS = frozenset(
    {
        "activate-workflow",
        "trigger-workflow",
        "retry-execution",
        "edit-workflow",
        "delete-workflow",
        "delete-execution",
        "create-credential",
        "update-credential",
        "delete-credential",
        "create-tag",
        "delete-tag",
        "set-workflow-tags",
    }
)
ACTION_TARGET_TYPES = {
    "deactivate-workflow": "workflow",
    "archive-workflow": "workflow",
    "unarchive-workflow": "workflow",
    "cancel-execution": "execution",
}
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$")
REVISION_RE = re.compile(r"^[0-9a-f]{64}$")
ERROR_MESSAGES = {
    "invalid_request": "Tool input failed validation",
    "denied": "n8n request was denied",
    "not_found": "n8n resource was not found",
    "unavailable": "n8n observation is unavailable",
    "timeout": "n8n observation timed out",
    "protocol_error": "n8n observation failed",
}
STATUSES = frozenset({"ok", "partial", "unavailable", "denied", "invalid"})
ACTION_STATES = frozenset({"pending", "approved", "expired", "consumed", "failed", "unknown"})
PROPOSAL_TTL_MS = 15 * 60 * 1000
MAX_COLLECTION = 25
MAX_NAME_CHARS = 80
MAX_IDENTIFIER = 64
MAX_RESPONSE_BYTES = 65_536
MAX_STRING_CHARS = 200


class N8nError(Exception):
    """Stable public n8n failure. Messages never include paths or secret values."""

    def __init__(self, code: str, message: str | None = None):
        self.code = code
        self.message = message if message is not None else ERROR_MESSAGES[code]
        super().__init__(self.message)

    def public_error(self) -> dict[str, dict[str, str]]:
        return {"error": {"code": self.code, "message": ERROR_MESSAGES[self.code]}}

    def __repr__(self) -> str:
        return f"N8nError({self.code!r})"


def parse_safe_path_segment(value: object, *, maximum: int = MAX_IDENTIFIER) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= maximum or not SAFE_ID_RE.fullmatch(value):
        raise N8nError("invalid_request", ERROR_MESSAGES["invalid_request"])
    return value


def parse_identifier(value: object, *, maximum: int = MAX_IDENTIFIER) -> str:
    return parse_safe_path_segment(value, maximum=maximum)


def parse_action_id(value: object) -> str:
    action_id = parse_identifier(value)
    if action_id not in ACTION_IDS:
        raise N8nError("invalid_request", ERROR_MESSAGES["invalid_request"])
    return action_id


def target_type_for_action(action_id: str) -> str:
    parsed = parse_action_id(action_id)
    return ACTION_TARGET_TYPES[parsed]


def is_offset_datetime(value: object) -> bool:
    return isinstance(value, str) and DATETIME_RE.fullmatch(value) is not None


def parse_overview_input(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw:
        raise N8nError("invalid_request", ERROR_MESSAGES["invalid_request"])
    return {}


def parse_workflow_status_input(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict) or set(raw) != {"workflow_id"}:
        raise N8nError("invalid_request", ERROR_MESSAGES["invalid_request"])
    return {"workflow_id": parse_safe_path_segment(raw.get("workflow_id"))}


def parse_execution_bundle_input(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict) or set(raw) != {"execution_id"}:
        raise N8nError("invalid_request", ERROR_MESSAGES["invalid_request"])
    return {"execution_id": parse_safe_path_segment(raw.get("execution_id"))}


def parse_propose_input(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict) or set(raw) != {"action_id", "target_id"}:
        raise N8nError("invalid_request", ERROR_MESSAGES["invalid_request"])
    return {
        "action_id": parse_action_id(raw.get("action_id")),
        "target_id": parse_safe_path_segment(raw.get("target_id")),
    }


def parse_action_status_input(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict) or set(raw) != {"proposal_id"}:
        raise N8nError("invalid_request", ERROR_MESSAGES["invalid_request"])
    return {"proposal_id": parse_safe_path_segment(raw.get("proposal_id"))}


def parse_execute_input(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict) or set(raw) != {"proposal_id"}:
        raise N8nError("invalid_request", ERROR_MESSAGES["invalid_request"])
    return {"proposal_id": parse_safe_path_segment(raw.get("proposal_id"))}


def omit_undefined(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item is not None}
