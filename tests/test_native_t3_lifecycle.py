"""Governed native T3 lifecycle: observe, cancel, ack, finalize and status."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from brigade import (
    cli,
    native_t3_dispatch as dispatch,
    native_t3_lifecycle as lifecycle,
    native_t3_observations as observations,
    run_events,
    run_journal,
    run_projector,
    run_tasks,
)
from tests.test_native_t3_claim import (
    POSIX_ONLY,
    TASK_TEXT,
    THREAD,
    _claim,
    _enroll,
    _events,
    _native,
    _reply,
    _request_key,
    _run_dir,
    _terminal,
    _terminal_reply,
    t3_task_id,
)

SKILL = Path(__file__).resolve().parents[1] / "src/brigade/templates/skills/t3-governed-delegation/SKILL.md"


def _journal(workspace: Path, run_id: str) -> Path:
    return _run_dir(workspace, run_id) / "events" / "lifecycle.jsonl"


def _observe(workspace: Path, run_id: str, task_id: str = "one", *, parent: str = THREAD, **changes: object) -> dict:
    return lifecycle.observe(
        workspace=workspace,
        run_id=run_id,
        task_id=task_id,
        parent_thread=parent,
        payload=_terminal_reply(workspace, run_id, task_id, **changes),
    )


def _accepted(workspace: Path, run_id: str, task_id: str = "one", payload: dict | None = None) -> dict:
    return dispatch.accepted(
        workspace=workspace,
        run_id=run_id,
        task_id=task_id,
        parent_thread=THREAD,
        payload=payload if payload is not None else _reply(workspace, run_id, task_id),
    )


def _complete(workspace: Path, run_id: str, task_id: str, *, status: str = "completed") -> str:
    _claim(workspace, run_id, task_id)
    report = _accepted(workspace, run_id, task_id, _terminal_reply(workspace, run_id, task_id, status=status))
    assert report["result_id"] is not None
    return report["result_id"]


@POSIX_ONLY
def test_observe_requires_a_recorded_correlation(tmp_path):
    workspace, run_id = _enroll(tmp_path, ("one",))
    _claim(workspace, run_id, "one")
    before = _journal(workspace, run_id).read_bytes()
    with pytest.raises(dispatch.DispatchError) as refused:
        _observe(workspace, run_id)
    assert refused.value.code == "no-correlation"
    assert _journal(workspace, run_id).read_bytes() == before
    assert not [event for event in _events(workspace, run_id) if event.event_type == dispatch.CORRELATION_EVENT]


@pytest.mark.parametrize(
    ("changes", "parent", "code"),
    [
        ({"childRunId": "run-other"}, THREAD, "handle-mismatch"),
        ({"childThreadId": "thread-other"}, THREAD, "handle-mismatch"),
        ({"providerInstanceId": "other-provider"}, THREAD, "provider-mismatch"),
        ({"model": "other-model"}, THREAD, "model-mismatch"),
        ({"options": []}, THREAD, "options-mismatch"),
        ({}, "thread-other", "parent-mismatch"),
    ],
)
@POSIX_ONLY
def test_observe_refuses_a_mismatched_handle(tmp_path, changes, parent, code):
    workspace, run_id = _enroll(tmp_path, ("one",))
    _claim(workspace, run_id, "one")
    _accepted(workspace, run_id)
    before = _journal(workspace, run_id).read_bytes()
    with pytest.raises(dispatch.DispatchError) as refused:
        _observe(workspace, run_id, parent=parent, **changes)
    assert refused.value.code == code
    assert _journal(workspace, run_id).read_bytes() == before


@POSIX_ONLY
def test_observe_refuses_a_sibling_tasks_request_key(tmp_path):
    workspace, run_id = _enroll(tmp_path, ("one", "two"))
    _claim(workspace, run_id, "one")
    sibling = _claim(workspace, run_id, "two")
    _accepted(workspace, run_id)
    before = _journal(workspace, run_id).read_bytes()
    with pytest.raises(dispatch.DispatchError) as refused:
        lifecycle.observe(
            workspace=workspace,
            run_id=run_id,
            task_id="one",
            parent_thread=THREAD,
            payload=_native(request_key=sibling["clientRequestId"]),
        )
    assert refused.value.code == "native-mismatch"
    assert _journal(workspace, run_id).read_bytes() == before


@POSIX_ONLY
def test_observe_records_a_nonterminal_then_terminal_projection(tmp_path):
    workspace, run_id = _enroll(tmp_path, ("one",))
    _claim(workspace, run_id, "one")
    _accepted(workspace, run_id)

    active = lifecycle.observe(
        workspace=workspace,
        run_id=run_id,
        task_id="one",
        parent_thread=THREAD,
        payload=_reply(workspace, run_id, hasPendingChildRuns=True),
    )
    assert active["family"] == "awaiting" and active["result_id"] is None
    assert active["replayed"] is False

    terminal = _observe(workspace, run_id)
    assert terminal["family"] == "terminal-candidate"
    assert terminal["status"] == "completed" and terminal["result_id"] is not None
    assert terminal["replayed"] is False

    before = _journal(workspace, run_id).read_bytes()
    replay = _observe(workspace, run_id)
    assert replay["replayed"] is True and replay["result_id"] == terminal["result_id"]
    assert _journal(workspace, run_id).read_bytes() == before

    task = next(item for item in run_tasks.read_tasks(_run_dir(workspace, run_id)) if item.handle.spec.task_id == "one")
    assert task.status == "completed" and task.artifact_kind == "receipt"
    assert task.artifact_digest is not None and task.result_id == terminal["result_id"]

    with pytest.raises(dispatch.DispatchError) as conflict:
        _observe(workspace, run_id, latestTerminalRunId="run-other")
    assert conflict.value.code == "observation-conflict"


def test_terminal_evidence_changes_the_result_identity():
    reference = observations.NativeTaskReference("env-origin", "request-key", "one")
    request_key = "brigade-t3:" + "a" * 64
    node_id = t3_task_id(request_key)
    first = dispatch.parse_native_result(_terminal(request_key=request_key), task_id="one", request_key=request_key)
    second = dispatch.parse_native_result(
        _terminal(request_key=request_key)
        | {"latestTerminalResultContextTransferId": "transfer-2", "resultContextTransferId": "transfer-2"},
        task_id="one",
        request_key=request_key,
    )
    assert first.provider_task_id == node_id and second.provider_task_id == node_id
    one = observations.NativeObservation(
        "terminal-candidate",
        "terminal-evidence-complete",
        reference,
        "completed",
        "run-child",
        "transfer-1",
        "quiescent",
    )
    two = observations.NativeObservation(
        "terminal-candidate",
        "terminal-evidence-complete",
        reference,
        "completed",
        "run-child",
        "transfer-2",
        "quiescent",
    )
    digest_one = dispatch._evidence_digest("a" * 64, first, one)
    digest_two = dispatch._evidence_digest("a" * 64, second, two)
    assert digest_one != digest_two

    handle = run_tasks.TaskHandle("run-one", "request-one", run_tasks.TaskSpec("one", "coder", "b" * 64), "c" * 64)
    base = {"handle": handle, "status": "completed", "provider_task_id": "one", "original_run_id": "run-child"}
    assert run_tasks._result_id(run_tasks.TaskResult(**base, artifact_kind="receipt", artifact_digest=digest_one)) != (
        run_tasks._result_id(run_tasks.TaskResult(**base, artifact_kind="receipt", artifact_digest=digest_two))
    )


@POSIX_ONLY
def test_legacy_correlation_without_provider_task_id_stays_usable(tmp_path, monkeypatch):
    """A journal written before provider_task_id existed still reads, observes and cancels."""
    workspace, run_id = _enroll(tmp_path, ("one", "two"))
    _claim(workspace, run_id, "one")
    sibling = _claim(workspace, run_id, "two")
    real_append = dispatch._append

    def legacy_append(workspace_arg, run_dir, event_type, payload, *, key):
        if event_type == dispatch.CORRELATION_EVENT:
            payload = {name: value for name, value in payload.items() if name != "provider_task_id"}
        return real_append(workspace_arg, run_dir, event_type, payload, key=key)

    monkeypatch.setattr(dispatch, "_append", legacy_append)
    _accepted(workspace, run_id)
    monkeypatch.setattr(dispatch, "_append", real_append)
    correlated = [event for event in _events(workspace, run_id) if event.event_type == dispatch.CORRELATION_EVENT]
    assert correlated and "provider_task_id" not in correlated[0].payload

    status = lifecycle.status(workspace=workspace, run_id=run_id)
    assert [task["correlated"] for task in status["tasks"] if task["task_id"] == "one"] == [True]
    cancel = lifecycle.cancel(workspace=workspace, run_id=run_id, task_id="one", parent_thread=THREAD)
    assert cancel["task_cancel"]["taskId"] == "one"

    before = _journal(workspace, run_id).read_bytes()
    with pytest.raises(dispatch.DispatchError) as refused:
        lifecycle.observe(
            workspace=workspace,
            run_id=run_id,
            task_id="one",
            parent_thread=THREAD,
            payload=_native(request_key=sibling["clientRequestId"]),
        )
    assert refused.value.code == "native-mismatch"
    assert _journal(workspace, run_id).read_bytes() == before
    assert _observe(workspace, run_id)["result_id"] is not None


@pytest.mark.parametrize("node", ["node:delegated-task:\ud800", "node:delegated-task:command%3Amcp%3A\udfff"])
def test_lone_surrogate_task_node_refuses_without_raising(node):
    assert dispatch._is_t3_task_node(node, "brigade-t3:" + "a" * 64) is False


def test_overlong_task_node_refuses_before_decoding():
    """A well-formed node id longer than the journal's string bound refuses."""
    from urllib.parse import quote

    safe = "-_.!~*'()"
    key = "brigade-t3:" + "a" * 64
    namespace = "a" + ":" * 200
    command = "command:mcp:" + quote(namespace, safe=safe) + ":delegate-task:" + quote(key, safe=safe)
    node = "node:delegated-task:" + quote(command, safe=safe)
    short = "node:delegated-task:" + quote("command:mcp:a:delegate-task:" + quote(key, safe=safe), safe=safe)
    assert len(node) > run_events.MAX_PAYLOAD_STR_LEN
    assert dispatch._is_t3_task_node(short, key) is True
    assert dispatch._is_t3_task_node(node, key) is False


