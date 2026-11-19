"""The persisted dispatch exclusion index must match the full receipt scan.

The index caches a fold over the local intent receipts, so every test compares
it against an independent full scan of the same receipts. The scan helpers
below are a literal copy of the pre-index implementation; they are the oracle,
not a second copy of the new code.
"""

from __future__ import annotations

import hashlib
import json
import random
from pathlib import Path
from uuid import uuid4

import pytest

from brigade import localio, work_cmd, worker_failure
from brigade.work_cmd import dispatch_index, dispatch_state as store
from tests.support import PRIVATE_FILE_MODE, assert_private_mode

SESSION = "dispatch-0123456789abcdef"
FAIL_KIND = {"failure_kind": "worker-exhausted", "infrastructure": False}
POLICY_KIND = {"failure_kind": "budget-exhausted", "infrastructure": False}
INFRA_KIND = {"failure_kind": "spawn-error", "infrastructure": True}
TERMINALS = [FAIL_KIND, POLICY_KIND, INFRA_KIND, None]
STATES = ["planned", "claimed", "running", "unknown", "completed", "failed", "canceled", "orphaned"]


def _resets(target: Path) -> dict[str, str]:
    resets: dict[str, str] = {}
    for path in (store.root(target) / "resets").glob("*.json"):
        row = store.read(path)
        task_id = row["task_id"]
        resets[task_id] = max(resets.get(task_id, ""), row["created_at"])
    return resets


def _reference_counts(target: Path) -> dict[str, int]:
    resets = _resets(target)
    counts: dict[str, int] = {}
    for _, row in store.intents(target):
        task_id = row["task_id"]
        if row["created_at"] <= resets.get(task_id, ""):
            continue
        if row["state"] == "completed":
            counts[task_id] = 0
        elif row["state"] == "failed":
            terminal = row.get("terminal") or {}
            if (
                terminal.get("infrastructure") is not True
                and terminal.get("failure_kind") not in worker_failure.RUN_POLICY_TERMINAL_FAILURE_KINDS
            ):
                counts[task_id] = counts.get(task_id, 0) + 1
    return counts


def _reference_exclusions(target: Path) -> dict[str, str]:
    from brigade.work_cmd import claiming
    from brigade.work_cmd.ledger import locking

    result = {task_id: "circuit-breaker" for task_id, count in _reference_counts(target).items() if count >= 3}
    tasks = {task["id"]: task for task in locking._read_task_ledger(target)["tasks"]}
    resets = _resets(target)
    for _, row in store.intents(target):
        task_id = row["task_id"]
        if row["state"] in store.ACTIVE or row["state"] == "unknown":
            result[task_id] = "active-intent"
        elif row["state"] == "completed" and row["created_at"] > resets.get(task_id, ""):
            task = tasks.get(task_id, {})
            claim = claiming.claim_record(task)
            revision = row.get("claimed_item_revision", row["task_item_revision"] + 1)
            if (
                task.get("status") == "in_progress"
                and claim is not None
                and claim["claim_id"] == row["claim_id"]
                and task.get("item_revision") == revision
            ):
                result[task_id] = "completed-unlanded"
    return result


def _assert_matches_full_scan(target: Path) -> None:
    assert store.failure_counts(target) == _reference_counts(target)
    assert store.exclusions(target) == _reference_exclusions(target)


def _task(task_id: str, *, status: str = "in_progress", item_revision: int = 2, claim_id: str | None = None) -> dict:
    task = {
        "id": task_id,
        "text": f"Change {task_id}",
        "status": status,
        "created_at": "2026-01-01T00:00:00Z",
        "item_revision": item_revision,
    }
    if claim_id is not None:
        task["claim"] = {"claim_id": claim_id, "actor": "coder", "claimed_at": "2026-01-01T00:00:00+00:00"}
    return task


def _write_ledger(target: Path, tasks: list[dict]) -> None:
    work_cmd._write_task_ledger(target, {"version": 2, "tasks": tasks, "edges": []})


def _stamp(index: int) -> str:
    return f"2026-01-01T00:00:00.{index:06d}+00:00"


def _receipt(
    target: Path,
    *,
    task_id: str,
    stamp: str,
    state: str,
    attempt: int = 1,
    terminal: dict | None = None,
    item_revision: int = 2,
    claimed_revision: int | None = None,
    session: str = SESSION,
) -> tuple[Path, dict]:
    intent_id = hashlib.sha256(f"{session}|{task_id}|{stamp}|{attempt}".encode()).hexdigest()[:16]
    row = {
        "intent_id": intent_id,
        "task_id": task_id,
        "task_item_revision": item_revision,
        "attempt_no": attempt,
        "claim_id": "dispatch-" + intent_id,
        "state": state,
        "state_seq": 1,
        "created_at": stamp,
        "updated_at": stamp,
        "terminal": terminal,
    }
    if claimed_revision is not None:
        row["claimed_item_revision"] = claimed_revision
    path = store.root(target) / session / "intents" / f"{intent_id}.json"
    localio.write_json(path, row)
    return path, row


