"""Action catalog, store concurrency, and restarter tests."""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from brigade.grokbot_fleet import actions as actions_mod
from brigade.grokbot_fleet.actions import (
    EXECUTABLE_ACTION,
    FleetActionStore,
    FleetActionStoreError,
    ResearchBridgeRestarter,
    finding_revision,
    is_executable_remediation,
    write_fleet_approval_file,
)
from brigade.grokbot_fleet.contracts import FleetError
from brigade.grokbot_fleet.exec import PrivateExecResult

FINDING = {
    "finding_id": EXECUTABLE_ACTION["finding_id"],
    "target_alias": EXECUTABLE_ACTION["target_alias"],
    "proposed_action_id": EXECUTABLE_ACTION["action_id"],
    "reason": "Service health is unhealthy",
    "blast_radius": "one registered service",
    "verification_id": EXECUTABLE_ACTION["verification_id"],
    "rollback_id": EXECUTABLE_ACTION["rollback_id"],
    "observed_at": "2026-08-27T12:00:00Z",
    "incident_receipt_ref": "receipt-1",
}
PROPOSAL = {
    "version": 1,
    "proposal_id": "a" * 32,
    "finding_id": EXECUTABLE_ACTION["finding_id"],
    "service_id": EXECUTABLE_ACTION["service_id"],
    "target_alias": EXECUTABLE_ACTION["target_alias"],
    "finding_revision": "b" * 64,
    "system_revision": "c" * 64,
    "action_id": EXECUTABLE_ACTION["action_id"],
    "verification_id": EXECUTABLE_ACTION["verification_id"],
    "rollback_id": EXECUTABLE_ACTION["rollback_id"],
    "nonce": "d" * 32,
    "created_at": "2026-08-27T12:00:00Z",
    "expires_at": "2026-08-27T12:15:00Z",
}


def _dirs(tmp_path: Path) -> tuple[str, str]:
    actions = tmp_path / "actions"
    approvals = tmp_path / "approvals"
    actions.mkdir(mode=0o700)
    approvals.mkdir(mode=0o700)
    os.chmod(actions, 0o700)
    os.chmod(approvals, 0o700)
    return str(actions), str(approvals)


def test_executable_remediation_is_narrow():
    assert is_executable_remediation(FINDING)
    assert not is_executable_remediation({**FINDING, "finding_id": "control-plane:unreachable"})
    revision = finding_revision(FINDING)
    assert len(revision) == 64
    assert revision.isalnum()


def test_action_store_is_exclusive_and_replay_safe(tmp_path: Path):
    actions, approvals = _dirs(tmp_path)
    store = FleetActionStore(action_state_path=actions, approval_dir=approvals)
    store.ready()
    store.create_proposal(PROPOSAL)
    with pytest.raises(FleetActionStoreError):
        store.create_proposal(PROPOSAL)
    approval = {
        **{key: PROPOSAL[key] for key in PROPOSAL if key != "created_at"},
        "approved_at": "2026-08-27T12:01:00Z",
    }
    write_fleet_approval_file(approvals, approval)
    assert store.read_approval(PROPOSAL["proposal_id"])["proposal_id"] == PROPOSAL["proposal_id"]
    claimed = store.claim_consumed({**approval, "consumed_at": "2026-08-27T12:02:00Z"})
    assert claimed["consumed_at"] == "2026-08-27T12:02:00Z"
    with pytest.raises(FleetActionStoreError) as caught:
        store.claim_consumed({**approval, "consumed_at": "2026-08-27T12:03:00Z"})
    assert caught.value.outcome == "replayed"


