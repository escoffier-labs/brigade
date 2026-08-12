"""Dispatch-wave swim-lane dashboard view.

Renders parallel-safe waves from the additive ready JSON shape introduced by
#803 (``waves``, ``wave_count``, ``partition_mode``, …). When that flag is not
yet on main, ``work_graph.fetch_ready`` leaves a seam and this view shows an
explicit unavailable panel plus the unpartitioned ready set as a single lane
preview — never invents a partition client-side.
"""

from __future__ import annotations

from pathlib import Path

from brigade.center_cmd.dashboard import render as html
from brigade.center_cmd.dashboard import work_graph as wg

NAME = "waves"
TITLE = "Waves"
ORDER = 9


def fetch(target: Path) -> dict:
    return wg.fetch_ready(target)


def render(payload: dict, nonce: str) -> str:
    if payload.get("error"):
        return html.error_panel(TITLE, str(payload["error"]))

    waves = payload.get("waves")
    has_waves = isinstance(waves, list)

    parts = [_stylesheet(nonce)]
    if has_waves:
        parts.append(_partition_summary(payload))
        parts.append(_swim_lanes(waves))
    else:
        parts.append(_unavailable_panel(payload))
        parts.append(_preview_lane(payload))
    return "".join(parts)


def _unavailable_panel(payload: dict) -> str:
    ready_count = payload.get("ready_count")
    if ready_count is None:
        ready = payload.get("ready")
        ready_count = len(ready) if isinstance(ready, list) else 0
    message = (
        "Dispatch waves are not in this ready JSON yet. "
        "They appear when brigade work ready --parallel-safe is available "
        "(PR #803). Showing the unpartitioned ready set as a single lane."
    )
    detail = f"{ready_count} ready task(s)."
    return html.panel(
        html.esc("Waves unavailable"),
        f"<p>{html.esc(message)}</p><p>{html.esc(detail)}</p>",
    )


def _partition_summary(payload: dict) -> str:
    degraded = payload.get("partition_degraded") is True
    mode = payload.get("partition_mode") or "-"
    reason = payload.get("partition_degraded_reason") or ""
    rows = [
        [html.esc("Wave count"), html.esc(payload.get("wave_count", len(payload.get("waves") or [])))],
        [html.esc("Partition mode"), html.esc(mode)],
        [html.esc("Degraded"), html.esc("yes" if degraded else "no")],
    ]
    if degraded and reason:
        rows.append([html.esc("Degrade reason"), html.esc(reason)])
    return html.panel(
        html.esc("Parallel-safe partition"),
        html.table([html.esc("Signal"), html.esc("Value")], rows),
    )


def _preview_lane(payload: dict) -> str:
    ready = payload.get("ready")
    tasks = [item for item in ready if isinstance(item, dict)] if isinstance(ready, list) else []
    wave = {
        "index": 0,
        "exclusive": False,
        "task_ids": [str(item.get("id") or "") for item in tasks],
        "tasks": tasks,
    }
    return html.panel(
        html.esc("Ready set (unpartitioned)"),
        _lane_markup(wave, preview=True),
    )


def _swim_lanes(waves: list) -> str:
    lanes = []
    for wave in waves:
        if not isinstance(wave, dict):
            continue
        lanes.append(_lane_markup(wave, preview=False))
    if not lanes:
        return html.panel(html.esc(TITLE), f"<p>{html.esc('Nothing here.')}</p>")
    return html.panel(html.esc(TITLE), f'<div class="wg-swimlanes">{"".join(lanes)}</div>')


def _lane_markup(wave: dict, *, preview: bool) -> str:
    index = wave.get("index", 0)
    exclusive = wave.get("exclusive") is True
    tasks = wave.get("tasks")
    if not isinstance(tasks, list):
        task_ids = wave.get("task_ids") if isinstance(wave.get("task_ids"), list) else []
        tasks = [{"id": task_id} for task_id in task_ids if isinstance(task_id, str)]

    chips = []
    for item in tasks:
        if not isinstance(item, dict):
            continue
        task_id = str(item.get("id") or "")
        title = str(item.get("text") or item.get("title") or "")
        tip = task_id
        label = task_id if len(task_id) <= 22 else f"{task_id[:10]}…{task_id[-6:]}"
        chips.append(
            f'<li class="wg-chip" title="{html.esc(tip)}">'
            f'<span class="wg-chip-id">{html.esc(label)}</span>'
            f'<span class="wg-chip-title">{html.esc(_clip(title))}</span>'
            f"</li>"
        )
    if not chips:
        chips.append(f'<li class="wg-chip wg-chip-empty">{html.esc("empty wave")}</li>')

    exclusive_mark = ' data-exclusive="1"' if exclusive else ""
    lane_class = "wg-lane wg-lane-preview" if preview else "wg-lane"
    if exclusive:
        lane_class += " wg-lane-exclusive"
    heading = "Preview lane" if preview else f"Wave {index}"
    if exclusive:
        heading = f"{heading} (exclusive)"
    return (
        f'<section class="{lane_class}"{exclusive_mark}>'
        f'<h3 class="wg-lane-title">{html.esc(heading)}</h3>'
        f'<ol class="wg-lane-tasks">{"".join(chips)}</ol>'
        f"</section>"
    )


def _clip(text: str, limit: int = 40) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) <= limit:
        return cleaned
    return f"{cleaned[: limit - 1]}…"


def _stylesheet(nonce: str) -> str:
    return f"""<style nonce="{html.esc(nonce)}">
.wg-swimlanes {{
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
}}
.wg-lane {{
  border: 1px solid #c5d4e8;
  border-left: 4px solid #0066cc;
  background: #f5f9fd;
  padding: 0.75rem 1rem;
}}
.wg-lane-exclusive {{
  border-left-color: #8b0000;
  background: #fdf5f5;
}}
.wg-lane-preview {{
  border-left-color: #888;
  background: #f7f7f7;
}}
.wg-lane-title {{
  margin: 0 0 0.5rem;
  font-size: 0.95rem;
}}
.wg-lane-tasks {{
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
}}
.wg-chip {{
  border: 1px solid #bbb;
  border-radius: 0.2rem;
  background: #fff;
  padding: 0.35rem 0.55rem;
  min-width: 8rem;
  max-width: 16rem;
}}
.wg-chip-empty {{
  color: #666;
  font-style: italic;
}}
.wg-chip-id {{
  display: block;
  font-weight: 700;
  font-size: 0.8rem;
}}
.wg-chip-title {{
  display: block;
  font-size: 0.8rem;
  color: #333;
}}
</style>"""
