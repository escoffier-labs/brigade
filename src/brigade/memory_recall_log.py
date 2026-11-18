"""Search-log entries for prompt-driven recall that never store the query (#2196).

Recall that searched with a person's text (a prompt, or ``memory recall
--query``) logs a keyed hash of the normalized query, the query source, and
the returned card ids. The raw text never reaches disk; only explicit
``memory search`` calls log the query itself.

The hash is HMAC-SHA256 with a per-host secret, because a plain hash of a short
prompt can be reversed by guessing. The secret lives next to the search log
under ``.brigade/`` (0600), is created on first use, and is never synced.
Everything here is fail-open: a logging problem never breaks recall.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import stat
import sys
from pathlib import Path
from typing import Any

from .memory_search_diagnostics import _normalize_search_query

KEY_REL = ".brigade/memory/search-log-key"
_KEY_BYTES = 32


class KeyRefusedError(ValueError):
    """An existing per-host key failed its ownership, mode, or format checks."""


def key_path(target: Path) -> Path:
    return target.expanduser().resolve() / KEY_REL


def _warn_refused(path: Path, exc: Exception) -> None:
    detail = str(exc)[:200]
    message = f"warning: search-log key refused at {path}: {detail}. Run chmod 600 {path} or remove it."
    print(message[:500], file=sys.stderr)


def _read_key(path: Path) -> bytes:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise KeyRefusedError(f"search-log key cannot be inspected: {exc}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise KeyRefusedError("search-log key must be a regular file")
    uid = getattr(os, "getuid", None)
    if uid is not None and metadata.st_uid != uid():
        raise KeyRefusedError("search-log key must be owned by the current user")
    if os.name == "posix" and stat.S_IMODE(metadata.st_mode) != 0o600:
        raise KeyRefusedError("search-log key must have mode 0600")
    try:
        text = path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        raise
    except (OSError, UnicodeDecodeError) as exc:
        raise KeyRefusedError(f"search-log key cannot be read: {exc}") from exc
    try:
        key = bytes.fromhex(text)
    except ValueError as exc:
        raise KeyRefusedError("search-log key is not valid hex") from exc
    if len(key) != _KEY_BYTES:
        raise KeyRefusedError(f"search-log key must be {_KEY_BYTES} bytes")
    return key


def _ignore_key(path: Path) -> None:
    """Keep the key out of Git even when Brigade's .gitignore block was skipped.

    ``apply_gitignore`` normally excludes ``.brigade/`` state, but setups that
    opt out of gitignore edits would let a plain ``git add`` commit the key.
    A one-line ``.gitignore`` beside it covers that case; an existing file is
    left alone.
    """
    ignore = path.parent / ".gitignore"
    if not ignore.exists():
        ignore.write_text(f"/{path.name}\n", encoding="utf-8")


def load_or_create_key(target: Path) -> bytes | None:
    """Return the per-host key, creating it (0600) on first use; ``None`` on failure."""
    path = key_path(target)
    try:
        return _read_key(path)
    except FileNotFoundError:
        pass
    except KeyRefusedError as exc:
        _warn_refused(path, exc)
        return None
    except (OSError, ValueError):
        return None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write a private temp file, then link it into place so a concurrent
        # reader never sees a partial key and a concurrent writer never wins twice.
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="ascii") as handle:
                handle.write(secrets.token_bytes(_KEY_BYTES).hex() + "\n")
            _ignore_key(path)
            try:
                os.link(tmp, path)
            except FileExistsError:
                pass
            except OSError:
                # No hard links on this filesystem. The temp file is complete,
                # so an atomic replace still never exposes a partial key.
                if not path.exists():
                    os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)
        return _read_key(path)
    except KeyRefusedError as exc:
        _warn_refused(path, exc)
        return None
    except (OSError, ValueError):
        return None


def query_digest(key: bytes, query: str) -> str:
    """HMAC-SHA256 of the normalized query (lowercased, whitespace collapsed)."""
    return hmac.new(key, _normalize_search_query(query).encode("utf-8"), hashlib.sha256).hexdigest()


def record_recall(
    target: Path,
    *,
    query: str,
    query_source: str,
    matches: list[dict[str, Any]],
    ts: str | None = None,
) -> None:
    """Append one hashed recall entry to the target's search log."""
    from . import memory_cmd

    if not _normalize_search_query(query):
        return
    key = load_or_create_key(target)
    if key is None:
        return
    memory_cmd._append_search_log_entry(
        target,
        {
            "ts": ts or memory_cmd._utc_iso(),
            "query_hmac": query_digest(key, query),
            "query_source": query_source,
            "card_ids": memory_cmd._search_log_card_ids(matches),
            "card_aliases": memory_cmd._search_log_aliases(matches),
        },
    )
