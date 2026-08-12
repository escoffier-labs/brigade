"""Read-only memory operations contracts (issue #875).

Assembles topology and inventory JSON from existing health helpers.
No new dependencies, config, or durable state.
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from . import care_cmd, evidence_cmd, handoff_cmd, memory_cmd
from .budgets import HANDOFF_BACKLOG_STALE_SECONDS
from .config import load_config
from .localio import utc_now_iso_z
from .selection import WRITER_INBOXES

SCHEMA_NAME = "memory-topology"
SCHEMA_VERSION = 1

INVENTORY_SCHEMA_NAME = "memory-inventory"
INVENTORY_SCHEMA_VERSION = 1
INVENTORY_DEFAULT_LIMIT = 100
INVENTORY_MAX_LIMIT = 500

STORE_TYPES = ("card", "rule", "tools", "user", "learning")
FRESHNESS_VALUES = ("fresh", "stale", "missing", "unassessable", "not_applicable")
REVIEW_STATES = ("reviewed", "missing")
EVIDENCE_STATES = ("present", "missing", "unresolved")

STORE_LOGICAL = {
    "card": "cards",
    "rule": "rules",
    "tools": "tools",
    "user": "user",
    "learning": "learnings",
}

CANONICAL_OWNER = "canonical memory system"
BRIGADE_OWNER = "brigade"
SCHEDULER_OWNER = "scheduler"
MANUAL_OWNER = "manual process"
EXTERNAL_ADAPTER_OWNER = "external adapter"
EXTERNAL_SCHEDULER_SURFACES = frozenset({"shell_crontab", "openclaw_cron"})

# Legitimate read-only scan edges are exempt from unsafe cycle detection.
CYCLE_EXEMPT_EDGE_IDS = frozenset({"edge:cards:care_scan"})

CANONICAL_DESTINATIONS: tuple[tuple[str, str, str], ...] = (
    ("cards", "memory/cards", "Memory cards"),
    ("rules", "rules", "Rules"),
    ("tools", "TOOLS.md", "TOOLS.md"),
    ("user", "USER.md", "USER.md"),
    ("learnings", ".learnings", "Learnings"),
)

CARE_CADENCE = {
    "daily-care": "daily",
    "ingest-sweep": "every_30m",
    "weekly-outcome-ratchet": "weekly",
    "daily-observability": "daily",
    "nightly-ops": "nightly",
}

# Cadence-aware stale thresholds. Weekly jobs stay healthy past day 4.
CADENCE_STALE_SECONDS = {
    "every_30m": 6 * 60 * 60,
    "daily": HANDOFF_BACKLOG_STALE_SECONDS,
    "nightly": HANDOFF_BACKLOG_STALE_SECONDS,
    "weekly": 8 * 24 * 60 * 60,
    "scheduled": HANDOFF_BACKLOG_STALE_SECONDS,
}

_INBOX_TO_HARNESS = {inbox: harness for harness, inbox in WRITER_INBOXES.items()}


def topology_payload(target: Path) -> dict[str, Any]:
    """Build the versioned memory-topology JSON contract for *target*."""
    target = target.expanduser().resolve()
    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []
    paths: list[dict[str, Any]] = []
    flags: list[dict[str, Any]] = []

    care_health = memory_cmd.health(target)
    handoff_health = handoff_cmd.inspect(target)
    draft_queue = handoff_cmd.draft_queue_payload(target)
    evidence = _safe_evidence_status(target)
    care_status = _safe_care_status(target)
    care_entries = list(care_status.get("entries") or [])
    care_enabled = bool(care_status.get("enabled"))

    _add_shared_stages(nodes)
    _add_canonical_nodes(nodes)
    _add_manual_refresh_node(nodes)
    _add_care_nodes(nodes, care_entries, care_enabled=care_enabled, care_health=care_health)
    _add_evidence_node(nodes, evidence)

    active_harnesses, config_malformed = _active_writer_harnesses(target, handoff_health)
    ingest_receipt = _normalize_receipt(_latest_ingest_receipt(draft_queue))
    shared_edge_ids = _add_shared_ingest_edges(edges, ingest_receipt=ingest_receipt)
    for harness, inbox_rel, present, pending, processed in active_harnesses:
        path = _add_harness_path(
            nodes,
            edges,
            harness=harness,
            inbox_rel=inbox_rel,
            present=present,
            pending=pending,
            processed=processed,
            shared_edge_ids=shared_edge_ids,
        )
        paths.append(path)

    _add_unknown_adapter_nodes(nodes, edges, target)
    _add_external_scheduler_node(nodes, target)
    _add_care_and_evidence_edges(nodes, edges, care_entries, evidence=evidence, target=target)

    health = _health_blocks(
        care_health=care_health,
        handoff_health=handoff_health,
        draft_queue=draft_queue,
        evidence=evidence,
        config_malformed=config_malformed,
    )
    if config_malformed:
        flags.append(
            {
                "kind": "malformed_config",
                "detail": "brigade config.json is malformed or unreadable",
            }
        )

    node_list = sorted(nodes.values(), key=lambda item: str(item.get("id") or ""))
    edge_list = sorted(edges, key=lambda item: str(item.get("id") or ""))
    path_list = sorted(paths, key=lambda item: str(item.get("id") or ""))
    flags.extend(detect_flags(node_list, edge_list, health=health))
    return {
        "schema_version": SCHEMA_VERSION,
        "schema": {"name": SCHEMA_NAME, "version": SCHEMA_VERSION},
        "target": ".",
        "generated_at": utc_now_iso_z(),
        "nodes": node_list,
        "edges": edge_list,
        "paths": path_list,
        "flags": flags,
        "health": health,
    }


def topology(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload = topology_payload(target)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(
        "memory topology: "
        f"nodes={len(payload['nodes'])} edges={len(payload['edges'])} "
        f"paths={len(payload['paths'])} flags={len(payload['flags'])}"
    )
    for key, block in payload["health"].items():
        print(f"health.{key}: {block.get('status')}")
    if payload["flags"]:
        print("flags:")
        for flag in payload["flags"][:12]:
            print(f"- {flag.get('kind')}: {flag.get('detail') or flag.get('destination') or ''}".rstrip(": "))
    return 0


def _inventory_today() -> date:
    return datetime.now(timezone.utc).date()


def inventory_payload(
    target: Path,
    *,
    offset: int = 0,
    limit: int = INVENTORY_DEFAULT_LIMIT,
    store_type: list[str] | None = None,
    category: list[str] | None = None,
    tag: list[str] | None = None,
    freshness: list[str] | None = None,
    review_state: list[str] | None = None,
    evidence_state: list[str] | None = None,
    source_harness: list[str] | None = None,
    owning_workflow: list[str] | None = None,
) -> dict[str, Any]:
    """Build the versioned memory-inventory JSON contract for *target*."""
    target = target.expanduser().resolve()
    filters = {
        "store_type": _normalize_filter_values(store_type),
        "category": _normalize_filter_values(category),
        "tag": _normalize_filter_values(tag),
        "freshness": _normalize_filter_values(freshness),
        "review_state": _normalize_filter_values(review_state),
        "evidence_state": _normalize_filter_values(evidence_state),
        "source_harness": _normalize_filter_values(source_harness),
        "owning_workflow": _normalize_filter_values(owning_workflow),
    }
    care_index = _load_care_index(target)
    items = [
        _inventory_item_from_source(target, source, care_index=care_index)
        for source in _enumerate_inventory_sources(target)
    ]
    items.sort(key=lambda item: str(item.get("canonical_path") or ""))
    filtered = [item for item in items if _item_matches_filters(item, filters)]
    total = len(filtered)
    page = filtered[offset : offset + limit]
    returned = len(page)
    has_more = offset + returned < total
    return {
        "schema_version": INVENTORY_SCHEMA_VERSION,
        "schema": {"name": INVENTORY_SCHEMA_NAME, "version": INVENTORY_SCHEMA_VERSION},
        "target": ".",
        "generated_at": utc_now_iso_z(),
        "pagination": {
            "offset": offset,
            "limit": limit,
            "total": total,
            "returned": returned,
            "has_more": has_more,
            "next_offset": (offset + returned) if has_more else None,
        },
        "filters": filters,
        "items": page,
    }


def inventory(
    *,
    target: Path,
    json_output: bool = False,
    offset: int = 0,
    limit: int = INVENTORY_DEFAULT_LIMIT,
    store_type: list[str] | None = None,
    category: list[str] | None = None,
    tag: list[str] | None = None,
    freshness: list[str] | None = None,
    review_state: list[str] | None = None,
    evidence_state: list[str] | None = None,
    source_harness: list[str] | None = None,
    owning_workflow: list[str] | None = None,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    try:
        payload = inventory_payload(
            target,
            offset=offset,
            limit=limit,
            store_type=store_type,
            category=category,
            tag=tag,
            freshness=freshness,
            review_state=review_state,
            evidence_state=evidence_state,
            source_harness=source_harness,
            owning_workflow=owning_workflow,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    page = payload["pagination"]
    print(
        "memory inventory: "
        f"total={page['total']} offset={page['offset']} limit={page['limit']} "
        f"returned={page['returned']}"
    )
    for item in payload["items"][:12]:
        print(
            f"- {item.get('canonical_path')} [{item.get('store_type')}] "
            f"{item.get('title')} freshness={item.get('freshness')}"
        )
    if page["returned"] > 12:
        print(f"... {page['returned'] - 12} more")
    return 0


def detect_flags(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    *,
    health: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Detect duplicate writers, stale receipts, missing owners, missing closeout, and cycles."""
    flags: list[dict[str, Any]] = []
    health = health or {}
    nodes_by_id = {str(node.get("id")): node for node in nodes if isinstance(node, dict)}

    writers_by_dest: dict[str, list[str]] = {}
    queue_producers: dict[tuple[str, str], list[str]] = {}
    for edge in edges:
        if not edge.get("enabled"):
            continue
        if edge.get("authority") not in {"canonical_writer", "derived_writer"} and edge.get("flow") not in {
            "write",
            "mutate",
        }:
            if edge.get("authority") == "queue_producer":
                dest = str(edge.get("to") or "")
                writer = str(edge.get("writer_component") or edge.get("from") or "unknown")
                queue_producers.setdefault((dest, writer), []).append(str(edge.get("id") or ""))
            continue
        dest = str(edge.get("to") or "")
        writer = str(edge.get("writer_component") or edge.get("from") or "unknown")
        writers_by_dest.setdefault(dest, []).append(writer)
    for dest, writers in sorted(writers_by_dest.items()):
        unique = sorted(set(writers))
        if len(unique) > 1:
            flags.append(
                {
                    "kind": "duplicate_enabled_writer",
                    "destination": dest,
                    "writers": unique,
                    "detail": f"{len(unique)} enabled writers for {dest}",
                }
            )
    for (dest, writer), edge_ids in sorted(queue_producers.items()):
        unique_ids = sorted({edge_id for edge_id in edge_ids if edge_id})
        if len(unique_ids) > 1:
            flags.append(
                {
                    "kind": "duplicate_enabled_queue_producer",
                    "destination": dest,
                    "writer_component": writer,
                    "edge_ids": unique_ids,
                    "detail": f"{len(unique_ids)} enabled queue producers for {dest} ({writer})",
                }
            )

    stale_groups: dict[tuple[str, str, str, int], list[str]] = {}
    for edge in edges:
        if not edge.get("enabled"):
            continue
        receipt = edge.get("latest_receipt")
        if not isinstance(receipt, dict) or receipt.get("evidence_state") != "present":
            continue
        completed = _receipt_completion_timestamp(receipt)
        age = _age_seconds(completed)
        threshold = _stale_threshold_seconds(edge, nodes_by_id)
        if threshold is None or age is None or age < threshold:
            continue
        writer = str(edge.get("writer_component") or "")
        run_id = str(receipt.get("run_id") or "")
        completed_key = str(completed or "")
        edge_id = str(edge.get("id") or "")
        if not edge_id:
            continue
        stale_groups.setdefault((writer, run_id, completed_key, threshold), []).append(edge_id)
    for (writer, _run_id, _completed, threshold), edge_ids in sorted(stale_groups.items()):
        unique_ids = sorted(set(edge_ids))
        flags.append(
            {
                "kind": "stale_receipt",
                "edge_id": unique_ids[0],
                "edge_ids": unique_ids,
                "writer_component": writer,
                "detail": f"receipt older than {max(1, threshold // 86400)}d for cadence",
            }
        )

    for node in nodes:
        if node.get("owner") == "unknown":
            flags.append(
                {
                    "kind": "missing_owner",
                    "node_id": node.get("id"),
                    "detail": f"owner unknown for {node.get('id')}",
                }
            )

    closeout = health.get("closeout") if isinstance(health.get("closeout"), dict) else {}
    refresh = health.get("refresh_queue") if isinstance(health.get("refresh_queue"), dict) else {}
    if closeout.get("status") in {None, "missing", "absent"} and (refresh.get("count") or 0) > 0:
        flags.append(
            {
                "kind": "missing_closeout",
                "detail": "refresh queue has items but no memory-care closeout",
            }
        )

    for cycle in _detect_cycles(edges):
        flags.append({"kind": "cycle", "nodes": cycle, "detail": " -> ".join(cycle)})

    return flags


