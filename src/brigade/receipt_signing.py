"""Optional local HMAC signing for Brigade receipt digests."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import stat
import sys
from pathlib import Path
from typing import Any

from . import dirfd, localio

KEY_ENV = "BRIGADE_RECEIPT_SIGNING_KEY_FILE"
KEY_NAME = "receipt-signing-key"
_WINDOWS_KEY_REPAIR = (
    "Windows requires an owner-only workspace signing key. "
    "Move .brigade/receipt-signing-key aside and run brigade receipts keygen in the workspace "
    "with BRIGADE_RECEIPT_SIGNING_KEY_FILE unset."
)


class WindowsKeyRefused(OSError):
    """The fixed Windows key could not be opened or written owner-only."""


def key_path(target: Path) -> Path:
    configured = os.environ.get(KEY_ENV)
    if configured:
        return Path(configured).expanduser()
    return target.expanduser().resolve() / ".brigade" / KEY_NAME


def key_id(key: bytes) -> str:
    return hashlib.sha256(key).hexdigest()[:8]


def load_key(target: Path) -> tuple[bytes, str] | None:
    path = key_path(target)
    if not path.is_file():
        return None
    raw = path.read_text().strip()
    key = bytes.fromhex(raw)
    if len(key) != 32:
        raise ValueError(f"receipt signing key must be 32 bytes: {path}")
    return key, key_id(key)


def sign(digest_hex: str, key: bytes) -> str:
    return hmac.new(key, digest_hex.encode("utf-8"), hashlib.sha256).hexdigest()


def workspace_key(target: Path) -> bytes | None:
    """Fixed-path authority key. Never honor overrides or provision a key here.

    Match origin-stamp confinement: no linked parents, single-link regular file,
    current owner and private permissions.
    """
    from . import dirfd
    from .run_dirfd import bound_run_dir

    if sys.platform == "win32":
        return _windows_workspace_key(target)
    if not dirfd.posix_available() or not hasattr(os, "getuid"):
        return None
    try:
        with bound_run_dir(target.expanduser().resolve()) as bound:
            fd = bound.open_file((".brigade",), KEY_NAME, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
            with os.fdopen(fd, "rb") as handle:
                before = os.fstat(handle.fileno())
                if (
                    not stat.S_ISREG(before.st_mode)
                    or before.st_nlink != 1
                    or before.st_uid != os.getuid()
                    or stat.S_IMODE(before.st_mode) & 0o077
                    or before.st_size > 256
                ):
                    return None
                raw = handle.read(257)
                after = os.fstat(handle.fileno())
                now = bound.stat((".brigade",), KEY_NAME)
                if (
                    any(
                        getattr(before, field) != getattr(after, field)
                        for field in (
                            "st_dev",
                            "st_ino",
                            "st_mode",
                            "st_nlink",
                            "st_uid",
                            "st_gid",
                            "st_size",
                            "st_mtime_ns",
                            "st_ctime_ns",
                        )
                    )
                    or (now.st_dev, now.st_ino) != (before.st_dev, before.st_ino)
                    or len(raw) != before.st_size
                ):
                    return None
                key = bytes.fromhex(raw.decode("ascii").strip())
                return key if len(key) == 32 else None
    except (OSError, ValueError, UnicodeError):
        return None


def _windows_workspace_key(target: Path) -> bytes | None:
    from .work_cmd import nt_private

    try:
        parent = dirfd.open_directory_chain(target.expanduser().resolve() / ".brigade", writable=False)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        print(_WINDOWS_KEY_REPAIR, file=sys.stderr)
        return None
    try:
        # Distinguish an absent key from a failure in an API that verifies it.
        try:
            dirfd.stat_child(parent, KEY_NAME, allow_reparse=True)
        except FileNotFoundError:
            return None
        fd = nt_private.open_file(parent, KEY_NAME, os.O_RDONLY, share_write=False)
        with os.fdopen(fd, "rb") as handle:
            nt_private.verify_fd(fd, directory=False)
            raw = handle.read(257)
            nt_private.verify_fd(fd, directory=False)
            held = os.fstat(fd)
            named = dirfd.stat_child(parent, KEY_NAME)
            if (held.st_dev, held.st_ino) != (named.st_dev, named.st_ino) or len(raw) != held.st_size or len(raw) > 256:
                raise ValueError("unstable or oversized workspace signing key")
            key = bytes.fromhex(raw.decode("ascii").strip())
            if len(key) != 32:
                raise ValueError("invalid workspace signing key length")
            return key
    except (OSError, ValueError):
        print(_WINDOWS_KEY_REPAIR, file=sys.stderr)
        return None
    finally:
        os.close(parent)


def _write_windows_key(path: Path, key: bytes, *, force: bool) -> None:
    from .work_cmd import nt_private

    try:
        parent = dirfd.open_directory_chain(path.parent, create=True)
        try:
            try:
                fd = nt_private.open_file(parent, path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, share_write=False)
            except FileExistsError:
                if not force:
                    raise
                fd = nt_private.open_file(parent, path.name, os.O_WRONLY, share_write=False)
            with os.fdopen(fd, "wb") as handle:
                nt_private.verify_fd(fd, directory=False)
                handle.write(key.hex().encode("ascii") + b"\n")
                handle.flush()
                if force:
                    handle.truncate()
                os.fsync(fd)
        finally:
            os.close(parent)
    except FileExistsError:
        raise
    except (OSError, ValueError):
        raise WindowsKeyRefused(_WINDOWS_KEY_REPAIR) from None


def generate_key(target: Path, *, force: bool = False) -> tuple[Path, str]:
    path = key_path(target)
    if sys.platform == "win32" and path.absolute() == target.expanduser().resolve() / ".brigade" / KEY_NAME:
        key = secrets.token_bytes(32)
        _write_windows_key(path.absolute(), key, force=force)
        return path, key_id(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT
    flags |= os.O_TRUNC if force else os.O_EXCL
    key = secrets.token_bytes(32)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(key.hex() + "\n")
    os.chmod(path, 0o600)
    return path, key_id(key)


def load_workspace_key(target: Path) -> tuple[bytes, str] | None:
    """Fixed-path authority, with the origin-stamp confinement rules from #2577.

    Never create a key or honor the receipt-only environment override. This
    authenticates tooling against text forgery, not an agent with shell access.
    """
    if sys.platform == "win32":
        key = _windows_workspace_key(target)
        return (key, key_id(key)) if key is not None else None
    if not dirfd.posix_available() or not hasattr(os, "getuid"):
        return None
    try:
        path = target.expanduser().resolve() / ".brigade" / KEY_NAME
        parent = dirfd.open_directory_chain(path.parent)
        try:
            fd = dirfd.open_child_file(parent, path.name, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
            with os.fdopen(fd, "rb") as handle:
                before = os.fstat(handle.fileno())
                if (
                    not stat.S_ISREG(before.st_mode)
                    or before.st_nlink != 1
                    or before.st_uid != os.getuid()
                    or before.st_mode & 0o077
                    or before.st_size > 256
                ):
                    return None
                data = handle.read(257)
                after = os.fstat(handle.fileno())
                live = dirfd.stat_child(parent, path.name)
                fields = (
                    "st_dev",
                    "st_ino",
                    "st_mode",
                    "st_nlink",
                    "st_uid",
                    "st_gid",
                    "st_size",
                    "st_mtime_ns",
                    "st_ctime_ns",
                )
                if (
                    any(getattr(before, f) != getattr(after, f) for f in fields)
                    or (live.st_dev, live.st_ino) != (before.st_dev, before.st_ino)
                    or not stat.S_ISREG(live.st_mode)
                    or len(data) != before.st_size
                ):
                    return None
        finally:
            os.close(parent)
        key = bytes.fromhex(data.decode("ascii").strip())
        return (key, key_id(key)) if len(key) == 32 else None
    except (OSError, ValueError, UnicodeError):
        return None


def authentic_workspace_receipt(target: Path, receipt: dict[str, Any]) -> bool:
    """Verify the canonical receipt bytes with confined workspace authority."""
    key = load_workspace_key(target)
    digests = receipt.get("digests")
    if key is None or not isinstance(digests, dict):
        return False
    digest = localio.canonical_json_digest(receipt, exclude_keys={"digests"})
    signature = digests.get("workspace_signature")
    return (
        digests.get("receipt_sha256") == digest
        and digests.get("workspace_key_id") == key[1]
        and isinstance(signature, str)
        and signature.isascii()
        and hmac.compare_digest(signature, sign("brigade.work-verify.v1\n" + digest, key[0]))
    )


def stamp_workspace_signature(target: Path, receipt: dict[str, Any]) -> None:
    """Add confined authority without changing the legacy audit-key override."""
    key = load_workspace_key(target)
    if key is not None:
        receipt["digests"]["workspace_signature"] = sign(
            "brigade.work-verify.v1\n" + receipt["digests"]["receipt_sha256"], key[0]
        )
        receipt["digests"]["workspace_key_id"] = key[1]
