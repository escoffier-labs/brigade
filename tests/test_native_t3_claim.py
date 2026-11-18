"""Governed native T3 claims, reported correlation and holds: fail closed."""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from brigade import (
    cli,
    native_t3_dispatch as dispatch,
    native_t3_enrollment as enrollment,
    proc,
    run_journal,
    run_tasks,
)

TASK_TEXT = "retained-input-marker"
THREAD = "thread-parent"
POSIX_ONLY = pytest.mark.skipif(
    os.name == "nt", reason="native T3 custody refuses Windows for lack of owner-only DACLs"
)


def _git(repo: Path, *args: str) -> None:
    result = proc.run(["git", *args], cwd=repo)
    assert result.code == 0, result.stderr


def _workspace(tmp_path: Path, *, targets: tuple[str, ...] = ("coder",)) -> Path:
    repo = tmp_path / "workspace"
    repo.mkdir(parents=True)
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test User")
    (repo / "tracked.txt").write_text("base\n")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "initial")
    lines = ['orchestrator = "coder"', "", "[agents.coder]", 'cli = "codex"', 'role = "coder"', ""]
    for name in targets:
        lines += [
            f"[t3_targets.{name}]",
            'origin_environment = "env-origin"',
            'origin_thread = "thread-parent"',
            'target_environment = "env-target"',
            'target_project = "project-test"',
            'provider_instance = "codex-test"',
            'driver = "codex"',
            'model = "launch-model"',
            'isolation = "worktree"',
            "",
            f"[t3_targets.{name}.options]",
            'reasoningEffort = "high"',
            "",
        ]
    brigade_dir = repo / ".brigade"
    brigade_dir.mkdir()
    (brigade_dir / "roster.toml").write_text("\n".join(lines), encoding="utf-8")
    return repo


def _batch(tmp_path: Path, ids: tuple[str, ...] = ("one", "two"), *, seat: str = "coder") -> Path:
    payload = {
        "schema": "brigade.run_batch.v1",
        "request_id": "request-1",
        "tasks": [
            {"task_id": task_id, "text": f"{TASK_TEXT} {task_id}", "seat": seat, "isolation": "worktree"}
            for task_id in ids
        ],
        "synthesis": None,
    }
    path = tmp_path / "batch.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _enroll(tmp_path: Path, ids: tuple[str, ...] = ("one", "two")) -> tuple[Path, str]:
    workspace = _workspace(tmp_path)
    report = enrollment.enroll(batch_path=_batch(tmp_path, ids), target=workspace)
    return workspace, report["run_id"]


def _run_dir(workspace: Path, run_id: str) -> Path:
    return workspace / ".brigade" / "runs" / run_id


def _events(workspace: Path, run_id: str) -> list[run_journal.RunEvent]:
    return list(run_journal.read_journal_bounded(_run_dir(workspace, run_id) / "events" / "lifecycle.jsonl").events)


def _native(task_id: str = "one", **changes: object) -> dict:
    payload = {
        "taskId": task_id,
        "childThreadId": "thread-child",
        "childRunId": "run-child",
        "childNodeId": task_id,
        "status": "running",
        "workState": "working",
        "hasPendingChildRuns": False,
        "hasPendingQuestion": False,
        "pendingApproval": None,
        "waitTimedOut": False,
        "latestTerminalRunId": None,
        "latestTerminalStatus": None,
        "latestTerminalSummary": None,
        "latestTerminalResultContextTransferId": None,
        "providerInstanceId": "codex-test",
        "model": "launch-model",
        "options": [{"id": "reasoningEffort", "value": "high"}],
        "summary": None,
        "resultContextTransferId": None,
    }
    payload.update(changes)
    return payload


def _claim(workspace: Path, run_id: str, task_id: str = "one", *, parent: str = THREAD) -> dict:
    return dispatch.claim(workspace=workspace, run_id=run_id, task_id=task_id, parent_thread=parent)


