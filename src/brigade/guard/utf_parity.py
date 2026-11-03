"""Default rules whose native UTF-mode results differ from Python `re`.

The native engine's UTF mode (`"mode": "utf"`, PCRE2 UTF and UCP) exists for
the hub, which scans Unicode memory text with no Python fallback. The local
guard keeps every non-ASCII subject on Python `re`, so nothing here changes
guard results. Rules listed here must not rely on UTF mode alone. The rest
match Python span for span on the guard's Unicode fixture
(`tests/guard/unicode_fixture.py`), checked by `tests/guard/test_native_utf.py`.

Measured by comparing every code point from U+0080 to U+10FFFF for `\\w`,
`\\d`, `\\s`, `.` and each caseless ASCII letter, then confirmed per rule on
the fixture. Kelvin sign and long s fold the same way in both engines.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from .rules import DEFAULT_RULES
from .types import Rule

SPACE_REASON = (
    "\\s and \\S: PCRE2 counts U+180E MONGOLIAN VOWEL SEPARATOR as space and"
    " Python does not; Python counts U+001C to U+001F as space and PCRE2 does not"
)
CASELESS_I_REASON = (
    "caseless i: Python folds U+0130 LATIN CAPITAL LETTER I WITH DOT ABOVE and"
    " U+0131 LATIN SMALL LETTER DOTLESS I to i, PCRE2 does not"
)
WORD_CLASS_REASON = (
    "\\w and \\b on PCRE2 10.43 and later: combining marks (Mn) and connector"
    " punctuation (Pc) are word characters in PCRE2 and not in Python"
)
UNICODE_VERSION_NOTE = (
    "Characters assigned in only one of the two Unicode databases classify"
    " differently in every \\w, \\d, \\s and \\b rule. Compare the engine's"
    " reported unicode_version with unicodedata.unidata_version."
)

UTF_DIVERGENT_RULES: dict[str, tuple[str, ...]] = {
    "example-phone-555": (SPACE_REASON,),
    "example-email-reserved": (CASELESS_I_REASON,),
    "ssh-private-target": (SPACE_REASON,),
    "port-reference": (SPACE_REASON,),
    "coauthored-by-trailer": (SPACE_REASON,),
    "windows-home-path": (CASELESS_I_REASON,),
    "email": (SPACE_REASON, CASELESS_I_REASON),
    "us-phone": (SPACE_REASON,),
    "bearer-token": (SPACE_REASON, CASELESS_I_REASON),
    "api-key-assignment": (SPACE_REASON, CASELESS_I_REASON),
    "bare-token-prefix": (CASELESS_I_REASON,),
    "private-hostname": (CASELESS_I_REASON,),
}
"""Divergent on every PCRE2 build. Every other default rule needs a matching
Unicode database, and on PCRE2 10.43 or later also no `\\w` or `\\b`."""

_WORD_ESCAPES = frozenset("wWbB")


def uses_word_class(pattern: str) -> bool:
    """True when the pattern has a `\\w`, `\\W`, `\\b` or `\\B` escape."""
    index = 0
    while index < len(pattern) - 1:
        if pattern[index] == "\\":
            if pattern[index + 1] in _WORD_ESCAPES:
                return True
            index += 2
        else:
            index += 1
    return False


def pcre2_version_tuple(version: str) -> tuple[int, int]:
    """Parse the engine's `pcre2_version`, for example `10.42 2022-12-11`."""
    found = re.match(r"(\d+)\.(\d+)", version)
    if found is None:
        raise ValueError(f"unrecognized PCRE2 version: {version!r}")
    return int(found.group(1)), int(found.group(2))


def utf_divergent_rules(pcre2_version: str, rules: Iterable[Rule] = DEFAULT_RULES) -> dict[str, tuple[str, ...]]:
    """Every rule whose UTF-mode results may differ from Python on this PCRE2 build."""
    word_class = pcre2_version_tuple(pcre2_version) >= (10, 43)
    divergent: dict[str, tuple[str, ...]] = {}
    for rule in rules:
        reasons = UTF_DIVERGENT_RULES.get(rule.id, ())
        if word_class and uses_word_class(rule.pattern):
            reasons = (*reasons, WORD_CLASS_REASON)
        if reasons:
            divergent[rule.id] = reasons
    return divergent
