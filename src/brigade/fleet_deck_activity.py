"""Activity, queue and read-pool presentation over the Deck's reported metadata."""

from __future__ import annotations

from datetime import datetime
from typing import Mapping, Sequence

from . import fleet_command_deck as deck

_esc = deck._esc
REASONS = {
    "acceptance-required": "need acceptance criteria",
    "not-ready": "not ready",
    "not-eligible": "not eligible for automatic work",
    "manual-mode": "require manual execution",
    "blocker": "blocked",
    "attempt-limit": "reached the attempt limit",
    "review-after": "waiting for review date",
    "source-policy": "excluded by source policy",
}
HARNESS_NAMES = {
    "claude": "Claude",
    "codex": "Codex",
    "openclaw": "OpenClaw",
    "cursor": "Cursor",
    "gemini": "Gemini",
    "dot": "Dot",
    "t3-fleet": "T3",
    "t3": "T3 Code",
}


def parallel_entries(
    targets: set[str], runs: Sequence[deck.LiveRun], labels: Mapping[str, str]
) -> tuple[deck.RailEntry, ...]:
    """Same-label activity across machines establishes neither repo identity nor conflict."""
    entries = []
    for target in sorted(targets):
        nodes = sorted({run.node_id for run in runs if run.repo == target})
        machines = ", ".join(labels.get(node, node[:12]) for node in nodes)
        entries.append(deck.RailEntry("parallel", "", target, "", f"Same repo label on {machines}."))
    return tuple(entries)


def parallel_note(detail: str, *, overlap: bool = False) -> str:
    warning = '<p class="flag">Shared modified paths reported</p>' if overlap else ""
    evidence = (
        "Sessions with matching repository identity report the same modified path. Review checkout details."
        if overlap
        else "File overlap unknown."
    )
    return (
        warning + f"<details><summary>{'parallel activity' if detail or not overlap else 'Overlap details'}</summary>"
        f'<p class="row-meta">{_esc(" ".join(filter(None, (detail, evidence))))}</p></details>'
    )


def queue_counts(queue: deck.QueueState) -> str:
    """Equal complete-scan counts describe the same ready items, not two totals."""
    ready, queued = queue.eligible_count, queue.queued_count
    label = f"{ready} ready to run" if ready != deck.UNKNOWN else "Ready to run: unknown"
    if ready != queued or ready == deck.UNKNOWN:
        label += f" · {queued} queued" if queued != deck.UNKNOWN else " · queued: unknown"
    return f'<p class="station-meta">{_esc(label)}</p>'


def queue_exclusions(queue: deck.QueueState, *, worklore_enabled: bool) -> str:
    """Fold routine exclusions; real blockers remain in the Deck's alert list."""
    if not queue.exclusions:
        return ""
    total = sum(int(deck._cp_text(item["count"])) for item in queue.exclusions)
    summary = (
        f"{total} not ready"
        if all(item["reason"] == "not-ready" for item in queue.exclusions)
        else f"{total} excluded from ready queue"
    )
    rows = "".join(
        f"<li>{_esc(str(item['count']))} {_esc(REASONS.get(str(item['reason']), str(item['reason'])))}</li>"
        for item in queue.exclusions
    )
    link = '<p><a href="/deck/work">Work</a></p>' if worklore_enabled else ""
    return f'<details><summary>{summary}</summary><ul class="attention-list">{rows}</ul>{link}</details>'


def _membership(session: deck.InteractiveSession) -> str:
    if session.cloud_context is not None:
        return f"<li><p>Repository: {_esc(session.repo_identity or deck.UNKNOWN)}</p></li>"
    dirty = str(len(session.dirty_paths)) + ("+" if session.dirty_truncated else "")
    return (
        f"<li><strong>{_esc(session.repo_label or session.repo_identity)}</strong>"
        f"<p>Repository: {_esc(session.repo_identity)}<br>Checkout: {_esc(session.checkout_path)}"
        f"<br>Branch: {_esc(session.branch or deck.UNKNOWN)} · Dirty paths: {_esc(dirty)}</p></li>"
    )


def cloud_session_context(raw: object) -> deck.CloudSessionContext | None:
    from .fleet_dot import DotReportError, validate_cloud_context

    if raw is None:
        return None
    try:
        context = validate_cloud_context(raw)
        if "evidence_refs" in context:
            context["evidence_refs"] = tuple(context["evidence_refs"])
        return deck.CloudSessionContext(**context)
    except DotReportError:
        return None


