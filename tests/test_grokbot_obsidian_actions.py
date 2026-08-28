"""Proposal store, approval binding, and execute gates."""

from __future__ import annotations

import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from brigade.grokbot_obsidian.catalog import hash_action_content
from brigade.grokbot_obsidian.contracts import ERROR_MESSAGES, ObsidianError
from brigade.grokbot_obsidian import store as store_mod
from brigade.grokbot_obsidian.store import ObsidianActionStore, write_approval_file
from brigade.grokbot_obsidian.tools import ObsidianTools


ACTION_ID = "a" * 32
RECEIPT = "b" * 32
NONCE = "c" * 32
REQUEST = "req-12345"
CREATE = {
    "kind": "create_note",
    "path": "00 - Inbox/Agent Notes/demo.md",
    "type": "agent-note",
    "para": "inbox",
    "agent": "cerebro-curator",
    "tags": ["agent-note"],
    "body": "Hello.\n",
}
POLICY = {
    "dailyNotesFolder": "",
    "sensitivePathPrefixes": (),
    "sensitiveTags": (),
    "dashboardRoot": "01 - Projects/Dashboard.base",
    "excalidrawSuffix": ".excalidraw.md",
    "tags": (),
}


class MemoryVault:
    def __init__(self) -> None:
        self.files: dict[str, dict[str, object]] = {}

    def search(self, query: str, limit: int):
        return []

    def read(self, path: str):
        got = self.files.get(path)
        if got is None:
            return None
        return {"bytes": got["bytes"], "etag": got["etag"]}

    def write_new(self, path: str, data: bytes) -> None:
        if path in self.files:
            raise ObsidianError("conflict", ERROR_MESSAGES["conflict"])
        self.files[path] = {"bytes": data, "etag": "aaaaaa"}

    def command_list(self):
        return []

    def tags(self, path: str):
        return []


class FakeExecutor:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def _ack(self, action):
        self.calls.append(action["kind"])
        return {"outcome": "verified"}

    def create_note(self, action):
        return self._ack(action)

    def patch_note(self, action):
        return self._ack(action)

    def copy_note(self, action):
        return self._ack(action)

    def move_note(self, action):
        return self._ack(action)

    def trash_note(self, action):
        return self._ack(action)

    def append_flashcard(self, action):
        return self._ack(action)

    def create_canvas(self, action):
        return self._ack(action)

    def create_base(self, action):
        return self._ack(action)

    def apply_template(self, action, _context):
        return self._ack(action)

    def create_excalidraw(self, action, _context=None):
        return self._ack(action)

    def patch_canvas(self, action):
        return self._ack(action)

    def patch_base(self, action):
        return self._ack(action)

    def update_excalidraw(self, action, _context=None):
        return self._ack(action)


class CreateOnlyExecutor:
    def create_note(self, action):
        return {"outcome": "verified"}


def _ids(values: list[str]):
    for value in values:
        yield value
    while True:
        yield values[-1]


class FrozenClock:
    def __init__(self, stamp: datetime):
        self.stamp = stamp

    def __call__(self) -> datetime:
        return self.stamp


def _store(tmp_path: Path, clock=None) -> ObsidianActionStore:
    actions = tmp_path / "actions"
    approvals = tmp_path / "approvals"
    actions.mkdir(mode=0o700)
    approvals.mkdir(mode=0o700)
    os.chmod(actions, 0o700)
    os.chmod(approvals, 0o700)
    store = ObsidianActionStore(str(actions), str(approvals), clock=clock)
    store.ready()
    return store


def _proposal(now: datetime, **overrides):
    created = now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    expires = (now + timedelta(minutes=30)).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    payload = {"request_id": REQUEST, "action": CREATE}
    record = {
        "version": 1,
        "action_id": ACTION_ID,
        "request_id": REQUEST,
        "content_hash": hash_action_content(payload),
        "kind": "create_note",
        "action": CREATE,
        "target_path": CREATE["path"],
        "target_version": "absent",
        "blast_radius": "one new markdown file",
        "nonce": NONCE,
        "created_at": created,
        "expires_at": expires,
    }
    record.update(overrides)
    return record


def test_proposal_is_single_use_and_replay_is_denied(tmp_path: Path):
    now = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
    store = _store(tmp_path, clock=lambda: now)
    record = _proposal(now)
    store.create_proposal(record)
    with pytest.raises(ObsidianError) as caught:
        store.create_proposal(record)
    assert caught.value.code == "denied"
    approval = {key: record[key] for key in record if key != "created_at"}
    approval["approved_at"] = record["created_at"]
    approval["approval_receipt"] = RECEIPT
    write_approval_file(store.approval_dir.as_posix(), approval)
    claimed = store.claim_consumed({**approval, "consumed_at": record["created_at"]})
    assert claimed["action_id"] == ACTION_ID
    with pytest.raises(ObsidianError) as replay:
        store.claim_consumed({**approval, "consumed_at": record["created_at"]})
    assert replay.value.outcome == "replayed"  # type: ignore[attr-defined]


