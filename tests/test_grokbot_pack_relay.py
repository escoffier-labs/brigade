# content-guard: allow bearer-token file
"""First-party steward ledger relay through generic findings."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from brigade import fleet_client, grokbot_findings, grokbot_packs
from brigade.grokbot_backup.ledger import BackupLedger
from brigade.grokbot_fleet.ledger import FleetLedger

NOW = datetime(2026, 8, 30, 9, 0, tzinfo=timezone.utc)
PRIVATE_FLEET = "PRIVATE_FLEET_REASON_TOKEN"
PRIVATE_BACKUP = "PRIVATE_BACKUP_SUMMARY_TOKEN"
RESULT_KEYS = {
    "apply",
    "eligible",
    "known",
    "created",
    "skipped",
    "pending",
    "reported",
    "limit",
    "relays",
}
HEX_64 = __import__("re").compile(r"^[0-9a-f]{64}$")


def _owner(path: Path) -> Path:
    path.mkdir(mode=0o700)
    os.chmod(path, 0o700)
    return path


def _steward_paths(root: Path, ledger_name: str) -> dict[str, Path]:
    runtime = root / "runtime.json"
    ledger = root / "ledger" / ledger_name
    actions = root / "actions"
    approvals = root / "approvals"
    root.mkdir()
    runtime.write_text("{}", encoding="utf-8")
    os.chmod(runtime, 0o600)
    ledger.parent.mkdir(mode=0o700)
    actions.mkdir(mode=0o700)
    approvals.mkdir(mode=0o700)
    os.chmod(ledger.parent, 0o700)
    os.chmod(actions, 0o700)
    os.chmod(approvals, 0o700)
    return {
        "runtime_path": runtime,
        "ledger_path": ledger,
        "action_state_path": actions,
        "approval_dir": approvals,
    }


def _setup_stewards(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path, Path]:
    monkeypatch.setenv("TEST_GROKBOT_BEARER", "not-a-real-token-value-32chars!!")
    target = tmp_path / "queue"
    target.mkdir()
    owner = _owner(tmp_path / "owner")
    backup_paths = _steward_paths(tmp_path / "backup-state", "ledger.jsonl")
    fleet_paths = _steward_paths(tmp_path / "fleet-state", "ledger.json")
    grokbot_packs.apply_setup(target, "backup-steward", bearer_env="TEST_GROKBOT_BEARER", **backup_paths)
    grokbot_packs.apply_setup(target, "fleet-steward", bearer_env="TEST_GROKBOT_BEARER", **fleet_paths)
    BackupLedger(str(backup_paths["ledger_path"])).ready()
    FleetLedger(str(fleet_paths["ledger_path"])).ready()
    return target, owner, backup_paths["ledger_path"], fleet_paths["ledger_path"]


def _backup_finding(finding_id: str = "media-archive:stale-lock") -> dict[str, object]:
    return {
        "finding_id": finding_id,
        "target_alias": finding_id.split(":", 1)[0],
        "kind": "stale-lock",
        "severity_class": "warning",
        "summary": PRIVATE_BACKUP,
        "observed_at": "2026-08-30T09:00:00Z",
        "proposed_action_id": "run-backup",
        "blast_radius": "one registered restic target",
        "verification_statement": "compare the next snapshot receipt",
        "recovery_statement": "operator reruns the approved backup",
    }


def _fleet_finding(finding_id: str = "control-plane:unreachable") -> dict[str, object]:
    return {
        "finding_id": finding_id,
        "target_alias": finding_id.split(":", 1)[0],
        "proposed_action_id": "inspect-host",
        "reason": PRIVATE_FLEET,
        "blast_radius": "one registered host",
        "verification_id": "verify-host-reachability",
        "rollback_id": "no-rollback",
        "observed_at": "2026-08-30T09:00:00Z",
    }


def _assert_public(result: object) -> None:
    assert isinstance(result, dict)
    assert set(result) == RESULT_KEYS
    dumped = json.dumps(result)
    assert PRIVATE_FLEET not in dumped
    assert PRIVATE_BACKUP not in dumped
    assert "schema" not in dumped
    assert "/backup-state" not in dumped
    assert "/fleet-state" not in dumped
    assert "TEST_GROKBOT_BEARER" not in dumped
    for relay_id in result["relays"]:
        assert isinstance(relay_id, str)
        assert HEX_64.fullmatch(relay_id)


def test_collect_manifests_adapts_steward_ledgers_in_memory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from brigade import grokbot_pack_relay

    target, _owner, backup_path, fleet_path = _setup_stewards(tmp_path, monkeypatch)
    BackupLedger(str(backup_path)).record_finding(_backup_finding())
    FleetLedger(str(fleet_path)).replace_findings("control-plane", [_fleet_finding()])

    manifests = grokbot_pack_relay.collect_manifests(target)

    assert [item["schema"] for item in manifests] == [
        grokbot_findings.FINDINGS_SCHEMA,
        grokbot_findings.FINDINGS_SCHEMA,
        grokbot_findings.FINDINGS_SCHEMA,
    ]
    producers = [[entry["producer"] for entry in item["entries"]] for item in manifests]
    assert producers == [["backup"], ["fleet"], []]
    for manifest in manifests:
        for entry in manifest["entries"]:
            assert set(entry) == grokbot_findings.REQUIRED_ENTRY_KEYS
    assert not list(target.rglob("*findings*.json"))
    assert PRIVATE_BACKUP not in json.dumps(
        [{"schema": item["schema"], "count": len(item["entries"])} for item in manifests]
    )


def test_preview_relay_counts_without_writing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from brigade import grokbot_pack_relay

    target, owner, backup_path, fleet_path = _setup_stewards(tmp_path, monkeypatch)
    BackupLedger(str(backup_path)).record_finding(_backup_finding())
    FleetLedger(str(fleet_path)).replace_findings("control-plane", [_fleet_finding()])
    before = {path: path.stat().st_mtime_ns for path in target.rglob("*") if path.is_file()}

    result = grokbot_pack_relay.preview_relay(target, owner, limit=50)

    assert result["apply"] is False
    assert result["eligible"] == 2
    assert result["known"] == 0
    assert result["created"] == 0
    assert result["limit"] == 50
    _assert_public(result)
    assert not (owner / "memory" / "handoff-inbox").exists()
    after = {path: path.stat().st_mtime_ns for path in target.rglob("*") if path.is_file()}
    assert after == before


def test_known_backup_does_not_starve_fleet_creation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from brigade import grokbot_pack_relay

    monkeypatch.setattr(fleet_client, "report_event", lambda *_args, **_kwargs: True)
    target, owner, backup_path, fleet_path = _setup_stewards(tmp_path, monkeypatch)
    BackupLedger(str(backup_path)).record_finding(_backup_finding())
    first = grokbot_pack_relay.apply_relay(target, owner, limit=1, now=NOW)
    assert first["created"] == 1
    FleetLedger(str(fleet_path)).replace_findings("control-plane", [_fleet_finding()])

    result = grokbot_pack_relay.apply_relay(target, owner, limit=1, now=NOW)

    assert result["apply"] is True
    assert result["known"] == 1
    assert result["created"] == 1
    assert result["eligible"] == 1
    _assert_public(result)
    drafts = list((owner / "memory" / "handoff-inbox").glob("*.md"))
    assert len(drafts) == 2
    bodies = "\n".join(path.read_text(encoding="utf-8") for path in drafts)
    assert PRIVATE_BACKUP in bodies
    assert PRIVATE_FLEET in bodies


def test_apply_recovers_pending_wazuh_relays_and_confirms_them(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from brigade import grokbot_pack_relay
    from brigade.grokbot_wazuh.normalize import normalize_alert
    from brigade.grokbot_wazuh.policy import classify
    from brigade.grokbot_wazuh.store import WazuhStore

    monkeypatch.setenv("TEST_GROKBOT_BEARER", "not-a-real-token-value-32chars!!")
    monkeypatch.setattr(fleet_client, "report_event", lambda *_args, **_kwargs: True)
    target = tmp_path / "queue"
    target.mkdir()
    owner = _owner(tmp_path / "owner")
    wazuh_paths = _steward_paths(tmp_path / "wazuh-state", "wazuh.json")
    grokbot_packs.apply_setup(target, "wazuh-triage", bearer_env="TEST_GROKBOT_BEARER", **wazuh_paths)
    store = WazuhStore(str(wazuh_paths["ledger_path"]))
    store.ready()
    alert = {
        "rule_id": "504",
        "rule_level": 12,
        "rule_description": "Agent disconnected",
        "rule_groups": ["agent_disconnected"],
        "agent_id": "001",
        "decoder": "agent-buffer",
        "timestamp": "2026-08-30T09:00:00Z",
        "detail": "PRIVATE_WAZUH_BODY_TOKEN",
    }
    record = normalize_alert(alert)
    store.upsert_alert(record, classify(record, now=NOW), now=NOW)
    assert store.pending_relay_entries()

    result = grokbot_pack_relay.apply_relay(target, owner, limit=1, now=NOW)

    assert result["apply"] is True
    assert result["created"] == 1
    _assert_public(result)
    assert "PRIVATE_WAZUH_BODY_TOKEN" not in json.dumps(result)
    assert store.pending_relay_entries() == []
    drafts = list((owner / "memory" / "handoff-inbox").glob("*.md"))
    assert len(drafts) == 1


def test_invalid_limit_is_rejected_before_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from brigade import grokbot_pack_relay

    target, owner, backup_path, _fleet_path = _setup_stewards(tmp_path, monkeypatch)
    BackupLedger(str(backup_path)).record_finding(_backup_finding())
    owner_before = list(owner.rglob("*"))

    with pytest.raises(grokbot_pack_relay.PackRelayError) as caught:
        grokbot_pack_relay.apply_relay(target, owner, limit=0, now=NOW)

    assert caught.value.reason == "invalid-limit"
    assert list(owner.rglob("*")) == owner_before
    assert not (target / ".brigade" / "cloud" / "grokbot" / "outbox").exists()
