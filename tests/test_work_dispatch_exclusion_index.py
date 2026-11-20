"""The persisted dispatch exclusion index must match the full receipt scan.

The index caches a fold over the local intent receipts, so every test compares
it against an independent full scan of the same receipts. The scan helpers
below are a literal copy of the pre-index implementation; they are the oracle,
not a second copy of the new code.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import time
from pathlib import Path
from uuid import uuid4

import pytest

from brigade import localio, work_cmd, worker_failure
from brigade.work_cmd import dispatch as dispatch_cmd
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
    """Compare inside the dispatch lease, which is when the index is written."""
    with store.dispatch_lock(target):
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
    intent_id: str | None = None,
    session: str = SESSION,
) -> tuple[Path, dict]:
    if intent_id is None:
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


def _rewrite(path: Path, row: dict, state: str, terminal: dict | None = None, **updates: object) -> dict:
    updated = {**row, **updates, "state": state, "terminal": terminal, "state_seq": row["state_seq"] + 1}
    localio.write_json(path, updated)
    return updated


def _rewrite_in_place(path: Path, row: dict, state: str) -> None:
    """Rewrite a receipt keeping its inode, byte length, and mtime.

    This is what a recycled inode plus a coarse filesystem timestamp look like to
    a reader that trusts those three values: the bytes changed and nothing else
    did.
    """
    before = path.stat()
    path.write_text(json.dumps({**row, "state": state}, indent=2, sort_keys=True) + "\n")
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = path.stat()
    assert after.st_size == before.st_size
    assert after.st_mtime_ns == before.st_mtime_ns
    assert after.st_ino == before.st_ino


def _age_receipts(target: Path, *, seconds: int = 3600) -> None:
    """Move receipt mtimes into the past, as an established watch history has."""
    stamp = time.time_ns() - seconds * 1_000_000_000
    for path in store.root(target).glob("dispatch-*/intents/*.json"):
        stat = path.stat()
        os.utime(path, ns=(stat.st_atime_ns, stamp))


def _reset(target: Path, task_id: str, created_at: str) -> None:
    localio.write_json(
        store.root(target) / "resets" / f"{uuid4().hex}.json",
        {"schema": "brigade.work_dispatch_reset.v1", "task_id": task_id, "reason": "test", "created_at": created_at},
    )


def _completed_revision(row: dict) -> int:
    return row.get("claimed_item_revision", row["task_item_revision"] + 1)


@pytest.fixture
def target(tmp_path: Path) -> Path:
    _write_ledger(tmp_path, [_task("task-0", status="pending"), _task("task-1", status="pending")])
    return tmp_path


def test_index_keeps_every_completed_receipt_that_matches_the_live_claim(target):
    # A later completion must not hide an earlier one whose claim the ledger
    # still holds; the full scan excludes task-0 here.
    _, early = _receipt(target, task_id="task-0", stamp=_stamp(0), state="completed", attempt=1, claimed_revision=3)
    _receipt(target, task_id="task-0", stamp=_stamp(1), state="completed", attempt=2, claimed_revision=4)
    _write_ledger(target, [_task("task-0", item_revision=3, claim_id=early["claim_id"])])

    assert _reference_exclusions(target) == {"task-0": "completed-unlanded"}
    assert store.exclusions(target) == {"task-0": "completed-unlanded"}
    _assert_matches_full_scan(target)


def test_index_matches_full_scan_through_random_history(target, monkeypatch):
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
    receipts: list[tuple[Path, dict]] = []
    completions: dict[str, list[tuple[str, int]]] = {task_id: [] for task_id in tasks}
    attempts = dict.fromkeys(tasks, 0)
    counter = 0

    def point_ledger(task_id: str) -> None:
        pool = completions[task_id]
        if not pool or rng.random() < 0.25:
            tasks[task_id] = _task(task_id, status=rng.choice(["pending", "in_progress"]))
            return
        claim_id, revision = pool[rng.randrange(len(pool))]
        tasks[task_id] = _task(task_id, item_revision=revision, claim_id=claim_id)

    def add(task_id: str, stamp: str, state: str, **kwargs: object) -> None:
        path, row = _receipt(target, task_id=task_id, stamp=stamp, state=state, attempt=attempts[task_id], **kwargs)
        receipts.append((path, row))
        if state == "completed":
            completions[task_id].append((row["claim_id"], _completed_revision(row)))
            if rng.random() < 0.5:
                point_ledger(task_id)

    for _ in range(320):
        action = rng.choices(
            ["append", "rewrite", "ledger", "reset", "delete", "insert", "shadow"],
            weights=[45, 18, 10, 6, 7, 5, 9],
        )[0]
        task_id = rng.choice(sorted(tasks))
        if action == "append":
            counter += 1
            attempts[task_id] += 1
            state = rng.choice(STATES)
            if state == "completed":
                add(
                    task_id,
                    _stamp(counter),
                    state,
                    item_revision=rng.choice([2, 3]),
                    claimed_revision=rng.choice([2, 3, 4, 5]),
                )
            elif state == "failed":
                add(task_id, _stamp(counter), state, terminal=rng.choice(TERMINALS))
            else:
                add(task_id, _stamp(counter), state)
        elif action == "rewrite" and receipts:
            index = rng.randrange(len(receipts))
            path, row = receipts[index]
            state = rng.choice(["planned", "failed", "completed", "running"])
            if state == "completed":
                updated = _rewrite(
                    path,
                    row,
                    state,
                    claimed_revision=rng.choice([2, 3, 4, 5]),
                    claim_id="dispatch-rewrite-" + row["intent_id"],
                )
                completions[row["task_id"]].append((updated["claim_id"], _completed_revision(updated)))
                if rng.random() < 0.5:
                    point_ledger(row["task_id"])
            elif state == "failed":
                updated = _rewrite(path, row, state, terminal=rng.choice(TERMINALS))
            else:
                updated = _rewrite(path, row, state)
            receipts[index] = (path, updated)
        elif action == "shadow" and completions[task_id]:
            # Force the shape the fold must not lose: a newer completion lands
            # while the ledger still holds an earlier claim for the same task.
            counter += 1
            attempts[task_id] += 1
            apparent_claim, apparent_revision = completions[task_id][rng.randrange(len(completions[task_id]))]
            _, row = _receipt(
                target,
                task_id=task_id,
                stamp=_stamp(counter),
                state="completed",
                attempt=attempts[task_id],
                claimed_revision=rng.choice([2, 3, 4, 5]),
            )
            completions[task_id].append((row["claim_id"], _completed_revision(row)))
            tasks[task_id] = _task(task_id, item_revision=apparent_revision, claim_id=apparent_claim)
        elif action == "ledger":
            point_ledger(task_id)
        elif action == "reset":
            counter += 1
            _reset(target, task_id, _stamp(counter))
        elif action == "delete" and receipts:
            path, _ = receipts.pop(rng.randrange(len(receipts)))
            path.unlink()
        elif action == "insert":
            # Clock skew, or the same created_at with a smaller intent_id: both
            # sort behind the cursor and cannot be appended to it.
            counter += 1
            if rng.random() < 0.5 and receipts:
                stamp = receipts[-1][1]["created_at"]
                intent_id = f"000000000000000{rng.randrange(10)}"
            else:
                stamp = _stamp(max(0, counter - 25))
                intent_id = f"f{hashlib.sha256(str(counter).encode()).hexdigest()[:14]}"
            add(
                task_id,
                stamp,
                rng.choice(STATES),
                intent_id=intent_id,
                terminal=rng.choice(TERMINALS),
            )
        _write_ledger(target, [tasks[key] for key in sorted(tasks)])
        _assert_matches_full_scan(target)
    assert 0 < len(rebuilds) < 320
    with store.dispatch_lock(target):
        index = json.loads(dispatch_index.state_path(target).read_text())
        assert index["journal"]["offset"] == len(store.intents(target))


def test_index_rebuilds_when_the_journal_gets_shorter(target):
    for step in range(5):
        _receipt(target, task_id="task-0", stamp=_stamp(step), state="failed", attempt=step + 1, terminal=FAIL_KIND)
    _assert_matches_full_scan(target)
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


def test_index_reads_only_new_or_recently_written_receipts_per_tick(target, monkeypatch):
    for step in range(8):
        _receipt(
            target,
            task_id=f"task-{step % 2}",
            stamp=_stamp(step),
            state="failed",
            attempt=step + 1,
            terminal=FAIL_KIND,
        )
    # An established history: these receipts are older than the safety window.
    _age_receipts(target)
    _assert_matches_full_scan(target)
    _assert_matches_full_scan(target)

    reads: list[str] = []
    read_record = dispatch_index._read_record

    def counting(path: Path) -> dict:
        reads.append(path.name)
        return read_record(path)

    monkeypatch.setattr(dispatch_index, "_read_record", counting)

    # A tick with no journal change reads nothing.
    _assert_matches_full_scan(target)
    assert reads == []

    # One more receipt arrives. A planning call must not re-read the eight already
    # folded ones.
    _receipt(target, task_id="task-0", stamp=_stamp(8), state="completed", attempt=99)
    with store.dispatch_lock(target):
        assert store.exclusions(target) == _reference_exclusions(target)
    assert len(reads) == 1, reads

    # A receipt written inside the safety window is re-verified by content until
    # it ages out, because identity alone cannot rule out a same-tick rewrite.
    reads.clear()
    with store.dispatch_lock(target):
        assert store.exclusions(target) == _reference_exclusions(target)
    assert len(reads) == 1, reads

    _age_receipts(target)
    _assert_matches_full_scan(target)
    reads.clear()
    with store.dispatch_lock(target):
        assert store.exclusions(target) == _reference_exclusions(target)
    assert reads == []


def test_index_refolds_a_rewritten_receipt(target):
    receipts = []
    for attempt in range(1, 4):
        path, row = _receipt(target, task_id="task-0", stamp=_stamp(attempt), state="planned", attempt=attempt)
        receipts.append((path, row))
    _assert_matches_full_scan(target)
    assert store.exclusions(target) == {"task-0": "active-intent"}
    assert store.failure_counts(target) == {}

    for expected, (path, row) in enumerate(receipts, start=1):
        _rewrite(path, row, "failed", FAIL_KIND)
        # A rewritten receipt must be re-folded without waiting for a new append.
        with store.dispatch_lock(target):
            assert store.failure_counts(target)["task-0"] == expected
        _assert_matches_full_scan(target)
    assert store.exclusions(target) == {"task-0": "circuit-breaker"}


def test_index_verifies_a_receipt_that_hides_its_active_state(target):
    # A finished receipt becomes an active state with the same byte length, the
    # same mtime, and the same inode. Trusting that identity alone leaves the task
    # dispatchable while a worker already holds it.
    path, row = _receipt(target, task_id="task-0", stamp=_stamp(0), state="completed", attempt=1)
    _assert_matches_full_scan(target)
    assert store.exclusions(target) == {}

    _rewrite_in_place(path, row, "launching")

    assert _reference_exclusions(target) == {"task-0": "active-intent"}
    _assert_matches_full_scan(target)


def test_index_verifies_a_receipt_that_hides_its_completion(target):
    # The same rewrite the other way: a stale view keeps a finished task excluded.
    path, row = _receipt(target, task_id="task-0", stamp=_stamp(0), state="launching", attempt=1)
    _assert_matches_full_scan(target)
    assert store.exclusions(target) == {"task-0": "active-intent"}

    _rewrite_in_place(path, row, "completed")

    assert _reference_exclusions(target) == {}
    _assert_matches_full_scan(target)


def test_index_rebuilds_when_reset_receipts_change(target, monkeypatch):
    for step in range(4):
        _receipt(target, task_id="task-0", stamp=_stamp(step), state="failed", attempt=step + 1, terminal=FAIL_KIND)
    _assert_matches_full_scan(target)
    assert store.exclusions(target) == {"task-0": "circuit-breaker"}

    rebuilds: list[int] = []
    rebuild = dispatch_index._rebuild

    def counting_rebuild(*args, **kwargs):
        rebuilds.append(len(rebuilds))
        return rebuild(*args, **kwargs)

    monkeypatch.setattr(dispatch_index, "_rebuild", counting_rebuild)
    _reset(target, "task-0", _stamp(9))

    _assert_matches_full_scan(target)
    assert rebuilds
    assert store.exclusions(target) == {}


def test_index_reevaluates_completed_unlanded_against_the_live_ledger(target, monkeypatch):
    _, row = _receipt(target, task_id="task-0", stamp=_stamp(0), state="completed", attempt=1, claimed_revision=3)
    _write_ledger(target, [_task("task-0", item_revision=3, claim_id=row["claim_id"])])
    _age_receipts(target)
    _assert_matches_full_scan(target)
    _assert_matches_full_scan(target)
    assert store.exclusions(target) == {"task-0": "completed-unlanded"}

    reads: list[str] = []
    read_record = dispatch_index._read_record

    def counting(path: Path) -> dict:
        reads.append(path.name)
        return read_record(path)

    monkeypatch.setattr(dispatch_index, "_read_record", counting)
    _write_ledger(target, [_task("task-0", status="pending", item_revision=3)])
    with store.dispatch_lock(target):
        assert store.exclusions(target) == {}
    assert reads == []


def test_index_skips_refolding_a_rewrite_whose_bytes_match(target, monkeypatch):
    receipts = [
        _receipt(target, task_id="task-0", stamp=_stamp(attempt), state="failed", attempt=attempt, terminal=FAIL_KIND)
        for attempt in range(1, 4)
    ]
    _age_receipts(target)
    _assert_matches_full_scan(target)
    _assert_matches_full_scan(target)

    reads: list[str] = []
    read_record = dispatch_index._read_record

    def counting(path: Path) -> dict:
        reads.append(path.name)
        return read_record(path)

    monkeypatch.setattr(dispatch_index, "_read_record", counting)
    # The last receipt is rewritten with its own bytes, so its digest still
    # matches and the task does not need re-folding.
    localio.write_json(receipts[-1][0], receipts[-1][1])
    with store.dispatch_lock(target):
        assert store.exclusions(target) == _reference_exclusions(target)
    assert len(reads) == 1, reads


def test_index_rebuilds_when_a_receipt_is_inserted_before_the_cursor(target):
    _receipt(target, task_id="task-0", stamp=_stamp(5), state="failed", attempt=1, terminal=FAIL_KIND)
    _assert_matches_full_scan(target)

    # An older receipt shows up behind the cursor: the fold order changes, so the
    # index cannot be advanced by appending.
    _receipt(target, task_id="task-0", stamp=_stamp(1), state="failed", attempt=2, terminal=FAIL_KIND)

    _assert_matches_full_scan(target)
    assert store.failure_counts(target)["task-0"] == 2


def test_read_only_callers_never_write_the_index(target):
    _receipt(target, task_id="task-0", stamp=_stamp(0), state="running", attempt=1)

    # A dry-run plan and `dispatch status` compute in memory only.
    assert store.exclusions(target) == {"task-0": "active-intent"}
    assert store.failure_counts(target) == {}
    assert not dispatch_index.state_path(target).exists()

    with store.dispatch_lock(target):
        assert store.exclusions(target) == {"task-0": "active-intent"}
    assert dispatch_index.state_path(target).is_file()


def test_dry_run_and_status_leave_no_index(target, capsys):
    _receipt(target, task_id="task-0", stamp=_stamp(0), state="running", attempt=1)
    roster = target / ".brigade" / "roster.toml"
    roster.write_text(
        'orchestrator = "chef"\n'
        '[agents.chef]\ncli = "codex"\nrole = "plan"\n'
        '[agents.coder]\ncli = "codex"\nrole = "write code"\n'
        "[limits]\nmax_workers = 2\n"
    )

    assert dispatch_cmd.dispatch(target=target, dry_run=True, worker="coder", json_output=True) == 0
    assert not dispatch_index.state_path(target).exists()

    assert store.status(target=target, json_output=True) == 0
    assert not dispatch_index.state_path(target).exists()
    capsys.readouterr()


@pytest.mark.parametrize("blocker", ["unwritable", "directory"])
def test_index_write_failure_still_returns_exclusions(target, monkeypatch, blocker):
    _receipt(target, task_id="task-0", stamp=_stamp(0), state="running", attempt=1)
    path = dispatch_index.state_path(target)
    if blocker == "unwritable":

        def fail_write(*args, **kwargs):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(dispatch_index, "_write", fail_write)
    else:
        path.mkdir(parents=True)

    with store.dispatch_lock(target):
        with pytest.warns(UserWarning, match="exclusion index"):
            assert store.exclusions(target) == {"task-0": "active-intent"}
        with pytest.warns(UserWarning, match="exclusion index"):
            assert store.failure_counts(target) == {}


def test_index_file_stays_private(target):
    _receipt(target, task_id="task-0", stamp=_stamp(0), state="running", attempt=1)
    _assert_matches_full_scan(target)
    assert_private_mode(dispatch_index.state_path(target), PRIVATE_FILE_MODE)
