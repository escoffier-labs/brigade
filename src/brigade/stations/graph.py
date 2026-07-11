"""Deterministic station capability graph."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any, Iterable

from . import catalog

GRAPH_SCHEMA = "brigade.stations.graph.v1"
_SLUG_RE = re.compile(r"[^a-z0-9._:-]+")


def _slug(value: object) -> str:
    text = str(value).strip().lower()
    text = _SLUG_RE.sub("-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text or "unknown"


def _node(nodes: dict[str, dict[str, Any]], node_id: str, kind: str, label: str, **extra: Any) -> None:
    node_id = _slug(node_id)
    nodes.setdefault(
        node_id,
        {
            "id": node_id,
            "kind": kind,
            "label": label,
            **extra,
        },
    )


def _edge(
    edges: dict[str, dict[str, Any]],
    source: str,
    target: str,
    relation: str,
    **extra: Any,
) -> None:
    source_id = _slug(source)
    target_id = _slug(target)
    relation_id = _slug(relation)
    edge_id = f"{source_id}->{target_id}:{relation_id}"
    edges.setdefault(
        edge_id,
        {
            "id": edge_id,
            "source": source_id,
            "target": target_id,
            "relation": relation_id,
            **extra,
        },
    )


def _surface_node_id(tool_node_id: str, surface_kind: str, index: int) -> str:
    return f"surface:{tool_node_id}:{surface_kind}:{index}"


def graph_payload(*, external_manifests: Iterable[Path | str] = ()) -> dict[str, Any]:
    catalog_payload = catalog.catalog_payload(external_manifests=external_manifests)
    nodes: dict[str, dict[str, Any]] = {}
    edges: dict[str, dict[str, Any]] = {}

    for row in catalog_payload["rows"]:
        station_name = row["station"]["name"]
        station_node_id = f"station:{station_name}"
        _node(nodes, station_node_id, "station", station_name, source=row["source"])

        tool = row["tool"]
        tool_node_id = f"{row['source']}:{tool['name']}" if row["source"] == "managed" else row["id"]
        _node(
            nodes,
            tool_node_id,
            "tool",
            tool["name"],
            source=row["source"],
            station=station_name,
            lifecycle=row["lifecycle"],
            compatible=row["compatible"],
        )
        _edge(edges, station_node_id, tool_node_id, "has-tool")

        for index, surface in enumerate(tool.get("surfaces") or []):
            surface_kind = surface["kind"]
            surface_node_id = _surface_node_id(tool_node_id, surface_kind, index)
            _node(
                nodes,
                surface_node_id,
                "surface",
                surface_kind,
                source=row["source"],
                read_only=surface.get("read_only"),
            )
            _edge(edges, tool_node_id, surface_node_id, "exposes")
            if row["source"] == "managed":
                capability_node_id = f"capability:{surface_kind}"
                _node(nodes, capability_node_id, "capability", surface_kind, derived_from="surface")
                _edge(edges, tool_node_id, capability_node_id, "produces")

        for relation in ("produces", "consumes", "dependencies"):
            for capability in tool.get(relation) or []:
                capability_node_id = f"capability:{capability}"
                _node(nodes, capability_node_id, "capability", capability)
                if relation == "consumes":
                    _edge(edges, capability_node_id, tool_node_id, relation)
                else:
                    _edge(edges, tool_node_id, capability_node_id, relation)

    node_list = sorted(nodes.values(), key=lambda node: node["id"])
    edge_list = sorted(edges.values(), key=lambda edge: edge["id"])
    return {
        "schema": GRAPH_SCHEMA,
        "node_count": len(node_list),
        "edge_count": len(edge_list),
        "nodes": node_list,
        "edges": edge_list,
        "catalog": {
            "row_count": catalog_payload["row_count"],
            "source_counts": catalog_payload["source_counts"],
            "compatibility_counts": catalog_payload["compatibility_counts"],
            "errors": catalog_payload["errors"],
        },
    }
