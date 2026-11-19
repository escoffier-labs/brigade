"""Narrow same-line f-string sub-expression coverage for #1983."""

import pytest

from brigade.guard.engine import scan_text
from brigade.guard.policy import Policy


HOST = "db" + ".internal"


@pytest.mark.parametrize(
    "source",
    [
        # A nested f-string field holds the host as bare code.
        "f\"https://{f'{cfg.HOST}'}/\"",
        "url = f\"https://{f'{cfg.HOST}'}/\"",
        "f'https://{f\"{cfg.HOST}\"}/'",
        # A call argument holds the host as bare code.
        'f"https://{lookup(cfg.HOST)}/p"',
        "f\"https://{lookup('key')}/{cfg.HOST}/p\"",
        # A subscript holds the host as bare code.
        'f"https://{mapping[cfg.HOST]}/p"',
        "f\"https://{items['a']['b']}/{cfg.HOST}/p\"",
        # A boolean expression holds the host as bare code.
        'f"https://{x==cfg.HOST}/p"',
    ],
)
def test_fstring_subexpression_host_is_code(source: str) -> None:
    text = source.replace("HOST", HOST)
    result = scan_text(text, policy=Policy(rules={"private-hostname": "redact"}))
    assert not [finding for finding in result.findings if finding.rule_id == "private-hostname"]
    assert result.redacted_text == text


@pytest.mark.parametrize(
    "source",
    [
        # The nested field is code, but a literal host on the same line stays.
        "f\"https://{f'{cfg.HOST}'}/\"  # https://cfg.HOST/p",
        # A quoted call argument is a literal, not a code expression.
        "f\"https://{lookup('cfg.HOST')}/p\"",
        'f"https://{lookup(cfg.HOST)}/p" # https://cfg.HOST/p',
        # A quoted subscript key is a literal.
        "f\"https://{mapping['cfg.HOST']}/p\"",
        # A quoted boolean operand is a literal.
        "f\"https://{x=='cfg.HOST'}/p\"",
        # Whole-field controls from #2112 still report.
        'f"https://{cfg.HOST or fallback}/p"',
        'f"https://{cfg.HOST[0]}/p"',
    ],
)
def test_fstring_subexpression_keeps_literal_hosts(source: str) -> None:
    text = source.replace("HOST", HOST)
    result = scan_text(text, policy=Policy(rules={"private-hostname": "redact"}))
    hosts = [finding for finding in result.findings if finding.rule_id == "private-hostname"]
    assert hosts
    assert result.redacted_text.count(HOST) < text.count(HOST)


@pytest.mark.parametrize(
    "source",
    [
        # Escaped braces are literal text, not a replacement field.
        'f"https://{{x+HOST}}"',
        'f"https://{{host=HOST}}"',
        "f\"https://{f'{{HOST}}'}/\"",
        # Format-spec and conversion text is not an expression operand.
        'f"https://{x:>HOST}/p"',
        'f"https://{x:.2f+HOST}/p"',
        # Comment prefixes and block comments keep main's detection.
        'const x = 1; /* sample = f"https://{lookup(HOST)}/x" */',
        '; url = f"https://{lookup(HOST)}/x"',
        "* url = f\"https://{f'{HOST}'}/x\"",
    ],
)
def test_fstring_literal_text_never_loses_hosts(source: str) -> None:
    text = source.replace("HOST", HOST)
    result = scan_text(text, policy=Policy(rules={"private-hostname": "redact"}))
    hosts = [finding for finding in result.findings if finding.rule_id == "private-hostname"]
    assert hosts
    assert result.redacted_text.count(HOST) < text.count(HOST)


@pytest.mark.parametrize(
    "source",
    [
        # Each code shape is wrapped in escaped braces, making it literal text.
        "f\"https://{{f'{cfg.HOST}'}}/\"",
        "url = f\"https://{{f'{cfg.HOST}'}}/\"",
        "f'https://{{f\"{cfg.HOST}\"}}/'",
        'f"https://{{lookup(cfg.HOST)}}/p"',
        "f\"https://{{lookup('key')}}/{{cfg.HOST}}/p\"",
        'f"https://{{mapping[cfg.HOST]}}/p"',
        "f\"https://{{items['a']['b']}}/{{cfg.HOST}}/p\"",
        'f"https://{{x==cfg.HOST}}/p"',
    ],
)
def test_escaped_brace_run_keeps_hosts_reporting(source: str) -> None:
    text = source.replace("HOST", HOST)
    result = scan_text(text, policy=Policy(rules={"private-hostname": "redact"}))
    hosts = [finding for finding in result.findings if finding.rule_id == "private-hostname"]
    assert hosts
    assert result.redacted_text.count(HOST) < text.count(HOST)
