# content-guard: allow bearer-token file
"""Public Wazuh triage tools: ingest, classify, propose, and opaque status."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from brigade import fleet_client
from brigade.grokbot_wazuh.contracts import TOOLS, WazuhError
from brigade.grokbot_wazuh.store import WazuhStore
from brigade.grokbot_wazuh.tools import WazuhTriageTools

NOW = datetime(2026, 8, 28, 16, 0, tzinfo=timezone.utc)
SECRET = "ghp_exampleNotARealToken0000000000000001"
FORBIDDEN_PUBLIC = {
    "body",
    "title",
    "source_ref",
    "source_digest",
    "command",
    "credential",
    "password",
    "token",
    "secret",
    "path",
}


def _alert(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "rule_id": "504",
        "rule_level": 12,
        "rule_description": "Agent disconnected",
        "rule_groups": ["agent_disconnected"],
        "agent_id": "001",
        "decoder": "agent-buffer",
        "timestamp": "2026-08-28T16:00:00Z",
        "detail": f"Bearer {SECRET} agent keepalive stopped",
    }
    payload.update(overrides)
    return payload


def _tools(tmp_path: Path, monkeypatch) -> tuple[WazuhTriageTools, Path, Path]:
    home = tmp_path / "home"
    monkeypatch.setenv("BRIGADE_HOME", str(home))
    monkeypatch.delenv("BRIGADE_FLEET_HUB_URL", raising=False)
    monkeypatch.delenv("BRIGADE_FLEET_TOKEN", raising=False)
    monkeypatch.setattr(fleet_client, "report_event", lambda *_args, **_kwargs: True)
    queue = tmp_path / "queue"
    owner = tmp_path / "owner"
    queue.mkdir()
    owner.mkdir()
    store_path = tmp_path / "state" / "wazuh.json"
    store = WazuhStore(str(store_path))
    store.ready()
    tools = WazuhTriageTools(
        store=store,
        target=queue,
        owner=owner,
        now=lambda: NOW,
        request_id=lambda: "req-wazuh-1",
        create_proposal_id=lambda: "a" * 32,
    )
    return tools, queue, owner


def _public_keys(value: object) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        keys.update(value)
        for item in value.values():
            keys.update(_public_keys(item))
    elif isinstance(value, list):
        for item in value:
            keys.update(_public_keys(item))
    return keys


def test_ingest_dedupes_and_emits_one_review_finding(tmp_path: Path, monkeypatch):
    tools, queue, owner = _tools(tmp_path, monkeypatch)
    first = tools.call_tool("wazuh_ingest", {"alerts": [_alert()]})
    second = tools.call_tool("wazuh_ingest", {"alerts": [_alert()]})
    assert first["data"]["current"] == 1
    assert first["data"]["created"] == 1
    assert second["data"]["current"] == 1
    assert second["data"]["created"] == 0
    drafts = list((owner / "memory" / "handoff-inbox").glob("*.md"))
    assert len(drafts) == 1
    assert "untrusted" in drafts[0].read_text(encoding="utf-8").casefold()
    assert SECRET not in drafts[0].read_text(encoding="utf-8")
    outbox = list((queue / ".brigade" / "cloud" / "grokbot" / "outbox").glob("*.json"))
    assert len(outbox) == 1
    event = json.loads(outbox[0].read_text(encoding="utf-8"))["event"]
    assert set(event) == {"run_id", "seat", "harness", "state", "ts", "sequence", "digest"}
    assert SECRET not in json.dumps(event)
    assert SECRET not in json.dumps(first)
    assert SECRET not in json.dumps(second)
    assert FORBIDDEN_PUBLIC.isdisjoint(_public_keys(first))
    assert FORBIDDEN_PUBLIC.isdisjoint(_public_keys(second))


def test_status_classify_bundle_and_propose_stay_public_and_gated(tmp_path: Path, monkeypatch):
    tools, _queue, _owner = _tools(tmp_path, monkeypatch)
    ingested = tools.call_tool("wazuh_ingest", {"alerts": [_alert()]})
    fingerprint = ingested["data"]["fingerprints"][0]
    status = tools.call_tool("wazuh_alert_status", {})
    assert status["data"]["current"] == 1
    assert status["data"]["escalated"] == 1
    assert status["data"]["last_seen"] == "2026-08-28T16:00:00Z"
    classified = tools.call_tool("wazuh_classify", {"fingerprint": fingerprint})
    assert classified["data"]["category"] == "escalate"
    assert classified["data"]["reason"]
    bundle = tools.call_tool("wazuh_incident_bundle", {"scope": "agent-disconnected"})
    assert bundle["data"]["findings"]
    assert FORBIDDEN_PUBLIC.isdisjoint(_public_keys(bundle))
    proposed = tools.call_tool("wazuh_propose_remediation", {"finding_id": "001:agent-disconnected:504"})
    assert proposed["data"]["state"] == "proposed"
    assert proposed["data"]["proposal_id"] == "a" * 32
    action = tools.call_tool("wazuh_action_status", {"proposal_id": "a" * 32})
    assert action["data"]["state"] == "proposed"
    assert set(TOOLS) == set(tools.inventory())
    noise = tools.call_tool(
        "wazuh_ingest",
        {
            "alerts": [
                _alert(
                    rule_id="80790",
                    rule_level=3,
                    rule_description="SCA check failed",
                    rule_groups=["sca"],
                    decoder="sca",
                    detail="Repeated SCA compliance finding",
                    agent_id="002",
                )
            ]
        },
    )
    noise_id = "002:sca-repeat:80790"
    try:
        tools.call_tool("wazuh_propose_remediation", {"finding_id": noise_id})
        raised = False
    except WazuhError as exc:
        raised = True
        assert exc.code == "denied"
    assert raised
    assert "created" in noise["data"]


def test_identical_ingest_retries_after_relay_failure_and_pending_or_ready_outbox(tmp_path: Path, monkeypatch):
    from brigade.grokbot_findings import FindingsError
    from brigade.grokbot_wazuh import tools as tools_mod

    tools, queue, owner = _tools(tmp_path, monkeypatch)
    real_relay = tools_mod.grokbot_findings_relay.relay_apply

    def fail_relay(*_args, **_kwargs):
        raise FindingsError("unsafe-storage")

    monkeypatch.setattr(tools_mod.grokbot_findings_relay, "relay_apply", fail_relay)
    try:
        tools.call_tool("wazuh_ingest", {"alerts": [_alert()]})
        raised = False
    except FindingsError:
        raised = True
    assert raised
    assert tools.store.current_count() == 1
    monkeypatch.setattr(tools_mod.grokbot_findings_relay, "relay_apply", real_relay)
    recovered = tools.call_tool("wazuh_ingest", {"alerts": [_alert()]})
    assert recovered["data"]["current"] == 1
    drafts = list((owner / "memory" / "handoff-inbox").glob("*.md"))
    assert len(drafts) == 1
    outbox_dir = queue / ".brigade" / "cloud" / "grokbot" / "outbox"
    outbox = list(outbox_dir.glob("*.json"))
    assert len(outbox) == 1
    assert json.loads(outbox[0].read_text(encoding="utf-8"))["status"] == "reported"

    pending_clock = {"now": datetime(2026, 8, 28, 17, 0, tzinfo=timezone.utc)}
    pending_tools = WazuhTriageTools(
        store=WazuhStore(str(tmp_path / "pending-state" / "wazuh.json")),
        target=tmp_path / "pending-queue",
        owner=tmp_path / "pending-owner",
        now=lambda: pending_clock["now"],
        request_id=lambda: "req-wazuh-pending",
        create_proposal_id=lambda: "b" * 32,
    )
    (tmp_path / "pending-queue").mkdir()
    (tmp_path / "pending-owner").mkdir()
    pending_tools.store.ready()
    from brigade import grokbot_findings

    real_deliver = grokbot_findings._deliver_one

    def fail_after_pending(*args, **kwargs):
        raise FindingsError("interrupted-draft")

    monkeypatch.setattr(grokbot_findings, "_deliver_one", fail_after_pending)
    try:
        pending_tools.call_tool("wazuh_ingest", {"alerts": [_alert(agent_id="003")]})
        pending_raised = False
    except FindingsError:
        pending_raised = True
    assert pending_raised
    monkeypatch.setattr(grokbot_findings, "_deliver_one", real_deliver)
    pending_recovered = pending_tools.call_tool("wazuh_ingest", {"alerts": [_alert(agent_id="003")]})
    assert pending_recovered["data"]["current"] == 1
    pending_outbox = list((tmp_path / "pending-queue" / ".brigade" / "cloud" / "grokbot" / "outbox").glob("*.json"))
    assert pending_outbox
    assert json.loads(pending_outbox[0].read_text(encoding="utf-8"))["status"] == "reported"

    ready_calls = {"count": 0}

    def report_ready_then_true(*_args, **_kwargs):
        ready_calls["count"] += 1
        return ready_calls["count"] > 1

    (tmp_path / "ready").mkdir()
    ready_tools, ready_queue, _ready_owner = _tools(tmp_path / "ready", monkeypatch)
    monkeypatch.setattr(fleet_client, "report_event", report_ready_then_true)
    first_ready = ready_tools.call_tool("wazuh_ingest", {"alerts": [_alert(agent_id="004")]})
    assert first_ready["data"]["current"] == 1
    ready_outbox = list((ready_queue / ".brigade" / "cloud" / "grokbot" / "outbox").glob("*.json"))
    assert json.loads(ready_outbox[0].read_text(encoding="utf-8"))["status"] == "ready"
    second_ready = ready_tools.call_tool("wazuh_ingest", {"alerts": [_alert(agent_id="004")]})
    assert second_ready["data"]["created"] == 0
    ready_outbox = list((ready_queue / ".brigade" / "cloud" / "grokbot" / "outbox").glob("*.json"))
    assert json.loads(ready_outbox[0].read_text(encoding="utf-8"))["status"] == "reported"


def test_ingest_batch_preflights_capacity_and_does_not_persist_a_prefix(tmp_path: Path, monkeypatch):
    from brigade.grokbot_wazuh import store as store_mod

    monkeypatch.setattr(store_mod, "MAX_ALERTS", 2)
    tools, _queue, _owner = _tools(tmp_path, monkeypatch)
    tools.call_tool("wazuh_ingest", {"alerts": [_alert(agent_id="001")]})
    assert tools.store.current_count() == 1
    try:
        tools.call_tool(
            "wazuh_ingest",
            {"alerts": [_alert(agent_id="002"), _alert(agent_id="003")]},
        )
        raised = False
        code = ""
    except WazuhError as exc:
        raised = True
        code = exc.code
    assert raised
    assert code == "unavailable"
    assert tools.store.current_count() == 1
    assert tools.store.get_finding("002:agent-disconnected:504") is None
    assert tools.store.get_finding("003:agent-disconnected:504") is None


def test_semantic_change_updates_stored_fields_and_relays_severity_escalation(tmp_path: Path, monkeypatch):
    tools, queue, owner = _tools(tmp_path, monkeypatch)
    first = tools.call_tool(
        "wazuh_ingest",
        {"alerts": [_alert(rule_level=8, rule_description="Agent disconnected", detail="first body")]},
    )
    fingerprint = first["data"]["fingerprints"][0]
    stored = tools.store.get_alert(fingerprint)
    assert stored is not None
    first_revision = stored["revision"]
    assert stored["severity"] == "warning"
    assert stored["fingerprint"] == fingerprint
    second = tools.call_tool(
        "wazuh_ingest",
        {
            "alerts": [
                _alert(
                    rule_level=15,
                    rule_description="Agent disconnected critical",
                    detail="escalated body",
                )
            ]
        },
    )
    assert second["data"]["current"] == 1
    assert second["data"]["created"] == 0
    updated = tools.store.get_alert(fingerprint)
    assert updated is not None
    assert updated["fingerprint"] == fingerprint
    assert updated["severity"] == "critical"
    assert updated["title"] == "Agent disconnected critical"
    assert updated["body"] == "escalated body"
    assert updated["revision"] != first_revision
    assert updated["content_digest"] != stored["content_digest"]
    drafts = list((owner / "memory" / "handoff-inbox").glob("*.md"))
    assert len(drafts) == 2
    events = list((queue / ".brigade" / "cloud" / "grokbot" / "outbox").glob("*.json"))
    assert len(events) == 2
    states = {json.loads(path.read_text(encoding="utf-8"))["event"]["state"] for path in events}
    assert "finding.warning" in states
    assert "finding.critical" in states


def test_action_status_reports_and_persists_expired_after_clock_passes(tmp_path: Path, monkeypatch):
    clock = {"now": NOW}
    home = tmp_path / "home"
    monkeypatch.setenv("BRIGADE_HOME", str(home))
    monkeypatch.delenv("BRIGADE_FLEET_HUB_URL", raising=False)
    monkeypatch.delenv("BRIGADE_FLEET_TOKEN", raising=False)
    monkeypatch.setattr(fleet_client, "report_event", lambda *_args, **_kwargs: True)
    queue = tmp_path / "queue"
    owner = tmp_path / "owner"
    queue.mkdir()
    owner.mkdir()
    store = WazuhStore(str(tmp_path / "state" / "wazuh.json"))
    store.ready()
    tools = WazuhTriageTools(
        store=store,
        target=queue,
        owner=owner,
        now=lambda: clock["now"],
        request_id=lambda: "req-wazuh-expire",
        create_proposal_id=lambda: "c" * 32,
    )
    tools.call_tool("wazuh_ingest", {"alerts": [_alert()]})
    proposed = tools.call_tool("wazuh_propose_remediation", {"finding_id": "001:agent-disconnected:504"})
    assert proposed["data"]["state"] == "proposed"
    from datetime import timedelta

    clock["now"] = NOW + timedelta(hours=2)
    action = tools.call_tool("wazuh_action_status", {"proposal_id": "c" * 32})
    assert action["data"]["state"] == "expired"
    persisted = store.get_proposal("c" * 32)
    assert persisted is not None
    assert persisted["state"] == "expired"
    later = tools.call_tool("wazuh_action_status", {"proposal_id": "c" * 32})
    assert later["data"]["state"] == "expired"


def test_multi_alert_ingest_relays_every_pending_entry_and_retries_safely(tmp_path: Path, monkeypatch):
    from brigade.grokbot_findings import FindingsError
    from brigade.grokbot_wazuh import tools as tools_mod

    tools, queue, owner = _tools(tmp_path, monkeypatch)
    first = tools.call_tool(
        "wazuh_ingest",
        {"alerts": [_alert(agent_id="001"), _alert(agent_id="002")]},
    )
    assert first["data"]["created"] == 2
    assert first["data"]["current"] == 2
    drafts = list((owner / "memory" / "handoff-inbox").glob("*.md"))
    assert len(drafts) == 2
    outbox = list((queue / ".brigade" / "cloud" / "grokbot" / "outbox").glob("*.json"))
    assert len(outbox) == 2
    assert {json.loads(path.read_text(encoding="utf-8"))["status"] for path in outbox} == {"reported"}
    for fingerprint in first["data"]["fingerprints"]:
        stored = tools.store.get_alert(fingerprint)
        assert stored is not None
        assert stored["relay_status"] == "reported"

    real_relay = tools_mod.grokbot_findings_relay.relay_apply

    def fail_relay(*_args, **_kwargs):
        raise FindingsError("unsafe-storage")

    monkeypatch.setattr(tools_mod.grokbot_findings_relay, "relay_apply", fail_relay)
    try:
        tools.call_tool(
            "wazuh_ingest",
            {"alerts": [_alert(agent_id="003"), _alert(agent_id="004")]},
        )
        raised = False
    except FindingsError:
        raised = True
    assert raised
    failed = [
        tools.store.get_finding("003:agent-disconnected:504"),
        tools.store.get_finding("004:agent-disconnected:504"),
    ]
    assert all(item is not None and item["relay_status"] == "pending" for item in failed)
    monkeypatch.setattr(tools_mod.grokbot_findings_relay, "relay_apply", real_relay)
    recovered = tools.call_tool(
        "wazuh_ingest",
        {"alerts": [_alert(agent_id="003"), _alert(agent_id="004")]},
    )
    assert recovered["data"]["created"] == 0
    assert recovered["data"]["current"] == 4
    drafts = list((owner / "memory" / "handoff-inbox").glob("*.md"))
    assert len(drafts) == 4
    outbox = list((queue / ".brigade" / "cloud" / "grokbot" / "outbox").glob("*.json"))
    assert len(outbox) == 4
    assert {json.loads(path.read_text(encoding="utf-8"))["status"] for path in outbox} == {"reported"}
    for finding_id in ("003:agent-disconnected:504", "004:agent-disconnected:504"):
        stored = tools.store.get_finding(finding_id)
        assert stored is not None
        assert stored["relay_status"] == "reported"


def test_same_agent_same_class_different_rule_ids_keep_distinct_identities(tmp_path: Path, monkeypatch):
    tools, queue, owner = _tools(tmp_path, monkeypatch)
    first = tools.call_tool(
        "wazuh_ingest",
        {
            "alerts": [
                _alert(rule_id="501", rule_description="Agent disconnected"),
                _alert(rule_id="504", rule_description="Agent disconnected"),
            ]
        },
    )
    assert first["data"]["created"] == 2
    assert first["data"]["current"] == 2
    assert first["data"]["fingerprints"][0] != first["data"]["fingerprints"][1]
    stored = [
        tools.store.get_finding("001:agent-disconnected:501"),
        tools.store.get_finding("001:agent-disconnected:504"),
    ]
    assert all(item is not None for item in stored)
    assert stored[0]["finding_id"] != stored[1]["finding_id"]
    assert stored[0]["rule_class"] == stored[1]["rule_class"] == "agent-disconnected"
    drafts = list((owner / "memory" / "handoff-inbox").glob("*.md"))
    assert len(drafts) == 2
    outbox = list((queue / ".brigade" / "cloud" / "grokbot" / "outbox").glob("*.json"))
    assert len(outbox) == 2
    assert {json.loads(path.read_text(encoding="utf-8"))["status"] for path in outbox} == {"reported"}
    replay = tools.call_tool(
        "wazuh_ingest",
        {
            "alerts": [
                _alert(rule_id="501", rule_description="Agent disconnected"),
                _alert(rule_id="504", rule_description="Agent disconnected"),
            ]
        },
    )
    assert replay["data"]["created"] == 0
    assert replay["data"]["current"] == 2
    drafts = list((owner / "memory" / "handoff-inbox").glob("*.md"))
    assert len(drafts) == 2
    outbox = list((queue / ".brigade" / "cloud" / "grokbot" / "outbox").glob("*.json"))
    assert len(outbox) == 2
    assert {json.loads(path.read_text(encoding="utf-8"))["status"] for path in outbox} == {"reported"}
    for finding_id in ("001:agent-disconnected:501", "001:agent-disconnected:504"):
        item = tools.store.get_finding(finding_id)
        assert item is not None
        assert item["relay_status"] == "reported"


def test_expired_suppression_is_not_reported_as_suppressed_by_status_or_bundle(tmp_path: Path, monkeypatch):
    from datetime import timedelta

    clock = {"now": NOW}
    home = tmp_path / "home"
    monkeypatch.setenv("BRIGADE_HOME", str(home))
    monkeypatch.delenv("BRIGADE_FLEET_HUB_URL", raising=False)
    monkeypatch.delenv("BRIGADE_FLEET_TOKEN", raising=False)
    monkeypatch.setattr(fleet_client, "report_event", lambda *_args, **_kwargs: True)
    queue = tmp_path / "queue"
    owner = tmp_path / "owner"
    queue.mkdir()
    owner.mkdir()
    store = WazuhStore(str(tmp_path / "state" / "wazuh.json"))
    store.ready()
    tools = WazuhTriageTools(
        store=store,
        target=queue,
        owner=owner,
        now=lambda: clock["now"],
        request_id=lambda: "req-wazuh-suppression",
        create_proposal_id=lambda: "d" * 32,
    )
    ingested = tools.call_tool(
        "wazuh_ingest",
        {
            "alerts": [
                _alert(
                    rule_id="80790",
                    rule_level=3,
                    rule_description="SCA check failed",
                    rule_groups=["sca"],
                    decoder="sca",
                    detail="Repeated SCA compliance finding",
                    agent_id="002",
                )
            ]
        },
    )
    assert ingested["data"]["created"] == 1
    active = tools.call_tool("wazuh_alert_status", {})
    assert active["data"]["suppressed"] == 1
    assert active["data"]["watched"] == 0
    clock["now"] = NOW + timedelta(days=8)
    expired = tools.call_tool("wazuh_alert_status", {})
    assert expired["data"]["suppressed"] == 0
    assert expired["data"]["watched"] == 1
    bundle = tools.call_tool("wazuh_incident_bundle", {"scope": "sca-repeat"})
    assert bundle["data"]["findings"]
    assert {item["category"] for item in bundle["data"]["findings"]} == {"watch"}
