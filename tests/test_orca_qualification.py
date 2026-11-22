"""Offline qualification fixtures for Orca 1.4.269 and the baseline route."""

import hashlib
import json
from dataclasses import replace

import pytest

from brigade import (
    native_child_custody,
    native_child_enrollment,
    native_orca_enrollment,
    native_orca_host,
    orca_qualification,
    run_checkpoint,
    run_journal,
    run_projector,
    run_tasks,
    runguard,
)

TASKS = ("alpha", "beta", "gamma")


def write_json(path, data):
    path.write_bytes(run_projector.encode_snapshot_bytes(data))


@pytest.fixture
def batch(tmp_path):
    def make(arm, state="completed", *, live=0, row_held=False, run_status="ok", actual=None):
        actual = actual or {}
        target = replace(
            native_orca_host.NativeTarget("codex", "new-child", "1.4.269", "c" * 40, "gpt-6.2-sol", "medium"),
            **actual.get("target", {}),
        )
        run_dir = tmp_path / ".brigade" / "runs" / "example-run"
        native_child_custody.create_run_dir(run_dir)
        is_orca = arm == "orca"
        with runguard.run_lock(tmp_path, run_dir=run_dir):
            write_json(
                run_dir / "run.json",
                {
                    "schema": "brigade.run.v1",
                    "status": run_status,
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
            if is_orca:
                write_json(
                    run_dir / "orca-enrollment.json",
                    {
                        "schema": native_orca_enrollment.SCHEMA,
                        "source_revision": actual.get("marker_revision", target.source_revision),
                    },
                )
            else:
                write_json(
                    run_dir / "batch-request.json",
                    {
                        "schema": "brigade.run_batch.v1",
                        "tasks": [{"task_id": task, "read_only": True} for task in TASKS],
                    },
                )
            for task in TASKS:
                spec = run_tasks.TaskSpec(task, "worker", "a" * 64)
                artifact = {}
                if is_orca:
                    name = native_child_enrollment.asset_name(task)
                    data = native_child_enrollment.input_bytes(
                        native_orca_enrollment.INPUT_SCHEMA,
                        task_id=task,
                        seat="worker",
                        text="fixture-secret",
                        target=target,
                    )
                    native_child_custody.write_asset(run_dir, name, data)
                    run_tasks.record_native_enrollment(
                        run_dir,
                        workspace=tmp_path,
                        task_id=task,
                        enrollment=run_tasks.TaskEnrollment(
                            1,
                            name,
                            hashlib.sha256(data).hexdigest(),
                            native_orca_enrollment.template_digest(run_dir.name, spec, target),
                        ),
                    )
                else:
                    if actual.get("isolation", "worktree") == "worktree":
                        run_tasks.record_workspace_binding(
                            run_dir,
                            workspace=tmp_path,
                            task_id=task,
                            binding=run_tasks.TaskWorkspaceBinding(
                                actual.get("source_revision", "c" * 40),
                                "a" * 64,
                                "b" * 64,
                                "c" * 64,
                            ),
                        )
                    digest = hashlib.sha256(task.encode()).hexdigest()
                    worker_path = run_dir / f"batch-result-{digest}.json"
                    write_json(
                        worker_path,
                        {
                            "requested_model": actual.get("model", "gpt-6.2-sol"),
                            "reasoning": actual.get("effort", "medium"),
                            "text": "fixture-secret",
                        },
                    )
                    artifact = {
                        "artifact_kind": "receipt",
                        "artifact_digest": hashlib.sha256(worker_path.read_bytes()).hexdigest(),
                    }
                attempt = run_tasks.prepare_attempt(run_dir, workspace=tmp_path, task_id=task, native_governed=is_orca)
                run_tasks.observe_task(
                    run_dir,
                    workspace=tmp_path,
                    handle=attempt,
                    observation=run_tasks.TaskObservation(state if task == "beta" else "completed", **artifact),
                )
        status = tmp_path / "status.json"
        write_json(
            status,
            {
                "schema": "brigade.orca_status.v1",
                "capacity": {"live": live, "max_workers": actual.get("concurrency", 3)},
                "status": "failed" if state != "completed" else "completed",
                "run_id": run_dir.name if is_orca else "earlier-orca-run",
                "tasks": [
                    {
                        "task_id": task,
                        "holds_capacity": row_held and task == "beta",
                        "held_reason": None,
                        "capacity_reason": "awaiting-terminal" if row_held and task == "beta" else None,
                        "terminal": task != "beta" or (not row_held and state != "unknown"),
                        "acked": task != "beta" or (not row_held and state != "unknown"),
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
                "concurrency": 3,
                "isolation": "worktree",
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
def test_collects_closed_record(batch, tmp_path, arm, state, held, verified):
    record = orca_qualification.collect(arm, *batch(arm, state, live=int(held), row_held=state == "unknown"))
    assert record["configuration_mismatch"] is False
    assert record["isolation"] == "worktree"
    assert record["read_only_enforcement"] == ("prompt-only" if arm == "orca" else "brigade-read-only-policy")
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
    assert "example-run" not in output
    assert str(tmp_path) not in output
    assert json.dumps(str(tmp_path))[1:-1] not in output


def test_status_requires_capacity_evidence(batch):
    run_dir, status, measurements = batch("orca", "completed")
    receipt = json.loads(status.read_bytes())
    del receipt["tasks"][0]["holds_capacity"]
    write_json(status, receipt)
    with pytest.raises(ValueError, match="capacity"):
        orca_qualification.collect("orca", run_dir, status, measurements)


@pytest.mark.parametrize("arm", ["orca", "run-batch"])
@pytest.mark.parametrize(
    ("field", "value", "target_field"),
    [
        ("model", "gpt-6-luna", "launch_model"),
        ("effort", "high", "effort"),
        ("source_revision", "d" * 40, "source_revision"),
        ("isolation", "shared", "worktree"),
        ("concurrency", 2, None),
    ],
)
def test_retained_configuration_mismatch(batch, arm, field, value, target_field):
    actual = {field: value}
    if arm == "orca" and target_field:
        target_value = "identity:wt2:local:fixture" if field == "isolation" else value
        actual = {"target": {target_field: target_value}}
    record = orca_qualification.collect(arm, *batch(arm, actual=actual))
    assert field in record["mismatched_fields"]
    assert record["configuration_mismatch"] is True
    assert record["verified_completion_seconds"] is None
    assert record[field] == value


def test_orca_marker_must_match_retained_target(batch):
    record = orca_qualification.collect("orca", *batch("orca", actual={"marker_revision": "d" * 40}))
    assert record["mismatched_fields"] == ["source_revision"]
    assert record["source_revision"] is None
    assert record["verified_completion_seconds"] is None


def test_running_baseline_cannot_have_verified_completion_time(batch):
    record = orca_qualification.collect("run-batch", *batch("run-batch", run_status="running"))
    assert record["all_tasks_terminal"] is True
    assert record["verified_completion_seconds"] is None


def test_output_verification_is_required(batch):
    run_dir, status, measurements = batch("run-batch")
    sheet = json.loads(measurements.read_bytes())
    sheet["verification_passed"] = False
    write_json(measurements, sheet)
    record = orca_qualification.collect("run-batch", run_dir, status, measurements)
    assert record["all_tasks_terminal"] is True
    assert record["slot_check_passed"] is True
    assert record["verified_completion_seconds"] is None


@pytest.mark.parametrize(("live", "row_held"), [(0, True), (1, False)])
def test_both_capacity_observations_must_be_clear(batch, live, row_held):
    record = orca_qualification.collect("run-batch", *batch("run-batch", live=live, row_held=row_held))
    assert record["held_capacity"] == live
    assert record["slot_check_passed"] is False
    assert record["verified_completion_seconds"] is None


def test_cli_reports_unsupported_version(batch, monkeypatch, capsys):
    paths = batch("orca", actual={"target": {"orca_version": "1.4.270"}})
    sheet = json.loads(paths[2].read_bytes())
    sheet["orca_version"] = "1.4.270"
    write_json(paths[2], sheet)
    monkeypatch.setattr("sys.argv", ["qualification", "orca", *map(str, paths)])
    orca_qualification.main()
    output = capsys.readouterr()
    assert json.loads(output.out)["orca_version"] == "1.4.270"
    assert "outside Brigade's supported set" in output.err


def test_cli_bounds_journal_errors(batch, monkeypatch, capsys):
    paths = batch("run-batch")
    monkeypatch.setattr("sys.argv", ["qualification", "run-batch", *map(str, paths)])

    def unavailable(_run_dir):
        raise run_journal.RunJournalError("fixture-secret")

    monkeypatch.setattr(run_tasks, "read_tasks", unavailable)
    with pytest.raises(SystemExit, match="2"):
        orca_qualification.main()
    assert capsys.readouterr().err == "Qualification evidence is missing or invalid. Check the local inputs.\n"
