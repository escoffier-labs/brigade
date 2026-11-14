"""Reconcile and the restore plan, from bytes alone (matrix W13 to W16, W27, W29, P3 to P5, P13, P14, P17, P24, P27, S10, S18)."""

from __future__ import annotations

import dataclasses
import hashlib
from datetime import date
from typing import NamedTuple

from brigade.instruction_reconcile import (
    RecordedPointer,
    RecordedRun,
    Refusal,
    Restore,
    card_body,
    plan_restore,
    reconcile,
)
from brigade.instruction_sections import KEEP, parse
from brigade.instruction_trim import Limits, card_bytes, plan_append, pointer_line
from tests._owner_shape import owner_user_md

TODAY = date(2026, 11, 14)
ID = "20261114T061500Z-instructions-1a2b3c4d"
LIMITS = Limits(limit=4000, act_at=3400, trim_to=2800)
DIR = "memory/cards/instructions/"
PREF = DIR + "user-md--overflow-preference.md"
KEY = "user-md"


class Moved(NamedTuple):
    before: bytes
    after: bytes
    runs: tuple[RecordedRun, ...]
    pointers: tuple[RecordedPointer, ...]
    cards: dict[str, bytes | None]


def _moved(data: bytes | None = None, *, overflow: tuple[str, ...] = ()) -> Moved:
    """Task 2's plan on the owner file, recorded the way the stage records a move."""
    data = owner_user_md() if data is None else data
    plan = plan_append(
        parse(data),
        file_key=KEY,
        limits=LIMITS,
        today=TODAY,
        decision_id=ID,
        taken=frozenset(),
        held=frozenset(),
        config_pins=(),
        overflow=overflow,
        max_moves=8,
        max_card_bytes=12_000,
    )
    runs: list[RecordedRun] = []
    cards: dict[str, bytes | None] = {}
    for run in plan.runs:
        section = plan.before[run.start : run.end]
        runs.append(RecordedRun(ID, run.card_rel, run.pointer[:-1], section, hashlib.sha256(section).hexdigest()))
        cards[run.card_rel] = card_bytes(
            file_key=KEY,
            slug=run.slug,
            section=section,
            source_name="USER.md",
            decision_id=ID,
            today=TODAY,
            fresh_until=None,
            card_id="c-1",
        )
    pointers = tuple(RecordedPointer(ID, a.card_rel, a.data, a.separator) for a in plan.added)
    return Moved(data, plan.after, tuple(runs), pointers, cards)


def _reconcile(file, moved, cards=None, *, runs=None, pointers=None):
    return reconcile(
        file,
        moved.runs if runs is None else runs,
        moved.pointers if pointers is None else pointers,
        moved.cards if cards is None else cards,
        file_key=KEY,
    )


def _restore(file, moved, cards=None, *, runs=None, pointers=None):
    return plan_restore(
        file,
        moved.runs if runs is None else runs,
        moved.pointers if pointers is None else pointers,
        moved.cards if cards is None else cards,
        file_key=KEY,
    )


def _swap(run: RecordedRun) -> bytes:
    """The bytes a move left in the file for `run`: its pointer line and the blank line after it."""
    return run.pointer + b"\n"


def test_card_body_is_what_follows_the_first_frontmatter_block():
    assert card_body(b"---\ntitle: a\n---\nbody\n---\nmore\n") == b"body\n---\nmore\n"
    assert card_body(b"---\ntitle: a\n---\n") == b""
    assert card_body(b"no frontmatter\n") is None and card_body(b"---\nnever closed\n") is None


