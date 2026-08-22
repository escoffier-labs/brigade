"""Persisted 0600 HMAC key for the parent-held directory-authority store.

Location: ``<config_root>/brigade/authority/store-hmac.key`` (Linux:
``$XDG_CONFIG_HOME/brigade/authority/store-hmac.key`` or ``~/.config/...``),
overridable with ``BRIGADE_AUTHORITY_KEY_FILE``. Mode 0600, parent directory
0700. The key is read only by the parent verifier and must never be written
inside the workspace or the scanner-reachable ``.brigade`` tree.

Scanner children must never receive this path on their env allowlist.
``authority_store.isolation = "external-key"`` in ``.brigade/security.toml``
is the operator opt-in; ``brigade doctor`` WARNs when that flag is off.

This key is same-UID readable. That is the documented residual of the crypto
tier: a child that finds and reads this file can forge a valid store envelope.
``--isolated-scanners`` is the tier that closes that residual.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import sys
from collections.abc import Mapping
from pathlib import Path

from . import component_paths

KEY_ENV = "BRIGADE_AUTHORITY_KEY_FILE"
KEY_NAME = "store-hmac.key"
KEY_BYTES = 32
REQUIRE_SIGNED_ENV = "BRIGADE_AUTHORITY_REQUIRE_SIGNED"

_CACHE: dict[str, tuple[bytes, str]] = {}


def authority_dir(*, env: Mapping[str, str] | None = None, system: str | None = None) -> Path:
    try:
        root = component_paths.config_root(env=env, system=system)
    except ValueError as exc:
        raise OSError("external directory authority key is unavailable") from exc
    return Path(root) / "brigade" / "authority"


def key_path(*, env: Mapping[str, str] | None = None, system: str | None = None) -> Path:
    environment = env if env is not None else os.environ
    configured = environment.get(KEY_ENV)
    if configured:
        return Path(configured).expanduser()
    return authority_dir(env=env, system=system) / KEY_NAME


def _absolute_unresolved(path: Path) -> Path:
    """Absolute path without following symlinks (``..`` and ``.`` stay as parts)."""

    expanded = path.expanduser()
    if not expanded.is_absolute():
        expanded = Path.cwd() / expanded
    return expanded


def _parts_prefix(path: Path, tree: Path) -> bool:
    return len(path.parts) >= len(tree.parts) and path.parts[: len(tree.parts)] == tree.parts


def key_is_inside_tree(path: Path, tree: Path) -> bool:
    """True when ``path`` resolves inside ``tree`` using absolute real paths."""

    try:
        path.expanduser().resolve().relative_to(tree.expanduser().resolve())
    except ValueError:
        return False
    return True


def _workspace_symlink_component(path: Path, workspace: Path) -> bool:
    """True when a workspace-local component of ``path`` is a symlink."""

    lexical = _absolute_unresolved(path)
    work = _absolute_unresolved(workspace)
    acc = Path(lexical.parts[0])
    for part in lexical.parts[1:]:
        acc = acc / part
        if not _parts_prefix(acc, work):
            continue
        try:
            if acc.is_symlink():
                return True
        except OSError:
            continue
    return False


def key_path_is_scanner_reachable(path: Path, workspace: Path | None = None) -> bool:
    """True when the *HMAC key* would sit in the workspace or ``.brigade`` state.

    Uses the unresolved path first so a directory symlink inside the workspace
    that points outside cannot pass as an external key. Also refuses a symlink
    component under the workspace and a resolved path that lands inside it.

    Do not reuse this helper for the signed marker. Markers are required to
    live under ``~/.brigade`` (or ``$BRIGADE_USER_DIR``), which contains a
    ``.brigade`` path component by design. See
    ``authority_marker.reject_unsafe_marker_path``.
    """

    unresolved = _absolute_unresolved(path)
    if ".brigade" in unresolved.parts:
        return True
    if workspace is None:
        return False
    work = _absolute_unresolved(workspace)
    if _parts_prefix(unresolved, work):
        return True
    if _workspace_symlink_component(unresolved, work):
        return True
    return key_is_inside_tree(path, workspace)


def reject_scanner_reachable_key_path(path: Path, workspace: Path | None = None) -> None:
    """Refuse a key path inside the workspace or any scanner-reachable tree.

    Containment is decided on the unresolved absolute path and on any symlink
    component under the workspace, before any create or open, so an override
    such as ``<workspace>/operator-authority.key`` or
    ``<workspace>/escape-link/k.key`` cannot mint a reachable HMAC key.
    """

    if key_path_is_scanner_reachable(path, workspace):
        raise OSError("authority store HMAC key must not be written inside the workspace or scanner-reachable tree")


def sequence_path(*, env: Mapping[str, str] | None = None, system: str | None = None) -> Path:
    return authority_dir(env=env, system=system) / "sequence.json"


def key_id(key: bytes) -> str:
    return hashlib.sha256(key).hexdigest()[:8]


def require_signed(*, env: Mapping[str, str] | None = None) -> bool:
    environment = env if env is not None else os.environ
    return environment.get(REQUIRE_SIGNED_ENV) == "1"


def hmac_enabled(target: Path | None) -> bool:
    """True when ``authority_store.isolation = "external-key"`` for ``target``."""

    if target is None:
        return False
    from .security_cmd.config import authority_store_isolation_mode
    from .security_cmd.models import AUTHORITY_STORE_ISOLATION_EXTERNAL_KEY

    return authority_store_isolation_mode(target) == AUTHORITY_STORE_ISOLATION_EXTERNAL_KEY


def _cache_key(path: Path) -> str:
    return str(path)


def _validate_key_stat(metadata: os.stat_result, path: Path) -> None:
    if not stat.S_ISREG(metadata.st_mode):
        raise OSError(f"external directory authority key is not a regular file: {path}")
    if metadata.st_nlink != 1:
        raise OSError(f"external directory authority key is not a single-link file: {path}")
    # Windows permission bits are not POSIX 0600; ACLs are the access model there.
    if os.name == "posix" and stat.S_IMODE(metadata.st_mode) & 0o077:
        raise OSError(f"external directory authority key must not be group or world readable: {path}")


def _nt_nofollow_available() -> bool:
    """Return whether Windows handle-relative no-follow opens can be used."""
    if sys.platform != "win32":
        return False
    from .work_cmd import nt_dirfd

    return nt_dirfd.available()


def _open_file_nofollow(path: Path, flags: int, mode: int = 0o600) -> int:
    """Open a key or sequence file without following a final symlink or reparse point."""
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if nofollow:
        if flags & os.O_CREAT:
            return os.open(path, flags | nofollow | getattr(os, "O_CLOEXEC", 0), mode)
        return os.open(path, flags | nofollow | getattr(os, "O_CLOEXEC", 0))
    if _nt_nofollow_available():
        from .work_cmd import nt_dirfd

        return nt_dirfd.open_path_file(path, flags, mode)
    raise OSError("no-follow file open is unavailable")


def _fsync_directory(path: Path) -> None:
    """Flush a parent directory; skip or use backup semantics when POSIX flags are absent."""
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    if nofollow and directory_flag:
        directory = os.open(path, os.O_RDONLY | directory_flag | nofollow)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return
    if _nt_nofollow_available():
        from .work_cmd import nt_dirfd

        directory = nt_dirfd.open_root_directory(path)
        try:
            try:
                os.fsync(directory)
            except OSError as exc:
                if sys.platform == "win32" and getattr(exc, "winerror", None) in {1, 5}:
                    return
                raise
        finally:
            os.close(directory)


def generate_key(
    *,
    env: Mapping[str, str] | None = None,
    system: str | None = None,
    force: bool = False,
    workspace: Path | None = None,
) -> tuple[bytes, str]:
    path = key_path(env=env, system=system)
    reject_scanner_reachable_key_path(path, workspace)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    # Re-check the unresolved path, symlink components, and final real path
    # after mkdir and before any key bytes are written.
    reject_scanner_reachable_key_path(path, workspace)
    key = secrets.token_bytes(KEY_BYTES)
    if force and path.exists():
        flags = os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        descriptor = _open_file_nofollow(path, flags)
    else:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        descriptor = _open_file_nofollow(path, flags, 0o600)
    try:
        with os.fdopen(os.dup(descriptor), "w", encoding="utf-8") as handle:
            handle.write(key.hex() + "\n")
            handle.flush()
            handle.truncate()
            os.fsync(handle.fileno())
        os.chmod(path, 0o600)
        _fsync_directory(path.parent)
    finally:
        os.close(descriptor)
    material = key, key_id(key)
    _CACHE[_cache_key(path)] = material
    return material


def load_key(
    *,
    env: Mapping[str, str] | None = None,
    system: str | None = None,
    create: bool = False,
    workspace: Path | None = None,
) -> tuple[bytes, str]:
    """Load the persisted store key. Missing key fails closed unless ``create``."""

    path = key_path(env=env, system=system)
    reject_scanner_reachable_key_path(path, workspace)
    cached = _CACHE.get(_cache_key(path))
    if cached is not None:
        return cached
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = _open_file_nofollow(path, flags)
    except FileNotFoundError:
        if create:
            return generate_key(env=env, system=system, workspace=workspace)
        raise OSError("external directory authority key is unavailable") from None
    except OSError as exc:
        raise OSError("external directory authority key is unavailable") from exc
    try:
        metadata = os.fstat(descriptor)
        _validate_key_stat(metadata, path)
        raw = os.read(descriptor, 256).decode("utf-8").strip()
    except OSError as exc:
        if "group or world readable" in str(exc) or "single-link" in str(exc) or "regular file" in str(exc):
            raise
        raise OSError("external directory authority key is unavailable") from exc
    finally:
        os.close(descriptor)
    try:
        key = bytes.fromhex(raw)
    except ValueError as exc:
        raise OSError("external directory authority key is malformed") from exc
    if len(key) != KEY_BYTES:
        raise OSError("external directory authority key must be 32 bytes")
    material = key, key_id(key)
    _CACHE[_cache_key(path)] = material
    return material


def clear_key_cache() -> None:
    _CACHE.clear()


def _write_private_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = -1
    try:
        descriptor = _open_file_nofollow(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        with os.fdopen(os.dup(descriptor), "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True, separators=(",", ":")))
            handle.flush()
            os.fsync(handle.fileno())
        # Windows refuses os.replace while the source handle is still open (WinError 32).
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if descriptor != -1:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def load_sequence(*, env: Mapping[str, str] | None = None, secret: bytes, key_id: str) -> dict[str, int]:
    from . import authority_broker

    path = sequence_path(env=env)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise OSError("authority sequence file is malformed")
    if payload.get("envelope_version") != 1:
        raise OSError("authority sequence file is unsigned or malformed")
    signature = payload.get("signature")
    records = payload.get("records")
    if not isinstance(signature, dict) or not isinstance(records, dict):
        raise OSError("authority sequence file is malformed")
    if signature.get("key_id") != key_id:
        raise OSError("authority sequence file key_id does not match the loaded key")
    expected = signature.get("mac")
    if not isinstance(expected, str) or not authority_broker.verify_mac(
        secret, authority_broker.SEQUENCE_DOMAIN, authority_broker.canonical_dumps(records), expected
    ):
        raise OSError("authority sequence file MAC mismatch")
    out: dict[str, int] = {}
    for digest, value in records.items():
        if isinstance(digest, str) and isinstance(value, int) and not isinstance(value, bool) and value >= 1:
            out[digest] = value
    return out


def write_sequence(
    records: Mapping[str, int], *, env: Mapping[str, str] | None = None, secret: bytes, key_id: str
) -> None:
    from . import authority_broker

    payload = {
        "envelope_version": 1,
        "signature": {
            "alg": "HMAC-SHA256",
            "key_id": key_id,
            "mac": authority_broker.mac(
                secret, authority_broker.SEQUENCE_DOMAIN, authority_broker.canonical_dumps(dict(records))
            ),
        },
        "records": dict(records),
    }
    _write_private_json(sequence_path(env=env), payload)


def next_sequence(target_digest: str, *, env: Mapping[str, str] | None = None, secret: bytes, key_id: str) -> int:
    records = load_sequence(env=env, secret=secret, key_id=key_id)
    current = int(records.get(target_digest, 0)) + 1
    records[target_digest] = current
    write_sequence(records, env=env, secret=secret, key_id=key_id)
    return current


def sequence_for(target_digest: str, *, env: Mapping[str, str] | None = None, secret: bytes, key_id: str) -> int | None:
    records = load_sequence(env=env, secret=secret, key_id=key_id)
    return records.get(target_digest)


def drop_sequence(target_digest: str, *, env: Mapping[str, str] | None = None, secret: bytes, key_id: str) -> bool:
    """Remove one target from the sequence file so a later re-sign can start clean."""

    records = load_sequence(env=env, secret=secret, key_id=key_id)
    if target_digest not in records:
        return False
    del records[target_digest]
    write_sequence(records, env=env, secret=secret, key_id=key_id)
    return True
