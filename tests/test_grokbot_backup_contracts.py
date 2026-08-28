"""Closed Backup Steward public contracts and catalog gating."""

from __future__ import annotations

import pytest

from brigade.grokbot_backup.actions import catalog_actions_for, catalog_entry
from brigade.grokbot_backup.contracts import (
    ACTION_IDS,
    ERROR_MESSAGES,
    TOOLS,
    BackupError,
    parse_action_id,
    parse_execute_input,
    parse_identifier,
    parse_operation_input,
    parse_overview_input,
    parse_propose_input,
    parse_target_input,
)


def test_public_tool_inventory_is_exact():
    assert TOOLS == {
        "backup_overview",
        "backup_target_status",
        "backup_restore_readiness",
        "backup_operation_status",
        "backup_propose_action",
        "backup_execute_action",
    }


def test_identifiers_accept_registered_aliases_and_reject_paths():
    for value in (
        "configuration-nas",
        "configuration-cloud",
        "media-archive",
        "virtualization",
        "repository-sweep",
        "run-backup",
        "op-abc123def456",
        "configuration-nas:stale-lock",
    ):
        assert parse_identifier(value) == value
    for value in (
        "",
        "a" * 65,
        "-leading",
        "../configuration-nas",
        "configuration/nas",
        "configuration nas",
        "host;unlock",
    ):
        with pytest.raises(BackupError) as caught:
            parse_identifier(value)
        assert caught.value.code == "invalid_request"
        assert ".." not in str(caught.value)


def test_action_catalog_is_fixed_and_observation_only_targets_have_no_actions():
    assert ACTION_IDS == {"run-backup", "run-integrity-check", "run-restore-rehearsal"}
    for alias in ("configuration-nas", "configuration-cloud", "media-archive"):
        assert catalog_actions_for(alias) == ("run-backup", "run-integrity-check", "run-restore-rehearsal")
        assert catalog_entry(alias, "run-backup")["automatic_rollback"] is False
    for alias in ("virtualization", "repository-sweep"):
        assert catalog_actions_for(alias) == ()
        assert catalog_entry(alias, "run-backup") is None
    for action_id in ("unlock", "delete", "prune", "forget", "restore-production", "rotate-credentials"):
        with pytest.raises(BackupError) as caught:
            parse_action_id(action_id)
        assert caught.value.code == "invalid_request"
        assert catalog_entry("configuration-nas", action_id) is None


def test_input_parsers_are_strict_and_errors_are_generic():
    assert parse_overview_input({}) == {}
    with pytest.raises(BackupError) as caught:
        parse_overview_input({"extra": "no"})
    assert caught.value.public_error() == {
        "error": {"code": "invalid_request", "message": ERROR_MESSAGES["invalid_request"]}
    }
    assert parse_target_input({"target_alias": "media-archive"}) == {"target_alias": "media-archive"}
    with pytest.raises(BackupError):
        parse_target_input({"alias": "media-archive"})
    assert parse_operation_input({"operation_id": "op-1"}) == {"operation_id": "op-1"}
    assert (
        parse_propose_input(
            {"target_alias": "media-archive", "action_id": "run-backup", "finding_id": "media-archive:stale-lock"}
        )["action_id"]
        == "run-backup"
    )
    with pytest.raises(BackupError):
        parse_propose_input({"target_alias": "media-archive", "action_id": "unlock", "finding_id": "x"})
    assert parse_execute_input({"proposal_id": "abc123"}) == {"proposal_id": "abc123"}
    with pytest.raises(BackupError):
        parse_execute_input({"proposal_id": "abc123", "command": "restic unlock"})
