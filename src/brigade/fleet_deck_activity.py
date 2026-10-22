"""Activity and queue presentation over the Deck's reported metadata."""

from __future__ import annotations

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
    dirty = str(len(session.dirty_paths)) + ("+" if session.dirty_truncated else "")
    return (
        f"<li><strong>{_esc(session.repo_label or session.repo_identity)}</strong>"
        f"<p>Repository: {_esc(session.repo_identity)}<br>Checkout: {_esc(session.checkout_path)}"
        f"<br>Branch: {_esc(session.branch or deck.UNKNOWN)} · Dirty paths: {_esc(dirty)}</p></li>"
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
        rows.append(
            '<li class="row"><div class="row-body">'
            f'<p class="row-title">{_esc(repos or "Repository unknown")}</p>'
            f'<p class="row-meta">{deck.harness_badge_html(harness)} {_esc(name)} · '
            f"{_esc(labels.get(node, 'Unlisted machine'))} · current presence</p>"
            "<details><summary>Session and checkout details</summary>"
            f"<p>Session: {_esc(session_id)}<br>Node: {_esc(node)}</p><ul>"
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
