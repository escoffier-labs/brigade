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

from . import __version__ as BRIGADE_VERSION
from . import mcp_server
from .localio import slugify, utc_now_iso as _now
from .projection import kernel as projection
from .templates import template_root
from .untrusted import scan_untrusted

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
        lint_payload = _lint_path_payload(Path(staging), str(staged_dir), mode="lenient", contain=True)
        lint_payload = _finalize_staged_registry_lint(lint_payload, target, dest, staged_dir)
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
    state_kind = _state_root_selector_kind(target, requested)
    if state_kind == "registry":
        parts = _lexical_state_root_parts(target, candidate)
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
        state_kind = _state_root_selector_kind(target, requested)
        if state_kind == "registry":
            parts = _lexical_state_root_parts(target, candidate)
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


def _unreadable_source_lint_payload(target: Path, display_dir: Path, message: str) -> dict[str, Any]:
    """Lint payload shape used when the source could not be read safely."""
    return {
        "target": str(target),
        "skill_dir": str(display_dir),
        "skill_id": display_dir.name,
        "valid": False,
        "errors": [message, f"SKILL.md not found: {display_dir / 'SKILL.md'}"],
        "warnings": [],
        "harness": None,
        "render_errors": [],
        "injection": {"flagged": False, "count": 0, "markers": []},
        "metadata": {},
        "source": _source_identity(
            skill_dir=display_dir,
            skill_id=_slug(display_dir.name),
            kind="registry",
            reviewed=False,
        ),
        "agent_skills": {
            "mode": "lenient",
            "fields": {},
            "diagnostics": [],
            "allowed_tools_are_permissions": False,
        },
        "fingerprint": None,
        "changelog": {"present": False, "path": None, "fingerprint": None, "headings": []},
        "trust_score": {
            "score": 0,
            "trust_level": "unreviewed",
            "signals": ["skill directory unreadable"],
            "tests_declared": 0,
            "changelog": {"present": False, "path": None, "fingerprint": None, "headings": []},
        },
    }


def _finalize_staged_registry_lint(
    payload: dict[str, Any], target: Path, display_dir: Path, staged_dir: Path
) -> dict[str, Any]:
    """Repoint a lint computed over a private staging copy at the real entry.

    Provenance describes the real registry entry while fingerprints stay
    computed over the staged snapshot bytes; no message or payload field keeps
    a reference into the trusted-but-ephemeral staging directory.
    """

    def _repoint(value: str) -> str:
        return value.replace(str(staged_dir), str(display_dir))

    for key in ("errors", "warnings", "render_errors"):
        payload[key] = [_repoint(str(item)) for item in payload.get(key, [])]
    for changelog_key in ("changelog",):
        changelog = payload.get(changelog_key)
        if isinstance(changelog, dict) and isinstance(changelog.get("path"), str):
            changelog["path"] = _repoint(changelog["path"])
    trust_score = payload.get("trust_score")
    if isinstance(trust_score, dict) and isinstance(trust_score.get("changelog"), dict):
        nested = trust_score["changelog"]
        if isinstance(nested.get("path"), str):
            nested["path"] = _repoint(nested["path"])
    metadata = raw_metadata if isinstance((raw_metadata := payload.get("metadata")), dict) else {}
    resolved_id = _slug(str(metadata.get("id") or display_dir.name))
    staged_source = raw_staged_source if isinstance((raw_staged_source := payload.get("source")), dict) else {}
    source = _source_identity(skill_dir=display_dir, skill_id=resolved_id, kind="registry", reviewed=False)
    source["skill_version"] = staged_source.get("skill_version")
    source["fingerprint"] = staged_source.get("fingerprint")
    source["metadata_fingerprint"] = staged_source.get("metadata_fingerprint")
    payload["source"] = source
    payload["fingerprint"] = source["fingerprint"]
    if isinstance(trust_score, dict):
        trust_score["provenance"] = source
    payload["target"] = str(target)
    payload["skill_dir"] = str(display_dir)
    return payload


def _repoint_staged_lint_paths(payload: dict[str, Any], display_dir: Path, staged_dir: Path) -> dict[str, Any]:
    """Repoint staged-copy lint references at the real display directory.

    Unlike :func:`_finalize_staged_registry_lint` the payload's own source
    identity is kept (path-kind staging) and only string references move, so
    no temporary staging directory survives into errors, output, or persisted
    state while fingerprints stay snapshot-derived.
    """

    def _repoint(value: str) -> str:
        return value.replace(str(staged_dir), str(display_dir))

    for key in ("errors", "warnings", "render_errors"):
        payload[key] = [_repoint(str(item)) for item in payload.get(key, [])]
    for container_key in ("changelog",):
        container = payload.get(container_key)
        if isinstance(container, dict) and isinstance(container.get("path"), str):
            container["path"] = _repoint(container["path"])
    trust_score = payload.get("trust_score")
    if isinstance(trust_score, dict) and isinstance(trust_score.get("changelog"), dict):
        nested = trust_score["changelog"]
        if isinstance(nested.get("path"), str):
            nested["path"] = _repoint(nested["path"])
    source = payload.get("source")
    if isinstance(source, dict) and isinstance(source.get("identity"), str):
        source["identity"] = _repoint(source["identity"])
    agent_skills = payload.get("agent_skills")
    if isinstance(agent_skills, dict) and isinstance(agent_skills.get("diagnostics"), list):
        agent_skills["diagnostics"] = [
            _repoint(item) if isinstance(item, str) else item for item in agent_skills["diagnostics"]
        ]
    payload["skill_dir"] = str(display_dir)
    return payload


def _lint_registry_payload(
    target: Path,
    skill_id: str,
    harness: str | None = None,
    *,
    mode: str = "lenient",
) -> dict[str, Any]:
    """Lint a registry skill from one anchored snapshot staged into private temp space.

    The entry tree is collected through the held state-root descriptor exactly
    once; every lint read (SKILL.md, metadata, changelog, format validation)
    consumes only those collected bytes from a trusted staging directory. A
    refused or symlinked entry yields an invalid payload whose messages name
    display paths only — outside file content is never echoed.
    """
    display_dir = _skill_path(target, skill_id)
    try:
        with _held_state_root(target) as anchor:
            snapshot_dirs, snapshot_files = _read_registry_entry_tree(anchor, skill_id)
            if ("skill.json",) not in snapshot_files:
                # A missing, symlinked, or otherwise unreadable metadata file
                # refuses the entry instead of linting an anonymous default.
                return _unreadable_source_lint_payload(
                    target,
                    display_dir,
                    "registry entry must contain a plain single-link skill.json"
                    f" (symlinks are never followed): {display_dir / 'skill.json'}",
                )
            with tempfile.TemporaryDirectory(prefix="brigade-registry-lint-") as staging:
                staged_root = Path(staging)
                staged_dir = staged_root / skill_id
                _write_snapshot_tree(snapshot_dirs, snapshot_files, staged_dir)
                payload = _lint_path_payload(staged_root, str(staged_dir), harness=harness, mode=mode, contain=True)
                _finalize_staged_registry_lint(payload, target, display_dir, staged_dir)
    except SkillsStatePathError as exc:
        return _unreadable_source_lint_payload(target, display_dir, str(exc))
    return payload


def _lint_payload(
    target: Path, skill_or_path: str, harness: str | None = None, *, mode: str = "lenient"
) -> dict[str, Any]:
    requested = str(skill_or_path)
    if requested.startswith("registry:"):
        selector_id = requested.removeprefix("registry:")
        return _lint_registry_payload(target, _slug(selector_id), harness=harness, mode=mode)
    kind = _state_root_selector_kind(target, requested)
    if kind == "registry":
        parts = _lexical_state_root_parts(target, Path(requested))
        assert parts is not None
        return _lint_registry_payload(target, parts[2], harness=harness, mode=mode)
    if kind == "refuse":
        # A location inside the attacker-influenced state root that is not a
        # registry entry is never read by pathname: refuse without echoing
        # anything the planted location points at.
        display = Path(requested).expanduser()
        return _unreadable_source_lint_payload(
            target,
            display if display.name else display.parent,
            f"refusing skills state path outside the registry anchoring: {display}",
        )
    return _lint_path_payload(target, skill_or_path, harness=harness, mode=mode)


def _lint_path_payload(
    target: Path, skill_or_path: str, harness: str | None = None, *, mode: str = "lenient", contain: bool = False
) -> dict[str, Any]:
    from . import agent_skill_format

    skill_dir, metadata, source = _load_skill(target, skill_or_path)
    skill_md = _skill_md_path(skill_dir)
    errors: list[str] = []
    warnings: list[str] = []
    render_errors: list[str] = []
    if not skill_dir.is_dir():
        errors.append(f"skill directory not found: {skill_dir}")
    if not skill_md.is_file():
        errors.append(f"SKILL.md not found: {skill_md}")
        text = ""
    else:
        text = skill_md.read_text(encoding="utf-8", errors="replace")
        if not text.strip():
            errors.append("SKILL.md is empty")
        if len(text) > 40_000:
            warnings.append("SKILL.md exceeds 40000 characters")
    if not metadata.get("id"):
        errors.append("metadata id is required")
    if metadata.get("trust_level") and metadata["trust_level"] not in TRUST_LEVELS:
        errors.append(f"unknown trust_level: {metadata['trust_level']}")
    for key in ("required_tools", "required_mcp_servers", "supported_harnesses", "tests"):
        if key in metadata and not isinstance(metadata[key], list):
            errors.append(f"metadata {key} must be a list")
    if "obligations" in metadata:
        from . import skill_obligations

        _, obligation_errors = skill_obligations.parse_obligations(metadata)
        errors.extend(obligation_errors)
    format_result = agent_skill_format.validate(skill_dir, mode=mode)
    errors.extend(format_result.errors)
    warnings.extend(format_result.diagnostics)
    if harness is not None:
        adapters = _adapter_map(target)
        adapter = adapters.get(harness)
        if adapter is None:
            errors.append(f"unknown harness adapter: {harness}")
        elif adapter.get("status") == "planned":
            errors.append(f"harness adapter is planned: {harness}")
        elif not adapter.get("install_path"):
            errors.append(f"harness adapter has no install path: {harness}")
        elif text:
            skill_id = _slug(str(metadata.get("id") or skill_dir.name))
            rendered = _render_skill_text_for_harness(text, metadata, skill_id, harness)
            render_errors = _rendered_skill_validation(rendered, harness)
            errors.extend(render_errors)
    injection = scan_untrusted(text)
    if injection.flagged:
        warnings.append("SKILL.md contains injection-like text; review as untrusted content before installing")
    source = dict(source)
    source["skill_version"] = str(metadata.get("version") or "0.1.0")
    source["fingerprint"] = _fingerprint(skill_dir) if skill_dir.is_dir() else None
    source["metadata_fingerprint"] = _file_fingerprint(_metadata_path(skill_dir))
    payload = {
        "target": str(target),
        "skill_dir": str(skill_dir),
        "skill_id": metadata.get("id") or skill_dir.name,
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "harness": harness,
        "render_errors": render_errors,
        "injection": {
            "flagged": injection.flagged,
            "count": injection.count,
            "markers": injection.markers,
        },
        "metadata": metadata,
        "source": source,
        "agent_skills": {
            "mode": mode,
            "fields": format_result.fields,
            "diagnostics": list(format_result.diagnostics),
            "allowed_tools_are_permissions": False,
        },
        "fingerprint": source["fingerprint"],
    }
    payload["changelog"] = (
        _changelog_payload(skill_dir, metadata, contain=contain)
        if skill_dir.is_dir()
        else {"present": False, "path": None, "fingerprint": None, "headings": []}
    )
    payload["trust_score"] = (
        _trust_score_payload(skill_dir, metadata, payload, source, contain=contain)
        if skill_dir.is_dir()
        else {
            "score": 0,
            "trust_level": metadata.get("trust_level") or "unreviewed",
            "signals": ["skill directory missing"],
            "tests_declared": 0,
            "changelog": payload["changelog"],
        }
    )
    return payload


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


def search(*, target: Path, query: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    terms = [term.casefold() for term in query.split() if term]
    matches: list[dict[str, Any]] = []
    for row in _iter_registry(target):
        metadata = row["metadata"]
        haystack = " ".join(
            str(metadata.get(key, ""))
            for key in ("id", "title", "description", "required_tools", "required_mcp_servers", "supported_harnesses")
        ).casefold()
        if not terms or all(term in haystack for term in terms):
            matches.append(row)
    payload = {"target": str(target), "query": query, "count": len(matches), "skills": matches}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"skills search: {query}")
    if not matches:
        print("no skills found")
    for row in matches:
        metadata = row["metadata"]
        print(f"- {metadata.get('id')} [{metadata.get('trust_level', 'unreviewed')}] {metadata.get('title', '')}")
    return 0


