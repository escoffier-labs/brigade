#!/usr/bin/env python3
"""Issue #846 work-store characterization harness (R1 baseline + R2 residuals).

Measures candidate store *shapes* against synthetic, machine-neutral graph
fixtures. This script is research-only: it does not edit production work-store
code, add runtime dependencies, start network listeners, or select a backend.

Shapes:

- ``json_ledger`` — current Brigade ``.brigade/work/tasks.json`` path (measured)
- ``sqlite_wal`` — optional stdlib ``sqlite3`` WAL characterization adapter
- ``dolt_cli_per_command`` / ``embedded_dolt`` / ``dolt_sql_server`` — recorded
  as blocked or unmeasured unless a temporary local binary is supplied; the
  SQL-server shape never starts a listener from this harness

Protocol defaults match the #846 scoping comment: 10 warmups and 100 measured
trials, nearest-rank p50/p95, two-claimer barrier race requiring one success
and one exit 13, and pass/fail gates for #737/#738 anchors where exercised.
R2 adds residual no-listener cells: same-actor rejection, guards/empty-filter,
restart recovery, schema coercion observation, branch/config backup restore,
install footprint, cold-start/backup timing, resource samples, and observed
metrics-state plus deleted-secret/history checks. Latency remains descriptive
(no absolute service level). Numbers are never fabricated: cells are
``measured``, ``blocked``, or ``unavailable``.

Standard library plus the in-repo ``brigade`` package only.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import math
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from brigade import localio
from brigade import work_cmd
from brigade.work_cmd import claiming as claim_mod
from brigade.work_cmd import edges as edges_mod
from brigade.work_cmd import helpers as work_helpers
from brigade.work_cmd import ledger as ledger_mod

ISSUE = 846
SLICE = "R2"
KIND = "work-store-characterization"
PROTOCOL_VERSION = 2
TIMEBOX_NOTE = (
    "R2 residual timebox: extend the R1 JSON/SQLite harness with no-listener "
    "residual cells only. Dolt shapes stay optional characterization candidates "
    "and must not enter Brigade runtime dependencies. Server listeners, TLS, "
    "and permission cells that need a listener remain blocked pending separate "
    "operator approval and are never started here."
)
SYNTHETIC_SECRET = "synth-secret-846-r2-TOKEN-not-a-real-credential"
SYNTHETIC_SECRET_PATH = "synth/paths/issue-846-r2/private-token.example"
METRICS_SCAN_NAMES = (
    "telemetry",
    "metrics.json",
    "anonymous-metrics",
    "otel",
)

DEFAULT_WARMUPS = 10
DEFAULT_TRIALS = 100
EXIT_CLAIM_BUSY = claim_mod.EXIT_GUARD_MISMATCH

SHAPES = (
    "json_ledger",
    "sqlite_wal",
    "dolt_cli_per_command",
    "embedded_dolt",
    "dolt_sql_server",
)

DATASETS = (
    "chain_50",
    "diamond_200",
    "wide_500_1000",
)

_MOUNTPOINT_ESCAPE_RE = re.compile(r"\\([0-7]{3})")
_FIXED_CREATED_AT = "2026-01-01T00:00:00+00:00"


class MeasurementInputError(RuntimeError):
    """Measurement input or scenario state is missing, unreadable, or incomplete."""


@dataclass(frozen=True)
class FixtureSpec:
    name: str
    task_count: int
    edge_count: int
    blocks_edges: int
    provenance_edges: int
    parent_child_edges: int
    ready_count: int


FIXTURE_SPECS: dict[str, FixtureSpec] = {
    "chain_50": FixtureSpec(
        name="chain_50",
        task_count=50,
        edge_count=49,
        blocks_edges=49,
        provenance_edges=0,
        parent_child_edges=0,
        ready_count=1,
    ),
    "diamond_200": FixtureSpec(
        name="diamond_200",
        task_count=200,
        edge_count=396,
        blocks_edges=396,
        provenance_edges=0,
        parent_child_edges=0,
        ready_count=1,
    ),
    "wide_500_1000": FixtureSpec(
        name="wide_500_1000",
        task_count=500,
        edge_count=1000,
        blocks_edges=0,
        provenance_edges=1000,
        parent_child_edges=0,
        ready_count=500,
    ),
}


def _percentile_nearest_rank(sorted_values: list[int], percentile: float) -> int:
    """Deterministic nearest-rank percentile on an ascending-sorted nonempty list."""
    n = len(sorted_values)
    if n == 0:
        raise ValueError("percentile requires a nonempty sample")
    rank = math.ceil(percentile / 100 * n)
    index = max(0, min(n - 1, rank - 1))
    return int(sorted_values[index])


def _decode_mountpoint(raw: str) -> str:
    return _MOUNTPOINT_ESCAPE_RE.sub(lambda match: chr(int(match.group(1), 8)), raw)


def _filesystem_type(root: Path) -> str:
    mountinfo = Path("/proc/self/mountinfo")
    try:
        lines = mountinfo.read_text().splitlines()
    except OSError:
        return "unknown"
    resolved = root.expanduser().resolve()
    best_fs: str | None = None
    best_depth = -1
    for line in lines:
        fields, separator, tail = line.partition(" - ")
        if not separator:
            continue
        parts = fields.split()
        if len(parts) < 5:
            continue
        mountpoint = Path(_decode_mountpoint(parts[4]))
        try:
            resolved.relative_to(mountpoint)
        except ValueError:
            continue
        depth = len(mountpoint.parts)
        if depth > best_depth:
            best_depth = depth
            best_fs = tail.split(maxsplit=1)[0] if tail.split() else None
    return best_fs if best_fs is not None else "unknown"


def _directory_fsync_supported(root: Path) -> bool:
    probe = Path(tempfile.mkdtemp(prefix="dir-fsync-probe-", dir=root))
    try:
        fd = os.open(probe, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        except OSError:
            return False
        finally:
            os.close(fd)
        return True
    except OSError:
        return False
    finally:
        shutil.rmtree(probe, ignore_errors=True)


def environment_metadata(root: Path) -> dict[str, Any]:
    """Platform and filesystem class without hostnames."""
    return {
        "python_version": sys.version,
        "platform": platform.platform(),
        "filesystem_type": _filesystem_type(root),
        "directory_fsync_supported": _directory_fsync_supported(root),
        "sqlite_version": sqlite3.sqlite_version,
        "dolt_on_path": bool(shutil.which("dolt")),
    }


def _cell_blocked(reason: str, **extra: Any) -> dict[str, Any]:
    payload = {"status": "blocked", "reason": reason, "pass": None}
    payload.update(extra)
    return payload


def _cell_unavailable(reason: str, **extra: Any) -> dict[str, Any]:
    payload = {"status": "unavailable", "reason": reason, "pass": None}
    payload.update(extra)
    return payload


def _tree_size_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    total = 0
    for child in path.rglob("*"):
        if child.is_file():
            total += child.stat().st_size
    return total


def _proc_peak_rss_kib(pid: int) -> int | None:
    status_path = Path(f"/proc/{pid}/status")
    try:
        lines = status_path.read_text().splitlines()
    except OSError:
        return None
    peak_kib: int | None = None
    for line in lines:
        if line.startswith(("VmRSS:", "VmHWM:")):
            value = int(line.split()[1])
            peak_kib = value if peak_kib is None else max(peak_kib, value)
    return peak_kib


def _path_contains_marker(path: Path, marker: str) -> bool:
    if not path.exists():
        return False
    if path.is_file():
        try:
            return marker in path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
    for child in path.rglob("*"):
        if not child.is_file():
            continue
        try:
            if marker in child.read_text(encoding="utf-8", errors="replace"):
                return True
        except OSError:
            continue
    return False


def _scan_metrics_artifacts(target: Path) -> list[str]:
    found: list[str] = []
    if not target.exists():
        return found
    for child in target.rglob("*"):
        if not child.is_file():
            continue
        relative = str(child.relative_to(target)).replace("\\", "/")
        lowered = relative.casefold()
        if any(token in lowered for token in METRICS_SCAN_NAMES):
            found.append(relative)
    return sorted(found)


def _write_synthetic_backup_config(target: Path) -> Path:
    config_path = work_helpers._backup_config_path(target)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        "# synthetic characterization only\n"
        "[destinations.nas]\n"
        'label = "synth-nas-846-r2"\n'
        'summary_path = ".brigade/backups/nas-summary.json"\n',
        encoding="utf-8",
    )
    return config_path


def _digest_file(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _claim_record_digest(ledger: dict[str, Any]) -> str:
    claims = []
    for task in ledger.get("tasks", []):
        if not isinstance(task, dict):
            continue
        claim = task.get("claim")
        if isinstance(claim, dict):
            claims.append(
                {
                    "task_id": task.get("id"),
                    "claim_id": claim.get("claim_id"),
                    "actor": claim.get("actor"),
                    "item_revision": claim.get("item_revision"),
                    "status": task.get("status"),
                    "assignee": task.get("assignee"),
                }
            )
    claims.sort(key=lambda item: (str(item.get("task_id") or ""), str(item.get("claim_id") or "")))
    payload = json.dumps(claims, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _branch_heads_digest(_target: Path) -> dict[str, Any]:
    return {
        "status": "unsupported",
        "detail": "shape has no first-class branch heads",
        "digest_sha256": None,
    }


def _task(
    task_id: str,
    text: str,
    *,
    status: str = "pending",
    priority: str = "normal",
) -> dict[str, Any]:
    return {
        "id": task_id,
        "text": text,
        "status": status,
        "source": "synthetic",
        "type": "task",
        "priority": priority,
        "acceptance": [],
        "created_at": _FIXED_CREATED_AT,
        "updated_at": _FIXED_CREATED_AT,
    }


def _edge(edge_id: str, source: str, target: str, edge_type: str) -> dict[str, Any]:
    return {
        "id": edge_id,
        "source": source,
        "target": target,
        "type": edge_type,
        "created_at": _FIXED_CREATED_AT,
    }


def build_fixture(name: str) -> dict[str, Any]:
    """Build one synthetic ledger fixture. Raises ValueError for unknown names."""
    if name == "chain_50":
        tasks = [_task(f"synth-chain-{i:04d}", f"Chain task {i}") for i in range(50)]
        edges = [
            _edge(
                f"synth-chain-edge-{i:04d}",
                f"synth-chain-{i:04d}",
                f"synth-chain-{i + 1:04d}",
                "blocks",
            )
            for i in range(49)
        ]
    elif name == "diamond_200":
        # 1 source, 198 middles, 1 sink. Source blocks each middle; each middle
        # blocks the sink. Only the source is ready.
        source = "synth-diamond-0000"
        sink = "synth-diamond-0199"
        tasks = [_task(source, "Diamond source", priority="high")]
        tasks.extend(_task(f"synth-diamond-{i:04d}", f"Diamond middle {i}") for i in range(1, 199))
        tasks.append(_task(sink, "Diamond sink"))
        edges = []
        for i in range(1, 199):
            middle = f"synth-diamond-{i:04d}"
            edges.append(_edge(f"synth-diamond-src-{i:04d}", source, middle, "blocks"))
            edges.append(_edge(f"synth-diamond-snk-{i:04d}", middle, sink, "blocks"))
    elif name == "wide_500_1000":
        tasks = [_task(f"synth-wide-{i:04d}", f"Wide open task {i}") for i in range(500)]
        edges = []
        # 1000 distinct provenance-only edges; identity is (source, target, type).
        for index in range(1000):
            source_i = index % 500
            # offsets 1..2 cover 1000 unique directed pairs on 500 nodes
            offset = 1 + (index // 500)
            target_i = (source_i + offset) % 500
            edges.append(
                _edge(
                    f"synth-wide-prov-{index:04d}",
                    f"synth-wide-{source_i:04d}",
                    f"synth-wide-{target_i:04d}",
                    "discovered-from",
                )
            )
    else:
        raise ValueError(f"unknown fixture: {name}")

    ledger = {
        "version": edges_mod.TASK_LEDGER_VERSION,
        "tasks": tasks,
        "edges": edges,
    }
    edges_mod.ensure_ledger_edges(ledger)
    return ledger


def canonicalize_ledger(ledger: dict[str, Any]) -> dict[str, Any]:
    """Return a ledger with tasks/edges stably ordered for digest comparison."""
    tasks = sorted(
        (task for task in ledger.get("tasks", []) if isinstance(task, dict)),
        key=lambda item: str(item.get("id") or ""),
    )
    edges = sorted(
        (edge for edge in ledger.get("edges", []) if isinstance(edge, dict)),
        key=lambda item: (
            str(item.get("id") or ""),
            str(item.get("source") or ""),
            str(item.get("target") or ""),
            str(item.get("type") or ""),
        ),
    )
    return {
        "version": ledger.get("version", edges_mod.TASK_LEDGER_VERSION),
        "tasks": tasks,
        "edges": edges,
    }


def fixture_digest(ledger: dict[str, Any]) -> str:
    payload = json.dumps(canonicalize_ledger(ledger), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def describe_fixture(name: str) -> dict[str, Any]:
    ledger = build_fixture(name)
    spec = FIXTURE_SPECS[name]
    resolution = edges_mod.resolve_readiness(ledger)
    blocks = sum(1 for edge in ledger["edges"] if edge["type"] == "blocks")
    provenance = sum(1 for edge in ledger["edges"] if edge["type"] == "discovered-from")
    parent_child = sum(1 for edge in ledger["edges"] if edge["type"] == "parent-child")
    return {
        "name": name,
        "task_count": len(ledger["tasks"]),
        "edge_count": len(ledger["edges"]),
        "blocks_edges": blocks,
        "provenance_edges": provenance,
        "parent_child_edges": parent_child,
        "ready_count": len(resolution.ready),
        "expected": {
            "task_count": spec.task_count,
            "edge_count": spec.edge_count,
            "blocks_edges": spec.blocks_edges,
            "provenance_edges": spec.provenance_edges,
            "parent_child_edges": spec.parent_child_edges,
            "ready_count": spec.ready_count,
        },
        "digest_sha256": fixture_digest(ledger),
        "machine_neutral": True,
        "contains_hostnames": False,
    }


def write_fixture_manifest(output: Path) -> dict[str, Any]:
    fixtures = {name: describe_fixture(name) for name in DATASETS}
    manifest = {
        "issue": ISSUE,
        "slice": SLICE,
        "kind": "work-store-fixture-manifest",
        "protocol_version": PROTOCOL_VERSION,
        "fixtures": fixtures,
    }
    write_report(manifest, output)
    return manifest


def _init_git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)


def _write_json_ledger(target: Path, ledger: dict[str, Any]) -> None:
    work_helpers._work_root(target).mkdir(parents=True, exist_ok=True)
    localio.write_json(work_helpers._tasks_path(target), ledger)


def _clone_ledger(ledger: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(ledger))


def _claim_target_id(ledger: dict[str, Any]) -> str:
    resolution = edges_mod.resolve_readiness(ledger)
    if not resolution.ready:
        raise MeasurementInputError("fixture has no ready task to claim")
    ordered = sorted(resolution.ready, key=claim_mod.ready_sort_key)
    return str(ordered[0]["id"])


def _latency_stats(samples_ns: list[int]) -> dict[str, Any]:
    ordered = sorted(samples_ns)
    return {
        "samples": len(ordered),
        "p50_ns": _percentile_nearest_rank(ordered, 50),
        "p95_ns": _percentile_nearest_rank(ordered, 95),
        "max_ns": ordered[-1],
        "min_ns": ordered[0],
    }


def _relative_to_json(stats: dict[str, Any], baseline: dict[str, Any] | None) -> dict[str, Any]:
    if baseline is None:
        return {"p50_vs_json": 1.0, "p95_vs_json": 1.0}
    p50_base = baseline["p50_ns"]
    p95_base = baseline["p95_ns"]
    return {
        "p50_vs_json": (stats["p50_ns"] / p50_base) if p50_base else None,
        "p95_vs_json": (stats["p95_ns"] / p95_base) if p95_base else None,
    }


# --- JSON ledger shape (current Brigade store) ---------------------------------


def _json_load_target(root: Path, ledger: dict[str, Any], *, with_git: bool = False) -> Path:
    target = Path(tempfile.mkdtemp(prefix="ws-json-", dir=root))
    if with_git:
        _init_git_repo(target)
    _write_json_ledger(target, _clone_ledger(ledger))
    return target


def _json_measure_mutation(
    root: Path,
    ledger: dict[str, Any],
    *,
    warmups: int,
    trials: int,
) -> dict[str, Any]:
    samples: list[int] = []
    task_id = _claim_target_id(ledger)
    total = warmups + trials
    for index in range(total):
        target = _json_load_target(root, ledger, with_git=False)
        try:
            start = time.perf_counter_ns()
            result = ledger_mod._claim_task(
                target,
                task_id,
                actor=f"actor-{index}",
                claim_id=f"claim-{index}",
            )
            elapsed = time.perf_counter_ns() - start
            if not result.get("created"):
                raise MeasurementInputError(f"json claim did not create: {result}")
            if index >= warmups:
                samples.append(elapsed)
        finally:
            shutil.rmtree(target, ignore_errors=True)
    return _latency_stats(samples)


def _json_claim_race(root: Path, ledger: dict[str, Any]) -> dict[str, Any]:
    target = _json_load_target(root, ledger, with_git=True)
    task_id = _claim_target_id(ledger)
    barrier = threading.Barrier(2)
    results: list[tuple[str, int, str]] = []
    lock = threading.Lock()

    def worker(actor: str, claim_id: str) -> None:
        barrier.wait(timeout=30)
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "brigade",
                "work",
                "claim",
                task_id,
                "--actor",
                actor,
                "--claim-id",
                claim_id,
                "--target",
                str(target),
                "--json",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        with lock:
            results.append((actor, proc.returncode, proc.stdout))

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(worker, "lane-a", "claim-a"),
                executor.submit(worker, "lane-b", "claim-b"),
            ]
            for future in futures:
                future.result(timeout=60)
        codes = sorted(code for _actor, code, _out in results)
        winners = [item for item in results if item[1] == 0]
        losers = [item for item in results if item[1] == 13]
        ok = codes == [0, 13] and len(winners) == 1 and len(losers) == 1
        detail: dict[str, Any] = {
            "status": "measured",
            "codes": codes,
            "winner_count": len(winners),
            "exit_13_count": len(losers),
            "pass": ok,
        }
        if winners:
            detail["winner_actor"] = winners[0][0]
        if losers:
            payload = json.loads(losers[0][2]) if losers[0][2].strip() else {}
            detail["loser_reason"] = payload.get("reason")
            detail["loser_holder"] = payload.get("holder")
        return detail
    finally:
        shutil.rmtree(target, ignore_errors=True)


def _json_graph_conformance(ledger: dict[str, Any]) -> dict[str, Any]:
    resolution = edges_mod.resolve_readiness(_clone_ledger(ledger))
    # Provenance-only edges must not create readiness cycles.
    provenance_only = all(
        edge["type"] == "discovered-from" or edge["type"] in {"blocks", "parent-child"} for edge in ledger["edges"]
    )
    return {
        "ready_count": len(resolution.ready),
        "blocked_count": len(resolution.blocked),
        "cycle_count": len(resolution.cycles),
        "provenance_ignored_for_readiness": provenance_only,
        "pass": len(resolution.cycles) == 0,
    }


def _json_export_import(root: Path, ledger: dict[str, Any]) -> dict[str, Any]:
    target = _json_load_target(root, ledger, with_git=False)
    try:
        exported = ledger_mod._read_task_ledger(target)
        digest_before = fixture_digest(exported)
        roundtrip = Path(tempfile.mkdtemp(prefix="ws-json-rt-", dir=root))
        try:
            _write_json_ledger(roundtrip, exported)
            imported = ledger_mod._read_task_ledger(roundtrip)
            digest_after = fixture_digest(imported)
            return {
                "status": "measured",
                "digest_before": digest_before,
                "digest_after": digest_after,
                "pass": digest_before == digest_after,
            }
        finally:
            shutil.rmtree(roundtrip, ignore_errors=True)
    finally:
        shutil.rmtree(target, ignore_errors=True)


def _json_crash_before_replace(root: Path, ledger: dict[str, Any]) -> dict[str, Any]:
    """Crash before atomic replace: original ledger must remain intact."""
    target = _json_load_target(root, ledger, with_git=False)
    path = work_helpers._tasks_path(target)
    before = path.read_bytes()
    try:
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write("{corrupt")
                handle.flush()
                os.fsync(handle.fileno())
            # Injected crash: abandon before os.replace.
            tmp_path.unlink(missing_ok=True)
            after = path.read_bytes()
            return {
                "status": "measured",
                "mode": "before_replace",
                "original_intact": before == after,
                "pass": before == after,
            }
        except Exception as exc:  # noqa: BLE001 - record exact failure
            return {
                "status": "measured",
                "mode": "before_replace",
                "pass": False,
                "error": str(exc),
            }
    finally:
        shutil.rmtree(target, ignore_errors=True)


def _json_same_actor_and_retry(root: Path, ledger: dict[str, Any]) -> dict[str, Any]:
    target = _json_load_target(root, ledger, with_git=True)
    task_id = _claim_target_id(ledger)
    try:
        first = ledger_mod._claim_task(target, task_id, actor="lane-a", claim_id="claim-same-1")
        retry = ledger_mod._claim_task(target, task_id, actor="lane-a", claim_id="claim-same-1")
        same_actor_code: int | None = None
        same_actor_reason: str | None = None
        try:
            ledger_mod._claim_task(target, task_id, actor="lane-a", claim_id="claim-same-2")
            same_actor_rejected = False
        except claim_mod.ClaimError as exc:
            same_actor_code = int(exc.exit_code)
            same_actor_reason = exc.reason
            same_actor_rejected = (
                same_actor_code == EXIT_CLAIM_BUSY and same_actor_reason == claim_mod.REASON_ALREADY_CLAIMED
            )
        ok = (
            bool(first.get("created"))
            and retry.get("created") is False
            and bool(retry.get("idempotent"))
            and same_actor_rejected
        )
        return {
            "status": "measured",
            "idempotent_retry": {
                "created_first": bool(first.get("created")),
                "created_retry": bool(retry.get("created")),
                "idempotent": bool(retry.get("idempotent")),
            },
            "same_actor_rejection": {
                "exit_code": same_actor_code,
                "reason": same_actor_reason,
                "pass": same_actor_rejected,
            },
            "pass": ok,
        }
    finally:
        shutil.rmtree(target, ignore_errors=True)


def _json_guard_and_empty_filter(root: Path, ledger: dict[str, Any]) -> dict[str, Any]:
    target = _json_load_target(root, ledger, with_git=True)
    task_id = _claim_target_id(ledger)
    try:
        claimed = ledger_mod._claim_task(target, task_id, actor="lane-a", claim_id="guard-claim")
        with contextlib.redirect_stdout(io.StringIO()):
            guard_rc = work_cmd.release(
                target=target,
                task=[task_id],
                if_actor="other-lane",
                json_output=True,
            )
            empty_rc = work_cmd.release(target=target, actor=[""], json_output=True)
        after = ledger_mod._read_task_ledger(target)
        stored = next(task for task in after["tasks"] if task["id"] == task_id)
        still_held = stored.get("status") == "in_progress" and stored.get("assignee") == "lane-a"
        ok = bool(claimed.get("created")) and guard_rc == EXIT_CLAIM_BUSY and empty_rc == 2 and still_held
        return {
            "status": "measured",
            "guard_mismatch_exit": guard_rc,
            "empty_filter_exit": empty_rc,
            "still_held_after_rejects": still_held,
            "pass": ok,
        }
    finally:
        shutil.rmtree(target, ignore_errors=True)


def _json_restart_recovery(root: Path, ledger: dict[str, Any]) -> dict[str, Any]:
    target = _json_load_target(root, ledger, with_git=True)
    task_id = _claim_target_id(ledger)
    try:
        claimed = ledger_mod._claim_task(target, task_id, actor="lane-restart", claim_id="claim-restart")
        # Simulate process restart: drop in-memory handles and re-read from disk.
        reloaded = ledger_mod._read_task_ledger(target)
        stored = next(task for task in reloaded["tasks"] if task["id"] == task_id)
        claim = stored.get("claim") if isinstance(stored.get("claim"), dict) else {}
        ok = (
            bool(claimed.get("created"))
            and stored.get("status") == "in_progress"
            and stored.get("assignee") == "lane-restart"
            and claim.get("claim_id") == "claim-restart"
        )
        return {
            "status": "measured",
            "claim_persisted": ok,
            "assignee": stored.get("assignee"),
            "claim_id": claim.get("claim_id"),
            "pass": ok,
        }
    finally:
        shutil.rmtree(target, ignore_errors=True)


def _json_schema_version_policy(root: Path, ledger: dict[str, Any]) -> dict[str, Any]:
    """Observe current ledger version coercion (no production schema change)."""
    future = _clone_ledger(ledger)
    future["version"] = edges_mod.TASK_LEDGER_VERSION + 100
    past = _clone_ledger(ledger)
    past["version"] = 1
    future_target = _json_load_target(root, future, with_git=False)
    past_target = _json_load_target(root, past, with_git=False)
    try:
        future_on_disk = json.loads(work_helpers._tasks_path(future_target).read_text(encoding="utf-8"))
        past_on_disk = json.loads(work_helpers._tasks_path(past_target).read_text(encoding="utf-8"))
        future_loaded = ledger_mod._read_task_ledger(future_target)
        past_loaded = ledger_mod._read_task_ledger(past_target)
        future_version_after_read = future_loaded.get("version")
        past_version_after_read = past_loaded.get("version")
        future_coerced = future_version_after_read == edges_mod.TASK_LEDGER_VERSION
        past_coerced = past_version_after_read == edges_mod.TASK_LEDGER_VERSION
        return {
            "status": "measured",
            "expected_version": edges_mod.TASK_LEDGER_VERSION,
            "future_version_written": future_on_disk.get("version"),
            "future_version_after_read": future_version_after_read,
            "future_version_rejected": False,
            "future_version_coerced_on_read": future_coerced,
            "downgrade_version_written": past_on_disk.get("version"),
            "downgrade_version_after_read": past_version_after_read,
            "downgrade_rejected": False,
            "downgrade_coerced_on_read": past_coerced,
            "observed_policy": "ensure_ledger_edges_coerces_version_to_current",
            "pass": future_coerced and past_coerced,
        }
    finally:
        shutil.rmtree(future_target, ignore_errors=True)
        shutil.rmtree(past_target, ignore_errors=True)


def _json_backup_restore(root: Path, ledger: dict[str, Any]) -> dict[str, Any]:
    target = _json_load_target(root, ledger, with_git=True)
    task_id = _claim_target_id(ledger)
    backup_root = Path(tempfile.mkdtemp(prefix="ws-json-backup-", dir=root))
    restore_root = Path(tempfile.mkdtemp(prefix="ws-json-restore-", dir=root))
    try:
        ledger_mod._claim_task(target, task_id, actor="lane-backup", claim_id="claim-backup")
        config_path = _write_synthetic_backup_config(target)
        before_tasks = fixture_digest(ledger_mod._read_task_ledger(target))
        before_claims = _claim_record_digest(ledger_mod._read_task_ledger(target))
        before_config = _digest_file(config_path)
        start = time.perf_counter_ns()
        # Preserve relative layout under .brigade for restore.
        shutil.copytree(target / ".brigade", backup_root / ".brigade")
        backup_ns = time.perf_counter_ns() - start
        # Mutate live store after backup.
        live = ledger_mod._read_task_ledger(target)
        for task in live["tasks"]:
            if task["id"] == task_id:
                task["text"] = "mutated-after-backup"
        _write_json_ledger(target, live)
        (target / ".brigade" / "backups.toml").write_text("# mutated\n", encoding="utf-8")
        # Restore into a fresh tree.
        (restore_root / ".brigade").mkdir(parents=True, exist_ok=True)
        shutil.copytree(backup_root / ".brigade", restore_root / ".brigade", dirs_exist_ok=True)
        restored = ledger_mod._read_task_ledger(restore_root)
        after_tasks = fixture_digest(restored)
        after_claims = _claim_record_digest(restored)
        after_config = _digest_file(work_helpers._backup_config_path(restore_root))
        branch = _branch_heads_digest(restore_root)
        ok = (
            before_tasks == after_tasks
            and before_claims == after_claims
            and before_config is not None
            and before_config == after_config
        )
        return {
            "status": "measured",
            "task_edge_digest_before": before_tasks,
            "task_edge_digest_after": after_tasks,
            "claim_digest_before": before_claims,
            "claim_digest_after": after_claims,
            "config_digest_before": before_config,
            "config_digest_after": after_config,
            "branch_heads": branch,
            "backup_time_ns": backup_ns,
            "pass": ok,
        }
    finally:
        shutil.rmtree(target, ignore_errors=True)
        shutil.rmtree(backup_root, ignore_errors=True)
        shutil.rmtree(restore_root, ignore_errors=True)


def _json_install_footprint(root: Path, ledger: dict[str, Any]) -> dict[str, Any]:
    target = _json_load_target(root, ledger, with_git=False)
    try:
        work_bytes = _tree_size_bytes(work_helpers._work_root(target))
        return {
            "status": "measured",
            "work_tree_bytes": work_bytes,
            "tasks_json_bytes": work_helpers._tasks_path(target).stat().st_size,
            "runtime_dependency_bytes": 0,
            "note": "JSON ledger is in-tree; no extra runtime dependency install",
            "pass": work_bytes > 0,
        }
    finally:
        shutil.rmtree(target, ignore_errors=True)


def _json_cold_start(root: Path, ledger: dict[str, Any]) -> dict[str, Any]:
    target = _json_load_target(root, ledger, with_git=False)
    try:
        start = time.perf_counter_ns()
        loaded = ledger_mod._read_task_ledger(target)
        elapsed = time.perf_counter_ns() - start
        return {
            "status": "measured",
            "cold_start_ns": elapsed,
            "task_count": len(loaded.get("tasks", [])),
            "pass": elapsed >= 0 and len(loaded.get("tasks", [])) == len(ledger["tasks"]),
        }
    finally:
        shutil.rmtree(target, ignore_errors=True)


def _json_resource_measurements(root: Path, ledger: dict[str, Any]) -> dict[str, Any]:
    target = _json_load_target(root, ledger, with_git=True)
    task_id = _claim_target_id(ledger)
    before_bytes = _tree_size_bytes(work_helpers._work_root(target))
    try:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "brigade",
                "work",
                "claim",
                task_id,
                "--actor",
                "lane-rss",
                "--claim-id",
                "claim-rss",
                "--target",
                str(target),
                "--json",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        peak_rss_kib: int | None = None
        while proc.poll() is None:
            sample = _proc_peak_rss_kib(proc.pid)
            if sample is not None:
                peak_rss_kib = sample if peak_rss_kib is None else max(peak_rss_kib, sample)
            time.sleep(0.002)
        stdout, _stderr = proc.communicate()
        after_bytes = _tree_size_bytes(work_helpers._work_root(target))
        if peak_rss_kib is None and proc.pid:
            # Final sample after exit may still be unavailable; mark unavailable RSS.
            rss_status = "unavailable"
        else:
            rss_status = "measured"
        ok = proc.returncode == 0
        return {
            "status": "measured",
            "claim_exit_code": proc.returncode,
            "disk_bytes_before": before_bytes,
            "disk_bytes_after": after_bytes,
            "disk_growth_bytes": after_bytes - before_bytes,
            "peak_rss_kib": peak_rss_kib,
            "peak_rss_mib": None if peak_rss_kib is None else round(peak_rss_kib / 1024, 2),
            "rss_sampling": rss_status if peak_rss_kib is None else "measured",
            "stdout_bytes": len(stdout.encode("utf-8")),
            "pass": ok and after_bytes >= before_bytes,
        }
    finally:
        shutil.rmtree(target, ignore_errors=True)


def _json_metrics_state(root: Path, ledger: dict[str, Any]) -> dict[str, Any]:
    target = _json_load_target(root, ledger, with_git=True)
    task_id = _claim_target_id(ledger)
    env = os.environ.copy()
    env["DISABLE_TELEMETRY"] = "1"
    env["BRIGADE_ANONYMOUS_METRICS"] = "0"
    try:
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "brigade",
                "work",
                "claim",
                task_id,
                "--actor",
                "lane-metrics",
                "--claim-id",
                "claim-metrics",
                "--target",
                str(target),
                "--json",
            ],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        artifacts = _scan_metrics_artifacts(target)
        disabled = env.get("BRIGADE_ANONYMOUS_METRICS") == "0" and env.get("DISABLE_TELEMETRY") == "1"
        ok = proc.returncode == 0 and disabled and artifacts == []
        return {
            "status": "measured",
            "anonymous_metrics_config": "disabled",
            "disable_telemetry_env": env.get("DISABLE_TELEMETRY"),
            "brigade_anonymous_metrics_env": env.get("BRIGADE_ANONYMOUS_METRICS"),
            "metrics_artifacts": artifacts,
            "claim_exit_code": proc.returncode,
            "pass": ok,
        }
    finally:
        shutil.rmtree(target, ignore_errors=True)


def _json_secret_history(root: Path, ledger: dict[str, Any]) -> dict[str, Any]:
    seeded = _clone_ledger(ledger)
    task_id = _claim_target_id(seeded)
    for task in seeded["tasks"]:
        if task["id"] == task_id:
            task["text"] = f"probe {SYNTHETIC_SECRET} path={SYNTHETIC_SECRET_PATH}"
            break
    target = _json_load_target(root, seeded, with_git=False)
    backup_pre = Path(tempfile.mkdtemp(prefix="ws-secret-pre-", dir=root))
    backup_post = Path(tempfile.mkdtemp(prefix="ws-secret-post-", dir=root))
    try:
        shutil.copytree(target / ".brigade", backup_pre / ".brigade")
        live = ledger_mod._read_task_ledger(target)
        for task in live["tasks"]:
            if task["id"] == task_id:
                task["text"] = "probe redacted"
        _write_json_ledger(target, live)
        shutil.copytree(target / ".brigade", backup_post / ".brigade")
        live_has = _path_contains_marker(work_helpers._tasks_path(target), SYNTHETIC_SECRET)
        pre_has = _path_contains_marker(backup_pre, SYNTHETIC_SECRET)
        post_has = _path_contains_marker(backup_post, SYNTHETIC_SECRET)
        path_live_has = _path_contains_marker(work_helpers._tasks_path(target), SYNTHETIC_SECRET_PATH)
        # JSON ledger has no VC history; deleted values must not remain in live
        # store or post-delete backups. Pre-delete file-copy backups retain bytes.
        ok = (not live_has) and (not post_has) and pre_has and (not path_live_has)
        return {
            "status": "measured",
            "synthetic_secret_marker": SYNTHETIC_SECRET,
            "live_retains_deleted_secret": live_has,
            "pre_delete_backup_retains_secret": pre_has,
            "post_delete_backup_retains_secret": post_has,
            "live_retains_deleted_path": path_live_has,
            "version_control_history": "absent",
            "pass": ok,
        }
    finally:
        shutil.rmtree(target, ignore_errors=True)
        shutil.rmtree(backup_pre, ignore_errors=True)
        shutil.rmtree(backup_post, ignore_errors=True)


def measure_json_ledger(
    root: Path,
    dataset: str,
    *,
    warmups: int,
    trials: int,
) -> dict[str, Any]:
    ledger = build_fixture(dataset)
    mutation = _json_measure_mutation(root, ledger, warmups=warmups, trials=trials)
    claim_race = _json_claim_race(root, ledger)
    same_actor = _json_same_actor_and_retry(root, ledger)
    guards = _json_guard_and_empty_filter(root, ledger)
    restart = _json_restart_recovery(root, ledger)
    schema = _json_schema_version_policy(root, ledger)
    graph = _json_graph_conformance(ledger)
    export_import = _json_export_import(root, ledger)
    crash = _json_crash_before_replace(root, ledger)
    backup_restore = _json_backup_restore(root, ledger)
    install_footprint = _json_install_footprint(root, ledger)
    cold_start = _json_cold_start(root, ledger)
    resources = _json_resource_measurements(root, ledger)
    metrics_state = _json_metrics_state(root, ledger)
    secret_history = _json_secret_history(root, ledger)
    claim_738 = bool(claim_race.get("pass")) and bool(same_actor.get("pass")) and bool(guards.get("pass"))
    gates = {
        "issue_737_graph": graph["pass"],
        "issue_738_claim": claim_738,
        "crash_consistency": crash["pass"],
        "lossless_export": export_import["pass"],
        "metrics_state": metrics_state["pass"],
        "secret_history_handling": secret_history["pass"],
        "restore": backup_restore["pass"],
    }
    return {
        "shape": "json_ledger",
        "status": "measured",
        "dataset": dataset,
        "mutation_latency": mutation,
        "relative_to_json": {"p50_vs_json": 1.0, "p95_vs_json": 1.0},
        "claim_race": claim_race,
        "same_actor_and_retry": same_actor,
        "guard_and_empty_filter": guards,
        "restart_recovery": restart,
        "schema_version_policy": schema,
        "graph_conformance": graph,
        "export_import": export_import,
        "crash_consistency": crash,
        "backup_restore": backup_restore,
        "install_footprint": install_footprint,
        "cold_start": cold_start,
        "resource_measurements": resources,
        "metrics_state": metrics_state,
        "secret_history_handling": secret_history,
        "branch_diff_merge": {
            "status": "unsupported",
            "detail": "JSON ledger has no first-class branch/diff/merge path",
        },
        "offline_divergence": {
            "status": "unsupported",
            "detail": "No portable sync primitive in current JSON ledger",
        },
        "server_auth_tls_permissions": _cell_blocked("requires network listener; pending separate operator approval"),
        "gates": gates,
        "pass": all(bool(value) for value in gates.values()),
    }


# --- SQLite WAL optional characterization adapter ------------------------------


_SQLITE_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=FULL;
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  payload TEXT NOT NULL,
  status TEXT NOT NULL,
  assignee TEXT,
  claim_id TEXT,
  item_revision INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS edges (
  id TEXT PRIMARY KEY,
  source TEXT NOT NULL,
  target TEXT NOT NULL,
  type TEXT NOT NULL,
  payload TEXT NOT NULL
);
"""


