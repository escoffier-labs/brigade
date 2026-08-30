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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence

CLAIM_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")

TERMINAL_STATES = frozenset({"run.completed", "run.failed", "run.interrupted", "run.orphaned"})
AWAITING_STATES = frozenset({"run.paused", "approval.requested", "approval.held"})
TERMINAL_STATE_SUFFIXES = (
    ".completed",
    ".failed",
    ".interrupted",
    ".cancelled",
    ".canceled",
    ".cancel-acknowledged",
    ".timed_out",
    ".timeout",
    ".orphaned",
    ".expired",
)
FAILURE_STATE_SUFFIXES = (
    ".failed",
    ".cancelled",
    ".canceled",
    ".timed_out",
    ".timeout",
)
ACTIVE_LIMIT, OBSERVER_LIMIT, RAIL_FAILURE_LIMIT, SESSION_LIMIT = 200, 8, 10, 500
CLOUD_PROVIDER_LIMIT, CLOUD_LEASE_LIMIT = 8, 16
CLOUD_TEXT_LIMIT = 256
SESSION_REPO_IDENTITY_LIMIT, SESSION_CHECKOUT_PATH_LIMIT = 512, 1024
BUCKET_RANK = {"failed": 0, "awaiting approval": 1, "stale": 2, "running": 3, "queued": 4}
INTERNAL_OUTCOME_STATES = frozenset({"run.dispatch.completed", "run.synthesis.completed"})

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
class CloudProviderConfig:
    """Safe, startup-frozen admission defaults for one cloud provider."""

    limit: int
    enabled: bool = True
    hosted: bool = True


@dataclass(frozen=True)
class CloudConfig:
    """Hosted subscription pool limits, separate from physical stations."""

    global_limit: int
    providers: Mapping[str, CloudProviderConfig]


def default_cloud_config() -> CloudConfig:
    """Public fleet defaults. Private/operator-specific lanes stay absent."""
    return CloudConfig(
        global_limit=4,
        providers={
            "cursor": CloudProviderConfig(limit=3),
            "codex": CloudProviderConfig(limit=2),
            "claude": CloudProviderConfig(limit=0, enabled=False),
            "jules": CloudProviderConfig(limit=15),
            # Grok Bot is tracked but has no hosted-subscription admission cost.
            "grok-bot": CloudProviderConfig(limit=64, hosted=False),
        },
    )


@dataclass(frozen=True)
class DeckConfig:
    stations: Sequence[StationConfig] = ()
    stale_after_seconds: int = 1800
    stale_history_after_seconds: int = 86400
    outcome_window: int = 20
    failed_lookback_seconds: int = 86400
    cloud: CloudConfig = field(default_factory=default_cloud_config)


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
    repo_identity: str = ""


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
    interactive_overlap: bool = False
    repo_identity: str = ""


@dataclass(frozen=True)
class InteractiveSession:
    """One live interactive session. Separate from runs and station capacity."""

    node_id: str
    harness: str
    session_id: str
    repo_identity: str
    identity_scope: str
    repo_label: str
    checkout_path: str
    branch: str | None
    dirty_paths: tuple[str, ...]
    dirty_truncated: bool


@dataclass(frozen=True)
class CloudLease:
    """The active cloud-lease fields safe to project into the Command Deck."""

    provider_task_id: str
    repo: str
    label: str
    state: str
    artifact_ref: str


@dataclass(frozen=True)
class CloudWorker:
    """One bounded provider card, separate from physical station capacity."""

    provider: str
    used: int
    limit: int
    circuit_state: str
    leases: tuple[CloudLease, ...]


@dataclass(frozen=True)
class DeckView:
    stations: tuple[StationView, ...]
    rail: tuple[RailEntry, ...]
    repos: tuple[RepoRow, ...]
    outcomes: tuple[LiveRun, ...]
    observers: tuple[tuple[str, str], ...]
    cloud_workers: tuple[CloudWorker, ...] = ()
    interactive_sessions: tuple[InteractiveSession, ...] = ()


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
        stale_history_after_seconds=_bounded_int(raw, "stale_history_after_seconds", 86400, 3600, 604800),
        outcome_window=_bounded_int(raw, "outcome_window", 20, 1, 100),
        failed_lookback_seconds=_bounded_int(raw, "failed_lookback_seconds", 86400, 1, 2_592_000),
        cloud=_cloud_config(raw.get("cloud")),
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


def _cloud_config(raw: object) -> CloudConfig:
    defaults = default_cloud_config()
    if raw is None:
        return defaults
    if not isinstance(raw, Mapping):
        raise DeckConfigError("cloud must be a JSON object")
    global_limit = _bounded_int(raw, "global_limit", defaults.global_limit, 0, 64)
    configured = raw.get("providers", {})
    if not isinstance(configured, Mapping):
        raise DeckConfigError("cloud.providers must be a JSON object")
    providers = dict(defaults.providers)
    for name, item in configured.items():
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", name):
            raise DeckConfigError("cloud provider name is invalid")
        if not isinstance(item, Mapping):
            raise DeckConfigError("cloud provider must be a JSON object")
        previous = providers.get(name, CloudProviderConfig(limit=0, enabled=False))
        limit = _bounded_int(item, "limit", previous.limit, 0, 64)
        enabled = item.get("enabled", previous.enabled)
        hosted = item.get("hosted", previous.hosted)
        if type(enabled) is not bool or type(hosted) is not bool:
            raise DeckConfigError("cloud provider enabled and hosted must be booleans")
        providers[name] = CloudProviderConfig(limit=limit, enabled=enabled, hosted=hosted)
    return CloudConfig(global_limit=global_limit, providers=providers)


