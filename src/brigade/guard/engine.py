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
    occupied: list[tuple[int, int, str]] = []
    root_spans: list[tuple[int, int]] = []
    js_fields: set[int] | None = None

    for rule in active_policy.all_rules():
        is_root_path = rule.id == "root-path" and not any(rule is custom for custom in active_policy.custom_rules)
        regex = re.compile(rule.pattern, rule.flags)
        for match in regex.finditer(text):
            start, end = match.span()
            if start == end:
                continue
            if rule.id == "email" and active_policy.allows_agent_coauthor_email(
                match.group(0), _line_at(text, start, end)
            ):
                continue
            if rule.id == "private-hostname" and not any(
                rule is custom_rule for custom_rule in active_policy.custom_rules
            ):
                if js_fields is None:
                    js_fields = set()
                    _js_template_code_position(text, field_positions=js_fields)
                if not _hostname_context(text, start, end, js_fields):
                    continue
            if _inside_ranges(start, end, skipped_ranges):
                continue
            # Earlier detectors can occupy part of an expanded root path.
            # Retain its independent decision when it fully encloses them;
            # ordinary and partial-overlap precedence stays unchanged.
            if any(
                start < prev_end
                and end > prev_start
                and not (is_root_path and start <= prev_start and prev_end <= end)
                and not (
                    rule.category in {"secret", "pii"}
                    and rule.category != prev_category
                    and any(
                        root_start <= min(start, prev_start) and max(end, prev_end) <= root_end
                        for root_start, root_end in root_spans
                    )
                )
                for prev_start, prev_end, prev_category in occupied
            ):
                continue
            # Keep nested secret and PII decisions independent of root policy.
            if any(
                start < root_end
                and end > root_start
                and not (rule.category in {"secret", "pii"} and root_start <= start and end <= root_end)
                for root_start, root_end in root_spans
            ):
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
            if is_root_path:
                root_spans.append((start, end))
            else:
                occupied.append((start, end, rule.category))

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

    redacted = _apply_edits(text, _edits_for(findings, root_spans))

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


# An f-string opening quote with no other quote between it and the field.
_FSTRING_OPEN = re.compile(r"""(?<!\w)(?:[rR]?[fF]|[fF][rR])(?:\"\"\"|'''|"|')[^"'\n]*$""")
# Positions where prose does not put an f-string: line start, '=', a
# `return` statement, a call paren right after a name, or a bracket opened
# after '=' or '('.
_FSTRING_POSITION = re.compile(r"(?:^|=|(?:^|;)\s*return|\w\(|[=(]\s*[\[{])\s*$")
# The rest of a field after its expression: '=', a conversion, a format spec
# with at most simple nested fields, then the closing brace.
_FIELD_CLOSE = re.compile(r"""\s*=?\s*(?:![rsa])?(?::(?:[^{}"'\n]|\{[A-Za-z_][\w.]*\})*)?\}""")
_HOSTNAME_KEY = r"(?:[a-z][a-z0-9]*[_-])*(?:host|hostname|server|endpoint|domain|url|address)"


def _whole_fstring_field(before: str, after: str) -> bool:
    """Whether a match is the entire expression of one f-string field."""
    opener = before.rstrip()
    braces = len(opener) - len(opener.rstrip("{"))
    # '{{' is a literal brace, so only an odd run opens a replacement field.
    if braces % 2 == 0:
        return False
    stem = opener[:-braces]
    match = _FSTRING_OPEN.search(stem)
    # The prefix must start a literal in an expression position, not in prose,
    # another string, or a comment, and the literal must still be open at the brace.
    if match is None or not _FSTRING_POSITION.search(stem[: match.start()]):
        return False
    # `_quote_at` stops at '#' and '//' comments but not at a trailing '/*'.
    if "/*" in stem[: match.start()]:
        return False
    if stem.lstrip().startswith(_LINE_START_COMMENT_PREFIXES) or _quote_at(stem, match.start(), None) is not None:
        return False
    return _quote_at(stem, len(stem), None) is not None and bool(_FIELD_CLOSE.match(after))


