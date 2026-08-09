from datetime import datetime, timezone
from pathlib import Path

from brigade.center_cmd.dashboard.views import task_claims
from brigade.center_cmd.dashboard import work_graph as wg


def test_fetch_uses_tasks_all_json(monkeypatch, tmp_path):
    expected = {"tasks": []}
    calls: list[tuple[Path, list[str]]] = []

    def fake_run_json(target: Path, args: list[str]) -> dict:
        calls.append((target, list(args)))
        return expected

    monkeypatch.setattr("brigade.center_cmd.dashboard.data.run_json", fake_run_json)
    assert task_claims.fetch(tmp_path) == expected
    assert calls == [(tmp_path, ["work", "tasks", "--all"])]


def test_render_claim_and_footprint_rows_with_stale_flags(monkeypatch):
    fixed_now = datetime(2026, 8, 9, 18, 0, 0, tzinfo=timezone.utc)

    class _FixedDateTime:
        @staticmethod
        def now(tz=None):
            return fixed_now

    monkeypatch.setattr(task_claims, "datetime", _FixedDateTime)

    payload = {
        "tasks": [
            {
                "id": "task-fresh",
                "text": "Fresh claim",
                "status": "in_progress",
                "assignee": "alice",
                "claim": {
                    "claim_id": "claim-fresh",
                    "actor": "alice",
                    "claimed_at": "2026-08-09T12:00:00+00:00",
                    "item_revision": 1,
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
                "text": "Old claim",
                "status": "in_progress",
                "claim": {
                    "claim_id": "claim-old",
                    "actor": "bob",
                    "claimed_at": "2026-08-01T12:00:00+00:00",
                    "item_revision": 1,
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
                "id": "task-done",
                "text": "Done without reconcile",
                "status": "completed",
                "metadata": {
                    "footprint": {
                        "phase": "refined",
                        "files": ["src/b.py"],
                        "symbol_ids": [],
                        "snapshot_hash": "def",
                    }
                },
            },
            {
                "id": "task-degraded",
                "text": "Degraded predicted",
                "status": "pending",
                "metadata": {
                    "footprint": {
                        "phase": "predicted",
                        "files": [],
                        "symbol_ids": [],
                        "snapshot_hash": "",
                        "degraded": True,
                        "degraded_reason": "graphtrail binary not found",
                    }
                },
            },
        ]
    }

    fragment = task_claims.render(payload, "claim-nonce")

    assert '<style nonce="claim-nonce">' in fragment
    assert "task-fresh" in fragment
    assert "alice / claim-fresh" in fragment
    assert "refined" in fragment
    assert "stale-claim" in fragment
    assert "still predicted after claim" in fragment
    assert "not reconciled (refined)" in fragment
    assert "degraded: graphtrail binary not found" in fragment
    assert "wg-flag-stale" in fragment
    assert "wg-flag-ok" in fragment
    assert "Stale claims" in fragment
    assert "Stale footprints" in fragment


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


def test_render_escapes_hostile_task_fields():
    payload = {
        "tasks": [
            {
                "id": "evil",
                "text": "<script>alert(1)</script>",
                "status": "pending",
                "claim": {
                    "claim_id": "<img src=x onerror=alert(2)>",
                    "actor": "<b>bad</b>",
                    "claimed_at": "2026-08-09T12:00:00+00:00",
                },
                "metadata": {
                    "footprint": {
                        "phase": "predicted",
                        "files": [],
                        "symbol_ids": [],
                        "snapshot_hash": "",
                        "degraded": True,
                        "degraded_reason": "<svg onload=alert(3)>",
                    }
                },
            }
        ]
    }

    fragment = task_claims.render(payload, "unused")

    # XSS boundary: no tag may form from attacker text. Escaped substrings
    # like onerror= in text nodes are inert.
    assert "<script" not in fragment
    assert "<img" not in fragment
    assert "<svg" not in fragment
    assert "&lt;b&gt;bad&lt;/b&gt;" in fragment
    assert "&lt;img src=x onerror=alert(2)&gt;" in fragment
    assert "&lt;svg onload=alert(3)&gt;" in fragment

    import re

    for tag in re.findall(r"<[^>]*>", fragment):
        assert "alert(" not in tag
        assert "<script" not in tag
        assert "<img" not in tag
        assert "<svg" not in tag


def test_render_error_and_empty_payloads_degrade_safely():
    assert "boom" in task_claims.render({"error": "boom"}, "unused")
    assert "Nothing here." in task_claims.render({}, "unused")
    assert "Nothing here." in task_claims.render({"tasks": []}, "unused")
