from __future__ import annotations

import pytest

from brigade import toml_compat


def test_fallback_loads_array_tables_and_basic_values(monkeypatch):
    monkeypatch.setattr(toml_compat, "_stdlib_tomllib", None)

    payload = toml_compat.loads(
        """
        [[repo]]
        id = "alpha"
        label = "service alpha"
        enabled = true
        tags = ["api", "daily"]
        timeout = 30

        [[repo.health_command]]
        label = "brief"
        argv = ["brigade", "work", "brief", "--json"]
        timeout = 20

        [[repo]]
        id = "beta"
        enabled = false

        [policy]
        max_timeout = 120
        effects = ["read", "write"]
        argument_template = { path = "{path}", "bad-key!" = "{raw}" }
        """
    )

    assert payload["repo"][0]["id"] == "alpha"
    assert payload["repo"][0]["enabled"] is True
    assert payload["repo"][0]["tags"] == ["api", "daily"]
    assert payload["repo"][0]["timeout"] == 30
    assert payload["repo"][0]["health_command"][0]["argv"] == ["brigade", "work", "brief", "--json"]
    assert payload["repo"][1]["id"] == "beta"
    assert payload["repo"][1]["enabled"] is False
    assert payload["policy"]["max_timeout"] == 120
    assert payload["policy"]["effects"] == ["read", "write"]
    assert payload["policy"]["argument_template"] == {"path": "{path}", "bad-key!": "{raw}"}


def test_fallback_preserves_hash_inside_quoted_values(monkeypatch):
    monkeypatch.setattr(toml_compat, "_stdlib_tomllib", None)

    payload = toml_compat.loads('label = "value # not comment" # comment\n')

    assert payload["label"] == "value # not comment"


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (r"value = 'C:\tools\runner.exe'", {"value": r"C:\tools\runner.exe"}),
        (r"value = '\\server\share\'", {"value": "\\\\server\\share\\"}),
        (r"value = '\n\t\u0041'", {"value": r"\n\t\u0041"}),
        (r"value = 'C:\temp\' # ignored", {"value": "C:\\temp\\"}),
        (r"value = 'C:\temp\#data' # ignored", {"value": r"C:\temp\#data"}),
        (r"value = ['C:\temp\', 'next']", {"value": ["C:\\temp\\", "next"]}),
        (r"value = {path = 'C:\temp\', label = 'next'}", {"value": {"path": "C:\\temp\\", "label": "next"}}),
        ("value = [\n  'C:\\temp\\', # ignored\n  'next',\n]", {"value": ["C:\\temp\\", "next"]}),
        ("value = ''", {"value": ""}),
        ("value = 'a\u0085b\u2028c\u2029d'", {"value": "a\u0085b\u2028c\u2029d"}),
        (r'value = "C:\\tools\\runner.exe"', {"value": r"C:\tools\runner.exe"}),
    ],
)
def test_fallback_preserves_literal_string_backslashes(monkeypatch, source, expected):
    if toml_compat._stdlib_tomllib is not None:
        assert toml_compat.loads(source) == expected
    monkeypatch.setattr(toml_compat, "_stdlib_tomllib", None)

    assert toml_compat.loads(source) == expected


@pytest.mark.parametrize("value", ["'", "'unfinished", "'one' 'two'", "'one'two'", "'bad\x00'", "'bad\x7f'"])
def test_fallback_rejects_malformed_literal_strings(monkeypatch, value):
    monkeypatch.setattr(toml_compat, "_stdlib_tomllib", None)

    with pytest.raises(toml_compat.TOMLDecodeError):
        toml_compat.loads(f"value = {value}")


@pytest.mark.parametrize(
    "value",
    [
        "",
        r"C:\tools\runner.exe",
        "C:\\temp\\",
        "both 'single' and \"double\" quotes",
        "apostrophe's C:\\temp\\",
        "line\nwith\ttabs\rand\x00controls\x7f",
        "café \U0001f355",
        "a\u0085b\u2028c\u2029d",
        "quoted's a\u0085b\u2028c\u2029d",
    ],
)
def test_formatted_strings_round_trip_with_both_parsers(monkeypatch, value):
    source = f"value = {toml_compat.format_toml_value(value)}"
    assert "\n" not in source
    if toml_compat._stdlib_tomllib is not None:
        assert toml_compat.loads(source) == {"value": value}
    monkeypatch.setattr(toml_compat, "_stdlib_tomllib", None)

    assert toml_compat.loads(source) == {"value": value}


def test_fallback_preserves_literal_strings_with_crlf_lines(monkeypatch):
    monkeypatch.setattr(toml_compat, "_stdlib_tomllib", None)

    assert toml_compat.loads("value = 'C:\\temp\\'\r\nnext = 'ok'\r\n") == {"value": "C:\\temp\\", "next": "ok"}


