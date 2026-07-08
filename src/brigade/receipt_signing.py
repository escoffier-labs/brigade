"""Optional local HMAC signing for Brigade receipt digests."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
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
