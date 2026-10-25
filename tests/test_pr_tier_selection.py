"""Strict PR tiers at the selector's temporary-tree boundary."""

import json
from pathlib import Path

import pytest

from scripts import pr_tier_selection as selector


ROOT = Path(__file__).resolve().parents[1]


def write(repo, name, text=""):
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path):
    write(tmp_path, "tests/test_cli_help.py")  # approved A
    write(tmp_path, "tests/test_bug_report.py", "import brigade.example")  # approved C
    write(tmp_path, "tests/test_unknown.py", "from brigade import example")  # new B
    write(tmp_path, "tests/test_sibling.py", "import brigade.other")
    return tmp_path


def test_source_changes_select_smoke_and_imported_unknown_b_but_never_imported_c(repo):
    result = selector.select_tests(repo, ["src/brigade/example.py"])
    assert result["selected"] == ["tests/test_cli_help.py", "tests/test_unknown.py"]
    assert result["reasons"]["tests/test_cli_help.py"] == ["tier-a"]
    assert result["reasons"]["tests/test_unknown.py"] == ["import"]
    assert result["fallback"] is None
    assert selector.select_tests(repo, ["docs/note.md"])["selected"] == ["tests/test_cli_help.py"]


def test_changed_c_survives_and_deleted_tests_are_absent(repo):
    result = selector.select_tests(repo, ["tests/test_bug_report.py", "tests/test_deleted.py"])
    assert result["selected"] == ["tests/test_bug_report.py", "tests/test_cli_help.py"]
    assert result["reasons"]["tests/test_bug_report.py"] == ["changed"]


@pytest.mark.parametrize(
    "changed",
    [
        "pyproject.toml",
        "tests/nested/conftest.py",
        "tests/support.py",
        "src/brigade/assets/example.json",
        "src/brigade/__init__.py",
        ".github/workflows/ci.yml",
        "scripts/windows_affected_tests.py",
        "scripts/pr_test_tiers.json",
        "scripts/pr_tier_selection.py",
        "scripts/pr_tier_shadow.py",
    ],
)
def test_shared_fallback_keeps_all_b_and_changed_c_only(repo, changed):
    write(repo, "tests/test_release_cmd.py", "import brigade.example")  # another C
    result = selector.select_tests(repo, [changed, "tests/test_bug_report.py"])
    assert result["selected"] == [
        "tests/test_bug_report.py",
        "tests/test_cli_help.py",
        "tests/test_sibling.py",
        "tests/test_unknown.py",
    ]
    assert result["fallback"] == "shared"


@pytest.mark.parametrize("size,fallback", [(250, None), (251, "over-250")])
def test_threshold_is_strict_and_changed_c_counts_toward_initial_selection(tmp_path, size, fallback):
    write(tmp_path, "tests/test_bug_report.py")
    for index in range(size - 1):
        write(tmp_path, f"tests/test_import_{index:03}.py", "import brigade.example")
    write(tmp_path, "tests/test_extra.py")
    write(tmp_path, "tests/test_release_cmd.py", "import brigade.example")
    result = selector.select_tests(tmp_path, ["src/brigade/example.py", "tests/test_bug_report.py"])
    assert result["initial_count"] == size
    assert result["fallback"] == fallback
    assert len(result["selected"]) == size + (1 if fallback else 0)
    assert "tests/test_bug_report.py" in result["selected"]
    assert "tests/test_release_cmd.py" not in result["selected"]


def test_selection_is_deterministic_and_carries_replay_inputs(repo):
    changed = ["tests/test_unknown.py", "src/brigade/example.py"]
    result = selector.select_tests(repo, changed)
    assert result == selector.select_tests(repo, list(reversed(changed)) + changed)
    assert result["changed"] == sorted(changed)
    assert result["reasons"]["tests/test_unknown.py"] == ["changed", "import"]
    assert result["tiers"]["tests/test_unknown.py"] == "B"
    assert len(result["tier_manifest_sha256"]) == 64


def test_curated_smoke_contains_replay_miss_invariants():
    manifest = json.loads((ROOT / "scripts/pr_test_tiers.json").read_text())
    assert len(manifest["A"]) == 23
    assert len(manifest["C"]) == 39
    assert not set(manifest["A"]) & set(manifest["C"])
    for path in (
        "tests/test_module_size_ratchet.py",
        "tests/test_mypy_override_ratchet.py",
        "tests/test_posix_facade_guard.py",
        "tests/test_phase257_facades.py",
        "tests/test_verify_brigade_skill.py",
    ):
        assert path in manifest["A"]
