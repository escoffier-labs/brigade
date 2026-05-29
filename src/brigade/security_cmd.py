"""Read-only security scanner for agent workspaces."""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import urlparse

from . import work_cmd

SEVERITY_ORDER = {
    "info": 0,
    "low": 1,
    "medium": 2,
    "high": 3,
    "critical": 4,
}
CONFIG_REL_PATH = ".brigade/security.toml"
ARTIFACTS_REL_PATH = ".brigade/security/latest"
POLICIES = {
    "personal": {
        "fail_on": "critical",
        "include_templates": False,
    },
    "public-repo": {
        "fail_on": "high",
        "include_templates": False,
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
PRIVATE_KEY_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
ENV_ASSIGNMENT_RE = re.compile(r"(?i)\b[A-Z0-9_]*(TOKEN|SECRET|PASSWORD|API_KEY)\s*=\s*[A-Za-z0-9_./+=:-]{16,}")
REMOTE_SHELL_RE = re.compile(r"\b(curl|wget)\b[^\n|;]*(\||;)\s*(sh|bash)\b")
DESTRUCTIVE_RE = re.compile(r"\b(rm\s+-rf|git\s+reset\s+--hard|git\s+clean\s+-fdx|chmod\s+777)\b")
UNPINNED_NPX_RE = re.compile(r"\bnpx\s+(?:-y\s+)?([a-zA-Z0-9_.-]+)(?:\s|$)")
ENV_DUMP_RE = re.compile(r"\b(env|printenv|set)\b.*(>\s*\S+|\|\s*(curl|nc|netcat|tee))")
UNPINNED_ACTION_RE = re.compile(r"uses:\s*['\"]?([^@\s'\":]+/[^@\s'\"]+|docker://[^@\s'\"]+)['\"]?\s*$")
PINNED_ACTION_RE = re.compile(r"uses:\s*['\"]?([^@\s'\"]+)@([^@\s'\"]+)")
PYTHON_URL_DEP_RE = re.compile(r"(?i)(https?://|git\+https?://|git\+ssh://)")
HTTP_MCP_RE = re.compile(r'"url"\s*:\s*"https?://')
AUTO_APPROVE_RE = re.compile(r"(?i)(auto[_-]?approve|always[_-]?allow|allow[_-]?all)")
PROMPT_INJECTION_RE = re.compile(
    r"(?i)(ignore (all )?(previous|prior) instructions|do not (tell|reveal)|hidden instruction|"
    r"send (all )?(secrets|tokens)|exfiltrat|disable safety|bypass safety)"
)
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


def config_path(target: Path) -> Path:
    return target / CONFIG_REL_PATH


def default_artifacts_dir(target: Path) -> Path:
    return target / ARTIFACTS_REL_PATH


def inspect_evidence_bundle(path: Path) -> dict[str, Any]:
    path = path.expanduser().resolve()
    json_path = path / "security-report.json"
    markdown_path = path / "security-report.md"
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
        "enrichment_ready": enrichment_path.is_file(),
    }


def _parse_toml_value(raw: str) -> object:
    value = raw.strip()
    if value == "true":
        return True
    if value == "false":
        return False
    try:
        return ast.literal_eval(value)
    except (SyntaxError, ValueError):
        return value


def _read_toml_object(path: Path) -> dict[str, object]:
    data: dict[str, object] = {}
    current = data
    for line_number, raw_line in enumerate(path.read_text().splitlines(), start=1):
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            table = line[1:-1].strip()
            if table not in {"suppressions", "suppression_reasons", "enrichment"}:
                raise ValueError(f"invalid security config line {line_number}: unsupported table [{table}]")
            current = data.setdefault(table, {})
            if not isinstance(current, dict):
                raise ValueError(f"invalid security config line {line_number}: {table} must be a table")
            continue
        if "=" not in line:
            raise ValueError(f"invalid security config line {line_number}: expected key = value")
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError(f"invalid security config line {line_number}: empty key")
        current[key] = _parse_toml_value(raw_value)
    return data


def load_config(target: Path) -> SecurityConfig | None:
    path = config_path(target.expanduser().resolve())
    if not path.is_file():
        return None
    data = _read_toml_object(path)
    policy = data.get("policy", "personal")
    if not isinstance(policy, str) or policy not in POLICIES:
        raise ValueError("policy must be one of: personal, public-repo, strict")
    scan_profile = data.get("scan_profile", "local-only-audit")
    if not isinstance(scan_profile, str) or scan_profile not in SCAN_PROFILES:
        raise ValueError("scan_profile must be one of: public-repo, internal-workspace, local-only-audit")
    fail_on = data.get("fail_on")
    if fail_on is not None and (not isinstance(fail_on, str) or fail_on not in SEVERITY_ORDER and fail_on != "none"):
        raise ValueError("fail_on must be one of: none, low, medium, high, critical")
    include_templates = data.get("include_templates")
    if include_templates is not None and not isinstance(include_templates, bool):
        raise ValueError("include_templates must be true or false")
    enabled_checks = _parse_string_list(
        data.get("enabled_checks", list(SECURITY_CHECKS)),
        field_name="enabled_checks",
        allowed=SECURITY_CHECKS,
    )
    include_paths = _parse_string_list(data.get("include_paths", []), field_name="include_paths")
    exclude_paths = _parse_string_list(data.get("exclude_paths", []), field_name="exclude_paths")
    severity_threshold = data.get("severity_threshold", "low")
    if not isinstance(severity_threshold, str) or severity_threshold not in SEVERITY_ORDER:
        raise ValueError("severity_threshold must be one of: info, low, medium, high, critical")
    output_path = data.get("output_path", ARTIFACTS_REL_PATH)
    if not isinstance(output_path, str) or not output_path.strip():
        raise ValueError("output_path must be a non-empty relative path")
    output = Path(output_path)
    if output.is_absolute() or ".." in output.parts:
        raise ValueError("output_path must be relative and must not contain '..'")
    suppressions: tuple[str, ...] = ()
    raw_suppressions = data.get("suppressions", {})
    if raw_suppressions:
        if not isinstance(raw_suppressions, dict):
            raise ValueError("suppressions must be a table")
        fingerprints = raw_suppressions.get("fingerprints", [])
        if not isinstance(fingerprints, list) or not all(isinstance(item, str) for item in fingerprints):
            raise ValueError("suppressions.fingerprints must be a list of strings")
        suppressions = tuple(item.strip() for item in fingerprints if item.strip())
    suppression_reasons: dict[str, str] = {}
    raw_reasons = data.get("suppression_reasons", {})
    if raw_reasons:
        if not isinstance(raw_reasons, dict):
            raise ValueError("suppression_reasons must be a table")
        for fingerprint, reason in raw_reasons.items():
            if not isinstance(fingerprint, str) or not isinstance(reason, str):
                raise ValueError("suppression_reasons entries must be string = string")
            if fingerprint.strip() and reason.strip():
                suppression_reasons[fingerprint.strip()] = reason.strip()
    enrichment = _parse_enrichment_config(data.get("enrichment", {}))
    return SecurityConfig(
        policy=policy,
        scan_profile=scan_profile,
        fail_on=fail_on,
        include_templates=include_templates,
        enabled_checks=enabled_checks,
        include_paths=include_paths,
        exclude_paths=exclude_paths,
        severity_threshold=severity_threshold,
        output_path=output_path.strip(),
        suppressions=suppressions,
        suppression_reasons=suppression_reasons,
        enrichment=enrichment,
    )


def _parse_string_list(raw: object, *, field_name: str, allowed: tuple[str, ...] | None = None) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise ValueError(f"{field_name} must be a list of strings")
    values = tuple(item.strip() for item in raw if item.strip())
    if allowed is not None:
        bad = [item for item in values if item not in allowed]
        if bad:
            raise ValueError(f"{field_name} entries must be one of: {', '.join(allowed)}")
    return values


def _parse_enrichment_config(raw: object) -> SecurityEnrichmentConfig:
    if raw in ({}, None):
        return SecurityEnrichmentConfig()
    if not isinstance(raw, dict):
        raise ValueError("enrichment must be a table")
    provider = raw.get("provider")
    if provider is not None:
        if not isinstance(provider, str) or provider not in ENRICHMENT_PROVIDERS:
            raise ValueError("enrichment.provider must be one of: local, misp")
    misp_url = raw.get("misp_url")
    if misp_url is not None and not isinstance(misp_url, str):
        raise ValueError("enrichment.misp_url must be a string")
    misp_api_key_env = raw.get("misp_api_key_env", "MISP_API_KEY")
    if not isinstance(misp_api_key_env, str) or not misp_api_key_env.strip():
        raise ValueError("enrichment.misp_api_key_env must be a non-empty string")
    timeout_seconds = raw.get("timeout_seconds", 10)
    if not isinstance(timeout_seconds, int) or timeout_seconds <= 0:
        raise ValueError("enrichment.timeout_seconds must be a positive integer")
    cache_path = raw.get("cache_path", ".brigade/security/enrichment-cache.json")
    if not isinstance(cache_path, str) or not cache_path.strip():
        raise ValueError("enrichment.cache_path must be a non-empty relative path")
    cache = Path(cache_path)
    if cache.is_absolute() or ".." in cache.parts:
        raise ValueError("enrichment.cache_path must be relative and must not contain '..'")
    return SecurityEnrichmentConfig(
        provider=provider,
        misp_url=misp_url.strip() if isinstance(misp_url, str) and misp_url.strip() else None,
        misp_api_key_env=misp_api_key_env.strip(),
        timeout_seconds=timeout_seconds,
        cache_path=cache_path.strip(),
    )


def _effective_policy(
    target: Path,
    *,
    policy: str | None,
    fail_on: str | None,
    include_templates: bool | None,
) -> EffectivePolicy:
    loaded = load_config(target)
    policy_name = policy or (loaded.policy if loaded is not None else "personal")
    if policy_name not in POLICIES:
        raise ValueError("policy must be one of: personal, public-repo, strict")
    preset = POLICIES[policy_name]
    effective_fail_on = fail_on or (loaded.fail_on if loaded and loaded.fail_on is not None else str(preset["fail_on"]))
    if include_templates is not None:
        effective_include_templates = include_templates
    elif loaded and loaded.include_templates is not None:
        effective_include_templates = loaded.include_templates
    else:
        effective_include_templates = bool(preset["include_templates"])
    if effective_fail_on not in SEVERITY_ORDER and effective_fail_on != "none":
        raise ValueError("fail_on must be one of: none, low, medium, high, critical")
    return EffectivePolicy(
        policy=policy_name,
        scan_profile=loaded.scan_profile if loaded is not None else "local-only-audit",
        fail_on=effective_fail_on,
        include_templates=effective_include_templates,
        enabled_checks=loaded.enabled_checks if loaded is not None else SECURITY_CHECKS,
        include_paths=loaded.include_paths if loaded is not None else (),
        exclude_paths=loaded.exclude_paths if loaded is not None else (),
        severity_threshold=loaded.severity_threshold if loaded is not None else "low",
        output_path=loaded.output_path if loaded is not None else ARTIFACTS_REL_PATH,
        suppressions=loaded.suppressions if loaded is not None else (),
        config_path=config_path(target),
        config_loaded=loaded is not None,
    )


def write_default_config(target: Path, *, force: bool = False) -> Path:
    path = config_path(target.expanduser().resolve())
    if path.exists() and not force:
        raise FileExistsError(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "# Local security scanner config. Keep secrets and host-private paths out of this file.",
                "# scan_profile options: public-repo, internal-workspace, local-only-audit",
                'policy = "personal"',
                'scan_profile = "local-only-audit"',
                'fail_on = "critical"',
                "include_templates = false",
                'enabled_checks = ["automation", "mcp", "permissions", "prompt-injection", "secrets", "supply-chain"]',
                "include_paths = []",
                "exclude_paths = []",
                'severity_threshold = "low"',
                'output_path = ".brigade/security/latest"',
                "",
                "[suppressions]",
                "fingerprints = []",
                "",
                "[suppression_reasons]",
                "",
                "[enrichment]",
                'provider = "local"',
                'misp_url = ""',
                'misp_api_key_env = "MISP_API_KEY"',
                "timeout_seconds = 10",
                'cache_path = ".brigade/security/enrichment-cache.json"',
                "",
            ]
        )
    )
    return path


