"""Tests for fleet policy core completion:

1. Transitive dependency expansion in preview _affected.
2. Explicit data.allow_free, seat cost_class, and OpenCode Contributor Free.
3. Acknowledged session snapshot storage, projection, and history persistence.
"""

from __future__ import annotations

import copy
import hashlib
import sqlite3
import threading
from typing import Any

import pytest

from brigade import fleet_hub, fleet_hub_policy, fleet_policy
from brigade.fleet_hub import FleetHubError, FleetHubForbidden
from brigade.fleet_policy import FleetPolicyError


NODE_A = "11111111-1111-4111-8111-111111111111"
NODE_B = "22222222-2222-4222-8222-222222222222"


@pytest.fixture()
def conn(tmp_path):
    connection = fleet_hub.init_db(tmp_path / "fleet.db")
    try:
        yield connection
    finally:
        connection.close()


def _doc(**kwargs: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema": fleet_policy.POLICY_SCHEMA,
        "defaults": {
            "roles": {"impl": "seat-alpha"},
            "data": {"allow_training": False, "allow_free": False},
        },
        "machines": {"worker-linux-1": {"os": "linux", "concurrency": 2}},
        "seats": {
            "seat-alpha": {"provider": "provider-a", "model": "model-a-1"},
            "seat-beta": {"provider": "provider-b", "model": "model-b-1"},
        },
        "consumers": {
            "consumer-one": {"reload": "refreshable"},
            "consumer-two": {"reload": "restart-required"},
        },
        "repositories": {"repo/public": {"privacy": "public"}},
    }
    base.update(kwargs)
    return base


def _preview(conn: sqlite3.Connection, proposed: dict[str, Any]) -> dict[str, Any]:
    current = fleet_hub_policy.current_policy(conn)
    return fleet_hub_policy.preview_policy(
        conn, proposed, expected_version=current["revision"], actor="tester", reason="preview"
    )


def _prepare(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    consumer: str = "consumer-one",
    node_id: str = NODE_A,
    repo_identity: str | None = "repo/public",
    origin: str = "test",
    overrides: dict[str, Any] | None = None,
    override_reason: str | None = None,
    instructions: str | None = None,
    context_hash: str | None = None,
) -> dict[str, Any]:
    current = fleet_hub_policy.current_policy(conn)
    effective = fleet_policy.resolve_policy(
        current["document"],
        consumer=consumer,
        repo_identity=repo_identity,
        overrides=overrides,
    )
    if context_hash is None:
        material = f"{session_id}:{consumer}:{repo_identity}:{current['revision']}:{current['digest']}:{overrides}:{instructions}"
        context_hash = "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()
    return fleet_hub_policy.record_pending_policy(
        conn,
        session_id=session_id,
        consumer=consumer,
        node_id=node_id,
        repo_identity=repo_identity,
        origin=origin,
        revision=current["revision"],
        digest=current["digest"],
        overrides=overrides or {},
        override_reason=override_reason,
        effective=effective,
        sources={"defaults": "policy"},
        instructions=instructions,
        context_hash=context_hash,
    )


# =============================================================================
# Part 1: _affected dependency expansion
# =============================================================================


def test_affected_detects_model_only_change(conn):
    doc1 = _doc(
        seats={
            "seat-alpha": {"provider": "provider-a", "model": "model-1"},
            "seat-unused": {"provider": "provider-u", "model": "model-u"},
        },
    )
    fleet_hub_policy.save_policy(conn, doc1, expected_version=1, actor="tester", reason="doc1")
    doc2 = copy.deepcopy(doc1)
    doc2["seats"]["seat-alpha"]["model"] = "model-2"
    affected = _preview(conn, doc2)["affected"]
    assert "consumer-one" in affected["consumers"]


def test_affected_detects_provider_only_change(conn):
    doc1 = _doc(
        seats={"seat-alpha": {"provider": "provider-a", "model": "model-1"}},
    )
    fleet_hub_policy.save_policy(conn, doc1, expected_version=1, actor="tester", reason="doc1")
    doc2 = copy.deepcopy(doc1)
    doc2["seats"]["seat-alpha"]["provider"] = "provider-b"
    affected = _preview(conn, doc2)["affected"]
    assert "consumer-one" in affected["consumers"]


def test_affected_detects_effort_change(conn):
    doc1 = _doc(
        seats={"seat-alpha": {"provider": "provider-a", "model": "model-1", "effort": "medium"}},
    )
    fleet_hub_policy.save_policy(conn, doc1, expected_version=1, actor="tester", reason="doc1")
    doc2 = copy.deepcopy(doc1)
    doc2["seats"]["seat-alpha"]["effort"] = "high"
    affected = _preview(conn, doc2)["affected"]
    assert "consumer-one" in affected["consumers"]


def test_affected_detects_bindings_change(conn):
    doc1 = _doc(
        seats={
            "seat-alpha": {
                "provider": "provider-a",
                "model": "model-1",
                "bindings": {"native": {"model": "native-1"}},
            }
        },
    )
    fleet_hub_policy.save_policy(conn, doc1, expected_version=1, actor="tester", reason="doc1")
    doc2 = copy.deepcopy(doc1)
    doc2["seats"]["seat-alpha"]["bindings"] = {"native": {"model": "native-2"}}
    affected = _preview(conn, doc2)["affected"]
    assert "consumer-one" in affected["consumers"]


def test_affected_detects_fallback_change(conn):
    doc1 = _doc(
        seats={
            "seat-alpha": {"provider": "provider-a", "model": "model-1", "fallback": ["seat-beta"]},
            "seat-beta": {"provider": "provider-b", "model": "model-b-1"},
        },
    )
    fleet_hub_policy.save_policy(conn, doc1, expected_version=1, actor="tester", reason="doc1")
    doc2 = copy.deepcopy(doc1)
    doc2["seats"]["seat-beta"]["model"] = "model-b-2"
    affected = _preview(conn, doc2)["affected"]
    assert "consumer-one" in affected["consumers"]


def test_affected_detects_machine_override_change(conn):
    doc1 = _doc(
        machines={"worker-linux-1": {"os": "linux", "priority": 100}},
        consumers={"consumer-one": {"default_patches": {"execution": {"machine": "worker-linux-1"}}}},
    )
    fleet_hub_policy.save_policy(conn, doc1, expected_version=1, actor="tester", reason="doc1")
    doc2 = copy.deepcopy(doc1)
    doc2["machines"]["worker-linux-1"]["priority"] = 200
    affected = _preview(conn, doc2)["affected"]
    assert "consumer-one" in affected["consumers"]


def test_affected_leaves_consumers_empty_for_unused_seat_edit(conn):
    doc1 = _doc(
        seats={
            "seat-alpha": {"provider": "provider-a", "model": "model-1"},
            "seat-unused": {"provider": "provider-u", "model": "model-u-1"},
        },
        consumers={"consumer-one": {"reload": "refreshable"}},
    )
    fleet_hub_policy.save_policy(conn, doc1, expected_version=1, actor="tester", reason="doc1")
    doc2 = copy.deepcopy(doc1)
    doc2["seats"]["seat-unused"]["model"] = "model-u-2"
    affected = _preview(conn, doc2)["affected"]
    assert affected["consumers"] == []


def test_affected_unchanged_document(conn):
    doc1 = _doc()
    fleet_hub_policy.save_policy(conn, doc1, expected_version=1, actor="tester", reason="doc1")
    affected = _preview(conn, doc1)["affected"]
    assert affected["consumers"] == []
    assert affected["repositories"] == []
    assert affected["sessions"] == []


