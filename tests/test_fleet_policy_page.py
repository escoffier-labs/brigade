"""Hub-served policy page (``/deck/policy``): projection, scoped edits, preview, save.

Every test drives the page module directly against a hub connection, the way
``test_fleet_roster_page`` drives the roster page. Inventory and activation are
injected: the page must never probe a live provider or guess activation.
"""

from __future__ import annotations

import hashlib
import html
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import pytest

from brigade import fleet_hub, fleet_hub_policy, fleet_policy, fleet_policy_page


NOW = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)
NODE_A = "11111111-1111-4111-8111-111111111111"


@pytest.fixture()
def conn(tmp_path):
    connection = fleet_hub.init_db(tmp_path / "fleet.db")
    try:
        yield connection
    finally:
        connection.close()


def _document() -> dict:
    return {
        "schema": fleet_policy.POLICY_SCHEMA,
        "defaults": {
            "roles": {"impl": "seat-alpha", "review": "seat-beta"},
            "data": {"allow_training": False, "retention": "30d"},
            "execution": {"concurrency": 2},
        },
        "machines": {"worker-linux-1": {"os": "linux", "concurrency": 2}},
        "seats": {
            "seat-alpha": {
                "provider": "provider-a",
                "model": "model-a-1",
                "effort": "high",
                "eligible_machines": ["worker-linux-1"],
                "bindings": {"brigade": {"cli": "cli-a"}},
            },
            "seat-beta": {"provider": "provider-b", "model": "model-b-1"},
            "seat-pinned": {"provider": "provider-a", "model": "model-a-2", "pinned": True},
        },
        "consumers": {
            "brigade-run": {"reload": "refreshable"},
            "t3-fleet": {"reload": "restart-required"},
        },
        "repositories": {
            "acme/public-tool": {"privacy": "public"},
            "acme/private-tool": {"privacy": "private"},
        },
    }


def _seed(conn, document=None, *, actor="operator", reason="seed") -> int:
    saved = fleet_hub_policy.save_policy(
        conn,
        document if document is not None else _document(),
        expected_version=fleet_hub_policy.current_policy(conn)["revision"],
        actor=actor,
        reason=reason,
    )
    return int(saved["revision"])


def _fresh_inventory(**overrides) -> fleet_policy_page.Inventory:
    providers = {
        "provider-a": {
            "state": "fresh",
            "available": ["model-a-1", "model-a-2", "model-a-3"],
            "retired": [],
            "blocked": [],
        },
        "provider-b": {
            "state": "fresh",
            "available": ["model-b-1"],
            "retired": ["model-b-0"],
            "blocked": ["model-b-x"],
        },
    }
    providers.update(overrides)
    return fleet_policy_page.Inventory(providers=providers, source="test-injected", captured_at="2026-09-05T11:00:00Z")


def _submission(scope: str, action: str, revision: int, **fields) -> fleet_policy_page.Submission:
    body = {
        "scope": scope,
        "action": action,
        "expected_version": str(revision),
        "csrf": "token",
        "reason": fields.pop("reason", "test change"),
        "target": fields.pop("target", ""),
    }
    for key, value in fields.items():
        body[f"field.{key}"] = value
    return fleet_policy_page.parse_form(urlencode(body).encode("utf-8"))


REVIEW_KEY = fleet_policy_page.review_secret("test-review-key")


def _apply(conn, submission, *, actor="operator", inventory, activation):
    """Drive the reviewed-preview handshake a save now requires.

    A save is refused unless it carries the token the server minted for the
    exact document it previewed, so these tests take the same two steps a
    browser does: preview, then confirm the previewed document.
    """
    call = dict(actor=actor, inventory=inventory, activation=activation, review_key=REVIEW_KEY)
    if submission.action != "save":
        return fleet_policy_page.apply(conn, submission, **call)
    previewed = fleet_policy_page.apply(conn, replace(submission, action="preview"), **call)
    if previewed.status != "previewed":
        return previewed
    return fleet_policy_page.apply(conn, replace(submission, review=previewed.review or ""), **call)


def _render(view, **kwargs):
    return fleet_policy_page.render(
        view,
        nonce="test-nonce",
        now=NOW,
        csrf="csrf-token",
        editable=kwargs.pop("editable", True),
        **kwargs,
    )


# --- projection --------------------------------------------------------------


def test_view_reports_the_full_effective_policy_identity(conn):
    revision = _seed(conn)
    view = fleet_policy_page.load_view(conn, inventory=_fresh_inventory())
    assert view.revision == revision
    assert view.digest == fleet_hub_policy.current_policy(conn)["digest"]
    assert view.actor == "operator"
    assert view.created_at
    assert view.parent_revision == revision - 1
    page = _render(view)
    assert f"revision {revision}" in page
    assert view.digest in page
    # The revision's own timestamp is when it was saved. "Loaded at" is a
    # per-session fact and belongs to the session table, not to this panel.
    identity_panel = page.split('id="identity"', 1)[1].split("</section>", 1)[0]
    assert "saved at" in identity_panel
    assert "loaded" not in identity_panel.lower()


def test_view_exposes_per_leaf_provenance_for_inherited_and_overridden_values(conn):
    document = _document()
    document["repositories"]["acme/public-tool"]["patches"] = {"execution": {"concurrency": 8}}
    _seed(conn, document)
    view = fleet_policy_page.load_view(conn, inventory=_fresh_inventory())
    row = next(item for item in view.repositories if item.identity == "acme/public-tool")
    assert row.effective["execution"]["concurrency"] == 8
    assert row.sources["execution.concurrency"]["layer"] == "repo:acme/public-tool"
    assert row.sources["data.retention"]["layer"] == "fleet-defaults"
    page = _render(view)
    assert "inherited" in page.lower()


def test_repository_without_a_policy_record_is_inherited_not_dropped(conn):
    _seed(conn)
    view = fleet_policy_page.load_view(
        conn, inventory=_fresh_inventory(), known_repositories=("acme/public-tool", "acme/unlisted-tool")
    )
    identities = [row.identity for row in view.repositories]
    assert "acme/unlisted-tool" in identities
    row = next(item for item in view.repositories if item.identity == "acme/unlisted-tool")
    assert row.in_policy is False
    assert row.privacy == "unknown"
    page = _render(view)
    assert "acme/unlisted-tool" in page
    assert "not in policy" in page.lower()


def test_private_repository_shows_the_training_denial_from_policy_not_a_hardcoded_rule(conn):
    document = _document()
    document["defaults"]["data"]["allow_training"] = True
    _seed(conn, document)
    view = fleet_policy_page.load_view(conn, inventory=_fresh_inventory())
    private = next(item for item in view.repositories if item.identity == "acme/private-tool")
    public = next(item for item in view.repositories if item.identity == "acme/public-tool")
    assert private.effective["data"]["allow_training"] is False
    assert private.denied_overrides and private.denied_overrides[0]["reason"] == "private-repository"
    assert public.effective["data"]["allow_training"] is True
    page = _render(view)
    assert "private-repository" in page


def test_free_model_eligibility_is_reported_as_not_configured(conn):
    _seed(conn)
    view = fleet_policy_page.load_view(conn, inventory=_fresh_inventory())
    page = _render(view)
    assert "free-model" in page.lower()
    assert "allow_free" in page
    assert "quota pools and cost classes stay separate" in page.lower()


def test_retention_and_training_come_from_policy_fields(conn):
    _seed(conn)
    view = fleet_policy_page.load_view(conn, inventory=_fresh_inventory())
    page = _render(view)
    assert "30d" in page
    assert "allow_training" in page


# --- inventory ---------------------------------------------------------------


def test_inventory_distinguishes_available_missing_retired_and_blocked(conn):
    document = _document()
    document["seats"]["seat-gone"] = {"provider": "provider-a", "model": "model-a-ghost"}
    document["seats"]["seat-retired"] = {"provider": "provider-b", "model": "model-b-0"}
    document["seats"]["seat-blocked"] = {"provider": "provider-b", "model": "model-b-x"}
    _seed(conn, document)
    view = fleet_policy_page.load_view(conn, inventory=_fresh_inventory())
    states = {row.name: row.inventory_state for row in view.seats}
    assert states["seat-alpha"] == "available"
    assert states["seat-gone"] == "missing"
    assert states["seat-retired"] == "retired"
    assert states["seat-blocked"] == "policy-blocked"


def test_unavailable_inventory_is_not_reported_as_missing(conn):
    _seed(conn)
    view = fleet_policy_page.load_view(conn, inventory=fleet_policy_page.unavailable_inventory("provider login failed"))
    states = {row.name: row.inventory_state for row in view.seats}
    assert set(states.values()) == {"unavailable"}
    page = _render(view)
    assert "unavailable" in page.lower()
    # No seat may be reported as missing when the snapshot could not confirm
    # anything: the seat table is where a state is claimed per seat.
    seat_table = page.split('id="seats"', 1)[1].split("</table>", 1)[0]
    assert "missing" not in seat_table.lower()
    assert seat_table.lower().count("unavailable") == len(view.seats)


def test_default_inventory_is_unavailable_never_a_live_probe(conn):
    _seed(conn)
    view = fleet_policy_page.load_view(conn)
    assert view.inventory.source == "unavailable"
    assert all(row.inventory_state == "unavailable" for row in view.seats)


