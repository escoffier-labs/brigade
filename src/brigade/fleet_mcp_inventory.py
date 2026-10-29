"""Hub side of the MCP servers page: stored node reports and the fleet projection.

Read-only in phase 1. The Hub stores what each node reported about its own
MCP config and groups it by server or by machine; it never writes a config,
never runs a command on a machine, and never sees env values, header values,
arguments, full URLs, or paths (the validator rejects anything outside the
closed schema).
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

SCHEMA_VERSION = 1
MAX_ROWS = 300
MAX_HARNESSES = 64
MAX_TEXT = 120
MAX_REPORT_BYTES = 256 * 1024
PUBLISH_COMMAND = "brigade fleet mcp report"
SYNC_COMMAND = "brigade mcp sync"

STATUSES = (
    "current",
    "missing",
    "stale",
    "conflicted",
    "foreign",
    "adopted",
    "invalid",
    "orphan",
    "orphan-edited",
    "unmanaged",
    "disabled",
)
ACCOUNT_STATUSES = ("connected", "needs_auth", "failed", "unknown")
TRANSPORTS = ("stdio", "http", "sse", "unknown")
SCOPES = ("user", "project", "account")
GROUPS = (("server", "Server"), ("machine", "Machine"))
FILTERS = (
    ("needs", "Needs attention"),
    ("drift", "Drift"),
    ("unmanaged", "Unmanaged"),
    ("all", "All"),
)

# Plan statuses grouped by what an admin should read them as.
DRIFT_STATUSES = frozenset({"stale", "conflicted", "foreign", "adopted", "invalid", "orphan-edited"})
UNMANAGED_STATUSES = frozenset({"unmanaged", "orphan"})
LABELS = {
    **{status: "drift" for status in DRIFT_STATUSES},
    **{status: "unmanaged" for status in UNMANAGED_STATUSES},
    "missing": "missing",
    "disabled": "off",
    "current": "ok",
    "connected": "connected",
    "needs_auth": "needs auth",
    "failed": "failed",
    "unknown": "unknown",
}
SEVERITY = {
    "drift": 4,
    "missing": 3,
    "needs auth": 3,
    "failed": 3,
    "unmanaged": 2,
    "off": 1,
    "unknown": 1,
    "ok": 0,
    "connected": 0,
}
HEALTHY = frozenset({"ok", "connected"})
NEEDS_ATTENTION = frozenset({"drift", "missing", "needs auth", "failed", "unmanaged"})

_TOP_KEYS = {"schema_version", "brigade_version", "observed_at", "catalog_present", "truncated", "servers", "harnesses"}
_SERVER_KEYS = {"name", "harness", "scope", "transport", "enabled", "status", "managed", "remote_host"}
_HARNESS_KEYS = {"harness", "scope", "config_found", "readable"}
_HOST = re.compile(r"[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?")
_ABSOLUTE = re.compile(r"^(?:[/\\~]|[A-Za-z]:[\\/])|/(?:home|Users)/|\\Users\\")


class McpReportError(ValueError):
    """The report is malformed, out of bounds, or carries something it must not."""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS fleet_mcp_reports (node_id TEXT PRIMARY KEY, observed_at TEXT NOT NULL, "
        "report TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )


def _iso(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _parse(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------- validation


def _text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise McpReportError(f"{field_name} must be a non-empty string")
    if len(value) > MAX_TEXT or not value.isprintable():
        raise McpReportError(f"{field_name} is too long or has control characters")
    if _ABSOLUTE.search(value):
        raise McpReportError(f"{field_name} must not contain a filesystem path")
    return value


def _choice(value: Any, field_name: str, allowed: tuple[str, ...]) -> str:
    if value not in allowed:
        raise McpReportError(f"{field_name} must be one of {', '.join(allowed)}")
    return str(value)


def _flag(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise McpReportError(f"{field_name} must be a boolean")
    return value


def _host(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 253 or not _HOST.fullmatch(value):
        raise McpReportError("remote_host must be a bare hostname")
    return value


def _closed(row: Any, keys: set[str], field_name: str, *, optional: set[str] | None = None) -> Mapping[str, Any]:
    required = keys - (optional or set())
    if not isinstance(row, dict) or not set(row) <= keys or not required <= set(row):
        raise McpReportError(f"{field_name} has missing or unknown fields")
    return row


def validate_report(raw: Any) -> dict[str, Any]:
    """Return the normalised report, or raise ``McpReportError``."""
    top = _closed(raw, _TOP_KEYS, "report", optional={"truncated"})
    if top["schema_version"] != SCHEMA_VERSION or isinstance(top["schema_version"], bool):
        raise McpReportError(f"schema_version must be {SCHEMA_VERSION}")
    observed = _parse(top["observed_at"])
    if observed is None:
        raise McpReportError("observed_at must be an ISO timestamp")
    version = _text(top["brigade_version"], "brigade_version")
    servers_raw, harnesses_raw = top["servers"], top["harnesses"]
    if not isinstance(servers_raw, list) or len(servers_raw) > MAX_ROWS:
        raise McpReportError(f"servers must be a list of at most {MAX_ROWS} rows")
    if not isinstance(harnesses_raw, list) or len(harnesses_raw) > MAX_HARNESSES:
        raise McpReportError(f"harnesses must be a list of at most {MAX_HARNESSES} rows")
    servers, seen = [], set()
    for row in servers_raw:
        row = _closed(row, _SERVER_KEYS, "server")
        transport = _choice(row["transport"], "transport", TRANSPORTS)
        host = _host(row["remote_host"])
        scope = _choice(row["scope"], "scope", SCOPES)
        account_status = row["status"] in ACCOUNT_STATUSES
        if (scope == "account") != account_status:
            raise McpReportError("account connectors and only account connectors use connector statuses")
        if scope == "account" and (transport != "http" or row["managed"] is not False):
            raise McpReportError("an account connector is an unmanaged http server")
        if transport == "stdio" and host is not None:
            raise McpReportError("remote_host must be null for a stdio server")
        item = {
            "name": _text(row["name"], "name"),
            "harness": _text(row["harness"], "harness"),
            "scope": scope,
            "transport": transport,
            "enabled": _flag(row["enabled"], "enabled"),
            "status": _choice(row["status"], "status", STATUSES + ACCOUNT_STATUSES),
            "managed": _flag(row["managed"], "managed"),
            "remote_host": host,
        }
        key = (item["name"], item["harness"], item["scope"])
        if key in seen:
            raise McpReportError("duplicate server row")
        seen.add(key)
        servers.append(item)
    harnesses: list[dict[str, Any]] = []
    seen_h: set[tuple[str, str]] = set()
    for row in harnesses_raw:
        row = _closed(row, _HARNESS_KEYS, "harness", optional={"scope"})
        item = {
            "harness": _text(row["harness"], "harness"),
            "scope": _choice(row.get("scope", "project"), "scope", SCOPES),
            "config_found": _flag(row["config_found"], "config_found"),
            "readable": _flag(row["readable"], "readable"),
        }
        harness_key = (str(item["harness"]), str(item["scope"]))
        if harness_key in seen_h:
            raise McpReportError("duplicate harness row")
        seen_h.add(harness_key)
        harnesses.append(item)
    return {
        "schema_version": SCHEMA_VERSION,
        "brigade_version": version,
        "observed_at": _iso(observed),
        "catalog_present": _flag(top["catalog_present"], "catalog_present"),
        "truncated": bool(top.get("truncated", False)),
        "servers": servers,
        "harnesses": harnesses,
    }


def store_report(conn: sqlite3.Connection, node_id: str, raw: Any, *, now: datetime | None = None) -> dict[str, Any]:
    report = validate_report(raw)
    encoded = json.dumps(report, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_REPORT_BYTES:
        raise McpReportError("report exceeds the size limit")
    ensure_schema(conn)
    floor = datetime.min.replace(tzinfo=timezone.utc)
    existing = conn.execute("SELECT observed_at FROM fleet_mcp_reports WHERE node_id=?", (node_id,)).fetchone()
    ignored = existing is not None and (_parse(existing[0]) or floor) > (_parse(report["observed_at"]) or floor)
    if not ignored:
        conn.execute(
            "INSERT INTO fleet_mcp_reports (node_id, observed_at, report, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(node_id) DO UPDATE SET observed_at=excluded.observed_at, report=excluded.report, "
            "updated_at=excluded.updated_at",
            (node_id, report["observed_at"], encoded, _iso(now)),
        )
        conn.commit()
    return {"ok": True, "ignored": ignored, "observed_at": report["observed_at"], "servers": len(report["servers"])}


# ---------------------------------------------------------------- projection


def column_key(harness: str, scope: str) -> str:
    """Matrix column: account connectors get their own column beside the harness's local config."""
    return f"{harness} account" if scope == "account" else harness


