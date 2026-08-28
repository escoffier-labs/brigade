"""Backup Steward action gating, expiry, approvals, replay, and concurrency."""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from brigade.grokbot_backup import actions as actions_mod
from brigade.grokbot_backup.actions import (
    PROPOSAL_TTL_MS,
    BackupActionExecutor,
    BackupActionStore,
    catalog_entry,
)
from brigade.grokbot_backup.contracts import BackupError
from brigade.grokbot_backup.exec import ExecRequest, PrivateExecResult
from brigade.grokbot_backup.ledger import BackupLedger, backup_finding_revision
from brigade.grokbot_backup.runtime_config import parse_backup_private_runtime, project_backup_registry
from brigade.grokbot_backup.tools import BackupStewardTools
from tests.test_grokbot_backup_runtime import _runtime


NOW = datetime(2026, 8, 28, tzinfo=timezone.utc)


def _dirs(tmp_path: Path) -> tuple[BackupLedger, BackupActionStore, dict[str, datetime]]:
    ledger_dir = tmp_path / "ledger"
    actions = tmp_path / "actions"
    approvals = tmp_path / "approvals"
    ledger_dir.mkdir(parents=True, mode=0o700)
    actions.mkdir(mode=0o700)
    approvals.mkdir(mode=0o700)
    os.chmod(ledger_dir, 0o700)
    os.chmod(actions, 0o700)
    os.chmod(approvals, 0o700)
    clock = {"value": NOW}

    def now() -> datetime:
        return clock["value"]

    ledger = BackupLedger(str(ledger_dir / "ledger.jsonl"))
    ledger.ready()
    store = BackupActionStore(
        action_state_path=str(actions),
        approval_dir=str(approvals),
        now=now,
        create_proposal_id=lambda: "proposal1proposal1proposal1propos",
        create_nonce=lambda: "nonce1nonce1nonce1nonce1nonce1no",
    )
    store.ready()
    return ledger, store, clock


def _seed_finding(ledger: BackupLedger) -> dict[str, object]:
    observation = {
        "target_alias": "media-archive",
        "health": "degraded",
        "lock_class": "stale_lock",
        "observed_at": "2026-08-28T00:00:00Z",
        "freshness_seconds": 60,
        "detail": "ok",
    }
    ledger.record_observation(observation, "receipt-1")
    finding = {
        "finding_id": "media-archive:stale-lock",
        "target_alias": "media-archive",
        "kind": "stale-lock",
        "severity_class": "warning",
        "summary": "A stale backup lock is present",
        "observed_at": "2026-08-28T00:00:00Z",
        "receipt_ref": "receipt-1",
        "proposed_action_id": "run-backup",
        "blast_radius": "one registered restic target",
        "verification_statement": "compare the next snapshot receipt",
        "recovery_statement": "operator reruns the approved backup",
    }
    ledger.replace_findings("media-archive", [finding])
    return finding


def _tools(tmp_path: Path, *, safety_ready: bool = True, runner=None):
    ledger, store, clock = _dirs(tmp_path)
    runtime = parse_backup_private_runtime(_runtime(tmp_path, safety_ready=safety_ready))
    registry = project_backup_registry(runtime)
    fake_runner = runner or (lambda request: PrivateExecResult(stdout=""))
    executor = BackupActionExecutor(
        runtime=runtime,
        store=store,
        env={},
        now=lambda: clock["value"],
        create_operation_id=lambda: "operation1operation1operation1oper",
        create_receipt_ref=lambda: "op-receipt-1",
        secrets=["secret-token"],
        runner=fake_runner,
        schedule=lambda work: work(),
    )
    tools = BackupStewardTools(
        registry=registry,
        runtime=runtime,
        observers=None,  # type: ignore[arg-type]
        ledger=ledger,
        store=store,
        executor=executor,
        now=lambda: clock["value"],
        request_id=lambda: "request-1",
        secrets=["secret-token"],
    )
    return tools, ledger, store, clock


def test_propose_is_denied_for_observation_only_and_unready_runtime(tmp_path: Path):
    tools, ledger, _store, _clock = _tools(tmp_path, safety_ready=True)
    _seed_finding(ledger)
    with pytest.raises(BackupError) as caught:
        tools.backup_propose_action(
            {"target_alias": "virtualization", "action_id": "run-backup", "finding_id": "media-archive:stale-lock"}
        )
    assert caught.value.code == "denied"
    tools, ledger, _store, _clock = _tools(tmp_path / "unready", safety_ready=False)
    _seed_finding(ledger)
    with pytest.raises(BackupError):
        tools.backup_propose_action(
            {"target_alias": "media-archive", "action_id": "run-backup", "finding_id": "media-archive:stale-lock"}
        )


