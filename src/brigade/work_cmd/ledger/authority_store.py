"""Stage 1 compatibility seam for the task and import ledger."""
# ruff: noqa: F401

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence, cast
from uuid import uuid4

from .. import constants, edges as edges_mod, helpers, inbox_lock
from ..inbox_lock import verify_canonical_write_locks
from ... import component_paths, evidence_redaction, provenance, runguard, trust_gate
from ...untrusted import scan_handoff_injection_heuristics

from . import descriptor_anchors, import_model


def _directory_authority_store_path(target: Path, *, env: Mapping[str, str] | None = None) -> Path:
    """Return the verifier-owned authority record for one resolved workspace.

    ``env`` resolves the record under a different data root, which the scanner
    child sandbox uses to seed a copy without exposing the operator's store.
    """
    from ... import authority_marker

    digest = authority_marker.target_fingerprint(target)
    try:
        data_root = Path(component_paths.data_root() if env is None else component_paths.data_root(env=env))
    except ValueError as exc:
        raise OSError("external directory authority storage is unavailable") from exc
    return data_root / "brigade" / "directory-authority" / f"{digest}.json"


def _directory_authority_scope(components: tuple[str, ...]) -> str:
    return "/".join(components)


def _directory_identity(descriptor: int) -> dict[str, int]:
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode):
        raise OSError("directory authority is not a directory")
    return {"device": metadata.st_dev, "inode": metadata.st_ino}


def _posix_dirfd_available() -> bool:
    """Return whether POSIX openat/O_NOFOLLOW primitives can hold a parent."""
    return (
        os.name == "posix"
        and bool(getattr(os, "O_NOFOLLOW", 0))
        and bool(getattr(os, "O_DIRECTORY", 0))
        and os.open in os.supports_dir_fd
        and os.mkdir in os.supports_dir_fd
        and os.stat in os.supports_dir_fd
        and os.unlink in os.supports_dir_fd
        and os.rename in os.supports_dir_fd
    )


def _nt_dirfd_available() -> bool:
    """Return whether Windows handle-relative no-follow operations can be used."""
    if sys.platform != "win32":
        return False
    from .. import nt_dirfd

    return nt_dirfd.available()


def _dirfd_available() -> bool:
    return _posix_dirfd_available() or _nt_dirfd_available()


def _dirfd_unavailable(kind: str) -> OSError:
    return OSError(f"descriptor-relative {kind} are unavailable")


def _open_directory_nofollow(path: Path) -> int:
    """Open a directory without following a final symlink or reparse point."""
    if _posix_dirfd_available():
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        return os.open(path, flags)
    if _nt_dirfd_available():
        from .. import nt_dirfd

        return nt_dirfd.open_root_directory(path)
    raise _dirfd_unavailable("directory operations")


def _open_file_nofollow(path: Path, flags: int, mode: int = 0o600) -> int:
    """Open a file path without following a final symlink or reparse point."""
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if nofollow:
        if flags & os.O_CREAT:
            return os.open(path, flags | nofollow | getattr(os, "O_CLOEXEC", 0), mode)
        return os.open(path, flags | nofollow | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0))
    if _nt_dirfd_available():
        from .. import nt_dirfd

        return nt_dirfd.open_path_file(path, flags, mode)
    raise OSError("no-follow file open is unavailable")


def _dirfd_open_dir(parent: int, name: str) -> int:
    if _posix_dirfd_available():
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        return os.open(name, flags, dir_fd=parent)
    if _nt_dirfd_available():
        from .. import nt_dirfd

        return nt_dirfd.open_child_directory(parent, name)
    raise _dirfd_unavailable("directory operations")


def _dirfd_mkdir(parent: int, name: str) -> None:
    if _posix_dirfd_available():
        os.mkdir(name, 0o700, dir_fd=parent)
        return
    if _nt_dirfd_available():
        from .. import nt_dirfd

        nt_dirfd.mkdir_child(parent, name)
        return
    raise _dirfd_unavailable("directory operations")


def _dirfd_open_file(parent: int, name: str, flags: int, mode: int = 0o600) -> int:
    if _posix_dirfd_available():
        if flags & os.O_CREAT:
            return os.open(name, flags, mode, dir_fd=parent)
        return os.open(name, flags, dir_fd=parent)
    if _nt_dirfd_available():
        from .. import nt_dirfd

        return nt_dirfd.open_file(parent, name, flags, mode)
    raise _dirfd_unavailable("import inbox operations")


