"""Reproducible tests for the Keep-a-Changelog section-aware merge driver.

Covers the pure merge function and a real ``git merge`` with the driver
configured — including the unsafe whole-file ``merge=union`` footgun that #828
claimed did not exist (released-section edits must conflict).
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / "scripts" / "git_merge_changelog_unreleased.py"
CONFIGURE = ROOT / "scripts" / "configure_changelog_merge_driver.sh"


def _load_driver():
    spec = importlib.util.spec_from_file_location("git_merge_changelog_unreleased", DRIVER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


driver = _load_driver()

BASE_CHANGELOG = """\
# Changelog

## [Unreleased]

### Added

## [0.26.1] - 2026-08-09

### Changed
- released entry
"""


def _git(
    cwd: Path, *args: str, check: bool = True, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    full_env = os.environ.copy()
    full_env.update(
        {
            "GIT_AUTHOR_NAME": "Test User",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Test User",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
        }
    )
    if env:
        full_env.update(env)
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=check,
        capture_output=True,
        text=True,
        env=full_env,
    )


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.name", "Test User")
    _git(path, "config", "user.email", "test@example.invalid")


def test_parallel_unreleased_bullets_union_cleanly() -> None:
    ours = BASE_CHANGELOG.replace(
        "### Added\n",
        "### Added\n- PR A: add feature foo (#100)\n",
    )
    theirs = BASE_CHANGELOG.replace(
        "### Added\n",
        "### Added\n- PR B: fix bar handling (#101)\n",
    )
    result, clean = driver.merge_changelog(BASE_CHANGELOG, ours, theirs)
    assert clean is True
    assert "- PR A: add feature foo (#100)" in result
    assert "- PR B: fix bar handling (#101)" in result
    assert result.count("### Added") == 1
    assert "## [0.26.1]" in result
    assert result.index("### Added") < result.index("## [0.26.1]")


def test_parallel_new_subsections_fold_under_one_header() -> None:
    ours = BASE_CHANGELOG.replace(
        "### Added\n\n",
        "### Added\n\n### Fixed\n- PR A: fix z (#102)\n\n",
    )
    theirs = BASE_CHANGELOG.replace(
        "### Added\n\n",
        "### Added\n\n### Fixed\n- PR B: fix y (#103)\n\n",
    )
    result, clean = driver.merge_changelog(BASE_CHANGELOG, ours, theirs)
    assert clean is True
    assert result.count("### Fixed") == 1
    assert "- PR A: fix z (#102)" in result
    assert "- PR B: fix y (#103)" in result


def test_released_section_edits_conflict() -> None:
    ours = BASE_CHANGELOG.replace("- released entry", "- released entry from A")
    theirs = BASE_CHANGELOG.replace("- released entry", "- released entry from B")
    result, clean = driver.merge_changelog(BASE_CHANGELOG, ours, theirs)
    assert clean is False
    assert "<<<<<<< ours" in result
    assert "- released entry from A" in result
    assert "- released entry from B" in result
    assert ">>>>>>> theirs" in result


def test_released_conflict_still_unions_unreleased() -> None:
    ours = (
        "# Changelog\n\n## [Unreleased]\n\n### Added\n- A bullet\n\n"
        "## [0.26.1] - 2026-08-09\n\n### Changed\n- released entry from A\n"
    )
    theirs = (
        "# Changelog\n\n## [Unreleased]\n\n### Added\n- B bullet\n\n"
        "## [0.26.1] - 2026-08-09\n\n### Changed\n- released entry from B\n"
    )
    result, clean = driver.merge_changelog(BASE_CHANGELOG, ours, theirs)
    assert clean is False
    assert "- A bullet" in result
    assert "- B bullet" in result
    assert result.count("### Added") == 1
    assert "<<<<<<< ours" in result
    assert "- released entry from A" in result
    assert "- released entry from B" in result


def test_whole_file_union_silently_merges_released_sections(tmp_path: Path) -> None:
    """Document the #828 footgun: built-in merge=union keeps both released lines."""
    repo = tmp_path / "union-footgun"
    _init_repo(repo)
    (repo / ".gitattributes").write_text("CHANGELOG.md merge=union\n", encoding="utf-8")
    (repo / "CHANGELOG.md").write_text(BASE_CHANGELOG, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")

    _git(repo, "checkout", "-qb", "pr-a")
    (repo / "CHANGELOG.md").write_text(
        BASE_CHANGELOG.replace("- released entry", "- released entry from A").replace(
            "### Added\n", "### Added\n- PR A\n"
        ),
        encoding="utf-8",
    )
    _git(repo, "add", "CHANGELOG.md")
    _git(repo, "commit", "-qm", "a")

    _git(repo, "checkout", "-qb", "pr-b", "main")
    (repo / "CHANGELOG.md").write_text(
        BASE_CHANGELOG.replace("- released entry", "- released entry from B").replace(
            "### Added\n", "### Added\n- PR B\n"
        ),
        encoding="utf-8",
    )
    _git(repo, "add", "CHANGELOG.md")
    _git(repo, "commit", "-qm", "b")

    _git(repo, "checkout", "-qb", "integrate", "main")
    _git(repo, "merge", "pr-a", "-qm", "merge a")
    merged = _git(repo, "merge", "pr-b", "-m", "merge b", check=False)
    assert merged.returncode == 0, merged.stderr
    text = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    # Silent dual keep — the unsafe behavior this corrective change rejects.
    assert "- released entry from A" in text
    assert "- released entry from B" in text


def test_git_merge_with_driver_unions_unreleased_and_conflicts_released(tmp_path: Path) -> None:
    repo = tmp_path / "driver-repo"
    _init_repo(repo)
    (repo / ".gitattributes").write_text("CHANGELOG.md merge=changelog-unreleased\n", encoding="utf-8")
    # Copy driver into the scratch repo so the configured relative path resolves.
    scripts = repo / "scripts"
    scripts.mkdir()
    scripts.joinpath("git_merge_changelog_unreleased.py").write_text(
        DRIVER.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    _git(
        repo,
        "config",
        "merge.changelog-unreleased.driver",
        "python3 scripts/git_merge_changelog_unreleased.py %O %A %B",
    )
    (repo / "CHANGELOG.md").write_text(BASE_CHANGELOG, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")

    _git(repo, "checkout", "-qb", "pr-a")
    (repo / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [Unreleased]\n\n### Added\n- PR A: add feature foo (#100)\n\n"
        "## [0.26.1] - 2026-08-09\n\n### Changed\n- released entry from A\n",
        encoding="utf-8",
    )
    _git(repo, "add", "CHANGELOG.md")
    _git(repo, "commit", "-qm", "a")

    _git(repo, "checkout", "-qb", "pr-b", "main")
    (repo / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [Unreleased]\n\n### Added\n- PR B: fix bar (#101)\n\n"
        "## [0.26.1] - 2026-08-09\n\n### Changed\n- released entry from B\n",
        encoding="utf-8",
    )
    _git(repo, "add", "CHANGELOG.md")
    _git(repo, "commit", "-qm", "b")

    _git(repo, "checkout", "-qb", "integrate", "main")
    _git(repo, "merge", "pr-a", "-qm", "merge a")
    conflicted = _git(repo, "merge", "pr-b", "-m", "merge b", check=False)
    assert conflicted.returncode != 0
    text = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "- PR A: add feature foo (#100)" in text
    assert "- PR B: fix bar (#101)" in text
    assert "<<<<<<< ours" in text
    assert "- released entry from A" in text
    assert "- released entry from B" in text


def test_git_merge_with_driver_clean_when_only_unreleased_diverges(tmp_path: Path) -> None:
    repo = tmp_path / "driver-clean"
    _init_repo(repo)
    (repo / ".gitattributes").write_text("CHANGELOG.md merge=changelog-unreleased\n", encoding="utf-8")
    scripts = repo / "scripts"
    scripts.mkdir()
    scripts.joinpath("git_merge_changelog_unreleased.py").write_text(
        DRIVER.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    _git(
        repo,
        "config",
        "merge.changelog-unreleased.driver",
        "python3 scripts/git_merge_changelog_unreleased.py %O %A %B",
    )
    (repo / "CHANGELOG.md").write_text(BASE_CHANGELOG, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")

    _git(repo, "checkout", "-qb", "pr-a")
    (repo / "CHANGELOG.md").write_text(
        BASE_CHANGELOG.replace("### Added\n", "### Added\n- PR A: add feature foo (#100)\n"),
        encoding="utf-8",
    )
    _git(repo, "add", "CHANGELOG.md")
    _git(repo, "commit", "-qm", "a")

    _git(repo, "checkout", "-qb", "pr-b", "main")
    (repo / "CHANGELOG.md").write_text(
        BASE_CHANGELOG.replace("### Added\n", "### Added\n- PR B: fix bar (#101)\n"),
        encoding="utf-8",
    )
    _git(repo, "add", "CHANGELOG.md")
    _git(repo, "commit", "-qm", "b")

    _git(repo, "checkout", "-qb", "integrate", "main")
    _git(repo, "merge", "pr-a", "-qm", "merge a")
    merged = _git(repo, "merge", "pr-b", "-m", "merge b", check=False)
    assert merged.returncode == 0, merged.stderr + merged.stdout
    text = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "- PR A: add feature foo (#100)" in text
    assert "- PR B: fix bar (#101)" in text
    assert "<<<<<<<" not in text
    assert text.count("- released entry") == 1


def test_repo_gitattributes_names_custom_driver_not_union() -> None:
    text = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "CHANGELOG.md merge=changelog-unreleased" in text
    assert "merge=union" not in text


def test_configure_script_registers_driver(tmp_path: Path) -> None:
    repo = tmp_path / "configure"
    _init_repo(repo)
    # Script expects to run from a tree that contains scripts/; mirror layout.
    scripts = repo / "scripts"
    scripts.mkdir()
    scripts.joinpath("configure_changelog_merge_driver.sh").write_text(
        CONFIGURE.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    scripts.joinpath("git_merge_changelog_unreleased.py").write_bytes(DRIVER.read_bytes())
    result = subprocess.run(
        ["bash", "scripts/configure_changelog_merge_driver.sh"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "Configured merge.changelog-unreleased" in result.stdout
    driver_cfg = _git(repo, "config", "--get", "merge.changelog-unreleased.driver")
    assert "git_merge_changelog_unreleased.py" in driver_cfg.stdout


def test_cli_writes_ours_path(tmp_path: Path) -> None:
    base = tmp_path / "base.md"
    ours = tmp_path / "ours.md"
    theirs = tmp_path / "theirs.md"
    base.write_text(BASE_CHANGELOG, encoding="utf-8")
    ours.write_text(
        BASE_CHANGELOG.replace("### Added\n", "### Added\n- A\n"),
        encoding="utf-8",
    )
    theirs.write_text(
        BASE_CHANGELOG.replace("### Added\n", "### Added\n- B\n"),
        encoding="utf-8",
    )
    code = driver.main([str(base), str(ours), str(theirs)])
    assert code == 0
    text = ours.read_text(encoding="utf-8")
    assert "- A" in text and "- B" in text


@pytest.mark.parametrize(
    "body",
    [
        "# Changelog\n\n## [0.1.0]\n\n- only released\n",
        "",
    ],
)
def test_missing_unreleased_falls_back_to_strict_region(body: str) -> None:
    ours = body + ("\n# ours\n" if body else "# ours\n")
    theirs = body + ("\n# theirs\n" if body else "# theirs\n")
    result, clean = driver.merge_changelog(body, ours, theirs)
    assert clean is False
    assert "<<<<<<< ours" in result
