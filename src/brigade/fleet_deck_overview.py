"""Overview page renderers for the Command Deck.

Pure functions over ``DeckView``: no database access and no I/O. Every value
that came from a run, station, session, provider, or snapshot goes through
``_esc`` before it reaches markup or an attribute. The page is built from data
the hub already stores, and a missing value renders as ``unknown``, never as
zero or as an all-clear.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Sequence
from urllib.parse import urlencode

from . import fleet_command_deck as deck
from .fleet_command_deck import UNKNOWN, DeckView, LiveRun, MachineState, StationView, Tile

_esc = deck._esc
_UTC = timezone.utc

# Staleness is the hub's verdict against the policy TTL, never a second Deck threshold.
STALE_REASONS = frozenset({"stale-telemetry", "unknown-telemetry"})
POLICY_LIMITS = {
    "machine-disabled": "disabled",
    "machine-draining": "draining",
    "routing-disabled": "routing off",
    "machine-stale-auth": "stale auth",
    "machine-unknown-auth": "unknown auth",
    "machine-unknown-inventory": "unknown inventory",
    "machine-seat-unusable": "no usable seat",
}


def format_age(seconds: int | None) -> str:
    """``11s``, ``4m``, ``1h 13m``, ``40d 2h``; ``unknown`` when there is no age."""
    if seconds is None:
        return UNKNOWN
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m"
    if total < 86400:
        return f"{total // 3600}h {total % 3600 // 60}m"
    return f"{total // 86400}d {total % 86400 // 3600}h"


def seconds_since(stamp: str | None, now: datetime) -> int | None:
    """Whole seconds from an ISO stamp to *now*, or None when it cannot be read."""
    if not stamp or stamp == UNKNOWN:
        return None
    try:
        moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_UTC)
    return max(0, int((now - moment).total_seconds()))


def collapse_outcomes(outcomes: Sequence[LiveRun]) -> tuple[tuple[LiveRun, int], ...]:
    """Merge adjacent rows with the same state, repo, and node into one with a count."""
    rows: list[tuple[LiveRun, int]] = []
    for run in outcomes:
        if rows and _outcome_key(rows[-1][0]) == _outcome_key(run):
            rows[-1] = (rows[-1][0], rows[-1][1] + 1)
        else:
            rows.append((run, 1))
    return tuple(rows)


def _outcome_key(run: LiveRun) -> tuple[str, str, str]:
    return (run.state, run.repo, run.node_id)


def machine_for(view: DeckView, station: StationView) -> MachineState | None:
    """The policy machine for a station, joined on node id. Never matched by label."""
    plane = view.control_plane
    node_id = station.station.node_id
    if plane is None or not node_id:
        return None
    for machine in plane.machines:
        if machine.node_id != UNKNOWN and machine.node_id == node_id:
            return machine
    return None


def is_stale(station: StationView, machine: MachineState | None) -> bool:
    """An enrolled station whose hub-reported telemetry is stale or missing."""
    return station.enrolled and machine is not None and machine.unused_reason in STALE_REASONS


def station_status(station: StationView, machine: MachineState | None) -> str:
    """``offline``, ``stale``, ``limited``, ``unknown``, ``working``, or ``idle``. Drives dots and card counts.

    ``unknown`` is an enrolled station with no matching hub machine: its seats
    are neither idle nor offline, so it adds to neither the Idle nor Offline card.

    ``limited`` is a fresh machine whose state is neither ``available`` nor
    unreported (busy, draining, unavailable): it is not offline, but it takes no
    new work, so it adds no idle seats.
    """
    if not station.enrolled:
        return "offline"
    if machine is None:
        return "unknown"
    if is_stale(station, machine):
        return "stale"
    if machine.state == "offline":
        return "offline"
    if machine.state not in ("available", UNKNOWN) or machine.unused_reason in POLICY_LIMITS:
        return "limited"
    return "working" if station.busy else "idle"


def free_seats(station: StationView, machine: MachineState | None) -> int:
    """Seats that can take work now: capacity minus live runs, capped by the hub's slots."""
    base = max(station.station.capacity - station.busy, 0)
    if machine is not None and machine.available_slots.isdigit():
        base = min(base, int(machine.available_slots))
    return base


