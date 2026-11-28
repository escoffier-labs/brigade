"""Operator release of one uncorrelated governed native T3 claim."""

from __future__ import annotations

import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor, wait
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
    native_t3_lifecycle as lifecycle,
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


def _limit_one(workspace: Path) -> None:
    """Declare one workspace-wide governed unit for a capacity race."""
    roster = workspace / ".brigade" / "roster.toml"
    text = roster.read_text(encoding="utf-8")
    roster.write_text(f"{text.rstrip()}\n\n[limits]\nmax_workers = 1\n", encoding="utf-8")


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


def _claim_moment(workspace: Path, run_id: str, task_id: str = "one") -> datetime:
    for event in _events(workspace, run_id):
        if event.event_type == journal_core.CLAIM_EVENT and event.payload.get("task_id") == task_id:
            moment = dispatch._evidence_moment(event.recorded_at)
            assert moment is not None
            return moment
    raise AssertionError("the task has no durable claim")


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


def _reply_digest(workspace: Path, run_id: str, payload: dict, *, task_id: str = "one") -> str:
    """The handle digest the journal records for one reply."""
    verified = dispatch._verified(workspace, _run_dir(workspace, run_id), only=task_id)[task_id]
    binding = journal_core.claim_binding(_events(workspace, run_id), task_id)
    assert binding is not None
    result = dispatch.parse_claimed_result(payload, verified=verified, binding=binding)
    return dispatch._handle_digest(result, binding.parent_thread)


def _reclaim_events(workspace: Path, run_id: str) -> list:
    return [event for event in _events(workspace, run_id) if event.event_type == journal_core.RECLAIMED_EVENT]


def _void_events(workspace: Path, run_id: str) -> list:
    return [event for event in _events(workspace, run_id) if event.event_type == journal_core.VOIDED_EVENT]


@POSIX_ONLY
def test_late_reply_after_release_holds_and_restores_the_slot(tmp_path):
    workspace, run_id = _enroll(tmp_path, ("one",))
    _claim(workspace, run_id, "one")
    roster = admission.load_roster(workspace)
    _release(workspace, run_id, _log(tmp_path))
    released = capacity.governed_occupancy(workspace)
    assert released.live == 0
    free_after_release = capacity.assess(roster, "coder", released).free
    assert free_after_release is not None

    reply = _reply(workspace, run_id)
    with pytest.raises(dispatch.DispatchError) as refused:
        dispatch.accepted(workspace=workspace, run_id=run_id, task_id="one", parent_thread=THREAD, payload=reply)
    assert refused.value.code == "accept-conflict"

    events = _events(workspace, run_id)
    assert journal_core.held(events, "one") == journal_core.ACCEPT_CONFLICT
    assert not [event for event in events if event.event_type == dispatch.CORRELATION_EVENT]
    assert capacity_core.capacity_reasons(events) == {"one": "accept-conflict"}
    after = capacity.governed_occupancy(workspace)
    assert after == capacity.Occupancy(1, (("coder", 1),), True)
    assert capacity.assess(roster, "coder", after).free == free_after_release - 1

    (release,) = _release_events(workspace, run_id)
    (reclaimed,) = [event.payload for event in _reclaim_events(workspace, run_id)]
    assert reclaimed["reply_digest"] == _reply_digest(workspace, run_id, reply)
    assert reclaimed["release_ref"] == journal_core.release_reference(release.payload)
    assert reclaimed["reclaimed_at"].endswith("Z")


