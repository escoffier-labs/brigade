"""Planning receipts for work dispatch, without launch side effects."""

from __future__ import annotations

import hashlib
import json

import pytest

from brigade import cli, localio, work_cmd
from brigade.work_cmd import footprint


@pytest.fixture
def dispatch_target(tmp_path):
    roster = tmp_path / ".brigade" / "roster.toml"
    roster.parent.mkdir()
    roster.write_text(
        'orchestrator = "chef"\n'
        '[agents.chef]\ncli = "codex"\nrole = "plan"\n'
        '[agents.coder]\ncli = "codex"\nrole = "write code"\n'
        "[limits]\nmax_workers = 3\n"
    )
    return tmp_path


def _tasks(target, files):
    tasks = []
    for index, paths in enumerate(files):
        task = {
            "id": f"task-{index}",
            "text": f"Change module {index}",
            "status": "pending",
            "created_at": f"2026-01-01T00:00:{index:02d}Z",
            "item_revision": index + 2,
            "metadata": {"seat_class": "mechanical"},
        }
        footprint.set_task_footprint(task, footprint.normalize_footprint({"files": paths}))
        tasks.append(task)
    work_cmd._write_task_ledger(target, {"version": 2, "tasks": tasks, "edges": []})
    return tasks


def _dispatch(target, capsys, *options):
    assert cli.main(["work", "dispatch", "--target", str(target), "--dry-run", "--json", *options]) == 0
    payload = json.loads(capsys.readouterr().out)
    directory = target / ".brigade" / "dispatch" / payload["dispatch_id"]
    return json.loads((directory / "plan.json").read_text()), directory


def _snapshot(target):
    return {str(path.relative_to(target)): path.read_bytes() for path in target.rglob("*") if path.is_file()}


def test_dry_run_only_writes_two_receipts_with_stable_canonical_digest(dispatch_target, capsys):
    target = dispatch_target
    _tasks(target, [["src/a.py"], ["src/b.py"]])
    before = _snapshot(target)
    plan, directory = _dispatch(target, capsys, "--worker", "coder")
    again, _ = _dispatch(target, capsys, "--worker", "coder")
    assert plan == again
    assert plan["schema"] == "brigade.work_dispatch_plan.v1"
    unsigned = {key: value for key, value in plan.items() if key != "plan_digest"}
    canonical = json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
    assert plan["plan_digest"] == hashlib.sha256(canonical).hexdigest()
    assert [item["task_item_revision"] for item in plan["candidates"]] == [2, 3]
    assert [item["seat"] for item in plan["candidates"]] == ["coder", "coder"]
    assert sorted(path.name for path in directory.iterdir()) == ["plan.json", "session.json"]
    after = _snapshot(target)
    assert {key: value for key, value in after.items() if not key.startswith(".brigade/dispatch/")} == before
    assert all(key.startswith(".brigade/dispatch/") for key in after.keys() - before.keys())
    session = json.loads((directory / "session.json").read_text())
    assert session["ledger_digest"] == plan["ledger_digest"]
    assert session["plan_digest"] == plan["plan_digest"]


@pytest.mark.parametrize(
    ("options", "count"),
    [
        ([], 3),
        (["--max", "2"], 2),
        (["--max", "5", "--batch-size", "1"], 1),
        (["--max", "9"], 4),
        (["--max", "0"], 0),
        (["--batch-size", "0"], 0),
    ],
)
def test_count_is_minimum_of_capacity_batch_and_wave(dispatch_target, capsys, options, count):
    _tasks(dispatch_target, [[f"src/{index}.py"] for index in range(4)])
    plan, _ = _dispatch(dispatch_target, capsys, *options)
    assert plan["dispatch_count"] == count
    assert len(plan["selected"]) == count
    assert plan["live_children"] == 0


def test_wave_pinning_and_skipped_reasons(dispatch_target, capsys):
    _tasks(dispatch_target, [["shared.py"], ["shared.py"], ["other.py"]])
    default, _ = _dispatch(dispatch_target, capsys)
    pinned, _ = _dispatch(dispatch_target, capsys, "--wave", "1")
    assert default["wave"] == 0
    assert [item["task_id"] for item in default["selected"]] == ["task-0", "task-2"]
    assert pinned["wave"] == 1
    assert [item["task_id"] for item in pinned["selected"]] == ["task-1"]
    assert {item["reason"] for item in pinned["skipped"]} == {"other_wave"}


def test_claimed_in_progress_footprint_is_occupied(dispatch_target, capsys):
    tasks = _tasks(dispatch_target, [["shared.py"], ["shared.py"], ["free.py"]])
    tasks[0].update(status="in_progress", claim={"claim_id": "claim-a", "actor": "coder"})
    work_cmd._write_task_ledger(dispatch_target, {"version": 2, "tasks": tasks, "edges": []})
    plan, _ = _dispatch(dispatch_target, capsys)
    assert [item["task_id"] for item in plan["selected"]] == ["task-2"]
    assert {"task_id": "task-1", "reason": "occupied"} in plan["skipped"]


def test_claimed_in_progress_task_without_footprint_holds_all_pending_work(dispatch_target, capsys):
    tasks = _tasks(dispatch_target, [[], ["a.py"], ["b.py"]])
    tasks[0].pop("metadata")
    tasks[0].update(status="in_progress", claim={"claim_id": "claim-a", "actor": "coder"})
    work_cmd._write_task_ledger(dispatch_target, {"version": 2, "tasks": tasks, "edges": []})
    plan, _ = _dispatch(dispatch_target, capsys)
    assert plan["selected"] == []
    assert {item["task_id"] for item in plan["skipped"] if item["reason"] == "occupied"} == {
        "task-1",
        "task-2",
    }


