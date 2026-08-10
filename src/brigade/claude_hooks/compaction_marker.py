"""One-shot compaction restore markers (issue #736).

Markers live under the user cache directory (never the repo), keyed by a hash of
session id plus workspace path. Lifecycle:

1. Compaction (``PreCompact``) writes a versioned ``pending`` record.
2. A later inject-capable event atomically renames ``pending`` to a per-process
   ``claimed`` file, renders the #735 envelope, then deletes the claim after a
   successful stdout write.
3. Rendering or stdout failure returns the claim to ``pending`` so the next
   event retries.
4. True ``SessionStart`` (and Claude's inject-capable ``source=compact`` path)
   clears both states for the session-workspace key.

Hot path: callers ``stat`` for presence; absent markers skip store opens and
subprocesses.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import localio
from ..component_paths import cache_root

SCHEMA = "brigade.claude-hooks.compaction-marker.v1"
SCHEMA_VERSION = 1
MARKER_DIRNAME = "compaction"
STALE_CLAIM_SECONDS = 60
CLAIM_KEY = "_brigade_compaction_claim"
RESTORE_PREFIX = "[Brigade] Context was compacted; the workflow rules below are restored."


@dataclass(frozen=True)
class CompactionClaim:
    """A process-exclusive claim on a pending compaction marker."""

    key: str
    path: Path
    record: dict[str, Any]


def markers_root(*, env: dict[str, str] | None = None) -> Path:
    """Return ``$cache/brigade/claude-hooks/compaction`` (never under a repo)."""
    root = Path(cache_root(env=env)) / "brigade" / "claude-hooks" / MARKER_DIRNAME
    return root


def marker_key(session_id: str, workspace: Path) -> str:
    """Stable key distinguishing concurrent sessions and workspaces."""
    workspace_key = str(workspace.expanduser().resolve(strict=False))
    return localio.stable_hash({"session_id": session_id, "workspace": workspace_key})


def key_dir(key: str, *, env: dict[str, str] | None = None) -> Path:
    return markers_root(env=env) / key


def pending_path(key: str, *, env: dict[str, str] | None = None) -> Path:
    return key_dir(key, env=env) / "pending.json"


def cheap_workspace_root(cwd: object) -> Path | None:
    """Locate a Brigade-wired root with stats only (no config parse, no store)."""
    if not isinstance(cwd, str) or not cwd.strip():
        return None
    try:
        current = Path(cwd).expanduser().resolve(strict=False)
    except OSError:
        return None
    if not current.is_dir():
        current = current.parent
    for candidate in (current, *current.parents):
        try:
            if (candidate / ".brigade" / "config.json").is_file():
                return candidate
        except OSError:
            continue
    return None


def marker_present(key: str, *, env: dict[str, str] | None = None) -> bool:
    """Fast presence check: pending or any claim file under the key directory."""
    root = key_dir(key, env=env)
    try:
        if not root.is_dir() or root.is_symlink():
            return False
        if pending_path(key, env=env).is_file():
            return True
        return any(root.glob("claimed-*.json"))
    except OSError:
        return False


def _chmod_private(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        return


def _ensure_key_dir(key: str, *, env: dict[str, str] | None = None) -> Path:
    root = markers_root(env=env)
    _reject_symlink(root.parent.parent)  # brigade/
    _reject_symlink(root.parent)  # claude-hooks/
    _reject_symlink(root)
    try:
        root.mkdir(parents=True, exist_ok=True)
        os.chmod(root, 0o700)
    except OSError:
        pass
    _reject_symlink(root)
    path = key_dir(key, env=env)
    _reject_symlink(path)
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    _reject_symlink(path)
    return path


def _reject_symlink(path: Path) -> None:
    if path.exists() and path.is_symlink():
        raise OSError(f"compaction marker path must not be a symlink: {path}")


def _record(
    *,
    session_id: str,
    workspace: Path,
    trigger: str,
    trigger_detail: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "session_id": session_id,
        "workspace": str(workspace.expanduser().resolve(strict=False)),
        "created_at": localio.utc_now_iso(),
        "trigger": trigger,
    }
    if trigger_detail:
        payload["trigger_detail"] = trigger_detail
    return payload


def write_pending(
    session_id: str,
    workspace: Path,
    *,
    trigger: str = "PreCompact",
    trigger_detail: str | None = None,
    env: dict[str, str] | None = None,
) -> Path:
    """Publish or replace the pending marker for this session-workspace key."""
    key = marker_key(session_id, workspace)
    _ensure_key_dir(key, env=env)
    path = pending_path(key, env=env)
    localio.write_text_atomic(
        path,
        json.dumps(
            _record(
                session_id=session_id,
                workspace=workspace,
                trigger=trigger,
                trigger_detail=trigger_detail,
            ),
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )
    _chmod_private(path)
    return path


def clear_markers(session_id: str, workspace: Path, *, env: dict[str, str] | None = None) -> None:
    """Remove pending and claimed markers for the session-workspace key."""
    key = marker_key(session_id, workspace)
    root = key_dir(key, env=env)
    if not root.exists():
        return
    if root.is_symlink():
        return
    for path in (pending_path(key, env=env), *sorted(root.glob("claimed-*.json"))):
        try:
            if path.is_symlink():
                continue
            path.unlink(missing_ok=True)
        except OSError:
            continue
    try:
        root.rmdir()
    except OSError:
        return


def _read_record(path: Path) -> dict[str, Any] | None:
    payload = localio.read_json_dict(path)
    if payload is None:
        return None
    if payload.get("schema") != SCHEMA:
        return None
    if payload.get("schema_version") != SCHEMA_VERSION:
        return None
    return payload


def _claim_age_seconds(record: dict[str, Any]) -> float | None:
    claimed_at = localio.parse_iso_datetime(record.get("claimed_at"))
    if claimed_at is None:
        return None
    return max(0.0, time.time() - claimed_at.timestamp())


def _recover_stale_claims(key: str, *, env: dict[str, str] | None = None) -> None:
    root = key_dir(key, env=env)
    if not root.is_dir() or root.is_symlink():
        return
    pending = pending_path(key, env=env)
    for path in sorted(root.glob("claimed-*.json")):
        if path.is_symlink():
            continue
        record = _read_record(path)
        if record is None:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                continue
            continue
        age = _claim_age_seconds(record)
        if age is None or age < STALE_CLAIM_SECONDS:
            continue
        if pending.exists():
            try:
                path.unlink(missing_ok=True)
            except OSError:
                continue
            continue
        try:
            os.rename(path, pending)
            _chmod_private(pending)
        except OSError:
            continue


def try_claim(
    session_id: str,
    workspace: Path,
    *,
    env: dict[str, str] | None = None,
    pid: int | None = None,
) -> CompactionClaim | None:
    """Atomically claim a pending marker, recovering abandoned claims first."""
    key = marker_key(session_id, workspace)
    _recover_stale_claims(key, env=env)
    pending = pending_path(key, env=env)
    if not pending.is_file():
        # A stale claim may have been the only remnant; recovery can restore it.
        _recover_stale_claims(key, env=env)
        if not pending.is_file():
            return None
    record = _read_record(pending)
    if record is None:
        try:
            pending.unlink(missing_ok=True)
        except OSError:
            pass
        return None
    claimer = os.getpid() if pid is None else pid
    nonce = secrets.token_hex(4)
    claimed = key_dir(key, env=env) / f"claimed-{claimer}-{nonce}.json"
    try:
        # Rename first so only one process can own the marker; writers that lose
        # the race see ENOENT and leave without creating a second claim.
        os.rename(pending, claimed)
    except OSError:
        return None
    if claimed.is_symlink():
        try:
            claimed.unlink(missing_ok=True)
        except OSError:
            pass
        return None
    claimed_record = dict(record)
    claimed_record["claimed_at"] = localio.utc_now_iso()
    claimed_record["claimer_pid"] = claimer
    try:
        localio.write_text_atomic(
            claimed,
            json.dumps(claimed_record, indent=2, sort_keys=True) + "\n",
        )
        _chmod_private(claimed)
    except OSError:
        # Ownership already held; keep the claim file so stale recovery can run.
        return CompactionClaim(key=key, path=claimed, record=record)
    return CompactionClaim(key=key, path=claimed, record=claimed_record)


def release_claim(claim: CompactionClaim, *, env: dict[str, str] | None = None) -> None:
    """Return a claim to pending so a later event can retry injection."""
    release_claim_path(claim.path, env=env)


def release_claim_path(path: Path, *, env: dict[str, str] | None = None) -> None:
    """Return a claim file at ``path`` to pending (best-effort)."""
    del env  # key directory is derived from the claim path
    if not path.is_file() or path.is_symlink():
        return
    pending = path.parent / "pending.json"
    record = _read_record(path)
    if record is not None:
        cleaned = {key: value for key, value in record.items() if key not in {"claimed_at", "claimer_pid"}}
        try:
            localio.write_text_atomic(
                path,
                json.dumps(cleaned, indent=2, sort_keys=True) + "\n",
            )
            _chmod_private(path)
        except OSError:
            return
    if pending.exists():
        try:
            path.unlink(missing_ok=True)
        except OSError:
            return
        return
    try:
        os.rename(path, pending)
        _chmod_private(pending)
    except OSError:
        return


def complete_claim(claim: CompactionClaim) -> None:
    """Drop a claim after the #735 envelope was written to stdout successfully."""
    complete_claim_path(claim.path)


def complete_claim_path(path: Path) -> None:
    try:
        if path.is_symlink():
            return
        path.unlink(missing_ok=True)
    except OSError:
        return
    try:
        path.parent.rmdir()
    except OSError:
        return
