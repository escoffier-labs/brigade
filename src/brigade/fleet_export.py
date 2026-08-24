"""Fleet export and the optional Dolt history sink (issue #1127, epic #1121 phase 5).

``brigade fleet export`` streams the hub's ``events`` table (or, with
``--claims``, the current ``claims`` snapshot) as deterministic JSONL or CSV:
rows come out in primary-key order, columns in a fixed order, and the digest
chain is included, so two exports of the same database are byte-identical and
safe to diff, archive, or replay. Records go to stdout (or ``--out``); human
status goes to stderr so the stream pipes cleanly.

The optional sink mirrors those rows into a local Dolt database for versioned
fleet history/analytics. Configuration lives in the existing fleet config
(``~/.brigade/fleet.toml``, honoring ``BRIGADE_HOME``)::

    [fleet.sink]
    enabled = true             # off by default; the sink never runs unless asked
    dolt_binary = "dolt"       # resolved on PATH (or an explicit path)
    dolt_dir = "~/.brigade/dolt"
    db = "~/.brigade/fleet-hub.db"  # hub database override

``brigade fleet sink`` performs ONE import pass (operators schedule it with
cron/systemd): the events and claims tables are exported through the same
deterministic writers and fed to ``dolt table import -u`` as a subprocess, so
re-running a pass updates rows in place instead of duplicating them. Dolt is
invoked as a binary and is never a Python dependency. When the sink is
disabled, or no ``dolt`` binary is present, the command says so and exits 0:
a documented no-op, never a scheduled-job failure.

The hub's HTTP surface (``/status``, ``/claims``, dashboard) is untouched:
the exporter reads the hub SQLite database read-only on the host that owns it.
Claim fencing tokens are capability secrets and are never exported.
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

from . import toml_compat as tomllib
from .fleet_client import FLEET_CONFIG_REL_PATH, brigade_home

DEFAULT_HUB_DB_REL_PATH = Path(".brigade") / "fleet-hub.db"
DEFAULT_DOLT_BINARY = "dolt"
DEFAULT_DOLT_DIR = "~/.brigade/dolt"
DOLT_TIMEOUT_SECONDS = 120.0
SINK_TABLE_EVENTS = "fleet_events"
SINK_TABLE_CLAIMS = "fleet_claims"

EVENT_COLUMNS = ("node_id", "run_id", "sequence", "digest", "repo", "seat", "harness", "state", "ts", "received_at")
CLAIM_COLUMNS = ("target", "owner_node", "owner_conductor", "acquired_at", "renewed_at", "ttl_seconds", "expires_at")

_DOLT_SCHEMA_EVENTS = (
    f"CREATE TABLE IF NOT EXISTS {SINK_TABLE_EVENTS} ("
    "node_id varchar(128) NOT NULL, "
    "run_id varchar(256) NOT NULL, "
    "sequence bigint NOT NULL, "
    "digest varchar(64) NOT NULL, "
    "repo varchar(512), "
    "seat varchar(512), "
    "harness varchar(512), "
    "state varchar(64) NOT NULL, "
    "ts varchar(64) NOT NULL, "
    "received_at varchar(64) NOT NULL, "
    "PRIMARY KEY (node_id, run_id, sequence, digest))"
)
_DOLT_SCHEMA_CLAIMS = (
    f"CREATE TABLE IF NOT EXISTS {SINK_TABLE_CLAIMS} ("
    "target varchar(512) NOT NULL PRIMARY KEY, "
    "owner_node varchar(128) NOT NULL, "
    "owner_conductor varchar(128), "
    "acquired_at varchar(64) NOT NULL, "
    "renewed_at varchar(64) NOT NULL, "
    "ttl_seconds bigint NOT NULL, "
    "expires_at varchar(64) NOT NULL)"
)


class FleetExportError(RuntimeError):
    """Operator-facing export or sink configuration/input failure."""


@dataclass(frozen=True)
class SinkConfig:
    """Parsed ``[fleet.sink]`` configuration; everything defaults to off."""

    enabled: bool = False
    dolt_binary: str = DEFAULT_DOLT_BINARY
    dolt_dir: str = DEFAULT_DOLT_DIR
    db: str | None = None


def default_hub_db_path() -> Path:
    """Default hub database location, matching ``brigade fleet serve``."""
    return Path.home() / DEFAULT_HUB_DB_REL_PATH


def resolve_hub_db(raw: str | Path | None = None) -> Path:
    """Resolve the hub database path from an override, else the serve default."""
    candidate = Path(raw).expanduser() if raw is not None else default_hub_db_path()
    return candidate.expanduser()


def parse_since(raw: str | None) -> datetime | None:
    """Parse ``--since`` as ISO-8601 (``Z`` tolerated); naive means UTC."""
    if raw is None or not raw.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        raise FleetExportError(f"--since must be an ISO-8601 timestamp, got {raw!r}") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _epoch_to_iso(value: Any) -> Any:
    """Render a Unix-epoch ``expires_at`` as UTC ISO-8601 (pure, deterministic)."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    return value


