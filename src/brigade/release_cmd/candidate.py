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


def _commit_subjects(target: Path, base_ref: str | None) -> list[str]:
    args = ["log", "--format=%s"]
    if base_ref:
        args.append(f"{base_ref}..HEAD")
    else:
        args.extend(["-n", "20"])
    result = _git(target, *args)
    if result.returncode != 0:
        return []
    return [_release_safe_text(line.strip()) for line in result.stdout.splitlines() if line.strip()]


def _changelog_unreleased(path: Path) -> list[str]:
    changelog = path / "CHANGELOG.md"
    if not changelog.is_file():
        return []
    lines = changelog.read_text().splitlines()
    capture = False
    items: list[str] = []
    for line in lines:
        if line.startswith("## [Unreleased]"):
            capture = True
            continue
        if capture and line.startswith("## "):
            break
        if capture and line.strip().startswith("- "):
            items.append(_release_safe_text(line.strip()[2:]))
        if len(items) >= 20:
            break
    return items


def _latest_release_or_payload(target: Path, *, base_ref: str | None) -> dict[str, Any]:
    latest = _latest_release_receipt(target)
    if latest is not None:
        return latest
    payload = _payload(target, base_ref=base_ref, run_checks=True)
    return {
        **payload,
        "run_id": "inline-readiness",
        "path": None,
        "started_at": _now().isoformat(),
        "completed_at": _now().isoformat(),
    }


def _candidate_payload(target: Path, *, base_ref: str | None) -> dict[str, Any]:
    readiness = _latest_release_or_payload(target, base_ref=base_ref)
    evidence = readiness.get("evidence") if isinstance(readiness.get("evidence"), dict) else {}
    git = evidence.get("git") if isinstance(evidence.get("git"), dict) else _git_state(target)
    changed_files = evidence.get("docs", {}).get("changed_files") if isinstance(evidence.get("docs"), dict) else None
    if not isinstance(changed_files, list):
        changed_files = _changed_files(target, base_ref)
    return {
        "target": str(target),
        "base_ref": base_ref,
        "release_readiness": {
            "run_id": readiness.get("run_id"),
            "status": readiness.get("status"),
            "ready": readiness.get("ready"),
            "path": readiness.get("path"),
            "blockers": readiness.get("blockers") if isinstance(readiness.get("blockers"), list) else [],
            "warnings": readiness.get("warnings") if isinstance(readiness.get("warnings"), list) else [],
            "checks": readiness.get("checks") if isinstance(readiness.get("checks"), list) else [],
        },
        "release_readiness_receipt": readiness,
        "work_closeout": evidence.get("latest_work_closeout"),
        "verification": evidence.get("latest_verification"),
        "code_review": {
            "latest_closeout": evidence.get("latest_review_closeout"),
            "health": evidence.get("code_review"),
        },
        "scanner_sweep": evidence.get("scanner_sweep"),
        "security": evidence.get("security"),
        "ci_platform": evidence.get("ci_platform"),
        "install_smoke": evidence.get("install_smoke"),
        "security_closeout": evidence.get("security_closeout"),
        "handoff_drafts": evidence.get("handoff_drafts"),
        "backup": evidence.get("backup"),
        "tool_catalog": evidence.get("tool_catalog"),
        "task_acceptance": evidence.get("task_acceptance"),
        "inbox_quality": evidence.get("inbox_quality"),
        "memory_care": evidence.get("memory_care"),
        "context": evidence.get("context"),
        "projects": evidence.get("projects"),
        "learning": evidence.get("learning"),
        "operator_report": evidence.get("operator_report"),
        "operator_center_contract": evidence.get("operator_center_contract"),
        "daily_driver": evidence.get("daily_driver"),
        "daily_hardening": evidence.get("daily_hardening"),
        "release_dogfood": evidence.get("release_dogfood"),
        "repo_fleet": evidence.get("repo_fleet"),
        "repo_fleet_daily_use": evidence.get("repo_fleet_daily_use"),
        "roadmap": evidence.get("roadmap"),
        "phase_ledger": evidence.get("phase_ledger"),
        "git": git,
        "changed_files": changed_files,
        "docs_touch_status": _candidate_docs_touch([str(item) for item in changed_files]),
        "content_guard": {
            str(check.get("name")): check
            for check in readiness.get("checks", [])
            if isinstance(check, dict) and str(check.get("name", "")).startswith("content_guard")
        },
        "release_notes_inputs": {
            "changelog_unreleased": _changelog_unreleased(target),
            "commit_subjects": _commit_subjects(target, base_ref),
            "touched_docs": [path for path in changed_files if str(path).startswith("docs/")],
        },
        "command_contract": _command_contract_snapshot(target),
        "blockers": readiness.get("blockers") if isinstance(readiness.get("blockers"), list) else [],
        "warnings": readiness.get("warnings") if isinstance(readiness.get("warnings"), list) else [],
        "suggested_next_commands": [
            "brigade release doctor",
            "brigade work verify run",
            "brigade work closeout latest",
            "brigade release candidate build",
        ],
    }