@POSIX_ONLY
def test_hold_resolve_voids_the_release_and_the_run_closes(tmp_path):
    workspace, run_id = _enroll(tmp_path, ("one",))
    _claim(workspace, run_id, "one")
    _release(workspace, run_id, _log(tmp_path))
    reply = _reply(workspace, run_id)
    with pytest.raises(dispatch.DispatchError) as refused:
        dispatch.accepted(workspace=workspace, run_id=run_id, task_id="one", parent_thread=THREAD, payload=reply)
    assert refused.value.code == "accept-conflict"

    report = dispatch.hold_resolve(workspace=workspace, run_id=run_id, task_id="one", payload=reply, abandon=False)

    assert report["mode"] == "hold-resolved" and report["voided"] is True
    events = _events(workspace, run_id)
    assert journal_core.held(events, "one") is None
    assert journal_core.released(events, "one") is None
    (release,) = _release_events(workspace, run_id)
    (void,) = [event.payload for event in _void_events(workspace, run_id)]
    assert void["reply_digest"] == _reply_digest(workspace, run_id, reply)
    assert void["release_ref"] == journal_core.release_reference(release.payload)
    assert void["voided_at"].endswith("Z")
    assert void["attempt_id"] == release.payload["attempt_id"] == report["attempt_id"]

    row = lifecycle.status(workspace=workspace, run_id=run_id)["tasks"][0]
    assert row["released"] is False and row["voided"] is True and row["held_reason"] is None
    assert row["correlated"] is True
    assert capacity_core.capacity_reasons(events) == {"one": "awaiting-terminal"}

    observed = lifecycle.observe(
        workspace=workspace,
        run_id=run_id,
        task_id="one",
        parent_thread=THREAD,
        payload=_terminal_reply(workspace, run_id),
    )
    result_id = observed["result_id"]
    assert result_id is not None
    lifecycle.ack(workspace=workspace, run_id=run_id, task_id="one", result_id=result_id)
    closed = lifecycle.finalize(workspace=workspace, run_id=run_id)
    assert closed["status"] == "completed"
    assert capacity_core.capacity_reasons(_events(workspace, run_id)) == {}
    assert capacity.governed_occupancy(workspace).live == 0


@POSIX_ONLY
def test_void_with_a_sibling_reply_refuses_and_writes_nothing(tmp_path):
    workspace, run_id = _enroll(tmp_path, ("one", "two"))
    _claim(workspace, run_id, "one")
    _claim(workspace, run_id, "two")
    _release(workspace, run_id, _log(tmp_path), task_id="one")
    with pytest.raises(dispatch.DispatchError) as first:
        dispatch.accepted(
            workspace=workspace, run_id=run_id, task_id="one", parent_thread=THREAD, payload=_reply(workspace, run_id)
        )
    assert first.value.code == "accept-conflict"
    journal_path = _journal_path(workspace, run_id)
    before = journal_path.read_bytes()

    with pytest.raises(dispatch.DispatchError) as refused:
        dispatch.hold_resolve(
            workspace=workspace,
            run_id=run_id,
            task_id="one",
            payload=_reply(workspace, run_id, "two"),
            abandon=False,
        )
    assert refused.value.code == "native-mismatch"
    assert journal_path.read_bytes() == before
    assert journal_core.held(_events(workspace, run_id), "one") == journal_core.ACCEPT_CONFLICT
    assert _void_events(workspace, run_id) == []
    assert capacity_core.capacity_reasons(_events(workspace, run_id))["one"] == "accept-conflict"


@POSIX_ONLY
def test_second_void_refuses_and_writes_nothing(tmp_path):
    workspace, run_id = _enroll(tmp_path, ("one",))
    _claim(workspace, run_id, "one")
    _release(workspace, run_id, _log(tmp_path))
    reply = _reply(workspace, run_id)
    with pytest.raises(dispatch.DispatchError):
        dispatch.accepted(workspace=workspace, run_id=run_id, task_id="one", parent_thread=THREAD, payload=reply)
    dispatch.hold_resolve(workspace=workspace, run_id=run_id, task_id="one", payload=reply, abandon=False)
    journal_path = _journal_path(workspace, run_id)
    before = journal_path.read_bytes()

    with pytest.raises(dispatch.DispatchError) as refused:
        dispatch.hold_resolve(workspace=workspace, run_id=run_id, task_id="one", payload=reply, abandon=False)
    assert refused.value.code == "already-voided"
    assert journal_path.read_bytes() == before
    assert len(_void_events(workspace, run_id)) == 1