@POSIX_ONLY
def test_cancel_replays_identically_and_refuses_once_terminal(tmp_path, capsys):
    workspace, run_id = _enroll(tmp_path, ("one",))
    _claim(workspace, run_id, "one")
    _accepted(workspace, run_id)
    first = lifecycle.cancel(workspace=workspace, run_id=run_id, task_id="one", parent_thread=THREAD)
    expected = lifecycle.cancel_key(run_id, "one", first["attempt_id"], first["handle_digest"])
    assert first["task_cancel"] == {"taskId": t3_task_id(_request_key(workspace, run_id)), "clientRequestId": expected}
    assert first["replayed"] is False
    assert len([event for event in _events(workspace, run_id) if event.event_type == lifecycle.CANCEL_EVENT]) == 1

    before = _journal(workspace, run_id).read_bytes()
    replay = lifecycle.cancel(workspace=workspace, run_id=run_id, task_id="one", parent_thread=THREAD)
    assert replay["task_cancel"] == first["task_cancel"] and replay["replayed"] is True
    assert _journal(workspace, run_id).read_bytes() == before

    _observe(workspace, run_id)
    capsys.readouterr()
    argv = ["t3", "cancel", run_id, "one", "--parent-thread", THREAD, "--target", str(workspace)]
    assert cli.main(argv) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "[task-terminal]" in captured.err


