"""UTF mode of the native engine against Python `re` on the guard's Unicode fixture.

The local guard still sends only ASCII to the engine (`test_native.py`). These
tests call the binary's UTF mode directly, the way the hub will, and require
span-for-span parity on every rule outside the divergence list, and a visible
difference on every rule inside it.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import unicodedata

import pytest

from brigade.guard.rules import DEFAULT_RULES
from brigade.guard.types import Rule
from brigade.guard.utf_parity import (
    CASELESS_I_REASON,
    SPACE_REASON,
    UTF_DIVERGENT_RULES,
    WORD_CLASS_REASON,
    pcre2_version_tuple,
    uses_word_class,
    utf_divergent_rules,
)

from .unicode_fixture import UNICODE_FIXTURE, UNICODE_FIXTURE_LINES

RULE_IDS = [rule.id for rule in DEFAULT_RULES]


def _python_spans(text: str, rule: Rule) -> list[tuple[int, int]]:
    return [match.span() for match in re.finditer(rule.pattern, text, rule.flags)]


def _request(text: str, rules, **extra) -> dict:
    return {
        "protocol": 1,
        **extra,
        "text": text,
        "rules": [{"id": rule.id, "pattern": rule.pattern, "flags": int(rule.flags)} for rule in rules],
    }


def _run(request: dict) -> dict:
    binary = os.environ.get("BRIGADE_GUARD_BIN")
    if not binary:
        pytest.skip("set BRIGADE_GUARD_BIN to exercise the built engine")
    process = subprocess.run(
        [binary], input=json.dumps(request, ensure_ascii=True), capture_output=True, text=True, timeout=30, check=True
    )
    return json.loads(process.stdout)


def test_fixture_exercises_every_default_rule_on_unicode_lines():
    for rule in DEFAULT_RULES:
        assert _python_spans(UNICODE_FIXTURE, rule), f"{rule.id} has no match on the Unicode fixture"
    for line in UNICODE_FIXTURE_LINES:
        assert not line.isascii() or line == "next line", line
    # Unicode 3.2 already assigned every character, so neither engine's newer
    # database can classify one differently.
    unassigned = {char for char in UNICODE_FIXTURE if unicodedata.ucd_3_2_0.category(char) == "Cn"}
    assert unassigned == set()


def test_divergence_list_names_default_rules():
    assert set(UTF_DIVERGENT_RULES) < set(RULE_IDS)
    for reasons in UTF_DIVERGENT_RULES.values():
        assert reasons and set(reasons) <= {SPACE_REASON, CASELESS_I_REASON}


def _strip_escapes_and_names(pattern: str) -> str:
    pattern = re.sub(r"\(\?P<\w+>|\(\?P=\w+\)|\(\?\(\w+\)|\(\?-?i:", "(", pattern)
    return re.sub(r"\\.", "", pattern)


def test_divergence_reasons_follow_the_rule_constructs():
    # A new or edited default rule must be classified: these constructs are the
    # measured class-level differences, so the listing follows from them.
    for rule in DEFAULT_RULES:
        space = bool(re.search(r"\\[sS]", rule.pattern.replace(r"[\s\S]", "")))
        plain = _strip_escapes_and_names(rule.pattern)
        caseless_i = bool(rule.flags & re.IGNORECASE) and bool(re.search(r"[iI]|A-Z|a-z", plain))
        expected = tuple(
            reason for reason, present in ((SPACE_REASON, space), (CASELESS_I_REASON, caseless_i)) if present
        )
        assert UTF_DIVERGENT_RULES.get(rule.id, ()) == expected, rule.id


def test_word_class_rules_join_the_list_on_pcre2_10_43():
    old = utf_divergent_rules("10.42 2022-12-11")
    new = utf_divergent_rules("10.46 2025-08-27")
    assert old == UTF_DIVERGENT_RULES
    assert set(new) == set(UTF_DIVERGENT_RULES) | {rule.id for rule in DEFAULT_RULES if uses_word_class(rule.pattern)}
    assert set(RULE_IDS) - set(new) == {"private-key-block"}
    assert all(WORD_CLASS_REASON in new[rule_id] for rule_id in set(new) - set(UTF_DIVERGENT_RULES))
    assert uses_word_class(r"\\w") is False and uses_word_class(r"a\b") is True
    assert pcre2_version_tuple("10.9") == (10, 9)
    with pytest.raises(ValueError):
        pcre2_version_tuple("unknown")


def test_utf_mode_matches_python_on_the_unicode_fixture():
    response = _run(_request(UNICODE_FIXTURE, DEFAULT_RULES, mode="utf"))
    engine = response["engine"]
    divergent = utf_divergent_rules(engine["pcre2_version"])
    assert [entry["id"] for entry in response["rules"]] == RULE_IDS
    same, different = [], []
    for rule, entry in zip(DEFAULT_RULES, response["rules"], strict=True):
        native = [(span["start"], span["end"]) for span in entry["spans"]]
        (same if native == _python_spans(UNICODE_FIXTURE, rule) else different).append(rule.id)
    # Parity outside the list, and every listed reason visible on the fixture.
    assert different == [rule_id for rule_id in RULE_IDS if rule_id in divergent], engine
    assert same, engine


def test_utf_mode_reports_byte_and_character_offsets():
    text = "😀 𐐀 é «" + "local" + "host» 𝟎"
    rules = [rule for rule in DEFAULT_RULES if rule.id == "localhost-bare"]
    response = _run(_request(text, rules, mode="utf"))
    (span,) = response["rules"][0]["spans"]
    assert (span["start"], span["end"]) == _python_spans(text, rules[0])[0]
    encoded = text.encode("utf-8")
    assert span["byte_start"] == len(text[: span["start"]].encode("utf-8"))
    assert encoded[span["byte_start"] : span["byte_end"]].decode("utf-8") == text[span["start"] : span["end"]]
    assert re.fullmatch(r"\d+\.\d+(?: .*)?", response["engine"]["pcre2_version"])
    assert re.fullmatch(r"\d+\.\d+\.\d+", response["engine"]["unicode_version"])


def test_ascii_mode_is_the_default_and_reports_no_engine():
    text = "serve on " + "local" + "host port 12345\n"
    implicit = _run(_request(text, DEFAULT_RULES))
    explicit = _run(_request(text, DEFAULT_RULES, mode="ascii"))
    assert implicit == explicit
    assert set(implicit) == {"protocol", "rules"}
    for rule, entry in zip(DEFAULT_RULES, implicit["rules"], strict=True):
        assert [(span["start"], span["end"]) for span in entry["spans"]] == _python_spans(text, rule)