@POSIX_ONLY
def test_late_observe_as_first_contact_restores_the_slot(tmp_path):
    workspace, run_id = _enroll(tmp_path, ("one",))
    _claim(workspace, run_id, "one")
    _release(workspace, run_id, _log(tmp_path))
    reply = _reply(workspace, run_id)

    with pytest.raises(dispatch.DispatchError) as refused:
        lifecycle.observe(workspace=workspace, run_id=run_id, task_id="one", parent_thread=THREAD, payload=reply)
    assert refused.value.code == "accept-conflict"

    events = _events(workspace, run_id)
    assert journal_core.held(events, "one") == journal_core.ACCEPT_CONFLICT
    (reclaimed,) = [event.payload for event in _reclaim_events(workspace, run_id)]
    assert reclaimed["reply_digest"] == _reply_digest(workspace, run_id, reply)
    assert capacity_core.capacity_reasons(events) == {"one": "accept-conflict"}
    assert capacity.governed_occupancy(workspace).live == 1


@POSIX_ONLY
def test_abandon_on_a_voided_task_matches_any_correlated_task(tmp_path):
    workspace, run_id = _enroll(tmp_path, ("one",))
    _claim(workspace, run_id, "one")
    _release(workspace, run_id, _log(tmp_path))
    reply = _reply(workspace, run_id)
    with pytest.raises(dispatch.DispatchError):
        dispatch.accepted(workspace=workspace, run_id=run_id, task_id="one", parent_thread=THREAD, payload=reply)
    dispatch.hold_resolve(workspace=workspace, run_id=run_id, task_id="one", payload=reply, abandon=False)

    report = dispatch.hold_resolve(workspace=workspace, run_id=run_id, task_id="one", payload=None, abandon=True)

    assert report["mode"] == journal_core.ABANDONED
    events = _events(workspace, run_id)
    assert journal_core.held(events, "one") == journal_core.ABANDONED
    assert capacity_core.capacity_reasons(events) == {"one": "abandoned-unknown"}
    with pytest.raises(dispatch.DispatchError) as refused:
        lifecycle.finalize(workspace=workspace, run_id=run_id)
    assert refused.value.code == "finalize-abandoned"


@POSIX_ONLY
def test_reclaim_and_void_leave_another_task_untouched(tmp_path):
    workspace, run_id = _enroll(tmp_path, ("one", "two"))
    _claim(workspace, run_id, "one")
    _claim(workspace, run_id, "two")
    _release(workspace, run_id, _log(tmp_path), task_id="one")
    reply = _reply(workspace, run_id)
    with pytest.raises(dispatch.DispatchError):
        dispatch.accepted(workspace=workspace, run_id=run_id, task_id="one", parent_thread=THREAD, payload=reply)
    dispatch.hold_resolve(workspace=workspace, run_id=run_id, task_id="one", payload=reply, abandon=False)

    events = _events(workspace, run_id)
    assert capacity_core.capacity_reasons(events) == {"one": "awaiting-terminal", "two": "awaiting-terminal"}
    assert capacity.governed_occupancy(workspace) == capacity.Occupancy(2, (("coder", 2),), True)
    rows = {row["task_id"]: row for row in lifecycle.status(workspace=workspace, run_id=run_id)["tasks"]}
    assert rows["one"]["voided"] is True and rows["one"]["released"] is False
    assert rows["two"]["voided"] is False and rows["two"]["released"] is False
    assert rows["two"]["held_reason"] is None


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


