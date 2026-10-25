"""Only template text can introduce a hostname replacement field."""

import pytest

from brigade.guard.engine import scan_text
from brigade.guard.policy import Policy
from brigade.guard.types import Action

HOST = "cfg.db" + ".local"
FIELD = "https://${" + HOST + "}/p"


@pytest.mark.parametrize(
    "source",
    [
        "const url = `https://${ /* " + FIELD + " */ value }`;",
        "const url = `https://${ /* prefix ` " + FIELD + " ` */ value }`;",
        "const url = `https://${ /* closed */ /* " + FIELD + " */ value }`;",
        "const url = `https://${ // " + FIELD + "\n value }`;",
        "const url = `https://${ (() => { /* " + FIELD + " */ return value; })() }`;",
        "const url = `https://${ '" + FIELD + "' }`;",
        'const url = `https://${ "' + FIELD + '" }`;',
        'const text = "text => `' + FIELD + '`";',
        "Use call(): `" + FIELD + "` as text.",
        "const n = 1; Use: `" + FIELD + "` as text.",
        "x = 1; # const url = `" + FIELD + "`",
        "SELECT 1; -- const url = `" + FIELD + "`",
        "/* const url = `" + FIELD + "` */",
        "const url = `https://${ /* " + FIELD,
    ],
)
@pytest.mark.parametrize("action", ["allow", "warn", "redact", "block"])
def test_interpolation_comment_and_literal_hosts_remain_detectable(source, action: Action):
    result = scan_text(source, Policy(rules={"private-hostname": action}))
    assert [(f.rule_id, f.match, f.action) for f in result.findings] == [("private-hostname", HOST, action)]
    start = source.index(HOST)
    assert [(f.start, f.end) for f in result.findings] == [(start, start + len(HOST))]
    assert result.blocked == (action == "block")
    replacement = "[redacted-host]" if action in {"redact", "block"} else HOST
    assert result.redacted_text == source[:start] + replacement + source[start + len(HOST) :]


@pytest.mark.parametrize(
    "source",
    [
        "const url = `" + FIELD + "`;",
        "const url = `https://${ /* comment */ value }/" + FIELD + "`;",
        "const url = `https://${ (() => { /* comment */ return value; })() }/" + FIELD + "`;",
        "/* closed comment */ const url = `" + FIELD + "`;",
        "const fn = () => `" + FIELD + "`;",
        "const obj = {url: `" + FIELD + "`};",
        "url = flag ? `" + FIELD + "` : fallback;",
        "fetch(flag ? fallback : `" + FIELD + "`);",
        "tag`" + FIELD + "`;",
    ],
)
@pytest.mark.parametrize("action", ["allow", "warn", "redact", "block"])
def test_actual_template_text_fields_remain_code(source, action: Action):
    result = scan_text(source, Policy(rules={"private-hostname": action}))
    assert result.findings == []
    assert not result.blocked
    assert result.redacted_text == source
