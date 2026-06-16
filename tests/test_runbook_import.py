"""Tests for routing failed runbook steps into the work import inbox (#90)."""

from __future__ import annotations

import json
from pathlib import Path

from brigade import runbook_cmd


def _write_receipt(target: Path, run_id: str) -> None:
    run_dir = runbook_cmd._runs_root(target) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "receipt.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "runbook_id": "rb",
                "status": "failed",
                "started_at": "2026-06-16T00:00:00Z",
                "steps": [
                    {"id": "build", "status": "completed"},
                    {"id": "deploy", "status": "failed"},
                ],
            }
        )
    )


def test_runbook_closeout_imports_failed_steps(tmp_path: Path, capsys):
    _write_receipt(tmp_path, "run-1")
    rc = runbook_cmd.closeout(target=tmp_path, run_id="run-1", import_issues=True, json_output=True)
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["import_issues"]["failed_step_count"] == 1
    assert payload["import_issues"]["created"] == 1

    # Idempotent: the same failed step does not create a duplicate import.
    runbook_cmd.closeout(target=tmp_path, run_id="run-1", import_issues=True, json_output=True)
    second = json.loads(capsys.readouterr().out)
    assert second["import_issues"]["created"] == 0


def test_runbook_closeout_without_import_flag_skips_imports(tmp_path: Path, capsys):
    _write_receipt(tmp_path, "run-2")
    runbook_cmd.closeout(target=tmp_path, run_id="run-2", json_output=True)
    payload = json.loads(capsys.readouterr().out)
    assert "import_issues" not in payload
