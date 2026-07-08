from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Action = Literal["allow", "warn", "redact", "block"]


@dataclass(frozen=True)
class Rule:
    id: str
    category: str
    pattern: str
    replacement: str
    description: str = ""
    flags: int = 0


@dataclass(frozen=True)
class Finding:
    rule_id: str
    category: str
    action: Action
    match: str
    replacement: str
    line: int
    column: int
    start: int
    end: int
    source: str = "regex"
    message: str = ""
    allowed_by: str | None = None

    @property
    def blocks(self) -> bool:
        return self.action == "block"

    @property
    def redacts(self) -> bool:
        return self.action in {"redact", "block"}


@dataclass
class ScanOptions:
    scan_frontmatter: bool = False
    scan_code_blocks: bool = True
    honor_allow_comments: bool = True
    include_opf: bool = False
    opf_device: str | None = None
    opf_bin: str | None = None


@dataclass
class TextEdit:
    start: int
    end: int
    replacement: str


@dataclass
class GuardResult:
    text: str
    redacted_text: str
    findings: list[Finding] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return any(f.blocks for f in self.findings)

    @property
    def changed(self) -> bool:
        return self.text != self.redacted_text

    def counts_by_action(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for finding in self.findings:
            counts[finding.action] = counts.get(finding.action, 0) + 1
        return counts

    def counts_by_category(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for finding in self.findings:
            counts[finding.category] = counts.get(finding.category, 0) + 1
        return counts
