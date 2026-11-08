"""A fleet policy write never lists a memory verifier as a reflect principal (phase-memory-verify D4)."""

from __future__ import annotations

import json

import pytest

from brigade import fleet_hub, fleet_hub_policy, fleet_hub_policy_api, fleet_hub_verifiers, fleet_policy


@pytest.fixture()
def conn(tmp_path):
    connection = fleet_hub.init_db(tmp_path / "fleet-hub.db")
    try:
        yield connection
    finally:
        connection.close()


def _publish(conn, verifiers: list[str]) -> None:
    path = fleet_hub_verifiers.sidecar_path(conn)
    assert path is not None
    path.write_text(json.dumps({"schema": fleet_hub_verifiers.SCHEMA, "verifiers": verifiers}))


def _document(reflect: list[str]) -> dict:
    document = fleet_policy.empty_document()
    document["memory"] = {"tier0_op_classes": ["append_evidence"], "reflect_principals": reflect}
    return document


def _save(conn, reflect: list[str], *, expected: int) -> dict:
    return fleet_hub_policy.save_policy(
        conn, _document(reflect), expected_version=expected, actor="admin", reason="memory reflect principals"
    )


def _revision(conn) -> int:
    return fleet_hub_policy.current_policy(conn)["revision"]


def test_the_sidecar_sits_beside_the_hub_database(conn, tmp_path):
    assert fleet_hub_verifiers.sidecar_path(conn) == tmp_path / "fleet-hub-memory-verifiers.json"


def test_a_policy_listing_a_verifier_is_refused_including_a_replay(conn):
    _publish(conn, ["agent:verify"])
    for _ in range(2):
        with pytest.raises(fleet_hub_verifiers.VerifierListed) as refused:
            _save(conn, ["agent:other", "agent:verify"], expected=1)
        assert refused.value.code == "memory_policy_verifier_listed"
        assert "agent:verify" in str(refused.value)
        assert "agent:other" not in str(refused.value)
        assert _revision(conn) == 1
    assert _save(conn, ["agent:other"], expected=1)["revision"] == 2


def test_rollback_to_a_revision_that_lists_a_later_verifier_is_refused(conn):
    assert _save(conn, ["agent:verify"], expected=1)["revision"] == 2
    assert _save(conn, [], expected=2)["revision"] == 3
    _publish(conn, ["agent:verify"])
    with pytest.raises(fleet_hub_verifiers.VerifierListed):
        fleet_hub_policy.rollback_policy(conn, to_revision=2, expected_version=3, actor="admin", reason="restore")
    assert _revision(conn) == 3
    _publish(conn, [])
    rolled = fleet_hub_policy.rollback_policy(conn, to_revision=2, expected_version=3, actor="admin", reason="restore")
    assert rolled["revision"] == 4


def test_no_published_list_means_no_verifier(conn):
    assert _save(conn, ["agent:verify"], expected=1)["revision"] == 2


@pytest.mark.parametrize("body", [b"{", b"[]", b'{"schema":"other","verifiers":[]}', b'{"schema":"x"}'])
def test_an_unreadable_list_refuses_only_policies_that_list_reflect_principals(conn, body):
    path = fleet_hub_verifiers.sidecar_path(conn)
    assert path is not None
    path.write_bytes(body.replace(b'"x"', json.dumps(fleet_hub_verifiers.SCHEMA).encode()))
    with pytest.raises(fleet_hub_verifiers.VerifierListed) as refused:
        _save(conn, ["agent:other"], expected=1)
    assert refused.value.code == "memory_policy_verifier_list_unreadable"
    assert _save(conn, [], expected=1)["revision"] == 2


def test_the_policy_api_reports_the_refusal_code(conn):
    _publish(conn, ["agent:verify"])
    body = {"action": "save", "document": _document(["agent:verify"]), "expected_version": 1, "reason": "listing"}
    with pytest.raises(fleet_hub_policy_api.FleetPolicyApiError) as refused:
        fleet_hub_policy_api.handle_policy(conn, body, caller_node=None, is_admin=True)
    assert (refused.value.status, refused.value.code) == (409, "memory_policy_verifier_listed")
    assert _revision(conn) == 1


def test_a_missing_list_after_memory_init_refuses_a_listing_policy(conn, tmp_path):
    (tmp_path / "fleet-hub-memory.db").write_bytes(b"")
    with pytest.raises(fleet_hub_verifiers.VerifierListed) as refused:
        _save(conn, ["agent:any"], expected=1)
    assert refused.value.code == "memory_policy_verifier_list_unreadable"
    assert "missing" in str(refused.value)
    assert _revision(conn) == 1
    assert _save(conn, [], expected=1)["revision"] == 2
    _publish(conn, [])
    assert _save(conn, ["agent:any"], expected=2)["revision"] == 3
