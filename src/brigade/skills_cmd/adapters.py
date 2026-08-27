"""Skill adapters, MCP, publish, fleet status, and doctor."""
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

if TYPE_CHECKING:
    import brigade.skills_cmd.install as _install_mod
else:
    from . import install as _install_mod


def _mcp_contract_payload(target: Path) -> dict[str, Any]:
    resources = []
    for row in _registry_mod._iter_registry(target):
        metadata = row["metadata"]
        skill_id = str(metadata.get("id") or Path(row["skill_dir"]).name)
        lint_payload = _search_validate_mod._lint_payload(target, f"registry:{skill_id}")
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
    skill_id = _registry_mod._slug(skill_id)
    # Every registry read below goes through the held state-root anchor; a
    # refused or absent entry yields (None, None) instead of outside content.
    try:
        with _registry_mod._held_state_root(target) as anchor:
            if name == "SKILL.md":
                raw = _registry_mod._read_state_file_bytes(anchor, "skills", "registry", skill_id, "SKILL.md")
                return (raw.decode("utf-8", errors="replace"), "text/markdown") if raw is not None else (None, None)
            if name == "skill.json":
                metadata = _registry_mod._anchored_registry_metadata(anchor, skill_id)
                metadata.setdefault("id", skill_id)
                return json.dumps(metadata, indent=2, sort_keys=True) + "\n", "application/json"
            if name == "CHANGELOG.md":
                metadata = _registry_mod._anchored_registry_metadata(anchor, skill_id)
                text = _registry_mod._anchored_entry_changelog_text(
                    anchor, "skills", "registry", skill_id, metadata=metadata
                )
                return (text, "text/markdown") if text is not None else (None, None)
            if name == "compatibility.json":
                return (
                    json.dumps(
                        _search_validate_mod._compatibility_payload(target, f"registry:{skill_id}"),
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n",
                    "application/json",
                )
            if name == "history.json":
                payload = {"skill_id": skill_id, "history": _install_mod._install_history(target, skill_id=skill_id)}
                return json.dumps(payload, indent=2, sort_keys=True) + "\n", "application/json"
    except _registry_mod.SkillsStatePathError:
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
        for row in _registry_mod._iter_registry(target):
            metadata = row["metadata"]
            haystack = " ".join(str(metadata.get(key, "")) for key in ("id", "title", "description")).casefold()
            if not terms or all(term in haystack for term in terms):
                matches.append(metadata)
        return {"query": query, "count": len(matches), "skills": matches}, False
    skill_id = _registry_mod._slug(str(arguments.get("skill_id") or ""))
    if not skill_id:
        return {"error": "skill_id is required"}, True
    if name == "get_skill":
        text, _ = _mcp_read_resource(target, f"skill://registry/{skill_id}/SKILL.md")
        return text or "", text is None
    if name == "get_skill_metadata":
        try:
            with _registry_mod._held_state_root(target) as anchor:
                metadata = _registry_mod._anchored_registry_metadata(anchor, skill_id)
        except _registry_mod.SkillsStatePathError:
            metadata = {}
        metadata.setdefault("id", skill_id)
        return metadata, False
    if name == "get_skill_changelog":
        text, _ = _mcp_read_resource(target, f"skill://registry/{skill_id}/CHANGELOG.md")
        return text or "", text is None
    if name == "get_skill_compatibility":
        return _search_validate_mod._compatibility_payload(target, f"registry:{skill_id}"), False
    if name == "get_skill_history":
        return {"skill_id": skill_id, "history": _install_mod._install_history(target, skill_id=skill_id)}, False
    if name == "lint_skill":
        return _search_validate_mod._lint_payload(target, f"registry:{skill_id}"), False
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
    scope_slug = _registry_mod._slug(scope)
    if not scope_slug:
        print(f"error: invalid publish scope: {scope}", file=sys.stderr)
        return 2
    lint_payload = _search_validate_mod._lint_payload(target, skill)
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
    proposal_name = f"{_registry_mod._slug(str(lint_payload['skill_id']))}-{scope_slug}.json"
    out = target / ".brigade" / "skills" / "publish-proposals" / proposal_name
    try:
        with _registry_mod._held_state_root(target) as state_anchor:
            _registry_mod._require_plain_state_dirs(state_anchor, "skills", "publish-proposals")
            _registry_mod._write_state_file(
                state_anchor, "skills", "publish-proposals", proposal_name, data=_registry_mod._json_bytes(payload)
            )
    except _registry_mod.SkillsStatePathError as exc:
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
    path = _registry_mod._adapters_config_path(target)
    # Existence is answered through the anchor, never by pathname probe.
    try:
        with _registry_mod._held_state_root(target) as anchor:
            config_exists = _registry_mod._state_file_exists(anchor, "skills", "adapters.json")
    except _registry_mod.SkillsStatePathError as exc:
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
        with _registry_mod._held_state_root(target) as anchor:
            _registry_mod._write_state_file(anchor, "skills", "adapters.json", data=_registry_mod._json_bytes(payload))
    except _registry_mod.SkillsStatePathError as exc:
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
    adapter_map = _install_mod._adapter_map(target)
    adapters = [
        {"id": adapter_id, **data}
        for adapter_id, data in adapter_map.items()
        if include_planned or data["status"] in {"built-in", "local"}
    ]
    payload = {
        "target": str(target),
        "config_path": str(_registry_mod._adapters_config_path(target)),
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
    adapter = _install_mod._adapter_map(target).get(adapter_id)
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


def _fleet_skill_ids(target: Path) -> list[str]:
    bundled_root = template_root() / "skills"
    bundled = (
        {path.name for path in bundled_root.iterdir() if path.is_dir() and _registry_mod._skill_md_path(path).is_file()}
        if bundled_root.is_dir()
        else set()
    )
    registry = {
        _registry_mod._slug(str(row["metadata"].get("id") or Path(str(row["skill_dir"])).name))
        for row in _registry_mod._iter_registry(target)
    }
    return sorted(bundled | registry)


def _plain_files_under_anchor(anchor: _registry_mod._StateRootAnchor, *relative: str) -> list[str] | None:
    """List plain regular-file names under an anchored state path.

    Mirrors :func:`_plain_subdirs_under_anchor`: ``None`` for a missing
    directory, refusal for symlinked components, symlinked entries skipped.
    """
    if not _registry_mod._HAS_DESCRIPTOR_ANCHOR:
        base = (anchor.workspace / ".brigade").joinpath(*relative)
        current = anchor.workspace / ".brigade"
        for part in relative:
            current = current / part
            if not current.exists():
                return None
            if current.is_symlink() or _registry_mod._is_reparse_point(current) or not current.is_dir():
                raise _registry_mod.SkillsStatePathError(f"skills state path must be plain directories: {current}")
        names: list[str] = []
        for entry in sorted(base.iterdir(), key=lambda item: item.name):
            if entry.is_symlink():
                continue
            try:
                st = entry.lstat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise _registry_mod.SkillsStatePathError(
                    f"skills state entry could not be inspected safely: {entry}"
                ) from exc
            if stat_module.S_ISREG(st.st_mode):
                names.append(entry.name)
        return names
    fd, opened = _registry_mod._anchor_open_chain(anchor, *relative, missing_ok=True)
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
                raise _registry_mod.SkillsStatePathError(
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
        with _registry_mod._held_state_root(target) as anchor:
            names = _plain_files_under_anchor(anchor, "skills", "installs")
            if not names:
                return receipts
            install_targets = set(_install_mod._install_targets(target))
            for name in names:
                if not name.endswith(".json"):
                    continue
                raw = _registry_mod._read_state_file_bytes(anchor, "skills", "installs", name)
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
                    and name == f"{_registry_mod._slug(skill_id)}-{harness}.json"
                ):
                    receipts[(_registry_mod._slug(skill_id), harness)] = parsed
    except _registry_mod.SkillsStatePathError:
        return {}
    return receipts


def _fleet_copy_keys(target: Path) -> list[tuple[str, str]]:
    keys = set(_fleet_receipts(target))
    skill_ids = _fleet_skill_ids(target)
    for harness in sorted(_install_mod._install_targets(target)):
        if harness == "hermes":
            continue
        probe, _probe_escapes = _install_mod._evaluate_install_dir(target, harness, "__brigade_probe__")
        root = probe.parent
        if root.is_dir():
            for path in sorted(item for item in root.iterdir() if item.is_dir()):
                if _registry_mod._skill_md_path(path).is_file():
                    keys.add((_registry_mod._slug(path.name), harness))
        for skill_id in skill_ids:
            install_dir, escapes = _install_mod._evaluate_install_dir(target, harness, skill_id)
            if not escapes and install_dir.exists():
                keys.add((skill_id, harness))
    return sorted(keys)


def _fleet_source_selector(skill_id: str, receipt: dict[str, Any]) -> str | None:
    if not receipt:
        return skill_id
    source = raw_source if isinstance((raw_source := receipt.get("source")), dict) else {}
    kind = source.get("kind")
    identity = source.get("identity")
    if kind == "brigade-bundle" and identity == f"{_registry_mod.BUNDLED_SOURCE_PREFIX}{skill_id}":
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
        install_dir, escapes = _install_mod._evaluate_install_dir(target, harness, skill_id)
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
        lint_payload = (
            _search_validate_mod._lint_payload(target, selector) if selector is not None else {"valid": False}
        )
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
            source_text = (
                _registry_mod._anchored_registry_text(
                    target, _registry_mod._slug(selector.removeprefix("registry:")), "SKILL.md"
                )
                or ""
            )
        else:
            source_text = _registry_mod._skill_md_path(source_dir).read_text(encoding="utf-8", errors="replace")
        supported = raw_supported if isinstance((raw_supported := metadata.get("supported_harnesses")), list) else []
        source = raw_source if isinstance((raw_source := lint_payload.get("source")), dict) else {}
        supported_state = harness in supported or not supported
        installed_dir = install_dir
        rendered = _registry_mod._render_skill_text_for_harness(source_text, metadata, skill_id, harness)
        drift = _install_mod._drift_payload(
            target=target,
            skill_id=skill_id,
            harness=harness,
            lint_payload=lint_payload,
            rendered=rendered,
            installed_dir=installed_dir,
        )
        if not drift["receipt_known"]:
            status = "unknown"
        elif _registry_mod._skill_md_path(installed_dir).is_file() and not supported_state:
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
    registry = _registry_mod._iter_registry(target)
    if not registry:
        return issues
    for row in registry:
        metadata = row["metadata"]
        skill_id = _registry_mod._slug(str(metadata.get("id") or Path(row["skill_dir"]).name))
        lint_payload = _search_validate_mod._lint_payload(target, f"registry:{skill_id}")
        for error in lint_payload.get("errors", []):
            issues.append(
                {
                    "status": _registry_mod.FAIL,
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
                    "status": _registry_mod.WARN,
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
                    "status": _registry_mod.WARN,
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
                    "status": _registry_mod.WARN,
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
                    "status": _registry_mod.WARN,
                    "name": "skill_changelog_missing",
                    "issue_type": "changelog_missing",
                    "skill_id": skill_id,
                    "detail": "skill has no CHANGELOG.md or changelog_path metadata",
                    "fingerprint": lint_payload.get("fingerprint"),
                }
            )
        compat = _search_validate_mod._compatibility_payload(target, f"registry:{skill_id}")
        for adapter in compat.get("adapters", []):
            if not isinstance(adapter, dict):
                continue
            adapter_id = str(adapter.get("id") or "")
            if adapter.get("version_drift"):
                issues.append(
                    {
                        "status": _registry_mod.WARN,
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
                        "status": _registry_mod.WARN,
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
    registry = _registry_mod._iter_registry(target)
    issues = _skill_health_issues(target)
    return {
        "target": str(target),
        "registry_path": str(_registry_mod._registry_root(target)),
        "skill_count": len(registry),
        "valid": not any(issue.get("status") == _registry_mod.FAIL for issue in issues),
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
                f"[{issue.get('status', _registry_mod.WARN)}] {issue.get('name')}: {issue.get('skill_id')}{harness}: {issue.get('detail')}"
            )
    else:
        print("[ok] skill_registry: no issues")
    print(f"skill_issues: {payload['issue_count']}")
    return 0 if payload["valid"] else 1
