"""Repo-level auditor: aggregate scan results across a directory tree.

The `audit` command is reporting-oriented. It runs the same engine as
`scan`, but instead of per-file output it produces a consolidated summary
(counts by action / category / rule, top-offender files).

Two enumeration modes:
- `tracked` (default): use git's `ls-files` from inside the target directory.
  Only files Git already knows about. This matches what the pre-push hook
  scans and is the right default for "what could leak out of this repo".
- `tree`: walk the filesystem with `Path.rglob("*")`, skipping the standard
  excluded dirs (node_modules, .git, dist, etc.) and binary files. This is
  the "what's lurking on disk" mode useful for local lint reviews.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .engine import scan_text
from .git_scan import _read_text, _tracked_paths
from .policy import Policy
from .types import GuardResult, ScanOptions

DEFAULT_EXCLUDE_DIR_NAMES = frozenset(
    {
        "node_modules",
        ".git",
        "dist",
        "build",
        "coverage",
        ".next",
        ".cache",
        ".venv",
        "__pycache__",
        ".pytest_cache",
        "out",
        ".turbo",
        ".parcel-cache",
        "vendor",
        ".claude",
    }
)


@dataclass
class FileAudit:
    path: str
    findings: int
    blocked: bool


@dataclass
class AuditReport:
    target: str
    scope: str
    files_scanned: int = 0
    files_with_findings: int = 0
    total_findings: int = 0
    blocked: bool = False
    by_action: dict[str, int] = field(default_factory=dict)
    by_category: dict[str, int] = field(default_factory=dict)
    by_rule: dict[str, int] = field(default_factory=dict)
    file_audits: list[FileAudit] = field(default_factory=list)

    def top_rules(self, limit: int = 10) -> list[tuple[str, int]]:
        return sorted(self.by_rule.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]

    def top_offenders(self, limit: int = 5) -> list[FileAudit]:
        with_findings = [f for f in self.file_audits if f.findings > 0]
        return sorted(with_findings, key=lambda f: (-f.findings, f.path))[:limit]

    def to_payload(self, *, top_rules: int = 10, top_offenders: int = 5) -> dict:
        return {
            "summary": {
                "target": self.target,
                "scope": self.scope,
                "files_scanned": self.files_scanned,
                "files_with_findings": self.files_with_findings,
                "total_findings": self.total_findings,
                "blocked": self.blocked,
            },
            "by_action": dict(sorted(self.by_action.items())),
            "by_category": dict(sorted(self.by_category.items())),
            "by_rule": [{"rule_id": rule_id, "count": count} for rule_id, count in self.top_rules(limit=top_rules)],
            "top_offenders": [
                {"path": entry.path, "findings": entry.findings, "blocked": entry.blocked}
                for entry in self.top_offenders(limit=top_offenders)
            ],
        }


def run_audit(
    target: Path,
    *,
    policy: Policy,
    scope: str = "tracked",
    options: ScanOptions | None = None,
    exclude_dirs: Iterable[str] = DEFAULT_EXCLUDE_DIR_NAMES,
) -> AuditReport:
    """Walk `target` per `scope`, scan each text file, and aggregate."""

    if scope not in {"tracked", "tree"}:
        raise ValueError(f"unknown scope: {scope!r} (expected 'tracked' or 'tree')")

    target = Path(target)
    if not target.exists():
        raise FileNotFoundError(f"audit target does not exist: {target}")
    if not target.is_dir():
        raise NotADirectoryError(f"audit target must be a directory: {target}")

    options = options or ScanOptions()
    report = AuditReport(target=str(target), scope=scope)
    paths = _enumerate(target, scope=scope, exclude_dirs=frozenset(exclude_dirs))

    for path in paths:
        text = _read_text(path)
        if text is None:
            continue

        result = scan_text(text, policy=policy, options=options)
        report.files_scanned += 1
        _record(report, path, result, target=target)

    return report


def _enumerate(target: Path, *, scope: str, exclude_dirs: frozenset[str]) -> list[Path]:
    if scope == "tracked":
        rel_paths = _tracked_paths(all_tracked=True, cwd=target)
        return [target / rel for rel in rel_paths]

    # scope == "tree"
    paths: list[Path] = []
    for entry in sorted(target.rglob("*")):
        if not entry.is_file():
            continue
        try:
            rel_parts = entry.relative_to(target).parts
        except ValueError:
            rel_parts = entry.parts
        if any(part in exclude_dirs for part in rel_parts):
            continue
        paths.append(entry)
    return paths


def _record(report: AuditReport, path: Path, result: GuardResult, *, target: Path) -> None:
    findings = result.findings
    try:
        rel = path.relative_to(target)
        path_str = str(rel)
    except ValueError:
        path_str = str(path)

    report.file_audits.append(FileAudit(path=path_str, findings=len(findings), blocked=result.blocked))

    if not findings:
        return

    report.files_with_findings += 1
    report.total_findings += len(findings)
    if result.blocked:
        report.blocked = True

    for finding in findings:
        report.by_action[finding.action] = report.by_action.get(finding.action, 0) + 1
        report.by_category[finding.category] = report.by_category.get(finding.category, 0) + 1
        report.by_rule[finding.rule_id] = report.by_rule.get(finding.rule_id, 0) + 1


def render_text(report: AuditReport) -> str:
    """Render `report` as a human-readable multi-section summary."""

    lines: list[str] = []
    lines.append(f"content-guard audit: {report.target}")
    lines.append("")
    lines.append("Summary")
    lines.append(f"  scope:               {report.scope}")
    lines.append(f"  files scanned:       {report.files_scanned}")
    lines.append(f"  files with findings: {report.files_with_findings}")
    lines.append(f"  total findings:      {report.total_findings}")
    lines.append(f"  blocked:             {str(report.blocked).lower()}")

    if report.by_action:
        lines.append("")
        lines.append("By action")
        for action, count in sorted(report.by_action.items()):
            lines.append(f"  {action:<8} {count}")

    if report.by_category:
        lines.append("")
        lines.append("By category")
        for category, count in sorted(report.by_category.items()):
            lines.append(f"  {category:<16} {count}")

    top_rules = report.top_rules(limit=10)
    if top_rules:
        lines.append("")
        lines.append("By rule (top 10)")
        for rule_id, count in top_rules:
            lines.append(f"  {rule_id:<32} {count}")

    top_offenders = report.top_offenders(limit=5)
    if top_offenders:
        lines.append("")
        lines.append("Top offenders (top 5)")
        for entry in top_offenders:
            marker = " [BLOCKED]" if entry.blocked else ""
            lines.append(f"  {entry.findings:>4}  {entry.path}{marker}")

    if report.total_findings == 0:
        lines.append("")
        lines.append("Clean. No findings.")

    return "\n".join(lines)
