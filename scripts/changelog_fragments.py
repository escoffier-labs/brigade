#!/usr/bin/env python3
"""Changelog fragments: validate, guard and assemble ``changelog.d/*.md``.

Pull requests do not edit ``CHANGELOG.md``. Each user-visible PR adds one
fragment file, ``changelog.d/<pr-number-or-slug>.md``, holding one or more
``- `` bullets. The release cut assembles the fragments into the
``## [Unreleased]`` section and deletes them in the same commit that rolls the
changelog under a version heading. That removes the shared Unreleased hunk that
GitHub reports as ``CONFLICTING`` for every sibling PR (#2231, #994).

Subcommands (stdlib only, no dependencies):

  check      every fragment parses, none is empty, names are valid and unique.
  guard      fail when a diff edits CHANGELOG.md outside a release commit.
  assemble   append fragments to ``## [Unreleased]`` and delete them.

A release commit is a diff that adds a new ``## [X.Y.Z] - YYYY-MM-DD`` heading
to CHANGELOG.md. It must also leave no fragment behind.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
FRAGMENT_DIR = "changelog.d"
CHANGELOG = "CHANGELOG.md"
# Files in the fragment directory that are not fragments.
IGNORED_NAMES = frozenset({"README.md", ".gitkeep"})
# Lowercase only, so names cannot collide on case-insensitive filesystems.
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*\.md$")
UNRELEASED_RE = re.compile(r"^## \[Unreleased\]\s*$")
RELEASE_HEADING_RE = re.compile(r"^## \[\d+\.\d+\.\d+[^\]]*\] - \d{4}-\d{2}-\d{2}\s*$")


class FragmentError(ValueError):
    """A fragment or changelog is malformed."""


def parse_fragment(text: str) -> list[str]:
    """Return the entries of one fragment, each as its (possibly wrapped) text.

    An entry starts with ``- `` at column zero. Indented lines continue the
    entry above them. Blank lines may separate entries. Nothing else is allowed,
    so a heading, prose paragraph or stray text fails instead of being dropped.
    """
    entries: list[list[str]] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.rstrip()
        if not line:
            continue
        if line.startswith("- "):
            if not line[2:].strip():
                raise FragmentError(f"line {number}: empty bullet")
            entries.append([line])
        elif line[0] in " \t" and entries and entries[-1]:
            entries[-1].append(line)
        else:
            raise FragmentError(f"line {number}: expected a '- ' bullet, got {line[:40]!r}")
    if not entries:
        raise FragmentError("no entries: add at least one '- ' bullet")
    return ["\n".join(entry) for entry in entries]


def natural_key(name: str) -> list[tuple[int, int, str]]:
    """Sort ``99.md`` before ``2231.md`` and ``a2`` before ``a10``."""
    return [(0, int(part), "") if part.isdigit() else (1, 0, part) for part in re.split(r"(\d+)", name) if part]


def validate_names(names: list[str]) -> list[str]:
    problems: list[str] = []
    seen: dict[str, str] = {}
    for name in names:
        if not NAME_RE.fullmatch(name):
            problems.append(f"{FRAGMENT_DIR}/{name}: name must match {NAME_RE.pattern}")
        folded = name.casefold()
        if folded in seen:
            problems.append(f"{FRAGMENT_DIR}/{name}: duplicates {FRAGMENT_DIR}/{seen[folded]}")
        else:
            seen[folded] = name
    return problems


def load_fragments(root: Path) -> tuple[list[tuple[str, list[str]]], list[str]]:
    """Return ``([(name, entries)], problems)`` in assembly order."""
    directory = root / FRAGMENT_DIR
    if not directory.is_dir():
        return [], []
    problems: list[str] = []
    names: list[str] = []
    for path in sorted(directory.iterdir(), key=lambda item: item.name):
        if path.name in IGNORED_NAMES:
            continue
        if not path.is_file() or path.is_symlink():
            problems.append(f"{FRAGMENT_DIR}/{path.name}: must be a regular file")
            continue
        names.append(path.name)
    problems.extend(validate_names(names))
    fragments: list[tuple[str, list[str]]] = []
    seen_entries: dict[str, str] = {}
    for name in sorted(names, key=natural_key):
        if not NAME_RE.fullmatch(name):
            continue
        try:
            entries = parse_fragment((directory / name).read_text(encoding="utf-8"))
        except (FragmentError, UnicodeDecodeError, OSError) as exc:
            problems.append(f"{FRAGMENT_DIR}/{name}: {exc}")
            continue
        for entry in entries:
            first = seen_entries.setdefault(entry, name)
            if first != name:
                problems.append(f"{FRAGMENT_DIR}/{name}: repeats an entry already in {FRAGMENT_DIR}/{first}")
        fragments.append((name, entries))
    return fragments, problems


def insert_into_unreleased(changelog: str, entries: list[str]) -> str:
    """Append ``entries`` at the end of the ``## [Unreleased]`` section."""
    newline = "\r\n" if "\r\n" in changelog else "\n"
    lines = changelog.splitlines()
    starts = [index for index, line in enumerate(lines) if UNRELEASED_RE.match(line)]
    if len(starts) != 1:
        raise FragmentError(f"{CHANGELOG} needs exactly one '## [Unreleased]' heading, found {len(starts)}")
    start = starts[0]
    end = next((index for index in range(start + 1, len(lines)) if lines[index].startswith("## ")), len(lines))
    body = lines[start + 1 : end]
    while body and not body[-1].strip():
        body.pop()
    added = [line for entry in entries for line in entry.split("\n")]
    if any(body):
        body = [*body, "", *added]
    else:
        body = ["", *added]
    tail = lines[end:]
    result = [*lines[: start + 1], *body, *([""] if tail else []), *tail]
    return newline.join(result) + newline


