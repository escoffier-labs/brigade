"""The hub copy of a card id follows the local cards still in use.

R1: the hub never shares the text of a card that is archived here.
R2: while any live local card holds the id, and this workspace published it, the hub shares it.
"""

import pytest

from brigade import memory_archive as archive, memory_care_hub as hub, memory_care_journal as journal
from brigade import memory_care_tend as tend, memory_hub_store as store
from tests.test_memory_archive import setup_card
from tests.test_memory_archive_review import configure_hub, move

A = "memory/cards/old.md"
B = "memory/cards/other.md"
C = "memory/cards/third.md"


def card(target, rel, body="Body", visibility=None):
    path = target / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    marker = f"visibility: {visibility}\n" if visibility else ""
    path.write_bytes(f"---\ncard_id: old\n{marker}---\n{body}\n".encode())
    return path


def record(target, fake):
    with store.locked(target):
        return store.published(target)[fake.identity]["fleet"]["old"]


def set_record(target, fake, **fields):
    with store.locked(target):
        state = store.published(target)
        state[fake.identity]["fleet"]["old"] = {"revision": 1, "log_epoch": "epoch", **fields}
        store.save_published(target, state)


def sent(fake, action):
    return [f for a, f in fake.calls if a == action]


def two_cards(tmp_path, monkeypatch, owner=A):
    setup_card(tmp_path, b"---\ncard_id: old\n---\nA text\n")
    card(tmp_path, B, "B text")
    return configure_hub(tmp_path, monkeypatch, path=owner)


def test_record_without_path_retires_the_sole_holder(tmp_path, monkeypatch):
    setup_card(tmp_path)
    fake = configure_hub(tmp_path, monkeypatch)
    set_record(tmp_path, fake)  # an older release wrote no path
    d = move(tmp_path)
    assert d.hub["status"] == "done"
    assert len(sent(fake, "retire")) == 1
    assert record(tmp_path, fake)["path"] == A


def test_record_without_path_beside_another_holder_is_left_alone(tmp_path, monkeypatch):
    fake = two_cards(tmp_path, monkeypatch)
    set_record(tmp_path, fake)
    d = move(tmp_path)
    # Rule: without a path the archived file owns the record only when it is the sole holder.
    assert d.hub["reason"] == "card-id-collision"
    assert not sent(fake, "retire")
    assert "path" not in record(tmp_path, fake)


def test_archiving_the_owner_beside_one_holder_hands_over(tmp_path, monkeypatch):
    fake = two_cards(tmp_path, monkeypatch)
    d = move(tmp_path, A)
    assert d.hub["status"] == "skipped"
    assert d.hub["reason"] == "card-id-handover"
    assert not [a for a, _ in fake.calls if a != "status"]
    assert record(tmp_path, fake)["path"] == B


def test_archiving_both_owner_first_retires_once_on_the_second(tmp_path, monkeypatch):
    fake = two_cards(tmp_path, monkeypatch)
    move(tmp_path, A)
    assert not sent(fake, "retire")
    d = move(tmp_path, B)
    assert d.hub["status"] == "done"
    assert len(sent(fake, "retire")) == 1


def test_archiving_both_non_owner_first_retires_once_on_the_second(tmp_path, monkeypatch):
    fake = two_cards(tmp_path, monkeypatch)
    first = move(tmp_path, B)
    # Rule: a non-owning file archived while the owner is live keeps the skip and sends nothing.
    assert first.hub["reason"] == "card-id-collision"
    assert not sent(fake, "retire")
    assert record(tmp_path, fake)["path"] == A
    d = move(tmp_path, A)
    assert d.hub["status"] == "done"
    assert len(sent(fake, "retire")) == 1


def test_two_other_holders_keep_the_skip(tmp_path, monkeypatch):
    fake = two_cards(tmp_path, monkeypatch)
    card(tmp_path, C, "C text")
    d = move(tmp_path, A)
    assert d.hub["reason"] == "card-id-collision"
    assert not sent(fake, "retire")
    assert record(tmp_path, fake)["path"] == A


def test_undo_order_never_leaves_a_live_card_with_a_retired_id(tmp_path, monkeypatch):
    fake = two_cards(tmp_path, monkeypatch)
    archived_b = move(tmp_path, B)
    archived_a = move(tmp_path, A)
    assert archived_a.hub["status"] == "done"
    assert len(sent(fake, "retire")) == 1
    fake.calls.clear()
    archive.restore(tmp_path, archived_b)
    # B is the only holder and this workspace retired the id, so B's text goes back and B owns the record.
    puts = sent(fake, "put")
    assert len(puts) == 1
    assert puts[0]["text"] == "---\ncard_id: old\n---\nB text\n"
    assert record(tmp_path, fake)["path"] == B
    fake.calls.clear()
    undo_a = archive.restore(tmp_path, archived_a)
    # Another live holder owns the hub copy, so the second undo writes nothing.
    assert not sent(fake, "put")
    assert undo_a.hub["reason"] == "card-id-collision"
    assert (tmp_path / A).exists() and (tmp_path / B).exists()
    assert record(tmp_path, fake)["path"] == B


