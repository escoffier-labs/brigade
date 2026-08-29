"""Local interactive-session identity, dirty-path snapshots, and overlap projection.

This module never persists remotes, credentials, diffs, or file contents. The
Hub receives only the bounded operational snapshot built here.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Mapping, Sequence
from urllib.parse import unquote, urlparse

DEFAULT_TTL_SECONDS = 900
MIN_TTL_SECONDS = 120
MAX_TTL_SECONDS = 3600
MAX_DIRTY_PATHS = 64
MAX_DIRTY_PATH_CHARS = 512
MAX_DIRTY_JSON_BYTES = 32 * 1024
MAX_OVERLAP_PATHS = 8
MAX_PRESENCE_STDIN_BYTES = 1_048_576
MAX_PRESENCE_ERROR_CHARS = 200
MAX_REPO_IDENTITY_CHARS = 512
_IDENTITY_CACHE_LIMIT = 32
_GIT_DIR_FILE_LIMIT = 4096
_CURSOR_WRITE_TOOLS = frozenset(
    {
        "Write",
        "StrReplace",
        "Delete",
        "EditNotebook",
        "NotebookEdit",
        "Edit",
        "TabWrite",
        "MultiEdit",
        "ApplyPatch",
        "apply_patch",
        "search_replace",
        "write_file",
    }
)
CURSOR_WORK_LOOP_CONTEXT = (
    "BRIGADE WORK LOOP: In a Brigade-wired repository, invoke the global brigade-work skill before substantive "
    "work. Start with brigade work brief --target .. Run checks that should count through brigade work verify "
    "run with --capture brigade-work. Capture failures, export new receipts to MiseLedger when installed, and "
    "finish durable work with a Memory Handoff."
)
_CURSOR_HOOK_EVENTS = {
    "sessionStart": "start",
    "postToolUse": "heartbeat",
    "sessionEnd": "end",
}
_UPSERT_EVENTS = frozenset({"SessionStart", "PostToolUse", "start", "heartbeat"})
_END_EVENTS = frozenset({"Stop", "end"})

_SCP_REMOTE = re.compile(r"^(?:(?P<user>[^@/]+)@)?(?P<host>[^:/\s]+):(?P<path>.+)$")
_SECRET_LIKE_IDENTITY = re.compile(r"(?i)(://|token=|password=|[^/]+:[^/@]+@)")
_ENCODED_PATH_SEP = re.compile(r"%2[fF]|%5[cC]")
_CASEFOLD_HOSTS = frozenset(
    {
        "github.com",
        "gist.github.com",
        "gitlab.com",
        "bitbucket.org",
    }
)


@dataclass(frozen=True)
class RepositoryIdentity:
    value: str
    scope: Literal["fleet", "node"]


@dataclass(frozen=True)
class DirtyPathSnapshot:
    paths: tuple[str, ...]
    truncated: bool


@dataclass(frozen=True)
class SessionSnapshot:
    harness: str
    session_id: str
    repo_identity: str
    identity_scope: str
    repo_label: str
    checkout_path: str
    branch: str | None
    dirty_paths: tuple[str, ...]
    dirty_truncated: bool
    ttl_seconds: int = DEFAULT_TTL_SECONDS


_IDENTITY_CACHE: dict[str, tuple[RepositoryIdentity, int | None]] = {}


def clear_repository_identity_cache() -> None:
    """Drop the process-local repository identity cache."""
    _IDENTITY_CACHE.clear()


def validate_repo_identity(value: str) -> str | None:
    """Return a credential-free identity, or None when the value is unsafe."""
    if not value or len(value) > MAX_REPO_IDENTITY_CHARS:
        return None
    if _contains_controls(value):
        return None
    if _SECRET_LIKE_IDENTITY.search(value) is not None:
        return None
    return value


def repository_identity(target: Path) -> RepositoryIdentity:
    """Return a credential-free repository identity for ``target``."""
    try:
        key = str(target.resolve())
    except OSError:
        key = str(target)
    stamp = _identity_cache_stamp(target)
    cached = _IDENTITY_CACHE.get(key)
    if cached is not None and cached[1] == stamp:
        return cached[0]
    remote = _origin_url(target)
    parsed = _parse_remote(remote) if remote else None
    identity = _local_identity(target) if parsed is None else RepositoryIdentity(value=parsed, scope="fleet")
    _IDENTITY_CACHE[key] = (identity, stamp)
    while len(_IDENTITY_CACHE) > _IDENTITY_CACHE_LIMIT:
        _IDENTITY_CACHE.pop(next(iter(_IDENTITY_CACHE)))
    return identity


def collect_dirty_paths(target: Path) -> DirtyPathSnapshot:
    """Collect bounded, repository-relative dirty paths from one porcelain -z run."""
    try:
        completed = subprocess.run(
            ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            shell=False,
            timeout=5,
            cwd=target,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="surrogateescape",
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return DirtyPathSnapshot(paths=(), truncated=True)
    if completed.returncode != 0:
        return DirtyPathSnapshot(paths=(), truncated=True)
    collected: list[str] = []
    skipped = False
    for raw_path in _parse_porcelain_z(completed.stdout):
        normalized = _normalize_dirty_path(raw_path)
        if normalized is None:
            if raw_path and not _is_excluded_brigade_path(raw_path):
                skipped = True
            continue
        collected.append(normalized)
    unique = sorted(set(collected))
    truncated = skipped or len(unique) > MAX_DIRTY_PATHS
    bounded = unique[:MAX_DIRTY_PATHS]
    while bounded:
        encoded = json.dumps(bounded, ensure_ascii=False).encode("utf-8")
        if len(encoded) <= MAX_DIRTY_JSON_BYTES:
            break
        bounded = bounded[:-1]
        truncated = True
    return DirtyPathSnapshot(paths=tuple(bounded), truncated=truncated)


def build_snapshot(
    target: Path,
    *,
    harness: str,
    session_id: str,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> SessionSnapshot:
    """Build one immutable local session snapshot for Hub publication."""
    identity = repository_identity(target)
    dirty = collect_dirty_paths(target)
    checkout = str(target.resolve())
    label = identity.value.rsplit("/", 1)[-1]
    if identity.scope == "node":
        label = target.name or label
    return SessionSnapshot(
        harness=harness,
        session_id=session_id,
        repo_identity=identity.value,
        identity_scope=identity.scope,
        repo_label=label[:256],
        checkout_path=checkout,
        branch=_current_branch(target),
        dirty_paths=dirty.paths,
        dirty_truncated=dirty.truncated,
        ttl_seconds=_clamp_ttl(ttl_seconds),
    )


def overlap_warnings(
    current: SessionSnapshot,
    rows: Sequence[Mapping[str, object]],
    *,
    current_node: str,
) -> list[dict[str, object]]:
    """Return advisory overlap warnings for other live sessions on the same identity."""
    now = time.time()
    warnings: list[dict[str, object]] = []
    current_paths = {_normalize_overlap_path(path) for path in current.dirty_paths}
    current_paths.discard("")
    for row in rows:
        if not _row_is_live(row, now=now):
            continue
        if str(row.get("repo_identity") or "") != current.repo_identity:
            continue
        if str(row.get("identity_scope") or "") != current.identity_scope:
            continue
        node_id = str(row.get("node_id") or "")
        if current.identity_scope == "node" and node_id != current_node:
            continue
        harness = str(row.get("harness") or "")
        session_id = str(row.get("session_id") or "")
        if node_id == current_node and harness == current.harness and session_id == current.session_id:
            continue
        other_paths = {_normalize_overlap_path(path) for path in _row_dirty_paths(row.get("dirty_paths"))}
        other_paths.discard("")
        intersection = sorted(current_paths & other_paths)
        if not intersection:
            continue
        other_truncated = bool(row.get("dirty_truncated"))
        warnings.append(
            {
                "node_id": node_id,
                "harness": harness,
                "session_id": session_id,
                "branch": row.get("branch"),
                "checkout_path": row.get("checkout_path"),
                "age": _age_seconds(row.get("started_at"), now),
                "paths": intersection[:MAX_OVERLAP_PATHS],
                "partial": current.dirty_truncated or other_truncated,
            }
        )
    return warnings


def _clamp_ttl(ttl_seconds: int) -> int:
    return min(MAX_TTL_SECONDS, max(MIN_TTL_SECONDS, int(ttl_seconds)))


def _origin_url(target: Path) -> str | None:
    completed = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        shell=False,
        timeout=5,
        cwd=target,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        return None
    remote = completed.stdout.strip()
    return remote or None


def _local_identity(target: Path) -> RepositoryIdentity:
    completed = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"],
        shell=False,
        timeout=5,
        cwd=target,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode == 0 and completed.stdout.strip():
        raw = completed.stdout.strip()
        path = Path(raw) if Path(raw).is_absolute() else target / raw
        try:
            canonical = str(path.resolve())
        except OSError:
            canonical = str(target.resolve())
    else:
        canonical = str(target.resolve())
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    return RepositoryIdentity(value=f"local:{digest}", scope="node")


def _parse_remote(remote: str) -> str | None:
    value = remote.strip()
    if not value or "\x00" in value or _contains_controls(value):
        return None
    if "://" in value:
        parsed = urlparse(value)
        host = (parsed.hostname or "").strip().lower()
        raw_path = parsed.path or ""
    else:
        match = _SCP_REMOTE.fullmatch(value)
        if match is None:
            return None
        host = match.group("host").strip().lower()
        raw_path = match.group("path") or ""
        if len(host) == 1:
            return None
    if not host or "/" in host:
        return None
    path = _decode_remote_path(raw_path)
    if path is None:
        return None
    path = path.strip("/")
    if path.lower().endswith(".git"):
        path = path[:-4]
    path = path.strip("/")
    if not path or path.startswith(".") or ".." in Path(path).parts:
        return None
    if host in _CASEFOLD_HOSTS:
        path = path.lower()
    identity = f"{host}/{path}"
    return validate_repo_identity(identity)


def _parse_porcelain_z(payload: str) -> list[str]:
    parts = payload.split("\0")
    paths: list[str] = []
    index = 0
    while index < len(parts):
        record = parts[index]
        index += 1
        if not record:
            continue
        status = record[:2]
        path = record[3:] if len(record) > 3 else ""
        if "R" in status or "C" in status:
            # Porcelain -z emits dest\0origin. Keep dest and consume origin.
            if index < len(parts):
                index += 1
        if path:
            paths.append(path)
    return paths


def _decode_remote_path(path: str) -> str | None:
    """Decode a remote path without collapsing encoded separators into ``/``."""
    parts: list[str] = []
    for raw_segment in path.replace("\\", "/").split("/"):
        if not raw_segment:
            continue
        if _ENCODED_PATH_SEP.search(raw_segment):
            return None
        segment = unquote(raw_segment)
        if _ENCODED_PATH_SEP.search(segment):
            return None
        if "/" in segment or "\\" in segment or _contains_controls(segment):
            return None
        parts.append(segment)
    return "/".join(parts)


def _contains_controls(value: str) -> bool:
    return any(ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F for ch in value)


def _read_bounded_text(path: Path, *, limit: int = _GIT_DIR_FILE_LIMIT) -> str | None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            return None
        raw = os.read(fd, limit + 1)
        if len(raw) > limit:
            return None
        return raw.decode("utf-8", errors="replace")
    except OSError:
        return None
    finally:
        os.close(fd)


def _linked_worktree_config(git_file: Path, target: Path) -> Path | None:
    text = _read_bounded_text(git_file)
    if text is None:
        return None
    gitdir: Path | None = None
    for line in text.splitlines():
        if line.lower().startswith("gitdir:"):
            value = line.split(":", 1)[1].strip()
            gitdir = Path(value) if Path(value).is_absolute() else target / value
            break
    if gitdir is None:
        return None
    commondir_file = gitdir / "commondir"
    if commondir_file.is_file():
        common_raw = (_read_bounded_text(commondir_file) or "").strip()
        if common_raw:
            common = Path(common_raw) if Path(common_raw).is_absolute() else gitdir / common_raw
            config = common / "config"
            if config.is_file():
                return config
    config = gitdir / "config"
    return config if config.is_file() else None


def _identity_cache_stamp(target: Path) -> int | None:
    git_meta = target / ".git"
    try:
        if git_meta.is_file():
            config = _linked_worktree_config(git_meta, target)
            return config.stat().st_mtime_ns if config is not None else None
        return (git_meta / "config").stat().st_mtime_ns
    except OSError:
        return None


def _is_excluded_brigade_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.startswith(".brigade/")


def _normalize_dirty_path(path: str) -> str | None:
    normalized = path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    if not normalized or normalized.startswith(".brigade/"):
        return None
    if len(normalized) > MAX_DIRTY_PATH_CHARS:
        return None
    candidate = Path(normalized)
    if candidate.is_absolute() or ".." in candidate.parts:
        return None
    if normalized.startswith("/") or normalized.startswith("~"):
        return None
    return normalized


def _current_branch(target: Path) -> str | None:
    completed = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        shell=False,
        timeout=5,
        cwd=target,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        return None
    branch = completed.stdout.strip()
    if not branch or branch == "HEAD":
        return None
    return branch


def _row_is_live(row: Mapping[str, object], *, now: float) -> bool:
    if str(row.get("state") or "active") != "active":
        return False
    expires_at = row.get("expires_at")
    if isinstance(expires_at, (int, float)) and not isinstance(expires_at, bool):
        return float(expires_at) > now
    if isinstance(expires_at, str) and expires_at:
        try:
            stamp = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            return stamp.timestamp() > now
        except ValueError:
            return False
    return True


def _row_dirty_paths(raw: object) -> list[str]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(item) for item in raw if isinstance(item, str)]


def _normalize_overlap_path(path: str) -> str:
    return _normalize_dirty_path(path) or ""


def _hub_url_configured() -> bool:
    try:
        from . import fleet_client

        return bool(fleet_client.load_fleet_config().get("hub_url"))
    except Exception:
        return False


def _publish_presence(event: str, target: Path, session_id: str, *, harness: str = "claude") -> None:
    """Best-effort Hub publication. Exceptions never escape to hook callers."""
    try:
        from . import fleet_client

        if not _hub_url_configured():
            return
        snapshot = build_snapshot(target, harness=harness, session_id=session_id)
        if event in _END_EVENTS:
            fleet_client.end_session(snapshot)
        elif event in _UPSERT_EVENTS:
            fleet_client.upsert_session(snapshot)
    except Exception:
        return


def run_presence_hook(
    *,
    target: Path | None = None,
    harness: str | None = None,
    session: str | None = None,
    event: str | None = None,
    stdin: bool = False,
    context: str | None = None,
) -> int:
    """Publish or end one interactive session. Always exits 0 for adapter safety."""
    try:
        if harness == "cursor" and stdin and context == "cursor-work-loop":
            _run_cursor_presence_hook()
            return 0
        _run_explicit_presence_hook(target=target, harness=harness, session=session, event=event)
    except Exception:
        if harness == "cursor" and stdin and context == "cursor-work-loop":
            _print_cursor_hook_output(None)
    return 0


def _run_explicit_presence_hook(
    *,
    target: Path | None,
    harness: str | None,
    session: str | None,
    event: str | None,
) -> None:
    if target is None or not harness or not session or event not in {"start", "heartbeat", "end"}:
        return
    _publish_presence(event, target, session, harness=harness)


def _run_cursor_presence_hook() -> None:
    payload = _read_bounded_json_object()
    hook_event = payload.get("hook_event_name") if payload else None
    mapped = _CURSOR_HOOK_EVENTS.get(hook_event) if isinstance(hook_event, str) else None
    session_id = _cursor_session_id(payload)
    workspace = _cursor_workspace(payload)
    target = _cursor_presence_target(workspace)
    if mapped and session_id and target is not None:
        if mapped == "heartbeat":
            if _cursor_tool_is_write_capable(payload):
                _publish_presence("heartbeat", target, session_id, harness="cursor")
        else:
            _publish_presence(mapped, target, session_id, harness="cursor")
    _print_cursor_hook_output(hook_event if isinstance(hook_event, str) else None)


def _cursor_presence_target(workspace: Path | None) -> Path | None:
    """Match Claude's user-scope boundary: wired Cursor targets only, never home."""
    if workspace is None:
        return None
    try:
        resolved = workspace.expanduser().resolve()
        home = Path.home().expanduser().resolve()
    except OSError:
        return None
    if resolved == home:
        return None
    from .wiring import resolve_wired_target

    target = resolve_wired_target(str(resolved), harness="cursor")
    if target is None:
        return None
    try:
        return None if target.resolve() == home else target
    except OSError:
        return None


