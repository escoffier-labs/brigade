"""Reconcile an instruction file with its moved cards, and plan a restore, from bytes alone.

Pure functions. Both enforce the recovery limits themselves, so a forged or damaged journal record
gains nothing over a direct write (slice 2 plan, "Threat model"): a record whose card is outside
`memory/cards/instructions/<file-key>--*` is skipped, text goes back only when it hashes to the
digest recorded at move time, and a card is removed only when its body is present in the file as
whole parsed sections.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .instruction_sections import (
    FILE_KEY,
    POINTER_LINE,
    PointerSpan,
    parse,
    pointer_spans,
    whole_section_count,
    whole_section_spans,
)


@dataclass(frozen=True)
class RecordedRun:
    decision_id: str
    card_rel: str
    pointer: bytes  # the pointer line with its newline
    section: bytes  # read from the move's before-image of the file
    section_sha256: str


@dataclass(frozen=True)
class RecordedPointer:
    decision_id: str
    card_rel: str
    data: bytes  # the overflow pointer line the move appended, without any separator
    separator: bool = False  # the move added a "\n" before this pointer (AddedPointer.separator, row W29)


@dataclass(frozen=True)
class Reconciled:
    after: bytes
    remove_cards: tuple[str, ...]
    restored: tuple[str, ...]  # cards whose pointer went back to text
    dropped: tuple[str, ...]  # overflow cards whose pointer was removed
    completed: tuple[str, ...]  # decision ids with no run or overflow pointer left, every run back or removed_by_edit
    removed_by_edit: tuple[str, ...]  # cards whose pointer and card were both deleted after the move (P24)
    kept: tuple[tuple[str, str], ...]  # (card, "card-changed" | "pointer-duplicated" | "pointer-in-block"
    #  | "pointer-missing" | "lost")
    ignored: int  # recorded runs and pointers outside memory/cards/instructions/<file_key>--*


@dataclass(frozen=True)
class Restore:
    after: bytes  # equals the file when only cards are removed
    remove_cards: tuple[str, ...]
    held: tuple[str, ...]  # sha256 of every unit of parse(after) inside a span put back
    held_cards: tuple[str, ...]  # overflow cards whose pointer this restore removed


@dataclass(frozen=True)
class Refusal:
    reason: str  # "pointer-missing" | "pointer-duplicated" | "pointer-in-block" | "card-missing"
    # | "card-changed" | "lost" | "already-restored" | "nothing-to-restore"
    card_rel: str | None


@dataclass(frozen=True)
class _Edit:
    start: int
    end: int
    text: bytes
    separator: bool = False  # removes an overflow pointer whose move also added a "\n" before it


def card_body(card: bytes) -> bytes | None:
    """The bytes after the first frontmatter block (`---` first line, closed by the next `---` line), or None."""
    if not card.startswith(b"---\n"):
        return None
    offset = 4
    while offset < len(card):
        newline = card.find(b"\n", offset)
        stop = len(card) if newline == -1 else newline + 1
        if card[offset:stop].strip() == b"---":
            return card[stop:]
        offset = stop
    return None


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _card_pattern(file_key: str) -> re.Pattern[str] | None:
    """The card paths this file's records may name, or None when `file_key` is not a safe key."""
    if FILE_KEY.fullmatch(file_key) is None:
        return None
    return re.compile(rf"memory/cards/instructions/{re.escape(file_key)}--[a-z0-9-]+\.md")


def _bound(line: bytes, kind: bytes, card_rel: str, decision_id: str) -> bool:
    """`line` (with its one newline) is a `kind` pointer line that names `card_rel` and `decision_id`."""
    found = POINTER_LINE.fullmatch(line[:-1]) if line.endswith(b"\n") else None
    return (
        found is not None
        and found["kind"] == kind
        and found["path"] == card_rel.encode()
        and found["undo"] == decision_id.encode()
    )


def _trusted(
    file_key: str, runs: Sequence[RecordedRun], pointers: Sequence[RecordedPointer]
) -> tuple[list[RecordedRun], list[RecordedPointer]]:
    """The records recovery may use. A record is used only when its card is under this file's key and its
    recorded pointer line is of the right kind and names that card and decision, so a record can never act on
    another record's pointer. The rest are foreign and counted in `ignored`."""
    pattern = _card_pattern(file_key)
    if pattern is None:
        return [], []
    good_runs = [
        run
        for run in runs
        if pattern.fullmatch(run.card_rel) and _bound(run.pointer, b"Moved to", run.card_rel, run.decision_id)
    ]
    good_pointers = [
        pointer
        for pointer in pointers
        if pattern.fullmatch(pointer.card_rel)
        and _bound(pointer.data, b"Overflow in", pointer.card_rel, pointer.decision_id)
    ]
    return good_runs, good_pointers


def _find(file: bytes, spans: Sequence[PointerSpan], pointer: bytes) -> list[PointerSpan]:
    """The pointer spans whose bytes are exactly `pointer`, the line with its newline."""
    return [span for span in spans if file[span.start : span.end] == pointer]


