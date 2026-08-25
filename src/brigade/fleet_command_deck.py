"""Fleet Command Deck: config loading, bounded reads, projections, and HTML.

Independent of ``fleet_hub`` by design: this module owns immutable deck
configuration, capped SQLite read helpers over the shared ``events`` and
``claims`` tables, pure projections, and server-rendered HTML. It never
imports ``fleet_hub``.
"""

import html
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence

CLAIM_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")

TERMINAL_STATES = frozenset({"run.completed", "run.failed", "run.interrupted"})
AWAITING_STATES = frozenset({"run.paused", "approval.requested", "approval.held"})
ACTIVE_LIMIT, OBSERVER_LIMIT, RAIL_FAILURE_LIMIT = 200, 8, 10
BUCKET_RANK = {"failed": 0, "awaiting approval": 1, "stale": 2, "running": 3, "queued": 4}

_START_CHUNK = 400
_UTC = timezone.utc


class DeckConfigError(ValueError):
    pass


@dataclass(frozen=True)
class StationConfig:
    node_id: str
    name: str
    capacity: int


@dataclass(frozen=True)
class DeckConfig:
    stations: Sequence[StationConfig] = ()
    stale_after_seconds: int = 1800
    outcome_window: int = 20
    failed_lookback_seconds: int = 86400


@dataclass(frozen=True)
class LiveRun:
    node_id: str
    run_id: str
    repo: str
    seat: str
    harness: str
    state: str
    bucket: str
    age_seconds: int | None
    elapsed_seconds: int | None


@dataclass(frozen=True)
class Claim:
    target: str
    owner_node: str
    owner_conductor: str
    ttl_remaining: int


@dataclass(frozen=True)
class Tile:
    run: LiveRun
    claim: Claim | None
    collision: bool


@dataclass(frozen=True)
class StationView:
    station: StationConfig
    label: str
    enrolled: bool
    busy: int
    last_heard: str | None
    tiles: tuple[Tile, ...]


@dataclass(frozen=True)
class RailEntry:
    kind: str
    node_id: str
    repo: str
    run_id: str
    detail: str


@dataclass(frozen=True)
class RepoRow:
    target: str
    claim: Claim | None
    live: tuple[LiveRun, ...]
    collision: bool


@dataclass(frozen=True)
class DeckView:
    stations: tuple[StationView, ...]
    rail: tuple[RailEntry, ...]
    repos: tuple[RepoRow, ...]
    outcomes: tuple[LiveRun, ...]
    observers: tuple[tuple[str, str], ...]


def resolve_config_path(flag_value: str | Path | None, environ: Mapping[str, str]) -> Path | None:
    value = flag_value if flag_value is not None else environ.get("BRIGADE_FLEET_DECK_CONFIG")
    return Path(value).expanduser() if isinstance(value, (str, Path)) and str(value).strip() else None


def load_config(path: Path) -> DeckConfig:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DeckConfigError(f"invalid deck config: {exc}") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("stations"), list):
        raise DeckConfigError("deck config requires a stations array")
    stations = tuple(_station(item) for item in raw["stations"])
    if not 1 <= len(stations) <= 16 or len({item.node_id for item in stations}) != len(stations):
        raise DeckConfigError("deck config requires 1..16 unique stations")
    return DeckConfig(
        stations=stations,
        stale_after_seconds=_bounded_int(raw, "stale_after_seconds", 1800, 300, 21600),
        outcome_window=_bounded_int(raw, "outcome_window", 20, 1, 100),
        failed_lookback_seconds=_bounded_int(raw, "failed_lookback_seconds", 86400, 1, 2_592_000),
    )


def _station(raw: object) -> StationConfig:
    if not isinstance(raw, dict):
        raise DeckConfigError("station must be a JSON object")
    node_id = raw.get("node_id")
    if not isinstance(node_id, str) or not CLAIM_ID_RE.match(node_id) or node_id == "unknown":
        raise DeckConfigError("station node_id is invalid")
    name_raw = raw.get("name")
    if name_raw is None:
        name = ""
    else:
        if not isinstance(name_raw, str):
            raise DeckConfigError("station name must be a string")
        name = _strip_controls(name_raw)
        if not 1 <= len(name) <= 64:
            raise DeckConfigError("station name must be 1..64 characters after stripping controls")
    capacity = raw.get("capacity")
    if type(capacity) is not int or not 1 <= capacity <= 64:
        raise DeckConfigError("station capacity must be an integer in 1..64")
    return StationConfig(node_id=node_id, name=name, capacity=capacity)


