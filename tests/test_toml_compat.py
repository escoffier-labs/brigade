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
