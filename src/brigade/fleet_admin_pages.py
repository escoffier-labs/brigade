"""Operational admin views over existing projections, without collection or writes."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any, Mapping
from urllib.parse import parse_qs, urlencode

from . import fleet_admin_explorer as explorer
from . import fleet_investigation as investigation
from . import fleet_project_board as project_board_store
from . import fleet_project_board_page as board_page
from . import fleet_projects_prs as pull_requests
from . import fleet_command_deck as deck
from . import fleet_deck_activity as activity
from . import fleet_deck_overview as overview
from .fleet_machine_label import status_label
from . import worklore_campaigns as campaigns
from .fleet_campaign import MAX_MEMBERS
from .fleet_work_page import _reference_url
from .fleet_deck_empty_copy import COPY
from .ui_theme import empty_state
from .worklore_store import WorkloreNotFound

_esc = deck._esc
PROJECT_LIMIT = 20


def facts(values: Mapping[str, object], *, audience: str = "") -> str:
    return explorer.facts(values, audience=audience)


def actions(*links: str) -> str:
    return '<div class="explorer-actions">' + "".join(links) + "</div>"


def document(
    content: str,
    view: deck.DeckView | None,
    now: datetime,
    *,
    nonce: str,
    title: str,
    active: str,
    worklore_enabled: bool,
) -> str:
    return deck._document(
        f'<main class="deck-shell admin-page">{content}</main>',
        nonce=nonce,
        now=now,
        title=title,
        active=active,
        view=view,
        worklore_enabled=worklore_enabled,
        refresh=False,
        extra_css=explorer.CSS,
        script=explorer.SCRIPT,
    )


def activity_entries(view: deck.DeckView, now: datetime | None = None) -> list[explorer.Entry]:
    entries: list[explorer.Entry] = []
    seen: set[str] = set()

    def add(key: str, title: str, status: str, detail: str) -> None:
        if key not in seen:
            seen.add(key)
            entries.append(explorer.Entry(key, title, status, detail))

    for kind, runs in (
        ("Managed run", [tile.run for station in view.stations for tile in station.tiles]),
        ("Recorded outcome", list(view.outcomes)),
    ):
        for run in runs:
            href = "/view/machines?" + urlencode({"node": run.node_id, "repo": run.repo, "all": "1"})
            add(
                kind + ":" + run.node_id + ":" + run.run_id,
                run.repo or "Repository unknown",
                kind + " · " + (outcome_label(run.state) if kind == "Recorded outcome" else run.bucket),
                explorer.hero(
                    run.repo or "Repository unknown",
                    outcome_label(run.state) if kind == "Recorded outcome" else run.bucket,
                )
                + facts(
                    {
                        "What": kind,
                        "Owner": run.seat,
                        "Model": run.launch_model or run.model,
                        "Elapsed": overview.format_age(run.elapsed_seconds),
                        "Repository": run.repo,
                    }
                )
                + explorer.agent(
                    "<h3>Evidence</h3>"
                    + facts(
                        {
                            "Reported code": run.state,
                            "Run": run.run_id,
                            "Node": run.node_id,
                            "Repository identity": run.repo_identity,
                            "Evidence age": overview.format_age(run.age_seconds),
                        }
                    )
                )
                + actions(f'<a href="{_esc(href)}">Open run history</a>')
                + (
                    investigation.render(
                        run.repo,
                        route="/deck/activity",
                        observation=f"{now or 'page snapshot'}; evidence age {run.age_seconds} seconds",
                        facts={
                            "Kind": kind,
                            "State/code": run.state,
                            "Run": run.run_id,
                            "Node": run.node_id,
                            "Repository identity": run.repo_identity,
                            "Owner": run.seat,
                            "Model": run.launch_model or run.model,
                            "Elapsed seconds": run.elapsed_seconds,
                        },
                        limits="Managed run report; historical outcomes do not establish current execution. Exact exception details not collected.",
                    )
                    if investigation.needed(run.state)
                    or investigation.needed(run.bucket)
                    or (kind == "Recorded outcome" and outcome_label(run.state) == "unknown")
                    else ""
                ),
            )
    for historical, sessions in ((False, view.interactive_sessions), (True, view.recent_cloud_reports)):
        for session in sessions:
            context = session.cloud_context
            if context is None or context.version != 2:
                title = (
                    (context.agent_label if context else session.harness) + " · " + (session.repo_label or "Session")
                )
                href = "/view/machines?" + urlencode({"node": session.node_id, "all": "1"})
                add(
                    "Session report:"
                    + ":".join(
                        (
                            session.node_id,
                            session.harness,
                            session.session_id,
                            session.repo_identity,
                            session.checkout_path,
                        )
                    ),
                    title,
                    "Session report · Presence only · " + session.presence_state,
                    explorer.hero(title, session.presence_state)
                    + "<p>Presence only; running work unobserved.</p>"
                    + facts(
                        {
                            "Repository": session.repo_identity or session.repo_label,
                            "Harness": session.harness,
                            "Branch": session.branch,
                            "Checkout": session.checkout_path,
                        }
                    )
                    + explorer.agent(
                        "<h3>Evidence</h3>"
                        + facts(
                            {
                                "Freshness": session.presence_state,
                                "Session": session.session_id,
                                "Reporter": session.node_id,
                                "Reported changed paths": ", ".join(session.dirty_paths),
                            }
                        )
                        + (
                            facts(
                                {
                                    "Provider": context.provider,
                                    "Source": context.source,
                                    "Scope": context.source_scope,
                                    "Parent": context.parent_session_id,
                                    "Work": context.work_id,
                                    "Coverage": context.coverage,
                                    "Observed": context.observed_at,
                                }
                            )
                            if context
                            else ""
                        )
                    )
                    + actions(f'<a href="{_esc(href)}">Open machine history</a>'),
                )
                continue
            kind = "Reported outcome" if historical else "Reported task"
            title = context.task or "Untitled reported task"
            add(
                kind + ":" + session.node_id + ":" + session.session_id,
                title,
                kind + " · " + context.reported_state + (" · historical" if historical else ""),
                explorer.hero(title, context.reported_state)
                + explorer.agent(
                    ("Historical report. " if historical else "") + "Provider lifecycle unverified.", tag="p"
                )
                + facts(
                    {
                        "What": "Reported outcome" if historical else "Reported task",
                        "Repository": session.repo_identity or session.repo_label,
                        "Owner": context.agent_label,
                        "Progress": context.progress,
                        "Blocker": context.blocker,
                        "Result": context.result,
                    }
                )
                + explorer.agent(
                    "<h3>Evidence</h3>"
                    + facts(
                        {
                            "Reported state": context.reported_state,
                            "Observed": context.observed_at,
                            "Freshness": session.presence_state,
                            "Session": session.session_id,
                            "Reporter": session.node_id,
                            "Provider": context.provider,
                            "Source": context.source,
                            "Scope": context.source_scope,
                            "Coverage": context.coverage,
                            "Work": context.work_id,
                            "Parent": context.parent_session_id,
                            "References": ", ".join(context.evidence_refs),
                        }
                    )
                )
                + actions('<a href="/view/machines?all=1&amp;attention=1">Open run history</a>')
                + (
                    investigation.render(
                        title,
                        route="/deck/activity",
                        observation=context.observed_at,
                        facts={
                            "State": context.reported_state,
                            "Blocker": context.blocker,
                            "Progress": context.progress,
                            "Result": context.result,
                            "Session": session.session_id,
                            "Reporter": session.node_id,
                            "Repository": session.repo_identity or session.repo_label,
                            "Provider": context.provider,
                            "Source": context.source,
                            "Scope": context.source_scope,
                            "Coverage": context.coverage,
                            "Work": context.work_id,
                            "Freshness": session.presence_state,
                            "Historical": historical,
                        },
                        limits="Self-reported task state; provider lifecycle unverified.",
                    )
                    if investigation.needed(context.reported_state)
                    else ""
                ),
            )
    for worker in view.cloud_workers:
        for index, lease in enumerate(worker.leases):
            title = lease.label or lease.repo or lease.provider_task_id or "Provider lease"
            add(
                "Provider lease:" + worker.provider + ":" + (lease.provider_task_id or f"unknown-{index}"),
                title,
                "Provider lease · " + lease.state,
                explorer.hero(title, lease.state)
                + facts({"What": "Provider lease", "Provider": worker.provider, "Repository": lease.repo})
                + explorer.agent(
                    "<h3>Evidence</h3>"
                    + facts(
                        {"Reported state": lease.state, "Task": lease.provider_task_id, "Artifact": lease.artifact_ref}
                    )
                )
                + actions('<a href="/deck/capacity/cloud">View cloud configuration</a>')
                + (
                    investigation.render(
                        title,
                        route="/deck/activity",
                        observation=now or "unknown; provider lease projection",
                        facts={
                            "State": lease.state,
                            "Provider": worker.provider,
                            "Task": lease.provider_task_id,
                            "Repository": lease.repo,
                            "Artifact": lease.artifact_ref,
                        },
                        limits="Lease projection only; current provider lifecycle unverified.",
                    )
                    if investigation.needed(lease.state)
                    else ""
                ),
            )
    return entries


def render_activity(view: deck.DeckView, now: datetime, *, nonce: str, worklore_enabled: bool) -> str:
    entries = activity_entries(view, now)
    groups = (
        "".join(
            f'<a class="row-action" href="/deck/observed/{_esc(group.key)}">{_esc(group.label)} (configured observation)</a> '
            for group in view.observed_work_groups
        )
        if worklore_enabled
        else ""
    )
    content = explorer.render(
        entries,
        label="activity",
        empty="No work activity reported",
        empty_hint="Entries appear here as stations report runs and queued work.",
    ) + (f'<div class="explorer-actions explorer-footer">{groups}</div>' if groups else "")
    return document(
        content, view, now, nonce=nonce, title="Activity", active="activity", worklore_enabled=worklore_enabled
    )


def render_capacity(
    view: deck.DeckView, now: datetime, *, nonce: str, worklore_enabled: bool, tab: str = "capacity", query: str = ""
) -> str:
    if tab == "github-reads":
        # This dedicated tab intentionally shows the whole bounded observation.
        content = activity.render_octopool_observation(view, expand_metrics=True)
    elif tab == "cloud-details":
        entries = [
            explorer.Entry(
                worker.provider,
                worker.provider,
                f"{worker.used} leases · configured limit {worker.limit}",
                explorer.hero(
                    worker.provider, "" if worker.circuit_state in {"closed", "ok", "healthy"} else worker.circuit_state
                )
                + facts(
                    {
                        "Limit": worker.limit,
                        "In use": worker.used,
                        "Circuit": "" if worker.circuit_state in {"closed", "ok", "healthy"} else worker.circuit_state,
                    }
                )
                + explorer.agent(
                    "Configured limits do not establish usable headroom. Lease inventory excludes self-reported tasks.",
                    tag="p",
                )
                + "<h3>Leases</h3>"
                + (
                    '<ul class="plain">'
                    + "".join(
                        f"<li>{_esc(lease.label or lease.provider_task_id)} {explorer.pill(lease.state)}"
                        + (
                            explorer.agent(
                                facts(
                                    {
                                        "Task": lease.provider_task_id,
                                        "Artifact": lease.artifact_ref,
                                        "Repository": lease.repo,
                                    }
                                )
                            )
                        )
                        + "</li>"
                        for lease in worker.leases
                    )
                    + "</ul>"
                    if worker.leases
                    else "<p>No leases.</p>"
                )
                + actions('<a href="/deck/activity">View work activity</a>')
                + (
                    investigation.render(
                        worker.provider,
                        route="/deck/capacity/cloud",
                        observation=now,
                        facts={
                            "Circuit": worker.circuit_state,
                            "Configured limit": worker.limit,
                            "Reported leases": worker.used,
                            "Problem leases": "; ".join(
                                f"{lease.provider_task_id}: {lease.state}; {lease.repo}"
                                for lease in worker.leases
                                if investigation.needed(lease.state)
                            ),
                        },
                        limits="Configured limits do not establish usable headroom; lease inventory excludes self-reported tasks. Observation is page render time; lease/circuit observation time not collected.",
                    )
                    if worker.circuit_state not in {"closed", "ok", "healthy"}
                    or any(investigation.needed(lease.state) for lease in worker.leases)
                    else ""
                ),
            )
            for worker in view.cloud_workers
        ]
        content = explorer.render(
            entries, label="cloud providers", empty=COPY["no_clouds"][0], empty_hint=COPY["no_clouds"][1]
        )
    else:
        entries = []
        for station in view.stations:
            machine = overview.machine_for(view, station)
            status = overview.station_status(station, machine)
            shown = status_label(station, machine, status)
            kind = "warn" if status == "limited" else ""
            capacity = station.station.capacity
            bars = (
                '<div class="resource-bars">'
                f"<span>Capacity: {capacity} configured slots</span>"
                f'<progress class="capacity-bar" value="{capacity}" max="{max(1, capacity)}" aria-label="Configured slots"></progress>'
                f"<span>Use: {station.busy} / {capacity} slots</span>"
                f'<progress value="{station.busy}" max="{max(1, capacity)}" aria-label="Reported used slots"></progress></div>'
            )
            entries.append(
                explorer.Entry(
                    station.station.node_id,
                    station.label,
                    shown
                    + (" · not enrolled" if not station.enrolled else "")
                    + " · telemetry "
                    + (
                        overview.format_age(overview.seconds_since(machine.last_observed_at, now))
                        if machine
                        else "unknown"
                    ),
                    explorer.hero(station.label, shown, kind)
                    + facts(
                        {
                            "Capacity": f"{capacity} slots",
                            "In use": f"{station.busy} of {capacity} slots",
                            **({"Agent sessions": station.sessions} if station.sessions else {}),
                            "Load": overview._load_cell(machine).split("<", 1)[0],
                            "Last seen": overview.format_age(overview.seconds_since(machine.last_observed_at, now))
                            if machine
                            else "unknown",
                            "OS": machine.os if machine else "unknown",
                            "Enrollment": "" if station.enrolled else "Not enrolled",
                        }
                    )
                    + explorer.agent(
                        "<h3>Evidence</h3>"
                        + facts(
                            {
                                "Status code": status,
                                "Reason code": machine.unused_reason if machine else "unknown",
                                "Last run report": station.last_heard or "unknown",
                                "Node": station.station.node_id,
                            }
                        )
                        + "<p data-search-exclude>Usable for selected work: unknown. Configured slots do not confirm work eligibility.</p>"
                    )
                    + actions(
                        f'<a href="{_esc("/view/machines?" + urlencode({"node": station.station.node_id}))}">Open machine history</a>'
                    )
                    + (
                        investigation.render(
                            station.label,
                            route="/deck/capacity",
                            observation=machine.last_observed_at if machine else "unknown (no telemetry)",
                            facts={
                                "Node": station.station.node_id,
                                "Status": status,
                                "Enrolled": station.enrolled,
                                "Configured slots": capacity,
                                "Reported used slots": station.busy,
                                "Agent sessions": station.sessions,
                                "Telemetry age": overview.format_age(
                                    overview.seconds_since(machine.last_observed_at, now)
                                )
                                if machine
                                else "unknown",
                                "Reason": machine.unused_reason if machine else "unknown",
                                "Last run report": station.last_heard,
                            },
                            limits="Capacity telemetry only; stale or missing reports do not prove the machine is offline. Configured slots do not confirm eligibility.",
                        )
                        if not station.enrolled or status in {"stale", "offline", "unknown"}
                        else ""
                    ),
                    # Only problematic telemetry receives an investigation action.
                    summary_html=bars,
                )
            )
        content = (
            '<div id="machines-table">'
            + explorer.render(
                entries, label="machines", empty=COPY["no_stations"][0], empty_hint=COPY["no_stations"][1], query=query
            )
            + "</div>"
        )
    return document(content, view, now, nonce=nonce, title="Capacity", active=tab, worklore_enabled=worklore_enabled)


def render_overview_machines(view: deck.DeckView, now: datetime) -> str:
    """Compact machine scan using only the overview's existing projection."""
    rows = []
    for station in view.stations:
        machine = overview.machine_for(view, station)
        status = overview.station_status(station, machine)
        capacity = station.station.capacity
        age = overview.format_age(overview.seconds_since(machine.last_observed_at, now)) if machine else "unknown"
        if not station.enrolled:
            state = "not enrolled · offline"
        elif status == "unknown":
            state = "telemetry unknown"
        else:
            state = status_label(station, machine, status)
        load_cell = overview._load_cell(machine)
        load = load_cell.split("<", 1)[0]
        load_bar = load_cell[len(load) :] if load != overview.UNKNOWN else ""
        rows.append(
            '<div class="overview-machine-row">'
            '<div class="overview-machine-name"><a href="/deck/capacity'
            + explorer.selection_href(station.station.node_id)
            + '">'
            + _esc(station.label)
            + "</a><span>"
            + _esc(machine.os if machine else "unknown")
            + "</span><small>"
            + explorer.pill(state, "warn" if status == "limited" else "")
            + "</small></div>"
            '<div class="overview-machine-capacity"><div><span>Configured capacity</span><b>'
            + _esc(f"{capacity} slots")
            + '</b></div><progress class="overview-capacity-bar" value="'
            + str(capacity)
            + '" max="'
            + str(max(1, capacity))
            + '" aria-label="Configured capacity"></progress><div><span>Reported use</span><b>'
            + _esc(f"{station.busy}/{capacity} used")
            + '</b></div><progress class="overview-use-bar" value="'
            + str(station.busy)
            + '" max="'
            + str(max(1, capacity))
            + '" aria-label="Reported used slots"></progress>'
            + (f"<div><span>Agent sessions</span><b>{station.sessions}</b></div>" if station.sessions else "")
            + "</div>"
            '<div class="overview-machine-load"><span>Reported load</span><b>'
            + _esc(load)
            + "</b>"
            + load_bar
            + '</div><div class="overview-machine-age"><span>Telemetry</span><b>'
            + _esc(age)
            + "</b></div></div>"
        )
    body = "".join(rows) or '<p class="overview-machines-empty">No machines configured.</p>'
    return (
        '<section class="overview-machines" aria-labelledby="overview-machines-title"><header>'
        f'<h2 id="overview-machines-title">Machines ({len(view.stations)})</h2>'
        '<a class="card-link" href="/deck/capacity">View capacity</a></header>'
        f'<div class="overview-machine-rows">{body}</div></section>'
    )


