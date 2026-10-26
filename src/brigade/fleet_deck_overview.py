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

from . import fleet_action_sources, fleet_command_deck as deck, ui_theme
from .fleet_action_auth import canonical
from . import fleet_deck_activity as activity
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
    """Spare configured seats, capped by reported slots; not verified work eligibility."""
    base = max(station.station.capacity - station.busy, 0)
    if machine is not None and machine.available_slots.isdigit():
        base = min(base, int(machine.available_slots))
    return base


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
    """Secondary reported counts, kept inside the machine panel's context."""
    summary = summarize(view)
    running = sum(tile.run.bucket == "running" for station in view.stations for tile in station.tiles)
    working_bar = (
        f'<progress class="bar" value="{summary.busy}" max="{summary.total}" aria-label="Busy slots"></progress>'
        if summary.total > 0
        else ""
    )
    queue = (
        view.control_plane.queue if view.control_plane is not None and "queue" in view.control_plane.present else None
    )
    return (
        '<section class="reported-counts" aria-label="Fleet summary">'
        + _card(
            "working",
            "Running",
            str(running),
            f"{summary.busy} reported jobs of {summary.total} configured slots",
            working_bar,
        )
        + _card("queued", "Ready to run", summary.queued, "queue eligibility")
        + _card("not-ready", "Not ready", _not_ready(queue), "excluded or blocked; review reasons")
        + _card("idle", "Spare configured slots", summary.idle, "fresh telemetry; usability not verified")
        + _card("offline", "Offline", summary.offline, "stale or offline")
        + "</section>"
    )


def _not_ready(queue: deck.QueueState | None) -> str:
    if queue is None or not queue.complete or not queue.blocked_known:
        return UNKNOWN
    return str(sum(int(deck._cp_text(item["count"])) for item in queue.exclusions) + len(queue.blocked_items))


@dataclass(frozen=True)
class Alert:
    kind: str
    title: str
    detail: str
    since: str
    source_ref: str = ""


def _first_observed(age_seconds: int | None) -> str:
    return "first observed unknown" if age_seconds is None else f"first observed {format_age(age_seconds)} ago"


def page_alerts(view: DeckView, now: datetime) -> tuple[Alert, ...]:
    """Attention items, then collisions, stale telemetry, and coverage gaps."""
    labels = {station.station.node_id: station.label for station in view.stations}
    attention: list[Alert] = []
    collisions: list[Alert] = []
    for entry in view.rail:
        if entry.kind == "parallel":
            continue
        since = _first_observed(entry.age_seconds)
        if entry.kind == "collision":
            collisions.append(Alert("collision", entry.repo or UNKNOWN, entry.detail, since))
            continue
        where = labels.get(entry.node_id) or entry.node_id[:12]
        attention.append(
            Alert(
                entry.kind,
                f"{entry.repo or UNKNOWN} on {where}",
                entry.detail,
                since,
                canonical({"kind": "run", "node_id": entry.node_id, "run_id": entry.run_id})
                if entry.node_id and entry.run_id
                else "",
            )
        )
    plane = view.control_plane
    if plane is not None:
        for item in plane.integrations:
            if item.routine_state == "needs-attention":
                detail = f"routine {item.routine_state}" + (f" · {item.detail}" if item.detail else "")
                attention.append(Alert("integration", item.id, detail, _first_observed(None)))
        for blocked in plane.queue.blocked_items:
            reason = deck._cp_text(blocked.get("reason"))
            attention.append(
                Alert(
                    "blocked",
                    deck._cp_text(blocked.get("id")),
                    reason,
                    _first_observed(None),
                    fleet_action_sources.work_reference(blocked),
                )
            )
        for excluded in plane.queue.actionable_exclusions:
            count = excluded["count"]
            title = f"{count} work item{'' if count == 1 else 's'} blocked"
            attention.append(Alert("blocked", title, deck._cp_text(excluded["reason"]), "reported by queue"))
    stale: list[Alert] = []
    coverage: list[Alert] = []
    for station in view.stations:
        machine = machine_for(view, station)
        if machine is not None and is_stale(station, machine):
            seen = seconds_since(machine.last_observed_at, now)
            since = "no telemetry received" if seen is None else f"last telemetry {format_age(seen)} ago"
            stale.append(
                Alert(
                    "stale telemetry",
                    station.label,
                    machine.unused_reason,
                    since,
                    canonical({"kind": "telemetry", "node_id": station.station.node_id}) if station.enrolled else "",
                )
            )
        elif machine is None and station.enrolled and plane is not None and "machines" in plane.present:
            # A failed node_id join leaves the station's telemetry unknown, so it can never be all clear.
            coverage.append(Alert("no telemetry row", station.label, "node not in the routing snapshot", "ongoing"))
    gaps = deck.coverage_gaps(plane)
    if gaps:
        title = "Control-plane snapshot missing" if plane is None else "Telemetry coverage gaps"
        coverage.append(Alert("coverage", title, "no telemetry for: " + ", ".join(gaps), "ongoing"))
    return tuple(attention + collisions + stale + coverage)


