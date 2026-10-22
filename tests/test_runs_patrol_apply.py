"""Explicit patrol actions preserve reaper and claim fences."""

from __future__ import annotations

import json
import os
from datetime import timedelta

import pytest

from brigade import cli, localio, run_journal, runguard, runs_patrol
from tests.test_run_reap import JOURNAL_REQUESTED, NOW, _repo, _write_run, _write_stale_lock
from tests.test_runs_patrol import _ledger


def _patrol(repo, capsys, *, apply, now=NOW, **kwargs):
    code = runs_patrol.patrol(target=repo, now=now, json_output=True, apply=apply, **kwargs)
    captured = capsys.readouterr()
    assert code == 0, captured.err
    return json.loads(captured.out)


def test_apply_reap_uses_journal_writer_and_is_repeatable(tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path)
    run = _write_run(repo, "20260820-120000-patrol01", extra=JOURNAL_REQUESTED)
    run_journal.append_event(
        run / "events/lifecycle.jsonl",
        run_id=run.name,
        event_type="run.resumed",
        payload={},
        idempotency_key="started",
        expected_previous_sequence=0,
        recorded_at="2026-08-20T12:00:00.000000Z",
    )
    _write_stale_lock(repo, run)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: False)
    report = _patrol(repo, capsys, apply="reap")
    assert json.loads((run / "run.json").read_text())["status"] == "orphaned"
    events = [json.loads(line) for line in (run / "events/lifecycle.jsonl").read_text().splitlines()]
    assert events[-1]["event_type"] == "run.orphaned"
    action = report["actions"][0]
    assert action["action"] == "reap" and action["status"] == "applied"
    assert action["before"]["state"] == "dead" and action["after"]["state"] == "orphaned"
    assert len(action["idempotency_key"]) == 64
    assert json.loads((repo / report["receipt"]).read_text()) == report
    before = (run / "events/lifecycle.jsonl").read_bytes()
    again = _patrol(repo, capsys, apply="reap")
    assert not any(item["status"] == "applied" for item in again["actions"])
    assert (run / "events/lifecycle.jsonl").read_bytes() == before


@pytest.mark.parametrize("live,foreign", [(True, False), (False, True)])
def test_apply_never_reaps_live_or_foreign_lock(tmp_path, monkeypatch, capsys, live, foreign):
    repo = _repo(tmp_path)
    run = _write_run(repo, "20260820-120000-patrol01")
    lock = _write_stale_lock(repo, run)
    if foreign:
        owner = json.loads((lock / "owner.json").read_text())
        owner["run_dir"] = str(run.parent / "20260820-120000-foreign1")
        localio.write_json(lock / "owner.json", owner)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: live)
    before = (run / "run.json").read_bytes(), (lock / "owner.json").read_bytes()
    report = _patrol(repo, capsys, apply="reap,release-claim")
    assert not any(item["status"] == "applied" for item in report["actions"])
    assert ((run / "run.json").read_bytes(), (lock / "owner.json").read_bytes()) == before


def test_release_claim_for_terminal_run(tmp_path, capsys):
    repo = _repo(tmp_path)
    _write_run(repo, "20260820-120000-patrol01", status="ok", extra={"claim_id": "claim-1"})
    _ledger(repo)
    report = _patrol(repo, capsys, apply="release-claim")
    task = json.loads((repo / ".brigade/work/tasks.json").read_text())["tasks"][0]
    assert task.get("claim") is None
    assert task["updated_at"]
    action = next(item for item in report["actions"] if item["action"] == "release-claim")
    assert action["status"] == "applied"
    assert action["before"]["claim_id"] == "claim-1" and action["after"]["claim_id"] is None


@pytest.mark.parametrize("state,status", [("completed", "ok"), ("failed", "failed")])
@pytest.mark.parametrize("scope", ["ledger", "hub"])
def test_release_preserves_dispatch_intent_ownership(tmp_path, monkeypatch, capsys, state, status, scope):
    from brigade import fleet_claim_release, fleet_client

    repo = _repo(tmp_path)
    run = _write_run(repo, "20260820-120000-patrol01", status=status, extra={"claim_id": "claim-1"})
    path = _ledger(repo)
    intent = repo / ".brigade/dispatch/dispatch-example/intents/intent-1.json"
    intent.parent.mkdir(parents=True)
    localio.write_json(
        intent,
        {"intent_id": "intent-1", "run_id": run.name, "task_id": "task-1", "claim_id": "claim-1", "state": state},
    )
    before = path.read_bytes(), intent.read_bytes()
    calls = []
    monkeypatch.setattr(
        fleet_client, "load_fleet_config", lambda: {"hub_url": "https://hub.example.invalid"} if scope == "hub" else {}
    )

    def release(*args, **kwargs):
        calls.append(kwargs)
        print(json.dumps({"released": True}))
        return 0

    monkeypatch.setattr(fleet_claim_release, "_release_claim", release)
    report = _patrol(repo, capsys, apply="release-claim")
    assert report["lanes"][0]["intent"]["state"] == state
    assert calls == []
    task = json.loads(path.read_text())["tasks"][0]
    assert task["status"] == "in_progress"
    assert task["claim"]["claim_id"] == "claim-1"
    assert (path.read_bytes(), intent.read_bytes()) == before
    assert not any(action["status"] == "applied" for action in report["actions"])


