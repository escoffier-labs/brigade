"""Approval-gated Fleet Steward remediation bound to Wazuh triage contracts."""

from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timezone
from typing import Any, Mapping

import pytest

from brigade.grokbot_fleet.actions import FleetActionStoreError
from brigade.grokbot_fleet.contracts import ERROR_MESSAGES, FleetError
from brigade.grokbot_fleet.ledger import FleetLedger
from brigade.grokbot_fleet.policy import WAZUH_ACTION_CATALOG, bind_wazuh_remediation
from brigade.grokbot_fleet.probes import FleetProbes
from brigade.grokbot_fleet.registry import create_fleet_registry
from brigade.grokbot_fleet.runtime_config import parse_fleet_private_runtime, project_fleet_public_registry
from brigade.grokbot_fleet.tools import FleetStewardTools
from brigade.grokbot_wazuh.contracts import parse_ingest_input
from brigade.grokbot_wazuh.normalize import normalize_alert

WINDOW_NOW = datetime(2026, 8, 28, 2, 30, tzinfo=timezone.utc)
RUNTIME = {
    "version": 1,
    "roles": {
        "control-plane": {"adapter": "local"},
        "hypervisor": {"adapter": "linux", "ssh_alias": "lab-hypervisor"},
        "worker": {"adapter": "linux", "ssh_alias": "lab-worker.01"},
    },
    "services": {
        "research-bridge": {"unit": "research-bridge.service", "manager": "user"},
        "virtualization-api": {"unit": "virtualization-api.service", "manager": "system"},
        "overlay-network": {"unit": "overlay-network.service", "manager": "system"},
    },
}
SENSITIVE_MARKER = "sensitive-marker-do-not-emit"
FORBIDDEN = (
    SENSITIVE_MARKER,
    "/etc/shadow",
    "bash -c",
    "curl http",
    "lab-hypervisor",
    "research-bridge.service",
)


class _Clock:
    def __init__(self, stamp: datetime):
        self.stamp = stamp

    def __call__(self) -> datetime:
        return self.stamp


class _WazuhSource:
    def __init__(self, findings: list[dict[str, Any]] | None = None):
        self.findings = {item["finding_id"]: dict(item) for item in findings or []}
        self.calls: list[tuple[str, str]] = []

    def current_finding(self, finding_id: str) -> dict[str, Any] | None:
        self.calls.append(("finding", finding_id))
        found = self.findings.get(finding_id)
        return None if found is None else dict(found)

    def suppressions(self) -> list[dict[str, str]]:
        return []


class _Remediator:
    def __init__(
        self,
        *,
        health: str = "unhealthy",
        revision: str = "c" * 64,
        verify_ok: bool = True,
        execute_error: Exception | None = None,
        verify_error: Exception | None = None,
    ):
        self.health = health
        self.revision = revision
        self.verify_ok = verify_ok
        self.execute_error = execute_error
        self.verify_error = verify_error
        self.calls: list[tuple[str, str]] = []

    def recheck(self, action_id: str) -> dict[str, str]:
        self.calls.append(("recheck", action_id))
        return {"health_class": self.health, "system_revision": self.revision}

    def execute(self, action_id: str) -> None:
        self.calls.append(("execute", action_id))
        if self.execute_error is not None:
            raise self.execute_error

    def verify(self, verification_id: str) -> bool:
        self.calls.append(("verify", verification_id))
        if self.verify_error is not None:
            raise self.verify_error
        return self.verify_ok

    def rollback(self, rollback_id: str) -> None:
        self.calls.append(("rollback", rollback_id))


class _Claims:
    def __init__(
        self,
        *,
        grant: bool = True,
        release_granted: bool = True,
        release_error: Exception | None = None,
        release_reason: str = "",
        release_holder: str | None = None,
    ):
        self.grant = grant
        self.release_granted = release_granted
        self.release_error = release_error
        self.release_reason = release_reason
        self.release_holder = release_holder
        self.calls: list[tuple[str, str]] = []
        self.held: list[dict[str, str]] = []
        self.released: list[dict[str, str]] = []

    def acquire(self, target_alias: str) -> dict[str, str]:
        self.calls.append(("acquire", target_alias))
        if not self.grant:
            raise FleetError("unavailable", ERROR_MESSAGES["unavailable"])
        claim = {"target_alias": target_alias, "holder": "b" * 32}
        self.held.append(claim)
        return claim

    def release(self, claim: Mapping[str, str]) -> dict[str, Any]:
        self.calls.append(("release", str(claim["target_alias"])))
        self.released.append(dict(claim))
        if self.release_error is not None:
            raise self.release_error
        if self.release_granted:
            self.held = [item for item in self.held if item["holder"] != claim.get("holder")]
        return {
            "granted": self.release_granted,
            "holder": self.release_holder or str(claim["holder"]),
            "reason": self.release_reason,
            "target_alias": str(claim["target_alias"]),
        }