@POSIX_ONLY
@pytest.mark.parametrize("side", ["before", "after"])
def test_delegate_timestamp_without_an_offset_refuses(tmp_path, side):
    workspace, run_id = _enroll(tmp_path, ("one",))
    _claim(workspace, run_id, "one")
    claim = _claim_moment(workspace, run_id)
    shift = timedelta(hours=-1) if side == "before" else timedelta(hours=1)
    naive = (claim + shift).replace(tzinfo=None).isoformat()
    with pytest.raises(dispatch.DispatchError) as refused:
        _release(
            workspace,
            run_id,
            _log(tmp_path, records=[{"ts": naive, "tool": "delegate_task", "thread_id": THREAD}]),
        )
    assert refused.value.code == "evidence-conflict"
    assert _release_events(workspace, run_id) == []
    assert capacity.governed_occupancy(workspace).live == 1


@POSIX_ONLY
def test_date_only_delegate_timestamp_refuses(tmp_path):
    workspace, run_id = _enroll(tmp_path, ("one",))
    _claim(workspace, run_id, "one")
    day = _claim_moment(workspace, run_id).astimezone(timezone.utc).strftime("%Y-%m-%d")
    with pytest.raises(dispatch.DispatchError) as refused:
        _release(workspace, run_id, _log(tmp_path, records=[{"ts": day, "tool": "delegate_task", "thread_id": THREAD}]))
    assert refused.value.code == "evidence-conflict"
    assert _release_events(workspace, run_id) == []


@POSIX_ONLY
def test_whole_second_delegate_timestamp_in_the_claim_second_refuses(tmp_path):
    workspace, run_id = _enroll(tmp_path, ("one",))
    _claim(workspace, run_id, "one")
    stamp = _claim_moment(workspace, run_id).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
    with pytest.raises(dispatch.DispatchError) as refused:
        _release(
            workspace,
            run_id,
            _log(tmp_path, records=[{"ts": stamp, "tool": "delegate_task", "thread_id": THREAD}]),
        )
    assert refused.value.code == "evidence-conflict"
    assert _release_events(workspace, run_id) == []


@POSIX_ONLY
def test_explicit_offset_before_the_claim_is_allowed(tmp_path):
    workspace, run_id = _enroll(tmp_path, ("one",))
    _claim(workspace, run_id, "one")
    earlier = (_claim_moment(workspace, run_id) - timedelta(seconds=30)).astimezone(
        timezone(timedelta(hours=5, minutes=30))
    )
    report = _release(
        workspace,
        run_id,
        _log(tmp_path, records=[{"ts": earlier.isoformat(), "tool": "delegate_task", "thread_id": THREAD}]),
    )
    assert report["state"] == "released"
    assert len(_release_events(workspace, run_id)) == 1


@POSIX_ONLY
def test_evidence_with_a_repeated_key_refuses(tmp_path):
    workspace, run_id = _enroll(tmp_path, ("one",))
    _claim(workspace, run_id, "one")
    path = tmp_path / "duplicate-key.json"
    row = '{"ts": "%s", "tool": "task_status", "thread_id": "%s"}' % (_now(), THREAD)
    path.write_text(
        '{"parent_thread": "%s", "events": [], "events": [%s]}' % (THREAD, row),
        encoding="utf-8",
    )
    with pytest.raises(dispatch.DispatchError) as refused:
        _release(workspace, run_id, path)
    assert refused.value.code == "evidence-invalid"
    assert _release_events(workspace, run_id) == []
    assert capacity.governed_occupancy(workspace).live == 1


@POSIX_ONLY
@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_evidence_with_a_non_finite_number_refuses(tmp_path, constant):
    workspace, run_id = _enroll(tmp_path, ("one",))
    _claim(workspace, run_id, "one")
    path = tmp_path / f"non-finite-{constant.strip('-')}.json"
    path.write_text(
        '{"parent_thread": "%s", "events": [{"ts": %s, "tool": "task_status", "thread_id": "%s"}]}'
        % (THREAD, constant, THREAD),
        encoding="utf-8",
    )
    with pytest.raises(dispatch.DispatchError) as refused:
        _release(workspace, run_id, path)
    assert refused.value.code == "evidence-invalid"
    assert _release_events(workspace, run_id) == []


