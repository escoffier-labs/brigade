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

from .. import work_cmd
from ..selection import WRITER_INBOXES
from ..untrusted import PROMPT_INJECTION_RE, scan_untrusted
from .. import localio
from ..localio import read_json_dict as _read_json, utc_now_iso_z as _utc_iso, write_json as _write_json


SEVERITY_ORDER = {
    "info": 0,
    "low": 1,
    "medium": 2,
    "high": 3,
    "critical": 4,
}


CONFIG_REL_PATH = ".brigade/security.toml"


ARTIFACTS_REL_PATH = ".brigade/security/latest"


SUPPRESSION_HEALTH_CACHE_REL_PATH = ".brigade/security/suppression-health-cache.json"


FINGERPRINT_MIGRATION_MAP_REL_PATH = ".brigade/security/fingerprint-migration-map.json"


FINGERPRINT_MIGRATION_MAP_SCHEMA = "brigade.security.fingerprint-migration-map.v1"


SUPPRESSION_HEALTH_CACHE_VERSION = 1


POLICIES = {
    "personal": {
        "fail_on": "critical",
        "include_templates": False,
    },
    "public-repo": {
        "fail_on": "high",
        "include_templates": False,
    },
    "ci": {
        "fail_on": "high",
        "include_templates": True,
    },
    "strict": {
        "fail_on": "medium",
        "include_templates": True,
    },
}


SCAN_PROFILES = {
    "public-repo": "public-repo",
    "internal-workspace": "personal",
    "local-only-audit": "strict",
}


SECURITY_CHECKS = (
    "automation",
    "handoff-injection",
    "mcp",
    "permissions",
    "prompt-injection",
    "secrets",
    "supply-chain",
)


DEFAULT_EXCLUDE_PATHS = (".brigade/**",)


CONFIG_TOP_LEVEL_KEYS = frozenset(
    {
        "policy",
        "scan_profile",
        "fail_on",
        "include_templates",
        "enabled_checks",
        "include_paths",
        "exclude_paths",
        "severity_threshold",
        "output_path",
    }
)


CONFIG_SUPPRESSIONS_KEYS = frozenset({"fingerprints"})


CONFIG_ENRICHMENT_KEYS = frozenset(
    {
        "provider",
        "misp_url",
        "misp_api_key_env",
        "timeout_seconds",
        "cache_path",
    }
)


SKIP_DIRS = {
    ".git",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "node_modules",
}


SKIP_PREFIXES = (
    (".brigade", "runs"),
    (".brigade", "security"),
    (".brigade", "work"),
    (".claude", "memory-handoffs"),
    (".codex", "memory-handoffs"),
    (".opencode", "memory-handoffs"),
    (".antigravity", "memory-handoffs"),
    (".pi", "memory-handoffs"),
    (".cursor", "memory-handoffs"),
    (".aider", "memory-handoffs"),
    (".goose", "memory-handoffs"),
    (".continue", "memory-handoffs"),
    (".copilot", "memory-handoffs"),
    (".qwen", "memory-handoffs"),
    (".kimi", "memory-handoffs"),
    (".adal", "memory-handoffs"),
    (".openhands", "memory-handoffs"),
    (".grok", "memory-handoffs"),
    (".amp", "memory-handoffs"),
    (".crush", "memory-handoffs"),
    (".hermes", "memory-handoffs"),
)


TEXT_SUFFIXES = {
    "",
    ".bash",
    ".cfg",
    ".conf",
    ".env",
    ".ini",
    ".json",
    ".jsonl",
    ".md",
    ".mjs",
    ".py",
    ".sh",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}


SECRET_VALUE_RE = re.compile(
    r"(?i)\b(api[_-]?key|secret|token|password|passwd|pwd)\b\s*[:=]\s*['\"]?([A-Za-z0-9_./+=:-]{16,})"
)


PLAINTEXT_PASSWORD_RE = re.compile(
    r"(?i)\b[A-Za-z0-9_-]*(password|passwd|pwd|passphrase)[A-Za-z0-9_-]*\b\s*[:=]\s*['\"]?([^'\"\s#]{8,})"
)


