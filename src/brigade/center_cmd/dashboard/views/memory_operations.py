"""Memory Operations dashboard view (topology + inventory).

Contract-only: fetch via ``data.run_json`` for
``brigade memory topology --json`` and paginated
``brigade memory inventory --json``. No filesystem, cron, or body reads.

Operator-facing copy uses plain language; raw node/edge/receipt ids stay in
``<details>`` expanders or ``title`` tooltips for debugging.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from brigade.center_cmd.dashboard import data
from brigade.center_cmd.dashboard import render as html

NAME = "memory"
TITLE = "Memory Operations"
ORDER = 3

_INVENTORY_LIMIT = 500
_CLIENT_PAGE_SIZE = 50
_TAG_CHIP_LIMIT = 5

# Status palette (dataviz reserved): always paired with icon + word, never color alone.
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

_HEALTH_KEYS = (
    ("care_scan", "Care scan"),
    ("refresh_queue", "Refresh queue"),
    ("handoff_backlog", "Handoff backlog"),
    ("quarantine", "Quarantine"),
    ("closeout", "Closeout"),
    ("evidence_projection", "Evidence projection"),
)

_HARNESS_ORDER = ("claude", "codex", "grok", "hermes")
_CANONICAL_ORDER = ("cards", "rules", "tools", "user", "learnings")
_CANONICAL_LABELS = {
    "cards": "Canonical cards",
    "rules": "Rules",
    "tools": "TOOLS.md",
    "user": "USER.md",
    "learnings": "Learnings",
}

_FLAG_PLAIN = {
    "duplicate_enabled_writer": "Two writers can update the same store",
    "duplicate_enabled_queue_producer": "Two producers feed the same queue",
    "missing_owner": "A memory node has no owner",
    "missing_closeout": "Care work is open without a closeout record",
    "stale_receipt": "A writer receipt is older than its cadence",
    "cycle": "Flow loop detected",
    "disabled": "A scheduled care job is disabled",
}


def fetch(target: Path) -> dict:
    topology = data.run_json(target, ["memory", "topology"])
    inventory = _fetch_inventory(target)
    return {"topology": topology, "inventory": inventory}


def render(payload: dict, nonce: str) -> str:
    topology = payload.get("topology") if isinstance(payload.get("topology"), dict) else {}
    inventory = payload.get("inventory") if isinstance(payload.get("inventory"), dict) else {}

    parts = [
        _stylesheet(nonce),
        _mode_tabs(),
        (
            '<div id="mo-topology" class="mo-panel" data-mo-panel="topology" '
            'role="tabpanel" aria-labelledby="mo-tab-topology">'
        ),
        _render_topology(topology, inventory),
        "</div>",
        (
            '<div id="mo-inventory" class="mo-panel" data-mo-panel="inventory" '
            'role="tabpanel" aria-labelledby="mo-tab-inventory">'
        ),
        _render_inventory(inventory),
        "</div>",
        _script(nonce),
    ]
    return "".join(parts)


def _fetch_inventory(target: Path) -> dict:
    items: list[dict[str, Any]] = []
    offset = 0
    seen_offsets: set[int] = set()
    last_error: str | None = None
    contract_total: int | None = None
    partial = False
    partial_reason: str | None = None

    while True:
        if offset in seen_offsets:
            partial = True
            partial_reason = "Inventory is partial: pagination repeated an offset."
            break
        seen_offsets.add(offset)
        page = data.run_json(
            target,
            [
                "memory",
                "inventory",
                "--offset",
                str(offset),
                "--limit",
                str(_INVENTORY_LIMIT),
            ],
        )
        if page.get("error"):
            last_error = str(page["error"])
            if items:
                partial = True
                partial_reason = (
                    f"Inventory is partial: page command failed after loading {len(items)} item(s) ({last_error})."
                )
            break

        page_items = page.get("items")
        if isinstance(page_items, list):
            for item in page_items:
                if isinstance(item, dict):
                    items.append(item)

        pagination = page.get("pagination") if isinstance(page.get("pagination"), dict) else {}
        total_val = pagination.get("total")
        if isinstance(total_val, int):
            contract_total = total_val
        has_more = bool(pagination.get("has_more"))
        if not has_more:
            break
        next_offset = pagination.get("next_offset")
        if not isinstance(next_offset, int) or next_offset <= offset:
            partial = True
            if isinstance(contract_total, int):
                partial_reason = (
                    f"Inventory is partial: loaded {len(items)} of {contract_total} (pagination did not advance)."
                )
            else:
                partial_reason = f"Inventory is partial: loaded {len(items)} item(s) (pagination did not advance)."
            break
        offset = next_offset

    result: dict[str, Any] = {
        "items": items,
    }
    if isinstance(contract_total, int):
        result["contract_total"] = contract_total
        result["total"] = contract_total
    elif not partial:
        result["total"] = len(items)
    else:
        result["loaded"] = len(items)

    if partial:
        result["partial"] = True
        result["loaded"] = len(items)
        if partial_reason:
            result["warning"] = partial_reason
        elif last_error is not None:
            result["warning"] = f"Inventory is partial: loaded {len(items)} item(s) ({last_error})."
        else:
            result["warning"] = f"Inventory is partial: loaded {len(items)} item(s)."
    elif last_error is not None and not items:
        result["error"] = last_error
    elif last_error is not None:
        result["warning"] = last_error
        result["partial"] = True
        result["loaded"] = len(items)
    return result


def _mode_tabs() -> str:
    return (
        '<div class="mo-modes" role="tablist" aria-label="' + html.esc("Memory Operations modes") + '">'
        '<a href="#mo-topology" id="mo-tab-topology" class="mo-mode" role="tab" '
        'data-mo-mode="topology" aria-selected="true" aria-controls="mo-topology" '
        'tabindex="0">' + html.esc("Topology") + "</a>"
        '<a href="#mo-inventory" id="mo-tab-inventory" class="mo-mode" role="tab" '
        'data-mo-mode="inventory" aria-selected="false" aria-controls="mo-inventory" '
        'tabindex="-1">' + html.esc("Inventory") + "</a>"
        "</div>"
    )


def _render_topology(topology: dict, inventory: dict) -> str:
    if topology.get("error"):
        return html.error_panel("Topology", str(topology["error"]))

    health = topology.get("health") if isinstance(topology.get("health"), dict) else {}
    flags = topology.get("flags") if isinstance(topology.get("flags"), list) else []
    paths = topology.get("paths") if isinstance(topology.get("paths"), list) else []
    nodes = topology.get("nodes") if isinstance(topology.get("nodes"), list) else []
    edges = topology.get("edges") if isinstance(topology.get("edges"), list) else []

    if not health and not flags and not paths and not nodes and not edges:
        return html.panel(html.esc("Topology"), f"<p>{html.esc('Nothing here.')}</p>")

    dest_counts = _destination_counts(inventory)
    sections = [
        _summary_strip(health, nodes, flags),
        html.panel(html.esc("Health"), _health_tiles(health)),
        html.panel(html.esc("Pipeline"), _pipeline_svg(nodes, edges, paths, dest_counts)),
        html.panel(html.esc("Attention"), _flags_block(flags)),
        _debug_details(nodes, edges),
    ]
    return "".join(sections)


def _destination_counts(inventory: dict) -> dict[str, int]:
    counts: dict[str, int] = {}
    items = inventory.get("items") if isinstance(inventory.get("items"), list) else []
    for item in items:
        if not isinstance(item, dict):
            continue
        dest = item.get("logical_destination")
        if dest in (None, ""):
            continue
        key = str(dest)
        counts[key] = counts.get(key, 0) + 1
    return counts


def _node_by_id(nodes: list) -> dict[str, dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for node in nodes:
        if isinstance(node, dict) and node.get("id") is not None:
            by_id[str(node["id"])] = node
    return by_id


def _edge_lookup(edges: list) -> dict[str, dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for edge in edges:
        if isinstance(edge, dict) and edge.get("id") is not None:
            by_id[str(edge["id"])] = edge
    return by_id


def _status_role(raw: object) -> tuple[str, str, str, str]:
    """Map contract status to (role, word, icon, color)."""
    status = str(raw or "unknown").strip().lower()
    if status in {"ok", "healthy", "present", "enabled", "canonical", "fresh"}:
        return ("good", "OK", "\u2713", _STATUS_GOOD)
    if status in {"warn", "warning", "unknown", "manual", "disabled", "degraded"}:
        return ("warning", "WARN", "!", _STATUS_WARNING)
    if status in {"timeout", "timed_out"}:
        return ("serious", "TIMEOUT", "!", _STATUS_SERIOUS)
    if status in {"missing"}:
        return ("serious", "MISSING", "!", _STATUS_SERIOUS)
    if status in {"stale"}:
        return ("serious", "STALE", "!", _STATUS_SERIOUS)
    if status in {"error", "fail", "failed", "critical"}:
        return ("critical", "FAILED", "\u2717", _STATUS_CRITICAL)
    return ("warning", "WARN", "?", _STATUS_WARNING)


def _health_word(raw: object) -> str:
    """Full operator status word for health tiles (not truncated SVG chips)."""
    status = str(raw or "unknown").strip().lower()
    if status in {"ok", "healthy", "present", "enabled", "canonical", "fresh"}:
        return "OK"
    if status in {"warn", "warning", "unknown", "manual", "disabled", "degraded"}:
        return "ATTENTION"
    if status in {"timeout", "timed_out"}:
        return "TIMED OUT"
    if status in {"missing"}:
        return "MISSING"
    if status in {"stale"}:
        return "STALE"
    if status in {"error", "fail", "failed", "critical"}:
        return "FAILED"
    return status.replace("_", " ").upper() or "UNKNOWN"


def _summary_strip(health: dict, nodes: list, flags: list) -> str:
    sentences: list[str] = []
    care = health.get("care_scan") if isinstance(health.get("care_scan"), dict) else {}
    refresh = health.get("refresh_queue") if isinstance(health.get("refresh_queue"), dict) else {}
    backlog = health.get("handoff_backlog") if isinstance(health.get("handoff_backlog"), dict) else {}
    evidence = health.get("evidence_projection") if isinstance(health.get("evidence_projection"), dict) else {}
    closeout = health.get("closeout") if isinstance(health.get("closeout"), dict) else {}

    issue_count = care.get("issue_count")
    queued = refresh.get("count")
    if isinstance(issue_count, int) and issue_count > 0:
        noun = "card is" if issue_count == 1 else "cards are"
        sentences.append(f"{issue_count} {noun} waiting on care review.")
    elif isinstance(queued, int) and queued > 0:
        noun = "item is" if queued == 1 else "items are"
        sentences.append(f"{queued} refresh-queue {noun} waiting.")
    else:
        sentences.append("No cards are waiting on care review.")

    pending = backlog.get("pending")
    by_id = _node_by_id(nodes)
    if not isinstance(pending, int):
        pending = 0
        for harness in _HARNESS_ORDER:
            inbox = by_id.get(f"inbox:{harness}")
            if not isinstance(inbox, dict):
                continue
            counts = inbox.get("counts") if isinstance(inbox.get("counts"), dict) else {}
            value = counts.get("pending")
            if isinstance(value, int):
                pending += value
    if pending == 0:
        sentences.append("Handoff inboxes are empty.")
    else:
        noun = "handoff is" if pending == 1 else "handoffs are"
        sentences.append(f"{pending} {noun} waiting in inboxes.")

    ev_status = str(evidence.get("status") or "").lower()
    if ev_status in {"timeout", "timed_out"}:
        sentences.append("Evidence projection timed out, so evidence-linked data below may be stale.")
    elif ev_status in {"missing", "error", "fail", "failed"}:
        sentences.append("Evidence projection is unavailable; evidence-linked data may be incomplete.")
    elif str(closeout.get("status") or "").lower() == "missing" and (
        (isinstance(issue_count, int) and issue_count > 0) or (isinstance(queued, int) and queued > 0)
    ):
        sentences.append("Care work is open, but there is no closeout record yet.")
    elif flags:
        sentences.append(f"{len(flags)} attention flag(s) need a look.")
    else:
        sentences.append("Evidence projection looks healthy.")

    body = " ".join(sentences[:3])
    return (
        f'<section class="mo-summary" data-mo-summary="1" aria-label="{html.esc("Memory health summary")}">'
        f"<p>{html.esc(body)}</p>"
        "</section>"
    )


def _health_meaning(key: str, block: dict) -> str:
    status = str(block.get("status") or "unknown").lower()
    if key == "care_scan":
        count = block.get("issue_count")
        if status == "ok" and (count in (0, None)):
            return "No care issues right now."
        if isinstance(count, int) and count > 0:
            return f"{count} card(s) need care review; run brigade memory care scan."
        return "Care scan needs attention; run brigade memory care scan."
    if key == "refresh_queue":
        count = block.get("count")
        if status == "ok" and (count in (0, None)):
            return "Refresh queue is clear."
        if isinstance(count, int) and count > 0:
            return f"{count} item(s) queued; run brigade memory care plan-fixes."
        return "Refresh queue needs attention; run brigade memory care plan-fixes."
    if key == "handoff_backlog":
        pending = block.get("pending")
        if pending in (0, None) and status == "ok":
            return "No pending handoffs waiting to ingest."
        if isinstance(pending, int) and pending > 0:
            return f"{pending} handoff(s) waiting; run brigade ingest."
        return "Handoff backlog needs a look."
    if key == "quarantine":
        if status == "unknown":
            return "Quarantine count is not available from a public source."
        count = block.get("count")
        if isinstance(count, int):
            return f"Quarantine holds {count} item(s)."
        return "Quarantine status reported without a count."
    if key == "closeout":
        if status == "missing":
            return "No memory-care closeout on file; finish or record a care pass."
        return "Closeout record is present."
    if key == "evidence_projection":
        if status in {"timeout", "timed_out"}:
            return "Dashboard data may be stale; run brigade evidence status."
        if status in {"ok", "healthy"}:
            return "Evidence projection is current."
        return "Evidence projection needs a check; run brigade evidence status."
    return "See topology details for more."


def _health_tiles(health: dict) -> str:
    tiles: list[str] = []
    for key, label in _HEALTH_KEYS:
        block = health.get(key) if isinstance(health.get(key), dict) else {}
        status = block.get("status", "unknown")
        role, _word, icon, color = _status_role(status)
        meaning = _health_meaning(key, block)
        debug_bits: list[str] = []
        for detail_key in (
            "issue_count",
            "count",
            "pending",
            "draft_pending",
            "draft_reviewed",
            "closeout_id",
            "created_at",
            "healthy",
            "capability",
        ):
            if detail_key in block and block.get(detail_key) is not None:
                debug_bits.append(f"{detail_key}={block.get(detail_key)}")
        debug = (
            f'<details class="mo-debug"><summary>{html.esc("Raw signal")}</summary>'
            f"<code>{html.esc(', '.join(debug_bits) if debug_bits else '-')}</code></details>"
            if debug_bits
            else ""
        )
        tiles.append(
            f'<article class="mo-health-tile mo-health-{html.esc(role)}" data-mo-health="{html.esc(key)}">'
            f'<div class="mo-health-head">'
            f'<span class="mo-chip-icon" aria-hidden="true" style="background:{html.esc(color)}">'
            f"{html.esc(icon)}</span>"
            f'<span class="mo-health-label">{html.esc(label)}</span>'
            f'<span class="mo-chip-word">{html.esc(_health_word(status))}</span>'
            f"</div>"
            f'<p class="mo-health-meaning">{html.esc(meaning)}</p>'
            f"{debug}"
            "</article>"
        )
    if not tiles:
        return f"<p>{html.esc('Nothing here.')}</p>"
    return f'<div class="mo-health-tiles">{"".join(tiles)}</div>'


def _human_harness(name: str) -> str:
    mapping = {"claude": "Claude", "codex": "Codex", "grok": "Grok", "hermes": "Hermes"}
    return mapping.get(name.lower(), name[:1].upper() + name[1:])


def _count_label(node: dict | None, dest_counts: dict[str, int], node_id: str) -> str:
    if node_id.startswith("canonical:"):
        key = node_id.split(":", 1)[1]
        if key in dest_counts:
            return str(dest_counts[key])
    if not isinstance(node, dict):
        return ""
    counts = node.get("counts") if isinstance(node.get("counts"), dict) else {}
    for key in ("pending", "issue_count", "queued", "record_count", "processed"):
        if isinstance(counts.get(key), int):
            if key == "processed":
                continue
            return str(counts[key])
    return ""


def _svg_box(
    x: int,
    y: int,
    w: int,
    h: int,
    label: str,
    count: str,
    chip_word: str,
    chip_color: str,
    chip_icon: str,
    title: str,
) -> str:
    sub = f" ({count})" if count else ""
    text = f"{label}{sub}"
    tip = html.esc(title)
    chip_w = 64 if len(chip_word) > 4 else 48
    chip_x = x + w - chip_w - 8
    chip_y = y + 8
    word_x = chip_x + 10 + (chip_w - 14) / 2
    return (
        f'<g class="mo-node" role="img">'
        f"<title>{tip}</title>"
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="6" ry="6" '
        f'fill="{html.esc(_BOX_FILL)}" stroke="{html.esc(_BOX_STROKE)}" stroke-width="1"/>'
        f'<text x="{x + 10}" y="{y + 22}" class="mo-svg-label">{html.esc(text)}</text>'
        f'<rect x="{chip_x}" y="{chip_y}" width="{chip_w}" height="18" rx="9" ry="9" '
        f'fill="{html.esc(_SURFACE)}" stroke="{html.esc(chip_color)}" stroke-width="1.5"/>'
        f'<circle cx="{chip_x + 10}" cy="{chip_y + 9}" r="5" fill="{html.esc(chip_color)}"/>'
        f'<text x="{chip_x + 10}" y="{chip_y + 12}" text-anchor="middle" class="mo-svg-chip-icon">'
        f"{html.esc(chip_icon)}</text>"
        f'<text x="{word_x}" y="{chip_y + 13}" text-anchor="middle" class="mo-svg-chip-word">'
        f"{html.esc(chip_word)}</text>"
        f"</g>"
    )


def _svg_arrow(x1: int, y1: int, x2: int, y2: int, title: str) -> str:
    tip = html.esc(title)
    # Simple polyline with arrow head.
    return (
        f'<g class="mo-edge">'
        f"<title>{tip}</title>"
        f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" '
        f'stroke="{html.esc(_BOX_STROKE)}" stroke-width="1.5" marker-end="url(#mo-arrow)"/>'
        f"</g>"
    )


def _pipeline_svg(nodes: list, edges: list, paths: list, dest_counts: dict[str, int]) -> str:
    by_id = _node_by_id(nodes)
    edge_by_id = _edge_lookup(edges)

    # Discover harnesses from nodes/paths; prefer the fixed operator order.
    harnesses: list[str] = []
    for name in _HARNESS_ORDER:
        if f"harness:{name}" in by_id or f"inbox:{name}" in by_id:
            harnesses.append(name)
    for path in paths:
        if not isinstance(path, dict):
            continue
        name = str(path.get("harness") or "").lower()
        if name and name not in harnesses:
            harnesses.append(name)
    if not harnesses:
        for nid in by_id:
            if nid.startswith("harness:"):
                harnesses.append(nid.split(":", 1)[1])

    col_w = 150
    gap = 36
    box_h = 40
    harness_h = 36
    left = 16
    top = 16

    # Column x positions: harnesses | inbox | lint | ingest | canonical | care | evidence
    xs = [left + i * (col_w + gap) for i in range(7)]
    width = xs[-1] + col_w + 16

    harness_boxes: list[str] = []
    harness_centers: list[tuple[int, int]] = []
    for i, name in enumerate(harnesses):
        node = by_id.get(f"harness:{name}")
        y = top + i * (harness_h + 10)
        role, word, icon, color = _status_role((node or {}).get("state") if node else "unknown")
        harness_boxes.append(
            _svg_box(
                xs[0],
                y,
                col_w,
                harness_h,
                _human_harness(name),
                "",
                word,
                color,
                icon,
                f"harness:{name}",
            )
        )
        harness_centers.append((xs[0] + col_w, y + harness_h // 2))

    # Shared pipeline nodes
    pending_total = 0
    for name in harnesses:
        inbox = by_id.get(f"inbox:{name}")
        counts = (inbox or {}).get("counts") if isinstance((inbox or {}).get("counts"), dict) else {}
        if isinstance(counts.get("pending"), int):
            pending_total += counts["pending"]

    inbox_node = {"state": "present" if harnesses else "unknown", "counts": {"pending": pending_total}}
    lint_node = by_id.get("stage:lint")
    ingest_node = by_id.get("stage:ingest")
    care_node = by_id.get("stage:care_scan") or by_id.get("stage:refresh_queue")
    refresh_node = by_id.get("stage:refresh_queue")
    evidence_node = by_id.get("stage:evidence_projection")

    shared_y = top + max(0, (len(harnesses) - 1)) * (harness_h + 10) // 2
    if not harnesses:
        shared_y = top + 40

    def place(col: int, node: dict | None, label: str, node_id: str, count: str = "") -> tuple[str, int, int]:
        role, word, icon, color = _status_role(
            (node or {}).get("counts", {}).get("status")
            if isinstance((node or {}).get("counts"), dict)
            and (node or {}).get("counts", {}).get("status") not in (None, "")
            else ((node or {}).get("state") if node else "unknown")
        )
        if not count:
            count = _count_label(node, dest_counts, node_id)
        box = _svg_box(xs[col], shared_y, col_w, box_h, label, count, word, color, icon, node_id)
        return box, xs[col], shared_y + box_h // 2

    inbox_box, inbox_x, inbox_cy = place(
        1, inbox_node, "Handoffs", "inbox:*", str(pending_total) if pending_total else ""
    )
    lint_box, lint_x, lint_cy = place(2, lint_node, "Lint gate", "stage:lint")
    ingest_box, ingest_x, ingest_cy = place(3, ingest_node, "Ingestion", "stage:ingest")

    canonical_boxes: list[str] = []
    canonical_centers: list[tuple[int, int, str]] = []
    canon_start_y = top
    for i, key in enumerate(_CANONICAL_ORDER):
        node_id = f"canonical:{key}"
        node = by_id.get(node_id)
        if (
            node is None
            and key not in dest_counts
            and not any(isinstance(e, dict) and e.get("to") == node_id for e in edges)
        ):
            # Still show the five canonical stores when inventory/topology imply the fan-out.
            if not edges and not paths:
                continue
        y = canon_start_y + i * (box_h + 8)
        label = _CANONICAL_LABELS.get(key, key)
        count = _count_label(node, dest_counts, node_id)
        role, word, icon, color = _status_role((node or {}).get("state") if node else "canonical")
        canonical_boxes.append(_svg_box(xs[4], y, col_w, box_h, label, count, word, color, icon, node_id))
        canonical_centers.append((xs[4], y + box_h // 2, node_id))

    if not canonical_boxes:
        # Fallback single store box so the diagram still reads left-to-right.
        canonical_boxes.append(
            _svg_box(xs[4], shared_y, col_w, box_h, "Canonical stores", "", "OK", _STATUS_GOOD, "\u2713", "canonical:*")
        )
        canonical_centers.append((xs[4], shared_y + box_h // 2, "canonical:*"))

    care_label = "Care / refresh"
    care_count = ""
    care_source = care_node or refresh_node
    if isinstance(care_node, dict):
        c = care_node.get("counts") if isinstance(care_node.get("counts"), dict) else {}
        if isinstance(c.get("issue_count"), int):
            care_count = str(c["issue_count"])
    if not care_count and isinstance(refresh_node, dict):
        c = refresh_node.get("counts") if isinstance(refresh_node.get("counts"), dict) else {}
        if isinstance(c.get("queued"), int):
            care_count = str(c["queued"])
    care_box, care_x, care_cy = place(5, care_source, care_label, "stage:care_scan", care_count)
    evidence_box, evidence_x, evidence_cy = place(6, evidence_node, "Evidence", "stage:evidence_projection")

    arrows: list[str] = []
    # Harnesses -> shared handoffs
    for hx, hy in harness_centers:
        edge_title = "harness -> inbox"
        for name in harnesses:
            eid = f"edge:{name}:emit"
            if eid in edge_by_id:
                edge_title = eid
                break
        arrows.append(_svg_arrow(hx, hy, inbox_x, inbox_cy, edge_title))

    # Shared pipeline
    lint_edge = "edge:lint:ingest" if "edge:lint:ingest" in edge_by_id else "inbox -> lint -> ingest"
    arrows.append(_svg_arrow(inbox_x + col_w, inbox_cy, lint_x, lint_cy, "inbox -> lint"))
    arrows.append(_svg_arrow(lint_x + col_w, lint_cy, ingest_x, ingest_cy, lint_edge))

    # Fan-out to canonical
    for cx, cy, nid in canonical_centers:
        key = nid.split(":", 1)[-1]
        eid = f"edge:ingest:write:{key}"
        title = eid if eid in edge_by_id else f"ingest -> {nid}"
        arrows.append(_svg_arrow(ingest_x + col_w, ingest_cy, cx, cy, title))

    # Care and evidence (from cards / shared mid)
    if canonical_centers:
        mid = canonical_centers[0]
        care_edge = "edge:cards:care_scan" if "edge:cards:care_scan" in edge_by_id else "canonical -> care"
        arrows.append(_svg_arrow(mid[0] + col_w, mid[1], care_x, care_cy, care_edge))
    arrows.append(
        _svg_arrow(
            care_x + col_w,
            care_cy,
            evidence_x,
            evidence_cy,
            "edge:cards:evidence" if "edge:cards:evidence" in edge_by_id else "care -> evidence",
        )
    )

    height = max(
        top + max(len(harnesses), 1) * (harness_h + 10) + 20,
        top + max(len(canonical_centers), 1) * (box_h + 8) + 20,
        shared_y + box_h + 40,
    )

    parts = [
        f'<div class="mo-pipeline-wrap" data-mo-pipeline="1">'
        f'<svg class="mo-pipeline" viewBox="0 0 {width} {height}" width="100%" '
        f'role="img" aria-label="{html.esc("Memory pipeline from harnesses to evidence")}">'
        "<defs>"
        '<marker id="mo-arrow" viewBox="0 0 10 10" refX="9" refY="5" '
        'markerWidth="6" markerHeight="6" orient="auto-start-reverse">'
        f'<path d="M 0 0 L 10 5 L 0 10 z" fill="{html.esc(_BOX_STROKE)}"/>'
        "</marker>"
        "</defs>",
        *harness_boxes,
        inbox_box,
        lint_box,
        ingest_box,
        *canonical_boxes,
        care_box,
        evidence_box,
        *arrows,
        "</svg></div>",
    ]
    return "".join(parts)


def _flags_block(flags: list) -> str:
    if not flags:
        return f"<p>{html.esc('No attention flags.')}</p>"
    items: list[str] = []
    for flag in flags:
        if not isinstance(flag, dict):
            continue
        kind = str(flag.get("kind") or "flag")
        plain = _FLAG_PLAIN.get(kind, kind.replace("_", " "))
        detail = flag.get("detail") or flag.get("destination") or ""
        extras: list[str] = []
        for key in ("destination", "node_id", "edge_id", "writer_component", "kind"):
            if flag.get(key) not in (None, ""):
                extras.append(f"{key}={flag.get(key)}")
        writers = flag.get("writers")
        if isinstance(writers, list) and writers:
            extras.append("writers=" + ", ".join(str(w) for w in writers))
        nodes = flag.get("nodes")
        if isinstance(nodes, list) and nodes:
            extras.append("nodes=" + " -> ".join(str(n) for n in nodes))
        detail_html = f" {html.esc(str(detail))}" if detail else ""
        debug = ""
        if extras:
            debug = (
                f' <details class="mo-debug"><summary>{html.esc("Details")}</summary>'
                f"<code>{html.esc('; '.join(extras))}</code></details>"
            )
        items.append(f"<li><strong>{html.esc(plain)}</strong>.{detail_html}{debug}</li>")
    if not items:
        return f"<p>{html.esc('No attention flags.')}</p>"
    return f'<ul class="mo-flags">{"".join(items)}</ul>'


def _debug_details(nodes: list, edges: list) -> str:
    """Collapsed raw contract tables for debugging; not the primary operator UI."""
    node_rows: list[str] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        latest = node.get("latest_run") if isinstance(node.get("latest_run"), dict) else {}
        counts = node.get("counts") if isinstance(node.get("counts"), dict) else {}
        schedule = node.get("schedule")
        if isinstance(schedule, dict):
            schedule_text = f"{schedule.get('source') or '-'} / {schedule.get('cadence') or '-'}"
        else:
            schedule_text = "-" if schedule in (None, "") else str(schedule)
        node_rows.append(
            "<tr>"
            f"<td>{html.esc(_display(node.get('id')))}</td>"
            f"<td>{html.esc(_display(node.get('label')))}</td>"
            f"<td>{html.esc(_display(node.get('kind')))}</td>"
            f"<td>{html.esc(_display(node.get('state')))}</td>"
            f"<td>{html.esc(_display(node.get('owner')))}</td>"
            f"<td>{html.esc(_display(node.get('path')))}</td>"
            f"<td>{html.esc(schedule_text)}</td>"
            f"<td>{html.esc(', '.join(f'{k}={v}' for k, v in counts.items()) if counts else '-')}</td>"
            f"<td>{html.esc(_display(latest.get('status')))}</td>"
            f"<td>{html.esc(_display(latest.get('run_id')))}</td>"
            f"<td>{html.esc(_display(latest.get('completed_at')))}</td>"
            f"<td>{html.esc(_display(latest.get('duration_seconds')))}</td>"
            f"<td>{html.esc(_display(latest.get('evidence_state')))}</td>"
            f"<td>{html.esc(_display(node.get('next_action')))}</td>"
            "</tr>"
        )

    edge_rows: list[str] = []
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        receipt = edge.get("latest_receipt") if isinstance(edge.get("latest_receipt"), dict) else {}
        enabled = "yes" if edge.get("enabled") else "no"
        edge_rows.append(
            "<tr>"
            f"<td>{html.esc(_display(edge.get('id')))}</td>"
            f"<td>{html.esc(_display(edge.get('from')))}</td>"
            f"<td>{html.esc(_display(edge.get('to')))}</td>"
            f"<td>{html.esc(_display(edge.get('flow')))}</td>"
            f"<td>{html.esc(_display(edge.get('authority')))}</td>"
            f"<td>{html.esc(_display(edge.get('writer_component')))}</td>"
            f"<td>{html.esc(enabled)}</td>"
            f"<td>{html.esc(_display(receipt.get('status')))}</td>"
            f"<td>{html.esc(_display(receipt.get('run_id')))}</td>"
            f"<td>{html.esc(_display(receipt.get('duration_seconds')))}</td>"
            f"<td>{html.esc(_display(receipt.get('evidence_state')))}</td>"
            f"<td>{html.esc(_display(receipt.get('completed_at')))}</td>"
            "</tr>"
        )

    if not node_rows and not edge_rows:
        return ""

    nodes_table = (
        '<div class="mo-scroll"><table class="data-table"><thead><tr>'
        + "".join(
            f"<th>{html.esc(h)}</th>"
            for h in (
                "Id",
                "Label",
                "Kind",
                "State",
                "Owner",
                "Path",
                "Schedule",
                "Counts",
                "Run status",
                "Run id",
                "Completed",
                "Duration",
                "Evidence",
                "Next action",
            )
        )
        + "</tr></thead><tbody>"
        + "".join(node_rows)
        + "</tbody></table></div>"
        if node_rows
        else f"<p>{html.esc('No nodes.')}</p>"
    )
    edges_table = (
        '<div class="mo-scroll"><table class="data-table"><thead><tr>'
        + "".join(
            f"<th>{html.esc(h)}</th>"
            for h in (
                "Id",
                "From",
                "To",
                "Flow",
                "Authority",
                "Writer",
                "Enabled",
                "Receipt status",
                "Receipt",
                "Duration",
                "Evidence",
                "Completed",
            )
        )
        + "</tr></thead><tbody>"
        + "".join(edge_rows)
        + "</tbody></table></div>"
        if edge_rows
        else f"<p>{html.esc('No edges.')}</p>"
    )
    return (
        '<details class="mo-debug-panel">'
        f"<summary>{html.esc('Contract details (nodes and edges)')}</summary>"
        f"<h3>{html.esc('Nodes')}</h3>{nodes_table}"
        f"<h3>{html.esc('Edges')}</h3>{edges_table}"
        "</details>"
    )


def _display(value: Any) -> str:
    """Render a scalar for ops tables; explicit nulls become '-', never 'None'."""
    if value is None or value == "":
        return "-"
    return str(value)


def _render_inventory(inventory: dict) -> str:
    if inventory.get("error"):
        return html.error_panel("Inventory", str(inventory["error"]))

    items = inventory.get("items") if isinstance(inventory.get("items"), list) else []
    warning = inventory.get("warning")
    partial = bool(inventory.get("partial"))
    loaded = len(items)
    contract_total = inventory.get("contract_total")
    if not isinstance(contract_total, int):
        raw_total = inventory.get("total")
        contract_total = raw_total if isinstance(raw_total, int) and not partial else None

    if not items:
        inner = f"<p>{html.esc('Nothing here.')}</p>"
        if warning:
            inner = f'<p class="error">{html.esc(warning)}</p>' + inner
        return html.panel(html.esc("Inventory"), inner)

    columns = _inventory_columns(items)
    filter_bar = _inventory_filters(items)
    rows: list[list[str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        rows.append(_inventory_row(item, columns))

    headers = [html.esc(col["label"]) for col in columns]
    table = html.table(headers, rows)
    parts = table.split("<tbody>", 1)
    if len(parts) == 2:
        head, rest = parts
        body, tail = rest.split("</tbody>", 1)
        body = body.replace("<tr>", '<tr data-mo-row="1">')
        table = head + "<tbody>" + body + "</tbody>" + tail

    if partial and isinstance(contract_total, int):
        total_attr = str(contract_total)
    elif not partial:
        pager_total = inventory.get("total") if isinstance(inventory.get("total"), int) else loaded
        total_attr = str(pager_total)
    else:
        total_attr = str(loaded)

    pager = (
        f'<div class="mo-pager" data-mo-page-size="{_CLIENT_PAGE_SIZE}" '
        f'data-mo-total="{html.esc(total_attr)}">'
        f'<button type="button" data-mo-page="prev" disabled>{html.esc("Prior")}</button>'
        f"<span data-mo-page-status>{html.esc('Page 1')}</span>"
        f'<button type="button" data-mo-page="next">{html.esc("Next")}</button>'
        "</div>"
    )
    warn_bits: list[str] = []
    if warning:
        warn_bits.append(str(warning))
    elif partial:
        if isinstance(contract_total, int):
            warn_bits.append(f"Inventory is partial: loaded {loaded} of {contract_total}.")
        else:
            warn_bits.append(f"Inventory is partial: loaded {loaded} item(s).")
    warn_html = "".join(f'<p class="error">{html.esc(bit)}</p>' for bit in warn_bits)
    meta = ""
    if partial and isinstance(contract_total, int):
        meta = f'<p class="mo-muted">{html.esc(f"Loaded {loaded} of {contract_total} (partial).")}</p>'
    hidden_notes = _hidden_column_notes(items)
    hidden_note = ""
    if hidden_notes:
        hidden_note = f'<p class="mo-muted">{html.esc("; ".join(hidden_notes))}</p>'
    return html.panel(
        html.esc("Inventory"),
        warn_html + meta + hidden_note + filter_bar + pager + f'<div class="mo-scroll">{table}</div>',
    )


def _hidden_column_notes(items: list) -> list[str]:
    notes: list[str] = []
    if (
        items
        and all(not isinstance(item, dict) or item.get("created_at") in (None, "") for item in items)
        and all(not isinstance(item, dict) or item.get("updated_at") in (None, "") for item in items)
    ):
        notes.append("Created/Updated not tracked for these items")
    return notes


def _inventory_columns(items: list) -> list[dict[str, Any]]:
    """Choose visible columns; drop all-empty created/updated; fold Id into Title."""

    def any_value(key: str) -> bool:
        for item in items:
            if not isinstance(item, dict):
                continue
            val = item.get(key)
            if val not in (None, "", [], {}):
                return True
        return False

    columns: list[dict[str, Any]] = [
        {"key": "title", "label": "Title"},
        {"key": "canonical_path", "label": "Path"},
        {"key": "logical_destination", "label": "Destination"},
        {"key": "store_type", "label": "Type"},
        {"key": "category", "label": "Category"},
        {"key": "tags", "label": "Tags"},
    ]
    if any_value("created_at"):
        columns.append({"key": "created_at", "label": "Created"})
    if any_value("updated_at"):
        columns.append({"key": "updated_at", "label": "Updated"})
    columns.extend(
        [
            {"key": "last_reviewed", "label": "Reviewed"},
            {"key": "fresh_until", "label": "Fresh until"},
            {"key": "freshness", "label": "Freshness"},
            {"key": "review_state", "label": "Review"},
            {"key": "evidence_state", "label": "Evidence"},
            {"key": "source_harness", "label": "Source harness"},
            {"key": "source_handoff", "label": "Source handoff"},
            {"key": "owning_workflow", "label": "Owning workflow"},
            {"key": "mutation", "label": "Mutation"},
            {"key": "care", "label": "Care"},
        ]
    )
    return columns


def _inventory_filters(items: list) -> str:
    def options(values: set[str]) -> str:
        parts = [f'<option value="">{html.esc("All")}</option>']
        for value in sorted(values):
            parts.append(f'<option value="{html.esc(value)}">{html.esc(value)}</option>')
        return "".join(parts)

    store_types: set[str] = set()
    categories: set[str] = set()
    tags: set[str] = set()
    freshness: set[str] = set()
    review_states: set[str] = set()
    evidence_states: set[str] = set()
    harnesses: set[str] = set()
    workflows: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("store_type"):
            store_types.add(str(item["store_type"]))
        if item.get("category"):
            categories.add(str(item["category"]))
        raw_tags = item.get("tags")
        if isinstance(raw_tags, list):
            for tag in raw_tags:
                if tag not in (None, ""):
                    tags.add(str(tag))
        if item.get("freshness"):
            freshness.add(str(item["freshness"]))
        if item.get("review_state"):
            review_states.add(str(item["review_state"]))
        if item.get("evidence_state"):
            evidence_states.add(str(item["evidence_state"]))
        if item.get("source_harness"):
            harnesses.add(str(item["source_harness"]))
        if item.get("owning_workflow"):
            workflows.add(str(item["owning_workflow"]))

    controls = [
        ("store_type", "Store type", options(store_types)),
        ("category", "Category", options(categories)),
        ("tag", "Tag", options(tags)),
        ("freshness", "Freshness", options(freshness)),
        ("review_state", "Review state", options(review_states)),
        ("evidence_state", "Evidence state", options(evidence_states)),
        ("source_harness", "Source harness", options(harnesses)),
        ("owning_workflow", "Owning workflow", options(workflows)),
    ]
    fields: list[str] = []
    for key, label, opts in controls:
        fields.append(f'<label>{html.esc(label)} <select data-mo-filter="{html.esc(key)}">{opts}</select></label>')
    fields.append(
        "<label>" + html.esc("Search") + ' <input type="search" data-mo-filter-text '
        f'placeholder="{html.esc("Filter inventory")}" '
        f'aria-label="{html.esc("Filter inventory")}">'
        "</label>"
    )
    return f'<div class="mo-filters">{"".join(fields)}</div>'


def _mutation_text(item: dict) -> str:
    mutation = item.get("last_mutation") if isinstance(item.get("last_mutation"), dict) else None
    if not mutation:
        return "-"
    receipt = mutation.get("receipt") if isinstance(mutation.get("receipt"), dict) else {}
    workflow = _display(mutation.get("workflow"))
    return (
        f"{workflow} / "
        f"evidence={_display(receipt.get('evidence_state'))} / "
        f"status={_display(receipt.get('status'))} / "
        f"run_id={_display(receipt.get('run_id'))} / "
        f"completed_at={_display(receipt.get('completed_at'))} / "
        f"duration_seconds={_display(receipt.get('duration_seconds'))}"
    )


def _tag_chips(tags: list[str]) -> str:
    if not tags:
        return html.esc("-")
    visible = tags[:_TAG_CHIP_LIMIT]
    extra = len(tags) - len(visible)
    chips = "".join(f'<span class="mo-tag">{html.esc(tag)}</span>' for tag in visible)
    if extra > 0:
        chips += f'<span class="mo-tag mo-tag-more">{html.esc(f"+{extra} more")}</span>'
    return f'<span class="mo-tags">{chips}</span>'


def _inventory_row(item: dict, columns: list[dict[str, Any]]) -> list[str]:
    tags = item.get("tags")
    if isinstance(tags, list):
        tag_list = [str(t) for t in tags if t not in (None, "")]
    else:
        tag_list = [] if tags in (None, "") else [str(tags)]
    mutation_text = _mutation_text(item)
    care = item.get("care") if isinstance(item.get("care"), dict) else {}
    issues = care.get("issues") if isinstance(care.get("issues"), list) else []
    issue_text = ", ".join(str(i) for i in issues) if issues else "-"
    queued = care.get("queued_action")
    care_text = f"{issue_text}; queued={queued or '-'}"

    tags_json = json.dumps(tag_list, separators=(",", ":"))
    attrs = {
        "store_type": item.get("store_type") or "",
        "category": item.get("category") or "",
        "freshness": item.get("freshness") or "",
        "review_state": item.get("review_state") or "",
        "evidence_state": item.get("evidence_state") or "",
        "source_harness": item.get("source_harness") or "",
        "owning_workflow": item.get("owning_workflow") or "",
    }
    item_id = _display(item.get("id"))
    title_cell = (
        f'<span data-mo-store_type="{html.esc(attrs["store_type"])}" '
        f'data-mo-category="{html.esc(attrs["category"])}" '
        f'data-mo-tags="{html.esc(tags_json)}" '
        f'data-mo-freshness="{html.esc(attrs["freshness"])}" '
        f'data-mo-review_state="{html.esc(attrs["review_state"])}" '
        f'data-mo-evidence_state="{html.esc(attrs["evidence_state"])}" '
        f'data-mo-source_harness="{html.esc(attrs["source_harness"])}" '
        f'data-mo-owning_workflow="{html.esc(attrs["owning_workflow"])}">'
        f"{html.esc(_display(item.get('title')))}"
        f'<details class="mo-debug"><summary>{html.esc("Id")}</summary>'
        f"<code>{html.esc(item_id)}</code></details>"
        "</span>"
    )

    values: dict[str, str] = {
        "title": title_cell,
        "canonical_path": html.esc(_display(item.get("canonical_path"))),
        "logical_destination": html.esc(_display(item.get("logical_destination"))),
        "store_type": html.esc(_display(item.get("store_type"))),
        "category": html.esc(_display(item.get("category"))),
        "tags": _tag_chips(tag_list),
        "created_at": html.esc(_display(item.get("created_at"))),
        "updated_at": html.esc(_display(item.get("updated_at"))),
        "last_reviewed": html.esc(_display(item.get("last_reviewed"))),
        "fresh_until": html.esc(_display(item.get("fresh_until"))),
        "freshness": html.esc(_display(item.get("freshness"))),
        "review_state": html.esc(_display(item.get("review_state"))),
        "evidence_state": html.esc(_display(item.get("evidence_state"))),
        "source_harness": html.esc(_display(item.get("source_harness"))),
        "source_handoff": html.esc(_display(item.get("source_handoff"))),
        "owning_workflow": html.esc(_display(item.get("owning_workflow"))),
        "mutation": (
            f'<details class="mo-debug"><summary>{html.esc(_display(item.get("owning_workflow") or "mutation"))}</summary>'
            f"<code>{html.esc(mutation_text)}</code></details>"
            if mutation_text != "-"
            else html.esc("-")
        ),
        "care": html.esc(care_text),
    }
    return [values[col["key"]] for col in columns]


def _stylesheet(nonce: str) -> str:
    return f"""<style nonce="{html.esc(nonce)}">
