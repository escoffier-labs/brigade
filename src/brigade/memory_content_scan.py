"""Shared prompt-load rules for local connectors and the native hub.

The versioned bundle exports the guard defaults and the Dot injection families.
Hub scanning deliberately uses raw matches without repository context exceptions.
"""

from __future__ import annotations

import json
import bisect
import hashlib
import re
import unicodedata
from importlib.resources import files
from typing import Iterable, Iterator

BIDI_RULE = "bidi-control"
INVISIBLE_RULE = "invisible-character"
JOINER_RULE = "zero-width-joiner"
# U+034F and U+200C arrive in pasted text and carry no payload of their own.
# The scan copies already remove them, so they cannot split a rule match.
# Stored bytes keep them, so they scan private. Prompt recall renders without
# them (memory_hooks and the native prompt_hit).
GRAPHEME_JOINER_RULE = "combining-grapheme-joiner"
NON_JOINER_RULE = "zero-width-non-joiner"
_PRIVATE_CHARACTERS = {"\u034f": GRAPHEME_JOINER_RULE, "\u200c": NON_JOINER_RULE}
# Verdicts, most severe first. Private sits between clean and flagged: it is
# stored, searchable and loaded into local prompts, and refused on egress.
BLOCKED, FLAGGED, PRIVATE, CLEAN = "blocked", "flagged", "private", "clean"
# The refusal code when only the private class withholds text on egress.
PRIVATE_CODE = "memory_content_private"
# Explicit sets shared with the native scanner, independent of runtime Unicode.
# `WHITESPACE` is public: `untrusted` folds the same set.
WHITESPACE = (
    "\t\n\v\f\r\x1c\x1d\x1e\x1f \x85\xa0\u1680"
    + "".join(map(chr, range(0x2000, 0x200B)))
    + "\u2028\u2029\u202f\u205f\u3000"
)
_BOUNDARIES = "\n\v\f\r\x1c\x1d\x1e\x85\u2028\u2029"
_BIDI = frozenset(chr(code) for code in (0x61C, 0x200E, 0x200F, *range(0x202A, 0x202F), *range(0x2066, 0x206A)))
# Unicode 16 Default_Ignorable_Code_Point ranges. Never edit stored bytes.
_IGNORABLE_RANGES = (
    (0xAD, 0xAD),
    (0x34F, 0x34F),
    (0x61C, 0x61C),
    (0x115F, 0x1160),
    (0x17B4, 0x17B5),
    (0x180B, 0x180F),
    (0x200B, 0x200F),
    (0x202A, 0x202E),
    (0x2060, 0x206F),
    (0x3164, 0x3164),
    (0xFE00, 0xFE0F),
    (0xFEFF, 0xFEFF),
    (0xFFA0, 0xFFA0),
    (0xFFF0, 0xFFF8),
    (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A),
    (0xE0000, 0xE0FFF),
)
_IGNORABLE = frozenset(chr(code) for lo, hi in _IGNORABLE_RANGES for code in range(lo, hi + 1))
_INVISIBLE = _IGNORABLE | frozenset("\ufff9\ufffa\ufffb")
_STRIP_IGNORABLE = dict.fromkeys(map(ord, _IGNORABLE))
# The v6 fold stays a scan copy beside NFKC. NFKC can absorb a trailing ASCII
# letter into a composed letter (A + U+0301) and turn a compatibility symbol
# into word characters (U+2122 to TM), which breaks `\b` and `(?!\w)` around
# a match. Scanning both copies makes every verdict at least as severe as v6.
_FOLD_FULLWIDTH = {code: code - 0xFEE0 for code in range(0xFF01, 0xFF5F)} | {0x3000: 0x20}
_LINE_TRANSLATION = dict.fromkeys(map(ord, _BOUNDARIES), "\n")
_SPACE_TRANSLATION = dict.fromkeys(map(ord, WHITESPACE), " ")


# Extended_Pictographic blocks whose characters take U+FE0F for emoji
# presentation, plus the text-default singletons. Shared with the native hub.
_EMOJI_BASE_RANGES = (
    (0xA9, 0xA9),
    (0xAE, 0xAE),
    (0x203C, 0x203C),
    (0x2049, 0x2049),
    (0x2122, 0x2122),
    (0x2139, 0x2139),
    (0x2190, 0x21FF),
    (0x2300, 0x23FF),
    (0x24C2, 0x24C2),
    (0x25A0, 0x25FF),
    (0x2600, 0x27BF),
    (0x2934, 0x2935),
    (0x2B00, 0x2BFF),
    (0x3030, 0x3030),
    (0x303D, 0x303D),
    (0x3297, 0x3297),
    (0x3299, 0x3299),
    (0x1F000, 0x1F2FF),
    (0x1F300, 0x1FAFF),
)


