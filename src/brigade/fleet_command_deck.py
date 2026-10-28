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

from brigade.fleet_deck_brands import (
    Brand,
    harness_brand,
    provider_brand,
)

from .fleet_event_freshness import observation_state
from . import fleet_deck_shell, ui_theme

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
CLOUD_REPORT_HISTORY_LIMIT = 50
CLOUD_TEXT_LIMIT = 256
SESSION_REPO_IDENTITY_LIMIT, SESSION_CHECKOUT_PATH_LIMIT = 512, 1024
BUCKET_RANK = {"failed": 0, "awaiting approval": 1, "unknown": 2, "stale": 2, "running": 3, "queued": 4}
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
class ObservedWorkGroup:
    """Operator-configured retained observations, independent of fleet admission."""

    key: str
    label: str
    coverage: str
    source_ref: str
    proxy_ref: str
    parent_ref: str
    work_ids: tuple[str, ...]
    snapshot_observed_at: datetime | None = None


def snapshot_freshness(snapshot: datetime | None, *, now: datetime, stale_after_seconds: int) -> tuple[str, str]:
    """Return the explicit snapshot's stamp and freshness, independent of record updates."""
    if snapshot is None:
        return "unknown (no configured snapshot observation time)", "unknown"
    age = (now - snapshot).total_seconds()
    freshness = "future" if age < 0 else "stale" if age > stale_after_seconds else "recent"
    return snapshot.isoformat(), freshness


@dataclass(frozen=True)
class DeckConfig:
    stations: Sequence[StationConfig] = ()
    stale_after_seconds: int = 1800
    stale_history_after_seconds: int = 86400
    outcome_window: int = 20
    failed_lookback_seconds: int = 86400
    cloud: CloudConfig = field(default_factory=default_cloud_config)
    observed_work_groups: tuple[ObservedWorkGroup, ...] = ()


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
    provider: str = ""
    model: str = ""
    launch_model: str = ""


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
    age_seconds: int | None = None


@dataclass(frozen=True)
class RepoRow:
    target: str
    claim: Claim | None
    live: tuple[LiveRun, ...]
    collision: bool
    interactive_overlap: bool = False
    repo_identity: str = ""


@dataclass(frozen=True)
class CloudSessionContext:
    """Validated reported presence, never a provider job or lease."""

    version: int
    provider: str
    agent_label: str
    sequence: int
    observed_at: str
    source: str
    source_scope: str
    coverage: str
    parent_session_id: str = ""
    work_id: str = ""
    task: str = ""
    reported_state: str = "unknown"
    progress: str = ""
    blocker: str = ""
    result: str = ""
    evidence_refs: tuple[str, ...] = ()


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
    cloud_context: CloudSessionContext | None = None
    presence_state: str = "active"
    observation_age_seconds: int | None = None


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


UNKNOWN = "unknown"
CONTROL_PLANE_SECTIONS = ("policy", "sessions", "machines", "quota", "routes", "queue", "integrations")


# SVG geometry is trusted constant data; labels and titles are always escaped.
def _badge_html(brand: Brand, raw_name: str) -> str:
    title = raw_name.strip() or UNKNOWN
    return (
        f'<span class="badge badge-icon" role="img" '
        f'title="{_esc(title)}" aria-label="{_esc(brand.label)}">' + brand.svg + "</span>"
    )


def harness_badge_html(harness: str) -> str:
    """One compact harness vendor-mark badge, safe for hostile names."""
    return _badge_html(harness_brand(harness), harness)


def provider_badge_html(provider: str) -> str:
    """One compact provider vendor-mark badge, safe for hostile names."""
    return _badge_html(provider_brand(provider), provider)


@dataclass(frozen=True)
class PolicyStamp:
    """Which policy revision the control plane believes is current."""

    version: str = UNKNOWN
    digest: str = UNKNOWN
    updated_at: str = UNKNOWN


@dataclass(frozen=True)
class AuthorityState:
    """Fleet-policy-migration activation as reported by the routing snapshot."""

    active: str = UNKNOWN
    status: str = UNKNOWN


@dataclass(frozen=True)
class MachineState:
    """One machine's observed capacity. Every field defaults to ``unknown``."""

    id: str
    os: str = UNKNOWN
    state: str = UNKNOWN
    last_observed_at: str = UNKNOWN
    load: str = UNKNOWN
    capacity: str = UNKNOWN
    active_claims: str = UNKNOWN
    available_slots: str = UNKNOWN
    eligible_queued_work: str = UNKNOWN
    unused_reason: str = UNKNOWN
    # The enrolled node this policy machine runs on. Additive: snapshots from
    # before the key existed parse with ``unknown`` and join to no station.
    node_id: str = UNKNOWN


@dataclass(frozen=True)
class RouteDecision:
    """One routing receipt, including any operator override reason."""

    decision_id: str
    policy_version: str = UNKNOWN
    origin: str = UNKNOWN
    selected: str = UNKNOWN
    reason: str = UNKNOWN
    candidates: tuple[str, ...] = ()
    created_at: str = UNKNOWN
    override_reason: str = ""
    state: str = UNKNOWN


def _is_queue_exclusion(item: Mapping[str, object]) -> bool:
    """Recognize the hub's Worklore scan buckets, leaving malformed blocks actionable."""
    count = item.get("count")
    reason = item.get("reason")
    return (
        _cp_text(item.get("id")) == UNKNOWN
        and type(count) is int
        and count > 0
        and isinstance(reason, str)
        and reason
        in {
            "acceptance-required",
            "not-ready",
            "not-eligible",
            "manual-mode",
            "blocker",
            "attempt-limit",
            "review-after",
            "source-policy",
        }
    )


@dataclass(frozen=True)
class QueueState:
    eligible_count: str = UNKNOWN
    queued_count: str = UNKNOWN
    blocked: tuple[Mapping[str, object], ...] = ()
    blocked_known: bool = False
    complete: bool = False

    @property
    def exclusions(self) -> tuple[Mapping[str, object], ...]:
        """Counted scan exclusions have no job identity or observation timestamp."""
        return tuple(item for item in self.blocked if _is_queue_exclusion(item))

    @property
    def blocked_items(self) -> tuple[Mapping[str, object], ...]:
        """Keep individual blocks separate from aggregate eligibility exclusions."""
        return tuple(item for item in self.blocked if not _is_queue_exclusion(item))

    @property
    def actionable_exclusions(self) -> tuple[Mapping[str, object], ...]:
        """One counted alert per blocker or exhausted-attempt scan bucket."""
        return tuple(item for item in self.exclusions if item.get("reason") in {"blocker", "attempt-limit"})


