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
    if _contains_private_key_material(line):
        return PRIVATE_KEY_RE.sub("-----BEGIN REDACTED PRIVATE KEY-----", line)

    def redact_secret(match: re.Match[str]) -> str:
        return match.group(0).replace(match.group(2), "[REDACTED]")

    redacted = SECRET_VALUE_RE.sub(redact_secret, line)
    redacted = PLAINTEXT_PASSWORD_RE.sub(redact_secret, redacted)

    def redact_env(match: re.Match[str]) -> str:
        text = match.group(0)
        if "=" not in text:
            return "[REDACTED]"
        key, _ = text.split("=", 1)
        return f"{key}=[REDACTED]"

    return ENV_ASSIGNMENT_RE.sub(redact_env, redacted)


def _contains_private_key_material(line: str) -> bool:
    return bool(PRIVATE_KEY_RE.search(line) and "REDACTED PRIVATE KEY" not in line)


def _template_relpath(target: Path, path: Path) -> str:
    try:
        return str(path.relative_to(target))
    except ValueError:
        return path.name


def _template_audit_finding(
    *, target: Path, path: Path, line_number: int, category: str, title: str, line: str
) -> dict[str, Any]:
    rel = _template_relpath(target, path)
    evidence = _redact_secret_evidence(line.strip())
    if len(evidence) > 220:
        evidence = evidence[:217].rstrip() + "..."
    payload = {
        "id": f"template-privacy-{hashlib.sha256(f'{rel}:{line_number}:{category}:{evidence}'.encode()).hexdigest()[:12]}",
        "path": rel,
        "line": line_number,
        "category": category,
        "title": title,
        "severity": "high" if category == "secret" else "medium",
        "safe_excerpt": evidence,
        "surface": _surface_for(path, target),
        "confidence": "template" if "templates" in path.parts else "docs",
    }
    payload["fingerprint"] = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]
    return payload


def template_privacy_payload(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    findings: list[dict[str, Any]] = []
    scanned_files: list[str] = []
    roots = [target / rel for rel in TEMPLATE_AUDIT_ROOTS]
    for root in roots:
        if not root.exists():
            continue
        paths = [root] if root.is_file() else sorted(path for path in root.rglob("*") if path.is_file())
        for path in paths:
            if path.suffix not in TEXT_SUFFIXES:
                continue
            try:
                text = path.read_text(errors="replace")
            except OSError:
                continue
            rel = _template_relpath(target, path)
            scanned_files.append(rel)
            for line_number, line in enumerate(text.splitlines(), start=1):
                if TEMPLATE_ALLOWLIST_RE.search(line):
                    continue
                if (
                    SECRET_VALUE_RE.search(line)
                    or ENV_ASSIGNMENT_RE.search(line)
                    or _contains_private_key_material(line)
                ):
                    findings.append(
                        _template_audit_finding(
                            target=target,
                            path=path,
                            line_number=line_number,
                            category="secret",
                            title="Template contains secret-looking value",
                            line=line,
                        )
                    )
                elif TEMPLATE_PRIVATE_PATH_RE.search(line):
                    findings.append(
                        _template_audit_finding(
                            target=target,
                            path=path,
                            line_number=line_number,
                            category="private-path",
                            title="Template contains host-private path",
                            line=line,
                        )
                    )
                elif TEMPLATE_PRIVATE_URL_RE.search(line):
                    findings.append(
                        _template_audit_finding(
                            target=target,
                            path=path,
                            line_number=line_number,
                            category="private-url",
                            title="Template contains private-looking URL",
                            line=line,
                        )
                    )
    findings.sort(key=lambda item: (str(item.get("path") or ""), int(item.get("line") or 0), str(item.get("id") or "")))
    return {
        "target": str(target),
        "roots": TEMPLATE_AUDIT_ROOTS,
        "scanned_files": scanned_files,
        "scanned_file_count": len(scanned_files),
        "finding_count": len(findings),
        "findings": findings,
        "status": "warn" if findings else "ok",
        "top_finding": findings[0] if findings else None,
        "allowlisted_examples": [
            "example.com",
            "example.invalid",
            "loopback-host",
            "loopback-ipv4",
            "wildcard-ipv4",
            "<placeholder>",
            "{{placeholder}}",
            "$ENV_LABEL",
        ],
    }


def harness_wiring_payload(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    findings: list[dict[str, Any]] = []
    scanned_files: list[str] = []
    for path in _iter_scan_files(target):
        if not _is_harness_wiring_document(path, target):
            continue
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        scanned_files.append(str(path.relative_to(target)))
        _scan_harness_wiring_document(
            findings,
            target=target,
            path=path,
            text=text,
            classification=_classification_for(path, target),
        )
    findings = _filter_findings(
        findings,
        enabled_checks=SECURITY_CHECKS,
        include_paths=(),
        exclude_paths=(),
        severity_threshold="low",
    )
    findings.sort(key=lambda item: (str(item.get("path") or ""), int(item.get("line") or 0), str(item.get("id") or "")))
    return {
        "target": str(target),
        "scanned_files": scanned_files,
        "scanned_file_count": len(scanned_files),
        "finding_count": len(findings),
        "findings": findings,
        "status": "warn" if findings else "ok",
        "top_finding": findings[0] if findings else None,
    }


def template_audit(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    payload = template_privacy_payload(target)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"security template audit: {target}")
    print(f"scanned_files: {payload['scanned_file_count']}")
    print(f"findings: {payload['finding_count']}")
    for finding in payload["findings"]:
        print(f"- {finding['path']}:{finding['line']} {finding['title']}")
    return 0
