"""Governed native T3 claims, reported correlation and holds: fail closed."""

from __future__ import annotations

import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from brigade import (
    aboyeur,
    cli,
    native_t3_admission as admission,
    native_t3_dispatch as dispatch,
    native_t3_enrollment as enrollment,
    proc,
    run_events,
    run_journal,
    run_lifecycle,
    run_tasks,
)
from brigade.native_t3_contract import NativeIntentTemplate, NativeTarget

TASK_TEXT = "retained-input-marker"
THREAD = "thread-parent"
# Field names pinned from the T3 Code delegate_task input schema at
# packages/contracts/src/orchestratorMcp.ts in revision c52df24d28. The printed
# claim arguments must stay a subset of these reviewed names.
REVIEWED_DELEGATE_TASK_FIELDS = frozenset(
    {
        "task",
        "target",
        "title",
        "role",
        "isolation",
        "mode",
        "timeoutMs",
        "clientRequestId",
        "runtimeMode",
        "interactionMode",
    }
)
REVIEWED_TARGET_FIELDS = frozenset(
    {"providerInstanceId", "driverKind", "model", "options", "environmentId", "projectId"}
)
POSIX_ONLY = pytest.mark.skipif(
    os.name == "nt", reason="native T3 custody refuses Windows for lack of owner-only DACLs"
)


def _git(repo: Path, *args: str) -> None:
    result = proc.run(["git", *args], cwd=repo)
    assert result.code == 0, result.stderr


