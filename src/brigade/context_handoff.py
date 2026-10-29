"""Body-free, locally bound Codex accounting handoffs. No config discovery."""

from __future__ import annotations

from contextlib import ExitStack
from hashlib import sha256
import json
import os
from pathlib import Path
import stat
from typing import Any

from . import __version__
from . import context_preview as cp

MAX_PATH_BYTES = 4096
MAX_ABSOLUTE_DEPTH = 128
MAX_ENVELOPE_BYTES = 8 * 1024 * 1024
MAX_CANONICAL_DEPTH = 64
LIMITATIONS = [
    "fresh_disk_only",
    "no_atomic_snapshot",
    "timestamp_granularity_limits_change_detection",
    "access_times_may_change",
    "thread_and_session_state_unknown",
    "global_not_requested",
    "profile_project_system_managed_launch_overrides_unresolved",
    "integrity_digest_not_authorization",
    "under_cap_not_behavior_guarantee",
]


class _Refusal(Exception):
    def __init__(self, reason: str):
        self.reason = reason


def error(reason: str) -> dict[str, Any]:
    return {
        "schema": "brigade.context_handoff.error",
        "schema_version": 1,
        "actual_session_observed": False,
        "reason": reason,
    }


def _strict_value(value: Any, depth: int = 0) -> None:
    if depth > MAX_CANONICAL_DEPTH:
        raise ValueError("canonical depth limit")
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, int) and 0 <= value <= 2**64 - 1:
        return
    if isinstance(value, str):
        try:
            value.encode("utf-8", errors="strict")
        except UnicodeError:
            raise ValueError("invalid canonical string") from None
        return
    if isinstance(value, list):
        for item in value:
            _strict_value(item, depth + 1)
        return
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        for key, item in value.items():
            _strict_value(key, depth + 1)
            _strict_value(item, depth + 1)
        return
    raise ValueError("invalid canonical value")


def canonical_bytes(value: Any) -> bytes:
    """Strict v1 canonical JSON, including any digest supplied in the value."""
    _strict_value(value)
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(raw) > MAX_ENVELOPE_BYTES:
        raise ValueError("canonical size limit")
    return raw


def integrity_digest(value: dict[str, Any]) -> str:
    """Exclude only the top-level digest. This is not authentication."""
    return "sha256:" + sha256(canonical_bytes({k: v for k, v in value.items() if k != "digest"})).hexdigest()


def _path_text(value: str, *, basename: bool = False) -> None:
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeError:
        raise _Refusal("invalid_settings" if basename else "unsafe_path") from None
    if len(encoded) > MAX_PATH_BYTES:
        raise _Refusal("resource_limit")
    if any(ch in "\\:" or ord(ch) < 32 or 127 <= ord(ch) <= 159 for ch in value):
        raise _Refusal("invalid_settings" if basename else "unsafe_path")
    if basename and (not cp._basename(value) or value != value.strip(cp.RUST_WHITESPACE)):
        raise _Refusal("invalid_settings")


def _absolute(value: str | Path) -> str:
    try:
        raw = os.fspath(value)
    except TypeError:
        raise _Refusal("unsafe_path") from None
    if not isinstance(raw, str) or not raw.startswith("/"):
        raise _Refusal("unsafe_path")
    _path_text(raw)
    if any(part in {".", ".."} for part in raw.split("/")):
        raise _Refusal("unsafe_path")
    normalized = cp._absolute(raw)
    if len([part for part in normalized.split("/") if part]) > MAX_ABSOLUTE_DEPTH:
        raise _Refusal("resource_limit")
    return normalized


