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
    timeout_seconds = 120
    events_incremental = true

``brigade fleet sink`` performs ONE import pass (operators schedule it with
cron/systemd): the events and claims tables are exported through the same
deterministic writers. Events use ``dolt table import -u`` as an append-only
log, while claims use ``dolt table import -r`` so released claims disappear
from the current snapshot. Dolt is invoked as a binary and is never a Python
dependency. When the sink is disabled, or no ``dolt`` binary is present, the
command says so and exits 0: a documented no-op, never a scheduled-job failure.

The hub's HTTP surface (``/status``, ``/claims``, dashboard) is untouched:
the exporter reads the hub SQLite database read-only on the host that owns it.
Claim fencing tokens are capability secrets and are never exported.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO, cast

from . import toml_compat as tomllib
from .fleet_client import FLEET_CONFIG_REL_PATH, brigade_home

DEFAULT_HUB_DB_REL_PATH = Path(".brigade") / "fleet-hub.db"
DEFAULT_DOLT_BINARY = "dolt"
DEFAULT_DOLT_DIR = "~/.brigade/dolt"
DEFAULT_DOLT_TIMEOUT_SECONDS = 120.0
EVENTS_WATERMARK_FILE = ".brigade-fleet-events-watermark"
SINK_TABLE_EVENTS = "fleet_events"
SINK_TABLE_CLAIMS = "fleet_claims"

EVENT_COLUMNS = ("node_id", "run_id", "sequence", "digest", "repo", "seat", "harness", "state", "ts", "received_at")
_CLAIM_DB_COLUMNS = (
    "target",
    "owner_node",
    "owner_conductor",
    "acquired_at",
    "renewed_at",
    "ttl_seconds",
    "expires_at",
)
CLAIM_COLUMNS = (*_CLAIM_DB_COLUMNS, "expired")

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
    "expires_at varchar(64) NOT NULL, "
    "expired boolean NOT NULL)"
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
    timeout_seconds: float = DEFAULT_DOLT_TIMEOUT_SECONDS
    events_incremental: bool = True


@dataclass
class ExportStats:
    """Counts export rows omitted for reasons that operators should see."""

    unparseable_timestamps: int = 0
    last_received_at: str | None = None


def default_hub_db_path() -> Path:
    """Default hub database location, matching ``brigade fleet serve``."""
    return Path.home() / DEFAULT_HUB_DB_REL_PATH


def resolve_hub_db(raw: str | Path | None = None) -> Path:
    """Resolve the hub database path from an override, else the serve default."""
    candidate = Path(raw).expanduser() if raw is not None else default_hub_db_path()
    return candidate.expanduser()


def parse_since(raw: str | None, *, option: str = "--since") -> datetime | None:
    """Parse an incremental timestamp as ISO-8601 (``Z`` tolerated); naive means UTC."""
    if raw is None or not raw.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        raise FleetExportError(f"{option} must be an ISO-8601 timestamp, got {raw!r}") from None
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


def _csv_value(value: Any) -> Any:
    """Neutralize spreadsheet formulas without changing non-CSV exports."""
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + value
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
        raise FleetExportError(
            f"could not open fleet hub database {db_path}: {exc}; "
            "a WAL-mode database may require its readable -shm sidecar beside the database"
        ) from exc
    return conn


