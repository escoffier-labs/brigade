"""Normalization and sanitization tests for Fleet Steward."""

from __future__ import annotations

import pytest

from brigade.grokbot_fleet.contracts import FleetError
from brigade.grokbot_fleet.normalize import normalize_host_observation, sanitize_detail

TARGET = {
    "alias": "control-plane",
    "tier": "infrastructure",
    "adapter": "local",
    "probe_ids": ["linux-host-summary-v1"],
    "timeout_ms": 12_000,
    "freshness_seconds": 60,
}


def test_sanitize_detail_redacts_secrets_uris_addresses_and_paths():
    redaction_marker = "sensitive-marker-value"
    detail = (
        f"marker={redaction_marker} uri=https://user:pass@example.invalid/path "
        "ipv4=198.51.100.1 ipv6=2001:db8::1 quoted='/var/lib/secret' bare=/etc/secret"
    )
    sanitized = sanitize_detail(detail, [redaction_marker])
    assert redaction_marker not in sanitized
    assert "user:pass" not in sanitized
    assert "198.51.100.1" not in sanitized
    assert "2001:db8::1" not in sanitized
    assert "/var/lib/secret" not in sanitized
    assert "/etc/secret" not in sanitized
    assert "[redacted]" in sanitized


def test_normalize_host_observation_maps_classes_and_filters_fields():
    raw = {
        "alias": "control-plane",
        "probe_id": "linux-host-summary-v1",
        "observed_at": "2026-08-27T12:00:00Z",
        "reachability": "reachable",
        "uptime_seconds": 90_000,
        "storage_percent": 80,
        "failed_services": 1,
        "reboot_pending": False,
        "detail": "ok",
    }
    observation = normalize_host_observation(raw, TARGET, "linux-host-summary-v1", [])
    assert observation["uptime_class"] == "one-to-seven-days"
    assert observation["storage_pressure"] == "elevated"
    assert observation["failed_service_count"] == 1
    protected = {**TARGET, "tier": "protected-status-only"}
    filtered = normalize_host_observation(raw, protected, "linux-host-summary-v1", [])
    assert "detail" not in filtered
    with pytest.raises(FleetError) as caught:
        normalize_host_observation({**raw, "alias": "other"}, TARGET, "linux-host-summary-v1", [])
    assert caught.value.code == "protocol_error"
