"""Root paths retain nested PII and earlier detector policy decisions."""

from pathlib import Path
import re

import pytest

from brigade.guard.engine import scan_text
from brigade.guard.policy import Policy, load_policy
from brigade.guard.types import Action, Rule

_ROOT = "/" + "root"
_EMAIL = "synthetic-person" + "@" + "fixture-provider" + ".com"
_PHONE = "202" + "-555-" + "0137"
_RESERVED_EMAIL = "fixture" + "@" + "example.com"
_RESERVED_PHONE = "555" + "-123-" + "4567"
_LOOPBACK = "127" + ".0.0.1"
_KNOWN = "fixture-node" + ".internal"
_GITHUB = "ghp_" + "A" * 36
_CASES = [
    (_EMAIL, "email", "<PRIVATE_EMAIL>"),
    (_PHONE, "us-phone", "<PRIVATE_PHONE>"),
    (_RESERVED_EMAIL, "example-email-reserved", "<EXAMPLE_EMAIL>"),
    (_RESERVED_PHONE, "example-phone-555", "<EXAMPLE_PHONE>"),
    (_LOOPBACK, "loopback-ipv4", "[redacted-ip]"),
    (_KNOWN, "known-host", "[redacted-known-host]"),
]


@pytest.mark.parametrize("root_action", ["warn", "allow", "redact", "block"])
@pytest.mark.parametrize("nested_action", ["warn", "allow", "redact", "block"])
@pytest.mark.parametrize("quote", ['"', "'", "`"])
@pytest.mark.parametrize("payload,rule_id,replacement", _CASES)
def test_root_and_nested_privacy_actions_are_independent(
    root_action: Action, nested_action: Action, quote, payload, rule_id, replacement
):
    path = _ROOT + "/Private Folder/" + payload
    text = f"Before {quote}{path}{quote}, after."
    policy = Policy(
        rules={"root-path": root_action, rule_id: nested_action},
        known_hosts=[_KNOWN] if rule_id == "known-host" else [],
    )
    result = scan_text(text, policy=policy)
    assert [(f.rule_id, f.action) for f in result.findings] == [
        ("root-path", root_action),
        (rule_id, nested_action),
    ]
    root, nested = result.findings
    assert (root.start, root.end, root.match) == (text.index(path), text.index(path) + len(path), path)
    assert (nested.start, nested.end, nested.match) == (
        text.index(payload),
        text.index(payload) + len(payload),
        payload,
    )
    assert result.blocked == ("block" in (root_action, nested_action))
    if root_action in {"redact", "block"}:
        expected = "[redacted-root-path]"
    elif nested_action in {"redact", "block"}:
        expected = _ROOT + "/Private Folder/" + replacement
    else:
        expected = path
    assert result.redacted_text == f"Before {quote}{expected}{quote}, after."


@pytest.mark.parametrize(
    "payload,rule_id",
    [(_LOOPBACK, "loopback-ipv4"), (_RESERVED_EMAIL, "example-email-reserved"), (_RESERVED_PHONE, "example-phone-555")],
)
@pytest.mark.parametrize("quote", ['"', "'", "`"])
def test_public_policy_keeps_root_block_when_earlier_detector_warns(payload, rule_id, quote):
    policy = load_policy(Path(__file__).resolve().parents[2] / "src/brigade/guard/policies/public-repo.json")
    path = _ROOT + "/Private Folder/" + payload + "/log"
    result = scan_text(f"Before {quote}{path}{quote}, after.", policy=policy)
    assert [(f.rule_id, f.action) for f in result.findings] == [("root-path", "block"), (rule_id, "warn")]
    assert result.blocked
    assert result.redacted_text == f"Before {quote}[redacted-root-path]{quote}, after."


@pytest.mark.parametrize("root_action", ["warn", "allow", "redact", "block"])
def test_mixed_nested_privacy_and_secrets_preserve_outside_edit(root_action: Action):
    path = _ROOT + "/cache " + _EMAIL + " " + _GITHUB + " " + _LOOPBACK
    text = f'Before "{path}" then {_PHONE} after.'
    policy = Policy(rules={"root-path": root_action, "email": "block", "us-phone": "redact", "loopback-ipv4": "redact"})
    result = scan_text(text, policy=policy)
    assert [f.rule_id for f in result.findings] == [
        "root-path",
        "email",
        "bare-token-prefix",
        "loopback-ipv4",
        "us-phone",
    ]
    assert all(text[f.start : f.end] == f.match for f in result.findings)
    expected_path = (
        "[redacted-root-path]"
        if root_action in {"block", "redact"}
        else _ROOT + "/cache <PRIVATE_EMAIL> [redacted-secret] [redacted-ip]"
    )
    assert result.redacted_text == f'Before "{expected_path}" then <PRIVATE_PHONE> after.'
    assert result.blocked


@pytest.mark.parametrize("allow", ["root-path", "infrastructure/root-path"])
@pytest.mark.parametrize(
    "payload,rule_id,replacement", [(_EMAIL, "email", "<PRIVATE_EMAIL>"), (_PHONE, "us-phone", "<PRIVATE_PHONE>")]
)
def test_root_allow_comment_does_not_allow_nested_pii(allow, payload, rule_id, replacement):
    path = _ROOT + "/cache " + payload
    text = f'<!-- content-guard: allow {allow} -->\n"{path}"'
    result = scan_text(text, policy=Policy(rules={rule_id: "block"}))
    root, nested = result.findings
    assert root.allowed_by == allow and root.action == "allow"
    assert nested.allowed_by is None and nested.blocks
    assert result.redacted_text == f'<!-- content-guard: allow {allow} -->\n"{_ROOT}/cache {replacement}"'


def test_all_rule_allow_stays_explicit_for_nested_pii():
    path = _ROOT + "/cache " + _EMAIL
    text = f'<!-- content-guard: allow all -->\n"{path}"'
    result = scan_text(text, policy=Policy(rules={"email": "block"}))
    assert [f.rule_id for f in result.findings] == ["root-path", "email"]
    assert all(f.action == "allow" and f.allowed_by == "all" for f in result.findings)
    assert not result.blocked
    assert result.redacted_text == text


@pytest.mark.parametrize("replacement", ["X", "X" * 100])
@pytest.mark.parametrize("root_action", ["redact", "block"])
def test_equal_span_pii_keeps_finding_and_uses_single_outer_mask(replacement, root_action: Action):
    path = _ROOT + "/cache"
    rule = Rule("synthetic-pii", "pii", re.escape(path), replacement)
    result = scan_text(f'"{path}" after.', policy=Policy(rules={"root-path": root_action}, custom_rules=[rule]))
    assert [f.rule_id for f in result.findings] == ["root-path", "synthetic-pii"]
    assert result.redacted_text == '"[redacted-root-path]" after.'


def test_earlier_partial_overlap_keeps_existing_precedence():
    path = _ROOT + "/cache"
    text = f'"{path}" after'
    result = scan_text(text, policy=Policy(known_hosts=['cache" after']))
    assert [f.rule_id for f in result.findings] == ["known-host"]
    assert result.redacted_text == '"' + _ROOT + "/[redacted-known-host]"
