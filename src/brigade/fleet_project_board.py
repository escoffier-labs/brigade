"""Hub side of the Projects board: one stored GitHub Projects snapshot per board.

A node with the GitHub ``read:project`` scope collects a bounded snapshot
(``brigade fleet projects sync``) and posts it with its node token. The Hub
validates it against a closed schema, rebuilds every link from validated owner,
repository and number (never from a reported URL), and keeps the last good
snapshot. Nothing here talks to GitHub or writes to a board.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Mapping

SCHEMA_VERSION = 1
MAX_ITEMS = 500
MAX_TITLE = 200
MAX_LABELS = 8
MAX_ASSIGNEES = 5
MAX_PRS = 5
MAX_OPTIONS = 20
MAX_NAME = 50
MAX_SNAPSHOT_BYTES = 900 * 1024
MAX_BODY_BYTES = 1024 * 1024
DEFAULT_OWNER = "escoffier-labs"
DEFAULT_NUMBER = 1
SYNC_COMMAND = f"brigade fleet projects sync --owner {DEFAULT_OWNER} --number {DEFAULT_NUMBER}"

_OWNER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")
_REPO = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}")
_LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})(?:\[bot\])?")
_COLOR = re.compile(r"[0-9a-f]{6}")
_NODE_ID = re.compile(r"[A-Za-z0-9_=-]{1,100}")
_TOP = {
    "schema_version",
    "owner",
    "number",
    "title",
    "url",
    "public",
    "project_id",
    "fields",
    "items",
    "observed_at",
    "complete",
    "skipped",
    "rate_limit",
}
_TOP_OPTIONAL = {"project_id", "skipped", "rate_limit"}
_ITEM = {
    "number",
    "repo",
    "title",
    "state",
    "url",
    "status",
    "labels",
    "assignees",
    "parent",
    "sub_total",
    "sub_done",
    "linked_prs",
    "updated_at",
}


class BoardError(ValueError):
    """The snapshot is malformed, out of bounds, or carries something it must not."""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS fleet_project_boards (owner TEXT NOT NULL, number INTEGER NOT NULL, "
        "observed_at TEXT NOT NULL, snapshot TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY (owner, number))"
    )


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def board_url(owner: str, number: int) -> str:
    return f"https://github.com/orgs/{owner}/projects/{number}"


def issue_url(repo: str, number: int) -> str:
    return f"https://github.com/{repo}/issues/{number}"


def pull_url(repo: str, number: int) -> str:
    return f"https://github.com/{repo}/pull/{number}"


def _int(value: Any, name: str, *, low: int = 0, high: int = 10**9) -> int:
    if type(value) is not int or not low <= value <= high:
        raise BoardError(f"{name} must be an integer between {low} and {high}")
    return value


def _flag(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise BoardError(f"{name} must be a boolean")
    return value


def _text(value: Any, name: str, limit: int, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise BoardError(f"{name} must be a non-empty string")
    if len(value) > limit or not value.isprintable():
        raise BoardError(f"{name} is too long or has control characters")
    return value


def _closed(row: Any, keys: set[str], name: str, optional: set[str] | None = None) -> Mapping[str, Any]:
    if not isinstance(row, dict) or not set(row) <= keys or not (keys - (optional or set())) <= set(row):
        raise BoardError(f"{name} has missing or unknown fields")
    return row


def _list(value: Any, name: str, limit: int) -> list[Any]:
    if not isinstance(value, list) or len(value) > limit:
        raise BoardError(f"{name} must be a list of at most {limit} entries")
    return value


def _stamp(value: Any, name: str) -> str:
    parsed = parse_time(value)
    if parsed is None:
        raise BoardError(f"{name} must be an ISO timestamp")
    return _iso(parsed)


def validate_snapshot(raw: Any) -> dict[str, Any]:
    """Return the normalised snapshot, or raise ``BoardError``."""
    top = _closed(raw, _TOP, "snapshot", _TOP_OPTIONAL)
    if type(top["schema_version"]) is not int or top["schema_version"] != SCHEMA_VERSION:
        raise BoardError(f"schema_version must be {SCHEMA_VERSION}")
    owner = top["owner"]
    if not isinstance(owner, str) or not _OWNER.fullmatch(owner):
        raise BoardError("owner must be a GitHub login")
    number = _int(top["number"], "number", low=1, high=10**6)
    if top["url"] != board_url(owner, number):
        raise BoardError("url must be the board's github.com address")
    fields = _closed(top["fields"], {"status_options"}, "fields")
    options = [
        _text(name, "status option", MAX_NAME)
        for name in _list(fields["status_options"], "status_options", MAX_OPTIONS)
    ]
    if len(set(options)) != len(options):
        raise BoardError("status options must be unique")
    project_id = top.get("project_id")
    if project_id is not None and (not isinstance(project_id, str) or not _NODE_ID.fullmatch(project_id)):
        raise BoardError("project_id must be a GitHub node id")
    limit = top.get("rate_limit")
    if limit is not None:
        limit = _closed(limit, {"cost", "remaining"}, "rate_limit")
        limit = {
            "cost": _int(limit["cost"], "cost", high=100000),
            "remaining": _int(limit["remaining"], "remaining", high=10**7),
        }
    items: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for row in _list(top["items"], "items", MAX_ITEMS):
        row = _closed(row, _ITEM, "item")
        repo = row["repo"]
        if not isinstance(repo, str) or not _REPO.fullmatch(repo):
            raise BoardError("repo must be owner/name")
        item_number = _int(row["number"], "item number", low=1)
        if row["url"] != issue_url(repo, item_number):
            raise BoardError("item url must be the issue's github.com address")
        if row["state"] not in ("OPEN", "CLOSED"):
            raise BoardError("state must be OPEN or CLOSED")
        status = row["status"]
        if status is not None and status not in options:
            raise BoardError("status must be one of the board's status options")
        labels = []
        for label in _list(row["labels"], "labels", MAX_LABELS):
            label = _closed(label, {"name", "color"}, "label")
            color = label["color"]
            if color is not None and (not isinstance(color, str) or not _COLOR.fullmatch(color)):
                raise BoardError("label color must be six lowercase hex digits or null")
            labels.append({"name": _text(label["name"], "label name", MAX_NAME), "color": color})
        assignees = []
        for login in _list(row["assignees"], "assignees", MAX_ASSIGNEES):
            if not isinstance(login, str) or not _LOGIN.fullmatch(login):
                raise BoardError("assignee must be a GitHub login")
            assignees.append(login)
        parent = row["parent"]
        if parent is not None:
            parent = _int(parent, "parent", low=1)
        sub_total = _int(row["sub_total"], "sub_total", high=10000)
        sub_done = _int(row["sub_done"], "sub_done", high=10000)
        if sub_done > sub_total:
            raise BoardError("sub_done cannot exceed sub_total")
        prs = [_int(value, "linked pull request", low=1) for value in _list(row["linked_prs"], "linked_prs", MAX_PRS)]
        key = (repo, item_number)
        if key in seen:
            raise BoardError("duplicate item")
        seen.add(key)
        items.append(
            {
                "number": item_number,
                "repo": repo,
                "title": _text(row["title"], "title", MAX_TITLE),
                "state": row["state"],
                "url": issue_url(repo, item_number),
                "status": status,
                "labels": labels,
                "assignees": assignees,
                "parent": parent,
                "sub_total": sub_total,
                "sub_done": sub_done,
                "linked_prs": prs,
                "updated_at": _stamp(row["updated_at"], "updated_at"),
            }
        )
    snapshot: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "owner": owner,
        "number": number,
        "title": _text(top["title"], "title", MAX_TITLE),
        "url": board_url(owner, number),
        "public": _flag(top["public"], "public"),
        "fields": {"status_options": options},
        "items": items,
        "observed_at": _stamp(top["observed_at"], "observed_at"),
        "complete": _flag(top["complete"], "complete"),
        "skipped": _int(top.get("skipped", 0), "skipped", high=10**6),
    }
    if project_id is not None:
        snapshot["project_id"] = project_id
    if limit is not None:
        snapshot["rate_limit"] = limit
    return snapshot


def store_snapshot(conn: sqlite3.Connection, raw: Any, *, now: datetime | None = None) -> dict[str, Any]:
    """Validate and keep *raw*. An older observation never replaces a newer one."""
    snapshot = validate_snapshot(raw)
    encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_SNAPSHOT_BYTES:
        raise BoardError("snapshot exceeds the size limit")
    ensure_schema(conn)
    floor = datetime.min.replace(tzinfo=timezone.utc)
    key = (snapshot["owner"], snapshot["number"])
    existing = conn.execute("SELECT observed_at FROM fleet_project_boards WHERE owner=? AND number=?", key).fetchone()
    ignored = existing is not None and (parse_time(existing[0]) or floor) > (
        parse_time(snapshot["observed_at"]) or floor
    )
    if not ignored:
        conn.execute(
            "INSERT INTO fleet_project_boards (owner, number, observed_at, snapshot, updated_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(owner, number) DO UPDATE SET observed_at=excluded.observed_at, "
            "snapshot=excluded.snapshot, updated_at=excluded.updated_at",
            (*key, snapshot["observed_at"], encoded, _iso(now or datetime.now(timezone.utc))),
        )
        conn.commit()
    return {"ok": True, "ignored": ignored, "observed_at": snapshot["observed_at"], "items": len(snapshot["items"])}


def load_board(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """The most recently observed stored board, or None. Read-only: never creates the table."""
    try:
        rows = conn.execute("SELECT snapshot FROM fleet_project_boards ORDER BY observed_at DESC LIMIT 5").fetchall()
    except sqlite3.OperationalError:
        return None
    for (encoded,) in rows:
        try:
            return validate_snapshot(json.loads(encoded))
        except (BoardError, json.JSONDecodeError, TypeError):
            continue
    return None
