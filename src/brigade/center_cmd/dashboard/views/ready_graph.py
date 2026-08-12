"""Ready-set dependency graph dashboard view.

Renders an SVG dependency graph from ``brigade work ready --explain --json``:
ready nodes, blocked nodes with blocker edges, and parent-child edges as a
secondary stroke. No frontend framework — layered layout plus inline SVG,
styled through a nonced ``<style>`` block (CSP has no ``unsafe-inline``).
"""

from __future__ import annotations

from pathlib import Path

from brigade.center_cmd.dashboard import render as html
from brigade.center_cmd.dashboard import work_graph as wg

NAME = "graph"
TITLE = "Graph"
ORDER = 8

_NODE_WIDTH = 160
_NODE_HEIGHT = 52
_COL_GAP = 72
_ROW_GAP = 28
_PAD_X = 24
_PAD_Y = 24


def fetch(target: Path) -> dict:
    return wg.fetch_ready(target)


def render(payload: dict, nonce: str) -> str:
    if payload.get("error"):
        return html.error_panel(TITLE, str(payload["error"]))

    nodes = wg.graph_nodes(payload)
    if not nodes:
        return html.panel(html.esc(TITLE), f"<p>{html.esc('Nothing here.')}</p>")

    edges = wg.dependency_edges(payload)
    layers = wg.layout_layers(nodes, edges)
    svg = _render_svg(layers, edges)
    summary = _summary_panel(payload, nodes, edges)
    legend = _legend()
    return f"{_stylesheet(nonce)}{summary}{html.panel(html.esc(TITLE), f'{legend}{svg}')}"


def _summary_panel(payload: dict, nodes: list[dict], edges: list[dict]) -> str:
    ready_count = payload.get("ready_count")
    if ready_count is None:
        ready_count = sum(1 for node in nodes if node.get("kind") == "ready")
    blocked_count = payload.get("blocked_count")
    if blocked_count is None:
        blocked_count = sum(1 for node in nodes if node.get("kind") == "blocked")
    rows = [
        [html.esc("Ready"), html.esc(ready_count)],
        [html.esc("Blocked"), html.esc(blocked_count)],
        [html.esc("Cycles"), html.esc(payload.get("cycle_count", 0))],
        [html.esc("Drawn edges"), html.esc(len(edges))],
        [html.esc("Nodes"), html.esc(len(nodes))],
    ]
    return html.panel(
        html.esc("Ready set"),
        html.table([html.esc("Signal"), html.esc("Value")], rows),
    )


def _legend() -> str:
    return (
        '<ul class="wg-legend">'
        f'<li><span class="wg-swatch wg-swatch-ready"></span>{html.esc("Ready")}</li>'
        f'<li><span class="wg-swatch wg-swatch-blocked"></span>{html.esc("Blocked")}</li>'
        f'<li><span class="wg-swatch wg-swatch-other"></span>{html.esc("Related")}</li>'
        f'<li><span class="wg-edge-sample wg-edge-blocks"></span>{html.esc("blocks")}</li>'
        f'<li><span class="wg-edge-sample wg-edge-parent"></span>{html.esc("parent-child")}</li>'
        "</ul>"
    )


def _stylesheet(nonce: str) -> str:
    return f"""<style nonce="{html.esc(nonce)}">
.wg-legend {{
  list-style: none;
  margin: 0 0 1rem;
  padding: 0;
  display: flex;
  flex-wrap: wrap;
  gap: 0.75rem 1.25rem;
  font-size: 0.85rem;
}}
.wg-legend li {{
  display: flex;
  align-items: center;
  gap: 0.4rem;
}}
.wg-swatch {{
  display: inline-block;
  width: 0.9rem;
  height: 0.9rem;
  border: 1px solid #333;
  border-radius: 0.15rem;
}}
.wg-swatch-ready {{ background: #d9f2e3; }}
.wg-swatch-blocked {{ background: #f8e0e0; }}
.wg-swatch-other {{ background: #ececec; }}
.wg-edge-sample {{
  display: inline-block;
  width: 1.4rem;
  height: 0;
  border-top: 2px solid #333;
}}
.wg-edge-blocks {{ border-top-color: #8b0000; }}
.wg-edge-parent {{
  border-top-style: dashed;
  border-top-color: #666;
}}
svg.wg-graph {{
  display: block;
  max-width: 100%;
  height: auto;
  background: #fafafa;
  border: 1px solid #ddd;
}}
.wg-node-ready rect {{ fill: #d9f2e3; stroke: #1a7f4b; }}
.wg-node-blocked rect {{ fill: #f8e0e0; stroke: #8b0000; }}
.wg-node-other rect {{ fill: #ececec; stroke: #666; }}
.wg-node text {{
  font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  font-size: 11px;
  fill: #111;
}}
.wg-node .wg-id {{
  font-weight: 700;
  font-size: 10px;
}}
.wg-edge-blocks {{
  stroke: #8b0000;
  stroke-width: 1.75;
  fill: none;
}}
.wg-edge-parent-child {{
  stroke: #666;
  stroke-width: 1.25;
  stroke-dasharray: 4 3;
  fill: none;
}}
</style>"""


