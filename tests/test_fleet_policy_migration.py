"""Explicit Fleet authority migration: preview, CAS activation, and projected reads."""

from __future__ import annotations

import copy
import sqlite3

import pytest

from brigade import (
    fleet_hub,
    fleet_hub_model_roster,
    fleet_hub_policy,
    fleet_hub_policy_api,
    fleet_hub_preference,
    fleet_model_roster,
    fleet_policy,
    fleet_policy_migration,
)
from brigade.fleet_hub import FleetHubConflict, FleetHubError

LAUNCH_REQUEST = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1"


NODE_A = "11111111-1111-4111-8111-111111111111"
SEAT_ALPHA = "seat-alpha"
SEAT_BETA = "seat-beta"
SEAT_GAMMA = "seat-gamma"
PROVIDER_A = "provider-a"
PROVIDER_B = "provider-b"
MODEL_A = "model-a-1"
MODEL_B = "model-b-1"
MODEL_A_DRIFT = "model-a-9"
MACHINE = "worker-linux-1"
REPO = "repo/public"


@pytest.fixture()
def conn(tmp_path):
    connection = fleet_hub.init_db(tmp_path / "fleet.db")
    try:
        yield connection
    finally:
        connection.close()


def _dump(conn: sqlite3.Connection) -> str:
    return "\n".join(conn.iterdump())


