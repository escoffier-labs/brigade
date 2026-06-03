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

HARNESS_ADAPTERS: dict[str, dict[str, Any]] = {
    "codex": {"status": "built-in", "format": "codex-skill", "install_path": ".codex/skills/{skill_id}"},
    "claude": {"status": "built-in", "format": "claude-skill", "install_path": ".claude/skills/{skill_id}"},
    "opencode": {"status": "built-in", "format": "opencode-skill", "install_path": ".opencode/skills/{skill_id}"},
    "gemini": {"status": "built-in", "format": "gemini-agent-skill", "install_path": ".agents/skills/{skill_id}"},
    "openclaw": {"status": "built-in", "format": "openclaw-skill", "install_path": ".openclaw/skills/{skill_id}"},
    "hermes": {"status": "built-in", "format": "hermes-skill", "install_path": ".hermes/skills/{skill_id}"},
    "mcp": {"status": "built-in", "format": "mcp-resource", "install_path": ".brigade/skills/mcp-resources/{skill_id}"},
    "antigravity": {"status": "planned", "format": "adapter-needed", "install_path": None},
    "pi": {"status": "planned", "format": "adapter-needed", "install_path": None},
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
    targets = install_targets if harness == "all" else (harness,)
    receipts: list[dict[str, Any]] = []
    for install_target in targets:
        dest = _install_dir(workspace, install_target, skill_id)
        if dest.exists() and not force:
            print(f"error: installed skill already exists: {dest}", file=sys.stderr)
            return 2
        rollback_snapshot: str | None = None
        if dest.exists():
            rollback_dir = _rollback_root(workspace, skill_id, install_target) / _now().replace(":", "").replace("+", "Z").replace(".", "-")
            shutil.copytree(dest, rollback_dir)
            rollback_snapshot = str(rollback_dir)
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
            "rollback_snapshot": rollback_snapshot,
        }
        receipt_path = workspace / ".brigade" / "skills" / "installs" / f"{skill_id}-{install_target}.json"
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        _write_json(receipt_path, receipt)
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
            "antigravity": {"status": "planned", "format": "adapter-needed", "install_path": None},
            "pi": {"status": "planned", "format": "adapter-needed", "install_path": None},
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


def compatibility(*, target: Path, skill: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    lint_payload = _lint_payload(target, skill)
    metadata = lint_payload.get("metadata") if isinstance(lint_payload.get("metadata"), dict) else {}
    supported = metadata.get("supported_harnesses") if isinstance(metadata.get("supported_harnesses"), list) else []
    skill_id = _slug(str(lint_payload.get("skill_id") or skill))
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
        adapters.append(
            {
                "id": adapter_id,
                "status": adapter.get("status"),
                "format": adapter.get("format"),
                "supported": supported_state,
                "installed": installed,
                "installed_path": installed_path,
                "blockers": blockers,
            }
        )
    payload = {
        "target": str(target),
        "skill_id": skill_id,
        "valid": bool(lint_payload.get("valid")),
        "fingerprint": lint_payload.get("fingerprint"),
        "adapters": adapters,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["valid"] else 1
    print(f"skill compatibility: {skill_id}")
    print(f"valid: {str(payload['valid']).lower()}")
    for row in adapters:
        blocked = f" blockers={len(row['blockers'])}" if row["blockers"] else ""
        print(f"- {row['id']} [{row['status']}] supported={row['supported']} installed={row['installed']}{blocked}")
    return 0 if payload["valid"] else 1


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
