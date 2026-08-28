"""Backup Steward runtime config and path-safety tests."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from brigade.grokbot_backup.contracts import BackupError
from brigade.grokbot_backup.runtime_config import (
    PUBLIC_ALIASES,
    load_backup_runtime_env,
    parse_backup_private_runtime,
    project_backup_registry,
    read_secure_runtime_text,
)


def _command(path: Path) -> dict[str, object]:
    return {"executable": str(path), "args": ["--summary"]}


def _target(**overrides: object) -> dict[str, object]:
    payload = {
        "reporter_id": "restic-target-summary-v1",
        "readiness_reporter_id": "restic-restore-readiness-v1",
        "action_runner_ids": ["restic-backup-runner-v1"],
        "stale_lock_seconds": 3600,
        "timeout_ms": 12000,
        "freshness_seconds": 3600,
    }
    payload.update(overrides)
    return payload


def _runtime(tmp_path: Path, *, safety_ready: bool = True) -> dict[str, object]:
    binaries = {
        name: tmp_path / name
        for name in (
            "restic-target",
            "restic-readiness",
            "backup-status",
            "virt-summary",
            "repo-sweep",
            "restic-backup",
            "restic-integrity",
            "restic-rehearsal",
        )
    }
    for path in binaries.values():
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)
    return {
        "version": 1,
        "safety_ready": safety_ready,
        "targets": {
            "configuration-nas": _target(),
            "configuration-cloud": _target(),
            "media-archive": _target(),
            "virtualization": _target(
                reporter_id="virtualization-backup-summary-v1",
                action_runner_ids=[],
            ),
            "repository-sweep": _target(
                reporter_id="repository-sweep-summary-v1",
                action_runner_ids=[],
            ),
        },
        "reporters": {
            "restic-target-summary-v1": _command(binaries["restic-target"]),
            "restic-restore-readiness-v1": _command(binaries["restic-readiness"]),
            "backup-operation-status-v1": _command(binaries["backup-status"]),
            "virtualization-backup-summary-v1": _command(binaries["virt-summary"]),
            "repository-sweep-summary-v1": _command(binaries["repo-sweep"]),
        },
        "action_runners": {
            "restic-backup-runner-v1": _command(binaries["restic-backup"]),
            "restic-integrity-runner-v1": _command(binaries["restic-integrity"]),
            "restic-rehearsal-runner-v1": _command(binaries["restic-rehearsal"]),
        },
    }


def test_runtime_projects_generic_aliases_and_hides_runners_when_not_safety_ready(tmp_path: Path):
    runtime = parse_backup_private_runtime(_runtime(tmp_path, safety_ready=False))
    registry = project_backup_registry(runtime)
    assert [target["alias"] for target in registry.targets] == list(PUBLIC_ALIASES)
    assert registry.safety_ready is False
    assert registry.target("media-archive")["action_runner_ids"] == ()
    with pytest.raises(BackupError) as caught:
        registry.target("/etc/passwd")
    assert caught.value.code == "not_found"
    assert "/etc" not in str(caught.value)


def test_runtime_rejects_relative_executables_duplicate_bins_and_private_hosts(tmp_path: Path):
    payload = _runtime(tmp_path)
    payload["reporters"]["restic-target-summary-v1"]["executable"] = "restic"
    with pytest.raises(BackupError) as caught:
        parse_backup_private_runtime(payload)
    assert caught.value.code == "invalid_request"
    assert "restic" not in str(caught.value)
    payload = _runtime(tmp_path)
    payload["reporters"]["restic-restore-readiness-v1"]["executable"] = payload["reporters"][
        "restic-target-summary-v1"
    ]["executable"]
    with pytest.raises(BackupError):
        parse_backup_private_runtime(payload)
    payload = _runtime(tmp_path)
    payload["targets"]["media-archive"]["action_runner_ids"] = ["restic-backup-runner-v1", "restic-backup-runner-v1"]
    with pytest.raises(BackupError):
        parse_backup_private_runtime(payload)


def test_runtime_env_requires_loopback_disjoint_paths_and_distinct_token(tmp_path: Path):
    runtime = tmp_path / "runtime.json"
    ledger = tmp_path / "ledger" / "ledger.json"
    actions = tmp_path / "actions"
    approvals = tmp_path / "approvals"
    env = {
        "GROKBOT_BACKUP_TOKEN": "a" * 32,
        "GROKBOT_BACKUP_HOST": "127.0.0.1",
        "GROKBOT_BACKUP_PORT": "8772",
        "GROKBOT_BACKUP_RUNTIME_PATH": str(runtime),
        "GROKBOT_BACKUP_LEDGER_PATH": str(ledger),
        "GROKBOT_BACKUP_ACTION_STATE_PATH": str(actions),
        "GROKBOT_BACKUP_APPROVAL_DIR": str(approvals),
    }
    loaded = load_backup_runtime_env(env)
    assert loaded["host"] == "127.0.0.1"
    assert loaded["port"] == 8772
    with pytest.raises(BackupError):
        load_backup_runtime_env({**env, "GROKBOT_BACKUP_HOST": "0.0.0.0"})
    with pytest.raises(BackupError):
        load_backup_runtime_env({**env, "GROKBOT_BACKUP_LEDGER_PATH": str(actions / "nested")})
    with pytest.raises(BackupError):
        load_backup_runtime_env(env, dispatch_token="a" * 32)
    with pytest.raises(BackupError):
        load_backup_runtime_env({**env, "GROKBOT_BACKUP_RUNTIME_PATH": "../runtime.json"})


def test_secure_runtime_read_rejects_world_readable_and_symlink(tmp_path: Path):
    runtime = tmp_path / "runtime.json"
    runtime.write_text("{}", encoding="utf-8")
    os.chmod(runtime, 0o644)
    with pytest.raises(BackupError) as caught:
        read_secure_runtime_text(str(runtime))
    assert caught.value.code == "invalid_request"
    assert str(runtime) not in str(caught.value)
    os.chmod(runtime, 0o600)
    link = tmp_path / "runtime.link"
    link.symlink_to(runtime)
    with pytest.raises(BackupError):
        read_secure_runtime_text(str(link))
    assert json.loads(read_secure_runtime_text(str(runtime))) == {}


def test_runtime_accepts_process_cap_and_rejects_invalid_values(tmp_path: Path):
    payload = _runtime(tmp_path)
    payload["max_concurrent_processes"] = 1
    parsed = parse_backup_private_runtime(payload)
    assert parsed["max_concurrent_processes"] == 1
    for invalid in (0, 9, "1", 1.5, True):
        broken = dict(payload)
        broken["max_concurrent_processes"] = invalid
        with pytest.raises(BackupError) as caught:
            parse_backup_private_runtime(broken)
        assert caught.value.code == "invalid_request"
    env = {
        "GROKBOT_BACKUP_TOKEN": "a" * 32,
        "GROKBOT_BACKUP_HOST": "127.0.0.1",
        "GROKBOT_BACKUP_PORT": "8772",
        "GROKBOT_BACKUP_RUNTIME_PATH": str(tmp_path / "runtime.json"),
        "GROKBOT_BACKUP_LEDGER_PATH": str(tmp_path / "ledger.json"),
        "GROKBOT_BACKUP_ACTION_STATE_PATH": str(tmp_path / "actions"),
        "GROKBOT_BACKUP_APPROVAL_DIR": str(tmp_path / "approvals"),
        "GROKBOT_BACKUP_MAX_CONCURRENT_PROCESSES": "0",
    }
    with pytest.raises(BackupError) as env_caught:
        load_backup_runtime_env(env)
    assert env_caught.value.code == "invalid_request"
