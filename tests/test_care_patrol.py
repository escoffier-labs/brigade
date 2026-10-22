"""Run patrol scheduling uses the existing care backends and receipts."""

from __future__ import annotations

import json
import sys

import pytest

from brigade import care_cmd, runbook_cmd


@pytest.mark.parametrize("backend,schedule", [("systemd", "*:0/10"), ("launchd", "*/10 * * * *")])
def test_selected_patrol_installs_report_only_runbook(tmp_path, monkeypatch, capsys, backend, schedule):
    target = tmp_path / "workspace"
    target.mkdir()
    monkeypatch.setattr(care_cmd, "_ensure_memory_care_runbooks", lambda target: 0)
    monkeypatch.setattr(runbook_cmd, "_pin_payload_from_runbook", lambda *args: ({}, None))
    assert (
        care_cmd.install(
            target=target,
            backend=backend,
            home=tmp_path / "home",
            entry_ids=["run-patrol"],
            schedule_specs=[f"run-patrol={schedule}"],
            json_output=True,
        )
        == 0
    )
    capsys.readouterr()
    book = json.loads((target / ".brigade/runbooks/run-patrol.json").read_text())
    command = book["steps"][0]["run"]
    assert "--fail-on stalled,zombie,dead" in command
    assert "--json --receipt" in command and "--apply" not in command
    receipts = [{"runbook_id": "run-patrol", "status": "failed", "run_id": "patrol-run"}] * 2
    monkeypatch.setattr(runbook_cmd, "_run_receipts", lambda target: receipts)
    entry = care_cmd._entry_status(target, care_cmd._catalog_by_id()["run-patrol"])
    assert entry["last_receipt"]["run_id"] == "patrol-run"
    assert entry["repeated_failure"]


def test_patrol_is_optional_except_in_atomic_crontab(tmp_path, monkeypatch, capsys):
    assert "run-patrol" not in {entry.entry_id for entry in care_cmd.CARE_ENTRIES}
    assert "run-patrol.json" in care_cmd._crontab_body(workspace=tmp_path)
    monkeypatch.setattr(care_cmd, "_read_crontab", lambda: ("", None))
    monkeypatch.setattr(care_cmd, "_ensure_care_runbooks", lambda target: 0)
    assert care_cmd.install(target=tmp_path, backend="crontab", entry_ids=["run-patrol"]) == 2
    assert "atomic managed block" in capsys.readouterr().err


@pytest.mark.parametrize("brigade_available", [True, False])
def test_crontab_entry_rejection_leaves_target_unchanged(tmp_path, monkeypatch, capsys, brigade_available):
    target = tmp_path / "workspace"
    target.mkdir()
    monkeypatch.setattr(care_cmd, "_read_crontab", lambda: ("", None))
    monkeypatch.setattr(runbook_cmd.shutil, "which", lambda command: sys.executable if brigade_available else None)

    assert care_cmd.install(target=target, backend="crontab", entry_ids=["run-patrol"]) == 2
    assert "atomic managed block" in capsys.readouterr().err
    assert list(target.iterdir()) == []


def test_patrol_windows_plan(tmp_path, capsys):
    assert care_cmd.install(target=tmp_path, backend="schtasks", entry_ids=["run-patrol"], json_output=True) == 3
    payload = capsys.readouterr().out
    assert "run-patrol" in payload and "/SC MINUTE /MO 10" in payload


def test_status_includes_latest_direct_patrol_receipt(tmp_path, capsys):
    from brigade import runs_patrol
    from tests.test_run_reap import NOW

    assert runs_patrol.patrol(target=tmp_path, receipt=True, now=NOW, json_output=True) == 0
    receipt = json.loads(capsys.readouterr().out)
    entry = care_cmd._catalog_by_id()["run-patrol"]
    status = care_cmd._entry_status(tmp_path, entry)
    assert status["patrol_receipt"]["patrol_id"] == receipt["patrol_id"]
    assert status["patrol_receipt"]["exit_code"] == 0


def test_status_reads_only_newest_patrol_receipt(tmp_path, monkeypatch):
    from brigade import localio, runs_patrol_receipts

    root = tmp_path / ".brigade/patrol"
    for index in range(30):
        path = root / f"20260820T1200{index:02d}-receipt/patrol.json"
        localio.write_json(
            path,
            {
                "schema": "brigade.runs_patrol.v1",
                "patrol_id": path.parent.name,
                "observed_at": f"2026-08-20T12:00:{index:02d}+00:00",
                "exit_code": 0,
                "actions": [],
            },
        )
    read = runs_patrol_receipts.read_receipt
    reads = []

    def tracked(directory):
        reads.append(directory)
        return read(directory)

    monkeypatch.setattr(runs_patrol_receipts, "read_receipt", tracked)
    status = care_cmd._entry_status(tmp_path, care_cmd._catalog_by_id()["run-patrol"])
    assert status["patrol_receipt"]["patrol_id"] == "20260820T120029-receipt"
    assert len(reads) == 1


def test_explicit_crontab_selection_does_not_add_patrol(tmp_path):
    body = care_cmd._crontab_body(workspace=tmp_path, entries=(care_cmd.CARE_ENTRIES[0],))
    assert "run-patrol" not in body
    assert len(body.splitlines()) == 2


@pytest.mark.parametrize("backend", ["systemd", "launchd"])
def test_unselected_patrol_does_not_write_or_pin_runbook(tmp_path, monkeypatch, capsys, backend):
    monkeypatch.setattr(care_cmd, "_ensure_memory_care_runbooks", lambda target: 0)
    monkeypatch.setattr(care_cmd, "_write_daily_observability_runbook", lambda target: 0)

    def unexpected(*args):
        pytest.fail("unselected patrol must not pin its runbook")

    monkeypatch.setattr(runbook_cmd, "_pin_payload_from_runbook", unexpected)
    assert care_cmd.install(target=tmp_path, backend=backend, home=tmp_path / "home") == 0
    capsys.readouterr()
    assert not (tmp_path / ".brigade/runbooks/run-patrol.json").exists()