def test_identity_change_is_blocked_when_inventory_is_unavailable(conn):
    revision = _seed(conn)
    submission = _submission(
        "seat",
        "save",
        revision,
        target="seat-alpha",
        provider="provider-a",
        model="model-a-3",
        effort="high",
        concurrency="1",
        enabled="1",
    )
    result = _apply(
        conn,
        submission,
        actor="operator",
        inventory=fleet_policy_page.unavailable_inventory("provider login failed"),
        activation=fleet_policy_page.UNKNOWN_ACTIVATION,
    )
    assert result.status == "blocked"
    codes = {error["code"] for error in result.plan.errors}
    assert "inventory_unavailable" in codes
    assert fleet_hub_policy.current_policy(conn)["revision"] == revision


def test_identity_change_to_a_missing_model_is_blocked_with_suggestions_only(conn):
    revision = _seed(conn)
    submission = _submission(
        "seat",
        "save",
        revision,
        target="seat-alpha",
        provider="provider-a",
        model="model-a-9",
        concurrency="1",
        enabled="1",
    )
    result = _apply(
        conn,
        submission,
        actor="operator",
        inventory=_fresh_inventory(),
        activation=fleet_policy_page.UNKNOWN_ACTIVATION,
    )
    assert result.status == "blocked"
    error = next(item for item in result.plan.errors if item["code"] == "inventory_missing")
    assert error["seat"] == "seat-alpha"
    assert error["suggestions"]
    assert "model-a-1" in error["suggestions"]
    assert fleet_hub_policy.current_policy(conn)["revision"] == revision


def test_unavailable_inventory_does_not_freeze_edits_to_unchanged_seats(conn):
    revision = _seed(conn)
    submission = _submission(
        "seat",
        "save",
        revision,
        target="seat-alpha",
        provider="provider-a",
        model="model-a-1",
        effort="high",
        concurrency="4",
        timeout_seconds="900",
        notes="raised concurrency",
        enabled="1",
    )
    result = _apply(
        conn,
        submission,
        actor="operator",
        inventory=fleet_policy_page.unavailable_inventory("provider login failed"),
        activation=fleet_policy_page.UNKNOWN_ACTIVATION,
    )
    assert result.status == "saved", result.message
    saved = fleet_hub_policy.current_policy(conn)
    assert saved["revision"] == revision + 1
    assert saved["document"]["seats"]["seat-alpha"]["concurrency"] == 4
    assert saved["document"]["seats"]["seat-alpha"]["model"] == "model-a-1"


def test_form_submitted_inventory_claims_are_ignored(conn):
    revision = _seed(conn)
    body = {
        "scope": "seat",
        "action": "save",
        "expected_version": str(revision),
        "csrf": "token",
        "reason": "spoof",
        "target": "seat-alpha",
        "field.provider": "provider-a",
        "field.model": "model-a-9",
        "field.concurrency": "1",
        "field.enabled": "1",
        "inventory.provider-a.available": "model-a-9",
        "field.inventory_state": "available",
    }
    submission = fleet_policy_page.parse_form(urlencode(body).encode("utf-8"))
    result = _apply(
        conn,
        submission,
        actor="operator",
        inventory=_fresh_inventory(),
        activation=fleet_policy_page.UNKNOWN_ACTIVATION,
    )
    assert result.status == "blocked"
    assert {error["code"] for error in result.plan.errors} == {"inventory_missing"}


# --- scoped edits ------------------------------------------------------------


def test_seat_edit_touches_only_that_seat(conn):
    revision = _seed(conn)
    before = fleet_hub_policy.current_policy(conn)["document"]
    submission = _submission(
        "seat",
        "save",
        revision,
        target="seat-alpha",
        provider="provider-a",
        model="model-a-1",
        effort="high",
        eligible_machines="worker-linux-1",
        concurrency="4",
        quota_pool="pool-one",
        retention="7d",
        enabled="1",
        bindings='{"brigade": {"cli": "cli-a"}}',
    )
    result = _apply(
        conn,
        submission,
        actor="operator",
        inventory=_fresh_inventory(),
        activation=fleet_policy_page.UNKNOWN_ACTIVATION,
    )
    assert result.status == "saved", result.message
    after = fleet_hub_policy.current_policy(conn)["document"]
    assert after["seats"]["seat-beta"] == before["seats"]["seat-beta"]
    assert after["consumers"] == before["consumers"]
    assert after["repositories"] == before["repositories"]
    assert after["defaults"] == before["defaults"]
    assert after["seats"]["seat-alpha"]["quota_pool"] == "pool-one"
    assert after["seats"]["seat-alpha"]["retention"] == "7d"


def test_every_core_seat_field_is_editable(conn):
    revision = _seed(conn)
    submission = _submission(
        "seat",
        "save",
        revision,
        target="seat-beta",
        provider="provider-b",
        model="model-b-1",
        effort="low",
        eligible_machines="worker-linux-1",
        concurrency="3",
        timeout_seconds="1200",
        fallback="seat-alpha",
        training_allowed="1",
        retention="14d",
        quota_pool="pool-two",
        enabled="1",
        pinned="1",
        notes="beta seat",
        bindings='{"t3_fleet": {"instance_id": "inst-1", "service_tier": "tier-a"}}',
    )
    result = _apply(
        conn,
        submission,
        actor="operator",
        inventory=_fresh_inventory(),
        activation=fleet_policy_page.UNKNOWN_ACTIVATION,
    )
    assert result.status == "saved", result.message
    seat = fleet_hub_policy.current_policy(conn)["document"]["seats"]["seat-beta"]
    assert seat["effort"] == "low"
    assert seat["eligible_machines"] == ["worker-linux-1"]
    assert seat["concurrency"] == 3
    assert seat["timeout_seconds"] == 1200
    assert seat["fallback"] == ["seat-alpha"]
    assert seat["training_allowed"] is True
    assert seat["retention"] == "14d"
    assert seat["quota_pool"] == "pool-two"
    assert seat["pinned"] is True
    assert seat["notes"] == "beta seat"
    assert seat["bindings"]["t3_fleet"] == {"instance_id": "inst-1", "service_tier": "tier-a"}


def test_repository_edit_stays_sparse(conn):
    revision = _seed(conn)
    submission = _submission(
        "repository",
        "save",
        revision,
        target="acme/public-tool",
        privacy="public",
        owner="team-tools",
        patch_execution_concurrency="6",
    )
    result = _apply(
        conn,
        submission,
        actor="operator",
        inventory=_fresh_inventory(),
        activation=fleet_policy_page.UNKNOWN_ACTIVATION,
    )
    assert result.status == "saved", result.message
    record = fleet_hub_policy.current_policy(conn)["document"]["repositories"]["acme/public-tool"]
    assert record["patches"] == {"execution": {"concurrency": 6}}
    assert record["owner"] == "team-tools"
    assert "seats" not in record
    assert "defaults" not in record


def test_blank_patch_leaf_means_inherit_not_null(conn):
    document = _document()
    document["repositories"]["acme/public-tool"]["patches"] = {"execution": {"concurrency": 8}}
    revision = _seed(conn, document)
    submission = _submission(
        "repository",
        "save",
        revision,
        target="acme/public-tool",
        privacy="public",
        patch_execution_concurrency="",
    )
    result = _apply(
        conn,
        submission,
        actor="operator",
        inventory=_fresh_inventory(),
        activation=fleet_policy_page.UNKNOWN_ACTIVATION,
    )
    assert result.status == "saved", result.message
    record = fleet_hub_policy.current_policy(conn)["document"]["repositories"]["acme/public-tool"]
    assert record["patches"].get("execution", {}) == {}
    resolved = fleet_policy.resolve_policy(fleet_hub_policy.current_policy(conn)["document"], None, "acme/public-tool")
    assert resolved["effective"]["execution"]["concurrency"] == 2


def test_consumer_role_patch_is_scoped_to_that_consumer(conn):
    revision = _seed(conn)
    submission = _submission(
        "consumer",
        "save",
        revision,
        target="t3-fleet",
        reload="restart-required",
        coverage="verified",
        role_impl="seat-beta",
    )
    result = _apply(
        conn,
        submission,
        actor="operator",
        inventory=_fresh_inventory(),
        activation=fleet_policy_page.UNKNOWN_ACTIVATION,
    )
    assert result.status == "saved", result.message
    document = fleet_hub_policy.current_policy(conn)["document"]
    assert document["consumers"]["t3-fleet"]["default_patches"]["roles"]["impl"] == "seat-beta"
    assert document["defaults"]["roles"]["impl"] == "seat-alpha"
    assert document["consumers"]["brigade-run"]["default_patches"] == {}


def test_consumer_seat_bindings_are_editable(conn):
    revision = _seed(conn)
    submission = _submission(
        "consumer",
        "save",
        revision,
        target="brigade-run",
        reload="refreshable",
        seat_bindings='{"seat-alpha": {"brigade": {"cli": "cli-override"}}}',
    )
    result = _apply(
        conn,
        submission,
        actor="operator",
        inventory=_fresh_inventory(),
        activation=fleet_policy_page.UNKNOWN_ACTIVATION,
    )
    assert result.status == "saved", result.message
    document = fleet_hub_policy.current_policy(conn)["document"]
    bindings = fleet_policy.effective_seat_bindings(document, "brigade-run", "seat-alpha")
    assert bindings["brigade"]["cli"] == "cli-override"
    assert fleet_policy.effective_seat_bindings(document, "t3-fleet", "seat-alpha")["brigade"]["cli"] == "cli-a"


