"""memory-care.toml is read as UTF-8 on every platform, with or without a byte-order mark."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from brigade import memory_cmd
from brigade import memory_hub_client as hubc

SRC = str(Path(__file__).resolve().parents[1] / "src")
BOM = b"\xef\xbb\xbf"
NON_ASCII = 'card_roots = ["memory/café"]\n'


def _write(target: Path, raw: bytes) -> None:
    path = memory_cmd.config_path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)


def test_non_ascii_config_loads_as_utf8(tmp_path):
    _write(tmp_path, NON_ASCII.encode("utf-8"))
    config = memory_cmd.load_config(tmp_path)
    assert config is not None and config.card_roots == ("memory/café",)


def test_a_leading_byte_order_mark_is_ignored(tmp_path):
    _write(tmp_path, BOM + NON_ASCII.encode("utf-8"))
    config = memory_cmd.load_config(tmp_path)
    assert config is not None and config.card_roots == ("memory/café",)


def test_bytes_that_are_not_utf8_keep_the_invalid_config_error(tmp_path):
    _write(tmp_path, b'card_roots = ["memory/caf\xe9"]\n')
    with pytest.raises(ValueError, match="invalid memory-care config"):
        memory_cmd.load_config(tmp_path)


def test_the_hub_reader_ignores_a_byte_order_mark(tmp_path):
    _write(tmp_path, BOM + b'[hub]\nmode = "read"\n')
    assert hubc.hub_mode(tmp_path) == "read"


def test_non_ascii_config_loads_under_a_non_utf8_locale(tmp_path):
    """The locale encoding must not decide how the file reads.

    With UTF-8 mode and locale coercion off under the C locale, a bare
    `read_text()` decodes as ASCII and rejects the accented letter.
    """
    _write(tmp_path, NON_ASCII.encode("utf-8"))
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONUTF8", "PYTHONIOENCODING", "PYTHONCOERCECLOCALE", "LC_ALL", "LANG", "LC_CTYPE"}
    }
    env.update(
        PYTHONPATH=SRC,
        PYTHONUTF8="0",
        PYTHONCOERCECLOCALE="0",
        PYTHONIOENCODING="ascii:backslashreplace",
        LC_ALL="C",
        LANG="C",
    )
    code = (
        "import locale, sys\n"
        "from pathlib import Path\n"
        "from brigade import memory_cmd\n"
        "assert locale.getpreferredencoding(False).lower() not in {'utf-8', 'utf8'}, locale.getpreferredencoding(False)\n"
        "config = memory_cmd.load_config(Path(sys.argv[1]))\n"
        "assert config is not None\n"
        "print(ascii(config.card_roots))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "('memory/caf\\xe9',)"
