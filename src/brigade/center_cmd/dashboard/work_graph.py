"""Shared helpers for work-graph dashboard views.

Views stay presentation-only: they consume ``work ready --json`` and
``work tasks --json`` via ``data.run_json``, never open ``.brigade/``.

The waves seam understands the additive #803 shape (``waves``,
``wave_count``, ``partition_mode``, ``partition_degraded``) when present.
Until ``--parallel-safe`` lands on main, fetch falls back to explain-only
ready JSON and the waves panel renders an explicit unavailable state.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from brigade.center_cmd.dashboard import data
from brigade.work_cmd import constants as work_constants
from brigade.work_cmd import footprint as footprint_mod

TERMINAL_STATUSES = frozenset({"completed", "done", "cancelled", "canceled"})


def fetch_ready(target: Path) -> dict[str, Any]:
    """Load readiness JSON, preferring ``--parallel-safe`` when the CLI has it."""
    preferred = data.run_json(target, ["work", "ready", "--explain", "--parallel-safe"])
    if not preferred.get("error"):
        return preferred

    error_text = str(preferred.get("error") or "").casefold()
    # Seam: flag not yet on main (or rejected by an older parser).
    if "parallel-safe" in error_text or "unrecognized arguments" in error_text:
        fallback = data.run_json(target, ["work", "ready", "--explain"])
        if not fallback.get("error"):
            enriched = dict(fallback)
            enriched["parallel_safe"] = False
            enriched["waves_unavailable"] = True
            return enriched
        return fallback
    return preferred


def fetch_tasks(target: Path) -> dict[str, Any]:
    """Load the full task ledger view (includes claims and footprints)."""
    return data.run_json(target, ["work", "tasks", "--all"])


def task_index(tasks_payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    tasks = tasks_payload.get("tasks")
    if not isinstance(tasks, list):
        return {}
    indexed: dict[str, dict[str, Any]] = {}
    for task in tasks:
        if isinstance(task, dict) and isinstance(task.get("id"), str):
            indexed[task["id"]] = task
    return indexed


def claim_record(task: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(task, dict):
        return None
    claim = task.get("claim")
    if isinstance(claim, dict) and isinstance(claim.get("claim_id"), str) and claim["claim_id"].strip():
        return claim
    return None


def footprint_of(task: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(task, dict):
        return None
    raw = footprint_mod.task_footprint(task)
    if raw is None:
        return None
    # Preserve degrade markers from the stored object (normalize drops them).
    metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
    stored = metadata.get("footprint") if isinstance(metadata, dict) else None
    if isinstance(stored, dict) and stored.get("degraded") is True:
        raw = dict(raw)
        raw["degraded"] = True
        reason = stored.get("degraded_reason")
        if isinstance(reason, str) and reason.strip():
            raw["degraded_reason"] = reason.strip()
    return raw


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def claim_age_hours(claim: dict[str, Any] | None, *, now: datetime | None = None) -> float | None:
    if not isinstance(claim, dict):
        return None
    claimed_at = _parse_timestamp(claim.get("claimed_at"))
    if claimed_at is None:
        return None
    current = now or datetime.now(timezone.utc)
    return max(0.0, (current - claimed_at).total_seconds() / 3600.0)


def claim_is_stale(claim: dict[str, Any] | None, *, now: datetime | None = None) -> bool:
    age = claim_age_hours(claim, now=now)
    if age is None:
        return False
    return age >= float(work_constants.CLAIM_STALE_HOURS)


def footprint_staleness(
    task: dict[str, Any],
    footprint: dict[str, Any] | None,
) -> str | None:
    """Return a short stale reason, or None when the footprint matches task state.

    Presentation rules (derived from the three-phase lifecycle, not a new
    CLI contract):

    - ``in_progress`` still on ``predicted`` → should have refined at claim
    - terminal status not yet ``reconciled`` → completion join pending
    - stored ``degraded`` predicted footprint → oracle miss, still empty
    """
    if footprint is None:
        status = str(task.get("status") or "pending")
        if status == "in_progress" or status in TERMINAL_STATUSES:
            return "missing footprint"
        return None

    phase = str(footprint.get("phase") or footprint_mod.PHASE_PREDICTED)
    status = str(task.get("status") or "pending")

    if footprint.get("degraded") is True and phase == footprint_mod.PHASE_PREDICTED:
        reason = footprint.get("degraded_reason")
        if isinstance(reason, str) and reason.strip():
            return f"degraded: {reason.strip()}"
        return "degraded predicted footprint"

    if status == "in_progress" and phase == footprint_mod.PHASE_PREDICTED:
        return "still predicted after claim"

    if status in TERMINAL_STATUSES and phase != footprint_mod.PHASE_RECONCILED:
        return f"not reconciled ({phase})"

    return None


def graph_nodes(ready_payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Build display nodes from ready + blocked entries (and edge endpoints)."""
    nodes: dict[str, dict[str, Any]] = {}

    def _ensure(task_id: str, *, kind: str, title: str = "", status: str = "") -> None:
        existing = nodes.get(task_id)
        if existing is None:
            nodes[task_id] = {
                "id": task_id,
                "title": title,
                "kind": kind,
                "status": status,
            }
            return
        if title and not existing.get("title"):
            existing["title"] = title
        if status and not existing.get("status"):
            existing["status"] = status
        # Prefer ready/blocked kinds over generic edge endpoints.
        if existing.get("kind") == "other" and kind != "other":
            existing["kind"] = kind

    for item in ready_payload.get("ready") or []:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        _ensure(
            item["id"],
            kind="ready",
            title=str(item.get("text") or item.get("title") or ""),
            status=str(item.get("status") or "pending"),
        )

    for item in ready_payload.get("blocked") or []:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        _ensure(
            item["id"],
            kind="blocked",
            title=str(item.get("title") or item.get("text") or ""),
            status=str(item.get("status") or "pending"),
        )
        for blocker in item.get("blockers") or []:
            if isinstance(blocker, dict) and isinstance(blocker.get("id"), str):
                _ensure(
                    blocker["id"],
                    kind="other",
                    title="",
                    status=str(blocker.get("status") or ""),
                )

    for edge in ready_payload.get("edges") or []:
        if not isinstance(edge, dict):
            continue
        edge_type = str(edge.get("type") or "")
        if edge_type not in {"blocks", "parent-child"}:
            continue
        for key in ("source", "target"):
            value = edge.get(key)
            if isinstance(value, str) and value.strip():
                _ensure(value.strip(), kind="other")

    ordered = sorted(nodes.values(), key=lambda item: str(item["id"]))
    return ordered