class _Store:
    def __init__(
        self,
        *,
        reserve_error: Exception | None = None,
        commit_error: Exception | None = None,
        consume_error: Exception | None = None,
        release_error: Exception | None = None,
    ) -> None:
        self.proposals: dict[str, dict[str, Any]] = {}
        self.approvals: dict[str, dict[str, Any]] = {}
        self.consumed: dict[str, dict[str, Any]] = {}
        self.receipts: list[dict[str, Any]] = []
        self.reservations: dict[str, list[dict[str, Any]]] = {}
        self.cleanup: dict[str, Any] | None = None
        self.calls: list[str] = []
        self.reserve_error = reserve_error
        self.commit_error = commit_error
        self.consume_error = consume_error
        self.release_error = release_error
        self.before_commit = None
        self.cleanup_fail_on_call: int | None = None
        self.cleanup_writes = 0

    def create_proposal(self, record: Mapping[str, Any]) -> None:
        self.calls.append("create_proposal")
        self.proposals[record["proposal_id"]] = dict(record)

    def read_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        self.calls.append("read_proposal")
        found = self.proposals.get(proposal_id)
        return None if found is None else dict(found)

    def read_approval(self, proposal_id: str) -> dict[str, Any] | None:
        self.calls.append("read_approval")
        found = self.approvals.get(proposal_id)
        return None if found is None else dict(found)

    def is_consumed(self, proposal_id: str) -> bool:
        return proposal_id in self.consumed

    def claim_consumed(self, record: Mapping[str, Any]) -> dict[str, Any]:
        self.calls.append("claim_consumed")
        if self.consume_error is not None:
            raise self.consume_error
        if record["proposal_id"] in self.consumed:
            raise FleetActionStoreError("replayed")
        stored = dict(record)
        self.consumed[record["proposal_id"]] = stored
        return stored

    def write_receipt(self, record: Mapping[str, Any]) -> None:
        self.calls.append("write_receipt")
        if self.reservations:
            raise FleetActionStoreError("denied")
        self.receipts.append(dict(record))

    def reserve_receipt_capacity(self, records: list[Mapping[str, Any]]) -> str:
        self.calls.append("reserve_receipts")
        if self.reserve_error is not None:
            raise self.reserve_error
        reservation_id = "1" * 32
        self.reservations[reservation_id] = [dict(item) for item in records]
        return reservation_id

    def commit_reserved_receipts(self, reservation_id: str, records: list[Mapping[str, Any]]) -> None:
        self.calls.append("commit_receipts")
        if self.commit_error is not None:
            raise self.commit_error
        reserved = {item["receipt_id"]: item for item in self.reservations[reservation_id]}
        if len({record["receipt_id"] for record in records}) != len(records):
            raise FleetActionStoreError("denied")
        for record in records:
            expected = reserved.get(record["receipt_id"])
            if expected is None or any(record.get(key) != value for key, value in expected.items() if key != "outcome"):
                raise FleetActionStoreError("denied")
        if self.before_commit is not None:
            self.before_commit(reservation_id, records)
        for record in records:
            self.receipts.append(dict(record))
        self.reservations.pop(reservation_id, None)

    def release_receipt_reservation(self, reservation_id: str) -> None:
        self.calls.append("release_reservation")
        if self.release_error is not None:
            raise self.release_error
        self.reservations.pop(reservation_id, None)

    def write_claim_cleanup(self, record: Mapping[str, Any]) -> None:
        self.calls.append("write_cleanup")
        self.cleanup_writes += 1
        if self.cleanup_fail_on_call == self.cleanup_writes:
            raise OSError("cleanup write failed")
        self.cleanup = dict(record)

    def read_claim_cleanup(self) -> dict[str, Any] | None:
        self.calls.append("read_cleanup")
        return None if self.cleanup is None else dict(self.cleanup)

    def clear_claim_cleanup(self) -> None:
        self.calls.append("clear_cleanup")
        self.cleanup = None


def _alert(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "rule_id": "533",
        "rule_level": 12,
        "rule_description": "Service failure",
        "rule_groups": ["service_availability"],
        "agent_id": "001",
        "decoder": "systemd",
        "timestamp": "2026-08-28T02:30:00Z",
        "detail": f"unit failed after token {SENSITIVE_MARKER}",
    }
    payload.update(overrides)
    return normalize_alert(parse_ingest_input({"alerts": [payload]})["alerts"][0])


def _host_raw(alias: str) -> dict[str, object]:
    return {
        "alias": alias,
        "probe_id": "linux-host-summary-v1",
        "observed_at": "2026-08-28T02:30:00Z",
        "reachability": "reachable",
        "uptime_seconds": 100,
        "storage_percent": 10,
        "failed_services": 0,
        "reboot_pending": False,
        "detail": "ok",
    }


def _tools(
    tmp_path,
    *,
    wazuh: _WazuhSource | None = None,
    remediator: _Remediator | None = None,
    claims: _Claims | None = None,
    store: _Store | None = None,
    clock: _Clock | None = None,
    registry=None,
):
    chosen_registry = registry or project_fleet_public_registry(parse_fleet_private_runtime(RUNTIME))
    ledger = FleetLedger(str(tmp_path / "ledger.json"))
    ledger.ready()

    def observe_host_raw(target, probe_id):
        return _host_raw(target["alias"])

    def observe_service_raw(target, service):
        return {
            "service_id": service["service_id"],
            "target_alias": target["alias"],
            "probe_id": service["probe_id"],
            "observed_at": "2026-08-28T02:30:00Z",
            "health_class": "unhealthy",
            "detail": "Service health is available",
        }

    probes = FleetProbes(
        registry=chosen_registry,
        observe_host_raw=observe_host_raw,
        observe_service_raw=observe_service_raw,
        now=lambda: (clock or _Clock(WINDOW_NOW))(),
        create_receipt_ref=lambda: "receipt-1",
        secrets=[SENSITIVE_MARKER],
    )
    ids = iter(f"{index:032x}" for index in range(10, 40))
    return FleetStewardTools(
        registry=chosen_registry,
        probes=probes,
        ledger=ledger,
        store=store or _Store(),
        executor=_Remediator(),
        now=clock or _Clock(WINDOW_NOW),
        request_id=lambda: "req-remediation-1",
        create_proposal_id=lambda: next(ids),
        create_nonce=lambda: next(ids),
        create_receipt_id=lambda: next(ids),
        secrets=[SENSITIVE_MARKER],
        wazuh_source=wazuh,
        remediator=remediator or _Remediator(),
        claims=claims or _Claims(),
    )


def _approve(store: _Store, proposal_id: str, approved_at: str = "2026-08-28T02:31:00Z") -> None:
    proposal = store.proposals[proposal_id]
    store.approvals[proposal_id] = {
        **{key: proposal[key] for key in proposal if key != "created_at"},
        "approved_at": approved_at,
    }


def _assert_clean(value: object) -> None:
    text = repr(value)
    for needle in FORBIDDEN:
        assert needle not in text


