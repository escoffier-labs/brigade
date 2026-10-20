"""Fail-closed bug-bundle scrubbing. Removed values never enter receipts."""

from __future__ import annotations

import getpass
import os
import re
import socket
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

from .evidence_redaction import apply_origin_redaction
from .guard.engine import scan_text
from .guard.policy import Policy
from .guard.types import GuardResult, Rule, ScanOptions


# Bug bundles need stricter leak detection than repository content. Keep these
# rules local so existing tracked content retains its guard policy.
_BUG_RULES: tuple[Rule, ...] = (
    Rule(
        id="windows-home-path",
        category="pii",
        pattern=r"(?i)(?<![\w])(?:[A-Z]:[\\/]+(?:Users|Documents and Settings)[\\/]+)[^\\/\r\n\"<>|]+(?:[\\/]+[^\s\"<>|]+)*",
        replacement="<PRIVATE_HOME_PATH>",
        description="Windows user home path, including spaced user names and escaped separators.",
    ),
    Rule(
        id="root-home-path",
        category="pii",
        pattern=r"(?<![\w.-])/root(?:/[^\s\"'<>`]+)?(?![\w.-])",
        replacement="<PRIVATE_HOME_PATH>",
        description="Root user home directory.",
    ),
    Rule(
        id="bare-token",
        category="secret",
        pattern=r"\b(?:ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}|AKIA[A-Z0-9]{16}|xox[baprs]-[A-Za-z0-9-]{20,})",
        replacement="[redacted-secret]",
        description="Recognizable credential prefixes without an assignment.",
    ),
    Rule(
        id="private-hostname",
        category="infrastructure",
        pattern=r"\b(?:[A-Za-z0-9-]+\.)+(?:internal|local|lan|corp|intranet|home\.arpa)\b(?![\w.-]|\s*\()|\b(?:hostname|host)[ \t]*:[ \t]*(?!(?:str|object|int|Any|float|bool|string)\b)[A-Za-z0-9][A-Za-z0-9.-]*",
        replacement="[redacted-host]",
        description="Private DNS suffix or explicitly identified hostname.",
        flags=re.IGNORECASE,
    ),
)


class ReportBlocked(ValueError):
    """A report could not be proved safe to share."""


def assert_clean(text: str) -> None:
    """Scan the final bytes independently, ignoring embedded allow directives."""
    try:
        result = scan_text(
            text,
            policy=Policy(custom_rules=list(_BUG_RULES)),
            options=ScanOptions(
                honor_allow_comments=False,
                scan_frontmatter=True,
                scan_code_blocks=True,
                include_opf=False,
            ),
        )
    except Exception:
        raise ReportBlocked("Bug report blocked: second scan failed") from None
    if not isinstance(result, GuardResult) or result.findings:
        raise ReportBlocked("Bug report blocked: second scan has remaining findings")


class Redactor:
    def __init__(self, target: Path):
        self.counts: Counter[str] = Counter()
        self.hostname = socket.gethostname()
        values = {
            str(Path.home()),
            str(target.resolve()),
            tempfile.gettempdir(),
            self.hostname,
            getpass.getuser(),
        }
        values.update(
            value
            for key, value in os.environ.items()
            if re.search(r"TOKEN|KEY|SECRET|PASSWORD|PASS|CREDENTIAL", key, re.I) and value
        )
        self.literals = sorted((value for value in values if value and value not in {"/", "\\"}), key=len, reverse=True)

    def text(self, text: str) -> str:
        for literal in self.literals:
            # Path/secret literals may occur inside larger strings. Bare user
            # names need boundaries to avoid corrupting unrelated identifiers.
            pattern = re.escape(literal)
            if literal == getpass.getuser() or (len(literal) <= 3 and literal.isalnum()):
                pattern = rf"(?<![\w.]){pattern}(?![\w.])"
            text, count = re.subn(
                pattern, "<local>", text, flags=re.I if literal == self.hostname or "\\" in literal else 0
            )
            self.counts["local-literal"] += count
        text, count = re.subn(
            r"(?i)\bAuthorization:[ \t]*(?:Bearer|Basic)[ \t]+[^\s]+", "Authorization: <credential>", text
        )
        self.counts["authorization-header"] += count
        text, count = re.subn(r"(?:op|keepass)://[^\s\"'<>`]+", "<credential-reference>", text, flags=re.I)
        self.counts["credential-reference"] += count
        for rule in _BUG_RULES:
            text, count = re.subn(rule.pattern, rule.replacement, text, flags=rule.flags)
            self.counts[rule.id] += count
        try:
            verdict = apply_origin_redaction(text, origin="unknown")
        except Exception:
            raise ReportBlocked("Bug report blocked: redaction scanner failed") from None
        if verdict.status == "error":
            raise ReportBlocked("Bug report blocked: redaction scanner failed")
        # The origin verdict gives a total and detector ids. A separate scan
        # obtains per-detector counts without retaining matched values.
        if verdict.count:
            try:
                counts = scan_text(text, options=ScanOptions(honor_allow_comments=False))
                self.counts.update(f.rule_id for f in counts.findings)
            except Exception:
                raise ReportBlocked("Bug report blocked: redaction counts unavailable") from None
        return verdict.persisted_text

    def project(self, value: Any) -> Any:
        """Scrub values of an already closed-schema projection, never its keys."""
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {key: self.project(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.project(item) for item in value]
        return value

    def record(self) -> dict[str, Any]:
        counts = {key: value for key, value in sorted(self.counts.items()) if value}
        return {"count": sum(counts.values()), "detectors": counts}
