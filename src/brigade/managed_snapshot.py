"""Bundled declarative station contracts for Brigade's managed sidecars."""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from . import station_manifest, templates

SCHEMA = "brigade.managed_snapshot.v1"
STATION_SCHEMA = "brigade.station.v1"


def _manifest_digest(manifest: Mapping[str, Any]) -> str:
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _git_revision(root: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def snapshot_path() -> Path:
    return templates.template_root() / "stations" / "managed-snapshot.json"


def build_snapshot(paths: Sequence[Path]) -> dict[str, Any]:
    if not paths:
        raise ValueError("at least one station manifest path is required")
    records: list[dict[str, Any]] = []
    names: set[str] = set()
    for source_path in paths:
        path = source_path.expanduser().resolve()
        try:
            station_manifest.load(str(path))
        except ValueError as exc:
            raise ValueError(f"invalid station manifest: {path}: {exc}") from exc
        try:
            manifest = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid station manifest: {path}") from exc
        if not isinstance(manifest, dict) or manifest.get("schema") != STATION_SCHEMA:
            raise ValueError(f"invalid station manifest: {path}")
        name = manifest.get("name")
        if not isinstance(name, str) or not name or name in names:
            raise ValueError(f"duplicate or invalid station name: {name!r}")
        names.add(name)
        records.append(
            {
                "manifest": manifest,
                "source": {
                    "repository": path.parent.name,
                    "revision": _git_revision(path.parent),
                    "manifest_sha256": _manifest_digest(manifest),
                },
            }
        )
    records.sort(key=lambda record: record["manifest"]["name"])
    return {"schema": SCHEMA, "records": records}


def render_snapshot(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def load_snapshot(path: Path | None = None) -> dict[str, Any]:
    source_path = path or snapshot_path()
    try:
        payload = json.loads(source_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"managed snapshot could not be loaded: {source_path}") from exc
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        raise ValueError(f"managed snapshot schema must be {SCHEMA}")
    records = payload.get("records")
    if not isinstance(records, list):
        raise ValueError("managed snapshot records must be a list")
    names: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("managed snapshot record must be an object")
        manifest = record.get("manifest")
        source = record.get("source")
        if not isinstance(manifest, dict) or manifest.get("schema") != STATION_SCHEMA:
            raise ValueError("managed snapshot contains an invalid manifest")
        if not isinstance(source, dict) or source.get("manifest_sha256") != _manifest_digest(manifest):
            raise ValueError("managed snapshot manifest digest mismatch")
        if source.get("kind") == "lifecycle-assertion":
            if (
                source.get("asserted_by") != "brigade-cli"
                or not isinstance(source.get("observed_repository"), str)
                or not isinstance(source.get("observed_revision"), str)
            ):
                raise ValueError("managed snapshot lifecycle assertion provenance is invalid")
        name = manifest.get("name")
        if not isinstance(name, str) or not name or name in names:
            raise ValueError(f"managed snapshot duplicate or invalid name: {name!r}")
        names.add(name)
    return payload


def executable_contracts(payload: Mapping[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    snapshot = payload if payload is not None else load_snapshot()
    records = snapshot.get("records")
    if not isinstance(records, list):
        raise ValueError("managed snapshot records must be a list")
    contracts: dict[str, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        manifest = record.get("manifest")
        if not isinstance(manifest, dict) or manifest.get("lifecycle", "active") != "active":
            continue
        station = manifest.get("station")
        tools = manifest.get("tools")
        if not isinstance(station, str) or not isinstance(tools, list):
            continue
        for tool in tools:
            if not isinstance(tool, dict) or tool.get("kind", "executable") != "executable":
                continue
            name = tool.get("name")
            if not isinstance(name, str) or not name:
                raise ValueError("managed snapshot executable tool needs a name")
            contracts[name] = {"station": station, **tool}
    return contracts
