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

from . import models as _family_base

globals().update({name: value for name, value in vars(_family_base).items() if not name.startswith("__")})


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
        raise ValueError("policy must be one of: ci, personal, public-repo, strict")
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
        raise ValueError("policy must be one of: ci, personal, public-repo, strict")
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
                "# policy options: personal, public-repo, ci, strict",
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
    from ..selection import Selection

    loaded = load_config(target)
    if loaded is not None:
        return loaded.selection
    return Selection(depth="repo", harnesses=[], owner="this-repo", includes=[])


def fix(*, target: Path, dry_run: bool = False) -> int:
    from ..install import apply_gitignore

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
    config_ignored = localio.check_git_ignored(target, config_path(target))
    artifacts_ignored = localio.check_git_ignored(target, artifacts_root)
    print(f"security_artifacts_dir: {artifacts_root}")
    print(f"gitignore: {result}")
    print(f"security_config_ignored: {config_ignored}")
    print(f"security_artifacts_ignored: {artifacts_ignored}")
    return 0
