"""Work dashboard view.

Operator question: what can I dispatch, in what order, who owns it, and
what is blocking what?

Folds the former Graph, Waves, Claims, and Activity pages into one surface.
Summary strip in sentences, plain-word tiles, raw IDs in expanders.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from brigade.center_cmd.dashboard import data
from brigade.center_cmd.dashboard import render as html
from brigade.center_cmd.dashboard import work_graph as wg
from brigade.work_cmd import constants as work_constants

NAME = "work"
TITLE = "Work"
ORDER = 6

_RUNS_FETCH_LIMIT = 500
_RECENT_STRIP_LIMIT = 8
_STATUS_GOOD = "#0ca30c"
_STATUS_WARNING = "#fab219"
_STATUS_SERIOUS = "#ec835a"
_TEXT_PRIMARY = "#0b0b0b"
_TEXT_SECONDARY = "#52514e"
_SURFACE = "#fcfcfb"
_BORDER = "rgba(11,11,11,0.10)"


def fetch(target: Path) -> dict:
    return {
        "ready": wg.fetch_ready(target),
        "tasks": wg.fetch_tasks(target),
        "runs": data.run_json(target, ["work", "verify", "runs", "--limit", str(_RUNS_FETCH_LIMIT)]),
    }


def render(payload: dict, nonce: str) -> str:
    ready = payload.get("ready") if isinstance(payload.get("ready"), dict) else {}
    tasks = payload.get("tasks") if isinstance(payload.get("tasks"), dict) else {}
    runs = payload.get("runs") if isinstance(payload.get("runs"), dict) else {}
    now = datetime.now(timezone.utc)
    return "".join(
        [
            _stylesheet(nonce),
            _ready_section(ready),
            _claims_section(tasks, now=now),
            _recent_section(runs),
        ]
    )


def _ready_section(ready: dict) -> str:
    inner: list[str] = []
    if ready.get("error"):
        inner.append(html.error_panel("Ready", str(ready["error"])))
    else:
        summary, tiles, body, debug = _ready_body(ready)
        inner.append(_summary_strip(summary, "What can run"))
        inner.append(html.panel(html.esc("Ready now"), tiles + body + debug))
    return f'<section id="ready">{"".join(inner)}</section>'


def _ready_body(ready: dict) -> tuple[str, str, str, str]:
    ready_items = [item for item in (ready.get("ready") or []) if isinstance(item, dict)]
    blocked_items = [item for item in (ready.get("blocked") or []) if isinstance(item, dict)]
    ready_count = ready.get("ready_count")
    if not isinstance(ready_count, int):
        ready_count = len(ready_items)
    blocked_count = ready.get("blocked_count")
    if not isinstance(blocked_count, int):
        blocked_count = len(blocked_items)
    show_waves = _show_wave_structure(ready)
    summary = _ready_summary(ready_count, blocked_count, show_waves)
    can_run_together = ready_count > 1 and not show_waves and blocked_count == 0
    tiles = _tiles(
        [
            ("Ready", str(ready_count), "good" if ready_count else "warning", "Tasks that can start now."),
            (
                "Blocked",
                str(blocked_count),
                "serious" if blocked_count else "good",
                "Tasks waiting on another task.",
            ),
            (
                "Together",
                "yes" if can_run_together else "no",
                "good" if can_run_together else "warning",
                (
                    "Ready tasks can start at the same time."
                    if can_run_together
                    else "Ready tasks cannot all start at the same time."
                ),
            ),
        ]
    )
    diagram = _dependency_diagram(ready)
    if ready_count == 0 and blocked_count == 0:
        body = f"<p>{html.esc('Nothing is ready to run right now.')}</p>"
    elif show_waves:
        body = _wave_lanes(ready) + _blocked_list(blocked_items) + diagram
    else:
        body = _plain_task_list(ready_items) + _blocked_list(blocked_items) + diagram
    debug = _raw_details("Ready ids and partition", _ready_debug(ready))
    return summary, tiles, body, debug


def _ready_summary(ready_count: int, blocked_count: int, show_waves: bool) -> str:
    if ready_count == 0 and blocked_count == 0:
        return "Nothing is ready to run right now."
    if ready_count > 0 and blocked_count == 0 and not show_waves:
        noun = "task is" if ready_count == 1 else "tasks are"
        parallel = "it can run now" if ready_count == 1 else "they could all run in parallel"
        return f"{ready_count} {noun} ready and independent - {parallel}."
    parts: list[str] = []
    if ready_count:
        noun = "task is" if ready_count == 1 else "tasks are"
        parts.append(f"{ready_count} {noun} ready")
    else:
        parts.append("No tasks are ready")
    if blocked_count:
        wait_noun = "task is" if blocked_count == 1 else "tasks are"
        blocker = "a blocker" if blocked_count == 1 else "blockers"
        parts.append(f"{blocked_count} {wait_noun} waiting on {blocker}")
    return ". ".join(parts) + "."


def _show_wave_structure(ready: dict) -> bool:
    waves = ready.get("waves")
    if isinstance(waves, list):
        for wave in waves:
            if isinstance(wave, dict) and len(_wave_tasks(wave)) > 1:
                return True
    return bool(wg.dependency_edges(ready))


def _wave_tasks(wave: dict) -> list[dict[str, Any]]:
    tasks = wave.get("tasks")
    if isinstance(tasks, list) and tasks:
        return [item for item in tasks if isinstance(item, dict)]
    ids = wave.get("task_ids") if isinstance(wave.get("task_ids"), list) else []
    return [{"id": task_id} for task_id in ids if isinstance(task_id, str)]


def _plain_task_list(items: list[dict]) -> str:
    if not items:
        return ""
    rows = "".join(_task_item(item) for item in items)
    return f'<ul class="wk-list">{rows}</ul>'


def _wave_lanes(ready: dict) -> str:
    waves = ready.get("waves")
    if not isinstance(waves, list):
        return _plain_task_list([item for item in (ready.get("ready") or []) if isinstance(item, dict)])
    valid = [wave for wave in waves if isinstance(wave, dict)]
    total = len(valid)
    lanes: list[str] = []
    for position, wave in enumerate(valid):
        raw_index = wave.get("index", position)
        try:
            index = int(raw_index)
        except (TypeError, ValueError):
            index = position
        heading = _wave_heading(wave, step=index + 1, total=total)
        tasks = _wave_tasks(wave)
        chips = "".join(_task_item(item) for item in tasks) or f"<li>{html.esc('empty wave')}</li>"
        lanes.append(
            f'<section class="wk-lane"><h3 class="wk-lane-title">{html.esc(heading)}</h3>'
            f'<ul class="wk-list">{chips}</ul></section>'
        )
    if not lanes:
        return _plain_task_list([item for item in (ready.get("ready") or []) if isinstance(item, dict)])
    return f'<div class="wk-lanes">{"".join(lanes)}</div>'


def _wave_heading(wave: dict, *, step: int, total: int) -> str:
    exclusive = bool(wave.get("exclusive"))
    task_count = len(_wave_tasks(wave))
    if exclusive and task_count <= 1:
        if step < total:
            return f"Step {step} of {total} - one task at a time (blocks step {step + 1})"
        return f"Step {step} of {total} - one task at a time"
    if exclusive:
        if step < total:
            return f"Step {step} of {total} - exclusive (file overlap with later steps)"
        return f"Step {step} of {total} - exclusive"
    if task_count > 1:
        return f"Step {step} of {total} - these can run together"
    return f"Step {step} of {total} - ready now"


def _blocked_list(items: list[dict]) -> str:
    if not items:
        return ""
    rows = "".join(_task_item(item, blocked=True) for item in items)
    return f'<p class="wk-muted">{html.esc("Waiting on a blocker:")}</p><ul class="wk-list">{rows}</ul>'


def _dependency_diagram(ready: dict) -> str:
    edges = wg.dependency_edges(ready)
    if not edges:
        return ""
    nodes = wg.graph_nodes(ready)
    if not nodes:
        return ""
    rows = _diagram_rows(ready, nodes, edges)
    if not rows:
        return ""

    box_w = 200
    box_h = 36
    gap_x = 24
    gap_y = 52
    pad = 24
    max_cols = max(len(row) for row in rows)
    width = max(720, pad * 2 + max_cols * box_w + max(0, max_cols - 1) * gap_x)
    height = pad * 2 + len(rows) * box_h + max(0, len(rows) - 1) * gap_y
    positions: dict[str, tuple[int, int, int, int]] = {}
    boxes: list[str] = []
    for row_index, row in enumerate(rows):
        count = len(row)
        row_width = count * box_w + max(0, count - 1) * gap_x
        start_x = pad if count >= max_cols else max(pad, (width - row_width) // 2)
        y = pad + row_index * (box_h + gap_y)
        for col, node in enumerate(row):
            x = start_x + col * (box_w + gap_x)
            task_id = str(node.get("id") or "")
            visible, full = _diagram_label(node)
            kind = str(node.get("kind") or "other")
            if kind == "ready":
                stroke = _STATUS_GOOD
            elif kind == "blocked":
                stroke = _STATUS_SERIOUS
            else:
                stroke = "#888780"
            positions[task_id] = (x, y, box_w, box_h)
            boxes.append(
                f'<g class="wk-node wk-node-{html.esc(kind)}">'
                f"<title>{html.esc(full)}</title>"
                f'<rect x="{x}" y="{y}" width="{box_w}" height="{box_h}" rx="4" '
                f'fill="{html.esc(_SURFACE)}" stroke="{html.esc(stroke)}" stroke-width="1.5"/>'
                f'<text x="{x + box_w // 2}" y="{y + 23}" text-anchor="middle" '
                f'class="wk-svg-label">{html.esc(visible)}</text>'
                f"</g>"
            )

    lines: list[str] = []
    for edge in edges:
        source = edge["source"]
        target = edge["target"]
        if source not in positions or target not in positions:
            continue
        sx, sy, sw, sh = positions[source]
        tx, ty, tw, th = positions[target]
        x1, y1 = sx + sw // 2, sy + sh
        x2, y2 = tx + tw // 2, ty
        edge_type = edge.get("type") or "blocks"
        verb = "blocks" if edge_type == "blocks" else "leads to"
        lines.append(
            f'<g class="wk-edge" data-wk-from="{html.esc(source)}" data-wk-to="{html.esc(target)}" '
            f'data-wk-type="{html.esc(edge_type)}">'
            f"<title>{html.esc(f'{source} {verb} {target}')}</title>"
            f'<path d="M {x1} {y1} L {x2} {y2}" fill="none" stroke="{html.esc(_TEXT_SECONDARY)}" '
            f'stroke-width="1.5" marker-end="url(#wk-dep-arrow)"/>'
            f"</g>"
        )
    if not lines:
        return ""

    caption = html.esc("What is blocking what")
    return (
        f'<div class="wk-diagram-wrap">'
        f'<p class="wk-muted">{caption}</p>'
        f'<svg class="wk-diagram" viewBox="0 0 {width} {height}" width="100%" '
        f'role="img" aria-label="{caption}">'
        "<defs>"
        '<marker id="wk-dep-arrow" viewBox="0 0 10 10" refX="9" refY="5" '
        'markerWidth="6" markerHeight="6" orient="auto">'
        f'<path d="M 0 0 L 10 5 L 0 10 z" fill="{html.esc(_TEXT_SECONDARY)}"/>'
        "</marker></defs>" + "".join(lines) + "".join(boxes) + "</svg></div>"
    )


def _diagram_rows(ready: dict, nodes: list[dict[str, Any]], edges: list[dict[str, str]]) -> list[list[dict[str, Any]]]:
    wave_row: dict[str, int] = {}
    waves = ready.get("waves")
    if isinstance(waves, list):
        for wave in waves:
            if not isinstance(wave, dict):
                continue
            raw = wave.get("index", 0)
            try:
                rank = int(raw)
            except (TypeError, ValueError):
                continue
            for task in _wave_tasks(wave):
                task_id = task.get("id")
                if isinstance(task_id, str) and task_id.strip():
                    wave_row[task_id] = rank

    by_id = {str(node["id"]): node for node in nodes if isinstance(node.get("id"), str)}
    rows_map: dict[int, list[dict[str, Any]]] = {}
    if wave_row:
        for task_id, rank in wave_row.items():
            node = by_id.get(task_id)
            if node is not None:
                rows_map.setdefault(rank, []).append(node)
        leftover = [node for node in nodes if str(node["id"]) not in wave_row]
        extra_start = max(wave_row.values()) + 1
        leftover_layers = wg.layout_layers(leftover, edges) if leftover else []
        for offset, layer in enumerate(leftover_layers):
            if layer:
                rows_map.setdefault(extra_start + offset, []).extend(layer)
    else:
        for offset, layer in enumerate(wg.layout_layers(nodes, edges)):
            if layer:
                rows_map.setdefault(offset, []).extend(layer)

    rows: list[list[dict[str, Any]]] = []
    for key in sorted(rows_map):
        ordered = sorted(rows_map[key], key=lambda item: str(item.get("id") or ""))
        if ordered:
            rows.append(ordered)
    return rows


def _diagram_label(node: dict[str, Any]) -> tuple[str, str]:
    title = str(node.get("title") or "").strip()
    task_id = str(node.get("id") or "")
    full = title or task_id or "Untitled task"
    visible = full if len(full) <= 28 else f"{full[:25]}..."
    return visible, full


def _task_item(item: dict, *, blocked: bool = False) -> str:
    title = str(item.get("text") or item.get("title") or "Untitled task")
    task_id = str(item.get("id") or "")
    mark = " blocked" if blocked else ""
    details = _id_details("Task id", task_id) if task_id else ""
    return f'<li class="wk-item{mark}"><span class="wk-title">{html.esc(title)}</span>{details}</li>'


def _ready_debug(ready: dict) -> str:
    blob = {
        "ready_count": ready.get("ready_count"),
        "blocked_count": ready.get("blocked_count"),
        "wave_count": ready.get("wave_count"),
        "partition_mode": ready.get("partition_mode"),
        "ids": [item.get("id") for item in (ready.get("ready") or []) if isinstance(item, dict)],
    }
    return json.dumps(blob, indent=2, sort_keys=True)


def _claims_section(tasks_payload: dict, *, now: datetime) -> str:
    inner: list[str] = []
    if tasks_payload.get("error"):
        inner.append(html.error_panel("Claims", str(tasks_payload["error"])))
    else:
        summary, tiles, body, debug = _claims_body(tasks_payload, now=now)
        inner.append(_summary_strip(summary, "What is claimed"))
        inner.append(html.panel(html.esc("In progress"), tiles + body + debug))
    return f'<section id="claims">{"".join(inner)}</section>'


def _claims_body(tasks_payload: dict, *, now: datetime) -> tuple[str, str, str, str]:
    tasks = [item for item in (tasks_payload.get("tasks") or []) if isinstance(item, dict)]
    abandoned: list[tuple[dict, float]] = []
    missing_files: list[dict] = []
    in_progress: list[dict] = []
    for task in tasks:
        claim = wg.claim_record(task)
        footprint = wg.footprint_of(task)
        stale = wg.claim_is_stale(claim, now=now)
        age = wg.claim_age_hours(claim, now=now)
        status = str(task.get("status") or "pending")
        if stale and age is not None:
            abandoned.append((task, age))
        elif claim is not None and status == "in_progress":
            in_progress.append(task)
        if wg.footprint_staleness(task, footprint) is not None and status in wg.TERMINAL_STATUSES:
            missing_files.append(task)
        elif footprint is None and status in wg.TERMINAL_STATUSES:
            missing_files.append(task)

    summary = _claims_summary(abandoned, missing_files)
    tiles = _tiles(
        [
            (
                "Abandoned",
                str(len(abandoned)),
                "serious" if abandoned else "good",
                f"Claims held longer than {work_constants.CLAIM_STALE_HOURS}h with no activity.",
            ),
            (
                "Missing files",
                str(len(missing_files)),
                "warning" if missing_files else "good",
                "Finished work that never recorded the files it touched.",
            ),
            (
                "In progress",
                str(len(in_progress)),
                "good" if in_progress or not abandoned else "warning",
                "Claims that still look active.",
            ),
        ]
    )
    body_parts: list[str] = []
    if abandoned:
        body_parts.append(f'<p class="wk-muted">{html.esc("Abandoned claims")}</p>')
        body_parts.append('<ul class="wk-list">' + "".join(_claim_item(task, age) for task, age in abandoned) + "</ul>")
    if missing_files:
        body_parts.append(f'<p class="wk-muted">{html.esc("Finished without a file list")}</p>')
        body_parts.append('<ul class="wk-list">' + "".join(_task_item(task) for task in missing_files) + "</ul>")
    if in_progress:
        body_parts.append(f'<p class="wk-muted">{html.esc("Still moving")}</p>')
        body_parts.append('<ul class="wk-list">' + "".join(_task_item(task) for task in in_progress) + "</ul>")
    if not body_parts:
        body_parts.append(f"<p>{html.esc('Nothing is claimed, and no finished work is missing a file list.')}</p>")
    debug = _raw_details("Claim and task ids", _claims_debug(abandoned, missing_files, in_progress))
    return summary, tiles, "".join(body_parts), debug


def _claims_summary(abandoned: list[tuple[dict, float]], missing_files: list[dict]) -> str:
    parts: list[str] = []
    if abandoned:
        oldest = max(age for _task, age in abandoned)
        count = len(abandoned)
        noun = "claim looks" if count == 1 else "claims look"
        parts.append(f"{count} {noun} abandoned - claimed {oldest:.0f}h ago with no activity")
    if missing_files:
        count = len(missing_files)
        noun = "finished task never recorded" if count == 1 else "finished tasks never recorded"
        parts.append(f"{count} {noun} what files they touched")
    if not parts:
        return "No claims look abandoned, and finished work recorded the files it touched."
    return "; ".join(parts) + "."


def _claim_item(task: dict, age: float) -> str:
    title = str(task.get("text") or task.get("title") or "Untitled task")
    claim = wg.claim_record(task) or {}
    raw = json.dumps(
        {
            "task_id": task.get("id"),
            "claim_id": claim.get("claim_id"),
            "actor": claim.get("actor"),
            "age_hours": round(age, 1),
        },
        indent=2,
        sort_keys=True,
    )
    return (
        f'<li class="wk-item"><span class="wk-title">{html.esc(title)}</span>'
        f'<span class="wk-muted"> {html.esc(f"claimed {age:.0f}h ago")}</span>'
        f"{_raw_details('Claim id', raw)}</li>"
    )


def _claims_debug(
    abandoned: list[tuple[dict, float]],
    missing_files: list[dict],
    in_progress: list[dict],
) -> str:
    blob = {
        "abandoned": [task.get("id") for task, _age in abandoned],
        "missing_files": [task.get("id") for task in missing_files],
        "in_progress": [task.get("id") for task in in_progress],
    }
    return json.dumps(blob, indent=2, sort_keys=True)


def _recent_section(runs_payload: dict) -> str:
    inner: list[str] = []
    if runs_payload.get("error"):
        inner.append(html.error_panel("Recent runs", str(runs_payload["error"])))
    else:
        summary, body, debug = _recent_body(runs_payload)
        inner.append(_summary_strip(summary, "What just happened"))
        inner.append(html.panel(html.esc("Recent runs"), body + debug))
    return f'<section id="recent">{"".join(inner)}</section>'


def _recent_body(runs_payload: dict) -> tuple[str, str, str]:
    runs = [item for item in (runs_payload.get("runs") or []) if isinstance(item, dict)]
    runs = sorted(runs, key=lambda item: str(item.get("started_at") or item.get("run_id") or ""), reverse=True)
    if not runs:
        body = f'<p>{html.esc("No recent verify runs.")} <a href="/view/runs">{html.esc("Open Runs")}</a></p>'
        return "Nothing has run recently.", body, ""
    shown = runs[:_RECENT_STRIP_LIMIT]
    failed = sum(1 for item in shown if str(item.get("status") or "") not in {"completed", "passed", "ok"})
    summary = (
        f"{len(shown)} recent verify run(s). "
        + ("Some failed." if failed else "The latest ones passed.")
        + " Full history is on Runs."
    )
    items = "".join(_run_item(item) for item in shown)
    body = f'<p><a href="/view/runs">{html.esc("See all runs")}</a></p><ul class="wk-list">{items}</ul>'
    debug = _raw_details("Run ids", json.dumps([item.get("run_id") for item in shown], indent=2))
    return summary, body, debug


def _run_item(receipt: dict) -> str:
    status = str(receipt.get("status") or "unknown")
    passed = status in {"completed", "passed", "ok"}
    word = "passed" if passed else "failed"
    commands = receipt.get("commands")
    command_records = commands if isinstance(commands, list) else []
    command = "-"
    for record in command_records:
        if isinstance(record, dict) and record.get("command"):
            command = str(record["command"])
            break
    run_id = str(receipt.get("run_id") or "")
    return (
        f'<li class="wk-item"><span class="wk-run-{html.esc("ok" if passed else "bad")}">{html.esc(word)}</span> '
        f'<span class="wk-title">{html.esc(command)}</span>'
        f"{_id_details('Run id', run_id)}</li>"
    )


def _summary_strip(text: str, label: str) -> str:
    return (
        f'<section class="mo-summary" data-mo-summary="1" aria-label="{html.esc(label)}">'
        f"<p>{html.esc(text)}</p>"
        "</section>"
    )


def _tiles(items: list[tuple[str, str, str, str]]) -> str:
    tiles: list[str] = []
    for label, word, role, meaning in items:
        icon = "✓" if role == "good" else "!"
        tiles.append(
            f'<article class="mo-health-tile mo-health-{html.esc(role)}">'
            f'<div class="mo-health-head">'
            f'<span class="mo-chip-icon wk-chip-{html.esc(role)}" aria-hidden="true">{html.esc(icon)}</span>'
            f'<span class="mo-health-label">{html.esc(label)}</span>'
            f'<span class="mo-chip-word">{html.esc(word)}</span>'
            f"</div>"
            f'<p class="mo-health-meaning">{html.esc(meaning)}</p>'
            "</article>"
        )
    return f'<div class="mo-health-tiles">{"".join(tiles)}</div>'


def _id_details(label: str, value: str) -> str:
    if not value:
        return ""
    return f'<details class="mo-debug"><summary>{html.esc(label)}</summary><code>{html.esc(value)}</code></details>'


def _raw_details(label: str, body: str) -> str:
    return f'<details class="mo-debug"><summary>{html.esc(label)}</summary><pre>{html.esc(body)}</pre></details>'


def _stylesheet(nonce: str) -> str:
    return f"""<style nonce="{html.esc(nonce)}">