def test_admission_default_role_is_distinct_from_impl(conn):
    revision = _seed(conn)
    submission = _submission(
        "defaults",
        "save",
        revision,
        role_admission_default="seat-beta",
        role_impl="seat-alpha",
    )
    result = _apply(
        conn,
        submission,
        actor="operator",
        inventory=_fresh_inventory(),
        activation=fleet_policy_page.UNKNOWN_ACTIVATION,
    )
    assert result.status == "saved", result.message
    roles = fleet_hub_policy.current_policy(conn)["document"]["defaults"]["roles"]
    assert roles["admission_default"] == "seat-beta"
    assert roles["impl"] == "seat-alpha"


def test_runtime_role_keys_and_friendly_labels_both_render(conn):
    _seed(conn)
    view = fleet_policy_page.load_view(conn, inventory=_fresh_inventory())
    page = _render(view)
    for key in ("impl", "review", "research", "scout", "security", "chef"):
        assert f"role.{key}" in page or f'value="{key}"' in page or f">{key}<" in page
    assert "Worker" in page
    assert "Orchestrator" in page
    assert "Security reviewer" in page
    assert "reviewer</" not in page.replace("Security reviewer</", "")


# --- preview, CAS, pins ------------------------------------------------------


def test_preview_shows_the_precise_diff_and_affected_scope_without_writing(conn):
    revision = _seed(conn)
    submission = _submission(
        "defaults",
        "preview",
        revision,
        role_impl="seat-beta",
    )
    result = _apply(
        conn,
        submission,
        actor="operator",
        inventory=_fresh_inventory(),
        activation=fleet_policy_page.UNKNOWN_ACTIVATION,
    )
    assert result.status == "previewed"
    assert fleet_hub_policy.current_policy(conn)["revision"] == revision
    changed = {item["path"] for item in result.plan.diff["changed"]}
    assert changed == {"defaults.roles.impl"}
    assert result.plan.affected["consumers"] == ["brigade-run", "t3-fleet"]
    page = _render(fleet_policy_page.load_view(conn, inventory=_fresh_inventory()), plan=result.plan, draft=submission)
    assert "defaults.roles.impl" in page
    assert "seat-beta" in page


def test_preview_reports_session_application_and_refresh_state(conn):
    revision = _seed(conn)
    fleet_hub_policy.record_session_policy(
        conn,
        node_id=NODE_A,
        session_id="session-one",
        consumer="brigade-run",
        repo_identity="acme/public-tool",
        revision=revision,
        digest=fleet_hub_policy.current_policy(conn)["digest"],
        source="hub",
        loaded_at="2026-09-05T11:30:00Z",
    )
    fleet_hub_policy.record_session_policy(
        conn,
        node_id=NODE_A,
        session_id="session-two",
        consumer="t3-fleet",
        repo_identity="acme/public-tool",
        revision=revision,
        digest=fleet_hub_policy.current_policy(conn)["digest"],
        source="hub",
        loaded_at="2026-09-05T11:30:00Z",
    )
    submission = _submission("defaults", "preview", revision, role_impl="seat-beta")
    result = _apply(
        conn,
        submission,
        actor="operator",
        inventory=_fresh_inventory(),
        activation=fleet_policy_page.UNKNOWN_ACTIVATION,
    )
    impacts = {item["session_id"]: item for item in result.plan.session_impact}
    assert impacts["session-one"]["application"] == "refresh"
    assert impacts["session-two"]["application"] == "restart-required"
    view = fleet_policy_page.load_view(conn, inventory=_fresh_inventory())
    page = _render(view, plan=result.plan, draft=submission)
    assert "session-one" in page
    assert "restart-required" in page
    # Under an unknown authority a save reaches no session, future or running,
    # so the panel must not promise that it applies on the next load.
    assert "Activation status is unknown" in page
    assert "once the authority is activated" in page


def test_stale_sessions_are_projected_after_a_save(conn):
    revision = _seed(conn)
    fleet_hub_policy.record_session_policy(
        conn,
        node_id=NODE_A,
        session_id="session-one",
        consumer="brigade-run",
        repo_identity="acme/public-tool",
        revision=revision,
        digest=fleet_hub_policy.current_policy(conn)["digest"],
        source="hub",
        loaded_at="2026-09-05T11:30:00Z",
    )
    _seed(conn, _document(), reason="second")
    view = fleet_policy_page.load_view(conn, inventory=_fresh_inventory())
    row = next(item for item in view.sessions if item.session_id == "session-one")
    assert row.state in ("stale", "refreshable")
    page = _render(view)
    assert "session-one" in page


def test_stale_expected_version_is_a_conflict_that_keeps_the_draft(conn):
    revision = _seed(conn)
    _seed(conn, _document(), reason="someone else")
    submission = _submission("defaults", "save", revision, role_impl="seat-beta")
    result = _apply(
        conn,
        submission,
        actor="operator",
        inventory=_fresh_inventory(),
        activation=fleet_policy_page.UNKNOWN_ACTIVATION,
    )
    assert result.status == "conflict"
    assert "policy_revision_conflict" in {error["code"] for error in result.plan.errors}
    page = _render(
        fleet_policy_page.load_view(conn, inventory=_fresh_inventory()),
        error=result.message,
        draft=submission,
    )
    assert "seat-beta" in page
    assert "reload" in page.lower()


def test_pinned_seat_identity_change_is_refused_and_unpin_is_a_separate_save(conn):
    revision = _seed(conn)
    change = _submission(
        "seat",
        "save",
        revision,
        target="seat-pinned",
        provider="provider-a",
        model="model-a-3",
        concurrency="1",
        enabled="1",
        pinned="1",
    )
    refused = _apply(
        conn,
        change,
        actor="operator",
        inventory=_fresh_inventory(),
        activation=fleet_policy_page.UNKNOWN_ACTIVATION,
    )
    assert refused.status == "blocked"
    assert "pinned-seat-change" in {error["code"] for error in refused.plan.errors}

    unpin = _submission(
        "seat",
        "save",
        revision,
        target="seat-pinned",
        provider="provider-a",
        model="model-a-2",
        concurrency="1",
        enabled="1",
    )
    unpinned = _apply(
        conn,
        unpin,
        actor="operator",
        inventory=_fresh_inventory(),
        activation=fleet_policy_page.UNKNOWN_ACTIVATION,
    )
    assert unpinned.status == "saved", unpinned.message
    assert fleet_hub_policy.current_policy(conn)["document"]["seats"]["seat-pinned"]["pinned"] is False

    now_change = _submission(
        "seat",
        "save",
        unpinned.revision,
        target="seat-pinned",
        provider="provider-a",
        model="model-a-3",
        concurrency="1",
        enabled="1",
    )
    moved = _apply(
        conn,
        now_change,
        actor="operator",
        inventory=_fresh_inventory(),
        activation=fleet_policy_page.UNKNOWN_ACTIVATION,
    )
    assert moved.status == "saved", moved.message
    assert fleet_hub_policy.current_policy(conn)["document"]["seats"]["seat-pinned"]["model"] == "model-a-3"


def test_unrelated_edit_does_not_disturb_a_pinned_seat(conn):
    revision = _seed(conn)
    submission = _submission(
        "seat",
        "save",
        revision,
        target="seat-beta",
        provider="provider-b",
        model="model-b-1",
        concurrency="2",
        enabled="1",
    )
    result = _apply(
        conn,
        submission,
        actor="operator",
        inventory=_fresh_inventory(),
        activation=fleet_policy_page.UNKNOWN_ACTIVATION,
    )
    assert result.status == "saved", result.message
    pinned = fleet_hub_policy.current_policy(conn)["document"]["seats"]["seat-pinned"]
    assert pinned["pinned"] is True
    assert pinned["model"] == "model-a-2"


# --- audit history and rollback ----------------------------------------------


def test_history_is_projected_newest_first(conn):
    _seed(conn, reason="first change")
    _seed(conn, _document(), reason="second change")
    view = fleet_policy_page.load_view(conn, inventory=_fresh_inventory())
    revisions = [row["revision"] for row in view.revisions]
    assert revisions == sorted(revisions, reverse=True)
    page = _render(view)
    assert "second change" in page
    assert "first change" in page


def test_rollback_previews_then_restores_as_a_new_revision(conn):
    first = _seed(conn, reason="first")
    document = _document()
    document["defaults"]["roles"]["impl"] = "seat-beta"
    second = _seed(conn, document, reason="second")

    preview = _apply(
        conn,
        _submission("rollback", "preview", second, to_revision=str(first)),
        actor="operator",
        inventory=_fresh_inventory(),
        activation=fleet_policy_page.UNKNOWN_ACTIVATION,
    )
    assert preview.status == "previewed"
    assert {item["path"] for item in preview.plan.diff["changed"]} == {"defaults.roles.impl"}
    assert fleet_hub_policy.current_policy(conn)["revision"] == second

    applied = _apply(
        conn,
        _submission("rollback", "save", second, to_revision=str(first)),
        actor="operator",
        inventory=_fresh_inventory(),
        activation=fleet_policy_page.UNKNOWN_ACTIVATION,
    )
    assert applied.status == "saved", applied.message
    current = fleet_hub_policy.current_policy(conn)
    assert current["revision"] == second + 1
    assert current["document"]["defaults"]["roles"]["impl"] == "seat-alpha"


