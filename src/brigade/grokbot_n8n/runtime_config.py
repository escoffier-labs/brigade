"""Private n8n-operator runtime JSON, URL rules, and secure file reads."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, NoReturn
from urllib.parse import urlsplit, urlunsplit

from .contracts import N8nError

RUNTIME_KEYS = frozenset({"version", "base_url", "api_key_file"})
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
MAX_RUNTIME_BYTES = 16_384
MAX_API_KEY_BYTES = 1_024


def _runtime_error() -> NoReturn:
    raise N8nError("invalid_request", "n8n runtime configuration is invalid")


def _environment_error() -> NoReturn:
    raise N8nError("invalid_request", "n8n environment is invalid")


def _has_explicit_dot_segment(value: str) -> bool:
    return any(segment in {".", ".."} for segment in value.split("/"))


def normalize_absolute_path(path_value: str) -> str:
    trimmed = os.path.normpath(path_value).rstrip("/")
    return trimmed if trimmed else "/"


def required_absolute_path(value: object) -> str:
    if not isinstance(value, str) or not value.startswith("/") or "\0" in value:
        _environment_error()
    if _has_explicit_dot_segment(value):
        _environment_error()
    return normalize_absolute_path(value)


def paths_overlap(left: str, right: str) -> bool:
    normalized_left = normalize_absolute_path(left)
    normalized_right = normalize_absolute_path(right)
    if normalized_left == normalized_right:
        return True
    return normalized_left.startswith(f"{normalized_right}/") or normalized_right.startswith(f"{normalized_left}/")


def assert_disjoint_paths(paths: list[str]) -> None:
    normalized = [required_absolute_path(path) for path in paths]
    for left_index, left in enumerate(normalized):
        for right in normalized[left_index + 1 :]:
            if paths_overlap(left, right):
                _environment_error()


def permission_policy() -> str:
    return "posix" if os.name == "posix" else "unsupported"


def _require_posix_permissions() -> None:
    if permission_policy() != "posix":
        _environment_error()


def validate_base_url(value: object) -> str:
    if not isinstance(value, str) or not value or "\0" in value:
        _runtime_error()
    if "%" in value or any(ord(char) < 32 for char in value):
        _runtime_error()
    try:
        parsed = urlsplit(value)
    except ValueError:
        _runtime_error()
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        _runtime_error()
    if parsed.path not in {"", "/"}:
        _runtime_error()
    if parsed.scheme not in {"http", "https"}:
        _runtime_error()
    try:
        port = parsed.port
    except ValueError:
        _runtime_error()
    if port is not None and not 1 <= port <= 65535:
        _runtime_error()
    host = (parsed.hostname or "").casefold()
    if not host or "%" in host or any(ord(char) < 32 for char in host):
        _runtime_error()
    raw_netloc = parsed.netloc
    if not raw_netloc or raw_netloc.endswith(":") or "%" in raw_netloc:
        _runtime_error()
    if parsed.scheme == "http" and host not in LOOPBACK_HOSTS:
        _runtime_error()
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc = f"{netloc}:{port}"
    return urlunsplit((parsed.scheme, netloc, "", "", ""))


def _assert_secure_stat(info: os.stat_result) -> None:
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
        _environment_error()
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        _environment_error()


def _file_identity(info: os.stat_result) -> tuple[int, int]:
    return (info.st_dev, info.st_ino)


def _open_secure_file(path_text: str) -> tuple[int, os.stat_result]:
    from .. import grokbot_ops

    _require_posix_permissions()
    normalized = required_absolute_path(path_text)
    path = Path(normalized)
    try:
        prior = os.lstat(normalized)
    except OSError as exc:
        raise N8nError("invalid_request", "n8n environment is invalid") from exc
    attributes = getattr(prior, "st_file_attributes", 0)
    reparse_point = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if stat.S_ISLNK(prior.st_mode) or bool(attributes & reparse_point):
        _environment_error()
    _assert_secure_stat(prior)
    parent = -1
    descriptor = -1
    try:
        parent = grokbot_ops._open_parent_nofollow(path, create=False)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(path.name, flags, dir_fd=parent)
        info = os.fstat(descriptor)
        _assert_secure_stat(info)
        if _file_identity(info) != _file_identity(prior) or info.st_nlink != 1:
            _environment_error()
        os.close(parent)
        parent = -1
        return descriptor, info
    except N8nError:
        if descriptor != -1:
            os.close(descriptor)
        if parent != -1:
            os.close(parent)
        raise
    except OSError as exc:
        if descriptor != -1:
            os.close(descriptor)
        if parent != -1:
            os.close(parent)
        raise N8nError("invalid_request", "n8n environment is invalid") from exc


def _read_bounded_fd(descriptor: int, *, maximum: int) -> bytes:
    payload = bytearray()
    while True:
        chunk = os.read(descriptor, min(4096, maximum + 1 - len(payload)))
        if not chunk:
            break
        payload.extend(chunk)
        if len(payload) > maximum:
            _environment_error()
    return bytes(payload)


def _read_secure_text(path_text: str, *, maximum: int) -> str:
    descriptor = -1
    try:
        descriptor, info = _open_secure_file(path_text)
        if info.st_size > maximum:
            _environment_error()
        try:
            return _read_bounded_fd(descriptor, maximum=maximum).decode("utf-8")
        except UnicodeDecodeError:
            _environment_error()
    finally:
        if descriptor != -1:
            os.close(descriptor)


def assert_distinct_secret_files(left: str, right: str) -> None:
    first = -1
    second = -1
    try:
        first, left_info = _open_secure_file(left)
        second, right_info = _open_secure_file(right)
        if _file_identity(left_info) == _file_identity(right_info):
            _environment_error()
    finally:
        if first != -1:
            os.close(first)
        if second != -1:
            os.close(second)


def read_secure_runtime_text(path_text: str) -> str:
    """Read a current-UID-owned mode-0600 runtime file through one no-follow descriptor."""
    return _read_secure_text(path_text, maximum=MAX_RUNTIME_BYTES)


def read_secure_api_key(path_text: str) -> str:
    text = _read_secure_text(path_text, maximum=MAX_API_KEY_BYTES)
    key = text.splitlines()[0].strip() if text else ""
    if not key or not key.isascii() or not 8 <= len(key) <= 512 or any(ord(char) < 32 for char in key):
        _environment_error()
    return key


def parse_n8n_private_runtime(raw: object) -> MappingProxyType:
    if not isinstance(raw, Mapping) or set(raw) != RUNTIME_KEYS:
        _runtime_error()
    if raw.get("version") != 1:
        _runtime_error()
    base_url = validate_base_url(raw.get("base_url"))
    api_key_file = required_absolute_path(raw.get("api_key_file"))
    return MappingProxyType({"version": 1, "base_url": base_url, "api_key_file": api_key_file})


def parse_runtime_json(text: str) -> MappingProxyType:
    try:
        payload = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        _runtime_error()
    return parse_n8n_private_runtime(payload)
