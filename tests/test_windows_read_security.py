"""Fail-closed coverage for private Grok Bot reads without a Windows owner API."""

from __future__ import annotations

import builtins
import os
from pathlib import Path

import pytest

from brigade import grokbot_feed, grokbot_findings, grokbot_scout_feed
from brigade.grokbot_backup import actions as backup_actions
from brigade.grokbot_backup import ledger as backup_ledger
from brigade.grokbot_backup import runtime_config as backup_runtime_config
from brigade.grokbot_backup.contracts import BackupError
from brigade.grokbot_fleet import ledger as fleet_ledger
from brigade.grokbot_fleet import runtime_config as fleet_runtime_config
from brigade.grokbot_fleet.contracts import FleetError
from brigade.grokbot_n8n import runtime_config as n8n_runtime_config
from brigade.grokbot_n8n.contracts import N8nError
from brigade.grokbot_obsidian import runtime_config as obsidian_runtime_config
from brigade.grokbot_obsidian import store as obsidian_store
from brigade.grokbot_obsidian.contracts import ObsidianError
from brigade.grokbot_wazuh import store as wazuh_store
from brigade.grokbot_wazuh.contracts import WazuhError


REASON = "secure-owner-read-unavailable"
UNSAFE_PATHS = (
    "/missing/private-runtime.json",
    r"\\server\share\runtime.json",
    r"\\.\pipe\runtime.json",
    r"C:/private\runtime.json",
)


def _filesystem_accessed(*args, **kwargs):
    raise AssertionError("private read reached the filesystem before owner security was established")


def _read_cases(path_text: str):
    return (
        (lambda: grokbot_findings._read_manifest_snapshot(Path(path_text)), grokbot_findings.FindingsError, None),
        (
            lambda: grokbot_findings._open_queue_child_readonly(Path(path_text), "findings"),
            grokbot_findings.FindingsError,
            None,
        ),
        (
            lambda: backup_actions.BackupActionStore(
                action_state_path=path_text, approval_dir=path_text
            ).active_operations(),
            BackupError,
            "unavailable",
        ),
        (
            lambda: backup_actions.BackupActionStore(
                action_state_path=path_text, approval_dir=path_text
            )._list_consumed(),
            BackupError,
            "unavailable",
        ),
        (lambda: backup_ledger.BackupLedger(path_text)._ensure_state_dir(), BackupError, "unavailable"),
        (lambda: backup_ledger.BackupLedger(path_text)._load_records(), BackupError, "unavailable"),
        (lambda: fleet_ledger.FleetLedger(path_text)._ensure_state_dir(), FleetError, "unavailable"),
        (lambda: fleet_ledger.FleetLedger(path_text)._load_document(), FleetError, "unavailable"),
        (lambda: obsidian_store._ensure_directory(Path(path_text)), ObsidianError, "unavailable"),
        (lambda: obsidian_store._read_json(Path(path_text).parent, "record.json"), ObsidianError, "unavailable"),
        (lambda: obsidian_store._hex_json_files(Path(path_text)), ObsidianError, "unavailable"),
        (lambda: wazuh_store.WazuhStore(path_text).ready(), WazuhError, "unavailable"),
        (lambda: wazuh_store.read_secure_text(path_text, expected_mode=0o644), WazuhError, "unavailable"),
        (lambda: backup_runtime_config.read_secure_runtime_text(path_text), BackupError, "unavailable"),
        (lambda: fleet_runtime_config.read_secure_runtime_text(path_text), FleetError, "unavailable"),
        (lambda: n8n_runtime_config.read_secure_runtime_text(path_text), N8nError, "unavailable"),
        (lambda: n8n_runtime_config.read_secure_api_key(path_text), N8nError, "unavailable"),
        (lambda: obsidian_runtime_config.read_secure_runtime_text(path_text), ObsidianError, "unavailable"),
        (lambda: grokbot_feed._read_manifest_snapshot(Path(path_text)), grokbot_feed.FeedError, None),
        (lambda: grokbot_scout_feed._read_policy_snapshot(Path(path_text)), grokbot_scout_feed.ScoutFeedError, None),
    )


def _assert_no_filesystem_access(monkeypatch: pytest.MonkeyPatch, path_text: str) -> None:
    with monkeypatch.context() as filesystem:
        filesystem.setattr(Path, "lstat", _filesystem_accessed)
        filesystem.setattr(Path, "read_text", _filesystem_accessed)
        filesystem.setattr(Path, "mkdir", _filesystem_accessed)
        filesystem.setattr(Path, "exists", _filesystem_accessed)
        filesystem.setattr(Path, "is_symlink", _filesystem_accessed)
        filesystem.setattr(Path, "is_dir", _filesystem_accessed)
        filesystem.setattr(Path, "open", _filesystem_accessed)
        filesystem.setattr(builtins, "open", _filesystem_accessed)
        filesystem.setattr(os, "fstat", _filesystem_accessed)
        filesystem.setattr(os, "lstat", _filesystem_accessed)
        filesystem.setattr(os, "listdir", _filesystem_accessed)
        filesystem.setattr(os, "open", _filesystem_accessed)
        filesystem.setattr(os, "scandir", _filesystem_accessed)
        filesystem.setattr(os, "stat", _filesystem_accessed)
        for read, error_type, code in _read_cases(path_text):
            with pytest.raises(error_type) as error:
                read()
            assert str(error.value) == REASON
            if code is not None:
                assert error.value.code == code


@pytest.mark.parametrize("path_text", UNSAFE_PATHS)
def test_private_readers_fail_closed_before_filesystem_access(monkeypatch: pytest.MonkeyPatch, path_text: str) -> None:
    """Linux simulates the unavailable Windows ownership capability per module."""
    modules = (
        grokbot_findings,
        backup_actions,
        backup_ledger,
        fleet_ledger,
        obsidian_store,
        wazuh_store,
        backup_runtime_config,
        fleet_runtime_config,
        n8n_runtime_config,
        obsidian_runtime_config,
        grokbot_feed,
        grokbot_scout_feed,
    )
    for module in modules:
        monkeypatch.setattr(module, "SECURE_OWNER_READ_AVAILABLE", False)
    _assert_no_filesystem_access(monkeypatch, path_text)


def test_filesystem_spies_restore_before_failure_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(AssertionError, match="filesystem before owner security"):
        with monkeypatch.context() as filesystem:
            filesystem.setattr(Path, "exists", _filesystem_accessed)
            Path("missing").exists()
    assert Path("missing").name == "missing"


def test_native_windows_capabilities_are_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Native Windows uses the same typed reason, while Linux uses injection above."""
    if os.name != "nt":
        pytest.skip("native Windows assertion")
    for module in (
        grokbot_findings,
        backup_actions,
        backup_ledger,
        fleet_ledger,
        obsidian_store,
        wazuh_store,
        backup_runtime_config,
        fleet_runtime_config,
        n8n_runtime_config,
        obsidian_runtime_config,
        grokbot_feed,
        grokbot_scout_feed,
    ):
        assert module.SECURE_OWNER_READ_AVAILABLE is False
    _assert_no_filesystem_access(monkeypatch, UNSAFE_PATHS[0])
