"""Search, import, lint, and compatibility commands."""
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

from . import registry
from . import packs as _packs_mod

if TYPE_CHECKING:
    import brigade.skills_cmd.install as _install_mod
else:
    from . import install as _install_mod


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
        "source": registry._source_identity(
            skill_dir=display_dir,
            skill_id=registry._slug(display_dir.name),
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
    resolved_id = registry._slug(str(metadata.get("id") or display_dir.name))
    staged_source = raw_staged_source if isinstance((raw_staged_source := payload.get("source")), dict) else {}
    source = registry._source_identity(skill_dir=display_dir, skill_id=resolved_id, kind="registry", reviewed=False)
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
    display_dir = registry._skill_path(target, skill_id)
    try:
        with registry._held_state_root(target) as anchor:
            snapshot_dirs, snapshot_files = registry._read_registry_entry_tree(anchor, skill_id)
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
                registry._write_snapshot_tree(snapshot_dirs, snapshot_files, staged_dir)
                payload = _lint_path_payload(staged_root, str(staged_dir), harness=harness, mode=mode, contain=True)
                _finalize_staged_registry_lint(payload, target, display_dir, staged_dir)
    except registry.SkillsStatePathError as exc:
        return _unreadable_source_lint_payload(target, display_dir, str(exc))
    return payload


def _lint_payload(
    target: Path, skill_or_path: str, harness: str | None = None, *, mode: str = "lenient"
) -> dict[str, Any]:
    requested = str(skill_or_path)
    if requested.startswith("registry:"):
        selector_id = requested.removeprefix("registry:")
        return _lint_registry_payload(target, registry._slug(selector_id), harness=harness, mode=mode)
    kind = _packs_mod._state_root_selector_kind(target, requested)
    if kind == "registry":
        parts = _packs_mod._lexical_state_root_parts(target, Path(requested))
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
    from .. import agent_skill_format

    skill_dir, metadata, source = registry._load_skill(target, skill_or_path)
    skill_md = registry._skill_md_path(skill_dir)
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
    if metadata.get("trust_level") and metadata["trust_level"] not in registry.TRUST_LEVELS:
        errors.append(f"unknown trust_level: {metadata['trust_level']}")
    for key in ("required_tools", "required_mcp_servers", "supported_harnesses", "tests"):
        if key in metadata and not isinstance(metadata[key], list):
            errors.append(f"metadata {key} must be a list")
    if "obligations" in metadata:
        from .. import skill_obligations

        _, obligation_errors = skill_obligations.parse_obligations(metadata)
        errors.extend(obligation_errors)
    format_result = agent_skill_format.validate(skill_dir, mode=mode)
    errors.extend(format_result.errors)
    warnings.extend(format_result.diagnostics)
    if harness is not None:
        adapters = _install_mod._adapter_map(target)
        adapter = adapters.get(harness)
        if adapter is None:
            errors.append(f"unknown harness adapter: {harness}")
        elif adapter.get("status") == "planned":
            errors.append(f"harness adapter is planned: {harness}")
        elif not adapter.get("install_path"):
            errors.append(f"harness adapter has no install path: {harness}")
        elif text:
            skill_id = registry._slug(str(metadata.get("id") or skill_dir.name))
            rendered = registry._render_skill_text_for_harness(text, metadata, skill_id, harness)
            render_errors = registry._rendered_skill_validation(rendered, harness)
            errors.extend(render_errors)
    injection = scan_untrusted(text)
    if injection.flagged:
        warnings.append("SKILL.md contains injection-like text; review as untrusted content before installing")
    source = dict(source)
    source["skill_version"] = str(metadata.get("version") or "0.1.0")
    source["fingerprint"] = registry._fingerprint(skill_dir) if skill_dir.is_dir() else None
    source["metadata_fingerprint"] = registry._file_fingerprint(registry._metadata_path(skill_dir))
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
        registry._changelog_payload(skill_dir, metadata, contain=contain)
        if skill_dir.is_dir()
        else {"present": False, "path": None, "fingerprint": None, "headings": []}
    )
    payload["trust_score"] = (
        registry._trust_score_payload(skill_dir, metadata, payload, source, contain=contain)
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


def search(*, target: Path, query: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    terms = [term.casefold() for term in query.split() if term]
    matches: list[dict[str, Any]] = []
    for row in registry._iter_registry(target):
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
    payload, error, rc = registry._registry_import_payload(target=target, source=source, skill_id=skill_id, force=force)
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


def _compatibility_payload(target: Path, skill: str) -> dict[str, Any]:
    lint_payload = _lint_payload(target, skill)
    metadata = raw_metadata if isinstance((raw_metadata := lint_payload.get("metadata")), dict) else {}
    supported = raw_supported if isinstance((raw_supported := metadata.get("supported_harnesses")), list) else []
    skill_id = registry._slug(str(lint_payload.get("skill_id") or skill))
    current_version = str(metadata.get("version") or "0.1.0")
    source_text = ""
    skill_dir = Path(str(lint_payload.get("skill_dir") or ""))
    skill_md = registry._skill_md_path(skill_dir)
    if skill.startswith("registry:"):
        # Anchored registry read; a refused entry contributes no text.
        source_text = (
            registry._anchored_registry_text(target, registry._slug(skill.removeprefix("registry:")), "SKILL.md") or ""
        )
    elif skill_md.is_file():
        source_text = skill_md.read_text(encoding="utf-8", errors="replace")
    adapters = []
    for adapter_id, adapter in _install_mod._adapter_map(target).items():
        install_path = adapter.get("install_path")
        installed = False
        installed_path = None
        if install_path:
            installed_dir = _install_mod._install_dir(target, adapter_id, skill_id)
            installed_path = str(installed_dir)
            installed = registry._skill_md_path(installed_dir).is_file()
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
            rendered = registry._render_skill_text_for_harness(source_text, metadata, skill_id, adapter_id)
            render_fingerprint = registry._text_fingerprint(rendered)
            rendered_errors = registry._rendered_skill_validation(rendered, adapter_id)
            blockers.extend(rendered_errors)
        drift = (
            _install_mod._drift_payload(
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
        latest_receipt = (
            drift.get("receipt") if drift else _install_mod._latest_install_receipt(target, skill_id, adapter_id)
        )
        latest_receipt = latest_receipt if isinstance(latest_receipt, dict) else {}
        if not _install_mod._valid_receipt_contract(latest_receipt, skill_id=skill_id, harness=adapter_id):
            latest_receipt = {}
        history_count = len(_install_mod._install_history(target, skill_id=skill_id, harness=adapter_id))
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
                "receipt_path": str(registry._installs_root(target) / f"{skill_id}-{adapter_id}.json")
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
