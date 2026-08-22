import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from brigade.center_cmd.dashboard.views import run_timeline


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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


def test_docstring_answers_operator_question():
    assert "what verification ran recently" in run_timeline.__doc__.lower()
    assert "did it pass" in run_timeline.__doc__.lower()
    assert "command" in run_timeline.__doc__.lower()


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
    assert "dispatch" in fragment
    assert "read-only" in fragment
    assert "Skipped 2 invalid run directories." in fragment
    assert "build the &lt;thing&gt;" in fragment
    assert "<thing>" not in fragment
    # Primary column is task + chip; Run ID demoted into a details expander.
    assert 'class="mo-chip mo-chip-fail">fail</span> build the &lt;thing&gt;' in fragment
    assert "<details><summary>Run ID</summary><code>20260101-090000-aaaa</code></details>" in fragment
    first_row = re.search(r"<tr data-mo-row=\"1\">.*?</tr>", fragment, re.DOTALL)
    assert first_row is None  # brigade panel rows carry no pager hook
    assert fragment.index("build the &lt;thing&gt;") < fragment.index("<summary>Run ID</summary>")


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
    for heading in ("Command", "Exit code", "Duration", "Timestamp", "Receipt"):
        assert f"<th>{heading}</th>" in fragment
    assert "<details>" in fragment
    assert "<summary>Receipt JSON</summary>" in fragment
    # Primary column is command + chip; Run ID demoted into its own expander.
    assert 'class="mo-chip mo-chip-pass">pass</span> fake check &amp;lt;safe&amp;gt;' in fragment
    assert "<details><summary>Run ID</summary><code>run-late</code></details>" in fragment
    assert "&amp;lt;safe&amp;gt;" in fragment
    assert "Receipt truncated at 4000 characters." in fragment
    assert "x" * 4_001 not in fragment


def test_render_error_and_empty_payloads_degrade_safely():
    fragment = run_timeline.render({"verify": {"error": "boom"}, "brigade": {}}, "unused")
    assert "boom" in fragment

    assert "Nothing here." in run_timeline.render({}, "unused")
    assert "Nothing here." in run_timeline.render({"verify": {"runs": []}, "brigade": {"runs": []}}, "unused")


def test_render_paginates_with_prior_next_like_memory_inventory(monkeypatch):
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


def test_summary_strip_counts_week_runs_failures_and_last_run(monkeypatch):
    now = datetime(2026, 8, 21, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(run_timeline, "_now", lambda: now)
    week_run = now - timedelta(hours=3)
    old_run = now - timedelta(days=14)
    payload = {
        "brigade": {
            "schema": "brigade.runs-list.v1",
            "runs": [
                {
                    "run_id": "b-week",
                    "status": "failed",
                    "task": "week task",
                    "started_at": _iso(week_run),
                },
                {
                    "run_id": "b-old",
                    "status": "completed",
                    "task": "old task",
                    "started_at": _iso(old_run),
                },
            ],
        },
        "verify": {
            "runs": [
                {
                    "run_id": "v-week",
                    "status": "completed",
                    "started_at": _iso(now - timedelta(minutes=10)),
                    "commands": [{"command": "fake check", "exit_code": 0}],
                }
            ]
        },
    }

    fragment = run_timeline.render(payload, "unused")

    assert "2 runs this week, 1 failed, last run 10m ago." in fragment


def test_summary_strip_counts_unparseable_stamps_as_unknown_age(monkeypatch):
    now = datetime(2026, 8, 21, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(run_timeline, "_now", lambda: now)
    payload = {
        "brigade": {"schema": "brigade.runs-list.v1", "runs": []},
        "verify": {"runs": [{"run_id": "v-bad", "status": "completed", "started_at": "not-a-timestamp"}]},
    }

    fragment = run_timeline.render(payload, "unused")

    assert "0 runs this week, 0 failed, last run unknown age." in fragment
    assert "<td>unknown age</td>" in fragment


def test_relative_time_renders_hours_ago_and_future_skew_as_just_now(monkeypatch):
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(run_timeline, "_now", lambda: now)
    assert run_timeline._relative_time(None) == "unknown age"
    assert run_timeline._relative_time("garbage") == "unknown age"
    hours_ago = _iso(now - timedelta(hours=3))
    minutes_ago = _iso(now - timedelta(minutes=5))
    days_ago = _iso(now - timedelta(days=3))
    future = _iso(now + timedelta(minutes=30))
    assert run_timeline._relative_time(hours_ago) == "3h ago"
    assert run_timeline._relative_time(minutes_ago) == "5m ago"
    assert run_timeline._relative_time(days_ago) == "3d ago"
    # Future stamps (clock skew) render as just now.
    assert run_timeline._relative_time(future) == "just now"


def test_render_timestamps_use_relative_time_in_rows(monkeypatch):
    now = datetime(2026, 8, 21, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(run_timeline, "_now", lambda: now)
    payload = {
        "brigade": {"schema": "brigade.runs-list.v1", "runs": []},
        "verify": {
            "runs": [
                {
                    "run_id": "v-rel",
                    "status": "completed",
                    "started_at": _iso(now - timedelta(hours=2)),
                    "commands": [{"command": "fake check", "exit_code": 0}],
                }
            ]
        },
    }

    fragment = run_timeline.render(payload, "unused")

    assert "<td>2h ago</td>" in fragment
    assert "<td>2026" not in fragment
