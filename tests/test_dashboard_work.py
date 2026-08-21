"""Work page: Graph + Waves + Claims + Activity folded into one operator view."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from brigade.center_cmd.dashboard import work_graph as wg
from brigade.center_cmd.dashboard.views import all_views, render_nav, view_by_name
from brigade.center_cmd.dashboard.views import work as work_view


def _fixed_now():
    class _FixedDateTime:
        @staticmethod
        def now(tz=None):
            return datetime(2026, 8, 13, 18, 0, 0, tzinfo=timezone.utc)

    return _FixedDateTime


def _ready_task(task_id: str, title: str) -> dict:
    return {"id": task_id, "text": title, "status": "pending"}


def _exclusive_singles(titles: list[str]) -> dict:
    tasks = [_ready_task(f"task-{index}", title) for index, title in enumerate(titles)]
    waves = []
    for index, task in enumerate(tasks):
        waves.append(
            {
                "index": index,
                "exclusive": True,
                "task_ids": [task["id"]],
                "tasks": [task],
            }
        )
    return {
        "ready_count": len(tasks),
        "blocked_count": 0,
        "cycle_count": 0,
        "ready": tasks,
        "blocked": [],
        "edges": [],
        "parallel_safe": True,
        "waves": waves,
        "wave_count": len(waves),
        "partition_mode": "file_overlap+symbol_impact",
    }


def _payload(*, ready: dict | None = None, tasks: dict | None = None, runs: dict | None = None) -> dict:
    return {
        "ready": ready if ready is not None else {"ready": [], "blocked": [], "edges": []},
        "tasks": tasks if tasks is not None else {"tasks": []},
        "runs": runs if runs is not None else {"runs": []},
    }


def test_work_view_contract_and_nav_replace_folded_pages():
    assert work_view.NAME == "work"
    assert work_view.TITLE == "Work"
    names = [module.NAME for module in all_views()]
    titles = [module.TITLE for module in all_views()]
    assert "work" in names
    assert view_by_name("work") is work_view
    for folded in ("graph", "waves", "claims", "activity"):
        assert folded not in names
    for folded_title in ("Graph", "Waves", "Claims", "Activity"):
        assert folded_title not in titles
    nav = render_nav("work")
    assert ">Work<" in nav
    assert 'href="/view/work"' in nav
    assert ">Graph<" not in nav
    assert ">Waves<" not in nav
    assert ">Claims<" not in nav
    assert 'href="/view/activity"' not in nav
    assert (
        "operator question" in (work_view.__doc__ or "").lower() or "what can run" in (work_view.__doc__ or "").lower()
    )


def test_fetch_combines_ready_tasks_and_runs(monkeypatch, tmp_path):
    calls: list[list[str]] = []

    def fake_run_json(target: Path, args: list[str]) -> dict:
        del target
        calls.append(list(args))
        joined = " ".join(args)
        if args[:2] == ["work", "ready"]:
            return {"ready": [], "ready_count": 0, "waves": []}
        if args[:2] == ["work", "tasks"]:
            return {"tasks": []}
        if args[:3] == ["work", "verify", "runs"]:
            return {"runs": []}
        return {"error": f"unexpected {joined}"}

    monkeypatch.setattr("brigade.center_cmd.dashboard.data.run_json", fake_run_json)
    payload = work_view.fetch(tmp_path)
    assert "ready" in payload
    assert "tasks" in payload
    assert "runs" in payload
    assert any(call[:3] == ["work", "verify", "runs"] and "--limit" in call for call in calls)
    assert any(call[:2] == ["work", "tasks"] for call in calls)
    assert any(call[:2] == ["work", "ready"] for call in calls)


def test_independent_single_task_waves_collapse_to_plain_parallel_list():
    titles = [
        "Mint stable card ids",
        "Repair outcome ledger",
        "Paginate runs view",
        "Fold claims into work",
        "Rewrite activity strip",
    ]
    fragment = work_view.render(_payload(ready=_exclusive_singles(titles)), "work-nonce")

    assert '<style nonce="work-nonce">' in fragment
    assert 'id="ready"' in fragment
    assert "5 tasks are ready and independent" in fragment
    assert "could all run in parallel" in fragment
    assert ">Together<" in fragment
    assert "Ready tasks can start at the same time." in fragment
    for title in titles:
        assert title in fragment
    assert "Wave 0" not in fragment
    assert "Step 1 of 5" not in fragment
    assert "(exclusive)" not in fragment
    assert "file_overlap+symbol_impact" not in fragment.split("<details", 1)[0]
    assert "Parallel-safe partition" not in fragment
    assert 'class="wk-diagram"' not in fragment
    for task_id in (f"task-{index}" for index in range(5)):
        assert task_id in fragment
        assert "<details" in fragment
    assert "stale-footprint" not in fragment


def test_multi_task_wave_keeps_visual_structure():
    payload = _payload(
        ready={
            "ready_count": 3,
            "blocked_count": 0,
            "ready": [
                _ready_task("task-a", "Alpha work"),
                _ready_task("task-b", "Beta work"),
                _ready_task("task-c", "Gamma work"),
            ],
            "blocked": [],
            "edges": [],
            "parallel_safe": True,
            "wave_count": 2,
            "waves": [
                {
                    "index": 0,
                    "exclusive": False,
                    "task_ids": ["task-a", "task-b"],
                    "tasks": [
                        _ready_task("task-a", "Alpha work"),
                        _ready_task("task-b", "Beta work"),
                    ],
                },
                {
                    "index": 1,
                    "exclusive": True,
                    "task_ids": ["task-c"],
                    "tasks": [_ready_task("task-c", "Gamma work")],
                },
            ],
        }
    )
    fragment = work_view.render(payload, "unused")
    assert "Alpha work" in fragment
    assert "Beta work" in fragment
    assert "Gamma work" in fragment
    assert "Step 1 of 2 - these can run together" in fragment
    assert "Step 2 of 2 - one task at a time" in fragment
    assert "Wave 0" not in fragment
    assert ">Together<" in fragment
    assert "Ready tasks cannot all start at the same time." in fragment
    assert "could all run in parallel" not in fragment
    assert 'class="wk-diagram"' not in fragment


def test_conflict_edge_keeps_visual_structure_even_for_single_task_waves():
    payload = _payload(
        ready={
            "ready_count": 1,
            "blocked_count": 1,
            "ready": [_ready_task("task-a", "Ready alpha")],
            "blocked": [
                {
                    "id": "task-b",
                    "title": "Blocked beta",
                    "status": "pending",
                    "blockers": [{"id": "task-a", "status": "pending", "via": ["blocks"]}],
                }
            ],
            "edges": [{"source": "task-a", "target": "task-b", "type": "blocks"}],
            "parallel_safe": True,
            "wave_count": 1,
            "waves": [
                {
                    "index": 0,
                    "exclusive": True,
                    "task_ids": ["task-a"],
                    "tasks": [_ready_task("task-a", "Ready alpha")],
                }
            ],
        }
    )
    fragment = work_view.render(payload, "unused")
    assert "Ready alpha" in fragment
    assert "Blocked beta" in fragment
    assert "Step 1 of 1 - one task at a time" in fragment
    assert "could all run in parallel" not in fragment
    assert "waiting" in fragment.lower() or "blocked" in fragment.lower()
    assert 'class="wk-diagram"' in fragment
    assert "<svg" in fragment
    assert 'data-wk-from="task-a"' in fragment
    assert 'data-wk-to="task-b"' in fragment
    assert "What is blocking what" in fragment
    assert "style=" not in fragment.split("<style", 1)[-1].split("</style>", 1)[-1]


def test_exclusive_single_task_waves_use_plain_step_headings_when_edges_force_lanes():
    titles = [
        "Mint stable card ids",
        "Repair outcome ledger",
        "Paginate runs view",
        "Fold claims into work",
        "Rewrite activity strip",
    ]
    ready = _exclusive_singles(titles)
    ready["edges"] = [{"source": "task-0", "target": "task-1", "type": "blocks"}]
    ready["blocked_count"] = 0
    fragment = work_view.render(_payload(ready=ready), "step-nonce")

    assert '<style nonce="step-nonce">' in fragment
    assert "Step 2 of 5 - one task at a time (blocks step 3)" in fragment
    assert "Step 1 of 5 - one task at a time (blocks step 2)" in fragment
    assert "Step 5 of 5 - one task at a time" in fragment
    assert "Wave 0" not in fragment
    assert "Wave 1" not in fragment
    assert ">Together<" in fragment
    assert "Ready tasks cannot all start at the same time." in fragment
    assert 'class="wk-diagram"' in fragment
    assert 'data-wk-from="task-0"' in fragment
    assert 'data-wk-to="task-1"' in fragment
    for title in titles:
        assert title in fragment


def test_drawn_edges_render_when_waves_are_absent():
    payload = _payload(
        ready={
            "ready_count": 1,
            "blocked_count": 1,
            "ready": [_ready_task("task-a", "Ready alpha")],
            "blocked": [
                {
                    "id": "task-b",
                    "title": "Blocked beta",
                    "status": "pending",
                    "blockers": [{"id": "task-a", "status": "pending", "via": ["blocks"]}],
                }
            ],
            "edges": [{"source": "task-a", "target": "task-b", "type": "blocks"}],
            "parallel_safe": True,
        }
    )
    fragment = work_view.render(payload, "edge-nonce")
    assert '<style nonce="edge-nonce">' in fragment
    assert "Ready alpha" in fragment
    assert "Blocked beta" in fragment
    assert 'class="wk-lane"' not in fragment
    assert "Wave " not in fragment
    assert "Step 1 of" not in fragment
    assert 'class="wk-diagram"' in fragment
    assert "<svg" in fragment
    assert 'data-wk-from="task-a"' in fragment
    assert 'data-wk-to="task-b"' in fragment
    assert 'data-wk-type="blocks"' in fragment
    assert "What is blocking what" in fragment
    assert ">Together<" in fragment
    assert "Ready tasks cannot all start at the same time." in fragment


def test_claims_use_plain_words_not_footprint_jargon(monkeypatch):
    monkeypatch.setattr(work_view, "datetime", _fixed_now())
    payload = _payload(
        tasks={
            "tasks": [
                {
                    "id": "task-fresh",
                    "text": "Fresh claim still moving",
                    "status": "in_progress",
                    "assignee": "alice",
                    "claim": {
                        "claim_id": "claim-fresh",
                        "actor": "alice",
                        "claimed_at": "2026-08-13T12:00:00+00:00",
                    },
                    "metadata": {
                        "footprint": {
                            "phase": "refined",
                            "files": ["src/a.py"],
                            "symbol_ids": ["sym.a"],
                            "snapshot_hash": "abc",
                        }
                    },
                },
                {
                    "id": "task-stale-claim",
                    "text": "Old abandoned claim",
                    "status": "in_progress",
                    "claim": {
                        "claim_id": "claim-old",
                        "actor": "bob",
                        "claimed_at": "2026-08-10T21:00:00+00:00",
                    },
                    "metadata": {
                        "footprint": {
                            "phase": "predicted",
                            "files": [],
                            "symbol_ids": [],
                            "snapshot_hash": "",
                        }
                    },
                },
                {
                    "id": "task-done-missing",
                    "text": "Finished without files",
                    "status": "completed",
                },
            ]
        }
    )
    fragment = work_view.render(payload, "claim-nonce")
    assert 'id="claims"' in fragment
    assert "abandoned" in fragment.lower()
    assert "claimed 69h ago" in fragment or "69h" in fragment
    assert "never recorded what files they touched" in fragment
    assert "Old abandoned claim" in fragment
    assert "Finished without files" in fragment
    assert "stale-footprint" not in fragment
    assert "missing footprint" not in fragment.split("<details", 1)[0]
    assert "stale-claim" not in fragment.split("<details", 1)[0]
    primary = fragment.split("<details", 1)[0]
    assert "claim-old" not in primary
    assert "task-stale-claim" not in primary


def test_recent_runs_strip_links_to_runs_and_skips_heatmap():
    payload = _payload(
        runs={
            "runs": [
                {
                    "run_id": "run-late",
                    "status": "completed",
                    "started_at": "2026-08-13T17:00:00Z",
                    "commands": [{"command": "pytest -q", "exit_code": 0}],
                },
                {
                    "run_id": "run-early",
                    "status": "failed",
                    "started_at": "2026-08-13T09:00:00Z",
                    "commands": [{"command": "ruff check .", "exit_code": 1}],
                },
            ]
        }
    )
    fragment = work_view.render(payload, "unused")
    assert 'id="recent"' in fragment
    assert 'href="/view/runs"' in fragment
    assert "pytest -q" in fragment
    assert "ruff check ." in fragment
    assert "activity-cell" not in fragment
    assert "activity-heatmap" not in fragment
    primary = fragment.split("<details", 1)[0]
    assert "run-late" not in primary


def test_render_escapes_hostile_work_fields():
    payload = _payload(
        ready={
            "ready_count": 1,
            "blocked_count": 0,
            "ready": [
                {
                    "id": "evil",
                    "text": "<script>alert(1)</script><img src=x onerror=alert(2)>",
                    "status": "pending",
                }
            ],
            "blocked": [],
            "edges": [],
            "waves": [
                {
                    "index": 0,
                    "exclusive": True,
                    "tasks": [
                        {
                            "id": "evil",
                            "text": "<script>alert(1)</script><img src=x onerror=alert(2)>",
                        }
                    ],
                }
            ],
        },
        tasks={
            "tasks": [
                {
                    "id": "evil-claim",
                    "text": "<b>bad</b>",
                    "status": "in_progress",
                    "claim": {
                        "claim_id": "<img src=x onerror=alert(2)>",
                        "actor": "<svg onload=alert(3)>",
                        "claimed_at": "2026-08-01T12:00:00+00:00",
                    },
                }
            ]
        },
        runs={
            "runs": [
                {
                    "run_id": "run-evil",
                    "status": "failed",
                    "started_at": "2026-08-13T12:00:00Z",
                    "commands": [{"command": "<script>alert(4)</script>", "exit_code": 1}],
                }
            ]
        },
    )
    fragment = work_view.render(payload, "unused")
    assert "<script" not in fragment
    assert "<img" not in fragment
    assert "<svg" not in fragment
    assert "&lt;script&gt;" in fragment
    assert "&lt;b&gt;bad&lt;/b&gt;" in fragment

    import re

    for tag in re.findall(r"<[^>]*>", fragment):
        assert "alert(" not in tag


def test_render_error_and_empty_payloads_degrade_safely():
    assert "boom" in work_view.render({"ready": {"error": "boom"}, "tasks": {}, "runs": {}}, "unused")
    empty = work_view.render(_payload(), "unused")
    assert 'id="ready"' in empty
    assert 'id="claims"' in empty
    assert 'id="recent"' in empty


def test_footprint_staleness_helpers():
    assert (
        wg.footprint_staleness(
            {"status": "in_progress"},
            {"phase": "predicted", "files": [], "symbol_ids": [], "snapshot_hash": ""},
        )
        == "still predicted after claim"
    )
    assert (
        wg.footprint_staleness(
            {"status": "completed"},
            {"phase": "refined", "files": ["a.py"], "symbol_ids": [], "snapshot_hash": ""},
        )
        == "not reconciled (refined)"
    )
    assert (
        wg.footprint_staleness(
            {"status": "pending"},
            {
                "phase": "predicted",
                "files": [],
                "symbol_ids": [],
                "snapshot_hash": "",
                "degraded": True,
                "degraded_reason": "graphtrail index not found",
            },
        )
        == "degraded: graphtrail index not found"
    )
    assert (
        wg.footprint_staleness(
            {"status": "in_progress"},
            {"phase": "refined", "files": ["a.py"], "symbol_ids": [], "snapshot_hash": ""},
        )
        is None
    )
