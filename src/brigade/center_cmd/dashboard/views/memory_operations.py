"""Memory Operations dashboard view (topology + inventory).

Contract-only: fetch via ``data.run_json`` for
``brigade memory topology --json`` and paginated
``brigade memory inventory --json``. No filesystem, cron, or body reads.
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
_HEALTH_KEYS = (
    ("care_scan", "care scan"),
    ("refresh_queue", "refresh queue"),
    ("handoff_backlog", "handoff backlog"),
    ("quarantine", "quarantine"),
    ("closeout", "closeout"),
    ("evidence_projection", "evidence projection"),
)


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
        _render_topology(topology),
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
        # Partial without a safe contract total: expose loaded count only as loaded.
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


def _render_topology(topology: dict) -> str:
    if topology.get("error"):
        return html.error_panel("Topology", str(topology["error"]))

    health = topology.get("health") if isinstance(topology.get("health"), dict) else {}
    flags = topology.get("flags") if isinstance(topology.get("flags"), list) else []
    paths = topology.get("paths") if isinstance(topology.get("paths"), list) else []
    nodes = topology.get("nodes") if isinstance(topology.get("nodes"), list) else []
    edges = topology.get("edges") if isinstance(topology.get("edges"), list) else []

    if not health and not flags and not paths and not nodes and not edges:
        return html.panel(html.esc("Topology"), f"<p>{html.esc('Nothing here.')}</p>")

    sections = [
        html.panel(html.esc("Health"), _health_table(health)),
        html.panel(html.esc("Flags"), _flags_block(flags)),
        html.panel(html.esc("Harness paths"), _paths_block(paths, edges)),
        html.panel(html.esc("Flow"), _flow_graph(paths, nodes, edges)),
        html.panel(html.esc("Nodes"), _nodes_table(nodes)),
        html.panel(html.esc("Edges"), _edges_table(edges)),
    ]
    return "".join(sections)


def _health_table(health: dict) -> str:
    rows: list[list[str]] = []
    for key, label in _HEALTH_KEYS:
        block = health.get(key) if isinstance(health.get(key), dict) else {}
        detail_parts: list[str] = []
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
                detail_parts.append(f"{detail_key}={block.get(detail_key)}")
        rows.append(
            [
                html.esc(label),
                html.esc(block.get("status", "-")),
                html.esc(", ".join(detail_parts) if detail_parts else "-"),
            ]
        )
    if not rows:
        return f"<p>{html.esc('Nothing here.')}</p>"
    return html.table(
        [html.esc("Signal"), html.esc("Status"), html.esc("Detail")],
        rows,
    )


def _flags_block(flags: list) -> str:
    if not flags:
        return f"<p>{html.esc('No flags.')}</p>"
    items: list[str] = []
    for flag in flags:
        if not isinstance(flag, dict):
            continue
        kind = html.esc(flag.get("kind", "flag"))
        detail = html.esc(flag.get("detail") or flag.get("destination") or "-")
        extras: list[str] = []
        for key in ("destination", "node_id", "edge_id", "writer_component"):
            if flag.get(key) not in (None, ""):
                extras.append(f"{key}={flag.get(key)}")
        writers = flag.get("writers")
        if isinstance(writers, list) and writers:
            extras.append("writers=" + ", ".join(str(w) for w in writers))
        nodes = flag.get("nodes")
        if isinstance(nodes, list) and nodes:
            extras.append("nodes=" + " -> ".join(str(n) for n in nodes))
        extra_html = html.esc("; ".join(extras)) if extras else ""
        suffix = f" <span class='mo-muted'>({extra_html})</span>" if extra_html else ""
        items.append(f"<li><strong>{kind}</strong>: {detail}{suffix}</li>")
    if not items:
        return f"<p>{html.esc('No flags.')}</p>"
    return f'<ul class="mo-flags">{"".join(items)}</ul>'


def _edge_lookup(edges: list) -> dict[str, dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for edge in edges:
        if isinstance(edge, dict) and edge.get("id") is not None:
            by_id[str(edge["id"])] = edge
    return by_id


def _path_edge_segments(path: dict, edge_by_id: dict[str, dict[str, Any]]) -> list[str]:
    """Return display segments for a path from edge_ids only (never node_ids chains)."""
    edge_ids = path.get("edge_ids") if isinstance(path.get("edge_ids"), list) else []
    segments: list[str] = []
    for edge_id in edge_ids:
        key = str(edge_id)
        edge = edge_by_id.get(key)
        if edge is None:
            segments.append(f"[{key}]")
            continue
        frm = edge.get("from") if edge.get("from") not in (None, "") else "-"
        to = edge.get("to") if edge.get("to") not in (None, "") else "-"
        segments.append(f"{frm} -> {to} [{key}]")
    return segments


def _paths_block(paths: list, edges: list) -> str:
    if not paths:
        return f"<p>{html.esc('No harness paths.')}</p>"
    edge_by_id = _edge_lookup(edges)
    blocks: list[str] = []
    for path in paths:
        if not isinstance(path, dict):
            continue
        harness = html.esc(path.get("harness") or path.get("id") or "path")
        segments = _path_edge_segments(path, edge_by_id)
        if segments:
            flow = "; ".join(segments)
        else:
            flow = "-"
        blocks.append(f'<li><strong>{harness}</strong>: <span class="mo-path-flow">{html.esc(flow)}</span></li>')
    if not blocks:
        return f"<p>{html.esc('No harness paths.')}</p>"
    return f'<ul class="mo-paths">{"".join(blocks)}</ul>'


def _ordered_path_edges(paths: list, edge_by_id: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Unique edges in path order; edge_ids are the only connection authority."""
    ordered: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in paths:
        if not isinstance(path, dict):
            continue
        edge_ids = path.get("edge_ids") if isinstance(path.get("edge_ids"), list) else []
        for edge_id in edge_ids:
            key = str(edge_id)
            if key in seen:
                continue
            edge = edge_by_id.get(key)
            if edge is None:
                continue
            seen.add(key)
            ordered.append(edge)
    return ordered


