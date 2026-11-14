"""Plan the moves for an append-class instruction file, build the card bytes and run the offline checks.

Pure functions over bytes. No function reads a file, the clock or the environment. The caller passes
the date, the decision id and every set the plan depends on (see the slice 2 plan, "Planner decisions").
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from .guard.engine import scan_text
from .guard.types import ScanOptions
from .instruction_overflow import card_trust_problem
from .instruction_sections import (
    POINTER_LINE,
    SectionDocument,
    Unit,
    heading_lines,
    managed_blocks,
    parse,
    pointer_spans,
)

CARD_OVERHEAD = 512
SLUG_MAX = 48
POINTER_MAX = 240
CARD_DIR = "memory/cards/instructions/"
# Reasons that mean the file is left exactly as it was.
_UNCHANGED = frozenset({"within", "unbalanced-markers", "not-utf8", "pinned-heading-missing"})
_REASONS = _UNCHANGED | {None, "nothing-movable", "still-over-after-moves"}
_NON_SLUG = re.compile(r"[^a-z0-9]+")
_POINTER_DATE = re.compile(rb"memory (\d{4}-\d{2}-\d{2}):")


@dataclass(frozen=True)
class Limits:
    limit: int
    act_at: int
    trim_to: int


@dataclass(frozen=True)
class Run:
    units: tuple[int, ...]  # indexes into doc.units, adjacent, in file order
    start: int
    end: int
    slug: str  # the card's file stem after `<file-key>--`, so it ends in `-<n>` when the name was taken
    card_rel: str
    pointer: bytes  # pointer_line (ends in "\n") plus one more "\n"


@dataclass(frozen=True)
class AddedPointer:
    card_rel: str
    data: bytes  # the pointer line appended at the end, ending in its "\n"
    separator: bool  # a "\n" was added before this pointer because the file did not end in one
    # (only ever the first added pointer, row W29)


@dataclass(frozen=True)
class TrimPlan:
    before: bytes
    after: bytes
    runs: tuple[Run, ...]  # in file order
    added: tuple[AddedPointer, ...]
    reason: str | None  # "within" | "nothing-movable" | "still-over-after-moves"
    # | "unbalanced-markers" | "not-utf8" | "pinned-heading-missing" | None
    skipped: tuple[tuple[int, str], ...]  # (unit index, "pinned" | "held" | "secret" | "blocked"
    # | "too-large-for-card" | "small-run")


def slug(heading: bytes) -> str:
    """A card name part from a heading: `[a-z0-9-]` only, at most 48 characters, never empty, never `overflow...`."""
    text = heading.decode("utf-8", "replace").lstrip().lstrip("#").lower()
    name = _NON_SLUG.sub("-", text).strip("-") or "section"
    if name.startswith("overflow"):
        name = "section-" + name
    return name[:SLUG_MAX].strip("-")


def card_rel(file_key: str, slug: str, n: int) -> str:
    return f"{CARD_DIR}{file_key}--{slug}{'' if n == 1 else f'-{n}'}.md"


def pointer_line(card_rel: str, *, today: date, decision_id: str, overflow: bool = False) -> bytes:
    kind = "Overflow in" if overflow else "Moved to"
    return f"- {kind} memory {today.isoformat()}: {card_rel} (undo: {decision_id})\n".encode()


def order_units(doc: SectionDocument) -> tuple[int, ...]:
    """Units that are mostly links from the last to the first, then every other unit from the last to the first."""
    indexes = range(len(doc.units) - 1, -1, -1)
    return (*(i for i in indexes if doc.units[i].links), *(i for i in indexes if not doc.units[i].links))


def _pin_active(unit: Unit, today: date) -> bool:
    if unit.pin is None:
        return False
    if unit.pin == "marker":
        return True
    return date.fromisoformat(unit.pin.removeprefix("until:")) >= today


def _config_pinned(unit: Unit, wanted: frozenset[bytes]) -> bool:
    return any(heading.rstrip() in wanted for heading in unit.headings)


def _groups(doc: SectionDocument, indexes: Sequence[int], cap: int) -> list[tuple[int, ...]]:
    """Maximal groups of chosen units whose spans touch, split before a unit that would pass the card cap."""
    groups: list[list[int]] = []
    for index in sorted(indexes):
        unit = doc.units[index]
        if groups:
            first, last = doc.units[groups[-1][0]], doc.units[groups[-1][-1]]
            if last.end == unit.start and unit.end - first.start <= cap:
                groups[-1].append(index)
                continue
        groups.append([index])
    return [tuple(group) for group in groups]


def _runs(
    doc: SectionDocument,
    groups: Sequence[tuple[int, ...]],
    *,
    file_key: str,
    today: date,
    decision_id: str,
    taken: frozenset[str],
) -> tuple[Run, ...]:
    used = set(taken)
    runs = []
    for group in groups:
        base = slug(doc.units[group[0]].heading)
        number = 1
        while card_rel(file_key, base, number).rsplit("/", 1)[1] in used:
            number += 1
        rel = card_rel(file_key, base, number)
        used.add(rel.rsplit("/", 1)[1])
        pointer = pointer_line(rel, today=today, decision_id=decision_id) + b"\n"
        runs.append(
            Run(
                units=group,
                start=doc.units[group[0]].start,
                end=doc.units[group[-1]].end,
                slug=rel.removeprefix(f"{CARD_DIR}{file_key}--").removesuffix(".md"),
                card_rel=rel,
                pointer=pointer,
            )
        )
    return tuple(runs)


def _replace(data: bytes, runs: Sequence[Run]) -> bytes:
    out: list[bytes] = []
    cursor = 0
    for run in runs:
        out += [data[cursor : run.start], run.pointer]
        cursor = run.end
    out.append(data[cursor:])
    return b"".join(out)


def _with_pointers(
    body: bytes,
    overflow: Sequence[str],
    *,
    today: date,
    decision_id: str,
    limit: int | None,
) -> tuple[bytes, tuple[AddedPointer, ...]]:
    """`body` with a pointer appended for each overflow card. With a `limit`, a pointer that would take the
    result over it is left out. Its separator byte counts, as the first added pointer carries it."""
    out = body
    added: list[AddedPointer] = []
    for rel in overflow:
        line = pointer_line(rel, today=today, decision_id=decision_id, overflow=True)
        separator = not added and bool(out) and not out.endswith(b"\n")
        if limit is not None and len(out) + separator + len(line) > limit:
            continue
        out = out + (b"\n" if separator else b"") + line
        added.append(AddedPointer(rel, line, separator))
    return out, tuple(added)


def plan_append(
    doc: SectionDocument,
    *,
    file_key: str,
    limits: Limits,
    today: date,
    decision_id: str,
    taken: frozenset[str],
    held: frozenset[str],
    config_pins: tuple[str, ...],
    overflow: Sequence[str],
    max_moves: int,
    max_card_bytes: int,
) -> TrimPlan:
    data = doc.data

    def unchanged(reason: str, skipped: Sequence[tuple[int, str]] = ()) -> TrimPlan:
        return TrimPlan(data, data, (), (), reason, tuple(skipped))

    if doc.problem is not None:
        return unchanged(doc.problem)
    wanted = frozenset(pin.encode("utf-8").rstrip() for pin in config_pins)
    if wanted - {heading.rstrip() for heading in heading_lines(data)}:
        return unchanged("pinned-heading-missing")
    # A pointer appended inside an unclosed fence would sit where restore and reconcile never look (row W30).
    due = () if doc.eof_in_fence else tuple(overflow)
    moving = len(data) > limits.act_at
    if not moving and not due:
        return unchanged("within")

    def finish(runs: tuple[Run, ...]) -> tuple[bytes, tuple[AddedPointer, ...]]:
        return _with_pointers(_replace(data, runs), due, today=today, decision_id=decision_id, limit=limits.limit)

    skipped: list[tuple[int, str]] = []
    runs: tuple[Run, ...] = ()
    unused = False
    if moving:
        cap = max_card_bytes - CARD_OVERHEAD
        candidates: list[int] = []
        for index in order_units(doc):
            unit = doc.units[index]
            text = data[unit.start : unit.end].decode("utf-8")
            if _pin_active(unit, today) or _config_pinned(unit, wanted):
                skip = "pinned"
            elif unit.blocked is not None:
                skip = "blocked"
            elif unit.sha256 in held:
                skip = "held"
            elif unit.end - unit.start > cap:
                skip = "too-large-for-card"
            elif scan_text(text, options=ScanOptions(honor_allow_comments=False)).blocked:
                skip = "secret"
            else:
                candidates.append(index)
                continue
            skipped.append((index, skip))

        def build(chosen: Sequence[int]) -> tuple[Run, ...]:
            return _runs(
                doc,
                _groups(doc, chosen, cap),
                file_key=file_key,
                today=today,
                decision_id=decision_id,
                taken=taken,
            )

        def projected(chosen: Sequence[int]) -> int:
            body = _replace(data, build(chosen))
            return len(_with_pointers(body, due, today=today, decision_id=decision_id, limit=None)[0])

        chosen: list[int] = []
        queue = list(candidates)
        while queue and len(chosen) < max_moves and projected(chosen) > limits.trim_to:
            chosen.append(queue.pop(0))
        unused = bool(queue)
        # The three-times rule applies per run, once. A dropped run is reported and nothing refills its place.
        kept = []
        for group in _groups(doc, chosen, cap):
            probe = build(group)[0]
            if probe.end - probe.start < 3 * len(probe.pointer):
                skipped.extend((index, "small-run") for index in group)
            else:
                kept.append(group)
        runs = _runs(doc, kept, file_key=file_key, today=today, decision_id=decision_id, taken=taken)
    after, added = finish(runs)
    if not moving:
        reason = None if added else "within"
    elif len(after) > limits.trim_to and unused:
        reason = "still-over-after-moves"
    elif len(after) > limits.act_at and not unused:
        reason = "nothing-movable"
    else:
        reason = None
    return TrimPlan(data, after, runs, added, reason, tuple(sorted(skipped)))


def card_bytes(
    *,
    file_key: str,
    slug: str,
    section: bytes,
    source_name: str,
    decision_id: str,
    today: date,
    fresh_until: str | None,
    card_id: str,
) -> bytes:
    """The private card for one moved run: frontmatter, then the run's bytes unchanged. No heading text enters it."""
    from .memory_care_instructions import encode_value

    lines = [
        "---",
        f"title: {file_key}--{slug}",
        "category: instructions",
        f"tags: [moved-instructions, {file_key}, moved]",
        "visibility: private",
        f"source_file: {encode_value(source_name)}",
        "archive: never",
        f"moved_on: {today.isoformat()}",
        f"moved_by: {decision_id}",
        f"last_reviewed: {today.isoformat()}",
    ]
    if fresh_until is not None:
        lines.append(f"fresh_until: {fresh_until}")
    lines += [f"id: {card_id}", "---"]
    return ("\n".join(lines) + "\n").encode("utf-8") + section