def _bounded_int(raw: Mapping[str, object], key: str, default: int, low: int, high: int) -> int:
    if key not in raw:
        return default
    value = raw[key]
    if type(value) is not int or not low <= value <= high:
        raise DeckConfigError(f"{key} must be an integer in {low}..{high}")
    return value


def _strip_controls(value: str) -> str:
    return _CONTROL_RE.sub("", value).strip()


def bucket_for(state: str, *, age_seconds: int | None, stale_after_seconds: int) -> str:
    if state in AWAITING_STATES:
        return "awaiting approval"
    if state.endswith("failed"):
        return "failed"
    if age_seconds is not None and age_seconds > stale_after_seconds:
        return "stale"
    if state == "run.created":
        return "queued"
    return "running"


def collides(target: str, live_runs: Sequence[LiveRun], claims: Mapping[str, Claim]) -> bool:
    nodes = {run.node_id for run in live_runs if run.repo == target}
    claim = claims.get(target)
    if len(nodes) >= 2:
        return True
    return bool(nodes) and claim is not None and claim.owner_node not in nodes


_LATEST_ROWS = (
    "SELECT node_id, run_id, repo, seat, harness, state, ts FROM ("
    "  SELECT e.*, ROW_NUMBER() OVER ("
    "    PARTITION BY node_id, run_id ORDER BY sequence DESC, received_at DESC, digest DESC"
    "  ) AS rn FROM events e"
    ") WHERE rn = 1"
)

_AWAITING_PLACEHOLDERS = ",".join("?" for _ in AWAITING_STATES)
_TERMINAL_PLACEHOLDERS = ",".join("?" for _ in TERMINAL_STATES)


def fetch_live_runs(conn: sqlite3.Connection, *, now: datetime, stale_after_seconds: int) -> list[LiveRun]:
    rank_sql = f"CASE WHEN state LIKE '%failed' THEN 0 WHEN state IN ({_AWAITING_PLACEHOLDERS}) THEN 1 ELSE 3 END"
    query = (
        f"{_LATEST_ROWS} AND state NOT IN ({_TERMINAL_PLACEHOLDERS})"
        f" ORDER BY {rank_sql}, ts DESC, node_id, run_id LIMIT {ACTIVE_LIMIT}"
    )
    rows = conn.execute(query, (*TERMINAL_STATES, *AWAITING_STATES)).fetchall()
    started = fetch_started_at(conn, [(row[0], row[1]) for row in rows])
    runs: list[LiveRun] = []
    for node_id, run_id, repo, seat, harness, state, ts in rows:
        age = _age_seconds(ts, now)
        runs.append(
            LiveRun(
                node_id=node_id,
                run_id=run_id,
                repo=repo or "",
                seat=seat or "",
                harness=harness or "",
                state=state,
                bucket=bucket_for(state, age_seconds=age, stale_after_seconds=stale_after_seconds),
                age_seconds=age,
                elapsed_seconds=_elapsed_seconds(started.get((node_id, run_id)), now),
            )
        )
    return runs


def fetch_started_at(conn: sqlite3.Connection, keys: Sequence[tuple[str, str]]) -> dict[tuple[str, str], str]:
    result: dict[tuple[str, str], str] = {}
    for start in range(0, len(keys), _START_CHUNK):
        chunk = keys[start : start + _START_CHUNK]
        predicate = " OR ".join("(node_id = ? AND run_id = ?)" for _ in chunk)
        params = [value for pair in chunk for value in pair]
        rows = conn.execute(
            "SELECT node_id, run_id, ts FROM ("
            "  SELECT node_id, run_id, ts, ROW_NUMBER() OVER ("
            "    PARTITION BY node_id, run_id ORDER BY sequence ASC, received_at ASC, digest ASC"
            "  ) AS rn FROM events WHERE " + predicate + ")"
            " WHERE rn = 1",
            params,
        ).fetchall()
        for row in rows:
            result[(row[0], row[1])] = row[2]
    return result


