"""Fixed-command Backup Steward observers."""

from __future__ import annotations

import json
from typing import Any, Callable, Mapping, NoReturn, Sequence

from .contracts import ACTION_IDS, OPERATION_STATES, BackupError, is_offset_datetime, parse_action_id, parse_identifier
from .exec import (
    EXEC_DEFAULT_OUTPUT_BYTES,
    BackupProcessLimiter,
    ExecRequest,
    Runner,
    create_process_limiter,
    run_exec,
)
from .runtime_config import BackupRegistry

WORKING_DIRECTORY = "/"


def _invalid_request() -> NoReturn:
    raise BackupError("invalid_request", "Backup observation request is invalid")


def _protocol_error() -> NoReturn:
    raise BackupError("protocol_error", "Backup observation was invalid")


def _assert_absolute_executable(executable: str) -> None:
    if not executable.startswith("/") or "\0" in executable:
        _invalid_request()
    parts = executable.split("/")
    for part in parts[1:]:
        if part in {"", ".", ".."}:
            _invalid_request()


def _parse_json(stdout: str) -> object:
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        _protocol_error()


def _require_mapping(raw: object, keys: set[str]) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping) or set(raw) != keys:
        _protocol_error()
    return raw


def parse_raw_target_observation(raw: object) -> dict[str, Any]:
    payload = _require_mapping(
        raw,
        {
            "target_alias",
            "reporter_id",
            "observed_at",
            "health",
            "snapshot_age_seconds",
            "last_successful_operation",
            "scheduler_state",
            "integrity_state",
            "retention_evidence_state",
            "live_operation_id",
            "lock_age_seconds",
            "lock_owner",
            "detail",
        },
    )
    if not is_offset_datetime(payload.get("observed_at")):
        _protocol_error()
    return dict(payload)


def parse_raw_restore_readiness(raw: object) -> dict[str, Any]:
    payload = _require_mapping(
        raw,
        {
            "target_alias",
            "reporter_id",
            "observed_at",
            "readiness",
            "last_rehearsal_at",
            "last_rehearsal_result",
            "evidence_freshness_seconds",
            "supported_recovery_scope",
            "detail",
        },
    )
    if not is_offset_datetime(payload.get("observed_at")):
        _protocol_error()
    return dict(payload)


def parse_raw_operation(raw: object) -> dict[str, Any]:
    payload = _require_mapping(
        raw,
        {
            "operation_id",
            "target_alias",
            "action_id",
            "state",
            "created_at",
            "updated_at",
            "summary",
        },
    )
    try:
        parse_identifier(payload.get("operation_id"))
        parse_identifier(payload.get("target_alias"))
        parse_action_id(payload.get("action_id"))
    except BackupError:
        _protocol_error()
    if payload.get("state") not in OPERATION_STATES:
        _protocol_error()
    if not is_offset_datetime(payload.get("created_at")) or not is_offset_datetime(payload.get("updated_at")):
        _protocol_error()
    summary = payload.get("summary")
    if not isinstance(summary, str) or len(summary.encode("utf-8")) > 4_096:
        _protocol_error()
    if payload.get("action_id") not in ACTION_IDS:
        _protocol_error()
    return dict(payload)


class BackupObservers:
    def __init__(
        self,
        *,
        runtime: Mapping[str, Any],
        registry: BackupRegistry,
        env: Mapping[str, str],
        create_receipt_ref: Callable[[], str],
        secrets: Sequence[str],
        runner: Runner | None = None,
        process_limiter: BackupProcessLimiter | None = None,
    ):
        self.runtime = runtime
        self.registry = registry
        self.env = env
        self.create_receipt_ref = create_receipt_ref
        self.secrets = secrets
        self.runner = runner
        self.limiter = process_limiter or create_process_limiter()

    def observe_target(self, alias: str) -> dict[str, Any]:
        target = self.registry.target(alias)
        return self._observe(
            self.runtime["reporters"][target["reporter_id"]],
            target["timeout_ms"],
            parse_raw_target_observation,
            ("--target-alias", target["alias"]),
        )

    def observe_restore_readiness(self, alias: str) -> dict[str, Any]:
        target = self.registry.target(alias)
        reporter_id = target.get("readiness_reporter_id")
        if reporter_id is None:
            raise BackupError("invalid_request", "Backup observation request is invalid")
        return self._observe(
            self.runtime["reporters"][reporter_id],
            target["timeout_ms"],
            parse_raw_restore_readiness,
            ("--target-alias", target["alias"]),
        )

    def observe_operation(self, operation_id: str) -> dict[str, Any]:
        validated = parse_identifier(operation_id)
        timeout_ms = self.registry.targets[0]["timeout_ms"] if self.registry.targets else 12_000
        return self._observe(
            self.runtime["reporters"]["backup-operation-status-v1"],
            timeout_ms,
            parse_raw_operation,
            ("--operation-id", validated),
        )

    def _observe(
        self,
        command: Mapping[str, Any] | None,
        timeout_ms: int,
        parser: Callable[[object], dict[str, Any]],
        selector: tuple[str, str],
    ) -> dict[str, Any]:
        if command is None:
            raise BackupError("invalid_request", "Backup observation request is invalid")
        stdout = self.limiter.run(lambda: self._run_reporter(command, timeout_ms, selector))
        return {"raw": parser(_parse_json(stdout)), "receipt_ref": self.create_receipt_ref()}

    def _run_reporter(self, command: Mapping[str, Any], timeout_ms: int, selector: tuple[str, str]) -> str:
        executable = command["executable"]
        _assert_absolute_executable(executable)
        result = run_exec(
            ExecRequest(
                file=executable,
                args=tuple(command["args"]) + selector,
                cwd=WORKING_DIRECTORY,
                timeout_ms=timeout_ms,
                max_buffer_bytes=EXEC_DEFAULT_OUTPUT_BYTES,
                env=self.env,
            ),
            runner=self.runner,
        )
        return result.stdout
