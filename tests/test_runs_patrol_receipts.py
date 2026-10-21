"""Patrol receipts publish atomically and retain action fences."""

from __future__ import annotations

import json
from datetime import timedelta

from brigade import localio, runs_patrol
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
    fence = seed(NOW - timedelta(days=8), "fence", [{"status": "pending"}])
    recent = [seed(NOW - timedelta(minutes=i + 1), "recent", []) for i in range(146)]
    assert runs_patrol.patrol(target=tmp_path, receipt=True, now=NOW, json_output=True) == 0
    report = json.loads(capsys.readouterr().out)
    assert not old.exists()
    assert fence.exists()
    assert recent[0].exists() and not recent[-1].exists()
    assert len(list(root.glob("*/patrol.json"))) == 145  # 144 reports plus the action fence.
    assert (tmp_path / report["receipt"]).exists()
    assert runs_patrol.patrol(target=tmp_path, receipt=True, now=NOW + timedelta(days=8)) == 0
    capsys.readouterr()
    assert fence.exists()
    assert len(list(root.glob("*/patrol.json"))) == 2


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