def test_propose_expires_and_binds_finding_revision(tmp_path: Path):
    tools, ledger, store, clock = _tools(tmp_path)
    finding = _seed_finding(ledger)
    result = tools.backup_propose_action(
        {"target_alias": "media-archive", "action_id": "run-backup", "finding_id": "media-archive:stale-lock"}
    )
    proposal = store.read_proposal(result["data"]["proposal_id"])
    assert proposal["finding_revision"] == backup_finding_revision(finding)
    assert result["data"]["automatic_rollback"] is False
    assert catalog_entry("media-archive", "run-backup") is not None
    clock["value"] = NOW + timedelta(milliseconds=PROPOSAL_TTL_MS + 1)
    store.write_approval_file(
        {
            "version": 1,
            "proposal_id": proposal["proposal_id"],
            "target_alias": proposal["target_alias"],
            "action_id": proposal["action_id"],
            "finding_id": proposal["finding_id"],
            "finding_revision": proposal["finding_revision"],
            "nonce": proposal["nonce"],
            "expires_at": proposal["expires_at"],
            "approved_at": "2026-08-28T00:01:00Z",
        }
    )
    with pytest.raises(BackupError) as caught:
        tools.backup_execute_action({"proposal_id": proposal["proposal_id"]})
    assert caught.value.code == "denied"


def test_execute_requires_operator_approval_and_is_single_use(tmp_path: Path):
    tools, ledger, store, _clock = _tools(tmp_path)
    _seed_finding(ledger)
    proposed = tools.backup_propose_action(
        {"target_alias": "media-archive", "action_id": "run-backup", "finding_id": "media-archive:stale-lock"}
    )
    proposal_id = proposed["data"]["proposal_id"]
    with pytest.raises(BackupError):
        tools.backup_execute_action({"proposal_id": proposal_id})
    proposal = store.read_proposal(proposal_id)
    store.write_approval_file(
        {
            "version": 1,
            "proposal_id": proposal["proposal_id"],
            "target_alias": proposal["target_alias"],
            "action_id": proposal["action_id"],
            "finding_id": proposal["finding_id"],
            "finding_revision": proposal["finding_revision"],
            "nonce": proposal["nonce"],
            "expires_at": proposal["expires_at"],
            "approved_at": "2026-08-28T00:01:00Z",
        }
    )
    executed = tools.backup_execute_action({"proposal_id": proposal_id})
    assert executed["data"]["operation_id"]
    operation = store.read_operation(executed["data"]["operation_id"])
    assert operation["state"] == "succeeded"
    with pytest.raises(BackupError) as replay:
        tools.backup_execute_action({"proposal_id": proposal_id})
    assert replay.value.code == "denied"


def test_execute_denies_stale_finding_revision(tmp_path: Path):
    tools, ledger, store, _clock = _tools(tmp_path)
    _seed_finding(ledger)
    proposed = tools.backup_propose_action(
        {"target_alias": "media-archive", "action_id": "run-backup", "finding_id": "media-archive:stale-lock"}
    )
    proposal = store.read_proposal(proposed["data"]["proposal_id"])
    store.write_approval_file(
        {
            "version": 1,
            "proposal_id": proposal["proposal_id"],
            "target_alias": proposal["target_alias"],
            "action_id": proposal["action_id"],
            "finding_id": proposal["finding_id"],
            "finding_revision": proposal["finding_revision"],
            "nonce": proposal["nonce"],
            "expires_at": proposal["expires_at"],
            "approved_at": "2026-08-28T00:01:00Z",
        }
    )
    ledger.replace_findings(
        "media-archive",
        [
            {
                "finding_id": "media-archive:stale-lock",
                "target_alias": "media-archive",
                "kind": "stale-lock",
                "severity_class": "warning",
                "summary": "A stale backup lock is present",
                "observed_at": "2026-08-28T00:05:00Z",
                "receipt_ref": "receipt-2",
                "proposed_action_id": "run-backup",
                "blast_radius": "one registered restic target",
                "verification_statement": "compare the next snapshot receipt",
                "recovery_statement": "operator reruns the approved backup",
            }
        ],
    )
    with pytest.raises(BackupError) as caught:
        tools.backup_execute_action({"proposal_id": proposal["proposal_id"]})
    assert caught.value.code == "denied"


