"""Line comments stop at every JavaScript line terminator."""

import pytest

from brigade.guard.engine import scan_text
from brigade.guard.policy import Policy
from brigade.guard.types import Action

HOST = "cfg.db" + ".local"
FIELD = "https://${" + HOST + "}/p"
LINE_ENDS = ["\n", "\r", "\r\n", "\u2028", "\u2029"]


@pytest.mark.parametrize("line_end", LINE_ENDS)
@pytest.mark.parametrize("comment", ["// comment", "# comment", "-- comment"])
@pytest.mark.parametrize("action", ["allow", "warn", "redact", "block"])
def test_line_end_does_not_skip_enclosing_block_comment(line_end, comment, action: Action):
    source = comment + line_end + "/*\nconst url = `" + FIELD + "`;\n*/"
    result = scan_text(source, Policy(rules={"private-hostname": action}))
    start = source.index(HOST)
    assert [(f.rule_id, f.match, f.action, f.start, f.end) for f in result.findings] == [
        ("private-hostname", HOST, action, start, start + len(HOST))
    ]
    assert result.blocked == (action == "block")
    replacement = "[redacted-host]" if action in {"redact", "block"} else HOST
    assert result.redacted_text == source[:start] + replacement + source[start + len(HOST) :]


@pytest.mark.parametrize("line_end", LINE_ENDS)
@pytest.mark.parametrize("comment", ["// comment", "# comment", "-- comment"])
@pytest.mark.parametrize("between", ["", "/* closed comment */ "])
@pytest.mark.parametrize("action", ["allow", "warn", "redact", "block"])
def test_real_code_after_terminated_comment_stays_exempt(line_end, comment, between, action: Action):
    source = comment + line_end + between + "const url = `" + FIELD + "`;"
    result = scan_text(source, Policy(rules={"private-hostname": action}))
    assert result.findings == []
    assert not result.blocked
    assert result.redacted_text == source


@pytest.mark.parametrize("line_end", LINE_ENDS)
@pytest.mark.parametrize("action", ["allow", "warn", "redact", "block"])
def test_terminator_inside_quoted_string_is_not_a_line_comment(line_end, action: Action):
    # An escaped physical newline or literal Unicode separator remains string
    # data. A comment-shaped substring must not change its literal host role.
    physical = "\\" + line_end if line_end in {"\n", "\r", "\r\n"} else line_end
    source = 'const note = "// comment' + physical + FIELD + '";'
    result = scan_text(source, Policy(rules={"private-hostname": action}))
    start = source.index(HOST)
    assert [(f.match, f.start, f.end) for f in result.findings] == [(HOST, start, start + len(HOST))]
    assert result.blocked == (action == "block")
    replacement = "[redacted-host]" if action in {"redact", "block"} else HOST
    assert result.redacted_text == source[:start] + replacement + source[start + len(HOST) :]


@pytest.mark.parametrize("non_end", ["\t", "\v", "\f", "\x85", "\xa0", "\u2000", r"\u2028", r"\u2029"])
@pytest.mark.parametrize("action", ["allow", "warn", "redact", "block"])
def test_other_whitespace_and_escape_text_do_not_end_line_comments(non_end, action: Action):
    source = "// comment" + non_end + "const literal = `" + FIELD + "`;\nconst real = `" + FIELD + "`;"
    result = scan_text(source, Policy(rules={"private-hostname": action}))
    start = source.index(HOST)
    assert [(f.match, f.start, f.end) for f in result.findings] == [(HOST, start, start + len(HOST))]
    assert result.blocked == (action == "block")
    replacement = "[redacted-host]" if action in {"redact", "block"} else HOST
    assert result.redacted_text == source[:start] + replacement + source[start + len(HOST) :]