def test_affected_sparse_inheritance_and_deleted_roles_and_consumers(conn):
    doc1 = _doc(
        defaults={"roles": {"impl": "seat-alpha", "review": "seat-beta"}},
        consumers={
            "consumer-one": {"default_patches": {"roles": {"impl": "seat-beta"}}},
            "consumer-two": {"default_patches": {"roles": {"review": "seat-alpha"}}},
        },
    )
    fleet_hub_policy.save_policy(conn, doc1, expected_version=1, actor="tester", reason="doc1")
    # Delete consumer-two
    doc2 = copy.deepcopy(doc1)
    del doc2["consumers"]["consumer-two"]
    affected = _preview(conn, doc2)["affected"]
    assert "consumer-two" in affected["consumers"]


def test_affected_fallback_cycles_bounded(conn):
    doc1 = _doc(
        seats={
            "seat-alpha": {"provider": "provider-a", "model": "m", "fallback": ["seat-beta"]},
            "seat-beta": {"provider": "provider-b", "model": "m", "fallback": ["seat-gamma"]},
            "seat-gamma": {"provider": "provider-c", "model": "m", "fallback": ["seat-alpha"]},
        },
    )
    fleet_hub_policy.save_policy(conn, doc1, expected_version=1, actor="tester", reason="cycles")
    doc2 = copy.deepcopy(doc1)
    doc2["seats"]["seat-gamma"]["effort"] = "high"
    affected = _preview(conn, doc2)["affected"]
    assert "consumer-one" in affected["consumers"]


def test_affected_repository_when_registered_consumer_dependency_changes(conn):
    doc1 = _doc()
    fleet_hub_policy.save_policy(conn, doc1, expected_version=1, actor="tester", reason="doc1")
    doc2 = copy.deepcopy(doc1)
    doc2["seats"]["seat-alpha"]["model"] = "model-changed"
    affected = _preview(conn, doc2)["affected"]
    assert "repo/public" in affected["repositories"]


def test_affected_empty_unknown_repo_resolution(conn):
    doc1 = _doc()
    fleet_hub_policy.save_policy(conn, doc1, expected_version=1, actor="tester", reason="doc1")
    current = fleet_hub_policy.current_policy(conn)
    fleet_hub_policy.record_session_policy(
        conn,
        session_id="session-no-repo",
        consumer="consumer-one",
        node_id=NODE_A,
        repo_identity=None,
        revision=current["revision"],
        digest=current["digest"],
        source="startup",
    )
    doc2 = copy.deepcopy(doc1)
    doc2["seats"]["seat-alpha"]["model"] = "model-changed"
    affected = _preview(conn, doc2)["affected"]
    assert "consumer-one" in affected["consumers"]


# =============================================================================
# Part 2: cost_class and OpenCode Muse Spark 1.3 Contributor Free
# =============================================================================


def test_allow_free_defaults_to_false():
    doc = fleet_policy.parse_document({"schema": fleet_policy.POLICY_SCHEMA})
    assert doc["defaults"] == {}
    res = fleet_policy.resolve_policy(doc, "consumer-one", "repo/public")
    assert res["effective"]["data"]["allow_free"] is False
    assert res["effective"]["data"]["allow_training"] is False

    doc_explicit = fleet_policy.parse_document(
        {
            "schema": fleet_policy.POLICY_SCHEMA,
            "defaults": {"data": {"allow_free": True}},
        }
    )
    assert doc_explicit["defaults"]["data"]["allow_free"] is True
    res_explicit = fleet_policy.resolve_policy(doc_explicit, "consumer-one", "repo/public")
    assert res_explicit["effective"]["data"]["allow_free"] is True

    with pytest.raises(FleetPolicyError):
        fleet_policy.parse_document(
            {
                "schema": fleet_policy.POLICY_SCHEMA,
                "defaults": {"data": {"allow_free": "not-a-bool"}},
            }
        )


def test_cost_class_default_and_enum_validation():
    doc = fleet_policy.parse_document(
        {
            "schema": fleet_policy.POLICY_SCHEMA,
            "seats": {"s1": {"provider": "provider-a", "model": "m"}},
        }
    )
    assert doc["seats"]["s1"]["cost_class"] == "unknown"

    for val in ("paid", "subscription", "free", "unknown"):
        d = fleet_policy.parse_document(
            {
                "schema": fleet_policy.POLICY_SCHEMA,
                "seats": {"s1": {"provider": "provider-a", "model": "m", "cost_class": val}},
            }
        )
        assert d["seats"]["s1"]["cost_class"] == val

    with pytest.raises(FleetPolicyError, match="cost_class"):
        fleet_policy.parse_document(
            {
                "schema": fleet_policy.POLICY_SCHEMA,
                "seats": {"s1": {"provider": "provider-a", "model": "m", "cost_class": "pro"}},
            }
        )


def test_opencode_contributor_free_defaults():
    for model_id in ("muse-spark-1.3-contributor-free", "opencode/muse-spark-1.3-contributor-free"):
        doc = fleet_policy.parse_document(
            {
                "schema": fleet_policy.POLICY_SCHEMA,
                "seats": {"s1": {"provider": "opencode", "model": model_id}},
            }
        )
        assert doc["seats"]["s1"]["cost_class"] == "free"
        assert doc["seats"]["s1"]["training_allowed"] is True


def test_opencode_contributor_free_contradiction_rejection():
    with pytest.raises(FleetPolicyError, match="cannot claim cost_class 'paid'"):
        fleet_policy.parse_document(
            {
                "schema": fleet_policy.POLICY_SCHEMA,
                "seats": {
                    "s1": {"provider": "opencode", "model": "muse-spark-1.3-contributor-free", "cost_class": "paid"}
                },
            }
        )

    with pytest.raises(FleetPolicyError, match="cannot claim cost_class 'subscription'"):
        fleet_policy.parse_document(
            {
                "schema": fleet_policy.POLICY_SCHEMA,
                "seats": {
                    "s1": {
                        "provider": "opencode",
                        "model": "opencode/muse-spark-1.3-contributor-free",
                        "cost_class": "subscription",
                    }
                },
            }
        )

    with pytest.raises(FleetPolicyError, match="training_allowed cannot be false"):
        fleet_policy.parse_document(
            {
                "schema": fleet_policy.POLICY_SCHEMA,
                "seats": {
                    "s1": {
                        "provider": "opencode",
                        "model": "muse-spark-1.3-contributor-free",
                        "training_allowed": False,
                    }
                },
            }
        )


def test_opencode_contributor_floor_enforcement_with_alias_and_overlay():
    doc = {
        "schema": fleet_policy.POLICY_SCHEMA,
        "defaults": {"data": {"allow_free": False, "allow_training": False}},
        "seats": {
            "my-seat": {
                "provider": "opencode",
                "model": "generic-alias",
                "bindings": {"native": {"model": "muse-spark-1.3-contributor-free"}},
            }
        },
        "consumers": {
            "consumer-one": {},
        },
    }
    resolution = fleet_policy.resolve_policy(doc, "consumer-one", "repo/public")
    admissible = fleet_policy.admissible_seat(doc, "my-seat", resolution)
    assert not admissible["admissible"]
    assert "free-not-permitted" in admissible["reasons"]
    assert "training-not-permitted" in admissible["reasons"]

    # Consumer seat_bindings overlay
    doc_overlay = {
        "schema": fleet_policy.POLICY_SCHEMA,
        "defaults": {"data": {"allow_free": True, "allow_training": False}},
        "seats": {
            "my-seat": {
                "provider": "opencode",
                "model": "some-model",
            }
        },
        "consumers": {
            "consumer-one": {
                "seat_bindings": {"my-seat": {"native": {"model": "opencode/muse-spark-1.3-contributor-free"}}},
            }
        },
    }
    resolution_ov = fleet_policy.resolve_policy(doc_overlay, "consumer-one", "repo/public")
    admissible_ov = fleet_policy.admissible_seat(doc_overlay, "my-seat", resolution_ov)
    assert not admissible_ov["admissible"]
    assert "training-not-permitted" in admissible_ov["reasons"]


