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


def health(target: Path, *, suppression_cache_only: bool = False) -> dict[str, Any]:
    target = target.expanduser().resolve()
    checks: list[dict[str, Any]] = []
    closeouts = _read_closeouts(target)
    latest_closeout = closeouts[0] if closeouts else None
    accepted_fingerprints = (
        {
            str(fingerprint)
            for fingerprint in latest_closeout.get("source_fingerprints", [])
            if isinstance(fingerprint, str) and fingerprint
        }
        if isinstance(latest_closeout, dict)
        and latest_closeout.get("status") == "accepted-risk"
        and isinstance(latest_closeout.get("policy_pack"), dict)
        and latest_closeout["policy_pack"].get("accepted_risk") is True
        else set()
    )
    config_ok = True
    try:
        loaded = load_config(target)
    except ValueError as exc:
        config_ok = False
        loaded = None
        checks.append({"status": "fail", "name": "security_config", "detail": str(exc)})
    if config_ok:
        if loaded is None:
            checks.append(
                {
                    "status": "warn",
                    "name": "security_config",
                    "detail": f"missing, run `brigade security init --target {target}`",
                }
            )
        else:
            checks.append(
                {
                    "status": "ok",
                    "name": "security_config",
                    "detail": f"{config_path(target)} (profile={loaded.scan_profile})",
                }
            )
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
    template_audit_payload = template_privacy_payload(target)
    if template_audit_payload["finding_count"]:
        top_template = (
            template_audit_payload["top_finding"] if isinstance(template_audit_payload.get("top_finding"), dict) else {}
        )
        checks.append(
            {
                "status": "warn",
                "name": "security_template_privacy",
                "detail": f"{template_audit_payload['finding_count']} public template privacy finding(s), top={top_template.get('path')}:{top_template.get('line')}",
            }
        )
    else:
        checks.append(
            {
                "status": "ok",
                "name": "security_template_privacy",
                "detail": f"{template_audit_payload['scanned_file_count']} public file(s) checked",
            }
        )
    raw_harness_wiring = harness_wiring_payload(target)
    raw_harness_findings = [item for item in raw_harness_wiring.get("findings", []) if isinstance(item, dict)]
    include_templates = (
        bool(
            loaded.include_templates
            if loaded.include_templates is not None
            else POLICIES[loaded.policy]["include_templates"]
        )
        if loaded is not None
        else False
    )
    eligible_harness_findings = [
        item for item in raw_harness_findings if include_templates or item.get("confidence") != "template"
    ]
    active_harness_findings = [
        item for item in eligible_harness_findings if str(item.get("fingerprint") or "") not in accepted_fingerprints
    ]
    quieted_harness_findings = [
        item for item in eligible_harness_findings if str(item.get("fingerprint") or "") in accepted_fingerprints
    ]
    harness_wiring = {
        **raw_harness_wiring,
        "active_findings": active_harness_findings,
        "active_finding_count": len(active_harness_findings),
        "active_top_finding": active_harness_findings[0] if active_harness_findings else None,
        "quieted_findings": quieted_harness_findings,
        "quieted_finding_count": len(quieted_harness_findings),
        "ignored_template_finding_count": len(raw_harness_findings) - len(eligible_harness_findings),
    }
    if active_harness_findings:
        top_harness = active_harness_findings[0]
        checks.append(
            {
                "status": "warn",
                "name": "security_harness_wiring",
                "detail": f"{len(active_harness_findings)} harness wiring finding(s), top={top_harness.get('path')}:{top_harness.get('line')}",
            }
        )
    else:
        checks.append(
            {
                "status": "ok",
                "name": "security_harness_wiring",
                "detail": (
                    f"{harness_wiring['scanned_file_count']} harness wiring file(s) checked; "
                    f"quieted={len(quieted_harness_findings)} "
                    f"templates-excluded={harness_wiring['ignored_template_finding_count']}"
                ),
            }
        )
    suppression_cache: dict[str, Any] | None = None
    try:
        if suppression_cache_only:
            suppression_cache = suppression_health_cache(target)
            if suppression_cache["status"] == "ok":
                suppression = suppression_cache["health"]
            else:
                checks.append(
                    {
                        "status": "warn",
                        "name": "security_suppressions_cache",
                        "detail": suppression_cache["detail"],
                        "next_command": suppression_cache["next_command"],
                    }
                )
                suppression = None
        else:
            suppression = suppression_health(target)
    except ValueError as exc:
        checks.append({"status": "fail", "name": "security_suppressions", "detail": str(exc)})
    else:
        if suppression is not None and suppression["stale"]:
            checks.append(
                {"status": "warn", "name": "security_stale_suppressions", "detail": ", ".join(suppression["stale"][:5])}
            )
        if suppression is not None and suppression["missing_reasons"]:
            checks.append(
                {
                    "status": "warn",
                    "name": "security_suppression_reasons",
                    "detail": ", ".join(suppression["missing_reasons"][:5]),
                }
            )
        if suppression is not None and not suppression["stale"] and not suppression["missing_reasons"]:
            checks.append(
                {
                    "status": "ok",
                    "name": "security_suppressions",
                    "detail": f"{suppression['suppression_count']} configured",
                }
            )
    top_finding: dict[str, Any] | None = None
    raw_open_findings: list[dict[str, Any]] = []
    quieted_findings: list[dict[str, Any]] = []
    if bundle.get("ready"):
        try:
            report = _load_report(default_artifacts_dir(target))
            raw_open_findings = [
                item for item in _report_findings_for_review(target, report) if item.get("status") != "suppressed"
            ]
        except (OSError, ValueError, json.JSONDecodeError):
            raw_open_findings = []
        quieted_findings = [
            item for item in raw_open_findings if str(item.get("fingerprint") or "") in accepted_fingerprints
        ]
        records = [
            item for item in raw_open_findings if str(item.get("fingerprint") or "") not in accepted_fingerprints
        ]
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
        "template_privacy": template_audit_payload,
        "harness_wiring": harness_wiring,
        "suppression_cache": suppression_cache,
        "raw_open_finding_count": len(raw_open_findings),
        "quieted_findings": quieted_findings,
        "quieted_finding_count": len(quieted_findings),
        "changed_fingerprint_count": len(raw_open_findings) - len(quieted_findings) if accepted_fingerprints else 0,
        "latest_closeout": latest_closeout,
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
        print(
            f"top_finding: {top.get('id')} [{top.get('severity')}] {top.get('path')}:{top.get('line')} {top.get('title')}"
        )
        print(f"show_command: brigade security show {top.get('id')}")
    return 0 if payload["valid"] else 1