def _node(
    *,
    node_id: str,
    kind: str,
    label: str,
    owner: str,
    authority: str,
    state: str,
    path: str | None = None,
    schedule: Any = None,
    counts: dict[str, Any] | None = None,
    latest_run: Any = None,
    next_action: str | None = None,
) -> dict[str, Any]:
    return {
        "id": node_id,
        "kind": kind,
        "label": label,
        "owner": owner,
        "authority": authority,
        "state": state,
        "path": path,
        "schedule": schedule,
        "counts": counts or {},
        "latest_run": latest_run,
        "next_action": next_action,
    }


def _edge(
    *,
    edge_id: str,
    frm: str,
    to: str,
    flow: str,
    authority: str,
    writer_component: str | None,
    latest_receipt: dict[str, Any],
    enabled: bool,
    cycle_exempt: bool = False,
) -> dict[str, Any]:
    payload = {
        "id": edge_id,
        "from": frm,
        "to": to,
        "flow": flow,
        "authority": authority,
        "writer_component": writer_component,
        "latest_receipt": latest_receipt,
        "enabled": enabled,
    }
    if cycle_exempt or edge_id in CYCLE_EXEMPT_EDGE_IDS:
        payload["cycle_exempt"] = True
    return payload


def _add_shared_stages(nodes: dict[str, dict[str, Any]]) -> None:
    nodes["stage:lint"] = _node(
        node_id="stage:lint",
        kind="stage",
        label="handoff lint",
        owner="brigade",
        authority="read_only",
        state="enabled",
        path=None,
        next_action="brigade handoff lint",
    )
    nodes["stage:ingest"] = _node(
        node_id="stage:ingest",
        kind="stage",
        label="handoff ingest",
        owner="brigade",
        authority="canonical_writer",
        state="enabled",
        path=None,
        next_action="brigade ingest",
    )


