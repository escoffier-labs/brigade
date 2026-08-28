"""Host observation normalization and secret sanitization."""

from __future__ import annotations

import re
from typing import Any, Mapping, NoReturn

from .contracts import FleetError, REACHABILITY, is_offset_datetime
from .policy import public_host_fields

_CREDENTIAL_URI_RE = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*://)([^/\s@]+)@")
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_IPV6_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:\[(?:[0-9A-Fa-f]{0,4}:){2,}[0-9A-Fa-f:]{0,4}(?:%[A-Za-z0-9._-]+)?\]|"
    r"(?:[0-9A-Fa-f]{0,4}:){2,}[0-9A-Fa-f:]{0,4}(?:%[A-Za-z0-9._-]+)?)(?![A-Za-z0-9])"
)
_QUOTED_PATH_RE = re.compile(r"(['\"])(?:[A-Za-z]:[\\/]|/)[^\"'\r\n]*\1")
_BARE_PATH_RE = re.compile(r"(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/]|/)[^|;\r\n]*")


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


def _escape_reg_exp(value: str) -> str:
    return re.escape(value)


def sanitize_detail(value: str, secrets: list[str] | tuple[str, ...]) -> str:
    try:
        sanitized = value
        ordered = sorted({secret for secret in secrets if secret}, key=len, reverse=True)
        for secret in ordered:
            bounded = re.compile(rf"(?<![A-Za-z0-9]){_escape_reg_exp(secret)}(?![A-Za-z0-9])")
            sanitized = bounded.sub("[redacted]", sanitized)
        sanitized = _CREDENTIAL_URI_RE.sub(r"\1[redacted]@", sanitized)
        sanitized = _IPV4_RE.sub("[redacted]", sanitized)
        sanitized = _IPV6_RE.sub("[redacted]", sanitized)
        sanitized = _QUOTED_PATH_RE.sub(r"\1[redacted]\1", sanitized)
        sanitized = _BARE_PATH_RE.sub("[redacted]", sanitized)
        return truncate_utf8(sanitized, 4_096)
    except Exception:
        return "[redacted]"


def _uptime_class(value: int | None) -> str:
    if value is None:
        return "unknown"
    if value < 86_400:
        return "under-day"
    if value <= 604_800:
        return "one-to-seven-days"
    return "over-seven-days"


def _storage_pressure(value: float | None) -> str:
    if value is None:
        return "unknown"
    if value < 75:
        return "normal"
    if value < 90:
        return "elevated"
    return "critical"


def _protocol_error() -> NoReturn:
    raise FleetError("protocol_error", "Fleet observation was invalid")


def normalize_host_observation(
    raw: object,
    target: Mapping[str, Any],
    probe_id: str,
    secrets: list[str] | tuple[str, ...],
) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or set(raw) != {
        "alias",
        "probe_id",
        "observed_at",
        "reachability",
        "uptime_seconds",
        "storage_percent",
        "failed_services",
        "reboot_pending",
        "detail",
    }:
        _protocol_error()
    alias = raw.get("alias")
    observed_probe = raw.get("probe_id")
    observed_at = raw.get("observed_at")
    reachability = raw.get("reachability")
    uptime_seconds = raw.get("uptime_seconds")
    storage_percent = raw.get("storage_percent")
    failed_services = raw.get("failed_services")
    reboot_pending = raw.get("reboot_pending")
    detail = raw.get("detail")
    if alias != target["alias"] or observed_probe != probe_id:
        _protocol_error()
    if not isinstance(alias, str) or not 1 <= len(alias) <= 64:
        _protocol_error()
    if not isinstance(observed_probe, str) or not 1 <= len(observed_probe) <= 64:
        _protocol_error()
    if not is_offset_datetime(observed_at):
        _protocol_error()
    if reachability not in REACHABILITY:
        _protocol_error()
    if uptime_seconds is not None and not (type(uptime_seconds) is int and uptime_seconds >= 0):
        _protocol_error()
    if storage_percent is not None:
        if type(storage_percent) not in {int, float} or not 0 <= float(storage_percent) <= 100:
            _protocol_error()
    if not (type(failed_services) is int and failed_services >= 0):
        _protocol_error()
    if not isinstance(reboot_pending, bool):
        _protocol_error()
    if not isinstance(detail, str) or utf8_byte_length(detail) > 16_384:
        _protocol_error()
    candidate = {
        "alias": target["alias"],
        "tier": target["tier"],
        "reachability": reachability,
        "uptime_class": _uptime_class(uptime_seconds),
        "storage_pressure": _storage_pressure(None if storage_percent is None else float(storage_percent)),
        "failed_service_count": failed_services,
        "reboot_pending": reboot_pending,
        "observed_at": observed_at,
        "freshness_seconds": target["freshness_seconds"],
        "changed": False,
        "detail": sanitize_detail(detail, secrets),
    }
    allowed = public_host_fields(target["tier"])
    return {key: value for key, value in candidate.items() if key in allowed}
