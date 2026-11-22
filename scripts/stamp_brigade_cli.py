#!/usr/bin/env python3
"""Write a temporary copy of the project that builds as the ``brigade-cli`` distribution.

Through 0.33 ``brigade-cli`` is a full build of the same source under the old distribution
name, so ``pipx upgrade brigade-cli`` stays an ordinary in-place upgrade. The copy differs from
the checkout in exactly three ways: the distribution name, the console scripts, and the long
description. Version, dependencies, extras and package contents are the checkout's own.

The checkout is only read. Build the copy with ``python -m build --outdir dist <out>``.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README_SOURCE = "distribution/brigade-cli/pypi/README.md"
# What the root sdist holds besides src/ and the README that gets replaced: MANIFEST.in's files, plus
# the tests/test*.py that setuptools adds by default. tests/test_brigadestack_distribution.py compares
# the two sdists, so a file the root build includes and this copy drops fails a test.
COPIED_FILES = ("pyproject.toml", "MANIFEST.in", "LICENSE", "QUICKSTART.md", "THIRD_PARTY_NOTICES.md")
SCRIPTS = {
    "brigade": "brigade.cli:process_main",
    "brigade-cli": "brigade.cli:process_main",
    "brigade-hook": "brigade.claude_hooks.entry:main",
}
NAME_LINE = re.compile(r'(?m)^name = "brigadestack"$')
SCRIPTS_TABLE = re.compile(r"(?ms)^\[project\.scripts\]\n.*?(?=^\[)")


def stamp_pyproject(text: str) -> str:
    """Return the root pyproject text renamed to ``brigade-cli`` with its own console scripts."""
    stamped, count = NAME_LINE.subn('name = "brigade-cli"', text)
    if count != 1:
        raise ValueError('expected exactly one name = "brigadestack" line in pyproject.toml')
    table = "[project.scripts]\n" + "".join(f'{name} = "{target}"\n' for name, target in SCRIPTS.items()) + "\n"
    stamped, count = SCRIPTS_TABLE.subn(lambda _match: table, stamped)
    if count != 1:
        raise ValueError("expected exactly one [project.scripts] table in pyproject.toml")
    project = tomllib.loads(stamped)["project"]
    if project["name"] != "brigade-cli" or project["scripts"] != SCRIPTS:
        raise ValueError("stamped pyproject.toml does not carry the brigade-cli name and scripts")
    return stamped


def stamp(root: Path, out: Path) -> None:
    if out.exists() and any(out.iterdir()):
        raise ValueError(f"output directory {out} is not empty")
    out.mkdir(parents=True, exist_ok=True)
    for name in COPIED_FILES:
        shutil.copy2(root / name, out / name)
    stamped = stamp_pyproject((root / "pyproject.toml").read_text(encoding="utf-8"))
    (out / "pyproject.toml").write_text(stamped, encoding="utf-8")
    shutil.copy2(root / README_SOURCE, out / "README.md")
    (out / "tests").mkdir()
    for path in sorted((root / "tests").glob("test*.py")):
        shutil.copy2(path, out / "tests" / path.name)
    shutil.copytree(
        root / "src",
        out / "src",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info"),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out", type=Path, help="empty directory to write the brigade-cli project copy into")
    parser.add_argument("--root", type=Path, default=ROOT, help="checkout to copy (default: this repository)")
    args = parser.parse_args()
    try:
        stamp(args.root.resolve(), args.out)
    except ValueError as exc:
        print(f"stamp_brigade_cli: {exc}", file=sys.stderr)
        return 1
    print(f"wrote brigade-cli project copy to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
