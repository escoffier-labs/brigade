"""Read authorization, Wazuh binding, and public host-field projection."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from ..grokbot_wazuh.policy import classify, is_action_eligible
from .contracts import ERROR_MESSAGES, FleetError, parse_fingerprint, parse_identifier, parse_wazuh_finding_id
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


REMEDIATION_ALLOWED_TIER = "infrastructure"
DENIED_REMEDIATION_TIERS = frozenset(
    {
        "protected-status-only",
        "appliance-read-only",
        "indirect-only",
    }
)
DENIED_REMEDIATION_ADAPTERS = frozenset({"windows", "proxmox-guest", "indirect"})
DENIED_TARGET_CLASSES = frozenset({"protected", "appliance", "family", "container", "indirect"})
ENROLLED_WAZUH_AGENTS = {"001": "control-plane"}
WAZUH_ACTION_CATALOG: dict[str, dict[str, Any]] = {
    "service-failure": {
        "action_id": "restart-service",
        "automatic_rollback": False,
        "blast_radius": "one registered service",
        "maintenance_window_id": "control-plane-service",
        "rollback_id": "no-rollback",
        "rule_class": "service-failure",
        "service_id": "research-bridge",
        "target_alias": "control-plane",
        "verification_id": "verify-service",
    }
}
WAZUH_REVALIDATION_KEYS = (
    "action_id",
    "automatic_rollback",
    "blast_radius",
    "finding_id",
    "finding_revision",
    "maintenance_window_id",
    "rollback_id",
    "service_id",
    "target_alias",
    "verification_id",
    "wazuh_fingerprint",
)
MAINTENANCE_WINDOWS: dict[str, dict[str, Any]] = {
    "control-plane-service": {
        "end_minute": 4 * 60,
        "start_minute": 2 * 60,
        "weekdays": frozenset(range(7)),
        "window_id": "control-plane-service",
    }
}


def target_allows_remediation(target: Mapping[str, Any]) -> bool:
    alias = str(target.get("alias") or "")
    tier = str(target.get("tier") or "")
    adapter = str(target.get("adapter") or "")
    if tier != REMEDIATION_ALLOWED_TIER or tier in DENIED_REMEDIATION_TIERS:
        return False
    if adapter in DENIED_REMEDIATION_ADAPTERS:
        return False
    labels = {alias.casefold(), tier.casefold(), adapter.casefold()}
    if labels & DENIED_TARGET_CLASSES:
        return False
    if any(token in labels for token in DENIED_TARGET_CLASSES):
        return False
    return True


def in_maintenance_window(window_id: str, now: datetime) -> bool:
    window = MAINTENANCE_WINDOWS.get(window_id)
    if window is None:
        return False
    stamp = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    stamp = stamp.astimezone(timezone.utc)
    if stamp.weekday() not in window["weekdays"]:
        return False
    minute = stamp.hour * 60 + stamp.minute
    return window["start_minute"] <= minute < window["end_minute"]


def catalog_for_wazuh_finding(finding: Mapping[str, Any]) -> dict[str, Any] | None:
    rule_class = finding.get("rule_class")
    if not isinstance(rule_class, str):
        return None
    entry = WAZUH_ACTION_CATALOG.get(rule_class)
    return None if entry is None else dict(entry)


def bind_wazuh_remediation(
    finding: Mapping[str, Any],
    *,
    registry: FleetRegistry,
    now: datetime,
    suppressions: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...] = (),
) -> dict[str, Any]:
    decision = classify(finding, suppressions=suppressions, now=now)
    if not is_action_eligible(decision):
        raise FleetError("denied", ERROR_MESSAGES["denied"])
    catalog = catalog_for_wazuh_finding(finding)
    if catalog is None:
        raise FleetError("denied", ERROR_MESSAGES["denied"])
    agent_id = finding.get("agent_id")
    enrolled = ENROLLED_WAZUH_AGENTS.get(agent_id) if isinstance(agent_id, str) else None
    if enrolled != catalog["target_alias"]:
        raise FleetError("denied", ERROR_MESSAGES["denied"])
    try:
        target = authorize_host_read(registry, catalog["target_alias"])
    except FleetError as exc:
        if exc.code in {"not_found", "denied"}:
            raise FleetError("denied", ERROR_MESSAGES["denied"]) from exc
        raise
    if not target_allows_remediation(target):
        raise FleetError("denied", ERROR_MESSAGES["denied"])
    if not in_maintenance_window(str(catalog["maintenance_window_id"]), now):
        raise FleetError("denied", ERROR_MESSAGES["denied"])
    try:
        fingerprint = parse_fingerprint(finding.get("fingerprint"))
        revision = parse_identifier(finding.get("revision"), maximum=64)
        finding_id = parse_wazuh_finding_id(finding.get("finding_id"))
    except FleetError as exc:
        raise FleetError("denied", ERROR_MESSAGES["denied"]) from exc
    return {
        "action_id": catalog["action_id"],
        "automatic_rollback": bool(catalog["automatic_rollback"]),
        "blast_radius": catalog["blast_radius"],
        "finding_id": finding_id,
        "finding_revision": revision,
        "maintenance_window_id": catalog["maintenance_window_id"],
        "rollback_id": catalog["rollback_id"],
        "service_id": catalog["service_id"],
        "target_alias": catalog["target_alias"],
        "verification_id": catalog["verification_id"],
        "wazuh_fingerprint": fingerprint,
    }
