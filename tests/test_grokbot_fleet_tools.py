"""Public tool contract tests for Fleet Steward."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from brigade.grokbot_fleet import actions as actions_mod
from brigade.grokbot_fleet.actions import FleetActionStore, write_fleet_approval_file
from brigade.grokbot_fleet.contracts import ERROR_MESSAGES, FleetError
from brigade.grokbot_fleet.ledger import FleetLedger
from brigade.grokbot_fleet.probes import FleetProbes
from brigade.grokbot_fleet.runtime_config import parse_fleet_private_runtime, project_fleet_public_registry
from brigade.grokbot_fleet.tools import FleetStewardTools

NOW = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
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


class _Store:
    def __init__(self):
        self.proposals = {}
        self.approvals = {}
        self.consumed = {}
        self.receipts = []
        self.reservations = {}
        self.claim_cleanup = None
        self._reservation_seq = 0

    def create_proposal(self, record):
        self.proposals[record["proposal_id"]] = record

    def read_proposal(self, proposal_id):
        return self.proposals.get(proposal_id)

    def read_approval(self, proposal_id):
        return self.approvals.get(proposal_id)

    def claim_consumed(self, record):
        if record["proposal_id"] in self.consumed:
            from brigade.grokbot_fleet.actions import FleetActionStoreError

            raise FleetActionStoreError("replayed")
        self.consumed[record["proposal_id"]] = record
        return record

    def write_receipt(self, record):
        self.receipts.append(record)

    def reserve_receipt_capacity(self, records):
        from brigade.grokbot_fleet.actions import FleetActionStoreError

        if not records:
            raise FleetActionStoreError("denied")
        proposal_id = records[0]["proposal_id"]
        if any(item["proposal_id"] == proposal_id for item in self.reservations.values()):
            raise FleetActionStoreError("denied")
        self._reservation_seq += 1
        reservation_id = f"{self._reservation_seq:032x}"
        self.reservations[reservation_id] = {"proposal_id": proposal_id}
        return reservation_id

    def commit_reserved_receipts(self, reservation_id, records):
        from brigade.grokbot_fleet.actions import FleetActionStoreError

        if reservation_id not in self.reservations:
            raise FleetActionStoreError("denied")
        del self.reservations[reservation_id]
        for record in records:
            self.write_receipt(record)

    def release_receipt_reservation(self, reservation_id):
        self.reservations.pop(reservation_id, None)

    def write_claim_cleanup(self, record):
        self.claim_cleanup = dict(record)

    def read_claim_cleanup(self):
        return self.claim_cleanup

    def clear_claim_cleanup(self):
        self.claim_cleanup = None


class _Executor:
    def __init__(self, health="unhealthy", revision="c" * 64):
        self.health = health
        self.revision = revision
        self.restarted = 0

    def observe_research_bridge_revision(self):
        return {"system_revision": self.revision, "health_class": self.health}

    def restart_research_bridge(self):
        self.restarted += 1


def _host_raw(alias: str, **overrides):
    payload = {
        "alias": alias,
        "probe_id": "linux-host-summary-v1",
        "observed_at": "2026-08-27T12:00:00Z",
        "reachability": "reachable",
        "uptime_seconds": 100,
        "storage_percent": 10,
        "failed_services": 0,
        "reboot_pending": False,
        "detail": "ok",
    }
    payload.update(overrides)
    return payload


def _tools(tmp_path, *, fail_aliases=(), store=None, executor=None):
    registry = project_fleet_public_registry(parse_fleet_private_runtime(RUNTIME))
    ledger = FleetLedger(str(tmp_path / "ledger.json"))
    ledger.ready()

    def observe_host_raw(target, probe_id):
        if target["alias"] in fail_aliases:
            raise FleetError("unavailable", "Fleet observation is unavailable")
        return _host_raw(target["alias"])

    def observe_service_raw(target, service):
        return {
            "service_id": service["service_id"],
            "target_alias": target["alias"],
            "probe_id": service["probe_id"],
            "observed_at": "2026-08-27T12:00:00Z",
            "health_class": "unhealthy",
            "detail": "Service health is available",
        }

    probes = FleetProbes(
        registry=registry,
        observe_host_raw=observe_host_raw,
        observe_service_raw=observe_service_raw,
        now=lambda: NOW,
        create_receipt_ref=lambda: "receipt-1",
        secrets=["super-secret-token-value"],
    )
    ids = iter(["a" * 32, "b" * 32, "c" * 32, "d" * 32, "e" * 32, "f" * 32])
    return FleetStewardTools(
        registry=registry,
        probes=probes,
        ledger=ledger,
        store=store or _Store(),
        executor=executor or _Executor(),
        now=lambda: NOW,
        request_id=lambda: "req-1",
        create_proposal_id=lambda: next(ids),
        create_nonce=lambda: next(ids),
        create_receipt_id=lambda: next(ids),
        secrets=["super-secret-token-value"],
    )


def _leftover_observer_proposal(proposal_id: str, nonce: str) -> dict[str, object]:
    return {
        "version": 1,
        "proposal_id": proposal_id,
        "finding_id": "research-bridge:unhealthy-service",
        "service_id": "research-bridge",
        "target_alias": "control-plane",
        "finding_revision": "b" * 64,
        "system_revision": "c" * 64,
        "action_id": "restart-service",
        "verification_id": "verify-service",
        "rollback_id": "no-rollback",
        "nonce": nonce,
        "created_at": "2026-08-27T12:00:00Z",
        "expires_at": "2027-08-27T12:15:00Z",
    }


def test_overview_preserves_order_and_bounds_unavailable_targets(tmp_path):
    tools = _tools(tmp_path, fail_aliases=("hypervisor",))
    result = tools.fleet_overview({})
    assert result["status"] == "partial"
    assert [item["alias"] for item in result["data"]] == ["control-plane", "worker"]
    assert result["errors"][0]["target_alias"] == "hypervisor"
    assert result["errors"][0]["code"] == "unavailable"


def test_host_status_and_service_health_authorize_registered_targets(tmp_path):
    tools = _tools(tmp_path)
    host = tools.host_status({"alias": "control-plane"})
    assert host["data"]["alias"] == "control-plane"
    service = tools.service_health({"service_id": "research-bridge"})
    assert service["data"]["health_class"] == "unhealthy"
    with pytest.raises(FleetError) as caught:
        tools.host_status({"alias": "unknown-host"})
    assert caught.value.code == "not_found"
    assert "unknown-host" not in str(caught.value)


def test_incident_bundle_catalogs_supported_findings(tmp_path):
    tools = _tools(tmp_path)
    bundle = tools.incident_bundle({"scope": "research-bridge"})
    ids = {finding["finding_id"] for finding in bundle["findings"]}
    assert "research-bridge:unhealthy-service" in ids
    assert tools.ledger.finding("research-bridge:unhealthy-service")["finding_id"] == (
        "research-bridge:unhealthy-service"
    )


def test_propose_and_execute_follow_fail_closed_replay_rules(tmp_path):
    store = _Store()
    executor = _Executor()
    tools = _tools(tmp_path, store=store, executor=executor)
    tools.incident_bundle({"scope": "research-bridge"})
    finding = tools.ledger.finding("research-bridge:unhealthy-service")
    proposed = tools.propose_remediation({"finding_id": finding["finding_id"]})
    assert proposed["data"]["execution_available"] is False
    assert proposed["data"]["approval_required"] is True
    assert "proposal_id" not in proposed["data"]
    assert store.proposals == {}
    assert executor.restarted == 0


def test_unsupported_findings_propose_without_execution(tmp_path):
    tools = _tools(tmp_path)
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
                "observed_at": "2026-08-27T12:00:00Z",
                "incident_receipt_ref": "receipt-1",
            }
        ],
    )
    proposed = tools.propose_remediation({"finding_id": "control-plane:unreachable"})
    assert proposed["data"]["execution_available"] is False
    assert proposed["data"]["approval_required"] is True
    assert "proposal_id" not in proposed["data"]


def test_repeated_rejected_execution_does_not_unbounded_receipts(tmp_path: Path):
    actions = tmp_path / "actions"
    approvals = tmp_path / "approvals"
    actions.mkdir(mode=0o700)
    approvals.mkdir(mode=0o700)
    os.chmod(actions, 0o700)
    os.chmod(approvals, 0o700)
    store = FleetActionStore(action_state_path=str(actions), approval_dir=str(approvals))
    store.ready()
    now = datetime.now(timezone.utc)
    ids = iter(f"{index:032x}" for index in range(1, 10_000))
    tools = _tools(tmp_path, store=store)
    tools.now = lambda: now
    tools.create_proposal_id = lambda: next(ids)
    tools.create_nonce = lambda: next(ids)
    tools.create_receipt_id = lambda: next(ids)
    tools.incident_bundle({"scope": "research-bridge"})
    proposed = tools.propose_remediation({"finding_id": "research-bridge:unhealthy-service"})
    assert proposed["data"]["execution_available"] is False
    assert "proposal_id" not in proposed["data"]
    proposal_id = next(ids)
    leftover = _leftover_observer_proposal(proposal_id, next(ids))
    store.create_proposal(leftover)
    receipts = actions / "receipts"
    before = len(list(receipts.glob("*.json")))
    for _ in range(20):
        with pytest.raises(FleetError) as caught:
            tools.execute_remediation({"proposal_id": proposal_id})
        assert caught.value.code == "denied"
        assert caught.value.message == ERROR_MESSAGES["denied"]
        assert str(caught.value.public_error()) == str(
            {"error": {"code": "denied", "message": ERROR_MESSAGES["denied"]}}
        )
    after = len(list(receipts.glob("*.json")))
    assert after - before <= 1


def _write_live_receipt(
    store: FleetActionStore,
    receipt_id: str,
    proposal_id: str,
    *,
    kind: str = "rejection",
    outcome: str = "denied",
) -> None:
    store.write_receipt(
        {
            "version": 1,
            "receipt_id": receipt_id,
            "kind": kind,
            "proposal_id": proposal_id,
            "outcome": outcome,
            "created_at": "2026-08-27T12:10:00Z",
        }
    )


def test_execute_remediation_denies_saturated_live_receipts_before_claim_or_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(actions_mod, "MAX_RECEIPTS", 3)
    monkeypatch.setattr(actions_mod, "MAX_RECEIPT_BYTES", 262_144)
    actions = tmp_path / "actions"
    approvals = tmp_path / "approvals"
    actions.mkdir(mode=0o700)
    approvals.mkdir(mode=0o700)
    os.chmod(actions, 0o700)
    os.chmod(approvals, 0o700)
    store = FleetActionStore(action_state_path=str(actions), approval_dir=str(approvals))
    store.ready()
    now = datetime.now(timezone.utc)
    ids = iter(f"{index:032x}" for index in range(1, 10_000))
    executor = _Executor()
    tools = _tools(tmp_path, store=store, executor=executor)
    tools.now = lambda: now
    tools.create_proposal_id = lambda: next(ids)
    tools.create_nonce = lambda: next(ids)
    tools.create_receipt_id = lambda: next(ids)
    tools.incident_bundle({"scope": "research-bridge"})
    proposed = tools.propose_remediation({"finding_id": "research-bridge:unhealthy-service"})
    assert proposed["data"]["execution_available"] is False
    proposal_id = next(ids)
    store.create_proposal(_leftover_observer_proposal(proposal_id, next(ids)))
    _write_live_receipt(store, next(ids), proposal_id, outcome="denied")
    _write_live_receipt(store, next(ids), proposal_id, outcome="stale")
    _write_live_receipt(store, next(ids), proposal_id, outcome="expired")
    assert len(list((actions / "receipts").glob("*.json"))) == 3
    approval = {
        **{
            key: store.read_proposal(proposal_id)[key]
            for key in store.read_proposal(proposal_id)
            if key != "created_at"
        },
        "approved_at": now.isoformat().replace("+00:00", "Z"),
    }
    write_fleet_approval_file(str(approvals), approval)
    with pytest.raises(FleetError) as caught:
        tools.execute_remediation({"proposal_id": proposal_id})
    assert caught.value.code in {"denied", "unavailable"}
    assert caught.value.message == ERROR_MESSAGES[caught.value.code]
    assert executor.restarted == 0
    assert list((actions / "consumed").glob("*.json")) == []


def test_execute_remediation_releases_stale_reservation_capacity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(actions_mod, "MAX_RECEIPTS", 3)
    monkeypatch.setattr(actions_mod, "MAX_RECEIPT_BYTES", 262_144)
    actions = tmp_path / "actions"
    approvals = tmp_path / "approvals"
    actions.mkdir(mode=0o700)
    approvals.mkdir(mode=0o700)
    os.chmod(actions, 0o700)
    os.chmod(approvals, 0o700)
    store = FleetActionStore(action_state_path=str(actions), approval_dir=str(approvals))
    store.ready()
    now = datetime.now(timezone.utc)
    ids = iter(f"{index:032x}" for index in range(1, 10_000))
    executor = _Executor()

    def fail_restart() -> None:
        executor.restarted += 1
        raise FleetError("unavailable", ERROR_MESSAGES["unavailable"])

    executor.restart_research_bridge = fail_restart  # type: ignore[method-assign]
    tools = _tools(tmp_path, store=store, executor=executor)
    tools.now = lambda: now
    tools.create_proposal_id = lambda: next(ids)
    tools.create_nonce = lambda: next(ids)
    tools.create_receipt_id = lambda: next(ids)
    tools.incident_bundle({"scope": "research-bridge"})
    proposed = tools.propose_remediation({"finding_id": "research-bridge:unhealthy-service"})
    assert proposed["data"]["execution_available"] is False
    proposal_id = next(ids)
    store.create_proposal(_leftover_observer_proposal(proposal_id, next(ids)))
    approval = {
        **{
            key: store.read_proposal(proposal_id)[key]
            for key in store.read_proposal(proposal_id)
            if key != "created_at"
        },
        "approved_at": now.isoformat().replace("+00:00", "Z"),
    }
    write_fleet_approval_file(str(approvals), approval)
    with pytest.raises(FleetError) as caught:
        tools.execute_remediation({"proposal_id": proposal_id})
    assert caught.value.code == "denied"
    assert executor.restarted == 0
    assert list((actions / "reservations").glob("*.json")) == []
    receipt_id = next(ids)
    _write_live_receipt(store, receipt_id, proposal_id, outcome="failed")
    assert (actions / "receipts" / f"{receipt_id}.json").is_file()


def test_replay_loser_cannot_drop_winner_reservation_and_winner_commits_after_one_restart(
    tmp_path: Path,
):
    actions = tmp_path / "actions"
    approvals = tmp_path / "approvals"
    actions.mkdir(mode=0o700)
    approvals.mkdir(mode=0o700)
    os.chmod(actions, 0o700)
    os.chmod(approvals, 0o700)
    store = FleetActionStore(action_state_path=str(actions), approval_dir=str(approvals))
    store.ready()
    now = datetime.now(timezone.utc)
    ids = iter(f"{index:032x}" for index in range(1, 10_000))
    executor = _Executor()
    tools = _tools(tmp_path, store=store, executor=executor)
    tools.now = lambda: now
    tools.create_proposal_id = lambda: next(ids)
    tools.create_nonce = lambda: next(ids)
    tools.create_receipt_id = lambda: next(ids)
    tools.incident_bundle({"scope": "research-bridge"})
    proposed = tools.propose_remediation({"finding_id": "research-bridge:unhealthy-service"})
    assert proposed["data"]["execution_available"] is False
    proposal_id = next(ids)
    leftover = _leftover_observer_proposal(proposal_id, next(ids))
    store.create_proposal(leftover)
    approval = {
        **{key: leftover[key] for key in leftover if key != "created_at"},
        "approved_at": now.isoformat().replace("+00:00", "Z"),
    }
    write_fleet_approval_file(str(approvals), approval)
    with pytest.raises(FleetError) as caught:
        tools.execute_remediation({"proposal_id": proposal_id})
    assert caught.value.code == "denied"
    assert executor.restarted == 0
    assert list((actions / "consumed").glob("*.json")) == []
    assert list((actions / "reservations").glob("*.json")) == []
