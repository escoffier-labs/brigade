"""Classification precedence, expiry, and action eligibility."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from brigade.grokbot_wazuh.contracts import parse_ingest_input
from brigade.grokbot_wazuh.normalize import normalize_alert
from brigade.grokbot_wazuh.policy import classify, is_action_eligible

NOW = datetime(2026, 8, 28, 16, 0, tzinfo=timezone.utc)


def _record(**overrides: object) -> dict[str, object]:
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
    alert = parse_ingest_input({"alerts": [payload]})["alerts"][0]
    return normalize_alert(alert)


def test_known_noise_expires_to_watch_and_high_confidence_event_escalates():
    sca = _record(
        rule_id="80790",
        rule_level=3,
        rule_description="SCA check failed",
        rule_groups=["sca"],
        decoder="sca",
        detail="Repeated SCA compliance finding",
    )
    installer = _record(
        rule_id="18107",
        rule_level=5,
        rule_description="Windows installer event",
        rule_groups=["windows"],
        decoder="windows_eventchannel",
        detail="Expected installer noise",
    )
    disconnected = _record()
    sca_now = classify(sca, suppressions=(), now=NOW)
    assert sca_now["category"] == "suppress"
    assert sca_now["expires_at"]
    assert not is_action_eligible(sca_now)
    expired = classify(
        sca,
        suppressions=(
            {
                "reason": "sca-repeat",
                "fingerprint": sca["fingerprint"],
                "scope": "sca-repeat",
                "created_at": (NOW - timedelta(days=8)).isoformat().replace("+00:00", "Z"),
                "expires_at": (NOW - timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
            },
        ),
        now=NOW,
    )
    assert expired["category"] == "watch"
    assert not is_action_eligible(expired)
    installer_now = classify(installer, suppressions=(), now=NOW)
    assert installer_now["category"] in {"suppress", "watch"}
    assert installer_now["expires_at"] or installer_now["category"] == "watch"
    assert not is_action_eligible(installer_now)
    escalated = classify(disconnected, suppressions=(), now=NOW)
    assert escalated["category"] == "escalate"
    assert escalated["reason"]
    assert is_action_eligible(escalated)


def test_malformed_unknown_and_explicit_suppression_follow_precedence():
    unknown = _record(
        rule_id="99999",
        rule_level=7,
        rule_description="Unrecognized decoder event",
        rule_groups=["local"],
        decoder="unknown-decoder",
        detail="No catalog match",
    )
    classified = classify(unknown, suppressions=(), now=NOW)
    assert classified["category"] == "watch"
    assert not is_action_eligible(classified)
    fingerprint = unknown["fingerprint"]
    suppressed = classify(
        unknown,
        suppressions=(
            {
                "reason": "operator-noise",
                "fingerprint": fingerprint,
                "scope": "unknown",
                "created_at": NOW.isoformat().replace("+00:00", "Z"),
                "expires_at": (NOW + timedelta(days=1)).isoformat().replace("+00:00", "Z"),
            },
        ),
        now=NOW,
    )
    assert suppressed["category"] == "suppress"
    assert suppressed["reason"] == "operator-noise"
    assert not is_action_eligible(suppressed)
    port_change = _record(
        rule_id="550",
        rule_level=7,
        rule_description="Port change observed",
        rule_groups=["syslog"],
        decoder="netstat",
        detail="Listening port set changed",
    )
    watched = classify(port_change, suppressions=(), now=NOW)
    assert watched["category"] == "watch"
    assert not is_action_eligible(watched)