def _command_contract_snapshot(target: Path) -> dict[str, Any]:
    payload = roadmap_cmd.command_contract_payload(target)
    snapshot = {
        "cli_command_count": len(payload.get("cli_commands") if isinstance(payload.get("cli_commands"), list) else []),
        "documented_command_count": len(
            payload.get("normalized_documented_commands")
            if isinstance(payload.get("normalized_documented_commands"), list)
            else []
        ),
        "issue_count": payload.get("issue_count"),
        "top_issue": payload.get("top_issue"),
    }
    snapshot["fingerprint"] = work_cmd._stable_hash(snapshot)
    return snapshot


def _candidate_health(target: Path) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    latest = _latest_candidate(target)
    if latest is None:
        return {"latest": None, "checks": checks, "issue_count": 0, "top_issue": None}
    created = work_cmd._parse_iso_datetime(latest.get("created_at"))
    if created is not None:
        age_hours = (_now() - created).total_seconds() / 3600
        if age_hours > RELEASE_CANDIDATE_STALE_HOURS:
            checks.append(
                {
                    "status": WARN,
                    "name": "release_candidate_stale",
                    "detail": f"{latest.get('candidate_id')}={age_hours:.1f}h",
                }
            )
    git = latest.get("git") if isinstance(latest.get("git"), dict) else {}
    current_head = _git_value(target, "rev-parse", "HEAD")
    if git.get("head") and current_head and git.get("head") != current_head:
        checks.append(
            {
                "status": WARN,
                "name": "release_candidate_head_changed",
                "detail": f"{latest.get('candidate_id')} head changed",
            }
        )
    readiness = latest.get("release_readiness") if isinstance(latest.get("release_readiness"), dict) else {}
    if readiness.get("ready") is False:
        checks.append(
            {
                "status": WARN,
                "name": "release_candidate_blocked",
                "detail": f"{latest.get('candidate_id')} readiness was blocked",
            }
        )
    for label, value in (
        ("release_candidate_missing_release_receipt", readiness.get("path")),
        (
            "release_candidate_missing_work_closeout",
            (latest.get("work_closeout") or {}).get("path") if isinstance(latest.get("work_closeout"), dict) else None,
        ),
        (
            "release_candidate_missing_verification",
            (latest.get("verification") or {}).get("path") if isinstance(latest.get("verification"), dict) else None,
        ),
    ):
        if value and not Path(str(value)).exists():
            checks.append({"status": WARN, "name": label, "detail": str(value)})
    return {"latest": latest, "checks": checks, "issue_count": len(checks), "top_issue": checks[0] if checks else None}