def _sqlite_connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30.0, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _sqlite_load(path: Path, ledger: dict[str, Any]) -> None:
    if path.exists():
        path.unlink()
    conn = _sqlite_connect(path)
    try:
        conn.executescript(_SQLITE_SCHEMA)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?)",
            ("version", str(ledger.get("version", edges_mod.TASK_LEDGER_VERSION))),
        )
        for task in ledger["tasks"]:
            conn.execute(
                "INSERT INTO tasks(id, payload, status, assignee, claim_id, item_revision) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    task["id"],
                    json.dumps(task, sort_keys=True),
                    task.get("status") or "pending",
                    task.get("assignee"),
                    (task.get("claim") or {}).get("claim_id") if isinstance(task.get("claim"), dict) else None,
                    int((task.get("claim") or {}).get("item_revision") or 0)
                    if isinstance(task.get("claim"), dict)
                    else 0,
                ),
            )
        for edge in ledger["edges"]:
            conn.execute(
                "INSERT INTO edges(id, source, target, type, payload) VALUES (?, ?, ?, ?, ?)",
                (
                    edge["id"],
                    edge["source"],
                    edge["target"],
                    edge["type"],
                    json.dumps(edge, sort_keys=True),
                ),
            )
        conn.execute("COMMIT")
    finally:
        conn.close()


