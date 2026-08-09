"""Three-phase task footprint metadata (files, symbol_ids, snapshot_hash).

Schema follows the #770 spike's validated shape. The same object is rewritten
with increasing accuracy:

1. ``predicted`` at filing — GraphTrail ``impact`` of named symbols when the
   binary and index are present; otherwise an empty predicted footprint
   (fail-open degrade).
2. ``refined`` at claim — plan-named concrete files.
3. ``reconciled`` at completion — join of the verify receipt's
   ``code_graph_delta`` / ``graphtrail_delta`` field (what was actually
   touched).

GraphTrail stays a read-only oracle; footprints live only on the task ledger.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable, Sequence

from .. import component_bins, proc

PHASE_PREDICTED = "predicted"
PHASE_REFINED = "refined"
PHASE_RECONCILED = "reconciled"
FOOTPRINT_PHASES = (PHASE_PREDICTED, PHASE_REFINED, PHASE_RECONCILED)
# Bound hostile / hand-edited footprint lists so a single task cannot balloon
# the ledger. Spike-validated workloads stay well under this.
MAX_FOOTPRINT_ENTRIES = 512

_PATH_TOKEN = re.compile(r"(?P<path>(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+\.[A-Za-z0-9]+|[A-Za-z0-9_.-]+\.[A-Za-z0-9]+)")


def is_safe_footprint_path(value: str) -> bool:
    """Return True when ``value`` is a repo-relative path without traversal."""
    text = value.strip()
    if not text:
        return False
    if text.startswith("/") or text.startswith("\\"):
        return False
    # Windows drive / UNC
    if len(text) >= 2 and text[1] == ":":
        return False
    if text.startswith("\\\\") or text.startswith("//"):
        return False
    normalized = text.replace("\\", "/")
    if normalized.startswith("./"):
        normalized = normalized[2:]
    parts = [part for part in normalized.split("/") if part not in ("", ".")]
    if not parts:
        return False
    if any(part == ".." for part in parts):
        return False
    return True


def normalize_footprint(raw: object, *, phase: str | None = None) -> dict[str, Any]:
    """Return a footprint object with the spike-validated core fields."""
    data = raw if isinstance(raw, dict) else {}
    files = [path for path in _unique_strings(data.get("files")) if is_safe_footprint_path(path)]
    symbol_ids = _unique_strings(data.get("symbol_ids") if "symbol_ids" in data else data.get("symbols"))
    snapshot_hash = data.get("snapshot_hash")
    if snapshot_hash is None:
        snapshot_hash = ""
    else:
        snapshot_hash = str(snapshot_hash)
    resolved_phase = phase or data.get("phase")
    if resolved_phase not in FOOTPRINT_PHASES:
        resolved_phase = PHASE_PREDICTED
    return {
        "files": files,
        "symbol_ids": symbol_ids,
        "snapshot_hash": snapshot_hash,
        "phase": resolved_phase,
    }


def empty_predicted(*, reason: str = "graphtrail unavailable") -> dict[str, Any]:
    """Empty predicted footprint used when GraphTrail cannot enrich the write."""
    footprint = normalize_footprint({}, phase=PHASE_PREDICTED)
    footprint["degraded"] = True
    footprint["degraded_reason"] = reason
    return footprint


def task_footprint(task: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(task, dict):
        return None
    metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else None
    if metadata is None:
        return None
    raw = metadata.get("footprint")
    if raw is None:
        return None
    return normalize_footprint(raw)


def set_task_footprint(task: dict[str, Any], footprint: dict[str, Any]) -> dict[str, Any]:
    """Write ``metadata.footprint`` onto a task record (mutates in place)."""
    raw_metadata = task.get("metadata")
    metadata: dict[str, Any] = dict(raw_metadata) if isinstance(raw_metadata, dict) else {}
    normalized = normalize_footprint(footprint, phase=str(footprint.get("phase") or PHASE_PREDICTED))
    # Preserve degrade markers only on predicted empty writes.
    if footprint.get("degraded") is True and normalized["phase"] == PHASE_PREDICTED:
        normalized["degraded"] = True
        reason = footprint.get("degraded_reason")
        if isinstance(reason, str) and reason.strip():
            normalized["degraded_reason"] = reason.strip()
    metadata["footprint"] = normalized
    task["metadata"] = metadata
    return normalized


def symbols_from_metadata(metadata: dict[str, Any] | None) -> list[str]:
    if not isinstance(metadata, dict):
        return []
    if "symbol_ids" in metadata:
        return _unique_strings(metadata.get("symbol_ids"))
    return _unique_strings(metadata.get("symbols"))


def files_from_metadata(metadata: dict[str, Any] | None) -> list[str]:
    if not isinstance(metadata, dict):
        return []
    return [path for path in _unique_strings(metadata.get("files")) if is_safe_footprint_path(path)]


def predict_footprint(
    target: Path,
    *,
    symbols: Sequence[str] | None = None,
    files: Sequence[str] | None = None,
    impact_runner: Any | None = None,
) -> dict[str, Any]:
    """Predicted footprint at filing time.

    Calls GraphTrail ``impact`` when the binary and index exist. Degrades to an
    empty predicted footprint otherwise (never raises).
    """
    target = target.expanduser().resolve()
    seed_symbols = _unique_strings(symbols)
    seed_files = _unique_strings(files)
    binary = component_bins.resolve("graphtrail")
    db_path = target / ".graphtrail" / "graphtrail.db"
    if binary is None:
        return empty_predicted(reason="graphtrail binary not found")
    if not db_path.is_file():
        return empty_predicted(reason="graphtrail index not found")
    if not seed_symbols and not seed_files:
        # Index is present but filing named nothing to impact — still predicted,
        # empty, and not degraded (the oracle was available).
        return normalize_footprint(
            {"files": [], "symbol_ids": [], "snapshot_hash": _snapshot_hash(db_path)},
            phase=PHASE_PREDICTED,
        )

    runner = impact_runner or _run_graphtrail_impact
    collected_files: list[str] = list(seed_files)
    collected_symbols: list[str] = list(seed_symbols)
    queries = seed_symbols or seed_files
    for query in queries:
        payload = runner(target, binary, db_path, query)
        if not isinstance(payload, dict):
            continue
        collected_files.extend(_files_from_impact_payload(payload))
        collected_symbols.extend(_symbols_from_impact_payload(payload))
    return normalize_footprint(
        {
            "files": collected_files,
            "symbol_ids": collected_symbols,
            "snapshot_hash": _snapshot_hash(db_path),
        },
        phase=PHASE_PREDICTED,
    )


def refine_footprint(
    *,
    files: Sequence[str] | None = None,
    prior: dict[str, Any] | None = None,
    symbol_ids: Sequence[str] | None = None,
    snapshot_hash: str | None = None,
) -> dict[str, Any]:
    """Refined footprint at claim time from plan-named files."""
    prior_norm = normalize_footprint(prior) if prior is not None else None
    refined_files = _unique_strings(files)
    if not refined_files and prior_norm is not None:
        refined_files = list(prior_norm["files"])
    refined_symbols = _unique_strings(symbol_ids)
    if not refined_symbols and prior_norm is not None:
        refined_symbols = list(prior_norm["symbol_ids"])
    hash_value = snapshot_hash
    if hash_value is None and prior_norm is not None:
        hash_value = prior_norm["snapshot_hash"]
    if hash_value is None:
        hash_value = ""
    return normalize_footprint(
        {
            "files": refined_files,
            "symbol_ids": refined_symbols,
            "snapshot_hash": hash_value,
        },
        phase=PHASE_REFINED,
    )


def reconcile_footprint(
    delta: dict[str, Any] | None,
    *,
    prior: dict[str, Any] | None = None,
    target: Path | None = None,
) -> dict[str, Any]:
    """Reconciled footprint at completion by joining a verify graph delta."""
    prior_norm = normalize_footprint(prior) if prior is not None else None
    payload = _delta_payload(delta, target=target)
    files = _files_from_delta(payload)
    symbols = _symbols_from_delta(payload)
    snapshot_hash = _snapshot_hash_from_delta(payload)
    if not files and prior_norm is not None:
        files = list(prior_norm["files"])
    if not symbols and prior_norm is not None:
        symbols = list(prior_norm["symbol_ids"])
    if not snapshot_hash and prior_norm is not None:
        snapshot_hash = prior_norm["snapshot_hash"]
    return normalize_footprint(
        {
            "files": files,
            "symbol_ids": symbols,
            "snapshot_hash": snapshot_hash or "",
        },
        phase=PHASE_RECONCILED,
    )


def extract_plan_files(plan: dict[str, Any] | None) -> list[str]:
    """Pull concrete file paths named in a plan artifact receipt."""
    if not isinstance(plan, dict):
        return []
    candidates: list[str] = []
    for key in ("files", "paths", "touch", "touched_files"):
        candidates.extend(_unique_strings(plan.get(key)))
    for key in ("steps", "source_context", "assumptions", "risks"):
        values = plan.get(key)
        if not isinstance(values, list):
            continue
        for item in values:
            candidates.extend(_path_tokens(str(item)))
    return _unique_strings(candidates)


def receipt_graph_delta(receipt: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return the verify receipt's graphtrail delta field (canonical or alias)."""
    if not isinstance(receipt, dict):
        return None
    for key in ("code_graph_delta", "graphtrail_delta"):
        value = receipt.get(key)
        if isinstance(value, dict):
            return value
    return None


