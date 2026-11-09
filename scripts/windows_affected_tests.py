#!/usr/bin/env python3
"""Select Windows tests from a base/head diff, using the checked-out head tree.

Directly changed tests and static import matches are never capped. Shared
infrastructure (including test helpers and source data) adds 24 tests ordered by SHA256 of their portable relative name,
then name. This stable sample avoids the alphabetic bias of the first 24 files
without turning infrastructure-only PRs into another full nightly run. Dynamic
imports and transitive dependencies remain the nightly's responsibility.
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Sequence

# The workflow runs this file directly, while tests import it as a module.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Re-exported: scripts/pr_tier_selection.py and the tests import these names from here.
from scripts.ci_selection_core import discover_tests, imports_changed, module_name  # noqa: F401

BROAD_SAMPLE_SIZE = 24
SHARED_PATHS = {
    "pyproject.toml",
    "scripts/windows_pytest.py",
    "scripts/windows_pytest_diagnostics.py",
    "scripts/windows_affected_tests.py",
    "scripts/ci_selection_core.py",
    "tests/windows_pytest_allowlist.json",
    "src/brigade/__init__.py",
    ".github/workflows/ci.yml",
}


def select_tests(repo: Path, changed: Sequence[str]) -> list[str]:
    files = discover_tests(repo)
    changed_set = set(changed)
    selected = {name for name in files if f"tests/{name}" in changed_set}
    modules = {module for path in changed if (module := module_name(path)) is not None}
    if modules:
        selected.update(
            name for name, path in files.items() if imports_changed(path.read_text(encoding="utf-8"), modules)
        )
    shared = changed_set & SHARED_PATHS or any(
        PurePosixPath(path).name == "conftest.py"
        or (path.startswith("tests/") and not (PurePosixPath(path).name.startswith("test_") and path.endswith(".py")))
        or (path.startswith("src/brigade/") and not path.endswith(".py"))
        for path in changed
    )
    if shared:
        sampled = sorted(files, key=lambda name: (hashlib.sha256(name.encode()).hexdigest(), name))
        selected.update(sampled[:BROAD_SAMPLE_SIZE])
    return sorted(selected)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    # --no-renames makes moved tests additions and removals, with no rename threshold ambiguity.
    diff = subprocess.run(
        ["git", "-C", str(args.repo), "diff", "--no-renames", "--name-only", "-z", args.base, args.head, "--"],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    selected = select_tests(args.repo, [path for path in diff.stdout.split("\0") if path])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("".join(name + "\n" for name in selected), encoding="utf-8")
    print(f"windows affected tests: files={len(selected)}")
    for name in selected:
        print(name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
