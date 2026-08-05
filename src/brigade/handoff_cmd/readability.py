"""Standalone-readability heuristics for Brigade memory handoffs.

Handoff authors often write bullets like "the fix was reverting it" or
"that file needs the guard". Those phrases depend on session context and
become unintelligible once the session is gone. This module flags them with
regex heuristics so authors can rewrite with explicit subjects and dates.

Only Durable facts and Suggested card content are scanned. Fenced code blocks
are skipped, except when a fence is the first non-blank line of a scanned
section: that opening fence is treated as a Suggested card content wrapper
(skip the fence line, keep scanning the body). Fence and HTML-comment state
reset at every ``## `` heading so skip mode never bleeds across sections.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

READABILITY_CATEGORIES: tuple[str, ...] = ("bare-pronoun", "deictic", "relative-date")

READABILITY_PATTERN_DOCS: str = """
Pattern classes scanned in Durable facts and Suggested card content only:

bare-pronoun — after stripping a leading list marker, the line opens with one of
it, its, this, that, they, them, their, these, those (word boundary). Mid-line
pronouns are ignored because they usually refer to an earlier noun in the same
bullet. Suppressed when a deictic pattern matches at the same stripped line start.

deictic — matched anywhere in the scan string (case-insensitive, word boundaries):
  (this|that) + (file|files|repo|repos|repository|script|scripts|commit|commits|
  test|tests|function|functions|method|module|class|error|errors|issue|bug|fix|
  change|patch|branch|directory|folder|command|line|config|setting|value|step|run|
  job|workflow|one|thing);
  the (above|below|aforementioned|former|latter);
  the (earlier|previous|prior|last) <word>;
  the same <word>;
  as (before|above|previously|mentioned|described|noted);
  said (file|script|command|error|fix|change).

relative-date — matched anywhere (case-insensitive, word boundaries):
  yesterday, today, tomorrow, tonight, this morning, this afternoon, this evening,
  earlier today, just now, right now, currently, at the moment, recently, lately,
  nowadays, these days, the other day;
  (last|this|next) (week|month|year|night|time|quarter|sprint);
  a (few|couple of) (days|weeks|months|years) ago;
  <digits> (day|days|week|weeks|month|months|year|years) ago.

