"""Agent-facing doctor findings: omit passes, emit actionable structured output.

Issue #734: ``brigade doctor --agent`` (composes with ``--json``) prints a one-line
pass/finding count, then only actionable findings with severity, explanation,
observed, expected, and remediation commands.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from .doctor import (
    ACTIONABLE_STATUSES,
    FAIL,
    INFO,
    MANUAL,
    OK,
    OUTCOME_LEDGER_STALE_DAYS,
    WARN,
    CheckResult,
    ScopedCheckResult,
    _annotate_target_detail,
    _normalize_scoped_check,
    _status_counts,
)

_SEVERITY_LABEL = {
    FAIL: "error",
    WARN: "warn",
    MANUAL: "manual",
}

_HARNESS_CHECK_PREFIXES: tuple[tuple[str, str], ...] = (
    ("openclaw:", "openclaw"),
    ("hermes:", "hermes"),
    ("claude work loop", "claude"),
    ("adapter: skills: claude", "claude"),
    ("adapter: skills: codex", "codex"),
    ("adapter: skills: cursor", "cursor"),
    ("adapter: skills: openclaw", "openclaw"),
    ("adapter: skills: hermes", "hermes"),
    ("adapter: skills: gemini", "gemini"),
)

_EXPLANATIONS: tuple[tuple[str, str], ...] = (
    (
        "outcome loop: dormant",
        "Brigade is wired but the verify-and-capture loop has never been fed; "
        "without a captured verify the outcome ledger stays empty and ranking stays blind.",
    ),
    (
        "outcome loop: ledger stale",
        "The outcome ledger has had no new records for too long, so the installed "
        "work loop looks dormant even if older receipts exist.",
    ),
    (
        "outcome loop: half-fed",
        "Verify runs exist but none produced eligible scorecard receipts, so "
        "verified learning cannot promote or rank skills.",
    ),
    (
        "claude work loop",
        "Keeps Claude Code hooked into Brigade's verify-and-capture work loop so "
        "the install is exercised instead of sitting dormant.",
    ),
    (
        "bootstrap:",
        "Startup files agents load every session; missing or overgrown files "
        "truncate context or leave the agent without Brigade guidance.",
    ),
    (
        "agents-quality:",
        "AGENTS.md quality so agents see an explicit definition of done and a memory-handoff path.",
    ),
    (
        "memory-care:",
        "Memory-care decay scanner wiring and freshness so stale cards get refreshed instead of silently rotting.",
    ),
    (
        "security:",
        "Local security scan config and evidence so health and release checks have something truthful to read.",
    ),
    (
        "openclaw:",
        "OpenClaw host wiring Brigade expects for memory handoffs and care schedules.",
    ),
    (
        "hermes:",
        "Hermes harness adapter files and handoff inboxes Brigade projects into.",
    ),
    (
        "adapter: skills:",
        "Default-wired Brigade skills so the selected harness actually runs the "
        "work loop instead of only installing files.",
    ),
    (
        "receipts:",
        "Receipt digest integrity for verify runs and outcomes so drift is visible.",
    ),
    (
        "mcp:",
        "MCP catalog and adapter projection health for the selected harnesses.",
    ),
    (
        "runs:",
        "Run recovery checkpoints and journal headroom so interrupted runs stay recoverable.",
    ),
)

_COMMAND_RE = re.compile(r"`([^`]+)`")
_FIX_PREFIX_RE = re.compile(r"(?i)\bfix:\s*(.+)$")
_RUN_PREFIX_RE = re.compile(r"(?i)\brun\s+`([^`]+)`")


def harness_artifacts_present(target: Path, harness: str) -> bool:
    """True when the harness leaves detectable artifacts in or for the target."""
    target = target.expanduser().resolve()
    if harness == "claude":
        return (target / ".claude").is_dir() or (target / "CLAUDE.md").is_file()
    if harness == "codex":
        return (target / ".codex").is_dir() or (target / "codex.md").is_file()
    if harness == "cursor":
        return (target / ".cursor").is_dir()
    if harness == "gemini":
        return (target / ".gemini").is_dir()
    if harness == "hermes":
        return (target / ".brigade" / "hermes").is_dir() or (target / ".hermes").is_dir()
    if harness == "openclaw":
        return (Path.home() / ".openclaw" / "openclaw.json").is_file()
    return False


def harness_for_check_name(name: str) -> str | None:
    lower = name.lower()
    for prefix, harness in _HARNESS_CHECK_PREFIXES:
        if lower.startswith(prefix.lower()):
            return harness
    return None


def explanation_for(name: str) -> str:
    for prefix, text in _EXPLANATIONS:
        if name == prefix or name.startswith(prefix):
            return text
    return (
        f"Doctor check {name!r} protects local Brigade wiring; act on the "
        "observed/expected gap below so the profile stays usable."
    )


def extract_commands(detail: str) -> list[str]:
    """Pull remediation commands out of a doctor detail string."""
    commands: list[str] = []
    seen: set[str] = set()

    def _add(raw: str) -> None:
        command = raw.strip()
        # Drop a sentence-final period, but keep path arguments like `--target .`.
        if command.endswith(".") and not command.endswith(" ."):
            command = command[:-1].rstrip()
        if not command or command in seen:
            return
        if not _looks_like_command(command):
            return
        seen.add(command)
        commands.append(command)

    for match in _RUN_PREFIX_RE.finditer(detail):
        _add(match.group(1))
    for match in _FIX_PREFIX_RE.finditer(detail):
        _add(match.group(1))
    for match in _COMMAND_RE.finditer(detail):
        _add(match.group(1))
    return commands


def _looks_like_command(text: str) -> bool:
    lowered = text.lower()
    if lowered.startswith("brigade ") or lowered == "brigade":
        return True
    if text.startswith("./") or text.startswith("python"):
        return True
    if " " in text and not text.startswith("http"):
        # Prefer multi-token shell-like strings; skip bare paths/filenames.
        first = text.split()[0]
        return first in {"brigade", "python", "python3", "pip", "uv", "cargo", "npm", "go"} or first.endswith(".py")
    return False


def _split_observed_expected(detail: str) -> tuple[str, str]:
    text = detail.strip()

    def _path_tail(raw: str) -> str:
        path = raw.strip().rstrip(".")
        return path.split(";", 1)[0].strip()

    missing = re.search(r"(?i)\bmissing(?:\s+at)?\s+(.+)$", text)
    if missing:
        path = _path_tail(missing.group(1))
        return f"missing: {path}", f"present: {path}"
    not_present = re.search(r"(?i)\bnot present(?:\s+at)?\s+(.+)$", text)
    if not_present:
        path = _path_tail(not_present.group(1))
        return f"absent: {path}", f"present: {path}"
    not_found = re.search(r"(?i)\bnot found(?:\s+at)?\s+(.+)$", text)
    if not_found:
        path = _path_tail(not_found.group(1))
        return f"not found: {path}", f"present: {path}"
    invalid = re.search(r"(?i)\binvalid JSON:\s*(.+)$", text)
    if invalid:
        return f"parse failure: {invalid.group(1).strip()}", "valid JSON object"
    unable = re.search(r"(?i)unable to read[^:]*:\s*(.+)$", text)
    if unable:
        return f"parse failure: {unable.group(1).strip()}", "readable, valid JSON settings"
    if "wired but no verify run" in text.lower():
        return "no verify runs captured under .brigade/work/verify-runs/", "at least one captured verify run"
    if "no outcome ledger record" in text.lower() or "ledger empty" in text.lower():
        return text, f"an outcome ledger append within the last {OUTCOME_LEDGER_STALE_DAYS} days"
    return text, "check status OK"


def is_actionable_agent_check(
    check: CheckResult | ScopedCheckResult,
    *,
    target: Path,
) -> bool:
    status, name, _detail, _scope = _normalize_scoped_check(check)
    if status not in ACTIONABLE_STATUSES:
        return False
    harness = harness_for_check_name(name)
    if harness is not None and not harness_artifacts_present(target, harness):
        return False
    return True


def finding_from_check(
    check: CheckResult | ScopedCheckResult,
    *,
    target: Path,
) -> dict[str, Any] | None:
    status, name, detail, scope = _normalize_scoped_check(check)
    if not is_actionable_agent_check(check, target=target):
        return None
    annotated = _annotate_target_detail(target, (status, name, detail, scope))
    # Strip the target= prefix from observed/expected prose; keep it out of agent noise.
    prefix = f"target={target}: "
    clean_detail = annotated[len(prefix) :] if annotated.startswith(prefix) else annotated
    observed, expected = _split_observed_expected(clean_detail)
    commands = extract_commands(clean_detail)
    if name.startswith("outcome loop:") and not commands:
        commands = [
            'brigade work verify run --target . --command "<test>" --capture <skill-or-card-id>',
        ]
    if (
        name == "claude work loop"
        and "hook settings error" in clean_detail.lower()
        and not any("hooks install" in command for command in commands)
    ):
        commands.append(f"brigade work hooks install --target {target}")
    return {
        "severity": _SEVERITY_LABEL.get(status, status.lower()),
        "name": name,
        "explanation": explanation_for(name),
        "observed": observed,
        "expected": expected,
        "commands": commands,
        "scope": scope,
        "status": status,
        "detail": annotated,
    }


def build_findings(
    checks: Sequence[CheckResult | ScopedCheckResult],
    *,
    target: Path,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for check in checks:
        finding = finding_from_check(check, target=target)
        if finding is not None:
            findings.append(finding)
    return findings


def summary_line(passed: int, findings: int) -> str:
    finding_word = "finding" if findings == 1 else "findings"
    check_word = "check" if passed == 1 else "checks"
    return f"{passed} {check_word} passed, {findings} {finding_word}"


def _passed_count(checks: Sequence[CheckResult | ScopedCheckResult], findings: Sequence[Mapping[str, Any]]) -> int:
    """Count silent-healthy checks: OK/INFO plus harness N/A suppressions."""
    counts = _status_counts(list(checks))
    actionable = sum(1 for check in checks if _normalize_scoped_check(check)[0] in ACTIONABLE_STATUSES)
    suppressed = actionable - len(findings)
    return counts[OK] + counts[INFO] + max(0, suppressed)


def format_finding_text(finding: Mapping[str, Any]) -> str:
    lines = [
        f"severity: {finding['severity']}",
        f"name: {finding['name']}",
        f"explanation: {finding['explanation']}",
        f"observed: {finding['observed']}",
        f"expected: {finding['expected']}",
        "commands:",
    ]
    commands = finding.get("commands") if isinstance(finding.get("commands"), list) else []
    if commands:
        lines.extend(f"  - {command}" for command in commands)
    else:
        lines.append("  - (none; inspect observed/expected and fix the wiring)")
    return "\n".join(lines)


def report_agent_text(
    checks: Sequence[CheckResult | ScopedCheckResult],
    *,
    target: Path,
) -> int:
    findings = build_findings(checks, target=target)
    passed = _passed_count(checks, findings)
    print(summary_line(passed, len(findings)))
    if findings:
        print()
        for index, finding in enumerate(findings):
            if index:
                print()
            print(format_finding_text(finding))
    failed = any(finding.get("status") == FAIL or finding.get("severity") == "error" for finding in findings)
    return 1 if failed else 0


def report_agent_json(
    ctx: Any,
    checks: Sequence[CheckResult | ScopedCheckResult],
    *,
    operator: bool = False,
) -> int:
    findings = build_findings(checks, target=ctx.target)
    passed = _passed_count(checks, findings)
    counts = _status_counts(list(checks))
    sel = ctx.selection
    payload = {
        "mode": "agent",
        "target": str(ctx.target),
        "harnesses": list(ctx.harnesses),
        "owner": getattr(sel, "owner", None),
        "depth": getattr(sel, "depth", None),
        "operator": operator,
        "summary": {
            "total": len(list(checks)),
            "passed": passed,
            "findings": len(findings),
            "ok": counts[OK],
            "warn": counts[WARN],
            "manual": counts[MANUAL],
            "failed": counts[FAIL],
            "info": counts[INFO],
            "line": summary_line(passed, len(findings)),
        },
        "findings": [
            {
                "severity": finding["severity"],
                "name": finding["name"],
                "explanation": finding["explanation"],
                "observed": finding["observed"],
                "expected": finding["expected"],
                "commands": list(finding["commands"]),
                "scope": finding["scope"],
            }
            for finding in findings
        ],
        "ready": counts[FAIL] == 0 and not any(f.get("severity") == "error" for f in findings),
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 1 if not payload["ready"] else 0