_JS_WORD = re.compile(r"[A-Za-z_$][\w$]*")
_JS_NUMBER = re.compile(r"[0-9]+(?:\.[0-9]+)?")
_JS_LINE_END = re.compile(r"[\r\n\u2028\u2029]")
# Exhaustive reserved tokens, including strict/future reserved words. Only the
# explicit roles below may consume them; unsupported grammar retains detection.
_JS_RESERVED_WORDS = frozenset(
    "await break case catch class const continue debugger default delete do else enum export extends false "
    "finally for function if implements import in instanceof interface let new null package private protected "
    "public return static super switch this throw true try typeof var void while with yield".split()
)
_JS_VALUE_WORDS = frozenset({"false", "null", "super", "this", "true"})


class _JSCode:
    """Locally proven lexical roles; uncertainty never consumes a literal."""

    def __init__(self, kind: str = "root", anchor: bool = False):
        self.kind = kind
        self.operand = True
        self.anchor = anchor
        self.previous = ""
        self.member = False
        self.async_head = False
        self.tag = False
        self.binding = False
        self.parameters = False
        self.body = ""
        self.questions = 0
        self.control = False
        self.key = kind == "object"
        self.bindings: set[str] = set()
        self.keywords: frozenset[str] = frozenset()

    def value(self, token: str) -> None:
        self.operand = False
        self.previous = token
        self.async_head = False
        self.member = False
        self.binding = False
        self.parameters = False
        self.body = ""
        self.control = False


def _js_regex_end(text: str, start: int) -> int | None:
    index = start + 1
    in_class = False
    while index < len(text):
        char = text[index]
        if char in "\r\n\u2028\u2029":
            return None
        if char == "\\":
            if index + 1 < len(text) and text[index + 1] in "\r\n\u2028\u2029":
                return None
            index += 2
            continue
        if char == "[":
            in_class = True
        elif char == "]":
            in_class = False
        elif char == "/" and not in_class:
            index += 1
            while index < len(text) and text[index] in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ":
                index += 1
            return index
        index += 1
    return None


