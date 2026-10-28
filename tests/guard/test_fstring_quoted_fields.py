"""Narrow quoted-subscript-field coverage for #1983."""

import pytest

from brigade.guard.engine import scan_text
from brigade.guard.policy import Policy


HOST = "db" + ".internal"


@pytest.mark.parametrize(
    "source",
    [
        "f\"https://{parts['key']}/{cfg.HOST}/p\"",
        "url = f\"https://{parts['key']}/{cfg.HOST}/p\"",
        "return f\"https://{parts['key']}/{cfg.HOST}/p\"",
        "fetch(f\"https://{parts['key']}/{cfg.HOST}/p\")",
        "rf\"https://{parts['key']}/{cfg.HOST}/p\"",
        "fr\"https://{parts['key']}/{cfg.HOST}/p\"",
        "f'https://{parts[\"key\"]}/{cfg.HOST}/p'",
        'f"""https://{parts[\'key\']}/{cfg.HOST}/p"""',
        "f\"https://{parts['key']}/{other['next']}/{ cfg.HOST }/p\"",
        "f\"https://{prefix}/{cfg.parts['key']}/{cfg.HOST!r}/p\"",
        "f\"https://{{{parts['key']}}}/{cfg.HOST:>{width}}/p\"",
        "f\"https://{parts['a}b{c']}/{cfg.HOST}/p\"",
        r"""f"https://{parts['can\'t']}/{cfg.HOST}/p" """,
    ],
)
def test_whole_host_field_after_quoted_subscript_is_code(source: str) -> None:
    text = source.replace("HOST", HOST)
    result = scan_text(text, policy=Policy(rules={"private-hostname": "redact"}))
    assert not [finding for finding in result.findings if finding.rule_id == "private-hostname"]
    assert result.redacted_text == text


@pytest.mark.parametrize(
    "source",
    [
        "f\"https://{parts['key']}/HOST/p\"",
        "f\"https://{parts['HOST']}/{cfg.name}/p\"",
        "f\"https://{parts['key']}/{'HOST'}/p\"",
        "f\"https://{parts['key']}/{lookup('HOST')}/p\"",
        "f\"https://{parts['key']}/{cfg.HOST[0]}/p\"",
        "f\"https://{parts['key']}/{cfg.HOST or fallback}/p\"",
        "f\"https://{parts['key']}/{value:HOST}/p\"",
        "f\"https://{parts['key']}/{{cfg.HOST}}/p\"",
        "\"https://{parts['key']}/{cfg.HOST}/p\"",
        "# f\"https://{parts['key']}/{cfg.HOST}/p\"",
        "// f\"https://{parts['key']}/{cfg.HOST}/p\"",
        "Use f\"https://{parts['key']}/{cfg.HOST}/p\" here.",
        'hint = \'f"https://{parts["key"]}/{cfg.HOST}/p"\'',
        'f"https://{parts[\'key\']}"; url = "https://{cfg.HOST}/p"',
        "f\"https://{parts['key']}/{cfg.HOST}/p\" # https://HOST/p",
        'f"https://{parts[\'key\']}/{cfg.HOST}/p"; url = "https://HOST/p"',
    ],
)
def test_quoted_fields_do_not_hide_literal_hosts(source: str) -> None:
    text = source.replace("HOST", HOST)
    result = scan_text(text, policy=Policy(rules={"private-hostname": "redact"}))
    hosts = [finding for finding in result.findings if finding.rule_id == "private-hostname"]
    assert hosts
    assert result.redacted_text.count(HOST) < text.count(HOST)
