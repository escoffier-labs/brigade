import re
from pathlib import Path

from brigade.center_cmd.dashboard.views import run_timeline


def test_fetch_uses_runs_list_and_verify_runs_contracts(monkeypatch, tmp_path):
    brigade_payload = {"schema": "brigade.runs-list.v1", "runs": [], "skipped_invalid": 0}
    verify_payload = {"runs": []}
    calls: list[tuple[str, Path, list[str]]] = []

    def fake_run_json(target: Path, args: list[str]) -> dict:
        calls.append(("target", target, args))
        return verify_payload

    def fake_run_json_cwd(target: Path, args: list[str]) -> dict:
        calls.append(("cwd", target, args))
        return brigade_payload

    monkeypatch.setattr("brigade.center_cmd.dashboard.data.run_json", fake_run_json)
    monkeypatch.setattr("brigade.center_cmd.dashboard.data.run_json_cwd", fake_run_json_cwd)

    payload = run_timeline.fetch(tmp_path)

    assert payload == {"brigade": brigade_payload, "verify": verify_payload}
    styles = {call[0] for call in calls}
    assert styles == {"target", "cwd"}
    cwd_argv = next(call[2] for call in calls if call[0] == "cwd")
    assert cwd_argv[:2] == ["runs", "list"]
    assert "--limit" in cwd_argv
    verify_argv = next(call[2] for call in calls if call[0] == "target")
    assert verify_argv[:3] == ["work", "verify", "runs"]
    assert "--limit" in verify_argv
    limit = int(verify_argv[verify_argv.index("--limit") + 1])
    assert limit >= 50


def test_render_brigade_runs_from_versioned_list_contract():
    payload = {
        "brigade": {
            "schema": "brigade.runs-list.v1",
            "skipped_invalid": 2,
            "runs": [
                {
                    "run_id": "20260101-090000-aaaa",
                    "status": "failed",
                    "task": "build the <thing>",
                    "started_at": "2026-01-01T09:00:00Z",
                    "duration_seconds": 4.5,
                    "failure_phase": "dispatch",
                    "mode": "read-only",
                    "resume_available": True,
                }
            ],
        },
        "verify": {"runs": []},
    }

    fragment = run_timeline.render(payload, "unused")

    assert "Brigade runs" in fragment
    assert "20260101-090000-aaaa" in fragment
    assert "dispatch" in fragment
    assert "read-only" in fragment
    assert "Skipped 2 invalid run directories." in fragment
    assert "build the &lt;thing&gt;" in fragment
    assert "<thing>" not in fragment


def test_render_brigade_runs_error_is_empty_state_with_diagnostic():
    payload = {
        "brigade": {"error": "error: runs directory not found: <root>"},
        "verify": {"runs": []},
    }

    fragment = run_timeline.render(payload, "unused")

    assert "No Brigade runs yet." in fragment
    assert "CLI diagnostic" in fragment
    assert "runs directory not found" in fragment
    assert "<root>" not in fragment


def test_render_lists_verify_runs_newest_first_with_bounded_receipts():
    payload = {
        "brigade": {"schema": "brigade.runs-list.v1", "runs": [], "skipped_invalid": 0},
        "verify": {
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
        },
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
    fragment = run_timeline.render({"verify": {"error": "boom"}, "brigade": {}}, "unused")
    assert "boom" in fragment

    assert "Nothing here." in run_timeline.render({}, "unused")
    assert "Nothing here." in run_timeline.render({"verify": {"runs": []}, "brigade": {"runs": []}}, "unused")


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
    fragment = run_timeline.render({"verify": {"runs": runs}, "brigade": {}}, "run-nonce")
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
