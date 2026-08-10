"""Typed dependency edges and the computed ready-set resolver for the work ledger.

Edges live as a normalized top-level ``edges`` array inside ``tasks.json``
(schema version 2). Keeping them in the same file as tasks makes bulk graph
applies a single atomic write; a sibling store would need a two-file commit
protocol the ledger does not have today.

Edge direction (pinned for CLI and JSON):

- ``blocks``: ``source`` blocks ``target`` — ``target`` is not ready while
  ``source`` is non-terminal.
- ``parent-child``: ``source`` is the parent, ``target`` is the child.
  Parents are not startable work. Children do not wait for the parent to
  complete, but open blockers on an ancestor propagate to descendants.
  Direct and transitive cycles on ``blocks`` and ``parent-child`` edges are
  rejected at mutation time. ``discovered-from`` is provenance only and never
  participates in readiness or cycle detection.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from . import constants, helpers

EDGE_TYPES = ("blocks", "parent-child", "discovered-from")
READINESS_EDGE_TYPES = frozenset({"blocks", "parent-child"})
TERMINAL_TASK_STATUSES = frozenset({"done", "cancelled", "dismissed"})
READY_EXCLUDED_STATUSES = frozenset({"in_progress", "blocked", "deferred"}) | TERMINAL_TASK_STATUSES

REASON_UNKNOWN_EDGE_TYPE = "unknown_edge_type"
REASON_SELF_EDGE = "self_edge"
REASON_UNKNOWN_SOURCE = "unknown_source"
REASON_UNKNOWN_TARGET = "unknown_target"
REASON_DEPENDENCY_CYCLE = "dependency_cycle"
REASON_OPEN_CHILDREN = "open_children"
REASON_DUPLICATE_NODE_KEY = "duplicate_node_key"
REASON_UNKNOWN_NODE_REF = "unknown_node_ref"
REASON_INVALID_GRAPH = "invalid_graph"

TASK_LEDGER_VERSION = 2


def _ready_dispatch_annotations(task: dict[str, Any]) -> dict[str, str]:
    """Project present, valid seat_class / spend_by hints onto ready items (#815)."""
    raw_metadata = task.get("metadata")
    metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
    annotations: dict[str, str] = {}
    raw_seat = metadata.get("seat_class")
    if isinstance(raw_seat, str) and raw_seat.strip() in constants.TASK_SEAT_CLASSES:
        annotations["seat_class"] = raw_seat.strip()
    raw_spend = metadata.get("spend_by")
    if isinstance(raw_spend, str) and helpers._parse_iso_datetime(raw_spend.strip()) is not None:
        annotations["spend_by"] = raw_spend.strip()
    return annotations


class EdgeError(ValueError):
    """Validation failure for an edge mutation or graph apply."""

    def __init__(self, message: str, *, reason: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.reason = reason
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        payload = {"error": str(self), "reason": self.reason}
        payload.update(self.details)
        return payload


@dataclass(frozen=True)
class ReadinessResult:
    ready: list[dict[str, Any]]
    blocked: list[dict[str, Any]]
    cycles: list[dict[str, Any]]
    orphans: list[dict[str, Any]]
    parents: list[str]

    def as_dict(self, *, explain: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ready": list(self.ready),
            "ready_count": len(self.ready),
            "blocked_count": len(self.blocked),
            "cycle_count": len(self.cycles),
            "orphan_count": len(self.orphans),
            "parent_ids": list(self.parents),
        }
        if explain:
            payload["blocked"] = list(self.blocked)
            payload["cycles"] = list(self.cycles)
            payload["orphans"] = list(self.orphans)
        return payload


def _normalize_edge_type(value: object) -> str:
    if isinstance(value, str) and value.strip() in EDGE_TYPES:
        return value.strip()
    raise EdgeError(
        f"unknown edge type: {value!r} (expected one of: {', '.join(EDGE_TYPES)})",
        reason=REASON_UNKNOWN_EDGE_TYPE,
        details={"edge_type": value},
    )


def _edge_identity(source: str, target: str, edge_type: str) -> tuple[str, str, str]:
    return source, target, edge_type


def _normalize_edge_record(raw: object, *, require_id: bool = False) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    source = raw.get("source")
    target = raw.get("target")
    edge_type = raw.get("type")
    if not isinstance(source, str) or not source.strip():
        return None
    if not isinstance(target, str) or not target.strip():
        return None
    if not isinstance(edge_type, str) or edge_type.strip() not in EDGE_TYPES:
        return None
    edge: dict[str, Any] = {
        "source": source.strip(),
        "target": target.strip(),
        "type": edge_type.strip(),
    }
    edge_id = raw.get("id")
    if isinstance(edge_id, str) and edge_id.strip():
        edge["id"] = edge_id.strip()
    elif require_id:
        return None
    created_at = raw.get("created_at")
    if isinstance(created_at, str) and created_at.strip():
        edge["created_at"] = created_at.strip()
    return edge


def normalize_edges(raw_edges: object) -> list[dict[str, Any]]:
    if not isinstance(raw_edges, list):
        return []
    edges: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for raw in raw_edges:
        edge = _normalize_edge_record(raw)
        if edge is None:
            continue
        key = _edge_identity(edge["source"], edge["target"], edge["type"])
        if key in seen:
            continue
        seen.add(key)
        edges.append(edge)
    edges.sort(key=lambda item: (item["type"], item["source"], item["target"], item.get("id") or ""))
    return edges


def ensure_ledger_edges(ledger: dict[str, Any]) -> list[dict[str, Any]]:
    edges = normalize_edges(ledger.get("edges"))
    ledger["edges"] = edges
    ledger["version"] = TASK_LEDGER_VERSION
    if not isinstance(ledger.get("tasks"), list):
        ledger["tasks"] = []
    return edges


def _task_map(ledger: dict[str, Any]) -> dict[str, dict[str, Any]]:
    tasks: dict[str, dict[str, Any]] = {}
    for task in ledger.get("tasks") or []:
        if not isinstance(task, dict):
            continue
        task_id = task.get("id")
        if isinstance(task_id, str) and task_id.strip():
            tasks[task_id] = task
    return tasks


def _status_of(task: dict[str, Any] | None) -> str:
    if task is None:
        return "missing"
    status = task.get("status", "pending")
    return status if isinstance(status, str) and status.strip() else "pending"


def _is_terminal_status(status: str) -> bool:
    return status in TERMINAL_TASK_STATUSES


def _is_open_blocker_status(status: str) -> bool:
    return status != "missing" and not _is_terminal_status(status)


def _parent_ids(edges: list[dict[str, Any]]) -> set[str]:
    return {edge["source"] for edge in edges if edge["type"] == "parent-child"}


def _children_by_parent(edges: list[dict[str, Any]]) -> dict[str, list[str]]:
    mapping: dict[str, list[str]] = {}
    for edge in edges:
        if edge["type"] != "parent-child":
            continue
        mapping.setdefault(edge["source"], []).append(edge["target"])
    for parent, children in mapping.items():
        mapping[parent] = sorted(set(children))
    return mapping


def _parents_by_child(edges: list[dict[str, Any]]) -> dict[str, list[str]]:
    mapping: dict[str, list[str]] = {}
    for edge in edges:
        if edge["type"] != "parent-child":
            continue
        mapping.setdefault(edge["target"], []).append(edge["source"])
    for child, parents in mapping.items():
        mapping[child] = sorted(set(parents))
    return mapping


def _direct_blockers(edges: list[dict[str, Any]]) -> dict[str, list[str]]:
    mapping: dict[str, list[str]] = {}
    for edge in edges:
        if edge["type"] != "blocks":
            continue
        mapping.setdefault(edge["target"], []).append(edge["source"])
    for target, sources in mapping.items():
        mapping[target] = sorted(set(sources))
    return mapping


def _readiness_adjacency(edges: list[dict[str, Any]]) -> dict[str, set[str]]:
    """Directed adjacency used for cycle detection on readiness-affecting edges.

    ``blocks``: source -> target (dependency flows toward the blocked task).
    ``parent-child``: parent -> child (blocker propagation walks parent to child;
    cycles in this union are rejected so readiness traversal cannot loop).
    """
    adjacency: dict[str, set[str]] = {}
    for edge in edges:
        if edge["type"] not in READINESS_EDGE_TYPES:
            continue
        adjacency.setdefault(edge["source"], set()).add(edge["target"])
        adjacency.setdefault(edge["target"], set())
    return adjacency


def find_readiness_cycles(edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    adjacency = _readiness_adjacency(edges)
    cycles: list[dict[str, Any]] = []
    seen_cycle_keys: set[tuple[str, ...]] = set()
    index = 0
    indices: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()

    def strongconnect(node: str) -> None:
        nonlocal index
        indices[node] = index
        lowlink[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)
        for nxt in sorted(adjacency.get(node, ())):
            if nxt not in indices:
                strongconnect(nxt)
                lowlink[node] = min(lowlink[node], lowlink[nxt])
            elif nxt in on_stack:
                lowlink[node] = min(lowlink[node], indices[nxt])
        if lowlink[node] == indices[node]:
            component: list[str] = []
            while True:
                member = stack.pop()
                on_stack.discard(member)
                component.append(member)
                if member == node:
                    break
            if len(component) > 1 or node in adjacency.get(node, ()):
                ordered = tuple(sorted(component))
                if ordered in seen_cycle_keys:
                    return
                seen_cycle_keys.add(ordered)
                cycles.append(
                    {
                        "reason": REASON_DEPENDENCY_CYCLE,
                        "nodes": list(ordered),
                    }
                )

    for node in sorted(adjacency):
        if node not in indices:
            strongconnect(node)
    cycles.sort(key=lambda item: tuple(item.get("nodes") or ()))
    return cycles


def would_create_readiness_cycle(
    edges: list[dict[str, Any]],
    *,
    source: str,
    target: str,
    edge_type: str,
) -> list[dict[str, Any]]:
    if edge_type not in READINESS_EDGE_TYPES:
        return []
    tentative = [{"source": source, "target": target, "type": edge_type}, *edges]
    return find_readiness_cycles(tentative)


def _ancestor_ids(task_id: str, parents_by_child: dict[str, list[str]]) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    stack = list(parents_by_child.get(task_id, ()))
    while stack:
        parent = stack.pop()
        if parent in seen:
            continue
        seen.add(parent)
        ordered.append(parent)
        stack.extend(parents_by_child.get(parent, ()))
    ordered.sort()
    return ordered


def _blocker_paths_for_task(
    task_id: str,
    *,
    tasks: dict[str, dict[str, Any]],
    direct_blockers: dict[str, list[str]],
    parents_by_child: dict[str, list[str]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (open_blocker_entries, orphan_entries) for one task."""
    open_blockers: list[dict[str, Any]] = []
    orphans: list[dict[str, Any]] = []
    seen_blocker: set[str] = set()
    seen_orphan: set[str] = set()

    def consider(blocker_id: str, via: list[str]) -> None:
        task = tasks.get(blocker_id)
        status = _status_of(task)
        if status == "missing":
            if blocker_id not in seen_orphan:
                seen_orphan.add(blocker_id)
                orphans.append({"id": blocker_id, "via": via, "status": "missing"})
            return
        if not _is_open_blocker_status(status):
            return
        if blocker_id in seen_blocker:
            return
        seen_blocker.add(blocker_id)
        open_blockers.append(
            {
                "id": blocker_id,
                "title": str(task.get("text") or "") if task else "",
                "status": status,
                "via": via,
            }
        )

    for blocker_id in direct_blockers.get(task_id, ()):
        consider(blocker_id, via=[task_id])
    for ancestor in _ancestor_ids(task_id, parents_by_child):
        for blocker_id in direct_blockers.get(ancestor, ()):
            consider(blocker_id, via=[ancestor, task_id])

    open_blockers.sort(key=lambda item: (item["id"], tuple(item.get("via") or ())))
    orphans.sort(key=lambda item: (item["id"], tuple(item.get("via") or ())))
    return open_blockers, orphans


def resolve_readiness(ledger: dict[str, Any]) -> ReadinessResult:
    """Pure readiness resolver over a task ledger payload."""
    edges = normalize_edges(ledger.get("edges"))
    tasks = _task_map(ledger)
    parents = sorted(_parent_ids(edges))
    parent_set = set(parents)
    direct_blockers = _direct_blockers(edges)
    parents_by_child = _parents_by_child(edges)
    cycles = find_readiness_cycles(edges)

    cycle_nodes = {node for cycle in cycles for node in cycle.get("nodes") or []}
    ready: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    orphans: list[dict[str, Any]] = []

    candidates = [
        task
        for task in tasks.values()
        if isinstance(task.get("text"), str)
        and task["text"].strip()
        and _status_of(task) == "pending"
        and _status_of(task) not in READY_EXCLUDED_STATUSES
    ]
    candidates.sort(key=lambda task: str(task.get("created_at") or task.get("id") or ""))

    for task in candidates:
        task_id = str(task["id"])
        if task_id in parent_set:
            blocked.append(
                {
                    "id": task_id,
                    "title": str(task.get("text") or ""),
                    "status": _status_of(task),
                    "reason": "parent_not_startable",
                    "blockers": [],
                }
            )
            continue
        if task_id in cycle_nodes:
            blocked.append(
                {
                    "id": task_id,
                    "title": str(task.get("text") or ""),
                    "status": _status_of(task),
                    "reason": REASON_DEPENDENCY_CYCLE,
                    "blockers": [],
                }
            )
            continue
        open_blockers, task_orphans = _blocker_paths_for_task(
            task_id,
            tasks=tasks,
            direct_blockers=direct_blockers,
            parents_by_child=parents_by_child,
        )
        orphans.extend(task_orphans)
        if open_blockers:
            blocked.append(
                {
                    "id": task_id,
                    "title": str(task.get("text") or ""),
                    "status": _status_of(task),
                    "reason": "open_blockers",
                    "blockers": open_blockers,
                }
            )
            continue
        ready_item: dict[str, Any] = {
            "id": task_id,
            "text": str(task.get("text") or ""),
            "status": _status_of(task),
            "source": task.get("source", "manual"),
            "type": task.get("type", "task"),
            "priority": task.get("priority", "normal"),
            "created_at": task.get("created_at"),
        }
        ready_item.update(_ready_dispatch_annotations(task))
        ready.append(ready_item)

    # Stable ready ordering: created_at then id (already sorted via candidates).
    ready.sort(key=lambda item: (str(item.get("created_at") or ""), str(item.get("id") or "")))
    blocked.sort(key=lambda item: str(item.get("id") or ""))
    # Deduplicate orphans by id+via
    orphan_keys: set[tuple[str, tuple[str, ...]]] = set()
    unique_orphans: list[dict[str, Any]] = []
    for item in orphans:
        key = (str(item.get("id") or ""), tuple(item.get("via") or ()))
        if key in orphan_keys:
            continue
        orphan_keys.add(key)
        unique_orphans.append(item)
    unique_orphans.sort(key=lambda item: (str(item.get("id") or ""), tuple(item.get("via") or ())))

    return ReadinessResult(
        ready=ready,
        blocked=blocked,
        cycles=cycles,
        orphans=unique_orphans,
        parents=parents,
    )


def make_edge(
    *,
    source: str,
    target: str,
    edge_type: str,
    edge_id: str | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    now = created_at or helpers._now().isoformat()
    return {
        "id": edge_id or f"edge-{uuid4().hex[:10]}",
        "source": source,
        "target": target,
        "type": edge_type,
        "created_at": now,
    }


def validate_edge_endpoints(
    ledger: dict[str, Any],
    *,
    source: str,
    target: str,
    edge_type: str,
    allow_missing: bool = False,
) -> str:
    edge_type = _normalize_edge_type(edge_type)
    if source == target:
        raise EdgeError("self-edge rejected", reason=REASON_SELF_EDGE, details={"source": source, "target": target})
    tasks = _task_map(ledger)
    if not allow_missing and source not in tasks:
        raise EdgeError(
            f"unknown source task: {source}",
            reason=REASON_UNKNOWN_SOURCE,
            details={"source": source},
        )
    if not allow_missing and target not in tasks:
        raise EdgeError(
            f"unknown target task: {target}",
            reason=REASON_UNKNOWN_TARGET,
            details={"target": target},
        )
    return edge_type


def add_edge_to_ledger(
    ledger: dict[str, Any],
    *,
    source: str,
    target: str,
    edge_type: str,
    allow_missing: bool = False,
) -> tuple[dict[str, Any], bool]:
    """Add an edge. Returns (edge, created). Duplicate identity is deduped."""
    edges = ensure_ledger_edges(ledger)
    edge_type = validate_edge_endpoints(
        ledger, source=source, target=target, edge_type=edge_type, allow_missing=allow_missing
    )
    identity = _edge_identity(source, target, edge_type)
    for existing in edges:
        if _edge_identity(existing["source"], existing["target"], existing["type"]) == identity:
            return existing, False
    cycles = would_create_readiness_cycle(edges, source=source, target=target, edge_type=edge_type)
    if cycles:
        raise EdgeError(
            "dependency cycle rejected",
            reason=REASON_DEPENDENCY_CYCLE,
            details={"cycles": cycles, "source": source, "target": target, "type": edge_type},
        )
    edge = make_edge(source=source, target=target, edge_type=edge_type)
    edges.append(edge)
    ledger["edges"] = normalize_edges(edges)
    return edge, True


def remove_edge_from_ledger(
    ledger: dict[str, Any],
    *,
    edge_id: str | None = None,
    source: str | None = None,
    target: str | None = None,
    edge_type: str | None = None,
) -> list[dict[str, Any]]:
    edges = ensure_ledger_edges(ledger)
    removed: list[dict[str, Any]] = []
    kept: list[dict[str, Any]] = []
    for edge in edges:
        match = False
        if edge_id and edge.get("id") == edge_id:
            match = True
        elif (
            source
            and target
            and edge_type
            and edge["source"] == source
            and edge["target"] == target
            and edge["type"] == edge_type
        ):
            match = True
        if match:
            removed.append(edge)
        else:
            kept.append(edge)
    ledger["edges"] = normalize_edges(kept)
    return removed


def edges_for_task(ledger: dict[str, Any], task_id: str) -> list[dict[str, Any]]:
    return [edge for edge in ensure_ledger_edges(ledger) if edge["source"] == task_id or edge["target"] == task_id]


def parse_deps_spec(specs: list[str] | None, *, new_task_id: str) -> list[dict[str, str]]:
    """Parse ``type:other_id`` deps for a newly created task.

    - ``blocks:<id>``: ``<id>`` blocks the new task
    - ``discovered-from:<id>``: new task discovered-from ``<id>``
    - ``parent-child:<id>``: ``<id>`` is the parent of the new task
    """
    proposed: list[dict[str, str]] = []
    for raw in specs or []:
        text = raw.strip()
        if not text:
            continue
        if ":" not in text:
            raise EdgeError(
                f"invalid --deps entry (expected type:id): {raw}",
                reason=REASON_INVALID_GRAPH,
                details={"entry": raw},
            )
        edge_type, other = text.split(":", 1)
        edge_type = _normalize_edge_type(edge_type)
        other = other.strip()
        if not other:
            raise EdgeError(
                f"invalid --deps entry (missing id): {raw}",
                reason=REASON_INVALID_GRAPH,
                details={"entry": raw},
            )
        if edge_type == "blocks":
            proposed.append({"type": edge_type, "source": other, "target": new_task_id})
        elif edge_type == "discovered-from":
            proposed.append({"type": edge_type, "source": new_task_id, "target": other})
        else:  # parent-child
            proposed.append({"type": edge_type, "source": other, "target": new_task_id})
    return proposed


def proposed_edges_from_metadata(metadata: dict[str, Any] | None, *, new_task_id: str) -> list[dict[str, str]]:
    if not isinstance(metadata, dict):
        return []
    raw = metadata.get("proposed_edges")
    if raw is None:
        raw = metadata.get("edges")
    if not isinstance(raw, list):
        return []
    proposed: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        edge_type_raw = item.get("type")
        try:
            edge_type = _normalize_edge_type(edge_type_raw)
        except EdgeError:
            continue
        source = item.get("source")
        target = item.get("target")
        if source in {None, "", "self"}:
            source = new_task_id
        if target in {None, "", "self"}:
            target = new_task_id
        if not isinstance(source, str) or not isinstance(target, str):
            continue
        source = source.strip()
        target = target.strip()
        if not source or not target:
            continue
        proposed.append({"type": edge_type, "source": source, "target": target})
    return proposed


def close_side_effects(ledger: dict[str, Any], closed_task_id: str) -> dict[str, Any]:
    """Report newly unblocked tasks and parents whose children are all terminal.

    Caller must already have marked ``closed_task_id`` terminal in ``ledger``.
    """
    edges = ensure_ledger_edges(ledger)
    tasks = _task_map(ledger)
    after = resolve_readiness(ledger)

    affected: set[str] = set()
    for edge in edges:
        if edge["type"] == "blocks" and edge["source"] == closed_task_id:
            affected.add(edge["target"])
    # Parent-blocker propagation: if the closed task blocked a parent, children
    # that only waited on that inherited blocker may now be ready.
    children_by_parent = _children_by_parent(edges)
    stack = list(affected)
    while stack:
        node = stack.pop()
        for child in children_by_parent.get(node, ()):
            if child not in affected:
                affected.add(child)
                stack.append(child)

    newly_unblocked = [item for item in after.ready if item["id"] in affected]
    newly_unblocked.sort(key=lambda item: (str(item.get("created_at") or ""), str(item.get("id") or "")))

    parents_by_child = _parents_by_child(edges)
    completable_parents: list[dict[str, Any]] = []
    for parent_id in parents_by_child.get(closed_task_id, ()):
        children = children_by_parent.get(parent_id, [])
        if not children:
            continue
        if all(_is_terminal_status(_status_of(tasks.get(child_id))) for child_id in children):
            parent = tasks.get(parent_id)
            completable_parents.append(
                {
                    "id": parent_id,
                    "title": str(parent.get("text") or "") if parent else "",
                    "status": _status_of(parent),
                    "children": children,
                }
            )
    completable_parents.sort(key=lambda item: str(item.get("id") or ""))
    return {
        "newly_unblocked": newly_unblocked,
        "completable_parents": completable_parents,
    }


def open_children_for_parent(ledger: dict[str, Any], parent_id: str) -> list[dict[str, Any]]:
    edges = ensure_ledger_edges(ledger)
    tasks = _task_map(ledger)
    children = _children_by_parent(edges).get(parent_id, [])
    open_children: list[dict[str, Any]] = []
    for child_id in children:
        task = tasks.get(child_id)
        status = _status_of(task)
        if _is_terminal_status(status):
            continue
        open_children.append(
            {
                "id": child_id,
                "title": str(task.get("text") or "") if task else "",
                "status": status,
            }
        )
    return open_children


def validate_graph_plan(plan: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """Validate a bulk graph plan before allocating ids.

    Returns (nodes, edges, warnings). Raises EdgeError on hard failures.
    """
    if not isinstance(plan, dict):
        raise EdgeError("graph plan must be a JSON object", reason=REASON_INVALID_GRAPH)

    known_top = {"nodes", "edges", "tasks"}
    warnings = [f"unknown field: {key}" for key in sorted(plan) if key not in known_top]

    raw_nodes = plan.get("nodes")
    if raw_nodes is None:
        raw_nodes = plan.get("tasks")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise EdgeError("graph plan requires a non-empty nodes array", reason=REASON_INVALID_GRAPH)

    known_node_fields = {
        "key",
        "text",
        "type",
        "priority",
        "acceptance",
        "template",
        "source",
        "metadata",
    }
    nodes: list[dict[str, Any]] = []
    keys: set[str] = set()
    for index, raw in enumerate(raw_nodes):
        if not isinstance(raw, dict):
            raise EdgeError(
                f"graph node {index} must be an object",
                reason=REASON_INVALID_GRAPH,
                details={"index": index},
            )
        for field in sorted(raw):
            if field not in known_node_fields:
                warnings.append(f"unknown node field: {field}")
        key = raw.get("key")
        if not isinstance(key, str) or not key.strip():
            raise EdgeError(
                f"graph node {index} missing key",
                reason=REASON_INVALID_GRAPH,
                details={"index": index},
            )
        key = key.strip()
        if key in keys:
            raise EdgeError(
                f"duplicate node key: {key}",
                reason=REASON_DUPLICATE_NODE_KEY,
                details={"key": key},
            )
        keys.add(key)
        text = raw.get("text")
        if not isinstance(text, str) or not text.strip():
            raise EdgeError(
                f"graph node {key!r} missing text",
                reason=REASON_INVALID_GRAPH,
                details={"key": key},
            )
        node: dict[str, Any] = {
            "key": key,
            "text": text.strip(),
            "type": raw.get("type") if isinstance(raw.get("type"), str) else "task",
            "priority": raw.get("priority") if isinstance(raw.get("priority"), str) else "normal",
            "acceptance": raw.get("acceptance") if isinstance(raw.get("acceptance"), list) else [],
            "template": raw.get("template") if isinstance(raw.get("template"), str) else None,
            "source": raw.get("source") if isinstance(raw.get("source"), str) else "graph",
            "metadata": raw.get("metadata") if isinstance(raw.get("metadata"), dict) else None,
        }
        nodes.append(node)

    raw_edges = plan.get("edges")
    if raw_edges is None:
        raw_edges = []
    if not isinstance(raw_edges, list):
        raise EdgeError("graph plan edges must be an array", reason=REASON_INVALID_GRAPH)

    known_edge_fields = {"source", "target", "type"}
    edges: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_edges):
        if not isinstance(raw, dict):
            raise EdgeError(
                f"graph edge {index} must be an object",
                reason=REASON_INVALID_GRAPH,
                details={"index": index},
            )
        for field in sorted(raw):
            if field not in known_edge_fields:
                warnings.append(f"unknown edge field: {field}")
        source = raw.get("source")
        target = raw.get("target")
        if not isinstance(source, str) or source.strip() not in keys:
            raise EdgeError(
                f"graph edge {index} references unknown source: {source!r}",
                reason=REASON_UNKNOWN_NODE_REF,
                details={"index": index, "source": source},
            )
        if not isinstance(target, str) or target.strip() not in keys:
            raise EdgeError(
                f"graph edge {index} references unknown target: {target!r}",
                reason=REASON_UNKNOWN_NODE_REF,
                details={"index": index, "target": target},
            )
        edge_type = _normalize_edge_type(raw.get("type"))
        source = source.strip()
        target = target.strip()
        if source == target:
            raise EdgeError(
                f"graph edge {index} is a self-edge",
                reason=REASON_SELF_EDGE,
                details={"index": index, "source": source},
            )
        edges.append({"source": source, "target": target, "type": edge_type})

    # Cycle check on plan-local keys (same readiness adjacency rules).
    cycles = find_readiness_cycles(edges)
    if cycles:
        raise EdgeError(
            "dependency cycle rejected in graph plan",
            reason=REASON_DEPENDENCY_CYCLE,
            details={"cycles": cycles},
        )
    return nodes, edges, warnings