def test_rollback_honours_the_optimistic_version_check(conn):
    first = _seed(conn, reason="first")
    second = _seed(conn, _document(), reason="second")
    _seed(conn, _document(), reason="third")
    result = _apply(
        conn,
        _submission("rollback", "save", second, to_revision=str(first)),
        actor="operator",
        inventory=_fresh_inventory(),
        activation=fleet_policy_page.UNKNOWN_ACTIVATION,
    )
    assert result.status == "conflict"


# --- activation compatibility ------------------------------------------------


def test_activation_is_unknown_when_the_migration_module_is_absent(conn, monkeypatch):
    _seed(conn)
    monkeypatch.setattr(fleet_policy_page, "_migration_module", lambda: None)
    activation = fleet_policy_page.probe_activation(conn)
    assert activation.state == "unknown"
    view = fleet_policy_page.load_view(conn, inventory=_fresh_inventory(), activation=activation)
    page = _render(view)
    assert "staged policy" in page.lower()
    assert "does not yet" in page.lower() or "not yet" in page.lower()


def test_activation_probe_reads_the_real_migration_module_when_present(conn):
    """The adapter must line up with the module the authority lane actually ships."""
    _seed(conn)
    activation = fleet_policy_page.probe_activation(conn)
    assert activation.state in ("active", "staged", "unknown")
    module = fleet_policy_page._migration_module()
    if module is None:
        assert activation.state == "unknown"
        return
    assert activation.state == ("active" if module.is_activated(conn) else "staged")
    assert activation.compatibility_behavior == module.migration_status(conn)["compatibility_behavior"]
    page = _render(fleet_policy_page.load_view(conn, inventory=_fresh_inventory(), activation=activation))
    assert ("staged policy" in page.lower()) is not activation.active


def test_activation_is_never_inferred_from_a_populated_policy(conn):
    _seed(conn)
    view = fleet_policy_page.load_view(conn, inventory=_fresh_inventory())
    assert view.activation.state == "unknown"
    assert view.seats


def test_activation_projection_reads_the_migration_status_contract(conn):
    _seed(conn)
    status = {
        "schema": {"table": "fleet_policy_authority", "present": True, "ready": True, "activated": True},
        "activated": True,
        "sources": {"policy_revision": 2, "policy_digest": "sha256:abc"},
        "compatibility_behavior": "authority-owned",
        "affected": ["brigade-run"],
    }
    activation = fleet_policy_page.activation_from_status(status)
    assert activation.state == "active"
    assert activation.compatibility_behavior == "authority-owned"
    view = fleet_policy_page.load_view(conn, inventory=_fresh_inventory(), activation=activation)
    page = _render(view)
    assert "staged policy" not in page.lower()
    assert "authority-owned" in page


def test_activation_status_without_activation_is_staged(conn):
    status = {"activated": False, "compatibility_behavior": "legacy-writable"}
    activation = fleet_policy_page.activation_from_status(status)
    assert activation.state == "staged"
    assert activation.compatibility_behavior == "legacy-writable"


# --- reviewed-preview handshake ---------------------------------------------


def _defaults_change(revision: int, action: str = "save", **extra) -> fleet_policy_page.Submission:
    return _submission("defaults", action, revision, role_impl="seat-beta", **extra)


def test_initial_forms_offer_preview_only(conn):
    _seed(conn)
    page = _render(fleet_policy_page.load_view(conn, inventory=_fresh_inventory()))
    assert 'value="preview"' in page
    assert 'value="save"' not in page
    assert "Confirm save" not in page
    assert "Saving is a second, explicit step" in page


def test_a_save_without_a_preview_is_refused_and_keeps_the_draft(conn):
    revision = _seed(conn)
    submission = _defaults_change(revision)
    result = fleet_policy_page.apply(
        conn,
        submission,
        actor="operator",
        inventory=_fresh_inventory(),
        activation=fleet_policy_page.UNKNOWN_ACTIVATION,
        review_key=REVIEW_KEY,
    )
    assert result.status == "review-required"
    assert "not confirmed against a preview" in result.message
    assert fleet_hub_policy.current_policy(conn)["revision"] == revision
    page = _render(
        fleet_policy_page.load_view(conn, inventory=_fresh_inventory()),
        error=result.message,
        plan=result.plan,
        draft=submission,
        review=result.review,
    )
    # The refusal shows the diff it refused to apply and keeps the draft,
    # including the reason the operator typed.
    assert "defaults.roles.impl" in page
    assert "Confirm save" in page
    assert 'name="reason" maxlength="240" value="test change"' in page


def test_a_successful_preview_renders_the_diff_and_a_confirm_control(conn):
    revision = _seed(conn)
    submission = _defaults_change(revision, "preview")
    previewed = fleet_policy_page.apply(
        conn,
        submission,
        actor="operator",
        inventory=_fresh_inventory(),
        activation=fleet_policy_page.UNKNOWN_ACTIVATION,
        review_key=REVIEW_KEY,
    )
    assert previewed.status == "previewed"
    assert previewed.review
    assert fleet_hub_policy.current_policy(conn)["revision"] == revision
    page = _render(
        fleet_policy_page.load_view(conn, inventory=_fresh_inventory()),
        plan=previewed.plan,
        draft=submission,
        review=previewed.review,
    )
    assert f'name="review" value="{previewed.review}"' in page
    assert 'name="action" value="save"' in page
    confirmed = fleet_policy_page.apply(
        conn,
        replace(submission, action="save", review=previewed.review),
        actor="operator",
        inventory=_fresh_inventory(),
        activation=fleet_policy_page.UNKNOWN_ACTIVATION,
        review_key=REVIEW_KEY,
    )
    assert confirmed.status == "saved", confirmed.message
    assert fleet_hub_policy.current_policy(conn)["document"]["defaults"]["roles"]["impl"] == "seat-beta"


def test_an_edited_draft_invalidates_the_reviewed_token(conn):
    revision = _seed(conn)
    previewed = fleet_policy_page.apply(
        conn,
        _defaults_change(revision, "preview"),
        actor="operator",
        inventory=_fresh_inventory(),
        activation=fleet_policy_page.UNKNOWN_ACTIVATION,
        review_key=REVIEW_KEY,
    )
    # Same scope, same base revision, different document.
    edited = _submission("defaults", "save", revision, role_impl="seat-alpha")
    result = fleet_policy_page.apply(
        conn,
        replace(edited, review=previewed.review or ""),
        actor="operator",
        inventory=_fresh_inventory(),
        activation=fleet_policy_page.UNKNOWN_ACTIVATION,
        review_key=REVIEW_KEY,
    )
    assert result.status == "review-required"
    assert "moved since it was previewed" in result.message
    assert fleet_hub_policy.current_policy(conn)["revision"] == revision


def test_a_reviewed_token_does_not_carry_to_another_scope_or_base_revision(conn):
    revision = _seed(conn)
    minted = fleet_policy_page.review_token(
        REVIEW_KEY, scope="defaults", target="", expected_version=revision, digest="d", reason="r"
    )
    assert minted != fleet_policy_page.review_token(
        REVIEW_KEY, scope="seat", target="", expected_version=revision, digest="d", reason="r"
    )
    assert minted != fleet_policy_page.review_token(
        REVIEW_KEY, scope="defaults", target="seat-alpha", expected_version=revision, digest="d", reason="r"
    )
    assert minted != fleet_policy_page.review_token(
        REVIEW_KEY, scope="defaults", target="", expected_version=revision + 1, digest="d", reason="r"
    )
    assert minted != fleet_policy_page.review_token(
        REVIEW_KEY, scope="defaults", target="", expected_version=revision, digest="other", reason="r"
    )
    assert minted != fleet_policy_page.review_secret("test-review-key-2")


def test_confirmation_revalidates_inventory_and_the_revision(conn):
    """The confirm step re-runs the gates; a preview is not a warrant to write."""
    revision = _seed(conn)
    submission = _submission(
        "seat",
        "save",
        revision,
        target="seat-alpha",
        provider="provider-a",
        model="model-a-3",
        concurrency="1",
        enabled="1",
    )
    previewed = fleet_policy_page.apply(
        conn,
        replace(submission, action="preview"),
        actor="operator",
        inventory=_fresh_inventory(),
        activation=fleet_policy_page.UNKNOWN_ACTIVATION,
        review_key=REVIEW_KEY,
    )
    assert previewed.status == "previewed"
    # The snapshot degrades between preview and confirmation.
    blocked = fleet_policy_page.apply(
        conn,
        replace(submission, review=previewed.review or ""),
        actor="operator",
        inventory=fleet_policy_page.unavailable_inventory("provider login failed"),
        activation=fleet_policy_page.UNKNOWN_ACTIVATION,
        review_key=REVIEW_KEY,
    )
    assert blocked.status == "blocked"
    assert fleet_hub_policy.current_policy(conn)["revision"] == revision
    # Someone else saves, so the base revision the token bound has moved.
    _seed(conn, _document(), reason="someone else")
    stale = fleet_policy_page.apply(
        conn,
        replace(submission, review=previewed.review or ""),
        actor="operator",
        inventory=_fresh_inventory(),
        activation=fleet_policy_page.UNKNOWN_ACTIVATION,
        review_key=REVIEW_KEY,
    )
    assert stale.status == "conflict"