def _settings(cap: Any, fallbacks: Any, markers: Any, defaults: bool, trust: str, access: str) -> dict[str, Any]:
    if (
        type(defaults) is not bool
        or trust not in ("trusted", "unset", "untrusted")
        or access not in ("full", "restricted")
    ):
        raise _Refusal("invalid_settings")
    result = {}
    for key, supplied, default in (
        ("project_doc_max_bytes", cap, 32768),
        ("fallback_filenames", fallbacks, []),
        ("root_markers", markers, [".git"]),
    ):
        if supplied is None and not defaults:
            raise _Refusal("invalid_settings")
        value = default if supplied is None else supplied
        if key == "project_doc_max_bytes":
            if type(value) is not int or value < 0:
                raise _Refusal("invalid_settings")
            if value > cp.RESOURCE_MAX_BYTES:
                raise _Refusal("resource_limit")
        else:
            if not isinstance(value, list):
                raise _Refusal("invalid_settings")
            if len(value) > cp.RESOURCE_MAX_LIST_ENTRIES:
                raise _Refusal("resource_limit")
            for name in value:
                if not isinstance(name, str):
                    raise _Refusal("invalid_settings")
                _path_text(name, basename=True)
            value = list(value)
        result[key] = {"value": value, "source": "default-assumption" if supplied is None else "caller-supplied"}
    result.update(
        {
            "trust": {"value": trust, "source": "caller-supplied"},
            "read_access": {"value": access, "source": "caller-supplied"},
            "environment_order": {"value": ["explicit-cwd"], "source": "caller-supplied"},
        }
    )
    if cap == 0 or trust == "untrusted" or access == "restricted":
        raise _Refusal("inspection_disabled")
    return result


def _identity(info: os.stat_result) -> dict[str, Any]:
    kinds = (
        (stat.S_ISREG, "regular"),
        (stat.S_ISDIR, "directory"),
        (stat.S_ISFIFO, "fifo"),
        (stat.S_ISSOCK, "socket"),
        (stat.S_ISCHR, "character"),
        (stat.S_ISBLK, "block"),
    )
    kind = next((name for predicate, name in kinds if predicate(info.st_mode)), None)
    if kind is None:
        raise _Refusal("unsafe_path")
    result = {"device": info.st_dev, "inode": info.st_ino, "kind": kind}
    if kind != "directory":
        result.update(size_bytes=info.st_size, mtime_ns=info.st_mtime_ns, ctime_ns=info.st_ctime_ns)
    return result


def _observations(observations: list[cp._MarkerObservation], paths: dict[int, str]) -> list[dict[str, Any]]:
    result = []
    for fd, name, info in observations:
        identity = _identity(info) if info is not None else None
        result.append(
            {
                "directory": paths[fd],
                "name": name,
                "state": identity["kind"] if identity else "absent",
                "identity": identity,
            }
        )
    return result


def _finish_validation(
    bindings: list[cp._DirectoryBinding],
    markers: list[cp._MarkerObservation],
    selections: list[cp._Selection],
    evidence: cp._HandoffEvidence,
) -> None:
    cp._revalidate_directories(bindings)
    cp._revalidate_markers(markers)
    cp._revalidate_markers(evidence.observations)
    cp._revalidate(selections)
    for fd, before in evidence.held_sources:
        if cp._fingerprint(os.fstat(fd)) != cp._fingerprint(before):
            raise cp._Boundary("file_changed")