.mo-summary {{
  margin: 0 0 1rem;
  padding: 0.85rem 1rem;
  border: 1px solid {_BORDER};
  border-radius: 0.35rem;
  background: {_SURFACE};
  color: {_TEXT_PRIMARY};
  max-width: 72rem;
}}
.mo-summary p {{ margin: 0; }}
.mo-health-tiles {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(14rem, 1fr));
  gap: 0.75rem;
  margin-bottom: 0.75rem;
}}
.mo-health-tile {{
  border: 1px solid {_BORDER};
  border-radius: 0.35rem;
  padding: 0.75rem;
  background: {_SURFACE};
}}
.mo-health-head {{
  display: flex;
  align-items: center;
  gap: 0.45rem;
  margin-bottom: 0.35rem;
}}
.mo-health-label {{ font-weight: 600; flex: 1; }}
.mo-health-meaning {{ margin: 0; color: {_TEXT_SECONDARY}; font-size: 0.92rem; }}
.mo-chip-word {{ font-size: 0.75rem; font-weight: 700; }}
.mo-chip-icon {{
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 1.1rem;
  height: 1.1rem;
  border-radius: 999px;
  color: #fff;
  font-size: 0.7rem;
}}
.wk-chip-good {{ background: {_STATUS_GOOD}; }}
.wk-chip-warning {{ background: {_STATUS_WARNING}; }}
.wk-chip-serious {{ background: {_STATUS_SERIOUS}; }}
.wk-list {{ list-style: none; margin: 0; padding: 0; }}
.wk-item {{
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 0.4rem 0.75rem;
  padding: 0.35rem 0;
  border-bottom: 1px solid {_BORDER};
}}
.wk-title {{ font-weight: 600; color: {_TEXT_PRIMARY}; }}
.wk-muted {{ color: {_TEXT_SECONDARY}; font-size: 0.92rem; }}
.wk-lanes {{ display: flex; flex-direction: column; gap: 0.75rem; }}
.wk-lane {{
  border: 1px solid {_BORDER};
  border-left: 4px solid #0066cc;
  background: {_SURFACE};
  padding: 0.75rem 1rem;
}}
.wk-lane-title {{ margin: 0 0 0.5rem; font-size: 0.95rem; }}
.wk-diagram-wrap {{ overflow-x: auto; max-width: 100%; margin: 0.75rem 0 0; }}
.wk-diagram {{ display: block; min-height: 120px; }}
.wk-svg-label {{
  fill: {_TEXT_PRIMARY};
  font-size: 12px;
  font-family: system-ui, sans-serif;
}}
.wk-run-ok {{ color: {_STATUS_GOOD}; font-weight: 700; }}
.wk-run-bad {{ color: {_STATUS_SERIOUS}; font-weight: 700; }}
.mo-debug {{ margin: 0; font-size: 0.85rem; color: {_TEXT_SECONDARY}; }}
#ready, #claims, #recent {{ margin-bottom: 1.25rem; }}
</style>"""