def _rewrite(path: Path, row: dict, state: str, terminal: dict | None = None) -> dict:
    updated = {**row, "state": state, "terminal": terminal, "state_seq": row["state_seq"] + 1}
    localio.write_json(path, updated)
    return updated


def _reset(target: Path, task_id: str, created_at: str) -> None:
    localio.write_json(
        store.root(target) / "resets" / f"{uuid4().hex}.json",
        {"schema": "brigade.work_dispatch_reset.v1", "task_id": task_id, "reason": "test", "created_at": created_at},
    )


@pytest.fixture
def target(tmp_path: Path) -> Path:
    _write_ledger(tmp_path, [_task("task-0", status="pending"), _task("task-1", status="pending")])
    return tmp_path


def test_index_matches_full_scan_after_every_random_append(target, monkeypatch):
    tasks = {task["id"]: task for task in work_cmd._read_task_ledger(target)["tasks"]}
    tasks["task-2"] = _task("task-2")
    _write_ledger(target, [tasks[key] for key in sorted(tasks)])
    rebuilds: list[int] = []
    rebuild = dispatch_index._rebuild

    def counting_rebuild(*args, **kwargs):
        rebuilds.append(len(rebuilds))
        return rebuild(*args, **kwargs)

    monkeypatch.setattr(dispatch_index, "_rebuild", counting_rebuild)
    rng = random.Random(1946)
    attempts: dict[str, int] = {}
    for step in range(60):
        task_id = rng.choice(sorted(tasks))
        task = tasks[task_id]
        state = rng.choice(STATES)
        stamp = _stamp(step)
        attempt = attempts.get(task_id, 0) + 1
        attempts[task_id] = attempt
        if state == "completed":
            # Half the completions match the live claim and half do not, so both
            # arms of the completed-unlanded rule are exercised.
            if rng.random() < 0.5:
                _, row = _receipt(target, task_id=task_id, stamp=stamp, state=state, attempt=attempt)
                task["claim"] = {"claim_id": row["claim_id"], "actor": "coder", "claimed_at": stamp}
                task["item_revision"] = 3
            else:
                _receipt(target, task_id=task_id, stamp=stamp, state=state, attempt=attempt, item_revision=7)
        elif state == "failed":
            _receipt(target, task_id=task_id, stamp=stamp, state=state, attempt=attempt, terminal=rng.choice(TERMINALS))
        elif state in store.ACTIVE or state == "unknown":
            _, row = _receipt(target, task_id=task_id, stamp=stamp, state=state, attempt=attempt)
            task["claim"] = {"claim_id": "dispatch-live-" + task_id, "actor": "coder", "claimed_at": stamp}
        else:
            _receipt(target, task_id=task_id, stamp=stamp, state=state, attempt=attempt)
        if rng.random() < 0.12:
            _reset(target, task_id, stamp)
        _write_ledger(target, [tasks[key] for key in sorted(tasks)])
        _assert_matches_full_scan(target)
    # Resets invalidate the fold; ordinary appends and rewrites must not.
    assert 0 < len(rebuilds) < 10
    index = json.loads(dispatch_index.state_path(target).read_text())
    assert index["journal"]["offset"] == len(store.intents(target))


def test_index_rebuilds_when_the_journal_gets_shorter(target):
    for step in range(5):
        _receipt(target, task_id="task-0", stamp=_stamp(step), state="failed", attempt=step + 1, terminal=FAIL_KIND)
    assert store.exclusions(target) == {"task-0": "circuit-breaker"}
    assert json.loads(dispatch_index.state_path(target).read_text())["journal"]["offset"] == 5

    for path in sorted((store.root(target) / SESSION / "intents").glob("*.json"))[:2]:
        path.unlink()

    _assert_matches_full_scan(target)
    assert json.loads(dispatch_index.state_path(target).read_text())["journal"]["offset"] == 3


@pytest.mark.parametrize(
    "corruption",
    ["{not json", "[]", '{"schema": "other"}', '{"schema": "brigade.work_dispatch_exclusion_index.v1"}'],
)
def test_index_rebuilds_after_corruption(target, corruption):
    _receipt(target, task_id="task-0", stamp=_stamp(0), state="failed", attempt=1, terminal=FAIL_KIND)
    _receipt(target, task_id="task-1", stamp=_stamp(1), state="running", attempt=1)
    _assert_matches_full_scan(target)

    dispatch_index.state_path(target).write_text(corruption)

    _assert_matches_full_scan(target)
    index = json.loads(dispatch_index.state_path(target).read_text())
    assert index["schema"] == dispatch_index.SCHEMA
    assert index["journal"]["offset"] == 2