def interactive_session(raw: Mapping[str, object], *, now: datetime) -> deck.InteractiveSession:
    dirty = raw.get("dirty_paths")
    paths = tuple(item for item in dirty if isinstance(item, str)) if isinstance(dirty, (list, tuple)) else ()
    branch = raw.get("branch")
    context = cloud_session_context(raw.get("cloud_context"))
    return deck.InteractiveSession(
        node_id=deck._safe_display_text(raw.get("node_id")),
        harness=deck._safe_display_text(raw.get("harness")),
        session_id=deck._safe_display_text(raw.get("session_id")),
        repo_identity=deck._safe_display_text(raw.get("repo_identity"), limit=deck.SESSION_REPO_IDENTITY_LIMIT),
        identity_scope=deck._safe_display_text(raw.get("identity_scope")) or "fleet",
        repo_label=deck._safe_display_text(raw.get("repo_label")),
        checkout_path=deck._safe_display_text(raw.get("checkout_path"), limit=deck.SESSION_CHECKOUT_PATH_LIMIT),
        branch=deck._safe_display_text(branch) if isinstance(branch, str) else None,
        dirty_paths=paths,
        dirty_truncated=bool(raw.get("dirty_truncated")),
        cloud_context=context,
        presence_state="stale" if raw.get("presence_state") == "stale" else "active",
        observation_age_seconds=deck._age_seconds(context.observed_at, now) if context else None,
    )


def render_cloud_presence(view: deck.DeckView, *, preview_limit: int | None = None) -> str:
    """Bounded human labels from reported presence, independent of lease inventory."""
    about = (
        "<details><summary>About reports</summary><p>Managed jobs occupy station slots. "
        "Coverage is limited to explicitly reported sessions. Presence reports may be idle or overlap managed jobs. "
        "Current presence means the Hub has an unexpired report.</p></details>"
    )
    agents = {
        (session.node_id, session.harness, session.session_id): session.cloud_context
        for session in view.interactive_sessions
        if session.cloud_context is not None
    }
    if not agents:
        return (
            '<p class="station-meta">Dot reporting coverage unknown. '
            "No current Dot reports; publishing may not be configured.</p>" + about
        )
    # Presence-only reports have no task card; never repeat task agents in a label list.
    presence_only = [context for context in agents.values() if context.version != 2]
    labels = ", ".join(context.agent_label for context in presence_only[:8])
    more = f" · {len(presence_only) - 8} more in session details" if len(presence_only) > 8 else ""
    presence = (
        f'<p class="row-meta">{deck.harness_badge_html("dot")} Dot · {_esc(labels)}{more}</p>' if presence_only else ""
    )
    tasks = [
        session
        for session in view.interactive_sessions
        if session.cloud_context is not None and session.cloud_context.version == 2
    ]
    shown = tasks if preview_limit is None else tasks[:preview_limit]
    task_rows = "".join(_task_report_row(session) for session in shown)
    remaining = tasks[len(shown) :]
    more_tasks = (
        f'<details><summary>More reported tasks ({len(remaining)})</summary><ul class="activity-cards">'
        + "".join(_task_report_row(session) for session in remaining)
        + "</ul></details>"
        if remaining
        else ""
    )
    notice = (
        "Self-reported task state; provider execution unverified. "
        if task_rows
        else "Presence only; running work unobserved. "
    )
    return (
        f"<h3>Reported cloud agents ({len(agents)})</h3>"
        + presence
        + f'<p class="station-meta">{notice}</p>'
        + about
        + (f'<ul class="activity-cards">{task_rows}</ul>' if task_rows else "")
        + more_tasks
    )


def status_badge(state: str, *, reported: bool = False, stale: bool = False) -> str:
    label = ("Reported " if reported else "") + state + (" · stale" if stale else "")
    tone = "stale" if stale else state
    return f'<span class="activity-state" data-state="{_esc(tone)}">{_esc(label)}</span>'


