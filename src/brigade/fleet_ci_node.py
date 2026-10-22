"""Node-local relay cursor and hourly, on-demand reconciliation. No daemon."""

from __future__ import annotations

import json
import sqlite3
import urllib.request
import urllib.error
import time
from collections import OrderedDict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urlencode, urlsplit

from . import fleet_ci_board as board
from .fleet_ci_reconcile import reconcile_repo
from .fleet_ci_relay import MAX_PAGE, page_batch
from .github_reader import GitHubReader, SubprocessRunner
from .github_read_ledger import BudgetLedger

MAX_BYTES = 4 * 1024 * 1024


class NodeState:
    """Private node-local cache. No credentials are written here."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS ci_cursor (relay TEXT PRIMARY KEY, cursor TEXT NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS ci_cache (repo TEXT PRIMARY KEY, snapshot TEXT NOT NULL)")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS ci_etags (repo TEXT NOT NULL, endpoint TEXT NOT NULL, etag TEXT NOT NULL, body BLOB NOT NULL, touched REAL NOT NULL, PRIMARY KEY(repo, endpoint))"
            )
        path.chmod(0o600)

    def etag_cache(self, repo: str) -> OrderedDict[str, tuple[str, bytes]]:
        cache: OrderedDict[str, tuple[str, bytes]] = OrderedDict()
        with sqlite3.connect(self.path) as conn:
            rows = conn.execute(
                "SELECT endpoint, etag, body FROM ci_etags WHERE repo=? ORDER BY touched LIMIT 128", (repo,)
            )
            size = 0
            for endpoint, etag, body in rows:
                if (
                    not isinstance(endpoint, str)
                    or not (
                        endpoint == f"/repos/{repo}"
                        or endpoint.startswith(f"/repos/{repo}/")
                        or endpoint.startswith(f"/repos/{repo}?")
                    )
                    or not isinstance(etag, str)
                    or len(etag) > 8192
                    or any(c in etag for c in "\r\n\x00")
                    or not isinstance(body, bytes)
                    or len(body) > 16 * 1024 * 1024
                ):
                    continue
                size += len(body)
                if size > 16 * 1024 * 1024:
                    break
                cache[endpoint] = (etag, body)
        return cache

    def save_etag_cache(self, repo: str, cache: OrderedDict[str, tuple[str, bytes]]) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.execute("DELETE FROM ci_etags WHERE repo=?", (repo,))
            for index, (endpoint, (etag, body)) in enumerate(list(cache.items())[-128:]):
                if len(body) <= 16 * 1024 * 1024:
                    conn.execute(
                        "INSERT INTO ci_etags VALUES (?, ?, ?, ?, ?)",
                        (repo, endpoint, etag, body, time.time() + index / 1000000),
                    )
            while True:
                size = conn.execute("SELECT coalesce(sum(length(body)),0) FROM ci_etags").fetchone()[0]
                if size <= 16 * 1024 * 1024:
                    break
                conn.execute("DELETE FROM ci_etags WHERE rowid=(SELECT rowid FROM ci_etags ORDER BY touched LIMIT 1)")

    def cursor(self, relay: str) -> str:
        with sqlite3.connect(self.path) as conn:
            row = conn.execute("SELECT cursor FROM ci_cursor WHERE relay=?", (board.refresh_key(relay),)).fetchone()
        return row[0] if row else "0"

    def set_cursor(self, relay: str, cursor: str) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                "INSERT INTO ci_cursor VALUES (?, ?) ON CONFLICT(relay) DO UPDATE SET cursor=excluded.cursor "
                "WHERE CAST(excluded.cursor AS INTEGER)>CAST(ci_cursor.cursor AS INTEGER)",
                (board.refresh_key(relay), cursor),
            )

    def cached(self, repo: str) -> dict[str, Any]:
        with sqlite3.connect(self.path) as conn:
            row = conn.execute("SELECT snapshot FROM ci_cache WHERE repo=?", (repo,)).fetchone()
        return json.loads(row[0]) if row else {}

    def save(self, snapshot: dict[str, Any]) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                "INSERT INTO ci_cache VALUES (?, ?) ON CONFLICT(repo) DO UPDATE SET snapshot=excluded.snapshot",
                (snapshot["repo"], json.dumps(snapshot)),
            )


def fetch_json(url: str, *, token: str, body: dict[str, Any] | None = None) -> Any:
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.fragment
        or not token
        or any(c in token for c in "\r\n")
    ):
        raise ValueError("invalid CI transport configuration")

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
            return None

    headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
    data = json.dumps(body).encode() if body is not None else None
    if data is not None and len(data) > MAX_BYTES:
        raise ValueError("oversized CI request")
    request = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect()).open(
            request, timeout=30
        ) as response:
            raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("oversized CI response")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError("invalid CI response")
        return result
    except urllib.error.HTTPError as exc:
        raise ValueError(f"CI transport HTTP {exc.code}") from None
    except Exception:
        raise ValueError("CI transport unavailable") from None


def pull(
    state: NodeState, *, relay_url: str, relay_token: str, hub_url: str, node_token: str, limit: int = 100
) -> dict[str, Any]:
    if type(limit) is not int or not 1 <= limit <= MAX_PAGE:
        raise ValueError("relay limit must be in 1..100")
    relay = urlsplit(relay_url)
    if (
        relay.scheme != "https"
        or relay.path not in {"", "/"}
        or relay.query
        or relay.fragment
        or relay.username
        or relay.password
    ):
        raise ValueError("relay requires an HTTPS origin")
    after = state.cursor(relay_url)
    value = fetch_json(
        relay_url.rstrip("/") + "/v1/events?" + urlencode({"after": after, "limit": limit}), token=relay_token
    )
    payload, cursor = page_batch(value, after=after, limit=limit, now=board.stamp())
    payload["relay"] = board.refresh_key(relay_url.rstrip("/"))
    # Incomplete records are acknowledged only after the Hub atomically stores
    # unknown scope alongside all identifiable provider objects. Only admin
    # refresh requests bypass the ordinary hourly reconciliation bound.
    result = fetch_json(hub_url.rstrip("/") + "/ci", token=node_token, body=payload)
    if result.get("events") != len(payload["events"]):
        raise ValueError("Hub did not acknowledge CI batch")
    incomplete = len(payload["invalidations"])
    if incomplete and result.get("invalidations") != incomplete:
        raise ValueError("Hub did not acknowledge CI invalidations")
    state.set_cursor(relay_url, cursor)
    return {"events": len(payload["events"]), "cursor": cursor, "incomplete": incomplete}


def inventory(target: Path) -> list[dict[str, str]]:
    from .repos_cmd.fleet import _load_config
    from .fleet_session_presence import repository_identity

    entries, errors, loaded = _load_config(target)
    if loaded and errors:
        raise ValueError("invalid local repository inventory")
    result = []
    for entry in entries:
        if not entry.enabled:
            continue
        identity = repository_identity(entry.path).value
        if identity and identity.startswith("github.com/"):
            name = identity.removeprefix("github.com/")
            result.append({"repo": board.repo_name(name), "visibility": "unknown"})
    return result


def reconcile(
    state: NodeState,
    *,
    target: Path,
    hub_url: str,
    node_token: str,
    admin_token: str | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    now = board.stamp(now)
    plan = fetch_json(hub_url.rstrip("/") + "/ci/work", token=admin_token or node_token)
    plan_received = time.monotonic()
    plan_as_of = board.stamp(plan["as_of"]) if plan.get("as_of") else None
    policy = plan.get("repositories")
    refreshes = plan.get("refresh_requests")
    if not isinstance(policy, list) or len(policy) > board.MAX_REPOSITORIES or not isinstance(refreshes, dict):
        raise ValueError("invalid CI work plan")
    repos = {r["repo"]: r for r in inventory(target)}
    for row in policy:
        if not isinstance(row, dict) or row.get("visibility") not in {"public", "private", "unknown"}:
            raise ValueError("invalid CI work repository")
        name = board.repo_name(row.get("repo"))
        if not admin_token and row["visibility"] != "public":
            raise ValueError("node work plan must not disclose private repositories")
        repos[name] = row
    if len(repos) > board.MAX_REPOSITORIES:
        raise ValueError("CI repository budget exceeded")
    ledger = BudgetLedger(state.path.parent / "github-read-budget.db")
    count, unknown = 0, 0
    failures = []
    for repo, entry in sorted(repos.items()):
        try:
            previous = state.cached(repo)
            refresh_id = refreshes.get(board.refresh_key(repo))
            if refresh_id is not None:
                refresh_id = board.stamp(refresh_id)
            last = previous.get("attempted_at") or previous.get("observed_at")
            age = (
                None
                if not last
                else (
                    datetime.fromisoformat(now.replace("Z", "+00:00"))
                    - datetime.fromisoformat(last.replace("Z", "+00:00"))
                ).total_seconds()
            )
            if age is not None and 0 <= age < 3600 and not refresh_id:
                continue
            state.save(dict(previous, repo=repo, attempted_at=now))
            visibility = cast(Literal["public", "private", "unknown"], entry["visibility"])
            cache = state.etag_cache(repo)
            source = GitHubReader(repo, visibility=visibility, runner=SubprocessRunner(), ledger=ledger, cache=cache)
            try:
                repair_started_at = (
                    board.stamp(
                        (
                            datetime.fromisoformat(plan_as_of.replace("Z", "+00:00"))
                            + timedelta(seconds=max(0.0, time.monotonic() - plan_received))
                        ).isoformat()
                    )
                    if plan_as_of
                    else None
                )
                payload = reconcile_repo(source, now=now, previous=previous, refresh_id=refresh_id)
                payload["repositories"][0]["repair_started_at"] = repair_started_at
            finally:
                state.save_etag_cache(repo, cache)
            result = fetch_json(hub_url.rstrip("/") + "/ci", token=node_token, body=payload)
            if result.get("repositories") != 1:
                raise ValueError("Hub did not acknowledge CI reconcile")
            snapshot = payload["repositories"][0]
            state.save(dict(snapshot, attempted_at=now))
            count += 1
            unknown += not snapshot["complete"]
        except (ValueError, OSError, sqlite3.Error):
            unknown += 1
            failures.append({"repo": repo, "reason": "collection-unavailable"})
    result_payload: dict[str, Any] = {"reconciled": count, "incomplete": unknown}
    if failures:
        result_payload["failures"] = failures
    return result_payload
