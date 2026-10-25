"""Unsupported numeric text retains detection instead of asserting."""

import sys

import pytest

from brigade.guard.engine import scan_text
from brigade.guard.policy import Policy
from brigade.guard.types import Action

HOST = "cfg.db" + ".local"
UNICODE_DIGITS = [
    chr(code) for code in range(sys.maxunicode + 1) if chr(code).isdigit() and chr(code) not in "0123456789"
]


@pytest.mark.parametrize("digit", UNICODE_DIGITS)
@pytest.mark.parametrize("prior", ["const n = {}; ", "const n = 1{}; ", "const prior = `value ${{{}}}`; "])
@pytest.mark.parametrize("action", ["allow", "warn", "redact", "block"])
def test_unsupported_digits_preserve_literal_host_decisions(digit, prior, action: Action):
    prefix = prior.format(digit) + "const url = `https://${"
    text = prefix + HOST + "}/p`;"
    result = scan_text(text, Policy(rules={"private-hostname": action}))
    assert [(f.rule_id, f.match, f.action) for f in result.findings] == [("private-hostname", HOST, action)]
    assert [(f.start, f.end) for f in result.findings] == [(len(prefix), len(prefix) + len(HOST))]
    assert result.blocked == (action == "block")
    replacement = "[redacted-host]" if action in {"redact", "block"} else HOST
    assert result.redacted_text == prefix + replacement + "}/p`;"


@pytest.mark.parametrize("number", ["0", "1", "1234567890", "0.5", "123456.789"])
@pytest.mark.parametrize("action", ["allow", "warn", "redact", "block"])
def test_ascii_numbers_preserve_supported_template_positions(number, action: Action):
    text = f"const n = {number}; const url = `https://${{{HOST}}}/p`;"
    result = scan_text(text, Policy(rules={"private-hostname": action}))
    assert result.findings == []
    assert not result.blocked
    assert result.redacted_text == text