def test_rollback_uses_the_same_reviewed_preview_flow(conn):
    first = _seed(conn)
    document = _document()
    document["defaults"]["roles"]["impl"] = "seat-beta"
    second = _seed(conn, document, reason="second")
    unconfirmed = fleet_policy_page.apply(
        conn,
        _submission("rollback", "save", second, to_revision=str(first)),
        actor="operator",
        inventory=_fresh_inventory(),
        activation=fleet_policy_page.UNKNOWN_ACTIVATION,
        review_key=REVIEW_KEY,
    )
    assert unconfirmed.status == "review-required"
    assert fleet_hub_policy.current_policy(conn)["revision"] == second
    applied = _apply(
        conn,
        _submission("rollback", "save", second, to_revision=str(first)),
        actor="operator",
        inventory=_fresh_inventory(),
        activation=fleet_policy_page.UNKNOWN_ACTIVATION,
    )
    assert applied.status == "saved", applied.message
    assert fleet_hub_policy.current_policy(conn)["document"]["defaults"]["roles"]["impl"] == "seat-alpha"


# --- inventory adapter honesty ------------------------------------------------


def test_the_page_states_that_no_inventory_adapter_is_wired(conn):
    _seed(conn)
    page = _render(fleet_policy_page.load_view(conn))
    assert "Provider inventory is <strong>unavailable</strong>" in page
    assert "make_handler" not in page
    assert "Unavailable is not the same as missing" in page


def test_an_injected_snapshot_is_named_as_its_source(conn):
    _seed(conn)
    page = _render(fleet_policy_page.load_view(conn, inventory=_fresh_inventory()))
    assert "test-injected" in page
    assert "never probes a provider and never reads a credential" in page
    assert "No server-owned provider inventory is wired" not in page


def test_repository_anchors_are_stable_and_collision_free(conn):
    _seed(conn)
    page = _render(fleet_policy_page.load_view(conn, inventory=_fresh_inventory()))
    anchor = fleet_policy_page.repository_anchor("acme/public-tool")
    assert f'id="{anchor}"' in page
    assert anchor == fleet_policy_page.repository_anchor("acme/public-tool")
    assert anchor != fleet_policy_page.repository_anchor("acme-public-tool")


# --- auth, escaping, read-only ----------------------------------------------


def test_read_only_render_offers_no_save_control(conn):
    _seed(conn)
    view = fleet_policy_page.load_view(conn, inventory=_fresh_inventory())
    page = _render(view, editable=False)
    assert "<button" not in page
    assert 'name="csrf"' not in page
    assert "read-only" in page.lower()


def test_values_are_escaped(conn):
    document = _document()
    document["seats"]["seat-alpha"]["notes"] = '<script>alert("x")</script>'
    document["repositories"]["acme/public-tool"]["owner"] = "<b>owner</b>"
    _seed(conn, document)
    view = fleet_policy_page.load_view(conn, inventory=_fresh_inventory())
    page = _render(view)
    assert "<script>alert" not in page
    assert "&lt;script&gt;" in page
    assert "<b>owner</b>" not in page


def test_no_secret_material_is_rendered(conn):
    _seed(conn)
    view = fleet_policy_page.load_view(conn, inventory=_fresh_inventory())
    page = _render(view)
    assert "csrf-token" in page  # the form token is derived from the admin token, never the token itself
    for banned in ("Authorization", "Bearer ", "node_token", "admin_token"):
        assert banned not in page


def test_csrf_value_is_derived_from_the_token_and_is_purpose_bound(conn):
    first = fleet_policy_page.csrf_value("token-one")
    second = fleet_policy_page.csrf_value("token-two")
    assert first != second
    assert first != "token-one"
    from brigade import fleet_hub_roster_page

    assert first != fleet_hub_roster_page.csrf_value("token-one")


def test_malformed_form_is_rejected(conn):
    with pytest.raises(fleet_policy_page.FormError):
        fleet_policy_page.parse_form(b"expected_version=not-a-number&scope=seat&action=save")
    with pytest.raises(fleet_policy_page.FormError):
        fleet_policy_page.parse_form(urlencode({"expected_version": "1", "scope": "nope", "action": "save"}).encode())
    with pytest.raises(fleet_policy_page.FormError):
        fleet_policy_page.parse_form(urlencode({"expected_version": "1", "scope": "seat", "action": "nope"}).encode())


def test_invalid_document_is_reported_without_writing_and_keeps_the_draft(conn):
    revision = _seed(conn)
    submission = _submission(
        "seat",
        "save",
        revision,
        target="seat-alpha",
        provider="provider-a",
        model="model-a-1",
        concurrency="not-a-number",
        enabled="1",
    )
    result = _apply(
        conn,
        submission,
        actor="operator",
        inventory=_fresh_inventory(),
        activation=fleet_policy_page.UNKNOWN_ACTIVATION,
    )
    assert result.status == "invalid"
    assert fleet_hub_policy.current_policy(conn)["revision"] == revision
    page = _render(
        fleet_policy_page.load_view(conn, inventory=_fresh_inventory()),
        error=result.message,
        draft=submission,
    )
    assert "not-a-number" in page


def test_bindings_json_editor_rejects_unknown_groups(conn):
    revision = _seed(conn)
    submission = _submission(
        "seat",
        "save",
        revision,
        target="seat-alpha",
        provider="provider-a",
        model="model-a-1",
        concurrency="1",
        enabled="1",
        bindings='{"unknown_group": {"cli": "x"}}',
    )
    result = _apply(
        conn,
        submission,
        actor="operator",
        inventory=_fresh_inventory(),
        activation=fleet_policy_page.UNKNOWN_ACTIVATION,
    )
    assert result.status == "invalid"
    assert fleet_hub_policy.current_policy(conn)["revision"] == revision


# --- HTTP route (auth, CSRF, read-only) --------------------------------------


class _RouteHub:
    """A live hub bound to loopback, so the policy route is exercised end to end."""

    def __init__(self, tmp_path, *, trust_tailscale=False):
        from brigade import fleet_hub as hub_mod

        self.token = "test-admin-token-policy"  # content-guard: allow api-key-assignment
        self.server = hub_mod.make_server(
            "127.0.0.1",  # content-guard: allow loopback-ipv4
            0,
            tmp_path / "hub" / "fleet.db",
            self.token,
            trust_tailscale_identity=trust_tailscale,
        )
        self.db = tmp_path / "hub" / "fleet.db"
        self.address = ("127.0.0.1", self.server.server_address[1])  # content-guard: allow loopback-ipv4

    def __enter__(self):
        import threading

        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(self, method, path, *, headers=None, body=None):
        import http.client

        conn = http.client.HTTPConnection(*self.address, timeout=5)
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        text = response.read().decode("utf-8")
        result = (response.status, {k.lower(): v for k, v in response.getheaders()}, text)
        conn.close()
        return result

    def cookie(self):
        status, headers, _ = self.request("GET", f"/deck/policy?token={self.token}")
        assert status == 303
        assert headers["location"] == "/deck/policy"
        return headers["set-cookie"].split(";")[0]

    def form(self, fields, *, cookie=None, extra=None):
        headers = {"Content-Type": "application/x-www-form-urlencoded", "Sec-Fetch-Site": "same-origin"}
        if cookie:
            headers["Cookie"] = cookie
        headers.update(extra or {})
        return self.request("POST", "/deck/policy", headers=headers, body=urlencode(fields).encode())


def _confirm_fields(page: str) -> dict:
    """The hidden fields of the rendered confirm-save form, as a browser would post them."""
    import re

    form = page.split('class="roster-form confirm-save"', 1)[1].split("</form>", 1)[0]
    return dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', form))


def test_policy_route_requires_authorization(tmp_path):
    with _RouteHub(tmp_path) as hub:
        status, _headers, text = hub.request("GET", "/deck/policy")
        assert status == 401
        assert "edits the policy page" in text
        assert hub.token not in text


def test_policy_route_renders_for_the_admin_cookie_and_bearer(tmp_path):
    with _RouteHub(tmp_path) as hub:
        cookie = hub.cookie()
        status, headers, page = hub.request("GET", "/deck/policy", headers={"Cookie": cookie})
        assert status == 200
        assert headers["cache-control"] == "no-store"
        assert "content-security-policy" in headers
        assert "Command Deck &middot; Policy" in page
        assert hub.token not in page and cookie.split("=", 1)[1] not in page
        bearer = {"Authorization": f"Bearer {hub.token}"}
        assert hub.request("GET", "/deck/policy", headers=bearer)[0] == 200


def test_policy_route_is_read_only_under_tailscale_identity(tmp_path):
    with _RouteHub(tmp_path, trust_tailscale=True) as hub:
        headers = {"Tailscale-User-Login": "viewer@example.test"}
        status, _headers, page = hub.request("GET", "/deck/policy", headers=headers)
        assert status == 200
        assert "read-only" in page
        assert "<button" not in page
        status, _headers, text = hub.form(
            {"scope": "defaults", "action": "save", "expected_version": "1", "csrf": "x"}, extra=headers
        )
        assert status == 403
        assert "read-only" in text


def test_policy_route_refuses_a_cross_origin_post(tmp_path):
    with _RouteHub(tmp_path) as hub:
        cookie = hub.cookie()
        status, _headers, text = hub.form(
            {"scope": "defaults", "action": "save", "expected_version": "1", "csrf": "x"},
            cookie=cookie,
            extra={"Sec-Fetch-Site": "cross-site"},
        )
        assert status == 403
        assert "cross-origin" in text


def test_policy_route_refuses_a_bad_form_token(tmp_path):
    with _RouteHub(tmp_path) as hub:
        cookie = hub.cookie()
        status, _headers, text = hub.form(
            {"scope": "defaults", "action": "save", "expected_version": "1", "csrf": "wrong"}, cookie=cookie
        )
        assert status == 403
        assert "form token mismatch" in text


