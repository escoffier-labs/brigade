"""Versioned code-graph export contract for Center and operators (#887).

Assembles module map, impact drill-down, and optional change overlay by calling
the Brigade Code facade (graphtrail subprocess). Dashboard code must use
``brigade code export --json`` via ``data.run_json``; it never opens the graph
database directly.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path, PurePosixPath
from typing import Any

from . import context_cmd, proc
from .localio import utc_now_iso_z

SCHEMA = "brigade.code-graph-export.v1"
SCHEMA_VERSION = 1

MODULE_CAP = 15
EDGE_CAP = 24
MAX_BLAST_HOPS = 5
_TOP_FILES = 5
_TEST_CAP = 8
_SEARCH_LIMIT = 12
_GRAPH_TIMEOUT = 30.0


def parse_blast_hops(raw: object, default: int = 1) -> int:
    """Clamp a hop-depth query to ``1..MAX_BLAST_HOPS``."""
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return max(1, min(MAX_BLAST_HOPS, value))


def export_payload(
    target: Path,
    *,
    symbol: str | None = None,
    overlay: bool = False,
    focus: str | None = None,
    up_hops: int = 1,
    down_hops: int = 1,
) -> dict[str, Any]:
    """Build the versioned export JSON contract for *target*."""
    target = target.expanduser().resolve()
    binary = context_cmd._graphtrail_bin()
    if binary is None:
        return _error_payload(target, "graphtrail is not installed; run `brigade setup`")

    db_path = target / ".graphtrail" / "graphtrail.db"
    if not db_path.is_file():
        return _error_payload(target, "code graph is not indexed; run `brigade code sync`")

    stats = _run_json(binary, db_path, target, "stats", "--json")
    if stats is None:
        return _error_payload(target, "code graph stats unavailable")

    file_graph = _load_file_graph(binary, db_path, target)
    if file_graph is None:
        return _error_payload(target, "code graph export unavailable")

    changed_files = _git_changed_files(target) if overlay else []
    module_map = _build_module_map(file_graph, changed_files=changed_files)

    payload: dict[str, Any] = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "target": ".",
        "generated_at": utc_now_iso_z(),
        "stats": stats,
        "module_map": module_map,
    }

    if symbol and symbol.strip():
        payload["impact"] = _impact_section(binary, db_path, target, symbol.strip())

    changed_modules: list[str] = []
    if overlay:
        changed_modules = sorted({_module_key(path) for path in changed_files if _module_key(path)})
        payload["change_overlay"] = {
            "changed_files": changed_files,
            "changed_modules": changed_modules,
        }

    seed = (focus or "").strip()
    if not seed and len(changed_modules) == 1:
        seed = changed_modules[0]
    if seed:
        payload["blast_radius"] = focus_neighborhood(
            file_graph,
            seed,
            up_hops=parse_blast_hops(up_hops),
            down_hops=parse_blast_hops(down_hops),
            changed_modules=set(changed_modules),
        )

    return payload


def run_cli(target: Path, forwarded: list[str]) -> int:
    """Dispatch ``brigade code export`` when ``--json`` is present."""
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2

    json_output = False
    symbol: str | None = None
    overlay = False
    focus: str | None = None
    up_hops = 1
    down_hops = 1
    index = 0
    while index < len(forwarded):
        token = forwarded[index]
        if token == "--json":
            json_output = True
            index += 1
            continue
        if token == "--symbol":
            if index + 1 >= len(forwarded):
                print("error: --symbol requires a value", file=sys.stderr)
                return 2
            symbol = forwarded[index + 1]
            index += 2
            continue
        if token.startswith("--symbol="):
            symbol = token.split("=", 1)[1]
            index += 1
            continue
        if token == "--overlay":
            overlay = True
            index += 1
            continue
        if token == "--focus":
            if index + 1 >= len(forwarded):
                print("error: --focus requires a value", file=sys.stderr)
                return 2
            focus = forwarded[index + 1]
            index += 2
            continue
        if token.startswith("--focus="):
            focus = token.split("=", 1)[1]
            index += 1
            continue
        if token == "--up":
            if index + 1 >= len(forwarded):
                print("error: --up requires a value", file=sys.stderr)
                return 2
            up_hops = parse_blast_hops(forwarded[index + 1])
            index += 2
            continue
        if token.startswith("--up="):
            up_hops = parse_blast_hops(token.split("=", 1)[1])
            index += 1
            continue
        if token == "--down":
            if index + 1 >= len(forwarded):
                print("error: --down requires a value", file=sys.stderr)
                return 2
            down_hops = parse_blast_hops(forwarded[index + 1])
            index += 2
            continue
        if token.startswith("--down="):
            down_hops = parse_blast_hops(token.split("=", 1)[1])
            index += 1
            continue
        if token in {"--format", "--scope", "--out"}:
            if token in {"--format", "--scope", "--out"} and index + 1 < len(forwarded):
                index += 2
                continue
        index += 1

    if not json_output:
        print(
            "error: brigade code export requires --json for the Center contract "
            "(use graphtrail export for dot/graphml/jsonl)",
            file=sys.stderr,
        )
        return 2

    payload = export_payload(
        target,
        symbol=symbol,
        overlay=overlay,
        focus=focus,
        up_hops=up_hops,
        down_hops=down_hops,
    )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if "error" not in payload else 1


def _error_payload(target: Path, message: str) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "target": ".",
        "generated_at": utc_now_iso_z(),
        "stats": {},
        "module_map": {
            "modules": [],
            "edges": [],
            "truncation": _truncation_note(0, 0, 0, 0),
        },
        "error": message,
    }


def _run_json(binary: str, db_path: Path, target: Path, verb: str, *extra: str) -> Any | None:
    argv = [binary, "--db", str(db_path), verb, *extra]
    result = proc.run(argv, timeout=_GRAPH_TIMEOUT, cwd=target)
    if result.code != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def _run_text(binary: str, db_path: Path, target: Path, verb: str, *extra: str) -> str | None:
    argv = [binary, "--db", str(db_path), verb, *extra]
    result = proc.run(argv, timeout=_GRAPH_TIMEOUT, cwd=target)
    if result.code != 0:
        return None
    return result.stdout


def _load_file_graph(binary: str, db_path: Path, target: Path) -> dict[str, Any] | None:
    raw = _run_text(
        binary,
        db_path,
        target,
        "export",
        "--format",
        "jsonl",
        "--scope",
        "files",
    )
    if raw is None:
        return None
    nodes: dict[str, int] = {}
    edges: list[tuple[str, str, int]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        if row.get("type") == "node":
            file_id = str(row.get("id") or "")
            symbols = row.get("symbols")
            count = int(symbols) if isinstance(symbols, int) and symbols >= 0 else 0
            if file_id:
                nodes[file_id] = count
        elif row.get("type") == "edge":
            source = str(row.get("source") or "")
            target_file = str(row.get("target") or "")
            weight = row.get("calls")
            if not isinstance(weight, int) or weight < 0:
                weight = 1
            if source and target_file:
                edges.append((source, target_file, weight))
    return {"nodes": nodes, "edges": edges}


def _canonical_file_path(file_path: str) -> str | None:
    """Return a repo-relative POSIX path, or None to drop (worktrees / empty)."""
    normalized = file_path.replace("\\", "/").strip()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    parts = PurePosixPath(normalized).parts
    if not parts or parts == (".",):
        return None
    if ".worktrees" in parts:
        return None
    return str(PurePosixPath(*parts))


def _module_key(file_path: str) -> str:
    canonical = _canonical_file_path(file_path)
    if canonical is None:
        return ""
    parts = PurePosixPath(canonical).parts
    if not parts:
        return "(root)"
    if "tests" in parts:
        index = parts.index("tests")
        return str(PurePosixPath(*parts[: index + 1]))
    if parts[0] == "src" and len(parts) >= 4:
        return str(PurePosixPath(*parts[:3]))
    if parts[0] == "src" and len(parts) == 3:
        return str(PurePosixPath(*parts[:2]))
    if parts[0] == "engines" and len(parts) >= 4:
        return str(PurePosixPath(*parts[:3]))
    if parts[0] == "engines" and len(parts) == 3:
        return str(PurePosixPath(*parts[:2]))
    if len(parts) >= 2:
        return str(PurePosixPath(*parts[:-1]))
    return parts[0]


def _module_label(module_id: str) -> str:
    parts = PurePosixPath(module_id).parts
    name = parts[-1] if parts else module_id
    if name == "tests" and len(parts) >= 2:
        return f"{parts[-2]} tests"
    return name or module_id


def _package_group(module_id: str) -> str:
    parts = PurePosixPath(module_id).parts
    if not parts:
        return "(root)"
    if parts[0] == "src":
        return parts[1] if len(parts) > 1 else "src"
    if parts[0] == "engines":
        return parts[1] if len(parts) > 1 else "engines"
    if parts[-1] == "tests" and len(parts) >= 2:
        return f"{parts[-2]} tests"
    return parts[0]


def _is_test_path(file_path: str) -> bool:
    return "tests" in PurePosixPath(file_path).parts


def _uncapped_module_graph(file_graph: dict[str, Any]) -> dict[str, Any]:
    """Build the full module adjacency (no MODULE_CAP / EDGE_CAP)."""
    raw_nodes = file_graph.get("nodes")
    nodes: dict[str, int] = raw_nodes if isinstance(raw_nodes, dict) else {}
    raw_edge_list = file_graph.get("edges")
    raw_edges: list[tuple[str, str, int]] = raw_edge_list if isinstance(raw_edge_list, list) else []

    labels: dict[str, str] = {}
    seen_files: set[str] = set()
    for file_path in nodes:
        canonical = _canonical_file_path(str(file_path))
        if canonical is None or canonical in seen_files:
            continue
        seen_files.add(canonical)
        module_id = _module_key(canonical)
        if module_id:
            labels[module_id] = _module_label(module_id)

    edge_weight: dict[tuple[str, str], int] = defaultdict(int)
    seen_edge_files: set[tuple[str, str]] = set()
    inbound: dict[str, set[str]] = defaultdict(set)
    outbound: dict[str, set[str]] = defaultdict(set)
    for source, target_file, weight in raw_edges:
        source_path = _canonical_file_path(str(source))
        target_path = _canonical_file_path(str(target_file))
        if source_path is None or target_path is None:
            continue
        edge_key = (source_path, target_path)
        if edge_key in seen_edge_files:
            continue
        seen_edge_files.add(edge_key)
        source_mod = _module_key(source_path)
        target_mod = _module_key(target_path)
        if not source_mod or not target_mod or source_mod == target_mod:
            continue
        amount = int(weight) if isinstance(weight, int) and weight >= 0 else 1
        edge_weight[(source_mod, target_mod)] += amount
        inbound[target_mod].add(source_mod)
        outbound[source_mod].add(target_mod)
        labels.setdefault(source_mod, _module_label(source_mod))
        labels.setdefault(target_mod, _module_label(target_mod))

    return {
        "labels": labels,
        "edge_weight": dict(edge_weight),
        "inbound": {key: set(value) for key, value in inbound.items()},
        "outbound": {key: set(value) for key, value in outbound.items()},
    }


def _resolve_focus_id(labels: dict[str, str], focus: str) -> str | None:
    requested = focus.strip()
    if not requested:
        return None
    if requested in labels:
        return requested
    lowered = requested.lower()
    for module_id in labels:
        if module_id.lower() == lowered:
            return module_id
    for module_id, label in labels.items():
        if label == requested:
            return module_id
    for module_id, label in labels.items():
        if label.lower() == lowered:
            return module_id
    keyed = _module_key(requested)
    if keyed in labels:
        return keyed
    return None


def _walk_hops(
    start: str,
    adjacency: dict[str, set[str]],
    hops: int,
    *,
    edge_weight: dict[tuple[str, str], int],
    labels: dict[str, str],
    inbound: bool,
) -> list[dict[str, Any]]:
    seen: set[str] = {start}
    frontier: set[str] = {start}
    rows: list[dict[str, Any]] = []
    depth = parse_blast_hops(hops)
    for hop in range(1, depth + 1):
        nxt: set[str] = set()
        hop_rows: list[dict[str, Any]] = []
        for node in frontier:
            for neighbor in adjacency.get(node, ()):
                if neighbor in seen:
                    continue
                seen.add(neighbor)
                nxt.add(neighbor)
                pair = (neighbor, node) if inbound else (node, neighbor)
                hop_rows.append(
                    {
                        "id": neighbor,
                        "label": labels.get(neighbor, _module_label(neighbor)),
                        "hops": hop,
                        "weight": int(edge_weight.get(pair, 0)),
                    }
                )
        hop_rows.sort(key=lambda row: (-int(row["weight"]), str(row["id"])))
        rows.extend(hop_rows)
        frontier = nxt
        if not frontier:
            break
    return rows


def focus_neighborhood(
    file_graph: dict[str, Any],
    focus: str,
    *,
    up_hops: int = 1,
    down_hops: int = 1,
    changed_modules: set[str] | None = None,
) -> dict[str, Any]:
    """Return uncapped upstream/downstream rows for *focus* from the full graph.

    This is the blast-radius query helper: neighbors are walked on the complete
    module adjacency, never on a MODULE_CAP / EDGE_CAP truncated export.
    """
    graph = _uncapped_module_graph(file_graph)
    labels: dict[str, str] = graph["labels"]
    edge_weight: dict[tuple[str, str], int] = graph["edge_weight"]
    inbound: dict[str, set[str]] = graph["inbound"]
    outbound: dict[str, set[str]] = graph["outbound"]
    resolved = _resolve_focus_id(labels, focus)
    changed = changed_modules or set()
    up_depth = parse_blast_hops(up_hops)
    down_depth = parse_blast_hops(down_hops)
    if resolved is None:
        requested = focus.strip() or focus
        return {
            "focus": {
                "id": requested,
                "label": requested,
                "changed": requested in changed,
            },
            "resolved": False,
            "upstream": [],
            "downstream": [],
            "upstream_depth": up_depth,
            "downstream_depth": down_depth,
        }

    upstream = _walk_hops(
        resolved,
        inbound,
        up_depth,
        edge_weight=edge_weight,
        labels=labels,
        inbound=True,
    )
    downstream = _walk_hops(
        resolved,
        outbound,
        down_depth,
        edge_weight=edge_weight,
        labels=labels,
        inbound=False,
    )
    for row in (*upstream, *downstream):
        row["changed"] = str(row["id"]) in changed
    return {
        "focus": {
            "id": resolved,
            "label": labels.get(resolved, _module_label(resolved)),
            "changed": resolved in changed,
        },
        "resolved": True,
        "upstream": upstream,
        "downstream": downstream,
        "upstream_depth": up_depth,
        "downstream_depth": down_depth,
    }


def _insight_ref(
    module_id: str,
    *,
    symbol_count: int | None = None,
    inbound: int | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"id": module_id, "label": _module_label(module_id)}
    if symbol_count is not None:
        payload["symbol_count"] = symbol_count
    if inbound is not None:
        payload["inbound"] = inbound
    return payload


def _build_module_map(file_graph: dict[str, Any], *, changed_files: list[str]) -> dict[str, Any]:
    raw_nodes = file_graph.get("nodes")
    nodes: dict[str, int] = raw_nodes if isinstance(raw_nodes, dict) else {}
    raw_edge_list = file_graph.get("edges")
    raw_edges: list[tuple[str, str, int]] = raw_edge_list if isinstance(raw_edge_list, list) else []

    seen_files: set[str] = set()
    module_symbols: dict[str, int] = defaultdict(int)
    module_files: dict[str, int] = defaultdict(int)
    files_by_module: dict[str, list[tuple[str, int]]] = defaultdict(list)
    changed_modules = {key for path in changed_files if (key := _module_key(path))}

    for file_path, symbol_count in nodes.items():
        canonical = _canonical_file_path(str(file_path))
        if canonical is None or canonical in seen_files:
            continue
        seen_files.add(canonical)
        module_id = _module_key(canonical)
        if not module_id:
            continue
        count = int(symbol_count) if isinstance(symbol_count, int) and symbol_count >= 0 else 0
        module_symbols[module_id] += count
        module_files[module_id] += 1
        files_by_module[module_id].append((canonical, count))

    module_edges: dict[tuple[str, str], int] = defaultdict(int)
    tests_for_module: dict[str, set[str]] = defaultdict(set)
    seen_edge_files: set[tuple[str, str]] = set()
    for source, target_file, weight in raw_edges:
        source_path = _canonical_file_path(str(source))
        target_path = _canonical_file_path(str(target_file))
        if source_path is None or target_path is None:
            continue
        edge_key = (source_path, target_path)
        if edge_key in seen_edge_files:
            continue
        seen_edge_files.add(edge_key)
        source_mod = _module_key(source_path)
        target_mod = _module_key(target_path)
        if not source_mod or not target_mod or source_mod == target_mod:
            continue
        amount = int(weight) if isinstance(weight, int) and weight >= 0 else 1
        module_edges[(source_mod, target_mod)] += amount
        if _is_test_path(source_path) and not _is_test_path(target_path):
            tests_for_module[target_mod].add(source_path)
        elif _is_test_path(target_path) and not _is_test_path(source_path):
            tests_for_module[source_mod].add(target_path)

    inbound_mods: dict[str, set[str]] = defaultdict(set)
    outbound_mods: dict[str, set[str]] = defaultdict(set)
    inbound_weight: dict[str, int] = defaultdict(int)
    outbound_weight: dict[str, int] = defaultdict(int)
    for (source_mod, target_mod), weight in module_edges.items():
        inbound_mods[target_mod].add(source_mod)
        outbound_mods[source_mod].add(target_mod)
        inbound_weight[target_mod] += weight
        outbound_weight[source_mod] += weight

    def connectivity(module_id: str) -> tuple[int, int, int, str]:
        degree = len(inbound_mods[module_id]) + len(outbound_mods[module_id])
        weight = inbound_weight[module_id] + outbound_weight[module_id]
        return (degree, weight, module_symbols[module_id], module_id)

    ranked = sorted(
        module_symbols,
        key=lambda module_id: (
            -connectivity(module_id)[0],
            -connectivity(module_id)[1],
            -connectivity(module_id)[2],
            module_id,
        ),
    )
    pinned = [module_id for module_id in ranked if module_id in changed_modules]
    remainder = [module_id for module_id in ranked if module_id not in changed_modules]
    shown_ids_list = (pinned + remainder)[:MODULE_CAP]
    shown_ids = set(shown_ids_list)
    total_modules = len(module_symbols)
    hidden_modules = max(0, total_modules - len(shown_ids_list))

    modules = []
    for module_id in shown_ids_list:
        top_files = sorted(files_by_module[module_id], key=lambda item: (-item[1], item[0]))[:_TOP_FILES]
        dependents = sorted(_module_label(other) for other in inbound_mods[module_id])
        tests = sorted(tests_for_module[module_id])[:_TEST_CAP]
        row: dict[str, Any] = {
            "id": module_id,
            "label": _module_label(module_id),
            "symbol_count": module_symbols[module_id],
            "file_count": module_files[module_id],
            "package": _package_group(module_id),
            "inbound_count": len(inbound_mods[module_id]),
            "dependents": dependents,
            "top_files": [{"path": path, "symbol_count": count} for path, count in top_files],
            "attributed_tests": tests,
        }
        if module_id in changed_modules:
            row["changed"] = True
        modules.append(row)

    edges_sorted = sorted(module_edges.items(), key=lambda item: (-item[1], item[0][0], item[0][1]))
    visible_edges = [(pair, weight) for pair, weight in edges_sorted if pair[0] in shown_ids and pair[1] in shown_ids]
    shown_edge_rows = visible_edges[:EDGE_CAP]
    hidden_edges = max(0, len(module_edges) - len(shown_edge_rows))
    edges = [{"from": source, "to": target, "weight": weight} for (source, target), weight in shown_edge_rows]

    production_ids = [module_id for module_id in module_symbols if not _is_test_path(module_id)]
    insight_ids = production_ids or list(module_symbols)
    package_symbols: dict[str, int] = defaultdict(int)
    for module_id in insight_ids:
        package_symbols[_package_group(module_id)] += module_symbols[module_id]
    core_package = max(package_symbols, key=lambda name: (package_symbols[name], name), default="")
    core_id = max(
        (module_id for module_id in insight_ids if _package_group(module_id) == core_package),
        key=lambda module_id: (module_symbols[module_id], module_id),
        default="",
    )
    largest_id = max(module_symbols, key=lambda module_id: (module_symbols[module_id], module_id), default="")
    hub_id = max(
        insight_ids,
        key=lambda module_id: (len(inbound_mods[module_id]), inbound_weight[module_id], module_id),
        default="",
    )
    isolated_count = sum(1 for module_id in insight_ids if not inbound_mods[module_id] and not outbound_mods[module_id])
    total_symbols = sum(module_symbols.values())
    insights: dict[str, Any] = {
        "total_modules": total_modules,
        "isolated_count": isolated_count,
        "total_symbols": total_symbols,
    }
    if core_package:
        insights["core"] = {"id": core_id, "label": core_package, "symbol_count": total_symbols}
    if largest_id:
        insights["largest"] = _insight_ref(largest_id, symbol_count=module_symbols[largest_id])
    if hub_id:
        insights["most_connected"] = _insight_ref(hub_id, inbound=len(inbound_mods[hub_id]))
    changed_ranked = [module_id for module_id in ranked if module_id in changed_modules]
    if changed_ranked:
        change_id = changed_ranked[0]
        insights["biggest_change"] = _insight_ref(change_id, inbound=len(inbound_mods[change_id]))

    trunc = _truncation_note(len(modules), hidden_modules, len(edges), hidden_edges)
    return {"modules": modules, "edges": edges, "truncation": trunc, "insights": insights}


def _truncation_note(
    shown_modules: int,
    hidden_modules: int,
    shown_edges: int,
    hidden_edges: int,
) -> dict[str, Any]:
    total_modules = shown_modules + hidden_modules
    note_parts: list[str] = []
    if hidden_modules:
        note_parts.append(f"showing top {shown_modules} of {total_modules} modules")
        note_parts.append(f"{hidden_edges} edges hidden")
    elif hidden_edges:
        note_parts.append(f"{hidden_edges} edges hidden")
    note = ", ".join(note_parts)
    return {
        "shown_modules": shown_modules,
        "hidden_modules": hidden_modules,
        "shown_edges": shown_edges,
        "hidden_edges": hidden_edges,
        **({"note": note} if note else {}),
    }


def _impact_argv(binary: str, db: str, verb: str, query: str) -> list[str]:
    if query.startswith("-"):
        return [binary, "--db", db, verb, "--json", "--", query]
    return [binary, "--db", db, verb, query, "--json"]


def _impact_section(binary: str, db_path: Path, target: Path, query: str) -> dict[str, Any]:
    db = str(db_path)
    search = _run_json(binary, db_path, target, "search", query, "--json")
    search_hits = search if isinstance(search, list) else []
    resolved = search_hits[0] if search_hits and isinstance(search_hits[0], dict) else None
    impact_query = str((resolved or {}).get("qualified_name") or query)

    impact_result = proc.run(_impact_argv(binary, db, "impact", impact_query), timeout=_GRAPH_TIMEOUT, cwd=target)
    impact_edges: list[Any] = []
    if impact_result.code == 0:
        try:
            parsed = json.loads(impact_result.stdout)
            if isinstance(parsed, list):
                impact_edges = parsed
        except json.JSONDecodeError:
            impact_edges = []

    callers_result = proc.run(_impact_argv(binary, db, "callers", impact_query), timeout=_GRAPH_TIMEOUT, cwd=target)
    callers: list[Any] = []
    if callers_result.code == 0:
        try:
            parsed = json.loads(callers_result.stdout)
            if isinstance(parsed, list):
                callers = parsed
        except json.JSONDecodeError:
            callers = []

    impacted_files = sorted(
        {
            str(row.get("source_file") or row.get("target_file") or "").strip()
            for row in impact_edges
            if isinstance(row, dict)
        }
        - {""}
    )
    affected: dict[str, Any] | None = None
    if impacted_files:
        argv = [binary, "--db", db, "affected", *impacted_files[:12], "--json"]
        affected_result = proc.run(argv, timeout=_GRAPH_TIMEOUT, cwd=target)
        if affected_result.code == 0:
            try:
                parsed = json.loads(affected_result.stdout)
                if isinstance(parsed, dict):
                    affected = parsed
            except json.JSONDecodeError:
                affected = None

    return {
        "query": query,
        "resolved_symbol": resolved,
        "search_hits": search_hits[:_SEARCH_LIMIT],
        "callers": callers,
        "edges": impact_edges,
        "affected_tests": affected,
    }


def _git_changed_files(target: Path) -> list[str]:
    """Return changed file paths for overlay (best-effort, read-only)."""
    commands = [
        ["git", "-C", str(target), "diff", "--name-only", "HEAD"],
        ["git", "-C", str(target), "diff", "--name-only", "--cached"],
    ]
    for base_cmd in (
        ["git", "-C", str(target), "merge-base", "HEAD", "origin/main"],
        ["git", "-C", str(target), "merge-base", "HEAD", "main"],
    ):
        base_result = proc.run(base_cmd, timeout=5.0)
        if base_result.code == 0:
            base = (base_result.stdout or "").strip()
            if base:
                commands.append(["git", "-C", str(target), "diff", "--name-only", base, "HEAD"])
                break

    files: list[str] = []
    seen: set[str] = set()
    for argv in commands:
        result = proc.run(argv, timeout=5.0)
        if result.code != 0:
            continue
        for line in (result.stdout or "").splitlines():
            path = line.strip()
            if path and path not in seen:
                seen.add(path)
                files.append(path)
    return sorted(files)


_SVG_CHAR_EM = 0.62


def svg_text_width(text: str, *, font_size: int = 12) -> int:
    """Approximate rendered SVG text width in pixels for layout."""
    return max(0, int(round(len(text) * max(6.0, font_size * _SVG_CHAR_EM))))


def fit_svg_label(text: str, max_width: int, *, font_size: int = 12) -> tuple[str, str]:
    """Return (visible label, full label) with width-aware truncation for SVG."""
    full = text.strip() or "?"
    if svg_text_width(full, font_size=font_size) <= max_width:
        return full, full
    ellipsis = "…"
    if svg_text_width(ellipsis, font_size=font_size) >= max_width:
        return ellipsis, full
    lo, hi = 0, len(full)
    visible = ellipsis
    while lo <= hi:
        mid = (lo + hi) // 2
        candidate = full[:mid] + ellipsis
        if svg_text_width(candidate, font_size=font_size) <= max_width:
            visible = candidate
            lo = mid + 1
        else:
            hi = mid - 1
    return visible, full
