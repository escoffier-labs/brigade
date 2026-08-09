from pathlib import Path

from brigade.center_cmd.dashboard.views import run_timeline


def test_fetch_uses_verify_runs_json(monkeypatch, tmp_path):
    expected = {"runs": []}
    calls: list[tuple[Path, list[str]]] = []

    def fake_run_json(target: Path, args: list[str]) -> dict:
        calls.append((target, args))
        return expected

    monkeypatch.setattr("brigade.center_cmd.dashboard.data.run_json", fake_run_json)

    assert run_timeline.fetch(tmp_path) == expected
    assert calls == [(tmp_path, ["work", "verify", "runs"])]


def test_render_lists_runs_newest_first_with_bounded_receipts():
    payload = {
        "runs": [
            {
                "run_id": "run-early",
                "status": "failed",
                "started_at": "2026-01-01T09:00:00Z",
                "duration_seconds": 1.25,
                "commands": [{"command": "fake check", "exit_code": 1}],
            },
            {
                "run_id": "run-late",
                "status": "completed",
                "started_at": "2026-01-02T09:00:00Z",
                "duration_seconds": 2.5,
                "commands": [{"command": "fake check &lt;safe&gt;", "exit_code": 0}],
                "large_field": "x" * 5_000,
            },
        ]
    }

    fragment = run_timeline.render(payload, "unused")

    assert fragment.index("run-late") < fragment.index("run-early")
    for heading in ("Run ID", "Status", "Command", "Exit code", "Duration", "Timestamp"):
        assert f"<th>{heading}</th>" in fragment
    assert "<details>" in fragment
    assert "<summary>Receipt JSON</summary>" in fragment
    assert "&amp;lt;safe&amp;gt;" in fragment
    assert "Receipt truncated at 4000 characters." in fragment
    assert "x" * 4_001 not in fragment


def test_render_error_and_empty_payloads_degrade_safely():
    assert "boom" in run_timeline.render({"error": "boom"}, "unused")

    assert "Nothing here." in run_timeline.render({}, "unused")
    assert "Nothing here." in run_timeline.render({"runs": []}, "unused")