def _strip_controls(value: str) -> str:
    return _CONTROL_RE.sub("", value).strip()


def is_terminal_state(state: str) -> bool:
    return state in TERMINAL_STATES or state.endswith(TERMINAL_STATE_SUFFIXES)


def bucket_for(state: str, *, age_seconds: int | None, stale_after_seconds: int) -> str:
    # The Hub's synthetic history state is already an age verdict; bucket it
    # the same way ``fleet_dashboard.bucket_for`` does regardless of age.
    if state == "run.stale":
        return "stale"
    if state in AWAITING_STATES:
        return "awaiting approval"
    if state.endswith(FAILURE_STATE_SUFFIXES):
        return "failed"
    if age_seconds is not None and age_seconds > stale_after_seconds:
        return "stale"
    if state == "run.created":
        return "queued"
    return "running"


def collides(target: str, live_runs: Sequence[LiveRun], claims: Mapping[str, Claim]) -> bool:
    del claims
    nodes = {run.node_id for run in live_runs if run.repo == target}
    return len(nodes) >= 2


_LATEST_ROWS = (
    "SELECT node_id, run_id, repo, seat, harness, state, ts, received_at, repo_identity FROM ("
    "  SELECT e.*, ROW_NUMBER() OVER ("
    "    PARTITION BY node_id, run_id ORDER BY sequence DESC, received_at DESC, digest DESC"
    "  ) AS rn FROM events e"
    ") WHERE rn = 1"
)

_AWAITING_PLACEHOLDERS = ",".join("?" for _ in AWAITING_STATES)
_TERMINAL_PLACEHOLDERS = ",".join("?" for _ in TERMINAL_STATES)
_NOT_TERMINAL_SQL = f"state NOT IN ({_TERMINAL_PLACEHOLDERS})" + "".join(
    f" AND state NOT LIKE '%{suffix}'" for suffix in TERMINAL_STATE_SUFFIXES
)
# Self-parenthesized: callers append further AND predicates, and an unwrapped
# OR chain would bind them to the final LIKE term only.
_IS_TERMINAL_SQL = (
    f"(state IN ({_TERMINAL_PLACEHOLDERS})"
    + "".join(f" OR state LIKE '%{suffix}'" for suffix in TERMINAL_STATE_SUFFIXES)
    + ")"
)
_FAILURE_MATCH_SQL = " OR ".join(f"state LIKE '%{suffix}'" for suffix in FAILURE_STATE_SUFFIXES)
_INTERNAL_OUTCOME_PARAMS = tuple(sorted(INTERNAL_OUTCOME_STATES))
_INTERNAL_OUTCOME_PLACEHOLDERS = ",".join("?" for _ in _INTERNAL_OUTCOME_PARAMS)
_REPO_IDENTITY_SQL = "COALESCE(NULLIF(repo_identity, ''), NULLIF(repo, ''))"


def fetch_live_runs(conn: sqlite3.Connection, *, now: datetime, stale_after_seconds: int) -> list[LiveRun]:
    rank_sql = (
        "CASE WHEN state LIKE '%failed' THEN 0"
        + "".join(f" WHEN state LIKE '%{suffix}' THEN 0" for suffix in FAILURE_STATE_SUFFIXES[1:])
        + f" WHEN state IN ({_AWAITING_PLACEHOLDERS}) THEN 1 ELSE 3 END"
    )
    cutoff = datetime.fromtimestamp(now.timestamp() - stale_after_seconds, tz=now.tzinfo or _UTC).isoformat()
    query = (
        f"{_LATEST_ROWS} AND {_NOT_TERMINAL_SQL} AND received_at >= ?"
        f" ORDER BY {rank_sql}, ts DESC, node_id, run_id LIMIT {ACTIVE_LIMIT}"
    )
    rows = conn.execute(query, (*TERMINAL_STATES, cutoff, *AWAITING_STATES)).fetchall()
    started = fetch_started_at(conn, [(row[0], row[1]) for row in rows])
    runs: list[LiveRun] = []
    for node_id, run_id, repo, seat, harness, state, _ts, received_at, repo_identity in rows:
        age = _age_seconds(received_at, now)
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
                repo_identity=repo_identity or "",
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
    """Terminal rows an operator should read, minus Brigade's internal milestones."""
    rows = conn.execute(
        f"{_LATEST_ROWS} AND {_IS_TERMINAL_SQL}"
        f" AND state NOT IN ({_INTERNAL_OUTCOME_PLACEHOLDERS}) ORDER BY ts DESC LIMIT ?",
        (*TERMINAL_STATES, *_INTERNAL_OUTCOME_PARAMS, int(outcome_window)),
    ).fetchall()
    return [_live_run_from_row(row) for row in rows]