def import_skill(
    *,
    target: Path,
    source: Path,
    skill_id: str | None = None,
    force: bool = False,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    source = source.expanduser().resolve()
    payload, error, rc = _registry_import_payload(target=target, source=source, skill_id=skill_id, force=force)
    if payload is None:
        print(f"error: {error}", file=sys.stderr)
        return rc
    lint_payload = payload["lint"]
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return rc
    print(f"skill_import: {payload['skill_id']}")
    print(f"skill_dir: {payload['skill_dir']}")
    print(f"fingerprint: {lint_payload.get('fingerprint')}")
    for warning in lint_payload["warnings"]:
        print(f"warning: {warning}")
    for error in lint_payload["errors"]:
        print(f"error: {error}")
    return rc


def lint(
    *, target: Path, skill: str, harness: str | None = None, mode: str = "lenient", json_output: bool = False
) -> int:
    target = target.expanduser().resolve()
    payload = _lint_payload(target, skill, harness=harness, mode=mode)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["valid"] else 1
    print(f"skill lint: {payload['skill_id']}")
    if harness is not None:
        print(f"harness: {harness}")
    print(f"valid: {str(payload['valid']).lower()}")
    for warning in payload["warnings"]:
        print(f"warning: {warning}")
    for error in payload["errors"]:
        print(f"error: {error}")
    return 0 if payload["valid"] else 1


def _evaluate_install_dir(workspace: Path, harness: str, skill_id: str) -> tuple[Path, bool]:
    """Return ``(projected_install_dir, escapes_workspace)`` without raising.

    Read-only callers (fleet status) use this to describe a copy that resolves
    outside the workspace instead of treating it as a writable destination.
    """
    if harness == "hermes":
        # Real Hermes reads skills from its own data dir (auto-discovered as a
        # local skill), not the repo. Install there so it actually takes effect.
        return _hermes_skills_root() / _slug(skill_id), False
    adapter = _adapter_map(workspace)[harness]
    install_dir = workspace / str(adapter["install_path"]).format(skill_id=skill_id)
    workspace_resolved = workspace.resolve()
    resolved = install_dir.resolve()
    escapes = (
        ".." in install_dir.parts or resolved == workspace_resolved or not resolved.is_relative_to(workspace_resolved)
    )
    return install_dir, escapes


def _install_dir(workspace: Path, harness: str, skill_id: str) -> Path:
    install_dir, escapes = _evaluate_install_dir(workspace, harness, skill_id)
    if escapes:
        raise ValueError(f"skill install path escapes workspace: {install_dir}")
    return install_dir


def _safe_install_path(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value.strip())
    if path.is_absolute() or ".." in path.parts:
        return None
    return value.strip()


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
    if _state_root_selector_kind(target, requested) == "registry":
        return True
    candidate = Path(requested).expanduser()
    if candidate.exists():
        return False
    bundled = _bundled_skill_path(_slug(requested))
    return not _skill_md_path(bundled).is_file()


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _adapter_map(target: Path) -> dict[str, dict[str, Any]]:
    adapters = {key: dict(value) for key, value in HARNESS_ADAPTERS.items()}
    config = _state_json_via_anchor(target, "skills", "adapters.json")
    overlay = config.get("adapters")
    if isinstance(overlay, dict):
        for adapter_id, value in overlay.items():
            if not isinstance(value, dict):
                continue
            adapters[_slug(str(adapter_id))] = {
                "status": str(value.get("status") or "local"),
                "format": str(value.get("format") or "custom-skill"),
                "install_path": _safe_install_path(value.get("install_path")),
                "source": "local-config",
            }
    return adapters


def _install_targets(workspace: Path) -> tuple[str, ...]:
    return tuple(
        key
        for key, value in _adapter_map(workspace).items()
        if value.get("status") in {"built-in", "local"} and value.get("install_path")
    )


def _latest_install_receipt(target: Path, skill_id: str, harness: str) -> dict[str, Any]:
    """Read the canonical receipt through the held state-root anchor.

    A symlinked (or otherwise refused) receipt contributes no data: callers
    report the installation as unknown instead of surfacing outside JSON.
    """
    try:
        with _held_state_root(target) as state_anchor:
            raw = _read_state_file_bytes(state_anchor, "skills", "installs", f"{_slug(skill_id)}-{harness}.json")
    except SkillsStatePathError:
        return {}
    if raw is None:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _install_history(target: Path, skill_id: str | None = None, harness: str | None = None) -> list[dict[str, Any]]:
    """Read ``history.jsonl`` through the held state-root anchor.

    A symlinked or refused history file yields no rows rather than following
    the link to outside content.
    """
    try:
        with _held_state_root(target) as state_anchor:
            raw = _read_state_file_bytes(state_anchor, "skills", "installs", "history.jsonl")
    except SkillsStatePathError:
        raw = None
    rows: list[dict[str, Any]] = []
    if raw is not None:
        for line in raw.decode("utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                rows.append(value)
    if skill_id is not None:
        rows = [row for row in rows if row.get("skill_id") == _slug(skill_id)]
    if harness is not None:
        rows = [row for row in rows if row.get("target") == harness]
    rows.sort(key=lambda row: str(row.get("installed_at") or row.get("receipt_id") or ""), reverse=True)
    return rows


def _renderer_identity(contract: dict[str, Any]) -> str:
    identity = hashlib.sha256(json.dumps(contract, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return f"skills-renderer:{identity}"


def _renderer_contract(target: Path, harness: str) -> dict[str, Any]:
    adapter = _adapter_map(target).get(harness, {})
    contract = {
        "schema_version": RENDERER_SCHEMA_VERSION,
        "harness": harness,
        "format": adapter.get("format"),
    }
    return {**contract, "identity": _renderer_identity(contract)}


def _valid_receipt_contract(receipt: dict[str, Any], *, skill_id: str, harness: str) -> bool:
    source = receipt.get("source")
    render = receipt.get("render")
    installed = receipt.get("installed")
    if receipt.get("schema_version") != RECEIPT_SCHEMA_VERSION:
        return False
    if receipt.get("skill_id") != skill_id or receipt.get("target") != harness:
        return False
    if not isinstance(source, dict) or not isinstance(render, dict) or not isinstance(installed, dict):
        return False
    if source.get("schema_version") != SOURCE_SCHEMA_VERSION:
        return False
    if not all(
        isinstance(source.get(key), str) and source.get(key)
        for key in ("identity", "fingerprint", "metadata_fingerprint")
    ):
        return False
    kind = source.get("kind")
    identity = source["identity"]
    reviewed = source.get("reviewed")
    if not isinstance(reviewed, bool) or not isinstance(source.get("skill_version"), str):
        return False
    if kind == "brigade-bundle":
        if (
            identity != f"{BUNDLED_SOURCE_PREFIX}{skill_id}"
            or reviewed is not True
            or not isinstance(source.get("brigade_version"), str)
            or not source["brigade_version"]
        ):
            return False
    elif kind == "registry":
        if identity != f"registry://skills/{skill_id}" or reviewed is not False:
            return False
    elif kind == "path":
        source_path = identity.removeprefix("path:")
        if identity == source_path or reviewed is not False or not Path(source_path).is_absolute():
            return False
    else:
        return False
    if not all(isinstance(render.get(key), str) and render.get(key) for key in ("identity", "fingerprint")):
        return False
    render_contract = {
        "schema_version": render.get("schema_version"),
        "harness": render.get("harness"),
        "format": render.get("format"),
    }
    if (
        type(render_contract["schema_version"]) is not int
        or render_contract["schema_version"] < 1
        or render_contract["harness"] != harness
        or not isinstance(render_contract["format"], str)
        or not render_contract["format"]
        or render["identity"] != _renderer_identity(render_contract)
    ):
        return False
    if not all(
        isinstance(installed.get(key), str) and installed.get(key)
        for key in ("bundle_fingerprint", "skill_fingerprint", "metadata_fingerprint")
    ):
        return False
    return installed.get("skill_fingerprint") == render.get("fingerprint")


def _previous_install_receipt(anchor: _StateRootAnchor, *, skill_id: str, harness: str) -> dict[str, Any]:
    """Read the canonical receipt through the anchor; retain only contract-valid receipts.

    The read goes through ``_read_state_file_bytes`` (``O_NOFOLLOW``, regular
    file, single link), so a symlinked or hardlinked receipt refuses the
    operation instead of dragging outside content into state or output.
    Content that parses but does not satisfy the receipt contract contributes
    no ``previous_receipt``.
    """
    raw = _read_state_file_bytes(anchor, "skills", "installs", f"{skill_id}-{harness}.json")
    if raw is None:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict) or not _valid_receipt_contract(data, skill_id=skill_id, harness=harness):
        return {}
    return data


def _dir_fd_subtree_plain(fd: int) -> bool:
    for name in os.listdir(fd):
        try:
            st = os.lstat(name, dir_fd=fd)
        except OSError:
            return False
        if stat_module.S_ISLNK(st.st_mode):
            return False
        if stat_module.S_ISDIR(st.st_mode):
            try:
                child = os.open(name, _DIR_OPEN_FLAGS, dir_fd=fd)
            except OSError:
                return False
            try:
                if not _dir_fd_subtree_plain(child):
                    return False
            finally:
                os.close(child)
        elif not stat_module.S_ISREG(st.st_mode):
            return False
    return True


def _anchor_subtree_is_plain(anchor: _StateRootAnchor, *relative: str) -> bool:
    """True when the anchored subtree holds only plain directories and files.

    A skipped symlink anywhere below means the on-disk copy is richer than
    anything the collector can return; mutation planners must refuse such a
    destination instead of writing over or around the planted entry.
    """
    if not _HAS_DESCRIPTOR_ANCHOR:
        return True
    try:
        fd, opened = _anchor_open_chain(anchor, *relative, missing_ok=True)
    except SkillsStatePathError:
        return False
    if fd is None:
        return True
    try:
        try:
            return _dir_fd_subtree_plain(fd)
        except OSError:
            return False
    finally:
        for held in reversed(opened):
            os.close(held)


def _installed_tree_snapshot(
    workspace: Path, installed_dir: Path, anchor: _StateRootAnchor | None = None
) -> tuple[list[tuple[str, ...]], dict[tuple[str, ...], bytes]]:
    """Collect an installed copy's plain files for inspection.

    Copies located inside ``.brigade`` (e.g. the built-in ``mcp`` target's
    ``mcp-resources``) are state-root content: they are read through the held
    state-root descriptor, so a planted symlink is skipped or refuses instead
    of dragging outside bytes into diffs, drift, or rollback material.
    Trusted harness directories outside the state root keep the plain
    descriptor walk.
    """
    parts = _lexical_state_root_parts(workspace, installed_dir)
    if parts is None:
        if not installed_dir.is_dir():
            return [], {}
        return _collect_source_tree(installed_dir)

    def _collect(held: _StateRootAnchor) -> tuple[list[tuple[str, ...]], dict[tuple[str, ...], bytes]]:
        if _state_entry_kind(held, *parts) != "dir":
            return [], {}
        return _read_tree_from_anchor(held, *parts)

    if anchor is not None:
        return _collect(anchor)
    try:
        with _held_state_root(workspace) as fresh_anchor:
            return _collect(fresh_anchor)
    except SkillsStatePathError:
        # A redirected or planted component contributes nothing rather than
        # being followed; callers report absence instead of outside content.
        return [], {}


def _drift_payload(
    *,
    target: Path,
    skill_id: str,
    harness: str,
    lint_payload: dict[str, Any],
    rendered: str,
    installed_dir: Path,
) -> dict[str, Any]:
    receipt = _latest_install_receipt(target, skill_id, harness)
    snapshot_dirs, snapshot_files = _installed_tree_snapshot(target, installed_dir)
    installed_raw = snapshot_files.get(("SKILL.md",))
    installed_present = installed_raw is not None
    installed_text = installed_raw.decode("utf-8", errors="replace") if installed_raw is not None else ""
    current_source = raw_current_source if isinstance((raw_current_source := lint_payload.get("source")), dict) else {}
    current_render = _renderer_contract(target, harness)
    current_render_fingerprint = _text_fingerprint(rendered)
    installed_skill_fingerprint = _text_fingerprint(installed_text) if installed_present else None
    installed_bundle_fingerprint = _files_fingerprint(snapshot_files) if snapshot_files else None
    installed_metadata_fingerprint = _bytes_fingerprint(snapshot_files.get(("skill.json",)))
    known = _valid_receipt_contract(receipt, skill_id=skill_id, harness=harness)
    if not installed_present and receipt and not known:
        overall = "unknown"
        source_state = "unknown"
        render_state = "unknown"
        local_state = "unknown"
    elif not installed_present:
        overall = "missing"
        source_state = "unknown"
        render_state = "unknown"
        local_state = "unknown"
    elif not known:
        overall = "unknown"
        source_state = "unknown"
        render_state = "unknown"
        local_state = "unknown"
    else:
        receipt_source = receipt["source"]
        receipt_render = receipt["render"]
        receipt_installed = receipt["installed"]
        source_state = (
            "current"
            if receipt_source.get("identity") == current_source.get("identity")
            and receipt_source.get("fingerprint") == current_source.get("fingerprint")
            and receipt_source.get("metadata_fingerprint") == current_source.get("metadata_fingerprint")
            else "changed"
        )
        render_state = "current" if receipt_render.get("identity") == current_render.get("identity") else "changed"
        local_state = (
            "current"
            if receipt_installed.get("bundle_fingerprint") == installed_bundle_fingerprint
            and receipt_installed.get("skill_fingerprint") == installed_skill_fingerprint
            and receipt_installed.get("metadata_fingerprint") == installed_metadata_fingerprint
            else "changed"
        )
        overall = "changed" if "changed" in {source_state, render_state, local_state} else "current"
    return {
        "overall": overall,
        "source": source_state,
        "render": render_state,
        "local_edit": local_state,
        "receipt_known": known,
        "content_changed": installed_text != rendered,
        "installed": installed_present,
        "installed_skill_fingerprint": installed_skill_fingerprint,
        "installed_bundle_fingerprint": installed_bundle_fingerprint,
        "installed_metadata_fingerprint": installed_metadata_fingerprint,
        "current_source": current_source,
        "current_render": {**current_render, "fingerprint": current_render_fingerprint},
        "receipt": receipt or None,
    }


def install(
    *,
    workspace: Path,
    skill: str,
    harness: str,
    force: bool = False,
    json_output: bool = False,
) -> int:
    workspace = workspace.expanduser().resolve()
    install_targets = _install_targets(workspace)
    if harness not in (*install_targets, "all"):
        print(f"error: unknown skill install target: {harness}", file=sys.stderr)
        return 2
    # Fail fast on a redirected or broken state root so the typed refusal
    # surfaces the same way for every source kind.
    try:
        _hold_state_root(workspace).close()
    except SkillsStatePathError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    # Resolve the source once (the metadata read here is discarded), then
    # collect the source tree exactly once before anything validates it.
    # Metadata, lint, rendering, and every fingerprint below consume only
    # these collected bytes, so a source swapped between validation steps can
    # never make the command install a generation nobody linted. Registry
    # selections are collected through the held state-root anchor; external
    # and bundled sources are trusted operator paths.
    try:
        source_dir, _discarded_metadata, resolved_source = _load_skill(workspace, skill)
    except SkillsStatePathError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    collected: tuple[list[tuple[str, ...]], dict[tuple[str, ...], bytes]] | None = None
    staged_dir: Path | None = None
    staging_root: str | None = None
    try:
        if resolved_source.get("kind") == "registry":
            # Collect exactly the entry that was requested (by its directory
            # name), never one named by attacker-controlled metadata.
            skill_slug = _slug(skill.removeprefix("registry:") if skill.startswith("registry:") else skill)
            try:
                with _held_state_root(workspace) as state_anchor:
                    snapshot_dirs, snapshot_files = _read_registry_entry_tree(state_anchor, skill_slug)
            except SkillsStatePathError:
                raise
        else:
            snapshot_dirs, snapshot_files = _collect_source_tree(source_dir)
        collected = (snapshot_dirs, snapshot_files)
        with tempfile.TemporaryDirectory(prefix="brigade-install-lint-") as staging:
            staged_dir = Path(staging) / source_dir.name
            staging_root = staging
            _write_snapshot_tree(snapshot_dirs, snapshot_files, staged_dir)
            lint_payload = _lint_payload(Path(staging), str(staged_dir))
            # Repoint immediately so every later consumer — success or
            # failure — references the real source, never the ephemeral
            # staging directory.
            lint_payload["target"] = str(workspace)
            lint_payload = _repoint_staged_lint_paths(lint_payload, source_dir, staged_dir)
    except SkillsStatePathError as exc:
        lint_payload = {
            "target": str(workspace),
            "skill_dir": str(source_dir),
            "skill_id": source_dir.name,
            "valid": False,
            "errors": [str(exc)],
            "warnings": [],
            "harness": None,
            "render_errors": [],
            "injection": {"flagged": False, "count": 0, "markers": []},
            "metadata": {},
            "source": {},
            "agent_skills": {
                "mode": "lenient",
                "fields": {},
                "diagnostics": [],
                "allowed_tools_are_permissions": False,
            },
            "fingerprint": None,
            "changelog": {"present": False, "path": None, "fingerprint": None, "headings": []},
            "trust_score": {
                "score": 0,
                "trust_level": "unreviewed",
                "signals": ["skill directory unreadable"],
                "tests_declared": 0,
                "changelog": {"present": False, "path": None, "fingerprint": None, "headings": []},
            },
        }
    lint_payload["target"] = str(workspace)
    lint_payload["skill_dir"] = str(source_dir)
    snapshot_dirs, snapshot_files = collected if collected is not None else ([], {})
    source_skill_md = snapshot_files.get(("SKILL.md",))
    if source_skill_md is None:
        # The collector skips symlinks instead of following them: a symlinked
        # SKILL.md must refuse the install rather than let the pathname
        # re-reads below launder outside content into state.
        message = "SKILL.md must be a plain regular file in the skill source (symlinks are never followed): " + str(
            _skill_md_path(source_dir)
        )
        if json_output:
            print(
                json.dumps(
                    {"workspace": str(workspace), "installed": False, "errors": [message], "lint": lint_payload},
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            print(f"error: {message}", file=sys.stderr)
        return 2
    if not lint_payload["valid"]:
        if json_output:
            print(
                json.dumps(
                    {"workspace": str(workspace), "installed": False, "lint": lint_payload}, indent=2, sort_keys=True
                )
            )
        else:
            print(f"error: skill lint failed: {skill}", file=sys.stderr)
        return 1
    skill_id = _slug(str(lint_payload["skill_id"]))
    metadata = raw_metadata if isinstance((raw_metadata := lint_payload.get("metadata")), dict) else {}
    source_identity = dict(resolved_source)
    source_identity["skill_version"] = str(metadata.get("version") or "0.1.0")
    version = str(metadata.get("version") or "0.1.0")
    source_path = str(metadata.get("source") or source_dir)
    snapshot_fingerprint = _files_fingerprint(snapshot_files)
    # Provenance is derived from the original resolution; the fingerprints are
    # derived from the collected snapshot. Together they describe exactly the
    # generation that was linted and that the copies below materialize.
    source_identity["fingerprint"] = snapshot_fingerprint
    source_identity["metadata_fingerprint"] = _bytes_fingerprint(snapshot_files.get(("skill.json",)))
    lint_payload["fingerprint"] = snapshot_fingerprint
    lint_payload["source"] = source_identity
    # Trust scoring is provenance-sensitive: recompute it against the real
    # resolution so a staged-lint run scores the skill exactly like a direct
    # lint of the source would.
    if staged_dir is not None:
        lint_payload["trust_score"] = _trust_score_payload(staged_dir, metadata, lint_payload, source_identity)
    # The staged copy must not leak its temporary location into output or
    # receipts: report changelog paths against the real source directory.
    trust_block = lint_payload.get("trust_score")
    for container in (
        lint_payload.get("changelog"),
        trust_block.get("changelog") if isinstance(trust_block, dict) else None,
    ):
        if isinstance(container, dict):
            path_text = container.get("path")
            if staging_root is not None and isinstance(path_text, str) and path_text.startswith(staging_root):
                container["path"] = str(source_dir / Path(path_text).relative_to(staging_root))
    targets = install_targets if harness == "all" else (harness,)
    receipts: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    try:
        state_anchor = _hold_state_root(workspace)
    except SkillsStatePathError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        for install_target in targets:
            if install_target == "hermes" and not _hermes_home().exists():
                # Do not create ~/.hermes for someone who does not run Hermes.
                skipped.append({"target": "hermes", "reason": "Hermes home not found (is Hermes installed?)"})
                continue
            dest = _install_dir(workspace, install_target, skill_id)
            source_text = source_skill_md.decode("utf-8", errors="replace")
            rendered_text = _render_skill_text_for_harness(source_text, metadata, skill_id, install_target)
            render_fingerprint = _text_fingerprint(rendered_text)
            render_contract = _renderer_contract(workspace, install_target)
            render_errors = _rendered_skill_validation(rendered_text, install_target)
            if render_errors:
                if json_output:
                    print(
                        json.dumps(
                            {
                                "workspace": str(workspace),
                                "installed": False,
                                "target": install_target,
                                "errors": render_errors,
                                "lint": lint_payload,
                            },
                            indent=2,
                            sort_keys=True,
                        )
                    )
                else:
                    for error in render_errors:
                        print(f"error: {install_target}: {error}", file=sys.stderr)
                return 1
            if dest.exists() and not force:
                print(f"error: installed skill already exists: {dest}", file=sys.stderr)
                return 2
            state_anchor.revalidate()
            receipt_name = f"{skill_id}-{install_target}.json"
            receipt_path = _installs_root(workspace) / receipt_name
            install_rel = str(_adapter_map(workspace)[install_target]["install_path"]).format(skill_id=skill_id)
            under_state_root = install_rel.startswith(".brigade/")
            state_parts = tuple(part for part in install_rel.split("/") if part)[1:]
            # Refuse redirected state paths before anything is mutated.
            _require_plain_state_dirs(state_anchor, "skills", "installs")
            _require_plain_state_dirs(state_anchor, "skills", "rollback")
            previous_receipt = _previous_install_receipt(state_anchor, skill_id=skill_id, harness=install_target)
            rollback_snapshot: str | None = None
            rollback_snapshot_fingerprint: str | None = None
            if dest.exists():
                stamp = _now().replace(":", "").replace("+", "Z").replace(".", "-")
                rollback_dir = _rollback_root(workspace, skill_id, install_target) / stamp
                # Collect the about-to-be-replaced copy once and materialize
                # the snapshot from those bytes; the snapshot fingerprint is
                # recorded in the new receipt so rollback can prove the
                # snapshot it restores is byte-identical to what was captured.
                # dest is normally a trusted harness dir; for custom adapters
                # installing under .brigade it is state-root content captured
                # to the rollback bar (accepted residual, see docstring).
                replaced_dirs, replaced_files = _collect_source_tree(dest)
                _write_collected_tree_into_anchor(
                    replaced_dirs, replaced_files, state_anchor, "skills", "rollback", skill_id, install_target, stamp
                )
                rollback_snapshot = str(rollback_dir)
                rollback_snapshot_fingerprint = _files_fingerprint(replaced_files)
                if under_state_root:
                    _remove_tree_in_anchor(state_anchor, *state_parts)
                else:
                    shutil.rmtree(dest)
            install_files = dict(snapshot_files)
            install_files[("SKILL.md",)] = rendered_text.encode("utf-8")
            if under_state_root:
                _write_collected_tree_into_anchor(snapshot_dirs, install_files, state_anchor, *state_parts)
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                _write_snapshot_tree(snapshot_dirs, install_files, dest)
            # Fingerprints are computed from the same bytes that were written;
            # nothing is read back through a pathname.
            installed_fingerprint = _files_fingerprint(install_files)
            installed_skill_fingerprint = render_fingerprint
            installed_metadata_fingerprint = _bytes_fingerprint(install_files.get(("skill.json",)))
            installed_at = _now()
            receipt = {
                "schema_version": RECEIPT_SCHEMA_VERSION,
                "workspace": str(workspace),
                "receipt_id": f"{installed_at[:19].replace(':', '').replace('-', '')}-{skill_id}-{install_target}",
                "skill_id": skill_id,
                "target": install_target,
                "installed_dir": str(dest),
                "installed_at": installed_at,
                "version": version,
                "source_path": source_path,
                "source": source_identity,
                "render": {**render_contract, "fingerprint": render_fingerprint},
                "installed": {
                    "bundle_fingerprint": installed_fingerprint,
                    "skill_fingerprint": installed_skill_fingerprint,
                    "metadata_fingerprint": installed_metadata_fingerprint,
                },
                "fingerprint": lint_payload.get("fingerprint"),
                "source_fingerprint": lint_payload.get("fingerprint"),
                "render_fingerprint": render_fingerprint,
                "installed_fingerprint": installed_fingerprint,
                "format": _adapter_map(workspace)[install_target].get("format"),
                "rollback_snapshot": rollback_snapshot,
                "rollback_snapshot_fingerprint": rollback_snapshot_fingerprint,
                "previous_receipt": previous_receipt if previous_receipt else None,
                "trust_score": lint_payload.get("trust_score"),
                "changelog": lint_payload.get("changelog"),
            }
            receipt["receipt_path"] = str(receipt_path)
            history_receipt = dict(receipt)
            history_receipt["receipt_path"] = str(receipt_path)
            # The canonical receipt file keeps its original schema (no
            # receipt_path key); the single anchored write below replaces the
            # former raw path-based write plus anchored rewrite pair.
            canonical_receipt = {key: value for key, value in receipt.items() if key != "receipt_path"}
            _write_state_file(state_anchor, "skills", "installs", receipt_name, data=_json_bytes(canonical_receipt))
            _append_state_line(
                state_anchor,
                "skills",
                "installs",
                "history.jsonl",
                data=(json.dumps(history_receipt, sort_keys=True) + "\n").encode("utf-8"),
            )
            receipts.append(receipt)
    except SkillsStatePathError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        state_anchor.close()
    receipt = {
        "workspace": str(workspace),
        "skill_id": skill_id,
        "target": harness,
        "installed_at": _now(),
        "fingerprint": lint_payload.get("fingerprint"),
        "source": lint_payload.get("source"),
        "targets": list(targets),
        "receipts": receipts,
        "skipped": skipped,
    }
    payload = {"installed": True, "receipt": receipt}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"skill_install: {skill_id}")
    print(f"target: {harness}")
    for item in receipts:
        print(f"- {item['target']}: {item['installed_dir']}")
        print(f"  receipt: {item['receipt_path']}")
    for item in skipped:
        print(f"- {item['target']}: skipped ({item['reason']})")
    return 0


def _trust_at_least(actual: str, minimum: str) -> bool:
    try:
        return TRUST_LEVELS.index(actual) >= TRUST_LEVELS.index(minimum)
    except ValueError:
        return False


def _sync_plan(*, workspace: Path, harness: str, trust: str) -> tuple[list[dict[str, Any]], str | None]:
    install_targets = _install_targets(workspace)
    if harness not in (*install_targets, "all"):
        return [], f"unknown skill install target: {harness}"
    if trust not in TRUST_LEVELS:
        return [], f"unknown skill trust level: {trust}"
    targets = install_targets if harness == "all" else (harness,)
    items: list[dict[str, Any]] = []
    registry = _iter_registry(workspace)
    for registry_row in registry:
        registry_metadata = registry_row["metadata"]
        skill_id = _slug(str(registry_metadata.get("id") or Path(str(registry_row["skill_dir"])).name))
        lint_payload = _lint_payload(workspace, f"registry:{skill_id}")
        metadata = raw if isinstance((raw := lint_payload.get("metadata")), dict) else registry_metadata
        trust_score = raw_trust_score if isinstance((raw_trust_score := lint_payload.get("trust_score")), dict) else {}
        actual_trust = str(trust_score.get("trust_level") or "unreviewed")
        supported = metadata.get("supported_harnesses")
        supported_harnesses = set(supported) if isinstance(supported, list) else set()
        source = raw_source if isinstance((raw_source := lint_payload.get("source")), dict) else {}
        for install_target in targets:
            item: dict[str, Any] = {
                "skill_id": skill_id,
                "harness": install_target,
                "source": source,
                "trust_level": actual_trust,
                "minimum_trust": trust,
                "state": "blocked",
                "action": "none",
                "result": "blocked",
                "reason": None,
                "receipt": None,
                "error": None,
            }
            if not lint_payload.get("valid"):
                errors = raw_errors if isinstance((raw_errors := lint_payload.get("errors")), list) else []
                item["reason"] = "; ".join(str(error) for error in errors) or "skill lint failed"
                items.append(item)
                continue
            if metadata.get("enabled", True) is False:
                item.update(
                    state="excluded",
                    result="excluded",
                    reason="skill is disabled by registry metadata",
                )
                items.append(item)
                continue
            if not _trust_at_least(actual_trust, trust):
                item.update(
                    state="excluded",
                    result="excluded",
                    reason=f"trust level {actual_trust} is below {trust}",
                )
                items.append(item)
                continue
            if supported_harnesses and install_target not in supported_harnesses:
                item.update(
                    state="excluded",
                    result="excluded",
                    reason=f"harness {install_target} is not supported by registry metadata",
                )
                items.append(item)
                continue
            if install_target == "hermes" and not _hermes_home().exists():
                item.update(
                    state="excluded",
                    result="excluded",
                    reason="Hermes home not found (is Hermes installed?)",
                )
                items.append(item)
                continue
            # Registry rendering text comes from the anchored read; a refused
            # entry contributes no text and the render below fails validation.
            source_text = _anchored_registry_text(workspace, skill_id, "SKILL.md") or ""
            rendered = _render_skill_text_for_harness(source_text, metadata, skill_id, install_target)
            render_errors = _rendered_skill_validation(rendered, install_target)
            if render_errors:
                item["reason"] = "; ".join(render_errors)
                items.append(item)
                continue
            installed_dir = _install_dir(workspace, install_target, skill_id)
            drift = _drift_payload(
                target=workspace,
                skill_id=skill_id,
                harness=install_target,
                lint_payload=lint_payload,
                rendered=rendered,
                installed_dir=installed_dir,
            )
            item["drift"] = {
                key: drift[key]
                for key in ("overall", "source", "render", "local_edit", "receipt_known", "content_changed")
            }
            if drift["overall"] == "current":
                item.update(state="current", result="unchanged")
            elif drift["overall"] == "missing":
                item.update(state="missing", action="install", result="planned")
            elif drift["overall"] == "changed":
                item.update(state="changed", action="update", result="planned")
            else:
                item["reason"] = "installed copy has no valid provenance receipt"
            items.append(item)
    return items, None


@dataclass(frozen=True)
class UserProfileSkillPackage:
    """A reviewed, supported skill package materialized as in-memory bytes.

    ``files`` maps POSIX relative paths to file bytes. ``SKILL.md`` is replaced
    with harness-rendered UTF-8 bytes; every other file is copied byte-for-byte
    from the registry skill directory. No directories are created.
    """

    skill_id: str
    source_identity: str
    source_fingerprint: str
    metadata_fingerprint: str
    files: dict[str, bytes]


_USER_PROFILE_MAX_FILES = 512
_USER_PROFILE_MAX_BYTES = 8 * 1024 * 1024
_USER_PROFILE_HARNESSES = {"claude", "codex", "openclaw", "kimi", "grok", "cursor", "opencode"}


def _snapshot_package_files(snapshot_files: dict[tuple[str, ...], bytes]) -> dict[str, bytes] | None:
    """Package-file view of an already-collected registry snapshot.

    Returns ``None`` if the package would exceed the file-count or byte cap,
    so the caller excludes the whole package rather than truncating it.
    """
    files: dict[str, bytes] = {}
    total = 0
    for parts, data in sorted(snapshot_files.items()):
        rel = Path(*parts).as_posix()
        if rel == ".git" or rel.startswith(".git/"):
            continue
        # lexical containment: reject absolute-looking keys and ".." components
        if rel.startswith("/") or ".." in Path(rel).parts:
            continue
        if len(files) >= _USER_PROFILE_MAX_FILES:
            return None
        if total + len(data) > _USER_PROFILE_MAX_BYTES:
            return None
        files[rel] = data
        total += len(data)
    return dict(sorted(files.items()))


def _user_profile_package(
    *,
    workspace: Path,
    harness: str,
    registry_row: dict[str, Any],
    minimum_trust: str,
) -> UserProfileSkillPackage | None:
    registry_metadata = registry_row["metadata"]
    skill_id = _slug(str(registry_metadata.get("id") or Path(str(registry_row["skill_dir"])).name))
    lint_payload = _lint_payload(workspace, f"registry:{skill_id}", harness=harness)
    if not lint_payload.get("valid"):
        return None
    metadata = raw_metadata if isinstance((raw_metadata := lint_payload.get("metadata")), dict) else registry_metadata
    if metadata.get("enabled", True) is False:
        return None
    trust_score = raw_trust_score if isinstance((raw_trust_score := lint_payload.get("trust_score")), dict) else {}
    actual_trust = str(trust_score.get("trust_level") or "unreviewed")
    if not _trust_at_least(actual_trust, minimum_trust):
        return None
    supported = metadata.get("supported_harnesses")
    supported_harnesses = set(supported) if isinstance(supported, list) else set()
    if supported_harnesses and harness not in supported_harnesses:
        return None
    source = raw_source if isinstance((raw_source := lint_payload.get("source")), dict) else {}
    source_identity = source.get("identity")
    if not (isinstance(source_identity, str) and source_identity):
        return None
    source_fingerprint = lint_payload.get("fingerprint")
    if not (isinstance(source_fingerprint, str) and source_fingerprint):
        return None
    metadata_fingerprint = source.get("metadata_fingerprint")
    if not (isinstance(metadata_fingerprint, str) and metadata_fingerprint):
        return None
    # The package body comes from one anchored registry snapshot; nothing is
    # walked or re-read by pathname under the state root.
    try:
        with _held_state_root(workspace) as anchor:
            _snapshot_dirs, snapshot_files = _read_registry_entry_tree(anchor, skill_id)
    except SkillsStatePathError:
        return None
    files = _snapshot_package_files(snapshot_files)
    if files is None:
        return None
    source_text = snapshot_files.get(("SKILL.md",))
    if "SKILL.md" in files and source_text is not None:
        rendered = _render_skill_text_for_harness(
            source_text.decode("utf-8", errors="replace"), metadata, skill_id, harness
        )
        if _rendered_skill_validation(rendered, harness):
            return None
        files = {**files, "SKILL.md": rendered.encode("utf-8")}
        files = dict(sorted(files.items()))
    return UserProfileSkillPackage(
        skill_id=skill_id,
        source_identity=source_identity,
        source_fingerprint=source_fingerprint,
        metadata_fingerprint=metadata_fingerprint,
        files=files,
    )


def user_profile_skill_packages(
    *,
    workspace: Path,
    harness: str,
    minimum_trust: str = "workspace",
) -> tuple[UserProfileSkillPackage, ...]:
    """Reviewed, supported skill packages for a native user-profile harness.

    Iterates the registry, runs lint + render validation through the existing
    helpers, and returns whole in-memory packages (regular files only, SKILL.md
    rendered for the harness). Excludes disabled, untrusted, unsupported,
    lint-invalid, render-invalid, and over-cap packages. Creates no directories
    and never calls ``_install_dir()``.
    """
    workspace = workspace.expanduser().resolve()
    if harness not in _USER_PROFILE_HARNESSES:
        raise ValueError(f"unknown harness adapter: {harness}")
    if minimum_trust not in TRUST_LEVELS:
        raise ValueError(f"unknown skill trust level: {minimum_trust}")
    packages: list[UserProfileSkillPackage] = []
    for registry_row in _iter_registry(workspace):
        package = _user_profile_package(
            workspace=workspace,
            harness=harness,
            registry_row=registry_row,
            minimum_trust=minimum_trust,
        )
        if package is not None:
            packages.append(package)
    return tuple(sorted(packages, key=lambda package: package.skill_id))


def _sync_file_entries(root: Path) -> dict[str, tuple[bytes, int]]:
    if not root.is_dir():
        return {}
    return {
        path.relative_to(root).as_posix(): (path.read_bytes(), path.stat().st_mode & 0o777)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _sync_rendered_files(
    snapshot_files: dict[tuple[str, ...], bytes], metadata: dict[str, Any], skill_id: str, harness: str
) -> dict[str, tuple[bytes, int]]:
    """Build the desired install bundle from an already-collected snapshot.

    Every byte comes from the snapshot; the registry source is never re-read
    by pathname after validation.
    """
    files = {Path(*parts).as_posix(): (data, 0o644) for parts, data in sorted(snapshot_files.items())}
    skill = snapshot_files.get(("SKILL.md",))
    if skill is not None:
        rendered = _render_skill_text_for_harness(skill.decode("utf-8", errors="replace"), metadata, skill_id, harness)
        files["SKILL.md"] = (rendered.encode("utf-8"), 0o644)
    return files


def _sync_files_fingerprint(files: dict[str, tuple[bytes, int]]) -> str:
    digest = hashlib.sha256()
    for relative, (content, _mode) in sorted(files.items()):
        if Path(relative).name in {".DS_Store", "skill.json"}:
            continue
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    return digest.hexdigest()


def _sync_file_mutations(
    *,
    root: Path,
    before: dict[str, tuple[bytes, int]],
    after: dict[str, tuple[bytes, int]],
    display_prefix: str,
) -> list[projection.MutationSpec]:
    mutations: list[projection.MutationSpec] = []
    for relative in sorted(set(before) | set(after)):
        old = before.get(relative)
        new = after.get(relative)
        if old == new:
            continue
        destination = root / relative
        mutation_type = "create" if old is None else "remove" if new is None else "replace"
        mutations.append(
            projection.mutation(
                destination=destination,
                display_path=f"{display_prefix}/{relative}",
                mutation=cast(Literal["create", "replace", "remove"], mutation_type),
                expected_before=projection.content_digest(old[0] if old else None),
                desired_after=projection.content_digest(new[0] if new else None),
                staged_bytes=new[0] if new else None,
                mode=new[1] if new else None,
            )
        )
    return mutations


def _sync_single_file_mutation(path: Path, before: bytes | None, after: bytes | None) -> projection.MutationSpec | None:
    if before == after:
        return None
    mutation_type = "create" if before is None else "remove" if after is None else "replace"
    return projection.mutation(
        destination=path,
        display_path=projection.safe_display_path(path, target=path.parent),
        mutation=cast(Literal["create", "replace", "remove"], mutation_type),
        expected_before=projection.content_digest(before),
        desired_after=projection.content_digest(after),
        staged_bytes=after,
    )


def _sync_recovery_records(workspace: Path) -> list[dict[str, str]]:
    return [
        {
            "operation_id": record.operation_id,
            "status": record.status,
            "recovery_command": f"brigade projection recover {record.operation_id}",
        }
        for record in projection.unfinished_operations(workspace)
        if record.operation_id.startswith("skills-")
    ]


def _sync_projection_plan(
    *, workspace: Path, items: list[dict[str, Any]]
) -> tuple[projection.Plan | None, list[dict[str, Any]], str | None]:
    """Plan sync mutations from one anchored snapshot per registry entry.

    Each entry's bytes are collected through the held state-root descriptor
    before linting; rendering, fingerprints, receipts, and every mutation
    derive only from those collected bytes. History and prior receipts are
    read through ``_read_state_file_bytes``, so a symlinked state file refuses
    the plan instead of being followed.
    """
    mutations: list[projection.MutationSpec] = []
    history_path = _install_history_path(workspace)
    history_lines: list[bytes] = []
    source_rows: list[dict[str, Any]] = []
    with _held_state_root(workspace) as state_anchor:
        history_before = _read_state_file_bytes(state_anchor, "skills", "installs", "history.jsonl")
        for item in items:
            skill_id = str(item["skill_id"])
            harness = str(item["harness"])
            # Collect exactly once, then validate the collected generation:
            # a source swapped after this point cannot change what is installed.
            snapshot_dirs, snapshot_files = _read_registry_entry_tree(state_anchor, skill_id)
            with tempfile.TemporaryDirectory(prefix="brigade-sync-lint-") as staging:
                staged_dir = Path(staging) / skill_id
                _write_snapshot_tree(snapshot_dirs, snapshot_files, staged_dir)
                lint = _lint_payload(Path(staging), str(staged_dir))
            _finalize_staged_registry_lint(lint, workspace, _skill_path(workspace, skill_id), staged_dir)
            if not lint.get("valid"):
                return None, items, f"skill lint failed during sync planning: {skill_id}"
            metadata = raw_metadata if isinstance((raw_metadata := lint.get("metadata")), dict) else {}
            # Every eligibility gate repeats on THIS snapshot's generation:
            # planning evaluated an earlier registry read, so a generation
            # swapped in between must be re-checked here, never installed on
            # the strength of the plan-phase decision.
            trust_score = raw_trust_score if isinstance((raw_trust_score := lint.get("trust_score")), dict) else {}
            actual_trust = str(trust_score.get("trust_level") or "unreviewed")
            minimum_trust = str(item.get("minimum_trust") or "unreviewed")
            supported = metadata.get("supported_harnesses")
            supported_harnesses = set(supported) if isinstance(supported, list) else set()
            enabled = metadata.get("enabled", True)
            ineligible_reason: str | None = None
            if enabled is False:
                ineligible_reason = "skill is disabled by registry metadata"
            elif not _trust_at_least(actual_trust, minimum_trust):
                ineligible_reason = f"trust level {actual_trust} is below {minimum_trust}"
            elif supported_harnesses and harness not in supported_harnesses:
                ineligible_reason = f"harness {harness} is not supported by registry metadata"
            if ineligible_reason is not None:
                item.update(state="excluded", action="none", result="excluded", reason=ineligible_reason)
                item["receipt"] = None
                continue
            desired_files = _sync_rendered_files(snapshot_files, metadata, skill_id, harness)
            installed_dir = _install_dir(workspace, harness, skill_id)
            installed_parts = _lexical_state_root_parts(workspace, installed_dir)
            if installed_parts is not None:
                # State-backed install destinations are inspected through the
                # same held anchor: rollback material derives from collected
                # bytes, never from a pathname walk that could follow a
                # planted symlink out of the state root. A destination holding
                # non-plain entries is refused for this run instead of being
                # written over or around.
                if not _anchor_subtree_is_plain(state_anchor, *installed_parts):
                    item.update(
                        state="excluded",
                        action="none",
                        result="excluded",
                        reason="installed state copy contains non-plain entries; remove it before syncing",
                    )
                    item["receipt"] = None
                    continue
                _previous_dirs, previous_snapshot = _installed_tree_snapshot(
                    workspace, installed_dir, anchor=state_anchor
                )
                previous_files = {
                    Path(*parts).as_posix(): (data, 0o644) for parts, data in sorted(previous_snapshot.items())
                }
            else:
                previous_files = _sync_file_entries(installed_dir)
            mutations.extend(
                _sync_file_mutations(
                    root=installed_dir,
                    before=previous_files,
                    after=desired_files,
                    display_prefix=f"bundle:{skill_id}:{harness}",
                )
            )
            installed_at = _now()
            rollback_snapshot: str | None = None
            if previous_files:
                rollback_dir = (
                    _rollback_root(workspace, skill_id, harness)
                    / f"{installed_at[:19].replace(':', '').replace('-', '')}-{uuid.uuid4().hex[:8]}"
                )
                rollback_snapshot = str(rollback_dir)
                mutations.extend(
                    _sync_file_mutations(
                        root=rollback_dir,
                        before={},
                        after=previous_files,
                        display_prefix=f"rollback:{skill_id}:{harness}",
                    )
                )
            source = raw_source if isinstance((raw_source := lint.get("source")), dict) else {}
            render = _renderer_contract(workspace, harness)
            skill_content = desired_files.get("SKILL.md", (b"", 0))[0].decode("utf-8", errors="replace")
            receipt_path = _installs_root(workspace) / f"{skill_id}-{harness}.json"
            receipt_before = _read_state_file_bytes(state_anchor, "skills", "installs", f"{skill_id}-{harness}.json")
            previous_receipt: dict[str, Any] = {}
            if receipt_before is not None:
                try:
                    parsed = json.loads(receipt_before.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    parsed = None
                if isinstance(parsed, dict):
                    previous_receipt = parsed
            receipt = {
                "schema_version": RECEIPT_SCHEMA_VERSION,
                "workspace": str(workspace),
                "receipt_id": f"{installed_at[:19].replace(':', '').replace('-', '')}-{skill_id}-{harness}",
                "skill_id": skill_id,
                "target": harness,
                "installed_dir": str(installed_dir),
                "installed_at": installed_at,
                "version": str(metadata.get("version") or "0.1.0"),
                "source_path": str(metadata.get("source") or _skill_path(workspace, skill_id)),
                "source": source,
                "render": {**render, "fingerprint": _text_fingerprint(skill_content)},
                "installed": {
                    "bundle_fingerprint": _sync_files_fingerprint(desired_files),
                    "skill_fingerprint": _text_fingerprint(skill_content),
                    "metadata_fingerprint": _text_fingerprint(
                        desired_files.get("skill.json", (b"", 0))[0].decode("utf-8", errors="replace")
                    ),
                },
                "fingerprint": lint.get("fingerprint"),
                "source_fingerprint": lint.get("fingerprint"),
                "render_fingerprint": _text_fingerprint(skill_content),
                "installed_fingerprint": _sync_files_fingerprint(desired_files),
                "format": _adapter_map(workspace)[harness].get("format"),
                "rollback_snapshot": rollback_snapshot,
                "rollback_snapshot_fingerprint": (
                    _files_fingerprint(
                        {tuple(Path(relative).parts): data for relative, (data, _mode) in previous_files.items()}
                    )
                    if rollback_snapshot
                    else None
                ),
                "previous_receipt": previous_receipt if previous_receipt else None,
                "trust_score": lint.get("trust_score"),
                "changelog": lint.get("changelog"),
            }
            receipt_after = (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode("utf-8")
            receipt_mutation = _sync_single_file_mutation(receipt_path, receipt_before, receipt_after)
            if receipt_mutation is not None:
                mutations.append(receipt_mutation)
            item["receipt"] = {**receipt, "receipt_path": str(receipt_path)}
            history_lines.append((json.dumps(item["receipt"], sort_keys=True) + "\n").encode("utf-8"))
            source_rows.append({"skill_id": skill_id, "harness": harness, "fingerprint": lint.get("fingerprint")})
    history_after = (history_before or b"") + b"".join(history_lines)
    history_mutation = _sync_single_file_mutation(history_path, history_before, history_after)
    if history_mutation is not None:
        mutations.append(history_mutation)
    if not mutations:
        return None, items, None
    source_fingerprint = hashlib.sha256(json.dumps(source_rows, sort_keys=True).encode("utf-8")).hexdigest()
    return (
        projection.build_plan(
            operation_id=f"skills-{uuid.uuid4().hex}",
            projector="skills",
            source_fingerprint=source_fingerprint,
            mutations=mutations,
            target=workspace,
        ),
        items,
        None,
    )


def sync(
    *,
    workspace: Path,
    harness: str,
    trust: str = "workspace",
    write: bool = False,
    json_output: bool = False,
) -> int:
    """Reconcile every eligible registry skill against one or all harness targets."""
    workspace = workspace.expanduser().resolve()
    items, error = _sync_plan(workspace=workspace, harness=harness, trust=trust)
    if error is not None:
        print(f"error: {error}", file=sys.stderr)
        return 2

    applied = {"installed": 0, "updated": 0, "restored": 0, "failed": 0}
    projection_view: dict[str, Any] | None = None
    if write:
        mutable_items = [item for item in items if item["action"] in {"install", "update"}]
        if mutable_items:
            recovery = _sync_recovery_records(workspace)
            if recovery:
                blocked = {"workspace": str(workspace), "recovery": recovery, "terminal_state": "recovery-required"}
                print(
                    json.dumps(blocked, indent=2, sort_keys=True)
                    if json_output
                    else f"error: recovery required: {recovery[0]['recovery_command']}"
                )
                return 1
            try:
                planned, mutable_items, error = _sync_projection_plan(workspace=workspace, items=mutable_items)
            except (OSError, projection.PlanError, ValueError, SkillsStatePathError) as exc:
                planned, error = None, str(exc)
            if error is not None:
                print(f"error: {error}", file=sys.stderr)
                return 1
            if planned is not None:
                try:
                    receipt = projection.execute(planned, target=workspace)
                except projection.ProjectionError as exc:
                    print(f"error: {exc}", file=sys.stderr)
                    return 1
                projection_view = receipt.to_dict()
                if receipt.terminal_state == "committed":
                    for item in mutable_items:
                        if item["action"] not in {"install", "update"}:
                            # The projection pass re-gated eligibility on its
                            # own snapshot and excluded this item.
                            continue
                        result = "installed" if item["action"] == "install" else "updated"
                        item["result"] = result
                        applied[result] += 1
                else:
                    state = "restored" if receipt.terminal_state == "restored" else "recovery-required"
                    for item in mutable_items:
                        item["result"] = state
                    applied["restored"] = len(mutable_items) if state == "restored" else 0
                    applied["failed"] = 1

    counts = {
        state: sum(item["state"] == state for item in items)
        for state in ("current", "missing", "changed", "blocked", "excluded")
    }
    payload: dict[str, Any] = {
        "schema_version": 1,
        "workspace": str(workspace),
        "target": harness,
        "minimum_trust": trust,
        "write": write,
        "registry_count": len({str(item["skill_id"]) for item in items}),
        "item_count": len(items),
        "counts": counts,
        "applied": applied,
        "items": items,
        "projection": projection_view,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        mode = "write" if write else "dry-run"
        print(f"skills sync: {workspace}")
        print(f"mode: {mode}")
        print(f"target: {harness}")
        print(f"minimum_trust: {trust}")
        if projection_view is not None:
            print(f"projection operation: {projection_view['operation_id']}")
            print(f"projection status: {projection_view['terminal_state']}")
        print(
            " ".join(f"{state}={counts[state]}" for state in ("current", "missing", "changed", "blocked", "excluded"))
        )
        for item in items:
            detail_value = item.get("error") or item.get("reason")
            detail = f" ({detail_value})" if detail_value else ""
            print(
                f"- {item['skill_id']} [{item['harness']}] {item['state']} "
                f"action={item['action']} result={item['result']}{detail}"
            )
    return (
        1
        if applied["failed"] or (projection_view is not None and projection_view["terminal_state"] != "committed")
        else 0
    )


def uninstall(*, workspace: Path, skill: str, harness: str, json_output: bool = False) -> int:
    """Remove an installed skill from one or all harnesses, the inverse of install."""
    workspace = workspace.expanduser().resolve()
    install_targets = _install_targets(workspace)
    if harness not in (*install_targets, "all"):
        print(f"error: unknown skill install target: {harness}", file=sys.stderr)
        return 2
    skill_id = _slug(skill)
    targets = install_targets if harness == "all" else (harness,)
    removed: list[dict[str, Any]] = []
    try:
        state_anchor = _hold_state_root(workspace)
    except SkillsStatePathError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        for install_target in targets:
            state_anchor.revalidate()
            try:
                dest = _install_dir(workspace, install_target, skill_id)
            except ValueError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
            receipt_name = f"{skill_id}-{install_target}.json"
            install_rel = str(_adapter_map(workspace)[install_target]["install_path"]).format(skill_id=skill_id)
            under_state_root = install_rel.startswith(".brigade/")
            state_parts = tuple(part for part in install_rel.split("/") if part)[1:]
            # Refuse redirected state paths before anything is deleted.
            _require_plain_state_dirs(state_anchor, "skills", "installs")
            has_receipt = _state_file_exists(state_anchor, "skills", "installs", receipt_name)
            if not dest.exists() and not has_receipt:
                continue
            if dest.exists():
                if under_state_root:
                    _remove_tree_in_anchor(state_anchor, *state_parts)
                else:
                    shutil.rmtree(dest)
            if has_receipt:
                _unlink_in_anchor(state_anchor, "skills", "installs", receipt_name)
            _append_state_line(
                state_anchor,
                "skills",
                "installs",
                "history.jsonl",
                data=(
                    json.dumps(
                        {
                            "action": "uninstall",
                            "skill_id": skill_id,
                            "harness": install_target,
                            "workspace": str(workspace),
                            "uninstalled_at": _now(),
                        },
                        sort_keys=True,
                    )
                    + "\n"
                ).encode("utf-8"),
            )
            removed.append({"harness": install_target, "path": str(dest)})
    except SkillsStatePathError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        state_anchor.close()
    if not removed:
        print(f"error: skill not installed: {skill} ({harness})", file=sys.stderr)
        return 1
    payload = {"workspace": str(workspace), "skill_id": skill_id, "uninstalled": removed, "count": len(removed)}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"skills uninstall: {skill_id}")
    for record in removed:
        print(f"removed: {record['harness']} -> {record['path']}")
    return 0


def rollback(*, workspace: Path, skill: str, harness: str, json_output: bool = False) -> int:
    workspace = workspace.expanduser().resolve()
    skill_id = _slug(skill)
    if harness not in _install_targets(workspace):
        print(f"error: unknown skill install target: {harness}", file=sys.stderr)
        return 2
    receipt_name = f"{skill_id}-{harness}.json"
    rollback_receipt_name = f"{skill_id}-{harness}-rollback.json"
    rollback_receipt_path = workspace / ".brigade" / "skills" / "installs" / rollback_receipt_name
    try:
        with _held_state_root(workspace) as state_anchor:
            # Refuse redirected installs and rollback components before any
            # receipt is read or anything is copied, written, or deleted.
            _require_plain_state_dirs(state_anchor, "skills", "installs")
            receipt_bytes = _read_state_file_bytes(state_anchor, "skills", "installs", receipt_name)
            current_receipt: dict[str, Any] = {}
            if receipt_bytes is not None:
                try:
                    parsed = json.loads(receipt_bytes.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    parsed = None
                if isinstance(parsed, dict):
                    current_receipt = parsed
            if not _valid_receipt_contract(current_receipt, skill_id=skill_id, harness=harness):
                print(f"error: invalid rollback receipt for {skill_id} on {harness}", file=sys.stderr)
                return 1
            snapshot_value = current_receipt.get("rollback_snapshot")
            if not isinstance(snapshot_value, str) or not snapshot_value:
                print(f"error: no receipt-bound rollback snapshot for {skill_id} on {harness}", file=sys.stderr)
                return 1
            rollback_base = _rollback_root(workspace, skill_id, harness)
            try:
                relative_snapshot = Path(snapshot_value).relative_to(rollback_base)
            except ValueError:
                relative_snapshot = Path()
            if len(relative_snapshot.parts) != 1 or relative_snapshot.parts[0] in {"", ".", ".."}:
                print(f"error: invalid receipt-bound rollback snapshot for {skill_id} on {harness}", file=sys.stderr)
                return 1
            previous_receipt_value = current_receipt.get("previous_receipt")
            if previous_receipt_value is not None and (
                not isinstance(previous_receipt_value, dict)
                or not _valid_receipt_contract(previous_receipt_value, skill_id=skill_id, harness=harness)
            ):
                print(f"error: invalid previous rollback receipt for {skill_id} on {harness}", file=sys.stderr)
                return 1
            previous_receipt = previous_receipt_value or {}
            # The snapshot must be a plain directory directly under the held
            # rollback component; symlinked components refuse the whole rollback.
            snapshot_rel = ("skills", "rollback", skill_id, harness, relative_snapshot.parts[0])
            if _state_entry_kind(state_anchor, *snapshot_rel) != "dir":
                print(f"error: invalid receipt-bound rollback snapshot for {skill_id} on {harness}", file=sys.stderr)
                return 1
            dest = _install_dir(workspace, harness, skill_id)
            install_rel = str(_adapter_map(workspace)[harness]["install_path"]).format(skill_id=skill_id)
            under_state_root = install_rel.startswith(".brigade/")
            state_parts = tuple(part for part in install_rel.split("/") if part)[1:]
            dirs, files = _read_tree_from_anchor(state_anchor, *snapshot_rel)
            # The snapshot lives in attacker-influenced state: treat it as
            # untrusted input. It is only restored when its bytes match the
            # fingerprint recorded for this installation at snapshot-capture
            # time and when the same lint and rendered-text validation a fresh
            # install runs accepts those bytes. Unbound legacy snapshots carry
            # no recorded fingerprint and are refused outright.
            recorded_snapshot_fingerprint = current_receipt.get("rollback_snapshot_fingerprint")
            if not isinstance(recorded_snapshot_fingerprint, str) or not recorded_snapshot_fingerprint:
                print(
                    f"error: rollback snapshot carries no recorded fingerprint for {skill_id} on {harness}",
                    file=sys.stderr,
                )
                return 2
            snapshot_skill_md = files.get(("SKILL.md",))
            snapshot_text = snapshot_skill_md.decode("utf-8", errors="replace") if snapshot_skill_md is not None else ""
            if _files_fingerprint(files) != recorded_snapshot_fingerprint:
                print(
                    f"error: rollback snapshot does not match the fingerprint recorded for {skill_id} on {harness}",
                    file=sys.stderr,
                )
                return 2
            with tempfile.TemporaryDirectory(prefix="brigade-rollback-lint-") as staging:
                staged = Path(staging) / "snapshot"
                _write_snapshot_tree(dirs, files, staged)
                snapshot_lint = _lint_payload(Path(staging), str(staged))
            render_refusals = _rendered_skill_validation(snapshot_text, harness)
            if render_refusals or not snapshot_lint.get("valid"):
                problems = [*render_refusals, *(snapshot_lint.get("errors") or [])]
                detail = "; ".join(problems[:5])
                print(
                    f"error: rollback snapshot failed validation for {skill_id} on {harness}: {detail}",
                    file=sys.stderr,
                )
                return 2
            payload = {
                "workspace": str(workspace),
                "skill_id": skill_id,
                "target": harness,
                "snapshot": snapshot_value,
                "installed_dir": str(dest),
                "rolled_back_at": _now(),
                "restored_receipt": bool(previous_receipt),
                "snapshot_consumed": True,
            }
            if under_state_root:
                _remove_tree_in_anchor(state_anchor, *state_parts)
                _write_collected_tree_into_anchor(dirs, files, state_anchor, *state_parts)
            else:
                if dest.exists():
                    shutil.rmtree(dest)
                dest.mkdir(parents=True, exist_ok=True)
                for parts in dirs:
                    (dest.joinpath(*parts)).mkdir(parents=True, exist_ok=True)
                for parts, data in files.items():
                    (dest.joinpath(*parts)).write_bytes(data)
            if previous_receipt:
                _write_state_file(state_anchor, "skills", "installs", receipt_name, data=_json_bytes(previous_receipt))
            else:
                _unlink_in_anchor(state_anchor, "skills", "installs", receipt_name)
            _write_state_file(state_anchor, "skills", "installs", rollback_receipt_name, data=_json_bytes(payload))
            _remove_tree_in_anchor(state_anchor, *snapshot_rel)
    except SkillsStatePathError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    payload["receipt_path"] = str(rollback_receipt_path)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"skill_rollback: {skill_id}")
    print(f"target: {harness}")
    print(f"installed_dir: {dest}")
    return 0


def history(
    *, target: Path, skill: str | None = None, harness: str | None = None, json_output: bool = False, limit: int = 20
) -> int:
    target = target.expanduser().resolve()
    rows = _install_history(target, skill_id=skill, harness=harness)[:limit]
    payload = {
        "target": str(target),
        "skill_id": _slug(skill) if skill else None,
        "harness": harness,
        "count": len(rows),
        "history": rows,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"skill install history: {target}")
    if skill:
        print(f"skill_id: {_slug(skill)}")
    if harness:
        print(f"harness: {harness}")
    for row in rows:
        print(f"- {row.get('installed_at')} {row.get('skill_id')} {row.get('target')} version={row.get('version')}")
    if not rows:
        print("no install history")
    return 0


def diff(*, target: Path, skill: str, harness: str, against: str = "bundled", json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    baseline_skill = _resolve_diff_baseline(target, skill, against=against)
    lint_payload = _lint_payload(target, baseline_skill)
    if not lint_payload["valid"]:
        if json_output:
            print(
                json.dumps(
                    {"target": str(target), "skill_id": skill, "valid": False, "lint": lint_payload},
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            print(f"error: skill lint failed: {skill}", file=sys.stderr)
        return 1
    skill_id = _slug(str(lint_payload.get("skill_id") or skill))
    if harness not in _install_targets(target):
        print(f"error: unknown skill install target: {harness}", file=sys.stderr)
        return 2
    source_dir = Path(str(lint_payload["skill_dir"]))
    metadata = raw_metadata if isinstance((raw_metadata := lint_payload.get("metadata")), dict) else {}
    if baseline_skill.startswith("registry:"):
        # The registry side of the diff is served through the state-root
        # anchor, never re-read by pathname after the lint.
        source_text = _anchored_registry_text(target, _slug(baseline_skill.removeprefix("registry:")), "SKILL.md")
        source_text = source_text or ""
    else:
        source_text = _skill_md_path(source_dir).read_text(encoding="utf-8", errors="replace")
    rendered = _render_skill_text_for_harness(source_text, metadata, skill_id, harness)
    installed_dir = _install_dir(target, harness, skill_id)
    # The installed side is inspected through one anchored snapshot; a
    # symlinked SKILL.md in a state-backed install target contributes
    # absence, never outside file content.
    _snapshot_dirs, snapshot_files = _installed_tree_snapshot(target, installed_dir)
    installed_raw = snapshot_files.get(("SKILL.md",))
    installed_present = installed_raw is not None
    installed_text = installed_raw.decode("utf-8", errors="replace") if installed_raw is not None else ""
    installed_skill = _skill_md_path(installed_dir)
    source = raw_source if isinstance((raw_source := lint_payload.get("source")), dict) else {}
    diff_lines = list(
        difflib.unified_diff(
            installed_text.splitlines(),
            rendered.splitlines(),
            fromfile=str(installed_skill),
            tofile=f"{source.get('identity') or f'resolved:{skill_id}'}#{harness}",
            lineterm="",
        )
    )
    drift = _drift_payload(
        target=target,
        skill_id=skill_id,
        harness=harness,
        lint_payload=lint_payload,
        rendered=rendered,
        installed_dir=installed_dir,
    )
    payload = {
        "target": str(target),
        "skill_id": skill_id,
        "harness": harness,
        "against": against,
        "baseline_skill": baseline_skill,
        "installed": installed_present,
        "installed_path": str(installed_skill),
        "changed": bool(diff_lines),
        "diff": diff_lines,
        "drift": {
            key: drift[key] for key in ("overall", "source", "render", "local_edit", "receipt_known", "content_changed")
        },
        "source": source,
        "render": drift["current_render"],
        "source_fingerprint": lint_payload.get("fingerprint"),
        "render_fingerprint": _text_fingerprint(rendered),
        "installed_fingerprint": drift["installed_skill_fingerprint"],
        "installed_skill_fingerprint": drift["installed_skill_fingerprint"],
        "installed_bundle_fingerprint": drift["installed_bundle_fingerprint"],
        "receipt": drift["receipt"],
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    if diff_lines:
        print("\n".join(diff_lines))
    else:
        print("no diff")
    return 0


def _skill_pack_payload(target: Path) -> dict[str, Any]:
    skills: list[dict[str, Any]] = []
    for row in _iter_registry(target):
        skill_dir = Path(str(row["skill_dir"]))
        metadata = row["metadata"]
        skill_id = _slug(str(metadata.get("id") or skill_dir.name))
        lint_payload = _lint_payload(target, f"registry:{skill_id}")
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
    pack_dir = _skill_packs_root(target) / pack_id
    try:
        with _held_state_root(target) as state_anchor:
            payload = _skill_pack_payload(target)
            payload.update({"pack_id": pack_id, "status": "built"})
            for row in _iter_registry(target):
                skill_dir = Path(str(row["skill_dir"]))
                skill_id = _slug(str(row["metadata"].get("id") or skill_dir.name))
                snapshot_dirs, snapshot_files = _read_registry_entry_tree(state_anchor, skill_id)
                _write_collected_tree_into_anchor(
                    snapshot_dirs, snapshot_files, state_anchor, "skills", "packs", pack_id, "skills", skill_id
                )
            _write_state_file(state_anchor, "skills", "packs", pack_id, "skill-pack.json", data=_json_bytes(payload))
            _write_state_file(
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
    except SkillsStatePathError as exc:
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
        with _held_state_root(target) as anchor:
            for sub, archived in ((("skills", "packs"), False), (("skills", "packs-archive"), True)):
                names = _plain_subdirs_under_anchor(anchor, *sub)
                if not names:
                    continue
                for name in names:
                    payload = _anchored_pack_manifest_at(anchor, *sub, name)
                    if payload:
                        payload.setdefault("path", str(_pack_display(target, *sub, name)))
                        payload.setdefault("archived", archived)
                        packs.append(payload)
    except SkillsStatePathError:
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
    if len(parts) >= 3 and parts[:2] == ("skills", "registry") and _slug(parts[2]) == parts[2]:
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
            payload = _read_json(path / "skill-pack.json")
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


def _anchored_pack_manifest_at(anchor: _StateRootAnchor, *relative: str) -> dict[str, Any]:
    """Read one pack manifest from the anchored state root (never by external path)."""
    raw = _read_state_file_bytes(anchor, *relative, "skill-pack.json")
    if raw is None:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _anchored_pack_manifest(anchor: _StateRootAnchor, name: str) -> dict[str, Any]:
    return _anchored_pack_manifest_at(anchor, "skills", "packs", name)


def pack_archive(*, target: Path, pack_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    archive_root = _skill_packs_archive_root(target)
    try:
        with _held_state_root(target) as state_anchor:
            if not _HAS_DESCRIPTOR_ANCHOR:
                raise _fallback_mutation_refusal("moves", archive_root)
            # The archived entry is always a validated plain directory
            # directly under the anchored packs root; a manifest-supplied
            # physical path is never honoured as the move source.
            candidates: list[tuple[str, dict[str, Any]]] = []
            try:
                packs_fd, packs_opened = _anchor_open_chain(state_anchor, "skills", "packs")
            except SkillsStatePathError:
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
            archive_fd, archive_opened = _anchor_open_chain(state_anchor, "skills", "packs-archive", create=True)
            try:
                packs_fd, packs_opened = _anchor_open_chain(state_anchor, "skills", "packs")
                try:
                    try:
                        os.stat(pack_name, dir_fd=archive_fd, follow_symlinks=False)
                    except FileNotFoundError:
                        pass
                    else:
                        print(f"error: archived skill pack already exists: {destination}", file=sys.stderr)
                        return 2
                    with _anchored_fs_errors(str(destination)):
                        os.rename(pack_name, pack_name, src_dir_fd=packs_fd, dst_dir_fd=archive_fd)
                finally:
                    for fd in reversed(packs_opened):
                        os.close(fd)
            finally:
                for fd in reversed(archive_opened):
                    os.close(fd)
    except SkillsStatePathError as exc:
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
    manifest = _read_json(pack_dir / "skill-pack.json")
    skills_dir = pack_dir / "skills"
    if not manifest or not skills_dir.is_dir():
        print(f"error: not a skill pack: {display_dir}", file=sys.stderr)
        return 2
    imported: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    errors: list[str] = []
    skill_paths = sorted(path for path in skills_dir.iterdir() if path.is_dir())
    for skill_path in skill_paths:
        metadata = _read_json(skill_path / "skill.json")
        skill_id = _slug(str(metadata.get("id") or skill_path.name))
        if _registry_entry_present(target, skill_id) and not force:
            conflicts.append(
                {"skill_id": skill_id, "existing": str(_skill_path(target, skill_id)), "source": str(skill_path)}
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
        metadata = _read_json(skill_path / "skill.json")
        skill_id = _slug(str(metadata.get("id") or skill_path.name))
        # Provenance keeps the displayed pack location; imports from an
        # anchored snapshot staging copy never record the staging path.
        row, error, rc = _registry_import_payload(
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
            with _held_state_root(target) as anchor:
                for sub in (("skills", "packs"), ("skills", "packs-archive")):
                    names = _plain_subdirs_under_anchor(anchor, *sub)
                    if names and pack_name in names:
                        snapshot = _read_tree_from_anchor(anchor, *sub, pack_name)
                        break
        except SkillsStatePathError:
            snapshot = None
        if snapshot is None:
            print(f"error: skill pack not found: {display_pack}", file=sys.stderr)
            return 2
        with tempfile.TemporaryDirectory(prefix="brigade-pack-import-") as staging:
            staged_pack = Path(staging) / pack_name
            _write_snapshot_tree(snapshot[0], snapshot[1], staged_pack)
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


def _mcp_contract_payload(target: Path) -> dict[str, Any]:
    resources = []
    for row in _iter_registry(target):
        metadata = row["metadata"]
        skill_id = str(metadata.get("id") or Path(row["skill_dir"]).name)
        lint_payload = _lint_payload(target, f"registry:{skill_id}")
        compatibility_payload = {
            "skill": f"skill://registry/{skill_id}/compatibility.json",
            "summary": "Use brigade skills compatibility for the full local view.",
        }
        resources.append(
            {
                "skill_id": skill_id,
                "skill": f"skill://registry/{skill_id}/SKILL.md",
                "metadata": f"skill://registry/{skill_id}/skill.json",
                "changelog": f"skill://registry/{skill_id}/CHANGELOG.md"
                if lint_payload.get("changelog", {}).get("present")
                else None,
                "compatibility": compatibility_payload["skill"],
                "history": f"skill://registry/{skill_id}/history.json",
                "fingerprint": metadata.get("fingerprint"),
                "version": metadata.get("version"),
                "trust_score": lint_payload.get("trust_score"),
                "read_only": True,
            }
        )
    payload = {
        "target": str(target),
        "status": "ready",
        "read_only": True,
        "resources": [
            "skill://registry/{skill_id}/SKILL.md",
            "skill://registry/{skill_id}/skill.json",
            "skill://registry/{skill_id}/CHANGELOG.md",
            "skill://registry/{skill_id}/compatibility.json",
            "skill://registry/{skill_id}/history.json",
        ],
        "registered_resources": resources,
        "resource_count": len(resources),
        "tools": [
            "search_skills",
            "get_skill",
            "get_skill_metadata",
            "get_skill_changelog",
            "get_skill_compatibility",
            "get_skill_history",
            "lint_skill",
        ],
        "blocked_tools": ["install_skill", "publish_skill", "fork_skill"],
        "detail": "Local registry resources are available for a read-only MCP adapter; this command reports the contract and does not start a long-running server.",
    }
    return payload


def _mcp_resource_items(target: Path) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for resource in _mcp_contract_payload(target)["registered_resources"]:
        skill_id = str(resource["skill_id"])
        for key, mime_type in (
            ("skill", "text/markdown"),
            ("metadata", "application/json"),
            ("changelog", "text/markdown"),
            ("compatibility", "application/json"),
            ("history", "application/json"),
        ):
            uri = resource.get(key)
            if not uri:
                continue
            items.append({"uri": uri, "name": f"{skill_id} {key}", "mimeType": mime_type})
    return items


def _mcp_read_resource(target: Path, uri: str) -> tuple[str, str] | tuple[None, None]:
    prefix = "skill://registry/"
    if not uri.startswith(prefix):
        return None, None
    remainder = uri[len(prefix) :]
    if "/" not in remainder:
        return None, None
    skill_id, name = remainder.split("/", 1)
    skill_id = _slug(skill_id)
    # Every registry read below goes through the held state-root anchor; a
    # refused or absent entry yields (None, None) instead of outside content.
    try:
        with _held_state_root(target) as anchor:
            if name == "SKILL.md":
                raw = _read_state_file_bytes(anchor, "skills", "registry", skill_id, "SKILL.md")
                return (raw.decode("utf-8", errors="replace"), "text/markdown") if raw is not None else (None, None)
            if name == "skill.json":
                metadata = _anchored_registry_metadata(anchor, skill_id)
                metadata.setdefault("id", skill_id)
                return json.dumps(metadata, indent=2, sort_keys=True) + "\n", "application/json"
            if name == "CHANGELOG.md":
                metadata = _anchored_registry_metadata(anchor, skill_id)
                text = _anchored_entry_changelog_text(anchor, "skills", "registry", skill_id, metadata=metadata)
                return (text, "text/markdown") if text is not None else (None, None)
            if name == "compatibility.json":
                return (
                    json.dumps(_compatibility_payload(target, f"registry:{skill_id}"), indent=2, sort_keys=True) + "\n",
                    "application/json",
                )
            if name == "history.json":
                payload = {"skill_id": skill_id, "history": _install_history(target, skill_id=skill_id)}
                return json.dumps(payload, indent=2, sort_keys=True) + "\n", "application/json"
    except SkillsStatePathError:
        return None, None
    return None, None


def _mcp_tool_specs() -> list[dict[str, Any]]:
    schema_skill = {
        "type": "object",
        "properties": {"skill_id": {"type": "string"}},
        "required": ["skill_id"],
        "additionalProperties": False,
    }
    return [
        {
            "name": "search_skills",
            "description": "Search the local reviewed skill registry.",
            "inputSchema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
        },
        {"name": "get_skill", "description": "Read a skill SKILL.md file.", "inputSchema": schema_skill},
        {"name": "get_skill_metadata", "description": "Read skill.json metadata.", "inputSchema": schema_skill},
        {"name": "get_skill_changelog", "description": "Read skill changelog text.", "inputSchema": schema_skill},
        {
            "name": "get_skill_compatibility",
            "description": "Read skill compatibility JSON.",
            "inputSchema": schema_skill,
        },
        {"name": "get_skill_history", "description": "Read skill install history JSON.", "inputSchema": schema_skill},
        {"name": "lint_skill", "description": "Read skill lint JSON.", "inputSchema": schema_skill},
    ]


def _mcp_tool_call(target: Path, name: str, arguments: dict[str, Any]) -> tuple[object, bool]:
    if name == "search_skills":
        query = str(arguments.get("query") or "")
        terms = [term.casefold() for term in query.split() if term]
        matches = []
        for row in _iter_registry(target):
            metadata = row["metadata"]
            haystack = " ".join(str(metadata.get(key, "")) for key in ("id", "title", "description")).casefold()
            if not terms or all(term in haystack for term in terms):
                matches.append(metadata)
        return {"query": query, "count": len(matches), "skills": matches}, False
    skill_id = _slug(str(arguments.get("skill_id") or ""))
    if not skill_id:
        return {"error": "skill_id is required"}, True
    if name == "get_skill":
        text, _ = _mcp_read_resource(target, f"skill://registry/{skill_id}/SKILL.md")
        return text or "", text is None
    if name == "get_skill_metadata":
        try:
            with _held_state_root(target) as anchor:
                metadata = _anchored_registry_metadata(anchor, skill_id)
        except SkillsStatePathError:
            metadata = {}
        metadata.setdefault("id", skill_id)
        return metadata, False
    if name == "get_skill_changelog":
        text, _ = _mcp_read_resource(target, f"skill://registry/{skill_id}/CHANGELOG.md")
        return text or "", text is None
    if name == "get_skill_compatibility":
        return _compatibility_payload(target, f"registry:{skill_id}"), False
    if name == "get_skill_history":
        return {"skill_id": skill_id, "history": _install_history(target, skill_id=skill_id)}, False
    if name == "lint_skill":
        return _lint_payload(target, f"registry:{skill_id}"), False
    return {"error": f"unknown read-only skill tool: {name}"}, True


def _run_mcp_stdio(target: Path) -> int:
    return mcp_server.serve_stdio(
        server_name="brigade-skills-readonly",
        list_resources=lambda: _mcp_resource_items(target),
        read_resource=lambda uri: _mcp_read_resource(target, uri),
        list_tools=_mcp_tool_specs,
        call_tool=lambda name, arguments: _mcp_tool_call(target, name, arguments),
    )


def serve_mcp(*, target: Path, json_output: bool = False, stdio: bool = False) -> int:
    target = target.expanduser().resolve()
    if stdio:
        return _run_mcp_stdio(target)
    payload = _mcp_contract_payload(target)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print("skills MCP resources: ready read_only=true")
    print(
        "resources: skill://registry/{skill_id}/SKILL.md, skill://registry/{skill_id}/skill.json, skill://registry/{skill_id}/compatibility.json, skill://registry/{skill_id}/history.json"
    )
    print(
        "tools: search_skills, get_skill, get_skill_metadata, get_skill_changelog, get_skill_compatibility, get_skill_history, lint_skill"
    )
    print(f"registered_resources: {len(payload['registered_resources'])}")
    return 0


def publish(*, target: Path, skill: str, scope: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    scope_slug = _slug(scope)
    if not scope_slug:
        print(f"error: invalid publish scope: {scope}", file=sys.stderr)
        return 2
    lint_payload = _lint_payload(target, skill)
    if not lint_payload["valid"]:
        print(f"error: skill lint failed: {skill}", file=sys.stderr)
        return 1
    payload = {
        "target": str(target),
        "skill_id": lint_payload["skill_id"],
        "scope": scope,
        "status": "review-required",
        "fingerprint": lint_payload.get("fingerprint"),
        "created_at": _now(),
        "next": "Review provenance, compatibility, permissions, and rollback before sharing this skill.",
    }
    # The destination name is derived from validated slugs only; the write
    # itself goes through the held state-root anchor, so a symlinked
    # publish-proposals component refuses the command instead of redirecting
    # it outside the workspace.
    proposal_name = f"{_slug(str(lint_payload['skill_id']))}-{scope_slug}.json"
    out = target / ".brigade" / "skills" / "publish-proposals" / proposal_name
    try:
        with _held_state_root(target) as state_anchor:
            _require_plain_state_dirs(state_anchor, "skills", "publish-proposals")
            _write_state_file(state_anchor, "skills", "publish-proposals", proposal_name, data=_json_bytes(payload))
    except SkillsStatePathError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    payload["proposal_path"] = str(out)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"skill_publish_proposal: {payload['skill_id']}")
    print(f"scope: {scope}")
    print(f"status: {payload['status']}")
    print(f"proposal: {out}")
    return 0


def adapters_init(*, target: Path, force: bool = False, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    path = _adapters_config_path(target)
    # Existence is answered through the anchor, never by pathname probe.
    try:
        with _held_state_root(target) as anchor:
            config_exists = _state_file_exists(anchor, "skills", "adapters.json")
    except SkillsStatePathError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if config_exists and not force:
        print(f"error: skill adapter config already exists: {path}", file=sys.stderr)
        return 2
    payload = {
        "description": "Local skill harness adapter overlay. install_path is relative to the workspace and may use {skill_id}.",
        "adapters": {},
    }
    payload = {
        "description": "Local skill harness adapter overlay. install_path is relative to the workspace and may use {skill_id}.",
        "adapters": {},
    }
    try:
        with _held_state_root(target) as anchor:
            _write_state_file(anchor, "skills", "adapters.json", data=_json_bytes(payload))
    except SkillsStatePathError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    output = {"target": str(target), "path": str(path), "adapter_count": len(payload["adapters"])}
    if json_output:
        print(json.dumps(output, indent=2, sort_keys=True))
        return 0
    print(f"skill_adapters_config: {path}")
    print("next_command: brigade skills adapters list --include-planned")
    return 0


def adapters_list(*, target: Path = Path("."), include_planned: bool = False, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    adapter_map = _adapter_map(target)
    adapters = [
        {"id": adapter_id, **data}
        for adapter_id, data in adapter_map.items()
        if include_planned or data["status"] in {"built-in", "local"}
    ]
    payload = {
        "target": str(target),
        "config_path": str(_adapters_config_path(target)),
        "adapters": adapters,
        "count": len(adapters),
        "include_planned": include_planned,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print("skill adapters:")
    for item in adapters:
        print(f"- {item['id']} [{item['status']}] {item['format']} {item.get('install_path') or '(planned)'}")
    return 0


def adapters_show(*, target: Path = Path("."), adapter_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    adapter = _adapter_map(target).get(adapter_id)
    if adapter is None:
        print(f"error: skill adapter not found: {adapter_id}", file=sys.stderr)
        return 2
    payload = {"id": adapter_id, **adapter}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"skill adapter: {adapter_id}")
    print(f"status: {adapter['status']}")
    print(f"format: {adapter['format']}")
    print(f"install_path: {adapter.get('install_path') or '(planned)'}")
    return 0


def _compatibility_payload(target: Path, skill: str) -> dict[str, Any]:
    lint_payload = _lint_payload(target, skill)
    metadata = raw_metadata if isinstance((raw_metadata := lint_payload.get("metadata")), dict) else {}
    supported = raw_supported if isinstance((raw_supported := metadata.get("supported_harnesses")), list) else []
    skill_id = _slug(str(lint_payload.get("skill_id") or skill))
    current_version = str(metadata.get("version") or "0.1.0")
    source_text = ""
    skill_dir = Path(str(lint_payload.get("skill_dir") or ""))
    skill_md = _skill_md_path(skill_dir)
    if skill.startswith("registry:"):
        # Anchored registry read; a refused entry contributes no text.
        source_text = _anchored_registry_text(target, _slug(skill.removeprefix("registry:")), "SKILL.md") or ""
    elif skill_md.is_file():
        source_text = skill_md.read_text(encoding="utf-8", errors="replace")
    adapters = []
    for adapter_id, adapter in _adapter_map(target).items():
        install_path = adapter.get("install_path")
        installed = False
        installed_path = None
        if install_path:
            installed_dir = _install_dir(target, adapter_id, skill_id)
            installed_path = str(installed_dir)
            installed = _skill_md_path(installed_dir).is_file()
        supported_state = adapter_id in supported or not supported
        blockers: list[str] = []
        if adapter.get("status") == "planned":
            blockers.append("adapter planned")
        if not install_path:
            blockers.append("install_path missing")
        if not supported_state:
            blockers.append("skill metadata does not list this harness")
        rendered_errors: list[str] = []
        render_fingerprint: str | None = None
        rendered: str | None = None
        if lint_payload.get("valid") and adapter.get("status") != "planned" and install_path:
            rendered = _render_skill_text_for_harness(source_text, metadata, skill_id, adapter_id)
            render_fingerprint = _text_fingerprint(rendered)
            rendered_errors = _rendered_skill_validation(rendered, adapter_id)
            blockers.extend(rendered_errors)
        drift = (
            _drift_payload(
                target=target,
                skill_id=skill_id,
                harness=adapter_id,
                lint_payload=lint_payload,
                rendered=rendered,
                installed_dir=Path(str(installed_path)),
            )
            if rendered is not None and installed_path is not None
            else None
        )
        latest_receipt = drift.get("receipt") if drift else _latest_install_receipt(target, skill_id, adapter_id)
        latest_receipt = latest_receipt if isinstance(latest_receipt, dict) else {}
        if not _valid_receipt_contract(latest_receipt, skill_id=skill_id, harness=adapter_id):
            latest_receipt = {}
        history_count = len(_install_history(target, skill_id=skill_id, harness=adapter_id))
        installed_source_fingerprint = latest_receipt.get("source_fingerprint") or latest_receipt.get("fingerprint")
        installed_render_fingerprint = latest_receipt.get("render_fingerprint")
        version_drift = bool(latest_receipt and latest_receipt.get("version") != current_version)
        source_drift = bool(drift and drift.get("source") == "changed")
        render_drift = bool(drift and drift.get("render") == "changed")
        local_drift = bool(drift and drift.get("local_edit") == "changed")
        adapters.append(
            {
                "id": adapter_id,
                "status": adapter.get("status"),
                "format": adapter.get("format"),
                "supported": supported_state,
                "installed": installed,
                "installed_path": installed_path,
                "installed_at": latest_receipt.get("installed_at"),
                "installed_version": latest_receipt.get("version"),
                "current_version": current_version,
                "installed_source_fingerprint": installed_source_fingerprint,
                "current_source_fingerprint": lint_payload.get("fingerprint"),
                "installed_render_fingerprint": installed_render_fingerprint,
                "current_render_fingerprint": render_fingerprint,
                "version_drift": version_drift,
                "source_drift": source_drift,
                "render_drift": render_drift,
                "local_drift": local_drift,
                "drift": (
                    {
                        key: drift[key]
                        for key in (
                            "overall",
                            "source",
                            "render",
                            "local_edit",
                            "receipt_known",
                            "content_changed",
                        )
                    }
                    if drift
                    else None
                ),
                "install_history_count": history_count,
                "receipt_path": str(_installs_root(target) / f"{skill_id}-{adapter_id}.json")
                if latest_receipt
                else None,
                "render_valid": not rendered_errors,
                "render_errors": rendered_errors,
                "blockers": blockers,
            }
        )
    payload = {
        "target": str(target),
        "skill_id": skill_id,
        "valid": bool(lint_payload.get("valid")),
        "fingerprint": lint_payload.get("fingerprint"),
        "source": lint_payload.get("source"),
        "version": current_version,
        "trust_score": lint_payload.get("trust_score"),
        "changelog": lint_payload.get("changelog"),
        "adapters": adapters,
    }
    return payload


def compatibility(*, target: Path, skill: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    payload = _compatibility_payload(target, skill)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["valid"] else 1
    print(f"skill compatibility: {payload['skill_id']}")
    print(f"valid: {str(payload['valid']).lower()}")
    for row in payload["adapters"]:
        blocked = f" blockers={len(row['blockers'])}" if row["blockers"] else ""
        print(f"- {row['id']} [{row['status']}] supported={row['supported']} installed={row['installed']}{blocked}")
    return 0 if payload["valid"] else 1


def _fleet_skill_ids(target: Path) -> list[str]:
    bundled_root = template_root() / "skills"
    bundled = (
        {path.name for path in bundled_root.iterdir() if path.is_dir() and _skill_md_path(path).is_file()}
        if bundled_root.is_dir()
        else set()
    )
    registry = {
        _slug(str(row["metadata"].get("id") or Path(str(row["skill_dir"])).name)) for row in _iter_registry(target)
    }
    return sorted(bundled | registry)


def _plain_files_under_anchor(anchor: _StateRootAnchor, *relative: str) -> list[str] | None:
    """List plain regular-file names under an anchored state path.

    Mirrors :func:`_plain_subdirs_under_anchor`: ``None`` for a missing
    directory, refusal for symlinked components, symlinked entries skipped.
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
            if stat_module.S_ISREG(st.st_mode):
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
            if stat_module.S_ISREG(st.st_mode):
                names.append(name)
        return names
    finally:
        for held_fd in reversed(opened):
            os.close(held_fd)


def _fleet_receipts(target: Path) -> dict[tuple[str, str], dict[str, Any]]:
    """Read install receipts through the held state-root anchor.

    Receipt files are enumerated through the anchor and each one is read with
    ``_read_state_file_bytes``; a symlinked receipt contributes nothing.
    """
    receipts: dict[tuple[str, str], dict[str, Any]] = {}
    try:
        with _held_state_root(target) as anchor:
            names = _plain_files_under_anchor(anchor, "skills", "installs")
            if not names:
                return receipts
            install_targets = set(_install_targets(target))
            for name in names:
                if not name.endswith(".json"):
                    continue
                raw = _read_state_file_bytes(anchor, "skills", "installs", name)
                if raw is None:
                    continue
                try:
                    parsed = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if not isinstance(parsed, dict):
                    continue
                skill_id = parsed.get("skill_id")
                harness = parsed.get("target")
                if (
                    isinstance(skill_id, str)
                    and skill_id
                    and isinstance(harness, str)
                    and harness in install_targets
                    and name == f"{_slug(skill_id)}-{harness}.json"
                ):
                    receipts[(_slug(skill_id), harness)] = parsed
    except SkillsStatePathError:
        return {}
    return receipts


def _fleet_copy_keys(target: Path) -> list[tuple[str, str]]:
    keys = set(_fleet_receipts(target))
    skill_ids = _fleet_skill_ids(target)
    for harness in sorted(_install_targets(target)):
        if harness == "hermes":
            continue
        probe, _probe_escapes = _evaluate_install_dir(target, harness, "__brigade_probe__")
        root = probe.parent
        if root.is_dir():
            for path in sorted(item for item in root.iterdir() if item.is_dir()):
                if _skill_md_path(path).is_file():
                    keys.add((_slug(path.name), harness))
        for skill_id in skill_ids:
            install_dir, escapes = _evaluate_install_dir(target, harness, skill_id)
            if not escapes and install_dir.exists():
                keys.add((skill_id, harness))
    return sorted(keys)


def _fleet_source_selector(skill_id: str, receipt: dict[str, Any]) -> str | None:
    if not receipt:
        return skill_id
    source = raw_source if isinstance((raw_source := receipt.get("source")), dict) else {}
    kind = source.get("kind")
    identity = source.get("identity")
    if kind == "brigade-bundle" and identity == f"{BUNDLED_SOURCE_PREFIX}{skill_id}":
        return f"bundled:{skill_id}"
    if kind == "registry" and identity == f"registry://skills/{skill_id}":
        return f"registry:{skill_id}"
    if kind == "path" and isinstance(identity, str) and identity.startswith("path:"):
        source_path = identity.removeprefix("path:")
        return source_path if Path(source_path).is_absolute() else None
    return None


def _fleet_update_command(*, target: Path, skill_id: str, harness: str, source: dict[str, Any]) -> str | None:
    kind = source.get("kind")
    identity = source.get("identity")
    if kind == "brigade-bundle":
        selector = f"bundled:{skill_id}"
    elif kind == "registry":
        selector = f"registry:{skill_id}"
    elif kind == "path" and isinstance(identity, str) and identity.startswith("path:"):
        selector = identity.removeprefix("path:")
        if not Path(selector).is_dir():
            return None
    else:
        return None
    return shlex.join(
        [
            "brigade",
            "skills",
            "install",
            selector,
            "--workspace",
            str(target),
            "--target",
            harness,
            "--force",
        ]
    )


def _fleet_remove_command(*, target: Path, skill_id: str, harness: str) -> str:
    return shlex.join(
        [
            "brigade",
            "skills",
            "uninstall",
            skill_id,
            "--workspace",
            str(target),
            "--target",
            harness,
        ]
    )


def _fleet_status_payload(target: Path) -> dict[str, Any]:
    copies: list[dict[str, Any]] = []
    receipts = _fleet_receipts(target)
    for skill_id, harness in _fleet_copy_keys(target):
        receipt = receipts.get((skill_id, harness), {})
        install_dir, escapes = _evaluate_install_dir(target, harness, skill_id)
        if escapes:
            copies.append(
                {
                    "skill_id": skill_id,
                    "harness": harness,
                    "installed_path": str(install_dir),
                    "status": "external",
                    "drift": {
                        "overall": "unknown",
                        "source": "unknown",
                        "render": "unknown",
                        "local_edit": "unknown",
                        "receipt_known": False,
                        "content_changed": False,
                    },
                    "source": receipt.get("source") if isinstance(receipt.get("source"), dict) else None,
                    "supported": None,
                    "update_command": None,
                }
            )
            continue
        selector = _fleet_source_selector(skill_id, receipt)
        lint_payload = _lint_payload(target, selector) if selector is not None else {"valid": False}
        if not lint_payload.get("valid"):
            copies.append(
                {
                    "skill_id": skill_id,
                    "harness": harness,
                    "installed_path": str(install_dir),
                    "status": "unknown",
                    "drift": {
                        "overall": "unknown",
                        "source": "unknown",
                        "render": "unknown",
                        "local_edit": "unknown",
                        "receipt_known": False,
                        "content_changed": False,
                    },
                    "source": receipt.get("source") if isinstance(receipt.get("source"), dict) else None,
                    "supported": None,
                    "update_command": None,
                }
            )
            continue
        source_dir = Path(str(lint_payload["skill_dir"]))
        metadata = raw_metadata if isinstance((raw_metadata := lint_payload.get("metadata")), dict) else {}
        if selector is not None and selector.startswith("registry:"):
            # Registry rendering text comes from the anchored read.
            source_text = _anchored_registry_text(target, _slug(selector.removeprefix("registry:")), "SKILL.md") or ""
        else:
            source_text = _skill_md_path(source_dir).read_text(encoding="utf-8", errors="replace")
        supported = raw_supported if isinstance((raw_supported := metadata.get("supported_harnesses")), list) else []
        source = raw_source if isinstance((raw_source := lint_payload.get("source")), dict) else {}
        supported_state = harness in supported or not supported
        installed_dir = install_dir
        rendered = _render_skill_text_for_harness(source_text, metadata, skill_id, harness)
        drift = _drift_payload(
            target=target,
            skill_id=skill_id,
            harness=harness,
            lint_payload=lint_payload,
            rendered=rendered,
            installed_dir=installed_dir,
        )
        if not drift["receipt_known"]:
            status = "unknown"
        elif _skill_md_path(installed_dir).is_file() and not supported_state:
            status = "unsupported"
        elif drift["overall"] == "missing":
            status = "missing"
        elif drift["overall"] == "changed":
            status = "stale"
        elif drift["overall"] == "current":
            status = "current"
        else:
            status = "unknown"
        update_command = (
            _fleet_update_command(target=target, skill_id=skill_id, harness=harness, source=source)
            if status in {"stale", "missing"} and supported_state
            else None
        )
        remove_command = (
            _fleet_remove_command(target=target, skill_id=skill_id, harness=harness)
            if status == "unsupported"
            else None
        )
        copies.append(
            {
                "skill_id": skill_id,
                "harness": harness,
                "installed_path": str(installed_dir),
                "status": status,
                "drift": {
                    key: drift[key]
                    for key in (
                        "overall",
                        "source",
                        "render",
                        "local_edit",
                        "receipt_known",
                        "content_changed",
                    )
                },
                "source": source,
                "supported": supported_state,
                "update_command": update_command,
                "remove_command": remove_command,
            }
        )
    copies.sort(key=lambda row: (str(row["skill_id"]), str(row["harness"])))
    counts = {
        state: sum(row["status"] == state for row in copies)
        for state in ("current", "stale", "missing", "unsupported", "unknown", "external")
    }
    return {
        "schema_version": 1,
        "target": str(target),
        "checked_count": len(copies),
        "current_count": counts["current"],
        "stale_count": counts["stale"],
        "missing_count": counts["missing"],
        "unsupported_count": counts["unsupported"],
        "unknown_count": counts["unknown"],
        "external_count": counts["external"],
        "copies": copies,
        "stale": [row for row in copies if row["status"] in {"stale", "missing"}],
    }


def fleet_status(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    payload = _fleet_status_payload(target)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"skills fleet status: {target}")
    print(
        f"checked={payload['checked_count']} current={payload['current_count']} "
        f"stale={payload['stale_count']} missing={payload['missing_count']} "
        f"unsupported={payload['unsupported_count']} unknown={payload['unknown_count']} "
        f"external={payload['external_count']}"
    )
    for row in (item for item in payload["copies"] if item["status"] != "current"):
        support = " supported=false" if row.get("supported") is False else ""
        print(f"- {row['skill_id']} [{row['harness']}] {row['status']}{support}")
        if row["update_command"]:
            print(f"  update: {row['update_command']}")
        elif row.get("remove_command"):
            print(f"  remove: {row['remove_command']}")
    return 0


def _skill_health_issues(target: Path) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    registry = _iter_registry(target)
    if not registry:
        return issues
    for row in registry:
        metadata = row["metadata"]
        skill_id = _slug(str(metadata.get("id") or Path(row["skill_dir"]).name))
        lint_payload = _lint_payload(target, f"registry:{skill_id}")
        for error in lint_payload.get("errors", []):
            issues.append(
                {
                    "status": FAIL,
                    "name": "skill_lint_error",
                    "issue_type": "lint_error",
                    "skill_id": skill_id,
                    "detail": str(error),
                    "fingerprint": lint_payload.get("fingerprint"),
                }
            )
        for warning in lint_payload.get("warnings", []):
            issues.append(
                {
                    "status": WARN,
                    "name": "skill_lint_warning",
                    "issue_type": "lint_warning",
                    "skill_id": skill_id,
                    "detail": str(warning),
                    "fingerprint": lint_payload.get("fingerprint"),
                }
            )
        trust_score = raw_trust_score if isinstance((raw_trust_score := lint_payload.get("trust_score")), dict) else {}
        if trust_score.get("trust_level") == "unreviewed":
            issues.append(
                {
                    "status": WARN,
                    "name": "skill_unreviewed_trust",
                    "issue_type": "unreviewed_trust",
                    "skill_id": skill_id,
                    "detail": "skill trust_level is unreviewed",
                    "fingerprint": lint_payload.get("fingerprint"),
                }
            )
        if trust_score.get("tests_declared") == 0:
            issues.append(
                {
                    "status": WARN,
                    "name": "skill_tests_missing",
                    "issue_type": "tests_missing",
                    "skill_id": skill_id,
                    "detail": "skill declares no tests",
                    "fingerprint": lint_payload.get("fingerprint"),
                }
            )
        changelog = raw_changelog if isinstance((raw_changelog := lint_payload.get("changelog")), dict) else {}
        if not changelog.get("present"):
            issues.append(
                {
                    "status": WARN,
                    "name": "skill_changelog_missing",
                    "issue_type": "changelog_missing",
                    "skill_id": skill_id,
                    "detail": "skill has no CHANGELOG.md or changelog_path metadata",
                    "fingerprint": lint_payload.get("fingerprint"),
                }
            )
        compat = _compatibility_payload(target, f"registry:{skill_id}")
        for adapter in compat.get("adapters", []):
            if not isinstance(adapter, dict):
                continue
            adapter_id = str(adapter.get("id") or "")
            if adapter.get("version_drift"):
                issues.append(
                    {
                        "status": WARN,
                        "name": "skill_version_drift",
                        "issue_type": "version_drift",
                        "skill_id": skill_id,
                        "harness": adapter_id,
                        "detail": f"{adapter_id} installed version differs from registry version",
                        "fingerprint": adapter.get("installed_source_fingerprint") or lint_payload.get("fingerprint"),
                    }
                )
            if adapter.get("source_drift") or adapter.get("render_drift") or adapter.get("local_drift"):
                issues.append(
                    {
                        "status": WARN,
                        "name": "skill_install_drift",
                        "issue_type": "install_drift",
                        "skill_id": skill_id,
                        "harness": adapter_id,
                        "detail": f"{adapter_id} installed skill differs from current registry render",
                        "fingerprint": adapter.get("installed_render_fingerprint")
                        or adapter.get("current_render_fingerprint"),
                    }
                )
    return issues


def _skills_doctor_payload(target: Path) -> dict[str, Any]:
    registry = _iter_registry(target)
    issues = _skill_health_issues(target)
    return {
        "target": str(target),
        "registry_path": str(_registry_root(target)),
        "skill_count": len(registry),
        "valid": not any(issue.get("status") == FAIL for issue in issues),
        "issue_count": len(issues),
        "issues": issues,
    }


def doctor(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload = _skills_doctor_payload(target)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["valid"] else 1
    print(f"skills doctor: {target}")
    print(f"registry_path: {payload['registry_path']}")
    if payload["issues"]:
        for issue in payload["issues"]:
            harness = f" {issue.get('harness')}" if issue.get("harness") else ""
            print(
                f"[{issue.get('status', WARN)}] {issue.get('name')}: {issue.get('skill_id')}{harness}: {issue.get('detail')}"
            )
    else:
        print("[ok] skill_registry: no issues")
    print(f"skill_issues: {payload['issue_count']}")
    return 0 if payload["valid"] else 1


def _skill_issue_records(target: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for issue in _skill_health_issues(target):
        issue_type = str(issue.get("issue_type") or issue.get("name") or "skill_issue")
        skill_id = str(issue.get("skill_id") or "registry")
        harness = str(issue.get("harness") or "")
        detail = str(issue.get("detail") or "")
        source_fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "skill_id": skill_id,
                    "harness": harness,
                    "issue_type": issue_type,
                    "detail": detail,
                    "fingerprint": issue.get("fingerprint"),
                },
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()[:16]
        metadata = {
            "skill_id": skill_id,
            "skill_harness": harness or None,
            "skill_issue_type": issue_type,
            "skill_issue_detail": detail,
            "source_item_key": f"skill-registry:{skill_id}:{issue_type}:{harness}",
            "source_fingerprint": source_fingerprint,
        }
        records.append(
            {
                "text": f"Repair skill registry issue {skill_id}/{issue_type}: {detail}",
                "kind": "task",
                "source": "skill-registry",
                "type": "workflow",
                "priority": "high" if issue.get("status") == FAIL else "normal",
                "template": "bugfix",
                "acceptance": [f"`brigade skills doctor` no longer reports {skill_id}/{issue_type}."],
                "metadata": metadata,
            }
        )
    return records


def import_issues(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    from . import work_cmd

    records = _skill_issue_records(target)
    imported, skipped, skipped_dismissed, _rejected = work_cmd._append_import_records(target, records)
    payload = {
        "target": str(target),
        "source": "skill-registry",
        "issue_count": len(records),
        "imported_count": len(imported),
        "skipped_count": len(skipped),
        "skipped_dismissed_count": len(skipped_dismissed),
        "imports": imported,
        "skipped": skipped,
        "skipped_dismissed": skipped_dismissed,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"skills import-issues: {target}")
    print(f"issues: {len(records)}")
    print(f"imported: {len(imported)}")
    print(f"skipped: {len(skipped)}")
    print(f"skipped_dismissed: {len(skipped_dismissed)}")
    return 0


def _proposal_meta_path(path: Path) -> Path:
    return path / "proposal.json"


def _proposal_skill_path(path: Path) -> Path:
    return path / "skill"


def _anchored_proposals(target: Path) -> list[tuple[str, dict[str, Any]]]:
    """List inbox proposals through the held state-root anchor.

    Only plain directories carrying a readable ``proposal.json`` contribute.
    A symlinked inbox component refuses the whole listing, a symlinked
    proposal directory is skipped, and a symlinked proposal file refuses that
    entry: outside file contents can never reach the output.
    """
    entries: list[tuple[str, bytes]] = []
    try:
        with _held_state_root(target) as state_anchor:
            if not _HAS_DESCRIPTOR_ANCHOR:
                # lstat-guarded fallback for platforms without anchoring.
                root = _inbox_root(target)
                if root.is_symlink() or _is_reparse_point(root) or not root.is_dir():
                    return []
                for child in sorted(root.iterdir(), key=lambda item: item.name):
                    if child.is_symlink() or _is_reparse_point(child) or not child.is_dir():
                        continue
                    meta = _proposal_meta_path(child)
                    try:
                        st = os.lstat(meta)
                    except OSError:
                        continue
                    if stat_module.S_ISLNK(st.st_mode) or getattr(st, "st_reparse_tag", False):
                        continue
                    if not stat_module.S_ISREG(st.st_mode):
                        continue
                    try:
                        entries.append((child.name, meta.read_bytes()))
                    except OSError:
                        continue
            else:
                try:
                    inbox_fd, opened = _anchor_open_chain(state_anchor, "skills", "inbox")
                except SkillsStatePathError:
                    return []
                names: list[str] = []
                try:
                    for name in sorted(os.listdir(inbox_fd)):
                        try:
                            st = os.lstat(name, dir_fd=inbox_fd)
                        except FileNotFoundError:
                            continue
                        if stat_module.S_ISDIR(st.st_mode):
                            names.append(name)
                finally:
                    for fd in reversed(opened):
                        os.close(fd)
                for name in names:
                    try:
                        raw = _read_state_file_bytes(state_anchor, "skills", "inbox", name, "proposal.json")
                    except SkillsStatePathError:
                        continue
                    if raw is not None:
                        entries.append((name, raw))
    except SkillsStatePathError:
        return []
    proposals: list[tuple[str, dict[str, Any]]] = []
    for name, raw in entries:
        try:
            meta = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(meta, dict):
            continue
        meta.setdefault("proposal_id", name)
        meta.setdefault("path", str(_inbox_root(target) / name))
        proposals.append((name, meta))
    return proposals


def _resolve_proposal(target: Path, proposal_id: str) -> tuple[str | None, dict[str, Any] | None, str | None]:
    """Resolve one proposal to its validated directory name under the anchor."""
    slug = _slug(proposal_id)
    matches = [(name, meta) for name, meta in _anchored_proposals(target) if name.startswith(slug)]
    if not matches:
        return None, None, f"skill proposal not found: {proposal_id}"
    if len(matches) > 1:
        return None, None, f"skill proposal id is ambiguous: {proposal_id}"
    return matches[0][0], matches[0][1], None


def inbox_add(
    *,
    target: Path,
    source: Path,
    skill_id: str | None = None,
    summary: str | None = None,
    force: bool = False,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    source_dir, error = _source_skill_dir(source)
    if source_dir is None:
        print(f"error: {error}", file=sys.stderr)
        return 2
    metadata = _read_json(source_dir / "skill.json")
    resolved_skill_id = _slug(skill_id or str(metadata.get("id") or source_dir.name))
    created = _now()
    proposal_id = f"{created[:19].replace(':', '').replace('-', '')}-{resolved_skill_id}"
    proposal_rel = ("skills", "inbox", _slug(proposal_id))
    proposal_dir = target / ".brigade" / "skills" / "inbox" / _slug(proposal_id)
    skill_dest = _proposal_skill_path(proposal_dir)
    try:
        with _held_state_root(target) as state_anchor:
            # Refuse a redirected inbox before anything is copied, replaced,
            # or deleted; a symlinked component never reaches the disk outside.
            _require_plain_state_dirs(state_anchor, "skills", "inbox")
            existing = _state_entry_kind(state_anchor, *proposal_rel)
            if existing == "dir" and not force:
                print(f"error: skill proposal already exists: {proposal_dir}", file=sys.stderr)
                return 2
            if force and existing != "missing":
                _remove_tree_in_anchor(state_anchor, *proposal_rel)
            # Collect the candidate once: the copy, the recorded fingerprint,
            # and the staged lint below all derive from these bytes; the
            # state-root proposal copy is never re-read by pathname.
            collected_dirs, collected_files = _collect_source_tree(source_dir)
            _write_collected_tree_into_anchor(collected_dirs, collected_files, state_anchor, *proposal_rel, "skill")
            with tempfile.TemporaryDirectory(prefix="brigade-inbox-lint-") as staging:
                staged_skill = Path(staging) / "skill"
                _write_snapshot_tree(collected_dirs, collected_files, staged_skill)
                lint_payload = _lint_path_payload(Path(staging), str(staged_skill), mode="lenient", contain=True)
                lint_payload["target"] = str(target)
                lint_payload = _repoint_staged_lint_paths(lint_payload, skill_dest, staged_skill)
            proposal = {
                "proposal_id": proposal_dir.name,
                "skill_id": resolved_skill_id,
                "status": "pending",
                "summary": summary or "",
                "source": str(source.expanduser().resolve()),
                "created_at": created,
                "path": str(proposal_dir),
                "skill_path": str(skill_dest),
                "fingerprint": _files_fingerprint(collected_files),
                "lint": lint_payload,
            }
            _write_state_file(state_anchor, *proposal_rel, "proposal.json", data=_json_bytes(proposal))
    except SkillsStatePathError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if json_output:
        print(json.dumps(proposal, indent=2, sort_keys=True))
        return 0 if lint_payload["valid"] else 1
    print(f"skill_proposal: {proposal['proposal_id']}")
    print(f"skill_id: {resolved_skill_id}")
    print("status: pending")
    return 0 if lint_payload["valid"] else 1


def inbox_list(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    proposals = [meta for _name, meta in _anchored_proposals(target)]
    payload = {"target": str(target), "proposal_count": len(proposals), "proposals": proposals}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"skill inbox: {target}")
    for proposal in proposals:
        print(f"- {proposal.get('proposal_id')} [{proposal.get('status')}] {proposal.get('skill_id')}")
    if not proposals:
        print("no skill proposals")
    return 0


def inbox_show(*, target: Path, proposal_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    _name, proposal, error = _resolve_proposal(target, proposal_id)
    if proposal is None:
        print(f"error: {error}", file=sys.stderr)
        return 2
    if json_output:
        print(json.dumps(proposal, indent=2, sort_keys=True))
        return 0
    print(f"skill proposal: {proposal.get('proposal_id')}")
    print(f"skill_id: {proposal.get('skill_id')}")
    print(f"status: {proposal.get('status')}")
    print(f"fingerprint: {proposal.get('fingerprint')}")
    return 0


def inbox_diff(*, target: Path, proposal_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    name, proposal, error = _resolve_proposal(target, proposal_id)
    if name is None or proposal is None:
        print(f"error: {error}", file=sys.stderr)
        return 2
    skill_id = _slug(str(proposal.get("skill_id") or name))
    proposed_display = _inbox_root(target) / name / "skill" / "SKILL.md"
    existing_display = _skill_path(target, skill_id) / "SKILL.md"
    try:
        with _held_state_root(target) as state_anchor:
            # Both sides are read through the held anchor: the proposed tree
            # via descriptor collection and the registry copy via the
            # anchored reader, so symlinked components never drag outside
            # file contents into the diff.
            _proposal_dirs, proposed_files = _read_tree_from_anchor(state_anchor, "skills", "inbox", name, "skill")
            existing_raw = _read_state_file_bytes(state_anchor, "skills", "registry", skill_id, "SKILL.md")
    except SkillsStatePathError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    proposed_raw = proposed_files.get(("SKILL.md",))
    before = existing_raw.decode("utf-8", errors="replace").splitlines() if existing_raw is not None else []
    after = proposed_raw.decode("utf-8", errors="replace").splitlines() if proposed_raw is not None else []
    diff = list(
        difflib.unified_diff(before, after, fromfile=str(existing_display), tofile=str(proposed_display), lineterm="")
    )
    payload = {
        "target": str(target),
        "proposal_id": proposal.get("proposal_id"),
        "skill_id": proposal.get("skill_id"),
        "diff": diff,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print("\n".join(diff) if diff else "no diff")
    return 0


def inbox_accept(*, target: Path, proposal_id: str, force: bool = False, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    name, proposal, error = _resolve_proposal(target, proposal_id)
    if name is None or proposal is None:
        print(f"error: {error}", file=sys.stderr)
        return 2
    if proposal.get("status") != "pending" and not force:
        print(f"error: skill proposal is not pending: {proposal.get('status')}", file=sys.stderr)
        return 2
    try:
        with _held_state_root(target) as state_anchor:
            # The proposal's skill tree is read back through the anchor and
            # staged from those collected bytes; a symlinked proposal
            # component can never redirect the import outside the held state
            # root, and the status write below goes through the anchor too.
            proposal_dirs, proposal_files = _read_tree_from_anchor(state_anchor, "skills", "inbox", name, "skill")
            with tempfile.TemporaryDirectory(prefix="brigade-proposal-import-") as staging:
                staged = Path(staging) / "skill"
                _write_snapshot_tree(proposal_dirs, proposal_files, staged)
                payload, import_error, rc = _registry_import_payload(
                    target=target,
                    source=staged,
                    skill_id=str(proposal.get("skill_id") or name),
                    force=force,
                    # Provenance stays the operator-supplied original; the
                    # private staging directory must never be persisted as
                    # the skill's source.
                    source_provenance=str(proposal.get("source") or (_inbox_root(target) / name / "skill")),
                )
            if payload is None:
                print(f"error: {import_error}", file=sys.stderr)
                return rc
            proposal.update({"status": "accepted", "accepted_at": _now(), "registry": payload})
            _write_state_file(state_anchor, "skills", "inbox", name, "proposal.json", data=_json_bytes(proposal))
    except SkillsStatePathError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if json_output:
        print(json.dumps(proposal, indent=2, sort_keys=True))
        return rc
    print(f"skill_proposal: {proposal.get('proposal_id')}")
    print("status: accepted")
    print(f"skill_id: {proposal.get('skill_id')}")
    return rc


def inbox_reject(*, target: Path, proposal_id: str, reason: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    name, proposal, error = _resolve_proposal(target, proposal_id)
    if name is None or proposal is None:
        print(f"error: {error}", file=sys.stderr)
        return 2
    try:
        with _held_state_root(target) as state_anchor:
            proposal.update({"status": "rejected", "rejected_at": _now(), "reason": reason})
            _write_state_file(state_anchor, "skills", "inbox", name, "proposal.json", data=_json_bytes(proposal))
    except SkillsStatePathError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if json_output:
        print(json.dumps(proposal, indent=2, sort_keys=True))
        return 0
    print(f"skill_proposal: {proposal.get('proposal_id')}")
    print("status: rejected")
    print(f"reason: {reason}")
    return 0