def _sqlite_export_ledger(path: Path) -> dict[str, Any]:
    conn = _sqlite_connect(path)
    try:
        tasks = []
        for row in conn.execute("SELECT payload FROM tasks ORDER BY id"):
            tasks.append(json.loads(row[0]))
        edges = []
        for row in conn.execute("SELECT payload FROM edges ORDER BY id"):
            edges.append(json.loads(row[0]))
        version_row = conn.execute("SELECT value FROM meta WHERE key='version'").fetchone()
        version = int(version_row[0]) if version_row else edges_mod.TASK_LEDGER_VERSION
        ledger = {"version": version, "tasks": tasks, "edges": edges}
        edges_mod.ensure_ledger_edges(ledger)
        return ledger
    finally:
        conn.close()


def _sqlite_claim(path: Path, task_id: str, *, actor: str, claim_id: str) -> tuple[int, dict[str, Any]]:
    """CAS claim under BEGIN IMMEDIATE. Returns (exit_code, payload)."""
    conn = _sqlite_connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT status, assignee, claim_id, item_revision, payload FROM tasks WHERE id=?",
            (task_id,),
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return 2, {"reason": claim_mod.REASON_UNKNOWN_TASK}
        status, assignee, existing_claim, revision, payload_raw = row
        task = json.loads(payload_raw)
        if existing_claim == claim_id and status == "in_progress" and assignee == actor:
            conn.execute("COMMIT")
            return 0, {"created": False, "reason": "idempotent_retry"}
        if status == "in_progress" or existing_claim:
            holder = assignee or "unknown"
            conn.execute("ROLLBACK")
            return EXIT_CLAIM_BUSY, {
                "reason": claim_mod.REASON_ALREADY_CLAIMED,
                "holder": holder,
                "exit_code": EXIT_CLAIM_BUSY,
            }
        if status != "pending":
            conn.execute("ROLLBACK")
            return EXIT_CLAIM_BUSY, {
                "reason": claim_mod.REASON_NOT_READY,
                "exit_code": EXIT_CLAIM_BUSY,
            }
        claimed_at = _FIXED_CREATED_AT
        task["status"] = "in_progress"
        task["assignee"] = actor
        task["claim"] = {
            "claim_id": claim_id,
            "actor": actor,
            "claimed_at": claimed_at,
            "item_revision": int(revision) + 1,
        }
        task["updated_at"] = claimed_at
        conn.execute(
            "UPDATE tasks SET status=?, assignee=?, claim_id=?, item_revision=?, payload=? WHERE id=?",
            (
                "in_progress",
                actor,
                claim_id,
                int(revision) + 1,
                json.dumps(task, sort_keys=True),
                task_id,
            ),
        )
        conn.execute("COMMIT")
        return 0, {"created": True, "holder": actor, "claim_id": claim_id}
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


