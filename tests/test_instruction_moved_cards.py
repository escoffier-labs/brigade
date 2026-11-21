"""The per-file moved-card cap (#2565): counting live cards and undoing across the cap."""

from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path

from brigade.instruction_moved_cards import count
from brigade.instruction_reconcile import RecordedRun, Restore, plan_restore
from brigade.instruction_sections import parse
from brigade.instruction_trim import MAX_MOVED_CARDS_PER_FILE, Limits, card_bytes, plan_append
from tests._owner_shape import owner_user_md

TODAY = date(2026, 11, 14)
ID = "20261114T061500Z-instructions-1a2b3c4d"
LIMITS = Limits(limit=4000, act_at=3400, trim_to=2800)
DIR = "memory/cards/instructions/"
CARD_ID = "card-0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"


def _plan(data: bytes, **changes):
    options = dict(
        file_key="user-md",
        limits=LIMITS,
        today=TODAY,
        decision_id=ID,
        taken=frozenset(),
        held=frozenset(),
        config_pins=(),
        overflow=(),
        max_moves=8,
        max_card_bytes=12_000,
    )
    return plan_append(parse(data), **{**options, **changes})


def _card(
    slug: str,
    section: bytes = b"## Elsewhere\nelsewhere\n",
    *,
    file_key: str = "user-md",
    source_name: str = "USER.md",
) -> bytes:
    return card_bytes(
        file_key=file_key,
        slug=slug,
        section=section,
        source_name=source_name,
        decision_id=ID,
        today=TODAY,
        fresh_until=None,
        card_id=CARD_ID,
    )


def _write(target: Path, rel: str, data: bytes) -> Path:
    path = target / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_count_reads_only_live_cards_that_carry_the_file_source(tmp_path):
    _write(tmp_path, DIR + "user-md--employment.md", _card("employment"))
    _write(tmp_path, DIR + "user-md--career.md", _card("career"))
    _write(tmp_path, DIR + "user-md--other-source.md", _card("other-source", source_name="AGENTS.md"))
    overflow = _card("overflow-preference").replace(
        b"tags: [moved-instructions, 'user-md', moved]", b"tags: [moved-instructions, 'user-md', overflow]"
    )
    _write(tmp_path, DIR + "user-md--overflow-preference.md", overflow)
    _write(tmp_path, DIR + "agents-md--employment.md", _card("employment", file_key="agents-md"))
    _write(tmp_path, DIR + "user-md--broken.md", b"no frontmatter here\n")
    assert count(tmp_path, file_key="user-md", source_name="USER.md") == 2


def test_a_deleted_card_frees_its_place(tmp_path):
    for index in range(MAX_MOVED_CARDS_PER_FILE):
        _write(tmp_path, DIR + f"user-md--section-{index}.md", _card(f"section-{index}"))
    assert count(tmp_path, file_key="user-md", source_name="USER.md") == MAX_MOVED_CARDS_PER_FILE
    (tmp_path / DIR / "user-md--section-7.md").unlink()
    assert count(tmp_path, file_key="user-md", source_name="USER.md") == MAX_MOVED_CARDS_PER_FILE - 1


def test_count_is_zero_without_a_card_folder(tmp_path):
    assert count(tmp_path, file_key="user-md", source_name="USER.md") == 0


def test_undo_of_an_earlier_move_restores_exactly_at_the_cap(tmp_path):
    data = owner_user_md()
    moved = _plan(data)
    assert len(moved.runs) == 3
    cards = {}
    for run in moved.runs:
        cards[run.card_rel] = _card(run.slug, moved.before[run.start : run.end])
        _write(tmp_path, run.card_rel, cards[run.card_rel])
    for index in range(MAX_MOVED_CARDS_PER_FILE - len(moved.runs)):
        _write(tmp_path, DIR + f"user-md--decoy-{index}.md", _card(f"decoy-{index}"))
    assert count(tmp_path, file_key="user-md", source_name="USER.md") == MAX_MOVED_CARDS_PER_FILE

    blocked = _plan(data, moved_cards=MAX_MOVED_CARDS_PER_FILE)
    assert (blocked.after, blocked.runs, blocked.reason) == (data, (), "moved_card_cap_reached")

    runs = tuple(
        RecordedRun(
            ID,
            run.card_rel,
            run.pointer[:-1],
            moved.before[run.start : run.end],
            hashlib.sha256(moved.before[run.start : run.end]).hexdigest(),
        )
        for run in moved.runs
    )
    restored = plan_restore(moved.after, runs, (), cards, file_key="user-md")
    assert isinstance(restored, Restore)
    assert restored.after == data
    assert restored.remove_cards == tuple(run.card_rel for run in moved.runs)