def _emoji_base(char: str) -> bool:
    code = ord(char)
    return any(lo <= code <= hi for lo, hi in _EMOJI_BASE_RANGES)


def _selector_exempt(text: str, index: int) -> bool:
    """U+FE0E or U+FE0F right after an emoji base, or a keycap's U+FE0F.

    U+FE00 to U+FE0D never pick a text or emoji presentation, so after a base
    they would hide data after every visible symbol. They block.
    """
    if not index or text[index] not in "\ufe0e\ufe0f":
        return False
    previous = text[index - 1]
    if _emoji_base(previous):
        return True
    return text[index] == "\ufe0f" and previous in "#*0123456789" and text[index + 1 : index + 2] == "\u20e3"


_BUNDLE_BYTES = files("brigade.guard").joinpath("memory-rules.json").read_bytes()
_BUNDLE = json.loads(_BUNDLE_BYTES.decode("utf-8"))
NFKC_UNICODE = "14.0.0"


def _check_nfkc_pin(bundle: dict) -> None:
    """Refuse a bundle whose NFKC table is not the Unicode version the parity tests cover."""
    if bundle["nfkc"]["unicode"] != NFKC_UNICODE:
        raise ValueError(f"memory scan bundle pins Unicode {bundle['nfkc']['unicode']}, expected {NFKC_UNICODE}")


_check_nfkc_pin(_BUNDLE)


def ruleset_version() -> str:
    """Bundle version and digest, including the pinned Python and Rust sources.

    After scanner edits regenerate with scripts/memory_scan_bundle.py. The
    hub reports the same ID in status.scan.ruleset_version.
    """
    return f"{_BUNDLE['version']}:{hashlib.sha256(_BUNDLE_BYTES).hexdigest()}"


def _assigned_run(spec: str) -> re.Pattern[str]:
    """Runs of code points the bundle's pinned Unicode version assigns."""
    unassigned = "".join(
        "\\U%08X-\\U%08X" % tuple(int(part, 16) for part in item.split("-")) for item in spec.split(",")
    )
    return re.compile(f"[^{unassigned}]+")


# NFKC applies only to code points the pinned Unicode version assigns (14.0.0,
# the Unicode data of Python 3.11). The normalization stability policy keeps
# their NFKC identical in every later Unicode version, so this runtime and the
# native hub's crate agree. Code points the pin leaves unassigned pass through
# unchanged, as an older runtime would treat them.
_NFKC_ASSIGNED = _assigned_run(_BUNDLE["nfkc"]["unassigned"])


def _nfkc(text: str) -> str:
    if text.isascii():
        return text
    return _NFKC_ASSIGNED.sub(lambda run: unicodedata.normalize("NFKC", run.group()), text)


def _compile(items: list[dict], ascii_mode: bool = False) -> list[tuple[str, str, re.Pattern[str]]]:
    return [
        (
            item["id"],
            item["action"],
            re.compile(item["pattern"], (item["flags"] & ~re.UNICODE) | re.ASCII if ascii_mode else item["flags"]),
        )
        for item in items
    ]


_INJECTIONS = _compile(_BUNDLE["injections"]) + _compile(_BUNDLE["injections"], True)
_ACTIONS = {item["id"]: item["action"] for item in _BUNDLE["injections"] + _BUNDLE["rules"]} | {
    BIDI_RULE: "block",
    INVISIBLE_RULE: "block",
    JOINER_RULE: "warn",
    **{rule: "private" for rule in _PRIVATE_CHARACTERS.values()},
}
_RULES = _INJECTIONS + _compile(_BUNDLE["rules"]) + _compile(_BUNDLE["rules"], True)


def invisible_rule(text: str) -> str | None:
    if set(text) & _BIDI:
        return BIDI_RULE
    for index, char in enumerate(text):
        if char == "\ufeff" and index == 0:
            continue
        if char in "\u200c\u200d" or char in _PRIVATE_CHARACTERS:
            continue
        if "\ufe00" <= char <= "\ufe0f" and _selector_exempt(text, index):
            continue
        if char in _INVISIBLE:
            return INVISIBLE_RULE
    return None


