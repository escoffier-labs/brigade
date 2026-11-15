"""Optional local HMAC signing for Brigade receipt digests."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import stat
import sys
from pathlib import Path

KEY_ENV = "BRIGADE_RECEIPT_SIGNING_KEY_FILE"
KEY_NAME = "receipt-signing-key"


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
    current owner and private mode. Windows awaits an ACL confinement proof.
    """
    from . import dirfd
    from .run_dirfd import bound_run_dir

    if sys.platform == "win32" or not dirfd.posix_available() or not hasattr(os, "getuid"):
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


def generate_key(target: Path, *, force: bool = False) -> tuple[Path, str]:
    path = key_path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT
    flags |= os.O_TRUNC if force else os.O_EXCL
    key = secrets.token_bytes(32)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(key.hex() + "\n")
    os.chmod(path, 0o600)
    return path, key_id(key)
