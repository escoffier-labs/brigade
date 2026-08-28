"""Backup observation normalization and secret sanitization."""

from __future__ import annotations

import re
from typing import Any, Mapping, NoReturn, Sequence

from .contracts import (
    BackupError,
    HEALTH_CLASSES,
    LOCK_CLASSES,
    is_offset_datetime,
    omit_undefined,
    parse_identifier,
)

_PID_RE = re.compile(r"\bpid=\d+\b", re.IGNORECASE)
_URL_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9+.-]*://[^\s]+")
_REST_URL_RE = re.compile(r"\brest:[A-Za-z][A-Za-z0-9+.-]*://[^\s]+")
_CREDENTIAL_URI_RE = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*://)([^/\s@]+)@")
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_IPV6_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:\[(?:[0-9A-Fa-f]{0,4}:){2,}[0-9A-Fa-f:]{0,4}(?:%[A-Za-z0-9._-]+)?\]|"
    r"(?:[0-9A-Fa-f]{0,4}:){2,}[0-9A-Fa-f:]{0,4}(?:%[A-Za-z0-9._-]+)?)(?![A-Za-z0-9])"
)
_HOSTNAME_RE = re.compile(r"\b[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+\b")
_QUOTED_PATH_RE = re.compile(r"(['\"])(?:[A-Za-z]:[\\/]|/)[^\"'\r\n]*\1")
_BARE_PATH_RE = re.compile(r"(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/]|/)[^|;\r\n]*")
RAW_TARGET_KEYS = frozenset(
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
    }
)
RAW_READINESS_KEYS = frozenset(
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
    }
)


def utf8_byte_length(value: str) -> int:
    return len(value.encode("utf-8"))


def truncate_utf8(text: str, max_bytes: int) -> str:
    result: list[str] = []
    used = 0
    for char in text:
        next_size = utf8_byte_length(char)
        if used + next_size > max_bytes:
            break
        result.append(char)
        used += next_size
    return "".join(result)


def sanitize_backup_detail(value: str, secrets: Sequence[str]) -> str:
    try:
        sanitized = value
        ordered = sorted({secret for secret in secrets if secret}, key=len, reverse=True)
        for secret in ordered:
            bounded = re.compile(rf"(?<![A-Za-z0-9]){re.escape(secret)}(?![A-Za-z0-9])")
            sanitized = bounded.sub("[redacted]", sanitized)
        sanitized = _PID_RE.sub("pid=[redacted]", sanitized)
        sanitized = _URL_RE.sub("[redacted]", sanitized)
        sanitized = _REST_URL_RE.sub("[redacted]", sanitized)
        sanitized = _CREDENTIAL_URI_RE.sub(r"\1[redacted]@", sanitized)
        sanitized = _IPV4_RE.sub("[redacted]", sanitized)
        sanitized = _IPV6_RE.sub("[redacted]", sanitized)
        sanitized = _HOSTNAME_RE.sub("[redacted]", sanitized)
        sanitized = _QUOTED_PATH_RE.sub(r"\1[redacted]\1", sanitized)
        sanitized = _BARE_PATH_RE.sub("[redacted]", sanitized)
        return truncate_utf8(sanitized, 4_096)
    except Exception:
        return "[redacted]"


def classify_backup_lock(
    input_value: Mapping[str, Any],
    *,
    stale_lock_seconds: int,
    allowlisted_operation_ids: Sequence[str],
) -> str:
    live_id = input_value.get("live_operation_id")
    if live_id is not None and live_id in allowlisted_operation_ids:
        return "active_operation"
    age = input_value.get("lock_age_seconds")
    owner = input_value.get("lock_owner")
    if age is None and owner is None:
        return "none"
    if owner is None:
        return "unknown_lock"
    if age is not None and age > stale_lock_seconds:
        return "stale_lock"
    return "unknown_lock"


def _protocol_error() -> NoReturn:
    raise BackupError("protocol_error", "Backup observation was invalid")