def _flow_graph(paths: list, nodes: list, edges: list) -> str:
    """Accessible compact flow list: every arrow is an actual edge.from -> edge.to."""
    if not paths:
        return f"<p>{html.esc('Nodes')}: {html.esc(len(nodes))}; {html.esc('Edges')}: {html.esc(len(edges))}</p>"

    edge_by_id = _edge_lookup(edges)
    used = _ordered_path_edges(paths, edge_by_id)
    if not used:
        # Fall back to all topology edges when paths omit edge_ids.
        used = [e for e in edges if isinstance(e, dict)]
    if not used:
        return f"<p>{html.esc('Nothing here.')}</p>"

    items: list[str] = []
    for edge in used:
        edge_id = str(edge.get("id") or "-")
        frm = edge.get("from") if edge.get("from") not in (None, "") else "-"
        to = edge.get("to") if edge.get("to") not in (None, "") else "-"
        items.append(
            "<li>"
            f'<span class="mo-flow-edge">{html.esc(f"{frm} -> {to}")}</span> '
            f'<span class="mo-muted">({html.esc(edge_id)})</span>'
            "</li>"
        )
    return (
        f'<ul class="mo-flow-list" aria-label="{html.esc("Harness to canonical memory flow")}">'
        + "".join(items)
        + "</ul>"
    )


def _display(value: Any) -> str:
    """Render a scalar for ops tables; explicit nulls become '-', never 'None'."""
    if value is None or value == "":
        return "-"
    return str(value)


def _latest_run_fields(node: dict) -> tuple[str, str, str, str, str]:
    latest = node.get("latest_run")
    if not isinstance(latest, dict):
        return ("-", "-", "-", "-", "-")
    return (
        _display(latest.get("status")),
        _display(latest.get("run_id")),
        _display(latest.get("completed_at")),
        _display(latest.get("duration_seconds")),
        _display(latest.get("evidence_state")),
    )


def _nodes_table(nodes: list) -> str:
    rows: list[list[str]] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        schedule = node.get("schedule")
        if isinstance(schedule, dict):
            schedule_text = f"{schedule.get('source') or '-'} / {schedule.get('cadence') or '-'}"
        else:
            schedule_text = "-" if schedule in (None, "") else str(schedule)
        counts = node.get("counts") if isinstance(node.get("counts"), dict) else {}
        count_text = ", ".join(f"{k}={v}" for k, v in counts.items()) if counts else "-"
        run_status, run_id, completed_at, duration, evidence = _latest_run_fields(node)
        rows.append(
            [
                html.esc(_display(node.get("id"))),
                html.esc(_display(node.get("label"))),
                html.esc(_display(node.get("kind"))),
                html.esc(_display(node.get("state"))),
                html.esc(_display(node.get("owner"))),
                html.esc(_display(node.get("authority"))),
                html.esc(_display(node.get("path"))),
                html.esc(schedule_text),
                html.esc(count_text),
                html.esc(run_status),
                html.esc(run_id),
                html.esc(completed_at),
                html.esc(duration),
                html.esc(evidence),
                html.esc(_display(node.get("next_action"))),
            ]
        )
    if not rows:
        return f"<p>{html.esc('Nothing here.')}</p>"
    return (
        '<div class="mo-scroll">'
        + html.table(
            [
                html.esc("Id"),
                html.esc("Label"),
                html.esc("Kind"),
                html.esc("State"),
                html.esc("Owner"),
                html.esc("Authority"),
                html.esc("Path"),
                html.esc("Schedule"),
                html.esc("Counts"),
                html.esc("Run status"),
                html.esc("Run id"),
                html.esc("Completed"),
                html.esc("Duration"),
                html.esc("Evidence"),
                html.esc("Next action"),
            ],
            rows,
        )
        + "</div>"
    )


