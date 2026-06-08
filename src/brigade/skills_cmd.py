"""Reviewed skill registry commands.

Skills are treated as versioned workflow code: imports land in a local Brigade
registry, lint checks provenance and injection risk, and installs materialize
reviewed packs into harness-specific folders.
"""
from __future__ import annotations

import hashlib
import difflib
import json
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .untrusted import scan_untrusted

OK = "ok"
WARN = "warn"
FAIL = "fail"

HARNESS_ADAPTERS: dict[str, dict[str, Any]] = {
    "codex": {"status": "built-in", "format": "codex-skill", "install_path": ".codex/skills/{skill_id}"},
    "claude": {"status": "built-in", "format": "claude-skill", "install_path": ".claude/skills/{skill_id}"},
    "opencode": {"status": "built-in", "format": "opencode-skill", "install_path": ".opencode/skills/{skill_id}"},
    "antigravity": {"status": "built-in", "format": "antigravity-skill", "install_path": ".antigravity/skills/{skill_id}"},
    "pi": {"status": "built-in", "format": "pi-skill", "install_path": ".pi/skills/{skill_id}"},
    "openclaw": {"status": "built-in", "format": "openclaw-skill", "install_path": ".openclaw/skills/{skill_id}"},
    "hermes": {"status": "built-in", "format": "hermes-skill", "install_path": ".hermes/skills/{skill_id}"},
    "mcp": {"status": "built-in", "format": "mcp-resource", "install_path": ".brigade/skills/mcp-resources/{skill_id}"},
    "gemini": {"status": "deprecated", "format": "adapter-deprecated", "install_path": None},
    "cursor": {"status": "planned", "format": "adapter-needed", "install_path": None},
}
HARNESS_TARGETS = tuple(key for key, value in HARNESS_ADAPTERS.items() if value["status"] == "built-in")
INSTALL_TARGETS = (*HARNESS_TARGETS, "all")
TRUST_LEVELS = ("unreviewed", "workspace", "team", "public")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9._-]+", "-", value.strip().lower()).strip("-")
    return slug or "skill"


def _registry_root(target: Path) -> Path:
    return target / ".brigade" / "skills" / "registry"


def _inbox_root(target: Path) -> Path:
    return target / ".brigade" / "skills" / "inbox"


def _adapters_config_path(target: Path) -> Path:
    return target / ".brigade" / "skills" / "adapters.json"


def _rollback_root(target: Path, skill_id: str, harness: str) -> Path:
    return target / ".brigade" / "skills" / "rollback" / _slug(skill_id) / harness


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
        metadata.get("description")
        or metadata.get("title")
        or f"Use this reviewed Brigade skill for {name}."
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


def _render_skill_text_for_harness(text: str, metadata: dict[str, Any], skill_id: str, harness: str) -> str:
    rendered = text if text.endswith("\n") else text + "\n"
    if harness == "codex":
        rendered = _ensure_codex_frontmatter(rendered, metadata, skill_id)
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
    return errors


def _copy_skill_for_harness(source_dir: Path, dest: Path, metadata: dict[str, Any], skill_id: str, harness: str) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source_dir, dest)
    source_skill = _skill_md_path(source_dir)
    if source_skill.is_file():
        rendered = _render_skill_text_for_harness(source_skill.read_text(errors="replace"), metadata, skill_id, harness)
        _skill_md_path(dest).write_text(rendered)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


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


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.is_file():
        return rows
    try:
        lines = path.read_text(errors="replace").splitlines()
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


def _changelog_payload(skill_dir: Path, metadata: dict[str, Any]) -> dict[str, Any]:
    configured = metadata.get("changelog_path") or metadata.get("changelog")
    candidates: list[Path] = []
    if isinstance(configured, str) and configured.strip():
        configured_path = Path(configured).expanduser()
        candidates.append(configured_path if configured_path.is_absolute() else skill_dir / configured_path)
    candidates.append(skill_dir / "CHANGELOG.md")
    path = next((candidate for candidate in candidates if candidate.is_file()), None)
    headings: list[str] = []
    fingerprint: str | None = None
    if path is not None:
        text = path.read_text(errors="replace")
        fingerprint = _text_fingerprint(text)
        headings = [line.strip("# ").strip() for line in text.splitlines() if line.startswith("#")][:8]
    return {
        "present": path is not None,
        "path": str(path) if path is not None else None,
        "fingerprint": fingerprint,
        "headings": headings,
    }


