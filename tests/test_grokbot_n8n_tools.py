"""Bounded n8n-operator read projections and redaction."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from brigade.grokbot_n8n.contracts import MAX_COLLECTION, N8nError
from brigade.grokbot_n8n.tools import N8nOperatorTools, project_execution, project_overview, project_workflow


class _FakeClient:
    def __init__(self, workflows=None, executions=None, workflow=None, execution=None):
        self.workflows = workflows if workflows is not None else {"data": []}
        self.executions = executions if executions is not None else {"data": []}
        self.workflow = workflow
        self.execution = execution
        self.mutations: list[tuple[str, str]] = []

    def list_workflows(self):
        return self.workflows

    def get_workflow(self, workflow_id: str):
        if self.workflow is None:
            raise N8nError("not_found")
        return self.workflow

    def list_executions(self):
        return self.executions

    def get_execution(self, execution_id: str):
        if self.execution is None:
            raise N8nError("not_found")
        return self.execution

    def mutate(self, action_id: str, target_id: str):
        self.mutations.append((action_id, target_id))
        return {"ok": True}


def _tools(client: _FakeClient, store=None) -> N8nOperatorTools:
    return N8nOperatorTools(
        client=client,
        store=store,
        now=lambda: datetime(2026, 8, 30, tzinfo=timezone.utc),
        request_id=lambda: "request-1",
        secrets=["n8n-placeholder-key-not-real", "/var/lib/n8n/key"],
    )


def test_projections_are_bounded_and_redact_definitions_credentials_and_data():
    raw_workflow = {
        "id": "wf1",
        "name": "A" * 200,
        "active": True,
        "isArchived": False,
        "updatedAt": "2026-08-30T00:00:00.000Z",
        "nodes": [{"type": "n8n-nodes-base.httpRequest", "credentials": {"http": "id"}}],
        "connections": {"a": {}},
        "settings": {"timezone": "America/New_York"},
        "staticData": {"env": "secret"},
        "pinData": {"x": 1},
        "meta": {"instanceId": "abc"},
    }
    projected = project_workflow(raw_workflow)
    assert set(projected) == {"id", "name", "active", "archived", "updated_at"}
    assert projected["id"] == "wf1"
    assert projected["active"] is True
    assert projected["archived"] is False
    assert len(projected["name"]) <= 80
    dumped = str(projected)
    assert "nodes" not in dumped
    assert "credentials" not in dumped
    assert "httpRequest" not in dumped
    assert "secret" not in dumped

    raw_execution = {
        "id": "ex1",
        "workflowId": "wf1",
        "status": "success",
        "startedAt": "2026-08-30T00:00:00.000Z",
        "stoppedAt": "2026-08-30T00:00:01.000Z",
        "mode": "manual",
        "data": {"resultData": {"runData": {"leak": True}}},
        "retryOf": "ex0",
    }
    execution = project_execution(raw_execution)
    assert set(execution) == {"id", "workflow_id", "status", "started_at", "stopped_at", "mode"}
    assert "resultData" not in str(execution)
    assert "leak" not in str(execution)


class _CapProbe(list):
    def __getitem__(self, index):  # type: ignore[override]
        if isinstance(index, int) and index >= MAX_COLLECTION + 1:
            raise AssertionError("indexed beyond cap+1")
        if isinstance(index, slice):
            stop = index.stop
            if stop is None or stop > MAX_COLLECTION + 1:
                raise AssertionError("sliced beyond cap+1")
        return super().__getitem__(index)

    def __iter__(self):  # type: ignore[override]
        for index, item in enumerate(super().__iter__()):
            if index >= MAX_COLLECTION + 1:
                raise AssertionError("iterated beyond cap+1")
            yield item


def test_overview_materializes_at_most_cap_plus_one_and_marks_truncated():
    workflows = {
        "data": _CapProbe(
            {
                "id": f"wf{index}",
                "name": f"Workflow {index}",
                "active": True,
                "isArchived": False,
                "updatedAt": "2026-08-30T00:00:00.000Z",
            }
            for index in range(MAX_COLLECTION + 8)
        )
    }
    executions = {"data": _CapProbe()}
    overview = project_overview(workflows, executions)
    assert len(overview["workflows"]) == MAX_COLLECTION
    assert overview["truncated"] is True


def test_overview_caps_collections_and_omits_raw_upstream_bodies():
    workflows = {
        "data": [
            {
                "id": f"wf{index}",
                "name": f"Workflow {index}",
                "active": index % 2 == 0,
                "isArchived": False,
                "updatedAt": "2026-08-30T00:00:00.000Z",
                "nodes": [{"parameters": {"url": "https://secret.invalid"}}],
            }
            for index in range(MAX_COLLECTION + 5)
        ]
    }
    executions = {
        "data": [
            {
                "id": f"ex{index}",
                "workflowId": "wf1",
                "status": "success",
                "startedAt": "2026-08-30T00:00:00.000Z",
                "stoppedAt": None,
                "mode": "trigger",
                "data": {"huge": "x" * 1000},
            }
            for index in range(MAX_COLLECTION + 3)
        ]
    }
    overview = project_overview(workflows, executions)
    assert len(overview["workflows"]) == MAX_COLLECTION
    assert len(overview["executions"]) == MAX_COLLECTION
    assert overview["truncated"] is True
    assert "secret.invalid" not in str(overview)
    assert "huge" not in str(overview)


def test_read_tools_reject_extra_keys_and_do_not_return_secrets():
    tools = _tools(
        _FakeClient(
            workflows={"data": [{"id": "wf1", "name": "demo", "active": True, "isArchived": False}]},
            workflow={"id": "wf1", "name": "demo", "active": True, "isArchived": False},
            execution={"id": "ex1", "workflowId": "wf1", "status": "error", "mode": "manual"},
        )
    )
    with pytest.raises(N8nError):
        tools.call_tool("n8n_overview", {"limit": 99})
    result = tools.call_tool("n8n_overview", {})
    assert result["status"] == "ok"
    assert "n8n-placeholder-key-not-real" not in str(result)
    assert "/var/lib/n8n/key" not in str(result)
    status = tools.call_tool("n8n_workflow_status", {"workflow_id": "wf1"})
    assert status["data"]["id"] == "wf1"
    with pytest.raises(N8nError):
        tools.call_tool("n8n_workflow_status", {"workflow_id": "wf1", "includeCredentials": True})
    bundle = tools.call_tool("n8n_execution_bundle", {"execution_id": "ex1"})
    assert bundle["data"]["id"] == "ex1"
    assert "error" not in bundle["data"] or bundle["data"].get("status") == "error"