@POSIX_ONLY
def test_two_tasks_get_distinct_attempts_intents_and_request_keys(tmp_path):
    workspace, run_id = _enroll(tmp_path)
    one = _claim(workspace, run_id, "one")
    two = _claim(workspace, run_id, "two")
    assert one["attempt_id"] != two["attempt_id"]
    assert one["clientRequestId"] != two["clientRequestId"]
    assert one["dispatch_attempt"] == 1 and two["dispatch_attempt"] == 2
    assert one["clientRequestId"] == enrollment.request_key(run_id, "one", one["attempt_id"])
    for report in (one, two):
        assert report["delegate_task"]["isolation"] == "worktree"
        assert report["delegate_task"]["target"] == {
            "providerInstanceId": "codex-test",
            "model": "launch-model",
            "options": [{"id": "reasoningEffort", "value": "high"}],
        }
    assert one["delegate_task"]["task"] == f"{TASK_TEXT} one"
    events = _events(workspace, run_id)
    claims = [event for event in events if event.event_type == dispatch.CLAIM_EVENT]
    assert len(claims) == 2
    assert len({event.payload["intent_digest"] for event in claims}) == 2
    requested = [event.payload["attempt"] for event in events if event.event_type == "run.dispatch.requested"]
    assert requested == [1, 2]
    journal_text = (_run_dir(workspace, run_id) / "events" / "lifecycle.jsonl").read_text(encoding="utf-8")
    assert TASK_TEXT not in journal_text


@POSIX_ONLY
def test_crash_after_claim_blocks_every_new_claim(tmp_path, monkeypatch):
    workspace, run_id = _enroll(tmp_path)

    original = dispatch._record_dispatch_requested

    def boom(*_args: object, **_kwargs: object) -> int:
        raise dispatch.DispatchError("journal-refused", "injected crash before the requested fact")

    monkeypatch.setattr(dispatch, "_record_dispatch_requested", boom)
    with pytest.raises(dispatch.DispatchError) as exc:
        _claim(workspace, run_id, "one")
    assert exc.value.code == "journal-refused"
    events = _events(workspace, run_id)
    assert [event.payload["dispatch_attempt"] for event in events if event.event_type == "task.launch-claimed"] == [1]
    assert not [event for event in events if event.event_type == "run.dispatch.requested"]

    with pytest.raises(dispatch.DispatchError) as blocked:
        _claim(workspace, run_id, "two")
    assert blocked.value.code == "run-barrier"
    assert len([event for event in _events(workspace, run_id) if event.event_type == "task.launch-claimed"]) == 1

    # Only an operator resolution frees the run. Abandoning the lost attempt
    # writes the missing request, keeps it charged and holds the task.
    monkeypatch.setattr(dispatch, "_record_dispatch_requested", original)
    dispatch.hold_resolve(workspace=workspace, run_id=run_id, task_id="one", payload=None, abandon=True)
    requested = [
        event.payload["attempt"] for event in _events(workspace, run_id) if event.event_type == "run.dispatch.requested"
    ]
    assert requested == [1]
    with pytest.raises(dispatch.DispatchError) as lost:
        _claim(workspace, run_id, "one")
    assert lost.value.code == "task-held"
    assert _claim(workspace, run_id, "two")["dispatch_attempt"] == 2
    assert [
        event.payload["attempt"] for event in _events(workspace, run_id) if event.event_type == "run.dispatch.requested"
    ] == [1, 2]


@POSIX_ONLY
def test_requested_but_unaccepted_charges_capacity_without_blocking_siblings(tmp_path):
    workspace, run_id = _enroll(tmp_path, ("one", "two", "three"))
    run_json = _run_dir(workspace, run_id) / "run.json"
    meta = json.loads(run_json.read_text(encoding="utf-8"))
    meta["run_budget"] = {
        "schema": "brigade.run_budget.v1",
        "schema_version": 1,
        "ceilings": {"wall_clock_seconds": None, "worker_dispatch_count": 2},
    }
    run_json.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    assert _claim(workspace, run_id, "one")["dispatch_attempt"] == 1
    assert _claim(workspace, run_id, "two")["dispatch_attempt"] == 2
    with pytest.raises(dispatch.DispatchError) as denied:
        _claim(workspace, run_id, "three")
    assert denied.value.code in {"capacity-denied", "budget-exhausted"}
    assert len([event for event in _events(workspace, run_id) if event.event_type == "run.dispatch.requested"]) == 2


