"""Token roles must preserve literal detection across completed JS prefixes."""

import pytest

from brigade.guard.engine import scan_text
from brigade.guard.policy import Policy
from brigade.guard.types import Action

HOST = "cfg.db" + ".local"
PREFIXES = [
    'const ratio = numerator++ / "/".length;',
    'const ratio = numerator-- / "/".length;',
    'const ratio = {} / "x/" / denominator;',
    'const ratio = function() {} / "x/" / denominator;',
    'const ratio = function named() {} / "x/" / denominator;',
    'const ratio = class Named {} / "x/" / denominator;',
    'const ratio = class {} / "x/" / denominator;',
    'obj.return / "x/" / denominator;',
    'obj.of / "x/" / denominator;',
    'obj.if(ready) / "x/" / denominator;',
    'obj.while(ready) / "x/" / denominator;',
    'obj?.return / "x/" / denominator;',
    'let of = 1; of / "x/" / denominator;',
    'let yield = 1; yield / "x/" / denominator;',
    'let await = 1; await / "x/" / denominator;',
    "const prior = `value ${a / b}`;",
    "const prior = `value ${a /= b}`;",
    "const prior = `value ${(a / b)}`;",
    "const prior = `value ${a / b / c}`;",
    "const prior = `value ${obj.value / divisor}`;",
    "const prior = `value ${`inner ${a / b}`}`;",
    'const prior = `value ${numerator++ / "/".length}`;',
    'const prior = `value ${obj.return / "x/" / denominator}`;',
    'const prior = `value ${(() => {if (ready) /"/.test(value)})()}`;',
    'if (ready) /"/.test(value);',
    'if (ready) {} /"/.test(value);',
    "const prior = {} / denominator;",
    "const prior = function() {} / denominator;",
    'const re = /"/;',
    'const prior = `outer ${`inner "`} tail`;',
]


def check(text: str, action: Action, detected: bool) -> None:
    result = scan_text(text, Policy(rules={"private-hostname": action}))
    if detected:
        assert [(f.rule_id, f.match, f.action, f.start, f.end) for f in result.findings] == [
            ("private-hostname", HOST, action, text.index(HOST), text.index(HOST) + len(HOST))
        ]
    else:
        assert result.findings == []
    assert result.blocked == (detected and action == "block")
    expected = text.replace(HOST, "[redacted-host]") if detected and action in {"redact", "block"} else text
    assert result.redacted_text == expected


@pytest.mark.parametrize("prefix", PREFIXES)
@pytest.mark.parametrize("shape", ["arrow", "property", "code"])
@pytest.mark.parametrize("action", ["allow", "warn", "redact", "block"])
def test_completed_prefix_token_roles(prefix, shape, action: Action):
    url = "`https://${" + HOST + "}/p`"
    if shape == "code":
        tail = " const url = " + url + ";"
    else:
        tail = ' const text = "' + ("text => " if shape == "arrow" else "{url: ") + url + '";'
    # Preserve the existing general-text '-- ' comment exclusion.
    detected = shape != "code" or prefix == PREFIXES[1]
    check(prefix + tail, action, detected)


@pytest.mark.parametrize(
    "prefix,suffix",
    [
        ("Use call(): ", " as text."),
        ("Use return ", " as text."),
        ("Use new ", " as text."),
        ("; const url = ", ""),
        ("* const url = ", ""),
        ("Example x=1; Use: ", " as text."),
        ("const n = 1; Use: ", " as text."),
        ("x = 1; # const url = ", ""),
        ("SELECT 1; -- const url = ", ""),
        ("x = 1; # text => ", ""),
    ],
)
@pytest.mark.parametrize("action", ["allow", "warn", "redact", "block"])
def test_distant_anchors_and_non_js_comments_keep_detection(prefix, suffix, action: Action):
    check(prefix + "`https://${" + HOST + "}/p`" + suffix, action, True)


@pytest.mark.parametrize(
    "prefix,suffix",
    [
        ("url = ok ? ", " : fallback;"),
        ("url = ok ? fallback : ", ";"),
        ("fetch(ok ? ", " : fallback);"),
        ("fetch(ok ? fallback : ", ");"),
        ("fetch(outer ? inner ? fallback : ", " : other);"),
        ("const options = {url: ", "};"),
        ("const options = {'url': ", "};"),
        ('const options = {"url": ', "};"),
        ("const mapper = value => ", ";"),
        ("const value = String.raw ", ";"),
    ],
)
@pytest.mark.parametrize("action", ["allow", "warn", "redact", "block"])
def test_current_expression_anchors(prefix, suffix, action: Action):
    check(prefix + "`https://${" + HOST + "}/p`" + suffix, action, False)
