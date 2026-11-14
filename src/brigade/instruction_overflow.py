"""Where an append to an always-loaded instruction file goes when the file cannot take it.

A handoff aimed at a known instruction file normally appends to that file. When the
append would carry the file past its act-at threshold, or the file is reached through a
symlink, or the `[instructions]` table cannot be read, the content goes to a private
overflow card instead: `memory/cards/instructions/<file-key>--overflow-<type>.md`,
rolling to `-2`, `-3`. Nothing here writes. The decision is read-only so ingest can make
it before it holds a lock, and the card is published by `commit_card_append`.

Exclusion and `enabled = false` keep routing on. The old size refusal is gone, so this
decision is the only guard on a capped file, and a configuration value that turns the
report off must not turn the guard off with it.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path
from typing import NamedTuple

from .bounded_read import read_regular_file
from .handoff_ingest_rules import document_destination_problem
from .instruction_inventory import (
    INSPECTION_CEILING,
    ROOT_NAMES,
    InstructionFile,
    Inventory,
    InstructionsConfig,
    discover,
    find,
    load_instructions_config,
)

HANDOFF_TYPES = (
    "setup",
    "workflow",
    "bugfix",
    "decision",
    "security",
    "preference",
    "research",
    "project-context",
)
CARDS_REL = "memory/cards/instructions"
MARKER_TAG = "moved-instructions"
# An overflow chain that long is not a care problem a handoff should be written into.
_MAX_CARDS = 1000


def handoff_type(sections: Mapping[str, str]) -> str:
    """The handoff's `Type`, when it is one of the template values, else `project-context`."""
    lines = sections.get("type", "").strip().splitlines()
    value = lines[0].strip().lower() if lines else ""
    return value if value in HANDOFF_TYPES else "project-context"


def append_block(content: str) -> bytes:
    """The bytes one append adds. The guard, the card writer and every document writer use this."""
    return b"\n\n" + content.strip().encode("utf-8") + b"\n"


def card_name(file_key: str, handoff_type: str, n: int) -> str:
    suffix = "" if n <= 1 else f"-{n}"
    return f"{file_key}--overflow-{handoff_type}{suffix}.md"


def normalize_for_dedupe(text: str) -> str:
    """Collapse whitespace and drop blank lines for content-equality checks. Ingest's duplicate check uses it too."""
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


class Held(NamedTuple):
    """Why a routed append is kept for a person: an ingest outcome code and the reason text."""

    code: str
    reason: str


@dataclass(frozen=True)
class OverflowRoute:
    file: InstructionFile
    card_rel: str  # "memory/cards/instructions/user-md--overflow-preference.md"
    reason: str  # "over-act-at" | "symlinked" | "config-invalid"
    card_problem: str | None  # document_destination_problem(card_rel, target)
    cards: tuple[str, ...]  # every existing overflow card for this file and type, in number order
    # The card this append would join is not one this feature created, or a card cannot be read in
    # full for the duplicate check. Nothing is written to it.
    card_untrusted: str | None = None
    # The block cannot fit in an empty card, or would not read back as a card this feature wrote.
    block_problem: str | None = None
    card_limit: int = 0  # effective bytes per card: max_card_bytes, never above what the duplicate scan reads
    limit_requested: int | None = None  # the configured max_card_bytes when it was clamped, else None

    def hold(self) -> Held | None:
        """The reason this append must not be written, or None when it may be."""
        if self.card_problem:
            return Held(code="document-destination-unsafe", reason=self.card_problem)
        if self.card_untrusted:
            return Held(code="overflow-card-untrusted", reason=f"{self.card_rel}: {self.card_untrusted}")
        if self.block_problem:
            return Held(code="overflow-block-too-large", reason=self.block_problem)
        return None


