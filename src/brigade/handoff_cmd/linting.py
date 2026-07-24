"""Handoff health checks shared by CLI doctors."""
# ruff: noqa: E402,F401,F403,F811,F821

from __future__ import annotations

import json
import hashlib
import re
import sys
import time
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import scrub
from ..budgets import HANDOFF_BACKLOG_STALE_SECONDS
from ..config import load_config as load_brigade_config
from ..localio import write_json as _write_json
from ..selection import WRITER_INBOXES as _WRITER_INBOX_MAP

from . import models as _family_base

globals().update({name: value for name, value in vars(_family_base).items() if not name.startswith("__")})


def _injection_hit_dict(hit: Any) -> dict[str, Any]:
    return {
        "line": hit.line,
        "severity": hit.severity,
        "rule": hit.rule,
        "excerpt": hit.excerpt,
    }


def _injection_messages(hits: tuple[Any, ...]) -> tuple[str, ...]:
    messages: list[str] = []
    for hit in hits:
        prefix = "info" if hit.severity == "info" else "warning"
        messages.append(f"line {hit.line}: {prefix}: [{hit.rule}] {hit.excerpt}")
    return tuple(messages)


def _read_handoff_text(path: Path) -> str | None:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return None


def lint(
    *,
    target: Path,
    paths: list[Path] | None = None,
    content_guard: bool = False,
    guard_policy: str = "personal",
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    from ..untrusted import scan_handoff_injection_heuristics, scan_untrusted

    results = lint_targets(target, paths=paths)
    guard_results: list[dict[str, Any]] = []
    if content_guard:
        for result in results:
            guard_item = _guard_handoff_path(result.path, target=target, policy=guard_policy)
            text = _read_handoff_text(result.path)
            hits = scan_handoff_injection_heuristics(text or "") if text is not None else ()
            guard_item["injection_heuristics"] = [_injection_hit_dict(hit) for hit in hits]
            guard_item["injection_warning_count"] = len([hit for hit in hits if hit.severity == "warning"])
            guard_results.append(guard_item)
    guard_ok = all(item.get("exit_code") == 0 for item in guard_results)
    injection_counts: dict[str, int] = {}
    injection_hits_by_path: dict[str, tuple[Any, ...]] = {}
    enriched_results: list[HandoffLintResult] = []
    for result in results:
        text = _read_handoff_text(result.path)
        if text is None:
            enriched_results.append(result)
            continue
        hits = scan_handoff_injection_heuristics(text)
        injection_hits_by_path[str(result.path)] = hits
        signal = scan_untrusted(text)
        if signal.flagged:
            injection_counts[str(result.path)] = signal.count
        injection_messages = _injection_messages(hits) if content_guard or hits else ()
        if not injection_messages and signal.flagged:
            injection_messages = (
                f"line ?: warning: [{signal.count} prompt-injection signal(s); see `brigade security scan`]",
            )
        enriched_results.append(
            HandoffLintResult(
                path=result.path,
                action=result.action,
                valid=result.valid,
                errors=result.errors,
                warnings=result.warnings + injection_messages,
                hints=result.hints,
            )
        )
    results = tuple(enriched_results)
    result_dicts = []
    for result in results:
        row = result.as_dict()
        path_key = str(result.path)
        row["injection_signals"] = injection_counts.get(path_key, 0)
        hits = injection_hits_by_path.get(path_key, ())
        row["injection_heuristics"] = [_injection_hit_dict(hit) for hit in hits]
        result_dicts.append(row)
    payload = {
        "target": str(target),
        "count": len(results),
        "valid": all(result.valid for result in results) and guard_ok,
        "injection_flagged_count": len(injection_counts),
        "results": result_dicts,
        "content_guard": guard_results,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["valid"] else 1
    print(f"handoff lint: {target}")
    print(f"files: {len(results)}")
    for result in results:
        status = OK if result.valid else FAIL
        action = f" ({result.action})" if result.action else ""
        print(f"[{status}] {result.path}{action}")
        for error in result.errors:
            print(f"  - {error}")
        for hint in result.hints:
            print(f"  hint: {hint}")
        for warning in result.warnings:
            if warning.startswith("line "):
                print(f"  {warning}")
            else:
                print(f"  warning: {warning}")
    if content_guard:
        print(f"content_guard_policy: {guard_policy} (leak scan + injection heuristics)")
        for item in guard_results:
            leak_status = OK if item.get("exit_code") == 0 else FAIL
            print(f"[{leak_status}] content_guard leaks: {item.get('path')} {item.get('detail')}")
            warning_count = int(item.get("injection_warning_count") or 0)
            if warning_count:
                print(f"  warning: {warning_count} injection heuristic hit(s)")
            for hit in item.get("injection_heuristics") or []:
                if hit.get("severity") == "info":
                    print(f"  line {hit['line']}: info: [{hit['rule']}] {hit['excerpt']}")
                elif hit.get("severity") == "warning":
                    print(f"  line {hit['line']}: warning: [{hit['rule']}] {hit['excerpt']}")
    return 0 if payload["valid"] else 1


def _guard_handoff_path(path: Path, *, target: Path, policy: str) -> dict[str, Any]:
    result = scrub.run_scan(path, repo_target=target, policy=policy)
    stdout_summary = _short(" ".join(str(result.get("stdout") or "").split()), 320)
    stderr_summary = _short(" ".join(str(result.get("stderr") or "").split()), 320)
    return {
        "path": str(path),
        "policy": policy,
        "available": bool(result.get("available")),
        "exit_code": result.get("exit_code"),
        "status": result.get("status"),
        "detail": result.get("detail"),
        "stdout_summary": stdout_summary,
        "stderr_summary": stderr_summary,
    }


def lint_targets(target: Path, paths: list[Path] | None = None) -> tuple[HandoffLintResult, ...]:
    target = target.expanduser().resolve()
    candidates = tuple(_resolve_lint_path(target, path) for path in paths) if paths else _pending_handoff_paths(target)
    return tuple(lint_file(path) for path in candidates)


def lint_file(path: Path) -> HandoffLintResult:
    path = path.expanduser().resolve()
    errors: list[str] = []
    warnings: list[str] = []
    hints: list[str] = []
    action: str | None = None
    try:
        text = path.read_text(errors="replace")
    except OSError as exc:
        return HandoffLintResult(
            path=path,
            action=None,
            valid=False,
            errors=(f"cannot read handoff file: {exc}",),
            warnings=(),
        )

    sections = _parse_markdown_sections(text)
    for required in ("Type", "Title", "Summary", "Recommended memory action"):
        if required not in sections or not _section_value(sections, required):
            errors.append(f"missing required section: {required}")

    if any(error.startswith("missing required section:") for error in errors):
        _, migration_gaps = _migrate_extract(text)
        if not migration_gaps:
            from ..untrusted import scan_untrusted

            if not scan_untrusted(text).flagged:
                hints.append("this looks like a freeform note; try `brigade handoff migrate --target .`")

    action_value = _section_value(sections, "Recommended memory action")
    if action_value:
        action = action_value.splitlines()[0].strip().casefold()
        if action not in HANDOFF_ACTIONS:
            errors.append("Recommended memory action must be one of: " + ", ".join(HANDOFF_ACTIONS))

    if action in CARD_ACTIONS:
        _lint_card_action(sections, errors, warnings)
    elif action == NO_CARD_ACTION:
        _lint_no_card_action(sections, errors)

    return HandoffLintResult(
        path=path,
        action=action,
        valid=not errors,
        errors=tuple(errors),
        warnings=tuple(warnings),
        hints=tuple(hints),
    )


def _loose_field(text: str, name: str) -> str | None:
    """Extract `- Name: value` / `Name: value` style metadata from a homegrown note."""
    match = re.search(_LOOSE_FIELD_TEMPLATE % re.escape(name), text, re.IGNORECASE | re.MULTILINE)
    return match.group(1).strip() if match else None


def _migrate_extract(text: str) -> tuple[dict[str, str], list[str]]:
    """Merge proper `## Section` values with loose bullet metadata; report gaps."""
    sections = _parse_markdown_sections(text)

    def field(section_name: str) -> str:
        return _section_value(sections, section_name) or _loose_field(text, section_name) or ""

    action_raw = field("Recommended memory action")
    extracted = {
        "type": field("Type"),
        "title": field("Title"),
        "summary": field("Summary"),
        "action": (action_raw.splitlines() or [""])[0].strip().casefold(),
        "target_card": field("Target card"),
        "target_document": field("Target document"),
        "card_content": _section_value(sections, "Suggested card content"),
        "document_content": _section_value(sections, "Suggested document content"),
    }
    missing: list[str] = []
    for key in ("type", "title", "summary"):
        if not extracted[key]:
            missing.append(key)
    if extracted["action"] not in HANDOFF_ACTIONS:
        missing.append("recommended memory action")
    elif extracted["action"] in CARD_ACTIONS:
        if not extracted["target_card"]:
            missing.append("target card")
        if not extracted["card_content"]:
            missing.append("suggested card content")
    else:
        if not extracted["target_document"]:
            missing.append("target document")
        if not extracted["document_content"]:
            missing.append("suggested document content")
    return extracted, missing
