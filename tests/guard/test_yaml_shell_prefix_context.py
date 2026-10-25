"""YAML sequence markers compose with existing shell declaration prefixes."""

import pytest

from brigade.guard.engine import scan_text
from brigade.guard.policy import Policy

HOST = "fixture-db" + ".internal"


@pytest.mark.parametrize("marker", ["", "- ", "  - ", "\t-\t"])
@pytest.mark.parametrize("declaration", ["export", "readonly", "set"])
@pytest.mark.parametrize("key", ["DATABASE_HOST", "database-host", "server"])
@pytest.mark.parametrize("action", ["block", "redact"])
def test_sequence_and_shell_declaration_compose(marker, declaration, key, action):
    prefix = f"script:\n{marker}{declaration} {key}="
    text = prefix + HOST + "\n"
    result = scan_text(text, policy=Policy(rules={"private-hostname": action}))
    assert [(f.rule_id, f.match, f.action) for f in result.findings] == [("private-hostname", HOST, action)]
    (finding,) = result.findings
    assert (finding.start, finding.end) == (len(prefix), len(prefix) + len(HOST))
    assert result.blocked == (action == "block")
    assert result.redacted_text == prefix + "[redacted-host]\n"


@pytest.mark.parametrize(
    "before,after",
    [
        ("- export arbitrary_value=", ""),
        ("- export DATABASE_HOSTNAME_SUFFIX=", ""),
        ("- export obj.DATABASE_HOST=", ""),
        ("- const DATABASE_HOST=", ";"),
        ("- export host=callback(", ")"),
        ("- export host=", " ? yes : no"),
        ("- export host=", ".example.com"),
        ("-export DATABASE_HOST=", ""),
        ("-- export DATABASE_HOST=", ""),
    ],
)
def test_sequence_declarations_preserve_exclusions(before, after):
    text = before + HOST + after
    result = scan_text(text, policy=Policy(rules={"private-hostname": "redact"}))
    assert result.findings == []
    assert not result.blocked
    assert result.redacted_text == text


@pytest.mark.parametrize("action", ["block", "redact"])
def test_sequence_declare_assignment_matches_and_redacts(action):
    prefix = "- declare DATABASE_HOST="
    text = prefix + HOST
    result = scan_text(text, policy=Policy(rules={"private-hostname": action}))
    assert [(f.rule_id, f.match, f.action) for f in result.findings] == [("private-hostname", HOST, action)]
    (finding,) = result.findings
    assert (finding.start, finding.end) == (len(prefix), len(prefix) + len(HOST))
    assert result.blocked == (action == "block")
    assert result.redacted_text == prefix + "[redacted-host]"