def test_missing_card_puts_its_section_back():  # matrix W15
    moved = _moved()
    cards = {**moved.cards, moved.runs[1].card_rel: None}
    result = _reconcile(moved.after, moved, cards)
    assert result.after == moved.after.replace(_swap(moved.runs[1]), moved.runs[1].section)
    assert (result.restored, result.remove_cards, result.completed) == ((moved.runs[1].card_rel,), (), ())
    both = {**cards, moved.runs[2].card_rel: None}
    result = _reconcile(moved.after, moved, both)  # two swaps in one pass keep their offsets
    expected = moved.after.replace(_swap(moved.runs[1]), moved.runs[1].section)
    assert result.after == expected.replace(_swap(moved.runs[2]), moved.runs[2].section)


def test_edited_card_body_is_kept_and_refused():  # matrix W14
    moved = _moved()
    rel = moved.runs[0].card_rel
    edited = {**moved.cards, rel: moved.cards[rel] + b"an agent added this\n"}
    assert _restore(moved.after, moved, edited) == Refusal("card-changed", rel)
    result = _reconcile(moved.before, moved, edited)  # the text is back and the card was edited
    assert rel not in result.remove_cards and (rel, "card-changed") in result.kept
    assert set(result.remove_cards) == {moved.runs[1].card_rel, moved.runs[2].card_rel}


def test_edited_pointer_refuses_and_keeps_the_card():  # matrix W16
    moved = _moved()
    edited = moved.after.replace(b"2026-11-14", b"2026-11-15", 1)
    assert _restore(edited, moved) == Refusal("pointer-missing", moved.runs[0].card_rel)
    result = _reconcile(edited, moved)
    assert (result.after, result.remove_cards) == (edited, ())
    assert (moved.runs[0].card_rel, "pointer-missing") in result.kept


def test_replaced_card_is_never_read_back():  # matrix P3, S10
    moved = _moved()
    rel = moved.runs[0].card_rel
    forged = {**moved.cards, rel: b"---\ntitle: x\n---\nEVIL TEXT\n"}
    assert _restore(moved.after, moved, forged) == Refusal("card-changed", rel)
    result = _reconcile(moved.after, moved, forged)
    assert b"EVIL" not in result.after and result.after == moved.after
    result = _reconcile(moved.before, moved, forged)
    assert rel not in result.remove_cards and (rel, "card-changed") in result.kept


def test_pointer_shaped_line_is_inert():  # matrix P4
    moved = _moved()
    inert = pointer_line(DIR + "user-md--other.md", today=TODAY, decision_id=ID)
    file = moved.after + inert
    assert _reconcile(file, moved).after == file
    restored = _restore(file, moved)
    assert isinstance(restored, Restore) and inert in restored.after
    assert restored.after == moved.before + inert


def test_duplicated_pointer_refuses():  # matrix P5
    moved = _moved()
    rel = moved.runs[0].card_rel
    doubled = moved.after + moved.runs[0].pointer
    assert _restore(doubled, moved) == Refusal("pointer-duplicated", rel)
    result = _reconcile(doubled, moved, {**moved.cards, rel: None})
    assert result.after == doubled and (rel, "pointer-duplicated") in result.kept


def test_duplicated_overflow_pointer_refuses_restore():  # matrix P5
    moved = _moved(overflow=(PREF,))
    doubled = moved.after + moved.pointers[0].data
    assert _restore(doubled, moved) == Refusal("pointer-duplicated", PREF)
    result = _reconcile(doubled, moved, {**moved.cards, PREF: None})
    assert result.dropped == () and result.after == doubled


def test_overflow_pointer_only_in_a_block_refuses_restore():  # matrix P17
    moved = _moved(overflow=(PREF,))
    data = moved.pointers[0].data
    hidden = moved.after.removesuffix(data) + b"```\n" + data + b"```\n"
    assert _restore(hidden, moved) == Refusal("pointer-in-block", PREF)
    result = _reconcile(hidden, moved, {**moved.cards, PREF: None})
    assert (result.dropped, result.after) == ((), hidden)


def test_overflow_pointer_removed_when_its_card_is_gone():  # matrix W13
    moved = _moved(overflow=(PREF,))
    result = _reconcile(moved.after, moved, {**moved.cards, PREF: None})
    assert result.dropped == (PREF,)
    assert result.after == moved.after.removesuffix(moved.pointers[0].data)
    assert _reconcile(moved.after, moved, {**moved.cards, PREF: b"card"}).dropped == ()


