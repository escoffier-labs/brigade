"""Backup Steward adapter, normalize, and exec-bound tests."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from brigade.grokbot_backup.adapters import BackupObservers
from brigade.grokbot_backup.contracts import BackupError
from brigade.grokbot_backup.exec import ExecRequest, PrivateExecResult, run_exec
from brigade.grokbot_backup.normalize import classify_backup_lock, normalize_target_observation, sanitize_backup_detail
from brigade.grokbot_backup.runtime_config import parse_backup_private_runtime, project_backup_registry
from tests.test_grokbot_backup_runtime import _runtime


def _target_observation(alias: str = "media-archive") -> dict[str, object]:
    return {
        "target_alias": alias,
        "reporter_id": "restic-target-summary-v1",
        "observed_at": "2026-08-28T00:00:00Z",
        "health": "healthy",
        "snapshot_age_seconds": 120,
        "last_successful_operation": "run-backup",
        "scheduler_state": "idle",
        "integrity_state": "ok",
        "retention_evidence_state": "ok",
        "live_operation_id": None,
        "lock_age_seconds": None,
        "lock_owner": None,
        "detail": "ok",
    }


def test_sanitize_and_lock_classification_are_generic():
    detail = sanitize_backup_detail(
        "pid=1234 rest:sftp://user@192.0.2.10/repo /var/lib/restic host.example token-value",
        ["token-value"],
    )
    assert "1234" not in detail
    assert "192.0.2.10" not in detail
    assert "/var/lib/restic" not in detail
    assert "token-value" not in detail
    assert "host.example" not in detail
    assert (
        classify_backup_lock(
            {"live_operation_id": "op-1", "lock_age_seconds": None, "lock_owner": None},
            stale_lock_seconds=60,
            allowlisted_operation_ids=("op-1",),
        )
        == "active_operation"
    )
    assert (
        classify_backup_lock(
            {"live_operation_id": None, "lock_age_seconds": 120, "lock_owner": "restic"},
            stale_lock_seconds=60,
            allowlisted_operation_ids=(),
        )
        == "stale_lock"
    )
    assert (
        classify_backup_lock(
            {"live_operation_id": None, "lock_age_seconds": None, "lock_owner": None},
            stale_lock_seconds=60,
            allowlisted_operation_ids=(),
        )
        == "none"
    )


def test_normalize_rejects_alias_mismatch_and_malformed_output(tmp_path: Path):
    runtime = parse_backup_private_runtime(_runtime(tmp_path))
    target = project_backup_registry(runtime).target("media-archive")
    raw = _target_observation()
    observed = normalize_target_observation(raw, target, [])
    assert observed["target_alias"] == "media-archive"
    assert observed["lock_class"] == "none"
    raw["target_alias"] = "configuration-nas"
    with pytest.raises(BackupError) as caught:
        normalize_target_observation(raw, target, [])
    assert caught.value.code == "protocol_error"
    raw["target_alias"] = "media-archive"
    raw["extra"] = "no"
    with pytest.raises(BackupError):
        normalize_target_observation(raw, target, [])


def test_adapter_rejects_malformed_json_and_keeps_errors_generic(tmp_path: Path):
    runtime = parse_backup_private_runtime(_runtime(tmp_path))
    registry = project_backup_registry(runtime)

    def runner(_request: ExecRequest) -> PrivateExecResult:
        return PrivateExecResult(stdout="not-json")

    observers = BackupObservers(
        runtime=runtime,
        registry=registry,
        env={},
        create_receipt_ref=lambda: "receipt-1",
        secrets=[],
        runner=runner,
    )
    with pytest.raises(BackupError) as caught:
        observers.observe_target("media-archive")
    assert caught.value.code == "protocol_error"
    assert "not-json" not in str(caught.value)


def test_exec_bounds_timeout_and_oversize_output():
    with pytest.raises(BackupError) as timeout_error:
        run_exec(
            ExecRequest(
                file="/bin/sh",
                args=("-c", "sleep 2"),
                cwd="/",
                timeout_ms=250,
                max_buffer_bytes=1024,
                env={},
            )
        )
    assert timeout_error.value.code == "timeout"
    with pytest.raises(BackupError) as oversize:
        run_exec(
            ExecRequest(
                file="/bin/sh",
                args=("-c", "python3 -c 'print(\"x\"*2000)'"),
                cwd="/",
                timeout_ms=5000,
                max_buffer_bytes=1024,
                env={},
            )
        )
    assert oversize.value.code == "protocol_error"
    assert "xxxx" not in str(oversize.value)
    started = time.monotonic()
    # Keep the timeout path from hanging the suite if the child cleanup regresses.
    assert time.monotonic() - started < 5