def test_proposal_requires_escalated_current_finding_and_operator_approval(tmp_path):
    catalog = WAZUH_ACTION_CATALOG["service-failure"]
    assert catalog["action_id"] == "restart-service"
    assert catalog["verification_id"] == "verify-service"
    assert catalog["rollback_id"] == "no-rollback"
    assert catalog["target_alias"] == "control-plane"
    assert catalog["maintenance_window_id"]
    assert catalog["blast_radius"] == "one registered service"
    for key in ("command", "path", "username", "address", "credential", "environment"):
        assert key not in catalog

    watch = _alert(
        rule_id="550",
        rule_level=7,
        rule_description="Port change",
        rule_groups=["syslog"],
        decoder="syslog",
        detail="Observed port change",
    )
    suppress = _alert(
        rule_id="80790",
        rule_level=3,
        rule_description="SCA check failed",
        rule_groups=["sca"],
        decoder="sca",
        detail="Repeated SCA compliance finding",
    )
    current = _alert()
    wrong_target = _alert(agent_id="002")
    remediator = _Remediator()
    store = _Store()
    tools = _tools(
        tmp_path,
        wazuh=_WazuhSource([watch, suppress, current, wrong_target]),
        remediator=remediator,
        store=store,
    )

    for finding in (watch, suppress, wrong_target):
        with pytest.raises(FleetError) as caught:
            tools.propose_remediation({"finding_id": finding["finding_id"]})
        assert caught.value.code == "denied"
        _assert_clean(caught.value)

    bound = bind_wazuh_remediation(current, registry=tools.registry, now=WINDOW_NOW)
    assert bound["target_alias"] == "control-plane"
    assert bound["wazuh_fingerprint"] == current["fingerprint"]
    assert bound["finding_revision"] == current["revision"]

    proposed = tools.propose_remediation({"finding_id": current["finding_id"]})
    proposal_id = proposed["data"]["proposal_id"]
    assert proposed["data"]["execution_available"] is True
    assert proposed["data"]["approval_required"] is True
    assert "command" not in proposed["data"]
    _assert_clean(proposed)

    with pytest.raises(FleetError) as missing:
        tools.execute_remediation({"proposal_id": proposal_id})
    assert missing.value.code == "denied"
    _assert_clean(missing.value)

    _approve(store, proposal_id)
    stale_source = tools.wazuh_source
    assert stale_source is not None
    stale_source.findings[current["finding_id"]]["revision"] = "d" * 64
    with pytest.raises(FleetError) as stale:
        tools.execute_remediation({"proposal_id": proposal_id})
    assert stale.value.code == "denied"
    stale_source.findings[current["finding_id"]]["revision"] = current["revision"]

    store.approvals[proposal_id]["expires_at"] = "2026-08-28T02:00:00Z"
    with pytest.raises(FleetError) as expired:
        tools.execute_remediation({"proposal_id": proposal_id})
    assert expired.value.code == "denied"

    assert [kind for kind, _name in remediator.calls if kind in {"execute", "rollback"}] == []


def test_execute_claims_rechecks_verifies_and_records_receipts(tmp_path, monkeypatch):
    current = _alert()
    remediator = _Remediator()
    claims = _Claims()
    store = _Store()
    wazuh = _WazuhSource([current])
    tools = _tools(tmp_path, wazuh=wazuh, remediator=remediator, claims=claims, store=store)
    proposed = tools.propose_remediation({"finding_id": current["finding_id"]})
    proposal_id = proposed["data"]["proposal_id"]
    _approve(store, proposal_id)
    remediator.calls.clear()
    wazuh.calls.clear()
    store.calls.clear()
    claims.calls.clear()

    executed = tools.execute_remediation({"proposal_id": proposal_id})
    assert executed["data"]["outcome"] == "verified"
    assert executed["data"]["action_id"] == "restart-service"
    _assert_clean(executed)
    assert remediator.calls == [
        ("recheck", "restart-service"),
        ("execute", "restart-service"),
        ("verify", "verify-service"),
    ]
    assert wazuh.calls == [("finding", current["finding_id"])]
    store_work = [call for call in store.calls if call != "read_cleanup"]
    assert store_work[:2] == ["read_proposal", "read_approval"]
    assert "write_receipt" in store.calls or "commit_receipts" in store.calls
    assert claims.calls == [("acquire", "control-plane"), ("release", "control-plane")]
    assert claims.held == []
    assert not any("bash" in name or " " in name for _kind, name in remediator.calls)

    with pytest.raises(FleetError) as replayed:
        tools.execute_remediation({"proposal_id": proposal_id})
    assert replayed.value.code == "denied"
    assert remediator.calls == [
        ("recheck", "restart-service"),
        ("execute", "restart-service"),
        ("verify", "verify-service"),
    ]

    failed_store = _Store()
    failed_claims = _Claims()
    failed_remediator = _Remediator(verify_ok=False)
    failed_tools = _tools(
        tmp_path / "fail",
        wazuh=_WazuhSource([current]),
        remediator=failed_remediator,
        claims=failed_claims,
        store=failed_store,
    )
    failed_proposed = failed_tools.propose_remediation({"finding_id": current["finding_id"]})
    failed_id = failed_proposed["data"]["proposal_id"]
    _approve(failed_store, failed_id)
    failed_remediator.calls.clear()
    with pytest.raises(FleetError):
        failed_tools.execute_remediation({"proposal_id": failed_id})
    assert failed_remediator.calls == [
        ("recheck", "restart-service"),
        ("execute", "restart-service"),
        ("verify", "verify-service"),
    ]
    assert failed_claims.calls[-1] == ("release", "control-plane")
    assert failed_claims.held == []
    assert any(item.get("outcome") == "failed" for item in failed_store.receipts)
    assert not any(kind == "rollback" for kind, _name in failed_remediator.calls)

    from brigade.grokbot_fleet import policy as policy_mod

    monkeypatch.setitem(policy_mod.WAZUH_ACTION_CATALOG["service-failure"], "automatic_rollback", True)
    no_rb_store = _Store()
    no_rb_claims = _Claims()
    no_rb_remediator = _Remediator(verify_ok=False)
    no_rb_tools = _tools(
        tmp_path / "norb",
        wazuh=_WazuhSource([current]),
        remediator=no_rb_remediator,
        claims=no_rb_claims,
        store=no_rb_store,
    )
    no_rb_proposed = no_rb_tools.propose_remediation({"finding_id": current["finding_id"]})
    no_rb_id = no_rb_proposed["data"]["proposal_id"]
    _approve(no_rb_store, no_rb_id)
    no_rb_remediator.calls.clear()
    with pytest.raises(FleetError):
        no_rb_tools.execute_remediation({"proposal_id": no_rb_id})
    assert [kind for kind, _name in no_rb_remediator.calls] == ["recheck", "execute", "verify"]
    assert any(item.get("outcome") == "failed" for item in no_rb_store.receipts)
    assert no_rb_claims.held == []

    lost_store = _Store()
    lost_claims = _Claims(grant=False)
    lost_remediator = _Remediator()
    lost_tools = _tools(
        tmp_path / "lost",
        wazuh=_WazuhSource([current]),
        remediator=lost_remediator,
        claims=lost_claims,
        store=lost_store,
    )
    lost_proposed = lost_tools.propose_remediation({"finding_id": current["finding_id"]})
    lost_id = lost_proposed["data"]["proposal_id"]
    _approve(lost_store, lost_id)
    lost_remediator.calls.clear()
    with pytest.raises(FleetError) as lost:
        lost_tools.execute_remediation({"proposal_id": lost_id})
    assert lost.value.code == "unavailable"
    assert [kind for kind, _name in lost_remediator.calls] == ["recheck"]
    assert lost_claims.held == []


