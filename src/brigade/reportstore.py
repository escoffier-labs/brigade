"""Shared primitives for the directory-per-bundle report lifecycle.

These helpers were extracted from near-identical private copies behind
`center report` (center_cmd), `repos report` and `repos release`
(repos_cmd), and `release candidate` (release_cmd). Each station keeps
its own roots, payload builders, markdown renderers, receipt shapes,
and output text; this module owns the evidence-file path convention,
the bundle read annotation, root listing and newest-first sorting,
latest/id-prefix resolution, the evidence-plus-documents bundle write,
the CLOSEOUT.json stamp, and the archive move.

The `work phases report` store in phases_cmd stays local on purpose:
it lists bundles by globbing `*/PHASE_EVIDENCE.json` in name order,
picks the latest report by ascending created_at, resolves ids by glob
count with a distinct "invalid phase report" error path, and has no
archive root. The repos release-train reader also stays local because
it strips "path" and stamps a privacy-safe "path_label" instead of the
shared "path" annotation, and its closeout write stays on plain
write_json because the train CLOSEOUT.json intentionally carries no
"path" field for the same privacy reason.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any, Callable, Iterable

from .localio import read_json_dict, write_json

CLOSEOUT_STATUSES = frozenset({"reviewed", "deferred", "superseded", "archived"})
_TIMESTAMP_PREFIXED_DIR = re.compile(r"^\d{8}-\d{6}")


def bundle_json_path(path: Path, evidence_name: str) -> Path:
    """Return the evidence JSON path inside a bundle dir, or path itself when it is a file."""
    return path / evidence_name if path.is_dir() else path


def read_bundle(path: Path, evidence_name: str) -> dict[str, Any] | None:
    """Read a bundle's evidence JSON, defaulting its "path" to the bundle directory."""
    json_path = bundle_json_path(path, evidence_name)
    payload = read_json_dict(json_path)
    if payload is not None:
        payload.setdefault("path", str(json_path.parent))
    return payload


def _bundle_dirs(
    roots: Iterable[Path],
    *,
    skip_child: Callable[[str], bool] | None = None,
) -> list[Path]:
    children: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        for child in root.iterdir():
            if child.is_dir() and (skip_child is None or not skip_child(child.name)):
                children.append(child)
    children.sort(key=lambda path: path.name, reverse=True)
    return children


def _dir_names_are_timestamp_prefixed(children: list[Path]) -> bool:
    return bool(children) and all(_TIMESTAMP_PREFIXED_DIR.match(child.name) for child in children)


def list_bundles(
    roots: Iterable[Path],
    read: Callable[[Path], dict[str, Any] | None],
    *,
    id_field: str,
    skip_child: Callable[[str], bool] | None = None,
) -> list[dict[str, Any]]:
    """Read the bundle dirs under roots via read, newest first by created_at then id_field."""
    bundles: list[dict[str, Any]] = []
    for child in _bundle_dirs(roots, skip_child=skip_child):
        payload = read(child)
        if payload is not None:
            bundles.append(payload)
    bundles.sort(key=lambda item: str(item.get("created_at") or item.get(id_field) or ""), reverse=True)
    return bundles


def latest_bundles(
    roots: Iterable[Path],
    read: Callable[[Path], dict[str, Any] | None],
    *,
    id_field: str,
    limit: int = 1,
    skip_child: Callable[[str], bool] | None = None,
) -> list[dict[str, Any]]:
    """Read up to limit newest bundles without decoding the full history when possible.

    Production bundle dirs are timestamp-prefixed and sortable by name; in that case
    only the newest ``limit`` dirs are decoded. Legacy or arbitrary dir names fall
    back to created_at ordering, which may require decoding every bundle.
    """
    if limit < 1:
        return []

    def sort_key(item: dict[str, Any]) -> str:
        return str(item.get("created_at") or item.get(id_field) or "")

    children = _bundle_dirs(roots, skip_child=skip_child)
    bundles: list[dict[str, Any]] = []
    if _dir_names_are_timestamp_prefixed(children):
        for child in children:
            payload = read(child)
            if payload is not None:
                bundles.append(payload)
            if len(bundles) >= limit:
                break
    else:
        for child in children:
            payload = read(child)
            if payload is None:
                continue
            bundles.append(payload)
            if len(bundles) > limit:
                bundles.sort(key=sort_key, reverse=True)
                del bundles[limit:]
    bundles.sort(key=sort_key, reverse=True)
    return bundles


def resolve_bundle(
    bundles: list[dict[str, Any]],
    bundle_id: str,
    *,
    id_field: str,
    label: str,
    latest: Callable[[], dict[str, Any] | None],
) -> tuple[dict[str, Any] | None, str | None]:
    """Resolve "latest" via the latest callable, or a unique id_field prefix against bundles.

    Returns (bundle, None) on success, otherwise (None, error) where the error
    is a "{label} not found" or "{label} id is ambiguous" message.
    """
    if bundle_id == "latest":
        found = latest()
        return (found, None) if found else (None, f"{label} not found: latest")
    matches = [item for item in bundles if str(item.get(id_field) or "").startswith(bundle_id)]
    if not matches:
        return None, f"{label} not found: {bundle_id}"
    if len(matches) > 1:
        return None, f"{label} id is ambiguous: {bundle_id}"
    return matches[0], None


def write_bundle(
    bundle_dir: Path,
    payload: dict[str, Any],
    *,
    evidence_name: str,
    documents: dict[str, str],
) -> None:
    """Write the evidence JSON and the rendered text documents for a bundle."""
    write_json(bundle_dir / evidence_name, payload)
    for name, text in documents.items():
        (bundle_dir / name).write_text(text)


def write_closeout(bundle_dir: Path, closeout: dict[str, Any]) -> Path:
    """Write CLOSEOUT.json under bundle_dir, stamping closeout["path"]; return its path."""
    closeout_path = bundle_dir / "CLOSEOUT.json"
    closeout["path"] = str(closeout_path)
    write_json(closeout_path, closeout)
    return closeout_path


def move_bundle(source: Path, archive_root: Path) -> tuple[Path, bool]:
    """Move source into archive_root, creating it; returns (destination, moved).

    moved is False when the destination already exists, in which case the
    source is left in place.
    """
    destination = archive_root / source.name
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        return destination, False
    shutil.move(str(source), str(destination))
    return destination, True