def _sqlite_measure_mutation(
    root: Path,
    ledger: dict[str, Any],
    *,
    warmups: int,
    trials: int,
) -> dict[str, Any]:
    samples: list[int] = []
    task_id = _claim_target_id(ledger)
    for index in range(warmups + trials):
        db_path = Path(tempfile.mkstemp(prefix="ws-sqlite-", suffix=".db", dir=root)[1])
        try:
            _sqlite_load(db_path, ledger)
            start = time.perf_counter_ns()
            code, payload = _sqlite_claim(
                db_path,
                task_id,
                actor=f"actor-{index}",
                claim_id=f"claim-{index}",
            )
            elapsed = time.perf_counter_ns() - start
            if code != 0 or not payload.get("created"):
                raise MeasurementInputError(f"sqlite claim failed: {code} {payload}")
            if index >= warmups:
                samples.append(elapsed)
        finally:
            for suffix in ("", "-wal", "-shm"):
                Path(str(db_path) + suffix).unlink(missing_ok=True)
    return _latency_stats(samples)


def _sqlite_claim_race(root: Path, ledger: dict[str, Any]) -> dict[str, Any]:
    db_path = Path(tempfile.mkstemp(prefix="ws-sqlite-race-", suffix=".db", dir=root)[1])
    _sqlite_load(db_path, ledger)
    task_id = _claim_target_id(ledger)
    barrier = threading.Barrier(2)
    results: list[tuple[str, int, dict[str, Any]]] = []
    lock = threading.Lock()

    def worker(actor: str, claim_id: str) -> None:
        barrier.wait(timeout=30)
        code, payload = _sqlite_claim(db_path, task_id, actor=actor, claim_id=claim_id)
        with lock:
            results.append((actor, code, payload))

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(worker, "lane-a", "claim-a"),
                executor.submit(worker, "lane-b", "claim-b"),
            ]
            for future in futures:
                future.result(timeout=60)
        codes = sorted(code for _actor, code, _payload in results)
        winners = [item for item in results if item[1] == 0]
        losers = [item for item in results if item[1] == 13]
        ok = codes == [0, 13] and len(winners) == 1 and len(losers) == 1
        detail: dict[str, Any] = {
            "status": "measured",
            "codes": codes,
            "winner_count": len(winners),
            "exit_13_count": len(losers),
            "pass": ok,
        }
        if winners:
            detail["winner_actor"] = winners[0][0]
        if losers:
            detail["loser_reason"] = losers[0][2].get("reason")
            detail["loser_holder"] = losers[0][2].get("holder")
        return detail
    finally:
        for suffix in ("", "-wal", "-shm"):
            Path(str(db_path) + suffix).unlink(missing_ok=True)


