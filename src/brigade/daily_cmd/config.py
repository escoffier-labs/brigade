"""Agent-facing daily driver over local Brigade operator state."""
# ruff: noqa: E402,F401,F403,F811,F821

from __future__ import annotations

import json
import os
import re
import signal
import shutil
import subprocess
import sys
import threading
import time
from contextlib import redirect_stdout
from collections import Counter
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from typing import Any
from uuid import uuid4

from .. import (
    center_cmd,
    context_cmd,
    handoff_cmd,
    memory_cmd,
    notifications_cmd,
    phases_cmd,
    security_cmd,
    toml_compat as tomllib,
    tools_cmd,
    work_cmd,
)
from ..localio import read_json_dict as _read_json, utc_now as _now, write_json as _write_json
from ..render import emit

SCHEMA_VERSION = 1

RUN_STATUSES = {"reviewed", "deferred", "blocked", "archived"}

APPROVAL_STATUSES = {"pending", "approved", "rejected", "held", "consumed"}

PREFERRED_MODES = {"task-first", "inbox-first", "readiness-first"}

RISK_LEVELS = {"low": 1, "medium": 2, "high": 3}

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "preferred_mode": "task-first",
    "max_risk_without_approval": "medium",
    "allow_context_pack_build": True,
    "allow_operator_report_build": True,
    "allow_readiness_imports": True,
    "allow_import_promotion_with_approval": True,
    "allow_work_run": True,
    "verification_required_for_work_run": False,
    "verification_required_for_import_promotion": False,
    "verification_required_for_release_actions": False,
    "allowed_verification_commands": "",
    "verification_timeout": 600,
    "stale_plan_threshold_hours": 12,
    "stale_run_threshold_hours": 12,
}

DEFAULT_STATUS_SECTION_TIMEOUT_SECONDS = 8


class _DailyStatusSectionTimeout(Exception):
    def __init__(self, label: str):
        super().__init__(label)
        self.label = label


def _daily_root(target: Path) -> Path:
    return target / ".brigade" / "daily"


def _config_path(target: Path) -> Path:
    return target / ".brigade" / "daily.toml"


def _plans_root(target: Path) -> Path:
    return _daily_root(target) / "plans"


def _runs_root(target: Path) -> Path:
    return _daily_root(target) / "runs"


def _approvals_root(target: Path) -> Path:
    return _daily_root(target) / "approvals"


def _approvals_archive_root(target: Path) -> Path:
    return _daily_root(target) / "approval-archive"


def _repairs_root(target: Path) -> Path:
    return _daily_root(target) / "repairs"


def _unblocks_root(target: Path) -> Path:
    return _daily_root(target) / "unblocks"


def _telemetry_root(target: Path) -> Path:
    return _daily_root(target) / "telemetry"


def _hardening_root(target: Path) -> Path:
    return _daily_root(target) / "hardening"


def _hardening_closeouts_root(target: Path) -> Path:
    return _hardening_root(target) / "closeouts"


HARDENING_WORKSTREAMS: list[dict[str, Any]] = [
    {
        "id": "daily-production-hardening",
        "phase_start": 115,
        "phase_end": 124,
        "focus": "make the daily loop recoverable, explainable, and consistently receipt-backed",
        "checks": ["daily config", "adapter receipts", "plan explanations", "approval hygiene", "telemetry health"],
    },
    {
        "id": "operator-center-contract-cleanup",
        "phase_start": 125,
        "phase_end": 134,
        "focus": "normalize center status, activity, reviews, templates, and schema contracts",
        "checks": ["center schema", "review item shape", "receipt references", "suggested commands"],
    },
    {
        "id": "inbox-evidence-quality",
        "phase_start": 135,
        "phase_end": 144,
        "focus": "reduce inbox noise and improve provenance, acceptance, and evidence quality",
        "checks": ["pending import acceptance", "source provenance", "stale imports", "noisy sources"],
    },
    {
        "id": "repo-fleet-daily-use",
        "phase_start": 145,
        "phase_end": 154,
        "focus": "keep fleet reports, actions, dispatch, and release trains visible in daily planning",
        "checks": ["repo fleet health", "fleet actions", "fleet sweeps", "release trains"],
    },
    {
        "id": "self-dogfood-release-loop",
        "phase_start": 155,
        "phase_end": 164,
        "focus": "make Brigade's own release path readable through daily receipts and release evidence",
        "checks": ["release readiness", "release candidate", "verification", "daily evidence in release"],
    },
]

