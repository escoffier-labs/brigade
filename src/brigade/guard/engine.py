from __future__ import annotations

import bisect
import re
from typing import NamedTuple

from .detectors.opf import run_opf
from .policy import Policy
from .types import Finding, GuardResult, Rule, ScanOptions, TextEdit

# The token class includes `/` because findings are displayed as
# `category/rule-id` and users copy that form into allow comments.
ALLOW_RE = re.compile(r"content-guard:\s*allow\s+([A-Za-z0-9_./:-]+|all)(?:\s+(file))?")

# Line-start comment / directive prefixes. `*` covers block-comment
# continuations (` * content-guard: ...`); `;` covers ini/lisp.
_LINE_START_COMMENT_PREFIXES = ("<!--", "//", "/*", "--", "#", ";", "*")
# Mid-line trailing comments. `;` and `*` are omitted: they are also
# statement terminators and operators.
_TRAILING_COMMENT_PREFIXES = ("//", "/*", "#")
_TRIPLE_QUOTES = ('"""', "'''")
_SINGLE_QUOTES = frozenset("\"'`")

IGNORED_FILE_ALLOW_RULE_ID = "ignored-file-allow"
IGNORED_FILE_ALLOW_IN_STRING = "File-scoped allow marker is inside a string literal and was ignored."
IGNORED_FILE_ALLOW_NOT_DIRECTIVE = "File-scoped allow marker is not in a comment or directive position and was ignored."


class _IgnoredFileAllow(NamedTuple):
    line: int
    column: int
    start: int
    end: int
    match: str
    message: str


def scan_text(text: str, policy: Policy | None = None, options: ScanOptions | None = None) -> GuardResult:
    active_policy = policy or Policy()
    active_options = options or ScanOptions()

    line_starts = _line_starts(text)
    skipped_ranges = _skipped_ranges(text, active_options)
    if active_options.honor_allow_comments:
        allow_by_line, file_allows, ignored_file_allows = _allow_comments_by_line(text)
    else:
        allow_by_line, file_allows, ignored_file_allows = {}, set(), []

    allow_values = set(active_policy.allow_values)

    findings: list[Finding] = []
    occupied: list[tuple[int, int]] = []

    for rule in active_policy.all_rules():
        regex = re.compile(rule.pattern, rule.flags)
        for match in regex.finditer(text):
            start, end = match.span()
            if start == end:
                continue
            if rule.id == "email" and active_policy.allows_agent_coauthor_email(
                match.group(0), _line_at(text, start, end)
            ):
                continue
            if _inside_ranges(start, end, skipped_ranges):
                continue
            if _overlaps(start, end, occupied):
                continue

            line = _line_for_offset(line_starts, start)
            allowed_by = _allowed_by(rule.id, rule.category, line, allow_by_line, file_allows)
            # A known-public literal exact-matches the whole finding text. This
            # reaches history scans of old diffs where no inline marker exists.
            if allowed_by is None and match.group(0) in allow_values:
                allowed_by = "allow-value"
            # A known-public literal may also be LONGER than the matched span
            # (e.g. one public path that a broad home-path rule matches only a
            # prefix of). It clears the finding only when this match span lies
            # inside an occurrence of the literal on its line, so the same
            # prefix elsewhere (even on the same line) stays blocked.
            if allowed_by is None:
                line_start = line_starts[line - 1]
                line_end = line_starts[line] if line < len(line_starts) else len(text)
                line_text = text[line_start:line_end]
                allowed_by = _allowed_by_superstring_value(
                    allow_values, match.group(0), line_text, line_start, start, end
                )
            action = "allow" if allowed_by else active_policy.action_for(rule)
            findings.append(
                Finding(
                    rule_id=rule.id,
                    category=rule.category,
                    action=action,
                    match=match.group(0),
                    replacement=rule.replacement,
                    line=line,
                    column=start - line_starts[line - 1] + 1,
                    start=start,
                    end=end,
                    source="regex",
                    message=rule.description,
                    allowed_by=allowed_by,
                )
            )
            occupied.append((start, end))

    for ignored in ignored_file_allows:
        findings.append(
            Finding(
                rule_id=IGNORED_FILE_ALLOW_RULE_ID,
                category="tooling",
                action="warn",
                match=ignored.match,
                replacement="",
                line=ignored.line,
                column=ignored.column,
                start=ignored.start,
                end=ignored.end,
                source="allow-comment",
                message=ignored.message,
            )
        )

    redacted = _apply_edits(text, _edits_for(findings))

    include_opf = active_options.include_opf or active_policy.opf_backend.enabled
    opf_device = active_options.opf_device or active_policy.opf_backend.device
    opf_bin = active_options.opf_bin or active_policy.opf_backend.bin

    if include_opf:
        opf_rule = Rule(
            id="opf-pii",
            category="pii",
            pattern="",
            replacement="<PRIVATE_DATA>",
            description="OPF changed the text, indicating model-detected PII.",
        )
        opf_result = run_opf(
            text,
            opf_bin=opf_bin,
            device=opf_device,
        )
        if opf_result.changed:
            action = active_policy.action_for(opf_rule)
            findings.append(
                Finding(
                    rule_id=opf_rule.id,
                    category=opf_rule.category,
                    action=action,
                    match="<OPF_DETECTED_PII>",
                    replacement="<PRIVATE_DATA>",
                    line=1,
                    column=1,
                    start=0,
                    end=0,
                    source="opf",
                    message="OPF redacted one or more spans.",
                )
            )
            if action in {"redact", "block"}:
                redacted = run_opf(
                    redacted,
                    opf_bin=opf_bin,
                    device=opf_device,
                ).redacted_text
        elif opf_result.available and opf_result.error:
            findings.append(
                Finding(
                    rule_id="opf-error",
                    category="tooling",
                    action="warn",
                    match="opf",
                    replacement="",
                    line=1,
                    column=1,
                    start=0,
                    end=0,
                    source="opf",
                    message=opf_result.error,
                )
            )
        elif not opf_result.available:
            findings.append(
                Finding(
                    rule_id="opf-unavailable",
                    category="tooling",
                    action="warn",
                    match="opf",
                    replacement="",
                    line=1,
                    column=1,
                    start=0,
                    end=0,
                    source="opf",
                    message=opf_result.error,
                )
            )

    findings.sort(key=lambda item: (item.line, item.column, item.rule_id))
    return GuardResult(text=text, redacted_text=redacted, findings=findings)


