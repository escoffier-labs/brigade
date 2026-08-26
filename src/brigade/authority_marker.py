"""User-level sticky marker: this target's authority store has been signed.

Once a workspace is HMAC-signed, a marker is recorded under the operator
``~/.brigade`` directory (override: ``BRIGADE_USER_DIR``), outside every repo
workspace. A raw, non-enveloped authority record for a marked target fails
closed regardless of the repo-writable ``security.toml`` flag.

The sibling ``authority-isolation`` directory records the per-target
isolation posture (#881): the first time genuine ``external-key`` isolation
is observed for a workspace, a marker is written here so the repo-writable
flag alone never remains the only evidence of that decision. The unsigned
dedupe fallback refuses while this marker exists; clearing requires the
explicit ``brigade security authority downgrade`` command.

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
ISOLATION_MARKER_DIRNAME = "authority-isolation"
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


def isolation_marker_root(*, env: Mapping[str, str] | None = None) -> Path:
    return user_brigade_dir(env=env) / ISOLATION_MARKER_DIRNAME


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


def isolation_marker_path(fingerprint: str, *, env: Mapping[str, str] | None = None) -> Path:
    return isolation_marker_root(env=env) / _require_fingerprint(fingerprint)


def _fingerprint_for(
    target: Path | None,
    record: Mapping[str, Any] | None = None,
) -> str | None:
    if target is not None:
        return target_fingerprint(target)
    return target_fingerprint_from_record(record)


def _marker_enters_workspace(path: Path, workspace: Path) -> bool:
    """True when the marker would sit inside ``workspace``.

    Same lexical / symlink / ``..`` rigor as the HMAC key check, but without
    the key's ``.brigade``-component heuristic. The default marker home is
    ``~/.brigade``, which must remain legal.
    """

    from . import authority_key

    unresolved = authority_key._absolute_unresolved(path)
    work = authority_key._absolute_unresolved(workspace)
    if authority_key._parts_prefix(unresolved, work):
        return True
    if authority_key._workspace_symlink_component(unresolved, work):
        return True
    return authority_key.key_is_inside_tree(path, workspace)


def _marker_is_under_user_brigade_dir(path: Path, *, env: Mapping[str, str] | None = None) -> bool:
    """True when ``path`` is under the configured user-level brigade directory."""

    from . import authority_key

    user = user_brigade_dir(env=env)
    unresolved = authority_key._absolute_unresolved(path)
    user_unresolved = authority_key._absolute_unresolved(user)
    if not authority_key._parts_prefix(unresolved, user_unresolved):
        return False
    return authority_key.key_is_inside_tree(path, user)


def reject_unsafe_marker_path(
    path: Path,
    workspace: Path,
    *,
    env: Mapping[str, str] | None = None,
) -> None:
    """Refuse a marker inside the workspace or outside the user-level brigade dir."""

    if _marker_enters_workspace(path, workspace):
        raise OSError("authority signed marker must not be written inside the workspace")
    if not _marker_is_under_user_brigade_dir(path, env=env):
        raise OSError("authority signed marker must live under the user-level brigade directory")


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
    if target is not None and _marker_enters_workspace(path, target):
        return False
    if not _marker_is_under_user_brigade_dir(path, env=env):
        return False
    try:
        return path.is_file() and not path.is_symlink()
    except OSError:
        return False


def isolation_marker_exists(
    target: Path | None,
    *,
    record: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
) -> bool:
    """True when the user-level isolation posture marker exists for this target.

    Same location discipline as ``marker_exists``: only the configured
    user-level ``authority-isolation`` directory is consulted, and a marker
    whose path sits inside ``target`` does not count.
    """

    fingerprint = _fingerprint_for(target, record)
    if fingerprint is None:
        return False
    try:
        path = isolation_marker_path(fingerprint, env=env)
    except OSError:
        return False
    if target is not None and _marker_enters_workspace(path, target):
        return False
    if not _marker_is_under_user_brigade_dir(path, env=env):
        return False
    try:
        return path.is_file() and not path.is_symlink()
    except OSError:
        return False


def _publish_private_marker(path: Path, payload: dict[str, Any]) -> None:
    """Atomically write a 0600 marker JSON payload, replacing any old bytes."""

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
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


def record_signed_marker(
    target: Path,
    *,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Create the sticky marker after a signed envelope is written. Idempotent."""

    fingerprint = target_fingerprint(target)
    path = signed_marker_path(fingerprint, env=env)
    reject_unsafe_marker_path(path, target, env=env)
    if path.is_file() and not path.is_symlink():
        return path
    payload = {
        "schema_version": 1,
        "target": str(target.expanduser().resolve()),
        "target_fingerprint": fingerprint,
        "signed_by": operator_identity(env=env),
        "created_at": utc_now_iso_z(),
    }
    _publish_private_marker(path, payload)
    return path