def parse_project_query(query: str) -> str | None:
    if not query:
        return None
    params = parse_qs(query, strict_parsing=True, keep_blank_values=True, max_num_fields=1)
    if set(params) != {"project"} or len(params["project"]) != 1 or not campaigns.valid_key(params["project"][0]):
        raise ValueError("invalid project query")
    return params["project"][0]


def parse_projects_query(query: str) -> tuple[str | None, board_page.Options]:
    """The goals selection (``project``) plus the board's own ``view`` and ``epic`` links."""
    if not query:
        return None, board_page.Options()
    params = parse_qs(query, strict_parsing=True, keep_blank_values=True, max_num_fields=4)
    if not set(params) <= {"project", "view", "epic", "repo"} or any(len(v) != 1 for v in params.values()):
        raise ValueError("invalid project query")
    project = params.get("project", [None])[0]
    if project is not None and not campaigns.valid_key(project):
        raise ValueError("invalid project query")
    view = params.get("view", ["board"])[0]
    epic = params.get("epic", [None])[0]
    repo = params.get("repo", [None])[0]
    if (
        view not in board_page.VIEWS
        or (epic is None) != (repo is None)
        or (epic is not None and not (epic.isdigit() and epic.isascii() and len(epic) <= 9 and int(epic) > 0))
        or (repo is not None and not project_board_store._REPO.fullmatch(repo))
    ):
        raise ValueError("invalid project query")
    return project, board_page.Options(view=view, epic=int(epic) if epic is not None else None, epic_repo=repo)


