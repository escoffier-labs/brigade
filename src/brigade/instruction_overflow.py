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
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path

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


@dataclass(frozen=True)
class OverflowRoute:
    file: InstructionFile
    card_rel: str  # "memory/cards/instructions/user-md--overflow-preference.md"
    reason: str  # "over-act-at" | "symlinked" | "config-invalid"
    card_problem: str | None  # document_destination_problem(card_rel, target)
    cards: tuple[str, ...]  # every existing overflow card for this file and type, in number order


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
    """Bytes in the real file now: a cached row can predate an append made earlier in the same run."""
    if not file.exists:
        return 0
    try:
        return os.stat(file.real_path).st_size
    except (OSError, ValueError):
        return file.size


def _max_card_bytes(target: Path) -> int:
    from . import memory_cmd

    try:
        return memory_cmd._config_or_default(target).max_card_bytes
    except (ValueError, OSError):
        return memory_cmd.MemoryCareConfig().max_card_bytes


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
    return _with_card(target, file, reason, handoff_type, len(append_block(content)))


def _with_card(target: Path, file: InstructionFile, reason: str, kind: str, extra: int) -> OverflowRoute:
    limit = _max_card_bytes(target)
    cards: list[str] = []
    chosen: str | None = None
    problem: str | None = None
    for number in range(1, _MAX_CARDS + 1):
        rel = f"{CARDS_REL}/{card_name(file.key, kind, number)}"
        found = document_destination_problem(rel, target)
        if found is not None:
            # A link or a non-file in the chain only matters when the append would land there.
            if chosen is None:
                chosen, problem = rel, found
            break
        if not (target / rel).exists():
            chosen = chosen or rel
            break
        cards.append(rel)
        if chosen is None:
            try:
                size = (target / rel).stat().st_size
            except OSError:
                size = limit  # unreadable: treat as full and open the next card
            if size + extra <= limit:
                chosen = rel
    else:
        if chosen is None:
            chosen, problem = f"{CARDS_REL}/{card_name(file.key, kind, _MAX_CARDS)}", "overflow card chain is too long"
    return OverflowRoute(file, chosen or f"{CARDS_REL}/{card_name(file.key, kind, 1)}", reason, problem, tuple(cards))


def existing_card_text(target: Path, overflow: OverflowRoute) -> list[str]:
    """Text of every existing overflow card for the file and type, for the duplicate check.

    Read through the bounded helper. Cards that are unreadable or not UTF-8 are skipped
    (a block cannot be proven present in them).
    """
    texts = []
    for rel in overflow.cards:
        read = read_regular_file(target / rel, max_bytes=INSPECTION_CEILING)
        if read.data is not None:
            texts.append(read.data.decode("utf-8", "replace"))
    return texts


def _source_name(file: InstructionFile) -> str:
    # A file reached by a link displays as its real path, which can be outside the workspace.
    for path in file.paths:
        if not path.startswith(("~/", "<")):
            return path
    return file.display


def card_block(
    target: Path, overflow: OverflowRoute, content: str, handoff_type: str, *, today: date
) -> tuple[bytes, int]:
    """Bytes to append to the overflow card, and how many of them are frontmatter.

    An existing card takes the plain block. A new card takes frontmatter, a blank line
    and the content, and its first block is the whole of that.
    """
    if overflow.card_rel in overflow.cards:
        return append_block(content), 0
    from . import memory_cmd
    from .card_fingerprint import ensure_card_id_frontmatter
    from .memory_care_plan import derived_fresh_until

    try:
        stale_after = memory_cmd._config_or_default(target).stale_after_days
    except (ValueError, OSError):
        stale_after = memory_cmd.MemoryCareConfig().stale_after_days
    name = _source_name(overflow.file)
    lines = [
        "---",
        f"title: {name} overflow: {handoff_type}",
        "category: instructions",
        f"tags: [moved-instructions, {overflow.file.key}, overflow]",
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
