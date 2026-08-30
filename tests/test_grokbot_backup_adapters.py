"""Backup Steward adapter, normalize, and exec-bound tests."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from brigade.grokbot_backup.adapters import WORKING_DIRECTORY, BackupObservers
from brigade.grokbot_backup.contracts import BackupError
from brigade.grokbot_backup.exec import EXEC_DEFAULT_OUTPUT_BYTES, ExecRequest, PrivateExecResult, run_exec
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


def _restore_readiness(alias: str = "media-archive") -> dict[str, object]:
    return {
        "target_alias": alias,
        "reporter_id": "restic-restore-readiness-v1",
        "observed_at": "2026-08-28T00:00:00Z",
        "readiness": "ready",
        "last_rehearsal_at": "2026-08-27T00:00:00Z",
        "last_rehearsal_result": "ok",
        "evidence_freshness_seconds": 120,
        "supported_recovery_scope": "target",
        "detail": "ok",
    }


def _operation_observation(operation_id: str = "op-abc123def456") -> dict[str, object]:
    return {
        "operation_id": operation_id,
        "target_alias": "media-archive",
        "action_id": "run-backup",
        "state": "succeeded",
        "created_at": "2026-08-28T00:00:00Z",
        "updated_at": "2026-08-28T00:00:00Z",
        "summary": "ok",
    }


def _observers(tmp_path: Path, runner):
    runtime_payload = _runtime(tmp_path)
    for command in runtime_payload["reporters"].values():
        command["args"] = ["--format", "json"]
    runtime = parse_backup_private_runtime(runtime_payload)
    registry = project_backup_registry(runtime)
    observers = BackupObservers(
        runtime=runtime,
        registry=registry,
        env={},
        create_receipt_ref=lambda: "receipt-1",
        secrets=[],
        runner=runner,
    )
    return observers, runtime, registry


def _assert_fixed_exec_bounds(request: ExecRequest, *, executable: str, timeout_ms: int) -> None:
    assert request.file == executable
    assert request.cwd == WORKING_DIRECTORY
    assert request.env == {}
    assert request.timeout_ms == timeout_ms
    assert request.max_buffer_bytes == EXEC_DEFAULT_OUTPUT_BYTES
    assert request.stdin is None
    assert isinstance(request.args, tuple)


def test_observe_target_appends_one_validated_target_alias_selector(tmp_path: Path):
    captured: list[ExecRequest] = []

    def runner(request: ExecRequest) -> PrivateExecResult:
        captured.append(request)
        return PrivateExecResult(stdout=json.dumps(_target_observation()))

    observers, runtime, registry = _observers(tmp_path, runner)
    target = registry.target("media-archive")
    command = runtime["reporters"][target["reporter_id"]]
    result = observers.observe_target("media-archive")
    assert result["receipt_ref"] == "receipt-1"
    assert len(captured) == 1
    request = captured[0]
    _assert_fixed_exec_bounds(
        request,
        executable=command["executable"],
        timeout_ms=target["timeout_ms"],
    )
    assert request.args == (*command["args"], "--target-alias", target["alias"])
    assert request.args[: len(command["args"])] == command["args"]
    assert request.args[len(command["args"]) :] == ("--target-alias", target["alias"])


def test_observe_restore_readiness_appends_one_validated_target_alias_selector(tmp_path: Path):
    captured: list[ExecRequest] = []

    def runner(request: ExecRequest) -> PrivateExecResult:
        captured.append(request)
        return PrivateExecResult(stdout=json.dumps(_restore_readiness()))

    observers, runtime, registry = _observers(tmp_path, runner)
    target = registry.target("media-archive")
    command = runtime["reporters"][target["readiness_reporter_id"]]
    result = observers.observe_restore_readiness("media-archive")
    assert result["receipt_ref"] == "receipt-1"
    assert len(captured) == 1
    request = captured[0]
    _assert_fixed_exec_bounds(
        request,
        executable=command["executable"],
        timeout_ms=target["timeout_ms"],
    )
    assert request.args == (*command["args"], "--target-alias", target["alias"])
    assert request.args[: len(command["args"])] == command["args"]
    assert request.args[len(command["args"]) :] == ("--target-alias", target["alias"])


def test_observe_operation_appends_one_validated_operation_id_selector(tmp_path: Path):
    captured: list[ExecRequest] = []
    operation_id = "op-abc123def456"

    def runner(request: ExecRequest) -> PrivateExecResult:
        captured.append(request)
        return PrivateExecResult(stdout=json.dumps(_operation_observation(operation_id)))

    observers, runtime, registry = _observers(tmp_path, runner)
    command = runtime["reporters"]["backup-operation-status-v1"]
    result = observers.observe_operation(operation_id)
    assert result["receipt_ref"] == "receipt-1"
    assert len(captured) == 1
    request = captured[0]
    _assert_fixed_exec_bounds(
        request,
        executable=command["executable"],
        timeout_ms=registry.targets[0]["timeout_ms"],
    )
    assert request.args == (*command["args"], "--operation-id", operation_id)
    assert request.args[: len(command["args"])] == command["args"]
    assert request.args[len(command["args"]) :] == ("--operation-id", operation_id)


def test_invalid_reporter_selectors_fail_before_runner(tmp_path: Path):
    captured: list[ExecRequest] = []

    def runner(request: ExecRequest) -> PrivateExecResult:
        captured.append(request)
        raise AssertionError("reporter runner must not run for invalid selectors")

    observers, _runtime_payload, _registry = _observers(tmp_path, runner)
    invalid = ("-leading", "a" * 65)
    for value in invalid:
        with pytest.raises(BackupError) as target_error:
            observers.observe_target(value)
        assert target_error.value.code == "not_found"
        with pytest.raises(BackupError) as readiness_error:
            observers.observe_restore_readiness(value)
        assert readiness_error.value.code == "not_found"
        with pytest.raises(BackupError) as operation_error:
            observers.observe_operation(value)
        assert operation_error.value.code == "invalid_request"
    assert captured == []


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