def iter_event_records(
    conn: sqlite3.Connection,
    since: datetime | None = None,
    since_received: datetime | None = None,
    stats: ExportStats | None = None,
    since_received_exclusive: bool = False,
) -> Iterator[dict[str, Any]]:
    """Yield every hub event in ``(node_id, run_id, sequence, digest)`` order.

    ``since`` filters on the event timestamp. ``since_received`` filters on the
    hub receipt timestamp, which is safer for incremental archival because a
    spooled event can arrive after its event timestamp. Rows stream from the
    cursor; nothing is materialized whole.
    """
    if since is not None and since_received is not None:
        raise FleetExportError("--since and --since-received cannot be used together")
    threshold = since_received if since_received is not None else since
    timestamp_column = "received_at" if since_received is not None else "ts"
    cursor = conn.execute(f"SELECT {', '.join(EVENT_COLUMNS)} FROM events ORDER BY node_id, run_id, sequence, digest")
    for row in cursor:
        record = dict(zip(EVENT_COLUMNS, row, strict=True))
        if threshold is not None:
            ts = _parse_ts(record.get(timestamp_column))
            if ts is None:
                if stats is not None:
                    stats.unparseable_timestamps += 1
                continue
            if ts < threshold or (since_received_exclusive and since_received is not None and ts == threshold):
                continue
        received_at = record.get("received_at")
        received_ts = _parse_ts(received_at)
        if stats is not None and isinstance(received_at, str) and received_ts is not None:
            previous_ts = _parse_ts(stats.last_received_at)
            if previous_ts is None or received_ts > previous_ts:
                stats.last_received_at = received_at
        yield record


def iter_claim_records(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    include_expired: bool = False,
) -> Iterator[dict[str, Any]]:
    """Yield the current claims snapshot in ``target`` order.

    Mirrors the hub's ``GET /claims`` payload columns; ``expires_at`` becomes
    its UTC ISO-8601 form. ``holder_token`` is a fencing capability and is
    never selected, so it can never leak into an export.
    """
    now_epoch = now.timestamp()
    cursor = conn.execute(
        f"SELECT {', '.join(_CLAIM_DB_COLUMNS)}, expires_at <= ? AS expired "
        "FROM claims WHERE ? OR expires_at > ? ORDER BY target",
        (now_epoch, include_expired, now_epoch),
    )
    for row in cursor:
        record = dict(zip(CLAIM_COLUMNS, row, strict=True))
        record["expires_at"] = _epoch_to_iso(record.get("expires_at"))
        record["expired"] = bool(record["expired"])
        yield record


def _write_records(
    handle: TextIO,
    records: Iterator[dict[str, Any]],
    *,
    columns: tuple[str, ...],
    fmt: str,
    spreadsheet_safe: bool = False,
) -> int:
    count = 0
    if fmt == "csv":
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(columns)
        for record in records:
            values = ["" if record[column] is None else record[column] for column in columns]
            if spreadsheet_safe:
                values = [_csv_value(value) for value in values]
            writer.writerow(values)
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
    since_received: datetime | None = None,
    fmt: str = "jsonl",
    claims: bool = False,
    include_expired: bool = False,
    export_now: datetime | None = None,
    stats: ExportStats | None = None,
    spreadsheet_safe: bool = False,
    since_received_exclusive: bool = False,
) -> int:
    """Stream one table from the hub database into ``handle``; returns rows."""
    conn = connect_hub_readonly(db_path)
    try:
        try:
            if claims:
                records: Iterator[dict[str, Any]] = iter_claim_records(
                    conn,
                    now=export_now or datetime.now(timezone.utc),
                    include_expired=include_expired,
                )
                columns: tuple[str, ...] = CLAIM_COLUMNS
            else:
                records = iter_event_records(
                    conn,
                    since,
                    since_received,
                    stats,
                    since_received_exclusive,
                )
                columns = EVENT_COLUMNS
            return _write_records(
                handle,
                records,
                columns=columns,
                fmt=fmt,
                spreadsheet_safe=spreadsheet_safe,
            )
        except sqlite3.Error as exc:
            raise FleetExportError(f"could not query fleet hub database {db_path}: {exc}") from exc
        except BrokenPipeError:
            raise
        except OSError as exc:
            raise FleetExportError(f"could not write fleet export: {exc}") from exc
    finally:
        conn.close()


