"""Read-only D1 and Analytics collection with no deployment identifiers in snapshots."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
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
ANALYTICS_QUERY = """query Stats($account: string!, $worker: string!, $start: Time!, $end: Time!) {
 viewer { accounts(filter: {accountTag: $account}) {
 workersInvocationsAdaptive(limit: 10000, filter: {scriptName: $worker, datetime_geq: $start, datetime_lt: $end}, orderBy: [date_ASC]) {
 dimensions { date } sum { requests errors }
 } } } }"""
VIEWER_QUERY = "query CredentialCheck { viewer { accounts { accountTag } } }"
STATUS_ANALYTICS_QUERY = """query Status($account: string!, $worker: string!, $start: Time!, $end: Time!) {
 viewer { accounts(filter: {accountTag: $account}) {
 workersInvocationsAdaptive(limit: 10000, filter: {scriptName: $worker, datetime_geq: $start, datetime_lt: $end}, orderBy: [date_ASC]) {
 dimensions { date } sum { requests errors }
 } } } }"""
STATUS_QUERY = "SELECT COUNT(*) AS row_count FROM version_observations WHERE internal = 0"
_MAX_RESPONSE = 4 * 1024 * 1024


def wrangler_argv(database: str, query: str) -> list[str]:
    if not isinstance(database, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", database):
        raise ValueError("invalid stats database setting")
    if query not in QUERIES:
        raise ValueError("unknown stats query")
    return ["wrangler", "d1", "execute", database, "--remote", "--json", "--command", QUERIES[query]]


def status_wrangler_argv(worker: str | None, database: str | None, action: str) -> list[str]:
    """Build only the named read operations used by the Deck status panel."""
    if action in ("deployments-status", "deployments-list", "versions-list"):
        if not isinstance(worker, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", worker):
            raise ValueError("invalid stats Worker setting")
        return {
            "deployments-status": ["wrangler", "deployments", "status", "--name", worker, "--json"],
            "deployments-list": ["wrangler", "deployments", "list", "--name", worker, "--json"],
            "versions-list": ["wrangler", "versions", "list", "--name", worker, "--json"],
        }[action]
    if action in ("d1-info", "d1-counts"):
        if not isinstance(database, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", database):
            raise ValueError("invalid stats database setting")
        if action == "d1-info":
            return ["wrangler", "d1", "info", database, "--json"]
        return ["wrangler", "d1", "execute", database, "--remote", "--json", "--command", STATUS_QUERY]
    raise ValueError("unknown Cloudflare status read")


def _status_read(argv: list[str]) -> bool:
    if any(not isinstance(part, str) for part in argv):
        return False
    if len(argv) == 6 and argv[:3] in (["wrangler", "deployments", "status"], ["wrangler", "deployments", "list"]):
        if argv[3] != "--name" or argv[5] != "--json":
            return False
        action = "deployments-status" if argv[2] == "status" else "deployments-list"
        return argv == status_wrangler_argv(argv[4], None, action)
    if len(argv) == 6 and argv[:3] == ["wrangler", "versions", "list"]:
        return (
            argv[3] == "--name" and argv[5] == "--json" and argv == status_wrangler_argv(argv[4], None, "versions-list")
        )
    if len(argv) == 5 and argv[:3] == ["wrangler", "d1", "info"]:
        return argv[4] == "--json" and argv == status_wrangler_argv(None, argv[3], "d1-info")
    return len(argv) == 8 and argv[-1] == STATUS_QUERY and argv == status_wrangler_argv(None, argv[3], "d1-counts")


def run_wrangler(argv: list[str], *, account: str | None = None, token_envs: tuple[str, ...] = ()) -> Any:
    """Only the exact generated read vector is executable. Never print tool output."""
    if not isinstance(argv, list):
        raise ValueError("command outside stats read allowlist")
    status_read = _status_read(argv)
    query = next((key for key, sql in QUERIES.items() if len(argv) == 8 and sql == argv[-1]), None)
    existing_read = (
        query is not None
        and argv == wrangler_argv(argv[3], query)
        and argv[-1].lstrip().startswith(("SELECT ", "WITH "))
    )
    if not status_read and not existing_read:
        raise ValueError("command outside stats read allowlist")
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_API_KEY", "CF_API_TOKEN", "CF_API_KEY", *token_envs)
        and not key.startswith("BRIGADE_")
    }
    if account is not None:
        if not isinstance(account, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", account):
            raise ValueError("invalid stats account setting")
        env["CLOUDFLARE_ACCOUNT_ID"] = account
    env["WRANGLER_SEND_METRICS"] = "false"
    try:
        return json.loads(_bounded_output(argv, env))
    except (OSError, subprocess.SubprocessError, ValueError):
        raise ValueError("Cloudflare read failed" if status_read else "D1 read failed") from None


def _bounded_output(argv: list[str], env: dict[str, str]) -> bytes:
    # Discard stderr at the OS boundary. It may contain credentials, and is not
    # needed to decode a successful aggregate response.
    process = subprocess.Popen(
        argv, shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env
    )
    pipe = process.stdout
    assert pipe is not None
    output = bytearray()
    failed: list[bool] = []

    def read() -> None:
        try:
            with pipe:
                while len(output) <= _MAX_RESPONSE:
                    chunk = pipe.read(min(65536, _MAX_RESPONSE + 1 - len(output)))
                    if not chunk:
                        return
                    output.extend(chunk)
                process.kill()
        except OSError:
            failed.append(True)

    reader = threading.Thread(target=read, daemon=True)
    deadline = time.monotonic() + 60
    reader.start()
    try:
        reader.join(timeout=max(0, deadline - time.monotonic()))
        if reader.is_alive() or failed or len(output) > _MAX_RESPONSE:
            raise ValueError("D1 output unavailable or oversized")
        if process.wait(timeout=max(0, deadline - time.monotonic())) != 0:
            raise ValueError("D1 command failed")
        return bytes(output)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()


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


def _d1(settings: dict[str, Any], query: str) -> list[dict[str, Any]]:
    if query not in QUERIES:
        raise ValueError("unknown stats query")
    if settings.get("database_id"):
        account, database = settings.get("account"), settings["database_id"]
        for value in (account, database):
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
                raise ValueError("invalid stats D1 setting")
        key = settings.get("d1_token_env")
        token = os.environ.get(key, "") if isinstance(key, str) else ""
        if not token:
            raise ValueError("D1 credential unavailable")
        payload = fetch_json(
            f"https://api.cloudflare.com/client/v4/accounts/{account}/d1/database/{database}/query",
            token=token,
            body={"sql": QUERIES[query]},
        )
        if payload.get("success") is not True or payload.get("errors"):
            raise ValueError("D1 read failed")
        return _results(payload["result"])
    token_envs = tuple(
        settings[key] for key in ("analytics_token_env", "d1_token_env") if isinstance(settings.get(key), str)
    )
    return _results(
        run_wrangler(wrangler_argv(settings["database"], query), account=settings.get("account"), token_envs=token_envs)
    )


def _analytics(settings: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
    key = settings.get("analytics_token_env")
    token = os.environ.get(key, "") if isinstance(key, str) else ""
    if not token:
        raise ValueError("analytics credential unavailable")
    end = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=30)
    payload = fetch_json(
        "https://api.cloudflare.com/client/v4/graphql",
        token=token,
        body={
            "query": ANALYTICS_QUERY,
            "variables": {
                "account": settings["account"],
                "worker": settings["worker"],
                "start": start.isoformat(),
                "end": end.isoformat(),
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


def _status_time(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def cloudflare_status(settings: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any] | None:
    """Return only validated Cloudflare status fields; never retain CLI or API payloads."""
    if settings.get("status_panel") is not True:
        return None
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    worker, database = settings.get("worker"), settings.get("database")
    if not worker and not database:
        return None
    result: dict[str, Any] = {
        "collected_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "credential": "unknown",
        "worker_version": None,
        "worker_published_at": None,
        "repository_head": None,
        "d1_rows": None,
        "last_purge": None,
        "requests_24h": None,
        "errors_24h": None,
    }
    repository_head = settings.get("repository_head")
    if isinstance(repository_head, str) and re.fullmatch(r"[A-Za-z0-9._+-]{1,128}", repository_head):
        result["repository_head"] = repository_head
    worker_reads: dict[str, tuple[bool, Any]] = {}
    d1_reads: dict[str, tuple[bool, Any]] = {}
    if isinstance(worker, str) or isinstance(database, str):
        token_envs = tuple(
            settings[key] for key in ("analytics_token_env", "d1_token_env") if isinstance(settings.get(key), str)
        )

        def read(action: str) -> tuple[bool, Any]:
            try:
                worker_name = worker if action in ("deployments-status", "deployments-list", "versions-list") else None
                database_name = database if action in ("d1-info", "d1-counts") else None
                value = run_wrangler(
                    status_wrangler_argv(worker_name, database_name, action),
                    account=settings.get("account"),
                    token_envs=token_envs,
                )
            except (ValueError, KeyError, TypeError, AttributeError):
                return False, None
            return True, value

        if isinstance(worker, str):
            worker_reads = {
                action: read(action) for action in ("deployments-status", "deployments-list", "versions-list")
            }
        if isinstance(database, str):
            d1_reads = {action: read(action) for action in ("d1-info", "d1-counts")}
    worker_ok = any(succeeded for succeeded, _ in worker_reads.values())
    d1_ok = any(succeeded for succeeded, _ in d1_reads.values())
    wrangler_ok = worker_ok or d1_ok
    current = worker_reads.get("deployments-status", (False, None))[1]
    deployments = worker_reads.get("deployments-list", (False, None))[1]
    versions = worker_reads.get("versions-list", (False, None))[1]

    if isinstance(worker, str):
        deployment = None
        if isinstance(current, dict):
            deployment = current.get("latest")
            if deployment is None and isinstance(current.get("versions"), list):
                deployment = current
        if deployment is None:
            entries = deployments if isinstance(deployments, list) else []
            if isinstance(deployments, dict) and isinstance(deployments.get("deployments"), list):
                entries = deployments["deployments"]
            deployment = entries[0] if entries else None

        version_id = None
        if isinstance(deployment, dict):
            deployed = deployment.get("versions")
            if isinstance(deployed, list) and deployed and isinstance(deployed[0], dict):
                candidate = deployed[0].get("version_id") or deployed[0].get("id")
                if isinstance(candidate, str) and re.fullmatch(r"[A-Za-z0-9._+-]{1,128}", candidate):
                    version_id = candidate
            result["worker_version"] = version_id

        version_rows = versions if isinstance(versions, list) else []
        if isinstance(versions, dict) and isinstance(versions.get("versions"), list):
            version_rows = versions["versions"]
        for version in version_rows:
            if isinstance(version, dict) and version_id in (version.get("id"), version.get("version_id")):
                metadata = version.get("metadata")
                created = metadata.get("created_on") if isinstance(metadata, dict) else None
                result["worker_published_at"] = _status_time(
                    created or version.get("created_on") or version.get("created_at")
                )
                break

    if d1_reads.get("d1-counts", (False, None))[0]:
        try:
            rows = _results(d1_reads["d1-counts"][1])
            if len(rows) == 1 and type(rows[0].get("row_count")) is int and rows[0]["row_count"] >= 0:
                result["d1_rows"] = rows[0]["row_count"]
        except (ValueError, KeyError, TypeError, AttributeError):
            pass
    token_key = settings.get("analytics_token_env")
    token = os.environ.get(token_key, "") if isinstance(token_key, str) else ""
    graphql_ok = False
    if token and isinstance(worker, str) and isinstance(settings.get("account"), str):
        try:
            viewer = fetch_json(
                "https://api.cloudflare.com/client/v4/graphql", body={"query": VIEWER_QUERY}, token=token
            )
            accounts = viewer["data"]["viewer"]["accounts"]
            graphql_ok = not viewer.get("errors") and isinstance(accounts, list) and bool(accounts)
            if graphql_ok:
                end = now
                payload = fetch_json(
                    "https://api.cloudflare.com/client/v4/graphql",
                    body={
                        "query": STATUS_ANALYTICS_QUERY,
                        "variables": {
                            "account": settings["account"],
                            "worker": worker,
                            "start": (end - timedelta(hours=24)).isoformat(),
                            "end": end.isoformat(),
                        },
                    },
                    token=token,
                )
                account_rows = payload["data"]["viewer"]["accounts"]
                if not payload.get("errors") and len(account_rows) == 1:
                    observations = account_rows[0]["workersInvocationsAdaptive"]
                    if len(observations) < 10000:
                        requests, errors = [], []
                        for row in observations:
                            requests.append(row["sum"]["requests"])
                            errors.append(row["sum"]["errors"])
                        if all(type(value) is int and value >= 0 for value in requests + errors):
                            result["requests_24h"] = sum(requests)
                            result["errors_24h"] = sum(errors)
        except (ValueError, KeyError, TypeError, AttributeError):
            pass
    if not token:
        result["credential"] = "unavailable"
    elif isinstance(worker, str) and worker_ok and graphql_ok:
        result["credential"] = "available"
    elif not isinstance(worker, str) and isinstance(database, str) and d1_ok:
        result["credential"] = "available"
    elif graphql_ok and not wrangler_ok:
        result["credential"] = "unavailable"
    # Available read-only status sources do not report a D1 purge event timestamp.
    # Keep last_purge unknown instead of presenting the newest data date as a purge.
    stats._cloudflare(result)
    return result


def collect(settings: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """Unavailable providers stay unknown. Successful empty queries never imply zero."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    sources = []
    weeks: list[dict[str, Any]] = []
    daily: list[dict[str, Any]] = []
    downloads: dict[str, Any] = {"pypi": None, "npm": None}
    if settings.get("database") or settings.get("database_id"):
        try:
            weekly = _d1(settings, "weekly")
            data: dict[str, list[dict[str, Any]] | None] = {}
            for key in ("versions", "upgrades"):
                try:
                    data[key] = _d1(settings, key)
                except (ValueError, KeyError, TypeError, AttributeError):
                    data[key] = None
            for row in weekly:
                week = row["week"]
                start = stats._week(week)
                versions = _histogram(data["versions"], week, upgrades=False)
                upgrades = _histogram(data["upgrades"], week, upgrades=True)
                if versions is None or upgrades is None:
                    versions = upgrades = []
                weeks.append(
                    {
                        "week": week,
                        "partial": start + timedelta(days=7) > now.date() or start <= now.date() - timedelta(days=90),
                        **{key: row[key] for key in stats.SOURCE_FIELDS},
                        "versions": versions,
                        "upgrades": upgrades,
                    }
                )
            # Validate within the source boundary so malformed D1 data cannot
            # discard successful Worker and download reads.
            weeks = stats.snapshot(taken_at=now, source_set=["d1"], weeks=weeks, daily=[], downloads=downloads)["weeks"]
            sources.append("d1")
        except (ValueError, KeyError, TypeError, AttributeError):
            weeks = []
    if settings.get("account") and settings.get("worker"):
        try:
            daily = stats.snapshot(
                taken_at=now,
                source_set=["worker"],
                weeks=[],
                daily=_analytics(settings, now),
                downloads={"pypi": None, "npm": None},
            )["daily"]
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
    cloudflare = cloudflare_status(settings, now=now)
    if cloudflare is not None:
        sources.append("cloudflare")
    return stats.snapshot(
        taken_at=now, source_set=sources, weeks=weeks, daily=daily, downloads=downloads, cloudflare=cloudflare
    )