def test_admissible_seat_free_not_permitted():
    doc = {
        "schema": fleet_policy.POLICY_SCHEMA,
        "defaults": {"data": {"allow_free": False, "allow_training": False}},
        "seats": {
            "free-seat": {
                "provider": "provider-a",
                "model": "m",
                "cost_class": "free",
                "training_allowed": False,
            }
        },
    }
    resolution = fleet_policy.resolve_policy(doc, "consumer-one", "repo/public")
    admissible = fleet_policy.admissible_seat(doc, "free-seat", resolution)
    assert not admissible["admissible"]
    assert admissible["reasons"] == ["free-not-permitted"]

    doc_allow = copy.deepcopy(doc)
    doc_allow["defaults"]["data"]["allow_free"] = True
    resolution_allow = fleet_policy.resolve_policy(doc_allow, "consumer-one", "repo/public")
    admissible_allow = fleet_policy.admissible_seat(doc_allow, "free-seat", resolution_allow)
    assert admissible_allow["admissible"]
    assert admissible_allow["reasons"] == []


def test_private_repo_allows_ordinary_free_but_forbids_contributor_training():
    doc = {
        "schema": fleet_policy.POLICY_SCHEMA,
        "defaults": {"data": {"allow_free": True, "allow_training": True}},
        "seats": {
            "ordinary-free": {
                "provider": "provider-a",
                "model": "m1",
                "cost_class": "free",
                "training_allowed": False,
            },
            "contributor-free": {"provider": "opencode", "model": "muse-spark-1.3-contributor-free"},
        },
        "repositories": {
            "org/private-repo": {"privacy": "private"},
        },
    }
    resolution = fleet_policy.resolve_policy(doc, "consumer-one", "org/private-repo")
    assert resolution["effective"]["data"]["allow_free"] is True
    assert resolution["effective"]["data"]["allow_training"] is False  # hard private repo training prohibition

    adm1 = fleet_policy.admissible_seat(doc, "ordinary-free", resolution)
    assert adm1["admissible"] is True
    assert adm1["reasons"] == []

    adm2 = fleet_policy.admissible_seat(doc, "contributor-free", resolution)
    assert adm2["admissible"] is False
    assert "training-not-permitted" in adm2["reasons"]


def test_public_repo_requires_both_allow_free_and_allow_training_for_contributor():
    # Only allow_free
    doc_free_only = {
        "schema": fleet_policy.POLICY_SCHEMA,
        "defaults": {"data": {"allow_free": True, "allow_training": False}},
        "seats": {"cf": {"provider": "opencode", "model": "muse-spark-1.3-contributor-free"}},
        "repositories": {"org/pub": {"privacy": "public"}},
    }
    p1 = fleet_policy.resolve_policy(doc_free_only, "consumer-one", "org/pub")
    adm1 = fleet_policy.admissible_seat(doc_free_only, "cf", p1)
    assert adm1["admissible"] is False
    assert adm1["reasons"] == ["training-not-permitted"]

    # Only allow_training
    doc_train_only = {
        "schema": fleet_policy.POLICY_SCHEMA,
        "defaults": {"data": {"allow_free": False, "allow_training": True}},
        "seats": {"cf": {"provider": "opencode", "model": "muse-spark-1.3-contributor-free"}},
        "repositories": {"org/pub": {"privacy": "public"}},
    }
    p2 = fleet_policy.resolve_policy(doc_train_only, "consumer-one", "org/pub")
    adm2 = fleet_policy.admissible_seat(doc_train_only, "cf", p2)
    assert adm2["admissible"] is False
    assert adm2["reasons"] == ["free-not-permitted"]

    # Both False
    doc_neither = {
        "schema": fleet_policy.POLICY_SCHEMA,
        "defaults": {"data": {"allow_free": False, "allow_training": False}},
        "seats": {"cf": {"provider": "opencode", "model": "muse-spark-1.3-contributor-free"}},
        "repositories": {"org/pub": {"privacy": "public"}},
    }
    p3 = fleet_policy.resolve_policy(doc_neither, "consumer-one", "org/pub")
    adm3 = fleet_policy.admissible_seat(doc_neither, "cf", p3)
    assert adm3["admissible"] is False
    assert set(adm3["reasons"]) == {"free-not-permitted", "training-not-permitted"}

    # Both True
    doc_both = {
        "schema": fleet_policy.POLICY_SCHEMA,
        "defaults": {"data": {"allow_free": True, "allow_training": True}},
        "seats": {"cf": {"provider": "opencode", "model": "muse-spark-1.3-contributor-free"}},
        "repositories": {"org/pub": {"privacy": "public"}},
    }
    p4 = fleet_policy.resolve_policy(doc_both, "consumer-one", "org/pub")
    adm4 = fleet_policy.admissible_seat(doc_both, "cf", p4)
    assert adm4["admissible"] is True
    assert adm4["reasons"] == []


def test_unknown_cost_class_remains_explicit_no_paid_certainty():
    doc = {
        "schema": fleet_policy.POLICY_SCHEMA,
        "defaults": {"data": {"allow_free": False}},
        "seats": {"s1": {"provider": "provider-a", "model": "custom-m"}},
    }
    p = fleet_policy.resolve_policy(doc, "consumer-one", "repo/public")
    parsed = fleet_policy.parse_document(doc)
    assert parsed["seats"]["s1"]["cost_class"] == "unknown"
    adm = fleet_policy.admissible_seat(doc, "s1", p)
    assert adm["admissible"] is True
    assert adm["reasons"] == []


# =============================================================================
# Part 3: record_acknowledged_policy
# =============================================================================


def test_record_acknowledged_policy_persists_snapshot_and_projection(conn):
    doc = _doc()
    fleet_hub_policy.save_policy(conn, doc, expected_version=1, actor="tester", reason="first")
    current = fleet_hub_policy.current_policy(conn)

    pending = _prepare(conn, session_id="session-ack-1")
    ack = fleet_hub_policy.record_acknowledged_policy(
        conn, pending, node_id=NODE_A, expected_context_hash=pending["context_hash"]
    )
    assert ack["external_session_id"] == "session-ack-1"
    assert ack["receipt_key"] == pending["pending_key"]
    assert ack["session_id"] == pending["pending_key"]
    assert ack["revision"] == current["revision"]
    assert ack["digest"] == current["digest"]
    assert ack["effective"] == pending["effective"]
    assert ack["sources"] == pending["sources"]
    assert ack["selected"] == pending["selected"]
    assert ack["loaded_at"] is not None
    assert ack["acknowledged_at"] is not None

    states = fleet_hub_policy.list_session_states(conn)
    matching = [s for s in states if s["external_session_id"] == "session-ack-1"]
    assert len(matching) == 1
    assert matching[0]["receipt_key"] == pending["pending_key"]
    assert matching[0]["loaded_snapshot"] == ack
    assert matching[0]["state"] == "current"


