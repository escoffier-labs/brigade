"""Explain reported alerts without inferring causes or executing repairs."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from . import fleet_action_page, fleet_command_deck as deck, fleet_deck_activity as activity, ui_theme
from .fleet_deck_overview import Alert, _esc, page_alerts


@dataclass(frozen=True)
class _Guidance:
    summary: str
    impact: str
    next_step: str
    href: str = "/deck/capacity"
    link: str = "View capacity"


def _guidance(alert: Alert, *, worklore_enabled: bool) -> _Guidance:
    """Interpret only the projected signal. Reported detail remains evidence."""
    if alert.kind in {"failed", "unknown", "stale", "awaiting approval"}:
        if alert.kind == "failed":
            code_only = (
                not alert.detail
                or alert.detail == deck.UNKNOWN
                or re.fullmatch(r"[a-z][a-z0-9_-]*(?:\.[a-z][a-z0-9_-]*)+", alert.detail)
            )
            summary = "This run reported a failure."
            if code_only:
                summary += " No failure reason was reported."
            else:
                summary += " Reported detail is available under Evidence."
            impact = "The requested work may be incomplete. This report does not establish its current state."
            step = "Inspect the run or repository logs before retrying."
        elif alert.kind == "unknown":
            summary = "This run's outcome could not be determined."
            impact = "Restarting without checking could create duplicate work."
            step = "Check the originating session or run before restarting."
        elif alert.kind == "stale":
            summary = "Run updates are too old to establish current progress."
            impact = "Its current progress is unknown. Work may still be running."
            step = "Check the originating session or run before restarting to avoid duplicate work."
        else:
            summary = "This run reported a wait for approval or input."
            impact = "Work may be waiting for an operator response. The current session state is unconfirmed."
            step = "Check the originating session and review the pending request before responding."
        return _Guidance(
            summary,
            impact,
            step + " View runs is not narrowed to this run. Match the repository and machine shown above.",
            "/view/machines",
            "View runs",
        )
    if alert.kind == "blocked":
        summary = "Queue reports: " + activity.REASONS.get(alert.detail, "blocked work") + "."
        if alert.detail in {"blocked", "blocker", "", deck.UNKNOWN}:
            summary = "Queue reported blocked work. No specific blocker was recorded."
        steps = {
            "acceptance-required": "Review and supply the task's acceptance criteria at its source.",
            "not-ready": "Review the task's readiness requirements at its source.",
            "not-eligible": "Review automatic-work eligibility and the task's source requirements.",
            "manual-mode": "Review the task's instructions for manual execution.",
            "blocker": "Check task or source details for a recorded blocker before changing readiness.",
            "attempt-limit": "Review previous attempts and their logs before deciding whether another attempt is appropriate.",
            "review-after": "Check the task's review date before changing readiness.",
            "source-policy": "Review the source policy and the task's eligibility requirements.",
            "no eligible machine": "Check capacity and routing requirements for this work.",
        }
        step = steps.get(alert.detail, "Review the reported blocker in the task's source before changing readiness.")
        if alert.since == "reported by queue":
            step += " This queue total does not identify individual tasks."
        return _Guidance(
            summary,
            "This queue report excludes the work from automatic dispatch.",
            step,
            "/deck/work" if worklore_enabled else "/deck#queued-title",
            "Stored tasks" if worklore_enabled else "View ready queue",
        )
    if alert.kind == "stale telemetry":
        return _Guidance(
            "No recent capacity report is available to assess this machine."
            if alert.detail == "unknown-telemetry" or alert.since == "no telemetry received"
            else "The last capacity report is too old to trust.",
            "Capacity is uncertain. This does not prove the machine is offline or work has stopped.",
            "Check the reporting agent and machine connection, then look for a fresh report in Capacity.",
        )
    if alert.kind == "no telemetry row":
        return _Guidance(
            "No capacity report was matched to this enrolled machine.",
            "The Deck cannot assess this machine's capacity from this snapshot.",
            "Check the reporting agent and machine connection, then confirm its report appears in Capacity.",
        )
    if alert.kind == "coverage":
        return _Guidance(
            "Monitoring evidence is missing. Evidence lists the gaps in this snapshot.",
            "The Deck cannot assess the missing sections or treat them as healthy.",
            "Check the reporting sources for the listed gaps and review Control plane details in Capacity.",
        )
    if alert.kind == "collision":
        return _Guidance(
            "A work or claim conflict was reported for this repository.",
            "Ownership or edits may overlap. The report alone does not establish a file conflict.",
            "Compare claims and live runs before changing ownership or starting more work.",
            "/deck/repos",
            "View repositories",
        )
    if alert.kind == "integration":
        return _Guidance(
            "An integration routine requested attention.",
            "Pickup and completion are not established by this routine state.",
            "Check the routine's latest report and pending work. Review Integrations in Capacity's Control plane details.",
        )
    return _Guidance(
        "The fleet reported an attention signal.",
        "Its operational impact is unknown from this report.",
        "Check Evidence and the originating report before taking action. Review reporting context in Capacity.",
    )


def render_alerts_page(
    view: deck.DeckView,
    now: datetime,
    *,
    nonce: str,
    worklore_enabled: bool,
    action_sources: frozenset[str] = frozenset(),
) -> str:
    alerts = page_alerts(view, now)
    content = render_alert_rows(alerts, action_sources=action_sources, worklore_enabled=worklore_enabled)
    body = (
        '<main class="deck-shell alerts-page"><header class="page-head"><div><h1>Alerts</h1>'
        f'<p class="page-sub">{len(alerts)} open</p></div></header>'
        f'<section class="panel">{content}</section></main>'
    )
    return deck._document(
        body,
        nonce=nonce,
        now=now,
        active="alerts",
        title="Alerts",
        view=view,
        worklore_enabled=worklore_enabled,
        extra_css=ui_theme.ALERTS_CSS,
    )


def render_alert_rows(
    alerts: Sequence[Alert],
    *,
    action_sources: frozenset[str] = frozenset(),
    compact: bool = False,
    worklore_enabled: bool = True,
) -> str:
    """Keep subject, state, age and the actual action together at every width."""
    rows = []
    for alert in alerts:
        guidance = _guidance(alert, worklore_enabled=worklore_enabled)
        detail = activity.REASONS.get(alert.detail, alert.detail) if alert.kind == "blocked" else alert.detail
        action = fleet_action_page.alert_link(alert.source_ref, action_sources, alert.title)
        navigation = f'<a href="{_esc(guidance.href)}">{_esc(guidance.link)}</a>'
        if action:
            note = (
                "Prompt only. Does not execute repair."
                if compact
                else "Preparing or copying a prompt does not execute a repair."
            )
            action += f'<small class="alert-action-note">{note}</small>'
            if not compact:
                action += navigation
        else:
            action = navigation
        explanation = f'<p class="alert-summary">{_esc(guidance.summary)}</p>'
        if not compact:
            explanation += (
                f'<p class="alert-guidance"><strong>Why it matters:</strong> {_esc(guidance.impact)}</p>'
                f'<p class="alert-guidance"><strong>Next step:</strong> {_esc(guidance.next_step)}</p>'
            )
        evidence = (
            f'<details class="{"alert-evidence-inline" if compact else "alert-evidence"}"><summary>Evidence</summary>'
            f'<p class="alert-detail">{_esc(detail)}</p>'
            + (
                f'<p class="alert-detail">Reported code: <code>{_esc(alert.detail)}</code></p>'
                if detail != alert.detail
                else ""
            )
            + "</details>"
        )
        rows.append(
            '<li class="alert-row"><div><span class="attention-kind">' + _esc(alert.kind) + "</span>"
            f'<h3>{_esc(alert.title)}</h3>{explanation}<p class="alert-age">Evidence: {_esc(alert.since)}</p>'
            + (evidence if compact else "")
            + "</div>"
            f'<div class="alert-action">{action}</div>' + ("" if compact else evidence) + "</li>"
        )
    return '<ul class="alert-rows">' + "".join(rows) + "</ul>" if rows else '<p class="empty">No active alerts</p>'
