"""Bare provider credentials end at their case-sensitive ASCII alphabet."""

import pytest

from brigade.guard.engine import scan_text
from brigade.guard.policy import Policy
from brigade.guard.types import Action

_FAMILIES = [("AIza", 30), ("glpat-", 16)]
_UNICODE = ["é", "λ", "中", "İ", "ı", "ſ", "K", "\u0301", "😀"]


@pytest.mark.parametrize("prefix,minimum", _FAMILIES)
@pytest.mark.parametrize("extra", [0, 1, 16, 4096])
@pytest.mark.parametrize("terminal", ["a", "Z", "0", "_", "-"])
@pytest.mark.parametrize("following", _UNICODE + ["", ".", ",", ")", " "])
@pytest.mark.parametrize("action", ["allow", "warn", "redact", "block"])
def test_bare_provider_ascii_endpoints_preserve_following_text(
    prefix, minimum, extra, terminal, following, action: Action
):
    token = prefix + "a" * (minimum + extra - 1) + terminal
    text = "Credential: " + token + following
    result = scan_text(text, Policy(rules={"bare-token-prefix": action}))
    assert [(f.rule_id, f.match, f.action) for f in result.findings] == [("bare-token-prefix", token, action)]
    (finding,) = result.findings
    assert (finding.start, finding.end) == (len("Credential: "), len("Credential: ") + len(token))
    assert result.blocked == (action == "block")
    expected = "[redacted-secret]" if action in {"block", "redact"} else token
    assert result.redacted_text == "Credential: " + expected + following


@pytest.mark.parametrize("prefix,minimum", _FAMILIES)
@pytest.mark.parametrize("following", _UNICODE + ["", ".", "-"])
def test_below_minimum_body_does_not_match(prefix, minimum, following):
    # A hyphen is a body character: keep the complete body below minimum.
    body = "a" * (minimum - 2) if following == "-" else "a" * (minimum - 1)
    text = "Credential: " + prefix + body + following
    result = scan_text(text)
    assert result.findings == []
    assert result.redacted_text == text


@pytest.mark.parametrize("prefix,minimum", _FAMILIES)
@pytest.mark.parametrize("continuation", list("aZ09_-"))
def test_ascii_body_continuations_are_consumed_completely(prefix, minimum, continuation):
    token = prefix + "a" * minimum + continuation
    result = scan_text("Credential: " + token)
    assert [(f.rule_id, f.match) for f in result.findings] == [("bare-token-prefix", token)]
    assert result.redacted_text == "Credential: [redacted-secret]"


@pytest.mark.parametrize("prefix,minimum", _FAMILIES)
@pytest.mark.parametrize("before", ["x", "_", "é", "中"])
def test_embedded_provider_prefixes_remain_excluded(prefix, minimum, before):
    text = before + prefix + "a" * minimum + "中"
    result = scan_text(text)
    assert result.findings == []
    assert result.redacted_text == text


@pytest.mark.parametrize("prefix,minimum", [("aiza", 30), ("AIZA", 30), ("GLPAT-", 16), ("glPat-", 16)])
def test_provider_prefix_case_is_still_significant(prefix, minimum):
    text = "Credential: " + prefix + "a" * minimum + "中"
    result = scan_text(text)
    assert result.findings == []
    assert result.redacted_text == text


@pytest.mark.parametrize("prefix,minimum", _FAMILIES)
@pytest.mark.parametrize("following", _UNICODE)
def test_bearer_precedence_and_assignment_precedence_stay_intact(prefix, minimum, following):
    token = prefix + "a" * (minimum - 1) + "-"
    bearer = scan_text("Bearer " + token + following)
    # Preserve the existing case-insensitive Bearer alphabet, including
    # Python's four special Unicode case-fold matches. Bare provider tokens
    # use a separate, explicitly case-sensitive ASCII alphabet.
    bearer_suffix = following if following in {"İ", "ı", "ſ", "K"} else ""
    assert [(f.rule_id, f.match) for f in bearer.findings] == [("bearer-token", "Bearer " + token + bearer_suffix)]
    assert bearer.redacted_text == "Bearer [redacted-secret]" + ("" if bearer_suffix else following)
    assigned = scan_text('api_key="' + token + '"' + following)
    assert [f.rule_id for f in assigned.findings] == ["api-key-assignment"]
    assert assigned.redacted_text == "[redacted-secret]" + following
