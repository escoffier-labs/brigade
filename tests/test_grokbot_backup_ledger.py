"""Bounded Backup Steward ledger tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from brigade.grokbot_backup.contracts import BackupError
from brigade.grokbot_backup.ledger import MAX_RECORDS, BackupLedger, backup_finding_revision


def _ledger(tmp_path: Path) -> BackupLedger:
    directory = tmp_path / "ledger"
    directory.mkdir(mode=0o700)
    os.chmod(directory, 0o700)
    ledger = BackupLedger(str(directory / "ledger.jsonl"))
    ledger.ready()
    return ledger


def _observation(alias: str = "media-archive", *, health: str = "healthy") -> dict[str, object]:
    return {
        "target_alias": alias,
        "health": health,
        "lock_class": "none",
        "observed_at": "2026-08-28T00:00:00Z",
        "freshness_seconds": 60,
        "detail": "ok",
    }


def _finding(finding_id: str = "media-archive:stale-lock") -> dict[str, object]:
    return {
        "finding_id": finding_id,
        "target_alias": "media-archive",
        "kind": "stale-lock",
        "severity_class": "warning",
        "summary": "A stale backup lock is present",
        "observed_at": "2026-08-28T00:00:00Z",
        "receipt_ref": "receipt-1",
        "proposed_action_id": "run-backup",
        "blast_radius": "one registered restic target",
        "verification_statement": "compare the next snapshot receipt",
        "recovery_statement": "operator reruns the approved backup",
    }


def test_ledger_records_observations_and_finding_revisions(tmp_path: Path):
    ledger = _ledger(tmp_path)
    first = ledger.record_observation(_observation(), "receipt-1")
    assert first["changed"] is False
    second = ledger.record_observation(_observation(health="degraded"), "receipt-2")
    assert second["changed"] is True
    assert ledger.last_observation("media-archive")["health"] == "degraded"
    recorded = ledger.record_finding(_finding())
    assert ledger.last_finding("media-archive:stale-lock") == recorded
    assert ledger.latest_finding_revision("media-archive:stale-lock") == backup_finding_revision(recorded)


def test_finding_records_returns_latest_sorted_detached_copies(tmp_path: Path):
    ledger = _ledger(tmp_path)
    assert ledger.finding_records() == []
    ledger.record_observation(_observation(), "receipt-1")
    first = _finding("zeta-archive:stale-lock")
    first["target_alias"] = "zeta-archive"
    later = {**_finding("media-archive:stale-lock"), "summary": "Lock was refreshed"}
    ledger.record_finding(_finding("media-archive:stale-lock"))
    ledger.record_finding(first)
    ledger.record_finding(later)
    snapshot = ledger.finding_records()
    assert [item["finding"]["finding_id"] for item in snapshot] == [
        "media-archive:stale-lock",
        "zeta-archive:stale-lock",
    ]
    assert snapshot[0]["revision"] == backup_finding_revision(ledger.last_finding("media-archive:stale-lock") or {})
    assert snapshot[0]["finding"]["summary"] == "Lock was refreshed"
    assert snapshot[0]["kind"] == "finding"
    snapshot[0]["finding"]["summary"] = "mutated"
    snapshot[0]["revision"] = "0" * 64
    stored = ledger.last_finding("media-archive:stale-lock")
    assert stored is not None
    assert stored["summary"] == "Lock was refreshed"
    assert ledger.latest_finding_revision("media-archive:stale-lock") != "0" * 64
    assert ledger.finding_records()[0]["finding"] is not snapshot[0]["finding"]


def test_ledger_rejects_private_paths_in_public_fields(tmp_path: Path):
    ledger = _ledger(tmp_path)
    with pytest.raises(BackupError) as caught:
        ledger.record_observation({**_observation(), "detail": "/var/lib/restic"}, "receipt-1")
    assert caught.value.code == "protocol_error"
    assert "/var/lib" not in str(caught.value)


def test_ledger_is_bounded_and_retains_latest_records(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("brigade.grokbot_backup.ledger.MAX_RECORDS", 8)
    ledger = _ledger(tmp_path)
    for index in range(12):
        ledger.record_observation(
            {
                **_observation(),
                "observed_at": f"2026-08-28T00:00:{index:02d}Z",
                "detail": f"ok-{index}",
            },
            f"receipt-{index}",
        )
    raw = Path(ledger._path).read_text(encoding="utf-8")
    assert raw.count("\n") <= 8
    assert "ok-11" in raw
    assert "ok-0" not in raw
    assert MAX_RECORDS == 2048