def _toml_string(value: str) -> str:
    return json.dumps(value)


def write_config(target: Path, config: SecurityConfig) -> Path:
    path = config_path(target.expanduser().resolve())
    path.parent.mkdir(parents=True, exist_ok=True)
    fingerprints = ", ".join(_toml_string(item) for item in config.suppressions)
    enrichment = config.enrichment
    lines = [
        f"policy = {_toml_string(config.policy)}",
        f"scan_profile = {_toml_string(config.scan_profile)}",
        f"fail_on = {_toml_string(config.fail_on or POLICIES[config.policy]['fail_on'])}",
        f"include_templates = {str(config.include_templates if config.include_templates is not None else POLICIES[config.policy]['include_templates']).lower()}",
        f"enabled_checks = [{', '.join(_toml_string(item) for item in config.enabled_checks)}]",
        f"include_paths = [{', '.join(_toml_string(item) for item in config.include_paths)}]",
        f"exclude_paths = [{', '.join(_toml_string(item) for item in config.exclude_paths)}]",
        f"severity_threshold = {_toml_string(config.severity_threshold)}",
        f"output_path = {_toml_string(config.output_path)}",
        "",
        "[suppressions]",
        f"fingerprints = [{fingerprints}]",
        "",
        "[suppression_reasons]",
    ]
    reasons = config.suppression_reasons
    for fingerprint in config.suppressions:
        reason = reasons.get(fingerprint)
        if reason:
            lines.append(f"{fingerprint} = {_toml_string(reason)}")
    lines.extend(
        [
            "",
            "[enrichment]",
            f"provider = {_toml_string(enrichment.provider or 'local')}",
            f"misp_url = {_toml_string(enrichment.misp_url or '')}",
            f"misp_api_key_env = {_toml_string(enrichment.misp_api_key_env)}",
            f"timeout_seconds = {enrichment.timeout_seconds}",
            f"cache_path = {_toml_string(enrichment.cache_path)}",
        ]
    )
    lines.append("")
    path.write_text("\n".join(lines))
    return path


def _load_config_or_default(target: Path) -> SecurityConfig:
    loaded = load_config(target)
    if loaded is not None:
        return loaded
    return SecurityConfig()


def _clean_reason(reason: str) -> str:
    return " ".join(reason.replace("#", " ").split()).strip()


def _gitignore_selection(target: Path):
    from .config import load_config
    from .selection import Selection

    loaded = load_config(target)
    if loaded is not None:
        return loaded.selection
    return Selection(depth="repo", harnesses=[], owner="this-repo", includes=[])


def fix(*, target: Path, dry_run: bool = False) -> int:
    from . import dogfood_cmd
    from .install import apply_gitignore

    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    try:
        selection = _gitignore_selection(target)
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"error: invalid Brigade config: {exc}", file=sys.stderr)
        return 2

    artifacts_root = default_artifacts_dir(target).parent
    print(f"security fix: {target}")
    if dry_run:
        print("dry_run: True")
        print(f"would_create: {artifacts_root}")
        print("would_update: .gitignore")
        return 0

    artifacts_root.mkdir(parents=True, exist_ok=True)
    result = apply_gitignore(target, selection)
    config_ignored = dogfood_cmd._check_git_ignored(target, config_path(target))
    artifacts_ignored = dogfood_cmd._check_git_ignored(target, artifacts_root)
    print(f"security_artifacts_dir: {artifacts_root}")
    print(f"gitignore: {result}")
    print(f"security_config_ignored: {config_ignored}")
    print(f"security_artifacts_ignored: {artifacts_ignored}")
    return 0


def _load_report(output_dir: Path) -> dict[str, Any]:
    path = output_dir.expanduser().resolve() / "security-report.json"
    return _load_report_file(path)


def _load_report_file(path: Path) -> dict[str, Any]:
    path = path.expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"security report must be a JSON object: {path}")
    return data