def _roster_revision(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT revision FROM model_roster_meta WHERE singleton=1").fetchone()[0])


def _policy_revision(conn: sqlite3.Connection) -> int:
    return fleet_hub_policy.current_policy(conn)["revision"]


def _annotate(*seats: str, **fields: object) -> dict:
    body = {"pinned": False, "training_allowed": False, **fields}
    return {"seats": {name: dict(body) for name in seats}}


def _set_seat(
    conn: sqlite3.Connection,
    *,
    seat: str,
    provider: str,
    model: str,
    reasoning: str = "high",
    enabled: bool = True,
    limit: int | None = 3,
    brigade_cli: str = "cli-alpha",
    t3_instance_id: str = "inst-alpha",
    t3_service_tier: str = "standard",
) -> dict:
    status, payload = fleet_hub_model_roster.handle_model_policy(
        conn,
        {
            "action": "set",
            "expected_revision": _roster_revision(conn),
            "seat": seat,
            "provider": provider,
            "model": model,
            "reasoning": reasoning,
            "enabled": enabled,
            "limit": limit,
            "brigade_cli": brigade_cli,
            "t3_instance_id": t3_instance_id,
            "t3_service_tier": t3_service_tier,
            "notes": "",
        },
    )
    assert status == 200, payload
    return payload


def _set_default(conn: sqlite3.Connection, consumer: str, seat: str) -> dict:
    status, payload = fleet_hub_model_roster.handle_model_policy(
        conn,
        {
            "action": "set-default",
            "expected_revision": _roster_revision(conn),
            "consumer": consumer,
            "seat": seat,
        },
    )
    assert status == 200, payload
    return payload


def _retire(conn: sqlite3.Connection, provider: str, family: str) -> dict:
    status, payload = fleet_hub_model_roster.handle_model_policy(
        conn,
        {
            "action": "retire",
            "expected_revision": _roster_revision(conn),
            "provider": provider,
            "family": family,
            "permanent": False,
            "reason_code": "operator-retired",
        },
    )
    assert status == 200, payload
    return payload


def _seed_legacy(conn: sqlite3.Connection) -> None:
    _set_seat(
        conn,
        seat=SEAT_ALPHA,
        provider=PROVIDER_A,
        model=MODEL_A,
        reasoning="high",
        enabled=True,
        limit=3,
        brigade_cli="cli-alpha",
        t3_instance_id="inst-alpha",
        t3_service_tier="standard",
    )
    _set_seat(
        conn,
        seat=SEAT_BETA,
        provider=PROVIDER_B,
        model=MODEL_B,
        reasoning="none",
        enabled=False,
        limit=1,
        brigade_cli="cli-beta",
        t3_instance_id="inst-beta",
        t3_service_tier="premium",
    )
    _set_default(conn, "t3-fleet", SEAT_ALPHA)
    fleet_hub_preference.set_run_preference(conn, {"impl": SEAT_ALPHA, "review": SEAT_BETA}, updated_by="admin")


def _authority_document() -> dict:
    return {
        "schema": fleet_policy.POLICY_SCHEMA,
        "defaults": {"data": {"allow_training": False}},
        "machines": {MACHINE: {"os": "linux", "concurrency": 2, "node_id": NODE_A}},
        "seats": {},
        "consumers": {
            "brigade-run": {"reload": "refreshable", "coverage": "unverified", "notes": "kept-brigade"},
            "t3-fleet": {"reload": "none", "coverage": "unverified", "notes": "kept-t3"},
        },
        "repositories": {REPO: {"privacy": "public"}},
    }


def _preview(conn: sqlite3.Connection, annotations=None):
    return fleet_policy_migration.preview_migration(
        conn,
        expected_policy_version=_policy_revision(conn),
        expected_roster_revision=_roster_revision(conn),
        annotations=annotations,
    )


def _activate(conn: sqlite3.Connection, preview: dict, *, actor: str = "operator", reason: str = "adopt authority"):
    return fleet_policy_migration.activate_migration(
        conn,
        expected_policy_version=preview["sources"]["policy_revision"],
        expected_roster_revision=preview["sources"]["roster_revision"],
        preview_digest=preview["preview_digest"],
        actor=actor,
        reason=reason,
        annotations=preview.get("annotations") or _annotate(SEAT_ALPHA, SEAT_BETA),
    )


def _admit(
    conn: sqlite3.Connection, *, seat: str | None, request_id: str, expect: dict | None = None
) -> tuple[int, dict]:
    roster = expect or fleet_hub_model_roster.project_roster(conn, audience_node_id=NODE_A)
    return fleet_hub_model_roster.handle_model_policy(
        conn,
        {
            "action": "admit",
            "schema": fleet_model_roster.ADMISSION_REQUEST_SCHEMA,
            "consumer": "t3-fleet",
            "seat": seat,
            "request_id": request_id,
            "phase": "controller",
            "expect_revision": roster["revision"],
            "expect_digest": roster["document_sha256"],
        },
        caller_node=NODE_A,
    )


def _lease(
    conn: sqlite3.Connection,
    *,
    seat: str,
    provider: str,
    model: str,
    lease_id: str = "lease-alpha",
    extra: dict | None = None,
):
    body = {
        "action": "acquire",
        "lease_id": lease_id,
        "node_id": NODE_A,
        "holder": "holder-alpha",
        "seat": seat,
        "provider": provider,
        "model": model,
        "ttl_seconds": 60,
    }
    if extra:
        body.update(extra)
    return fleet_hub_model_roster.handle_model_policy(conn, body, caller_node=NODE_A)


def test_preview_is_read_only_and_does_not_activate(conn):
    _seed_legacy(conn)
    before = _dump(conn)
    preview = _preview(conn, annotations=_annotate(SEAT_ALPHA, SEAT_BETA))
    assert preview["ok"] is True
    assert preview["activated"] is False
    assert preview["compatibility_behavior"] == "legacy-writable"
    assert fleet_policy_migration.is_activated(conn) is False
    assert fleet_policy_migration.migration_status(conn)["schema"]["present"] is False
    assert _dump(conn) == before
    assert _policy_revision(conn) == 1
    assert (
        conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='fleet_policy_migration'").fetchone()
        is None
    )


def test_preview_maps_bindings_roles_and_keeps_consumer_fallback_off_impl(conn):
    _seed_legacy(conn)
    fleet_hub_policy.save_policy(conn, _authority_document(), expected_version=1, actor="operator", reason="seed")
    preview = _preview(conn, annotations=_annotate(SEAT_ALPHA, SEAT_BETA))
    candidate = preview["candidate"]
    alpha = candidate["seats"][SEAT_ALPHA]
    assert alpha["provider"] == PROVIDER_A
    assert alpha["model"] == MODEL_A
    assert alpha["effort"] == "high"
    assert alpha["concurrency"] == 3
    assert alpha["enabled"] is True
    assert alpha["bindings"]["brigade"]["cli"] == "cli-alpha"
    assert alpha["bindings"]["t3_fleet"]["instance_id"] == "inst-alpha"
    assert alpha["bindings"]["t3_fleet"]["service_tier"] == "standard"
    beta = candidate["seats"][SEAT_BETA]
    assert beta["enabled"] is False
    assert beta["effort"] == "none"
    assert beta["concurrency"] == 1
    assert candidate["defaults"]["roles"]["impl"] == SEAT_ALPHA
    assert candidate["defaults"]["roles"]["review"] == SEAT_BETA
    assert "admission_default" not in candidate["defaults"].get("roles", {})
    assert candidate["consumers"]["t3-fleet"]["default_patches"]["roles"]["admission_default"] == SEAT_ALPHA
    assert "admission_default" not in candidate["consumers"]["brigade-run"].get("default_patches", {}).get("roles", {})
    assert candidate["consumers"]["brigade-run"]["notes"] == "kept-brigade"
    assert candidate["consumers"]["t3-fleet"]["notes"] == "kept-t3"
    assert candidate["consumers"]["brigade-run"]["coverage"] == "unverified"
    assert candidate["machines"][MACHINE]["node_id"] == NODE_A
    assert REPO in candidate["repositories"]
    assert preview["affected"]["sessions"] == []
    assert "brigade-run" in preview["affected"]["consumers"]
    assert "t3-fleet" in preview["affected"]["consumers"]


def test_missing_classification_annotations_are_activation_blockers(conn):
    _seed_legacy(conn)
    preview = _preview(conn, annotations=None)
    assert preview["ok"] is False
    codes = [error["code"] for error in preview["errors"]]
    assert "missing_classification" in codes
    assert any(item["code"] == "unclassified-imported-seat" for item in preview["warnings"])
    with pytest.raises(FleetHubError, match="missing_classification"):
        fleet_policy_migration.activate_migration(
            conn,
            expected_policy_version=preview["sources"]["policy_revision"],
            expected_roster_revision=preview["sources"]["roster_revision"],
            preview_digest=preview["preview_digest"],
            actor="operator",
            reason="blocked",
            annotations=None,
        )
    assert fleet_policy_migration.is_activated(conn) is False
    assert _policy_revision(conn) == 1


def test_unknown_annotation_fields_are_rejected(conn):
    _seed_legacy(conn)
    preview = _preview(
        conn,
        annotations={"seats": {SEAT_ALPHA: {"pinned": False, "training_allowed": False, "speed": "fast"}}},
    )
    assert preview["ok"] is False
    assert any(error["code"] == "invalid_annotations" for error in preview["errors"])


def test_atomic_activation_writes_revision_and_marker_together(conn):
    _seed_legacy(conn)
    fleet_hub_policy.save_policy(conn, _authority_document(), expected_version=1, actor="operator", reason="seed")
    annotations = _annotate(SEAT_ALPHA, SEAT_BETA)
    preview = _preview(conn, annotations=annotations)
    assert preview["ok"] is True
    activated = _activate(conn, preview, reason="adopt authority")
    assert activated["activated"] is True
    assert fleet_policy_migration.is_activated(conn) is True
    current = fleet_hub_policy.current_policy(conn)
    assert current["revision"] == preview["sources"]["policy_revision"] + 1
    assert current["document"]["seats"][SEAT_ALPHA]["effort"] == "high"
    row = conn.execute(
        "SELECT activated, preview_digest, policy_revision, roster_revision FROM fleet_policy_migration WHERE singleton=1"
    ).fetchone()
    assert int(row[0]) == 1
    assert row[1] == preview["preview_digest"]
    assert int(row[2]) == current["revision"]
    assert int(row[3]) == preview["sources"]["roster_revision"]
    status = fleet_policy_migration.migration_status(conn)
    assert status["schema"]["present"] is True
    assert status["activated"] is True
    assert status["compatibility_behavior"] == "authority-owned"


def test_activation_is_compare_and_swap_on_both_revisions(conn):
    _seed_legacy(conn)
    annotations = _annotate(SEAT_ALPHA, SEAT_BETA)
    preview = _preview(conn, annotations=annotations)
    with pytest.raises(FleetHubConflict, match="policy_revision_conflict"):
        fleet_policy_migration.activate_migration(
            conn,
            expected_policy_version=preview["sources"]["policy_revision"] - 1,
            expected_roster_revision=preview["sources"]["roster_revision"],
            preview_digest=preview["preview_digest"],
            actor="operator",
            reason="stale policy",
            annotations=annotations,
        )
    with pytest.raises(FleetHubConflict, match="roster_revision_conflict"):
        fleet_policy_migration.activate_migration(
            conn,
            expected_policy_version=preview["sources"]["policy_revision"],
            expected_roster_revision=preview["sources"]["roster_revision"] - 1,
            preview_digest=preview["preview_digest"],
            actor="operator",
            reason="stale roster",
            annotations=annotations,
        )
    assert fleet_policy_migration.is_activated(conn) is False
    fleet_hub_policy.save_policy(conn, _authority_document(), expected_version=1, actor="operator", reason="drift")
    with pytest.raises(FleetHubConflict, match="policy_revision_conflict"):
        fleet_policy_migration.activate_migration(
            conn,
            expected_policy_version=preview["sources"]["policy_revision"],
            expected_roster_revision=_roster_revision(conn),
            preview_digest=preview["preview_digest"],
            actor="operator",
            reason="policy moved",
            annotations=annotations,
        )
    _set_seat(conn, seat=SEAT_GAMMA, provider=PROVIDER_A, model="model-c-1", brigade_cli="cli-gamma")
    with pytest.raises(FleetHubConflict, match="roster_revision_conflict"):
        fleet_policy_migration.activate_migration(
            conn,
            expected_policy_version=_policy_revision(conn),
            expected_roster_revision=preview["sources"]["roster_revision"],
            preview_digest=preview["preview_digest"],
            actor="operator",
            reason="roster moved",
            annotations=_annotate(SEAT_ALPHA, SEAT_BETA, SEAT_GAMMA),
        )


def test_preference_drift_invalidates_preview_digest(conn):
    _seed_legacy(conn)
    annotations = _annotate(SEAT_ALPHA, SEAT_BETA)
    preview = _preview(conn, annotations=annotations)
    fleet_hub_preference.set_run_preference(conn, {"impl": SEAT_BETA}, updated_by="admin")
    assert _roster_revision(conn) == preview["sources"]["roster_revision"]
    drifted = _preview(conn, annotations=annotations)
    assert drifted["preview_digest"] != preview["preview_digest"]
    with pytest.raises(FleetHubConflict, match="preview_digest_conflict"):
        fleet_policy_migration.activate_migration(
            conn,
            expected_policy_version=_policy_revision(conn),
            expected_roster_revision=_roster_revision(conn),
            preview_digest=preview["preview_digest"],
            actor="operator",
            reason="stale digest",
            annotations=annotations,
        )
    assert fleet_policy_migration.is_activated(conn) is False


def test_repeated_activation_is_refused(conn):
    _seed_legacy(conn)
    annotations = _annotate(SEAT_ALPHA, SEAT_BETA)
    preview = _preview(conn, annotations=annotations)
    first = _activate(conn, preview)
    assert first["activated"] is True
    with pytest.raises(FleetHubError, match="already_activated"):
        fleet_policy_migration.activate_migration(
            conn,
            expected_policy_version=_policy_revision(conn),
            expected_roster_revision=_roster_revision(conn),
            preview_digest=preview["preview_digest"],
            actor="operator",
            reason="again",
            annotations=annotations,
        )
    later = _preview(conn, annotations=annotations)
    assert later["activated"] is True
    assert later["ok"] is False
    assert any(error["code"] == "already_activated" for error in later["errors"])


def test_activation_does_not_commit_caller_transaction_and_rolls_back_on_failure(conn):
    _seed_legacy(conn)
    annotations = _annotate(SEAT_ALPHA, SEAT_BETA)
    preview = _preview(conn, annotations=annotations)
    conn.execute("BEGIN")
    assert conn.in_transaction is True
    fleet_policy_migration.activate_migration(
        conn,
        expected_policy_version=preview["sources"]["policy_revision"],
        expected_roster_revision=preview["sources"]["roster_revision"],
        preview_digest=preview["preview_digest"],
        actor="operator",
        reason="caller owns commit",
        annotations=annotations,
    )
    assert conn.in_transaction is True
    conn.rollback()
    assert fleet_policy_migration.is_activated(conn) is False
    assert _policy_revision(conn) == 1

    def boom(*_args, **_kwargs):
        raise RuntimeError("save exploded")

    monkey = fleet_hub_policy.save_policy
    try:
        fleet_hub_policy.save_policy = boom  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="save exploded"):
            fleet_policy_migration.activate_migration(
                conn,
                expected_policy_version=preview["sources"]["policy_revision"],
                expected_roster_revision=preview["sources"]["roster_revision"],
                preview_digest=preview["preview_digest"],
                actor="operator",
                reason="fail closed",
                annotations=annotations,
            )
    finally:
        fleet_hub_policy.save_policy = monkey  # type: ignore[method-assign]
    assert fleet_policy_migration.is_activated(conn) is False
    assert _policy_revision(conn) == 1
    assert (
        conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='fleet_policy_migration'").fetchone()
        is None
    )
    assert conn.in_transaction is False


