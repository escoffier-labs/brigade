"""Registry and authorization tests for Fleet Steward."""

from __future__ import annotations

import pytest

from brigade.grokbot_fleet.contracts import FleetError
from brigade.grokbot_fleet.policy import (
    authorize_host_read,
    authorize_incident_read,
    authorize_service_read,
    public_host_fields,
)
from brigade.grokbot_fleet.registry import create_fleet_registry

TARGETS = [
    {
        "alias": "infra-a",
        "tier": "infrastructure",
        "adapter": "linux",
        "probe_ids": ["host-basic", "service-basic"],
        "timeout_ms": 1_000,
        "freshness_seconds": 60,
    },
    {
        "alias": "protected-a",
        "tier": "protected-status-only",
        "adapter": "windows",
        "probe_ids": ["host-basic"],
        "timeout_ms": 1_000,
        "freshness_seconds": 60,
    },
    {
        "alias": "appliance-a",
        "tier": "appliance-read-only",
        "adapter": "local",
        "probe_ids": ["host-basic"],
        "timeout_ms": 1_000,
        "freshness_seconds": 60,
    },
    {
        "alias": "mobile-a",
        "tier": "indirect-only",
        "adapter": "indirect",
        "probe_ids": ["host-basic"],
        "timeout_ms": 1_000,
        "freshness_seconds": 60,
    },
]
SERVICES = [{"service_id": "dns.primary", "target_alias": "infra-a", "probe_id": "service-basic"}]


def _registry():
    return create_fleet_registry({"targets": TARGETS, "services": SERVICES})


def test_registry_rejects_duplicate_and_inconsistent_records():
    with pytest.raises(FleetError):
        create_fleet_registry({"targets": [*TARGETS, TARGETS[0]], "services": SERVICES})
    with pytest.raises(FleetError):
        create_fleet_registry({"targets": TARGETS, "services": [*SERVICES, SERVICES[0]]})
    with pytest.raises(FleetError):
        create_fleet_registry({"targets": [{**TARGETS[0], "probe_ids": ["host-basic", "host-basic"]}], "services": []})
    with pytest.raises(FleetError):
        create_fleet_registry({"targets": TARGETS, "services": [{**SERVICES[0], "target_alias": "missing-a"}]})
    with pytest.raises(FleetError):
        create_fleet_registry({"targets": TARGETS, "services": [{**SERVICES[0], "probe_id": "missing-probe"}]})


def test_registry_looks_up_targets_services_and_scopes():
    registry = _registry()
    assert registry.target("infra-a")["alias"] == "infra-a"
    assert registry.service("dns.primary")["service_id"] == "dns.primary"
    assert "service" not in registry.scope("infra-a")
    assert registry.scope("dns.primary")["service"]["service_id"] == "dns.primary"
    with pytest.raises(FleetError) as caught:
        registry.target("missing")
    assert caught.value.code == "not_found"
    assert "missing" not in str(caught.value)


def test_authorization_and_public_fields():
    registry = _registry()
    assert authorize_host_read(registry, "infra-a")["alias"] == "infra-a"
    authorized = authorize_service_read(registry, "dns.primary")
    assert authorized["target"]["alias"] == "infra-a"
    assert authorize_incident_read(registry, "dns.primary")["service"]["service_id"] == "dns.primary"
    assert "detail" in public_host_fields("infrastructure")
    assert "detail" not in public_host_fields("protected-status-only")
    assert "uptime_class" not in public_host_fields("appliance-read-only")
    assert public_host_fields("indirect-only") == {
        "alias",
        "tier",
        "reachability",
        "observed_at",
        "freshness_seconds",
        "changed",
    }
