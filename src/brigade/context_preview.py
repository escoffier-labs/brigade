"""Bounded fresh-disk Codex instruction accounting for supplied settings.

One POSIX environment, no config discovery, Git traversal, or consumer process.
Descriptors anchor all document operations; bodies never leave this module.
"""

from __future__ import annotations

import os
import stat
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from . import dirfd
from .budgets import bootstrap_budget

SOURCE_COMMIT = "a956835d020762cb2b570053af06f643a11c0ecc"
RESOURCE_MAX_BYTES = 1024 * 1024  # Brigade ceiling, never a consumer-cap clamp.
RUST_WHITESPACE = "\t\n\v\f\r \x85\xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000"

# Native capabilities are fixed at import, independent of later instrumentation.
_NATIVE_OPEN, _NATIVE_STAT = os.open, os.stat
_POSIX_NOFOLLOW_SUPPORTED = (
    os.name == "posix"
    and {_NATIVE_OPEN, _NATIVE_STAT} <= os.supports_dir_fd
    and _NATIVE_STAT in os.supports_follow_symlinks
    and all(hasattr(os, flag) for flag in ("O_NOFOLLOW", "O_DIRECTORY", "O_NONBLOCK"))
)
_Selection = tuple[int, list[str], tuple[str, os.stat_result] | None]


class _Boundary(Exception):
    def __init__(self, reason: str, *, divergent: bool = False):
        self.reason = reason
        self.divergent = divergent


def _basename(value: object) -> bool:
    return (
        isinstance(value, str) and bool(value) and value not in {".", ".."} and "/" not in value and "\x00" not in value
    )


def _encodable(value: str) -> bool:
    try:
        os.fsencode(value)
    except UnicodeError:
        return False
    return True


def _absolute(value: str | Path) -> str:
    raw = os.fspath(value)
    if not isinstance(raw, str) or "\x00" in raw or not raw.startswith("/"):
        raise _Boundary("absolute_scope_required")
    # Rust has one filesystem root; POSIX normpath alone preserves leading //.
    normalized = os.path.normpath("/" + raw.lstrip("/"))
    if any(part in {".", ".."} for part in normalized.split("/")):
        raise _Boundary("unsupported_path")
    if not _encodable(normalized):
        raise _Boundary("path_encoding_not_evaluated")
    return normalized