@pytest.mark.parametrize("scope", ["ledger", "hub"])
@pytest.mark.parametrize(
    "scan",
    [
        "unreadable",
        "truncated",
        "root-link",
        "dispatch-link",
        "intents-link",
        "root-file",
        "complete",
        "run-id-number",
        "run-id-list",
        "run-id-object",
        "run-id-empty",
        "task-id-missing",
        "task-id-null",
        "task-id-number",
        "task-id-empty",
        "planned",
    ],
)
def test_release_requires_complete_intent_scan(tmp_path, monkeypatch, capsys, scope, scan):
    from contextlib import contextmanager

    from brigade import fleet_claim_release, fleet_client, run_lane_state

    repo = _repo(tmp_path)
    run = _write_run(repo, "20260820-120000-patrol01", status="ok", extra={"claim_id": "claim-1"})
    ledger = _ledger(repo)
    root = repo / ".brigade/dispatch"
    intent = root / "dispatch-example/intents/z-intent.json"
    intent.parent.mkdir(parents=True)
    localio.write_json(
        intent,
        {"intent_id": "intent-1", "run_id": run.name, "task_id": "task-1", "claim_id": "claim-1", "state": "completed"},
    )
    if scan == "unreadable":
        intent.write_text("{broken JSON")
    elif scan == "truncated":
        # The run's own intent lies beyond the scan cap. Its launch receipt
        # still fences the lane, without poisoning claim recovery elsewhere.
        (run / "dispatch-launch.json").write_text("{}")
        localio.write_json(intent.with_name("a-unrelated.json"), {"task_id": "other", "run_id": "other"})
        monkeypatch.setattr(run_lane_state, "RUN_SCAN_LIMIT", 1)
        scandir = os.scandir

        @contextmanager
        def ordered(path):
            with scandir(path) as entries:
                yield iter(sorted(entries, key=lambda entry: entry.name))

        monkeypatch.setattr(os, "scandir", ordered)
    elif scan.endswith("-link"):
        path = {"root-link": root, "dispatch-link": intent.parent.parent, "intents-link": intent.parent}[scan]
        moved = tmp_path / "saved-intents"
        path.rename(moved)
        path.symlink_to(moved, target_is_directory=True)
    elif scan == "root-file":
        root.rename(tmp_path / "saved-intents")
        root.write_text("not a directory")
    elif scan.startswith("run-id-"):
        row = json.loads(intent.read_text())
        row["run_id"] = {"run-id-number": 42, "run-id-list": [], "run-id-object": {}, "run-id-empty": ""}[scan]
        localio.write_json(intent, row)
    elif scan.startswith("task-id-"):
        row = json.loads(intent.read_text())
        if scan == "task-id-missing":
            del row["task_id"]
        else:
            row["task_id"] = {"task-id-null": None, "task-id-number": 42, "task-id-empty": ""}[scan]
        localio.write_json(intent, row)
    elif scan == "planned":
        localio.write_json(intent, {"task_id": "other", "run_id": None, "state": "planned"})
    else:
        # A complete scan with unrelated intents establishes this run has none.
        localio.write_json(intent, {"task_id": "other", "run_id": "other"})
    before = ledger.read_bytes()
    calls = []
    monkeypatch.setattr(
        fleet_client, "load_fleet_config", lambda: {"hub_url": "https://hub.example.invalid"} if scope == "hub" else {}
    )

    def release(*args, **kwargs):
        calls.append(kwargs)
        print(json.dumps({"released": True}))
        return 0

    monkeypatch.setattr(fleet_claim_release, "_release_claim", release)
    report = _patrol(repo, capsys, apply="release-claim")
    assert report["lanes"][0]["state"] == "complete"
    if scan in {"complete", "planned"}:
        assert json.loads(ledger.read_text())["tasks"][0].get("claim") is None
        assert len(calls) == (scope == "hub")
    else:
        assert calls == []
        assert ledger.read_bytes() == before
        assert not any(action["status"] == "applied" for action in report["actions"])
        details = {item["detail"] for item in report["lanes"][0]["evidence"]}
        expected = "dispatch launch receipt without scanned intent" if scan == "truncated" else "intent scan incomplete"
        assert expected in details