HARDENING_PHASE_TITLES: dict[int, str] = {
    115: "audit daily config and unsafe local policy states",
    116: "verify daily run receipts have normalized adapter results",
    117: "verify daily plan receipts have candidate explanations",
    118: "track approval hygiene and stale approval requests",
    119: "track telemetry warnings and repeated blockers",
    120: "route unresolved daily reliability findings into the work inbox",
    121: "close out reviewed daily reliability findings",
    122: "keep daily protocol output stable for wrappers",
    123: "keep JSON output clean when wrapped commands print noise",
    124: "carry daily reliability state into release evidence",
    125: "audit center schema manifest presence",
    126: "audit center review item field coverage",
    127: "verify review items include suggested next commands",
    128: "verify receipt references stay local and safe",
    129: "surface center contract findings in daily hardening audit",
    130: "route center contract findings into the work inbox",
    131: "keep center status readable without subsystem-specific parsing",
    132: "keep center reviews as the unified local review queue",
    133: "document center schema expectations for wrappers",
    134: "carry center contract state into release evidence",
    135: "audit pending imports missing acceptance",
    136: "audit pending imports missing provenance",
    137: "audit inbox hygiene issues",
    138: "penalize noisy and deferred imports in daily planning",
    139: "preserve changed-fingerprint resurfacing",
    140: "route inbox quality findings into the work inbox",
    141: "keep imported findings deduped",
    142: "bias daily action selection toward high-evidence items",
    143: "document inbox quality expectations",
    144: "carry inbox quality state into release evidence",
    145: "audit repo fleet health from the daily hardening layer",
    146: "surface fleet action queue health",
    147: "surface fleet sweep health",
    148: "surface fleet release train health",
    149: "route fleet daily-use findings into the work inbox",
    150: "keep fleet dispatch manual and local",
    151: "keep fleet release plans manual-only",
    152: "keep safe repo labels and receipt labels only",
    153: "document fleet daily-use expectations",
    154: "carry fleet state into release evidence",
    155: "audit latest release readiness receipt",
    156: "audit latest release candidate packet",
    157: "verify release candidate evidence includes daily driver state",
    158: "surface blocked release readiness in daily hardening audit",
    159: "route release dogfood findings into the work inbox",
    160: "keep publish steps manual-only",
    161: "keep daily closeout verification evidence visible",
    162: "document the daily-to-release self-dogfood path",
    163: "keep release schema output stable for wrappers",
    164: "close out the hardening tranche with verification and handoff",
}

IMPLEMENTED_HARDENING_PHASES: set[int] = set(range(115, 165))


def _hardening_phases() -> list[dict[str, Any]]:
    phases: list[dict[str, Any]] = []
    for stream in HARDENING_WORKSTREAMS:
        for phase in range(int(stream["phase_start"]), int(stream["phase_end"]) + 1):
            phases.append(
                {
                    "phase": phase,
                    "workstream": stream["id"],
                    "title": HARDENING_PHASE_TITLES.get(
                        phase, f"{stream['focus']} #{phase - int(stream['phase_start']) + 1}"
                    ),
                    "status": "implemented" if phase in IMPLEMENTED_HARDENING_PHASES else "planned",
                }
            )
    return phases


def _schema(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "version": SCHEMA_VERSION,
        "item_fields": [
            "action_id",
            "source_subsystem",
            "source_local_id",
            "safe_summary",
            "score",
            "risk_level",
            "approval_required",
            "suggested_next_command",
        ],
    }