def test_restore_is_exact_when_nothing_changed():
    moved = _moved()
    result = _restore(moved.after, moved)
    assert isinstance(result, Restore) and result.after == moved.before == owner_user_md()
    assert result.remove_cards == tuple(run.card_rel for run in moved.runs)
    with_overflow = _moved(overflow=(PREF,))
    result = _restore(with_overflow.after, with_overflow)
    assert isinstance(result, Restore) and result.after == with_overflow.before
    assert result.held_cards == (PREF,)


def test_restore_keeps_later_edits():
    moved = _moved()
    edited = moved.after.replace(b"## Voice", b"INSERTED\n## Voice", 1) + b"appended later\n"
    result = _restore(edited, moved)
    assert isinstance(result, Restore)
    assert result.after == moved.before.replace(b"## Voice", b"INSERTED\n## Voice", 1) + b"appended later\n"


def test_restore_follows_a_moved_pointer():
    moved = _moved()
    cut = moved.after.replace(_swap(moved.runs[1]), b"", 1) + moved.runs[1].pointer
    result = _restore(cut, moved)
    assert isinstance(result, Restore)
    assert result.after.endswith(moved.runs[1].section)
    assert moved.runs[0].section in result.after and b"Moved to memory" not in result.after


def test_restore_of_a_pointer_only_decision():
    small = owner_user_md(sections=(("Voice & Communication", 651), ("Preferences (core)", 510)))
    moved = _moved(small, overflow=(PREF,))
    assert moved.runs == () and len(moved.pointers) == 1
    result = _restore(moved.after, moved)
    assert result == Restore(after=small, remove_cards=(), held=(), held_cards=(PREF,))


def test_renamed_card_counts_as_missing():
    moved = _moved()
    run = moved.runs[0]
    renamed = {k: v for k, v in moved.cards.items() if k != run.card_rel}
    renamed[DIR + "user-md--renamed.md"] = moved.cards[run.card_rel]
    result = _reconcile(moved.after, moved, renamed)
    assert result.restored == (run.card_rel,)
    assert _restore(result.after, moved, renamed, runs=(run,)) == Refusal("already-restored", None)


def test_partly_restored_file_finishes_the_restore():
    moved = _moved()
    partly = moved.after.replace(_swap(moved.runs[0]), moved.runs[0].section)
    result = _reconcile(partly, moved)
    assert (result.remove_cards, result.completed, result.after) == ((moved.runs[0].card_rel,), (), partly)
    final = _restore(partly, moved)
    assert isinstance(final, Restore) and final.after == owner_user_md()
    assert final.remove_cards == tuple(run.card_rel for run in moved.runs)


def test_completed_lists_a_decision_whose_text_is_all_back():
    moved = _moved()
    result = _reconcile(moved.before, moved)
    assert result.completed == (ID,) and result.remove_cards == tuple(run.card_rel for run in moved.runs)
    assert result.after == moved.before


def test_record_card_outside_the_folder_is_ignored():  # matrix P13
    moved = _moved()
    outside = dataclasses.replace(moved.runs[0], card_rel="memory/cards/keep.md")
    other = dataclasses.replace(moved.runs[1], card_rel=DIR + "other--x.md")
    stray = RecordedPointer(ID, "memory/cards/keep.md", moved.runs[0].pointer)
    cards = {
        "memory/cards/keep.md": moved.cards[moved.runs[0].card_rel],
        DIR + "other--x.md": moved.cards[moved.runs[1].card_rel],
    }
    result = _reconcile(moved.before, moved, cards, runs=(outside, other), pointers=(stray,))
    assert (result.remove_cards, result.restored, result.dropped, result.ignored) == ((), (), (), 3)
    assert _restore(moved.before, moved, cards, runs=(outside, other), pointers=()) == Refusal(
        "nothing-to-restore", None
    )


