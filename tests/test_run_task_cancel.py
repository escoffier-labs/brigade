"""Recorded per-task cancel intent, honored before launch and on resume (#2049, S2a)."""

from __future__ import annotations

import hashlib
import json
import os

import pytest

from tests._home import set_home
from tests.test_run_batch_worktrees import Site, invoke
from tests.test_run_batch_worktrees import site as worktree_site  # noqa: F401  (fixture for getfixturevalue)

from brigade import aboyeur, agents, cli, run_events, runguard, run_journal, run_lifecycle
from brigade import run_task_inbox as inbox
from brigade import run_tasks

posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX no-follow inbox checks")


def _run_meta(workspace) -> dict[str, object]:
    return {
        "schema": "brigade.run.v1",
        "status": "running",
        "lock_workspace": str(workspace),
        "lifecycle_journal_requested": True,
        "approval_reference": {
            key: "test-reference"
            for key in (
                "approval_id",
                "source",
                "fingerprint",
                "source_fingerprint",
                "contract_fingerprint",
                "evidence_fingerprint",
            )
        },
    }


def _spec(task_id: str, seat: str, text: str) -> run_tasks.TaskSpec:
    return run_tasks.TaskSpec(task_id, seat, hashlib.sha256(text.encode()).hexdigest())


@pytest.fixture
def run_site(tmp_path, monkeypatch):
    """A run with two queued tasks; its lock is released before the test body."""
    home = tmp_path / "home"
    home.mkdir()
    set_home(monkeypatch, home)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / ("batch-" + hashlib.sha256(b"req").hexdigest())
    run_dir.mkdir(parents=True)
    with runguard.run_lock(workspace, run_dir=run_dir):
        aboyeur._write_json(run_dir / "run.json", _run_meta(workspace))
        run_tasks.accept_task_batch(
            run_dir,
            workspace=workspace,
            request_id="req",
            specs=(_spec("a", "worker", "task a"), _spec("b", "worker", "task b")),
        )
    return run_dir, workspace


@pytest.fixture
def held_run(run_site):
    """The same run, with this process holding its lock as the live owner."""
    run_dir, workspace = run_site
    with runguard.run_lock(workspace, run_dir=run_dir):
        yield run_dir, workspace


@pytest.fixture
def site(request):
    """The per-task worktree/batch site from the worktree suite, unchanged."""
    return request.getfixturevalue("worktree_site")


def cancel(run_dir, capsys, *task_ids: str, reason: str = "operator"):
    argv = ["runs", "tasks", "cancel", str(run_dir), *task_ids, "--reason", reason, "--json"]
    rc = cli.main(argv)
    captured = capsys.readouterr()
    assert captured.out, captured.err
    return rc, json.loads(captured.out)


def row(payload: dict, task_id: str) -> dict:
    return next(item for item in payload["tasks"] if item["task_id"] == task_id)


def facts(run_dir, event_type: str):
    report = run_journal.read_journal_bounded(run_lifecycle._journal_path(run_dir))
    return [event for event in report.events if event.event_type == event_type]


def inbox_names(run_dir):
    path = run_dir / "task-inbox"
    return sorted(entry.name for entry in path.iterdir()) if path.is_dir() else []


def state_of(run_dir, task_id: str) -> run_tasks.TaskResult:
    return next(result for result in run_tasks.read_tasks(run_dir) if result.handle.spec.task_id == task_id)


def prepare_batch_run(site: Site, task_names: list[str]) -> tuple[dict, str]:
    """Accept a batch before any dispatch, so a request can arrive pre-launch."""
    tasks = [site.task(name, read_only=True) for name in task_names]
    payload = {"schema": "brigade.run_batch.v1", "request_id": "req", "tasks": tasks}
    run_dir = site.repo / ".brigade" / "runs" / ("batch-" + hashlib.sha256(b"req").hexdigest())
    run_dir.mkdir(parents=True)
    with runguard.run_lock(site.repo, run_dir=run_dir):
        aboyeur._write_json(run_dir / "run.json", _run_meta(site.repo))
        run_tasks.accept_task_batch(
            run_dir,
            workspace=site.repo,
            request_id="req",
            specs=tuple(_spec(task["task_id"], task["seat"], task["text"]) for task in tasks),
        )
    return payload, run_dir.name


def record_launches(monkeypatch, prompts: list[str]) -> None:
    def model(agent, prompt, **kwargs):
        prompts.append(prompt)
        return agents.AgentResult(text="done", ok=True)

    monkeypatch.setattr(agents, "run_agent", model)


