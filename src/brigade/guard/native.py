"""Optional match acceleration. Policy and Python regex semantics stay authoritative."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess

from .rules import DEFAULT_RULES
from .types import Rule

PROTOCOL = 1
MIN_NATIVE_CHARS = 65536
_LOG = logging.getLogger(__name__)


def native_spans(text: str, rules: list[Rule]) -> list[list[tuple[int, int]]] | None:
    """Return ordered raw spans, or None to run the authoritative Python engine.

    `native` bypasses the size threshold for parity testing. It still falls
    back on errors and on inputs outside the proven PCRE2 compatibility set.
    """
    mode = os.environ.get("BRIGADE_GUARD_BACKEND", "auto")
    if mode == "python" or (mode == "auto" and len(text) < MIN_NATIVE_CHARS):
        return None
    # Python's Unicode database, case folding and word/space classes differ
    # from PCRE2. In ASCII Python also recognizes four extra space controls.
    # Custom patterns can use Python-only constructs even on ASCII subjects.
    if (
        not text.isascii()
        or any(char in text for char in "\x1c\x1d\x1e\x1f")
        or any(not any(rule is default for default in DEFAULT_RULES) for rule in rules)
    ):
        return None
    binary = os.environ.get("BRIGADE_GUARD_BIN") or shutil.which("brigade-guard")
    if not binary:
        return None
    try:
        if mode not in {"auto", "native"}:
            raise ValueError("invalid backend")
        request = {
            "protocol": PROTOCOL,
            "text": text,
            "rules": [{"id": rule.id, "pattern": rule.pattern, "flags": int(rule.flags)} for rule in rules],
        }
        process = subprocess.run(
            [binary],
            input=json.dumps(request, ensure_ascii=True),
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        response = json.loads(process.stdout)
        if type(response["protocol"]) is not int or response["protocol"] != PROTOCOL:
            raise ValueError("protocol mismatch")
        if len(response["rules"]) != len(rules):
            raise ValueError("rule count mismatch")
        result: list[list[tuple[int, int]]] = []
        for rule, entry in zip(rules, response["rules"], strict=True):
            if entry["id"] != rule.id:
                raise ValueError("rule order mismatch")
            spans: list[tuple[int, int]] = []
            previous_end = 0
            for span in entry["spans"]:
                start, end = span["start"], span["end"]
                if (
                    type(start) is not int
                    or type(end) is not int
                    or not previous_end <= start < end <= len(text)
                    or span["byte_start"] != start
                    or span["byte_end"] != end
                ):
                    raise ValueError("invalid offsets")
                spans.append((start, end))
                previous_end = end
            result.append(spans)
        return result
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        _LOG.warning("brigade guard: native backend failed; using Python re")
        return None


def rule_spans(text: str, rules: list[Rule]) -> list[list[tuple[int, int]]]:
    spans = native_spans(text, rules)
    if spans is not None:
        return spans
    return [[match.span() for match in re.finditer(rule.pattern, text, rule.flags)] for rule in rules]