def _js_template_code_position(
    prefix: str, *, template_text: bool = False, field_positions: set[int] | None = None
) -> bool:
    """Recognize bounded expression positions with one iterative lexical walk.

    The same token transitions handle code before a template and code inside
    completed template literals. Unknown syntax retains hostname detection.
    """
    if prefix.lstrip().startswith(("<!--", ";", "*")) or (
        field_positions is None and prefix.lstrip().startswith(("//", "--", "#"))
    ):
        return False
    frames: list[_JSCode | str] = [_JSCode()]
    contextual: dict[str, list[bool]] = {"await": [], "yield": [], "let": []}
    template_depth = 0
    eligible_templates: set[int] = set()

    def leave_scope(code: _JSCode) -> None:
        for contextual_name in code.bindings - code.keywords:
            contextual[contextual_name].pop()
        for contextual_name in code.keywords:
            contextual[contextual_name].pop()

    def binding_follows(index: int) -> bool:
        # Sloppy-script 'let' can be an identifier. Prove a declaration's next
        # binding token rather than guessing from the word alone.
        while index < len(prefix):
            if prefix[index].isspace():
                index += 1
            elif prefix.startswith("/*", index):
                end = prefix.find("*/", index + 2)
                if end == -1:
                    return False
                index = end + 2
            else:
                following = _JS_WORD.match(prefix, index)
                return following is not None and (
                    following.group() not in _JS_RESERVED_WORDS or following.group() in {"await", "yield", "let"}
                )
        return False

    index = 0
    first_word = True
    while index < len(prefix):
        frame = frames[-1]
        char = prefix[index]
        if isinstance(frame, str):
            if char == "\\":
                index += 2
            elif frame != "`" and char in "\r\n":
                return False
            elif char == frame:
                if frame == "`":
                    template_depth -= 1
                    eligible_templates.discard(len(frames) - 1)
                frames.pop()
                parent = frames[-1]
                assert isinstance(parent, _JSCode)
                parent.value("literal")
                index += 1
            elif frame == "`" and prefix.startswith("${", index):
                if field_positions is not None and len(frames) - 1 in eligible_templates:
                    position = index + 2
                    while position < len(prefix) and prefix[position].isspace():
                        position += 1
                    field_positions.add(position)
                frames.append(_JSCode("interpolation", True))
                index += 2
            else:
                index += 1
            continue
        # An expression-bodied async arrow ends at its enclosing expression's
        # delimiter. Its keyword role must not leak into the following code.
        if frame.kind == "async-arrow" and char in ";,)]}":
            if frame.operand or frame.questions:
                return False
            frames.pop()
            leave_scope(frame)
            parent = frames[-1]
            assert isinstance(parent, _JSCode)
            parent.value("value")
            continue
        if char.isspace():
            index += 1
            continue
        # Preserve general-text comment exclusions, including the established
        # conservative interpretation of whitespace-followed '--'.
        if (
            char == "#"
            or prefix.startswith("//", index)
            or (prefix.startswith("--", index) and (index + 2 == len(prefix) or prefix[index + 2].isspace()))
        ):
            if field_positions is None:
                return False
            line_end = _JS_LINE_END.search(prefix, index)
            if line_end is None:
                return False
            index = line_end.end()
            continue
        if prefix.startswith("/*", index):
            end = prefix.find("*/", index + 2)
            if end == -1:
                return False
            index = end + 2
            continue
        # A pending binding or control delimiter cannot start a regex/string.
        # Do not consume quotes after malformed or unproven keyword contexts.
        if frame.control and char != "(":
            return False
        if frame.binding and _JS_WORD.match(prefix, index) is None:
            if not ((frame.parameters and char in "(*") or (frame.body and char == "{")):
                return False
        if char in "\"'`":
            if char == "`":
                if (
                    field_positions is not None
                    and template_depth == 0
                    and not frame.member
                    and (
                        (frame.operand and frame.anchor)
                        or (frame.tag and (frame.anchor or not prefix[index - 1 : index].isspace()))
                        or (first_word and not prefix[:index].strip())
                    )
                ):
                    eligible_templates.add(len(frames))
                template_depth += 1
            frames.append(char)
            index += 1
            first_word = False
            continue
        if char == "/":
            if frame.operand:
                regex_end = _js_regex_end(prefix, index)
                if regex_end is None:
                    return False
                frame.value("literal")
                index = regex_end
            else:
                frame.operand = True
                frame.previous = "/"
                frame.async_head = False
                frame.tag = False
                index += 2 if prefix.startswith("/=", index) else 1
            continue
        if word := _JS_WORD.match(prefix, index):
            token = word.group()
            if frame.member or (frame.kind == "object" and frame.key and frame.operand):
                frame.value(token)
            elif token in {"const", "let", "var"} and frame.previous in {"", ";", "("}:
                if token == "let" and not binding_follows(word.end()):
                    if not contextual["let"] or not contextual["let"][-1] or not frame.operand:
                        return False
                    frame.value(token)
                else:
                    frame.binding = True
                    frame.operand = True
                    frame.previous = token
            elif frame.binding:
                if token in _JS_RESERVED_WORDS and token not in {"await", "yield", "let"}:
                    return False
                if token in contextual and token not in frame.bindings:
                    frame.bindings.add(token)
                    if token not in frame.keywords:
                        contextual[token].append(True)
                body, parameters = frame.body, frame.parameters
                frame.value(token)
                frame.body, frame.parameters = body, parameters
            elif token in {"function", "class"}:
                if not frame.operand:
                    return False
                frame.body = "expression" if frame.anchor and frame.operand else "declaration"
                frame.parameters = token == "function"
                frame.binding = True
                frame.previous = token
                frame.operand = True
            elif token in {"if", "while", "for", "with", "switch", "catch"}:
                if not frame.operand:
                    return False
                frame.previous = token
                frame.operand = True
                frame.control = True
                frame.tag = False
            elif token in {"return", "throw"}:
                if frame.previous not in {"", ";"}:
                    return False
                frame.operand = frame.anchor = True
                frame.previous = token
                frame.tag = False
            elif token in {"delete", "void", "typeof", "new"}:
                if not frame.operand:
                    return False
                frame.previous = token
                frame.tag = False
            elif token in {"in", "instanceof"} or (token == "of" and frame.kind == "for" and not frame.operand):
                if frame.operand:
                    return False
                frame.operand = True
                frame.previous = token
                frame.tag = False
            elif token in {"await", "yield", "let"}:
                # A contextual word is an identifier only with a proven binding
                # in the active lexical scopes. A generator's yield role wins
                # over any binding outside that function. Unknown roles retain
                # detection before a slash can consume literal delimiters.
                if not contextual[token] or not contextual[token][-1]:
                    return False
                if not frame.operand:
                    return False
                frame.value(token)
            elif token in {"else", "do"} and frame.operand:
                frame.previous = token
                frame.tag = False
            elif token in _JS_VALUE_WORDS:
                if not frame.operand:
                    return False
                tag = frame.anchor
                frame.value(token)
                frame.tag = tag
            elif token in _JS_RESERVED_WORDS:
                return False
            else:
                # Other contextual spellings (as/from/get/set/using) are
                # identifiers here. Their unrecognized grammar cannot supply
                # an operand or statement role through a reserved-word guess.
                if not frame.operand:
                    return False
                tag = frame.anchor or (first_word and len(frames) == 1)
                frame.value(token)
                frame.async_head = token == "async"
                frame.tag = tag
            first_word = False
            index = word.end()
            continue
        if char in "0123456789":
            number = _JS_NUMBER.match(prefix, index)
            assert number is not None
            frame.value("number")
            frame.tag = False
            index = number.end()
            continue
        pair = prefix[index : index + 2]
        if pair in {"++", "--"}:
            frame.previous = pair
            frame.tag = False
            index += 2
            continue
        if char == "." or pair == "?.":
            if frame.operand:
                return False
            frame.member = True
            frame.previous = "."
            index += 2 if pair == "?." else 1
            continue
        if pair == "=>":
            if frame.previous == "async-parameters":
                child = _JSCode("async-arrow", True)
                child.body = "expression"
                child.keywords = frozenset({"await"})
                contextual["await"].append(False)
                frames.append(child)
                index += 2
                continue
            frame.operand = frame.anchor = True
            frame.body = "expression"
            frame.previous = "=>"
            frame.tag = False
            index += 2
            continue
        if char == "(":
            if frame.parameters:
                kind = "parameters"
                frame.parameters = False
            elif frame.async_head:
                kind = "async-parameters"
            elif frame.control:
                kind = "for" if frame.previous == "for" else "control"
            else:
                kind = "group" if frame.operand else "call"
            frame.control = False
            frames.append(_JSCode(kind, kind in {"call", "async-parameters"} or frame.anchor))
            index += 1
            continue
        if char == "[":
            frames.append(_JSCode("array", frame.operand or frame.anchor))
            index += 1
            continue
        if char == "{":
            if frame.body:
                kind = "expression-body" if frame.body.endswith("expression") else "block"
            elif frame.operand and (frame.anchor or frame.kind in {"group", "interpolation"}):
                kind = "object"
            elif frame.operand:
                kind = "block"
            else:
                return False
            child = _JSCode(kind)
            if frame.body.startswith("generator-"):
                child.keywords = frozenset({"yield"})
                contextual["yield"].append(False)
            frames.append(child)
            index += 1
            continue
        if char in ")]}":
            if len(frames) == 1:
                return False
            kind = frame.kind
            allowed = {
                ")": {"parameters", "async-parameters", "for", "control", "group", "call"},
                "]": {"array"},
                "}": {"object", "expression-body", "block", "interpolation"},
            }
            if kind not in allowed[char]:
                return False
            frames.pop()
            leave_scope(frame)
            parent = frames[-1]
            index += 1
            if kind == "interpolation":
                assert parent == "`"
                continue
            assert isinstance(parent, _JSCode)
            if kind == "parameters":
                parent.previous = ")"
            elif kind == "async-parameters":
                parent.value("async-parameters")
                parent.tag = False
            elif kind in {"control", "for", "block"}:
                parent.operand = True
                parent.anchor = False
                parent.previous = ";"
                parent.tag = False
                parent.body = ""
            else:
                parent.value(")" if char == ")" else "value")
                parent.tag = False
            continue
        if char == "?":
            if frame.operand or not frame.anchor:
                return False
            frame.questions += 1
            frame.operand = True
        elif char == ":":
            if frame.questions:
                frame.questions -= 1
                frame.anchor = frame.operand = True
            elif frame.kind == "object" and frame.key and not frame.operand:
                frame.key = False
                frame.anchor = frame.operand = True
            else:
                return False
        elif char == ";":
            frame.operand = True
            frame.anchor = False
            frame.binding = False
            frame.body = ""
        elif char == ",":
            if frame.kind not in {"call", "array", "object", "parameters", "for"} and not frame.anchor:
                return False
            frame.operand = True
            frame.anchor = frame.kind in {"call", "array"}
            frame.key = frame.kind == "object"
        elif char == "=":
            frame.operand = frame.anchor = True
            index += 1 if pair in {"==", "!="} else 0
        elif char == "*" and frame.previous == "function" and frame.body:
            frame.body = "generator-" + frame.body
        elif char in "+-*%&|!~^<>":
            frame.operand = True
        else:
            return False
        frame.previous = char
        frame.async_head = False
        frame.tag = False
        frame.member = False
        index += 1
    if template_text:
        return frames[-1] == "`"
    if any(isinstance(frame, str) or frame.kind == "interpolation" for frame in frames):
        return False
    frame = frames[-1]
    assert isinstance(frame, _JSCode)
    if frame.member:
        return False
    if frame.operand and frame.anchor:
        return True
    if frame.tag:
        return frame.anchor or not prefix[-1:].isspace()
    return not prefix.strip()