def fetch_failed_outcomes(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    lookback_seconds: int,
    stale_after_seconds: int = 1800,
) -> list[LiveRun]:
    cutoff = datetime.fromtimestamp(now.timestamp() - lookback_seconds, tz=now.tzinfo or _UTC).isoformat()
    stale_cutoff = datetime.fromtimestamp(now.timestamp() - stale_after_seconds, tz=now.tzinfo or _UTC).isoformat()
    rows = conn.execute(
        "WITH latest AS (" + _LATEST_ROWS + "), failures AS ("
        "SELECT node_id, run_id, repo, seat, harness, state, ts, received_at, repo_identity, "
        f"{_REPO_IDENTITY_SQL} AS identity, ROW_NUMBER() OVER ("
        f"PARTITION BY node_id, {_REPO_IDENTITY_SQL} ORDER BY received_at DESC, run_id DESC"
        ") AS attention_rank FROM latest "
        f"WHERE ({_FAILURE_MATCH_SQL}) AND received_at >= ?"
        "), successors AS ("
        f"SELECT node_id, {_REPO_IDENTITY_SQL} AS identity, received_at FROM latest "
        f"WHERE ({_NOT_TERMINAL_SQL} AND received_at >= ?)"
        f" OR (state LIKE '%.completed' AND state NOT IN ({_INTERNAL_OUTCOME_PLACEHOLDERS}))"
        ") SELECT node_id, run_id, repo, seat, harness, state, ts, received_at, repo_identity "
        "FROM failures AS failure "
        "WHERE attention_rank = 1 AND NOT EXISTS ("
        "SELECT 1 FROM successors WHERE successors.node_id = failure.node_id "
        "AND failure.identity IS NOT NULL AND successors.identity IS NOT NULL "
        "AND successors.identity = failure.identity AND successors.received_at > failure.received_at"
        f") ORDER BY received_at DESC, run_id DESC LIMIT {RAIL_FAILURE_LIMIT}",
        (cutoff, *TERMINAL_STATES, stale_cutoff, *_INTERNAL_OUTCOME_PARAMS),
    ).fetchall()
    return [_live_run_from_row(row, bucket="failed") for row in rows]


def cloud_workers_from_snapshot(snapshot: Mapping[str, object]) -> tuple[CloudWorker, ...]:
    """Build bounded, safe cloud cards from the hub's request-local snapshot."""
    policy = snapshot.get("policy")
    if not isinstance(policy, Mapping):
        return ()
    provider_rows = policy.get("providers")
    lease_rows = snapshot.get("leases")
    if not isinstance(provider_rows, Sequence) or isinstance(provider_rows, (str, bytes)):
        return ()
    if not isinstance(lease_rows, Sequence) or isinstance(lease_rows, (str, bytes)):
        lease_rows = ()
    leases_by_provider: dict[str, list[CloudLease]] = {}
    for raw in lease_rows:
        if not isinstance(raw, Mapping) or raw.get("released_at") is not None or raw.get("expired") is True:
            continue
        provider = _safe_display_text(raw.get("provider"))
        if not provider:
            continue
        leases_by_provider.setdefault(provider, []).append(
            CloudLease(
                provider_task_id=_safe_display_text(raw.get("provider_task_id")),
                repo=_safe_display_text(raw.get("repo")),
                label=_safe_display_text(raw.get("label")),
                state=_safe_display_text(raw.get("state")),
                artifact_ref=_safe_display_text(raw.get("artifact_ref")),
            )
        )
    workers: list[CloudWorker] = []
    for raw in provider_rows:
        if not isinstance(raw, Mapping):
            continue
        provider = _safe_display_text(raw.get("provider"))
        if not provider:
            continue
        used = raw.get("used")
        limit = raw.get("limit")
        if type(used) is not int or type(limit) is not int or used < 0 or limit < 0:
            continue
        workers.append(
            CloudWorker(
                provider=provider,
                used=used,
                limit=limit,
                circuit_state=_safe_display_text(raw.get("circuit_state")) or "unknown",
                leases=tuple(leases_by_provider.get(provider, ())[:CLOUD_LEASE_LIMIT]),
            )
        )
    return tuple(sorted(workers, key=lambda worker: worker.provider)[:CLOUD_PROVIDER_LIMIT])


def _safe_display_text(value: object, *, limit: int = CLOUD_TEXT_LIMIT) -> str:
    return _strip_controls(value)[:limit] if isinstance(value, str) else ""


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


def fetch_interactive_sessions(conn: sqlite3.Connection, *, now: datetime) -> list[dict[str, object]]:
    """Active unexpired sessions via the Hub session authority. Capped separately."""
    from . import fleet_hub_sessions

    return fleet_hub_sessions.list_sessions(conn, now_epoch=now.timestamp())[:SESSION_LIMIT]


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
        repo_identity=(row[8] or "") if len(row) > 8 else "",
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


def _node_label(node_id: str, enrolled_labels: Mapping[str, str]) -> str:
    return _strip_controls(enrolled_labels.get(node_id, "")) or node_id[:12]