def load_projects(conn: sqlite3.Connection, *, enabled: bool, selected: str | None = None) -> dict[str, Any]:
    if not enabled:
        return {"state": "disabled", "campaigns": [], "selected": None}
    try:
        # list_campaigns eagerly resolves every member and its links. Read the
        # same explicit campaign->work_id relation for bounded list labels, then
        # use the canonical detail projection for the selected campaign only.
        summaries = [
            {
                "campaign_key": str(row[0]),
                "title": str(row[1]),
                "status": str(row[2]),
                "search": str(row[3] or "") + " " + " ".join(json.loads(row[4] or "[]")),
            }
            for row in conn.execute(
                "SELECT c.campaign_key, w.title, w.status, w.description, w.acceptance_json "
                "FROM work_campaigns c JOIN work_items w ON w.work_id = c.work_id "
                "ORDER BY c.created_at DESC, c.campaign_key DESC LIMIT ?",
                (PROJECT_LIMIT,),
            )
        ]
        key = selected or (summaries[0]["campaign_key"] if summaries else None)
        if (
            key
            and conn.execute("SELECT COUNT(*) FROM work_campaign_members WHERE campaign_key = ?", (key,)).fetchone()[0]
            > MAX_MEMBERS
        ):
            return {"state": "unavailable", "campaigns": [], "selected": None}
        project = campaigns.show_campaign(conn, key) if key else None
    except WorkloreNotFound as exc:
        raise ValueError("unknown project") from exc
    except (sqlite3.Error, json.JSONDecodeError, TypeError):
        return {"state": "unavailable", "campaigns": [], "selected": None}
    if project and not any(item["campaign_key"] == key for item in summaries):
        chosen = project["campaign"]
        summaries = summaries[: PROJECT_LIMIT - 1] + [
            {
                "campaign_key": str(chosen["campaign_key"]),
                "title": chosen["title"],
                "status": chosen["status"],
                "search": chosen["description"] + " " + " ".join(chosen["acceptance"]),
            }
        ]
    if project:
        project["members"] = project["members"][:MAX_MEMBERS]
    return {"state": "ready" if summaries or project else "empty", "campaigns": summaries, "selected": project}