def test_projected_reads_preserve_bindings_effort_concurrency_and_disable(conn):
    _seed_legacy(conn)
    preview = _preview(conn, annotations=_annotate(SEAT_ALPHA, SEAT_BETA))
    _activate(conn, preview)
    roster = fleet_hub_model_roster.project_roster(conn)
    seats = {item["seat"]: item for item in roster["seats"]}
    assert seats[SEAT_ALPHA]["reasoning"] == "high"
    assert seats[SEAT_ALPHA]["limit"] == 3
    assert seats[SEAT_ALPHA]["enabled"] is True
    assert seats[SEAT_ALPHA]["bindings"]["brigade"]["cli"] == "cli-alpha"
    assert seats[SEAT_ALPHA]["bindings"]["t3_fleet"] == {"instance_id": "inst-alpha", "service_tier": "standard"}
    assert seats[SEAT_BETA]["enabled"] is False
    assert seats[SEAT_BETA]["reasoning"] == "none"
    assert seats[SEAT_BETA]["limit"] == 1
    assert roster["consumer_defaults"]["t3-fleet"] == SEAT_ALPHA
    assert roster["consumer_defaults"]["brigade-run"] is None
    pref = fleet_hub_preference.get_run_preference(conn)
    assert pref["impl"] == SEAT_ALPHA
    assert pref["review"] == SEAT_BETA
    assert fleet_model_roster.roster_digest(roster) == roster["document_sha256"]
    signed = fleet_hub_model_roster.project_roster(
        conn, audience_node_id=NODE_A, raw_node_bearer="node-bearer-for-roster-mac"
    )
    assert signed["mac"]["algorithm"] == fleet_model_roster.MAC_ALGORITHM
    assert signed["mac"]["value"] == fleet_model_roster.roster_mac("node-bearer-for-roster-mac", signed)


