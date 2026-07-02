#!/usr/bin/env python3
"""Verify (and optionally fix) that every place the version is stamped matches
pyproject.toml.

Three kinds of location carry the version, each as a literal string decoupled
from the ``[project].version`` source of truth:

  - ``src/brigade/__init__.py``      -> ``__version__ = "X"``
  - ``src/brigade/templates/**/*.json`` -> ``"_brigade_version": "X"``
  - recorded demo assets             -> a ``brigade X.Y.Z`` token baked into the
                                        rendered frames (``.cast`` and ``.svg``)

The recordings are the easy one to forget: a rendered ``.cast``/``.svg`` carries
the version as literal text, so a release bump silently strands it while the rest
of the tree moves on. ``--check`` (the default, run in CI) fails on any drift;
``--write`` rewrites every location to the pyproject version, so the daily bump
is one command instead of a hand-edit-and-hope across generated assets.

Fleet reuse: every location is just ``(path, regex-with-one-capture-group)``.
Point ``VERSION_FILE`` / ``INIT_FILE`` / ``TEMPLATES_GLOB`` / ``RECORDINGS`` at
another repo's equivalents and the rest is repo-agnostic.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import re
import sys
import tomllib

ROOT = pathlib.Path(__file__).resolve().parent.parent

# --- Per-repo configuration -------------------------------------------------
# Source of truth for the version.
VERSION_FILE = "pyproject.toml"  # read [project].version
# Simple, single-token locations checked/rewritten by regex. Each pattern must
# have exactly one capture group: the version token.
INIT_FILE = ("src/brigade/__init__.py", r'__version__ = "([^"]+)"')
TEMPLATES_GLOB = "src/brigade/templates/**/*.json"
TEMPLATE_PATTERN = r'"_brigade_version"\s*:\s*"([^"]+)"'
# Recorded demo assets with the version baked into the frames. The .cast is the
# source the .svg is rendered from; both are checked so editing one and
# forgetting the other is caught.
RECORDINGS = [
    ("docs/assets/quickstart.cast", r"brigade (\d+\.\d+\.\d+)"),
    ("docs/assets/quickstart.svg", r">brigade</text><text[^>]*>(\d+\.\d+\.\d+)</text>"),
]
# ---------------------------------------------------------------------------


def _expected() -> str:
    data = tomllib.loads((ROOT / VERSION_FILE).read_text())
    return data["project"]["version"]


def _locations() -> list[tuple[str, str]]:
    """Return every (path, pattern) location the version is stamped into."""
    locs: list[tuple[str, str]] = [INIT_FILE]
    for path in sorted(ROOT.glob(TEMPLATES_GLOB)):
        if '"_brigade_version"' in path.read_text():
            locs.append((path.relative_to(ROOT).as_posix(), TEMPLATE_PATTERN))
    locs.extend(RECORDINGS)
    return locs


def check(expected: str) -> int:
    github = bool(os.environ.get("GITHUB_ACTIONS"))
    problems: list[str] = []
    checked = 1  # pyproject.toml is the source
    for rel, pattern in _locations():
        text = (ROOT / rel).read_text()
        found = re.findall(pattern, text)
        checked += 1
        if not found:
            problems.append(f"{rel}: no version token found (pattern drifted or asset not rendered)")
            continue
        for value in found:
            if value != expected:
                problems.append(f"{rel} declares {value}, {VERSION_FILE} says {expected}")
    print(f"version={expected} checked={checked} locations")
    for msg in problems:
        prefix = "::error::" if github else "version sync: "
        print(f"{prefix}{msg}", file=sys.stderr)
    return 1 if problems else 0


def write(expected: str) -> int:
    changed: list[str] = []
    for rel, pattern in _locations():
        path = ROOT / rel
        text = path.read_text()

        def _sub(match: re.Match[str]) -> str:
            return match.group(0).replace(match.group(1), expected)

        new_text = re.sub(pattern, _sub, text)
        if new_text != text:
            path.write_text(new_text)
            changed.append(rel)
    if changed:
        print(f"version={expected} rewrote {len(changed)} file(s):")
        for rel in changed:
            print(f"  {rel}")
    else:
        print(f"version={expected} already in sync")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        action="store_true",
        help="verify every location matches the pyproject version (default)",
    )
    mode.add_argument(
        "--write",
        action="store_true",
        help="rewrite every location to the pyproject version instead of just checking",
    )
    args = parser.parse_args()
    expected = _expected()
    return write(expected) if args.write else check(expected)


if __name__ == "__main__":
    raise SystemExit(main())
