"""Template exclusions preserve enclosing multiline lexical context."""

import re

import pytest

from brigade.guard.engine import scan_text
from brigade.guard.policy import Policy
from brigade.guard.rules import Rule
from brigade.guard.types import Action

HOST = "cfg.db" + ".local"
FIELD = "https://${" + HOST + "}/p"


@pytest.mark.parametrize(
    "source",
    [
        "/*\nconst url = `" + FIELD + "`;\n*/",
        'const n = "/*"; /*\nconst url = `' + FIELD + "`;\n*/",
        '/*\nconst re = /"/; const url = `' + FIELD + "`;\n*/",
        "/*/\nconst url = `" + FIELD + "`;",
        "const note = `literal\nconst url = \\`https://\\${" + HOST + "}/p\\`;\n`;",
        'const note = "prefix\\\nconst url = `' + FIELD + '`";',
        'const note = "unterminated\nconst url = `' + FIELD + "`;",
        'const re = /unterminated\nconst text = "/"; const url = `' + FIELD + "`;",
        "<!--\nconst url = `" + FIELD + "`;\n-->",
        "General prose before the snippet.\nconst url = `" + FIELD + "`;",
    ],
)
@pytest.mark.parametrize("action", ["allow", "warn", "redact", "block"])
def test_multiline_literal_or_unknown_context_retains_host(source, action: Action):
    result = scan_text(source, Policy(rules={"private-hostname": action}))
    start = source.index(HOST)
    assert [(f.rule_id, f.match, f.action, f.start, f.end) for f in result.findings] == [
        ("private-hostname", HOST, action, start, start + len(HOST))
    ]
    assert result.blocked == (action == "block")
    replacement = "[redacted-host]" if action in {"redact", "block"} else HOST
    assert result.redacted_text == source[:start] + replacement + source[start + len(HOST) :]


@pytest.mark.parametrize(
    "prefix",
    [
        "/* first\nclosed */\n",
        "// complete line comment\n",
        'const n = 1; // quote " and backtick ` in comment\n',
        'const label = "/*";\n',
        'const label = "*/";\n',
        "const re = /[/][*]/;\n",
        'const re = /"/;\n',
        "const prior = `value ${a / b}`;\n",
    ],
)
@pytest.mark.parametrize("action", ["allow", "warn", "redact", "block"])
def test_proven_code_after_closed_multiline_prefix_stays_exempt(prefix, action: Action):
    text = prefix + "const url = `" + FIELD + "`;"
    result = scan_text(text, Policy(rules={"private-hostname": action}))
    assert result.findings == []
    assert not result.blocked
    assert result.redacted_text == text


@pytest.mark.parametrize("action", ["allow", "warn", "redact", "block"])
def test_repeated_fields_and_comment_literal_keep_original_offsets(action: Action):
    prefix = "\n".join("const url = `" + FIELD + "`;" for _ in range(128)) + "\n/*\n"
    source = prefix + "const literal = `" + FIELD + "`;\n*/"
    result = scan_text(source, Policy(rules={"private-hostname": action}))
    start = source.rindex(HOST)
    assert [(f.match, f.start, f.end) for f in result.findings] == [(HOST, start, start + len(HOST))]
    assert result.blocked == (action == "block")
    replacement = "[redacted-host]" if action in {"redact", "block"} else HOST
    assert result.redacted_text == source[:start] + replacement + source[start + len(HOST) :]


@pytest.mark.parametrize("action", ["allow", "warn", "redact", "block"])
def test_custom_hostname_rule_does_not_use_builtin_template_exclusion(action: Action):
    text = "// prefix\nconst url = `" + FIELD + "`;"
    rule = Rule(
        id="private-hostname",
        category="infrastructure",
        pattern=re.escape(HOST),
        replacement="[custom-host]",
        description="Synthetic custom rule control.",
    )
    result = scan_text(text, Policy(custom_rules=[rule], rules={"private-hostname": action}))
    assert [(f.match, f.action) for f in result.findings] == [(HOST, action)]
    assert result.blocked == (action == "block")
    assert result.redacted_text == (text.replace(HOST, "[custom-host]") if action in {"redact", "block"} else text)
