"""Quoted root paths retain independent nested-secret policy decisions."""

from __future__ import annotations

import re

import pytest

from brigade.guard.engine import scan_text
from brigade.guard.policy import Policy
from brigade.guard.types import Action, Rule

_ROOT = "/" + "root"
_GITHUB = "ghp_" + "A" * 36
_JWT = "eyJ" + "a" * 10 + "." + "b" * 12 + "." + "c" * 12
_GITLAB = "glpat-" + "a" * 15 + "-"
_GOOGLE = "AIza" + "a" * 29 + "-"
_CASES = [
    (_GITHUB, _GITHUB, "bare-token-prefix", "[redacted-secret]"),
    ("ASIA" + "A" * 16, "ASIA" + "A" * 16, "bare-token-prefix", "[redacted-secret]"),
    (_GOOGLE, _GOOGLE, "bare-token-prefix", "[redacted-secret]"),
    (_GITLAB, _GITLAB, "bare-token-prefix", "[redacted-secret]"),
    (_JWT, _JWT, "jwt-token", "[redacted-jwt]"),
    ("Bearer " + _GITLAB, "Bearer " + _GITLAB, "bearer-token", "Bearer [redacted-secret]"),
    ("Bearer " + _JWT, _JWT, "jwt-token", "Bearer [redacted-jwt]"),
]


@pytest.mark.parametrize("root_action", ["warn", "allow", "redact", "block"])
@pytest.mark.parametrize("secret_action", ["warn", "allow", "redact", "block"])
@pytest.mark.parametrize("quote", ['"', "'", "`"])
@pytest.mark.parametrize("payload,secret_match,rule_id,replacement", _CASES)
def test_nested_secret_actions_and_full_root_output(
    root_action: Action, secret_action: Action, quote, payload, secret_match, rule_id, replacement
):
    path = _ROOT + "/Private Folder/" + payload
    text = f"Before {quote}{path}{quote}, after."
    policy = Policy(rules={"root-path": root_action, rule_id: secret_action})
    result = scan_text(text, policy=policy)
    assert [(f.rule_id, f.action) for f in result.findings] == [
        ("root-path", root_action),
        (rule_id, secret_action),
    ]
    root, secret = result.findings
    assert (root.start, root.end, root.match) == (text.index(path), text.index(path) + len(path), path)
    start = text.index(secret_match)
    assert (secret.start, secret.end, secret.match) == (start, start + len(secret_match), secret_match)
    assert result.blocked == ("block" in (root_action, secret_action))
    if root_action in {"redact", "block"}:
        expected_path = "[redacted-root-path]"
    elif secret_action in {"redact", "block"}:
        expected_path = _ROOT + "/Private Folder/" + replacement
    else:
        expected_path = path
    assert result.redacted_text == f"Before {quote}{expected_path}{quote}, after."


@pytest.mark.parametrize("root_action", ["warn", "allow", "redact", "block"])
def test_multiple_nested_and_outside_secrets_keep_original_offsets(root_action: Action):
    inside = _GITHUB + " " + _GITLAB
    path = _ROOT + "/cache " + inside
    text = f'Before "{path}" then {_GOOGLE} after.'
    result = scan_text(text, policy=Policy(rules={"root-path": root_action}))
    assert [f.rule_id for f in result.findings] == ["root-path"] + ["bare-token-prefix"] * 3
    assert [f.match for f in result.findings[1:]] == [_GITHUB, _GITLAB, _GOOGLE]
    assert all(text[f.start : f.end] == f.match for f in result.findings)
    expected_path = (
        "[redacted-root-path]"
        if root_action in {"redact", "block"}
        else _ROOT + "/cache [redacted-secret] [redacted-secret]"
    )
    assert result.redacted_text == f'Before "{expected_path}" then [redacted-secret] after.'
    assert result.blocked