def _whole_js_template_field(before: str, after: str) -> bool:
    """Whether a match is the entire expression of an unescaped `${...}` field."""
    opener = before.rstrip()
    if not opener.endswith("${") or not re.match(r"\s*\}", after):
        return False
    stem = opener[:-2]
    if (len(stem) - len(stem.rstrip("\\"))) % 2:
        return False
    # A quote may open a literal inside an earlier expression. Keep detecting
    # its contents rather than treating its `${...}` text as a template field.
    match = re.search(r"""`(?:\\.|[^`\\"'])*$""", stem)
    if match is None:
        return False
    if not _js_template_code_position(stem[: match.start()]):
        return False
    # Reuse the same lexical walk through this field: interpolation comments
    # and literals cannot lend their `${...}` text a template-text role.
    return _js_template_code_position(stem, template_text=True)


def _hostname_option_context(before: str, after: str) -> bool:
    """Recognize this option's command context, not every later shell argument."""
    if not re.search(r"(?:^|\s)--?" + _HOSTNAME_KEY + r"=\s*$", before, re.IGNORECASE):
        return False
    # Retain the original simple-option recall, including argument punctuation.
    if not re.search(r"[(){}\[\];]", before) and not re.match(r"\s*[,)}\];]", after):
        return True
    if re.match(r"\s*,", after):
        return False
    line = before + "hostname" + after
    masked = list(line)
    quote: str | None = None
    escaped = False
    word_boundary = True
    for index, char in enumerate(line):
        if escaped:
            masked[index] = "x"
            escaped = False
            word_boundary = False
        elif char == "\\" and quote != "'":
            masked[index] = "x"
            escaped = True
            word_boundary = False
        elif quote is not None:
            masked[index] = "x"
            word_boundary = False
            if char == quote:
                quote = None
        elif char in "\"'`":
            masked[index] = "x"
            quote = char
            word_boundary = False
        elif char == "#" and word_boundary:
            masked[index:] = " " * (len(line) - index)
            break
        else:
            word_boundary = char.isspace()
    tokens = list(re.finditer(r"\$\(|&&|\|\||[();{}|&]|[^\s();{}|&$]+|\$(?!\()", "".join(masked)))
    # A complete earlier word expansion is argument data. Balance delimiters
    # lexically without interpreting parameter or arithmetic expressions.
    pairs: dict[int, int] = {}
    openings: list[tuple[str, int]] = []
    for index, token in enumerate(tokens):
        value = token.group()
        if value in {"(", "$(", "{"}:
            openings.append(("}" if value == "{" else ")", index))
        elif value in {")", "}"}:
            if openings and openings[-1][0] == value:
                pairs[openings.pop()[1]] = index
            else:
                openings.clear()
    words: list[str] = []
    groups: list[tuple[str, list[str], bool, bool]] = []
    bracket_in_command = False
    found = False
    previous = ""
    skip_until = -1
    for index, token in enumerate(tokens):
        if index <= skip_until:
            continue
        value = token.group()
        expansion = value == "$(" or (value == "{" and line[max(0, token.start() - 1) : token.start()] == "$")
        if not found and expansion:
            closing = pairs.get(index)
            if closing is not None and tokens[closing].end() <= len(before):
                words.append("word")
                previous = "word"
                skip_until = closing
                continue
            if value == "{" or line[token.start() : token.start() + 3] == "$((":
                return False  # The option itself is inside an expression.
        if value in {"(", "$(", "{"}:
            if not found and value != "$(" and words:
                return False  # callback( ... ) and callback ( ... ) are calls.
            if not found and value == "{" and (token.end() == len(line) or not line[token.end()].isspace()):
                return False
            groups.append((value, words, not found, bracket_in_command))
            words = []
            bracket_in_command = False
        elif value in {")", "}"}:
            if not groups:
                return False
            opening, outer_words, enclosing, outer_bracket = groups.pop()
            if (value == "}") != (opening == "{"):
                return False
            if enclosing and value == "}" and previous not in {";", "&"}:
                return False
            if not words and previous not in {")", "}", ";", "&"}:
                return False
            words = outer_words + ["group"]
            bracket_in_command = outer_bracket
            if found and not groups:
                return True  # Later commands cannot invalidate this context.
        elif value in {";", "&&", "||", "|", "&"}:
            if not words:
                return False
            words = []
            bracket_in_command = False
        else:
            if not found and re.search(r"[\[\]]", value):
                bracket_in_command = True
            if token.start() <= len(before) < token.end():
                if bracket_in_command:
                    return False
                found = True
                if not groups:
                    return not bool(re.match(r"\s*[,)}\]]", after))
            words.append(value)
        previous = value
    return False


