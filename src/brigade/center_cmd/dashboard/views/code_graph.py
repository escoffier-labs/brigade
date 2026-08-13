"""Code Graph dashboard view — module map, impact drill-down, change overlay.

Contract-only: ``brigade code export --json`` (and ``--symbol`` for impact).
No direct graph database reads.
"""

from __future__ import annotations

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

_COLS = 6
_BOX_W = 132
_BOX_MIN_H = 36
_BOX_MAX_H = 88
_GAP_X = 28
_GAP_Y = 22
_PAD = 20


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
    trunc = module_map.get("truncation") if isinstance(module_map.get("truncation"), dict) else {}
    modules = module_map.get("modules") if isinstance(module_map.get("modules"), list) else []
    overlay = export.get("change_overlay") if isinstance(export.get("change_overlay"), dict) else {}

    symbols = stats.get("symbols")
    files = stats.get("files")
    sentences: list[str] = []
    if isinstance(symbols, int) and isinstance(files, int):
        sentences.append(f"This graph indexes {symbols:,} symbols across {files:,} files.")
    else:
        sentences.append("This graph summarizes how code is connected in the repository.")

    changed = overlay.get("changed_modules") if isinstance(overlay.get("changed_modules"), list) else []
    if changed:
        sentences.append(f"{len(changed)} module(s) include files changed on this branch.")
    elif modules:
        top = max(modules, key=lambda row: int(row.get("symbol_count") or 0) if isinstance(row, dict) else 0)
        if isinstance(top, dict):
            sentences.append(f"Largest module by symbol count: {top.get('label') or top.get('id')}.")

    note = trunc.get("note")
    if isinstance(note, str) and note.strip():
        sentences.append(note.strip())

    body = " ".join(sentences[:3])
    return (
        f'<section class="cg-summary" aria-label="{html.esc("Code graph summary")}"><p>{html.esc(body)}</p></section>'
    )


def _render_module_map(export: dict) -> str:
    module_map = export.get("module_map") if isinstance(export.get("module_map"), dict) else {}
    modules = [row for row in module_map.get("modules", []) if isinstance(row, dict)]
    edges = [row for row in module_map.get("edges", []) if isinstance(row, dict)]
    trunc = module_map.get("truncation") if isinstance(module_map.get("truncation"), dict) else {}

    if not modules:
        return html.panel(html.esc("Module map"), f"<p>{html.esc('Nothing here.')}</p>")

    trunc_note = ""
    if isinstance(trunc.get("note"), str) and trunc["note"].strip():
        trunc_note = f'<p class="cg-muted">{html.esc(trunc["note"])}</p>'

    return (
        _summary_strip(export)
        + html.panel(html.esc("Module map"), trunc_note + _module_map_svg(modules, edges))
        + _debug_details(export)
    )


def _module_map_svg(modules: list[dict], edges: list[dict]) -> str:
    if not modules:
        return f"<p>{html.esc('Nothing here.')}</p>"

    positions: dict[str, tuple[int, int, int, int]] = {}
    max_symbols = max(int(row.get("symbol_count") or 0) for row in modules) or 1
    cols = min(_COLS, max(1, len(modules)))
    rows = (len(modules) + cols - 1) // cols

    boxes: list[str] = []
    for index, module in enumerate(modules):
        col = index % cols
        row = index // cols
        x = _PAD + col * (_BOX_W + _GAP_X)
        y = _PAD + row * (_BOX_MAX_H + _GAP_Y)
        symbol_count = int(module.get("symbol_count") or 0)
        ratio = symbol_count / max_symbols
        height = int(_BOX_MIN_H + ratio * (_BOX_MAX_H - _BOX_MIN_H))
        module_id = str(module.get("id") or "")
        label = str(module.get("label") or module_id)
        visible, full = code_export.fit_svg_label(label, _BOX_W - 16)
        count_text = f" ({symbol_count})" if symbol_count else ""
        fill = _CHANGED_FILL if module.get("changed") else _BOX_FILL
        stroke = _CHANGED_STROKE if module.get("changed") else _BOX_STROKE
        chip = _status_chip(x, y, height, "CHANGED" if module.get("changed") else "OK", module.get("changed") is True)
        positions[module_id] = (x, y, _BOX_W, height)
        boxes.append(
            f'<g class="cg-module" role="img">'
            f"<title>{html.esc(full)}{html.esc(count_text)}</title>"
            f'<rect x="{x}" y="{y}" width="{_BOX_W}" height="{height}" rx="6" ry="6" '
            f'fill="{html.esc(fill)}" stroke="{html.esc(stroke)}" stroke-width="1.5"/>'
            f'<text x="{x + 8}" y="{y + 20}" class="cg-svg-label">{html.esc(visible)}{html.esc(count_text)}</text>'
            f"{chip}"
            f"</g>"
        )

    edge_lines: list[str] = []
    for edge in edges:
        source = str(edge.get("from") or "")
        target = str(edge.get("to") or "")
        if source not in positions or target not in positions:
            continue
        sx, sy, sw, sh = positions[source]
        tx, ty, tw, th = positions[target]
        x1, y1 = sx + sw, sy + sh // 2
        x2, y2 = tx, ty + th // 2
        weight = int(edge.get("weight") or 0)
        edge_lines.append(
            f'<g class="cg-edge"><title>{html.esc(f"{source} → {target} ({weight})")}</title>'
            f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{html.esc(_BOX_STROKE)}" '
            f'stroke-width="{min(4, 1 + weight // 3)}" marker-end="url(#cg-arrow)"/></g>'
        )

    width = _PAD * 2 + cols * _BOX_W + max(0, cols - 1) * _GAP_X
    height = _PAD * 2 + rows * _BOX_MAX_H + max(0, rows - 1) * _GAP_Y

    return (
        f'<div class="cg-map-wrap"><svg class="cg-map" viewBox="0 0 {width} {height}" width="100%" '
        f'role="img" aria-label="{html.esc("Module map")}">'
        "<defs>"
        '<marker id="cg-arrow" viewBox="0 0 10 10" refX="9" refY="5" '
        'markerWidth="6" markerHeight="6" orient="auto-start-reverse">'
        f'<path d="M 0 0 L 10 5 L 0 10 z" fill="{html.esc(_BOX_STROKE)}"/>'
        "</marker></defs>" + "".join(edge_lines) + "".join(boxes) + "</svg></div>"
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
.cg-muted {{ color:{_TEXT_SECONDARY}; font-size:0.9rem; }}
.cg-map-wrap {{ overflow-x:auto; max-width:100%; }}
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
  document.addEventListener("click", function (e) {{
    var t = e.target;
    if (!t || !t.getAttribute) return;
    var mode = t.getAttribute("data-cg-mode");
    if (mode) {{ e.preventDefault(); selectMode(mode); }}
  }});
  var params = new URLSearchParams(window.location.search || "");
  selectMode(params.get("symbol") ? "impact" : "map");
}})();
</script>"""