def render_attention(
    view: DeckView, now: datetime, *, action_sources: frozenset[str], worklore_enabled: bool = True
) -> str:
    from .fleet_deck_alerts import render_alert_rows

    alerts = page_alerts(view, now)
    remaining = len(alerts) - 3
    return (
        '<section class="panel" aria-labelledby="attention-title"><header><h2 id="attention-title">Needs attention</h2>'
        '<a href="/deck/alerts">View all alerts</a></header>'
        + render_alert_rows(alerts[:3], action_sources=action_sources, compact=True, worklore_enabled=worklore_enabled)
        + (f'<p class="station-meta">{remaining} more alerts</p>' if remaining > 0 else "")
        + "</section>"
    )


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


def _verdict_display(verdict: str) -> str:
    for original, display in (("NEEDS ATTENTION", "Needs attention"), ("COVERAGE UNKNOWN", "Coverage unknown")):
        if verdict.startswith(original):
            return display + verdict[len(original) :]
    return "All clear" if verdict == "ALL CLEAR" else verdict


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
        '<header class="page-head"><div>'
        f'<h1>Fleet overview <span class="verdict-status verdict-status--{tone}">{_esc(_verdict_display(verdict))}</span></h1>'
        f'<p class="page-sub">{_esc(sub)}</p></div>'
        f'<p class="header-meta">Updated {_esc(now.strftime("%H:%M UTC"))} '
        '<a class="refresh-link" href="/deck">Refresh</a></p></header>'
    )


def _working_row(station: StationView, tile: Tile, parallel_detail: str = "", *, overlap: bool = False) -> str:
    run = tile.run
    seat = run.seat or (tile.claim.owner_conductor if tile.claim is not None else "") or UNKNOWN
    params = {"node": run.node_id, "repo": run.repo[:128]}
    if run.bucket in ("stale", "unknown"):
        params["attention"] = "1"  # the board hides these buckets unless attention is on
    href = "/view/machines?" + urlencode(params)
    parallel = activity.parallel_note(parallel_detail, overlap=overlap) if tile.collision or overlap else ""
    model = run.launch_model or run.model
    model_attr = f' title="{_esc(model)}"' if model else ""
    return (
        '<li class="activity-card" data-activity-kind="managed">'
        f'<div class="activity-head">{activity.status_badge(run.bucket)}<span class="activity-kind">Managed</span></div>'
        f'<p class="row-title"><a href="{_esc(href)}">{_esc(run.repo or UNKNOWN)}</a></p>'
        f'<p class="row-meta"{model_attr}>{deck.harness_badge_html(run.harness)} {_esc(seat)} · {_esc(station.label)}</p>'
        f'<p class="activity-fact">{_esc(format_age(run.elapsed_seconds))} elapsed</p>{parallel}'
        '<div class="activity-footer"><details><summary>Details</summary>'
        f"<p>Run: {_esc(run.run_id)}<br>Node: {_esc(run.node_id)}<br>State: {_esc(run.state)}"
        f"<br>Repository: {_esc(run.repo_identity or UNKNOWN)}<br>Provider: {_esc(run.provider or UNKNOWN)}"
        f"<br>Model: {_esc(model or UNKNOWN)}<br>Station last heard: {_esc(station.last_heard or UNKNOWN)}</p>"
        f'</details><span class="activity-age">evidence {_esc(format_age(run.age_seconds))} ago</span></div></li>'
    )