def test_execute_requires_out_of_band_approval_and_denies_cross_bind(tmp_path: Path):
    now = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
    store = _store(tmp_path, clock=lambda: now)
    native = MemoryVault()
    tools = ObsidianTools(
        native=native,
        policy=POLICY,
        store=store,
        reconcile=lambda: {
            "status": "ok",
            "capabilities": {
                "phase": "phase1",
                "search_backend": "native_bounded_search",
                "plugins": [],
                "supported_action_ids": [],
            },
        },
        executor=FakeExecutor(),
        clock=lambda: now,
        random_id=_ids([ACTION_ID, NONCE, RECEIPT, "e" * 32, "f" * 32, "1" * 32, "2" * 32]).__next__,
    )
    proposed = tools.propose_action({"request_id": REQUEST, "action": CREATE})
    assert proposed["action_id"] == ACTION_ID
    with pytest.raises(ObsidianError) as missing:
        tools.execute_action({"action_id": ACTION_ID, "approval_receipt": RECEIPT})
    assert missing.value.code == "denied"
    proposal = store.read_proposal(ACTION_ID)
    assert proposal is not None
    approval = {key: proposal[key] for key in proposal if key != "created_at"}
    approval["approved_at"] = proposal["created_at"]
    approval["approval_receipt"] = RECEIPT
    write_approval_file(store.approval_dir.as_posix(), approval)
    with pytest.raises(ObsidianError) as cross:
        tools.execute_action({"action_id": ACTION_ID, "approval_receipt": "d" * 32})
    assert cross.value.code == "denied"
    result = tools.execute_action({"action_id": ACTION_ID, "approval_receipt": RECEIPT})
    assert result["outcome"] == "verified"
    with pytest.raises(ObsidianError) as replay:
        tools.execute_action({"action_id": ACTION_ID, "approval_receipt": RECEIPT})
    assert replay.value.code == "denied"


def test_expired_proposal_is_rejected_without_mutation(tmp_path: Path):
    created = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
    later = created + timedelta(minutes=31)
    store = _store(tmp_path, clock=lambda: later)
    store.create_proposal(_proposal(created))
    native = MemoryVault()
    tools = ObsidianTools(
        native=native,
        policy=POLICY,
        store=store,
        executor=FakeExecutor(),
        clock=lambda: later,
    )
    with pytest.raises(ObsidianError):
        tools.execute_action({"action_id": ACTION_ID, "approval_receipt": RECEIPT})
    assert native.files == {}


def test_one_mutation_and_four_reads_are_gated(tmp_path: Path):
    store = _store(tmp_path)
    native = MemoryVault()
    tools = ObsidianTools(
        native=native,
        policy=POLICY,
        store=store,
        reconcile=lambda: {
            "status": "ok",
            "capabilities": {
                "phase": "phase1",
                "search_backend": "native_bounded_search",
                "plugins": [],
                "supported_action_ids": [],
            },
        },
    )
    assert tools.capabilities({})["phase"] == "phase1"
    assert tools.search({"query": "alpha"})["backend"] == "native_bounded_search"
    assert tools._reads._limit == 4
    assert tools._mutations._limit == 1


def _approval_from(record: dict) -> dict:
    approval = {key: record[key] for key in record if key != "created_at"}
    approval["approved_at"] = record["created_at"]
    approval["approval_receipt"] = RECEIPT
    return approval