def _card_body(card: bytes, section: bytes) -> bool:
    """Whether `card` is one frontmatter block followed by exactly `section`."""
    if not card.endswith(section) or not card.startswith(b"---\n"):
        return False
    head = card[: len(card) - len(section)].split(b"\n")[:-1]
    return head[-1:] == [b"---"] and all(line.strip() != b"---" for line in head[1:-1])


def _plan_date(plan: TrimPlan) -> date | None:
    for pointer in (*(run.pointer for run in plan.runs), *(added.data for added in plan.added)):
        found = _POINTER_DATE.search(pointer)
        if found is not None:
            return date.fromisoformat(found.group(1).decode("ascii"))
    return None


def _pinned_unit(unit: Unit, today: date | None, wanted: frozenset[bytes]) -> bool:
    if _config_pinned(unit, wanted) or unit.pin == "marker":
        return True
    return unit.pin is not None and (today is None or _pin_active(unit, today))


def _reversible(plan: TrimPlan) -> bool:
    """`after` is `before` with each run replaced by its pointer, then the added pointers at the end."""
    added = plan.added
    if any(pointer.separator for pointer in added[1:]):
        return False
    tail = (b"\n" if added and added[0].separator else b"") + b"".join(pointer.data for pointer in added)
    body = plan.after
    if tail:
        if not body.endswith(tail):
            return False
        body = body[: -len(tail)]
    cursor = 0
    expected = 0
    for run in plan.runs:
        gap = plan.before[expected : run.start]
        if run.start < expected or run.end > len(plan.before) or body[cursor : cursor + len(gap)] != gap:
            return False
        cursor += len(gap)
        if body[cursor : cursor + len(run.pointer)] != run.pointer:
            return False
        cursor += len(run.pointer)
        expected = run.end
    return body[cursor:] == plan.before[expected:]


