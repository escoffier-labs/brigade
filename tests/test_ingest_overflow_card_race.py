"""Every invariant on an overflow append is enforced on the opened destination, under the lock, at the write.

A check made earlier, on a path name, or on only some paths is what each of these tests defeats: recovery of
a committed decision, two runs measuring the same file, a card filling between choosing and writing, a file
that is already hard linked, and a card that blocks the drafts behind it.
"""

from __future__ import annotations

import json
import os

import pytest

from brigade import ingest, instruction_overflow
from brigade import memory_care_journal as journal
from brigade import memory_inbox_drain as drain
from brigade.instruction_overflow import CARDS_REL, RouteContext, card_trust_problem
from brigade.memory_hub_sync import VISIBILITY_PRIVATE, withheld
from tests._home import set_home
from tests._instruction_fixtures import CARD_HEAD, field_workspace, handoff_text, sized, write_handoff
from tests.test_ingest_overflow_card_trust import (
    CARD,
    CARD_2,
    NOTE,
    PUBLIC,
    TODAY,
    _cap_cards,
    _drafts,
    _held,
    _ingest,
    _plant,
)

POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="POSIX hard links")
BLOCKED = ".brigade/memory-care/inbox/blocked-cards.json"


def _interrupt_first_publication(monkeypatch):
    """The first append of a published decision fails after the decision is committed (an interrupted publication)."""
    real = ingest._append_document_nofollow
    calls: list[int] = []

    def fail_once(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("interrupted")
        return real(*args, **kwargs)

    monkeypatch.setattr(ingest, "_append_document_nofollow", fail_once)
    return real


def _card_decision(ws):
    (decision,) = [d for d in journal.entries(ws) if d.reason.endswith("document-routed-to-card")]
    return journal._payload(ws, decision.decision_id)


def _act_at(ws, home, env):
    return instruction_overflow.find(RouteContext(ws, home, env).load()[0], "USER.md").act_at


# --- A1: recovery validates the exact opened destination before it writes ---------------------------


@pytest.mark.parametrize("existing", [False, True])
def test_recovery_never_appends_to_a_card_replaced_after_the_decision_committed(tmp_path, monkeypatch, existing):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    if existing:
        sized(ws / CARD, 200, head=CARD_HEAD)
    real = _interrupt_first_publication(monkeypatch)
    assert _ingest(ws) == 0
    (held,) = _drafts(ws)
    assert "document-destination-unwritable" in held.read_text()
    (ws / CARD).unlink(missing_ok=True)
    planted = _plant(ws, PUBLIC + "\nplanted\n")
    before = planted.read_bytes()
    monkeypatch.setattr(ingest, "_append_document_nofollow", real)
    recovery = journal.resolve_prepared(ws)
    assert planted.read_bytes() == before and NOTE not in planted.read_text()
    payload = _card_decision(ws)
    # A card that was missing is opened exclusively, so a file that appeared is refused as a changed state;
    # an existing card is read back first and refused as not one this feature created.
    assert payload["append_refused"].startswith(
        "document-destination-unwritable" if not existing else "overflow-card-untrusted"
    )
    assert not payload.get("appends_complete")
    assert [row["phase"] for row in recovery] == ["append-refused"]
    # Recovery does not try it again, and no card that holds handoff bytes would be published.
    assert journal.resolve_prepared(ws) == [] and planted.read_bytes() == before
    for card in (ws / CARDS_REL).glob("*.md"):
        if NOTE in card.read_text():
            assert withheld(card.read_text()) == VISIBILITY_PRIVATE
    # The handoff stays held: the drain re-evaluates it, finds the card still failing, and says which card.
    result = drain.drain(ws, today=TODAY, deadline_days=14)
    assert (result["retried"], result["held"], result["kept_for_person"]) == (0, 1, 0), result
    assert [(c["path"], c["held_drafts"]) for c in result["blocked_cards"]] == [(CARD, 1)]
    assert planted.read_bytes() == before and held.exists()


def test_recovery_refuses_a_card_that_changed_after_the_commit_and_the_handoff_lands_once(tmp_path, monkeypatch):
    # The card is still one this feature created, but it is not the bytes the decision pinned.
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    sized(ws / CARD, 200, head=CARD_HEAD)
    real = _interrupt_first_publication(monkeypatch)
    assert _ingest(ws) == 0
    with (ws / CARD).open("ab") as handle:
        handle.write(b"\n\nanother block, written by a later run\n")
    monkeypatch.setattr(ingest, "_append_document_nofollow", real)
    journal.resolve_prepared(ws)
    assert NOTE not in (ws / CARD).read_text()
    assert "overflow card changed" in _card_decision(ws)["append_refused"]
    assert drain.drain(ws, today=TODAY, deadline_days=14)["retried"] == 1
    assert (ws / CARD).read_text().count(NOTE) == 1
    assert drain.drain(ws, today=TODAY, deadline_days=14)["retried"] == 0


def test_a_refused_recovery_of_a_retried_draft_returns_the_draft_to_the_inbox(tmp_path, monkeypatch):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    draft = _held(ws)
    original = draft.read_bytes()
    real = _interrupt_first_publication(monkeypatch)
    first = drain.drain(ws, today=TODAY, deadline_days=14)
    assert first["skipped"] == 1 and not draft.exists()  # the draft moved aside when the decision committed
    planted = _plant(ws, PUBLIC + "\nplanted\n")
    before = planted.read_bytes()
    monkeypatch.setattr(ingest, "_append_document_nofollow", real)
    journal.resolve_prepared(ws)
    assert planted.read_bytes() == before
    assert draft.read_bytes() == original  # held again, byte for byte
    held = drain.drain(ws, today=TODAY, deadline_days=14)
    assert (held["retried"], held["held"]) == (0, 1) and planted.read_bytes() == before
    planted.unlink()
    assert drain.drain(ws, today=TODAY, deadline_days=14)["retried"] == 1
    assert (ws / CARD).read_text().count(NOTE) == 1 and not draft.exists()


def test_a_refused_recovery_of_a_cited_retry_makes_the_draft_pending_again(tmp_path, monkeypatch):
    # A cited draft stays where it is and a ledger row marks it handled. The refusal withdraws that row.
    from brigade import memory_evidence_log

    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    draft = _held(ws)
    rel = draft.relative_to(ws).as_posix()
    monkeypatch.setattr(memory_evidence_log, "read_evidence", lambda _: ([{"evidence_id": rel}], 0, False))
    real = _interrupt_first_publication(monkeypatch)
    assert drain.drain(ws, today=TODAY, deadline_days=14)["skipped"] == 1
    assert rel in drain._ledger(ws) and draft.exists() and draft not in drain.pending(ws)
    planted = _plant(ws, PUBLIC + "\nplanted\n")
    before = planted.read_bytes()
    monkeypatch.setattr(ingest, "_append_document_nofollow", real)
    journal.resolve_prepared(ws)
    assert planted.read_bytes() == before
    assert rel not in drain._ledger(ws) and draft in drain.pending(ws)


def test_a_refused_new_destination_leaves_no_empty_file_behind(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    target = ws / "notes.md"
    big = ingest.AppendExpect(created=True, max_size=3)
    with pytest.raises(ingest.AppendRefused) as refusal:
        ingest._append_document_nofollow(ws, target, "\n\nmore than three bytes\n", expect=big)
    assert refusal.value.over_limit and not target.exists()
    # and an exclusive create refuses a file that appeared since it was decided
    target.write_text("someone else's\n")
    with pytest.raises(ingest.AppendRefused):
        ingest._append_document_nofollow(ws, target, "\n\nx\n", expect=ingest.AppendExpect(created=True))
    assert target.read_text() == "someone else's\n"


def test_the_write_rule_checks_state_trust_and_size_on_the_bytes_it_is_given():
    block = b"\n\nnote\n"
    card = CARD_HEAD.encode() + b"\nbody\n"
    rule = instruction_overflow.write_problem
    assert rule(card, block, created=False, card_limit=len(card) + len(block)) is None
    full = rule(card, block, created=False, card_limit=len(card) + len(block) - 1)
    assert full is not None and full.full
    assert rule(None, block, created=False, card_limit=None).code == "document-destination-unwritable"
    assert rule(card, block, created=True, card_limit=None).code == "document-destination-unwritable"
    assert rule(PUBLIC.encode(), block, created=False, card_limit=None).code == "overflow-card-untrusted"


# --- A2: size routing under the lock ----------------------------------------------------------------


def _decide_then_fill(monkeypatch, ws, filler_to):
    """Route once for real, then grow USER.md as a second ingest run would have between decide and the write."""
    real = instruction_overflow.route
    state = {"calls": 0}

    def route(*args, **kwargs):
        state["calls"] += 1
        found = real(*args, **kwargs)
        if state["calls"] == 1:
            assert found is None  # decide() saw room
            sized(ws / "USER.md", filler_to)
        return found

    monkeypatch.setattr(instruction_overflow, "route", route)
    return state


def test_a_file_filled_between_decide_and_the_locked_write_goes_to_the_card_in_the_same_run(tmp_path, monkeypatch):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    limit = _act_at(ws, home, env)
    sized(ws / "USER.md", limit - 100)
    _decide_then_fill(monkeypatch, ws, limit - 10)
    assert _ingest(ws) == 0
    assert (ws / "USER.md").stat().st_size == limit - 10 <= limit
    assert (ws / "USER.md").read_text().count(NOTE) == 0
    assert (ws / CARD).read_text().count(NOTE) == 1
    assert not _drafts(ws)


def test_the_sink_holds_the_limit_when_the_locked_measurement_also_missed_the_fill(tmp_path, monkeypatch):
    # decide() and the locked route both see room; the file fills before the open. Only the opened file knows.
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    limit = _act_at(ws, home, env)
    sized(ws / "USER.md", limit - 100)
    real = instruction_overflow.route
    state = {"calls": 0}

    def route(*args, **kwargs):
        state["calls"] += 1
        if state["calls"] == 2:  # the locked route: say it fits, then the file fills before the open
            sized(ws / "USER.md", limit - 10)
            return None
        return real(*args, **kwargs)

    monkeypatch.setattr(instruction_overflow, "route", route)
    assert _ingest(ws) == 0
    assert (ws / "USER.md").stat().st_size == limit - 10 <= limit
    assert (ws / CARD).read_text().count(NOTE) == 1 and not _drafts(ws)


def test_two_handoffs_in_one_run_never_cross_act_at(tmp_path, monkeypatch):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    limit = _act_at(ws, home, env)
    sized(ws / "USER.md", limit - 50)  # room for one note (34 bytes), not two
    for index in range(2):
        write_handoff(ws, f"2026-11-13-{index}.md", handoff_text("USER.md", f"{NOTE} {index}", type_="preference"))
    assert ingest.run(target=ws, promote_cards=True, route_documents=True) == 0
    assert (ws / "USER.md").stat().st_size <= limit
    landed = (ws / "USER.md").read_text() + (ws / CARD).read_text()
    assert [landed.count(f"{NOTE} {index}") for index in range(2)] == [1, 1]


def test_a_card_filled_between_choosing_and_writing_rolls_to_the_next_card(tmp_path, monkeypatch):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    _cap_cards(ws, 600)
    sized(ws / CARD, 300, head=CARD_HEAD)
    real = instruction_overflow.refresh
    state = {"calls": 0}

    def refresh(*args, **kwargs):
        found = real(*args, **kwargs)
        state["calls"] += 1
        if state["calls"] == 1:
            assert found.card_rel == CARD  # chosen with room, by the size measured now
            sized(ws / CARD, 590, head=CARD_HEAD)  # a writer that does not take our lock fills it
        return found

    monkeypatch.setattr(instruction_overflow, "refresh", refresh)
    assert _ingest(ws) == 0
    assert (ws / CARD).stat().st_size == 590 <= 600 and NOTE not in (ws / CARD).read_text()
    assert (ws / CARD_2).read_text().count(NOTE) == 1 and not _drafts(ws)


def test_a_retry_never_writes_to_a_card_that_filled_after_it_was_chosen(tmp_path, monkeypatch):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    _cap_cards(ws, 600)
    sized(ws / CARD, 300, head=CARD_HEAD)
    draft = _held(ws)
    real = instruction_overflow.refresh
    state = {"calls": 0}

    def refresh(*args, **kwargs):
        found = real(*args, **kwargs)
        state["calls"] += 1
        if state["calls"] == 1:
            sized(ws / CARD, 590, head=CARD_HEAD)
        return found

    monkeypatch.setattr(instruction_overflow, "refresh", refresh)
    first = drain.drain(ws, today=TODAY, deadline_days=14)
    assert first["retried"] == 0 and (ws / CARD).stat().st_size == 590 and draft.exists()
    assert drain.drain(ws, today=TODAY, deadline_days=14)["retried"] == 1  # the next drain chooses card 2
    assert (ws / CARD_2).read_text().count(NOTE) == 1 and (ws / CARD).stat().st_size == 590


def test_the_retry_of_a_direct_append_measures_the_file_under_the_lock(tmp_path, monkeypatch):
    # A draft held for another reason, with room in USER.md when it was decided and none by the write.
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    limit = _act_at(ws, home, env)
    sized(ws / "USER.md", limit - 100)
    source = write_handoff(ws, "2026-10-01-0.md", handoff_text("USER.md", NOTE, type_="preference"))
    ingest._execute(
        ingest.Outcome("inboxed", code="document-destination-unwritable"),
        source,
        ws,
        {},
        ws / drain.INBOX_REL,
        source.parent / "processed",
        False,
        snapshot=source.read_bytes(),
        promote_cards=True,
        route_documents=True,
    )
    real = instruction_overflow.route
    state = {"calls": 0}

    def route(*args, **kwargs):
        found = real(*args, **kwargs)
        state["calls"] += 1
        if state["calls"] == 1:
            sized(ws / "USER.md", limit - 10)
        return found

    monkeypatch.setattr(instruction_overflow, "route", route)
    assert drain.drain(ws, today=TODAY, deadline_days=14)["retried"] == 1
    assert (ws / "USER.md").stat().st_size == limit - 10 and (ws / CARD).read_text().count(NOTE) == 1


# --- A3: an already hard linked instruction file is supported; a swap is not ------------------------


@POSIX_ONLY
def test_an_unchanged_hard_linked_instruction_file_still_takes_a_direct_append(tmp_path, monkeypatch):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    sized(ws / "USER.md", 100)
    second = ws / "USER-copy.md"
    os.link(ws / "USER.md", second)
    assert _ingest(ws) == 0
    assert not _drafts(ws)
    assert NOTE in (ws / "USER.md").read_text() and (ws / "USER.md").read_bytes() == second.read_bytes()


@POSIX_ONLY
def test_a_pinned_identity_that_no_longer_matches_is_refused_even_for_a_linked_file(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    doc = ws / "notes.md"
    doc.write_bytes(b"x" * 10)
    os.link(doc, ws / "alias.md")
    info = doc.stat()
    ingest._append_document_nofollow(
        ws, doc, "\n\nok\n", expect=ingest.AppendExpect(created=False, identity=(info.st_dev, info.st_ino))
    )
    with pytest.raises(ingest.AppendRefused):
        ingest._append_document_nofollow(
            ws, doc, "\n\nno\n", expect=ingest.AppendExpect(created=False, identity=(info.st_dev, info.st_ino + 1))
        )
    assert doc.read_bytes().endswith(b"ok\n") and b"no" not in doc.read_bytes()[-5:]


# --- A4: the marker tag is an exact member ---------------------------------------------------------


@pytest.mark.parametrize(
    "tags,ok",
    [
        ("[moved-instructions, user-md, overflow]", True),
        ("moved-instructions, overflow", True),
        ("moved-instructions", True),
        ("['moved-instructions', overflow]", True),
        ("[not-moved-instructions]", False),
        ("[moved-instructions-extra, overflow]", False),
        ("not-moved-instructions", False),
        ("[premoved-instructions]", False),
    ],
)
def test_the_provenance_check_reads_the_tags_not_a_substring(tags, ok):
    text = CARD_HEAD.replace("tags: [moved-instructions, user-md, overflow]", f"tags: {tags}") + "\nbody\n"
    assert (card_trust_problem(text) is None) is ok


# --- A5: a blocked card is recorded, and the drain delivers once it passes -------------------------


def test_ingest_records_each_untrusted_card_and_the_drafts_waiting_on_it(tmp_path, monkeypatch, capsys):
    from brigade import instruction_overflow_blocked

    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    _plant(ws, PUBLIC + "\nplanted\n")
    for index in range(2):
        write_handoff(ws, f"2026-11-13-{index}.md", handoff_text("USER.md", f"{NOTE} {index}", type_="preference"))
    stats = ingest.IngestStats()
    assert ingest.ingest_into(source=ws, owner=ws, stats=stats, promote_cards=True, route_documents=True) == 0
    assert stats.inboxed == 2
    (card,) = stats.blocked_cards
    assert (card["path"], card["held_drafts"]) == (CARD, 2) and "visibility" in card["problem"]
    assert json.loads((ws / BLOCKED).read_text()) == {"version": 1, "cards": stats.blocked_cards}
    assert instruction_overflow_blocked.load(ws) == stats.blocked_cards
    ingest._report(stats, dry_run=False)
    assert f"blocked overflow card {CARD}: 2 draft(s) held" in capsys.readouterr().out


def test_the_record_holds_paths_and_counts_never_handoff_content(tmp_path, monkeypatch):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    _plant(ws, PUBLIC + "\nplanted\n")
    assert _ingest(ws) == 0
    assert NOTE not in (ws / BLOCKED).read_text()


@pytest.mark.parametrize("repair", ["remove", "repair"])
def test_the_drain_delivers_the_held_drafts_once_the_card_passes(tmp_path, monkeypatch, repair):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    planted = _plant(ws, PUBLIC + "\nplanted\n")
    for index in range(2):
        write_handoff(ws, f"2026-11-13-{index}.md", handoff_text("USER.md", f"{NOTE} {index}", type_="preference"))
    assert ingest.run(target=ws, promote_cards=True, route_documents=True) == 0
    assert len(_drafts(ws)) == 2
    # Still failing: held every drain, nobody is asked to do anything, the record says which card.
    for _ in range(2):
        result = drain.drain(ws, today=TODAY, deadline_days=14)
        assert (result["retried"], result["held"], result["kept_for_person"]) == (0, 2, 0), result
        assert [(c["path"], c["held_drafts"]) for c in result["blocked_cards"]] == [(CARD, 2)]
    assert len(_drafts(ws)) == 2
    if repair == "remove":
        planted.unlink()
    else:
        planted.write_text(CARD_HEAD + "\nkept by a person\n")
    result = drain.drain(ws, today=TODAY, deadline_days=14)
    assert (result["retried"], result["held"], result["kept_for_person"]) == (2, 0, 0), result
    assert result["blocked_cards"] == [] and json.loads((ws / BLOCKED).read_text())["cards"] == []
    text = (ws / CARD).read_text()
    assert [text.count(f"{NOTE} {index}") for index in range(2)] == [1, 1] and not _drafts(ws)


# --- B: a card routing may choose is a card retry can read ----------------------------------------


def test_retry_reads_a_card_larger_than_a_draft_may_be(tmp_path, monkeypatch):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    _cap_cards(ws, 600_000)  # above the 256 KiB a draft may be, under what the duplicate scan reads
    sized(ws / CARD, 300_000, head=CARD_HEAD)
    draft = _held(ws)
    assert not (ws / CARD_2).exists()
    result = drain.drain(ws, today=TODAY, deadline_days=14)
    assert result["retried"] == 1, result
    assert (ws / CARD).read_text().endswith(f"\n{NOTE}\n") and not draft.exists() and not (ws / CARD_2).exists()


def test_an_interrupted_refusal_has_already_returned_the_draft(tmp_path, monkeypatch):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    draft = _held(ws)
    original = draft.read_bytes()
    real = _interrupt_first_publication(monkeypatch)
    assert drain.drain(ws, today=TODAY, deadline_days=14)["skipped"] == 1 and not draft.exists()
    planted = _plant(ws, PUBLIC + "\nplanted\n")
    before = planted.read_bytes()
    monkeypatch.setattr(ingest, "_append_document_nofollow", real)
    annotate, crashed = journal.annotate, []

    def crash_after_recording(target, decision_id, **fields):
        annotate(target, decision_id, **fields)
        if "append_refused" in fields and not crashed:
            crashed.append(1)
            raise RuntimeError("interrupted right after the refusal was recorded")

    monkeypatch.setattr(journal, "annotate", crash_after_recording)
    try:
        journal.resolve_prepared(ws)
    except RuntimeError:
        pass
    assert crashed, "the refusal path was not reached"
    # Rule: the draft is back before the refusal is recorded, so an interruption cannot lose it.
    assert draft.read_bytes() == original and planted.read_bytes() == before
    monkeypatch.setattr(journal, "annotate", annotate)
    journal.resolve_prepared(ws)
    planted.unlink()
    assert drain.drain(ws, today=TODAY, deadline_days=14)["retried"] == 1
    assert (ws / CARD).read_text().count(NOTE) == 1 and not draft.exists()
