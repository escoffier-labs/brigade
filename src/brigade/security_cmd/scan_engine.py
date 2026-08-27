"""Read-only security scanner for agent workspaces."""
# ruff: noqa: E402,F401,F403,F811,F821

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import urlparse

from .. import localio, work_cmd
from ..selection import WRITER_INBOXES
from ..untrusted import PROMPT_INJECTION_RE, scan_untrusted
from ..localio import read_json_dict as _read_json, utc_now_iso_z as _utc_iso, write_json as _write_json

from . import scan_detectors as _scan_detectors_mod
from . import template_audit_ops as _template_audit_ops_mod
from .config import load_config
from .models import (
    AUTO_APPROVE_RE,
    DESTRUCTIVE_RE,
    ENV_ASSIGNMENT_RE,
    ENV_DUMP_RE,
    FileClassification,
    HTTP_MCP_RE,
    PLAINTEXT_PASSWORD_RE,
    POLICIES,
    REMOTE_SHELL_RE,
    SECRET_VALUE_RE,
    SECURITY_CHECKS,
    SESSION_CHAT_PARTS,
    SEVERITY_ORDER,
    SKIP_DIRS,
    SKIP_PREFIXES,
    SecurityConfig,
    TEXT_SUFFIXES,
    UNPINNED_NPX_RE,
    _coalesce_findings,
    _env_assignment_value,
    _escape_finding_markdown,
    _is_attribute_secret_value,
    _is_runtime_secret_value,
    _is_secret_path_assignment,
    _is_security_scanner_literal,
    _match_value_is_quoted,
    _read_fingerprint_migration_map,
    _sanitize_finding_for_output,
    _should_skip_harness_wiring_path,
    _strip_internal_finding_keys,
    config_path,
)


