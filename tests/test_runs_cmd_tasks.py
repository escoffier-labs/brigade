"""Read-only `brigade runs tasks` contracts: list, show and wait."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from brigade import aboyeur, cli
from brigade import run_task_journal as task_journal
from brigade import run_tasks as tasks
from brigade import runs_cmd_tasks
from tests import test_run_tasks as support

task_run = support.task_run

SENTINEL = "SENTINEL-task-text-must-never-print"
TASK_TEXT = 64 * "a"


def _accept(run_dir, workspace, task_id):
    return tasks.accept_task(
        run_dir,
        workspace=workspace,
        request_id=f"request-{task_id}",
        spec=tasks.TaskSpec(task_id, "worker", TASK_TEXT),
    )


def _complete(run_dir, workspace, task_id, *, acknowledged=False):
    _accept(run_dir, workspace, task_id)
    handle = tasks.prepare_attempt(run_dir, workspace=workspace, task_id=task_id)
    sidecar = run_dir / f"batch-result-{hashlib.sha256(task_id.encode()).hexdigest()}.json"
    aboyeur._write_json(sidecar, {"task_id": task_id, "text": SENTINEL})
    digest = hashlib.sha256(sidecar.read_bytes()).hexdigest()
    result = tasks.observe_task(
        run_dir,
        workspace=workspace,
        handle=handle,
        observation=tasks.TaskObservation(
            status="completed",
            containment="quiescent",
            artifact_kind="receipt",
            artifact_digest=digest,
        ),
    )
    if acknowledged:
        tasks.acknowledge_result(run_dir, workspace=workspace, task_id=task_id, result_id=result.result_id)
    return result


def _fail(run_dir, workspace, task_id):
    _accept(run_dir, workspace, task_id)
    handle = tasks.prepare_attempt(run_dir, workspace=workspace, task_id=task_id)
    return tasks.observe_task(
        run_dir,
        workspace=workspace,
        handle=handle,
        observation=tasks.TaskObservation(status="failed", containment="quiescent"),
    )


def _wait_for_children(run_dir, workspace, task_id):
    _accept(run_dir, workspace, task_id)
    handle = tasks.prepare_attempt(run_dir, workspace=workspace, task_id=task_id)
    return tasks.observe_task(
        run_dir,
        workspace=workspace,
        handle=handle,
        observation=tasks.TaskObservation(status="completed", containment="active"),
    )


@pytest.fixture
def populated(task_run):
    run_dir, workspace = task_run
    _accept(run_dir, workspace, "queued")
    _complete(run_dir, workspace, "done", acknowledged=True)
    _fail(run_dir, workspace, "failed")
    _wait_for_children(run_dir, workspace, "waiting")
    # Task text lives outside the journal; the read surface must never echo it.
    aboyeur._write_json(
        run_dir / "batch-request.json",
        {"request_id": "batch-one", "tasks": [{"task_id": "queued", "text": SENTINEL}]},
    )
    return run_dir, workspace


def _output(captured):
    return captured.out + captured.err


def test_list_shows_every_task_and_hides_task_text(populated, capsys):
    _run_dir, workspace = populated
    assert cli.main(["runs", "tasks", "list", "test-task-run", "--cwd", str(workspace)]) == 0
    captured = capsys.readouterr()
    output = _output(captured)

    for task_id in ("queued", "done", "failed", "waiting"):
        assert task_id in output
    assert "waiting-for-children" in output
    assert "completed" in output
    assert "failed" in output
    # Acknowledgement and terminality are separate visible columns.
    assert "yes" in output
    assert SENTINEL not in output


def test_list_json_contract_has_schema_and_stable_keys(populated, capsys):
    run_dir, workspace = populated
    assert cli.main(["runs", "tasks", "list", "test-task-run", "--cwd", str(workspace), "--json"]) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert payload["schema"] == "brigade.runs_tasks.v1"
    assert payload["run_id"] == "test-task-run"
    assert list(payload) == sorted(payload)
    rows = {row["task_id"]: row for row in payload["tasks"]}
    assert list(rows) == ["queued", "done", "failed", "waiting"]
    assert set(rows["done"]) == {
        "task_id",
        "status",
        "attempt_id",
        "terminal",
        "acknowledged",
        "containment",
        "result_id",
        "artifact_kind",
        "artifact_digest",
    }
    assert rows["queued"]["status"] == "queued"
    assert rows["queued"]["attempt_id"] is None
    assert rows["queued"]["terminal"] is False
    assert rows["queued"]["acknowledged"] is False
    assert rows["done"]["status"] == "completed"
    assert rows["done"]["terminal"] is True
    assert rows["done"]["acknowledged"] is True
    assert rows["done"]["containment"] == "quiescent"
    assert rows["done"]["result_id"]
    assert rows["done"]["artifact_kind"] == "receipt"
    assert rows["failed"]["status"] == "failed"
    assert rows["failed"]["terminal"] is True
    assert rows["failed"]["acknowledged"] is False
    assert rows["failed"]["artifact_kind"] == "unavailable"
    assert rows["failed"]["artifact_digest"] is None
    assert rows["waiting"]["status"] == "waiting-for-children"
    assert rows["waiting"]["terminal"] is False
    assert SENTINEL not in _output(captured)


def test_show_reports_result_sidecar_reference_and_digest(populated, capsys):
    run_dir, workspace = populated
    expected = next(result for result in tasks.read_tasks(run_dir) if result.handle.spec.task_id == "done")
    sidecar = run_dir / f"batch-result-{hashlib.sha256(b'done').hexdigest()}.json"
    expected_digest = hashlib.sha256(sidecar.read_bytes()).hexdigest()

    assert cli.main(["runs", "tasks", "show", str(run_dir), "done"]) == 0
    captured = capsys.readouterr()
    output = _output(captured)
    assert f"result_id: {expected.result_id}" in output
    assert f"result_ref: {sidecar.name}" in output
    assert f"result_sha256: {expected_digest}" in output
    assert "containment: quiescent" in output
    assert "acknowledged: yes" in output
    assert SENTINEL not in output

    assert cli.main(["runs", "tasks", "show", str(run_dir), "done", "--json"]) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["schema"] == "brigade.runs_tasks.v1"
    assert list(payload) == sorted(payload)
    assert payload["task"]["task_id"] == "done"
    assert payload["task"]["result_id"] == expected.result_id
    assert payload["task"]["result_ref"] == sidecar.name
    assert payload["task"]["result_sha256"] == expected_digest
    assert SENTINEL not in _output(captured)


def test_show_non_terminal_task_has_no_result_reference(populated, capsys):
    run_dir, _workspace = populated
    assert cli.main(["runs", "tasks", "show", str(run_dir), "queued", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["task"]["status"] == "queued"
    assert payload["task"]["result_ref"] is None
    assert payload["task"]["result_sha256"] is None


def test_unknown_task_exits_2_and_lists_known_ids(populated, capsys):
    run_dir, _workspace = populated
    assert cli.main(["runs", "tasks", "show", str(run_dir), "missing"]) == 2
    captured = capsys.readouterr()
    output = _output(captured)
    assert "unknown-task" in output
    assert "done, failed, queued, waiting" in output
    assert SENTINEL not in output


def test_known_task_ids_are_sorted_and_bounded():
    ids = [f"task-{index:02d}" for index in range(12)]
    assert runs_cmd_tasks._bounded_ids(reversed(ids)) == sorted(ids)[:10]


def test_missing_journal_exits_2_no_task_journal(populated, capsys):
    run_dir, _workspace = populated
    (run_dir / "events" / "lifecycle.jsonl").unlink()

    assert cli.main(["runs", "tasks", "list", str(run_dir)]) == 2
    captured = capsys.readouterr()
    assert "no-task-journal" in _output(captured)

    assert cli.main(["runs", "tasks", "wait", str(run_dir), "--timeout", "0"]) == 2
    captured = capsys.readouterr()
    assert "no-task-journal" in _output(captured)


def test_wait_returns_zero_when_terminal(populated, capsys):
    run_dir, _workspace = populated
    assert cli.main(["runs", "tasks", "wait", str(run_dir), "done", "--timeout", "0"]) == 0
    captured = capsys.readouterr()
    assert "done" in captured.out
    assert SENTINEL not in _output(captured)

    assert cli.main(["runs", "tasks", "wait", str(run_dir), "done", "failed", "--timeout", "0", "--json"]) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["schema"] == "brigade.runs_tasks.v1"
    assert payload["timed_out"] is False
    assert payload["waiting_for"] == []
    assert [row["task_id"] for row in payload["tasks"]] == ["done", "failed"]


def test_wait_timeout_exits_3_without_changing_the_journal(populated, capsys):
    run_dir, _workspace = populated
    journal_path = run_dir / "events" / "lifecycle.jsonl"
    before = journal_path.read_bytes()
    before_state = {result.handle.spec.task_id: result for result in tasks.read_tasks(run_dir)}

    assert cli.main(["runs", "tasks", "wait", str(run_dir), "queued", "waiting", "--timeout", "0"]) == 3
    captured = capsys.readouterr()
    output = _output(captured)
    assert "wait-timeout" in output
    assert "queued" in output and "waiting" in output
    assert journal_path.read_bytes() == before
    after_state = {result.handle.spec.task_id: result for result in tasks.read_tasks(run_dir)}
    assert after_state == before_state
    assert SENTINEL not in output

    assert cli.main(["runs", "tasks", "wait", str(run_dir), "queued", "--timeout", "0", "--json"]) == 3
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["timed_out"] is True
    assert payload["waiting_for"] == ["queued"]
    assert [row["task_id"] for row in payload["tasks"]] == ["queued"]
    assert payload["tasks"][0]["terminal"] is False
    assert journal_path.read_bytes() == before


def test_wait_unknown_task_exits_2(populated, capsys):
    run_dir, _workspace = populated
    assert cli.main(["runs", "tasks", "wait", str(run_dir), "missing", "--timeout", "0"]) == 2
    assert "unknown-task" in _output(capsys.readouterr())


def test_wait_polls_at_a_bounded_interval_until_terminal(populated):
    run_dir, workspace = populated
    sleeps: list[float] = []

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 1:
            handle = tasks.prepare_attempt(run_dir, workspace=workspace, task_id="queued")
            tasks.observe_task(
                run_dir,
                workspace=workspace,
                handle=handle,
                observation=tasks.TaskObservation(status="completed", containment="quiescent"),
            )

    assert (
        runs_cmd_tasks.wait_tasks(
            str(run_dir),
            ["queued"],
            cwd=Path("."),
            interval=runs_cmd_tasks.POLL_INTERVAL_SECONDS,
            sleep=sleep,
        )
        == 0
    )
    assert sleeps == [runs_cmd_tasks.POLL_INTERVAL_SECONDS]


def test_terminal_states_come_from_the_shared_journal_definition():
    assert task_journal.TERMINAL == frozenset({"completed", "failed", "interrupted", "cancelled"})
    for status in ("waiting-for-children", "unknown", "waiting-for-input", "queued", "running"):
        assert status not in task_journal.TERMINAL
