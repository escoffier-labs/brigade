"""Skill pack build, list, show, archive, and import."""
# ruff: noqa: F401

from __future__ import annotations

import contextlib
import difflib
import hashlib
import json
import os
import shlex
import shutil
import stat as stat_module
import sys
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator, Literal, cast

from .. import __version__ as BRIGADE_VERSION
from .. import mcp_server
from ..localio import slugify, utc_now_iso as _now
from ..projection import kernel as projection
from ..templates import template_root
from ..untrusted import scan_untrusted

from . import registry as _registry_mod
from . import search_validate as _search_validate_mod


def _skill_pack_payload(target: Path) -> dict[str, Any]:
    skills: list[dict[str, Any]] = []
    for row in _registry_mod._iter_registry(target):
        skill_dir = Path(str(row["skill_dir"]))
        metadata = row["metadata"]
        skill_id = _registry_mod._slug(str(metadata.get("id") or skill_dir.name))
        lint_payload = _search_validate_mod._lint_payload(target, f"registry:{skill_id}")
        skills.append(
            {
                "id": skill_id,
                "title": metadata.get("title"),
                "version": str(metadata.get("version") or "0.1.0"),
                "trust_level": metadata.get("trust_level") or "unreviewed",
                "fingerprint": lint_payload.get("fingerprint"),
                "source_path": f"skills/{skill_id}",
                "valid": lint_payload.get("valid"),
                "errors": lint_payload.get("errors"),
                "warnings": lint_payload.get("warnings"),
                "trust_score": lint_payload.get("trust_score"),
                "changelog": lint_payload.get("changelog"),
            }
        )
    payload = {
        "pack_format": "brigade-skill-pack-v1",
        "created_at": _now(),
        "target": str(target),
        "skill_count": len(skills),
        "skills": skills,
    }
    payload["evidence_fingerprint"] = hashlib.sha256(
        json.dumps(
            [{"id": item["id"], "fingerprint": item["fingerprint"], "version": item["version"]} for item in skills],
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return payload


def pack_build(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    pack_id = f"{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-skill-pack"
    pack_dir = _registry_mod._skill_packs_root(target) / pack_id
    try:
        with _registry_mod._held_state_root(target) as state_anchor:
            payload = _skill_pack_payload(target)
            payload.update({"pack_id": pack_id, "status": "built"})
            for row in _registry_mod._iter_registry(target):
                skill_dir = Path(str(row["skill_dir"]))
                skill_id = _registry_mod._slug(str(row["metadata"].get("id") or skill_dir.name))
                snapshot_dirs, snapshot_files = _registry_mod._read_registry_entry_tree(state_anchor, skill_id)
                _registry_mod._write_collected_tree_into_anchor(
                    snapshot_dirs, snapshot_files, state_anchor, "skills", "packs", pack_id, "skills", skill_id
                )
            _registry_mod._write_state_file(
                state_anchor, "skills", "packs", pack_id, "skill-pack.json", data=_registry_mod._json_bytes(payload)
            )
            _registry_mod._write_state_file(
                state_anchor,
                "skills",
                "packs",
                pack_id,
                "SKILL_PACK.md",
                data=(
                    f"# Skill Pack {pack_id}\n\n"
                    f"- skills: {payload['skill_count']}\n"
                    f"- fingerprint: {payload['evidence_fingerprint']}\n"
                    f"- import: brigade skills pack import {pack_dir}\n"
                ).encode("utf-8"),
            )
    except _registry_mod.SkillsStatePathError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    payload["path"] = str(pack_dir)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"skill_pack: {pack_id}")
    print(f"path: {pack_dir}")
    print(f"skills: {payload['skill_count']}")
    return 0


def _skill_packs(target: Path) -> list[dict[str, Any]]:
    """Enumerate pack manifests through the held state-root anchor.

    Pack directories are listed by descriptor (symlinked entries are skipped,
    never followed) and each ``skill-pack.json`` is read through
    ``_read_state_file_bytes``; a refused read fails the whole listing closed.
    """
    packs: list[dict[str, Any]] = []
    try:
        with _registry_mod._held_state_root(target) as anchor:
            for sub, archived in ((("skills", "packs"), False), (("skills", "packs-archive"), True)):
                names = _registry_mod._plain_subdirs_under_anchor(anchor, *sub)
                if not names:
                    continue
                for name in names:
                    payload = _anchored_pack_manifest_at(anchor, *sub, name)
                    if payload:
                        payload.setdefault("path", str(_pack_display(target, *sub, name)))
                        payload.setdefault("archived", archived)
                        packs.append(payload)
    except _registry_mod.SkillsStatePathError:
        return []
    packs.sort(key=lambda item: str(item.get("created_at") or item.get("pack_id") or ""), reverse=True)
    return packs


def _pack_display(target: Path, *relative: str) -> Path:
    return (target / ".brigade").joinpath(*relative)


def _is_under_state_root(path: Path, target: Path) -> bool:
    """True when *path* is lexically inside the workspace's ``.brigade`` state root.

    Deliberately no ``resolve()``: resolving would let a planted symlink
    re-classify state-root content as a trusted external path. Same-uid
    replacement of trusted ancestors stays out of scope (#1093).
    """
    return _lexical_state_root_parts(target, path) is not None


def _lexical_state_root_parts(target: Path, path: Path) -> tuple[str, ...] | None:
    """Classify an un-resolved path against ``<target>/.brigade`` lexically.

    Returns the path's components below ``.brigade`` (empty tuple for the
    state root itself) or ``None`` when the path lies outside it.
    """
    try:
        candidate = os.path.normpath(os.path.abspath(os.path.expanduser(str(path))))
        state_root = os.path.normpath(os.path.abspath(str(target / ".brigade")))
    except OSError:
        return None
    if candidate == state_root:
        return ()
    prefix = state_root + os.sep
    if not candidate.startswith(prefix):
        return None
    parts = tuple(part for part in candidate[len(prefix) :].split(os.sep) if part)
    if ".." in parts or not parts:
        return None
    return parts


def _state_root_selector_kind(target: Path, requested: str) -> str:
    """``"registry"``, ``"refuse"``, or ``"external"`` for a raw path selector.

    Classification is lexical and must precede any ``exists()`` probe: an
    existing registry pathname still selects the anchored registry entry, and
    every other location inside the attacker-influenced state root is refused
    as a pathname source instead of being read through the filesystem.
    """
    parts = _lexical_state_root_parts(target, Path(requested))
    if parts is None:
        return "external"
    if len(parts) >= 3 and parts[:2] == ("skills", "registry") and _registry_mod._slug(parts[2]) == parts[2]:
        if all(part not in {"", ".", ".."} for part in parts[3:]):
            return "registry"
    return "refuse"


def _find_skill_pack(target: Path, pack_id: str) -> tuple[dict[str, Any] | None, str | None]:
    packs = _skill_packs(target)
    if pack_id == "latest":
        return (packs[0], None) if packs else (None, "skill pack not found: latest")
    matches = [pack for pack in packs if str(pack.get("pack_id") or "").startswith(pack_id)]
    if not matches:
        path = Path(pack_id).expanduser()
        # A user-supplied external pack path stays readable, but locations
        # inside the state root are only ever served through the anchored
        # enumeration above.
        if not _is_under_state_root(path, target) and path.is_dir() and (path / "skill-pack.json").is_file():
            payload = _registry_mod._read_json(path / "skill-pack.json")
            payload.setdefault("path", str(path))
            return payload, None
        return None, f"skill pack not found: {pack_id}"
    if len(matches) > 1:
        return None, f"skill pack id is ambiguous: {pack_id}"
    return matches[0], None


def pack_list(*, target: Path, json_output: bool = False, limit: int = 20) -> int:
    target = target.expanduser().resolve()
    packs = _skill_packs(target)[:limit]
    payload = {"target": str(target), "pack_count": len(packs), "packs": packs}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"skill packs: {target}")
    for pack in packs:
        suffix = " archived=true" if pack.get("archived") else ""
        print(f"- {pack.get('pack_id')} skills={pack.get('skill_count')}{suffix}")
    if not packs:
        print("no skill packs")
    return 0


def pack_show(*, target: Path, pack_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    pack, error = _find_skill_pack(target, pack_id)
    if pack is None:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if json_output:
        print(json.dumps({"target": str(target), "pack": pack}, indent=2, sort_keys=True))
        return 0
    print(f"skill_pack: {pack.get('pack_id')}")
    print(f"skills: {pack.get('skill_count')}")
    print(f"fingerprint: {pack.get('evidence_fingerprint')}")
    return 0


def _anchored_pack_manifest_at(anchor: _registry_mod._StateRootAnchor, *relative: str) -> dict[str, Any]:
    """Read one pack manifest from the anchored state root (never by external path)."""
    raw = _registry_mod._read_state_file_bytes(anchor, *relative, "skill-pack.json")
    if raw is None:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _anchored_pack_manifest(anchor: _registry_mod._StateRootAnchor, name: str) -> dict[str, Any]:
    return _anchored_pack_manifest_at(anchor, "skills", "packs", name)


def pack_archive(*, target: Path, pack_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    archive_root = _registry_mod._skill_packs_archive_root(target)
    try:
        with _registry_mod._held_state_root(target) as state_anchor:
            if not _registry_mod._HAS_DESCRIPTOR_ANCHOR:
                raise _registry_mod._fallback_mutation_refusal("moves", archive_root)
            # The archived entry is always a validated plain directory
            # directly under the anchored packs root; a manifest-supplied
            # physical path is never honoured as the move source.
            candidates: list[tuple[str, dict[str, Any]]] = []
            try:
                packs_fd, packs_opened = _registry_mod._anchor_open_chain(state_anchor, "skills", "packs")
            except _registry_mod.SkillsStatePathError:
                packs_fd, packs_opened = None, []
            if packs_fd is not None:
                try:
                    for name in sorted(os.listdir(packs_fd)):
                        try:
                            st = os.lstat(name, dir_fd=packs_fd)
                        except FileNotFoundError:
                            continue
                        if stat_module.S_ISDIR(st.st_mode):
                            candidates.append((name, _anchored_pack_manifest(state_anchor, name)))
                finally:
                    for fd in reversed(packs_opened):
                        os.close(fd)
            if pack_id == "latest":
                dated = [row for row in candidates if row[1]]
                matches = [max(dated, key=lambda row: (str(row[1].get("created_at") or ""), row[0]))] if dated else []
            else:
                matches = [row for row in candidates if row[0].startswith(pack_id)]
            if not matches:
                print(f"error: skill pack not found: {pack_id}", file=sys.stderr)
                return 1
            if len(matches) > 1:
                print(f"error: skill pack id is ambiguous: {pack_id}", file=sys.stderr)
                return 1
            pack_name, _manifest = matches[0]
            destination = archive_root / pack_name
            archive_fd, archive_opened = _registry_mod._anchor_open_chain(
                state_anchor, "skills", "packs-archive", create=True
            )
            try:
                packs_fd, packs_opened = _registry_mod._anchor_open_chain(state_anchor, "skills", "packs")
                try:
                    try:
                        os.stat(pack_name, dir_fd=archive_fd, follow_symlinks=False)
                    except FileNotFoundError:
                        pass
                    else:
                        print(f"error: archived skill pack already exists: {destination}", file=sys.stderr)
                        return 2
                    with _registry_mod._anchored_fs_errors(str(destination)):
                        os.rename(pack_name, pack_name, src_dir_fd=packs_fd, dst_dir_fd=archive_fd)
                finally:
                    for fd in reversed(packs_opened):
                        os.close(fd)
            finally:
                for fd in reversed(archive_opened):
                    os.close(fd)
    except _registry_mod.SkillsStatePathError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    payload = {
        "target": str(target),
        "pack_id": pack_name,
        "status": "archived",
        "archive_path": str(destination),
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"archived: {pack_name}")
    print(f"path: {destination}")
    return 0


def _pack_import_from_dir(
    *,
    target: Path,
    pack_dir: Path,
    display_dir: Path,
    force: bool,
    json_output: bool,
) -> int:
    """Import one already-located pack directory, reporting *display_dir* paths."""
    manifest = _registry_mod._read_json(pack_dir / "skill-pack.json")
    skills_dir = pack_dir / "skills"
    if not manifest or not skills_dir.is_dir():
        print(f"error: not a skill pack: {display_dir}", file=sys.stderr)
        return 2
    imported: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    errors: list[str] = []
    skill_paths = sorted(path for path in skills_dir.iterdir() if path.is_dir())
    for skill_path in skill_paths:
        metadata = _registry_mod._read_json(skill_path / "skill.json")
        skill_id = _registry_mod._slug(str(metadata.get("id") or skill_path.name))
        if _registry_mod._registry_entry_present(target, skill_id) and not force:
            conflicts.append(
                {
                    "skill_id": skill_id,
                    "existing": str(_registry_mod._skill_path(target, skill_id)),
                    "source": str(skill_path),
                }
            )
    if conflicts:
        payload = {
            "target": str(target),
            "pack": str(display_dir),
            "imported": imported,
            "conflicts": conflicts,
            "errors": errors,
            "valid": False,
        }
        if json_output:
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 1
        for conflict in conflicts:
            print(f"conflict: {conflict['skill_id']} already exists at {conflict['existing']}")
        return 1
    for skill_path in skill_paths:
        metadata = _registry_mod._read_json(skill_path / "skill.json")
        skill_id = _registry_mod._slug(str(metadata.get("id") or skill_path.name))
        # Provenance keeps the displayed pack location; imports from an
        # anchored snapshot staging copy never record the staging path.
        row, error, rc = _registry_mod._registry_import_payload(
            target=target,
            source=skill_path,
            skill_id=skill_id,
            force=force,
            source_provenance=display_dir / "skills" / skill_path.name,
        )
        if row is None:
            errors.append(str(error))
            continue
        imported.append({"skill_id": row["skill_id"], "skill_dir": row["skill_dir"], "returncode": rc})
    result = {
        "target": str(target),
        "pack": str(display_dir),
        "pack_id": manifest.get("pack_id"),
        "valid": not errors,
        "imported_count": len(imported),
        "imported": imported,
        "conflicts": conflicts,
        "errors": errors,
    }
    if json_output:
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if not errors else 1
    print(f"skill_pack_import: {manifest.get('pack_id') or display_dir.name}")
    print(f"imported: {len(imported)}")
    for error in errors:
        print(f"error: {error}")
    return 0 if not errors else 1


def pack_import(*, target: Path, pack: Path, force: bool = False, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    raw_pack = Path(pack).expanduser()
    display_pack = Path(os.path.normpath(os.path.abspath(str(raw_pack))))
    if _is_under_state_root(raw_pack, target):
        # A state-root pack is served only through the anchored enumeration;
        # resolving the pathname would let a planted symlink pose as a
        # trusted external pack directory.
        pack_name = display_pack.name
        snapshot: tuple[list[tuple[str, ...]], dict[tuple[str, ...], bytes]] | None = None
        try:
            with _registry_mod._held_state_root(target) as anchor:
                for sub in (("skills", "packs"), ("skills", "packs-archive")):
                    names = _registry_mod._plain_subdirs_under_anchor(anchor, *sub)
                    if names and pack_name in names:
                        snapshot = _registry_mod._read_tree_from_anchor(anchor, *sub, pack_name)
                        break
        except _registry_mod.SkillsStatePathError:
            snapshot = None
        if snapshot is None:
            print(f"error: skill pack not found: {display_pack}", file=sys.stderr)
            return 2
        with tempfile.TemporaryDirectory(prefix="brigade-pack-import-") as staging:
            staged_pack = Path(staging) / pack_name
            _registry_mod._write_snapshot_tree(snapshot[0], snapshot[1], staged_pack)
            return _pack_import_from_dir(
                target=target,
                pack_dir=staged_pack,
                display_dir=display_pack,
                force=force,
                json_output=json_output,
            )
    return _pack_import_from_dir(
        target=target,
        pack_dir=Path(os.path.normpath(os.path.abspath(str(raw_pack)))),
        display_dir=display_pack,
        force=force,
        json_output=json_output,
    )