def _trust_score_payload(skill_dir: Path, metadata: dict[str, Any], lint_payload: dict[str, Any] | None = None) -> dict[str, Any]:
    score = 100
    signals: list[str] = []
    trust_level = str(metadata.get("trust_level") or "unreviewed")
    if trust_level == "unreviewed":
        score -= 35
        signals.append("trust_level is unreviewed")
    elif trust_level == "workspace":
        score -= 10
        signals.append("trust_level is workspace")
    elif trust_level not in {"team", "public"}:
        score -= 20
        signals.append(f"unknown trust_level: {trust_level}")
    tests = metadata.get("tests") if isinstance(metadata.get("tests"), list) else []
    if not tests:
        score -= 15
        signals.append("no tests declared")
    changelog = _changelog_payload(skill_dir, metadata)
    if not changelog["present"]:
        score -= 5
        signals.append("no changelog found")
    if lint_payload is not None:
        warnings = lint_payload.get("warnings") if isinstance(lint_payload.get("warnings"), list) else []
        errors = lint_payload.get("errors") if isinstance(lint_payload.get("errors"), list) else []
        injection = lint_payload.get("injection") if isinstance(lint_payload.get("injection"), dict) else {}
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


def _copy_skill_source(source_dir: Path, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source_dir, dest)


def _registry_import_payload(
    *,
    target: Path,
    source: Path,
    skill_id: str | None,
    force: bool,
) -> tuple[dict[str, Any] | None, str | None, int]:
    source_dir, error = _source_skill_dir(source)
    if source_dir is None:
        return None, error, 2
    incoming_metadata = _read_json(source_dir / "skill.json")
    resolved_id = _slug(skill_id or str(incoming_metadata.get("id") or source_dir.name))
    dest = _skill_path(target, resolved_id)
    if dest.exists() and not force:
        return None, f"skill already exists: {dest}", 2
    _copy_skill_source(source_dir, dest)
    metadata = dict(incoming_metadata)
    metadata.update(
        {
            "id": resolved_id,
            "version": str(metadata.get("version") or "0.1.0"),
            "source": str(source),
            "imported_at": _now(),
            "trust_level": str(metadata.get("trust_level") or "unreviewed"),
            "required_tools": metadata.get("required_tools") if isinstance(metadata.get("required_tools"), list) else [],
            "required_mcp_servers": metadata.get("required_mcp_servers") if isinstance(metadata.get("required_mcp_servers"), list) else [],
            "supported_harnesses": metadata.get("supported_harnesses") if isinstance(metadata.get("supported_harnesses"), list) else list(HARNESS_TARGETS),
            "tests": metadata.get("tests") if isinstance(metadata.get("tests"), list) else [],
        }
    )
    metadata["fingerprint"] = _fingerprint(dest)
    _write_json(_metadata_path(dest), metadata)
    lint_payload = _lint_payload(target, resolved_id)
    return {"target": str(target), "skill_id": resolved_id, "skill_dir": str(dest), "lint": lint_payload}, None, 0 if lint_payload["valid"] else 1


def _load_skill(target: Path, skill_or_path: str) -> tuple[Path, dict[str, Any]]:
    candidate = Path(skill_or_path).expanduser()
    if candidate.exists():
        skill_dir = candidate if candidate.is_dir() else candidate.parent
    else:
        skill_dir = _skill_path(target, skill_or_path)
    metadata = _read_json(_metadata_path(skill_dir))
    metadata.setdefault("id", skill_dir.name)
    return skill_dir, metadata


def _lint_payload(target: Path, skill_or_path: str, harness: str | None = None) -> dict[str, Any]:
    skill_dir, metadata = _load_skill(target, skill_or_path)
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
        text = skill_md.read_text(errors="replace")
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
        "fingerprint": _fingerprint(skill_dir) if skill_dir.is_dir() else None,
    }
    payload["changelog"] = _changelog_payload(skill_dir, metadata) if skill_dir.is_dir() else {"present": False, "path": None, "fingerprint": None, "headings": []}
    payload["trust_score"] = _trust_score_payload(skill_dir, metadata, payload) if skill_dir.is_dir() else {"score": 0, "trust_level": metadata.get("trust_level") or "unreviewed", "signals": ["skill directory missing"], "tests_declared": 0, "changelog": payload["changelog"]}
    return payload


def _iter_registry(target: Path) -> list[dict[str, Any]]:
    root = _registry_root(target)
    rows: list[dict[str, Any]] = []
    if not root.is_dir():
        return rows
    for skill_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        metadata = _read_json(_metadata_path(skill_dir))
        metadata.setdefault("id", skill_dir.name)
        metadata.setdefault("title", metadata["id"])
        rows.append({"skill_dir": str(skill_dir), "metadata": metadata})
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


