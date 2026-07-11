"""Local release readiness receipts."""
# ruff: noqa: E402,F401,F403,F811,F821

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from .. import (
    context_cmd,
    handoff_cmd,
    learn_cmd,
    memory_cmd,
    phases_cmd,
    projects_cmd,
    repos_cmd,
    reportstore,
    research_cmd,
    roadmap_cmd,
    scrub,
    security_cmd,
    tools_cmd,
    work_cmd,
)
from ..selection import KNOWN_HARNESSES
from ..localio import (
    read_json_dict as _read_json,
    read_jsonl_dicts as _read_jsonl,
    utc_now as _now,
    write_json as _write_json,
)

from . import paths as _family_base

globals().update({name: value for name, value in vars(_family_base).items() if not name.startswith("__")})


def _schema_manifest_schemas() -> list[dict[str, Any]]:
    return [
        {
            "id": "release-readiness-receipt",
            "file": ".brigade/release/runs/<run-id>/receipt.json",
            "description": "Local release readiness receipt.",
            "required_fields": [
                _field("run_id", "string", "Unique local release run id."),
                _field("target", "string", "Inspected repo or workspace."),
                _field("status", "string", "ready or blocked."),
                _field("ready", "boolean", "True when no blockers were found."),
                _field("blockers", "array<string>", "Blocking readiness findings."),
                _field("warnings", "array<string>", "Non-blocking readiness findings."),
                _field("checks", "array<object>", "Local check summaries."),
                _field("evidence", "object", "Collected subsystem evidence."),
            ],
            "optional_fields": [
                _field("started_at", "string", "Start timestamp."),
                _field("completed_at", "string", "Completion timestamp."),
                _field("path", "string", "Local receipt directory."),
            ],
        },
        {
            "id": "release-candidate-evidence",
            "file": ".brigade/release/candidates/<candidate-id>/EVIDENCE.json",
            "description": "Local release candidate evidence packet.",
            "required_fields": [
                _field("candidate_id", "string", "Unique local candidate id."),
                _field("release_readiness", "object", "Readiness summary copied into the candidate."),
                _field("release_readiness_receipt", "object", "Full readiness receipt or inline readiness payload."),
                _field("git", "object", "Captured git state."),
                _field("changed_files", "array<string>", "Changed files for review."),
                _field("blockers", "array<string>", "Candidate blockers."),
                _field("warnings", "array<string>", "Candidate warnings."),
                _field("bundle_files", "array<string>", "Files written in the candidate bundle."),
            ],
            "optional_fields": [
                _field("work_closeout", "object", "Latest work closeout receipt."),
                _field("verification", "object", "Latest verification receipt."),
                _field("code_review", "object", "Code review closeout summary."),
                _field("security", "object", "Security health and closeout summary."),
                _field("ci_platform", "object", "Local GitHub Actions platform deprecation summary."),
                _field("install_smoke", "object", "Local install smoke matrix receipt summary."),
                _field("handoff_drafts", "object", "Handoff draft and ingest summary."),
            ],
        },
        {
            "id": "fleet-release-train-evidence",
            "file": ".brigade/repos/releases/<train-id>/FLEET_RELEASE_EVIDENCE.json",
            "description": "Local repo-fleet release train evidence packet.",
            "required_fields": [
                _field("train_id", "string", "Unique local train id."),
                _field("repos", "array<object>", "Safe per-repo release states."),
                _field("classifications", "object", "Per-repo readiness classes."),
                _field("blocker_count", "integer", "Total blocker count."),
                _field("warning_count", "integer", "Total warning count."),
            ],
            "optional_fields": [
                _field("closeout", "object", "Reviewed, deferred, superseded, or archived closeout state."),
                _field("manual_publish_plan", "object", "Manual-only publish checklist references."),
            ],
        },
        {
            "id": "fleet-release-waiver",
            "file": ".brigade/repos/releases/waivers.jsonl",
            "description": "Local waiver record for fleet release ready gates.",
            "required_fields": [
                _field("waiver_id", "string", "Stable waiver id."),
                _field("train_id", "string", "Release train id."),
                _field("scope", "string", "Waived blocker scope."),
                _field("status", "string", "active or revoked."),
                _field("reason", "string", "Reviewed reason."),
            ],
            "optional_fields": [
                _field("repo_id", "string", "Optional safe repo id."),
                _field("expires_at", "string", "Optional expiry timestamp."),
                _field("owner_label", "string", "Optional safe review owner label."),
                _field("source_fingerprint", "string", "Source fingerprint at waiver time."),
            ],
        },
        {
            "id": "fleet-release-manual-evidence",
            "file": ".brigade/repos/releases/evidence.jsonl",
            "description": "Local manual evidence record for fleet release steps.",
            "required_fields": [
                _field("evidence_id", "string", "Stable local evidence id."),
                _field("repo_id", "string", "Safe repo id."),
                _field("train_id", "string", "Release train id."),
                _field("step", "string", "Manual release step."),
                _field("status", "string", "completed, skipped, deferred, blocked, or missing."),
                _field("safe_summary", "string", "Private-safe summary."),
            ],
            "optional_fields": [
                _field("source_fingerprint", "string", "Fingerprint for reconciliation."),
                _field("receipt_label", "string", "Local receipt label."),
            ],
        },
        {
            "id": "release-dogfood-health",
            "file": "computed",
            "description": "Local self-dogfood release health summary used by daily hardening and release evidence.",
            "required_fields": [
                _field("latest_readiness", "object|null", "Latest local release readiness reference."),
                _field("latest_candidate", "object|null", "Latest local release candidate reference."),
                _field("latest_daily_run", "object|null", "Latest local daily run reference."),
                _field("checks", "array<object>", "Dogfood release checks."),
                _field("issue_count", "integer", "Number of dogfood health issues."),
            ],
        },
    ]