def check(plan: TrimPlan, cards: Mapping[str, bytes], *, config_pins: tuple[str, ...]) -> list[str]:
    """One name per failed check, empty when the plan is safe to commit. Eight checks run:

    reversible (the design's 1), cards (2), managed-blocks (3), fixed-and-pinned (4), structure (5),
    pointers (6), size (7), and pointer-in-block, which the plan adds: every added pointer must be a
    `pointer_spans` entry of `after` that sits outside any fence or managed block.

    `check` has no `Limits`, so `size` verifies what the planner claims: a known reason, and no change
    to the file when the reason says nothing was done.
    """
    before, after = plan.before, plan.after
    failed: list[str] = []
    if not _reversible(plan):
        failed.append("reversible")
    cards_ok = True
    for run in plan.runs:
        card = cards.get(run.card_rel)
        if card is None or not _card_body(card, before[run.start : run.end]):
            cards_ok = False
            continue
        try:
            cards_ok = cards_ok and card_trust_problem(card.decode("utf-8")) is None
        except UnicodeDecodeError:
            cards_ok = False
    if not cards_ok:
        failed.append("cards")
    if managed_blocks(before) != managed_blocks(after):
        failed.append("managed-blocks")
    old = parse(before)
    fixed_ok = True
    if old.problem is None:
        fixed_ok = after.startswith(before[: old.fixed_end])
        wanted = frozenset(pin.encode("utf-8").rstrip() for pin in config_pins)
        today = _plan_date(plan)
        for unit in old.units:
            if not _pinned_unit(unit, today, wanted):
                continue
            moved = any(run.start < unit.end and unit.start < run.end for run in plan.runs)
            fixed_ok = fixed_ok and not moved and before[unit.start : unit.end] in after
    if not fixed_ok:
        failed.append("fixed-and-pinned")
    new = parse(after)
    if plan.reason in {"unbalanced-markers", "not-utf8"}:
        structure_ok = after == before
    else:
        structure_ok = new.problem is None and (old.eof_in_fence or not new.eof_in_fence)
    if not structure_ok:
        failed.append("structure")
    lines = [run.pointer[:-1] for run in plan.runs] + [pointer.data for pointer in plan.added]
    pointers_ok = all(
        POINTER_LINE.fullmatch(line[:-1]) is not None and len(line) <= POINTER_MAX and after.count(line) == 1
        for line in lines
    ) and all(run.card_rel in cards and run.card_rel.encode() in run.pointer for run in plan.runs)
    if not pointers_ok:
        failed.append("pointers")
    if plan.reason not in _REASONS or (plan.reason in _UNCHANGED and (after != before or plan.runs or plan.added)):
        failed.append("size")
    spans = {span.start: span for span in pointer_spans(after)}
    start = len(after) - sum(len(pointer.data) for pointer in plan.added)
    for pointer in plan.added:
        span = spans.get(start)
        if span is None or span.in_block or after[span.start : span.end] != pointer.data:
            failed.append("pointer-in-block")
            break
        start += len(pointer.data)
    return failed
