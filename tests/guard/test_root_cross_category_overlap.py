"""Nested privacy detectors must not hide independent secret decisions."""

import pytest

from brigade.guard.engine import scan_text
from brigade.guard.policy import Policy
from brigade.guard.types import Action

_ACTIONS = ["allow", "warn", "redact", "block"]
_GITHUB = "ghp_" + "A" * 36
_EMAIL = _GITHUB + "@" + "fixture-provider.com"
_PHONE = "202" + "-555-" + "0137"
_SLACK = "xoxb-" + _PHONE + "-" + "A" * 20
_BEARER = "Bearer " + "A" * 20 + "/" + _GITHUB
_CASES = [
    (_EMAIL, "bare-token-prefix", _GITHUB, "email", _EMAIL, "<PRIVATE_EMAIL>", "[redacted-secret]"),
    (_SLACK, "bare-token-prefix", _SLACK, "us-phone", _PHONE, "<PRIVATE_PHONE>", "[redacted-secret]"),
    (
        _BEARER + "@fixture-provider.com",
        "bearer-token",
        _BEARER,
        "email",
        _EMAIL,
        "<PRIVATE_EMAIL>",
        "Bearer [redacted-secret]",
    ),
]


@pytest.mark.parametrize("root_action", _ACTIONS)
@pytest.mark.parametrize("secret_action", _ACTIONS)
@pytest.mark.parametrize("pii_action", _ACTIONS)
@pytest.mark.parametrize("quote", ['"', "'", "`"])
@pytest.mark.parametrize("payload,secret_id,secret,pii_id,pii,pii_mask,secret_mask", _CASES)
def test_nested_cross_category_decisions_and_union_mask(
    root_action: Action,
    secret_action: Action,
    pii_action: Action,
    quote,
    payload,
    secret_id,
    secret,
    pii_id,
    pii,
    pii_mask,
    secret_mask,
):
    path = "/" + "root/cache " + payload
    text = f"Before {quote}{path}{quote}, after."
    result = scan_text(text, Policy(rules={"root-path": root_action, secret_id: secret_action, pii_id: pii_action}))
    assert {(f.rule_id, f.action) for f in result.findings} == {
        ("root-path", root_action),
        (secret_id, secret_action),
        (pii_id, pii_action),
    }
    assert result.blocked == ("block" in (root_action, secret_action, pii_action))
    for finding in result.findings:
        assert text[finding.start : finding.end] == finding.match
        assert finding.match == {"root-path": path, secret_id: secret, pii_id: pii}[finding.rule_id]
    masks = []
    for value, action, replacement in ((secret, secret_action, secret_mask), (pii, pii_action, pii_mask)):
        if action in {"block", "redact"}:
            start = payload.index(value)
            masks.append((start, start + len(value), replacement))
    if root_action in {"block", "redact"}:
        expected = "[redacted-root-path]"
    else:
        expected_payload = payload
        if masks:
            masks.sort(key=lambda item: (item[0], -item[1]))
            # These three fixtures deliberately contain or cross each other.
            start, _, replacement = masks[0]
            end = max(item[1] for item in masks)
            expected_payload = payload[:start] + replacement + payload[end:]
        expected = "/" + "root/cache " + expected_payload
    assert result.redacted_text == f"Before {quote}{expected}{quote}, after."


@pytest.mark.parametrize("root_action", _ACTIONS)
@pytest.mark.parametrize("secret_action", _ACTIONS)
@pytest.mark.parametrize("pii_action", _ACTIONS)
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("secret,pii", [("ABCD", "CDEF"), ("CDEF", "ABCD"), ("ABCDEF", "ABCDEF")])
def test_custom_nested_cross_category_overlap_preserves_both_decisions(
    root_action: Action, secret_action: Action, pii_action: Action, reverse, secret, pii
):
    from brigade.guard.types import Rule

    rules = [Rule("synthetic-secret", "secret", secret, "SECRET"), Rule("synthetic-pii", "pii", pii, "PII")]
    if reverse:
        rules.reverse()
    payload = "ABCDEF"
    path = "/" + "root/cache " + payload
    text = f'Before "{path}" after.'
    result = scan_text(
        text,
        Policy(
            rules={"root-path": root_action, "synthetic-secret": secret_action, "synthetic-pii": pii_action},
            custom_rules=rules,
        ),
    )
    assert {(f.rule_id, f.action) for f in result.findings} == {
        ("root-path", root_action),
        ("synthetic-secret", secret_action),
        ("synthetic-pii", pii_action),
    }
    assert result.blocked == ("block" in (root_action, secret_action, pii_action))
    assert all(text[f.start : f.end] == f.match for f in result.findings)
    masks = []
    for rule in rules:
        action = secret_action if rule.category == "secret" else pii_action
        if action in {"redact", "block"}:
            start = payload.index(rule.pattern)
            masks.append((start, start + len(rule.pattern), rule.replacement))
    if root_action in {"redact", "block"}:
        expected = "[redacted-root-path]"
    else:
        expected_payload = payload
        if masks:
            masks.sort(key=lambda item: (item[0], -item[1]))
            start, _, replacement = masks[0]
            end = max(item[1] for item in masks)
            expected_payload = payload[:start] + replacement + payload[end:]
        expected = "/" + "root/cache " + expected_payload
    assert result.redacted_text == f'Before "{expected}" after.'


@pytest.mark.parametrize("category", ["secret", "pii"])
@pytest.mark.parametrize("inside_root", [False, True])
def test_same_category_precedence_is_unchanged(category, inside_root):
    from brigade.guard.types import Rule

    payload = "ABCDEFGHIJ"
    text = '"' + "/" + "root/cache " + payload + '"' if inside_root else payload
    rules = [Rule("first", category, "ABCDE", "FIRST"), Rule("second", category, "CDEFG", "SECOND")]
    result = scan_text(
        text, Policy(rules={"root-path": "allow", "first": "warn", "second": "block"}, custom_rules=rules)
    )
    assert [f.rule_id for f in result.findings] == (["root-path"] if inside_root else []) + ["first"]
    assert not result.blocked
    assert result.redacted_text == text


def test_nested_union_preserves_adjacent_and_outside_edits():
    from brigade.guard.types import Rule

    path = "/" + "root/cache ABCDEFZZ"
    text = f'Before "{path}" OUTSIDE after.'
    rules = [
        Rule("first", "pii", "ABCD", "PII"),
        Rule("second", "secret", "CDEF", "SECRET"),
        Rule("adjacent", "secret", "ZZ", "NEXT"),
        Rule("outside", "secret", "OUTSIDE", "OUT"),
    ]
    result = scan_text(
        text, Policy(rules={"root-path": "allow"}, defaults={"pii": "redact", "secret": "block"}, custom_rules=rules)
    )
    assert result.redacted_text == 'Before "' + "/" + 'root/cache PIINEXT" OUT after.'
    assert result.blocked
    assert len(result.findings) == 5