def _edges_table(edges: list) -> str:
    rows: list[list[str]] = []
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        receipt = edge.get("latest_receipt") if isinstance(edge.get("latest_receipt"), dict) else {}
        enabled = "yes" if edge.get("enabled") else "no"
        rows.append(
            [
                html.esc(_display(edge.get("id"))),
                html.esc(_display(edge.get("from"))),
                html.esc(_display(edge.get("to"))),
                html.esc(_display(edge.get("flow"))),
                html.esc(_display(edge.get("authority"))),
                html.esc(_display(edge.get("writer_component"))),
                html.esc(enabled),
                html.esc(_display(receipt.get("status"))),
                html.esc(_display(receipt.get("run_id"))),
                html.esc(_display(receipt.get("duration_seconds"))),
                html.esc(_display(receipt.get("evidence_state"))),
                html.esc(_display(receipt.get("completed_at"))),
            ]
        )
    if not rows:
        return f"<p>{html.esc('Nothing here.')}</p>"
    return (
        '<div class="mo-scroll">'
        + html.table(
            [
                html.esc("Id"),
                html.esc("From"),
                html.esc("To"),
                html.esc("Flow"),
                html.esc("Authority"),
                html.esc("Writer"),
                html.esc("Enabled"),
                html.esc("Receipt status"),
                html.esc("Receipt"),
                html.esc("Duration"),
                html.esc("Evidence"),
                html.esc("Completed"),
            ],
            rows,
        )
        + "</div>"
    )


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

    filter_bar = _inventory_filters(items)
    rows: list[list[str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        rows.append(_inventory_row(item))

    table = html.table(
        [
            html.esc("Title"),
            html.esc("Id"),
            html.esc("Path"),
            html.esc("Destination"),
            html.esc("Type"),
            html.esc("Category"),
            html.esc("Tags"),
            html.esc("Created"),
            html.esc("Updated"),
            html.esc("Reviewed"),
            html.esc("Fresh until"),
            html.esc("Freshness"),
            html.esc("Review"),
            html.esc("Evidence"),
            html.esc("Source harness"),
            html.esc("Source handoff"),
            html.esc("Owning workflow"),
            html.esc("Mutation"),
            html.esc("Care"),
        ],
        rows,
    )
    # Mark every body row for client filter/pagination (leave thead alone).
    parts = table.split("<tbody>", 1)
    if len(parts) == 2:
        head, rest = parts
        body, tail = rest.split("</tbody>", 1)
        body = body.replace("<tr>", '<tr data-mo-row="1">')
        table = head + "<tbody>" + body + "</tbody>" + tail

    if partial and isinstance(contract_total, int):
        pager_total = contract_total
        total_attr = str(contract_total)
    elif not partial:
        pager_total = inventory.get("total") if isinstance(inventory.get("total"), int) else loaded
        total_attr = str(pager_total)
    else:
        # Partial without safe contract total: do not advertise loaded as complete.
        pager_total = loaded
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
    return html.panel(
        html.esc("Inventory"),
        warn_html + meta + filter_bar + pager + f'<div class="mo-scroll">{table}</div>',
    )


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


def _inventory_row(item: dict) -> list[str]:
    tags = item.get("tags")
    if isinstance(tags, list):
        tag_list = [str(t) for t in tags if t not in (None, "")]
        tag_text = ", ".join(tag_list)
    else:
        tag_list = [] if tags in (None, "") else [str(tags)]
        tag_text = "" if tags in (None, "") else str(tags)
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
        "</span>"
    )
    return [
        title_cell,
        html.esc(_display(item.get("id"))),
        html.esc(_display(item.get("canonical_path"))),
        html.esc(_display(item.get("logical_destination"))),
        html.esc(_display(item.get("store_type"))),
        html.esc(_display(item.get("category"))),
        html.esc(tag_text or "-"),
        html.esc(_display(item.get("created_at"))),
        html.esc(_display(item.get("updated_at"))),
        html.esc(_display(item.get("last_reviewed"))),
        html.esc(_display(item.get("fresh_until"))),
        html.esc(_display(item.get("freshness"))),
        html.esc(_display(item.get("review_state"))),
        html.esc(_display(item.get("evidence_state"))),
        html.esc(_display(item.get("source_harness"))),
        html.esc(_display(item.get("source_handoff"))),
        html.esc(_display(item.get("owning_workflow"))),
        html.esc(mutation_text),
        html.esc(care_text),
    ]


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
  color: #111;
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
  color: #111;
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
  color: #111;
  cursor: pointer;
}}
.mo-scroll {{
  overflow-x: auto;
  -webkit-overflow-scrolling: touch;
}}
.mo-flags, .mo-paths, .mo-flow-list {{
  margin: 0;
  padding-left: 1.2rem;
}}
.mo-muted {{
  color: #444;
  font-size: 0.9rem;
}}
.mo-flow-edge {{
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