# --- summary cards ------------------------------------------------------------


@dataclass(frozen=True)
class Summary:
    busy: int
    total: int
    idle: str
    offline: str
    queued: str


def summarize(view: DeckView) -> Summary:
    plane = view.control_plane
    busy = sum(station.busy for station in view.stations)
    total = sum(station.station.capacity for station in view.stations)
    idle = offline = UNKNOWN
    if plane is not None and "machines" in plane.present:
        idle_slots = offline_count = 0
        for station in view.stations:
            machine = machine_for(view, station)
            status = station_status(station, machine)
            if status in {"offline", "stale"}:
                offline_count += 1
            elif status in {"idle", "working"} and machine is not None:
                idle_slots += free_seats(station, machine)
        idle, offline = str(idle_slots), str(offline_count)
    queued = plane.queue.eligible_count if plane is not None and "queue" in plane.present else UNKNOWN
    return Summary(busy=busy, total=total, idle=idle, offline=offline, queued=queued)


def _card(name: str, title: str, value: str, sub: str, bar: str = "") -> str:
    return (
        f'<article class="card card--{name}" data-card="{name}"><h2 class="card-title">{title}</h2>'
        f'<p class="card-value">{_esc(value)}</p><p class="card-sub">{_esc(sub)}</p>{bar}</article>'
    )


def render_summary(view: DeckView) -> str:
    summary = summarize(view)
    working_bar = (
        f'<progress class="bar" value="{summary.busy}" max="{summary.total}" aria-label="Busy slots"></progress>'
        if summary.total > 0
        else ""
    )
    return (
        '<section class="cards" aria-label="Fleet summary">'
        + _card("working", "Working", str(summary.busy), f"of {summary.total} slots", working_bar)
        + _card("idle", "Idle", summary.idle, "free slots, fresh telemetry")
        + _card("offline", "Offline", summary.offline, "stale or offline")
        + _card("queued", "Queued", summary.queued, "eligible to run")
        + "</section>"
    )


# --- alerts ---------------------------------------------------------------------


@dataclass(frozen=True)
class Alert:
    kind: str
    title: str
    detail: str
    since: str


def _first_observed(age_seconds: int | None) -> str:
    return "first observed unknown" if age_seconds is None else f"first observed {format_age(age_seconds)} ago"


def page_alerts(view: DeckView, now: datetime) -> tuple[Alert, ...]:
    """Attention items, then collisions, stale telemetry, and coverage gaps."""
    labels = {station.station.node_id: station.label for station in view.stations}
    attention: list[Alert] = []
    collisions: list[Alert] = []
    for entry in view.rail:
        since = _first_observed(entry.age_seconds)
        if entry.kind == "collision":
            collisions.append(Alert("collision", entry.repo or UNKNOWN, entry.detail, since))
            continue
        where = labels.get(entry.node_id) or entry.node_id[:12]
        attention.append(Alert(entry.kind, f"{entry.repo or UNKNOWN} on {where}", entry.detail, since))
    plane = view.control_plane
    if plane is not None:
        for item in plane.integrations:
            if item.routine_state == "needs-attention":
                detail = f"routine {item.routine_state}" + (f" · {item.detail}" if item.detail else "")
                attention.append(Alert("integration", item.id, detail, _first_observed(None)))
        for blocked in plane.queue.blocked_items:
            reason = deck._cp_text(blocked.get("reason"))
            attention.append(Alert("blocked", deck._cp_text(blocked.get("id")), reason, _first_observed(None)))
        for excluded in plane.queue.actionable_exclusions:
            count = excluded["count"]
            title = f"{count} work item{'' if count == 1 else 's'} blocked"
            attention.append(Alert("blocked", title, deck._cp_text(excluded["reason"]), "observed scan"))
    stale: list[Alert] = []
    coverage: list[Alert] = []
    for station in view.stations:
        machine = machine_for(view, station)
        if machine is not None and is_stale(station, machine):
            seen = seconds_since(machine.last_observed_at, now)
            since = "no telemetry received" if seen is None else f"last telemetry {format_age(seen)} ago"
            stale.append(Alert("stale telemetry", station.label, machine.unused_reason, since))
        elif machine is None and station.enrolled and plane is not None and "machines" in plane.present:
            # A failed node_id join leaves the station's telemetry unknown, so it can never be all clear.
            coverage.append(Alert("no telemetry row", station.label, "node not in the routing snapshot", "ongoing"))
    gaps = deck.coverage_gaps(plane)
    if gaps:
        title = "Control-plane snapshot missing" if plane is None else "Telemetry coverage gaps"
        coverage.append(Alert("coverage", title, "no telemetry for: " + ", ".join(gaps), "ongoing"))
    return tuple(attention + collisions + stale + coverage)


