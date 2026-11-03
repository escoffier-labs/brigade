"""Prompt-load scan for memory text a connector profile hands to a model.

A profile that serves memory to an agent reading untrusted content scans the
exact text it is about to return. The scan is the interim one the v2 design
sets until hub-side content scanning ships: the local guard's bundled default
rules (no repository rules, baselines or allow markers), the invisible and
bidirectional character check, and the instruction-override heuristics shared
with handoff linting. The caller replaces a hit with
``{card_id, blocked: <rule id>}`` and never returns the text.
"""

from __future__ import annotations

from typing import Iterable

from brigade.guard import Policy, scan_text
from brigade.guard.types import ScanOptions
from brigade.untrusted import scan_handoff_injection_heuristics

BIDI_RULE = "bidi-control"
INVISIBLE_RULE = "invisible-character"

# Bidi embedding, override and isolate controls.
_BIDI = frozenset(chr(code) for code in (*range(0x202A, 0x202F), *range(0x2066, 0x206A)))
# Zero-width space, word joiner and invisible operators, BOM and tag characters.
# Zero-width joiner and non-joiner stay allowed: some scripts and emoji need them.
_INVISIBLE = frozenset(chr(code) for code in (0x200B, *range(0x2060, 0x2065), 0xFEFF, *range(0xE0000, 0xE0080)))
_BOM = chr(0xFEFF)
_GUARD_OPTIONS = ScanOptions(scan_frontmatter=True, honor_allow_comments=False)


def invisible_rule(text: str) -> str | None:
    """Return the rule id for a blocked invisible or bidi character, without regex."""
    found = set(text[1:] if text.startswith(_BOM) else text)
    if found & _BIDI:
        return BIDI_RULE
    if found & _INVISIBLE:
        return INVISIBLE_RULE
    return None


def blocked_rule(texts: Iterable[str]) -> str | None:
    """Return the first rule id that blocks any of ``texts``, or None when all are clean.

    Every instruction-override heuristic hit blocks, including ones the handoff
    linter would downgrade as documentation: a reader on this path cannot tell
    quoted discussion from a payload.
    """
    text = "\n".join(value for value in texts if isinstance(value, str))
    if not text:
        return None
    rule = invisible_rule(text)
    if rule:
        return rule
    hits = scan_handoff_injection_heuristics(text)
    if hits:
        return hits[0].rule
    for finding in scan_text(text, Policy(), _GUARD_OPTIONS).findings:
        if finding.blocks:
            return finding.rule_id
    return None
