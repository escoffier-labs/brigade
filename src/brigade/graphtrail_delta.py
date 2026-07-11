"""Fail-open GraphTrail delta snapshots for verification receipts."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path
from typing import Any

SNAPSHOT_NAME = "graphtrail-before.db"
SNAPSHOT_AFTER_NAME = "graphtrail-after.db"
SIDECAR_NAME = "graph-delta.json"
CHANGED_SYMBOL_LIMIT = 20
STALE_SOURCE_LIMIT = 10
SOURCE_SUFFIXES = {
    ".c",
    ".cc",
    ".cpp",
    ".cs",
    ".go",
    ".java",
    ".js",
    ".jsx",
    ".kt",
    ".mjs",
    ".py",
    ".rb",
    ".rs",
    ".swift",
    ".ts",
    ".tsx",
}
SKIP_DIRS = {
    ".brigade",
    ".git",
    ".graphtrail",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "node_modules",
}
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
    """Snapshot an existing GraphTrail sqlite DB; return status data, never raise."""
    try:
        target = target.expanduser().resolve()
        run_dir = run_dir.expanduser().resolve()
        db_path = target / ".graphtrail" / "graphtrail.db"
        refresh = _refresh_required(target, db_path)
        if refresh is not None:
            return refresh
        binary = _graphtrail_bin()
        if binary is None:
            return _status(
                "unavailable",
                "code graph delta unavailable: graphtrail binary not found",
                db_path=str(db_path),
                refresh_plan_command="brigade search refresh plan",
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
    """Diff the existing database against the snapshot, writing a sidecar when available."""
    try:
        target = target.expanduser().resolve()
        run_dir = run_dir.expanduser().resolve()
        before = before if isinstance(before, dict) else {}
        if before.get("status") == "unavailable":
            return _compact(_status("unavailable", str(before.get("summary") or "code graph delta unavailable")))
        if before.get("ok") is not True:
            payload = _failure_payload(target, before, str(before.get("status") or "capture_failed"))
            return _write_and_compact(run_dir, payload)

        binary = str(before.get("binary") or "")
        db_path = Path(str(before.get("db_path") or target / ".graphtrail" / "graphtrail.db"))
        snapshot_path = Path(str(before.get("before_snapshot_path") or run_dir / SNAPSHOT_NAME))
        refresh = _refresh_required(target, db_path)
        if refresh is not None:
            payload = _failure_payload(target, refresh, "refresh_required")
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
        )
        if diff["returncode"] != 0:
            payload = _failure_payload(
                target,
                before,
                "diff_failed",
                summary=f"code graph delta unavailable: graphtrail diff failed ({diff['returncode']})",
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
            "summary": _summary("ok", raw_counts, edge_churn, len(changed_symbols)),
            "raw_counts": raw_counts,
            "changed_symbols": changed_symbols,
            "changed_symbol_count": len(changed_symbols),
            "changed_symbols_truncated": _symbol_count(diff_payload) > CHANGED_SYMBOL_LIMIT,
            "edge_churn": edge_churn,
            "line_insensitive_edge_churn": edge_churn,
            "before_snapshot_path": str(snapshot_path),
            "db_path": str(db_path),
            "commands": {"diff": diff},
            "attestations": {
                "before_snapshot_sha256": before.get("before_snapshot_sha256"),
                "after_snapshot_sha256": after_snapshot_sha256,
                "diff_stdout_sha256": _sha256_text(diff["stdout"]),
            },
        }
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


def _refresh_required(target: Path, db_path: Path) -> dict[str, Any] | None:
    if not db_path.is_file():
        return _refresh_required_payload(
            target,
            db_path,
            "graphtrail_database_missing",
            "code graph delta refresh required: graphtrail database missing",
        )
    newer_files = _newer_source_files(target, db_path)
    if newer_files:
        return _refresh_required_payload(
            target,
            db_path,
            "graphtrail_database_stale",
            "code graph delta refresh required: graphtrail database is stale",
            newer_files=newer_files,
        )
    return None


def _refresh_required_payload(
    target: Path,
    db_path: Path,
    reason: str,
    summary: str,
    *,
    newer_files: list[str] | None = None,
) -> dict[str, Any]:
    payload = _status(
        "refresh_required",
        summary,
        target=str(target),
        db_path=str(db_path),
        reason=reason,
        refresh_required=True,
        refresh_plan_command="brigade search refresh plan",
        refresh_command=["graphtrail", "sync", str(target)],
        raw_counts={},
        changed_symbols=[],
        changed_symbol_count=0,
        edge_churn=0,
    )
    if newer_files is not None:
        payload["newer_files"] = newer_files
        payload["newer_files_truncated"] = len(newer_files) >= STALE_SOURCE_LIMIT
    return payload


def _newer_source_files(target: Path, db_path: Path) -> list[str]:
    try:
        db_mtime = db_path.stat().st_mtime
    except OSError:
        return []
    newer: list[str] = []
    try:
        walker = os.walk(target)
        for root, dirs, files in walker:
            dirs[:] = [dirname for dirname in dirs if dirname not in SKIP_DIRS]
            root_path = Path(root)
            for filename in files:
                path = root_path / filename
                if path.suffix not in SOURCE_SUFFIXES:
                    continue
                try:
                    if path.stat().st_mtime <= db_mtime:
                        continue
                    newer.append(path.relative_to(target).as_posix())
                except OSError:
                    continue
                if len(newer) >= STALE_SOURCE_LIMIT:
                    return newer
    except OSError:
        return []
    return newer


def _run_graphtrail(
    binary: str, db_path: Path, command: str, *extra: str, timeout: float, json_output: bool = False
) -> dict[str, Any]:
    # `graphtrail sync` rejects --json; only diff-style subcommands accept it.
    argv = [binary, "--db", str(db_path), command, *extra]
    if json_output:
        argv.append("--json")
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
        return {
            "argv": argv,
            "returncode": completed.returncode,
            "stdout": completed.stdout or "",
            "stderr": completed.stderr or "",
            "timed_out": False,
        }
    except FileNotFoundError:
        return {"argv": argv, "returncode": 127, "stdout": "", "stderr": "command not found", "timed_out": False}
    except subprocess.TimeoutExpired as exc:
        return {
            "argv": argv,
            "returncode": 124,
            "stdout": exc.stdout if isinstance(exc.stdout, str) else "",
            "stderr": exc.stderr if isinstance(exc.stderr, str) else f"timeout after {timeout:g}s",
            "timed_out": True,
        }


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
) -> dict[str, Any]:
    commands: dict[str, Any] = {}
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
        "commands": commands,
        "attestations": {
            "before_snapshot_sha256": before.get("before_snapshot_sha256"),
            "after_snapshot_sha256": None,
            "diff_stdout_sha256": _sha256_text(diff.get("stdout", "")) if isinstance(diff, dict) else None,
        },
    }
    if after_sync is not None:
        commands["after_sync"] = after_sync
    if diff is not None:
        commands["diff"] = diff
    for key in (
        "reason",
        "refresh_required",
        "refresh_plan_command",
        "refresh_command",
        "newer_files",
        "newer_files_truncated",
    ):
        if key in before:
            payload[key] = before[key]
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
    compact = {
        "status": payload.get("status", "unknown"),
        "ok": bool(payload.get("ok")),
        "summary": _compact_summary(payload),
        "raw_counts": payload.get("raw_counts") if isinstance(payload.get("raw_counts"), dict) else {},
        "edge_churn": int(payload.get("edge_churn") or 0),
        "changed_symbols": payload.get("changed_symbols") if isinstance(payload.get("changed_symbols"), list) else [],
        "changed_symbol_count": int(payload.get("changed_symbol_count") or 0),
    }
    for key in ("refresh_required", "refresh_plan_command", "reason"):
        if key in payload:
            compact[key] = payload[key]
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
