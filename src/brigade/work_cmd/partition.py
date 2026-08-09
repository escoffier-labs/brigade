"""Query-time parallel-safe partition of the ready set into dispatch waves.

Wraps the native ready set (#737 / #783). Pairwise footprint intersection
(exact file overlap first; one-hop GraphTrail symbol impact as refinement)
greedy-partitions ready tasks into waves of mutually non-overlapping work.

Same wave = parallel-safe concurrent lanes; cross-wave = serialize. Computed
at query time and never stored, so never stale.

When GraphTrail is unavailable the partition degrades cleanly to
file-overlap-only. Empty effective footprints are conservative: their wave
is exclusive (they conflict with every other ready task, including other
empty-footprint tasks).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from .. import component_bins
from . import footprint as footprint_mod

MODE_FILE_OVERLAP = "file_overlap"
MODE_FILE_OVERLAP_SYMBOL_IMPACT = "file_overlap+symbol_impact"


def partition_ready(
    ready: Sequence[dict[str, Any]],
    tasks_by_id: dict[str, dict[str, Any]],
    target: Path,
    *,
    impact_runner: Any | None = None,
) -> dict[str, Any]:
    """Greedy-partition ``ready`` into dispatch waves by footprint intersection.

    Returns a payload suitable for additive merge onto the readiness JSON:

    ``waves``, ``wave_count``, ``partition_mode``, ``partition_degraded``,
    and optional ``partition_degraded_reason``.
    """
    target = target.expanduser().resolve()
    available, degrade_reason, binary, db_path = _graphtrail_availability(target)
    use_symbol_impact = available
    mode = MODE_FILE_OVERLAP_SYMBOL_IMPACT if use_symbol_impact else MODE_FILE_OVERLAP

    runner = impact_runner or footprint_mod._run_graphtrail_impact
    effective: dict[str, frozenset[str]] = {}
    exclusive: dict[str, bool] = {}
    for item in ready:
        task_id = str(item.get("id") or "")
        task = tasks_by_id.get(task_id)
        footprint = footprint_mod.task_footprint(task) if task is not None else None
        files = _effective_files(
            footprint,
            target=target,
            binary=binary,
            db_path=db_path,
            use_symbol_impact=use_symbol_impact,
            impact_runner=runner,
        )
        effective[task_id] = files
        exclusive[task_id] = len(files) == 0

    waves: list[list[dict[str, Any]]] = []
    wave_file_sets: list[set[str]] = []
    wave_has_exclusive: list[bool] = []

    for item in ready:
        task_id = str(item.get("id") or "")
        files = effective.get(task_id, frozenset())
        is_exclusive = exclusive.get(task_id, True)
        placed = False
        for index, wave in enumerate(waves):
            if wave_has_exclusive[index]:
                continue
            if is_exclusive:
                continue
            if files & wave_file_sets[index]:
                continue
            wave.append(item)
            wave_file_sets[index].update(files)
            placed = True
            break
        if not placed:
            waves.append([item])
            wave_file_sets.append(set(files))
            wave_has_exclusive.append(is_exclusive)

    wave_payloads = [
        {
            "index": index,
            "task_ids": [str(item.get("id") or "") for item in wave],
            "tasks": list(wave),
            "exclusive": wave_has_exclusive[index],
        }
        for index, wave in enumerate(waves)
    ]
    payload: dict[str, Any] = {
        "waves": wave_payloads,
        "wave_count": len(wave_payloads),
        "partition_mode": mode,
        "partition_degraded": not use_symbol_impact,
    }
    if not use_symbol_impact and degrade_reason:
        payload["partition_degraded_reason"] = degrade_reason
    return payload


def footprints_overlap(
    left: dict[str, Any] | None,
    right: dict[str, Any] | None,
    *,
    left_files: frozenset[str] | None = None,
    right_files: frozenset[str] | None = None,
) -> bool:
    """Return True when two footprints cannot share a parallel-safe wave.

    Empty effective file sets are exclusive (overlap with everything).
    """
    files_left = left_files if left_files is not None else _files_only(left)
    files_right = right_files if right_files is not None else _files_only(right)
    if not files_left or not files_right:
        return True
    return bool(files_left & files_right)


def _files_only(footprint: dict[str, Any] | None) -> frozenset[str]:
    if not isinstance(footprint, dict):
        return frozenset()
    normalized = footprint_mod.normalize_footprint(footprint)
    return frozenset(normalized["files"])


def _graphtrail_availability(target: Path) -> tuple[bool, str | None, str | None, Path | None]:
    binary = component_bins.resolve("graphtrail")
    db_path = target / ".graphtrail" / "graphtrail.db"
    if binary is None:
        return False, "graphtrail binary not found", None, None
    if not db_path.is_file():
        return False, "graphtrail index not found", None, None
    return True, None, binary, db_path


def _effective_files(
    footprint: dict[str, Any] | None,
    *,
    target: Path,
    binary: str | None,
    db_path: Path | None,
    use_symbol_impact: bool,
    impact_runner: Any,
) -> frozenset[str]:
    if not isinstance(footprint, dict):
        return frozenset()
    normalized = footprint_mod.normalize_footprint(footprint)
    files = set(normalized["files"])
    if not use_symbol_impact or binary is None or db_path is None:
        return frozenset(files)
    for symbol in normalized["symbol_ids"]:
        payload = impact_runner(target, binary, db_path, symbol)
        if not isinstance(payload, dict):
            continue
        files.update(footprint_mod._files_from_impact_payload(payload))
    return frozenset(files)