def attach_predicted(target: Path, task: dict[str, Any], *, impact_runner: Any | None = None) -> dict[str, Any]:
    """Predict-and-attach footprint onto a newly filed task."""
    metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
    footprint = predict_footprint(
        target,
        symbols=symbols_from_metadata(metadata),
        files=files_from_metadata(metadata),
        impact_runner=impact_runner,
    )
    return set_task_footprint(task, footprint)


def _run_graphtrail_impact(target: Path, binary: str, db_path: Path, query: str) -> dict[str, Any] | None:
    result = proc.run(
        [binary, "--db", str(db_path), "impact", query, "--json"],
        timeout=10.0,
        cwd=target,
    )
    if result.code != 0:
        # Text-mode fallback: some GraphTrail builds omit --json for impact.
        text_result = proc.run(
            [binary, "--db", str(db_path), "impact", query],
            timeout=10.0,
            cwd=target,
        )
        if text_result.code != 0:
            return None
        return {"related_files": _path_tokens(text_result.stdout), "symbol_ids": [query]}
    data = result.json()
    if isinstance(data, dict):
        return data
    if isinstance(data, list):
        return {"items": data}
    return None


def _snapshot_hash(db_path: Path) -> str:
    try:
        digest = hashlib.sha256()
        with db_path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return ""