def record_isolation_marker(
    target: Path,
    *,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Create the sticky isolation-posture marker for ``target``. Idempotent.

    Recorded the first time genuine ``external-key`` isolation is observed so
    the repo-writable ``security.toml`` flag alone never remains the only
    record of that decision (#881). Raises ``OSError`` for unsafe paths;
    read-path callers that cannot persist should treat that as best effort.
    """

    fingerprint = target_fingerprint(target)
    path = isolation_marker_path(fingerprint, env=env)
    reject_unsafe_marker_path(path, target, env=env)
    if path.is_file() and not path.is_symlink():
        return path
    payload = {
        "schema_version": 1,
        "kind": "isolation-posture",
        "target": str(target.expanduser().resolve()),
        "target_fingerprint": fingerprint,
        "recorded_by": operator_identity(env=env),
        "created_at": utc_now_iso_z(),
    }
    _publish_private_marker(path, payload)
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
    try:
        if path.exists() and (path.is_dir() or path.is_symlink() or not path.is_file()):
            raise OSError("authority downgrade audit is not a regular file")
    except OSError as exc:
        if "not a regular file" in str(exc):
            raise
        raise OSError("authority downgrade audit is unavailable") from exc
    line = json.dumps(dict(record), sort_keys=True, separators=(",", ":")) + "\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise OSError("authority downgrade audit is unavailable") from exc
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


def _restore_marker_bytes(path: Path, data: bytes) -> None:
    """Put the exact pre-downgrade marker bytes back after a failed audit."""

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.restore.tmp")
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(temporary, flags, 0o600)
        try:
            with os.fdopen(os.dup(descriptor), "wb") as handle:
                handle.write(data)
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


def remove_signed_marker(
    target: Path,
    *,
    actor: str | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Remove the sticky marker only after a durable downgrade audit.

    Intent is logged first. If completion logging fails, the exact marker
    bytes are restored so trust is never dropped without an audit record.
    """

    fingerprint = target_fingerprint(target)
    path = signed_marker_path(fingerprint, env=env)
    reject_unsafe_marker_path(path, target, env=env)
    try:
        marker_bytes = path.read_bytes()
    except FileNotFoundError:
        return {
            "action": "authority-downgrade",
            "actor": actor if actor is not None else operator_identity(env=env),
            "created_at": utc_now_iso_z(),
            "phase": "complete",
            "removed": False,
            "target": str(target.expanduser().resolve()),
            "target_fingerprint": fingerprint,
        }
    except OSError as exc:
        raise OSError("authority signed marker is unreadable") from exc
    actor_value = actor if actor is not None else operator_identity(env=env)
    resolved = str(target.expanduser().resolve())
    intent = {
        "action": "authority-downgrade",
        "actor": actor_value,
        "created_at": utc_now_iso_z(),
        "phase": "intent",
        "removed": False,
        "target": resolved,
        "target_fingerprint": fingerprint,
    }
    try:
        append_audit(intent, env=env)
    except OSError as exc:
        raise OSError("authority downgrade audit is unavailable; marker not removed") from exc
    try:
        path.unlink()
    except FileNotFoundError:
        return {
            **intent,
            "phase": "complete",
            "removed": False,
        }
    completion = {
        "action": "authority-downgrade",
        "actor": actor_value,
        "created_at": utc_now_iso_z(),
        "phase": "complete",
        "removed": True,
        "target": resolved,
        "target_fingerprint": fingerprint,
    }
    try:
        completion["audit_path"] = str(append_audit(completion, env=env))
    except OSError as exc:
        try:
            _restore_marker_bytes(path, marker_bytes)
        except OSError as restore_exc:
            raise OSError("authority downgrade audit failed and marker restore failed") from restore_exc
        raise OSError("authority downgrade audit failed; marker restored") from exc
    return completion
