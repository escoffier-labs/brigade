"""Ledger retention and permission tests."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from brigade.grokbot_fleet.contracts import FleetError
from brigade.grokbot_fleet.ledger import FleetLedger, MAX_HOST_OBSERVATIONS

HOST = {
    "alias": "control-plane",
    "tier": "infrastructure",
    "reachability": "reachable",
    "uptime_class": "under-day",
    "storage_pressure": "normal",
    "failed_service_count": 0,
    "reboot_pending": False,
    "observed_at": "2026-08-27T12:00:00Z",
    "freshness_seconds": 60,
    "changed": False,
}


def test_ledger_creates_owner_only_dir_and_file(tmp_path: Path):
    path = tmp_path / "state" / "ledger.json"
    ledger = FleetLedger(str(path))
    ledger.ready()
    ledger.record_observation(HOST, "receipt-1")
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert ledger.last_host_observation("control-plane")["alias"] == "control-plane"


def test_ledger_marks_change_and_retains_only_the_bound(tmp_path: Path):
    path = tmp_path / "state" / "ledger.json"
    ledger = FleetLedger(str(path))
    first = ledger.record_host_observation_with_change(HOST, None)
    assert first["changed"] is False
    second = ledger.record_host_observation_with_change({**HOST, "reachability": "unreachable"}, None)
    assert second["changed"] is True
    for index in range(MAX_HOST_OBSERVATIONS + 5):
        ledger.record_observation({**HOST, "failed_service_count": index}, None)
    document = json.loads(path.read_text(encoding="utf-8"))
    assert len(document["hosts"]["control-plane"]) == MAX_HOST_OBSERVATIONS


def _finding(finding_id: str, target_alias: str) -> dict[str, str]:
    return {
        "finding_id": finding_id,
        "target_alias": target_alias,
        "proposed_action_id": "inspect-host",
        "reason": "Host is unreachable",
        "blast_radius": "one registered host",
        "verification_id": "verify-host-reachability",
        "rollback_id": "no-rollback",
    }


def test_ledger_replaces_only_the_requested_scope(tmp_path: Path):
    path = tmp_path / "state" / "ledger.json"
    ledger = FleetLedger(str(path))
    finding = _finding("control-plane:unreachable", "control-plane")
    other = _finding("worker:unreachable", "worker")
    ledger.replace_findings("control-plane", [finding])
    ledger.replace_findings("worker", [other])
    ledger.replace_findings("control-plane", [])
    assert ledger.finding("control-plane:unreachable") is None
    assert ledger.finding("worker:unreachable")["finding_id"] == "worker:unreachable"


def test_findings_returns_sorted_detached_copies(tmp_path: Path):
    path = tmp_path / "state" / "ledger.json"
    ledger = FleetLedger(str(path))
    ledger.ready()
    assert ledger.findings() == []
    worker = _finding("worker:unreachable", "worker")
    control = _finding("control-plane:unreachable", "control-plane")
    ledger.replace_findings("worker", [worker])
    ledger.replace_findings("control-plane", [control])
    snapshot = ledger.findings()
    assert [item["finding_id"] for item in snapshot] == [
        "control-plane:unreachable",
        "worker:unreachable",
    ]
    snapshot[0]["reason"] = "mutated"
    snapshot[0]["finding_id"] = "mutated"
    stored = ledger.finding("control-plane:unreachable")
    assert stored is not None
    assert stored["reason"] == "Host is unreachable"
    assert stored["finding_id"] == "control-plane:unreachable"
    assert ledger.findings()[0] is not snapshot[0]


def test_ledger_fails_closed_on_unsafe_permissions(tmp_path: Path):
    path = tmp_path / "state" / "ledger.json"
    path.parent.mkdir(parents=True)
    os.chmod(path.parent, 0o755)
    ledger = FleetLedger(str(path))
    with pytest.raises(FleetError) as caught:
        ledger.ready()
    assert caught.value.code == "protocol_error"
    assert str(path) not in str(caught.value)


def test_ledger_fails_closed_on_corruption_and_symlink(tmp_path: Path):
    path = tmp_path / "state" / "ledger.json"
    ledger = FleetLedger(str(path))
    ledger.ready()
    path.write_text("{not-json", encoding="utf-8")
    os.chmod(path, 0o600)
    with pytest.raises(FleetError) as caught:
        ledger.last_host_observation("control-plane")
    assert caught.value.code == "protocol_error"
    assert "{not-json" not in str(caught.value)
    if os.name == "posix":
        linked = tmp_path / "state" / "linked.json"
        linked.symlink_to(path)
        with pytest.raises(FleetError) as linked_caught:
            FleetLedger(str(linked)).ready()
        assert linked_caught.value.code in {"protocol_error", "invalid_request"}
        assert str(linked) not in str(linked_caught.value)


def test_ledger_partial_and_zero_writes_preserve_prior_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "state" / "ledger.json"
    ledger = FleetLedger(str(path))
    ledger.ready()
    ledger.record_observation(HOST, "receipt-1")
    original = path.read_text(encoding="utf-8")
    real_write = os.write
    calls = {"n": 0}

    def flaky_write(fd: int, data: bytes) -> int:
        calls["n"] += 1
        if calls["n"] == 1:
            chunk = data[: max(1, len(data) // 3)]
            return real_write(fd, chunk)
        return 0

    monkeypatch.setattr(os, "write", flaky_write)
    with pytest.raises(FleetError) as caught:
        ledger.record_observation({**HOST, "failed_service_count": 1}, "receipt-2")
    assert caught.value.code == "unavailable"
    assert path.read_text(encoding="utf-8") == original
    assert not Path(f"{path}.tmp").exists()
    assert ledger.last_host_observation("control-plane")["failed_service_count"] == 0