@pytest.mark.parametrize("scope", ["ledger", "hub"])
@pytest.mark.parametrize(
    "scan",
    [
        "run-id-missing",
        "null-run-id-with-run-dir",
        "null-run-id-with-pid",
        "null-run-id-running",
        "null-run-id-launching",
        "null-run-id-completed",
        "unrelated-task-id-missing",
        "unrelated-task-id-null",
        "unrelated-task-id-number",
        "unrelated-task-id-empty",
        "planned",
        "claimed",
        "workspace_ready",
        "lost_race",
        "failed",
        "null-run-id-state-missing",
        "null-run-id-state-null",
        "null-run-id-state-bogus",
        "null-run-id-state-list",
        "null-run-id-state-dict",
        "null-run-id-state-number",
        "null-run-id-state-orphaned",
        "null-run-id-state-unknown",
        "run-id-missing-planned",
        "canceled",
    ],
)
def test_release_validates_all_intent_ownership_rows(tmp_path, monkeypatch, capsys, scope, scan):
    from brigade import fleet_claim_release, fleet_client

    repo = _repo(tmp_path)
    run = _write_run(repo, "20260820-120000-patrol01", status="ok", extra={"claim_id": "claim-1"})
    ledger = _ledger(repo)
    intent = repo / ".brigade/dispatch/dispatch-example/intents/intent-1.json"
    intent.parent.mkdir(parents=True)
    # Keep the pre-allocation controls in the shape written by _new_intent.
    row = {
        "intent_id": "intent-1",
        "task_id": "other",
        "task_item_revision": 1,
        "attempt_no": 1,
        "claim_id": "dispatch-intent-1",
        "seat": "example",
        "base_sha": "a" * 40,
        "branch": "brigade/dispatch/other-intent1",
        "worktree": str(tmp_path / "other-worktree"),
        "run_id": None,
        "run_dir": None,
        "pid": None,
        "state": "planned",
        "state_seq": 0,
        "created_at": "2026-08-20T12:00:00.000000Z",
        "updated_at": "2026-08-20T12:00:00.000000Z",
        "terminal": None,
        "landing": None,
    }
    preallocation = {"planned", "claimed", "workspace_ready", "lost_race", "failed"}
    preallocation.add("canceled")
    if scan in preallocation:
        row["state"] = scan
    elif scan.startswith("unrelated-task-id-"):
        row["run_id"] = "other"
        if scan == "unrelated-task-id-missing":
            del row["task_id"]
        else:
            row["task_id"] = {
                "unrelated-task-id-null": None,
                "unrelated-task-id-number": 42,
                "unrelated-task-id-empty": "",
            }[scan]
    else:
        row.update(task_id="task-1", claim_id="claim-1", state="completed")
        if scan == "run-id-missing-planned":
            del row["run_id"]
            row["state"] = "planned"
        elif scan.startswith("null-run-id-state-"):
            if scan == "null-run-id-state-missing":
                del row["state"]
            else:
                row["state"] = {
                    "null-run-id-state-null": None,
                    "null-run-id-state-bogus": "bogus",
                    "null-run-id-state-list": [],
                    "null-run-id-state-dict": {},
                    "null-run-id-state-number": 42,
                    "null-run-id-state-orphaned": "orphaned",
                    "null-run-id-state-unknown": "unknown",
                }[scan]
        elif scan == "run-id-missing":
            del row["run_id"]
        elif scan == "null-run-id-with-run-dir":
            row.update(state="workspace_ready", run_dir=str(run))
        elif scan == "null-run-id-with-pid":
            row.update(state="claimed", pid=12345)
        else:
            row["state"] = scan.removeprefix("null-run-id-")
    localio.write_json(intent, row)
    before = ledger.read_bytes(), intent.read_bytes()
    calls = []
    monkeypatch.setattr(
        fleet_client, "load_fleet_config", lambda: {"hub_url": "https://hub.example.invalid"} if scope == "hub" else {}
    )

    def release(*args, **kwargs):
        calls.append(kwargs)
        print(json.dumps({"released": True}))
        return 0

    monkeypatch.setattr(fleet_claim_release, "_release_claim", release)
    report = _patrol(repo, capsys, apply="release-claim")
    assert report["lanes"][0]["state"] == "complete"
    assert intent.read_bytes() == before[1]
    if scan in preallocation:
        assert json.loads(ledger.read_text())["tasks"][0].get("claim") is None
        assert len(calls) == (scope == "hub")
        assert any(action["status"] == "applied" for action in report["actions"])
    else:
        assert ledger.read_bytes() == before[0]
        assert calls == []
        assert not any(action["status"] == "applied" for action in report["actions"])
        assert any(item["detail"] == "intent scan incomplete" for item in report["lanes"][0]["evidence"])


def test_release_refuses_newer_claim(tmp_path, monkeypatch, capsys):
    from brigade.work_cmd import ledger

    repo = _repo(tmp_path)
    _write_run(repo, "20260820-120000-patrol01", status="ok", extra={"claim_id": "claim-1"})
    _ledger(repo)
    read = ledger._read_task_ledger

    def reclaimed(target):
        value = read(target)
        value["tasks"][0]["claim"]["claim_id"] = "claim-2"
        return value

    monkeypatch.setattr(ledger, "_read_task_ledger", reclaimed)
    report = _patrol(repo, capsys, apply="release-claim")
    assert not any(item["status"] == "applied" for item in report["actions"])
    assert "claim" in json.loads((repo / ".brigade/work/tasks.json").read_text())["tasks"][0]


def test_escalation_cooldown_cap_and_fleet_message_idempotence(tmp_path, monkeypatch, capsys):
    from brigade import fleet_client_messages

    repo = _repo(tmp_path)
    run = _write_run(repo, "20260820-120000-patrol01")
    _write_stale_lock(repo, run)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: False)
    sent = []
    monkeypatch.setattr(fleet_client_messages, "send", lambda **kwargs: sent.append(kwargs) or {"sent": True})
    kwargs = {"escalate_via": "fleet-msg", "escalate_to": "operator"}
    first = _patrol(repo, capsys, apply="escalate", **kwargs)
    assert first["actions"][0]["status"] == "applied"
    for hours in (0, 3, 4, 8):
        _patrol(repo, capsys, apply="escalate", now=NOW + timedelta(hours=hours), **kwargs)
    assert len(sent) == 2
    assert sent[0]["client_id"] != sent[1]["client_id"]
    assert all(item["recipient"] == "operator" for item in sent)


def test_apply_validates_options_before_writing(tmp_path, capsys):
    assert cli.main(["runs", "patrol", "--target", str(tmp_path), "--apply", "interrupt"]) == 2
    assert "unknown action" in capsys.readouterr().err
    assert not (tmp_path / ".brigade").exists()


def test_apply_refuses_unwritable_receipt_before_mutation(tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path)
    run = _write_run(repo, "20260820-120000-patrol01")
    _write_stale_lock(repo, run)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: False)
    (repo / ".brigade/patrol").symlink_to(tmp_path, target_is_directory=True)
    before = (run / "run.json").read_bytes()
    assert runs_patrol.patrol(target=repo, now=NOW, apply="reap") == 2
    assert "symlink" in capsys.readouterr().err
    assert (run / "run.json").read_bytes() == before


@pytest.mark.parametrize("actions", ["reap,release-claim", "release-claim,reap"])
def test_reap_and_release_use_updated_run_evidence(tmp_path, monkeypatch, capsys, actions):
    repo = _repo(tmp_path)
    run = _write_run(repo, "20260820-120000-patrol01", extra={"claim_id": "claim-1"})
    _write_stale_lock(repo, run)
    _ledger(repo)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: False)
    report = _patrol(repo, capsys, apply=actions)
    task = json.loads((repo / ".brigade/work/tasks.json").read_text())["tasks"][0]
    assert task.get("claim") is None, report["actions"]
    assert json.loads((run / "run.json").read_text())["status"] == "orphaned"


