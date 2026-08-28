"""Reviewed skill registry commands.

Skills are treated as versioned workflow code: imports land in a local Brigade
registry, lint checks provenance and injection risk, and installs materialize
reviewed packs into harness-specific folders.

Residuals (accepted, documented): rollback validates restored bytes to the
fresh-install bar against a capture-time fingerprint and does not claim
state-root content authenticity; and on platforms without descriptor
anchoring (Windows), state-root reads fall back to the lstat-guarded walker,
which closes symlink/reparse redirection but keeps a small unavoidable
check-then-use window because no held directory descriptor exists there.

Further residuals from the final #1211 review are tracked in escoffier-labs/brigade#1214:
generic ``import`` and ``inbox add`` classify a source only after resolving it;
forced install snapshots a state-backed destination by pathname before the
anchored delete refuses it; the lexical classifier does not normalise case or a
symlinked workspace alias; outcome discovery walks the registry by pathname;
plus small staging-path, hash-oracle, name-disclosure, and control-character
items. All need write access to the state root without read access to the
trusted skill directories.
"""
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
from typing import Any, Iterator, Literal, cast

from .. import __version__ as BRIGADE_VERSION
from .. import mcp_server
from ..localio import slugify, utc_now_iso as _now
from ..projection import kernel as projection
from ..templates import template_root
from ..untrusted import scan_untrusted

from . import search_validate
from . import packs as _packs_mod

OK = "ok"
WARN = "warn"
FAIL = "fail"

HARNESS_ADAPTERS: dict[str, dict[str, Any]] = {
    "codex": {"status": "built-in", "format": "codex-skill", "install_path": ".codex/skills/{skill_id}"},
    "claude": {"status": "built-in", "format": "claude-skill", "install_path": ".claude/skills/{skill_id}"},
    "opencode": {"status": "built-in", "format": "opencode-skill", "install_path": ".opencode/skills/{skill_id}"},
    "antigravity": {
        "status": "built-in",
        "format": "antigravity-skill",
        "install_path": ".antigravity/skills/{skill_id}",
    },
    "pi": {"status": "built-in", "format": "pi-skill", "install_path": ".pi/skills/{skill_id}"},
    "cursor": {"status": "built-in", "format": "cursor-skill", "install_path": ".cursor/skills/{skill_id}"},
    "aider": {"status": "built-in", "format": "aider-skill", "install_path": ".aider/skills/{skill_id}"},
    "goose": {"status": "built-in", "format": "goose-skill", "install_path": ".goose/skills/{skill_id}"},
    "continue": {"status": "built-in", "format": "continue-skill", "install_path": ".continue/skills/{skill_id}"},
    "copilot": {"status": "built-in", "format": "copilot-skill", "install_path": ".copilot/skills/{skill_id}"},
    "qwen": {"status": "built-in", "format": "qwen-skill", "install_path": ".qwen/skills/{skill_id}"},
    "kimi": {"status": "built-in", "format": "kimi-skill", "install_path": ".kimi/skills/{skill_id}"},
    "adal": {"status": "built-in", "format": "adal-skill", "install_path": ".adal/skills/{skill_id}"},
    "openhands": {"status": "built-in", "format": "openhands-skill", "install_path": ".openhands/skills/{skill_id}"},
    "grok": {"status": "built-in", "format": "grok-skill", "install_path": ".grok/skills/{skill_id}"},
    "amp": {"status": "built-in", "format": "amp-skill", "install_path": ".amp/skills/{skill_id}"},
    "crush": {"status": "built-in", "format": "crush-skill", "install_path": ".crush/skills/{skill_id}"},
    "openclaw": {"status": "built-in", "format": "openclaw-skill", "install_path": ".openclaw/skills/{skill_id}"},
    "hermes": {"status": "built-in", "format": "hermes-skill", "install_path": ".hermes/skills/{skill_id}"},
    "mcp": {"status": "built-in", "format": "mcp-resource", "install_path": ".brigade/skills/mcp-resources/{skill_id}"},
}
HARNESS_TARGETS = tuple(key for key, value in HARNESS_ADAPTERS.items() if value["status"] == "built-in")
INSTALL_TARGETS = (*HARNESS_TARGETS, "all")
TRUST_LEVELS = ("unreviewed", "workspace", "team", "public")
SOURCE_SCHEMA_VERSION = 1
RECEIPT_SCHEMA_VERSION = 2
RENDERER_SCHEMA_VERSION = 1
BUNDLED_SOURCE_PREFIX = "brigade://bundled-skills/"


def _slug(value: str) -> str:
    return slugify(value, fallback="skill")


def _registry_root(target: Path) -> Path:
    return target / ".brigade" / "skills" / "registry"


def _inbox_root(target: Path) -> Path:
    return target / ".brigade" / "skills" / "inbox"


def _adapters_config_path(target: Path) -> Path:
    return target / ".brigade" / "skills" / "adapters.json"


def _rollback_root(target: Path, skill_id: str, harness: str) -> Path:
    return target / ".brigade" / "skills" / "rollback" / _slug(skill_id) / harness


def _hermes_home() -> Path:
    """Hermes data dir (HERMES_HOME, default ~/.hermes). Its existence proxies 'Hermes is installed'."""
    return Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes")).expanduser()


def _hermes_skills_root() -> Path:
    """Where real Hermes auto-discovers Brigade-installed skills (a 'local' skill category)."""
    return _hermes_home() / "skills" / "brigade-imports"


def _installs_root(target: Path) -> Path:
    return target / ".brigade" / "skills" / "installs"


def _install_history_path(target: Path) -> Path:
    return _installs_root(target) / "history.jsonl"


def _skill_packs_root(target: Path) -> Path:
    return target / ".brigade" / "skills" / "packs"


def _skill_packs_archive_root(target: Path) -> Path:
    return target / ".brigade" / "skills" / "packs-archive"


def _skill_path(target: Path, skill_id: str) -> Path:
    return _registry_root(target) / _slug(skill_id)


def _metadata_path(skill_dir: Path) -> Path:
    return skill_dir / "skill.json"


def _skill_md_path(skill_dir: Path) -> Path:
    return skill_dir / "SKILL.md"


def _json_string(value: str) -> str:
    return json.dumps(" ".join(value.split()))


def _has_yaml_frontmatter(text: str) -> bool:
    lines = text.splitlines()
    if len(lines) < 2 or lines[0].strip() != "---":
        return False
    return any(line.strip() == "---" for line in lines[1:])


def _codex_frontmatter_values(metadata: dict[str, Any], skill_id: str) -> dict[str, str]:
    name = _slug(str(metadata.get("id") or skill_id))
    description = str(
        metadata.get("description") or metadata.get("title") or f"Use this reviewed Brigade skill for {name}."
    )
    return {"name": name, "description": description}


def _codex_frontmatter(metadata: dict[str, Any], skill_id: str) -> str:
    values = _codex_frontmatter_values(metadata, skill_id)
    return "\n".join(
        [
            "---",
            f"name: {_json_string(values['name'])}",
            f"description: {_json_string(values['description'])}",
            "---",
            "",
        ]
    )