def _alert_html(alert: Alert) -> str:
    kind_class = alert.kind.replace(" ", "-")
    return (
        f'<li class="alert alert--{_esc(kind_class)}"><span class="alert-icon" aria-hidden="true">!</span>'
        f'<div class="alert-body"><p class="alert-title"><span class="alert-kind">{_esc(alert.kind)}</span> '
        f'{_esc(alert.title)}</p><p class="alert-detail">{_esc(alert.detail)}</p>'
        f'<p class="alert-since">{_esc(alert.since)}</p></div></li>'
    )


def render_alerts(view: DeckView, now: datetime) -> str:
    alerts = page_alerts(view, now)
    items = "".join(_alert_html(alert) for alert in alerts)
    body = f'<ul class="alerts">{items}</ul>' if items else '<p class="empty">No active alerts</p>'
    return (
        '<section class="panel" id="alerts" aria-labelledby="alerts-title"><header>'
        f'<h2 id="alerts-title">Active alerts ({len(alerts)})</h2></header>{body}</section>'
    )


# --- header and verdict ---------------------------------------------------------


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def verdict_text(view: DeckView, now: datetime) -> str:
    """The Deck verdict, counting stale telemetry alongside the attention items."""
    alerts = page_alerts(view, now)
    stale = sum(1 for alert in alerts if alert.kind == "stale telemetry")
    count = len(deck.attention_items(view)) + stale
    if count:
        return f"NEEDS ATTENTION ({count})"
    unknown = sum(1 for alert in alerts if alert.kind == "no telemetry row") + len(
        deck.coverage_gaps(view.control_plane)
    )
    return f"COVERAGE UNKNOWN ({unknown})" if unknown else deck.deck_verdict(view)


def render_header(view: DeckView, now: datetime) -> str:
    verdict = verdict_text(view, now)
    tone = "attention" if verdict.startswith("NEEDS") else "unknown" if verdict.startswith("COVERAGE") else "clear"
    slots = sum(station.station.capacity for station in view.stations)
    sub = " · ".join(
        (
            _plural(len(view.stations), "machine"),
            _plural(slots, "slot"),
            _plural(len(view.cloud_workers), "cloud provider"),
        )
    )
    return (
        '<header class="page-head"><div><p class="crumbs">Fleet &rsaquo; Fleet overview</p>'
        f'<h1>Fleet overview <span class="verdict-status verdict-status--{tone}">{_esc(verdict)}</span></h1>'
        f'<p class="page-sub">{_esc(sub)}</p></div>'
        f'<p class="header-meta">Updated {_esc(now.strftime("%H:%M UTC"))} '
        '<a class="refresh-link" href="/deck">Refresh</a></p></header>'
    )


# --- working now and queued -------------------------------------------------------