def test_current_wazuh_binding_must_match_every_catalogued_field(tmp_path):
    from brigade.grokbot_fleet import policy as policy_mod

    changes = {
        "action_id": "different-action",
        "automatic_rollback": True,
        "blast_radius": "two registered services",
        "maintenance_window_id": "alternate-control-plane-service",
        "rollback_id": "different-rollback",
        "service_id": "different-service",
        "verification_id": "different-verification",
    }
    for field, replacement in changes.items():
        current = _alert()
        remediator = _Remediator()
        store = _Store()
        tools = _tools(
            tmp_path / field,
            wazuh=_WazuhSource([current]),
            remediator=remediator,
            store=store,
        )
        proposal = tools.propose_remediation({"finding_id": current["finding_id"]})
        _approve(store, proposal["data"]["proposal_id"])
        remediator.calls.clear()
        catalog = policy_mod.WAZUH_ACTION_CATALOG["service-failure"]
        original = catalog[field]
        if field == "maintenance_window_id":
            policy_mod.MAINTENANCE_WINDOWS[replacement] = dict(policy_mod.MAINTENANCE_WINDOWS[original])
            policy_mod.MAINTENANCE_WINDOWS[replacement]["window_id"] = replacement
        catalog[field] = replacement
        try:
            with pytest.raises(FleetError) as caught:
                tools.execute_remediation({"proposal_id": proposal["data"]["proposal_id"]})
            assert caught.value.code == "denied"
            assert remediator.calls == []
        finally:
            catalog[field] = original
            if field == "maintenance_window_id":
                del policy_mod.MAINTENANCE_WINDOWS[replacement]


def test_reservation_contract_and_recovery_journal_precede_terminal_commit(tmp_path):
    store = _Store()
    tools, proposal_id, _remediator, claims, store = _ready_approved(tmp_path, store=store)
    observed: list[list[dict[str, Any]]] = []

    def capture_before_commit(_reservation_id: str, records: list[Mapping[str, Any]]) -> None:
        journal = store.read_claim_cleanup()
        assert isinstance(journal, dict)
        assert journal["receipt_pending"] is True
        assert journal["release_pending"] is True
        assert journal["terminal_receipts"] == [dict(record) for record in records]
        observed.append([dict(record) for record in records])

    store.before_commit = capture_before_commit
    result = tools.execute_remediation({"proposal_id": proposal_id})
    assert result["data"]["outcome"] == "verified"
    assert len(observed) == 1
    assert claims.calls == [("acquire", "control-plane"), ("release", "control-plane")]


def test_non_catalogued_wazuh_findings_remain_review_only(tmp_path):
    remediator = _Remediator()
    disconnected = _alert(
        rule_id="504",
        rule_level=12,
        rule_description="Agent disconnected",
        rule_groups=["agent_disconnected"],
        decoder="agent-buffer",
        detail="Wazuh agent stopped sending keepalive",
    )
    storage = _alert(
        rule_id="530",
        rule_level=12,
        rule_description="Critical storage",
        rule_groups=["syslog"],
        decoder="df",
        detail="Root filesystem is critically full",
    )
    brute = _alert(
        rule_id="5712",
        rule_level=12,
        rule_description="Auth brute force",
        rule_groups=["syslog_sshd"],
        decoder="sshd",
        detail="Repeated authentication failures",
    )
    unknown = _alert(
        rule_id="99999",
        rule_level=7,
        rule_description="Unknown event",
        rule_groups=["other"],
        decoder="other",
        detail="Unrecognized observation",
    )
    watch = _alert(
        rule_id="550",
        rule_level=7,
        rule_description="Port change",
        rule_groups=["syslog"],
        decoder="syslog",
        detail="Observed port change",
    )
    suppress = _alert(
        rule_id="80790",
        rule_level=3,
        rule_description="SCA check failed",
        rule_groups=["sca"],
        decoder="sca",
        detail="Repeated SCA compliance finding",
    )
    findings = [disconnected, storage, brute, unknown, watch, suppress]
    tools = _tools(tmp_path, wazuh=_WazuhSource(findings), remediator=remediator)
    for finding in findings:
        with pytest.raises(FleetError) as caught:
            tools.propose_remediation({"finding_id": finding["finding_id"]})
        assert caught.value.code == "denied"
        _assert_clean(caught.value)
    assert remediator.calls == []

    for tier, adapter, alias in (
        ("protected-status-only", "local", "control-plane"),
        ("appliance-read-only", "local", "control-plane"),
        ("indirect-only", "indirect", "control-plane"),
        ("infrastructure", "windows", "control-plane"),
        ("infrastructure", "proxmox-guest", "control-plane"),
    ):
        registry = create_fleet_registry(
            {
                "targets": [
                    {
                        "alias": alias,
                        "tier": tier,
                        "adapter": adapter,
                        "probe_ids": ["linux-host-summary-v1"],
                        "timeout_ms": 12_000,
                        "freshness_seconds": 60,
                    }
                ],
                "services": [
                    {
                        "service_id": "research-bridge",
                        "target_alias": alias,
                        "probe_id": "linux-host-summary-v1",
                    }
                ],
            }
        )
        blocked = _tools(
            tmp_path / f"{tier}-{adapter}",
            wazuh=_WazuhSource([_alert()]),
            remediator=_Remediator(),
            registry=registry,
        )
        with pytest.raises(FleetError) as caught:
            blocked.propose_remediation({"finding_id": "001:service-failure:533"})
        assert caught.value.code == "denied"
        _assert_clean(caught.value)