@pytest.mark.parametrize("replacement", ["X", "X" * 100])
@pytest.mark.parametrize("root_action", ["redact", "block"])
def test_outer_mask_dominates_shorter_and_longer_nested_edits(replacement, root_action: Action):
    token = "SYNTHETIC"
    path = _ROOT + "/cache " + token
    text = f'Before "{path}" after.'
    rule = Rule("synthetic-secret", "secret", token, replacement)
    result = scan_text(text, policy=Policy(rules={"root-path": root_action}, custom_rules=[rule]))
    assert [f.rule_id for f in result.findings] == ["root-path", "synthetic-secret"]
    assert result.redacted_text == 'Before "[redacted-root-path]" after.'


@pytest.mark.parametrize("allow", ["root-path", "infrastructure/root-path"])
def test_root_specific_allow_does_not_allow_nested_secret(allow):
    path = _ROOT + "/cache " + _GITHUB
    text = f'<!-- content-guard: allow {allow} -->\n"{path}"'
    result = scan_text(text)
    root, secret = result.findings
    assert root.allowed_by == allow and root.action == "allow"
    assert secret.allowed_by is None and secret.blocks
    assert result.redacted_text == f'<!-- content-guard: allow {allow} -->\n"{_ROOT}/cache [redacted-secret]"'


def test_all_rule_allow_semantics_remain_explicit():
    path = _ROOT + "/cache " + _GITHUB
    text = f'<!-- content-guard: allow all -->\n"{path}"'
    result = scan_text(text)
    assert [f.rule_id for f in result.findings] == ["root-path", "bare-token-prefix"]
    assert all(f.action == "allow" and f.allowed_by == "all" for f in result.findings)
    assert not result.blocked
    assert result.redacted_text == text


def test_equal_span_secret_preserves_precedence_and_single_root_edit():
    path = _ROOT + "/cache"
    rules = [
        Rule("first-secret", "secret", re.escape(path), "FIRST"),
        Rule("second-secret", "secret", re.escape(path), "SECOND"),
    ]
    result = scan_text(f'"{path}"', policy=Policy(custom_rules=rules))
    assert [f.rule_id for f in result.findings] == ["first-secret", "root-path"]
    assert result.redacted_text == '"[redacted-root-path]"'


def test_partial_overlap_and_nonsecret_root_overlap_remain_suppressed():
    path = _ROOT + "/cache"
    text = f'"{path}" outside'
    rules = [
        Rule("partial-secret", "secret", re.escape(path + '" outside'), "PARTIAL"),
        Rule("nested-business", "business", "cache", "BUSINESS"),
    ]
    result = scan_text(text, policy=Policy(custom_rules=rules))
    assert [f.rule_id for f in result.findings] == ["root-path"]
    assert result.redacted_text == '"[redacted-root-path]" outside'


def test_adjacent_nonoverlapping_masks_are_preserved():
    left, right = _ROOT + "/Left Folder", _ROOT + "/Right Folder"
    result = scan_text(f'"{left}""{right}"')
    assert [f.match for f in result.findings] == [left, right]
    assert result.redacted_text == '"[redacted-root-path]""[redacted-root-path]"'


@pytest.mark.parametrize("payload,secret_match,rule_id,replacement", _CASES)
def test_no_root_secret_findings_and_redactions_keep_precedence(payload, secret_match, rule_id, replacement):
    text = "Before " + payload + ", after."
    result = scan_text(text)
    assert [(f.rule_id, f.match, f.action) for f in result.findings] == [(rule_id, secret_match, "block")]
    assert result.redacted_text == "Before " + replacement + ", after."


def test_known_host_still_claims_overlapping_secret_before_other_rules():
    result = scan_text(_GITHUB, policy=Policy(known_hosts=[_GITHUB]))
    assert [f.rule_id for f in result.findings] == ["known-host"]
    assert result.redacted_text == "[redacted-known-host]"


def test_no_root_assignment_still_claims_provider_token():
    text = 'token="' + _GITHUB + '"'
    result = scan_text(text)
    assert [f.rule_id for f in result.findings] == ["api-key-assignment"]
    assert result.redacted_text == "[redacted-secret]"
