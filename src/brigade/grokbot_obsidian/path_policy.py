"""Vault-relative POSIX path policy for Phase 1 reads and writes."""

from __future__ import annotations

import re
from typing import Any, Mapping, NoReturn

from .contracts import ERROR_MESSAGES, ObsidianError
from .utf8 import is_well_formed, utf8_byte_length

URI_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
DAILY_NOTE_FILE = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")
MAX_PATH_BYTES = 512
WRITE_ROOTS = (
    "00 - Inbox/Agent Notes",
    "01 - Projects",
    "02 - Areas/07 - Agent Work Log",
    "03 - Resources",
)
PROJECTS_ROOT = "01 - Projects"
ARCHIVE_ROOT = "04 - Archive"
BRIGADE_MEMORY_ROOT = "Brigade Memory"
EXCALIDRAW_ROOT = "03 - Resources/Excalidraw"
ATTACHMENTS_SEGMENT = "Attachments"
NOTE_WRITE_KINDS = frozenset({"create_note", "patch_note", "trash_note", "append_flashcard", "apply_template"})
CANVAS_WRITE_KINDS = frozenset({"create_canvas", "patch_canvas"})


def _deny() -> NoReturn:
    raise ObsidianError("denied", ERROR_MESSAGES["denied"])


def _strip_trailing_slash(value: str) -> str:
    return value[:-1] if value.endswith("/") else value


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(f"{root}/")


def _strictly_under(path: str, root: str) -> bool:
    return path.startswith(f"{root}/") and len(path) > len(root) + 1


def _has_attachments(path: str) -> bool:
    return ATTACHMENTS_SEGMENT in path.split("/")


def _is_daily_note(path: str, folder: str) -> bool:
    trimmed = _strip_trailing_slash(folder)
    if trimmed == "":
        return DAILY_NOTE_FILE.fullmatch(path) is not None
    prefix = f"{trimmed}/"
    return path.startswith(prefix) and DAILY_NOTE_FILE.fullmatch(path[len(prefix) :]) is not None


def _matches_sensitive_prefix(path: str, prefixes: tuple[str, ...]) -> bool:
    for prefix in prefixes:
        if not isinstance(prefix, str) or not prefix:
            continue
        trimmed = _strip_trailing_slash(prefix)
        if trimmed and _under(path, trimmed):
            return True
    return False


def _sensitive_tags_denied(policy: Mapping[str, Any]) -> bool:
    tags = policy.get("tags")
    if tags is None or tags == "unknown":
        return True
    if not isinstance(tags, (list, tuple)):
        return True
    sensitive = set(policy.get("sensitiveTags", ()))
    return any(tag in sensitive for tag in tags)


def _has_verified_excalidraw_suffix(path: str, suffix: str) -> bool:
    if suffix == ".excalidraw.md":
        return path.endswith(".excalidraw.md")
    return path.endswith(".excalidraw") and not path.endswith(".excalidraw.md")


def _is_writable_note_path(path: str) -> bool:
    return any(_strictly_under(path, root) for root in WRITE_ROOTS)


def _is_ordinary_markdown(path: str) -> bool:
    return path.endswith(".md") and not path.endswith(".excalidraw.md")


def vault_path_policy_from_runtime(runtime: Mapping[str, Any], tags: object = "unknown") -> dict[str, Any]:
    return {
        "dailyNotesFolder": runtime["daily_notes_folder"],
        "sensitivePathPrefixes": tuple(runtime["sensitive_path_prefixes"]),
        "sensitiveTags": tuple(runtime["sensitive_tags"]),
        "dashboardRoot": runtime["dashboard_root"],
        "excalidrawSuffix": runtime["excalidraw"]["verified_suffix"],
        "tags": tags,
    }


def policy_for_tags(policy: Mapping[str, Any], tags: object) -> dict[str, Any]:
    return {
        "dailyNotesFolder": policy.get("dailyNotesFolder", ""),
        "sensitivePathPrefixes": tuple(policy.get("sensitivePathPrefixes", ())),
        "sensitiveTags": tuple(policy.get("sensitiveTags", ())),
        "dashboardRoot": policy.get("dashboardRoot", "01 - Projects/Dashboard.base"),
        "excalidrawSuffix": policy.get("excalidrawSuffix", ".excalidraw.md"),
        "tags": tags,
    }