def test_section_that_fails_its_digest_is_never_restored():  # matrix P14
    moved = _moved()
    run = dataclasses.replace(moved.runs[0], section=moved.runs[0].section + b"FORGED\n")
    rel = run.card_rel
    result = _reconcile(moved.after, moved, {**moved.cards, rel: None}, runs=(run, *moved.runs[1:]))
    assert b"FORGED" not in result.after and result.after == moved.after
    assert (rel, "lost") in result.kept and result.completed == ()
    restored = _restore(moved.after, moved, {**moved.cards, rel: None}, runs=(run, *moved.runs[1:]))
    assert restored == Refusal("card-missing", rel)


def test_pointer_in_a_fence_or_managed_block_is_refused():  # matrix P17
    moved = _moved()
    run = moved.runs[0]
    stripped = moved.after.replace(_swap(run), b"", 1)
    for wrapped in (b"```\n" + run.pointer + b"```\n", b"<!-- X:START -->\n" + run.pointer + b"<!-- X:END -->\n"):
        file = stripped + wrapped
        assert _restore(file, moved) == Refusal("pointer-in-block", run.card_rel)
        result = _reconcile(file, moved, {**moved.cards, run.card_rel: None})
        assert result.after == file and (run.card_rel, "pointer-in-block") in result.kept


def test_card_text_inside_a_larger_section_keeps_the_card():  # matrix S18
    moved = _moved()
    run = moved.runs[2]
    fenced = b"## Z\n```\n" + run.section + b"```\n"
    for file in (fenced, run.section + b"one more line\n"):
        result = _reconcile(file, moved, runs=(run,))
        assert result.remove_cards == () and result.completed == ()
        assert (run.card_rel, "pointer-missing") in result.kept


def test_restore_holds_every_unit_of_a_multi_section_run():
    moved = _moved()
    result = _restore(moved.after, moved)
    units = parse(moved.before).units
    assert isinstance(result, Restore)
    assert set(result.held) == {units[i].sha256 for i in (0, 1, 2, 3, 5, 6, 8)}
    assert len(result.held) == 7


def test_cards_only_restore_when_every_section_is_back():  # matrix W27
    moved = _moved()
    result = _restore(moved.before, moved)
    assert isinstance(result, Restore) and result.after == moved.before
    assert result.remove_cards == tuple(run.card_rel for run in moved.runs)
    assert len(result.held) == 7
    assert _restore(moved.before, moved, {}) == Refusal("already-restored", None)


def test_pointer_then_card_deleted_completes_the_run():  # matrix P24
    moved = _moved()
    run = moved.runs[0]
    file = moved.after.replace(_swap(run), b"", 1)
    result = _reconcile(file, moved, {**moved.cards, run.card_rel: None}, runs=(run,))
    assert (result.removed_by_edit, result.completed, result.after) == ((run.card_rel,), (ID,), file)
    assert result.restored == () and result.remove_cards == ()


def test_move_with_live_overflow_pointer_is_not_completed():  # matrix P27
    moved = _moved(overflow=(PREF,))
    file = moved.before + moved.pointers[0].data
    live = _reconcile(file, moved, {**moved.cards, PREF: b"card"})
    assert live.completed == () and live.remove_cards == tuple(run.card_rel for run in moved.runs)
    gone = _reconcile(file, moved, {**moved.cards, PREF: None})
    assert (gone.dropped, gone.completed, gone.after) == ((PREF,), (ID,), moved.before)
    # Sections back, run cards already removed, the overflow card still there: undo removes the pointer.
    result = _restore(file, moved, {PREF: b"card"})
    assert isinstance(result, Restore)
    assert (result.after, result.remove_cards, result.held_cards) == (moved.before, (), (PREF,))
    assert len(result.held) == 7


