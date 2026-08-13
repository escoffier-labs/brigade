"""Code Graph dashboard view: module map, impact drill-down, change overlay.

Operator question: What parts of the codebase matter, which module is the hub,
and what breaks if I touch a changed file?

Contract-only: ``brigade code export --json`` (and ``--symbol`` for impact).
No direct graph database reads.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from brigade import code_export
from brigade.center_cmd.dashboard import data
from brigade.center_cmd.dashboard import render as html

NAME = "code"
TITLE = "Code Graph"
ORDER = 6.25

_STATUS_GOOD = "#0ca30c"
_STATUS_WARNING = "#fab219"
_STATUS_SERIOUS = "#ec835a"
_STATUS_CRITICAL = "#d03b3b"
_TEXT_PRIMARY = "#0b0b0b"
_TEXT_SECONDARY = "#52514e"
_SURFACE = "#fcfcfb"
_BORDER = "rgba(11,11,11,0.10)"
_BOX_FILL = "#f3f3f1"
_BOX_STROKE = "#c3c2b7"
_CHANGED_FILL = "#fff4e0"
_CHANGED_STROKE = "#fab219"

_COLS = 5
_BOX_W = 132
_BOX_MIN_H = 36
_BOX_MAX_H = 72
_GAP_X = 28
_GAP_Y = 36
_PAD = 20
_LABEL_W = 96


def fetch(target: Path, query: dict[str, str] | None = None) -> dict[str, Any]:
    symbol = (query or {}).get("symbol", "").strip() or None
    args = ["code", "export"]
    if symbol:
        args.extend(["--symbol", symbol])
    args.append("--overlay")
    export = data.run_json(target, args, timeout=60.0)
    return {"export": export, "symbol": symbol}


def render(payload: dict, nonce: str) -> str:
    export = payload.get("export") if isinstance(payload.get("export"), dict) else {}
    symbol = payload.get("symbol")

    if export.get("error"):
        return html.error_panel(TITLE, str(export["error"]))

    parts = [
        _stylesheet(nonce),
        _mode_tabs(bool(symbol)),
        '<div id="cg-map" class="cg-panel" data-cg-panel="map" role="tabpanel">',
        _render_module_map(export),
        "</div>",
        '<div id="cg-impact" class="cg-panel" data-cg-panel="impact" role="tabpanel">',
        _render_impact(export, symbol),
        "</div>",
        _script(nonce),
    ]
    return "".join(parts)


def _mode_tabs(impact_active: bool) -> str:
    map_sel = "false" if impact_active else "true"
    impact_sel = "true" if impact_active else "false"
    return (
        '<div class="cg-modes" role="tablist" aria-label="' + html.esc("Code graph modes") + '">'
        f'<a href="#cg-map" class="cg-mode" data-cg-mode="map" aria-selected="{map_sel}" '
        f'tabindex="{"0" if not impact_active else "-1"}">' + html.esc("Module map") + "</a>"
        f'<a href="#cg-impact" class="cg-mode" data-cg-mode="impact" aria-selected="{impact_sel}" '
        f'tabindex="{"0" if impact_active else "-1"}">' + html.esc("Impact") + "</a>"
        "</div>"
    )


def _summary_strip(export: dict) -> str:
    stats = export.get("stats") if isinstance(export.get("stats"), dict) else {}
    module_map = export.get("module_map") if isinstance(export.get("module_map"), dict) else {}
    insights = module_map.get("insights") if isinstance(module_map.get("insights"), dict) else {}
    modules = [row for row in module_map.get("modules", []) if isinstance(row, dict)]

    core = insights.get("core") if isinstance(insights.get("core"), dict) else {}
    hub = insights.get("most_connected") if isinstance(insights.get("most_connected"), dict) else {}
    change = insights.get("biggest_change") if isinstance(insights.get("biggest_change"), dict) else {}
    isolated = insights.get("isolated_count")
    total = insights.get("total_modules")
    if not isinstance(total, int):
        total = len(modules)

    core_label = str(core.get("label") or "this repository")
    core_symbols = core.get("symbol_count")
    if not isinstance(core_symbols, int):
        core_symbols = stats.get("symbols") if isinstance(stats.get("symbols"), int) else 0
    hub_label = str(hub.get("label") or core_label)
    hub_id = str(hub.get("id") or "")
    hub_in = hub.get("inbound") if isinstance(hub.get("inbound"), int) else 0
    isolated_n = isolated if isinstance(isolated, int) else 0
    change_label = str(change.get("label") or "none on this branch")
    change_id = str(change.get("id") or "")

    s1 = (
        f'<a href="#cg-map" data-cg-jump="map">'
        f"{html.esc(core_label)}'s core package: {core_symbols:,} symbols "
        f"across {total} modules.</a>"
    )
    s2 = (
        f'<a href="#cg-hub" data-cg-jump="{html.esc(hub_id)}">'
        f"Most connected: {html.esc(hub_label)} (imported by {hub_in} others).</a>"
    )
    s3 = f'<a href="#cg-isolated" data-cg-jump="isolated">{isolated_n} modules are isolated.</a>'
    s4 = (
        f'<a href="#cg-changed" data-cg-jump="{html.esc(change_id)}">'
        f"Biggest recent change impact: {html.esc(change_label)}.</a>"
    )
    return (
        f'<section class="cg-summary" data-cg-summary="1" aria-label="{html.esc("Code graph summary")}">'
        f"<p>{s1} {s2} {s3} {s4}</p></section>"
    )


def _render_module_map(export: dict) -> str:
    module_map = export.get("module_map") if isinstance(export.get("module_map"), dict) else {}
    modules = [row for row in module_map.get("modules", []) if isinstance(row, dict)]
    edges = [row for row in module_map.get("edges", []) if isinstance(row, dict)]
    trunc = module_map.get("truncation") if isinstance(module_map.get("truncation"), dict) else {}

    if not modules:
        return html.panel(html.esc("Module map"), f"<p>{html.esc('Nothing here.')}</p>")

    trunc_note = ""
    note = trunc.get("note")
    if isinstance(note, str) and note.strip():
        trunc_note = f'<p class="cg-muted" data-cg-truncation="1" id="cg-isolated">{html.esc(note.strip())}</p>'

    hub_id = ""
    insights = module_map.get("insights") if isinstance(module_map.get("insights"), dict) else {}
    hub = insights.get("most_connected") if isinstance(insights.get("most_connected"), dict) else {}
    if isinstance(hub.get("id"), str):
        hub_id = hub["id"]

    return (
        _summary_strip(export)
        + html.panel(
            html.esc("Module map"),
            trunc_note + _module_map_svg(modules, edges, hub_id=hub_id) + _module_card_shell(),
        )
        + _debug_details(export)
    )


def _module_card_shell() -> str:
    return (
        '<aside id="cg-card" class="cg-card" hidden>'
        f"<h3 data-cg-card-title>{html.esc('Module')}</h3>"
        f"<p data-cg-card-meaning>{html.esc('What it is.')}</p>"
        f"<h4>{html.esc('Top files (by symbol count)')}</h4>"
        "<ul data-cg-card-files></ul>"
        f"<h4>{html.esc('Imported by (who depends on it)')}</h4>"
        "<ul data-cg-card-dependents></ul>"
        f"<h4>{html.esc('Attributed tests')}</h4>"
        "<ul data-cg-card-tests></ul>"
        f'<button type="button" data-cg-card-close>{html.esc("Close")}</button>'
        "</aside>"
    )


def _module_meaning(module: dict) -> str:
    package = str(module.get("package") or module.get("label") or "this module")
    files = int(module.get("file_count") or 0)
    symbols = int(module.get("symbol_count") or 0)
    return f"{package} package: {files} files, {symbols} symbols."


def _module_card_payload(module: dict) -> str:
    payload = {
        "id": str(module.get("id") or ""),
        "label": str(module.get("label") or module.get("id") or ""),
        "meaning": _module_meaning(module),
        "top_files": [
            f"{row.get('path')} ({row.get('symbol_count')})"
            for row in module.get("top_files", [])
            if isinstance(row, dict)
        ],
        "dependents": [str(item) for item in module.get("dependents", []) if item],
        "tests": [str(item) for item in module.get("attributed_tests", []) if item],
    }
    return html.esc(json.dumps(payload, separators=(",", ":")))


def _modules_by_package(modules: list[dict]) -> list[tuple[str, list[dict]]]:
    grouped: dict[str, list[dict]] = {}
    order: list[str] = []
    for module in modules:
        package = str(module.get("package") or module.get("label") or "(root)")
        if package not in grouped:
            grouped[package] = []
            order.append(package)
        grouped[package].append(module)
    return [(package, grouped[package]) for package in order]


def _module_map_svg(modules: list[dict], edges: list[dict], *, hub_id: str = "") -> str:
    if not modules:
        return f"<p>{html.esc('Nothing here.')}</p>"

    positions: dict[str, tuple[int, int, int, int]] = {}
    max_symbols = max(int(row.get("symbol_count") or 0) for row in modules) or 1
    grouped = _modules_by_package(modules)
    boxes: list[str] = []
    package_labels: list[str] = []
    y = _PAD
    max_x = _PAD + _LABEL_W + _BOX_W
    marked_changed = False

    for package, rows in grouped:
        package_labels.append(
            f'<text x="{_PAD}" y="{y + 22}" class="cg-svg-heading">{html.esc(code_export.fit_svg_label(package, _LABEL_W - 8)[0])}</text>'
        )
        x = _PAD + _LABEL_W
        row_height = _BOX_MAX_H
        for module in rows:
            if x > _PAD + _LABEL_W + _COLS * (_BOX_W + _GAP_X) - _GAP_X:
                x = _PAD + _LABEL_W
                y += _BOX_MAX_H + _GAP_Y
            symbol_count = int(module.get("symbol_count") or 0)
            ratio = symbol_count / max_symbols
            height = int(_BOX_MIN_H + ratio * (_BOX_MAX_H - _BOX_MIN_H))
            module_id = str(module.get("id") or "")
            label = str(module.get("label") or module_id)
            visible, full = code_export.fit_svg_label(label, _BOX_W - 16)
            count_text = f" ({symbol_count})" if symbol_count else ""
            fill = _CHANGED_FILL if module.get("changed") else _BOX_FILL
            stroke = _CHANGED_STROKE if module.get("changed") else _BOX_STROKE
            chip = _status_chip(
                x, y, height, "CHANGED" if module.get("changed") else "OK", module.get("changed") is True
            )
            positions[module_id] = (x, y, _BOX_W, height)
            extra_ids = []
            if hub_id and module_id == hub_id:
                extra_ids.append("cg-hub")
            if module.get("changed") and not marked_changed:
                extra_ids.append("cg-changed")
                marked_changed = True
            id_attr = f'id="cg-module-{html.esc(module_id)}"'
            extra = "".join(f'<g id="{html.esc(item)}"></g>' for item in extra_ids)
            boxes.append(
                f'<g class="cg-module" {id_attr} role="button" tabindex="0" data-cg-card="{_module_card_payload(module)}">'
                f"<title>{html.esc(full)}{html.esc(count_text)}</title>"
                f'<rect x="{x}" y="{y}" width="{_BOX_W}" height="{height}" rx="6" ry="6" '
                f'fill="{html.esc(fill)}" stroke="{html.esc(stroke)}" stroke-width="1.5"/>'
                f'<text x="{x + 8}" y="{y + 20}" class="cg-svg-label">{html.esc(visible)}{html.esc(count_text)}</text>'
                f"{chip}"
                f"</g>"
                f"{extra}"
            )
            x += _BOX_W + _GAP_X
            max_x = max(max_x, x)
        y += row_height + _GAP_Y

    max_weight = max((int(edge.get("weight") or 0) for edge in edges), default=1) or 1
    edge_lines: list[str] = []
    for edge in edges:
        source = str(edge.get("from") or "")
        target = str(edge.get("to") or "")
        if source not in positions or target not in positions:
            continue
        sx, sy, sw, sh = positions[source]
        tx, ty, tw, th = positions[target]
        x1, y1 = sx + sw // 2, sy + sh
        x2, y2 = tx + tw // 2, ty
        weight = int(edge.get("weight") or 0)
        cx = (x1 + x2) // 2
        cy = min(y1, y2) - 18
        width = min(4, 1 + weight // 3)
        opacity = 0.25 + 0.75 * (weight / max_weight)
        edge_lines.append(
            f'<g class="cg-edge"><title>{html.esc(f"{source} → {target} ({weight})")}</title>'
            f'<path d="M {x1} {y1} Q {cx} {cy} {x2} {y2}" fill="none" stroke="{html.esc(_BOX_STROKE)}" '
            f'stroke-width="{width}" stroke-opacity="{opacity:.2f}" marker-end="url(#cg-arrow)"/></g>'
        )

    width = max(max_x + _PAD, 720)
    height = y + _PAD

    return (
        f'<div class="cg-map-wrap"><svg class="cg-map" viewBox="0 0 {width} {height}" width="100%" '
        f'role="img" aria-label="{html.esc("Module map")}">'
        "<defs>"
        '<marker id="cg-arrow" viewBox="0 0 10 10" refX="9" refY="5" '
        'markerWidth="6" markerHeight="6" orient="auto-start-reverse">'
        f'<path d="M 0 0 L 10 5 L 0 10 z" fill="{html.esc(_BOX_STROKE)}"/>'
        "</marker></defs>" + "".join(edge_lines) + "".join(package_labels) + "".join(boxes) + "</svg></div>"
    )


def _status_chip(box_x: int, box_y: int, box_h: int, word: str, changed: bool) -> str:
    if changed:
        color, icon = _STATUS_WARNING, "!"
    else:
        color, icon = _STATUS_GOOD, "\u2713"
    chip_x = box_x + _BOX_W - 58
    chip_y = box_y + box_h - 24
    return (
        f'<rect x="{chip_x}" y="{chip_y}" width="50" height="16" rx="8" ry="8" fill="{html.esc(_SURFACE)}" '
        f'stroke="{html.esc(color)}" stroke-width="1.2"/>'
        f'<circle cx="{chip_x + 8}" cy="{chip_y + 8}" r="4" fill="{html.esc(color)}"/>'
        f'<text x="{chip_x + 8}" y="{chip_y + 11}" text-anchor="middle" class="cg-svg-chip-icon">{html.esc(icon)}</text>'
        f'<text x="{chip_x + 30}" y="{chip_y + 11}" text-anchor="middle" class="cg-svg-chip-word">{html.esc(word)}</text>'
    )


def _render_impact(export: dict, symbol: str | None) -> str:
    search_form = (
        '<form class="cg-search" method="get" action="/view/code">'
        f"<label>{html.esc('Symbol or file')} "
        f'<input type="search" name="symbol" value="{html.esc(symbol or "")}" '
        f'placeholder="{html.esc("e.g. dispatch or src/brigade/proc.py")}" '
        f'aria-label="{html.esc("Symbol search")}"></label>'
        f'<button type="submit">{html.esc("Show impact")}</button>'
        "</form>"
    )

    impact = export.get("impact") if isinstance(export.get("impact"), dict) else None
    if not symbol:
        return html.panel(
            html.esc("Impact"),
            search_form
            + f"<p>{html.esc('Search for a symbol to see callers, blast radius, and attributed tests.')}</p>",
        )
    if not impact:
        return html.panel(
            html.esc("Impact"),
            search_form + f"<p>{html.esc('No impact data for that symbol.')}</p>",
        )

    resolved = impact.get("resolved_symbol") if isinstance(impact.get("resolved_symbol"), dict) else {}
    plain_name = str(resolved.get("name") or resolved.get("qualified_name") or symbol)
    file_path = str(resolved.get("file_path") or "")
    intro = f"Blast radius for {plain_name}."
    if file_path:
        intro += f" Defined in {file_path}."

    callers = impact.get("callers") if isinstance(impact.get("callers"), list) else []
    edges = impact.get("edges") if isinstance(impact.get("edges"), list) else []
    affected = impact.get("affected_tests") if isinstance(impact.get("affected_tests"), dict) else {}

    sections = [
        search_form,
        f'<p class="cg-impact-intro">{html.esc(intro)}</p>',
        _impact_diagram(plain_name, callers, edges),
        _impact_table("Direct callers", callers, limit=20),
        _impact_table("Impact set", edges, limit=40),
        _affected_tests_table(affected),
        _impact_debug(impact),
    ]
    return html.panel(html.esc("Impact"), "".join(sections))


def _impact_diagram(focus: str, callers: list, edges: list) -> str:
    layers = [
        ("Callers", callers[:8]),
        ("Focus", [{"source": row.get("source"), "target": focus} for row in callers[:1]] or [{"target": focus}]),
        ("Impact", edges[:10]),
    ]
    width = 720
    height = 200
    parts = [
        f'<svg class="cg-impact-diagram" viewBox="0 0 {width} {height}" width="100%" role="img" '
        f'aria-label="{html.esc("Impact layers")}">'
    ]
    x_positions = [80, width // 2, width - 120]
    for (title, rows), x in zip(layers, x_positions, strict=False):
        parts.append(f'<text x="{x}" y="24" text-anchor="middle" class="cg-svg-heading">{html.esc(title)}</text>')
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            label = str(row.get("source") or row.get("target") or "?")
            visible, full = code_export.fit_svg_label(label, 120)
            box_y = 40 + index * 22
            parts.append(
                f"<g><title>{html.esc(full)}</title>"
                f'<rect x="{x - 60}" y="{box_y}" width="120" height="18" rx="4" fill="{html.esc(_BOX_FILL)}" '
                f'stroke="{html.esc(_BOX_STROKE)}"/>'
                f'<text x="{x}" y="{box_y + 13}" text-anchor="middle" class="cg-svg-label">{html.esc(visible)}</text>'
                f"</g>"
            )
    parts.append("</svg>")
    return "".join(parts)


def _impact_table(title: str, rows: list, *, limit: int) -> str:
    shown = [row for row in rows if isinstance(row, dict)][:limit]
    hidden = max(0, len(rows) - len(shown))
    note = ""
    if hidden:
        note = f'<p class="cg-muted">{html.esc(f"showing top {len(shown)} rows, {hidden} hidden")}</p>'
    if not shown:
        return f"<h3>{html.esc(title)}</h3><p>{html.esc('Nothing here.')}</p>"

    body_rows: list[list[str]] = []
    for row in shown:
        source = str(row.get("source") or "-")
        target = str(row.get("target") or "-")
        kind = str(row.get("kind") or "-")
        hops = str(row.get("hops") if row.get("hops") is not None else "-")
        body_rows.append(
            [
                f"{html.esc(source)} "
                f'<details class="cg-debug"><summary>{html.esc("Ids")}</summary>'
                f"<code>{html.esc(str(row.get('source_id') or '-'))} → {html.esc(str(row.get('target_id') or '-'))}</code>"
                f"</details>",
                html.esc(target),
                html.esc(kind),
                html.esc(hops),
            ]
        )
    table = html.table(
        [html.esc("From"), html.esc("To"), html.esc("Kind"), html.esc("Hops")],
        body_rows,
    )
    return f'<h3>{html.esc(title)}</h3>{note}<div class="cg-scroll">{table}</div>'


def _affected_tests_table(affected: dict) -> str:
    tests = affected.get("affected_tests") if isinstance(affected.get("affected_tests"), list) else []
    if not tests:
        return f"<h3>{html.esc('Attributed tests')}</h3><p>{html.esc('No attributed tests.')}</p>"
    attr = affected.get("attribution")
    note = f'<p class="cg-muted">{html.esc(str(attr))}</p>' if isinstance(attr, str) and attr else ""
    rows = []
    for row in tests:
        if not isinstance(row, dict):
            continue
        path = str(row.get("file_path") or "-")
        hops = str(row.get("min_hops") if row.get("min_hops") is not None else "-")
        via = row.get("via") if isinstance(row.get("via"), list) else []
        via_text = ", ".join(str(item) for item in via[:5])
        rows.append([html.esc(path), html.esc(hops), html.esc(via_text or "-")])
    table = html.table([html.esc("Test file"), html.esc("Hops"), html.esc("Via")], rows)
    return f'<h3>{html.esc("Attributed tests")}</h3>{note}<div class="cg-scroll">{table}</div>'


def _impact_debug(impact: dict) -> str:
    hits = impact.get("search_hits") if isinstance(impact.get("search_hits"), list) else []
    if not hits:
        return ""
    lines = []
    for row in hits:
        if not isinstance(row, dict):
            continue
        lines.append(
            f"<li>{html.esc(str(row.get('qualified_name') or row.get('name') or '?'))} "
            f"({html.esc(str(row.get('file_path') or '-'))})</li>"
        )
    return (
        '<details class="cg-debug-panel">'
        f"<summary>{html.esc('Search matches (raw)')}</summary>"
        f"<ul>{''.join(lines)}</ul></details>"
    )


def _debug_details(export: dict) -> str:
    stats = export.get("stats") if isinstance(export.get("stats"), dict) else {}
    if not stats:
        return ""
    bits = ", ".join(f"{key}={value}" for key, value in sorted(stats.items()))
    return (
        '<details class="cg-debug-panel">'
        f"<summary>{html.esc('Graph stats (raw)')}</summary>"
        f"<code>{html.esc(bits)}</code></details>"
    )


def _stylesheet(nonce: str) -> str:
    return f"""<style nonce="{html.esc(nonce)}">