def _task_report_row(session: deck.InteractiveSession) -> str:
    from .fleet_deck_overview import format_age

    context = session.cloud_context
    if context is None:
        return ""
    agent = context.agent_label.replace("_", " ").replace("-", " ")
    # Use the most relevant supplied fact, retaining all other facts in Details.
    priorities = (
        (("Blocker", context.blocker), ("Progress", context.progress), ("Result", context.result))
        if context.reported_state == "blocked"
        else (("Result", context.result), ("Progress", context.progress), ("Blocker", context.blocker))
        if context.reported_state == "completed"
        else (("Progress", context.progress), ("Blocker", context.blocker), ("Result", context.result))
    )
    fact = next(((label, value) for label, value in priorities if value), None)
    prominent = f"{fact[0]}: {_esc(fact[1])}" if fact else "No progress reported."
    facts = "".join(
        f"<p>{label}: {_esc(value)}</p>"
        for label, value in (("Progress", context.progress), ("Blocker", context.blocker), ("Result", context.result))
        if value and (label, value) != fact
    )
    state = status_badge(context.reported_state, reported=True, stale=session.presence_state == "stale")
    return (
        '<li class="activity-card" data-activity-kind="reported">'
        f'<div class="activity-head">{state}<span class="activity-kind">Dot report</span></div>'
        f'<p class="row-title">{_esc(context.task)}</p>'
        f'<p class="row-meta">{deck.harness_badge_html(session.harness)} Dot · {_esc(agent)}</p>'
        f'<p class="activity-fact">{prominent}</p>'
        '<div class="activity-footer"><details><summary>Details</summary>'
        + facts
        + f"<p>Session: {_esc(session.session_id)}<br>Reporter: {_esc(session.node_id)}"
        + f"<br>Repository: {_esc(session.repo_identity or deck.UNKNOWN)}"
        + f"<br>Freshness: {'stale' if session.presence_state == 'stale' else 'unexpired report'}"
        + "<br>Task state: reported-unverified</p>"
        + _cloud_details(context)
        + f'</details><span class="activity-age">Observed {format_age(session.observation_age_seconds)} ago</span></div></li>'
    )


def render_cloud_report_history(view: deck.DeckView) -> str:
    if not view.recent_cloud_reports:
        return ""
    rows = "".join(_task_report_row(session) for session in view.recent_cloud_reports)
    cap = (
        '<p class="station-meta">Recent report list may be capped.</p>'
        if len(view.recent_cloud_reports) >= deck.CLOUD_REPORT_HISTORY_LIMIT
        else ""
    )
    return (
        '<details class="activity-history">'
        f"<summary>Recent cloud reports ({len(view.recent_cloud_reports)})</summary>"
        '<p class="station-meta">Stale presence or reported outcomes. Self-reported task state; provider execution unverified. '
        "Coverage is limited to explicitly reported sessions. Outcomes do not complete leases.</p>"
        f'<ul class="activity-cards">{rows}</ul>{cap}</details>'
    )


def _cloud_details(context: deck.CloudSessionContext) -> str:
    return (
        f"<p>Provider: {_esc(context.provider)} · lifecycle unobserved"
        f"<br>Parent: {_esc(context.parent_session_id or deck.UNKNOWN)}"
        f"<br>Work: {_esc(context.work_id or deck.UNKNOWN)}"
        f"<br>Source: {_esc(context.source)}<br>Scope: {_esc(context.source_scope)}"
        f"<br>Coverage: {_esc(context.coverage)}<br>Observed: {_esc(context.observed_at)}"
        f"<br>Context version: {context.version} · Sequence: {context.sequence}"
        + (
            f"<br>Reported-unverified evidence: {_esc(', '.join(context.evidence_refs))}"
            if context.evidence_refs
            else ""
        )
        + "</p>"
    )


