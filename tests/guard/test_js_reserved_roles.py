"""Reserved tokens must not fall through to an identifier's slash role."""

import pytest

from brigade.guard.engine import scan_text
from brigade.guard.policy import Policy
from brigade.guard.types import Action

HOST = "cfg.db" + ".local"
WORDS = (
    "as async await break case catch class const continue debugger default delete do else enum export extends "
    "false finally for from function get if implements import in instanceof interface let meta new null of package "
    "private protected public return set static super switch target this throw true try typeof using var void while with yield"
).split()


def check(text: str, action: Action, detected: bool) -> None:
    result = scan_text(text, Policy(rules={"private-hostname": action}))
    assert [(f.rule_id, f.match, f.action, f.start, f.end) for f in result.findings] == (
        [("private-hostname", HOST, action, text.index(HOST), text.index(HOST) + len(HOST))] if detected else []
    )
    assert result.blocked == (detected and action == "block")
    expected = text.replace(HOST, "[redacted-host]") if detected and action in {"redact", "block"} else text
    assert result.redacted_text == expected


@pytest.mark.parametrize("word", WORDS)
@pytest.mark.parametrize("shape", ["member", "optional", "call", "property"])
@pytest.mark.parametrize("literal", [False, True])
@pytest.mark.parametrize("action", ["allow", "warn", "redact", "block"])
def test_reserved_and_contextual_property_roles(word, shape, literal, action: Action):
    url = "`https://${" + HOST + "}/p`"
    value = '"() => ' + url + '"' if literal else url
    if shape == "property":
        text = "const config={" + word + ":" + value + "};"
    else:
        access = "obj?." if shape == "optional" else "obj."
        before = access + word + ("(value)" if shape == "call" else "") + ' / "x/" / denominator;'
        text = before + "const text=" + value + ";"
    check(text, action, literal)


@pytest.mark.parametrize(
    "prefix,suffix",
    [
        ('switch(x){case /"/:', "}"),
        ('function f(){switch(x){case /"/:', "}}"),
        ('switch(x){case /"/:{', "}}"),
        ('switch(x){case /["`]/:', "}"),
        ('const C=class extends /"/ {};', ""),
        ('function f(){const C=class extends /"/ {};', "}"),
        ('let / "x/" / denominator;', ""),
        ('let /*comment*/ / "x/" / denominator;', ""),
    ],
)
@pytest.mark.parametrize("tail", ["() => ", "{url: ", "const url = ", "value => "])
@pytest.mark.parametrize("action", ["allow", "warn", "redact", "block"])
def test_unsupported_keyword_roles_keep_literal_hosts(prefix, suffix, tail, action: Action):
    check(prefix + 'const text="' + tail + "`https://${" + HOST + '}/p`";' + suffix, action, True)


@pytest.mark.parametrize(
    "prefix",
    [
        'var let=1; let / "x/" / denominator;',
        'var let=1; let /*comment*/ / "x/" / denominator;',
        'var let=fn; let() / "x/" / denominator;',
        "var let=1; let item=2;",
    ],
)
@pytest.mark.parametrize("literal", [False, True])
@pytest.mark.parametrize("action", ["allow", "warn", "redact", "block"])
def test_proven_legacy_let_binding_preserves_division(prefix, literal, action: Action):
    url = "`https://${" + HOST + "}/p`"
    value = '"() => ' + url + '"' if literal else url
    check(prefix + "const text=" + value + ";", action, literal)


@pytest.mark.parametrize("word", ["const", "let", "var", "function", "class", "if", "for", "while", "switch"])
@pytest.mark.parametrize("action", ["allow", "warn", "redact", "block"])
def test_incomplete_declaration_and_control_text_retains_detection(word, action: Action):
    # General text, deliberately not claimed to be syntactically valid JS.
    text = word + ' /*comment*/ /"/; const text="() => `https://${' + HOST + '}/p`";'
    check(text, action, True)
