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
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs

from . import fleet_command_deck, fleet_hub, fleet_hub_model_roster, fleet_hub_preference, fleet_model_roster
from . import fleet_hub_policy, fleet_policy_page, run_preference

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
class PolicyContext:
    """What the authoritative policy document says about the fields on this page.

    Read-only here. Once the authority is active the dropdowns stay usable but
    submit a scoped policy preview instead of a legacy write, so they need the
    document's own revision, roles, and seat names to render honestly.
    """

    revision: int = 0
    roles: dict[str, str] = field(default_factory=dict)
    admission_defaults: dict[str, str] = field(default_factory=dict)
    seats: tuple[str, ...] = ()
    available: bool = False


@dataclass(frozen=True)
class RosterView:
    revision: int
    revision_updated_at: str
    updated_by: str
    seats: tuple[SeatRow, ...]
    cloud: tuple[CloudRow, ...]
    cloud_state: str
    defaults: dict[str, str | None]
    retired: tuple[dict[str, Any], ...]
    preference: dict[str, str | None]
    preference_updated_at: str
    activation: fleet_policy_page.Activation = fleet_policy_page.UNKNOWN_ACTIVATION
    policy: PolicyContext = field(default_factory=PolicyContext)


@dataclass(frozen=True)
class Submission:
    expected_revision: int
    expected_preference_updated_at: str
    expected_cloud_state: str
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


def load_view(
    conn: sqlite3.Connection,
    config: fleet_command_deck.DeckConfig,
    *,
    activation: fleet_policy_page.Activation | None = None,
) -> RosterView:
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
    cloud_state = hashlib.sha256(
        fleet_model_roster.canonical_json([[row.provider, row.enabled] for row in cloud]).encode("ascii")
    ).hexdigest()
    pref_meta = fleet_hub_preference.get_run_preference_meta(conn)
    return RosterView(
        policy=_policy_context(conn),
        revision=int(meta[0]),
        revision_updated_at=str(meta[1]),
        updated_by=str(meta[2] or ""),
        seats=tuple(seats),
        cloud=cloud,
        cloud_state=cloud_state,
        defaults=fleet_hub_model_roster._consumer_defaults(conn),
        retired=tuple(retired_rows),
        preference=fleet_hub_preference.get_run_preference(conn),
        preference_updated_at=str(pref_meta["updated_at"] or ""),
        activation=activation if activation is not None else fleet_policy_page.UNKNOWN_ACTIVATION,
    )


def _policy_context(conn: sqlite3.Connection) -> PolicyContext:
    """Read the authoritative document. An unreadable authority stays unavailable."""
    try:
        current = fleet_hub_policy.current_policy(conn)
        document = current["document"]
        consumers = document["consumers"]
        return PolicyContext(
            revision=int(current["revision"]),
            roles={key: str(value) for key, value in (document["defaults"].get("roles") or {}).items()},
            admission_defaults={
                consumer: str(
                    ((consumers.get(consumer) or {}).get("default_patches") or {})
                    .get("roles", {})
                    .get(fleet_policy_page.ADMISSION_ROLE, "")
                    or ""
                )
                for consumer in CONSUMERS
            },
            seats=tuple(sorted(document["seats"])),
            available=True,
        )
    except (fleet_hub.FleetHubError, sqlite3.Error, KeyError, TypeError, ValueError):
        return PolicyContext()


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
        f"{' checked' if checked else ''}{'' if editable else ' disabled'}>"
    )


def _policy_select(name: str, current: str, seats: tuple[str, ...]) -> str:
    """A seat dropdown backed by the policy document's own seat names."""
    options = [f'<option value=""{" selected" if not current else ""}>(unset)</option>']
    options.extend(
        f'<option value="{_esc(seat)}"{" selected" if seat == current else ""}>{_esc(seat)}</option>' for seat in seats
    )
    if current and current not in seats:
        options.append(f'<option value="{_esc(current)}" selected>{_esc(current)} (not in policy)</option>')
    return f'<select name="{_esc(name)}">{"".join(options)}</select>'


def _policy_hidden(scope: str, target: str, revision: int, csrf: str) -> str:
    return (
        f'<input type="hidden" name="scope" value="{_esc(scope)}">'
        f'<input type="hidden" name="target" value="{_esc(target)}">'
        f'<input type="hidden" name="expected_version" value="{revision}">'
        f'<input type="hidden" name="csrf" value="{_esc(csrf)}">'
    )


