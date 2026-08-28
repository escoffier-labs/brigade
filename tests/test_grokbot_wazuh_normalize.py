# content-guard: allow bearer-token file
# content-guard: allow api-key-assignment file
"""Normalization, redaction, and deterministic fingerprint tests."""

from __future__ import annotations

from brigade.grokbot_wazuh.contracts import FINDING_KEYS, parse_ingest_input
from brigade.grokbot_wazuh.normalize import fingerprint_for, normalize_alert, rule_class_for, sanitize_detail


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


def test_normalized_record_has_required_finding_fields_and_redacted_body():
    secret = "ghp_exampleNotARealToken0000000000000001"
    alert = parse_ingest_input(
        {
            "alerts": [
                _alert(
                    detail=(
                        f"Bearer {secret} ssh root@192.0.2.10 "
                        "command: curl http://example.invalid/secret "
                        "path=/var/ossec/logs/alerts/alerts.json"
                    )
                )
            ]
        }
    )["alerts"][0]
    record = normalize_alert(alert, secrets=(secret,))
    assert FINDING_KEYS <= set(record)
    assert record["producer"] == "wazuh"
    assert record["finding_id"] == "001:agent-disconnected:504"
    assert record["revision"] != "1"
    assert len(record["revision"]) == 64
    assert record["observed_at"] == "2026-08-28T16:00:00Z"
    assert record["severity"] == "high"
    assert record["title"] == "Agent disconnected"
    assert record["source_ref"] == "wazuh:agent-disconnected"
    assert secret not in record["body"]
    assert "192.0.2.10" not in record["body"]
    assert "http://example.invalid/secret" not in record["body"]
    assert "/var/ossec/logs/alerts/alerts.json" not in record["body"]
    assert "[redacted]" in record["body"]
    assert record["content_digest"].startswith("sha256:")
    assert record["source_digest"].startswith("sha256:")


def test_fingerprints_are_deterministic_and_ignore_volatile_detail():
    left = parse_ingest_input({"alerts": [_alert(detail="first seen")]})["alerts"][0]
    right = parse_ingest_input({"alerts": [_alert(detail="later seen")]})["alerts"][0]
    assert fingerprint_for(left, rule_class_for(left)) == fingerprint_for(right, rule_class_for(right))
    assert normalize_alert(left)["fingerprint"] == normalize_alert(right)["fingerprint"]


def test_sanitize_detail_never_raises_and_truncates():
    assert sanitize_detail("ok") == "ok"
    assert len(sanitize_detail("x" * 20_000).encode("utf-8")) <= 4_096
    assert sanitize_detail("token=not-a-real-token", secrets=("not-a-real-token",)) == "token=[redacted]"


def test_title_and_common_secret_forms_are_sanitized_before_storage():
    alert = parse_ingest_input(
        {
            "alerts": [
                _alert(
                    rule_description="Authorization: Bearer not-a-real-bearer-token",
                    detail=(
                        "Authorization: Bearer not-a-real-bearer-token "
                        "password=not-a-real-password token=not-a-real-token "
                        "host=web.internal.example src=192.0.2.10 path=/var/ossec/logs/alerts.json"
                    ),
                )
            ]
        }
    )["alerts"][0]
    record = normalize_alert(alert)
    assert "Authorization" not in record["title"]
    assert "Bearer" not in record["title"]
    assert "not-a-real-bearer-token" not in record["title"]
    assert "not-a-real-bearer-token" not in record["body"]
    assert "not-a-real-password" not in record["body"]
    assert "not-a-real-token" not in record["body"]
    assert "web.internal.example" not in record["body"]
    assert "192.0.2.10" not in record["body"]
    assert "/var/ossec/logs/alerts.json" not in record["body"]
    assert "[redacted]" in record["title"]
    assert "[redacted]" in record["body"]


def test_semantic_revision_is_deterministic_and_changes_with_sanitized_content():
    first = normalize_alert(parse_ingest_input({"alerts": [_alert(rule_level=8)]})["alerts"][0])
    same = normalize_alert(parse_ingest_input({"alerts": [_alert(rule_level=8)]})["alerts"][0])
    escalated = normalize_alert(
        parse_ingest_input({"alerts": [_alert(rule_level=15, detail="later body")]})["alerts"][0]
    )
    assert first["fingerprint"] == same["fingerprint"] == escalated["fingerprint"]
    assert first["revision"] == same["revision"]
    assert first["revision"] != "1"
    assert first["revision"] != escalated["revision"]
    assert first["severity"] == "warning"
    assert escalated["severity"] == "critical"


def test_composed_finding_id_accepts_maximum_agent_id():
    agent_id = "a" * 64
    rule_id = "r" * 32
    alert = parse_ingest_input({"alerts": [_alert(agent_id=agent_id, rule_id=rule_id)]})["alerts"][0]
    record = normalize_alert(alert)
    assert record["finding_id"] == f"{agent_id}:agent-disconnected:{rule_id}"
    assert record["agent_id"] == agent_id
    assert record["rule_id"] == rule_id
    assert len(record["finding_id"]) <= 128


def test_sensitive_assignments_are_redacted_in_equals_colon_and_json_forms():
    alert = parse_ingest_input(
        {
            "alerts": [
                _alert(
                    detail=(
                        "api_key=not-a-real-api-key-value-01 "
                        "secret: not-a-real-secret-value-01 "
                        '"credential": "not-a-real-credential-value-01" '
                        "private_key = not-a-real-private-key-01"
                    )
                )
            ]
        }
    )["alerts"][0]
    record = normalize_alert(alert)
    assert "not-a-real-api-key-value-01" not in record["body"]
    assert "not-a-real-secret-value-01" not in record["body"]
    assert "not-a-real-credential-value-01" not in record["body"]
    assert "not-a-real-private-key-01" not in record["body"]
    assert record["body"].count("[redacted]") >= 4