@dataclass(frozen=True)
class IntegrationState:
    """A routine-driven integration. ``needs-attention`` is never 'paused'.

    Four separate facts, deliberately not collapsed into one health word:
    whether the *routine* is scheduled (``routine_state``), whether work was
    observed being picked up and reported, how long the oldest item has waited,
    and whether the integration is authenticated. An unauthenticated bot with a
    scheduled routine still executes nothing, and a scheduled routine with no
    observed pickup is unproven, not healthy.
    """

    id: str
    routine_state: str = UNKNOWN
    last_pickup: str = UNKNOWN
    last_report: str = UNKNOWN
    queued_age: str = UNKNOWN
    detail: str = ""
    auth_state: str = UNKNOWN


@dataclass(frozen=True)
class QuotaWindow:
    """One quota window inside a centrally computed effective-capacity row."""

    window_id: str = UNKNOWN
    state: str = UNKNOWN
    used: str = UNKNOWN
    limit: str = UNKNOWN
    remaining: str = UNKNOWN
    resets_at: str = UNKNOWN
    source: str = UNKNOWN
    observation_id: str = UNKNOWN
    reason: str = ""


@dataclass(frozen=True)
class QuotaEffective:
    """``fleet_quota``'s verdict for one account/pool/provider.

    This projection is the only thing the Deck may present as available
    headroom. Raw observations stay history: they carry no override or
    freshness precedence, and the Deck must not re-derive one.
    """

    account_id: str = UNKNOWN
    pool_id: str = UNKNOWN
    provider: str = UNKNOWN
    admitted: str = UNKNOWN
    reasons: tuple[str, ...] = ()
    windows: tuple[QuotaWindow, ...] = ()


@dataclass(frozen=True)
class ControlPlane:
    """Policy, capacity, routing, and integration telemetry, or its stated absence."""

    policy: PolicyStamp = field(default_factory=PolicyStamp)
    sessions: tuple[Mapping[str, object], ...] = ()
    machines: tuple[MachineState, ...] = ()
    quota: tuple[Mapping[str, object], ...] = ()
    routes: tuple[RouteDecision, ...] = ()
    queue: QueueState = field(default_factory=QueueState)
    integrations: tuple[IntegrationState, ...] = ()
    present: frozenset[str] = frozenset()
    # Optional and centrally computed. Absent means effective headroom is
    # unknown, never that the raw observations can stand in for it.
    quota_effective: tuple[QuotaEffective, ...] = ()
    quota_effective_present: bool = False
    # Optional. Absent means the Deck has not been given an authority stamp,
    # never that the migration is inactive.
    authority: AuthorityState = field(default_factory=AuthorityState)
    authority_present: bool = False


@dataclass(frozen=True)
class OctopoolObservation:
    """A supplied capture of shared-pool usage, with no availability verdict."""

    captured_at: str
    window_seconds: int
    scope: str = "pool-wide"
    requests: int | None = None
    saved_github_requests: int | None = None  # Historical cache-served alias, never upstream savings.
    cache_served_responses: int | None = None
    uncached_outcomes: int | None = None
    fallbacks: int | None = None
    service_errors: int | None = None
    eligible_cache_hit_rate: float | None = None


@dataclass(frozen=True)
class DeckView:
    stations: tuple[StationView, ...]
    rail: tuple[RailEntry, ...]
    repos: tuple[RepoRow, ...]
    outcomes: tuple[LiveRun, ...]
    observers: tuple[tuple[str, str], ...]
    cloud_workers: tuple[CloudWorker, ...] = ()
    interactive_sessions: tuple[InteractiveSession, ...] = ()
    recent_cloud_reports: tuple[InteractiveSession, ...] = ()
    control_plane: ControlPlane | None = None
    observed_work_groups: tuple[ObservedWorkGroup, ...] = ()
    stale_after_seconds: int = 1800
    octopool_observation: OctopoolObservation | Mapping[str, object] | None = None


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
        observed_work_groups=_observed_work_groups(raw.get("observed_work_groups", [])),
    )


def _observed_work_groups(raw: object) -> tuple[ObservedWorkGroup, ...]:
    error = "invalid observed work groups"
    if not isinstance(raw, list) or len(raw) > 8:
        raise DeckConfigError(error)
    groups = []
    keys: set[str] = set()
    required = {"key", "label", "coverage", "source_ref", "proxy_ref", "parent_ref", "work_ids"}
    for item in raw:
        if (
            not isinstance(item, dict)
            or not required <= item.keys()
            or item.keys() - required - {"snapshot_observed_at"}
        ):
            raise DeckConfigError(error)
        for name in required - {"work_ids"}:
            value = item[name]
            limit = 64 if name in {"key", "label"} else 256
            if not isinstance(value, str) or not value.strip() or len(value) > limit or _CONTROL_RE.search(value):
                raise DeckConfigError(error)
        key = item["key"]
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", key) or key in keys:
            raise DeckConfigError(error)
        keys.add(key)
        ids = item["work_ids"]
        if not isinstance(ids, list) or not 1 <= len(ids) <= 25:
            raise DeckConfigError(error)
        if any(not isinstance(value, str) or not CLAIM_ID_RE.fullmatch(value) for value in ids):
            raise DeckConfigError(error)
        if len(set(ids)) != len(ids):
            raise DeckConfigError(error)
        snapshot = None
        if "snapshot_observed_at" in item:
            value = item["snapshot_observed_at"]
            if not isinstance(value, str) or not 1 <= len(value) <= 64 or _CONTROL_RE.search(value):
                raise DeckConfigError(error)
            try:
                snapshot = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                raise DeckConfigError(error) from None
            if snapshot.tzinfo is None or snapshot.utcoffset() is None:
                raise DeckConfigError(error)
        groups.append(
            ObservedWorkGroup(
                key=key,
                label=item["label"],
                coverage=item["coverage"],
                source_ref=item["source_ref"],
                proxy_ref=item["proxy_ref"],
                parent_ref=item["parent_ref"],
                work_ids=tuple(ids),
                snapshot_observed_at=snapshot,
            )
        )
    return tuple(groups)


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
    if state == "external.unknown":
        return "unknown"
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
    """Broad parallel activity by repo label, without identity or file-overlap proof."""
    del claims
    nodes = {run.node_id for run in live_runs if run.repo == target}
    return bool(target.strip()) and len(nodes) >= 2