def _report_findings_for_review(target: Path, report: dict[str, Any]) -> list[dict[str, Any]]:
    config = load_config(target) or SecurityConfig()
    suppressed = set(config.suppressions)
    reasons = config.suppression_reasons
    records: list[dict[str, Any]] = []
    for finding in report.get("findings", []):
        if not isinstance(finding, dict):
            continue
        fingerprint = str(finding.get("fingerprint") or "")
        record = dict(finding)
        record["status"] = "suppressed" if fingerprint in suppressed else "open"
        if fingerprint in reasons:
            record["reason"] = reasons[fingerprint]
        records.append(record)
    for finding in report.get("suppressed_findings", []):
        if not isinstance(finding, dict):
            continue
        fingerprint = str(finding.get("fingerprint") or "")
        record = dict(finding)
        record["status"] = "suppressed"
        if fingerprint in reasons:
            record["reason"] = reasons[fingerprint]
        records.append(record)
    records.sort(
        key=lambda item: (
            -SEVERITY_ORDER.get(str(item.get("severity")), 0),
            str(item.get("category") or ""),
            str(item.get("path") or ""),
            int(item.get("line") or 0),
        )
    )
    return records


def review(*, target: Path, output_dir: Path | None = None, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    artifacts_dir = output_dir.expanduser().resolve() if output_dir is not None else default_artifacts_dir(target)
    try:
        report = _load_report(artifacts_dir)
        records = _report_findings_for_review(target, report)
    except FileNotFoundError as exc:
        print(f"error: security report not found: {exc}", file=sys.stderr)
        return 2
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"error: invalid security report: {exc}", file=sys.stderr)
        return 2

    payload = {
        "artifacts": str(artifacts_dir),
        "generated_at": report.get("generated_at"),
        "policy": report.get("policy"),
        "findings": records,
        "finding_count": len(records),
        "open_count": len([item for item in records if item.get("status") != "suppressed"]),
        "suppressed_count": len([item for item in records if item.get("status") == "suppressed"]),
    }
    enrichment = _load_enrichment_payload(artifacts_dir)
    if enrichment is not None:
        payload["enrichment"] = enrichment
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    print(f"security review: {artifacts_dir}")
    print(f"generated_at: {payload['generated_at']}")
    print(f"policy: {payload['policy']}")
    print(f"findings: {payload['finding_count']}")
    print(f"open: {payload['open_count']}")
    print(f"suppressed: {payload['suppressed_count']}")
    current_group: tuple[str, str] | None = None
    for finding in records:
        group = (str(finding.get("severity") or "unknown"), str(finding.get("category") or "unknown"))
        if group != current_group:
            current_group = group
            print(f"{group[0]} / {group[1]}:")
        print(
            f"- {finding.get('fingerprint')} [{finding.get('status')}] "
            f"{finding.get('path')}:{finding.get('line')} {finding.get('title')}"
        )
        if finding.get("reason"):
            print(f"  reason: {finding['reason']}")
        print(f"  suggestion: {finding.get('suggestion')}")
    if enrichment is not None:
        print("enrichment:")
        print(f"- provider: {enrichment.get('provider')}")
        print(f"- indicators: {enrichment.get('indicator_count')}")
        print(f"- hits: {enrichment.get('hit_count')}")
    return 0


def findings(*, target: Path, output_dir: Path | None = None, json_output: bool = False) -> int:
    return review(target=target, output_dir=output_dir, json_output=json_output)


def _resolve_finding_record(target: Path, identifier: str, output_dir: Path | None = None) -> tuple[dict[str, Any] | None, str | None]:
    artifacts_dir = output_dir.expanduser().resolve() if output_dir is not None else default_artifacts_dir(target)
    try:
        report = _load_report(artifacts_dir)
        records = _report_findings_for_review(target, report)
    except FileNotFoundError as exc:
        return None, f"security report not found: {exc}"
    except (ValueError, json.JSONDecodeError) as exc:
        return None, f"invalid security report: {exc}"
    needle = identifier.strip()
    matches = [
        item
        for item in records
        if needle
        and (
            str(item.get("id") or "") == needle
            or str(item.get("fingerprint") or "") == needle
            or str(item.get("id") or "").startswith(needle)
            or str(item.get("fingerprint") or "").startswith(needle)
        )
    ]
    if not matches:
        return None, f"finding not found: {identifier}"
    if len(matches) > 1:
        return None, f"finding id is ambiguous: {identifier}"
    return matches[0], None