def _base_lines(text: str) -> str:
    # Preserve all splitlines boundaries before applying MULTILINE line rules.
    return text.replace("\r\n", "\n").translate(_LINE_TRANSLATION).translate(_STRIP_IGNORABLE)


def _derive_copies(lines: str) -> tuple[str, str, str]:
    normalized = " ".join(part for part in lines.translate(_SPACE_TRANSLATION).split(" ") if part)
    injection_lines = "\n".join(
        " ".join(part for part in line.translate(_SPACE_TRANSLATION).split(" ") if part) for line in lines.split("\n")
    )
    return lines, injection_lines, normalized


def _scan_copies(text: str) -> tuple[str, str, str]:
    """The NFKC line, injection-line and normalized copies."""
    return _derive_copies(_nfkc(_base_lines(text)))


def _scan_copy_sets(text: str) -> tuple[tuple[str, str, str], ...]:
    """The v6 full-width fold copies and the NFKC copies, equal sets merged."""
    base = _base_lines(text)
    # Derive once per distinct line copy: ASCII text costs nothing extra.
    return tuple(_derive_copies(lines) for lines in dict.fromkeys((base.translate(_FOLD_FULLWIDTH), _nfkc(base))))


# Necessary conditions only. These linear searches have no failing suffix on
# their token runs. They avoid retrying a bounded local-part or digit search
# at every dotted word in a 512 KiB source card. IGNORECASE includes Python's
# extra Unicode case folds, so the filter is also a superset of ASCII mode.
_EMAIL_DOMAIN = re.compile(r"@[A-Z0-9.-]{1,256}\.[A-Z]{2,63}", re.IGNORECASE)
# Anchor the necessary domain suffix to its @ and bounded label sequence.
# A reserved word elsewhere cannot make an overlong near-miss viable.
# Omit the suffix boundary so this also admits ASCII matches before Unicode.
_RESERVED_DOMAIN = re.compile(
    r"@(?:[A-Z0-9-]{1,63}\.){0,127}(?:example\.(?:com|org|net)|test|invalid|localhost|local)",
    re.IGNORECASE,
)
_SSH_HOST = re.compile(r"@(?:10\.|172\.(?:1[6-9]|2\d|3[01])\.|192\.168\.|127\.)")
_ASSIGNMENT_START = re.compile(r"\b(?:api[_-]?key|token|secret)\s{0,256}[:=]\s{0,256}", re.IGNORECASE)
_ASSIGNMENT_START_ASCII = re.compile(_ASSIGNMENT_START.pattern, re.IGNORECASE | re.ASCII)
_DIGIT = re.compile(r"[0-9]")
# Every pinned bundle rule needs an ASCII literal letter or digit, except
# us-phone which can consist entirely of Unicode digits and punctuation.
# IGNORECASE admits the four extra Unicode letter folds too. Recheck this
# necessary condition whenever the versioned bundle gains a rule.
_BUNDLE_LITERAL = re.compile(r"[A-Z0-9]", re.IGNORECASE)
_TOKEN_RUN = re.compile(r"[A-Z0-9._~+/=-]+", re.IGNORECASE)


def _assignment_search(pattern: re.Pattern[str], text: str) -> bool:
    # Short unquoted matches need an ASCII digit within 4096 characters.
    # Saturated runs and quoted values bypass that requirement. Index both
    # once instead of walking 4096 characters at every assignment start.
    prefixes = _ASSIGNMENT_START_ASCII if pattern.flags & re.ASCII else _ASSIGNMENT_START
    if prefixes.search(text) is None:
        return False
    digits = [match.start() for match in _DIGIT.finditer(text)]
    long_runs = [(run.start(), run.end()) for run in _TOKEN_RUN.finditer(text) if run.end() - run.start() >= 4097]
    run_index = 0
    for prefix in prefixes.finditer(text):
        value = prefix.end()
        if value == len(text):
            continue
        while run_index < len(long_runs) and long_runs[run_index][1] < value:
            run_index += 1
        saturated = run_index < len(long_runs) and long_runs[run_index][0] <= value <= long_runs[run_index][1] - 4097
        if text[value] not in "\"'" and not saturated:
            digit = bisect.bisect_left(digits, value)
            if digit == len(digits) or digits[digit] - value > 4095:
                continue
        if pattern.match(text, prefix.start()):
            return True
    return False