_LATEST_ROWS = (
    "SELECT node_id, run_id, repo, seat, harness, state, ts, received_at, repo_identity FROM ("
    "  SELECT node_id, run_id, sequence, digest, ROW_NUMBER() OVER ("
    "    PARTITION BY CASE WHEN harness = 'grokbot' THEN 'grokbot' ELSE 'node:' || node_id END, run_id "
    "    ORDER BY sequence DESC, received_at DESC, digest DESC"
    "  ) AS rn FROM events e"
    ") AS ranked JOIN events USING (node_id, run_id, sequence, digest) WHERE rn = 1"
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

_NON_EMPTY_FIELD = "IS NOT NULL AND trim({column}) != '' AND trim({column}) != '-'"


def _last_known_field(conn: sqlite3.Connection, *, node_id: str, run_id: str, column: str, grokbot: bool) -> str:
    """Last non-empty ``column`` for a run.

    Root cause for ``seat /`` cards: status lifecycle events carry no
    seat/harness payload, so the latest event row for a
    ``brigade run --worker <seat>`` run is blank even though an earlier
    ``run.dispatch.requested`` event named the seat. The status projection
    (``fleet_hub_status``) backfills the seat; the Deck did not, for seat or
    harness. A genuinely absent field stays absent (""), never a guess.
    """
    if column not in ("seat", "harness"):
        raise ValueError(f"deck backfill supports seat/harness, not {column!r}")
    if grokbot:
        row = conn.execute(
            f"SELECT {column} FROM events WHERE harness = 'grokbot' AND run_id = ? "
            f"AND {column} {_NON_EMPTY_FIELD.format(column=column)} "
            "ORDER BY sequence DESC, received_at DESC, digest DESC LIMIT 1",
            (run_id,),
        ).fetchone()
    else:
        row = conn.execute(
            f"SELECT {column} FROM events WHERE node_id = ? AND run_id = ? "
            f"AND {column} {_NON_EMPTY_FIELD.format(column=column)} "
            "ORDER BY sequence DESC, received_at DESC, digest DESC LIMIT 1",
            (node_id, run_id),
        ).fetchone()
    return str(row[0]) if row else ""


def _model_policy_for_seats(conn: sqlite3.Connection, seats: Sequence[str]) -> dict[str, dict[str, str]]:
    """Seat policy rows keyed by seat: provider, model, harness CLI, launch id.

    The events journal records seat/harness only; provider, canonical model,
    and the native launch id (``bindings.brigade.model``) live in the hub's
    ``model_policy`` table. Absent table or columns means unknown, never a
    guess. ``sqlite3.Error`` covers both (older hub DBs predate the columns).
    """
    wanted = sorted({seat for seat in seats if seat})
    if not wanted:
        return {}
    placeholders = ",".join("?" for _ in wanted)
    try:
        rows = conn.execute(
            "SELECT seat, provider, model, brigade_cli, brigade_model "
            f"FROM model_policy WHERE seat IN ({placeholders})",
            wanted,
        ).fetchall()
        fields = ("provider", "model", "brigade_cli", "brigade_model")
    except sqlite3.Error:
        try:
            rows = conn.execute(
                f"SELECT seat, provider, model FROM model_policy WHERE seat IN ({placeholders})",
                wanted,
            ).fetchall()
        except sqlite3.Error:
            return {}
        fields = ("provider", "model", "", "")
    policy: dict[str, dict[str, str]] = {}
    for row in rows:
        policy[str(row[0])] = {name: str(value or "") for name, value in zip(fields, row[1:], strict=True)}
    return policy


def _enrich_runs(conn: sqlite3.Connection, runs: list[LiveRun]) -> list[LiveRun]:
    """Backfill blank seat/harness from earlier events; join model policy."""
    if not runs:
        return runs
    backfilled: list[LiveRun] = []
    for run in runs:
        grokbot = run.harness == "grokbot"
        seat = run.seat or _last_known_field(
            conn, node_id=run.node_id, run_id=run.run_id, column="seat", grokbot=grokbot
        )
        harness = run.harness or _last_known_field(
            conn, node_id=run.node_id, run_id=run.run_id, column="harness", grokbot=grokbot
        )
        backfilled.append(
            run
            if seat == run.seat and harness == run.harness
            else LiveRun(
                node_id=run.node_id,
                run_id=run.run_id,
                repo=run.repo,
                seat=seat,
                harness=harness,
                state=run.state,
                bucket=run.bucket,
                age_seconds=run.age_seconds,
                elapsed_seconds=run.elapsed_seconds,
                repo_identity=run.repo_identity,
            )
        )
    policy = _model_policy_for_seats(conn, [run.seat for run in backfilled])
    enriched: list[LiveRun] = []
    for run in backfilled:
        row = policy.get(run.seat)
        if not row:
            enriched.append(run)
            continue
        harness = run.harness or row.get("brigade_cli", "")
        enriched.append(
            LiveRun(
                node_id=run.node_id,
                run_id=run.run_id,
                repo=run.repo,
                seat=run.seat,
                harness=harness,
                state=run.state,
                bucket=run.bucket,
                age_seconds=run.age_seconds,
                elapsed_seconds=run.elapsed_seconds,
                repo_identity=run.repo_identity,
                provider=row.get("provider", ""),
                model=row.get("model", ""),
                launch_model=row.get("brigade_model", "") or row.get("model", ""),
            )
        )
    return enriched


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
    started = fetch_started_at(conn, [(row[0], row[1], row[4]) for row in rows])
    runs: list[LiveRun] = []
    for node_id, run_id, repo, seat, harness, state, _ts, received_at, repo_identity in rows:
        age = _age_seconds(received_at, now)
        state = observation_state(state, harness, _ts, now=now)
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
    return _enrich_runs(conn, runs)


def fetch_started_at(
    conn: sqlite3.Connection, runs: Sequence[tuple[str, str, str | None]]
) -> dict[tuple[str, str], str]:
    result: dict[tuple[str, str], str] = {}
    generic_keys = [(node_id, run_id) for node_id, run_id, harness in runs if harness != "grokbot"]
    for start in range(0, len(generic_keys), _START_CHUNK):
        chunk = generic_keys[start : start + _START_CHUNK]
        predicate = " OR ".join("(node_id = ? AND run_id = ?)" for _ in chunk)
        params = [value for pair in chunk for value in pair]
        rows = conn.execute(
            "SELECT node_id, run_id, ts FROM ("
            "  SELECT node_id, run_id, ts, ROW_NUMBER() OVER ("
            "    PARTITION BY node_id, run_id ORDER BY sequence ASC, received_at ASC, digest ASC"
            "  ) AS rn FROM events WHERE (" + predicate + ") AND (harness IS NULL OR harness != 'grokbot')"
            ")"
            " WHERE rn = 1",
            params,
        ).fetchall()
        for row in rows:
            result[(row[0], row[1])] = row[2]

    grokbot_keys = [(node_id, run_id) for node_id, run_id, harness in runs if harness == "grokbot"]
    for start in range(0, len(grokbot_keys), _START_CHUNK):
        chunk = grokbot_keys[start : start + _START_CHUNK]
        run_ids = tuple({run_id for _node_id, run_id in chunk})
        placeholders = ",".join("?" for _ in run_ids)
        rows = conn.execute(
            "SELECT run_id, ts FROM ("
            "  SELECT run_id, ts, ROW_NUMBER() OVER ("
            "    PARTITION BY run_id ORDER BY sequence ASC, received_at ASC, digest ASC"
            "  ) AS rn FROM events WHERE harness = 'grokbot' AND run_id IN (" + placeholders + ")"
            ") WHERE rn = 1",
            run_ids,
        ).fetchall()
        started_by_run_id = {row[0]: row[1] for row in rows}
        for node_id, run_id in chunk:
            if run_id in started_by_run_id:
                result[(node_id, run_id)] = started_by_run_id[run_id]
    return result


def fetch_outcomes(conn: sqlite3.Connection, *, outcome_window: int) -> list[LiveRun]:
    """Terminal rows an operator should read, minus Brigade's internal milestones."""
    rows = conn.execute(
        f"{_LATEST_ROWS} AND {_IS_TERMINAL_SQL}"
        f" AND state NOT IN ({_INTERNAL_OUTCOME_PLACEHOLDERS}) ORDER BY ts DESC LIMIT ?",
        (*TERMINAL_STATES, *_INTERNAL_OUTCOME_PARAMS, int(outcome_window)),
    ).fetchall()
    return _enrich_runs(conn, [_live_run_from_row(row) for row in rows])


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
    return _enrich_runs(conn, [_live_run_from_row(row, bucket="failed", now=now) for row in rows])


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


def octopool_observation_from_snapshot(raw: object) -> OctopoolObservation | None:
    """Project only bounded capture provenance and allowlisted usage counters."""
    names = (
        "requests",
        "saved_github_requests",
        "cache_served_responses",
        "uncached_outcomes",
        "fallbacks",
        "service_errors",
    )
    if isinstance(raw, OctopoolObservation):
        raw = {
            "captured_at": raw.captured_at,
            "scope": raw.scope,
            "window": {"seconds": raw.window_seconds},
            "pool_usage": {
                name: getattr(raw, name)
                for name in (*names, "eligible_cache_hit_rate")
                if getattr(raw, name) is not None
            },
        }
    if not isinstance(raw, Mapping) or raw.get("scope") != "pool-wide":
        return None
    stamp, window, usage = raw.get("captured_at"), raw.get("window"), raw.get("pool_usage")
    if not isinstance(stamp, str) or len(stamp) > 64 or _strip_controls(stamp) != stamp:
        return None
    if not isinstance(window, Mapping) or not isinstance(usage, Mapping):
        return None
    seconds = window.get("seconds")
    if type(seconds) is not int or not 0 < seconds <= 2**53 - 1:
        return None
    try:
        captured = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if captured.tzinfo is None:
            return None
        stamp = captured.astimezone(_UTC).isoformat()
    except (ValueError, OverflowError):
        return None
    usage = dict(usage)
    if "cache_served_responses" not in usage:
        usage["cache_served_responses"] = usage.get("saved_github_requests")
    if "uncached_outcomes" not in usage:
        usage["uncached_outcomes"] = usage.get("backend_requests")
    counters = {
        name: value if type(value) is int and 0 <= value <= 2**53 - 1 else None
        for name in names
        for value in (usage.get(name),)
    }
    rate = usage.get("eligible_cache_hit_rate")
    rate = float(rate) if isinstance(rate, (int, float)) and not isinstance(rate, bool) and 0 <= rate <= 1 else None
    return OctopoolObservation(
        captured_at=stamp,
        window_seconds=seconds,
        requests=counters["requests"],
        saved_github_requests=counters["saved_github_requests"],
        cache_served_responses=counters["cache_served_responses"],
        uncached_outcomes=counters["uncached_outcomes"],
        fallbacks=counters["fallbacks"],
        service_errors=counters["service_errors"],
        eligible_cache_hit_rate=rate,
    )


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
    """Active presence plus separately bounded v2 stale/terminal task reports."""
    from . import fleet_hub_sessions

    active = fleet_hub_sessions.list_sessions(conn, now_epoch=now.timestamp(), exclude_terminal_cloud_reports=True)[
        :SESSION_LIMIT
    ]
    history = fleet_hub_sessions.list_cloud_reports(conn, now_epoch=now.timestamp(), limit=CLOUD_REPORT_HISTORY_LIMIT)
    return active + history


def fetch_observers(conn: sqlite3.Connection, configured: frozenset[str]) -> list[tuple[str, str]]:
    placeholders = ",".join("?" for _ in configured)
    predicate = f" WHERE node_id NOT IN ({placeholders})" if configured else ""
    query = (
        "SELECT node_id, MAX(received_at) FROM events"
        + predicate
        + f" GROUP BY node_id ORDER BY MAX(received_at) DESC LIMIT {OBSERVER_LIMIT}"
    )
    return [(row[0], row[1]) for row in conn.execute(query, tuple(configured)).fetchall()]


def _live_run_from_row(row: tuple, *, bucket: str | None = None, now: datetime | None = None) -> LiveRun:
    return LiveRun(
        node_id=row[0],
        run_id=row[1],
        repo=row[2] or "",
        seat=row[3] or "",
        harness=row[4] or "",
        state=row[5],
        bucket=row[5] if bucket is None else bucket,
        age_seconds=_age_seconds(row[7] or "", now) if now is not None and len(row) > 7 else None,
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
    control_plane: ControlPlane | None = None,
) -> DeckView:
    from .fleet_dot import TERMINAL_STATES
    from .fleet_deck_activity import interactive_session

    active_runs = tuple(
        run for run in live_runs if not is_terminal_state(run.state) and run.bucket not in {"stale", "unknown"}
    )
    claims_by_target = {claim.target: claim for claim in claims}
    display_claims = {target: _display_claim(claim, enrolled_labels) for target, claim in claims_by_target.items()}
    parallel_targets = {
        target for target in {run.repo for run in active_runs} if collides(target, active_runs, claims_by_target)
    }
    session_rows_list = []
    history_rows = []
    for item in interactive_sessions:
        session = interactive_session(item, now=now)
        cloud = session.cloud_context
        if (
            cloud is not None
            and cloud.version == 2
            and (session.presence_state == "stale" or cloud.reported_state in TERMINAL_STATES)
        ):
            history_rows.append(session)
        elif item.get("presence_state") != "stale":
            session_rows_list.append(session)
    session_rows = tuple(session_rows_list[:SESSION_LIMIT])
    overlap_targets = _exact_path_overlap_targets(interactive_sessions)
    stations: list[StationView] = []
    for station in config.stations:
        tiles = tuple(
            sorted(
                (
                    Tile(run=run, claim=display_claims.get(run.repo), collision=run.repo in parallel_targets)
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
        RailEntry(
            kind="failed",
            node_id=run.node_id,
            repo=run.repo,
            run_id=run.run_id,
            detail=run.state,
            age_seconds=run.age_seconds,
        )
        for run in failed_outcomes
    ]
    rail.extend(
        RailEntry(
            kind=run.bucket,
            node_id=run.node_id,
            repo=run.repo,
            run_id=run.run_id,
            detail=run.state,
            age_seconds=run.age_seconds,
        )
        for run in live_runs
        if run.bucket in ("awaiting approval", "stale", "unknown")
    )
    from .fleet_deck_activity import parallel_entries

    labels = dict(enrolled_labels)
    labels.update({station.station.node_id: station.label for station in stations})
    rail.extend(parallel_entries(parallel_targets, active_runs, labels))
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
                    collision=target in parallel_targets,
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
            key=lambda row: (not row.interactive_overlap, row.target),
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
        recent_cloud_reports=tuple(history_rows[:CLOUD_REPORT_HISTORY_LIMIT]),
        control_plane=control_plane,
        observed_work_groups=config.observed_work_groups,
        stale_after_seconds=config.stale_after_seconds,
    )


def _cp_text(raw: object, *, limit: int = CLOUD_TEXT_LIMIT) -> str:
    """A telemetry scalar, or ``unknown``. Absent is never rendered as 0 or free.

    A real ``0`` is kept: an observed zero and an unobserved value are
    different answers and the Deck must not collapse them.
    """
    if raw is None or raw == "":
        return UNKNOWN
    if isinstance(raw, bool):
        return "yes" if raw else "no"
    if isinstance(raw, (int, float)):
        return str(raw)[:limit]
    return _safe_display_text(raw, limit=limit) or UNKNOWN


def _cp_list(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return ()
    return tuple(_safe_display_text(item) for item in list(raw)[:CLOUD_LEASE_LIMIT])


def _cp_records(raw: Mapping[str, object], key: str) -> list[Mapping[str, object]]:
    """The mapping rows under ``key``. A non-sequence or scalar yields nothing."""
    value = raw.get(key)
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def control_plane_from_snapshot(raw: Mapping[str, object] | None) -> ControlPlane | None:
    """Adapt a routing/telemetry snapshot into the Deck's projection.

    The adapter records which sections the snapshot actually supplied. A
    section that is absent stays absent, so the Deck can say ``unknown``
    instead of inventing an all-clear from silence. This is the seam a future
    ``fleet_hub_routing.snapshot`` attaches to.
    """
    if not isinstance(raw, Mapping):
        return None
    present = {name for name in CONTROL_PLANE_SECTIONS if raw.get(name) is not None}

    policy_raw = raw.get("policy")
    policy = PolicyStamp()
    if isinstance(policy_raw, Mapping):
        policy = PolicyStamp(
            version=_cp_text(policy_raw.get("version")),
            digest=_cp_text(policy_raw.get("digest")),
            updated_at=_cp_text(policy_raw.get("updated_at")),
        )

    machines: list[MachineState] = []
    for item in _cp_records(raw, "machines"):
        machines.append(
            MachineState(
                id=_cp_text(item.get("id")),
                os=_cp_text(item.get("os")),
                state=_cp_text(item.get("state")),
                last_observed_at=_cp_text(item.get("last_observed_at")),
                load=_cp_text(item.get("load")),
                capacity=_cp_text(item.get("capacity")),
                active_claims=_cp_text(item.get("active_claims")),
                available_slots=_cp_text(item.get("available_slots")),
                eligible_queued_work=_cp_text(item.get("eligible_queued_work")),
                unused_reason=_cp_text(item.get("unused_reason")),
                node_id=_cp_text(item.get("node_id")),
            )
        )

    routes: list[RouteDecision] = []
    for item in _cp_records(raw, "routes"):
        routes.append(
            RouteDecision(
                decision_id=_cp_text(item.get("decision_id")),
                policy_version=_cp_text(item.get("policy_version")),
                origin=_cp_text(item.get("origin")),
                selected=_cp_text(item.get("selected")),
                reason=_cp_text(item.get("reason")),
                candidates=_cp_list(item.get("candidates")),
                created_at=_cp_text(item.get("created_at")),
                override_reason=_safe_display_text(item.get("override_reason") or ""),
                state=_cp_text(item.get("state")),
            )
        )

    queue_raw = raw.get("queue")
    queue = QueueState()
    if isinstance(queue_raw, Mapping):
        blocked = tuple(_cp_records(queue_raw, "blocked"))
        queue = QueueState(
            eligible_count=_cp_text(queue_raw.get("eligible_count")),
            queued_count=_cp_text(queue_raw.get("queued_count")),
            blocked=blocked,
            blocked_known=queue_raw.get("blocked") is not None,
            complete=queue_raw.get("complete") is True,
        )

    integrations: list[IntegrationState] = []
    for item in _cp_records(raw, "integrations"):
        state = item.get("routine_state")
        auth = item.get("auth_state")
        integrations.append(
            IntegrationState(
                id=_cp_text(item.get("id")),
                routine_state=state if state in ("active", "needs-attention") else UNKNOWN,
                last_pickup=_cp_text(item.get("last_pickup")),
                last_report=_cp_text(item.get("last_report")),
                queued_age=_cp_text(item.get("queued_age")),
                detail=_safe_display_text(item.get("detail") or ""),
                auth_state=auth if auth in ("authenticated", "unauthenticated", "expired") else UNKNOWN,
            )
        )

    sessions = tuple(_cp_records(raw, "sessions"))
    quota = tuple(_cp_records(raw, "quota"))
    authority_raw = raw.get("authority")
    authority = AuthorityState()
    authority_present = False
    if isinstance(authority_raw, Mapping):
        authority_present = True
        authority = AuthorityState(
            active=_cp_text(authority_raw.get("active")),
            status=_cp_text(authority_raw.get("status")),
        )
    return ControlPlane(
        policy=policy,
        sessions=sessions,
        machines=tuple(machines),
        quota=quota,
        routes=tuple(routes),
        queue=queue,
        integrations=tuple(integrations),
        present=frozenset(present),
        quota_effective=_quota_effective(raw),
        quota_effective_present=raw.get("quota_effective") is not None,
        authority=authority,
        authority_present=authority_present,
    )


def _quota_effective(raw: Mapping[str, object]) -> tuple[QuotaEffective, ...]:
    """Adapt ``quota_effective`` rows. The Deck computes no precedence of its own."""
    rows: list[QuotaEffective] = []
    for item in _cp_records(raw, "quota_effective"):
        windows = tuple(
            QuotaWindow(
                window_id=_cp_text(window.get("window_id")),
                state=_cp_text(window.get("state")),
                used=_cp_text(window.get("used")),
                limit=_cp_text(window.get("limit")),
                remaining=_cp_text(window.get("remaining")),
                resets_at=_cp_text(window.get("resets_at")),
                source=_cp_text(window.get("source")),
                observation_id=_cp_text(window.get("observation_id")),
                reason=_safe_display_text(window.get("reason") or ""),
            )
            for window in _cp_records(item, "windows")
        )
        rows.append(
            QuotaEffective(
                account_id=_cp_text(item.get("account_id")),
                pool_id=_cp_text(item.get("pool_id")),
                provider=_cp_text(item.get("provider")),
                admitted=_cp_text(item.get("admitted")),
                reasons=_cp_list(item.get("reasons")),
                windows=windows,
            )
        )
    return tuple(rows)


def coverage_gaps(control_plane: ControlPlane | None) -> tuple[str, ...]:
    """Telemetry the Deck does not have. A gap is never an all-clear."""
    if control_plane is None:
        return ("control-plane telemetry",)
    gaps = [section for section in CONTROL_PLANE_SECTIONS if section not in control_plane.present]
    if "machines" in control_plane.present and any(
        machine.capacity == UNKNOWN or machine.state == UNKNOWN for machine in control_plane.machines
    ):
        gaps.append("machine capacity")
    if "queue" in control_plane.present and (
        control_plane.queue.eligible_count == UNKNOWN or not control_plane.queue.blocked_known
    ):
        gaps.append("queue eligibility")
    if any(item.routine_state == UNKNOWN for item in control_plane.integrations):
        gaps.append("integration readiness")
    # A legacy or partial snapshot carries no ``authority`` key, and the panel
    # then prints "authority unknown". Without this the same panel also claims
    # every section reported, and deck_verdict can answer ALL CLEAR while
    # control-plane authority is explicitly unknown.
    if not control_plane.authority_present or (
        control_plane.authority.active == UNKNOWN and control_plane.authority.status == UNKNOWN
    ):
        gaps.append("control-plane authority")
    return tuple(dict.fromkeys(gaps))


def attention_items(view: DeckView) -> tuple[str, ...]:
    """Real signals that need someone: rail entries plus flagged integrations."""
    items = [f"{entry.kind}:{entry.repo}" for entry in view.rail if entry.kind != "parallel"]
    control_plane = view.control_plane
    if control_plane is not None:
        items.extend(
            f"integration:{item.id}" for item in control_plane.integrations if item.routine_state == "needs-attention"
        )
        items.extend(f"blocked:{item.get('reason', UNKNOWN)}" for item in control_plane.queue.blocked_items)
        items.extend(f"blocked:{item['reason']}" for item in control_plane.queue.actionable_exclusions)
    return tuple(items)


def deck_verdict(view: DeckView) -> str:
    """``NEEDS ATTENTION`` beats ``COVERAGE UNKNOWN`` beats ``ALL CLEAR``.

    An empty attention rail is not an all-clear on its own: with no telemetry
    the Deck cannot see whether anything needs anyone, and says so.
    """
    attention = attention_items(view)
    if attention:
        return f"NEEDS ATTENTION ({len(attention)})"
    gaps = coverage_gaps(view.control_plane)
    if gaps:
        return f"COVERAGE UNKNOWN ({len(gaps)})"
    return "ALL CLEAR"


def render_control_plane(control_plane: ControlPlane | None, *, worklore_enabled: bool = True) -> str:
    """The control-plane panel. Missing telemetry renders as ``unknown``."""
    gaps = coverage_gaps(control_plane)
    gap_html = (
        '<p class="collision">no telemetry for: ' + _esc(", ".join(gaps)) + "</p>"
        if gaps
        else '<p class="station-meta">all control-plane sections reported.</p>'
    )
    if control_plane is None:
        return (
            '<section class="panel" aria-labelledby="control-plane"><header>'
            '<h2 id="control-plane">Control plane</h2></header>'
            f"{gap_html}"
            '<p class="empty">No control-plane projection is attached to this Deck yet, so policy version, '
            "machine capacity, quota, routing receipts, queue eligibility, and integration readiness are all "
            "unknown. That is not the same as nothing being wrong.</p></section>"
        )
    policy = control_plane.policy
    if control_plane.authority_present:
        authority_label = control_plane.authority.status
        if authority_label == UNKNOWN:
            authority_label = control_plane.authority.active
        authority_html = f"authority {_esc(authority_label)} &middot; "
    else:
        authority_html = "authority unknown &middot; "
    machine_rows = "".join(
        f"<tr><td>{_esc(machine.id)}</td><td>{_esc(machine.os)}</td><td>{_esc(machine.state)}</td>"
        f"<td>{_esc(machine.load)}/{_esc(machine.capacity)}</td><td>{_esc(machine.active_claims)}</td>"
        f"<td>{_esc(machine.available_slots)}</td><td>{_esc(machine.eligible_queued_work)}</td>"
        f"<td>{_esc(machine.unused_reason)}</td><td>{_esc(machine.last_observed_at)}</td></tr>"
        for machine in control_plane.machines
    )
    machines_html = (
        '<div class="table-wrap"><table class="roster-table"><thead><tr><th>Machine</th><th>OS</th><th>State</th>'
        "<th>Load/capacity</th><th>Claims</th><th>Free slots</th><th>Eligible queued</th><th>Why idle</th>"
        f"<th>Observed</th></tr></thead><tbody>{machine_rows}</tbody></table></div>"
        if machine_rows
        else '<p class="empty">No machine capacity reported (unknown, not idle).</p>'
    )
    quota_rows = "".join(
        f"<li>{_esc(row.get('pool', UNKNOWN))} &middot; used {_esc(_cp_text(row.get('used')))} of "
        f"{_esc(_cp_text(row.get('limit')))} &middot; observed {_esc(_cp_text(row.get('observed_at')))}</li>"
        for row in control_plane.quota
    )
    quota_html = (
        f'<ul class="attention-list">{quota_rows}</ul>'
        if quota_rows
        else '<p class="empty">No quota observations (unknown, not unlimited).</p>'
    )
    effective_html = _quota_effective_html(control_plane)
    route_rows = "".join(
        f"<li>{_esc(route.decision_id)} &middot; policy {_esc(route.policy_version)} &middot; "
        f"{_esc(route.origin)} &rarr; {_esc(route.selected)} &middot; {_esc(route.reason)} &middot; "
        f"state {_esc(route.state)} &middot; candidates {_esc(', '.join(route.candidates) or UNKNOWN)}"
        + (f" &middot; override: {_esc(route.override_reason)}" if route.override_reason else "")
        + "</li>"
        for route in control_plane.routes
    )
    routes_html = (
        f'<ul class="attention-list">{route_rows}</ul>'
        if route_rows
        else '<p class="empty">No routing receipts (unknown, not unrouted).</p>'
    )
    from .fleet_deck_activity import REASONS

    blocked_rows = "".join(
        f"<li>{_esc(item.get('id', UNKNOWN))} &middot; "
        f"{_esc(REASONS.get(_cp_text(item.get('reason')), _cp_text(item.get('reason'))))}</li>"
        for item in control_plane.queue.blocked_items
    )
    queue_html = (
        _queue_counts_html(control_plane.queue)
        + (
            f'<ul class="attention-list">{blocked_rows}</ul>'
            if blocked_rows
            else (
                ""
                if control_plane.queue.exclusions
                else '<p class="empty">No blocked reasons reported.</p>'
                if control_plane.queue.blocked_known
                else '<p class="empty">Blocked reasons unknown.</p>'
            )
        )
        + _queue_exclusions_html(control_plane.queue, worklore_enabled=worklore_enabled)
    )
    integration_rows = "".join(
        f"<li>{_esc(item.id)} &middot; routine {_esc(item.routine_state)} &middot; auth {_esc(item.auth_state)} "
        f"&middot; last pickup {_esc(item.last_pickup)} "
        f"&middot; last report {_esc(item.last_report)} &middot; queued for {_esc(item.queued_age)}"
        + (f" &middot; {_esc(item.detail)}" if item.detail else "")
        + "</li>"
        for item in control_plane.integrations
    )
    integrations_html = (
        f'<ul class="attention-list">{integration_rows}</ul>'
        if integration_rows
        else (
            '<p class="empty">No integration readiness reports in this snapshot.</p>'
            if "integrations" in control_plane.present
            else '<p class="empty">Integration readiness unknown: no telemetry section reported.</p>'
        )
    )
    return (
        '<section class="panel" aria-labelledby="control-plane"><header><h2 id="control-plane">Control plane</h2>'
        f'<p class="panel-count">{authority_html}policy {_esc(policy.version)} &middot; {_esc(policy.digest)} &middot; '
        f"updated {_esc(policy.updated_at)}</p></header>{gap_html}"
        f"<h3>Machines</h3>{machines_html}"
        f"<h3>Effective capacity</h3>{effective_html}"
        f"<h3>Quota observation history</h3>{quota_html}"
        '<p class="station-meta">These are raw observations kept as history. They are not available '
        "subscription headroom: only the effective projection above answers that, and the Deck applies no "
        "override or freshness precedence of its own.</p>"
        f"<h3>Routing</h3>{routes_html}"
        f"<h3>Queue</h3>{queue_html}"
        f"<h3>Integrations</h3>{integrations_html}"
        '<p class="station-meta">Quota headroom is a subscription number. It is not evidence that a bot or '
        "runner will execute, and it is not harness capacity: an integration that has not confirmed is "
        "<em>needs-attention</em> or <em>unknown</em>, never proof it is paused, and an observed pickup proves "
        "only that pickup. Routine scheduling, observed pickup and report, queue age, and authentication are "
        "four separate facts; a pending away prompt is evidence that something needs attention.</p>"
        "</section>"
    )


def _queue_counts_html(queue: QueueState) -> str:
    from .fleet_deck_activity import queue_counts

    return queue_counts(queue)


def _queue_exclusions_html(queue: QueueState, *, worklore_enabled: bool) -> str:
    from .fleet_deck_activity import queue_exclusions

    return queue_exclusions(queue, worklore_enabled=worklore_enabled)


def _quota_effective_html(control_plane: ControlPlane) -> str:
    """Effective remaining capacity, or an honest unknown. Never derived here."""
    if not control_plane.quota_effective_present:
        return (
            '<p class="empty">Effective headroom is <strong>unknown</strong>: no centrally computed quota '
            "projection is attached. Raw observations below are history, not available headroom.</p>"
        )
    rows = "".join(
        f"<li>{_esc(row.account_id)} &middot; pool {_esc(row.pool_id)} &middot; {_esc(row.provider)} &middot; "
        f"admitted {_esc(row.admitted)}"
        + (f" &middot; {_esc(', '.join(row.reasons))}" if row.reasons else "")
        + (
            "<ul>"
            + "".join(
                f"<li>{_esc(window.window_id)} &middot; {_esc(window.state)} &middot; "
                f"{_esc(window.used)}/{_esc(window.limit)} used &middot; remaining "
                f"<strong>{_esc(window.remaining)}</strong> &middot; resets {_esc(window.resets_at)} &middot; "
                f"source {_esc(window.source)} &middot; observation {_esc(window.observation_id)}"
                + (f" &middot; {_esc(window.reason)}" if window.reason else "")
                + "</li>"
                for window in row.windows
            )
            + "</ul>"
            if row.windows
            else '<p class="empty">No windows reported for this row.</p>'
        )
        + "</li>"
        for row in control_plane.quota_effective
    )
    if not rows:
        return '<p class="empty">The effective quota projection reported no rows.</p>'
    return f'<ul class="attention-list">{rows}</ul>'


# The shared document stamps this style's nonce.
_STYLE = """
.badge { display: inline-flex; flex: 0 0 20px; align-items: center; justify-content: center; width: 20px; height: 20px; padding: 0; vertical-align: middle; color: var(--ink); }
.badge-icon svg { width: 20px; height: 20px; display: block; }
.cloud-worker-card h3 .badge, .attention-list li .badge { margin-right: 6px; }
.tile-facts { display: grid; grid-template-columns: auto minmax(0, 1fr); gap: var(--space-1) 10px; margin: 10px 0 0; color: var(--muted); font-size: 12px; }
.tile-facts dt { color: var(--muted); }
.tile-facts dd { min-width: 0; margin: 0; color: var(--ink); }
.work-text { white-space: pre-wrap; overflow-wrap: anywhere; }
.work-item h2, .work-item li { overflow-wrap: anywhere; }
.work-item h3, .owner-card h3 { margin-top: var(--space-4); }
.claim, .collision { margin: 10px 0 0; font-size: 12px; }
.collision { color: var(--signal); font-weight: 800; }
.cloud-workers { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: var(--space-4); margin-top: var(--space-4); }
.cloud-worker-card { min-width: 0; padding: var(--space-4); border: 1px solid var(--line); border-radius: var(--radius-card); background: var(--surface); }
.cloud-worker-card > header { display: flex; align-items: baseline; justify-content: space-between; gap: var(--space-2); margin-bottom: 10px; }
.cloud-capacity { margin: 0; color: var(--signal); font-size: 12px; font-weight: 700; font-variant-numeric: tabular-nums; white-space: nowrap; }
.cloud-state { margin: 0 0 10px; color: var(--muted); font-size: 12px; }
.cloud-lease-list { display: grid; gap: var(--space-2); margin: 0; padding: 0; list-style: none; }
.cloud-lease-list li { padding-top: var(--space-2); border-top: 1px solid var(--line-quiet); color: var(--muted); font-size: 12px; }
.attention-list, .timeline, .observer-list { margin: 0; padding: 0; list-style: none; }
.attention-list { display: grid; gap: var(--space-2); }
.attention-list li { padding: 10px 0; border-top: 1px solid var(--line-quiet); color: var(--muted); }
.attention-list li:first-child { border-top: 0; padding-top: 0; }
.attention-kind { color: var(--signal); font-size: 11px; font-weight: 800; letter-spacing: .06em; text-transform: uppercase; }
.timeline { display: grid; gap: 0; }
.timeline li { display: grid; grid-template-columns: 140px minmax(0, 1fr) auto; gap: 10px; padding: 10px 0; border-top: 1px solid var(--line-quiet); color: var(--muted); font-size: 12px; }
.timeline li:first-child { border-top: 0; padding-top: 0; }
.timeline-state { color: var(--ink); font-weight: 700; }
.observer-list { display: grid; gap: var(--space-2); margin-top: var(--space-3); font-size: 12px; }
th:nth-child(1) { width: 22%; } th:nth-child(2) { width: 20%; } th:nth-child(3) { width: 13%; } th:nth-child(4) { width: 34%; } th:nth-child(5) { width: 11%; }
.roster-table th:nth-child(n), .owners-table th:nth-child(n) { width: auto; }
.owners-table { min-width: 640px; }
.seat--off td { color: var(--faint); }
@media (max-width: 700px) {
  .timeline li { grid-template-columns: 1fr; gap: 2px; }
}
"""


def render_deck(
    view: DeckView,
    *,
    nonce: str,
    now: datetime,
    worklore_enabled: bool = True,
    action_sources: frozenset[str] = frozenset(),
) -> str:
    # Imported here because fleet_deck_overview imports this module for its types.
    from . import fleet_deck_overview

    body = fleet_deck_overview.render_overview(
        view, now, worklore_enabled=worklore_enabled, action_sources=action_sources
    )
    return _document(
        body,
        nonce=nonce,
        now=now,
        active="overview",
        view=view,
        worklore_enabled=worklore_enabled,
        extra_css=ui_theme.OVERVIEW_CSS,
    )


def repo_coordination_table(view: DeckView) -> str:
    """The claim/run coordination table. ``fleet_repo_policy_page`` frames it."""
    labels = {station.station.node_id: station.label for station in view.stations}
    rows = "".join(
        "<tr>"
        f"<td>{_esc(row.target or UNKNOWN)}</td>"
        f"<td>{_esc(_owner_text(row.claim) or 'No active claim')}</td>"
        f"<td>{_esc(str(row.claim.ttl_remaining) + 's left') if row.claim else 'No active claim'}</td>"
        f"<td>{_esc(', '.join(f'{labels.get(run.node_id, run.node_id[:12])}:{run.state}' for run in row.live))}</td>"
        f'<td class="{"flag" if row.interactive_overlap else ""}">{_esc(_repo_flags(row))}</td>'
        "</tr>"
        for row in view.repos
    )
    if not rows:
        return "<p>No claims or live repos.</p>"
    return (
        '<div class="table-wrap"><table><thead><tr><th>Target</th><th>Owner</th><th>TTL</th><th>Live now</th>'
        f"<th>Flags</th></tr></thead><tbody>{rows}</tbody></table></div>"
    )


def _repo_flags(row: RepoRow) -> str:
    flags: list[str] = []
    if row.collision:
        flags.append("parallel activity")
    if row.interactive_overlap:
        flags.append("! overlap")
    return " ".join(flags)


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


def _document(
    body: str,
    *,
    nonce: str,
    now: datetime,
    title: str = "Command Deck",
    refresh: bool = True,
    refresh_seconds: int = 10,
    active: str = "overview",
    view: DeckView | None = None,
    worklore_enabled: bool | None = None,
    extra_css: str = "",
    script: str = "",
) -> str:
    """Every Deck page: its body inside the shared two-column shell.

    *view* feeds the sidebar machine tree; pages that never build a
    ``DeckView`` pass ``None`` and get the navigation only.
    """
    # Imported here because the sidebar imports this module for its types.
    from . import fleet_deck_sidebar

    if worklore_enabled is None:
        from . import worklore_http

        worklore_enabled = worklore_http.enabled()
    sidebar = fleet_deck_sidebar.render_sidebar(view, active, now, worklore_enabled=worklore_enabled)
    return fleet_deck_shell.deck_page(
        title,
        active,
        sidebar,
        fleet_deck_sidebar.render_section_tabs(active, worklore_enabled=worklore_enabled) + body,
        nonce,
        as_of=_stamp(now),
        refresh_seconds=refresh_seconds if refresh else None,
        extra_css=_STYLE + extra_css,
        script=script,
    )
