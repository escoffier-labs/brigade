#!/usr/bin/env python3
"""Git merge driver for Keep-a-Changelog files.

Union-merge only the ``## [Unreleased]`` section (subsection-aware). Everything
else — preamble and released/versioned sections — uses a strict region merge that
conflicts when both sides diverge from the base. That avoids the silent
corruption risk of whole-file ``merge=union``.

Git invokes this as::

    python3 scripts/git_merge_changelog_unreleased.py %O %A %B

``%A`` is overwritten with the merge result. Exit 0 on a clean merge, 1 when
conflict markers were written (or on I/O / usage errors).

Register once per clone (see CONTRIBUTING.md)::

    git config merge.changelog-unreleased.driver \\
      'python3 scripts/git_merge_changelog_unreleased.py %O %A %B'
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

_UNRELEASED_RE = re.compile(r"^## \[Unreleased\]\s*$")
_VERSION_HEADING_RE = re.compile(r"^## \[.+\]")
_SUBSECTION_RE = re.compile(r"^### (.+?)\s*$")
_CONFLICT_START = "<<<<<<< ours"
_CONFLICT_MID = "======="
_CONFLICT_END = ">>>>>>> theirs"


@dataclass
class ChangelogParts:
    """A Keep-a-Changelog file split around ``## [Unreleased]``."""

    preamble: str
    unreleased: str | None
    remainder: str
    has_unreleased: bool


@dataclass
class UnreleasedSection:
    """Parsed ``## [Unreleased]`` body with optional ``###`` subsections."""

    subsections: dict[str, list[str]] = field(default_factory=dict)
    section_order: list[str] = field(default_factory=list)
    loose_lines: list[str] = field(default_factory=list)


def split_changelog(text: str) -> ChangelogParts:
    """Split ``text`` into preamble, unreleased block, and remainder."""
    lines = text.splitlines(keepends=True)
    unreleased_idx: int | None = None
    for i, line in enumerate(lines):
        if _UNRELEASED_RE.match(line.rstrip("\n")):
            unreleased_idx = i
            break
    if unreleased_idx is None:
        return ChangelogParts(preamble=text, unreleased=None, remainder="", has_unreleased=False)

    remainder_idx: int | None = None
    for i in range(unreleased_idx + 1, len(lines)):
        stripped = lines[i].rstrip("\n")
        if _VERSION_HEADING_RE.match(stripped) and not _UNRELEASED_RE.match(stripped):
            remainder_idx = i
            break

    preamble = "".join(lines[:unreleased_idx])
    if remainder_idx is None:
        unreleased = "".join(lines[unreleased_idx:])
        remainder = ""
    else:
        unreleased = "".join(lines[unreleased_idx:remainder_idx])
        remainder = "".join(lines[remainder_idx:])
    return ChangelogParts(
        preamble=preamble,
        unreleased=unreleased,
        remainder=remainder,
        has_unreleased=True,
    )


def parse_unreleased(block: str | None) -> UnreleasedSection:
    """Parse an Unreleased block into subsections and loose lines."""
    parsed = UnreleasedSection()
    if not block:
        return parsed

    lines = block.splitlines()
    # Drop the ## [Unreleased] heading; callers re-emit it.
    if lines and _UNRELEASED_RE.match(lines[0]):
        lines = lines[1:]

    current: str | None = None
    for raw in lines:
        subsection = _SUBSECTION_RE.match(raw)
        if subsection:
            current = subsection.group(1)
            if current not in parsed.subsections:
                parsed.subsections[current] = []
                parsed.section_order.append(current)
            continue
        if current is None:
            if raw.strip() == "":
                continue
            parsed.loose_lines.append(raw)
            continue
        if raw.strip() == "":
            continue
        parsed.subsections[current].append(raw)
    return parsed


def _union_preserve_order(primary: list[str], secondary: list[str]) -> list[str]:
    """Return ``primary`` lines then any ``secondary`` lines not already present."""
    seen = set(primary)
    out = list(primary)
    for line in secondary:
        if line not in seen:
            out.append(line)
            seen.add(line)
    return out


def merge_unreleased(base: str | None, ours: str | None, theirs: str | None) -> str:
    """Subsection-aware union merge of the Unreleased section."""
    base_p = parse_unreleased(base)
    ours_p = parse_unreleased(ours)
    theirs_p = parse_unreleased(theirs)

    # Section order: base, then ours-only additions, then theirs-only additions.
    order: list[str] = []
    for name in base_p.section_order + ours_p.section_order + theirs_p.section_order:
        if name not in order:
            order.append(name)

    # Tip union is the source of truth for append-only Unreleased workflows:
    # a base bullet deleted on both tips stays gone; deleted on only one tip is
    # kept when the other tip still has it (because that tip's list still carries it).
    loose = _union_preserve_order(ours_p.loose_lines, theirs_p.loose_lines)

    chunks: list[str] = ["## [Unreleased]", ""]
    if loose:
        chunks.extend(loose)
        chunks.append("")

    for name in order:
        if name not in ours_p.subsections and name not in theirs_p.subsections:
            continue
        ours_items = ours_p.subsections.get(name, [])
        theirs_items = theirs_p.subsections.get(name, [])
        items = _union_preserve_order(ours_items, theirs_items)
        chunks.append(f"### {name}")
        chunks.extend(items)
        chunks.append("")

    text = "\n".join(chunks)
    if not text.endswith("\n"):
        text += "\n"
    return text


def _ensure_trailing_newline(text: str) -> str:
    if text == "" or text.endswith("\n"):
        return text
    return text + "\n"


def merge_region(base: str, ours: str, theirs: str) -> tuple[str, bool]:
    """Strict 3-way region merge. Returns ``(text, clean)``."""
    if ours == theirs:
        return ours, True
    if ours == base:
        return theirs, True
    if theirs == base:
        return ours, True
    conflicted = (
        f"{_CONFLICT_START}\n"
        f"{_ensure_trailing_newline(ours)}"
        f"{_CONFLICT_MID}\n"
        f"{_ensure_trailing_newline(theirs)}"
        f"{_CONFLICT_END}\n"
    )
    return conflicted, False


def merge_changelog(base_text: str, ours_text: str, theirs_text: str) -> tuple[str, bool]:
    """Merge three changelog versions. Returns ``(result, clean)``."""
    base = split_changelog(base_text)
    ours = split_changelog(ours_text)
    theirs = split_changelog(theirs_text)

    preamble, preamble_clean = merge_region(base.preamble, ours.preamble, theirs.preamble)
    remainder, remainder_clean = merge_region(base.remainder, ours.remainder, theirs.remainder)

    # If any side lacks [Unreleased], fall back to treating the whole file as one
    # strict region so we never invent structure or silently union released text.
    if not (base.has_unreleased and ours.has_unreleased and theirs.has_unreleased):
        whole, clean = merge_region(base_text, ours_text, theirs_text)
        return whole, clean

    unreleased = merge_unreleased(base.unreleased, ours.unreleased, theirs.unreleased)
    result = preamble + unreleased + remainder
    return result, preamble_clean and remainder_clean


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base", type=Path, help="%%O ancestor file")
    parser.add_argument("ours", type=Path, help="%%A current branch file (also output)")
    parser.add_argument("theirs", type=Path, help="%%B other branch file")
    args = parser.parse_args(argv)

    try:
        result, clean = merge_changelog(_read(args.base), _read(args.ours), _read(args.theirs))
        _write(args.ours, result)
    except OSError as exc:
        print(f"changelog merge driver failed: {exc}", file=sys.stderr)
        return 1
    return 0 if clean else 1


if __name__ == "__main__":
    raise SystemExit(main())