def fetch_outcomes(conn: sqlite3.Connection, *, outcome_window: int) -> list[LiveRun]:
    rows = conn.execute(
        f"{_LATEST_ROWS} AND state IN ({_TERMINAL_PLACEHOLDERS}) ORDER BY ts DESC LIMIT ?",
        (*TERMINAL_STATES, int(outcome_window)),
    ).fetchall()
    return [_live_run_from_row(row) for row in rows]


def fetch_failed_outcomes(conn: sqlite3.Connection, *, now: datetime, lookback_seconds: int) -> list[LiveRun]:
    cutoff = datetime.fromtimestamp(now.timestamp() - lookback_seconds, tz=now.tzinfo or _UTC).isoformat()
    rows = conn.execute(
        f"{_LATEST_ROWS} AND state = 'run.failed' AND ts >= ? ORDER BY ts DESC LIMIT {RAIL_FAILURE_LIMIT}",
        (cutoff,),
    ).fetchall()
    return [_live_run_from_row(row, bucket="failed") for row in rows]


def fetch_last_heard(conn: sqlite3.Connection, node_ids: Sequence[str]) -> dict[str, str]:
    ids = sorted(set(node_ids))
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT node_id, MAX(received_at) FROM events WHERE node_id IN ({placeholders}) GROUP BY node_id",
        ids,
    ).fetchall()
    return {row[0]: row[1] for row in rows}


def fetch_observers(conn: sqlite3.Connection, configured: frozenset[str]) -> list[tuple[str, str]]:
    placeholders = ",".join("?" for _ in configured)
    predicate = f" WHERE node_id NOT IN ({placeholders})" if configured else ""
    query = (
        "SELECT node_id, MAX(received_at) FROM events"
        + predicate
        + f" GROUP BY node_id ORDER BY MAX(received_at) DESC LIMIT {OBSERVER_LIMIT}"
    )
    return [(row[0], row[1]) for row in conn.execute(query, tuple(configured)).fetchall()]


def _live_run_from_row(row: tuple, *, bucket: str | None = None) -> LiveRun:
    return LiveRun(
        node_id=row[0],
        run_id=row[1],
        repo=row[2] or "",
        seat=row[3] or "",
        harness=row[4] or "",
        state=row[5],
        bucket=row[5] if bucket is None else bucket,
        age_seconds=None,
        elapsed_seconds=None,
    )


def _age_seconds(ts: str, now: datetime) -> int | None:
    try:
        moment = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=now.tzinfo or _UTC)
    return max(0, int((now - moment).total_seconds()))


def _elapsed_seconds(started_ts: str | None, now: datetime) -> int | None:
    if started_ts is None:
        return None
    return _age_seconds(started_ts, now)


def build_view(
    config: DeckConfig,
    *,
    live_runs: Sequence[LiveRun],
    claims: Sequence[Claim],
    enrolled_labels: Mapping[str, str],
    last_heard: Mapping[str, str],
    outcomes: Sequence[LiveRun],
    failed_outcomes: Sequence[LiveRun],
    observers: Sequence[tuple[str, str]],
    now: datetime,
) -> DeckView:
    del now
    claims_by_target = {claim.target: claim for claim in claims}
    collision_targets = {
        target for target in {run.repo for run in live_runs} if collides(target, live_runs, claims_by_target)
    }
    stations: list[StationView] = []
    for station in config.stations:
        tiles = tuple(
            sorted(
                (
                    Tile(run=run, claim=claims_by_target.get(run.repo), collision=run.repo in collision_targets)
                    for run in live_runs
                    if run.node_id == station.node_id and run.state not in TERMINAL_STATES
                ),
                key=lambda tile: BUCKET_RANK.get(tile.run.bucket, len(BUCKET_RANK)),
            )
        )
        label = station.name or enrolled_labels.get(station.node_id, "") or station.node_id[:12]
        stations.append(
            StationView(
                station=station,
                label=label,
                enrolled=station.node_id in enrolled_labels,
                busy=len(tiles),
                last_heard=last_heard.get(station.node_id),
                tiles=tiles,
            )
        )
    rail: list[RailEntry] = [
        RailEntry(kind="failed", node_id=run.node_id, repo=run.repo, run_id=run.run_id, detail=run.state)
        for run in failed_outcomes
    ]
    rail.extend(
        RailEntry(kind=run.bucket, node_id=run.node_id, repo=run.repo, run_id=run.run_id, detail=run.state)
        for run in live_runs
        if run.bucket in ("awaiting approval", "stale")
    )
    rail.extend(
        RailEntry(
            kind="collision",
            node_id=claim.owner_node,
            repo=target,
            run_id="",
            detail=f"held by {claim.owner_node}, live on {target}",
        )
        for target, claim in sorted(claims_by_target.items())
        if target in collision_targets
    )
    repos = tuple(
        sorted(
            (
                RepoRow(
                    target=target,
                    claim=claims_by_target.get(target),
                    live=tuple(run for run in live_runs if run.repo == target),
                    collision=target in collision_targets,
                )
                for target in {run.repo for run in live_runs} | set(claims_by_target)
            ),
            key=lambda row: (not row.collision, row.target),
        )
    )
    return DeckView(
        stations=tuple(stations),
        rail=tuple(rail),
        repos=repos,
        outcomes=tuple(outcomes),
        observers=tuple(observers),
    )