def _swap_end(file: bytes, span: PointerSpan) -> int:
    """A pointer's span plus the one blank line a move left after it, when it is still there."""
    return span.end + 1 if file[span.end : span.end + 1] == b"\n" else span.end


def _put_back(file: bytes, span: PointerSpan, body: bytes) -> _Edit:
    """The edit that swaps a pointer for its section. A section moved from the end of a file with no final
    newline gets one back only when text now follows it, so it never runs into that text, while a restore
    at the end of the file stays byte for byte."""
    end = _swap_end(file, span)
    text = body if body.endswith(b"\n") or end >= len(file) else body + b"\n"
    return _Edit(span.start, end, text)


def _repeated(runs: Sequence[RecordedRun], pointers: Sequence[RecordedPointer]) -> set[bytes]:
    """Pointer bytes that more than one record claims. When such a pointer is in the file, each record would
    find the same span and two edits on one range would corrupt the result, so those records are refused or
    kept, never applied. Records whose shared pointer is gone edit nothing and are handled as usual."""
    seen = Counter([run.pointer for run in runs] + [pointer.data for pointer in pointers])
    return {data for data, count in seen.items() if count > 1}


def _apply(file: bytes, edits: Sequence[_Edit]) -> tuple[bytes, list[_Edit]]:
    """`file` with every edit applied. A removed overflow pointer that carried a separator takes the "\n" before
    it too, but only when that "\n" is then the file's last byte (row W29). Returns the edits as applied."""
    ordered = sorted(edits, key=lambda edit: edit.start)
    applied: list[_Edit] = []
    for index, edit in enumerate(ordered):
        start = edit.start
        if edit.separator and start > 0 and file[start - 1 : start] == b"\n":
            cursor = edit.end
            for later in ordered[index + 1 :]:
                if later.start != cursor or later.text:
                    break
                cursor = later.end
            covered = any(other.start <= start - 1 < other.end for other in ordered if other is not edit)
            if cursor == len(file) and not covered:
                start -= 1
        applied.append(_Edit(start, edit.end, edit.text))
    out: list[bytes] = []
    cursor = 0
    for edit in applied:
        out += [file[cursor : edit.start], edit.text]
        cursor = edit.end
    out.append(file[cursor:])
    return b"".join(out), applied


def _shift(applied: Sequence[_Edit], offset: int) -> int:
    """Where a byte of the old file lands in the new one."""
    return offset + sum(len(edit.text) - (edit.end - edit.start) for edit in applied if edit.end <= offset)


def _held(after: bytes, ranges: Sequence[tuple[int, int]]) -> tuple[str, ...]:
    digests: dict[str, None] = {}
    for unit in parse(after).units:
        # Any overlap, not only containment: a heading added above a pointer can make the restored text part of
        # a larger unit, and that unit must be held too, or the next pass moves it straight back out.
        if any(unit.start < end and start < unit.end for start, end in ranges):
            digests[unit.sha256] = None
    return tuple(digests)


def _unique(items: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(items))


def reconcile(
    file: bytes,
    runs: Sequence[RecordedRun],
    pointers: Sequence[RecordedPointer],
    cards: Mapping[str, bytes | None],
    *,
    file_key: str,
) -> Reconciled:
    """What a pass repairs before it plans anything: cards no pointer names whose text is back in the file,
    pointers whose card is gone, and overflow pointers whose card is gone."""
    good_runs, good_pointers = _trusted(file_key, runs, pointers)
    ignored = len(runs) - len(good_runs) + len(pointers) - len(good_pointers)
    doc = parse(file)
    if doc.problem is not None:
        return Reconciled(file, (), (), (), (), (), (), ignored)
    spans = pointer_spans(file)
    edits: list[_Edit] = []
    remove: list[str] = []
    restored: list[str] = []
    dropped: list[str] = []
    by_edit: list[str] = []
    kept: list[tuple[str, str]] = []
    repeated = _repeated(good_runs, good_pointers)
    for run in good_runs:
        rel = run.card_rel
        found = _find(file, spans, run.pointer)
        if run.pointer in repeated and any(not span.in_block for span in found):
            kept.append((rel, "pointer-duplicated"))
            continue
        card = cards.get(rel)
        if not found:
            if card is not None:
                body = card_body(card)
                if body is None or _sha(body) != run.section_sha256:
                    kept.append((rel, "card-changed"))
                elif whole_section_count(doc, body) >= 1:
                    remove.append(rel)
                else:
                    kept.append((rel, "pointer-missing"))  # the card holds the only copy
            elif whole_section_count(doc, run.section) == 0:
                by_edit.append(rel)  # pointer and card were both deleted: nothing left to put back
        elif len(found) > 1:
            kept.append((rel, "pointer-duplicated"))
        elif card is None:
            if found[0].in_block:
                kept.append((rel, "pointer-in-block"))
            elif _sha(run.section) != run.section_sha256:
                kept.append((rel, "lost"))
            else:
                edits.append(_put_back(file, found[0], run.section))
                restored.append(rel)
    for pointer in good_pointers:
        found = _find(file, spans, pointer.data)
        if cards.get(pointer.card_rel) is not None or not found:
            continue
        if pointer.data in repeated and any(not span.in_block for span in found):
            kept.append((pointer.card_rel, "pointer-duplicated"))
            continue
        outside = [span for span in found if not span.in_block]  # a copy inside a block is inert (clarification b)
        if len(outside) > 1:
            kept.append((pointer.card_rel, "pointer-duplicated"))
        elif not outside:
            kept.append((pointer.card_rel, "pointer-in-block"))
        else:
            edits.append(_Edit(outside[0].start, outside[0].end, b"", pointer.separator))
            dropped.append(pointer.card_rel)
    after, _ = _apply(file, edits)
    final = parse(after)
    final_spans = pointer_spans(after)
    completed: list[str] = []
    for decision in _unique([run.decision_id for run in good_runs] + [p.decision_id for p in good_pointers]):
        mine_runs = [run for run in good_runs if run.decision_id == decision]
        mine_pointers = [pointer for pointer in good_pointers if pointer.decision_id == decision]
        if any(_find(after, final_spans, run.pointer) for run in mine_runs):
            continue
        if any(not span.in_block for pointer in mine_pointers for span in _find(after, final_spans, pointer.data)):
            continue  # a recorded overflow pointer occurs only outside blocks
        if all(whole_section_count(final, run.section) >= 1 or run.card_rel in by_edit for run in mine_runs):
            completed.append(decision)
    return Reconciled(
        after=after,
        remove_cards=_unique(remove),
        restored=tuple(restored),
        dropped=tuple(dropped),
        completed=tuple(completed),
        removed_by_edit=_unique(by_edit),
        kept=tuple(dict.fromkeys(kept)),
        ignored=ignored,
    )