def _release_dogfood_health(target: Path) -> dict[str, Any]:
    from .. import daily_cmd

    target = target.expanduser().resolve()

    def local_ref(payload: dict[str, Any] | None, id_field: str) -> dict[str, Any] | None:
        if not isinstance(payload, dict):
            return None
        return {
            "id": payload.get(id_field),
            "status": payload.get("status"),
            "path": payload.get("path"),
        }

    checks: list[dict[str, Any]] = []
    latest_readiness = _latest_release_receipt(target)
    latest_candidate = _latest_candidate(target)
    latest_daily_run = daily_cmd._latest_run(target)
    if latest_readiness is None:
        checks.append(
            {
                "status": WARN,
                "name": "release_dogfood_readiness_missing",
                "detail": "no release readiness receipt found",
                "phase": 155,
                "suggested_next_command": "brigade release run",
            }
        )
    elif not latest_readiness.get("ready"):
        checks.append(
            {
                "status": WARN,
                "name": "release_dogfood_readiness_blocked",
                "detail": str(latest_readiness.get("run_id") or "latest"),
                "phase": 158,
                "suggested_next_command": f"brigade release show {latest_readiness.get('run_id')}",
            }
        )
    if latest_candidate is None:
        checks.append(
            {
                "status": WARN,
                "name": "release_dogfood_candidate_missing",
                "detail": "no release candidate evidence found",
                "phase": 156,
                "suggested_next_command": "brigade release candidate build",
            }
        )
    else:
        for key in ("daily_driver", "daily_hardening", "inbox_quality", "repo_fleet_daily_use"):
            if not isinstance(latest_candidate.get(key), dict):
                checks.append(
                    {
                        "status": WARN,
                        "name": f"release_dogfood_candidate_missing_{key}",
                        "detail": f"candidate missing {key}",
                        "phase": 157,
                        "suggested_next_command": "brigade release candidate build",
                    }
                )
        candidate_path = Path(str(latest_candidate.get("path") or ""))
        publish_plan = candidate_path / "PUBLISH_PLAN.md"
        if publish_plan.is_file():
            unsafe_lines = [
                line
                for line in publish_plan.read_text().splitlines()
                if any(command in line for command in ("git push", "git tag", "gh release create"))
                and "Manual-only" not in line
            ]
            if unsafe_lines:
                checks.append(
                    {
                        "status": WARN,
                        "name": "release_dogfood_publish_plan_not_manual",
                        "detail": "remote-mutating publish command is not marked manual-only",
                        "phase": 160,
                        "suggested_next_command": f"brigade release candidate show {latest_candidate.get('candidate_id')}",
                    }
                )
        else:
            checks.append(
                {
                    "status": WARN,
                    "name": "release_dogfood_publish_plan_missing",
                    "detail": "candidate publish plan is missing",
                    "phase": 160,
                    "suggested_next_command": "brigade release candidate build",
                }
            )
    if latest_daily_run is not None:
        if latest_daily_run.get("closeout_status") and "verification_status" not in latest_daily_run:
            checks.append(
                {
                    "status": WARN,
                    "name": "release_dogfood_daily_closeout_missing_verification",
                    "detail": str(latest_daily_run.get("run_id") or "latest"),
                    "phase": 161,
                    "suggested_next_command": "brigade daily closeout --json",
                }
            )
    schema_ids = {schema["id"] for schema in _schema_manifest_schemas()}
    if "release-dogfood-health" not in schema_ids:
        checks.append(
            {
                "status": WARN,
                "name": "release_dogfood_schema_missing",
                "detail": "schema manifest missing release dogfood health",
                "phase": 163,
                "suggested_next_command": "brigade release schema",
            }
        )
    return {
        "schema_version": SCHEMA_MANIFEST_VERSION,
        "schema": {"name": "release-dogfood-health", "version": SCHEMA_MANIFEST_VERSION},
        "target": str(target),
        "latest_readiness": local_ref(latest_readiness, "run_id"),
        "latest_candidate": local_ref(latest_candidate, "candidate_id"),
        "latest_daily_run": local_ref(latest_daily_run, "run_id"),
        "checks": checks,
        "issue_count": len(checks),
        "top_issue": checks[0] if checks else None,
    }


def _receipt_ref(payload: dict[str, Any] | None, id_field: str) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    return {
        "id": payload.get(id_field),
        "path": payload.get("path"),
        "status": payload.get("status"),
    }


def _candidate_release_notes(candidate: dict[str, Any]) -> str:
    inputs = candidate.get("release_notes_inputs") if isinstance(candidate.get("release_notes_inputs"), dict) else {}
    changelog = inputs.get("changelog_unreleased") if isinstance(inputs.get("changelog_unreleased"), list) else []
    commits = inputs.get("commit_subjects") if isinstance(inputs.get("commit_subjects"), list) else []
    docs = inputs.get("touched_docs") if isinstance(inputs.get("touched_docs"), list) else []
    lines = ["# Release Notes Draft", "", "## Highlights", ""]
    if changelog:
        lines.extend(f"- {item}" for item in changelog[:10])
    else:
        lines.append("- review-needed: summarize user-visible changes.")
    lines.extend(["", "## Commit Subjects", ""])
    lines.extend(f"- {item}" for item in commits[:20]) if commits else lines.append(
        "- review-needed: no commit subjects found for base ref."
    )
    lines.extend(["", "## Documentation Touched", ""])
    lines.extend(f"- `{item}`" for item in docs[:20]) if docs else lines.append(
        "- review-needed: confirm docs coverage."
    )
    return "\n".join(lines) + "\n"


