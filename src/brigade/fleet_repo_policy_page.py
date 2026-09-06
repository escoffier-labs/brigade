"""Hub-served repos page (``/deck/repos``): repository policy joined to coordination.

The old page was a claim table. This one answers the question an operator
actually has about a repository - *what policy applies here, and what is
happening on it right now* - by joining two server-owned projections:

* ``fleet_command_deck.DeckView.repos``: claims, live runs, and overlap flags.
* ``fleet_policy_page.PolicyView``: the authoritative policy document, its
  per-leaf provenance, and the recorded session states.

The join key is an explicit canonical repository identity carried by the
records themselves. A claim target with no identity is shown with its policy
linkage stated as unknown; nothing is inferred from a basename, and no
ownership or privacy is invented for a repository the policy does not name.
There is no second policy store here: every edit link points at the scoped
repository editor on ``/deck/policy``, which stays the authoritative editor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from . import fleet_command_deck, fleet_policy_page

UNKNOWN = fleet_command_deck.UNKNOWN
_esc = fleet_command_deck._esc

NO_TELEMETRY = (
    "the hub records run events and policy session receipts; it carries no verification receipt or memory "
    "handoff state per repository"
)

# A repository with no ``eligible_machines`` list is not a gap in the hub's
# knowledge: it simply carries no repository-level override, so the fleet
# machine policy applies to it. Naming a concrete machine list here would be an
# invention, because the machines a given piece of work can actually run on also
# depend on the seat and workload constraints resolved at dispatch.
INHERITED_MACHINES = "inherited: no repository override"
MACHINE_POLICY_NOTE = (
    "fleet machine policy applies, narrowed at dispatch by the seat and workload constraints for the job; "
    "this page does not resolve that set"
)


@dataclass(frozen=True)
class LiveLine:
    """One live run on a repository, as raw server-provided text.

    The fields are stored unescaped and escaped exactly once by ``_live_items``.
    Nothing here is pre-formatted into HTML, so there is no path where a value
    reaches the page without passing the renderer's escape.
    """

    node_id: str
    repo: str
    state: str


@dataclass(frozen=True)
class SessionLine:
    session_id: str
    consumer: str
    state: str
    refresh_state: str
    revision: str
    drift: str


@dataclass(frozen=True)
class LeafLine:
    """One resolved policy leaf: its value, where it came from, and whether it is an override."""

    path: str
    value: str
    layer: str
    overridden: bool


@dataclass(frozen=True)
class RepoPolicyRow:
    """One repository, or one claim target whose policy linkage is unknown."""

    label: str
    identity: str
    linked: bool
    linkage_note: str
    in_policy: bool = False
    privacy: str = UNKNOWN
    owner: str = UNKNOWN
    training: str = UNKNOWN
    free: str = UNKNOWN
    eligible_machines: str = UNKNOWN
    eligible_machines_note: str = ""
    policy_revision: str = UNKNOWN
    editor_href: str = ""
    roles: tuple[LeafLine, ...] = ()
    overrides: tuple[LeafLine, ...] = ()
    denied: tuple[str, ...] = ()
    claim_owner: str = ""
    claim_ttl: str = ""
    live: tuple[LiveLine, ...] = ()
    sessions: tuple[SessionLine, ...] = ()
    verification: str = UNKNOWN
    flags: str = ""
    warnings: tuple[str, ...] = field(default_factory=tuple)


def _training_text(effective: dict) -> str:
    value = (effective.get("data") or {}).get("allow_training")
    if value is True:
        return "allowed"
    if value is False:
        return "denied"
    return UNKNOWN


def _free_text(effective: dict) -> str:
    value = (effective.get("data") or {}).get("allow_free")
    if value is True:
        return "allowed"
    if value is False:
        return "denied"
    return UNKNOWN


def _leaf(path: str, source: dict) -> LeafLine:
    layer = source.get("layer")
    value = source.get("value")
    if value is None:
        shown = UNKNOWN
    elif isinstance(value, bool):
        shown = "yes" if value else "no"
    else:
        shown = str(value)
    return LeafLine(
        path=path,
        value=shown,
        layer=fleet_policy_page._layer_text(source),
        overridden=isinstance(layer, str) and layer.startswith("repo:"),
    )


def _sessions_for(policy: fleet_policy_page.PolicyView, identity: str) -> tuple[SessionLine, ...]:
    lines: list[SessionLine] = []
    for row in policy.sessions:
        if row.repository != identity:
            continue
        if row.revision is None:
            drift = "session revision unknown"
        elif int(row.revision) != policy.revision:
            drift = f"drift: loaded revision {row.revision}, policy is at {policy.revision}"
        else:
            drift = "on the current revision"
        lines.append(
            SessionLine(
                session_id=row.session_id,
                consumer=row.consumer,
                state=row.state,
                refresh_state=row.refresh_state,
                revision=UNKNOWN if row.revision is None else str(row.revision),
                drift=drift,
            )
        )
    return tuple(lines)


def _coordination(deck_rows: list[fleet_command_deck.RepoRow]) -> dict:
    """Claim, live runs, and flags for the deck rows that share one identity."""
    claim = next((row.claim for row in deck_rows if row.claim is not None), None)
    live = tuple(
        LiveLine(node_id=run.node_id[:12], repo=run.repo, state=run.state) for row in deck_rows for run in row.live
    )
    flags = " ".join(
        sorted({fleet_command_deck._repo_flags(row) for row in deck_rows if fleet_command_deck._repo_flags(row)})
    )
    return {
        "claim_owner": fleet_command_deck._owner_text(claim) if claim is not None else "",
        "claim_ttl": f"{claim.ttl_remaining}s left" if claim is not None else "",
        "live": live,
        "flags": flags,
    }


def build_rows(
    view: fleet_command_deck.DeckView,
    policy: fleet_policy_page.PolicyView | None,
    *,
    unavailable_reason: str = "",
) -> tuple[RepoPolicyRow, ...]:
    """Join policy repositories to coordination rows on canonical identity only."""
    by_identity: dict[str, list[fleet_command_deck.RepoRow]] = {}
    unlinked: list[fleet_command_deck.RepoRow] = []
    for row in view.repos:
        if row.repo_identity:
            by_identity.setdefault(row.repo_identity, []).append(row)
        else:
            unlinked.append(row)

    rows: list[RepoPolicyRow] = []
    if policy is not None:
        for repo in policy.repositories:
            deck_rows = by_identity.pop(repo.identity, [])
            coordination = _coordination(deck_rows)
            roles = tuple(
                _leaf(f"roles.{role}", dict(repo.sources.get(f"roles.{role}") or {}))
                for role in fleet_policy_page.POLICY_ROLES
                if repo.sources.get(f"roles.{role}")
            )
            overrides = tuple(
                _leaf(path, dict(source))
                for path, source in sorted(repo.sources.items())
                if not path.startswith("roles.")
            )
            rows.append(
                RepoPolicyRow(
                    label=repo.identity,
                    identity=repo.identity,
                    linked=True,
                    linkage_note=(
                        "policy record"
                        if repo.in_policy
                        else "no repository record: every value below is inherited, and owner and privacy "
                        "metadata are not invented for it"
                    ),
                    in_policy=repo.in_policy,
                    privacy=repo.privacy,
                    owner=repo.owner or UNKNOWN,
                    training=_training_text(dict(repo.effective)),
                    free=_free_text(dict(repo.effective)),
                    eligible_machines=", ".join(repo.eligible_machines) or INHERITED_MACHINES,
                    eligible_machines_note=("" if repo.eligible_machines else MACHINE_POLICY_NOTE),
                    policy_revision=str(policy.revision),
                    editor_href=f"/deck/policy#{fleet_policy_page.repository_anchor(repo.identity)}",
                    roles=roles,
                    overrides=overrides,
                    denied=tuple(
                        f"{item.get('path')} requested {item.get('requested')}: {item.get('reason')}"
                        for item in repo.denied_overrides
                    ),
                    claim_owner=coordination["claim_owner"],
                    claim_ttl=coordination["claim_ttl"],
                    live=coordination["live"],
                    sessions=_sessions_for(policy, repo.identity),
                    verification=UNKNOWN,
                    flags=coordination["flags"],
                    warnings=repo.warnings,
                )
            )

    # Anything the policy projection did not cover: a canonical identity the
    # policy view did not return, then claim targets with no identity at all.
    leftovers = [(identity, deck_rows) for identity, deck_rows in sorted(by_identity.items())]
    leftovers.extend((row.repo_identity, [row]) for row in unlinked)
    for identity, deck_rows in leftovers:
        coordination = _coordination(deck_rows)
        note = unavailable_reason or (
            "no canonical repository identity is recorded for this target, so it cannot be joined to a policy "
            "repository without guessing from its name"
            if not identity
            else "this identity is not covered by the policy projection"
        )
        rows.append(
            RepoPolicyRow(
                label=identity or deck_rows[0].target,
                identity=identity,
                linked=False,
                linkage_note=note,
                claim_owner=coordination["claim_owner"],
                claim_ttl=coordination["claim_ttl"],
                live=coordination["live"],
                flags=coordination["flags"],
            )
        )
    return tuple(rows)


def _leaf_items(lines: tuple[LeafLine, ...]) -> str:
    return "".join(
        f"<li><code>{_esc(line.path)}</code>: {_esc(line.value)} &middot; "
        f"{'<strong>override</strong>' if line.overridden else 'inherited'} &middot; {_esc(line.layer)}</li>"
        for line in lines
    )


def _live_items(lines: tuple[LiveLine, ...]) -> str:
    """Escape each live-run field exactly once. The only place ``LiveLine`` becomes HTML."""
    return "".join(
        f"<li>{_esc(line.node_id)} &middot; {_esc(line.repo)} &middot; {_esc(line.state)}</li>" for line in lines
    )


def _row_html(row: RepoPolicyRow) -> str:
    if not row.linked:
        live = _live_items(row.live)
        return (
            f"<details><summary>{_esc(row.label)} &middot; policy linkage {UNKNOWN}</summary>"
            f'<p class="collision">Policy linkage {UNKNOWN}: {_esc(row.linkage_note)}.</p>'
            f"<p>owner {_esc(row.claim_owner or UNKNOWN)} &middot; ttl {_esc(row.claim_ttl or UNKNOWN)} "
            f"&middot; flags {_esc(row.flags or 'none')}</p>"
            + (f'<ul class="attention-list">{live}</ul>' if live else '<p class="empty">No live runs.</p>')
            + "</details>"
        )
    session_items = "".join(
        f"<li>{_esc(line.session_id)} &middot; {_esc(line.consumer)} &middot; state {_esc(line.state)} &middot; "
        f"refresh {_esc(line.refresh_state)} &middot; revision {_esc(line.revision)} &middot; {_esc(line.drift)}</li>"
        for line in row.sessions
    )
    live_items = _live_items(row.live)
    denied_items = "".join(f"<li>{_esc(item)}</li>" for item in row.denied)
    warning_items = "".join(f"<li>{_esc(item)}</li>" for item in row.warnings)
    return (
        f"<details><summary>{_esc(row.label)} &middot; {_esc(row.privacy)} &middot; training "
        f"{_esc(row.training)} &middot; free {_esc(row.free)}</summary>"
        f"<p>owner {_esc(row.owner)} &middot; free models {_esc(row.free)} &middot; eligible machines {_esc(row.eligible_machines)}"
        + (
            f' (<a href="{_esc(row.editor_href)}">{_esc(row.eligible_machines_note)}</a>)'
            if row.eligible_machines_note
            else ""
        )
        + f" &middot; policy revision {_esc(row.policy_revision)} &middot; {_esc(row.linkage_note)}</p>"
        f'<p><a href="{_esc(row.editor_href)}">edit this repository on the policy page</a></p>'
        "<h3>Claims and runs</h3>"
        f"<p>claim {_esc(row.claim_owner or 'none')} &middot; ttl {_esc(row.claim_ttl or '-')} &middot; "
        f"flags {_esc(row.flags or 'none')}</p>"
        + (f'<ul class="attention-list">{live_items}</ul>' if live_items else '<p class="empty">No live runs.</p>')
        + "<h3>Effective roles</h3>"
        + (
            f'<ul class="attention-list">{_leaf_items(row.roles)}</ul>'
            if row.roles
            else '<p class="empty">No role is resolved for this repository.</p>'
        )
        + "<h3>Resolved settings</h3>"
        + (
            f'<ul class="attention-list">{_leaf_items(row.overrides)}</ul>'
            if row.overrides
            else '<p class="empty">No resolved settings.</p>'
        )
        + (f'<h3>Refused overrides</h3><ul class="attention-list">{denied_items}</ul>' if denied_items else "")
        + (f'<h3>Warnings</h3><ul class="attention-list">{warning_items}</ul>' if warning_items else "")
        + "<h3>Sessions</h3>"
        + (
            f'<ul class="attention-list">{session_items}</ul>'
            if session_items
            else '<p class="empty">No session receipts for this repository.</p>'
        )
        + f'<p class="station-meta">verification and handoff state: {_esc(row.verification)}. '
        f"{_esc(NO_TELEMETRY)}.</p></details>"
    )


def render(
    view: fleet_command_deck.DeckView,
    *,
    rows: tuple[RepoPolicyRow, ...] = (),
    policy_available: bool = False,
    unavailable_reason: str = "",
    nonce: str,
    now: datetime,
) -> str:
    """The repos page: policy per repository, then the claim/run coordination table."""
    linked = sum(1 for row in rows if row.linked)
    if policy_available:
        state = (
            f'<p class="station-meta">{linked} repository/repositories joined by canonical identity. '
            "Values come from the policy authority; this page never writes policy.</p>"
        )
    else:
        state = (
            f'<p class="collision">Repository policy is {UNKNOWN}'
            + (f": {_esc(unavailable_reason)}" if unavailable_reason else "")
            + ". Claim and run coordination below is unaffected.</p>"
        )
    blocks = "".join(_row_html(row) for row in rows) or '<p class="empty">No repositories to show.</p>'
    body = (
        '<main class="deck-shell"><header class="masthead"><div><p class="eyebrow">Fleet operations</p>'
        "<h1>Command Deck &middot; Repos</h1></div>"
        f'<p class="header-meta">{_esc(fleet_command_deck._stamp(now))}</p></header>'
        '<nav aria-label="Command Deck"><a href="/">deck</a> <a href="/deck/repos">repos</a> '
        '<a href="/deck/roster">roster</a> <a href="/deck/policy">policy</a> '
        '<a href="/view/machines">machines board</a></nav>'
        '<section class="panel" aria-labelledby="repo-policy"><header>'
        '<h2 id="repo-policy">Repository policy</h2>'
        f'<p class="panel-count">{len(rows)} target(s)</p></header>'
        f"{state}{blocks}</section>"
        '<section class="repo-panel" aria-label="Repository coordination">'
        "<h2>Claims and live runs</h2>"
        + fleet_command_deck.repo_coordination_table(view)
        + '</section><footer><a href="/view/machines">machines board</a> '
        '<a href="/view/repos">repos board</a></footer></main>'
    )
    # No meta refresh: a 10-second reload would collapse every open repository
    # while an operator is reading it.
    return fleet_command_deck._document(body, nonce=nonce, now=now, title="Repos", refresh=False)