def dependency_edges(ready_payload: dict[str, Any]) -> list[dict[str, str]]:
    """Return edges that should draw on the dependency graph."""
    drawn: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for edge in ready_payload.get("edges") or []:
        if not isinstance(edge, dict):
            continue
        edge_type = str(edge.get("type") or "")
        if edge_type not in {"blocks", "parent-child"}:
            # discovered-from is provenance-only and never gates readiness.
            continue
        source = edge.get("source")
        target = edge.get("target")
        if not isinstance(source, str) or not isinstance(target, str):
            continue
        key = (source, target, edge_type)
        if key in seen:
            continue
        seen.add(key)
        drawn.append({"source": source, "target": target, "type": edge_type})
    return drawn


def layout_layers(nodes: list[dict[str, Any]], edges: list[dict[str, str]]) -> list[list[dict[str, Any]]]:
    """Assign nodes to columns by longest path through ``blocks`` edges."""
    by_id = {str(node["id"]): node for node in nodes}
    blockers: dict[str, set[str]] = {node_id: set() for node_id in by_id}
    for edge in edges:
        if edge.get("type") != "blocks":
            continue
        source = edge["source"]
        target = edge["target"]
        if source in by_id and target in by_id:
            blockers[target].add(source)

    depth: dict[str, int] = {}

    def _depth(node_id: str, stack: set[str]) -> int:
        if node_id in depth:
            return depth[node_id]
        if node_id in stack:
            return 0
        stack.add(node_id)
        parents = blockers.get(node_id) or set()
        value = 0 if not parents else 1 + max(_depth(parent, stack) for parent in parents)
        stack.remove(node_id)
        depth[node_id] = value
        return value

    for node_id in by_id:
        _depth(node_id, set())

    max_depth = max(depth.values()) if depth else 0
    layers: list[list[dict[str, Any]]] = [[] for _ in range(max_depth + 1)]
    for node in sorted(nodes, key=lambda item: (depth.get(str(item["id"]), 0), str(item["id"]))):
        layers[depth.get(str(node["id"]), 0)].append(node)
    return layers