@dataclass(frozen=True)
class Row:
    node_id: str
    machine: str
    name: str
    harness: str
    scope: str
    transport: str
    enabled: bool
    status: str
    managed: bool
    remote_host: str | None

    @property
    def label(self) -> str:
        return LABELS.get(self.status, "drift")

    @property
    def column(self) -> str:
        return column_key(self.harness, self.scope)


@dataclass(frozen=True)
class HarnessConfig:
    harness: str
    scope: str
    config_found: bool
    readable: bool

    @property
    def column(self) -> str:
        return column_key(self.harness, self.scope)


@dataclass(frozen=True)
class Machine:
    node_id: str
    label: str
    reported: bool
    observed_at: str = ""
    brigade_version: str = ""
    catalog_present: bool = False
    truncated: bool = False
    harnesses: tuple[HarnessConfig, ...] = ()
    rows: tuple[Row, ...] = ()

    @property
    def has_account(self) -> bool:
        return any(row.scope == "account" for row in self.rows)

    @property
    def attention(self) -> int:
        """Distinct servers on this machine that drift, are missing, or are unmanaged."""
        return len({row.name for row in self.rows if row.label in NEEDS_ATTENTION})

    @property
    def unreadable(self) -> int:
        """Harness configs that exist but could not be read, so nothing in them was checked."""
        return sum(1 for config in self.harnesses if not config.readable)