def test_new_prepare_after_old_ack_does_not_change_loaded_snapshot(conn):
    doc = _doc()
    fleet_hub_policy.save_policy(conn, doc, expected_version=1, actor="tester", reason="first")

    pending1 = _prepare(
        conn,
        session_id="session-ack-2",
        overrides={"roles": {"impl": "seat-alpha"}},
    )
    ack1 = fleet_hub_policy.record_acknowledged_policy(
        conn, pending1, node_id=NODE_A, expected_context_hash=pending1["context_hash"]
    )
    assert ack1["overrides"] == {"roles": {"impl": "seat-alpha"}}

    # Prepare later with different overrides
    _prepare(
        conn,
        session_id="session-ack-2",
        overrides={"roles": {"impl": "seat-beta"}},
    )
    # The loaded snapshot must NOT have changed
    loaded = fleet_hub_policy.get_acknowledged_policy(conn, receipt_key=pending1["pending_key"])
    assert loaded["overrides"] == {"roles": {"impl": "seat-alpha"}}
    assert loaded["loaded_at"] == ack1["loaded_at"]

    states = fleet_hub_policy.list_session_states(conn)
    session_state = next(s for s in states if s["external_session_id"] == "session-ack-2")
    assert session_state["loaded_snapshot"]["overrides"] == {"roles": {"impl": "seat-alpha"}}
    assert session_state["pending_snapshot"]["overrides"] == {"roles": {"impl": "seat-beta"}}


def test_failed_ack_leaves_last_loaded_unchanged(conn):
    doc = _doc()
    fleet_hub_policy.save_policy(conn, doc, expected_version=1, actor="tester", reason="first")

    pending1 = _prepare(conn, session_id="session-ack-3")
    ack1 = fleet_hub_policy.record_acknowledged_policy(
        conn, pending1, node_id=NODE_A, expected_context_hash=pending1["context_hash"]
    )

    with pytest.raises(FleetHubForbidden):
        fleet_hub_policy.record_acknowledged_policy(
            conn, pending1, node_id=NODE_B, expected_context_hash=pending1["context_hash"]
        )

    loaded = fleet_hub_policy.get_acknowledged_policy(conn, receipt_key=pending1["pending_key"])
    assert loaded == ack1


def test_re_ack_history_idempotency_vs_changed_override_context(conn):
    doc = _doc()
    fleet_hub_policy.save_policy(conn, doc, expected_version=1, actor="tester", reason="first")

    pending1 = _prepare(conn, session_id="session-ack-4")
    fleet_hub_policy.record_acknowledged_policy(
        conn, pending1, node_id=NODE_A, expected_context_hash=pending1["context_hash"]
    )
    history1 = fleet_hub_policy.acknowledged_policy_history(conn, receipt_key=pending1["pending_key"])
    assert len(history1) == 1

    # Repeated identical ack is idempotent
    fleet_hub_policy.record_acknowledged_policy(
        conn, pending1, node_id=NODE_A, expected_context_hash=pending1["context_hash"]
    )
    history1_repeat = fleet_hub_policy.acknowledged_policy_history(conn, receipt_key=pending1["pending_key"])
    assert len(history1_repeat) == 1

    # New prepare with changed override context, then ack
    pending2 = _prepare(
        conn,
        session_id="session-ack-4",
        overrides={"roles": {"impl": "seat-beta"}},
    )
    fleet_hub_policy.record_acknowledged_policy(
        conn, pending2, node_id=NODE_A, expected_context_hash=pending2["context_hash"]
    )
    history2 = fleet_hub_policy.acknowledged_policy_history(conn, receipt_key=pending2["pending_key"])
    assert len(history2) == 2
    assert history2[0]["overrides"] == {}
    assert history2[1]["overrides"] == {"roles": {"impl": "seat-beta"}}

    current_ack = fleet_hub_policy.get_acknowledged_policy(conn, receipt_key=pending2["pending_key"])
    assert current_ack["overrides"] == {"roles": {"impl": "seat-beta"}}


def test_owner_mismatch_refusal(conn):
    doc = _doc()
    fleet_hub_policy.save_policy(conn, doc, expected_version=1, actor="tester", reason="first")

    pending = _prepare(conn, session_id="session-ack-5")
    with pytest.raises(FleetHubForbidden, match="owned by another node"):
        fleet_hub_policy.record_acknowledged_policy(
            conn, pending, node_id=NODE_B, expected_context_hash=pending["context_hash"]
        )


def test_old_static_version_refusal(conn):
    doc1 = _doc()
    fleet_hub_policy.save_policy(conn, doc1, expected_version=1, actor="tester", reason="first")

    pending = _prepare(conn, session_id="session-ack-6")

    doc2 = copy.deepcopy(doc1)
    doc2["seats"]["seat-beta"]["model"] = "model-b-2"
    fleet_hub_policy.save_policy(conn, doc2, expected_version=2, actor="tester", reason="second")

    with pytest.raises(FleetHubError, match="stale revision"):
        fleet_hub_policy.record_acknowledged_policy(
            conn, pending, node_id=NODE_A, expected_context_hash=pending["context_hash"]
        )


def test_transaction_rollback_on_failed_ack(conn):
    doc = _doc()
    fleet_hub_policy.save_policy(conn, doc, expected_version=1, actor="tester", reason="first")

    pending = _prepare(conn, session_id="session-ack-7")

    conn.execute("BEGIN IMMEDIATE")
    try:
        fleet_hub_policy.record_acknowledged_policy(
            conn, pending, node_id=NODE_A, expected_context_hash=pending["context_hash"]
        )
        raise RuntimeError("simulated caller failure")
    except RuntimeError:
        conn.rollback()

    with pytest.raises(FleetHubError, match="no acknowledged receipt"):
        fleet_hub_policy.get_acknowledged_policy(conn, receipt_key=pending["pending_key"])


def test_migration_existing_rows_returns_unknown_snapshot(conn):
    doc = _doc()
    fleet_hub_policy.save_policy(conn, doc, expected_version=1, actor="tester", reason="first")
    current = fleet_hub_policy.current_policy(conn)

    fleet_hub_policy.record_session_policy(
        conn,
        session_id="session-legacy",
        consumer="consumer-one",
        node_id=NODE_A,
        repo_identity="repo/public",
        revision=current["revision"],
        digest=current["digest"],
        source="startup",
    )

    states = fleet_hub_policy.list_session_states(conn)
    legacy = next(s for s in states if s["session_id"] == "session-legacy")
    assert legacy["external_session_id"] == "session-legacy"
    assert legacy["loaded_snapshot"]["status"] == "unknown"
    assert legacy["loaded_snapshot"]["effective"] is None
    assert legacy["loaded_snapshot"]["sources"] is None
    assert legacy["loaded_snapshot"]["selected"] is None


def test_find_loaded_session_by_external_id_and_receipt_key(conn):
    doc = _doc()
    fleet_hub_policy.save_policy(conn, doc, expected_version=1, actor="tester", reason="first")

    pending = _prepare(conn, session_id="session-find-1")
    ack = fleet_hub_policy.record_acknowledged_policy(
        conn, pending, node_id=NODE_A, expected_context_hash=pending["context_hash"]
    )

    # Find by external session_id with node_id
    found1 = fleet_hub_policy.find_loaded_session(
        conn, session_id="session-find-1", consumer="consumer-one", repo_identity="repo/public", node_id=NODE_A
    )
    assert found1 is not None
    assert found1["receipt_key"] == ack["receipt_key"]

    # Find by receipt_key with node_id
    found2 = fleet_hub_policy.find_loaded_session(
        conn, session_id=ack["receipt_key"], consumer="consumer-one", repo_identity="repo/public", node_id=NODE_A
    )
    assert found2 is not None
    assert found2["receipt_key"] == ack["receipt_key"]

    # Find by external session_id without node_id
    found3 = fleet_hub_policy.find_loaded_session(
        conn, session_id="session-find-1", consumer="consumer-one", repo_identity="repo/public"
    )
    assert found3 is not None
    assert found3["receipt_key"] == ack["receipt_key"]