def _ensure_codex_frontmatter(text: str, metadata: dict[str, Any], skill_id: str) -> str:
    if not _has_yaml_frontmatter(text):
        return _codex_frontmatter(metadata, skill_id) + text
    lines = text.splitlines(keepends=True)
    closing_index = next(index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---")
    existing_keys = {
        line.split(":", 1)[0].strip()
        for line in lines[1:closing_index]
        if ":" in line and not line.lstrip().startswith("#")
    }
    values = _codex_frontmatter_values(metadata, skill_id)
    additions = []
    if "name" not in existing_keys:
        additions.append(f"name: {_json_string(values['name'])}\n")
    if "description" not in existing_keys:
        additions.append(f"description: {_json_string(values['description'])}\n")
    if not additions:
        return text
    return "".join(lines[:closing_index] + additions + lines[closing_index:])


def _hermes_frontmatter_values(metadata: dict[str, Any], skill_id: str) -> dict[str, str]:
    name = _slug(str(metadata.get("id") or skill_id))
    description = str(metadata.get("description") or metadata.get("title") or f"Reviewed Brigade skill for {name}.")
    version = str(metadata.get("version") or "0.1.0")
    return {"name": name, "description": description, "version": version}


def _hermes_frontmatter(metadata: dict[str, Any], skill_id: str) -> str:
    values = _hermes_frontmatter_values(metadata, skill_id)
    return "\n".join(
        [
            "---",
            f"name: {_json_string(values['name'])}",
            f"description: {_json_string(values['description'])}",
            f"version: {_json_string(values['version'])}",
            "---",
            "",
        ]
    )


def _ensure_hermes_frontmatter(text: str, metadata: dict[str, Any], skill_id: str) -> str:
    if not _has_yaml_frontmatter(text):
        return _hermes_frontmatter(metadata, skill_id) + text
    lines = text.splitlines(keepends=True)
    closing_index = next(index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---")
    existing_keys = {
        line.split(":", 1)[0].strip()
        for line in lines[1:closing_index]
        if ":" in line and not line.lstrip().startswith("#")
    }
    values = _hermes_frontmatter_values(metadata, skill_id)
    additions = [
        f"{key}: {_json_string(values[key])}\n"
        for key in ("name", "description", "version")
        if key not in existing_keys
    ]
    if not additions:
        return text
    return "".join(lines[:closing_index] + additions + lines[closing_index:])


def _render_skill_text_for_harness(text: str, metadata: dict[str, Any], skill_id: str, harness: str) -> str:
    rendered = text if text.endswith("\n") else text + "\n"
    if harness == "codex":
        rendered = _ensure_codex_frontmatter(rendered, metadata, skill_id)
    elif harness == "hermes":
        rendered = _ensure_hermes_frontmatter(rendered, metadata, skill_id)
    return rendered


def _rendered_skill_validation(text: str, harness: str) -> list[str]:
    errors: list[str] = []
    if not text.strip():
        errors.append("rendered SKILL.md is empty")
    if harness == "codex" and not _has_yaml_frontmatter(text):
        errors.append("codex SKILL.md missing YAML frontmatter")
    if harness == "codex" and _has_yaml_frontmatter(text):
        lines = text.splitlines()
        closing_index = next(index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---")
        existing_keys = {
            line.split(":", 1)[0].strip()
            for line in lines[1:closing_index]
            if ":" in line and not line.lstrip().startswith("#")
        }
        for key in ("name", "description"):
            if key not in existing_keys:
                errors.append(f"codex SKILL.md frontmatter missing {key}")
    if harness == "hermes" and not _has_yaml_frontmatter(text):
        errors.append("hermes SKILL.md missing YAML frontmatter")
    return errors


def _write_snapshot_tree(dirs: list[tuple[str, ...]], files: dict[tuple[str, ...], bytes], dest: Path) -> None:
    """Materialize a collected snapshot at *dest* (trusted, non-state-root destination).

    Every byte written comes from the snapshot; no source file is re-read by
    path, so a symlink planted in the source after collection cannot smuggle
    outside content into the copy.
    """
    if dest.is_symlink() or _is_reparse_point(dest):
        dest.unlink()
    elif dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    for parts in dirs:
        (dest.joinpath(*parts)).mkdir(parents=True, exist_ok=True)
    for parts, data in files.items():
        (dest.joinpath(*parts)).write_bytes(data)


def _files_fingerprint(files: dict[tuple[str, ...], bytes]) -> str:
    """``_fingerprint`` over an already-collected snapshot (same digest layout)."""
    digest = hashlib.sha256()
    for parts in sorted(files, key=lambda key: "/".join(key)):
        if parts[-1] in {".DS_Store", "skill.json"}:
            continue
        digest.update("/".join(parts).encode("utf-8"))
        digest.update(b"\0")
        digest.update(files[parts])
        digest.update(b"\0")
    return digest.hexdigest()


def _bytes_fingerprint(data: bytes | None) -> str:
    return hashlib.sha256(b"<missing>" if data is None else data).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _fingerprint(skill_dir: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in skill_dir.rglob("*") if p.is_file()):
        if path.name in {".DS_Store", "skill.json"}:
            continue
        rel = path.relative_to(skill_dir).as_posix()
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _text_fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _file_fingerprint(path: Path) -> str:
    content = path.read_bytes() if path.is_file() else b"<missing>"
    return hashlib.sha256(content).hexdigest()


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.is_file():
        return rows
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return rows
    for line in lines:
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def _changelog_payload(skill_dir: Path, metadata: dict[str, Any], *, contain: bool = False) -> dict[str, Any]:
    configured = metadata.get("changelog_path") or metadata.get("changelog")
    candidates: list[Path] = []
    if isinstance(configured, str) and configured.strip():
        configured_path = Path(configured).expanduser()
        # Contained mode (state-root served content) never reaches outside the
        # skill directory for auxiliary files: an absolute or escaping
        # changelog_path would launder outside file content into payloads.
        escapes = configured_path.is_absolute() or ".." in configured_path.parts
        if not (contain and escapes):
            candidates.append(configured_path if configured_path.is_absolute() else skill_dir / configured_path)
    candidates.append(skill_dir / "CHANGELOG.md")
    path = next((candidate for candidate in candidates if candidate.is_file()), None)
    headings: list[str] = []
    fingerprint: str | None = None
    if path is not None:
        text = path.read_text(encoding="utf-8", errors="replace")
        fingerprint = _text_fingerprint(text)
        headings = [line.strip("# ").strip() for line in text.splitlines() if line.startswith("#")][:8]
    return {
        "present": path is not None,
        "path": str(path) if path is not None else None,
        "fingerprint": fingerprint,
        "headings": headings,
    }


def _trust_score_payload(
    skill_dir: Path,
    metadata: dict[str, Any],
    lint_payload: dict[str, Any] | None = None,
    source: dict[str, Any] | None = None,
    *,
    contain: bool = False,
) -> dict[str, Any]:
    score = 100
    signals: list[str] = []
    bundled_reviewed = bool(
        source
        and source.get("kind") == "brigade-bundle"
        and source.get("reviewed") is True
        and source.get("identity") == f"{BUNDLED_SOURCE_PREFIX}{_slug(str(metadata.get('id') or skill_dir.name))}"
    )
    trust_level = "bundled" if bundled_reviewed else str(metadata.get("trust_level") or "unreviewed")
    if bundled_reviewed:
        signals.append("canonical Brigade bundle is reviewed")
    elif trust_level == "unreviewed":
        score -= 35
        signals.append("trust_level is unreviewed")
    elif trust_level == "workspace":
        score -= 10
        signals.append("trust_level is workspace")
    elif trust_level not in {"team", "public"}:
        score -= 20
        signals.append(f"unknown trust_level: {trust_level}")
    tests = raw_tests if isinstance((raw_tests := metadata.get("tests")), list) else []
    if not tests:
        score -= 15
        signals.append("no tests declared")
    changelog = _changelog_payload(skill_dir, metadata, contain=contain)
    if not changelog["present"]:
        score -= 5
        signals.append("no changelog found")
    if lint_payload is not None:
        warnings = raw_warnings if isinstance((raw_warnings := lint_payload.get("warnings")), list) else []
        errors = raw_errors if isinstance((raw_errors := lint_payload.get("errors")), list) else []
        injection = raw_injection if isinstance((raw_injection := lint_payload.get("injection")), dict) else {}
        score -= min(len(warnings) * 5, 20)
        score -= min(len(errors) * 20, 60)
        if injection.get("flagged"):
            score -= 20
            signals.append("injection-like text detected")
    score = max(0, min(100, score))
    return {
        "score": score,
        "trust_level": trust_level,
        "signals": signals,
        "tests_declared": len(tests),
        "changelog": changelog,
        "provenance": source,
    }


def _source_skill_dir(source: Path) -> tuple[Path | None, str | None]:
    source = source.expanduser().resolve()
    if not source.exists():
        return None, f"source not found: {source}"
    source_dir = source if source.is_dir() else source.parent
    source_skill_md = source_dir / "SKILL.md" if source.is_dir() else source
    if source_skill_md.name != "SKILL.md" or not source_skill_md.is_file():
        return None, "skill source must be a SKILL.md file or a directory containing SKILL.md"
    return source_dir, None


def _registry_import_payload(
    *,
    target: Path,
    source: Path,
    skill_id: str | None,
    force: bool,
    source_provenance: Path | str | None = None,
) -> tuple[dict[str, Any] | None, str | None, int]:
    source_dir, error = _source_skill_dir(source)
    if source_dir is None:
        return None, error, 2
    incoming_metadata = _read_json(source_dir / "skill.json")
    resolved_id = _slug(skill_id or str(incoming_metadata.get("id") or source_dir.name))
    dest = _skill_path(target, resolved_id)
    if dest.exists() and not force:
        return None, f"skill already exists: {dest}", 2
    try:
        # Collect exactly once: the anchored copy, the recorded fingerprint,
        # and the staged lint below all consume these bytes; nothing re-reads
        # the entry through its state-root pathname.
        collected_dirs, collected_files = _collect_source_tree(source_dir)
        with _held_state_root(target) as anchor:
            _write_collected_tree_into_anchor(
                collected_dirs, collected_files, anchor, "skills", "registry", resolved_id
            )
            metadata = dict(incoming_metadata)
            metadata.update(
                {
                    "id": resolved_id,
                    "version": str(metadata.get("version") or "0.1.0"),
                    "source": str(source_provenance if source_provenance is not None else source),
                    "imported_at": _now(),
                    "trust_level": str(metadata.get("trust_level") or "unreviewed"),
                    "required_tools": metadata.get("required_tools")
                    if isinstance(metadata.get("required_tools"), list)
                    else [],
                    "required_mcp_servers": metadata.get("required_mcp_servers")
                    if isinstance(metadata.get("required_mcp_servers"), list)
                    else [],
                    "supported_harnesses": metadata.get("supported_harnesses")
                    if isinstance(metadata.get("supported_harnesses"), list)
                    else list(HARNESS_TARGETS),
                    "tests": metadata.get("tests") if isinstance(metadata.get("tests"), list) else [],
                }
            )
            metadata["fingerprint"] = _files_fingerprint(collected_files)
            _write_state_file(anchor, "skills", "registry", resolved_id, "skill.json", data=_json_bytes(metadata))
    except SkillsStatePathError as exc:
        return None, str(exc), 2
    # The lint consumes the already-collected generation from private staging;
    # a swapped or symlinked state-root entry can never reach it.
    with tempfile.TemporaryDirectory(prefix="brigade-import-lint-") as staging:
        staged_dir = Path(staging) / resolved_id
        _write_snapshot_tree(collected_dirs, collected_files, staged_dir)
        lint_payload = search_validate._lint_path_payload(Path(staging), str(staged_dir), mode="lenient", contain=True)
        lint_payload = search_validate._finalize_staged_registry_lint(lint_payload, target, dest, staged_dir)
    return (
        {"target": str(target), "skill_id": resolved_id, "skill_dir": str(dest), "lint": lint_payload},
        None,
        0 if lint_payload["valid"] else 1,
    )


def _bundled_skill_path(skill_id: str) -> Path:
    return template_root() / "skills" / _slug(skill_id)


def _bundled_skill_exists(skill_id: str) -> bool:
    return _skill_md_path(_bundled_skill_path(skill_id)).is_file()


def _resolve_diff_baseline(target: Path, skill_or_path: str, *, against: str = "bundled") -> str:
    requested = str(skill_or_path)
    candidate = Path(requested).expanduser()
    # Lexical state-root classification precedes any exists() probe: an
    # existing registry pathname selects the anchored entry, and other
    # state-root locations fall through to a lint refusal.
    state_kind = _packs_mod._state_root_selector_kind(target, requested)
    if state_kind == "registry":
        parts = _packs_mod._lexical_state_root_parts(target, candidate)
        assert parts is not None
        return f"registry:{parts[2]}"
    if candidate.exists():
        return requested
    if requested.startswith("registry:"):
        skill_id = _slug(requested.removeprefix("registry:"))
    elif requested.startswith("bundled:"):
        skill_id = _slug(requested.removeprefix("bundled:"))
    else:
        skill_id = _slug(requested)
    # Registry existence is answered through the state-root anchor; a
    # symlinked entry is treated as absent rather than followed.
    try:
        with _held_state_root(target) as anchor:
            registry_exists = _read_state_file_bytes(anchor, "skills", "registry", skill_id, "SKILL.md") is not None
    except SkillsStatePathError:
        registry_exists = False
    bundled_exists = _bundled_skill_exists(skill_id)
    if against == "registry":
        if registry_exists:
            return f"registry:{skill_id}"
        if bundled_exists:
            return f"bundled:{skill_id}"
        return requested
    if bundled_exists:
        return f"bundled:{skill_id}"
    if registry_exists:
        return f"registry:{skill_id}"
    return requested


def _source_identity(*, skill_dir: Path, skill_id: str, kind: str, reviewed: bool) -> dict[str, Any]:
    if kind == "brigade-bundle":
        identity = f"{BUNDLED_SOURCE_PREFIX}{skill_id}"
    elif kind == "registry":
        identity = f"registry://skills/{skill_id}"
    else:
        identity = f"path:{skill_dir.resolve()}"
    return {
        "schema_version": SOURCE_SCHEMA_VERSION,
        "kind": kind,
        "identity": identity,
        "reviewed": reviewed,
        "brigade_version": BRIGADE_VERSION if kind == "brigade-bundle" else None,
    }


def _anchored_load_registry_metadata(target: Path, skill_id: str) -> tuple[Path, dict[str, Any]]:
    """Registry half of :func:`_load_skill`: display path plus anchored metadata."""
    slug = _slug(skill_id)
    skill_dir = _skill_path(target, slug)
    try:
        with _held_state_root(target) as anchor:
            metadata = _anchored_registry_metadata(anchor, slug)
    except SkillsStatePathError:
        metadata = {}
    return skill_dir, metadata


def _registry_entry_present(target: Path, skill_id: str) -> bool:
    """Anchored existence check for one plain registry entry directory."""
    try:
        with _held_state_root(target) as anchor:
            return _state_entry_kind(anchor, "skills", "registry", _slug(skill_id)) == "dir"
    except SkillsStatePathError:
        return False


def _load_skill(target: Path, skill_or_path: str) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    requested = str(skill_or_path)
    candidate = Path(requested).expanduser()
    kind = "path"
    reviewed = False
    if requested.startswith("registry:"):
        skill_dir, metadata = _anchored_load_registry_metadata(target, requested.removeprefix("registry:"))
        kind = "registry"
    elif requested.startswith("bundled:"):
        requested_id = _slug(requested.removeprefix("bundled:"))
        skill_dir = _bundled_skill_path(requested_id)
        kind = "brigade-bundle"
        reviewed = True
        metadata = _read_json(_metadata_path(skill_dir))
    else:
        # Classify before any exists() probe: an existing state-root pathname
        # must never be re-read through the filesystem. Registry entries stay
        # anchored; every other state-root location is refused.
        state_kind = _packs_mod._state_root_selector_kind(target, requested)
        if state_kind == "registry":
            parts = _packs_mod._lexical_state_root_parts(target, candidate)
            assert parts is not None
            skill_dir, metadata = _anchored_load_registry_metadata(target, parts[2])
            kind = "registry"
        elif state_kind == "refuse":
            raise SkillsStatePathError(f"refusing skills state path outside the registry anchoring: {candidate}")
        elif candidate.exists():
            skill_dir = candidate if candidate.is_dir() else candidate.parent
            metadata = _read_json(_metadata_path(skill_dir))
        else:
            requested_id = _slug(requested)
            bundled = _bundled_skill_path(requested_id)
            if (_skill_md_path(bundled)).is_file():
                skill_dir = bundled
                kind = "brigade-bundle"
                reviewed = True
                metadata = _read_json(_metadata_path(bundled))
            else:
                skill_dir, metadata = _anchored_load_registry_metadata(target, requested_id)
                kind = "registry"
    if not isinstance(metadata, dict):
        metadata = {}
    metadata.setdefault("id", skill_dir.name)
    skill_id = _slug(str(metadata.get("id") or skill_dir.name))
    return (
        skill_dir,
        metadata,
        _source_identity(
            skill_dir=skill_dir,
            skill_id=skill_id,
            kind=kind,
            reviewed=reviewed,
        ),
    )


def _plain_subdirs_under_anchor(anchor: _StateRootAnchor, *relative: str) -> list[str] | None:
    """List plain subdirectory names under an anchored state path.

    Returns ``None`` when the directory itself is missing; a symlinked or
    non-directory component raises instead of being followed. On descriptor
    platforms entries are enumerated from the held chain; elsewhere the
    lstat-guarded fallback walker applies (symlinks skipped).
    """
    if not _HAS_DESCRIPTOR_ANCHOR:
        base = (anchor.workspace / ".brigade").joinpath(*relative)
        current = anchor.workspace / ".brigade"
        for part in relative:
            current = current / part
            if not current.exists():
                return None
            if current.is_symlink() or _is_reparse_point(current) or not current.is_dir():
                raise SkillsStatePathError(f"skills state path must be plain directories: {current}")
        names: list[str] = []
        for entry in sorted(base.iterdir(), key=lambda item: item.name):
            if entry.is_symlink():
                continue
            try:
                st = entry.lstat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise SkillsStatePathError(f"skills state entry could not be inspected safely: {entry}") from exc
            if stat_module.S_ISDIR(st.st_mode):
                names.append(entry.name)
        return names
    fd, opened = _anchor_open_chain(anchor, *relative, missing_ok=True)
    if fd is None:
        return None
    try:
        names = []
        for name in sorted(os.listdir(fd)):
            try:
                st = os.lstat(name, dir_fd=fd)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise SkillsStatePathError(
                    f"skills state entry could not be inspected safely: {(anchor.workspace / '.brigade').joinpath(*relative, name)}"
                ) from exc
            if stat_module.S_ISDIR(st.st_mode):
                names.append(name)
        return names
    finally:
        for held_fd in reversed(opened):
            os.close(held_fd)


def _iter_registry(target: Path) -> list[dict[str, Any]]:
    """List registry entries through the held state-root anchor.

    Entries are enumerated through the anchor (symlinked entry directories are
    skipped, never followed) and each ``skill.json`` is read through
    ``_read_state_file_bytes``, so no pathname lookup touches state-root
    content. A refused anchor yields an empty listing.
    """
    rows: list[dict[str, Any]] = []
    try:
        with _held_state_root(target) as anchor:
            names = _plain_subdirs_under_anchor(anchor, "skills", "registry")
            if names is None:
                return rows
            for name in names:
                metadata = _anchored_registry_metadata(anchor, name)
                metadata.setdefault("id", name)
                metadata.setdefault("title", metadata["id"])
                rows.append({"skill_dir": str(_skill_path(target, name)), "metadata": metadata})
    except SkillsStatePathError:
        return []
    return rows


class SkillsStatePathError(RuntimeError):
    """The ``.brigade`` skills state root was redirected or swapped mid-operation."""


_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_DIR_OPEN_FLAGS = os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _CLOEXEC
# O_NONBLOCK so opening a planted FIFO cannot block the read; regular files
# ignore it.
_FILE_READ_FLAGS = os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK | _CLOEXEC
_FILE_REPLACE_FLAGS = os.O_WRONLY | _O_NOFOLLOW | _CLOEXEC
_HAS_DESCRIPTOR_ANCHOR = bool(_O_DIRECTORY) and bool(_O_NOFOLLOW) and os.open in os.supports_dir_fd


@dataclass
class _StateRootAnchor:
    """A validated hold on the workspace's ``.brigade`` state root.

    On POSIX the anchor holds an open descriptor taken with
    ``O_DIRECTORY|O_NOFOLLOW``, so writes can be performed relative to the
    held directory and a swapped-in replacement (different device/inode) is
    detected by ``revalidate()``. On platforms without those APIs the anchor
    validates via strict resolution immediately before each write; every
    destination component below the state root is additionally rejected when
    it is a symlink or reparse point and the parent is re-resolved strictly
    just before use, but a small check-then-use window remains unavoidable
    there because the final open cannot be anchored to a held descriptor.
    """

    workspace: Path
    fd: int | None = None
    identity: tuple[int, int] | None = None

    def revalidate(self) -> None:
        if not _HAS_DESCRIPTOR_ANCHOR:
            _verify_state_root_no_descriptor(self.workspace)
            return
        expected = self.identity
        fd, identity = _open_state_root_descriptor(self.workspace)
        os.close(fd)
        if identity != expected:
            raise SkillsStatePathError(
                f"skills state root swapped between validation and write: {self.workspace / '.brigade'}"
            )

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
        self.fd = None
        self.identity = None


def _open_state_root_descriptor(workspace: Path) -> tuple[int, tuple[int, int]]:
    """Validate and open ``.brigade`` relative to a held workspace descriptor.

    The workspace directory itself is opened first; ``.brigade`` is then only
    ever looked up relative to that held descriptor, so swapping an ancestor
    of the workspace while the state root is being created cannot redirect the
    new directory (or the validation that follows) outside the workspace.
    """
    try:
        ws_fd = os.open(workspace, _DIR_OPEN_FLAGS)
    except OSError as exc:
        raise SkillsStatePathError(f"cannot open workspace directory: {workspace}") from exc
    try:
        try:
            fd = os.open(".brigade", _DIR_OPEN_FLAGS, dir_fd=ws_fd)
        except FileNotFoundError:
            try:
                os.mkdir(".brigade", 0o755, dir_fd=ws_fd)
            except FileExistsError:
                pass
            except OSError as exc:
                raise SkillsStatePathError(f"cannot create skills state root: {workspace / '.brigade'}") from exc
            try:
                fd = os.open(".brigade", _DIR_OPEN_FLAGS, dir_fd=ws_fd)
            except OSError as exc:
                raise SkillsStatePathError(
                    f"cannot open skills state root without following symlinks: {workspace / '.brigade'}"
                ) from exc
            # The root was just created: make sure the workspace path still
            # reaches the directory we hold. An ancestor swapped during the
            # creation window would otherwise leave both the created root and
            # its validation pointing outside the real workspace.
            try:
                via_path = os.stat(workspace)
            except OSError as exc:
                raise SkillsStatePathError(f"workspace changed while creating skills state root: {workspace}") from exc
            held = os.fstat(ws_fd)
            if (via_path.st_dev, via_path.st_ino) != (held.st_dev, held.st_ino):
                raise SkillsStatePathError(f"workspace changed while creating skills state root: {workspace}") from None
        except OSError as exc:
            raise SkillsStatePathError(
                f"skills state root must be a plain directory inside the workspace: {workspace / '.brigade'}"
            ) from exc
        st = os.fstat(fd)
        return fd, (st.st_dev, st.st_ino)
    finally:
        os.close(ws_fd)


def _verify_state_root_no_descriptor(workspace: Path) -> None:
    state_root = workspace / ".brigade"
    if not state_root.exists():
        try:
            state_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise SkillsStatePathError(f"cannot create skills state root: {state_root}") from exc
    if not state_root.is_dir() or state_root.is_symlink():
        raise SkillsStatePathError(f"skills state root is not a plain directory: {state_root}")
    probe = state_root.resolve(strict=True)
    if probe != Path(os.path.realpath(workspace)) / ".brigade":
        raise SkillsStatePathError(f"skills state root resolves outside the workspace: {state_root} -> {probe}")


def _hold_state_root(workspace: Path) -> _StateRootAnchor:
    """Validate the state root and take a held descriptor anchor on POSIX."""
    if _HAS_DESCRIPTOR_ANCHOR:
        fd, identity = _open_state_root_descriptor(workspace)
        return _StateRootAnchor(workspace=workspace, fd=fd, identity=identity)
    _verify_state_root_no_descriptor(workspace)
    return _StateRootAnchor(workspace=workspace)


@contextlib.contextmanager
def _held_state_root(workspace: Path) -> Iterator[_StateRootAnchor]:
    anchor = _hold_state_root(workspace)
    try:
        yield anchor
    finally:
        anchor.close()


@contextlib.contextmanager
def _anchored_fs_errors(display: str) -> Iterator[None]:
    """Translate unexpected filesystem errors inside anchor primitives.

    Callers only catch :class:`SkillsStatePathError`; a race that loses an
    open, mkdir, or write mid-primitive must surface as the typed refusal
    (original exception preserved as ``__cause__``), never as a raw
    ``OSError`` traceback.
    """
    try:
        yield
    except SkillsStatePathError:
        raise
    except OSError as exc:
        raise SkillsStatePathError(f"skills state operation could not complete safely: {display}: {exc}") from exc


def _require_single_link_regular(fd: int, display: str) -> None:
    """Validate an opened descriptor: plain regular file with exactly one link.

    A second link means the state file is a hardlink sharing an inode with an
    outside victim; truncating or appending would damage that victim. FIFOs
    and other non-regular files are refused as well.
    """
    st = os.fstat(fd)
    if not stat_module.S_ISREG(st.st_mode) or st.st_nlink != 1:
        raise SkillsStatePathError(f"skills state file must be a plain single-link regular file: {display}")


def _write_all_fd(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]


def _fallback_mutation_refusal(verb: str, display: Path) -> SkillsStatePathError:
    return SkillsStatePathError(
        f"skills state-root {verb} requires descriptor anchoring, which is unavailable on this platform; "
        f"refusing unanchored mutation: {display}"
    )


def _write_state_file(anchor: _StateRootAnchor, *relative: str, data: bytes, mode: int = 0o644) -> None:
    """Create or replace one file under the anchored state root."""
    anchor.revalidate()
    display = (anchor.workspace / ".brigade").joinpath(*relative)
    if not _HAS_DESCRIPTOR_ANCHOR:
        # Fail closed: without a held directory descriptor the final open
        # cannot be anchored to the validated root, so the check-then-use
        # window would be exploitable. Mutate nothing.
        raise _fallback_mutation_refusal("writes", display)
    assert anchor.fd is not None
    parent_fd = anchor.fd
    opened_chain: list[int] = []
    try:
        with _anchored_fs_errors(str(display)):
            for part in relative[:-1]:
                try:
                    next_fd = os.open(part, _DIR_OPEN_FLAGS, dir_fd=parent_fd)
                except FileNotFoundError:
                    os.mkdir(part, 0o755, dir_fd=parent_fd)
                    next_fd = os.open(part, _DIR_OPEN_FLAGS, dir_fd=parent_fd)
                opened_chain.append(next_fd)
                parent_fd = next_fd
            try:
                # Try O_EXCL first: a file created here cannot alias
                # anything else.
                file_fd = os.open(
                    relative[-1],
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _CLOEXEC,
                    mode,
                    dir_fd=parent_fd,
                )
            except FileExistsError:
                # Replace an existing file without O_TRUNC: open it, prove
                # the descriptor is a single-link regular file, and only
                # then truncate. A hardlink to an outside victim is
                # refused before the victim loses a byte. O_NONBLOCK keeps
                # a planted FIFO from blocking the open itself; it has no
                # effect once fstat proves the descriptor is a regular file.
                file_fd = os.open(
                    relative[-1],
                    _FILE_REPLACE_FLAGS | _O_NONBLOCK,
                    dir_fd=parent_fd,
                )
                _require_single_link_regular(file_fd, str(display))
            try:
                os.ftruncate(file_fd, 0)
                _write_all_fd(file_fd, data)
            finally:
                os.close(file_fd)
    finally:
        for fd in reversed(opened_chain):
            os.close(fd)


def _append_state_line(anchor: _StateRootAnchor, *relative: str, data: bytes) -> None:
    """Append one line to a JSONL file under the anchored state root."""
    anchor.revalidate()
    display = (anchor.workspace / ".brigade").joinpath(*relative)
    if not _HAS_DESCRIPTOR_ANCHOR:
        raise _fallback_mutation_refusal("appends", display)
    assert anchor.fd is not None
    parent_fd = anchor.fd
    opened_chain: list[int] = []
    try:
        with _anchored_fs_errors(str(display)):
            for part in relative[:-1]:
                try:
                    next_fd = os.open(part, _DIR_OPEN_FLAGS, dir_fd=parent_fd)
                except FileNotFoundError:
                    os.mkdir(part, 0o755, dir_fd=parent_fd)
                    next_fd = os.open(part, _DIR_OPEN_FLAGS, dir_fd=parent_fd)
                opened_chain.append(next_fd)
                parent_fd = next_fd
            # O_NONBLOCK keeps a planted FIFO from blocking the open itself;
            # it has no effect once fstat proves the descriptor is a regular
            # file, so appends to real state files behave exactly as before.
            flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | _O_NOFOLLOW | _O_NONBLOCK | _CLOEXEC
            file_fd = os.open(relative[-1], flags, 0o644, dir_fd=parent_fd)
            try:
                # Validate before the first byte moves: an append to a
                # hardlinked state file would also append to the victim.
                _require_single_link_regular(file_fd, str(display))
                _write_all_fd(file_fd, data)
            finally:
                os.close(file_fd)
    finally:
        for fd in reversed(opened_chain):
            os.close(fd)


def _is_reparse_point(path: Path) -> bool:
    """True when *path* is a Windows reparse point (junction or mount point)."""
    if hasattr(os, "isjunction") and os.path.isjunction(path):
        return True
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return bool(getattr(st, "st_reparse_tag", False))


def _fallback_prepare_parent(anchor: _StateRootAnchor, *relative_dirs: str, create: bool = True) -> Path | None:
    """No-descriptor platforms: validate the destination chain, then resolve the parent.

    Rejects symlinks and reparse points across every destination component and
    strictly re-resolves the parent immediately before use. With ``create``
    the missing tail is created (and re-checked); without it a missing tail
    yields ``None`` so read-only callers can treat the path as absent. A
    residual check-then-use window remains on these platforms because the
    final open cannot be anchored to a held directory descriptor; see
    ``_StateRootAnchor``.
    """
    base = anchor.workspace / ".brigade"
    chain = [base.joinpath(*relative_dirs[: index + 1]) for index in range(len(relative_dirs))]
    for candidate in chain:
        if candidate.is_symlink() or _is_reparse_point(candidate):
            raise SkillsStatePathError(f"skills state path must not contain symlinks or reparse points: {candidate}")
        if candidate.exists() and not candidate.is_dir():
            raise SkillsStatePathError(f"skills state path must be plain directories: {candidate}")
    parent = chain[-1] if chain else base
    if not parent.exists():
        if not create:
            return None
        parent.mkdir(parents=True, exist_ok=True)
    for candidate in chain:
        if candidate.is_symlink() or _is_reparse_point(candidate):
            raise SkillsStatePathError(
                f"skills state path gained a symlink or reparse point during creation: {candidate}"
            )
    resolved = Path(os.path.realpath(parent, strict=True))
    expected = (Path(os.path.realpath(anchor.workspace)) / ".brigade").joinpath(*relative_dirs)
    if resolved != expected:
        raise SkillsStatePathError(f"skills state path resolves outside the workspace: {parent} -> {resolved}")
    return parent


def _require_plain_state_dirs(anchor: _StateRootAnchor, *relative: str) -> None:
    """Refuse symlinked or non-directory components along an existing state path.

    Missing tail components are allowed (later steps create them); anything
    that exists must be a plain directory reachable without following links.
    """
    if not _HAS_DESCRIPTOR_ANCHOR:
        base = anchor.workspace / ".brigade"
        current = base
        for part in relative:
            current = current / part
            if current.is_symlink() or _is_reparse_point(current):
                raise SkillsStatePathError(f"skills state path must not contain symlinks or reparse points: {current}")
            if current.exists() and not current.is_dir():
                raise SkillsStatePathError(f"skills state path must be plain directories: {current}")
        return
    assert anchor.fd is not None
    parent_fd = anchor.fd
    opened: list[int] = []
    current = anchor.workspace / ".brigade"
    try:
        for part in relative:
            current = current / part
            try:
                fd = os.open(part, _DIR_OPEN_FLAGS, dir_fd=parent_fd)
            except FileNotFoundError:
                break
            except OSError as exc:
                raise SkillsStatePathError(f"skills state path must be plain directories: {current}") from exc
            opened.append(fd)
            parent_fd = fd
    finally:
        for fd in reversed(opened):
            os.close(fd)


def _anchor_open_chain(
    anchor: _StateRootAnchor, *relative: str, create: bool = False, missing_ok: bool = False
) -> tuple[int | None, list[int]]:
    """Open every component below the held state root relative to its parent.

    Returns ``(fd_of_last_component, fds_to_close)``. Each component is opened
    with ``O_DIRECTORY|O_NOFOLLOW`` against the previous descriptor, so no
    lookup can be redirected by a nested symlink; link components raise
    ``SkillsStatePathError`` instead of being followed. With ``missing_ok`` a
    missing tail yields ``(None, opened)`` so absence can be handled without
    following any path.
    """
    assert anchor.fd is not None
    opened: list[int] = []
    current = anchor.workspace / ".brigade"
    try:
        parent_fd = anchor.fd
        for part in relative:
            current = current / part
            try:
                fd = os.open(part, _DIR_OPEN_FLAGS, dir_fd=parent_fd)
            except FileNotFoundError:
                if create:
                    try:
                        os.mkdir(part, 0o755, dir_fd=parent_fd)
                    except OSError as exc:
                        raise SkillsStatePathError(f"skills state path could not be created safely: {current}") from exc
                    try:
                        fd = os.open(part, _DIR_OPEN_FLAGS, dir_fd=parent_fd)
                    except OSError as exc:
                        raise SkillsStatePathError(
                            f"skills state path could not be reopened safely: {current}"
                        ) from exc
                elif missing_ok:
                    return None, opened
                else:
                    raise SkillsStatePathError(
                        f"skills state path is missing under the state root: {current}"
                    ) from None
            except OSError as exc:
                raise SkillsStatePathError(
                    f"skills state path must be plain directories inside the state root: {current}"
                ) from exc
            opened.append(fd)
            parent_fd = fd
    except BaseException:
        for fd in reversed(opened):
            os.close(fd)
        raise
    return opened[-1], opened


def _state_file_exists(anchor: _StateRootAnchor, *relative: str) -> bool:
    """Existence check for one regular file under the anchored state root."""
    if not _HAS_DESCRIPTOR_ANCHOR:
        parent = _fallback_prepare_parent(anchor, *relative[:-1], create=False)
        if parent is None:
            return False
        target = (anchor.workspace / ".brigade").joinpath(*relative)
        return target.exists() and not target.is_symlink() and not _is_reparse_point(target) and target.is_file()
    parent_fd, opened = _anchor_open_chain(anchor, *relative[:-1], missing_ok=True)
    if parent_fd is None:
        return False
    try:
        try:
            st = os.stat(relative[-1], dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise SkillsStatePathError(f"skills state path is not usable: {anchor.workspace / '.brigade'}") from exc
        if stat_module.S_ISLNK(st.st_mode):
            raise SkillsStatePathError(
                f"skills state file must not be a symlink: {(anchor.workspace / '.brigade').joinpath(*relative)}"
            )
        return stat_module.S_ISREG(st.st_mode)
    finally:
        for fd in reversed(opened):
            os.close(fd)


def _rm_children_recursive(dir_fd: int) -> None:
    """Delete every child of an open directory without following symlinks."""
    for name in os.listdir(dir_fd):
        st = os.lstat(name, dir_fd=dir_fd)
        if stat_module.S_ISDIR(st.st_mode):
            child_fd = os.open(name, _DIR_OPEN_FLAGS, dir_fd=dir_fd)
            try:
                _rm_children_recursive(child_fd)
            finally:
                os.close(child_fd)
            os.rmdir(name, dir_fd=dir_fd)
        else:
            os.unlink(name, dir_fd=dir_fd)


def _remove_tree_in_anchor(anchor: _StateRootAnchor, *relative: str) -> None:
    """Recursively delete a subtree under the anchored state root, ``dir_fd`` throughout."""
    display = (anchor.workspace / ".brigade").joinpath(*relative)
    if not _HAS_DESCRIPTOR_ANCHOR:
        raise _fallback_mutation_refusal("removals", display)
    parent_fd, opened = _anchor_open_chain(anchor, *relative[:-1], missing_ok=True)
    if parent_fd is None:
        return
    try:
        with _anchored_fs_errors(str(display)):
            try:
                st = os.stat(relative[-1], dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                return
            except OSError as exc:
                raise SkillsStatePathError(f"skills state path is not removable safely: {display}") from exc
            if stat_module.S_ISDIR(st.st_mode):
                child_fd = os.open(relative[-1], _DIR_OPEN_FLAGS, dir_fd=parent_fd)
                try:
                    _rm_children_recursive(child_fd)
                finally:
                    os.close(child_fd)
                os.rmdir(relative[-1], dir_fd=parent_fd)
            else:
                os.unlink(relative[-1], dir_fd=parent_fd)
    finally:
        for fd in reversed(opened):
            os.close(fd)


def _unlink_in_anchor(anchor: _StateRootAnchor, *relative: str) -> None:
    """Unlink one file under the anchored state root without following symlinks."""
    display = (anchor.workspace / ".brigade").joinpath(*relative)
    if not _HAS_DESCRIPTOR_ANCHOR:
        raise _fallback_mutation_refusal("unlinks", display)
    parent_fd, opened = _anchor_open_chain(anchor, *relative[:-1])
    try:
        try:
            os.unlink(relative[-1], dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise SkillsStatePathError(f"skills state file could not be removed safely: {display}") from exc
    finally:
        for fd in reversed(opened):
            os.close(fd)


def _read_regular_file_via_fd(name: str, dir_fd: int, display: Path, label: str) -> bytes:
    """Read one regular file through an ``O_NOFOLLOW`` descriptor below *dir_fd*."""
    try:
        fd = os.open(name, _FILE_READ_FLAGS, dir_fd=dir_fd)
    except OSError as exc:
        raise SkillsStatePathError(f"{label} file changed while being read: {display}") from exc
    try:
        st = os.fstat(fd)
        if not stat_module.S_ISREG(st.st_mode) or st.st_nlink > 1:
            raise SkillsStatePathError(f"{label} file must be a plain single-link file: {display}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        os.close(fd)
    return b"".join(chunks)


def _collect_fd_tree(
    dir_fd: int, prefix: tuple[str, ...], display: Path, label: str
) -> tuple[list[tuple[str, ...]], dict[tuple[str, ...], bytes]]:
    """Walk an open directory descriptor, skipping symlinks and refusing hardlinks.

    Every entry gets exactly one ``lstat`` against the held directory
    descriptor; directories are re-opened with ``O_DIRECTORY|O_NOFOLLOW`` and
    files are read through ``O_NOFOLLOW`` descriptors validated by ``fstat``,
    so an entry swapped to a symlink between check and use cannot redirect a
    read outside the walked tree.
    """
    dirs: list[tuple[str, ...]] = []
    files: dict[tuple[str, ...], bytes] = {}
    try:
        names = sorted(os.listdir(dir_fd))
    except OSError as exc:
        raise SkillsStatePathError(f"{label} directory could not be listed safely: {display}") from exc
    for name in names:
        entry_display = display / name
        try:
            st = os.lstat(name, dir_fd=dir_fd)
        except FileNotFoundError:
            raise SkillsStatePathError(f"{label} entry changed while being read: {entry_display}") from None
        except OSError as exc:
            raise SkillsStatePathError(f"{label} entry could not be inspected safely: {entry_display}") from exc
        mode = st.st_mode
        if stat_module.S_ISLNK(mode):
            continue
        if stat_module.S_ISDIR(mode):
            parts = (*prefix, name)
            dirs.append(parts)
            try:
                child_fd = os.open(name, _DIR_OPEN_FLAGS, dir_fd=dir_fd)
            except OSError as exc:
                raise SkillsStatePathError(f"{label} directory changed while being walked: {entry_display}") from exc
            try:
                sub_dirs, sub_files = _collect_fd_tree(child_fd, parts, entry_display, label)
            finally:
                os.close(child_fd)
            dirs.extend(sub_dirs)
            files.update(sub_files)
        elif stat_module.S_ISREG(mode):
            if st.st_nlink > 1:
                raise SkillsStatePathError(f"{label} file must not be a hardlink: {entry_display}")
            files[(*prefix, name)] = _read_regular_file_via_fd(name, dir_fd, entry_display, label)
        # every other entry kind is skipped, matching the previous is_file() filter
    return dirs, files


def _collect_source_tree(source_dir: Path) -> tuple[list[tuple[str, ...]], dict[tuple[str, ...], bytes]]:
    """Collect plain subdirectories and file contents from *source_dir*, skipping symlinks.

    The tree is walked through held directory descriptors (see
    ``_collect_fd_tree``), so separate check-then-use lookups cannot race;
    regular files carrying more than one link (hardlinks) are refused.
    """
    if not _HAS_DESCRIPTOR_ANCHOR:
        dirs: list[tuple[str, ...]] = []
        files: dict[tuple[str, ...], bytes] = {}

        def walk(current: Path, prefix: tuple[str, ...]) -> None:
            for entry in sorted(current.iterdir(), key=lambda item: item.name):
                if entry.is_symlink():
                    continue
                st = entry.lstat()
                if stat_module.S_ISDIR(st.st_mode):
                    parts = (*prefix, entry.name)
                    dirs.append(parts)
                    walk(entry, parts)
                elif stat_module.S_ISREG(st.st_mode):
                    if st.st_nlink > 1:
                        raise SkillsStatePathError(f"skills source file must not be a hardlink: {entry}")
                    files[(*prefix, entry.name)] = entry.read_bytes()

        walk(source_dir, ())
        return dirs, files
    try:
        root_fd = os.open(source_dir, _DIR_OPEN_FLAGS)
    except OSError as exc:
        raise SkillsStatePathError(f"skills source must be a plain readable directory: {source_dir}") from exc
    try:
        return _collect_fd_tree(root_fd, (), source_dir, "skills source")
    finally:
        os.close(root_fd)


def _read_state_file_bytes(anchor: _StateRootAnchor, *relative: str) -> bytes | None:
    """Read one regular single-link file under the anchored state root.

    Returns ``None`` when the file is missing; a symlinked tail, a FIFO or
    other non-regular file, and hardlinks are refused instead of followed.
    """
    display = (anchor.workspace / ".brigade").joinpath(*relative)
    if not _HAS_DESCRIPTOR_ANCHOR:
        parent = _fallback_prepare_parent(anchor, *relative[:-1], create=False)
        if parent is None:
            return None
        target = parent / relative[-1]
        try:
            st = os.lstat(target)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise SkillsStatePathError(f"skills state file could not be inspected safely: {display}") from exc
        if stat_module.S_ISLNK(st.st_mode) or getattr(st, "st_reparse_tag", False):
            raise SkillsStatePathError(f"skills state file must not be a symlink or reparse point: {display}")
        if not stat_module.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise SkillsStatePathError(f"skills state file must be a plain single-link regular file: {display}")
        return target.read_bytes()
    parent_fd, opened = _anchor_open_chain(anchor, *relative[:-1], missing_ok=True)
    if parent_fd is None:
        return None
    try:
        with _anchored_fs_errors(str(display)):
            try:
                file_fd = os.open(relative[-1], _FILE_READ_FLAGS, dir_fd=parent_fd)
            except FileNotFoundError:
                return None
            except OSError as exc:
                raise SkillsStatePathError(f"skills state file could not be read safely: {display}") from exc
            try:
                # O_NOFOLLOW refuses the symlink at open; fstat closes the
                # remaining gaps: FIFOs (opened via O_NONBLOCK), other
                # non-regular files, and hardlinks to outside victims.
                _require_single_link_regular(file_fd, str(display))
                chunks: list[bytes] = []
                while True:
                    chunk = os.read(file_fd, 1 << 16)
                    if not chunk:
                        break
                    chunks.append(chunk)
            finally:
                os.close(file_fd)
            return b"".join(chunks)
    finally:
        for fd in reversed(opened):
            os.close(fd)


def _state_entry_kind(anchor: _StateRootAnchor, *relative: str) -> str:
    """Classify one path under the anchored state root as ``missing``, ``dir``, or ``other``.

    A symlinked component anywhere along the path is refused instead of followed.
    """
    if not _HAS_DESCRIPTOR_ANCHOR:
        parent = _fallback_prepare_parent(anchor, *relative[:-1], create=False)
        if parent is None:
            return "missing"
        target = parent / relative[-1]
        if target.is_symlink() or _is_reparse_point(target):
            raise SkillsStatePathError(
                f"skills state path must not contain symlinks or reparse points: {(anchor.workspace / '.brigade').joinpath(*relative)}"
            )
        if not target.exists():
            return "missing"
        return "dir" if target.is_dir() else "other"
    parent_fd, opened = _anchor_open_chain(anchor, *relative[:-1], missing_ok=True)
    if parent_fd is None:
        return "missing"
    try:
        try:
            st = os.stat(relative[-1], dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return "missing"
        except OSError as exc:
            raise SkillsStatePathError(
                f"skills state path is not usable: {(anchor.workspace / '.brigade').joinpath(*relative)}"
            ) from exc
        if stat_module.S_ISLNK(st.st_mode):
            raise SkillsStatePathError(
                f"skills state path must not be a symlink: {(anchor.workspace / '.brigade').joinpath(*relative)}"
            )
        return "dir" if stat_module.S_ISDIR(st.st_mode) else "other"
    finally:
        for fd in reversed(opened):
            os.close(fd)


def _read_tree_from_anchor(
    anchor: _StateRootAnchor, *relative: str
) -> tuple[list[tuple[str, ...]], dict[tuple[str, ...], bytes]]:
    """Read a plain directory subtree out of the anchored state root via descriptors.

    Without descriptor anchoring the subtree is collected through the
    lstat-guarded fallback walker (symlinks skipped, hardlinks refused);
    callers still fail closed on any anchored mutation they attempt with the
    result.
    """
    if not _HAS_DESCRIPTOR_ANCHOR:
        return _collect_source_tree((anchor.workspace / ".brigade").joinpath(*relative))
    dir_fd, opened = _anchor_open_chain(anchor, *relative)
    assert dir_fd is not None
    try:
        return _collect_fd_tree(dir_fd, (), (anchor.workspace / ".brigade").joinpath(*relative), "skills state")
    finally:
        for fd in reversed(opened):
            os.close(fd)


def _write_collected_tree_into_anchor(
    dirs: list[tuple[str, ...]], files: dict[tuple[str, ...], bytes], anchor: _StateRootAnchor, *relative: str
) -> None:
    """Write a previously collected directory tree into the anchored state root.

    Destination components are created and opened with ``O_NOFOLLOW`` relative
    to the held descriptor, so a planted symlink anywhere below the state root
    refuses the write instead of redirecting it.     An existing destination is
    removed first, matching the previous copytree-over behaviour.
    """
    display = (anchor.workspace / ".brigade").joinpath(*relative)
    if not _HAS_DESCRIPTOR_ANCHOR:
        raise _fallback_mutation_refusal("copies", display)
    _remove_tree_in_anchor(anchor, *relative)
    dest_parent_fd, opened = _anchor_open_chain(anchor, *relative[:-1], create=True)
    try:
        with _anchored_fs_errors(str(display)):
            try:
                os.mkdir(relative[-1], 0o755, dir_fd=dest_parent_fd)
                dest_fd = os.open(relative[-1], _DIR_OPEN_FLAGS, dir_fd=dest_parent_fd)
            except OSError as exc:
                raise SkillsStatePathError(f"skills state destination could not be created safely: {display}") from exc
            try:
                for parts in dirs:
                    parent_fd = dest_fd
                    chain: list[int] = []
                    try:
                        for part in parts:
                            os.mkdir(part, 0o755, dir_fd=parent_fd)
                            next_fd = os.open(part, _DIR_OPEN_FLAGS, dir_fd=parent_fd)
                            chain.append(next_fd)
                            parent_fd = next_fd
                    finally:
                        for fd in reversed(chain):
                            os.close(fd)
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _CLOEXEC
                for parts, data in files.items():
                    parent_fd = dest_fd
                    chain = []
                    try:
                        for part in parts[:-1]:
                            next_fd = os.open(part, _DIR_OPEN_FLAGS, dir_fd=parent_fd)
                            chain.append(next_fd)
                            parent_fd = next_fd
                        file_fd = os.open(parts[-1], flags, 0o644, dir_fd=parent_fd)
                        try:
                            _write_all_fd(file_fd, data)
                        finally:
                            os.close(file_fd)
                    finally:
                        for fd in reversed(chain):
                            os.close(fd)
            finally:
                os.close(dest_fd)
    finally:
        for fd in reversed(opened):
            os.close(fd)


def _read_registry_entry_tree(
    anchor: _StateRootAnchor, skill_id: str
) -> tuple[list[tuple[str, ...]], dict[tuple[str, ...], bytes]]:
    """Snapshot one registry entry through the held state-root descriptor.

    A symlinked entry directory refuses here instead of being followed;
    symlinked files inside the entry are skipped by the collector, so the
    returned bytes are always plain state-root content reached through
    descriptors.
    """
    return _read_tree_from_anchor(anchor, "skills", "registry", skill_id)


def _anchored_registry_metadata(anchor: _StateRootAnchor, skill_id: str) -> dict[str, Any]:
    """Read one registry ``skill.json`` through the anchor (missing/refused -> empty)."""
    raw = _read_state_file_bytes(anchor, "skills", "registry", skill_id, "skill.json")
    if raw is None:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _state_json_via_anchor(target: Path, *relative: str) -> dict[str, Any]:
    """Read one JSON file under the state root through the held anchor.

    Missing or refused files yield ``{}``; outside content is never followed.
    """
    try:
        with _held_state_root(target) as anchor:
            raw = _read_state_file_bytes(anchor, *relative)
    except SkillsStatePathError:
        return {}
    if raw is None:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _anchored_registry_text(target: Path, skill_id: str, *relative_tail: str) -> str | None:
    """Read one text file from a registry entry through the state-root anchor."""
    try:
        with _held_state_root(target) as anchor:
            raw = _read_state_file_bytes(anchor, "skills", "registry", skill_id, *relative_tail)
    except SkillsStatePathError:
        return None
    if raw is None:
        return None
    return raw.decode("utf-8", errors="replace")


def _anchored_entry_changelog_text(
    anchor: _StateRootAnchor, *entry_relative: str, metadata: dict[str, Any]
) -> str | None:
    """Read an entry's changelog through the anchor, honouring only contained paths.

    A configured ``changelog_path`` is honoured when it is a relative path
    staying inside the entry; absolute or escaping locations are ignored so a
    state-root entry can never cause a read of outside content.
    """
    configured = metadata.get("changelog_path") or metadata.get("changelog")
    candidates: list[tuple[str, ...]] = [("CHANGELOG.md",)]
    if isinstance(configured, str) and configured.strip():
        configured_path = Path(configured.strip())
        if not configured_path.is_absolute() and ".." not in configured_path.parts:
            candidates.insert(0, tuple(configured_path.parts))
    for parts in candidates:
        raw = _read_state_file_bytes(anchor, *entry_relative, *parts)
        if raw is not None:
            return raw.decode("utf-8", errors="replace")
    return None


def _selects_registry_skill(target: Path, requested: str) -> bool:
    """Mirror :func:`_load_skill` resolution: does this selector land on the registry?

    The state-root classification is lexical and precedes any ``exists()``
    probe, so an explicit registry pathname selects the anchored entry rather
    than being re-read by pathname; non-registry state-root locations are
    refused by :func:`_lint_payload` before any read.
    """
    if requested.startswith("registry:"):
        return True
    if requested.startswith("bundled:"):
        return False
    if _packs_mod._state_root_selector_kind(target, requested) == "registry":
        return True
    candidate = Path(requested).expanduser()
    if candidate.exists():
        return False
    bundled = _bundled_skill_path(_slug(requested))
    return not _skill_md_path(bundled).is_file()


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