def test_action_store_concurrency_serializes_claims(tmp_path: Path):
    actions, approvals = _dirs(tmp_path)
    store = FleetActionStore(action_state_path=actions, approval_dir=approvals)
    store.ready()
    store.create_proposal(PROPOSAL)
    approval = {
        **{key: PROPOSAL[key] for key in PROPOSAL if key != "created_at"},
        "approved_at": "2026-08-27T12:01:00Z",
    }
    write_fleet_approval_file(approvals, approval)
    results: list[str] = []

    def worker() -> None:
        try:
            store.claim_consumed({**approval, "consumed_at": "2026-08-27T12:02:00Z"})
            results.append("ok")
        except FleetActionStoreError as exc:
            results.append(exc.outcome)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count("ok") == 1
    assert results.count("replayed") == 3


def test_restarter_observes_and_restarts_with_fixed_argv():
    calls: list[tuple[str, ...]] = []

    def runner(request):
        calls.append(request.args)
        if request.args[1] == "show":
            return PrivateExecResult(
                stdout=(
                    "Id=research-bridge.service\n"
                    "ActiveState=active\n"
                    "SubState=running\n"
                    "StateChangeTimestampMonotonic=12\n"
                    "InvocationID=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
                )
            )
        return PrivateExecResult(stdout="")

    restarter = ResearchBridgeRestarter(
        systemctl_file="/usr/bin/systemctl",
        mapping={"unit": "research-bridge.service", "manager": "user"},
        env={},
        runner=runner,
    )
    revision = restarter.observe_research_bridge_revision()
    assert revision["health_class"] == "healthy"
    restarter.restart_research_bridge()
    assert ("--user", "restart", "research-bridge.service") in calls
    with pytest.raises(FleetError) as caught:
        ResearchBridgeRestarter(
            systemctl_file="/usr/bin/systemctl",
            mapping={"unit": "research-bridge.service", "manager": "system"},
            env={},
            runner=runner,
        ).observe_research_bridge_revision()
    assert caught.value.code == "unavailable"
    assert "research-bridge.service" not in str(caught.value)


def test_action_store_fails_closed_on_corrupt_proposal_and_symlink(tmp_path: Path):
    actions, approvals = _dirs(tmp_path)
    store = FleetActionStore(action_state_path=actions, approval_dir=approvals)
    store.ready()
    store.create_proposal(PROPOSAL)
    proposal_path = Path(actions) / "proposals" / f"{PROPOSAL['proposal_id']}.json"
    proposal_path.write_text("{broken", encoding="utf-8")
    os.chmod(proposal_path, 0o600)
    with pytest.raises(FleetError) as caught:
        store.read_proposal(PROPOSAL["proposal_id"])
    assert caught.value.code == "protocol_error"
    assert "{broken" not in str(caught.value)
    if os.name == "posix":
        linked = tmp_path / "linked-actions"
        linked.symlink_to(actions)
        with pytest.raises(FleetError) as linked_caught:
            FleetActionStore(action_state_path=str(linked), approval_dir=approvals).ready()
        assert str(linked) not in str(linked_caught.value)


LIVE_EXPIRES = "2099-01-01T00:00:00Z"
EXPIRED_AT = "2020-01-01T00:00:00Z"


def _proposal(
    proposal_id: str,
    *,
    expires_at: str = LIVE_EXPIRES,
    created_at: str = "2026-08-27T12:00:00Z",
    system_revision: str | None = None,
) -> dict[str, str]:
    return {
        **PROPOSAL,
        "proposal_id": proposal_id,
        "nonce": proposal_id,
        "created_at": created_at,
        "expires_at": expires_at,
        "system_revision": system_revision or PROPOSAL["system_revision"],
    }


def _receipt(receipt_id: str, proposal_id: str, *, kind: str = "rejection", outcome: str = "denied") -> dict[str, str]:
    return {
        "version": 1,
        "receipt_id": receipt_id,
        "kind": kind,
        "proposal_id": proposal_id,
        "outcome": outcome,
        "created_at": "2026-08-27T12:10:00Z",
    }