@dataclass
class RouteContext:
    """The inventory for one ingest run, read once.

    Discovery reads several configuration files and resolves every candidate path, so a
    batch of handoffs must not repeat it. Only sizes change between handoffs, and `route`
    measures the real file again every time.
    """

    target: Path
    home: Path | None = None
    environ: Mapping[str, str] | None = None
    _loaded: tuple[Inventory, bool] | None = field(default=None, repr=False)

    def load(self) -> tuple[Inventory, bool]:
        if self._loaded is None:
            self._loaded = _discover(self.target, self.home, self.environ)
        return self._loaded


def _discover(target: Path, home: Path | None, environ: Mapping[str, str] | None) -> tuple[Inventory, bool]:
    invalid = False
    try:
        config = load_instructions_config(target)
    except ValueError:
        config, invalid = InstructionsConfig(), True
    # decision 14: the report honours these two keys, routing does not.
    config = replace(config, enabled=True, exclude=())
    return discover(target, config, home=home, environ=environ, include_missing=True), invalid


def _has_symlink(document: str, target: Path) -> bool:
    path = target
    for part in Path(document).parts:
        path = path / part
        if path.is_symlink():
            return True
    return False


def _size_now(file: InstructionFile) -> int:
    """Bytes in the real file now.

    A cached row can predate an append made earlier in the same run, and a file that was
    missing when discovery ran may have been created since, so the file is always measured
    again. Only a file that is still missing counts as empty.
    """
    try:
        return os.stat(file.real_path).st_size
    except FileNotFoundError:
        return 0
    except (OSError, ValueError):
        return file.size


def _card_limit(target: Path) -> tuple[int, int | None]:
    """Bytes one card may hold, and the configured value when it had to be clamped.

    The duplicate check reads a whole card or none of it, up to `INSPECTION_CEILING`, so a
    configured `max_card_bytes` above that is not applied (the inventory clamps limits the same way).
    """
    from . import memory_cmd

    try:
        configured = memory_cmd._config_or_default(target).max_card_bytes
    except (ValueError, OSError):
        configured = memory_cmd.MemoryCareConfig().max_card_bytes
    if configured > INSPECTION_CEILING:
        return INSPECTION_CEILING, configured
    return configured, None


def route(
    target: Path,
    document: str,
    content: str,
    handoff_type: str,
    *,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
    context: RouteContext | None = None,
) -> OverflowRoute | None:
    """The overflow route for an append to `document`, or None when it goes to the file itself.

    None also means the document is not a known instruction file, so the caller's own
    destination checks decide. The row comes from the inventory for the real file.
    """
    inventory, invalid = context.load() if context is not None else _discover(target, home, environ)
    file = find(inventory, document)
    if file is None:
        return None
    symlinked = _has_symlink(document, target)
    if symlinked and document not in ROOT_NAMES:
        # Only a per-name root file may route through a link. A file a configuration value
        # enrolled keeps the plain refusal: a configuration value is not a way past a symlink.
        return None
    if invalid:
        reason = "config-invalid"
    elif symlinked:
        reason = "symlinked"
    elif _size_now(file) + len(append_block(content)) > file.act_at:
        reason = "over-act-at"
    else:
        return None
    return _with_card(target, file, reason, handoff_type, content)


def _card_numbers(target: Path, file_key: str, kind: str) -> dict[int, str]:
    """Number to workspace path for every overflow card of the file and type that exists.

    One directory listing, not a walk that stops at the first gap: after an undo deletes card 1
    and leaves card 2, card 2 still holds published content that a replay must find.
    """
    pattern = re.compile(rf"^{re.escape(file_key)}--overflow-{re.escape(kind)}(?:-([1-9][0-9]*))?\.md$")
    found: dict[int, str] = {}
    try:
        with os.scandir(target / CARDS_REL) as entries:
            names = [entry.name for entry in entries]
    except (OSError, ValueError):
        return found
    for name in names:
        match = pattern.match(name)
        if match is None:
            continue
        number = int(match.group(1)) if match.group(1) else 1
        if (match.group(1) is None or number >= 2) and number <= _MAX_CARDS:
            found[number] = f"{CARDS_REL}/{name}"
    return found


