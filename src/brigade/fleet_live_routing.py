"""Bounded read-only routing presentation shared by Settings and Overview."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from html import escape
from typing import TYPE_CHECKING

from . import fleet_admin_explorer as explorer, fleet_investigation as investigation

if TYPE_CHECKING:
    from .fleet_hub_roster_page import RosterView, SeatRow, PolicySeat

CSS = """
.overview-routing{border:1px solid var(--line);border-radius:var(--radius-card);background:transparent}
.overview-routing header{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;padding:12px 16px;border-bottom:1px solid var(--line)}
.overview-routing :is(h2,h3){margin:0;font-size:14px;font-weight:600}.overview-routing p{color:var(--muted);font-size:12px;margin:0;padding:8px 16px}
.routing-roles{display:grid;grid-template-columns:repeat(2,minmax(0,1fr))}
.routing-role{display:flex;align-items:baseline;justify-content:space-between;gap:12px;padding:9px 16px;border-top:1px solid var(--line-quiet);min-width:0;overflow-wrap:anywhere;font-size:13px}
.routing-roles .routing-role:nth-child(-n+2){border-top:0}
.routing-roles .routing-role:nth-child(odd){border-right:1px solid var(--line-quiet)}
.routing-role:hover{background:var(--surface-raised)}
.routing-role span{font-size:13px;color:var(--muted);flex:0 0 auto}
.routing-role strong{font-size:13px;color:var(--ink);font-weight:500;text-align:right}
.overview-routing .investigation{padding:0 16px 8px}
.routing-approved{border-top:1px solid var(--line)}
.routing-approved .routing-role div{min-width:0;text-align:right}
.routing-approved code{display:block;color:var(--muted);font-size:11px;margin-top:3px}
.routing-approved .routing-role:hover{background:transparent}
@media(max-width:700px){.routing-roles{grid-template-columns:minmax(0,1fr)}.routing-roles .routing-role:nth-child(odd){border-right:0}.routing-roles .routing-role:nth-child(2){border-top:1px solid var(--line-quiet)}}
@media(max-width:700px){.routing-approved .routing-role{display:block}.routing-approved .routing-role div,.routing-approved .routing-role strong{text-align:left}}
"""


# Maintained by hand for display only. Dispatch does not read this list.
_APPROVED_MODELS = (
    (
        "Worker",
        "GPT 6.2 Sol (medium), GPT 6 Luna, DeepSeek V4.1 Flash (opencode Go), Claude Sonnet 5.5 (Claude builds)",
        "gpt-6.2-sol, gpt-6-luna, opencode-go/deepseek-v4.1-flash, claude-sonnet-5-5",
    ),
    (
        "Reviewer",
        "DeepSeek V4.1 Flash (opencode Go) and Gemini 4 Hydrogen (first pass), Claude Opus 5.5 (final)",
        "opencode-go/deepseek-v4.1-flash, gemini-4-hydrogen-high, claude-opus-5-5",
    ),
    ("Orchestrator", "Claude Opus 5.5", "claude-opus-5-5"),
    ("Researcher and Scout", "DeepSeek V4.1 Flash (opencode Go) first", "opencode-go/deepseek-v4.1-flash"),
    ("Security reviewer", "GPT Daybreak Blue", "gpt-daybreak-blue-latest"),
    (
        "Second opinion",
        "GPT 6.2 Astra and Claude Fable 5.2, low or medium effort, sign-off required",
        "gpt-6.2-astra, claude-fable-5-2",
    ),
    ("Writing", "Gemini 4 Neon (via agy)", "gemini-4-neon"),
)


@dataclass(frozen=True)
class Assignment:
    key: str
    label: str
    seat: str | None
    model: str
    provider: str = ""
    reasoning: str = ""
    state: str = ""

    @property
    def problem(self) -> bool:
        return bool(self.state) or self.model in {"Assignment unknown", "Model configuration missing"}


@dataclass(frozen=True)
class Routing:
    roles: tuple[Assignment, ...]
    fallbacks: tuple[Assignment, ...]
    source: str
    activation: str
    detail: str
    uncertain: bool
    error_code: str = ""


def project(view: RosterView) -> Routing:
    """The roster's existing authority decision and seat resolver, shared once."""
    from . import fleet_policy_page, run_preference

    roles: Mapping[str, str | None] = view.preference
    defaults: Mapping[str, str | None] = view.defaults
    seats: Mapping[str, SeatRow | PolicySeat] = {row.seat: row for row in view.seats}
    source = "run preference"
    if view.activation.active:
        roles = view.policy.roles if view.policy.available else dict.fromkeys(run_preference.ROLE_FIELDS, "unknown")
        defaults = view.policy.admission_defaults if view.policy.available else {}
        seats = view.policy.seat_details if view.policy.available else {}
        source = "active policy" if view.policy.available else "active policy unavailable"

    def assignment(key: str, label: str, assigned: str | None) -> Assignment:
        row = seats.get(assigned or "")
        model = "Not assigned" if not assigned else "Model configuration missing" if row is None else row.model
        state = ""
        if assigned == "unknown":
            model = "Assignment unknown"
        elif row is not None:
            if row.retired:
                state = "Retired seat"
            elif not row.enabled:
                state = "Disabled seat"
        return Assignment(key, label, assigned, model, row.provider if row else "", row.reasoning if row else "", state)

    detail = {
        "active": "Current assignments from active settings.",
        "staged": "Current assignments. Saved settings are not active.",
        "unknown": "Activation unknown. Current assignments come from run preference.",
    }.get(view.activation.state, "Activation unknown.")
    if view.activation.active and not view.policy.available:
        detail = "Active settings unavailable. Current assignments are unknown."
    return Routing(
        roles=tuple(
            assignment(role, fleet_policy_page.ROLE_LABELS.get(role, role), roles.get(role))
            for role in run_preference.ROLE_FIELDS
        ),
        fallbacks=tuple(
            assignment(
                "fallback:" + consumer,
                {"brigade-run": "Brigade dispatch fallback", "t3-fleet": "T3 dispatch fallback"}.get(
                    consumer, consumer + " dispatch fallback"
                ),
                seat,
            )
            for consumer, seat in defaults.items()
            if seat
        ),
        source=source,
        activation=view.activation.state,
        detail=detail,
        uncertain=view.activation.state == "unknown" or (view.activation.active and not view.policy.available),
    )


