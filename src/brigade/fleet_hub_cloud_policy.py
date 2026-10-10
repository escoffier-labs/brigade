"""Atomic updates for administrator-controlled hosted admission limits."""

from __future__ import annotations

import sqlite3
from typing import Any

from . import fleet_command_deck


def set_cloud_policy(
    conn: sqlite3.Connection, request: dict[str, Any], config: fleet_command_deck.DeckConfig
) -> dict[str, Any]:
    from . import fleet_hub

    conn.execute("BEGIN IMMEDIATE")
    try:
        if "global_limit" in request:
            conn.execute(
                "INSERT INTO cloud_global_state (singleton, limit_count) VALUES (1, ?) "
                "ON CONFLICT(singleton) DO UPDATE SET limit_count=excluded.limit_count",
                (request["global_limit"],),
            )
        provider = request["provider"]
        if provider is not None:
            current = fleet_hub._cloud_policy(conn, config)["providers"].get(
                provider, fleet_hub._provider_defaults(config, provider)
            )
            for key in ("enabled", "limit", "hosted", "circuit_state"):
                if request.get(key) is not None:
                    current[key] = request[key]
            for key in ("reason", "subscription_pool", "reset_at", "expires_at"):
                if key in request:
                    current[key] = request[key]
            conn.execute(
                "INSERT INTO cloud_provider_state (provider, enabled, limit_count, hosted, circuit_state, reason, subscription_pool, reset_at, expires_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(provider) DO UPDATE SET enabled=excluded.enabled, limit_count=excluded.limit_count, "
                "hosted=excluded.hosted, circuit_state=excluded.circuit_state, reason=excluded.reason, "
                "subscription_pool=excluded.subscription_pool, reset_at=excluded.reset_at, expires_at=excluded.expires_at, updated_at=excluded.updated_at",
                (
                    provider,
                    int(current["enabled"]),
                    int(current["limit"]),
                    int(current["hosted"]),
                    current["circuit_state"],
                    current.get("reason"),
                    current.get("subscription_pool"),
                    current.get("reset_at"),
                    current.get("expires_at"),
                    fleet_hub._utc_now(),
                ),
            )
        policy = fleet_hub._cloud_policy(conn, config)
        result = {"provider": provider, **policy["providers"][provider]} if provider is not None else {}
        result["global_limit"] = policy["global_limit"]
        conn.commit()
        return result
    except BaseException:
        conn.rollback()
        raise