def dispatch_export(args: argparse.Namespace) -> int:
    """Entry point for ``brigade fleet export``."""
    claims = bool(getattr(args, "claims", False))
    include_expired = bool(getattr(args, "include_expired", False))
    since_raw = getattr(args, "since", None)
    since_received_raw = getattr(args, "since_received", None)
    try:
        since = parse_since(since_raw)
        since_received = parse_since(since_received_raw, option="--since-received")
        if since is not None and since_received is not None:
            raise FleetExportError("--since and --since-received cannot be used together")
        if include_expired and not claims:
            raise FleetExportError("--include-expired requires --claims")
        if (since is not None or since_received is not None) and claims:
            print("note: incremental filters apply to events; the claims snapshot is current state", file=sys.stderr)
        db_path = resolve_hub_db(getattr(args, "db", None))
        out_path = getattr(args, "out", None)
        stats = ExportStats()
        if out_path is not None:
            destination = Path(out_path).expanduser()
            temporary: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    "w",
                    newline="",
                    encoding="utf-8",
                    dir=destination.parent,
                    prefix=f".{destination.name}.",
                    suffix=".tmp",
                    delete=False,
                ) as handle:
                    temporary = Path(handle.name)
                    count = export_into(
                        cast(TextIO, handle),
                        db_path=db_path,
                        since=since,
                        since_received=since_received,
                        fmt=args.format,
                        claims=claims,
                        include_expired=include_expired,
                        stats=stats,
                        spreadsheet_safe=args.format == "csv",
                    )
                assert temporary is not None
                os.replace(temporary, destination)
                temporary = None
            except OSError as exc:
                raise FleetExportError(f"could not write fleet export to {out_path}: {exc}") from exc
            finally:
                if temporary is not None:
                    try:
                        temporary.unlink()
                    except FileNotFoundError:
                        pass
        else:
            try:
                reconfigure_stdout = getattr(sys.stdout, "reconfigure", None)
                if not callable(reconfigure_stdout):
                    raise FleetExportError("stdout cannot be configured for UTF-8 with LF newlines; use --out")
                reconfigure_stdout(encoding="utf-8", errors="strict", newline="\n")
                count = export_into(
                    sys.stdout,
                    db_path=db_path,
                    since=since,
                    since_received=since_received,
                    fmt=args.format,
                    claims=claims,
                    include_expired=include_expired,
                    stats=stats,
                    spreadsheet_safe=args.format == "csv",
                )
            except BrokenPipeError:
                try:
                    sys.stdout.close()
                except (BrokenPipeError, OSError):
                    pass
                sys.stdout = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
                return 0
    except FleetExportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    what = "claim(s)" if claims else "event(s)"
    summary = f"exported {count} fleet {what} from {db_path} ({args.format})"
    if (since is not None or since_received is not None) and not claims:
        timestamp_column = "received_at" if since_received is not None else "ts"
        summary += f"; skipped {stats.unparseable_timestamps} event(s) with unparseable {timestamp_column}"
    print(summary, file=sys.stderr)
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
        timeout_seconds=(
            float(section["timeout_seconds"])
            if isinstance(section.get("timeout_seconds"), (int, float))
            and not isinstance(section["timeout_seconds"], bool)
            and section["timeout_seconds"] > 0
            else DEFAULT_DOLT_TIMEOUT_SECONDS
        ),
        events_incremental=(
            section["events_incremental"] if isinstance(section.get("events_incremental"), bool) else True
        ),
    )