def _queue_body(plane: deck.ControlPlane | None, *, worklore_enabled: bool) -> str:
    if plane is None or "queue" not in plane.present:
        return '<p class="empty">Queue unknown: the control-plane snapshot has no queue section.</p>'
    queue = plane.queue
    rows = "".join(
        '<li class="row"><div class="row-body">'
        f'<p class="row-title">{_esc(deck._cp_text(item.get("id")))}</p>'
        f'<p class="row-meta">blocked · {_esc(activity.REASONS.get(deck._cp_text(item.get("reason")), deck._cp_text(item.get("reason"))))}</p></div></li>'
        for item in queue.blocked_items
    )
    reasons = f'<ul class="rows">{rows}</ul>' if rows else ""
    if not queue.blocked_known:
        reasons += '<p class="empty">Blocked reasons unknown.</p>'
    return (
        f'<p class="station-meta">{_esc(queue.eligible_count)} ready to run · Not ready: {_esc(_not_ready(queue))}</p>'
        "<details><summary>Queue counts and reasons</summary>"
        + activity.queue_counts(queue)
        + deck._queue_exclusions_html(queue, worklore_enabled=worklore_enabled)
        + reasons
        + "</details>"
    )


def render_ready_queue(view: DeckView, *, worklore_enabled: bool) -> str:
    return (
        '<section class="panel" aria-labelledby="queued-title"><header><h2 id="queued-title">Ready queue</h2>'
        '<a class="card-link" href="/view/machines">View runs</a></header>'
        + _queue_body(view.control_plane, worklore_enabled=worklore_enabled)
        + "</section>"
    )


def render_work_lists(view: DeckView, *, worklore_enabled: bool = True, include_queue: bool = True) -> str:
    parallel = {entry.repo: entry.detail for entry in view.rail if entry.kind == "parallel"}
    overlaps = {row.target for row in view.repos if row.interactive_overlap}
    jobs = [(station, tile) for station in view.stations for tile in station.tiles]

    def rows(items: list[tuple[StationView, Tile]]) -> str:
        return (
            '<ul class="activity-cards">'
            + "".join(
                _working_row(station, tile, parallel.get(tile.run.repo, ""), overlap=tile.run.repo in overlaps)
                for station, tile in items
            )
            + "</ul>"
        )

    working = rows(jobs[:3]) if jobs else '<p class="empty">No managed jobs reported.</p>'
    if len(jobs) > 3:
        working += f"<details><summary>More managed jobs ({len(jobs) - 3})</summary>{rows(jobs[3:])}</details>"
    running = sum(tile.run.bucket == "running" for _, tile in jobs)
    return (
        '<section class="panel" aria-labelledby="working-title"><header><h2 id="working-title">Activity</h2>'
        '<a class="card-link" href="/view/machines">View runs</a></header>'
        f'<p class="station-meta">Running: {running} · {len(jobs)} managed jobs reported.</p>'
        f"{working}{activity.render_cloud_presence(view, preview_limit=3)}</section>"
        + (render_ready_queue(view, worklore_enabled=worklore_enabled) if include_queue else "")
    )


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
    return (
        f'<td title="{_esc(text)}">{bar}'
        f'<span class="seat-text mono" aria-hidden="true">{station.busy} / {capacity}</span></td>'
    )


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


def machine_notes(station: StationView, machine: MachineState | None, status: str) -> tuple[tuple[str, bool], ...]:
    """Status and positive claims notes in display order, before HTML escaping."""
    notes: list[tuple[str, bool]] = []
    if status == "stale":
        notes.append(("stale", True))
    elif status == "limited" and machine is not None:
        notes.append((POLICY_LIMITS.get(machine.unused_reason, machine.state), False))
    elif status == "offline" and station.enrolled:
        notes.append(("offline", False))
    elif status == "unknown":
        notes.append(("no telemetry", False))
    if machine is not None and machine.active_claims.isdigit():
        try:
            claims = int(machine.active_claims)
        except ValueError:
            claims = 0
        if claims > 0:
            notes.append((_plural(claims, "claim"), False))
    return tuple(notes)