@POSIX_ONLY
def test_concurrent_claims_grant_exactly_once(tmp_path):
    workspace, run_id = _enroll(tmp_path, ("one",))

    def attempt(_index: int) -> str:
        try:
            _claim(workspace, run_id, "one")
        except (dispatch.DispatchError, enrollment.EnrollmentError):
            return "refused"
        return "granted"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, range(2)))
    assert sorted(results) == ["granted", "refused"]
    events = _events(workspace, run_id)
    assert len([event for event in events if event.event_type == "task.launch-claimed"]) == 1
    assert len([event for event in events if event.event_type == "run.dispatch.requested"]) == 1


@POSIX_ONLY
def test_refusal_paths_have_empty_stdout_and_no_task_text(tmp_path, capsys):
    workspace, run_id = _enroll(tmp_path)
    assert _claim(workspace, run_id, "one")["state"] == "claimed"
    capsys.readouterr()

    def argv(*extra: str) -> list[str]:
        return ["t3", *extra, "--target", str(workspace)]

    native_file = tmp_path / "native.json"
    native_file.write_text(json.dumps(_native("two")), encoding="utf-8")
    cases = [
        (argv("claim", run_id, "one", "--parent-thread", THREAD), "already-claimed"),
        (argv("claim", "t3-" + "0" * 64, "one", "--parent-thread", THREAD), "run-missing"),
        (
            argv("accepted", run_id, "two", "--parent-thread", THREAD, "--native-json", str(native_file)),
            "no-claim",
        ),
        (argv("hold-resolve", run_id, "two", "--abandon"), "no-claim"),
    ]
    run_json = _run_dir(workspace, run_id) / "run.json"
    original = run_json.read_text(encoding="utf-8")
    for command, code in cases[:2]:
        assert cli.main(command) == 2
        captured = capsys.readouterr()
        assert captured.out == ""
        assert f"[{code}]" in captured.err and TASK_TEXT not in captured.err
    meta = json.loads(original)
    meta["status"] = "held"
    run_json.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    assert cli.main(argv("claim", run_id, "two", "--parent-thread", THREAD)) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "[run-held]" in captured.err and TASK_TEXT not in captured.err
    run_json.write_text(original, encoding="utf-8")
    for command, code in cases[2:]:
        assert cli.main(command) == 2
        captured = capsys.readouterr()
        assert captured.out == "" and f"[{code}]" in captured.err and TASK_TEXT not in captured.err


