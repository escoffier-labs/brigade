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
.overview-routing h2{margin:0;font-size:14px;font-weight:600}.overview-routing p{color:var(--muted);font-size:12px;margin:0;padding:8px 16px}
.routing-roles{display:grid;grid-template-columns:repeat(2,minmax(0,1fr))}
.routing-role{display:flex;align-items:baseline;justify-content:space-between;gap:12px;padding:9px 16px;border-top:1px solid var(--line-quiet);min-width:0;overflow-wrap:anywhere;font-size:13px}
.routing-roles .routing-role:nth-child(-n+2){border-top:0}
.routing-roles .routing-role:nth-child(odd){border-right:1px solid var(--line-quiet)}
.routing-role:hover{background:var(--surface-raised)}
.routing-role span{font-size:13px;color:var(--muted);flex:0 0 auto}
.routing-role strong{font-size:13px;color:var(--ink);font-weight:500;text-align:right}
.overview-routing .investigation{padding:0 16px 8px}
@media(max-width:700px){.routing-roles{grid-template-columns:minmax(0,1fr)}.routing-roles .routing-role:nth-child(odd){border-right:0}.routing-roles .routing-role:nth-child(2){border-top:1px solid var(--line-quiet)}}
"""


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
            f'<a class="routing-role" data-routing-role="{escape(role.key)}" href="/deck/roster#{explorer.anchor(role.key)}">'
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
    return (
        '<section class="overview-routing" id="overview-routing" aria-labelledby="overview-routing-title">'
        '<header><h2 id="overview-routing-title">Live routing</h2>'
        '<a class="card-link" href="/deck/roster">View live routing</a></header>'
        f'<div class="routing-roles">{rows}</div>'
        '<div data-audience="agent">'
        f"<p>{escape(routing.detail)} Source: {escape(routing.source)} \u00b7 Activation: {escape(routing.activation)}.</p>"
        "<p>Configured assignment; provider availability and sign-in are unverified.</p></div>" + prompt + "</section>"
    )