def test_hard_pinned_seat_conflict_blocks_until_explicit_unpin(conn):
    _seed_legacy(conn)
    pinned = _authority_document()
    pinned["seats"] = {
        SEAT_ALPHA: {
            "provider": PROVIDER_A,
            "model": MODEL_A,
            "effort": "high",
            "pinned": True,
            "enabled": True,
        }
    }
    fleet_hub_policy.save_policy(conn, pinned, expected_version=1, actor="operator", reason="pin")
    conn.execute("UPDATE model_policy SET model=? WHERE seat=?", (MODEL_A_DRIFT, SEAT_ALPHA))
    conn.commit()
    preview = _preview(conn, annotations=_annotate(SEAT_BETA))
    assert preview["ok"] is False
    assert any(error["code"] in {"pinned-seat-change", "unpin-and-change"} for error in preview["errors"])
    with pytest.raises(FleetHubError, match="pinned-seat-change|unpin-and-change"):
        fleet_policy_migration.activate_migration(
            conn,
            expected_policy_version=preview["sources"]["policy_revision"],
            expected_roster_revision=preview["sources"]["roster_revision"],
            preview_digest=preview["preview_digest"],
            actor="operator",
            reason="blocked pin",
            annotations=_annotate(SEAT_BETA),
        )
    unpinned = copy.deepcopy(pinned)
    unpinned["seats"][SEAT_ALPHA]["pinned"] = False
    fleet_hub_policy.save_policy(conn, unpinned, expected_version=2, actor="operator", reason="unpin")
    ready = _preview(conn, annotations=_annotate(SEAT_ALPHA, SEAT_BETA))
    assert ready["ok"] is True
    _activate(conn, ready)
    assert fleet_hub_policy.current_policy(conn)["document"]["seats"][SEAT_ALPHA]["model"] == MODEL_A_DRIFT