.mo-modes {{
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
  margin: 0 0 1rem;
}}
.mo-mode {{
  font: inherit;
  padding: 0.4rem 0.75rem;
  border: 1px solid #666;
  border-radius: 0.25rem;
  background: #f8f8f8;
  color: {_TEXT_PRIMARY};
  cursor: pointer;
  text-decoration: none;
}}
.mo-mode:hover {{
  background: #eee;
}}
.mo-mode:focus {{
  outline: 2px solid #0066cc;
  outline-offset: 2px;
}}
.mo-mode[aria-selected="true"] {{
  background: #0066cc;
  border-color: #0066cc;
  color: #fff;
  font-weight: 600;
}}
.mo-mode:disabled,
.mo-pager button:disabled {{
  opacity: 0.5;
  cursor: not-allowed;
}}
.mo-summary {{
  margin: 0 0 1rem;
  padding: 0.85rem 1rem;
  border: 1px solid {_BORDER};
  border-radius: 0.35rem;
  background: {_SURFACE};
  color: {_TEXT_PRIMARY};
  max-width: 72rem;
}}
.mo-summary p {{
  margin: 0;
  color: {_TEXT_PRIMARY};
}}
.mo-health-tiles {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(14rem, 1fr));
  gap: 0.75rem;
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
  color: {_TEXT_PRIMARY};
}}
.mo-health-label {{
  font-weight: 600;
  color: {_TEXT_PRIMARY};
  flex: 1;
}}
.mo-health-meaning {{
  margin: 0;
  color: {_TEXT_SECONDARY};
  font-size: 0.92rem;
}}
.mo-chip,
.mo-chip-word {{
  color: {_TEXT_PRIMARY};
  font-size: 0.75rem;
  font-weight: 700;
  letter-spacing: 0.02em;
}}
.mo-chip {{
  display: inline-flex;
  align-items: center;
  gap: 0.3rem;
}}
.mo-chip-icon {{
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 1.1rem;
  height: 1.1rem;
  border-radius: 999px;
  color: #fff;
  font-size: 0.7rem;
  line-height: 1;
}}
.mo-pipeline-wrap {{
  overflow-x: auto;
  max-width: 100%;
}}
.mo-pipeline {{
  display: block;
  min-width: 980px;
  max-width: 1400px;
  height: auto;
}}
.mo-svg-label {{
  fill: {_TEXT_PRIMARY};
  font-size: 12px;
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
}}
.mo-svg-chip-word {{
  fill: {_TEXT_PRIMARY};
  font-size: 9px;
  font-weight: 700;
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
}}
.mo-svg-chip-icon {{
  fill: #fff;
  font-size: 8px;
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
}}
.mo-filters {{
  display: flex;
  flex-wrap: wrap;
  gap: 0.75rem 1rem;
  margin: 0 0 0.75rem;
  font-size: 0.9rem;
}}
.mo-filters label {{
  display: flex;
  flex-direction: column;
  gap: 0.2rem;
}}
.mo-filters select,
.mo-filters input[type="search"] {{
  font: inherit;
  min-width: 9rem;
  padding: 0.25rem 0.4rem;
  border: 1px solid #666;
  border-radius: 0.2rem;
  background: #fff;
  color: {_TEXT_PRIMARY};
}}
.mo-filters select:focus,
.mo-filters input:focus,
.mo-pager button:focus {{
  outline: 2px solid #0066cc;
  outline-offset: 2px;
}}
.mo-pager {{
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.75rem;
  margin: 0 0 0.75rem;
}}
.mo-pager button {{
  font: inherit;
  padding: 0.3rem 0.7rem;
  border: 1px solid #666;
  border-radius: 0.2rem;
  background: #f8f8f8;
  color: {_TEXT_PRIMARY};
  cursor: pointer;
}}
.mo-scroll {{
  overflow-x: auto;
  -webkit-overflow-scrolling: touch;
  max-width: 1440px;
}}
.mo-flags {{
  margin: 0;
  padding-left: 1.2rem;
  color: {_TEXT_PRIMARY};
}}
.mo-muted {{
  color: {_TEXT_SECONDARY};
  font-size: 0.9rem;
}}
.mo-tags {{
  display: inline-flex;
  flex-wrap: nowrap;
  gap: 0.25rem;
  max-width: 18rem;
  overflow: hidden;
  vertical-align: middle;
}}
.mo-tag {{
  display: inline-block;
  padding: 0.1rem 0.4rem;
  border: 1px solid {_BORDER};
  border-radius: 999px;
  background: {_SURFACE};
  color: {_TEXT_PRIMARY};
  font-size: 0.78rem;
  white-space: nowrap;
}}
.mo-tag-more {{
  color: {_TEXT_SECONDARY};
}}
.mo-debug,
.mo-debug-panel {{
  margin-top: 0.35rem;
  color: {_TEXT_SECONDARY};
  font-size: 0.85rem;
}}
.mo-debug summary,
.mo-debug-panel summary {{
  cursor: pointer;
  color: {_TEXT_SECONDARY};
}}
.mo-debug code,
.mo-debug-panel code {{
  color: {_TEXT_PRIMARY};
  word-break: break-word;
}}
.mo-panel[hidden] {{
  display: none;
}}
@media (max-width: 720px) {{
  .mo-filters label {{
    min-width: 100%;
  }}
  .mo-filters select,
  .mo-filters input[type="search"] {{
    min-width: 100%;
  }}
}}
</style>
<noscript><style nonce="{html.esc(nonce)}">
.mo-panel[hidden] {{ display: block !important; }}
</style></noscript>"""


def _script(nonce: str) -> str:
    return f"""<script nonce="{html.esc(nonce)}">
