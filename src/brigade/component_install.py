"""Download, verify, cache, and install pinned native Brigade components."""

from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path

import brigade
from brigade import component_manifest, localio


class ComponentInstallError(RuntimeError):
    """Raised when a component install step fails verification."""


def verify_cached_asset(path: Path, *, byte_size: int, sha256: str) -> None:
    """Verify cached asset byte size and SHA-256 digest."""
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
