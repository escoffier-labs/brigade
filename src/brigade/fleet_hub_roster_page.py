"""Hub-served roster page (``/deck/roster``): projection, HTML, form parse, apply.

Everything here is a pure function of a hub connection and a startup-frozen
deck config, so tests render without a socket. ``fleet_hub_http`` owns
authentication, the CSRF and same-origin checks, and body limits; this
module owns what the page shows and the one transaction a Save performs.
No token, cookie value, or identity is ever rendered.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.parse import parse_qs

from . import fleet_command_deck, fleet_hub, fleet_hub_model_roster, fleet_hub_preference, fleet_model_roster
from . import run_preference

CSRF_PURPOSE = b"brigade.fleet-roster-form.v1"
MAX_FORM_BYTES = 64 * 1024
FORM_CONTENT_TYPE = "application/x-www-form-urlencoded"
ROLES = run_preference.ROLE_FIELDS
CONSUMERS = ("brigade-run", "t3-fleet")
UPDATED_BY = "deck-form"
_MAX_FIELDS = 512


class FormError(ValueError):
    """The submitted form is malformed (not a policy failure)."""


def csrf_value(token: str) -> str:
    """Hidden form token derived from the admin token; distinct from the cookie."""
    return hmac.new(token.encode("utf-8"), CSRF_PURPOSE, hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class SeatRow:
    seat: str
    provider: str
    model: str
    reasoning: str
    limit: int | None
    enabled: bool
    brigade_cli: str
    t3_instance_id: str
    t3_service_tier: str
    notes: str | None
    retired: bool


@dataclass(frozen=True)
class CloudRow:
    provider: str
    enabled: bool
    limit: int
    hosted: bool
    circuit_state: str
    policy: dict[str, Any]


@dataclass(frozen=True)
class RosterView:
    revision: int
    revision_updated_at: str
    updated_by: str
    seats: tuple[SeatRow, ...]
    cloud: tuple[CloudRow, ...]
    defaults: dict[str, str | None]
    retired: tuple[dict[str, Any], ...]
    preference: dict[str, str | None]
    preference_updated_at: str


@dataclass(frozen=True)
class Submission:
    expected_revision: int
    expected_preference_updated_at: str
    csrf: str
    roles: dict[str, str]
    notes: str
    seats_on: frozenset[str]
    cloud_on: frozenset[str]
    defaults: dict[str, str]


@dataclass(frozen=True)
class ApplyResult:
    status: str  # "saved" | "conflict" | "invalid"
    message: str
    revision: int


# --- projection --------------------------------------------------------------


def load_view(conn: sqlite3.Connection, config: fleet_command_deck.DeckConfig) -> RosterView:
    meta = conn.execute("SELECT revision, updated_at, updated_by FROM model_roster_meta WHERE singleton=1").fetchone()
    if meta is None:
        raise fleet_hub.FleetHubError("model roster revision metadata is missing")
    retired_rows = fleet_hub_model_roster._retired_rows(conn)
    seats: list[SeatRow] = []
    for row in conn.execute(
        "SELECT seat, provider, model, reasoning, enabled, limit_count, brigade_cli, t3_instance_id, "
        "t3_service_tier, notes FROM model_policy ORDER BY seat"
    ).fetchall():
        seats.append(
            SeatRow(
                seat=str(row[0]),
                provider=str(row[1]),
                model=str(row[2]),
                reasoning=str(row[3] or "none"),
                limit=None if row[5] is None else int(row[5]),
                enabled=bool(row[4]),
                brigade_cli=str(row[6] or ""),
                t3_instance_id=str(row[7] or ""),
                t3_service_tier=str(row[8] or ""),
                notes=row[9],
                retired=fleet_model_roster.retired_reason(str(row[1]), str(row[2]), retired_rows) is not None,
            )
        )
    providers = fleet_hub._cloud_policy(conn, config)["providers"]
    cloud = tuple(
        CloudRow(
            provider=name,
            enabled=bool(policy.get("enabled")),
            limit=int(policy.get("limit", 0)),
            hosted=bool(policy.get("hosted", True)),
            circuit_state=str(policy.get("circuit_state", "closed")),
            policy=dict(policy),
        )
        for name, policy in sorted(providers.items())
    )
    pref_meta = fleet_hub_preference.get_run_preference_meta(conn)
    return RosterView(
        revision=int(meta[0]),
        revision_updated_at=str(meta[1]),
        updated_by=str(meta[2] or ""),
        seats=tuple(seats),
        cloud=cloud,
        defaults=fleet_hub_model_roster._consumer_defaults(conn),
        retired=tuple(retired_rows),
        preference=fleet_hub_preference.get_run_preference(conn),
        preference_updated_at=str(pref_meta["updated_at"] or ""),
    )


# --- render ------------------------------------------------------------------


def _esc(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _select(name: str, current: str, seats: tuple[SeatRow, ...], *, editable: bool, binding: str | None = None) -> str:
    """One ``<select>``: ``(unset)`` first, usable seats, then an optgroup of the rest."""
    usable: list[str] = []
    rest: list[str] = []
    for row in seats:
        bound = True if binding is None else bool(getattr(row, binding))
        (usable if row.enabled and not row.retired and bound else rest).append(row.seat)
    disabled = "" if editable else " disabled"

    def option(seat: str) -> str:
        selected = " selected" if seat == current else ""
        return f'<option value="{_esc(seat)}"{selected}>{_esc(seat)}</option>'

    parts = [f'<select name="{_esc(name)}"{disabled}>']
    parts.append(f'<option value=""{" selected" if not current else ""}>(unset)</option>')
    parts.extend(option(seat) for seat in usable)
    if rest:
        label = "disabled" if binding is None else "no binding"
        parts.append(f'<optgroup label="{label}">' + "".join(option(seat) for seat in rest) + "</optgroup>")
    parts.append("</select>")
    return "".join(parts)


def _checkbox(name: str, checked: bool, *, editable: bool) -> str:
    return (
        f'<input type="checkbox" name="{_esc(name)}" value="1"'
        f'{" checked" if checked else ""}{"" if editable else " disabled"}>'
    )


def render(
    view: RosterView,
    *,
    nonce: str,
    now: datetime,
    csrf: str,
    editable: bool,
    banner: str | None = None,
    error: str | None = None,
    submission: Submission | None = None,
) -> str:
    """The roster page. ``submission`` re-selects what the operator sent on a 409 or 422."""
    roles = dict(view.preference)
    notes = view.preference.get("notes") or ""
    seats_on = {row.seat for row in view.seats if row.enabled}
    cloud_on = {row.provider for row in view.cloud if row.enabled}
    defaults = dict(view.defaults)
    if submission is not None:
        roles.update(submission.roles)
        notes = submission.notes
        seats_on = set(submission.seats_on)
        cloud_on = set(submission.cloud_on)
        defaults.update(submission.defaults)
    d = "" if editable else " disabled"
    parts: list[str] = ['<main class="deck-shell">']
    parts.append(
        '<header class="masthead"><div><p class="eyebrow">Fleet operations</p><h1>Command Deck &middot; Roster</h1>'
        f'<p class="station-meta">revision {view.revision}, updated {_esc(view.revision_updated_at)} by '
        f'{_esc(view.updated_by or "unknown")}</p></div>'
        f'<p class="header-meta">{_esc(fleet_command_deck._stamp(now))}</p></header>'
    )
    parts.append(
        '<nav aria-label="Command Deck"><a href="/">deck</a> <a href="/deck/repos">repos</a> '
        '<a href="/deck/roster">roster</a> <a href="/view/machines">machines board</a></nav>'
    )
    if banner:
        parts.append(f'<p class="banner">{_esc(banner)}</p>')
    if error:
        parts.append(f'<p class="banner banner--error">{_esc(error)}</p>')
    if not editable:
        parts.append('<p class="banner">read-only: enroll with the fleet token to edit</p>')
    parts.append('<form method="post" action="/deck/roster" class="roster-form">')
    parts.append(f'<input type="hidden" name="expected_revision" value="{view.revision}">')
    parts.append(
        f'<input type="hidden" name="expected_preference_updated_at" value="{_esc(view.preference_updated_at)}">'
    )
    if editable:
        parts.append(f'<input type="hidden" name="csrf" value="{_esc(csrf)}">')
    # 1. roles
    role_cells = "".join(
        f'<label>{_esc(role)}{_select(f"role.{role}", roles.get(role) or "", view.seats, editable=editable)}</label>'
        for role in ROLES
    )
    parts.append(
        '<section class="panel" aria-labelledby="roles"><header><h2 id="roles">Roles</h2></header>'
        f'<div class="roster-grid">{role_cells}</div>'
        f'<label>notes<textarea name="notes" maxlength="240" rows="2"{d}>{_esc(notes)}</textarea></label></section>'
    )
    # 2. seats
    seat_rows = []
    for row in view.seats:
        cls = ' class="seat--off"' if row.seat not in seats_on else ""
        flag = ' <span class="flag">retired</span>' if row.retired else ""
        box = _checkbox(f"seat.{row.seat}", row.seat in seats_on and not row.retired, editable=editable and not row.retired)
        seat_rows.append(
            f"<tr{cls}><td>{_esc(row.seat)}{flag}</td><td>{_esc(row.provider)}/{_esc(row.model)}</td>"
            f"<td>{_esc(row.reasoning)}</td><td>{_esc('-' if row.limit is None else row.limit)}</td>"
            f"<td>{_esc(row.brigade_cli or '-')}</td><td>{_esc(row.t3_instance_id or '-')}</td><td>{box}</td></tr>"
        )
    parts.append(
        '<section class="panel" aria-labelledby="seats"><header><h2 id="seats">Seats</h2>'
        f'<p class="panel-count">{len(view.seats)} seat(s)</p></header><div class="table-wrap"><table class="roster-table">'
        "<thead><tr><th>Seat</th><th>Provider/model</th><th>Reasoning</th><th>Limit</th><th>Brigade CLI</th>"
        f'<th>T3 instance</th><th>On</th></tr></thead><tbody>{"".join(seat_rows)}</tbody></table></div></section>'
    )
    # 3. cloud lanes
    cloud_rows = "".join(
        f"<tr><td>{_esc(row.provider)}</td><td>{_checkbox(f'cloud.{row.provider}', row.provider in cloud_on, editable=editable)}</td>"
        f"<td>{row.limit}</td><td>{'yes' if row.hosted else 'no'}</td><td>{_esc(row.circuit_state)}</td></tr>"
        for row in view.cloud
    )
    parts.append(
        '<section class="panel" aria-labelledby="cloud"><header><h2 id="cloud">Cloud lanes</h2></header>'
        '<div class="table-wrap"><table class="roster-table"><thead><tr><th>Provider</th><th>On</th><th>Limit</th>'
        f"<th>Hosted</th><th>Circuit</th></tr></thead><tbody>{cloud_rows}</tbody></table></div></section>"
    )
    # 4. consumer defaults
    default_cells = "".join(
        f'<label>{_esc(consumer)}{_select(f"default.{consumer}", defaults.get(consumer) or "", view.seats, editable=editable, binding="brigade_cli" if consumer == "brigade-run" else "t3_instance_id")}</label>'
        for consumer in CONSUMERS
    )
    parts.append(
        '<section class="panel" aria-labelledby="defaults"><header><h2 id="defaults">Consumer defaults</h2></header>'
        f'<div class="roster-grid">{default_cells}</div></section>'
    )
    # 5. retired
    retired_items = "".join(
        f"<li>{_esc(item['provider'])}/{_esc(item['family'])} &middot; "
        f"{'permanent' if item.get('permanent') else 'operator'} &middot; {_esc(item.get('reason_code'))}</li>"
        for item in view.retired
    )
    parts.append(
        '<section class="panel" aria-labelledby="retired"><header><h2 id="retired">Retired families</h2></header>'
        + (f'<ul class="observer-list">{retired_items}</ul>' if retired_items else '<p class="empty">None.</p>')
        + "</section>"
    )
    if editable:
        parts.append('<p class="roster-actions"><button type="submit">Save</button></p>')
    parts.append("</form></main>")
    return fleet_command_deck._document("\n".join(parts), nonce=nonce, now=now, title="Roster", refresh=False)
