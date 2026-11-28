"""Operator release of one uncorrelated governed native T3 claim."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from brigade import (
    cli,
    native_child_capacity as capacity_core,
    native_child_journal as journal_core,
    native_t3_admission as admission,
    native_t3_capacity as capacity,
    native_t3_dispatch as dispatch,
    native_t3_enrollment as enrollment,
    run_tasks,
)
from tests.test_native_t3_claim import (
    POSIX_ONLY,
    TASK_TEXT,
    THREAD,
    _claim,
    _enroll,
    _events,
    _reply,
    _run_dir,
    _terminal_reply,
    _workspace,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _log(tmp_path: Path, *, parent: str = THREAD, records: list[dict] | None = None, name: str = "activity.json"):
    payload = {
        "parent_thread": parent,
        "events": records if records is not None else [{"ts": _now(), "tool": "task_status", "thread_id": parent}],
    }
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _jsonl(tmp_path: Path, *, records: list[dict] | None = None, name: str = "activity.jsonl"):
    rows = records if records is not None else [{"ts": _now(), "tool": "task_status", "thread_id": THREAD}]
    path = tmp_path / name
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return path


def _release(
    workspace: Path,
    run_id: str,
    evidence: Path,
    *,
    task_id: str = "one",
    parent: str = THREAD,
    confirm: bool = True,
) -> dict:
    return dispatch.release_claim(
        workspace=workspace,
        run_id=run_id,
        task_id=task_id,
        parent_thread=parent,
        evidence=evidence,
        confirm_no_delegate=confirm,
    )


def _release_events(workspace: Path, run_id: str) -> list:
    return [event for event in _events(workspace, run_id) if event.event_type == journal_core.RELEASED_EVENT]


@POSIX_ONLY
@pytest.mark.parametrize("builder", [_log, _jsonl])
def test_release_frees_one_slot_and_records_the_evidence_digest(tmp_path, builder):
    workspace, run_id = _enroll(tmp_path)
    _claim(workspace, run_id, "one")
    _claim(workspace, run_id, "two")
    before = capacity.governed_occupancy(workspace)
    assert before == capacity.Occupancy(2, (("coder", 2),), True)
    roster = admission.load_roster(workspace)
    free_before = capacity.assess(roster, "coder", before).free
    assert free_before is not None

    evidence = builder(tmp_path)
    raw = evidence.read_bytes()
    report = _release(workspace, run_id, evidence)

    assert report["schema"] == dispatch.RELEASE_SCHEMA
    assert report["state"] == "released"
    assert report["task_id"] == "one" and report["operator_assertion"] is True
    assert report["evidence_sha256"] == hashlib.sha256(raw).hexdigest()
    assert report["evidence_bytes"] == len(raw)

    events = _events(workspace, run_id)
    (released,) = _release_events(workspace, run_id)
    assert released.payload["evidence_sha256"] == hashlib.sha256(raw).hexdigest()
    assert released.payload["evidence_bytes"] == len(raw)
    assert released.payload["operator_assertion"] is True
    assert released.payload["parent_thread"] == THREAD
    assert released.payload["attempt_id"] == report["attempt_id"]
    assert released.payload["dispatch_attempt"] == 1
    assert released.payload["released_at"].endswith("Z")

    reasons = capacity_core.capacity_reasons(events)
    assert "one" not in reasons
    assert reasons == {"two": "awaiting-terminal"}
    after = capacity.governed_occupancy(workspace)
    assert after == capacity.Occupancy(1, (("coder", 1),), True)
    assert capacity.assess(roster, "coder", after).free == free_before + 1


@POSIX_ONLY
def test_release_requires_the_operator_assertion(tmp_path):
    workspace, run_id = _enroll(tmp_path, ("one",))
    _claim(workspace, run_id, "one")
    evidence = _log(tmp_path)
    with pytest.raises(dispatch.DispatchError) as refused:
        _release(workspace, run_id, evidence, confirm=False)
    assert refused.value.code == "confirm-required"
    assert _release_events(workspace, run_id) == []
    assert capacity.governed_occupancy(workspace).live == 1


@POSIX_ONLY
def test_cli_release_requires_the_flag_and_prints_the_report(tmp_path, capsys):
    workspace, run_id = _enroll(tmp_path, ("one",))
    _claim(workspace, run_id, "one")
    evidence = _log(tmp_path)
    base = [
        "t3",
        "release-claim",
        run_id,
        "one",
        "--parent-thread",
        THREAD,
        "--evidence",
        str(evidence),
        "--target",
        str(workspace),
    ]
    capsys.readouterr()
    assert cli.main(base) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "[confirm-required]" in captured.err
    assert _release_events(workspace, run_id) == []

    assert cli.main([*base, "--confirm-no-delegate"]) == 0
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["state"] == "released" and report["task_id"] == "one"
    assert capacity.governed_occupancy(workspace).live == 0


@POSIX_ONLY
def test_release_refuses_a_correlated_task(tmp_path):
    workspace, run_id = _enroll(tmp_path, ("one",))
    _claim(workspace, run_id, "one")
    dispatch.accepted(
        workspace=workspace, run_id=run_id, task_id="one", parent_thread=THREAD, payload=_reply(workspace, run_id)
    )
    with pytest.raises(dispatch.DispatchError) as refused:
        _release(workspace, run_id, _log(tmp_path))
    assert refused.value.code == "native-reply-recorded"
    assert _release_events(workspace, run_id) == []
    assert capacity.governed_occupancy(workspace).live == 1


@POSIX_ONLY
def test_release_refuses_a_terminal_task(tmp_path):
    workspace, run_id = _enroll(tmp_path, ("one",))
    _claim(workspace, run_id, "one")
    dispatch.accepted(
        workspace=workspace,
        run_id=run_id,
        task_id="one",
        parent_thread=THREAD,
        payload=_terminal_reply(workspace, run_id),
    )
    before = (_run_dir(workspace, run_id) / "events" / "lifecycle.jsonl").read_bytes()
    with pytest.raises(dispatch.DispatchError) as refused:
        _release(workspace, run_id, _log(tmp_path))
    assert refused.value.code == "task-terminal"
    assert (_run_dir(workspace, run_id) / "events" / "lifecycle.jsonl").read_bytes() == before


@POSIX_ONLY
def test_release_refuses_a_held_task(tmp_path):
    workspace, run_id = _enroll(tmp_path, ("one",))
    _claim(workspace, run_id, "one")
    dispatch.hold_resolve(workspace=workspace, run_id=run_id, task_id="one", payload=None, abandon=True)
    with pytest.raises(dispatch.DispatchError) as refused:
        _release(workspace, run_id, _log(tmp_path))
    assert refused.value.code == "task-held"
    assert _release_events(workspace, run_id) == []


@POSIX_ONLY
@pytest.mark.parametrize(
    ("build", "code"),
    [
        (lambda tmp: tmp / "absent.json", "evidence-missing"),
        (lambda tmp: _invalid(tmp), "evidence-invalid"),
    ],
)
def test_release_refuses_missing_or_invalid_evidence(tmp_path, build, code):
    workspace, run_id = _enroll(tmp_path, ("one",))
    _claim(workspace, run_id, "one")
    before = (_run_dir(workspace, run_id) / "events" / "lifecycle.jsonl").read_bytes()
    with pytest.raises(dispatch.DispatchError) as refused:
        _release(workspace, run_id, build(tmp_path))
    assert refused.value.code == code
    assert (_run_dir(workspace, run_id) / "events" / "lifecycle.jsonl").read_bytes() == before
    assert capacity.governed_occupancy(workspace).live == 1


def _invalid(tmp_path: Path) -> Path:
    path = tmp_path / "activity-invalid.json"
    path.write_text("not json", encoding="utf-8")
    return path


@POSIX_ONLY
@pytest.mark.parametrize(
    "build",
    [
        lambda tmp: _log(tmp, parent="thread-other"),
        lambda tmp: _jsonl(tmp, records=[{"ts": _now(), "tool": "task_status", "thread_id": "thread-other"}]),
        lambda tmp: _jsonl(tmp, records=[{"ts": _now(), "tool": "task_status"}]),
    ],
)
def test_release_refuses_evidence_for_another_or_unnamed_thread(tmp_path, build):
    workspace, run_id = _enroll(tmp_path, ("one",))
    _claim(workspace, run_id, "one")
    with pytest.raises(dispatch.DispatchError) as refused:
        _release(workspace, run_id, build(tmp_path))
    assert refused.value.code in {"evidence-thread-mismatch", "evidence-thread-missing"}
    assert _release_events(workspace, run_id) == []
    assert capacity.governed_occupancy(workspace).live == 1


@POSIX_ONLY
@pytest.mark.parametrize(
    "record_factory",
    [
        lambda: {"ts": _now(), "tool": "delegate_task", "thread_id": THREAD},
        lambda: {"tool": "delegate_task", "thread_id": THREAD},
    ],
)
def test_release_refuses_a_delegate_call_in_the_claim_window(tmp_path, record_factory):
    workspace, run_id = _enroll(tmp_path, ("one",))
    _claim(workspace, run_id, "one")
    with pytest.raises(dispatch.DispatchError) as refused:
        _release(workspace, run_id, _log(tmp_path, records=[record_factory()]))
    assert refused.value.code == "evidence-conflict"
    assert _release_events(workspace, run_id) == []
    assert capacity.governed_occupancy(workspace).live == 1


@POSIX_ONLY
def test_evidence_before_the_claim_is_allowed(tmp_path):
    workspace, run_id = _enroll(tmp_path, ("one",))
    _claim(workspace, run_id, "one")
    earlier = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    records = [
        {"ts": earlier, "tool": "delegate_task", "thread_id": THREAD},
        {"ts": _now(), "tool": "task_status", "thread_id": THREAD},
    ]
    report = _release(workspace, run_id, _log(tmp_path, records=records))
    assert report["state"] == "released"
    assert capacity.governed_occupancy(workspace).live == 0


@POSIX_ONLY
def test_release_refuses_a_recorded_native_reply(tmp_path):
    workspace, run_id = _enroll(tmp_path, ("one",))
    _claim(workspace, run_id, "one")
    result = next(
        item for item in run_tasks.read_tasks(_run_dir(workspace, run_id)) if item.handle.spec.task_id == "one"
    )
    with enrollment.owned_run(workspace, _run_dir(workspace, run_id)):
        run_tasks.observe_task(
            _run_dir(workspace, run_id),
            workspace=workspace,
            handle=result.handle,
            observation=run_tasks.TaskObservation(status="running", containment="unknown"),
            expected_version=result.version,
        )
    with pytest.raises(dispatch.DispatchError) as refused:
        _release(workspace, run_id, _log(tmp_path))
    assert refused.value.code == "native-reply-recorded"
    assert _release_events(workspace, run_id) == []


@POSIX_ONLY
def test_late_reply_after_release_holds_without_a_second_slot(tmp_path):
    workspace, run_id = _enroll(tmp_path, ("one",))
    _claim(workspace, run_id, "one")
    _release(workspace, run_id, _log(tmp_path))
    assert capacity.governed_occupancy(workspace).live == 0

    with pytest.raises(dispatch.DispatchError) as refused:
        dispatch.accepted(
            workspace=workspace, run_id=run_id, task_id="one", parent_thread=THREAD, payload=_reply(workspace, run_id)
        )
    assert refused.value.code == "accept-conflict"
    events = _events(workspace, run_id)
    assert journal_core.held(events, "one") == journal_core.ACCEPT_CONFLICT
    assert not [event for event in events if event.event_type == dispatch.CORRELATION_EVENT]
    assert capacity_core.capacity_reasons(events) == {}
    assert capacity.governed_occupancy(workspace).live == 0


@POSIX_ONLY
def test_second_release_refuses_already_released(tmp_path):
    workspace, run_id = _enroll(tmp_path, ("one",))
    _claim(workspace, run_id, "one")
    evidence = _log(tmp_path)
    _release(workspace, run_id, evidence)
    journal = _run_dir(workspace, run_id) / "events" / "lifecycle.jsonl"
    before = journal.read_bytes()
    with pytest.raises(dispatch.DispatchError) as refused:
        _release(workspace, run_id, evidence)
    assert refused.value.code == "already-released"
    assert journal.read_bytes() == before
    assert len(_release_events(workspace, run_id)) == 1
    assert capacity.governed_occupancy(workspace).live == 0


@POSIX_ONLY
def test_release_leaves_another_run_and_task_untouched(tmp_path):
    workspace = _workspace(tmp_path)
    first = enrollment.enroll(batch_path=_batch(tmp_path, "request-a"), target=workspace)["run_id"]
    second = enrollment.enroll(batch_path=_batch(tmp_path, "request-b"), target=workspace)["run_id"]
    _claim(workspace, first, "one")
    _claim(workspace, second, "one")
    assert capacity.governed_occupancy(workspace) == capacity.Occupancy(2, (("coder", 2),), True)

    _release(workspace, first, _log(tmp_path), task_id="one")

    occupancy = capacity.governed_occupancy(workspace)
    assert occupancy == capacity.Occupancy(1, (("coder", 1),), True)
    assert capacity_core.capacity_reasons(_events(workspace, first)) == {}
    assert capacity_core.capacity_reasons(_events(workspace, second)) == {"one": "awaiting-terminal"}


def _batch(tmp_path: Path, request_id: str) -> Path:
    payload = {
        "schema": "brigade.run_batch.v1",
        "request_id": request_id,
        "tasks": [{"task_id": "one", "text": f"{TASK_TEXT} one", "seat": "coder", "isolation": "worktree"}],
        "synthesis": None,
    }
    path = tmp_path / f"batch-{request_id}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path