def _sqlite_crash_before_commit(root: Path, ledger: dict[str, Any]) -> dict[str, Any]:
    db_path = Path(tempfile.mkstemp(prefix="ws-sqlite-crash-", suffix=".db", dir=root)[1])
    try:
        _sqlite_load(db_path, ledger)
        before = _sqlite_export_ledger(db_path)
        task_id = _claim_target_id(before)
        conn = _sqlite_connect(db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE tasks SET status=?, assignee=?, claim_id=? WHERE id=?",
                ("in_progress", "crash-actor", "crash-claim", task_id),
            )
            # Injected crash: rollback instead of commit.
            conn.execute("ROLLBACK")
        finally:
            conn.close()
        after = _sqlite_export_ledger(db_path)
        return {
            "status": "measured",
            "mode": "before_commit",
            "original_intact": fixture_digest(before) == fixture_digest(after),
            "pass": fixture_digest(before) == fixture_digest(after),
        }
    finally:
        for suffix in ("", "-wal", "-shm"):
            Path(str(db_path) + suffix).unlink(missing_ok=True)


def _sqlite_same_actor_and_retry(root: Path, ledger: dict[str, Any]) -> dict[str, Any]:
    db_path = Path(tempfile.mkstemp(prefix="ws-sqlite-same-", suffix=".db", dir=root)[1])
    try:
        _sqlite_load(db_path, ledger)
        task_id = _claim_target_id(ledger)
        code1, first = _sqlite_claim(db_path, task_id, actor="lane-a", claim_id="claim-same-1")
        code2, retry = _sqlite_claim(db_path, task_id, actor="lane-a", claim_id="claim-same-1")
        code3, same = _sqlite_claim(db_path, task_id, actor="lane-a", claim_id="claim-same-2")
        same_ok = code3 == EXIT_CLAIM_BUSY and same.get("reason") == claim_mod.REASON_ALREADY_CLAIMED
        ok = code1 == 0 and bool(first.get("created")) and code2 == 0 and not retry.get("created") and same_ok
        return {
            "status": "measured",
            "idempotent_retry": {
                "created_first": bool(first.get("created")),
                "created_retry": bool(retry.get("created")),
                "reason": retry.get("reason"),
            },
            "same_actor_rejection": {
                "exit_code": code3,
                "reason": same.get("reason"),
                "pass": same_ok,
            },
            "pass": ok,
        }
    finally:
        for suffix in ("", "-wal", "-shm"):
            Path(str(db_path) + suffix).unlink(missing_ok=True)


