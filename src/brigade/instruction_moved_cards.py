"""Count the moved cards that still exist for one instruction file (issue #2565).

A moved card carries `archive: never`, so the retire stage never removes one and a file
that keeps receiving appends would keep producing cards. `instruction_trim` bounds that
growth with `MAX_MOVED_CARDS_PER_FILE` and reports `moved_card_cap_reached` when a file
reaches it. The move stage (slice 2 PR A3) must pass `count` to the planner as
`moved_cards`, so the cap takes effect when that stage lands.

Counting reads the card folder, not the journal, so it stays exact after a card is
deleted or a move is undone: a card that no longer exists does not count, and a card
that does not carry this file's `source_file` does not count. Nothing here merges,
compacts or deletes a card, so every decision the journal still records keeps its
before-image and its card and can still be undone byte for byte.

The count fails closed. A missing folder is 0, because no card exists. Every other scan
failure (a permission error, a name that is not a directory, too many open files) returns
`MAX_MOVED_CARDS_PER_FILE`, so the planner reports the cap and creates nothing rather
than growing a folder it cannot read. A symlinked card entry is never counted. A card
folder that is itself a symlink or not a directory also fails closed, and a matching
entry that cannot be read in full, is oversized, is not a regular file or is not UTF-8
counts, because it holds the place.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

from .bounded_read import read_regular_file
from .instruction_inventory import INSPECTION_CEILING
from .instruction_overflow import CARDS_REL
from .instruction_sections import FILE_KEY
from .instruction_trim import MAX_MOVED_CARDS_PER_FILE
from .memory_care_instructions import encode_source, normalize_source

# The tag every card this feature creates carries, and the member that separates a moved
# card from an overflow card. Both must be exact members of the card's `tags` value.
MARKER_TAG = "moved-instructions"
MOVED_TAG = "moved"


def _frontmatter(text: str) -> dict[str, list[str]] | None:
    """The key to values of the one leading frontmatter block, or None when there is none.

    Only physical `key: value` lines are read. A line that continues another is ignored,
    because no field this count needs is written as a block.
    """
    lines = text.removeprefix("\ufeff").replace("\r\n", "\n").split("\n")
    if not lines or lines[0].strip() != "---":
        return None
    end = next((index for index in range(1, len(lines)) if lines[index].strip() == "---"), None)
    if end is None:
        return None
    fields: dict[str, list[str]] = {}
    for line in lines[1:end]:
        if not line or line[0].isspace() or ":" not in line:
            continue
        key, value = line.split(":", 1)
        fields.setdefault(key.strip().lower(), []).append(value.strip())
    return fields


def _tags(values: list[str]) -> set[str]:
    """The exact tag members of every `tags:` line, in list form (`[a, b]`) or string form (`a, b`)."""
    found: set[str] = set()
    for value in values:
        if value.startswith("[") and value.endswith("]"):
            value = value[1:-1]
        found.update(tag.strip().strip("'\"") for tag in value.split(","))
    return found


def _source(values: list[str]) -> str | None:
    """The `source_file` value, unquoted, or None when it is absent or blank."""
    for value in values:
        value = value.strip().strip("'\"")
        if value:
            return value
    return None


def is_moved_card(text: str, *, source_name: str) -> bool:
    """Whether `text` is a moved card this feature created for the file named by `source_name`.

    `source_file` is compared in its POSIX form without a leading `./` and without a case
    change, the form `card_bytes` writes after this change. A raw backslash path written
    by hand compares the same way.
    """
    fields = _frontmatter(text)
    if fields is None:
        return False
    tags = _tags(fields.get("tags", []))
    if MARKER_TAG not in tags or MOVED_TAG not in tags:
        return False
    stored = _source(fields.get("source_file", []))
    return stored is not None and normalize_source(stored) == encode_source(source_name)


def _occupies_place(path: Path, *, source_name: str) -> int:
    """1 when this matching entry holds a cap place, 0 when it is another card this file can ignore.

    A directory entry that is not a regular file, cannot be read in full, or is not UTF-8
    holds a place: it has the name a moved card would use and nothing proves it is not one.
    A readable card that names another source, or is not a moved card, does not hold one.
    """
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return 1
    except OSError:
        return 1
    read = read_regular_file(path, max_bytes=INSPECTION_CEILING, no_follow=True)
    if read.data is None:
        return 1
    try:
        text = read.data.decode("utf-8")
    except UnicodeDecodeError:
        return 1
    return 1 if is_moved_card(text, source_name=source_name) else 0


def count(target: Path, *, file_key: str, source_name: str) -> int:
    """Existing moved cards for one instruction file, or `MAX_MOVED_CARDS_PER_FILE` when the folder cannot be trusted.

    `source_name` is the value the move stage passes to `card_bytes`, so creation and this
    count always compare the same `source_file` form. The scan stops as soon as the count
    reaches the cap, so a folder with more matching names costs one pass to the cap, not a
    read of every match.
    """
    if FILE_KEY.fullmatch(file_key) is None:
        return 0  # no card can be named for it, so no card can carry its source
    folder = target / CARDS_REL
    try:
        info = os.lstat(folder)
    except FileNotFoundError:
        return 0
    except OSError:
        return MAX_MOVED_CARDS_PER_FILE
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        return MAX_MOVED_CARDS_PER_FILE
    pattern = re.compile(rf"^{re.escape(file_key)}--[a-z0-9-]+\.md$")
    found = 0
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                if pattern.match(entry.name) is None:
                    continue
                if entry.is_symlink():
                    continue  # never read through a link, and a link holds no place
                found += _occupies_place(folder / entry.name, source_name=source_name)
                if found >= MAX_MOVED_CARDS_PER_FILE:
                    return MAX_MOVED_CARDS_PER_FILE
    except FileNotFoundError:
        return 0
    except OSError:
        return MAX_MOVED_CARDS_PER_FILE
    return found
