import errno
import json
import os
import shutil
import threading
import time as system_time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests import thread_sync

from brigade import aboyeur
from brigade import cli
from brigade import daily_cmd
from brigade import run_checkpoint
from brigade import run_events
from brigade import run_journal
from brigade import run_lifecycle
from brigade import run_resume
from brigade import run_redaction
from brigade import runguard
from brigade import runs_cmd
from brigade import tools_cmd


def _write_json(path, payload):
    path.write_text(json.dumps(payload, indent=2) + "\n")


def test_artifact_collection_status_is_nonterminal():
    assert runs_cmd._is_terminal({"status": "artifact-collection"}) is False
    assert runs_cmd._is_terminal({"status": "result-processing"}) is False


def test_legacy_handoff_failed_status_remains_terminal():
    assert runs_cmd._is_terminal({"status": "handoff-failed"}) is True


def test_approval_wait_uses_previous_reader_nonterminal_status():
    meta = {
        "status": "running",
        "approval_reference": {
            "decision_state": "pending",
        },
    }

    assert meta["status"] in run_resume._NONTERMINAL_RUN_STATUSES
    assert runs_cmd._is_terminal(meta) is False
    assert runs_cmd._is_intentional_approval_pause(meta) is True
    assert runs_cmd._approval_needs_reconciliation(meta) is False


def test_consumed_approval_marker_is_nonterminal_and_needs_reconciliation():
    meta = {
        "status": "running",
        "approval_reference": {
            "decision_state": "consumed",
        },
    }

    assert runs_cmd._is_terminal(meta) is False
    assert runs_cmd._is_intentional_approval_pause(meta) is False
    assert runs_cmd._approval_needs_reconciliation(meta) is True


def test_true_terminal_status_remains_terminal_with_approval_reference():
    assert (
        runs_cmd._is_terminal(
            {
                "status": "ok",
                "finished_at": "2026-07-30T18:00:00+00:00",
                "approval_reference": {"decision_state": "consumed"},
            }
        )
        is True
    )


def test_previous_resume_reader_refuses_new_approval_wait_before_provider(tmp_path, monkeypatch, capsys):
    approval = _daily_approval()
    run_dir, _ = _approval_run(tmp_path, source="daily", record=approval)

    monkeypatch.setattr(
        run_resume.codex_appserver,
        "AppServer",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("previous reader must not reach provider")),
    )

    assert run_resume.resume(run_dir) == 2
    assert "run is not terminal" in capsys.readouterr().err


def _approval_run(tmp_path, *, source, record):
    run_dir = tmp_path / ".brigade" / "runs" / "run-approval-1"
    run_dir.mkdir(parents=True)
    reference = runs_cmd._approval_reference_from_record(source, record)
    _write_json(
        run_dir / "run.json",
        {
            "status": "running",
            "cwd": str(tmp_path),
            "lock_workspace": str(tmp_path),
            "approval_reference": {**reference, "decision_state": "pending"},
        },
    )
    return run_dir, reference


def _daily_approval(*, status="approved", consumed_run_id=None):
    return {
        "approval_id": "daily-approval-1",
        "status": status,
        "reviewed_at": "2026-07-30T18:00:00+00:00",
        "selected_action_id": "daily-action-1",
        "source_fingerprint": "daily-source-fingerprint",
        "config_fingerprint": "daily-config-fingerprint",
        "evidence_refs": ["receipt:daily-1"],
        "consumed_run_id": consumed_run_id,
        "consumed_at": "2026-07-30T18:01:00+00:00" if consumed_run_id else None,
    }


def _patch_daily_store(monkeypatch, approval, *, blockers=None):
    monkeypatch.setattr(daily_cmd.approvals, "_find_approval", lambda target, approval_id: approval)
    monkeypatch.setattr(daily_cmd.config, "_load_config", lambda target: ({}, []))
    monkeypatch.setattr(
        daily_cmd.approvals,
        "_approval_blockers",
        lambda target, record, config: list(blockers or []),
    )
    monkeypatch.setattr(
        daily_cmd.approvals,
        "_redeemed_reconciliation_blockers",
        lambda record, config: list(blockers or []),
    )


def _patch_tool_store(monkeypatch, call, *, blockers=None):
    monkeypatch.setattr(
        tools_cmd.calls,
        "_resolve_call",
        lambda target, call_id: (call, [call], None),
    )
    monkeypatch.setattr(
        tools_cmd.calls,
        "_call_run_blockers",
        lambda target, record: list(blockers or []),
    )


def _mark_redeemed(record, run_id):
    redeemed_at = "2026-07-30T18:01:00+00:00"
    record["approval_claim"] = {
        "run_id": run_id,
        "state": "redeemed",
        "reserved_at": "2026-07-30T18:00:30+00:00",
        "token_fingerprint": "a" * 64,
        "redeemed_at": redeemed_at,
    }
    return redeemed_at


def _mark_daily_action_completed(record, target, owner_run_id):
    daily_run_id = "daily-action-run-1"
    completed_at = "2026-07-30T18:02:00+00:00"
    record["approval_action_receipt"] = {
        "state": "completed",
        "owner_run_id": owner_run_id,
        "daily_run_id": daily_run_id,
        "action_id": record["selected_action_id"],
        "source_fingerprint": record["source_fingerprint"],
        "completed_at": completed_at,
    }
    receipt_path = target / ".brigade" / "daily" / "runs" / daily_run_id / "run.json"
    receipt_path.parent.mkdir(parents=True)
    _write_json(
        receipt_path,
        {
            "status": "completed",
            "run_id": daily_run_id,
            "approval_id": record["approval_id"],
            "selected_action_id": record["selected_action_id"],
            "selected_action": {"source_fingerprint": record["source_fingerprint"]},
            "completed_at": completed_at,
        },
    )


def test_approval_pause_marker_binds_exact_event_and_redacts_artifact(tmp_path, monkeypatch):
    approval = _daily_approval(status="pending")
    marker = "m" * 43
    run_dir = tmp_path / ".brigade" / "runs" / "marker-run"
    daily_cmd.approvals._write_approval(tmp_path, approval)
    monkeypatch.setenv(runs_cmd._APPROVAL_CORRELATION_ENV, marker)

    with runguard.run_lock(tmp_path, run_dir=run_dir):
        stored = daily_cmd.approvals._find_approval(tmp_path, approval["approval_id"])
        assert stored is not None
        assert runs_cmd._attach_approval_pause_request(tmp_path, "daily", stored) is True
        daily_cmd.approvals._write_approval(tmp_path, stored)
        writer = aboyeur._worker_event_writer(
            run_dir / "events",
            "requester",
            workspace=tmp_path,
            correlation_marker=marker,
        )
        assert writer is not None
        writer(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "thread-requester",
                    "turnId": "turn-requester",
                    "item": {
                        "id": "command-1",
                        "type": "commandExecution",
                        "command": f"{runs_cmd._APPROVAL_CORRELATION_ENV}={marker} brigade daily run",
                    },
                },
            }
        )
        reference = runs_cmd._approval_pause_reference_for_owned_run(tmp_path, run_dir)
        assert reference is not None
        assert reference.requester_worker == "requester"
        assert reference.requester_thread_id == "thread-requester"
        assert reference.requester_turn_id == "turn-requester"
        with pytest.raises(runs_cmd.ApprovalResumeError, match="already bound"):
            runs_cmd._bind_approval_pause_request(
                tmp_path,
                correlation_marker=marker,
                worker="requester",
                thread_id="thread-requester",
                turn_id="turn-requester",
            )
        with pytest.raises(runs_cmd.ApprovalResumeError, match="exactly one request"):
            runs_cmd._bind_approval_pause_request(
                tmp_path,
                correlation_marker="x" * 43,
                worker="other",
                thread_id="thread-other",
                turn_id="turn-other",
            )

    event_text = (run_dir / "events" / "requester.jsonl").read_text()
    approval_text = daily_cmd.approvals._approval_path(tmp_path, approval["approval_id"]).read_text()
    assert marker not in event_text
    assert marker not in approval_text
    assert "[redacted]" in event_text


@pytest.mark.parametrize("source", ["daily", "tool"])
def test_approval_pause_binding_rejects_request_replaced_before_source_lock(
    tmp_path,
    monkeypatch,
    source,
):
    marker = "n" * 43
    run_dir = tmp_path / ".brigade" / "runs" / f"{source}-replacement-run"
    record = _daily_approval(status="pending") if source == "daily" else _tool_call()
    monkeypatch.setenv(runs_cmd._APPROVAL_CORRELATION_ENV, marker)

    with runguard.run_lock(tmp_path, run_dir=run_dir):
        assert runs_cmd._attach_approval_pause_request(tmp_path, source, record) is True
        request = record[runs_cmd._APPROVAL_PAUSE_REQUEST_FIELD]
        original_reference = dict(request["approval_reference"])
        replaced = False

        @contextmanager
        def replace_before_locked_reread(*args, **kwargs):
            nonlocal replaced
            assert replaced is False
            request["approval_reference"] = {
                **original_reference,
                "fingerprint": "replacement-fingerprint",
            }
            replaced = True
            yield

        if source == "daily":
            monkeypatch.setattr(daily_cmd.approvals, "_read_approvals", lambda target: ([record], []))
            monkeypatch.setattr(daily_cmd.approvals, "_approval_store_lock", replace_before_locked_reread)
            monkeypatch.setattr(daily_cmd.approvals, "_find_approval", lambda target, approval_id: record)
            monkeypatch.setattr(
                daily_cmd.approvals,
                "_write_approval_unlocked",
                lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("replaced request was bound")),
            )
            monkeypatch.setattr(tools_cmd.calls, "_read_calls", lambda target: [])
        else:
            monkeypatch.setattr(daily_cmd.approvals, "_read_approvals", lambda target: ([], []))
            monkeypatch.setattr(tools_cmd.calls, "_read_calls", lambda target: [record])
            monkeypatch.setattr(tools_cmd.calls, "_calls_store_lock", replace_before_locked_reread)
            monkeypatch.setattr(
                tools_cmd.calls,
                "_resolve_call",
                lambda target, approval_id: (record, [record], None),
            )
            monkeypatch.setattr(
                tools_cmd.calls,
                "_write_calls_unlocked",
                lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("replaced request was bound")),
            )

        with pytest.raises(runs_cmd.ApprovalResumeError, match="reference changed"):
            runs_cmd._bind_approval_pause_request(
                tmp_path,
                correlation_marker=marker,
                worker="requester",
                thread_id="replacement-thread",
                turn_id="replacement-turn",
            )

    assert replaced is True
    assert request["requester_worker"] is None
    assert request["requester_thread_id"] is None
    assert request["requester_turn_id"] is None


def test_resume_paused_daily_consumes_once_under_fresh_lock(tmp_path, monkeypatch):
    approval = _daily_approval()
    run_dir, _ = _approval_run(tmp_path, source="daily", record=approval)
    observed = []

    _patch_daily_store(monkeypatch, approval)

    original_reserve = daily_cmd.approvals.reserve_for_run

    def reserve(target, approval_id, reference, run_id, token_fingerprint):
        observed.append(("reserve", runguard.run_lock_state(tmp_path, run_dir), run_id))
        return original_reserve(target, approval_id, reference, run_id, token_fingerprint)

    monkeypatch.setattr(daily_cmd.approvals, "reserve_for_run", reserve)
    monkeypatch.setattr(
        run_lifecycle,
        "record_lifecycle_event",
        lambda *args, event_type, **kwargs: observed.append(("event", event_type)),
    )

    def continue_run(path, *, approval_resume, approval_token):
        assert isinstance(approval_token, str)
        observed.append(("continue", runguard.run_lock_state(tmp_path, run_dir), approval_resume))
        return 0

    monkeypatch.setattr(run_resume, "_resume_locked", continue_run)

    assert runs_cmd.resume(run_dir) == 0
    assert observed == [
        ("reserve", "live", run_dir.name),
        ("event", "approval.granted"),
        ("continue", "live", True),
    ]
    assert runguard.run_lock_state(tmp_path, run_dir) == "absent"


def test_daily_reservation_rotation_refuses_foreign_run(tmp_path, monkeypatch):
    approval = _daily_approval()
    reference = runs_cmd._approval_reference_from_record("daily", approval)
    approval["approval_claim"] = {
        "run_id": "other-run",
        "state": "reserved",
        "reserved_at": "2026-07-30T18:01:00+00:00",
        "token_fingerprint": "a" * 64,
        "redeemed_at": None,
    }
    _patch_daily_store(monkeypatch, approval)

    with pytest.raises(daily_cmd.approvals.ApprovalClaimError, match="reserved by other-run"):
        daily_cmd.approvals.reserve_for_run(
            tmp_path,
            approval["approval_id"],
            reference,
            "requesting-run",
            "b" * 64,
        )

    assert approval["approval_claim"]["run_id"] == "other-run"
    assert approval["approval_claim"]["token_fingerprint"] == "a" * 64


def test_resume_paused_daily_legacy_consumed_store_fails_closed(tmp_path, monkeypatch, capsys):
    approval = _daily_approval(status="consumed", consumed_run_id="run-approval-1")
    run_dir, _ = _approval_run(tmp_path, source="daily", record=approval)
    events = []

    _patch_daily_store(monkeypatch, approval)
    monkeypatch.setattr(
        run_lifecycle,
        "record_lifecycle_event",
        lambda *args, event_type, **kwargs: events.append(event_type),
    )
    monkeypatch.setattr(
        run_resume,
        "_resume_locked",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("legacy claim reached provider")),
    )

    assert runs_cmd.resume(run_dir) == 2
    assert events == []
    assert "daily approval is consumed" in capsys.readouterr().err


def test_resume_paused_legacy_consumed_store_does_not_rewrite_snapshot(tmp_path, monkeypatch):
    approval = _daily_approval(status="consumed", consumed_run_id="run-approval-1")
    run_dir, _ = _approval_run(tmp_path, source="daily", record=approval)
    continued = []

    _patch_daily_store(monkeypatch, approval)
    monkeypatch.setattr(run_lifecycle, "record_lifecycle_event", lambda *args, **kwargs: None)
    before = (run_dir / "run.json").read_bytes()
    monkeypatch.setattr(run_resume, "_resume_locked", lambda *args, **kwargs: continued.append(args) or 0)

    assert runs_cmd.resume(run_dir) == 2
    assert continued == []
    assert (run_dir / "run.json").read_bytes() == before


def test_consumed_daily_reconciliation_revalidates_live_evidence(tmp_path, monkeypatch, capsys):
    approval = _daily_approval(status="consumed", consumed_run_id="run-approval-1")
    run_dir, _ = _approval_run(tmp_path, source="daily", record=approval)
    checked = []

    monkeypatch.setattr(daily_cmd.approvals, "_find_approval", lambda target, approval_id: approval)
    monkeypatch.setattr(daily_cmd.config, "_load_config", lambda target: ({}, []))

    def blockers(target, record, config):
        checked.append((record["status"], record.get("consumed_run_id")))
        return ["selected action fingerprint changed since approval"]

    monkeypatch.setattr(daily_cmd.approvals, "_approval_blockers", blockers)
    monkeypatch.setattr(
        run_resume,
        "_resume_locked",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("stale evidence must not reach provider")),
    )

    assert runs_cmd.resume(run_dir) == 2
    assert checked == [("approved", None)]
    assert "selected action fingerprint changed" in capsys.readouterr().err


@pytest.mark.parametrize("status", ["rejected", "held"])
def test_resume_paused_records_terminal_daily_decision_without_consuming(tmp_path, monkeypatch, capsys, status):
    approval = _daily_approval(status=status)
    run_dir, _ = _approval_run(tmp_path, source="daily", record=approval)
    events = []

    _patch_daily_store(monkeypatch, approval)
    monkeypatch.setattr(
        daily_cmd.approvals,
        "_consume_approval",
        lambda *args: (_ for _ in ()).throw(AssertionError("terminal decision must not consume")),
    )
    monkeypatch.setattr(
        run_lifecycle,
        "record_lifecycle_event",
        lambda *args, event_type, **kwargs: events.append(event_type),
    )
    monkeypatch.setattr(runs_cmd, "_refresh_paused_snapshot", lambda *args, **kwargs: None)

    assert runs_cmd.resume(run_dir) == 2
    assert events == [f"approval.{status}"]
    assert f"approval is {status}" in capsys.readouterr().err


def test_held_marker_stays_approval_aware_and_resumes_after_store_grant(tmp_path, monkeypatch, capsys):
    approval = _daily_approval(status="held")
    run_dir, _ = _approval_run(tmp_path, source="daily", record=approval)
    continued = []
    claims = []

    _patch_daily_store(monkeypatch, approval)
    monkeypatch.setattr(run_lifecycle, "record_lifecycle_event", lambda *args, **kwargs: None)
    original_reserve = daily_cmd.approvals.reserve_for_run

    def reserve(target, approval_id, reference, run_id, token_fingerprint):
        claims.append(run_id)
        return original_reserve(target, approval_id, reference, run_id, token_fingerprint)

    monkeypatch.setattr(daily_cmd.approvals, "reserve_for_run", reserve)
    monkeypatch.setattr(
        run_resume,
        "_resume_locked",
        lambda path, *, approval_resume, approval_token: continued.append((path, approval_resume)) or 0,
    )

    assert runs_cmd.resume(run_dir) == 2
    assert "approval is held" in capsys.readouterr().err
    held = json.loads((run_dir / "run.json").read_text())
    assert held["approval_reference"]["decision_state"] == "held"
    assert runs_cmd._approval_resume_state(held) == "held"

    approval["status"] = "approved"
    approval["reviewed_at"] = "2026-07-30T18:05:00+00:00"
    assert runs_cmd.resume(run_dir) == 0
    assert continued == [(run_dir, True)]
    assert claims == [run_dir.name]
    assert approval["status"] == "approved"
    assert approval["approval_claim"]["state"] == "reserved"
    assert approval["approval_claim"]["run_id"] == run_dir.name


def test_rejected_marker_stays_approval_aware_and_never_reaches_provider(tmp_path, monkeypatch, capsys):
    approval = _daily_approval(status="rejected")
    run_dir, _ = _approval_run(tmp_path, source="daily", record=approval)

    _patch_daily_store(monkeypatch, approval)
    monkeypatch.setattr(run_lifecycle, "record_lifecycle_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        run_resume,
        "_resume_locked",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("rejected approval reached provider")),
    )

    assert runs_cmd.resume(run_dir) == 2
    assert "approval is rejected" in capsys.readouterr().err
    rejected = json.loads((run_dir / "run.json").read_text())
    assert rejected["approval_reference"]["decision_state"] == "rejected"
    assert runs_cmd._approval_resume_state(rejected) == "rejected"

    assert runs_cmd.resume(run_dir) == 2
    assert "approval is rejected" in capsys.readouterr().err


def test_resume_paused_refuses_stale_daily_reference_before_consuming(tmp_path, monkeypatch, capsys):
    approval = _daily_approval()
    run_dir, reference = _approval_run(tmp_path, source="daily", record=approval)
    reference["evidence_fingerprint"] = "stale"
    meta = json.loads((run_dir / "run.json").read_text())
    meta["approval_reference"] = {**reference, "decision_state": "pending"}
    _write_json(run_dir / "run.json", meta)

    _patch_daily_store(monkeypatch, approval)
    monkeypatch.setattr(
        daily_cmd.approvals,
        "_consume_approval",
        lambda *args: (_ for _ in ()).throw(AssertionError("stale approval must not consume")),
    )

    assert runs_cmd.resume(run_dir) == 2
    assert "approval fingerprints changed" in capsys.readouterr().err