def _unique_strings(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        values: Iterable[object] = [value]
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        values = value
    else:
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in values:
        text = str(item).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
        if len(result) >= MAX_FOOTPRINT_ENTRIES:
            break
    return result


def _path_tokens(text: str) -> list[str]:
    return [match.group("path") for match in _PATH_TOKEN.finditer(text) if is_safe_footprint_path(match.group("path"))]


def _files_from_impact_payload(payload: dict[str, Any]) -> list[str]:
    files: list[str] = []
    for key in ("files", "related_files", "affected_files", "paths"):
        files.extend(_unique_strings(payload.get(key)))
    for key in ("entry_points", "nodes", "items", "results", "impact"):
        values = payload.get(key)
        if not isinstance(values, list):
            continue
        for item in values:
            if not isinstance(item, dict):
                continue
            for file_key in ("file_path", "path", "file"):
                file_value = item.get(file_key)
                if isinstance(file_value, str) and file_value.strip():
                    files.append(file_value.strip())
    return [path for path in _unique_strings(files) if is_safe_footprint_path(path)]


def _symbols_from_impact_payload(payload: dict[str, Any]) -> list[str]:
    symbols: list[str] = []
    for key in ("symbol_ids", "symbols", "changed_symbols", "impacted_symbols"):
        symbols.extend(_unique_strings(payload.get(key)))
    for key in ("entry_points", "nodes", "items", "results", "impact"):
        values = payload.get(key)
        if not isinstance(values, list):
            continue
        for item in values:
            if not isinstance(item, dict):
                continue
            for symbol_key in ("qualified_name", "symbol", "name", "id"):
                symbol_value = item.get(symbol_key)
                if isinstance(symbol_value, str) and symbol_value.strip():
                    symbols.append(symbol_value.strip())
                    break
    return _unique_strings(symbols)


def _delta_payload(delta: dict[str, Any] | None, *, target: Path | None = None) -> dict[str, Any]:
    if not isinstance(delta, dict):
        return {}
    # Prefer an on-disk sidecar when present so join sees the full payload.
    # Sidecar reads are confined to ``target`` when provided so a hostile
    # receipt cannot exfiltrate arbitrary host files into the footprint.
    sidecar = delta.get("sidecar_path")
    if isinstance(sidecar, str) and sidecar.strip():
        path = Path(sidecar.strip())
        if target is not None:
            try:
                resolved = path.expanduser().resolve(strict=False)
                root = target.expanduser().resolve(strict=False)
            except OSError:
                return delta
            try:
                resolved.relative_to(root)
            except ValueError:
                return delta
        if path.is_file():
            try:
                loaded = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                loaded = None
            if isinstance(loaded, dict):
                merged = dict(loaded)
                merged.update({key: value for key, value in delta.items() if key not in merged})
                return merged
    return delta


def _files_from_delta(payload: dict[str, Any]) -> list[str]:
    files: list[str] = []
    for key in ("files", "changed_files", "related_files"):
        files.extend(_unique_strings(payload.get(key)))
    nodes = payload.get("code_reference_nodes")
    if isinstance(nodes, list):
        for node in nodes:
            if not isinstance(node, dict):
                continue
            path = node.get("file_path") or node.get("path")
            if isinstance(path, str) and path.strip():
                files.append(path.strip())
    for key in ("added_nodes", "removed_nodes", "changed_nodes"):
        values = payload.get(key)
        if not isinstance(values, list):
            continue
        for node in values:
            if not isinstance(node, dict):
                continue
            path = node.get("file_path") or node.get("path")
            if isinstance(path, str) and path.strip():
                files.append(path.strip())
    return [path for path in _unique_strings(files) if is_safe_footprint_path(path)]


def _symbols_from_delta(payload: dict[str, Any]) -> list[str]:
    symbols = _unique_strings(payload.get("changed_symbols"))
    symbols.extend(_unique_strings(payload.get("symbol_ids")))
    nodes = payload.get("code_reference_nodes")
    if isinstance(nodes, list):
        for node in nodes:
            if not isinstance(node, dict):
                continue
            name = node.get("qualified_name") or node.get("symbol") or node.get("name")
            if isinstance(name, str) and name.strip():
                symbols.append(name.strip())
    return _unique_strings(symbols)


def _snapshot_hash_from_delta(payload: dict[str, Any]) -> str:
    for key in ("snapshot_hash", "after_snapshot_sha256", "before_snapshot_sha256", "sidecar_sha256"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    attestations = payload.get("attestations")
    if isinstance(attestations, dict):
        for key in ("after_snapshot_sha256", "before_snapshot_sha256", "diff_stdout_sha256"):
            value = attestations.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""
