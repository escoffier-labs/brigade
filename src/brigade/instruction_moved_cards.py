"""Count the moved cards that still exist for one instruction file (issue #2565).

A moved card carries `archive: never`, so the retire stage never removes one and a file
that keeps receiving appends would keep producing cards. `instruction_trim` bounds that
growth with `MAX_MOVED_CARDS_PER_FILE` and reports `moved_card_cap_reached` when a file
reaches it. The move stage passes `count` to the planner.

Counting reads the card folder, not the journal, so it stays exact after a card is
deleted or a move is undone: only a card that still exists and still names the file in
`source_file` counts. Nothing here merges, compacts or deletes a card, so every decision
the journal still records keeps its before-image and its card and can still be undone
byte for byte.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from .bounded_read import read_regular_file
from .instruction_inventory import INSPECTION_CEILING
from .instruction_overflow import CARDS_REL
from .instruction_sections import FILE_KEY
from .memory_care_instructions import encode_value

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
    """Whether `text` is a moved card this feature created for the file named by `source_name`."""
    fields = _frontmatter(text)
    if fields is None:
        return False
    tags = _tags(fields.get("tags", []))
    if MARKER_TAG not in tags or MOVED_TAG not in tags:
        return False
    return _source(fields.get("source_file", [])) == encode_value(source_name)


def count(target: Path, *, file_key: str, source_name: str) -> int:
    """Existing moved cards for one instruction file, named by its key and its card source value.

    `source_name` is the value the move stage passes to `card_bytes`, so creation and this
    count always compare the same encoded `source_file` value. A card that cannot be read
    in full, or does not parse, does not count: it cannot be shown to carry this source.
    """
    if FILE_KEY.fullmatch(file_key) is None:
        return 0
    pattern = re.compile(rf"^{re.escape(file_key)}--[a-z0-9-]+\.md$")
    try:
        with os.scandir(target / CARDS_REL) as entries:
            names = sorted(entry.name for entry in entries)
    except (OSError, ValueError):
        return 0
    found = 0
    for name in names:
        if pattern.match(name) is None:
            continue
        read = read_regular_file(target / CARDS_REL / name, max_bytes=INSPECTION_CEILING)
        if read.data is None:
            continue
        try:
            text = read.data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if is_moved_card(text, source_name=source_name):
            found += 1
    return found