def _add_canonical_nodes(nodes: dict[str, dict[str, Any]]) -> None:
    for key, rel, label in CANONICAL_DESTINATIONS:
        nodes[f"canonical:{key}"] = _node(
            node_id=f"canonical:{key}",
            kind="canonical",
            label=label,
            owner=CANONICAL_OWNER,
            authority="canonical_writer",
            state="canonical",
            path=rel,
        )


def _add_manual_refresh_node(nodes: dict[str, dict[str, Any]]) -> None:
    nodes["stage:manual_refresh"] = _node(
        node_id="stage:manual_refresh",
        kind="manual_writer",
        label="manual card refresh / backfill",
        owner=MANUAL_OWNER,
        authority="canonical_writer",
        state="manual",
        path=".brigade/memory-care",
        schedule={"source": "manual", "cadence": "on_demand"},
        next_action="brigade memory care backfill",
    )


def _add_care_nodes(
    nodes: dict[str, dict[str, Any]],
    care_entries: list[dict[str, Any]],
    *,
    care_enabled: bool,
    care_health: dict[str, Any],
) -> None:
    scan_meta = care_health.get("metadata") if isinstance(care_health.get("metadata"), dict) else {}
    scan_issue_count = care_health.get("scan_issue_count")
    queue_count = int(care_health.get("queue_count") or 0)
    top = care_health.get("top_issue")
    # Brigade owns scan/queue primitives; the operator scheduler owns timing (care_job:*).
    stage_state = "enabled" if care_enabled else "manual"
    nodes["stage:care_scan"] = _node(
        node_id="stage:care_scan",
        kind="stage",
        label="memory care scan",
        owner=BRIGADE_OWNER,
        authority="queue_producer",
        state=stage_state,
        path=".brigade/memory-care",
        schedule={"source": "manual", "cadence": "on_demand"},
        counts={"issue_count": scan_issue_count},
        latest_run=_public_run(scan_meta.get("scanned_at") or scan_meta.get("generated_at")),
        next_action="brigade memory care scan",
    )
    nodes["stage:refresh_queue"] = _node(
        node_id="stage:refresh_queue",
        kind="queue",
        label="memory care refresh queue",
        owner=BRIGADE_OWNER,
        authority="queue_producer",
        state=stage_state,
        path=".brigade/memory-care",
        counts={"queued": queue_count},
        latest_run=_public_run(
            (care_health.get("latest_closeout") or {}).get("created_at")
            if isinstance(care_health.get("latest_closeout"), dict)
            else None
        ),
        next_action="brigade memory care plan-fixes" if top else None,
    )
    for entry in care_entries:
        entry_id = str(entry["id"])
        state = "enabled" if care_enabled else "disabled"
        if not entry.get("runbook_present"):
            state = "missing" if care_enabled else "disabled"
        receipt = entry.get("last_receipt")
        nodes[f"care_job:{entry_id}"] = _node(
            node_id=f"care_job:{entry_id}",
            kind="care_job",
            label=str(entry.get("description") or entry_id),
            owner=SCHEDULER_OWNER,
            authority="queue_producer",
            state=state,
            path=(
                _safe_repo_path(str(entry.get("runbook") or ""))
                or ("redacted:unsafe-path" if entry.get("runbook") else None)
            ),
            schedule={"source": "brigade_care", "cadence": CARE_CADENCE.get(entry_id, "scheduled")},
            counts={},
            latest_run=_public_run_from_receipt(receipt if isinstance(receipt, dict) else None),
            next_action=None if state == "enabled" else "brigade care install",
        )


def _add_evidence_node(nodes: dict[str, dict[str, Any]], evidence: dict[str, Any]) -> None:
    projection = evidence.get("memory_projection") if isinstance(evidence.get("memory_projection"), dict) else {}
    status = projection.get("status") or evidence.get("health") or "unknown"
    nodes["stage:evidence_projection"] = _node(
        node_id="stage:evidence_projection",
        kind="derived",
        label="evidence projection",
        owner="brigade",
        authority="derived_writer",
        state="derived",
        path=".brigade/evidence",
        counts={
            "canonical_count": projection.get("canonical_count"),
            "live_count": projection.get("live_count"),
        },
        latest_run=projection.get("latest_run"),
        next_action="brigade evidence status",
    )
    nodes["stage:evidence_projection"]["counts"]["status"] = status


