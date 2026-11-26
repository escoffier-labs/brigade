"""Fail-open GraphTrail delta snapshots for verification receipts."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from . import code_graph_background, code_references, component_bins, engine_paths

SNAPSHOT_NAME = "code-graph-before.db"
SNAPSHOT_AFTER_NAME = "code-graph-after.db"
SIDECAR_NAME = "graph-delta.json"
CHANGED_SYMBOL_LIMIT = 20
CODE_REFERENCE_NODE_LIMIT = 20
LINE_KEYS = {
    "line",
    "line_number",
    "lineno",
    "start_line",
    "end_line",
    "start",
    "end",
    "span",
    "range",
}


def _request_background_refresh(target: Path, db: Path, binary: str, details: dict[str, Any]) -> dict[str, str] | None:
    # A verify run can ask at several points, but only the first request can launch.
    if details.get("_background_launch_requested"):
        return code_graph_background.status_or_start(target, db)
    details["_background_launch_requested"] = True
    return code_graph_background.status_or_start(target, db, binary)


def capture_before(
    target: Path,
    run_dir: Path,
    *,
    timeout: float = 10.0,
    snapshot_timeout: float | None = None,
    defer_sync: bool = False,
) -> dict[str, Any]:
    """Sync and snapshot the index, retaining a stale baseline when possible."""
    try:
        target = target.expanduser().resolve()
        run_dir = run_dir.expanduser().resolve()
        binary = _graphtrail_bin()
        if binary is None:
            return _status("unavailable", "code graph delta unavailable: graphtrail binary not found")
        db_path = engine_paths.code_graph_db(target).path
        backup_budget = timeout if snapshot_timeout is None else snapshot_timeout
        details: dict[str, Any] = {
            "binary": binary,
            "db_path": str(db_path),
            "graphtrail_timeout_seconds": backup_budget,
            "snapshots": {},
        }
        refresh = code_graph_background.status_or_start(target, db_path) if defer_sync else None
        ready = code_graph_background.index_ready(db_path) if defer_sync else db_path.is_file()
        sync_budget = timeout
        if defer_sync and not ready:
            if refresh is None:
                refresh = _request_background_refresh(target, db_path, binary, details)
            if refresh:
                details["background_refresh"] = refresh
                if refresh["status"] != "launch_failed":
                    return _status("sync_deferred", "code graph delta deferred to background sync", **details)
                if refresh.get("failure_kind") == "detach_refused":
                    sync_budget = backup_budget
                    details["foreground_fallback"] = {
                        "reason": "detached launch refused",
                        "timeout_seconds": sync_budget,
                    }

        sync = None
        busy = bool(refresh and refresh["status"] == "busy")
        if not busy:
            stage = "initial-index" if "foreground_fallback" in details or not db_path.is_file() else "incremental-sync"
            sync = _run_graphtrail(binary, db_path, "sync", timeout=sync_budget, stage=stage, cwd=target)
            if sync["returncode"] != 0 and not sync.get("timed_out"):
                refresh = code_graph_background.status_or_start(target, db_path) if defer_sync else None
                busy = bool(refresh and refresh["status"] == "busy")
                if not busy:
                    return _status(
                        "sync_failed", f"code graph sync failed ({sync['returncode']})", sync=sync, **details
                    )
        if busy:
            details["background_refresh"] = refresh
            ready = ready or code_graph_background.index_ready(db_path)
        stale = busy or sync is None or bool(sync.get("timed_out"))
        details["sync"] = sync
        if stale and not ready:
            if busy:
                return _status("sync_deferred", "code graph delta deferred to background sync", **details)
            return _status("sync_timed_out", f"code graph sync timed out after {sync_budget:g}s", **details)
        if not db_path.is_file():
            return _status("no_database", "code graph sync did not create a database", **details)

        snapshot_path = run_dir / SNAPSHOT_NAME
        try:
            digest = _timed_snapshot(db_path, snapshot_path, backup_budget, details["snapshots"], "before")
        except TimeoutError:
            if sync and sync.get("timed_out"):
                if defer_sync and (refresh := _request_background_refresh(target, db_path, binary, details)):
                    details["background_refresh"] = refresh
                return _status("sync_timed_out", f"code graph sync timed out after {sync_budget:g}s", **details)
            return _status("capture_failed", "code graph baseline snapshot timed out", **details)
        if defer_sync and sync and sync.get("timed_out"):
            if refresh := _request_background_refresh(target, db_path, binary, details):
                details["background_refresh"] = refresh
        return {
            **details,
            "ok": True,
            "status": "captured",
            "summary": "code graph baseline captured from stale database" if stale else "code graph baseline captured",
            "before_snapshot_path": str(snapshot_path),
            "before_snapshot_sha256": digest,
            "stale_graph_used": stale,
        }
    except Exception as exc:
        return _status("capture_failed", f"code graph delta unavailable: {type(exc).__name__}: {exc}")


def _timed_snapshot(db: Path, path: Path, timeout: float, timings: dict[str, Any], stage: str) -> str:
    started = time.monotonic()
    result: dict[str, Any] = {"timeout_seconds": timeout, "timed_out": False}
    try:
        _backup_sqlite(db, path, timeout=timeout)
        return _file_sha256(path)
    except TimeoutError:
        result["timed_out"] = True
        raise
    finally:
        result["duration_seconds"] = time.monotonic() - started
        timings[stage] = result


def capture_after_and_diff(
    target: Path,
    run_dir: Path,
    before: dict[str, Any] | None,
    *,
    timeout: float = 10.0,
    sync_timeout: float | None = None,
) -> dict[str, Any]:
    """Sync after verification, diff against the snapshot, write sidecar when available."""
    try:
        target = target.expanduser().resolve()
        run_dir = run_dir.expanduser().resolve()
        before = before if isinstance(before, dict) else {}
        if before.get("status") == "unavailable":
            return _compact(_status("unavailable", str(before.get("summary") or "code graph delta unavailable")))
        if before.get("ok") is not True:
            before_sync = before.get("sync")
            db_path_value = before.get("db_path")
            if (
                sync_timeout is not None
                and db_path_value
                and (not before_sync or before_sync.get("returncode") != 0)
                and not code_graph_background.index_ready(Path(db_path_value))
                and (last_exit := code_graph_background.last_exit_reason(target))
            ):
                before["background_refresh_last_exit"] = f"last background refresh exited: {last_exit}"
            payload = _failure_payload(
                target,
                before,
                str(before.get("status") or "capture_failed"),
                graphtrail_timeout_seconds=float(before.get("graphtrail_timeout_seconds") or timeout),
            )
            return _write_and_compact(run_dir, payload)

        binary = str(before.get("binary") or "")
        db_path = Path(str(before.get("db_path") or engine_paths.code_graph_db(target).path))
        snapshot_path = Path(str(before.get("before_snapshot_path") or run_dir / SNAPSHOT_NAME))
        # A live refresh owns the write path. Read the last committed index instead.
        refresh = code_graph_background.status_or_start(target, db_path) if sync_timeout is not None else None
        stale_after = bool(refresh and refresh["status"] == "busy")
        sync = None
        if not stale_after:
            sync_budget = timeout if sync_timeout is None else sync_timeout
            sync = _run_graphtrail(binary, db_path, "sync", timeout=sync_budget, stage="incremental-sync", cwd=target)
            if sync.get("timed_out") and sync_timeout is not None:
                stale_after = True
                refresh = _request_background_refresh(target, db_path, binary, before)
            elif sync["returncode"] != 0 and sync_timeout is not None:
                refresh = code_graph_background.status_or_start(target, db_path)
                stale_after = bool(refresh and refresh["status"] == "busy")

        if stale_after:
            previous_refresh = before.get("background_refresh")
            if refresh and refresh["status"] in ("started", "busy"):
                before["background_refresh"] = {
                    **refresh,
                    "status": "started"
                    if previous_refresh and previous_refresh.get("status") == "started"
                    else refresh["status"],
                    "in_progress": True,
                }
            elif refresh:
                before["background_refresh"] = refresh
            if not code_graph_background.index_ready(db_path):
                if refresh and refresh["status"] in ("started", "busy"):
                    payload = _failure_payload(
                        target,
                        before,
                        "sync_deferred",
                        summary="code graph after-state deferred to background sync",
                        after_sync=sync,
                    )
                    return _write_and_compact(run_dir, payload, snapshot_path=snapshot_path)
                stale_after = False
            else:
                before["stale_graph_used"] = True
                before["stale_after"] = True

        if not stale_after and sync is not None and sync["returncode"] != 0:
            timed_out = sync.get("timed_out")
            reason = f"timed out after {sync_budget:g}s" if timed_out else f"failed ({sync['returncode']})"
            payload = _failure_payload(
                target,
                before,
                "sync_timed_out" if timed_out else "sync_failed",
                summary=f"code graph delta unavailable: incremental sync {reason}",
                after_sync=sync,
            )
            return _write_and_compact(run_dir, payload, snapshot_path=snapshot_path)

        after_snapshot_path = run_dir / SNAPSHOT_AFTER_NAME
        try:
            after_snapshot_sha256 = _timed_snapshot(
                db_path, after_snapshot_path, timeout, before.setdefault("snapshots", {}), "after"
            )
        except TimeoutError:
            payload = _failure_payload(
                target, before, "capture_failed", summary="code graph final snapshot timed out", after_sync=sync
            )
            return _write_and_compact(run_dir, payload, snapshot_path=snapshot_path)
        diff = _run_graphtrail(
            binary,
            db_path,
            "diff",
            "--before",
            str(snapshot_path),
            "--after",
            str(after_snapshot_path),
            timeout=timeout,
            json_output=True,
            stage="diff",
        )
        if diff.get("timed_out"):
            payload = _failure_payload(
                target,
                before,
                "diff_timed_out",
                summary=f"code graph delta unavailable: graphtrail diff timed out after {timeout:g}s",
                after_sync=sync,
                diff=diff,
                graphtrail_timeout_seconds=timeout,
            )
            return _write_and_compact(
                run_dir, payload, snapshot_path=snapshot_path, after_snapshot_path=after_snapshot_path
            )
        if diff["returncode"] != 0:
            payload = _failure_payload(
                target,
                before,
                "diff_failed",
                summary=f"code graph delta unavailable: graphtrail diff failed ({diff['returncode']})",
                after_sync=sync,
                diff=diff,
            )
            return _write_and_compact(
                run_dir, payload, snapshot_path=snapshot_path, after_snapshot_path=after_snapshot_path
            )
        try:
            diff_payload = json.loads(diff["stdout"])
        except json.JSONDecodeError as exc:
            payload = _failure_payload(
                target,
                before,
                "diff_malformed",
                summary=f"code graph delta unavailable: graphtrail diff returned malformed JSON: {exc.msg}",
                after_sync=sync,
                diff=diff,
            )
            return _write_and_compact(
                run_dir, payload, snapshot_path=snapshot_path, after_snapshot_path=after_snapshot_path
            )
        if not isinstance(diff_payload, dict):
            payload = _failure_payload(
                target,
                before,
                "diff_malformed",
                summary="code graph delta unavailable: graphtrail diff JSON is not an object",
                after_sync=sync,
                diff=diff,
            )
            return _write_and_compact(
                run_dir, payload, snapshot_path=snapshot_path, after_snapshot_path=after_snapshot_path
            )

        changed_symbols = _changed_symbols(diff_payload)
        # graphtrail names its counts block "summary" (locked by the diff golden fixture).
        counts_value = diff_payload.get("summary")
        raw_counts: dict[str, Any] = counts_value if isinstance(counts_value, dict) else {}
        edge_churn = _edge_churn(diff_payload)
        payload = {
            "ok": True,
            "status": "ok",
            "target": str(target),
            "graphtrail_timeout_seconds": timeout,
            "summary": _summary("ok", raw_counts, edge_churn, len(changed_symbols))
            + (" (stale after-state)" if stale_after else ""),
            "raw_counts": raw_counts,
            "changed_symbols": changed_symbols,
            "changed_symbol_count": len(changed_symbols),
            "changed_symbols_truncated": _symbol_count(diff_payload) > CHANGED_SYMBOL_LIMIT,
            **_code_reference_node_metadata(diff_payload),
            "edge_churn": edge_churn,
            "line_insensitive_edge_churn": edge_churn,
            "before_snapshot_path": str(snapshot_path),
            "db_path": str(db_path),
            "commands": {"before_sync": before.get("sync"), "after_sync": sync, "diff": diff},
            "attestations": {
                "before_snapshot_sha256": before.get("before_snapshot_sha256"),
                "after_snapshot_sha256": after_snapshot_sha256,
                "diff_stdout_sha256": _sha256_text(diff["stdout"]),
            },
            "stale_graph_used": before.get("stale_graph_used") is True,
            **_capture_metadata(before),
        }
        return _write_and_compact(
            run_dir, payload, snapshot_path=snapshot_path, after_snapshot_path=after_snapshot_path
        )
    except Exception as exc:
        payload = _status("capture_failed", f"code graph delta unavailable: {type(exc).__name__}: {exc}")
        try:
            return _write_and_compact(run_dir, payload)
        except Exception:
            return _compact(payload)


def _compact_summary(delta: dict[str, Any] | None) -> str:
    """Return a one-line summary for markdown; never raise."""
    try:
        if not isinstance(delta, dict):
            return "code graph delta unavailable"
        return str(delta.get("summary") or _summary(str(delta.get("status") or "unknown"), {}, 0, 0))
    except Exception:
        return "code graph delta unavailable"


def _graphtrail_bin() -> str | None:
    return component_bins.resolve("brigade-code-graph")


def _run_graphtrail(
    binary: str,
    db_path: Path,
    command: str,
    *extra: str,
    timeout: float,
    json_output: bool = False,
    stage: str | None = None,
    cwd: Path | None = None,
) -> dict[str, Any]:
    """Run one engine command. ``cwd`` is the repository it indexes.

    `sync` indexes its working directory when given no root, so callers pass the
    target they are working on. Without one, the root is derived from where the
    index lives, which fails for an index placed outside the repository.
    """
    # `graphtrail sync` rejects --json; only diff-style subcommands accept it.
    argv = [binary, "--db", str(db_path), command, *extra]
    if json_output:
        argv.append("--json")
    started = time.monotonic()
    try:
        completed = subprocess.run(
            argv,
            cwd=cwd or engine_paths.code_graph_repo_root(db_path),
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            timeout=timeout,
        )
        return _command_result(
            argv=argv,
            returncode=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
            timed_out=False,
            duration_seconds=time.monotonic() - started,
            timeout=timeout,
            stage=stage,
        )
    except FileNotFoundError:
        return _command_result(
            argv=argv,
            returncode=127,
            stdout="",
            stderr="command not found",
            timed_out=False,
            duration_seconds=time.monotonic() - started,
            timeout=timeout,
            stage=stage,
        )
    except subprocess.TimeoutExpired as exc:
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        if not stderr:
            stderr = f"timeout after {timeout:g}s"
        return _command_result(
            argv=argv,
            returncode=124,
            stdout=exc.stdout if isinstance(exc.stdout, str) else "",
            stderr=stderr,
            timed_out=True,
            duration_seconds=time.monotonic() - started,
            timeout=timeout,
            stage=stage,
        )


def _command_result(
    *,
    argv: list[str],
    returncode: int,
    stdout: str,
    stderr: str,
    timed_out: bool,
    duration_seconds: float,
    timeout: float,
    stage: str | None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "argv": argv,
        "returncode": returncode,
        "stdout": stdout,
        "stderr": stderr,
        "timed_out": timed_out,
        "duration_seconds": duration_seconds,
        "graphtrail_timeout_seconds": timeout,
    }
    if stage:
        result["stage"] = stage
    return result


def _backup_sqlite(source: Path, destination: Path, *, timeout: float | None = None) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.unlink(missing_ok=True)
    source_uri = f"file:{source}?mode=ro"
    started = time.monotonic()

    def progress(status: int, _remaining: int, _total: int) -> None:
        if status in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            raise TimeoutError("code graph snapshot is busy")
        if timeout is not None and time.monotonic() - started >= timeout:
            raise TimeoutError(f"timeout after {timeout:g}s")

    try:
        busy_timeout = 5.0 if timeout is None else 0.0
        with sqlite3.connect(source_uri, uri=True, timeout=busy_timeout) as source_conn:
            with sqlite3.connect(destination, timeout=busy_timeout) as dest_conn:
                if timeout is None:
                    source_conn.backup(dest_conn)
                else:
                    source_conn.backup(dest_conn, pages=32, progress=progress)
                    if time.monotonic() - started >= timeout:
                        raise TimeoutError(f"timeout after {timeout:g}s")
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def _failure_payload(
    target: Path,
    before: dict[str, Any],
    status: str,
    *,
    summary: str | None = None,
    after_sync: dict[str, Any] | None = None,
    diff: dict[str, Any] | None = None,
    graphtrail_timeout_seconds: float | None = None,
) -> dict[str, Any]:
    timeout_value = graphtrail_timeout_seconds
    if timeout_value is None:
        raw_timeout = before.get("graphtrail_timeout_seconds")
        timeout_value = float(raw_timeout) if isinstance(raw_timeout, (int, float)) else None
    payload: dict[str, Any] = {
        "ok": False,
        "status": status,
        "target": str(target),
        "summary": summary or str(before.get("summary") or f"code graph delta unavailable: {status}"),
        "before_snapshot_path": before.get("before_snapshot_path"),
        "db_path": before.get("db_path"),
        "raw_counts": {},
        "changed_symbols": [],
        "changed_symbol_count": 0,
        "edge_churn": 0,
        "commands": {"before_sync": before.get("sync"), "after_sync": after_sync, "diff": diff},
        "attestations": {
            "before_snapshot_sha256": before.get("before_snapshot_sha256"),
            "after_snapshot_sha256": None,
            "diff_stdout_sha256": _sha256_text(diff.get("stdout", "")) if isinstance(diff, dict) else None,
        },
        "stale_graph_used": before.get("stale_graph_used") is True,
    }
    payload.update(_capture_metadata(before))
    if timeout_value is not None:
        # frozen identifier: see docs/phase-engine-rename.md; persisted or signed contract.
        payload["graphtrail_timeout_seconds"] = timeout_value
    return payload


def _write_and_compact(
    run_dir: Path,
    payload: dict[str, Any],
    *,
    snapshot_path: Path | None = None,
    after_snapshot_path: Path | None = None,
) -> dict[str, Any]:
    deleted = _delete_snapshot(snapshot_path or _payload_snapshot_path(payload))
    deleted = _delete_snapshot(after_snapshot_path) and deleted if after_snapshot_path else deleted
    payload["snapshot_deleted"] = deleted
    sidecar = run_dir / SIDECAR_NAME
    _write_json(sidecar, payload)
    compact = _compact(payload)
    compact["sidecar_path"] = str(sidecar)
    compact["sidecar_sha256"] = _file_sha256(sidecar)
    return compact


def _capture_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        key: payload[key]
        for key in (
            "snapshots",
            "background_refresh",
            "background_refresh_last_exit",
            "foreground_fallback",
            "stale_after",
        )
        if payload.get(key) is not None
    }


def _compact(payload: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {
        "status": payload.get("status", "unknown"),
        "ok": bool(payload.get("ok")),
        "summary": _compact_summary(payload),
        "raw_counts": payload.get("raw_counts") if isinstance(payload.get("raw_counts"), dict) else {},
        "edge_churn": int(payload.get("edge_churn") or 0),
        "changed_symbols": payload.get("changed_symbols") if isinstance(payload.get("changed_symbols"), list) else [],
        "changed_symbol_count": int(payload.get("changed_symbol_count") or 0),
        **_code_reference_node_metadata(payload),
    }
    timeout = payload.get("graphtrail_timeout_seconds")
    if isinstance(timeout, (int, float)) and not isinstance(timeout, bool):
        # frozen identifier: see docs/phase-engine-rename.md; persisted or signed contract.
        compact["graphtrail_timeout_seconds"] = float(timeout)
    compact.update(_capture_metadata(payload))
    compact["stale_graph_used"] = payload.get("stale_graph_used") is True
    commands = payload.get("commands")
    if isinstance(commands, dict):
        compact["commands"] = commands
    return compact


def _code_reference_node_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep canonical nodes plus their valid pre-cap candidate accounting."""
    existing = payload.get("code_reference_nodes")
    if isinstance(existing, list):
        normalized_nodes = [code_references.normalize_delta_node(value) for value in existing]
        malformed_candidate = any(node is None for node in normalized_nodes)
        compact_nodes = normalized_nodes
        compact_nodes = [node for node in compact_nodes if node is not None]
        compact_nodes.sort(key=lambda node: json.dumps(node, sort_keys=True, separators=(",", ":")))
        declared_total = payload.get("code_reference_nodes_total")
        declared_truncated = payload.get("code_reference_nodes_truncated")
        if (
            not malformed_candidate
            and isinstance(declared_total, int)
            and not isinstance(declared_total, bool)
            and (
                (declared_truncated is False and declared_total == len(compact_nodes))
                or (
                    declared_truncated is True
                    and len(compact_nodes) == CODE_REFERENCE_NODE_LIMIT
                    and declared_total > CODE_REFERENCE_NODE_LIMIT
                )
            )
        ):
            total = declared_total
        else:
            total = len(compact_nodes)
        return {
            "code_reference_nodes": compact_nodes[:CODE_REFERENCE_NODE_LIMIT],
            "code_reference_nodes_total": total,
            "code_reference_nodes_truncated": total > CODE_REFERENCE_NODE_LIMIT,
        }
    nodes: list[dict[str, Any]] = []
    for source_key, change_kind in (
        ("added_nodes", "added"),
        ("changed_nodes", "changed"),
        ("removed_nodes", "removed"),
    ):
        values = payload.get(source_key)
        if not isinstance(values, list):
            continue
        for value in values:
            normalized = code_references.normalize_delta_node(value, change_kind)
            if normalized is not None:
                nodes.append(normalized)
    nodes.sort(key=lambda node: json.dumps(node, sort_keys=True, separators=(",", ":")))
    return {
        "code_reference_nodes": nodes[:CODE_REFERENCE_NODE_LIMIT],
        "code_reference_nodes_total": len(nodes),
        "code_reference_nodes_truncated": len(nodes) > CODE_REFERENCE_NODE_LIMIT,
    }