# =============================================================================
# Part 4: Mandatory Regressions (1-5)
# =============================================================================


def test_find_loaded_session_two_nodes_same_id_different_repo(conn):
    doc = _doc(
        repositories={
            "repo/one": {"privacy": "public"},
            "repo/two": {"privacy": "public"},
        }
    )
    fleet_hub_policy.save_policy(conn, doc, expected_version=1, actor="tester", reason="doc")

    pending_a = _prepare(
        conn, session_id="shared-id", consumer="consumer-one", node_id=NODE_A, repo_identity="repo/one"
    )
    ack_a = fleet_hub_policy.record_acknowledged_policy(
        conn, pending_a, node_id=NODE_A, expected_context_hash=pending_a["context_hash"]
    )

    pending_b = _prepare(
        conn, session_id="shared-id", consumer="consumer-one", node_id=NODE_B, repo_identity="repo/two"
    )
    ack_b = fleet_hub_policy.record_acknowledged_policy(
        conn, pending_b, node_id=NODE_B, expected_context_hash=pending_b["context_hash"]
    )

    # Node A looking in repo/one gets Node A's row
    found_a = fleet_hub_policy.find_loaded_session(
        conn, session_id="shared-id", consumer="consumer-one", repo_identity="repo/one", node_id=NODE_A
    )
    assert found_a is not None
    assert found_a["receipt_key"] == ack_a["receipt_key"]
    assert found_a["owner_node"] == NODE_A
    assert found_a["repo_identity"] == "repo/one"

    # Node B looking in repo/one finds nothing (it only has repo/two)
    assert (
        fleet_hub_policy.find_loaded_session(
            conn, session_id="shared-id", consumer="consumer-one", repo_identity="repo/one", node_id=NODE_B
        )
        is None
    )

    # Node B looking in repo/two gets Node B's row
    found_b = fleet_hub_policy.find_loaded_session(
        conn, session_id="shared-id", consumer="consumer-one", repo_identity="repo/two", node_id=NODE_B
    )
    assert found_b is not None
    assert found_b["receipt_key"] == ack_b["receipt_key"]
    assert found_b["owner_node"] == NODE_B
    assert found_b["repo_identity"] == "repo/two"

    # Node A looking in repo/two finds nothing
    assert (
        fleet_hub_policy.find_loaded_session(
            conn, session_id="shared-id", consumer="consumer-one", repo_identity="repo/two", node_id=NODE_A
        )
        is None
    )


def test_find_loaded_session_same_consumer_collision_rejects_unspecified_ambiguity(conn):
    doc = _doc()
    fleet_hub_policy.save_policy(conn, doc, expected_version=1, actor="tester", reason="doc")

    pending_a = _prepare(
        conn, session_id="col-session", consumer="consumer-one", node_id=NODE_A, repo_identity="repo/public"
    )
    ack_a = fleet_hub_policy.record_acknowledged_policy(
        conn, pending_a, node_id=NODE_A, expected_context_hash=pending_a["context_hash"]
    )

    pending_b = _prepare(
        conn, session_id="col-session", consumer="consumer-one", node_id=NODE_B, repo_identity="repo/public"
    )
    ack_b = fleet_hub_policy.record_acknowledged_policy(
        conn, pending_b, node_id=NODE_B, expected_context_hash=pending_b["context_hash"]
    )

    # Specified node_id matches exact owner
    found_a = fleet_hub_policy.find_loaded_session(
        conn, session_id="col-session", consumer="consumer-one", repo_identity="repo/public", node_id=NODE_A
    )
    assert found_a is not None
    assert found_a["receipt_key"] == ack_a["receipt_key"]

    found_b = fleet_hub_policy.find_loaded_session(
        conn, session_id="col-session", consumer="consumer-one", repo_identity="repo/public", node_id=NODE_B
    )
    assert found_b is not None
    assert found_b["receipt_key"] == ack_b["receipt_key"]

    # Unspecified node lookup must reject ambiguity because both Node A and Node B match
    unspecified = fleet_hub_policy.find_loaded_session(
        conn, session_id="col-session", consumer="consumer-one", repo_identity="repo/public", node_id=None
    )
    assert unspecified is None


def test_find_loaded_session_top_level_loaded_context_from_stored_ack(conn):
    doc = _doc()
    fleet_hub_policy.save_policy(conn, doc, expected_version=1, actor="tester", reason="doc")

    pending1 = _prepare(conn, session_id="sess-top-level", overrides={"roles": {"impl": "seat-alpha"}})
    ack1 = fleet_hub_policy.record_acknowledged_policy(
        conn, pending1, node_id=NODE_A, expected_context_hash=pending1["context_hash"]
    )

    # Prepare later with changed context (pending differs from ack)
    pending2 = _prepare(conn, session_id="sess-top-level", overrides={"roles": {"impl": "seat-beta"}})

    found = fleet_hub_policy.find_loaded_session(
        conn, session_id="sess-top-level", consumer="consumer-one", repo_identity="repo/public", node_id=NODE_A
    )
    assert found is not None
    # Top-level context fields must come from stored ack, NOT pending!
    assert found["context_hash"] == ack1["context_hash"]
    assert found["context_hash"] != pending2["context_hash"]
    assert found["effective"] == ack1["effective"]
    assert found["sources"] == ack1["sources"]
    assert found["selected"] == ack1["selected"]
    assert found["origin"] == ack1["origin"]
    assert found["external_session_id"] == "sess-top-level"

    # Separate nested loaded_snapshot and pending_snapshot
    assert found["loaded_snapshot"]["context_hash"] == ack1["context_hash"]
    assert found["pending_snapshot"]["context_hash"] == pending2["context_hash"]


def test_record_acknowledged_policy_same_version_reprepare_refuses_old_ack(conn):
    doc = _doc()
    fleet_hub_policy.save_policy(conn, doc, expected_version=1, actor="tester", reason="doc")

    # A prepares instructions A
    pending_a = _prepare(conn, session_id="sess-reprep", instructions="do task A")
    hash_a = pending_a["context_hash"]

    # Under same version, reprepare B with instructions B
    pending_b = _prepare(conn, session_id="sess-reprep", instructions="do task B")
    hash_b = pending_b["context_hash"]
    assert hash_a != hash_b

    # Ack A with hash A must be refused because stored pending has hash B
    with pytest.raises(FleetHubError, match="does not match"):
        fleet_hub_policy.record_acknowledged_policy(conn, pending_a, node_id=NODE_A, expected_context_hash=hash_a)

    # B is not loaded
    assert fleet_hub_policy.find_acknowledged_policy(conn, receipt_key=pending_a["pending_key"]) is None