def _machine_row(view: DeckView, station: StationView, now: datetime, *, show_notes: bool) -> str:
    machine = machine_for(view, station)
    status = station_status(station, machine)
    enrol = "" if station.enrolled else ' <span class="chip chip-muted">not enrolled</span>'
    os_name = machine.os if machine is not None else UNKNOWN
    state = machine.state if machine is not None else UNKNOWN
    reason = machine.unused_reason if machine is not None else UNKNOWN
    telemetry = format_age(seconds_since(machine.last_observed_at, now)) if machine is not None else UNKNOWN
    last_run = "never" if station.last_heard is None else format_age(seconds_since(station.last_heard, now))
    notes = " ".join(
        f'<span class="tag{" tag--warn" if warn else ""}">{_esc(label)}</span>'
        for label, warn in machine_notes(station, machine, status)
    )
    notes_cell = f"<td>{notes}</td>" if show_notes else ""
    seen_class = " seen--stale" if status == "stale" else ""
    seen = f"stale {telemetry}" if status == "stale" and telemetry != UNKNOWN else telemetry
    return (
        f'<tr data-status="{_esc(status)}" title="state {_esc(state)} · reason {_esc(reason)} · '
        f'last run {_esc(last_run)} · node {_esc(station.station.node_id)}"><td>'
        f'<span class="machine-name" title="{_esc(station.station.node_id)}">{_esc(station.label)}</span>{enrol}'
        f'<span class="sub-inline">{_esc(os_name)}</span></td>'
        f"{_seats_html(station, machine, status)}<td>{_load_cell(machine)}</td>{notes_cell}"
        f'<td class="seen mono{seen_class}">{_esc(seen)}</td></tr>'
    )


def render_machine_table(view: DeckView, now: datetime, *, include_context: bool = False) -> str:
    header = (
        '<section class="panel" id="machines-table" aria-labelledby="machines-title"><header><div>'
        f'<h2 id="machines-title">Machines ({len(view.stations)})</h2>'
        '<p class="seat-legend" aria-hidden="true"><span class="legend-swatch legend-swatch--busy"></span>busy '
        '<span class="legend-swatch legend-swatch--free"></span>idle '
        '<span class="legend-swatch legend-swatch--off"></span>unavailable</p></div>'
        '<a class="card-link" href="/deck/capacity">View capacity</a></header>'
    )
    context = (
        "<details><summary>Reported counts and capacity context</summary>"
        '<p class="station-meta">Usable for selected work: unknown. Configured slots describe reported capacity. '
        '<a href="/deck/capacity">Capacity details</a></p>'
        "<p>Authentication, provider readiness, policy eligibility and shared quotas are not verified here. "
        "Local and cloud pools may overlap and are not added together.</p>" + render_summary(view) + "</details>"
        if include_context
        else ""
    )
    if not view.stations:
        return (
            header
            + '<p class="empty">No stations configured. Start the hub with --deck-config to add them.</p>'
            + context
            + "</section>"
        )
    show_notes = any(
        machine_notes(station, machine_for(view, station), station_status(station, machine_for(view, station)))
        for station in view.stations
    )
    rows = "".join(_machine_row(view, station, now, show_notes=show_notes) for station in view.stations)
    table_class = "machine-table" + ("" if show_notes else " machine-table--no-notes")
    notes_header = '<th><span class="visually-hidden">Notes</span></th>' if show_notes else ""
    return (
        header + f'<div class="table-wrap"><table class="{table_class}"><thead><tr><th>Machine</th><th>Seats</th>'
        f"<th>Load</th>{notes_header}<th>Seen</th></tr></thead>"
        f"<tbody>{rows}</tbody></table></div>{context}</section>"
    )


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
        '<section class="panel" aria-labelledby="cloud-workers"><header><h2 id="cloud-workers">Provider leases</h2></header>'
        + activity.render_provider_leases(view)
        + f"<details><summary>Provider details ({len(view.cloud_workers)})</summary>"
        '<p class="station-meta">Lease inventory does not cover reported agent presence.</p>'
        + (
            f'<div class="cloud-workers">{cards}</div>'
            if cards
            else '<p class="empty">No cloud providers configured.</p>'
        )
        + "</details></section>"
    )


