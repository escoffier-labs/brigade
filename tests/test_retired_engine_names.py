"""Retired names need explicit reasons and removable shims have a deadline."""

import json
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

from brigade.component_manifest import COMPONENT_FAMILIES

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "retired_engine_names.py"
RATCHET = runpy.run_path(str(SCRIPT))
LEGACY = next(iter(COMPONENT_FAMILIES))


def test_retired_engine_names_are_allowlisted() -> None:
    entries = json.loads((ROOT / RATCHET["ALLOWLIST"]).read_text(encoding="utf-8"))
    assert not (failures := RATCHET["violations"](ROOT, entries)), "\n".join(failures)


def _repo(tmp_path: Path) -> tuple[Path, list[dict]]:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    source = tmp_path / "example.txt"
    source.write_text(LEGACY + "\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    return source, [
        {
            "path": source.name,
            "kind": "shim",
            "reason": "Compatibility entry point",
            "literals": {LEGACY: 1},
            "remove_in": "0.33.0",
        }
    ]


@pytest.mark.parametrize("new_file", [False, True])
def test_new_retired_name_line_fails_ratchet(tmp_path: Path, new_file: bool) -> None:
    source, entries = _repo(tmp_path)
    source.write_text("", encoding="utf-8")
    assert RATCHET["violations"](tmp_path, entries) == []
    if new_file:
        source = tmp_path / "added.txt"
        source.write_text(LEGACY, encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    else:
        source.write_text((LEGACY + "\n") * 2, encoding="utf-8")
    assert any(source.name in failure for failure in RATCHET["violations"](tmp_path, entries))


def test_paths_and_spelling_variants_are_counted(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    source = tmp_path / (LEGACY + ".txt")
    source.write_text(LEGACY.upper() + " " + LEGACY.title(), encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    assert sum(RATCHET["matches"](tmp_path)[source.name].values()) == 3
    assert RATCHET["violations"](tmp_path, [])


@pytest.mark.parametrize("version", [(0, 33, 0), (0, 33, 1), (0, 34, 0), (1, 0, 0)])
def test_shims_expire_at_removal_version(tmp_path: Path, version: tuple[int, int, int]) -> None:
    _, entries = _repo(tmp_path)
    assert any("expired" in failure for failure in RATCHET["violations"](tmp_path, entries, release_version=version))


def test_frozen_and_historical_names_survive_deadline(tmp_path: Path) -> None:
    _, entries = _repo(tmp_path)
    for kind in ("frozen", "history", "fixture"):
        entries[0]["kind"] = kind
        entries[0].pop("remove_in", None)
        assert RATCHET["violations"](tmp_path, entries, release_version=(0, 33, 0)) == []


def test_allowance_cannot_admit_a_different_literal(tmp_path: Path) -> None:
    source, entries = _repo(tmp_path)
    source.write_text(LEGACY.upper(), encoding="utf-8")
    assert RATCHET["violations"](tmp_path, entries)


def test_removable_allowance_requires_deadline(tmp_path: Path) -> None:
    _, entries = _repo(tmp_path)
    entries[0].pop("remove_in")
    assert any("requires remove_in" in failure for failure in RATCHET["violations"](tmp_path, entries))


@pytest.mark.parametrize(
    ("source_version", "release_version", "status", "message"),
    [
        ("0.33.0", None, 0, ""),
        ("0.32.0", "0.32.0", 0, ""),
        ("0.33.0", "0.33.0", 1, "expired in 0.33.0"),
        ("0.33.0", "0.32.0", 2, "does not match package version"),
        ("0.33.0.dev0", "0.33.0.dev0", 2, "expected a stable version"),
    ],
)
def test_guard_cli_applies_expiry_at_stable_release_cut(
    tmp_path: Path, source_version: str, release_version: str | None, status: int, message: str
) -> None:
    _, entries = _repo(tmp_path)
    package = tmp_path / "src/brigade/__init__.py"
    package.parent.mkdir(parents=True)
    package.write_text(f'__version__ = "{source_version}"\n', encoding="utf-8")
    allowlist = tmp_path / RATCHET["ALLOWLIST"]
    allowlist.parent.mkdir(parents=True)
    allowlist.write_text(json.dumps(entries), encoding="utf-8")
    command = [sys.executable, str(SCRIPT), "--root", str(tmp_path)]
    if release_version is not None:
        command.extend(["--release-version", release_version])
    result = subprocess.run(command, capture_output=True, text=True)
    output = result.stdout + result.stderr
    assert result.returncode == status, output
    if message:
        assert message in output
    else:
        assert output == ""


def test_stable_publish_enforces_expiry_after_tag_identity() -> None:
    workflow = (ROOT / ".github/workflows/publish.yml").read_text(encoding="utf-8")
    validate = workflow[workflow.index("  validate-release:") : workflow.index("  build-rust-native:")]
    version_check = validate.index("      - name: Verify tag matches every declared version")
    expiry = validate.index(
        "      - name: Enforce shim expiry at the stable release cut\n"
        "        env:\n"
        "          TAG: ${{ github.ref_name }}\n"
        '        run: python3 scripts/retired_engine_names.py --release-version "${TAG#v}"\n'
    )
    assert version_check < expiry
