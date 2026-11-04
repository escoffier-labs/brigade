"""Managed compatibility links derived from component families and verified state."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from brigade import component_manifest, component_paths, component_state

if TYPE_CHECKING:
    from collections.abc import Callable


def shim_paths(state: component_state.InstalledState | None, data_root: str) -> list[Path]:
    if state is None:
        return []
    return [
        Path(component_paths.managed_executable_path(data_root, legacy))
        for legacy, canonical in component_manifest.COMPONENT_FAMILIES.items()
        if canonical in state.components
    ]


def remove_shims(
    state: component_state.InstalledState | None,
    data_root: str,
    verify: Callable[..., None],
) -> None:
    if state is None:
        return
    for legacy, canonical in component_manifest.COMPONENT_FAMILIES.items():
        record = state.components.get(canonical)
        if record is None:
            continue
        expected = Path(component_paths.managed_executable_path(data_root, canonical))
        if Path(record.executable) != expected:
            continue
        shim = Path(component_paths.managed_executable_path(data_root, legacy))
        if shim.is_symlink() or not shim.is_file():
            continue
        if not _shares_inode(shim, expected):
            # A copy is ours only while it still matches the recorded asset.
            try:
                verify(shim, byte_size=record.byte_size, sha256=record.sha256)
            except RuntimeError:
                continue
        # A hardlink to the managed executable is ours even when an in-place
        # write corrupted both names, so setup can still repair the canonical file.
        shim.unlink()


def _shares_inode(path: Path, other: Path) -> bool:
    try:
        return other.is_file() and not other.is_symlink() and os.path.samefile(path, other)
    except OSError:
        return False


def create_shims(
    state: component_state.InstalledState,
    data_root: str,
    verify: Callable[..., None],
    materialize: Callable[..., None],
) -> None:
    for legacy, canonical in component_manifest.COMPONENT_FAMILIES.items():
        record = state.components.get(canonical)
        if record is None:
            continue
        source = Path(component_paths.managed_executable_path(data_root, canonical))
        if Path(record.executable) != source:
            raise ValueError(f"shim source is outside managed bin: {canonical}")
        verify(source, byte_size=record.byte_size, sha256=record.sha256)
        shim = Path(component_paths.managed_executable_path(data_root, legacy))
        if shim.exists() or shim.is_symlink():
            raise ValueError(f"refusing to replace unmanaged compatibility executable: {shim}")
        fd, temporary = tempfile.mkstemp(dir=shim.parent, prefix=f".{legacy}.")
        os.close(fd)
        staged = Path(temporary)
        staged.unlink()
        try:
            try:
                if os.name == "nt":
                    raise OSError("Windows uses verified copies")
                os.link(source, staged)
            except OSError:
                materialize(cache_path=source, managed_path=staged)
            verify(staged, byte_size=record.byte_size, sha256=record.sha256)
            os.replace(staged, shim)
        finally:
            staged.unlink(missing_ok=True)