_STYLE = """
* { box-sizing: border-box; }
.deck { max-width: 100%; overflow-wrap: anywhere; }
.stations { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); }
.tile { min-width: 0; border: 1px solid currentcolor; padding: 0.5rem; }
.not-enrolled { opacity: 0.5; }
table { width: 100%; }
@media (max-width: 700px) {
  .deck { overflow-wrap: anywhere; }
  .stations { grid-template-columns: 1fr; }
  .tile { width: 100%; }
}
"""

_SCRIPT = (
    "function tickElapsed() {"
    "document.querySelectorAll('[data-elapsed]').forEach(function (el) {"
    "var value = parseInt(el.getAttribute('data-elapsed'), 10);"
    "if (!isNaN(value)) { el.setAttribute('data-elapsed', String(value + 10)); }"
    "});}"
    "setInterval(tickElapsed, 10000);"
)


def render_deck(view: DeckView, *, nonce: str, now: datetime) -> str:
    total_capacity = sum(station.station.capacity for station in view.stations)
    total_busy = sum(station.busy for station in view.stations)
    verdict = f"NEEDS ATTENTION ({len(view.rail)})" if view.rail else "ALL CLEAR"
    parts: list[str] = [
        "<h1>Command Deck</h1>",
        f'<p class="verdict">{_esc(verdict)}</p>',
        f"<p>{total_busy}/{total_capacity} slots busy &middot; {_esc(_stamp(now))}</p>",
        '<nav><a href="/deck/repos">repos</a> <a href="/">classic boards</a></nav>',
    ]
    if not view.stations:
        parts.append("<p>No stations configured. Start the hub with --deck-config to add them.</p>")
    for index, station in enumerate(view.stations):
        enrol = "" if station.enrolled else " (not enrolled)"
        cls = "" if station.enrolled else ' class="not-enrolled"'
        tiles_html = "".join(_tile_html(tile) for tile in station.tiles)
        tiles_html = tiles_html or "<p>No active runs.</p>"
        heard = f"heard {heard_age(station.last_heard)} ago" if station.last_heard else "never heard"
        parts.append(
            f'<section aria-labelledby="station-{index}" class="stations"><h2 id="station-{index}"'
            f' title="{_esc(station.station.node_id)}"{cls}>{_esc(station.label)}{enrol}</h2>'
            f"<p>{station.busy}/{station.station.capacity} busy &middot; {_esc(heard)}</p>{tiles_html}</section>"
        )
    rail_items = "".join(
        f"<li>{_esc(entry.kind)} &middot; {_esc(entry.repo)} &middot; "
        f'{_esc(entry.node_id[:12])} &middot; <span title="{_esc(entry.run_id or entry.node_id)}">'
        f"{_esc((entry.run_id or entry.node_id)[:12])}</span> &middot; {_esc(entry.detail)}</li>"
        for entry in view.rail
    )
    parts.append(
        f'<section aria-labelledby="rail"><h2 id="rail">Needs you ({len(view.rail)})</h2>'
        + (f"<ul>{rail_items}</ul>" if rail_items else "<p>Nothing needs you.</p>")
        + "</section>"
    )
    timeline = "".join(
        "<li>"
        f"{_esc(run.state)} &middot; {_esc(run.repo)} &middot; {_esc(run.node_id[:12])} &middot; "
        f'<span title="{_esc(run.run_id)}">{_esc(run.run_id[:12])}</span></li>'
        for run in view.outcomes
    )
    parts.append(
        '<section aria-labelledby="timeline"><h2 id="timeline">Recent outcomes</h2>'
        + (f"<ul>{timeline}</ul>" if timeline else "<p>No recent outcomes.</p>")
        + "</section>"
    )
    observer_items = "".join(
        f'<li><span title="{_esc(node)}">{_esc(node[:12])}</span> &middot; heard {heard_age(received)} ago</li>'
        for node, received in view.observers
    )
    summary = f"Other observers ({len(view.observers)})"
    parts.append(f"<details><summary>{_esc(summary)}</summary><ul>{observer_items}</ul></details>")
    parts.append('<footer><a href="/view/machines">machines board</a> <a href="/view/repos">repos board</a></footer>')
    return _document("\n".join(parts), nonce=nonce, now=now)