def _rule_id(category: str, title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return f"{category}.{slug or 'finding'}"


def _scan_path_selected(rel_path: str, *, include_paths: tuple[str, ...], exclude_paths: tuple[str, ...]) -> bool:
    if include_paths and not _path_matches_any(rel_path, include_paths):
        return False
    return not (exclude_paths and _path_matches_any(rel_path, exclude_paths))


def _candidate_scan_roots(target: Path, include_paths: tuple[str, ...]) -> list[Path]:
    if not include_paths:
        return [target]
    roots: list[Path] = []
    seen: set[Path] = set()
    for pattern in include_paths:
        clean = pattern.strip().replace("\\", "/").strip("/")
        if not clean:
            continue
        root = (target / clean).resolve()
        try:
            root.relative_to(target)
        except ValueError:
            continue
        if root.exists() and root not in seen:
            roots.append(root)
            seen.add(root)
    return sorted(roots)


def _should_scan_file(path: Path) -> bool:
    if path.suffix.lower() not in TEXT_SUFFIXES:
        return False
    try:
        return path.stat().st_size <= 500_000
    except OSError:
        return False


def _iter_scan_files(
    target: Path, *, include_paths: tuple[str, ...] = (), exclude_paths: tuple[str, ...] = ()
) -> list[Path]:
    paths: list[Path] = []
    for root in _candidate_scan_roots(target, include_paths):
        if root.is_file():
            rel = str(root.relative_to(target))
            rel_parts = root.relative_to(target).parts
            if (
                not _scan_path_selected(rel, include_paths=include_paths, exclude_paths=exclude_paths)
                or any(part in SKIP_DIRS for part in rel_parts)
                or any(rel_parts[: len(prefix)] == prefix for prefix in SKIP_PREFIXES)
                or not _should_scan_file(root)
            ):
                continue
            paths.append(root)
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            current = Path(dirpath)
            try:
                rel_current = current.relative_to(target)
            except ValueError:
                dirnames[:] = []
                continue
            rel_parts = rel_current.parts
            if rel_parts and (
                any(part in SKIP_DIRS for part in rel_parts)
                or any(rel_parts[: len(prefix)] == prefix for prefix in SKIP_PREFIXES)
                or not _scan_path_selected(str(rel_current), include_paths=include_paths, exclude_paths=exclude_paths)
            ):
                dirnames[:] = []
                continue
            kept_dirs: list[str] = []
            for dirname in sorted(dirnames):
                child = current / dirname
                try:
                    child_rel = child.relative_to(target)
                except ValueError:
                    continue
                child_parts = child_rel.parts
                if (
                    dirname in SKIP_DIRS
                    or any(child_parts[: len(prefix)] == prefix for prefix in SKIP_PREFIXES)
                    or not _scan_path_selected(str(child_rel), include_paths=include_paths, exclude_paths=exclude_paths)
                ):
                    continue
                kept_dirs.append(dirname)
            dirnames[:] = kept_dirs
            for filename in sorted(filenames):
                path = current / filename
                rel = str(path.relative_to(target))
                if _scan_path_selected(
                    rel,
                    include_paths=include_paths,
                    exclude_paths=exclude_paths,
                ) and _should_scan_file(path):
                    paths.append(path)
    paths.sort()
    unique_paths: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        if path in seen:
            continue
        unique_paths.append(path)
        seen.add(path)
    return unique_paths


def _surface_for(path: Path, target: Path) -> str:
    rel = path.relative_to(target)
    parts = rel.parts
    if _is_session_chat_path(path, target):
        return "session-chat"
    if "memory-handoffs" in parts:
        return "handoff-inbox"
    if "skills" in parts and path.name == "SKILL.md":
        return "skill"
    if "commands" in parts and path.suffix.lower() == ".md":
        return "slash-command"
    if ("agents" in parts or "subagents" in parts) and path.suffix.lower() == ".md":
        return "subagent"
    if any(part in {"wrappers", "tools", "tool-wrappers"} for part in parts) and path.suffix.lower() in {
        ".sh",
        ".py",
        ".js",
        ".ts",
        ".toml",
        ".json",
        ".md",
    }:
        return "tool-wrapper"
    if parts and parts[0] == ".brigade":
        if path.name == "tools.toml" or "tools" in parts:
            return "tool-wrapper"
        return "brigade"
    if parts and parts[0] == ".codex":
        if "skills" in parts:
            return "skill"
        return "codex"
    if parts and parts[0] == ".claude":
        if "commands" in parts:
            return "slash-command"
        if "agents" in parts or "subagents" in parts:
            return "subagent"
        return "claude"
    if "mcp" in path.name.lower():
        return "mcp"
    if parts and parts[0] in {"hooks", "scripts"}:
        if any(part in {"wrappers", "tools", "tool-wrappers"} for part in parts):
            return "tool-wrapper"
        return "automation"
    if path.name in {"AGENTS.md", "CLAUDE.md", "SAFETY_RULES.md", "INSTALL_FOR_AGENTS.md"}:
        return "agent-instructions"
    return "repo"


def _confidence_for(path: Path, target: Path) -> str:
    rel = path.relative_to(target)
    parts = rel.parts
    if _is_session_chat_path(path, target):
        return "runtime"
    if parts and parts[0] == "src" and "templates" in parts:
        return "template"
    if parts and parts[0] in {".brigade", ".claude", ".codex", "hooks", "scripts"}:
        return "runtime"
    if path.name in {"AGENTS.md", "CLAUDE.md", "SAFETY_RULES.md", "INSTALL_FOR_AGENTS.md"}:
        return "runtime"
    return "repo"


def _classification_for(path: Path, target: Path) -> FileClassification:
    return FileClassification(surface=_surface_for(path, target), confidence=_confidence_for(path, target))


def _is_session_chat_path(path: Path, target: Path) -> bool:
    try:
        rel = path.relative_to(target)
    except ValueError:
        rel = path
    parts = {part.lower() for part in rel.parts}
    if parts & SESSION_CHAT_PARTS:
        return True
    name = path.name.lower()
    return any(token in name for token in ("chat", "conversation", "session", "transcript"))


def _secret_response_options(path: Path, target: Path) -> list[str]:
    options = [
        "move_to_env: Store active app secrets in a gitignored .env file or environment variable, then commit only a placeholder.",
        "scrub_or_rotate: Remove the unredacted value from tracked files and rotate the credential if it was committed, shared, or exposed.",
        "keepass_review: Show the redacted finding to the operator so they can save the real value in KeePass before deciding what to remove.",
    ]
    if _is_session_chat_path(path, target):
        options.insert(
            1,
            "scrub_session_chat: Redact, archive, or delete the session/chat transcript before sharing or syncing the workspace.",
        )
    return options


def _normalized_matched_content(evidence: str) -> str:
    return _template_audit_ops_mod._short(evidence, limit=96)


def _fingerprint(
    *,
    rule_id: str,
    rel_path: str,
    matched_content: str,
    occurrence: int,
    duplicate_count: int = 1,
) -> str:
    if duplicate_count > 1:
        stable = "\n".join([rule_id, rel_path, matched_content, str(duplicate_count), str(occurrence)])
    else:
        stable = "\n".join([rule_id, rel_path, matched_content, str(occurrence)])
    return hashlib.sha256(stable.encode()).hexdigest()[:16]


def _legacy_fingerprint(*, category: str, title: str, rel_path: str, line: int, evidence: str) -> str:
    stable = "\n".join([category, title, rel_path, str(line), _template_audit_ops_mod._short(evidence, limit=96)])
    return hashlib.sha256(stable.encode()).hexdigest()[:16]


def _finding_duplicate_count(finding: dict[str, Any]) -> int:
    duplicate_count = finding.get("duplicate_count")
    if isinstance(duplicate_count, int) and duplicate_count > 0:
        return duplicate_count
    return 1


def _safe_fingerprint_aliases(finding: dict[str, Any]) -> tuple[str, ...]:
    aliases: list[str] = []
    primary = str(finding.get("fingerprint") or "")
    if primary:
        aliases.append(primary)
    if _finding_duplicate_count(finding) <= 1:
        legacy = str(finding.get("legacy_fingerprint") or "")
        if legacy and legacy not in aliases:
            aliases.append(legacy)
    return tuple(aliases)


def _finding_fingerprint_aliases(finding: dict[str, Any]) -> tuple[str, ...]:
    return _safe_fingerprint_aliases(finding)


def _display_fingerprint_aliases(finding: dict[str, Any]) -> tuple[str, ...]:
    aliases: list[str] = []
    primary = str(finding.get("fingerprint") or "")
    if primary:
        aliases.append(primary)
    legacy = str(finding.get("legacy_fingerprint") or "")
    if legacy and legacy not in aliases:
        aliases.append(legacy)
    return tuple(aliases)


def _expand_suppression_fingerprints(fingerprints: set[str], migration_map: dict[str, str] | None = None) -> set[str]:
    expanded = set(fingerprints)
    if not migration_map:
        return expanded
    for legacy, primary in migration_map.items():
        if primary in fingerprints:
            expanded.add(legacy)
        if legacy in fingerprints:
            expanded.add(primary)
    return expanded


def _suppression_reason_for_configured_fingerprint(
    fingerprint: str, *, reasons: dict[str, str], migration_map: dict[str, str] | None = None
) -> str | None:
    reason = reasons.get(fingerprint)
    if reason:
        return reason
    if not migration_map:
        return None
    primary = migration_map.get(fingerprint)
    if primary:
        return reasons.get(primary)
    for legacy, mapped_primary in migration_map.items():
        if mapped_primary == fingerprint:
            mapped_reason = reasons.get(mapped_primary) or reasons.get(legacy)
            if mapped_reason:
                return mapped_reason
    return None


def _finding_matches_fingerprints(
    finding: dict[str, Any],
    fingerprints: set[str],
    *,
    migration_map: dict[str, str] | None = None,
) -> bool:
    if not fingerprints:
        return False
    effective = _expand_suppression_fingerprints(fingerprints, migration_map)
    return any(alias in effective for alias in _safe_fingerprint_aliases(finding))


def _suppression_reason_for_finding(
    finding: dict[str, Any],
    *,
    suppressions: set[str],
    reasons: dict[str, str],
    migration_map: dict[str, str] | None = None,
) -> str | None:
    aliases = _safe_fingerprint_aliases(finding)
    effective = _expand_suppression_fingerprints(suppressions, migration_map)
    if not any(alias in effective for alias in aliases):
        return None
    for alias in aliases:
        reason = _suppression_reason_for_configured_fingerprint(alias, reasons=reasons, migration_map=migration_map)
        if reason:
            return reason
    return None


def _expand_finding_suppression_aliases(
    finding: dict[str, Any],
    *,
    migration_map: dict[str, str] | None = None,
) -> set[str]:
    aliases = set(_safe_fingerprint_aliases(finding))
    if not migration_map:
        return aliases
    return _expand_suppression_fingerprints(aliases, migration_map)


def _configured_suppression_aliases(
    finding: dict[str, Any],
    *,
    suppressions: set[str],
    reasons: dict[str, str] | None = None,
    migration_map: dict[str, str] | None = None,
) -> tuple[str, ...]:
    configured = _expand_suppression_fingerprints(set(suppressions), migration_map)
    if reasons is not None:
        configured.update(_expand_suppression_fingerprints(set(reasons), migration_map))
    finding_aliases = _expand_finding_suppression_aliases(finding, migration_map=migration_map)
    return tuple(sorted(alias for alias in configured if alias in finding_aliases))


def _migrate_legacy_suppressions(
    config: SecurityConfig,
    report: dict[str, Any],
) -> tuple[SecurityConfig, list[dict[str, str]]]:
    migrations: list[dict[str, str]] = []
    suppressions = list(config.suppressions)
    suppression_set = set(suppressions)
    reasons = dict(config.suppression_reasons)
    migrated_from: set[str] = set()
    seen_findings: set[tuple[str, str]] = set()

    for bucket in ("findings", "suppressed_findings"):
        for finding in report.get(bucket) or []:
            if not isinstance(finding, dict):
                continue
            primary = str(finding.get("fingerprint") or "")
            legacy = str(finding.get("legacy_fingerprint") or "")
            if not primary or not legacy or legacy not in suppression_set:
                continue
            if _finding_duplicate_count(finding) > 1:
                continue
            identity = (primary, legacy)
            if identity in seen_findings or legacy in migrated_from:
                continue
            seen_findings.add(identity)
            reason = reasons.get(legacy) or reasons.get(primary)
            suppressions = [item for item in suppressions if item != legacy]
            if primary not in suppressions:
                suppressions.append(primary)
            reasons.pop(legacy, None)
            if reason:
                reasons[primary] = reason
            migrations.append({"from": legacy, "to": primary})
            migrated_from.add(legacy)
            suppression_set.discard(legacy)
            suppression_set.add(primary)

    if not migrations:
        return config, migrations

    deduped: list[str] = []
    seen: set[str] = set()
    for fingerprint in suppressions:
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        deduped.append(fingerprint)

    migrated = SecurityConfig(
        policy=config.policy,
        scan_profile=config.scan_profile,
        fail_on=config.fail_on,
        include_templates=config.include_templates,
        enabled_checks=config.enabled_checks,
        include_paths=config.include_paths,
        exclude_paths=config.exclude_paths,
        severity_threshold=config.severity_threshold,
        output_path=config.output_path,
        suppressions=tuple(deduped),
        suppression_reasons=reasons,
        enrichment=config.enrichment,
    )
    return migrated, migrations


def _migrate_closeout_fingerprints(
    closeout: dict[str, Any],
    quieted_findings: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if closeout.get("status") != "accepted-risk":
        return None
    source_fingerprints = [
        str(item) for item in closeout.get("source_fingerprints", []) if isinstance(item, str) and item.strip()
    ]
    source_set = set(source_fingerprints)
    raw_migrations = closeout.get("fingerprint_migrations")
    migrations = dict(raw_migrations) if isinstance(raw_migrations, dict) else {}
    changed = False
    for finding in quieted_findings:
        if not isinstance(finding, dict):
            continue
        primary = str(finding.get("fingerprint") or "")
        legacy = str(finding.get("legacy_fingerprint") or "")
        if not primary or not legacy or legacy not in source_set or primary in source_set:
            continue
        if _finding_duplicate_count(finding) > 1:
            continue
        source_fingerprints.append(primary)
        source_set.add(primary)
        migrations[legacy] = primary
        changed = True
    if not changed:
        return None
    updated = dict(closeout)
    deduped: list[str] = []
    seen: set[str] = set()
    for fingerprint in source_fingerprints:
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        deduped.append(fingerprint)
    updated["source_fingerprints"] = deduped
    updated["fingerprint_migrations"] = migrations
    closeout_path = closeout.get("path")
    if isinstance(closeout_path, str) and closeout_path.strip():
        trusted_path = Path(closeout_path).expanduser().resolve()
        _write_json(trusted_path, updated)
    return updated


def _import_content_identity(
    *,
    rule_id: str,
    rel_path: str,
    safe_detail: str,
    occurrence: int,
    duplicate_count: int,
) -> tuple[str, str, str, int, int]:
    return (
        rule_id,
        rel_path,
        _normalized_matched_content(safe_detail),
        duplicate_count,
        occurrence,
    )


def _import_content_base_identity(
    *,
    rule_id: str,
    rel_path: str,
    safe_detail: str,
) -> tuple[str, str, str]:
    return (
        rule_id,
        rel_path,
        _normalized_matched_content(safe_detail),
    )


def _finding_import_content_identity(finding: dict[str, Any]) -> tuple[str, str, str, int, int]:
    safe_detail = str(finding.get("safe_excerpt") or finding.get("evidence") or "")
    return _import_content_identity(
        rule_id=str(finding.get("rule_id") or ""),
        rel_path=str(finding.get("path") or ""),
        safe_detail=safe_detail,
        occurrence=int(finding.get("occurrence") or 0),
        duplicate_count=int(finding.get("duplicate_count") or 1),
    )


def _import_metadata_content_identity(metadata: dict[str, Any]) -> tuple[str, str, str, int, int] | None:
    occurrence = metadata.get("occurrence")
    duplicate_count = metadata.get("duplicate_count")
    if not isinstance(occurrence, int) or not isinstance(duplicate_count, int):
        return None
    safe_detail = str(metadata.get("safe_detail") or metadata.get("safe_excerpt") or "")
    return _import_content_identity(
        rule_id=str(metadata.get("rule_id") or ""),
        rel_path=str(metadata.get("path") or ""),
        safe_detail=safe_detail,
        occurrence=occurrence,
        duplicate_count=duplicate_count,
    )


def _import_metadata_content_base_identity(metadata: dict[str, Any]) -> tuple[str, str, str]:
    safe_detail = str(metadata.get("safe_detail") or metadata.get("safe_excerpt") or "")
    return _import_content_base_identity(
        rule_id=str(metadata.get("rule_id") or ""),
        rel_path=str(metadata.get("path") or ""),
        safe_detail=safe_detail,
    )


def _assign_fingerprints(findings: list[dict[str, Any]]) -> None:
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for finding in findings:
        key = (
            str(finding.get("rule_id") or ""),
            str(finding.get("path") or ""),
            str(finding.get("_fingerprint_content") or finding.get("safe_excerpt") or finding.get("evidence") or ""),
        )
        groups.setdefault(key, []).append(finding)
    for (_, _, matched_content), items in groups.items():
        items.sort(key=lambda finding: (int(finding.get("line") or 0), str(finding.get("id") or "")))
        duplicate_count = len(items)
        for occurrence, finding in enumerate(items):
            fingerprint = _fingerprint(
                rule_id=str(finding.get("rule_id") or ""),
                rel_path=str(finding.get("path") or ""),
                matched_content=matched_content,
                occurrence=occurrence,
                duplicate_count=duplicate_count,
            )
            finding["occurrence"] = occurrence
            finding["duplicate_count"] = duplicate_count
            finding["fingerprint"] = fingerprint
            finding["legacy_fingerprint"] = _legacy_fingerprint(
                category=str(finding.get("category") or ""),
                title=str(finding.get("title") or ""),
                rel_path=str(finding.get("path") or ""),
                line=int(finding.get("line") or 0),
                evidence=str(finding.get("safe_excerpt") or finding.get("evidence") or ""),
            )
            finding["id"] = f"security-{fingerprint}"
            finding.pop("_fingerprint_content", None)


def _finding(
    findings: list[dict[str, Any]],
    *,
    target: Path,
    path: Path,
    line: int,
    severity: str,
    category: str,
    title: str,
    evidence: str,
    suggestion: str,
    response_options: list[str] | None = None,
    classification: FileClassification | None = None,
) -> None:
    rel = path.relative_to(target)
    file_classification = classification or _classification_for(path, target)
    safe_excerpt = _template_audit_ops_mod._short(evidence)
    matched_content = _normalized_matched_content(evidence)
    rule = _rule_id(category, title)
    findings.append(
        {
            "rule_id": rule,
            "severity": severity,
            "category": category,
            "title": title,
            "path": str(rel),
            "line": line,
            "surface": file_classification.surface,
            "confidence": file_classification.confidence,
            "evidence": safe_excerpt,
            "safe_excerpt": safe_excerpt,
            "_fingerprint_content": matched_content,
            "suggestion": suggestion,
            "remediation_hint": suggestion,
            "response_options": response_options or [],
        }
    )


def _first_committed_secret_match(pattern: re.Pattern[str], line: str) -> re.Match[str] | None:
    """First match on the line whose value is a committed literal, not a placeholder or runtime read.

    Scoped to the matched value so a line that mixes a real credential with a
    ``secrets.token_hex()`` call or an environment read still reports the credential.
    """
    for match in pattern.finditer(line):
        value = match.group(2)
        if _is_secret_path_assignment(match):
            continue
        if _template_audit_ops_mod._is_placeholder(value):
            continue
        if not _match_value_is_quoted(line, match) and _is_runtime_secret_value(value):
            continue
        return match
    return None


def _first_committed_env_assignment(line: str) -> re.Match[str] | None:
    for match in ENV_ASSIGNMENT_RE.finditer(line):
        value = _env_assignment_value(match)
        if _is_runtime_secret_value(value) or _is_attribute_secret_value(value):
            continue
        return match
    return None


def _scan_line(
    findings: list[dict[str, Any]],
    *,
    target: Path,
    path: Path,
    line_number: int,
    line: str,
    classification: FileClassification | None = None,
) -> None:
    file_classification = classification or _classification_for(path, target)
    if _is_security_scanner_literal(path, line, target):
        return
    secret_candidates: dict[str, str] = {}

    def consider_secret(title: str, suggestion: str) -> None:
        secret_candidates.setdefault(title, suggestion)

    try:
        rel_path = path.relative_to(target).as_posix()
    except ValueError:
        rel_path = path.as_posix()
    if not rel_path.startswith("src/brigade/guard/examples/"):
        secret_match = _first_committed_secret_match(SECRET_VALUE_RE, line)
        password_match = _first_committed_secret_match(PLAINTEXT_PASSWORD_RE, line)
        password_emitted = password_match is not None
        session_chat = _is_session_chat_path(path, target)
        session_suggestion = (
            "Redact or archive the session transcript, rotate the credential if real, "
            "and move active use to .env, environment variables, or KeePass."
        )
        if password_emitted:
            consider_secret(
                "Plaintext password",
                "Move the password into local secret storage or a gitignored environment file, "
                "then scrub the raw value from shared files.",
            )
        if secret_match and not password_emitted:
            consider_secret(
                "Session chat contains exposed credential" if session_chat else "Possible hardcoded credential",
                session_suggestion
                if session_chat
                else "Move the value into local environment or secret storage and commit only a placeholder.",
            )
        if _template_audit_ops_mod._contains_private_key_material(line) or (
            _first_committed_env_assignment(line) is not None
            and not password_emitted
            and not _template_audit_ops_mod._is_placeholder(line)
        ):
            consider_secret(
                "Session chat contains exposed credential" if session_chat else "Possible sensitive secret material",
                session_suggestion
                if session_chat
                else "Remove secret material from the repo and rotate the credential if it was real.",
            )
    for title, suggestion in secret_candidates.items():
        _finding(
            findings,
            target=target,
            path=path,
            line=line_number,
            severity="high",
            category="secrets",
            title=title,
            evidence=_template_audit_ops_mod._redact_secret_evidence(line),
            suggestion=suggestion,
            classification=file_classification,
            response_options=_secret_response_options(path, target),
        )
        findings[-1]["_coalesce_group"] = "secrets-line"
    if "danger-full-access" in line or "sandbox_permissions" in line and "require_escalated" in line:
        _finding(
            findings,
            target=target,
            path=path,
            line=line_number,
            severity="medium",
            category="permissions",
            title="Broad agent execution permission",
            evidence=line,
            suggestion="Prefer read-only or workspace-scoped execution unless this is an explicitly trusted local path.",
            classification=file_classification,
        )
    if REMOTE_SHELL_RE.search(line):
        _finding(
            findings,
            target=target,
            path=path,
            line=line_number,
            severity="high",
            category="automation",
            title="Remote script piped into shell",
            evidence=line,
            suggestion="Pin and verify downloaded scripts before execution, or replace with a checked-in script.",
            classification=file_classification,
        )
    if DESTRUCTIVE_RE.search(line):
        _finding(
            findings,
            target=target,
            path=path,
            line=line_number,
            severity="medium",
            category="automation",
            title="Destructive command pattern",
            evidence=line,
            suggestion="Gate destructive commands behind explicit operator approval and document recovery steps.",
            classification=file_classification,
        )
    if ENV_DUMP_RE.search(line):
        _finding(
            findings,
            target=target,
            path=path,
            line=line_number,
            severity="high",
            category="secrets",
            title="Environment dump or exfiltration pattern",
            evidence=_template_audit_ops_mod._redact_secret_evidence(line),
            suggestion="Avoid dumping environment variables near file redirection or network commands.",
            classification=file_classification,
            response_options=_secret_response_options(path, target),
        )
    npx_match = UNPINNED_NPX_RE.search(line)
    if npx_match and "@" not in npx_match.group(1):
        _finding(
            findings,
            target=target,
            path=path,
            line=line_number,
            severity="medium",
            category="supply-chain",
            title="Unpinned remote package execution",
            evidence=line,
            suggestion="Pin remote package versions or install through a reviewed lockfile.",
            classification=file_classification,
        )
    if "mcp" in path.name.lower() or '"mcpServers"' in line:
        if HTTP_MCP_RE.search(line):
            _finding(
                findings,
                target=target,
                path=path,
                line=line_number,
                severity="medium",
                category="mcp",
                title="Remote MCP transport",
                evidence=line,
                suggestion="Prefer local MCP servers, pin remote hosts, and document authentication boundaries.",
                classification=file_classification,
            )
        if AUTO_APPROVE_RE.search(line):
            _finding(
                findings,
                target=target,
                path=path,
                line=line_number,
                severity="medium",
                category="mcp",
                title="MCP auto-approval pattern",
                evidence=line,
                suggestion="Avoid blanket auto-approval and require review for mutable or networked tools.",
                classification=file_classification,
            )
    if file_classification.surface in {
        "agent-instructions",
        "claude",
        "codex",
        "repo",
        "skill",
        "slash-command",
        "subagent",
        "tool-wrapper",
    } and PROMPT_INJECTION_RE.search(line):
        _finding(
            findings,
            target=target,
            path=path,
            line=line_number,
            severity="low",
            category="prompt-injection",
            title="Prompt-injection style instruction",
            evidence=line,
            suggestion="Keep hostile examples clearly labeled as examples and avoid executable language in trusted instructions.",
            classification=file_classification,
        )


def _path_matches_any(rel_path: str, patterns: tuple[str, ...]) -> bool:
    normalized = rel_path.replace("\\", "/")
    for pattern in patterns:
        clean = pattern.strip().replace("\\", "/").strip("/")
        if not clean:
            continue
        if clean.endswith("/**"):
            prefix = clean[:-3].rstrip("/")
            if normalized == prefix or normalized.startswith(prefix + "/"):
                return True
            continue
        if normalized == clean or normalized.startswith(clean.rstrip("/") + "/"):
            return True
    return False


def _severity_selected(finding: dict[str, Any], threshold: str) -> bool:
    return SEVERITY_ORDER.get(str(finding.get("severity")), 0) >= SEVERITY_ORDER.get(threshold, 0)


def _filter_findings(
    findings: list[dict[str, Any]],
    *,
    enabled_checks: tuple[str, ...],
    include_paths: tuple[str, ...],
    exclude_paths: tuple[str, ...],
    severity_threshold: str,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    enabled = set(enabled_checks)
    for finding in findings:
        path = str(finding.get("path") or "")
        if enabled and finding.get("category") not in enabled:
            continue
        if include_paths and not _path_matches_any(path, include_paths):
            continue
        if exclude_paths and _path_matches_any(path, exclude_paths):
            continue
        if not _severity_selected(finding, severity_threshold):
            continue
        selected.append(finding)
    return selected


def _scan_handoff_inboxes(
    findings: list[dict[str, Any]],
    *,
    target: Path,
    include_paths: tuple[str, ...] = (),
    exclude_paths: tuple[str, ...] = (),
) -> list[str]:
    """Screen pending handoff notes for injection signals.

    Handoff inboxes are excluded from the line scanner via SKIP_PREFIXES so
    untrusted note content is not attributed to the repo author. Injection-style
    instructions are reported through the untrusted-context lens for review before
    any ingester reads them. `processed/` and TEMPLATE.md are skipped.
    """
    scanned: list[str] = []
    for inbox_rel in sorted(set(WRITER_INBOXES.values())):
        inbox = target / inbox_rel
        if not inbox.is_dir():
            continue
        for path in sorted(inbox.glob("*.md")):
            if path.name == "TEMPLATE.md":
                continue
            rel = str(path.relative_to(target))
            if not _scan_path_selected(rel, include_paths=include_paths, exclude_paths=exclude_paths):
                continue
            try:
                text = path.read_text(errors="replace")
            except OSError:
                continue
            scanned.append(rel)
            signal = scan_untrusted(text)
            if signal.flagged:
                classification = _classification_for(path, target)
                _finding(
                    findings,
                    target=target,
                    path=path,
                    line=1,
                    severity="medium",
                    category="handoff-injection",
                    title="Pending handoff carries prompt-injection signals",
                    evidence=signal.markers[0] if signal.markers else "injection signal",
                    suggestion="Review this handoff before ingest; do not let an ingester auto-promote it.",
                    classification=classification,
                )
    return scanned


def scan_target(
    target: Path,
    *,
    include_templates: bool = False,
    suppressions: tuple[str, ...] = (),
    enabled_checks: tuple[str, ...] = SECURITY_CHECKS,
    include_paths: tuple[str, ...] = (),
    exclude_paths: tuple[str, ...] = (),
    severity_threshold: str = "low",
) -> dict[str, Any]:
    target = target.expanduser().resolve()
    findings: list[dict[str, Any]] = []
    scanned_files: list[str] = []
    scanned_files.extend(
        _scan_handoff_inboxes(
            findings,
            target=target,
            include_paths=include_paths,
            exclude_paths=exclude_paths,
        )
    )
    for path in _iter_scan_files(target, include_paths=include_paths, exclude_paths=exclude_paths):
        classification = _classification_for(path, target)
        if not include_templates and classification.confidence == "template":
            continue
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        scanned_files.append(str(path.relative_to(target)))
        for line_number, line in enumerate(text.splitlines(), start=1):
            _scan_line(
                findings,
                target=target,
                path=path,
                line_number=line_number,
                line=line,
                classification=classification,
            )
        _scan_detectors_mod._scan_mcp_document(
            findings, target=target, path=path, text=text, classification=classification
        )
        if not _should_skip_harness_wiring_path(path, target):
            _scan_detectors_mod._scan_harness_wiring_document(
                findings, target=target, path=path, text=text, classification=classification
            )
        _scan_detectors_mod._scan_package_json(
            findings, target=target, path=path, text=text, classification=classification
        )
        _scan_detectors_mod._scan_github_actions(
            findings, target=target, path=path, text=text, classification=classification
        )
        _scan_detectors_mod._scan_python_project(
            findings, target=target, path=path, text=text, classification=classification
        )
    _assign_fingerprints(findings)
    findings = _filter_findings(
        findings,
        enabled_checks=enabled_checks,
        include_paths=include_paths,
        exclude_paths=exclude_paths,
        severity_threshold=severity_threshold,
    )
    migration_map = _read_fingerprint_migration_map(target)
    suppression_set = set(suppressions)
    suppressed = []
    open_findings = []
    for finding in findings:
        if _finding_matches_fingerprints(finding, suppression_set, migration_map=migration_map):
            finding.pop("_coalesce_group", None)
            suppressed.append(finding)
        else:
            open_findings.append(finding)
    findings = _coalesce_findings(open_findings)
    for finding in suppressed:
        _strip_internal_finding_keys(finding)
    counts: dict[str, int] = {}
    for finding in findings:
        severity = str(finding["severity"])
        counts[severity] = counts.get(severity, 0) + 1
    return {
        "target": str(target),
        "scanned_files": scanned_files,
        "scanned_file_count": len(scanned_files),
        "finding_count": len(findings),
        # suppressed_count is per fingerprint; open findings are coalesced first.
        "suppressed_count": len(suppressed),
        "severity_counts": dict(sorted(counts.items())),
        "findings": findings,
        "suppressed_findings": suppressed,
    }


def _should_fail(findings: list[dict[str, Any]], fail_on: str) -> bool:
    if fail_on == "none":
        return False
    threshold = SEVERITY_ORDER[fail_on]
    return any(SEVERITY_ORDER.get(str(item.get("severity")), 0) >= threshold for item in findings)


def _import_findings(
    target: Path,
    findings: list[dict[str, Any]],
    *,
    evidence_path: Path | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    existing_identities: dict[tuple[str, str, str, int, int], set[str]] = {}
    legacy_counts: dict[tuple[str, str, str], dict[str, int]] = {}
    for item in work_cmd._read_imports(target):
        if not isinstance(item, dict) or item.get("source") != "security-scan":
            continue
        metadata = item.get("metadata")
        if not isinstance(metadata, dict):
            continue
        severity = str(metadata.get("severity") or "")
        identity = _import_metadata_content_identity(metadata)
        if identity is not None:
            existing_identities.setdefault(identity, set()).add(severity)
            continue
        base_identity = _import_metadata_content_base_identity(metadata)
        severity_counts = legacy_counts.setdefault(base_identity, {})
        severity_counts[severity] = severity_counts.get(severity, 0) + 1
    records = []
    skipped: list[dict[str, Any]] = []
    legacy_remaining = {key: counts.copy() for key, counts in legacy_counts.items()}
    for finding in findings:
        content_identity = _finding_import_content_identity(finding)
        base_identity = content_identity[:3]
        severity = str(finding.get("severity") or "")
        known_severities = existing_identities.get(content_identity)
        if known_severities is not None and severity in known_severities:
            skipped.append(finding)
            continue
        duplicate_count = int(finding.get("duplicate_count") or 1)
        original_severity_counts = legacy_counts.get(base_identity, {})
        original_legacy_count = sum(original_severity_counts.values())
        remaining_severity_counts = legacy_remaining.get(base_identity, {})
        remaining_legacy = remaining_severity_counts.get(severity, 0)
        if remaining_legacy > 0 and original_legacy_count == duplicate_count:
            remaining_severity_counts[severity] = remaining_legacy - 1
            skipped.append(finding)
            continue
        path = finding.get("path")
        line = finding.get("line")
        title = finding.get("title")
        category = finding.get("category")
        kind = "incident" if SEVERITY_ORDER.get(str(severity), 0) >= SEVERITY_ORDER["high"] else "finding"
        acceptance = [
            f"`brigade security findings` no longer reports {finding.get('id')}.",
            "The mitigation or suppression reason is documented without exposing secret values.",
        ]
        response_options = finding.get("response_options")
        if isinstance(response_options, list) and response_options:
            acceptance.append(
                "The chosen response path is recorded, such as .env storage, scrub/rotate, KeePass review, transcript redaction, or accepted local risk."
            )
        records.append(
            {
                "text": (
                    f"Review security finding [{severity}] {category} in {path}:{line}: {title} "
                    f"[{finding.get('fingerprint')}]"
                ),
                "kind": kind,
                "source": "security-scan",
                "type": "security",
                "priority": "high" if kind == "incident" else "normal",
                "template": "security-follow-up",
                "acceptance": acceptance,
                "metadata": {
                    "finding_id": finding.get("id"),
                    "rule_id": finding.get("rule_id"),
                    "issue_type": category,
                    "fingerprint": finding.get("fingerprint"),
                    "legacy_fingerprint": finding.get("legacy_fingerprint"),
                    "occurrence": finding.get("occurrence", 0),
                    "duplicate_count": finding.get("duplicate_count", 1),
                    "source_item_key": (
                        "security-scan:"
                        + work_cmd._stable_hash(
                            {
                                "rule_id": finding.get("rule_id"),
                                "path": path,
                                "safe_detail": _normalized_matched_content(
                                    str(finding.get("safe_excerpt") or finding.get("evidence") or "")
                                ),
                                "occurrence": finding.get("occurrence", 0),
                                "duplicate_count": finding.get("duplicate_count", 1),
                            }
                        )
                    ),
                    "source_fingerprint": work_cmd._stable_hash(
                        {
                            "rule_id": finding.get("rule_id"),
                            "fingerprint": finding.get("fingerprint"),
                            "legacy_fingerprint": finding.get("legacy_fingerprint"),
                            "severity": severity,
                            "path": path,
                            "line": line,
                            "occurrence": finding.get("occurrence", 0),
                            "duplicate_count": finding.get("duplicate_count", 1),
                            "safe_excerpt": finding.get("safe_excerpt") or finding.get("evidence"),
                        }
                    ),
                    "severity": severity,
                    "category": category,
                    "safe_summary": f"[{severity}] {category}: {title} on {finding.get('surface') or 'repo'}",
                    "path": path,
                    "line": line,
                    "surface": finding.get("surface"),
                    "confidence": finding.get("confidence"),
                    "safe_detail": finding.get("safe_excerpt") or finding.get("evidence"),
                    "remediation_hint": finding.get("remediation_hint") or finding.get("suggestion"),
                    "response_options": response_options if isinstance(response_options, list) else [],
                    "local_evidence_path": str(evidence_path) if evidence_path is not None else None,
                },
            }
        )
        existing_identities.setdefault(content_identity, set()).add(severity)
    imported, duplicate_records, dismissed_records, _rejected = work_cmd._append_import_records(target, records)
    skipped.extend(duplicate_records)
    skipped.extend(dismissed_records)
    return imported, skipped


def _render_markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# Brigade Security Report",
        "",
        f"- target: `{report['target']}`",
        f"- generated_at: `{report['generated_at']}`",
        f"- policy: `{report['policy']}`",
        f"- fail_on: `{report['fail_on']}`",
        f"- include_templates: `{report['include_templates']}`",
        f"- scanned_files: `{report['scanned_file_count']}`",
        f"- findings: `{report['finding_count']}`",
        f"- suppressed: `{report['suppressed_count']}`",
        "",
        "## Severity Counts",
        "",
    ]
    if report["severity_counts"]:
        for severity, count in report["severity_counts"].items():
            lines.append(f"- {severity}: {count}")
    else:
        lines.append("- none: 0")
    lines.extend(["", "## Findings", ""])
    if not report["findings"]:
        lines.append("No unsuppressed findings.")
    for finding in report["findings"]:
        finding = _sanitize_finding_for_output(finding)
        md = _escape_finding_markdown
        finding_lines = [
            f"### {md(finding['id'])} - {md(finding['title'])}",
            "",
            f"- fingerprint: `{md(finding['fingerprint'])}`",
            f"- severity: `{md(finding['severity'])}`",
            f"- category: `{md(finding['category'])}`",
            f"- path: `{md(finding['path'])}:{md(finding['line'])}`",
            f"- surface: `{md(finding['surface'])}`",
            f"- confidence: `{md(finding['confidence'])}`",
            f"- evidence: `{md(finding['evidence'])}`",
            f"- suggestion: {md(finding['suggestion'])}",
        ]
        response_options = finding.get("response_options") or []
        if response_options:
            finding_lines.append("- response_options:")
            finding_lines.extend(f"  - {md(item)}" for item in response_options)
        finding_lines.append("")
        lines.extend(finding_lines)
    return "\n".join(lines).rstrip() + "\n"


def _sarif_level(severity: object) -> str:
    rendered = str(severity or "").lower()
    if rendered in {"critical", "high"}:
        return "error"
    if rendered == "medium":
        return "warning"
    return "note"


def _sarif_report(report: dict[str, Any]) -> dict[str, Any]:
    rules: dict[str, dict[str, Any]] = {}
    results: list[dict[str, Any]] = []
    for finding in report.get("findings", []):
        if not isinstance(finding, dict):
            continue
        finding = _sanitize_finding_for_output(finding)
        rule_id = str(finding.get("rule_id") or finding.get("category") or "brigade.security")
        if rule_id not in rules:
            rules[rule_id] = {
                "id": rule_id,
                "name": str(finding.get("title") or rule_id),
                "shortDescription": {"text": str(finding.get("title") or rule_id)},
                "fullDescription": {
                    "text": str(
                        finding.get("remediation_hint") or finding.get("suggestion") or finding.get("title") or rule_id
                    )
                },
                "properties": {
                    "category": finding.get("category"),
                    "severity": finding.get("severity"),
                },
            }
        region: dict[str, Any] = {}
        try:
            line = int(finding.get("line") or 0)
        except (TypeError, ValueError):
            line = 0
        if line > 0:
            region["startLine"] = line
        results.append(
            {
                "ruleId": rule_id,
                "level": _sarif_level(finding.get("severity")),
                "message": {
                    "text": str(
                        finding.get("title") or finding.get("safe_excerpt") or finding.get("evidence") or rule_id
                    )
                },
                "locations": [
                    {
                        "physicalLocation": {
                            "artifactLocation": {"uri": str(finding.get("path") or "")},
                            **({"region": region} if region else {}),
                        }
                    }
                ],
                "partialFingerprints": {
                    "primaryLocationLineHash": str(finding.get("fingerprint") or finding.get("id") or "")
                },
                "properties": {
                    "id": finding.get("id"),
                    "fingerprint": finding.get("fingerprint"),
                    "severity": finding.get("severity"),
                    "category": finding.get("category"),
                    "confidence": finding.get("confidence"),
                    "surface": finding.get("surface"),
                    "safe_excerpt": finding.get("safe_excerpt") or finding.get("evidence"),
                    "remediation_hint": finding.get("remediation_hint") or finding.get("suggestion"),
                    "response_options": finding.get("response_options") or [],
                },
            }
        )
    return {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "Brigade Security",
                        "informationUri": "https://github.com/escoffier-labs/brigade",
                        "rules": list(rules.values()),
                    }
                },
                "results": results,
                "properties": {
                    "target": report.get("target"),
                    "generated_at": report.get("generated_at"),
                    "policy": report.get("policy"),
                    "finding_count": report.get("finding_count"),
                },
            }
        ],
    }