@POSIX_ONLY
def test_cancel_refuses_a_held_task_with_empty_stdout(tmp_path, capsys):
    workspace, run_id = _enroll(tmp_path, ("one",))
    _claim(workspace, run_id, "one")
    _accepted(workspace, run_id)
    dispatch.hold_resolve(workspace=workspace, run_id=run_id, task_id="one", payload=None, abandon=True)
    capsys.readouterr()
    argv = ["t3", "cancel", run_id, "one", "--parent-thread", THREAD, "--target", str(workspace)]
    assert cli.main(argv) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "[task-held]" in captured.err


@POSIX_ONLY
def test_ack_requires_an_exact_terminal_result_and_is_idempotent(tmp_path, capsys):
    workspace, run_id = _enroll(tmp_path, ("one", "two"))
    _claim(workspace, run_id, "one")
    with pytest.raises(dispatch.DispatchError) as nonterminal:
        lifecycle.ack(workspace=workspace, run_id=run_id, task_id="one", result_id="a" * 64)
    assert nonterminal.value.code == "task-nonterminal"

    report = _accepted(workspace, run_id, "one", _terminal_reply(workspace, run_id))
    result_id = report["result_id"]
    assert result_id is not None
    with pytest.raises(dispatch.DispatchError) as wrong:
        lifecycle.ack(workspace=workspace, run_id=run_id, task_id="one", result_id="b" * 64)
    assert wrong.value.code == "result-mismatch"
    first = lifecycle.ack(workspace=workspace, run_id=run_id, task_id="one", result_id=result_id)
    assert first["acknowledged"] is True and first["replayed"] is False
    replay = lifecycle.ack(workspace=workspace, run_id=run_id, task_id="one", result_id=result_id)
    assert replay["acknowledged"] is True and replay["replayed"] is True

    _claim(workspace, run_id, "two")
    _accepted(workspace, run_id, "two")
    dispatch.hold_resolve(workspace=workspace, run_id=run_id, task_id="two", payload=None, abandon=True)
    capsys.readouterr()
    argv = ["t3", "ack", run_id, "two", "--result-id", "c" * 64, "--target", str(workspace)]
    assert cli.main(argv) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "[task-held]" in captured.err


