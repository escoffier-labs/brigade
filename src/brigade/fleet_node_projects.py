"""Hub store for the T3 projects each node can run, keyed by repository identity.

A node reports, per T3 environment, which projects carry a repository identity.
The Hub keeps each node's latest full set so a read-only placement query can
answer which machine has a repository as a T3 project. No path, title or remote
URL is ever accepted or stored.

A node token proves only the node id. Each machine's T3 environment id is
bound by the operator in the policy document (``t3_environment_id``), and a
report is marked ``bound`` only while that binding matches. The read recomputes
the binding from the current policy, so a policy change applies without a
republish. Unbound rows are visible but must not be used for placement.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from . import fleet_hub_policy, fleet_policy
from .fleet_hub import FleetHubConflict, FleetHubError, FleetHubForbidden
from .fleet_hub_routing import MAX_FUTURE_SECONDS, _bounded, _machine_for_node, _parse_stamp

MAX_PROJECTS = 2_000
# project_id and repo_key share the 512 identity limit on the Hub and in the observer.
MAX_ID_CHARS = 512
MAX_ENVIRONMENT_ID_CHARS = 256
# host.tld/segment/segment...: lowercase, a dotted host, at least two path segments,
# and no scheme, userinfo, leading slash or tilde, backslash, whitespace or "..".
REPO_KEY_PATTERN = r"(?!.*\.\.)[a-z0-9-]+(?:\.[a-z0-9-]+)+(?:/(?!\.+(?:/|\Z))[a-z0-9._-]+){2,}"
_REPO_KEY = re.compile(REPO_KEY_PATTERN)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS fleet_node_projects (
    node_id TEXT NOT NULL,
    environment_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    repo_key TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    ttl_seconds INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    bound INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (node_id, project_id)
);
"""
# One row per node, so an empty set still orders against older snapshots.
_SNAPSHOT_SCHEMA = """
CREATE TABLE IF NOT EXISTS fleet_node_project_snapshots (
    node_id TEXT NOT NULL PRIMARY KEY,
    environment_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    ttl_seconds INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Optional tables, created on first report. Not part of the hub startup schema."""
    conn.execute(_SCHEMA)
    conn.execute(_SNAPSHOT_SCHEMA)
    conn.execute("CREATE INDEX IF NOT EXISTS fleet_node_projects_repo ON fleet_node_projects (repo_key)")


def clean_id(raw: object, limit: int) -> str | None:
    """A bounded printable token without whitespace, or None."""
    if (
        isinstance(raw, str)
        and 0 < len(raw) <= limit
        and all(not char.isspace() and char.isprintable() for char in raw)
    ):
        return raw
    return None


def normalize_repo_key(raw: object) -> str | None:
    """Casefold, strip one trailing ``.git``, and require the canonical key shape."""
    if not isinstance(raw, str) or len(raw) > MAX_ID_CHARS:
        return None
    key = raw.casefold()
    if key.endswith(".git"):
        key = key[: -len(".git")]
    if len(key) > MAX_ID_CHARS or _REPO_KEY.fullmatch(key) is None:
        return None
    return key


def project_row(item: object) -> tuple[str, str] | None:
    """The one per-row predicate. The Hub rejects a whole body on None, the observer drops the row."""
    if not isinstance(item, Mapping):
        return None
    project_id = clean_id(item.get("project_id"), MAX_ID_CHARS)
    key = normalize_repo_key(item.get("repo_key"))
    if project_id is None or key is None:
        return None
    return project_id, key


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _validated_projects(raw: Any) -> list[tuple[str, str]]:
    if not isinstance(raw, list):
        raise FleetHubError("node projects field 'projects' must be a list")
    if len(raw) > MAX_PROJECTS:
        raise FleetHubError(f"node projects field 'projects' must have at most {MAX_PROJECTS} entries")
    seen: dict[str, str] = {}
    for index, item in enumerate(raw):
        row = project_row(item)
        if row is None:
            raise FleetHubError(
                f"node projects field 'projects[{index}]' needs a bounded project_id and a canonical repo_key "
                "(lowercase host.tld/segment/segment, no scheme, path or userinfo)"
            )
        if row[0] in seen:
            raise FleetHubError(f"node projects field 'projects[{index}]' repeats a project_id")
        seen[row[0]] = row[1]
    return sorted(seen.items())