def _candidate_publish_plan(candidate: dict[str, Any]) -> str:
    head = candidate.get("git", {}).get("short_head") if isinstance(candidate.get("git"), dict) else None
    branch = candidate.get("git", {}).get("branch") if isinstance(candidate.get("git"), dict) else None
    lines = [
        "# Publish Plan",
        "",
        "- [ ] Review `RELEASE_CANDIDATE.md`.",
        "- [ ] Review `EVIDENCE.json`.",
        "- [ ] Run `brigade work verify run` if verification is stale.",
        "- [ ] Run `brigade work closeout latest` if work closeout is stale.",
        "- [ ] Run `brigade release doctor`.",
        "- [ ] Run content-guard through the configured pre-push hook or `brigade scrub`.",
        f"- [ ] Manual-only remote step: `git tag <version> {head or 'HEAD'}`.",
        f"- [ ] Manual-only remote step: `git push origin {branch or '<branch>'} --tags`.",
        "- [ ] Manual-only remote step: `gh release create <version> --notes-file RELEASE_NOTES_DRAFT.md`.",
    ]
    return "\n".join(lines) + "\n"


def _candidate_summary(candidate: dict[str, Any]) -> str:
    readiness = candidate.get("release_readiness") if isinstance(candidate.get("release_readiness"), dict) else {}
    lines = [
        "# Release Candidate",
        "",
        f"- Candidate: `{candidate.get('candidate_id')}`",
        f"- Status: {candidate.get('status')}",
        f"- Ready: {candidate.get('ready')}",
        f"- Readiness: `{readiness.get('run_id')}` [{readiness.get('status')}]",
        f"- Base ref: {candidate.get('base_ref')}",
        "",
        "## Blockers",
        "",
    ]
    blockers = candidate.get("blockers") if isinstance(candidate.get("blockers"), list) else []
    lines.extend(f"- {item}" for item in blockers) if blockers else lines.append("- none")
    lines.extend(["", "## Warnings", ""])
    warnings = candidate.get("warnings") if isinstance(candidate.get("warnings"), list) else []
    lines.extend(f"- {item}" for item in warnings) if warnings else lines.append("- none")
    lines.extend(["", "## Changed Files", ""])
    changed = candidate.get("changed_files") if isinstance(candidate.get("changed_files"), list) else []
    lines.extend(f"- `{item}`" for item in changed[:80]) if changed else lines.append("- none")
    return "\n".join(lines) + "\n"


def _write_candidate_bundle(candidate_dir: Path, candidate: dict[str, Any]) -> None:
    reportstore.write_bundle(
        candidate_dir,
        candidate,
        evidence_name="EVIDENCE.json",
        documents={
            "RELEASE_CANDIDATE.md": _candidate_summary(candidate),
            "RELEASE_NOTES_DRAFT.md": _candidate_release_notes(candidate),
            "PUBLISH_PLAN.md": _candidate_publish_plan(candidate),
        },
    )


def plan(*, target: Path, base_ref: str | None = "origin/main", json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload = _payload(target, base_ref=base_ref, run_checks=False)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"release plan: {target}")
    print(f"status: {payload['status']}")
    print(f"blockers: {len(payload['blockers'])}")
    for blocker in payload["blockers"]:
        print(f"- {blocker}")
    print(f"warnings: {len(payload['warnings'])}")
    for warning in payload["warnings"]:
        print(f"- {warning}")
    print("run: brigade release run")
    return 0