def _tool_call():
    call = {
        "id": "call-approval-1",
        "status": "approved",
        "reviewed_at": "2026-07-30T18:00:00+00:00",
        "tool_id": "fake-tool",
        "source_fingerprint": "tool-source-fingerprint",
        "contract_fingerprint": "tool-contract-fingerprint",
        "call_fingerprint": "tool-call-fingerprint",
        "approval_fingerprint": "approved-fingerprint",
    }
    return call


def test_resume_paused_tool_consumes_existing_call_record_once(tmp_path, monkeypatch):
    call = _tool_call()
    run_dir, _ = _approval_run(tmp_path, source="tool", record=call)
    writes = []

    monkeypatch.setattr(tools_cmd.calls, "_resolve_call", lambda target, call_id: (call, [call], None))
    monkeypatch.setattr(tools_cmd.calls, "_call_run_blockers", lambda target, record: [])
    monkeypatch.setattr(
        tools_cmd.calls,
        "_write_calls_unlocked",
        lambda target, calls: writes.append([dict(item) for item in calls]),
    )
    monkeypatch.setattr(
        run_lifecycle,
        "record_lifecycle_event",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        run_resume,
        "_resume_locked",
        lambda path, *, approval_resume, approval_token: 0,
    )

    assert runs_cmd.resume(run_dir) == 0
    assert len(writes) == 1
    assert writes[0][0]["status"] == "approved"
    assert writes[0][0].get("run_id") is None
    assert writes[0][0]["approval_claim"]["state"] == "reserved"
    assert writes[0][0]["approval_claim"]["run_id"] == run_dir.name


def test_tool_reservation_rotation_refuses_foreign_run(tmp_path, monkeypatch):
    call = _tool_call()
    reference = runs_cmd._approval_reference_from_record("tool", call)
    call["approval_claim"] = {
        "run_id": "other-run",
        "state": "reserved",
        "reserved_at": "2026-07-30T18:01:00+00:00",
        "token_fingerprint": "a" * 64,
        "redeemed_at": None,
    }
    writes = []
    monkeypatch.setattr(tools_cmd.calls, "_resolve_call", lambda target, call_id: (call, [call], None))
    monkeypatch.setattr(tools_cmd.calls, "_call_run_blockers", lambda target, record: [])
    monkeypatch.setattr(
        tools_cmd.calls,
        "_write_calls_unlocked",
        lambda target, calls: writes.append([dict(item) for item in calls]),
    )

    with pytest.raises(tools_cmd.calls.CallClaimError, match="reserved by other-run"):
        tools_cmd.calls.reserve_for_run(
            tmp_path,
            call["id"],
            reference,
            "requesting-run",
            "b" * 64,
        )

    assert writes == []
    assert call["approval_claim"]["run_id"] == "other-run"
    assert call["approval_claim"]["token_fingerprint"] == "a" * 64


def test_consumed_tool_reconciliation_revalidates_live_contract(tmp_path, monkeypatch, capsys):
    call = _tool_call()
    call.update({"status": "running", "run_id": "run-approval-1"})
    run_dir, _ = _approval_run(tmp_path, source="tool", record=call)
    checked = []

    monkeypatch.setattr(tools_cmd.calls, "_resolve_call", lambda target, call_id: (call, [call], None))

    def blockers(target, record):
        checked.append((record["status"], record.get("run_id")))
        return ["contract fingerprint is stale"]

    monkeypatch.setattr(tools_cmd.calls, "_call_run_blockers", blockers)
    monkeypatch.setattr(
        run_resume,
        "_resume_locked",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("stale contract must not reach provider")),
    )

    assert runs_cmd.resume(run_dir) == 2
    assert checked == [("approved", None)]
    assert "contract fingerprint is stale" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("source", "status"),
    [
        ("daily", "rejected"),
        ("daily", "held"),
        ("tool", "rejected"),
        ("tool", "held"),
    ],
)
def test_redeemed_retry_refuses_changed_source_decision(
    tmp_path,
    monkeypatch,
    capsys,
    source,
    status,
):
    record = _daily_approval(status=status) if source == "daily" else _tool_call()
    if source == "tool":
        record["status"] = status
    run_dir, _ = _approval_run(tmp_path, source=source, record=record)
    _mark_redeemed(record, run_dir.name)
    events = []

    if source == "daily":
        _patch_daily_store(monkeypatch, record)
    else:
        _patch_tool_store(monkeypatch, record)
    monkeypatch.setattr(
        run_lifecycle,
        "record_lifecycle_event",
        lambda *args, event_type, **kwargs: events.append(event_type),
    )

    assert runs_cmd.resume(run_dir) == 2
    assert events == []
    assert status in capsys.readouterr().err


@pytest.mark.parametrize("source", ["daily", "tool"])
def test_redeemed_retry_revalidates_current_blockers(tmp_path, monkeypatch, capsys, source):
    if source == "daily":
        record = _daily_approval(status="consumed", consumed_run_id="run-approval-1")
    else:
        record = _tool_call()
        record["status"] = "completed"
    run_dir, _ = _approval_run(tmp_path, source=source, record=record)
    redeemed_at = _mark_redeemed(record, run_dir.name)
    if source == "daily":
        record["consumed_at"] = redeemed_at
        _mark_daily_action_completed(record, tmp_path, run_dir.name)
        _patch_daily_store(monkeypatch, record, blockers=["daily config changed since approval"])
    else:
        _patch_tool_store(monkeypatch, record, blockers=["contract fingerprint is stale"])
    events = []
    monkeypatch.setattr(
        run_lifecycle,
        "record_lifecycle_event",
        lambda *args, event_type, **kwargs: events.append(event_type),
    )

    assert runs_cmd.resume(run_dir) == 2
    assert events == []
    assert "stale or blocked" in capsys.readouterr().err


def test_redeemed_daily_retry_requires_completed_action_receipt(tmp_path, monkeypatch, capsys):
    record = _daily_approval(status="consumed", consumed_run_id="run-approval-1")
    run_dir, _ = _approval_run(tmp_path, source="daily", record=record)
    redeemed_at = _mark_redeemed(record, run_dir.name)
    record["consumed_at"] = redeemed_at
    _patch_daily_store(monkeypatch, record)
    events = []
    monkeypatch.setattr(
        run_lifecycle,
        "record_lifecycle_event",
        lambda *args, event_type, **kwargs: events.append(event_type),
    )

    assert runs_cmd.resume(run_dir) == 2
    assert events == []
    assert "completed action receipt" in capsys.readouterr().err


def test_redeemed_daily_retry_refuses_tampered_action_receipt(tmp_path, monkeypatch, capsys):
    record = _daily_approval(status="consumed", consumed_run_id="run-approval-1")
    run_dir, _ = _approval_run(tmp_path, source="daily", record=record)
    redeemed_at = _mark_redeemed(record, run_dir.name)
    record["consumed_at"] = redeemed_at
    _mark_daily_action_completed(record, tmp_path, run_dir.name)
    daily_run_id = record["approval_action_receipt"]["daily_run_id"]
    action_receipt_path = tmp_path / ".brigade" / "daily" / "runs" / daily_run_id / "run.json"
    action_receipt = json.loads(action_receipt_path.read_text())
    action_receipt["status"] = "failed"
    _write_json(action_receipt_path, action_receipt)
    _patch_daily_store(monkeypatch, record)
    events = []
    monkeypatch.setattr(
        run_lifecycle,
        "record_lifecycle_event",
        lambda *args, event_type, **kwargs: events.append(event_type),
    )

    assert runs_cmd.resume(run_dir) == 2
    assert events == []
    assert "no longer matches its run" in capsys.readouterr().err


@pytest.mark.parametrize("source", ["daily", "tool"])
def test_redeemed_reconciliation_holds_source_lock_through_journal_commit(
    tmp_path,
    monkeypatch,
    capsys,
    source,
):
    if source == "daily":
        record = _daily_approval(status="consumed", consumed_run_id="run-approval-1")
    else:
        record = _tool_call()
        record["status"] = "completed"
    run_dir, _ = _approval_run(tmp_path, source=source, record=record)
    run_meta = json.loads((run_dir / "run.json").read_text())
    run_meta["lifecycle_journal_requested"] = True
    _write_json(run_dir / "run.json", run_meta)
    redeemed_at = _mark_redeemed(record, run_dir.name)
    if source == "daily":
        record["consumed_at"] = redeemed_at
        _mark_daily_action_completed(record, tmp_path, run_dir.name)
        daily_cmd.approvals._write_approval(tmp_path, record)
        monkeypatch.setattr(
            daily_cmd.approvals,
            "_redeemed_reconciliation_blockers",
            lambda approval, config: [],
        )
    else:
        tools_cmd.calls._write_calls(tmp_path, [record])
        monkeypatch.setattr(tools_cmd.calls, "_call_run_blockers", lambda target, call: [])

    append_entered = threading.Event()
    attempted_before_snapshot = threading.Event()
    lock_contended = threading.Event()
    successful_before_snapshot = threading.Event()
    review_finished = threading.Event()
    review_result = []
    events = []
    snapshot_refreshing = threading.Event()
    refresh_started = threading.Event()
    snapshot_done = threading.Event()
    snapshot_classification = threading.Lock()
    if source == "daily":
        source_store_lock_path = daily_cmd.approvals._approval_lock_path(tmp_path, record["approval_id"]).resolve()
    else:
        source_store_lock_path = tools_cmd.calls._calls_lock_path(tmp_path).resolve()
    original_acquire_lock = runguard._acquire_lock
    original_record_lifecycle_event = run_lifecycle.record_lifecycle_event
    original_refresh_resumed_snapshot = runs_cmd._refresh_resumed_snapshot

    def instrumented_acquire_lock(path, *, run_dir=None):
        if Path(path).resolve() == source_store_lock_path:
            with snapshot_classification:
                during_refresh = snapshot_refreshing.is_set() and not snapshot_done.is_set()
                if during_refresh:
                    attempted_before_snapshot.set()
            if during_refresh:
                try:
                    ownership = original_acquire_lock(path, run_dir=run_dir)
                except runguard.RunLockError as exc:
                    if "another brigade run appears active" in str(exc):
                        with snapshot_classification:
                            lock_contended.set()
                    raise
                with snapshot_classification:
                    succeeded_during_refresh = snapshot_refreshing.is_set() and not snapshot_done.is_set()
                    if succeeded_during_refresh:
                        successful_before_snapshot.set()
                if not succeeded_during_refresh:
                    return ownership
                runguard._release_lock(path, ownership)
                raise AssertionError(
                    "source-store lock acquired before journal commit completed: "
                    f"events={events!r} refresh_started={refresh_started.is_set()}"
                )
        return original_acquire_lock(path, run_dir=run_dir)

    def review_source():
        thread_sync.wait_for_event(append_entered, description="journal append entered")
        if thread_sync.current_thread_cancelled():
            return
        if source == "daily":
            rc = daily_cmd.approvals_hold(
                target=tmp_path,
                approval_id=record["approval_id"],
                reason="concurrent hold",
            )
        else:
            rc = tools_cmd.call_reject(
                target=tmp_path,
                call_id=record["id"],
                reason="concurrent rejection",
            )
        review_result.append(rc)
        review_finished.set()

    def append_fact(*args, event_type, **kwargs):
        if not snapshot_refreshing.is_set():
            snapshot_refreshing.set()
            append_entered.set()
            run_lifecycle.prepare_lifecycle_journal(run_dir, workspace=tmp_path)
            thread_sync.wait_for_predicate(
                lambda: lock_contended.is_set() or successful_before_snapshot.is_set() or not reviewer.is_alive(),
                description="reviewer source-store lock attempt outcome",
            )
            if not reviewer.is_alive() and not attempted_before_snapshot.is_set():
                thread_sync.join_thread(reviewer, description="reviewer failed before source-lock attempt")
            assert attempted_before_snapshot.is_set()
            if successful_before_snapshot.is_set():
                raise AssertionError("source-store lock released before journal commit")
            assert lock_contended.is_set()
            assert not review_finished.is_set()
        original_record_lifecycle_event(*args, event_type=event_type, **kwargs)
        events.append(event_type)

    def refresh_resumed_snapshot(*args, **kwargs):
        refresh_started.set()
        result = original_refresh_resumed_snapshot(*args, **kwargs)
        with snapshot_classification:
            snapshot_done.set()
        return result

    monkeypatch.setattr(runguard, "_acquire_lock", instrumented_acquire_lock)
    monkeypatch.setattr(run_lifecycle, "record_lifecycle_event", append_fact)
    monkeypatch.setattr(runs_cmd, "_refresh_resumed_snapshot", refresh_resumed_snapshot)
    reviewer = thread_sync.start_thread(review_source)
    try:
        assert runs_cmd.resume(run_dir) == 0
        thread_sync.join_thread(reviewer, description="reviewer thread finished")
        assert snapshot_done.is_set()
        assert attempted_before_snapshot.is_set()
        assert not successful_before_snapshot.is_set()
        assert review_result == [1]
        assert events == ["approval.consumed", "run.resumed"]
        if source == "daily":
            stored = daily_cmd.approvals._find_approval(tmp_path, record["approval_id"])
            assert stored is not None
            assert stored["status"] == "consumed"
        else:
            stored, _calls, error = tools_cmd.calls._resolve_call(tmp_path, record["id"])
            assert error is None
            assert stored is not None
            assert stored["status"] == "completed"
        capsys.readouterr()
    except BaseException as primary_error:
        append_entered.set()
        thread_sync.cancel_thread(reviewer)
        try:
            thread_sync.join_thread(reviewer, description="reviewer cleanup", hard_timeout=1.0)
        except BaseException as cleanup_error:
            thread_sync.note_cleanup_failure(primary_error, cleanup_error)
        raise


@pytest.mark.parametrize("source", ["daily", "tool"])
def test_redeemed_retry_refuses_claim_for_another_run(tmp_path, monkeypatch, capsys, source):
    if source == "daily":
        record = _daily_approval(status="consumed", consumed_run_id="other-run")
    else:
        record = _tool_call()
        record["status"] = "completed"
    run_dir, _ = _approval_run(tmp_path, source=source, record=record)
    redeemed_at = _mark_redeemed(record, "other-run")
    if source == "daily":
        record["consumed_at"] = redeemed_at
        _patch_daily_store(monkeypatch, record)
    else:
        _patch_tool_store(monkeypatch, record)
    events = []
    monkeypatch.setattr(
        run_lifecycle,
        "record_lifecycle_event",
        lambda *args, event_type, **kwargs: events.append(event_type),
    )

    assert runs_cmd.resume(run_dir) == 2
    assert events == []
    assert "belongs to other-run" in capsys.readouterr().err


def test_redeemed_retry_refuses_legacy_tool_consumption_without_claim(tmp_path, monkeypatch, capsys):
    call = _tool_call()
    call.update({"status": "running", "run_id": "run-approval-1"})
    run_dir, _ = _approval_run(tmp_path, source="tool", record=call)
    _patch_tool_store(monkeypatch, call)
    events = []
    monkeypatch.setattr(
        run_lifecycle,
        "record_lifecycle_event",
        lambda *args, event_type, **kwargs: events.append(event_type),
    )

    assert runs_cmd.resume(run_dir) == 2
    assert events == []
    assert "tool approval is running" in capsys.readouterr().err


def test_resume_nonapproval_run_uses_legacy_fallback(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_json(run_dir / "run.json", {"status": "failed"})
    seen = []
    monkeypatch.setattr(run_resume, "resume", lambda path: seen.append(path) or 0)

    assert runs_cmd.resume(run_dir) == 0
    assert seen == [run_dir]


def test_watch_reports_unrecoverable_artifact_collection_lock_error(tmp_path, monkeypatch, capsys):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_json(
        run_dir / "run.json",
        {
            "status": "artifact-collection",
            "cwd": str(tmp_path),
            "lock_workspace": str(tmp_path),
            "started_at": "2026-07-17T00:00:00Z",
        },
    )
    monkeypatch.setattr(
        runguard,
        "recover_stale_run",
        lambda *args, **kwargs: (_ for _ in ()).throw(runguard.RunLockError("run lock has no owner metadata")),
    )

    assert runs_cmd.watch(run_dir, cwd=tmp_path, interval=0) == 2
    assert "artifact-collection recovery failed: run lock has no owner metadata" in capsys.readouterr().err


def test_watch_reports_artifact_collection_without_matching_lock(tmp_path, capsys):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_json(
        run_dir / "run.json",
        {
            "status": "artifact-collection",
            "cwd": str(tmp_path),
            "lock_workspace": str(tmp_path),
            "started_at": "2026-07-17T00:00:00Z",
        },
    )

    assert runs_cmd.watch(run_dir, cwd=tmp_path, interval=0) == 2
    assert "artifact-collection run has no matching recoverable lock" in capsys.readouterr().err


def test_watch_handles_artifact_collection_completion_race(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    run_path = run_dir / "run.json"
    payload = {
        "status": "artifact-collection",
        "cwd": str(tmp_path),
        "lock_workspace": str(tmp_path),
        "started_at": "2026-07-17T00:00:00Z",
    }
    _write_json(run_path, payload)

    def finish_without_recovery(*args, **kwargs):
        payload["status"] = "ok"
        payload["finished_at"] = "2026-07-17T00:00:01Z"
        _write_json(run_path, payload)
        return False

    monkeypatch.setattr(runguard, "recover_stale_run", finish_without_recovery)

    assert runs_cmd.watch(run_dir, cwd=tmp_path, interval=0) == 0


def test_watch_handles_artifact_collection_lock_vanishing_during_probe(tmp_path, monkeypatch, capsys):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    run_path = run_dir / "run.json"
    payload = {
        "status": "artifact-collection",
        "cwd": str(tmp_path),
        "lock_workspace": str(tmp_path),
        "started_at": "2026-07-17T00:00:00Z",
    }
    _write_json(run_path, payload)
    lock_path = runguard.lock_path(tmp_path)
    lock_path.mkdir(parents=True)
    original_lstat = Path.lstat
    vanished = False

    def finish_after_successful_lock_lstat(self, *args, **kwargs):
        nonlocal vanished
        result = original_lstat(self, *args, **kwargs)
        if self == lock_path and not vanished:
            shutil.rmtree(lock_path)
            payload["status"] = "ok"
            payload["finished_at"] = "2026-07-17T00:00:01Z"
            _write_json(run_path, payload)
            vanished = True
        return result

    monkeypatch.setattr(Path, "lstat", finish_after_successful_lock_lstat)

    assert runs_cmd.watch(run_dir, cwd=tmp_path, interval=0) == 0
    assert vanished is True
    assert "artifact-collection recovery failed" not in capsys.readouterr().err


def test_watch_stops_on_pending_approval_with_no_lock(tmp_path, capsys):
    approval = _daily_approval(status="pending")
    run_dir, _ = _approval_run(tmp_path, source="daily", record=approval)

    assert runguard.run_lock_state(tmp_path, run_dir) == "absent"
    assert runs_cmd.watch(run_dir, cwd=tmp_path, interval=0) == 1
    assert "summary: running" in capsys.readouterr().out


def test_watch_waits_for_consumed_approval_while_provider_lock_is_live(tmp_path, monkeypatch):
    approval = _daily_approval(status="consumed", consumed_run_id="run-approval-1")
    run_dir, _ = _approval_run(tmp_path, source="daily", record=approval)
    run_path = run_dir / "run.json"
    meta = json.loads(run_path.read_text())
    meta["approval_reference"]["decision_state"] = "consumed"
    meta["approval_reference"]["consuming_run_id"] = run_dir.name
    _write_json(run_path, meta)

    watch_holding_on_live_lock = threading.Event()
    provider_finish_gate = thread_sync.ThreadGate()

    def coordinating_sleep(interval):
        run_meta = json.loads(run_path.read_text())
        if runs_cmd._approval_needs_reconciliation(run_meta) and runguard.run_lock_state(tmp_path, run_dir) == "live":
            watch_holding_on_live_lock.set()
            provider_finish_gate.wait_open(description="provider finished run under live lock")
        system_time.sleep(interval)

    class RunsTimeProxy:
        def sleep(self, interval):
            coordinating_sleep(interval)

        def __getattr__(self, name):
            return getattr(system_time, name)

    monkeypatch.setattr(runs_cmd, "time", RunsTimeProxy())

    def finish_provider():
        thread_sync.wait_for_event(
            watch_holding_on_live_lock,
            description="watch entered live-lock wait for consumed approval",
        )
        if thread_sync.current_thread_cancelled():
            return
        finished = json.loads(run_path.read_text())
        finished["status"] = "ok"
        finished["finished_at"] = "2026-07-30T18:10:00+00:00"
        _write_json(run_path, finished)
        provider_finish_gate.open()

    with runguard.run_lock(tmp_path, run_dir=run_dir):
        worker = thread_sync.start_thread(finish_provider)
        try:
            assert runs_cmd.watch(run_dir, cwd=tmp_path, interval=0.001) == 0
            thread_sync.join_thread(worker, description="provider worker")
        except BaseException as primary_error:
            watch_holding_on_live_lock.set()
            provider_finish_gate.open()
            thread_sync.cancel_thread(worker)
            try:
                thread_sync.join_thread(worker, description="provider worker cleanup", hard_timeout=1.0)
            except BaseException as cleanup_error:
                thread_sync.note_cleanup_failure(primary_error, cleanup_error)
            raise


def test_watch_consumed_approval_without_lock_requests_resume_reconciliation(tmp_path, capsys):
    approval = _daily_approval(status="consumed", consumed_run_id="run-approval-1")
    run_dir, _ = _approval_run(tmp_path, source="daily", record=approval)
    meta = json.loads((run_dir / "run.json").read_text())
    meta["approval_reference"]["decision_state"] = "consumed"
    meta["approval_reference"]["consuming_run_id"] = run_dir.name
    _write_json(run_dir / "run.json", meta)

    assert runguard.run_lock_state(tmp_path, run_dir) == "absent"
    assert runs_cmd.watch(run_dir, cwd=tmp_path, interval=0) == 2
    assert "approval resume requires reconciliation" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("pending", "approval wait is intentional"),
        ("consumed", "approval resume requires reconciliation"),
    ],
)
def test_recover_refuses_approval_control_states_without_terminalizing(tmp_path, capsys, state, expected):
    approval = _daily_approval(
        status="consumed" if state == "consumed" else "pending",
        consumed_run_id="run-approval-1" if state == "consumed" else None,
    )
    run_dir, _ = _approval_run(tmp_path, source="daily", record=approval)
    meta = json.loads((run_dir / "run.json").read_text())
    meta["approval_reference"]["decision_state"] = state
    if state == "consumed":
        meta["approval_reference"]["consuming_run_id"] = run_dir.name
    _write_json(run_dir / "run.json", meta)

    assert runs_cmd.recover(run_dir, cwd=tmp_path) == 2
    assert expected in capsys.readouterr().err
    assert json.loads((run_dir / "run.json").read_text())["status"] == "running"