def _schemas() -> dict[str, Any]:
    base_fields = _schema("daily-item")["item_fields"]
    return {
        "schema_version": SCHEMA_VERSION,
        "schemas": [
            {
                "name": "daily-status",
                "top_level_fields": ["target", "selected_action", "next_recommended_command", "daily_health"],
                "item_fields": base_fields,
            },
            {
                "name": "daily-plan",
                "top_level_fields": [
                    "plan_id",
                    "candidate_actions",
                    "selected_action",
                    "approval_required",
                    "recorded",
                ],
                "item_fields": base_fields,
            },
            {
                "name": "daily-review",
                "top_level_fields": [
                    "selected_action",
                    "selected_adapter",
                    "source_evidence_refs",
                    "acceptance",
                    "config_blockers",
                    "context_pack_would_build",
                ],
                "item_fields": base_fields,
            },
            {
                "name": "daily-run",
                "top_level_fields": [
                    "run_id",
                    "plan_id",
                    "selected_action",
                    "status",
                    "commands_invoked",
                    "receipts_created",
                    "blockers",
                ],
                "item_fields": base_fields,
            },
            {
                "name": "daily-closeout",
                "top_level_fields": ["run_id", "closeout_status", "reviewed_at", "handoff_path"],
                "item_fields": [],
            },
            {
                "name": "daily-history",
                "top_level_fields": ["runs", "plans", "run_count", "plan_count"],
                "item_fields": ["id", "status", "created_at", "path"],
            },
            {
                "name": "daily-doctor",
                "top_level_fields": ["checks", "issue_count", "top_issue", "health"],
                "item_fields": ["status", "name", "detail"],
            },
            {
                "name": "daily-approval",
                "top_level_fields": [
                    "approval_id",
                    "status",
                    "selected_action",
                    "selected_adapter",
                    "source_fingerprint",
                ],
                "item_fields": ["approval_id", "status", "safe_summary", "suggested_next_command"],
            },
            {
                "name": "daily-approval-compare",
                "top_level_fields": ["approval_id", "issues", "ok"],
                "item_fields": ["name", "status", "detail"],
            },
            {
                "name": "daily-approval-archive",
                "top_level_fields": ["archived", "archived_count"],
                "item_fields": ["approval_id", "status", "archive_path"],
            },
            {
                "name": "daily-resume",
                "top_level_fields": ["status", "latest_run", "action_taken", "next_recommended_command"],
                "item_fields": ["name", "detail", "status"],
            },
            {
                "name": "daily-repair",
                "top_level_fields": ["repair_id", "checks", "suggestions", "writes"],
                "item_fields": ["name", "detail", "status"],
            },
            {
                "name": "daily-unblock",
                "top_level_fields": ["unblock_id", "created_imports", "approval_request", "blockers"],
                "item_fields": ["id", "source", "kind", "status"],
            },
            {
                "name": "daily-protocol",
                "top_level_fields": ["steps", "commands", "safety_boundaries"],
                "item_fields": ["step", "command", "purpose"],
            },
            {
                "name": "daily-telemetry",
                "top_level_fields": ["metrics", "issue_count", "top_issue"],
                "item_fields": ["name", "value", "detail"],
            },
            {
                "name": "daily-hardening-plan",
                "top_level_fields": ["phase_count", "workstreams", "phases"],
                "item_fields": ["phase", "workstream", "title", "status"],
            },
            {
                "name": "daily-hardening-audit",
                "top_level_fields": ["workstreams", "findings", "issue_count", "top_issue"],
                "item_fields": ["finding_id", "workstream", "severity", "safe_summary"],
            },
            {
                "name": "daily-hardening-import-issues",
                "top_level_fields": ["created_imports", "skipped_imports", "finding_count"],
                "item_fields": ["id", "source", "kind", "status"],
            },
            {
                "name": "daily-hardening-closeout",
                "top_level_fields": ["closeout_id", "status", "finding_count", "unresolved_count"],
                "item_fields": ["finding_id", "severity", "safe_summary"],
            },
        ],
    }