@POSIX_ONLY
def test_finalize_completed_when_every_task_completed_and_acked(tmp_path):
    workspace, run_id = _enroll(tmp_path, ("one", "two"))
    results = {}
    for task_id in ("one", "two"):
        results[task_id] = _complete(workspace, run_id, task_id)
        lifecycle.ack(workspace=workspace, run_id=run_id, task_id=task_id, result_id=results[task_id])
    report = lifecycle.finalize(workspace=workspace, run_id=run_id)
    assert report["status"] == "completed" and report["acknowledged"] == 2
    meta = json.loads((_run_dir(workspace, run_id) / "run.json").read_text(encoding="utf-8"))
    assert meta["status"] == "completed" and meta["finished_at"]
    assert "native_finalize" in run_projector.PRESERVED_FIELDS
    events = list(run_journal.read_journal_bounded(_journal(workspace, run_id)).events)
    run_projector.project_run_snapshot(meta, events, journal_present=True)
    view = lifecycle.status(workspace=workspace, run_id=run_id)
    assert view["status"] == "completed"
    assert all(task["terminal"] and task["acked"] for task in view["tasks"])
    again = lifecycle.ack(workspace=workspace, run_id=run_id, task_id="one", result_id=results["one"])
    assert again["replayed"] is True
    assert lifecycle.status(workspace=workspace, run_id=run_id)["tasks"][0]["acked"] is True


@POSIX_ONLY
def test_finalize_failed_when_a_task_is_incomplete(tmp_path):
    workspace, run_id = _enroll(tmp_path, ("one", "two"))
    result_id = _complete(workspace, run_id, "one")
    lifecycle.ack(workspace=workspace, run_id=run_id, task_id="one", result_id=result_id)
    _claim(workspace, run_id, "two")
    _accepted(workspace, run_id, "two")
    report = lifecycle.finalize(workspace=workspace, run_id=run_id)
    assert report["status"] == "failed" and report["acknowledged"] == 1


@pytest.mark.parametrize("status", ["failed", "cancelled", "interrupted"])
@POSIX_ONLY
def test_finalize_failed_for_an_acknowledged_non_completed_terminal(tmp_path, status):
    workspace, run_id = _enroll(tmp_path, ("one",))
    result_id = _complete(workspace, run_id, "one", status=status)
    lifecycle.ack(workspace=workspace, run_id=run_id, task_id="one", result_id=result_id)
    report = lifecycle.finalize(workspace=workspace, run_id=run_id)
    assert report["status"] == "failed"
    meta = json.loads((_run_dir(workspace, run_id) / "run.json").read_text(encoding="utf-8"))
    assert meta["status"] == "failed"


@POSIX_ONLY
def test_finalize_failed_for_a_mixed_terminal_outcome(tmp_path):
    workspace, run_id = _enroll(tmp_path, ("one", "two"))
    for task_id, status in (("one", "completed"), ("two", "failed")):
        result_id = _complete(workspace, run_id, task_id, status=status)
        lifecycle.ack(workspace=workspace, run_id=run_id, task_id=task_id, result_id=result_id)
    assert lifecycle.finalize(workspace=workspace, run_id=run_id)["status"] == "failed"