def test_observer_findings_remain_non_executable_without_wazuh_source(tmp_path):
    remediator = _Remediator()
    tools = _tools(tmp_path, remediator=remediator)
    tools.ledger.replace_findings(
        "control-plane",
        [
            {
                "finding_id": "control-plane:unreachable",
                "target_alias": "control-plane",
                "proposed_action_id": "inspect-host",
                "reason": "Host is unreachable",
                "blast_radius": "one registered host",
                "verification_id": "verify-host-reachability",
                "rollback_id": "no-rollback",
                "observed_at": "2026-08-28T02:30:00Z",
                "incident_receipt_ref": "receipt-1",
            }
        ],
    )
    proposed = tools.propose_remediation({"finding_id": "control-plane:unreachable"})
    assert proposed["data"]["execution_available"] is False
    assert "proposal_id" not in proposed["data"]
    assert remediator.calls == []


def test_legacy_actionable_ledger_finding_never_creates_or_executes_proposals(tmp_path):
    remediator = _Remediator()
    store = _Store()
    claims = _Claims()
    tools = _tools(tmp_path, remediator=remediator, store=store, claims=claims)
    tools.ledger.replace_findings(
        "research-bridge",
        [
            {
                "finding_id": "research-bridge:unhealthy-service",
                "target_alias": "control-plane",
                "proposed_action_id": "restart-service",
                "reason": "Service health is unhealthy",
                "blast_radius": "one registered service",
                "verification_id": "verify-service",
                "rollback_id": "rollback-service",
                "observed_at": "2026-08-28T02:30:00Z",
                "incident_receipt_ref": "receipt-1",
            }
        ],
    )
    proposed = tools.propose_remediation({"finding_id": "research-bridge:unhealthy-service"})
    assert proposed["data"]["execution_available"] is False
    assert "proposal_id" not in proposed["data"]
    assert store.proposals == {}
    assert remediator.calls == []
    assert claims.calls == []

    leftover_id = "a" * 32
    store.proposals[leftover_id] = {
        "version": 1,
        "proposal_id": leftover_id,
        "finding_id": "research-bridge:unhealthy-service",
        "service_id": "research-bridge",
        "target_alias": "control-plane",
        "finding_revision": "b" * 64,
        "system_revision": "c" * 64,
        "action_id": "restart-service",
        "verification_id": "verify-service",
        "rollback_id": "no-rollback",
        "nonce": "d" * 32,
        "created_at": "2026-08-28T02:30:00Z",
        "expires_at": "2026-08-28T02:45:00Z",
    }
    _approve(store, leftover_id)
    with pytest.raises(FleetError) as caught:
        tools.execute_remediation({"proposal_id": leftover_id})
    assert caught.value.code == "denied"
    assert remediator.calls == []
    assert "claim_consumed" not in store.calls
    assert leftover_id not in store.consumed


def test_missing_wazuh_source_or_finding_fails_closed(tmp_path):
    current = _alert()
    remediator = _Remediator()
    store = _Store()
    tools = _tools(tmp_path, wazuh=_WazuhSource([current]), remediator=remediator, store=store)
    proposed = tools.propose_remediation({"finding_id": current["finding_id"]})
    proposal_id = proposed["data"]["proposal_id"]
    _approve(store, proposal_id)
    remediator.calls.clear()

    tools.wazuh_source = None
    with pytest.raises(FleetError) as missing_source:
        tools.execute_remediation({"proposal_id": proposal_id})
    assert missing_source.value.code == "denied"
    assert remediator.calls == []
    assert "claim_consumed" not in store.calls

    tools.wazuh_source = _WazuhSource()
    with pytest.raises(FleetError) as missing_finding:
        tools.execute_remediation({"proposal_id": proposal_id})
    assert missing_finding.value.code == "denied"
    assert remediator.calls == []
    assert "claim_consumed" not in store.calls

    miss_tools = _tools(
        tmp_path / "miss",
        wazuh=_WazuhSource(),
        remediator=_Remediator(),
        store=_Store(),
    )
    with pytest.raises(FleetError) as missed_propose:
        miss_tools.propose_remediation({"finding_id": current["finding_id"]})
    assert missed_propose.value.code == "denied"
    assert miss_tools.store.proposals == {}


def _ready_approved(tmp_path, **overrides):
    current = _alert()
    store = overrides.pop("store", _Store())
    remediator = overrides.pop("remediator", _Remediator())
    claims = overrides.pop("claims", _Claims())
    wazuh = overrides.pop("wazuh", _WazuhSource([current]))
    tools = _tools(
        tmp_path,
        wazuh=wazuh,
        remediator=remediator,
        claims=claims,
        store=store,
        **overrides,
    )
    proposed = tools.propose_remediation({"finding_id": current["finding_id"]})
    proposal_id = proposed["data"]["proposal_id"]
    _approve(store, proposal_id)
    remediator.calls.clear()
    claims.calls.clear()
    store.calls.clear()
    return tools, proposal_id, remediator, claims, store