def _policy_preview_actions(reason: str) -> str:
    """Preview only: the policy page renders the diff and the confirm-save step."""
    return (
        '<p class="roster-actions"><input type="hidden" name="reason" '
        f'value="{_esc(reason)}">'
        '<button type="submit" name="action" value="preview">Preview change</button>'
        '<span class="hint">Opens the policy page with the exact diff, the affected consumers and repositories, '
        "and the live sessions. Saving is a separate confirmation there.</span></p>"
    )


ADMISSION_HINT = (
    '<p class="hint">This is the seat the low-level admission call falls back to when a caller asks to admit '
    "work without naming a seat. Ordinary <code>brigade run</code> passes an explicit worker, or resolves a "
    "seat from the run role preferences above, so it does not reach this fallback; the t3-fleet controller "
    "normally requires an explicit seat too. It only affects callers that omit the seat. It is not a model, "
    "not a default model, and not a replacement global roster. Left unset, an omitted-seat admission is "
    "refused as <code>default-missing</code> rather than silently picking a seat. The authoritative policy "
    "name for this fallback is <code>admission_default</code>, edited on the "
    '<a href="/deck/policy">policy page</a>; it is a distinct role from <code>impl</code>.</p>'
)


def _authoritative_roles_panel(view: RosterView, policy_csrf: str) -> str:
    """The same Roles dropdowns, submitting one scoped ``defaults.roles`` preview."""
    cells = "".join(
        f"<label>{_esc(fleet_policy_page.ROLE_LABELS.get(role, role))} ({_esc(role)})"
        f"{_policy_select(f'field.role_{role}', view.policy.roles.get(role) or '', view.policy.seats)}</label>"
        for role in ROLES
    )
    return (
        '<section class="panel" aria-labelledby="roles"><header><h2 id="roles">Roles</h2>'
        f'<p class="panel-count">policy revision {view.policy.revision}</p></header>'
        '<form method="post" action="/deck/policy" class="roster-form">'
        + _policy_hidden("defaults", "", view.policy.revision, policy_csrf)
        + f'<div class="roster-grid">{cells}</div>'
        '<p class="hint">These write <code>defaults.roles</code> in the fleet policy document, which is what '
        "dispatch resolves once the authority is active. The keys (impl, review, research, scout, security, "
        "chef) are what consumers read; the names beside them are labels only. Seats come from the policy "
        'document; the full editor is on the <a href="/deck/policy">policy page</a>.</p>'
        + _policy_preview_actions("role change from the roster page")
        + "</form></section>"
    )


def _authoritative_admission_panel(view: RosterView, policy_csrf: str) -> str:
    """One scoped consumer preview per consumer for ``admission_default``."""
    blocks = "".join(
        '<form method="post" action="/deck/policy" class="roster-form">'
        + _policy_hidden("consumer", consumer, view.policy.revision, policy_csrf)
        + f"<label>{_esc(consumer)}"
        + _policy_select(
            f"field.role_{fleet_policy_page.ADMISSION_ROLE}",
            view.policy.admission_defaults.get(consumer) or "",
            view.policy.seats,
        )
        + "</label>"
        + _policy_preview_actions(f"admission fallback for {consumer}")
        + "</form>"
        for consumer in CONSUMERS
    )
    return (
        '<section class="panel" aria-labelledby="defaults"><header>'
        '<h2 id="defaults">Admission fallback seat</h2>'
        f'<p class="panel-count">policy revision {view.policy.revision}</p></header>'
        f'<div class="roster-grid">{blocks}</div>{ADMISSION_HINT}'
        '<p class="hint">Under an active authority this is the consumer\'s '
        "<code>default_patches.roles.admission_default</code> leaf, not the legacy consumer-defaults table.</p>"
        "</section>"
    )


