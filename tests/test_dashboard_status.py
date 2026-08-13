from pathlib import Path

from brigade.center_cmd.dashboard.views import status_grid


def test_fetch_uses_work_brief_json(monkeypatch, tmp_path):
    expected = {"handoff_issues": {"count": 2}}
    calls: list[tuple[Path, list[str], float | None]] = []

    def fake_run_json(target: Path, args: list[str], *, timeout: float | None = None) -> dict:
        calls.append((target, args, timeout))
        return expected

    monkeypatch.setattr("brigade.center_cmd.dashboard.data.run_json", fake_run_json)

    assert status_grid.fetch(tmp_path) == expected
    assert calls == [(tmp_path, ["work", "brief"], 60.0)]


def test_render_displays_status_signals():
    payload = {
        "handoff_issues": {"count": 2, "total_count": 5},
        "memory_care": {"issue_count": 3},
        "outcome_loop": {
            "verify_run_count": 12,
            "record_count": 9,
            "eligible_receipt_count": 7,
        },
        "pending_tasks": [{"id": "task-a"}, {"id": "task-b"}],
        "inbox_hygiene": {"issue_count": 1},
    }

    fragment = status_grid.render(payload, "unused")

    assert "Pending handoff issues" in fragment
    assert "Total handoff issues" in fragment
    assert "Memory cards at refresh risk" in fragment
    assert "Latest verify receipt" in fragment
    assert "Latest verify signal" in fragment
    assert "Verify runs" in fragment
    assert "Outcome records" in fragment
    assert "Open work-inbox items" in fragment
    assert "Inbox hygiene issues" in fragment
    for value in ("2", "5", "3", "12", "9", "7", "1"):
        assert f">{value}<" in fragment
    assert ">-<" in fragment


def test_render_error_and_empty_payloads_degrade_safely():
    assert "boom" in status_grid.render({"error": "boom"}, "unused")

    fragment = status_grid.render({}, "unused")
    assert "Nothing here." in fragment
