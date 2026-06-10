"""Local chat surface export scanner helpers."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import toml_compat as tomllib
from .install import apply_gitignore
from .selection import Selection
from .localio import utc_now as _now

CONFIG_REL_PATH = ".brigade/chat-surfaces.toml"
OUTPUT_ROOT_REL_PATH = ".brigade/chat-memory-sweeps"
SWEEP_STALE_HOURS = 48
PROVIDERS = ("discord-export", "slack-export", "telegram-export", "clickclack-export", "generic-jsonl")
PROVIDER_ALIASES = {
    "clickclack": "clickclack-export",
    "clickclack-export": "clickclack-export",
    "discord": "discord-export",
    "discord-export": "discord-export",
    "discord-json": "discord-export",
    "generic": "generic-jsonl",
    "generic-json": "generic-jsonl",
    "generic-jsonl": "generic-jsonl",
    "jsonl": "generic-jsonl",
    "slack": "slack-export",
    "slack-export": "slack-export",
    "slack-json": "slack-export",
    "telegram": "telegram-export",
    "telegram-export": "telegram-export",
    "telegram-json": "telegram-export",
}
PRIVACY_MODES = ("summary-only", "redact-raw", "strict")
EVIDENCE_POLICIES = ("summary-only", "local-path", "none")
PRIORITIES = ("low", "normal", "high", "urgent")
ISSUE_TYPES = ("task", "incident", "finding", "decision", "preference", "link", "command")
RAW_FIELD_NAMES = {
    "body",
    "bodies",
    "message",
    "message_body",
    "message_bodies",
    "message_text",
    "messages",
    "private_text",
    "quote",
    "quotes",
    "raw",
    "raw_body",
    "raw_message",
    "raw_messages",
    "raw_text",
    "text",
    "transcript",
    "transcripts",
}
UNSAFE_FIELD_NAMES = {
    "channel_id",
    "dm_id",
    "host",
    "hostname",
    "message_id",
    "private_url",
    "token",
    "url",
    "user_id",
    "webhook",
    "webhook_url",
}
UNSAFE_VALUE_RE = re.compile(
    r"(?:https?://[^\s]+|[A-Za-z0-9_]*token[A-Za-z0-9_]*|xox[baprs]-[A-Za-z0-9-]+|[A-Za-z0-9.-]+\.[A-Za-z]{2,})",
    re.IGNORECASE,
)
DEFAULT_SURFACES = (
    {
        "id": "discord-export",
        "provider": "discord-export",
        "workspace_label": "local-discord-export",
        "channel_label": "triage",
        "export_path": ".brigade/chat-surfaces/discord-export.json",
        "sweep_output_path": ".brigade/chat-memory-sweeps/discord-export-latest.json",
        "enabled": False,
        "privacy_mode": "summary-only",
        "evidence_policy": "local-path",
        "confidence_threshold": "medium",
    },
    {
        "id": "slack-export",
        "provider": "slack-export",
        "workspace_label": "local-slack-export",
        "channel_label": "triage",
        "export_path": ".brigade/chat-surfaces/slack-export.json",
        "sweep_output_path": ".brigade/chat-memory-sweeps/slack-export-latest.json",
        "enabled": False,
        "privacy_mode": "summary-only",
        "evidence_policy": "local-path",
        "confidence_threshold": "medium",
    },
    {
        "id": "telegram-export",
        "provider": "telegram-export",
        "workspace_label": "local-telegram-export",
        "channel_label": "triage",
        "export_path": ".brigade/chat-surfaces/telegram-export.json",
        "sweep_output_path": ".brigade/chat-memory-sweeps/telegram-export-latest.json",
        "enabled": False,
        "privacy_mode": "summary-only",
        "evidence_policy": "local-path",
        "confidence_threshold": "medium",
    },
    {
        "id": "clickclack-export",
        "provider": "clickclack-export",
        "workspace_label": "local-clickclack-export",
        "channel_label": "triage",
        "export_path": ".brigade/chat-surfaces/clickclack-export.json",
        "sweep_output_path": ".brigade/chat-memory-sweeps/clickclack-export-latest.json",
        "enabled": False,
        "privacy_mode": "summary-only",
        "evidence_policy": "local-path",
        "confidence_threshold": "medium",
    },
    {
        "id": "generic-jsonl",
        "provider": "generic-jsonl",
        "workspace_label": "local-generic-jsonl",
        "channel_label": "triage",
        "export_path": ".brigade/chat-surfaces/generic-jsonl.jsonl",
        "sweep_output_path": ".brigade/chat-memory-sweeps/generic-jsonl-latest.json",
        "enabled": False,
        "privacy_mode": "summary-only",
        "evidence_policy": "local-path",
        "confidence_threshold": "medium",
    },
)
CONFIDENCE_RANK = {"high": 0, "medium": 1, "normal": 1, "low": 2}


def _config_path(target: Path) -> Path:
    return target / CONFIG_REL_PATH


def _resolve_target_path(target: Path, value: str | None, fallback: str) -> Path:
    raw = value or fallback
    path = Path(raw).expanduser()
    if path.is_absolute():
        return path
    return target / path


def _format_config() -> str:
    lines = [
        "# Local chat surface export scanner config. Keep raw exports gitignored.",
        "",
    ]
    for item in DEFAULT_SURFACES:
        lines.append("[[surface]]")
        for key, value in item.items():
            if isinstance(value, bool):
                rendered = "true" if value else "false"
            elif isinstance(value, (int, float)):
                rendered = str(value)
            else:
                rendered = json.dumps(value)
            lines.append(f"{key} = {rendered}")
        lines.append("")
    return "\n".join(lines)


def _load_config(target: Path) -> tuple[list[dict[str, Any]], list[str]]:
    path = _config_path(target)
    if not path.is_file():
        return [], [f"chat surface config missing: {path}"]
    if tomllib is None:
        return [], ["tomllib is not available"]
    try:
        payload = tomllib.loads(path.read_text())
    except Exception as exc:
        return [], [f"chat surface config invalid: {exc}"]
    surfaces = payload.get("surface")
    if not isinstance(surfaces, list):
        return [], ["chat surface config requires [[surface]] entries"]
    valid: list[dict[str, Any]] = []
    errors: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(surfaces, start=1):
        label = f"surface {index}"
        if not isinstance(item, dict):
            errors.append(f"{label} must be a table")
            continue
        surface_id = _string(item.get("id"))
        provider = _provider(item.get("provider"))
        if not surface_id:
            errors.append(f"{label} requires id")
            continue
        if surface_id in seen:
            errors.append(f"{label} duplicate id: {surface_id}")
            continue
        seen.add(surface_id)
        if provider not in PROVIDERS:
            aliases = ", ".join(sorted(PROVIDER_ALIASES))
            errors.append(f"{surface_id}: provider must be one of: {', '.join(PROVIDERS)} or aliases: {aliases}")
            continue
        entry = dict(item)
        entry["id"] = surface_id
        entry["provider"] = provider
        entry["enabled"] = bool(item.get("enabled", True))
        entry["workspace_label"] = _string(item.get("workspace_label")) or surface_id
        entry["channel_label"] = _string(item.get("channel_label")) or "unknown"
        entry["export_path"] = _string(item.get("export_path")) or f".brigade/chat-surfaces/{surface_id}.json"
        entry["sweep_output_path"] = (
            _string(item.get("sweep_output_path")) or f".brigade/chat-memory-sweeps/{surface_id}-latest.json"
        )
        entry["privacy_mode"] = _string(item.get("privacy_mode")) or "summary-only"
        entry["evidence_policy"] = _string(item.get("evidence_policy")) or "local-path"
        entry["confidence_threshold"] = _string(item.get("confidence_threshold")) or "medium"
        if entry["privacy_mode"] not in PRIVACY_MODES:
            errors.append(f"{surface_id}: privacy_mode must be one of: {', '.join(PRIVACY_MODES)}")
        if entry["evidence_policy"] not in EVIDENCE_POLICIES:
            errors.append(f"{surface_id}: evidence_policy must be one of: {', '.join(EVIDENCE_POLICIES)}")
        valid.append(entry)
    return valid, errors


def _string(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    return None


def _provider(value: object) -> str | None:
    text = _string(value)
    if not text:
        return None
    return PROVIDER_ALIASES.get(text.casefold())


def _confidence_allows(value: str | None, threshold: str | None) -> bool:
    if not threshold:
        return True
    return CONFIDENCE_RANK.get(value or "medium", 1) <= CONFIDENCE_RANK.get(threshold, 1)


def _safe_value(value: object) -> object:
    if isinstance(value, str):
        if UNSAFE_VALUE_RE.search(value):
            return "[redacted]"
        return value
    if isinstance(value, list):
        return [_safe_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _safe_value(item) for key, item in value.items()}
    return value


def _contains_unsafe_value(value: object) -> bool:
    if isinstance(value, str):
        return bool(UNSAFE_VALUE_RE.search(value))
    if isinstance(value, list):
        return any(_contains_unsafe_value(item) for item in value)
    if isinstance(value, dict):
        return any(_contains_unsafe_value(item) for item in value.values())
    return False


def _load_export(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    if not path.is_file():
        return [], [f"chat export not found: {path}"]
    if path.suffix == ".jsonl":
        records: list[dict[str, Any]] = []
        errors: list[str] = []
        for line_number, line in enumerate(path.read_text().splitlines(), start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                errors.append(f"line {line_number}: invalid JSON: {exc.msg}")
                continue
            if not isinstance(value, dict):
                errors.append(f"line {line_number}: expected JSON object")
                continue
            records.append(value)
        return records, errors
    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        return [], [f"invalid chat export JSON: {exc.msg}"]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)], [
            f"item {index} must be an object"
            for index, item in enumerate(payload, start=1)
            if not isinstance(item, dict)
        ]
    if not isinstance(payload, dict):
        return [], ["chat export must be an object, array, or JSONL file"]
    findings = payload.get("findings", payload.get("issues", []))
    if not isinstance(findings, list):
        return [], ["chat export findings must be a list"]
    return [item for item in findings if isinstance(item, dict)], [
        f"finding {index} must be an object"
        for index, item in enumerate(findings, start=1)
        if not isinstance(item, dict)
    ]


def _normalize_finding(
    value: dict[str, Any],
    *,
    surface: dict[str, Any],
    index: int,
    source_path: Path,
) -> tuple[dict[str, Any] | None, list[str], int]:
    errors: list[str] = []
    redacted = 0
    provider = _provider(value.get("provider")) or str(surface["provider"])
    surface_id = _string(value.get("surface_id")) or str(surface["id"])
    issue_id = _string(value.get("issue_id")) or _string(value.get("id"))
    issue_type = _string(value.get("issue_type")) or _string(value.get("kind")) or "task"
    priority = _string(value.get("priority")) or _string(value.get("severity")) or "normal"
    confidence = _string(value.get("confidence")) or "medium"
    safe_summary = _string(value.get("safe_summary")) or _string(value.get("summary"))
    evidence_summary = _string(value.get("evidence_summary"))
    suggested_task_text = _string(value.get("suggested_task_text")) or _string(value.get("task"))
    acceptance = value.get("acceptance_criteria", value.get("acceptance", []))
    if not issue_id:
        errors.append(f"finding {index} requires issue_id")
    if provider not in PROVIDERS:
        aliases = ", ".join(sorted(PROVIDER_ALIASES))
        errors.append(f"finding {index} provider must be one of: {', '.join(PROVIDERS)} or aliases: {aliases}")
    if surface_id != surface["id"]:
        errors.append(f"finding {index} surface_id must match selected surface: {surface['id']}")
    if issue_type not in ISSUE_TYPES:
        errors.append(f"finding {index} issue_type must be one of: {', '.join(ISSUE_TYPES)}")
    if priority not in PRIORITIES:
        errors.append(f"finding {index} priority must be one of: {', '.join(PRIORITIES)}")
    if not safe_summary:
        errors.append(f"finding {index} requires safe_summary")
    if not evidence_summary:
        errors.append(f"finding {index} requires evidence_summary")
    if not suggested_task_text:
        errors.append(f"finding {index} requires suggested_task_text")
    if not isinstance(acceptance, list) or not all(isinstance(item, str) and item.strip() for item in acceptance):
        errors.append(f"finding {index} acceptance criteria must be a list of non-empty strings")
    raw_fields = [
        key for key in value if str(key).casefold() in RAW_FIELD_NAMES or str(key).casefold().startswith("raw_")
    ]
    if raw_fields and surface.get("privacy_mode") in {"summary-only", "strict"}:
        errors.append(f"finding {index} contains raw private chat fields: {', '.join(sorted(raw_fields))}")
    unsafe_fields = [key for key in value if str(key).casefold() in UNSAFE_FIELD_NAMES]
    unsafe_values = [key for key, item in value.items() if _contains_unsafe_value(item)]
    if unsafe_fields or unsafe_values:
        redacted += len(set([*unsafe_fields, *unsafe_values]))
    if errors:
        return None, errors, redacted
    fingerprint = _string(value.get("source_fingerprint"))
    metadata = {
        "provider": provider,
        "surface_id": surface_id,
        "workspace": _safe_value(_string(value.get("workspace_label")) or surface.get("workspace_label")),
        "channel": _safe_value(_string(value.get("channel_label")) or surface.get("channel_label")),
        "thread": _safe_value(_string(value.get("thread_label")) or _string(value.get("thread"))),
        "message_range": _safe_value(_string(value.get("message_range_label")) or _string(value.get("message_range"))),
        "confidence": confidence,
        "evidence_summary": _safe_value(evidence_summary),
        "local_evidence_path": _safe_value(_string(value.get("local_evidence_path"))),
        "chat_export_path": str(source_path),
    }
    explicit_actionable = value.get("actionable")
    actionable = issue_type == "task" or (isinstance(explicit_actionable, bool) and explicit_actionable)
    normalized = {
        "id": issue_id,
        "issue_id": issue_id,
        "title": suggested_task_text,
        "summary": safe_summary,
        "kind": "task" if issue_type == "task" else issue_type,
        "severity": priority,
        "priority": priority,
        "confidence": confidence,
        "actionable": actionable,
        "provider": provider,
        "surface": surface_id,
        "workspace": metadata["workspace"],
        "channel": metadata["channel"],
        "thread": metadata["thread"],
        "message_range": metadata["message_range"],
        "evidence_summary": metadata["evidence_summary"],
        "acceptance": [item.strip() for item in acceptance],
        "metadata": {key: item for key, item in metadata.items() if item not in (None, "")},
    }
    if fingerprint:
        normalized["metadata"]["source_fingerprint"] = fingerprint
    else:
        normalized["metadata"]["source_fingerprint"] = _stable_hash(
            {
                "provider": provider,
                "surface_id": surface_id,
                "issue_id": issue_id,
                "summary": safe_summary,
                "acceptance": normalized["acceptance"],
            }
        )
    return normalized, [], redacted


def _stable_hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]


def _surface_by_id(target: Path, surface_id: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]], list[str]]:
    surfaces, errors = _load_config(target)
    if errors:
        return None, surfaces, errors
    matches = [surface for surface in surfaces if surface.get("id") == surface_id]
    if not matches:
        return None, surfaces, [f"chat surface not found: {surface_id}"]
    return matches[0], surfaces, []


def _normalized_sweep(
    target: Path, surface: dict[str, Any], findings: list[dict[str, Any]], source_path: Path
) -> tuple[dict[str, Any], list[str], int, int]:
    issues: list[dict[str, Any]] = []
    errors: list[str] = []
    redacted = 0
    invalid = 0
    for index, finding in enumerate(findings, start=1):
        normalized, finding_errors, redacted_count = _normalize_finding(
            finding,
            surface=surface,
            index=index,
            source_path=source_path,
        )
        redacted += redacted_count
        if finding_errors:
            errors.extend(finding_errors)
            invalid += 1
            continue
        if normalized is not None and _confidence_allows(
            normalized.get("confidence"), surface.get("confidence_threshold")
        ):
            issues.append(normalized)
    sweep_id = f"{surface['id']}-{_now().strftime('%Y%m%d-%H%M%S')}"
    return (
        {
            "sweep_id": sweep_id,
            "generated_at": _now().isoformat(),
            "provider": surface["provider"],
            "surface_id": surface["id"],
            "workspace": surface.get("workspace_label"),
            "channel": surface.get("channel_label"),
            "redacted": redacted,
            "issues": issues,
        },
        errors,
        invalid,
        redacted,
    )


def _write_sweep_output(target: Path, surface: dict[str, Any], payload: dict[str, Any]) -> Path:
    output_path = _resolve_target_path(
        target, _string(surface.get("sweep_output_path")), f"{OUTPUT_ROOT_REL_PATH}/{surface['id']}-latest.json"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    latest = target / OUTPUT_ROOT_REL_PATH / "latest.json"
    latest.parent.mkdir(parents=True, exist_ok=True)
    latest.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return output_path


def _health(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    surfaces, errors = _load_config(target)
    checks: list[dict[str, Any]] = []
    if errors:
        checks.append({"status": "warn", "name": "chat_surfaces_config", "detail": "; ".join(errors)})
    else:
        checks.append({"status": "ok", "name": "chat_surfaces_config", "detail": f"{len(surfaces)} surface(s)"})
    stale: list[str] = []
    missing: list[str] = []
    for surface in surfaces:
        if not surface.get("enabled"):
            continue
        output = _resolve_target_path(
            target, _string(surface.get("sweep_output_path")), f"{OUTPUT_ROOT_REL_PATH}/{surface['id']}-latest.json"
        )
        if not output.exists():
            missing.append(str(surface["id"]))
            continue
        age_hours = (_now() - datetime.fromtimestamp(output.stat().st_mtime, tz=timezone.utc)).total_seconds() / 3600
        if age_hours > SWEEP_STALE_HOURS:
            stale.append(f"{surface['id']}={age_hours:.1f}h")
    checks.append(
        {
            "status": "warn" if missing else "ok",
            "name": "chat_surface_outputs",
            "detail": ", ".join(missing) if missing else "present",
        }
    )
    checks.append(
        {
            "status": "warn" if stale else "ok",
            "name": "chat_surface_stale_outputs",
            "detail": ", ".join(stale) if stale else "none",
        }
    )
    issues = [check for check in checks if check["status"] != "ok"]
    return {
        "target": str(target),
        "config_path": str(_config_path(target)),
        "surfaces": surfaces,
        "checks": checks,
        "issue_count": len(issues),
        "top_issue": issues[0] if issues else None,
    }


def health(target: Path) -> dict[str, Any]:
    return _health(target)


def surfaces_init(*, target: Path, force: bool = False, update_gitignore: bool = True) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    path = _config_path(target)
    if path.exists() and not force:
        print(f"error: chat surface config already exists: {path}", file=sys.stderr)
        return 2
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_format_config())
    print(f"chat_surfaces_config: {path}")
    print(f"surfaces: {len(DEFAULT_SURFACES)}")
    if update_gitignore:
        result = apply_gitignore(target, Selection(depth="repo", harnesses=[], owner="this-repo", includes=[]))
        print(f"gitignore: {result}")
    else:
        print("gitignore: skipped")
    print("next_command: brigade chat surfaces list")
    return 0


def surfaces_list(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    surfaces, errors = _load_config(target)
    payload = {"target": str(target), "config_path": str(_config_path(target)), "surfaces": surfaces, "errors": errors}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if not errors else 2
    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        return 2
    print(f"chat surfaces: {target}")
    for surface in surfaces:
        print(
            f"- {surface['id']} [{surface['provider']}] enabled={surface['enabled']} channel={surface['channel_label']}"
        )
    return 0


def surfaces_show(*, target: Path, surface_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    surface, _, errors = _surface_by_id(target, surface_id)
    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        return 2
    payload = {"target": str(target), "surface": surface}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    assert surface is not None
    print(f"surface: {surface['id']}")
    print(f"provider: {surface['provider']}")
    print(f"enabled: {surface['enabled']}")
    print(f"export_path: {surface['export_path']}")
    print(f"sweep_output_path: {surface['sweep_output_path']}")
    print(f"privacy_mode: {surface['privacy_mode']}")
    return 0


def surfaces_doctor(*, target: Path, json_output: bool = False) -> int:
    payload = _health(target)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"chat surfaces doctor: {target.expanduser().resolve()}")
    for check in payload["checks"]:
        print(f"[{check['status']}] {check['name']}: {check['detail']}")
    return 0


def sweep_validate(*, target: Path, input_path: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    findings, load_errors = _load_export(input_path.expanduser().resolve())
    errors = list(load_errors)
    redacted = 0
    invalid = len(load_errors)
    for index, finding in enumerate(findings, start=1):
        dummy = {
            "id": _string(finding.get("surface_id")) or "validate",
            "provider": _provider(finding.get("provider")) or "generic-jsonl",
            "workspace_label": "validate",
            "channel_label": "validate",
            "privacy_mode": "summary-only",
            "confidence_threshold": "low",
        }
        _, finding_errors, redacted_count = _normalize_finding(
            finding,
            surface=dummy,
            index=index,
            source_path=input_path,
        )
        errors.extend(finding_errors)
        if finding_errors:
            invalid += 1
        redacted += redacted_count
    payload = {
        "input": str(input_path),
        "valid": not errors,
        "findings": len(findings),
        "invalid": invalid,
        "redacted": redacted,
        "errors": errors,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        if errors:
            for error in errors:
                print(f"error: {error}", file=sys.stderr)
        else:
            print(f"chat sweep valid: {input_path}")
            print(f"findings: {len(findings)}")
    return 0 if not errors else 2


def sweep_ingest(*, target: Path, surface_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    surface, _, errors = _surface_by_id(target, surface_id)
    if errors or surface is None:
        payload = {"target": str(target), "surface_id": surface_id, "valid": False, "errors": errors}
        if json_output:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            for error in errors:
                print(f"error: {error}", file=sys.stderr)
        return 2
    export_path = _resolve_target_path(
        target, _string(surface.get("export_path")), f".brigade/chat-surfaces/{surface_id}.json"
    )
    findings, load_errors = _load_export(export_path)
    sweep_payload, validate_errors, invalid, redacted = _normalized_sweep(target, surface, findings, export_path)
    errors = [*load_errors, *validate_errors]
    if errors:
        payload = {
            "target": str(target),
            "surface_id": surface_id,
            "valid": False,
            "errors": errors,
            "invalid": invalid or len(errors),
            "redacted": redacted,
        }
        if json_output:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            for error in errors:
                print(f"error: {error}", file=sys.stderr)
        return 2
    output_path = _write_sweep_output(target, surface, sweep_payload)
    payload = {
        "target": str(target),
        "surface_id": surface_id,
        "valid": True,
        "input": str(export_path),
        "output": str(output_path),
        "findings": len(findings),
        "issues": len(sweep_payload["issues"]),
        "invalid": 0,
        "redacted": redacted,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"chat sweep ingest: {surface_id}")
    print(f"input: {export_path}")
    print(f"output: {output_path}")
    print(f"issues: {len(sweep_payload['issues'])}")
    print(f"redacted: {redacted}")
    return 0


def sweep_import_issues(*, target: Path, surface_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    surface, _, errors = _surface_by_id(target, surface_id)
    if errors or surface is None:
        if json_output:
            print(
                json.dumps(
                    {"target": str(target), "surface_id": surface_id, "valid": False, "errors": errors},
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            for error in errors:
                print(f"error: {error}", file=sys.stderr)
        return 2
    output_path = _resolve_target_path(
        target, _string(surface.get("sweep_output_path")), f"{OUTPUT_ROOT_REL_PATH}/{surface_id}-latest.json"
    )
    if not output_path.is_file():
        export_path = _resolve_target_path(
            target, _string(surface.get("export_path")), f".brigade/chat-surfaces/{surface_id}.json"
        )
        findings, load_errors = _load_export(export_path)
        sweep_payload, validate_errors, invalid, redacted = _normalized_sweep(target, surface, findings, export_path)
        errors = [*load_errors, *validate_errors]
        if errors:
            if json_output:
                print(
                    json.dumps(
                        {
                            "target": str(target),
                            "surface_id": surface_id,
                            "valid": False,
                            "errors": errors,
                            "invalid": invalid or len(errors),
                            "redacted": redacted,
                        },
                        indent=2,
                        sort_keys=True,
                    )
                )
            else:
                for error in errors:
                    print(f"error: {error}", file=sys.stderr)
            return 2
        output_path = _write_sweep_output(target, surface, sweep_payload)
    from . import work_cmd

    try:
        payload = json.loads(output_path.read_text())
    except json.JSONDecodeError as exc:
        print(f"error: invalid chat memory sweep JSON: {exc}", file=sys.stderr)
        return 2
    if not isinstance(payload, dict):
        print(f"error: chat memory sweep must be an object: {output_path}", file=sys.stderr)
        return 2
    records, errors, issue_count = work_cmd._chat_sweep_records(payload, sweep_path=output_path)
    if errors:
        output = {
            "input": str(output_path),
            "imports_path": str(work_cmd._imports_path(target)),
            "valid": False,
            "errors": errors,
            "created": 0,
            "skipped": 0,
            "dismissed": 0,
            "invalid": len(errors),
            "redacted": int(payload.get("redacted", 0) or 0),
        }
        if json_output:
            print(json.dumps(output, indent=2, sort_keys=True))
        else:
            for error in errors:
                print(f"error: {error}", file=sys.stderr)
        return 2
    imported, skipped, skipped_dismissed = work_cmd._append_import_records(target, records)
    output = {
        "input": str(output_path),
        "imports_path": str(work_cmd._imports_path(target)),
        "surface_id": surface_id,
        "valid": True,
        "issues": issue_count,
        "created": len(imported),
        "imported": len(imported),
        "skipped": len(skipped),
        "skipped_duplicates": len(skipped),
        "dismissed": len(skipped_dismissed),
        "skipped_dismissed": len(skipped_dismissed),
        "invalid": 0,
        "redacted": int(payload.get("redacted", 0) or 0),
        "imports": imported,
    }
    if json_output:
        print(json.dumps(output, indent=2, sort_keys=True))
        return 0
    print(f"chat sweep import: {surface_id}")
    print(f"input: {output_path}")
    print(f"issues: {issue_count}")
    print(f"imported: {len(imported)}")
    print(f"skipped_duplicates: {len(skipped)}")
    print(f"skipped_dismissed: {len(skipped_dismissed)}")
    print(f"redacted: {output['redacted']}")
    return 0