def _write_config(path: Path, config: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Local Brigade daily driver settings.", ""]
    for key, value in config.items():
        if isinstance(value, bool):
            rendered = "true" if value else "false"
        elif isinstance(value, int):
            rendered = str(value)
        else:
            rendered = json.dumps(str(value))
        lines.append(f"{key} = {rendered}")
    path.write_text("\n".join(lines) + "\n")


def _load_config(target: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    path = _config_path(target)
    checks: list[dict[str, Any]] = []
    config = dict(DEFAULT_CONFIG)
    if not path.exists():
        checks.append(
            {"status": "warn", "name": "daily_config_missing", "detail": f"missing {path}; run `brigade daily init`"}
        )
        return config, checks
    try:
        loaded = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:
        checks.append({"status": "fail", "name": "daily_config_invalid", "detail": str(exc)})
        return config, checks
    if not isinstance(loaded, dict):
        checks.append({"status": "fail", "name": "daily_config_invalid", "detail": "config must be a TOML table"})
        return config, checks
    config.update(loaded)
    checks.extend(_validate_config(config))
    if not checks:
        checks.append({"status": "ok", "name": "daily_config", "detail": str(path)})
    return config, checks


def _validate_config(config: dict[str, Any]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    if config.get("preferred_mode") not in PREFERRED_MODES:
        checks.append(
            {
                "status": "fail",
                "name": "daily_preferred_mode",
                "detail": "expected task-first, inbox-first, or readiness-first",
            }
        )
    if config.get("max_risk_without_approval") not in RISK_LEVELS:
        checks.append({"status": "fail", "name": "daily_max_risk", "detail": "expected low, medium, or high"})
    for key in (
        "enabled",
        "allow_context_pack_build",
        "allow_operator_report_build",
        "allow_readiness_imports",
        "allow_import_promotion_with_approval",
        "allow_work_run",
        "verification_required_for_work_run",
        "verification_required_for_import_promotion",
        "verification_required_for_release_actions",
    ):
        if not isinstance(config.get(key), bool):
            checks.append({"status": "fail", "name": key, "detail": "expected boolean"})
    for key in ("stale_plan_threshold_hours", "stale_run_threshold_hours", "verification_timeout"):
        value = config.get(key)
        if not isinstance(value, int) or value < 1:
            checks.append({"status": "fail", "name": key, "detail": "expected positive integer"})
    commands = config.get("allowed_verification_commands")
    if not isinstance(commands, str) and not (
        isinstance(commands, list) and all(isinstance(item, str) for item in commands)
    ):
        checks.append(
            {"status": "fail", "name": "allowed_verification_commands", "detail": "expected string or list of strings"}
        )
    if config.get("enabled") is False:
        checks.append(
            {"status": "warn", "name": "daily_disabled", "detail": "daily driver is disabled in local config"}
        )
    if config.get("max_risk_without_approval") == "high":
        checks.append(
            {"status": "warn", "name": "daily_risk_policy", "detail": "high risk actions are allowed without approval"}
        )
    return checks


def _safe_text(target: Path, value: object) -> str:
    text = str(value or "")
    text = text.replace(str(target), "<target>")
    text = re.sub(r"/(?:tmp|home|Users|private|mnt|Volumes)/[A-Za-z0-9_.@/-]+", "<path>", text)
    text = re.sub(r"https?://[^\s`\"'<>]+", "<url>", text)
    text = re.sub(r"(?i)(token|secret|password|api[_-]?key)=\S+", r"\1=<redacted>", text)
    return text[:500]


def _fingerprint(value: Any) -> str:
    return work_cmd._stable_hash(value)


def _slug(value: object) -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "item").strip().lower()).strip("-")
    return text[:80] or "item"


def _status_section_timeout_seconds() -> int:
    raw = os.environ.get("BRIGADE_DAILY_STATUS_SECTION_TIMEOUT")
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            return DEFAULT_STATUS_SECTION_TIMEOUT_SECONDS
    return DEFAULT_STATUS_SECTION_TIMEOUT_SECONDS


def _status_section_check(label: str, status: str, detail: str, elapsed_ms: int) -> dict[str, Any]:
    return {
        "status": status,
        "name": f"daily_status_section:{label}",
        "detail": detail,
        "elapsed_ms": elapsed_ms,
    }


def _bounded_status_call(
    label: str, func, fallback: Any, *, timeout_seconds: int | None = None
) -> tuple[Any, dict[str, Any]]:
    timeout = timeout_seconds or _status_section_timeout_seconds()
    start = time.monotonic()
    use_alarm = threading.current_thread() is threading.main_thread() and hasattr(signal, "SIGALRM")
    previous_handler = None
    previous_timer = None
    if use_alarm:
        previous_handler = signal.getsignal(signal.SIGALRM)
        previous_timer = signal.getitimer(signal.ITIMER_REAL)

        def _raise_timeout(_signum, _frame):
            raise _DailyStatusSectionTimeout(label)

        signal.signal(signal.SIGALRM, _raise_timeout)
        signal.setitimer(signal.ITIMER_REAL, timeout)
    try:
        result = func()
        elapsed = int((time.monotonic() - start) * 1000)
        return result, _status_section_check(label, "ok", "completed", elapsed)
    except _DailyStatusSectionTimeout:
        elapsed = int((time.monotonic() - start) * 1000)
        return fallback, _status_section_check(label, "warn", f"timed out after {timeout}s", elapsed)
    except Exception as exc:
        elapsed = int((time.monotonic() - start) * 1000)
        return fallback, _status_section_check(
            label, "warn", _safe_text(Path("."), f"{type(exc).__name__}: {exc}"), elapsed
        )
    finally:
        if use_alarm:
            signal.setitimer(signal.ITIMER_REAL, 0)
            if previous_handler is not None:
                signal.signal(signal.SIGALRM, previous_handler)
            if previous_timer is not None and previous_timer[0] > 0:
                signal.setitimer(signal.ITIMER_REAL, previous_timer[0], previous_timer[1])