def _active_writer_harnesses(target: Path, handoff_health: Any) -> tuple[list[tuple[str, str, bool, int, int]], bool]:
    configured: set[str] = set()
    config_malformed = False
    try:
        cfg = load_config(target)
    except (OSError, json.JSONDecodeError, ValueError, TypeError, AttributeError):
        cfg = None
        config_path = target / ".brigade" / "config.json"
        config_malformed = config_path.is_file()
    if cfg is not None:
        configured = {h for h in cfg.selection.harnesses if h in WRITER_INBOXES}

    inbox_stats: dict[str, tuple[bool, int, int]] = {}
    for inbox in getattr(handoff_health, "inboxes", ()) or ():
        harness = _INBOX_TO_HARNESS.get(inbox.inbox)
        if harness is None:
            continue
        inbox_stats[harness] = (bool(inbox.exists), int(inbox.pending or 0), int(inbox.processed or 0))

    active: list[tuple[str, str, bool, int, int]] = []
    for harness, inbox_rel in WRITER_INBOXES.items():
        present = (target / inbox_rel).is_dir()
        pending = inbox_stats.get(harness, (present, 0, 0))[1]
        processed = inbox_stats.get(harness, (present, 0, 0))[2]
        has_items = pending > 0 or processed > 0 or _inbox_has_handoffs(target / inbox_rel)
        if harness in configured or present or has_items:
            active.append((harness, inbox_rel, present, pending, processed))
    return active, config_malformed


def _inbox_has_handoffs(inbox: Path) -> bool:
    if not inbox.is_dir():
        return False
    for path in inbox.glob("*.md"):
        if path.name != "TEMPLATE.md" and path.is_file():
            return True
    return False


def _add_shared_ingest_edges(
    edges: list[dict[str, Any]],
    *,
    ingest_receipt: dict[str, Any],
) -> list[str]:
    shared_ids: list[str] = []
    edges.append(
        _edge(
            edge_id="edge:lint:ingest",
            frm="stage:lint",
            to="stage:ingest",
            flow="approve",
            authority="read_only",
            writer_component=None,
            latest_receipt={"evidence_state": "missing"},
            enabled=True,
        )
    )
    shared_ids.append("edge:lint:ingest")
    for key, _rel, _label in CANONICAL_DESTINATIONS:
        edge_id = f"edge:ingest:write:{key}"
        edges.append(
            _edge(
                edge_id=edge_id,
                frm="stage:ingest",
                to=f"canonical:{key}",
                flow="write",
                authority="canonical_writer",
                writer_component="ingest",
                latest_receipt=ingest_receipt,
                enabled=True,
            )
        )
        shared_ids.append(edge_id)
    return shared_ids


def _add_harness_path(
    nodes: dict[str, dict[str, Any]],
    edges: list[dict[str, Any]],
    *,
    harness: str,
    inbox_rel: str,
    present: bool,
    pending: int,
    processed: int,
    shared_edge_ids: list[str],
) -> dict[str, Any]:
    harness_id = f"harness:{harness}"
    inbox_id = f"inbox:{harness}"
    nodes[harness_id] = _node(
        node_id=harness_id,
        kind="harness",
        label=harness,
        owner=harness,
        authority="queue_producer",
        state="present" if present else "configured",
        path=None,
    )
    nodes[inbox_id] = _node(
        node_id=inbox_id,
        kind="inbox",
        label=f"{harness} handoff inbox",
        owner=harness,
        authority="queue_producer",
        state="present" if present else "missing",
        path=inbox_rel,
        counts={"pending": pending, "processed": processed},
        next_action="brigade handoff lint" if pending else None,
    )
    emit_id = f"edge:{harness}:emit"
    lint_id = f"edge:{harness}:lint"
    edges.append(
        _edge(
            edge_id=emit_id,
            frm=harness_id,
            to=inbox_id,
            flow="emit",
            authority="queue_producer",
            writer_component=None,
            latest_receipt={"evidence_state": "missing"},
            enabled=True,
        )
    )
    edges.append(
        _edge(
            edge_id=lint_id,
            frm=inbox_id,
            to="stage:lint",
            flow="validate",
            authority="read_only",
            writer_component=None,
            latest_receipt={"evidence_state": "missing"},
            enabled=True,
        )
    )
    node_ids = [harness_id, inbox_id, "stage:lint", "stage:ingest"]
    edge_ids = [emit_id, lint_id, *shared_edge_ids]
    for key, _rel, _label in CANONICAL_DESTINATIONS:
        node_ids.append(f"canonical:{key}")
    return {"id": f"path:{harness}", "harness": harness, "node_ids": node_ids, "edge_ids": edge_ids}


def _add_unknown_adapter_nodes(
    nodes: dict[str, dict[str, Any]],
    edges: list[dict[str, Any]],
    target: Path,
) -> None:
    sources_path = handoff_cmd.default_sources_path(target)
    if not sources_path.is_file():
        return
    try:
        payload = json.loads(sources_path.read_text())
    except (OSError, json.JSONDecodeError):
        return
    sources = payload.get("sources") if isinstance(payload, dict) else None
    if not isinstance(sources, list):
        return
    seen: set[str] = set()
    for entry in sources:
        if not isinstance(entry, dict):
            continue
        root = entry.get("root", ".")
        inbox_values = entry.get("inboxes") or []
        if not isinstance(inbox_values, list):
            continue
        if root != ".":
            for inbox in inbox_values:
                if not isinstance(inbox, str):
                    continue
                node_id = _opaque_adapter_id("external", str(root), inbox)
                if node_id in seen:
                    continue
                seen.add(node_id)
                nodes[node_id] = _node(
                    node_id=node_id,
                    kind="adapter",
                    label="external handoff source",
                    owner="unknown",
                    authority="queue_producer",
                    state="unknown",
                    path="redacted:external-root",
                    next_action="identify owning adapter",
                )
            continue
        for inbox in inbox_values:
            if not isinstance(inbox, str):
                continue
            safe = _safe_repo_path(inbox)
            if safe is None:
                node_id = _opaque_adapter_id("redacted", inbox)
                if node_id in seen:
                    continue
                seen.add(node_id)
                nodes[node_id] = _node(
                    node_id=node_id,
                    kind="adapter",
                    label="redacted adapter inbox",
                    owner="unknown",
                    authority="queue_producer",
                    state="unknown",
                    path="redacted:unsafe-path",
                    next_action="identify owning adapter",
                )
                continue
            if safe in _INBOX_TO_HARNESS or safe in seen:
                continue
            seen.add(safe)
            node_id = f"adapter:{safe}"
            present = (target / safe).is_dir()
            nodes[node_id] = _node(
                node_id=node_id,
                kind="adapter",
                label=safe,
                owner="unknown",
                authority="queue_producer",
                state="present" if present else "missing",
                path=safe,
                next_action="identify owning adapter",
            )
            edges.append(
                _edge(
                    edge_id=f"edge:unknown:{safe}:lint",
                    frm=node_id,
                    to="stage:lint",
                    flow="validate",
                    authority="read_only",
                    writer_component=None,
                    latest_receipt={"evidence_state": "missing"},
                    enabled=True,
                )
            )


