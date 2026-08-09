from datetime import date
from pathlib import Path

from brigade.center_cmd.dashboard.views import activity_heatmap


def test_fetch_uses_verify_runs_json(monkeypatch, tmp_path):
    expected = {"runs": []}
    calls: list[tuple[Path, list[str]]] = []

    def fake_run_json(target: Path, args: list[str]) -> dict:
        calls.append((target, args))
        return expected

    monkeypatch.setattr("brigade.center_cmd.dashboard.data.run_json", fake_run_json)

    assert activity_heatmap.fetch(tmp_path) == expected
    assert calls == [(tmp_path, ["work", "verify", "runs", "--limit", "50"])]


def test_render_builds_year_grid_with_titles(monkeypatch):
    # Tuesday anchor so the Sunday-aligned grid has leading out-of-window days.
    anchor = date(2026, 2, 3)
    monkeypatch.setattr(activity_heatmap, "_today_utc", lambda: anchor)

    payload = {
        "runs": [
            {"started_at": "2026-01-15T09:00:00Z"},
            {"started_at": "2026-01-15T18:00:00Z"},
            {"started_at": "2026-01-20T12:00:00Z"},
        ]
    }

    fragment = activity_heatmap.render(payload, "test-nonce")

    assert fragment.count('class="activity-cell ') == 52 * 7
    assert 'title="2026-01-15: 2 runs"' in fragment
    assert 'title="2026-01-20: 1 run"' in fragment
    assert 'title="2026-01-16: 0 runs"' in fragment
    assert "out of range" in fragment

    # A dormant day inside the window must be visually distinct from a cell
    # outside the window, and both from a day that saw runs.
    assert 'class="activity-cell activity-l0"' in fragment
    assert 'class="activity-cell activity-out"' in fragment
    assert 'class="activity-cell activity-l4"' in fragment
    assert f".activity-cell.activity-l0 {{ background-color: {activity_heatmap._ZERO_IN_WINDOW_COLOR}; }}" in fragment
    assert f".activity-cell.activity-out {{ background-color: {activity_heatmap._OUT_OF_WINDOW_COLOR}; }}" in fragment


def test_render_shades_via_nonced_stylesheet_not_inline_styles(monkeypatch):
    """The dashboard CSP has no `unsafe-inline`, so shades must ride a nonce."""
    monkeypatch.setattr(activity_heatmap, "_today_utc", lambda: date(2026, 2, 3))
    payload = {"runs": [{"started_at": "2026-01-15T09:00:00Z"}]}

    fragment = activity_heatmap.render(payload, "n0nce-value")

    assert '<style nonce="n0nce-value">' in fragment
    assert 'style="' not in fragment
    assert "background-color" not in fragment.split("</style>", 1)[1]


def test_window_includes_the_current_day_in_its_sunday_aligned_grid():
    anchor = date(2026, 2, 3)

    window_start, window_end, grid_start = activity_heatmap._window_bounds(anchor)

    assert grid_start.weekday() == 6
    assert window_start <= anchor <= window_end
    assert grid_start <= anchor < grid_start + activity_heatmap._GRID_DAYS * (date.resolution)


def test_render_error_and_empty_payloads_degrade_safely():
    assert "boom" in activity_heatmap.render({"error": "boom"}, "unused")

    assert "Nothing here." in activity_heatmap.render({}, "unused")
    assert "Nothing here." in activity_heatmap.render({"runs": []}, "unused")


def test_render_skips_malformed_timestamps_without_raising(monkeypatch):
    anchor = date(2026, 1, 31)
    monkeypatch.setattr(activity_heatmap, "_today_utc", lambda: anchor)

    payload = {
        "runs": [
            {"started_at": "not-a-timestamp"},
            {"completed_at": ""},
            {"started_at": "2026-01-10T08:00:00Z"},
        ]
    }

    fragment = activity_heatmap.render(payload, "unused")

    assert fragment.count('class="activity-cell ') == 52 * 7
    assert 'title="2026-01-10: 1 run"' in fragment