(function () {{
  function selectMode(mode) {{
    var tabs = document.querySelectorAll("[data-mo-mode]");
    var panels = document.querySelectorAll("[data-mo-panel]");
    for (var i = 0; i < tabs.length; i++) {{
      var tab = tabs[i];
      var active = tab.getAttribute("data-mo-mode") === mode;
      tab.setAttribute("aria-selected", active ? "true" : "false");
      tab.tabIndex = active ? 0 : -1;
    }}
    for (var j = 0; j < panels.length; j++) {{
      var panel = panels[j];
      panel.hidden = panel.getAttribute("data-mo-panel") !== mode;
    }}
  }}

  document.addEventListener("click", function (e) {{
    var t = e.target;
    if (!t || !t.getAttribute) return;
    var mode = t.getAttribute("data-mo-mode");
    if (mode) {{
      e.preventDefault();
      selectMode(mode);
      return;
    }}
    var pageDir = t.getAttribute("data-mo-page");
    if (pageDir) {{
      e.preventDefault();
      shiftPage(pageDir);
    }}
  }});

  document.addEventListener("keydown", function (e) {{
    var t = e.target;
    if (!t || !t.getAttribute || !t.getAttribute("data-mo-mode")) return;
    var tabs = Array.prototype.slice.call(document.querySelectorAll("[data-mo-mode]"));
    var idx = tabs.indexOf(t);
    if (idx < 0) return;
    var next = -1;
    if (e.key === "ArrowRight" || e.key === "ArrowDown") next = (idx + 1) % tabs.length;
    if (e.key === "ArrowLeft" || e.key === "ArrowUp") next = (idx - 1 + tabs.length) % tabs.length;
    if (e.key === "Home") next = 0;
    if (e.key === "End") next = tabs.length - 1;
    if (next < 0) return;
    e.preventDefault();
    tabs[next].focus();
    selectMode(tabs[next].getAttribute("data-mo-mode"));
  }});

  document.addEventListener("change", function (e) {{
    var t = e.target;
    if (!t || !t.getAttribute) return;
    if (t.getAttribute("data-mo-filter") || t.hasAttribute("data-mo-filter-text")) {{
      applyInventory();
    }}
  }});
  document.addEventListener("input", function (e) {{
    var t = e.target;
    if (!t || !t.hasAttribute || !t.hasAttribute("data-mo-filter-text")) return;
    applyInventory();
  }});

  var pageIndex = 0;

  function rowMatches(row) {{
    var filters = document.querySelectorAll("[data-mo-filter]");
    var span = row.querySelector("span[data-mo-store_type], td span");
    for (var i = 0; i < filters.length; i++) {{
      var sel = filters[i];
      var key = sel.getAttribute("data-mo-filter");
      var wanted = (sel.value || "").toLowerCase();
      if (!wanted) continue;
      if (key === "tag") {{
        var rawTags = span && span.getAttribute ? (span.getAttribute("data-mo-tags") || "[]") : "[]";
        var parsed = [];
        try {{ parsed = JSON.parse(rawTags); }} catch (err) {{ parsed = []; }}
        var found = false;
        for (var t = 0; t < parsed.length; t++) {{
          if (String(parsed[t]).toLowerCase() === wanted) {{ found = true; break; }}
        }}
        if (!found) return false;
        continue;
      }}
      var actual = "";
      if (span && span.getAttribute) {{
        actual = (span.getAttribute("data-mo-" + key) || "").toLowerCase();
      }}
      if (actual !== wanted) {{
        return false;
      }}
    }}
    var textInput = document.querySelector("[data-mo-filter-text]");
    if (textInput && textInput.value) {{
      var q = textInput.value.toLowerCase();
      var text = (row.textContent || "").toLowerCase();
      if (text.indexOf(q) === -1) return false;
    }}
    return true;
  }}

  function applyInventory() {{
    pageIndex = 0;
    renderPage();
  }}

  function shiftPage(dir) {{
    if (dir === "next") pageIndex += 1;
    if (dir === "prev") pageIndex -= 1;
    renderPage();
  }}

  function renderPage() {{
    var pager = document.querySelector(".mo-pager");
    if (!pager) return;
    var size = parseInt(pager.getAttribute("data-mo-page-size") || "50", 10);
    var rows = document.querySelectorAll("tr[data-mo-row]");
    var matched = [];
    for (var i = 0; i < rows.length; i++) {{
      if (rowMatches(rows[i])) matched.push(rows[i]);
    }}
    var pages = Math.max(1, Math.ceil(matched.length / size) || 1);
    if (pageIndex >= pages) pageIndex = pages - 1;
    if (pageIndex < 0) pageIndex = 0;
    var start = pageIndex * size;
    var end = start + size;
    for (var r = 0; r < rows.length; r++) {{
      rows[r].hidden = true;
    }}
    for (var m = 0; m < matched.length; m++) {{
      matched[m].hidden = !(m >= start && m < end);
    }}
    var status = pager.querySelector("[data-mo-page-status]");
    if (status) {{
      status.textContent = "Page " + (pageIndex + 1) + " of " + pages +
        " (" + matched.length + " items)";
    }}
    var prev = pager.querySelector('[data-mo-page="prev"]');
    var next = pager.querySelector('[data-mo-page="next"]');
    if (prev) prev.disabled = pageIndex <= 0;
    if (next) next.disabled = pageIndex >= pages - 1;
  }}

  selectMode("topology");
  renderPage();
}})();
</script>"""
