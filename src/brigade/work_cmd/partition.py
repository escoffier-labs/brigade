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

Campaign ready sets (#814 / #999) compose those per-repo waves at query
time: global wave N is each member's local wave N, in campaign order.
Cross-repo path identity is not a conflict. Empty footprints stay exclusive
inside their member repository only.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from .. import component_bins
from . import footprint as footprint_mod

MODE_FILE_OVERLAP = "file_overlap"
MODE_FILE_OVERLAP_SYMBOL_IMPACT = "file_overlap+symbol_impact"


def partition_campaign_ready(
    ready: Sequence[dict[str, Any]],
    *,
    members: Sequence[dict[str, Any]] | None = None,
    tasks_by_id: dict[str, dict[str, Any]] | None = None,
    impact_runner: Any | None = None,
) -> dict[str, Any]:
    """Compose campaign waves from each member's per-repo footprint partition.

    For every configured member in campaign order: select that member's
    repo-qualified ready tasks, run :func:`partition_ready` against the
    member target and code graph, then place local wave N into global
    wave N. File and symbol-impact safety stay intra-repo. Matching
    relative paths in separate repositories do not conflict.

    Per-member GraphTrail degradation is reported without discarding the
    file-overlap waves that member still produced. ``tasks_by_id`` should
    be the qualified synthetic ledger map; when omitted, footprints look
    up empty and those tasks become exclusive inside their member.
    """
    lookup = tasks_by_id if tasks_by_id is not None else {}
    member_list = [member for member in (members or []) if isinstance(member, dict)]
    ready_by_member: dict[str, list[dict[str, Any]]] = {}
    for item in ready:
        member_id = _ready_member_id(item)
        if not member_id:
            continue
        ready_by_member.setdefault(member_id, []).append(item)

    composed_tasks: list[list[dict[str, Any]]] = []
    composed_exclusive: list[bool] = []
    member_partitions: list[dict[str, Any]] = []
    modes: list[str] = []
    degraded = False
    degrade_reasons: list[str] = []

    for member in member_list:
        member_id = str(member.get("id") or "").strip()
        if not member_id:
            continue
        member_ready = ready_by_member.get(member_id) or []
        if not member_ready:
            continue
        target = _member_partition_target(member)
        local = partition_ready(
            member_ready,
            lookup,
            target,
            impact_runner=impact_runner,
        )
        member_report: dict[str, Any] = {
            "member_id": member_id,
            "partition_mode": local.get("partition_mode"),
            "partition_degraded": bool(local.get("partition_degraded")),
            "wave_count": int(local.get("wave_count") or 0),
        }
        if local.get("partition_degraded_reason"):
            member_report["partition_degraded_reason"] = local["partition_degraded_reason"]
        member_partitions.append(member_report)
        mode = str(local.get("partition_mode") or MODE_FILE_OVERLAP)
        modes.append(mode)
        if local.get("partition_degraded"):
            degraded = True
            reason = local.get("partition_degraded_reason")
            if isinstance(reason, str) and reason.strip():
                degrade_reasons.append(f"{member_id}: {reason.strip()}")
        for index, wave in enumerate(local.get("waves") or []):
            if not isinstance(wave, dict):
                continue
            tasks = [task for task in (wave.get("tasks") or []) if isinstance(task, dict)]
            if not tasks:
                continue
            while len(composed_tasks) <= index:
                composed_tasks.append([])
                composed_exclusive.append(False)
            local_exclusive = bool(wave.get("exclusive"))
            if not composed_tasks[index] and local_exclusive and len(tasks) == 1:
                composed_exclusive[index] = True
            composed_tasks[index].extend(tasks)
            if len(composed_tasks[index]) > 1:
                composed_exclusive[index] = False

    wave_payloads = [
        {
            "index": index,
            "task_ids": [str(item.get("id") or "") for item in wave],
            "tasks": list(wave),
            "exclusive": composed_exclusive[index],
        }
        for index, wave in enumerate(composed_tasks)
    ]
    if MODE_FILE_OVERLAP_SYMBOL_IMPACT in modes and MODE_FILE_OVERLAP not in modes:
        partition_mode = MODE_FILE_OVERLAP_SYMBOL_IMPACT
    else:
        partition_mode = MODE_FILE_OVERLAP
    payload: dict[str, Any] = {
        "waves": wave_payloads,
        "wave_count": len(wave_payloads),
        "partition_mode": partition_mode,
        "partition_degraded": degraded,
        "member_partitions": member_partitions,
    }
    if degraded and degrade_reasons:
        payload["partition_degraded_reason"] = "; ".join(degrade_reasons)
    elif degraded:
        payload["partition_degraded_reason"] = "graphtrail unavailable"
    return payload


def _ready_member_id(item: dict[str, Any]) -> str:
    repo = item.get("repo")
    if isinstance(repo, str) and repo.strip():
        return repo.strip()
    task_id = str(item.get("id") or "")
    if ":" not in task_id:
        return ""
    member_id, local_id = task_id.split(":", 1)
    if not member_id.strip() or not local_id.strip():
        return ""
    return member_id.strip()


def _member_partition_target(member: dict[str, Any]) -> Path:
    raw = member.get("target") or member.get("path") or "."
    return Path(str(raw)).expanduser()


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
    impact_cache: dict[str, dict[str, Any] | None] = {}
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
            impact_cache=impact_cache,
        )
        effective[task_id] = files
        exclusive[task_id] = len(files) == 0

    waves: list[list[dict[str, Any]]] = []
    wave_task_ids: list[list[str]] = []
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
            overlaps = False
            for existing_id in wave_task_ids[index]:
                if footprints_overlap(
                    None,
                    None,
                    left_files=files,
                    right_files=effective.get(existing_id, frozenset()),
                ):
                    overlaps = True
                    break
            if overlaps:
                continue
            wave.append(item)
            wave_task_ids[index].append(task_id)
            placed = True
            break
        if not placed:
            waves.append([item])
            wave_task_ids.append([task_id])
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
    impact_cache: dict[str, dict[str, Any] | None] | None = None,
) -> frozenset[str]:
    if not isinstance(footprint, dict):
        return frozenset()
    normalized = footprint_mod.normalize_footprint(footprint)
    files = set(normalized["files"])
    if not use_symbol_impact or binary is None or db_path is None:
        return frozenset(files)
    cache = impact_cache if impact_cache is not None else {}
    for symbol in normalized["symbol_ids"]:
        if symbol in cache:
            payload = cache[symbol]
        else:
            payload = impact_runner(target, binary, db_path, symbol)
            cache[symbol] = payload
        if not isinstance(payload, dict):
            continue
        files.update(footprint_mod._files_from_impact_payload(payload))
    return frozenset(files)