Skip rules — fenced code blocks and HTML comments are not scanned. When a fence
line is the first non-blank line inside a scanned section, it is a content
wrapper (common for Suggested card content): skip that fence line only and keep
scanning; later fences in the same section toggle skip mode normally. Fence and
HTML-comment state reset whenever a new ## heading is encountered.
""".strip()

_EXCERPT_LIMIT = 160

_SCANNED_SECTIONS: frozenset[str] = frozenset({"Durable facts", "Suggested card content"})

_SECTION_SYNONYMS: dict[str, str] = {
    "durable facts": "Durable facts",
    "facts": "Durable facts",
    "suggested card content": "Suggested card content",
    "card content": "Suggested card content",
}

_CATEGORY_ORDER: dict[str, int] = {name: index for index, name in enumerate(READABILITY_CATEGORIES)}

_SUGGESTIONS: dict[str, str] = {
    "bare-pronoun": (
        "name the subject explicitly; a bullet that opens with a pronoun has no antecedent once the session is gone"
    ),
    "deictic": "name the path, symbol, or identifier instead of pointing at it",
    "relative-date": ("use an absolute date (YYYY-MM-DD); relative time is meaningless when the card is read later"),
}

_DEICTIC_NOUNS = (
    "file",
    "files",
    "repo",
    "repos",
    "repository",
    "script",
    "scripts",
    "commit",
    "commits",
    "test",
    "tests",
    "function",
    "functions",
    "method",
    "module",
    "class",
    "error",
    "errors",
    "issue",
    "bug",
    "fix",
    "change",
    "patch",
    "branch",
    "directory",
    "folder",
    "command",
    "line",
    "config",
    "setting",
    "value",
    "step",
    "run",
    "job",
    "workflow",
    "one",
    "thing",
)

_DEICTIC_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(rf"\b(?:this|that)\s+(?:{'|'.join(_DEICTIC_NOUNS)})\b", re.IGNORECASE),
    re.compile(r"\bthe\s+(?:above|below|aforementioned|former|latter)\b", re.IGNORECASE),
    re.compile(r"\bthe\s+(?:earlier|previous|prior|last)\s+\w+\b", re.IGNORECASE),
    re.compile(r"\bthe\s+same\s+\w+\b", re.IGNORECASE),
    re.compile(r"\bas\s+(?:before|above|previously|mentioned|described|noted)\b", re.IGNORECASE),
    re.compile(r"\bsaid\s+(?:file|script|command|error|fix|change)\b", re.IGNORECASE),
)

_RELATIVE_DATE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bthis\s+morning\b", re.IGNORECASE),
    re.compile(r"\bthis\s+afternoon\b", re.IGNORECASE),
    re.compile(r"\bthis\s+evening\b", re.IGNORECASE),
    re.compile(r"\bearlier\s+today\b", re.IGNORECASE),
    re.compile(r"\bjust\s+now\b", re.IGNORECASE),
    re.compile(r"\bright\s+now\b", re.IGNORECASE),
    re.compile(r"\bat\s+the\s+moment\b", re.IGNORECASE),
    re.compile(r"\bthe\s+other\s+day\b", re.IGNORECASE),
    re.compile(r"\bthese\s+days\b", re.IGNORECASE),
    re.compile(r"\ba\s+(?:few|couple\s+of)\s+(?:days|weeks|months|years)\s+ago\b", re.IGNORECASE),
    re.compile(
        r"\b\d+\s+(?:day|days|week|weeks|month|months|year|years)\s+ago\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:last|this|next)\s+(?:week|month|year|night|time|quarter|sprint)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:yesterday|today|tomorrow|tonight|currently|recently|lately|nowadays)\b",
        re.IGNORECASE,
    ),
)

_BARE_PRONOUN_PATTERN = re.compile(
    r"^(it|its|this|that|they|them|their|these|those)\b",
    re.IGNORECASE,
)

_LIST_MARKER_PATTERN = re.compile(r"^[\s*_]*(?:[-*+]|\d+[.)])\s+")

_INLINE_CODE_PATTERN = re.compile(r"`[^`]*`")

_MARKDOWN_LINK_URL_PATTERN = re.compile(r"\[([^\]]*)\]\([^)]*\)")


@dataclass(frozen=True)
class ReadabilityFinding:
    line: int
    section: str
    category: str
    match: str
    excerpt: str
    suggestion: str

    def as_dict(self) -> dict[str, object]:
        return {
            "line": self.line,
            "section": self.section,
            "category": self.category,
            "match": self.match,
            "excerpt": self.excerpt,
            "suggestion": self.suggestion,
        }


def scan_standalone_readability(text: str) -> tuple[ReadabilityFinding, ...]:
    findings: list[ReadabilityFinding] = []
    current_section: str | None = None
    in_fence = False
    in_comment = False
    section_seen_nonblank = False

    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()

        if stripped.startswith("## "):
            current_section = _resolve_section_name(stripped[3:])
            in_fence = False
            in_comment = False
            section_seen_nonblank = False
            continue

        if current_section is None:
            continue

        if stripped.startswith("```"):
            if not section_seen_nonblank:
                # First non-blank fence in the section wraps card content; do not skip the body.
                section_seen_nonblank = True
                continue
            in_fence = not in_fence
            continue
        if in_fence:
            continue

        if in_comment:
            if "-->" in stripped:
                in_comment = False
            continue
        if "<!--" in stripped:
            section_seen_nonblank = True
            if "-->" not in stripped.split("<!--", 1)[1]:
                in_comment = True
            continue

        if not stripped:
            continue
        section_seen_nonblank = True
        if stripped.startswith("#"):
            continue

        scan_string = _build_scan_string(line)
        excerpt = stripped[:_EXCERPT_LIMIT]
        stripped_for_pronoun = _LIST_MARKER_PATTERN.sub("", scan_string).lstrip()

        deictic_at_start = _earliest_pattern_match(_DEICTIC_PATTERNS, stripped_for_pronoun, at_start=True)
        if deictic_at_start is None:
            pronoun_match = _BARE_PRONOUN_PATTERN.match(stripped_for_pronoun)
            if pronoun_match is not None:
                findings.append(
                    ReadabilityFinding(
                        line=line_number,
                        section=current_section,
                        category="bare-pronoun",
                        match=pronoun_match.group(1).lower(),
                        excerpt=excerpt,
                        suggestion=_SUGGESTIONS["bare-pronoun"],
                    )
                )

        deictic_match = _earliest_pattern_match(_DEICTIC_PATTERNS, scan_string)
        if deictic_match is not None:
            findings.append(
                ReadabilityFinding(
                    line=line_number,
                    section=current_section,
                    category="deictic",
                    match=deictic_match.lower(),
                    excerpt=excerpt,
                    suggestion=_SUGGESTIONS["deictic"],
                )
            )

        relative_match = _earliest_pattern_match(_RELATIVE_DATE_PATTERNS, scan_string)
        if relative_match is not None:
            findings.append(
                ReadabilityFinding(
                    line=line_number,
                    section=current_section,
                    category="relative-date",
                    match=relative_match.lower(),
                    excerpt=excerpt,
                    suggestion=_SUGGESTIONS["relative-date"],
                )
            )

    return tuple(
        sorted(
            findings,
            key=lambda finding: (
                finding.line,
                _CATEGORY_ORDER[finding.category],
                finding.match,
            ),
        )
    )


def _normalize_section_heading(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().casefold()).rstrip(":").strip()


def _resolve_section_name(raw_heading: str) -> str | None:
    canonical = _SECTION_SYNONYMS.get(_normalize_section_heading(raw_heading))
    if canonical in _SCANNED_SECTIONS:
        return canonical
    return None


def _build_scan_string(line: str) -> str:
    without_code = _INLINE_CODE_PATTERN.sub("", line)
    return _MARKDOWN_LINK_URL_PATTERN.sub(r"[\1]", without_code)


def _earliest_pattern_match(
    patterns: tuple[re.Pattern[str], ...],
    text: str,
    *,
    at_start: bool = False,
) -> str | None:
    best_position: int | None = None
    best_match: str | None = None
    for pattern in patterns:
        match = pattern.match(text) if at_start else pattern.search(text)
        if match is None:
            continue
        position = match.start()
        if best_position is None or position < best_position:
            best_position = position
            best_match = match.group(0)
    return best_match
