"""Closed Wazuh triage contracts: exact-key ingest and public result bounds."""

from __future__ import annotations

import pytest

from brigade.grokbot_wazuh.contracts import (
    ERROR_MESSAGES,
    FORBIDDEN_ALERT_KEYS,
    MAX_DETAIL_BYTES,
    MAX_FINDING_ID,
    MAX_RULE_ID,
    TOOLS,
    WazuhError,
    parse_action_status_input,
    parse_alert_status_input,
    parse_classify_input,
    parse_identifier,
    parse_incident_input,
    parse_ingest_input,
    parse_opaque_id,
    parse_propose_input,
)


def _alert(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "rule_id": "504",
        "rule_level": 12,
        "rule_description": "Agent disconnected",
        "rule_groups": ["agent_disconnected"],
        "agent_id": "001",
        "decoder": "agent-buffer",
        "timestamp": "2026-08-28T16:00:00Z",
        "detail": "Wazuh agent stopped sending keepalive",
    }
    payload.update(overrides)
    return payload


def test_public_tool_inventory_is_exact():
    assert TOOLS == {
        "wazuh_action_status",
        "wazuh_alert_status",
        "wazuh_classify",
        "wazuh_incident_bundle",
        "wazuh_ingest",
        "wazuh_propose_remediation",
    }


def test_ingest_rejects_unbounded_or_secret_fields():
    assert parse_ingest_input({"alerts": [_alert()]})["alerts"][0]["rule_id"] == "504"
    with pytest.raises(WazuhError) as unknown:
        parse_ingest_input({"alerts": [_alert(note="extra")]})
    assert unknown.value.code == "invalid_request"
    with pytest.raises(WazuhError) as oversized:
        parse_ingest_input({"alerts": [_alert(detail="x" * (MAX_DETAIL_BYTES + 1))]})
    assert oversized.value.code == "invalid_request"
    for key in sorted(FORBIDDEN_ALERT_KEYS):
        raw = _alert()
        raw[key] = "not-a-real-token"
        with pytest.raises(WazuhError) as forbidden:
            parse_ingest_input({"alerts": [raw]})
        assert forbidden.value.code == "invalid_request"
        assert key not in str(forbidden.value)
        assert "not-a-real-token" not in str(forbidden.value)
    for value in ("/etc/shadow", "C:\\Windows\\Temp", "curl http://example.invalid", "ssh root@192.0.2.10"):
        with pytest.raises(WazuhError) as supplied:
            parse_ingest_input({"alerts": [_alert(rule_description=value)]})
        assert supplied.value.code == "invalid_request"
        assert value not in str(supplied.value)


def test_identifiers_and_tool_inputs_are_strict():
    assert parse_identifier("agent-disconnected") == "agent-disconnected"
    assert parse_opaque_id("a" * 32) == "a" * 32
    assert parse_alert_status_input({}) == {}
    assert parse_classify_input({"fingerprint": "a" * 64}) == {"fingerprint": "a" * 64}
    assert parse_incident_input({"scope": "agent-disconnected"}) == {"scope": "agent-disconnected"}
    assert parse_propose_input({"finding_id": "agent-disconnected"}) == {"finding_id": "agent-disconnected"}
    assert parse_action_status_input({"proposal_id": "a" * 32}) == {"proposal_id": "a" * 32}
    with pytest.raises(WazuhError) as caught:
        parse_ingest_input({"alerts": [_alert()], "extra": True})
    assert caught.value.public_error() == {
        "error": {"code": "invalid_request", "message": ERROR_MESSAGES["invalid_request"]}
    }
    for value in ("", "a" * 65, "../host", "host/sub", "host;id"):
        with pytest.raises(WazuhError):
            parse_identifier(value)
    finding_id = f"{'a' * 64}:agent-disconnected:{'r' * MAX_RULE_ID}"
    assert len(finding_id) <= MAX_FINDING_ID
    assert parse_propose_input({"finding_id": finding_id})["finding_id"] == finding_id
    assert parse_incident_input({"scope": finding_id})["scope"] == finding_id
    with pytest.raises(WazuhError) as oversized_rule:
        parse_ingest_input({"alerts": [_alert(rule_id="r" * (MAX_RULE_ID + 1))]})
    assert oversized_rule.value.code == "invalid_request"
