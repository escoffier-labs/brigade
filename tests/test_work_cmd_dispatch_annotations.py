"""Dispatch seat-class / spend-by annotations on work tasks (#815)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from brigade import work_cmd

from tests.work_cmd_test_helpers import _init_git_repo


def test_task_add_and_ready_round_trip_annotations(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd.helpers,
        "_now",
        lambda: datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc),
    )

    assert (
        work_cmd.task_add(
            target=tmp_path,
            text="Burn mechanical quota work",
            seat_class="mechanical",
            spend_by="2026-08-10",
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "seat_class: mechanical" in out
    assert "spend_by: 2026-08-10" in out
    task_id = out.split("task: ", 1)[1].splitlines()[0]

    ledger = work_cmd._read_task_ledger(tmp_path)
    task = ledger["tasks"][0]
    assert task["metadata"]["seat_class"] == "mechanical"
    assert task["metadata"]["spend_by"] == "2026-08-10"

    assert work_cmd.ready(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ready_count"] == 1
    ready = payload["ready"][0]
    assert ready["id"] == task_id
    assert ready["seat_class"] == "mechanical"
    assert ready["spend_by"] == "2026-08-10"

    assert work_cmd.ready(target=tmp_path) == 0
    text_out = capsys.readouterr().out
    assert "seat_class=mechanical" in text_out
    assert "spend_by=2026-08-10" in text_out


def test_absent_annotations_change_nothing(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd.helpers,
        "_now",
        lambda: datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc),
    )

    assert work_cmd.task_add(target=tmp_path, text="Plain task") == 0
    capsys.readouterr()
    assert work_cmd.ready(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    ready = payload["ready"][0]
    assert "seat_class" not in ready
    assert "spend_by" not in ready
    ledger = work_cmd._read_task_ledger(tmp_path)
    metadata = ledger["tasks"][0].get("metadata") or {}
    assert "seat_class" not in metadata
    assert "spend_by" not in metadata


def test_invalid_seat_class_and_spend_by_rejected(tmp_path, capsys):
    _init_git_repo(tmp_path)
    assert work_cmd.task_add(target=tmp_path, text="Bad seat", seat_class="frontier") == 2
    err = capsys.readouterr().err
    assert "seat_class must be one of" in err
    assert work_cmd._read_task_ledger(tmp_path)["tasks"] == []

    assert work_cmd.task_add(target=tmp_path, text="Bad spend", spend_by="next friday") == 2
    err = capsys.readouterr().err
    assert "spend_by must be an ISO-8601" in err
    assert work_cmd._read_task_ledger(tmp_path)["tasks"] == []


def test_graph_plan_metadata_annotations_round_trip(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd.helpers,
        "_now",
        lambda: datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc),
    )
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "nodes": [
                    {
                        "key": "a",
                        "text": "Review quota burn",
                        "metadata": {"seat_class": "review", "spend_by": "2026-08-12"},
                    }
                ],
                "edges": [],
            }
        )
    )
    assert work_cmd.task_add(target=tmp_path, graph=plan_path) == 0
    capsys.readouterr()
    assert work_cmd.ready(target=tmp_path, json_output=True) == 0
    ready = json.loads(capsys.readouterr().out)["ready"][0]
    assert ready["seat_class"] == "review"
    assert ready["spend_by"] == "2026-08-12"
