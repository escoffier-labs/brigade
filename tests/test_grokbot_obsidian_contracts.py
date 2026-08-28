"""Closed Phase 1 public contracts for the Obsidian Operator pack."""

from __future__ import annotations

import pytest

from brigade.grokbot_obsidian.contracts import (
    ERROR_MESSAGES,
    PACK_ID,
    PHASE2_ONLY_ACTION_IDS,
    PUBLIC_ROUTE,
    TOOLS,
    ObsidianError,
    parse_action_status_input,
    parse_capabilities_input,
    parse_capabilities_result,
    parse_execute_input,
    parse_phase1_action,
    parse_propose_input,
    parse_read_input,
    parse_search_input,
)

ACTION_ID = "a" * 32
RECEIPT = "b" * 32
IF_MATCH = "c" * 6
CREATE_NOTE = {
    "kind": "create_note",
    "path": "00 - Inbox/Agent Notes/demo.md",
    "type": "agent-note",
    "para": "inbox",
    "agent": "cerebro-curator",
    "tags": ["agent-note"],
    "body": "Hello.\n",
}


def test_pack_identity_and_closed_tool_inventory():
    assert PACK_ID == "obsidian-operator"
    assert PUBLIC_ROUTE == "/mcp"
    assert TOOLS == {
        "obsidian_capabilities",
        "obsidian_search",
        "obsidian_read",
        "obsidian_action_status",
        "obsidian_propose_action",
        "obsidian_execute_action",
    }


def test_accepts_minimal_public_inputs_and_rejects_extras():
    assert parse_capabilities_input({}) == {}
    assert parse_search_input({"query": "alpha"}) == {"query": "alpha", "limit": 5}
    assert parse_search_input({"query": "alpha", "limit": 10}) == {"query": "alpha", "limit": 10}
    assert parse_read_input({"path": "01 - Projects/demo.md"}) == {"path": "01 - Projects/demo.md"}
    assert parse_action_status_input({"action_id": ACTION_ID}) == {"action_id": ACTION_ID}
    assert parse_execute_input({"action_id": ACTION_ID, "approval_receipt": RECEIPT})["action_id"] == ACTION_ID
    for extra in ({"command": "id"}, {"vault_write": True}, {"api_key": "x"}, {"argv": []}):
        with pytest.raises(ObsidianError) as caught:
            parse_capabilities_input(extra)
        assert caught.value.code == "invalid_request"
        with pytest.raises(ObsidianError):
            parse_search_input({"query": "alpha", **extra})


def test_create_note_is_accepted_and_forbidden_kinds_are_rejected():
    parsed = parse_propose_input({"request_id": "req-1234", "action": CREATE_NOTE})
    assert parsed["action"]["kind"] == "create_note"
    for kind in (
        "delete_permanent",
        "command_execute",
        "vault_write",
        "open_file",
        "run_plugin_command",
        *PHASE2_ONLY_ACTION_IDS,
    ):
        with pytest.raises(ObsidianError) as caught:
            parse_phase1_action({"kind": kind, "path": "01 - Projects/demo.md"})
        assert caught.value.code == "invalid_request"
        assert "01 - Projects" not in str(caught.value)


def test_revision_token_and_opaque_ids_are_strict():
    with pytest.raises(ObsidianError):
        parse_phase1_action(
            {
                "kind": "patch_note",
                "path": "01 - Projects/demo.md",
                "ifMatch": "ABCDEF",
                "patch": {"target": "heading", "heading": ["Intro"], "body": "x"},
            }
        )
    with pytest.raises(ObsidianError):
        parse_execute_input({"action_id": "not-hex", "approval_receipt": RECEIPT})


def test_public_errors_never_include_paths_or_child_output():
    with pytest.raises(ObsidianError) as caught:
        parse_read_input({"path": "/etc/passwd", "command": "id"})
    assert caught.value.public_error() == {
        "error": {"code": "invalid_request", "message": ERROR_MESSAGES["invalid_request"]}
    }
    assert "/etc/passwd" not in str(caught.value)


def test_capabilities_projection_is_generic_and_phase1_only():
    parsed = parse_capabilities_result(
        {
            "phase": "phase1",
            "search_backend": "native_bounded_search",
            "plugins": [{"id": "core", "version": "1.0.0", "supported_action_ids": ["create_note"]}],
            "supported_action_ids": ["create_note"],
        }
    )
    assert parsed["phase"] == "phase1"
    with pytest.raises(ObsidianError):
        parse_capabilities_result(
            {
                "phase": "phase1",
                "search_backend": "native_bounded_search",
                "plugins": [{"id": "core", "version": "1.0.0", "supported_action_ids": ["patch_canvas"]}],
                "supported_action_ids": ["patch_canvas"],
            }
        )