def show(*, target: Path, finding_id: str, output_dir: Path | None = None, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    finding, message = _resolve_finding_record(target, finding_id, output_dir=output_dir)
    if finding is None:
        print(f"error: {message}", file=sys.stderr)
        return 1 if message and "not found" in message else 2
    if json_output:
        print(json.dumps({"finding": finding}, indent=2, sort_keys=True))
        return 0
    print(f"security finding: {finding.get('id')}")
    print(f"status: {finding.get('status', 'open')}")
    print(f"fingerprint: {finding.get('fingerprint')}")
    print(f"rule_id: {finding.get('rule_id')}")
    print(f"severity: {finding.get('severity')}")
    print(f"category: {finding.get('category')}")
    print(f"path: {finding.get('path')}:{finding.get('line')}")
    print(f"title: {finding.get('title')}")
    print(f"safe_excerpt: {finding.get('safe_excerpt') or finding.get('evidence')}")
    print(f"remediation: {finding.get('remediation_hint') or finding.get('suggestion')}")
    if finding.get("reason"):
        print(f"reason: {finding['reason']}")
    return 0


def _load_enrichment_payload(output_dir: Path) -> dict[str, Any] | None:
    path = output_dir.expanduser().resolve() / "security-enrichment.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return {"ready": False, "path": str(path), "reason": "invalid JSON"}
    if not isinstance(data, dict):
        return {"ready": False, "path": str(path), "reason": "security-enrichment.json must contain an object"}
    return data


def _indicator_source(finding: dict[str, Any]) -> dict[str, Any]:
    return {
        "fingerprint": finding.get("fingerprint"),
        "title": finding.get("title"),
        "path": finding.get("path"),
        "line": finding.get("line"),
        "category": finding.get("category"),
    }


def _add_indicator(
    indicators: list[dict[str, Any]],
    seen: set[tuple[str, str]],
    *,
    kind: str,
    value: str,
    finding: dict[str, Any],
) -> None:
    value = value.strip().strip(".,);]")
    if not value:
        return
    key = (kind, value.lower())
    if key in seen:
        for indicator in indicators:
            if indicator["type"] == kind and indicator["value"].lower() == value.lower():
                indicator["sources"].append(_indicator_source(finding))
                return
    seen.add(key)
    indicators.append({"type": kind, "value": value, "sources": [_indicator_source(finding)]})


def _extract_enrichment_indicators(report: dict[str, Any]) -> list[dict[str, Any]]:
    indicators: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for finding in list(report.get("findings", [])) + list(report.get("suppressed_findings", [])):
        if not isinstance(finding, dict):
            continue
        evidence = str(finding.get("evidence") or "")
        title = str(finding.get("title") or "")
        for match in INDICATOR_URL_RE.finditer(evidence):
            url = match.group(0)
            _add_indicator(indicators, seen, kind="url", value=url, finding=finding)
            parsed = urlparse(url)
            if parsed.hostname:
                _add_indicator(indicators, seen, kind="domain", value=parsed.hostname.lower(), finding=finding)
        npx_match = INDICATOR_NPX_RE.search(evidence)
        if npx_match:
            _add_indicator(indicators, seen, kind="npm-package", value=npx_match.group(1), finding=finding)
        action_match = INDICATOR_GITHUB_ACTION_RE.search(evidence)
        if action_match and "GitHub Action" in title:
            _add_indicator(indicators, seen, kind="github-action", value=action_match.group(1), finding=finding)
    return indicators


def _local_enrich(indicators: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for indicator in indicators:
        results.append(
            {
                "provider": "local",
                "type": indicator["type"],
                "value": indicator["value"],
                "status": "observed",
                "match_count": 0,
                "cache_hit": False,
                "summary": "Observed in the local security report; no external lookup was performed.",
                "source_fingerprints": [source.get("fingerprint") for source in indicator["sources"] if source.get("fingerprint")],
            }
        )
    return results


def _cache_file(target: Path, config: SecurityEnrichmentConfig) -> Path:
    return target / config.cache_path


def _read_enrichment_cache(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _write_enrichment_cache(path: Path, cache: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, indent=2, sort_keys=True) + "\n")


def _misp_query_indicator(
    *,
    base_url: str,
    api_key: str,
    timeout_seconds: int,
    indicator: dict[str, Any],
) -> dict[str, Any]:
    endpoint = base_url.rstrip("/") + "/attributes/restSearch"
    body = json.dumps({"returnFormat": "json", "value": indicator["value"]}).encode()
    req = urlrequest.Request(
        endpoint,
        data=body,
        headers={
            "Authorization": api_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urlrequest.urlopen(req, timeout=timeout_seconds) as response:
        raw = response.read().decode("utf-8", errors="replace")
    payload = json.loads(raw) if raw.strip() else {}
    attributes = _misp_attributes(payload)
    tags = sorted(
        {
            str(tag.get("name"))
            for attribute in attributes
            if isinstance(attribute, dict)
            for tag in attribute.get("Tag", [])
            if isinstance(tag, dict) and tag.get("name")
        }
    )
    return {
        "provider": "misp",
        "type": indicator["type"],
        "value": indicator["value"],
        "status": "hit" if attributes else "miss",
        "match_count": len(attributes),
        "tags": tags[:10],
        "cache_hit": False,
        "summary": f"MISP returned {len(attributes)} attribute match(es).",
        "source_fingerprints": [source.get("fingerprint") for source in indicator["sources"] if source.get("fingerprint")],
    }


def _misp_attributes(payload: object) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        response = payload.get("response", payload)
        if isinstance(response, dict):
            attributes = response.get("Attribute", [])
            return [item for item in attributes if isinstance(item, dict)] if isinstance(attributes, list) else []
        if isinstance(response, list):
            return [item for item in response if isinstance(item, dict)]
    return []


def _misp_enrich(target: Path, config: SecurityEnrichmentConfig, indicators: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not config.misp_url:
        raise ValueError("enrichment.misp_url is required when provider is misp")
    api_key = os.environ.get(config.misp_api_key_env)
    if not api_key:
        raise ValueError(f"environment variable {config.misp_api_key_env} is required when provider is misp")
    cache_path = _cache_file(target, config)
    cache = _read_enrichment_cache(cache_path)
    results: list[dict[str, Any]] = []
    changed = False
    for indicator in indicators:
        cache_key = f"misp:{indicator['type']}:{indicator['value'].lower()}"
        cached = cache.get(cache_key)
        if isinstance(cached, dict):
            result = dict(cached)
            result["cache_hit"] = True
            results.append(result)
            continue
        try:
            result = _misp_query_indicator(
                base_url=config.misp_url,
                api_key=api_key,
                timeout_seconds=config.timeout_seconds,
                indicator=indicator,
            )
        except (OSError, urlerror.URLError, json.JSONDecodeError) as exc:
            result = {
                "provider": "misp",
                "type": indicator["type"],
                "value": indicator["value"],
                "status": "error",
                "match_count": 0,
                "cache_hit": False,
                "summary": f"MISP lookup failed: {exc}",
                "source_fingerprints": [source.get("fingerprint") for source in indicator["sources"] if source.get("fingerprint")],
            }
        cache[cache_key] = result
        changed = True
        results.append(result)
    if changed:
        _write_enrichment_cache(cache_path, cache)
    return results


def _render_enrichment_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Brigade Security Enrichment",
        "",
        f"- provider: `{payload['provider']}`",
        f"- generated_at: `{payload['generated_at']}`",
        f"- report: `{payload['report']}`",
        f"- indicators: `{payload['indicator_count']}`",
        f"- hits: `{payload['hit_count']}`",
        f"- errors: `{payload['error_count']}`",
        "",
        "## Results",
        "",
    ]
    if not payload["results"]:
        lines.append("No enrichment indicators were extracted.")
    for result in payload["results"]:
        lines.extend(
            [
                f"### {result['type']} - {result['value']}",
                "",
                f"- status: `{result['status']}`",
                f"- matches: `{result['match_count']}`",
                f"- cache_hit: `{result.get('cache_hit', False)}`",
                f"- summary: {result['summary']}",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _render_enrichment_summary(payload: dict[str, Any]) -> str:
    return "\n".join(
        [
            ENRICHMENT_MARKDOWN_START,
            "## Enrichment",
            "",
            f"- provider: `{payload['provider']}`",
            f"- generated_at: `{payload['generated_at']}`",
            f"- indicators: `{payload['indicator_count']}`",
            f"- hits: `{payload['hit_count']}`",
            f"- errors: `{payload['error_count']}`",
            f"- details: `security-enrichment.md`",
            ENRICHMENT_MARKDOWN_END,
            "",
        ]
    )


def _upsert_report_enrichment_summary(output_dir: Path, payload: dict[str, Any]) -> None:
    report_markdown = output_dir / "security-report.md"
    if not report_markdown.is_file():
        return
    existing = report_markdown.read_text()
    summary = _render_enrichment_summary(payload)
    start = existing.find(ENRICHMENT_MARKDOWN_START)
    end = existing.find(ENRICHMENT_MARKDOWN_END)
    if start != -1 and end != -1 and end > start:
        end += len(ENRICHMENT_MARKDOWN_END)
        updated = existing[:start].rstrip() + "\n\n" + summary + existing[end:].lstrip()
    else:
        updated = existing.rstrip() + "\n\n" + summary
    report_markdown.write_text(updated)


def write_enrichment_bundle(payload: dict[str, Any], output_dir: Path) -> Path:
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = dict(payload)
    payload["artifacts"] = str(output_dir)
    (output_dir / "security-enrichment.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    (output_dir / "security-enrichment.md").write_text(_render_enrichment_markdown(payload))
    _upsert_report_enrichment_summary(output_dir, payload)
    return output_dir


def enrichment_health(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    config = load_config(target)
    if config is None:
        return {"configured": False, "provider": None, "status": "missing config"}
    provider = config.enrichment.provider
    if not provider:
        return {"configured": False, "provider": None, "status": "missing provider"}
    if provider == "local":
        return {"configured": True, "provider": provider, "status": "offline local provider"}
    if provider == "misp":
        missing = []
        if not config.enrichment.misp_url:
            missing.append("misp_url")
        if not os.environ.get(config.enrichment.misp_api_key_env):
            missing.append(config.enrichment.misp_api_key_env)
        return {
            "configured": not missing,
            "provider": provider,
            "status": "ready" if not missing else f"missing {', '.join(missing)}",
        }
    return {"configured": False, "provider": provider, "status": "unsupported provider"}


def enrich(
    *,
    target: Path,
    output_dir: Path | None = None,
    report_path: Path | None = None,
    provider: str | None = None,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    artifacts_dir = output_dir.expanduser().resolve() if output_dir is not None else default_artifacts_dir(target)
    report_file = report_path.expanduser().resolve() if report_path is not None else artifacts_dir / "security-report.json"
    try:
        loaded = load_config(target)
    except ValueError as exc:
        print(f"error: invalid security config: {exc}", file=sys.stderr)
        return 2
    config = loaded.enrichment if loaded is not None else SecurityEnrichmentConfig()
    provider_name = provider or config.provider
    if provider_name is None:
        print(
            "error: security enrichment provider is not configured; run `brigade security init` or pass `--provider local`",
            file=sys.stderr,
        )
        return 2
    if provider_name not in ENRICHMENT_PROVIDERS:
        print("error: --provider must be one of: local, misp", file=sys.stderr)
        return 2
    if provider is not None:
        config = SecurityEnrichmentConfig(
            provider=provider,
            misp_url=config.misp_url,
            misp_api_key_env=config.misp_api_key_env,
            timeout_seconds=config.timeout_seconds,
            cache_path=config.cache_path,
        )
    try:
        report = _load_report_file(report_file)
    except FileNotFoundError as exc:
        print(f"error: security report not found: {exc}", file=sys.stderr)
        return 2
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"error: invalid security report: {exc}", file=sys.stderr)
        return 2

    indicators = _extract_enrichment_indicators(report)
    try:
        if provider_name == "local":
            results = _local_enrich(indicators)
        else:
            results = _misp_enrich(target, config, indicators)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    payload = {
        "target": str(target),
        "report": str(report_file),
        "provider": provider_name,
        "generated_at": _utc_iso(),
        "indicator_count": len(indicators),
        "result_count": len(results),
        "hit_count": len([item for item in results if item.get("status") == "hit"]),
        "error_count": len([item for item in results if item.get("status") == "error"]),
        "indicators": indicators,
        "results": results,
    }
    artifacts_path = write_enrichment_bundle(payload, artifacts_dir)
    payload["artifacts"] = str(artifacts_path)

    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"security enrich: {target}")
        print(f"provider: {provider_name}")
        print(f"report: {report_file}")
        print(f"artifacts: {artifacts_path}")
        print(f"indicators: {payload['indicator_count']}")
        print(f"hits: {payload['hit_count']}")
        print(f"errors: {payload['error_count']}")
    return 1 if payload["error_count"] else 0


def suppress(*, target: Path, fingerprint: str, reason: str) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    fingerprint = fingerprint.strip()
    cleaned_reason = _clean_reason(reason)
    if not FINGERPRINT_RE.match(fingerprint):
        finding, message = _resolve_finding_record(target, fingerprint)
        if finding is None or not FINGERPRINT_RE.match(str(finding.get("fingerprint") or "")):
            print(f"error: {message or 'finding id or fingerprint is invalid'}", file=sys.stderr)
            return 2
        fingerprint = str(finding["fingerprint"])
    if not cleaned_reason:
        print("error: --reason is required", file=sys.stderr)
        return 2
    try:
        config = _load_config_or_default(target)
    except ValueError as exc:
        print(f"error: invalid security config: {exc}", file=sys.stderr)
        return 2
    suppressions = list(config.suppressions)
    if fingerprint not in suppressions:
        suppressions.append(fingerprint)
    reasons = dict(config.suppression_reasons)
    reasons[fingerprint] = cleaned_reason
    path = write_config(
        target,
        SecurityConfig(
            policy=config.policy,
            scan_profile=config.scan_profile,
            fail_on=config.fail_on,
            include_templates=config.include_templates,
            enabled_checks=config.enabled_checks,
            include_paths=config.include_paths,
            exclude_paths=config.exclude_paths,
            severity_threshold=config.severity_threshold,
            output_path=config.output_path,
            suppressions=tuple(suppressions),
            suppression_reasons=reasons,
            enrichment=config.enrichment,
        ),
    )
    print(f"security_config: {path}")
    print(f"suppressed: {fingerprint}")
    print(f"reason: {cleaned_reason}")
    return 0


def unsuppress(*, target: Path, fingerprint: str) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    fingerprint = fingerprint.strip()
    if not FINGERPRINT_RE.match(fingerprint):
        finding, message = _resolve_finding_record(target, fingerprint)
        if finding is None or not FINGERPRINT_RE.match(str(finding.get("fingerprint") or "")):
            print(f"error: {message or 'finding id or fingerprint is invalid'}", file=sys.stderr)
            return 2
        fingerprint = str(finding["fingerprint"])
    try:
        config = _load_config_or_default(target)
    except ValueError as exc:
        print(f"error: invalid security config: {exc}", file=sys.stderr)
        return 2
    if fingerprint not in config.suppressions and fingerprint not in config.suppression_reasons:
        print(f"error: suppression not found: {fingerprint}", file=sys.stderr)
        return 1
    suppressions = tuple(item for item in config.suppressions if item != fingerprint)
    reasons = dict(config.suppression_reasons)
    reasons.pop(fingerprint, None)
    path = write_config(
        target,
        SecurityConfig(
            policy=config.policy,
            scan_profile=config.scan_profile,
            fail_on=config.fail_on,
            include_templates=config.include_templates,
            enabled_checks=config.enabled_checks,
            include_paths=config.include_paths,
            exclude_paths=config.exclude_paths,
            severity_threshold=config.severity_threshold,
            output_path=config.output_path,
            suppressions=suppressions,
            suppression_reasons=reasons,
            enrichment=config.enrichment,
        ),
    )
    print(f"security_config: {path}")
    print(f"unsuppressed: {fingerprint}")
    return 0


def suppression_health(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    config = load_config(target)
    if config is None:
        return {"suppression_count": 0, "missing_reasons": [], "stale": []}
    if not config.suppressions:
        return {"suppression_count": 0, "missing_reasons": [], "stale": []}
    effective = _effective_policy(target, policy=None, fail_on=None, include_templates=None)
    report = scan_target(
        target,
        include_templates=effective.include_templates,
        suppressions=(),
        enabled_checks=effective.enabled_checks,
        include_paths=effective.include_paths,
        exclude_paths=effective.exclude_paths,
        severity_threshold=effective.severity_threshold,
    )
    active = {str(item.get("fingerprint")) for item in report["findings"] if item.get("fingerprint")}
    stale = [fingerprint for fingerprint in config.suppressions if fingerprint not in active]
    missing_reasons = [fingerprint for fingerprint in config.suppressions if not config.suppression_reasons.get(fingerprint)]
    return {
        "suppression_count": len(config.suppressions),
        "missing_reasons": missing_reasons,
        "stale": stale,
    }


def _short(text: str, limit: int = 160) -> str:
    rendered = " ".join(text.split())
    if len(rendered) <= limit:
        return rendered
    return rendered[: limit - 3].rstrip() + "..."


def _is_placeholder(value: str) -> bool:
    lowered = value.lower()
    return any(
        marker in lowered
        for marker in (
            "example",
            "placeholder",
            "changeme",
            "your_",
            "your-",
            "<",
            "{{",
            "xxxxx",
            "dummy",
        )
    )


def _redact_secret_evidence(line: str) -> str:
    if PRIVATE_KEY_RE.search(line):
        return PRIVATE_KEY_RE.sub("-----BEGIN REDACTED PRIVATE KEY-----", line)

    def redact_secret(match: re.Match[str]) -> str:
        return match.group(0).replace(match.group(2), "[REDACTED]")

    redacted = SECRET_VALUE_RE.sub(redact_secret, line)

    def redact_env(match: re.Match[str]) -> str:
        text = match.group(0)
        if "=" not in text:
            return "[REDACTED]"
        key, _ = text.split("=", 1)
        return f"{key}=[REDACTED]"

    return ENV_ASSIGNMENT_RE.sub(redact_env, redacted)


def _rule_id(category: str, title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return f"{category}.{slug or 'finding'}"


def _line_number_for(text: str, needle: str) -> int:
    if not needle:
        return 1
    for line_number, line in enumerate(text.splitlines(), start=1):
        if needle in line:
            return line_number
    return 1


def _is_mcp_document(path: Path, text: str) -> bool:
    return "mcp" in path.name.lower() or '"mcpServers"' in text


def _server_timeout(server: dict[str, Any]) -> object:
    for key in ("timeout", "timeout_seconds", "timeoutSeconds", "startupTimeout", "startupTimeoutMs"):
        if key in server:
            return server[key]
    return None


def _scan_mcp_document(findings: list[dict[str, Any]], *, target: Path, path: Path, text: str) -> None:
    if not _is_mcp_document(path, text):
        return
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return
    if not isinstance(data, dict):
        return
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        return
    if len(servers) > MCP_SERVER_COUNT_WARN:
        _finding(
            findings,
            target=target,
            path=path,
            line=_line_number_for(text, "mcpServers"),
            severity="low",
            category="mcp",
            title="Large MCP server set",
            evidence=f"mcpServers: {len(servers)} configured",
            suggestion="Review whether every MCP server is still needed and disable stale or duplicate servers.",
        )
    for server_name, raw_server in servers.items():
        if not isinstance(server_name, str) or not isinstance(raw_server, dict):
            continue
        server = raw_server
        line_number = _line_number_for(text, server_name)
        command = server.get("command")
        args = server.get("args", [])
        command_name = Path(command).name if isinstance(command, str) else None
        if command_name in MCP_HIGH_RISK_COMMANDS:
            _finding(
                findings,
                target=target,
                path=path,
                line=line_number,
                severity="medium",
                category="mcp",
                title="MCP high-risk local command",
                evidence=f"{server_name}: command={command}",
                suggestion="Prefer purpose-built MCP binaries with narrow capabilities over direct shell, container, or remote-copy commands.",
            )
        if isinstance(command, str) and command == "npx" and isinstance(args, list):
            package = _first_npx_package(args)
            if package and "@" not in package:
                _finding(
                    findings,
                    target=target,
                    path=path,
                    line=line_number,
                    severity="medium",
                    category="mcp",
                    title="MCP unpinned npx package",
                    evidence=f"{server_name}: npx {package}",
                    suggestion="Pin MCP package versions or install through a reviewed lockfile.",
                )
        if isinstance(args, list):
            for arg in args:
                if not isinstance(arg, str):
                    continue
                if MCP_SHELL_META_RE.search(arg):
                    _finding(
                        findings,
                        target=target,
                        path=path,
                        line=line_number,
                        severity="high",
                        category="mcp",
                        title="MCP shell metacharacter in argument",
                        evidence=f"{server_name}: arg={arg}",
                        suggestion="Remove shell metacharacters from MCP args and pass structured arguments directly.",
                    )
                if arg in MCP_BROAD_PATHS:
                    _finding(
                        findings,
                        target=target,
                        path=path,
                        line=line_number,
                        severity="medium",
                        category="mcp",
                        title="MCP broad filesystem argument",
                        evidence=f"{server_name}: arg={arg}",
                        suggestion="Scope MCP filesystem access to explicit project directories instead of home or filesystem roots.",
                    )
                if MCP_SENSITIVE_ARG_RE.search(arg):
                    _finding(
                        findings,
                        target=target,
                        path=path,
                        line=line_number,
                        severity="medium",
                        category="mcp",
                        title="MCP sensitive file argument",
                        evidence=f"{server_name}: arg={arg}",
                        suggestion="Avoid passing broad sensitive file paths to MCP servers; scope access to explicit project files.",
                    )
        env = server.get("env")
        if isinstance(env, dict):
            for key, value in env.items():
                if not isinstance(key, str) or not isinstance(value, str):
                    continue
                if re.search(r"(?i)(TOKEN|SECRET|PASSWORD|API_KEY)", key) and not _is_placeholder(value):
                    _finding(
                        findings,
                        target=target,
                        path=path,
                        line=line_number,
                        severity="high",
                        category="mcp",
                        title="MCP hardcoded environment secret",
                        evidence=_redact_secret_evidence(f"{server_name}.env.{key}={value}"),
                        suggestion="Load MCP secrets from local environment or secret storage instead of checked-in config.",
                    )
        url = server.get("url")
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            _finding(
                findings,
                target=target,
                path=path,
                line=line_number,
                severity="medium",
                category="mcp",
                title="Remote MCP transport",
                evidence=f"{server_name}: url={url}",
                suggestion="Prefer local MCP servers, pin remote hosts, and document authentication boundaries.",
            )
        if _server_timeout(server) is None:
            _finding(
                findings,
                target=target,
                path=path,
                line=line_number,
                severity="low",
                category="mcp",
                title="MCP server missing timeout",
                evidence=f"{server_name}: timeout unset",
                suggestion="Set an explicit MCP startup or request timeout so hung servers fail predictably.",
            )


def _first_npx_package(args: list[object]) -> str | None:
    skip_next = False
    for arg in args:
        if not isinstance(arg, str):
            continue
        if skip_next:
            skip_next = False
            continue
        if arg in {"-y", "--yes", "--quiet"}:
            continue
        if arg in {"--package", "-p"}:
            skip_next = True
            continue
        if arg.startswith("-"):
            continue
        return arg
    return None


def _scan_package_json(findings: list[dict[str, Any]], *, target: Path, path: Path, text: str) -> None:
    if path.name != "package.json":
        return
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return
    if not isinstance(data, dict):
        return
    scripts = data.get("scripts", {})
    if not isinstance(scripts, dict):
        return
    for name, command in scripts.items():
        if not isinstance(name, str) or not isinstance(command, str):
            continue
        line_number = _line_number_for(text, f'"{name}"')
        evidence = f"scripts.{name}: {command}"
        if REMOTE_SHELL_RE.search(command):
            _finding(
                findings,
                target=target,
                path=path,
                line=line_number,
                severity="high",
                category="supply-chain",
                title="Package script pipes remote content into shell",
                evidence=evidence,
                suggestion="Replace curl-to-shell package scripts with checked-in, pinned, and reviewed installer steps.",
            )
        if DESTRUCTIVE_RE.search(command):
            _finding(
                findings,
                target=target,
                path=path,
                line=line_number,
                severity="medium",
                category="supply-chain",
                title="Package script contains destructive command",
                evidence=evidence,
                suggestion="Gate destructive package scripts behind explicit operator approval and document recovery steps.",
            )
        npx_match = UNPINNED_NPX_RE.search(command)
        if npx_match and "@" not in npx_match.group(1):
            _finding(
                findings,
                target=target,
                path=path,
                line=line_number,
                severity="medium",
                category="supply-chain",
                title="Package script uses unpinned npx",
                evidence=evidence,
                suggestion="Pin npx package versions or move execution behind a reviewed lockfile.",
            )
        if ENV_DUMP_RE.search(command):
            _finding(
                findings,
                target=target,
                path=path,
                line=line_number,
                severity="high",
                category="supply-chain",
                title="Package script may leak environment",
                evidence=evidence,
                suggestion="Avoid dumping environment variables in package scripts, especially near network or file redirection.",
            )


def _scan_github_actions(findings: list[dict[str, Any]], *, target: Path, path: Path, text: str) -> None:
    rel = path.relative_to(target)
    if len(rel.parts) < 3 or rel.parts[0] != ".github" or rel.parts[1] != "workflows":
        return
    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("pull_request_target:") or stripped == "- pull_request_target":
            _finding(
                findings,
                target=target,
                path=path,
                line=line_number,
                severity="high",
                category="supply-chain",
                title="GitHub Actions uses pull_request_target",
                evidence=stripped,
                suggestion="Avoid pull_request_target for untrusted code paths or isolate it from checkout and secret access.",
            )
        if stripped.startswith("permissions: write-all"):
            _finding(
                findings,
                target=target,
                path=path,
                line=line_number,
                severity="high",
                category="supply-chain",
                title="GitHub Actions grants write-all permissions",
                evidence=stripped,
                suggestion="Use least-privilege workflow permissions instead of write-all.",
            )
        action_match = UNPINNED_ACTION_RE.search(stripped)
        if action_match:
            _finding(
                findings,
                target=target,
                path=path,
                line=line_number,
                severity="medium",
                category="supply-chain",
                title="GitHub Action missing pinned ref",
                evidence=stripped,
                suggestion="Pin actions to an immutable commit SHA or a reviewed release ref.",
            )
        pinned_match = PINNED_ACTION_RE.search(stripped)
        if pinned_match:
            ref = pinned_match.group(2)
            if ref.lower() in GITHUB_ACTION_FLOATING_REFS or (not ref.startswith("v") and not re.fullmatch(r"[a-fA-F0-9]{40}", ref)):
                _finding(
                    findings,
                    target=target,
                    path=path,
                    line=line_number,
                    severity="medium",
                    category="supply-chain",
                    title="GitHub Action uses floating ref",
                    evidence=stripped,
                    suggestion="Pin GitHub Actions to immutable commit SHAs for release-sensitive workflows.",
                )


def _scan_python_project(findings: list[dict[str, Any]], *, target: Path, path: Path, text: str) -> None:
    if path.name not in {"pyproject.toml", "setup.cfg", "requirements.txt"}:
        return
    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if PYTHON_URL_DEP_RE.search(stripped):
            _finding(
                findings,
                target=target,
                path=path,
                line=line_number,
                severity="medium",
                category="supply-chain",
                title="Python dependency uses URL source",
                evidence=stripped,
                suggestion="Prefer pinned package versions or reviewed immutable commit URLs for Python dependencies.",
            )
        if "setup_requires" in stripped or "dependency_links" in stripped:
            _finding(
                findings,
                target=target,
                path=path,
                line=line_number,
                severity="medium",
                category="supply-chain",
                title="Python project uses legacy install hook",
                evidence=stripped,
                suggestion="Avoid legacy install-time dependency hooks and move dependencies into static project metadata.",
            )


def _iter_scan_files(target: Path) -> list[Path]:
    paths: list[Path] = []
    for path in target.rglob("*"):
        if path.is_dir():
            continue
        rel_parts = path.relative_to(target).parts
        if any(part in SKIP_DIRS for part in rel_parts):
            continue
        if any(rel_parts[: len(prefix)] == prefix for prefix in SKIP_PREFIXES):
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            if path.stat().st_size > 500_000:
                continue
        except OSError:
            continue
        paths.append(path)
    paths.sort()
    return paths


def _surface_for(path: Path, target: Path) -> str:
    rel = path.relative_to(target)
    parts = rel.parts
    if parts and parts[0] == ".brigade":
        return "brigade"
    if parts and parts[0] == ".codex":
        return "codex"
    if parts and parts[0] == ".claude":
        return "claude"
    if "mcp" in path.name.lower():
        return "mcp"
    if parts and parts[0] in {"hooks", "scripts"}:
        return "automation"
    if path.name in {"AGENTS.md", "CLAUDE.md", "SAFETY_RULES.md", "INSTALL_FOR_AGENTS.md"}:
        return "agent-instructions"
    return "repo"


def _confidence_for(path: Path, target: Path) -> str:
    rel = path.relative_to(target)
    parts = rel.parts
    if parts and parts[0] == "src" and "templates" in parts:
        return "template"
    if parts and parts[0] in {".brigade", ".claude", ".codex", "hooks", "scripts"}:
        return "runtime"
    if path.name in {"AGENTS.md", "CLAUDE.md", "SAFETY_RULES.md", "INSTALL_FOR_AGENTS.md"}:
        return "runtime"
    return "repo"


def _fingerprint(*, category: str, title: str, rel_path: Path, line: int, evidence: str) -> str:
    stable = "\n".join([category, title, str(rel_path), str(line), _short(evidence, limit=96)])
    return hashlib.sha256(stable.encode()).hexdigest()[:16]


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
) -> None:
    rel = path.relative_to(target)
    safe_excerpt = _short(evidence)
    fingerprint = _fingerprint(category=category, title=title, rel_path=rel, line=line, evidence=safe_excerpt)
    finding_id = f"security-{fingerprint}"
    findings.append(
        {
            "id": finding_id,
            "fingerprint": fingerprint,
            "rule_id": _rule_id(category, title),
            "severity": severity,
            "category": category,
            "title": title,
            "path": str(rel),
            "line": line,
            "surface": _surface_for(path, target),
            "confidence": _confidence_for(path, target),
            "evidence": safe_excerpt,
            "safe_excerpt": safe_excerpt,
            "suggestion": suggestion,
            "remediation_hint": suggestion,
        }
    )


def _scan_line(findings: list[dict[str, Any]], *, target: Path, path: Path, line_number: int, line: str) -> None:
    secret_match = SECRET_VALUE_RE.search(line)
    if secret_match and not _is_placeholder(secret_match.group(2)):
        _finding(
            findings,
            target=target,
            path=path,
            line=line_number,
            severity="high",
            category="secrets",
            title="Possible hardcoded credential",
            evidence=_redact_secret_evidence(line),
            suggestion="Move the value into local environment or secret storage and commit only a placeholder.",
        )
    if PRIVATE_KEY_RE.search(line) or (ENV_ASSIGNMENT_RE.search(line) and not _is_placeholder(line)):
        _finding(
            findings,
            target=target,
            path=path,
            line=line_number,
            severity="high",
            category="secrets",
            title="Possible sensitive secret material",
            evidence=_redact_secret_evidence(line),
            suggestion="Remove secret material from the repo and rotate the credential if it was real.",
        )
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
            )
    if _surface_for(path, target) in {"agent-instructions", "claude", "codex", "repo"} and PROMPT_INJECTION_RE.search(line):
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
        )


def _path_matches_any(rel_path: str, patterns: tuple[str, ...]) -> bool:
    normalized = rel_path.replace("\\", "/")
    for pattern in patterns:
        clean = pattern.strip().replace("\\", "/").strip("/")
        if not clean:
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
    for path in _iter_scan_files(target):
        if not include_templates and _confidence_for(path, target) == "template":
            continue
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        scanned_files.append(str(path.relative_to(target)))
        for line_number, line in enumerate(text.splitlines(), start=1):
            _scan_line(findings, target=target, path=path, line_number=line_number, line=line)
        _scan_mcp_document(findings, target=target, path=path, text=text)
        _scan_package_json(findings, target=target, path=path, text=text)
        _scan_github_actions(findings, target=target, path=path, text=text)
        _scan_python_project(findings, target=target, path=path, text=text)
    findings = _filter_findings(
        findings,
        enabled_checks=enabled_checks,
        include_paths=include_paths,
        exclude_paths=exclude_paths,
        severity_threshold=severity_threshold,
    )
    suppressed = [finding for finding in findings if finding.get("fingerprint") in suppressions]
    findings = [finding for finding in findings if finding.get("fingerprint") not in suppressions]
    counts: dict[str, int] = {}
    for finding in findings:
        severity = str(finding["severity"])
        counts[severity] = counts.get(severity, 0) + 1
    return {
        "target": str(target),
        "scanned_files": scanned_files,
        "scanned_file_count": len(scanned_files),
        "finding_count": len(findings),
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
    existing_fingerprints = {
        str(item.get("metadata", {}).get("fingerprint"))
        for item in work_cmd._pending_imports(target)
        if isinstance(item, dict)
        and item.get("source") == "security-scan"
        and isinstance(item.get("metadata"), dict)
        and item.get("metadata", {}).get("fingerprint")
    }
    records = []
    skipped: list[dict[str, Any]] = []
    for finding in findings:
        fingerprint = str(finding.get("fingerprint") or "")
        if fingerprint and fingerprint in existing_fingerprints:
            skipped.append(finding)
            continue
        path = finding.get("path")
        line = finding.get("line")
        title = finding.get("title")
        severity = finding.get("severity")
        category = finding.get("category")
        kind = "incident" if SEVERITY_ORDER.get(str(severity), 0) >= SEVERITY_ORDER["high"] else "finding"
        acceptance = [
            f"`brigade security findings` no longer reports {finding.get('id')}.",
            "The mitigation or suppression reason is documented without exposing secret values.",
        ]
        records.append(
            {
                "text": f"Review security finding [{severity}] {category} in {path}:{line}: {title}",
                "kind": kind,
                "source": "security-scan",
                "type": "security",
                "priority": "high" if kind == "incident" else "normal",
                "template": "security-follow-up",
                "acceptance": acceptance,
                "metadata": {
                    "finding_id": finding.get("id"),
                    "rule_id": finding.get("rule_id"),
                    "fingerprint": finding.get("fingerprint"),
                    "source_item_key": f"security-scan:{finding.get('fingerprint')}",
                    "source_fingerprint": work_cmd._stable_hash(
                        {
                            "rule_id": finding.get("rule_id"),
                            "fingerprint": finding.get("fingerprint"),
                            "severity": severity,
                            "path": path,
                            "line": line,
                            "safe_excerpt": finding.get("safe_excerpt") or finding.get("evidence"),
                        }
                    ),
                    "severity": severity,
                    "category": category,
                    "path": path,
                    "line": line,
                    "surface": finding.get("surface"),
                    "confidence": finding.get("confidence"),
                    "safe_detail": finding.get("safe_excerpt") or finding.get("evidence"),
                    "remediation_hint": finding.get("remediation_hint") or finding.get("suggestion"),
                    "local_evidence_path": str(evidence_path) if evidence_path is not None else None,
                },
            }
        )
        if fingerprint:
            existing_fingerprints.add(fingerprint)
    imported, duplicate_records, dismissed_records = work_cmd._append_import_records(target, records)
    skipped.extend(duplicate_records)
    skipped.extend(dismissed_records)
    return imported, skipped


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


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
        lines.extend(
            [
                f"### {finding['id']} - {finding['title']}",
                "",
                f"- fingerprint: `{finding['fingerprint']}`",
                f"- severity: `{finding['severity']}`",
                f"- category: `{finding['category']}`",
                f"- path: `{finding['path']}:{finding['line']}`",
                f"- surface: `{finding['surface']}`",
                f"- confidence: `{finding['confidence']}`",
                f"- evidence: `{finding['evidence']}`",
                f"- suggestion: {finding['suggestion']}",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def write_evidence_bundle(report: dict[str, Any], output_dir: Path) -> Path:
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    report = dict(report)
    report["artifacts"] = str(output_dir)
    (output_dir / "security-report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    (output_dir / "security-report.md").write_text(_render_markdown_report(report))
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


def show_config(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    try:
        payload = _config_payload(target)
    except ValueError as exc:
        print(f"error: invalid security config: {exc}", file=sys.stderr)
        return 2
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["configured"] else 1
    print(f"security config: {payload['config_path']}")
    if not payload["configured"]:
        print("status: missing")
        print(f"next_command: brigade security init --target {target}")
        return 1
    config = payload["config"] or {}
    print("status: configured")
    print(f"policy: {config.get('policy')}")
    print(f"scan_profile: {config.get('scan_profile')}")
    print(f"fail_on: {config.get('fail_on')}")
    print(f"include_templates: {config.get('include_templates')}")
    print(f"enabled_checks: {', '.join(config.get('enabled_checks', []))}")
    print(f"severity_threshold: {config.get('severity_threshold')}")
    print(f"output_path: {config.get('output_path')}")
    print(f"suppressions: {len(config.get('suppressions', []))}")
    return 0


def health(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    checks: list[dict[str, Any]] = []
    config_ok = True
    try:
        loaded = load_config(target)
    except ValueError as exc:
        config_ok = False
        loaded = None
        checks.append({"status": "fail", "name": "security_config", "detail": str(exc)})
    if config_ok:
        if loaded is None:
            checks.append({"status": "warn", "name": "security_config", "detail": f"missing, run `brigade security init --target {target}`"})
        else:
            checks.append({"status": "ok", "name": "security_config", "detail": f"{config_path(target)} (profile={loaded.scan_profile})"})
    bundle = inspect_evidence_bundle(default_artifacts_dir(target))
    if bundle.get("ready"):
        checks.append(
            {
                "status": "ok",
                "name": "security_evidence",
                "detail": f"{bundle.get('path')} findings={bundle.get('finding_count')}",
            }
        )
    else:
        checks.append({"status": "warn", "name": "security_evidence", "detail": str(bundle.get("reason"))})
    try:
        suppression = suppression_health(target)
    except ValueError as exc:
        checks.append({"status": "fail", "name": "security_suppressions", "detail": str(exc)})
    else:
        if suppression["stale"]:
            checks.append({"status": "warn", "name": "security_stale_suppressions", "detail": ", ".join(suppression["stale"][:5])})
        if suppression["missing_reasons"]:
            checks.append({"status": "warn", "name": "security_suppression_reasons", "detail": ", ".join(suppression["missing_reasons"][:5])})
        if not suppression["stale"] and not suppression["missing_reasons"]:
            checks.append({"status": "ok", "name": "security_suppressions", "detail": f"{suppression['suppression_count']} configured"})
    top_finding: dict[str, Any] | None = None
    if bundle.get("ready"):
        try:
            report = _load_report(default_artifacts_dir(target))
            records = [item for item in _report_findings_for_review(target, report) if item.get("status") != "suppressed"]
        except (OSError, ValueError, json.JSONDecodeError):
            records = []
        if records:
            top_finding = records[0]
            checks.append(
                {
                    "status": "warn",
                    "name": "security_open_findings",
                    "detail": f"{len(records)} open finding(s), top={top_finding.get('id')}",
                }
            )
        else:
            checks.append({"status": "ok", "name": "security_open_findings", "detail": "none"})
    issues = [item for item in checks if item["status"] != "ok"]
    return {
        "target": str(target),
        "config_path": str(config_path(target)),
        "valid": not any(item["status"] == "fail" for item in checks),
        "issue_count": len(issues),
        "top_issue": issues[0] if issues else None,
        "top_finding": top_finding,
        "checks": checks,
        "evidence": bundle,
    }


def doctor(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload = health(target)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["valid"] else 1
    print(f"security doctor: {target}")
    for check in payload["checks"]:
        print(f"[{check['status']}] {check['name']}: {check['detail']}")
    top = payload.get("top_finding") if isinstance(payload.get("top_finding"), dict) else None
    if top:
        print(f"top_finding: {top.get('id')} [{top.get('severity')}] {top.get('path')}:{top.get('line')} {top.get('title')}")
        print(f"show_command: brigade security show {top.get('id')}")
    return 0 if payload["valid"] else 1


def scan(
    *,
    target: Path,
    json_output: bool = False,
    policy: str | None = None,
    fail_on: str | None = None,
    include_templates: bool | None = None,
    import_findings: bool = False,
    output_dir: Path | None = None,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    try:
        effective = _effective_policy(
            target,
            policy=policy,
            fail_on=fail_on,
            include_templates=include_templates,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    report = scan_target(
        target,
        include_templates=effective.include_templates,
        suppressions=effective.suppressions,
        enabled_checks=effective.enabled_checks,
        include_paths=effective.include_paths,
        exclude_paths=effective.exclude_paths,
        severity_threshold=effective.severity_threshold,
    )
    report["policy"] = effective.policy
    report["scan_profile"] = effective.scan_profile
    report["fail_on"] = effective.fail_on
    report["include_templates"] = effective.include_templates
    report["enabled_checks"] = list(effective.enabled_checks)
    report["include_paths"] = list(effective.include_paths)
    report["exclude_paths"] = list(effective.exclude_paths)
    report["severity_threshold"] = effective.severity_threshold
    report["config"] = str(effective.config_path)
    report["config_loaded"] = effective.config_loaded
    report["generated_at"] = _utc_iso()
    configured_output_dir = target / effective.output_path
    requested_output_dir = output_dir
    if requested_output_dir is None and (import_findings or effective.config_loaded):
        requested_output_dir = configured_output_dir
    if requested_output_dir is not None:
        artifacts_dir = write_evidence_bundle(report, requested_output_dir)
        report["artifacts"] = str(artifacts_dir)
    imported: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    if import_findings and report["findings"]:
        evidence_path = Path(report["artifacts"]) / "security-report.json" if report.get("artifacts") else None
        imported, skipped = _import_findings(target, report["findings"], evidence_path=evidence_path)
        report["imported_findings"] = len(imported)
        report["skipped_duplicate_imports"] = len(skipped)
        if requested_output_dir is not None:
            artifacts_dir = write_evidence_bundle(report, requested_output_dir)
            report["artifacts"] = str(artifacts_dir)

    if json_output:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"security scan: {target}")
        print(f"policy: {effective.policy}")
        print(f"scan_profile: {effective.scan_profile}")
        print(f"fail_on: {effective.fail_on}")
        print(f"include_templates: {effective.include_templates}")
        print(f"enabled_checks: {', '.join(effective.enabled_checks)}")
        print(f"severity_threshold: {effective.severity_threshold}")
        print(f"scanned_files: {report['scanned_file_count']}")
        print(f"findings: {report['finding_count']}")
        print(f"suppressed: {report['suppressed_count']}")
        for severity, count in report["severity_counts"].items():
            print(f"{severity}: {count}")
        if import_findings:
            print(f"imported_findings: {len(imported)}")
            print(f"skipped_duplicate_imports: {len(skipped)}")
        if requested_output_dir is not None:
            print(f"artifacts: {report['artifacts']}")
        for finding in report["findings"]:
            print(
                f"- [{finding['severity']}] {finding['category']} "
                f"{finding['path']}:{finding['line']} {finding['title']}"
            )
            print(f"  fingerprint: {finding['fingerprint']}")
            print(f"  evidence: {finding['evidence']}")
            print(f"  suggestion: {finding['suggestion']}")

    return 1 if _should_fail(report["findings"], effective.fail_on) else 0


def init(*, target: Path, force: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    try:
        path = write_default_config(target, force=force)
    except FileExistsError as exc:
        print(f"error: security config already exists: {exc.args[0]}", file=sys.stderr)
        return 1
    print(f"security_config: {path}")
    print("policy: personal")
    return 0