def _add_care_and_evidence_edges(
    nodes: dict[str, dict[str, Any]],
    edges: list[dict[str, Any]],
    care_entries: list[dict[str, Any]],
    *,
    evidence: dict[str, Any],
    target: Path,
) -> None:
    scan_receipt = {"evidence_state": "missing"}
    for entry in care_entries:
        if entry.get("id") == "daily-care" and isinstance(entry.get("last_receipt"), dict):
            scan_receipt = _normalize_receipt(entry.get("last_receipt"))
            break
    backfill_receipt = _normalize_receipt(_latest_backfill_receipt(target))
    edges.append(
        _edge(
            edge_id="edge:care_scan:queue",
            frm="stage:care_scan",
            to="stage:refresh_queue",
            flow="enqueue",
            authority="queue_producer",
            writer_component="memory-care-scan",
            latest_receipt=scan_receipt,
            # Manual `brigade memory care scan` remains traversable without a scheduler.
            enabled=True,
        )
    )
    edges.append(
        _edge(
            edge_id="edge:queue:manual_refresh",
            frm="stage:refresh_queue",
            to="stage:manual_refresh",
            flow="review",
            authority="read_only",
            writer_component="memory-care-review",
            latest_receipt={"evidence_state": "missing"},
            enabled=True,
        )
    )
    edges.append(
        _edge(
            edge_id="edge:manual_refresh:cards",
            frm="stage:manual_refresh",
            to="canonical:cards",
            flow="mutate",
            authority="canonical_writer",
            writer_component="memory-care-backfill",
            latest_receipt=backfill_receipt,
            # Historical backfill receipts are evidence only; the writer stays manual-disabled.
            enabled=False,
        )
    )
    edges.append(
        _edge(
            edge_id="edge:cards:care_scan",
            frm="canonical:cards",
            to="stage:care_scan",
            flow="scan",
            authority="read_only",
            writer_component="memory-care-scan",
            latest_receipt=scan_receipt,
            enabled=True,
            cycle_exempt=True,
        )
    )
    for entry in care_entries:
        entry_id = str(entry["id"])
        node_id = f"care_job:{entry_id}"
        if node_id not in nodes:
            continue
        receipt = _normalize_receipt(
            entry.get("last_receipt") if isinstance(entry.get("last_receipt"), dict) else None,
            next_action=nodes[node_id].get("next_action"),
        )
        if entry_id == "daily-care":
            destination = "stage:care_scan"
        elif entry_id == "ingest-sweep":
            destination = "stage:ingest"
        else:
            destination = "stage:refresh_queue"
        edges.append(
            _edge(
                edge_id=f"edge:care_job:{entry_id}",
                frm=node_id,
                to=destination,
                flow="schedule",
                authority="queue_producer",
                writer_component=entry_id,
                latest_receipt=receipt,
                enabled=nodes[node_id]["state"] == "enabled",
            )
        )
    projection = evidence.get("memory_projection") if isinstance(evidence.get("memory_projection"), dict) else {}
    evidence_receipt = {"evidence_state": "missing"}
    latest = projection.get("latest_run")
    if isinstance(latest, dict) and latest:
        evidence_receipt = _normalize_receipt(
            {
                "status": latest.get("status") or projection.get("status"),
                "started_at": latest.get("started_at"),
                "finished_at": latest.get("finished_at"),
                "completed_at": latest.get("completed_at"),
                "duration_seconds": latest.get("duration_seconds"),
                "run_id": latest.get("scan_id") or latest.get("run_id"),
            }
        )
    edges.append(
        _edge(
            edge_id="edge:cards:evidence",
            frm="canonical:cards",
            to="stage:evidence_projection",
            flow="project",
            authority="derived_writer",
            writer_component="evidence-projection",
            latest_receipt=evidence_receipt,
            enabled=True,
        )
    )


def _health_blocks(
    *,
    care_health: dict[str, Any],
    handoff_health: Any,
    draft_queue: dict[str, Any],
    evidence: dict[str, Any],
    config_malformed: bool,
) -> dict[str, Any]:
    scan_status = "missing"
    for check in care_health.get("checks") or []:
        if isinstance(check, dict) and check.get("name") == "memory_care_scan":
            scan_status = check.get("status") or "unknown"
            break
    queue_status = "missing"
    for check in care_health.get("checks") or []:
        if isinstance(check, dict) and check.get("name") == "memory_care_queue":
            queue_status = check.get("status") or "unknown"
            break
    queue_count = int(care_health.get("queue_count") or 0)
    scan_issue_count = care_health.get("scan_issue_count")

    pending_total = sum(int(inbox.pending or 0) for inbox in getattr(handoff_health, "inboxes", ()) or ())
    backlog_status = "ok" if pending_total == 0 else "warn"
    draft_counts = draft_queue.get("counts") if isinstance(draft_queue.get("counts"), dict) else {}

    closeout = care_health.get("latest_closeout")
    if isinstance(closeout, dict) and closeout:
        closeout_block: dict[str, Any] = {
            "status": str(closeout.get("status") or "ok"),
            "closeout_id": closeout.get("closeout_id"),
            "created_at": closeout.get("created_at"),
        }
    else:
        closeout_block = {"status": "missing", "closeout_id": None, "created_at": None}

    projection = evidence.get("memory_projection") if isinstance(evidence.get("memory_projection"), dict) else None
    if projection is None:
        evidence_block: dict[str, Any] = {
            "status": str(evidence.get("health") or "missing"),
            "healthy": None,
            "capability": None,
        }
    else:
        evidence_block = {
            "status": str(projection.get("status") or evidence.get("health") or "unknown"),
            "healthy": projection.get("healthy"),
            "capability": projection.get("capability"),
        }

    return {
        "care_scan": {"status": scan_status, "issue_count": scan_issue_count},
        "refresh_queue": {"status": queue_status, "count": queue_count},
        "handoff_backlog": {
            "status": backlog_status,
            "pending": pending_total,
            "draft_pending": draft_counts.get("pending"),
            "draft_reviewed": draft_counts.get("reviewed"),
        },
        "quarantine": {"status": "unknown", "count": None},
        "closeout": closeout_block,
        "evidence_projection": evidence_block,
        "config": {
            "status": "malformed" if config_malformed else "ok",
        },
    }