@pytest.mark.parametrize("tamper", ["digest", "symlink", "mode"])
@POSIX_ONLY
def test_asset_integrity_refuses_the_whole_run(tmp_path, tamper):
    workspace, run_id = _enroll(tmp_path)
    result = next(
        item for item in run_tasks.read_tasks(_run_dir(workspace, run_id)) if item.handle.spec.task_id == "one"
    )
    asset = _run_dir(workspace, run_id) / result.enrollment.asset_path
    if tamper == "digest":
        asset.write_bytes(asset.read_bytes().replace(TASK_TEXT.encode(), b"tampered-marker!"))
    elif tamper == "symlink":
        outside = tmp_path / "outside.json"
        outside.write_bytes(asset.read_bytes())
        asset.unlink()
        try:
            os.symlink(outside, asset)
        except (OSError, NotImplementedError):
            pytest.skip("symlink creation is unavailable on this platform")
    else:
        os.chmod(asset, 0o644)
    journal_path = _run_dir(workspace, run_id) / "events" / "lifecycle.jsonl"
    before = journal_path.read_bytes()
    with pytest.raises(dispatch.DispatchError) as refused:
        _claim(workspace, run_id, "two")
    assert refused.value.code in {"asset-digest", "asset-custody"}
    assert journal_path.read_bytes() == before
    assert not [event for event in _events(workspace, run_id) if event.event_type == dispatch.CLAIM_EVENT]


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("providerInstanceId", "other-provider", "provider-mismatch"),
        ("model", "other-model", "model-mismatch"),
        ("options", [], "options-mismatch"),
        ("childNodeId", "two", "native-mismatch"),
        ("taskId", "two", "native-mismatch"),
        ("childThreadId", None, "native-invalid"),
        ("childRunId", None, "native-invalid"),
    ],
)
@POSIX_ONLY
def test_accepted_refuses_mismatches(tmp_path, field, value, code):
    workspace, run_id = _enroll(tmp_path)
    _claim(workspace, run_id, "one")
    journal_path = _run_dir(workspace, run_id) / "events" / "lifecycle.jsonl"
    before = journal_path.read_bytes()
    with pytest.raises(dispatch.DispatchError) as refused:
        dispatch.accepted(
            workspace=workspace,
            run_id=run_id,
            task_id="one",
            parent_thread=THREAD,
            payload=_native(**{field: value}),
        )
    assert refused.value.code == code
    assert journal_path.read_bytes() == before


@POSIX_ONLY
def test_accepted_refuses_a_different_parent_thread(tmp_path):
    workspace, run_id = _enroll(tmp_path)
    _claim(workspace, run_id, "one")
    with pytest.raises(dispatch.DispatchError) as refused:
        dispatch.accepted(
            workspace=workspace,
            run_id=run_id,
            task_id="one",
            parent_thread="thread-other",
            payload=_native(),
        )
    assert refused.value.code == "parent-mismatch"


@POSIX_ONLY
def test_accepted_records_parent_reported_correlation_idempotently(tmp_path):
    workspace, run_id = _enroll(tmp_path)
    _claim(workspace, run_id, "one")
    first = dispatch.accepted(
        workspace=workspace, run_id=run_id, task_id="one", parent_thread=THREAD, payload=_native()
    )
    assert first["mode"] == dispatch.PARENT_REPORTED and first["replayed"] is False
    assert first["status"] == "running" and first["child_run_id"] == "run-child"
    journal_path = _run_dir(workspace, run_id) / "events" / "lifecycle.jsonl"
    before = journal_path.read_bytes()
    replay = dispatch.accepted(
        workspace=workspace, run_id=run_id, task_id="one", parent_thread=THREAD, payload=_native()
    )
    assert replay == {**first, "replayed": True}
    assert journal_path.read_bytes() == before
    correlated = [event for event in _events(workspace, run_id) if event.event_type == dispatch.CORRELATION_EVENT]
    assert len(correlated) == 1 and correlated[0].payload["mode"] == dispatch.PARENT_REPORTED
    task = next(item for item in run_tasks.read_tasks(_run_dir(workspace, run_id)) if item.handle.spec.task_id == "one")
    assert task.provider_task_id == "one" and task.original_run_id == "run-child"
    assert TASK_TEXT not in journal_path.read_text(encoding="utf-8")


@POSIX_ONLY
def test_accepted_records_a_terminal_projection(tmp_path):
    workspace, run_id = _enroll(tmp_path)
    _claim(workspace, run_id, "one")
    report = dispatch.accepted(
        workspace=workspace,
        run_id=run_id,
        task_id="one",
        parent_thread=THREAD,
        payload=_native(
            status="completed",
            workState="result_available",
            latestTerminalStatus="completed",
            latestTerminalRunId="run-child",
        ),
    )
    assert report["status"] == "completed"
    task = next(item for item in run_tasks.read_tasks(_run_dir(workspace, run_id)) if item.handle.spec.task_id == "one")
    assert task.status == "completed" and task.result_id is not None
    assert task.artifact_kind == "unavailable"