def _dirfd_replace(parent: int, source: str, destination: str) -> None:
    if _posix_dirfd_available():
        os.replace(source, destination, src_dir_fd=parent, dst_dir_fd=parent)
        return
    if _nt_dirfd_available():
        from .. import nt_dirfd

        nt_dirfd.replace_children(parent, source, destination)
        return
    raise _dirfd_unavailable("import inbox operations")


def _dirfd_unlink(parent: int, name: str) -> None:
    if _posix_dirfd_available():
        os.unlink(name, dir_fd=parent)
        return
    if _nt_dirfd_available():
        from .. import nt_dirfd

        nt_dirfd.unlink_child(parent, name)
        return
    raise _dirfd_unavailable("import inbox operations")


def _dirfd_stat(parent: int, name: str) -> os.stat_result:
    if _posix_dirfd_available():
        return os.stat(name, dir_fd=parent, follow_symlinks=False)
    if _nt_dirfd_available():
        from .. import nt_dirfd

        return nt_dirfd.stat_child(parent, name)
    raise _dirfd_unavailable("import inbox validation")


def _dirfd_fsync(descriptor: int) -> None:
    """Flush a held descriptor; directory fsync is best-effort on Windows."""
    try:
        os.fsync(descriptor)
    except OSError as exc:
        if sys.platform == "win32" and getattr(exc, "winerror", None) in {1, 5}:
            return
        raise


def _workspace_directory_identity(target: Path) -> dict[str, int]:
    """Return the identity of the workspace root through a no-follow descriptor."""
    descriptor = _open_directory_nofollow(target.expanduser().resolve())
    try:
        return _directory_identity(descriptor)
    finally:
        os.close(descriptor)


def _require_workspace_directory_identity(target: Path, expected: dict[str, int]) -> None:
    """Require a reopened workspace path to identify the originally held root."""
    if _workspace_directory_identity(target) != expected:
        raise OSError("workspace directory identity does not match expected identity")


def _authority_workspace_from_record(record: Mapping[str, Any] | None) -> Path | None:
    if record is None:
        return None
    raw = record.get("target")
    if isinstance(raw, str) and raw:
        return Path(raw)
    return None


def _authority_hmac_enabled(workspace: Path | None) -> bool:
    from ... import authority_key

    return authority_key.hmac_enabled(workspace)


def _read_external_directory_authority_path(
    path: Path,
    *,
    env: Mapping[str, str] | None = None,
    key_material: tuple[bytes, str] | None = None,
    workspace: Path | None = None,
    allow_unsigned_upgrade: bool = True,
) -> dict[str, Any] | None:
    """Read one external authority record without deriving authority from its path."""
    try:
        descriptor = _open_file_nofollow(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0),
        )
    except FileNotFoundError:
        return None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise OSError("external directory authority record is not a single-link regular file")
        payload = json.loads(os.read(descriptor, 1024 * 1024))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OSError("external directory authority record is malformed") from exc
    finally:
        os.close(descriptor)
    if not isinstance(payload, dict):
        raise OSError("external directory authority record is malformed")
    return _unwrap_authority_envelope(
        path,
        payload,
        env=env,
        key_material=key_material,
        workspace=workspace,
        allow_unsigned_upgrade=allow_unsigned_upgrade,
    )


def _read_external_directory_authority(target: Path) -> tuple[Path, dict[str, Any] | None]:
    path = _directory_authority_store_path(target)
    return path, _read_external_directory_authority_path(path, workspace=target)


def _authority_target_digest(record: Mapping[str, Any]) -> str:
    target = record.get("target")
    if not isinstance(target, str) or not target:
        raise OSError("external directory authority record is missing target")
    return hashlib.sha256(target.encode("utf-8")).hexdigest()