_PROJECT_EMPTY_CSS = (
    ".project-empty { margin:0 0 16px; padding:12px 4px; text-align:left; }"
    " .project-empty a { color:var(--ink); font-weight:500; }"
    " .project-sync { margin-top:-12px; color:var(--muted); font-size:13px; } .project-sync code { user-select:all; }"
)


def render_projects(
    page: Mapping[str, Any],
    view: deck.DeckView,
    now: datetime,
    *,
    nonce: str,
    worklore_enabled: bool,
    board: Mapping[str, Any] | None = None,
    board_options: board_page.Options | None = None,
    ci_view: Mapping[str, Any] | None = None,
) -> str:
    state = page["state"]
    messages = {
        "disabled": ("Projects not connected", "Project goals need Worklore, which is disabled on this hub."),
        "unavailable": (
            "Project goals unavailable",
            "Campaign storage is not available, so recorded goals cannot be read.",
        ),
    }
    if state == "empty":
        # Projects are goals only. Open pull requests live on their own page.
        content = (
            '<p class="empty project-empty"><span class="empty-title">No projects yet</span>'
            '<span class="empty-hint">Boards from GitHub Projects appear here once connected. '
            '<a href="/deck/pulls">View open pull requests</a></span></p>'
            f'<p class="project-empty project-sync">Sync a board from a node with the read:project scope: <code>{_esc(project_board_store.SYNC_COMMAND)}</code></p>'
        )
    elif state != "ready":
        content = empty_state(*messages[state])
        if state in {"disabled", "unavailable"}:
            content += investigation.render(
                "Project goals",
                route="/deck/projects",
                observation=now,
                facts={"State": state, "Detail": messages[state]},
                limits="Page render time; storage observation timestamp and exact exception details not collected.",
            )
    else:
        selected = page["selected"]
        campaign = selected["campaign"]
        links = "".join(
            f'<a data-search="{_esc(item.get("search", ""))}" href="{_esc("/deck/projects?" + urlencode({"project": item["campaign_key"]}))}"'
            + (' aria-current="page"' if item["campaign_key"] == campaign["campaign_key"] else "")
            + f"><strong>{_esc(item['title'])}</strong><span>{_esc(item['status'])}</span></a>"
            for item in page["campaigns"]
        )
        members = []
        for member in selected["members"]:
            sources = []
            for kind in ("github", "brigade", "fleet_run"):
                link = member.get(kind)
                if isinstance(link, dict) and (url := _reference_url(link.get("url"))):
                    sources.append(f'<a href="{_esc(url)}" rel="noopener noreferrer">{_esc(kind)}</a>')
            members.append(
                f"<li><strong>{_esc(member['repo'])}</strong> {explorer.pill(str(member['bucket']))} "
                + " ".join(sources)
                + "</li>"
            )
        detail = (
            explorer.hero(str(campaign["title"]), str(campaign["status"]))
            + facts({"Goal": campaign["description"]})
            + "<h3>Acceptance</h3>"
            + '<ul class="plain">'
            + "".join(f"<li>{_esc(item)}</li>" for item in campaign["acceptance"])
            + '</ul><h3>Repositories</h3><ul class="plain">'
            + "".join(members)
            + "</ul>"
        )
        if investigation.needed(str(campaign["status"])) or any(
            investigation.needed(str(member["bucket"])) for member in selected["members"]
        ):
            detail += investigation.render(
                str(campaign["title"]),
                route="/deck/projects?" + urlencode({"project": campaign["campaign_key"]}),
                observation=now,
                facts={
                    "Project": campaign["campaign_key"],
                    "Stored status": campaign["status"],
                    "Goal": campaign["description"],
                    "Members": "; ".join(
                        str(member["repo"]) + ": " + str(member["bucket"]) for member in selected["members"]
                    ),
                },
                limits="Page render time. Recorded project status and member buckets do not establish live progress; individual task logs not collected.",
            )
        content = (
            explorer.agent(
                "Up to 20 recorded goals; selected members only. Recorded buckets do not establish live progress.",
                tag="p",
            )
            + '<section class="admin-explorer project-explorer"><aside class="explorer-list">'
            f'<div class="explorer-head"><span class="explorer-title">Projects</span><span class="explorer-count">{len(page["campaigns"])}</span></div>'
            + explorer.search_box("projects", " project-search")
            + f'<nav aria-label="Projects">{links}</nav>{empty_state("No matching projects", attrs="data-project-empty hidden")}</aside>'
            '<div class="explorer-details">'
            + empty_state("Select a matching project to view its details.", attrs="data-project-select hidden")
            + f'<article><div class="detail-bar"><span>{_esc(str(campaign["title"])[:120])}</span></div><div class="detail-body">{detail}</div></article></div></section>'
        )
    board_css = board_script = ""
    if board is not None:
        board_html, board_css, board_script = board_page.render(
            board, board_options or board_page.Options(), now, ci_view=ci_view
        )
        content = board_html + (content if state == "ready" else "")
    # Project changes are normal server links; filtering never fabricates unloaded member details.
    script = """(function(){function start(){var root=document.querySelector('.project-explorer');if(!root)return;
var input=root.querySelector('input'),links=Array.from(root.querySelectorAll('nav a')),detail=root.querySelector('.explorer-details article'),empty=root.querySelector('[data-project-empty]'),selection=root.querySelector('[data-project-select]');
root.classList.add('is-enhanced');input.disabled=false;root.querySelector('.project-search').hidden=false;
input.addEventListener('input',function(){var q=input.value.toLowerCase(),count=0,current=false;
links.forEach(function(link){link.hidden=(link.textContent+' '+link.dataset.search).toLowerCase().indexOf(q)===-1;if(!link.hidden){count++;if(link.hasAttribute('aria-current'))current=true;}});
empty.hidden=count>0;detail.hidden=!current;selection.hidden=current;selection.textContent=count?'Select a matching project to view its details.':'No matching projects.';});}
(typeof window!=='undefined'&&window.fleetOnInit||function(f){if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',f);else f();})(start);})();"""
    return deck._document(
        f'<main class="deck-shell admin-page">{content}</main>',
        nonce=nonce,
        now=now,
        title="Projects",
        active="projects",
        view=view,
        refresh=False,
        worklore_enabled=worklore_enabled,
        extra_css=explorer.CSS + _PROJECT_EMPTY_CSS + board_css,
        script=board_script + script,
    )