def _status(status: str, summary: str, **extra: Any) -> dict[str, Any]:
    payload = {"ok": False, "status": status, "summary": summary, "stale_graph_used": False}
    payload.update(extra)
    return payload


def _summary(status: str, counts: dict[str, Any], edge_churn: int, changed_symbol_count: int) -> str:
    if status != "ok":
        return f"code graph delta unavailable: {status}"
    parts = [
        "code graph delta: ok",
        f"changed_symbols={changed_symbol_count}",
        f"edge_churn={edge_churn}",
    ]
    for key in ("added_nodes", "removed_nodes", "changed_nodes", "added_edges", "removed_edges"):
        value = counts.get(key)
        if isinstance(value, int):
            parts.append(f"{key}={value}")
    return " ".join(parts)


def _changed_symbols(payload: dict[str, Any]) -> list[str]:
    symbols: list[str] = []
    for value in _symbol_values(payload):
        if value not in symbols:
            symbols.append(value)
        if len(symbols) >= CHANGED_SYMBOL_LIMIT:
            break
    return symbols


def _symbol_count(payload: dict[str, Any]) -> int:
    seen: set[str] = set()
    for value in _symbol_values(payload):
        seen.add(value)
    return len(seen)


def _symbol_values(payload: dict[str, Any]) -> list[str]:
    # graphtrail diff emits changed/added/removed node lists of DiffNode objects
    # keyed by qualified_name (locked by the diff golden fixture).
    result: list[str] = []
    for key in ("changed_nodes", "added_nodes", "removed_nodes"):
        values = payload.get(key)
        if not isinstance(values, list):
            continue
        for item in values:
            if isinstance(item, str):
                result.append(item)
            elif isinstance(item, dict):
                name = item.get("qualified_name") or item.get("name") or item.get("symbol")
                if isinstance(name, str):
                    result.append(name)
    return result