def test_plan_filters_missing_explicit_seat_mapping(dispatch_target, capsys):
    tasks = _tasks(dispatch_target, [["a.py"], ["b.py"], ["c.py"]])
    tasks[1]["metadata"]["seat_class"] = "review"
    tasks[2]["metadata"].pop("seat_class")
    work_cmd._write_task_ledger(dispatch_target, {"version": 2, "tasks": tasks, "edges": []})
    plan, _ = _dispatch(dispatch_target, capsys, "--seat-map", "mechanical=coder")
    assert [item["seat"] for item in plan["selected"]] == ["coder"]
    assert {item["task_id"] for item in plan["skipped"] if item["reason"] == "unmapped_seat_class"} == {
        "task-1",
        "task-2",
    }


def test_candidates_use_claim_next_priority_ranking(dispatch_target, capsys):
    tasks = _tasks(dispatch_target, [["shared.py"], ["shared.py"], ["free.py"]])
    tasks[0]["priority"] = "low"
    tasks[1]["priority"] = "urgent"
    work_cmd._write_task_ledger(dispatch_target, {"version": 2, "tasks": tasks, "edges": []})
    plan, _ = _dispatch(dispatch_target, capsys, "--batch-size", "1")
    assert [item["task_id"] for item in plan["selected"]] == ["task-1"]
    assert [item["task_id"] for item in plan["candidates"]] == ["task-1", "task-2"]


def test_changed_item_revision_changes_plan_digest(dispatch_target, capsys):
    tasks = _tasks(dispatch_target, [["a.py"]])
    first, _ = _dispatch(dispatch_target, capsys)
    tasks[0]["item_revision"] += 1
    work_cmd._write_task_ledger(dispatch_target, {"version": 2, "tasks": tasks, "edges": []})
    changed, _ = _dispatch(dispatch_target, capsys)
    assert changed["plan_digest"] != first["plan_digest"]
    assert changed["selected"][0]["task_item_revision"] == 3


def test_unresolved_plan_decision_is_skipped_without_mutating_ledger(dispatch_target, capsys):
    _tasks(dispatch_target, [["a.py"], ["b.py"]])
    assert (
        work_cmd.task_plan(
            target=dispatch_target,
            task_id="task-0",
            write=True,
            decision="approach",
            decision_prompt="Which approach?",
            decision_options=["first", "second"],
        )
        == 0
    )
    capsys.readouterr()
    before = _snapshot(dispatch_target)
    plan, _ = _dispatch(dispatch_target, capsys)
    assert [item["task_id"] for item in plan["selected"]] == ["task-1"]
    assert {"task_id": "task-0", "reason": "unresolved_plan_decisions"} in plan["skipped"]
    assert {
        key: value for key, value in _snapshot(dispatch_target).items() if not key.startswith(".brigade/dispatch/")
    } == before


def test_unclaimed_in_progress_work_does_not_supply_occupied_footprint(dispatch_target, capsys):
    tasks = _tasks(dispatch_target, [["shared.py"], ["shared.py"]])
    tasks[0]["status"] = "in_progress"
    work_cmd._write_task_ledger(dispatch_target, {"version": 2, "tasks": tasks, "edges": []})
    plan, _ = _dispatch(dispatch_target, capsys)
    assert [item["task_id"] for item in plan["selected"]] == ["task-1"]


def test_apply_requires_explicit_routing_without_writes(dispatch_target, capsys):
    before = _snapshot(dispatch_target)
    assert cli.main(["work", "dispatch", "--target", str(dispatch_target)]) == 2
    assert "--worker or --seat-map" in capsys.readouterr().err
    assert _snapshot(dispatch_target) == before


@pytest.mark.parametrize(
    "options",
    [
        ["--wave", "99"],
        ["--max", "-1"],
        ["--batch-size", "-1"],
        ["--seat-map", "unknown=coder"],
        ["--seat-map", "mechanical="],
        ["--seat-map", "mechanical=coder", "--seat-map", "mechanical=chef"],
        ["--worker", "codr"],
        ["--seat-map", "mechanical=codr"],
        ["--max", "2", "--worker", "codr"],
        ["--max", "2", "--seat-map", "mechanical=codr"],
    ],
)
def test_invalid_plan_inputs_do_not_write_receipts(dispatch_target, capsys, options):
    _tasks(dispatch_target, [["a.py"]])
    before = _snapshot(dispatch_target)
    assert cli.main(["work", "dispatch", "--target", str(dispatch_target), "--dry-run", *options]) == 2
    assert capsys.readouterr().err
    assert _snapshot(dispatch_target) == before


def test_plan_write_failure_does_not_publish_session(dispatch_target, capsys, monkeypatch):
    _tasks(dispatch_target, [["a.py"]])
    write_json = localio.write_json_exclusive

    def fail_plan_write(path, payload):
        if path.name == "plan.json":
            raise OSError("plan write failed")
        write_json(path, payload)

    monkeypatch.setattr(localio, "write_json_exclusive", fail_plan_write)
    assert cli.main(["work", "dispatch", "--target", str(dispatch_target), "--dry-run"]) == 2
    assert "plan write failed" in capsys.readouterr().err
    assert not list((dispatch_target / ".brigade" / "dispatch").rglob("session.json"))


def test_empty_ledger_still_writes_a_plan(dispatch_target, capsys):
    plan, _ = _dispatch(dispatch_target, capsys)
    assert plan["wave"] is None
    assert plan["dispatch_count"] == 0