def test_release_rechecks_run_after_acquiring_lock(tmp_path, monkeypatch, capsys):
    from tests.test_run_reap import _racing_lock_after_claim

    repo = _repo(tmp_path)
    run = _write_run(repo, "20260820-120000-patrol01", status="ok", extra={"claim_id": "claim-1"})
    _write_stale_lock(repo, run)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: False)
    ledger = _ledger(repo)
    monkeypatch.setattr(
        runguard, "run_lock", _racing_lock_after_claim(run, lambda meta: meta.update(status="dispatching"))
    )
    report = _patrol(repo, capsys, apply="release-claim")
    assert json.loads(ledger.read_text())["tasks"][0].get("claim"), report["actions"]
    assert not any(item["status"] == "applied" for item in report["actions"])


def test_unchanged_evidence_does_not_retry_uncertain_message(tmp_path, monkeypatch, capsys):
    from brigade import fleet_client_messages

    repo = _repo(tmp_path)
    run = _write_run(repo, "20260820-120000-patrol01")
    _write_stale_lock(repo, run)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: False)
    sent = []

    def ambiguous(**kwargs):
        sent.append(kwargs)
        raise RuntimeError("delivery response was lost")

    monkeypatch.setattr(fleet_client_messages, "send", ambiguous)
    kwargs = {"escalate_via": "fleet-msg", "escalate_to": "operator"}
    first = _patrol(repo, capsys, apply="escalate", **kwargs)
    assert first["actions"][0]["status"] == "failed"
    assert first["actions"][0]["severity"] == "urgent"
    assert first["actions"][0]["escalation_number"] == 1
    second = _patrol(repo, capsys, apply="escalate", **kwargs)
    assert second["actions"] == []
    assert len(sent) == 1


@pytest.mark.parametrize("days", [0, 8])
def test_pending_receipt_fences_a_crashed_patrol(tmp_path, monkeypatch, capsys, days):
    repo = _repo(tmp_path)
    run = _write_run(repo, "20260820-120000-patrol01")
    _write_stale_lock(repo, run)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: False)
    report = _patrol(repo, capsys, apply="escalate")
    report["actions"][0]["status"] = "pending"
    localio.write_json(repo / report["receipt"], report)
    again = _patrol(repo, capsys, apply="escalate", now=NOW + timedelta(days=days))
    if days:
        # The archived uncertain attempt still consumes the first delivery.
        assert again["actions"][0]["escalation_number"] == 2
        assert again["actions"][0]["idempotency_key"] != report["actions"][0]["idempotency_key"]
    else:
        assert again["actions"] == []


def test_escalation_budget_survives_receipt_retention(tmp_path, monkeypatch, capsys):
    from brigade import fleet_client_messages, runs_patrol_receipts

    repo = _repo(tmp_path)
    run = _write_run(repo, "20260820-120000-patrol01")
    _write_stale_lock(repo, run)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: False)
    monkeypatch.setattr(runs_patrol_receipts, "REPORT_RECEIPT_LIMIT", 1)
    sent = []
    monkeypatch.setattr(fleet_client_messages, "send", lambda **kwargs: sent.append(kwargs) or {"sent": True})
    kwargs = {"escalate_via": "fleet-msg", "escalate_to": "operator"}
    _patrol(repo, capsys, apply="escalate", **kwargs)
    _patrol(repo, capsys, apply="escalate", now=NOW + timedelta(hours=4), **kwargs)
    again = _patrol(repo, capsys, apply="escalate", now=NOW + timedelta(days=8), **kwargs)
    assert again["actions"] == [] and len(sent) == 2
    assert len(list((repo / ".brigade/patrol").glob("*/patrol.json"))) == 1


def test_release_refuses_a_live_terminal_owner(tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path)
    run = _write_run(repo, "20260820-120000-patrol01", status="ok", extra={"claim_id": "claim-1"})
    _write_stale_lock(repo, run)
    path = _ledger(repo)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: True)
    before = path.read_bytes()
    report = _patrol(repo, capsys, apply="release-claim")
    assert path.read_bytes() == before
    assert not any(item["status"] == "applied" for item in report["actions"])


def test_claim_release_refuses_a_resumed_journal_with_stale_snapshot(tmp_path, monkeypatch, capsys):
    from tests.test_run_reap import _racing_lock_after_claim

    repo = _repo(tmp_path)
    run = _write_run(repo, "20260820-120000-patrol01", status="ok", extra={"claim_id": "claim-1"})
    _write_stale_lock(repo, run)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: False)
    path = _ledger(repo)
    run_journal.append_event(
        run / "events/lifecycle.jsonl",
        run_id=run.name,
        event_type="run.completed",
        payload={"status": "ok"},
        idempotency_key="complete",
        expected_previous_sequence=0,
        recorded_at="2026-08-20T12:00:00.000000Z",
    )

    def resume(meta):
        run_journal.append_event(
            run / "events/lifecycle.jsonl",
            run_id=run.name,
            event_type="run.resumed",
            payload={},
            idempotency_key="resume",
            expected_previous_sequence=1,
            recorded_at="2026-08-28T17:59:00.000000Z",
        )

    monkeypatch.setattr(runguard, "run_lock", _racing_lock_after_claim(run, resume))
    report = _patrol(repo, capsys, apply="release-claim")
    assert json.loads(path.read_text())["tasks"][0].get("claim"), report["actions"]