@POSIX_ONLY
@pytest.mark.parametrize("kind", ["directory", "symlink"])
def test_release_refuses_evidence_that_is_not_a_regular_file(tmp_path, kind):
    workspace, run_id = _enroll(tmp_path, ("one",))
    _claim(workspace, run_id, "one")
    if kind == "directory":
        path = tmp_path / "evidence-dir"
        path.mkdir()
    else:
        path = tmp_path / "evidence-link.json"
        try:
            path.symlink_to(_log(tmp_path))
        except (OSError, NotImplementedError):
            pytest.skip("symlink creation is unavailable on this platform")
    with pytest.raises(dispatch.DispatchError) as refused:
        _release(workspace, run_id, path)
    assert refused.value.code == "evidence-unreadable"
    assert _release_events(workspace, run_id) == []
    assert capacity.governed_occupancy(workspace).live == 1


def _journal_path(workspace: Path, run_id: str) -> Path:
    return _run_dir(workspace, run_id) / "events" / "lifecycle.jsonl"


@POSIX_ONLY
def test_malformed_reply_after_release_records_no_hold(tmp_path):
    workspace, run_id = _enroll(tmp_path, ("one",))
    _claim(workspace, run_id, "one")
    _release(workspace, run_id, _log(tmp_path))
    journal_path = _journal_path(workspace, run_id)
    before = journal_path.read_bytes()
    with pytest.raises(dispatch.DispatchError) as refused:
        dispatch.accepted(workspace=workspace, run_id=run_id, task_id="one", parent_thread=THREAD, payload={"junk": 1})
    assert refused.value.code == "native-invalid"
    assert journal_core.held(_events(workspace, run_id), "one") is None
    assert journal_path.read_bytes() == before
    assert capacity.governed_occupancy(workspace).live == 0


@POSIX_ONLY
def test_sibling_reply_after_release_records_no_hold(tmp_path):
    workspace, run_id = _enroll(tmp_path, ("one", "two"))
    _claim(workspace, run_id, "one")
    _claim(workspace, run_id, "two")
    _release(workspace, run_id, _log(tmp_path), task_id="one")
    journal_path = _journal_path(workspace, run_id)
    before = journal_path.read_bytes()
    with pytest.raises(dispatch.DispatchError) as refused:
        dispatch.accepted(
            workspace=workspace,
            run_id=run_id,
            task_id="one",
            parent_thread=THREAD,
            payload=_reply(workspace, run_id, "two"),
        )
    assert refused.value.code == "native-mismatch"
    assert journal_core.held(_events(workspace, run_id), "one") is None
    assert journal_path.read_bytes() == before


@POSIX_ONLY
def test_malformed_observe_after_release_records_no_hold(tmp_path):
    workspace, run_id = _enroll(tmp_path, ("one",))
    _claim(workspace, run_id, "one")
    _release(workspace, run_id, _log(tmp_path))
    journal_path = _journal_path(workspace, run_id)
    before = journal_path.read_bytes()
    with pytest.raises(dispatch.DispatchError) as refused:
        lifecycle.observe(workspace=workspace, run_id=run_id, task_id="one", parent_thread=THREAD, payload={"junk": 1})
    assert refused.value.code == "native-invalid"
    assert journal_core.held(_events(workspace, run_id), "one") is None
    assert journal_path.read_bytes() == before


@POSIX_ONLY
def test_hold_resolve_with_a_sibling_reply_records_no_hold(tmp_path):
    workspace, run_id = _enroll(tmp_path, ("one", "two"))
    _claim(workspace, run_id, "one")
    _claim(workspace, run_id, "two")
    _release(workspace, run_id, _log(tmp_path), task_id="one")
    journal_path = _journal_path(workspace, run_id)
    before = journal_path.read_bytes()
    with pytest.raises(dispatch.DispatchError) as refused:
        dispatch.hold_resolve(
            workspace=workspace,
            run_id=run_id,
            task_id="one",
            payload=_reply(workspace, run_id, "two"),
            abandon=False,
        )
    assert refused.value.code == "native-mismatch"
    assert journal_core.held(_events(workspace, run_id), "one") is None
    assert journal_path.read_bytes() == before