def _normalized_shell_assignments(before: str) -> str:
    """Treat complete declaration assignment values as opaque shell words."""
    if not re.match(r"\s*(?:-[ \t]+)?(?:export|readonly|declare|local|@?set)\b", before, re.IGNORECASE):
        return before

    def closed(index: int, delimiter: str) -> int | None:
        delimiters = [delimiter]
        while index < len(before):
            delimiter = delimiters[-1]
            char = before[index]
            if char == delimiter:
                delimiters.pop()
                index += 1
                if not delimiters:
                    return index
                continue
            if delimiter == "'":
                index += 1
                continue
            if char == "\\":
                index += 2
                continue
            nested = None
            width = 1
            if before.startswith("${", index):
                nested, width = "}", 2
            elif before.startswith("$(", index):
                nested, width = ")", 2
            elif char in "\"'`" and not (delimiter == '"' and char == "'"):
                nested = char
            elif char == "(" and delimiter == ")":
                nested = delimiter
            if nested is not None:
                delimiters.append(nested)
                index += width
            else:
                index += 1
        return None

    pieces: list[str] = []
    position = 0
    for assignment in re.finditer(r"(?<!\S)[a-z_][a-z0-9_]*=", before, re.IGNORECASE):
        if assignment.start() < position:
            continue
        index = assignment.end()
        while index < len(before) and not before[index].isspace():
            char = before[index]
            nested = None
            width = 1
            if before.startswith("${", index):
                nested, width = "}", 2
            elif before.startswith("$(", index):
                nested, width = ")", 2
            elif char in "\"'`":
                nested = char
            elif char == "\\":
                if index + 1 == len(before):
                    return before
                index += 2
                continue
            # Attached braces are shell word data, including any brace left
            # after a parameter expansion closes. Command operators are not.
            elif char in ";|&()":
                return before
            if nested is not None:
                parsed = closed(index + width, nested)
                if parsed is None:
                    return before
                index = parsed
            else:
                index += 1
        pieces.append(before[position : assignment.end()])
        # Every successfully consumed word is opaque, including literal braces.
        # Keep the final empty host assignment open for the hostname matcher.
        pieces.append("value" if index > assignment.end() else "")
        position = index
    pieces.append(before[position:])
    return "".join(pieces)