def _edge_churn(payload: dict[str, Any]) -> int:
    added_value = payload.get("added_edges")
    removed_value = payload.get("removed_edges")
    if not isinstance(added_value, list) and not isinstance(removed_value, list):
        return _count_from_payload(payload, "added_edges") + _count_from_payload(payload, "removed_edges")
    added = _edge_fingerprints(added_value)
    removed = _edge_fingerprints(removed_value)
    for fingerprint in list(added):
        pairs = min(added.get(fingerprint, 0), removed.get(fingerprint, 0))
        if pairs:
            added[fingerprint] -= pairs
            removed[fingerprint] -= pairs
    return sum(max(0, count) for count in added.values()) + sum(max(0, count) for count in removed.values())


def _edge_fingerprints(value: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    if not isinstance(value, list):
        return counts
    for item in value:
        fingerprint = json.dumps(_strip_line_keys(item), sort_keys=True, separators=(",", ":"), default=str)
        counts[fingerprint] = counts.get(fingerprint, 0) + 1
    return counts


def _strip_line_keys(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _strip_line_keys(item) for key, item in value.items() if key not in LINE_KEYS}
    if isinstance(value, list):
        return [_strip_line_keys(item) for item in value]
    return value


def _count_from_payload(payload: dict[str, Any], key: str) -> int:
    counts = payload.get("summary")
    if isinstance(counts, dict) and isinstance(counts.get(key), int):
        return int(counts[key])
    value = payload.get(key)
    return int(value) if isinstance(value, int) else 0


def _payload_snapshot_path(payload: dict[str, Any]) -> Path | None:
    value = payload.get("before_snapshot_path")
    return Path(value) if isinstance(value, str) and value else None


def _delete_snapshot(path: Path | None) -> bool:
    if path is None:
        return False
    try:
        path.unlink(missing_ok=True)
        return not path.exists()
    except OSError:
        return False


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
