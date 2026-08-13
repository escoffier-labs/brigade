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

MODULE_CAP = 48
EDGE_CAP = 96
_SEARCH_LIMIT = 12
_GRAPH_TIMEOUT = 30.0


def export_payload(
    target: Path,
    *,
    symbol: str | None = None,
    overlay: bool = False,
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

    if overlay:
        payload["change_overlay"] = {
            "changed_files": changed_files,
            "changed_modules": sorted({_module_key(path) for path in changed_files if _module_key(path)}),
        }

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

    payload = export_payload(target, symbol=symbol, overlay=overlay)
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


def _module_key(file_path: str) -> str:
    parts = PurePosixPath(file_path.replace("\\", "/")).parts
    if not parts:
        return "(root)"
    if parts[0] == "src" and len(parts) >= 4:
        return str(PurePosixPath(*parts[:3]))
    if parts[0] == "src" and len(parts) == 3:
        return str(PurePosixPath(*parts[:2]))
    if parts[0] == "tests":
        return "tests" if len(parts) <= 2 else str(PurePosixPath(*parts[:2]))
    if parts[0] == "engines" and len(parts) >= 4:
        return str(PurePosixPath(*parts[:3]))
    if parts[0] == "engines" and len(parts) == 3:
        return str(PurePosixPath(*parts[:2]))
    if len(parts) >= 2:
        return str(PurePosixPath(*parts[:-1]))
    return parts[0]


def _module_label(module_id: str) -> str:
    name = PurePosixPath(module_id).name
    return name or module_id


def _build_module_map(file_graph: dict[str, Any], *, changed_files: list[str]) -> dict[str, Any]:
    raw_nodes = file_graph.get("nodes")
    nodes: dict[str, int] = raw_nodes if isinstance(raw_nodes, dict) else {}
    raw_edge_list = file_graph.get("edges")
    raw_edges: list[tuple[str, str, int]] = raw_edge_list if isinstance(raw_edge_list, list) else []

    module_symbols: dict[str, int] = defaultdict(int)
    module_files: dict[str, int] = defaultdict(int)
    changed_modules = {_module_key(path) for path in changed_files}

    for file_path, symbol_count in nodes.items():
        module_id = _module_key(file_path)
        module_symbols[module_id] += symbol_count
        module_files[module_id] += 1

    module_edges: dict[tuple[str, str], int] = defaultdict(int)
    for source, target_file, weight in raw_edges:
        source_mod = _module_key(source)
        target_mod = _module_key(target_file)
        if source_mod == target_mod:
            continue
        module_edges[(source_mod, target_mod)] += weight

    modules_sorted = sorted(
        module_symbols.items(),
        key=lambda item: (-item[1], item[0]),
    )
    total_modules = len(modules_sorted)
    shown_modules = modules_sorted[:MODULE_CAP]
    hidden_modules = max(0, total_modules - len(shown_modules))
    shown_ids = {module_id for module_id, _ in shown_modules}

    modules = [
        {
            "id": module_id,
            "label": _module_label(module_id),
            "symbol_count": count,
            "file_count": module_files.get(module_id, 0),
            **({"changed": True} if module_id in changed_modules else {}),
        }
        for module_id, count in shown_modules
    ]

    edges_sorted = sorted(module_edges.items(), key=lambda item: (-item[1], item[0][0], item[0][1]))
    filtered_edges = [(pair, weight) for pair, weight in edges_sorted if pair[0] in shown_ids and pair[1] in shown_ids]
    total_edges = len(filtered_edges)
    shown_edge_rows = filtered_edges[:EDGE_CAP]
    hidden_edges = max(0, total_edges - len(shown_edge_rows))

    edges = [{"from": source, "to": target, "weight": weight} for (source, target), weight in shown_edge_rows]

    trunc = _truncation_note(len(modules), hidden_modules, len(edges), hidden_edges)
    return {"modules": modules, "edges": edges, "truncation": trunc}


def _truncation_note(
    shown_modules: int,
    hidden_modules: int,
    shown_edges: int,
    hidden_edges: int,
) -> dict[str, Any]:
    note_parts: list[str] = []
    if hidden_modules:
        note_parts.append(f"showing top {shown_modules} modules, {hidden_modules} hidden")
    if hidden_edges:
        note_parts.append(f"showing top {shown_edges} edges, {hidden_edges} hidden")
    note = "; ".join(note_parts) if note_parts else ""
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


def fit_svg_label(text: str, max_width: int, *, font_size: int = 12) -> tuple[str, str]:
    """Return (visible label, full label) with width-aware truncation for SVG."""
    full = text.strip() or "?"
    approx_char = max(1, int(max_width / max(6.0, font_size * 0.62)))
    if len(full) <= approx_char:
        return full, full
    if approx_char <= 1:
        return "…", full
    return full[: approx_char - 1] + "…", full
