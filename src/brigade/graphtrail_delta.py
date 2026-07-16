"""Fail-open GraphTrail delta snapshots for verification receipts."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

SNAPSHOT_NAME = "graphtrail-before.db"
SNAPSHOT_AFTER_NAME = "graphtrail-after.db"
SIDECAR_NAME = "graph-delta.json"
CHANGED_SYMBOL_LIMIT = 20
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


def capture_before(target: Path, run_dir: Path, *, timeout: float = 10.0) -> dict[str, Any]:
    """Sync GraphTrail and snapshot its sqlite DB; return status data, never raise."""
    try:
        target = target.expanduser().resolve()
        run_dir = run_dir.expanduser().resolve()
        binary = _graphtrail_bin()
        if binary is None:
            return _status("unavailable", "code graph delta unavailable: graphtrail binary not found")
        db_path = target / ".graphtrail" / "graphtrail.db"
        sync_stage = "incremental-sync" if db_path.is_file() else "initial-index"
        sync = _run_graphtrail(binary, db_path, "sync", timeout=timeout, stage=sync_stage)
        if sync.get("timed_out"):
            if db_path.is_file():
                snapshot_path = run_dir / SNAPSHOT_NAME
                _backup_sqlite(db_path, snapshot_path)
                return {
                    "ok": True,
                    "status": "captured",
                    "summary": "code graph baseline captured from stale database",
                    "binary": binary,
                    "db_path": str(db_path),
                    "before_snapshot_path": str(snapshot_path),
                    "before_snapshot_sha256": _file_sha256(snapshot_path),
                    "stale_graph_used": True,
                    "graphtrail_timeout_seconds": timeout,
                    "sync": sync,
                }
            return _status(
                "sync_timed_out",
                f"code graph delta unavailable: graphtrail {sync_stage} sync timed out after {timeout:g}s",
                binary=binary,
                db_path=str(db_path),
                sync=sync,
                graphtrail_timeout_seconds=timeout,
            )
        if sync["returncode"] != 0:
            return _status(
                "sync_failed",
                f"code graph delta unavailable: graphtrail sync failed ({sync['returncode']})",
                binary=binary,
                db_path=str(db_path),
                sync=sync,
                graphtrail_timeout_seconds=timeout,
            )
        if not db_path.is_file():
            return _status(
                "no_database",
                "code graph delta unavailable: graphtrail sync did not create a database",
                binary=binary,
                db_path=str(db_path),
                sync=sync,
            )
        snapshot_path = run_dir / SNAPSHOT_NAME
        _backup_sqlite(db_path, snapshot_path)
        return {
            "ok": True,
            "status": "captured",
            "summary": "code graph baseline captured",
            "binary": binary,
            "db_path": str(db_path),
            "before_snapshot_path": str(snapshot_path),
            "before_snapshot_sha256": _file_sha256(snapshot_path),
            "graphtrail_timeout_seconds": timeout,
            "sync": sync,
        }
    except BaseException as exc:
        return _status("capture_failed", f"code graph delta unavailable: {type(exc).__name__}: {exc}")


def capture_after_and_diff(
    target: Path,
    run_dir: Path,
    before: dict[str, Any] | None,
    *,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """Sync after verification, diff against the snapshot, write sidecar when available."""
    try:
        target = target.expanduser().resolve()
        run_dir = run_dir.expanduser().resolve()
        before = before if isinstance(before, dict) else {}
        if before.get("status") == "unavailable":
            return _compact(_status("unavailable", str(before.get("summary") or "code graph delta unavailable")))
        if before.get("ok") is not True:
            payload = _failure_payload(
                target,
                before,
                str(before.get("status") or "capture_failed"),
                graphtrail_timeout_seconds=float(before.get("graphtrail_timeout_seconds") or timeout),
            )
            return _write_and_compact(run_dir, payload)

        binary = str(before.get("binary") or "")
        db_path = Path(str(before.get("db_path") or target / ".graphtrail" / "graphtrail.db"))
        snapshot_path = Path(str(before.get("before_snapshot_path") or run_dir / SNAPSHOT_NAME))
        sync = _run_graphtrail(binary, db_path, "sync", timeout=timeout, stage="incremental-sync")
        if sync.get("timed_out"):
            payload = _failure_payload(
                target,
                before,
                "sync_timed_out",
                summary=f"code graph delta unavailable: graphtrail incremental-sync timed out after {timeout:g}s",
                after_sync=sync,
                graphtrail_timeout_seconds=timeout,
            )
            return _write_and_compact(run_dir, payload, snapshot_path=snapshot_path)
        if sync["returncode"] != 0:
            payload = _failure_payload(
                target,
                before,
                "sync_failed",
                summary=f"code graph delta unavailable: graphtrail sync failed ({sync['returncode']})",
                after_sync=sync,
                graphtrail_timeout_seconds=timeout,
            )
            return _write_and_compact(run_dir, payload, snapshot_path=snapshot_path)

        after_snapshot_path = run_dir / SNAPSHOT_AFTER_NAME
        _backup_sqlite(db_path, after_snapshot_path)
        after_snapshot_sha256 = _file_sha256(after_snapshot_path)
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
            "summary": _summary("ok", raw_counts, edge_churn, len(changed_symbols)),
            "raw_counts": raw_counts,
            "changed_symbols": changed_symbols,
            "changed_symbol_count": len(changed_symbols),
            "changed_symbols_truncated": _symbol_count(diff_payload) > CHANGED_SYMBOL_LIMIT,
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
        }
        if before.get("stale_graph_used") is True:
            payload["stale_graph_used"] = True
        return _write_and_compact(
            run_dir, payload, snapshot_path=snapshot_path, after_snapshot_path=after_snapshot_path
        )
    except BaseException as exc:
        payload = _status("capture_failed", f"code graph delta unavailable: {type(exc).__name__}: {exc}")
        try:
            return _write_and_compact(run_dir, payload)
        except BaseException:
            return _compact(payload)


def _compact_summary(delta: dict[str, Any] | None) -> str:
    """Return a one-line summary for markdown; never raise."""
    try:
        if not isinstance(delta, dict):
            return "code graph delta unavailable"
        return str(delta.get("summary") or _summary(str(delta.get("status") or "unknown"), {}, 0, 0))
    except BaseException:
        return "code graph delta unavailable"


def _graphtrail_bin() -> str | None:
    override = os.environ.get("GRAPHTRAIL_BIN")
    if override and Path(override).is_file():
        return override
    found = shutil.which("graphtrail")
    if found:
        return found
    fallback = Path.home() / ".cargo" / "bin" / "graphtrail"
    return str(fallback) if fallback.is_file() else None


def _run_graphtrail(
    binary: str,
    db_path: Path,
    command: str,
    *extra: str,
    timeout: float,
    json_output: bool = False,
    stage: str | None = None,
) -> dict[str, Any]:
    # `graphtrail sync` rejects --json; only diff-style subcommands accept it.
    argv = [binary, "--db", str(db_path), command, *extra]
    if json_output:
        argv.append("--json")
    started = time.monotonic()
    try:
        completed = subprocess.run(
            argv,
            cwd=db_path.parent.parent if db_path.parent.name == ".graphtrail" else None,
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


def _backup_sqlite(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.unlink(missing_ok=True)
    source_uri = f"file:{source}?mode=ro"
    with sqlite3.connect(source_uri, uri=True) as source_conn:
        with sqlite3.connect(destination) as dest_conn:
            source_conn.backup(dest_conn)


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
    }
    if timeout_value is not None:
        payload["graphtrail_timeout_seconds"] = timeout_value
    if before.get("stale_graph_used") is True:
        payload["stale_graph_used"] = True
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


def _compact(payload: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {
        "status": payload.get("status", "unknown"),
        "ok": bool(payload.get("ok")),
        "summary": _compact_summary(payload),
        "raw_counts": payload.get("raw_counts") if isinstance(payload.get("raw_counts"), dict) else {},
        "edge_churn": int(payload.get("edge_churn") or 0),
        "changed_symbols": payload.get("changed_symbols") if isinstance(payload.get("changed_symbols"), list) else [],
        "changed_symbol_count": int(payload.get("changed_symbol_count") or 0),
    }
    timeout = payload.get("graphtrail_timeout_seconds")
    if isinstance(timeout, (int, float)) and not isinstance(timeout, bool):
        compact["graphtrail_timeout_seconds"] = float(timeout)
    if payload.get("stale_graph_used") is True:
        compact["stale_graph_used"] = True
    commands = payload.get("commands")
    if isinstance(commands, dict):
        compact["commands"] = commands
    return compact


def _status(status: str, summary: str, **extra: Any) -> dict[str, Any]:
    payload = {"ok": False, "status": status, "summary": summary}
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