def test_fallback_loads_multiline_array_of_inline_tables(monkeypatch):
    monkeypatch.setattr(toml_compat, "_stdlib_tomllib", None)

    payload = toml_compat.loads(
        """
        [fleet.worklore.brigade]
        targets = [
          # Keep this comment between values.
          { name = "primary", path = "/tmp/primary" },
          { name = "secondary", path = "/tmp/secondary" }, # trailing comment
        ]
        """
    )

    assert payload["fleet"]["worklore"]["brigade"]["targets"] == [
        {"name": "primary", "path": "/tmp/primary"},
        {"name": "secondary", "path": "/tmp/secondary"},
    ]


def test_fallback_reports_incomplete_multiline_value_at_start_line(monkeypatch):
    monkeypatch.setattr(toml_compat, "_stdlib_tomllib", None)
    payload = '[fleet]\ntargets = [\n  { name = "primary" },\n'

    with pytest.raises(toml_compat.TOMLDecodeError, match="incomplete TOML value on line 2"):
        toml_compat.loads(payload)


def test_fallback_scans_each_multiline_fragment_once(monkeypatch):
    monkeypatch.setattr(toml_compat, "_stdlib_tomllib", None)
    original_scan = toml_compat._CollectionState.scan
    scanned_characters = 0

    def counting_scan(self, fragment, line_number, *, preceded_by_newline=False):
        nonlocal scanned_characters
        scanned_characters += len(fragment) + preceded_by_newline
        return original_scan(
            self,
            fragment,
            line_number,
            preceded_by_newline=preceded_by_newline,
        )

    monkeypatch.setattr(toml_compat._CollectionState, "scan", counting_scan)
    fragments = ["  0," for _ in range(10_000)]
    payload = "values = [\n" + "\n".join(fragments)

    with pytest.raises(toml_compat.TOMLDecodeError, match="incomplete TOML value on line 1"):
        toml_compat.loads(payload)

    stripped_characters = len("[") + sum(len(fragment.strip()) for fragment in fragments)
    assert scanned_characters == stripped_characters + len(fragments)


@pytest.mark.parametrize(("opening", "closer"), [("[", "}"), ("{", "]")])
def test_fallback_rejects_mismatched_multiline_closer_at_start_line(monkeypatch, opening, closer):
    monkeypatch.setattr(toml_compat, "_stdlib_tomllib", None)
    payload = f'[fleet]\ntargets = {opening}\n  name = "primary",\n{closer}\n'

    with pytest.raises(toml_compat.TOMLDecodeError, match="mismatched closing delimiter on line 2"):
        toml_compat.loads(payload)


def test_fallback_reports_invalid_values(monkeypatch):
    monkeypatch.setattr(toml_compat, "_stdlib_tomllib", None)

    with pytest.raises(toml_compat.TOMLDecodeError):
        toml_compat.loads("enabled = maybe\n")


def test_fallback_parses_quoted_dotted_table_key(monkeypatch):
    # On Python 3.10 the fallback reader handles codex configs. A quoted dotted
    # server name (mcp_servers."io.github.example") must stay one segment instead
    # of fragmenting into nested io/github/example tables.
    monkeypatch.setattr(toml_compat, "_stdlib_tomllib", None)

    payload = toml_compat.loads(
        '[mcp_servers."io.github.example"]\ncommand = "npx"\n\n[mcp_servers.github]\ncommand = "plain"\n'
    )

    assert payload["mcp_servers"]["io.github.example"]["command"] == "npx"
    assert payload["mcp_servers"]["github"]["command"] == "plain"


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (r'"srvx\u007fy"', "srvx\x7fy"),
        (r'"café"', "café"),
        (r'"a\"b"', 'a"b'),
        (r'"a\\b"', "a\\b"),
        (r"'a\u007fb'", "a\\u007fb"),
        (r"'a\\b'", "a\\\\b"),
        (r'"io.github.example"', "io.github.example"),
    ],
)
def test_fallback_decodes_quoted_table_key_escapes(monkeypatch, header, expected):
    source = f'[mcp_servers.{header}]\ncommand = "npx"\n\n[[arr.{header}]]\nname = "x"\n'
    stdlib_payload = toml_compat.loads(source)
    monkeypatch.setattr(toml_compat, "_stdlib_tomllib", None)

    payload = toml_compat.loads(source)

    assert payload == stdlib_payload
    assert payload["mcp_servers"][expected]["command"] == "npx"
    assert payload["arr"][expected][0]["name"] == "x"


def test_fallback_escaped_backslash_before_dot_splits_segments(monkeypatch):
    source = '[a."b\\\\".c]\nv = 1\n'
    stdlib_payload = toml_compat.loads(source)
    monkeypatch.setattr(toml_compat, "_stdlib_tomllib", None)

    assert toml_compat.loads(source) == stdlib_payload == {"a": {"b\\": {"c": {"v": 1}}}}