@dataclass(frozen=True)
class Server:
    name: str
    rows: tuple[Row, ...]
    flags: frozenset[str]

    @property
    def machines(self) -> tuple[str, ...]:
        return tuple(sorted({row.machine for row in self.rows}))

    @property
    def harnesses(self) -> tuple[str, ...]:
        return tuple(sorted({row.column for row in self.rows}))

    @property
    def account_only(self) -> bool:
        return bool(self.rows) and all(row.scope == "account" for row in self.rows)

    def machines_with(self, label: str) -> tuple[str, ...]:
        return tuple(sorted({row.machine for row in self.rows if row.label == label}))

    @property
    def issue(self) -> str:
        if "drift" in self.flags:
            return "drift"
        if "missing" in self.flags:
            count = len(self.machines_with("missing"))
            return f"missing on {count} machine{'' if count == 1 else 's'}"
        for flag in ("failed", "needs auth", "unmanaged"):
            if flag in self.flags:
                return flag
        if "off" in self.flags:
            return "off"
        if "unknown" in self.flags:
            return "unknown"
        return "connected" if self.account_only else "ok"

    @property
    def issue_kind(self) -> str:
        if self.issue in {"ok", "connected"}:
            return "ok"
        return "muted" if self.issue in {"off", "unknown"} else "warn"


@dataclass(frozen=True)
class McpView:
    servers: tuple[Server, ...]
    machines: tuple[Machine, ...]
    reports: int

    @property
    def columns(self) -> tuple[str, ...]:
        """Harnesses that hold at least one server somewhere in the fleet."""
        return tuple(sorted({row.column for machine in self.machines for row in machine.rows}))


def matches(server: Server, key: str) -> bool:
    if key == "all":
        return True
    if key == "drift":
        return "drift" in server.flags
    if key == "unmanaged":
        return "unmanaged" in server.flags
    return bool(server.flags & NEEDS_ATTENTION)


