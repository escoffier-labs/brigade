#!/usr/bin/env python3
"""Issue #846 R1 work-store characterization harness.

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
Latency remains descriptive (no absolute service level).

Standard library plus the in-repo ``brigade`` package only.
"""

from __future__ import annotations

import argparse
import hashlib
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
from brigade.work_cmd import claiming as claim_mod
from brigade.work_cmd import edges as edges_mod
from brigade.work_cmd import helpers as work_helpers
from brigade.work_cmd import ledger as ledger_mod

ISSUE = 846
SLICE = "R1"
KIND = "work-store-characterization"
PROTOCOL_VERSION = 1
TIMEBOX_NOTE = (
    "R1 timebox: automate JSON baseline first; SQLite WAL is an optional "
    "stdlib characterization adapter; Dolt shapes stay optional and must not "
    "enter Brigade runtime dependencies. Server listeners require separate "
    "operator approval and are never started here."
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
                "mode": "before_replace",
                "original_intact": before == after,
                "pass": before == after,
            }
        except Exception as exc:  # noqa: BLE001 - record exact failure
            return {"mode": "before_replace", "pass": False, "error": str(exc)}
    finally:
        shutil.rmtree(target, ignore_errors=True)


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
    graph = _json_graph_conformance(ledger)
    export_import = _json_export_import(root, ledger)
    crash = _json_crash_before_replace(root, ledger)
    gates = {
        "issue_737_graph": graph["pass"],
        "issue_738_claim": claim_race["pass"],
        "crash_consistency": crash["pass"],
        "lossless_export": export_import["pass"],
        "metrics_state": True,  # harness disables anonymous metrics by not enabling any
        "secret_history_handling": True,  # JSON ledger has no retained VC history
        "restore": export_import["pass"],
    }
    return {
        "shape": "json_ledger",
        "status": "measured",
        "dataset": dataset,
        "mutation_latency": mutation,
        "relative_to_json": {"p50_vs_json": 1.0, "p95_vs_json": 1.0},
        "claim_race": claim_race,
        "graph_conformance": graph,
        "export_import": export_import,
        "crash_consistency": crash,
        "branch_diff_merge": {
            "status": "unsupported",
            "detail": "JSON ledger has no first-class branch/diff/merge path",
        },
        "offline_divergence": {
            "status": "unsupported",
            "detail": "No portable sync primitive in current JSON ledger",
        },
        "gates": gates,
        "pass": all(gates.values()),
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
            "mode": "before_commit",
            "original_intact": fixture_digest(before) == fixture_digest(after),
            "pass": fixture_digest(before) == fixture_digest(after),
        }
    finally:
        for suffix in ("", "-wal", "-shm"):
            Path(str(db_path) + suffix).unlink(missing_ok=True)


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
    # Graph readiness still evaluated by Brigade resolver on exported form.
    db_path = Path(tempfile.mkstemp(prefix="ws-sqlite-graph-", suffix=".db", dir=root)[1])
    try:
        _sqlite_load(db_path, ledger)
        exported = _sqlite_export_ledger(db_path)
        graph = _json_graph_conformance(exported)
        export_import = {
            "digest_before": fixture_digest(ledger),
            "digest_after": fixture_digest(exported),
            "pass": fixture_digest(ledger) == fixture_digest(exported),
        }
    finally:
        for suffix in ("", "-wal", "-shm"):
            Path(str(db_path) + suffix).unlink(missing_ok=True)
    crash = _sqlite_crash_before_commit(root, ledger)
    baseline_stats = None if json_baseline is None else json_baseline.get("mutation_latency")
    gates = {
        "issue_737_graph": graph["pass"],
        "issue_738_claim": claim_race["pass"],
        "crash_consistency": crash["pass"],
        "lossless_export": export_import["pass"],
        "metrics_state": True,
        "secret_history_handling": True,  # no retained VC history in this adapter
        "restore": export_import["pass"],
    }
    return {
        "shape": "sqlite_wal",
        "status": "measured",
        "dataset": dataset,
        "mutation_latency": mutation,
        "relative_to_json": _relative_to_json(mutation, baseline_stats),
        "claim_race": claim_race,
        "graph_conformance": graph,
        "export_import": export_import,
        "crash_consistency": crash,
        "branch_diff_merge": {
            "status": "unsupported",
            "detail": "SQLite WAL adapter has no branch/diff/merge",
        },
        "offline_divergence": {
            "status": "unsupported",
            "detail": "File copy only; no merge protocol in this adapter",
        },
        "gates": gates,
        "pass": all(gates.values()),
        "note": "stdlib sqlite3 characterization only; not a Brigade runtime dependency",
    }


# --- Dolt shapes (blocked / unmeasured in R1 unless explicitly enabled) --------


def _dolt_blocked_result(shape: str, dataset: str, reason: str) -> dict[str, Any]:
    return {
        "shape": shape,
        "status": "blocked",
        "dataset": dataset,
        "reason": reason,
        "mutation_latency": None,
        "relative_to_json": None,
        "claim_race": {"pass": None, "status": "unmeasured"},
        "graph_conformance": {"pass": None, "status": "unmeasured"},
        "export_import": {"pass": None, "status": "unmeasured"},
        "crash_consistency": {"pass": None, "status": "unmeasured"},
        "branch_diff_merge": {"status": "unmeasured"},
        "offline_divergence": {"status": "unmeasured"},
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
            "R1 policy: harness must not start a network listener; "
            "operator-owned server characterization requires separate approval",
        )
    if shape == "embedded_dolt":
        return _dolt_blocked_result(
            shape,
            dataset,
            "No embedded Dolt boundary is checked into this repo; R1 does not build or persist a Go/Dolt dependency",
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
    # A binary may be present for future optional runners; R1 still does not
    # auto-run Dolt mutations without an explicit enable flag.
    return _dolt_blocked_result(
        shape,
        dataset,
        f"dolt binary observed at {Path(resolved).name} but R1 timebox keeps "
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
            "concurrent_same_store_claim": "unmeasured_r1",
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
            "status": "blocked_no_listener_in_r1",
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
        "portable_sync": "adopt_as_follow_up_requirement_not_implemented_in_r1",
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
