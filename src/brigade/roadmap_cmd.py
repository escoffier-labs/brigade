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
DOC_COMMAND_TOP_LEVELS = {
    "add",
    "center",
    "chat",
    "context",
    "doctor",
    "dogfood",
    "handoff",
    "handoff-template",
    "hermes-fragments",
    "ingest",
    "init",
    "learn",
    "memory",
    "openclaw-fragments",
    "projects",
    "reconfigure",
    "release",
    "repos",
    "roadmap",
    "roster",
    "run",
    "runs",
    "scrub",
    "security",
    "status",
    "tools",
    "work",
}
COMMAND_INVENTORY_RELATIVE_PATH = Path("docs") / "command-inventory.md"
ROADMAP_ARCHIVE_RELATIVE_PATH = Path("docs") / "roadmap-archive.md"

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
        "owner": "context",
        "status": "covered",
        "test_hint": "context pack plan, build, show, and archive tests",
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

ACTIVE_ROADMAP_QUEUE_ITEMS: tuple[dict[str, Any], ...] = ()

ARCHIVED_ROADMAP_ITEMS: tuple[dict[str, Any], ...] = (
    {
        "id": "deeper-roadmap-ownership-modeling",
        "title": "Deeper roadmap ownership modeling",
        "subsystem": "roadmap",
        "owner": "roadmap",
        "source_section": "Roadmap State Audit And Closure Map",
        "closed_phase": 62,
        "archive_reason": "Closed for the completion-hardening queue with explicit deferred ownership records; richer roadmap workflow state belongs in the next roadmap.",
        "status": "carried-forward",
    },
    {
        "id": "private-pattern-source-aliases",
        "title": "Private pattern source aliases from local config",
        "subsystem": "roadmap",
        "owner": "roadmap",
        "source_section": "Inspiration Pattern Registry",
        "closed_phase": 62,
        "archive_reason": "Closed for public hardening by keeping exact private reference names out of public docs and using neutral pattern families.",
        "status": "implemented",
    },
    {
        "id": "outbound-backup-status-messages",
        "title": "Outbound backup operator status messages",
        "subsystem": "backup-health",
        "owner": "backup",
        "source_section": "Backup And Recovery Closure",
        "closed_phase": None,
        "archive_reason": "Closed as out of scope for the local read-only operator loop; outbound notification behavior belongs behind an explicit future surface.",
        "status": "out-of-scope",
    },
    {
        "id": "context-harness-destination-writes",
        "title": "Context pack writes into harness destinations",
        "subsystem": "context",
        "owner": "context",
        "source_section": "Context Engineering Packs",
        "closed_phase": 70,
        "archive_reason": "Closed for the foundation by shipping read-only sync plans and receipts; destination writes require a future explicit apply command.",
        "status": "carried-forward",
    },
    {
        "id": "repo-shareable-workflow-rule-templates",
        "title": "Repo-shareable workflow rule templates",
        "subsystem": "install",
        "owner": "templates",
        "source_section": "Issue And TDD Loop Closure",
        "closed_phase": 79,
        "archive_reason": "Closed with public-safe repo templates and work doctor visibility.",
        "status": "implemented",
    },
    {
        "id": "stale-issue-repair-imports",
        "title": "Stale active issue repair imports",
        "subsystem": "work",
        "owner": "work",
        "source_section": "Issue And TDD Loop Closure",
        "closed_phase": 80,
        "archive_reason": "Closed with local repair imports for stale or unreadable issue-backed task context.",
        "status": "implemented",
    },
    {
        "id": "cross-producer-provenance-audit",
        "title": "Cross-producer provenance audits across historical sources",
        "subsystem": "work-inbox",
        "owner": "work",
        "source_section": "Scanner And Inbox Closure",
        "closed_phase": 64,
        "archive_reason": "Closed with work import provenance checks and inbox doctor provenance contract warnings.",
        "status": "implemented",
    },
    {
        "id": "expanded-chat-export-parsers",
        "title": "Expanded chat export provider aliases and parser fixtures",
        "subsystem": "chat-surfaces",
        "owner": "chat",
        "source_section": "Chat Surface Export Completion",
        "closed_phase": 66,
        "archive_reason": "Closed with provider alias normalization, starter surfaces, JSONL fixtures, sweep review, task promotion, and handoff promotion.",
        "status": "implemented",
    },
    {
        "id": "tool-projection-parity-closeout",
        "title": "Separate tool projection parity closeout receipt",
        "subsystem": "tool-catalog",
        "owner": "tools",
        "source_section": "Shared Tool Catalog Completion",
        "closed_phase": 68,
        "archive_reason": "Closed with tools parity status and closeout receipts, doctor and brief integration, and changed-fingerprint resurfacing.",
        "status": "implemented",
    },
    {
        "id": "learning-accepted-risk-quieting",
        "title": "Rich accepted-risk quieting across learning sources",
        "subsystem": "learning",
        "owner": "learn",
        "source_section": "Self-Learning Loop Closure",
        "closed_phase": 74,
        "archive_reason": "Closed with learning closeout records for accepted-risk, dismissed, archived, and deferred outcomes.",
        "status": "implemented",
    },
    {
        "id": "security-sarif-output",
        "title": "Dependency-free security SARIF output",
        "subsystem": "security",
        "owner": "security",
        "source_section": "Security Plugin Closure",
        "closed_phase": 76,
        "archive_reason": "Closed with dependency-free SARIF 2.1.0 output in security scan bundles and `brigade security sarif` regeneration.",
        "status": "implemented",
    },
    {
        "id": "safe-memory-autofix-planning",
        "title": "Safe memory-care autofix planning",
        "subsystem": "memory-care",
        "owner": "memory",
        "source_section": "Memory And Handoff Closure",
        "closed_phase": 83,
        "archive_reason": "Closed with mutation-free `brigade memory care plan-fixes` planning and blocked-plan reporting.",
        "status": "implemented",
    },
    {
        "id": "recursive-repo-root-discovery",
        "title": "Safe repo root discovery from configured roots",
        "subsystem": "repo-fleet",
        "owner": "repos",
        "source_section": "Repository Fleet Readiness",
        "closed_phase": 93,
        "archive_reason": "Closed with dry-run `brigade repos discover plan`, configured-root parsing, safe candidate labels, include/exclude/max-depth handling, and path redaction.",
        "status": "implemented",
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


def _command_inventory_path(target: Path) -> Path:
    return target / COMMAND_INVENTORY_RELATIVE_PATH


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


def _commands_from_text(text: str) -> set[str]:
    commands: set[str] = set()
    command_re = re.compile(r"\bbrigade\b(?P<tail>\s+[^\n`]*)")

    def add_command(raw_command: str, *, require_known_head: bool = False) -> None:
        match = command_re.search(raw_command)
        if not match:
            return
        words: list[str] = []
        for raw in match.group("tail").split():
            word = raw.strip("`'\"(),.:;")
            if not word or word == "brigade" or word.startswith("-") or "<" in word or ">" in word:
                break
            if not re.fullmatch(r"[a-z0-9_-]+", word):
                break
            words.append(word)
            if len(words) >= 5:
                break
        if words:
            if require_known_head and words[0] not in DOC_COMMAND_TOP_LEVELS:
                return
            commands.add(" ".join(["brigade", *words]))

    for match in re.finditer(r"`([^\n`]*\bbrigade\b[^\n`]*)`", text):
        add_command(match.group(1))
    in_fence = False
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence and stripped.startswith("brigade "):
            add_command(stripped, require_known_head=True)
    return commands


def _documented_brigade_commands(target: Path) -> list[str]:
    commands: set[str] = set()
    for path in _public_doc_paths(target):
        commands.update(_commands_from_text(_read_text(path)))
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


def _cli_command_prefixes(commands: list[str]) -> set[str]:
    prefixes: set[str] = set()
    for command in commands:
        parts = command.split()
        for index in range(2, len(parts) + 1):
            prefixes.add(" ".join(parts[:index]))
    return prefixes


def _normalize_documented_command(command: str, known_prefixes: set[str]) -> str:
    parts = command.split()
    for length in range(len(parts), 1, -1):
        candidate = " ".join(parts[:length])
        if candidate in known_prefixes:
            return candidate
    return command


def _active_queue_items() -> list[dict[str, Any]]:
    return [dict(item) for item in ACTIVE_ROADMAP_QUEUE_ITEMS]


def _archived_items() -> list[dict[str, Any]]:
    return [dict(item) for item in ARCHIVED_ROADMAP_ITEMS]


def _deferred_item_checks(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    missing_owner = [item["id"] for item in items if not item.get("owner")]
    missing_phase = [item["id"] for item in items if item.get("status") != "out-of-scope" and not item.get("suggested_phase")]
    return [
        {
            "status": WARN if missing_owner else OK,
            "name": "roadmap_deferred_missing_owner",
            "detail": f"{len(missing_owner)} deferred item(s) missing owner" if missing_owner else "none",
            "items": missing_owner,
        },
        {
            "status": WARN if missing_phase else OK,
            "name": "roadmap_deferred_missing_phase",
            "detail": f"{len(missing_phase)} deferred item(s) missing suggested phase" if missing_phase else "none",
            "items": missing_phase,
        },
    ]


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
    cli_prefixes = _cli_command_prefixes(cli_commands)
    normalized_documented = sorted(
        {_normalize_documented_command(command, cli_prefixes) for command in documented}
    )
    documented_set = set(normalized_documented)
    cli_set = set(cli_commands)
    missing_cli = sorted(
        command
        for command in documented
        if "..." not in command and _normalize_documented_command(command, cli_prefixes) not in cli_prefixes
    )
    missing_docs = sorted(command for command in cli_set if command not in documented_set)
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
    command_contract = command_contract_payload(target)
    inventory_check = next(
        (check for check in command_contract["checks"] if check.get("name") == "roadmap_command_inventory_current"),
        None,
    )
    if inventory_check:
        checks.append(dict(inventory_check))
    deferred_items = _active_queue_items()
    archived_items = _archived_items()
    checks.extend(_deferred_item_checks(deferred_items))
    issues = [check for check in checks if check.get("status") != OK]
    return {
        "target": str(target),
        "roadmap": roadmap,
        "deferred_items": deferred_items,
        "deferred_item_count": len(deferred_items),
        "active_queue_items": deferred_items,
        "active_queue_item_count": len(deferred_items),
        "archived_items": archived_items,
        "archived_item_count": len(archived_items),
        "archive_path": str(target / ROADMAP_ARCHIVE_RELATIVE_PATH),
        "documented_commands": documented,
        "normalized_documented_commands": normalized_documented,
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
    print(f"deferred_items: {payload['deferred_item_count']}")
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


def archive_payload(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    items = _archived_items()
    checks = [
        {
            "status": OK if (target / ROADMAP_ARCHIVE_RELATIVE_PATH).is_file() else WARN,
            "name": "roadmap_archive_doc_exists",
            "detail": ROADMAP_ARCHIVE_RELATIVE_PATH.as_posix()
            if (target / ROADMAP_ARCHIVE_RELATIVE_PATH).is_file()
            else f"{ROADMAP_ARCHIVE_RELATIVE_PATH.as_posix()} missing",
        },
    ]
    missing_reason = [item["id"] for item in items if not item.get("archive_reason")]
    checks.append(
        {
            "status": WARN if missing_reason else OK,
            "name": "roadmap_archive_missing_reason",
            "detail": f"{len(missing_reason)} archived item(s) missing reason" if missing_reason else "none",
            "items": missing_reason,
        }
    )
    issues = [check for check in checks if check["status"] != OK]
    return {
        "target": str(target),
        "archive_path": str(target / ROADMAP_ARCHIVE_RELATIVE_PATH),
        "archive_relative_path": ROADMAP_ARCHIVE_RELATIVE_PATH.as_posix(),
        "archived_items": items,
        "archived_item_count": len(items),
        "checks": checks,
        "issues": issues,
        "issue_count": len(issues),
        "top_issue": issues[0] if issues else None,
    }


def archive(*, target: Path, json_output: bool = False) -> int:
    payload = archive_payload(target)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"roadmap archive: {payload['target']}")
    print(f"archive: {payload['archive_relative_path']}")
    print(f"archived_items: {payload['archived_item_count']}")
    for item in payload["archived_items"]:
        phase = item.get("closed_phase") or "n/a"
        print(f"- {item['id']} [{item['status']}] phase={phase}: {item['title']}")
    for check in payload["checks"]:
        if check["status"] != OK:
            print(f"[{check['status']}] {check['name']}: {check['detail']}")
    return 0


def command_contract_payload(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    documented = _documented_brigade_commands(target)
    cli_commands = _cli_command_paths()
    cli_prefixes = _cli_command_prefixes(cli_commands)
    normalized_documented = sorted(
        {_normalize_documented_command(command, cli_prefixes) for command in documented}
    )
    top_level_names = sorted({command.split()[1] for command in cli_commands if len(command.split()) > 1})
    groups: list[dict[str, Any]] = []
    missing_groups: list[str] = []
    for name in top_level_names:
        command = f"brigade {name}"
        documented_paths = [
            item
            for item in normalized_documented
            if item == command or item.startswith(f"{command} ")
        ]
        documented_group = command in normalized_documented or bool(documented_paths)
        if not documented_group:
            missing_groups.append(command)
        groups.append(
            {
                "command": command,
                "documented": documented_group,
                "documented_paths": documented_paths,
                "cli_path_count": sum(1 for item in cli_commands if item == command or item.startswith(f"{command} ")),
            }
        )
    expected_inventory = _command_inventory_markdown(
        groups=groups,
        cli_commands=cli_commands,
    )
    inventory_path = _command_inventory_path(target)
    existing_inventory = _read_text(inventory_path) if inventory_path.is_file() else ""
    inventory_current = existing_inventory == expected_inventory
    checks = [
        {
            "status": WARN if missing_groups else OK,
            "name": "roadmap_command_group_missing_docs",
            "detail": f"{len(missing_groups)} top-level command group(s) missing public docs" if missing_groups else "none",
            "commands": missing_groups,
        },
        {
            "status": OK if inventory_current else WARN,
            "name": "roadmap_command_inventory_current",
            "detail": f"{COMMAND_INVENTORY_RELATIVE_PATH.as_posix()} is current" if inventory_current else f"{COMMAND_INVENTORY_RELATIVE_PATH.as_posix()} missing or stale",
            "path": COMMAND_INVENTORY_RELATIVE_PATH.as_posix(),
        }
    ]
    issues = [check for check in checks if check["status"] != OK]
    return {
        "target": str(target),
        "documented_commands": documented,
        "normalized_documented_commands": normalized_documented,
        "cli_commands": cli_commands,
        "groups": groups,
        "group_count": len(groups),
        "inventory_path": str(inventory_path),
        "inventory_relative_path": COMMAND_INVENTORY_RELATIVE_PATH.as_posix(),
        "inventory_exists": inventory_path.is_file(),
        "inventory_current": inventory_current,
        "expected_inventory": expected_inventory,
        "checks": checks,
        "issues": issues,
        "issue_count": len(issues),
        "top_issue": issues[0] if issues else None,
    }


def _command_inventory_markdown(*, groups: list[dict[str, Any]], cli_commands: list[str]) -> str:
    lines = [
        "# Brigade Command Inventory",
        "",
        "This file is generated from the Brigade CLI parser.",
        "",
        "![Command inventory is generated from the CLI parser](assets/command-inventory-flow.svg)",
        "",
        "Regenerate with:",
        "",
        "```bash",
        "brigade roadmap commands --write",
        "```",
        "",
        "## Command Groups",
        "",
    ]
    for group in groups:
        lines.append(f"- `{group['command']}`: {group['cli_path_count']} command path(s)")
    lines.extend(["", "## Commands", ""])
    for command in cli_commands:
        lines.append(f"- `{command}`")
    lines.append("")
    return "\n".join(lines)


def commands(*, target: Path, json_output: bool = False, write_inventory: bool = False, check_inventory: bool = False) -> int:
    payload = command_contract_payload(target)
    if write_inventory:
        inventory_path = Path(payload["inventory_path"])
        inventory_path.parent.mkdir(parents=True, exist_ok=True)
        inventory_path.write_text(payload["expected_inventory"])
        payload = command_contract_payload(target)
    if json_output:
        output_payload = dict(payload)
        output_payload.pop("expected_inventory", None)
        print(json.dumps(output_payload, indent=2, sort_keys=True))
        return 1 if check_inventory and not payload["inventory_current"] else 0
    print(f"roadmap commands: {payload['target']}")
    print(f"groups: {payload['group_count']}")
    print(f"inventory: {payload['inventory_relative_path']} ({'current' if payload['inventory_current'] else 'stale'})")
    for group in payload["groups"]:
        status = OK if group["documented"] else WARN
        print(f"[{status}] {group['command']}: {group['cli_path_count']} CLI path(s)")
    for check in payload["checks"]:
        if check["status"] != OK:
            print(f"[{check['status']}] {check['name']}: {check['detail']}")
    return 1 if check_inventory and not payload["inventory_current"] else 0


def health(target: Path) -> dict[str, Any]:
    audit_data = audit_payload(target)
    pattern_data = patterns_payload(target)
    command_data = command_contract_payload(target)
    archive_data = archive_payload(target)
    checks = [
        *audit_data.get("issues", []),
        *pattern_data.get("issues", []),
        *command_data.get("issues", []),
        *archive_data.get("issues", []),
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
        "commands": {
            "issue_count": command_data["issue_count"],
            "top_issue": command_data["top_issue"],
        },
        "archive": {
            "issue_count": archive_data["issue_count"],
            "top_issue": archive_data["top_issue"],
            "archived_item_count": archive_data["archived_item_count"],
        },
        "checks": checks,
        "issue_count": len(checks),
        "top_issue": checks[0] if checks else None,
    }