def write_evidence_bundle(report: dict[str, Any], output_dir: Path) -> Path:
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    report = dict(report)
    for bucket in ("findings", "suppressed_findings"):
        for finding in report.get(bucket) or []:
            if isinstance(finding, dict):
                _strip_internal_finding_keys(finding)
    report["artifacts"] = str(output_dir)
    (output_dir / "security-report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    (output_dir / "security-report.md").write_text(_render_markdown_report(report))
    (output_dir / "security-report.sarif").write_text(
        json.dumps(_sarif_report(report), indent=2, sort_keys=True) + "\n"
    )
    return output_dir


def _config_payload(target: Path) -> dict[str, Any]:
    config = load_config(target)
    path = config_path(target)
    if config is None:
        return {"config_path": str(path), "configured": False, "config": None}
    return {
        "config_path": str(path),
        "configured": True,
        "config": {
            "policy": config.policy,
            "scan_profile": config.scan_profile,
            "fail_on": config.fail_on or POLICIES[config.policy]["fail_on"],
            "include_templates": config.include_templates
            if config.include_templates is not None
            else POLICIES[config.policy]["include_templates"],
            "enabled_checks": list(config.enabled_checks),
            "include_paths": list(config.include_paths),
            "exclude_paths": list(config.exclude_paths),
            "severity_threshold": config.severity_threshold,
            "output_path": config.output_path,
            "suppressions": list(config.suppressions),
            "suppression_reasons": config.suppression_reasons,
            "enrichment": {
                "provider": config.enrichment.provider,
                "misp_url_configured": bool(config.enrichment.misp_url),
                "misp_api_key_env": config.enrichment.misp_api_key_env,
                "timeout_seconds": config.enrichment.timeout_seconds,
                "cache_path": config.enrichment.cache_path,
            },
        },
    }
