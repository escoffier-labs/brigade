"""Closed Fleet Steward target and service registry."""

from __future__ import annotations

from types import MappingProxyType
from typing import Any, Mapping, NoReturn

from .contracts import FleetError, TIERS, parse_identifier

ADAPTERS = frozenset({"local", "linux", "windows", "proxmox-guest", "indirect"})
NEUTRAL_NOT_FOUND = "Fleet target was not found"


def _not_found() -> NoReturn:
    raise FleetError("not_found", NEUTRAL_NOT_FOUND)


def _duplicate(values: list[str]) -> bool:
    return len(values) != len(set(values))


def _bounded_int(value: object, minimum: int, maximum: int) -> bool:
    return type(value) is int and minimum <= value <= maximum


def _parse_target(raw: Mapping[str, Any]) -> dict[str, Any]:
    if set(raw) != {"alias", "tier", "adapter", "probe_ids", "timeout_ms", "freshness_seconds"}:
        raise FleetError("invalid_request", "Tool input failed validation")
    alias = parse_identifier(raw.get("alias"))
    tier = raw.get("tier")
    adapter = raw.get("adapter")
    probe_ids = raw.get("probe_ids")
    if tier not in TIERS or adapter not in ADAPTERS:
        raise FleetError("invalid_request", "Tool input failed validation")
    if not isinstance(probe_ids, list) or not 1 <= len(probe_ids) <= 16:
        raise FleetError("invalid_request", "Tool input failed validation")
    parsed_probes = tuple(parse_identifier(probe_id) for probe_id in probe_ids)
    if _duplicate(list(parsed_probes)):
        raise FleetError("invalid_request", "Duplicate probe IDs are not allowed on a target")
    if not _bounded_int(raw.get("timeout_ms"), 250, 30_000):
        raise FleetError("invalid_request", "Tool input failed validation")
    if not _bounded_int(raw.get("freshness_seconds"), 0, 86_400):
        raise FleetError("invalid_request", "Tool input failed validation")
    return {
        "alias": alias,
        "tier": tier,
        "adapter": adapter,
        "probe_ids": parsed_probes,
        "timeout_ms": raw["timeout_ms"],
        "freshness_seconds": raw["freshness_seconds"],
    }


def _parse_service(raw: Mapping[str, Any]) -> dict[str, Any]:
    if set(raw) != {"service_id", "target_alias", "probe_id"}:
        raise FleetError("invalid_request", "Tool input failed validation")
    return {
        "service_id": parse_identifier(raw.get("service_id")),
        "target_alias": parse_identifier(raw.get("target_alias")),
        "probe_id": parse_identifier(raw.get("probe_id")),
    }


class FleetRegistry:
    """Exact-key frozen target and service lookup."""

    def __init__(self, targets: tuple[MappingProxyType, ...], services: tuple[MappingProxyType, ...]):
        self.targets = targets
        self.services = services
        self._targets = {target["alias"]: target for target in targets}
        self._services = {service["service_id"]: service for service in services}

    def target(self, alias: str) -> MappingProxyType:
        found = self._targets.get(alias)
        if found is None:
            _not_found()
        return found

    def service(self, service_id: str) -> MappingProxyType:
        found = self._services.get(service_id)
        if found is None:
            _not_found()
        return found

    def scope(self, scope: str) -> dict[str, Any]:
        target = self._targets.get(scope)
        if target is not None:
            return {"target": target}
        service = self._services.get(scope)
        if service is None:
            _not_found()
        return {"target": self.target(service["target_alias"]), "service": service}


def create_fleet_registry(raw: object) -> FleetRegistry:
    if not isinstance(raw, Mapping) or set(raw) != {"targets", "services"}:
        raise FleetError("invalid_request", "Tool input failed validation")
    targets_raw = raw.get("targets")
    services_raw = raw.get("services")
    if not isinstance(targets_raw, list) or not isinstance(services_raw, list):
        raise FleetError("invalid_request", "Tool input failed validation")
    if len(targets_raw) > 64 or len(services_raw) > 128:
        raise FleetError("invalid_request", "Tool input failed validation")
    targets = tuple(MappingProxyType(_parse_target(item)) for item in targets_raw)
    if _duplicate([target["alias"] for target in targets]):
        raise FleetError("invalid_request", "Duplicate target aliases are not allowed")
    services = tuple(MappingProxyType(_parse_service(item)) for item in services_raw)
    if _duplicate([service["service_id"] for service in services]):
        raise FleetError("invalid_request", "Duplicate service IDs are not allowed")
    aliases = {target["alias"] for target in targets}
    probes_by_alias = {target["alias"]: set(target["probe_ids"]) for target in targets}
    for service in services:
        if service["target_alias"] not in aliases:
            raise FleetError("invalid_request", "Service references an unknown target")
        if service["probe_id"] not in probes_by_alias[service["target_alias"]]:
            raise FleetError("invalid_request", "Service references an unknown probe")
    return FleetRegistry(targets, services)