def normalize_vault_path(path: object) -> str:
    if not isinstance(path, str) or not is_well_formed(path):
        _deny()
    if "\0" in path or "\\" in path or "\r" in path:
        _deny()
    if URI_SCHEME.search(path) or path.startswith("/"):
        _deny()
    if utf8_byte_length(path) > MAX_PATH_BYTES:
        _deny()
    segments = path.split("/")
    for segment in segments:
        if (
            segment == ""
            or segment in {".", ".."}
            or segment.startswith(".")
            or segment.startswith("-")
            or "%" in segment
        ):
            _deny()
    normalized = "/".join(segments)
    if not normalized:
        _deny()
    return normalized


def _assert_static_denials(path: str, policy: Mapping[str, Any], *, allow_archive: bool = False) -> None:
    if _has_attachments(path):
        _deny()
    if _under(path, BRIGADE_MEMORY_ROOT):
        _deny()
    if _is_daily_note(path, str(policy.get("dailyNotesFolder", ""))):
        _deny()
    if _matches_sensitive_prefix(path, tuple(policy.get("sensitivePathPrefixes", ()))):
        _deny()
    if _under(path, ARCHIVE_ROOT) and not allow_archive:
        _deny()


def _assert_common_denials(path: str, policy: Mapping[str, Any], *, allow_archive: bool = False) -> None:
    _assert_static_denials(path, policy, allow_archive=allow_archive)
    if _sensitive_tags_denied(policy):
        _deny()


def assert_static_readable(path: object, policy: Mapping[str, Any]) -> str:
    normalized = normalize_vault_path(path)
    _assert_static_denials(normalized, policy)
    return normalized


def assert_readable(path: object, policy: Mapping[str, Any]) -> str:
    normalized = normalize_vault_path(path)
    _assert_common_denials(normalized, policy)
    return normalized


def _assert_write_kind(
    kind: str,
    path: object,
    dest_or_policy: object,
    maybe_policy: Mapping[str, Any] | None,
    source_path,
    dest_denials,
) -> str:
    if maybe_policy is not None:
        dest, policy = dest_or_policy, maybe_policy
    elif isinstance(dest_or_policy, Mapping) and "dailyNotesFolder" in dest_or_policy:
        dest, policy = None, dest_or_policy
    else:
        _deny()
        raise AssertionError("unreachable")

    if kind in {"copy_note", "move_note"}:
        if dest is None:
            _deny()
        source = source_path(path, policy)
        if not _is_ordinary_markdown(source):
            _deny()
        destination = normalize_vault_path(dest)
        if not _is_ordinary_markdown(destination):
            _deny()
        if _under(destination, EXCALIDRAW_ROOT):
            _deny()
        if kind == "copy_note":
            dest_denials(destination, policy)
            if not _is_writable_note_path(destination):
                _deny()
            return destination
        if _under(source, EXCALIDRAW_ROOT):
            _deny()
        if not _is_writable_note_path(source):
            _deny()
        dest_denials(destination, policy, allow_archive=True)
        if not _is_writable_note_path(destination) and not _strictly_under(destination, ARCHIVE_ROOT):
            _deny()
        return destination

    normalized = normalize_vault_path(path)
    dest_denials(normalized, policy)
    if kind in {"create_excalidraw", "update_excalidraw"}:
        if not _strictly_under(normalized, EXCALIDRAW_ROOT):
            _deny()
        if not _has_verified_excalidraw_suffix(normalized, str(policy.get("excalidrawSuffix", ""))):
            _deny()
        return normalized
    if _under(normalized, EXCALIDRAW_ROOT):
        _deny()
    if kind in {"create_base", "patch_base"}:
        dashboard = str(policy.get("dashboardRoot", ""))
        allowed_dashboard = dashboard.endswith(".base") and normalized == dashboard
        allowed_project = _strictly_under(normalized, PROJECTS_ROOT) and normalized.endswith(".base")
        if not allowed_dashboard and not allowed_project:
            _deny()
        return normalized
    if kind in CANVAS_WRITE_KINDS:
        if not normalized.endswith(".canvas") or not _is_writable_note_path(normalized):
            _deny()
        return normalized
    if kind not in NOTE_WRITE_KINDS:
        _deny()
    if not _is_ordinary_markdown(normalized) or not _is_writable_note_path(normalized):
        _deny()
    return normalized


def assert_writable(
    kind: str,
    path: object,
    dest_or_policy: object = None,
    maybe_policy: Mapping[str, Any] | None = None,
) -> str:
    return _assert_write_kind(kind, path, dest_or_policy, maybe_policy, assert_readable, _assert_common_denials)


def assert_static_writable(
    kind: str,
    path: object,
    dest_or_policy: object = None,
    maybe_policy: Mapping[str, Any] | None = None,
) -> str:
    return _assert_write_kind(
        kind,
        path,
        dest_or_policy,
        maybe_policy,
        assert_static_readable,
        _assert_static_denials,
    )