def _unwrap_authority_envelope(
    path: Path,
    payload: dict[str, Any],
    *,
    env: Mapping[str, str] | None = None,
    key_material: tuple[bytes, str] | None = None,
    workspace: Path | None = None,
    allow_unsigned_upgrade: bool = True,
) -> dict[str, Any]:
    from ... import authority_broker, authority_key, authority_marker

    inner = (
        payload["record"]
        if payload.get("envelope_version") == 1 and isinstance(payload.get("record"), dict)
        else payload
    )
    workspace = workspace or _authority_workspace_from_record(inner if isinstance(inner, dict) else None)
    hmac_on = _authority_hmac_enabled(workspace)

    if payload.get("envelope_version") == 1:
        # An existing envelope is always verified. The isolation flag only
        # gates whether new writes are signed; it must not skip a MAC check.
        try:
            secret, loaded_id = (
                key_material if key_material is not None else authority_key.load_key(env=env, workspace=workspace)
            )
        except OSError as exc:
            raise OSError("external directory authority key is unavailable") from exc
        try:
            record = authority_broker.verify_store_envelope(secret, payload, loaded_id)
        except ValueError as exc:
            raise OSError(str(exc)) from exc
        sequence = payload["signature"]["sequence"]
        expected = authority_key.sequence_for(
            _authority_target_digest(record), env=env, secret=secret, key_id=loaded_id
        )
        if expected is None:
            raise OSError(
                "authority sequence is missing; restore the operator store-hmac.key and sequence.json, or re-bind the workspace after confirming the directories are intact"
            )
        if expected != sequence:
            raise OSError(
                "authority sequence mismatch; restore the operator store-hmac.key and sequence.json, or re-bind the workspace after confirming the directories are intact"
            )
        expected_name = f"{_authority_target_digest(record)}.json"
        if path.name != expected_name:
            raise OSError("authority store filename does not match the bound target")
        if workspace is not None:
            authority_marker.record_signed_marker(workspace)
        return record
    if authority_marker.marker_exists(workspace, record=payload):
        raise OSError(
            "signed authority store refuses a raw unsigned record; "
            "run brigade security authority downgrade to intentionally downgrade"
        )
    if hmac_on and not allow_unsigned_upgrade:
        raise OSError(
            "signed authority store refuses a raw unsigned reanchor candidate; "
            "the destination isolation policy does not accept an unsigned store"
        )
    if not hmac_on:
        if "schema_version" not in payload or "target" not in payload:
            raise OSError("external directory authority record is malformed")
        return payload
    if authority_key.require_signed(env=env):
        raise OSError("unsigned authority store record is refused")
    if "schema_version" not in payload or "target" not in payload:
        raise OSError("external directory authority record is malformed")
    try:
        secret, loaded_id = (
            key_material
            if key_material is not None
            else authority_key.load_key(env=env, create=True, workspace=workspace)
        )
    except OSError as exc:
        raise OSError("external directory authority key is unavailable") from exc
    if (
        authority_key.sequence_for(_authority_target_digest(payload), env=env, secret=secret, key_id=loaded_id)
        is not None
    ):
        raise OSError("unsigned authority store downgrade is refused")
    _write_external_directory_authority(path, payload, env=env, key_material=(secret, loaded_id), workspace=workspace)
    return payload


def _directory_authority_rebind_command(target: Path) -> str:
    """Return the supported operator command that upgrades or re-binds a record."""
    return f"brigade work rebind-authority --target {target.expanduser().resolve()}"


def _format_authority_field_value(value: object) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    return str(value)


def _is_legacy_external_directory_authority(payload: Mapping[str, Any]) -> bool:
    """Return whether ``payload`` is the pre-workspace store format.

    History from the former ``ledger`` module: the first external record was
    ``{schema_version, target, directories}`` (unsigned). Relocation binding
    later required ``workspace: {device, inode}``. HMAC wrapping is handled
    by ``_unwrap_authority_envelope`` and is not a body-format change.
    A record that already has ``workspace`` is current, even if that field
    mismatches — that is forgery, not a superseded format.
    """
    if payload.get("schema_version") != import_model._EXTERNAL_DIRECTORY_AUTHORITY_SCHEMA_VERSION:
        return False
    if not isinstance(payload.get("target"), str) or not payload.get("target"):
        return False
    if not isinstance(payload.get("directories"), dict):
        return False
    return payload.get("workspace") is None


def _authority_record_mismatch_error(
    path: Path,
    *,
    field: str,
    expected: object,
    observed: object,
    target: Path,
    prefix: str = "external directory authority record does not match directory",
) -> OSError:
    return OSError(
        f"{prefix}: {path} field {field} expected {_format_authority_field_value(expected)} "
        f"observed {_format_authority_field_value(observed)}. "
        f"Run `{_directory_authority_rebind_command(target)}` after confirming the directories are intact."
    )


