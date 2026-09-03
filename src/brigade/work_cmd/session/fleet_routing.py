"""``fleet_routing`` block for ``brigade work brief`` (hub roster page spec).

The Claude SessionStart hook injects the brief, so this is how every
interactive session learns the pinned roles, disabled seats, and cloud lanes
without being told. Bounded to ``BUDGET_SECONDS`` and never raises: a down
hub prints the cache, an unconfigured fleet prints nothing.
"""

from __future__ import annotations

from typing import Any

BUDGET_SECONDS = 3.0
_KEYS = ("roles", "notes", "disabled_seats", "cloud_lanes", "source", "revision", "cached_at")


def _pull() -> tuple[Any, dict[str, Any], dict[str, Any] | None]:
    from ... import fleet_client_cloud, run_preference

    preference = run_preference.refresh_cache()
    try:
        snapshot = fleet_client_cloud.load_model_policy_snapshot()
    except Exception:  # noqa: BLE001
        snapshot = {"state": "unavailable"}
    try:
        cloud = fleet_client_cloud.fetch_cloud()
    except Exception:  # noqa: BLE001 - lanes are optional under a down hub
        cloud = None
    return preference, snapshot, cloud


def _lkg_snapshot() -> dict[str, Any]:
    try:
        from ... import fleet_model_admission

        record = fleet_model_admission._load_lkg_record()
        return {
            "state": "authoritative",
            "source": "lkg",
            "revision": record["roster"].get("revision"),
            "seats": record["roster"].get("seats") or [],
            "cached_at": record.get("cached_at"),
        }
    except Exception:  # noqa: BLE001
        return {"state": "unavailable"}


def fleet_routing_for_brief() -> dict[str, Any] | None:
    """The block, or ``None`` when no fleet hub is configured."""
    from ... import fleet_client, run_preference

    if not fleet_client.load_fleet_settings()["hub_url"]:
        return None
    result: dict[str, Any] = {
        "roles": {},
        "notes": None,
        "disabled_seats": [],
        "cloud_lanes": {},
        "source": "unavailable",
        "revision": None,
        "cached_at": None,
    }
    try:
        preference, snapshot, cloud = fleet_client._run_with_deadline(_pull, timeout=BUDGET_SECONDS)
    except Exception:  # noqa: BLE001 - brief stays fail-open
        preference, snapshot, cloud = run_preference.load_cached(), {"state": "unavailable"}, None
    if snapshot.get("state") != "authoritative":
        snapshot = _lkg_snapshot()
    result["roles"] = {role: value for role in run_preference.ROLE_FIELDS if (value := getattr(preference, role))}
    result["notes"] = preference.notes
    if snapshot.get("state") == "authoritative":
        result["source"] = str(snapshot.get("source") or "hub")
        result["revision"] = snapshot.get("revision")
        result["disabled_seats"] = sorted(
            str(row.get("seat"))
            for row in snapshot.get("seats") or []
            if isinstance(row, dict) and row.get("seat") and row.get("enabled") is not True
        )
        if result["source"] == "lkg":
            if "cached_at" in snapshot:
                result["cached_at"] = snapshot.get("cached_at")
            else:
                try:
                    from ... import fleet_model_admission

                    result["cached_at"] = fleet_model_admission._load_lkg_record().get("cached_at")
                except Exception:  # noqa: BLE001
                    result["cached_at"] = None
    if isinstance(cloud, dict):
        providers = (cloud.get("policy") or {}).get("providers") or []
        result["cloud_lanes"] = {
            str(row["provider"]): bool(row.get("enabled"))
            for row in providers
            if isinstance(row, dict) and row.get("provider")
        }
    return result


def print_fleet_routing(block: dict[str, Any]) -> None:
    roles = block.get("roles") or {}
    rendered = " ".join(f"{role}={seat}" for role, seat in roles.items()) or "(unset)"
    print(f"fleet_routing: {rendered}")
    print(f"fleet_routing_notes: {block.get('notes') or '(none)'}")
    disabled = block.get("disabled_seats") or []
    print(f"fleet_disabled_seats: {', '.join(disabled) if disabled else '(none)'}")
    lanes = block.get("cloud_lanes") or {}
    if lanes:
        print("fleet_cloud_lanes: " + " ".join(f"{name}={'on' if on else 'off'}" for name, on in sorted(lanes.items())))
    else:
        print("fleet_cloud_lanes: unknown (hub unreachable)")
    source = block.get("source")
    revision = block.get("revision")
    if source == "hub":
        print(f"fleet_routing_source: hub revision {revision}")
    elif source == "lkg":
        print(
            f"fleet_routing_source: cache revision {revision} (hub unreachable, cached {block.get('cached_at') or 'unknown'})"
        )
    else:
        print("fleet_routing_source: unavailable (hub unreachable, no cache)")