def test_record_acknowledged_policy_refuses_missing_context_hash(conn):
    doc = _doc()
    fleet_hub_policy.save_policy(conn, doc, expected_version=1, actor="tester", reason="doc")

    key = fleet_hub_policy.session_key(NODE_A, "consumer-one", "repo/public", "sess-no-hash")
    current = fleet_hub_policy.current_policy(conn)
    conn.execute(
        f"INSERT INTO fleet_policy_pending ({fleet_hub_policy._PENDING_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            key,
            "sess-no-hash",
            "consumer-one",
            NODE_A,
            "repo/public",
            "test",
            current["revision"],
            current["digest"],
            "{}",
            None,
            "{}",
            "{}",
            None,
            None,
            None,
            "resolve",
            fleet_hub._utc_now(),
            fleet_hub._utc_now(),
        ),
    )

    with pytest.raises(FleetHubError, match="no context_hash"):
        fleet_hub_policy.record_acknowledged_policy(
            conn, key, node_id=NODE_A, expected_context_hash="sha256:" + "a" * 64
        )


def test_record_acknowledged_policy_validates_expected_context_hash_format(conn):
    doc = _doc()
    fleet_hub_policy.save_policy(conn, doc, expected_version=1, actor="tester", reason="doc")
    pending = _prepare(conn, session_id="sess-bad-hash")

    with pytest.raises(FleetHubError, match="sha256:64hex"):
        fleet_hub_policy.record_acknowledged_policy(conn, pending, node_id=NODE_A, expected_context_hash="invalid-hash")

    valid = pending["context_hash"]
    for bad in (valid + "\n", valid + "\r", valid + "\x00", valid + "\t", "\n" + valid):
        with pytest.raises(FleetHubError, match="sha256:64hex"):
            fleet_hub_policy.record_acknowledged_policy(conn, pending, node_id=NODE_A, expected_context_hash=bad)


def test_record_acknowledged_policy_duplicate_identical_ack_preserves_first_loaded_at(conn):
    doc = _doc()
    fleet_hub_policy.save_policy(conn, doc, expected_version=1, actor="tester", reason="doc")

    pending = _prepare(conn, session_id="sess-idem")
    t1 = "2026-09-01T12:00:00+00:00"
    ack1 = fleet_hub_policy.record_acknowledged_policy(
        conn, pending, node_id=NODE_A, expected_context_hash=pending["context_hash"], loaded_at=t1
    )
    assert ack1["loaded_at"] == t1
    t2 = "2026-09-01T12:05:00+00:00"
    ack2 = fleet_hub_policy.record_acknowledged_policy(
        conn, pending, node_id=NODE_A, expected_context_hash=pending["context_hash"], loaded_at=t2
    )

    history = fleet_hub_policy.acknowledged_policy_history(conn, receipt_key=pending["pending_key"])
    assert len(history) == 1
    assert history[0]["loaded_at"] == t1
    assert ack2["loaded_at"] == t1
    sess_state = next(
        s for s in fleet_hub_policy.list_session_states(conn) if s["receipt_key"] == pending["pending_key"]
    )
    assert sess_state["loaded_at"] == t1


def test_idempotent_ack_cannot_restore_current_state_if_runtime_applied_false(conn):
    doc = _doc()
    fleet_hub_policy.save_policy(conn, doc, expected_version=1, actor="tester", reason="doc")

    pending = _prepare(conn, session_id="sess-runtime-fail")
    ack = fleet_hub_policy.record_acknowledged_policy(
        conn, pending, node_id=NODE_A, expected_context_hash=pending["context_hash"]
    )
    assert ack["loaded_at"] is not None

    states_before = fleet_hub_policy.list_session_states(conn)
    s_before = next(s for s in states_before if s["external_session_id"] == "sess-runtime-fail")
    assert s_before["state"] == "current"

    # Reload requested
    fleet_hub_policy.request_session_refresh(conn, s_before["receipt_key"], actor="operator")

    # Reload fails in runtime (applied=false, state="failed" acknowledged)
    fleet_hub_policy.acknowledge_session_refresh(
        conn, s_before["receipt_key"], node_id=NODE_A, state="failed", detail="crashed during reload"
    )

    # State is refreshable / failed, NOT current
    states_after = fleet_hub_policy.list_session_states(conn)
    s_after = next(s for s in states_after if s["external_session_id"] == "sess-runtime-fail")
    assert s_after["state"] == "refreshable"
    assert s_after["state"] != "current"
    assert s_after["refresh_state"] == "failed"

    # Stored ack snapshot remains unchanged
    loaded = fleet_hub_policy.get_acknowledged_policy(conn, receipt_key=pending["pending_key"])
    assert loaded["loaded_at"] == ack["loaded_at"]

    history_before = fleet_hub_policy.acknowledged_policy_history(conn, receipt_key=pending["pending_key"])
    first_loaded = ack["loaded_at"]
    refresh_state_before = s_after["refresh_state"]
    requested_at_before = s_after["refresh_requested_at"]
    detail_before = s_after["refresh_detail"]
    state_before = s_after["state"]

    fleet_hub_policy.record_acknowledged_policy(
        conn, pending, node_id=NODE_A, expected_context_hash=pending["context_hash"]
    )

    states_reack = fleet_hub_policy.list_session_states(conn)
    s_reack = next(s for s in states_reack if s["external_session_id"] == "sess-runtime-fail")
    assert s_reack["refresh_state"] == refresh_state_before == "failed"
    assert s_reack["refresh_requested_at"] == requested_at_before
    assert s_reack["refresh_detail"] == detail_before
    assert s_reack["state"] == state_before == "refreshable"
    assert s_reack["state"] != "current"
    assert s_reack["loaded_at"] == first_loaded
    history_after = fleet_hub_policy.acknowledged_policy_history(conn, receipt_key=pending["pending_key"])
    assert len(history_after) == len(history_before) == 1
    loaded_after = fleet_hub_policy.get_acknowledged_policy(conn, receipt_key=pending["pending_key"])
    assert loaded_after["loaded_at"] == first_loaded


