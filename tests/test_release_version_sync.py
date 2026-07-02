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


FULL_MANIFEST = """
[source]
file = "pyproject.toml"
key = "project.version"

[[location]]
path = "src/pkg/__init__.py"
pattern = '__version__ = "([^"]+)"'

[[location]]
glob = "src/pkg/templates/**/*.json"
guard = '"_v"'
pattern = '"_v"\\s*:\\s*"([^"]+)"'

[[location]]
path = "docs/quickstart.cast"
pattern = 'pkg (\\d+\\.\\d+\\.\\d+)'

[[location]]
path = "docs/quickstart.svg"
pattern = '>pkg</text><text[^>]*>(\\d+\\.\\d+\\.\\d+)</text>'
"""


def _repo(tmp_path, version="0.17.0", cast_version="0.17.0", svg_version="0.17.0"):
    (tmp_path / "pyproject.toml").write_text(f'[project]\nname = "pkg"\nversion = "{version}"\n')
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "__init__.py").write_text(f'__version__ = "{version}"\n')
    tpl = tmp_path / "src" / "pkg" / "templates" / "a"
    tpl.mkdir(parents=True)
    (tpl / "with.json").write_text(f'{{"_v": "{version}"}}\n')
    (tpl / "without.json").write_text('{"other": "x"}\n')
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "quickstart.cast").write_text(f'[2.0, "o", "pkg {cast_version}\\r\\n"]\n')
    (tmp_path / "docs" / "quickstart.svg").write_text(f'<text>pkg</text><text class="g">{svg_version}</text>')
    _manifest(tmp_path, FULL_MANIFEST)
    return tmp_path


def test_resolve_source_key(tmp_path):
    _repo(tmp_path)
    m = vs.load_manifest(tmp_path)
    assert vs.resolve_source(m, tmp_path) == "0.17.0"


def test_resolve_source_regex(tmp_path):
    (tmp_path / "VERSION").write_text("v = 1.2.3\n")
    _manifest(
        tmp_path,
        "[source]\nfile='VERSION'\nregex='v = (\\S+)'\n[[location]]\npath='VERSION'\npattern='v = (\\S+)'\n",
    )
    m = vs.load_manifest(tmp_path)
    assert vs.resolve_source(m, tmp_path) == "1.2.3"


def test_scan_all_ok(tmp_path):
    _repo(tmp_path)
    m = vs.load_manifest(tmp_path)
    results = vs.scan(m, tmp_path, "0.17.0")
    assert all(r.status == "ok" for r in results)
    # without.json (no guard) is skipped, not reported
    assert not any("without.json" in r.path for r in results)


def test_scan_detects_drift(tmp_path):
    _repo(tmp_path, cast_version="0.13.0")
    m = vs.load_manifest(tmp_path)
    results = vs.scan(m, tmp_path, "0.17.0")
    bad = [r for r in results if r.status == "mismatch"]
    assert [r.path for r in bad] == ["docs/quickstart.cast"]
    assert bad[0].found == ("0.13.0",)


def test_scan_missing_required_token(tmp_path):
    _repo(tmp_path)
    (tmp_path / "docs" / "quickstart.cast").write_text('[2.0, "o", "no version here\\r\\n"]\n')
    m = vs.load_manifest(tmp_path)
    results = vs.scan(m, tmp_path, "0.17.0")
    assert any(r.path == "docs/quickstart.cast" and r.status == "missing" for r in results)


def test_apply_fixes_only_drifted(tmp_path):
    _repo(tmp_path, cast_version="0.13.0")
    init_before = (tmp_path / "src" / "pkg" / "__init__.py").read_text()
    m = vs.load_manifest(tmp_path)
    changed = vs.apply(m, tmp_path, "0.17.0")
    assert changed == ["docs/quickstart.cast"]
    # untouched file is byte-identical
    assert (tmp_path / "src" / "pkg" / "__init__.py").read_text() == init_before
    # drifted file now matches and surrounding bytes preserved
    assert (tmp_path / "docs" / "quickstart.cast").read_text() == '[2.0, "o", "pkg 0.17.0\\r\\n"]\n'
    assert all(r.status == "ok" for r in vs.scan(m, tmp_path, "0.17.0"))


def test_apply_never_rewrites_source_file(tmp_path):
    (tmp_path / "VERSION").write_text("v = 0.13.0\n")
    _manifest(
        tmp_path,
        "[source]\nfile='VERSION'\nregex='v = (\\S+)'\n[[location]]\npath='VERSION'\npattern='v = (\\S+)'\n",
    )
    m = vs.load_manifest(tmp_path)
    assert vs.apply(m, tmp_path, "0.17.0") == []
    assert (tmp_path / "VERSION").read_text() == "v = 0.13.0\n"


def test_recording_svg_roundtrip(tmp_path):
    # The .svg glyph-adjacent pattern must round-trip through scan + apply.
    _repo(tmp_path, svg_version="0.9.9")
    m = vs.load_manifest(tmp_path)
    assert any(r.path == "docs/quickstart.svg" and r.status == "mismatch" for r in vs.scan(m, tmp_path, "0.17.0"))
    assert "docs/quickstart.svg" in vs.apply(m, tmp_path, "0.17.0")
    assert all(r.status == "ok" for r in vs.scan(m, tmp_path, "0.17.0"))


def test_version_sync_check_ok(tmp_path, capsys):
    _repo(tmp_path)
    assert vs.version_sync(target=tmp_path, write=False) == 0
    assert "checked=" in capsys.readouterr().out


def test_version_sync_check_drift(tmp_path, capsys):
    _repo(tmp_path, cast_version="0.13.0")
    assert vs.version_sync(target=tmp_path, write=False) == 1
    err = capsys.readouterr().err
    assert "quickstart.cast declares 0.13.0" in err


def test_version_sync_github_annotation(tmp_path, capsys, monkeypatch):
    _repo(tmp_path, cast_version="0.13.0")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    vs.version_sync(target=tmp_path, write=False)
    assert "::error::" in capsys.readouterr().err


def test_version_sync_write(tmp_path, capsys):
    _repo(tmp_path, cast_version="0.13.0")
    assert vs.version_sync(target=tmp_path, write=True) == 0
    assert vs.version_sync(target=tmp_path, write=False) == 0


def test_version_sync_missing_manifest(tmp_path, capsys):
    assert vs.version_sync(target=tmp_path, write=False) == 2
    assert "no version-sync manifest" in capsys.readouterr().err


def test_version_sync_json(tmp_path, capsys):
    _repo(tmp_path, cast_version="0.13.0")
    assert vs.version_sync(target=tmp_path, write=False, json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["version"] == "0.17.0"
    assert payload["ok"] is False
    assert any(r["status"] == "mismatch" for r in payload["results"])


def test_cli_check(tmp_path):
    _repo(tmp_path)
    assert cli.main(["release", "version-sync", "--target", str(tmp_path)]) == 0


def test_cli_write_then_check(tmp_path):
    _repo(tmp_path, cast_version="0.13.0")
    assert cli.main(["release", "version-sync", "--target", str(tmp_path), "--write"]) == 0
    assert cli.main(["release", "version-sync", "--target", str(tmp_path)]) == 0


def test_cli_check_write_mutually_exclusive(tmp_path):
    _repo(tmp_path)
    with pytest.raises(SystemExit) as exc:
        cli.main(["release", "version-sync", "--target", str(tmp_path), "--check", "--write"])
    assert exc.value.code == 2