def worst(rows: list[Row]) -> Row | None:
    return max(rows, key=lambda row: SEVERITY[row.label], default=None)


def load_view(conn: sqlite3.Connection) -> McpView:
    ensure_schema(conn)
    labels = {
        row[0]: row[1] or "unnamed machine"
        for row in conn.execute("SELECT node_id, label FROM nodes WHERE revoked_at IS NULL ORDER BY label, node_id")
    }
    reports: dict[str, dict[str, Any]] = {}
    for node_id, encoded in conn.execute("SELECT node_id, report FROM fleet_mcp_reports ORDER BY node_id"):
        try:
            reports[node_id] = validate_report(json.loads(encoded))
        except (ValueError, TypeError):
            continue  # a stored row that no longer validates reads as "No MCP report"
    machines: list[Machine] = []
    by_server: dict[str, list[Row]] = {}
    for node_id, label in labels.items():
        report = reports.get(node_id)
        if report is None:
            machines.append(Machine(node_id, label, False))
            continue
        rows = tuple(Row(node_id, label, **row) for row in report["servers"])
        for row in rows:
            by_server.setdefault(row.name, []).append(row)
        machines.append(
            Machine(
                node_id,
                label,
                True,
                report["observed_at"],
                report["brigade_version"],
                report["catalog_present"],
                report["truncated"],
                tuple(HarnessConfig(**item) for item in report["harnesses"]),
                rows,
            )
        )
    servers = tuple(
        Server(name, tuple(rows), frozenset(row.label for row in rows if row.label not in HEALTHY))
        for name, rows in sorted(by_server.items())
    )
    return McpView(servers, tuple(machines), len(reports))


def _plural(count: int, word: str) -> str:
    if word == "harness":
        return f"{count} harness" if count == 1 else f"{count} harnesses"
    return f"{count} {word}{'' if count == 1 else 's'}"


def attention_text(count: int) -> str:
    return f"{count} needs attention" if count == 1 else f"{count} need attention"


def summary(server: Server) -> str:
    return f"{_plural(len(server.machines), 'machine')}, {_plural(len(server.harnesses), 'harness')}"


def verdict(server: Server) -> str:
    """One plain sentence from the real rows."""
    configured = {row for row in server.rows if row.status not in {"missing", "disabled"}}
    machines = sorted({row.machine for row in configured})
    harnesses = sorted({row.column.capitalize() for row in configured})
    if machines:
        where = f"Configured on {_plural(len(machines), 'machine')} in {', '.join(harnesses)}."
    else:
        where = "Not configured on any reporting machine."
    parts = []
    for label, template in (
        ("drift", "Differs from the Brigade catalog on {}."),
        ("missing", "Missing on {}."),
        ("needs auth", "Needs authentication on {}."),
        ("failed", "Failed to connect on {}."),
        ("unmanaged", "Not in the Brigade catalog on {}."),
        ("off", "Turned off on {}."),
    ):
        names = server.machines_with(label)
        if names:
            parts.append(template.format(", ".join(names)))
    clear = "Connected everywhere it appears." if server.account_only else "Matches the Brigade catalog everywhere."
    return " ".join([where, *(parts or [clear])])


def machine_verdict(machine: Machine) -> str:
    if not machine.reported:
        return "No MCP report."
    names = {row.name for row in machine.rows}
    sentence = f"{_plural(len(names), 'server')} across {_plural(len({r.column for r in machine.rows}), 'harness')}"
    sentence += "."
    if not machine.catalog_present:
        sentence += " No Brigade catalog here, so servers read as unmanaged."
    elif machine.attention:
        sentence += (
            f" {_plural(machine.attention, 'server')} {'needs' if machine.attention == 1 else 'need'} attention."
        )
    elif not machine.unreadable:
        sentence += " Everything matches the Brigade catalog."
    if machine.unreadable:
        sentence += f" {_plural(machine.unreadable, 'config')} could not be read, so {'it was' if machine.unreadable == 1 else 'they were'} not checked."
    return sentence