def export(
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
) -> dict[str, Any]:
    """Return a complete v1 handoff or a body-free opaque refusal envelope."""
    try:
        if codex_version != "0.160.0":
            raise _Refusal("unsupported_version")
        settings = _settings(
            project_doc_max_bytes, fallback_filenames, root_markers, assume_codex_defaults, trust, read_access
        )
        if not cp._POSIX_NOFOLLOW_SUPPORTED:
            raise _Refusal("unsupported_platform")
        root, working = _absolute(target), _absolute(cwd)
        if os.path.commonpath([root, working]) != root:
            raise _Refusal("unsafe_path")
        relative = os.path.relpath(working, root)
        if len(Path(relative).parts) + 1 > cp.HANDOFF_MAX_DIRECTORIES:
            raise _Refusal("resource_limit")
        with ExitStack() as stack:
            bindings: list[cp._DirectoryBinding] = []
            directories = cp._project_directories(stack, bindings, root, working)
            if len(directories) > cp.HANDOFF_MAX_DIRECTORIES:
                raise _Refusal("resource_limit")
            paths = {fd: prefix or "." for prefix, fd in directories}
            markers: list[cp._MarkerObservation] = []
            evidence = cp._HandoffEvidence()
            accounting: dict[str, Any] = {"files": [], "totals": {}}
            selections = cp._project(
                accounting,
                directories,
                markers,
                settings["root_markers"]["value"],
                ["AGENTS.override.md", "AGENTS.md", *settings["fallback_filenames"]["value"]],
                settings["project_doc_max_bytes"]["value"],
                stack=stack,
                evidence=evidence,
            )
            sources = []
            for row, source in zip(accounting["files"], evidence.sources, strict=True):
                _path_text(row["path"])
                sources.append(
                    {key: value for key, value in row.items() if key not in ("scope", "brigade_advisory_budget_bytes")}
                )
                sources[-1].update(identity=_identity(source["info"]), sha256=source["sha256"])
            result: dict[str, Any] = {
                "schema": "brigade.context_handoff",
                "schema_version": 1,
                "producer": {"name": "brigade", "version": __version__},
                "consumer": {
                    "name": "codex",
                    "version": "0.160.0",
                    "version_source": "caller-supplied",
                    "source_commit": cp.SOURCE_COMMIT,
                },
                "actual_session_observed": False,
                "repository": {
                    "location_sha256": sha256(
                        b"brigade.context_handoff.repository.v1\0" + root.encode("utf-8")
                    ).hexdigest(),
                    "identity": _identity(os.fstat(directories[0][1])),
                },
                "cwd": relative,
                "settings": settings,
                "directories": [
                    {"path": prefix or ".", "identity": _identity(os.fstat(fd))} for prefix, fd in directories
                ],
                "root_marker_observations": _observations(markers, paths),
                "selection_observations": _observations(evidence.observations, paths),
                "sources": sources,
                "baseline": accounting["totals"]["project"],
                "completeness": {
                    "inspection": "complete",
                    "source_hashes": True,
                    "selection": True,
                    "directories": True,
                    "root_markers": True,
                },
                "limitations": list(LIMITATIONS),
            }
            try:
                result["digest"] = integrity_digest(result)
                canonical_bytes(result)  # Include the digest in the envelope ceiling.
            except ValueError:
                raise _Refusal("resource_limit") from None
            _finish_validation(bindings, markers, selections, evidence)
            return result
    except _Refusal as exc:
        return error(exc.reason)
    except cp._Boundary as exc:
        if "changed" in exc.reason:
            return error("inspection_changed")
        if exc.reason == "handoff_resource_limit":
            return error("resource_limit")
        if exc.reason == "symlink_not_evaluated":
            return error("unsafe_path")
        return error("identity_not_established")
    except (OSError, TypeError, ValueError, UnicodeError, OverflowError):
        return error("identity_not_established")


def _reject_constant(value: str) -> Any:
    raise ValueError("invalid JSON constant")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def command(*, json_output: bool, **settings: Any) -> int:
    """Parse explicit CLI settings strictly and print one canonical envelope."""
    result = None
    try:
        if not json_output:
            result = error("json_required")
        elif settings.pop("codex_home", None) is not None or settings.pop("global_max_bytes", None) is not None:
            result = error("invalid_settings")
        else:
            for key in ("project_doc_max_bytes", "fallback_filenames", "root_markers"):
                value = settings.get(key)
                if value is not None:
                    if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_ENVELOPE_BYTES:
                        raise ValueError("invalid JSON setting")
                    settings[key] = json.loads(value, parse_constant=_reject_constant, object_pairs_hook=_unique_object)
                    if settings[key] is None:
                        raise ValueError("null setting")
            result = export(**settings)
    except (TypeError, ValueError, UnicodeError, RecursionError):
        result = error("invalid_settings")
    print(canonical_bytes(result).decode("utf-8"))
    return 0 if result["schema"] == "brigade.context_handoff" else 2