def _with_card(target: Path, file: InstructionFile, reason: str, kind: str, content: str) -> OverflowRoute:
    limit, requested = _card_limit(target)
    extra = len(append_block(content))
    numbers = _card_numbers(target, file.key, kind)
    cards: list[str] = []
    unreadable: str | None = None
    chosen: str | None = None
    problem: str | None = None
    chosen_exists = False
    for number in range(1, min(max(numbers, default=0) + 1, _MAX_CARDS) + 1):
        rel = f"{CARDS_REL}/{card_name(file.key, kind, number)}"
        found = document_destination_problem(rel, target)
        if found is not None:
            # A link or a non-file in the chain only matters when the append would land there.
            if chosen is None:
                chosen, problem = rel, found
            continue
        if number not in numbers:
            if chosen is None:
                chosen = rel
            continue
        cards.append(rel)
        try:
            size = (target / rel).stat().st_size
        except OSError:
            size = limit  # unreadable: treat as full and open the next card
        if size > INSPECTION_CEILING and unreadable is None:
            unreadable = f"{rel} is over {INSPECTION_CEILING} bytes, so it cannot be searched for duplicates"
        if chosen is None and size + extra <= limit:
            chosen, chosen_exists = rel, True
    if chosen is None:
        chosen, problem = f"{CARDS_REL}/{card_name(file.key, kind, _MAX_CARDS)}", "overflow card chain is too long"
    untrusted = unreadable
    block_problem: str | None = None
    if problem is None and chosen_exists:
        untrusted = untrusted or _untrusted_card(target, chosen)
    elif problem is None:
        first, _ = _first_block(target, file, content, kind, today=date.today())
        if len(first) > limit:
            block_problem = (
                f"a {len(first)} byte first card block does not fit the {limit} byte card limit; "
                "split the content into smaller handoffs"
            )
        else:
            not_a_card = card_trust_problem(first.decode("utf-8", "replace"))
            if not_a_card:
                block_problem = f"the content would not read back as a private overflow card: {not_a_card}"
    return OverflowRoute(file, chosen, reason, problem, tuple(cards), untrusted, block_problem, limit, requested)


def _untrusted_card(target: Path, rel: str) -> str | None:
    """Why an existing card cannot take an append, or None when this feature created it.

    The path is predictable, so a file at it may be anyone's. A public card or one without
    the markers would publish a private handoff to the hub.
    """
    read = read_regular_file(target / rel, max_bytes=INSPECTION_CEILING)
    if read.data is None:
        return f"the card cannot be read ({read.reason})"
    try:
        text = read.data.decode("utf-8")
    except UnicodeDecodeError:
        return "the card is not UTF-8"
    return card_trust_problem(text)


class WriteProblem(NamedTuple):
    """Why bytes must not be appended to a card now: an ingest outcome code, the reason, and whether the card is full."""

    code: str
    reason: str
    full: bool = False


def write_problem(current: bytes | None, block: bytes, *, created: bool, card_limit: int | None) -> WriteProblem | None:
    """Whether `block` may be appended to a card whose exact bytes are `current` (None: no card).

    The one rule every writer applies to the bytes it is about to append to, under the document
    lock, at the moment of writing: the card is in the state the decision recorded, it is one this
    feature created, and the append keeps it within `card_limit`.
    """
    if (current is None) != created:
        return WriteProblem("document-destination-unwritable", "the overflow card changed since the block was built")
    if current is None:
        problem = card_trust_problem(block.decode("utf-8", "replace"))
        if problem:
            return WriteProblem("overflow-card-untrusted", f"the new card would not read back as private: {problem}")
    else:
        problem = untrusted_bytes(current)
        if problem:
            return WriteProblem("overflow-card-untrusted", f"the card is not one this feature created: {problem}")
    if card_limit is not None and len(current or b"") + len(block) > card_limit:
        return WriteProblem("document-destination-unwritable", "the overflow card has no room for the block", True)
    return None