PRIVATE_KEY_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")


ENV_ASSIGNMENT_RE = re.compile(r"(?i)\b[A-Z0-9_]*(TOKEN|SECRET|PASSWORD|API_KEY)\s*=\s*[A-Za-z0-9_./+=:-]{16,}")

RUNTIME_SECRET_VALUE_RE = re.compile(r"(?i)^(?:secrets\.\w+|os\.environ|os\.getenv|[\w.]*_from_env)\b")
ATTRIBUTE_SECRET_VALUE_RE = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+$")
SECRETS_TITLE_RANK = {
    "Session chat contains exposed credential": 40,
    "Plaintext password": 30,
    "Possible hardcoded credential": 20,
    "Possible sensitive secret material": 10,
}


def _is_runtime_secret_value(value: str) -> bool:
    """True when the assigned value is generated or read at runtime instead of committed.

    Quotes are deliberately not stripped here. A quoted value is a committed literal even
    when its text opens with a runtime expression: a string literal whose contents begin
    with ``os.environ.`` followed by key material must stay reportable. Callers that can
    still see the source line decide whether the value was quoted; see
    :func:`_match_value_is_quoted`.
    """
    return RUNTIME_SECRET_VALUE_RE.match(value.strip()) is not None


def _match_value_is_quoted(line: str, match: re.Match[str]) -> bool:
    """True when the captured secret value was wrapped in quotes on the source line.

    ``SECRET_VALUE_RE`` and ``PLAINTEXT_PASSWORD_RE`` both put the optional quote *outside*
    their value group, so the quote never reaches the captured text and has to be read back
    off the line.
    """
    start = match.start(2)
    return start > 0 and line[start - 1] in "'\""


def _is_attribute_secret_value(value: str) -> bool:
    """True when the assigned value is another object's attribute rather than a literal.

    A committed JWT is also dot-separated and can be purely alphanumeric, so
    JWT-shaped values (base64url header always starts with ``eyJ``, segments far
    longer than identifier hops) must still report.
    """
    candidate = value.strip()
    if ATTRIBUTE_SECRET_VALUE_RE.match(candidate) is None:
        return False
    if candidate.startswith("eyJ"):
        return False
    return all(len(part) <= 32 for part in candidate.split("."))


def _env_assignment_value(match: re.Match[str]) -> str:
    _, _, value = match.group(0).partition("=")
    return value.strip()


def _secrets_title_rank(title: str) -> int:
    return SECRETS_TITLE_RANK.get(title, 0)


_INTERNAL_FINDING_KEYS = frozenset({"_coalesce_group", "_fingerprint_content"})


def _strip_internal_finding_keys(finding: dict[str, Any]) -> dict[str, Any]:
    for key in _INTERNAL_FINDING_KEYS:
        finding.pop(key, None)
    return finding


def _finding_coalesce_rank(finding: dict[str, Any]) -> tuple[int, int]:
    """Rank coalesce-group members: severity first, then secrets title tie-break.

    Scanner-defined coalesce groups currently emit uniform severity; title rank
    breaks ties within the same severity so the most specific secret label wins.
    """
    severity = SEVERITY_ORDER.get(str(finding.get("severity") or ""), 0)
    return severity, _secrets_title_rank(str(finding.get("title") or ""))


