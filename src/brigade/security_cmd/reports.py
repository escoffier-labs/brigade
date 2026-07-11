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
        for option in finding.get("response_options") or []:
            print(f"  response_option: {option}")
    if enrichment is not None:
        print("enrichment:")
        print(f"- provider: {enrichment.get('provider')}")
        print(f"- indicators: {enrichment.get('indicator_count')}")
        print(f"- hits: {enrichment.get('hit_count')}")
    return 0


def findings(*, target: Path, output_dir: Path | None = None, json_output: bool = False) -> int:
    return review(target=target, output_dir=output_dir, json_output=json_output)


def _diff_finding_key(record: dict[str, Any]) -> str:
    fingerprint = str(record.get("fingerprint") or "")
    if fingerprint:
        return fingerprint
    # Fall back to a stable composite when a report predates fingerprints, so
    # unkeyed findings are not all collapsed into a single bucket.
    return "|".join(str(record.get(field) or "") for field in ("category", "path", "line", "title"))


def diff(
    *,
    target: Path,
    base_dir: Path,
    against_dir: Path | None = None,
    json_output: bool = False,
) -> int:
    """Compare two security reports: what is new, resolved, or persisting.

    Findings are matched by fingerprint (the scan's stable per-finding hash), so
    this answers "did my change add or fix a finding" without eyeballing two
    reports. Returns nonzero when there are new findings.
    """
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    base_dir = base_dir.expanduser().resolve()
    against_dir = against_dir.expanduser().resolve() if against_dir is not None else default_artifacts_dir(target)
    try:
        base_report = _load_report(base_dir)
        against_report = _load_report(against_dir)
    except FileNotFoundError as exc:
        print(f"error: security report not found: {exc}", file=sys.stderr)
        return 2
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"error: invalid security report: {exc}", file=sys.stderr)
        return 2

    base_records = _report_findings_for_review(target, base_report)
    against_records = _report_findings_for_review(target, against_report)
    base_keys = {_diff_finding_key(record) for record in base_records}
    against_keys = {_diff_finding_key(record) for record in against_records}

    new = [record for record in against_records if _diff_finding_key(record) not in base_keys]
    resolved = [record for record in base_records if _diff_finding_key(record) not in against_keys]
    persisting = [record for record in against_records if _diff_finding_key(record) in base_keys]

    payload = {
        "base": str(base_dir),
        "against": str(against_dir),
        "base_generated_at": base_report.get("generated_at"),
        "against_generated_at": against_report.get("generated_at"),
        "new": new,
        "resolved": resolved,
        "persisting": persisting,
        "new_count": len(new),
        "resolved_count": len(resolved),
        "persisting_count": len(persisting),
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 1 if new else 0

    print(f"security diff: {base_dir} -> {against_dir}")
    print(f"new: {len(new)}  resolved: {len(resolved)}  persisting: {len(persisting)}")
    for label, records in (("new", new), ("resolved", resolved), ("persisting", persisting)):
        if not records:
            continue
        print(f"{label}:")
        for finding in records:
            print(
                f"- [{finding.get('severity')}] {finding.get('category')} "
                f"{finding.get('path')}:{finding.get('line')} {finding.get('title')} "
                f"({finding.get('fingerprint')})"
            )
    return 1 if new else 0


def sarif(
    *, target: Path, output_dir: Path | None = None, output_path: Path | None = None, json_output: bool = False
) -> int:
    target = target.expanduser().resolve()
    artifacts_dir = output_dir.expanduser().resolve() if output_dir is not None else default_artifacts_dir(target)
    try:
        report = _load_report(artifacts_dir)
    except FileNotFoundError as exc:
        print(f"error: security report not found: {exc}", file=sys.stderr)
        return 2
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"error: invalid security report: {exc}", file=sys.stderr)
        return 2
    payload = _sarif_report(report)
    destination = (
        output_path.expanduser().resolve() if output_path is not None else artifacts_dir / "security-report.sarif"
    )
    _write_json(destination, payload)
    output = {
        "target": str(target),
        "path": str(destination),
        "result_count": len(payload["runs"][0]["results"]),
        "sarif": payload,
    }
    if json_output:
        print(json.dumps(output, indent=2, sort_keys=True))
        return 0
    print(f"security sarif: {destination}")
    print(f"results: {output['result_count']}")
    return 0


def _resolve_finding_record(
    target: Path, identifier: str, output_dir: Path | None = None
) -> tuple[dict[str, Any] | None, str | None]:
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
    for option in finding.get("response_options") or []:
        print(f"response_option: {option}")
    if finding.get("reason"):
        print(f"reason: {finding['reason']}")
    return 0
