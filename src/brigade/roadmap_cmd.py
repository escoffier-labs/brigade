"""Roadmap completion audit and neutral inspiration pattern registry."""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from . import work_cmd

OK = "ok"
WARN = "warn"
FAIL = "fail"

PATTERN_FAMILIES: tuple[dict[str, Any], ...] = (
    {
        "id": "command-harness-patterns",
        "family": "command-harness patterns",
        "owner": "work-cli",
        "status": "covered",
        "test_hint": "CLI text and JSON command tests",
    },
    {
        "id": "delivery-loop-patterns",
        "family": "delivery-loop patterns",
        "owner": "work-closeout",
        "status": "covered",
        "test_hint": "work verification, closeout, and release tests",
    },
    {
        "id": "durable-memory-eval-patterns",
        "family": "durable memory/eval patterns",
        "owner": "handoff",
        "status": "covered",
        "test_hint": "handoff queue and memory-care tests",
    },
    {
        "id": "portable-skill-patterns",
        "family": "portable skill patterns",
        "owner": "tool-catalog",
        "status": "covered",
        "test_hint": "tool catalog, projection, contract, and policy tests",
    },
    {
        "id": "agent-security-guardrails",
        "family": "agent-security guardrails",
        "owner": "security",
        "status": "covered",
        "test_hint": "security scan and import tests",
    },
    {
        "id": "context-engineering-packs",
        "family": "context-engineering packs",
        "owner": None,
        "status": "planned",
        "test_hint": None,
    },
    {
        "id": "cross-harness-skill-plugin-sync",
        "family": "cross-harness skill/plugin sync patterns",
        "owner": "tool-catalog",
        "status": "partial",
        "test_hint": "projection tests cover explicit apply only",
    },
    {
        "id": "local-side-project-categories",
        "family": "local side-project categories",
        "owner": "repo-fleet",
        "status": "partial",
        "test_hint": "repo-fleet scan and import tests",
    },
    {
        "id": "mcp-tooling",
        "family": "MCP tooling",
        "owner": "tool-catalog",
        "status": "covered",
        "test_hint": "MCP call execution tests",
    },
    {
        "id": "portable-tools",
        "family": "portable tools",
        "owner": "tool-catalog",
        "status": "covered",
        "test_hint": "portable tool lifecycle tests",
    },
    {
        "id": "security-gates",
        "family": "security gates",
        "owner": "release",
        "status": "covered",
        "test_hint": "release readiness and security tests",
    },
    {
        "id": "self-learning",
        "family": "self-learning",
        "owner": "memory-care",
        "status": "partial",
        "test_hint": "memory-care and handoff tests",
    },
    {
        "id": "release-gates",
        "family": "release gates",
        "owner": "release",
        "status": "covered",
        "test_hint": "release readiness and candidate tests",
    },
)

DECISION_RECORDS: tuple[dict[str, Any], ...] = (
    {
        "id": "publish-guard",
        "decision": "bake-in",
        "pattern_family": "release-gates",
        "reason": "Local release checks need first-class blocker and warning receipts.",
    },
    {
        "id": "memory-maintenance",
        "decision": "integrate",
        "pattern_family": "durable-memory-eval-patterns",
        "reason": "Memory refresh belongs behind reviewed imports and handoff drafts.",
    },
    {
        "id": "prompt-and-skill-library",
        "decision": "catalog-only",
        "pattern_family": "portable-skill-patterns",
        "reason": "Catalog discovery should inspect local sources before projection or execution.",
    },
    {
        "id": "side-project-fleet",
        "decision": "move-candidate",
        "pattern_family": "local-side-project-categories",
        "reason": "Repository disposition needs local metadata and operator review.",
    },
    {
        "id": "remote-product-roadmaps",
        "decision": "leave-alone",
        "pattern_family": "delivery-loop-patterns",
        "reason": "Brigade should not implement unrelated product roadmaps.",
    },
)


def _roadmap_path(target: Path) -> Path:
    return target / "ROADMAP.md"


def _public_doc_paths(target: Path) -> list[Path]:
    paths = [target / "README.md", target / "ROADMAP.md", target / "CHANGELOG.md"]
    docs = target / "docs"
    if docs.is_dir():
        paths.extend(sorted(path for path in docs.glob("*.md") if path.is_file()))
    return [path for path in paths if path.is_file()]


def _read_text(path: Path) -> str:
    try:
        return path.read_text()
    except OSError:
        return ""


def _classify_bullet(text: str) -> str:
    lowered = text.casefold()
    if "status:" in lowered:
        if "implemented" in lowered or "complete" in lowered or "shipped" in lowered:
            return "implemented"
        if "current" in lowered:
            return "current"
        if "started" in lowered or "partial" in lowered:
            return "started"
        if "deferred" in lowered:
            return "deferred"
        if "blocked" in lowered:
            return "blocked"
    if "[x]" in lowered:
        return "implemented"
    if "[ ]" in lowered:
        return "planned"
    return "planned"