def _present_directory_authority_mismatches(
    payload: Mapping[str, Any],
    *,
    target: Path,
    workspace: dict[str, int],
    components: tuple[str, ...] | None = None,
    directory: int | None = None,
) -> list[tuple[str, object, object]]:
    """Return mismatches for identity fields that already exist on ``payload``.

    Missing ``workspace`` is a legacy-format signal, not a mismatch. A
    missing ``directories[scope]`` is an unbound scope, not a present-field
    mismatch — callers must not treat that as a safe auto-migrate.
    """
    mismatches: list[tuple[str, object, object]] = []
    resolved = str(target.expanduser().resolve())
    if (
        "schema_version" in payload
        and payload.get("schema_version") != import_model._EXTERNAL_DIRECTORY_AUTHORITY_SCHEMA_VERSION
    ):
        mismatches.append(
            ("schema_version", import_model._EXTERNAL_DIRECTORY_AUTHORITY_SCHEMA_VERSION, payload.get("schema_version"))
        )
    if "target" in payload and payload.get("target") != resolved:
        mismatches.append(("target", resolved, payload.get("target")))
    if payload.get("workspace") is not None and payload.get("workspace") != workspace:
        mismatches.append(("workspace", workspace, payload.get("workspace")))
    directories = payload.get("directories")
    if "directories" in payload and not isinstance(directories, dict):
        mismatches.append(("directories", "object", type(directories).__name__))
        return mismatches
    if components is not None and directory is not None and isinstance(directories, dict):
        scope = _directory_authority_scope(components)
        if scope in directories:
            observed = _directory_identity(directory)
            recorded = directories.get(scope)
            if recorded != observed:
                mismatches.append((f"directories[{scope}]", recorded, observed))
    return mismatches


def _upgrade_legacy_directory_authority(
    path: Path, payload: Mapping[str, Any], *, workspace: dict[str, int]
) -> dict[str, Any]:
    """Write the current body format for a record whose extant fields already matched."""
    upgraded = dict(payload)
    upgraded["workspace"] = workspace
    _write_external_directory_authority(path, upgraded)
    return upgraded


def _adopt_legacy_directory_authority_if_safe(
    path: Path,
    payload: dict[str, Any],
    *,
    target: Path,
    workspace: dict[str, int],
    components: tuple[str, ...] | None = None,
    directory: int | None = None,
) -> dict[str, Any]:
    """Upgrade a verifiably legacy record, or raise on any present-field mismatch.

    Never rewrites a record whose extant identity fields disagree with the
    live directory. That path is the #1036/#1037/#1054 forgery refuse.
    """
    mismatches = _present_directory_authority_mismatches(
        payload, target=target, workspace=workspace, components=components, directory=directory
    )
    if mismatches:
        field, expected, observed = mismatches[0]
        raise _authority_record_mismatch_error(path, field=field, expected=expected, observed=observed, target=target)
    if not _is_legacy_external_directory_authority(payload):
        return payload
    return _upgrade_legacy_directory_authority(path, payload, workspace=workspace)


def _write_external_directory_authority(
    path: Path,
    payload: dict[str, Any],
    *,
    env: Mapping[str, str] | None = None,
    key_material: tuple[bytes, str] | None = None,
    workspace: Path | None = None,
) -> None:
    from ... import authority_broker, authority_key, authority_marker

    record = (
        payload["record"]
        if payload.get("envelope_version") == 1 and isinstance(payload.get("record"), dict)
        else payload
    )
    if record.get("envelope_version") == 1:
        record = dict(record.get("record") or {})
    workspace = workspace or _authority_workspace_from_record(record)
    hmac_on = _authority_hmac_enabled(workspace)
    if hmac_on:
        secret, loaded_id = (
            key_material
            if key_material is not None
            else authority_key.load_key(env=env, create=True, workspace=workspace)
        )
        sequence = authority_key.next_sequence(
            _authority_target_digest(record), env=env, secret=secret, key_id=loaded_id
        )
        to_write: dict[str, Any] = authority_broker.sign_store_record(secret, record, sequence, loaded_id)
    else:
        if authority_marker.marker_exists(workspace, record=record):
            raise OSError(
                "signed authority store refuses a raw unsigned record; "
                "run brigade security authority downgrade to intentionally downgrade"
            )
        to_write = dict(record)
    _publish_authority_store_payload(path, to_write)
    if hmac_on and workspace is not None:
        authority_marker.record_signed_marker(workspace)


def _downgrade_durability_checkpoint(path: Path) -> None:
    """Test seam: dest bytes have changed; parent-dir durability has not run yet."""