def _short_id(task_id: str) -> str:
    if len(task_id) <= 18:
        return task_id
    return f"{task_id[:8]}…{task_id[-6:]}"


def _short_title(title: str, limit: int = 28) -> str:
    text = " ".join(title.split())
    if len(text) <= limit:
        return text
    return f"{text[: limit - 1]}…"


def _positions(layers: list[list[dict]]) -> dict[str, tuple[float, float]]:
    positions: dict[str, tuple[float, float]] = {}
    for col, layer in enumerate(layers):
        for row, node in enumerate(layer):
            x = _PAD_X + col * (_NODE_WIDTH + _COL_GAP)
            y = _PAD_Y + row * (_NODE_HEIGHT + _ROW_GAP)
            positions[str(node["id"])] = (x, y)
    return positions


def _render_svg(layers: list[list[dict]], edges: list[dict[str, str]]) -> str:
    positions = _positions(layers)
    cols = max(len(layers), 1)
    rows = max((len(layer) for layer in layers), default=1)
    width = _PAD_X * 2 + cols * _NODE_WIDTH + max(cols - 1, 0) * _COL_GAP
    height = _PAD_Y * 2 + rows * _NODE_HEIGHT + max(rows - 1, 0) * _ROW_GAP

    edge_parts: list[str] = []
    for edge in edges:
        source = positions.get(edge["source"])
        target = positions.get(edge["target"])
        if source is None or target is None:
            continue
        x1 = source[0] + _NODE_WIDTH
        y1 = source[1] + _NODE_HEIGHT / 2
        x2 = target[0]
        y2 = target[1] + _NODE_HEIGHT / 2
        # Straight when same row-ish; gentle cubic otherwise.
        mid = (x1 + x2) / 2
        path = f"M {x1:.1f} {y1:.1f} C {mid:.1f} {y1:.1f}, {mid:.1f} {y2:.1f}, {x2:.1f} {y2:.1f}"
        edge_class = "wg-edge-blocks" if edge["type"] == "blocks" else "wg-edge-parent-child"
        marker = "url(#wg-arrow-blocks)" if edge["type"] == "blocks" else "url(#wg-arrow-parent)"
        edge_parts.append(f'<path class="{edge_class}" d="{path}" marker-end="{marker}" />')

    node_parts: list[str] = []
    for node in (item for layer in layers for item in layer):
        task_id = str(node["id"])
        x, y = positions[task_id]
        kind = str(node.get("kind") or "other")
        title = _short_title(str(node.get("title") or ""))
        label_id = _short_id(task_id)
        tip = f"{task_id}: {node.get('title') or ''}".strip()
        # SVG <title> is a text child, not an attribute — still escape.
        node_parts.append(
            f'<g class="wg-node wg-node-{html.esc(kind)}" transform="translate({x:.1f} {y:.1f})">'
            f"<title>{html.esc(tip)}</title>"
            f'<rect width="{_NODE_WIDTH}" height="{_NODE_HEIGHT}" rx="4" ry="4" />'
            f'<text class="wg-id" x="8" y="18">{html.esc(label_id)}</text>'
            f'<text x="8" y="36">{html.esc(title or "—")}</text>'
            f"</g>"
        )

    return (
        f'<svg class="wg-graph" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}" role="img" '
        f'aria-label="{html.esc("Work dependency graph")}">'
        "<defs>"
        '<marker id="wg-arrow-blocks" viewBox="0 0 10 10" refX="9" refY="5" '
        'markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
        '<path d="M 0 0 L 10 5 L 0 10 z" fill="#8b0000" />'
        "</marker>"
        '<marker id="wg-arrow-parent" viewBox="0 0 10 10" refX="9" refY="5" '
        'markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
        '<path d="M 0 0 L 10 5 L 0 10 z" fill="#666666" />'
        "</marker>"
        "</defs>"
        f"{''.join(edge_parts)}{''.join(node_parts)}"
        "</svg>"
    )