def _parse_roadmap(target: Path) -> dict[str, Any]:
    path = _roadmap_path(target)
    text = _read_text(path)
    sections: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    status_counts: dict[str, int] = {}
    if not text:
        return {
            "path": str(path),
            "exists": path.is_file(),
            "sections": sections,
            "status_counts": status_counts,
        }
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if line.startswith("#"):
            title = line.lstrip("#").strip()
            current = {"title": title, "line": line_number, "items": []}
            sections.append(current)
            continue
        if not line.startswith(("- ", "* ")):
            continue
        item_text = line[2:].strip()
        status = _classify_bullet(item_text)
        status_counts[status] = status_counts.get(status, 0) + 1
        if current is None:
            current = {"title": "Unsectioned", "line": line_number, "items": []}
            sections.append(current)
        current["items"].append({"line": line_number, "text": item_text, "status": status})
    return {
        "path": str(path),
        "exists": path.is_file(),
        "sections": sections,
        "status_counts": status_counts,
    }


def _section_stale_checks(sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for section in sections:
        title = str(section.get("title") or "")
        if "current" not in title.casefold() and "next" not in title.casefold():
            continue
        items = section.get("items") if isinstance(section.get("items"), list) else []
        if not items:
            continue
        finished = [
            item
            for item in items
            if isinstance(item, dict) and item.get("status") in {"implemented", "started"}
        ]
        ratio = len(finished) / len(items)
        if ratio >= 0.75:
            checks.append(
                {
                    "status": WARN,
                    "name": "roadmap_stale_phase_section",
                    "detail": f"{title} appears stale ({len(finished)}/{len(items)} items started or implemented)",
                    "section": title,
                }
            )
    if not checks:
        checks.append({"status": OK, "name": "roadmap_stale_phase_section", "detail": "none"})
    return checks


def _documented_brigade_commands(target: Path) -> list[str]:
    commands: set[str] = set()
    command_re = re.compile(r"\bbrigade\b(?P<tail>[^\n`]*)")
    for path in _public_doc_paths(target):
        text = _read_text(path)
        for match in command_re.finditer(text):
            words: list[str] = []
            for raw in match.group("tail").split():
                word = raw.strip("`'\"(),.:;")
                if not word or word == "brigade" or word.startswith("-") or "<" in word or ">" in word:
                    break
                if not re.fullmatch(r"[a-z0-9_-]+", word):
                    break
                words.append(word)
                if len(words) >= 4:
                    break
            if not words:
                continue
            commands.add(" ".join(["brigade", *words]))
    return sorted(commands)


def _cli_command_paths() -> list[str]:
    from . import cli

    parser = cli._build_parser()
    commands: set[str] = set()

    def walk(prefix: list[str], parser_obj: argparse.ArgumentParser) -> None:
        subparsers = [
            action
            for action in parser_obj._actions
            if isinstance(action, argparse._SubParsersAction)
        ]
        if not subparsers and prefix:
            commands.add(" ".join(["brigade", *prefix]))
            return
        for action in subparsers:
            for name, subparser in action.choices.items():
                walk([*prefix, str(name)], subparser)

    walk([], parser)
    return sorted(commands)


def audit_payload(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    roadmap = _parse_roadmap(target)
    checks: list[dict[str, Any]] = []
    if not roadmap["exists"]:
        checks.append({"status": WARN, "name": "roadmap_exists", "detail": "ROADMAP.md missing"})
    else:
        checks.append({"status": OK, "name": "roadmap_exists", "detail": roadmap["path"]})
    checks.extend(_section_stale_checks(roadmap["sections"]))

    documented = _documented_brigade_commands(target)
    cli_commands = _cli_command_paths()
    documented_set = set(documented)
    cli_set = set(cli_commands)
    missing_cli = sorted(command for command in documented_set - cli_set if "..." not in command)
    missing_docs = sorted(command for command in cli_set - documented_set)
    checks.append(
        {
            "status": WARN if missing_cli else OK,
            "name": "roadmap_documented_command_missing_cli",
            "detail": f"{len(missing_cli)} documented command(s) missing from CLI" if missing_cli else "none",
            "commands": missing_cli[:20],
        }
    )
    checks.append(
        {
            "status": WARN if missing_docs else OK,
            "name": "roadmap_cli_command_missing_docs",
            "detail": f"{len(missing_docs)} CLI command(s) missing from public docs" if missing_docs else "none",
            "commands": missing_docs[:20],
        }
    )
    issues = [check for check in checks if check.get("status") != OK]
    return {
        "target": str(target),
        "roadmap": roadmap,
        "documented_commands": documented,
        "cli_commands": cli_commands,
        "missing_cli_commands": missing_cli,
        "missing_documented_commands": missing_docs,
        "checks": checks,
        "issues": issues,
        "issue_count": len(issues),
        "top_issue": issues[0] if issues else None,
    }


def _roadmap_import_records(payload: dict[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for issue in payload.get("issues", []):
        if not isinstance(issue, dict):
            continue
        name = str(issue.get("name") or "roadmap_issue")
        detail = str(issue.get("detail") or name)
        fingerprint = work_cmd._stable_hash({"name": name, "detail": detail, "commands": issue.get("commands")})
        records.append(
            {
                "text": f"Resolve roadmap audit issue: {detail}",
                "kind": "task",
                "source": "roadmap-audit",
                "type": "docs",
                "priority": "normal",
                "template": "docs",
                "acceptance": [
                    "The roadmap audit issue is resolved or documented as deferred.",
                    "Public docs do not include private source or owner names.",
                ],
                "metadata": {
                    "issue_type": name,
                    "safe_summary": detail,
                    "source_item_key": name,
                    "source_fingerprint": fingerprint,
                },
            }
        )
    return records


def audit(*, target: Path, json_output: bool = False, import_issues: bool = False) -> int:
    payload = audit_payload(target)
    imported: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    dismissed: list[dict[str, Any]] = []
    if import_issues:
        imported, skipped, dismissed = work_cmd._append_import_records(target.expanduser().resolve(), _roadmap_import_records(payload))
        payload["imported"] = len(imported)
        payload["skipped"] = len(skipped)
        payload["dismissed"] = len(dismissed)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"roadmap audit: {payload['target']}")
    print(f"roadmap: {payload['roadmap']['path']}")
    print(f"sections: {len(payload['roadmap']['sections'])}")
    print(f"issues: {payload['issue_count']}")
    for check in payload["checks"]:
        print(f"[{check['status']}] {check['name']}: {check['detail']}")
    if import_issues:
        print(f"imported: {len(imported)}")
        print(f"skipped: {len(skipped)}")
        print(f"dismissed: {len(dismissed)}")
    return 0


def patterns_payload(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    families = [dict(item) for item in PATTERN_FAMILIES]
    decisions = [dict(item) for item in DECISION_RECORDS]
    missing_owner = [item["id"] for item in families if not item.get("owner")]
    missing_tests = [item["id"] for item in families if not item.get("test_hint")]
    checks = [
        {
            "status": WARN if missing_owner else OK,
            "name": "pattern_missing_owner",
            "detail": f"{len(missing_owner)} pattern family/families missing owner" if missing_owner else "none",
            "items": missing_owner,
        },
        {
            "status": WARN if missing_tests else OK,
            "name": "pattern_missing_tests",
            "detail": f"{len(missing_tests)} pattern family/families missing test hint" if missing_tests else "none",
            "items": missing_tests,
        },
    ]
    decision_types = {item["decision"] for item in decisions}
    required = {"bake-in", "integrate", "catalog-only", "move-candidate", "leave-alone"}
    missing_decisions = sorted(required - decision_types)
    checks.append(
        {
            "status": WARN if missing_decisions else OK,
            "name": "pattern_missing_decision_type",
            "detail": ", ".join(missing_decisions) if missing_decisions else "none",
            "items": missing_decisions,
        }
    )
    issues = [check for check in checks if check["status"] != OK]
    return {
        "target": str(target),
        "families": families,
        "decisions": decisions,
        "checks": checks,
        "issues": issues,
        "issue_count": len(issues),
        "top_issue": issues[0] if issues else None,
    }


def patterns(*, target: Path, json_output: bool = False) -> int:
    payload = patterns_payload(target)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"roadmap patterns: {payload['target']}")
    print(f"families: {len(payload['families'])}")
    print(f"decisions: {len(payload['decisions'])}")
    for family in payload["families"]:
        owner = family.get("owner") or "unassigned"
        print(f"- {family['id']} [{family['status']}] owner={owner}")
    for check in payload["checks"]:
        if check["status"] != OK:
            print(f"[{check['status']}] {check['name']}: {check['detail']}")
    return 0


def health(target: Path) -> dict[str, Any]:
    audit_data = audit_payload(target)
    pattern_data = patterns_payload(target)
    checks = [
        *audit_data.get("issues", []),
        *pattern_data.get("issues", []),
    ]
    return {
        "target": str(target.expanduser().resolve()),
        "audit": {
            "issue_count": audit_data["issue_count"],
            "top_issue": audit_data["top_issue"],
        },
        "patterns": {
            "issue_count": pattern_data["issue_count"],
            "top_issue": pattern_data["top_issue"],
        },
        "checks": checks,
        "issue_count": len(checks),
        "top_issue": checks[0] if checks else None,
    }