@POSIX_ONLY
def test_abandon_refuses_a_released_claim_and_writes_nothing(tmp_path):
    workspace, run_id = _enroll(tmp_path, ("one",))
    _claim(workspace, run_id, "one")
    _release(workspace, run_id, _log(tmp_path))
    journal_path = _journal_path(workspace, run_id)
    before = journal_path.read_bytes()
    with pytest.raises(dispatch.DispatchError) as refused:
        dispatch.hold_resolve(workspace=workspace, run_id=run_id, task_id="one", payload=None, abandon=True)
    assert refused.value.code == "already-released"
    assert journal_core.held(_events(workspace, run_id), "one") is None
    assert journal_path.read_bytes() == before
    assert capacity.governed_occupancy(workspace).live == 0


@POSIX_ONLY
def test_status_reports_a_released_task(tmp_path):
    workspace, run_id = _enroll(tmp_path, ("one",))
    _claim(workspace, run_id, "one")
    before = lifecycle.status(workspace=workspace, run_id=run_id)["tasks"][0]
    assert before["released"] is False
    _release(workspace, run_id, _log(tmp_path))
    after = lifecycle.status(workspace=workspace, run_id=run_id)["tasks"][0]
    assert after["released"] is True and after["held_reason"] is None


@POSIX_ONLY
def test_a_claim_racing_a_late_reply_cannot_take_the_reclaimed_slot(tmp_path, monkeypatch):
    """A reclaim takes the capacity lock first, so a claim cannot take its slot."""
    workspace = _workspace(tmp_path)
    _limit_one(workspace)
    first = enrollment.enroll(batch_path=_batch(tmp_path, "request-a"), target=workspace)["run_id"]
    second = enrollment.enroll(batch_path=_batch(tmp_path, "request-b"), target=workspace)["run_id"]
    _claim(workspace, first, "one")
    _release(workspace, first, _log(tmp_path))
    assert capacity.governed_occupancy(workspace).live == 0
    reply = _reply(workspace, first)

    entered = threading.Event()
    proceed = threading.Event()
    original = dispatch._record_reclaim

    def gated(*args: object, **kwargs: object):
        entered.set()
        assert proceed.wait(10)
        return original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(dispatch, "_record_reclaim", gated)

    def reclaim() -> str:
        with pytest.raises(dispatch.DispatchError) as refused:
            dispatch.accepted(workspace=workspace, run_id=first, task_id="one", parent_thread=THREAD, payload=reply)
        return refused.value.code

    def take_the_slot() -> str:
        try:
            dispatch.claim(workspace=workspace, run_id=second, task_id="one", parent_thread=THREAD)
        except dispatch.DispatchError as exc:
            return exc.code
        return "granted"

    with ThreadPoolExecutor(max_workers=2) as pool:
        reclaiming = pool.submit(reclaim)
        assert entered.wait(10), "the reclaim never reached its slot record"
        claiming = pool.submit(take_the_slot)
        done, _pending = wait([claiming], timeout=1.0)
        assert done == set(), "a claim committed while the reclaim held the capacity lock"
        proceed.set()
        assert reclaiming.result(timeout=10) == "accept-conflict"
        assert claiming.result(timeout=10) == "capacity-denied"

    assert capacity_core.capacity_reasons(_events(workspace, first)) == {"one": "accept-conflict"}
    assert capacity_core.capacity_reasons(_events(workspace, second)) == {}
    assert capacity.governed_occupancy(workspace) == capacity.Occupancy(1, (("coder", 1),), True)
    (reclaimed,) = [event.payload for event in _reclaim_events(workspace, first)]
    assert reclaimed["capacity"] == "within-limit"