def _store(tmp_path: Path) -> tuple[FleetActionStore, Path, str]:
    actions, approvals = _dirs(tmp_path)
    store = FleetActionStore(action_state_path=actions, approval_dir=approvals)
    store.ready()
    return store, Path(actions), approvals


def _json_names(directory: Path) -> set[str]:
    return {path.name for path in directory.iterdir() if path.suffix == ".json"}


def _directory_bytes(directory: Path) -> int:
    return sum(path.stat().st_size for path in directory.iterdir() if path.is_file())


def test_action_store_caps_proposals_and_preserves_live_claim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(actions_mod, "MAX_PROPOSALS", 3)
    monkeypatch.setattr(actions_mod, "MAX_PROPOSAL_BYTES", 262_144)
    store, root, approvals = _store(tmp_path)
    live = _proposal("a" * 32)
    expired = [
        _proposal(f"{index:032x}", expires_at=EXPIRED_AT, system_revision=f"{index + 1:064x}") for index in range(1, 6)
    ]
    store.create_proposal(live)
    store.create_proposal(expired[0])
    store.create_proposal(expired[1])
    for record in expired[2:]:
        store.create_proposal(record)
    live_path = root / "proposals" / f"{live['proposal_id']}.json"
    assert live_path.is_file()
    assert store.read_proposal(live["proposal_id"])["proposal_id"] == live["proposal_id"]
    assert len(_json_names(root / "proposals")) <= 3
    assert _directory_bytes(root / "proposals") <= 262_144
    approval = {
        **{key: live[key] for key in live if key != "created_at"},
        "approved_at": "2026-08-27T12:09:00Z",
    }
    write_fleet_approval_file(approvals, approval)
    claimed = store.claim_consumed({**approval, "consumed_at": "2026-08-27T12:09:30Z"})
    assert claimed["proposal_id"] == live["proposal_id"]
    assert (root / "consumed" / f"{live['proposal_id']}.json").is_file()
    assert (root / "consumed" / f"{live['system_revision']}.json").is_file()
    overflow = _proposal("b" * 32, expires_at=EXPIRED_AT, system_revision="d" * 64)
    store.create_proposal(overflow)
    assert store.read_proposal(live["proposal_id"]) is not None
    assert (root / "consumed" / f"{live['proposal_id']}.json").is_file()
    later = _proposal("c" * 32, system_revision="e" * 64)
    store.create_proposal(later)
    later_approval = {
        **{key: later[key] for key in later if key != "created_at"},
        "approved_at": "2026-08-27T12:10:00Z",
    }
    write_fleet_approval_file(approvals, later_approval)
    later_claimed = store.claim_consumed({**later_approval, "consumed_at": "2026-08-27T12:10:30Z"})
    assert later_claimed["proposal_id"] == later["proposal_id"]
    assert len(_json_names(root / "proposals")) <= 3


def test_action_store_caps_live_proposals_by_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    store, root, _approvals = _store(tmp_path)
    first = _proposal("a" * 32)
    store.create_proposal(first)
    size = (root / "proposals" / f"{first['proposal_id']}.json").stat().st_size
    monkeypatch.setattr(actions_mod, "MAX_PROPOSALS", 64)
    monkeypatch.setattr(actions_mod, "MAX_PROPOSAL_BYTES", size + 10)
    with pytest.raises(FleetActionStoreError) as caught:
        store.create_proposal(_proposal("b" * 32))
    assert caught.value.outcome == "denied"
    assert store.read_proposal(first["proposal_id"]) is not None
    assert _json_names(root / "proposals") == {f"{first['proposal_id']}.json"}
    assert _directory_bytes(root / "proposals") <= size + 10