def doctor(*, target: Path, base_ref: str | None = "origin/main", json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload = _payload_with_candidate_health(_payload(target, base_ref=base_ref, run_checks=True), target)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["ready"] else 1
    print(f"release doctor: {target}")
    print(f"status: {payload['status']}")
    for check in payload["checks"]:
        print(f"[{check['status']}] {check['name']}: {check['detail']}")
    for blocker in payload["blockers"]:
        print(f"blocker: {blocker}")
    for warning in payload["warnings"]:
        print(f"warning: {warning}")
    return 0 if payload["ready"] else 1


def schema(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload = _schema_manifest(target)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"release schema manifest: {target}")
    print(f"manifest_version: {payload['manifest_version']}")
    print(f"schemas: {payload['schema_count']}")
    print(f"issues: {payload['issue_count']}")
    for schema_item in payload["schemas"]:
        print(f"- {schema_item['id']}: {schema_item['file']}")
    for check in payload["checks"]:
        print(f"[{check['status']}] {check['name']}: {check['detail']}")
    return 0


def candidate_plan(*, target: Path, base_ref: str | None = "origin/main", json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    candidate = _candidate_payload(target, base_ref=base_ref)
    candidate.update(
        {
            "candidate_id": "planned",
            "created_at": None,
            "status": candidate["release_readiness"].get("status"),
            "ready": candidate["release_readiness"].get("ready"),
            "candidate_root": str(_release_candidates_root(target)),
            "bundle_files": ["RELEASE_CANDIDATE.md", "RELEASE_NOTES_DRAFT.md", "PUBLISH_PLAN.md", "EVIDENCE.json"],
        }
    )
    if json_output:
        print(json.dumps(candidate, indent=2, sort_keys=True))
        return 0
    print(f"release candidate plan: {target}")
    print(f"status: {candidate['status']}")
    print(f"ready: {candidate['ready']}")
    print(f"blockers: {len(candidate['blockers'])}")
    for blocker in candidate["blockers"]:
        print(f"- {blocker}")
    print(f"candidate_root: {candidate['candidate_root']}")
    print("run: brigade release candidate build")
    return 0


def candidate_build(*, target: Path, base_ref: str | None = "origin/main", json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    created = _now()
    candidate_id = f"{created.strftime('%Y%m%d-%H%M%S')}-candidate-{uuid4().hex[:6]}"
    candidate_dir = _release_candidates_root(target) / candidate_id
    candidate = _candidate_payload(target, base_ref=base_ref)
    candidate.update(
        {
            "candidate_id": candidate_id,
            "created_at": created.isoformat(),
            "status": candidate["release_readiness"].get("status"),
            "ready": candidate["release_readiness"].get("ready"),
            "path": str(candidate_dir),
            "bundle_files": ["RELEASE_CANDIDATE.md", "RELEASE_NOTES_DRAFT.md", "PUBLISH_PLAN.md", "EVIDENCE.json"],
        }
    )
    _write_candidate_bundle(candidate_dir, candidate)
    if json_output:
        print(json.dumps(candidate, indent=2, sort_keys=True))
        return 0
    print(f"release candidate: {candidate_id}")
    print(f"status: {candidate['status']}")
    print(f"ready: {candidate['ready']}")
    print(f"blockers: {len(candidate['blockers'])}")
    print(f"path: {candidate_dir}")
    return 0


def candidate_list(*, target: Path, limit: int = 20, json_output: bool = False) -> int:
    if limit < 1:
        print("error: --limit must be a positive integer", file=sys.stderr)
        return 2
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    candidates = _release_candidates(target)[:limit]
    payload = {"target": str(target), "candidate_root": str(_release_candidates_root(target)), "candidates": candidates}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"release candidates: {target}")
    print(f"candidate_root: {payload['candidate_root']}")
    if not candidates:
        print("candidates: none")
        return 0
    for candidate in candidates:
        print(
            f"- {candidate.get('candidate_id')} [{candidate.get('status')}] ready={candidate.get('ready')} {candidate.get('created_at')}"
        )
    return 0


def candidate_show(*, target: Path, candidate_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    candidate, error = _resolve_candidate(target, candidate_id)
    if candidate is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    if json_output:
        print(json.dumps(candidate, indent=2, sort_keys=True))
        return 0
    print(f"release candidate: {candidate.get('candidate_id')}")
    print(f"status: {candidate.get('status')}")
    print(f"ready: {candidate.get('ready')}")
    print(f"path: {candidate.get('path')}")
    print(f"blockers: {len(candidate.get('blockers') or [])}")
    for blocker in candidate.get("blockers") or []:
        print(f"- {blocker}")
    print(f"warnings: {len(candidate.get('warnings') or [])}")
    return 0


def candidate_archive(*, target: Path, candidate_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    candidate, error = _resolve_candidate(target, candidate_id)
    if candidate is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    source = Path(str(candidate.get("path") or ""))
    if not source.is_dir() or source.parent == _release_candidates_archive_root(target):
        print(f"error: release candidate cannot be archived: {candidate.get('candidate_id')}", file=sys.stderr)
        return 2
    destination, moved = reportstore.move_bundle(source, _release_candidates_archive_root(target))
    if not moved:
        print(f"error: archived release candidate already exists: {candidate.get('candidate_id')}", file=sys.stderr)
        return 2
    payload = {
        "target": str(target),
        "candidate_id": candidate.get("candidate_id"),
        "archived_at": _now().isoformat(),
        "archive_path": str(destination),
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"archived release candidate: {payload['candidate_id']}")
    print(f"archive_path: {payload['archive_path']}")
    return 0
