#!/usr/bin/env python3
"""Stamp the Python package metadata for an ephemeral development wheel."""

from __future__ import annotations

import argparse
import re
from pathlib import Path


DEV_VERSION = re.compile(r"^\d+\.\d+\.\d+\.dev\d+$")
DATE_SUFFIX = re.compile(r"^\d{8}$")
STABLE_VERSION = re.compile(r"^\d+\.\d+\.\d+$")
PYPROJECT_VERSION = re.compile(r'(?m)^version = "(\d+\.\d+\.\d+)"$')


def read_stable_version(root: Path) -> str:
    versions = PYPROJECT_VERSION.findall((root / "pyproject.toml").read_text())
    if len(versions) != 1:
        raise ValueError("could not find exactly one strict numeric X.Y.Z version declaration in pyproject.toml")
    return versions[0]


def next_minor_version(stable_version: str) -> str:
    if not STABLE_VERSION.fullmatch(stable_version):
        raise ValueError(f"stable version must be strict numeric X.Y.Z, got {stable_version!r}")
    major, minor, _patch = (int(part) for part in stable_version.split("."))
    return f"{major}.{minor + 1}.0"


def derive_dev_version(stable_version: str, date_suffix: str) -> str:
    if not DATE_SUFFIX.fullmatch(date_suffix):
        raise ValueError(f"date suffix must match YYYYMMDD, got {date_suffix!r}")
    version = f"{next_minor_version(stable_version)}.dev{date_suffix}"
    if not DEV_VERSION.fullmatch(version):
        raise ValueError(f"derived development version is invalid: {version!r}")
    return version


def prepare_dev_wheel(root: Path, version: str) -> None:
    """Stamp only Python package metadata, leaving stable template pins intact."""
    if not DEV_VERSION.fullmatch(version):
        raise ValueError(f"development version must match X.Y.Z.devN, got {version!r}")

    replacements = (
        (root / "pyproject.toml", r'(?m)^version = "[^"]+"$', f'version = "{version}"'),
        (
            root / "src" / "brigade" / "__init__.py",
            r'(?m)^__version__ = "[^"]+"$',
            f'__version__ = "{version}"',
        ),
    )
    for path, pattern, replacement in replacements:
        original = path.read_text()
        stamped, count = re.subn(pattern, replacement, original, count=1)
        if count != 1:
            raise ValueError(f"could not find exactly one version declaration in {path}")
        path.write_text(stamped)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "version",
        nargs="?",
        help="PEP 440 development version (X.Y.Z.devN); omit when using --date-suffix",
    )
    parser.add_argument(
        "--date-suffix",
        help="UTC build date suffix (YYYYMMDD) used to derive the next minor X.Y.Z.devYYYYMMDD from pyproject.toml",
    )
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    if args.version and args.date_suffix:
        parser.error("pass either version or --date-suffix, not both")
    if not args.version and not args.date_suffix:
        parser.error("version or --date-suffix is required")
    try:
        version = args.version or derive_dev_version(read_stable_version(args.root), args.date_suffix)
        prepare_dev_wheel(args.root, version)
    except ValueError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
