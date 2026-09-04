"""Terminal run receipts capture a final tree without including Brigade evidence."""

from __future__ import annotations

from pathlib import Path

from brigade.aboyeur import direct_worker_finish


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
