"""Reviewed skill registry commands.

Skills are treated as versioned workflow code: imports land in a local Brigade
registry, lint checks provenance and injection risk, and installs materialize
reviewed packs into harness-specific folders.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .untrusted import scan_untrusted

HARNESS_TARGETS = ("codex", "claude", "opencode", "gemini", "openclaw", "hermes", "mcp")
INSTALL_TARGETS = (*HARNESS_TARGETS, "all")
TRUST_LEVELS = ("unreviewed", "workspace", "team", "public")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9._-]+", "-", value.strip().lower()).strip("-")
    return slug or "skill"


def _registry_root(target: Path) -> Path:
    return target / ".brigade" / "skills" / "registry"


def _skill_path(target: Path, skill_id: str) -> Path:
    return _registry_root(target) / _slug(skill_id)


def _metadata_path(skill_dir: Path) -> Path:
    return skill_dir / "skill.json"


def _skill_md_path(skill_dir: Path) -> Path:
    return skill_dir / "SKILL.md"


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


def _load_skill(target: Path, skill_or_path: str) -> tuple[Path, dict[str, Any]]:
    candidate = Path(skill_or_path).expanduser()
    if candidate.exists():
        skill_dir = candidate if candidate.is_dir() else candidate.parent
    else:
        skill_dir = _skill_path(target, skill_or_path)
    metadata = _read_json(_metadata_path(skill_dir))
    metadata.setdefault("id", skill_dir.name)
    return skill_dir, metadata


def _lint_payload(target: Path, skill_or_path: str) -> dict[str, Any]:
    skill_dir, metadata = _load_skill(target, skill_or_path)
    skill_md = _skill_md_path(skill_dir)
    errors: list[str] = []
    warnings: list[str] = []
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
    injection = scan_untrusted(text)
    if injection.flagged:
        warnings.append("SKILL.md contains injection-like text; review as untrusted content before installing")
    return {
        "target": str(target),
        "skill_dir": str(skill_dir),
        "skill_id": metadata.get("id") or skill_dir.name,
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "injection": {
            "flagged": injection.flagged,
            "count": injection.count,
            "markers": injection.markers,
        },
        "metadata": metadata,
        "fingerprint": _fingerprint(skill_dir) if skill_dir.is_dir() else None,
    }


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
    if not source.exists():
        print(f"error: source not found: {source}", file=sys.stderr)
        return 2
    source_dir = source if source.is_dir() else source.parent
    source_skill_md = source_dir / "SKILL.md" if source.is_dir() else source
    if source_skill_md.name != "SKILL.md" or not source_skill_md.is_file():
        print("error: skill import source must be a SKILL.md file or a directory containing SKILL.md", file=sys.stderr)
        return 2
    incoming_metadata = _read_json(source_dir / "skill.json")
    resolved_id = _slug(skill_id or str(incoming_metadata.get("id") or source_dir.name))
    dest = _skill_path(target, resolved_id)
    if dest.exists() and not force:
        print(f"error: skill already exists: {dest}", file=sys.stderr)
        return 2
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source_dir, dest)
    else:
        dest.mkdir(parents=True)
        shutil.copy2(source_skill_md, dest / "SKILL.md")
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
    _metadata_path(dest).write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")
    lint_payload = _lint_payload(target, resolved_id)
    payload = {"target": str(target), "skill_id": resolved_id, "skill_dir": str(dest), "lint": lint_payload}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if lint_payload["valid"] else 1
    print(f"skill_import: {resolved_id}")
    print(f"skill_dir: {dest}")
    print(f"fingerprint: {metadata['fingerprint']}")
    for warning in lint_payload["warnings"]:
        print(f"warning: {warning}")
    for error in lint_payload["errors"]:
        print(f"error: {error}")
    return 0 if lint_payload["valid"] else 1


def lint(*, target: Path, skill: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    payload = _lint_payload(target, skill)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["valid"] else 1
    print(f"skill lint: {payload['skill_id']}")
    print(f"valid: {str(payload['valid']).lower()}")
    for warning in payload["warnings"]:
        print(f"warning: {warning}")
    for error in payload["errors"]:
        print(f"error: {error}")
    return 0 if payload["valid"] else 1


def _install_dir(workspace: Path, harness: str, skill_id: str) -> Path:
    if harness == "codex":
        return workspace / ".codex" / "skills" / skill_id
    if harness == "claude":
        return workspace / ".claude" / "skills" / skill_id
    if harness == "opencode":
        return workspace / ".opencode" / "skills" / skill_id
    if harness == "gemini":
        return workspace / ".agents" / "skills" / skill_id
    if harness == "openclaw":
        return workspace / ".openclaw" / "skills" / skill_id
    if harness == "hermes":
        return workspace / ".hermes" / "skills" / skill_id
    return workspace / ".brigade" / "skills" / "mcp-resources" / skill_id


def install(
    *,
    workspace: Path,
    skill: str,
    harness: str,
    force: bool = False,
    json_output: bool = False,
) -> int:
    workspace = workspace.expanduser().resolve()
    if harness not in INSTALL_TARGETS:
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
    targets = HARNESS_TARGETS if harness == "all" else (harness,)
    receipts: list[dict[str, Any]] = []
    for install_target in targets:
        dest = _install_dir(workspace, install_target, skill_id)
        if dest.exists() and not force:
            print(f"error: installed skill already exists: {dest}", file=sys.stderr)
            return 2
        if dest.exists():
            shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source_dir, dest)
        receipt = {
            "workspace": str(workspace),
            "skill_id": skill_id,
            "target": install_target,
            "installed_dir": str(dest),
            "installed_at": _now(),
            "fingerprint": lint_payload.get("fingerprint"),
        }
        receipt_path = workspace / ".brigade" / "skills" / "installs" / f"{skill_id}-{install_target}.json"
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
        receipt["receipt_path"] = str(receipt_path)
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


def serve_mcp(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    payload = {
        "target": str(target),
        "status": "planned",
        "resources": ["skill://registry/{skill_id}/SKILL.md", "skill://registry/{skill_id}/skill.json"],
        "tools": ["search_skills", "get_skill", "install_skill", "publish_skill", "fork_skill", "lint_skill"],
        "detail": "MCP serving is intentionally not started by this local planning command yet.",
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print("skills MCP server: planned")
    print("resources: skill://registry/{skill_id}/SKILL.md, skill://registry/{skill_id}/skill.json")
    print("tools: search_skills, get_skill, install_skill, publish_skill, fork_skill, lint_skill")
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
    out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    payload["proposal_path"] = str(out)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"skill_publish_proposal: {payload['skill_id']}")
    print(f"scope: {scope}")
    print(f"status: {payload['status']}")
    print(f"proposal: {out}")
    return 0