def test_cancel_before_launch_skips_the_task_and_keeps_the_sibling_result(site, capsys, monkeypatch):
    tasks, _run_id = prepare_batch_run(site, ["a", "b"])
    assert inbox.submit(site.run_dir(), "cancel", "b", reason="operator") == "created"
    prompts: list[str] = []
    record_launches(monkeypatch, prompts)

    rc, result, err = invoke(site, capsys, tasks["tasks"])

    assert rc == 1, err
    assert [prompt for prompt in prompts if "Batch task_id: b" in prompt] == []
    assert len([prompt for prompt in prompts if "Batch task_id: a" in prompt]) == 1
    assert result["tasks"]["a"]["status"] == "completed"
    assert result["tasks"]["a"]["result_id"]
    assert result["tasks"]["b"]["status"] == "cancelled"
    cancelled = facts(site.run_dir(), "task.observed")
    b_events = [event for event in cancelled if event.payload["task_id"] == "b"]
    assert [(event.payload["status"], event.payload["containment"]) for event in b_events] == [
        ("cancelled", "not-launched")
    ]
    assert [event for event in facts(site.run_dir(), "task.launch-claimed") if event.payload["task_id"] == "b"] == []


def test_intent_between_prepared_attempt_and_claim_is_cancelled_before_launch(held_run):
    run_dir, workspace = held_run
    handle = run_tasks.prepare_attempt(run_dir, workspace=workspace, task_id="a")
    stale_version = state_of(run_dir, "a").version
    assert inbox.submit(run_dir, "cancel", "a", reason="parent") == "created"
    assert inbox.drain(run_dir, workspace) == {"a": "recorded"}

    # The version captured at prepare is now stale; the cancel intent must win
    # instead of surfacing a generic history refusal.
    decision = run_tasks.claim_task_launch(
        run_dir,
        workspace=workspace,
        handle=handle,
        expected_version=stale_version,
        seat="worker",
        dispatch_attempt=1,
    )

    assert decision.outcome == "cancelled-before-launch"
    assert decision.task.cancel_requested is True
    assert facts(run_dir, "task.launch-claimed") == []


def test_cancel_of_a_terminal_task_writes_nothing(run_site, capsys):
    run_dir, workspace = run_site
    with runguard.run_lock(workspace, run_dir=run_dir):
        handle = run_tasks.prepare_attempt(run_dir, workspace=workspace, task_id="a")
        run_tasks.observe_task(
            run_dir,
            workspace=workspace,
            handle=handle,
            observation=run_tasks.TaskObservation(status="completed", containment="quiescent"),
        )
    before = (run_dir / "events" / "lifecycle.jsonl").read_bytes()

    rc, payload = cancel(run_dir, capsys, "a")

    assert rc == 2
    assert row(payload, "a")["outcome"] == "already-terminal"
    assert facts(run_dir, "task.cancel-requested") == []
    assert inbox_names(run_dir) == []
    assert (run_dir / "events" / "lifecycle.jsonl").read_bytes() == before
    assert state_of(run_dir, "a").status == "completed"


def test_cancel_while_a_live_owner_holds_the_lock_only_queues_the_request(held_run, capsys):
    run_dir, _workspace = held_run
    before = (run_dir / "events" / "lifecycle.jsonl").read_bytes()

    rc, payload = cancel(run_dir, capsys, "b")

    assert rc == 0
    assert row(payload, "b")["outcome"] == "queued"
    assert len(inbox_names(run_dir)) == 1
    assert facts(run_dir, "task.cancel-requested") == []
    assert (run_dir / "events" / "lifecycle.jsonl").read_bytes() == before


def test_cancel_without_a_live_batch_journals_and_resume_never_launches(site, capsys, monkeypatch):
    tasks, _run_id = prepare_batch_run(site, ["a", "b"])
    run_dir = site.run_dir()
    rc, payload = cancel(run_dir, capsys, "b", reason="budget")
    assert rc == 0
    assert row(payload, "b")["outcome"] == "recorded"
    assert [event.payload["reason"] for event in facts(run_dir, "task.cancel-requested")] == ["budget"]
    prompts: list[str] = []
    record_launches(monkeypatch, prompts)

    # The same request replayed: a queued task with an intent is never launched.
    rc, result, err = invoke(site, capsys, tasks["tasks"])

    assert rc == 1, err
    assert [prompt for prompt in prompts if "Batch task_id: b" in prompt] == []
    assert result["tasks"]["b"]["status"] == "cancelled"
    assert len(facts(run_dir, "task.cancel-requested")) == 1


def test_duplicate_cancel_is_one_fact_and_one_file(run_site, capsys):
    run_dir, _workspace = run_site
    first_rc, first = cancel(run_dir, capsys, "a")
    second_rc, second = cancel(run_dir, capsys, "a", reason="budget")

    assert (first_rc, second_rc) == (0, 0)
    assert row(first, "a")["outcome"] == row(second, "a")["outcome"] == "recorded"
    assert len(facts(run_dir, "task.cancel-requested")) == 1
    assert len(inbox_names(run_dir)) == 1
    # The first request is never overwritten by a later reason.
    assert [event.payload["reason"] for event in facts(run_dir, "task.cancel-requested")] == ["operator"]


