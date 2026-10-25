"""Literal shell assignment words must not hide later host declarations."""

import pytest

from brigade.guard.engine import scan_text
from brigade.guard.policy import Policy
from brigade.guard.types import Action

HOST = "fixture-db" + ".internal"


@pytest.mark.parametrize(
    "value",
    [
        "pre{foo}post",
        "{foo}",
        "{one,two}",
        "pre{foo",
        "foo}post",
        "{}",
        "{",
        "}",
        "a{b{c}d}e",
        r"pre\{foo\}post",
        "'pre{two words}post'",
        '"pre{two words}post"',
        r"pre{two\ words}post",
        "plain",
        "",
    ],
)
@pytest.mark.parametrize("declaration", ["export", "readonly -x", "declare -x", "local", "set"])
@pytest.mark.parametrize("marker", ["", "- ", "  - "])
@pytest.mark.parametrize("action", ["allow", "warn", "redact", "block"])
def test_literal_assignment_words_preserve_host_decisions(value, declaration, marker, action: Action):
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
        ("export A=pre{foo}post ARBITRARY_VALUE=", ""),
        ("export A={foo} DATABASE_HOSTNAME_SUFFIX=", ""),
        ("export A={foo} obj.HOST=", ""),
        ("callback(export A={foo} HOST=", ")"),
        ("export A={foo}; HOST=", ""),
        ("export A={foo} | HOST=", ""),
        ("export A={foo} && HOST=", ""),
        ("A={foo} HOST=", ""),
        ("offset A={foo} HOST=", ""),
        ("local A={foo} host = ", ""),
        ("export A={foo} HOST=", " ? yes : no"),
        ("export A={foo} HOST=", ".example.com"),
        ("export A={foo} HOST=callback(", ")"),
        ("-- export A={foo} HOST=", ""),
    ],
)
def test_literal_assignment_words_preserve_exclusions(before, after):
    text = before + HOST + after
    result = scan_text(text, Policy(rules={"private-hostname": "redact"}))
    assert result.findings == []
    assert result.redacted_text == text


@pytest.mark.parametrize("keyword", ["return", "if", "const", "let", "var", "assert", "import"])
@pytest.mark.parametrize("shape", ["{}", "'{}'", '"{}"', "$(printf {})", '${{MODE:-"{}"}}'])
@pytest.mark.parametrize("declaration", ["export", "readonly -x", "declare -x", "local", "set", "@set /a"])
@pytest.mark.parametrize("action", ["allow", "warn", "redact", "block"])
def test_assignment_word_keywords_preserve_host_decisions(keyword, shape, declaration, action: Action):
    prefix = f"{declaration} A={shape.format(keyword)} HOST="
    text = prefix + HOST + " --verbose"
    result = scan_text(text, Policy(rules={"private-hostname": action}))
    assert [(f.rule_id, f.match, f.action) for f in result.findings] == [("private-hostname", HOST, action)]
    (finding,) = result.findings
    assert (finding.start, finding.end) == (len(prefix), len(prefix) + len(HOST))
    assert result.blocked == (action == "block")
    replacement = "[redacted-host]" if action in {"block", "redact"} else HOST
    assert result.redacted_text == prefix + replacement + " --verbose"


@pytest.mark.parametrize("keyword", ["return", "if", "const", "let", "var", "assert", "import"])
@pytest.mark.parametrize("prefix", ["{} export A=plain HOST=", "export {} A=plain HOST=", "callback({} A=plain HOST="])
def test_keywords_outside_assignment_values_remain_code_shaped(keyword, prefix):
    text = prefix.format(keyword) + HOST
    result = scan_text(text, Policy(rules={"private-hostname": "redact"}))
    assert result.findings == []
    assert result.redacted_text == text


@pytest.mark.parametrize(
    "value", ["'f\"{'", "'f\"{value'", "'f\"{{'", "'F\"{value'", "'rf\"{value'", "'fr\"{value'", "$(printf 'f\"{')"]
)
@pytest.mark.parametrize("declaration", ["export", "readonly -x", "declare -x", "local", "set", "@set /a"])
@pytest.mark.parametrize("marker", ["", "- ", "  - "])
@pytest.mark.parametrize("action", ["allow", "warn", "redact", "block"])
def test_assignment_word_fstring_text_preserves_host_decisions(value, declaration, marker, action: Action):
    prefix = f"{marker}{declaration} A={value} HOST="
    text = prefix + HOST + " --verbose"
    result = scan_text(text, Policy(rules={"private-hostname": action}))
    assert [(f.rule_id, f.match, f.action) for f in result.findings] == [("private-hostname", HOST, action)]
    (finding,) = result.findings
    assert (finding.start, finding.end) == (len(prefix), len(prefix) + len(HOST))
    assert result.blocked == (action == "block")
    replacement = "[redacted-host]" if action in {"block", "redact"} else HOST
    assert result.redacted_text == prefix + replacement + " --verbose"


@pytest.mark.parametrize("prefix", ['f"{', 'export A=plain f"{', 'callback(f"{', 'export A="unterminated f\\"{'])
@pytest.mark.parametrize("action", ["allow", "warn", "redact", "block"])
def test_actual_or_unproven_fstring_context_stays_excluded(prefix, action: Action):
    text = prefix + HOST + '}"'
    result = scan_text(text, Policy(rules={"private-hostname": action}))
    assert result.findings == []
    assert not result.blocked
    assert result.redacted_text == text
