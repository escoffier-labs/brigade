"""User-level sticky marker: this target's authority store has been signed.

Once a workspace is HMAC-signed, a marker is recorded under the operator
``~/.brigade`` directory (override: ``BRIGADE_USER_DIR``), outside every repo
workspace. A raw, non-enveloped authority record for a marked target fails
closed regardless of the repo-writable ``security.toml`` flag.

This is defense in depth, not a hard boundary: a same-uid process that can
also write the user-level Brigade directory can still remove the marker.
``brigade security authority downgrade`` is the explicit, logged, confirmed
path. Tests must set ``BRIGADE_USER_DIR`` so nothing is written to the real
home directory.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .localio import utc_now_iso_z

USER_DIR_ENV = "BRIGADE_USER_DIR"
SIGNED_MARKER_DIRNAME = "authority-signed"
AUDIT_NAME = "audit.jsonl"
_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")


def user_brigade_dir(*, env: Mapping[str, str] | None = None) -> Path:
    """Return the user-level ``~/.brigade`` directory (never a workspace path)."""

    environment = env if env is not None else os.environ
    configured = environment.get(USER_DIR_ENV)
    if configured:
        return Path(configured).expanduser()
    home = environment.get("HOME")
    if home:
        return Path(home) / ".brigade"
    return Path.home() / ".brigade"


def signed_marker_root(*, env: Mapping[str, str] | None = None) -> Path:
    return user_brigade_dir(env=env) / SIGNED_MARKER_DIRNAME


def target_fingerprint(target: Path) -> str:
    """Fingerprint a workspace the same way the authority store filename does."""

    resolved = str(target.expanduser().resolve())
    return hashlib.sha256(resolved.encode("utf-8")).hexdigest()


def target_fingerprint_from_record(record: Mapping[str, Any] | None) -> str | None:
    if record is None:
        return None
    raw = record.get("target")
    if not isinstance(raw, str) or not raw:
        return None
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _require_fingerprint(fingerprint: str) -> str:
    if not _FINGERPRINT_RE.fullmatch(fingerprint):
        raise OSError("authority signed-marker fingerprint is malformed")
    return fingerprint


def signed_marker_path(fingerprint: str, *, env: Mapping[str, str] | None = None) -> Path:
    return signed_marker_root(env=env) / _require_fingerprint(fingerprint)


def _fingerprint_for(
    target: Path | None,
    record: Mapping[str, Any] | None = None,
) -> str | None:
    if target is not None:
        return target_fingerprint(target)
    return target_fingerprint_from_record(record)


def _marker_is_inside_workspace(path: Path, workspace: Path) -> bool:
    from . import authority_key

    return authority_key.key_path_is_scanner_reachable(path, workspace)


def marker_exists(
    target: Path | None,
    *,
    record: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
) -> bool:
    """True when the user-level sticky marker exists for this target.

    Workspace-relative plants are ignored: only the configured user-level
    ``authority-signed`` directory is consulted, and a marker whose path sits
    inside ``target`` does not count.
    """

    fingerprint = _fingerprint_for(target, record)
    if fingerprint is None:
        return False
    try:
        path = signed_marker_path(fingerprint, env=env)
    except OSError:
        return False
    if target is not None and _marker_is_inside_workspace(path, target):
        return False
    try:
        return path.is_file() and not path.is_symlink()
    except OSError:
        return False


def record_signed_marker(
    target: Path,
    *,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Create the sticky marker after a signed envelope is written. Idempotent."""

    fingerprint = target_fingerprint(target)
    path = signed_marker_path(fingerprint, env=env)
    if _marker_is_inside_workspace(path, target):
        raise OSError("authority signed marker must not be written inside the workspace")
    if path.is_file() and not path.is_symlink():
        return path
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    payload = {
        "schema_version": 1,
        "target": str(target.expanduser().resolve()),
        "target_fingerprint": fingerprint,
        "signed_by": operator_identity(env=env),
        "created_at": utc_now_iso_z(),
    }
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(temporary, flags, 0o600)
        try:
            with os.fdopen(os.dup(descriptor), "w", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, sort_keys=True, separators=(",", ":")))
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return path


def audit_path(*, env: Mapping[str, str] | None = None) -> Path:
    return signed_marker_root(env=env) / AUDIT_NAME


def operator_identity(*, env: Mapping[str, str] | None = None) -> str:
    """Return the current operator identity for signing and downgrade audit.

    Prefer ``USER`` so a test or operator shell can stamp the acting account
    without losing to ``LOGNAME`` (the first key ``getpass.getuser()`` reads).
    """

    environment = env if env is not None else os.environ
    for key in ("USER", "USERNAME", "LOGNAME"):
        value = environment.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    try:
        import getpass

        return getpass.getuser()
    except Exception:
        return "unknown"


def actor_name(*, env: Mapping[str, str] | None = None) -> str:
    return operator_identity(env=env)


def append_audit(record: Mapping[str, Any], *, env: Mapping[str, str] | None = None) -> Path:
    path = audit_path(env=env)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    line = json.dumps(dict(record), sort_keys=True, separators=(",", ":")) + "\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(os.dup(descriptor), "a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def remove_signed_marker(
    target: Path,
    *,
    actor: str | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Remove the sticky marker and append a security audit line."""

    fingerprint = target_fingerprint(target)
    path = signed_marker_path(fingerprint, env=env)
    if _marker_is_inside_workspace(path, target):
        raise OSError("authority signed marker path is inside the workspace")
    try:
        path.unlink()
        removed = True
    except FileNotFoundError:
        removed = False
    payload = {
        "action": "authority-downgrade",
        "actor": actor if actor is not None else operator_identity(env=env),
        "created_at": utc_now_iso_z(),
        "removed": removed,
        "target": str(target.expanduser().resolve()),
        "target_fingerprint": fingerprint,
    }
    if removed:
        payload["audit_path"] = str(append_audit(payload, env=env))
    return payload
