"""`brigade status` - show which stations are present and healthy."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TypedDict

from . import doctor as _doctor
from .registry import all_stations


class StatusRow(TypedDict):
    station: str
    health: str
    ok: int
    warn: int
    fail: int
    summary: str


STATUS_STATES = (
    "not-installed",
    "not-configured",
    "unchecked",
    "ok",
    "degraded",
    "failed",
)


def _normalize_payload_health(raw: object, *, installed: bool | None) -> str:
    value = str(raw or "").lower()
    if installed is False and value in {"", "manual", "missing"}:
        return "not-installed"
    if value == "missing":
        return "unchecked"
    return {
        "ok": "ok",
        "warn": "degraded",
        "fail": "failed",
        "timeout": "degraded",
        "incomplete": "degraded",
        "unwired": "not-configured",
    }.get(value, "unchecked")


def _health_from_checks(checks: list[_doctor.CheckResult]) -> str:
    levels = {status for status, _name, _detail in checks}
    if _doctor.FAIL in levels:
        return "failed"
    if _doctor.WARN in levels:
        return "degraded"
    if _doctor.OK in levels:
        return "ok"
    if _doctor.MANUAL in levels or _doctor.INFO in levels:
        return "not-configured"
    return "unchecked"


def _optional_station_payload(station: str, target: Path) -> dict[str, object] | None:
    if station == "tokens":
        from . import tokens_cmd

        return tokens_cmd.status_payload(target)
    if station == "search":
        from . import search_cmd

        return search_cmd.status_payload(target)
    if station == "pantry":
        from . import pantry_cmd

        return pantry_cmd.status_payload(target)
    if station == "notifications":
        from . import notifications_cmd

        return notifications_cmd._status_payload()
    if station == "evidence":
        from . import evidence_cmd

        return evidence_cmd.status_payload(target, include_doctor=False, timeout=5.0)
    return None


def run(target: Path, *, json_output: bool = False) -> int:
    ctx = _doctor.build_context(target)
    rows: list[StatusRow] = []
    for station in all_stations():
        payload = _optional_station_payload(station.name, ctx.target)
        if payload is not None:
            installed_raw = payload.get("installed")
            installed = installed_raw if isinstance(installed_raw, bool) else None
            health = _normalize_payload_health(
                payload.get("health") or payload.get("status"),
                installed=installed,
            )
            ok = int(health == "ok")
            warn = int(health == "degraded")
            fail = int(health == "failed")
            summary = str(payload.get("summary") or station.summary)
        else:
            checks = station.doctor(ctx) if station.doctor else []
            ok = sum(1 for s, _, _ in checks if s == _doctor.OK)
            warn = sum(1 for s, _, _ in checks if s == _doctor.WARN)
            fail = sum(1 for s, _, _ in checks if s == _doctor.FAIL)
            health = _health_from_checks(checks)
            summary = station.summary
        rows.append(
            {
                "station": station.name,
                "health": health,
                "ok": ok,
                "warn": warn,
                "fail": fail,
                "summary": summary,
            }
        )

    if json_output:
        print(json.dumps({"target": str(ctx.target), "stations": rows}, indent=2, sort_keys=True))
        return 0

    print(f"brigade status: {ctx.target}")
    width = max((len(s.name) for s in all_stations()), default=8)
    for row in rows:
        print(
            f"  {row['station'].ljust(width)}  [{row['health']}]  "
            f"{row['ok']} ok, {row['warn']} warn, {row['fail']} fail  - {row['summary']}"
        )
    return 0
