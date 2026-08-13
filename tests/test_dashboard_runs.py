import re
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
    argv = calls[0][1]
    assert argv[:3] == ["work", "verify", "runs"]
    assert "--limit" in argv
    limit = int(argv[argv.index("--limit") + 1])
    assert limit >= 50


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


def test_render_paginates_with_prior_next_like_memory_inventory():
    runs = []
    for index in range(55):
        runs.append(
            {
                "run_id": f"run-{index:04d}",
                "status": "completed" if index % 2 == 0 else "failed",
                "started_at": f"2026-08-01T{index % 24:02d}:00:00Z",
                "duration_seconds": 1,
                "commands": [{"command": f"check-{index:04d}", "exit_code": 0}],
            }
        )
    fragment = run_timeline.render({"runs": runs}, "run-nonce")
    assert '<style nonce="run-nonce">' in fragment or "data-mo-page=" in fragment
    assert 'data-mo-page-size="50"' in fragment
    assert 'data-mo-page="prev"' in fragment
    assert 'data-mo-page="next"' in fragment
    assert "data-mo-page-status" in fragment
    assert "Prior" in fragment
    assert "Next" in fragment
    assert re.search(r'data-mo-page="prev"[^>]*disabled', fragment) is not None
    next_btn = re.search(r'<button[^>]*data-mo-page="next"[^>]*>', fragment)
    assert next_btn is not None
    assert "disabled" not in next_btn.group(0)
    assert 'data-mo-row="1"' in fragment
    assert "check-0000" in fragment
    assert "check-0054" in fragment