def test_capacity_exhaustion_before_mutation_skips_claim_and_execute(tmp_path):
    remediator = _Remediator()
    claims = _Claims()
    store = _Store(reserve_error=FleetActionStoreError("denied"))
    tools, proposal_id, remediator, claims, store = _ready_approved(
        tmp_path, remediator=remediator, claims=claims, store=store
    )
    with pytest.raises(FleetError) as caught:
        tools.execute_remediation({"proposal_id": proposal_id})
    assert caught.value.code == "unavailable"
    assert remediator.calls == [("recheck", "restart-service")]
    assert claims.calls == []
    assert "claim_consumed" not in store.calls
    assert proposal_id not in store.consumed


def test_executor_failure_writes_terminal_receipt_before_release(tmp_path):
    remediator = _Remediator(execute_error=FleetError("unavailable", ERROR_MESSAGES["unavailable"]))
    claims = _Claims()
    store = _Store()
    tools, proposal_id, remediator, claims, store = _ready_approved(
        tmp_path, remediator=remediator, claims=claims, store=store
    )
    with pytest.raises(FleetError) as caught:
        tools.execute_remediation({"proposal_id": proposal_id})
    assert caught.value.code == "unavailable"
    assert remediator.calls == [("recheck", "restart-service"), ("execute", "restart-service")]
    assert any(item.get("outcome") in {"failed", "unverified"} for item in store.receipts)
    assert claims.calls[-1] == ("release", "control-plane")
    assert claims.held == []
    assert "reserve_receipts" in store.calls
    assert store.calls.index("reserve_receipts") < store.calls.index("claim_consumed")


def test_verifier_failure_writes_terminal_receipt_before_release(tmp_path):
    remediator = _Remediator(verify_error=RuntimeError("probe exploded"))
    claims = _Claims()
    store = _Store()
    tools, proposal_id, remediator, claims, store = _ready_approved(
        tmp_path, remediator=remediator, claims=claims, store=store
    )
    with pytest.raises(FleetError) as caught:
        tools.execute_remediation({"proposal_id": proposal_id})
    assert caught.value.code == "unavailable"
    _assert_clean(caught.value)
    assert remediator.calls == [
        ("recheck", "restart-service"),
        ("execute", "restart-service"),
        ("verify", "verify-service"),
    ]
    assert any(item.get("outcome") in {"failed", "unverified"} for item in store.receipts)
    assert claims.calls[-1] == ("release", "control-plane")
    assert claims.held == []


def test_receipt_commit_failure_writes_terminal_receipt_before_release(tmp_path):
    remediator = _Remediator()
    claims = _Claims()
    store = _Store(commit_error=FleetActionStoreError("denied"))
    tools, proposal_id, remediator, claims, store = _ready_approved(
        tmp_path, remediator=remediator, claims=claims, store=store
    )
    with pytest.raises(FleetError) as caught:
        tools.execute_remediation({"proposal_id": proposal_id})
    assert caught.value.code == "unavailable"
    journal = store.read_claim_cleanup()
    assert isinstance(journal, dict)
    assert journal["receipt_pending"] is True
    assert journal["release_pending"] is True
    assert journal["terminal_receipts"]
    assert claims.calls == [("acquire", "control-plane")]
    assert claims.held == [{"target_alias": "control-plane", "holder": "b" * 32}]


def test_claim_release_false_preserves_receipt_and_surfaces_unavailable(tmp_path):
    remediator = _Remediator()
    claims = _Claims(release_granted=False)
    store = _Store()
    tools, proposal_id, remediator, claims, store = _ready_approved(
        tmp_path, remediator=remediator, claims=claims, store=store
    )
    with pytest.raises(FleetError) as caught:
        tools.execute_remediation({"proposal_id": proposal_id})
    assert caught.value.code == "unavailable"
    assert any(item.get("kind") == "verification" and item.get("outcome") == "verified" for item in store.receipts)
    assert any(item.get("kind") == "execution" and item.get("outcome") == "verified" for item in store.receipts)
    assert proposal_id in store.consumed
    assert remediator.calls == [
        ("recheck", "restart-service"),
        ("execute", "restart-service"),
        ("verify", "verify-service"),
    ]
    journal = store.read_claim_cleanup()
    assert isinstance(journal, dict)
    assert journal["proposal_id"] == proposal_id
    assert journal["receipt_pending"] is False
    assert journal["release_pending"] is True


def test_claim_release_exception_preserves_receipt_and_surfaces_unavailable(tmp_path):
    remediator = _Remediator()
    claims = _Claims(release_error=RuntimeError("hub dropped"))
    store = _Store()
    tools, proposal_id, remediator, claims, store = _ready_approved(
        tmp_path, remediator=remediator, claims=claims, store=store
    )
    with pytest.raises(FleetError) as caught:
        tools.execute_remediation({"proposal_id": proposal_id})
    assert caught.value.code == "unavailable"
    _assert_clean(caught.value)
    assert any(item.get("kind") == "verification" and item.get("outcome") == "verified" for item in store.receipts)
    assert proposal_id in store.consumed
    journal = store.read_claim_cleanup()
    assert isinstance(journal, dict)
    assert journal["proposal_id"] == proposal_id
    assert journal["receipt_pending"] is False
    assert journal["release_pending"] is True


