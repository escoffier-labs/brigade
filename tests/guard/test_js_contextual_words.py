"""Unproven contextual keyword roles must never corrupt literal quote state."""

import pytest

from brigade.guard.engine import scan_text
from brigade.guard.policy import Policy
from brigade.guard.types import Action

HOST = "cfg.db" + ".local"


def check(text: str, action: Action, detected: bool) -> None:
    result = scan_text(text, Policy(rules={"private-hostname": action}))
    assert [(f.rule_id, f.match, f.action, f.start, f.end) for f in result.findings] == (
        [("private-hostname", HOST, action, text.index(HOST), text.index(HOST) + len(HOST))] if detected else []
    )
    assert result.blocked == (detected and action == "block")
    expected = text.replace(HOST, "[redacted-host]") if detected and action in {"redact", "block"} else text
    assert result.redacted_text == expected


@pytest.mark.parametrize(
    "prefix,suffix",
    [
        ('const x = await /"/;', ""),
        ('function* g(){ const x = yield /"/;', "}"),
        ('let yield = 1; function* g(){ const x = yield /"/;', "}"),
        ('let await = 1; async function f(){ const x = await /"/;', "}"),
    ],
)
@pytest.mark.parametrize("tail", ["() => ", "{url: ", "const url = "])
@pytest.mark.parametrize("action", ["allow", "warn", "redact", "block"])
def test_keyword_contexts_retain_literal_hosts(prefix, suffix, tail, action: Action):
    text = prefix + ' const text = "' + tail + "`https://${" + HOST + '}/p`";' + suffix
    check(text, action, True)


@pytest.mark.parametrize(
    "prefix,suffix",
    [
        ('let await = 1; await / "x/" / denominator;', ""),
        ('let yield = 1; yield / "x/" / denominator;', ""),
        ('obj.await / "x/" / denominator;', ""),
        ('obj.yield / "x/" / denominator;', ""),
        ('obj?.await / "x/" / denominator;', ""),
        ('function f(){ let yield = 1; yield / "x/" / denominator;', "}"),
        ('let yield = 1; function f(){ yield / "x/" / denominator;', "}"),
        ('let await = 1; function f(){ await / "x/" / denominator;', "}"),
    ],
)
@pytest.mark.parametrize("literal", [False, True])
@pytest.mark.parametrize("action", ["allow", "warn", "redact", "block"])
def test_proven_identifier_and_member_roles_keep_division(prefix, suffix, literal, action: Action):
    url = "`https://${" + HOST + "}/p`"
    tail = ' const text = "() => ' + url + '";' if literal else " const url = " + url + ";"
    check(prefix + tail + suffix, action, literal)