def connect_hub_readonly(db_path: Path) -> sqlite3.Connection:
    """Open the hub database strictly read-only; refuse to invent one."""
    db_path = Path(db_path).expanduser()
    if not db_path.is_file():
        raise FleetExportError(
            f"no fleet hub database at {db_path}; run 'brigade fleet serve' there first or pass --db"
        )
    try:
        conn = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
        conn.execute("SELECT 1")
    except sqlite3.Error as exc:
        raise FleetExportError(f"could not open fleet hub database {db_path}: {exc}") from exc
    return conn


def iter_event_records(conn: sqlite3.Connection, since: datetime | None = None) -> Iterator[dict[str, Any]]:
    """Yield every hub event in ``(node_id, run_id, sequence, digest)`` order.

    ``since`` filters on the event timestamp (events whose ``ts`` cannot be
    parsed are excluded while filtering, so the stream stays honest about
    ordering). Rows stream from the cursor; nothing is materialized whole.
    """
    cursor = conn.execute(f"SELECT {', '.join(EVENT_COLUMNS)} FROM events ORDER BY node_id, run_id, sequence, digest")
    for row in cursor:
        record = dict(zip(EVENT_COLUMNS, row, strict=True))
        if since is not None:
            ts = _parse_ts(record.get("ts"))
            if ts is None or ts < since:
                continue
        yield record


def iter_claim_records(conn: sqlite3.Connection) -> Iterator[dict[str, Any]]:
    """Yield the current claims snapshot in ``target`` order.

    Mirrors the hub's ``GET /claims`` payload columns; ``expires_at`` becomes
    its UTC ISO-8601 form. ``holder_token`` is a fencing capability and is
    never selected, so it can never leak into an export.
    """
    cursor = conn.execute(f"SELECT {', '.join(CLAIM_COLUMNS)} FROM claims ORDER BY target")
    for row in cursor:
        record = dict(zip(CLAIM_COLUMNS, row, strict=True))
        record["expires_at"] = _epoch_to_iso(record.get("expires_at"))
        yield record


def _write_records(handle: TextIO, records: Iterator[dict[str, Any]], *, columns: tuple[str, ...], fmt: str) -> int:
    count = 0
    if fmt == "csv":
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(columns)
        for record in records:
            writer.writerow(["" if record[column] is None else record[column] for column in columns])
            count += 1
    elif fmt == "jsonl":
        for record in records:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
            count += 1
    else:
        raise FleetExportError(f"unsupported export format {fmt!r} (use jsonl or csv)")
    return count


def export_into(
    handle: TextIO,
    *,
    db_path: Path,
    since: datetime | None = None,
    fmt: str = "jsonl",
    claims: bool = False,
) -> int:
    """Stream one table from the hub database into ``handle``; returns rows."""
    conn = connect_hub_readonly(db_path)
    try:
        if claims:
            records: Iterator[dict[str, Any]] = iter_claim_records(conn)
            columns: tuple[str, ...] = CLAIM_COLUMNS
        else:
            records = iter_event_records(conn, since)
            columns = EVENT_COLUMNS
        return _write_records(handle, records, columns=columns, fmt=fmt)
    finally:
        conn.close()


def dispatch_export(args: argparse.Namespace) -> int:
    """Entry point for ``brigade fleet export``."""
    claims = bool(getattr(args, "claims", False))
    since_raw = getattr(args, "since", None)
    try:
        since = parse_since(since_raw)
        if since is not None and claims:
            print("note: --since applies to events; the claims snapshot is current state", file=sys.stderr)
        db_path = resolve_hub_db(getattr(args, "db", None))
        out_path = getattr(args, "out", None)
        if out_path is not None:
            with Path(out_path).expanduser().open("w", newline="", encoding="utf-8") as handle:
                count = export_into(handle, db_path=db_path, since=since, fmt=args.format, claims=claims)
        else:
            count = export_into(sys.stdout, db_path=db_path, since=since, fmt=args.format, claims=claims)
    except FleetExportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    what = "claim(s)" if claims else "event(s)"
    print(f"exported {count} fleet {what} from {db_path} ({args.format})", file=sys.stderr)
    return 0


