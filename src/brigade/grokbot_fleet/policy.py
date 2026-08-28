"""Read authorization and public host-field projection."""

from __future__ import annotations

from typing import Any, Mapping

from .registry import FleetRegistry

HOST_FIELDS: dict[str, tuple[str, ...]] = {
    "infrastructure": (
        "alias",
        "tier",
        "reachability",
        "uptime_class",
        "storage_pressure",
        "failed_service_count",
        "reboot_pending",
        "observed_at",
        "freshness_seconds",
        "changed",
        "detail",
    ),
    "protected-status-only": (
        "alias",
        "tier",
        "reachability",
        "uptime_class",
        "storage_pressure",
        "failed_service_count",
        "reboot_pending",
        "observed_at",
        "freshness_seconds",
        "changed",
    ),
    "appliance-read-only": (
        "alias",
        "tier",
        "reachability",
        "storage_pressure",
        "observed_at",
        "freshness_seconds",
        "changed",
    ),
    "indirect-only": (
        "alias",
        "tier",
        "reachability",
        "observed_at",
        "freshness_seconds",
        "changed",
    ),
}


def authorize_host_read(registry: FleetRegistry, alias: str) -> Mapping[str, Any]:
    return registry.target(alias)


def authorize_service_read(registry: FleetRegistry, service_id: str) -> dict[str, Any]:
    service = registry.service(service_id)
    return {"target": registry.target(service["target_alias"]), "service": service}


def authorize_incident_read(registry: FleetRegistry, scope: str) -> dict[str, Any]:
    return registry.scope(scope)


def public_host_fields(tier: str) -> frozenset[str]:
    return frozenset(HOST_FIELDS[tier])