def redact_text(text: str, policy: Policy | None = None, options: ScanOptions | None = None) -> str:
    return scan_text(text, policy=policy, options=options).redacted_text


def _allowed_by_superstring_value(
    allow_values: set[str],
    matched: str,
    line_text: str,
    line_start: int,
    start: int,
    end: int,
) -> str | None:
    """Clear a match covered by a longer known-public allow value.

    The match span must sit inside an occurrence of the allow value on its own
    line; an identical match outside the literal (even on the same line) is
    not cleared.
    """
    for value in allow_values:
        if matched not in value:
            continue
        idx = line_text.find(value)
        while idx != -1:
            value_start = line_start + idx
            if value_start <= start and end <= value_start + len(value):
                return "allow-value"
            idx = line_text.find(value, idx + 1)
    return None


def _line_starts(text: str) -> list[int]:
    starts = [0]
    for match in re.finditer("\n", text):
        starts.append(match.end())
    return starts


def _line_at(text: str, start: int, end: int) -> str:
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", end)
    return text[line_start:] if line_end == -1 else text[line_start:line_end]


def _line_for_offset(starts: list[int], offset: int) -> int:
    return bisect.bisect_right(starts, offset)


def _allow_comments_by_line(text: str) -> tuple[dict[int, set[str]], set[str], list[_IgnoredFileAllow]]:
    """Parse allow comments. Returns (line-scoped tokens, file-scoped tokens, ignored file markers).

    Line-scoped: `<!-- content-guard: allow <rule-id> -->` applies to the
    comment's line and the line after (preserves existing semantics).

    File-scoped: `<!-- content-guard: allow <rule-id> file -->` applies to the
    entire file when the marker is in a comment or directive position
    (line-leading, after a comment prefix, or an HTML comment on its own
    line). A file-scoped marker inside a string literal is ignored and
    reported as a warning finding.
    """
    allowed: dict[int, set[str]] = {}
    file_allows: set[str] = set()
    ignored: list[_IgnoredFileAllow] = []
    open_quote: str | None = None
    offset = 0
    for line_no, line in enumerate(text.splitlines(keepends=True), 1):
        body = line.rstrip("\r\n")
        for match in ALLOW_RE.finditer(body):
            token = match.group(1)
            file_scope = match.group(2) == "file"
            if not file_scope:
                allowed.setdefault(line_no, set()).add(token)
                allowed.setdefault(line_no + 1, set()).add(token)
                continue
            inside_string = _quote_at(body, match.start(), open_quote) is not None
            if inside_string:
                ignored.append(
                    _IgnoredFileAllow(
                        line=line_no,
                        column=match.start() + 1,
                        start=offset + match.start(),
                        end=offset + match.end(),
                        match=match.group(0),
                        message=IGNORED_FILE_ALLOW_IN_STRING,
                    )
                )
                continue
            if _is_directive_position(body, match.start(), open_quote):
                file_allows.add(token)
                continue
            ignored.append(
                _IgnoredFileAllow(
                    line=line_no,
                    column=match.start() + 1,
                    start=offset + match.start(),
                    end=offset + match.end(),
                    match=match.group(0),
                    message=IGNORED_FILE_ALLOW_NOT_DIRECTIVE,
                )
            )
        open_quote = _quote_at(body, len(body), open_quote)
        offset += len(line)
    return allowed, file_allows, ignored