class DirectPin(NamedTuple):
    """What a direct append to a document must find when it opens it, decided under the document lock."""

    act_at: int | None  # the file may not pass this many bytes; None for a file that is not a known instruction file
    identity: tuple[int, int] | None  # (st_dev, st_ino) of the file now, None when it does not exist


def direct_pin(
    target: Path,
    document: str,
    *,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
    context: RouteContext | None = None,
) -> DirectPin:
    """The pin for a direct append to `document`. Call it holding the document lock, right before the write."""
    inventory, _ = context.load() if context is not None else _discover(target, home, environ)
    file = find(inventory, document)
    try:
        info = os.lstat(target / document)
    except (OSError, ValueError):
        identity = None
    else:
        identity = (info.st_dev, info.st_ino)
    return DirectPin(file.act_at if file is not None else None, identity)


def untrusted_bytes(data: bytes) -> str | None:
    """`card_trust_problem` for the bytes a writer is about to append to: the last check before the write."""
    try:
        return card_trust_problem(data.decode("utf-8"))
    except UnicodeDecodeError:
        return "the card is not UTF-8"


def _frontmatter_lines(text: str) -> tuple[list[str], list[str]] | None:
    """The lines of the one leading frontmatter block, and the lines after it. None when there is none."""
    lines = text.replace("\r\n", "\n").split("\n")
    if not lines or lines[0] != "---":
        return None
    end = next((index for index in range(1, len(lines)) if lines[index].strip() == "---"), None)
    if end is None:
        return None
    return lines[1:end], lines[end + 1 :]


def _tags(values: list[str]) -> set[str]:
    """Exact tag members of every `tags:` line, in the list form (`[a, b]`) or the string form (`a, b`)."""
    found: set[str] = set()
    for value in values:
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            value = value[1:-1]
        found.update(tag.strip().strip("'\"") for tag in value.split(","))
    return found


def _is_field(line: str) -> bool:
    return bool(re.match(r"^[A-Za-z_][A-Za-z0-9_-]*\s*:", line))


def card_trust_problem(text: str) -> str | None:
    """Why `text` is not an overflow card this feature created, or None.

    Visibility is decided by the parser hub sync uses, so the two cannot disagree. The card must
    also carry the fields `card_block` writes, and must have exactly one frontmatter block.
    """
    from .memory_hub_sync import VISIBILITY_PRIVATE, withheld

    parts = _frontmatter_lines(text)
    if parts is None:
        return "no frontmatter block"
    block, rest = parts
    if withheld(text) != VISIBILITY_PRIVATE:
        return "its visibility is not private"
    fields: dict[str, list[str]] = {}
    for line in block:
        if _is_field(line):
            key, value = line.split(":", 1)
            fields.setdefault(key.strip().lower(), []).append(value.strip().strip("'\""))
    if fields.get("visibility") != ["private"]:
        return "it does not have exactly one `visibility: private` line"
    if fields.get("archive") != ["never"]:
        return "it is not marked `archive: never`"
    if MARKER_TAG not in _tags(fields.get("tags", [])):
        return "it lacks the `moved-instructions` tag"
    if not any(value for value in fields.get("source_file", [])):
        return "it lacks `source_file`"
    after = next((line for line in rest if line.strip()), None)
    if after is not None and after.strip() == "---":
        # Another fence straight after the block, closed and made of fields only: a second block.
        tail = rest[rest.index(after) + 1 :]
        end = next((index for index, line in enumerate(tail) if line.strip() == "---"), None)
        if end is not None and all(_is_field(line) or not line.strip() for line in tail[:end]):
            return "it has a second frontmatter block"
    return None


def refresh(target: Path, overflow: OverflowRoute, content: str, handoff_type: str) -> OverflowRoute:
    """The same route with its cards read again. Writers call it holding the document lock.

    `route` ran before the lock, so a card may have been created or filled since. The block
    a writer builds must match the card as it is when the write happens, and the card must
    still be one this feature created.
    """
    return _with_card(target, overflow.file, overflow.reason, handoff_type, content)