@POSIX_ONLY
def test_an_over_limit_reclaim_is_recorded_and_reported(tmp_path):
    """A reclaim still records the child, and capacity keeps counting it."""
    workspace = _workspace(tmp_path)
    _limit_one(workspace)
    first = enrollment.enroll(batch_path=_batch(tmp_path, "request-a"), target=workspace)["run_id"]
    second = enrollment.enroll(batch_path=_batch(tmp_path, "request-b"), target=workspace)["run_id"]
    _claim(workspace, first, "one")
    _release(workspace, first, _log(tmp_path))
    _claim(workspace, second, "one")

    with pytest.raises(dispatch.DispatchError) as refused:
        dispatch.accepted(
            workspace=workspace, run_id=first, task_id="one", parent_thread=THREAD, payload=_reply(workspace, first)
        )
    assert refused.value.code == "accept-conflict"

    (reclaimed,) = [event.payload for event in _reclaim_events(workspace, first)]
    assert reclaimed["capacity"] == "over-limit"
    assert capacity_core.capacity_reasons(_events(workspace, first)) == {"one": "accept-conflict"}
    assert capacity_core.capacity_reasons(_events(workspace, second)) == {"one": "awaiting-terminal"}
    assert capacity.governed_occupancy(workspace) == capacity.Occupancy(2, (("coder", 2),), True)


@POSIX_ONLY
def test_a_retry_finishes_a_void_that_lost_its_hold_resolution(tmp_path, monkeypatch):
    workspace, run_id = _enroll(tmp_path, ("one",))
    _claim(workspace, run_id, "one")
    _release(workspace, run_id, _log(tmp_path))
    reply = _reply(workspace, run_id)
    with pytest.raises(dispatch.DispatchError):
        dispatch.accepted(workspace=workspace, run_id=run_id, task_id="one", parent_thread=THREAD, payload=reply)

    original = dispatch._append

    def crash_before_resolution(workspace_path: Path, run_dir: Path, event_type: str, payload: dict, *, key: str):
        if event_type == journal_core.HOLD_RESOLVED_EVENT:
            raise RuntimeError("simulated crash after the void record")
        return original(workspace_path, run_dir, event_type, payload, key=key)

    monkeypatch.setattr(dispatch, "_append", crash_before_resolution)
    with pytest.raises(RuntimeError):
        dispatch.hold_resolve(workspace=workspace, run_id=run_id, task_id="one", payload=reply, abandon=False)
    monkeypatch.setattr(dispatch, "_append", original)

    events = _events(workspace, run_id)
    assert len(_void_events(workspace, run_id)) == 1
    assert journal_core.held(events, "one") == journal_core.ACCEPT_CONFLICT
    assert not [event for event in events if event.event_type == journal_core.HOLD_RESOLVED_EVENT]

    report = dispatch.hold_resolve(workspace=workspace, run_id=run_id, task_id="one", payload=reply, abandon=False)

    assert report["mode"] == "hold-resolved"
    events = _events(workspace, run_id)
    assert journal_core.held(events, "one") is None
    assert journal_core.released(events, "one") is None
    assert len(_void_events(workspace, run_id)) == 1

    observed = lifecycle.observe(
        workspace=workspace,
        run_id=run_id,
        task_id="one",
        parent_thread=THREAD,
        payload=_terminal_reply(workspace, run_id),
    )
    result_id = observed["result_id"]
    assert result_id is not None
    lifecycle.ack(workspace=workspace, run_id=run_id, task_id="one", result_id=result_id)
    assert lifecycle.finalize(workspace=workspace, run_id=run_id)["status"] == "completed"


