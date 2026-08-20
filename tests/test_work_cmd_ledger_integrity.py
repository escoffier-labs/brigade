"""Mutation tests for readiness and decision integrity (#1033, #1034, #1035).

Each case reproduces a pre-fix fail-open and asserts the post-fix refusal.
Reverting the matching gate must turn the test red.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from brigade import dogfood_cmd
from brigade import work_cmd
from brigade.work_cmd import edges as edges_mod

from tests.work_cmd_test_helpers import _init_git_repo, _plan_task_id
from tests.test_work_cmd_work_graph_hardening import _write_raw_ledger


def _add(target: Path, text: str, **kwargs):
    task, created = work_cmd._add_task(target, text, **kwargs)
    assert created
    return task


def _tasks_path(target: Path) -> Path:
    return target / ".brigade" / "work" / "tasks.json"


def _plant_edge_type(target: Path, *, source: str, dest: str, edge_type: str) -> None:
    ledger = json.loads(_tasks_path(target).read_text())
    ledger.setdefault("edges", []).append(
        {
            "id": "edge-planted",
            "source": source,
            "target": dest,
            "type": edge_type,
            "created_at": "2026-01-01T00:00:00+00:00",
        }
    )
    _write_raw_ledger(target, ledger)


def _fake_successful_dogfood(monkeypatch, seen: dict):
    def fake_dogfood_run(task, **kwargs):
        seen["called"] = True
        seen["task"] = task
        run_dir = kwargs.get("output_dir")
        if run_dir is not None:
            Path(run_dir).mkdir(parents=True, exist_ok=True)
            (Path(run_dir) / "run.json").write_text(
                json.dumps({"started_at": "2026-05-26T12:10:00Z", "status": "ok", "task": task}) + "\n"
            )
            (Path(run_dir) / "final.txt").write_text("Done.\n")
        return 0

    monkeypatch.setattr(dogfood_cmd, "run", fake_dogfood_run)


def _ticking_clock(monkeypatch):
    clock = {"n": 0}

    def _now():
        clock["n"] += 1
        return datetime(2026, 8, 9, 12, 0, clock["n"], tzinfo=timezone.utc)

    monkeypatch.setattr(work_cmd.helpers, "_now", _now)


@pytest.mark.parametrize(
    "planted_type",
    ["BLOCKS", " BLOCKS ", "depends-on", "   "],
)
def test_unknown_edge_type_fail_closed_blocks_claim_and_ready(tmp_path, planted_type, capsys):
    """#1033: case/whitespace/unknown types must not make the dependent claimable."""
    _init_git_repo(tmp_path)
    blocker = _add(tmp_path, "Upstream blocker")
    dependent = _add(tmp_path, "Downstream dependent")
    _plant_edge_type(tmp_path, source=blocker["id"], dest=dependent["id"], edge_type=planted_type)

    resolution = edges_mod.resolve_readiness(json.loads(_tasks_path(tmp_path).read_text()))
    ready_ids = {item["id"] for item in resolution.ready}
    assert dependent["id"] not in ready_ids
    blocked = next(item for item in resolution.blocked if item["id"] == dependent["id"])
    assert blocked["reason"] == edges_mod.REASON_UNKNOWN_EDGE_TYPE

    rc = work_cmd.claim(target=tmp_path, task_id=dependent["id"], actor="ops")
    assert rc != 0
    err = capsys.readouterr().err
    assert "not ready" in err

    ledger = json.loads(_tasks_path(tmp_path).read_text())
    stored = next(task for task in ledger["tasks"] if task["id"] == dependent["id"])
    assert stored["status"] == "pending"
    types = [edge.get("type") for edge in ledger.get("edges", [])]
    assert planted_type in types


def test_whitespace_wrapped_blocks_is_not_migrated_on_rewrite(tmp_path):
    """#1033: claim/readiness must not rewrite ' blocks ' to 'blocks'."""
    _init_git_repo(tmp_path)
    blocker = _add(tmp_path, "Upstream blocker")
    dependent = _add(tmp_path, "Downstream dependent")
    planted = " blocks "
    _plant_edge_type(tmp_path, source=blocker["id"], dest=dependent["id"], edge_type=planted)

    _add(tmp_path, "Unrelated rewrite")
    rewritten = json.loads(_tasks_path(tmp_path).read_text())
    types = [edge.get("type") for edge in rewritten.get("edges", [])]
    assert planted in types
    assert "blocks" not in types

    resolution = edges_mod.resolve_readiness(rewritten)
    assert dependent["id"] not in {item["id"] for item in resolution.ready}


def test_work_run_task_id_refuses_blocked_dependency(tmp_path, monkeypatch, capsys):
    """#1034: work run --task-id must not start or complete a blocked task."""
    _init_git_repo(tmp_path)
    artifacts_dir = tmp_path / ".brigade" / "runs"
    dogfood_cmd.init(target=tmp_path, artifacts_dir=artifacts_dir)
    _ticking_clock(monkeypatch)
    blocker = _add(tmp_path, "Blocker remains open")
    blocked = _add(tmp_path, "Blocked work")
    assert (
        work_cmd.task_edge_add(
            target=tmp_path,
            edge_type="blocks",
            source=blocker["id"],
            target_id=blocked["id"],
        )
        == 0
    )
    seen: dict = {}
    _fake_successful_dogfood(monkeypatch, seen)

    rc = work_cmd.run(None, target=tmp_path, task_id=blocked["id"], output_dir=artifacts_dir / "blocked", handoff=False)
    assert rc != 0
    assert "not ready" in capsys.readouterr().err
    assert seen.get("called") is not True
    ledger = json.loads(_tasks_path(tmp_path).read_text())
    stored = next(task for task in ledger["tasks"] if task["id"] == blocked["id"])
    assert stored["status"] == "pending"
    assert "completed_at" not in stored


