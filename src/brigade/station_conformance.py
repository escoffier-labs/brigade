"""Generate a safe local station conformance kit."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import stat
import tempfile
from typing import Any

from .templates import template_root


SCHEMA = "brigade.station_conformance.v1"
TEMPLATE_DIR = "station-conformance"
FILES = (
    "README.md",
    "station.json",
    "fixtures/example-station",
    "tests/test_station_contract.py",
)
EXECUTABLE_FILES = frozenset({"fixtures/example-station"})
NEXT_COMMANDS = (
    'PATH="$PWD/fixtures:$PATH" brigade stations verify .',
    'PATH="$PWD/fixtures:$PATH" pytest -q tests/test_station_contract.py',
)


def conformance_payload(output: str | os.PathLike[str], *, force: bool = False) -> dict[str, Any]:
    """Return the JSON-ready plan for writing the station conformance kit."""
    output_path = Path(output)
    unsafe = _unsafe_output(output_path)
    if unsafe:
        payload = _base_payload(output_path, force=force)
        payload.update(
            ok=False,
            status="refused",
            would_write=False,
            wrote=False,
            existing=[],
            written=[],
            detail=unsafe,
        )
        return payload
    existing = _existing_entries(output_path)
    refused = (os.path.lexists(output_path) and not output_path.is_dir()) or (bool(existing) and not force)
    payload = _base_payload(output_path, force=force)
    payload["safety"]["writes_outside_output"] = False
    payload.update(
        ok=not refused,
        status="refused" if refused else "ready",
        would_write=not refused,
        wrote=False,
        existing=existing,
        written=[],
        detail=(
            "output path exists and is not a directory"
            if os.path.lexists(output_path) and not output_path.is_dir()
            else "output directory is non-empty; pass force=True to write kit files"
            if refused
            else "station conformance kit is ready to write"
        ),
    )
    return payload


def write_conformance_kit(output: str | os.PathLike[str], *, force: bool = False) -> dict[str, Any]:
    """Write the station conformance kit without running install, probe, or fixture commands."""
    output_path = Path(output)
    payload = conformance_payload(output_path, force=force)
    if not payload["ok"]:
        return payload
    source_root = template_root() / TEMPLATE_DIR
    output_path.mkdir(parents=True, exist_ok=True)
    trusted_root = output_path.resolve(strict=True)
    written: list[dict[str, Any]] = []
    for relative in FILES:
        source = source_root / relative
        destination = output_path / relative
        _ensure_local_parent(output_path, destination.parent, trusted_root)
        if destination.is_symlink():
            return _refuse(payload, f"destination is a symlink: {relative}")
        if source.is_file():
            _atomic_copy(source, destination)
        else:  # pragma: no cover - packaged template drift
            raise FileNotFoundError(f"station conformance template missing: {relative}")
        mode = destination.stat().st_mode
        if relative in EXECUTABLE_FILES:
            destination.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        written.append({"path": relative, "bytes": destination.stat().st_size})

    payload.update(
        ok=True,
        status="written",
        would_write=False,
        wrote=True,
        written=written,
        detail="station conformance kit written",
    )
    return payload


def _base_payload(output: Path, *, force: bool) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "ok": True,
        "status": "ready",
        "output": str(output),
        "force": force,
        "files": list(FILES),
        "safety": {
            "install_executed": False,
            "probe_executed": False,
            "writes_outside_output": None,
            "runtime_dependencies": [],
        },
        "next_commands": list(NEXT_COMMANDS),
    }


def _existing_entries(output: Path) -> list[str]:
    if not output.exists() or not output.is_dir():
        return []
    return sorted(entry.name for entry in output.iterdir())


def _unsafe_output(output: Path) -> str | None:
    absolute = output.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if not os.path.lexists(current):
            break
        if current.is_symlink():
            return f"output path contains a symlink: {current}"
    if os.path.lexists(output) and not output.is_dir():
        return None
    if output.is_dir():
        for relative in FILES:
            current = output
            parts = Path(relative).parts
            for index, part in enumerate(parts):
                current /= part
                if not os.path.lexists(current):
                    break
                if current.is_symlink():
                    return f"output contains a symlink: {current}"
                is_destination = index == len(parts) - 1
                if not is_destination and not current.is_dir():
                    return f"output parent is not a directory: {current}"
                if is_destination and current.is_dir():
                    return f"file destination is a directory: {current}"
    return None


def _ensure_local_parent(output: Path, parent: Path, trusted_root: Path) -> None:
    current = output
    for part in parent.relative_to(output).parts:
        current /= part
        if os.path.lexists(current):
            if current.is_symlink() or not current.is_dir():
                raise ValueError(f"unsafe output parent: {current}")
        else:
            current.mkdir()
        if not current.resolve(strict=True).is_relative_to(trusted_root):
            raise ValueError(f"output parent escapes trusted root: {current}")


def _atomic_copy(source: Path, destination: Path) -> None:
    fd, temp_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as target, source.open("rb") as template:
            shutil.copyfileobj(template, target)
        os.replace(temp_path, destination)
    finally:
        if os.path.lexists(temp_path):
            temp_path.unlink()


def _refuse(payload: dict[str, Any], detail: str) -> dict[str, Any]:
    payload.update(ok=False, status="refused", would_write=False, wrote=False, detail=detail)
    payload["safety"]["writes_outside_output"] = None
    return payload
