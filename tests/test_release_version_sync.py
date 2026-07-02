import json
from pathlib import Path

import pytest

from brigade import cli, release_version_sync as vs


def _manifest(target: Path, body: str) -> None:
    (target / ".brigade").mkdir(parents=True, exist_ok=True)
    (target / ".brigade" / "version-sync.toml").write_text(body)


BASIC_MANIFEST = """
[source]
file = "pyproject.toml"
key = "project.version"

[[location]]
path = "src/pkg/__init__.py"
pattern = '__version__ = "([^"]+)"'
"""


def test_load_manifest_ok(tmp_path):
    _manifest(tmp_path, BASIC_MANIFEST)
    manifest = vs.load_manifest(tmp_path)
    assert manifest.source.file == "pyproject.toml"
    assert manifest.source.key == "project.version"
    assert manifest.source.regex is None
    assert len(manifest.locations) == 1
    assert manifest.locations[0].path == "src/pkg/__init__.py"
    assert manifest.locations[0].required is True


def test_load_manifest_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        vs.load_manifest(tmp_path)


@pytest.mark.parametrize(
    "body, needle",
    [
        (
            "[source]\nfile='p'\nkey='a'\nregex='(x)'\n[[location]]\npath='f'\npattern='(x)'\n",
            "exactly one of `key` or `regex`",
        ),
        ("[source]\nfile='p'\nkey='a'\n", "at least one [[location]]"),
        (
            "[source]\nfile='p'\nkey='a'\n[[location]]\npath='f'\nglob='g'\npattern='(x)'\n",
            "exactly one of `path` or `glob`",
        ),
        (
            "[source]\nfile='p'\nkey='a'\n[[location]]\npath='f'\npattern='(x)(y)'\n",
            "exactly one capture group",
        ),
        (
            "[source]\nfile='p'\nkey='a'\n[[location]]\npath='f'\npattern='(x'\n",
            "not a valid regex",
        ),
        (
            "[source]\nfile='p'\nkey='a'\n[[location]]\npath='f'\npattern='(x)'\nguard='z'\n",
            "guard is only valid with `glob`",
        ),
    ],
)
def test_load_manifest_invalid(tmp_path, body, needle):
    _manifest(tmp_path, body)
    with pytest.raises(ValueError) as exc:
        vs.load_manifest(tmp_path)
    assert needle in str(exc.value)
