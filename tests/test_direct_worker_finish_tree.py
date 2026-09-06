"""Terminal run receipts capture a final tree without including Brigade evidence."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from brigade import agents
from brigade.aboyeur import artifacts, direct_worker_finish
from brigade.roster import Agent, Roster


def test_final_tree_base_records_tree_for_a_real_worktree(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(direct_worker_finish.localio, "tree_fingerprint", lambda path: "a" * 40)

    payload = direct_worker_finish._final_tree_base({"cwd": tmp_path, "dry_run": False})

    assert payload["tree_fingerprint"] == "a" * 40


def test_final_tree_base_omits_tree_for_dry_run_or_unavailable_fingerprint(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(direct_worker_finish.localio, "tree_fingerprint", lambda path: None)

    unavailable = direct_worker_finish._final_tree_base({"cwd": tmp_path, "dry_run": False})
    dry_run = direct_worker_finish._final_tree_base({"cwd": tmp_path, "dry_run": True})

    assert "tree_fingerprint" not in unavailable
    assert "tree_fingerprint" not in dry_run


def test_failed_terminal_payload_write_persists_final_tree(tmp_path: Path, monkeypatch) -> None:
    output_dir = tmp_path / "run"
    output_dir.mkdir()
    roster = Roster(orchestrator="chef", agents={"chef": Agent("chef", "codex", "plan")})
    monkeypatch.setattr(direct_worker_finish.localio, "tree_fingerprint", lambda path: "b" * 40)

    direct_worker_finish.write_failed_run_receipt(
        output_dir,
        write_json=artifacts.run_io._write_json,
        payload=artifacts._run_payload,
        final=agents.AgentResult(text="failed", ok=False, detail="worker failed"),
        direct_worker=False,
        worker=None,
        roster=roster,
        base={
            "task": "private task",
            "cwd": tmp_path,
            "lock_workspace": tmp_path,
            "roster": roster,
            "dry_run": False,
            "read_only": False,
            "started_at": datetime.now(timezone.utc),
            "output_dir": output_dir,
            "include_git": False,
        },
    )

    receipt = json.loads((output_dir / "run.json").read_text())
    assert receipt["status"] == "failed"
    assert receipt["tree_fingerprint"] == "b" * 40