@pytest.mark.parametrize("old_name", ["old", "000-old"])
def test_unreadable_history_refuses_actions_and_records_exit_disposition(tmp_path, monkeypatch, capsys, old_name):
    from brigade import runs_patrol_receipts

    repo = _repo(tmp_path)
    run = _write_run(repo, "20260820-120000-patrol01")
    _write_stale_lock(repo, run)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: False)
    monkeypatch.setattr(runs_patrol_receipts, "REPORT_RECEIPT_LIMIT", 1)
    old = repo / f".brigade/patrol/{old_name}/patrol.json"
    old.parent.mkdir(parents=True)
    old.write_text("not json")
    before = (run / "run.json").read_bytes()
    assert runs_patrol.patrol(target=repo, now=NOW, apply="reap") == 2
    assert "error: patrol:" in capsys.readouterr().err
    assert (run / "run.json").read_bytes() == before
    receipt = next(path for path in old.parent.parent.glob("*/patrol.json") if path != old)
    assert json.loads(receipt.read_text())["exit_code"] == 2


def test_reap_refresh_cannot_adopt_a_concurrent_nonterminal_snapshot(tmp_path, monkeypatch, capsys):
    from brigade import runs_patrol_actions

    repo = _repo(tmp_path)
    run = _write_run(repo, "20260820-120000-patrol01", extra={"claim_id": "claim-1"})
    _write_stale_lock(repo, run)
    ledger = _ledger(repo)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: False)
    reap = runs_patrol_actions._reap

    def raced(*args, **kwargs):
        result = reap(*args, **kwargs)
        meta = json.loads((run / "run.json").read_text())
        meta.update(status="dispatching")
        localio.write_json(run / "run.json", meta)
        return result

    monkeypatch.setattr(runs_patrol_actions, "_reap", raced)
    report = _patrol(repo, capsys, apply="reap,release-claim")
    assert json.loads(ledger.read_text())["tasks"][0].get("claim"), report["actions"]


@pytest.mark.parametrize("initial", ["unavailable", "unconfigured"])
def test_hub_release_retries_after_refusal_or_missing_configuration(tmp_path, monkeypatch, capsys, initial):
    from brigade import fleet_claim_release, fleet_client

    repo = _repo(tmp_path)
    _write_run(repo, "20260820-120000-patrol01", status="ok", extra={"claim_id": "claim-1"})
    available = False
    calls = []
    monkeypatch.setattr(
        fleet_client,
        "load_fleet_config",
        lambda: {"hub_url": "https://hub.example.invalid"} if available or initial == "unavailable" else {},
    )

    def release(*args, **kwargs):
        calls.append(kwargs)
        if not available:
            return 2
        print(json.dumps({"released": True}))
        return 0

    monkeypatch.setattr(fleet_claim_release, "_release_claim", release)
    first = _patrol(repo, capsys, apply="release-claim")
    if initial == "unavailable":
        hub = next(item for item in first["actions"] if item["scope"] == "hub")
        assert hub["status"] == "refused"
    else:
        assert first["actions"] == []
    available = True
    second = _patrol(repo, capsys, apply="release-claim")
    assert any(item["scope"] == "hub" and item["status"] == "applied" for item in second["actions"])
    _patrol(repo, capsys, apply="release-claim")
    assert len(calls) == (2 if initial == "unavailable" else 1)
    assert calls[-1]["force"] is False and calls[-1]["expected_run_dir"] is not None


def test_undelivered_escalation_does_not_consume_delivery_budget(tmp_path, monkeypatch, capsys):
    from brigade import fleet_client_messages

    repo = _repo(tmp_path)
    run = _write_run(repo, "20260820-120000-patrol01")
    _write_stale_lock(repo, run)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: False)
    first = _patrol(repo, capsys, apply="escalate")
    assert first["actions"][0]["status"] == "recorded"
    assert _patrol(repo, capsys, apply="escalate")["actions"] == []
    sent = []
    monkeypatch.setattr(fleet_client_messages, "send", lambda **kwargs: sent.append(kwargs) or {"sent": True})
    delivered = _patrol(repo, capsys, apply="escalate", escalate_via="fleet-msg", escalate_to="operator")
    assert delivered["actions"][0]["status"] == "applied"
    assert delivered["actions"][0]["escalation_number"] == 1
    assert len(sent) == 1


def test_incomplete_receipt_directory_does_not_block_apply(tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path)
    run = _write_run(repo, "20260820-120000-patrol01")
    _write_stale_lock(repo, run)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: False)
    (repo / ".brigade/patrol/20260820T120000-incomplete").mkdir(parents=True)
    report = _patrol(repo, capsys, apply="reap")
    assert report["actions"][0]["status"] == "applied"


def test_lock_acquisition_refusal_can_retry_ledger_release(tmp_path, monkeypatch, capsys):
    from contextlib import contextmanager

    repo = _repo(tmp_path)
    run = _write_run(repo, "20260820-120000-patrol01", status="ok", extra={"claim_id": "claim-1"})
    _write_stale_lock(repo, run)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: False)
    path = _ledger(repo)
    real_lock = runguard.run_lock

    @contextmanager
    def unavailable(*args, **kwargs):
        raise runguard.RunLockError("another writer acquired the lock")
        yield

    monkeypatch.setattr(runguard, "run_lock", unavailable)
    first = _patrol(repo, capsys, apply="release-claim")
    assert first["actions"][0]["status"] == "refused"
    assert json.loads(path.read_text())["tasks"][0].get("claim")
    monkeypatch.setattr(runguard, "run_lock", real_lock)
    second = _patrol(repo, capsys, apply="release-claim")
    assert any(item["scope"] == "ledger" and item["status"] == "applied" for item in second["actions"])
    assert json.loads(path.read_text())["tasks"][0].get("claim") is None