def _working_row(station: StationView, tile: Tile) -> str:
    run = tile.run
    seat = run.seat or (tile.claim.owner_conductor if tile.claim is not None else "") or UNKNOWN
    params = {"node": run.node_id, "repo": run.repo[:128]}
    if run.bucket in ("stale", "unknown"):
        params["attention"] = "1"  # the board hides these buckets unless attention is on
    href = "/view/machines?" + urlencode(params)
    flag = ' <span class="flag">! collision</span>' if tile.collision else ""
    state = "" if run.bucket == "running" else f' <span class="chip chip-muted">{_esc(run.bucket)}</span>'
    model = run.launch_model or run.model
    model_attr = f' title="{_esc(model)}"' if model else ""
    return (
        f'<li class="row" title="{_esc(run.run_id)}"><span class="dot dot--working" aria-hidden="true"></span>'
        f'<div class="row-body"><p class="row-title"><a href="{_esc(href)}">{_esc(run.repo or UNKNOWN)}</a>'
        f'{flag}{state}</p><p class="row-meta"{model_attr}>{deck.harness_badge_html(run.harness)} '
        f"{_esc(seat)} · {_esc(station.label)}</p></div>"
        f'<span class="row-time">{_esc(format_age(run.elapsed_seconds))}</span></li>'
    )


def _queue_body(plane: deck.ControlPlane | None, *, worklore_enabled: bool) -> str:
    if plane is None or "queue" not in plane.present:
        return '<p class="empty">Queue unknown: the control-plane snapshot has no queue section.</p>'
    queue = plane.queue
    counts = f'<p class="station-meta">eligible {_esc(queue.eligible_count)} · queued {_esc(queue.queued_count)}</p>'
    counts += deck._queue_exclusions_html(queue, worklore_enabled=worklore_enabled)
    rows = "".join(
        '<li class="row"><span class="dot dot--offline" aria-hidden="true"></span><div class="row-body">'
        f'<p class="row-title">{_esc(deck._cp_text(item.get("id")))}</p>'
        f'<p class="row-meta">blocked · {_esc(deck._cp_text(item.get("reason")))}</p></div></li>'
        for item in queue.blocked_items
    )
    if rows:
        return counts + f'<ul class="rows">{rows}</ul>'
    if queue.queued_count == "0":
        return counts + '<p class="empty">Nothing queued.</p>'
    if queue.queued_count == UNKNOWN:
        return counts + '<p class="empty">Queued items unknown.</p>'
    note = (
        "queue exclusions reported above"
        if queue.exclusions
        else "none blocked"
        if queue.blocked_known
        else "blocked reasons unknown"
    )
    return counts + f'<p class="empty">{_esc(queue.queued_count)} queued, {note}.</p>'


def render_work_lists(view: DeckView, *, worklore_enabled: bool = True) -> str:
    rows = "".join(_working_row(station, tile) for station in view.stations for tile in station.tiles)
    count = sum(station.busy for station in view.stations)
    working = f'<ul class="rows">{rows}</ul>' if rows else '<p class="empty">Nothing is running.</p>'
    return (
        '<div class="split"><section class="panel" aria-labelledby="working-title"><header>'
        f'<h2 id="working-title">Working now ({count})</h2></header>{working}</section>'
        '<section class="panel" aria-labelledby="queued-title"><header><h2 id="queued-title">Queued</h2></header>'
        f"{_queue_body(view.control_plane, worklore_enabled=worklore_enabled)}</section></div>"
    )


# --- machines table ---------------------------------------------------------------


SEGMENT_LIMIT = 16


