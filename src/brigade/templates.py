"""Template discovery and placeholder rendering."""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path
from typing import Any, Dict


TEMPLATE_PKG = "brigade"

TEXT_EXTENSIONS = {".md", ".txt"}


def template_root() -> Path:
    """Return the on-disk path to the packaged templates directory.

    `templates/` is intentionally not a sub-package (no __init__.py), so we
    anchor on the brigade package and descend.

    Assumes a filesystem-backed install (pip wheel or editable). If we ever
    ship as a zip-importable distribution, callers that do `.read_text()` /
    `.is_file()` will need to be migrated to importlib.resources Traversable
    or wrapped in `resources.as_file()`.
    """
    return Path(str(resources.files(TEMPLATE_PKG))) / "templates"


def load_depth_manifest(depth_id: str, *, templates_root: Path | None = None) -> Dict[str, Any]:
    """Load and merge a depth manifest, resolving `extends` chains."""
    return _load_layered("depth", depth_id, templates_root=templates_root)


def load_harness_manifest(harness_id: str, *, templates_root: Path | None = None) -> Dict[str, Any]:
    """Load a harness manifest. Harness manifests do not currently use `extends`."""
    return _load_layered("harnesses", harness_id, templates_root=templates_root)


def load_include_manifest(include_id: str, *, templates_root: Path | None = None) -> Dict[str, Any]:
    """Load an include (add-on) manifest, e.g. `publisher`."""
    return _load_layered("includes", include_id, templates_root=templates_root)


def template_data_path(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError(f"template path escapes the data root: {relative}")
    return path


def _load_layered(kind: str, manifest_id: str, *, templates_root: Path | None = None) -> Dict[str, Any]:
    if templates_root is None:
        path = template_root() / kind / f"{manifest_id}.json"
    else:
        path = template_data_path(templates_root, f"{kind}/{manifest_id}.json")
    if not path.is_file():
        raise FileNotFoundError(f"Unknown {kind}: {manifest_id} (looked at {path})")
    manifest = json.loads(path.read_text())
    parent_id = manifest.get("extends")
    if parent_id:
        parent = _load_layered(kind, parent_id, templates_root=templates_root)
        merged_files = list(parent.get("files", [])) + list(manifest.get("files", []))
        merged_dirs = list(parent.get("dirs", [])) + list(manifest.get("dirs", []))
        manifest["files"] = _dedupe_files(merged_files)
        manifest["dirs"] = sorted(set(merged_dirs))
    return manifest


def _dedupe_files(entries):
    """Keep the last occurrence per destination path."""
    seen: Dict[str, Dict[str, Any]] = {}
    for entry in entries:
        seen[entry["dst"]] = entry
    return list(seen.values())


def render(text: str, context: Dict[str, str]) -> str:
    """Substitute `{{key}}` placeholders. Unknown placeholders are left intact."""
    for key, value in context.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def is_text(src: str) -> bool:
    return Path(src).suffix.lower() in TEXT_EXTENSIONS


def canonical_template_bytes(path: Path) -> bytes:
    """Return UTF-8 template bytes with LF endings, preserving binary assets."""
    data = path.read_bytes()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return data
    return text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")


def harness_memory_owner(harness: str, fallback: str) -> str:
    """Map a harness id to a human-readable memory owner name."""
    mapping = {
        "openclaw": "OpenClaw",
        "hermes": "Hermes",
        "generic": "this repo's memory directory until an orchestrator ingests it",
        "this-repo": "this repo's memory directory until an orchestrator ingests it",
        "this-workspace": "this workspace's memory directory",
    }
    return mapping.get(harness, fallback or harness)
