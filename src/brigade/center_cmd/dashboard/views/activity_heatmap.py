"""Activity heatmap dashboard view."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from brigade import config
from brigade.center_cmd.dashboard import data
from brigade.center_cmd.dashboard import render as html

NAME = "activity"
TITLE = "Activity"
ORDER = 6

_WEEKS = 52
_DAYS_PER_WEEK = 7
_GRID_DAYS = _WEEKS * _DAYS_PER_WEEK
_WINDOW_DAYS = _GRID_DAYS
_ACCENT = (0, 102, 204)
_OUT_OF_WINDOW_COLOR = "#f8f8f8"
_ZERO_IN_WINDOW_COLOR = "#e6f0fa"
_SHADE_RATIOS = (0.25, 0.5, 0.75, 1.0)


def fetch(target: Path) -> dict:
    # Request the full retained verify-run history (default CLI limit is 20).
    limit = str(config.DEFAULT_VERIFY_RUNS_KEEP)
    return data.run_json(target, ["work", "verify", "runs", "--limit", limit])


def _today_utc() -> date:
    return datetime.now(timezone.utc).date()


def _parse_day(value: object) -> date | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).date()


def _bucket_counts(runs: list[object]) -> dict[date, int]:
    counts: dict[date, int] = {}
    for run in runs:
        if not isinstance(run, dict):
            continue
        ts = run.get("started_at") or run.get("completed_at")
        day = _parse_day(ts)
        if day is None:
            continue
        counts[day] = counts.get(day, 0) + 1
    return counts


def _window_bounds(end: date) -> tuple[date, date, date]:
    window_start = end - timedelta(days=_WINDOW_DAYS - 1)
    days_until_saturday = 6 - ((end.weekday() + 1) % 7)
    grid_end = end + timedelta(days=days_until_saturday)
    grid_start = grid_end - timedelta(days=_GRID_DAYS - 1)
    return window_start, end, grid_start


def _mix_accent(ratio: float) -> str:
    def mix(channel: int) -> int:
        return round(255 + (channel - 255) * ratio)

    red, green, blue = _ACCENT
    return f"#{mix(red):02x}{mix(green):02x}{mix(blue):02x}"


def _activity_level(count: int, max_count: int) -> int:
    """Bucket a day's run count into shade level 1..len(_SHADE_RATIOS)."""
    steps = len(_SHADE_RATIOS)
    if max_count <= 1:
        return steps
    scaled = (count - 1) / (max_count - 1)
    return min(steps, 1 + int(scaled * (steps - 1) + 0.999999))


def _stylesheet(nonce: str) -> str:
    """Emit the heatmap palette as a nonce'd stylesheet.

    The dashboard CSP sets `style-src 'nonce-...'` with no `unsafe-inline`, so
    inline `style=` attributes are dropped by the browser and every cell would
    render blank. Shades ship as classes in a nonced <style> block instead.
    """
    rules = [
        f".activity-cell.activity-out {{ background-color: {_OUT_OF_WINDOW_COLOR}; }}",
        f".activity-cell.activity-l0 {{ background-color: {_ZERO_IN_WINDOW_COLOR}; }}",
    ]
    for index, ratio in enumerate(_SHADE_RATIOS, start=1):
        rules.append(f".activity-cell.activity-l{index} {{ background-color: {_mix_accent(ratio)}; }}")
    rules.append(".activity-cell { width: 0.75rem; height: 0.75rem; padding: 0; }")
    return f'<style nonce="{html.esc(nonce)}">{"".join(rules)}</style>'


def render(payload: dict, nonce: str) -> str:
    if payload.get("error"):
        return html.error_panel(TITLE, str(payload["error"]))

    runs = payload.get("runs")
    if not isinstance(runs, list) or not runs:
        return html.panel(html.esc(TITLE), f"<p>{html.esc('Nothing here.')}</p>")

    counts = _bucket_counts(runs)
    window_end = _today_utc()
    window_start, window_end, grid_start = _window_bounds(window_end)
    in_window_counts = [count for day, count in counts.items() if window_start <= day <= window_end]
    max_count = max(in_window_counts) if in_window_counts else 0

    body_rows: list[str] = []
    for dow in range(_DAYS_PER_WEEK):
        cells: list[str] = []
        for week in range(_WEEKS):
            day = grid_start + timedelta(days=week * _DAYS_PER_WEEK + dow)
            if day < window_start or day > window_end:
                shade = "activity-out"
                title = f"{day.isoformat()}: out of range"
            else:
                count = counts.get(day, 0)
                if count == 0:
                    shade = "activity-l0"
                    title = f"{day.isoformat()}: 0 runs"
                else:
                    shade = f"activity-l{_activity_level(count, max_count)}"
                    suffix = "run" if count == 1 else "runs"
                    title = f"{day.isoformat()}: {count} {suffix}"
            cells.append(f'<td class="activity-cell {shade}" title="{html.esc(title)}"></td>')
        body_rows.append(f"<tr>{''.join(cells)}</tr>")

    table = f'<table class="data-table activity-heatmap"><tbody>{"".join(body_rows)}</tbody></table>'
    return html.panel(html.esc(TITLE), f"{_stylesheet(nonce)}{table}")