.cg-modes {{ display:flex; flex-wrap:wrap; gap:0.5rem; margin:0 0 1rem; }}
.cg-mode {{
  font:inherit; padding:0.4rem 0.75rem; border:1px solid #666; border-radius:0.25rem;
  background:#f8f8f8; color:{_TEXT_PRIMARY}; cursor:pointer; text-decoration:none;
}}
.cg-mode[aria-selected="true"] {{ background:#0066cc; border-color:#0066cc; color:#fff; font-weight:600; }}
.cg-summary {{
  margin:0 0 1rem; padding:0.85rem 1rem; border:1px solid {_BORDER}; border-radius:0.35rem;
  background:{_SURFACE}; color:{_TEXT_PRIMARY};
}}
.cg-summary p {{ margin:0; }}
.cg-summary a {{ color:{_TEXT_PRIMARY}; text-decoration:underline; text-underline-offset:2px; }}
.cg-muted {{ color:{_TEXT_SECONDARY}; font-size:0.9rem; }}
.cg-map-wrap {{ overflow-x:auto; max-width:100%; }}
.cg-module {{ cursor:pointer; }}
.cg-card {{
  margin:1rem 0 0; padding:0.85rem 1rem; border:1px solid {_BORDER}; border-radius:0.35rem;
  background:{_SURFACE}; color:{_TEXT_PRIMARY}; max-width:36rem;
}}
.cg-card h3, .cg-card h4 {{ margin:0.6rem 0 0.25rem; }}
.cg-card h3 {{ margin-top:0; }}
.cg-card ul {{ margin:0; padding-left:1.2rem; }}
.cg-card[hidden] {{ display:none; }}
.cg-map {{ display:block; min-width:720px; height:auto; }}
.cg-svg-label {{ fill:{_TEXT_PRIMARY}; font-size:12px; font-family:system-ui,sans-serif; }}
.cg-svg-heading {{ fill:{_TEXT_SECONDARY}; font-size:11px; font-weight:700; font-family:system-ui,sans-serif; }}
.cg-svg-chip-word {{ fill:{_TEXT_PRIMARY}; font-size:8px; font-weight:700; font-family:system-ui,sans-serif; }}
.cg-svg-chip-icon {{ fill:#fff; font-size:7px; font-family:system-ui,sans-serif; }}
.cg-search {{ display:flex; flex-wrap:wrap; gap:0.75rem; align-items:flex-end; margin:0 0 1rem; }}
.cg-search label {{ display:flex; flex-direction:column; gap:0.25rem; font-size:0.9rem; }}
.cg-search input {{ font:inherit; min-width:16rem; padding:0.35rem 0.5rem; }}
.cg-search button {{ font:inherit; padding:0.4rem 0.75rem; }}
.cg-impact-intro {{ color:{_TEXT_PRIMARY}; }}
.cg-impact-diagram {{ display:block; margin:0 0 1rem; min-height:160px; }}
.cg-scroll {{ overflow-x:auto; max-width:100%; }}
.cg-debug, .cg-debug-panel {{ margin-top:0.35rem; color:{_TEXT_SECONDARY}; font-size:0.85rem; }}
.cg-debug summary, .cg-debug-panel summary {{ cursor:pointer; }}
.cg-panel[hidden] {{ display:none; }}
</style>
<noscript><style nonce="{html.esc(nonce)}">.cg-panel[hidden] {{ display:block !important; }}</style></noscript>"""


def _script(nonce: str) -> str:
    return f"""<script nonce="{html.esc(nonce)}">
(function () {{
  function selectMode(mode) {{
    var tabs = document.querySelectorAll("[data-cg-mode]");
    var panels = document.querySelectorAll("[data-cg-panel]");
    for (var i = 0; i < tabs.length; i++) {{
      var tab = tabs[i];
      var active = tab.getAttribute("data-cg-mode") === mode;
      tab.setAttribute("aria-selected", active ? "true" : "false");
      tab.tabIndex = active ? 0 : -1;
    }}
    for (var j = 0; j < panels.length; j++) {{
      panels[j].hidden = panels[j].getAttribute("data-cg-panel") !== mode;
    }}
  }}
  function fillList(node, items) {{
    node.innerHTML = "";
    if (!items || !items.length) {{
      var empty = document.createElement("li");
      empty.textContent = "None listed.";
      node.appendChild(empty);
      return;
    }}
    for (var i = 0; i < items.length; i++) {{
      var li = document.createElement("li");
      li.textContent = items[i];
      node.appendChild(li);
    }}
  }}
  function showCard(raw) {{
    var card = document.getElementById("cg-card");
    if (!card) return;
    var data;
    try {{ data = JSON.parse(raw); }} catch (err) {{ return; }}
    var title = card.querySelector("[data-cg-card-title]");
    var meaning = card.querySelector("[data-cg-card-meaning]");
    if (title) title.textContent = data.label || "Module";
    if (meaning) meaning.textContent = data.meaning || "";
    fillList(card.querySelector("[data-cg-card-files]"), data.top_files);
    fillList(card.querySelector("[data-cg-card-dependents]"), data.dependents);
    fillList(card.querySelector("[data-cg-card-tests]"), data.tests);
    card.hidden = false;
    if (card.scrollIntoView) card.scrollIntoView({{ block: "nearest" }});
  }}
  function closestAttr(el, name) {{
    while (el && el.getAttribute) {{
      var value = el.getAttribute(name);
      if (value) return {{ el: el, value: value }};
      el = el.parentNode;
    }}
    return null;
  }}
  document.addEventListener("click", function (e) {{
    var t = e.target;
    if (!t || !t.getAttribute) return;
    var mode = t.getAttribute("data-cg-mode");
    if (mode) {{ e.preventDefault(); selectMode(mode); return; }}
    if (t.getAttribute("data-cg-card-close") !== null) {{
      var card = document.getElementById("cg-card");
      if (card) card.hidden = true;
      return;
    }}
    var jump = closestAttr(t, "data-cg-jump");
    if (jump && jump.value && jump.value !== "map" && jump.value !== "isolated") {{
      var target = document.getElementById("cg-module-" + jump.value) || document.getElementById("cg-hub");
      if (target) {{
        var raw = target.getAttribute("data-cg-card");
        if (raw) showCard(raw);
      }}
    }}
    var cardHit = closestAttr(t, "data-cg-card");
    if (cardHit) showCard(cardHit.value);
  }});
  document.addEventListener("keydown", function (e) {{
    if (e.key !== "Enter" && e.key !== " ") return;
    var hit = closestAttr(e.target, "data-cg-card");
    if (!hit) return;
    e.preventDefault();
    showCard(hit.value);
  }});
  var params = new URLSearchParams(window.location.search || "");
  selectMode(params.get("symbol") ? "impact" : "map");
}})();
</script>"""
