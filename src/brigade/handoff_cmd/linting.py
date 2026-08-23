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
from .readability import ReadabilityFinding, scan_standalone_readability

globals().update({name: value for name, value in vars(_family_base).items() if not name.startswith("__")})


def _readability_kind(category: str) -> str:
    return "relative date" if category == "relative-date" else "unresolved reference"


def _readability_warning(finding: ReadabilityFinding) -> str:
    kind = _readability_kind(finding.category)
    return (
        f"line {finding.line}: warning: [readability/{finding.category}] "
        f'{finding.section}: {kind} ("{finding.match}"): {finding.suggestion}'
    )


def _readability_error(finding: ReadabilityFinding) -> str:
    kind = _readability_kind(finding.category)
    return (
        f"line {finding.line}: [readability/{finding.category}] "
        f'{finding.section}: {kind} ("{finding.match}"): {finding.suggestion}'
    )


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


def _egress_verdict(exit_code: Any) -> str:
    return OK if exit_code == 0 else FAIL


def _injection_verdict(hits: tuple[Any, ...]) -> str:
    return FAIL if any(hit.severity == "warning" for hit in hits) else OK


def _injection_detail(*, warning_count: int, unscanned: bool = False) -> str:
    if unscanned:
        return "unscanned"
    if warning_count:
        label = "warning" if warning_count == 1 else "warnings"
        return f"{warning_count} injection {label}"
    return "clean"


def _read_handoff_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def lint(
    *,
    target: Path,
    paths: list[Path] | None = None,
    content_guard: bool = False,
    guard_policy: str = "personal",
    json_output: bool = False,
    strict: bool = False,
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
            if text is None:
                guard_item["injection_heuristics"] = []
                guard_item["injection_warning_count"] = 0
                guard_item["injection_unscanned"] = True
                guard_item["egress_verdict"] = _egress_verdict(guard_item.get("exit_code"))
                guard_item["injection_verdict"] = FAIL
            else:
                hits = scan_handoff_injection_heuristics(text)
                guard_item["injection_heuristics"] = [_injection_hit_dict(hit) for hit in hits]
                guard_item["injection_warning_count"] = len([hit for hit in hits if hit.severity == "warning"])
                guard_item["egress_verdict"] = _egress_verdict(guard_item.get("exit_code"))
                guard_item["injection_verdict"] = _injection_verdict(hits)
            guard_results.append(guard_item)
    guard_ok = all(item.get("egress_verdict") == OK and item.get("injection_verdict") == OK for item in guard_results)
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
                readability=result.readability,
                salvageable=result.salvageable,
            )
        )
    if strict:
        strict_results: list[HandoffLintResult] = []
        for result in enriched_results:
            if not result.readability:
                strict_results.append(result)
                continue
            promoted = tuple(_readability_error(finding) for finding in result.readability)
            strict_results.append(
                HandoffLintResult(
                    path=result.path,
                    action=result.action,
                    valid=False,
                    errors=result.errors + promoted,
                    warnings=result.warnings,
                    hints=result.hints,
                    readability=result.readability,
                    salvageable=result.salvageable,
                )
            )
        enriched_results = strict_results
    results = tuple(enriched_results)
    readability_flagged_count = sum(1 for result in results if result.readability)
    result_dicts = []
    for result in results:
        row = result.as_dict()
        path_key = str(result.path)
        row["injection_signals"] = injection_counts.get(path_key, 0)
        hits = injection_hits_by_path.get(path_key, ())
        row["injection_heuristics"] = [_injection_hit_dict(hit) for hit in hits]
        result_dicts.append(row)
    payload: dict[str, Any] = {
        "target": str(target),
        "count": len(results),
        "valid": all(result.valid for result in results) and guard_ok,
        "injection_flagged_count": len(injection_counts),
        "readability_flagged_count": readability_flagged_count,
        "strict": strict,
        "results": result_dicts,
        "content_guard": guard_results,
    }
    if content_guard:
        payload["content_guard_egress"] = (
            OK if all(item.get("egress_verdict") == OK for item in guard_results) else FAIL
        )
        payload["content_guard_injection"] = (
            OK if all(item.get("injection_verdict") == OK for item in guard_results) else FAIL
        )
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
    if readability_flagged_count:
        finding_count = sum(len(result.readability) for result in results)
        suffix = "(--strict: failing)" if strict else "(--strict to fail)"
        print(
            f"readability: {finding_count} standalone-readability warning(s) "
            f"in {readability_flagged_count} file(s) {suffix}"
        )
    if content_guard:
        print(f"content_guard_policy: {guard_policy} (egress leak scan + injection heuristics)")
        for item in guard_results:
            hits = item.get("injection_heuristics") or []
            egress_status = item.get("egress_verdict", OK)
            print(f"[{egress_status}] content_guard egress: {item.get('path')} {item.get('detail')}")
            injection_status = item.get("injection_verdict", OK)
            warning_count = int(item.get("injection_warning_count") or 0)
            unscanned = bool(item.get("injection_unscanned"))
            print(
                f"[{injection_status}] content_guard injection: {item.get('path')} "
                f"{_injection_detail(warning_count=warning_count, unscanned=unscanned)}"
            )
            for hit in hits:
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