def _rule_possible(name: str, text: str) -> bool:
    if name == "email":
        return _EMAIL_DOMAIN.search(text) is not None
    if name == "example-email-reserved":
        return "@" in text and _RESERVED_DOMAIN.search(text) is not None
    if name == "ssh-private-target":
        return _SSH_HOST.search(text) is not None
    return True


def _findings(texts: Iterable[str], *, blocking_only: bool = False) -> Iterator[tuple[str, str]]:
    """Every (rule id, action) in the order prompt loads report the first one.

    ``blocking_only`` yields block findings alone: embedding may pass warnings.
    """
    # Scan each field independently too: a leading BOM exemption belongs to one
    # field, and concatenation must not hide a field's role directive at line start.
    values = [value for value in texts if isinstance(value, str)]
    # Equal fields and an equal joined value cannot add a finding. Keep the
    # BOM-stripped joined copy when it differs, and preserve field order.
    for text in dict.fromkeys([*values, "\n".join(value.removeprefix("\ufeff") for value in values)]):
        # One character finding decides the verdict: both of them block.
        rule = invisible_rule(text)
        if rule:
            yield rule, "block"
        copy_sets = _scan_copy_sets(text)
        # Bundle patterns are ASCII. On an ASCII copy the earlier Unicode
        # pass finds a superset for these patterns, so only non-ASCII copies
        # need the ASCII pass. Bundle tests require review of inverse classes.
        copies = tuple(dict.fromkeys((text, *(lines for lines, _, _ in copy_sets))))
        extra_copies = tuple(
            copy
            for copy in dict.fromkeys(
                copy for _, injection_lines, normalized in copy_sets for copy in (injection_lines, normalized)
            )
            if copy not in copies
        )
        literal_copies = {copy: _BUNDLE_LITERAL.search(copy) is not None for copy in copies}
        possible: dict[tuple[str, str], bool] = {}
        for name, action, pattern in _RULES:
            if blocking_only and action != "block":
                continue
            for copy in copies:
                if name != "us-phone" and not literal_copies[copy]:
                    continue
                if pattern.flags & re.ASCII and copy.isascii():
                    continue
                key = (name, copy)
                if key not in possible:
                    possible[key] = _rule_possible(name, copy)
                if possible[key] and (
                    _assignment_search(pattern, copy) if name == "api-key-assignment" else pattern.search(copy)
                ):
                    yield name, action
                    break
        for name, action, pattern in _INJECTIONS:
            if blocking_only and action != "block":
                continue
            if any(pattern.search(copy) for copy in extra_copies if not (pattern.flags & re.ASCII and copy.isascii())):
                yield name, action
        if blocking_only:
            continue
        if "\u200d" in text:
            yield JOINER_RULE, "warn"
        for char, rule in _PRIVATE_CHARACTERS.items():
            if char in text:
                yield rule, "private"


def blocked_rule(texts: Iterable[str], *, blocking_only: bool = False) -> str | None:
    """Return an id only. Every finding withholds egress text, private included.

    Embedding passes ``blocking_only`` to let warnings and the private class through.
    """
    return next((name for name, _action in _findings(texts, blocking_only=blocking_only)), None)


def scan_verdict(texts: Iterable[str]) -> tuple[str, tuple[str, ...]]:
    """The hub's verdict for these texts: blocked, flagged, private or clean, with rule ids.

    Rule ids are distinct and ordered by severity, then by first finding.
    """
    found: dict[str, str] = {}
    for name, action in _findings(texts):
        found.setdefault(name, action)
    rank = {"block": 0, "warn": 1, "private": 2}
    if not found:
        return CLEAN, ()
    worst = min(found.values(), key=rank.__getitem__)
    verdict = {"block": BLOCKED, "warn": FLAGGED, "private": PRIVATE}[worst]
    return verdict, tuple(sorted(found, key=lambda name: rank[found[name]]))


def egress_refusal(texts: Iterable[str]) -> dict[str, str] | None:
    """The placeholder fields for text that must not reach a connector audience.

    None means the text may leave. A refusal names one rule id and never the
    text. A refusal for the private class alone carries ``PRIVATE_CODE``.
    """
    values = list(texts)
    rule = blocked_rule(values)
    if rule is None:
        return None
    if _ACTIONS.get(rule) != "private":
        return {"blocked": rule}
    verdict, rules = scan_verdict(values)
    if verdict == PRIVATE:
        return {"blocked": rules[0], "code": PRIVATE_CODE}
    return {"blocked": rules[0]}