def _run_dolt(
    argv: list[str],
    *,
    dolt_binary: str,
    cwd: Path,
    timeout_seconds: float,
) -> tuple[bool, str, str]:
    """Run one dolt subprocess; return success, operator message, and stdout."""
    command = [dolt_binary, *argv]
    try:
        proc = subprocess.run(  # noqa: S603 - argv is constructed, never shell-interpolated
            command,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, f"dolt timed out after {timeout_seconds:g}s: {' '.join(command)}", ""
    except OSError as exc:
        return False, f"could not run dolt: {exc}", ""
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        message = detail[-1] if detail else f"exit code {proc.returncode}"
        return False, f"dolt {' '.join(argv[:2])} failed: {message}", ""
    return True, "", proc.stdout


def _read_event_watermark(dolt_dir: Path) -> datetime | None:
    """Return a valid event receipt watermark, else request a full export."""
    try:
        raw = (dolt_dir / EVENTS_WATERMARK_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        return parse_since(raw, option="fleet sink event watermark")
    except FleetExportError:
        return None


def _write_event_watermark(dolt_dir: Path, received_at: str) -> None:
    """Atomically persist the last committed event receipt timestamp."""
    destination = dolt_dir / EVENTS_WATERMARK_FILE
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=dolt_dir,
            prefix=f".{EVENTS_WATERMARK_FILE}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(received_at + "\n")
        assert temporary is not None
        os.replace(temporary, destination)
        temporary = None
    except OSError as exc:
        raise FleetExportError(f"could not write fleet sink event watermark {destination}: {exc}") from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def run_sink_pass(*, db_path: Path, config: SinkConfig) -> int:
    """Export events as a log and claims as a snapshot; return an exit code."""
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
    try:
        export_now = datetime.now(timezone.utc)
        event_stats = ExportStats()
        watermark = _read_event_watermark(dolt_dir) if config.events_incremental else None
        with tempfile.TemporaryDirectory(prefix="brigade-fleet-sink-") as tmp:
            tmp_dir = Path(tmp)
            events_csv = tmp_dir / f"{SINK_TABLE_EVENTS}.csv"
            claims_csv = tmp_dir / f"{SINK_TABLE_CLAIMS}.csv"
            with events_csv.open("w", newline="", encoding="utf-8") as handle:
                event_count = export_into(
                    handle,
                    db_path=db_path,
                    since_received=watermark,
                    fmt="csv",
                    claims=False,
                    stats=event_stats,
                    since_received_exclusive=watermark is not None,
                )
            with claims_csv.open("w", newline="", encoding="utf-8") as handle:
                claim_count = export_into(
                    handle,
                    db_path=db_path,
                    fmt="csv",
                    claims=True,
                    include_expired=False,
                    export_now=export_now,
                )
            steps: list[list[str]] = []
            if not (dolt_dir / ".dolt").is_dir():
                steps.append(["init", "--name", "brigade", "--email", "brigade@localhost"])
            steps.append(["sql", "-q", _DOLT_SCHEMA_EVENTS])
            steps.append(["table", "import", "-u", SINK_TABLE_EVENTS, str(events_csv)])
            steps.append(["sql", "-q", _DOLT_SCHEMA_CLAIMS])
            steps.append(["table", "import", "-r", SINK_TABLE_CLAIMS, str(claims_csv)])
            for step in steps:
                ok, message, _ = _run_dolt(
                    step,
                    dolt_binary=dolt_binary,
                    cwd=dolt_dir,
                    timeout_seconds=config.timeout_seconds,
                )
                if not ok:
                    print(f"error: {message}", file=sys.stderr)
                    return 1
            ok, message, status = _run_dolt(
                ["status", "--porcelain"],
                dolt_binary=dolt_binary,
                cwd=dolt_dir,
                timeout_seconds=config.timeout_seconds,
            )
            if not ok:
                print(f"error: {message}", file=sys.stderr)
                return 1
            committed = bool(status.strip())
            if committed:
                timestamp = export_now.strftime("%Y-%m-%dT%H:%M:%SZ")
                commit_steps = [
                    ["add", "-A"],
                    ["commit", "-m", f"{timestamp}: fleet events/claims"],
                ]
                for step in commit_steps:
                    ok, message, _ = _run_dolt(
                        step,
                        dolt_binary=dolt_binary,
                        cwd=dolt_dir,
                        timeout_seconds=config.timeout_seconds,
                    )
                    if not ok:
                        print(f"error: {message}", file=sys.stderr)
                        return 1
                if config.events_incremental and event_stats.last_received_at is not None:
                    _write_event_watermark(dolt_dir, event_stats.last_received_at)
    except FleetExportError:
        raise
    except (sqlite3.Error, OSError) as exc:
        raise FleetExportError(f"could not export fleet sink data: {exc}") from exc
    commit_status = "committed changes" if committed else "no working-set changes to commit"
    print(
        f"fleet sink: imported {event_count} event(s) and {claim_count} claim(s) into dolt database {dolt_dir}; "
        f"{commit_status}"
    )
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