def _histogram(rows: list[dict[str, Any]] | None, week: str, *, upgrades: bool) -> list[dict[str, Any]] | None:
    # A global query limit can omit cells even in a small per-week histogram.
    if rows is None or len(rows) >= (12000 if upgrades else 6000):
        return None
    fields = ("from_version", "to_version") if upgrades else ("version",)
    cells = []
    for row in rows:
        try:
            if row["week"] != week:
                continue
            for field in fields:
                stats._version(row[field])
            stats._count(row["active_sources"])
            if upgrades and _release(row["to_version"]) <= _release(row["from_version"]):
                continue
            cells.append({key: row[key] for key in (*fields, "active_sources")})
        except (ValueError, KeyError, TypeError):
            # Keep valid peers. Weekly suppression still checks their counts.
            continue
    # An empty array renders as unknown. Never show a truncated distribution.
    return cells if len(cells) <= (200 if upgrades else 100) else None


def _release(version: str) -> tuple[int, ...]:
    stats._version(version)
    release = tuple(int(part) for part in re.match(r"[0-9.]+", version).group().rstrip(".").split("."))  # type: ignore[union-attr]
    return release + (0,) * (4 - len(release))


def publish(value: dict[str, Any]) -> None:
    stats.validate(value)
    settings = fleet_client.load_fleet_settings()
    hub, token = settings["hub_url"], settings["node_token"]
    if not hub or not token:
        raise ValueError("stats publishing requires a hub and node token")
    fleet_client._require_encrypted_or_loopback_hub(hub)
    request = urllib.request.Request(
        hub.rstrip("/") + "/stats",
        data=json.dumps(value, separators=(",", ":")).encode(),
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
        if not value["source_set"]:
            raise ValueError("no stats sources available")
        if args.no_publish:
            print(json.dumps(value, sort_keys=True))
        else:
            publish(value)
            print("Stats snapshot published.")
        return 0
    except (ValueError, fleet_client.FleetClientError):
        print("Stats collection failed. Check the operator stats config and credentials.", file=sys.stderr)
        return 1
