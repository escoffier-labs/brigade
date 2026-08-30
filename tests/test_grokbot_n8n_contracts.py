"""Closed n8n-operator public contracts, inventory, and catalog gating."""

from __future__ import annotations

import pytest

from brigade.grokbot_n8n.contracts import (
    ACTION_IDS,
    DEFAULT_BIND,
    ERROR_MESSAGES,
    EXCLUDED_ACTION_IDS,
    PACK_ID,
    PROPOSAL_TTL_MS,
    TOOL_INPUT_KEYS,
    TOOLS,
    N8nError,
    parse_action_id,
    parse_action_status_input,
    parse_execute_input,
    parse_identifier,
    parse_overview_input,
    parse_propose_input,
    parse_safe_path_segment,
    parse_workflow_status_input,
    parse_execution_bundle_input,
    target_type_for_action,
)


def test_pack_identity_and_exact_six_tool_inventory():
    assert PACK_ID == "n8n-operator"
    assert DEFAULT_BIND == "127.0.0.1:8775"
    assert TOOLS == {
        "n8n_overview",
        "n8n_workflow_status",
        "n8n_execution_bundle",
        "n8n_propose_action",
        "n8n_action_status",
        "n8n_execute_action",
    }
    assert set(TOOL_INPUT_KEYS) == TOOLS
    assert TOOL_INPUT_KEYS["n8n_overview"] == frozenset()
    assert TOOL_INPUT_KEYS["n8n_workflow_status"] == frozenset({"workflow_id"})
    assert TOOL_INPUT_KEYS["n8n_execution_bundle"] == frozenset({"execution_id"})
    assert TOOL_INPUT_KEYS["n8n_propose_action"] == frozenset({"action_id", "target_id"})
    assert TOOL_INPUT_KEYS["n8n_action_status"] == frozenset({"proposal_id"})
    assert TOOL_INPUT_KEYS["n8n_execute_action"] == frozenset({"proposal_id"})


def test_action_catalog_is_closed_and_rejects_excluded_mutations():
    assert ACTION_IDS == {
        "deactivate-workflow",
        "archive-workflow",
        "unarchive-workflow",
        "cancel-execution",
    }
    assert target_type_for_action("deactivate-workflow") == "workflow"
    assert target_type_for_action("archive-workflow") == "workflow"
    assert target_type_for_action("unarchive-workflow") == "workflow"
    assert target_type_for_action("cancel-execution") == "execution"
    assert PROPOSAL_TTL_MS == 15 * 60 * 1000
    for action_id in ACTION_IDS:
        assert parse_action_id(action_id) == action_id
    for action_id in EXCLUDED_ACTION_IDS | {
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
    }:
        with pytest.raises(N8nError) as caught:
            parse_action_id(action_id)
        assert caught.value.code == "invalid_request"
        assert caught.value.public_error() == {
            "error": {"code": "invalid_request", "message": ERROR_MESSAGES["invalid_request"]}
        }


def test_identifiers_are_strict_safe_path_segments():
    for value in ("wf1", "123", "exec-ab_cd.9", "a" * 64):
        assert parse_safe_path_segment(value) == value
        assert parse_identifier(value) == value
    for value in (
        "",
        "a" * 65,
        "../wf",
        "wf/id",
        "wf?x=1",
        "wf#frag",
        "wf%2e",
        "wf id",
        "-leading",
        ".hidden",
        "wf:colon",
        "wf;stop",
    ):
        with pytest.raises(N8nError) as caught:
            parse_safe_path_segment(value)
        assert caught.value.code == "invalid_request"
        assert ".." not in str(caught.value)
        assert "/" not in str(caught.value)


def test_input_parsers_reject_extra_and_missing_keys():
    assert parse_overview_input({}) == {}
    with pytest.raises(N8nError) as caught:
        parse_overview_input({"extra": "no"})
    assert caught.value.code == "invalid_request"
    assert parse_workflow_status_input({"workflow_id": "wf1"}) == {"workflow_id": "wf1"}
    with pytest.raises(N8nError):
        parse_workflow_status_input({"workflow_id": "wf1", "nodes": True})
    with pytest.raises(N8nError):
        parse_execution_bundle_input({"execution_id": "ex1", "includeData": True})
    assert parse_propose_input({"action_id": "archive-workflow", "target_id": "wf1"}) == {
        "action_id": "archive-workflow",
        "target_id": "wf1",
    }
    with pytest.raises(N8nError):
        parse_propose_input({"action_id": "delete-workflow", "target_id": "wf1"})
    assert parse_action_status_input({"proposal_id": "a" * 32}) == {"proposal_id": "a" * 32}
    assert parse_execute_input({"proposal_id": "b" * 32}) == {"proposal_id": "b" * 32}
    with pytest.raises(N8nError):
        parse_execute_input({"proposal_id": "b" * 32, "force": True})