def _coalesce_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Select the strongest remaining member of each scanner-defined group."""
    selected: list[tuple[int, dict[str, Any]]] = []
    grouped: dict[tuple[str, str, int], tuple[int, dict[str, Any]]] = {}
    for index, finding in enumerate(findings):
        group = finding.pop("_coalesce_group", None)
        if not group:
            selected.append((index, finding))
            continue
        key = (str(group), str(finding.get("path") or ""), int(finding.get("line") or 0))
        current = grouped.get(key)
        if current is None or _finding_coalesce_rank(finding) > _finding_coalesce_rank(current[1]):
            grouped[key] = (index, finding)
    selected.extend(grouped.values())
    coalesced = [finding for _, finding in sorted(selected, key=lambda item: item[0])]
    for finding in coalesced:
        _strip_internal_finding_keys(finding)
    return coalesced


def _is_security_scanner_literal(path: Path, line: str, target: Path) -> bool:
    try:
        rel = path.relative_to(target).as_posix()
    except ValueError:
        rel = path.as_posix()
    if not (rel.startswith("src/brigade/security_cmd/") and rel.endswith(".py")):
        return False
    stripped = line.strip()
    scanner_tokens = (
        "danger-full-access",
        "sandbox_permissions",
        "require_escalated",
        "npx package",
        "PLAINTEXT_PASSWORD_RE",
        "Environment dump or exfiltration pattern",
        "Plaintext password",
        "Possible hardcoded credential",
        "Possible sensitive secret material",
        "Session chat contains exposed credential",
    )
    if any(token in stripped for token in scanner_tokens):
        return True
    if stripped.startswith("suggestion=") or stripped.startswith("title="):
        return True
    if stripped.startswith(("password_match =", "password_emitted =")):
        return True
    return stripped.startswith("if ") and (
        '"danger-full-access"' in stripped or '"sandbox_permissions"' in stripped or '"require_escalated"' in stripped
    )


REMOTE_SHELL_RE = re.compile(r"\b(curl|wget)\b[^\n|;]*(\||;)\s*(sh|bash)\b")


DESTRUCTIVE_RE = re.compile(r"\b(rm\s+-rf|git\s+reset\s+--hard|git\s+clean\s+-fdx|chmod\s+777)\b")


UNPINNED_NPX_RE = re.compile(r"\bnpx\s+(?:-y\s+)?([a-zA-Z0-9_.-]+)(?:\s|$)")


ENV_DUMP_RE = re.compile(r"(?:^|[;&|]\s*)(env|printenv|set)(?:\s|$)[^\n]*(>\s*\S+|\|\s*(curl|nc|netcat|tee)\b)")


UNPINNED_ACTION_RE = re.compile(r"uses:\s*['\"]?([^@\s'\":]+/[^@\s'\"]+|docker://[^@\s'\"]+)['\"]?\s*$")


PINNED_ACTION_RE = re.compile(r"uses:\s*['\"]?([^@\s'\"]+)@([^@\s'\"]+)")


PYTHON_URL_DEP_RE = re.compile(r"(?i)(https?://|git\+https?://|git\+ssh://)")


TOML_TABLE_RE = re.compile(r"^\[+([A-Za-z0-9_.-]+)\]+$")


HTTP_MCP_RE = re.compile(r'"url"\s*:\s*"https?://')


AUTO_APPROVE_RE = re.compile(r"(?i)(auto[_-]?approve|always[_-]?allow|allow[_-]?all)")


MCP_SENSITIVE_ARG_RE = re.compile(
    r"(^|/)(\.env|id_rsa|id_ed25519|credentials|known_hosts|passwd|shadow)$|"
    r"(\.ssh/|\.aws/|\.config/gh/|\.docker/|/etc/passwd|/etc/shadow)",
    re.IGNORECASE,
)


MCP_BROAD_PATHS = {"~", "$HOME", "/", "/home", "/Users"}


MCP_HIGH_RISK_COMMANDS = {"bash", "sh", "zsh", "fish", "powershell", "pwsh", "docker", "podman", "ssh", "scp", "rsync"}


MCP_SERVER_COUNT_WARN = 8


MCP_SHELL_META_RE = re.compile(r"[;&|`<>]|\$\(")


FINGERPRINT_RE = re.compile(r"^[a-f0-9]{16}$")


GITHUB_ACTION_FLOATING_REFS = {"main", "master", "latest", "dev", "develop", "trunk", "head"}


INDICATOR_URL_RE = re.compile(r"https?://[^\s`\"'<>]+")


INDICATOR_NPX_RE = re.compile(r"\bnpx\s+(?:-y\s+)?([a-zA-Z0-9_.@/-]+)")


INDICATOR_GITHUB_ACTION_RE = re.compile(r"uses:\s*['\"]?([^@\s'\"]+)(?:@([^@\s'\"]+))?")


ENRICHMENT_PROVIDERS = {"local", "misp"}


ENRICHMENT_MARKDOWN_START = "<!-- brigade-security-enrichment:start -->"


ENRICHMENT_MARKDOWN_END = "<!-- brigade-security-enrichment:end -->"


TEMPLATE_AUDIT_ROOTS = ("src/brigade/templates", "templates", "docs")


TEMPLATE_PRIVATE_PATH_RE = re.compile(r"(?<![`$<])/(?:home|Users|private|mnt|Volumes)/[A-Za-z0-9_.@/-]+")


TEMPLATE_PRIVATE_URL_RE = re.compile(
    r"https?://(?:[A-Za-z0-9_-]+\.)*(?:lan|local|internal|private)(?:[/:][^\s`\"'<)]*)?", re.IGNORECASE
)


TEMPLATE_ALLOWLIST_RE = re.compile(
    r"(example[.](com|org|net|invalid)|local"
    r"host|127[.]0[.]0[.]1|0[.]0[.]0[.]0|<[^>]+>|\{\{[^}]+\}\}|\$\{?[A-Z_][A-Z0-9_]*(?::-[^}]*)?\}?)",
    re.IGNORECASE,
)


HARNESS_ROOTS = {
    ".brigade",
    ".claude",
    ".codex",
    ".opencode",
    ".antigravity",
    ".pi",
    ".cursor",
    ".aider",
    ".goose",
    ".continue",
    ".copilot",
    ".qwen",
    ".kimi",
    ".adal",
    ".openhands",
    ".grok",
    ".amp",
    ".crush",
    ".openclaw",
    ".hermes",
}


# Dependency lockfiles under harness roots (for example `.opencode/package-lock.json`)
# are not harness wiring documents. Exclude them from harness-wiring scans only.
HARNESS_WIRING_LOCKFILE_NAMES = frozenset(
    {
        "Cargo.lock",
        "Gemfile.lock",
        "Pipfile.lock",
        "composer.lock",
        "go.sum",
        "npm-shrinkwrap.json",
        "package-lock.json",
        "pnpm-lock.yaml",
        "poetry.lock",
        "uv.lock",
        "yarn.lock",
    }
)


def _should_skip_harness_wiring_path(path: Path, target: Path) -> bool:
    """Exclude nested worktrees, Brigade work artifacts, and lockfiles from wiring scans only."""
    try:
        rel = path.relative_to(target)
    except ValueError:
        return True
    if path.name in HARNESS_WIRING_LOCKFILE_NAMES:
        return True
    parts = rel.parts
    if any(parts[index : index + 2] == (".brigade", "work") for index in range(len(parts) - 1)):
        return True
    for ancestor in path.parents:
        if ancestor == target:
            break
        try:
            ancestor.relative_to(target)
        except ValueError:
            break
        git_marker = ancestor / ".git"
        try:
            marker_text = git_marker.read_text(encoding="utf-8", errors="replace") if git_marker.is_file() else ""
        except OSError:
            continue
        if marker_text.strip().startswith("gitdir:"):
            return True
    return False


HARNESS_PATH_KEYS = {
    "bootstrap_files",
    "cache_path",
    "document_targets_allowed",
    "dst",
    "handoff_inbox",
    "inbox",
    "inboxes",
    "inbox_dir",
    "last_run_log",
    "path",
    "processed_dir",
    "review_inbox",
    "root",
    "routing_targets",
    "src",
}


HARNESS_COMMAND_KEYS = {"args", "command", "commands", "script", "scripts"}


HARNESS_URL_KEYS = {"baseUrl", "endpoint", "host", "misp_url", "url"}


HARNESS_ALLOWED_URL_RE = re.compile(
    r"https?://(?:example[.](?:com|org|net|invalid)|localhost|127[.]0[.]0[.]1|0[.]0[.]0[.]0)(?::\d+)?(?:[/?#][^\s]*)?$",
    re.IGNORECASE,
)


SESSION_CHAT_PARTS = {
    "chat",
    "chats",
    "conversation",
    "conversations",
    "session",
    "sessions",
    "transcript",
    "transcripts",
}


@dataclass(frozen=True)
class SecurityEnrichmentConfig:
    provider: str | None = None
    misp_url: str | None = None
    misp_api_key_env: str = "MISP_API_KEY"
    timeout_seconds: int = 10
    cache_path: str = ".brigade/security/enrichment-cache.json"


@dataclass(frozen=True)
class SecurityConfig:
    policy: str = "personal"
    scan_profile: str = "local-only-audit"
    fail_on: str | None = None
    include_templates: bool | None = None
    enabled_checks: tuple[str, ...] = SECURITY_CHECKS
    include_paths: tuple[str, ...] = ()
    exclude_paths: tuple[str, ...] = DEFAULT_EXCLUDE_PATHS
    severity_threshold: str = "low"
    output_path: str = ARTIFACTS_REL_PATH
    suppressions: tuple[str, ...] = ()
    suppression_reasons: dict[str, str] = field(default_factory=dict)
    enrichment: SecurityEnrichmentConfig = field(default_factory=SecurityEnrichmentConfig)


@dataclass(frozen=True)
class EffectivePolicy:
    policy: str
    scan_profile: str
    fail_on: str
    include_templates: bool
    enabled_checks: tuple[str, ...]
    include_paths: tuple[str, ...]
    exclude_paths: tuple[str, ...]
    severity_threshold: str
    output_path: str
    suppressions: tuple[str, ...]
    config_path: Path
    config_loaded: bool


@dataclass(frozen=True)
class FileClassification:
    surface: str
    confidence: str


def config_path(target: Path) -> Path:
    return target / CONFIG_REL_PATH


def default_artifacts_dir(target: Path) -> Path:
    return target / ARTIFACTS_REL_PATH


def suppression_health_cache_path(target: Path) -> Path:
    return target / SUPPRESSION_HEALTH_CACHE_REL_PATH


def fingerprint_migration_map_path(target: Path) -> Path:
    return target / FINGERPRINT_MIGRATION_MAP_REL_PATH


def _workspace_state_path_is_safe(target: Path, path: Path) -> bool:
    target_resolved = target.expanduser().resolve()
    try:
        relative = path.relative_to(target_resolved)
    except ValueError:
        return False
    candidate = target_resolved
    for part in relative.parts:
        candidate /= part
        if candidate.is_symlink():
            return False
    try:
        path.resolve(strict=False).relative_to(target_resolved)
    except (OSError, ValueError):
        return False
    return True


def _closeout_path_is_symlink(path: Path) -> bool:
    return path.is_symlink()


def _closeout_path_contained_in(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def _closeouts_root(target: Path) -> Path:
    return target / ".brigade" / "security" / "closeouts"


def _read_fingerprint_migration_map(target: Path) -> dict[str, str]:
    target_resolved = target.expanduser().resolve()
    path = fingerprint_migration_map_path(target_resolved)
    if not _workspace_state_path_is_safe(target_resolved, path):
        return {}
    payload = localio.read_json_dict(path)
    if payload is None:
        return {}
    if payload.get("schema") != FINGERPRINT_MIGRATION_MAP_SCHEMA:
        return {}
    raw_migrations = payload.get("migrations")
    if not isinstance(raw_migrations, dict):
        return {}
    migrations: dict[str, str] = {}
    for legacy, primary in raw_migrations.items():
        if not isinstance(legacy, str) or not isinstance(primary, str):
            continue
        legacy = legacy.strip()
        primary = primary.strip()
        if FINGERPRINT_RE.fullmatch(legacy) and FINGERPRINT_RE.fullmatch(primary):
            migrations[legacy] = primary
    return migrations


def _write_fingerprint_migration_map(target: Path, migrations: dict[str, str]) -> bool:
    if not migrations:
        return True
    target_resolved = target.expanduser().resolve()
    path = fingerprint_migration_map_path(target_resolved)
    if not _workspace_state_path_is_safe(target_resolved, path):
        return False
    _write_json(
        path,
        {
            "schema": FINGERPRINT_MIGRATION_MAP_SCHEMA,
            "migrations": dict(sorted(migrations.items())),
        },
    )
    return True


def _merge_fingerprint_migration_map(target: Path, entries: list[dict[str, str]]) -> bool:
    merged = _read_fingerprint_migration_map(target)
    changed = False
    for entry in entries:
        legacy = str(entry.get("from") or "").strip()
        primary = str(entry.get("to") or "").strip()
        if not legacy or not primary:
            continue
        if merged.get(legacy) == primary:
            continue
        merged[legacy] = primary
        changed = True
    if changed:
        return _write_fingerprint_migration_map(target, merged)
    return True


def _read_closeouts(target: Path) -> list[dict[str, Any]]:
    target_resolved = target.expanduser().resolve()
    root = _closeouts_root(target_resolved)
    receipts: list[dict[str, Any]] = []
    if not root.is_dir() or _closeout_path_is_symlink(root):
        return receipts
    if not _closeout_path_contained_in(root, target_resolved):
        return receipts
    try:
        root_resolved = root.resolve()
    except OSError:
        return receipts
    for closeout_dir in sorted(item for item in root.iterdir() if item.is_dir()):
        if _closeout_path_is_symlink(closeout_dir):
            continue
        if not _closeout_path_contained_in(closeout_dir, root_resolved):
            continue
        closeout_json = closeout_dir / "closeout.json"
        if not closeout_json.is_file() or _closeout_path_is_symlink(closeout_json):
            continue
        try:
            trusted_path = closeout_json.resolve()
        except OSError:
            continue
        if not _closeout_path_contained_in(trusted_path, root_resolved):
            continue
        payload = _read_json(trusted_path)
        if payload is None:
            continue
        payload.setdefault("closeout_id", closeout_dir.name)
        payload["path"] = str(trusted_path)
        receipts.append(payload)
    return sorted(receipts, key=lambda item: str(item.get("created_at") or item.get("closeout_id") or ""), reverse=True)


def inspect_evidence_bundle(path: Path) -> dict[str, Any]:
    path = path.expanduser().resolve()
    json_path = path / "security-report.json"
    markdown_path = path / "security-report.md"
    sarif_path = path / "security-report.sarif"
    enrichment_path = path / "security-enrichment.json"
    if not path.is_dir():
        return {"ready": False, "path": str(path), "reason": "missing"}
    missing = [item.name for item in (json_path, markdown_path) if not item.is_file()]
    if missing:
        return {"ready": False, "path": str(path), "reason": f"missing {', '.join(missing)}"}
    try:
        payload = json.loads(json_path.read_text(encoding="utf-8"))
    except OSError:
        return {"ready": False, "path": str(path), "reason": "unreadable security-report.json"}
    except UnicodeDecodeError:
        return {"ready": False, "path": str(path), "reason": "security-report.json must be UTF-8"}
    except json.JSONDecodeError:
        return {"ready": False, "path": str(path), "reason": "security-report.json is invalid JSON"}
    if not isinstance(payload, dict):
        return {"ready": False, "path": str(path), "reason": "security-report.json must contain an object"}
    findings = payload.get("findings")
    if not isinstance(findings, list):
        return {"ready": False, "path": str(path), "reason": "security-report.json findings must be a list"}
    suppressed_findings = payload.get("suppressed_findings", [])
    if not isinstance(suppressed_findings, list):
        return {"ready": False, "path": str(path), "reason": "security-report.json suppressed_findings must be a list"}
    if any(not isinstance(item, dict) for item in [*findings, *suppressed_findings]):
        return {"ready": False, "path": str(path), "reason": "security-report.json findings must contain objects"}
    return {
        "ready": True,
        "path": str(path),
        "generated_at": payload.get("generated_at"),
        "finding_count": payload.get("finding_count"),
        "policy": payload.get("policy"),
        "sarif_ready": sarif_path.is_file(),
        "sarif_path": str(sarif_path) if sarif_path.is_file() else None,
        "enrichment_ready": enrichment_path.is_file(),
    }