def lint_targets(
    target: Path, paths: list[Path] | None = None, sources: Path | None = None
) -> tuple[HandoffLintResult, ...]:
    target = target.expanduser().resolve()
    candidates = (
        tuple(_resolve_lint_path(target, path) for path in paths)
        if paths
        else _pending_handoff_paths(target, sources=sources)
    )
    return tuple(lint_file(path) for path in candidates)


def lint_file(path: Path) -> HandoffLintResult:
    path = path.expanduser().resolve()
    errors: list[str] = []
    warnings: list[str] = []
    hints: list[str] = []
    action: str | None = None
    salvageable = False
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return HandoffLintResult(
            path=path,
            action=None,
            valid=False,
            errors=(f"cannot read handoff file: {exc}",),
            warnings=(),
        )

    raw_sections = _parse_markdown_sections(text)
    # Use the same structural boundary as ingest: a note with several populated
    # Markdown sections can be preserved for review, unlike unstructured text.
    from ..ingest import has_salvageable_structure

    salvageable = has_salvageable_structure(raw_sections)
    sections, collision_errors, noncanonical_headings = _canonicalize_sections(raw_sections)
    errors.extend(collision_errors)
    heading_warnings, heading_hints = _lint_noncanonical_heading_messages(noncanonical_headings)
    warnings.extend(heading_warnings)
    hints.extend(heading_hints)
    order_warnings, order_hints = _lint_section_order_messages(raw_sections)
    warnings.extend(order_warnings)
    hints.extend(order_hints)
    for required in ("Type", "Title", "Summary", "Recommended memory action"):
        if required not in sections or not _section_value(sections, required):
            errors.append(f"missing required section: {required}")

    if any(error.startswith("missing required section:") for error in errors):
        _, migration_gaps = _migrate_extract(text)
        if not migration_gaps:
            from ..untrusted import scan_untrusted

            if not scan_untrusted(text).flagged:
                hints.append("this looks like a freeform note; try `brigade handoff migrate --target .`")
        elif salvageable:
            from ..untrusted import scan_untrusted

            if not scan_untrusted(text).flagged:
                expected = ", ".join(f"## {name}" for name in CANONICAL_HANDOFF_SECTION_HEADINGS)
                hints.append(f"structured note detected; use Brigade handoff sections: {expected}")

    action_value = _section_value(sections, "Recommended memory action")
    if action_value:
        action = action_value.splitlines()[0].strip().casefold()
        if action not in HANDOFF_ACTIONS:
            errors.append("Recommended memory action must be one of: " + ", ".join(HANDOFF_ACTIONS))

    if action in CARD_ACTIONS:
        _lint_card_action(sections, errors, warnings)
    elif action == NO_CARD_ACTION:
        _lint_no_card_action(sections, errors)

    readability = scan_standalone_readability(text)
    warnings.extend(_readability_warning(finding) for finding in readability)

    return HandoffLintResult(
        path=path,
        action=action,
        valid=not errors,
        errors=tuple(errors),
        warnings=tuple(warnings),
        hints=tuple(hints),
        readability=readability,
        salvageable=salvageable,
    )


def _loose_field(text: str, name: str) -> str | None:
    """Extract `- Name: value` / `Name: value` style metadata from a homegrown note."""
    match = re.search(_LOOSE_FIELD_TEMPLATE % re.escape(name), text, re.IGNORECASE | re.MULTILINE)
    return match.group(1).strip() if match else None


def _migrate_extract(text: str) -> tuple[dict[str, str], list[str]]:
    """Merge proper `## Section` values with loose bullet metadata; report gaps."""
    raw_sections = _parse_markdown_sections(text)
    sections, _collision_errors, _ = _canonicalize_sections(raw_sections)

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