def scaled_segments(counts: tuple[int, int, int], capacity: int) -> tuple[int, int, int]:
    """Spread (busy, idle, off) seat counts over SEGMENT_LIMIT segments.

    Largest remainder of ``count * SEGMENT_LIMIT / capacity``, ties broken in
    category order. A category that has seats always keeps at least one segment,
    taken from the largest category, so a claimed or busy seat never looks free.
    """
    if capacity <= 0:
        return (0, 0, 0)
    exact = [count * SEGMENT_LIMIT / capacity for count in counts]
    units = [int(value) for value in exact]
    order = sorted(range(3), key=lambda index: (-(exact[index] - units[index]), index))
    for index in order[: SEGMENT_LIMIT - sum(units)]:
        units[index] += 1
    for index in range(3):
        if counts[index] > 0 and units[index] == 0:
            donor = max(range(3), key=lambda other: (units[other], -other))
            units[donor] -= 1
            units[index] += 1
    return (units[0], units[1], units[2])


def _seats_html(station: StationView, machine: MachineState | None, status: str) -> str:
    capacity = station.station.capacity
    # Seats on a stale, offline, limited, or unknown machine cannot be offered
    # as idle. The idle count is the Idle card's own formula.
    idle = 0
    off = max(capacity - station.busy, 0)
    if status in ("stale", "offline", "limited", "unknown"):
        word = "unknown" if status == "unknown" else "unavailable"
        text = f"{station.busy} busy, {off} {word}" if station.busy else f"{off} {word}"
    else:
        idle = free_seats(station, machine)
        off -= idle
        text = f"{station.busy} busy, {idle} idle" + (f", {off} claimed" if off else "")
    counts = (min(station.busy, capacity), idle, off)
    wrapper = "seats"
    if capacity > SEGMENT_LIMIT:
        counts = scaled_segments(counts, capacity)
        wrapper = "seats seats--scaled"
    segments = (
        '<span class="seg seg--busy"></span>' * counts[0]
        + '<span class="seg seg--free"></span>' * counts[1]
        + '<span class="seg seg--off"></span>' * counts[2]
    )
    bar = f'<span class="{wrapper}" role="img" aria-label="{_esc(text)}">{segments}</span>'
    return f'{bar}<span class="seat-text">{_esc(text)}</span>'


def _load_cell(machine: MachineState | None) -> str:
    if machine is None:
        return UNKNOWN
    try:
        value = float(machine.load)
    except ValueError:
        return UNKNOWN
    if not math.isfinite(value):
        return UNKNOWN
    percent = round(min(max(value, 0.0), 1.0) * 100)
    return f'{percent}%<progress class="bar bar--thin" value="{percent}" max="100" aria-label="Load"></progress>'


def _machine_row(view: DeckView, station: StationView, now: datetime) -> str:
    machine = machine_for(view, station)
    status = station_status(station, machine)
    enrol = "" if station.enrolled else ' <span class="chip chip-muted">not enrolled</span>'
    os_name = machine.os if machine is not None else UNKNOWN
    claims = machine.active_claims if machine is not None else UNKNOWN
    state = machine.state if machine is not None else UNKNOWN
    reason = machine.unused_reason if machine is not None else UNKNOWN
    reason_html = f'<span class="sub">{_esc(reason)}</span>' if reason != UNKNOWN else ""
    telemetry = format_age(seconds_since(machine.last_observed_at, now)) if machine is not None else UNKNOWN
    last_run = "never" if station.last_heard is None else format_age(seconds_since(station.last_heard, now))
    return (
        f'<tr data-status="{status}"><td><span class="dot dot--{status}" aria-hidden="true"></span> '
        f'<span class="machine-name" title="{_esc(station.station.node_id)}">{_esc(station.label)}</span>{enrol}'
        f'<span class="sub">{_esc(os_name)}</span></td>'
        f"<td>{_seats_html(station, machine, status)}</td><td>{_load_cell(machine)}</td><td>{_esc(claims)}</td>"
        f"<td>{_esc(state)}{reason_html}</td>"
        f'<td>{_esc(telemetry)}<span class="sub">last run {_esc(last_run)}</span></td></tr>'
    )