def test_action_store_caps_receipts_and_dedupes_identical_rejections(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(actions_mod, "MAX_RECEIPTS", 3)
    monkeypatch.setattr(actions_mod, "MAX_RECEIPT_BYTES", 262_144)
    store, root, _approvals = _store(tmp_path)
    live = _proposal("a" * 32)
    expired_id = "1" * 32
    store.create_proposal(live)
    store.create_proposal(_proposal(expired_id, expires_at=EXPIRED_AT, system_revision="f" * 64))
    store.write_receipt(_receipt("2" * 32, live["proposal_id"], kind="proposal", outcome="created"))
    for index in range(8):
        store.write_receipt(_receipt(f"{index + 3:032x}", expired_id))
    names = _json_names(root / "receipts")
    assert len(names) <= 3
    assert _directory_bytes(root / "receipts") <= 262_144
    live_receipts = []
    for path in (root / "receipts").iterdir():
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("proposal_id") == live["proposal_id"]:
            live_receipts.append(payload)
    assert live_receipts
    before = len(_json_names(root / "receipts"))
    for index in range(12):
        store.write_receipt(_receipt(f"{index + 20:032x}", live["proposal_id"]))
    assert len(_json_names(root / "receipts")) == before
    later = _proposal("c" * 32, system_revision="e" * 64)
    store.create_proposal(later)
    store.write_receipt(_receipt("d" * 32, later["proposal_id"], kind="proposal", outcome="created"))
    assert store.read_proposal(later["proposal_id"])["proposal_id"] == later["proposal_id"]


def test_action_store_partial_and_zero_writes_fail_and_do_not_consume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    store, root, approvals = _store(tmp_path)
    real_write = os.write
    state = {"mode": "partial"}

    def flaky_write(fd: int, data: bytes) -> int:
        if state["mode"] == "ok":
            return real_write(fd, data)
        if state["mode"] == "partial":
            state["mode"] = "zero"
            chunk = data[: max(1, len(data) // 2)]
            return real_write(fd, chunk)
        return 0

    monkeypatch.setattr(os, "write", flaky_write)
    with pytest.raises(FleetError) as caught:
        store.create_proposal(PROPOSAL)
    assert caught.value.code == "protocol_error"
    proposal_path = root / "proposals" / f"{PROPOSAL['proposal_id']}.json"
    assert not proposal_path.exists()

    state["mode"] = "ok"
    store.create_proposal(PROPOSAL)
    approval = {
        **{key: PROPOSAL[key] for key in PROPOSAL if key != "created_at"},
        "approved_at": "2026-08-27T12:01:00Z",
    }
    write_fleet_approval_file(approvals, approval)
    state["mode"] = "partial"
    with pytest.raises(FleetError):
        store.claim_consumed({**approval, "consumed_at": "2026-08-27T12:02:00Z"})
    assert not (root / "consumed" / f"{PROPOSAL['proposal_id']}.json").exists()
    assert not (root / "consumed" / f"{PROPOSAL['system_revision']}.json").exists()

    state["mode"] = "ok"
    claimed = store.claim_consumed({**approval, "consumed_at": "2026-08-27T12:02:00Z"})
    assert claimed["consumed_at"] == "2026-08-27T12:02:00Z"


def _approval_for(record: dict[str, str], approved_at: str = "2026-08-27T12:09:00Z") -> dict[str, str]:
    return {**{key: record[key] for key in record if key != "created_at"}, "approved_at": approved_at}


def _seed_consumed_pair(root: Path, record: dict[str, str], consumed_at: str = "2026-08-27T12:09:30Z") -> None:
    approval = _approval_for(record)
    consumed = {**approval, "consumed_at": consumed_at}
    revision_claim = {
        "version": 1,
        "proposal_id": record["proposal_id"],
        "system_revision": record["system_revision"],
        "claimed_at": consumed_at,
    }
    consumed_dir = root / "consumed"
    for name, payload in (
        (f"{record['system_revision']}.json", revision_claim),
        (f"{record['proposal_id']}.json", consumed),
    ):
        path = consumed_dir / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        os.chmod(path, 0o600)


def test_action_store_caps_consumed_and_preserves_live_claim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(actions_mod, "MAX_CONSUMED", 4)
    monkeypatch.setattr(actions_mod, "MAX_CONSUMED_BYTES", 262_144)
    store, root, approvals = _store(tmp_path)
    live = _proposal("a" * 32, system_revision="c" * 64)
    expired = [
        _proposal(f"{index:032x}", expires_at=EXPIRED_AT, system_revision=f"{index + 1:064x}") for index in range(1, 6)
    ]
    store.create_proposal(live)
    for record in expired:
        store.create_proposal(record)
    _seed_consumed_pair(root, live)
    for record in expired:
        _seed_consumed_pair(root, record)
    assert len(_json_names(root / "consumed")) > 4

    later = _proposal("b" * 32, system_revision="d" * 64)
    store.create_proposal(later)
    later_approval = _approval_for(later, "2026-08-27T12:10:00Z")
    write_fleet_approval_file(approvals, later_approval)
    later_claimed = store.claim_consumed({**later_approval, "consumed_at": "2026-08-27T12:10:30Z"})
    assert later_claimed["proposal_id"] == later["proposal_id"]

    consumed_names = _json_names(root / "consumed")
    assert len(consumed_names) <= 4
    assert _directory_bytes(root / "consumed") <= 262_144
    assert f"{live['proposal_id']}.json" in consumed_names
    assert f"{live['system_revision']}.json" in consumed_names
    assert f"{later['proposal_id']}.json" in consumed_names
    assert f"{later['system_revision']}.json" in consumed_names
    for record in expired:
        assert f"{record['proposal_id']}.json" not in consumed_names
        assert f"{record['system_revision']}.json" not in consumed_names

    live_approval = _approval_for(live)
    with pytest.raises(FleetActionStoreError) as replayed:
        store.claim_consumed({**live_approval, "consumed_at": "2026-08-27T12:11:00Z"})
    assert replayed.value.outcome == "replayed"
    assert (root / "consumed" / f"{live['proposal_id']}.json").is_file()
    assert (root / "consumed" / f"{live['system_revision']}.json").is_file()

    newest = _proposal("e" * 32, expires_at="2099-12-31T00:00:00Z", system_revision="f" * 64)
    store.create_proposal(newest)
    newest_approval = _approval_for(newest, "2026-08-27T12:12:00Z")
    write_fleet_approval_file(approvals, newest_approval)
    with pytest.raises(FleetActionStoreError) as full:
        store.claim_consumed({**newest_approval, "consumed_at": "2026-08-27T12:12:30Z"})
    assert full.value.outcome == "denied"
    assert (root / "consumed" / f"{live['proposal_id']}.json").is_file()
    assert (root / "consumed" / f"{later['proposal_id']}.json").is_file()

    monkeypatch.setattr(actions_mod, "_utc_now", lambda: datetime(2099, 1, 2, tzinfo=timezone.utc))
    newest_claimed = store.claim_consumed({**newest_approval, "consumed_at": "2026-08-27T12:13:00Z"})
    assert newest_claimed["proposal_id"] == newest["proposal_id"]
    assert (root / "consumed" / f"{newest['proposal_id']}.json").is_file()
    assert (root / "consumed" / f"{newest['system_revision']}.json").is_file()


def test_action_store_fails_closed_on_orphaned_consumed(tmp_path: Path):
    store, root, approvals = _store(tmp_path)
    live = _proposal("a" * 32)
    later = _proposal("b" * 32, system_revision="d" * 64)
    store.create_proposal(live)
    store.create_proposal(later)
    orphan = root / "consumed" / f"{live['system_revision']}.json"
    orphan.write_text(
        json.dumps(
            {
                "version": 1,
                "proposal_id": live["proposal_id"],
                "system_revision": live["system_revision"],
                "claimed_at": "2026-08-27T12:09:30Z",
            }
        ),
        encoding="utf-8",
    )
    os.chmod(orphan, 0o600)
    approval = _approval_for(later)
    write_fleet_approval_file(approvals, approval)
    with pytest.raises(FleetError) as caught:
        store.claim_consumed({**approval, "consumed_at": "2026-08-27T12:10:30Z"})
    assert caught.value.code == "protocol_error"
    assert str(orphan) not in str(caught.value)


def test_receipt_reservation_releases_and_does_not_pin_capacity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(actions_mod, "MAX_RECEIPTS", 3)
    monkeypatch.setattr(actions_mod, "MAX_RECEIPT_BYTES", 262_144)
    store, root, _approvals = _store(tmp_path)
    live = _proposal("a" * 32)
    store.create_proposal(live)
    store.write_receipt(_receipt("1" * 32, live["proposal_id"], kind="proposal", outcome="created"))
    reserved = [
        _receipt("2" * 32, live["proposal_id"], kind="execution", outcome="verified"),
        _receipt("3" * 32, live["proposal_id"], kind="verification", outcome="verified"),
    ]
    reservation_id = store.reserve_receipt_capacity(reserved)
    assert reservation_id != live["proposal_id"]
    assert (root / "reservations" / f"{reservation_id}.json").is_file()
    assert not (root / "reservations" / f"{live['proposal_id']}.json").exists()
    with pytest.raises(FleetActionStoreError) as caught:
        store.write_receipt(_receipt("4" * 32, live["proposal_id"], kind="rejection", outcome="denied"))
    assert caught.value.outcome == "denied"
    store.release_receipt_reservation(reservation_id)
    store.write_receipt(_receipt("4" * 32, live["proposal_id"], kind="rejection", outcome="denied"))
    assert (root / "receipts" / f"{'4' * 32}.json").is_file()
    (root / "receipts" / f"{'4' * 32}.json").unlink()
    later = [
        _receipt("5" * 32, live["proposal_id"], kind="execution", outcome="verified"),
        _receipt("6" * 32, live["proposal_id"], kind="verification", outcome="verified"),
    ]
    reservation_id = store.reserve_receipt_capacity(later)
    store.commit_reserved_receipts(reservation_id, later)
    assert (root / "receipts" / f"{'5' * 32}.json").is_file()
    assert (root / "receipts" / f"{'6' * 32}.json").is_file()
    assert list((root / "reservations").glob("*.json")) == []


def test_live_reservation_is_exclusive_and_token_scoped(tmp_path: Path):
    store, root, _approvals = _store(tmp_path)
    live = _proposal("a" * 32)
    store.create_proposal(live)
    reserved = [
        _receipt("2" * 32, live["proposal_id"], kind="execution", outcome="verified"),
        _receipt("3" * 32, live["proposal_id"], kind="verification", outcome="verified"),
    ]
    reservation_id = store.reserve_receipt_capacity(reserved)
    assert reservation_id != live["proposal_id"]
    with pytest.raises(FleetActionStoreError) as caught:
        store.reserve_receipt_capacity(
            [
                _receipt("4" * 32, live["proposal_id"], kind="execution", outcome="verified"),
                _receipt("5" * 32, live["proposal_id"], kind="verification", outcome="verified"),
            ]
        )
    assert caught.value.outcome == "denied"
    assert caught.value.message == "Fleet request was denied"
    assert (root / "reservations" / f"{reservation_id}.json").is_file()
    store.release_receipt_reservation(live["proposal_id"])
    store.release_receipt_reservation("b" * 32)
    assert (root / "reservations" / f"{reservation_id}.json").is_file()
    store.commit_reserved_receipts(reservation_id, reserved)
    assert (root / "receipts" / f"{'2' * 32}.json").is_file()
    assert (root / "receipts" / f"{'3' * 32}.json").is_file()
    assert list((root / "reservations").glob("*.json")) == []


def test_receipt_reservation_rejects_a_changed_static_contract(tmp_path: Path):
    store, _root, _approvals = _store(tmp_path)
    live = _proposal("a" * 32)
    store.create_proposal(live)
    reserved = [
        _receipt("2" * 32, live["proposal_id"], kind="execution", outcome="unverified"),
        _receipt("3" * 32, live["proposal_id"], kind="verification", outcome="unverified"),
    ]
    reservation_id = store.reserve_receipt_capacity(reserved)
    committed = [
        {**reserved[0], "outcome": "verified"},
        {**reserved[1], "outcome": "verified", "verification_id": "different-verification"},
    ]
    with pytest.raises(FleetError) as caught:
        store.commit_reserved_receipts(reservation_id, committed)
    assert caught.value.code == "protocol_error"


def test_receipt_reservation_recovers_only_exact_partially_persisted_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    store, root, _approvals = _store(tmp_path)
    live = _proposal("a" * 32)
    store.create_proposal(live)
    reserved = [
        _receipt("2" * 32, live["proposal_id"], kind="execution", outcome="verified"),
        _receipt("3" * 32, live["proposal_id"], kind="verification", outcome="verified"),
    ]
    reservation_id = store.reserve_receipt_capacity(reserved)

    write_exclusive_json = actions_mod._write_exclusive_json

    def fail_second_terminal_write(path: Path, record: dict[str, object]) -> None:
        if path.name == f"{'3' * 32}.json":
            raise OSError("simulated crash")
        write_exclusive_json(path, record)

    monkeypatch.setattr(actions_mod, "_write_exclusive_json", fail_second_terminal_write)
    with pytest.raises(OSError, match="simulated crash"):
        store.commit_reserved_receipts(reservation_id, reserved)

    # The first receipt and the reservation must survive an interrupted second write.
    assert (root / "receipts" / f"{'2' * 32}.json").is_file()
    assert (root / "reservations" / f"{reservation_id}.json").is_file()
    monkeypatch.setattr(actions_mod, "_write_exclusive_json", write_exclusive_json)
    store.commit_reserved_receipts(reservation_id, reserved)

    assert (root / "receipts" / f"{'2' * 32}.json").is_file()
    assert (root / "receipts" / f"{'3' * 32}.json").is_file()
    assert not (root / "reservations" / f"{reservation_id}.json").exists()

    mismatched = [
        _receipt("4" * 32, live["proposal_id"], kind="execution", outcome="verified"),
        _receipt("5" * 32, live["proposal_id"], kind="verification", outcome="verified"),
    ]
    mismatch_reservation_id = store.reserve_receipt_capacity(mismatched)
    store.write_receipt({**mismatched[0], "outcome": "failed"})

    with pytest.raises(FleetError) as caught:
        store.commit_reserved_receipts(mismatch_reservation_id, mismatched)

    assert caught.value.code == "protocol_error"
    assert (root / "reservations" / f"{mismatch_reservation_id}.json").is_file()
    assert not (root / "receipts" / f"{'5' * 32}.json").exists()


def test_claim_cleanup_replaces_a_provisional_receipt_with_the_terminal_set(tmp_path: Path):
    store, _root, _approvals = _store(tmp_path)
    proposal_id = "a" * 32
    common = {
        "version": 2,
        "proposal_id": proposal_id,
        "target_alias": "control-plane",
        "holder": "b" * 32,
        "reservation_id": "c" * 32,
        "receipt_pending": True,
        "release_pending": True,
    }
    provisional = _receipt("d" * 32, proposal_id, kind="execution", outcome="unverified")
    terminal = _receipt("e" * 32, proposal_id, kind="rejection", outcome="replayed")
    store.write_claim_cleanup({**common, "terminal_receipts": [provisional]})
    store.write_claim_cleanup({**common, "terminal_receipts": [terminal]})
    cleanup = store.read_claim_cleanup()
    assert cleanup is not None
    assert cleanup["terminal_receipts"] == [terminal]