@POSIX_ONLY
def test_a_different_reply_after_a_half_done_void_refuses(tmp_path, monkeypatch):
    workspace, run_id = _enroll(tmp_path, ("one",))
    _claim(workspace, run_id, "one")
    _release(workspace, run_id, _log(tmp_path))
    reply = _reply(workspace, run_id)
    with pytest.raises(dispatch.DispatchError):
        dispatch.accepted(workspace=workspace, run_id=run_id, task_id="one", parent_thread=THREAD, payload=reply)

    original = dispatch._append

    def crash_before_resolution(workspace_path: Path, run_dir: Path, event_type: str, payload: dict, *, key: str):
        if event_type == journal_core.HOLD_RESOLVED_EVENT:
            raise RuntimeError("simulated crash after the void record")
        return original(workspace_path, run_dir, event_type, payload, key=key)

    monkeypatch.setattr(dispatch, "_append", crash_before_resolution)
    with pytest.raises(RuntimeError):
        dispatch.hold_resolve(workspace=workspace, run_id=run_id, task_id="one", payload=reply, abandon=False)
    monkeypatch.setattr(dispatch, "_append", original)

    with pytest.raises(dispatch.DispatchError) as refused:
        dispatch.hold_resolve(
            workspace=workspace,
            run_id=run_id,
            task_id="one",
            payload=_reply(workspace, run_id, childRunId="run-other"),
            abandon=False,
        )
    assert refused.value.code == "already-voided"
    events = _events(workspace, run_id)
    assert journal_core.held(events, "one") == journal_core.ACCEPT_CONFLICT
    assert len(_void_events(workspace, run_id)) == 1


@POSIX_ONLY
def test_stale_evidence_refuses(tmp_path):
    workspace, run_id = _enroll(tmp_path, ("one",))
    _claim(workspace, run_id, "one")
    stale = (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat().replace("+00:00", "Z")
    with pytest.raises(dispatch.DispatchError) as refused:
        _release(
            workspace,
            run_id,
            _log(tmp_path, records=[{"ts": stale, "tool": "task_status", "thread_id": THREAD}]),
        )
    assert refused.value.code == "evidence-stale"
    assert _release_events(workspace, run_id) == []
    assert capacity.governed_occupancy(workspace).live == 1


@POSIX_ONLY
def test_future_evidence_refuses(tmp_path):
    workspace, run_id = _enroll(tmp_path, ("one",))
    _claim(workspace, run_id, "one")
    future = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    with pytest.raises(dispatch.DispatchError) as refused:
        _release(
            workspace,
            run_id,
            _log(tmp_path, records=[{"ts": future, "tool": "task_status", "thread_id": THREAD}]),
        )
    assert refused.value.code == "evidence-future"
    assert _release_events(workspace, run_id) == []
    assert capacity.governed_occupancy(workspace).live == 1


@POSIX_ONLY
def test_fresh_evidence_passes(tmp_path):
    workspace, run_id = _enroll(tmp_path, ("one",))
    _claim(workspace, run_id, "one")
    recent = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
    report = _release(
        workspace,
        run_id,
        _log(tmp_path, records=[{"ts": recent, "tool": "task_status", "thread_id": THREAD}]),
    )
    assert report["state"] == "released"
    assert capacity.governed_occupancy(workspace).live == 0


@POSIX_ONLY
def test_release_refuses_a_reply_recorded_after_the_evidence_check(tmp_path, monkeypatch):
    workspace, run_id = _enroll(tmp_path, ("one",))
    _claim(workspace, run_id, "one")
    binding = journal_core.claim_binding(_events(workspace, run_id), "one")
    assert binding is not None
    original = dispatch._evidence_bytes

    def inject_correlation(path: Path) -> bytes:
        raw = original(path)
        dispatch._append(
            workspace,
            _run_dir(workspace, run_id),
            dispatch.CORRELATION_EVENT,
            {
                "task_id": "one",
                "attempt_id": binding.attempt_id,
                "mode": dispatch.PARENT_REPORTED,
                "handle_digest": "a" * 64,
            },
            key="test-late-correlation",
        )
        return raw

    monkeypatch.setattr(dispatch, "_evidence_bytes", inject_correlation)
    with pytest.raises(dispatch.DispatchError) as refused:
        _release(workspace, run_id, _log(tmp_path))
    assert refused.value.code == "native-reply-recorded"
    assert _release_events(workspace, run_id) == []
    assert capacity.governed_occupancy(workspace).live == 1


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
