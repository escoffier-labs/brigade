"""Hub-served policy page (``/deck/policy``): projection, scoped forms, preview, save.

Everything here is a pure function of a hub connection plus two *injected*
projections the page is not allowed to guess:

* ``Inventory`` - a server-owned provider snapshot. The page never probes a
  provider while rendering, and never trusts an inventory claim that arrived in
  a form body. Callers may inject a snapshot for fixtures. When none is
  supplied the page loads the SQL inventory store, mapped through
  ``to_fleet_policy_inventory``. An empty or missing store is ``unavailable``,
  which is deliberately not ``missing``: a provider login that failed is not
  evidence that a saved model is gone. Catalog listings are not treated as
  authenticated CLI execution.
* ``Activation`` - whether the policy authority is live. Until the migration
  module reports activation, the document is labelled a *staged policy* and the
  page says plainly that edits do not yet change runtime behaviour. Activation
  is never inferred from a non-empty document.

Every write goes through ``fleet_hub_policy.preview_policy`` /
``save_policy`` / ``rollback_policy`` under the same compare-and-swap the JSON
API uses, and every edit is scoped: a seat edit rewrites one seat, a repository
edit writes one sparse override. Nothing here copies the whole document to
change one leaf. ``fleet_hub_http`` owns authentication, the CSRF and
same-origin checks, and body limits.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from . import fleet_command_deck, fleet_hub_policy, fleet_policy
from .fleet_hub import FleetHubConflict, FleetHubError
from .fleet_policy_form import ACTIONS as ACTIONS
from .fleet_policy_form import ADMISSION_ROLE as ADMISSION_ROLE
from .fleet_policy_form import POLICY_ROLES as POLICY_ROLES
from .fleet_policy_form import RUNTIME_ROLES as RUNTIME_ROLES
from .fleet_policy_form import SCOPES as SCOPES
from .fleet_policy_form import SEAT_BOOL_FIELDS as SEAT_BOOL_FIELDS
from .fleet_policy_form import SEAT_INT_FIELDS as SEAT_INT_FIELDS
from .fleet_policy_form import SEAT_LIST_FIELDS as SEAT_LIST_FIELDS
from .fleet_policy_form import SEAT_TEXT_FIELDS as SEAT_TEXT_FIELDS
from .fleet_policy_form import DocumentError as DocumentError
from .fleet_policy_form import FormError as FormError
from .fleet_policy_form import Submission as Submission
from .fleet_policy_form import _bool_field as _bool_field
from .fleet_policy_form import build_document as build_document
from .fleet_policy_form import parse_form as parse_form

CSRF_PURPOSE = b"brigade.fleet-policy-form.v1"
# The reviewed-preview handshake. A save is only accepted when it carries a
# token the server itself minted for the exact document it displayed, so a
# document can never be written before its diff was rendered.
REVIEW_PURPOSE = b"brigade.fleet-policy-review.v1"
MAX_FORM_BYTES = 256 * 1024
FORM_CONTENT_TYPE = "application/x-www-form-urlencoded"
UPDATED_BY = "policy-form"
# One word for "the hub does not carry this", shared with the deck so the two
# pages never disagree about what an absent value looks like.
UNKNOWN_TEXT = fleet_command_deck.UNKNOWN


# Canonical runtime role keys. These are the keys consumers actually read; the
# labels are for humans only and are never written into a document.
ROLE_LABELS: dict[str, str] = {
    "impl": "Worker",
    "review": "Reviewer",
    "research": "Researcher",
    "scout": "Scout",
    "security": "Security reviewer",
    "chef": "Orchestrator",
    "admission_default": "Admission fallback seat (legacy)",
}
# The omitted-seat admission fallback. It is a role name in its own right and
# must never be conflated with ``impl``.


_STATE_CODES = {
    "unavailable": "inventory_unavailable",
    "missing": "inventory_missing",
    "retired": "inventory_retired",
    "policy-blocked": "inventory_policy_blocked",
}


def csrf_value(token: str) -> str:
    """Hidden form token derived from the admin token; distinct from the cookie."""
    return hmac.new(token.encode("utf-8"), CSRF_PURPOSE, hashlib.sha256).hexdigest()


def review_secret(token: str) -> str:
    """Key the reviewed-preview tokens are minted under; distinct from the CSRF token."""
    return hmac.new(token.encode("utf-8"), REVIEW_PURPOSE, hashlib.sha256).hexdigest()


def review_token(key: str, *, scope: str, target: str, expected_version: int, digest: str, reason: str) -> str:
    """Bind one previewed change: normalized document digest, base revision, and scope.

    Every field a save could differ on is inside the MAC, so an edited draft,
    a re-pointed target, or a moved base revision all fail the comparison
    instead of silently saving something the operator never saw.
    """
    payload = "\x1f".join((scope, target, str(expected_version), digest, reason))
    return hmac.new(key.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


# --- injected projections -----------------------------------------------------


@dataclass(frozen=True)
class Activation:
    """Whether the policy authority owns runtime behaviour yet.

    ``state`` is ``active``, ``staged``, or ``unknown``. ``unknown`` is the
    default and the honest answer whenever the migration module has not
    reported; it is never upgraded by looking at the document.
    """

    state: str
    detail: str
    compatibility_behavior: str = "unknown"
    source: str = "none"

    @property
    def active(self) -> bool:
        return self.state == "active"


UNKNOWN_ACTIVATION = Activation(
    state="unknown",
    detail="activation status is not available in this build; treat the document as a staged policy",
    compatibility_behavior="unknown",
    source="none",
)


def activation_from_status(status: Mapping[str, Any] | None) -> Activation:
    """Adapt ``fleet_policy_migration.migration_status()`` output. Narrow on purpose."""
    if not isinstance(status, Mapping):
        return UNKNOWN_ACTIVATION
    raw = status.get("activated")
    behavior = status.get("compatibility_behavior")
    behavior_text = behavior if isinstance(behavior, str) and behavior else "unknown"
    if raw is True:
        return Activation("active", "policy authority is active", behavior_text, "fleet_policy_migration")
    if raw is False:
        return Activation(
            "staged",
            "policy authority is not activated; legacy run preference still owns runtime roles",
            behavior_text,
            "fleet_policy_migration",
        )
    return Activation("unknown", UNKNOWN_ACTIVATION.detail, behavior_text, "fleet_policy_migration")


def application_note(activation: Activation) -> str:
    """What saving a revision does to running work, given the activation state.

    Saving is a write to the policy authority, never a reload of a running
    session. Under an active authority the revision reaches a session when that
    session next loads policy or performs its documented refresh or restart;
    under a staged or unknown authority it does not reach runtime at all.
    """
    if activation.active:
        return (
            "The policy authority is active, so future session loads pick this revision up. Sessions already "
            "running keep the revision they loaded until they perform their documented refresh or restart."
        )
    if activation.state == "staged":
        return (
            "The policy authority is not activated, so this revision is staged: it is recorded, and it does "
            "not change runtime routing. Legacy run preference still owns runtime roles."
        )
    return (
        "Activation status is unknown, so this revision is treated as staged: it is recorded, and nothing "
        "here shows it reaching runtime."
    )


def _migration_module() -> Any:
    """The activation authority, or ``None`` while it is still being built."""
    try:
        from . import fleet_policy_migration
    except ImportError:
        return None
    return fleet_policy_migration


def probe_activation(conn: sqlite3.Connection) -> Activation:
    """Ask the migration module if it exists. Absent or erroring means ``unknown``."""
    module = _migration_module()
    status_fn = getattr(module, "migration_status", None) if module is not None else None
    if status_fn is None:
        return UNKNOWN_ACTIVATION
    try:
        return activation_from_status(status_fn(conn))
    except Exception:  # pragma: no cover - a broken adapter must not break the page
        return UNKNOWN_ACTIVATION


@dataclass(frozen=True)
class Inventory:
    """A server-owned provider snapshot, or the explicit absence of one."""

    providers: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    source: str = "unavailable"
    captured_at: str | None = None
    reason: str = "no server-side inventory snapshot"

    @property
    def usable(self) -> bool:
        return self.source != "unavailable" and bool(self.providers)


def unavailable_inventory(reason: str = "no server-side inventory snapshot") -> Inventory:
    return Inventory(providers={}, source="unavailable", captured_at=None, reason=reason)


def _cli_auth_state(coverage: Mapping[str, Any] | None) -> str:
    """CLI authentication coverage only. Catalog and browser listings stay unknown."""
    if not isinstance(coverage, Mapping):
        return UNKNOWN_TEXT
    if coverage.get("reason") == "ambiguous-provider-coverage":
        return UNKNOWN_TEXT
    error_reason = str(coverage.get("error_reason") or "")
    if error_reason in {"authentication-required", "authentication-revoked"}:
        return error_reason
    if coverage.get("evidence_type") == "cli_probe":
        if coverage.get("status") == "ok":
            return "cli_probe"
        return error_reason or UNKNOWN_TEXT
    return UNKNOWN_TEXT


def load_server_inventory(conn: sqlite3.Connection, *, document: Mapping[str, Any] | None = None) -> Inventory:
    """Map the persisted inventory store. Never probes a provider or invents timestamps."""
    from . import fleet_model_inventory

    try:
        fleet_model_inventory.ensure_schema(conn)
        snap = fleet_model_inventory.snapshot(conn)
    except sqlite3.Error:
        return unavailable_inventory("inventory-store-unavailable")
    coverages = snap.get("coverages") if isinstance(snap, Mapping) else None
    if not isinstance(coverages, list) or not coverages:
        return unavailable_inventory("no server-side inventory snapshot")
    policy_document = document
    if policy_document is None:
        policy_document = fleet_hub_policy.current_policy(conn)["document"]
    bindings = fleet_policy.inventory_collector_bindings(policy_document)
    mapped = fleet_model_inventory.to_fleet_policy_inventory(conn, bindings=bindings or None)
    if not mapped:
        return unavailable_inventory("no server-side inventory snapshot")
    raw_providers = snap.get("providers")
    snap_providers: Mapping[str, Any] = raw_providers if isinstance(raw_providers, Mapping) else {}
    providers: dict[str, dict[str, Any]] = {}
    sources: set[str] = set()
    captured: set[str] = set()
    for name, report in mapped.items():
        if not isinstance(report, Mapping):
            continue
        coverage = snap_providers.get(name) if isinstance(snap_providers.get(name), Mapping) else {}
        ambiguous = isinstance(coverage, Mapping) and coverage.get("reason") == "ambiguous-provider-coverage"
        scope = UNKNOWN_TEXT
        observed_at = UNKNOWN_TEXT
        if isinstance(coverage, Mapping) and not ambiguous:
            if coverage.get("scope"):
                scope = str(coverage["scope"])
            if coverage.get("captured_at"):
                observed_at = str(coverage["captured_at"])
                captured.add(observed_at)
            if coverage.get("source"):
                sources.add(str(coverage["source"]))
        providers[str(name)] = {
            "state": report.get("state") or UNKNOWN_TEXT,
            "reason": report.get("reason"),
            "available": list(report.get("available") or []),
            "retired": list(report.get("retired") or []),
            "blocked": list(report.get("blocked") or []),
            "scope": scope,
            "observed_at": observed_at,
            "cli_auth_state": _cli_auth_state(coverage if isinstance(coverage, Mapping) else None),
        }
    if not providers:
        return unavailable_inventory("no server-side inventory snapshot")
    source = next(iter(sources)) if len(sources) == 1 else "unknown"
    captured_at = next(iter(captured)) if len(captured) == 1 else None
    return Inventory(providers=providers, source=source, captured_at=captured_at, reason="")


def adapt_inventory(raw: Any) -> Inventory:
    """Pure adapter mapping callback-provided metadata strictly with UNKNOWN defaults.

    Never imports unverified inventory modules and never probes providers.
    """
    if isinstance(raw, Inventory):
        return raw
    if not isinstance(raw, Mapping):
        return unavailable_inventory("no server-side inventory snapshot")
    source = str(raw.get("source") or "unknown")
    captured_at = raw.get("captured_at")
    captured_at_str = str(captured_at) if captured_at is not None else None
    reason = str(raw.get("reason") or "no server-side inventory snapshot")
    raw_providers = raw.get("providers")
    if not isinstance(raw_providers, Mapping):
        return Inventory(providers={}, source=source, captured_at=captured_at_str, reason=reason)

    providers: dict[str, dict[str, Any]] = {}
    for name, p_data in raw_providers.items():
        if not isinstance(p_data, Mapping):
            continue
        state = str(p_data.get("state") or UNKNOWN_TEXT)
        scope = str(p_data.get("scope") or UNKNOWN_TEXT)
        observed_at = str(p_data.get("observed_at") or captured_at_str or UNKNOWN_TEXT)
        cli_auth_state = str(p_data.get("cli_auth_state") or UNKNOWN_TEXT)
        available = list(p_data.get("available") or [])
        retired = list(p_data.get("retired") or [])
        blocked = list(p_data.get("blocked") or [])
        providers[str(name)] = {
            "state": state,
            "scope": scope,
            "observed_at": observed_at,
            "cli_auth_state": cli_auth_state,
            "available": available,
            "retired": retired,
            "blocked": blocked,
        }
    return Inventory(providers=providers, source=source, captured_at=captured_at_str, reason=reason)


def _inventory_mapping(document: Mapping[str, Any], inventory: Inventory) -> dict[str, Any]:
    """The mapping ``fleet_policy.validate_inventory`` expects."""
    if inventory.usable:
        return dict(inventory.providers)
    providers = {record["provider"] for record in document.get("seats", {}).values()}
    return {name: {"state": "unavailable", "reason": inventory.reason} for name in sorted(providers)}


SUGGEST_LABEL = "related available identifiers (not recommendations, never applied)"
SUGGEST_BASIS = (
    "a shared id prefix says two identifiers come from the same family. It says nothing about benchmark "
    "results, latency, price or quota, privacy terms, or fitness for this seat's task, so none of these is a "
    "recommendation and a longer or higher-numbered id is not an upgrade. The list is alphabetical for that "
    "reason: the order carries no ranking"
)
SUGGEST_LIMIT = 5


def suggest_models(model: str, available: Sequence[str]) -> list[str]:
    """Available identifiers sharing a prefix with ``model``, alphabetically.

    Relatedness only. A shared prefix is evidence of a shared family and of
    nothing else, so the result is presented in a stable alphabetical order
    rather than ranked: ordering by prefix length or version number would imply
    a quality judgement this page has no evidence for. Recommending a model
    needs benchmark, latency, price/quota, privacy, and task-fit evidence; the
    hub carries none of that here and never fetches it. At most
    ``SUGGEST_LIMIT`` are shown, and nothing is ever applied automatically.
    """
    related: set[str] = set()
    for candidate in available:
        shared = 0
        for left, right in zip(model, candidate, strict=False):
            if left != right:
                break
            shared += 1
        if shared >= 4 and candidate != model:
            related.add(candidate)
    return sorted(related)[:SUGGEST_LIMIT]


# --- projection ---------------------------------------------------------------


@dataclass(frozen=True)
class SeatRow:
    name: str
    provider: str
    model: str
    effort: str | None
    eligible_machines: tuple[str, ...]
    concurrency: int | None
    timeout_seconds: int | None
    fallback: tuple[str, ...]
    training_allowed: bool
    retention: str | None
    quota_pool: str | None
    enabled: bool
    pinned: bool
    notes: str | None
    bindings: Mapping[str, Any]
    inventory_state: str
    inventory_reason: str | None
    admissible: bool
    reasons: tuple[str, ...]
    cost_class: str = "unknown"
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ConsumerRow:
    name: str
    reload: str
    coverage: str
    adapter_version: str | None
    legacy_bootstrap: bool
    notes: str | None
    default_patches: Mapping[str, Any]
    seat_bindings: Mapping[str, Any]
    roles: Mapping[str, Any]
    effective: Mapping[str, Any]
    sources: Mapping[str, Any]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class RepositoryRow:
    identity: str
    in_policy: bool
    privacy: str
    owner: str | None
    eligible_machines: tuple[str, ...]
    notes: str | None
    patches: Mapping[str, Any]
    effective: Mapping[str, Any]
    sources: Mapping[str, Any]
    denied_overrides: tuple[Mapping[str, Any], ...]
    constraints: tuple[Mapping[str, Any], ...]
    warnings: tuple[str, ...]


# The frozen contract for a session's *acknowledged snapshot* - the effective
# settings and per-leaf provenance a session actually loaded - is not available
# in this build. What the hub does persist is the acknowledgement itself: who
# acknowledged, where the load came from, when, and which revision and digest.
# Those are shown as recorded. The effective settings of that past load are not
# reconstructed by resolving today's document against the session, because that
# would report the current policy under the name of a past one.
SNAPSHOT_CONTRACT_UNAVAILABLE = (
    "the acknowledged-snapshot contract (the effective settings and per-leaf provenance a session loaded) is "
    "not available in this build, so those stay unknown. They are not reconstructed by resolving the current "
    "document, which would report today's policy as though it were the loaded one"
)


@dataclass(frozen=True)
class SessionRow:
    session_id: str
    consumer: str
    repository: str | None
    revision: int | None
    state: str
    reload: str | None
    refresh_state: str
    loaded_at: str | None
    # Persisted with the acknowledgement itself, never derived from the
    # current document.
    owner_node: str | None = None
    digest: str | None = None
    source: str | None = None
    refresh_requested_at: str | None = None
    receipt_key: str = ""
    external_session_id: str = ""
    context_state: str = "unknown"
    pending_apply_required: bool = False
    current_revision: int | None = None
    context_hash: str | None = None
    origin: str | None = None
    loaded_snapshot: Mapping[str, Any] | None = None
    pending_snapshot: Mapping[str, Any] | None = None
    effective: Mapping[str, Any] | None = None
    sources: Mapping[str, Any] | None = None
    selected: Mapping[str, Any] | None = None
    overrides: Mapping[str, Any] | None = None
    override_reason: str | None = None


@dataclass(frozen=True)
class PolicyView:
    revision: int
    digest: str
    actor: str
    reason: str
    created_at: str
    parent_revision: int | None
    document: Mapping[str, Any]
    seats: tuple[SeatRow, ...]
    consumers: tuple[ConsumerRow, ...]
    repositories: tuple[RepositoryRow, ...]
    sessions: tuple[SessionRow, ...]
    revisions: tuple[Mapping[str, Any], ...]
    machines: tuple[str, ...]
    activation: Activation
    inventory: Inventory
    inventory_summary: Mapping[str, int]


def _roles_for(resolution: Mapping[str, Any]) -> dict[str, Any]:
    return dict(resolution["effective"].get("roles", {}))


def load_view(
    conn: sqlite3.Connection,
    *,
    inventory: Inventory | None = None,
    activation: Activation | None = None,
    history_limit: int = 20,
    known_repositories: Sequence[str] = (),
) -> PolicyView:
    """Project the current policy. Inventory is the injected snapshot or the SQL store."""
    current = fleet_hub_policy.current_policy(conn)
    document = current["document"]
    snapshot = adapt_inventory(inventory) if inventory is not None else load_server_inventory(conn, document=document)
    report = fleet_policy.validate_inventory(document, _inventory_mapping(document, snapshot))
    inventory_rows = {row["seat"]: row for row in report["seats"]}

    fleet_resolution = fleet_policy.resolve_policy(document, None, None)
    seats: list[SeatRow] = []
    for name in sorted(document["seats"]):
        record = document["seats"][name]
        row = inventory_rows.get(name, {})
        verdict = fleet_policy.admissible_seat(document, name, fleet_resolution)
        terms = fleet_policy.effective_seat_terms(document, name, None)
        seats.append(
            SeatRow(
                name=name,
                provider=record["provider"],
                model=record["model"],
                effort=record["effort"],
                eligible_machines=tuple(record["eligible_machines"]),
                concurrency=record["concurrency"],
                timeout_seconds=record["timeout_seconds"],
                fallback=tuple(record["fallback"]),
                training_allowed=record["training_allowed"],
                retention=record["retention"],
                quota_pool=record["quota_pool"],
                enabled=record["enabled"],
                pinned=record["pinned"],
                notes=record["notes"],
                bindings=record["bindings"],
                inventory_state=str(row.get("state", "unavailable")),
                inventory_reason=row.get("reason"),
                admissible=bool(verdict["admissible"]),
                reasons=tuple(verdict["reasons"]),
                cost_class=terms.get("cost_class", record.get("cost_class", "unknown")),
                warnings=tuple(terms.get("warnings", ())),
            )
        )

    consumers: list[ConsumerRow] = []
    for name in sorted(document["consumers"]):
        record = document["consumers"][name]
        resolution = fleet_policy.resolve_policy(document, name, None)
        consumers.append(
            ConsumerRow(
                name=name,
                reload=record["reload"],
                coverage=record["coverage"],
                adapter_version=record["adapter_version"],
                legacy_bootstrap=record["legacy_bootstrap"],
                notes=record["notes"],
                default_patches=record["default_patches"],
                seat_bindings=record["seat_bindings"],
                roles=_roles_for(resolution),
                effective=resolution["effective"],
                sources=resolution["sources"],
                warnings=tuple(resolution["warnings"]),
            )
        )

    identities = sorted(set(document["repositories"]) | {str(item) for item in known_repositories})
    repositories: list[RepositoryRow] = []
    for identity in identities:
        record = document["repositories"].get(identity)
        resolution = fleet_policy.resolve_policy(document, None, identity)
        repositories.append(
            RepositoryRow(
                identity=identity,
                in_policy=record is not None,
                privacy=resolution["repository_privacy"],
                owner=(record or {}).get("owner"),
                eligible_machines=tuple((record or {}).get("eligible_machines", ())),
                notes=(record or {}).get("notes"),
                patches=(record or {}).get("patches", {}),
                effective=resolution["effective"],
                sources=resolution["sources"],
                denied_overrides=tuple(resolution["denied_overrides"]),
                constraints=tuple(resolution["constraints"]),
                warnings=tuple(resolution["warnings"]),
            )
        )

    sessions = tuple(
        SessionRow(
            session_id=row["session_id"],
            consumer=row["consumer"],
            repository=row["repo_identity"],
            revision=row["revision"],
            state=row["state"],
            reload=row["reload"],
            refresh_state=row["refresh_state"],
            loaded_at=row["loaded_at"],
            owner_node=row.get("owner_node"),
            digest=row.get("digest"),
            source=row.get("source"),
            refresh_requested_at=row.get("refresh_requested_at"),
            receipt_key=row.get("receipt_key") or row["session_id"],
            external_session_id=row.get("external_session_id") or row["session_id"],
            context_state=row.get("context_state", "unknown"),
            pending_apply_required=bool(row.get("pending_apply_required")),
            current_revision=row.get("current_revision"),
            context_hash=row.get("context_hash"),
            origin=row.get("origin") or (row.get("loaded_snapshot") or {}).get("origin"),
            loaded_snapshot=row.get("loaded_snapshot"),
            pending_snapshot=row.get("pending_snapshot"),
            effective=row.get("effective"),
            sources=row.get("sources"),
            selected=row.get("selected"),
            overrides=(row.get("loaded_snapshot") or {}).get("overrides"),
            override_reason=(row.get("loaded_snapshot") or {}).get("override_reason"),
        )
        for row in fleet_hub_policy.list_session_states(conn)
    )

    return PolicyView(
        revision=int(current["revision"]),
        digest=str(current["digest"]),
        actor=str(current["actor"] or ""),
        reason=str(current["reason"] or ""),
        created_at=str(current["created_at"] or ""),
        parent_revision=current["parent_revision"],
        document=document,
        seats=tuple(seats),
        consumers=tuple(consumers),
        repositories=tuple(repositories),
        sessions=sessions,
        revisions=tuple(fleet_hub_policy.list_revisions(conn, limit=history_limit)),
        machines=tuple(sorted(document["machines"])),
        activation=activation if activation is not None else UNKNOWN_ACTIVATION,
        inventory=snapshot,
        inventory_summary=dict(report["summary"]),
    )


# --- form ---------------------------------------------------------------------


@dataclass(frozen=True)
class Plan:
    ok: bool
    document: Mapping[str, Any] | None
    diff: Mapping[str, list[dict[str, Any]]]
    affected: Mapping[str, list[str]]
    session_impact: tuple[Mapping[str, Any], ...]
    errors: tuple[Mapping[str, Any], ...]
    digest: str | None
    current_revision: int
    next_revision: int


@dataclass(frozen=True)
class ApplyResult:
    status: str  # "previewed" | "saved" | "conflict" | "invalid" | "blocked" | "review-required"
    message: str
    revision: int
    plan: Plan | None
    # The token that confirms *this* previewed document. Present whenever a
    # plan is admissible, so the page can render an explicit confirm-save.
    review: str | None = None


_EMPTY_DIFF: dict[str, list[dict[str, Any]]] = {"added": [], "removed": [], "changed": []}
_EMPTY_AFFECTED: dict[str, list[str]] = {"consumers": [], "repositories": [], "sessions": []}


def identity_gate(
    current: Mapping[str, Any], proposed: Mapping[str, Any], inventory: Inventory
) -> list[dict[str, Any]]:
    """Refuse a *new* provider/model identity that no trusted snapshot can confirm.

    Only seats whose identity actually changes are gated. An unavailable
    snapshot therefore blocks new unverified identities without freezing every
    other setting in the document.
    """
    before = fleet_policy.parse_document(current)
    after = fleet_policy.parse_document(proposed)
    report = fleet_policy.validate_inventory(after, _inventory_mapping(after, inventory))
    rows = {row["seat"]: row for row in report["seats"]}
    mapping = _inventory_mapping(after, inventory)
    errors: list[dict[str, Any]] = []
    for name in sorted(after["seats"]):
        record = after["seats"][name]
        old = before["seats"].get(name)
        if old is not None and (old["provider"], old["model"]) == (record["provider"], record["model"]):
            continue
        row = rows.get(name, {"state": "unavailable", "reason": "provider-not-inventoried"})
        state = str(row.get("state"))
        if state == "available":
            continue
        provider_report = mapping.get(record["provider"]) if isinstance(mapping, Mapping) else None
        available = list((provider_report or {}).get("available") or [])
        errors.append(
            {
                "code": _STATE_CODES.get(state, "inventory_unavailable"),
                "seat": name,
                "provider": record["provider"],
                "model": record["model"],
                "state": state,
                "reason": row.get("reason"),
                "suggestions": suggest_models(record["model"], available),
            }
        )
    return errors


def _session_impact(conn: sqlite3.Connection, session_ids: Sequence[str]) -> tuple[Mapping[str, Any], ...]:
    rows = {row["session_id"]: row for row in fleet_hub_policy.list_session_states(conn)}
    impact: list[Mapping[str, Any]] = []
    for session_id in session_ids:
        row = rows.get(session_id)
        if row is None:
            continue
        capability = row.get("reload")
        application = {
            "refreshable": "refresh",
            "restart-required": "restart-required",
        }.get(str(capability), "future-sessions")
        impact.append(
            {
                "session_id": session_id,
                "consumer": row["consumer"],
                "repository": row["repo_identity"],
                "state": row["state"],
                "reload": capability,
                "application": application,
            }
        )
    return tuple(impact)


def _revision_document(conn: sqlite3.Connection, revision: int) -> Mapping[str, Any] | None:
    for row in fleet_hub_policy.list_revisions(conn, limit=500):
        if int(row["revision"]) == revision:
            return row["document"]
    return None


def _rollback_target(submission: Submission) -> int:
    raw = submission.value("to_revision") or ""
    try:
        return int(raw)
    except ValueError as exc:
        raise DocumentError("rollback needs an integer revision") from exc


def plan_change(
    conn: sqlite3.Connection,
    submission: Submission,
    *,
    actor: str,
    inventory: Inventory,
    activation: Activation,
) -> Plan:
    """Build the proposed document and dry-run it. Writes nothing."""
    current = fleet_hub_policy.current_policy(conn)
    reason = submission.reason or f"{submission.scope} edit from the policy page"
    try:
        if submission.scope == "rollback":
            target = _rollback_target(submission)
            document = _revision_document(conn, target)
            if document is None:
                raise DocumentError(f"policy revision {target} does not exist")
        else:
            document = build_document(current["document"], submission)
    except DocumentError as exc:
        return Plan(
            ok=False,
            document=None,
            diff=dict(_EMPTY_DIFF),
            affected=dict(_EMPTY_AFFECTED),
            session_impact=(),
            errors=({"code": "invalid_document", "detail": str(exc)},),
            digest=None,
            current_revision=int(current["revision"]),
            next_revision=int(current["revision"]) + 1,
        )
    preview = fleet_hub_policy.preview_policy(
        conn,
        document,
        expected_version=submission.expected_version,
        actor=actor,
        reason=reason,
    )
    errors: list[Mapping[str, Any]] = list(preview["errors"])
    if preview["digest"] is not None:
        errors.extend(identity_gate(current["document"], document, inventory))
    return Plan(
        ok=not errors,
        document=document,
        diff=preview["diff"],
        affected=preview["affected"],
        session_impact=_session_impact(conn, preview["affected"]["sessions"]),
        errors=tuple(errors),
        digest=preview["digest"],
        current_revision=int(preview["current_revision"]),
        next_revision=int(preview["next_revision"]),
    )


def _status_for(errors: Sequence[Mapping[str, Any]]) -> str:
    codes = {error.get("code") for error in errors}
    if "policy_revision_conflict" in codes:
        return "conflict"
    if "invalid_document" in codes:
        return "invalid"
    return "blocked"


def _message_for(status: str, plan: Plan) -> str:
    if status == "conflict":
        return (
            f"the policy changed underneath you: revision {plan.current_revision} is current. "
            "Reload before saving; your draft is kept below."
        )
    details = []
    for error in plan.errors:
        code = str(error.get("code"))
        if code.startswith("inventory_"):
            details.append(f"{error.get('seat')}: {error.get('provider')}/{error.get('model')} is {error.get('state')}")
        elif code in ("pinned-seat-change", "unpin-and-change", "pinned-seat-removed"):
            details.append(f"{error.get('seat')}: {code} ({', '.join(error.get('changed') or [])})")
        else:
            details.append(str(error.get("detail") or code))
    return "; ".join(details) or "the proposed policy was refused"


def apply(
    conn: sqlite3.Connection,
    submission: Submission,
    *,
    actor: str,
    inventory: Inventory,
    activation: Activation,
    review_key: str = "",
) -> ApplyResult:
    """Preview or save one scoped edit under the hub's compare-and-swap.

    A save is only performed when it carries the review token minted for the
    same normalized document, base revision, scope, target, and reason. The
    plan is rebuilt here, so the inventory gate and the compare-and-swap are
    re-evaluated at confirmation rather than trusted from the preview.
    """
    plan = plan_change(conn, submission, actor=actor, inventory=inventory, activation=activation)
    if not plan.ok:
        status = _status_for(plan.errors)
        return ApplyResult(status, _message_for(status, plan), plan.current_revision, plan)
    reason = submission.reason or f"{submission.scope} edit from the policy page"
    minted = review_token(
        review_key,
        scope=submission.scope,
        target=submission.target,
        expected_version=submission.expected_version,
        digest=str(plan.digest or ""),
        reason=reason,
    )
    if submission.action == "preview":
        return ApplyResult(
            "previewed",
            f"preview only: nothing was written. Review the change below, then confirm to create "
            f"revision {plan.next_revision}. {application_note(activation)}",
            plan.current_revision,
            plan,
            review=minted,
        )
    if not hmac.compare_digest(submission.review.encode("utf-8"), minted.encode("utf-8")):
        return ApplyResult(
            "review-required",
            (
                "this save was not confirmed against a preview of the change it makes"
                if not submission.review
                else "the change moved since it was previewed, so the earlier confirmation no longer applies"
            )
            + ". Nothing was written; your draft is kept below. Review the diff and confirm to save.",
            plan.current_revision,
            plan,
            review=minted,
        )
    try:
        if submission.scope == "rollback":
            saved = fleet_hub_policy.rollback_policy(
                conn,
                to_revision=_rollback_target(submission),
                expected_version=submission.expected_version,
                actor=actor,
                reason=reason,
            )
        else:
            assert plan.document is not None
            saved = fleet_hub_policy.save_policy(
                conn,
                plan.document,
                expected_version=submission.expected_version,
                actor=actor,
                reason=reason,
            )
    except FleetHubConflict as exc:
        return ApplyResult("conflict", str(exc), plan.current_revision, plan, review=minted)
    except (FleetHubError, DocumentError) as exc:
        return ApplyResult("invalid", str(exc), plan.current_revision, plan)
    revision = int(saved["revision"])
    return ApplyResult("saved", f"saved as revision {revision}. {application_note(activation)}", revision, plan)


# --- render -------------------------------------------------------------------


def _esc(value: object) -> str:
    return fleet_command_deck._esc(value)


def _text(value: object) -> str:
    return "-" if value is None or value == "" else _esc(value)


def _draft_for(draft: Submission | None, scope: str, target: str = "") -> Mapping[str, str]:
    if draft is None or draft.scope != scope or draft.target != target:
        return {}
    return draft.fields


def _reason_for(draft: Submission | None, scope: str, target: str = "") -> str:
    """The reason the operator typed. It lives beside ``field.*``, not inside it."""
    if draft is None or draft.scope != scope or draft.target != target:
        return ""
    return draft.reason


def _input(name: str, value: object, *, editable: bool, kind: str = "text", extra: str = "") -> str:
    disabled = "" if editable else " disabled"
    shown = "" if value is None else str(value)
    return f'<input type="{kind}" name="field.{_esc(name)}" value="{_esc(shown)}" id="f-{_esc(name)}"{extra}{disabled}>'


def _labelled(label: str, control: str, *, hint: str = "") -> str:
    hint_html = f'<span class="hint">{_esc(hint)}</span>' if hint else ""
    return f"<label>{_esc(label)}{hint_html}{control}</label>"


def _checkbox(name: str, checked: bool, *, editable: bool) -> str:
    return (
        f'<input type="checkbox" name="field.{_esc(name)}" value="1"'
        f"{' checked' if checked else ''}{'' if editable else ' disabled'}>"
    )


def _seat_options(name: str, current: object, seats: Sequence[SeatRow], *, editable: bool) -> str:
    disabled = "" if editable else " disabled"
    parts = [f'<select name="field.{_esc(name)}"{disabled}>']
    selected = "" if current else " selected"
    parts.append(f'<option value=""{selected}>(unset - no fallback)</option>')
    for row in seats:
        mark = " selected" if row.name == current else ""
        suffix = "" if row.enabled else " (disabled)"
        cost_txt = f" [{row.cost_class}]" if getattr(row, "cost_class", None) else ""
        parts.append(f'<option value="{_esc(row.name)}"{mark}>{_esc(row.name)}{_esc(suffix)}{_esc(cost_txt)}</option>')
    parts.append("</select>")
    return "".join(parts)


def _enum_select(name: str, current: object, values: Sequence[str], *, editable: bool, blank: str = "") -> str:
    disabled = "" if editable else " disabled"
    parts = [f'<select name="field.{_esc(name)}"{disabled}>']
    if blank:
        parts.append(f'<option value=""{"" if current else " selected"}>{_esc(blank)}</option>')
    for value in values:
        mark = " selected" if str(value) == str(current) else ""
        parts.append(f'<option value="{_esc(value)}"{mark}>{_esc(value)}</option>')
    parts.append("</select>")
    return "".join(parts)


def _hidden(scope: str, target: str, revision: int, csrf: str) -> str:
    return (
        f'<input type="hidden" name="scope" value="{_esc(scope)}">'
        f'<input type="hidden" name="target" value="{_esc(target)}">'
        f'<input type="hidden" name="expected_version" value="{revision}">'
        f'<input type="hidden" name="csrf" value="{_esc(csrf)}">'
    )


def _actions(reason: str) -> str:
    """Preview only. A save control appears after the server has shown the diff."""
    return (
        f'<p class="roster-actions"><label>reason<input type="text" name="reason" maxlength="240" '
        f'value="{_esc(reason)}"></label> '
        '<button type="submit" name="action" value="preview">Preview change</button>'
        '<span class="hint">Preview shows the exact diff, the affected consumers and repositories, and every '
        "live session before anything is written. Saving is a second, explicit step.</span></p>"
    )


def _confirm_form(draft: Submission | None, token: str | None) -> str:
    """Re-post exactly what was previewed, plus the token that vouches for it."""
    if draft is None or not token:
        return ""
    hidden = [
        f'<input type="hidden" name="scope" value="{_esc(draft.scope)}">',
        f'<input type="hidden" name="target" value="{_esc(draft.target)}">',
        f'<input type="hidden" name="expected_version" value="{draft.expected_version}">',
        f'<input type="hidden" name="csrf" value="{_esc(draft.csrf)}">',
        f'<input type="hidden" name="reason" value="{_esc(draft.reason)}">',
        f'<input type="hidden" name="review" value="{_esc(token)}">',
        '<input type="hidden" name="action" value="save">',
    ]
    hidden.extend(
        f'<input type="hidden" name="field.{_esc(name)}" value="{_esc(value)}">'
        for name, value in sorted(draft.fields.items())
    )
    return (
        '<form method="post" action="/deck/policy" class="roster-form confirm-save">'
        + "".join(hidden)
        + '<p class="roster-actions"><button type="submit">Confirm save</button>'
        '<span class="hint">Saves exactly the document previewed above. The revision check and the provider '
        "inventory check both run again at this point; if either has moved, the save is refused rather than "
        "applied.</span></p></form>"
    )


def _identity_panel(view: PolicyView) -> str:
    inventory = view.inventory
    if inventory.usable:
        snapshot = f"{inventory.source}, captured {inventory.captured_at or 'unknown'}"
    else:
        snapshot = f"unavailable ({inventory.reason})"
    rows = [
        ("version", f"revision {view.revision}"),
        ("digest", view.digest),
        ("source", "fleet hub policy authority"),
        # This is the revision's own created_at, not a session load. Session
        # load times live in the Sessions table, one row per session.
        ("saved at", view.created_at),
        ("saved by", view.actor),
        ("reason", view.reason),
        ("parent revision", "none" if view.parent_revision is None else view.parent_revision),
        ("provider snapshot", snapshot),
    ]
    body = "".join(f"<dt>{_esc(key)}</dt><dd>{_text(value)}</dd>" for key, value in rows)
    links = " ".join(f'<a href="#consumer-{_esc(row.name)}">{_esc(row.name)}</a>' for row in view.consumers)
    consumer_html = f'<p class="hint">consumers: {links}</p>' if links else ""
    return (
        '<section class="panel" aria-labelledby="identity"><header><h2 id="identity">Effective policy</h2></header>'
        f'<dl class="tile-facts">{body}</dl>{consumer_html}{_inventory_note(inventory)}</section>'
    )


def _provider_scope_rows(inventory: Inventory) -> str:
    """Per-provider scope and CLI auth, straight from the snapshot. Never probed.

    Availability and authentication are two different facts and stay in two
    different columns: a provider can list a model while the local CLI is signed
    out, and a signed-in CLI proves nothing about which models exist. Anything
    the snapshot does not carry reads as unknown rather than as a default.
    """
    rows = "".join(
        f"<tr><td>{_esc(name)}</td>"
        f"<td>{_esc(str(report.get('state') or UNKNOWN_TEXT))}</td>"
        f"<td>{_esc(str(report.get('scope') or UNKNOWN_TEXT))}</td>"
        f"<td>{_esc(str(report.get('observed_at') or inventory.captured_at or UNKNOWN_TEXT))}</td>"
        f"<td>{_esc(str(report.get('cli_auth_state') or UNKNOWN_TEXT))}</td></tr>"
        for name, report in sorted(inventory.providers.items())
        if isinstance(report, Mapping)
    )
    if not rows:
        return ""
    return (
        '<div class="table-wrap"><table class="roster-table"><thead><tr><th>Provider</th><th>Snapshot state</th>'
        "<th>Inventory scope</th><th>Observed at</th><th>CLI auth</th></tr></thead>"
        f"<tbody>{rows}</tbody></table></div>"
        '<p class="hint">Inventory scope is what the snapshot covered (for example a browser or cloud listing '
        "versus a native CLI listing); where the snapshot does not state it, it reads as unknown rather than "
        "being assumed. CLI auth is a separate fact from availability: a signed-out CLI is not evidence that a "
        "model is gone, and a signed-in CLI is not evidence that one exists.</p>"
    )


def _inventory_note(inventory: Inventory) -> str:
    """Say what the provider snapshot actually is, including when there is none."""
    if inventory.usable:
        return (
            f'<p class="hint">Model identities are checked against the <code>{_esc(inventory.source)}</code> '
            f"snapshot supplied by the server, observed {_esc(inventory.captured_at or UNKNOWN_TEXT)}. "
            "The page never probes a provider and never reads a credential. "
            "To refresh catalog information, trigger a server inventory refresh and reload the page.</p>"
            + _provider_scope_rows(inventory)
        )
    reason = _esc(inventory.reason)
    return (
        f'<p class="hint">Provider inventory is <strong>unavailable</strong> ({reason}): '
        f"source <code>{_esc(inventory.source)}</code>, "
        f"last observation {_esc(inventory.captured_at or UNKNOWN_TEXT)}, "
        f"scope {UNKNOWN_TEXT}, CLI auth {UNKNOWN_TEXT}. "
        "Until a snapshot is refreshed, new provider/model identities are refused rather than guessed, "
        "and existing configured fields remain editable. "
        "Refresh provider inventory on the server and reload this page to update. "
        "Unavailable is not the same as missing.</p>"
    )


def _activation_panel(view: PolicyView) -> str:
    activation = view.activation
    if activation.active:
        return (
            '<section class="panel" aria-labelledby="activation"><header><h2 id="activation">Authority</h2></header>'
            f"<p>The policy authority is <strong>active</strong> ({_esc(activation.compatibility_behavior)}). "
            "Role assignments are owned here; the roster page no longer writes them directly.</p></section>"
        )
    if activation.state == "staged":
        headline = "The policy authority is <strong>not activated</strong>."
        # The headline already says it; repeating the module's detail here
        # would just be the same sentence twice.
        detail = ""
    else:
        headline = "Activation status is <strong>unknown</strong> (no migration status available)."
        detail = f'<p class="hint">{_esc(activation.detail)}</p>'
    return (
        '<section class="panel banner" aria-labelledby="activation"><header><h2 id="activation">Authority</h2></header>'
        f"<p>{headline} This document is a <strong>staged policy</strong>: saving it records a revision, "
        "but it does not yet change live runtime routing. Legacy run preference still owns runtime roles "
        f"until the migration reports activation.</p>{detail}</section>"
    )


def _scope(names: Sequence[str] | None) -> str:
    return _esc(", ".join(names)) if names else "none"


def _plan_panel(
    plan: Plan | None,
    *,
    activation: Activation,
    draft: Submission | None = None,
    review: str | None = None,
) -> str:
    if plan is None:
        return ""
    error_items = "".join(
        f'<li><span class="attention-kind">{_esc(error.get("code"))}</span> '
        f"{_esc(error.get('seat') or error.get('detail') or '')} "
        + (
            f"&middot; {SUGGEST_LABEL}: {_esc(', '.join(error.get('suggestions') or []))}"
            if error.get("suggestions")
            else ""
        )
        + "</li>"
        for error in plan.errors
    )
    diff_items = "".join(
        f"<li>{_esc(kind)} &middot; {_esc(item['path'])} &middot; "
        f"{_esc(item.get('from', ''))} &rarr; {_esc(item.get('to', ''))}</li>"
        for kind in ("changed", "added", "removed")
        for item in plan.diff.get(kind, [])
    )
    impact_items = "".join(
        f"<li>{_esc(item['session_id'])} &middot; {_esc(item['consumer'])} &middot; state {_esc(item['state'])} "
        f"&middot; applies by <strong>{_esc(item['application'])}</strong></li>"
        for item in plan.session_impact
    )
    affected = plan.affected
    return (
        '<section class="panel" aria-labelledby="plan"><header><h2 id="plan">Proposed change</h2>'
        f'<p class="panel-count">revision {plan.current_revision} &rarr; {plan.next_revision}</p></header>'
        + (f'<ul class="attention-list">{error_items}</ul>' if error_items else "")
        + (f'<ul class="attention-list">{diff_items}</ul>' if diff_items else '<p class="empty">No leaf changes.</p>')
        + f"<p>affected consumers: {_scope(affected.get('consumers'))} &middot; "
        f"repositories: {_scope(affected.get('repositories'))} &middot; "
        f"sessions: {_scope(affected.get('sessions'))}</p>"
        + (
            f"<p>{_esc(application_note(activation))} "
            + (
                "Live sessions apply it as listed:</p>"
                if activation.active
                else "The rows below say how a live session would apply a revision once the authority is "
                "activated; they do not describe an effect of saving today:</p>"
            )
        )
        + (
            f'<ul class="attention-list">{impact_items}</ul>'
            if impact_items
            else '<p class="empty">No live sessions.</p>'
        )
        + _confirm_form(draft, review)
        + "</section>"
    )


def _defaults_form(view: PolicyView, *, editable: bool, csrf: str, draft: Submission | None) -> str:
    values = _draft_for(draft, "defaults")
    roles = view.document["defaults"].get("roles", {})
    data = view.document["defaults"].get("data", {})
    execution = view.document["defaults"].get("execution", {})

    def role_control(role: str) -> str:
        current = values.get(f"role_{role}", roles.get(role) or "")
        label = ROLE_LABELS.get(role, role)
        seat_obj = next((s for s in view.seats if s.name == current), None)
        cost_txt = f" &middot; cost {_esc(seat_obj.cost_class)}" if seat_obj else ""
        hint = f"policy role key: {role}{cost_txt}"
        return _labelled(label, _seat_options(f"role_{role}", current, view.seats, editable=editable), hint=hint)

    role_cells = "".join(role_control(role) for role in POLICY_ROLES)
    training = values.get("data_allow_training", "yes" if data.get("allow_training") else "no")
    allow_free = values.get("data_allow_free", _yes_no(data.get("allow_free")))
    body = (
        f'<div class="roster-grid">{role_cells}</div>'
        '<div class="roster-grid">'
        + _labelled(
            "Allow contributor training",
            _enum_select("data_allow_training", training, ("yes", "no"), editable=editable, blank="(inherit)"),
            hint="policy key: data.allow_training",
        )
        + _labelled(
            "Allow free models",
            _enum_select("data_allow_free", allow_free, ("yes", "no"), editable=editable, blank="(inherit)"),
            hint="policy key: data.allow_free",
        )
        + _labelled(
            "Data retention",
            _input("data_retention", values.get("data_retention", data.get("retention") or ""), editable=editable),
            hint="policy key: data.retention",
        )
        + _labelled(
            "Default machine",
            _input(
                "execution_machine",
                values.get("execution_machine", execution.get("machine") or ""),
                editable=editable,
            ),
            hint="policy key: execution.machine",
        )
        + _labelled(
            "Maximum concurrent workers",
            _input(
                "execution_concurrency",
                values.get(
                    "execution_concurrency",
                    execution.get("concurrency") if execution.get("concurrency") is not None else "",
                ),
                editable=editable,
            ),
            hint="policy key: execution.concurrency",
        )
        + _labelled(
            "Worker timeout (seconds)",
            _input(
                "execution_timeout_seconds",
                values.get(
                    "execution_timeout_seconds",
                    execution.get("timeout_seconds") if execution.get("timeout_seconds") is not None else "",
                ),
                editable=editable,
            ),
            hint="policy key: execution.timeout_seconds",
        )
        + "</div>"
        '<p class="hint">Free-model eligibility and contributor training are configured in the data section '
        "(allow_free and allow_training). A quota pool name is a label, not a price or cost classification. "
        "Quota pools and cost classes stay separate.</p>"
    )
    if not editable:
        return (
            '<section class="panel" aria-labelledby="defaults"><header><h2 id="defaults">Fleet defaults and roles'
            f"</h2></header>{body}</section>"
        )
    return (
        '<section class="panel" aria-labelledby="defaults"><header><h2 id="defaults">Fleet defaults and roles</h2>'
        '</header><form method="post" action="/deck/policy" class="roster-form">'
        + _hidden("defaults", "", view.revision, csrf)
        + body
        + _actions(_reason_for(draft, "defaults"))
        + "</form></section>"
    )


def _seat_row(row: SeatRow) -> str:
    off = "" if row.enabled else ' class="seat--off"'
    reason = "" if not row.inventory_reason else f" ({_esc(row.inventory_reason)})"
    warn = f" ({_esc(', '.join(row.warnings))})" if row.warnings else ""
    return (
        f"<tr{off}><td>{_esc(row.name)}</td>"
        f"<td>{_esc(row.provider)}/{_esc(row.model)}</td><td>{_text(row.effort)}</td>"
        f"<td>{_esc(row.inventory_state)}{reason}</td>"
        f"<td>{_esc(row.cost_class)}{warn}</td>"
        f"<td>{'yes' if row.enabled else 'no'}</td><td>{'yes' if row.pinned else 'no'}</td>"
        f"<td>{'yes' if row.training_allowed else 'no'}</td><td>{_text(row.retention)}</td>"
        f"<td>{_text(row.quota_pool)}</td><td>{_text(', '.join(row.eligible_machines))}</td>"
        f"<td>{_text(row.notes)}</td></tr>"
    )


def _seat_table(view: PolicyView) -> str:
    rows = "".join(_seat_row(row) for row in view.seats)
    legend = (
        '<p class="hint">Inventory states come from a server-owned snapshot only. '
        "&quot;unavailable&quot; means the snapshot could not confirm anything and is not the same as a model "
        "that the provider no longer lists; a retired or policy-blocked model is reported under its own state. "
        f"When an identity is refused, the page lists {_esc(SUGGEST_LABEL)}: {_esc(SUGGEST_BASIS)}. "
        "Pinned seats keep their identity until they are explicitly unpinned, and nothing here is applied "
        "automatically.</p>"
    )
    return (
        '<section class="panel" aria-labelledby="seats"><header><h2 id="seats">Seats</h2>'
        f'<p class="panel-count">{len(view.seats)} seat(s)</p></header><div class="table-wrap">'
        '<table class="roster-table"><thead><tr><th>Seat</th><th>Provider/model</th><th>Effort</th>'
        "<th>Inventory</th><th>Cost class</th><th>Enabled</th><th>Pinned</th><th>Training</th><th>Retention</th><th>Quota pool</th>"
        f"<th>Machines</th><th>Notes</th></tr></thead><tbody>{rows}</tbody></table></div>{legend}</section>"
    )


def _seat_form(view: PolicyView, row: SeatRow, *, csrf: str, draft: Submission | None) -> str:
    values = _draft_for(draft, "seat", row.name)

    def value(name: str, fallback: object) -> object:
        return values.get(name, "" if fallback is None else fallback)

    pin_note = (
        '<p class="hint">This seat is pinned. Changing provider, model, or effort takes two saves: '
        "clear the pin first, then change the identity.</p>"
        if row.pinned
        else ""
    )
    warn_note = (
        f'<p class="hint">Contributor terms enforced: {_esc(", ".join(row.warnings))}</p>' if row.warnings else ""
    )
    grid = "".join(
        [
            _labelled("Provider", _input("provider", value("provider", row.provider), editable=True), hint="provider"),
            _labelled("Exact model id", _input("model", value("model", row.model), editable=True), hint="model"),
            _labelled("Reasoning effort", _input("effort", value("effort", row.effort), editable=True), hint="effort"),
            _labelled(
                "Cost class",
                _enum_select(
                    "cost_class", value("cost_class", row.cost_class), fleet_policy.COST_CLASS_VALUES, editable=True
                ),
                hint="cost_class (paid, subscription, free, unknown)",
            ),
            _labelled(
                "Machines this seat may run on",
                _input("eligible_machines", value("eligible_machines", " ".join(row.eligible_machines)), editable=True),
                hint="eligible_machines",
            ),
            _labelled(
                "Maximum concurrent workers",
                _input("concurrency", value("concurrency", row.concurrency), editable=True),
                hint="concurrency",
            ),
            _labelled(
                "Worker timeout (seconds)",
                _input("timeout_seconds", value("timeout_seconds", row.timeout_seconds), editable=True),
                hint="timeout_seconds",
            ),
            _labelled(
                "Fallback seats",
                _input("fallback", value("fallback", " ".join(row.fallback)), editable=True),
                hint="fallback",
            ),
            _labelled(
                "Data retention",
                _input("retention", value("retention", row.retention), editable=True),
                hint="retention",
            ),
            _labelled(
                "Quota pool",
                _input("quota_pool", value("quota_pool", row.quota_pool), editable=True),
                hint="quota_pool (a label, not a price)",
            ),
            _labelled("Notes", _input("notes", value("notes", row.notes), editable=True), hint="notes"),
        ]
    )
    boxes = "".join(
        [
            _labelled(
                "Enabled",
                _checkbox("enabled", _flag(values, "enabled", row.enabled), editable=True),
                hint="enabled",
            ),
            _labelled(
                "Pinned (identity changes need two saves)",
                _checkbox("pinned", _flag(values, "pinned", row.pinned), editable=True),
                hint="pinned",
            ),
            _labelled(
                "Allow contributor training",
                _checkbox("training_allowed", _flag(values, "training_allowed", row.training_allowed), editable=True),
                hint="training_allowed",
            ),
        ]
    )
    bindings = values.get("bindings", json.dumps(row.bindings, indent=2, sort_keys=True))
    return (
        f'<details id="seat-{_esc(row.name)}"><summary>{_esc(row.name)} &middot; {_esc(row.provider)}/'
        f"{_esc(row.model)} &middot; inventory {_esc(row.inventory_state)}</summary>"
        f'{pin_note}{warn_note}<form method="post" action="/deck/policy" class="roster-form">'
        + _hidden("seat", row.name, view.revision, csrf)
        + f'<div class="roster-grid">{grid}</div><div class="roster-grid">{boxes}</div>'
        + _labelled(
            "bindings (exact JSON: brigade.cli, t3_fleet.instance_id/service_tier, native.instance_id/model)",
            f'<textarea name="field.bindings" rows="8">{_esc(bindings)}</textarea>',
        )
        + _actions(_reason_for(draft, "seat", row.name))
        + "</form></details>"
    )


def _flag(values: Mapping[str, str], name: str, fallback: bool) -> bool:
    if not values:
        return fallback
    return _bool_field(values.get(name))


def _consumer_section(view: PolicyView, *, editable: bool, csrf: str, draft: Submission | None) -> str:
    blocks: list[str] = []
    for row in view.consumers:
        values = _draft_for(draft, "consumer", row.name)
        role_lines_list = []
        for role in POLICY_ROLES:
            seat_name = row.roles.get(role)
            seat_obj = next((s for s in view.seats if s.name == seat_name), None)
            cost_info = f" &middot; cost {_esc(seat_obj.cost_class)}" if seat_obj else ""
            role_lines_list.append(
                f"<li>{_esc(ROLE_LABELS.get(role, role))} (<code>{_esc(role)}</code>): "
                f"{_text(seat_name)}{cost_info} &middot; "
                f"{_esc(_layer_text(row.sources.get('roles.' + role)))}</li>"
            )
        role_lines = "".join(role_lines_list)
        summary = (
            f"<summary>{_esc(row.name)} &middot; reload {_esc(row.reload)} &middot; "
            f"coverage {_esc(row.coverage)}</summary>"
        )
        facts = f'<ul class="attention-list">{role_lines}</ul>'
        if not editable:
            blocks.append(f'<details id="consumer-{_esc(row.name)}">{summary}{facts}</details>')
            continue
        role_cells = "".join(
            _labelled(
                f"{ROLE_LABELS.get(role, role)} patch",
                _seat_options(
                    f"role_{role}",
                    values.get(f"role_{role}", (row.default_patches.get("roles", {}) or {}).get(role) or ""),
                    view.seats,
                    editable=True,
                ),
                hint="policy role key: " + role,
            )
            for role in POLICY_ROLES
        )
        seat_bindings = values.get("seat_bindings", json.dumps(row.seat_bindings, indent=2, sort_keys=True))
        form = (
            '<form method="post" action="/deck/policy" class="roster-form">'
            + _hidden("consumer", row.name, view.revision, csrf)
            + '<div class="roster-grid">'
            + _labelled(
                "reload",
                _enum_select("reload", values.get("reload", row.reload), fleet_policy.RELOAD_VALUES, editable=True),
            )
            + _labelled(
                "coverage",
                _enum_select(
                    "coverage", values.get("coverage", row.coverage), fleet_policy.COVERAGE_VALUES, editable=True
                ),
            )
            + _labelled(
                "adapter_version",
                _input("adapter_version", values.get("adapter_version", row.adapter_version or ""), editable=True),
            )
            + _labelled("notes", _input("notes", values.get("notes", row.notes or ""), editable=True))
            + "</div>"
            + f'<div class="roster-grid">{role_cells}</div>'
            + _labelled(
                "seat bindings for this consumer (exact JSON, seat -> binding group)",
                f'<textarea name="field.seat_bindings" rows="8">{_esc(seat_bindings)}</textarea>',
            )
            + _actions(_reason_for(draft, "consumer", row.name))
            + "</form>"
        )
        blocks.append(f'<details id="consumer-{_esc(row.name)}">{summary}{facts}{form}</details>')
    return (
        '<section class="panel" aria-labelledby="consumers"><header><h2 id="consumers">Consumers</h2>'
        f'<p class="panel-count">{len(view.consumers)} consumer(s)</p></header>'
        + ("".join(blocks) or '<p class="empty">No consumers registered.</p>')
        + "</section>"
    )


_ANCHOR_SAFE = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")


def repository_anchor(identity: str) -> str:
    """Stable fragment id for one repository editor.

    The digest suffix keeps two identities that flatten to the same slug (say
    ``acme/tool`` and ``acme-tool``) from sharing an anchor, so a deep link
    always lands on the repository it names.
    """
    slug = "".join(char if char in _ANCHOR_SAFE else "-" for char in identity)[:80]
    return f"repo-{slug}-{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:8]}"


def _layer_text(source: Mapping[str, Any] | None) -> str:
    if not source or source.get("layer") is None:
        return "unset"
    layer = str(source["layer"])
    return layer if layer.startswith(("consumer:", "repo:", "constraint:")) else f"inherited from {layer}"


def _repository_section(view: PolicyView, *, editable: bool, csrf: str, draft: Submission | None) -> str:
    blocks: list[str] = []
    for row in view.repositories:
        values = _draft_for(draft, "repository", row.identity)
        flag = "" if row.in_policy else " &middot; <strong>not in policy (inherited, metadata unknown)</strong>"
        training = row.effective.get("data", {}).get("allow_training")
        allow_free = row.effective.get("data", {}).get("allow_free")
        denied = "".join(
            f"<li>denied {_esc(item['path'])} &middot; requested {_esc(item['requested'])} &middot; "
            f"{_esc(item['reason'])}</li>"
            for item in row.denied_overrides
        )
        leaves = "".join(
            f"<li><code>{_esc(path)}</code>: {_text(source['value'])} &middot; {_esc(_layer_text(source))}</li>"
            for path, source in sorted(row.sources.items())
        )
        facts = (
            f"<p>privacy {_esc(row.privacy)} &middot; owner {_text(row.owner)} &middot; "
            f"eligible machines {_text(', '.join(row.eligible_machines))} &middot; "
            f"allow_training {_text(training)} &middot; allow_free {_text(allow_free)}"
            f"{flag}</p>"
            + (f'<ul class="attention-list">{denied}</ul>' if denied else "")
            + f'<ul class="attention-list">{leaves}</ul>'
        )
        anchor = repository_anchor(row.identity)
        summary = f"<summary>{_esc(row.identity)} &middot; {_esc(row.privacy)}</summary>"
        if not editable:
            blocks.append(f'<details id="{_esc(anchor)}">{summary}{facts}</details>')
            continue
        patches = row.patches
        form = (
            '<form method="post" action="/deck/policy" class="roster-form">'
            + _hidden("repository", row.identity, view.revision, csrf)
            + '<div class="roster-grid">'
            + _labelled(
                "privacy",
                _enum_select("privacy", values.get("privacy", row.privacy), fleet_policy.PRIVACY_VALUES, editable=True),
            )
            + _labelled("owner", _input("owner", values.get("owner", row.owner or ""), editable=True))
            + _labelled(
                "eligible machines",
                _input(
                    "eligible_machines",
                    values.get("eligible_machines", " ".join(row.eligible_machines)),
                    editable=True,
                ),
            )
            + _labelled("notes", _input("notes", values.get("notes", row.notes or ""), editable=True))
            + '</div><div class="roster-grid">'
            + _labelled(
                "Override: allow contributor training",
                _enum_select(
                    "patch_data_allow_training",
                    values.get("patch_data_allow_training", _yes_no(patches.get("data", {}).get("allow_training"))),
                    ("yes", "no"),
                    editable=True,
                    blank="(inherit)",
                ),
                hint="policy key: patches.data.allow_training",
            )
            + _labelled(
                "Override: allow free models",
                _enum_select(
                    "patch_data_allow_free",
                    values.get("patch_data_allow_free", _yes_no(patches.get("data", {}).get("allow_free"))),
                    ("yes", "no"),
                    editable=True,
                    blank="(inherit)",
                ),
                hint="policy key: patches.data.allow_free",
            )
            + _labelled(
                "Override: data retention",
                _input(
                    "patch_data_retention",
                    values.get("patch_data_retention", patches.get("data", {}).get("retention") or ""),
                    editable=True,
                ),
                hint="policy key: patches.data.retention",
            )
            + _labelled(
                "Override: maximum concurrent workers",
                _input(
                    "patch_execution_concurrency",
                    values.get(
                        "patch_execution_concurrency",
                        _blank(patches.get("execution", {}).get("concurrency")),
                    ),
                    editable=True,
                ),
                hint="policy key: patches.execution.concurrency",
            )
            + _labelled(
                "Override: machine",
                _input(
                    "patch_execution_machine",
                    values.get("patch_execution_machine", patches.get("execution", {}).get("machine") or ""),
                    editable=True,
                ),
                hint="policy key: patches.execution.machine",
            )
            + "</div>"
            '<p class="hint">Only the leaves below are written for this repository. A blank leaf inherits; it is '
            "not a null. Hard constraints such as the training denial for a private repository stay with the "
            "resolver and cannot be overridden here.</p>"
            + _actions(_reason_for(draft, "repository", row.identity))
            + "</form>"
        )
        blocks.append(f'<details id="{_esc(anchor)}">{summary}{facts}{form}</details>')
    return (
        '<section class="panel" aria-labelledby="repos"><header><h2 id="repos">Repositories</h2>'
        f'<p class="panel-count">{len(view.repositories)} repository/repositories</p></header>'
        '<p class="hint">This is the authoritative sparse editor: only the leaves written here belong to a '
        "repository. The operational view of the same repositories - claims, live runs, sessions - is on the "
        '<a href="/deck/repos">repos page</a>.</p>'
        + ("".join(blocks) or '<p class="empty">No repositories in policy.</p>')
        + "</section>"
    )


def _yes_no(value: object) -> str:
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return ""


def _blank(value: object) -> str:
    return "" if value is None else str(value)


def _acknowledged_cell(value: object) -> str:
    """A persisted acknowledgement field, or an explicit unknown. Never derived."""
    text = "" if value is None else str(value)
    return _esc(text) if text else f'<span class="unknown">{UNKNOWN_TEXT}</span>'


def _effective_settings_cell(row: SessionRow) -> str:
    if (
        row.effective is None
        or (row.loaded_snapshot and row.loaded_snapshot.get("status") == "unknown")
        or row.context_hash is None
    ):
        return f'<span class="unknown">{UNKNOWN_TEXT}</span>'

    leaves = []
    sources = row.sources or {}
    for section_name, section_body in sorted(row.effective.items()):
        if isinstance(section_body, Mapping):
            for k, v in sorted(section_body.items()):
                path = f"{section_name}.{k}"
                src = sources.get(path)
                layer = _layer_text(src) if isinstance(src, Mapping) else (str(src) if src else "unknown")
                leaves.append(f"<li><code>{_esc(path)}</code>: {_esc(str(v))} &middot; {_esc(layer)}</li>")
        else:
            path = section_name
            src = sources.get(path)
            layer = _layer_text(src) if isinstance(src, Mapping) else (str(src) if src else "unknown")
            leaves.append(f"<li><code>{_esc(path)}</code>: {_esc(str(section_body))} &middot; {_esc(layer)}</li>")

    if row.overrides:
        overrides_desc = f"overrides: {_esc(json.dumps(row.overrides, sort_keys=True))}"
        if row.override_reason:
            overrides_desc += f" ({_esc(row.override_reason)})"
        leaves.append(f"<li><em>{overrides_desc}</em></li>")

    leaf_html = f'<ul class="attention-list">{"".join(leaves)}</ul>'
    hash_txt = f"<div><code>hash {_esc(row.context_hash)}</code></div>"
    return f"{hash_txt}<details><summary>acknowledged settings ({len(leaves)})</summary>{leaf_html}</details>"


def _pending_cell(row: SessionRow) -> str:
    if row.pending_snapshot is None:
        return f'<span class="unknown">{UNKNOWN_TEXT}</span>'
    p_rev = row.pending_snapshot.get("revision")
    p_hash = row.pending_snapshot.get("context_hash")
    p_status = "pending apply required" if row.pending_apply_required else "pending prepared"
    hash_str = f"<div><code>{_esc(p_hash or UNKNOWN_TEXT)}</code></div>"
    return f"<div><strong>rev {_text(p_rev)}</strong> ({_esc(p_status)})</div>{hash_str}"


def _session_id_cell(row: SessionRow) -> str:
    parts = [_esc(row.session_id)]
    if row.external_session_id and row.external_session_id != row.session_id:
        parts.append(f"<br><small>ext: {_esc(row.external_session_id)}</small>")
    if row.receipt_key and row.receipt_key != row.session_id and row.receipt_key != row.external_session_id:
        parts.append(f"<br><small>key: {_esc(row.receipt_key)}</small>")
    return "".join(parts)


def _origin_cell(row: SessionRow) -> str:
    """Render origin, or UNKNOWN. Source is a separate fact and is never origin."""
    origin_html = _acknowledged_cell(row.origin)
    if row.source:
        return f"{origin_html} <small>(src: {_esc(row.source)})</small>"
    return origin_html


def _sessions_section(view: PolicyView) -> str:
    """Two separated groups of facts: acknowledged snapshot vs current policy & pending refresh."""
    rows = "".join(
        f"<tr><td>{_session_id_cell(row)}</td><td>{_esc(row.consumer)}</td><td>{_text(row.repository)}</td>"
        f"<td>{_acknowledged_cell(row.owner_node)}</td><td>{_origin_cell(row)}</td>"
        f"<td>{_acknowledged_cell(row.loaded_at)}</td><td>{_acknowledged_cell(row.revision)}</td>"
        f"<td>{_acknowledged_cell(row.digest)}</td>"
        f"<td>{_effective_settings_cell(row)}</td>"
        f"<td>{_acknowledged_cell(row.current_revision)}</td>"
        f"<td>{_esc(row.state)}</td><td>{_pending_cell(row)}</td><td>{_text(row.reload)}</td>"
        f"<td>{_esc(row.refresh_state)}</td><td>{_acknowledged_cell(row.refresh_requested_at)}</td></tr>"
        for row in view.sessions
    )
    return (
        '<section class="panel" aria-labelledby="sessions"><header><h2 id="sessions">Sessions</h2>'
        f'<p class="panel-count">{len(view.sessions)} session(s)</p></header>'
        + (
            '<div class="table-wrap"><table class="roster-table"><thead>'
            '<tr><th colspan="9">Acknowledged by the session</th>'
            '<th colspan="6">Current policy and pending refresh</th></tr>'
            "<tr><th>Session</th><th>Consumer</th><th>Repository</th><th>Owner node</th><th>Origin</th>"
            "<th>Loaded at</th><th>Revision</th><th>Digest</th>"
            "<th>Loaded effective settings</th>"
            "<th>Current revision</th><th>State now</th><th>Pending snapshot</th><th>Reload</th><th>Refresh</th><th>Refresh requested</th>"
            f"</tr></thead><tbody>{rows}</tbody></table></div>"
            if rows
            else '<p class="empty">No session receipts.</p>'
        )
        + f'<p class="hint">{_esc(SNAPSHOT_CONTRACT_UNAVAILABLE)}.</p>'
        + "</section>"
    )


def _history_section(view: PolicyView, *, editable: bool, csrf: str) -> str:
    rows = "".join(
        f"<tr><td>{row['revision']}</td><td>{_text(row['created_at'])}</td><td>{_text(row['actor'])}</td>"
        f"<td>{_text(row['reason'])}</td><td>{_text(row['digest'])}</td></tr>"
        for row in view.revisions
    )
    options = "".join(
        f'<option value="{row["revision"]}">{row["revision"]} &middot; {_esc(row["reason"] or "")}</option>'
        for row in view.revisions
        if int(row["revision"]) != view.revision
    )
    form = ""
    if editable and options:
        form = (
            '<form method="post" action="/deck/policy" class="roster-form">'
            + _hidden("rollback", "", view.revision, csrf)
            + _labelled("roll back to revision", f'<select name="field.to_revision">{options}</select>')
            + _actions("rollback")
            + "</form>"
        )
    return (
        '<section class="panel" aria-labelledby="history"><header><h2 id="history">Audit history</h2>'
        f'<p class="panel-count">{len(view.revisions)} revision(s)</p></header>'
        '<div class="table-wrap"><table class="roster-table"><thead><tr><th>Revision</th><th>Created</th>'
        f"<th>Actor</th><th>Reason</th><th>Digest</th></tr></thead><tbody>{rows}</tbody></table></div>{form}</section>"
    )


def render(
    view: PolicyView,
    *,
    nonce: str,
    now: datetime,
    csrf: str,
    editable: bool,
    banner: str | None = None,
    error: str | None = None,
    plan: Plan | None = None,
    draft: Submission | None = None,
    review: str | None = None,
) -> str:
    """The policy page. ``draft`` re-renders what the operator sent on a refusal.

    ``review`` is the server-minted token for the previewed document; it is
    what turns the proposed-change panel into a confirm-save control.
    """
    parts: list[str] = ['<main class="deck-shell">']
    parts.append(
        '<header class="masthead"><div><p class="eyebrow">Fleet operations</p>'
        "<h1>Command Deck &middot; Policy</h1>"
        f'<p class="station-meta">revision {view.revision}, saved {_esc(view.created_at)} by '
        f"{_esc(view.actor or 'unknown')}</p></div>"
        f'<p class="header-meta">{_esc(fleet_command_deck._stamp(now))}</p></header>'
    )
    parts.append(
        '<nav aria-label="Command Deck"><a href="/">deck</a> <a href="/deck/repos">repos</a> '
        '<a href="/deck/roster">roster</a> <a href="/deck/policy">policy</a> '
        '<a href="/view/machines">machines board</a></nav>'
    )
    if banner:
        parts.append(f'<p class="banner">{_esc(banner)}</p>')
    if error:
        parts.append(f'<p class="banner banner--error">{_esc(error)}</p>')
    if not editable:
        parts.append('<p class="banner">read-only: enroll with the fleet token to edit</p>')
    parts.append(_identity_panel(view))
    parts.append(_activation_panel(view))
    parts.append(_plan_panel(plan, activation=view.activation, draft=draft, review=review if editable else None))
    parts.append(_defaults_form(view, editable=editable, csrf=csrf, draft=draft))
    parts.append(_seat_table(view))
    if editable:
        seat_forms = "".join(_seat_form(view, row, csrf=csrf, draft=draft) for row in view.seats)
        parts.append(
            '<section class="panel" aria-labelledby="seat-edit"><header><h2 id="seat-edit">Edit a seat</h2>'
            f'<p class="panel-count">{len(view.seats)} seat(s)</p></header>{seat_forms}</section>'
        )
    parts.append(_consumer_section(view, editable=editable, csrf=csrf, draft=draft))
    parts.append(_repository_section(view, editable=editable, csrf=csrf, draft=draft))
    parts.append(_sessions_section(view))
    parts.append(_history_section(view, editable=editable, csrf=csrf))
    parts.append("</main>")
    return fleet_command_deck._document("\n".join(parts), nonce=nonce, now=now, title="Policy", refresh=False)