def render_machine_table(view: DeckView, now: datetime) -> str:
    if not view.stations:
        return (
            '<section class="panel" aria-labelledby="machines-title"><header>'
            '<h2 id="machines-title">Machines (0)</h2></header>'
            '<p class="empty">No stations configured. Start the hub with --deck-config to add them.</p></section>'
        )
    rows = "".join(_machine_row(view, station, now) for station in view.stations)
    return (
        '<section class="panel" id="machines-table" aria-labelledby="machines-title"><header>'
        f'<h2 id="machines-title">Machines ({len(view.stations)})</h2></header>'
        '<div class="table-wrap"><table class="machine-table"><thead><tr><th>Machine</th><th>Seats</th>'
        "<th>Load</th><th>Claims</th><th>State</th><th>Telemetry</th></tr></thead>"
        f"<tbody>{rows}</tbody></table></div></section>"
    )


# --- below the fold ---------------------------------------------------------------


def _cloud_worker_html(worker: deck.CloudWorker) -> str:
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
        f"<h3>{deck.provider_badge_html(worker.provider)} {_esc(worker.provider)}</h3>"
        f'<p class="cloud-capacity">{worker.used}/{worker.limit}</p>'
        f'</header><p class="cloud-state">circuit: {_esc(worker.circuit_state)}</p>'
        + (
            f'<ul class="cloud-lease-list">{lease_items}</ul>'
            if lease_items
            else '<p class="empty">No active leases.</p>'
        )
        + "</article>"
    )


def render_cloud_workers(view: DeckView) -> str:
    cards = "".join(_cloud_worker_html(worker) for worker in view.cloud_workers)
    return (
        '<section class="panel" aria-labelledby="cloud-workers"><header><h2 id="cloud-workers">Cloud workers</h2>'
        f'<p class="panel-count">{len(view.cloud_workers)} provider(s)</p></header>'
        + (
            f'<div class="cloud-workers">{cards}</div>'
            if cards
            else '<p class="empty">No cloud providers configured.</p>'
        )
        + "</section>"
    )


def _session_row(session: deck.InteractiveSession) -> str:
    dirty = f"{len(session.dirty_paths)}+" if session.dirty_truncated else str(len(session.dirty_paths))
    return (
        f'<tr><td title="{_esc(session.checkout_path)}">{_esc(session.repo_label or session.repo_identity)}</td>'
        f"<td>{deck.harness_badge_html(session.harness)} "
        f"{_esc(session.harness or UNKNOWN)}/{_esc(session.session_id)}</td>"
        f"<td>{_esc(session.branch or '-')}</td><td>{_esc(dirty)}</td></tr>"
    )


def render_sessions(view: DeckView) -> str:
    rows = "".join(_session_row(session) for session in view.interactive_sessions)
    body = (
        '<div class="table-wrap"><table class="session-table"><thead><tr><th>Repo</th><th>Session</th>'
        f"<th>Branch</th><th>Dirty</th></tr></thead><tbody>{rows}</tbody></table></div>"
        if rows
        else '<p class="empty">No interactive sessions.</p>'
    )
    return (
        '<section class="panel" aria-labelledby="interactive-sessions"><header>'
        '<h2 id="interactive-sessions">Interactive sessions</h2>'
        f'<p class="panel-count">{len(view.interactive_sessions)} session(s)</p></header>{body}</section>'
    )


def render_outcomes(view: DeckView) -> str:
    labels = {station.station.node_id: station.label for station in view.stations}
    items = "".join(
        '<li><span class="timeline-state">'
        f"{_esc(run.state)}</span><span>{_esc(run.repo)} · {_esc(labels.get(run.node_id) or run.node_id[:12])}</span>"
        + (
            f"<span>×{count}</span>"
            if count > 1
            else f'<span title="{_esc(run.run_id)}">{_esc(run.run_id[:12])}</span>'
        )
        + "</li>"
        for run, count in collapse_outcomes(view.outcomes)
    )
    return (
        '<section class="panel" aria-labelledby="timeline"><header><h2 id="timeline">Recent outcomes</h2>'
        f'<p class="panel-count">{len(view.outcomes)} shown</p></header>'
        + (f'<ul class="timeline">{items}</ul>' if items else '<p class="empty">No recent outcomes.</p>')
        + "</section>"
    )