def test_index_rebuilds_when_missing(target):
    _receipt(target, task_id="task-0", stamp=_stamp(0), state="running", attempt=1)
    _assert_matches_full_scan(target)
    dispatch_index.state_path(target).unlink()
    _assert_matches_full_scan(target)
    assert dispatch_index.state_path(target).is_file()


def test_index_reads_only_new_receipts_per_tick(target, monkeypatch):
    for step in range(8):
        _receipt(
            target, task_id=f"task-{step % 2}", stamp=_stamp(step), state="failed", attempt=step + 1, terminal=FAIL_KIND
        )
    _assert_matches_full_scan(target)

    reads: list[str] = []
    read_record = dispatch_index._read_record

    def counting(path: Path) -> dict:
        reads.append(path.name)
        return read_record(path)

    monkeypatch.setattr(dispatch_index, "_read_record", counting)

    # One more receipt arrives. A tick must not re-read the eight already folded.
    _receipt(target, task_id="task-0", stamp=_stamp(8), state="completed", attempt=99)
    _assert_matches_full_scan(target)
    assert len(reads) == 1, reads

    reads.clear()
    # Two ticks with no journal change read no receipts at all.
    _assert_matches_full_scan(target)
    assert reads == []


def test_index_refolds_a_rewritten_receipt(target):
    receipts = []
    for attempt in range(1, 4):
        path, row = _receipt(target, task_id="task-0", stamp=_stamp(attempt), state="planned", attempt=attempt)
        receipts.append((path, row))
    assert store.exclusions(target) == {"task-0": "active-intent"}
    assert store.failure_counts(target) == {}

    for expected, (path, row) in enumerate(receipts, start=1):
        _rewrite(path, row, "failed", FAIL_KIND)
        # A rewritten receipt must be re-folded without waiting for a new append.
        assert store.failure_counts(target)["task-0"] == expected
        _assert_matches_full_scan(target)
    assert store.exclusions(target) == {"task-0": "circuit-breaker"}


def test_index_applies_a_reset_without_rescanning_history(target):
    for step in range(4):
        _receipt(target, task_id="task-0", stamp=_stamp(step), state="failed", attempt=step + 1, terminal=FAIL_KIND)
    assert store.exclusions(target) == {"task-0": "circuit-breaker"}

    _reset(target, "task-0", _stamp(9))

    _assert_matches_full_scan(target)
    assert store.exclusions(target) == {}


def test_index_reevaluates_completed_unlanded_against_the_live_ledger(target, monkeypatch):
    _, row = _receipt(target, task_id="task-0", stamp=_stamp(0), state="completed", attempt=1, claimed_revision=3)
    _write_ledger(target, [_task("task-0", item_revision=3, claim_id=row["claim_id"])])
    assert store.exclusions(target) == {"task-0": "completed-unlanded"}

    reads: list[str] = []
    read_record = dispatch_index._read_record

    def counting(path: Path) -> dict:
        reads.append(path.name)
        return read_record(path)

    monkeypatch.setattr(dispatch_index, "_read_record", counting)
    _write_ledger(target, [_task("task-0", status="pending", item_revision=3)])
    assert store.exclusions(target) == {}
    assert reads == []


def test_index_skips_refolding_a_tail_rewrite_that_keeps_its_bytes(target, monkeypatch):
    receipts = [
        _receipt(target, task_id="task-0", stamp=_stamp(attempt), state="failed", attempt=attempt, terminal=FAIL_KIND)
        for attempt in range(1, 4)
    ]
    _assert_matches_full_scan(target)

    reads: list[str] = []
    read_record = dispatch_index._read_record

    def counting(path: Path) -> dict:
        reads.append(path.name)
        return read_record(path)

    monkeypatch.setattr(dispatch_index, "_read_record", counting)
    # The last receipt is rewritten with its own bytes, so the stored prefix
    # fingerprint still matches and the task does not need re-folding.
    localio.write_json(receipts[-1][0], receipts[-1][1])
    _assert_matches_full_scan(target)
    assert len(reads) == 1, reads


def test_index_rebuilds_when_a_receipt_is_inserted_before_the_cursor(target):
    _receipt(target, task_id="task-0", stamp=_stamp(5), state="failed", attempt=1, terminal=FAIL_KIND)
    _assert_matches_full_scan(target)

    # An older receipt shows up behind the cursor: the fold order changes, so the
    # index cannot be advanced by appending.
    _receipt(target, task_id="task-0", stamp=_stamp(1), state="failed", attempt=2, terminal=FAIL_KIND)

    _assert_matches_full_scan(target)
    assert store.failure_counts(target)["task-0"] == 2


def test_index_file_stays_private(target):
    _receipt(target, task_id="task-0", stamp=_stamp(0), state="running", attempt=1)
    _assert_matches_full_scan(target)
    assert_private_mode(dispatch_index.state_path(target), PRIVATE_FILE_MODE)