def test_policy_and_action_records_use_128_character_wazuh_finding_id(tmp_path):
    from brigade.grokbot_fleet.actions import FleetActionStore, write_fleet_approval_file
    from brigade.grokbot_fleet.contracts import parse_wazuh_finding_id
    from brigade.grokbot_fleet.policy import bind_wazuh_remediation

    long_id = "A" * 128
    assert parse_wazuh_finding_id(long_id) == long_id
    current = {**_alert(), "finding_id": long_id}
    tools = _tools(tmp_path)
    bound = bind_wazuh_remediation(current, registry=tools.registry, now=WINDOW_NOW)
    assert bound["finding_id"] == long_id

    actions = tmp_path / "actions"
    approvals = tmp_path / "approvals"
    actions.mkdir(mode=0o700)
    approvals.mkdir(mode=0o700)
    store = FleetActionStore(action_state_path=str(actions), approval_dir=str(approvals))
    store.ready()
    proposal = {
        "version": 1,
        "proposal_id": "a" * 32,
        "finding_id": long_id,
        "service_id": "research-bridge",
        "target_alias": "control-plane",
        "finding_revision": current["revision"],
        "system_revision": "c" * 64,
        "action_id": "restart-service",
        "verification_id": "verify-service",
        "rollback_id": "no-rollback",
        "nonce": "d" * 32,
        "created_at": "2026-08-28T02:30:00Z",
        "expires_at": "2026-08-28T02:45:00Z",
        "wazuh_fingerprint": current["fingerprint"],
        "maintenance_window_id": "control-plane-service",
        "automatic_rollback": False,
        "blast_radius": "one registered service",
    }
    store.create_proposal(proposal)
    loaded = store.read_proposal(proposal["proposal_id"])
    assert loaded is not None
    assert loaded["finding_id"] == long_id
    write_fleet_approval_file(
        str(approvals),
        {**{key: proposal[key] for key in proposal if key != "created_at"}, "approved_at": "2026-08-28T02:31:00Z"},
    )
    approval = store.read_approval(proposal["proposal_id"])
    assert approval is not None
    assert approval["finding_id"] == long_id


def _has_terminal_failure_evidence(store: _Store) -> bool:
    if any(
        item.get("kind") in {"rejection", "verification", "execution"}
        and item.get("outcome") in {"failed", "unverified", "replayed", "denied"}
        for item in store.receipts
    ):
        return True
    cleanup = store.cleanup
    return isinstance(cleanup, dict) and bool(cleanup.get("terminal_receipts"))


def test_failed_claim_release_retries_after_restart_without_reexecuting(tmp_path):
    remediator = _Remediator()
    claims = _Claims(release_granted=False)
    store = _Store()
    tools, proposal_id, remediator, claims, store = _ready_approved(
        tmp_path, remediator=remediator, claims=claims, store=store
    )
    with pytest.raises(FleetError) as caught:
        tools.execute_remediation({"proposal_id": proposal_id})
    assert caught.value.code == "unavailable"
    _assert_clean(caught.value)
    assert remediator.calls == [
        ("recheck", "restart-service"),
        ("execute", "restart-service"),
        ("verify", "verify-service"),
    ]
    cleanup = store.read_claim_cleanup()
    assert isinstance(cleanup, dict)
    assert cleanup["proposal_id"] == proposal_id
    assert cleanup["target_alias"] == "control-plane"
    assert cleanup["holder"] == "b" * 32
    assert cleanup["terminal_receipts"]
    assert cleanup["receipt_pending"] is False
    assert cleanup["release_pending"] is True
    assert "receipt_ref" not in cleanup
    assert all("b" * 32 not in json.dumps(item) for item in store.receipts)
    assert claims.released[-1]["holder"] == "b" * 32

    remediator.calls.clear()
    claims.calls.clear()
    claims.release_granted = True
    recovered = _tools(
        tmp_path,
        wazuh=tools.wazuh_source,
        remediator=remediator,
        claims=claims,
        store=store,
    )
    overview = recovered.fleet_overview({})
    _assert_clean(overview)
    assert "b" * 32 not in json.dumps(overview)
    assert remediator.calls == []
    assert claims.calls == [("release", "control-plane")]
    assert claims.released[-1]["holder"] == "b" * 32
    assert claims.held == []
    assert store.read_claim_cleanup() is None


def test_recovery_accepts_only_the_same_missing_fence_after_a_post_release_write_failure(tmp_path):
    store = _Store()
    store.cleanup_fail_on_call = 4
    tools, proposal_id, remediator, claims, store = _ready_approved(tmp_path, store=store)

    with pytest.raises(FleetError) as caught:
        tools.execute_remediation({"proposal_id": proposal_id})
    assert caught.value.code == "unavailable"
    assert claims.held == []
    cleanup = store.read_claim_cleanup()
    assert isinstance(cleanup, dict)
    assert cleanup["receipt_pending"] is False
    assert cleanup["release_pending"] is True

    remediator.calls.clear()
    claims.calls.clear()
    claims.release_granted = False
    claims.release_reason = "missing"
    recovered = _tools(
        tmp_path,
        wazuh=tools.wazuh_source,
        remediator=remediator,
        claims=claims,
        store=store,
    )
    recovered.fleet_overview({})
    assert remediator.calls == []
    assert claims.calls == [("release", "control-plane")]
    assert store.read_claim_cleanup() is None

    blocked_store = _Store()
    blocked_tools, blocked_id, blocked_remediator, blocked_claims, blocked_store = _ready_approved(
        tmp_path / "different-holder", store=blocked_store
    )
    blocked_store.cleanup_fail_on_call = 4
    with pytest.raises(FleetError):
        blocked_tools.execute_remediation({"proposal_id": blocked_id})
    blocked_claims.release_granted = False
    blocked_claims.release_reason = "missing"
    blocked_claims.release_holder = "c" * 32
    recovered_blocked = _tools(
        tmp_path / "different-holder",
        wazuh=blocked_tools.wazuh_source,
        remediator=blocked_remediator,
        claims=blocked_claims,
        store=blocked_store,
    )
    with pytest.raises(FleetError) as blocked:
        recovered_blocked.fleet_overview({})
    assert blocked.value.code == "unavailable"
    assert blocked_store.read_claim_cleanup() is not None