def _safe_care_status(target: Path) -> dict[str, Any]:
    try:
        return care_cmd.status_payload(target=target)
    except Exception:  # noqa: BLE001 - topology must stay read-only and non-fatal
        return {
            "enabled": False,
            "backends": {
                "crontab": {"status": "unreadable"},
                "systemd": {"status": "unreadable"},
            },
            "entries": [
                {
                    "id": entry.entry_id,
                    "schedule": entry.schedule,
                    "on_calendar": entry.on_calendar,
                    "kind": entry.kind,
                    "description": entry.description,
                    "runbook": entry.runbook_rel,
                    "runbook_id": entry.runbook_id,
                    "runbook_present": (target / entry.runbook_rel).is_file() if entry.runbook_rel else False,
                    "last_receipt": None,
                }
                for entry in care_cmd.CARE_ENTRIES
            ],
        }


def _safe_evidence_status(target: Path) -> dict[str, Any]:
    try:
        return evidence_cmd.status_payload(target, include_doctor=False, timeout=5.0)
    except Exception as exc:  # noqa: BLE001 - topology must stay read-only and non-fatal
        return {"health": "unknown", "memory_projection": None, "error": type(exc).__name__}


def _latest_ingest_receipt(draft_queue: dict[str, Any]) -> dict[str, Any] | None:
    latest = draft_queue.get("latest_ingest_run")
    return latest if isinstance(latest, dict) else None


