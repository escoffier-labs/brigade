"""Read-only D1 and Analytics collection with no deployment identifiers in snapshots."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

from . import fleet_admin_stats as stats, fleet_client

# These statements return aggregates only. Dates are database expressions, never CLI SQL.
QUERIES = {
    "weekly": """WITH days AS (
 SELECT week, hash, COUNT(DISTINCT day) AS days FROM version_observations
 WHERE day >= date('now', '-90 days') AND internal = 0 GROUP BY week, hash)
 SELECT week, COUNT(*) AS active_sources,
 SUM(days >= 2) AS retained_2d_sources, SUM(days >= 3) AS retained_3d_sources
 FROM days GROUP BY week ORDER BY week DESC LIMIT 60""",
    "versions": """SELECT week, version, COUNT(DISTINCT hash) AS active_sources
 FROM version_observations WHERE day >= date('now', '-90 days') AND internal = 0
 GROUP BY week, version ORDER BY week DESC, version LIMIT 6000""",
    "upgrades": """SELECT a.week, a.version AS from_version, b.version AS to_version,
 COUNT(DISTINCT a.hash) AS active_sources FROM version_observations a
 JOIN version_observations b ON a.week = b.week AND a.hash = b.hash
 WHERE a.day >= date('now', '-90 days') AND a.internal = 0 AND b.internal = 0
 AND a.version != b.version AND a.first_seen < b.first_seen
 GROUP BY a.week, a.version, b.version ORDER BY a.week DESC, a.version, b.version LIMIT 12000""",
}
ANALYTICS_QUERY = """query Stats($account: String!, $worker: String!, $start: Time!, $end: Time!) {
 viewer { accounts(filter: {accountTag: $account}) {
 workersInvocationsAdaptive(limit: 10000, filter: {scriptName: $worker, datetime_geq: $start, datetime_lt: $end}, orderBy: [date_ASC]) {
 dimensions { date } sum { requests errors }
 } } } }"""
_MAX_RESPONSE = 4 * 1024 * 1024


def wrangler_argv(database: str, query: str) -> list[str]:
    if not isinstance(database, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", database):
        raise ValueError("invalid stats database setting")
    if query not in QUERIES:
        raise ValueError("unknown stats query")
    return ["wrangler", "d1", "execute", database, "--remote", "--json", "--command", QUERIES[query]]


def run_wrangler(argv: list[str], *, account: str | None = None) -> Any:
    """Only the exact generated read vector is executable. Never print tool output."""
    if not isinstance(argv, list) or len(argv) != 8 or argv[-1] not in QUERIES.values():
        raise ValueError("command outside stats read allowlist")
    query = next(key for key, sql in QUERIES.items() if sql == argv[-1])
    if argv != wrangler_argv(argv[3], query) or not argv[-1].lstrip().startswith(("SELECT ", "WITH ")):
        raise ValueError("command outside stats read allowlist")
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_API_KEY", "CF_API_TOKEN", "CF_API_KEY")
    }
    if account is not None:
        if not isinstance(account, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", account):
            raise ValueError("invalid stats account setting")
        env["CLOUDFLARE_ACCOUNT_ID"] = account
    env["WRANGLER_SEND_METRICS"] = "false"
    try:
        result = subprocess.run(argv, shell=False, capture_output=True, text=True, timeout=60, env=env, check=True)
        if len(result.stdout) > _MAX_RESPONSE:
            raise ValueError("oversized D1 response")
        return json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        raise ValueError("D1 read failed") from None


def fetch_json(url: str, *, body: dict[str, Any] | None = None, token: str | None = None) -> Any:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("stats sources require HTTPS")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(url, data=json.dumps(body).encode() if body else None, headers=headers)

    # Refuse redirects so the analytics bearer cannot be forwarded to another origin.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
            return None

    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=30) as response:
            raw = response.read(_MAX_RESPONSE + 1)
        if len(raw) > _MAX_RESPONSE:
            raise ValueError("oversized stats response")
        return json.loads(raw)
    except Exception:
        raise ValueError("stats source read failed") from None


def _results(payload: Any) -> list[dict[str, Any]]:
    if (
        not isinstance(payload, list)
        or len(payload) != 1
        or payload[0].get("success") is not True
        or not isinstance(payload[0].get("results"), list)
    ):
        raise ValueError("invalid D1 aggregate response")
    return payload[0]["results"]


def _analytics(settings: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
    key = settings.get("analytics_token_env")
    token = os.environ.get(key, "") if isinstance(key, str) else ""
    if not token:
        raise ValueError("analytics credential unavailable")
    start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=30)
    payload = fetch_json(
        "https://api.cloudflare.com/client/v4/graphql",
        token=token,
        body={
            "query": ANALYTICS_QUERY,
            "variables": {
                "account": settings["account"],
                "worker": settings["worker"],
                "start": start.isoformat(),
                "end": now.isoformat(),
            },
        },
    )
    if payload.get("errors"):
        raise ValueError("analytics unavailable")
    accounts = payload["data"]["viewer"]["accounts"]
    if len(accounts) != 1:
        raise ValueError("analytics account unavailable")
    rows = accounts[0]["workersInvocationsAdaptive"]
    if len(rows) >= 10000:
        raise ValueError("analytics response truncated")
    daily: dict[str, Any] = {}
    for row in rows:
        day = row["dimensions"]["date"]
        counts = row["sum"]
        stats._count(counts["requests"])
        stats._count(counts["errors"])
        if counts["requests"] is None or counts["errors"] is None:
            raise ValueError("analytics count unavailable")
        item = daily.setdefault(day, {"day": day, "worker_requests": 0, "worker_errors": 0})
        item["worker_requests"] += counts["requests"]
        item["worker_errors"] += counts["errors"]
    return [daily[key] for key in sorted(daily)]


def collect(settings: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """Unavailable providers stay unknown. Successful empty queries never imply zero."""
    now = now or datetime.now(timezone.utc)
    sources = []
    weeks: list[dict[str, Any]] = []
    daily: list[dict[str, Any]] = []
    downloads: dict[str, Any] = {"pypi": None, "npm": None}
    if settings.get("database"):
        try:
            data = {
                key: _results(run_wrangler(wrangler_argv(settings["database"], key), account=settings.get("account")))
                for key in QUERIES
            }
            for row in data["weekly"]:
                week = row["week"]
                start = stats._week(week)
                versions = [
                    {"version": c["version"], "active_sources": c["active_sources"]}
                    for c in data["versions"]
                    if c["week"] == week
                ]
                upgrades = [
                    {key: c[key] for key in ("from_version", "to_version", "active_sources")}
                    for c in data["upgrades"]
                    if c["week"] == week
                ]
                # Ignore downgrades when both versions have a numeric release tuple.
                upgrades = [c for c in upgrades if _release(c["to_version"]) > _release(c["from_version"])]
                weeks.append(
                    {
                        "week": week,
                        "partial": start + timedelta(days=7) > now.date() or start < now.date() - timedelta(days=90),
                        **{key: row[key] for key in stats.SOURCE_FIELDS},
                        "versions": versions,
                        "upgrades": upgrades,
                    }
                )
            sources.append("d1")
        except (ValueError, KeyError, TypeError, AttributeError):
            weeks = []
    if settings.get("account") and settings.get("worker"):
        try:
            daily = _analytics(settings, now)
            sources.append("worker")
        except (ValueError, KeyError, TypeError, AttributeError):
            daily = []
    if settings.get("downloads_url"):
        try:
            badge = fetch_json(settings["downloads_url"])
            if badge["schemaVersion"] != 1 or badge["period"]["days"] != 30 or badge["period"]["timezone"] != "UTC":
                raise ValueError("invalid download period")
            downloads = {name: badge["sources"][name]["downloads"] for name in ("pypi", "npm")}
            for count in downloads.values():
                stats._count(count)
            sources.append("downloads")
        except (ValueError, KeyError, TypeError, AttributeError):
            downloads = {"pypi": None, "npm": None}
    return stats.snapshot(taken_at=now, source_set=sources, weeks=weeks, daily=daily, downloads=downloads)


def _release(version: str) -> tuple[int, ...]:
    stats._version(version)
    return tuple(int(part) for part in re.match(r"[0-9.]+", version).group().rstrip(".").split("."))  # type: ignore[union-attr]


def publish(value: dict[str, Any]) -> None:
    stats.validate(value)
    settings = fleet_client.load_fleet_settings()
    hub, token = settings["hub_url"], settings["node_token"]
    if not hub or not token:
        raise ValueError("stats publishing requires a hub and node token")
    fleet_client._require_encrypted_or_loopback_hub(hub)
    request = urllib.request.Request(
        hub.rstrip("/") + "/stats",
        data=json.dumps(value).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + token},
    )
    try:
        with fleet_client._hub_open(request, timeout=30) as response:
            if response.status != 200:
                raise ValueError("stats publish refused")
    except Exception:
        raise ValueError("stats publish failed") from None


def register(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("stats", help="Aggregate adoption statistics.")
    commands = parser.add_subparsers(dest="stats_command", required=True)
    collect_parser = commands.add_parser(
        "collect", help="Read configured aggregate sources and post a snapshot to the Hub."
    )
    collect_parser.add_argument(
        "--no-publish", action="store_true", help="Print the aggregate snapshot without posting it."
    )
    collect_parser.set_defaults(func=dispatch)


def dispatch(args: argparse.Namespace) -> int:
    try:
        value = collect(stats.config())
        if args.no_publish:
            print(json.dumps(value, sort_keys=True))
        else:
            publish(value)
            print("Stats snapshot published.")
        return 0
    except (ValueError, fleet_client.FleetClientError):
        print("Stats collection failed. Check the operator stats config and credentials.", file=sys.stderr)
        return 1