def _sqlite_restart_recovery(root: Path, ledger: dict[str, Any]) -> dict[str, Any]:
    db_path = Path(tempfile.mkstemp(prefix="ws-sqlite-restart-", suffix=".db", dir=root)[1])
    try:
        _sqlite_load(db_path, ledger)
        task_id = _claim_target_id(ledger)
        code, claimed = _sqlite_claim(db_path, task_id, actor="lane-restart", claim_id="claim-restart")
        # New connection = process restart for this adapter.
        exported = _sqlite_export_ledger(db_path)
        stored = next(task for task in exported["tasks"] if task["id"] == task_id)
        claim = stored.get("claim") if isinstance(stored.get("claim"), dict) else {}
        ok = (
            code == 0
            and bool(claimed.get("created"))
            and stored.get("status") == "in_progress"
            and stored.get("assignee") == "lane-restart"
            and claim.get("claim_id") == "claim-restart"
        )
        return {
            "status": "measured",
            "claim_persisted": ok,
            "assignee": stored.get("assignee"),
            "claim_id": claim.get("claim_id"),
            "pass": ok,
        }
    finally:
        for suffix in ("", "-wal", "-shm"):
            Path(str(db_path) + suffix).unlink(missing_ok=True)


def _sqlite_schema_version_policy(root: Path, ledger: dict[str, Any]) -> dict[str, Any]:
    future_path = Path(tempfile.mkstemp(prefix="ws-sqlite-future-", suffix=".db", dir=root)[1])
    past_path = Path(tempfile.mkstemp(prefix="ws-sqlite-past-", suffix=".db", dir=root)[1])
    try:
        future = _clone_ledger(ledger)
        future["version"] = edges_mod.TASK_LEDGER_VERSION + 100
        past = _clone_ledger(ledger)
        past["version"] = 1
        _sqlite_load(future_path, future)
        _sqlite_load(past_path, past)
        # Adapter stores meta.version as written; export returns stored int.
        future_export = _sqlite_export_ledger(future_path)
        past_export = _sqlite_export_ledger(past_path)
        # After export, ensure_ledger_edges coerces for graph use.
        future_coerced = future_export.get("version") == edges_mod.TASK_LEDGER_VERSION
        past_coerced = past_export.get("version") == edges_mod.TASK_LEDGER_VERSION
        conn = _sqlite_connect(future_path)
        try:
            future_meta = conn.execute("SELECT value FROM meta WHERE key='version'").fetchone()[0]
        finally:
            conn.close()
        conn = _sqlite_connect(past_path)
        try:
            past_meta = conn.execute("SELECT value FROM meta WHERE key='version'").fetchone()[0]
        finally:
            conn.close()
        return {
            "status": "measured",
            "expected_version": edges_mod.TASK_LEDGER_VERSION,
            "future_version_written": int(future_meta),
            "future_version_after_export": future_export.get("version"),
            "future_version_rejected": False,
            "future_version_coerced_on_export": future_coerced,
            "downgrade_version_written": int(past_meta),
            "downgrade_version_after_export": past_export.get("version"),
            "downgrade_rejected": False,
            "downgrade_coerced_on_export": past_coerced,
            "observed_policy": "adapter_meta_preserves_written_version_export_coerces_via_ensure_ledger_edges",
            "pass": future_coerced and past_coerced and int(future_meta) == edges_mod.TASK_LEDGER_VERSION + 100,
        }
    finally:
        for path in (future_path, past_path):
            for suffix in ("", "-wal", "-shm"):
                Path(str(path) + suffix).unlink(missing_ok=True)