def test_recover_consumed_approval_refuses_live_provider_lock(tmp_path, capsys):
    approval = _daily_approval(status="consumed", consumed_run_id="run-approval-1")
    run_dir, _ = _approval_run(tmp_path, source="daily", record=approval)
    meta = json.loads((run_dir / "run.json").read_text())
    meta["approval_reference"]["decision_state"] = "consumed"
    meta["approval_reference"]["consuming_run_id"] = run_dir.name
    _write_json(run_dir / "run.json", meta)

    with runguard.run_lock(tmp_path, run_dir=run_dir):
        assert runs_cmd.recover(run_dir, cwd=tmp_path) == 2
    assert "run owner process is still active" in capsys.readouterr().err


def _write_run_artifacts(run_dir):
    run_dir.mkdir()
    _write_json(
        run_dir / "run.json",
        {
            "task": "build feature",
            "cwd": "/repo",
            "orchestrator": "chef",
            "dry_run": False,
            "read_only": True,
            "status": "ok",
            "started_at": "2026-05-26T14:00:00Z",
            "finished_at": "2026-05-26T14:00:02Z",
            "duration_seconds": 2.0,
            "artifacts": str(run_dir),
            "handoff": str(run_dir / "handoff.md"),
        },
    )
    _write_json(
        run_dir / "roster.json",
        {
            "orchestrator": "chef",
            "max_workers": 1,
            "timeout_seconds": 180.0,
            "allow_models": ["codex"],
            "agents": {
                "chef": {"cli": "codex", "role": "plan", "timeout_seconds": 180.0},
                "coder": {"cli": "codex", "role": "code", "timeout_seconds": None},
            },
        },
    )
    _write_json(
        run_dir / "plan.json",
        {"assignments": [{"worker": "coder", "task": "implement it"}]},
    )
    _write_json(
        run_dir / "worker-results.json",
        {"results": [{"worker": "coder", "task": "implement it", "ok": True, "detail": "", "text": "done"}]},
    )
    _write_json(
        run_dir / "synthesis.json",
        {"orchestrator": "chef", "result": {"ok": True, "detail": "", "text": "final answer"}},
    )
    (run_dir / "final.txt").write_text("final answer\n")


def _write_minimal_run(run_dir, *, task, status, started_at, duration=1.0, read_only=False, dry_run=False):
    run_dir.mkdir(parents=True)
    _write_json(
        run_dir / "run.json",
        {
            "task": task,
            "cwd": "/repo",
            "orchestrator": "chef",
            "dry_run": dry_run,
            "read_only": read_only,
            "status": status,
            "started_at": started_at,
            "duration_seconds": duration,
        },
    )


def _write_lock_owner(workspace, run_dir, *, pid=99999999, owner_token="owner"):
    lock_path = workspace / ".brigade" / "run.lock"
    lock_path.mkdir(parents=True)
    (lock_path / "pid").write_text(f"{pid}\n")
    _write_json(
        lock_path / "owner.json",
        {
            "schema": "brigade.run_lock.v1",
            "owner_token": owner_token,
            "pid": pid,
            "run_dir": str(run_dir.resolve()),
            "acquired_at": "2026-07-16T00:00:00+00:00",
        },
    )
    return lock_path


def test_runs_show_prints_summary(tmp_path, capsys):
    run_dir = tmp_path / "run"
    _write_run_artifacts(run_dir)

    assert runs_cmd.show(run_dir) == 0
    out = capsys.readouterr().out
    assert f"run: {run_dir}" in out
    assert "status: ok" in out
    assert "mode: read-only" in out
    assert "duration: 2s" in out
    assert "handoff:" in out
    assert "roster:" in out
    assert "  - chef: codex (orchestrator); timeout=180s" in out
    assert "plan:" in out
    assert "  -> coder: implement it" in out
    assert "workers:" in out
    assert "  [ok] coder" in out
    assert "synthesis:" in out
    assert "  [ok] chef" in out
    assert "final:" in out
    assert "  final answer" in out
    assert "lineage:" not in out
    assert not (run_dir / "revisions").exists()


def test_runs_show_prints_child_lineage(tmp_path, capsys):
    run_dir = tmp_path / "run"
    _write_run_artifacts(run_dir)
    payload = json.loads((run_dir / "run.json").read_text())
    payload["lineage"] = {
        "kind": "child",
        "parent_run_id": "20260817-120000-parent-aaaaaa",
        "branch_point_event_id": "20260817-120000-parent-aaaaaa-000003-bbbbbbbbbbbb",
        "shared_prefix": {
            "event_sequence": 3,
            "event_digest": "b" * 64,
            "previous_digest": "a" * 64,
        },
    }
    _write_json(run_dir / "run.json", payload)

    assert runs_cmd.show(run_dir) == 0
    out = capsys.readouterr().out
    assert "lineage:" in out
    assert "kind: child" in out
    assert "parent run: 20260817-120000-parent-aaaaaa" in out
    assert "branch point: 20260817-120000-parent-aaaaaa-000003-bbbbbbbbbbbb" in out
    assert "shared prefix: seq=3 digest=bbbbbbbbbbbb" in out


def test_runs_show_json_includes_child_lineage(tmp_path, capsys):
    run_dir = tmp_path / "run"
    _write_run_artifacts(run_dir)
    payload = json.loads((run_dir / "run.json").read_text())
    payload["lineage"] = {
        "kind": "child",
        "parent_run_id": "20260817-120000-parent-aaaaaa",
        "branch_point_event_id": "20260817-120000-parent-aaaaaa-000003-bbbbbbbbbbbb",
        "shared_prefix": {
            "event_sequence": 3,
            "event_digest": "b" * 64,
            "previous_digest": "a" * 64,
        },
    }
    _write_json(run_dir / "run.json", payload)

    assert runs_cmd.show(run_dir, json_output=True) == 0
    detail = json.loads(capsys.readouterr().out)
    assert detail["schema"] == "brigade.run-detail.v1"
    assert detail["run"]["lineage"] == {
        "kind": "child",
        "parent_run_id": "20260817-120000-parent-aaaaaa",
        "branch_point_event_id": "20260817-120000-parent-aaaaaa-000003-bbbbbbbbbbbb",
        "shared_prefix": {
            "event_sequence": 3,
            "event_digest": "b" * 64,
            "previous_digest": "a" * 64,
        },
    }


