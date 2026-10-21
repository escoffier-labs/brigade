"""Patrol receipts publish atomically and retain action fences."""

from __future__ import annotations

import json
from datetime import timedelta

from brigade import localio, runs_patrol, runs_patrol_actions, runs_patrol_receipts
from tests.test_run_reap import NOW


def test_report_retention_bounds_count_and_age_but_keeps_action_fences(tmp_path, capsys):
    root = tmp_path / ".brigade/patrol"

    def seed(stamp, suffix, actions):
        path = root / f"{stamp:%Y%m%dT%H%M%S}-{suffix}/patrol.json"
        localio.write_json(
            path,
            {
                "schema": "brigade.runs_patrol.v1",
                "patrol_id": path.parent.name,
                "observed_at": stamp.isoformat(),
                "actions": actions,
            },
        )
        return path

    old = seed(NOW - timedelta(days=8), "old", [])
    action = {"status": "pending", "idempotency_key": "fence", "scope": "local", "action": "reap"}
    fence = seed(NOW - timedelta(days=8), "fence", [action])
    recent = [seed(NOW - timedelta(minutes=i + 1), "recent", []) for i in range(146)]
    assert runs_patrol.patrol(target=tmp_path, receipt=True, now=NOW, json_output=True) == 0
    report = json.loads(capsys.readouterr().out)
    assert not old.exists()
    assert not fence.exists()
    assert action in runs_patrol_actions._history(root)
    assert recent[0].exists() and not recent[-1].exists()
    assert len(list(root.glob("*/patrol.json"))) == 144
    assert (tmp_path / report["receipt"]).exists()
    assert runs_patrol.patrol(target=tmp_path, receipt=True, now=NOW + timedelta(days=8)) == 0
    capsys.readouterr()
    assert action in runs_patrol_actions._history(root)
    assert len(list(root.glob("*/patrol.json"))) == 1


def test_apply_retention_prunes_receipts_with_only_refused_or_skipped_actions(tmp_path, capsys):
    root = tmp_path / ".brigade/patrol"
    old = root / "20260801T120000-refused/patrol.json"
    localio.write_json(
        old,
        {
            "schema": "brigade.runs_patrol.v1",
            "observed_at": (NOW - timedelta(days=8)).isoformat(),
            "actions": [{"status": "refused"}, {"status": "skipped"}],
        },
    )
    assert runs_patrol.patrol(target=tmp_path, apply="reap", now=NOW) == 0
    capsys.readouterr()
    assert not old.exists()
    assert runs_patrol_actions._history(root) == []


def test_history_reads_only_newest_receipts(tmp_path, monkeypatch):
    root = tmp_path / ".brigade/patrol"
    for index in range(6):
        localio.write_json(
            root / f"20260828T12000{index}-report/patrol.json",
            {"schema": "brigade.runs_patrol.v1", "actions": []},
        )
    monkeypatch.setattr(runs_patrol_receipts, "REPORT_RECEIPT_LIMIT", 2)
    read = runs_patrol_receipts.read_receipt
    reads = []

    def checked(directory):
        reads.append(directory.name)
        return read(directory)

    monkeypatch.setattr(runs_patrol_receipts, "read_receipt", checked)
    assert runs_patrol_actions._history(root) == []
    assert reads == ["20260828T120005-report", "20260828T120004-report"]


def test_archive_failure_preserves_the_original_action_receipt(tmp_path, monkeypatch, capsys):
    root = tmp_path / ".brigade/patrol"
    old = root / "20260801T120000-fence/patrol.json"
    action = {"status": "pending", "idempotency_key": "fence", "scope": "local", "action": "reap"}
    localio.write_json(
        old,
        {"schema": "brigade.runs_patrol.v1", "observed_at": (NOW - timedelta(days=8)).isoformat(), "actions": [action]},
    )
    write = localio.write_text_atomic

    def unavailable(path, content):
        if path.name == runs_patrol_receipts.FENCE_FILE:
            raise OSError("archive write refused")
        return write(path, content)

    monkeypatch.setattr(localio, "write_text_atomic", unavailable)
    assert runs_patrol.patrol(target=tmp_path, apply="reap", now=NOW) == 2
    assert "archive write refused" in capsys.readouterr().err
    assert json.loads(old.read_text())["actions"] == [action]
    monkeypatch.setattr(localio, "write_text_atomic", write)
    assert runs_patrol.patrol(target=tmp_path, apply="reap", now=NOW) == 0
    capsys.readouterr()
    assert not old.exists() and action in runs_patrol_actions._history(root)


def test_receipt_is_complete_before_publication(tmp_path, monkeypatch, capsys):
    replace = localio.os.replace
    published = []

    def checked(source, destination):
        if destination.name == "patrol.json":
            assert not destination.exists()
            published.append(json.loads(source.read_text()))
        return replace(source, destination)

    monkeypatch.setattr(localio.os, "replace", checked)
    assert runs_patrol.patrol(target=tmp_path, receipt=True, now=NOW, json_output=True) == 0
    report = json.loads(capsys.readouterr().out)
    assert published == [report]