def closeout(
    *,
    target: Path,
    output_dir: Path | None = None,
    reason: str | None = None,
    accept_risk: bool = False,
    json_output: bool = False,
) -> int:
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
    opened = [item for item in records if item.get("status") != "suppressed"]
    suppressed = [item for item in records if item.get("status") == "suppressed"]
    created_at = _utc_iso()
    closeout_id = f"{created_at.replace(':', '').replace('.', '').replace('Z', '')}-security-closeout"
    status = "accepted-risk" if accept_risk and opened else "reviewed"
    policy_name = str(report.get("policy") or "personal")
    policy = POLICIES.get(policy_name, POLICIES["personal"])
    fail_on = str(report.get("fail_on") or policy["fail_on"])
    blocker_count = sum(
        1
        for item in opened
        if SEVERITY_ORDER.get(str(item.get("severity")), 0) >= SEVERITY_ORDER.get(fail_on, SEVERITY_ORDER["critical"])
    )
    warning_count = max(0, len(opened) - blocker_count)
    finding_records = [
        {
            "id": item.get("id"),
            "fingerprint": item.get("fingerprint"),
            "status": item.get("status"),
            "severity": item.get("severity"),
            "category": item.get("category"),
            "path": item.get("path"),
            "line": item.get("line"),
            "reason": item.get("reason"),
        }
        for item in records
    ]
    payload = {
        "target": str(target),
        "closeout_id": closeout_id,
        "created_at": created_at,
        "status": status,
        "reason": reason or ("open findings accepted as local risk" if accept_risk else "security findings reviewed"),
        "artifacts": str(artifacts_dir),
        "generated_at": report.get("generated_at"),
        "policy": report.get("policy"),
        "policy_pack": {
            "name": policy_name,
            "fail_on": fail_on,
            "include_templates": report.get("include_templates"),
            "blocker_count": blocker_count,
            "warning_count": warning_count,
            "accepted_risk": bool(accept_risk and opened),
        },
        "finding_count": len(records),
        "open_count": len(opened),
        "suppressed_count": len(suppressed),
        "source_fingerprints": [str(item.get("fingerprint")) for item in opened if item.get("fingerprint")],
        "findings": finding_records,
        "path": str(_closeouts_root(target) / closeout_id / "closeout.json"),
    }
    _write_json(Path(payload["path"]), payload)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"security closeout: {closeout_id}")
    print(f"status: {status}")
    print(f"findings: {len(records)}")
    print(f"open: {len(opened)}")
    print(f"suppressed: {len(suppressed)}")
    print(f"path: {payload['path']}")
    return 0


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
    _write_suppression_health_cache_from_report(target, effective, report)
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
            for option in finding.get("response_options") or []:
                print(f"  response_option: {option}")

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