def plan_restore(
    file: bytes,
    runs: Sequence[RecordedRun],
    pointers: Sequence[RecordedPointer],
    cards: Mapping[str, bytes | None],
    *,
    file_key: str,
) -> Restore | Refusal:
    """The undo of one decision, planned from the live file and cards. It refuses, naming the card, rather than
    guess when a pointer or card was edited. A file whose markers do not balance cannot show where a block
    ends, so it refuses with `pointer-in-block` and no card."""
    good_runs, good_pointers = _trusted(file_key, runs, pointers)
    spans = pointer_spans(file)
    found_pointers = [(pointer, _find(file, spans, pointer.data)) for pointer in good_pointers]
    if not good_runs and not any(found for _, found in found_pointers):
        return Refusal("nothing-to-restore", None)
    doc = parse(file)
    if doc.problem is not None:
        return Refusal("pointer-in-block", None)
    repeated = _repeated(good_runs, good_pointers)
    for record_rel, data in [(run.card_rel, run.pointer) for run in good_runs] + [
        (pointer.card_rel, pointer.data) for pointer in good_pointers
    ]:
        if data in repeated and any(not span.in_block for span in _find(file, spans, data)):
            return Refusal("pointer-duplicated", record_rel)
    for pointer, found in found_pointers:
        outside = [span for span in found if not span.in_block]
        if len(outside) > 1:
            return Refusal("pointer-duplicated", pointer.card_rel)
        if found and not outside:
            return Refusal("pointer-in-block", pointer.card_rel)
    edits: list[_Edit] = []
    put_back: list[_Edit] = []  # each pointer span swapped for its card body
    back: list[tuple[int, int]] = []  # old offsets of each section found already back
    remove: list[str] = []
    for run in good_runs:
        rel = run.card_rel
        found = _find(file, spans, run.pointer)
        card = cards.get(rel)
        body = card_body(card) if card is not None else None
        if len(found) > 1:
            return Refusal("pointer-duplicated", rel)
        if found:
            if found[0].in_block:
                return Refusal("pointer-in-block", rel)
            if card is None:
                return Refusal("card-missing", rel)
            if body is None or _sha(body) != run.section_sha256:
                return Refusal("card-changed", rel)
            edit = _put_back(file, found[0], body)
            edits.append(edit)
            put_back.append(edit)
            remove.append(rel)
            continue
        sections = whole_section_spans(doc, run.section)
        if not sections:
            return Refusal("pointer-missing", rel)
        back.extend(sections)
        if body is not None and _sha(body) == run.section_sha256 and whole_section_count(doc, body) >= 1:
            remove.append(rel)
    held_cards: list[str] = []
    for pointer, found in found_pointers:
        outside = [span for span in found if not span.in_block]  # exactly one, checked above
        if outside:
            edits.append(_Edit(outside[0].start, outside[0].end, b"", pointer.separator))
            held_cards.append(pointer.card_rel)
    if good_runs and not edits and not remove:
        return Refusal("already-restored", None)
    after, applied = _apply(file, edits)
    ranges = [(_shift(applied, edit.start), _shift(applied, edit.start) + len(edit.text)) for edit in put_back]
    ranges += [(_shift(applied, start), _shift(applied, end)) for start, end in back]
    return Restore(after, _unique(remove), _held(after, ranges), tuple(held_cards))