def test_policy_route_previews_and_saves(tmp_path):
    with _RouteHub(tmp_path) as hub:
        conn = fleet_hub.open_db(hub.db)
        try:
            revision = _seed(conn)
        finally:
            conn.close()
        cookie = hub.cookie()
        csrf = fleet_policy_page.csrf_value(hub.token)
        base = {
            "scope": "defaults",
            "expected_version": str(revision),
            "csrf": csrf,
            "reason": "route test",
            "field.role_impl": "seat-beta",
        }
        # A save that was never previewed is refused, and nothing is written.
        status, _headers, page = hub.form({**base, "action": "save"}, cookie=cookie)
        assert status == 422
        assert "not confirmed against a preview" in page
        conn = fleet_hub.open_db(hub.db)
        try:
            assert fleet_hub_policy.current_policy(conn)["revision"] == revision
        finally:
            conn.close()
        status, _headers, page = hub.form({**base, "action": "preview"}, cookie=cookie)
        assert status == 200
        assert "defaults.roles.impl" in page
        assert "Confirm save" in page
        conn = fleet_hub.open_db(hub.db)
        try:
            assert fleet_hub_policy.current_policy(conn)["revision"] == revision
        finally:
            conn.close()
        # Confirm exactly what the preview rendered, the way a browser would.
        status, headers, _text = hub.form(_confirm_fields(page), cookie=cookie)
        assert status == 303
        assert headers["location"] == f"/deck/policy?saved={revision + 1}"
        conn = fleet_hub.open_db(hub.db)
        try:
            current = fleet_hub_policy.current_policy(conn)
            assert current["revision"] == revision + 1
            assert current["document"]["defaults"]["roles"]["impl"] == "seat-beta"
        finally:
            conn.close()


def test_policy_route_returns_409_on_a_stale_version(tmp_path):
    with _RouteHub(tmp_path) as hub:
        conn = fleet_hub.open_db(hub.db)
        try:
            revision = _seed(conn)
            _seed(conn, _document(), reason="someone else")
        finally:
            conn.close()
        cookie = hub.cookie()
        status, _headers, page = hub.form(
            {
                "scope": "defaults",
                "action": "save",
                "expected_version": str(revision),
                "csrf": fleet_policy_page.csrf_value(hub.token),
                "reason": "stale",
                "field.role_impl": "seat-beta",
            },
            cookie=cookie,
        )
        assert status == 409
        assert "changed underneath you" in page


def test_policy_route_blocks_an_unverified_identity_by_default(tmp_path):
    """No trusted snapshot is injected by default, so a new identity is refused."""
    with _RouteHub(tmp_path) as hub:
        conn = fleet_hub.open_db(hub.db)
        try:
            revision = _seed(conn)
        finally:
            conn.close()
        cookie = hub.cookie()
        status, _headers, page = hub.form(
            {
                "scope": "seat",
                "action": "save",
                "target": "seat-alpha",
                "expected_version": str(revision),
                "csrf": fleet_policy_page.csrf_value(hub.token),
                "reason": "identity move",
                "field.provider": "provider-a",
                "field.model": "model-a-3",
                "field.concurrency": "1",
                "field.enabled": "1",
            },
            cookie=cookie,
        )
        assert status == 422
        assert "unavailable" in page
        conn = fleet_hub.open_db(hub.db)
        try:
            assert fleet_hub_policy.current_policy(conn)["revision"] == revision
        finally:
            conn.close()


def test_policy_route_rejects_an_oversized_or_wrong_media_type_body(tmp_path):
    with _RouteHub(tmp_path) as hub:
        cookie = hub.cookie()
        status, _headers, _text = hub.request(
            "POST",
            "/deck/policy",
            headers={"Content-Type": "application/json", "Cookie": cookie, "Sec-Fetch-Site": "same-origin"},
            body=b"{}",
        )
        assert status == 415
        status, _headers, _text = hub.request(
            "POST",
            "/deck/policy",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Cookie": cookie,
                "Sec-Fetch-Site": "same-origin",
                "Content-Length": str(fleet_policy_page.MAX_FORM_BYTES + 1),
            },
            body=b"x" * (fleet_policy_page.MAX_FORM_BYTES + 1),
        )
        assert status == 413


def test_deck_nav_links_to_the_policy_page(tmp_path):
    with _RouteHub(tmp_path) as hub:
        bearer = {"Authorization": f"Bearer {hub.token}"}
        assert '<a href="/deck/policy">policy</a>' in hub.request("GET", "/", headers=bearer)[2]
        assert '<a href="/deck/policy">policy</a>' in hub.request("GET", "/deck/roster", headers=bearer)[2]


# --- activation-aware application text ----------------------------------------

ACTIVE = fleet_policy_page.Activation(
    state="active",
    detail="policy authority is active",
    compatibility_behavior="policy-owns-roles",
    source="fleet_policy_migration",
)
STAGED = fleet_policy_page.Activation(
    state="staged",
    detail="policy authority is not activated",
    compatibility_behavior="legacy-owns-roles",
    source="fleet_policy_migration",
)


def _preview(conn, revision, *, activation):
    return _apply(
        conn,
        _submission("defaults", "preview", revision, role_impl="seat-beta"),
        inventory=_fresh_inventory(),
        activation=activation,
    )


def _save(conn, revision, *, activation):
    return _apply(
        conn,
        _submission("defaults", "save", revision, role_impl="seat-beta"),
        inventory=_fresh_inventory(),
        activation=activation,
    )


def test_a_staged_save_does_not_claim_it_changed_runtime(conn):
    revision = _seed(conn)
    result = _save(conn, revision, activation=STAGED)
    assert result.status == "saved"
    assert "staged" in result.message
    assert "does not change runtime routing" in result.message
    assert "refresh" not in result.message
    assert "restart" not in result.message


def test_an_unknown_activation_is_treated_as_staged_on_save(conn):
    revision = _seed(conn)
    result = _save(conn, revision, activation=fleet_policy_page.UNKNOWN_ACTIVATION)
    assert result.status == "saved"
    assert "Activation status is unknown" in result.message
    assert "treated as staged" in result.message
    assert "nothing here shows it reaching runtime" in result.message


def test_an_active_save_names_load_and_the_documented_refresh(conn):
    revision = _seed(conn)
    result = _save(conn, revision, activation=ACTIVE)
    assert result.status == "saved"
    assert "future session loads pick this revision up" in result.message
    # A save is never reported as a reload of something already running.
    assert "documented refresh or restart" in result.message
    assert "reloaded" not in result.message


def test_the_preview_message_states_what_saving_would_do(conn):
    revision = _seed(conn)
    staged = _preview(conn, revision, activation=STAGED)
    assert staged.status == "previewed"
    assert "nothing was written" in staged.message
    assert "does not change runtime routing" in staged.message

    active = _preview(conn, revision, activation=ACTIVE)
    assert "future session loads pick this revision up" in active.message


def test_a_staged_plan_panel_does_not_promise_a_live_effect(conn):
    revision = _seed(conn)
    result = _preview(conn, revision, activation=STAGED)
    view = fleet_policy_page.load_view(conn, inventory=_fresh_inventory(), activation=STAGED)
    page = _render(view, plan=result.plan, review=result.review)

    assert "does not change runtime routing" in page
    assert "once the authority is activated" in page
    assert "they do not describe an effect of saving today" in page


def test_an_active_plan_panel_separates_load_from_running_sessions(conn):
    revision = _seed(conn)
    result = _preview(conn, revision, activation=ACTIVE)
    view = fleet_policy_page.load_view(conn, inventory=_fresh_inventory(), activation=ACTIVE)
    page = _render(view, plan=result.plan, review=result.review)

    assert "future session loads pick this revision up" in page
    assert "documented refresh or restart" in page


def test_the_confirmation_binding_and_compare_and_swap_still_hold(conn):
    """Activation wording is added on top of the handshake, never in place of it."""
    revision = _seed(conn)
    previewed = fleet_policy_page.apply(
        conn,
        _submission("defaults", "preview", revision, role_impl="seat-beta"),
        actor="operator",
        inventory=_fresh_inventory(),
        activation=ACTIVE,
        review_key=REVIEW_KEY,
    )
    unconfirmed = fleet_policy_page.apply(
        conn,
        _submission("defaults", "save", revision, role_impl="seat-beta"),
        actor="operator",
        inventory=_fresh_inventory(),
        activation=ACTIVE,
        review_key=REVIEW_KEY,
    )
    assert unconfirmed.status == "review-required"

    tampered = replace(
        _submission("defaults", "save", revision, role_impl="seat-alpha"),
        review=previewed.review or "",
    )
    assert (
        fleet_policy_page.apply(
            conn,
            tampered,
            actor="operator",
            inventory=_fresh_inventory(),
            activation=ACTIVE,
            review_key=REVIEW_KEY,
        ).status
        == "review-required"
    )

    stale = replace(
        _submission("defaults", "save", revision - 1, role_impl="seat-beta"),
        review=previewed.review or "",
    )
    assert (
        fleet_policy_page.apply(
            conn,
            stale,
            actor="operator",
            inventory=_fresh_inventory(),
            activation=ACTIVE,
            review_key=REVIEW_KEY,
        ).status
        == "conflict"
    )


# --- related identifiers, not recommendations ---------------------------------


