"""Async arrow scope must not inherit an outer await identifier role."""

import pytest

from brigade.guard.engine import scan_text
from brigade.guard.policy import Policy
from brigade.guard.types import Action

HOST = "cfg.db" + ".local"


def check(text: str, action: Action, detected: bool) -> None:
    result = scan_text(text, Policy(rules={"private-hostname": action}))
    expected = [("private-hostname", HOST, action, text.index(HOST), text.index(HOST) + len(HOST))] if detected else []
    assert [(f.rule_id, f.match, f.action, f.start, f.end) for f in result.findings] == expected
    assert result.blocked == (detected and action == "block")
    assert result.redacted_text == (
        text.replace(HOST, "[redacted-host]") if detected and action in {"redact", "block"} else text
    )


@pytest.mark.parametrize(
    "prefix,suffix",
    [
        ('let await=1; const f=async()=>{const x=await /"/;', "};"),
        ('let await=1; const f=(async()=>{const x=await /"/;', "});"),
        ('let await=1; const f=async/*comment*/()=>{const x=await /"/;', "};"),
        ('let await=1; const f=async(value)=>{const x=await /"/;', "};"),
        ('let await=1; function outer(){const f=async()=>{const x=await /"/;', "};}"),
        ('let await=1; const f=async()=> await /"/;', ""),
        ('let await=1; const f=async()=>void(await /"/);', ""),
    ],
)
@pytest.mark.parametrize("tail", ["() => ", "{url: ", "const url = "])
@pytest.mark.parametrize("action", ["allow", "warn", "redact", "block"])
def test_async_keyword_scope_preserves_literal_hostname(prefix, suffix, tail, action: Action):
    text = prefix + 'const text="' + tail + "`https://${" + HOST + '}/p`";' + suffix
    check(text, action, True)


@pytest.mark.parametrize(
    "prefix,suffix",
    [
        ("const f=async()=>", ";"),
        ("const f=async()=>(", ");"),
        ("const f=async()=>async()=>(", ");"),
        ("const f=()=>(", ");"),
        ("const f=async(value)=>", ";"),
        ("consume(async()=>", ");"),
        ("const f=[async()=>", "];"),
    ],
)
@pytest.mark.parametrize("action", ["allow", "warn", "redact", "block"])
def test_proven_async_arrow_template_position(prefix, suffix, action: Action):
    check(prefix + "`https://${" + HOST + "}/p`" + suffix, action, False)


@pytest.mark.parametrize(
    "prefix",
    [
        'let await=1; const f=async()=>0; await / "x/" / denominator;',
        'let await=1; const f=async()=>{}; await / "x/" / denominator;',
        'let await=1; const f=[async()=>0]; await / "x/" / denominator;',
        'let await=1; consume(async()=>0); await / "x/" / denominator;',
        'let await=1; const f=async()=>async()=>0; await / "x/" / denominator;',
        'let async=fn; async() / "x/" / denominator;',
        'obj.async() / "x/" / denominator;',
    ],
)
@pytest.mark.parametrize("literal", [False, True])
@pytest.mark.parametrize("action", ["allow", "warn", "redact", "block"])
def test_async_scope_exit_and_ordinary_async_calls(prefix, literal, action: Action):
    url = "`https://${" + HOST + "}/p`"
    tail = ' const text="() => ' + url + '";' if literal else " const url=" + url + ";"
    check(prefix + tail, action, literal)