@POSIX_ONLY
def test_finalize_refuses_an_unresolved_accept_conflict(tmp_path):
    workspace, run_id = _enroll(tmp_path, ("one",))
    _claim(workspace, run_id, "one")
    _accepted(workspace, run_id)
    with pytest.raises(dispatch.DispatchError) as conflict:
        _accepted(workspace, run_id, payload=_reply(workspace, run_id, childRunId="run-child-other"))
    assert conflict.value.code == "accept-conflict"
    with pytest.raises(dispatch.DispatchError) as refused:
        lifecycle.finalize(workspace=workspace, run_id=run_id)
    assert refused.value.code == "finalize-held"


@POSIX_ONLY
def test_hold_resolve_clears_a_resolvable_hold_and_never_regrants(tmp_path):
    workspace, run_id = _enroll(tmp_path, ("one",))
    _claim(workspace, run_id, "one")
    _accepted(workspace, run_id)
    with pytest.raises(dispatch.DispatchError) as conflict:
        _accepted(workspace, run_id, payload=_reply(workspace, run_id, childRunId="run-child-other"))
    assert conflict.value.code == "accept-conflict"
    cleared = dispatch.hold_resolve(
        workspace=workspace, run_id=run_id, task_id="one", payload=_reply(workspace, run_id), abandon=False
    )
    assert cleared["mode"] == "hold-resolved"
    assert lifecycle.status(workspace=workspace, run_id=run_id)["tasks"][0]["held_reason"] is None
    with pytest.raises(dispatch.DispatchError) as regrant:
        _claim(workspace, run_id, "one")
    assert regrant.value.code == "already-claimed"

    terminal = _observe(workspace, run_id)
    assert terminal["result_id"] is not None
    lifecycle.ack(workspace=workspace, run_id=run_id, task_id="one", result_id=terminal["result_id"])
    assert lifecycle.finalize(workspace=workspace, run_id=run_id)["status"] == "completed"


@POSIX_ONLY
def test_hold_resolve_refuses_a_different_handle_and_abandoned_stays_held(tmp_path):
    workspace, run_id = _enroll(tmp_path, ("one",))
    _claim(workspace, run_id, "one")
    _accepted(workspace, run_id)
    with pytest.raises(dispatch.DispatchError):
        _accepted(workspace, run_id, payload=_reply(workspace, run_id, childRunId="run-child-other"))
    with pytest.raises(dispatch.DispatchError) as refused:
        dispatch.hold_resolve(
            workspace=workspace,
            run_id=run_id,
            task_id="one",
            payload=_reply(workspace, run_id, childRunId="run-child-third"),
            abandon=False,
        )
    assert refused.value.code == "accept-conflict"
    dispatch.hold_resolve(workspace=workspace, run_id=run_id, task_id="one", payload=None, abandon=True)
    assert lifecycle.status(workspace=workspace, run_id=run_id)["tasks"][0]["held_reason"] == dispatch.ABANDONED
    with pytest.raises(dispatch.DispatchError) as held:
        dispatch.hold_resolve(
            workspace=workspace, run_id=run_id, task_id="one", payload=_reply(workspace, run_id), abandon=False
        )
    assert held.value.code == "task-held"


@POSIX_ONLY
def test_finalize_refuses_an_abandoned_task_and_status_still_reads(tmp_path):
    workspace, run_id = _enroll(tmp_path, ("one", "two"))
    result_id = _complete(workspace, run_id, "one")
    lifecycle.ack(workspace=workspace, run_id=run_id, task_id="one", result_id=result_id)
    _claim(workspace, run_id, "two")
    dispatch.hold_resolve(workspace=workspace, run_id=run_id, task_id="two", payload=None, abandon=True)
    with pytest.raises(dispatch.DispatchError) as refused:
        lifecycle.finalize(workspace=workspace, run_id=run_id)
    assert refused.value.code == "finalize-abandoned"
    meta = json.loads((_run_dir(workspace, run_id) / "run.json").read_text(encoding="utf-8"))
    assert meta["status"] == "awaiting-native-parent"
    view = lifecycle.status(workspace=workspace, run_id=run_id)
    assert view["tasks"][1]["held_reason"] == dispatch.ABANDONED