def lint(*, target: Path, skill: str, harness: str | None = None, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    payload = _lint_payload(target, skill, harness=harness)
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


def _install_dir(workspace: Path, harness: str, skill_id: str) -> Path:
    adapter = _adapter_map(workspace)[harness]
    return workspace / str(adapter["install_path"]).format(skill_id=skill_id)


def _adapter_map(target: Path) -> dict[str, dict[str, Any]]:
    adapters = {key: dict(value) for key, value in HARNESS_ADAPTERS.items()}
    config = _read_json(_adapters_config_path(target))
    overlay = config.get("adapters")
    if isinstance(overlay, dict):
        for adapter_id, value in overlay.items():
            if not isinstance(value, dict):
                continue
            adapters[_slug(str(adapter_id))] = {
                "status": str(value.get("status") or "local"),
                "format": str(value.get("format") or "custom-skill"),
                "install_path": value.get("install_path"),
                "source": "local-config",
            }
    return adapters


def _install_targets(workspace: Path) -> tuple[str, ...]:
    return tuple(
        key for key, value in _adapter_map(workspace).items()
        if value.get("status") in {"built-in", "local"} and value.get("install_path")
    )


def _latest_install_receipt(target: Path, skill_id: str, harness: str) -> dict[str, Any]:
    latest_path = _installs_root(target) / f"{_slug(skill_id)}-{harness}.json"
    return _read_json(latest_path) if latest_path.is_file() else {}


def _install_history(target: Path, skill_id: str | None = None, harness: str | None = None) -> list[dict[str, Any]]:
    rows = _read_jsonl(_install_history_path(target))
    if skill_id is not None:
        rows = [row for row in rows if row.get("skill_id") == _slug(skill_id)]
    if harness is not None:
        rows = [row for row in rows if row.get("target") == harness]
    rows.sort(key=lambda row: str(row.get("installed_at") or row.get("receipt_id") or ""), reverse=True)
    return rows


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
    lint_payload = _lint_payload(workspace, skill)
    if not lint_payload["valid"]:
        if json_output:
            print(json.dumps({"workspace": str(workspace), "installed": False, "lint": lint_payload}, indent=2, sort_keys=True))
        else:
            print(f"error: skill lint failed: {skill}", file=sys.stderr)
        return 1
    source_dir = Path(lint_payload["skill_dir"])
    skill_id = _slug(str(lint_payload["skill_id"]))
    metadata = lint_payload.get("metadata") if isinstance(lint_payload.get("metadata"), dict) else {}
    version = str(metadata.get("version") or "0.1.0")
    source_path = str(metadata.get("source") or source_dir)
    targets = install_targets if harness == "all" else (harness,)
    receipts: list[dict[str, Any]] = []
    for install_target in targets:
        dest = _install_dir(workspace, install_target, skill_id)
        source_text = _skill_md_path(source_dir).read_text(errors="replace")
        rendered_text = _render_skill_text_for_harness(source_text, metadata, skill_id, install_target)
        render_fingerprint = _text_fingerprint(rendered_text)
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
        receipt_path = _installs_root(workspace) / f"{skill_id}-{install_target}.json"
        previous_receipt = _read_json(receipt_path) if receipt_path.is_file() else {}
        rollback_snapshot: str | None = None
        if dest.exists():
            rollback_dir = _rollback_root(workspace, skill_id, install_target) / _now().replace(":", "").replace("+", "Z").replace(".", "-")
            shutil.copytree(dest, rollback_dir)
            rollback_snapshot = str(rollback_dir)
            shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        _copy_skill_for_harness(source_dir, dest, metadata, skill_id, install_target)
        installed_fingerprint = _fingerprint(dest)
        installed_at = _now()
        receipt = {
            "workspace": str(workspace),
            "receipt_id": f"{installed_at[:19].replace(':', '').replace('-', '')}-{skill_id}-{install_target}",
            "skill_id": skill_id,
            "target": install_target,
            "installed_dir": str(dest),
            "installed_at": installed_at,
            "version": version,
            "source_path": source_path,
            "fingerprint": lint_payload.get("fingerprint"),
            "source_fingerprint": lint_payload.get("fingerprint"),
            "render_fingerprint": render_fingerprint,
            "installed_fingerprint": installed_fingerprint,
            "format": _adapter_map(workspace)[install_target].get("format"),
            "rollback_snapshot": rollback_snapshot,
            "previous_receipt": previous_receipt if previous_receipt else None,
            "trust_score": lint_payload.get("trust_score"),
            "changelog": lint_payload.get("changelog"),
        }
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        _write_json(receipt_path, receipt)
        receipt["receipt_path"] = str(receipt_path)
        history_receipt = dict(receipt)
        history_receipt["receipt_path"] = str(receipt_path)
        _append_jsonl(_install_history_path(workspace), history_receipt)
        receipts.append(receipt)
    receipt = {
        "workspace": str(workspace),
        "skill_id": skill_id,
        "target": harness,
        "installed_at": _now(),
        "fingerprint": lint_payload.get("fingerprint"),
        "targets": list(targets),
        "receipts": receipts,
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
    return 0


def rollback(*, workspace: Path, skill: str, harness: str, json_output: bool = False) -> int:
    workspace = workspace.expanduser().resolve()
    skill_id = _slug(skill)
    if harness not in _install_targets(workspace):
        print(f"error: unknown skill install target: {harness}", file=sys.stderr)
        return 2
    root = _rollback_root(workspace, skill_id, harness)
    snapshots = sorted([path for path in root.iterdir() if path.is_dir()], reverse=True) if root.is_dir() else []
    if not snapshots:
        print(f"error: no rollback snapshot for {skill_id} on {harness}", file=sys.stderr)
        return 1
    snapshot = snapshots[0]
    dest = _install_dir(workspace, harness, skill_id)
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(snapshot, dest)
    payload = {
        "workspace": str(workspace),
        "skill_id": skill_id,
        "target": harness,
        "snapshot": str(snapshot),
        "installed_dir": str(dest),
        "rolled_back_at": _now(),
    }
    receipt_path = workspace / ".brigade" / "skills" / "installs" / f"{skill_id}-{harness}-rollback.json"
    _write_json(receipt_path, payload)
    payload["receipt_path"] = str(receipt_path)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"skill_rollback: {skill_id}")
    print(f"target: {harness}")
    print(f"installed_dir: {dest}")
    return 0


def history(*, target: Path, skill: str | None = None, harness: str | None = None, json_output: bool = False, limit: int = 20) -> int:
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


def diff(*, target: Path, skill: str, harness: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    lint_payload = _lint_payload(target, skill)
    if not lint_payload["valid"]:
        if json_output:
            print(json.dumps({"target": str(target), "skill_id": skill, "valid": False, "lint": lint_payload}, indent=2, sort_keys=True))
        else:
            print(f"error: skill lint failed: {skill}", file=sys.stderr)
        return 1
    skill_id = _slug(str(lint_payload.get("skill_id") or skill))
    if harness not in _install_targets(target):
        print(f"error: unknown skill install target: {harness}", file=sys.stderr)
        return 2
    source_dir = Path(str(lint_payload["skill_dir"]))
    metadata = lint_payload.get("metadata") if isinstance(lint_payload.get("metadata"), dict) else {}
    source_text = _skill_md_path(source_dir).read_text(errors="replace")
    rendered = _render_skill_text_for_harness(source_text, metadata, skill_id, harness)
    installed_dir = _install_dir(target, harness, skill_id)
    installed_skill = _skill_md_path(installed_dir)
    installed_text = installed_skill.read_text(errors="replace") if installed_skill.is_file() else ""
    diff_lines = list(
        difflib.unified_diff(
            installed_text.splitlines(),
            rendered.splitlines(),
            fromfile=str(installed_skill),
            tofile=f"registry-rendered:{skill_id}:{harness}",
            lineterm="",
        )
    )
    latest_receipt = _latest_install_receipt(target, skill_id, harness)
    payload = {
        "target": str(target),
        "skill_id": skill_id,
        "harness": harness,
        "installed": installed_skill.is_file(),
        "installed_path": str(installed_skill),
        "changed": bool(diff_lines),
        "diff": diff_lines,
        "source_fingerprint": lint_payload.get("fingerprint"),
        "render_fingerprint": _text_fingerprint(rendered),
        "installed_fingerprint": _text_fingerprint(installed_text) if installed_text else None,
        "receipt": latest_receipt or None,
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
        lint_payload = _lint_payload(target, skill_id)
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
    skills_dir = pack_dir / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    payload = _skill_pack_payload(target)
    payload.update({"pack_id": pack_id, "status": "built"})
    for row in _iter_registry(target):
        skill_dir = Path(str(row["skill_dir"]))
        skill_id = _slug(str(row["metadata"].get("id") or skill_dir.name))
        shutil.copytree(skill_dir, skills_dir / skill_id)
    _write_json(pack_dir / "skill-pack.json", payload)
    (pack_dir / "SKILL_PACK.md").write_text(
        f"# Skill Pack {pack_id}\n\n"
        f"- skills: {payload['skill_count']}\n"
        f"- fingerprint: {payload['evidence_fingerprint']}\n"
        f"- import: brigade skills pack import {pack_dir}\n"
    )
    payload["path"] = str(pack_dir)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"skill_pack: {pack_id}")
    print(f"path: {pack_dir}")
    print(f"skills: {payload['skill_count']}")
    return 0


def _skill_packs(target: Path) -> list[dict[str, Any]]:
    packs: list[dict[str, Any]] = []
    for root in (_skill_packs_root(target), _skill_packs_archive_root(target)):
        if not root.is_dir():
            continue
        for path in root.iterdir():
            payload = _read_json(path / "skill-pack.json")
            if payload:
                payload.setdefault("path", str(path))
                payload.setdefault("archived", root == _skill_packs_archive_root(target))
                packs.append(payload)
    packs.sort(key=lambda item: str(item.get("created_at") or item.get("pack_id") or ""), reverse=True)
    return packs


def _find_skill_pack(target: Path, pack_id: str) -> tuple[dict[str, Any] | None, str | None]:
    packs = _skill_packs(target)
    if pack_id == "latest":
        return (packs[0], None) if packs else (None, "skill pack not found: latest")
    matches = [pack for pack in packs if str(pack.get("pack_id") or "").startswith(pack_id)]
    if not matches:
        path = Path(pack_id).expanduser()
        if path.is_dir() and (path / "skill-pack.json").is_file():
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


def pack_archive(*, target: Path, pack_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    pack, error = _find_skill_pack(target, pack_id)
    if pack is None:
        print(f"error: {error}", file=sys.stderr)
        return 1
    source = Path(str(pack.get("path") or _skill_packs_root(target) / str(pack.get("pack_id"))))
    destination = _skill_packs_archive_root(target) / source.name
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        print(f"error: archived skill pack already exists: {destination}", file=sys.stderr)
        return 2
    source.rename(destination)
    payload = {"target": str(target), "pack_id": pack.get("pack_id"), "status": "archived", "archive_path": str(destination)}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"archived: {pack.get('pack_id')}")
    print(f"path: {destination}")
    return 0


def pack_import(*, target: Path, pack: Path, force: bool = False, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    pack = pack.expanduser().resolve()
    manifest = _read_json(pack / "skill-pack.json")
    skills_dir = pack / "skills"
    if not manifest or not skills_dir.is_dir():
        print(f"error: not a skill pack: {pack}", file=sys.stderr)
        return 2
    imported: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    errors: list[str] = []
    skill_paths = sorted(path for path in skills_dir.iterdir() if path.is_dir())
    for skill_path in skill_paths:
        metadata = _read_json(skill_path / "skill.json")
        skill_id = _slug(str(metadata.get("id") or skill_path.name))
        if _skill_path(target, skill_id).exists() and not force:
            conflicts.append({"skill_id": skill_id, "existing": str(_skill_path(target, skill_id)), "source": str(skill_path)})
    if conflicts:
        payload = {"target": str(target), "pack": str(pack), "imported": imported, "conflicts": conflicts, "errors": errors, "valid": False}
        if json_output:
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 1
        for conflict in conflicts:
            print(f"conflict: {conflict['skill_id']} already exists at {conflict['existing']}")
        return 1
    for skill_path in skill_paths:
        metadata = _read_json(skill_path / "skill.json")
        skill_id = _slug(str(metadata.get("id") or skill_path.name))
        payload, error, rc = _registry_import_payload(target=target, source=skill_path, skill_id=skill_id, force=force)
        if payload is None:
            errors.append(str(error))
            continue
        imported.append({"skill_id": payload["skill_id"], "skill_dir": payload["skill_dir"], "returncode": rc})
    result = {
        "target": str(target),
        "pack": str(pack),
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
    print(f"skill_pack_import: {manifest.get('pack_id') or pack.name}")
    print(f"imported: {len(imported)}")
    for error in errors:
        print(f"error: {error}")
    return 0 if not errors else 1


def _mcp_contract_payload(target: Path) -> dict[str, Any]:
    resources = []
    for row in _iter_registry(target):
        metadata = row["metadata"]
        skill_id = str(metadata.get("id") or Path(row["skill_dir"]).name)
        lint_payload = _lint_payload(target, skill_id)
        compatibility_payload = {
            "skill": f"skill://registry/{skill_id}/compatibility.json",
            "summary": "Use brigade skills compatibility for the full local view.",
        }
        resources.append(
            {
                "skill_id": skill_id,
                "skill": f"skill://registry/{skill_id}/SKILL.md",
                "metadata": f"skill://registry/{skill_id}/skill.json",
                "changelog": f"skill://registry/{skill_id}/CHANGELOG.md" if lint_payload.get("changelog", {}).get("present") else None,
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
        "tools": ["search_skills", "get_skill", "get_skill_metadata", "get_skill_changelog", "get_skill_compatibility", "get_skill_history", "lint_skill"],
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
    remainder = uri[len(prefix):]
    if "/" not in remainder:
        return None, None
    skill_id, name = remainder.split("/", 1)
    skill_id = _slug(skill_id)
    skill_dir = _skill_path(target, skill_id)
    metadata = _read_json(_metadata_path(skill_dir))
    if name == "SKILL.md":
        path = _skill_md_path(skill_dir)
        return (path.read_text(errors="replace"), "text/markdown") if path.is_file() else (None, None)
    if name == "skill.json":
        return json.dumps(metadata, indent=2, sort_keys=True) + "\n", "application/json"
    if name == "CHANGELOG.md":
        changelog = _changelog_payload(skill_dir, metadata)
        path = Path(str(changelog.get("path") or ""))
        return (path.read_text(errors="replace"), "text/markdown") if path.is_file() else (None, None)
    if name == "compatibility.json":
        return json.dumps(_compatibility_payload(target, skill_id), indent=2, sort_keys=True) + "\n", "application/json"
    if name == "history.json":
        payload = {"skill_id": skill_id, "history": _install_history(target, skill_id=skill_id)}
        return json.dumps(payload, indent=2, sort_keys=True) + "\n", "application/json"
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
        {"name": "get_skill_compatibility", "description": "Read skill compatibility JSON.", "inputSchema": schema_skill},
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
        return _read_json(_metadata_path(_skill_path(target, skill_id))), False
    if name == "get_skill_changelog":
        text, _ = _mcp_read_resource(target, f"skill://registry/{skill_id}/CHANGELOG.md")
        return text or "", text is None
    if name == "get_skill_compatibility":
        return _compatibility_payload(target, skill_id), False
    if name == "get_skill_history":
        return {"skill_id": skill_id, "history": _install_history(target, skill_id=skill_id)}, False
    if name == "lint_skill":
        return _lint_payload(target, skill_id), False
    return {"error": f"unknown read-only skill tool: {name}"}, True


def _mcp_response(request_id: object, *, result: object | None = None, error: dict[str, Any] | None = None) -> dict[str, Any]:
    response: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
    if error is not None:
        response["error"] = error
    else:
        response["result"] = result if result is not None else {}
    return response


def _run_mcp_stdio(target: Path) -> int:
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            print(json.dumps(_mcp_response(None, error={"code": -32700, "message": "parse error"})), flush=True)
            continue
        if not isinstance(request, dict):
            print(json.dumps(_mcp_response(None, error={"code": -32600, "message": "invalid request"})), flush=True)
            continue
        request_id = request.get("id")
        method = str(request.get("method") or "")
        params = request.get("params") if isinstance(request.get("params"), dict) else {}
        if request_id is None and method.startswith("notifications/"):
            continue
        if method == "initialize":
            result = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"resources": {}, "tools": {}},
                "serverInfo": {"name": "brigade-skills-readonly", "version": "0"},
            }
            print(json.dumps(_mcp_response(request_id, result=result), sort_keys=True), flush=True)
        elif method == "resources/list":
            print(json.dumps(_mcp_response(request_id, result={"resources": _mcp_resource_items(target)}), sort_keys=True), flush=True)
        elif method == "resources/read":
            uri = str(params.get("uri") or "")
            text, mime_type = _mcp_read_resource(target, uri)
            if text is None:
                print(json.dumps(_mcp_response(request_id, error={"code": -32004, "message": f"resource not found: {uri}"}), sort_keys=True), flush=True)
            else:
                print(json.dumps(_mcp_response(request_id, result={"contents": [{"uri": uri, "mimeType": mime_type, "text": text}]}), sort_keys=True), flush=True)
        elif method == "tools/list":
            print(json.dumps(_mcp_response(request_id, result={"tools": _mcp_tool_specs()}), sort_keys=True), flush=True)
        elif method == "tools/call":
            name = str(params.get("name") or "")
            arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
            payload, failed = _mcp_tool_call(target, name, arguments)
            text = payload if isinstance(payload, str) else json.dumps(payload, indent=2, sort_keys=True)
            result = {"content": [{"type": "text", "text": text}], "isError": failed}
            print(json.dumps(_mcp_response(request_id, result=result), sort_keys=True), flush=True)
        else:
            print(json.dumps(_mcp_response(request_id, error={"code": -32601, "message": f"method not found: {method}"}), sort_keys=True), flush=True)
    return 0


def serve_mcp(*, target: Path, json_output: bool = False, stdio: bool = False) -> int:
    target = target.expanduser().resolve()
    if stdio:
        return _run_mcp_stdio(target)
    payload = _mcp_contract_payload(target)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print("skills MCP resources: ready read_only=true")
    print("resources: skill://registry/{skill_id}/SKILL.md, skill://registry/{skill_id}/skill.json, skill://registry/{skill_id}/compatibility.json, skill://registry/{skill_id}/history.json")
    print("tools: search_skills, get_skill, get_skill_metadata, get_skill_changelog, get_skill_compatibility, get_skill_history, lint_skill")
    print(f"registered_resources: {len(resources)}")
    return 0


def publish(*, target: Path, skill: str, scope: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
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
    out = target / ".brigade" / "skills" / "publish-proposals" / f"{_slug(str(lint_payload['skill_id']))}-{scope}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    _write_json(out, payload)
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
    if path.exists() and not force:
        print(f"error: skill adapter config already exists: {path}", file=sys.stderr)
        return 2
    payload = {
        "description": "Local skill harness adapter overlay. install_path is relative to the workspace and may use {skill_id}.",
        "adapters": {
            "cursor": {"status": "planned", "format": "cursor-skill", "install_path": ".cursor/skills/{skill_id}"},
        },
    }
    _write_json(path, payload)
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
    payload = {"target": str(target), "config_path": str(_adapters_config_path(target)), "adapters": adapters, "count": len(adapters), "include_planned": include_planned}
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
    metadata = lint_payload.get("metadata") if isinstance(lint_payload.get("metadata"), dict) else {}
    supported = metadata.get("supported_harnesses") if isinstance(metadata.get("supported_harnesses"), list) else []
    skill_id = _slug(str(lint_payload.get("skill_id") or skill))
    current_version = str(metadata.get("version") or "0.1.0")
    source_text = ""
    skill_dir = Path(str(lint_payload.get("skill_dir") or ""))
    skill_md = _skill_md_path(skill_dir)
    if skill_md.is_file():
        source_text = skill_md.read_text(errors="replace")
    adapters = []
    for adapter_id, adapter in _adapter_map(target).items():
        install_path = adapter.get("install_path")
        installed = False
        installed_path = None
        if install_path:
            installed_path = str(target / str(install_path).format(skill_id=skill_id))
            installed = Path(installed_path).is_dir()
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
        if lint_payload.get("valid") and adapter.get("status") != "planned" and install_path:
            rendered = _render_skill_text_for_harness(source_text, metadata, skill_id, adapter_id)
            render_fingerprint = _text_fingerprint(rendered)
            rendered_errors = _rendered_skill_validation(rendered, adapter_id)
            blockers.extend(rendered_errors)
        latest_receipt = _latest_install_receipt(target, skill_id, adapter_id)
        history_count = len(_install_history(target, skill_id=skill_id, harness=adapter_id))
        installed_source_fingerprint = latest_receipt.get("source_fingerprint") or latest_receipt.get("fingerprint")
        installed_render_fingerprint = latest_receipt.get("render_fingerprint")
        version_drift = bool(latest_receipt and latest_receipt.get("version") != current_version)
        source_drift = bool(latest_receipt and installed_source_fingerprint != lint_payload.get("fingerprint"))
        render_drift = bool(latest_receipt and render_fingerprint and installed_render_fingerprint != render_fingerprint)
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
                "install_history_count": history_count,
                "receipt_path": str(_installs_root(target) / f"{skill_id}-{adapter_id}.json") if latest_receipt else None,
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
    print(f"skill compatibility: {skill_id}")
    print(f"valid: {str(payload['valid']).lower()}")
    for row in adapters:
        blocked = f" blockers={len(row['blockers'])}" if row["blockers"] else ""
        print(f"- {row['id']} [{row['status']}] supported={row['supported']} installed={row['installed']}{blocked}")
    return 0 if payload["valid"] else 1


def _skill_health_issues(target: Path) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    registry = _iter_registry(target)
    if not registry:
        return issues
    for row in registry:
        metadata = row["metadata"]
        skill_id = _slug(str(metadata.get("id") or Path(row["skill_dir"]).name))
        lint_payload = _lint_payload(target, skill_id)
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
        trust_score = lint_payload.get("trust_score") if isinstance(lint_payload.get("trust_score"), dict) else {}
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
        changelog = lint_payload.get("changelog") if isinstance(lint_payload.get("changelog"), dict) else {}
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
        compat = _compatibility_payload(target, skill_id)
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
            if adapter.get("source_drift") or adapter.get("render_drift"):
                issues.append(
                    {
                        "status": WARN,
                        "name": "skill_install_drift",
                        "issue_type": "install_drift",
                        "skill_id": skill_id,
                        "harness": adapter_id,
                        "detail": f"{adapter_id} installed skill differs from current registry render",
                        "fingerprint": adapter.get("installed_render_fingerprint") or adapter.get("current_render_fingerprint"),
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
            print(f"[{issue.get('status', WARN)}] {issue.get('name')}: {issue.get('skill_id')}{harness}: {issue.get('detail')}")
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
    imported, skipped, skipped_dismissed = work_cmd._append_import_records(target, records)
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


def _proposal_path(target: Path, proposal_id: str) -> Path:
    return _inbox_root(target) / _slug(proposal_id)


def _proposal_meta_path(path: Path) -> Path:
    return path / "proposal.json"


def _proposal_skill_path(path: Path) -> Path:
    return path / "skill"


def _read_proposal(path: Path) -> dict[str, Any]:
    payload = _read_json(_proposal_meta_path(path))
    payload.setdefault("proposal_id", path.name)
    payload.setdefault("path", str(path))
    return payload


def _proposal_paths(target: Path) -> list[Path]:
    root = _inbox_root(target)
    if not root.is_dir():
        return []
    return sorted([path for path in root.iterdir() if path.is_dir()], key=lambda p: p.name)


def _resolve_proposal(target: Path, proposal_id: str) -> tuple[Path | None, dict[str, Any] | None, str | None]:
    matches = [path for path in _proposal_paths(target) if path.name.startswith(_slug(proposal_id))]
    if not matches:
        return None, None, f"skill proposal not found: {proposal_id}"
    if len(matches) > 1:
        return None, None, f"skill proposal id is ambiguous: {proposal_id}"
    return matches[0], _read_proposal(matches[0]), None


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
    proposal_dir = _proposal_path(target, proposal_id)
    if proposal_dir.exists() and not force:
        print(f"error: skill proposal already exists: {proposal_dir}", file=sys.stderr)
        return 2
    skill_dest = _proposal_skill_path(proposal_dir)
    _copy_skill_source(source_dir, skill_dest)
    lint_payload = _lint_payload(target, str(skill_dest))
    proposal = {
        "proposal_id": proposal_dir.name,
        "skill_id": resolved_skill_id,
        "status": "pending",
        "summary": summary or "",
        "source": str(source.expanduser().resolve()),
        "created_at": created,
        "path": str(proposal_dir),
        "skill_path": str(skill_dest),
        "fingerprint": _fingerprint(skill_dest),
        "lint": lint_payload,
    }
    _write_json(_proposal_meta_path(proposal_dir), proposal)
    if json_output:
        print(json.dumps(proposal, indent=2, sort_keys=True))
        return 0 if lint_payload["valid"] else 1
    print(f"skill_proposal: {proposal['proposal_id']}")
    print(f"skill_id: {resolved_skill_id}")
    print(f"status: pending")
    return 0 if lint_payload["valid"] else 1


def inbox_list(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    proposals = [_read_proposal(path) for path in _proposal_paths(target)]
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
    _, proposal, error = _resolve_proposal(target, proposal_id)
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
    path, proposal, error = _resolve_proposal(target, proposal_id)
    if path is None or proposal is None:
        print(f"error: {error}", file=sys.stderr)
        return 2
    proposed = _proposal_skill_path(path) / "SKILL.md"
    existing = _skill_path(target, str(proposal.get("skill_id") or path.name)) / "SKILL.md"
    before = existing.read_text(errors="replace").splitlines() if existing.is_file() else []
    after = proposed.read_text(errors="replace").splitlines() if proposed.is_file() else []
    diff = list(difflib.unified_diff(before, after, fromfile=str(existing), tofile=str(proposed), lineterm=""))
    payload = {"target": str(target), "proposal_id": proposal.get("proposal_id"), "skill_id": proposal.get("skill_id"), "diff": diff}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print("\n".join(diff) if diff else "no diff")
    return 0


def inbox_accept(*, target: Path, proposal_id: str, force: bool = False, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    path, proposal, error = _resolve_proposal(target, proposal_id)
    if path is None or proposal is None:
        print(f"error: {error}", file=sys.stderr)
        return 2
    if proposal.get("status") != "pending" and not force:
        print(f"error: skill proposal is not pending: {proposal.get('status')}", file=sys.stderr)
        return 2
    payload, import_error, rc = _registry_import_payload(
        target=target,
        source=_proposal_skill_path(path),
        skill_id=str(proposal.get("skill_id") or path.name),
        force=force,
    )
    if payload is None:
        print(f"error: {import_error}", file=sys.stderr)
        return rc
    proposal.update({"status": "accepted", "accepted_at": _now(), "registry": payload})
    _write_json(_proposal_meta_path(path), proposal)
    if json_output:
        print(json.dumps(proposal, indent=2, sort_keys=True))
        return rc
    print(f"skill_proposal: {proposal.get('proposal_id')}")
    print("status: accepted")
    print(f"skill_id: {proposal.get('skill_id')}")
    return rc


def inbox_reject(*, target: Path, proposal_id: str, reason: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    path, proposal, error = _resolve_proposal(target, proposal_id)
    if path is None or proposal is None:
        print(f"error: {error}", file=sys.stderr)
        return 2
    proposal.update({"status": "rejected", "rejected_at": _now(), "reason": reason})
    _write_json(_proposal_meta_path(path), proposal)
    if json_output:
        print(json.dumps(proposal, indent=2, sort_keys=True))
        return 0
    print(f"skill_proposal: {proposal.get('proposal_id')}")
    print("status: rejected")
    print(f"reason: {reason}")
    return 0
