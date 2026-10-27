"""Local mesh private-key custody. No Hub data or trust acceptance lives here.

Callers supply an already authenticated hub id. One private JSON document per
(hub, principal) commits the retry key and complete key history together. A
rotation is durable before it returns, so publication can use the saved record.
An interrupted write leaves the previous or next complete generation; abandoned
temporary files are never considered state. No automatic history pruning occurs.

The current implementation requires POSIX dirfd/no-follow opens and flock. It
fails closed elsewhere rather than approximating owner-only permissions with
Windows mode bits. Python cannot guarantee zeroization of immutable key bytes.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import time
from typing import Any, Iterator

from . import fleet_mesh_crypto as crypto

_MAX_STATE_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class LocalKey:
    private_keys: crypto.PrivateKeys = field(repr=False)
    record: dict[str, Any]
    retired_at: str | None = None


@dataclass(frozen=True)
class LocalKeys:
    principal: str
    hub_id: str
    active: LocalKey
    retired: tuple[LocalKey, ...]
    retry_key: bytes = field(repr=False)


def _identity(principal: str, hub_id: str) -> str:
    if not isinstance(principal, str) or not re.fullmatch(
        r"(?:[A-Za-z0-9][A-Za-z0-9._-]{0,127}|agent:[a-z][a-z0-9._-]{0,62})", principal, re.ASCII
    ):
        raise crypto.MeshError("mesh_keys_identity_invalid")
    if hub_id != "mesh":
        try:
            if not isinstance(hub_id, str) or not hub_id.startswith("h_"):
                raise ValueError
            crypto.decode_binary(hub_id[2:], 16)
        except ValueError:
            raise crypto.MeshError("mesh_keys_identity_invalid") from None
    return hashlib.sha256(crypto.canonical([hub_id, principal])).hexdigest() + ".json"


def _private_stat(fd: int, *, directory: bool = False) -> None:
    info = os.fstat(fd)
    expected = 0o700 if directory else 0o600
    if (
        not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != expected
        or (not directory and info.st_nlink != 1)
    ):
        raise crypto.MeshError("mesh_keys_unsafe")


@contextmanager
def _directory(home: Path, *, create: bool) -> Iterator[int]:
    if os.name != "posix" or not getattr(os, "O_NOFOLLOW", 0) or not getattr(os, "O_DIRECTORY", 0):
        raise crypto.MeshError("mesh_keys_platform_unsupported")
    # Do not resolve: doing so would conceal a symlink before the no-follow walk.
    if ".." in home.parts:
        raise crypto.MeshError("mesh_keys_unsafe")
    home = home.absolute()
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = None
    try:
        fd = os.open(home.anchor, flags)
        components = (*home.parts[1:], "fleet", "mesh-keys")
        for index, component in enumerate(components):
            private = index >= len(home.parts) - 2
            try:
                child = os.open(component, flags, dir_fd=fd)
            except FileNotFoundError:
                if not create or not private:
                    raise crypto.MeshError("mesh_keys_missing") from None
                try:
                    os.mkdir(component, 0o700, dir_fd=fd)
                    os.fsync(fd)
                except FileExistsError:
                    pass  # Another initializer won; validate its directory below.
                child = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = child
            if private:
                _private_stat(fd, directory=True)
        yield fd
    except OSError as error:
        # Errors raised by reads/writes through yield also arrive here. Storage
        # failures must not be diagnosed as a path or permission violation.
        if error.errno in {errno.ENOSPC, errno.EDQUOT, errno.EIO, errno.EROFS}:
            raise crypto.MeshError("mesh_keys_io_error") from None
        raise crypto.MeshError("mesh_keys_unsafe") from None
    finally:
        if fd is not None:
            os.close(fd)


@contextmanager
def _locked(fd: int) -> Iterator[None]:
    # Lock the anchored directory inode: no separate lock file can be replaced
    # or removed to split concurrent initializers onto different locks.
    import fcntl

    deadline = time.monotonic() + 10
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise crypto.MeshError("mesh_keys_busy") from None
            time.sleep(0.01)
    try:
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError
        value[key] = item
    return value


def _decode(raw: bytes, *, principal: str, hub_id: str) -> LocalKeys:
    try:
        state = json.loads(raw.decode("ascii"), object_pairs_hook=_unique_object)
        if (
            not isinstance(state, dict)
            or set(state) != {"v", "principal", "hub_id", "retry_key", "entries"}
            or type(state["v"]) is not int
            or state["v"] != 1
            or state["principal"] != principal
            or state["hub_id"] != hub_id
            or not isinstance(state["entries"], list)
            or not state["entries"]
        ):
            raise ValueError
        retry_key = crypto.decode_binary(state["retry_key"], 32)
        entries: list[LocalKey] = []
        previous: LocalKey | None = None
        for entry in state["entries"]:
            if not isinstance(entry, dict) or set(entry) != {"signing_key", "kem_seed", "record", "retired_at"}:
                raise ValueError
            keys = crypto.PrivateKeys(
                crypto.decode_binary(entry["signing_key"], 32), crypto.decode_binary(entry["kem_seed"], 32)
            )
            record = entry["record"]
            if not isinstance(record, dict):
                raise ValueError
            expected = crypto.build_key_record(
                keys,
                principal=principal,
                hub=hub_id,
                created_at=record["created_at"],
                previous=previous.private_keys if previous else None,
            )
            if crypto.canonical(expected) != crypto.canonical(record):
                raise ValueError
            if previous:
                if previous.retired_at != record["created_at"]:
                    raise ValueError
                crypto.verify_key_record(record, previous_record=previous.record)
            previous = LocalKey(keys, record, entry["retired_at"])
            entries.append(previous)
        if entries[-1].retired_at is not None:
            raise ValueError
        return LocalKeys(principal, hub_id, entries[-1], tuple(entries[:-1]), retry_key)
    except crypto.MeshExtraMissing:
        raise
    except crypto.MeshError as error:
        if error.code == "mesh_backend_unsupported":
            raise
        raise crypto.MeshError("mesh_keys_invalid") from None
    except (ValueError, KeyError, TypeError, RecursionError):
        raise crypto.MeshError("mesh_keys_invalid") from None


def _read(fd: int, name: str, *, principal: str, hub_id: str) -> LocalKeys | None:
    try:
        source = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=fd)
    except FileNotFoundError:
        return None
    try:
        _private_stat(source)
        if os.fstat(source).st_size > _MAX_STATE_BYTES:
            raise crypto.MeshError("mesh_keys_invalid")
        with os.fdopen(source, "rb", closefd=False) as stream:
            raw = stream.read(_MAX_STATE_BYTES + 1)
        if len(raw) > _MAX_STATE_BYTES:
            raise crypto.MeshError("mesh_keys_invalid")
        return _decode(raw, principal=principal, hub_id=hub_id)
    finally:
        os.close(source)


def _reap_temporary_files(fd: int) -> None:
    # Called only under the exclusive directory lock before a write. Never
    # open, decode or promote abandoned contents, and leave unsafe entries alone.
    for name in os.listdir(fd):
        if not re.fullmatch(r"\.[0-9a-f]{32}\.tmp", name, re.ASCII):
            continue
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if (
            stat.S_ISREG(info.st_mode)
            and info.st_uid == os.geteuid()
            and stat.S_IMODE(info.st_mode) == 0o600
            and info.st_nlink == 1
        ):
            os.unlink(name, dir_fd=fd)


def _write(fd: int, name: str, state: LocalKeys) -> None:
    raw = crypto.canonical(
        {
            "v": 1,
            "principal": state.principal,
            "hub_id": state.hub_id,
            "retry_key": crypto.encode_binary(state.retry_key),
            "entries": [
                {
                    "signing_key": crypto.encode_binary(entry.private_keys.signing_key),
                    "kem_seed": crypto.encode_binary(entry.private_keys.kem_seed),
                    "record": entry.record,
                    "retired_at": entry.retired_at,
                }
                for entry in (*state.retired, state.active)
            ],
        }
    )
    if len(raw) > _MAX_STATE_BYTES:
        raise crypto.MeshError("mesh_keys_invalid")
    _reap_temporary_files(fd)
    temporary = "." + secrets.token_hex(16) + ".tmp"
    dest = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=fd)
    try:
        with os.fdopen(dest, "wb", closefd=False) as stream:
            _private_stat(dest)
            stream.write(raw)
            stream.flush()
            os.fsync(dest)
        os.replace(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
        os.fsync(fd)
    finally:
        os.close(dest)
        try:
            os.unlink(temporary, dir_fd=fd)
        except FileNotFoundError:
            pass


def load_local_keys(*, brigade_home: Path, principal: str, hub_id: str, create: bool = False) -> LocalKeys:
    """Read saved custody without mutation; initialize only with explicit create.

    Missing state, invalid state and unsafe paths are distinct fixed diagnostics.
    Importing this module needs no crypto extra. Reading verifies the saved
    private/public bindings and therefore does need the extra.
    """
    name = _identity(principal, hub_id)
    with _directory(brigade_home, create=create) as fd, _locked(fd):
        state = _read(fd, name, principal=principal, hub_id=hub_id)
        if state is None:
            if not create:
                raise crypto.MeshError("mesh_keys_missing")
            keys = crypto.generate_keys()
            record = crypto.build_key_record(
                keys, principal=principal, hub=hub_id, created_at=datetime.now(timezone.utc).isoformat()
            )
            state = LocalKeys(principal, hub_id, LocalKey(keys, record), (), secrets.token_bytes(32))
            _write(fd, name, state)
        return state


def rotate_local_keys(*, brigade_home: Path, principal: str, hub_id: str) -> LocalKeys:
    """Commit a new independently generated pair and retain all previous keys.

    A lost return must be reconciled by reading the saved active record before
    another rotation. This operation is deliberately local and never publishes.
    """
    name = _identity(principal, hub_id)
    with _directory(brigade_home, create=False) as fd, _locked(fd):
        state = _read(fd, name, principal=principal, hub_id=hub_id)
        if state is None:
            raise crypto.MeshError("mesh_keys_missing")
        created_at = datetime.now(timezone.utc).isoformat()
        if datetime.fromisoformat(created_at) <= datetime.fromisoformat(state.active.record["created_at"]):
            raise crypto.MeshError("mesh_keys_clock_rollback")
        keys = crypto.generate_keys()
        record = crypto.build_key_record(
            keys, principal=principal, hub=hub_id, created_at=created_at, previous=state.active.private_keys
        )
        rotated = LocalKeys(
            principal,
            hub_id,
            LocalKey(keys, record),
            (*state.retired, replace(state.active, retired_at=created_at)),
            state.retry_key,
        )
        _write(fd, name, rotated)
        return rotated