def test_runs_list_json_includes_parent_run_id(tmp_path, capsys):
    runs_root = tmp_path / ".brigade" / "runs"
    runs_root.mkdir(parents=True)
    child = runs_root / "20260817-130000-child-bbbbbb"
    _write_minimal_run(child, task="branched work", status="ok", started_at="2026-08-17T13:00:00Z")
    payload = json.loads((child / "run.json").read_text())
    payload["lineage"] = {
        "kind": "child",
        "parent_run_id": "20260817-120000-parent-aaaaaa",
        "branch_point_event_id": "20260817-120000-parent-aaaaaa-000003-bbbbbbbbbbbb",
        "shared_prefix": {"event_sequence": 3, "event_digest": "b" * 64, "previous_digest": "a" * 64},
    }
    _write_json(child / "run.json", payload)

    assert cli.main(["runs", "list", "--cwd", str(tmp_path), "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["schema"] == "brigade.runs-list.v1"
    assert listed["runs"][0]["parent_run_id"] == "20260817-120000-parent-aaaaaa"


def test_runs_show_reports_missing_run_json(tmp_path, capsys):
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    assert runs_cmd.show(run_dir) == 2
    assert "run.json not found" in capsys.readouterr().err


def test_runs_show_reports_invalid_json(tmp_path, capsys):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "run.json").write_text("not json")

    assert runs_cmd.show(run_dir) == 2
    assert "run.json is not valid JSON" in capsys.readouterr().err


def test_runs_show_cli(tmp_path, capsys):
    run_dir = tmp_path / "run"
    _write_run_artifacts(run_dir)

    assert cli.main(["runs", "show", str(run_dir)]) == 0
    assert "status: ok" in capsys.readouterr().out


def _write_detail_run_artifacts(run_dir):
    _write_run_artifacts(run_dir)
    run_meta = json.loads((run_dir / "run.json").read_text())
    run_meta["code_graph_brief"] = {"attached": True, "bytes": 1200}
    run_meta["drift_impact_brief"] = {"attached": False, "bytes": 0, "pending_count": 1}
    run_meta["evidence_brief"] = {"attached": True, "bytes": 800}
    _write_json(run_dir / "run.json", run_meta)
    _write_json(
        run_dir / "worker-results.json",
        {
            "results": [
                {
                    "worker": "coder",
                    "task": "implement it",
                    "ok": True,
                    "detail": "",
                    "text": "secret transcript body",
                    "stdout_log": "logs/worker-001-coder.stdout.log",
                    "stderr_log": "logs/worker-001-coder.stderr.log",
                    "duration_seconds": 12.5,
                    "exit_code": 0,
                    "timed_out": False,
                    "requested_model": "gpt-5.5",
                    "transport": "cli",
                }
            ],
            "ground_truth": {
                "available": True,
                "cwd": "/repo",
                "verify_receipts": [
                    {
                        "run_id": "verify-1",
                        "status": "ok",
                        "started_at": "2026-05-26T14:00:01Z",
                        "duration_seconds": 3.2,
                        "commands": [{"command": "./scripts/verify", "exit_code": 0, "duration_seconds": 3.0}],
                    }
                ],
            },
        },
    )


def test_runs_show_json_emits_versioned_detail_contract(tmp_path, capsys):
    run_dir = tmp_path / "run"
    _write_detail_run_artifacts(run_dir)

    assert runs_cmd.show(run_dir, json_output=True) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "brigade.run-detail.v1"
    assert sorted(payload) == [
        "briefs",
        "plan",
        "roster",
        "run",
        "schema",
        "synthesis",
        "verification",
        "workers",
    ]
    assert payload["run"]["run_id"] == "run"
    assert payload["run"]["status"] == "ok"
    assert payload["run"]["mode"] == "read-only"
    assert payload["run"]["resume_available"] is False
    assert payload["roster"]["orchestrator"] == "chef"
    assert payload["roster"]["agents"]["chef"]["cli"] == "codex"
    assert payload["plan"]["assignments"] == [{"worker": "coder", "task": "implement it"}]
    assert payload["workers"]["results"][0]["worker"] == "coder"
    assert payload["workers"]["results"][0]["requested_model"] == "gpt-5.5"
    assert payload["synthesis"]["result"]["ok"] is True
    assert payload["verification"] == [
        {
            "run_id": "verify-1",
            "status": "ok",
            "started_at": "2026-05-26T14:00:01Z",
            "duration_seconds": 3.2,
            "commands": [{"command": "./scripts/verify", "exit_code": 0, "duration_seconds": 3.0}],
        }
    ]
    assert payload["briefs"] == [
        {"name": "code-graph", "attached": True, "bytes": 1200},
        {"name": "drift-impact", "attached": False, "bytes": 0, "pending_count": 1},
        {"name": "evidence", "attached": True, "bytes": 800},
    ]


def test_runs_show_json_excludes_private_runtime_values(tmp_path, capsys):
    run_dir = tmp_path / "run"
    _write_detail_run_artifacts(run_dir)

    assert runs_cmd.show(run_dir, json_output=True) == 0

    out = capsys.readouterr().out
    assert str(run_dir) not in out
    assert "/repo" not in out
    assert "secret transcript body" not in out
    assert "stdout_log" not in out
    assert "stderr_log" not in out
    assert "handoff" not in out
    assert "artifacts" not in out


def test_runs_view_contracts_omit_planted_token_path_and_prompt(tmp_path, capsys):
    from tests.test_runs_watch import (
        _assert_no_planted_secrets,
        _plant_watch_secrets,
    )

    run_dir = tmp_path / ".brigade" / "runs" / "20260817-100000-watch-aaaaaa"
    leaks = _plant_watch_secrets(run_dir, tmp_path)

    assert runs_cmd.list_runs(cwd=tmp_path, json_output=True) == 0
    listed = capsys.readouterr().out
    _assert_no_planted_secrets(listed, leaks)
    assert json.loads(listed)["schema"] == "brigade.runs-list.v1"

    assert runs_cmd.show(run_dir, json_output=True) == 0
    detail = capsys.readouterr().out
    _assert_no_planted_secrets(detail, leaks)
    assert json.loads(detail)["schema"] == "brigade.run-detail.v1"

    assert runs_cmd.watch(run_dir, cwd=tmp_path, interval=0.0, json_output=True) == 0
    watched = capsys.readouterr().out
    _assert_no_planted_secrets(watched, leaks)
    frames = [json.loads(line) for line in watched.splitlines()]
    assert frames
    assert all(frame["schema"] == "brigade.run-watch.v1" for frame in frames)


def test_runs_show_json_bounds_long_task_text(tmp_path, capsys):
    run_dir = tmp_path / "run"
    _write_run_artifacts(run_dir)
    run_meta = json.loads((run_dir / "run.json").read_text())
    run_meta["task"] = "x" * 5_000
    _write_json(run_dir / "run.json", run_meta)

    assert runs_cmd.show(run_dir, json_output=True) == 0

    payload = json.loads(capsys.readouterr().out)
    assert len(payload["run"]["task"]) <= 400
    assert payload["run"]["task"].endswith("...")


def test_runs_show_json_cli_flag(tmp_path, capsys):
    run_dir = tmp_path / "run"
    _write_run_artifacts(run_dir)

    assert cli.main(["runs", "show", str(run_dir), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "brigade.run-detail.v1"


def test_runs_latest_json_shares_detail_contract(tmp_path, capsys):
    runs_root = tmp_path / ".brigade" / "runs"
    early = runs_root / "20260526-130000-aaaa"
    late = runs_root / "20260526-140000-bbbb"
    runs_root.mkdir(parents=True)
    _write_run_artifacts(early)
    _write_run_artifacts(late)
    late_meta = json.loads((late / "run.json").read_text())
    late_meta["started_at"] = "2026-05-26T15:00:00Z"
    _write_json(late / "run.json", late_meta)

    assert cli.main(["runs", "latest", "--cwd", str(tmp_path), "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "brigade.run-detail.v1"
    assert payload["run"]["run_id"] == "20260526-140000-bbbb"


def test_runs_list_json_emits_versioned_contract(tmp_path, capsys):
    runs_root = tmp_path / ".brigade" / "runs"
    runs_root.mkdir(parents=True)
    _write_minimal_run(
        runs_root / "20260526-130000-aaaa",
        task="t" * 5_000,
        status="ok",
        started_at="2026-05-26T13:00:00Z",
        read_only=True,
    )
    _write_minimal_run(
        runs_root / "20260526-140000-bbbb",
        task="fix the bug",
        status="failed",
        started_at="2026-05-26T14:00:00Z",
        dry_run=True,
    )
    failed = json.loads((runs_root / "20260526-140000-bbbb" / "run.json").read_text())
    failed["failure_phase"] = "dispatch"
    _write_json(runs_root / "20260526-140000-bbbb" / "run.json", failed)
    invalid = runs_root / "20260526-150000-cccc"
    invalid.mkdir()
    (invalid / "run.json").write_text("not json")

    assert cli.main(["runs", "list", "--cwd", str(tmp_path), "--json"]) == 0

    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["schema"] == "brigade.runs-list.v1"
    assert payload["skipped_invalid"] == 1
    assert [run["run_id"] for run in payload["runs"]] == [
        "20260526-140000-bbbb",
        "20260526-130000-aaaa",
    ]
    newest, oldest = payload["runs"]
    assert newest["status"] == "failed"
    assert newest["failure_phase"] == "dispatch"
    assert newest["mode"] == "normal, dry-run"
    assert newest["resume_available"] is False
    assert oldest["mode"] == "read-only"
    assert len(oldest["task"]) <= 160
    assert str(runs_root) not in out
    assert "/repo" not in out


def test_runs_list_json_respects_limit(tmp_path, capsys):
    runs_root = tmp_path / ".brigade" / "runs"
    runs_root.mkdir(parents=True)
    for hour in (13, 14):
        _write_minimal_run(
            runs_root / f"20260526-{hour}0000-aaaa",
            task="task",
            status="ok",
            started_at=f"2026-05-26T{hour}:00:00Z",
        )

    assert runs_cmd.list_runs(cwd=tmp_path, limit=1, json_output=True) == 0

    payload = json.loads(capsys.readouterr().out)
    assert [run["run_id"] for run in payload["runs"]] == ["20260526-140000-aaaa"]


def test_runs_recover_cli_dispatches_resolved_run(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    seen = {}
    monkeypatch.setattr(
        runs_cmd,
        "recover",
        lambda run, **kwargs: seen.update(run=run, **kwargs) or 0,
        raising=False,
    )

    rc = cli.main(["runs", "recover", str(run_dir), "--cwd", str(tmp_path)])

    assert rc == 0
    assert seen == {"run": str(run_dir), "cwd": tmp_path, "runs_dir": None}


def test_runs_child_cli_dispatches_parent_and_event(tmp_path, monkeypatch):
    parent_dir = tmp_path / "run"
    parent_dir.mkdir()
    seen = {}

    def fake_child(run, event_id, **kwargs):
        seen.update(run=run, event_id=event_id, **kwargs)
        return 0

    monkeypatch.setattr(runs_cmd, "child", fake_child, raising=False)

    rc = cli.main(["runs", "child", str(parent_dir), "event-123", "--cwd", str(tmp_path)])

    assert rc == 0
    assert seen == {"run": str(parent_dir), "event_id": "event-123", "cwd": tmp_path, "runs_dir": None}


def test_runs_redact_cli_dispatches_operator_procedure(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    seen = {}

    def fake_redact(run, **kwargs):
        seen.update(run=run, **kwargs)
        return 0

    monkeypatch.setattr(runs_cmd, "redact", fake_redact, raising=False)

    rc = cli.main(
        [
            "runs",
            "redact",
            str(run_dir),
            "--from-sequence",
            "2",
            "--to-sequence",
            "4",
            "--reason",
            "credential-exposure",
            "--operator-confirm",
            "--cwd",
            str(tmp_path),
        ]
    )

    assert rc == 0
    assert seen == {
        "run": str(run_dir),
        "cwd": tmp_path,
        "runs_dir": None,
        "sequence_start": 2,
        "sequence_end": 4,
        "reason": "credential-exposure",
        "operator_confirmed": True,
        "cleanup_operation": None,
    }


def test_runs_redact_cli_dispatches_explicit_cleanup(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    seen = {}
    monkeypatch.setattr(
        runs_cmd,
        "redact",
        lambda run, **kwargs: seen.update(run=run, **kwargs) or 0,
        raising=False,
    )

    rc = cli.main(
        [
            "runs",
            "redact",
            str(run_dir),
            "--cleanup-quarantine",
            "redact-abc123",
            "--operator-confirm",
            "--cwd",
            str(tmp_path),
        ]
    )

    assert rc == 0
    assert seen == {
        "run": str(run_dir),
        "cwd": tmp_path,
        "runs_dir": None,
        "sequence_start": None,
        "sequence_end": None,
        "reason": None,
        "operator_confirmed": True,
        "cleanup_operation": "redact-abc123",
    }


def test_runs_redact_cli_reports_removed_quarantine_on_cleaned_replay(tmp_path, monkeypatch, capsys):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setattr(
        run_redaction,
        "redact_journal",
        lambda *args, **kwargs: run_redaction.RedactionReport(
            operation_id="redact-0123456789abcdef",
            sequence_start=2,
            sequence_end=2,
            quarantine_path=run_dir / "events" / "redactions" / "redact-0123456789abcdef" / "original.jsonl",
            record_path=run_dir / "events" / "redactions" / "redact-0123456789abcdef" / "record.json",
            cleaned=True,
        ),
    )

    rc = runs_cmd.redact(
        run_dir,
        cwd=tmp_path,
        sequence_start=2,
        sequence_end=2,
        reason="credential-exposure",
        operator_confirmed=True,
    )

    assert rc == 0
    output = capsys.readouterr().out
    assert "quarantine: removed" in output
    assert "quarantine: retained" not in output


def test_runs_recover_marks_dead_owner_run_terminal(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    run_dir = workspace / ".brigade" / "runs" / "orphan"
    _write_minimal_run(
        run_dir,
        task="orphaned task",
        status="dispatching",
        started_at="2026-07-16T00:00:00Z",
    )
    run_meta = json.loads((run_dir / "run.json").read_text())
    run_meta["cwd"] = str(workspace)
    _write_json(run_dir / "run.json", run_meta)
    lock_path = _write_lock_owner(workspace, run_dir)

    rc = runs_cmd.recover(str(run_dir), cwd=workspace)

    assert rc == 0
    recovered = json.loads((run_dir / "run.json").read_text())
    assert recovered["status"] == "failed"
    assert recovered["failure_phase"] == "stale-lock-recovery"
    assert not lock_path.exists()
    out = capsys.readouterr().out
    assert f"recovered: {run_dir}" in out
    assert "resume: unavailable" in out


def test_runs_recover_uses_recorded_lock_workspace_for_worktree_run(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    detached = tmp_path / "detached"
    detached.mkdir()
    run_dir = workspace / ".brigade" / "runs" / "orphan"
    _write_minimal_run(
        run_dir,
        task="orphaned worktree task",
        status="dispatching",
        started_at="2026-07-16T00:00:00Z",
    )
    run_meta = json.loads((run_dir / "run.json").read_text())
    run_meta.update({"cwd": str(detached), "lock_workspace": str(workspace)})
    _write_json(run_dir / "run.json", run_meta)
    lock_path = _write_lock_owner(workspace, run_dir)

    rc = runs_cmd.recover(str(run_dir), cwd=detached)

    assert rc == 0
    assert not lock_path.exists()
    assert json.loads((run_dir / "run.json").read_text())["status"] == "failed"
    assert f"recovered: {run_dir}" in capsys.readouterr().out


def test_runs_recover_infers_lock_workspace_for_legacy_worktree_run(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    detached = tmp_path / "detached"
    detached.mkdir()
    run_dir = workspace / ".brigade" / "runs" / "legacy"
    _write_minimal_run(
        run_dir,
        task="legacy worktree task",
        status="dispatching",
        started_at="2026-07-16T00:00:00Z",
    )
    run_meta = json.loads((run_dir / "run.json").read_text())
    run_meta["cwd"] = str(detached)
    _write_json(run_dir / "run.json", run_meta)
    lock_path = _write_lock_owner(workspace, run_dir)

    rc = runs_cmd.recover(str(run_dir), cwd=tmp_path)

    assert rc == 0
    assert not lock_path.exists()
    assert json.loads((run_dir / "run.json").read_text())["status"] == "failed"
    assert f"recovered: {run_dir}" in capsys.readouterr().out


def test_runs_recover_reconstructs_missing_run_json_from_matching_dead_lock(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    run_dir = workspace / ".brigade" / "runs" / "orphan"
    run_dir.mkdir(parents=True)
    _write_lock_owner(workspace, run_dir)

    rc = runs_cmd.recover(str(run_dir), cwd=workspace)

    assert rc == 0
    recovered = json.loads((run_dir / "run.json").read_text())
    assert recovered["status"] == "failed"
    assert recovered["failure"]["prior_status"] == "artifact-unavailable"
    assert f"recovered: {run_dir}" in capsys.readouterr().out


def test_runs_recover_preserves_and_reconstructs_corrupt_run_json(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    run_dir = workspace / ".brigade" / "runs" / "orphan"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text("not json")
    _write_lock_owner(workspace, run_dir)

    rc = runs_cmd.recover(str(run_dir), cwd=workspace)

    assert rc == 0
    recovered = json.loads((run_dir / "run.json").read_text())
    preserved = Path(recovered["recovery_preserved_artifact"])
    assert preserved.read_text() == "not json"
    assert f"recovered: {run_dir}" in capsys.readouterr().out


def test_runs_recover_refuses_live_owner_without_changing_artifacts(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    run_dir = workspace / ".brigade" / "runs" / "active"
    _write_minimal_run(
        run_dir,
        task="active task",
        status="dispatching",
        started_at="2026-07-16T00:00:00Z",
    )
    run_meta = json.loads((run_dir / "run.json").read_text())
    run_meta["cwd"] = str(workspace)
    _write_json(run_dir / "run.json", run_meta)
    lock_path = _write_lock_owner(workspace, run_dir, pid=os.getpid())

    rc = runs_cmd.recover(str(run_dir), cwd=workspace)

    assert rc == 2
    assert json.loads((run_dir / "run.json").read_text())["status"] == "dispatching"
    assert lock_path.is_dir()
    assert "run owner process is still active" in capsys.readouterr().err


def test_runs_recover_refuses_lock_for_different_run(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    requested = workspace / ".brigade" / "runs" / "requested"
    recorded = workspace / ".brigade" / "runs" / "recorded"
    _write_minimal_run(
        requested,
        task="requested task",
        status="dispatching",
        started_at="2026-07-16T00:00:00Z",
    )
    run_meta = json.loads((requested / "run.json").read_text())
    run_meta["cwd"] = str(workspace)
    _write_json(requested / "run.json", run_meta)
    lock_path = _write_lock_owner(workspace, recorded)

    rc = runs_cmd.recover(str(requested), cwd=workspace)

    assert rc == 2
    assert json.loads((requested / "run.json").read_text())["status"] == "dispatching"
    assert lock_path.is_dir()
    assert "run lock belongs to a different run" in capsys.readouterr().err


def test_runs_recover_is_idempotent_for_terminal_run(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    run_dir = workspace / ".brigade" / "runs" / "failed"
    _write_minimal_run(
        run_dir,
        task="failed task",
        status="failed",
        started_at="2026-07-16T00:00:00Z",
    )

    rc = runs_cmd.recover(str(run_dir), cwd=workspace)

    assert rc == 0
    out = capsys.readouterr().out
    assert f"already terminal: {run_dir} [failed]" in out
    assert "resume: unavailable" in out


def test_runs_recover_treats_lost_concurrent_claim_as_already_terminal(tmp_path, monkeypatch, capsys):
    from brigade import runguard

    workspace = tmp_path / "workspace"
    run_dir = workspace / ".brigade" / "runs" / "orphan"
    _write_minimal_run(run_dir, task="orphaned task", status="dispatching", started_at="2026-07-16T00:00:00Z")
    run_meta = json.loads((run_dir / "run.json").read_text())
    run_meta["cwd"] = str(workspace)
    _write_json(run_dir / "run.json", run_meta)

    def lose_claim(cwd, requested_run, *, required=True):
        terminal = json.loads((run_dir / "run.json").read_text())
        terminal.update({"status": "failed", "failure_phase": "stale-lock-recovery"})
        _write_json(run_dir / "run.json", terminal)
        raise runguard.RunLockError(f"run lock not found for run: {requested_run}")

    monkeypatch.setattr(runguard, "recover_stale_run", lose_claim)

    assert runs_cmd.recover(str(run_dir), cwd=workspace) == 0
    assert f"already terminal: {run_dir} [failed]" in capsys.readouterr().out


def test_runs_recover_lost_claim_with_invalid_utf8_run_json_is_bounded(tmp_path, capsys):
    """Invalid UTF-8 in run.json must not escape the concurrent-terminal
    fallback as a raw ``UnicodeDecodeError``; recovery stays bounded at rc=2."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / "orphan"
    _activate_journal_with_checkpoint(workspace, run_dir, {"status": "planning", "task": "demo", "cwd": str(workspace)})
    # A concurrent recovery already cleared the matching lock.
    shutil.rmtree(workspace / ".brigade" / "run.lock")
    # run.json is corrupt: invalid UTF-8 bytes.
    (run_dir / "run.json").write_bytes(b"\xff\xfe invalid utf8 \x00")

    rc = runs_cmd.recover(str(run_dir), cwd=workspace)

    assert rc == 2
    err = capsys.readouterr().err
    assert "Traceback" not in err


def test_runs_recover_lost_claim_with_deeply_nested_run_json_is_bounded(tmp_path, capsys):
    """A ``RecursionError`` from deeply nested run.json must not escape the
    concurrent-terminal fallback; recovery stays bounded at rc=2."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / "orphan"
    _activate_journal_with_checkpoint(workspace, run_dir, {"status": "planning", "task": "demo", "cwd": str(workspace)})
    shutil.rmtree(workspace / ".brigade" / "run.lock")
    # Valid but deeply nested JSON: json.loads recurses and raises RecursionError.
    nesting = 10000
    (run_dir / "run.json").write_text('{"a":' * nesting + "1" + "}" * nesting)

    rc = runs_cmd.recover(str(run_dir), cwd=workspace)

    assert rc == 2
    err = capsys.readouterr().err
    assert "Traceback" not in err


def test_runs_recover_terminal_run_ignores_foreign_workspace_lock(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    run_dir = workspace / ".brigade" / "runs" / "failed"
    foreign_run = workspace / ".brigade" / "runs" / "other"
    _write_minimal_run(
        run_dir,
        task="failed task",
        status="failed",
        started_at="2026-07-16T00:00:00Z",
    )
    run_meta = json.loads((run_dir / "run.json").read_text())
    run_meta.update({"cwd": str(workspace), "failure_phase": "stale-lock-recovery"})
    _write_json(run_dir / "run.json", run_meta)
    lock_path = _write_lock_owner(workspace, foreign_run)

    rc = runs_cmd.recover(str(run_dir), cwd=workspace)

    assert rc == 0
    assert lock_path.is_dir()
    assert f"already terminal: {run_dir} [failed]" in capsys.readouterr().out


def test_runs_recover_clears_matching_dead_lock_after_artifact_was_already_terminal(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    run_dir = workspace / ".brigade" / "runs" / "failed"
    _write_minimal_run(
        run_dir,
        task="failed task",
        status="failed",
        started_at="2026-07-16T00:00:00Z",
    )
    run_meta = json.loads((run_dir / "run.json").read_text())
    run_meta.update({"cwd": str(workspace), "failure_phase": "stale-lock-recovery"})
    _write_json(run_dir / "run.json", run_meta)
    lock_path = _write_lock_owner(workspace, run_dir)

    rc = runs_cmd.recover(str(run_dir), cwd=workspace)

    assert rc == 0
    assert not lock_path.exists()
    assert f"already terminal: {run_dir} [failed]" in capsys.readouterr().out


def test_runs_recover_reports_app_server_resume_when_available(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    run_dir = workspace / ".brigade" / "runs" / "failed"
    _write_minimal_run(
        run_dir,
        task="failed task",
        status="failed",
        started_at="2026-07-16T00:00:00Z",
    )
    _write_json(
        run_dir / "worker-results.json",
        {
            "results": [
                {
                    "worker": "coder",
                    "ok": False,
                    "status": "failed",
                    "thread_id": "thread-123",
                }
            ]
        },
    )

    rc = runs_cmd.recover(str(run_dir), cwd=workspace)

    assert rc == 0
    assert f"resume: brigade runs resume {run_dir}" in capsys.readouterr().out


def test_runs_show_surfaces_stale_lock_recovery_and_returns_nonzero(tmp_path, capsys):
    run_dir = tmp_path / "run"
    _write_minimal_run(
        run_dir,
        task="orphaned task",
        status="failed",
        started_at="2026-07-16T00:00:00Z",
    )
    run_meta = json.loads((run_dir / "run.json").read_text())
    run_meta.update(
        {
            "cwd": str(tmp_path),
            "finished_at": "2026-07-16T00:01:00Z",
            "failure_phase": "stale-lock-recovery",
            "failure": {
                "phase": "stale-lock-recovery",
                "kind": "owner-process-exited",
                "detail": "run owner process 99999999 is no longer active",
            },
        }
    )
    _write_json(run_dir / "run.json", run_meta)

    rc = runs_cmd.show(run_dir)

    assert rc == 1
    out = capsys.readouterr().out
    assert "failure phase: stale-lock-recovery" in out
    assert "failure kind: owner-process-exited" in out
    assert f"inspect: brigade runs show {run_dir}" in out
    assert "recover: completed (stale lock cleared)" in out
    assert "resume: unavailable" in out


def test_runs_watch_surfaces_stale_lock_recovery_and_returns_nonzero(tmp_path, capsys):
    run_dir = tmp_path / "run"
    _write_minimal_run(
        run_dir,
        task="orphaned task",
        status="failed",
        started_at="2026-07-16T00:00:00Z",
    )
    run_meta = json.loads((run_dir / "run.json").read_text())
    run_meta.update(
        {
            "cwd": str(tmp_path),
            "finished_at": "2026-07-16T00:01:00Z",
            "failure_phase": "stale-lock-recovery",
            "failure": {
                "phase": "stale-lock-recovery",
                "kind": "owner-process-exited",
                "detail": "run owner process 99999999 is no longer active",
            },
        }
    )
    _write_json(run_dir / "run.json", run_meta)

    rc = runs_cmd.watch(run_dir, cwd=tmp_path, interval=0.0)

    assert rc == 1
    out = capsys.readouterr().out
    assert "failure phase: stale-lock-recovery" in out
    assert f"inspect: brigade runs show {run_dir}" in out
    assert "recover: completed (stale lock cleared)" in out
    assert "resume: unavailable" in out


def test_runs_show_does_not_claim_stale_lock_was_cleared_while_matching_lock_remains(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    run_dir = workspace / ".brigade" / "runs" / "orphan"
    _write_minimal_run(
        run_dir,
        task="orphaned task",
        status="failed",
        started_at="2026-07-16T00:00:00Z",
    )
    run_meta = json.loads((run_dir / "run.json").read_text())
    run_meta.update({"cwd": str(workspace), "failure_phase": "stale-lock-recovery"})
    _write_json(run_dir / "run.json", run_meta)
    _write_lock_owner(workspace, run_dir)

    assert runs_cmd.show(run_dir) == 1
    out = capsys.readouterr().out
    assert "recover: required (stale lock remains)" in out
    assert "recover: completed (stale lock cleared)" not in out


def test_runs_watch_does_not_claim_stale_lock_was_cleared_while_matching_claim_remains(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    run_dir = workspace / ".brigade" / "runs" / "orphan"
    _write_minimal_run(
        run_dir,
        task="orphaned task",
        status="failed",
        started_at="2026-07-16T00:00:00Z",
    )
    run_meta = json.loads((run_dir / "run.json").read_text())
    run_meta.update({"cwd": str(workspace), "failure_phase": "stale-lock-recovery"})
    _write_json(run_dir / "run.json", run_meta)
    lock_path = _write_lock_owner(workspace, run_dir)
    claimed = lock_path.with_name(f".{lock_path.name}.crashed.stale")
    lock_path.rename(claimed)

    assert runs_cmd.watch(run_dir, cwd=workspace, interval=0.0) == 1
    out = capsys.readouterr().out
    assert "recover: required (stale lock remains)" in out
    assert "recover: completed (stale lock cleared)" not in out


def test_runs_list_prints_recent_runs(tmp_path, capsys):
    runs_root = tmp_path / ".brigade" / "runs"
    _write_minimal_run(
        runs_root / "older",
        task="older task",
        status="failed",
        started_at="2026-05-26T13:00:00Z",
    )
    _write_minimal_run(
        runs_root / "newer",
        task="newer task",
        status="ok",
        started_at="2026-05-26T14:00:00Z",
        duration=2.5,
        read_only=True,
    )

    assert runs_cmd.list_runs(cwd=tmp_path, limit=10) == 0
    out = capsys.readouterr().out
    first = out.index("newer task")
    second = out.index("older task")
    assert first < second
    assert "[ok] 2.5s read-only" in out
    assert str(runs_root / "newer") in out


def test_runs_list_flags_over_timeout_nonterminal_run_without_mutating_it(tmp_path, monkeypatch, capsys):
    runs_root = tmp_path / ".brigade" / "runs"
    run_dir = runs_root / "stale"
    _write_minimal_run(
        run_dir,
        task="stalled task",
        status="dispatching",
        started_at="2026-05-26T14:00:00Z",
    )
    _write_json(
        run_dir / "roster.json",
        {
            "orchestrator": "chef",
            "timeout_seconds": 180.0,
            "agents": {
                "chef": {"timeout_seconds": None},
                "coder": {"timeout_seconds": 30.0},
                "reviewer": {"timeout_seconds": 90.0},
            },
        },
    )
    _write_json(
        run_dir / "plan.json",
        {
            "assignments": [
                {"stage": 1, "worker": "coder", "task": "inspect"},
                {"stage": 2, "worker": "reviewer", "task": "review"},
            ]
        },
    )
    before = (run_dir / "run.json").read_bytes()
    monkeypatch.setattr(runs_cmd.time, "time", lambda: 1779804091.0)

    assert runs_cmd.list_runs(cwd=tmp_path, limit=10) == 0

    out = capsys.readouterr().out
    assert "[dispatching; stale > 90s timeout]" in out
    assert (run_dir / "run.json").read_bytes() == before


def test_runs_list_uses_current_synthesis_phase_age(tmp_path, monkeypatch, capsys):
    runs_root = tmp_path / ".brigade" / "runs"
    run_dir = runs_root / "synthesizing"
    _write_minimal_run(
        run_dir,
        task="synthesize result",
        status="synthesizing",
        started_at="2026-05-26T14:00:00Z",
    )
    run_meta = json.loads((run_dir / "run.json").read_text())
    run_meta["status_started_at"] = "2026-05-26T14:01:10Z"
    _write_json(run_dir / "run.json", run_meta)
    _write_json(
        run_dir / "roster.json",
        {
            "orchestrator": "chef",
            "timeout_seconds": 180.0,
            "agents": {"chef": {"timeout_seconds": 30.0}},
        },
    )
    monkeypatch.setattr(runs_cmd.time, "time", lambda: 1779804091.0)

    assert runs_cmd.list_runs(cwd=tmp_path, limit=10) == 0

    out = capsys.readouterr().out
    assert "[synthesizing]" in out
    assert "stale" not in out


def test_handoff_timeout_uses_orchestrator_default_not_planned_workers(tmp_path):
    run_dir = tmp_path / ".brigade" / "runs" / "handoff"
    _write_minimal_run(
        run_dir,
        task="write handoff",
        status="handoff",
        started_at="2026-05-26T14:00:00Z",
    )
    _write_json(
        run_dir / "roster.json",
        {
            "orchestrator": "chef",
            "timeout_seconds": 180.0,
            "agents": {
                "chef": {"timeout_seconds": None},
                "coder": {"timeout_seconds": 30.0},
                "reviewer": {"timeout_seconds": 90.0},
            },
        },
    )
    _write_json(
        run_dir / "plan.json",
        {
            "assignments": [
                {"stage": 1, "worker": "coder", "task": "implement"},
                {"stage": 2, "worker": "reviewer", "task": "review"},
            ]
        },
    )

    run_meta = json.loads((run_dir / "run.json").read_text())
    assert runs_cmd._run_timeout_seconds(run_dir, run_meta) == 180.0


def test_result_processing_timeout_uses_orchestrator_not_direct_worker(tmp_path):
    run_dir = tmp_path / ".brigade" / "runs" / "result-processing"
    _write_minimal_run(
        run_dir,
        task="process worker result",
        status="result-processing",
        started_at="2026-05-26T14:00:00Z",
    )
    run_meta = json.loads((run_dir / "run.json").read_text())
    run_meta["worker"] = "coder"
    _write_json(run_dir / "run.json", run_meta)
    _write_json(
        run_dir / "roster.json",
        {
            "orchestrator": "chef",
            "timeout_seconds": 180.0,
            "agents": {
                "chef": {"timeout_seconds": 90.0},
                "coder": {"timeout_seconds": 30.0},
            },
        },
    )

    assert runs_cmd._run_timeout_seconds(run_dir, run_meta) == 90.0


def test_runs_list_uses_current_later_stage_age(tmp_path, monkeypatch, capsys):
    runs_root = tmp_path / ".brigade" / "runs"
    run_dir = runs_root / "stage-two"
    _write_minimal_run(
        run_dir,
        task="review implementation",
        status="dispatching",
        started_at="2026-05-26T14:00:00Z",
    )
    run_meta = json.loads((run_dir / "run.json").read_text())
    run_meta.update(
        {
            "status_started_at": "2026-05-26T14:01:10Z",
            "active_stage": 2,
            "active_seats": ["reviewer"],
        }
    )
    _write_json(run_dir / "run.json", run_meta)
    _write_json(
        run_dir / "roster.json",
        {
            "orchestrator": "chef",
            "timeout_seconds": 180.0,
            "agents": {
                "chef": {"timeout_seconds": 180.0},
                "coder": {"timeout_seconds": 30.0},
                "reviewer": {"timeout_seconds": 90.0},
            },
        },
    )
    _write_json(
        run_dir / "plan.json",
        {
            "assignments": [
                {"stage": 1, "worker": "coder", "task": "implement"},
                {"stage": 2, "worker": "reviewer", "task": "review"},
            ]
        },
    )
    monkeypatch.setattr(runs_cmd.time, "time", lambda: 1779804091.0)

    assert runs_cmd.list_runs(cwd=tmp_path, limit=10) == 0

    out = capsys.readouterr().out
    assert "[dispatching]" in out
    assert "stale" not in out


def test_runs_list_detects_stale_active_phase_with_corrupt_optional_plan(tmp_path, monkeypatch, capsys):
    runs_root = tmp_path / ".brigade" / "runs"
    run_dir = runs_root / "active-corrupt-plan"
    _write_minimal_run(
        run_dir,
        task="review implementation",
        status="dispatching",
        started_at="2026-05-26T14:00:00Z",
    )
    run_meta = json.loads((run_dir / "run.json").read_text())
    run_meta["active_seats"] = ["reviewer"]
    _write_json(run_dir / "run.json", run_meta)
    _write_json(
        run_dir / "roster.json",
        {
            "orchestrator": "chef",
            "timeout_seconds": 180.0,
            "agents": {
                "chef": {"timeout_seconds": 180.0},
                "reviewer": {"timeout_seconds": 90.0},
            },
        },
    )
    (run_dir / "plan.json").write_text("not json")
    monkeypatch.setattr(runs_cmd.time, "time", lambda: 1779804091.0)

    assert runs_cmd.list_runs(cwd=tmp_path, limit=10) == 0

    assert "[dispatching; stale > 90s timeout]" in capsys.readouterr().out


def test_runs_list_uses_direct_worker_timeout_while_status_is_started(tmp_path, monkeypatch, capsys):
    runs_root = tmp_path / ".brigade" / "runs"
    run_dir = runs_root / "direct-worker"
    _write_minimal_run(
        run_dir,
        task="direct task",
        status="started",
        started_at="2026-05-26T14:01:00Z",
    )
    run_meta = json.loads((run_dir / "run.json").read_text())
    run_meta["worker"] = "coder"
    _write_json(run_dir / "run.json", run_meta)
    _write_json(
        run_dir / "roster.json",
        {
            "orchestrator": "chef",
            "timeout_seconds": 180.0,
            "agents": {
                "chef": {"timeout_seconds": 180.0},
                "coder": {"timeout_seconds": 30.0},
            },
        },
    )
    monkeypatch.setattr(runs_cmd.time, "time", lambda: 1779804091.0)

    assert runs_cmd.list_runs(cwd=tmp_path, limit=10) == 0

    assert "[started; stale > 30s timeout]" in capsys.readouterr().out


def test_runs_list_treats_naive_legacy_timestamp_as_utc(tmp_path, monkeypatch, capsys):
    runs_root = tmp_path / ".brigade" / "runs"
    run_dir = runs_root / "legacy-naive"
    _write_minimal_run(
        run_dir,
        task="legacy task",
        status="started",
        started_at="2026-05-26T14:00:00",
    )
    _write_json(
        run_dir / "roster.json",
        {
            "orchestrator": "chef",
            "timeout_seconds": 30.0,
            "agents": {"chef": {"timeout_seconds": None}},
        },
    )
    previous_tz = os.environ.get("TZ")
    try:
        os.environ["TZ"] = "America/New_York"
        system_time.tzset()
        monkeypatch.setattr(runs_cmd.time, "time", lambda: 1779804031.0)

        assert runs_cmd.list_runs(cwd=tmp_path, limit=10) == 0
    finally:
        if previous_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous_tz
        system_time.tzset()

    assert "[started; stale > 30s timeout]" in capsys.readouterr().out


def test_runs_list_respects_limit(tmp_path, capsys):
    runs_root = tmp_path / ".brigade" / "runs"
    _write_minimal_run(
        runs_root / "one",
        task="one task",
        status="ok",
        started_at="2026-05-26T13:00:00Z",
    )
    _write_minimal_run(
        runs_root / "two",
        task="two task",
        status="ok",
        started_at="2026-05-26T14:00:00Z",
    )

    assert runs_cmd.list_runs(cwd=tmp_path, limit=1) == 0
    out = capsys.readouterr().out
    assert "two task" in out
    assert "one task" not in out


def test_runs_list_reports_missing_runs_dir(tmp_path, capsys):
    assert runs_cmd.list_runs(cwd=tmp_path) == 2
    assert "runs directory not found" in capsys.readouterr().err


def test_runs_list_rejects_bad_limit(tmp_path, capsys):
    assert runs_cmd.list_runs(cwd=tmp_path, limit=0) == 2
    assert "--limit must be a positive integer" in capsys.readouterr().err


def test_runs_list_cli_with_explicit_runs_dir(tmp_path, capsys):
    runs_root = tmp_path / "runs"
    _write_minimal_run(
        runs_root / "one",
        task="cli task",
        status="dry-run",
        started_at="2026-05-26T14:00:00Z",
        dry_run=True,
    )

    assert cli.main(["runs", "list", "--cwd", str(tmp_path), "--runs-dir", str(runs_root)]) == 0
    out = capsys.readouterr().out
    assert "cli task" in out
    assert "dry-run" in out


def test_runs_latest_shows_newest_run(tmp_path, capsys):
    runs_root = tmp_path / ".brigade" / "runs"
    _write_minimal_run(
        runs_root / "older",
        task="older task",
        status="failed",
        started_at="2026-05-26T13:00:00Z",
    )
    newest = runs_root / "newer"
    _write_run_artifacts(newest)

    assert runs_cmd.show_latest(cwd=tmp_path) == 0
    out = capsys.readouterr().out
    assert f"run: {newest}" in out
    assert "task: build feature" in out
    assert "final:" in out


def test_runs_latest_reports_no_runs(tmp_path, capsys):
    runs_root = tmp_path / ".brigade" / "runs"
    runs_root.mkdir(parents=True)

    assert runs_cmd.show_latest(cwd=tmp_path) == 1
    assert "no runs found" in capsys.readouterr().err


def test_runs_latest_cli_with_explicit_runs_dir(tmp_path, capsys):
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    run_dir = runs_root / "one"
    _write_run_artifacts(run_dir)

    assert cli.main(["runs", "latest", "--cwd", str(tmp_path), "--runs-dir", str(runs_root)]) == 0
    assert f"run: {run_dir}" in capsys.readouterr().out


def test_runs_child_creates_lineaged_child_run(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runs_root = workspace / ".brigade" / "runs"
    parent_dir = runs_root / "20260817-120000-parent-aaaaaa"
    events = _write_checkpointed_parent_run(workspace, parent_dir)
    branch_event = events[-1]

    assert runs_cmd.child(parent_dir.name, branch_event.event_id, cwd=workspace) == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert out and out[-1].startswith("child: ")
    child_dir = Path(out[-1].split("child: ", 1)[1])
    assert child_dir.parent == runs_root
    assert child_dir.name != parent_dir.name

    child_meta = json.loads((child_dir / "run.json").read_text())
    assert child_meta["lineage"] == {
        "kind": "child",
        "parent_run_id": parent_dir.name,
        "branch_point_event_id": branch_event.event_id,
        "shared_prefix": {
            "event_sequence": branch_event.sequence,
            "event_digest": branch_event.event_digest,
            "previous_digest": branch_event.previous_digest,
        },
    }
    child_events = _events(child_dir)
    assert all(event.run_id == child_dir.name for event in child_events)
    assert [event.event_type for event in child_events[-2:]] == ["run.snapshot.checkpointed", "run.planning.started"]
    assert child_events[-2].payload["paired_event_type"] == "run.planning.started"
    assert child_events[-1].payload == {"detail": "branched child snapshot"}
    repaired = run_checkpoint.recover_from_checkpoint(child_dir, None)
    assert repaired["status"] == "planning"


def test_runs_child_uses_fresh_started_at_not_parent(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runs_root = workspace / ".brigade" / "runs"
    parent_dir = runs_root / "20260817-120000-parent-aaaaaa"
    events = _write_checkpointed_parent_run(workspace, parent_dir)
    parent_started = json.loads((parent_dir / "run.json").read_text())["started_at"]
    assert parent_started == "2026-08-17T12:00:00Z"
    before = datetime.now(timezone.utc) - timedelta(seconds=1)

    assert runs_cmd.child(parent_dir.name, events[-1].event_id, cwd=workspace) == 0
    out = capsys.readouterr().out.strip().splitlines()
    child_dir = Path(out[-1].split("child: ", 1)[1])
    child_meta = json.loads((child_dir / "run.json").read_text())

    after = datetime.now(timezone.utc) + timedelta(seconds=1)
    assert child_meta["started_at"] != parent_started
    assert child_meta["started_at"] == child_meta["status_started_at"]
    started = datetime.fromisoformat(child_meta["started_at"].replace("Z", "+00:00"))
    assert before <= started <= after


def test_runs_child_rejects_checkpoint_event_as_branch_point(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runs_root = workspace / ".brigade" / "runs"
    parent_dir = runs_root / "20260817-120000-parent-aaaaaa"
    events = _write_checkpointed_parent_run(workspace, parent_dir)
    checkpoint_event = next(event for event in events if event.event_type == run_checkpoint.CHECKPOINT_EVENT_TYPE)

    assert runs_cmd.child(parent_dir.name, checkpoint_event.event_id, cwd=workspace) == 2
    err = capsys.readouterr().err
    assert "unsupported branch point event" in err
    assert "not the checkpoint itself" in err
    children = [path for path in runs_root.iterdir() if path.name != parent_dir.name]
    assert children == []


def test_runs_child_fails_closed_for_unsupported_event_without_partial_run(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runs_root = workspace / ".brigade" / "runs"
    parent_dir = runs_root / "20260817-120000-parent-aaaaaa"
    _write_checkpointed_parent_run(workspace, parent_dir)
    with runguard.run_lock(workspace, run_dir=parent_dir):
        run_lifecycle.record_lifecycle_event(
            parent_dir,
            event_type="run.result-processing.started",
            payload={"detail": "uncovered tail"},
            idempotency_key="unsupported-tail",
            workspace=workspace,
        )
    unsupported_event = _events(parent_dir)[-1]

    assert runs_cmd.child(parent_dir.name, unsupported_event.event_id, cwd=workspace) == 2
    err = capsys.readouterr().err
    assert "unsupported branch point event" in err
    children = [path for path in runs_root.iterdir() if path.name != parent_dir.name]
    assert children == []


def test_runs_child_fails_closed_for_unknown_event_without_partial_run(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runs_root = workspace / ".brigade" / "runs"
    parent_dir = runs_root / "20260817-120000-parent-aaaaaa"
    _write_checkpointed_parent_run(workspace, parent_dir)

    assert runs_cmd.child(parent_dir.name, "missing-event-id", cwd=workspace) == 2
    assert "unknown branch point event" in capsys.readouterr().err
    children = [path for path in runs_root.iterdir() if path.name != parent_dir.name]
    assert children == []


def test_runs_child_fails_closed_for_corrupt_parent_journal_without_partial_run(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runs_root = workspace / ".brigade" / "runs"
    parent_dir = runs_root / "20260817-120000-parent-aaaaaa"
    events = _write_checkpointed_parent_run(workspace, parent_dir)
    with _journal_path(parent_dir).open("ab") as handle:
        handle.write(b"{not-json")

    assert runs_cmd.child(parent_dir.name, events[-1].event_id, cwd=workspace) == 2
    assert "parent lifecycle journal has a partial trailing record" in capsys.readouterr().err
    children = [path for path in runs_root.iterdir() if path.name != parent_dir.name]
    assert children == []


def test_runs_child_fails_closed_for_digest_mismatch_without_partial_run(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runs_root = workspace / ".brigade" / "runs"
    parent_dir = runs_root / "20260817-120000-parent-aaaaaa"
    events = _write_checkpointed_parent_run(workspace, parent_dir)
    journal = _journal_path(parent_dir)
    raw = journal.read_bytes()
    ended_nl = raw.endswith(b"\n")
    lines = raw.split(b"\n")
    complete = [index for index, line in enumerate(lines) if line]
    idx = complete[-1]
    payload = json.loads(lines[idx])
    digest = payload["event_digest"]
    assert isinstance(digest, str) and len(digest) == 64
    payload["event_digest"] = ("0" if digest[0] != "0" else "1") + digest[1:]
    lines[idx] = run_journal.canonical_bytes(payload)
    rebuilt = b"\n".join(lines)
    if ended_nl and not rebuilt.endswith(b"\n"):
        rebuilt += b"\n"
    journal.write_bytes(rebuilt)

    assert runs_cmd.child(parent_dir.name, events[-1].event_id, cwd=workspace) == 2
    assert "parent lifecycle journal is corrupt" in capsys.readouterr().err
    children = [path for path in runs_root.iterdir() if path.name != parent_dir.name]
    assert children == []


def test_runs_child_fails_closed_for_legacy_parent_without_journal(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runs_root = workspace / ".brigade" / "runs"
    parent_dir = runs_root / "20260817-120000-legacy-aaaaaa"
    _write_minimal_run(
        parent_dir,
        task="legacy root",
        status="ok",
        started_at="2026-08-17T12:00:00Z",
    )

    assert runs_cmd.child(parent_dir.name, "missing-event-id", cwd=workspace) == 2
    assert "parent run has no lifecycle journal" in capsys.readouterr().err
    children = [path for path in runs_root.iterdir() if path.name != parent_dir.name]
    assert children == []


def test_runs_child_refuses_workspace_lock_without_partial_run(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runs_root = workspace / ".brigade" / "runs"
    parent_dir = runs_root / "20260817-120000-parent-aaaaaa"
    events = _write_checkpointed_parent_run(workspace, parent_dir)

    with runguard.run_lock(workspace, run_dir=parent_dir):
        assert runs_cmd.child(parent_dir.name, events[-1].event_id, cwd=workspace) == 2
    err = capsys.readouterr().err
    assert "could not create child run" in err
    assert "another brigade run appears active" in err
    children = [path for path in runs_root.iterdir() if path.name != parent_dir.name]
    assert children == []


def test_runs_show_lists_recorded_children_and_parent_lineage(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runs_root = workspace / ".brigade" / "runs"
    parent_dir = runs_root / "20260817-120000-parent-aaaaaa"
    events = _write_checkpointed_parent_run(workspace, parent_dir)
    branch_event = events[-1]

    assert runs_cmd.child(parent_dir.name, branch_event.event_id, cwd=workspace) == 0
    out = capsys.readouterr().out.strip().splitlines()
    child_dir = Path(out[-1].split("child: ", 1)[1])

    assert runs_cmd.show(child_dir) == 0
    child_out = capsys.readouterr().out
    assert "lineage:" in child_out
    assert "kind: child" in child_out
    assert f"parent run: {parent_dir.name}" in child_out
    assert f"branch point: {branch_event.event_id}" in child_out

    assert runs_cmd.show(parent_dir) == 0
    parent_out = capsys.readouterr().out
    assert "lineage:" in parent_out
    assert f"    - {child_dir.name}" in parent_out
    assert f"branch point: {branch_event.event_id}" in parent_out


def test_runs_child_cleans_up_partial_dir_when_checkpoint_fails_after_mkdir(tmp_path, capsys, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runs_root = workspace / ".brigade" / "runs"
    parent_dir = runs_root / "20260817-120000-parent-aaaaaa"
    events = _write_checkpointed_parent_run(workspace, parent_dir)
    lock_held_during_cleanup: list[bool] = []
    original_rmtree = shutil.rmtree

    def boom(*_args, **_kwargs):
        raise run_checkpoint.CheckpointError("injected post-mkdir failure", category="test")

    def tracking_rmtree(path, *args, **kwargs):
        target = Path(path)
        if target.parent == runs_root:
            lock_held_during_cleanup.append(runguard.is_active_run_owner(workspace, target))
        return original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(run_checkpoint, "write_checkpoint", boom)
    monkeypatch.setattr(runs_cmd.shutil, "rmtree", tracking_rmtree)

    assert runs_cmd.child(parent_dir.name, events[-1].event_id, cwd=workspace) == 2
    err = capsys.readouterr().err
    assert "could not create child run" in err
    assert "injected post-mkdir failure" in err
    children = [path for path in runs_root.iterdir() if path.name != parent_dir.name]
    assert children == []
    assert lock_held_during_cleanup == [True]


def _run_artifact_fingerprint(run_dir: Path) -> dict[str, bytes]:
    """Byte snapshot of resume-relevant artifacts for mutation assertions."""
    payload: dict[str, bytes] = {}
    for relative in ("run.json", "events/lifecycle.jsonl"):
        path = run_dir / relative
        if path.is_file():
            payload[relative] = path.read_bytes()
    return payload


def _create_lineaged_child(workspace: Path, parent_dir: Path, event_id: str, capsys) -> Path:
    assert runs_cmd.child(parent_dir.name, event_id, cwd=workspace) == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert out and out[-1].startswith("child: ")
    return Path(out[-1].split("child: ", 1)[1])


def test_runs_resume_child_from_branch_point(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runs_root = workspace / ".brigade" / "runs"
    parent_dir = runs_root / "20260817-120000-parent-aaaaaa"
    events = _write_checkpointed_parent_run(workspace, parent_dir)
    branch_event = events[-1]
    child_dir = _create_lineaged_child(workspace, parent_dir, branch_event.event_id, capsys)
    before = _run_artifact_fingerprint(child_dir)
    create_event_types = [event.event_type for event in _events(child_dir)]

    assert cli.main(["runs", "resume", child_dir.name, "--cwd", str(workspace)]) == 0
    out = capsys.readouterr().out
    assert f"restored: {child_dir}" in out
    assert "did not re-execute" in out
    assert "resumed:" not in out

    child_meta = json.loads((child_dir / "run.json").read_text())
    assert child_meta["status"] == "planning"
    assert child_meta["lineage"] == {
        "kind": "child",
        "parent_run_id": parent_dir.name,
        "branch_point_event_id": branch_event.event_id,
        "shared_prefix": {
            "event_sequence": branch_event.sequence,
            "event_digest": branch_event.event_digest,
            "previous_digest": branch_event.previous_digest,
        },
    }
    assert _run_artifact_fingerprint(child_dir) == before
    assert [event.event_type for event in _events(child_dir)] == create_event_types
    assert all(event.run_id == child_dir.name for event in _events(child_dir))


def test_runs_resume_child_refuses_workspace_lock_without_partial_state(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runs_root = workspace / ".brigade" / "runs"
    parent_dir = runs_root / "20260817-120000-parent-aaaaaa"
    events = _write_checkpointed_parent_run(workspace, parent_dir)
    child_dir = _create_lineaged_child(workspace, parent_dir, events[-1].event_id, capsys)
    before = _run_artifact_fingerprint(child_dir)

    with runguard.run_lock(workspace, run_dir=parent_dir):
        assert runs_cmd.resume(child_dir) == 2
    err = capsys.readouterr().err
    assert "could not resume child run" in err
    assert "another brigade run appears active" in err
    assert _run_artifact_fingerprint(child_dir) == before


def test_runs_resume_child_leaves_parent_and_siblings_unchanged(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runs_root = workspace / ".brigade" / "runs"
    parent_dir = runs_root / "20260817-120000-parent-aaaaaa"
    events = _write_checkpointed_parent_run(workspace, parent_dir)
    branch_event = events[-1]
    child_a = _create_lineaged_child(workspace, parent_dir, branch_event.event_id, capsys)
    child_b = _create_lineaged_child(workspace, parent_dir, branch_event.event_id, capsys)
    parent_meta = json.loads((parent_dir / "run.json").read_text())
    parent_meta["status"] = "ok"
    parent_meta["finished_at"] = "2026-08-17T13:00:00Z"
    (parent_dir / "run.json").write_text(json.dumps(parent_meta, indent=2, sort_keys=True) + "\n")

    parent_before = _run_artifact_fingerprint(parent_dir)
    sibling_before = _run_artifact_fingerprint(child_b)
    assert runs_cmd.show(parent_dir) == 0
    parent_show_before = capsys.readouterr().out
    assert f"    - {child_a.name}" in parent_show_before
    assert f"    - {child_b.name}" in parent_show_before
    assert f"branch point: {branch_event.event_id}" in parent_show_before
    assert "status: ok" in parent_show_before

    assert runs_cmd.resume(child_a) == 0
    capsys.readouterr()

    assert _run_artifact_fingerprint(parent_dir) == parent_before
    assert _run_artifact_fingerprint(child_b) == sibling_before
    assert json.loads((parent_dir / "run.json").read_text())["status"] == "ok"

    assert runs_cmd.show(parent_dir) == 0
    parent_show_after = capsys.readouterr().out
    assert f"    - {child_a.name}" in parent_show_after
    assert f"    - {child_b.name}" in parent_show_after
    assert f"branch point: {branch_event.event_id}" in parent_show_after
    assert "status: ok" in parent_show_after
    assert f"    - {child_a.name} (planning; branch point: {branch_event.event_id})" in parent_show_after
    assert f"    - {child_b.name} (planning; branch point: {branch_event.event_id})" in parent_show_after
    assert json.loads((child_a / "run.json").read_text())["status"] == "planning"

    assert runs_cmd.show(child_a) == 0
    child_out = capsys.readouterr().out
    assert "kind: child" in child_out
    assert f"parent run: {parent_dir.name}" in child_out
    assert f"branch point: {branch_event.event_id}" in child_out
    assert "status: planning" in child_out


def test_runs_resume_child_fails_closed_for_corrupt_branch_point_without_partial_state(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runs_root = workspace / ".brigade" / "runs"
    parent_dir = runs_root / "20260817-120000-parent-aaaaaa"
    events = _write_checkpointed_parent_run(workspace, parent_dir)
    child_dir = _create_lineaged_child(workspace, parent_dir, events[-1].event_id, capsys)
    with _journal_path(parent_dir).open("ab") as handle:
        handle.write(b"{not-json")
    before = _run_artifact_fingerprint(child_dir)

    assert runs_cmd.resume(child_dir) == 2
    assert "parent lifecycle journal has a partial trailing record" in capsys.readouterr().err
    assert _run_artifact_fingerprint(child_dir) == before
    children = sorted(path.name for path in runs_root.iterdir())
    assert children == sorted([parent_dir.name, child_dir.name])


def test_runs_resume_child_fails_closed_for_unsupported_branch_point_without_partial_state(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runs_root = workspace / ".brigade" / "runs"
    parent_dir = runs_root / "20260817-120000-parent-aaaaaa"
    events = _write_checkpointed_parent_run(workspace, parent_dir)
    child_dir = _create_lineaged_child(workspace, parent_dir, events[-1].event_id, capsys)
    checkpoint_event = next(event for event in events if event.event_type == run_checkpoint.CHECKPOINT_EVENT_TYPE)
    child_meta = json.loads((child_dir / "run.json").read_text())
    child_meta["lineage"]["branch_point_event_id"] = checkpoint_event.event_id
    (child_dir / "run.json").write_text(json.dumps(child_meta, indent=2, sort_keys=True) + "\n")
    before = _run_artifact_fingerprint(child_dir)

    assert runs_cmd.resume(child_dir) == 2
    err = capsys.readouterr().err
    assert "unsupported branch point event" in err
    assert _run_artifact_fingerprint(child_dir) == before


def test_runs_resume_child_invokes_existing_continuation(tmp_path, capsys, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runs_root = workspace / ".brigade" / "runs"
    parent_dir = runs_root / "20260817-120000-parent-aaaaaa"
    events = _write_checkpointed_parent_run(workspace, parent_dir)
    branch_event = events[-1]
    child_dir = _create_lineaged_child(workspace, parent_dir, branch_event.event_id, capsys)
    child_meta = json.loads((child_dir / "run.json").read_text())
    child_meta["status"] = "failed"
    (child_dir / "run.json").write_text(json.dumps(child_meta, indent=2, sort_keys=True) + "\n")
    _write_json(
        child_dir / "roster.json",
        {
            "orchestrator": "chef",
            "max_workers": 4,
            "timeout_seconds": 600.0,
            "allow_models": [],
            "sandbox": None,
            "agents": {
                "chef": {"cli": "claude", "model": None, "role": "plan", "timeout_seconds": None},
                "cook": {"cli": "codex", "model": None, "role": "code", "timeout_seconds": None},
            },
        },
    )
    _write_json(
        child_dir / "worker-results.json",
        {
            "results": [
                {
                    "worker": "cook",
                    "task": "write code",
                    "ok": False,
                    "detail": "timeout",
                    "thread_id": "t-child-1",
                    "status": "interrupted",
                }
            ],
            "ground_truth": {},
        },
    )
    parent_before = _run_artifact_fingerprint(parent_dir)
    continued: list[Path] = []
    monkeypatch.setattr(run_resume, "_resume_locked", lambda path, **_kwargs: continued.append(path) or 0)

    assert runs_cmd.resume(child_dir) == 0
    assert continued == [child_dir]
    assert _run_artifact_fingerprint(parent_dir) == parent_before
    assert json.loads((child_dir / "run.json").read_text())["lineage"]["parent_run_id"] == parent_dir.name
    assert json.loads((child_dir / "run.json").read_text())["lineage"]["branch_point_event_id"] == branch_event.event_id
    assert "did not re-execute" not in capsys.readouterr().out


def test_runs_resume_legacy_run_without_child_metadata_is_unaffected(tmp_path, capsys, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runs_root = workspace / ".brigade" / "runs"
    legacy_dir = runs_root / "20260817-120000-legacy-aaaaaa"
    _write_minimal_run(
        legacy_dir,
        task="legacy root",
        status="failed",
        started_at="2026-08-17T12:00:00Z",
    )
    before = _run_artifact_fingerprint(legacy_dir)
    seen = []
    monkeypatch.setattr(run_resume, "resume", lambda path: seen.append(path) or 0)

    assert runs_cmd.resume(legacy_dir) == 0
    assert seen == [legacy_dir]
    assert _run_artifact_fingerprint(legacy_dir) == before
    assert "lineage" not in json.loads((legacy_dir / "run.json").read_text())
    assert capsys.readouterr().err == ""


def test_runs_child_does_not_inherit_parent_control_socket_or_parent_paths(tmp_path, capsys):
    from brigade import run_control

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runs_root = workspace / ".brigade" / "runs"
    parent_dir = runs_root / "20260817-120000-parent-aaaaaa"
    parent_socket = "/tmp/brigade-parent-worker.sock"
    events = _write_checkpointed_parent_run(
        workspace,
        parent_dir,
        extra={
            "control_transport": {
                "schema": "brigade.run_control_transport.v1",
                "kind": "unix",
                "path": parent_socket,
            },
            "control_socket": parent_socket,
            "active_stage": "dispatch",
            "active_seats": ["coder"],
            "phase_owner": "coder",
            "artifacts": str(parent_dir),
            "handoff": str(parent_dir / "handoff.md"),
            "error": "parent timed out",
            "failure_phase": "dispatch",
            "failure_kind": "timeout",
            "failure": {"phase": "dispatch", "kind": "timeout", "detail": "parent timed out"},
            "worker_failure_summary": "coder timed out",
        },
    )

    assert runs_cmd.child(parent_dir.name, events[-1].event_id, cwd=workspace) == 0
    out = capsys.readouterr().out.strip().splitlines()
    child_dir = Path(out[-1].split("child: ", 1)[1])
    child_meta = json.loads((child_dir / "run.json").read_text())

    assert "control_transport" not in child_meta
    assert "control_socket" not in child_meta
    assert "active_stage" not in child_meta
    assert "active_seats" not in child_meta
    assert "phase_owner" not in child_meta
    assert "error" not in child_meta
    assert "failure" not in child_meta
    assert "failure_phase" not in child_meta
    assert "failure_kind" not in child_meta
    assert "worker_failure_summary" not in child_meta
    assert "handoff" not in child_meta
    assert child_meta.get("artifacts") == str(child_dir)
    with pytest.raises(run_control.ControlError):
        run_control.control_transport_from_run(child_dir)


def test_runs_show_prints_ground_truth(tmp_path, capsys):
    run_dir = tmp_path / "run"
    _write_run_artifacts(run_dir)
    payload = json.loads((run_dir / "worker-results.json").read_text())
    payload["ground_truth"] = {
        "available": True,
        "diffstat": " a.txt | 1 +\n 1 file changed, 1 insertion(+)",
        "changed_files": ["a.txt"],
        "untracked_files": ["notes.md"],
        "patch_ref": "changes.patch",
        "verify_receipts": [
            {
                "run_id": "20260703-000000-work-verify-abc",
                "status": "completed",
                "commands": [{"command": "pytest -q", "status": "completed", "exit_code": 0}],
            }
        ],
    }
    _write_json(run_dir / "worker-results.json", payload)

    assert runs_cmd.show(run_dir) == 0
    out = capsys.readouterr().out
    assert "ground truth:" in out
    assert "changed_files: 1 (a.txt)" in out
    assert "untracked_files: 1 (notes.md)" in out
    assert "1 file changed, 1 insertion(+)" in out
    assert "patch_ref: changes.patch" in out
    assert "verify: 20260703-000000-work-verify-abc completed" in out
    assert "pytest -q" in out and "exit=0" in out


def test_runs_show_prints_unavailable_ground_truth_reason(tmp_path, capsys):
    run_dir = tmp_path / "run"
    _write_run_artifacts(run_dir)
    payload = json.loads((run_dir / "worker-results.json").read_text())
    payload["ground_truth"] = {"available": False, "reason": "not a git worktree"}
    _write_json(run_dir / "worker-results.json", payload)

    assert runs_cmd.show(run_dir) == 0
    out = capsys.readouterr().out
    assert "ground truth: unavailable (not a git worktree)" in out


def test_runs_show_without_ground_truth_stays_quiet(tmp_path, capsys):
    run_dir = tmp_path / "run"
    _write_run_artifacts(run_dir)

    assert runs_cmd.show(run_dir) == 0
    assert "ground truth" not in capsys.readouterr().out


# -- Issue #568 slice 5 Task 5: checkpoint-backed runs recover integration --


_RUN_ID = "20260727-153045-a1b2c3d4"
_RECORDED_AT = "2026-07-27T15:30:45.123456Z"


def _writer_bytes(obj: dict) -> bytes:
    return (json.dumps(obj, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _activate_journal_with_checkpoint(
    workspace: Path,
    run_dir: Path,
    run_json_obj: dict,
    *,
    paired_event_type: str | None = "run.planning.started",
) -> None:
    """Build an activated lifecycle journal ending in a checkpoint event.

    Bootstraps run.json with the durable request, then under a live run lock
    activates the journal and appends one ``run.snapshot.checkpointed``
    event (the recoverable state: crash after the checkpoint publish+append
    but before the paired status append). Finally replaces the lock owner
    with a dead pid so ``runs_cmd.recover`` sees a stale matching lock.
    """
    runs_dir = run_dir.parent
    runs_dir.mkdir(parents=True, exist_ok=True)
    run_dir.mkdir(parents=True, exist_ok=True)
    localio = _localio()
    localio.write_json(
        run_dir / "run.json",
        {"schema": "brigade.run.v1", **run_json_obj, "lifecycle_journal_requested": True},
    )
    with runguard.run_lock(workspace, run_dir=run_dir):
        run_lifecycle.prepare_lifecycle_journal(run_dir, workspace=workspace)
        run_checkpoint.write_checkpoint(
            run_dir,
            _writer_bytes(run_json_obj),
            workspace=workspace,
            paired_event_type=paired_event_type,
        )
    _write_lock_owner(workspace, run_dir, pid=99999999)


def _activate_authority_journal_with_checkpoint(
    workspace: Path,
    run_dir: Path,
    run_json_obj: dict,
    *,
    paired_event_type: str | None = "run.planning.started",
) -> None:
    """Authority-journal mirror of ``_activate_journal_with_checkpoint``.

    Bootstraps run.json with BOTH durable request flags true and writes the
    checkpoint with ``body_kind="base-stripped"`` over a base that carries
    both request flags (the write path strips the journal metadata fields).
    Ends with a dead pid lock owner so ``runs_cmd.recover`` sees a stale
    matching lock.
    """
    runs_dir = run_dir.parent
    runs_dir.mkdir(parents=True, exist_ok=True)
    run_dir.mkdir(parents=True, exist_ok=True)
    base = {
        "schema": "brigade.run.v1",
        **run_json_obj,
        "lifecycle_journal_requested": True,
        "run_journal_authority_requested": True,
    }
    _localio().write_json(run_dir / "run.json", base)
    with runguard.run_lock(workspace, run_dir=run_dir):
        run_lifecycle.prepare_lifecycle_journal(run_dir, workspace=workspace)
        run_checkpoint.write_checkpoint(
            run_dir,
            _writer_bytes(base),
            workspace=workspace,
            paired_event_type=paired_event_type,
            body_kind="base-stripped",
        )
    _write_lock_owner(workspace, run_dir, pid=99999999)


def _localio():
    from brigade import localio

    return localio


def _journal_path(run_dir: Path) -> Path:
    return run_dir / "events" / "lifecycle.jsonl"


def _events(run_dir: Path) -> list:
    return run_journal.read_journal(_journal_path(run_dir)).events


def _write_checkpointed_parent_run(
    workspace: Path,
    run_dir: Path,
    *,
    extra: dict | None = None,
) -> list[run_journal.RunEvent]:
    run_dir.parent.mkdir(parents=True, exist_ok=True)
    run_dir.mkdir(parents=True, exist_ok=True)
    run_json_obj = {
        "schema": "brigade.run.v1",
        "schema_version": 1,
        "kind": "work",
        "task": "branchable parent",
        "cwd": str(workspace),
        "lock_workspace": str(workspace),
        "status": "planning",
        "started_at": "2026-08-17T12:00:00Z",
        "status_started_at": "2026-08-17T12:00:00Z",
        "read_only": False,
        "dry_run": False,
        "lifecycle_journal_requested": True,
        "run_journal_authority_requested": True,
    }
    if extra:
        run_json_obj.update(extra)
    _localio().write_json(run_dir / "run.json", run_json_obj)
    with runguard.run_lock(workspace, run_dir=run_dir):
        run_lifecycle.prepare_lifecycle_journal(run_dir, workspace=workspace)
        run_lifecycle.record_lifecycle_event(
            run_dir,
            event_type="run.created",
            payload={"status": "started"},
            idempotency_key="parent-created",
            workspace=workspace,
        )
        run_checkpoint.write_checkpoint(
            run_dir,
            _writer_bytes(run_json_obj),
            workspace=workspace,
            paired_event_type="run.planning.started",
            body_kind="base-stripped",
        )
        run_lifecycle.record_lifecycle_event(
            run_dir,
            event_type="run.planning.started",
            payload={"detail": "planning"},
            idempotency_key="parent-planning",
            workspace=workspace,
        )
    return _events(run_dir)


def _overwrite_lock_owner(workspace, run_dir, *, pid, owner_token="owner"):
    """Overwrite an existing run.lock's owner metadata in place (no mkdir)."""
    lock_path = workspace / ".brigade" / "run.lock"
    (lock_path / "pid").write_text(f"{pid}\n")
    _write_json(
        lock_path / "owner.json",
        {
            "schema": "brigade.run_lock.v1",
            "owner_token": owner_token,
            "pid": pid,
            "run_dir": str(run_dir.resolve()),
            "acquired_at": "2026-07-16T00:00:00+00:00",
        },
    )
    return lock_path


def test_runs_recover_restores_missing_run_json_from_latest_checkpoint(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / _RUN_ID
    run_json_obj = {
        "status": "planning",
        "task": "demo task",
        "cwd": str(workspace),
        "orchestrator": "chef",
    }
    _activate_journal_with_checkpoint(workspace, run_dir, run_json_obj)
    (run_dir / "run.json").unlink()
    assert not (run_dir / "run.json").exists()

    rc = runs_cmd.recover(str(run_dir), cwd=workspace)

    assert rc == 0
    recovered = json.loads((run_dir / "run.json").read_text())
    assert recovered["status"] == "failed"
    assert recovered["failure_phase"] == "stale-lock-recovery"
    # Restored receipt fields are preserved through terminalization.
    assert recovered["task"] == "demo task"
    assert recovered["orchestrator"] == "chef"
    assert f"recovered: {run_dir}" in capsys.readouterr().out


def test_runs_recover_preserves_corrupt_run_json_and_restores_from_checkpoint(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / _RUN_ID
    run_json_obj = {"status": "planning", "task": "demo task", "cwd": str(workspace)}
    _activate_journal_with_checkpoint(workspace, run_dir, run_json_obj)
    (run_dir / "run.json").write_text("not json")

    rc = runs_cmd.recover(str(run_dir), cwd=workspace)

    assert rc == 0
    recovered = json.loads((run_dir / "run.json").read_text())
    assert recovered["status"] == "failed"
    assert recovered["task"] == "demo task"
    preserved = list(run_dir.glob("run.json.corrupt-*"))
    assert len(preserved) == 1
    assert preserved[0].read_text() == "not json"


def test_runs_recover_terminalization_preserves_restored_receipt_fields(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / _RUN_ID
    run_json_obj = {
        "status": "dispatching",
        "task": "inspect",
        "cwd": str(workspace),
        "orchestrator": "chef",
        "active_seats": ["coder"],
        "duration_seconds": 5,
    }
    _activate_journal_with_checkpoint(workspace, run_dir, run_json_obj)
    (run_dir / "run.json").unlink()

    rc = runs_cmd.recover(str(run_dir), cwd=workspace)

    assert rc == 0
    recovered = json.loads((run_dir / "run.json").read_text())
    assert recovered["status"] == "failed"
    assert recovered["failure_phase"] == "stale-lock-recovery"
    assert recovered["task"] == "inspect"
    assert recovered["orchestrator"] == "chef"
    assert recovered["duration_seconds"] == 5
    assert recovered["failure"]["prior_status"] == "dispatching"


def test_runs_recover_quarantines_partial_journal_tail_then_verifies(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / _RUN_ID
    run_json_obj = {"status": "planning", "task": "demo", "cwd": str(workspace)}
    _activate_journal_with_checkpoint(workspace, run_dir, run_json_obj)
    (run_dir / "run.json").unlink()
    journal = _journal_path(run_dir)
    partial = b'{"schema":"brigade.run_event.v1","event_type":"run.plan'
    journal.write_bytes(journal.read_bytes() + partial)
    complete_bytes = journal.read_bytes()[: -len(partial)]

    rc = runs_cmd.recover(str(run_dir), cwd=workspace)

    assert rc == 0
    assert journal.read_bytes() == complete_bytes
    quarantine = list((run_dir / "events" / "quarantine").iterdir())
    assert quarantine, "partial tail was not quarantined"
    recovered = json.loads((run_dir / "run.json").read_text())
    assert recovered["status"] == "failed"


def test_runs_recover_fails_closed_on_invalid_latest_checkpoint(tmp_path, capsys):
    import hashlib

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / _RUN_ID
    run_json_obj = {"status": "planning", "task": "demo", "cwd": str(workspace)}
    _activate_journal_with_checkpoint(workspace, run_dir, run_json_obj)
    (run_dir / "run.json").unlink()
    sha = hashlib.sha256(_writer_bytes(run_json_obj)).hexdigest()
    cp_file = run_checkpoint.checkpoint_path(run_dir, sha)
    cp_file.write_bytes(b"x" * len(_writer_bytes(run_json_obj)))
    os.chmod(cp_file, 0o600)
    lock_path = workspace / ".brigade" / "run.lock"

    rc = runs_cmd.recover(str(run_dir), cwd=workspace)

    assert rc == 2
    assert not (run_dir / "run.json").exists()
    # The dead lock is restored (callback failed; lock restored by runguard).
    assert lock_path.is_dir()


def _activate_pending_dispatch_recovery_run(workspace: Path, run_dir: Path) -> None:
    run_json_obj = {"status": "dispatching", "task": "demo", "cwd": str(workspace)}
    _activate_journal_with_checkpoint(workspace, run_dir, run_json_obj)
    shutil.rmtree(workspace / ".brigade" / "run.lock")
    with runguard.run_lock(workspace, run_dir=run_dir):
        run_lifecycle.record_dispatch_fact(
            run_dir,
            workspace=workspace,
            event_type="run.dispatch.requested",
            seat="coder",
        )
    _write_lock_owner(workspace, run_dir, pid=99999999)


def test_runs_recover_prints_pending_dispatch_only_after_validated_recovery(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / _RUN_ID
    _activate_pending_dispatch_recovery_run(workspace, run_dir)

    rc = runs_cmd.recover(str(run_dir), cwd=workspace)

    assert rc == 0
    out = capsys.readouterr().out
    assert f"recovered: {run_dir}" in out
    assert "dispatch recovery: at-least-once work required (seat=coder, attempt=1)" in out


def test_runs_recover_invalid_pending_dispatch_checkpoint_prints_no_recovery_hint(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / _RUN_ID
    _activate_pending_dispatch_recovery_run(workspace, run_dir)
    latest = run_checkpoint.latest_checkpoint_event(run_journal.read_journal(_journal_path(run_dir)).events)
    assert latest is not None
    checkpoint_path = run_checkpoint.checkpoint_path(run_dir, latest.payload["sha256"])
    checkpoint_path.write_bytes(b"x" * latest.payload["byte_size"])

    rc = runs_cmd.recover(str(run_dir), cwd=workspace)

    captured = capsys.readouterr()
    assert rc == 2
    assert "dispatch recovery:" not in captured.out


def test_runs_recover_accepts_covered_paired_status_event_and_preserves_fields(tmp_path, capsys):
    """CLI recovery accepts a checkpoint N plus matching status N+1 and preserves fields.

    The checkpoint at N carries ``paired_event_type`` for the checkpointed
    status, and the N+1 event is the matching paired status event whose
    derived status equals the checkpoint status. Per the slice-5 coverage
    semantics this is a covered tail, so recovery restores the checkpoint
    bytes and terminalizes, preserving the restored receipt fields
    (``task``, ``orchestrator``, ``cwd``, ``active_seats``,
    ``duration_seconds``) and stamping the stale-lock-recovery failure.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / _RUN_ID
    run_json_obj = {
        "status": "dispatching",
        "task": "inspect",
        "cwd": str(workspace),
        "orchestrator": "chef",
        "active_seats": ["coder"],
        "duration_seconds": 5,
    }
    # Build the journal with a checkpoint AND its matching paired status event
    # (covered tail) under a live lock, then replace the owner with a dead pid.
    runs_dir = run_dir.parent
    runs_dir.mkdir(parents=True, exist_ok=True)
    run_dir.mkdir(parents=True, exist_ok=True)
    _localio().write_json(
        run_dir / "run.json",
        {"schema": "brigade.run.v1", **run_json_obj, "lifecycle_journal_requested": True},
    )
    with runguard.run_lock(workspace, run_dir=run_dir):
        run_lifecycle.prepare_lifecycle_journal(run_dir, workspace=workspace)
        run_checkpoint.write_checkpoint(
            run_dir,
            _writer_bytes(run_json_obj),
            workspace=workspace,
            paired_event_type="run.dispatch.requested",
        )
        run_journal.append_event(
            _journal_path(run_dir),
            run_id=_RUN_ID,
            event_type="run.dispatch.requested",
            payload={"detail": "dispatching"},
            idempotency_key="dispatch-req-1",
            expected_previous_sequence=1,
            recorded_at="2026-07-27T15:30:46.000000Z",
        )
    _write_lock_owner(workspace, run_dir, pid=99999999)
    (run_dir / "run.json").unlink()

    rc = runs_cmd.recover(str(run_dir), cwd=workspace)

    assert rc == 0
    recovered = json.loads((run_dir / "run.json").read_text())
    assert recovered["status"] == "failed"
    assert recovered["failure_phase"] == "stale-lock-recovery"
    # Restoration and terminalization field preservation.
    assert recovered["task"] == "inspect"
    assert recovered["orchestrator"] == "chef"
    assert recovered["cwd"] == str(workspace)
    assert recovered["active_seats"] == ["coder"]
    assert recovered["duration_seconds"] == 5
    assert recovered["failure"]["prior_status"] == "dispatching"
    assert f"recovered: {run_dir}" in capsys.readouterr().out


def test_runs_recover_fails_closed_on_uncovered_tail_wrong_event_type(tmp_path, capsys):
    """CLI recovery fails closed when the N+1 event_type is not the checkpoint's pair.

    A checkpoint at N with ``paired_event_type`` set, followed by an N+1
    event whose ``event_type`` is not the paired type, is an uncovered tail.
    Recovery exits 2, restores no run.json, and the runguard claim is
    restored (lock dir remains).
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / _RUN_ID
    run_json_obj = {"status": "planning", "task": "demo", "cwd": str(workspace)}
    runs_dir = run_dir.parent
    runs_dir.mkdir(parents=True, exist_ok=True)
    run_dir.mkdir(parents=True, exist_ok=True)
    _localio().write_json(
        run_dir / "run.json",
        {"schema": "brigade.run.v1", **run_json_obj, "lifecycle_journal_requested": True},
    )
    with runguard.run_lock(workspace, run_dir=run_dir):
        run_lifecycle.prepare_lifecycle_journal(run_dir, workspace=workspace)
        run_checkpoint.write_checkpoint(
            run_dir,
            _writer_bytes(run_json_obj),
            workspace=workspace,
            paired_event_type="run.planning.started",
        )
        run_journal.append_event(
            _journal_path(run_dir),
            run_id=_RUN_ID,
            event_type="run.dispatch.requested",
            payload={"detail": "dispatching"},
            idempotency_key="dispatch-1",
            expected_previous_sequence=1,
            recorded_at="2026-07-27T15:30:46.000000Z",
        )
    _write_lock_owner(workspace, run_dir, pid=99999999)
    (run_dir / "run.json").unlink()
    lock_path = workspace / ".brigade" / "run.lock"

    rc = runs_cmd.recover(str(run_dir), cwd=workspace)

    assert rc == 2
    assert not (run_dir / "run.json").exists()
    assert lock_path.is_dir()


def test_runs_recover_fails_closed_on_uncovered_tail_with_parseable_run_json(tmp_path, capsys):
    """An uncovered tail fails closed even when run.json is parseable; run.json is untouched."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / _RUN_ID
    run_json_obj = {"status": "planning", "task": "demo", "cwd": str(workspace)}
    runs_dir = run_dir.parent
    runs_dir.mkdir(parents=True, exist_ok=True)
    run_dir.mkdir(parents=True, exist_ok=True)
    _localio().write_json(
        run_dir / "run.json",
        {"schema": "brigade.run.v1", **run_json_obj, "lifecycle_journal_requested": True},
    )
    with runguard.run_lock(workspace, run_dir=run_dir):
        run_lifecycle.prepare_lifecycle_journal(run_dir, workspace=workspace)
        run_checkpoint.write_checkpoint(
            run_dir,
            _writer_bytes(run_json_obj),
            workspace=workspace,
            paired_event_type="run.planning.started",
        )
        run_journal.append_event(
            _journal_path(run_dir),
            run_id=_RUN_ID,
            event_type="run.dispatch.requested",
            payload={"detail": "dispatching"},
            idempotency_key="dispatch-1",
            expected_previous_sequence=1,
            recorded_at="2026-07-27T15:30:46.000000Z",
        )
    _write_lock_owner(workspace, run_dir, pid=99999999)
    parseable = {"schema": "brigade.run.v1", "status": "planning", "task": "different", "cwd": str(workspace)}
    _write_json(run_dir / "run.json", parseable)
    before = (run_dir / "run.json").read_bytes()
    lock_path = workspace / ".brigade" / "run.lock"

    rc = runs_cmd.recover(str(run_dir), cwd=workspace)

    assert rc == 2
    assert (run_dir / "run.json").read_bytes() == before
    assert lock_path.is_dir()
    assert not list(run_dir.glob("run.json.corrupt-*"))


def test_runs_recover_fails_closed_on_chain_break(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / _RUN_ID
    run_json_obj = {"status": "planning", "task": "demo", "cwd": str(workspace)}
    _activate_journal_with_checkpoint(workspace, run_dir, run_json_obj)
    (run_dir / "run.json").unlink()
    journal = _journal_path(run_dir)
    events = _events(run_dir)
    env = events[0].to_dict()
    env["sequence"] = 3
    env["event_digest"] = run_events.compute_event_digest(env)
    env["event_id"] = run_events.make_event_id(
        run_id=env["run_id"], sequence=env["sequence"], event_digest=env["event_digest"]
    )
    journal.write_bytes(run_events.canonical_bytes(env) + b"\n")

    rc = runs_cmd.recover(str(run_dir), cwd=workspace)

    assert rc == 2
    assert not (run_dir / "run.json").exists()


def test_runs_recover_fails_closed_on_envelope_run_id_mismatch(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / _RUN_ID
    run_json_obj = {"status": "planning", "task": "demo", "cwd": str(workspace)}
    _activate_journal_with_checkpoint(workspace, run_dir, run_json_obj)
    (run_dir / "run.json").unlink()
    journal = _journal_path(run_dir)
    events = _events(run_dir)
    env = events[0].to_dict()
    env["run_id"] = "a-different-run-id"
    env["event_digest"] = run_events.compute_event_digest(env)
    env["event_id"] = run_events.make_event_id(
        run_id=env["run_id"], sequence=env["sequence"], event_digest=env["event_digest"]
    )
    journal.write_bytes(run_events.canonical_bytes(env) + b"\n")

    rc = runs_cmd.recover(str(run_dir), cwd=workspace)

    assert rc == 2
    assert not (run_dir / "run.json").exists()


def test_runs_recover_refuses_live_owner_without_mutation_activated(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / _RUN_ID
    run_json_obj = {"status": "planning", "task": "demo", "cwd": str(workspace)}
    _activate_journal_with_checkpoint(workspace, run_dir, run_json_obj)
    # Replace the dead owner with the current live pid.
    _overwrite_lock_owner(workspace, run_dir, pid=os.getpid())
    before = (run_dir / "run.json").read_bytes()
    lock_path = workspace / ".brigade" / "run.lock"

    rc = runs_cmd.recover(str(run_dir), cwd=workspace)

    assert rc == 2
    assert (run_dir / "run.json").read_bytes() == before
    assert lock_path.is_dir()
    assert "run owner process is still active" in capsys.readouterr().err


def test_runs_recover_refuses_foreign_pid_without_mutation_activated(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / _RUN_ID
    run_json_obj = {"status": "planning", "task": "demo", "cwd": str(workspace)}
    _activate_journal_with_checkpoint(workspace, run_dir, run_json_obj)
    other_run = workspace / ".brigade" / "runs" / "other-run"
    _overwrite_lock_owner(workspace, other_run, pid=99999999)
    before = (run_dir / "run.json").read_bytes()
    lock_path = workspace / ".brigade" / "run.lock"

    rc = runs_cmd.recover(str(run_dir), cwd=workspace)

    assert rc == 2
    assert (run_dir / "run.json").read_bytes() == before
    assert lock_path.is_dir()
    assert "run lock belongs to a different run" in capsys.readouterr().err


def test_runs_recover_refuses_invalid_lock_without_mutation(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / _RUN_ID
    run_json_obj = {"status": "planning", "task": "demo", "cwd": str(workspace)}
    _activate_journal_with_checkpoint(workspace, run_dir, run_json_obj)
    # Corrupt the lock: remove owner.json so run_lock_state returns "invalid".
    lock_path = workspace / ".brigade" / "run.lock"
    (lock_path / "owner.json").unlink()
    before = (run_dir / "run.json").read_bytes()

    rc = runs_cmd.recover(str(run_dir), cwd=workspace)

    assert rc == 2
    assert (run_dir / "run.json").read_bytes() == before
    assert lock_path.is_dir()


def test_runs_recover_terminal_run_with_activated_journal_unchanged(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / _RUN_ID
    run_json_obj = {"status": "planning", "task": "demo", "cwd": str(workspace)}
    _activate_journal_with_checkpoint(workspace, run_dir, run_json_obj)
    # Mark the run terminal with a stale-lock-recovery failure phase.
    run_meta = json.loads((run_dir / "run.json").read_text())
    run_meta.update(
        {
            "status": "failed",
            "failure_phase": "stale-lock-recovery",
            "finished_at": "2026-07-27T15:31:00Z",
            "failure": {
                "phase": "stale-lock-recovery",
                "kind": "owner-process-exited",
                "detail": "run owner process 99999999 is no longer active",
            },
        }
    )
    _write_json(run_dir / "run.json", run_meta)

    rc = runs_cmd.recover(str(run_dir), cwd=workspace)

    assert rc == 0
    out = capsys.readouterr().out
    assert f"already terminal: {run_dir} [failed]" in out


def test_runs_recover_no_journal_legacy_dead_owner_marks_terminal(tmp_path, capsys):
    # A nonterminal run with a dead matching lock but NO activated journal
    # keeps the legacy recovery path (no checkpoint callback).
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / "legacy"
    _write_minimal_run(
        run_dir,
        task="legacy task",
        status="dispatching",
        started_at="2026-07-16T00:00:00Z",
    )
    run_meta = json.loads((run_dir / "run.json").read_text())
    run_meta["cwd"] = str(workspace)
    _write_json(run_dir / "run.json", run_meta)
    _write_lock_owner(workspace, run_dir)

    rc = runs_cmd.recover(str(run_dir), cwd=workspace)

    assert rc == 0
    assert json.loads((run_dir / "run.json").read_text())["status"] == "failed"
    assert f"recovered: {run_dir}" in capsys.readouterr().out


# -- Issue #568 slice 5 Task 5 sendback: bounded checkpoint reason surfacing --


def test_runs_recover_invalid_checkpoint_surfaces_bounded_reason_and_preserves_lock(tmp_path, capsys):
    """Invalid checkpoint recovery surfaces the bounded checkpoint reason.

    runguard wraps every ``before_terminalize`` callback exception into a
    ``RunLockError`` after restoring the claimed lock, so
    ``_recover_from_checkpoint`` cannot catch ``CheckpointError`` directly.
    It must preserve the original bounded ``CheckpointError`` in a local holder,
    catch the ``RunLockError``, and print the stored checkpoint diagnostic
    while still returning exit 2 and leaving the lock restored.
    """
    import hashlib

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / _RUN_ID
    run_json_obj = {"status": "planning", "task": "demo", "cwd": str(workspace)}
    _activate_journal_with_checkpoint(workspace, run_dir, run_json_obj)
    (run_dir / "run.json").unlink()
    sha = hashlib.sha256(_writer_bytes(run_json_obj)).hexdigest()
    cp_file = run_checkpoint.checkpoint_path(run_dir, sha)
    cp_file.write_bytes(b"x" * len(_writer_bytes(run_json_obj)))
    os.chmod(cp_file, 0o600)
    lock_path = workspace / ".brigade" / "run.lock"

    rc = runs_cmd.recover(str(run_dir), cwd=workspace)

    assert rc == 2
    assert not (run_dir / "run.json").exists()
    # The dead lock is restored (callback failed; lock restored by runguard).
    assert lock_path.is_dir()
    # The specific bounded checkpoint reason is surfaced to stderr, not the
    # generic runguard "before_terminalize callback failed" wrapper.
    err = capsys.readouterr().err
    assert "checkpoint digest mismatch" in err
    assert "before_terminalize callback failed" not in err


# -- Issue #568 slice 5 Task 5 second sendback: run.json input edges --


def test_runs_recover_preserves_non_utf8_run_json_and_restores_from_checkpoint(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / _RUN_ID
    run_json_obj = {"status": "planning", "task": "demo task", "cwd": str(workspace)}
    _activate_journal_with_checkpoint(workspace, run_dir, run_json_obj)
    corrupt_bytes = b"\xff\xfe not utf-8"
    (run_dir / "run.json").write_bytes(corrupt_bytes)

    rc = runs_cmd.recover(str(run_dir), cwd=workspace)

    assert rc == 0
    recovered = json.loads((run_dir / "run.json").read_text())
    assert recovered["status"] == "failed"
    assert recovered["task"] == "demo task"
    preserved = list(run_dir.glob("run.json.corrupt-*"))
    assert len(preserved) == 1
    assert preserved[0].read_bytes() == corrupt_bytes
    assert f"recovered: {run_dir}" in capsys.readouterr().out


def test_runs_recover_preserves_recursion_error_run_json_and_restores_from_checkpoint(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / _RUN_ID
    run_json_obj = {"status": "planning", "task": "demo task", "cwd": str(workspace)}
    _activate_journal_with_checkpoint(workspace, run_dir, run_json_obj)
    depth = 20000
    deep_json = ("[" * depth) + ("]" * depth)
    (run_dir / "run.json").write_text(deep_json)

    rc = runs_cmd.recover(str(run_dir), cwd=workspace)

    assert rc == 0
    recovered = json.loads((run_dir / "run.json").read_text())
    assert recovered["status"] == "failed"
    assert recovered["task"] == "demo task"
    preserved = list(run_dir.glob("run.json.corrupt-*"))
    assert len(preserved) == 1
    assert preserved[0].read_text() == deep_json
    assert f"recovered: {run_dir}" in capsys.readouterr().out


def test_runs_recover_run_json_read_oserror_exits_without_mutation(tmp_path, capsys, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / _RUN_ID
    run_json_obj = {"status": "planning", "task": "demo", "cwd": str(workspace)}
    _activate_journal_with_checkpoint(workspace, run_dir, run_json_obj)
    run_json = run_dir / "run.json"
    before_run_json = run_json.read_bytes()
    lock_path = workspace / ".brigade" / "run.lock"
    lock_pid_before = (lock_path / "pid").read_text()
    lock_owner_before = (lock_path / "owner.json").read_text()

    real_read_text = Path.read_text

    def fail_read_text(self, *args, **kwargs):
        if self == run_json:
            raise OSError(errno.EACCES, "read denied")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_read_text)

    rc = runs_cmd.recover(str(run_dir), cwd=workspace)

    assert rc == 2
    assert run_json.read_bytes() == before_run_json
    assert lock_path.is_dir()
    assert (lock_path / "pid").read_text() == lock_pid_before
    assert (lock_path / "owner.json").read_text() == lock_owner_before
    assert "could not read run.json" in capsys.readouterr().err


def test_runs_recover_proceeds_under_foreign_lock_with_matching_stale_claim(tmp_path, capsys):
    """CLI recovery proceeds under a foreign current lock when a matching persistent
    ``.stale`` claim exists, recovering only the matching claim via the checkpoint
    path and leaving the foreign lock byte-for-byte and path-state unchanged.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / _RUN_ID
    run_json_obj = {"status": "planning", "task": "demo task", "cwd": str(workspace)}
    _activate_journal_with_checkpoint(workspace, run_dir, run_json_obj)
    lock_path = workspace / ".brigade" / "run.lock"
    # Relocate the matching dead lock to a persistent .stale claim.
    stale = lock_path.with_name(f".{lock_path.name}.crashed.stale")
    lock_path.rename(stale)
    # Install a FOREIGN dead lock at the lock path (different run_dir).
    foreign_run = workspace / ".brigade" / "runs" / "foreign-run"
    foreign_run.mkdir(parents=True)
    _write_lock_owner(workspace, foreign_run, pid=99999999, owner_token="foreign")
    foreign_owner_bytes = (lock_path / "owner.json").read_bytes()
    foreign_pid_bytes = (lock_path / "pid").read_bytes()

    rc = runs_cmd.recover(str(run_dir), cwd=workspace)

    assert rc == 0
    out = capsys.readouterr().out
    assert f"recovered: {run_dir}" in out
    # The foreign lock is byte-for-byte and path-state unchanged.
    assert lock_path.is_dir()
    assert (lock_path / "owner.json").read_bytes() == foreign_owner_bytes
    assert (lock_path / "pid").read_bytes() == foreign_pid_bytes
    # The matching .stale claim was cleared.
    assert not stale.exists()
    # The run was terminalized via the checkpoint recovery path.
    recovered = json.loads((run_dir / "run.json").read_text())
    assert recovered["status"] == "failed"
    assert recovered["failure_phase"] == "stale-lock-recovery"
    assert recovered["task"] == "demo task"


def test_runs_recover_proceeds_under_invalid_lock_with_matching_stale_claim(tmp_path, capsys):
    """CLI recovery proceeds under an invalid current lock when a matching persistent
    ``.stale`` claim exists, recovering only the matching claim and leaving the invalid
    lock path-state unchanged (still a directory with no owner.json).
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / _RUN_ID
    run_json_obj = {"status": "planning", "task": "demo task", "cwd": str(workspace)}
    _activate_journal_with_checkpoint(workspace, run_dir, run_json_obj)
    lock_path = workspace / ".brigade" / "run.lock"
    stale = lock_path.with_name(f".{lock_path.name}.crashed.stale")
    lock_path.rename(stale)
    # Install an INVALID lock at the lock path: directory with no owner.json.
    lock_path.mkdir(parents=True)
    (lock_path / "pid").write_text(f"{os.getpid()}\n")
    invalid_pid_bytes = (lock_path / "pid").read_bytes()

    rc = runs_cmd.recover(str(run_dir), cwd=workspace)

    assert rc == 0
    out = capsys.readouterr().out
    assert f"recovered: {run_dir}" in out
    # The invalid lock is path-state unchanged.
    assert lock_path.is_dir()
    assert not (lock_path / "owner.json").exists()
    assert (lock_path / "pid").read_bytes() == invalid_pid_bytes
    assert not stale.exists()
    recovered = json.loads((run_dir / "run.json").read_text())
    assert recovered["status"] == "failed"
    assert recovered["failure_phase"] == "stale-lock-recovery"
    assert recovered["task"] == "demo task"


def test_runs_recover_lost_matching_claim_under_foreign_lock_is_bounded(tmp_path, monkeypatch, capsys):
    """When the matching ``.stale`` claim disappears between preflight and recovery,
    CLI recovery returns bounded rc=2 without mutating the foreign lock or run.json.
    """
    import shutil

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / _RUN_ID
    run_json_obj = {"status": "planning", "task": "demo task", "cwd": str(workspace)}
    _activate_journal_with_checkpoint(workspace, run_dir, run_json_obj)
    lock_path = workspace / ".brigade" / "run.lock"
    stale = lock_path.with_name(f".{lock_path.name}.crashed.stale")
    lock_path.rename(stale)
    foreign_run = workspace / ".brigade" / "runs" / "foreign-run"
    foreign_run.mkdir(parents=True)
    _write_lock_owner(workspace, foreign_run, pid=99999999, owner_token="foreign")
    foreign_owner_bytes = (lock_path / "owner.json").read_bytes()
    run_json_before = (run_dir / "run.json").read_bytes()

    real_recover = runguard.recover_stale_run

    def lose_claim(cwd, requested_run, **kwargs):
        if stale.exists():
            shutil.rmtree(stale, ignore_errors=True)
        return real_recover(cwd, requested_run, **kwargs)

    monkeypatch.setattr(runguard, "recover_stale_run", lose_claim)

    rc = runs_cmd.recover(str(run_dir), cwd=workspace)

    assert rc == 2
    assert (lock_path / "owner.json").read_bytes() == foreign_owner_bytes
    assert (run_dir / "run.json").read_bytes() == run_json_before
    err = capsys.readouterr().err
    assert "run lock not found for run:" in err
    assert "Traceback" not in err


def test_runs_recover_restores_from_retained_lock_after_initial_run_json_write_failure(tmp_path, monkeypatch, capsys):
    """``runs_cmd.recover`` restores a run whose first ``run.json`` write failed
    after checkpoint publication, leaving the lock retained with durable
    journal/checkpoint state but no ``run.json``.

    This is the end-to-end recovery assertion for the
    ``RetainRunLockError`` translation in ``aboyeur.record_run_start``: the lock
    is retained (not orphaned), the lifecycle journal and recovery checkpoint
    survive, and ``runs_cmd.recover`` terminalizes the run from the retained
    state.
    """
    from brigade import aboyeur
    from brigade.roster import Agent, Roster

    monkeypatch.setenv("BRIGADE_LIFECYCLE_JOURNAL", "1")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / _RUN_ID
    run_dir.mkdir(parents=True)

    real_write_text_atomic = aboyeur.localio.write_text_atomic

    def fail_run_json_write(path, text):
        if Path(path).name == "run.json":
            raise OSError("receipt disk full")
        return real_write_text_atomic(path, text)

    monkeypatch.setattr(aboyeur.localio, "write_text_atomic", fail_run_json_write)

    roster = Roster(
        orchestrator="chef",
        agents={
            "chef": Agent("chef", "codex", "plan and synthesize"),
            "coder": Agent("coder", "ollama:llama3.3", "write code"),
        },
        max_workers=1,
    )

    with pytest.raises(runguard.RetainRunLockError):
        with runguard.run_lock(workspace, run_dir=run_dir):
            aboyeur.record_run_start(
                run_dir,
                task="demo task",
                cwd=workspace,
                roster=roster,
                read_only=False,
                lock_workspace=workspace,
            )

    # Pre-conditions: lock retained, run.json absent, durable state present.
    lock_path = workspace / ".brigade" / "run.lock"
    assert lock_path.is_dir()
    assert not (run_dir / "run.json").is_file()
    assert run_lifecycle._journal_path(run_dir).is_file()
    assert any(run_checkpoint.checkpoint_dir(run_dir).glob("*.json"))

    # Mark the retained lock owner dead so recovery can claim it.
    _overwrite_lock_owner(workspace, run_dir, pid=99999999)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: False)

    # Stop failing the write so recovery can write the terminal receipt.
    monkeypatch.setattr(aboyeur.localio, "write_text_atomic", real_write_text_atomic)

    rc = runs_cmd.recover(str(run_dir), cwd=workspace)

    assert rc == 0
    out = capsys.readouterr().out
    assert f"recovered: {run_dir}" in out
    recovered = json.loads((run_dir / "run.json").read_text())
    assert recovered["status"] == "failed"
    assert recovered["failure_phase"] == "stale-lock-recovery"
    assert recovered["task"] == "demo task"
    assert not lock_path.exists()


# -- Issue #568 slice 6: authority-aware base-stripped runs recover ------------


def test_runs_recover_authority_checkpoint_terminalizes_preserving_fields_and_provenance(tmp_path, capsys):
    """runs recover over a base-stripped authority checkpoint.

    With run.json missing, recovery projects the validated stripped base over
    the verified journal events and terminalizes the projected receipt: the
    base fields (task/cwd/orchestrator), the authority request flag, and the
    current projector metadata survive, and the stale-lock-recovery failure
    provenance records the projected prior status.
    """
    from brigade import run_projector

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / _RUN_ID
    run_json_obj = {
        "status": "dispatching",
        "task": "inspect",
        "cwd": str(workspace),
        "orchestrator": "chef",
        "active_seats": ["coder"],
        "duration_seconds": 5,
    }
    _activate_authority_journal_with_checkpoint(workspace, run_dir, run_json_obj)
    (run_dir / "run.json").unlink()

    rc = runs_cmd.recover(str(run_dir), cwd=workspace)

    assert rc == 0
    recovered = json.loads((run_dir / "run.json").read_text())
    assert recovered["status"] == "failed"
    assert recovered["failure_phase"] == "stale-lock-recovery"
    # Restored (projected) receipt fields preserved through terminalization.
    assert recovered["task"] == "inspect"
    assert recovered["cwd"] == str(workspace)
    assert recovered["orchestrator"] == "chef"
    assert recovered["active_seats"] == ["coder"]
    assert recovered["duration_seconds"] == 5
    assert recovered["run_journal_authority_requested"] is True
    # Recovery provenance: the failure records the projected prior status.
    assert recovered["failure"]["prior_status"] == "dispatching"
    # Projector metadata proves the authority projection path ran (a verbatim
    # restore of the stripped body would carry none of these).
    assert recovered["projector_version"] == run_projector.PROJECTOR_VERSION
    assert recovered["journal_present"] is True
    assert recovered["journal_last_sequence"] == 1
    assert f"recovered: {run_dir}" in capsys.readouterr().out


def test_runs_recover_authority_checkpoint_preserves_corrupt_run_json(tmp_path, capsys):
    """A corrupt run.json is preserved by rename, then replaced by the projection."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / _RUN_ID
    run_json_obj = {"status": "planning", "task": "demo task", "cwd": str(workspace)}
    _activate_authority_journal_with_checkpoint(workspace, run_dir, run_json_obj)
    (run_dir / "run.json").write_text("not json")

    rc = runs_cmd.recover(str(run_dir), cwd=workspace)

    assert rc == 0
    recovered = json.loads((run_dir / "run.json").read_text())
    assert recovered["status"] == "failed"
    assert recovered["task"] == "demo task"
    assert recovered["journal_present"] is True
    preserved = list(run_dir.glob("run.json.corrupt-*"))
    assert len(preserved) == 1
    assert preserved[0].read_text() == "not json"
    assert f"recovered: {run_dir}" in capsys.readouterr().out


def test_runs_recover_authority_projection_failure_fails_closed(tmp_path, monkeypatch, capsys):
    """A failed authority projection maps to exit 2 with the bounded reason.

    The runguard claim is restored (lock dir remains), no run.json is
    restored, and the surfaced stderr reason is the bounded checkpoint
    projection diagnostic, not the raw projector error.
    """
    from brigade import run_projector

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / _RUN_ID
    run_json_obj = {"status": "planning", "task": "demo", "cwd": str(workspace)}
    _activate_authority_journal_with_checkpoint(workspace, run_dir, run_json_obj)
    (run_dir / "run.json").unlink()
    lock_path = workspace / ".brigade" / "run.lock"

    def boom(base_snapshot, events, *, journal_present):
        raise run_projector.ProjectionError("raw projector detail")

    monkeypatch.setattr(run_projector, "project_run_snapshot", boom)

    rc = runs_cmd.recover(str(run_dir), cwd=workspace)

    assert rc == 2
    assert not (run_dir / "run.json").exists()
    # The dead lock is restored (callback failed; lock restored by runguard).
    assert lock_path.is_dir()
    err = capsys.readouterr().err
    assert "projection failed" in err
    assert "raw projector detail" not in err


def test_runs_inspect_shows_child_lineage_and_children(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runs_root = workspace / ".brigade" / "runs"
    parent_dir = runs_root / "20260817-120000-parent-aaaaaa"
    events = _write_checkpointed_parent_run(workspace, parent_dir)

    assert runs_cmd.child(parent_dir.name, events[-1].event_id, cwd=workspace) == 0
    out = capsys.readouterr().out.strip().splitlines()
    child_dir = Path(out[-1].split("child: ", 1)[1])
    capsys.readouterr()

    assert runs_cmd.inspect(parent_dir.name, cwd=workspace) == 0
    out = capsys.readouterr().out
    assert f"run: {parent_dir}" in out
    assert f"run id: {parent_dir.name}" in out
    assert "children:" in out
    assert child_dir.name in out
    assert "terminal:" in out

    assert runs_cmd.inspect(child_dir, cwd=workspace) == 0
    out = capsys.readouterr().out
    assert "kind: child" in out
    assert f"parent run: {parent_dir.name}" in out
    assert f"branch point: {events[-1].event_id}" in out


def test_runs_inspect_legacy_root_has_no_lineage(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    runs_root = workspace / ".brigade" / "runs"
    _write_minimal_run(
        runs_root / "legacy",
        task="legacy task",
        status="ok",
        started_at="2026-05-26T14:00:00Z",
    )

    assert runs_cmd.inspect("legacy", cwd=workspace) == 0
    out = capsys.readouterr().out
    assert "lineage:" not in out
    assert "terminal: yes" in out


def test_runs_inspect_running_run_is_not_terminal(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    runs_root = workspace / ".brigade" / "runs"
    _write_minimal_run(
        runs_root / "active",
        task="running task",
        status="running",
        started_at="2026-05-26T14:00:00Z",
    )

    assert runs_cmd.inspect("active", cwd=workspace) == 0
    assert "terminal: no" in capsys.readouterr().out


def test_runs_inspect_reports_unknown_run(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    (workspace / ".brigade" / "runs").mkdir(parents=True)

    assert runs_cmd.inspect("no-such-run", cwd=workspace) == 2
    assert "run directory not found" in capsys.readouterr().err


def test_runs_inspect_reports_corrupt_run_json(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    runs_root = workspace / ".brigade" / "runs"
    runs_root.mkdir(parents=True)
    (runs_root / "broken").mkdir()
    (runs_root / "broken" / "run.json").write_text("not json")

    assert runs_cmd.inspect("broken", cwd=workspace) == 2
    assert "run.json is not valid JSON" in capsys.readouterr().err


def test_runs_inspect_reports_corrupt_own_journal(tmp_path, capsys):
    from brigade import run_lifecycle

    workspace = tmp_path / "workspace"
    runs_root = workspace / ".brigade" / "runs"
    run_dir = runs_root / "broken"
    _write_minimal_run(run_dir, task="task", status="ok", started_at="2026-08-17T13:00:00Z")
    journal = run_lifecycle._journal_path(run_dir)
    journal.parent.mkdir(parents=True)
    journal.write_bytes(b'{"partial":')

    assert runs_cmd.inspect(run_dir.name, cwd=workspace) == 2
    assert "lifecycle journal" in capsys.readouterr().err


def test_runs_show_prints_terminal_line(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    runs_root = workspace / ".brigade" / "runs"
    done = runs_root / "done"
    _write_minimal_run(done, task="finished", status="ok", started_at="2026-05-26T14:00:00Z")
    active = runs_root / "active"
    _write_minimal_run(active, task="in flight", status="running", started_at="2026-05-26T15:00:00Z")

    assert runs_cmd.show(done) == 0
    assert "terminal: yes" in capsys.readouterr().out

    assert runs_cmd.show(active) == 0
    assert "terminal: no" in capsys.readouterr().out