@pytest.mark.parametrize("actions", ["release-claim", "reap,release-claim"])
def test_release_preserves_dead_run_proof_when_reap_does_not_succeed(tmp_path, monkeypatch, capsys, actions):
    from brigade import run_reap

    repo = _repo(tmp_path)
    run = _write_run(repo, "20260820-120000-patrol01", extra={"claim_id": "claim-1"})
    lock = _write_stale_lock(repo, run)
    ledger = _ledger(repo)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: False)
    monkeypatch.setattr(run_reap, "_preflight", lambda *args, **kwargs: "ambiguous")
    before = (run / "run.json").read_bytes(), (lock / "owner.json").read_bytes(), ledger.read_bytes()
    report = _patrol(repo, capsys, apply=actions)
    assert ((run / "run.json").read_bytes(), (lock / "owner.json").read_bytes(), ledger.read_bytes()) == before
    assert not any(item["action"] == "release-claim" for item in report["actions"])


def test_ineligible_actions_do_not_write_attempts_or_query_hub(tmp_path, monkeypatch, capsys):
    from brigade import fleet_claim_release

    repo = _repo(tmp_path)
    _write_run(repo, "20260820-120000-patrol01", status="ok")

    def unexpected(*args, **kwargs):
        pytest.fail("a lane without an observed claim must not query the Hub")

    monkeypatch.setattr(fleet_claim_release, "_release_claim", unexpected)
    report = _patrol(repo, capsys, apply="reap,release-claim")
    assert report["actions"] == []


def test_terminal_release_fences_an_absent_run_lock_through_the_ledger_write(tmp_path, monkeypatch, capsys):
    from brigade.work_cmd import ledger

    repo = _repo(tmp_path)
    run = _write_run(repo, "20260820-120000-patrol01", status="ok", extra={"claim_id": "claim-1"})
    ledger_path = _ledger(repo)
    assert runguard.run_lock_state(repo, run) == "absent"
    observed = []
    write = ledger._write_task_ledger

    def checked(target, data):
        observed.append(runguard.run_lock_state(repo, run))
        with pytest.raises(runguard.RunLockError):
            with runguard.run_lock(repo, run_dir=run, wait_seconds=0):
                pass
        write(target, data)

    monkeypatch.setattr(ledger, "_write_task_ledger", checked)
    _patrol(repo, capsys, apply="release-claim")
    assert observed and observed[0] != "absent"
    assert json.loads(ledger_path.read_text())["tasks"][0].get("claim") is None
    assert runguard.run_lock_state(repo, run) == "absent"
    assert not (repo / ".brigade/run.lock").exists()


def test_truncated_intent_scan_still_releases_a_run_without_a_dispatch_receipt(tmp_path, monkeypatch, capsys):
    from brigade import run_lane_state

    repo = _repo(tmp_path)
    _write_run(repo, "20260820-120000-patrol01", status="ok", extra={"claim_id": "claim-1"})
    ledger_path = _ledger(repo)
    intents = repo / ".brigade/dispatch/dispatch-example/intents"
    for index in range(3):
        localio.write_json(intents / f"intent-{index}.json", {"task_id": f"other-{index}", "run_id": f"other-{index}"})
    monkeypatch.setattr(run_lane_state, "RUN_SCAN_LIMIT", 2)
    report = _patrol(repo, capsys, apply="release-claim")
    assert report["truncated"] is True
    assert json.loads(ledger_path.read_text())["tasks"][0].get("claim") is None
    assert any(item["status"] == "applied" for item in report["actions"])


def test_record_only_escalation_deduplicates_after_one_delivery(tmp_path, monkeypatch, capsys):
    from brigade import fleet_client_messages

    repo = _repo(tmp_path)
    run = _write_run(repo, "20260820-120000-patrol01")
    _write_stale_lock(repo, run)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: False)
    monkeypatch.setattr(fleet_client_messages, "send", lambda **kwargs: {"sent": True})
    _patrol(repo, capsys, apply="escalate", escalate_via="fleet-msg", escalate_to="operator")
    later = NOW + timedelta(hours=4)
    recorded = _patrol(repo, capsys, apply="escalate", now=later)
    assert recorded["actions"][0]["status"] == "recorded"
    assert recorded["actions"][0]["escalation_number"] == 2
    assert _patrol(repo, capsys, apply="escalate", now=later + timedelta(minutes=10))["actions"] == []


@pytest.mark.parametrize("case", ["orphaned", "normal-claim", "young-dead", "reaped"])
def test_harmless_or_resolved_lanes_never_escalate(tmp_path, monkeypatch, capsys, case):
    from brigade import fleet_client_messages

    repo = _repo(tmp_path)
    run = _write_run(
        repo,
        "20260820-120000-patrol01",
        status="orphaned" if case == "orphaned" else "dispatching",
        started_at=(NOW - timedelta(minutes=30)).isoformat() if case == "young-dead" else "2026-08-20T12:00:00+00:00",
    )
    if case != "orphaned":
        _write_stale_lock(repo, run)
    if case == "normal-claim":
        _ledger(repo)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: case == "normal-claim")
    sent = []
    monkeypatch.setattr(fleet_client_messages, "send", lambda **kw: sent.append(kw) or {})
    report = _patrol(
        repo,
        capsys,
        apply="reap,escalate" if case == "reaped" else "escalate",
        escalate_via="fleet-msg",
        escalate_to="operator",
        stall_after="30d" if case == "normal-claim" else "30m",
    )
    assert not any(action["action"] == "escalate" for action in report["actions"])
    assert sent == []
    if case == "normal-claim":
        assert any(row["claim_id"] == "claim-1" and row["state"] == "unknown" for row in report["lanes"])