@POSIX_ONLY
def test_accepted_refuses_after_its_asset_is_tampered(tmp_path):
    workspace, run_id = _enroll(tmp_path)
    _claim(workspace, run_id, "one")
    result = next(
        item for item in run_tasks.read_tasks(_run_dir(workspace, run_id)) if item.handle.spec.task_id == "one"
    )
    asset = _run_dir(workspace, run_id) / result.enrollment.asset_path
    asset.write_bytes(asset.read_bytes().replace(TASK_TEXT.encode(), b"tampered-marker!"))
    with pytest.raises(dispatch.DispatchError) as refused:
        dispatch.accepted(workspace=workspace, run_id=run_id, task_id="one", parent_thread=THREAD, payload=_native())
    assert refused.value.code == "asset-digest"


@POSIX_ONLY
def test_second_different_handle_holds_the_task_only(tmp_path):
    workspace, run_id = _enroll(tmp_path)
    _claim(workspace, run_id, "one")
    first = {"childThreadId": "thread-child-a", "childRunId": "run-child-a"}
    dispatch.accepted(workspace=workspace, run_id=run_id, task_id="one", parent_thread=THREAD, payload=_native(**first))
    with pytest.raises(dispatch.DispatchError) as conflict:
        dispatch.accepted(
            workspace=workspace,
            run_id=run_id,
            task_id="one",
            parent_thread=THREAD,
            payload=_native(childThreadId="thread-child-b", childRunId="run-child-b"),
        )
    assert conflict.value.code == "accept-conflict"
    held = [event for event in _events(workspace, run_id) if event.event_type == dispatch.HELD_EVENT]
    assert [event.payload["reason"] for event in held] == [dispatch.ACCEPT_CONFLICT]
    meta = json.loads((_run_dir(workspace, run_id) / "run.json").read_text(encoding="utf-8"))
    assert meta["status"] == enrollment.AWAITING_NATIVE_PARENT
    with pytest.raises(dispatch.DispatchError) as held_claim:
        _claim(workspace, run_id, "one")
    assert held_claim.value.code == "task-held"


@POSIX_ONLY
def test_hold_resolve_abandon_never_regrants(tmp_path):
    workspace, run_id = _enroll(tmp_path)
    _claim(workspace, run_id, "one")
    report = dispatch.hold_resolve(workspace=workspace, run_id=run_id, task_id="one", payload=None, abandon=True)
    assert report["mode"] == dispatch.ABANDONED and report["replayed"] is False
    replay = dispatch.hold_resolve(workspace=workspace, run_id=run_id, task_id="one", payload=None, abandon=True)
    assert replay["replayed"] is True
    with pytest.raises(dispatch.DispatchError) as held:
        _claim(workspace, run_id, "one")
    assert held.value.code == "task-held"
    events = _events(workspace, run_id)
    assert len([event for event in events if event.event_type == "task.launch-claimed"]) == 1
    assert len([event for event in events if event.event_type == "run.dispatch.requested"]) == 1


@POSIX_ONLY
def test_hold_resolve_assertion_is_operator_correlation(tmp_path):
    workspace, run_id = _enroll(tmp_path)
    _claim(workspace, run_id, "one")
    report = dispatch.hold_resolve(workspace=workspace, run_id=run_id, task_id="one", payload=_native(), abandon=False)
    assert report["mode"] == dispatch.OPERATOR_ASSERTED
    assert report["child_run_id"] == "run-child"
    with pytest.raises(dispatch.DispatchError):
        dispatch.hold_resolve(
            workspace=workspace,
            run_id=run_id,
            task_id="one",
            payload=_native(childRunId="run-child-other"),
            abandon=False,
        )
    journal_text = (_run_dir(workspace, run_id) / "events" / "lifecycle.jsonl").read_text(encoding="utf-8")
    assert TASK_TEXT not in journal_text
