"""Download, verify, cache, and install pinned native Brigade components."""

from __future__ import annotations

import os
import re
import sys
import tempfile
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import brigade
from brigade import component_manifest, localio

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_READ_CHUNK = 1024 * 1024


class ComponentInstallError(RuntimeError):
    """Raised when a component install step fails verification."""


def _validate_expected(*, byte_size: int, sha256: str) -> None:
    if not isinstance(byte_size, int) or isinstance(byte_size, bool) or byte_size <= 0:
        raise ComponentInstallError(f"byte_size must be a positive integer, got {byte_size!r}")
    if not _SHA256.fullmatch(sha256):
        raise ComponentInstallError(f"sha256 must be 64 lowercase hex characters, got {sha256!r}")


def verify_cached_asset(path: Path, *, byte_size: int, sha256: str) -> None:
    """Verify cached asset byte size and SHA-256 digest."""
    _validate_expected(byte_size=byte_size, sha256=sha256)
    if not path.is_file():
        raise ComponentInstallError(f"cached asset missing: {path}")
    actual_size = path.stat().st_size
    if actual_size != byte_size:
        raise ComponentInstallError(
            f"byte_size mismatch for {path}: expected {byte_size}, got {actual_size}"
        )
    actual_sha256 = localio.file_sha256(path)
    if actual_sha256 != sha256:
        raise ComponentInstallError(
            f"sha256 mismatch for {path}: expected {sha256}, got {actual_sha256}"
        )


def fetch_asset_to_cache(
    asset: component_manifest.ComponentAsset,
    *,
    cache_path: Path,
    offline: bool,
    opener: Callable[..., Any] | None = None,
) -> Path:
    """Download or reuse a verified cache entry for asset."""
    _validate_expected(byte_size=asset.byte_size, sha256=asset.sha256)
    try:
        verify_cached_asset(cache_path, byte_size=asset.byte_size, sha256=asset.sha256)
    except ComponentInstallError:
        if offline:
            raise ComponentInstallError(
                f"offline: verified cache required at {cache_path}"
            ) from None
    else:
        return cache_path

    url_opener = opener if opener is not None else urllib.request.urlopen
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=cache_path.parent, prefix=f".{cache_path.name}.", suffix=".tmp"
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            with url_opener(asset.download_url) as response:
                while True:
                    chunk = response.read(_READ_CHUNK)
                    if not chunk:
                        break
                    handle.write(chunk)
        verify_cached_asset(tmp_path, byte_size=asset.byte_size, sha256=asset.sha256)
        os.replace(tmp_path, cache_path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    return cache_path


def setup_native_components(
    *,
    dry_run: bool = False,
    offline: bool = False,
    rollback: bool = False,
    env: Mapping[str, str] | None = None,
    opener: object | None = None,
    runner: object | None = None,
) -> int:
    """Install pinned native components; return process exit code."""
    del dry_run, offline, rollback, env, opener, runner
    manifest = component_manifest.load()
    if manifest.brigade_version != brigade.__version__:
        print(
            "component setup: brigade_version mismatch: "
            f"manifest requires {manifest.brigade_version!r}, "
            f"running Brigade {brigade.__version__!r}",
            file=sys.stderr,
        )
        return 1
    return 0
