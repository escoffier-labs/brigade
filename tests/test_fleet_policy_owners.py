"""The ``owners`` section of the fleet policy document (charters)."""

from __future__ import annotations

import copy

import pytest

from brigade import fleet_hub, fleet_hub_policy, fleet_policy, fleet_policy_form
from brigade.fleet_policy import FleetPolicyError


def _owners() -> dict:
    return {
        "grokbot": {
            "label": "Grok Bot",
            "summary": "Builds approved issues.",
            "owns": ["Issues with the approval label"],
            "never": ["Deploys", "Releases"],
            "intake": ["Approval label"],
            "contact": "agent:grokbot",
            "match": {"grokbot_jobs": True, "harnesses": ["grokbot"]},
        },
        "miller": {
            "label": "Miller",
            "match": {"session_harnesses": ["dot"], "principals": ["miller"]},
        },
        "openclaw": {"label": "OpenClaw gateway"},
    }


def _document(owners: object | None = None) -> dict:
    document = fleet_policy.empty_document()
    if owners is not None:
        document["owners"] = owners
    return document


def test_full_record_parses_and_fills_defaults():
    parsed = fleet_policy.parse_document(_document(_owners()))
    grokbot = parsed["owners"]["grokbot"]
    assert grokbot["label"] == "Grok Bot"
    assert grokbot["never"] == ["Deploys", "Releases"]
    assert grokbot["contact"] == "agent:grokbot"
    assert grokbot["match"] == {
        "nodes": [],
        "harnesses": ["grokbot"],
        "conductors": [],
        "session_harnesses": [],
        "adapters": [],
        "principals": [],
        "grokbot_jobs": True,
    }
    openclaw = parsed["owners"]["openclaw"]
    assert openclaw["summary"] is None and openclaw["contact"] is None
    assert openclaw["owns"] == [] and openclaw["never"] == [] and openclaw["intake"] == []
    assert openclaw["match"]["grokbot_jobs"] is False
    assert list(parsed["owners"]) == ["grokbot", "miller", "openclaw"]


def test_parse_is_idempotent():
    once = fleet_policy.parse_document(_document(_owners()))
    assert fleet_policy.parse_document(once) == once


def test_missing_or_empty_section_is_omitted_so_old_digests_hold():
    base = fleet_policy.parse_document(fleet_policy.empty_document())
    assert "owners" not in base
    assert fleet_policy.parse_document(_document({})) == base
    assert fleet_policy.document_digest(_document({})) == fleet_policy.document_digest(base)
    assert "owners" not in fleet_policy.canonical_json(base)


@pytest.mark.parametrize(
    "owners, message",
    [
        ({"a": {"label": "A", "colour": "red"}}, "unknown owner 'a' field(s): colour"),
        ({"a": {"label": "A", "match": {"regex": ["x"]}}}, "unknown owner 'a' match field(s): regex"),
        ({"a": {}}, "owner 'a' field 'label' is required"),
        ({"a": {"label": ""}}, "owner 'a' field 'label' must not be empty"),
        ({"a": {"label": "x" * 257}}, "must be at most 256 characters"),
        ({"a": {"label": "A", "owns": "everything"}}, "owner 'a' field 'owns' must be a list"),
        ({"a": {"label": "A", "owns": [""]}}, "owner 'a' field 'owns'[0] must not be empty"),
        ({"a": {"label": "A", "owns": [7]}}, "owner 'a' field 'owns'[0] must be a string"),
        ({"a": {"label": "A", "never": ["x"] * 65}}, "must hold at most 64 items"),
        ({"a": {"label": "A", "contact": "has space"}}, "owner 'a' field 'contact' must match"),
        ({"a": {"label": "A", "match": {"nodes": ["*"]}}}, "owner 'a' match field 'nodes'[0] must match"),
        ({"a": {"label": "A", "match": {"nodes": "node-a"}}}, "owner 'a' match field 'nodes' must be a list"),
        ({"a": {"label": "A", "match": {"grokbot_jobs": "yes"}}}, "field 'grokbot_jobs' must be a boolean"),
        ({"a": {"label": "A", "match": ["nodes"]}}, "owner 'a' match must be a JSON object"),
        ({"bad name": {"label": "A"}}, "owner name must match"),
        ({"a": "text"}, "owner 'a' must be a JSON object"),
    ],
)
def test_invalid_owners_are_rejected_with_a_named_error(owners, message):
    with pytest.raises(FleetPolicyError) as raised:
        fleet_policy.parse_document(_document(owners))
    assert message in str(raised.value)


def test_section_must_be_an_object_and_bounded():
    with pytest.raises(FleetPolicyError, match="owners must be a JSON object"):
        fleet_policy.parse_document(_document(["grokbot"]))
    too_many = {f"owner-{index}": {"label": "x"} for index in range(fleet_policy.MAX_RECORDS + 1)}
    with pytest.raises(FleetPolicyError, match="'owners' must hold at most 256 records"):
        fleet_policy.parse_document(_document(too_many))


def test_owners_round_trip_through_save_and_rollback(tmp_path):
    conn = fleet_hub.init_db(tmp_path / "fleet.db")
    try:
        saved = fleet_hub_policy.save_policy(
            conn, _document(_owners()), expected_version=1, actor="admin", reason="seed owners"
        )
        assert saved["revision"] == 2
        stored = fleet_hub_policy.current_policy(conn)["document"]
        assert stored["owners"]["miller"]["match"]["principals"] == ["miller"]
        cleared = fleet_hub_policy.save_policy(
            conn, fleet_policy.empty_document(), expected_version=2, actor="admin", reason="clear owners"
        )
        assert "owners" not in cleared["document"]
        restored = fleet_hub_policy.rollback_policy(
            conn, to_revision=2, expected_version=3, actor="admin", reason="restore owners"
        )
        assert restored["document"]["owners"] == stored["owners"]
        assert restored["digest"] == saved["digest"]
    finally:
        conn.close()


def test_a_scoped_policy_form_edit_keeps_the_owners_section():
    current = fleet_policy.parse_document(_document(_owners()))
    submission = fleet_policy_form.parse_form(
        b"scope=defaults&action=preview&expected_version=1&field.data_allow_free=true"
    )
    edited = fleet_policy_form.build_document(copy.deepcopy(current), submission)
    assert edited["owners"] == current["owners"]
    assert edited["defaults"]["data"]["allow_free"] is True