def _hostname_context(text: str, start: int, end: int, js_fields: set[int] | None = None) -> bool:
    """Keep textual hosts while avoiding unquoted attribute expressions.

    This is a lexical heuristic, not a source parser. Bare dotted identifiers,
    call arguments, interpolation expressions and table names are intentionally
    skipped. Quoted strings, URLs, prose and host-related config values remain
    detectable. A bare hostname with no textual context is ambiguous and skipped.
    """
    line = _line_at(text, start, end)
    column = start - (text.rfind("\n", 0, start) + 1)
    before, after = line[:column], line[column + end - start :]
    if re.search(r"[a-z][a-z0-9+.-]*://[^\s]*$", before, re.IGNORECASE):
        # A replacement field holding only the host is an expression even in a
        # formatted URL (#1918). Any other URL context keeps matching.
        if _whole_fstring_field(before, after):
            return False
        if js_fields is not None:
            return not (start in js_fields and bool(re.match(r"\s*\}", after)))
        return not _whole_js_template_field(text[:start], after)
    # These delimiters introduce expressions, including expressions inside
    # interpolated strings. Check them before accepting a surrounding quote.
    if re.search(r"\bf[\"'][^\n]*\{[^}]*$", _normalized_shell_assignments(before), re.IGNORECASE):
        return False
    if before.rstrip().endswith(("[", "{")):
        return False
    if _quote_at(line, column, None) is not None:
        return True
    # Prose punctuation is valid around a host, including commas and
    # parenthesized examples. An identifier immediately before '(' is a call.
    # Complete earlier shell assignment words are opaque to code keywords.
    if re.search(r"\b(?:return|if|const|let|var|assert|import)\b", _normalized_shell_assignments(before)):
        return False
    if re.fullmatch(r"\s*(?:[a-z]+[,:]?\s+)+(?:\(\s*)?", before, re.IGNORECASE):
        return True
    if before.rstrip().endswith("("):
        return False
    if re.match(r"\s*(?:=|\?)", after):
        return False
    # Host-related keys after a YAML sequence marker or shell declaration
    # prefixes (export/readonly, and Windows batch set) are configuration.
    # Require whitespace after '-' so a hyphenated key is not a sequence item.
    if re.fullmatch(
        r"\s*(?:-[ \t]+)?(?:(?:(?:export|readonly|declare|(?P<local_declaration>local))(?:[ \t]+-[a-z]+)*"
        r"|@?set(?:[ \t]+/a)?)[ \t]+"
        r"(?:[a-z_][a-z0-9_]*=(?:[^\s\"'`;|&(){}]|'[^'\r\n]*'|\"[^\"\r\n]*\")*[ \t]+)*)*"
        + _HOSTNAME_KEY
        + r"(?(local_declaration)=|\s*[:=]\s*)",
        _normalized_shell_assignments(before),
        re.IGNORECASE,
    ):
        return True
    # Separators and balanced shell groups bound the active option; punctuation
    # inside earlier arguments must not hide it or turn a call into a command.
    if _hostname_option_context(before, after):
        return True
    if re.search(r"[(){}\[\];=]|\b(?:return|const|let|var|assert|import)\b", before):
        return False
    if re.match(r"\s*[,)}\];]", after):
        return False
    return bool(re.search(r"[a-z]{2,}\s+$", before, re.IGNORECASE) or re.match(r"\s+[a-z]{2,}\b", after, re.IGNORECASE))


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