def _display_claim(claim: Claim | None, enrolled_labels: Mapping[str, str]) -> Claim | None:
    if claim is None:
        return None
    return Claim(
        target=claim.target,
        owner_node=_node_label(claim.owner_node, enrolled_labels),
        owner_conductor=claim.owner_conductor,
        ttl_remaining=claim.ttl_remaining,
    )


def _interactive_session(raw: Mapping[str, object]) -> InteractiveSession:
    dirty = raw.get("dirty_paths")
    paths = tuple(item for item in dirty if isinstance(item, str)) if isinstance(dirty, (list, tuple)) else ()
    branch = raw.get("branch")
    return InteractiveSession(
        node_id=_safe_display_text(raw.get("node_id")),
        harness=_safe_display_text(raw.get("harness")),
        session_id=_safe_display_text(raw.get("session_id")),
        repo_identity=_safe_display_text(raw.get("repo_identity"), limit=SESSION_REPO_IDENTITY_LIMIT),
        identity_scope=_safe_display_text(raw.get("identity_scope")) or "fleet",
        repo_label=_safe_display_text(raw.get("repo_label")),
        checkout_path=_safe_display_text(raw.get("checkout_path"), limit=SESSION_CHECKOUT_PATH_LIMIT),
        branch=_safe_display_text(branch) if isinstance(branch, str) else None,
        dirty_paths=paths,
        dirty_truncated=bool(raw.get("dirty_truncated")),
    )


def _session_dirty_paths(raw: Mapping[str, object]) -> set[str]:
    from .fleet_session_presence import _normalize_overlap_path, _row_dirty_paths

    paths = {_normalize_overlap_path(path) for path in _row_dirty_paths(raw.get("dirty_paths"))}
    paths.discard("")
    return paths


def _session_row_label(session: InteractiveSession) -> str:
    label = session.repo_label.strip() if session.repo_label else ""
    if label:
        return label[:256]
    identity = session.repo_identity
    if "/" in identity:
        identity = identity.rsplit("/", 1)[-1]
    return (identity or "session")[:256]


def _session_identity_display_targets(
    active_runs: Sequence[LiveRun],
    claims_by_target: Mapping[str, Claim],
    session_rows: Sequence[InteractiveSession],
) -> dict[str, set[str]]:
    """Map canonical repo identity to existing run/claim display targets or a label."""
    mapped: dict[str, set[str]] = {}
    occupied = {run.repo for run in active_runs if run.repo} | set(claims_by_target)
    for run in active_runs:
        if run.repo_identity and run.repo:
            mapped.setdefault(run.repo_identity, set()).add(run.repo)
    for session in session_rows:
        if not session.repo_identity:
            continue
        targets = mapped.setdefault(session.repo_identity, set())
        if targets:
            continue
        if session.repo_identity in claims_by_target:
            targets.add(session.repo_identity)
            continue
        label = _session_row_label(session)
        if label in occupied:
            label = _identity_bearing_session_label(session, occupied)
        occupied.add(label)
        targets.add(label)
    return mapped


def _identity_bearing_session_label(session: InteractiveSession, occupied: set[str]) -> str:
    """Render a session-only repo row that cannot collide with a run/claim label."""
    identity = session.repo_identity.strip()
    label = _session_row_label(session)
    candidate = f"{label} ({identity})" if identity and identity != label else identity or label
    candidate = candidate[:256]
    if candidate not in occupied:
        return candidate
    bounded_identity = identity[:256]
    if bounded_identity and bounded_identity not in occupied:
        return bounded_identity
    suffix = 2
    while True:
        suffix_text = f" #{suffix}"
        extra = f"{candidate[: 256 - len(suffix_text)]}{suffix_text}"
        if extra not in occupied:
            return extra
        suffix += 1


def _repo_row_has_exact_overlap(
    target: str,
    *,
    active_runs: Sequence[LiveRun],
    overlap_identities: set[str],
    row_identity: str,
) -> bool:
    """Mark a run row only from exact ``run.repo_identity``; session-only by identity."""
    live = tuple(run for run in active_runs if run.repo == target)
    if live:
        return any(run.repo_identity and run.repo_identity in overlap_identities for run in live)
    return bool(row_identity) and row_identity in overlap_identities


