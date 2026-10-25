"""JavaScript expression positions and completed comments retain URL code context."""

import pytest

from brigade.guard.engine import scan_text
from brigade.guard.policy import Policy
from brigade.guard.types import Action

HOST = "cfg.db" + ".local"
CODE_FORMS = [
    "url = ok ? `https://${{{host}}}/p` : fallback;",
    "url = ok ? fallback : `https://${{{host}}}/p`;",
    "fetch(ok ? `https://${{{host}}}/p` : fallback);",
    "fetch(ok ? fallback : `https://${{{host}}}/p`);",
    "opts.url = ok ? `https://${{{host}}}/p` : fallback;",
    "fetch(ok ? tag`https://${{{host}}}/p` : fallback);",
    "const opts = {{url: `https://${{{host}}}/p`}};",
    "const opts = {{'url': `https://${{{host}}}/p`}};",
    'const opts = {{"url": `https://${{{host}}}/p`}};',
    "const f = () => `https://${{{host}}}/p`;",
    "const f = arg => `https://${{{host}}}/p`;",
    "const u = ok ? `https://${{{host}}}/p` : fallback;",
    "const u = ok ? fallback : `https://${{{host}}}/p`;",
    "return ok ? `https://${{{host}}}/p` : fallback;",
    "const u = tag`https://${{{host}}}/p`;",
    "const u = tag `https://${{{host}}}/p`;",
    "const u = String.raw`https://${{{host}}}/p`;",
    "tag`https://${{{host}}}/p`;",
    "/* config */ const url = `https://${{{host}}}/p`;",
    "/* don't print */ const url = `https://${{{host}}}/p`;",
    "/* first */ /* second */ const url = `https://${{{host}}}/p`;",
    'const marker = "/*"; const url = `https://${{{host}}}/p`;',
    'const marker = "//"; const url = `https://${{{host}}}/p`;',
    "const prior = `old`; /* ' */ const url = `https://${{{host}}}/p`;",
]
LITERAL_FORMS = [
    "// const url = `https://${{{host}}}/p`;",
    "const x = 1; // const url = `https://${{{host}}}/p`;",
    "/* const url = `https://${{{host}}}/p`; */",
    "/* open const url = `https://${{{host}}}/p`;",
    '"const url = `https://${{{host}}}/p`"',
    "'const url = `https://${{{host}}}/p`'",
    "Use `https://${{{host}}}/p` for that.",
    "Use: `https://${{{host}}}/p` for that.",
    "Use? `https://${{{host}}}/p` for that.",
    r"const url = `https://\${{{host}}}/p`;",
    "const u = `https://${{lookup('{host}')}}/p`;",
    "const u = `https://${{'{host}'}}/p`;",
    "const u = `https://{host}/${{path}}`;",
    "const u = `https://${{prefix}}/{host}/p`;",
]


@pytest.mark.parametrize("form", CODE_FORMS)
@pytest.mark.parametrize("action", ["allow", "warn", "redact", "block"])
def test_js_expression_fields_are_code(form, action: Action):
    text = form.format(host=HOST)
    result = scan_text(text, Policy(rules={"private-hostname": action}))
    assert result.findings == []
    assert not result.blocked
    assert result.redacted_text == text


@pytest.mark.parametrize("form", LITERAL_FORMS)
@pytest.mark.parametrize("action", ["allow", "warn", "redact", "block"])
def test_literals_comments_and_prose_keep_hostname_detection(form, action: Action):
    text = form.format(host=HOST)
    result = scan_text(text, Policy(rules={"private-hostname": action}))
    assert [(f.rule_id, f.match, f.action) for f in result.findings] == [("private-hostname", HOST, action)]
    (finding,) = result.findings
    assert (finding.start, finding.end) == (text.index(HOST), text.index(HOST) + len(HOST))
    assert result.blocked == (action == "block")
    expected = text.replace(HOST, "[redacted-host]") if action in {"block", "redact"} else text
    assert result.redacted_text == expected


@pytest.mark.parametrize("slashes", range(6))
def test_template_field_escape_parity(slashes):
    text = "const url = `https://" + "\\" * slashes + "${" + HOST + "}/p`;"
    result = scan_text(text)
    assert bool(result.findings) == bool(slashes % 2)
    assert result.redacted_text == (text.replace(HOST, "[redacted-host]") if slashes % 2 else text)


_REGEX_PREFIXES = [
    'const result = value instanceof /"/;',
    'const result = "key" in /"/;',
    'for (const value of /"/) {}',
    'const re = /"/;',
    "const re = /'/;",
    'const re = /["`]/g;',
    r"const re = /[\/]/;",
    r"const re = /\"/;",
    'if (ready) /"/.test(value);',
    "const ratio = numerator / denominator;",
    'const ratio = numerator / "two" / denominator;',
]


@pytest.mark.parametrize("prefix", _REGEX_PREFIXES)
@pytest.mark.parametrize("action", ["allow", "warn", "redact", "block"])
def test_regex_literals_cannot_turn_string_contents_into_template_code(prefix, action: Action):
    text = prefix + ' const text = "text => `https://${' + HOST + '}/p`";'
    result = scan_text(text, Policy(rules={"private-hostname": action}))
    assert [(f.rule_id, f.match, f.action) for f in result.findings] == [("private-hostname", HOST, action)]
    expected = text.replace(HOST, "[redacted-host]") if action in {"block", "redact"} else text
    assert result.redacted_text == expected
    assert result.blocked == (action == "block")


@pytest.mark.parametrize("prefix", _REGEX_PREFIXES)
def test_prior_regex_or_division_keeps_real_template_expression_code(prefix):
    text = prefix + " const url = `https://${" + HOST + "}/p`;"
    result = scan_text(text)
    assert result.findings == []
    assert result.redacted_text == text


_NESTED_TEMPLATE_PREFIXES = [
    'const prior = `outer ${`inner "`} tail`;',
    'const prior = `outer ${`inner ${name} "`} tail`;',
    'const prior = `outer ${`inner ${`deep "`}`} tail`;',
]


@pytest.mark.parametrize("prefix", _NESTED_TEMPLATE_PREFIXES)
@pytest.mark.parametrize("action", ["allow", "warn", "redact", "block"])
def test_nested_prior_templates_cannot_hide_literal_string_hosts(prefix, action: Action):
    text = prefix + ' const text = "text => `https://${' + HOST + '}/p`";'
    result = scan_text(text, Policy(rules={"private-hostname": action}))
    assert [(f.rule_id, f.match, f.action) for f in result.findings] == [("private-hostname", HOST, action)]
    expected = text.replace(HOST, "[redacted-host]") if action in {"block", "redact"} else text
    assert result.redacted_text == expected
    assert result.blocked == (action == "block")


@pytest.mark.parametrize("prefix", _NESTED_TEMPLATE_PREFIXES)
def test_nested_prior_templates_keep_real_template_expression_code(prefix):
    text = prefix + " const url = `https://${" + HOST + "}/p`;"
    result = scan_text(text)
    assert result.findings == []
    assert result.redacted_text == text


def test_deep_prior_templates_do_not_require_python_recursion():
    prior = "const prior = " + "`outer ${" * 1000 + "`inner`" + "}`" * 1000 + ";"
    text = prior + ' const text = "text => `https://${' + HOST + '}/p`";'
    result = scan_text(text)
    assert [(f.rule_id, f.match) for f in result.findings] == [("private-hostname", HOST)]
    assert result.blocked
    assert result.redacted_text == text.replace(HOST, "[redacted-host]")