@POSIX_ONLY
def test_correlation_events_and_status_carry_no_child_ids_or_text(tmp_path, capsys):
    workspace, run_id = _enroll(tmp_path, ("one",))
    _claim(workspace, run_id, "one")
    _accepted(workspace, run_id, "one", _terminal_reply(workspace, run_id))
    correlated = [event for event in _events(workspace, run_id) if event.event_type == dispatch.CORRELATION_EVENT]
    assert correlated and all(not {"child_thread_id", "child_run_id"} & set(event.payload) for event in correlated)
    view = lifecycle.status(workspace=workspace, run_id=run_id)
    text = json.dumps(view)
    assert TASK_TEXT not in text and "thread-child" not in text and "run-child" not in text
    assert set(view["tasks"][0]) == {
        "task_id",
        "state",
        "held_reason",
        "correlated",
        "terminal",
        "acked",
        "cancel_requested",
        "attempt",
        "seat_digest",
    }
    capsys.readouterr()
    assert cli.main(["t3", "status", run_id, "--json", "--target", str(workspace)]) == 0
    assert json.loads(capsys.readouterr().out)["schema"] == lifecycle.STATUS_SCHEMA


def test_new_native_event_shapes_are_closed():
    run_events.validate_event_request(
        event_type="task.native-correlated",
        payload={"task_id": "one", "attempt_id": "a" * 64, "mode": "parent-reported", "handle_digest": "b" * 64},
        idempotency_key="k",
    )
    run_events.validate_event_request(
        event_type="task.native-cancel-requested",
        payload={"task_id": "one", "attempt_id": "a" * 64, "handle_digest": "b" * 64, "cancel_key": "c" * 64},
        idempotency_key="k",
    )
    run_events.validate_event_request(
        event_type="task.native-hold-resolved",
        payload={"task_id": "one", "attempt_id": "a" * 64, "reason": "accept-conflict", "evidence_digest": "b" * 64},
        idempotency_key="k",
    )
    with pytest.raises(run_events.CanonicalizationError):
        run_events.validate_event_request(
            event_type="task.native-hold-resolved",
            payload={
                "task_id": "one",
                "attempt_id": "a" * 64,
                "reason": "abandoned-unknown",
                "evidence_digest": "b" * 64,
            },
            idempotency_key="k",
        )
    with pytest.raises(run_events.CanonicalizationError):
        run_events.validate_event_request(
            event_type="task.native-correlated",
            payload={
                "task_id": "one",
                "attempt_id": "a" * 64,
                "mode": "parent-reported",
                "handle_digest": "b" * 64,
                "child_thread_id": None,
            },
            idempotency_key="k",
        )


def test_skill_template_names_every_verb_and_the_ack_boundary():
    text = SKILL.read_text(encoding="utf-8")
    for verb in ("t3 open", "t3 claim", "t3 accepted", "t3 observe", "t3 cancel", "t3 ack", "t3 finalize"):
        assert verb in text
    assert "acknowledges T3 delivery" in text and "Brigade `ack` is separate" in text
    assert "do not resubmit `delegate_task`" in text.lower() or "do not resubmit `delegate_task`" in text
    assert "hold-resolve" in text and "delegate_task" in text
    assert "t3-unsupported" in text and "brigade run batch" in text


@POSIX_ONLY
def test_refused_hold_resolution_keeps_the_task_held(tmp_path):
    """A contradictory operator assertion must not clear the hold (security review)."""
    workspace, run_id = _enroll(tmp_path, ("one",))
    _claim(workspace, run_id, "one")
    _accepted(workspace, run_id, payload=_terminal_reply(workspace, run_id))
    with pytest.raises(dispatch.DispatchError) as conflict:
        _accepted(workspace, run_id, payload=_reply(workspace, run_id, childRunId="run-child-other"))
    assert conflict.value.code == "accept-conflict"

    with pytest.raises(dispatch.DispatchError):
        dispatch.hold_resolve(
            workspace=workspace,
            run_id=run_id,
            task_id="one",
            payload=_terminal_reply(workspace, run_id, status="failed"),
            abandon=False,
        )

    assert lifecycle.status(workspace=workspace, run_id=run_id)["tasks"][0]["held_reason"] == "accept-conflict"
    assert not [event for event in _events(workspace, run_id) if event.event_type == "task.native-hold-resolved"]
    with pytest.raises(dispatch.DispatchError) as refused:
        lifecycle.finalize(workspace=workspace, run_id=run_id)
    assert refused.value.code == "finalize-held"