def render_observed_groups(view: DeckView, now: datetime) -> str:
    """Operator-configured metadata, folded away because it is not live activity."""
    cards = ""
    for group in view.observed_work_groups:
        stamp, freshness = deck.snapshot_freshness(
            group.snapshot_observed_at, now=now, stale_after_seconds=view.stale_after_seconds
        )
        cards += (
            '<article class="panel"><h3><a href="/deck/observed/'
            + _esc(group.key)
            + '">'
            + _esc(group.label)
            + "</a></h3><p>"
            + str(len(group.work_ids))
            + " configured observation records</p><p>Coverage: "
            + _esc(group.coverage)
            + "</p><p>Configured source: "
            + _esc(group.source_ref)
            + "<br>Configured proxy: "
            + _esc(group.proxy_ref)
            + "<br>Configured parent: "
            + _esc(group.parent_ref)
            + "</p><p>Snapshot observation time (operator-configured metadata): "
            + _esc(stamp)
            + f"<br>Snapshot freshness: {freshness}. Stale after {view.stale_after_seconds} seconds.</p></article>"
        )
    return (
        f"<details><summary>Observed work groups ({len(view.observed_work_groups)})</summary>"
        "<p>Retained task observations. Counts describe configured records. "
        "Recent observation metadata does not establish live activity.</p>" + cards + "</details>"
    )


def render_observers(view: DeckView, now: datetime) -> str:
    items = "".join(
        f'<li><span title="{_esc(node)}">{_esc(node[:12])}</span> &middot; '
        f"heard {_esc(format_age(seconds_since(received, now)))} ago</li>"
        for node, received in view.observers
    )
    return (
        f"<details><summary>Other observers ({len(view.observers)})</summary>"
        f'<ul class="observer-list">{items}</ul></details>'
    )


def render_overview(view: DeckView, now: datetime, *, worklore_enabled: bool) -> str:
    """The whole main column for ``/deck``."""
    parts = [
        '<main class="deck-shell overview">',
        render_header(view, now),
        render_summary(view),
        render_alerts(view, now),
        render_work_lists(view, worklore_enabled=worklore_enabled),
        render_machine_table(view, now),
        render_cloud_workers(view),
        render_sessions(view),
        render_outcomes(view),
        "<details><summary>Control plane details</summary>"
        + deck.render_control_plane(view.control_plane, worklore_enabled=worklore_enabled)
        + "</details>",
    ]
    if worklore_enabled and view.observed_work_groups:
        parts.append(render_observed_groups(view, now))
    parts.append(render_observers(view, now))
    parts.append('<footer><a href="/view/machines">machines board</a> <a href="/view/repos">repos board</a></footer>')
    parts.append("</main>")
    return "\n".join(parts)


# --- sidebar ----------------------------------------------------------------------


NAV_ITEMS = (
    ("overview", "/deck", "overview"),
    ("repos", "/deck/repos", "repos"),
    ("work", "/deck/work", "work"),
    ("roster", "/deck/roster", "roster"),
    ("policy", "/deck/policy", "policy"),
    ("owners", "/deck/owners", "owners"),
    ("stats", "/deck/stats", "stats"),
    ("boards", "/view/machines", "boards"),
)


def _tree_summary(station: StationView, status: str, machine_state: str = UNKNOWN) -> str:
    if status == "offline":
        return "not enrolled" if not station.enrolled else "offline"
    if status == "stale":
        return "stale"
    if status == "limited":
        return machine_state
    if status == "unknown":
        return "unknown"
    return f"{station.busy} busy" if station.busy else "idle"


