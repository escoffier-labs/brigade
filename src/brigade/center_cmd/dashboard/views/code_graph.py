"""Code Graph dashboard view: module map, impact drill-down, change overlay.

Operator question: What parts of the codebase matter for my current change,
and what breaks if I touch X?

Contract-only: ``brigade code export --json`` (and ``--symbol`` for impact).
No direct graph database reads. Default module map is the branch overlay
(changed modules plus direct neighbors); the full repo grid is a fallback
when nothing changed locally or when explicitly requested.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

from brigade import code_export
from brigade.center_cmd.dashboard import data
from brigade.center_cmd.dashboard import render as html

NAME = "code"
TITLE = "Code Graph"
ORDER = 6.25

_NONCANONICAL_ROOTS = frozenset({".worktrees", ".git", ".hg", ".svn", ".venv", "node_modules"})
_IMPACT_CALLER_CAP = 8
_IMPACT_EDGE_CAP = 10

_STATUS_WARNING = "#fab219"
_TEXT_PRIMARY = "#0b0b0b"
_TEXT_SECONDARY = "#52514e"
_SURFACE = "#fcfcfb"
_BORDER = "rgba(11,11,11,0.10)"
_BOX_FILL = "#f3f3f1"
_BOX_STROKE = "#c3c2b7"
_CHANGED_FILL = "#fff4e0"
_CHANGED_STROKE = "#fab219"

_COLS = 5
_BOX_MIN_W = 96
_BOX_MAX_W = 220
_BOX_MIN_H = 36
_BOX_MAX_H = 72
_BOX_PAD_X = 8
_CHIP_GAP = 6
_CHIP_H = 16
_GAP_X = 28
_GAP_Y = 36
_PAD = 20
_LABEL_W = 96
_MAX_ROW_X = _PAD + _LABEL_W + _COLS * (132 + _GAP_X)


def fetch(target: Path, query: dict[str, str] | None = None) -> dict[str, Any]:
    query = query or {}
    symbol = query.get("symbol", "").strip() or None
    args = ["code", "export"]
    if symbol:
        args.extend(["--symbol", symbol])
    args.append("--overlay")
    export = data.run_json(target, args, timeout=60.0)
    return {
        "export": export,
        "symbol": symbol,
        "show_full_map": _query_flag(query, "map", "full"),
        "show_worktrees": _query_flag(query, "worktrees", "1"),
    }


def _query_flag(query: dict[str, str], key: str, truthy: str) -> bool:
    raw = (query.get(key) or "").strip().lower()
    return raw == truthy or raw == "1" or raw == "true"


def render(payload: dict, nonce: str) -> str:
    export = payload.get("export") if isinstance(payload.get("export"), dict) else {}
    symbol = payload.get("symbol")
    show_full_map = payload.get("show_full_map") is True
    show_worktrees = payload.get("show_worktrees") is True

    if export.get("error"):
        return html.error_panel(TITLE, str(export["error"]))

    parts = [
        _stylesheet(nonce),
        _mode_tabs(bool(symbol)),
        '<div id="cg-map" class="cg-panel" data-cg-panel="map" role="tabpanel">',
        _render_module_map(export, show_full_map=show_full_map, show_worktrees=show_worktrees),
        "</div>",
        '<div id="cg-impact" class="cg-panel" data-cg-panel="impact" role="tabpanel">',
        _render_impact(export, symbol, show_worktrees=show_worktrees, show_full_map=show_full_map),
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


def _changed_module_ids(export: dict) -> list[str]:
    overlay = export.get("change_overlay")
    if not isinstance(overlay, dict):
        return []
    raw = overlay.get("changed_modules") if isinstance(overlay.get("changed_modules"), list) else []
    ids: list[str] = []
    seen: set[str] = set()
    for item in raw:
        module_id = str(item).strip()
        if module_id and module_id not in seen:
            seen.add(module_id)
            ids.append(module_id)
    return ids


def _path_parts(value: str) -> list[str]:
    return [part for part in value.replace("\\", "/").split("/") if part]


def _is_noncanonical_module(module: dict) -> bool:
    for key in ("id", "package", "label"):
        raw = str(module.get(key) or "")
        if any(part in _NONCANONICAL_ROOTS for part in _path_parts(raw)):
            return True
    return False


def _neighbor_ids(changed: set[str], edges: list[dict]) -> set[str]:
    neighbors: set[str] = set()
    for edge in edges:
        source = str(edge.get("from") or "")
        target = str(edge.get("to") or "")
        if source in changed and target:
            neighbors.add(target)
        if target in changed and source:
            neighbors.add(source)
    return neighbors


def _filter_modules(
    modules: list[dict],
    edges: list[dict],
    *,
    keep_ids: set[str] | None,
    show_worktrees: bool,
) -> tuple[list[dict], list[dict], int]:
    hidden_noncanonical = 0
    visible: list[dict] = []
    for module in modules:
        module_id = str(module.get("id") or "")
        if keep_ids is not None and module_id not in keep_ids:
            continue
        if not show_worktrees and _is_noncanonical_module(module):
            hidden_noncanonical += 1
            continue
        visible.append(module)
    visible_ids = {str(row.get("id") or "") for row in visible}
    visible_edges = [
        edge
        for edge in edges
        if str(edge.get("from") or "") in visible_ids and str(edge.get("to") or "") in visible_ids
    ]
    return visible, visible_edges, hidden_noncanonical


def _code_href(
    *,
    symbol: str | None = None,
    show_full_map: bool = False,
    show_worktrees: bool = False,
) -> str:
    params: list[str] = []
    if symbol:
        params.append(f"symbol={quote_plus(symbol)}")
    if show_full_map:
        params.append("map=full")
    if show_worktrees:
        params.append("worktrees=1")
    query = ("?" + "&".join(params)) if params else ""
    return "/view/code" + query


def _summary_strip(
    export: dict,
    *,
    shown: list[dict],
    overlay_active: bool,
    changed_ids: list[str],
) -> str:
    stats = export.get("stats") if isinstance(export.get("stats"), dict) else {}
    module_map = export.get("module_map") if isinstance(export.get("module_map"), dict) else {}
    insights = module_map.get("insights") if isinstance(module_map.get("insights"), dict) else {}
    modules = shown if shown else [row for row in module_map.get("modules", []) if isinstance(row, dict)]

    core = insights.get("core") if isinstance(insights.get("core"), dict) else {}
    hub = insights.get("most_connected") if isinstance(insights.get("most_connected"), dict) else {}
    change = insights.get("biggest_change") if isinstance(insights.get("biggest_change"), dict) else {}
    largest = insights.get("largest") if isinstance(insights.get("largest"), dict) else {}
    isolated = insights.get("isolated_count")

    if overlay_active:
        changed_n = len(changed_ids)
        neighbor_n = max(0, len(modules) - changed_n)
        changed_word = "module" if changed_n == 1 else "modules"
        neighbor_word = "neighbor" if neighbor_n == 1 else "neighbors"
        shown_hub = hub if str(hub.get("id") or "") in {str(row.get("id") or "") for row in modules} else {}
        if not shown_hub and modules:
            top = max(modules, key=lambda row: int(row.get("inbound_count") or 0))
            shown_hub = {
                "id": str(top.get("id") or ""),
                "label": str(top.get("label") or top.get("id") or ""),
                "inbound": int(top.get("inbound_count") or 0),
            }
        hub_label = str(shown_hub.get("label") or "this overlay")
        hub_id = str(shown_hub.get("id") or "")
        hub_in = shown_hub.get("inbound") if isinstance(shown_hub.get("inbound"), int) else 0
        change_label = str(change.get("label") or (changed_ids[0] if changed_ids else "none on this branch"))
        change_id = str(change.get("id") or (changed_ids[0] if changed_ids else ""))
        s1 = (
            f'<a href="#cg-map" data-cg-jump="map">'
            f"{changed_n} {html.esc(changed_word)} changed on this branch; "
            f"showing {html.esc('changed modules and their direct neighbors')} "
            f"({changed_n} {html.esc(changed_word)}"
            f"{f', {neighbor_n} {html.esc(neighbor_word)}' if neighbor_n else ''}).</a>"
        )
        s2 = (
            f'<a href="#cg-hub" data-cg-jump="{html.esc(hub_id)}">'
            f"Most connected: {html.esc(hub_label)} (imported by {hub_in} others).</a>"
        )
        s4 = (
            f'<a href="#cg-changed" data-cg-jump="{html.esc(change_id)}">'
            f"Biggest recent change impact: {html.esc(change_label)}.</a>"
        )
        return (
            f'<section class="cg-summary" data-cg-summary="1" data-cg-summary-scope="overlay" '
            f'aria-label="{html.esc("Code graph summary")}">'
            f"<p>{s1} {s2} {s4}</p></section>"
        )

    total = insights.get("total_modules")
    if not isinstance(total, int):
        total = len(modules)

    core_label = str(core.get("label") or "this repository")
    core_symbols = core.get("symbol_count")
    if not isinstance(core_symbols, int):
        core_symbols = stats.get("symbols") if isinstance(stats.get("symbols"), int) else 0
    largest_label = str(largest.get("label") or "")
    largest_n = largest.get("symbol_count") if isinstance(largest.get("symbol_count"), int) else 0
    if not largest_label and modules:
        top = max(modules, key=lambda row: int(row.get("symbol_count") or 0))
        largest_label = str(top.get("label") or "")
        largest_n = int(top.get("symbol_count") or 0)
    hub_label = str(hub.get("label") or core_label)
    hub_id = str(hub.get("id") or "")
    hub_in = hub.get("inbound") if isinstance(hub.get("inbound"), int) else 0
    isolated_n = isolated if isinstance(isolated, int) else 0
    change_label = str(change.get("label") or "none on this branch")
    change_id = str(change.get("id") or "")

    largest_clause = ""
    if largest_label:
        largest_clause = f"; largest: {html.esc(largest_label)} ({largest_n:,})"
    s1 = (
        f'<a href="#cg-map" data-cg-jump="map">'
        f"{html.esc(core_label)}: {core_symbols:,} symbols "
        f"across {total} modules{largest_clause}.</a>"
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
        f'<section class="cg-summary" data-cg-summary="1" data-cg-summary-scope="full" '
        f'aria-label="{html.esc("Code graph summary")}">'
        f"<p>{s1} {s2} {s3} {s4}</p></section>"
    )


def _map_toggles(
    *,
    overlay_active: bool,
    hidden_noncanonical: int,
    show_full_map: bool,
    show_worktrees: bool,
    has_noncanonical: bool,
) -> str:
    links: list[str] = []
    if overlay_active:
        links.append(
            f'<a class="cg-toggle" data-cg-map-toggle="full" href="{html.esc(_code_href(show_full_map=True, show_worktrees=show_worktrees))}">'
            + html.esc("Show full module map")
            + "</a>"
        )
    elif show_full_map:
        links.append(
            f'<a class="cg-toggle" data-cg-map-toggle="overlay" href="{html.esc(_code_href(show_worktrees=show_worktrees))}">'
            + html.esc("Show branch overlay")
            + "</a>"
        )
    if has_noncanonical or hidden_noncanonical:
        if show_worktrees:
            links.append(
                f'<a class="cg-toggle" data-cg-worktrees-toggle="hide" href="{html.esc(_code_href(show_full_map=show_full_map))}">'
                + html.esc("Hide .worktrees copies")
                + "</a>"
            )
        else:
            label = (
                f"Show .worktrees copies ({hidden_noncanonical} hidden)"
                if hidden_noncanonical
                else "Show .worktrees copies"
            )
            links.append(
                f'<a class="cg-toggle" data-cg-worktrees-toggle="show" href="{html.esc(_code_href(show_full_map=show_full_map, show_worktrees=True))}">'
                + html.esc(label)
                + "</a>"
            )
    if not links:
        return ""
    return f'<p class="cg-toggles">{"".join(links)}</p>'


def _render_module_map(export: dict, *, show_full_map: bool, show_worktrees: bool) -> str:
    module_map = export.get("module_map") if isinstance(export.get("module_map"), dict) else {}
    modules = [row for row in module_map.get("modules", []) if isinstance(row, dict)]
    edges = [row for row in module_map.get("edges", []) if isinstance(row, dict)]
    trunc = module_map.get("truncation") if isinstance(module_map.get("truncation"), dict) else {}
    changed_ids = _changed_module_ids(export)
    has_overlay_data = isinstance(export.get("change_overlay"), dict)
    has_noncanonical = any(_is_noncanonical_module(row) for row in modules)

    keep_ids: set[str] | None = None
    overlay_active = False
    empty_change_banner = ""
    if changed_ids and not show_full_map:
        keep_ids = set(changed_ids) | _neighbor_ids(set(changed_ids), edges)
        overlay_active = True
    elif has_overlay_data and not changed_ids:
        empty_change_banner = (
            f'<p class="cg-banner" data-cg-empty-change="1">'
            f"{html.esc('Nothing changed locally. Showing the full module map.')}</p>"
        )

    shown, shown_edges, hidden_noncanonical = _filter_modules(
        modules, edges, keep_ids=keep_ids, show_worktrees=show_worktrees
    )

    if overlay_active and not shown:
        overlay_active = False
        keep_ids = None
        if has_overlay_data:
            empty_change_banner = (
                f'<p class="cg-banner" data-cg-empty-change="1">'
                f"{html.esc('Nothing changed locally. Showing the full module map.')}</p>"
            )
        shown, shown_edges, hidden_noncanonical = _filter_modules(
            modules, edges, keep_ids=None, show_worktrees=show_worktrees
        )

    if not shown:
        return html.panel(html.esc("Module map"), f"<p>{html.esc('Nothing here.')}</p>")

    trunc_note = ""
    if not overlay_active:
        note = trunc.get("note")
        if isinstance(note, str) and note.strip():
            trunc_note = f'<p class="cg-muted" data-cg-truncation="1" id="cg-isolated">{html.esc(note.strip())}</p>'
    elif overlay_active:
        neighbor_n = max(0, len(shown) - len(changed_ids))
        overlay_note = (
            f"Showing {len(changed_ids)} changed "
            f"{'module' if len(changed_ids) == 1 else 'modules'} and "
            f"{neighbor_n} direct {'neighbor' if neighbor_n == 1 else 'neighbors'} on this branch."
        )
        trunc_note = f'<p class="cg-muted" data-cg-overlay="1" id="cg-isolated">{html.esc(overlay_note)}</p>'

    hub_id = ""
    insights = module_map.get("insights") if isinstance(module_map.get("insights"), dict) else {}
    hub = insights.get("most_connected") if isinstance(insights.get("most_connected"), dict) else {}
    if isinstance(hub.get("id"), str) and any(str(row.get("id") or "") == hub["id"] for row in shown):
        hub_id = hub["id"]

    toggles = _map_toggles(
        overlay_active=overlay_active,
        hidden_noncanonical=hidden_noncanonical,
        show_full_map=show_full_map,
        show_worktrees=show_worktrees,
        has_noncanonical=has_noncanonical,
    )

    return (
        _summary_strip(export, shown=shown, overlay_active=overlay_active, changed_ids=changed_ids)
        + html.panel(
            html.esc("Module map"),
            empty_change_banner
            + toggles
            + trunc_note
            + _module_map_svg(shown, shown_edges, hub_id=hub_id)
            + _module_card_shell(),
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
    max_x = _PAD + _LABEL_W + _BOX_MIN_W
    marked_changed = False
    start_x = _PAD + _LABEL_W

    for package, rows in grouped:
        package_labels.append(
            f'<text x="{_PAD}" y="{y + 22}" class="cg-svg-heading">{html.esc(code_export.fit_svg_label(package, _LABEL_W - 8)[0])}</text>'
        )
        x = start_x
        row_height = _BOX_MAX_H
        for module in rows:
            symbol_count = int(module.get("symbol_count") or 0)
            ratio = symbol_count / max_symbols
            height = int(_BOX_MIN_H + ratio * (_BOX_MAX_H - _BOX_MIN_H))
            module_id = str(module.get("id") or "")
            label = str(module.get("label") or module_id)
            changed = module.get("changed") is True
            box_w, visible, full, chip = _module_box_parts(label, symbol_count, x, y, height, changed)
            if x > start_x and x + box_w > _MAX_ROW_X:
                x = start_x
                y += _BOX_MAX_H + _GAP_Y
                box_w, visible, full, chip = _module_box_parts(label, symbol_count, x, y, height, changed)
            fill = _CHANGED_FILL if changed else _BOX_FILL
            stroke = _CHANGED_STROKE if changed else _BOX_STROKE
            positions[module_id] = (x, y, box_w, height)
            extra_ids = []
            if hub_id and module_id == hub_id:
                extra_ids.append("cg-hub")
            if changed and not marked_changed:
                extra_ids.append("cg-changed")
                marked_changed = True
            id_attr = f'id="cg-module-{html.esc(module_id)}"'
            extra = "".join(f'<g id="{html.esc(item)}"></g>' for item in extra_ids)
            boxes.append(
                f'<g class="cg-module" {id_attr} role="button" tabindex="0" data-cg-card="{_module_card_payload(module)}">'
                f"<title>{html.esc(full)}</title>"
                f'<rect x="{x}" y="{y}" width="{box_w}" height="{height}" rx="6" ry="6" '
                f'fill="{html.esc(fill)}" stroke="{html.esc(stroke)}" stroke-width="1.5"/>'
                f'<text x="{x + _BOX_PAD_X}" y="{y + 20}" class="cg-svg-label">{html.esc(visible)}</text>'
                f"{chip}"
                f"</g>"
                f"{extra}"
            )
            x += box_w + _GAP_X
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


def _chip_width(word: str) -> int:
    return max(50, 18 + code_export.svg_text_width(word, font_size=8))


def _module_box_parts(
    label: str,
    symbol_count: int,
    box_x: int,
    box_y: int,
    box_h: int,
    changed: bool,
) -> tuple[int, str, str, str]:
    count_text = f" ({symbol_count})" if symbol_count else ""
    full_label = f"{label}{count_text}"
    chip_w = _chip_width("CHANGED") if changed else 0
    right = chip_w + _CHIP_GAP + _BOX_PAD_X if chip_w else _BOX_PAD_X
    text_w = code_export.svg_text_width(full_label)
    needed = _BOX_PAD_X + text_w + right
    box_w = max(_BOX_MIN_W, min(_BOX_MAX_W, needed))
    budget = max(8, box_w - _BOX_PAD_X - right)
    visible, full = code_export.fit_svg_label(full_label, budget)
    chip = _status_chip(box_x, box_y, box_h, box_w, "CHANGED") if changed else ""
    return box_w, visible, full, chip


def _status_chip(box_x: int, box_y: int, box_h: int, box_w: int, word: str) -> str:
    color, icon = _STATUS_WARNING, "!"
    chip_w = _chip_width(word)
    chip_x = box_x + box_w - chip_w - _BOX_PAD_X
    chip_y = box_y + box_h - 24
    word_x = chip_x + 16 + (chip_w - 20) / 2
    return (
        f'<rect x="{chip_x}" y="{chip_y}" width="{chip_w}" height="{_CHIP_H}" rx="8" ry="8" fill="{html.esc(_SURFACE)}" '
        f'stroke="{html.esc(color)}" stroke-width="1.2"/>'
        f'<circle cx="{chip_x + 8}" cy="{chip_y + 8}" r="4" fill="{html.esc(color)}"/>'
        f'<text x="{chip_x + 8}" y="{chip_y + 11}" text-anchor="middle" class="cg-svg-chip-icon">{html.esc(icon)}</text>'
        f'<text x="{word_x}" y="{chip_y + 11}" text-anchor="middle" class="cg-svg-chip-word">{html.esc(word)}</text>'
    )


def _changed_module_shortcut(
    export: dict,
    *,
    show_worktrees: bool,
    show_full_map: bool,
) -> str:
    changed_ids = _changed_module_ids(export)
    if not changed_ids:
        return ""
    module_map = export.get("module_map") if isinstance(export.get("module_map"), dict) else {}
    labels = {
        str(row.get("id") or ""): str(row.get("label") or row.get("id") or "")
        for row in module_map.get("modules", [])
        if isinstance(row, dict)
    }
    items: list[str] = []
    for module_id in changed_ids:
        if not show_worktrees and any(part in _NONCANONICAL_ROOTS for part in _path_parts(module_id)):
            continue
        label = labels.get(module_id) or module_id
        href = _code_href(symbol=module_id, show_full_map=show_full_map, show_worktrees=show_worktrees)
        items.append(f'<li><a href="{html.esc(href)}">{html.esc(label)}</a></li>')
    if not items:
        return ""
    return (
        '<div class="cg-impact-shortcut" data-cg-changed-shortcut="1">'
        f"<p>{html.esc('Start from changed modules')}</p>"
        f"<ul>{''.join(items)}</ul>"
        "</div>"
    )


def _render_impact(
    export: dict,
    symbol: str | None,
    *,
    show_worktrees: bool,
    show_full_map: bool,
) -> str:
    search_form = (
        '<form class="cg-search" method="get" action="/view/code">'
        f"<label>{html.esc('Symbol or file')} "
        f'<input type="search" name="symbol" value="{html.esc(symbol or "")}" '
        f'placeholder="{html.esc("e.g. dispatch or src/brigade/proc.py")}" '
        f'aria-label="{html.esc("Symbol search")}"></label>'
        f'<button type="submit">{html.esc("Show impact")}</button>'
        "</form>"
    )
    shortcut = _changed_module_shortcut(export, show_worktrees=show_worktrees, show_full_map=show_full_map)

    impact = export.get("impact") if isinstance(export.get("impact"), dict) else None
    if not symbol:
        return html.panel(
            html.esc("Impact"),
            search_form
            + shortcut
            + f"<p>{html.esc('Search for a symbol to see callers, blast radius, and attributed tests.')}</p>",
        )
    if not impact:
        return html.panel(
            html.esc("Impact"),
            search_form + shortcut + f"<p>{html.esc('No impact data for that symbol.')}</p>",
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
        shortcut,
        f'<p class="cg-impact-intro">{html.esc(intro)}</p>',
        _impact_diagram(plain_name, callers, edges),
        _impact_table("Direct callers", callers, limit=20),
        _impact_table("Impact set", edges, limit=40),
        _affected_tests_table(affected),
        _impact_debug(impact),
    ]
    return html.panel(html.esc("Impact"), "".join(sections))


def _impact_diagram(focus: str, callers: list, edges: list) -> str:
    caller_rows = [row for row in callers if isinstance(row, dict)]
    edge_rows = [row for row in edges if isinstance(row, dict)]
    shown_callers = caller_rows[:_IMPACT_CALLER_CAP]
    shown_edges = edge_rows[:_IMPACT_EDGE_CAP]
    trunc_note = ""
    hidden_callers = max(0, len(caller_rows) - len(shown_callers))
    hidden_edges = max(0, len(edge_rows) - len(shown_edges))
    if hidden_callers or hidden_edges:
        bits: list[str] = []
        if hidden_callers:
            bits.append(f"showing top {len(shown_callers)} callers, {hidden_callers} hidden")
        if hidden_edges:
            bits.append(f"showing top {len(shown_edges)} impact edges, {hidden_edges} hidden")
        trunc_note = f'<p class="cg-muted" data-cg-impact-truncation="1">{html.esc("; ".join(bits))}</p>'
    layers = [
        ("Callers", shown_callers),
        ("Focus", [{"source": row.get("source"), "target": focus} for row in shown_callers[:1]] or [{"target": focus}]),
        ("Impact", shown_edges),
    ]
    width = 720
    height = 200
    parts = [
        trunc_note,
        f'<svg class="cg-impact-diagram" viewBox="0 0 {width} {height}" width="100%" role="img" '
        f'aria-label="{html.esc("Impact layers")}">',
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
.cg-banner {{
  margin:0 0 0.75rem; padding:0.65rem 0.85rem; border:1px solid {_CHANGED_STROKE};
  border-radius:0.35rem; background:{_CHANGED_FILL}; color:{_TEXT_PRIMARY};
}}
.cg-toggles {{ display:flex; flex-wrap:wrap; gap:0.5rem; margin:0 0 0.75rem; }}
.cg-toggle {{
  font:inherit; padding:0.3rem 0.65rem; border:1px solid {_BOX_STROKE}; border-radius:0.25rem;
  background:{_SURFACE}; color:{_TEXT_PRIMARY}; text-decoration:none;
}}
.cg-impact-shortcut {{
  margin:0 0 1rem; padding:0.65rem 0.85rem; border:1px solid {_BORDER};
  border-radius:0.35rem; background:{_SURFACE};
}}
.cg-impact-shortcut p {{ margin:0 0 0.35rem; font-weight:600; }}
.cg-impact-shortcut ul {{ margin:0; padding-left:1.2rem; }}
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
