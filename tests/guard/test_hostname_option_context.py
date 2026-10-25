"""Equals-form host options preserve shell boundaries and code exclusions."""

import pytest

from brigade.guard.engine import scan_text
from brigade.guard.policy import Policy

HOST = "fixture-db" + ".internal"

POSITIVE_FORMS = [
    "prepare; tool --host={host}",
    "prepare;tool --host={host}",
    "tool --host={host}; echo done",
    "tool --host={host};echo done",
    "prepare;tool --host={host};echo done",
    "(tool --host={host})",
    "prepare; (tool --host={host}); echo done",
    "{{ tool --host={host}; }}",
    "value=$(tool --host={host})",
    "$(tool --host={host})",
    "value=$( (tool --host={host}))",
    "value=$(echo $(tool --host={host}))",
    "value=$(prepare; (tool --host={host}); echo done)",
    "prepare && tool --host={host} || echo done",
    "prepare | tool --host={host} | finish",
    'tool "earlier; (argument)" --host={host}',
    r"tool earlier\;argument --host={host}",
    r"tool \(earlier\) --host={host}",
    'tool "(" --host={host}',
    "tool '--unrelated=;' --host={host}",
    "tool --output=json --host={host} --verbose",
    "tool --labels=a,b --host={host}",
    "prepare; tool --labels=a,b --host={host}; echo done",
    "tool --pattern=a?b --host={host}",
    "prepare; tool --pattern=a?b --host={host}; echo done",
    "tool --host={host} --labels=a,b",
    "prepare; tool --host={host} # don't print",
    "prepare; tool --host={host} --later=${{VALUE}}",
    "prepare; tool --host={host}; echo $((1+2))",
    "(tool --host={host} &)",
    "prepare; tool --host={host} &",
    "(tool --host={host}) # don't print",
    "(tool --host={host} --later=${{VALUE}})",
    "(tool --host={host}; echo $((1+2)))",
    "{{ tool --host={host} & }}",
    "value=$(tool --host={host} --later=${{VALUE}})",
    "prepare; tool --later=${{VALUE}} --host={host}",
    "prepare; tool --earlier=$((1+(2))) --host={host}",
    "prepare; tool --earlier=$(printf value) --host={host}",
    "prepare; tool --earlier=${{VALUE:-${{OTHER}}}} --host={host}",
    "prepare; tool --host={host} --later=${{VALUE:-${{OTHER}}}}",
    "(tool --host={host}); callback --later=${{VALUE}}",
    'prepare; tool --host={host}; echo "don\'t print"',
    "prepare; tool --host={host}; printf '(' reference",
    "prepare; tool --host={host}; # don't print",
    "(tool --host={host}; echo nested $(printf x))",
    "[ -e config ] && tool --host={host}",
    "([ -e config ] && tool --host={host})",
    "[ -e config ] && (tool --host={host})",
    "echo [ab]; tool --host={host}",
    "(echo [ab]; tool --host={host})",
    "echo [ab] | tool --host={host}",
    "echo [ab] || tool --host={host}",
]


@pytest.mark.parametrize("form", POSITIVE_FORMS)
@pytest.mark.parametrize("action", ["block", "redact"])
def test_shell_option_context_exact_findings_and_output(form, action):
    text = form.format(host=HOST)
    result = scan_text(text, policy=Policy(rules={"private-hostname": action}))
    assert [(f.rule_id, f.match, f.action) for f in result.findings] == [("private-hostname", HOST, action)]
    (finding,) = result.findings
    assert (finding.start, finding.end) == (text.index(HOST), text.index(HOST) + len(HOST))
    assert result.blocked == (action == "block")
    assert result.redacted_text == text.replace(HOST, "[redacted-host]")


@pytest.mark.parametrize(
    "key", ["host", "hostname", "server", "endpoint", "domain", "url", "address", "database-host", "DB_HOST"]
)
@pytest.mark.parametrize("hyphens", ["-", "--"])
@pytest.mark.parametrize("suffix", ["", " --verbose"])
def test_shell_option_keys_and_repeated_hosts(key, hyphens, suffix):
    other = "fixture-cache" + ".internal"
    text = f"prepare; tool {hyphens}{key}={HOST} --host={other}{suffix}; echo done"
    result = scan_text(text, policy=Policy(rules={"private-hostname": "redact"}))
    assert [f.match for f in result.findings] == [HOST, other]
    assert result.redacted_text == text.replace(HOST, "[redacted-host]").replace(other, "[redacted-host]")


@pytest.mark.parametrize(
    "form",
    [
        "tool --arbitrary-value={host}",
        "tool --hostname-suffix={host}",
        "tool --ghost={host}",
        "tool --databasehost={host}",
        "arbitrary_value={host}",
        "obj.host={host}",
        "const value = tool --host={host};",
        "let value = tool --host={host};",
        "var value = tool --host={host};",
        "callback(--host={host})",
        "callback( --host={host})",
        "callback ( --host={host})",
        "callback(host={host})",
        "tool --host={host}()",
        "tool --host={host} ? yes : no",
        "tool --host={host}.example.com",
        "value=$((tool --host={host}))",
        "(tool --host={host}",
        "tool --host={host})",
        "{{ tool --host={host} }}",
        "{{tool --host={host}; }}",
        "tool (earlier) --host={host}",
        'const value = "earlier;"; tool --host={host}',
        "(tool --host={host} &&)",
        "(tool --host={host} |)",
        "{{ tool --host={host} && }}",
        "{{ tool --host={host} &",
        "(tool --host={host} # don't close )",
        "prepare; callback( --host={host})",
        "const value = tool --later=${{VALUE}} --host={host};",
        "callback( --earlier=${{VALUE}} --host={host})",
        "prepare; tool --later=$((--host={host}))",
        "prepare; tool --later=${{VALUE:- --host={host}}}",
        "prepare; tool --host={host}}}",
        "prepare; tool [ab] --host={host}",
        "obj[slot] --host={host}",
        "callback([thing], --host={host})",
        "const value = [thing]; tool --host={host}",
        "prepare; callback([thing], --host={host})",
    ],
)
def test_code_and_malformed_group_exclusions(form):
    text = form.format(host=HOST)
    result = scan_text(text, policy=Policy(rules={"private-hostname": "redact"}))
    assert result.findings == []
    assert not result.blocked
    assert result.redacted_text == text


@pytest.mark.parametrize("backslashes", range(6))
@pytest.mark.parametrize("action", ["block", "redact"])
def test_comment_boundary_respects_escaped_whitespace(backslashes, action):
    # Odd backslash counts escape the space: the hash is argument data and
    # the separate host option remains executable. Even counts start a comment.
    text = "prepare; tool label" + "\\" * backslashes + " #value --host=" + HOST
    result = scan_text(text, policy=Policy(rules={"private-hostname": action}))
    if backslashes % 2:
        assert [(f.rule_id, f.match) for f in result.findings] == [("private-hostname", HOST)]
        (finding,) = result.findings
        assert (finding.start, finding.end) == (text.index(HOST), text.index(HOST) + len(HOST))
        assert result.blocked == (action == "block")
        assert result.redacted_text == text.replace(HOST, "[redacted-host]")
    else:
        assert result.findings == []
        assert not result.blocked
        assert result.redacted_text == text
