"""Closed aggregate snapshot contract and 400-day Hub storage."""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Any

from . import fleet_client, toml_compat

SCHEMA = "brigade.admin-stats.v1"
RETENTION_DAYS = 400
SOURCE_FIELDS = ("active_sources", "retained_2d_sources", "retained_3d_sources")
WEEK_FIELDS = ("week", "partial", *SOURCE_FIELDS, "versions", "upgrades")
DAILY_FIELDS = ("day", "worker_requests", "worker_errors")
FIELDS = ("schema", "snapshot_id", "taken_at", "source_set", "weeks", "daily", "downloads", "digest")
FIELD_PATHS = (
    *FIELDS,
    *("weeks." + f for f in WEEK_FIELDS),
    "weeks.versions.version",
    "weeks.versions.active_sources",
    "weeks.upgrades.from_version",
    "weeks.upgrades.to_version",
    "weeks.upgrades.active_sources",
    *("daily." + f for f in DAILY_FIELDS),
    "downloads.pypi",
    "downloads.npm",
)
_VERSION = re.compile(r"[0-9]{1,6}(?:\.[0-9]{1,6}){1,3}(?:(?:a|b|rc|\.post|\.dev)[0-9]{1,6})?")


def config() -> dict[str, Any]:
    """Deployment settings live only in the operator's fleet.toml."""
    path = fleet_client.brigade_home() / "fleet.toml"
    try:
        payload = toml_compat.loads(path.read_text(encoding="utf-8"))
        section = payload.get("fleet", {}).get("stats", {})
        return section if isinstance(section, dict) else {}
    except (OSError, ValueError, AttributeError):
        return {}


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS admin_stats_snapshots (
        snapshot_id TEXT PRIMARY KEY, taken_at TEXT NOT NULL, source_set TEXT NOT NULL,
        schema TEXT NOT NULL, payload_json TEXT NOT NULL, digest TEXT NOT NULL)""")
    conn.execute("CREATE INDEX IF NOT EXISTS admin_stats_taken_at ON admin_stats_snapshots(taken_at)")


def _keys(value: Any, fields: tuple[str, ...]) -> None:
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ValueError("invalid snapshot fields")


def _count(value: Any, *, sources: bool = False) -> None:
    if value is not None and (type(value) is not int or not 0 <= value <= 10**15 or (sources and value < 5)):
        raise ValueError("invalid or unsuppressed count")


def _stamp(value: Any) -> datetime:
    if not isinstance(value, str) or len(value) != 20:
        raise ValueError("invalid timestamp")
    parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return parsed


def _week(value: Any) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-W[0-9]{2}", value):
        raise ValueError("invalid week")
    return date.fromisocalendar(int(value[:4]), int(value[-2:]), 1)


def _version(value: Any) -> None:
    if not isinstance(value, str) or _VERSION.fullmatch(value) is None:
        raise ValueError("invalid version")


def _rows(value: Any, limit: int) -> list[Any]:
    if not isinstance(value, list) or len(value) > limit:
        raise ValueError("invalid snapshot series")
    return value


def _digest(value: dict[str, Any]) -> str:
    body = {k: v for k, v in value.items() if k not in ("digest", "snapshot_id")}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def validate(value: Any) -> dict[str, Any]:
    """Reject unknown fields at every depth before checking the canonical digest."""
    _keys(value, FIELDS)
    if value["schema"] != SCHEMA:
        raise ValueError("unsupported snapshot schema")
    taken = _stamp(value["taken_at"])
    sources = _rows(value["source_set"], 3)
    if any(type(s) is not str or s not in ("d1", "worker", "downloads") for s in sources) or len(set(sources)) != len(
        sources
    ):
        raise ValueError("invalid source set")
    seen = set()
    for row in _rows(value["weeks"], 60):
        _keys(row, WEEK_FIELDS)
        start = _week(row["week"])
        if row["week"] in seen or start > taken.date():
            raise ValueError("invalid weekly series")
        seen.add(row["week"])
        if type(row["partial"]) is not bool or (not row["partial"] and start + timedelta(days=7) > taken.date()):
            raise ValueError("invalid partial week")
        for field in SOURCE_FIELDS:
            _count(row[field], sources=True)
        versions = set()
        for cell in _rows(row["versions"], 100):
            _keys(cell, ("version", "active_sources"))
            _version(cell["version"])
            _count(cell["active_sources"], sources=True)
            if cell["version"] in versions:
                raise ValueError("duplicate version")
            versions.add(cell["version"])
        pairs = set()
        for cell in _rows(row["upgrades"], 200):
            _keys(cell, ("from_version", "to_version", "active_sources"))
            _version(cell["from_version"])
            _version(cell["to_version"])
            _count(cell["active_sources"], sources=True)
            pair = (cell["from_version"], cell["to_version"])
            if pair in pairs or pair[0] == pair[1]:
                raise ValueError("invalid upgrade pair")
            pairs.add(pair)
    days = set()
    for row in _rows(value["daily"], 400):
        _keys(row, DAILY_FIELDS)
        if (
            not isinstance(row["day"], str)
            or date.fromisoformat(row["day"]).isoformat() != row["day"]
            or row["day"] > taken.date().isoformat()
            or row["day"] in days
        ):
            raise ValueError("invalid daily series")
        days.add(row["day"])
        for field in DAILY_FIELDS[1:]:
            _count(row[field])
    _keys(value["downloads"], ("pypi", "npm"))
    for count in value["downloads"].values():
        _count(count)
    digest = _digest(value)
    if (
        not isinstance(value["digest"], str)
        or not hmac.compare_digest(value["digest"], digest)
        or value["snapshot_id"] != digest
    ):
        raise ValueError("invalid snapshot digest")
    return value


def snapshot(
    *,
    taken_at: datetime,
    source_set: list[str],
    weeks: list[dict[str, Any]],
    daily: list[dict[str, Any]],
    downloads: dict[str, Any],
) -> dict[str, Any]:
    value: dict[str, Any] = copy.deepcopy(
        {
            "schema": SCHEMA,
            "taken_at": taken_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source_set": sorted(source_set),
            "weeks": weeks,
            "daily": daily,
            "downloads": downloads,
        }
    )
    for row in value["weeks"]:
        for cell, fields in [
            (row, SOURCE_FIELDS),
            *((c, ("active_sources",)) for c in row["versions"] + row["upgrades"]),
        ]:
            for field in fields:
                count = cell[field]
                if type(count) is int and 0 <= count < 5:
                    cell[field] = None
    value["digest"] = value["snapshot_id"] = _digest(value)
    return validate(value)


def store(conn: sqlite3.Connection, value: Any, *, now: datetime | None = None) -> None:
    value = validate(value)
    now = now or datetime.now(timezone.utc)
    taken = _stamp(value["taken_at"])
    if not now - timedelta(days=RETENTION_DAYS) <= taken <= now + timedelta(minutes=5):
        raise ValueError("snapshot timestamp outside retention window")
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO admin_stats_snapshots VALUES (?, ?, ?, ?, ?, ?)",
            (
                value["snapshot_id"],
                value["taken_at"],
                json.dumps(value["source_set"]),
                SCHEMA,
                json.dumps(value),
                value["digest"],
            ),
        )
        conn.execute(
            "DELETE FROM admin_stats_snapshots WHERE taken_at < ?",
            ((now - timedelta(days=RETENTION_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ"),),
        )


def load_view(conn: sqlite3.Connection, *, now: datetime | None = None) -> dict[str, Any]:
    cutoff = ((now or datetime.now(timezone.utc)) - timedelta(days=RETENTION_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = conn.execute(
        "SELECT payload_json FROM admin_stats_snapshots WHERE taken_at >= ? ORDER BY taken_at DESC, snapshot_id DESC LIMIT 1601",
        (cutoff,),
    ).fetchall()
    weeks: dict[str, Any] = {}
    latest = None
    for row in rows:
        value = validate(json.loads(row[0]))
        if latest is None:
            latest = value
        for week in value["weeks"]:
            weeks.setdefault(week["week"], week)
    return {"latest": latest, "weeks": [weeks[k] for k in sorted(weeks, reverse=True)[:60]]}
