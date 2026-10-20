"""Fail-closed bug-bundle scrubbing. Removed values never enter receipts."""

from __future__ import annotations

import getpass
import json
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

_SECRET_ENV_NAME = re.compile(r"TOKEN|KEY|SECRET|PASSWORD|PASS|CREDENTIAL", re.I)
_TERMINAL_CONTROLS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


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
        pattern=r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}|AKIA[A-Z0-9]{16}|xox[baprs]-[A-Za-z0-9-]{20,})",
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


def assert_terminal_safe(value: Any) -> None:
    """Reject legacy prepared fields that could control a preview or gh output."""
    if isinstance(value, str) and _TERMINAL_CONTROLS.search(value):
        raise ReportBlocked("Bug report blocked: terminal controls remain, prepare a new preview")
    if isinstance(value, dict):
        for item in value.values():
            assert_terminal_safe(item)
    elif isinstance(value, list):
        for item in value:
            assert_terminal_safe(item)


def assert_clean(text: str) -> None:
    """Scan the final bytes independently, ignoring embedded allow directives."""
    assert_terminal_safe(text)
    for name, value in os.environ.items():
        if not _SECRET_ENV_NAME.search(name) or not 0 < len(value) <= 3:
            continue
        # Rendered JSON may disclose the same value with quotes, control bytes,
        # or non-ASCII characters escaped.
        variants = (value, json.dumps(value)[1:-1], json.dumps(value, ensure_ascii=False)[1:-1])
        if any(variant in text for variant in variants):
            raise ReportBlocked(
                f"Bug report blocked: second scan found a short secret from environment variable {name}"
            )
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
        values.update(value for key, value in os.environ.items() if _SECRET_ENV_NAME.search(key) and len(value) >= 4)
        self.literals = sorted((value for value in values if value and value not in {"/", "\\"}), key=len, reverse=True)

    def text(self, text: str) -> str:
        for literal in self.literals:
            # Short secrets are checked by the second scan rather than replaced.
            # Bare user names need boundaries to avoid corrupting identifiers.
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
        # Keep newlines and tabs for readable logs, but render all other C0/C1
        # bytes visibly. Escaping ESC also neutralizes ANSI and OSC sequences.
        text, count = _TERMINAL_CONTROLS.subn(lambda match: f"\\x{ord(match[0]):02x}", verdict.persisted_text)
        self.counts["terminal-control"] += count
        return text

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
