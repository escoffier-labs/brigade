"""Public n8n-operator tool handlers and bounded projections."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

from .actions import N8nActionStore, target_revision
from .contracts import (
    ACTION_TARGET_TYPES,
    ERROR_MESSAGES,
    MAX_COLLECTION,
    MAX_NAME_CHARS,
    MAX_STRING_CHARS,
    N8nError,
    parse_action_status_input,
    parse_execute_input,
    parse_execution_bundle_input,
    parse_overview_input,
    parse_propose_input,
    parse_safe_path_segment,
    parse_workflow_status_input,
    target_type_for_action,
)

FORBIDDEN_PROJECTION_KEYS = frozenset(
    {
        "nodes",
        "connections",
        "credentials",
        "data",
        "pinData",
        "staticData",
        "settings",
        "meta",
        "password",
        "api_key",
        "token",
        "secret",
        "environment",
        "env",
    }
)


def _iso(now: Callable[[], datetime]) -> str:
    stamp = now()
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.isoformat().replace("+00:00", "Z")


def _bounded_text(value: object, *, maximum: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        return ""
    text = value.replace("\x00", "")
    return text[:maximum]


def _as_bool(value: object) -> bool:
    return value is True


def project_workflow(raw: object) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise N8nError("protocol_error", ERROR_MESSAGES["protocol_error"])
    workflow_id = parse_safe_path_segment(raw.get("id"))
    return {
        "id": workflow_id,
        "name": _bounded_text(raw.get("name"), maximum=MAX_NAME_CHARS),
        "active": _as_bool(raw.get("active")),
        "archived": _as_bool(raw.get("isArchived") or raw.get("archived")),
        "updated_at": _bounded_text(raw.get("updatedAt") or raw.get("updated_at"), maximum=MAX_STRING_CHARS),
    }


def project_execution(raw: object) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise N8nError("protocol_error", ERROR_MESSAGES["protocol_error"])
    execution_id = parse_safe_path_segment(raw.get("id"))
    workflow_id = parse_safe_path_segment(raw.get("workflowId") or raw.get("workflow_id"))
    return {
        "id": execution_id,
        "workflow_id": workflow_id,
        "status": _bounded_text(raw.get("status"), maximum=32),
        "started_at": _bounded_text(raw.get("startedAt") or raw.get("started_at"), maximum=MAX_STRING_CHARS),
        "stopped_at": _bounded_text(raw.get("stoppedAt") or raw.get("stopped_at"), maximum=MAX_STRING_CHARS) or None,
        "mode": _bounded_text(raw.get("mode"), maximum=32),
    }


def _items(raw: object) -> list[Any]:
    if isinstance(raw, Mapping):
        data = raw.get("data")
        if not isinstance(data, list):
            raise N8nError("protocol_error", ERROR_MESSAGES["protocol_error"])
        return data[: MAX_COLLECTION + 1]
    if isinstance(raw, list):
        return raw[: MAX_COLLECTION + 1]
    raise N8nError("protocol_error", ERROR_MESSAGES["protocol_error"])


def project_overview(workflows_raw: object, executions_raw: object) -> dict[str, Any]:
    workflows = _items(workflows_raw)
    executions = _items(executions_raw)
    truncated = len(workflows) > MAX_COLLECTION or len(executions) > MAX_COLLECTION
    return {
        "workflows": [project_workflow(item) for item in workflows[:MAX_COLLECTION]],
        "executions": [project_execution(item) for item in executions[:MAX_COLLECTION]],
        "truncated": truncated,
    }


def _sanitize(value: Any, secrets: Sequence[str]) -> Any:
    if isinstance(value, str):
        text = value
        for secret in secrets:
            if secret:
                text = text.replace(secret, "[redacted]")
        return text
    if isinstance(value, Mapping):
        return {key: _sanitize(item, secrets) for key, item in value.items() if key not in FORBIDDEN_PROJECTION_KEYS}
    if isinstance(value, list):
        return [_sanitize(item, secrets) for item in value]
    return value


def _envelope(*, request_id: str, observed_at: str, status: str, data: Any) -> dict[str, Any]:
    return {"request_id": request_id, "observed_at": observed_at, "status": status, "data": data}


class N8nOperatorTools:
    """Closed public n8n-operator tool surface."""

    def __init__(
        self,
        *,
        client: Any,
        store: N8nActionStore | None,
        now: Callable[[], datetime],
        request_id: Callable[[], str],
        secrets: Sequence[str],
    ):
        self.client = client
        self.store = store
        self.now = now
        self.request_id = request_id
        self.secrets = list(secrets)

    def call_tool(self, name: str, arguments: object) -> dict[str, Any]:
        handlers = {
            "n8n_overview": self.n8n_overview,
            "n8n_workflow_status": self.n8n_workflow_status,
            "n8n_execution_bundle": self.n8n_execution_bundle,
            "n8n_propose_action": self.n8n_propose_action,
            "n8n_action_status": self.n8n_action_status,
            "n8n_execute_action": self.n8n_execute_action,
        }
        if name not in handlers:
            raise N8nError("invalid_request", ERROR_MESSAGES["invalid_request"])
        return handlers[name](arguments)

    def n8n_overview(self, raw: object) -> dict[str, Any]:
        parse_overview_input(raw)
        data = project_overview(self.client.list_workflows(), self.client.list_executions())
        return _envelope(
            request_id=self.request_id(), observed_at=_iso(self.now), status="ok", data=_sanitize(data, self.secrets)
        )

    def n8n_workflow_status(self, raw: object) -> dict[str, Any]:
        parsed = parse_workflow_status_input(raw)
        data = project_workflow(self.client.get_workflow(parsed["workflow_id"]))
        return _envelope(
            request_id=self.request_id(), observed_at=_iso(self.now), status="ok", data=_sanitize(data, self.secrets)
        )

    def n8n_execution_bundle(self, raw: object) -> dict[str, Any]:
        parsed = parse_execution_bundle_input(raw)
        data = project_execution(self.client.get_execution(parsed["execution_id"]))
        return _envelope(
            request_id=self.request_id(), observed_at=_iso(self.now), status="ok", data=_sanitize(data, self.secrets)
        )

    def n8n_propose_action(self, raw: object) -> dict[str, Any]:
        if self.store is None:
            raise N8nError("denied", ERROR_MESSAGES["denied"])
        parsed = parse_propose_input(raw)
        projection = self._current_projection(parsed["action_id"], parsed["target_id"])
        proposal = self.store.create_proposal(parsed, revision=target_revision(projection))
        data = {
            "proposal_id": proposal["proposal_id"],
            "action_id": proposal["action_id"],
            "target_id": proposal["target_id"],
            "target_type": proposal["target_type"],
            "revision": proposal["revision"],
            "expires_at": proposal["expires_at"],
            "automatic_rollback": False,
        }
        return _envelope(request_id=self.request_id(), observed_at=_iso(self.now), status="ok", data=data)

    def n8n_action_status(self, raw: object) -> dict[str, Any]:
        if self.store is None:
            raise N8nError("denied", ERROR_MESSAGES["denied"])
        parsed = parse_action_status_input(raw)
        data = self.store.public_status(parsed["proposal_id"])
        return _envelope(
            request_id=self.request_id(), observed_at=_iso(self.now), status="ok", data=_sanitize(data, self.secrets)
        )

    def n8n_execute_action(self, raw: object) -> dict[str, Any]:
        if self.store is None:
            raise N8nError("denied", ERROR_MESSAGES["denied"])
        parsed = parse_execute_input(raw)
        proposal = self.store.read_proposal(parsed["proposal_id"])
        if proposal is None:
            if self.store.read_consumed(parsed["proposal_id"]) is not None:
                raise N8nError("denied", ERROR_MESSAGES["denied"])
            raise N8nError("denied", ERROR_MESSAGES["denied"])
        current = self._current_projection(proposal["action_id"], proposal["target_id"])
        consumed = self.store.consume_approved_proposal(
            parsed["proposal_id"],
            current_revision=target_revision(current),
        )
        try:
            self.client.mutate(consumed["action_id"], consumed["target_id"])
        except N8nError:
            self.store.mark_consumed_result(consumed["proposal_id"], "failed")
            raise
        self.store.mark_consumed_result(consumed["proposal_id"], "succeeded")
        data = {
            "proposal_id": consumed["proposal_id"],
            "action_id": consumed["action_id"],
            "target_type": consumed["target_type"],
            "state": "consumed",
        }
        return _envelope(request_id=self.request_id(), observed_at=_iso(self.now), status="ok", data=data)

    def _current_projection(self, action_id: str, target_id: str) -> dict[str, Any]:
        target_type = ACTION_TARGET_TYPES.get(action_id) or target_type_for_action(action_id)
        if target_type == "execution":
            return project_execution(self.client.get_execution(target_id))
        return project_workflow(self.client.get_workflow(target_id))
