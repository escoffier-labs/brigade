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


def load_workspace_key(target: Path) -> tuple[bytes, str] | None:
    """Fixed-path authority, with the origin-stamp confinement rules from #2577.

    Never create a key or honor the receipt-only environment override. This
    authenticates tooling against text forgery, not an agent with shell access.
    Windows remains review-only until an ACL confinement proof exists (#2588).
    """
    if sys.platform == "win32" or not dirfd.posix_available() or not hasattr(os, "getuid"):
        return None
    try:
        path = target.expanduser().resolve() / ".brigade" / KEY_NAME
        parent = dirfd.open_directory_chain(path.parent)
        try:
            fd = dirfd.open_child_file(parent, path.name, os.O_RDONLY | os.O_NONBLOCK)
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