def assemble(root: Path, *, dry_run: bool = False) -> tuple[int, list[str]]:
    """Fold fragments into CHANGELOG.md and delete them. Returns (count, problems)."""
    fragments, problems = load_fragments(root)
    if problems:
        return 0, problems
    if not fragments:
        return 0, []
    changelog_path = root / CHANGELOG
    try:
        # Decode the bytes: read_text() would turn CRLF into LF before the line ending is detected.
        updated = insert_into_unreleased(
            changelog_path.read_bytes().decode("utf-8"), [entry for _name, entries in fragments for entry in entries]
        )
    except (FragmentError, OSError, UnicodeDecodeError) as exc:
        return 0, [f"{CHANGELOG}: {exc}"]
    if dry_run:
        return len(fragments), []
    # Write the changelog first and replace it atomically, so a crash never
    # leaves fragments deleted without their entries recorded.
    handle, temporary = tempfile.mkstemp(dir=root, prefix=".CHANGELOG.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as stream:
            stream.write(updated)
        os.chmod(temporary, changelog_path.stat().st_mode & 0o7777)
        os.replace(temporary, changelog_path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    for name, _entries in fragments:
        (root / FRAGMENT_DIR / name).unlink()
    return len(fragments), []


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "--no-replace-objects", *args],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    if result.returncode != 0:
        raise FragmentError(f"git {' '.join(args[:2])} failed: {result.stderr.strip() or result.returncode}")
    return result.stdout


def guard(root: Path, base: str, head: str = "HEAD") -> list[str]:
    """Reject a CHANGELOG.md edit that is not part of a release commit."""
    changed = _git(root, "diff", "--name-only", "--no-renames", base, head).splitlines()
    if CHANGELOG not in changed:
        return []
    diff = _git(root, "diff", "-U0", "--no-color", "--no-renames", base, head, "--", CHANGELOG)
    added_headings = [line[1:] for line in diff.splitlines() if line.startswith("+## [")]
    if not any(RELEASE_HEADING_RE.match(heading) for heading in added_headings):
        return [
            f"{CHANGELOG}: pull requests do not edit the changelog. Add a fragment as "
            f"{FRAGMENT_DIR}/<pr-number-or-slug>.md instead (see {FRAGMENT_DIR}/README.md). "
            "Only a release commit, which adds a '## [X.Y.Z] - YYYY-MM-DD' heading, may edit it."
        ]
    ls_tree = _git(root, "ls-tree", "-r", "--name-only", head, "--", FRAGMENT_DIR).splitlines()
    leftover = [
        path
        for path in ls_tree
        if path.startswith(f"{FRAGMENT_DIR}/") and path.removeprefix(f"{FRAGMENT_DIR}/") not in IGNORED_NAMES
    ]
    if leftover:
        return [
            f"{path}: a release commit must assemble and delete every fragment "
            "(run 'python3 scripts/changelog_fragments.py assemble' on the merged tree)"
            for path in leftover
        ]
    return []


def _report(problems: list[str]) -> None:
    annotate = os.environ.get("GITHUB_ACTIONS") == "true"
    for problem in problems:
        path = problem.split(":", 1)[0]
        if annotate:
            print(f"::error file={path}::{problem}")
        else:
            print(f"error: {problem}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=ROOT, help="repository root (default: this checkout)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="validate every fragment")
    guard_parser = sub.add_parser("guard", help="fail on a CHANGELOG.md edit outside a release commit")
    guard_parser.add_argument("--base", required=True, help="revision the change is compared against")
    guard_parser.add_argument("--head", default="HEAD")
    assemble_parser = sub.add_parser("assemble", help="fold fragments into [Unreleased] and delete them")
    assemble_parser.add_argument("--dry-run", action="store_true", help="validate and report without writing")
    args = parser.parse_args(argv)
    root = args.root.resolve()

    try:
        if args.command == "check":
            fragments, problems = load_fragments(root)
            if not problems:
                print(f"{len(fragments)} changelog fragment(s) valid")
        elif args.command == "guard":
            problems = guard(root, args.base, args.head)
            if not problems:
                print("changelog edit policy satisfied")
        else:
            count, problems = assemble(root, dry_run=args.dry_run)
            if not problems:
                verb = "would assemble" if args.dry_run else "assembled"
                print(f"{verb} {count} changelog fragment(s) into [Unreleased]")
    except FragmentError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    _report(problems)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