def render_octopool_observation(view: deck.DeckView) -> str:
    observation = deck.octopool_observation_from_snapshot(view.octopool_observation)
    header = '<section class="panel" id="octopool-observation"><h2>GitHub reads / Octopool</h2>'
    if observation is None:
        return header + '<p class="station-meta">Octopool reporting not connected</p></section>'
    captured = datetime.fromisoformat(observation.captured_at)
    captured_label = f"{captured:%b} {captured.day} {captured:%H:%M} UTC"
    seconds = observation.window_seconds
    window = f"{seconds // 3600}h" if seconds % 3600 == 0 else f"{seconds}s"
    labels = {
        "requests": ("request", "requests"),
        "cache_served_responses": ("cache-served response", "cache-served responses"),
        "uncached_outcomes": ("uncached outcome", "uncached outcomes"),
        "fallbacks": ("fallback", "fallbacks"),
        "service_errors": ("service error", "service errors"),
    }
    metrics = " · ".join(
        f"{value} {words[value != 1]}"
        for name, words in labels.items()
        for value in (getattr(observation, name),)
        if value is not None
    )
    rate = observation.eligible_cache_hit_rate
    details = f"<p>Eligible cache hit rate: {rate:.2%}</p>" if rate is not None else ""
    return (
        header + f'<p class="station-meta">Captured {_esc(captured_label)} · {window} · '
        "Pool-wide, includes clients outside this fleet.</p>"
        f'<p class="row-meta">{metrics or "No valid counters provided."}</p>'
        "<details><summary>Metric details</summary><p>Source: Octopool stats capture</p>"
        f"<p>Capture timestamp: {_esc(observation.captured_at)}</p>"
        "<p>Cache-served responses include cache hits and stale responses, including revalidations. "
        "Uncached outcomes count responses served outside the cache. Neither counter measures upstream request savings. "
        "Fallbacks count expected local fallbacks and do not necessarily indicate failure.</p>"
        + details
        + "</details></section>"
    )


def render_provider_leases(view: deck.DeckView) -> str:
    rows = "".join(
        f"<li><strong>{_esc(worker.provider)} · {_esc(lease.label or 'Reported lease')}</strong>"
        f"<br>{_esc(lease.repo)} · {_esc(lease.state)}</li>"
        for worker in view.cloud_workers
        for lease in worker.leases
    )
    return (
        f'<ul class="cloud-lease-list">{rows}</ul>'
        if rows
        else '<p class="empty">No active provider leases reported.</p>'
    )


def render_cloud_presence_panel(view: deck.DeckView) -> str:
    return (
        '<section class="panel">'
        + render_cloud_presence(view)
        + "<details><summary>Reported session presence</summary>"
        + render_sessions(view)
        + "</details>"
        + render_cloud_report_history(view)
        + "</section>"
    )


def render_sessions(view: deck.DeckView) -> str:
    """One presence entry per node/harness/session, with every repo membership."""
    groups: dict[tuple[str, str, str], list[deck.InteractiveSession]] = {}
    for session in view.interactive_sessions:
        groups.setdefault((session.node_id, session.harness, session.session_id), []).append(session)
    labels = {station.station.node_id: station.label for station in view.stations}
    rows = []
    for (node, harness, session_id), memberships in groups.items():
        repos = ", ".join(dict.fromkeys(item.repo_label or item.repo_identity for item in memberships))
        name = HARNESS_NAMES.get(harness, harness or deck.UNKNOWN)
        cloud = memberships[0].cloud_context
        title = cloud.agent_label if cloud else repos or "Repository unknown"
        location = "reported cloud presence" if cloud else labels.get(node, "Unlisted machine")
        summary = "Cloud report details" if cloud else "Session and checkout details"
        rows.append(
            '<li class="row"><div class="row-body">'
            f'<p class="row-title">{_esc(title)}</p>'
            f'<p class="row-meta">{deck.harness_badge_html(harness)} {_esc(name)} · '
            f"{_esc(location)} · current presence</p>"
            f"<details><summary>{summary}</summary>"
            f"<p>Session: {_esc(session_id)}<br>Node: {_esc(node)}</p>"
            + (_cloud_details(cloud) if cloud else "")
            + "<ul>"
            + "".join(_membership(item) for item in memberships)
            + "</ul></details></div></li>"
        )
    body = f'<ul class="rows">{"".join(rows[:8])}</ul>' if rows else '<p class="empty">No agent sessions reported.</p>'
    if len(rows) > 8:
        body += (
            f"<details><summary>{len(rows) - 8} more reported agent sessions</summary>"
            f'<ul class="rows">{"".join(rows[8:])}</ul></details>'
        )
    if len(view.interactive_sessions) >= 500:
        body += '<p class="station-meta">500 repository memberships reported. The list may be capped. Session total unknown.</p>'
    return f'<h3 id="interactive-sessions">Reported agent sessions ({len(groups)})</h3>{body}'