def _optional_nonnegative_int(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 0:
        _protocol_error()
    return value


def _optional_identifier(value: object) -> str | None:
    if value is None:
        return None
    try:
        return parse_identifier(value)
    except BackupError:
        _protocol_error()


def _bounded_state(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        _protocol_error()
    return value


def normalize_target_observation(
    raw: object,
    target: Mapping[str, Any],
    secrets: Sequence[str],
    *,
    allowlisted_operation_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or set(raw) != RAW_TARGET_KEYS:
        _protocol_error()
    try:
        alias = parse_identifier(raw.get("target_alias"))
        reporter_id = parse_identifier(raw.get("reporter_id"))
    except BackupError:
        _protocol_error()
    if alias != target["alias"] or reporter_id != target["reporter_id"]:
        _protocol_error()
    if not is_offset_datetime(raw.get("observed_at")) or raw.get("health") not in HEALTH_CLASSES:
        _protocol_error()
    detail = raw.get("detail")
    if not isinstance(detail, str) or utf8_byte_length(detail) > 16_384:
        _protocol_error()
    lock_class = classify_backup_lock(
        {
            "live_operation_id": _optional_identifier(raw.get("live_operation_id")),
            "lock_age_seconds": _optional_nonnegative_int(raw.get("lock_age_seconds")),
            "lock_owner": None if raw.get("lock_owner") is None else _bounded_state(raw.get("lock_owner")),
        },
        stale_lock_seconds=target["stale_lock_seconds"],
        allowlisted_operation_ids=allowlisted_operation_ids or (),
    )
    if lock_class not in LOCK_CLASSES:
        _protocol_error()
    observation = {
        "target_alias": target["alias"],
        "health": raw["health"],
        "lock_class": lock_class,
        "snapshot_age_seconds": _optional_nonnegative_int(raw.get("snapshot_age_seconds")),
        "last_successful_operation": None
        if raw.get("last_successful_operation") is None
        else _bounded_state(raw.get("last_successful_operation")),
        "scheduler_state": _bounded_state(raw.get("scheduler_state")),
        "integrity_state": _bounded_state(raw.get("integrity_state")),
        "retention_evidence_state": _bounded_state(raw.get("retention_evidence_state")),
        "observed_at": raw["observed_at"],
        "freshness_seconds": target["freshness_seconds"],
        "changed": False,
        "detail": sanitize_backup_detail(detail, secrets),
    }
    return omit_undefined(observation)


def normalize_restore_readiness(
    raw: object,
    target: Mapping[str, Any],
    secrets: Sequence[str],
) -> dict[str, Any]:
    expected = target.get("readiness_reporter_id")
    if expected is None or not isinstance(raw, Mapping) or set(raw) != RAW_READINESS_KEYS:
        _protocol_error()
    try:
        alias = parse_identifier(raw.get("target_alias"))
        reporter_id = parse_identifier(raw.get("reporter_id"))
    except BackupError:
        _protocol_error()
    if alias != target["alias"] or reporter_id != expected:
        _protocol_error()
    if not is_offset_datetime(raw.get("observed_at")) or raw.get("readiness") not in HEALTH_CLASSES:
        _protocol_error()
    detail = raw.get("detail")
    if not isinstance(detail, str) or utf8_byte_length(detail) > 16_384:
        _protocol_error()
    last_rehearsal_at = raw.get("last_rehearsal_at")
    if last_rehearsal_at is not None and not is_offset_datetime(last_rehearsal_at):
        _protocol_error()
    readiness = {
        "target_alias": target["alias"],
        "readiness": raw["readiness"],
        "last_rehearsal_at": last_rehearsal_at,
        "last_rehearsal_result": None
        if raw.get("last_rehearsal_result") is None
        else _bounded_state(raw.get("last_rehearsal_result")),
        "evidence_freshness_seconds": _optional_nonnegative_int(raw.get("evidence_freshness_seconds")),
        "supported_recovery_scope": None
        if raw.get("supported_recovery_scope") is None
        else _bounded_state(raw.get("supported_recovery_scope")),
        "observed_at": raw["observed_at"],
        "detail": sanitize_backup_detail(detail, secrets),
    }
    return omit_undefined(readiness)