def test_ack_kind_is_refused_until_it_is_read(run_site):
    run_dir, _workspace = run_site
    with pytest.raises(inbox.TaskInboxError) as refused:
        inbox.submit(run_dir, "ack", "a", result_id="c" * 64)
    assert refused.value.code == "ack-unsupported"


def test_malformed_inbox_files_are_skipped(run_site):
    run_dir, workspace = run_site
    assert inbox.submit(run_dir, "cancel", "a") == "created"
    broken = run_dir / "task-inbox" / inbox.request_name("cancel", "b")
    broken.write_text("{not json")
    with runguard.run_lock(workspace, run_dir=run_dir):
        assert inbox.drain(run_dir, workspace) == {"a": "recorded"}
    assert inbox.pending(run_dir).malformed == (broken.name,)
    assert state_of(run_dir, "a").cancel_requested is True
    assert state_of(run_dir, "b").cancel_requested is False


def test_oversized_inbox_file_refuses_without_crashing_the_batch(site, capsys, monkeypatch):
    tasks, _run_id = prepare_batch_run(site, ["a", "b"])
    run_dir = site.run_dir()
    oversized = run_dir / "task-inbox" / inbox.request_name("cancel", "b")
    oversized.parent.mkdir(mode=0o700, exist_ok=True)
    oversized.write_text("x" * (inbox.MAX_REQUEST_BYTES + 1))
    with pytest.raises(inbox.TaskInboxError) as refused:
        inbox.pending(run_dir)
    assert refused.value.code == "inbox-overflow"
    prompts: list[str] = []
    record_launches(monkeypatch, prompts)

    rc, result, err = invoke(site, capsys, tasks["tasks"])

    # The refused inbox is reported and skipped; the batch still launches.
    assert "task cancel inbox refused" in err
    assert result["tasks"]["b"]["status"] == "completed"
    assert len([prompt for prompt in prompts if "Batch task_id: b" in prompt]) == 1


@posix_only
def test_symlinked_inbox_directory_and_request_are_refused(run_site, tmp_path):
    run_dir, _workspace = run_site
    outside = tmp_path / "outside"
    outside.mkdir()
    (run_dir / "task-inbox").symlink_to(outside)
    with pytest.raises(inbox.TaskInboxError) as directory_refused:
        inbox.submit(run_dir, "cancel", "a")
    assert directory_refused.value.code == "inbox-refused"

    (run_dir / "task-inbox").unlink()
    (run_dir / "task-inbox").mkdir(mode=0o700)
    target = run_dir / inbox.request_name("cancel", "a")
    target.write_text("{}")
    (run_dir / "task-inbox" / inbox.request_name("cancel", "b")).symlink_to(target)
    with pytest.raises(inbox.TaskInboxError) as request_refused:
        inbox.pending(run_dir)
    assert request_refused.value.code == "inbox-refused"


def test_cancel_fact_is_v3_and_every_other_event_stays_v2(run_site, capsys):
    run_dir, _workspace = run_site
    cancel(run_dir, capsys, "a")
    report = run_journal.read_journal_bounded(run_lifecycle._journal_path(run_dir))
    versions = {event.event_type: event.schema_version for event in report.events}

    assert versions["task.cancel-requested"] == run_events.WORKSPACE_SCHEMA_VERSION
    assert versions["task.batch-accepted"] == run_events.SCHEMA_VERSION


def test_runs_tasks_show_and_list_report_cancel_state(run_site, capsys):
    run_dir, _workspace = run_site
    cancel(run_dir, capsys, "a")
    assert inbox.submit(run_dir, "cancel", "b") == "created"

    assert cli.main(["runs", "tasks", "show", str(run_dir), "a", "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)["task"]
    assert shown["cancel"] == {"requested": True, "queued": False, "reason": "operator"}

    assert cli.main(["runs", "tasks", "list", str(run_dir), "--json"]) == 0
    listed = {item["task_id"]: item for item in json.loads(capsys.readouterr().out)["tasks"]}
    assert listed["a"]["cancel"] == {"requested": True, "queued": False, "reason": "operator"}
    assert listed["b"]["cancel"] == {"requested": False, "queued": True, "reason": "operator"}


def test_runs_tasks_cancel_reports_unknown_task(run_site, capsys):
    run_dir, _workspace = run_site
    rc, payload = cancel(run_dir, capsys, "missing")
    assert rc == 2
    assert row(payload, "missing")["outcome"] == "unknown-task"
    assert inbox_names(run_dir) == []