def _exact_path_overlap_targets(sessions: Sequence[Mapping[str, object]]) -> set[str]:
    """Repos whose live sessions share at least one exact normalized dirty path."""
    groups: dict[tuple[str, str, str], list[Mapping[str, object]]] = {}
    for session in sessions:
        identity = str(session.get("repo_identity") or "")
        if not identity:
            continue
        scope = str(session.get("identity_scope") or "fleet")
        node_id = str(session.get("node_id") or "")
        groups.setdefault((identity, scope, node_id if scope == "node" else ""), []).append(session)
    overlapped: set[str] = set()
    for (identity, _scope, _node), rows in groups.items():
        unique: list[Mapping[str, object]] = []
        seen: set[tuple[str, str, str]] = set()
        for row in rows:
            key = (str(row.get("node_id") or ""), str(row.get("harness") or ""), str(row.get("session_id") or ""))
            if key in seen:
                continue
            seen.add(key)
            unique.append(row)
        for index, left in enumerate(unique):
            left_paths = _session_dirty_paths(left)
            if not left_paths:
                continue
            if any(left_paths & _session_dirty_paths(right) for right in unique[index + 1 :]):
                overlapped.add(identity)
                break
    return overlapped


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
    cloud_workers: Sequence[CloudWorker] = (),
    interactive_sessions: Sequence[Mapping[str, object]] = (),
) -> DeckView:
    del now
    active_runs = tuple(run for run in live_runs if not is_terminal_state(run.state) and run.bucket != "stale")
    claims_by_target = {claim.target: claim for claim in claims}
    display_claims = {target: _display_claim(claim, enrolled_labels) for target, claim in claims_by_target.items()}
    collision_targets = {
        target for target in {run.repo for run in active_runs} if collides(target, active_runs, claims_by_target)
    }
    session_rows = tuple(_interactive_session(item) for item in interactive_sessions)
    overlap_targets = _exact_path_overlap_targets(interactive_sessions)
    stations: list[StationView] = []
    for station in config.stations:
        tiles = tuple(
            sorted(
                (
                    Tile(run=run, claim=display_claims.get(run.repo), collision=run.repo in collision_targets)
                    for run in active_runs
                    if run.node_id == station.node_id
                ),
                key=lambda tile: BUCKET_RANK.get(tile.run.bucket, len(BUCKET_RANK)),
            )
        )
        label = station.name or _node_label(station.node_id, enrolled_labels)
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
            node_id=_node_label(claim.owner_node, enrolled_labels),
            repo=target,
            run_id="",
            detail=f"held by {_node_label(claim.owner_node, enrolled_labels)}, live on {target}",
        )
        for target, claim in sorted(claims_by_target.items())
        if target in collision_targets
    )
    identity_to_display = _session_identity_display_targets(active_runs, claims_by_target, session_rows)
    run_identities = {run.repo_identity for run in active_runs if run.repo_identity}
    repo_targets = {run.repo for run in active_runs} | set(claims_by_target)
    session_only_targets = {
        target
        for identity, targets in identity_to_display.items()
        if identity not in run_identities
        for target in targets
        if target not in repo_targets
    }
    repo_targets |= session_only_targets
    identity_for_target: dict[str, str] = {}
    for identity, targets in identity_to_display.items():
        for target in targets:
            identity_for_target.setdefault(target, identity)
    for run in active_runs:
        if run.repo and run.repo_identity:
            identity_for_target.setdefault(run.repo, run.repo_identity)
    repos = tuple(
        sorted(
            (
                RepoRow(
                    target=target,
                    claim=display_claims.get(target),
                    live=tuple(run for run in active_runs if run.repo == target),
                    collision=target in collision_targets,
                    interactive_overlap=_repo_row_has_exact_overlap(
                        target,
                        active_runs=active_runs,
                        overlap_identities=overlap_targets,
                        row_identity=identity_for_target.get(target, ""),
                    ),
                    repo_identity=identity_for_target.get(target, ""),
                )
                for target in repo_targets
            ),
            key=lambda row: (not row.collision, not row.interactive_overlap, row.target),
        )
    )
    return DeckView(
        stations=tuple(stations),
        rail=tuple(rail),
        repos=repos,
        outcomes=tuple(outcomes),
        observers=tuple(observers),
        cloud_workers=tuple(cloud_workers[:CLOUD_PROVIDER_LIMIT]),
        interactive_sessions=session_rows,
    )