def test_legacy_external_id_cleanup_preserves_foreign_owner_and_repo_rows(conn):
    doc = _doc(
        repositories={
            "repo/one": {"privacy": "public"},
            "repo/two": {"privacy": "public"},
        }
    )
    fleet_hub_policy.save_policy(conn, doc, expected_version=1, actor="tester", reason="doc")
    current = fleet_hub_policy.current_policy(conn)

    fleet_hub_policy.record_session_policy(
        conn,
        session_id="shared-legacy",
        consumer="consumer-one",
        node_id=NODE_B,
        repo_identity="repo/two",
        revision=current["revision"],
        digest=current["digest"],
        source="startup",
    )
    pending_a = _prepare(
        conn, session_id="shared-legacy", consumer="consumer-one", node_id=NODE_A, repo_identity="repo/one"
    )
    ack_a = fleet_hub_policy.record_acknowledged_policy(
        conn, pending_a, node_id=NODE_A, expected_context_hash=pending_a["context_hash"]
    )

    found_b = fleet_hub_policy.find_loaded_session(
        conn, session_id="shared-legacy", consumer="consumer-one", repo_identity="repo/two", node_id=NODE_B
    )
    assert found_b is not None
    assert found_b["owner_node"] == NODE_B
    assert found_b["repo_identity"] == "repo/two"
    assert found_b["session_id"] == "shared-legacy"
    assert found_b["loaded_snapshot"]["status"] == "unknown"

    found_a = fleet_hub_policy.find_loaded_session(
        conn, session_id="shared-legacy", consumer="consumer-one", repo_identity="repo/one", node_id=NODE_A
    )
    assert found_a is not None
    assert found_a["receipt_key"] == ack_a["receipt_key"]
    assert found_a["owner_node"] == NODE_A
    assert found_a["loaded_snapshot"]["context_hash"] == ack_a["context_hash"]

    assert (
        fleet_hub_policy.find_loaded_session(
            conn, session_id="shared-legacy", consumer="consumer-one", repo_identity="repo/two", node_id=NODE_A
        )
        is None
    )
    assert (
        fleet_hub_policy.find_loaded_session(
            conn, session_id="shared-legacy", consumer="consumer-one", repo_identity="repo/one", node_id=NODE_B
        )
        is None
    )

    fleet_hub_policy.record_session_policy(
        conn,
        session_id="owner-legacy",
        consumer="consumer-one",
        node_id=NODE_A,
        repo_identity="repo/one",
        revision=current["revision"],
        digest=current["digest"],
        source="startup",
    )
    pending_same_owner = _prepare(
        conn, session_id="owner-legacy", consumer="consumer-one", node_id=NODE_A, repo_identity="repo/two"
    )
    ack_same_owner = fleet_hub_policy.record_acknowledged_policy(
        conn, pending_same_owner, node_id=NODE_A, expected_context_hash=pending_same_owner["context_hash"]
    )

    found_repo_one = fleet_hub_policy.find_loaded_session(
        conn, session_id="owner-legacy", consumer="consumer-one", repo_identity="repo/one", node_id=NODE_A
    )
    assert found_repo_one is not None
    assert found_repo_one["session_id"] == "owner-legacy"
    assert found_repo_one["repo_identity"] == "repo/one"
    assert found_repo_one["loaded_snapshot"]["status"] == "unknown"

    found_repo_two = fleet_hub_policy.find_loaded_session(
        conn, session_id="owner-legacy", consumer="consumer-one", repo_identity="repo/two", node_id=NODE_A
    )
    assert found_repo_two is not None
    assert found_repo_two["receipt_key"] == ack_same_owner["receipt_key"]
    assert found_repo_two["repo_identity"] == "repo/two"
    assert found_repo_two["loaded_snapshot"]["context_hash"] == ack_same_owner["context_hash"]


def test_concurrent_same_version_reprepare_and_old_ack(conn, tmp_path):
    doc = _doc()
    fleet_hub_policy.save_policy(conn, doc, expected_version=1, actor="tester", reason="doc")
    pending_a = _prepare(conn, session_id="sess-race", instructions="do task A")
    hash_a = pending_a["context_hash"]
    receipt_key = pending_a["pending_key"]
    db_path = tmp_path / "fleet.db"

    started = threading.Barrier(2)
    ack_result: dict[str, Any] = {}
    pending_result: dict[str, Any] = {}
    errors: dict[str, BaseException] = {}

    def ack_worker() -> None:
        worker = fleet_hub.init_db(db_path)
        try:
            started.wait(timeout=5)
            ack_result["ack"] = fleet_hub_policy.record_acknowledged_policy(
                worker, receipt_key, node_id=NODE_A, expected_context_hash=hash_a
            )
        except BaseException as exc:
            errors["ack"] = exc
        finally:
            worker.close()

    def reprepare_worker() -> None:
        worker = fleet_hub.init_db(db_path)
        try:
            started.wait(timeout=5)
            pending_result["pending"] = _prepare(worker, session_id="sess-race", instructions="do task B")
        except BaseException as exc:
            errors["prep"] = exc
        finally:
            worker.close()

    t_ack = threading.Thread(target=ack_worker)
    t_prep = threading.Thread(target=reprepare_worker)
    t_ack.start()
    t_prep.start()
    t_ack.join(timeout=10)
    t_prep.join(timeout=10)
    assert not t_ack.is_alive()
    assert not t_prep.is_alive()
    assert "prep" not in errors

    pending_now = fleet_hub_policy.get_pending_policy(conn, pending_key=receipt_key)
    ack_found = fleet_hub_policy.find_acknowledged_policy(conn, receipt_key=receipt_key)
    hash_b = pending_result["pending"]["context_hash"]
    assert hash_b != hash_a
    assert pending_now["context_hash"] == hash_b
    assert pending_now["instructions"] == "do task B"

    if "ack" in errors:
        assert isinstance(errors["ack"], FleetHubError)
        assert "does not match" in str(errors["ack"])
        assert ack_found is None
        assert fleet_hub_policy.find_acknowledged_policy(conn, receipt_key=receipt_key) is None
    else:
        ack = ack_result["ack"]
        assert ack_found is not None
        assert ack["context_hash"] == hash_a
        assert ack_found["context_hash"] == hash_a
        assert ack_found["context_hash"] != pending_now["context_hash"]
        states = fleet_hub_policy.list_session_states(conn)
        sess = next(s for s in states if s["external_session_id"] == "sess-race")
        assert sess["context_hash"] == hash_a
        assert sess["loaded_snapshot"]["context_hash"] == hash_a
        assert sess["pending_snapshot"]["context_hash"] == hash_b
        assert sess["pending_apply_required"] is True
        assert sess["context_state"] == "stale"


def test_list_session_states_changed_pending_context_shows_stale_not_current(conn):
    doc = _doc()
    fleet_hub_policy.save_policy(conn, doc, expected_version=1, actor="tester", reason="doc")

    pending1 = _prepare(conn, session_id="sess-pending-diff", overrides={"roles": {"impl": "seat-alpha"}})
    ack1 = fleet_hub_policy.record_acknowledged_policy(
        conn, pending1, node_id=NODE_A, expected_context_hash=pending1["context_hash"]
    )

    # Re-prepare under same version with different context
    pending2 = _prepare(conn, session_id="sess-pending-diff", overrides={"roles": {"impl": "seat-beta"}})
    assert pending2["context_hash"] != pending1["context_hash"]

    states = fleet_hub_policy.list_session_states(conn)
    sess = next(s for s in states if s["external_session_id"] == "sess-pending-diff")
    # Even though static revision matches, pending context changed!
    assert sess["pending_apply_required"] is True
    assert sess["context_state"] == "stale"
    assert sess["state"] == "refreshable"
    assert sess["context_hash"] == ack1["context_hash"]


def test_list_session_states_real_harness_capability_stale_refreshable_restart_required(conn):
    doc = _doc(
        consumers={
            "c-none": {"reload": "none"},
            "c-refresh": {"reload": "refreshable"},
            "c-restart": {"reload": "restart-required"},
        }
    )
    fleet_hub_policy.save_policy(conn, doc, expected_version=1, actor="tester", reason="doc")

    # For c-none
    p_none = _prepare(conn, session_id="s-none", consumer="c-none")
    fleet_hub_policy.record_acknowledged_policy(
        conn, p_none, node_id=NODE_A, expected_context_hash=p_none["context_hash"]
    )
    fleet_hub_policy.request_session_refresh(conn, p_none["pending_key"], actor="operator")

    # For c-refresh
    p_ref = _prepare(conn, session_id="s-ref", consumer="c-refresh")
    fleet_hub_policy.record_acknowledged_policy(
        conn, p_ref, node_id=NODE_A, expected_context_hash=p_ref["context_hash"]
    )
    fleet_hub_policy.request_session_refresh(conn, p_ref["pending_key"], actor="operator")

    # For c-restart
    p_res = _prepare(conn, session_id="s-res", consumer="c-restart")
    fleet_hub_policy.record_acknowledged_policy(
        conn, p_res, node_id=NODE_A, expected_context_hash=p_res["context_hash"]
    )
    fleet_hub_policy.request_session_refresh(conn, p_res["pending_key"], actor="operator")

    states = {s["external_session_id"]: s for s in fleet_hub_policy.list_session_states(conn)}
    # Never mark reload success when only requested, and state reflects real capability
    assert states["s-none"]["state"] == "stale"
    assert states["s-ref"]["state"] == "refreshable"
    assert states["s-res"]["state"] == "restart-required"