def test_related_identifiers_are_alphabetical_not_prefix_ranked():
    related = fleet_policy_page.suggest_models("model-a-9", ["model-a-3", "model-b-1", "model-a-1", "model-a-2"])
    assert related == sorted(related)
    # A longer shared prefix does not float to the top: order carries no ranking.
    assert related == ["model-a-1", "model-a-2", "model-a-3", "model-b-1"]


def test_related_identifiers_never_include_the_pinned_identity_itself():
    assert "model-a-1" not in fleet_policy_page.suggest_models("model-a-1", ["model-a-1", "model-a-2"])


def test_unrelated_identifiers_are_not_offered():
    assert fleet_policy_page.suggest_models("model-a-1", ["other-thing-1"]) == []


def test_a_refused_identity_labels_its_list_as_related_not_recommended(conn):
    revision = _seed(conn)
    result = _apply(
        conn,
        _submission(
            "seat",
            "preview",
            revision,
            target="seat-alpha",
            provider="provider-a",
            model="model-a-9",
            effort="high",
            eligible_machines="worker-linux-1",
        ),
        inventory=_fresh_inventory(),
        activation=STAGED,
    )
    assert result.status == "blocked"
    view = fleet_policy_page.load_view(conn, inventory=_fresh_inventory(), activation=STAGED)
    page = _render(view, plan=result.plan)

    assert fleet_policy_page.SUGGEST_LABEL in page
    assert "not recommendations, never applied" in page
    # The list is never framed as an upgrade path.
    assert "upgrade" not in page.replace("is not an upgrade", "")


def test_the_seat_legend_says_what_a_shared_prefix_is_not_evidence_of(conn):
    _seed(conn)
    page = _render(fleet_policy_page.load_view(conn, inventory=_fresh_inventory()))
    for phrase in ("benchmark results", "latency", "price or quota", "privacy terms", "fitness for this seat"):
        assert phrase in page
    assert "the order carries no ranking" in page


# --- inventory provenance -----------------------------------------------------


def test_the_inventory_panel_shows_source_scope_and_observation_time(conn):
    _seed(conn)
    inventory = fleet_policy_page.Inventory(
        providers={
            "provider-a": {
                "state": "fresh",
                "available": ["model-a-1", "model-a-2"],
                "scope": "native CLI listing",
                "observed_at": "2026-09-05T10:15:00Z",
                "cli_auth_state": "signed-in",
            },
            "provider-b": {"state": "fresh", "available": ["model-b-1"]},
        },
        source="test-injected",
        captured_at="2026-09-05T11:00:00Z",
    )
    page = _render(fleet_policy_page.load_view(conn, inventory=inventory))

    assert "test-injected" in page
    assert "2026-09-05T11:00:00Z" in page
    assert "native CLI listing" in page
    assert "2026-09-05T10:15:00Z" in page
    assert "signed-in" in page
    # Availability and CLI authentication stay separate facts.
    assert "CLI auth is a separate fact from availability" in page


def test_inventory_metadata_the_snapshot_omits_reads_as_unknown(conn):
    _seed(conn)
    inventory = fleet_policy_page.Inventory(
        providers={"provider-a": {"state": "fresh", "available": ["model-a-1"]}},
        source="test-injected",
        captured_at="2026-09-05T11:00:00Z",
    )
    page = _render(fleet_policy_page.load_view(conn, inventory=inventory))
    scope_row = page.split("<th>CLI auth</th>")[1]
    assert scope_row.count(fleet_policy_page.UNKNOWN_TEXT) >= 2


def test_an_unavailable_inventory_renders_no_provider_scope_claims(conn):
    _seed(conn)
    page = _render(
        fleet_policy_page.load_view(conn, inventory=fleet_policy_page.unavailable_inventory("provider login failed"))
    )
    assert "provider login failed" in page
    assert "<th>Inventory scope</th>" not in page


# --- acknowledged session snapshot --------------------------------------------


def _record_session(conn, revision):
    fleet_hub_policy.record_session_policy(
        conn,
        node_id=NODE_A,
        session_id="session-one",
        consumer="brigade-run",
        repo_identity="acme/public-tool",
        revision=revision,
        digest=fleet_hub_policy.current_policy(conn)["digest"],
        source="hub",
        loaded_at="2026-09-05T11:30:00Z",
    )


def test_the_session_projection_carries_what_the_session_acknowledged(conn):
    revision = _seed(conn)
    _record_session(conn, revision)
    row = fleet_policy_page.load_view(conn, inventory=_fresh_inventory()).sessions[0]

    assert row.owner_node == NODE_A
    assert row.source == "hub"
    assert row.loaded_at.startswith("2026-09-05T11:30:00")
    assert row.revision == revision
    assert row.digest == fleet_hub_policy.current_policy(conn)["digest"]


def test_the_sessions_table_separates_the_acknowledgement_from_policy_now(conn):
    revision = _seed(conn)
    _record_session(conn, revision)
    page = _render(fleet_policy_page.load_view(conn, inventory=_fresh_inventory()))

    assert "Acknowledged by the session" in page
    assert "Current policy and pending refresh" in page
    assert "<th>Owner node</th>" in page
    assert "<th>Origin</th>" in page
    assert "<th>Loaded at</th>" in page
    assert "<th>Digest</th>" in page
    assert "2026-09-05T11:30:00" in page


def test_the_loaded_effective_settings_stay_unknown_until_the_contract_lands(conn):
    """The hub records the acknowledgement, not the settings the session resolved."""
    revision = _seed(conn)
    _record_session(conn, revision)
    # Move the document on. The session's row must not start reporting the new
    # values as though the session had loaded them.
    document = _document()
    document["defaults"]["execution"]["concurrency"] = 9
    _seed_again = fleet_hub_policy.save_policy(
        conn,
        document,
        expected_version=revision,
        actor="operator",
        reason="raise concurrency",
    )
    page = _render(fleet_policy_page.load_view(conn, inventory=_fresh_inventory()))

    assert "<th>Loaded effective settings</th>" in page
    assert html.escape(fleet_policy_page.SNAPSHOT_CONTRACT_UNAVAILABLE) in page
    loaded_cell = page.split("<th>Loaded effective settings</th>")[1]
    assert f'<span class="unknown">{fleet_policy_page.UNKNOWN_TEXT}</span>' in loaded_cell
    # The old revision is still what the session acknowledged.
    assert str(revision) in loaded_cell
    assert int(_seed_again["revision"]) == revision + 1


def test_durable_session_snapshots_rendered_with_provenance_and_pending_status(conn):
    revision = _seed(conn)
    current = fleet_hub_policy.current_policy(conn)

    # 1. Prepare and acknowledge a snapshot for session-one
    resolved_one = fleet_policy.resolve_policy(
        current["document"],
        consumer="brigade-run",
        repo_identity="acme/public-tool",
        overrides={"execution": {"concurrency": 4}},
        override_reason="speed up test run",
    )
    chash_one = "sha256:" + hashlib.sha256(b"session-one-ctx-hash-val-12345678").hexdigest()
    pending_one = fleet_hub_policy.record_pending_policy(
        conn,
        session_id="session-one-ext",
        consumer="brigade-run",
        node_id=NODE_A,
        repo_identity="acme/public-tool",
        origin="web-terminal",
        revision=revision,
        digest=current["digest"],
        overrides={"execution": {"concurrency": 4}},
        override_reason="speed up test run",
        effective=resolved_one["effective"],
        sources=resolved_one["sources"],
        context_hash=chash_one,
    )
    fleet_hub_policy.record_acknowledged_policy(
        conn,
        pending_one,
        node_id=NODE_A,
        expected_context_hash=chash_one,
        loaded_at="2026-09-05T11:45:00Z",
    )

    # Bump document revision
    doc2 = _document()
    doc2["defaults"]["execution"]["concurrency"] = 8
    rev2 = _seed(conn, doc2, reason="bump concurrency")

    # Record a pending snapshot for session-one under revision 2 (not yet acknowledged)
    resolved_pending = fleet_policy.resolve_policy(
        doc2,
        consumer="brigade-run",
        repo_identity="acme/public-tool",
    )
    chash_pending = "sha256:" + hashlib.sha256(b"session-one-ctx-hash-val-87654321").hexdigest()
    fleet_hub_policy.record_pending_policy(
        conn,
        session_id="session-one-ext",
        consumer="brigade-run",
        node_id=NODE_A,
        repo_identity="acme/public-tool",
        origin="web-terminal",
        revision=rev2,
        digest=fleet_hub_policy.current_policy(conn)["digest"],
        overrides={},
        override_reason=None,
        effective=resolved_pending["effective"],
        sources=resolved_pending["sources"],
        context_hash=chash_pending,
    )

    # Also record a refresh request on session-one
    fleet_hub_policy.request_session_refresh(conn, pending_one["pending_key"], actor="tester")

    view = fleet_policy_page.load_view(conn, inventory=_fresh_inventory())
    matching = [s for s in view.sessions if s.external_session_id == "session-one-ext"]
    assert len(matching) == 1
    session_row = matching[0]

    assert session_row.owner_node == NODE_A
    assert session_row.origin == "web-terminal"
    assert session_row.context_hash == chash_one
    assert session_row.revision == revision
    assert session_row.current_revision == rev2
    assert session_row.pending_apply_required is True
    assert session_row.pending_snapshot is not None
    assert session_row.pending_snapshot["context_hash"] == chash_pending
    assert session_row.refresh_state == "requested"

    page = _render(view)
    # Check session identification
    assert "session-one-ext" in page
    # Check durable acknowledged settings and context hash
    assert chash_one in page
    assert "acknowledged settings" in page
    assert "speed up test run" in page
    # Check pending snapshot
    assert f"rev {rev2}" in page
    assert "pending apply required" in page
    assert chash_pending in page
    # Refresh requested must not claim applied
    assert "requested" in page
    assert "applied" not in session_row.refresh_state