def test_training_classification_is_retained_from_authority(conn):
    _seed_legacy(conn)
    trained = _authority_document()
    trained["seats"] = {
        SEAT_ALPHA: {
            "provider": PROVIDER_A,
            "model": MODEL_A,
            "effort": "high",
            "training_allowed": True,
            "pinned": False,
            "enabled": True,
        }
    }
    fleet_hub_policy.save_policy(conn, trained, expected_version=1, actor="operator", reason="classify")
    preview = _preview(conn, annotations=_annotate(SEAT_BETA))
    assert preview["ok"] is True
    assert preview["candidate"]["seats"][SEAT_ALPHA]["training_allowed"] is True
    _activate(conn, preview)
    assert fleet_hub_policy.current_policy(conn)["document"]["seats"][SEAT_ALPHA]["training_allowed"] is True


def test_annotation_cannot_implicitly_weaken_a_pin(conn):
    _seed_legacy(conn)
    pinned = _authority_document()
    pinned["seats"] = {
        SEAT_ALPHA: {
            "provider": PROVIDER_A,
            "model": MODEL_A,
            "effort": "high",
            "pinned": True,
            "training_allowed": False,
        }
    }
    fleet_hub_policy.save_policy(conn, pinned, expected_version=1, actor="operator", reason="pin")
    preview = _preview(
        conn,
        annotations={
            "seats": {
                SEAT_ALPHA: {"pinned": False, "training_allowed": False},
                SEAT_BETA: {"pinned": False, "training_allowed": False},
            }
        },
    )
    assert preview["ok"] is False
    assert any(error["code"] == "pin-weakened" for error in preview["errors"])