def test_active_proposal_capacity_is_bounded_before_create(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(store_mod, "MAX_ACTIVE_PROPOSALS", 2)
    now = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
    store = _store(tmp_path, clock=lambda: now)
    first = _proposal(now, action_id="1" * 32, request_id="req-00001", nonce="d" * 32)
    second = _proposal(now, action_id="2" * 32, request_id="req-00002", nonce="e" * 32)
    first["content_hash"] = hash_action_content({"request_id": first["request_id"], "action": CREATE})
    second["content_hash"] = hash_action_content({"request_id": second["request_id"], "action": CREATE})
    store.create_proposal(first)
    store.create_proposal(second)
    third = _proposal(now, action_id="3" * 32, request_id="req-00003", nonce="f" * 32)
    third["content_hash"] = hash_action_content({"request_id": third["request_id"], "action": CREATE})
    with pytest.raises(ObsidianError) as caught:
        store.create_proposal(third)
    assert caught.value.code == "denied"
    assert store.read_proposal("3" * 32) is None
    assert store.read_proposal("1" * 32) is not None


def test_expired_proposals_and_associated_state_are_pruned(tmp_path: Path):
    created = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
    clock = FrozenClock(created)
    store = _store(tmp_path, clock=clock)
    record = _proposal(created)
    store.create_proposal(record)
    approval = _approval_from(record)
    write_approval_file(store.approval_dir.as_posix(), approval)
    store.claim_consumed({**approval, "consumed_at": record["created_at"]})
    store.write_receipt(
        {
            "version": 1,
            "receipt_id": "d" * 32,
            "kind": "execution",
            "action_id": ACTION_ID,
            "outcome": "verified",
            "created_at": record["created_at"],
        }
    )
    clock.stamp = created + timedelta(minutes=31)
    store.ready()
    assert store.read_proposal(ACTION_ID) is None
    assert not (store.action_state_path / "consumed" / f"{ACTION_ID}.json").exists()
    assert not (store.action_state_path / "receipts" / f"{'d' * 32}.json").exists()
    replacement = _proposal(clock.stamp, request_id="req-99999")
    replacement["content_hash"] = hash_action_content({"request_id": "req-99999", "action": CREATE})
    store.create_proposal(replacement)
    assert store.read_proposal(ACTION_ID) is not None


def test_unexpired_proposals_survive_prune_and_replay_stays_denied(tmp_path: Path):
    created = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
    clock = FrozenClock(created + timedelta(minutes=29))
    store = _store(tmp_path, clock=clock)
    record = _proposal(created)
    store.create_proposal(record)
    approval = _approval_from(record)
    write_approval_file(store.approval_dir.as_posix(), approval)
    store.claim_consumed({**approval, "consumed_at": record["created_at"]})
    store.ready()
    assert store.read_proposal(ACTION_ID) is not None
    with pytest.raises(ObsidianError) as replay:
        store.claim_consumed({**approval, "consumed_at": record["created_at"]})
    assert replay.value.outcome == "replayed"  # type: ignore[attr-defined]


def test_concurrent_claims_are_single_use(tmp_path: Path):
    now = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
    store = _store(tmp_path, clock=lambda: now)
    record = _proposal(now)
    store.create_proposal(record)
    approval = _approval_from(record)
    write_approval_file(store.approval_dir.as_posix(), approval)
    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        try:
            store.claim_consumed({**approval, "consumed_at": record["created_at"]})
            label = "ok"
        except ObsidianError as exc:
            label = getattr(exc, "outcome", exc.code)
        with lock:
            outcomes.append(label)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert outcomes.count("ok") == 1
    assert outcomes.count("replayed") == 1
    assert (store.action_state_path / "consumed" / f"{ACTION_ID}.json").is_file()


def test_invoke_executor_accesses_only_the_selected_method(tmp_path: Path):
    tools = ObsidianTools(
        native=MemoryVault(),
        policy=POLICY,
        store=_store(tmp_path),
        executor=CreateOnlyExecutor(),
    )
    result = tools._invoke_executor(CREATE, {"target_version": "absent"})
    assert result == {"outcome": "verified"}


def test_tools_filter_and_deny_allowed_paths_with_sensitive_tags(tmp_path: Path):
    from brigade.grokbot_obsidian.adapters import NativeMcpPort

    allowed = "01 - Projects/demo.md"
    tagged = "01 - Projects/secret.md"
    policy = {**POLICY, "sensitiveTags": ("private",)}

    class TagAwareClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, object]] = []
            self.files = {
                allowed: {"content": "public note", "tags": [], "etag": "aaaaaa"},
                tagged: {"content": "secret note", "tags": ["private"], "etag": "bbbbbb"},
            }

        def call_tool(self, name: str, arguments=None):
            payload = arguments or {}
            self.calls.append((name, payload))
            if name == "search_simple":
                return [
                    {
                        "filename": path,
                        "score": 1.0,
                        "matches": [
                            {
                                "match": {"start": 0, "end": 4, "source": "content"},
                                "context": "note",
                            }
                        ],
                    }
                    for path in (allowed, tagged)
                ]
            if name == "vault_read" and payload.get("path") in self.files and set(payload) == {"path"}:
                return self.files[payload["path"]]
            if name == "vault_get_document_map":
                return {"version": "aaaaaa"}
            if name in {"vault_patch", "vault_move", "vault_delete"}:
                return {"ok": True}
            raise AssertionError(name)

        def close(self) -> None:
            return None

    client = TagAwareClient()
    tools = ObsidianTools(
        native=NativeMcpPort(client, policy=policy),
        policy=policy,
        store=_store(tmp_path),
        executor=FakeExecutor(),
    )
    found = tools.search({"query": "note", "limit": 8})
    assert [hit["path"] for hit in found["results"]] == [allowed]
    assert tools.read({"path": allowed})["text"] == "public note"
    with pytest.raises(ObsidianError) as read_denied:
        tools.read({"path": tagged})
    assert read_denied.value.code == "denied"
    for action in (
        {
            "kind": "patch_note",
            "path": tagged,
            "ifMatch": "bbbbbb",
            "patch": {"target": "heading", "heading": ["Intro"], "body": "x\n"},
        },
        {"kind": "move_note", "from": tagged, "to": "04 - Archive/secret.md"},
        {"kind": "trash_note", "path": tagged},
    ):
        with pytest.raises(ObsidianError) as proposed:
            tools.propose_action({"request_id": f"req-{action['kind']}-xx", "action": action})
        assert proposed.value.code == "denied"
    assert "vault_patch" not in {name for name, _ in client.calls}
    assert "vault_move" not in {name for name, _ in client.calls}
    assert "vault_delete" not in {name for name, _ in client.calls}