def _authority_banner(activation: fleet_policy_page.Activation) -> str:
    """State plainly which store these dropdowns write, and link the expert path."""
    if activation.active:
        return (
            '<p class="banner">Policy authority is <strong>active</strong>: role assignments are owned by the '
            "fleet policy document, so the dropdowns below now edit that document. Changing one opens a preview "
            'on the <a href="/deck/policy">policy page</a>, where the diff is confirmed and written under a '
            "compare-and-swap. This page no longer writes run preference directly.</p>"
        )
    staged = "not activated" if activation.state == "staged" else "unknown"
    return (
        f'<p class="banner">Policy authority is {_esc(staged)}. The Roles dropdowns below still write the legacy '
        "run preference, which is what dispatch reads today. The fleet policy document is a "
        "<strong>staged policy</strong> until the migration reports activation; edit it on the "
        '<a href="/deck/policy">policy page</a> for authoritative roles, seats, consumer overrides, and repository '
        "overrides.</p>"
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
    policy_csrf: str = "",
) -> str:
    """The roster page. ``submission`` re-selects what the operator sent on a 409 or 422.

    ``policy_csrf`` is the policy page's form token. It is only used once the
    authority is active, when the role and admission-fallback dropdowns post a
    scoped policy preview instead of a legacy run-preference write.
    """
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
    # A live policy authority owns roles and the admission fallback. The
    # controls stay usable, but they stop writing the legacy store: they submit
    # a scoped preview against the policy document instead. If the document
    # cannot be read, the controls go read-only rather than write anywhere.
    authoritative = editable and view.activation.active and view.policy.available
    legacy_editable = editable and not view.activation.active
    parts: list[str] = ['<main class="deck-shell">']
    parts.append(
        '<header class="masthead"><div><p class="eyebrow">Fleet operations</p><h1>Command Deck &middot; Roster</h1>'
        f'<p class="station-meta">revision {view.revision}, updated {_esc(view.revision_updated_at)} by '
        f"{_esc(view.updated_by or 'unknown')}</p></div>"
        f'<p class="header-meta">{_esc(fleet_command_deck._stamp(now))}</p></header>'
    )
    parts.append(
        '<nav aria-label="Command Deck"><a href="/">deck</a> <a href="/deck/repos">repos</a> '
        '<a href="/deck/roster">roster</a> <a href="/deck/policy">policy</a> '
        '<a href="/view/machines">machines board</a></nav>'
    )
    parts.append(_authority_banner(view.activation))
    if banner:
        parts.append(f'<p class="banner">{_esc(banner)}</p>')
    if error:
        parts.append(f'<p class="banner banner--error">{_esc(error)}</p>')
    if not editable:
        parts.append('<p class="banner">read-only: enroll with the fleet token to edit</p>')
    # 1. roles. Under an active authority this panel is its own policy form, so
    # it is emitted before the legacy form opens rather than nested inside it.
    if authoritative:
        parts.append(_authoritative_roles_panel(view, policy_csrf))
        parts.append(_authoritative_admission_panel(view, policy_csrf))
    parts.append('<form method="post" action="/deck/roster" class="roster-form">')
    parts.append(f'<input type="hidden" name="expected_revision" value="{view.revision}">')
    parts.append(
        f'<input type="hidden" name="expected_preference_updated_at" value="{_esc(view.preference_updated_at)}">'
    )
    parts.append(f'<input type="hidden" name="expected_cloud_state" value="{_esc(view.cloud_state)}">')
    if editable:
        parts.append(f'<input type="hidden" name="csrf" value="{_esc(csrf)}">')
    if not authoritative:
        role_cells = "".join(
            f"<label>{_esc(fleet_policy_page.ROLE_LABELS.get(role, role))} ({_esc(role)})"
            f"{_select(f'role.{role}', roles.get(role) or '', view.seats, editable=legacy_editable)}</label>"
            for role in ROLES
        )
        notes_disabled = "" if legacy_editable else " disabled"
        unreadable = (
            '<p class="hint">The policy authority is active but its document could not be read, so these '
            "controls are read-only rather than writing a store that no longer owns them.</p>"
            if view.activation.active
            else ""
        )
        parts.append(
            '<section class="panel" aria-labelledby="roles"><header><h2 id="roles">Roles</h2></header>'
            f'<div class="roster-grid">{role_cells}</div>'
            '<p class="hint">These are the run preference roles a dispatch reads when it does not name a seat '
            "explicitly. The keys (impl, review, research, scout, security, chef) are what consumers read; the "
            f"names beside them are labels only.</p>{unreadable}"
            f'<label>notes<textarea name="notes" maxlength="240" rows="2"{notes_disabled}>'
            f"{_esc(notes)}</textarea></label></section>"
        )
    # 2. seats
    seat_rows = []
    for row in view.seats:
        cls = ' class="seat--off"' if row.seat not in seats_on else ""
        flag = ' <span class="flag">retired</span>' if row.retired else ""
        box = _checkbox(
            f"seat.{row.seat}", row.seat in seats_on and not row.retired, editable=editable and not row.retired
        )
        seat_rows.append(
            f"<tr{cls}><td>{_esc(row.seat)}{flag}</td><td>{_esc(row.provider)}/{_esc(row.model)}</td>"
            f"<td>{_esc(row.reasoning)}</td><td>{_esc('-' if row.limit is None else row.limit)}</td>"
            f"<td>{_esc(row.brigade_cli or '-')}</td><td>{_esc(row.t3_instance_id or '-')}</td><td>{box}</td></tr>"
        )
    parts.append(
        '<section class="panel" aria-labelledby="seats"><header><h2 id="seats">Seats</h2>'
        f'<p class="panel-count">{len(view.seats)} seat(s)</p></header><div class="table-wrap"><table class="roster-table">'
        "<thead><tr><th>Seat</th><th>Provider/model</th><th>Reasoning</th><th>Limit</th><th>Brigade CLI</th>"
        f"<th>T3 instance</th><th>On</th></tr></thead><tbody>{''.join(seat_rows)}</tbody></table></div></section>"
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
    # 4. admission fallback seat (the old "consumer defaults")
    default_cells = "".join(
        f"<label>{_esc(consumer)}{_select(f'default.{consumer}', defaults.get(consumer) or '', view.seats, editable=legacy_editable, binding='brigade_cli' if consumer == 'brigade-run' else 't3_instance_id')}</label>"
        for consumer in CONSUMERS
    )
    if not authoritative:
        parts.append(
            '<section class="panel" aria-labelledby="defaults"><header>'
            '<h2 id="defaults">Admission fallback seat (legacy)</h2></header>'
            f'<div class="roster-grid">{default_cells}</div>'
            f"{ADMISSION_HINT}</section>"
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


# --- form ------------------------------------------------------------------


def parse_form(raw: bytes) -> Submission:
    """Decode a form body. Size and content-type are enforced by the HTTP layer."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FormError("form body is not UTF-8") from exc
    try:
        fields = parse_qs(text, keep_blank_values=True, max_num_fields=_MAX_FIELDS)
    except ValueError as exc:
        raise FormError("too many form fields") from exc
    first = {key: values[0] for key, values in fields.items() if values}
    try:
        expected = int(first.get("expected_revision", ""))
    except ValueError as exc:
        raise FormError("expected_revision must be an integer") from exc
    return Submission(
        expected_revision=expected,
        expected_preference_updated_at=first.get("expected_preference_updated_at", ""),
        expected_cloud_state=first.get("expected_cloud_state", ""),
        csrf=first.get("csrf", ""),
        roles={role: first.get(f"role.{role}", "").strip() for role in ROLES},
        notes=first.get("notes", "").strip(),
        seats_on=frozenset(key[len("seat.") :] for key in fields if key.startswith("seat.")),
        cloud_on=frozenset(key[len("cloud.") :] for key in fields if key.startswith("cloud.")),
        defaults={consumer: first.get(f"default.{consumer}", "").strip() for consumer in CONSUMERS},
    )


# --- apply -----------------------------------------------------------------


def _utc_now() -> str:
    return fleet_hub._utc_now()


def _validate(view: RosterView, submission: Submission) -> tuple[str | None, dict[str, bool]]:
    """``(error, target_enabled)``; ``error`` is ``None`` when the save is admissible."""
    known = {row.seat: row for row in view.seats}
    target = {name: (name in submission.seats_on) and not row.retired for name, row in known.items()}
    for role, seat in submission.roles.items():
        if not seat:
            continue
        if seat not in known:
            return f"role {role} names unknown seat {seat}", target
        if not target[seat]:
            return f"role {role} names seat {seat}, which is disabled or retired in this save", target
    for consumer, seat in submission.defaults.items():
        if not seat:
            continue
        if seat not in known:
            return f"default {consumer} names unknown seat {seat}", target
        if not target[seat]:
            return f"default {consumer} names seat {seat}, which is disabled or retired in this save", target
        row = known[seat]
        bound = row.brigade_cli if consumer == "brigade-run" else row.t3_instance_id
        if not bound:
            return f"default {consumer} names seat {seat}, which has no {consumer} binding", target
    raw = {role: seat for role, seat in submission.roles.items() if seat}
    if submission.notes:
        raw["notes"] = submission.notes
    try:
        run_preference.parse_preference(raw)
    except run_preference.RunPreferenceError as exc:
        return str(exc), target
    return None, target


def _write_cloud_enabled(conn: sqlite3.Connection, row: CloudRow, enabled: bool) -> None:
    """Same upsert as ``fleet_hub._set_cloud_policy`` minus its commit; only ``enabled`` differs."""
    policy = row.policy
    conn.execute(
        "INSERT INTO cloud_provider_state (provider, enabled, limit_count, hosted, circuit_state, reason, "
        "subscription_pool, reset_at, expires_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(provider) DO UPDATE SET enabled=excluded.enabled, limit_count=excluded.limit_count, "
        "hosted=excluded.hosted, circuit_state=excluded.circuit_state, reason=excluded.reason, "
        "subscription_pool=excluded.subscription_pool, reset_at=excluded.reset_at, expires_at=excluded.expires_at, "
        "updated_at=excluded.updated_at",
        (
            row.provider,
            int(enabled),
            int(row.limit),
            int(row.hosted),
            row.circuit_state,
            None if enabled else policy.get("reason"),
            policy.get("subscription_pool"),
            None if enabled else policy.get("reset_at"),
            None if enabled else policy.get("expires_at"),
            _utc_now(),
        ),
    )


def _authority_refusal(view: RosterView, submission: Submission) -> str | None:
    """Once the policy authority is active, this form must not be a second writer."""
    if not view.activation.active:
        return None
    wanted = {role: seat for role, seat in submission.roles.items() if seat}
    current = {role: seat for role, seat in view.preference.items() if seat and role in ROLES}
    if wanted != current or submission.notes != (view.preference.get("notes") or ""):
        return (
            "policy authority is active: role assignments are saved on the policy page, which routes them "
            "through preview and a compare-and-swap. This form no longer writes run preference."
        )
    for consumer, seat in submission.defaults.items():
        if seat != (view.defaults.get(consumer) or ""):
            return (
                "policy authority is active: the admission fallback seat is the admission_default role in the "
                "policy document. Change it on the policy page."
            )
    return None


def apply(
    conn: sqlite3.Connection,
    config: fleet_command_deck.DeckConfig,
    submission: Submission,
    *,
    activation: fleet_policy_page.Activation | None = None,
) -> ApplyResult:
    """One Save: fences, validation, then only the differences, in one transaction."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        view = load_view(conn, config, activation=activation)
        refusal = _authority_refusal(view, submission)
        if refusal is not None:
            conn.rollback()
            return ApplyResult("invalid", refusal, view.revision)
        if view.revision != submission.expected_revision:
            conn.rollback()
            return ApplyResult(
                "conflict",
                f"roster changed underneath you: revision {submission.expected_revision} is now {view.revision}. "
                "Reload before saving.",
                view.revision,
            )
        if view.preference_updated_at != submission.expected_preference_updated_at:
            conn.rollback()
            return ApplyResult(
                "conflict", "the run preference changed underneath you. Reload before saving.", view.revision
            )
        if view.cloud_state != submission.expected_cloud_state:
            conn.rollback()
            return ApplyResult(
                "conflict", "the cloud lanes changed underneath you. Reload before saving.", view.revision
            )
        error, target = _validate(view, submission)
        if error is not None:
            conn.rollback()
            return ApplyResult("invalid", error, view.revision)
        roster_changed = False
        for row in view.seats:
            if row.retired or target[row.seat] == row.enabled:
                continue
            written = fleet_hub_model_roster._write_set(
                conn,
                {
                    "seat": row.seat,
                    "provider": row.provider,
                    "model": row.model,
                    "reasoning": row.reasoning,
                    "enabled": target[row.seat],
                    "limit": row.limit,
                    "brigade_cli": row.brigade_cli,
                    "t3_instance_id": row.t3_instance_id,
                    "t3_service_tier": row.t3_service_tier,
                    "notes": row.notes,
                },
            )
            if written.get("error"):
                conn.rollback()
                return ApplyResult("invalid", f"seat {row.seat} is retired and cannot be enabled", view.revision)
            roster_changed = True
        now = _utc_now()
        for consumer, seat in submission.defaults.items():
            if seat == (view.defaults.get(consumer) or ""):
                continue
            conn.execute(
                "INSERT INTO model_consumer_defaults (consumer, seat, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(consumer) DO UPDATE SET seat=excluded.seat, updated_at=excluded.updated_at",
                (consumer, seat, now),
            )
            roster_changed = True
        for cloud_row in view.cloud:
            want = cloud_row.provider in submission.cloud_on
            if want != cloud_row.enabled:
                _write_cloud_enabled(conn, cloud_row, want)
        wanted_pref = {role: seat for role, seat in submission.roles.items() if seat}
        if submission.notes:
            wanted_pref["notes"] = submission.notes
        current_pref = {key: value for key, value in view.preference.items() if value}
        if wanted_pref != current_pref:
            fleet_hub_preference.upsert_run_preference(conn, wanted_pref, updated_by=UPDATED_BY)
        revision = view.revision
        if roster_changed:
            revision += 1
            conn.execute(
                "UPDATE model_roster_meta SET revision=?, updated_at=?, updated_by=? WHERE singleton=1",
                (revision, now, UPDATED_BY),
            )
        conn.commit()
        return ApplyResult("saved", f"saved as revision {revision}", revision)
    except BaseException:
        conn.rollback()
        raise