def test_failed_claim_release_writes_mode_0600_cleanup_with_holder(tmp_path, monkeypatch):
    from brigade.grokbot_fleet import actions as actions_mod
    from brigade.grokbot_fleet.actions import FleetActionStore, write_fleet_approval_file

    monkeypatch.setattr(actions_mod, "_utc_now", lambda: WINDOW_NOW)
    actions = tmp_path / "actions"
    approvals = tmp_path / "approvals"
    actions.mkdir(mode=0o700)
    approvals.mkdir(mode=0o700)
    os.chmod(actions, 0o700)
    os.chmod(approvals, 0o700)
    store = FleetActionStore(action_state_path=str(actions), approval_dir=str(approvals))
    store.ready()
    current = _alert()
    remediator = _Remediator()
    claims = _Claims(release_granted=False)
    tools = _tools(
        tmp_path,
        wazuh=_WazuhSource([current]),
        remediator=remediator,
        claims=claims,
        store=store,
    )
    proposed = tools.propose_remediation({"finding_id": current["finding_id"]})
    proposal_id = proposed["data"]["proposal_id"]
    proposal = store.read_proposal(proposal_id)
    assert proposal is not None
    write_fleet_approval_file(
        str(approvals),
        {**{key: proposal[key] for key in proposal if key != "created_at"}, "approved_at": "2026-08-28T02:31:00Z"},
    )
    remediator.calls.clear()
    with pytest.raises(FleetError) as caught:
        tools.execute_remediation({"proposal_id": proposal_id})
    assert caught.value.code == "unavailable"
    _assert_clean(caught.value)
    cleanup_files = list((actions / "cleanup").glob("*.json"))
    assert len(cleanup_files) == 1
    info = cleanup_files[0].stat()
    assert stat.S_IMODE(info.st_mode) == 0o600
    payload = json.loads(cleanup_files[0].read_text(encoding="utf-8"))
    assert payload["proposal_id"] == proposal_id
    assert payload["target_alias"] == "control-plane"
    assert payload["holder"] == "b" * 32
    assert payload["terminal_receipts"]
    assert payload["receipt_pending"] is False
    assert payload["release_pending"] is True
    assert "receipt_ref" not in payload
    for path in (actions / "receipts").glob("*.json"):
        assert "b" * 32 not in path.read_text(encoding="utf-8")


def test_consume_failure_keeps_terminal_evidence_when_reservation_release_fails(tmp_path):
    remediator = _Remediator()
    claims = _Claims()
    store = _Store(consume_error=FleetActionStoreError("replayed"), release_error=OSError("busy"))
    tools, proposal_id, remediator, claims, store = _ready_approved(
        tmp_path, remediator=remediator, claims=claims, store=store
    )
    with pytest.raises(FleetError) as caught:
        tools.execute_remediation({"proposal_id": proposal_id})
    assert caught.value.code in {"denied", "unavailable"}
    _assert_clean(caught.value)
    assert remediator.calls == [("recheck", "restart-service")]
    assert "execute" not in {kind for kind, _name in remediator.calls}
    assert _has_terminal_failure_evidence(store)
    assert claims.calls[-1] == ("release", "control-plane")


def test_receipt_commit_failure_keeps_terminal_evidence_when_reservation_release_fails(tmp_path):
    remediator = _Remediator()
    claims = _Claims()
    store = _Store(commit_error=FleetActionStoreError("denied"), release_error=OSError("busy"))
    tools, proposal_id, remediator, claims, store = _ready_approved(
        tmp_path, remediator=remediator, claims=claims, store=store
    )
    with pytest.raises(FleetError) as caught:
        tools.execute_remediation({"proposal_id": proposal_id})
    assert caught.value.code == "unavailable"
    _assert_clean(caught.value)
    assert remediator.calls == [
        ("recheck", "restart-service"),
        ("execute", "restart-service"),
        ("verify", "verify-service"),
    ]
    assert _has_terminal_failure_evidence(store)
    assert claims.calls == [("acquire", "control-plane")]


def test_terminal_recovery_journal_replays_exact_receipts_before_fenced_claim_release(tmp_path):
    remediator = _Remediator()
    claims = _Claims(release_granted=False)
    store = _Store(commit_error=FleetActionStoreError("denied"), release_error=OSError("busy"))
    tools, proposal_id, remediator, claims, store = _ready_approved(
        tmp_path, remediator=remediator, claims=claims, store=store
    )

    with pytest.raises(FleetError) as initial:
        tools.execute_remediation({"proposal_id": proposal_id})
    assert initial.value.code == "unavailable"
    journal = store.read_claim_cleanup()
    assert isinstance(journal, dict)
    assert journal["version"] == 2
    assert journal["proposal_id"] == proposal_id
    assert journal["target_alias"] == "control-plane"
    assert journal["holder"] == "b" * 32
    assert journal["reservation_id"] == "1" * 32
    assert journal["receipt_pending"] is True
    assert journal["release_pending"] is True
    assert "receipt_ref" not in journal
    assert [item["kind"] for item in journal["terminal_receipts"]] == ["execution", "verification"]
    assert all(item["outcome"] == "verified" for item in journal["terminal_receipts"])
    reserved = {item["receipt_id"]: item for item in store.reservations["1" * 32]}
    assert {item["receipt_id"] for item in journal["terminal_receipts"]} <= set(reserved)
    for receipt in journal["terminal_receipts"]:
        assert all(
            receipt.get(key) == value for key, value in reserved[receipt["receipt_id"]].items() if key != "outcome"
        )
    assert "release_reservation" not in store.calls
    assert all("b" * 32 not in json.dumps(item) for item in store.receipts)

    calls_before_retry = list(remediator.calls)
    journal_before_retry = dict(journal)
    with pytest.raises(FleetError) as blocked:
        tools.execute_remediation({"proposal_id": proposal_id})
    assert blocked.value.code == "unavailable"
    assert remediator.calls == calls_before_retry
    assert store.read_claim_cleanup() == journal_before_retry

    remediator.calls.clear()
    claims.release_granted = True
    claims.release_error = None
    store.commit_error = None
    recovered = _tools(
        tmp_path,
        wazuh=tools.wazuh_source,
        remediator=remediator,
        claims=claims,
        store=store,
    )
    overview = recovered.fleet_overview({})
    _assert_clean(overview)
    assert remediator.calls == []
    assert store.read_claim_cleanup() is None
    assert store.reservations == {}
    assert store.receipts[-2:] == journal_before_retry["terminal_receipts"]
    assert all("b" * 32 not in json.dumps(item) for item in store.receipts)
    assert claims.released[-1] == {"target_alias": "control-plane", "holder": "b" * 32}