def test_training_floor_brigade_model_contributor_behind_paid_canonical_name():
    doc = {
        "schema": fleet_policy.POLICY_SCHEMA,
        "seats": {
            "s1": {
                "provider": "custom-provider",
                "model": "expensive-paid-enterprise",
                "bindings": {
                    "brigade": {
                        "cli": "custom-cli",
                        "model": "opencode/muse-spark-1.3-contributor-free",
                    }
                },
            }
        },
    }
    parsed = fleet_policy.parse_document(doc)
    assert parsed["seats"]["s1"]["cost_class"] == "free"
    assert parsed["seats"]["s1"]["training_allowed"] is True

    # Reject if user tries to declare paid or training_allowed=False
    with pytest.raises(FleetPolicyError, match="cannot claim cost_class 'paid'"):
        doc_paid = copy.deepcopy(doc)
        doc_paid["seats"]["s1"]["cost_class"] = "paid"
        fleet_policy.parse_document(doc_paid)

    with pytest.raises(FleetPolicyError, match="training_allowed cannot be false"):
        doc_no_train = copy.deepcopy(doc)
        doc_no_train["seats"]["s1"]["training_allowed"] = False
        fleet_policy.parse_document(doc_no_train)


def test_training_floor_consumer_brigade_overlay():
    doc = {
        "schema": fleet_policy.POLICY_SCHEMA,
        "seats": {
            "clean-seat": {"provider": "custom", "model": "neutral-model"},
        },
        "consumers": {
            "brigade-run": {
                "seat_bindings": {
                    "clean-seat": {"brigade": {"model": "opencode/muse-spark-1.3-contributor-free"}},
                }
            }
        },
    }
    terms = fleet_policy.effective_seat_terms(doc, "clean-seat", "brigade-run")
    assert terms["is_contributor"] is True
    assert terms["cost_class"] == "free"
    assert terms["requires_training"] is True
    assert terms["warnings"] == ["contributor-terms-enforced"]


def test_training_floor_effective_seat_terms_helper_shape():
    doc = {
        "schema": fleet_policy.POLICY_SCHEMA,
        "seats": {
            "s-cf": {"provider": "opencode", "model": "opencode-muse-spark-1.3-contributor-free"},
            "s-ord": {"provider": "provider-a", "model": "free-m", "cost_class": "free", "training_allowed": False},
        },
    }
    terms_cf = fleet_policy.effective_seat_terms(doc, "s-cf")
    assert terms_cf["seat"] == "s-cf"
    assert terms_cf["is_contributor"] is True
    assert terms_cf["cost_class"] == "free"
    assert terms_cf["requires_training"] is True
    assert terms_cf["requires_free"] is True
    assert terms_cf["warnings"] == ["contributor-terms-enforced"]
    assert terms_cf["constraints"] == {"allow_training": True, "allow_free": True}

    terms_ord = fleet_policy.effective_seat_terms(doc, "s-ord")
    assert terms_ord["seat"] == "s-ord"
    assert terms_ord["is_contributor"] is False
    assert terms_ord["cost_class"] == "free"
    assert terms_ord["requires_training"] is False
    assert terms_ord["requires_free"] is True
    assert terms_ord["warnings"] == []
    assert terms_ord["constraints"] == {"allow_training": False, "allow_free": True}


def test_affected_auto_eligible_machine_priority_and_limit(conn):
    doc1 = _doc(
        machines={"worker-linux-1": {"os": "linux", "priority": 100, "concurrency": 2}},
        seats={"seat-alpha": {"provider": "provider-a", "model": "m1"}},
    )
    fleet_hub_policy.save_policy(conn, doc1, expected_version=1, actor="tester", reason="doc1")

    doc2 = copy.deepcopy(doc1)
    doc2["machines"]["worker-linux-1"]["priority"] = 250
    affected = _preview(conn, doc2)["affected"]
    assert "consumer-one" in affected["consumers"]


def test_affected_workload_os_requirement(conn):
    doc1 = _doc(
        routing={
            "enabled": True,
            "workload_requirements": {"eval": {"os": "linux"}},
        },
        machines={
            "worker-linux-1": {"os": "linux"},
            "worker-win-1": {"os": "windows"},
        },
    )
    fleet_hub_policy.save_policy(conn, doc1, expected_version=1, actor="tester", reason="doc1")

    # Editing worker-win-1 (which cannot satisfy linux workload requirement) does NOT affect consumer-one
    doc2 = copy.deepcopy(doc1)
    doc2["machines"]["worker-win-1"]["priority"] = 500
    affected = _preview(conn, doc2)["affected"]
    assert affected["consumers"] == []


def test_affected_repo_machine_override(conn):
    doc1 = _doc(
        machines={
            "worker-shared": {"os": "linux", "priority": 100},
            "worker-private": {"os": "linux", "priority": 100},
        },
        repositories={
            "repo/private-pool": {"privacy": "private", "eligible_machines": ["worker-private"]},
        },
    )
    fleet_hub_policy.save_policy(conn, doc1, expected_version=1, actor="tester", reason="doc1")

    # Changing worker-shared does NOT affect repo/private-pool because it is restricted to worker-private
    doc2 = copy.deepcopy(doc1)
    doc2["machines"]["worker-shared"]["priority"] = 200
    affected2 = _preview(conn, doc2)["affected"]
    assert "repo/private-pool" not in affected2["repositories"]

    # Changing worker-private DOES affect repo/private-pool
    doc3 = copy.deepcopy(doc1)
    doc3["machines"]["worker-private"]["priority"] = 200
    affected3 = _preview(conn, doc3)["affected"]
    assert "repo/private-pool" in affected3["repositories"]


def test_affected_consumer_specific_seat_bindings(conn):
    doc1 = _doc(
        consumers={
            "consumer-one": {
                "seat_bindings": {"seat-alpha": {"native": {"model": "native-1"}}},
            },
            "consumer-two": {},
        },
    )
    fleet_hub_policy.save_policy(conn, doc1, expected_version=1, actor="tester", reason="doc1")

    doc2 = copy.deepcopy(doc1)
    doc2["consumers"]["consumer-one"]["seat_bindings"]["seat-alpha"]["native"]["model"] = "native-2"
    affected = _preview(conn, doc2)["affected"]
    assert "consumer-one" in affected["consumers"]
    assert "consumer-two" not in affected["consumers"]


def test_affected_consumer_reload_change_affects_session_not_repo(conn):
    doc1 = _doc(
        consumers={"consumer-one": {"reload": "none"}},
    )
    fleet_hub_policy.save_policy(conn, doc1, expected_version=1, actor="tester", reason="doc1")
    current = fleet_hub_policy.current_policy(conn)

    fleet_hub_policy.record_session_policy(
        conn,
        session_id="session-rel",
        consumer="consumer-one",
        node_id=NODE_A,
        repo_identity="repo/public",
        revision=current["revision"],
        digest=current["digest"],
        source="startup",
    )

    doc2 = copy.deepcopy(doc1)
    doc2["consumers"]["consumer-one"]["reload"] = "refreshable"
    affected = _preview(conn, doc2)["affected"]
    assert "consumer-one" in affected["consumers"]
    assert "session-rel" in affected["sessions"]
    assert "repo/public" not in affected["repositories"]