def _cursor_tool_is_write_capable(payload: Mapping[str, object] | None) -> bool:
    if payload is None:
        return False
    tool = payload.get("tool_name")
    if tool is None:
        tool = payload.get("tool")
    return isinstance(tool, str) and tool in _CURSOR_WRITE_TOOLS


def _cursor_session_id(payload: Mapping[str, object] | None) -> str | None:
    if payload is None:
        return None
    for key in ("session_id", "conversation_id"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _cursor_workspace(payload: Mapping[str, object] | None) -> Path | None:
    if payload is None:
        return None
    roots = payload.get("workspace_roots")
    if not isinstance(roots, list) or not roots:
        return None
    first = roots[0]
    if not isinstance(first, str) or not first:
        return None
    return Path(first)


def _print_cursor_hook_output(hook_event: str | None) -> None:
    if hook_event == "sessionStart":
        print(json.dumps({"additional_context": CURSOR_WORK_LOOP_CONTEXT}, indent=2, sort_keys=True))
        return
    print("{}")


def _read_bounded_json_object() -> dict[str, object] | None:
    buffer = getattr(sys.stdin, "buffer", None)
    if buffer is not None:
        raw = buffer.read(MAX_PRESENCE_STDIN_BYTES + 1)
    else:
        text = sys.stdin.read(MAX_PRESENCE_STDIN_BYTES + 1)
        raw = text.encode("utf-8") if isinstance(text, str) else text
    if not raw or len(raw) > MAX_PRESENCE_STDIN_BYTES:
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _bounded_presence_error(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}"
    return text[:MAX_PRESENCE_ERROR_CHARS]


def _age_seconds(started_at: object, now: float) -> int:
    if not isinstance(started_at, str) or not started_at:
        return 0
    try:
        stamp = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    except ValueError:
        return 0
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return max(0, int(now - stamp.timestamp()))