def _edits_for(findings: list[Finding], root_spans: list[tuple[int, int]]) -> list[TextEdit]:
    # Findings retain original offsets, but overlapping replacements cannot
    # both use those offsets. A masking root edit covers all contained edits.
    masked_roots = [
        finding
        for finding in findings
        if finding.rule_id == "root-path"
        and finding.category == "infrastructure"
        and finding.redacts
        and (finding.start, finding.end) in root_spans
    ]
    edits = [
        TextEdit(finding.start, finding.end, finding.replacement)
        for finding in findings
        if finding.redacts
        and finding.start < finding.end
        and not any(
            root is not finding and root.start <= finding.start and finding.end <= root.end for root in masked_roots
        )
    ]
    # Independent nested detectors can overlap without either decision being
    # discarded. Mask their union once, using original source offsets. Keep
    # adjacent edits separate and preserve ordinary non-root precedence.
    selected: list[TextEdit] = []
    for edit in sorted(edits, key=lambda item: (item.start, -item.end)):
        if (
            selected
            and edit.start < selected[-1].end
            and any(
                root_start <= selected[-1].start and max(selected[-1].end, edit.end) <= root_end
                for root_start, root_end in root_spans
            )
        ):
            previous = selected[-1]
            selected[-1] = TextEdit(previous.start, max(previous.end, edit.end), previous.replacement)
        else:
            selected.append(edit)
    return selected


def _apply_edits(text: str, edits: list[TextEdit]) -> str:
    result = text
    for edit in sorted(edits, key=lambda item: item.start, reverse=True):
        result = result[: edit.start] + edit.replacement + result[edit.end :]
    return result