def unavailable(error_code: str) -> Routing:
    from . import fleet_policy_page, run_preference

    return Routing(
        tuple(
            Assignment(role, fleet_policy_page.ROLE_LABELS.get(role, role), "unknown", "Assignment unknown")
            for role in run_preference.ROLE_FIELDS
        ),
        (),
        "unavailable",
        "unknown",
        "Live routing unavailable. Current assignments are unknown.",
        True,
        error_code,
    )


def render_overview(routing: Routing, now: datetime) -> str:
    def row(role: Assignment) -> str:
        detail = " \u00b7 ".join(
            part
            for part in (
                role.model,
                f"{role.reasoning} reasoning" if role.reasoning and role.reasoning != "none" else "",
                role.state,
            )
            if part
        )
        return (
            f'<a class="routing-role" data-routing-role="{escape(role.key)}" href="/deck/roster{explorer.selection_href(role.key)}">'
            f"<span>{escape(role.label)}</span><strong>{escape(detail)}</strong></a>"
        )

    rows = "".join(row(role) for role in routing.roles)
    problems = [role for role in routing.roles if role.problem]
    prompt = (
        investigation.render(
            "Overview live routing",
            route="/deck",
            observation=now,
            facts={
                "Source": routing.source,
                "Activation": routing.activation,
                "Detail": routing.detail,
                "Reported code": routing.error_code,
                "Assignments needing investigation": "; ".join(
                    f"{role.label}: {role.seat}; {role.state or role.model}" for role in problems
                ),
            },
            limits="Page render time; configuration only. Provider availability and sign-in are unverified. No configuration document or exception details included.",
        )
        if routing.uncertain or problems
        else ""
    )
    approved_rows = "".join(
        f'<div class="routing-role"><span>{escape(lane)}</span>'
        f"<div><strong>{escape(models)}</strong><code>{escape(model_ids)}</code></div></div>"
        for lane, models, model_ids in _APPROVED_MODELS
    )
    return (
        '<section class="overview-routing" id="overview-routing" aria-labelledby="overview-routing-title">'
        '<header><h2 id="overview-routing-title">Live routing</h2>'
        '<a class="card-link" href="/deck/roster">View live routing</a></header>'
        f'<div class="routing-roles">{rows}</div>'
        '<div data-audience="agent">'
        f"<p>{escape(routing.detail)} Source: {escape(routing.source)} \u00b7 Activation: {escape(routing.activation)}.</p>"
        "<p>Configured assignment; provider availability and sign-in are unverified.</p></div>"
        f"{prompt}"
        '<section class="routing-approved" aria-labelledby="approved-models">'
        '<header><h3 id="approved-models">Approved models (display only)</h3></header>'
        "<p>Dispatch uses the assignment above.</p>"
        f'<div class="routing-roles">{approved_rows}</div></section></section>'
    )
