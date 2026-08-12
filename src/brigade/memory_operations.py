"""Read-only memory operations contracts (issue #875).

Assembles topology (and later inventory) JSON from existing health helpers.
No new dependencies, config, or durable state.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import care_cmd, evidence_cmd, handoff_cmd, memory_cmd
from .budgets import HANDOFF_BACKLOG_STALE_SECONDS
from .config import load_config
from .localio import utc_now_iso_z
from .selection import WRITER_INBOXES

SCHEMA_NAME = "memory-topology"
SCHEMA_VERSION = 1

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
    "nightly-maintenance": "nightly",
}

_INBOX_TO_HARNESS = {inbox: harness for harness, inbox in WRITER_INBOXES.items()}


def topology_payload(target: Path) -> dict[str, Any]:
    """Build the versioned memory-topology JSON contract for *target*."""
    target = target.expanduser().resolve()
    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []
    paths: list[dict[str, Any]] = []

    care_health = memory_cmd.health(target)
    handoff_health = handoff_cmd.inspect(target)
    draft_queue = handoff_cmd.draft_queue_payload(target)
    evidence = _safe_evidence_status(target)
    care_entries = [_care_entry_view(target, entry) for entry in care_cmd.CARE_ENTRIES]
    care_enabled = _care_jobs_enabled(target)

    _add_shared_stages(nodes)
    _add_canonical_nodes(nodes)
    _add_care_nodes(nodes, care_entries, care_enabled=care_enabled, care_health=care_health)
    _add_evidence_node(nodes, evidence)

    active_harnesses = _active_writer_harnesses(target, handoff_health)
    ingest_receipt = _normalize_receipt(_latest_ingest_receipt(draft_queue))
    for harness, inbox_rel, present, pending, processed in active_harnesses:
        path = _add_harness_path(
            nodes,
            edges,
            harness=harness,
            inbox_rel=inbox_rel,
            present=present,
            pending=pending,
            processed=processed,
            ingest_receipt=ingest_receipt,
        )
        paths.append(path)

    _add_unknown_adapter_nodes(nodes, edges, target)
    _add_care_and_evidence_edges(nodes, edges, care_entries, care_enabled=care_enabled, evidence=evidence)

    health = _health_blocks(
        care_health=care_health,
        handoff_health=handoff_health,
        draft_queue=draft_queue,
        evidence=evidence,
    )
    node_list = list(nodes.values())
    flags = detect_flags(node_list, edges, health=health)
    return {
        "schema_version": SCHEMA_VERSION,
        "schema": {"name": SCHEMA_NAME, "version": SCHEMA_VERSION},
        "target": ".",
        "generated_at": utc_now_iso_z(),
        "nodes": node_list,
        "edges": edges,
        "paths": paths,
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


def detect_flags(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    *,
    health: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Detect duplicate writers, stale receipts, missing owners, missing closeout, and cycles."""
    flags: list[dict[str, Any]] = []
    health = health or {}

    writers_by_dest: dict[str, list[str]] = {}
    for edge in edges:
        if not edge.get("enabled"):
            continue
        if edge.get("authority") not in {"canonical_writer", "derived_writer"} and edge.get("flow") not in {
            "write",
            "mutate",
        }:
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

    for edge in edges:
        receipt = edge.get("latest_receipt")
        if not isinstance(receipt, dict) or receipt.get("evidence_state") != "present":
            continue
        completed = receipt.get("completed_at") or receipt.get("started_at")
        age = _age_seconds(completed)
        if age is not None and age >= HANDOFF_BACKLOG_STALE_SECONDS:
            flags.append(
                {
                    "kind": "stale_receipt",
                    "edge_id": edge.get("id"),
                    "writer_component": edge.get("writer_component"),
                    "detail": f"receipt older than {HANDOFF_BACKLOG_STALE_SECONDS // 86400}d",
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
) -> dict[str, Any]:
    return {
        "id": edge_id,
        "from": frm,
        "to": to,
        "flow": flow,
        "authority": authority,
        "writer_component": writer_component,
        "latest_receipt": latest_receipt,
        "enabled": enabled,
    }


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
            owner="brigade",
            authority="canonical_writer",
            state="canonical",
            path=rel,
        )