def _latest_backfill_receipt(target: Path) -> dict[str, Any] | None:
    receipts_dir = target / ".brigade" / "memory-care" / "backfills"
    if not receipts_dir.is_dir():
        return None
    candidates = sorted(receipts_dir.glob("*.json"))
    if not candidates:
        return None
    try:
        payload = json.loads(candidates[-1].read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _receipt_completion_timestamp(receipt: dict[str, Any]) -> Any:
    return (
        receipt.get("finished_at")
        or receipt.get("completed_at")
        or receipt.get("generated_at")
        or receipt.get("started_at")
    )


def _normalize_receipt(
    receipt: dict[str, Any] | None,
    *,
    next_action: str | None = None,
) -> dict[str, Any]:
    if not isinstance(receipt, dict) or not receipt:
        return {"evidence_state": "missing"}
    completed = _receipt_completion_timestamp(receipt)
    status = receipt.get("status")
    run_id = receipt.get("run_id")
    started_at = receipt.get("started_at")
    duration_seconds = receipt.get("duration_seconds")
    if completed is None and status is None and run_id is None:
        return {"evidence_state": "missing"}
    payload: dict[str, Any] = {
        "evidence_state": "present",
        "status": status,
        "started_at": started_at,
        "completed_at": completed,
        "run_id": run_id,
    }
    if isinstance(duration_seconds, (int, float)):
        payload["duration_seconds"] = duration_seconds
    if next_action:
        payload["next_action"] = next_action
    return payload


def _public_run(timestamp: Any) -> dict[str, Any] | None:
    if not isinstance(timestamp, str) or not timestamp:
        return None
    return {"completed_at": timestamp}


def _public_run_from_receipt(receipt: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(receipt, dict):
        return None
    completed = _receipt_completion_timestamp(receipt)
    if not completed and not receipt.get("run_id"):
        return None
    payload: dict[str, Any] = {
        "run_id": receipt.get("run_id"),
        "status": receipt.get("status"),
        "completed_at": completed,
    }
    if receipt.get("started_at"):
        payload["started_at"] = receipt.get("started_at")
    duration_seconds = receipt.get("duration_seconds")
    if isinstance(duration_seconds, (int, float)):
        payload["duration_seconds"] = duration_seconds
    return payload


def _safe_repo_path(value: str) -> str | None:
    """Return a repo-relative path, or None when the value is absolute/home/Windows."""
    text = value.strip()
    if not text:
        return None
    while text.startswith("./"):
        text = text[2:]
    if text.startswith("~") or text.startswith("/") or text.startswith("\\"):
        return None
    if "\\" in text:
        return None
    if len(text) >= 2 and text[1] == ":" and text[0].isalpha():
        return None
    normalized = text.replace("\\", "/").strip("/")
    if not normalized or normalized.startswith("..") or "/../" in f"/{normalized}/":
        return None
    parts = Path(normalized).parts
    if any(part == ".." for part in parts):
        return None
    return normalized


def _stale_threshold_seconds(edge: dict[str, Any], nodes_by_id: dict[str, dict[str, Any]]) -> int | None:
    writer = str(edge.get("writer_component") or "")
    if writer in CARE_CADENCE:
        cadence = CARE_CADENCE[writer]
        return int(CADENCE_STALE_SECONDS.get(cadence, HANDOFF_BACKLOG_STALE_SECONDS))
    frm = nodes_by_id.get(str(edge.get("from") or ""))
    if isinstance(frm, dict) and isinstance(frm.get("schedule"), dict):
        cadence = str(frm["schedule"].get("cadence") or "")
        if cadence in {"on_demand", "manual"}:
            return None
        if cadence in CADENCE_STALE_SECONDS:
            return int(CADENCE_STALE_SECONDS[cadence])
    to = nodes_by_id.get(str(edge.get("to") or ""))
    if isinstance(to, dict) and isinstance(to.get("schedule"), dict):
        cadence = str(to["schedule"].get("cadence") or "")
        if cadence in {"on_demand", "manual"}:
            return None
    return int(HANDOFF_BACKLOG_STALE_SECONDS)


def _opaque_adapter_id(kind: str, *material: str) -> str:
    digest = hashlib.sha256(":".join(material).encode()).hexdigest()[:12]
    return f"adapter:{kind}:{digest}"


def _age_seconds(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds())


def _add_external_scheduler_node(nodes: dict[str, dict[str, Any]], target: Path) -> None:
    try:
        from .operator_cmd.surfaces import surfaces_list_payload
    except ImportError:
        return
    listed = surfaces_list_payload(target)
    if not isinstance(listed, dict) or listed.get("status") != "captured":
        return
    records = listed.get("records") if isinstance(listed.get("records"), list) else []
    scheduler_records = [
        record
        for record in records
        if isinstance(record, dict) and str(record.get("surface") or "") in EXTERNAL_SCHEDULER_SURFACES
    ]
    if not scheduler_records:
        return
    nodes["adapter:external_scheduler"] = _node(
        node_id="adapter:external_scheduler",
        kind="adapter",
        label="external scheduler",
        owner=EXTERNAL_ADAPTER_OWNER,
        authority="queue_producer",
        state="external",
        path="redacted:operator-surfaces",
        schedule={"source": "external", "cadence": "unknown"},
        counts={"record_count": len(scheduler_records)},
        next_action="brigade operator surfaces list --json",
    )


def _detect_cycles(edges: list[dict[str, Any]]) -> list[list[str]]:
    graph: dict[str, list[str]] = {}
    for edge in edges:
        if not edge.get("enabled", True):
            continue
        if edge.get("cycle_exempt") or edge.get("id") in CYCLE_EXEMPT_EDGE_IDS:
            continue
        frm = str(edge.get("from") or "")
        to = str(edge.get("to") or "")
        if not frm or not to:
            continue
        graph.setdefault(frm, []).append(to)

    cycles: list[list[str]] = []
    visiting: set[str] = set()
    visited: set[str] = set()
    stack: list[str] = []

    def dfs(node: str) -> None:
        if node in visiting:
            if node in stack:
                idx = stack.index(node)
                cycles.append(stack[idx:] + [node])
            return
        if node in visited:
            return
        visiting.add(node)
        stack.append(node)
        for nxt in graph.get(node, []):
            dfs(nxt)
        stack.pop()
        visiting.remove(node)
        visited.add(node)

    for node in list(graph):
        dfs(node)
    return cycles


def normalize_inventory_enum_filter(
    values: list[str] | None,
    *,
    allowed: tuple[str, ...],
    flag: str,
) -> list[str] | None:
    """Normalize case-insensitive enum filters; raise ValueError on unknown values."""
    if not values:
        return None
    allowed_set = {item.lower(): item for item in allowed}
    normalized: list[str] = []
    for value in values:
        key = str(value).strip().lower()
        if key not in allowed_set:
            raise ValueError(f"{flag} must be one of: {', '.join(allowed)}")
        canonical = allowed_set[key]
        if canonical not in normalized:
            normalized.append(canonical)
    return normalized


def _normalize_filter_values(values: list[str] | None) -> list[str]:
    if not values:
        return []
    normalized: list[str] = []
    for value in values:
        text = str(value).strip().lower()
        if text and text not in normalized:
            normalized.append(text)
    return normalized


def _enumerate_inventory_sources(target: Path) -> list[tuple[str, Path, str]]:
    """Return (store_type, absolute_path, canonical_path) in path order."""
    target = target.expanduser().resolve()
    sources: list[tuple[str, Path, str]] = []
    seen: set[str] = set()

    try:
        config = memory_cmd._config_or_default(target)
    except ValueError:
        config = memory_cmd.MemoryCareConfig()

    for path in memory_cmd._iter_cards(target, config):
        rel = _canonical_relpath(target, path)
        if rel is None or rel in seen:
            continue
        seen.add(rel)
        sources.append(("card", path, rel))

    rules_dir = target / "rules"
    if rules_dir.is_dir() and not rules_dir.is_symlink():
        for path in sorted(rules_dir.glob("*.md")):
            if path.is_symlink() or not path.is_file():
                continue
            rel = _canonical_relpath(target, path)
            if rel is None or rel in seen:
                continue
            seen.add(rel)
            sources.append(("rule", path, rel))

    for store_type, rel in (("tools", "TOOLS.md"), ("user", "USER.md")):
        path = target / rel
        if path.is_symlink() or not path.is_file():
            continue
        if rel in seen:
            continue
        if _canonical_relpath(target, path) is None:
            continue
        seen.add(rel)
        sources.append((store_type, path, rel))

    learnings = target / ".learnings"
    if learnings.is_dir() and not learnings.is_symlink():
        for path in sorted(learnings.glob("*.md")):
            if path.is_symlink() or not path.is_file():
                continue
            rel = _canonical_relpath(target, path)
            if rel is None or rel in seen:
                continue
            seen.add(rel)
            sources.append(("learning", path, rel))

    sources.sort(key=lambda item: item[2])
    return sources


def _canonical_relpath(target: Path, path: Path) -> str | None:
    try:
        resolved = path.resolve()
        rel = resolved.relative_to(target.resolve())
    except (OSError, ValueError):
        return None
    if path.is_symlink():
        return None
    text = str(rel).replace("\\", "/")
    if text.startswith("../") or text == ".." or "/../" in f"/{text}/":
        return None
    return text


def _read_frontmatter_only(path: Path) -> dict[str, Any]:
    try:
        # Read whole file for the shared frontmatter splitter; body is discarded.
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    meta, _has = memory_cmd._parse_frontmatter(text)
    return meta if isinstance(meta, dict) else {}


def _inventory_item_from_source(
    target: Path,
    source: tuple[str, Path, str],
    *,
    care_index: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    store_type, path, canonical_path = source
    meta = _read_frontmatter_only(path)
    title_value = memory_cmd._frontmatter_value(meta, "title")
    title = str(title_value).strip() if title_value not in (None, "") else path.stem
    category_value = memory_cmd._frontmatter_value(meta, "category")
    category = str(category_value).strip() if category_value not in (None, "") else None
    tags = _inventory_tags(meta)
    created_at = _explicit_date_string(meta, "created", "created_at")
    updated_at = _explicit_date_string(meta, "updated", "updated_at")
    last_reviewed = _explicit_date_string(meta, "last_reviewed", "last_reviewed_at", "reviewed_at")
    fresh_until_raw = memory_cmd._frontmatter_value(meta, "fresh_until", "expires_at", "expires")
    fresh_until = None
    freshness = _compute_freshness(store_type, fresh_until_raw)
    if isinstance(fresh_until_raw, str) and fresh_until_raw.strip():
        parsed = memory_cmd._parse_date(fresh_until_raw)
        fresh_until = parsed.isoformat() if parsed is not None else fresh_until_raw.strip()
    review_state = "reviewed" if last_reviewed is not None else "missing"
    evidence_state = _compute_evidence_state(meta, canonical_path, care_index=care_index)
    source_harness_value = memory_cmd._frontmatter_value(meta, "source_harness")
    source_harness = str(source_harness_value).strip() if source_harness_value not in (None, "") else "unknown"
    source_handoff_value = memory_cmd._frontmatter_value(meta, "source_handoff")
    source_handoff = str(source_handoff_value).strip() if source_handoff_value not in (None, "") else None
    owning_workflow_value = memory_cmd._frontmatter_value(meta, "owning_workflow")
    owning_workflow = str(owning_workflow_value).strip() if owning_workflow_value not in (None, "") else "unknown"
    return {
        "id": f"{store_type}:{canonical_path}",
        "title": title,
        "canonical_path": canonical_path,
        "logical_destination": STORE_LOGICAL[store_type],
        "store_type": store_type,
        "category": category,
        "tags": tags,
        "created_at": created_at,
        "updated_at": updated_at,
        "last_reviewed": last_reviewed,
        "fresh_until": fresh_until,
        "freshness": freshness,
        "review_state": review_state,
        "evidence_state": evidence_state,
        "source_harness": source_harness,
        "source_handoff": source_handoff,
        "owning_workflow": owning_workflow,
        "last_mutation": _inventory_last_mutation(meta),
        "care": _care_for_path(canonical_path, care_index),
    }


def _inventory_tags(meta: dict[str, Any]) -> list[str]:
    value = memory_cmd._frontmatter_value(meta, "tags", "tag")
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text else []


def _explicit_date_string(meta: dict[str, Any], *keys: str) -> str | None:
    value = memory_cmd._frontmatter_value(meta, *keys)
    if not isinstance(value, str) or not value.strip():
        return None
    parsed = memory_cmd._parse_date(value)
    return parsed.isoformat() if parsed is not None else value.strip()


def _compute_freshness(store_type: str, fresh_until_raw: Any) -> str:
    if fresh_until_raw in (None, ""):
        return "missing" if store_type == "card" else "not_applicable"
    if not isinstance(fresh_until_raw, str):
        return "unassessable"
    parsed = memory_cmd._parse_date(fresh_until_raw)
    if parsed is None:
        return "unassessable"
    today = _inventory_today()
    if parsed < today:
        return "stale"
    return "fresh"


def _compute_evidence_state(
    meta: dict[str, Any],
    canonical_path: str,
    *,
    care_index: dict[str, dict[str, Any]],
) -> str:
    if not memory_cmd._has_evidence(meta):
        return "missing"
    care = care_index.get(canonical_path) or {}
    raw_issues = care.get("issues")
    issues: list[str] = [str(item) for item in raw_issues] if isinstance(raw_issues, list) else []
    if "missing-evidence-ref" in issues:
        return "unresolved"
    return "present"


def _inventory_last_mutation(meta: dict[str, Any]) -> dict[str, Any] | None:
    workflow = memory_cmd._frontmatter_value(meta, "last_mutation_workflow")
    if workflow in (None, ""):
        return None
    status = memory_cmd._frontmatter_value(meta, "last_mutation_status")
    run_id = memory_cmd._frontmatter_value(meta, "last_mutation_run_id")
    completed_at = memory_cmd._frontmatter_value(meta, "last_mutation_completed_at")
    evidence_state = memory_cmd._frontmatter_value(meta, "last_mutation_evidence_state")
    duration_raw = memory_cmd._frontmatter_value(meta, "last_mutation_duration_seconds")
    duration_seconds: float | int | None
    if duration_raw in (None, ""):
        duration_seconds = None
    else:
        try:
            duration_seconds = float(duration_raw)
            if duration_seconds.is_integer():
                duration_seconds = int(duration_seconds)
        except (TypeError, ValueError):
            duration_seconds = None
    receipt = {
        "evidence_state": str(evidence_state).strip() if evidence_state not in (None, "") else "missing",
        "status": str(status).strip() if status not in (None, "") else None,
        "run_id": str(run_id).strip() if run_id not in (None, "") else None,
        "completed_at": str(completed_at).strip() if completed_at not in (None, "") else None,
        "duration_seconds": duration_seconds,
    }
    return {"workflow": str(workflow).strip(), "receipt": receipt}


def _load_care_index(target: Path) -> dict[str, dict[str, Any]]:
    """Join persisted care scan/queue facts by exact repo-relative path; fail open."""
    index: dict[str, dict[str, Any]] = {}
    try:
        config = memory_cmd._config_or_default(target)
    except ValueError:
        config = memory_cmd.MemoryCareConfig()

    scan_payload = _safe_json_object(memory_cmd._scan_path(target, config))
    queue_payload = _safe_json_object(memory_cmd._queue_path(target, config))

    for issue in _care_issue_rows(scan_payload):
        path = str(issue.get("file") or issue.get("path") or issue.get("card_file") or "").replace("\\", "/")
        if not path:
            continue
        entry = index.setdefault(path, {"issues": [], "queued_action": None})
        issue_type = str(issue.get("issue_type") or "").strip()
        if issue_type and issue_type not in entry["issues"]:
            entry["issues"].append(issue_type)
        action = issue.get("action")
        if entry["queued_action"] is None and isinstance(action, str) and action.strip():
            entry["queued_action"] = action.strip()

    for card in _care_issue_rows(queue_payload, key="cards"):
        path = str(card.get("file") or card.get("path") or card.get("card_file") or "").replace("\\", "/")
        if not path:
            continue
        entry = index.setdefault(path, {"issues": [], "queued_action": None})
        issue_type = str(card.get("issue_type") or card.get("refresh_reason") or "").strip()
        if issue_type and issue_type not in entry["issues"]:
            entry["issues"].append(issue_type)
        action = card.get("action")
        if entry["queued_action"] is None and isinstance(action, str) and action.strip():
            entry["queued_action"] = action.strip()
    return index


def _safe_json_object(path: Path) -> dict[str, Any] | None:
    try:
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _care_issue_rows(payload: dict[str, Any] | None, *, key: str = "issues") -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    rows = payload.get(key)
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _care_for_path(canonical_path: str, care_index: dict[str, dict[str, Any]]) -> dict[str, Any]:
    entry = care_index.get(canonical_path) or {}
    raw_issues = entry.get("issues")
    issues = [str(item) for item in raw_issues if str(item).strip()] if isinstance(raw_issues, list) else []
    queued = entry.get("queued_action")
    return {
        "issues": issues,
        "queued_action": str(queued).strip() if isinstance(queued, str) and queued.strip() else None,
    }


def _item_matches_filters(item: dict[str, Any], filters: dict[str, list[str]]) -> bool:
    if filters["store_type"] and str(item.get("store_type") or "").lower() not in filters["store_type"]:
        return False
    if filters["category"]:
        category = str(item.get("category") or "").lower()
        if category not in filters["category"]:
            return False
    if filters["tag"]:
        tags = {str(tag).lower() for tag in (item.get("tags") or [])}
        if not all(tag in tags for tag in filters["tag"]):
            return False
    if filters["freshness"] and str(item.get("freshness") or "").lower() not in filters["freshness"]:
        return False
    if filters["review_state"] and str(item.get("review_state") or "").lower() not in filters["review_state"]:
        return False
    if filters["evidence_state"] and str(item.get("evidence_state") or "").lower() not in filters["evidence_state"]:
        return False
    if filters["source_harness"] and str(item.get("source_harness") or "").lower() not in filters["source_harness"]:
        return False
    if filters["owning_workflow"] and str(item.get("owning_workflow") or "").lower() not in filters["owning_workflow"]:
        return False
    return True
