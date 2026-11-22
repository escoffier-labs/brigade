"""Recorded per-task cancel intent, honored before launch and on resume (#2049, S2a)."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from tests._home import set_home
from tests.test_run_batch_worktrees import Site, invoke
from tests.test_run_batch_worktrees import site as worktree_site  # noqa: F401  (fixture for getfixturevalue)

from brigade import aboyeur, agents, cli, proc, run_batch_artifacts, run_events, runguard, run_journal, run_lifecycle
from brigade import run_task_inbox as inbox
from brigade import run_tasks
from brigade.run_batch_execute import BatchExecution

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


def accept_batch(site: Site, tasks: list[dict]) -> dict:
    """Accept explicit task dicts before dispatch, like ``prepare_batch_run``."""
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
    return {"schema": "brigade.run_batch.v1", "request_id": "req", "tasks": tasks}


def launched(site: Site, task_id: str) -> int:
    """Launches by journal claim: later-wave prompts quote earlier tasks as context."""
    return len([event for event in facts(site.run_dir(), "task.launch-claimed") if event.payload["task_id"] == task_id])


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
    assert shown["cancel"] == {"requested": True, "queued": False, "reason": "operator", "too_late": False}

    assert cli.main(["runs", "tasks", "list", str(run_dir), "--json"]) == 0
    listed = {item["task_id"]: item for item in json.loads(capsys.readouterr().out)["tasks"]}
    assert listed["a"]["cancel"] == {"requested": True, "queued": False, "reason": "operator", "too_late": False}
    assert listed["b"]["cancel"] == {"requested": False, "queued": True, "reason": "operator"}


def test_runs_tasks_cancel_reports_unknown_task(run_site, capsys):
    run_dir, _workspace = run_site
    rc, payload = cancel(run_dir, capsys, "missing")
    assert rc == 2
    assert row(payload, "missing")["outcome"] == "unknown-task"
    assert inbox_names(run_dir) == []


def test_cancel_before_launch_does_not_skip_siblings_in_later_waves(site, capsys, monkeypatch):
    # max_workers is 2, so one dependency layer of three tasks runs as two waves.
    tasks = accept_batch(site, [site.task(name, read_only=True) for name in ("a", "b", "c")])
    assert inbox.submit(site.run_dir(), "cancel", "a", reason="operator") == "created"
    prompts: list[str] = []
    record_launches(monkeypatch, prompts)

    rc, result, err = invoke(site, capsys, tasks["tasks"])

    assert rc == 1, err
    assert (launched(site, "a"), launched(site, "b"), launched(site, "c")) == (0, 1, 1)
    assert result["tasks"]["a"]["status"] == "cancelled"
    assert result["tasks"]["b"]["status"] == "completed"
    assert result["tasks"]["c"]["status"] == "completed"


def test_dependent_of_a_cancelled_task_stays_queued_and_unrelated_tasks_run(site, capsys, monkeypatch):
    tasks = accept_batch(
        site,
        [
            site.task("a", read_only=True),
            site.task("b", read_only=True),
            site.task("c", read_only=True, depends_on=["a"]),
            site.task("d", read_only=True, depends_on=["b"]),
        ],
    )
    assert inbox.submit(site.run_dir(), "cancel", "a", reason="operator") == "created"
    prompts: list[str] = []
    record_launches(monkeypatch, prompts)

    rc, result, err = invoke(site, capsys, tasks["tasks"])

    assert rc == 1, err
    assert [launched(site, name) for name in "abcd"] == [0, 1, 0, 1]
    assert result["tasks"]["a"]["status"] == "cancelled"
    assert result["tasks"]["b"]["status"] == "completed"
    assert result["tasks"]["d"]["status"] == "completed"
    # The dependent was never launched or journaled: it keeps its queued state.
    assert result["tasks"]["c"]["status"] == "queued"
    assert state_of(site.run_dir(), "c").handle.attempt_id is None


def test_drain_failure_under_the_lock_is_a_refusal_not_queued(run_site, capsys, monkeypatch):
    run_dir, _workspace = run_site

    def broken(*args, **kwargs):
        raise inbox.TaskInboxError("journal-unreadable", "task journal is not readable")

    monkeypatch.setattr(inbox, "drain", broken)
    rc, payload = cancel(run_dir, capsys, "a")

    assert rc == 2
    assert payload["refusal"]["code"] == "journal-unreadable"
    assert payload["tasks"] == []


@posix_only
def test_request_swapped_for_a_fifo_after_inspection_is_refused_without_crashing(held_run, monkeypatch):
    run_dir, _workspace = held_run
    assert inbox.submit(run_dir, "cancel", "a", reason="operator") == "created"
    name = inbox_names(run_dir)[0]
    real_stat = inbox.dirfd.stat_child

    def swap_then_stat(descriptor, child):
        info = real_stat(descriptor, child)
        if child == name:
            target = run_dir / "task-inbox" / name
            target.unlink()
            os.mkfifo(target, 0o600)
        return info

    monkeypatch.setattr(inbox.dirfd, "stat_child", swap_then_stat)
    with pytest.raises(inbox.TaskInboxError) as excinfo:
        inbox.pending(run_dir)
    assert excinfo.value.code in {"inbox-refused", "inbox-unavailable"}


def test_intent_after_attempt_preparation_is_honored_at_the_claim_in_a_live_batch(site, capsys, monkeypatch):
    # The request arrives after the launch fence prepared "a", so only the
    # recorder's drain before the claim can see it. Later waves still run.
    tasks = accept_batch(site, [site.task(name, read_only=True) for name in ("a", "b", "c")])
    prompts: list[str] = []
    record_launches(monkeypatch, prompts)
    real_prepare = run_tasks.prepare_attempt

    def prepare_then_cancel(run_dir, *, workspace, task_id, **kwargs):
        handle = real_prepare(run_dir, workspace=workspace, task_id=task_id, **kwargs)
        if task_id == "a":
            inbox.submit(run_dir, "cancel", "a", reason="parent")
        return handle

    monkeypatch.setattr(run_tasks, "prepare_attempt", prepare_then_cancel)

    rc, result, err = invoke(site, capsys, tasks["tasks"])

    assert rc == 1, err
    assert (launched(site, "a"), launched(site, "b"), launched(site, "c")) == (0, 1, 1)
    assert result["tasks"]["a"]["status"] == "cancelled"
    assert [event for event in facts(site.run_dir(), "task.launch-claimed") if event.payload["task_id"] == "a"] == []


def test_partial_multi_task_submission_reports_the_written_requests(run_site, capsys, monkeypatch):
    run_dir, _workspace = run_site
    real_submit = inbox.submit

    def fail_on_b(run_dir_arg, kind, task_id, **kwargs):
        if task_id == "b":
            raise inbox.TaskInboxError("inbox-unavailable", "the task inbox could not be written")
        return real_submit(run_dir_arg, kind, task_id, **kwargs)

    monkeypatch.setattr(inbox, "submit", fail_on_b)
    rc, payload = cancel(run_dir, capsys, "a", "b")

    assert rc == 2
    assert payload["refusal"]["code"] == "inbox-unavailable"
    assert row(payload, "a")["outcome"] == "recorded"
    assert row(payload, "b")["outcome"] == "not-recorded"
    assert [event.payload["task_id"] for event in facts(run_dir, "task.cancel-requested")] == ["a"]


def test_queued_request_is_always_reported_as_possibly_running(held_run, capsys):
    run_dir, _workspace = held_run
    rc, payload = cancel(run_dir, capsys, "a")

    assert rc == 0
    assert row(payload, "a")["outcome"] == "queued"
    assert row(payload, "a")["live_termination"] == "live-termination-unavailable"


@posix_only
def test_publish_without_hard_links_renames_a_complete_file_and_a_crash_leaves_no_request(held_run, monkeypatch):
    # Simulate a filesystem with no hard links (the Windows fallback path): the
    # link fails and only the check right after it sees a non-POSIX platform.
    run_dir, _workspace = held_run
    real_replace = inbox.dirfd.replace_children
    real_posix = inbox.dirfd.posix_available
    state = {"link_failed": False}
    renamed: list[bool] = []

    def no_links(parent, source, destination):
        state["link_failed"] = True
        raise OSError(errno.EPERM, "hard links unavailable")

    def no_replace_rename(parent, source, destination, *, replace=True):
        state["link_failed"] = False
        renamed.append(replace)
        real_replace(parent, source, destination, replace=replace)

    monkeypatch.setattr(inbox.dirfd, "link_child", no_links)
    monkeypatch.setattr(inbox.dirfd, "posix_available", lambda: real_posix() and not state["link_failed"])
    monkeypatch.setattr(inbox.dirfd, "replace_children", no_replace_rename)

    real_write = inbox._write_all

    def crash(descriptor, data):
        raise OSError("crash during write")

    monkeypatch.setattr(inbox, "_write_all", crash)
    with pytest.raises(OSError):
        inbox.submit(run_dir, "cancel", "a", reason="operator")
    # A write that dies leaves no final-name request behind, so a retry is not blocked.
    assert [name for name in inbox_names(run_dir) if not name.startswith(".")] == []

    monkeypatch.setattr(inbox, "_write_all", real_write)
    assert inbox.submit(run_dir, "cancel", "a", reason="operator") == "created"
    assert renamed == [False]
    assert [request.task_id for request in inbox.pending(run_dir).requests] == ["a"]


@posix_only
def test_live_cancel_retains_partial_work_and_same_seat_siblings(site, capsys, monkeypatch):
    tasks = accept_batch(
        site,
        [site.task("a", isolation="worktree"), site.task("b", read_only=True), site.task("c", read_only=True)],
    )
    ready = {name: threading.Event() for name in ("a", "b")}
    target_exited = threading.Event()
    watchers = []
    watch = BatchExecution._watch_cancels

    def watch_and_record(self, stopped, **kwargs):
        watchers.append(threading.current_thread())
        watch(self, stopped, **kwargs)

    monkeypatch.setattr(BatchExecution, "_watch_cancels", watch_and_record)

    def model(agent, prompt, *, process_registry, cwd, **kwargs):
        name = prompt.split("Batch task_id: ", 1)[1].splitlines()[0]
        if name == "c":
            return agents.AgentResult(text="later wave", ok=True)
        if name == "a":
            (cwd / "partial.txt").write_text("retained partial work\n")
        child = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.stdin.read(); print('sibling result')"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            **proc.process_group_kwargs(),
        )
        try:
            process_registry.register(child)
            ready[name].set()
            if name == "a":
                code = child.wait(timeout=10)
                target_exited.set()
                assert code < 0
                return agents.AgentResult(text="", ok=False)
            assert target_exited.wait(10)
            assert child.poll() is None
            output, _ = child.communicate(timeout=10)
            return agents.AgentResult(text=output.decode().strip(), ok=child.returncode == 0)
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=10)
            process_registry.unregister(child)

    monkeypatch.setattr(agents, "run_agent", model)

    def request_cancel():
        assert ready["a"].wait(10) and ready["b"].wait(10)
        assert inbox._has_live_watcher(site.run_dir(), site.repo)
        return inbox.cancel(site.run_dir(), ["a"], workspace=site.repo)

    with ThreadPoolExecutor(max_workers=1) as pool:
        submitted = pool.submit(request_cancel)
        rc, result, err = invoke(site, capsys, tasks["tasks"])
        answer = submitted.result(timeout=10)

    assert row(answer, "a")["outcome"] == "queued"
    assert row(answer, "a")["live_termination"] == "requested"
    assert rc == 1, err
    assert {name: task["status"] for name, task in result["tasks"].items()} == {
        "a": "cancelled",
        "b": "completed",
        "c": "completed",
    }
    assert (launched(site, "a"), launched(site, "b"), launched(site, "c")) == (1, 1, 1)
    run_dir = site.run_dir()
    terminal = [event.payload for event in facts(run_dir, "task.observed") if event.payload["task_id"] == "a"]
    assert [(event["status"], event["containment"]) for event in terminal] == [("cancelled", "interrupted")]
    assert len(facts(run_dir, "task.cancel-requested")) == 1
    assert state_of(run_dir, "a").artifact_kind == "commit"
    assert "retained partial work" in (run_dir / run_batch_artifacts.patch_name("a")).read_text()
    assert "sibling result" in (run_dir / result["tasks"]["b"]["result_ref"]).read_text()
    assert len(watchers) == 1 and not watchers[0].is_alive()


def test_cancel_after_worker_result_handoff_is_too_late(site, capsys, monkeypatch):
    tasks, _ = prepare_batch_run(site, ["a"])
    record_launches(monkeypatch, [])
    handed_off, journaled, submitted = threading.Event(), threading.Event(), threading.Event()
    publish = BatchExecution._publish_result
    drain = BatchExecution._drain_inbox

    def pause_publication(self, result, task_id, containment):
        handed_off.set()
        assert journaled.wait(10) and submitted.wait(10)
        publish(self, result, task_id, containment)

    def signal_drain(self):
        drained = drain(self)
        if drained:
            journaled.set()
        return drained

    monkeypatch.setattr(BatchExecution, "_publish_result", pause_publication)
    monkeypatch.setattr(BatchExecution, "_drain_inbox", signal_drain)

    def request_cancel():
        assert handed_off.wait(10)
        try:
            return inbox.cancel(site.run_dir(), ["a"], workspace=site.repo)
        finally:
            submitted.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        request = pool.submit(request_cancel)
        rc, result, err = invoke(site, capsys, tasks["tasks"])
        assert row(request.result(timeout=10), "a")["live_termination"] == "requested"

    assert rc == 0, err
    assert result["tasks"]["a"]["status"] == "completed"
    assert len(facts(site.run_dir(), "task.observed")) == 1
    for mode in (["show", str(site.run_dir()), "a"], ["list", str(site.run_dir())]):
        assert cli.main(["runs", "tasks", *mode, "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        task = payload["task"] if "task" in payload else payload["tasks"][0]
        assert task["status"] == "completed"
        assert task["cancel"]["too_late"] is True


def test_cancel_claimed_task_without_live_batch_reports_unavailable(run_site, capsys):
    run_dir, workspace = run_site
    with runguard.run_lock(workspace, run_dir=run_dir):
        handle = run_tasks.prepare_attempt(run_dir, workspace=workspace, task_id="a")
        decision = run_tasks.claim_task_launch(
            run_dir,
            workspace=workspace,
            handle=handle,
            expected_version=state_of(run_dir, "a").version,
            seat="worker",
            dispatch_attempt=1,
        )
        assert decision.outcome == "granted"

    rc, payload = cancel(run_dir, capsys, "a")

    assert rc == 0
    assert row(payload, "a")["outcome"] == "recorded"
    assert row(payload, "a")["live_termination"] == "live-termination-unavailable"


def test_pending_intent_drained_after_terminal_observation_keeps_result(held_run):
    run_dir, workspace = held_run
    assert inbox.submit(run_dir, "cancel", "a") == "created"
    handle = run_tasks.prepare_attempt(run_dir, workspace=workspace, task_id="a")
    observed = run_tasks.observe_task(
        run_dir,
        workspace=workspace,
        handle=handle,
        observation=run_tasks.TaskObservation(status="completed", containment="quiescent"),
    )

    assert inbox.drain(run_dir, workspace) == {"a": "recorded"}
    result = state_of(run_dir, "a")
    assert result.status == "completed"
    assert result.result_id == observed.result_id
    assert result.cancel_too_late


def test_a_worker_that_reports_cancelled_on_its_own_still_aborts_later_waves(site, capsys, monkeypatch):
    # No cancel was requested: a provider-originated "cancelled" is a failure,
    # so fail-fast still skips the later wave. Only the batch's own watcher
    # outcome (task-cancelled) is exempt.
    tasks = accept_batch(site, [site.task(name, read_only=True) for name in ("a", "b", "c")])

    def model(agent, prompt, **kwargs):
        if "Batch task_id: a\n" in prompt:
            return agents.AgentResult(text="", ok=False, status="cancelled")
        return agents.AgentResult(text="done", ok=True)

    monkeypatch.setattr(agents, "run_agent", model)
    rc, result, err = invoke(site, capsys, tasks["tasks"])

    assert rc != 0, err
    assert launched(site, "c") == 0
    assert result["tasks"]["c"]["status"] == "queued"
    assert facts(site.run_dir(), "task.cancel-requested") == []


def test_remote_routed_watcher_only_journals_intent_and_never_terminates():
    from types import SimpleNamespace

    stopped = threading.Event()
    drains: list[int] = []

    def drain():
        drains.append(1)
        stopped.set()
        return True

    def forbidden(*args, **kwargs):
        raise AssertionError("a remote-routed batch must not claim live termination")

    fake = SimpleNamespace(
        _drain_inbox=drain,
        processes=SimpleNamespace(cancel_task=forbidden),
        turns=SimpleNamespace(interrupt=forbidden),
        runtime={},
        run_dir=None,
    )
    BatchExecution._watch_cancels(fake, stopped, terminate=False)
    assert drains == [1]
