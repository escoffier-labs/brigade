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


def suppress(*, target: Path, fingerprint: str, reason: str, json_output: bool = False) -> int:
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
    if json_output:
        print(
            json.dumps(
                {
                    "config": str(path),
                    "fingerprint": fingerprint,
                    "reason": cleaned_reason,
                    "suppressed_count": len(suppressions),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    print(f"security_config: {path}")
    print(f"suppressed: {fingerprint}")
    print(f"reason: {cleaned_reason}")
    return 0


def unsuppress(*, target: Path, fingerprint: str, json_output: bool = False) -> int:
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
    if json_output:
        print(
            json.dumps(
                {"config": str(path), "fingerprint": fingerprint, "suppressed_count": len(suppressions)},
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    print(f"security_config: {path}")
    print(f"unsuppressed: {fingerprint}")
    return 0


def _file_sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _candidate_file_records(target: Path, effective: EffectivePolicy) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in _iter_scan_files(target, include_paths=effective.include_paths, exclude_paths=effective.exclude_paths):
        classification = _classification_for(path, target)
        if not effective.include_templates and classification.confidence == "template":
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        records.append(
            {
                "path": str(path.relative_to(target)),
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
            }
        )
    for inbox_rel in sorted(set(WRITER_INBOXES.values())):
        inbox = target / inbox_rel
        if not inbox.is_dir():
            continue
        for path in sorted(inbox.glob("*.md")):
            if path.name == "TEMPLATE.md":
                continue
            rel = str(path.relative_to(target))
            if not _scan_path_selected(
                rel,
                include_paths=effective.include_paths,
                exclude_paths=effective.exclude_paths,
            ):
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            records.append({"path": rel, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns})
    return sorted(records, key=lambda item: str(item["path"]))


def _candidate_file_fingerprint(target: Path, effective: EffectivePolicy) -> str:
    records = _candidate_file_records(target, effective)
    payload = json.dumps(records, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _suppression_cache_key(target: Path, effective: EffectivePolicy, config: SecurityConfig) -> dict[str, Any]:
    config_digest = _file_sha256(effective.config_path) if effective.config_path.is_file() else None
    return {
        "version": SUPPRESSION_HEALTH_CACHE_VERSION,
        "config_digest": config_digest,
        "policy": effective.policy,
        "scan_profile": effective.scan_profile,
        "include_templates": effective.include_templates,
        "enabled_checks": list(effective.enabled_checks),
        "include_paths": list(effective.include_paths),
        "exclude_paths": list(effective.exclude_paths),
        "severity_threshold": effective.severity_threshold,
        "suppressions": list(effective.suppressions),
        "suppression_reasons": dict(sorted(config.suppression_reasons.items())),
        "candidate_fingerprint": _candidate_file_fingerprint(target, effective),
    }


def _suppression_health_from_active(config: SecurityConfig, active: set[str]) -> dict[str, Any]:
    stale = [fingerprint for fingerprint in config.suppressions if fingerprint not in active]
    missing_reasons = [
        fingerprint for fingerprint in config.suppressions if not config.suppression_reasons.get(fingerprint)
    ]
    return {
        "suppression_count": len(config.suppressions),
        "missing_reasons": missing_reasons,
        "stale": stale,
    }


def _write_suppression_health_cache(target: Path, effective: EffectivePolicy, health: dict[str, Any]) -> None:
    config = load_config(target)
    if config is None or not effective.config_loaded or not effective.suppressions:
        return
    payload = {
        "schema": "brigade.security.suppression-health-cache.v1",
        "key": _suppression_cache_key(target, effective, config),
        "health": health,
    }
    _write_json(suppression_health_cache_path(target), payload)


def _write_suppression_health_cache_from_report(
    target: Path, effective: EffectivePolicy, report: dict[str, Any]
) -> None:
    config = load_config(target)
    if config is None or not config.suppressions:
        return
    active = {
        str(item.get("fingerprint"))
        for item in list(report.get("findings") or []) + list(report.get("suppressed_findings") or [])
        if item.get("fingerprint")
    }
    _write_suppression_health_cache(target, effective, _suppression_health_from_active(config, active))


def _suppression_full_scan_next_command(target: Path) -> str:
    return f"brigade security scan --target {target} --output-dir {default_artifacts_dir(target)}"


def suppression_health_cache(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    config = load_config(target)
    if config is None or not config.suppressions:
        return {
            "status": "ok",
            "health": {"suppression_count": 0, "missing_reasons": [], "stale": []},
            "detail": "no suppressions configured",
            "next_command": None,
        }
    effective = _effective_policy(target, policy=None, fail_on=None, include_templates=None)
    path = suppression_health_cache_path(target)
    next_command = _suppression_full_scan_next_command(target)
    if not path.is_file():
        return {
            "status": "missing",
            "health": None,
            "detail": f"cache missing; run `{next_command}`",
            "next_command": next_command,
        }
    try:
        payload = _read_json(path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {
            "status": "invalid",
            "health": None,
            "detail": f"cache invalid: {exc}; run `{next_command}`",
            "next_command": next_command,
        }
    if not isinstance(payload, dict):
        return {
            "status": "invalid",
            "health": None,
            "detail": f"cache invalid: expected object; run `{next_command}`",
            "next_command": next_command,
        }
    cache_key = payload.get("key")
    if not isinstance(cache_key, dict):
        return {
            "status": "invalid",
            "health": None,
            "detail": f"cache invalid: missing key; run `{next_command}`",
            "next_command": next_command,
        }
    health = payload.get("health")
    if not isinstance(health, dict):
        return {
            "status": "invalid",
            "health": None,
            "detail": f"cache invalid: missing health; run `{next_command}`",
            "next_command": next_command,
        }
    expected_key = _suppression_cache_key(target, effective, config)
    if payload.get("key") != expected_key:
        return {
            "status": "stale",
            "health": None,
            "detail": f"cache stale; run `{next_command}`",
            "next_command": next_command,
        }
    return {"status": "ok", "health": health, "detail": "cache fresh", "next_command": None}


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
    health = _suppression_health_from_active(config, active)
    _write_suppression_health_cache(target, effective, health)
    return health