def test_allow_free_sparse_setting_and_inherited_distinction(conn):
    revision = _seed(conn)
    inv = _fresh_inventory()
    act = fleet_policy_page.Activation("active", "activated", "authority-owned", "test")

    # 1. Defaults form has allow_free input
    page_initial = _render(fleet_policy_page.load_view(conn, inventory=inv, activation=act))
    assert 'name="field.data_allow_free"' in page_initial
    assert "policy key: data.allow_free" in page_initial
    assert 'name="field.patch_data_allow_free"' in page_initial
    assert "policy key: patches.data.allow_free" in page_initial

    # 2. Repository override editor: set allow_free to "yes" on acme/public-tool
    sub = _submission("repository", "save", revision, target="acme/public-tool", patch_data_allow_free="yes")
    res = _apply(conn, sub, inventory=inv, activation=act)
    assert res.status == "saved"
    rev1 = res.revision

    view1 = fleet_policy_page.load_view(conn, inventory=inv, activation=act)
    repo1 = next(r for r in view1.repositories if r.identity == "acme/public-tool")
    assert repo1.effective["data"]["allow_free"] is True
    assert repo1.patches.get("data", {}).get("allow_free") is True
    page1 = _render(view1)
    assert "allow_free True" in page1 or "allow_free yes" in page1

    # Check that another repository without override inherits default (False)
    repo_priv = next(r for r in view1.repositories if r.identity == "acme/private-tool")
    assert repo_priv.effective["data"]["allow_free"] is False
    assert "allow_free" not in repo_priv.patches.get("data", {})

    # 3. Clear repository override back to inherit (empty string)
    sub_clear = _submission("repository", "save", rev1, target="acme/public-tool", patch_data_allow_free="")
    res_clear = _apply(conn, sub_clear, inventory=inv, activation=act)
    assert res_clear.status == "saved"

    view2 = fleet_policy_page.load_view(conn, inventory=inv, activation=act)
    repo2 = next(r for r in view2.repositories if r.identity == "acme/public-tool")
    assert repo2.effective["data"]["allow_free"] is False
    assert "allow_free" not in repo2.patches.get("data", {})


def test_seat_cost_class_expert_select_and_role_classification(conn):
    revision = _seed(conn)
    inv = _fresh_inventory()
    act = fleet_policy_page.Activation("active", "activated", "authority-owned", "test")

    page_initial = _render(fleet_policy_page.load_view(conn, inventory=inv, activation=act))
    # Seat table column Cost class is present
    assert "<th>Cost class</th>" in page_initial
    assert 'name="field.cost_class"' in page_initial
    assert "cost_class (paid, subscription, free, unknown)" in page_initial

    # Update seat-alpha cost_class to paid
    sub = _submission("seat", "save", revision, target="seat-alpha", cost_class="paid")
    res = _apply(conn, sub, inventory=inv, activation=act)
    assert res.status == "saved"

    view1 = fleet_policy_page.load_view(conn, inventory=inv, activation=act)
    seat_alpha = next(s for s in view1.seats if s.name == "seat-alpha")
    assert seat_alpha.cost_class == "paid"

    page1 = _render(view1)
    # Role assignment section shows cost classification
    assert "cost paid" in page1


def test_missing_origin_is_unknown_and_source_is_not_origin(conn):
    revision = _seed(conn)
    _record_session(conn, revision)
    view = fleet_policy_page.load_view(conn, inventory=_fresh_inventory())
    row = view.sessions[0]
    assert row.origin is None
    assert row.source == "hub"
    page = _render(view)
    origin_cell = page.split("<th>Origin</th>", 1)[1]
    assert f'<span class="unknown">{fleet_policy_page.UNKNOWN_TEXT}</span>' in origin_cell
    assert "(src: hub)" in page


def _coverage_window():
    now = datetime.now(timezone.utc).replace(microsecond=0)
    captured = (now - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    expires = (now + timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return captured, expires


def test_sql_inventory_snapshot_is_used_when_no_callback_is_injected(conn):
    from brigade import fleet_model_inventory as fmi

    captured, expires = _coverage_window()
    _seed(conn)
    fmi.ensure_schema(conn)
    fmi.ingest(
        conn,
        {
            "schema": fmi.PAYLOAD_SCHEMA,
            "source": "cli:agy",
            "provider": "provider-a",
            "harness": "agy",
            "account_id": "",
            "scope": "complete",
            "status": "ok",
            "captured_at": captured,
            "expires_at": expires,
            "models": ["model-a-1", "model-a-2", "model-a-3"],
        },
        trusted_source="cli:agy",
    )
    view = fleet_policy_page.load_view(conn)
    assert view.inventory.source == "cli:agy"
    assert view.inventory.captured_at == captured
    states = {row.name: row.inventory_state for row in view.seats}
    assert states["seat-alpha"] == "available"
    assert states["seat-beta"] == "unavailable"
    page = _render(view)
    assert "cli:agy" in page
    assert "cli_probe" in page


def test_opencode_catalog_is_not_cli_auth_coverage(conn):
    from brigade import fleet_model_inventory as fmi

    captured, expires = _coverage_window()
    _seed(conn)
    fmi.ensure_schema(conn)
    fmi.ingest(
        conn,
        {
            "schema": fmi.PAYLOAD_SCHEMA,
            "source": "cli:opencode",
            "provider": "provider-b",
            "harness": "opencode",
            "account_id": "",
            "scope": "complete",
            "status": "ok",
            "captured_at": captured,
            "expires_at": expires,
            "models": ["model-b-1"],
            "evidence_type": "catalog",
        },
        trusted_source="cli:opencode",
    )
    view = fleet_policy_page.load_view(conn)
    report = view.inventory.providers["provider-b"]
    assert report["state"] == "fresh"
    assert report["cli_auth_state"] == fleet_policy_page.UNKNOWN_TEXT


def test_policy_route_revalidates_changed_inventory_between_preview_and_save(tmp_path):
    from brigade import fleet_model_inventory as fmi

    captured, expires = _coverage_window()
    with _RouteHub(tmp_path) as hub:
        conn = fleet_hub.open_db(hub.db)
        try:
            revision = _seed(conn)
            fmi.ensure_schema(conn)
            fmi.ingest(
                conn,
                {
                    "schema": fmi.PAYLOAD_SCHEMA,
                    "source": "cli:agy",
                    "provider": "provider-a",
                    "harness": "agy",
                    "account_id": "",
                    "scope": "complete",
                    "status": "ok",
                    "captured_at": captured,
                    "expires_at": expires,
                    "models": ["model-a-1", "model-a-2", "model-a-3"],
                },
                trusted_source="cli:agy",
            )
            conn.commit()
        finally:
            conn.close()
        cookie = hub.cookie()
        csrf = fleet_policy_page.csrf_value(hub.token)
        fields = {
            "scope": "seat",
            "expected_version": str(revision),
            "csrf": csrf,
            "reason": "identity move",
            "target": "seat-alpha",
            "field.provider": "provider-a",
            "field.model": "model-a-3",
            "field.concurrency": "1",
            "field.enabled": "1",
        }
        status, _headers, page = hub.form({**fields, "action": "preview"}, cookie=cookie)
        assert status == 200, page
        assert "Confirm save" in page
        conn = fleet_hub.open_db(hub.db)
        try:
            conn.execute(
                "UPDATE fleet_model_inventory_projections SET expires_at=?, status='ok' "
                "WHERE provider=? AND harness=? AND account_id=?",
                ("2020-01-01T00:00:00Z", "provider-a", "agy", ""),
            )
            conn.commit()
        finally:
            conn.close()
        status, _headers, blocked = hub.form(_confirm_fields(page), cookie=cookie)
        assert status == 422
        assert "unavailable" in blocked
        conn = fleet_hub.open_db(hub.db)
        try:
            assert fleet_hub_policy.current_policy(conn)["revision"] == revision
        finally:
            conn.close()


def test_adapt_inventory_behavior():
    # Absent/invalid raw input
    inv_none = fleet_policy_page.adapt_inventory(None)
    assert inv_none.usable is False
    assert inv_none.source == "unavailable"

    # Minimal dictionary without providers
    inv_empty = fleet_policy_page.adapt_inventory({"source": "custom-source"})
    assert inv_empty.usable is False
    assert inv_empty.source == "custom-source"

    # Dictionary with valid provider metadata
    raw = {
        "source": "provider-cli",
        "captured_at": "2026-09-05T12:00:00Z",
        "providers": {
            "p1": {
                "state": "fresh",
                "scope": "cli",
                "cli_auth_state": "signed-in",
                "available": ["m1", "m2"],
            }
        },
    }
    inv_valid = fleet_policy_page.adapt_inventory(raw)
    assert inv_valid.usable is True
    assert inv_valid.source == "provider-cli"
    assert inv_valid.providers["p1"]["scope"] == "cli"
    assert inv_valid.providers["p1"]["cli_auth_state"] == "signed-in"
    assert inv_valid.providers["p1"]["available"] == ["m1", "m2"]