def _latest_fleet_release_train(target: Path) -> dict[str, Any] | None:
    try:
        return repos_cmd.latest_release_train(target)
    except Exception:
        return None


def _schema_manifest(target: Path) -> dict[str, Any]:
    latest_readiness = _latest_release_receipt(target)
    latest_candidate = _latest_candidate(target)
    latest_train = _latest_fleet_release_train(target)
    waivers_path = target / ".brigade" / "repos" / "releases" / "waivers.jsonl"
    manual_evidence_path = target / ".brigade" / "repos" / "releases" / "evidence.jsonl"
    checks: list[dict[str, Any]] = []
    checks.append(
        {
            "name": "release_readiness_latest",
            "status": OK if latest_readiness else WARN,
            "detail": str(latest_readiness.get("path")) if latest_readiness else "no release readiness receipt found",
        }
    )
    checks.append(
        {
            "name": "release_candidate_latest",
            "status": OK if latest_candidate else WARN,
            "detail": str(latest_candidate.get("path")) if latest_candidate else "no release candidate evidence found",
        }
    )
    candidate_health = _candidate_health(target)
    checks.extend(candidate_health.get("checks") if isinstance(candidate_health.get("checks"), list) else [])
    checks.append(
        {
            "name": "fleet_release_train_latest",
            "status": OK if latest_train else WARN,
            "detail": str(latest_train.get("path")) if latest_train else "no fleet release train evidence found",
        }
    )
    checks.append(
        {
            "name": "fleet_release_waivers",
            "status": OK,
            "detail": str(waivers_path) if waivers_path.exists() else "no waiver records found",
        }
    )
    checks.append(
        {
            "name": "fleet_release_manual_evidence",
            "status": OK,
            "detail": str(manual_evidence_path)
            if manual_evidence_path.exists()
            else "no manual evidence records found",
        }
    )
    return {
        "target": str(target),
        "manifest_version": SCHEMA_MANIFEST_VERSION,
        "generated_at": _now().isoformat(),
        "schema_count": len(_schema_manifest_schemas()),
        "schemas": _schema_manifest_schemas(),
        "latest": {
            "release_readiness": _receipt_ref(latest_readiness, "run_id"),
            "release_candidate": _receipt_ref(latest_candidate, "candidate_id"),
            "fleet_release_train": _receipt_ref(latest_train, "train_id"),
        },
        "checks": checks,
        "issue_count": len([check for check in checks if check.get("status") != OK]),
    }
