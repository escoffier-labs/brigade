"""Generate a safe local station conformance kit."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import stat
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
    existing = _existing_entries(output_path)
    refused = bool(existing) and not force
    payload = _base_payload(output_path, force=force)
    payload.update(
        ok=not refused,
        status="refused" if refused else "ready",
        would_write=not refused,
        wrote=False,
        existing=existing,
        written=[],
        detail=(
            "output directory is non-empty; pass force=True to write kit files"
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
    if output_path.exists() and not output_path.is_dir():
        payload.update(
            ok=False,
            status="refused",
            would_write=False,
            detail="output path exists and is not a directory",
        )
        return payload

    source_root = template_root() / TEMPLATE_DIR
    written: list[dict[str, Any]] = []
    for relative in FILES:
        source = source_root / relative
        destination = output_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_file():
            shutil.copyfile(source, destination)
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
            "writes_outside_output": False,
            "runtime_dependencies": [],
        },
        "next_commands": list(NEXT_COMMANDS),
    }


def _existing_entries(output: Path) -> list[str]:
    if not output.exists() or not output.is_dir():
        return []
    return sorted(entry.name for entry in output.iterdir())