def render_sessions(view: DeckView) -> str:
    return activity.render_sessions(view)


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
            f'<article class="panel"><h3><a href="/deck/observed/{_esc(group.key)}">{_esc(group.label)}</a></h3>'
            f"<p>{len(group.work_ids)} configured observation records</p><p>Coverage: {_esc(group.coverage)}</p>"
            f"<p>Configured source: {_esc(group.source_ref)}<br>Configured proxy: {_esc(group.proxy_ref)}"
            f"<br>Configured parent: {_esc(group.parent_ref)}</p>"
            f"<p>Snapshot observation time (operator-configured metadata): {_esc(stamp)}"
            f"<br>Snapshot freshness: {freshness}. Stale after {view.stale_after_seconds} seconds.</p></article>"
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


def render_overview(
    view: DeckView, now: datetime, *, worklore_enabled: bool, action_sources: frozenset[str] = frozenset()
) -> str:
    """The whole main column for ``/deck``."""
    parts = [
        '<main class="deck-shell overview">',
        render_header(view, now),
        '<div class="overview-grid">',
        render_attention(view, now, action_sources=action_sources, worklore_enabled=worklore_enabled),
        render_work_lists(view, worklore_enabled=worklore_enabled, include_queue=False),
        render_machine_table(view, now, include_context=True),
        render_ready_queue(view, worklore_enabled=worklore_enabled),
        "</div>",
        "<details><summary>Fleet details</summary>",
        "<details><summary>Reported session presence</summary><p>Sessions report presence and may be idle or overlap managed jobs.</p>"
        + render_sessions(view)
        + "</details>",
        activity.render_cloud_report_history(view),
        "<details><summary>Cloud diagnostics</summary>" + render_cloud_workers(view) + "</details>",
        "<details><summary>Recent outcomes</summary>" + render_outcomes(view) + "</details>",
        "<details><summary>Control plane details</summary>"
        + deck.render_control_plane(view.control_plane, worklore_enabled=worklore_enabled)
        + "</details>",
    ]
    if worklore_enabled and view.observed_work_groups:
        parts.append(render_observed_groups(view, now))
    parts.extend(
        [
            render_observers(view, now),
            "</details>",
            '<footer><a href="/view/machines">machines board</a> <a href="/view/repos">repos board</a></footer>',
            "</main>",
        ]
    )
    return "\n".join(parts)


def render_sidebar(view: DeckView | None, active: str, now: datetime, *, worklore_enabled: bool) -> str:
    """Delegate to the shared sidebar without a module-level circular import."""
    from . import fleet_deck_sidebar

    return fleet_deck_sidebar.render_sidebar(view, active, now, worklore_enabled=worklore_enabled)


def render_capacity_page(view: DeckView, now: datetime, *, nonce: str, worklore_enabled: bool) -> str:
    """Existing frozen Deck projection, with no extra collection or admission."""
    body = (
        '<main class="deck-shell overview capacity-page"><header class="page-head"><div><h1>Capacity</h1>'
        '<p class="page-sub">Configured capacity and reported observations</p></div></header>'
        + render_machine_table(view, now)
        + activity.render_octopool_observation(view)
        + '<details id="cloud-details"><summary>Advanced resource details</summary><p>Usable for selected work: unknown. Configured slots are not verified usable workers. Authentication, provider readiness, repository policy eligibility and shared quota may limit dispatch. Local and cloud pools may overlap and are not added together.</p>'
        + render_cloud_workers(view)
        + deck.render_control_plane(view.control_plane, worklore_enabled=worklore_enabled)
        + "</details></main>"
    )
    return deck._document(
        body,
        nonce=nonce,
        now=now,
        title="Capacity",
        active="capacity",
        view=view,
        worklore_enabled=worklore_enabled,
        extra_css=ui_theme.OVERVIEW_CSS,
    )