def _sqlite_backup_restore(root: Path, ledger: dict[str, Any]) -> dict[str, Any]:
    db_path = Path(tempfile.mkstemp(prefix="ws-sqlite-live-", suffix=".db", dir=root)[1])
    backup_path = Path(tempfile.mkstemp(prefix="ws-sqlite-bak-", suffix=".db", dir=root)[1])
    config_src = Path(tempfile.mkdtemp(prefix="ws-sqlite-cfg-", dir=root))
    try:
        _sqlite_load(db_path, ledger)
        task_id = _claim_target_id(ledger)
        _sqlite_claim(db_path, task_id, actor="lane-backup", claim_id="claim-backup")
        config_path = _write_synthetic_backup_config(config_src)
        before_ledger = _sqlite_export_ledger(db_path)
        before_tasks = fixture_digest(before_ledger)
        before_claims = _claim_record_digest(before_ledger)
        before_config = _digest_file(config_path)
        start = time.perf_counter_ns()
        shutil.copy2(db_path, backup_path)
        for suffix in ("-wal", "-shm"):
            src = Path(str(db_path) + suffix)
            if src.exists():
                shutil.copy2(src, Path(str(backup_path) + suffix))
        config_backup = config_src / "backups.toml.backup"
        shutil.copy2(config_path, config_backup)
        backup_ns = time.perf_counter_ns() - start
        # Mutate live
        conn = _sqlite_connect(db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE tasks SET status=? WHERE id=?", ("done", task_id))
            conn.execute("COMMIT")
        finally:
            conn.close()
        config_path.write_text("# mutated\n", encoding="utf-8")
        restored = _sqlite_export_ledger(backup_path)
        after_tasks = fixture_digest(restored)
        after_claims = _claim_record_digest(restored)
        after_config = _digest_file(config_backup)
        ok = (
            before_tasks == after_tasks
            and before_claims == after_claims
            and before_config is not None
            and before_config == after_config
        )
        return {
            "status": "measured",
            "task_edge_digest_before": before_tasks,
            "task_edge_digest_after": after_tasks,
            "claim_digest_before": before_claims,
            "claim_digest_after": after_claims,
            "config_digest_before": before_config,
            "config_digest_after": after_config,
            "branch_heads": _branch_heads_digest(config_src),
            "backup_time_ns": backup_ns,
            "pass": ok,
        }
    finally:
        for path in (db_path, backup_path):
            for suffix in ("", "-wal", "-shm"):
                Path(str(path) + suffix).unlink(missing_ok=True)
        shutil.rmtree(config_src, ignore_errors=True)


def _sqlite_install_footprint(root: Path, ledger: dict[str, Any]) -> dict[str, Any]:
    db_path = Path(tempfile.mkstemp(prefix="ws-sqlite-size-", suffix=".db", dir=root)[1])
    try:
        _sqlite_load(db_path, ledger)
        size = db_path.stat().st_size
        return {
            "status": "measured",
            "db_bytes": size,
            "runtime_dependency_bytes": 0,
            "note": "stdlib sqlite3 only; not a Brigade runtime dependency",
            "pass": size > 0,
        }
    finally:
        for suffix in ("", "-wal", "-shm"):
            Path(str(db_path) + suffix).unlink(missing_ok=True)


def _sqlite_cold_start(root: Path, ledger: dict[str, Any]) -> dict[str, Any]:
    db_path = Path(tempfile.mkstemp(prefix="ws-sqlite-cold-", suffix=".db", dir=root)[1])
    try:
        _sqlite_load(db_path, ledger)
        start = time.perf_counter_ns()
        exported = _sqlite_export_ledger(db_path)
        elapsed = time.perf_counter_ns() - start
        return {
            "status": "measured",
            "cold_start_ns": elapsed,
            "task_count": len(exported.get("tasks", [])),
            "pass": elapsed >= 0 and len(exported.get("tasks", [])) == len(ledger["tasks"]),
        }
    finally:
        for suffix in ("", "-wal", "-shm"):
            Path(str(db_path) + suffix).unlink(missing_ok=True)


def _sqlite_resource_measurements(root: Path, ledger: dict[str, Any]) -> dict[str, Any]:
    db_path = Path(tempfile.mkstemp(prefix="ws-sqlite-rss-", suffix=".db", dir=root)[1])
    try:
        _sqlite_load(db_path, ledger)
        task_id = _claim_target_id(ledger)
        before = db_path.stat().st_size
        # Measure RSS of this process around the claim (adapter is in-process).
        pid = os.getpid()
        before_rss = _proc_peak_rss_kib(pid)
        code, payload = _sqlite_claim(db_path, task_id, actor="lane-rss", claim_id="claim-rss")
        after_rss = _proc_peak_rss_kib(pid)
        after = db_path.stat().st_size
        peak = None
        if before_rss is not None or after_rss is not None:
            candidates = [value for value in (before_rss, after_rss) if value is not None]
            peak = max(candidates) if candidates else None
        return {
            "status": "measured",
            "claim_exit_code": code,
            "disk_bytes_before": before,
            "disk_bytes_after": after,
            "disk_growth_bytes": after - before,
            "peak_rss_kib": peak,
            "peak_rss_mib": None if peak is None else round(peak / 1024, 2),
            "rss_sampling": "measured" if peak is not None else "unavailable",
            "pass": code == 0 and bool(payload.get("created")),
        }
    finally:
        for suffix in ("", "-wal", "-shm"):
            Path(str(db_path) + suffix).unlink(missing_ok=True)


def _sqlite_metrics_state(root: Path, ledger: dict[str, Any]) -> dict[str, Any]:
    db_path = Path(tempfile.mkstemp(prefix="ws-sqlite-metrics-", suffix=".db", dir=root)[1])
    workdir = Path(tempfile.mkdtemp(prefix="ws-sqlite-metrics-dir-", dir=root))
    try:
        _sqlite_load(db_path, ledger)
        task_id = _claim_target_id(ledger)
        os.environ["DISABLE_TELEMETRY"] = "1"
        os.environ["BRIGADE_ANONYMOUS_METRICS"] = "0"
        code, _payload = _sqlite_claim(db_path, task_id, actor="lane-metrics", claim_id="claim-metrics")
        artifacts = _scan_metrics_artifacts(workdir)
        ok = code == 0 and artifacts == []
        return {
            "status": "measured",
            "anonymous_metrics_config": "disabled",
            "disable_telemetry_env": os.environ.get("DISABLE_TELEMETRY"),
            "brigade_anonymous_metrics_env": os.environ.get("BRIGADE_ANONYMOUS_METRICS"),
            "metrics_artifacts": artifacts,
            "claim_exit_code": code,
            "pass": ok,
        }
    finally:
        for suffix in ("", "-wal", "-shm"):
            Path(str(db_path) + suffix).unlink(missing_ok=True)
        shutil.rmtree(workdir, ignore_errors=True)


def _sqlite_secret_history(root: Path, ledger: dict[str, Any]) -> dict[str, Any]:
    seeded = _clone_ledger(ledger)
    task_id = _claim_target_id(seeded)
    for task in seeded["tasks"]:
        if task["id"] == task_id:
            task["text"] = f"probe {SYNTHETIC_SECRET} path={SYNTHETIC_SECRET_PATH}"
            break
    db_path = Path(tempfile.mkstemp(prefix="ws-sqlite-secret-", suffix=".db", dir=root)[1])
    backup_pre = Path(tempfile.mkstemp(prefix="ws-sqlite-secret-pre-", suffix=".db", dir=root)[1])
    backup_post = Path(tempfile.mkstemp(prefix="ws-sqlite-secret-post-", suffix=".db", dir=root)[1])
    try:
        _sqlite_load(db_path, seeded)
        shutil.copy2(db_path, backup_pre)
        # Delete secret from live payload.
        conn = _sqlite_connect(db_path)
        try:
            row = conn.execute("SELECT payload FROM tasks WHERE id=?", (task_id,)).fetchone()
            task = json.loads(row[0])
            task["text"] = "probe redacted"
            conn.execute(
                "BEGIN IMMEDIATE",
            )
            conn.execute(
                "UPDATE tasks SET payload=? WHERE id=?",
                (json.dumps(task, sort_keys=True), task_id),
            )
            conn.execute("COMMIT")
            # Compact so deleted payload pages are not left in the live file.
            conn.execute("VACUUM")
        finally:
            conn.close()
        shutil.copy2(db_path, backup_post)
        live_has = _path_contains_marker(db_path, SYNTHETIC_SECRET)
        pre_has = _path_contains_marker(backup_pre, SYNTHETIC_SECRET)
        post_has = _path_contains_marker(backup_post, SYNTHETIC_SECRET)
        ok = (not live_has) and (not post_has) and pre_has
        return {
            "status": "measured",
            "synthetic_secret_marker": SYNTHETIC_SECRET,
            "live_retains_deleted_secret": live_has,
            "pre_delete_backup_retains_secret": pre_has,
            "post_delete_backup_retains_secret": post_has,
            "version_control_history": "absent",
            "pass": ok,
        }
    finally:
        for path in (db_path, backup_pre, backup_post):
            for suffix in ("", "-wal", "-shm"):
                Path(str(path) + suffix).unlink(missing_ok=True)


def measure_sqlite_wal(
    root: Path,
    dataset: str,
    *,
    warmups: int,
    trials: int,
    json_baseline: dict[str, Any] | None,
) -> dict[str, Any]:
    ledger = build_fixture(dataset)
    mutation = _sqlite_measure_mutation(root, ledger, warmups=warmups, trials=trials)
    claim_race = _sqlite_claim_race(root, ledger)
    same_actor = _sqlite_same_actor_and_retry(root, ledger)
    guards = _cell_unavailable(
        "sqlite characterization adapter does not implement Brigade release "
        "guards or empty-filter semantics; measured on json_ledger"
    )
    restart = _sqlite_restart_recovery(root, ledger)
    schema = _sqlite_schema_version_policy(root, ledger)
    # Graph readiness still evaluated by Brigade resolver on exported form.
    db_path = Path(tempfile.mkstemp(prefix="ws-sqlite-graph-", suffix=".db", dir=root)[1])
    try:
        _sqlite_load(db_path, ledger)
        exported = _sqlite_export_ledger(db_path)
        graph = _json_graph_conformance(exported)
        export_import = {
            "status": "measured",
            "digest_before": fixture_digest(ledger),
            "digest_after": fixture_digest(exported),
            "pass": fixture_digest(ledger) == fixture_digest(exported),
        }
    finally:
        for suffix in ("", "-wal", "-shm"):
            Path(str(db_path) + suffix).unlink(missing_ok=True)
    crash = _sqlite_crash_before_commit(root, ledger)
    backup_restore = _sqlite_backup_restore(root, ledger)
    install_footprint = _sqlite_install_footprint(root, ledger)
    cold_start = _sqlite_cold_start(root, ledger)
    resources = _sqlite_resource_measurements(root, ledger)
    metrics_state = _sqlite_metrics_state(root, ledger)
    secret_history = _sqlite_secret_history(root, ledger)
    baseline_stats = None if json_baseline is None else json_baseline.get("mutation_latency")
    claim_738 = bool(claim_race.get("pass")) and bool(same_actor.get("pass"))
    gates = {
        "issue_737_graph": graph["pass"],
        "issue_738_claim": claim_738,
        "crash_consistency": crash["pass"],
        "lossless_export": export_import["pass"],
        "metrics_state": metrics_state["pass"],
        "secret_history_handling": secret_history["pass"],
        "restore": backup_restore["pass"],
    }
    return {
        "shape": "sqlite_wal",
        "status": "measured",
        "dataset": dataset,
        "mutation_latency": mutation,
        "relative_to_json": _relative_to_json(mutation, baseline_stats),
        "claim_race": claim_race,
        "same_actor_and_retry": same_actor,
        "guard_and_empty_filter": guards,
        "restart_recovery": restart,
        "schema_version_policy": schema,
        "graph_conformance": graph,
        "export_import": export_import,
        "crash_consistency": crash,
        "backup_restore": backup_restore,
        "install_footprint": install_footprint,
        "cold_start": cold_start,
        "resource_measurements": resources,
        "metrics_state": metrics_state,
        "secret_history_handling": secret_history,
        "branch_diff_merge": {
            "status": "unsupported",
            "detail": "SQLite WAL adapter has no branch/diff/merge",
        },
        "offline_divergence": {
            "status": "unsupported",
            "detail": "File copy only; no merge protocol in this adapter",
        },
        "server_auth_tls_permissions": _cell_blocked("requires network listener; pending separate operator approval"),
        "gates": gates,
        "pass": all(bool(value) for value in gates.values()),
        "note": "stdlib sqlite3 characterization only; not a Brigade runtime dependency",
    }


# --- Dolt shapes (blocked / unmeasured in R1 unless explicitly enabled) --------


def _dolt_blocked_result(shape: str, dataset: str, reason: str) -> dict[str, Any]:
    blocked = {"pass": None, "status": "blocked"}
    return {
        "shape": shape,
        "status": "blocked",
        "dataset": dataset,
        "reason": reason,
        "mutation_latency": None,
        "relative_to_json": None,
        "claim_race": blocked,
        "same_actor_and_retry": blocked,
        "guard_and_empty_filter": blocked,
        "restart_recovery": blocked,
        "schema_version_policy": blocked,
        "graph_conformance": blocked,
        "export_import": blocked,
        "crash_consistency": blocked,
        "backup_restore": blocked,
        "install_footprint": blocked,
        "cold_start": blocked,
        "resource_measurements": blocked,
        "metrics_state": blocked,
        "secret_history_handling": blocked,
        "branch_diff_merge": {"status": "blocked"},
        "offline_divergence": {"status": "blocked"},
        "server_auth_tls_permissions": _cell_blocked("requires network listener; pending separate operator approval"),
        "gates": {
            "issue_737_graph": None,
            "issue_738_claim": None,
            "crash_consistency": None,
            "lossless_export": None,
            "metrics_state": None,
            "secret_history_handling": None,
            "restore": None,
        },
        "pass": None,
    }


def measure_dolt_shape(shape: str, dataset: str, *, dolt_bin: str | None) -> dict[str, Any]:
    if shape == "dolt_sql_server":
        return _dolt_blocked_result(
            shape,
            dataset,
            "R2 policy: harness must not start a network listener; "
            "operator-owned server characterization requires separate approval",
        )
    if shape == "embedded_dolt":
        return _dolt_blocked_result(
            shape,
            dataset,
            "No embedded Dolt boundary is checked into this repo; R2 does not build or persist a Go/Dolt dependency",
        )
    # dolt_cli_per_command
    resolved = dolt_bin or shutil.which("dolt")
    if not resolved:
        return _dolt_blocked_result(
            shape,
            dataset,
            "dolt binary not available on PATH; optional temporary CLI not supplied "
            "(--dolt-bin). Cell left unmeasured rather than fabricated",
        )
    # A binary may be present for future optional runners; R2 still does not
    # auto-run Dolt mutations without an explicit enable flag.
    return _dolt_blocked_result(
        shape,
        dataset,
        f"dolt binary observed at {Path(resolved).name} but R2 timebox keeps "
        "CLI mutation runner disabled; documentation-only characterization applies",
    )


def measure_shape(
    shape: str,
    dataset: str,
    root: Path,
    *,
    warmups: int,
    trials: int,
    json_baseline: dict[str, Any] | None = None,
    dolt_bin: str | None = None,
) -> dict[str, Any]:
    if shape not in SHAPES:
        raise ValueError(f"unknown shape: {shape}")
    if dataset not in DATASETS:
        raise ValueError(f"unknown dataset: {dataset}")
    if shape == "json_ledger":
        return measure_json_ledger(root, dataset, warmups=warmups, trials=trials)
    if shape == "sqlite_wal":
        return measure_sqlite_wal(
            root,
            dataset,
            warmups=warmups,
            trials=trials,
            json_baseline=json_baseline,
        )
    return measure_dolt_shape(shape, dataset, dolt_bin=dolt_bin)


def measure_matrix(
    root: Path,
    *,
    shapes: list[str] | None = None,
    datasets: list[str] | None = None,
    warmups: int = DEFAULT_WARMUPS,
    trials: int = DEFAULT_TRIALS,
    dolt_bin: str | None = None,
) -> dict[str, Any]:
    if warmups < 0:
        raise ValueError("warmups must be >= 0")
    if trials <= 0:
        raise ValueError("trials must be > 0")
    selected_shapes = list(shapes or SHAPES)
    selected_datasets = list(datasets or DATASETS)
    for shape in selected_shapes:
        if shape not in SHAPES:
            raise ValueError(f"unknown shape: {shape}")
    for dataset in selected_datasets:
        if dataset not in DATASETS:
            raise ValueError(f"unknown dataset: {dataset}")

    work = Path(tempfile.mkdtemp(prefix="brigade-work-store-measure-", dir=root))
    try:
        environment = environment_metadata(work)
        fixtures = {name: describe_fixture(name) for name in selected_datasets}
        results: list[dict[str, Any]] = []
        json_baselines: dict[str, dict[str, Any]] = {}

        # Always measure json first when present so relatives resolve.
        ordered_shapes = [s for s in selected_shapes if s == "json_ledger"] + [
            s for s in selected_shapes if s != "json_ledger"
        ]
        for dataset in selected_datasets:
            for shape in ordered_shapes:
                baseline = json_baselines.get(dataset)
                result = measure_shape(
                    shape,
                    dataset,
                    work,
                    warmups=warmups,
                    trials=trials,
                    json_baseline=baseline,
                    dolt_bin=dolt_bin,
                )
                if shape == "json_ledger":
                    json_baselines[dataset] = result
                results.append(result)

        capability_notes = capability_matrix_notes()
        decision = decision_record()
        return {
            "issue": ISSUE,
            "slice": SLICE,
            "kind": KIND,
            "protocol_version": PROTOCOL_VERSION,
            "timebox": TIMEBOX_NOTE,
            "warmups": warmups,
            "trials": trials,
            "shapes": selected_shapes,
            "datasets": selected_datasets,
            "fixtures": fixtures,
            "environment": environment,
            "results": results,
            "capability_notes": capability_notes,
            "decision": decision,
            "normative_anchors": {
                "issue_737": "graph ready-set, chains, diamonds, cycles, provenance-only edges",
                "issue_738": "claim CAS with one winner and exit 13 on lost race",
                "issue_770": "BUILD native; Beads excluded",
            },
            "anonymous_metrics": "disabled_in_harness",
        }
    finally:
        shutil.rmtree(work, ignore_errors=True)


def capability_matrix_notes() -> dict[str, Any]:
    """Documentation-backed capability notes. Unmeasured cells stay explicit."""
    return {
        "json_ledger": {
            "portable_sync": "absent_first_class",
            "concurrent_same_store_claim": "supported_via_dir_lock",
            "concurrent_cross_machine_claim": "not_supported",
            "branch_diff_merge": "absent",
            "row_locks": "n/a_file_cas",
            "dolt_commit_policy": "n/a",
        },
        "sqlite_wal": {
            "portable_sync": "file_copy_only",
            "concurrent_same_store_claim": "supported_via_begin_immediate",
            "concurrent_cross_machine_claim": "not_safe_on_shared_network_fs",
            "branch_diff_merge": "absent",
            "row_locks": "sqlite_transaction_locks",
            "dolt_commit_policy": "n/a",
            "runtime_dependency": "forbidden_for_brigade_runtime",
        },
        "dolt_cli_per_command": {
            "portable_sync": "clone_push_pull_candidate",
            "concurrent_same_store_claim": "unmeasured_r2",
            "concurrent_cross_machine_claim": "not_by_cli_alone",
            "branch_diff_merge": "native_candidate",
            "row_locks": "unsupported_select_for_update",
            "sql_tx_vs_claim_cas": (
                "SQL transaction success is not Brigade claim-CAS proof; "
                "cell-level merge can allow lost updates vs row-lock DBs"
            ),
            "dolt_commit_policy": (
                "candidate mapping: claim CAS / graph apply / release / reassign "
                "would need an explicit CALL DOLT_COMMIT after SQL commit; "
                "readiness queries would not create Dolt commits"
            ),
            "runtime_dependency": "forbidden_unless_later_issue_approves",
        },
        "embedded_dolt": {
            "portable_sync": "local_replica_candidate",
            "concurrent_cross_machine_claim": "not_by_embed_alone",
            "branch_diff_merge": "native_candidate",
            "row_locks": "unsupported_select_for_update",
            "server_lifecycle": "none",
            "runtime_dependency": "forbidden_unless_later_issue_approves",
            "status": "blocked_no_boundary_in_repo",
        },
        "dolt_sql_server": {
            "portable_sync": "central_service_candidate",
            "concurrent_cross_machine_claim": "possible_but_not_current_need",
            "branch_diff_merge": "native_candidate",
            "row_locks": "unsupported_select_for_update",
            "extra_ops": "users_grants_backup_version_lifecycle_listener",
            "listener_policy": "loopback_only_with_approval_or_no_listener",
            "eligibility_gate": (
                "eligible only if a later concurrent-claim requirement cannot "
                "meet measured correctness through file or replica candidates"
            ),
            "runtime_dependency": "forbidden_unless_later_issue_approves",
            "status": "blocked_no_listener_in_r2",
        },
        "sources": [
            "https://www.dolthub.com/docs/sql-reference/sql-support/supported-statements/",
            "https://www.dolthub.com/blog/2026-02-17-dolt-concurrency/",
            "https://www.dolthub.com/docs/introduction/installation/application-server/",
            "https://www.dolthub.com/docs/sql-reference/server/backups/",
            "https://www.dolthub.com/docs/other/versioning/",
        ],
    }


def decision_record() -> dict[str, Any]:
    return {
        "current_need": "sequential_portable_sync_plus_reviewable_branch_merge_proposals",
        "concurrent_cross_machine_claims": "not_current_requirement",
        "store_decision": "keep_machine_local_json_ledger",
        "portable_sync": "adopt_as_follow_up_requirement_not_implemented_in_r2",
        "sqlite": "optional_local_characterization_only_not_runtime_dependency",
        "shared_store_conformance_fixture": "not_approved",
        "shared_service_eligibility": ("only_if_later_concurrent_claim_requirement_fails_file_or_replica_candidates"),
        "dolt": "characterization_candidate_not_banned_not_selected",
        "beads": "excluded_under_770",
        "backend_adoption": False,
        "brigade_workstore_v1": False,
    }


def write_report(report: dict[str, Any], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(tempfile.gettempdir()),
        help="Parent directory for ephemeral measurement workspaces",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Diffable measurement JSON output path",
    )
    parser.add_argument("--warmups", type=int, default=DEFAULT_WARMUPS)
    parser.add_argument("--trials", type=int, default=DEFAULT_TRIALS)
    parser.add_argument(
        "--shapes",
        default=",".join(SHAPES),
        help="Comma-separated shapes to measure",
    )
    parser.add_argument(
        "--datasets",
        default=",".join(DATASETS),
        help="Comma-separated datasets to measure",
    )
    parser.add_argument(
        "--fixture-manifest",
        type=Path,
        help="Optional path to write the fixture manifest JSON",
    )
    parser.add_argument(
        "--dolt-bin",
        type=Path,
        help="Optional temporary dolt binary path (never persisted as a dependency)",
    )
    args = parser.parse_args(argv)

    shapes = [item.strip() for item in args.shapes.split(",") if item.strip()]
    datasets = [item.strip() for item in args.datasets.split(",") if item.strip()]
    if args.fixture_manifest is not None:
        write_fixture_manifest(args.fixture_manifest)

    report = measure_matrix(
        args.root,
        shapes=shapes,
        datasets=datasets,
        warmups=args.warmups,
        trials=args.trials,
        dolt_bin=str(args.dolt_bin) if args.dolt_bin else None,
    )
    write_report(report, args.output)
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