def load_sink_config() -> SinkConfig:
    """Read ``[fleet.sink]`` from the fleet config; missing file/keys mean off."""
    config_path = brigade_home() / FLEET_CONFIG_REL_PATH.name
    section: dict[str, Any] = {}
    if config_path.is_file():
        try:
            payload = tomllib.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            payload = {}
        fleet_section = payload.get("fleet") if isinstance(payload, dict) else None
        sink_section = fleet_section.get("sink") if isinstance(fleet_section, dict) else None
        if isinstance(sink_section, dict):
            section = sink_section
    enabled = section.get("enabled") is True

    def _string(key: str, fallback: str | None) -> str | None:
        value = section.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        return fallback

    return SinkConfig(
        enabled=enabled,
        dolt_binary=_string("dolt_binary", DEFAULT_DOLT_BINARY) or DEFAULT_DOLT_BINARY,
        dolt_dir=_string("dolt_dir", DEFAULT_DOLT_DIR) or DEFAULT_DOLT_DIR,
        db=_string("db", None),
    )


def _run_dolt(argv: list[str], *, dolt_binary: str, cwd: Path) -> tuple[bool, str]:
    """Run one dolt subprocess (fixed argv, no shell); (ok, operator message)."""
    command = [dolt_binary, *argv]
    try:
        proc = subprocess.run(  # noqa: S603 - argv is constructed, never shell-interpolated
            command,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=DOLT_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, f"dolt timed out after {int(DOLT_TIMEOUT_SECONDS)}s: {' '.join(command)}"
    except OSError as exc:
        return False, f"could not run dolt: {exc}"
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        message = detail[-1] if detail else f"exit code {proc.returncode}"
        return False, f"dolt {' '.join(argv[:2])} failed: {message}"
    return True, ""


def run_sink_pass(*, db_path: Path, config: SinkConfig) -> int:
    """One export + ``dolt table import -u`` pass; returns a process exit code."""
    dolt_binary = shutil.which(config.dolt_binary)
    if dolt_binary is None:
        print(
            f"fleet sink: no '{config.dolt_binary}' binary found on PATH; the Dolt sink is a documented no-op "
            "(install dolt, or point [fleet.sink] dolt_binary at it)"
        )
        return 0
    dolt_dir = Path(config.dolt_dir).expanduser()
    try:
        dolt_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"error: could not create dolt directory {dolt_dir}: {exc}", file=sys.stderr)
        return 1
    with tempfile.TemporaryDirectory(prefix="brigade-fleet-sink-") as tmp:
        tmp_dir = Path(tmp)
        events_csv = tmp_dir / f"{SINK_TABLE_EVENTS}.csv"
        claims_csv = tmp_dir / f"{SINK_TABLE_CLAIMS}.csv"
        try:
            with events_csv.open("w", newline="", encoding="utf-8") as handle:
                event_count = export_into(handle, db_path=db_path, fmt="csv", claims=False)
            with claims_csv.open("w", newline="", encoding="utf-8") as handle:
                claim_count = export_into(handle, db_path=db_path, fmt="csv", claims=True)
        except FleetExportError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        steps: list[list[str]] = []
        if not (dolt_dir / ".dolt").is_dir():
            steps.append(["init"])
        steps.append(["sql", "-q", _DOLT_SCHEMA_EVENTS])
        steps.append(["table", "import", "-u", SINK_TABLE_EVENTS, str(events_csv)])
        steps.append(["sql", "-q", _DOLT_SCHEMA_CLAIMS])
        steps.append(["table", "import", "-u", SINK_TABLE_CLAIMS, str(claims_csv)])
        for step in steps:
            ok, message = _run_dolt(step, dolt_binary=dolt_binary, cwd=dolt_dir)
            if not ok:
                print(f"error: {message}", file=sys.stderr)
                return 1
    print(f"fleet sink: imported {event_count} event(s) and {claim_count} claim(s) into dolt database {dolt_dir}")
    return 0


def dispatch_sink(args: argparse.Namespace) -> int:
    """Entry point for ``brigade fleet sink``."""
    config = load_sink_config()
    if not config.enabled:
        print(
            "fleet sink: disabled ([fleet.sink] enabled = true in ~/.brigade/fleet.toml activates it); "
            "nothing was exported and no dolt binary was run"
        )
        return 0
    try:
        db_path = resolve_hub_db(getattr(args, "db", None) or config.db)
        return run_sink_pass(db_path=db_path, config=config)
    except FleetExportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