def test_overflow_separator_removed_with_its_pointer():  # matrix W29
    data = b"# T\n\n## V\n" + KEEP.encode() + b"\nbody text with no final newline"
    moved = _moved(data, overflow=(PREF,))
    assert moved.pointers[0].separator and moved.after == data + b"\n" + moved.pointers[0].data
    assert _reconcile(moved.after, moved, {PREF: None}).after == data
    later = moved.after + b"more\n"
    assert _reconcile(later, moved, {PREF: None}).after == data + b"\nmore\n"
    result = _restore(moved.after, moved, {PREF: b"card"})
    assert isinstance(result, Restore) and result.after == data


def test_two_cards_with_one_body_are_both_removed_when_the_text_is_present():
    moved = _moved()
    run = moved.runs[2]
    twin = dataclasses.replace(run, card_rel=DIR + "user-md--twin.md")
    cards = {**moved.cards, twin.card_rel: moved.cards[run.card_rel]}
    result = _reconcile(moved.before, moved, cards, runs=(run, twin))
    assert set(result.remove_cards) == {run.card_rel, twin.card_rel}


def test_a_problem_file_is_left_alone():
    moved = _moved()
    broken = moved.after + b"<!-- X:START -->\n"
    result = _reconcile(broken, moved, {k: None for k in moved.cards})
    assert (result.after, result.restored, result.remove_cards) == (broken, (), ())
    assert _restore(broken, moved) == Refusal("pointer-in-block", None)


def test_nothing_to_restore_without_records_or_pointers():
    moved = _moved()
    assert _restore(moved.after, moved, runs=(), pointers=()) == Refusal("nothing-to-restore", None)
    pointer = RecordedPointer(ID, PREF, pointer_line(PREF, today=TODAY, decision_id=ID, overflow=True))
    assert _restore(moved.before, moved, {}, runs=(), pointers=(pointer,)) == Refusal("nothing-to-restore", None)


def test_overflow_pointer_copy_in_a_block_does_not_keep_the_move_open():  # clarification (b) on P27
    moved = _moved(overflow=(PREF,))
    data = moved.pointers[0].data
    fixed = parse(moved.before).fixed_end  # a fence in the fixed text leaves every section as it was
    fenced = moved.before[:fixed] + b"```\n" + data + b"```\n" + moved.before[fixed:]
    result = _reconcile(fenced, moved, {**moved.cards, PREF: b"card"})
    assert result.completed == (ID,) and result.after == fenced
    # One live copy and one block copy: the live one goes, the inert one stays.
    both = fenced + data
    result = _reconcile(both, moved, {**moved.cards, PREF: None})
    assert result.dropped == (PREF,)
    assert result.after == fenced and result.completed == (ID,)


def test_restore_removes_the_live_overflow_pointer_not_a_fenced_copy():  # matrix P17, review of #2567
    moved = _moved(overflow=(PREF,))
    data = moved.pointers[0].data
    fixed = parse(moved.before).fixed_end
    fenced = moved.before[:fixed] + b"```\n" + data + b"```\n" + moved.before[fixed:]
    result = _restore(fenced + data, moved, {}, runs=(), pointers=moved.pointers)
    assert not isinstance(result, Refusal)
    assert result.after == fenced and result.held_cards == (PREF,)


def test_records_sharing_pointer_bytes_are_refused_and_kept():  # forged duplicate records, review of #2567
    moved = _moved()
    run = moved.runs[0]
    twin = dataclasses.replace(run, card_rel=run.card_rel.replace(".md", "-twin.md"))
    runs = (run, twin)
    assert _restore(moved.after, moved, runs=runs) == Refusal("pointer-duplicated", run.card_rel)
    result = _reconcile(moved.after, moved, {**moved.cards, run.card_rel: None}, runs=runs)
    assert result.after == moved.after
    assert (run.card_rel, "pointer-duplicated") in result.kept and (twin.card_rel, "pointer-duplicated") in result.kept