def test_concurrent_active_operations_are_rejected(tmp_path: Path):
    _tools_unused, ledger, store, clock = _tools(tmp_path)
    _seed_finding(ledger)
    store.write_operation(
        {
            "operation_id": "active1active1active1active1activ",
            "target_alias": "media-archive",
            "action_id": "run-backup",
            "state": "running",
            "created_at": "2026-08-28T00:00:00Z",
            "updated_at": "2026-08-28T00:00:00Z",
            "summary": "running",
            "receipt_ref": "receipt-live",
        }
    )
    with pytest.raises(BackupError) as caught:
        store.write_operation(
            {
                "operation_id": "active2active2active2active2activ",
                "target_alias": "media-archive",
                "action_id": "run-integrity-check",
                "state": "queued",
                "created_at": "2026-08-28T00:00:01Z",
                "updated_at": "2026-08-28T00:00:01Z",
                "summary": "queued",
                "receipt_ref": "receipt-next",
            }
        )
    assert caught.value.code == "denied"
    assert clock["value"] == NOW


def test_executor_appends_only_the_catalog_target_selector(tmp_path: Path):
    seen: list[ExecRequest] = []

    def runner(request: ExecRequest) -> PrivateExecResult:
        seen.append(request)
        return PrivateExecResult(stdout="")

    tools, ledger, store, _clock = _tools(tmp_path, runner=runner)
    _seed_finding(ledger)
    proposed = tools.backup_propose_action(
        {"target_alias": "media-archive", "action_id": "run-backup", "finding_id": "media-archive:stale-lock"}
    )
    proposal = store.read_proposal(proposed["data"]["proposal_id"])
    store.write_approval_file(
        {
            "version": 1,
            "proposal_id": proposal["proposal_id"],
            "target_alias": proposal["target_alias"],
            "action_id": proposal["action_id"],
            "finding_id": proposal["finding_id"],
            "finding_revision": proposal["finding_revision"],
            "nonce": proposal["nonce"],
            "expires_at": proposal["expires_at"],
            "approved_at": "2026-08-28T00:01:00Z",
        }
    )
    tools.backup_execute_action({"proposal_id": proposal["proposal_id"]})
    assert seen
    assert seen[0].args[-1] == "media-archive"
    assert seen[0].cwd == "/"


def _write_state_json(path: Path, record: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, separators=(",", ":"), sort_keys=True), encoding="utf-8")
    os.chmod(path.parent, 0o700)
    os.chmod(path, 0o600)


def _proposal_record(proposal_id: str, *, expires_at: str, finding_id: str) -> dict[str, object]:
    return {
        "version": 1,
        "proposal_id": proposal_id,
        "target_alias": "media-archive",
        "action_id": "run-backup",
        "finding_id": finding_id,
        "finding_revision": "a" * 64,
        "nonce": f"nonce-{proposal_id}"[:32],
        "created_at": "2026-08-27T00:00:00Z",
        "expires_at": expires_at,
    }


def _consumed_record(proposal: dict[str, object]) -> dict[str, object]:
    return {
        "version": 1,
        "proposal_id": proposal["proposal_id"],
        "target_alias": proposal["target_alias"],
        "action_id": proposal["action_id"],
        "finding_id": proposal["finding_id"],
        "finding_revision": proposal["finding_revision"],
        "nonce": proposal["nonce"],
        "expires_at": proposal["expires_at"],
        "approved_at": "2026-08-27T00:01:00Z",
        "consumed_at": "2026-08-27T00:02:00Z",
    }