def _step_quote(text: str, index: int, quote: str | None, escape: bool) -> tuple[int, str | None, bool]:
    """Advance one character (or quote token) of string-literal state."""
    if index >= len(text):
        return index, quote, False
    char = text[index]
    if quote is not None:
        if escape:
            return index + 1, quote, False
        if char == "\\" and len(quote) == 1:
            return index + 1, quote, True
        if text.startswith(quote, index):
            return index + len(quote), None, False
        return index + 1, quote, False
    if text.startswith(_TRIPLE_QUOTES, index):
        return index + 3, text[index : index + 3], False
    if char in _SINGLE_QUOTES:
        return index + 1, char, False
    return index + 1, None, False


def _is_line_comment_start(text: str, index: int) -> bool:
    rest = text[index:]
    if rest.startswith("//") or rest.startswith("#"):
        return True
    return bool(rest.startswith("--") and (len(rest) == 2 or rest[2].isspace()))


def _quote_at(line: str, until: int, open_quote: str | None) -> str | None:
    """Return the open quote at `until`, tracking strings and skipping line comments."""
    quote = open_quote
    escape = False
    index = 0
    limit = min(until, len(line))
    while index < limit:
        if quote is None and _is_line_comment_start(line, index):
            break
        if quote is None and line.startswith("<!--", index):
            closer = line.find("-->", index + 4)
            if closer == -1 or closer >= limit:
                break
            index = closer + 3
            continue
        index, quote, escape = _step_quote(line, index, quote, escape)
    return quote


def _is_directive_position(line: str, match_start: int, open_quote: str | None) -> bool:
    """True when a file-scoped marker sits in a comment or directive position."""
    if _quote_at(line, match_start, open_quote) is not None:
        return False
    before = line[:match_start]
    if not before.strip():
        return True
    stripped = before.lstrip()
    if stripped.startswith(_LINE_START_COMMENT_PREFIXES):
        return True
    quote = open_quote
    escape = False
    index = 0
    while index < match_start:
        if quote is None and line.startswith(_TRAILING_COMMENT_PREFIXES, index):
            return True
        if quote is None and line.startswith("--", index):
            after = index + 2
            if after >= match_start or line[after].isspace():
                return True
        index, quote, escape = _step_quote(line, index, quote, escape)
    return False


def _allowed_by(
    rule_id: str,
    category: str,
    line: int,
    allow_by_line: dict[int, set[str]],
    file_allows: set[str],
) -> str | None:
    # Findings are displayed as `category/rule-id` (e.g. `secret/bearer-token`),
    # so accept that form alongside the bare rule id - a copied display id must
    # not produce a silently dead allow comment.
    accepted = (rule_id, f"{category}/{rule_id}")
    if "all" in file_allows:
        return "all"
    for token in accepted:
        if token in file_allows:
            return token
    tokens = allow_by_line.get(line, set())
    if "all" in tokens:
        return "all"
    for token in accepted:
        if token in tokens:
            return token
    return None


def _skipped_ranges(text: str, options: ScanOptions) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    lines = text.splitlines(keepends=True)

    if not options.scan_frontmatter and lines and lines[0].strip() == "---":
        end = len(lines[0])
        for line in lines[1:]:
            end += len(line)
            if line.strip() == "---":
                ranges.append((0, end))
                break

    if not options.scan_code_blocks:
        in_fence = False
        fence_start = 0
        current = 0
        for line in lines:
            stripped = line.lstrip()
            if stripped.startswith("```") or stripped.startswith("~~~"):
                if not in_fence:
                    in_fence = True
                    fence_start = current
                else:
                    ranges.append((fence_start, current + len(line)))
                    in_fence = False
            current += len(line)
        if in_fence:
            ranges.append((fence_start, len(text)))

    current = 0
    for line in lines:
        if "content-guard:" in line:
            ranges.append((current, current + len(line)))
        current += len(line)

    return ranges


def _inside_ranges(start: int, end: int, ranges: list[tuple[int, int]]) -> bool:
    return any(start < range_end and end > range_start for range_start, range_end in ranges)


def _overlaps(start: int, end: int, occupied: list[tuple[int, int]]) -> bool:
    return any(start < prev_end and end > prev_start for prev_start, prev_end in occupied)


def _edits_for(findings: list[Finding]) -> list[TextEdit]:
    return [
        TextEdit(finding.start, finding.end, finding.replacement)
        for finding in findings
        if finding.redacts and finding.start < finding.end
    ]


def _apply_edits(text: str, edits: list[TextEdit]) -> str:
    result = text
    for edit in sorted(edits, key=lambda item: item.start, reverse=True):
        result = result[: edit.start] + edit.replacement + result[edit.end :]
    return result