def contains_block(target: Path, overflow: OverflowRoute, content: str) -> bool:
    """Whether the content is already in an overflow card of the file and type (a replay)."""
    needle = normalize_for_dedupe(content)
    return bool(needle) and any(needle in normalize_for_dedupe(text) for text in existing_card_text(target, overflow))


def existing_card_text(target: Path, overflow: OverflowRoute) -> list[str]:
    """Text of every existing overflow card for the file and type, for the duplicate check.

    Read through the bounded helper. Cards that are unreadable are skipped, and `_with_card`
    holds the handoff when a card is too large to read, so a skipped card never hides a replay.
    """
    return _read_cards(target, overflow.cards)


def _read_cards(target: Path, rels: tuple[str, ...] | list[str]) -> list[str]:
    texts = []
    for rel in rels:
        read = read_regular_file(target / rel, max_bytes=INSPECTION_CEILING)
        if read.data is not None:
            texts.append(read.data.decode("utf-8", "replace"))
    return texts


def direct_card_text(
    target: Path,
    document: str,
    handoff_type: str,
    *,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
    context: RouteContext | None = None,
) -> list[str]:
    """Text of the overflow cards of a known file, for an append that goes to the file itself.

    A person may trim the file after content was published to a card and before a replay, so
    the direct path looks in the cards too, or the content would land in both places.
    """
    inventory, _ = context.load() if context is not None else _discover(target, home, environ)
    file = find(inventory, document)
    if file is None:
        return []
    rels = [
        rel
        for _, rel in sorted(_card_numbers(target, file.key, handoff_type).items())
        if document_destination_problem(rel, target) is None
    ]
    return _read_cards(target, rels)


def _source_name(file: InstructionFile) -> str:
    # A file reached by a link displays as its real path, which can be outside the workspace.
    for path in file.paths:
        if not path.startswith(("~/", "<")):
            return path
    return file.display


def _first_block(
    target: Path, file: InstructionFile, content: str, handoff_type: str, *, today: date
) -> tuple[bytes, int]:
    from . import memory_cmd
    from .card_fingerprint import ensure_card_id_frontmatter
    from .memory_care_plan import derived_fresh_until

    try:
        stale_after = memory_cmd._config_or_default(target).stale_after_days
    except (ValueError, OSError):
        stale_after = memory_cmd.MemoryCareConfig().stale_after_days
    name = _source_name(file)
    # Undo of the block that created a card needs these bytes to stay exactly as written: a later
    # stage that rewrites this frontmatter (`last_reviewed`, `fresh_until`) makes that undo refuse.
    lines = [
        "---",
        f"title: {name} overflow: {handoff_type}",
        "category: instructions",
        f"tags: [moved-instructions, {file.key}, overflow]",
        "visibility: private",
        f"source_file: {name}",
        "archive: never",
        f"last_reviewed: {today.isoformat()}",
    ]
    fresh = derived_fresh_until(today, stale_after)
    if fresh is not None:
        lines.append(f"fresh_until: {fresh}")
    lines.append("---")
    frontmatter = ensure_card_id_frontmatter("\n".join(lines) + "\n").encode("utf-8")
    return frontmatter + b"\n" + content.strip().encode("utf-8") + b"\n", len(frontmatter)


def card_block(
    target: Path, overflow: OverflowRoute, content: str, handoff_type: str, *, today: date
) -> tuple[bytes, int]:
    """Bytes to append to the overflow card, and how many of them are frontmatter.

    An existing card takes the plain block. A new card takes frontmatter, a blank line
    and the content, and its first block is the whole of that.
    """
    if overflow.card_rel in overflow.cards:
        return append_block(content), 0
    return _first_block(target, overflow.file, content, handoff_type, today=today)