def _workspace(
    tmp_path: Path,
    *,
    targets: tuple[str, ...] = ("coder",),
    target_environment: str = "env-target",
    target_extra: tuple[str, ...] = (),
) -> Path:
    repo = tmp_path / "workspace"
    repo.mkdir(parents=True)
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test User")
    (repo / "tracked.txt").write_text("base\n")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "initial")
    lines = [
        'orchestrator = "lead"',
        "",
        "[agents.lead]",
        'cli = "codex"',
        'role = "orchestrator"',
        "",
        "[agents.coder]",
        'cli = "codex"',
        'role = "coder"',
        "",
    ]
    for name in targets:
        lines += [
            f"[t3_targets.{name}]",
            'origin_environment = "env-target"',
            'origin_thread = "thread-parent"',
            f'target_environment = "{target_environment}"',
            'target_project = "project-test"',
            'provider_instance = "codex-test"',
            'driver = "codex"',
            'model = "launch-model"',
            'isolation = "worktree"',
            *target_extra,
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


def _enroll(tmp_path: Path, ids: tuple[str, ...] = ("one", "two"), **workspace_kwargs: object) -> tuple[Path, str]:
    workspace = _workspace(tmp_path, **workspace_kwargs)  # type: ignore[arg-type]
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


def _terminal(task_id: str = "one", *, status: str = "completed", **changes: object) -> dict:
    payload = _native(
        task_id,
        status=status,
        workState="result_available",
        latestTerminalStatus=status,
        latestTerminalRunId="run-child",
        latestTerminalResultContextTransferId="transfer-1",
        resultContextTransferId="transfer-1",
    )
    payload.update(changes)
    return payload


def _claim(workspace: Path, run_id: str, task_id: str = "one", *, parent: str = THREAD) -> dict:
    return dispatch.claim(workspace=workspace, run_id=run_id, task_id=task_id, parent_thread=parent)


def _unchecked_target(**changes: object) -> NativeTarget:
    """Build a target as an older Brigade release would have retained it."""
    target = NativeTarget(
        origin_environment="env-origin",
        origin_thread="thread-parent",
        target_environment="env-target",
        target_project="project-test",
        provider_instance="codex-test",
        driver="codex",
        launch_model="launch-model",
        options=(("reasoningEffort", "high"),),
        runtime_mode="inherit",
        interaction_mode="inherit",
        isolation="worktree",
        source_revision="a" * 40,
    )
    legacy = object.__new__(NativeTarget)
    legacy.__dict__.update({**target.__dict__, **changes})
    return legacy


def _legacy_claim_run(
    workspace: Path, *, target_changes: dict[str, object] | None = None, seed: bytes = b"legacy-request"
) -> str:
    """Create a run whose retained asset predates the current target rules."""
    text = f"{TASK_TEXT} one"
    head = enrollment._clean_revision(workspace)
    run_id = "t3-" + hashlib.sha256(seed).hexdigest()
    run_dir = _run_dir(workspace, run_id)
    run_dir.mkdir(parents=True, mode=0o700)
    spec = run_tasks.TaskSpec("one", "coder", hashlib.sha256(text.encode("utf-8")).hexdigest())
    changes = {"runtime_mode": "app-server", "source_revision": head, **(target_changes or {})}
    target = _unchecked_target(**changes)
    content = enrollment.delegation_input_bytes(task_id="one", seat="coder", text=text, target=target)
    name = enrollment.asset_name("one")
    template = NativeIntentTemplate(
        run_id=run_id,
        task_id="one",
        seat="coder",
        target=target,
        payload_digest=spec.payload_digest,
        source_revision=head,
    )
    with enrollment.owned_run(workspace, run_dir):
        aboyeur._write_json(
            run_dir / "run.json",
            {
                "schema": "brigade.run.v1",
                "kind": enrollment.RUN_KIND,
                "status": enrollment.AWAITING_NATIVE_PARENT,
                "task": "Legacy native T3 run",
                "started_at": "2026-01-01T00:00:00Z",
                "lock_workspace": str(workspace),
                "lifecycle_journal_requested": True,
            },
        )
        run_lifecycle.prepare_lifecycle_journal(run_dir, workspace=workspace)
        run_tasks.accept_task_batch(run_dir, workspace=workspace, request_id="request-legacy", specs=(spec,))
        enrollment.write_delegation_asset(run_dir, name, content)
        run_tasks.record_native_enrollment(
            run_dir,
            workspace=workspace,
            task_id="one",
            enrollment=run_tasks.TaskEnrollment(
                descriptor_version=enrollment.journal.NATIVE_ENROLLMENT_DESCRIPTOR_VERSION,
                asset_path=name,
                asset_digest=hashlib.sha256(content).hexdigest(),
                template_digest=template.template_digest,
            ),
        )
    return run_id


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
            "driverKind": "codex",
            "model": "launch-model",
            "projectId": "project-test",
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
def test_inherit_defaults_omit_mode_fields_from_claim_output(tmp_path):
    workspace, run_id = _enroll(tmp_path)
    report = _claim(workspace, run_id)
    arguments = report["delegate_task"]
    assert arguments["target"]["projectId"] == "project-test"
    assert "environmentId" not in arguments["target"]
    assert "runtimeMode" not in arguments and "interactionMode" not in arguments


@POSIX_ONLY
def test_explicit_native_modes_appear_in_claim_output(tmp_path):
    workspace, run_id = _enroll(
        tmp_path,
        target_extra=('runtime_mode = "auto"', 'interaction_mode = "plan"'),
    )
    arguments = _claim(workspace, run_id)["delegate_task"]
    assert arguments["runtimeMode"] == "auto"
    assert arguments["interactionMode"] == "plan"


@POSIX_ONLY
def test_claim_arguments_are_a_subset_of_the_reviewed_delegate_schema(tmp_path):
    workspace, run_id = _enroll(tmp_path)
    arguments = _claim(workspace, run_id)["delegate_task"]
    assert set(arguments) <= REVIEWED_DELEGATE_TASK_FIELDS
    assert set(arguments["target"]) <= REVIEWED_TARGET_FIELDS
    assert "environmentId" not in arguments["target"]


@pytest.mark.parametrize(
    ("target_environment", "target_extra", "code"),
    [
        ("env-target", ('runtime_mode = "app-server"',), "target-mode-invalid"),
        ("env-remote", (), "target-remote-unsupported"),
    ],
)
@POSIX_ONLY
def test_open_refuses_modes_t3_rejects_and_remote_targets(tmp_path, capsys, target_environment, target_extra, code):
    workspace = _workspace(tmp_path, target_environment=target_environment, target_extra=target_extra)
    batch = _batch(tmp_path)
    with pytest.raises(enrollment.EnrollmentError) as refused:
        enrollment.enroll(batch_path=batch, target=workspace)
    assert refused.value.code == code
    capsys.readouterr()
    assert cli.main(["t3", "open", str(batch), "--target", str(workspace)]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and f"[{code}]" in captured.err and TASK_TEXT not in captured.err


@POSIX_ONLY
def test_admission_predicate_refuses_legacy_modes_and_remote_targets(tmp_path):
    workspace = _workspace(tmp_path)
    roster = admission.load_roster(workspace)
    hub = admission.HubAuthority(state="unenrolled")
    for target, reason in (
        (_unchecked_target(runtime_mode="app-server"), admission.TARGET_MODE_INVALID),
        (_unchecked_target(target_environment="env-remote"), admission.TARGET_REMOTE_UNSUPPORTED),
    ):
        verdict = admission.evaluate(roster, "coder", target, hub=hub)
        assert verdict.code == "admission-denied" and verdict.reason == reason


@POSIX_ONLY
def test_legacy_retained_app_server_is_refused_by_claim(tmp_path, capsys):
    workspace = _workspace(tmp_path)
    run_id = _legacy_claim_run(workspace)
    with pytest.raises(dispatch.DispatchError) as refused:
        _claim(workspace, run_id)
    assert refused.value.code == "target-mode-invalid"
    assert not [event for event in _events(workspace, run_id) if event.event_type == dispatch.CLAIM_EVENT]
    capsys.readouterr()
    assert cli.main(["t3", "claim", run_id, "one", "--parent-thread", THREAD, "--target", str(workspace)]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "[target-mode-invalid]" in captured.err and TASK_TEXT not in captured.err


@POSIX_ONLY
def test_legacy_retained_remote_target_is_refused_by_claim(tmp_path, capsys):
    workspace = _workspace(tmp_path)
    run_id = _legacy_claim_run(
        workspace,
        target_changes={"target_environment": "env-remote", "runtime_mode": "inherit"},
        seed=b"legacy-remote",
    )
    with pytest.raises(dispatch.DispatchError) as refused:
        _claim(workspace, run_id)
    assert refused.value.code == "admission-denied"
    assert admission.TARGET_REMOTE_UNSUPPORTED in str(refused.value)
    capsys.readouterr()
    assert cli.main(["t3", "claim", run_id, "one", "--parent-thread", THREAD, "--target", str(workspace)]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "[admission-denied]" in captured.err
    assert admission.TARGET_REMOTE_UNSUPPORTED in captured.err and TASK_TEXT not in captured.err


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


@pytest.mark.parametrize("transfers", ["both", "latest-only", "result-only"])
@POSIX_ONLY
def test_accepted_records_a_terminal_projection(tmp_path, transfers):
    workspace, run_id = _enroll(tmp_path)
    _claim(workspace, run_id, "one")
    payload = _terminal()
    if transfers == "latest-only":
        payload["resultContextTransferId"] = None
    elif transfers == "result-only":
        payload["latestTerminalResultContextTransferId"] = None
    report = dispatch.accepted(
        workspace=workspace,
        run_id=run_id,
        task_id="one",
        parent_thread=THREAD,
        payload=payload,
    )
    assert report["status"] == "completed"
    task = next(item for item in run_tasks.read_tasks(_run_dir(workspace, run_id)) if item.handle.spec.task_id == "one")
    assert task.status == "completed" and task.result_id is not None
    assert task.artifact_kind == "receipt" and task.artifact_digest is not None


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


# Fix round for #2702: bounded keys, lost-request closure in both modes, the
# pre-launch barrier, final holds, closed event shapes, missing targets and
# bounded native reads.


@POSIX_ONLY
def test_long_task_id_keeps_fixed_length_idempotency_keys(tmp_path):
    task_id = "a" * 128
    workspace, run_id = _enroll(tmp_path, (task_id,))
    report = _claim(workspace, run_id, task_id)
    accepted = dispatch.accepted(
        workspace=workspace, run_id=run_id, task_id=task_id, parent_thread=THREAD, payload=_native(task_id)
    )
    assert accepted["mode"] == dispatch.PARENT_REPORTED
    abandoned = dispatch.hold_resolve(workspace=workspace, run_id=run_id, task_id=task_id, payload=None, abandon=True)
    assert abandoned["mode"] == dispatch.ABANDONED
    assert report["clientRequestId"]
    events = _events(workspace, run_id)
    native = [
        event
        for event in events
        if event.event_type in {dispatch.CLAIM_EVENT, dispatch.CORRELATION_EVENT, dispatch.HELD_EVENT}
    ]
    assert len(native) == 3
    assert all(len(event.idempotency_key) <= run_events.MAX_IDEMPOTENCY_KEY_LEN for event in native)


@POSIX_ONLY
def test_native_json_closes_a_lost_dispatch_request(tmp_path, monkeypatch):
    workspace, run_id = _enroll(tmp_path)
    original = dispatch._record_dispatch_requested

    def boom(*_args: object, **_kwargs: object) -> int:
        raise dispatch.DispatchError("journal-refused", "injected crash before the requested fact")

    monkeypatch.setattr(dispatch, "_record_dispatch_requested", boom)
    with pytest.raises(dispatch.DispatchError):
        _claim(workspace, run_id, "one")
    monkeypatch.setattr(dispatch, "_record_dispatch_requested", original)
    assert not [event for event in _events(workspace, run_id) if event.event_type == "run.dispatch.requested"]
    with pytest.raises(dispatch.DispatchError) as blocked:
        _claim(workspace, run_id, "two")
    assert blocked.value.code == "run-barrier"

    report = dispatch.hold_resolve(workspace=workspace, run_id=run_id, task_id="one", payload=_native(), abandon=False)
    assert report["mode"] == dispatch.OPERATOR_ASSERTED
    requested = [
        event.payload["attempt"] for event in _events(workspace, run_id) if event.event_type == "run.dispatch.requested"
    ]
    assert requested == [1]
    assert _claim(workspace, run_id, "two")["dispatch_attempt"] == 2


@POSIX_ONLY
def test_crash_before_the_launch_claim_blocks_and_abandon_resolves(tmp_path, monkeypatch):
    workspace, run_id = _enroll(tmp_path)
    original = dispatch.run_tasks.claim_task_launch

    def crash(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("injected crash before the launch claim")

    monkeypatch.setattr(dispatch.run_tasks, "claim_task_launch", crash)
    with pytest.raises(RuntimeError):
        _claim(workspace, run_id, "one")
    monkeypatch.setattr(dispatch.run_tasks, "claim_task_launch", original)
    events = _events(workspace, run_id)
    assert len([event for event in events if event.event_type == dispatch.CLAIM_EVENT]) == 1
    assert not [event for event in events if event.event_type == "task.launch-claimed"]
    assert not [event for event in events if event.event_type == "run.dispatch.requested"]

    with pytest.raises(dispatch.DispatchError) as blocked:
        _claim(workspace, run_id, "two")
    assert blocked.value.code == "run-barrier"
    with pytest.raises(dispatch.DispatchError) as prelaunch:
        dispatch.hold_resolve(workspace=workspace, run_id=run_id, task_id="one", payload=_native(), abandon=False)
    assert prelaunch.value.code == "no-claim"

    report = dispatch.hold_resolve(workspace=workspace, run_id=run_id, task_id="one", payload=None, abandon=True)
    assert report["mode"] == dispatch.ABANDONED and report["status"] == "queued"
    assert not [event for event in _events(workspace, run_id) if event.event_type == "run.dispatch.requested"]
    with pytest.raises(dispatch.DispatchError) as held:
        _claim(workspace, run_id, "one")
    assert held.value.code == "task-held"
    assert _claim(workspace, run_id, "two")["dispatch_attempt"] == 1


@POSIX_ONLY
def test_native_json_refuses_an_abandoned_task(tmp_path):
    workspace, run_id = _enroll(tmp_path)
    _claim(workspace, run_id, "one")
    dispatch.hold_resolve(workspace=workspace, run_id=run_id, task_id="one", payload=None, abandon=True)
    journal_path = _run_dir(workspace, run_id) / "events" / "lifecycle.jsonl"
    before = journal_path.read_bytes()
    with pytest.raises(dispatch.DispatchError) as refused:
        dispatch.hold_resolve(workspace=workspace, run_id=run_id, task_id="one", payload=_native(), abandon=False)
    assert refused.value.code == "task-held"
    assert journal_path.read_bytes() == before


@pytest.mark.parametrize(
    ("event_type", "payload"),
    [
        (
            "task.native-claimed",
            {
                "task_id": "one",
                "attempt_id": "a" * 64,
                "dispatch_attempt": 1,
                "template_digest": "b" * 64,
                "intent_digest": "c" * 64,
            },
        ),
        (
            "task.native-claimed",
            {
                "task_id": "one",
                "attempt_id": "a" * 64,
                "dispatch_attempt": True,
                "parent_thread": "thread-parent",
                "template_digest": "b" * 64,
                "intent_digest": "c" * 64,
            },
        ),
        (
            "task.native-claimed",
            {
                "task_id": "one",
                "attempt_id": "a" * 64,
                "dispatch_attempt": 1,
                "parent_thread": "thread-parent",
                "template_digest": "not-a-digest",
                "intent_digest": "c" * 64,
            },
        ),
        (
            "task.native-correlated",
            {
                "task_id": "one",
                "attempt_id": "a" * 64,
                "mode": "authenticated",
                "handle_digest": "b" * 64,
                "child_thread_id": "thread-child",
                "child_run_id": "run-child",
            },
        ),
        (
            "task.native-correlated",
            {
                "task_id": "one",
                "attempt_id": "a" * 64,
                "mode": "parent-reported",
                "handle_digest": "b" * 64,
                "child_thread_id": None,
                "child_run_id": "run-child",
            },
        ),
        (
            "task.native-held",
            {"task_id": "one", "attempt_id": "a" * 64, "reason": "lost", "evidence_digest": "b" * 64},
        ),
        (
            "task.native-held",
            {
                "task_id": "one",
                "attempt_id": "a" * 64,
                "reason": "abandoned-unknown",
                "evidence_digest": "b" * 64,
                "task_text": "leak",
            },
        ),
    ],
)
def test_native_event_payloads_are_closed(event_type, payload):
    with pytest.raises(run_events.CanonicalizationError):
        run_events.validate_event_request(event_type=event_type, payload=payload, idempotency_key="k")
    valid = {
        "task.native-claimed": {
            "task_id": "one",
            "attempt_id": "a" * 64,
            "dispatch_attempt": 1,
            "parent_thread": "thread-parent",
            "template_digest": "b" * 64,
            "intent_digest": "c" * 64,
        },
        "task.native-correlated": {
            "task_id": "one",
            "attempt_id": "a" * 64,
            "mode": "parent-reported",
            "handle_digest": "b" * 64,
            "child_thread_id": "thread-child",
            "child_run_id": "run-child",
        },
        "task.native-held": {
            "task_id": "one",
            "attempt_id": "a" * 64,
            "reason": "abandoned-unknown",
            "evidence_digest": "b" * 64,
        },
    }[event_type]
    run_events.validate_event_request(event_type=event_type, payload=valid, idempotency_key="k")


@POSIX_ONLY
def test_missing_target_is_a_bounded_run_missing_refusal(tmp_path, capsys):
    missing = tmp_path / "does-not-exist"
    command = [
        "t3",
        "claim",
        "t3-" + "0" * 64,
        "one",
        "--parent-thread",
        THREAD,
        "--target",
        str(missing),
    ]
    assert cli.main(command) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "[run-missing]" in captured.err and TASK_TEXT not in captured.err
    assert not missing.exists()


@POSIX_ONLY
def test_native_json_refuses_non_regular_and_oversized_files(tmp_path, capsys):
    workspace, run_id = _enroll(tmp_path)
    _claim(workspace, run_id, "one")
    capsys.readouterr()

    def argv(path: str) -> list[str]:
        return [
            "t3",
            "accepted",
            run_id,
            "one",
            "--parent-thread",
            THREAD,
            "--native-json",
            path,
            "--target",
            str(workspace),
        ]

    assert cli.main(argv("/dev/zero")) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "[native-unreadable]" in captured.err
    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b" " * (dispatch._MAX_NATIVE_BYTES + 1))
    assert cli.main(argv(str(oversized))) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "[native-too-large]" in captured.err


TERMINAL_STATUSES = ("completed", "failed", "cancelled", "interrupted")
BRIGADE_TERMINAL = {"completed", "failed", "cancelled", "interrupted"}


@pytest.mark.parametrize("status", TERMINAL_STATUSES)
@pytest.mark.parametrize("pending", ["children", "question", "approval", "timeout"])
@POSIX_ONLY
def test_pending_evidence_never_records_a_terminal_result(tmp_path, pending, status):
    workspace, run_id = _enroll(tmp_path, ("one",))
    _claim(workspace, run_id, "one")
    changes = {
        "children": {"hasPendingChildRuns": True},
        "question": {"hasPendingQuestion": True},
        "approval": {
            "pendingApproval": {"requestId": "request-1", "kind": "command", "runId": "run-1"},
        },
        "timeout": {"waitTimedOut": True},
    }[pending]
    report = dispatch.accepted(
        workspace=workspace,
        run_id=run_id,
        task_id="one",
        parent_thread=THREAD,
        payload=_terminal(status=status, **changes),
    )
    assert report["status"] not in BRIGADE_TERMINAL
    task = next(item for item in run_tasks.read_tasks(_run_dir(workspace, run_id)) if item.handle.spec.task_id == "one")
    assert task.status not in BRIGADE_TERMINAL and task.result_id is None

    # The normal path: a later clean projection records the terminal result.
    clean = dispatch.accepted(
        workspace=workspace, run_id=run_id, task_id="one", parent_thread=THREAD, payload=_terminal(status=status)
    )
    assert clean["status"] == status
    task = next(item for item in run_tasks.read_tasks(_run_dir(workspace, run_id)) if item.handle.spec.task_id == "one")
    assert task.status == status and task.result_id is not None


@POSIX_ONLY
def test_failed_with_active_children_stays_recoverable(tmp_path):
    workspace, run_id = _enroll(tmp_path, ("one",))
    _claim(workspace, run_id, "one")
    report = dispatch.accepted(
        workspace=workspace,
        run_id=run_id,
        task_id="one",
        parent_thread=THREAD,
        payload=_terminal(status="failed", hasPendingChildRuns=True),
    )
    assert report["status"] == "waiting-for-children"
    task = next(item for item in run_tasks.read_tasks(_run_dir(workspace, run_id)) if item.handle.spec.task_id == "one")
    assert task.status == "waiting-for-children" and task.result_id is None
    corrected = dispatch.accepted(
        workspace=workspace, run_id=run_id, task_id="one", parent_thread=THREAD, payload=_terminal(status="failed")
    )
    assert corrected["status"] == "failed"
    task = next(item for item in run_tasks.read_tasks(_run_dir(workspace, run_id)) if item.handle.spec.task_id == "one")
    assert task.status == "failed" and task.result_id is not None


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("pendingApproval", {"requestId": 1, "kind": "command", "runId": "run-1"}, "native-invalid"),
        ("pendingApproval", {"requestId": "request-1", "kind": "unknown", "runId": "run-1"}, "native-invalid"),
        ("pendingApproval", {"kind": "command", "runId": "run-1"}, "native-invalid"),
        ("hasPendingQuestion", "false", "native-invalid"),
        ("waitTimedOut", 0, "native-invalid"),
        ("latestTerminalRunId", 5, "native-invalid"),
        ("latestTerminalResultContextTransferId", 5, "native-invalid"),
        ("resultContextTransferId", {}, "native-invalid"),
        ("summary", 5, "native-invalid"),
        ("latestTerminalSummary", 5, "native-invalid"),
        ("remoteChild", {"environmentId": "env", "threadId": "thread", "label": "x"}, "native-mismatch"),
        ("remoteChild", {"environmentId": "env", "threadId": "thread"}, "native-invalid"),
    ],
)
@POSIX_ONLY
def test_malformed_projection_refuses(tmp_path, field, value, code):
    workspace, run_id = _enroll(tmp_path, ("one",))
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
def test_native_json_refuses_pipes_and_symlinks(tmp_path, capsys):
    workspace, run_id = _enroll(tmp_path, ("one",))
    _claim(workspace, run_id, "one")
    capsys.readouterr()

    def argv(path: str) -> list[str]:
        return [
            "t3",
            "accepted",
            run_id,
            "one",
            "--parent-thread",
            THREAD,
            "--native-json",
            path,
            "--target",
            str(workspace),
        ]

    fifo = tmp_path / "native.fifo"
    try:
        os.mkfifo(fifo)
    except (AttributeError, OSError, NotImplementedError):
        pytest.skip("named pipes are unavailable on this platform")
    assert cli.main(argv(str(fifo))) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "[native-unreadable]" in captured.err

    real = tmp_path / "native.json"
    real.write_text(json.dumps(_native()), encoding="utf-8")
    link = tmp_path / "native-link.json"
    try:
        os.symlink(real, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable on this platform")
    assert cli.main(argv(str(link))) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "[native-unreadable]" in captured.err