def render_pulls(
    ci_view: Mapping[str, Any] | None, view: deck.DeckView, now: datetime, *, nonce: str, worklore_enabled: bool
) -> str:
    """Open pull requests from the stored CI projection, on their own page."""
    if ci_view is None:
        content = empty_state("Pull requests unavailable", "The CI projection could not be read.")
        pr_css = script = ""
    else:
        content, pr_css = pull_requests.render(ci_view, now)
        script = pull_requests.SCRIPT
    return deck._document(
        f'<main class="deck-shell admin-page">{content}</main>',
        nonce=nonce,
        now=now,
        title="Pull requests",
        active="pulls",
        view=view,
        refresh=False,
        worklore_enabled=worklore_enabled,
        extra_css=explorer.CSS + pr_css,
        script=script,
    )


def render_reporting(
    page: Mapping[str, Any], view: deck.DeckView, now: datetime, *, nonce: str, worklore_enabled: bool
) -> str:
    latest = page.get("latest")
    if page.get("unavailable"):
        content = empty_state(
            "Reporting observations unavailable", "The adoption report could not be read from the hub."
        )
    elif not latest and not page.get("weeks"):
        content = empty_state(
            "No adoption observations available", "Collection may be unconfigured or not yet reported."
        )
    else:
        content = (
            "<h2>Reported observations</h2>"
            + facts({"Weekly observations": len(page.get("weeks") or [])})
            + explorer.agent(
                facts(
                    {
                        "Latest snapshot": latest.get("taken_at") if latest else "unavailable",
                        "Sources": ", ".join(latest.get("source_set") or []) if latest else "unknown",
                    }
                )
            )
        )
    if page.get("unavailable") or (not latest and not page.get("weeks")):
        content += investigation.render(
            "Reporting observations",
            route="/deck/reporting",
            observation=now,
            facts={
                "State": "unavailable" if page.get("unavailable") else "no observations",
                "Weekly observations in projection": len(page.get("weeks") or []),
            },
            limits="Page render time; collection may be unconfigured. Missing/privacy-suppressed data do not establish zero activity. Exact exception details not collected.",
        )
    content += explorer.agent(
        "<p>Missing and privacy-suppressed data do not establish zero activity.</p>"
        '<a href="/deck/stats">Observation history and methodology</a>'
    )
    return document(
        content, view, now, nonce=nonce, title="Reporting", active="stats", worklore_enabled=worklore_enabled
    )


def overview_attention(view: deck.DeckView, now: datetime) -> str:
    from . import fleet_deck_alerts

    alerts = overview.page_alerts(view, now)
    ids = explorer.identifiers([fleet_deck_alerts.entry_key(alert) for alert in alerts])
    rows = "".join(
        f'<li><a href="/deck/alerts#{identifier}">{_esc(alert.title[:120])}</a>{explorer.pill(alert.kind)}</li>'
        for alert, identifier in zip(alerts[:3], ids[:3], strict=True)
    )
    return rows or "<li>No attention signals reported.</li>"


def outcome_label(state: str) -> str:
    suffix = state.rsplit(".", 1)[-1]
    return {
        "completed": "completed",
        "failed": "failed",
        "interrupted": "interrupted",
        "orphaned": "interrupted",
        "canceled": "interrupted",
        "cancelled": "interrupted",
        "timed_out": "interrupted",
        "timeout": "interrupted",
        "expired": "expired",
    }.get(suffix, "unknown")
