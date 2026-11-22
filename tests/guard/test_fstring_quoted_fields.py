"""Private hostname detection in Python f-strings."""

import sys

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


@pytest.mark.parametrize(
    "source",
    [
        "f\"https://{lookup('key')}/{cfg.HOST}/p\"",
        "f\"https://{parts['a']['b']}/{cfg.HOST}/p\"",
        "f\"https://{f'{cfg.HOST}'}/p\"",
        'url = f"""\nhttps://{cfg.HOST}/p\n"""',
        'url = f"""\r\nhttps://{cfg.HOST}/p\r\n"""',
        'url = f"""https://\n{cfg.HOST}/p\n"""',
        'f"https://{lookup(cfg.HOST)}/p"',
        'f"https://{parts[cfg.HOST]}/p"',
        'f"https://{x==cfg.HOST}/p"',
        'f"https://{fallback or cfg.HOST}/p"',
        'f"https://{x == cfg.HOST}/p"',
        "f\"https://{lookup('café', cfg.HOST)}/p\"",
    ],
)
def test_fstring_hostname_expressions_are_code(source: str) -> None:
    text = source.replace("HOST", HOST)
    result = scan_text(text, policy=Policy(rules={"private-hostname": "redact"}))
    assert not [finding for finding in result.findings if finding.rule_id == "private-hostname"]
    assert result.redacted_text == text


@pytest.mark.parametrize(
    "source",
    [
        'f"https://LITERAL/{lookup(cfg.HOST)}/p"',
        'f"https://{lookup(cfg.HOST)}/LITERAL"',
        'f"https://{x:>HOST}/p"',
        'f"https://{x:.2f+HOST}/p"',
        'f"https://{{lookup(cfg.HOST)}}/p"',
        "f\"https://{{f'{cfg.HOST}'}}/p\"",
        "f\"https://{base}/{{f'{cfg.HOST}'}}/p\"",
        "f\"https://{f'{{cfg.HOST}}'}/p\"",
        "f\"https://{lookup('cfg.HOST')}/p\"",
        "f\"https://{parts['cfg.HOST']}/p\"",
        "f\"https://{x=='cfg.HOST'}/p\"",
        "f\"https://{x or 'HOST'}/p\"",
        "f\"https://{'HOST' if a else b}/p\"",
        "f\"https://{f('HOST')}/p\"",
        "f\"https://{d['HOST']}/p\"",
        'f"""https://{f"{\'HOST\'}"}/p"""',
        "f\"https://{x//'HOST'}/p\"",
        "f\"https://{x#'HOST'}/p\"",
        r"""f"https://{lookup('''a\''''+'HOST')}/p" """,
        '/*\nurl = f"https://{lookup(HOST)}/x"\n*/',
        'const x = 1; /* sample = f"https://{lookup(HOST)}/x" */',
        '; url = f"https://{lookup(HOST)}/x"',
        "* url = f\"https://{f'{HOST}'}/x\"",
        'hint = """\nf"https://{HOST}/p"\n"""',
        'hint = `\nf"https://{HOST}/p"\n`',
        'hint = """\nf"{HOST}"\n"""',
        'hint = `\nf"{HOST}"\n`',
        'f"{value} literal HOST"',
        "f\"{lookup('cfg.HOST')}\"",
        "config = {'key': f\"{x or 'HOST'}\"}",
        "value = prefix + f\"{'HOST' if a else b}\"",
    ],
)
def test_fstring_literal_host_remains_reported(source: str) -> None:
    text = source.replace("HOST", HOST)
    host = HOST
    if "LITERAL" in text:
        start = text.index("LITERAL")
        text = text.replace("LITERAL", HOST)
    else:
        if "cfg." + HOST in text:
            host = "cfg." + HOST
        start = text.index(host)
    result = scan_text(text, policy=Policy(rules={"private-hostname": "redact"}))
    end = start + len(host)
    assert [(f.start, f.end, f.match) for f in result.findings if f.rule_id == "private-hostname"] == [
        (start, end, host)
    ]
    assert result.redacted_text == text[:start] + "[redacted-host]" + text[end:]


@pytest.mark.parametrize(
    "source",
    [
        r"""f"https://{parts['can\'t']}/{cfg.HOST}/p" """,
        """f"https://{parts["key"]}/{cfg.HOST}/p" """,
    ],
)
def test_fstring_uses_running_python_grammar(source: str) -> None:
    text = source.replace("HOST", HOST)
    result = scan_text(text, policy=Policy(rules={"private-hostname": "redact"}))
    hosts = [f.match for f in result.findings if f.rule_id == "private-hostname"]
    assert hosts == (["cfg." + HOST] if sys.version_info < (3, 12) else [])


def test_many_fstring_hosts_keep_their_literal_offsets() -> None:
    field = "{lookup(cfg." + HOST + ")}"
    segment = field + "/" + HOST + "/"
    prefix = 'f"https://'
    text = prefix + segment * 256 + '"'
    result = scan_text(text, policy=Policy(rules={"private-hostname": "redact"}))
    start = len(prefix) + len(field) + 1
    assert [f.start for f in result.findings if f.rule_id == "private-hostname"] == list(
        range(start, len(text), len(segment))
    )