def test_work_run_task_id_and_default_refuse_unresolved_decision(tmp_path, monkeypatch, capsys):
    """#1034: explicit and default run must honor decision checkpoints."""
    _init_git_repo(tmp_path)
    artifacts_dir = tmp_path / ".brigade" / "runs"
    dogfood_cmd.init(target=tmp_path, artifacts_dir=artifacts_dir)
    _ticking_clock(monkeypatch)
    task_id = _plan_task_id(tmp_path, capsys)
    assert (
        work_cmd.task_plan(
            target=tmp_path,
            task_id=task_id[:12],
            write=True,
            decision="store",
            decision_prompt="Which store?",
            decision_options=["sqlite", "postgres"],
        )
        == 0
    )
    capsys.readouterr()
    seen: dict = {}
    _fake_successful_dogfood(monkeypatch, seen)

    rc = work_cmd.run(None, target=tmp_path, task_id=task_id[:12], output_dir=artifacts_dir / "explicit", handoff=False)
    assert rc != 0
    assert "unresolved plan decision" in capsys.readouterr().err
    assert seen.get("called") is not True

    rc = work_cmd.run(None, target=tmp_path, output_dir=artifacts_dir / "default", handoff=False)
    assert rc == 0
    ledger = json.loads(_tasks_path(tmp_path).read_text())
    stored = next(task for task in ledger["tasks"] if task["id"] == task_id)
    assert stored["status"] == "pending"
    assert "completed_at" not in stored


def test_deleted_plan_receipt_blocks_claim(tmp_path, capsys):
    """#1035: deleting an expected plan receipt must not yield an empty decision list."""
    _init_git_repo(tmp_path)
    task_id = _plan_task_id(tmp_path, capsys)
    assert (
        work_cmd.task_plan(
            target=tmp_path,
            task_id=task_id[:12],
            write=True,
            decision="store",
            decision_prompt="Which store?",
            decision_options=["sqlite", "postgres"],
        )
        == 0
    )
    capsys.readouterr()
    json_path, _ = work_cmd._plan_paths(tmp_path, task_id)
    assert json_path.is_file()
    json_path.unlink()

    assert work_cmd.claim(target=tmp_path, task_id=task_id[:12], actor="ops") == 2
    err = capsys.readouterr().err
    assert "missing" in err
    ledger = json.loads(_tasks_path(tmp_path).read_text())
    stored = next(task for task in ledger["tasks"] if task["id"] == task_id)
    assert stored["status"] == "pending"
    assert stored.get("plan_receipt", {}).get("schema") == work_cmd.PLAN_RECEIPT_SCHEMA


def test_corrupt_or_substituted_plan_receipt_blocks_claim(tmp_path, capsys):
    """#1035: corrupt JSON and emptied decisions must not authorize claim."""
    _init_git_repo(tmp_path)
    task_id = _plan_task_id(tmp_path, capsys)
    assert (
        work_cmd.task_plan(
            target=tmp_path,
            task_id=task_id[:12],
            write=True,
            decision="store",
            decision_prompt="Which store?",
            decision_options=["sqlite", "postgres"],
        )
        == 0
    )
    capsys.readouterr()
    json_path, _ = work_cmd._plan_paths(tmp_path, task_id)
    original = json_path.read_text()

    json_path.write_text("{not-json")
    assert work_cmd.claim(target=tmp_path, task_id=task_id[:12], actor="ops") == 2
    assert "corrupt" in capsys.readouterr().err

    receipt = json.loads(original)
    receipt["decisions"] = []
    json_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    assert work_cmd.claim(target=tmp_path, task_id=task_id[:12], actor="ops") == 2
    assert "digest" in capsys.readouterr().err

    ledger = json.loads(_tasks_path(tmp_path).read_text())
    stored = next(task for task in ledger["tasks"] if task["id"] == task_id)
    assert stored["status"] == "pending"


def test_plan_write_stamps_binding_under_ledger_lock(tmp_path, capsys):
    """#1035: plan writes persist a generation/digest binding on the task."""
    _init_git_repo(tmp_path)
    task_id = _plan_task_id(tmp_path, capsys)
    assert work_cmd.task_plan(target=tmp_path, task_id=task_id[:12], write=True) == 0
    capsys.readouterr()
    ledger = json.loads(_tasks_path(tmp_path).read_text())
    stored = next(task for task in ledger["tasks"] if task["id"] == task_id)
    binding = stored["plan_receipt"]
    assert binding["schema"] == work_cmd.PLAN_RECEIPT_SCHEMA
    assert binding["task_id"] == task_id
    assert binding["kind"] == "plan"
    assert binding["generation"] == 1
    assert isinstance(binding["digest"], str) and len(binding["digest"]) == 64

    assert work_cmd.task_plan(target=tmp_path, task_id=task_id[:12], write=True, title="Second") == 0
    ledger = json.loads(_tasks_path(tmp_path).read_text())
    stored = next(task for task in ledger["tasks"] if task["id"] == task_id)
    assert stored["plan_receipt"]["generation"] == 2
    assert stored["plan_receipt"]["digest"] != binding["digest"]