_STYLE = """
:root {
  color-scheme: dark;
  --canvas: #111617;
  --surface: #182022;
  --surface-raised: #202a2c;
  --line: #3a4849;
  --line-quiet: #293536;
  --ink: #e5ece8;
  --muted: #a4b1ad;
  --faint: #75837f;
  --signal: #d5ab69;
  --signal-quiet: #382f22;
}
* { box-sizing: border-box; }
body.deck {
  margin: 0;
  min-width: 0;
  background: var(--canvas);
  color: var(--ink);
  font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  font-size: 14px;
  line-height: 1.45;
}
.deck { max-width: 100%; overflow-wrap: anywhere; }
.deck-shell { width: min(1480px, 100%); margin: 0 auto; padding: 18px; }
.masthead {
  display: flex;
  align-items: end;
  justify-content: space-between;
  gap: 16px;
  padding: 0 0 14px;
  border-bottom: 1px solid var(--line);
}
.eyebrow { margin: 0 0 3px; color: var(--faint); font-size: 11px; font-weight: 700; letter-spacing: .11em; text-transform: uppercase; }
h1, h2, h3, p { margin-top: 0; }
h1 { margin-bottom: 3px; font-size: 22px; line-height: 1.1; letter-spacing: -.02em; }
h2 { margin-bottom: 0; font-size: 15px; line-height: 1.25; }
h3 { margin-bottom: 0; font-size: 13px; }
.header-meta { margin: 0; color: var(--muted); font-variant-numeric: tabular-nums; text-align: right; }
.verdict { color: var(--signal); font-size: 12px; font-weight: 800; letter-spacing: .08em; }
nav { display: flex; flex-wrap: wrap; gap: 8px; margin: 14px 0; }
a { color: var(--ink); text-underline-offset: 3px; }
nav a { border: 1px solid var(--line); padding: 4px 8px; color: var(--muted); text-decoration: none; }
nav a:hover, nav a:focus-visible { border-color: var(--signal); color: var(--ink); outline: none; }
.stations { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); }
.stations { gap: 12px; align-items: start; }
.station-card, .panel, .repo-panel, details {
  min-width: 0;
  border: 1px solid var(--line-quiet);
  background: var(--surface);
}
.station-card { padding: 12px; }
.station-card > header { display: flex; align-items: start; justify-content: space-between; gap: 10px; padding-bottom: 10px; border-bottom: 1px solid var(--line-quiet); }
.station-meta { margin: 3px 0 0; color: var(--muted); font-size: 12px; }
.capacity { color: var(--signal); font-size: 12px; font-weight: 700; font-variant-numeric: tabular-nums; text-align: right; white-space: nowrap; }
.not-enrolled { color: var(--faint); }
.run-stack { display: grid; gap: 8px; margin-top: 10px; }
.empty { margin: 0; padding: 10px; color: var(--muted); background: #141b1c; font-size: 12px; }
.tile { min-width: 0; }
.tile { padding: 10px; border: 1px solid var(--line); border-left: 3px solid var(--signal); background: var(--surface-raised); }
.tile--failed, .tile--awaiting-approval, .tile--stale { border-color: var(--signal); background: var(--signal-quiet); }
.tile-head { display: flex; align-items: start; justify-content: space-between; gap: 8px; }
.repo-name { margin: 0; font-weight: 750; }
.state { margin: 0; color: var(--signal); font-size: 11px; font-weight: 800; letter-spacing: .06em; text-align: right; text-transform: uppercase; }
.tile-facts { display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 2px 9px; margin: 9px 0 0; color: var(--muted); font-size: 12px; }
.tile-facts dt { color: var(--faint); }
.tile-facts dd { min-width: 0; margin: 0; }
.claim, .collision { margin: 9px 0 0; font-size: 12px; }
.collision { color: var(--signal); font-weight: 800; }
.dashboard-grid { display: grid; grid-template-columns: minmax(0, 2fr) minmax(280px, 1fr); gap: 12px; margin-top: 12px; }
.cloud-workers { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; margin-top: 12px; }
.cloud-worker-card { min-width: 0; padding: 12px; border: 1px solid var(--line-quiet); background: var(--surface); }
.cloud-worker-card > header { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; margin-bottom: 8px; }
.cloud-capacity { margin: 0; color: var(--signal); font-size: 12px; font-weight: 700; font-variant-numeric: tabular-nums; white-space: nowrap; }
.cloud-state { margin: 0 0 8px; color: var(--muted); font-size: 12px; }
.cloud-lease-list { display: grid; gap: 7px; margin: 0; padding: 0; list-style: none; }
.cloud-lease-list li { padding-top: 7px; border-top: 1px solid var(--line-quiet); color: var(--muted); font-size: 12px; }
.panel, .repo-panel { padding: 12px; }
.panel > header, .repo-panel > header { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; margin-bottom: 10px; }
.panel-count { margin: 0; color: var(--faint); font-size: 12px; font-variant-numeric: tabular-nums; }
.attention-panel { border-color: #6d5635; }
.attention-list, .timeline, .observer-list { margin: 0; padding: 0; list-style: none; }
.attention-list { display: grid; gap: 7px; }
.attention-list li { padding: 8px 0; border-top: 1px solid var(--line-quiet); color: var(--muted); }
.attention-list li:first-child { border-top: 0; padding-top: 0; }
.attention-kind { color: var(--signal); font-size: 11px; font-weight: 800; letter-spacing: .06em; text-transform: uppercase; }
.timeline { display: grid; gap: 0; }
.timeline li { display: grid; grid-template-columns: 140px minmax(0, 1fr) auto; gap: 10px; padding: 8px 0; border-top: 1px solid var(--line-quiet); color: var(--muted); font-size: 12px; }
.timeline li:first-child { border-top: 0; padding-top: 0; }
.timeline-state { color: var(--ink); font-weight: 700; }
details { margin-top: 12px; padding: 10px 12px; color: var(--muted); }
summary { cursor: pointer; color: var(--ink); font-weight: 700; }
.observer-list { display: grid; gap: 6px; margin-top: 10px; font-size: 12px; }
.table-wrap { overflow: hidden; }
table { width: 100%; table-layout: fixed; }
th, td { padding: 8px; border-bottom: 1px solid var(--line-quiet); overflow-wrap: anywhere; text-align: left; vertical-align: top; }
th { color: var(--faint); font-size: 11px; letter-spacing: .06em; text-transform: uppercase; }
td { color: var(--muted); font-size: 12px; }
th:nth-child(1) { width: 22%; } th:nth-child(2) { width: 20%; } th:nth-child(3) { width: 13%; } th:nth-child(4) { width: 34%; } th:nth-child(5) { width: 11%; }
.flag { color: var(--signal); font-weight: 800; }
footer { margin-top: 18px; color: var(--faint); font-size: 12px; }
footer a { margin-right: 12px; color: var(--muted); }
@media (max-width: 700px) {
  .stations { grid-template-columns: 1fr; }
  .tile { width: 100%; }
  .deck-shell { padding: 12px; }
  .masthead, .station-card > header { align-items: start; flex-direction: column; }
  .header-meta, .capacity { text-align: left; }
  .dashboard-grid { grid-template-columns: 1fr; }
  .timeline li { grid-template-columns: 1fr; gap: 2px; }
  th, td { padding: 7px 4px; font-size: 11px; }
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
        '<main class="deck-shell">',
        '<header class="masthead"><div><p class="eyebrow">Fleet operations</p><h1>Command Deck</h1>',
        f'<p class="verdict">{_esc(verdict)}</p></div><p class="header-meta">'
        f"{total_busy}/{total_capacity} slots busy<br>{_esc(_stamp(now))}</p></header>",
        '<nav aria-label="Command Deck"><a href="/">deck</a> <a href="/deck/repos">repos</a> <a href="/view/machines">machines board</a></nav>',
    ]
    if not view.stations:
        parts.append(
            '<section class="panel"><p class="empty">No stations configured. Start the hub with --deck-config to add them.</p></section>'
        )
    else:
        station_cards: list[str] = []
        for index, station in enumerate(view.stations):
            enrol = "" if station.enrolled else " (not enrolled)"
            cls = "" if station.enrolled else ' class="not-enrolled"'
            tiles_html = "".join(_tile_html(tile) for tile in station.tiles)
            tiles_html = tiles_html or '<p class="empty">No active runs.</p>'
            heard = f"heard {heard_age(station.last_heard)} ago" if station.last_heard else "never heard"
            station_cards.append(
                f'<section aria-labelledby="station-{index}" class="station-card"><header><div><h2 id="station-{index}"'
                f' title="{_esc(station.station.node_id)}"{cls}>{_esc(station.label)}{enrol}</h2>'
                f'<p class="station-meta">{_esc(heard)}</p></div><p class="capacity">'
                f'{station.busy}/{station.station.capacity} busy</p></header><div class="run-stack">{tiles_html}</div></section>'
            )
        parts.append('<section class="stations" aria-label="Fleet stations">' + "".join(station_cards) + "</section>")
    cloud_cards = "".join(_cloud_worker_html(worker) for worker in view.cloud_workers)
    parts.append(
        '<section class="panel" aria-labelledby="cloud-workers"><header><h2 id="cloud-workers">Cloud workers</h2>'
        f'<p class="panel-count">{len(view.cloud_workers)} provider(s)</p></header>'
        + (
            f'<div class="cloud-workers">{cloud_cards}</div>'
            if cloud_cards
            else '<p class="empty">No cloud providers configured.</p>'
        )
        + "</section>"
    )
    session_items = "".join(_interactive_session_html(session) for session in view.interactive_sessions)
    parts.append(
        '<section class="panel" aria-labelledby="interactive-sessions"><header>'
        '<h2 id="interactive-sessions">Interactive sessions</h2>'
        f'<p class="panel-count">{len(view.interactive_sessions)} session(s)</p></header>'
        + (
            f'<ul class="attention-list">{session_items}</ul>'
            if session_items
            else '<p class="empty">No interactive sessions.</p>'
        )
        + "</section>"
    )
    rail_items = "".join(
        f'<li><span class="attention-kind">{_esc(entry.kind)}</span> &middot; {_esc(entry.repo)} &middot; '
        f'{_esc(entry.node_id[:12])} &middot; <span title="{_esc(entry.run_id or entry.node_id)}">'
        f"{_esc((entry.run_id or entry.node_id)[:12])}</span> &middot; {_esc(entry.detail)}</li>"
        for entry in view.rail
    )
    timeline = "".join(
        '<li><span class="timeline-state">'
        f"{_esc(run.state)}</span><span>{_esc(run.repo)} &middot; {_esc(run.node_id[:12])}</span>"
        f'<span title="{_esc(run.run_id)}">{_esc(run.run_id[:12])}</span></li>'
        for run in view.outcomes
    )
    parts.append(
        '<div class="dashboard-grid"><section class="panel attention-panel" aria-labelledby="rail"><header><h2 id="rail">Needs you</h2>'
        f'<p class="panel-count">{len(view.rail)} item(s)</p></header>'
        + (f'<ul class="attention-list">{rail_items}</ul>' if rail_items else '<p class="empty">Nothing needs you.</p>')
        + '</section><section class="panel" aria-labelledby="timeline"><header><h2 id="timeline">Recent outcomes</h2>'
        f'<p class="panel-count">{len(view.outcomes)} shown</p></header>'
        + (f'<ul class="timeline">{timeline}</ul>' if timeline else '<p class="empty">No recent outcomes.</p>')
        + "</section></div>"
    )
    observer_items = "".join(
        f'<li><span title="{_esc(node)}">{_esc(node[:12])}</span> &middot; heard {heard_age(received)} ago</li>'
        for node, received in view.observers
    )
    summary = f"Other observers ({len(view.observers)})"
    parts.append(
        f'<details><summary>{_esc(summary)}</summary><ul class="observer-list">{observer_items}</ul></details>'
    )
    parts.append(
        '<footer><a href="/view/machines">machines board</a> <a href="/view/repos">repos board</a></footer></main>'
    )
    return _document("\n".join(parts), nonce=nonce, now=now)


def render_repos(view: DeckView, *, nonce: str, now: datetime) -> str:
    rows = "".join(
        "<tr>"
        f"<td>{_esc(row.target)}</td>"
        f"<td>{_esc(_owner_text(row.claim))}</td>"
        f"<td>{_esc(str(row.claim.ttl_remaining) + 's left') if row.claim else ''}</td>"
        f"<td>{_esc(', '.join(f'{run.node_id[:12]}:{run.state}' for run in row.live))}</td>"
        f'<td class="flag">{_esc(_repo_flags(row))}</td>'
        "</tr>"
        for row in view.repos
    )
    table = (
        '<div class="table-wrap"><table><thead><tr><th>Target</th><th>Owner</th><th>TTL</th><th>Live now</th><th>Flags</th></tr></thead>'
        f"<tbody>{rows}</tbody></table></div>"
        if rows
        else "<p>No claims or live repos.</p>"
    )
    body = (
        '<main class="deck-shell"><header class="masthead"><div><p class="eyebrow">Fleet operations</p>'
        "<h1>Command Deck &middot; Repos</h1></div>"
        f'<p class="header-meta">{_esc(_stamp(now))}</p></header>'
        '<nav aria-label="Command Deck"><a href="/">deck</a> <a href="/view/machines">machines board</a></nav>'
        '<section class="repo-panel" aria-label="Repository coordination">'
        + table
        + '</section><footer><a href="/view/machines">machines board</a> <a href="/view/repos">repos board</a></footer></main>'
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
    tile_class = tile.run.bucket.replace(" ", "-")
    return (
        f'<article class="tile tile--{_esc(tile_class)}" data-elapsed="{_esc(str(tile.run.elapsed_seconds or 0))}">'
        f'<header class="tile-head"><p class="repo-name">{_esc(tile.run.repo)}</p>'
        f'<p class="state">{_esc(tile.run.bucket)}</p></header><dl class="tile-facts">'
        f"<dt>seat</dt><dd>{_esc(tile.run.seat)}/{_esc(tile.run.harness)}</dd>"
        f'<dt>elapsed</dt><dd class="elapsed">{elapsed}</dd>'
        f'<dt>run</dt><dd class="run-id" title="{_esc(tile.run.run_id)}">{_esc(tile.run.run_id[:12])}</dd></dl>'
        + claim_html
        + collision_html
        + "</article>"
    )


def _interactive_session_html(session: InteractiveSession) -> str:
    dirty = f"{len(session.dirty_paths)}+" if session.dirty_truncated else str(len(session.dirty_paths))
    branch = session.branch or "-"
    return (
        "<li>"
        f"{_esc(session.repo_label or session.repo_identity)} &middot; "
        f"{_esc(session.harness)}/{_esc(session.session_id)} &middot; "
        f"{_esc(session.checkout_path)} &middot; "
        f"{_esc(branch)} &middot; {_esc(dirty)} dirty"
        "</li>"
    )


def _repo_flags(row: RepoRow) -> str:
    flags: list[str] = []
    if row.collision:
        flags.append("! collision")
    if row.interactive_overlap:
        flags.append("! overlap")
    return " ".join(flags)


def _cloud_worker_html(worker: CloudWorker) -> str:
    lease_items = "".join(
        "<li>"
        f"<strong>{_esc(lease.label or lease.provider_task_id or 'active lease')}</strong><br>"
        f"{_esc(lease.repo)} &middot; {_esc(lease.state)}"
        + (f'<br><span title="task id">{_esc(lease.provider_task_id)}</span>' if lease.provider_task_id else "")
        + (f'<br><span title="artifact">{_esc(lease.artifact_ref)}</span>' if lease.artifact_ref else "")
        + "</li>"
        for lease in worker.leases
    )
    return (
        '<article class="cloud-worker-card"><header>'
        f'<h3>{_esc(worker.provider)}</h3><p class="cloud-capacity">{worker.used}/{worker.limit}</p>'
        f'</header><p class="cloud-state">circuit: {_esc(worker.circuit_state)}</p>'
        + (
            f'<ul class="cloud-lease-list">{lease_items}</ul>'
            if lease_items
            else '<p class="empty">No active leases.</p>'
        )
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
        '<meta name="theme-color" content="#111617">'
        '<meta name="application-name" content="Fleet Hub">'
        '<meta name="apple-mobile-web-app-title" content="Fleet Hub">'
        '<link rel="icon" type="image/x-icon" href="/favicon.ico">'
        '<link rel="icon" type="image/png" sizes="32x32" href="/favicon-32x32.png">'
        '<link rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png">'
        '<link rel="manifest" href="/site.webmanifest">'
        "<title>Command Deck</title>"
        f'<style nonce="{_esc(nonce)}">{_STYLE}</style>'
        f'<script nonce="{_esc(nonce)}">{_SCRIPT}</script>'
        f'</head><body class="deck" data-as-of="{_esc(_stamp(now))}">{body}</body></html>'
    )