def test_corrupt_evidence_unknown_still_escalates(tmp_path, capsys):
    repo = _repo(tmp_path)
    run = _write_run(repo, "20260820-120000-patrol01")
    (run / "events").mkdir()
    (run / "events/lifecycle.jsonl").write_text("not json\n")
    report = _patrol(repo, capsys, apply="escalate")
    assert report["lanes"][0]["state"] == "unknown"
    assert report["actions"][0]["status"] == "recorded"


def test_hub_release_without_synthetic_claim_id(tmp_path, monkeypatch, capsys):
    from brigade import fleet_claim_release, fleet_client

    repo = _repo(tmp_path)
    run = _write_run(repo, "20260820-120000-patrol01", status="ok")
    monkeypatch.setattr(fleet_client, "load_fleet_config", lambda: {"hub_url": "https://hub.example.invalid"})
    calls = []

    def release(*args, **kwargs):
        calls.append(kwargs)
        print(json.dumps({"released": True}))
        return 0

    monkeypatch.setattr(fleet_claim_release, "_release_claim", release)
    report = _patrol(repo, capsys, apply="release-claim")
    assert report["actions"][0]["scope"] == "hub"
    assert report["actions"][0]["status"] == "applied"
    assert calls[0]["expected_run_dir"] == run
    assert _patrol(repo, capsys, apply="release-claim")["actions"] == []
    assert len(calls) == 1


@pytest.mark.parametrize("refresh_failure", ["snapshot", "journal"])
def test_reap_success_survives_refresh_failure(tmp_path, monkeypatch, capsys, refresh_failure):
    from brigade import runs_patrol_actions

    repo = _repo(tmp_path)
    run = _write_run(repo, "20260820-120000-patrol01", extra={"claim_id": "claim-1"})
    _write_stale_lock(repo, run)
    path = _ledger(repo)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: False)
    if refresh_failure == "snapshot":
        reap = runs_patrol_actions._reap

        def unavailable_after_reap(*args, **kwargs):
            result = reap(*args, **kwargs)
            monkeypatch.setattr(runs_patrol_actions.run_reap, "_read_run_bytes", lambda path: (None, None))
            return result

        monkeypatch.setattr(runs_patrol_actions, "_reap", unavailable_after_reap)
    else:

        def corrupt(*args):
            raise ValueError("journal became corrupt")

        monkeypatch.setattr(runs_patrol_actions, "_journal_digest", corrupt)
    report = _patrol(repo, capsys, apply="reap,release-claim,escalate")
    assert json.loads((run / "run.json").read_text())["status"] == "orphaned"
    action = report["actions"][0]
    assert action["status"] == "applied"
    assert action["after"]["state"] == "orphaned"
    assert action["refresh_error"] == "ValueError"
    assert len(report["actions"]) == 1
    assert json.loads(path.read_text())["tasks"][0].get("claim")


def test_reap_refresh_failure_does_not_skip_later_lane_escalation(tmp_path, monkeypatch, capsys):
    from brigade import runs_patrol_actions

    repo = _repo(tmp_path)
    later = _write_run(repo, "20260820-120000-patrol01")
    _write_stale_lock(repo, later)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: False)
    reap = runs_patrol_actions._reap
    monkeypatch.setattr(runs_patrol_actions, "_reap", lambda *args: {"status": "failed", "reason": "reap unavailable"})
    prior = _patrol(repo, capsys, apply="reap")
    assert prior["actions"][0]["status"] == "failed"
    monkeypatch.setattr(runs_patrol_actions, "_reap", reap)

    workspace = tmp_path / "other-workspace"
    workspace.mkdir()
    first = _write_run(repo, "20260820-120000-patrol02", extra={"lock_workspace": str(workspace)})
    _write_stale_lock(workspace, first)
    os.utime(later, (NOW.timestamp(), NOW.timestamp()))
    os.utime(first, (NOW.timestamp() + 1, NOW.timestamp() + 1))

    def unavailable_journal(*args):
        raise ValueError("journal unavailable after reap")

    monkeypatch.setattr(runs_patrol_actions, "_journal_digest", unavailable_journal)
    report = _patrol(repo, capsys, apply="reap,release-claim,escalate")
    assert [lane["run_id"] for lane in report["lanes"]] == [first.name, later.name]
    assert report["actions"][0]["status"] == "applied"
    assert report["actions"][0]["refresh_error"] == "ValueError"
    assert any(
        action["lane_id"] == report["lanes"][1]["lane_id"]
        and action["action"] == "escalate"
        and action["status"] == "recorded"
        for action in report["actions"]
    )


def test_unconfigured_hub_does_not_reserve_repeated_actions(tmp_path, monkeypatch, capsys):
    from brigade import fleet_client

    repo = _repo(tmp_path)
    _write_run(repo, "20260820-120000-patrol01", status="ok", extra={"claim_id": "claim-1"})
    _ledger(repo)
    monkeypatch.setattr(fleet_client, "load_fleet_config", lambda: {})
    first = _patrol(repo, capsys, apply="release-claim")
    assert [action["scope"] for action in first["actions"]] == ["ledger"]
    assert _patrol(repo, capsys, apply="release-claim")["actions"] == []


