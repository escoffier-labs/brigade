from __future__ import annotations

import tomllib

import pytest

from brigade.toml_format import format_toml_value


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
def test_formatted_strings_round_trip(value):
    source = f"value = {format_toml_value(value)}"
    assert "\n" not in source
    assert tomllib.loads(source) == {"value": value}