def _tree_run(tile: Tile) -> str:
    run = tile.run
    return (
        f'<li class="tree-run"><span class="tree-run-name">{_esc(run.repo or UNKNOWN)} · {_esc(run.seat or UNKNOWN)}'
        f'</span><span class="tree-time">{_esc(format_age(run.elapsed_seconds))}</span></li>'
    )


def _machine_tree(view: DeckView) -> str:
    rows = []
    for station in view.stations:
        machine = machine_for(view, station)
        status = station_status(station, machine)
        machine_state = POLICY_LIMITS.get(machine.unused_reason, machine.state) if machine is not None else UNKNOWN
        runs = "".join(_tree_run(tile) for tile in station.tiles)
        rows.append(
            f'<li class="tree-row" data-filter-row title="{_esc(station.station.node_id)}"><div class="tree-line">'
            f'<span class="dot dot--{status}" aria-hidden="true"></span>'
            f'<span class="tree-label">{_esc(station.label)}</span>'
            f'<span class="tree-meta">{_esc(_tree_summary(station, status, machine_state))}</span></div>'
            + (f'<ul class="tree-children">{runs}</ul>' if runs else "")
            + "</li>"
        )
    body = f'<ul class="tree-list">{"".join(rows)}</ul>' if rows else '<p class="tree-empty">No stations.</p>'
    return (
        '<section class="tree" aria-labelledby="tree-machines"><h2 id="tree-machines" class="tree-heading">'
        f'Machines <span class="nav-count">{len(view.stations)}</span></h2>{body}</section>'
    )


def _cloud_tree(view: DeckView) -> str:
    rows = "".join(
        '<li class="tree-row" data-filter-row><div class="tree-line">'
        f'<span class="tree-label">{_esc(worker.provider)}</span>'
        f'<span class="tree-meta">{worker.used}/{worker.limit}'
        + ("" if worker.circuit_state == "closed" else f" · {_esc(worker.circuit_state)}")
        + "</span></div></li>"
        for worker in view.cloud_workers
    )
    body = f'<ul class="tree-list">{rows}</ul>' if rows else '<p class="tree-empty">None configured.</p>'
    return (
        '<section class="tree" aria-labelledby="tree-cloud"><h2 id="tree-cloud" class="tree-heading">'
        f'Cloud <span class="nav-count">{len(view.cloud_workers)}</span></h2>{body}</section>'
    )


def _sidebar_foot(view: DeckView, now: datetime) -> str:
    plane = view.control_plane
    ages = [] if plane is None else [seconds_since(machine.last_observed_at, now) for machine in plane.machines]
    known = [age for age in ages if age is not None]
    text = f"Hub telemetry {format_age(min(known))} ago" if known else "Hub telemetry unknown"
    return f'<p class="sidebar-foot">{_esc(text)}</p>'


def render_sidebar(view: DeckView | None, active: str, now: datetime, *, worklore_enabled: bool) -> str:
    """Brand, page nav, and (when a view is available) the machine and cloud trees.

    Pages that do not build a ``DeckView`` pass ``None`` and get the nav only.
    """
    links = []
    for key, href, label in NAV_ITEMS:
        if key == "work" and not worklore_enabled:
            continue
        current = ' aria-current="page"' if key == active else ""
        count = (
            f' <span class="nav-count">{len(view.stations)}</span>' if key == "overview" and view is not None else ""
        )
        links.append(f'<a href="{href}"{current}>{label}{count}</a>')
    parts = [
        '<aside class="shell-sidebar" aria-label="Fleet navigation"><div class="brand">'
        '<p class="brand-name">Brigade Fleet Hub</p><p class="brand-sub">self-hosted</p></div>',
        f'<nav class="deck-nav" aria-label="Command Deck">{"".join(links)}</nav>',
    ]
    if view is not None:
        parts += ['<div class="tree-filter" data-tree-filter></div>', _machine_tree(view), _cloud_tree(view)]
        parts.append(_sidebar_foot(view, now))
    parts.append("</aside>")
    return "".join(parts)