def test_cleanup_bounds_expired_and_consumed_under_live_threshold(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(actions_mod, "MAX_PROPOSALS", 8)
    monkeypatch.setattr(actions_mod, "MAX_CONSUMED", 4)
    ids = iter(f"{index:032x}" for index in range(1, 20))
    actions = tmp_path / "actions"
    approvals = tmp_path / "approvals"
    actions.mkdir(mode=0o700)
    approvals.mkdir(mode=0o700)
    os.chmod(actions, 0o700)
    os.chmod(approvals, 0o700)
    store = BackupActionStore(
        action_state_path=str(actions),
        approval_dir=str(approvals),
        now=lambda: NOW,
        create_proposal_id=lambda: next(ids),
        create_nonce=lambda: "nonce1nonce1nonce1nonce1nonce1no",
    )
    store.ready()
    live = _proposal_record(
        "liveproposal1liveproposal1livepro", expires_at="2026-08-28T01:00:00Z", finding_id="live-finding"
    )
    _write_state_json(actions / "proposals" / f"{live['proposal_id']}.json", live)
    _write_state_json(actions / "consumed" / f"{live['proposal_id']}.json", _consumed_record(live))
    expired = [
        _proposal_record(f"{index:032x}", expires_at="2026-08-27T00:00:00Z", finding_id=f"expired-{index}")
        for index in range(10, 22)
    ]
    for record in expired:
        _write_state_json(actions / "proposals" / f"{record['proposal_id']}.json", record)
        _write_state_json(actions / "consumed" / f"{record['proposal_id']}.json", _consumed_record(record))
    assert len(list((actions / "proposals").glob("*.json"))) > 8
    assert len(list((actions / "consumed").glob("*.json"))) > 4
    created = store.create_proposal(
        {
            "target_alias": "media-archive",
            "action_id": "run-integrity-check",
            "finding_id": "fresh-finding",
            "finding_revision": "b" * 64,
        }
    )
    proposal_names = {path.name for path in (actions / "proposals").glob("*.json")}
    consumed_names = {path.name for path in (actions / "consumed").glob("*.json")}
    assert f"{live['proposal_id']}.json" in proposal_names
    assert f"{created['proposal_id']}.json" in proposal_names
    assert len(proposal_names) <= 8
    assert all(record["proposal_id"] + ".json" not in proposal_names for record in expired)
    assert len(consumed_names) <= 4
    assert f"{live['proposal_id']}.json" in consumed_names
    assert all(record["proposal_id"] + ".json" not in consumed_names for record in expired)
    store.write_approval_file(
        {
            "version": 1,
            "proposal_id": live["proposal_id"],
            "target_alias": live["target_alias"],
            "action_id": live["action_id"],
            "finding_id": live["finding_id"],
            "finding_revision": live["finding_revision"],
            "nonce": live["nonce"],
            "expires_at": live["expires_at"],
            "approved_at": "2026-08-28T00:01:00Z",
        }
    )
    with pytest.raises(BackupError) as replay:
        store.consume_approved_proposal(str(live["proposal_id"]))
    assert replay.value.code == "denied"


def test_executor_start_returns_queued_before_runner_releases(tmp_path: Path):
    tools, ledger, store, _clock = _tools(tmp_path)
    _seed_finding(ledger)
    proposed = tools.backup_propose_action(
        {"target_alias": "media-archive", "action_id": "run-backup", "finding_id": "media-archive:stale-lock"}
    )
    proposal = store.read_proposal(proposed["data"]["proposal_id"])
    store.write_approval_file(
        {
            "version": 1,
            "proposal_id": proposal["proposal_id"],
            "target_alias": proposal["target_alias"],
            "action_id": proposal["action_id"],
            "finding_id": proposal["finding_id"],
            "finding_revision": proposal["finding_revision"],
            "nonce": proposal["nonce"],
            "expires_at": proposal["expires_at"],
            "approved_at": "2026-08-28T00:01:00Z",
        }
    )
    entered = threading.Event()
    release = threading.Event()

    def runner(_request: ExecRequest) -> PrivateExecResult:
        entered.set()
        assert release.wait(timeout=2)
        return PrivateExecResult(stdout="")

    started: dict[str, object] = {}

    def call_start() -> None:
        executor = BackupActionExecutor(
            runtime=tools.executor.runtime,
            store=store,
            env={},
            now=lambda: NOW,
            create_operation_id=lambda: "operation2operation2operation2oper",
            create_receipt_ref=lambda: "op-receipt-2",
            secrets=["secret-token"],
            runner=runner,
        )
        started["operation"] = executor.start(
            {
                "target_alias": "media-archive",
                "action_id": "run-backup",
                "proposal_id": proposal["proposal_id"],
            }
        )

    worker = threading.Thread(target=call_start)
    worker.start()
    assert entered.wait(timeout=2)
    assert "operation" in started
    operation = started["operation"]
    assert operation["state"] == "queued"
    release.set()
    worker.join(timeout=2)
    assert not worker.is_alive()
    deadline = time.monotonic() + 2
    terminal = store.read_operation(str(operation["operation_id"]))
    while terminal is not None and terminal["state"] not in {"succeeded", "failed"} and time.monotonic() < deadline:
        time.sleep(0.01)
        terminal = store.read_operation(str(operation["operation_id"]))
    assert terminal is not None
    assert terminal["state"] == "succeeded"
