"""Batch refusal refunds require a complete, bounded durability read."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from brigade import run_batch_launch, run_budget, run_events, run_journal, run_tasks
from brigade.run_transport import Assignment, LaunchRefused
from tests._home import set_home
from tests import test_run_tasks as support

task_run = support.task_run


@pytest.mark.parametrize("kind", ["run.dispatch.requested", "task.launch-claimed", "task.native-claimed"])
@pytest.mark.parametrize("attempt_id", ["a" * 64, None])
def test_has_durable_dispatch_write_real_payloads(kind, attempt_id):
    payload = {"seat": "worker", "attempt": 3, "detail": "requested"}
    if kind != "run.dispatch.requested":
        payload = {"task_id": "one", "attempt_id": "a" * 64, "dispatch_attempt": 3}
        if kind == "task.launch-claimed":
            payload["seat"] = "worker"
        else:
            payload.update(
                parent_thread="fixture-thread",
                orca_task_id="fixture-task",
                intent_digest="b" * 64,
                template_digest="c" * 64,
            )
    event = run_events.build_event(
        run_id="fixture-run",
        sequence=1,
        event_type=kind,
        payload=payload,
        idempotency_key="fixture-claim",
        recorded_at="2026-01-01T00:00:00.000000Z",
        previous_digest=None,
    )

    def probe(**kwargs):
        return run_budget.has_durable_dispatch_write([event], **kwargs)

    binding = {"seat": "worker", "attempt": 3, "task_attempt": ("one", attempt_id)}
    assert probe(**binding)
    assert not probe(**{**binding, "attempt": 4})
    if kind == "run.dispatch.requested":
        assert not probe(**{**binding, "seat": "other"})
    else:
        assert not probe(**{**binding, "task_attempt": ("other", attempt_id)})
        assert not probe(**{**binding, "task_attempt": ("one", "c" * 64)})
        assert not probe(**{**binding, "task_attempt": None})


@pytest.mark.parametrize("stage", ["before-claim", "after-claim", "torn-tail", "broken-chain", "read-error"])
def test_recorder_refusal_refunds_only_without_durable_permission(task_run, monkeypatch, tmp_path, stage):
    set_home(monkeypatch, tmp_path / "isolated-home")
    run_dir, workspace = task_run
    handle = support.prepared(task_run)
    assignment = Assignment(worker="worker", task="fixture", task_id="one", attempt_id=handle.attempt_id)
    probe = run_batch_launch.durable_write_probe(run_dir, "worker", assignment)
    # Preparation is durable, but grants no permission to launch a worker.
    assert not probe(1)
    requested = Mock(side_effect=LaunchRefused("fixture refusal after claim"))
    refused = []
    record = run_batch_launch.launch_recorder(
        run_dir=run_dir,
        workspace=workspace,
        assignment=assignment,
        seat="worker",
        prepared=(handle, "0" * 64 if stage != "after-claim" else run_tasks.read_tasks(run_dir)[0].version),
        record_requested=requested,
        on_refused=refused.append,
    )
    if stage in {"torn-tail", "broken-chain"}:
        with (run_dir / "events/lifecycle.jsonl").open("ab") as stream:
            stream.write(b'{"event_type":' if stage == "torn-tail" else b"{}\n")
    if stage == "read-error":
        monkeypatch.setattr(run_journal, "read_journal_bounded", Mock(side_effect=OSError("fixture read failure")))
    coordinator = run_budget.BudgetCoordinator(
        declaration=run_budget.RunBudgetDeclaration(worker_dispatch_count=1),
        append_event=lambda *args: None,
    )
    with pytest.raises(LaunchRefused):
        coordinator.reserve_and_record_dispatch(
            seat="worker",
            allocate_attempt=lambda: 1,
            record_requested=record,
            reclaim_pending=False,
            task_attempt=("one", handle.attempt_id),
            durable_write_exists=probe,
        )
    retained = stage != "before-claim"
    assert coordinator.projection.used["worker_dispatch_count"] == int(retained)
    if stage == "after-claim":
        requested.assert_called_once_with(1)
        assert run_tasks.read_tasks(run_dir)[0].launch_claim.dispatch_attempt == 1
        assert refused == []
    else:
        requested.assert_not_called()
        assert refused == ["one"]
    if retained:
        with pytest.raises(run_budget.BudgetPolicyError, match="worker_dispatch_count"):
            coordinator.reserve_and_record_dispatch(
                seat="worker", allocate_attempt=lambda: 2, record_requested=record, reclaim_pending=False
            )
    else:
        assert (
            coordinator.reserve_and_record_dispatch(
                seat="worker",
                allocate_attempt=lambda: 2,
                record_requested=lambda attempt: attempt,
                reclaim_pending=False,
            )
            == 2
        )