def observe_projects(conn: sqlite3.Connection, body: Any, caller_node: str) -> dict[str, Any]:
    """Replace one node's inventory. Older snapshots are ignored, same-instant disagreements conflict."""
    payload = body if isinstance(body, Mapping) else None
    if payload is None:
        raise FleetHubError("node projects report must be a JSON object")
    node_id = _bounded(payload.get("node_id"), "node_id")
    caller = _bounded(caller_node, "caller_node")
    assert node_id is not None and caller is not None
    if node_id != caller:
        raise FleetHubForbidden(f"projects node_id {node_id!r} does not match the caller ({caller})")
    observed_at = _bounded(payload.get("observed_at"), "observed_at")
    assert observed_at is not None
    try:
        parsed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
    # A timestamp without an offset is ambiguous, so it is rejected rather than read as local time.
    if parsed is None or parsed.tzinfo is None:
        raise FleetHubError("node projects field 'observed_at' must be an ISO-8601 timestamp")
    stamp = parsed.astimezone(timezone.utc)
    if (stamp - _now()).total_seconds() > MAX_FUTURE_SECONDS:
        raise FleetHubError("node projects field 'observed_at' is too far in the future")
    ttl = payload.get("ttl_seconds")
    if type(ttl) is not int or not 1 <= ttl <= fleet_policy.MAX_TIMEOUT_SECONDS:
        raise FleetHubError("node projects field 'ttl_seconds' must be an integer in 1..86400")
    environment_id = clean_id(payload.get("environment_id"), MAX_ENVIRONMENT_ID_CHARS)
    if environment_id is not None:
        # Compared and stored casefolded, so a case change cannot split or spoof a binding.
        environment_id = environment_id.casefold()
    if environment_id is None:
        raise FleetHubError(
            f"node projects field 'environment_id' must be a printable token of at most {MAX_ENVIRONMENT_ID_CHARS} characters"
        )
    rows = _validated_projects(payload.get("projects"))

    opened = False
    if conn.in_transaction is False:
        conn.execute("BEGIN IMMEDIATE")
        opened = True
    try:
        # The policy read and the binding check share the write transaction, so a
        # rebinding cannot land between them.
        document = fleet_hub_policy.current_policy(conn)["document"]
        machine = _machine_for_node(document, node_id)
        if machine is None:
            raise FleetHubError(f"projects node_id {node_id!r} is not a configured machine")
        bound_id = (document.get("machines", {}).get(machine) or {}).get("t3_environment_id")
        ensure_schema(conn)
        existing = conn.execute(
            "SELECT observed_at, environment_id FROM fleet_node_project_snapshots WHERE node_id=?", (node_id,)
        ).fetchone()
        previous = _parse_stamp(existing[0]) if existing is not None else None
        if bound_id and bound_id.casefold() != environment_id:
            # A report older than the stored snapshot changes nothing, like any stale report.
            if previous is None or stamp >= previous:
                # Stop placement using a stale environment now.
                conn.execute("DELETE FROM fleet_node_projects WHERE node_id=?", (node_id,))
                conn.execute("DELETE FROM fleet_node_project_snapshots WHERE node_id=?", (node_id,))
                # Commit before raising: handle_policy rolls back on any error, which would undo the
                # clear, and it has written nothing else by this point.
                conn.commit()
            raise FleetHubForbidden(
                "environment-binding-mismatch: the reported T3 environment is not bound to this machine"
            )
        bound = 1 if bound_id else 0
        if existing is not None:
            if previous is not None and stamp < previous:
                if opened:
                    conn.rollback()
                return {"node_id": node_id, "machine": machine, "observed_at": existing[0], "ignored": True}
            if previous is not None and stamp == previous:
                stored = conn.execute(
                    "SELECT project_id, repo_key FROM fleet_node_projects WHERE node_id=? ORDER BY project_id",
                    (node_id,),
                ).fetchall()
                if [tuple(row) for row in stored] != rows or existing[1] != environment_id:
                    raise FleetHubConflict("projects-conflict: same-instant snapshots for this node disagree")
        updated = _now().isoformat()
        conn.execute("DELETE FROM fleet_node_projects WHERE node_id=?", (node_id,))
        conn.executemany(
            "INSERT INTO fleet_node_projects (node_id, environment_id, project_id, repo_key, observed_at, "
            "ttl_seconds, updated_at, bound) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(node_id, environment_id, project_id, key, observed_at, ttl, updated, bound) for project_id, key in rows],
        )
        conn.execute(
            "INSERT INTO fleet_node_project_snapshots (node_id, environment_id, observed_at, ttl_seconds, updated_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(node_id) DO UPDATE SET environment_id=excluded.environment_id, "
            "observed_at=excluded.observed_at, ttl_seconds=excluded.ttl_seconds, updated_at=excluded.updated_at",
            (node_id, environment_id, observed_at, ttl, updated),
        )
        if opened:
            conn.commit()
        return {
            "node_id": node_id,
            "machine": machine,
            "observed_at": observed_at,
            "projects": len(rows),
            "bound": bool(bound),
            "ignored": False,
        }
    except BaseException:
        if opened:
            conn.rollback()
        raise


def projects_for_repo(conn: sqlite3.Connection, repo_key: str, now: datetime | None = None) -> list[dict[str, Any]]:
    """Pure read: every node project for one repository. Never writes or creates a table."""
    key = normalize_repo_key(repo_key) if isinstance(repo_key, str) else None
    if key is None:
        return []
    try:
        found = conn.execute(
            "SELECT node_id, environment_id, project_id, repo_key, observed_at, ttl_seconds "
            "FROM fleet_node_projects WHERE repo_key=? ORDER BY node_id, project_id",
            (key,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    if not found:
        return []
    moment = now or _now()
    machines = fleet_hub_policy.current_policy(conn)["document"].get("machines", {})
    by_node = {
        str(record.get("node_id")): (name, record.get("t3_environment_id"))
        for name, record in machines.items()
        if record.get("node_id")
    }
    result: list[dict[str, Any]] = []
    for node_id, environment_id, project_id, row_key, observed_at, ttl in found:
        machine, bound_id = by_node.get(node_id, (None, None))
        observed = _parse_stamp(observed_at)
        expires = observed + timedelta(seconds=int(ttl)) if observed is not None else None
        result.append(
            {
                "node_id": node_id,
                "machine": machine,
                "environment_id": environment_id,
                "project_id": project_id,
                "repo_key": row_key,
                "observed_at": observed_at,
                "expires_at": expires.isoformat() if expires is not None else None,
                "stale": expires is None or expires <= moment,
                "bound": bool(bound_id) and str(bound_id).casefold() == str(environment_id).casefold(),
            }
        )
    return result
