"""Release doctor checks for declared version vs git tag alignment."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from .. import toml_compat
from .paths import OK, WARN, _git

VERSION_TAG_CHECK_NAME = "declared_version_without_git_tag"
VERSION_TAG_CHECK_DESCRIPTION = (
    "Warn when pyproject.toml version has no matching vX.Y.Z git tag (unreleased version bump)."
)


def declared_project_version(target: Path) -> str | None:
    pyproject = target / "pyproject.toml"
    if not pyproject.is_file():
        return None
    try:
        data = toml_compat.loads(pyproject.read_text())
    except Exception:
        return None
    project = data.get("project")
    if not isinstance(project, dict):
        return None
    version = project.get("version")
    return version if isinstance(version, str) and version.strip() else None


def git_tag_exists(target: Path, tag: str) -> bool:
    return _git(target, "rev-parse", "-q", "--verify", f"refs/tags/{tag}^{{commit}}").returncode == 0


def version_tag_check(
    target: Path,
    *,
    tag_exists: Callable[[Path, str], bool] | None = None,
) -> dict[str, Any]:
    lookup = tag_exists or git_tag_exists
    version = declared_project_version(target)
    if version is None:
        return {
            "name": VERSION_TAG_CHECK_NAME,
            "status": WARN,
            "detail": "could not read project.version from pyproject.toml",
            "description": VERSION_TAG_CHECK_DESCRIPTION,
        }
    tag = f"v{version}"
    if lookup(target, tag):
        return {
            "name": VERSION_TAG_CHECK_NAME,
            "status": OK,
            "detail": f"{tag} exists",
            "description": VERSION_TAG_CHECK_DESCRIPTION,
        }
    return {
        "name": VERSION_TAG_CHECK_NAME,
        "status": WARN,
        "detail": f"pyproject.toml declares {version} but {tag} is missing",
        "description": VERSION_TAG_CHECK_DESCRIPTION,
    }
