"""Offline qualification fixtures for Orca 1.4.269 and the baseline route."""

import json
from pathlib import Path

import pytest

from brigade import run_checkpoint, run_projector, run_tasks, runguard

TASKS = ("alpha", "beta", "gamma")


def write_json(path, data):
    path.write_bytes(run_projector.encode_snapshot_bytes(data))


@pytest.fixture
def batch(tmp_path):
    def make(arm, state, *, held=False):
        run_dir = tmp_path / ".brigade" / "runs" / "example-run"
        run_dir.mkdir(parents=True)
        is_orca = arm == "orca"
        with runguard.run_lock(tmp_path, run_dir=run_dir):
            write_json(
                run_dir / "run.json",
                {
                    "schema": "brigade.run.v1",
                    "status": "running",
                    "lock_workspace": str(tmp_path),
                    "lifecycle_journal_requested": True,
                },
            )
            run_checkpoint.write_checkpoint(
                run_dir, (run_dir / "run.json").read_bytes(), workspace=tmp_path, paired_event_type=None
            )
            run_tasks.accept_task_batch(
                run_dir,
                workspace=tmp_path,
                request_id="qualification-example",
                specs=tuple(run_tasks.TaskSpec(task, "worker", "a" * 64) for task in TASKS),
            )
            for task in TASKS:
                attempt = run_tasks.prepare_attempt(run_dir, workspace=tmp_path, task_id=task)
                run_tasks.observe_task(
                    run_dir,
                    workspace=tmp_path,
                    handle=attempt,
                    observation=run_tasks.TaskObservation(state if task == "beta" else "completed"),
                )
        status = tmp_path / "status.json"
        write_json(
            status,
            {
                "schema": "brigade.orca_status.v1",
                "capacity": {"live": int(held)},
                "status": "failed" if state != "completed" else "completed",
                "run_id": run_dir.name if is_orca else "earlier-orca-run",
                "tasks": [
                    {
                        "task_id": task,
                        "holds_capacity": held and task == "beta",
                        "held_reason": "outcome_unknown" if state == "unknown" and task == "beta" else None,
                        "terminal": True,
                        "acked": True,
                    }
                    for task in TASKS
                ],
            },
        )
        measurements = tmp_path / "measurements.json"
        write_json(
            measurements,
            {
                "orca_version": "1.4.269",
                "brigade_commit": "b" * 40,
                "source_revision": "c" * 40,
                "model": "gpt-6.2-sol",
                "effort": "medium",
                "concurrency": 1,
                "verification_passed": state == "completed",
                "verified_completion_seconds": 17.5,
                "circuit_breaker_routed_around": False,
                "parent_interventions": int(is_orca),
                "other_errors": 0,
                "timings_seconds": {"queue": 0.3, "startup": 1, "execution": 8, "delivery": 1.4},
                "usage": {"input_tokens": 100, "credentials": "fixture-secret"},
                "prompt": "fixture-secret",
                "path": str(tmp_path),
            },
        )
        return run_dir, status, measurements

    return make


@pytest.mark.parametrize(
    ("arm", "state", "held", "verified"),
    [
        ("orca", "completed", False, 17.5),
        ("orca", "failed", True, None),
        ("run-batch", "completed", True, None),
        ("orca", "unknown", True, None),
        ("run-batch", "completed", False, 17.5),
    ],
)
def test_collects_closed_record(batch, arm, state, held, verified):
    from brigade import orca_qualification

    record = orca_qualification.collect(arm, *batch(arm, state, held=held))
    assert record["orca_version"] == "1.4.269"
    assert record["parent_interventions"] == int(arm == "orca")
    assert record["held_capacity"] == int(held)
    assert record["slot_check_passed"] is not held
    assert record["all_tasks_terminal"] is (state != "unknown")
    assert record["errors"]["failed_tasks"] == int(state == "failed")
    assert record["verified_completion_seconds"] == verified
    assert record["usage"] == {"input_tokens": 100, "output_tokens": None, "cached_input_tokens": None}
    assert record["timings_seconds"] == {
        "queue": 0.3,
        "startup": 1,
        "planning": None,
        "execution": 8,
        "synthesis": None,
        "delivery": 1.4,
    }
    output = json.dumps(record)
    assert "fixture-secret" not in output
    assert str(Path("example-run")) not in output


def test_status_requires_capacity_evidence(batch):
    from brigade import orca_qualification

    run_dir, status, measurements = batch("orca", "completed")
    receipt = json.loads(status.read_bytes())
    del receipt["tasks"][0]["holds_capacity"]
    write_json(status, receipt)
    with pytest.raises(ValueError, match="capacity"):
        orca_qualification.collect("orca", run_dir, status, measurements)
