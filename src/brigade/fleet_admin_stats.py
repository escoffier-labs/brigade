"""Closed aggregate snapshot contract and 400-day Hub storage."""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import re
import sqlite3
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import fleet_client, toml_compat

if TYPE_CHECKING:
    from .fleet_hub_sealed import StoragePolicy

SCHEMA = "brigade.admin-stats.v2"
LEGACY_SCHEMA = "brigade.admin-stats.v1"
RETENTION_DAYS = 400
RETENTION_INTERVAL_SECONDS = 3600
SOURCE_FIELDS = ("active_sources", "retained_2d_sources", "retained_3d_sources")
WEEK_FIELDS = ("week", "partial", *SOURCE_FIELDS, "versions", "upgrades")
DAILY_FIELDS = ("day", "worker_requests", "worker_errors")
V1_FIELDS = ("schema", "snapshot_id", "taken_at", "source_set", "weeks", "daily", "downloads", "digest")
FIELDS = (*V1_FIELDS[:-1], "cloudflare", V1_FIELDS[-1])
FIELD_PATHS = (
    *FIELDS,
    "cloudflare.collected_at",
    "cloudflare.credential",
    "cloudflare.worker_version",
    "cloudflare.worker_published_at",
    "cloudflare.repository_head",
    "cloudflare.d1_rows",
    "cloudflare.last_purge",
    "cloudflare.requests_24h",
    "cloudflare.errors_24h",
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
# Provider coverage of a stored snapshot. The Cloudflare status read is not a
# stats provider, so it never lifts a snapshot's coverage rank.
_COVERAGE = """json_array_length(source_set) -
            CASE WHEN source_set LIKE '%"cloudflare"%' THEN 1 ELSE 0 END"""
_VERSION = re.compile(r"[0-9]{1,6}(?:\.[0-9]{1,6}){1,3}(?:(?:a|b|rc|\.post)[0-9]{1,6}|\.dev[0-9]{1,8})?")


def config() -> dict[str, Any]:
    """Deployment settings live only in the operator's fleet.toml."""
    path = fleet_client.brigade_home() / "fleet.toml"
    try:
        payload = toml_compat.loads(path.read_text(encoding="utf-8"))
        section = payload.get("fleet", {}).get("stats", {})
        return section if isinstance(section, dict) else {}
    except (OSError, ValueError, AttributeError):
        return {}


def ensure_schema(conn: sqlite3.Connection, *, prune: bool = True) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS admin_stats_snapshots (
        snapshot_id TEXT PRIMARY KEY, taken_at TEXT NOT NULL, source_set TEXT NOT NULL,
        schema TEXT NOT NULL, payload_json TEXT NOT NULL, digest TEXT NOT NULL)""")
    conn.execute("CREATE INDEX IF NOT EXISTS admin_stats_taken_at ON admin_stats_snapshots(taken_at)")
    new_history = conn.execute("SELECT 1 FROM sqlite_master WHERE name='admin_stats_weeks'").fetchone() is None
    conn.execute("""CREATE TABLE IF NOT EXISTS admin_stats_weeks (
        week TEXT PRIMARY KEY, partial INTEGER NOT NULL, taken_at TEXT NOT NULL,
        payload_json TEXT NOT NULL)""")
    if new_history:
        # Preserve history from the earlier branch schema before compaction.
        for row in conn.execute("SELECT payload_json FROM admin_stats_snapshots"):
            try:
                value = validate(json.loads(row[0]))
            except (ValueError, TypeError, KeyError, AttributeError):
                continue
            _store_weeks(conn, value)
        if prune:
            _prune(conn, datetime.now(timezone.utc))


def _keys(value: Any, fields: tuple[str, ...]) -> None:
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ValueError("invalid snapshot fields")


def _count(value: Any, *, sources: bool = False) -> None:
    if value is not None and (type(value) is not int or not 0 <= value <= 10**15 or (sources and value < 5)):
        raise ValueError("invalid or unsuppressed count")


def _cloudflare(value: Any) -> None:
    if value is None:
        return
    fields = (
        "collected_at",
        "credential",
        "worker_version",
        "worker_published_at",
        "repository_head",
        "d1_rows",
        "last_purge",
        "requests_24h",
        "errors_24h",
    )
    _keys(value, fields)
    _stamp(value["collected_at"])
    if value["credential"] not in ("available", "unavailable", "unknown"):
        raise ValueError("invalid Cloudflare credential state")
    for key in ("worker_version", "repository_head"):
        if value[key] is not None and (
            not isinstance(value[key], str) or not re.fullmatch(r"[A-Za-z0-9._+-]{1,128}", value[key])
        ):
            raise ValueError("invalid Cloudflare version")
    for key in ("worker_published_at", "last_purge"):
        if value[key] is not None:
            _stamp(value[key])
    for key in ("d1_rows", "requests_24h", "errors_24h"):
        if value[key] is not None and (type(value[key]) is not int or not 0 <= value[key] <= 10**15):
            raise ValueError("invalid Cloudflare count")


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
    if not isinstance(value, dict):
        raise ValueError("invalid snapshot fields")
    legacy = value.get("schema") == LEGACY_SCHEMA
    _keys(value, V1_FIELDS if legacy else FIELDS)
    if value["schema"] not in (SCHEMA, LEGACY_SCHEMA):
        raise ValueError("unsupported snapshot schema")
    taken = _stamp(value["taken_at"])
    sources = _rows(value["source_set"], 4)
    if any(type(s) is not str or s not in ("d1", "worker", "downloads", "cloudflare") for s in sources) or len(
        set(sources)
    ) != len(sources):
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
        suppressed = copy.deepcopy(row)
        _suppress_week(suppressed)
        if suppressed != row:
            raise ValueError("invalid complementary suppression")
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
    if not legacy:
        _cloudflare(value["cloudflare"])
        if value["cloudflare"] is not None and "cloudflare" not in sources:
            raise ValueError("Cloudflare data absent from source set")
    if (
        (value["weeks"] and "d1" not in sources)
        or (value["daily"] and "worker" not in sources)
        or (any(count is not None for count in value["downloads"].values()) and "downloads" not in sources)
    ):
        raise ValueError("provider data absent from source set")
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
    cloudflare: dict[str, Any] | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = copy.deepcopy(
        {
            "schema": SCHEMA,
            "taken_at": taken_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source_set": sorted(source_set),
            "weeks": weeks,
            "daily": daily,
            "downloads": downloads,
            "cloudflare": cloudflare,
        }
    )
    for row in value["weeks"]:
        _suppress_week(row)
    value["digest"] = value["snapshot_id"] = _digest(value)
    return validate(value)


def _suppress_week(row: dict[str, Any]) -> None:
    """Suppress small cells and complements only across containing groups."""
    cells = [(row, SOURCE_FIELDS), *((c, ("active_sources",)) for c in row["versions"] + row["upgrades"])]
    counts = [cell[field] for cell, fields in cells for field in fields]
    for count in counts:
        _count(count)
    totals = [row[field] for field in SOURCE_FIELDS]
    if any(a is not None and b is not None and a < b for index, a in enumerate(totals) for b in totals[index + 1 :]):
        raise ValueError("source subset exceeds parent total")
    total = row["active_sources"]
    if total is not None and any(count is not None and count > total for count in counts[len(SOURCE_FIELDS) :]):
        raise ValueError("source subset exceeds active total")
    versions = [cell["active_sources"] for cell in row["versions"]]
    for field in SOURCE_FIELDS:
        if row[field] is not None and row[field] < 5:
            row[field] = None
    for field in ("versions", "upgrades"):
        # Drop category names too. The page provides an unknown other row
        # without disclosing whether omitted cells existed or their sum.
        row[field] = [cell for cell in row[field] if cell["active_sources"] is not None and cell["active_sources"] >= 5]
    totals = [row[field] for field in SOURCE_FIELDS]
    pairs = [(a, b) for index, a in enumerate(totals) for b in totals[index + 1 :]]
    pairs.extend((row["active_sources"], cell["active_sources"]) for cell in row["versions"])
    versions_by_name = {cell["version"]: cell["active_sources"] for cell in row["versions"]}
    pairs.extend(
        (versions_by_name.get(cell[field]), cell["active_sources"])
        for cell in row["upgrades"]
        for field in ("from_version", "to_version")
    )
    hidden = any(a is not None and b is not None and 0 < a - b < 5 for a, b in pairs)
    # Excess version observations bound the group using multiple versions,
    # even when the upgrade histogram is empty or unavailable.
    visible_versions = [cell["active_sources"] for cell in row["versions"]]
    for observations in (versions, visible_versions):
        if total is not None and all(count is not None for count in observations):
            hidden = hidden or 0 < sum(observations) - total < 5
    if hidden:
        for field in SOURCE_FIELDS:
            row[field] = None
        # Category names also disclose the existence of a protected cohort.
        row["versions"] = []
        row["upgrades"] = []


def store(conn: sqlite3.Connection, value: Any, *, now: datetime | None = None) -> None:
    value = validate(value)
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
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
        _store_weeks(conn, value)
        _prune(conn, now)


def _store_weeks(conn: sqlite3.Connection, value: dict[str, Any]) -> None:
    for week in value["weeks"]:
        # A self-contained validated payload keeps page reads small and lets
        # the complete row survive daily snapshot compaction.
        payload = snapshot(
            taken_at=_stamp(value["taken_at"]),
            source_set=["d1"],
            weeks=[week],
            daily=[],
            downloads={"pypi": None, "npm": None},
        )
        conn.execute(
            """INSERT INTO admin_stats_weeks VALUES (?, ?, ?, ?)
            ON CONFLICT(week) DO UPDATE SET partial=excluded.partial,
                taken_at=excluded.taken_at, payload_json=excluded.payload_json
            WHERE excluded.partial < admin_stats_weeks.partial OR
                (excluded.partial = admin_stats_weeks.partial AND
                 (excluded.taken_at, excluded.payload_json) >
                 (admin_stats_weeks.taken_at, admin_stats_weeks.payload_json))""",
            (week["week"], int(week["partial"]), value["taken_at"], json.dumps(payload, sort_keys=True)),
        )


def _prune(conn: sqlite3.Connection, now: datetime) -> None:
    # Each UTC day keeps two rows: the best regular snapshot (most successful
    # providers, ties to the newest) and the newest snapshot of any kind. The
    # newest one carries the operator's current Cloudflare opt-in state and
    # observation, so source coverage must never be able to delete it.
    conn.execute(f"""DELETE FROM admin_stats_snapshots WHERE snapshot_id NOT IN (
        SELECT snapshot_id FROM (
            SELECT snapshot_id, ROW_NUMBER() OVER (
                PARTITION BY substr(taken_at, 1, 10)
                ORDER BY {_COVERAGE} DESC, taken_at DESC, snapshot_id DESC) AS position
            FROM admin_stats_snapshots
            UNION ALL
            SELECT snapshot_id, ROW_NUMBER() OVER (
                PARTITION BY substr(taken_at, 1, 10)
                ORDER BY taken_at DESC, snapshot_id DESC) AS position
            FROM admin_stats_snapshots
        ) WHERE position = 1)""")
    cutoff = (now - timedelta(days=RETENTION_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute("DELETE FROM admin_stats_snapshots WHERE taken_at < ?", (cutoff,))
    conn.execute("DELETE FROM admin_stats_weeks WHERE taken_at < ?", (cutoff,))
    conn.execute("""DELETE FROM admin_stats_weeks WHERE week NOT IN (
        SELECT week FROM admin_stats_weeks ORDER BY week DESC LIMIT 60)""")


def maintain(db_path: Path, *, storage_policy: StoragePolicy | None = None) -> None:
    """Delete expired aggregates independently of collection and page reads."""
    from . import fleet_hub, fleet_hub_messages, fleet_hub_sealed

    conn = fleet_hub.open_db(db_path)
    try:
        with conn:
            _prune(conn, datetime.now(timezone.utc))
            fleet_hub_messages._prune_expired(conn, storage_policy=storage_policy)
            # Retention only: deadline/status transitions remain in the question adapter.
            conn.execute(
                "DELETE FROM fleet_questions WHERE created_at<?",
                (fleet_hub_messages._expiry_cutoff(storage_policy),),
            )
        while True:
            # Release the writer lock between bounded batches, including at
            # startup. Ciphertext never accumulates in one unbounded list.
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                purged = fleet_hub_sealed.purge_content(conn)
            if purged < fleet_hub_sealed.PURGE_BATCH_SIZE:
                break
    finally:
        conn.close()


def start_retention_sweeper(
    db_path: Path, *, stop: threading.Event, storage_policy: StoragePolicy | None = None
) -> threading.Thread:
    """Run hourly maintenance with a separate connection for each pass."""
    from .fleet_hub import FleetHubError

    def loop() -> None:
        while not stop.wait(RETENTION_INTERVAL_SECONDS):
            try:
                maintain(db_path, storage_policy=storage_policy)
            except (sqlite3.Error, FleetHubError):
                # Retry a locked or temporarily unavailable database next tick.
                continue

    thread = threading.Thread(target=loop, name="stats-retention-sweeper", daemon=True)
    thread.start()
    return thread


def _first_valid(conn: sqlite3.Connection, query: str, cutoff: str) -> dict[str, Any] | None:
    for row in conn.execute(query, (cutoff,)):
        try:
            return validate(json.loads(row[0]))
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
    return None


def load_view(conn: sqlite3.Connection, *, now: datetime | None = None) -> dict[str, Any]:
    """Read the page model.

    `latest` is the best regular snapshot of the newest retained day. The
    Cloudflare panel follows the newest valid snapshot alone: if it carries an
    observation the panel shows it, and if it has none (the operator opted out,
    or it is a retained v1 snapshot that predates the field) the panel is hidden.
    """
    cutoff = ((now or datetime.now(timezone.utc)) - timedelta(days=RETENTION_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    newest = _first_valid(
        conn,
        "SELECT payload_json FROM admin_stats_snapshots WHERE taken_at >= ? ORDER BY taken_at DESC, snapshot_id DESC",
        cutoff,
    )
    latest = _first_valid(
        conn,
        f"""SELECT payload_json FROM admin_stats_snapshots WHERE taken_at >= ?
        ORDER BY substr(taken_at, 1, 10) DESC, {_COVERAGE} DESC, taken_at DESC, snapshot_id DESC""",
        cutoff,
    )
    weeks = []
    for row in conn.execute(
        "SELECT payload_json FROM admin_stats_weeks WHERE taken_at >= ? ORDER BY week DESC LIMIT 60",
        (cutoff,),
    ):
        try:
            value = validate(json.loads(row[0]))
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
        weeks.extend(value["weeks"])
    return {
        "latest": latest,
        "cloudflare": newest.get("cloudflare") if newest else None,
        "weeks": weeks,
    }