def _publish_authority_store_payload(
    path: Path,
    payload: Mapping[str, Any],
    *,
    on_replaced: Callable[[], None] | None = None,
) -> None:
    """Atomically replace the authority store file. No HMAC or marker side effects."""

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    descriptor = -1
    try:
        descriptor = _open_file_nofollow(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        with os.fdopen(os.dup(descriptor), "wb") as handle:
            handle.write(json.dumps(dict(payload), sort_keys=True, separators=(",", ":")).encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        # Windows refuses os.replace while the source handle is still open (WinError 32).
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        if on_replaced is not None:
            on_replaced()
        directory = _open_directory_nofollow(path.parent)
        try:
            _dirfd_fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor != -1:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _read_raw_authority_store(path: Path) -> tuple[bytes | None, dict[str, Any] | None]:
    """Read store bytes and JSON without unwrapping or recording a marker."""

    try:
        descriptor = _open_file_nofollow(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0),
        )
    except FileNotFoundError:
        return None, None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise OSError("external directory authority record is not a single-link regular file")
        raw = os.read(descriptor, 1024 * 1024)
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OSError("external directory authority record is malformed") from exc
    finally:
        os.close(descriptor)
    if not isinstance(payload, dict):
        raise OSError("external directory authority record is malformed")
    return raw, payload


def _restore_authority_file(path: Path, data: bytes) -> None:
    from ... import authority_marker

    authority_marker._restore_marker_bytes(path, data)


def downgrade_external_directory_authority(
    target: Path,
    *,
    actor: str | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Convert a signed store to unsigned, remove the sticky marker, and audit.

    The envelope must be bound to this destination target. A valid MAC for a
    different inner target is refused before any mutation. Intent is logged
    first. Each artifact is recorded the moment dest bytes change so a
    durability-step failure still restores store, sequence, isolation, and
    marker to their pre-downgrade state.
    """

    from ... import authority_broker, authority_key, authority_marker
    from ...localio import utc_now_iso_z
    from ...security_cmd.config import turn_off_authority_store_isolation
    from ...security_cmd.models import config_path as security_config_path

    workspace = target.expanduser().resolve()
    store_path = _directory_authority_store_path(workspace, env=env)
    fingerprint = authority_marker.target_fingerprint(workspace)
    marker_path = authority_marker.signed_marker_path(fingerprint, env=env)
    authority_marker.reject_unsafe_marker_path(marker_path, workspace, env=env)
    posture_path = authority_marker.isolation_marker_path(fingerprint, env=env)
    authority_marker.reject_unsafe_marker_path(posture_path, workspace, env=env)
    isolation_path = security_config_path(workspace)

    store_backup, raw_payload = _read_raw_authority_store(store_path)
    unsigned_record: dict[str, Any] | None = None
    key_material: tuple[bytes, str] | None = None
    if raw_payload is not None and raw_payload.get("envelope_version") == 1:
        try:
            secret, loaded_id = authority_key.load_key(env=env, workspace=workspace)
            verified = authority_broker.verify_store_envelope(secret, raw_payload, loaded_id)
            key_material = (secret, loaded_id)
        except (OSError, ValueError) as exc:
            raise OSError(
                "authority downgrade cannot unwrap the signed store because the "
                "external HMAC key is unavailable; restore the operator "
                "store-hmac.key (or BRIGADE_AUTHORITY_KEY_FILE) and retry. "
                "the store and marker were not changed"
            ) from exc
        try:
            inner_digest = _authority_target_digest(verified)
        except OSError as exc:
            raise OSError("authority downgrade refuses a store envelope bound to a different target") from exc
        if inner_digest != fingerprint:
            raise OSError("authority downgrade refuses a store envelope bound to a different target")
        unsigned_record = verified

    try:
        marker_bytes = marker_path.read_bytes()
    except FileNotFoundError:
        marker_bytes = None
    except OSError as exc:
        raise OSError("authority signed marker is unreadable") from exc
    try:
        posture_bytes = posture_path.read_bytes()
    except FileNotFoundError:
        posture_bytes = None
    except OSError as exc:
        raise OSError("authority isolation posture marker is unreadable") from exc

    actor_value = actor if actor is not None else authority_marker.operator_identity(env=env)
    resolved = str(workspace)
    if unsigned_record is None and marker_bytes is None and posture_bytes is None:
        return {
            "action": "authority-downgrade",
            "actor": actor_value,
            "created_at": utc_now_iso_z(),
            "phase": "complete",
            "removed": False,
            "store_unwrapped": False,
            "target": resolved,
            "target_fingerprint": fingerprint,
        }

    intent = {
        "action": "authority-downgrade",
        "actor": actor_value,
        "created_at": utc_now_iso_z(),
        "phase": "intent",
        "removed": False,
        "store_unwrapped": unsigned_record is not None,
        "target": resolved,
        "target_fingerprint": fingerprint,
    }
    try:
        authority_marker.append_audit(intent, env=env)
    except OSError as exc:
        raise OSError("authority downgrade audit is unavailable; marker not removed") from exc

    sequence_path = authority_key.sequence_path(env=env)
    try:
        sequence_backup = sequence_path.read_bytes()
    except FileNotFoundError:
        sequence_backup = None
    except OSError:
        sequence_backup = None
    try:
        if isolation_path.is_file() and not isolation_path.is_symlink():
            isolation_backup = isolation_path.read_bytes()
        else:
            isolation_backup = None
    except OSError:
        isolation_backup = None

    changed: dict[Path, bytes] = {}
    marker_removed = False
    posture_removed = False

    def _record_change(path: Path, backup: bytes | None) -> None:
        if backup is not None:
            changed[path] = backup
        _downgrade_durability_checkpoint(path)

    def _restore_all() -> None:
        for path, backup in changed.items():
            _restore_authority_file(path, backup)
        if marker_removed and marker_bytes is not None:
            _restore_authority_file(marker_path, marker_bytes)
        if posture_removed and posture_bytes is not None:
            _restore_authority_file(posture_path, posture_bytes)

    try:
        if unsigned_record is not None:
            _publish_authority_store_payload(
                store_path,
                unsigned_record,
                on_replaced=lambda: _record_change(store_path, store_backup),
            )
            if key_material is not None:
                digest = _authority_target_digest(unsigned_record)
                authority_key.drop_sequence(
                    digest,
                    env=env,
                    secret=key_material[0],
                    key_id=key_material[1],
                    on_replaced=lambda: _record_change(sequence_path, sequence_backup),
                )
        turn_off_authority_store_isolation(
            workspace,
            on_replaced=lambda: _record_change(isolation_path, isolation_backup),
        )
        if marker_bytes is not None:
            try:
                marker_path.unlink()
            except FileNotFoundError:
                pass
            else:
                marker_removed = True
                _downgrade_durability_checkpoint(marker_path)
        if posture_bytes is not None:
            try:
                posture_path.unlink()
            except FileNotFoundError:
                pass
            else:
                posture_removed = True
                _downgrade_durability_checkpoint(posture_path)
    except OSError:
        try:
            _restore_all()
        except OSError:
            pass
        raise

    completion = {
        "action": "authority-downgrade",
        "actor": actor_value,
        "created_at": utc_now_iso_z(),
        "phase": "complete",
        "removed": marker_removed,
        "posture_marker_removed": posture_removed,
        "store_unwrapped": unsigned_record is not None,
        "target": resolved,
        "target_fingerprint": fingerprint,
    }
    try:
        completion["audit_path"] = str(authority_marker.append_audit(completion, env=env))
    except OSError as exc:
        try:
            _restore_all()
        except OSError as restore_exc:
            raise OSError("authority downgrade audit failed and marker restore failed") from restore_exc
        raise OSError("authority downgrade audit failed; marker restored") from exc
    return completion


def _record_external_directory_authority(
    target: Path, components: tuple[str, ...], directory: int, *, workspace: dict[str, int]
) -> None:
    """Persist a directory identity in user-owned state, outside the workspace."""
    _require_workspace_directory_identity(target, workspace)
    path, existing = _read_external_directory_authority(target)
    resolved = str(target.expanduser().resolve())
    _require_workspace_directory_identity(target, workspace)
    if existing is None:
        payload: dict[str, Any] = {
            "schema_version": import_model._EXTERNAL_DIRECTORY_AUTHORITY_SCHEMA_VERSION,
            "target": resolved,
            "workspace": workspace,
            "directories": {},
        }
    else:
        payload = _adopt_legacy_directory_authority_if_safe(path, existing, target=target, workspace=workspace)
        if (
            payload.get("schema_version") != import_model._EXTERNAL_DIRECTORY_AUTHORITY_SCHEMA_VERSION
            or payload.get("target") != resolved
            or payload.get("workspace") != workspace
            or not isinstance(payload.get("directories"), dict)
        ):
            raise _authority_record_mismatch_error(
                path,
                field="record",
                expected="current directory-authority body",
                observed="malformed",
                target=target,
                prefix="external directory authority record is malformed",
            )
    directories = payload["directories"]
    assert isinstance(directories, dict)
    scope = _directory_authority_scope(components)
    identity = _directory_identity(directory)
    existing_identity = directories.get(scope)
    if existing_identity is not None:
        if existing_identity != identity:
            raise _authority_record_mismatch_error(
                path,
                field=f"directories[{scope}]",
                expected=existing_identity,
                observed=identity,
                target=target,
            )
        return
    directories[scope] = identity
    _write_external_directory_authority(path, payload, workspace=target)


def _reanchor_external_directory_authority(
    target: Path, components: tuple[str, ...], directory: int, *, workspace: dict[str, int]
) -> bool:
    """Copy an external authority record only after exact root and directory identity matches."""
    _require_workspace_directory_identity(target, workspace)
    current_path = _directory_authority_store_path(target)
    current_target = str(target.expanduser().resolve())
    identity = _directory_identity(directory)
    scope = _directory_authority_scope(components)
    try:
        candidates = list(current_path.parent.iterdir())
    except FileNotFoundError:
        return False
    for candidate in candidates:
        if candidate == current_path:
            continue
        try:
            # Bind verification to the destination target, not the candidate's
            # self-declared target name. Never auto-upgrade a raw candidate.
            payload = _read_external_directory_authority_path(
                candidate,
                workspace=target,
                allow_unsigned_upgrade=False,
            )
        except OSError:
            continue
        if payload is None:
            continue
        recorded_target = payload.get("target")
        directories = payload.get("directories")
        if (
            not isinstance(recorded_target, str)
            or recorded_target == current_target
            or candidate.name != f"{hashlib.sha256(recorded_target.encode('utf-8')).hexdigest()}.json"
            or payload.get("schema_version") != import_model._EXTERNAL_DIRECTORY_AUTHORITY_SCHEMA_VERSION
            or payload.get("workspace") != workspace
            or not isinstance(directories, dict)
            or directories.get(scope) != identity
        ):
            continue
        # Carry the verified isolation posture onto the relocated path before
        # any unsigned state is evaluated (#881 round 4): the candidate record
        # just proved it belongs to the same physical workspace (HMAC envelope,
        # root identity, and directory identities verified above), so its
        # posture marker is transferred and re-verified here, ahead of the
        # store adoption. A transfer or verification failure refuses the
        # reanchor instead of silently dropping the posture.
        from ... import authority_marker

        source_target = payload.get("target")
        if isinstance(source_target, str) and source_target:
            authority_marker.transfer_isolation_marker(Path(source_target), target, destination_identity=workspace)
        _record_external_directory_authority(target, components, directory, workspace=workspace)
        new_path, new_payload = _read_external_directory_authority(target)
        if new_payload is None:
            return True
        old_directories = payload.get("directories")
        current_directories = new_payload.get("directories")
        if isinstance(old_directories, dict) and isinstance(current_directories, dict):
            for key, value in old_directories.items():
                if isinstance(key, str) and key not in current_directories:
                    current_directories[key] = value
        old_files = descriptor_anchors._external_file_authorities(payload)
        if old_files:
            merged = dict(old_files)
            merged.update(descriptor_anchors._external_file_authorities(new_payload))
            new_payload["files"] = merged
        _write_external_directory_authority(new_path, new_payload, workspace=target)
        return True
    return False


def _validate_external_directory_authority(
    target: Path, components: tuple[str, ...], directory: int, *, workspace: dict[str, int]
) -> None:
    _require_workspace_directory_identity(target, workspace)
    path, payload = _read_external_directory_authority(target)
    if payload is None:
        raise OSError(
            f"external directory authority record is missing: {path}. "
            f"Run `{_directory_authority_rebind_command(target)}` after confirming the directories are intact."
        )
    payload = _adopt_legacy_directory_authority_if_safe(
        path,
        payload,
        target=target,
        workspace=workspace,
        components=components,
        directory=directory,
    )
    directories = payload.get("directories")
    scope = _directory_authority_scope(components)
    identity = _directory_identity(directory)
    resolved = str(target.expanduser().resolve())
    if payload.get("schema_version") != import_model._EXTERNAL_DIRECTORY_AUTHORITY_SCHEMA_VERSION:
        raise _authority_record_mismatch_error(
            path,
            field="schema_version",
            expected=import_model._EXTERNAL_DIRECTORY_AUTHORITY_SCHEMA_VERSION,
            observed=payload.get("schema_version"),
            target=target,
        )
    if payload.get("target") != resolved:
        raise _authority_record_mismatch_error(
            path, field="target", expected=resolved, observed=payload.get("target"), target=target
        )
    if payload.get("workspace") != workspace:
        raise _authority_record_mismatch_error(
            path, field="workspace", expected=workspace, observed=payload.get("workspace"), target=target
        )
    if not isinstance(directories, dict):
        raise _authority_record_mismatch_error(
            path,
            field="directories",
            expected="object",
            observed=type(directories).__name__,
            target=target,
        )
    recorded = directories.get(scope)
    if recorded != identity:
        raise _authority_record_mismatch_error(
            path,
            field=f"directories[{scope}]",
            expected=identity,
            observed="missing" if recorded is None else recorded,
            target=target,
        )


def _external_workspace_directory_identity(target: Path) -> dict[str, int]:
    """Read and recheck the root identity already bound by an external authority record."""
    path, payload = _read_external_directory_authority(target)
    if payload is None:
        raise OSError(
            f"external directory authority record is missing: {path}. "
            f"Run `{_directory_authority_rebind_command(target)}` after confirming the directories are intact."
        )
    resolved = str(target.expanduser().resolve())
    if _is_legacy_external_directory_authority(payload) and payload.get("target") == resolved:
        live = _workspace_directory_identity(target)
        payload = _adopt_legacy_directory_authority_if_safe(path, payload, target=target, workspace=live)
        workspace = payload.get("workspace")
    else:
        workspace = payload.get("workspace")
    if (
        payload.get("schema_version") != import_model._EXTERNAL_DIRECTORY_AUTHORITY_SCHEMA_VERSION
        or payload.get("target") != resolved
        or not isinstance(workspace, dict)
        or set(workspace) != {"device", "inode"}
        or not all(isinstance(value, int) and not isinstance(value, bool) for value in workspace.values())
        or not isinstance(payload.get("directories"), dict)
    ):
        raise _authority_record_mismatch_error(
            path,
            field="record",
            expected="current directory-authority body",
            observed="malformed",
            target=target,
            prefix="external directory authority record is malformed",
        )
    _require_workspace_directory_identity(target, workspace)
    return workspace


def _live_directory_scope_mismatches(target: Path, payload: Mapping[str, Any]) -> list[tuple[str, object, object]]:
    """Compare each recorded directory scope that still exists on disk."""
    mismatches: list[tuple[str, object, object]] = []
    directories = payload.get("directories")
    if not isinstance(directories, dict):
        return mismatches
    for scope, recorded in directories.items():
        if not isinstance(scope, str) or not isinstance(recorded, dict):
            continue
        parts = tuple(part for part in scope.split("/") if part)
        if not parts:
            continue
        dir_path = target.joinpath(*parts)
        try:
            descriptor = _open_directory_nofollow(dir_path)
        except FileNotFoundError:
            continue
        except OSError as exc:
            mismatches.append((f"directories[{scope}]", recorded, f"unreadable: {exc}"))
            continue
        try:
            live = _directory_identity(descriptor)
        finally:
            os.close(descriptor)
        if recorded != live:
            mismatches.append((f"directories[{scope}]", recorded, live))
    return mismatches


def rebind_directory_authority(*, target: Path) -> int:
    """Upgrade a legacy directory-authority record after present fields match.

    Refuses to rewrite a record whose extant identity fields disagree with
    the live workspace. That is the supported remediation named by doctor
    and by the fail-closed validator.
    """
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    try:
        path, payload = _read_external_directory_authority(target)
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if payload is None:
        print(
            f"error: external directory authority record is missing: {path}. "
            f"Run `{_directory_authority_rebind_command(target)}` after confirming the directories are intact.",
            file=sys.stderr,
        )
        return 1
    workspace = _workspace_directory_identity(target)
    try:
        mismatches = _present_directory_authority_mismatches(payload, target=target, workspace=workspace)
        mismatches.extend(_live_directory_scope_mismatches(target, payload))
        if mismatches:
            field, expected, observed = mismatches[0]
            raise _authority_record_mismatch_error(
                path, field=field, expected=expected, observed=observed, target=target
            )
        was_legacy = _is_legacy_external_directory_authority(payload)
        if was_legacy:
            _upgrade_legacy_directory_authority(path, payload, workspace=workspace)
            print(f"directory-authority: upgraded legacy record {path}")
        else:
            print(f"directory-authority: {path} already current")
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0
