"""n8n-operator proposals, operator-only approvals, and consume-before-write."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from brigade import grokbot_ops
from brigade.grokbot_n8n.actions import (
    APPROVAL_KEYS,
    MAX_CONSUMED,
    MAX_PROPOSALS,
    PROPOSAL_TTL_MS,
    N8nActionStore,
    target_revision,
)
from brigade.grokbot_n8n.contracts import N8nError
from brigade.grokbot_n8n.tools import N8nOperatorTools, project_workflow

NOW = datetime(2026, 8, 30, tzinfo=timezone.utc)
PROPOSAL_ID = "a" * 32


def _dirs(tmp_path: Path) -> tuple[N8nActionStore, dict[str, datetime]]:
    actions = tmp_path / "actions"
    approvals = tmp_path / "approvals"
    actions.mkdir(mode=0o700)
    approvals.mkdir(mode=0o700)
    os.chmod(actions, 0o700)
    os.chmod(approvals, 0o700)
    clock = {"value": NOW}

    store = N8nActionStore(
        action_state_path=str(actions),
        approval_dir=str(approvals),
        now=lambda: clock["value"],
        create_proposal_id=lambda: PROPOSAL_ID,
    )
    store.ready()
    return store, clock


class _FakeClient:
    def __init__(self, workflow: dict[str, object]):
        self.workflow = dict(workflow)
        self.execution = None
        self.mutations: list[tuple[str, str]] = []
        self.fail_mutate = False

    def list_workflows(self):
        return {"data": [self.workflow]}

    def get_workflow(self, workflow_id: str):
        return dict(self.workflow)

    def list_executions(self):
        return {"data": []}

    def get_execution(self, execution_id: str):
        raise N8nError("not_found")

    def mutate(self, action_id: str, target_id: str):
        if self.fail_mutate:
            raise N8nError("unavailable")
        self.mutations.append((action_id, target_id))
        return {"ok": True}


def _workflow() -> dict[str, object]:
    return {
        "id": "wf1",
        "name": "demo",
        "active": True,
        "isArchived": False,
        "updatedAt": "2026-08-30T00:00:00.000Z",
    }


def _tools(tmp_path: Path, client: _FakeClient | None = None):
    store, clock = _dirs(tmp_path)
    fake = client or _FakeClient(_workflow())
    tools = N8nOperatorTools(
        client=fake,
        store=store,
        now=lambda: clock["value"],
        request_id=lambda: "request-1",
        secrets=["n8n-placeholder-key-not-real"],
    )
    return tools, store, clock, fake


def _write_approval(store: N8nActionStore, proposal: dict[str, object], **overrides: object) -> None:
    record = {
        "version": 1,
        "proposal_id": proposal["proposal_id"],
        "action_id": proposal["action_id"],
        "target_id": proposal["target_id"],
        "target_type": proposal["target_type"],
        "revision": proposal["revision"],
        "approved_at": "2026-08-30T00:01:00Z",
    }
    record.update(overrides)
    path = Path(store.approval_dir) / f"{proposal['proposal_id']}.json"
    path.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
    os.chmod(path, 0o600)


def test_proposal_binds_revision_and_expires_after_15_minutes(tmp_path: Path):
    tools, store, clock, fake = _tools(tmp_path)
    proposed = tools.call_tool("n8n_propose_action", {"action_id": "deactivate-workflow", "target_id": "wf1"})
    proposal = store.read_proposal(proposed["data"]["proposal_id"])
    assert proposal is not None
    assert proposal["action_id"] == "deactivate-workflow"
    assert proposal["target_id"] == "wf1"
    assert proposal["target_type"] == "workflow"
    assert proposal["revision"] == target_revision(project_workflow(fake.workflow))
    assert proposed["data"]["expires_at"] == "2026-08-30T00:15:00Z"
    assert PROPOSAL_TTL_MS == 15 * 60 * 1000
    clock["value"] = NOW + timedelta(milliseconds=PROPOSAL_TTL_MS + 1)
    _write_approval(store, proposal)
    with pytest.raises(N8nError) as caught:
        tools.call_tool("n8n_execute_action", {"proposal_id": proposal["proposal_id"]})
    assert caught.value.code == "denied"
    assert fake.mutations == []


def test_execute_requires_operator_approval_with_exact_schema(tmp_path: Path):
    tools, store, _clock, fake = _tools(tmp_path)
    proposed = tools.call_tool("n8n_propose_action", {"action_id": "archive-workflow", "target_id": "wf1"})
    proposal_id = proposed["data"]["proposal_id"]
    with pytest.raises(N8nError):
        tools.call_tool("n8n_execute_action", {"proposal_id": proposal_id})
    assert fake.mutations == []
    proposal = store.read_proposal(proposal_id)
    assert proposal is not None
    assert APPROVAL_KEYS == {
        "version",
        "proposal_id",
        "action_id",
        "target_id",
        "target_type",
        "revision",
        "approved_at",
    }
    path = Path(store.approval_dir) / f"{proposal_id}.json"
    path.write_text(json.dumps({"ok": True}), encoding="utf-8")
    os.chmod(path, 0o600)
    with pytest.raises(N8nError) as caught:
        tools.call_tool("n8n_execute_action", {"proposal_id": proposal_id})
    assert caught.value.code == "denied"
    path.unlink()
    _write_approval(store, proposal, revision="0" * 64)
    with pytest.raises(N8nError):
        tools.call_tool("n8n_execute_action", {"proposal_id": proposal_id})
    assert fake.mutations == []
    _write_approval(store, proposal)
    result = tools.call_tool("n8n_execute_action", {"proposal_id": proposal_id})
    assert result["data"]["state"] == "consumed"
    assert fake.mutations == [("archive-workflow", "wf1")]


def test_execute_is_single_use_and_denies_changed_revision(tmp_path: Path):
    tools, store, _clock, fake = _tools(tmp_path)
    proposed = tools.call_tool("n8n_propose_action", {"action_id": "unarchive-workflow", "target_id": "wf1"})
    proposal = store.read_proposal(proposed["data"]["proposal_id"])
    assert proposal is not None
    _write_approval(store, proposal)
    fake.workflow["updatedAt"] = "2026-08-30T00:10:00.000Z"
    with pytest.raises(N8nError) as caught:
        tools.call_tool("n8n_execute_action", {"proposal_id": proposal["proposal_id"]})
    assert caught.value.code == "denied"
    assert fake.mutations == []
    fake.workflow["updatedAt"] = "2026-08-30T00:00:00.000Z"
    first = tools.call_tool("n8n_execute_action", {"proposal_id": proposal["proposal_id"]})
    assert first["data"]["state"] == "consumed"
    with pytest.raises(N8nError) as caught:
        tools.call_tool("n8n_execute_action", {"proposal_id": proposal["proposal_id"]})
    assert caught.value.code == "denied"
    assert fake.mutations == [("unarchive-workflow", "wf1")]


def test_failed_write_remains_consumed_without_retry(tmp_path: Path):
    client = _FakeClient(_workflow())
    client.fail_mutate = True
    tools, store, _clock, fake = _tools(tmp_path, client)
    fake.execution = {
        "id": "ex1",
        "workflowId": "wf1",
        "status": "running",
        "startedAt": "2026-08-30T00:00:00.000Z",
        "stoppedAt": None,
        "mode": "manual",
    }

    def get_execution(execution_id: str):
        return dict(fake.execution)

    fake.get_execution = get_execution  # type: ignore[method-assign]
    proposed = tools.call_tool("n8n_propose_action", {"action_id": "cancel-execution", "target_id": "ex1"})
    proposal = store.read_proposal(proposed["data"]["proposal_id"])
    assert proposal is not None
    _write_approval(store, proposal)
    with pytest.raises(N8nError) as caught:
        tools.call_tool("n8n_execute_action", {"proposal_id": proposal["proposal_id"]})
    assert caught.value.code == "unavailable"
    status = tools.call_tool("n8n_action_status", {"proposal_id": proposal["proposal_id"]})
    assert status["data"]["state"] == "failed"
    assert "/actions" not in str(status)
    assert "n8n-placeholder-key-not-real" not in str(status)
    with pytest.raises(N8nError):
        tools.call_tool("n8n_execute_action", {"proposal_id": proposal["proposal_id"]})
    assert fake.mutations == []


def test_approval_must_be_operator_owned_mode_0600_regular_file(tmp_path: Path):
    tools, store, _clock, fake = _tools(tmp_path)
    proposed = tools.call_tool("n8n_propose_action", {"action_id": "deactivate-workflow", "target_id": "wf1"})
    proposal = store.read_proposal(proposed["data"]["proposal_id"])
    assert proposal is not None
    _write_approval(store, proposal)
    path = Path(store.approval_dir) / f"{proposal['proposal_id']}.json"
    os.chmod(path, 0o644)
    with pytest.raises(N8nError) as caught:
        tools.call_tool("n8n_execute_action", {"proposal_id": proposal["proposal_id"]})
    assert caught.value.code == "denied"
    body = path.read_text(encoding="utf-8")
    path.unlink()
    real = tmp_path / "real-approval.json"
    real.write_text(body, encoding="utf-8")
    os.chmod(real, 0o600)
    path.symlink_to(real)
    with pytest.raises(N8nError):
        tools.call_tool("n8n_execute_action", {"proposal_id": proposal["proposal_id"]})
    assert fake.mutations == []


def test_action_status_states_are_explicit_and_do_not_leak_paths(tmp_path: Path):
    tools, store, clock, _fake = _tools(tmp_path)
    proposed = tools.call_tool("n8n_propose_action", {"action_id": "deactivate-workflow", "target_id": "wf1"})
    proposal_id = proposed["data"]["proposal_id"]
    pending = tools.call_tool("n8n_action_status", {"proposal_id": proposal_id})
    assert pending["data"]["state"] == "pending"
    proposal = store.read_proposal(proposal_id)
    assert proposal is not None
    _write_approval(store, proposal)
    approved = tools.call_tool("n8n_action_status", {"proposal_id": proposal_id})
    assert approved["data"]["state"] == "approved"
    clock["value"] = NOW + timedelta(minutes=16)
    expired = tools.call_tool("n8n_action_status", {"proposal_id": proposal_id})
    assert expired["data"]["state"] == "expired"
    missing = tools.call_tool("n8n_action_status", {"proposal_id": "b" * 32})
    assert missing["data"]["state"] == "unknown"
    for payload in (pending, approved, expired, missing):
        dumped = json.dumps(payload)
        assert "approvals" not in dumped
        assert str(tmp_path) not in dumped


def _attacker_proposal(revision: str = "f" * 64) -> dict[str, object]:
    return {
        "version": 1,
        "proposal_id": PROPOSAL_ID,
        "action_id": "deactivate-workflow",
        "target_id": "wf1",
        "target_type": "workflow",
        "revision": revision,
        "created_at": "2026-08-30T00:00:00Z",
        "expires_at": "2026-08-30T00:15:00Z",
    }


def _swap_checked_path_before_open(monkeypatch: pytest.MonkeyPatch, target: Path, attacker: Path) -> list[str]:
    seen: list[str] = []
    real_open = os.open
    real_open_parent = grokbot_ops._open_parent_nofollow
    real_read_text = Path.read_text

    def swap() -> None:
        if seen:
            return
        seen.append(target.name)
        target.unlink()
        target.symlink_to(attacker)

    def open_child(path: str | int | bytes | os.PathLike[str], flags: int, *args: object, **kwargs: object) -> int:
        if kwargs.get("dir_fd") is not None and os.fspath(path) == target.name:
            swap()
        return real_open(path, flags, *args, **kwargs)

    def open_parent(path: Path, *, create: bool) -> int:
        if Path(path) == target:
            swap()
        return real_open_parent(path, create=create)

    def read_text(self: Path, *args: object, **kwargs: object) -> str:
        if self == target:
            swap()
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(os, "open", open_child)
    monkeypatch.setattr(grokbot_ops, "_open_parent_nofollow", open_parent)
    monkeypatch.setattr(Path, "read_text", read_text)
    return seen


def test_proposal_read_does_not_follow_swapped_symlink(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    tools, store, _clock, _fake = _tools(tmp_path)
    proposed = tools.call_tool("n8n_propose_action", {"action_id": "deactivate-workflow", "target_id": "wf1"})
    proposal_id = proposed["data"]["proposal_id"]
    target = Path(store._root) / "proposals" / f"{proposal_id}.json"
    attacker = tmp_path / "attacker-proposal.json"
    attacker.write_text(json.dumps(_attacker_proposal()), encoding="utf-8")
    os.chmod(attacker, 0o600)
    seen = _swap_checked_path_before_open(monkeypatch, target, attacker)
    with pytest.raises(N8nError) as caught:
        store.read_proposal(proposal_id)
    assert seen
    assert caught.value.code == "protocol_error"
    assert "n8n action state is invalid" in str(caught.value)
    assert str(attacker) not in str(caught.value)
    assert "ffffffffffffffff" not in str(caught.value)


def test_approval_read_does_not_follow_swapped_symlink(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    tools, store, _clock, fake = _tools(tmp_path)
    proposed = tools.call_tool("n8n_propose_action", {"action_id": "deactivate-workflow", "target_id": "wf1"})
    proposal = store.read_proposal(proposed["data"]["proposal_id"])
    assert proposal is not None
    _write_approval(store, proposal)
    target = Path(store.approval_dir) / f"{proposal['proposal_id']}.json"
    attacker = tmp_path / "attacker-approval.json"
    attacker.write_text(
        json.dumps(
            {
                "version": 1,
                "proposal_id": proposal["proposal_id"],
                "action_id": "deactivate-workflow",
                "target_id": "wf1",
                "target_type": "workflow",
                "revision": proposal["revision"],
                "approved_at": "2026-08-30T00:01:00Z",
            }
        ),
        encoding="utf-8",
    )
    os.chmod(attacker, 0o600)
    seen = _swap_checked_path_before_open(monkeypatch, target, attacker)
    with pytest.raises(N8nError) as caught:
        tools.call_tool("n8n_execute_action", {"proposal_id": proposal["proposal_id"]})
    assert seen
    assert caught.value.code == "denied"
    assert str(attacker) not in str(caught.value)
    assert fake.mutations == []


def test_exclusive_consume_write_stays_under_held_parent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    tools, store, _clock, fake = _tools(tmp_path)
    proposed = tools.call_tool("n8n_propose_action", {"action_id": "deactivate-workflow", "target_id": "wf1"})
    proposal = store.read_proposal(proposed["data"]["proposal_id"])
    assert proposal is not None
    _write_approval(store, proposal)
    consumed_dir = Path(store._root) / "consumed"
    outside = tmp_path / "outside-consumed"
    outside.mkdir()
    real_open = os.open
    swapped: list[Path] = []

    def open_and_swap(path: str | int | bytes | os.PathLike[str], flags: int, *args: object, **kwargs: object) -> int:
        dir_fd = kwargs.get("dir_fd")
        if dir_fd is None and isinstance(path, (str, os.PathLike)):
            candidate = Path(os.fspath(path))
            if candidate.parent == consumed_dir and candidate.suffix == ".json" and not swapped:
                swapped.append(candidate)
                consumed_dir.rename(tmp_path / "consumed-real")
                consumed_dir.symlink_to(outside)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", open_and_swap)
    result = tools.call_tool("n8n_execute_action", {"proposal_id": proposal["proposal_id"]})
    assert result["data"]["state"] == "consumed"
    assert fake.mutations == [("deactivate-workflow", "wf1")]
    assert list(outside.iterdir()) == []
    written = (
        (tmp_path / "consumed-real" / f"{proposal['proposal_id']}.json")
        if swapped
        else (consumed_dir / f"{proposal['proposal_id']}.json")
    )
    assert written.is_file()
    assert not written.is_symlink()


def test_atomic_result_write_uses_unpredictable_temp_and_dirfd_replace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    tools, store, _clock, _fake = _tools(tmp_path)
    proposed = tools.call_tool("n8n_propose_action", {"action_id": "deactivate-workflow", "target_id": "wf1"})
    proposal = store.read_proposal(proposed["data"]["proposal_id"])
    assert proposal is not None
    _write_approval(store, proposal)
    consumed = store.consume_approved_proposal(proposal["proposal_id"], current_revision=proposal["revision"])
    assert consumed["result"] == "pending_write"
    dest = Path(store._root) / "consumed" / f"{proposal['proposal_id']}.json"
    opened: list[str] = []
    replaced: list[tuple[object, object, object, object]] = []
    real_open = os.open
    real_replace = os.replace

    def track_open(path: str | int | bytes | os.PathLike[str], flags: int, *args: object, **kwargs: object) -> int:
        if isinstance(path, (str, os.PathLike)):
            opened.append(os.fspath(path))
        return real_open(path, flags, *args, **kwargs)

    def track_replace(src: object, dst: object, *args: object, **kwargs: object) -> None:
        replaced.append((src, dst, kwargs.get("src_dir_fd"), kwargs.get("dst_dir_fd")))
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "open", track_open)
    monkeypatch.setattr(os, "replace", track_replace)
    store.mark_consumed_result(proposal["proposal_id"], "succeeded")
    assert dest.read_text(encoding="utf-8")
    assert json.loads(dest.read_text(encoding="utf-8"))["result"] == "succeeded"
    assert str(dest) + ".tmp" not in opened
    assert replaced
    _src, _dst, src_dir_fd, dst_dir_fd = replaced[-1]
    assert src_dir_fd is not None
    assert dst_dir_fd is not None
    assert src_dir_fd == dst_dir_fd


def test_pending_write_status_is_unknown_until_marked_succeeded(tmp_path: Path):
    tools, store, _clock, _fake = _tools(tmp_path)
    proposed = tools.call_tool("n8n_propose_action", {"action_id": "deactivate-workflow", "target_id": "wf1"})
    proposal = store.read_proposal(proposed["data"]["proposal_id"])
    assert proposal is not None
    _write_approval(store, proposal)
    consumed = store.consume_approved_proposal(proposal["proposal_id"], current_revision=proposal["revision"])
    assert consumed["result"] == "pending_write"
    pending = store.public_status(proposal["proposal_id"])
    assert pending["state"] == "unknown"
    assert pending["proposal_id"] == proposal["proposal_id"]
    store.mark_consumed_result(proposal["proposal_id"], "succeeded")
    done = store.public_status(proposal["proposal_id"])
    assert done["state"] == "consumed"


def test_impossible_and_naive_timestamps_are_sanitized(tmp_path: Path):
    store, _clock = _dirs(tmp_path)
    proposal_path = Path(store._root) / "proposals" / f"{PROPOSAL_ID}.json"
    invalid = _attacker_proposal()
    invalid["created_at"] = "2026-02-30T00:00:00Z"
    invalid["expires_at"] = "2026-02-30T00:15:00Z"
    proposal_path.write_text(json.dumps(invalid, sort_keys=True), encoding="utf-8")
    os.chmod(proposal_path, 0o600)
    with pytest.raises(N8nError) as caught:
        store.read_proposal(PROPOSAL_ID)
    assert caught.value.code == "protocol_error"
    assert "ValueError" not in str(caught.value)
    assert "2026-02-30" not in str(caught.value)

    naive = {
        "version": 1,
        "proposal_id": PROPOSAL_ID,
        "action_id": "deactivate-workflow",
        "target_id": "wf1",
        "target_type": "workflow",
        "revision": "a" * 64,
        "approved_at": "2026-08-30T00:01:00",
    }
    approval_path = Path(store.approval_dir) / f"{PROPOSAL_ID}.json"
    approval_path.write_text(json.dumps(naive, sort_keys=True), encoding="utf-8")
    os.chmod(approval_path, 0o600)
    with pytest.raises(N8nError) as denied:
        store.read_approval(PROPOSAL_ID)
    assert denied.value.code == "denied"
    assert "ValueError" not in str(denied.value)
    assert "2026-08-30" not in str(denied.value)
    impossible_approval = dict(naive)
    impossible_approval["approved_at"] = "2026-02-30T00:01:00Z"
    approval_path.write_text(json.dumps(impossible_approval, sort_keys=True), encoding="utf-8")
    os.chmod(approval_path, 0o600)
    with pytest.raises(N8nError) as denied_date:
        store.read_approval(PROPOSAL_ID)
    assert denied_date.value.code == "denied"
    assert "ValueError" not in str(denied_date.value)
    assert "2026-02-30" not in str(denied_date.value)

    consumed = dict(invalid)
    consumed.update(
        {
            "approved_at": "2026-02-30T00:01:00Z",
            "consumed_at": "2026-02-30T00:02:00Z",
            "result": "pending_write",
        }
    )
    consumed_path = Path(store._root) / "consumed" / f"{PROPOSAL_ID}.json"
    consumed_path.write_text(json.dumps(consumed, sort_keys=True), encoding="utf-8")
    os.chmod(consumed_path, 0o600)
    with pytest.raises(N8nError) as consumed_error:
        store.read_consumed(PROPOSAL_ID)
    assert consumed_error.value.code == "protocol_error"
    assert "ValueError" not in str(consumed_error.value)


def _write_proposal_file(directory: Path, proposal_id: str, *, expired: bool = False) -> None:
    record = _attacker_proposal()
    record["proposal_id"] = proposal_id
    if expired:
        record["created_at"] = "2026-08-29T23:00:00Z"
        record["expires_at"] = "2026-08-29T23:15:00Z"
    path = directory / f"{proposal_id}.json"
    path.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
    os.chmod(path, 0o600)


def test_create_rejects_action_root_swap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    store, _clock = _dirs(tmp_path)
    root = Path(store._root)
    proposals = root / "proposals"
    for child in proposals.iterdir():
        child.unlink()
    proposals.rmdir()
    outside = tmp_path / "outside-actions"
    outside.mkdir(mode=0o700)
    os.chmod(outside, 0o700)
    (outside / "proposals").mkdir(mode=0o700)
    (outside / "consumed").mkdir(mode=0o700)
    os.chmod(outside / "proposals", 0o700)
    os.chmod(outside / "consumed", 0o700)
    real_mkdir = os.mkdir
    swapped: list[str] = []

    def mkdir_on_held_fd(path: str | bytes | os.PathLike[str], mode: int = 0o777, **kwargs: object) -> None:
        if kwargs.get("dir_fd") is None:
            raise AssertionError("path-based mkdir")
        if not swapped:
            swapped.append("root")
            root.rename(tmp_path / "actions-real")
            root.symlink_to(outside)
        return real_mkdir(path, mode, **kwargs)

    monkeypatch.setattr(os, "mkdir", mkdir_on_held_fd)
    with pytest.raises(N8nError) as caught:
        store.create_proposal({"action_id": "deactivate-workflow", "target_id": "wf1"}, revision="a" * 64)
    assert swapped
    assert caught.value.code == "protocol_error"
    assert "n8n action state is invalid" in str(caught.value)
    assert list((outside / "proposals").iterdir()) == []
    recreated = tmp_path / "actions-real" / "proposals"
    if recreated.exists():
        assert list(recreated.iterdir()) == []


def test_list_and_prune_stay_on_held_proposals_fd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    store, clock = _dirs(tmp_path)
    proposals = Path(store._root) / "proposals"
    _write_proposal_file(proposals, "expiredproposalaaaaaaaaaaaaaaaaaa", expired=True)
    attacker = tmp_path / "attacker-proposals"
    attacker.mkdir(mode=0o700)
    os.chmod(attacker, 0o700)
    _write_proposal_file(attacker, "attackerproposalaaaaaaaaaaaaaaaaaa")
    real_listdir = os.listdir
    real_unlink = os.unlink
    path_unlinks: list[str] = []

    def refuse_path_listdir(path: str | bytes | os.PathLike[str] | int, *args: object, **kwargs: object):
        if not isinstance(path, int) and Path(os.fspath(path)) == proposals:
            raise AssertionError("path-based proposals listdir")
        return real_listdir(path, *args, **kwargs)

    def track_unlink(path: str | bytes | os.PathLike[str], *args: object, **kwargs: object) -> None:
        if kwargs.get("dir_fd") is None:
            path_unlinks.append(os.fspath(path))
            raise AssertionError("path-based unlink")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "listdir", refuse_path_listdir)
    monkeypatch.setattr(os, "unlink", track_unlink)
    monkeypatch.setattr(Path, "unlink", lambda self: (_ for _ in ()).throw(AssertionError("Path.unlink")))
    clock["value"] = NOW
    store.create_proposal({"action_id": "deactivate-workflow", "target_id": "wf1"}, revision="a" * 64)
    assert path_unlinks == []
    assert (attacker / "attackerproposalaaaaaaaaaaaaaaaaaa.json").is_file()
    assert not (proposals / "expiredproposalaaaaaaaaaaaaaaaaaa.json").exists()


def test_proposal_enumeration_stops_at_cap_plus_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    store, _clock = _dirs(tmp_path)
    scanned = {"count": 0}

    class _Entry:
        def __init__(self, index: int):
            self.name = f"p{index:02d}{'a' * 30}.json"

        def is_symlink(self) -> bool:
            return False

        def stat(self, *, follow_symlinks: bool = True) -> os.stat_result:
            del follow_symlinks
            return os.stat(tmp_path)

    class _Scan:
        def __enter__(self) -> "_Scan":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def close(self) -> None:
            return None

        def __iter__(self):
            for index in range(MAX_PROPOSALS + 8):
                scanned["count"] += 1
                if scanned["count"] > MAX_PROPOSALS + 1:
                    raise AssertionError("enumerated past cap+1")
                yield _Entry(index)

    monkeypatch.setattr(os, "scandir", lambda _path: _Scan())
    with pytest.raises(N8nError) as caught:
        store.create_proposal({"action_id": "deactivate-workflow", "target_id": "wf1"}, revision="a" * 64)
    assert scanned["count"] == MAX_PROPOSALS + 1
    assert caught.value.code == "protocol_error"
    assert str(tmp_path) not in str(caught.value)


def test_consumed_enumeration_is_bounded(tmp_path: Path):
    store, _clock = _dirs(tmp_path)
    consumed = Path(store._root) / "consumed"
    for index in range(MAX_CONSUMED + 2):
        path = consumed / f"c{index:03d}{'a' * 28}.json"
        path.write_text("{}", encoding="utf-8")
        os.chmod(path, 0o600)
    with pytest.raises(N8nError) as caught:
        store.create_proposal({"action_id": "deactivate-workflow", "target_id": "wf1"}, revision="a" * 64)
    assert caught.value.code == "protocol_error"
    assert str(consumed) not in str(caught.value)


def test_public_status_requires_full_approval_binding(tmp_path: Path):
    tools, store, _clock, _fake = _tools(tmp_path)
    proposed = tools.call_tool("n8n_propose_action", {"action_id": "deactivate-workflow", "target_id": "wf1"})
    proposal = store.read_proposal(proposed["data"]["proposal_id"])
    assert proposal is not None
    mismatches = (
        {"action_id": "archive-workflow"},
        {"target_id": "wf99"},
        {"target_type": "execution"},
        {"proposal_id": "b" * 32},
        {"revision": "0" * 64},
    )
    for override in mismatches:
        _write_approval(store, proposal, **override)
        status = store.public_status(proposal["proposal_id"])
        assert status["state"] == "pending", override
        assert status["proposal_id"] == proposal["proposal_id"]
    _write_approval(store, proposal)
    approved = store.public_status(proposal["proposal_id"])
    assert approved["state"] == "approved"


def test_windows_action_store_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    actions = tmp_path / "actions"
    approvals = tmp_path / "approvals"
    actions.mkdir(mode=0o700)
    approvals.mkdir(mode=0o700)
    os.chmod(actions, 0o700)
    os.chmod(approvals, 0o700)
    monkeypatch.setattr("brigade.grokbot_n8n.runtime_config.permission_policy", lambda: "unsupported")
    store = N8nActionStore(action_state_path=str(actions), approval_dir=str(approvals), now=lambda: NOW)
    with pytest.raises(N8nError) as caught:
        store.ready()
    assert caught.value.code == "invalid_request"
    assert "n8n environment is invalid" in str(caught.value)
    assert str(actions) not in str(caught.value)


def _replace_dir_with_fresh_0700(path: Path) -> None:
    path.rename(path.with_name(f"{path.name}-prior"))
    path.mkdir(mode=0o700)
    os.chmod(path, 0o700)


def test_replacing_consumed_dir_after_ready_cannot_permit_replay(tmp_path: Path):
    tools, store, _clock, fake = _tools(tmp_path)
    proposed = tools.call_tool("n8n_propose_action", {"action_id": "deactivate-workflow", "target_id": "wf1"})
    proposal = store.read_proposal(proposed["data"]["proposal_id"])
    assert proposal is not None
    _write_approval(store, proposal)
    first = tools.call_tool("n8n_execute_action", {"proposal_id": proposal["proposal_id"]})
    assert first["data"]["state"] == "consumed"
    assert fake.mutations == [("deactivate-workflow", "wf1")]
    _replace_dir_with_fresh_0700(Path(store._root) / "consumed")
    with pytest.raises(N8nError) as caught:
        tools.call_tool("n8n_execute_action", {"proposal_id": proposal["proposal_id"]})
    assert caught.value.code == "protocol_error"
    assert "n8n action state is invalid" in str(caught.value)
    assert str(store._root) not in str(caught.value)
    assert fake.mutations == [("deactivate-workflow", "wf1")]


def test_replaced_proposals_dir_after_ready_fails_closed(tmp_path: Path):
    store, _clock = _dirs(tmp_path)
    _replace_dir_with_fresh_0700(Path(store._root) / "proposals")
    with pytest.raises(N8nError) as caught:
        store.create_proposal({"action_id": "deactivate-workflow", "target_id": "wf1"}, revision="a" * 64)
    assert caught.value.code == "protocol_error"
    assert "n8n action state is invalid" in str(caught.value)
    assert str(store._root) not in str(caught.value)


def test_replaced_approvals_dir_after_ready_fails_closed(tmp_path: Path):
    store, _clock = _dirs(tmp_path)
    _replace_dir_with_fresh_0700(Path(store.approval_dir))
    with pytest.raises(N8nError) as caught:
        store.read_approval(PROPOSAL_ID)
    assert caught.value.code == "protocol_error"
    assert "n8n action state is invalid" in str(caught.value)
    assert str(store.approval_dir) not in str(caught.value)


def _replace_regular_file_between_stat_and_open(
    monkeypatch: pytest.MonkeyPatch, target: Path, replacement: Path
) -> list[str]:
    seen: list[str] = []
    real_stat = os.stat
    real_open = os.open

    def is_target(path: object, dir_fd: object) -> bool:
        if dir_fd is None or isinstance(path, int) or not isinstance(dir_fd, int):
            return False
        if os.fspath(path) != target.name:
            return False
        parent = real_stat(target.parent, follow_symlinks=False)
        held = os.fstat(dir_fd)
        return (parent.st_dev, parent.st_ino) == (held.st_dev, held.st_ino)

    def stat_child(path: str | bytes | os.PathLike[str] | int, *args: object, **kwargs: object) -> os.stat_result:
        info = real_stat(path, *args, **kwargs)
        if is_target(path, kwargs.get("dir_fd")) and kwargs.get("follow_symlinks") is False:
            seen.append("stat")
        return info

    def open_child(path: str | int | bytes | os.PathLike[str], flags: int, *args: object, **kwargs: object) -> int:
        if is_target(path, kwargs.get("dir_fd")) and "stat" in seen and "replaced" not in seen:
            seen.append("replaced")
            os.replace(replacement, target)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "stat", stat_child)
    monkeypatch.setattr(os, "open", open_child)
    return seen


def test_proposal_replacement_between_stat_and_open_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    tools, store, _clock, _fake = _tools(tmp_path)
    proposed = tools.call_tool("n8n_propose_action", {"action_id": "deactivate-workflow", "target_id": "wf1"})
    proposal_id = proposed["data"]["proposal_id"]
    target = Path(store._root) / "proposals" / f"{proposal_id}.json"
    attacker = tmp_path / "replaced-proposal.json"
    attacker.write_text(json.dumps(_attacker_proposal()), encoding="utf-8")
    os.chmod(attacker, 0o600)
    seen = _replace_regular_file_between_stat_and_open(monkeypatch, target, attacker)
    with pytest.raises(N8nError) as caught:
        store.read_proposal(proposal_id)
    assert seen == ["stat", "replaced"]
    assert caught.value.code == "protocol_error"
    assert "n8n action state is invalid" in str(caught.value)
    assert str(attacker) not in str(caught.value)
    assert "ffffffffffffffff" not in str(caught.value)


def test_approval_replacement_between_stat_and_open_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    tools, store, _clock, fake = _tools(tmp_path)
    proposed = tools.call_tool("n8n_propose_action", {"action_id": "deactivate-workflow", "target_id": "wf1"})
    proposal = store.read_proposal(proposed["data"]["proposal_id"])
    assert proposal is not None
    _write_approval(store, proposal)
    target = Path(store.approval_dir) / f"{proposal['proposal_id']}.json"
    attacker = tmp_path / "replaced-approval.json"
    attacker.write_text(
        json.dumps(
            {
                "version": 1,
                "proposal_id": proposal["proposal_id"],
                "action_id": "deactivate-workflow",
                "target_id": "wf1",
                "target_type": "workflow",
                "revision": proposal["revision"],
                "approved_at": "2026-08-30T00:09:00Z",
            }
        ),
        encoding="utf-8",
    )
    os.chmod(attacker, 0o600)
    seen = _replace_regular_file_between_stat_and_open(monkeypatch, target, attacker)
    with pytest.raises(N8nError) as caught:
        tools.call_tool("n8n_execute_action", {"proposal_id": proposal["proposal_id"]})
    assert seen == ["stat", "replaced"]
    assert caught.value.code == "denied"
    assert str(attacker) not in str(caught.value)
    assert "2026-08-30T00:09:00Z" not in str(caught.value)
    assert fake.mutations == []


def test_consumed_replacement_between_stat_and_open_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    store, _clock = _dirs(tmp_path)
    proposal = store.create_proposal({"action_id": "deactivate-workflow", "target_id": "wf1"}, revision="a" * 64)
    _write_approval(store, proposal)
    consumed = store.consume_approved_proposal(proposal["proposal_id"], current_revision=proposal["revision"])
    assert consumed["result"] == "pending_write"
    target = Path(store._root) / "consumed" / f"{proposal['proposal_id']}.json"
    attacker = tmp_path / "replaced-consumed.json"
    replacement = dict(consumed)
    replacement["result"] = "succeeded"
    attacker.write_text(json.dumps(replacement, sort_keys=True), encoding="utf-8")
    os.chmod(attacker, 0o600)
    seen = _replace_regular_file_between_stat_and_open(monkeypatch, target, attacker)
    with pytest.raises(N8nError) as caught:
        store.read_consumed(proposal["proposal_id"])
    assert seen == ["stat", "replaced"]
    assert caught.value.code == "protocol_error"
    assert "n8n action state is invalid" in str(caught.value)
    assert str(attacker) not in str(caught.value)
    assert "succeeded" not in str(caught.value)
