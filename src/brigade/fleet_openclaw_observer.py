"""Opt-in metadata-only OpenClaw recent-session presence, bounded to one poll.

Run ``python -m brigade.fleet_openclaw_observer`` to inspect counts.
Only ``--publish`` writes presence through this node's enrolled public client.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Sequence, cast

from . import fleet_client, proc
from .fleet_session_presence import SessionSnapshot, _parse_remote

WINDOW_SECONDS = 900
TTL_SECONDS = 300
SESSION_LIMIT = 200
PUBLISH_LIMIT = 50
OBSERVATION_BUDGET_SECONDS = 15
CLI_TIMEOUT_SECONDS = 10
MAX_METADATA_BYTES = 262_144
_MARGIN_SECONDS = 0.25
_OPAQUE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


class ObservationError(ValueError):
    """A fixed error code safe to show without native command output."""


@dataclass(frozen=True)
class RecentSession:
    session_id: str
    workspace: Path
    updated_at: int


def _remaining(deadline: float, *, cap: float, reserve: float = 0) -> float:
    available = deadline - time.monotonic() - _MARGIN_SECONDS - reserve
    if available <= 0:
        raise ObservationError("observation_timeout")
    return min(cap, available)


def _bounded_json(result: proc.Result, *, limit: int = MAX_METADATA_BYTES) -> object:
    if (
        result.output_limit_exceeded
        or result.stream_limit_exceeded
        or result.stdout_bytes > limit
        or len(result.stdout.encode("utf-8")) > limit
    ):
        raise ObservationError("native_output_too_large")
    if result.code == 124:
        raise ObservationError("observation_timeout")
    if result.code != 0 or result.decode_failed or result.incomplete_process_group:
        raise ObservationError("native_command_failed")
    try:
        return json.loads(result.stdout)
    except (ValueError, RecursionError) as exc:
        raise ObservationError("native_json_invalid") from exc


def _native_json(args: list[str], deadline: float) -> object:
    result = proc.run(["openclaw", *args], timeout=_remaining(deadline, cap=CLI_TIMEOUT_SECONDS), supervise_group=True)
    _remaining(deadline, cap=1)
    return _bounded_json(result)


def _opaque_id(value: object) -> str:
    if not isinstance(value, str) or _OPAQUE_ID.fullmatch(value) is None:
        raise ObservationError("native_metadata_invalid")
    return value


def _absolute_workspace(raw: object, *, exists: bool = True) -> Path:
    if not isinstance(raw, str) or not raw or len(raw) > 1024 or any(ord(c) < 32 or ord(c) == 127 for c in raw):
        raise ObservationError("native_metadata_invalid")
    workspace = Path(raw).expanduser()
    try:
        if not workspace.is_absolute() or (exists and not workspace.is_dir()):
            raise ObservationError("workspace_unavailable")
        workspace = workspace.resolve() if exists else workspace
    except (OSError, RuntimeError) as exc:
        raise ObservationError("workspace_unavailable") from exc
    if len(str(workspace)) > 1024:
        raise ObservationError("native_metadata_invalid")
    return workspace


def _workspaces(payload: object) -> tuple[dict[str, Path], int]:
    if not isinstance(payload, list) or not payload or len(payload) > SESSION_LIMIT:
        raise ObservationError("native_metadata_invalid")
    result: dict[str, Path] = {}
    invalid = 0
    for row in payload:
        try:
            if not isinstance(row, dict):
                raise ObservationError("native_metadata_invalid")
            agent_id = _opaque_id(row.get("id"))
            if agent_id in result:
                raise ObservationError("native_identity_collision")
            result[agent_id] = _absolute_workspace(row.get("workspace"), exists=False)
        except ObservationError as exc:
            if str(exc) == "native_identity_collision":
                raise
            invalid += 1
    return result, invalid


def _recent_sessions(
    payload: object, workspaces: dict[str, Path], now_ms: int
) -> tuple[list[RecentSession], int, bool, int]:
    if not isinstance(payload, dict):
        raise ObservationError("native_metadata_invalid")
    rows, count, total, more = (payload.get(k) for k in ("sessions", "count", "totalCount", "hasMore"))
    if (
        not isinstance(rows, list)
        or len(rows) > SESSION_LIMIT
        or type(count) is not int
        or count != len(rows)
        or type(total) is not int
        or total < count
        or type(more) is not bool
        or more != (total > count)
    ):
        raise ObservationError("native_metadata_invalid")
    sessions: dict[str, RecentSession] = {}
    owners: dict[str, str] = {}
    invalid = 0
    for row in rows:
        try:
            if not isinstance(row, dict):
                raise ObservationError("native_metadata_invalid")
            session_id, agent_id = _opaque_id(row.get("sessionId")), _opaque_id(row.get("agentId"))
            updated = row.get("updatedAt")
            if type(updated) is not int or updated < 0 or updated > now_ms or agent_id not in workspaces:
                raise ObservationError("native_metadata_invalid")
            if session_id in owners and owners[session_id] != agent_id:
                raise ObservationError("native_identity_collision")
            owners[session_id] = agent_id
            if now_ms - updated >= WINDOW_SECONDS * 1000:
                continue
            raw_workspace = row.get("spawnedWorkspaceDir") or row.get("spawnedCwd") or str(workspaces[agent_id])
            workspace = _absolute_workspace(raw_workspace)
            previous = sessions.get(session_id)
            if previous is None or updated > previous.updated_at:
                sessions[session_id] = RecentSession(session_id, workspace, updated)
        except ObservationError as exc:
            if str(exc) == "native_identity_collision":
                raise
            invalid += 1
    return list(sessions.values()), total, more, invalid


def _git_metadata(target: Path, args: list[str], deadline: float) -> proc.Result:
    # No status, hooks, fsmonitor, external diff, textconv, or config execution.
    result = proc.run(
        ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", *args],
        cwd=target,
        timeout=_remaining(deadline, cap=1, reserve=fleet_client.REPORT_TIMEOUT_SECONDS),
        supervise_group=True,
    )
    _remaining(deadline, cap=1)
    if result.code == 124:
        raise ObservationError("observation_timeout")
    if (
        result.decode_failed
        or result.output_limit_exceeded
        or result.stream_limit_exceeded
        or result.incomplete_process_group
        or len(result.stdout) > 8192
    ):
        raise ObservationError("workspace_metadata_failed")
    return result


def _workspace_snapshot(target: Path, *, harness: str, deadline: float) -> SessionSnapshot:
    """Safe Git identity only; workspace dirt cannot be attributed to a session."""
    common = _git_metadata(target, ["rev-parse", "--git-common-dir"], deadline)
    branch = None
    parsed = None
    canonical = str(target)
    if common.code == 0 and common.stdout.strip():
        raw = Path(common.stdout.strip())
        canonical = str((raw if raw.is_absolute() else target / raw).resolve())
        remote = _git_metadata(target, ["remote", "get-url", "origin"], deadline)
        if remote.code == 0:
            parsed = _parse_remote(remote.stdout.strip())
        elif remote.code != 2:
            raise ObservationError("workspace_metadata_failed")
        head = _git_metadata(target, ["rev-parse", "--abbrev-ref", "HEAD"], deadline)
        if head.code == 0:
            value = head.stdout.strip()
            if value and value != "HEAD" and len(value) <= 256 and not any(ord(c) < 32 or ord(c) == 127 for c in value):
                branch = value
        elif head.code != 128:  # An unborn branch has no HEAD yet.
            raise ObservationError("workspace_metadata_failed")
    elif common.code != 128:  # Git's non-repository result uses path-scoped identity.
        raise ObservationError("workspace_metadata_failed")
    identity = parsed or "local:" + hashlib.sha256(canonical.encode()).hexdigest()
    return SessionSnapshot(
        harness=harness,
        session_id="observer",
        repo_identity=identity,
        identity_scope="fleet" if parsed else "node",
        repo_label=(identity.rsplit("/", 1)[-1] if parsed else target.name)[:256],
        checkout_path=str(target),
        branch=branch,
        dirty_paths=(),
        dirty_truncated=True,
        ttl_seconds=TTL_SECONDS,
    )


def _require_node_auth() -> None:
    settings = fleet_client.load_fleet_settings()
    if not settings.get("node_token"):
        raise ObservationError("node_auth_required")
    if not settings.get("hub_url"):
        raise ObservationError("hub_unconfigured")


def _rotation_state(harness: str) -> tuple[Path, int]:
    # Reuse the fleet client's portable private-file policy, including Windows.
    path = fleet_client.brigade_home() / "observer-cursors" / (harness + ".cursor")
    dir_fd = fleet_client._ensure_private_dir(path.parent)
    try:
        try:
            fd = fleet_client._open_private(path, os.O_RDONLY, dir_fd=dir_fd)
        except FileNotFoundError:
            return path, 0
        with os.fdopen(fd, "rb") as stream:
            raw = stream.read(128)
    finally:
        if dir_fd is not None:
            os.close(dir_fd)
    if len(raw) >= 128:
        raise ObservationError("rotation_state_unavailable")
    payload = json.loads(raw)
    offset = payload.get("offset") if isinstance(payload, dict) else None
    if type(offset) is not int or offset < 0 or offset > 2**63 - 1:
        raise ObservationError("rotation_state_unavailable")
    return path, offset


def _save_rotation(path: Path, offset: int) -> None:
    # One integer only. No native IDs, paths, messages, or credentials persist.
    dir_fd = fleet_client._ensure_private_dir(path.parent)
    try:
        fleet_client._write_spool_atomic(path, [{"offset": offset}], dir_fd=dir_fd)
    finally:
        if dir_fd is not None:
            os.close(dir_fd)


def _publish_recent(recent: list[RecentSession], *, harness: str, deadline: float) -> dict[str, object]:
    published = 0
    error = None
    cache: dict[Path, SessionSnapshot] = {}
    ordered = sorted(recent, key=lambda s: s.session_id)
    cursor_path = None
    start = 0
    attempted = 0
    if ordered:
        try:
            _remaining(deadline, cap=1, reserve=fleet_client.REPORT_TIMEOUT_SECONDS)
            cursor_path, offset = _rotation_state(harness)
            start = offset % len(ordered)
        except Exception as exc:
            return {
                "published": 0,
                "skipped": len(recent),
                "publish_limit": PUBLISH_LIMIT,
                "truncated": True,
                "error": str(exc) if isinstance(exc, ObservationError) else "rotation_state_unavailable",
            }
    selected = (ordered[start:] + ordered[:start])[:PUBLISH_LIMIT]
    for session in selected:
        try:
            _remaining(deadline, cap=1, reserve=fleet_client.REPORT_TIMEOUT_SECONDS)
            attempted += 1
            if session.workspace not in cache:
                cache[session.workspace] = _workspace_snapshot(session.workspace, harness=harness, deadline=deadline)
            snapshot = replace(cache[session.workspace], session_id=session.session_id)
            _remaining(deadline, cap=1, reserve=fleet_client.REPORT_TIMEOUT_SECONDS)
            if not fleet_client.publish_session(snapshot).ok:
                raise ObservationError("publication_failed")
            published += 1
        except ObservationError as exc:
            error = str(exc)
            if error == "observation_timeout":
                break
        except Exception:
            error = "publication_failed"
    if cursor_path is not None and attempted:
        try:
            _save_rotation(cursor_path, (start + attempted) % len(ordered))
        except Exception:
            error = "rotation_state_unavailable"
    result: dict[str, object] = {
        "published": published,
        "skipped": len(recent) - published,
        "publish_limit": PUBLISH_LIMIT,
        "truncated": published < len(recent),
    }
    if error:
        result["error"] = error
    return result


def observe(*, publish: bool = False) -> dict[str, object]:
    deadline = time.monotonic() + OBSERVATION_BUDGET_SECONDS
    if publish:
        _require_node_auth()
    workspaces, invalid_agents = _workspaces(_native_json(["agents", "list", "--json"], deadline))
    payload = _native_json(
        ["sessions", "--all-agents", "--active", "15", "--limit", str(SESSION_LIMIT), "--json"], deadline
    )
    now_ms = time.time_ns() // 1_000_000
    recent, total, truncated, invalid = _recent_sessions(payload, workspaces, now_ms)
    _remaining(deadline, cap=1)
    invalid += invalid_agents
    report: dict[str, object] = {
        "ok": not truncated and not invalid,
        "mode": "publish" if publish else "inspect",
        "recent_sessions": len(recent),
        "observed": len(recent),
        "total_sessions": total,
        "truncated": truncated,
        "invalid_rows": invalid,
        "skipped": invalid,
        "window_seconds": WINDOW_SECONDS,
        "ttl_seconds": TTL_SECONDS,
        "observed_at_ms": now_ms,
        "newest_activity_age_seconds": min((now_ms - s.updated_at) / 1000 for s in recent) if recent else None,
        "published": 0,
        "budget_seconds": OBSERVATION_BUDGET_SECONDS,
    }
    if publish:
        progress = _publish_recent(recent, harness="openclaw", deadline=deadline)
        report.update(progress)
        report["truncated"] = truncated or bool(progress["truncated"])
        report["skipped"] = cast(int, progress["skipped"]) + invalid
        report["ok"] = not (report["truncated"] or invalid or report.get("error"))
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--publish", action="store_true", help="Publish recent presence using this enrolled node")
    args = parser.parse_args(argv)
    try:
        report = observe(publish=args.publish)
    except ObservationError as exc:
        report = {"ok": False, "error": str(exc), "observed": 0, "published": 0, "skipped": 0}
    except Exception:
        report = {"ok": False, "error": "observation_failed", "observed": 0, "published": 0, "skipped": 0}
    print(json.dumps(report, sort_keys=True))
    if report.get("ok"):
        return 0
    return (
        2
        if report.get("truncated")
        and not report.get("invalid_rows")
        and report.get("error") in (None, "observation_timeout")
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