def test_consumer_fallback_is_distinct_from_global_impl(conn):
    _set_seat(conn, seat=SEAT_ALPHA, provider=PROVIDER_A, model=MODEL_A)
    _set_seat(
        conn,
        seat=SEAT_BETA,
        provider=PROVIDER_B,
        model=MODEL_B,
        reasoning="none",
        enabled=False,
        limit=1,
        brigade_cli="cli-beta",
        t3_instance_id="inst-beta",
        t3_service_tier="premium",
    )
    fleet_hub_preference.set_run_preference(conn, {"impl": SEAT_ALPHA, "review": SEAT_BETA}, updated_by="admin")
    preview = _preview(conn, annotations=_annotate(SEAT_ALPHA, SEAT_BETA))
    assert preview["candidate"]["defaults"]["roles"]["impl"] == SEAT_ALPHA
    assert "admission_default" not in preview["candidate"]["consumers"]["t3-fleet"].get("default_patches", {}).get(
        "roles", {}
    )
    _activate(conn, preview)
    status, missing = _admit(conn, seat=None, request_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    assert status == 409
    assert missing["error"] == "default-missing"
    status, explicit = _admit(conn, seat=SEAT_ALPHA, request_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
    assert status == 200
    assert explicit["seat"] == SEAT_ALPHA
    assert explicit["reasoning"] == "high"
    assert explicit["binding"] == {"instance_id": "inst-alpha", "service_tier": "standard"}


def test_active_reads_follow_later_policy_revision_edits(conn):
    _seed_legacy(conn)
    preview = _preview(conn, annotations=_annotate(SEAT_ALPHA, SEAT_BETA))
    _activate(conn, preview)
    current = fleet_hub_policy.current_policy(conn)
    edited = copy.deepcopy(current["document"])
    edited["seats"][SEAT_ALPHA]["model"] = MODEL_A_DRIFT
    edited["seats"][SEAT_ALPHA]["concurrency"] = 7
    edited["seats"][SEAT_ALPHA]["enabled"] = False
    edited["defaults"]["roles"]["impl"] = SEAT_BETA
    fleet_hub_policy.save_policy(
        conn, edited, expected_version=current["revision"], actor="operator", reason="edit after adoption"
    )
    roster = fleet_hub_model_roster.project_roster(conn)
    alpha = next(item for item in roster["seats"] if item["seat"] == SEAT_ALPHA)
    assert alpha["model"] == MODEL_A_DRIFT
    assert alpha["limit"] == 7
    assert alpha["enabled"] is False
    assert roster["revision"] == current["revision"] + 1
    assert fleet_hub_preference.get_run_preference(conn)["impl"] == SEAT_BETA
    status, denied = _admit(conn, seat=SEAT_ALPHA, request_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc")
    assert status == 409
    assert denied["error"] == "seat-disabled"
    assert denied["model"] == MODEL_A_DRIFT


def test_legacy_mutations_are_blocked_through_every_entrypoint(conn):
    _seed_legacy(conn)
    preview = _preview(conn, annotations=_annotate(SEAT_ALPHA, SEAT_BETA))
    _activate(conn, preview)
    raw = {
        "action": "set",
        "expected_revision": _roster_revision(conn),
        "seat": SEAT_GAMMA,
        "provider": PROVIDER_A,
        "model": "model-c-1",
        "reasoning": "high",
        "enabled": True,
        "limit": 1,
        "brigade_cli": "cli-gamma",
        "t3_instance_id": "inst-gamma",
        "t3_service_tier": "standard",
        "notes": "",
    }
    status, payload = fleet_hub_model_roster.handle_model_policy(conn, raw)
    assert status == 409
    assert payload["error"] == "authority_owned"
    with pytest.raises(FleetHubConflict, match="authority_owned"):
        fleet_hub_model_roster.set_model_policy(conn, raw)
    with pytest.raises(FleetHubError, match="authority_owned"):
        fleet_hub_model_roster._write_set(
            conn,
            {
                "seat": SEAT_GAMMA,
                "provider": PROVIDER_A,
                "model": "model-c-1",
                "reasoning": "high",
                "enabled": True,
                "limit": 1,
                "brigade_cli": "cli-gamma",
                "t3_instance_id": "inst-gamma",
                "t3_service_tier": "standard",
                "notes": "",
            },
        )
    status, defaulted = fleet_hub_model_roster.handle_model_policy(
        conn,
        {
            "action": "set-default",
            "expected_revision": _roster_revision(conn),
            "consumer": "brigade-run",
            "seat": SEAT_ALPHA,
        },
    )
    assert status == 409
    assert defaulted["error"] == "authority_owned"
    status, retired = fleet_hub_model_roster.handle_model_policy(
        conn,
        {
            "action": "retire",
            "expected_revision": _roster_revision(conn),
            "provider": PROVIDER_B,
            "family": "model-z",
            "permanent": False,
            "reason_code": "operator-retired",
        },
    )
    assert status == 409
    assert retired["error"] == "authority_owned"
    with pytest.raises(FleetHubError, match="authority_owned"):
        fleet_hub_preference.upsert_run_preference(conn, {"impl": SEAT_BETA}, updated_by="admin")
    with pytest.raises(FleetHubError, match="authority_owned"):
        fleet_hub_preference.set_run_preference(conn, {"impl": SEAT_BETA}, updated_by="admin")
    with pytest.raises(sqlite3.IntegrityError, match="authority_owned"):
        conn.execute(
            "INSERT INTO model_consumer_defaults (consumer, seat, updated_at) VALUES ('t3-fleet', 'seat-beta', 'now')"
        )
    assert conn.execute("SELECT model FROM model_policy WHERE seat=?", (SEAT_ALPHA,)).fetchone()[0] == MODEL_A
    assert fleet_hub_policy.current_policy(conn)["document"]["defaults"]["roles"]["impl"] == SEAT_ALPHA


def test_admission_and_leases_cannot_use_removed_disabled_or_drifted_seats(conn):
    _seed_legacy(conn)
    preview = _preview(conn, annotations=_annotate(SEAT_ALPHA, SEAT_BETA))
    _activate(conn, preview)
    current = fleet_hub_policy.current_policy(conn)
    edited = copy.deepcopy(current["document"])
    edited["seats"][SEAT_ALPHA]["model"] = MODEL_A_DRIFT
    edited["seats"][SEAT_BETA]["enabled"] = False
    del edited["seats"][SEAT_BETA]
    fleet_hub_policy.save_policy(
        conn, edited, expected_version=current["revision"], actor="operator", reason="remove and drift"
    )
    status, missing = _admit(conn, seat=SEAT_BETA, request_id="dddddddd-dddd-4ddd-8ddd-dddddddddddd")
    assert status == 409
    assert missing["error"] == "seat-missing"
    status, drifted = _lease(conn, seat=SEAT_ALPHA, provider=PROVIDER_A, model=MODEL_A, lease_id="lease-old")
    assert status == 409
    assert drifted["acquired"] is False
    status, prepared = fleet_hub_policy_api.handle_policy(
        conn,
        {
            "action": "prepare",
            "consumer": "brigade-run",
            "repo_identity": REPO,
            "session_id": "session-lease-ok",
            "origin": "local",
            "provider": PROVIDER_A,
            "model": MODEL_A_DRIFT,
            "instance_id": "cli-alpha",
        },
        caller_node=NODE_A,
        is_admin=False,
    )
    assert status == 200, prepared
    status, ack = fleet_hub_policy_api.handle_policy(
        conn,
        {
            "action": "acknowledge",
            "consumer": "brigade-run",
            "repo_identity": REPO,
            "session_id": "session-lease-ok",
            "version": prepared["version"],
            "digest": prepared["digest"],
            "status": "applied",
            "context_hash": prepared["context_hash"],
        },
        caller_node=NODE_A,
        is_admin=False,
    )
    assert status == 200, ack
    proof = {
        "policy_session_id": "session-lease-ok",
        "policy_version": prepared["version"],
        "policy_digest": prepared["digest"],
        "repo_identity": REPO,
        "policy_context_hash": prepared["context_hash"],
    }
    status, ack_only = _lease(
        conn,
        seat=SEAT_ALPHA,
        provider=PROVIDER_A,
        model=MODEL_A_DRIFT,
        lease_id="lease-ack-only",
        extra=dict(proof, request_id=LAUNCH_REQUEST),
    )
    assert status == 409
    assert ack_only["error"] == "policy-proof-missing"
    roster = fleet_hub_model_roster.project_roster(conn, audience_node_id=NODE_A)
    status, launched = fleet_hub_model_roster.handle_model_policy(
        conn,
        {
            "action": "admit",
            "schema": fleet_model_roster.ADMISSION_REQUEST_SCHEMA,
            "consumer": "brigade-run",
            "seat": SEAT_ALPHA,
            "request_id": LAUNCH_REQUEST,
            "phase": "launch",
            "expect_revision": roster["revision"],
            "expect_digest": roster["document_sha256"],
            **proof,
        },
        caller_node=NODE_A,
    )
    assert status == 200, launched
    status, ok = _lease(
        conn,
        seat=SEAT_ALPHA,
        provider=PROVIDER_A,
        model=MODEL_A_DRIFT,
        lease_id="lease-new",
        extra=dict(proof, request_id=LAUNCH_REQUEST),
    )
    assert status == 200, ok
    assert ok["acquired"] is True
    status, gone = _lease(conn, seat=SEAT_BETA, provider=PROVIDER_B, model=MODEL_B, lease_id="lease-beta")
    assert status == 409
    assert gone["acquired"] is False


def test_retired_guards_remain_after_activation(conn):
    _seed_legacy(conn)
    _set_seat(
        conn,
        seat=SEAT_GAMMA,
        provider=PROVIDER_A,
        model="model-retired-1",
        reasoning="none",
        brigade_cli="cli-gamma",
        t3_instance_id="inst-gamma",
        t3_service_tier="standard",
    )
    _retire(conn, PROVIDER_A, "model-retired")
    preview = _preview(conn, annotations=_annotate(SEAT_ALPHA, SEAT_BETA, SEAT_GAMMA))
    _activate(conn, preview)
    roster = fleet_hub_model_roster.project_roster(conn)
    families = {(row["provider"], row["family"]) for row in roster["retired_models"]}
    assert ("openai", "gpt-5.4") in families
    assert ("openai", "gpt-5.5") in families
    assert (PROVIDER_A, "model-retired") in families
    status, denied = _admit(conn, seat=SEAT_GAMMA, request_id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee")
    assert status == 409
    assert denied["error"] == "retired-model"
    status, leased = _lease(
        conn, seat=SEAT_GAMMA, provider=PROVIDER_A, model="model-retired-1", lease_id="lease-retired"
    )
    assert status == 409
    assert leased["acquired"] is False


def test_legacy_behavior_is_unchanged_before_activation(conn):
    _seed_legacy(conn)
    before = fleet_hub_model_roster.project_roster(conn)
    assert before["revision"] == _roster_revision(conn)
    status, payload = fleet_hub_model_roster.handle_model_policy(
        conn,
        {
            "action": "set",
            "expected_revision": _roster_revision(conn),
            "seat": SEAT_GAMMA,
            "provider": PROVIDER_A,
            "model": "model-c-1",
            "reasoning": "high",
            "enabled": True,
            "limit": 2,
            "brigade_cli": "cli-gamma",
            "t3_instance_id": "inst-gamma",
            "t3_service_tier": "standard",
            "notes": "",
        },
    )
    assert status == 200
    stored = fleet_hub_preference.set_run_preference(conn, {"impl": SEAT_GAMMA}, updated_by="admin")
    assert stored["impl"] == SEAT_GAMMA
    assert fleet_policy_migration.is_activated(conn) is False


def test_overlay_preserves_non_legacy_metadata_including_cost_class():
    mapped = {
        "provider": "provider-a",
        "model": "model-a-1",
        "concurrency": 1,
        "enabled": True,
        "bindings": {"brigade": {"cli": "cli-alpha"}},
        "pinned": False,
        "training_allowed": False,
    }
    current = {
        "provider": "provider-a",
        "model": "model-a-1",
        "cost_class": "standard",
        "allow_free": False,
        "eligible_machines": ["worker-linux-1"],
        "notes": "kept",
        "pinned": True,
        "training_allowed": True,
        "bindings": {"brigade": {"cli": "old", "model": "native-a"}, "native": {"model": "native-a"}},
    }
    merged = fleet_policy_migration._overlay_current_seat(mapped, current)
    assert merged["cost_class"] == "standard"
    assert merged["allow_free"] is False
    assert merged["eligible_machines"] == ["worker-linux-1"]
    assert merged["pinned"] is True
    assert merged["training_allowed"] is True
    assert merged["bindings"]["brigade"]["cli"] == "cli-alpha"
    assert merged["bindings"]["brigade"]["model"] == "native-a"


def test_classification_overlay_requires_exact_provider_model_identity(conn):
    _set_seat(conn, seat=SEAT_ALPHA, provider=PROVIDER_A, model=MODEL_A)
    trained = _authority_document()
    trained["seats"] = {
        SEAT_ALPHA: {
            "provider": PROVIDER_A,
            "model": MODEL_A,
            "effort": "high",
            "training_allowed": True,
            "pinned": True,
            "enabled": True,
        }
    }
    fleet_hub_policy.save_policy(conn, trained, expected_version=1, actor="operator", reason="classify")
    conn.execute(
        "UPDATE model_policy SET provider=?, model=? WHERE seat=?",
        ("provider-contrib", "model-contrib-train-1", SEAT_ALPHA),
    )
    conn.commit()
    preview = _preview(conn, annotations=None)
    assert preview["ok"] is False
    assert any(
        error["code"] == "missing_classification" and error.get("seat") == SEAT_ALPHA for error in preview["errors"]
    )
    candidate_seat = preview["candidate"]["seats"][SEAT_ALPHA]
    assert candidate_seat["provider"] == "provider-contrib"
    assert candidate_seat["model"] == "model-contrib-train-1"
    assert candidate_seat.get("training_allowed") is not True
    assert candidate_seat.get("pinned") is not True


def test_unbounded_legacy_limit_is_warned_not_called_preserved(conn):
    _set_seat(conn, seat=SEAT_ALPHA, provider=PROVIDER_A, model=MODEL_A, limit=None)
    preview = _preview(conn, annotations=_annotate(SEAT_ALPHA))
    warning = next(item for item in preview["warnings"] if item["code"] == "unbounded-limit-mapped")
    assert warning["seat"] == SEAT_ALPHA
    assert warning["before"] is None
    assert warning["after"] == 1
    assert preview["candidate"]["seats"][SEAT_ALPHA]["concurrency"] == 1
    reviewed = _preview(conn, annotations=_annotate(SEAT_ALPHA, concurrency=4))
    assert not any(item["code"] == "unbounded-limit-mapped" for item in reviewed["warnings"])
    assert reviewed["candidate"]["seats"][SEAT_ALPHA]["concurrency"] == 4


def test_candidate_deepcopy_keeps_unrelated_policy_sections(conn):
    _seed_legacy(conn)
    doc = _authority_document()
    doc["defaults"]["data"]["retention"] = "keep-30d"
    doc["defaults"]["execution"] = {"concurrency": 9}
    doc["machines"][MACHINE]["capabilities"] = ["general"]
    fleet_hub_policy.save_policy(conn, doc, expected_version=1, actor="operator", reason="seed")
    preview = _preview(conn, annotations=_annotate(SEAT_ALPHA, SEAT_BETA))
    candidate = preview["candidate"]
    assert candidate["defaults"]["data"]["retention"] == "keep-30d"
    assert candidate["defaults"]["data"]["allow_training"] is False
    assert candidate["defaults"]["execution"]["concurrency"] == 9
    assert candidate["machines"][MACHINE]["capabilities"] == ["general"]
    assert candidate["machines"][MACHINE]["node_id"] == NODE_A
    assert candidate["repositories"][REPO]["privacy"] == "public"


def test_activated_roster_carries_signed_authority_metadata(conn):
    _seed_legacy(conn)
    preview = _preview(conn, annotations=_annotate(SEAT_ALPHA, SEAT_BETA))
    _activate(conn, preview)
    current = fleet_hub_policy.current_policy(conn)
    roster = fleet_hub_model_roster.project_roster(
        conn, audience_node_id=NODE_A, raw_node_bearer="node-bearer-for-roster-mac"
    )
    assert roster["fleet_policy"] == {
        "active": True,
        "version": current["revision"],
        "digest": current["digest"],
    }
    assert roster["document_sha256"] == fleet_model_roster.roster_digest(roster)
    assert roster["mac"]["value"] == fleet_model_roster.roster_mac("node-bearer-for-roster-mac", roster)


def test_unactivated_roster_omits_authority_metadata_and_keeps_mac(conn):
    _seed_legacy(conn)
    roster = fleet_hub_model_roster.project_roster(
        conn, audience_node_id=NODE_A, raw_node_bearer="node-bearer-for-roster-mac"
    )
    assert "fleet_policy" not in roster
    assert roster["document_sha256"] == fleet_model_roster.roster_digest(roster)
    assert roster["mac"]["value"] == fleet_model_roster.roster_mac("node-bearer-for-roster-mac", roster)


def test_preference_meta_follows_policy_revision_after_activation(conn):
    _seed_legacy(conn)
    fleet_hub_preference.set_run_preference(conn, {"impl": SEAT_ALPHA}, updated_by="legacy-admin")
    legacy_meta = fleet_hub_preference.get_run_preference_meta(conn)
    assert legacy_meta["updated_by"] == "legacy-admin"
    preview = _preview(conn, annotations=_annotate(SEAT_ALPHA, SEAT_BETA))
    _activate(conn, preview, actor="operator")
    current = fleet_hub_policy.current_policy(conn)
    meta = fleet_hub_preference.get_run_preference_meta(conn)
    assert meta["updated_at"] == current["created_at"]
    assert meta["updated_by"] == current["actor"]
    edited = copy.deepcopy(current["document"])
    edited["defaults"]["roles"]["impl"] = SEAT_BETA
    fleet_hub_policy.save_policy(
        conn, edited, expected_version=current["revision"], actor="policy-editor", reason="advance meta"
    )
    later = fleet_hub_preference.get_run_preference_meta(conn)
    advanced = fleet_hub_policy.current_policy(conn)
    assert later["updated_at"] == advanced["created_at"]
    assert later["updated_by"] == "policy-editor"
    assert later["updated_at"] != meta["updated_at"]


def test_control_plane_status_projects_real_activation_state(conn):
    from brigade import fleet_hub_routing

    _seed_legacy(conn)
    before = fleet_hub_routing.control_plane_status(conn)["authority"]
    assert before == {"active": False, "status": "inactive"}
    preview = _preview(conn, annotations=_annotate(SEAT_ALPHA, SEAT_BETA))
    _activate(conn, preview)
    assert fleet_policy_migration.is_activated(conn) is True
    after = fleet_hub_routing.control_plane_status(conn)["authority"]
    assert after == {"active": True, "status": "active"}