@pytest.mark.parametrize("scope", ["ledger", "hub"])
@pytest.mark.parametrize("receipt_kind", ["file", "symlink", "directory", "permission", "not-directory", "os-error"])
def test_release_refuses_dispatch_receipt_without_scanned_intent(tmp_path, monkeypatch, capsys, scope, receipt_kind):
    from pathlib import Path

    from brigade import fleet_claim_release, fleet_client

    repo = _repo(tmp_path)
    run = _write_run(repo, "20260820-120000-patrol01", status="ok", extra={"claim_id": "claim-1"})
    ledger = _ledger(repo)
    receipt = run / "dispatch-launch.json"
    if receipt_kind == "file":
        receipt.write_text("{}")
    elif receipt_kind == "symlink":
        receipt.symlink_to(run / "missing-receipt")
    elif receipt_kind == "directory":
        receipt.mkdir()
    else:
        lstat = Path.lstat
        error = {"permission": PermissionError, "not-directory": NotADirectoryError, "os-error": OSError}[receipt_kind]

        def unavailable(path, *args, **kwargs):
            if path == receipt:
                raise error("receipt unavailable")
            return lstat(path, *args, **kwargs)

        monkeypatch.setattr(Path, "lstat", unavailable)
    before = ledger.read_bytes()
    calls = []
    monkeypatch.setattr(
        fleet_client, "load_fleet_config", lambda: {"hub_url": "https://hub.example.invalid"} if scope == "hub" else {}
    )

    def release(*args, **kwargs):
        calls.append(kwargs)
        print(json.dumps({"released": True}))
        return 0

    monkeypatch.setattr(fleet_claim_release, "_release_claim", release)
    report = _patrol(repo, capsys, apply="release-claim")
    action = next(item for item in report["actions"] if item["scope"] == "ledger")
    assert action["status"] == "refused"
    assert action["reason"] == "intent scan incomplete, dispatch ownership unverified"
    lane = report["lanes"][0]
    assert lane["intent"] is None
    assert any(
        item["source"] == "dispatch-launch.json" and item["detail"] == "dispatch launch receipt without scanned intent"
        for item in lane["evidence"]
    )
    assert ledger.read_bytes() == before
    assert calls == []
    assert not any(item["status"] == "applied" for item in report["actions"])


@pytest.mark.parametrize("scope", ["ledger", "hub"])
def test_release_without_dispatch_receipt_is_not_fenced_by_other_lane(tmp_path, monkeypatch, capsys, scope):
    from brigade import fleet_claim_release, fleet_client

    repo = _repo(tmp_path)
    ordinary = _write_run(repo, "20260820-120000-patrol01", status="ok", extra={"claim_id": "claim-1"})
    protected = _write_run(repo, "20260820-120000-patrol02", status="ok")
    (protected / "dispatch-launch.json").write_text("{}")
    ledger = _ledger(repo)
    calls = []
    monkeypatch.setattr(
        fleet_client, "load_fleet_config", lambda: {"hub_url": "https://hub.example.invalid"} if scope == "hub" else {}
    )

    def release(*args, **kwargs):
        calls.append(kwargs["expected_run_dir"])
        print(json.dumps({"released": True}))
        return 0

    monkeypatch.setattr(fleet_claim_release, "_release_claim", release)
    report = _patrol(repo, capsys, apply="release-claim")
    action = next(item for item in report["actions"] if item["scope"] == "ledger")
    assert action["status"] == "applied"
    assert json.loads(ledger.read_text())["tasks"][0].get("claim") is None
    assert calls == ([ordinary] if scope == "hub" else [])


@pytest.mark.parametrize("scope", ["ledger", "hub"])
def test_dispatch_receipt_preserves_joined_intent_refusal_reason(tmp_path, monkeypatch, capsys, scope):
    from brigade import fleet_claim_release, fleet_client

    repo = _repo(tmp_path)
    run = _write_run(repo, "20260820-120000-patrol01", status="ok", extra={"claim_id": "claim-1"})
    (run / "dispatch-launch.json").write_text("{}")
    ledger = _ledger(repo)
    intent = repo / ".brigade/dispatch/dispatch-example/intents/intent-1.json"
    intent.parent.mkdir(parents=True)
    localio.write_json(
        intent,
        {"intent_id": "intent-1", "run_id": run.name, "task_id": "task-1", "claim_id": "claim-1", "state": "running"},
    )
    before = ledger.read_bytes(), intent.read_bytes()
    calls = []
    monkeypatch.setattr(
        fleet_client, "load_fleet_config", lambda: {"hub_url": "https://hub.example.invalid"} if scope == "hub" else {}
    )

    def release(*args, **kwargs):
        calls.append(kwargs)
        print(json.dumps({"released": True}))
        return 0

    monkeypatch.setattr(fleet_claim_release, "_release_claim", release)
    report = _patrol(repo, capsys, apply="release-claim")
    action = next(item for item in report["actions"] if item["scope"] == "ledger")
    assert action["status"] == "refused"
    assert action["reason"] == "dispatch owns intent claim finalization"
    assert report["lanes"][0]["intent"]["state"] == "running"
    assert not any(item["source"] == "dispatch-launch.json" for item in report["lanes"][0]["evidence"])
    assert (ledger.read_bytes(), intent.read_bytes()) == before
    assert calls == []


def test_escalation_delivery_is_summarized_so_the_receipt_stays_readable(tmp_path, monkeypatch, capsys):
    from brigade import fleet_client_messages, runs_patrol_receipts

    repo = _repo(tmp_path)
    run = _write_run(repo, "20260820-120000-patrol01")
    _write_stale_lock(repo, run)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: False)
    huge = {"sent": True, "message": {"blob": "x" * 5_000_000}, "extra": "y" * 5000}
    monkeypatch.setattr(fleet_client_messages, "send", lambda **kwargs: huge)
    report = _patrol(repo, capsys, apply="escalate", escalate_via="fleet-msg", escalate_to="operator")
    delivery = report["actions"][0]["delivery"]
    assert delivery["sent"] is True and "message" not in delivery and len(delivery["extra"]) == 200
    receipt = repo / report["receipt"]
    assert runs_patrol_receipts.read_receipt(receipt.parent)["actions"]