def render_repos(view: DeckView, *, nonce: str, now: datetime) -> str:
    rows = "".join(
        "<tr>"
        f"<td>{_esc(row.target)}</td>"
        f"<td>{_esc(_owner_text(row.claim))}</td>"
        f"<td>{_esc(str(row.claim.ttl_remaining) + 's left') if row.claim else ''}</td>"
        f"<td>{_esc(', '.join(f'{run.node_id[:12]}:{run.state}' for run in row.live))}</td>"
        f"<td>{_esc('! collision') if row.collision else ''}</td>"
        "</tr>"
        for row in view.repos
    )
    table = (
        "<table><thead><tr><th>Target</th><th>Owner</th><th>TTL</th><th>Live now</th><th>Flags</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
        if rows
        else "<p>No claims or live repos.</p>"
    )
    body = (
        "<h1>Command Deck &middot; Repos</h1>"
        '<nav><a href="/deck">deck</a> <a href="/">classic boards</a></nav>'
        + table
        + '<footer><a href="/view/machines">machines board</a> <a href="/view/repos">repos board</a></footer>'
    )
    return _document(body, nonce=nonce, now=now)


def _tile_html(tile: Tile) -> str:
    claim_html = ""
    if tile.claim is not None:
        conductor = f" &middot; {_esc(tile.claim.owner_conductor)}" if tile.claim.owner_conductor else ""
        claim_html = (
            f'<p class="claim">claim: {_esc(tile.claim.owner_node[:12])}{conductor} &middot; '
            f"{tile.claim.ttl_remaining}s left</p>"
        )
    collision_html = '<p class="collision">! collision</p>' if tile.collision else ""
    elapsed = f"{tile.run.elapsed_seconds}s" if tile.run.elapsed_seconds is not None else "&mdash;"
    return (
        f'<article class="tile" data-elapsed="{_esc(str(tile.run.elapsed_seconds or 0))}">'
        f"<p>{_esc(tile.run.repo)}</p>"
        f"<p>{_esc(tile.run.seat)}/{_esc(tile.run.harness)}</p>"
        f"<p>{_esc(tile.run.bucket)}</p>"
        f'<p class="elapsed">{elapsed}</p>'
        f'<p class="run-id" title="{_esc(tile.run.run_id)}">{_esc(tile.run.run_id[:12])}</p>'
        + claim_html
        + collision_html
        + "</article>"
    )


def heard_age(received_at: str | None) -> str:
    if received_at is None:
        return "unknown"
    try:
        moment = datetime.fromisoformat(received_at)
    except ValueError:
        return "unknown"
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_UTC)
    seconds = max(0, int((datetime.now(_UTC) - moment).total_seconds()))
    return f"{seconds}s"


def _owner_text(claim: Claim | None) -> str:
    if claim is None:
        return ""
    owner = claim.owner_node[:12]
    if claim.owner_conductor:
        return f"{owner} · {claim.owner_conductor}"
    return owner


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M:%S UTC")


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def _document(body: str, *, nonce: str, now: datetime) -> str:
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta http-equiv="refresh" content="10">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>Command Deck</title>"
        f'<style nonce="{_esc(nonce)}">{_STYLE}</style>'
        f'<script nonce="{_esc(nonce)}">{_SCRIPT}</script>'
        f'</head><body class="deck" data-as-of="{_esc(_stamp(now))}">{body}</body></html>'
    )
