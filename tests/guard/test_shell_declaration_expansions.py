"""Preceding shell assignment expansions must not hide host declarations."""

import pytest

from brigade.guard.engine import scan_text
from brigade.guard.policy import Policy
from brigade.guard.types import Action

HOST = "fixture-db" + ".internal"
VALUES = [
    "${MODE:-dev}",
    "$(printf fixture)",
    "`printf fixture`",
    "$((1 + 2))",
    '${MODE:-$(printf "two words")}',
    '$(printf "%s" "${MODE:-dev}")',
    '"${MODE:-dev}"',
    "pre${MODE:-dev}post",
    "$(printf '(')",
    '$(printf "a;b")',
    '$(printf "a\\"b")',
    "${MODE:-${OTHER:-dev}}",
    "${MODE:-{}}",
    "${MODE:-{foo}}",
    "${MODE:-{one,two}}",
    "${MODE:-${OTHER:-{}}}",
    "pre{${MODE:-dev}post",
    "${MODE:-dev}post}",
    "${MODE:-a{b}",
    "${MODE:-${OTHER:-a{b}}",
    '"${MODE:-a{b}"',
    '"${MODE:-${OTHER:-a{b}}"',
    '${MODE:-"a{b"}',
    '"$(printf "%s" "two words")"',
    '"${MODE:-"two words"}"',
    '"pre$(printf "%s" "${OTHER:-two words}")post"',
    '"${MODE:-$(printf "%s" "two words")}"',
]


@pytest.mark.parametrize("value", VALUES)
@pytest.mark.parametrize("declaration", ["export", "export -n", "readonly -x", "declare -x", "local"])
@pytest.mark.parametrize("marker", ["", "- ", "  - "])
@pytest.mark.parametrize("action", ["allow", "warn", "redact", "block"])
def test_expansion_values_preserve_host_detection_and_mask(value, declaration, marker, action: Action):
    prefix = f"{marker}{declaration} A={value} B=second DATABASE_HOST="
    text = prefix + HOST + "\n"
    result = scan_text(text, Policy(rules={"private-hostname": action}))
    assert [(f.rule_id, f.match, f.action) for f in result.findings] == [("private-hostname", HOST, action)]
    (finding,) = result.findings
    assert (finding.start, finding.end) == (len(prefix), len(prefix) + len(HOST))
    assert result.blocked == (action == "block")
    replacement = "[redacted-host]" if action in {"block", "redact"} else HOST
    assert result.redacted_text == prefix + replacement + "\n"


@pytest.mark.parametrize(
    "before,after",
    [
        ("export A=${MODE:-dev} ARBITRARY_VALUE=", ""),
        ("export A=$(printf x) DATABASE_HOSTNAME_SUFFIX=", ""),
        ("export A=$(printf x) obj.HOST=", ""),
        ("callback(export A=$(printf x) HOST=", ")"),
        ("export A=$(printf x); HOST=", ""),
        ("export A=$(printf x) | HOST=", ""),
        ("export A=$(printf x) && HOST=", ""),
        ("A=$(printf x) HOST=", ""),
        ("offset A=$(printf x) HOST=", ""),
        ("local A=$(printf x) host = ", ""),
        ("export A=$(printf x) HOST=", " ? yes : no"),
        ("export A=$(printf x) HOST=", ".example.com"),
        ("export A=${MODE:-dev HOST=", ""),
        ("export A=$(printf x HOST=", ""),
        ("export A=$(printf '(') HOST=callback(", ")"),
        ("-- export A=${MODE:-dev} HOST=", ""),
    ],
)
def test_expansion_context_preserves_code_and_malformed_exclusions(before, after):
    text = before + HOST + after
    result = scan_text(text, Policy(rules={"private-hostname": "redact"}))
    assert result.findings == []
    assert result.redacted_text == text


def test_deep_balanced_expansions_do_not_require_python_recursion():
    value = "$(" * 5000 + "printf x" + ")" * 5000
    text = "export A=" + value + " HOST=" + HOST
    result = scan_text(text)
    assert [(finding.rule_id, finding.match) for finding in result.findings] == [("private-hostname", HOST)]
    assert result.blocked
    assert result.redacted_text == "export A=" + value + " HOST=[redacted-host]"


def test_unclosed_quoted_text_retains_existing_literal_host_detection():
    text = "export A=`printf x HOST=" + HOST
    result = scan_text(text)
    assert [(finding.rule_id, finding.match) for finding in result.findings] == [("private-hostname", HOST)]
    assert result.blocked