def test_undo_in_the_other_order_also_satisfies_both_rules(tmp_path, monkeypatch):
    fake = two_cards(tmp_path, monkeypatch)
    archived_b = move(tmp_path, B)
    archived_a = move(tmp_path, A)
    archive.restore(tmp_path, archived_a)
    assert len(sent(fake, "put")) == 1
    assert record(tmp_path, fake)["path"] == A
    fake.calls.clear()
    archive.restore(tmp_path, archived_b)
    assert not sent(fake, "put")
    assert record(tmp_path, fake)["path"] == A


def test_undoing_the_last_holder_revives_its_text(tmp_path, monkeypatch):
    setup_card(tmp_path, b"---\ncard_id: old\n---\nA text\n")
    fake = configure_hub(tmp_path, monkeypatch)
    d = move(tmp_path, A)
    assert d.hub["status"] == "done"
    fake.calls.clear()
    undo = archive.restore(tmp_path, d)
    assert undo.hub["status"] == "done"
    puts = sent(fake, "put")
    assert [p["text"] for p in puts] == ["---\ncard_id: old\n---\nA text\n"]


def test_tend_retires_an_earlier_skipped_sole_holder_once(tmp_path, monkeypatch):
    fake = two_cards(tmp_path, monkeypatch)
    card(tmp_path, C, "C text")
    d = move(tmp_path, A)
    assert d.hub["reason"] == "card-id-collision"
    first = tend.run(tmp_path, stages=["hub"])
    assert first["stages"]["hub"]["done"] == 0
    assert not sent(fake, "retire")
    (tmp_path / B).unlink()
    (tmp_path / C).unlink()
    result = tend.run(tmp_path, stages=["hub"])
    assert result["stages"]["hub"]["done"] == 1
    assert journal.load(tmp_path, d.decision_id).hub["status"] == "done"
    assert len(sent(fake, "retire")) == 1
    again = tend.run(tmp_path, stages=["hub"])
    assert again["stages"]["hub"]["done"] == 0
    assert len(sent(fake, "retire")) == 1


def test_tend_retires_both_skipped_archives_of_a_shared_id_once(tmp_path, monkeypatch):
    fake = two_cards(tmp_path, monkeypatch)
    card(tmp_path, C, "C text")
    first = move(tmp_path, A)
    second = move(tmp_path, B)
    third = move(tmp_path, C)
    assert {first.hub["reason"], second.hub["reason"]} == {"card-id-collision"}
    assert third.hub["status"] == "done"  # last holder out: the retire went out at archive time
    assert len(sent(fake, "retire")) == 1
    tend.run(tmp_path, stages=["hub"])
    tend.run(tmp_path, stages=["hub"])
    assert len(sent(fake, "retire")) == 1


def test_non_owner_with_live_owner_never_touches_the_hub(tmp_path, monkeypatch):
    fake = two_cards(tmp_path, monkeypatch)
    d = move(tmp_path, B)
    assert d.hub["reason"] == "card-id-collision"
    assert not [a for a, _ in fake.calls if a != "status"]
    assert record(tmp_path, fake)["path"] == A


@pytest.mark.parametrize("visibility", ["private", "secret"])
def test_withheld_cards_never_reach_the_hub_in_the_new_branches(tmp_path, monkeypatch, visibility):
    # Handover: the archived owner is withheld while a public holder remains.
    setup_card(tmp_path, f"---\ncard_id: old\nvisibility: {visibility}\n---\nsecret\n".encode())
    card(tmp_path, B, "B text")
    fake = configure_hub(tmp_path, monkeypatch)
    d = move(tmp_path, A)
    assert not sent(fake, "put")
    undo = archive.restore(tmp_path, d)
    assert not sent(fake, "put")
    assert undo.hub["status"] == "skipped"
    # Revive: a withheld sole holder is never put, even when the record names another file.
    (tmp_path / B).unlink()
    set_record(tmp_path, fake, path=B)
    moved = move(tmp_path, A)
    restored = archive.restore(tmp_path, moved)
    assert not sent(fake, "put")
    assert restored.hub["reason"].startswith("visibility-")
    # Repair: a withheld archived card is not put on the hub by tend either.
    tend.run(tmp_path, stages=["hub"])
    assert not sent(fake, "put")


@pytest.mark.parametrize("visibility", ["private", "secret"])
def test_a_withheld_card_is_not_a_holder(tmp_path, monkeypatch, visibility):
    setup_card(tmp_path, b"---\ncard_id: old\n---\nA text\n")
    card(tmp_path, B, "hidden", visibility=visibility)
    fake = configure_hub(tmp_path, monkeypatch)
    d = move(tmp_path, A)
    # Rule: only live, publishable cards hold an id, so the last publishable holder retires it.
    assert d.hub["status"] == "done"
    assert len(sent(fake, "retire")) == 1
    assert hub._holders(tmp_path, "old", A) == []


def test_withheld_sole_holder_still_retires_without_text(tmp_path, monkeypatch):
    setup_card(tmp_path, b"---\ncard_id: old\nvisibility: private\n---\nsecret\n")
    fake = configure_hub(tmp_path, monkeypatch)
    d = move(tmp_path, A)
    # A retire carries no card text, so it still removes any copy published before the card went private.
    assert d.hub["status"] == "done"
    assert "secret" not in repr(fake.calls)