def _add_care_nodes(
    nodes: dict[str, dict[str, Any]],
    care_entries: list[dict[str, Any]],
    *,
    care_enabled: bool,
    care_health: dict[str, Any],
) -> None:
    scan_meta = care_health.get("metadata") if isinstance(care_health.get("metadata"), dict) else {}
    nodes["stage:care_scan"] = _node(
        node_id="stage:care_scan",
        kind="stage",
        label="memory care scan",
        owner="brigade",
        authority="queue_producer",
        state="enabled" if care_enabled else "disabled",
        path=".brigade/memory-care",
        schedule={"source": "brigade_care", "cadence": "daily"},
        counts={"issue_count": care_health.get("issue_count")},
        latest_run=_public_run(scan_meta.get("scanned_at") or scan_meta.get("generated_at")),
        next_action="brigade memory care scan",
    )
    queue_count = 0
    top = care_health.get("top_issue")
    if isinstance(care_health.get("checks"), list):
        for check in care_health["checks"]:
            if isinstance(check, dict) and check.get("name") == "memory_care_open_issues":
                detail = str(check.get("detail") or "")
                if detail.startswith("queued"):
                    try:
                        queue_count = int(detail.split()[0].split("=")[-1]) if "=" in detail else 0
                    except ValueError:
                        queue_count = 0
                elif detail not in {"none", "queued issues reviewed or deferred"} and "queued" in detail:
                    # e.g. "3 queued, top=..."
                    try:
                        queue_count = int(detail.split()[0])
                    except ValueError:
                        queue_count = 0
    nodes["stage:refresh_queue"] = _node(
        node_id="stage:refresh_queue",
        kind="queue",
        label="memory care refresh queue",
        owner="brigade",
        authority="queue_producer",
        state="enabled",
        path=".brigade/memory-care",
        counts={"queued": queue_count},
        latest_run=_public_run((care_health.get("latest_closeout") or {}).get("created_at") if isinstance(care_health.get("latest_closeout"), dict) else None),
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
            owner="brigade",
            authority="queue_producer",
            state=state,
            path=str(entry.get("runbook") or ""),
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


def _active_writer_harnesses(
    target: Path, handoff_health: Any
) -> list[tuple[str, str, bool, int, int]]:
    configured: set[str] = set()
    cfg = load_config(target)
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
    return active


def _inbox_has_handoffs(inbox: Path) -> bool:
    if not inbox.is_dir():
        return False
    for path in inbox.glob("*.md"):
        if path.name != "TEMPLATE.md" and path.is_file():
            return True
    return False


def _add_harness_path(
    nodes: dict[str, dict[str, Any]],
    edges: list[dict[str, Any]],
    *,
    harness: str,
    inbox_rel: str,
    present: bool,
    pending: int,
    processed: int,
    ingest_receipt: dict[str, Any],
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
    edges.append(
        _edge(
            edge_id=f"edge:{harness}:emit",
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
            edge_id=f"edge:{harness}:lint",
            frm=inbox_id,
            to="stage:lint",
            flow="validate",
            authority="read_only",
            writer_component=None,
            latest_receipt={"evidence_state": "missing"},
            enabled=True,
        )
    )
    edges.append(
        _edge(
            edge_id=f"edge:{harness}:ingest-stage",
            frm="stage:lint",
            to="stage:ingest",
            flow="approve",
            authority="read_only",
            writer_component=None,
            latest_receipt={"evidence_state": "missing"},
            enabled=True,
        )
    )
    node_ids = [harness_id, inbox_id, "stage:lint", "stage:ingest"]
    for key, _rel, _label in CANONICAL_DESTINATIONS:
        dest_id = f"canonical:{key}"
        edges.append(
            _edge(
                edge_id=f"edge:{harness}:write:{key}",
                frm="stage:ingest",
                to=dest_id,
                flow="write",
                authority="canonical_writer",
                writer_component="ingest",
                latest_receipt=ingest_receipt,
                enabled=True,
            )
        )
        node_ids.append(dest_id)
    return {"id": f"path:{harness}", "harness": harness, "node_ids": node_ids}


def _add_unknown_adapter_nodes(
    nodes: dict[str, dict[str, Any]],
    edges: list[dict[str, Any]],
    target: Path,
) -> None:
    # Writer inboxes already covered. Surface watched source inboxes that are not known writers.
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
        if root != ".":
            # External roots stay visible as unknown without exposing host paths.
            inbox_values = entry.get("inboxes") or []
            if not isinstance(inbox_values, list):
                continue
            for inbox in inbox_values:
                if not isinstance(inbox, str):
                    continue
                node_id = f"adapter:external:{len(seen)}"
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
                    path="external:handoff-source",
                    next_action="identify owning adapter",
                )
            continue
        for inbox in entry.get("inboxes") or []:
            if not isinstance(inbox, str):
                continue
            normalized = inbox.strip().strip("/")
            if normalized in _INBOX_TO_HARNESS or normalized in seen:
                continue
            seen.add(normalized)
            node_id = f"adapter:{normalized}"
            present = (target / normalized).is_dir()
            nodes[node_id] = _node(
                node_id=node_id,
                kind="adapter",
                label=normalized,
                owner="unknown",
                authority="queue_producer",
                state="present" if present else "missing",
                path=normalized,
                next_action="identify owning adapter",
            )
            edges.append(
                _edge(
                    edge_id=f"edge:unknown:{normalized}:lint",
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
    care_enabled: bool,
    evidence: dict[str, Any],
) -> None:
    scan_receipt = {"evidence_state": "missing"}
    for entry in care_entries:
        if entry.get("id") == "daily-care" and isinstance(entry.get("last_receipt"), dict):
            scan_receipt = _normalize_receipt(entry.get("last_receipt"))
            break
    edges.append(
        _edge(
            edge_id="edge:care_scan:queue",
            frm="stage:care_scan",
            to="stage:refresh_queue",
            flow="enqueue",
            authority="queue_producer",
            writer_component="memory-care-scan",
            latest_receipt=scan_receipt,
            enabled=care_enabled,
        )
    )
    # Care refresh can mutate canonical cards when enabled.
    edges.append(
        _edge(
            edge_id="edge:refresh_queue:cards",
            frm="stage:refresh_queue",
            to="canonical:cards",
            flow="mutate",
            authority="canonical_writer",
            writer_component="memory-care",
            latest_receipt=scan_receipt,
            enabled=care_enabled,
        )
    )
    for entry in care_entries:
        entry_id = str(entry["id"])
        node_id = f"care_job:{entry_id}"
        if node_id not in nodes:
            continue
        receipt = _normalize_receipt(entry.get("last_receipt") if isinstance(entry.get("last_receipt"), dict) else None)
        edges.append(
            _edge(
                edge_id=f"edge:care_job:{entry_id}",
                frm=node_id,
                to="stage:care_scan" if entry_id == "daily-care" else "stage:ingest" if entry_id == "ingest-sweep" else "stage:refresh_queue",
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
        evidence_receipt = {
            "evidence_state": "present",
            "status": latest.get("status") or projection.get("status"),
            "completed_at": latest.get("completed_at") or latest.get("started_at"),
            "run_id": latest.get("scan_id") or latest.get("run_id"),
        }
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
) -> dict[str, Any]:
    scan_status = "missing"
    scan_count = None
    for check in care_health.get("checks") or []:
        if isinstance(check, dict) and check.get("name") == "memory_care_scan":
            scan_status = check.get("status") or "unknown"
            break
    queue_status = "missing"
    queue_count = 0
    for check in care_health.get("checks") or []:
        if isinstance(check, dict) and check.get("name") == "memory_care_queue":
            queue_status = check.get("status") or "unknown"
        if isinstance(check, dict) and check.get("name") == "memory_care_open_issues":
            detail = str(check.get("detail") or "")
            if detail and detail[0].isdigit():
                try:
                    queue_count = int(detail.split()[0])
                except ValueError:
                    queue_count = 0
            elif detail in {"none", "queued issues reviewed or deferred"}:
                queue_count = 0

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
        "care_scan": {"status": scan_status, "issue_count": care_health.get("issue_count")},
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
    }


def _care_entry_view(target: Path, entry: care_cmd.CareEntry) -> dict[str, Any]:
    return care_cmd._entry_status(target, entry)


def _care_jobs_enabled(target: Path) -> bool:
    """True when a Brigade care scheduler block is installed for this target."""
    try:
        current, error = care_cmd._read_crontab()
    except Exception:
        return False
    if error or not current:
        return False
    marker = str(target)
    # Avoid leaking that we compared absolute paths in the public payload; this is local only.
    return "BrigadeCare" in current and marker in current


def _safe_evidence_status(target: Path) -> dict[str, Any]:
    try:
        return evidence_cmd.status_payload(target, include_doctor=False, timeout=5.0)
    except Exception as exc:  # noqa: BLE001 - topology must stay read-only and non-fatal
        return {"health": "unknown", "memory_projection": None, "error": type(exc).__name__}


def _latest_ingest_receipt(draft_queue: dict[str, Any]) -> dict[str, Any] | None:
    latest = draft_queue.get("latest_ingest_run")
    return latest if isinstance(latest, dict) else None


def _normalize_receipt(receipt: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(receipt, dict) or not receipt:
        return {"evidence_state": "missing"}
    completed = receipt.get("completed_at") or receipt.get("started_at")
    status = receipt.get("status")
    run_id = receipt.get("run_id")
    if completed is None and status is None and run_id is None:
        return {"evidence_state": "missing"}
    return {
        "evidence_state": "present",
        "status": status,
        "completed_at": completed,
        "run_id": run_id,
    }


def _public_run(timestamp: Any) -> dict[str, Any] | None:
    if not isinstance(timestamp, str) or not timestamp:
        return None
    return {"completed_at": timestamp}


def _public_run_from_receipt(receipt: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(receipt, dict):
        return None
    completed = receipt.get("completed_at") or receipt.get("started_at")
    if not completed and not receipt.get("run_id"):
        return None
    return {
        "run_id": receipt.get("run_id"),
        "status": receipt.get("status"),
        "completed_at": completed,
    }


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


def _detect_cycles(edges: list[dict[str, Any]]) -> list[list[str]]:
    graph: dict[str, list[str]] = {}
    for edge in edges:
        if not edge.get("enabled", True):
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
