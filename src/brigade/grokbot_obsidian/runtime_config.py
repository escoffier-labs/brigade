"""Private Obsidian runtime JSON and environment path validation."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any, Mapping, NoReturn

from .. import grokbot_ops
from .contracts import (
    PHASE1_ACTION_IDS,
    PHASE2_ONLY_ACTION_IDS,
    ObsidianError,
    require_utf8,
)
from .content_policy import contains_executable_markdown
from .utf8 import utf8_in_range

SPKI_PIN = __import__("re").compile(r"^sha256:[A-Za-z0-9+/]{43}=$")
COMMAND_FINGERPRINT = __import__("re").compile(r"^sha256:[0-9a-f]{64}$")
MAX_CA_BYTES = 16_384
MAX_RECEIPT_BYTES = 8_192
MAX_RUNTIME_JSON_BYTES = 262_144
ADAPTER_ENVIRONMENT_KEYS = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "XDG_RUNTIME_DIR",
    "DBUS_SESSION_BUS_ADDRESS",
)


def _environment_error() -> NoReturn:
    raise ObsidianError("invalid_request", "Obsidian environment is invalid")


def _runtime_error() -> NoReturn:
    raise ObsidianError("invalid_request", "Obsidian runtime configuration is invalid")


def has_explicit_dot_segment(value: str) -> bool:
    return any(segment in {".", ".."} for segment in value.split("/"))


def is_absolute_safe_path(value: str) -> bool:
    return value.startswith("/") and "\0" not in value and not has_explicit_dot_segment(value)


def normalize_absolute_path(path_value: str) -> str:
    trimmed = os.path.normpath(path_value).rstrip("/")
    return trimmed if trimmed else "/"


def required_absolute_path(value: object) -> str:
    if not isinstance(value, str) or not is_absolute_safe_path(value):
        _environment_error()
    return normalize_absolute_path(value)


def paths_overlap(left: str, right: str) -> bool:
    normalized_left = normalize_absolute_path(left)
    normalized_right = normalize_absolute_path(right)
    if normalized_left == normalized_right:
        return True
    return normalized_left.startswith(f"{normalized_right}/") or normalized_right.startswith(f"{normalized_left}/")


def assert_disjoint_paths(paths: list[str]) -> None:
    for left_index, left in enumerate(paths):
        for right in paths[left_index + 1 :]:
            if paths_overlap(left, right):
                _environment_error()


def _lstat_nofollow(path_text: str) -> os.stat_result:
    path = Path(path_text)
    if grokbot_ops._path_is_symlink(path) or path.is_symlink():
        _environment_error()
    parent = -1
    try:
        parent = grokbot_ops._open_parent_nofollow(path, create=False)
        info = os.stat(path.name, dir_fd=parent, follow_symlinks=False) if os.name == "posix" else path.lstat()
    except OSError as exc:
        raise ObsidianError("invalid_request", "Obsidian environment is invalid") from exc
    finally:
        if parent != -1:
            os.close(parent)
    if stat.S_ISLNK(info.st_mode):
        _environment_error()
    return info


def validate_regular_executable(path_text: str) -> str:
    normalized = required_absolute_path(path_text)
    info = _lstat_nofollow(normalized)
    if not stat.S_ISREG(info.st_mode) or (info.st_mode & 0o111) == 0:
        _environment_error()
    return normalized


def descriptor_execution_available() -> bool:
    return os.name == "posix" and os.path.isdir("/proc/self/fd")


def open_validated_executable_fd(path_text: str) -> int:
    normalized = validate_regular_executable(path_text)
    path = Path(normalized)
    parent = -1
    descriptor = -1
    try:
        parent = grokbot_ops._open_parent_nofollow(path, create=False)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        if os.name != "posix":
            _environment_error()
        descriptor = os.open(path.name, flags, dir_fd=parent)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or (info.st_mode & 0o111) == 0:
            _environment_error()
        current = _lstat_nofollow(normalized)
        if (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
            _environment_error()
        kept = descriptor
        descriptor = -1
        return kept
    except ObsidianError:
        raise
    except OSError as exc:
        raise ObsidianError("invalid_request", "Obsidian environment is invalid") from exc
    finally:
        if descriptor != -1:
            os.close(descriptor)
        if parent != -1:
            os.close(parent)


def _assert_secure_runtime_stat(info: os.stat_result) -> None:
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
        _runtime_error()
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        _runtime_error()


def _read_secure_runtime_bytes(path_text: str, *, max_bytes: int) -> bytes:
    """Read a current-UID-owned mode-0600 file through one no-follow descriptor."""
    normalized = required_absolute_path(path_text)
    path = Path(normalized)
    try:
        prior = os.lstat(normalized)
    except OSError as exc:
        raise ObsidianError("invalid_request", "Obsidian runtime configuration is invalid") from exc
    attributes = getattr(prior, "st_file_attributes", 0)
    reparse_point = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if stat.S_ISLNK(prior.st_mode) or bool(attributes & reparse_point):
        _runtime_error()
    _assert_secure_runtime_stat(prior)
    parent = -1
    descriptor = -1
    try:
        parent = grokbot_ops._open_parent_nofollow(path, create=False)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        if os.name == "posix":
            descriptor = os.open(path.name, flags, dir_fd=parent)
        else:
            from ..work_cmd import nt_dirfd

            descriptor = nt_dirfd.open_file(parent, path.name, os.O_RDONLY)
        info = os.fstat(descriptor)
        _assert_secure_runtime_stat(info)
        if (info.st_dev, info.st_ino) != (prior.st_dev, prior.st_ino):
            _runtime_error()
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, 4096)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                _runtime_error()
            chunks.append(chunk)
        after = os.lstat(normalized)
        if (after.st_dev, after.st_ino) != (info.st_dev, info.st_ino):
            _runtime_error()
        raw = b"".join(chunks)
        if not raw:
            _runtime_error()
        return raw
    except ObsidianError:
        raise
    except OSError as exc:
        raise ObsidianError("invalid_request", "Obsidian runtime configuration is invalid") from exc
    finally:
        if descriptor != -1:
            os.close(descriptor)
        if parent != -1:
            os.close(parent)


def read_secure_runtime_bytes(path_text: str, *, max_bytes: int) -> bytes:
    return _read_secure_runtime_bytes(path_text, max_bytes=max_bytes)


def read_secure_runtime_text(path_text: str, *, max_bytes: int = MAX_RUNTIME_JSON_BYTES) -> str:
    raw = _read_secure_runtime_bytes(path_text, max_bytes=max_bytes)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ObsidianError("invalid_request", "Obsidian runtime configuration is invalid") from exc


def _unique_strings(values: list[str]) -> bool:
    return len(set(values)) == len(values)


def parse_private_runtime(raw: object, *, filesystem_read=None) -> dict[str, Any]:
    if not isinstance(raw, dict):
        _runtime_error()
    required = {
        "version",
        "upstream_tls",
        "templates",
        "home_note",
        "flashcard_note",
        "flashcard_heading",
        "dashboard_root",
        "daily_notes_folder",
        "sensitive_tags",
        "sensitive_path_prefixes",
        "command_fingerprint",
        "plugin_inventory",
        "excalidraw",
    }
    allowed = set(required)
    if "operator_adapter" in raw:
        allowed = allowed | {"operator_adapter"}
    if set(raw) != allowed or raw.get("version") != 1:
        _runtime_error()
    tls = raw.get("upstream_tls")
    if not isinstance(tls, dict) or set(tls) != {"ca_path", "spki_sha256"}:
        _runtime_error()
    ca_path = required_absolute_path(tls.get("ca_path"))
    pins = tls.get("spki_sha256")
    if not isinstance(pins, list) or not 1 <= len(pins) <= 2:
        _runtime_error()
    parsed_pins = []
    for pin in pins:
        if not isinstance(pin, str) or not SPKI_PIN.fullmatch(pin):
            _runtime_error()
        parsed_pins.append(pin)
    templates = raw.get("templates")
    if not isinstance(templates, dict) or set(templates) != {"catalog"}:
        _runtime_error()
    catalog_raw = templates.get("catalog")
    if not isinstance(catalog_raw, list) or len(catalog_raw) > 16:
        _runtime_error()
    catalog = []
    for entry in catalog_raw:
        if not isinstance(entry, dict) or set(entry) != {"id", "body", "variables"}:
            _runtime_error()
        body = entry.get("body")
        if not utf8_in_range(body, 1, 12_288) or contains_executable_markdown(str(body)):
            _runtime_error()
        variables = entry.get("variables")
        if not isinstance(variables, dict):
            _runtime_error()
        parsed_vars = {}
        for key, kind in variables.items():
            if not isinstance(key, str) or kind not in {"string", "string_array", "number", "boolean"}:
                _runtime_error()
            parsed_vars[key] = kind
        catalog.append({"id": require_utf8(entry.get("id"), 1, 64), "body": body, "variables": parsed_vars})
    plugins_raw = raw.get("plugin_inventory")
    if not isinstance(plugins_raw, list) or len(plugins_raw) > 32:
        _runtime_error()
    plugins: list[dict[str, Any]] = []
    for plugin in plugins_raw:
        if not isinstance(plugin, dict) or set(plugin) != {"id", "version", "supported_action_ids"}:
            _runtime_error()
        action_ids = plugin.get("supported_action_ids")
        if not isinstance(action_ids, list) or len(action_ids) > 16:
            _runtime_error()
        parsed_ids = []
        for action_id in action_ids:
            if not isinstance(action_id, str) or not 1 <= len(action_id) <= 64:
                _runtime_error()
            if action_id not in PHASE1_ACTION_IDS or action_id in PHASE2_ONLY_ACTION_IDS:
                _runtime_error()
            parsed_ids.append(action_id)
        if not _unique_strings(parsed_ids):
            _runtime_error()
        plugins.append(
            {
                "id": require_utf8(plugin.get("id"), 1, 64),
                "version": require_utf8(plugin.get("version"), 1, 32),
                "supported_action_ids": parsed_ids,
            }
        )
    if not _unique_strings([plugin["id"] for plugin in plugins]):
        _runtime_error()
    fingerprint = raw.get("command_fingerprint")
    if not isinstance(fingerprint, str) or not COMMAND_FINGERPRINT.fullmatch(fingerprint):
        _runtime_error()
    tags = raw.get("sensitive_tags")
    prefixes = raw.get("sensitive_path_prefixes")
    if not isinstance(tags, list) or not isinstance(prefixes, list) or len(tags) > 32 or len(prefixes) > 32:
        _runtime_error()
    excalidraw = raw.get("excalidraw")
    if not isinstance(excalidraw, dict):
        _runtime_error()
    allowed = {"enabled", "verified_suffix", "probe_receipt_sha256"}
    if "probe_receipt_path" in excalidraw:
        allowed = allowed | {"probe_receipt_path"}
    if set(excalidraw) != allowed:
        _runtime_error()
    suffix = excalidraw.get("verified_suffix")
    receipt_hash = excalidraw.get("probe_receipt_sha256")
    enabled = excalidraw.get("enabled")
    if enabled not in {True, False} or suffix not in {".excalidraw.md", ".excalidraw"}:
        _runtime_error()
    if not isinstance(receipt_hash, str) or not COMMAND_FINGERPRINT.fullmatch(receipt_hash):
        _runtime_error()
    reader = filesystem_read or (lambda path, maximum: read_secure_runtime_bytes(path, max_bytes=maximum))
    ca_bytes = reader(ca_path, MAX_CA_BYTES)
    receipt_bytes = None
    if enabled is True:
        receipt_path = excalidraw.get("probe_receipt_path")
        if not isinstance(receipt_path, str):
            _runtime_error()
        receipt_bytes = reader(required_absolute_path(receipt_path), MAX_RECEIPT_BYTES)
    adapter = None
    if "operator_adapter" in raw:
        adapter_raw = raw.get("operator_adapter")
        if not isinstance(adapter_raw, dict) or set(adapter_raw) != {"manifest_version", "tool_fingerprint"}:
            _runtime_error()
        adapter_version = adapter_raw.get("manifest_version")
        adapter_fingerprint = adapter_raw.get("tool_fingerprint")
        if not isinstance(adapter_version, str) or not 1 <= len(adapter_version) <= 32:
            _runtime_error()
        if not isinstance(adapter_fingerprint, str) or not COMMAND_FINGERPRINT.fullmatch(adapter_fingerprint):
            _runtime_error()
        adapter = {"manifest_version": adapter_version, "tool_fingerprint": adapter_fingerprint}
    parsed_runtime = {
        "version": 1,
        "upstream_tls": {
            "ca_path": ca_path,
            "ca_bytes": ca_bytes,
            "spki_sha256": tuple(parsed_pins),
        },
        "templates": {"catalog": catalog},
        "home_note": require_utf8(raw.get("home_note"), 1, 256),
        "flashcard_note": require_utf8(raw.get("flashcard_note"), 1, 256),
        "flashcard_heading": require_utf8(raw.get("flashcard_heading"), 1, 120),
        "dashboard_root": require_utf8(raw.get("dashboard_root"), 1, 256),
        "daily_notes_folder": require_utf8(raw.get("daily_notes_folder"), 0, 256),
        "sensitive_tags": tuple(require_utf8(tag, 1, 64) for tag in tags),
        "sensitive_path_prefixes": tuple(require_utf8(prefix, 1, 256) for prefix in prefixes),
        "command_fingerprint": fingerprint,
        "plugin_inventory": plugins,
        "excalidraw": {
            "enabled": enabled,
            "verified_suffix": suffix,
            "probe_receipt_sha256": receipt_hash,
            "probe_receipt_bytes": receipt_bytes,
        },
    }
    if adapter is not None:
        parsed_runtime["operator_adapter"] = adapter
    return parsed_runtime


def required_loopback_host(value: object) -> str:
    if value in {"127.0.0.1", "::1"}:
        return str(value)
    _environment_error()
    raise AssertionError("unreachable")


def required_upstream_url(value: object) -> str:
    if not isinstance(value, str) or any(char in value for char in "?#@"):
        _environment_error()
    from urllib.parse import urlparse

    try:
        parsed = urlparse(value)
    except ValueError:
        _environment_error()
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.query or parsed.fragment:
        _environment_error()
    host = parsed.hostname
    if host not in {"127.0.0.1", "::1"}:
        _environment_error()
    if parsed.port is not None and not 1 <= parsed.port <= 65_535:
        _environment_error()
    return value


def required_token(value: object, peers: Mapping[str, str] | None = None) -> str:
    if not isinstance(value, str) or len(value) < 32:
        _environment_error()
    if peers is not None:
        for peer in peers.values():
            if peer and value == peer:
                _environment_error()
    return value


def load_runtime_env(env: Mapping[str, str], *, peers: Mapping[str, str] | None = None) -> dict[str, Any]:
    if "NODE_TLS_REJECT_UNAUTHORIZED" in env:
        _environment_error()
    runtime_path = required_absolute_path(env.get("GROKBOT_OBSIDIAN_RUNTIME_PATH"))
    action_state_path = required_absolute_path(env.get("GROKBOT_OBSIDIAN_ACTION_STATE_PATH"))
    approval_dir = required_absolute_path(env.get("GROKBOT_OBSIDIAN_APPROVAL_DIR"))
    staging_dir = required_absolute_path(env.get("GROKBOT_OBSIDIAN_STAGING_DIR"))
    excalidraw_bin = validate_regular_executable(required_absolute_path(env.get("GROKBOT_OBSIDIAN_EXCALIDRAW_BIN")))
    assert_disjoint_paths([runtime_path, action_state_path, approval_dir, staging_dir])
    port_text = env.get("GROKBOT_OBSIDIAN_PORT")
    if port_text is None:
        port = 8773
    else:
        if not port_text.isdigit():
            _environment_error()
        port = int(port_text)
        if not 1 <= port <= 65_535:
            _environment_error()
    upstream_key = env.get("GROKBOT_OBSIDIAN_UPSTREAM_KEY")
    if not isinstance(upstream_key, str) or len(upstream_key) < 16:
        _environment_error()
    return {
        "token": required_token(env.get("GROKBOT_OBSIDIAN_TOKEN"), peers),
        "host": required_loopback_host(env.get("GROKBOT_OBSIDIAN_HOST")),
        "port": port,
        "upstream_url": required_upstream_url(env.get("GROKBOT_OBSIDIAN_UPSTREAM_URL")),
        "upstream_key": upstream_key,
        "runtime_path": runtime_path,
        "action_state_path": action_state_path,
        "approval_dir": approval_dir,
        "staging_dir": staging_dir,
        "excalidraw_bin": excalidraw_bin,
    }


def adapter_environment(source: Mapping[str, str]) -> dict[str, str]:
    return {key: source[key] for key in ADAPTER_ENVIRONMENT_KEYS if key in source}


def parse_runtime_json_text(text: str, *, filesystem_read=None) -> dict[str, Any]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        _runtime_error()
    return parse_private_runtime(payload, filesystem_read=filesystem_read)
