"""Dependency edges and computed ready set for the work task ledger (#737)."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from brigade import work_cmd
from brigade.work_cmd import edges as edges_mod

from tests.work_cmd_test_helpers import _init_git_repo


def _add(target: Path, text: str, **kwargs):
    task, created = work_cmd._add_task(target, text, **kwargs)
    assert created
    return task


def test_edge_crud_add_list_remove_and_dedupe(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd.helpers,
        "_now",
        lambda: datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc),
    )
    a = _add(tmp_path, "Task A")
    b = _add(tmp_path, "Task B")

    assert (
        work_cmd.task_edge_add(target=tmp_path, edge_type="blocks", source=a["id"], target_id=b["id"], json_output=True)
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["created"] is True
    assert payload["edge"]["source"] == a["id"]
    assert payload["edge"]["target"] == b["id"]
    assert payload["edge"]["type"] == "blocks"
    edge_id = payload["edge"]["id"]

    assert (
        work_cmd.task_edge_add(target=tmp_path, edge_type="blocks", source=a["id"], target_id=b["id"], json_output=True)
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["created"] is False
    assert payload["edge"]["id"] == edge_id

    assert work_cmd.task_edge_list(target=tmp_path, json_output=True) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["count"] == 1

    assert work_cmd.task_edge_remove(target=tmp_path, edge_id=edge_id, json_output=True) == 0
    removed = json.loads(capsys.readouterr().out)
    assert len(removed["removed"]) == 1
    assert work_cmd.task_edge_list(target=tmp_path, json_output=True) == 0
    assert json.loads(capsys.readouterr().out)["count"] == 0


def test_ready_gating_by_edge_type(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    clock = {"n": 0}

    def _now():
        clock["n"] += 1
        return datetime(2026, 8, 9, 12, 0, clock["n"], tzinfo=timezone.utc)

    monkeypatch.setattr(work_cmd.helpers, "_now", _now)

    blocker = _add(tmp_path, "Blocker")
    blocked = _add(tmp_path, "Blocked by blocks edge")
    child = _add(tmp_path, "Child")
    parent = _add(tmp_path, "Parent")
    discovered = _add(tmp_path, "Discovered item")
    provenance = _add(tmp_path, "Provenance source")

    assert (
        work_cmd.task_edge_add(target=tmp_path, edge_type="blocks", source=blocker["id"], target_id=blocked["id"]) == 0
    )
    assert (
        work_cmd.task_edge_add(target=tmp_path, edge_type="parent-child", source=parent["id"], target_id=child["id"])
        == 0
    )
    assert (
        work_cmd.task_edge_add(
            target=tmp_path,
            edge_type="discovered-from",
            source=discovered["id"],
            target_id=provenance["id"],
        )
        == 0
    )
    capsys.readouterr()

    assert work_cmd.ready(target=tmp_path, explain=True, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    ready_ids = {item["id"] for item in payload["ready"]}
    assert blocker["id"] in ready_ids
    assert blocked["id"] not in ready_ids
    # parent-child does not gate the child on parent completion
    assert child["id"] in ready_ids
    # parent itself is not startable
    assert parent["id"] not in ready_ids
    # discovered-from never gates
    assert discovered["id"] in ready_ids
    assert provenance["id"] in ready_ids

    blocked_entry = next(item for item in payload["blocked"] if item["id"] == blocked["id"])
    assert blocked_entry["reason"] == "open_blockers"
    assert blocked_entry["blockers"][0]["id"] == blocker["id"]


def test_parent_propagates_blockers_to_children(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd.helpers,
        "_now",
        lambda: datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc),
    )
    upstream = _add(tmp_path, "Upstream blocker")
    parent = _add(tmp_path, "Epic parent")
    child = _add(tmp_path, "Epic child")
    assert (
        work_cmd.task_edge_add(target=tmp_path, edge_type="parent-child", source=parent["id"], target_id=child["id"])
        == 0
    )
    assert (
        work_cmd.task_edge_add(target=tmp_path, edge_type="blocks", source=upstream["id"], target_id=parent["id"]) == 0
    )
    capsys.readouterr()
    assert work_cmd.ready(target=tmp_path, explain=True, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    ready_ids = {item["id"] for item in payload["ready"]}
    assert upstream["id"] in ready_ids
    assert child["id"] not in ready_ids
    child_entry = next(item for item in payload["blocked"] if item["id"] == child["id"])
    assert child_entry["blockers"][0]["id"] == upstream["id"]
    assert parent["id"] in child_entry["blockers"][0]["via"]


def test_blocks_cycle_rejected_at_add_time(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd.helpers,
        "_now",
        lambda: datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc),
    )
    a = _add(tmp_path, "A")
    b = _add(tmp_path, "B")
    assert work_cmd.task_edge_add(target=tmp_path, edge_type="blocks", source=a["id"], target_id=b["id"]) == 0
    capsys.readouterr()
    assert (
        work_cmd.task_edge_add(target=tmp_path, edge_type="blocks", source=b["id"], target_id=a["id"], json_output=True)
        == 2
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["reason"] == edges_mod.REASON_DEPENDENCY_CYCLE
    assert "cycles" in payload


def test_three_node_transitive_blocks_cycle_rejected_at_add_time(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd.helpers,
        "_now",
        lambda: datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc),
    )
    a = _add(tmp_path, "A")
    b = _add(tmp_path, "B")
    c = _add(tmp_path, "C")
    assert work_cmd.task_edge_add(target=tmp_path, edge_type="blocks", source=a["id"], target_id=b["id"]) == 0
    assert work_cmd.task_edge_add(target=tmp_path, edge_type="blocks", source=b["id"], target_id=c["id"]) == 0
    capsys.readouterr()
    assert (
        work_cmd.task_edge_add(target=tmp_path, edge_type="blocks", source=c["id"], target_id=a["id"], json_output=True)
        == 2
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["reason"] == edges_mod.REASON_DEPENDENCY_CYCLE
    cycle_nodes = set(payload["cycles"][0]["nodes"])
    assert cycle_nodes == {a["id"], b["id"], c["id"]}


def test_mixed_blocks_parent_child_transitive_cycle_rejected_at_add_time(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd.helpers,
        "_now",
        lambda: datetime(2026, 8, 9, 12, 30, 0, tzinfo=timezone.utc),
    )
    parent = _add(tmp_path, "Epic parent")
    child = _add(tmp_path, "Epic child")
    blocker = _add(tmp_path, "Upstream blocker")
    assert (
        work_cmd.task_edge_add(target=tmp_path, edge_type="parent-child", source=parent["id"], target_id=child["id"])
        == 0
    )
    assert work_cmd.task_edge_add(target=tmp_path, edge_type="blocks", source=child["id"], target_id=blocker["id"]) == 0
    capsys.readouterr()
    assert (
        work_cmd.task_edge_add(
            target=tmp_path,
            edge_type="blocks",
            source=blocker["id"],
            target_id=parent["id"],
            json_output=True,
        )
        == 2
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["reason"] == edges_mod.REASON_DEPENDENCY_CYCLE
    cycle_nodes = set(payload["cycles"][0]["nodes"])
    assert cycle_nodes == {parent["id"], child["id"], blocker["id"]}


def test_discovered_from_ignored_by_cycle_detection(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd.helpers,
        "_now",
        lambda: datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc),
    )
    a = _add(tmp_path, "A")
    b = _add(tmp_path, "B")
    assert work_cmd.task_edge_add(target=tmp_path, edge_type="discovered-from", source=a["id"], target_id=b["id"]) == 0
    assert work_cmd.task_edge_add(target=tmp_path, edge_type="discovered-from", source=b["id"], target_id=a["id"]) == 0
    capsys.readouterr()
    assert work_cmd.ready(target=tmp_path, explain=True, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["cycle_count"] == 0
    assert {item["id"] for item in payload["ready"]} == {a["id"], b["id"]}


def test_ready_excludes_in_progress_blocked_deferred(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd.helpers,
        "_now",
        lambda: datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc),
    )
    pending = _add(tmp_path, "Pending ready")
    for status, text in (
        ("in_progress", "In progress"),
        ("blocked", "Blocked status"),
        ("deferred", "Deferred"),
    ):
        task = _add(tmp_path, text)
        ledger = work_cmd._read_task_ledger(tmp_path)
        for item in ledger["tasks"]:
            if item.get("id") == task["id"]:
                item["status"] = status
        work_cmd._write_task_ledger(tmp_path, ledger)

    assert work_cmd.ready(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [item["id"] for item in payload["ready"]] == [pending["id"]]


def test_terminal_statuses_satisfy_blockers(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd.helpers,
        "_now",
        lambda: datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc),
    )
    blocker = _add(tmp_path, "Will complete")
    blocked = _add(tmp_path, "Waiting")
    assert (
        work_cmd.task_edge_add(target=tmp_path, edge_type="blocks", source=blocker["id"], target_id=blocked["id"]) == 0
    )
    capsys.readouterr()
    assert work_cmd.task_done(target=tmp_path, task_id=blocker["id"], json_output=True) == 0
    done_payload = json.loads(capsys.readouterr().out)
    assert blocked["id"] in {item["id"] for item in done_payload["newly_unblocked"]}
    assert work_cmd.ready(target=tmp_path, json_output=True) == 0
    ready_ids = {item["id"] for item in json.loads(capsys.readouterr().out)["ready"]}
    assert blocked["id"] in ready_ids


def test_orphan_blocker_does_not_gate_ready(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd.helpers,
        "_now",
        lambda: datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc),
    )
    task = _add(tmp_path, "Orphan blocked")
    ledger = work_cmd._read_task_ledger(tmp_path)
    edges_mod.add_edge_to_ledger(
        ledger,
        source="missing-blocker-id",
        target=task["id"],
        edge_type="blocks",
        allow_missing=True,
    )
    work_cmd._write_task_ledger(tmp_path, ledger)
    assert work_cmd.ready(target=tmp_path, explain=True, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert task["id"] in {item["id"] for item in payload["ready"]}
    assert payload["orphan_count"] >= 1


def test_task_add_deps_and_graph_atomic(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    clock = {"n": 0}

    def _now():
        clock["n"] += 1
        return datetime(2026, 8, 9, 13, 0, clock["n"], tzinfo=timezone.utc)

    monkeypatch.setattr(work_cmd.helpers, "_now", _now)
    upstream = _add(tmp_path, "Upstream")
    assert (
        work_cmd.task_add(
            target=tmp_path,
            text="Depends on upstream",
            deps=[f"blocks:{upstream['id']}", f"discovered-from:{upstream['id']}"],
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "edges: 2" in out

    plan = {
        "nodes": [
            {"key": "one", "text": "Graph one", "extra_ignored": True},
            {"key": "two", "text": "Graph two"},
        ],
        "edges": [{"source": "one", "target": "two", "type": "blocks"}],
    }
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan))
    assert work_cmd.task_add(target=tmp_path, graph=plan_path, dry_run=True, json_output=True) == 0
    dry = json.loads(capsys.readouterr().out)
    assert dry["dry_run"] is True
    assert dry["key_map"]["one"] is None
    assert any("unknown node field: extra_ignored" == w for w in dry["warnings"])

    assert work_cmd.task_add(target=tmp_path, graph=plan_path, json_output=True) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["dry_run"] is False
    assert applied["key_map"]["one"]
    assert applied["key_map"]["two"]
    assert len(applied["edges"]) == 1

    # Cycle graph is rejected before write.
    bad = {
        "nodes": [{"key": "a", "text": "A"}, {"key": "b", "text": "B"}],
        "edges": [
            {"source": "a", "target": "b", "type": "blocks"},
            {"source": "b", "target": "a", "type": "blocks"},
        ],
    }
    bad_path = tmp_path / "bad.json"
    bad_path.write_text(json.dumps(bad))
    before = work_cmd._read_task_ledger(tmp_path)
    before_count = len(before["tasks"])
    assert work_cmd.task_add(target=tmp_path, graph=bad_path, json_output=True) == 2
    err = json.loads(capsys.readouterr().out)
    assert err["reason"] == edges_mod.REASON_DEPENDENCY_CYCLE
    after = work_cmd._read_task_ledger(tmp_path)
    assert len(after["tasks"]) == before_count


def test_close_parent_refuses_open_children_unless_forced(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd.helpers,
        "_now",
        lambda: datetime(2026, 8, 9, 14, 0, 0, tzinfo=timezone.utc),
    )
    parent = _add(tmp_path, "Parent epic")
    child = _add(tmp_path, "Open child")
    assert (
        work_cmd.task_edge_add(target=tmp_path, edge_type="parent-child", source=parent["id"], target_id=child["id"])
        == 0
    )
    capsys.readouterr()
    assert work_cmd.task_done(target=tmp_path, task_id=parent["id"], json_output=True) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["reason"] == edges_mod.REASON_OPEN_CHILDREN
    assert work_cmd.task_done(target=tmp_path, task_id=parent["id"], force=True, json_output=True) == 0
    forced = json.loads(capsys.readouterr().out)
    assert forced["status"] == "done"
    assert forced["forced"] is True


def test_close_reports_completable_parents(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd.helpers,
        "_now",
        lambda: datetime(2026, 8, 9, 15, 0, 0, tzinfo=timezone.utc),
    )
    parent = _add(tmp_path, "Parent")
    child = _add(tmp_path, "Only child")
    assert (
        work_cmd.task_edge_add(target=tmp_path, edge_type="parent-child", source=parent["id"], target_id=child["id"])
        == 0
    )
    capsys.readouterr()
    assert work_cmd.task_done(target=tmp_path, task_id=child["id"], json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert parent["id"] in {item["id"] for item in payload["completable_parents"]}


def test_import_promote_applies_proposed_edges(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd.helpers,
        "_now",
        lambda: datetime(2026, 8, 9, 16, 0, 0, tzinfo=timezone.utc),
    )
    upstream = _add(tmp_path, "Existing upstream")
    assert (
        work_cmd.import_add(
            target=tmp_path,
            text="Imported with edge",
            kind="task",
            source="manual",
            metadata=["confidence=high"],
        )
        == 0
    )
    imports = work_cmd._read_imports(tmp_path)
    assert len(imports) == 1
    imports[0]["metadata"] = {
        **(imports[0].get("metadata") if isinstance(imports[0].get("metadata"), dict) else {}),
        "proposed_edges": [{"type": "blocks", "source": upstream["id"], "target": "self"}],
    }
    work_cmd._write_imports(tmp_path, imports)
    import_id = imports[0]["id"]
    assert work_cmd.import_promote(target=tmp_path, import_id=import_id) == 0
    out = capsys.readouterr().out
    task_id = out.split("task: ", 1)[1].splitlines()[0]
    ledger = work_cmd._read_task_ledger(tmp_path)
    edges = edges_mod.edges_for_task(ledger, task_id)
    assert any(edge["type"] == "blocks" and edge["source"] == upstream["id"] for edge in edges)


def test_tasks_and_next_json_include_ready_view(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd.helpers,
        "_now",
        lambda: datetime(2026, 8, 9, 17, 0, 0, tzinfo=timezone.utc),
    )
    a = _add(tmp_path, "Ready task")
    b = _add(tmp_path, "Blocked task")
    assert work_cmd.task_edge_add(target=tmp_path, edge_type="blocks", source=a["id"], target_id=b["id"]) == 0
    capsys.readouterr()
    assert work_cmd.tasks(target=tmp_path, json_output=True) == 0
    tasks_payload = json.loads(capsys.readouterr().out)
    assert "edges" in tasks_payload
    assert tasks_payload["ready"]["ready_count"] == 1
    assert work_cmd.next(target=tmp_path, json_output=True) == 0
    next_payload = json.loads(capsys.readouterr().out)
    assert next_payload["task_id"] == a["id"]
    assert next_payload["ready"]["ready_count"] == 1


def test_legacy_v1_ledger_migrates_edges_on_read(tmp_path):
    _init_git_repo(tmp_path)
    path = tmp_path / ".brigade" / "work" / "tasks.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "tasks": [
                    {
                        "id": "legacy-1",
                        "text": "Legacy pending",
                        "status": "pending",
                        "source": "manual",
                        "type": "task",
                        "priority": "normal",
                        "acceptance": [],
                        "created_at": "2026-01-01T00:00:00+00:00",
                        "updated_at": "2026-01-01T00:00:00+00:00",
                    }
                ],
            }
        )
    )
    ledger = work_cmd._read_task_ledger(tmp_path)
    assert ledger["version"] == 2
    assert ledger["edges"] == []
    assert work_cmd._ready_tasks(tmp_path)[0]["id"] == "legacy-1"


def test_self_edge_and_unknown_endpoints_rejected(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd.helpers,
        "_now",
        lambda: datetime(2026, 8, 9, 18, 0, 0, tzinfo=timezone.utc),
    )
    a = _add(tmp_path, "Alone")
    assert (
        work_cmd.task_edge_add(target=tmp_path, edge_type="blocks", source=a["id"], target_id=a["id"], json_output=True)
        == 2
    )
    assert json.loads(capsys.readouterr().out)["reason"] == edges_mod.REASON_SELF_EDGE
    assert (
        work_cmd.task_edge_add(
            target=tmp_path, edge_type="blocks", source=a["id"], target_id="missing", json_output=True
        )
        == 2
    )
    assert json.loads(capsys.readouterr().out)["reason"] == edges_mod.REASON_UNKNOWN_TARGET


def test_stable_ready_ordering(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    times = iter(
        [
            datetime(2026, 8, 9, 10, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 8, 9, 11, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc),
        ]
    )
    monkeypatch.setattr(work_cmd.helpers, "_now", lambda: next(times))
    first = _add(tmp_path, "First")
    second = _add(tmp_path, "Second")
    third = _add(tmp_path, "Third")
    assert work_cmd.ready(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [item["id"] for item in payload["ready"]] == [first["id"], second["id"], third["id"]]


def test_concurrent_edge_writes_do_not_lose_dependency_edges(tmp_path, monkeypatch):
    _init_git_repo(tmp_path)
    clock = {"n": 0}

    def _now():
        clock["n"] += 1
        return datetime(2026, 8, 9, 20, 0, clock["n"], tzinfo=timezone.utc)

    monkeypatch.setattr(work_cmd.helpers, "_now", _now)
    hub = _add(tmp_path, "Hub")
    targets = [_add(tmp_path, f"Target {index}") for index in range(12)]
    worker_count = len(targets)
    ready = threading.Barrier(worker_count)
    errors: list[str] = []

    def add_edge(target_task):
        ready.wait()
        rc = work_cmd.task_edge_add(
            target=tmp_path,
            edge_type="blocks",
            source=hub["id"],
            target_id=target_task["id"],
        )
        if rc != 0:
            errors.append(f"{target_task['id']}: rc={rc}")

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        list(executor.map(add_edge, targets))

    assert errors == []
    ledger = work_cmd._read_task_ledger(tmp_path)
    edge_pairs = {
        (edge["source"], edge["target"], edge["type"]) for edge in ledger.get("edges", []) if isinstance(edge, dict)
    }
    expected = {(hub["id"], target["id"], "blocks") for target in targets}
    assert expected <= edge_pairs
