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
    exclude_paths: tuple[str, ...] = ()
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


def _closeouts_root(target: Path) -> Path:
    return target / ".brigade" / "security" / "closeouts"


def _read_closeouts(target: Path) -> list[dict[str, Any]]:
    root = _closeouts_root(target.expanduser().resolve())
    receipts: list[dict[str, Any]] = []
    if not root.is_dir():
        return receipts
    for path in sorted(root.glob("*/closeout.json")):
        payload = _read_json(path)
        if payload is None:
            continue
        payload.setdefault("closeout_id", path.parent.name)
        payload.setdefault("path", str(path))
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
        payload = json.loads(json_path.read_text())
    except json.JSONDecodeError as exc:
        return {"ready": False, "path": str(path), "reason": f"invalid JSON: {exc}"}
    if not isinstance(payload, dict):
        return {"ready": False, "path": str(path), "reason": "security-report.json must contain an object"}
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
