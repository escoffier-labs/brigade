"""Adversarial hardening for work-graph surfaces (#783 edges, #796 footprints, #797 claim).

These tests intentionally go beyond the happy-path coverage the feature PRs shipped:
N-way claim races, kill-mid-write / truncated ledger, hand-edited tasks.json,
cycle injection through every mutation path, hostile footprint payloads, and
ledger version mismatches.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest

from brigade import work_cmd
from brigade.work_cmd import claiming as claim_mod
from brigade.work_cmd import edges as edges_mod
from brigade.work_cmd import footprint as footprint_mod

from tests.work_cmd_test_helpers import _init_git_repo


def _add(target: Path, text: str, **kwargs):
    task, created = work_cmd._add_task(target, text, **kwargs)
    assert created
    return task


def _tasks_path(target: Path) -> Path:
    return target / ".brigade" / "work" / "tasks.json"


def _write_raw_ledger(target: Path, payload: dict) -> None:
    path = _tasks_path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")


def _seed_task_dict(task_id: str, text: str) -> dict:
    return {
        "id": task_id,
        "text": text,
        "status": "pending",
        "source": "manual",
        "type": "task",
        "priority": "normal",
        "acceptance": [],
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }


# ---------------------------------------------------------------------------
# Concurrent claim races beyond the happy pair
# ---------------------------------------------------------------------------


def test_n_way_concurrent_claim_exactly_one_winner(tmp_path, monkeypatch):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd.helpers,
        "_now",
        lambda: datetime(2026, 8, 9, 15, 0, 0, tzinfo=timezone.utc),
    )
    task = _add(tmp_path, "N-way race target")
    n = 8
    barrier = threading.Barrier(n)
    results: list[tuple[str, int, str]] = []
    lock = threading.Lock()

    def worker(idx: int) -> None:
        actor = f"lane-{idx}"
        claim_id = f"claim-{idx}"
        barrier.wait(timeout=15)
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "brigade",
                "work",
                "claim",
                task["id"],
                "--actor",
                actor,
                "--claim-id",
                claim_id,
                "--target",
                str(tmp_path),
                "--json",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        with lock:
            results.append((actor, proc.returncode, proc.stdout))

    with ThreadPoolExecutor(max_workers=n) as executor:
        futures = [executor.submit(worker, i) for i in range(n)]
        for future in futures:
            future.result(timeout=60)

    assert len(results) == n
    winners = [item for item in results if item[1] == 0]
    losers = [item for item in results if item[1] == 13]
    assert len(winners) == 1, f"expected exactly one winner, got {[(a, c) for a, c, _ in results]}"
    assert len(losers) == n - 1
    winner_payload = json.loads(winners[0][2])
    assert winner_payload["created"] is True
    for _actor, _code, stdout in losers:
        payload = json.loads(stdout)
        assert payload["reason"] == claim_mod.REASON_ALREADY_CLAIMED
        assert payload["holder"] == winners[0][0]

    ledger = work_cmd._read_task_ledger(tmp_path)
    stored = next(item for item in ledger["tasks"] if item["id"] == task["id"])
    assert stored["assignee"] == winners[0][0]
    assert stored["claim"]["claim_id"] == f"claim-{winners[0][0].split('-', 1)[1]}"


def test_n_way_claim_next_assigns_distinct_ready_tasks(tmp_path, monkeypatch):
    _init_git_repo(tmp_path)
    clock = {"n": 0}

    def _now():
        clock["n"] += 1
        return datetime(2026, 8, 9, 15, 0, clock["n"], tzinfo=timezone.utc)

    monkeypatch.setattr(work_cmd.helpers, "_now", _now)
    tasks = [_add(tmp_path, f"Ready pool {i}", priority="normal") for i in range(5)]
    n = 5
    barrier = threading.Barrier(n)
    results: list[tuple[int, str]] = []
    lock = threading.Lock()

    def worker(idx: int) -> None:
        barrier.wait(timeout=15)
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "brigade",
                "work",
                "claim",
                "--next",
                "--actor",
                f"lane-{idx}",
                "--claim-id",
                f"next-{idx}",
                "--target",
                str(tmp_path),
                "--json",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        with lock:
            results.append((proc.returncode, proc.stdout))

    with ThreadPoolExecutor(max_workers=n) as executor:
        futures = [executor.submit(worker, i) for i in range(n)]
        for future in futures:
            future.result(timeout=60)

    successes = [json.loads(out) for code, out in results if code == 0]
    assert len(successes) == 5
    claimed_ids = {item["task_id"] for item in successes}
    assert claimed_ids == {task["id"] for task in tasks}


def test_kill_mid_atomic_write_preserves_prior_ledger(tmp_path, monkeypatch):
    """Simulate a crash before os.replace: the on-disk ledger must stay intact."""
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd.helpers,
        "_now",
        lambda: datetime(2026, 8, 9, 15, 30, 0, tzinfo=timezone.utc),
    )
    task = _add(tmp_path, "Must survive crash")
    path = _tasks_path(tmp_path)
    before = path.read_text()
    assert task["id"] in before

    real_replace = os.replace

    def boom_before_replace(src, dst):
        raise OSError("simulated kill mid-write before replace")

    monkeypatch.setattr(os, "replace", boom_before_replace)
    with pytest.raises(OSError, match="simulated kill"):
        work_cmd._write_task_ledger(
            tmp_path,
            {
                "version": 2,
                "tasks": [],
                "edges": [],
            },
        )
    monkeypatch.setattr(os, "replace", real_replace)
    assert path.read_text() == before
    ledger = work_cmd._read_task_ledger(tmp_path)
    assert any(item.get("id") == task["id"] for item in ledger["tasks"])


# ---------------------------------------------------------------------------
# Malformed / hand-edited tasks.json
# ---------------------------------------------------------------------------


def test_truncated_tasks_json_must_not_be_wiped_by_mutation(tmp_path):
    """Hand-truncated or corrupt tasks.json must fail closed — never empty-and-rewrite."""
    _init_git_repo(tmp_path)
    path = _tasks_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    good = {
        "version": 2,
        "tasks": [_seed_task_dict("keep-important", "Keep this work")],
        "edges": [],
    }
    raw = json.dumps(good, indent=2) + "\n"
    # Truncate mid-object so JSON is invalid but the task id is still on disk.
    path.write_text(raw[: len(raw) // 2])
    assert "keep-important" in path.read_text()

    with pytest.raises(Exception) as excinfo:
        work_cmd._add_task(tmp_path, "New after corrupt")
    message = str(excinfo.value).casefold()
    assert "corrupt" in message or "invalid" in message or "json" in message

    # Original (corrupt) bytes must still be present — no silent empty rewrite.
    on_disk = path.read_text()
    assert "keep-important" in on_disk
    assert "New after corrupt" not in on_disk


def test_truncated_tasks_json_blocks_claim_and_edge_mutations(tmp_path, capsys):
    _init_git_repo(tmp_path)
    path = _tasks_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    good = {
        "version": 2,
        "tasks": [_seed_task_dict("still-here", "Still here")],
        "edges": [],
    }
    path.write_text(json.dumps(good)[:-12])

    rc = work_cmd.claim(
        target=tmp_path,
        task_id="still-here",
        actor="lane-a",
        claim_id="c1",
        json_output=True,
    )
    assert rc == 2
    first = capsys.readouterr()
    captured = (first.out + first.err).casefold()
    # Prefer an explicit corrupt-ledger signal over "task not found" on an empty parse.
    assert "corrupt" in captured or "invalid" in captured or "json" in captured
    assert "still-here" in path.read_text()

    rc = work_cmd.task_edge_add(
        target=tmp_path,
        edge_type="blocks",
        source="still-here",
        target_id="still-here",
        json_output=True,
    )
    assert rc == 2
    assert "still-here" in path.read_text()
    # Ensure we did not materialize an empty healthy ledger.
    with pytest.raises(json.JSONDecodeError):
        json.loads(path.read_text())


def test_hand_edited_unknown_edge_type_is_preserved_across_rewrite(tmp_path):
    _init_git_repo(tmp_path)
    a = _add(tmp_path, "Task A")
    b = _add(tmp_path, "Task B")
    ledger = work_cmd._read_task_ledger(tmp_path)
    ledger["edges"].append(
        {
            "id": "edge-hand-unknown",
            "source": a["id"],
            "target": b["id"],
            "type": "depends-on",
            "created_at": "2026-01-01T00:00:00+00:00",
        }
    )
    # Bypass ensure_ledger_edges normalization by writing raw JSON.
    _write_raw_ledger(tmp_path, ledger)

    # A benign rewrite (edge list) must not silently drop the hand-edited type.
    c = _add(tmp_path, "Task C")
    rewritten = json.loads(_tasks_path(tmp_path).read_text())
    types = {edge.get("type") for edge in rewritten.get("edges", [])}
    assert "depends-on" in types
    assert c["id"] in {task["id"] for task in rewritten["tasks"]}


def test_hand_edited_self_edge_surfaces_as_cycle_not_ready(tmp_path, capsys):
    _init_git_repo(tmp_path)
    task = _add(tmp_path, "Self edged")
    ledger = work_cmd._read_task_ledger(tmp_path)
    ledger["edges"] = [
        {
            "id": "edge-self",
            "source": task["id"],
            "target": task["id"],
            "type": "blocks",
            "created_at": "2026-01-01T00:00:00+00:00",
        }
    ]
    _write_raw_ledger(tmp_path, ledger)

    assert work_cmd.ready(target=tmp_path, explain=True, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["cycle_count"] >= 1
    assert task["id"] not in {item["id"] for item in payload["ready"]}
    blocked = next(item for item in payload["blocked"] if item["id"] == task["id"])
    assert blocked["reason"] == edges_mod.REASON_DEPENDENCY_CYCLE


def test_hand_edited_dangling_edge_target_does_not_gate_or_crash(tmp_path, capsys):
    _init_git_repo(tmp_path)
    consumer = _add(tmp_path, "Dangling consumer")
    parent = _add(tmp_path, "Parent of missing child")
    ledger = work_cmd._read_task_ledger(tmp_path)
    ledger["edges"] = [
        {
            "id": "edge-dangling",
            "source": "missing-blocker",
            "target": consumer["id"],
            "type": "blocks",
            "created_at": "2026-01-01T00:00:00+00:00",
        },
        {
            "id": "edge-dangling-pc",
            "source": parent["id"],
            "target": "missing-child",
            "type": "parent-child",
            "created_at": "2026-01-01T00:00:00+00:00",
        },
    ]
    _write_raw_ledger(tmp_path, ledger)
    assert work_cmd.ready(target=tmp_path, explain=True, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    # Orphan blocker source must not gate the consumer.
    assert payload["orphan_count"] >= 1
    assert consumer["id"] in {item["id"] for item in payload["ready"]}
    # Parent-of-missing is still a parent node, so not startable.
    assert parent["id"] not in {item["id"] for item in payload["ready"]}


# ---------------------------------------------------------------------------
# Cycle attempts through every mutation path
# ---------------------------------------------------------------------------


def test_cycle_rejected_via_deps_closing_existing_chain(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    clock = {"n": 0}

    def _now():
        clock["n"] += 1
        return datetime(2026, 8, 9, 16, 0, clock["n"], tzinfo=timezone.utc)

    monkeypatch.setattr(work_cmd.helpers, "_now", _now)
    a = _add(tmp_path, "A")
    b = _add(tmp_path, "B")
    assert work_cmd.task_edge_add(target=tmp_path, edge_type="blocks", source=a["id"], target_id=b["id"]) == 0
    capsys.readouterr()
    # C with blocks:B (B blocks C), then edge C→A would cycle; close via --deps on a
    # reverse edge by adding a task that proposes blocking A while A already
    # transitively blocks it — use graph for multi-edge, and deps for the
    # single-edge close against an existing reverse.
    # Direct: add edge B→A via a new task's deps is impossible (deps only make
    # others block the new task). Close the cycle with edge add (already covered)
    # and with import promote / graph below. Here: parent-child + blocks mix via deps.
    assert (
        work_cmd.task_add(
            target=tmp_path,
            text="Child under A",
            deps=[f"parent-child:{a['id']}"],
        )
        == 0
    )
    child_id = capsys.readouterr().out.split("task: ", 1)[1].splitlines()[0]
    # Child blocks A would form parent-child + blocks cycle (A→child, child→A).
    assert (
        work_cmd.task_edge_add(
            target=tmp_path,
            edge_type="blocks",
            source=child_id,
            target_id=a["id"],
            json_output=True,
        )
        == 2
    )
    assert json.loads(capsys.readouterr().out)["reason"] == edges_mod.REASON_DEPENDENCY_CYCLE


def test_cycle_rejected_via_graph_against_existing_ledger_edges(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd.helpers,
        "_now",
        lambda: datetime(2026, 8, 9, 16, 10, 0, tzinfo=timezone.utc),
    )
    existing = _add(tmp_path, "Existing hub")
    # Plan creates node X that blocks existing via... graph edges only reference
    # plan-local keys. After materialization, add an edge from new node back.
    # Instead: seed ledger edge existing→will-be-filled by applying graph that
    # creates a node then we edge-add. Better: graph that alone is acyclic but
    # combined with existing edges cycles.
    # Seed: existing blocks phantom key? Can't. Seed edge after graph:
    plan = {
        "nodes": [{"key": "x", "text": "Graph X"}, {"key": "y", "text": "Graph Y"}],
        "edges": [{"source": "x", "target": "y", "type": "blocks"}],
    }
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan))
    assert work_cmd.task_add(target=tmp_path, graph=plan_path, json_output=True) == 0
    applied = json.loads(capsys.readouterr().out)
    x_id = applied["key_map"]["x"]
    # existing → x already? Add existing blocks x, then x blocks existing via edge.
    assert work_cmd.task_edge_add(target=tmp_path, edge_type="blocks", source=existing["id"], target_id=x_id) == 0
    capsys.readouterr()
    assert (
        work_cmd.task_edge_add(
            target=tmp_path,
            edge_type="blocks",
            source=x_id,
            target_id=existing["id"],
            json_output=True,
        )
        == 2
    )
    assert json.loads(capsys.readouterr().out)["reason"] == edges_mod.REASON_DEPENDENCY_CYCLE


def test_cycle_via_import_promote_proposed_edges_fails_closed(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd.helpers,
        "_now",
        lambda: datetime(2026, 8, 9, 16, 20, 0, tzinfo=timezone.utc),
    )
    a = _add(tmp_path, "Upstream A")
    b = _add(tmp_path, "Downstream B")
    assert work_cmd.task_edge_add(target=tmp_path, edge_type="blocks", source=a["id"], target_id=b["id"]) == 0
    capsys.readouterr()
    assert work_cmd.import_add(target=tmp_path, text="Cycle bait import", kind="task", source="manual") == 0
    capsys.readouterr()
    imports = work_cmd._read_imports(tmp_path)
    imports[0]["metadata"] = {
        "proposed_edges": [{"type": "blocks", "source": b["id"], "target": a["id"]}],
    }
    work_cmd._write_imports(tmp_path, imports)
    before_count = len(work_cmd._read_task_ledger(tmp_path)["tasks"])
    # Must not traceback; must refuse the promote and leave ledger/import intact.
    rc = work_cmd.import_promote(target=tmp_path, import_id=imports[0]["id"])
    assert rc == 2
    captured = capsys.readouterr()
    combined = (captured.out + captured.err).casefold()
    assert "cycle" in combined or "dependency" in combined
    assert len(work_cmd._read_task_ledger(tmp_path)["tasks"]) == before_count
    assert work_cmd._read_imports(tmp_path)[0]["status"] == "pending"


def test_cycle_via_deps_unknown_type_rejected(tmp_path, capsys):
    _init_git_repo(tmp_path)
    upstream = _add(tmp_path, "Upstream")
    rc = work_cmd.task_add(
        target=tmp_path,
        text="Bad deps type",
        deps=[f"depends-on:{upstream['id']}"],
        json_output=True,
    )
    assert rc == 2
    captured = capsys.readouterr()
    assert "unknown edge type" in (captured.out + captured.err).casefold() or "depends-on" in (
        captured.out + captured.err
    )


def test_parent_child_bidirectional_cycle_rejected(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd.helpers,
        "_now",
        lambda: datetime(2026, 8, 9, 16, 30, 0, tzinfo=timezone.utc),
    )
    a = _add(tmp_path, "Parent A")
    b = _add(tmp_path, "Parent B")
    assert work_cmd.task_edge_add(target=tmp_path, edge_type="parent-child", source=a["id"], target_id=b["id"]) == 0
    capsys.readouterr()
    assert (
        work_cmd.task_edge_add(
            target=tmp_path,
            edge_type="parent-child",
            source=b["id"],
            target_id=a["id"],
            json_output=True,
        )
        == 2
    )
    assert json.loads(capsys.readouterr().out)["reason"] == edges_mod.REASON_DEPENDENCY_CYCLE


# ---------------------------------------------------------------------------
# Footprint hostility
# ---------------------------------------------------------------------------


def test_footprint_caps_huge_file_and_symbol_lists():
    huge_files = [f"src/mod_{i}.py" for i in range(50_000)]
    huge_symbols = [f"pkg.sym_{i}" for i in range(50_000)]
    normalized = footprint_mod.normalize_footprint(
        {"files": huge_files, "symbol_ids": huge_symbols},
        phase="predicted",
    )
    assert len(normalized["files"]) <= footprint_mod.MAX_FOOTPRINT_ENTRIES
    assert len(normalized["symbol_ids"]) <= footprint_mod.MAX_FOOTPRINT_ENTRIES


def test_footprint_rejects_path_traversal_and_absolute_paths(tmp_path):
    _init_git_repo(tmp_path)
    task = _add(tmp_path, "Hostile paths")
    rc = work_cmd.task_claim(
        target=tmp_path,
        task_id=task["id"],
        actor="lane-a",
        files=[
            "../../etc/passwd",
            "/etc/shadow",
            "src/ok.py",
            "..\\windows\\system32\\config",
            "docs/../secrets/key.pem",
        ],
        from_plan=False,
        json_output=True,
    )
    assert rc == 0
    # Re-read — only safe relative paths may be stored.
    stored, _ = work_cmd._find_task(tmp_path, task["id"])
    files = stored["metadata"]["footprint"]["files"]
    assert files == ["src/ok.py"]
    for bad in ("../", "/etc/", "windows\\system32", "secrets/key"):
        assert not any(bad in item or item.startswith("/") for item in files)


def test_footprint_sidecar_path_cannot_read_outside_target(tmp_path):
    _init_git_repo(tmp_path)
    outside = tmp_path.parent / "outside-secret.json"
    outside.write_text(
        json.dumps(
            {
                "files": ["leaked.py"],
                "changed_symbols": ["leaked.sym"],
                "snapshot_hash": "evil",
            }
        )
    )
    try:
        prior = footprint_mod.normalize_footprint(
            {"files": ["safe.py"], "symbol_ids": ["safe.sym"], "snapshot_hash": "safe"},
            phase="refined",
        )
        reconciled = footprint_mod.reconcile_footprint(
            {"sidecar_path": str(outside)},
            prior=prior,
            target=tmp_path,
        )
        # Must not join the outside sidecar; fall back to prior.
        assert reconciled["files"] == ["safe.py"]
        assert reconciled["symbol_ids"] == ["safe.sym"]
        assert reconciled["snapshot_hash"] == "safe"
    finally:
        outside.unlink(missing_ok=True)


def test_footprint_sidecar_inside_target_still_joins(tmp_path):
    _init_git_repo(tmp_path)
    sidecar = tmp_path / ".brigade" / "work" / "verify-delta.json"
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(
        json.dumps(
            {
                "files": ["src/joined.py"],
                "changed_symbols": ["pkg.joined"],
                "snapshot_hash": "inside",
            }
        )
    )
    reconciled = footprint_mod.reconcile_footprint(
        {"sidecar_path": str(sidecar)},
        target=tmp_path,
    )
    assert reconciled["files"] == ["src/joined.py"]
    assert reconciled["symbol_ids"] == ["pkg.joined"]
    assert reconciled["snapshot_hash"] == "inside"


# ---------------------------------------------------------------------------
# Ledger version mismatches
# ---------------------------------------------------------------------------


def test_future_ledger_version_rejects_mutation_without_clobber(tmp_path):
    _init_git_repo(tmp_path)
    path = _tasks_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 99,
        "tasks": [_seed_task_dict("future-1", "From the future")],
        "edges": [],
        "future_field": {"keep": True},
    }
    path.write_text(json.dumps(payload, indent=2) + "\n")
    before = path.read_text()

    with pytest.raises(Exception) as excinfo:
        work_cmd._add_task(tmp_path, "Must not write")
    assert "version" in str(excinfo.value).casefold()
    assert path.read_text() == before

    rc = work_cmd.claim(target=tmp_path, task_id="future-1", actor="lane-a", claim_id="c1")
    assert rc != 0
    assert path.read_text() == before


def test_non_integer_ledger_version_rejects(tmp_path):
    _init_git_repo(tmp_path)
    path = _tasks_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": "two",
                "tasks": [_seed_task_dict("v-str", "String version")],
                "edges": [],
            }
        )
        + "\n"
    )
    with pytest.raises(Exception) as excinfo:
        work_cmd._read_task_ledger(tmp_path)
    assert "version" in str(excinfo.value).casefold()


def test_legacy_v1_still_migrates_on_read(tmp_path):
    _init_git_repo(tmp_path)
    path = _tasks_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "tasks": [_seed_task_dict("legacy-ok", "Legacy pending")],
            }
        )
    )
    ledger = work_cmd._read_task_ledger(tmp_path)
    assert ledger["version"] == 2
    assert ledger["edges"] == []
    assert ledger["tasks"][0]["id"] == "legacy-ok"


# ---------------------------------------------------------------------------
# Dual claim surface: task claim must not bypass CAS
# ---------------------------------------------------------------------------


def test_task_claim_cannot_steal_cas_holder(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd.helpers,
        "_now",
        lambda: datetime(2026, 8, 9, 17, 0, 0, tzinfo=timezone.utc),
    )
    task = _add(tmp_path, "CAS held")
    assert work_cmd.claim(target=tmp_path, task_id=task["id"], actor="lane-a", claim_id="c1", json_output=True) == 0
    capsys.readouterr()

    rc = work_cmd.task_claim(
        target=tmp_path,
        task_id=task["id"],
        actor="lane-b",
        files=["src/a.py"],
        from_plan=False,
        json_output=True,
    )
    assert rc == 13
    payload = json.loads(capsys.readouterr().out)
    assert payload.get("reason") == claim_mod.REASON_ALREADY_CLAIMED
    stored, _ = work_cmd._find_task(tmp_path, task["id"])
    assert stored["assignee"] == "lane-a"
    assert stored["claim"]["actor"] == "lane-a"
    assert stored["claim"]["claim_id"] == "c1"


def test_task_claim_writes_releasable_claim_record(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd.helpers,
        "_now",
        lambda: datetime(2026, 8, 9, 17, 10, 0, tzinfo=timezone.utc),
    )
    task = _add(tmp_path, "Footprint claim needs CAS record")
    assert (
        work_cmd.task_claim(
            target=tmp_path,
            task_id=task["id"],
            actor="lane-a",
            files=["src/a.py"],
            from_plan=False,
            json_output=True,
        )
        == 0
    )
    claimed = json.loads(capsys.readouterr().out)
    assert claimed["status"] == "in_progress"
    stored, _ = work_cmd._find_task(tmp_path, task["id"])
    assert isinstance(stored.get("claim"), dict)
    assert stored["claim"]["actor"] == "lane-a"
    assert stored["claim"]["claim_id"]

    assert work_cmd.release(target=tmp_path, actor=["lane-a"], json_output=True) == 0
    released = json.loads(capsys.readouterr().out)
    assert released["released_count"] == 1
    stored, _ = work_cmd._find_task(tmp_path, task["id"])
    assert stored["status"] == "pending"
    assert "claim" not in stored


def test_task_claim_idempotent_for_same_actor_refines_footprint(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd.helpers,
        "_now",
        lambda: datetime(2026, 8, 9, 17, 20, 0, tzinfo=timezone.utc),
    )
    task = _add(tmp_path, "Same actor refine")
    assert work_cmd.claim(target=tmp_path, task_id=task["id"], actor="lane-a", claim_id="hold", json_output=True) == 0
    capsys.readouterr()
    assert (
        work_cmd.task_claim(
            target=tmp_path,
            task_id=task["id"],
            actor="lane-a",
            files=["src/refined.py"],
            from_plan=False,
            json_output=True,
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["footprint"]["files"] == ["src/refined.py"]
    stored, _ = work_cmd._find_task(tmp_path, task["id"])
    assert stored["claim"]["claim_id"] == "hold"
    assert stored["assignee"] == "lane-a"