def _fingerprint(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_ctime_ns, info.st_mtime_ns, info.st_size


def _metadata(fd: int, name: str) -> os.stat_result | None:
    try:
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except (TypeError, NotImplementedError):
        raise _Boundary("posix_nofollow_required") from None
    except (OSError, UnicodeError, ValueError):
        raise _Boundary("metadata_not_evaluated") from None
    if stat.S_ISLNK(info.st_mode):
        raise _Boundary("symlink_not_evaluated", divergent=True)
    return info


def _directory(stack: ExitStack, name: str, parent: int | None = None) -> int:
    try:
        before = _metadata(parent, name) if parent is not None else None
        fd = os.open(name, dirfd.directory_flags(nofollow=True) | os.O_NONBLOCK, dir_fd=parent)
        stack.callback(os.close, fd)
        if before is not None and _fingerprint(before) != _fingerprint(os.fstat(fd)):
            raise _Boundary("directory_changed")
        return fd
    except _Boundary:
        raise
    except (TypeError, NotImplementedError):
        raise _Boundary("posix_nofollow_required") from None
    except (OSError, UnicodeError, ValueError):
        # Check metadata without following links to distinguish a refused link.
        if parent is not None:
            _metadata(parent, name)
        raise _Boundary("directory_not_evaluated") from None


def _scope(stack: ExitStack, root: str) -> int:
    fd = _directory(stack, "/")
    for part in root.split("/")[1:]:
        if part:
            fd = _directory(stack, part, fd)
    return fd


def _candidate(fd: int, names: list[str]) -> tuple[str, os.stat_result] | None:
    for name in names:
        info = _metadata(fd, name)
        if info is not None and stat.S_ISREG(info.st_mode):
            return name, info
    return None


def _revalidate(selections: list[_Selection]) -> None:
    """Recheck winners and absences using only nofollow metadata."""
    for fd, names, before in selections:
        after = _candidate(fd, names)
        if before is None and after is None:
            continue
        if (
            before is None
            or after is None
            or before[0] != after[0]
            or _fingerprint(before[1]) != _fingerprint(after[1])
        ):
            raise _Boundary("selection_changed")


def _read(fd: int, name: str, selected: os.stat_result, limit: int) -> bytes:
    held = None
    try:
        held = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        before = os.fstat(held)
        if not stat.S_ISREG(before.st_mode) or _fingerprint(before) != _fingerprint(selected):
            raise _Boundary("file_changed")
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(held, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(held)
        named = _metadata(fd, name)
        if _fingerprint(before) != _fingerprint(after) or named is None or _fingerprint(named) != _fingerprint(after):
            raise _Boundary("file_changed")
        return b"".join(chunks)[:limit]
    except _Boundary:
        raise
    except (TypeError, NotImplementedError):
        raise _Boundary("posix_nofollow_required") from None
    except (OSError, UnicodeError, ValueError):
        raise _Boundary("file_read_not_evaluated") from None
    finally:
        if held is not None:
            os.close(held)


def _row(scope: str, path: str, info: os.stat_result, order: int, cumulative: int) -> dict[str, Any]:
    return {
        "scope": scope,
        "path": path,
        "selection_order": order,
        "size_bytes": info.st_size,
        "cumulative_selected_bytes": cumulative,
        "consumed_raw_bytes": None,
        "rendered_utf8_bytes": None,
        "contribution": "unknown",
        "brigade_advisory_budget_bytes": bootstrap_budget(path.rsplit("/", 1)[-1]),
    }


def _totals(rows: list[dict[str, Any]], scope: str) -> dict[str, int | None]:
    selected = [row for row in rows if row["scope"] == scope]
    return {
        "selected_bytes": sum(row["size_bytes"] for row in selected),
        "consumed_raw_bytes": sum(row["consumed_raw_bytes"] for row in selected),
        "rendered_utf8_bytes": sum(row["rendered_utf8_bytes"] for row in selected),
    }


def _project_directories(stack: ExitStack, target: str, cwd: str) -> list[tuple[str, int]]:
    scope_fd = _scope(stack, target)
    directories: list[tuple[str, int]] = [("", scope_fd)]
    relative = os.path.relpath(cwd, target)
    if relative != ".":
        for part in relative.split("/"):
            prefix, parent = directories[-1]
            fd = _directory(stack, part, parent)
            directories.append((f"{prefix}/{part}".lstrip("/"), fd))
    return directories


def _project(
    result: dict[str, Any], directories: list[tuple[str, int]], markers: list[str], names: list[str], cap: int
) -> list[_Selection]:
    start = len(directories) - 1
    if markers:
        found = False
        for index in range(len(directories) - 1, -1, -1):
            if any(_metadata(directories[index][1], marker) is not None for marker in markers):
                start = index
                found = True
                break
        if not found:
            raise _Boundary("root_outside_scope_or_unknown")
    cumulative = 0
    selections: list[_Selection] = []
    candidates: list[tuple[int, str, os.stat_result, dict[str, Any]]] = []
    for prefix, fd in directories[start:]:
        selected = _candidate(fd, names)
        selections.append((fd, names, selected))
        if selected is None:
            continue
        name, info = selected
        cumulative += info.st_size
        path = f"{prefix}/{name}".lstrip("/")
        row = _row("project", path, info, len(result["files"]) + 1, cumulative)
        result["files"].append(row)
        candidates.append((fd, name, info, row))
    remaining = cap
    for fd, name, info, row in candidates:
        if remaining == 0:
            row.update(consumed_raw_bytes=0, rendered_utf8_bytes=0, contribution="cap_exhausted")
            continue
        raw = _read(fd, name, info, remaining)
        text = raw.decode("utf-8", errors="replace")
        if not text.strip(RUST_WHITESPACE):
            row.update(consumed_raw_bytes=0, rendered_utf8_bytes=0, contribution="empty")
            continue
        row.update(
            consumed_raw_bytes=len(raw),
            rendered_utf8_bytes=len(text.encode("utf-8")),
            contribution="truncated" if info.st_size > remaining else "loaded",
        )
        remaining -= len(raw)
    result["totals"]["project"] = _totals(result["files"], "project")
    return selections


def _global(stack: ExitStack, result: dict[str, Any], root: str, cap: int) -> list[_Selection]:
    fd = _scope(stack, root)
    cumulative = 0
    selections: list[_Selection] = []
    for name in ["AGENTS.override.md", "AGENTS.md"]:
        selected = _candidate(fd, [name])
        selections.append((fd, [name], selected))
        if selected is None:
            continue
        _, info = selected
        cumulative += info.st_size
        row = _row("global", name, info, len(result["files"]) + 1, cumulative)
        result["files"].append(row)
        if info.st_size > cap:
            raise _Boundary("global_safety_limit_exceeded", divergent=True)
        raw = _read(fd, name, info, cap)
        text = raw.decode("utf-8", errors="replace").strip(RUST_WHITESPACE)
        row.update(
            consumed_raw_bytes=len(raw),
            rendered_utf8_bytes=len(text.encode("utf-8")),
            contribution="loaded" if text else "empty",
        )
        if text:
            break
    result["totals"]["global"] = _totals(result["files"], "global")
    return selections


def _invalidate(result: dict[str, Any], scope: str, exc: _Boundary) -> None:
    result["limitations"].append(exc.reason)
    if exc.divergent:
        result["matches_codex"] = False
    # An unknown scope discards its load, never treating unknown as zero.
    for row in result["files"]:
        if row["scope"] == scope:
            row.update(
                consumed_raw_bytes=None,
                rendered_utf8_bytes=None,
                cumulative_selected_bytes=None,
                contribution="unknown",
            )
    result["totals"][scope] = dict.fromkeys(["selected_bytes", "consumed_raw_bytes", "rendered_utf8_bytes"])


def preview(
    *,
    target: str | Path,
    cwd: str | Path,
    codex_version: str | None,
    trust: str,
    read_access: str,
    assume_codex_defaults: bool = False,
    project_doc_max_bytes: int | None = None,
    fallback_filenames: list[str] | None = None,
    root_markers: list[str] | None = None,
    codex_home: str | Path | None = None,
    global_max_bytes: int | None = None,
) -> dict[str, Any]:
    """Return body-free accounting, with completeness only for supplied inputs.

    No intentional writes. Access times may change. Unsupported resource sizes
    refuse the entire inspection before any content reads, including global.
    """
    result: dict[str, Any] = {
        "schema_version": 1,
        "consumer": {
            "name": "codex",
            "version": "0.160.0" if codex_version == "0.160.0" else None,
            "version_source": "caller-supplied",
            "source_commit": SOURCE_COMMIT,
        },
        "status": "not_evaluated",
        "actual_session_observed": False,
        "matches_codex": None,
        "settings": {},
        "limitations": [
            "fresh_disk_only",
            "no_atomic_snapshot",
            "timestamp_granularity_limits_change_detection",
            "access_times_may_change",
            "thread_and_session_state_unknown",
        ],
        "files": [],
        "totals": {
            scope: dict.fromkeys(["selected_bytes", "consumed_raw_bytes", "rendered_utf8_bytes"])
            for scope in ["project", "global"]
        },
    }
    defaults: dict[str, Any] = {"project_doc_max_bytes": 32768, "fallback_filenames": [], "root_markers": [".git"]}
    supplied = {
        "project_doc_max_bytes": project_doc_max_bytes,
        "fallback_filenames": fallback_filenames,
        "root_markers": root_markers,
    }
    encoding_unknown = False
    for key, value in supplied.items():
        source = "caller-supplied"
        if value is None:
            source = "default-assumption" if assume_codex_defaults else "unknown"
            value = defaults[key] if assume_codex_defaults else None
        # Invalid filename settings must never echo absolute/private inputs.
        if key in {"fallback_filenames", "root_markers"}:
            if not isinstance(value, list):
                value = None
            elif any(isinstance(name, str) and not _encodable(name) for name in value):
                encoding_unknown = True
                value = None
        elif not isinstance(value, int):
            value = None
        if key == "fallback_filenames" and isinstance(value, list):
            normalized = [
                name.strip(RUST_WHITESPACE)
                for name in value
                if isinstance(name, str) and _basename(name.strip(RUST_WHITESPACE))
            ]
            if len(normalized) != len(value):
                result["limitations"].append("invalid_fallback_entries_ignored")
            value = normalized
        if key == "root_markers" and isinstance(value, list) and not all(_basename(name) for name in value):
            value = None
        result["settings"][key] = {"value": value, "source": source}
    result["settings"].update(
        {
            "trust": {
                "value": trust if trust in {"trusted", "untrusted", "unset"} else None,
                "source": "caller-supplied",
            },
            "read_access": {
                "value": read_access if read_access in {"full", "restricted"} else None,
                "source": "caller-supplied",
            },
            "environment_order": {"value": ["explicit-cwd"], "source": "caller-supplied"},
            "global_max_bytes": {
                "value": global_max_bytes if isinstance(global_max_bytes, int) else None,
                "source": "caller-supplied" if global_max_bytes is not None else "unknown",
            },
            "brigade_resource_max_bytes": {"value": RESOURCE_MAX_BYTES, "source": "brigade-safety-limit"},
        }
    )
    scope = "global" if codex_home is not None else "project"
    requested = {"project", "global"} if codex_home is not None else {"project"}
    completed: set[str] = set()
    try:
        if not _POSIX_NOFOLLOW_SUPPORTED:
            raise _Boundary("posix_nofollow_required")
        if encoding_unknown:
            raise _Boundary("filename_encoding_not_evaluated")
        if codex_version != "0.160.0":
            raise _Boundary("unsupported_or_unknown_version")
        if trust not in {"trusted", "untrusted", "unset"} or read_access not in {"full", "restricted"}:
            raise _Boundary("effective_trust_or_read_access_unknown")
        cap = result["settings"]["project_doc_max_bytes"]["value"]
        fallbacks = result["settings"]["fallback_filenames"]["value"]
        markers = result["settings"]["root_markers"]["value"]
        if cap is None or fallbacks is None or markers is None:
            raise _Boundary("configuration_provenance_unknown_or_invalid")
        if not isinstance(fallbacks, list) or not isinstance(markers, list):
            raise _Boundary("invalid_filename_settings")
        if isinstance(cap, bool) or not isinstance(cap, int) or not 0 <= cap <= RESOURCE_MAX_BYTES:
            raise _Boundary("project_resource_limit_not_evaluated")
        if global_max_bytes is not None and (
            isinstance(global_max_bytes, bool)
            or not isinstance(global_max_bytes, int)
            or not 0 <= global_max_bytes <= RESOURCE_MAX_BYTES
        ):
            raise _Boundary("global_resource_limit_not_evaluated")
        if codex_home is not None and global_max_bytes is None:
            raise _Boundary("explicit_global_safety_limit_required")
        project_root, working = _absolute(target), _absolute(cwd)
        if os.path.commonpath([project_root, working]) != project_root:
            raise _Boundary("cwd_outside_scope")
        global_root = _absolute(codex_home) if codex_home is not None else None
        with ExitStack() as stack:
            selections: dict[str, list[_Selection]] = {}
            try:
                if global_root is not None and global_max_bytes is not None:
                    selections["global"] = _global(stack, result, global_root, global_max_bytes)
                else:
                    result["totals"]["global"] = dict.fromkeys(
                        ["selected_bytes", "consumed_raw_bytes", "rendered_utf8_bytes"], 0
                    )
                    result["limitations"].append("global_not_requested")
                scope = "project"
                directories = _project_directories(stack, project_root, working)
                if trust == "untrusted" or cap == 0:
                    result["totals"]["project"] = dict.fromkeys(
                        ["selected_bytes", "consumed_raw_bytes", "rendered_utf8_bytes"], 0
                    )
                    selections["project"] = []
                else:
                    selections["project"] = _project(
                        result, directories, markers, ["AGENTS.override.md", "AGENTS.md", *fallbacks], cap
                    )
            except _Boundary as exc:
                _invalidate(result, scope, exc)
            # Keep descriptors alive for final validation, even after a load
            # failure in the other scope. Empty requested scopes count too.
            for scope, snapshots in selections.items():
                try:
                    _revalidate(snapshots)
                except _Boundary as exc:
                    _invalidate(result, scope, exc)
                else:
                    completed.add(scope)
    except _Boundary as exc:
        _invalidate(result, scope, exc)
        completed.discard(scope)
    if completed == requested:
        result.update(status="complete", matches_codex=True)
    else:
        result["status"] = "partial" if completed else "not_evaluated"
    return result
