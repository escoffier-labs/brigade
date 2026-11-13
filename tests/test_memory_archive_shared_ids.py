"""The hub copy of a card id follows the local cards still in use.

R1: the hub never shares the text of a card that is archived here.
R2: while any live local card holds the id, and this workspace published it, the hub shares it.
"""

import pytest

from brigade import memory_archive as archive, memory_care_hub as hub, memory_care_journal as journal
from brigade import memory_care_tend as tend, memory_hub_client as hubc, memory_hub_store as store
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


def text_of(target, rel):
    return (target / rel).read_text(encoding="utf-8")


def hash_of(target, rel):
    return hubc.content_hash(text_of(target, rel))


def sent(fake, action):
    return [f for a, f in fake.calls if a == action]


def two_cards(tmp_path, monkeypatch, owner=A):
    setup_card(tmp_path, b"---\ncard_id: old\n---\nA text\n")
    card(tmp_path, B, "B text")
    return configure_hub(tmp_path, monkeypatch, path=owner)


def test_record_without_path_retires_the_sole_holder(tmp_path, monkeypatch):
    setup_card(tmp_path)
    fake = configure_hub(tmp_path, monkeypatch)
    # An older release wrote no path and no `written`; the hub text is the archived text.
    set_record(tmp_path, fake, content_hash=hash_of(tmp_path, A))
    d = move(tmp_path)
    assert d.hub["status"] == "done"
    assert len(sent(fake, "retire")) == 1
    assert record(tmp_path, fake)["path"] == A


def test_record_without_path_beside_one_holder_needs_proof_of_the_archived_text(tmp_path, monkeypatch):
    fake = two_cards(tmp_path, monkeypatch)
    set_record(tmp_path, fake)
    d = move(tmp_path)
    # Rule: a record that names no file is orphaned, but nothing shows the hub text is archived text.
    assert d.hub["reason"] == "card-id-not-owned"
    assert not [a for a, _ in fake.calls if a != "status"]
    assert "path" not in record(tmp_path, fake)


def test_record_without_path_beside_one_holder_hands_over_when_the_hash_matches(tmp_path, monkeypatch):
    fake = two_cards(tmp_path, monkeypatch)
    set_record(tmp_path, fake, content_hash=hash_of(tmp_path, A))
    d = move(tmp_path)
    assert (d.hub["status"], d.hub["action"], d.hub["holder"]) == ("done", "handover", B)
    assert [p["text"] for p in sent(fake, "put")] == [text_of(tmp_path, B)]
    assert not sent(fake, "retire")
    assert record(tmp_path, fake)["path"] == B
    tend.run(tmp_path, stages=["hub"])
    assert len(sent(fake, "put")) == 1


def test_record_without_path_beside_two_holders_is_left_alone(tmp_path, monkeypatch):
    fake = two_cards(tmp_path, monkeypatch)
    card(tmp_path, C, "C text")
    set_record(tmp_path, fake, content_hash=hash_of(tmp_path, A))
    d = move(tmp_path)
    assert d.hub["reason"] == "card-id-collision"
    assert not [a for a, _ in fake.calls if a != "status"]
    assert "path" not in record(tmp_path, fake)


def test_archiving_the_owner_beside_one_holder_hands_over(tmp_path, monkeypatch):
    fake = two_cards(tmp_path, monkeypatch)
    d = move(tmp_path, A)
    assert d.hub["status"] == "done"
    assert d.hub["action"] == "handover"
    assert d.hub["holder"] == B
    assert not sent(fake, "retire")
    puts = sent(fake, "put")
    assert len(puts) == 1
    assert puts[0]["text"] == text_of(tmp_path, B)
    assert (puts[0]["base_revision"], puts[0]["base_epoch"], puts[0]["origin"]) == (1, "epoch", "interactive")
    saved = record(tmp_path, fake)
    assert (saved["path"], saved["revision"], saved["written"]) == (B, 3, True)
    assert saved["content_hash"] == hash_of(tmp_path, B)
    # The decision keeps the request it sent.
    assert journal.load(tmp_path, d.decision_id).hub["request"]["client_event_id"] == puts[0]["client_event_id"]


def test_handover_offline_stays_pending_and_replays_the_same_event(tmp_path, monkeypatch):
    fake = two_cards(tmp_path, monkeypatch)
    fake.unavailable = True
    d = move(tmp_path, A)
    assert d.hub["status"] == "pending"
    assert d.hub["action"] == "handover"
    event_id = d.hub["request"]["client_event_id"]
    assert not sent(fake, "put")  # the hub was unreachable, so the persisted request is all there is
    assert record(tmp_path, fake)["path"] == A  # unchanged until the hub acknowledges the put
    fake.unavailable = False
    fake.calls.clear()
    result = tend.run(tmp_path, stages=["hub"])
    assert result["stages"]["hub"]["done"] == 1
    puts = sent(fake, "put")
    assert [(f["client_event_id"], f["text"]) for f in puts] == [(event_id, text_of(tmp_path, B))]
    assert not sent(fake, "retire")
    assert journal.load(tmp_path, d.decision_id).hub["status"] == "done"
    assert record(tmp_path, fake)["path"] == B
    tend.run(tmp_path, stages=["hub"])
    assert len(sent(fake, "put")) == 1


@pytest.mark.parametrize("visibility", ["private", "secret"])
def test_handover_holder_gone_private_before_retry_retires_instead(tmp_path, monkeypatch, visibility):
    fake = two_cards(tmp_path, monkeypatch)
    fake.unavailable = True
    d = move(tmp_path, A)
    fake.unavailable = False
    fake.calls.clear()
    card(tmp_path, B, "B text", visibility=visibility)
    tend.run(tmp_path, stages=["hub"])
    assert not sent(fake, "put")
    assert len(sent(fake, "retire")) == 1
    assert "B text" not in repr(fake.calls)
    assert journal.load(tmp_path, d.decision_id).hub["status"] == "done"
    assert journal.load(tmp_path, d.decision_id).hub["action"] == "retire"
    assert "holder" not in journal.load(tmp_path, d.decision_id).hub


def test_handover_put_refused_as_a_conflict_becomes_a_proposal(tmp_path, monkeypatch):
    fake = two_cards(tmp_path, monkeypatch)
    real = fake.call

    def refuse(action, **fields):
        if action == "put":
            fake.calls.append((action, fields))
            raise hubc.HubRefusal(409, {"code": "memory_base_mismatch", "error": "stale"})
        return real(action, **fields)

    monkeypatch.setattr(fake, "call", refuse)
    d = move(tmp_path, A)
    assert d.hub["status"] == "conflict"
    assert d.hub["proposal_id"]
    assert record(tmp_path, fake)["path"] == A


def test_handover_is_refused_for_a_record_this_workspace_did_not_write(tmp_path, monkeypatch):
    fake = two_cards(tmp_path, monkeypatch)
    set_record(tmp_path, fake, path=A, content_hash="f" * 64)  # adopted head with other text
    d = move(tmp_path, A)
    assert d.hub["reason"] == "card-id-not-owned"
    assert not [a for a, _ in fake.calls if a != "status"]
    assert record(tmp_path, fake)["path"] == A


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
    assert first.hub["reason"] == "card-id-collision"
    assert (second.hub["status"], second.hub["action"], second.hub["holder"]) == ("done", "handover", C)
    assert third.hub["status"] == "done"  # last holder out: the retire went out at archive time
    assert len(sent(fake, "retire")) == 1
    tend.run(tmp_path, stages=["hub"])
    tend.run(tmp_path, stages=["hub"])
    assert len(sent(fake, "retire")) == 1
    assert len(sent(fake, "put")) == 1


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
    assert [p["text"] for p in sent(fake, "put")] == [text_of(tmp_path, B)]  # only the public holder's text
    undo = archive.restore(tmp_path, d)
    assert "secret" not in repr(fake.calls)
    assert undo.hub["status"] == "skipped"
    # Revive: a withheld sole holder is never put, even when the record names another file.
    (tmp_path / B).unlink()
    set_record(tmp_path, fake, path=B)
    moved = move(tmp_path, A)
    restored = archive.restore(tmp_path, moved)
    assert "secret" not in repr(fake.calls)
    assert restored.hub["reason"].startswith("visibility-")
    # Repair: a withheld archived card is not put on the hub by tend either.
    tend.run(tmp_path, stages=["hub"])
    assert "secret" not in repr(fake.calls)


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


def test_adopted_record_with_other_text_is_not_retired(tmp_path, monkeypatch):
    setup_card(tmp_path, b"---\ncard_id: old\n---\nA text\n")
    fake = configure_hub(tmp_path, monkeypatch)
    set_record(tmp_path, fake, path=A, content_hash="f" * 64)
    d = move(tmp_path, A)
    assert d.hub["status"] == "skipped"
    assert d.hub["reason"] == "card-id-not-owned"
    assert not [a for a, _ in fake.calls if a != "status"]
    # A later run leaves the decision alone.
    tend.run(tmp_path, stages=["hub"])
    assert not [a for a, _ in fake.calls if a != "status"]
    assert journal.load(tmp_path, d.decision_id).hub["reason"] == "card-id-not-owned"


@pytest.mark.parametrize("with_path", [True, False])
def test_adopted_record_with_the_archived_text_is_retired(tmp_path, monkeypatch, with_path):
    setup_card(tmp_path, b"---\ncard_id: old\n---\nA text\n")
    fake = configure_hub(tmp_path, monkeypatch)
    fields = {"path": A} if with_path else {}
    set_record(tmp_path, fake, content_hash=hash_of(tmp_path, A), **fields)
    d = move(tmp_path, A)
    assert d.hub["status"] == "done"
    assert len(sent(fake, "retire")) == 1


def test_written_record_is_retired_whatever_its_hash(tmp_path, monkeypatch):
    setup_card(tmp_path, b"---\ncard_id: old\n---\nA text\n")
    fake = configure_hub(tmp_path, monkeypatch)
    set_record(tmp_path, fake, path=A, written=True, content_hash="f" * 64)
    d = move(tmp_path, A)
    assert d.hub["status"] == "done"
    assert len(sent(fake, "retire")) == 1


def test_tend_repair_uses_the_journal_bytes_for_authority(tmp_path, monkeypatch):
    fake = two_cards(tmp_path, monkeypatch)
    card(tmp_path, C, "C text")
    set_record(tmp_path, fake, path=A, content_hash=hubc.content_hash("---\ncard_id: old\n---\nA text\n"))
    d = move(tmp_path, A)
    assert d.hub["reason"] == "card-id-collision"
    (tmp_path / B).unlink()
    (tmp_path / C).unlink()
    tend.run(tmp_path, stages=["hub"])
    assert journal.load(tmp_path, d.decision_id).hub["status"] == "done"
    assert len(sent(fake, "retire")) == 1


def test_tend_repair_refuses_a_record_it_cannot_prove_and_stays_final(tmp_path, monkeypatch):
    fake = two_cards(tmp_path, monkeypatch)
    card(tmp_path, C, "C text")
    set_record(tmp_path, fake, path=A, content_hash="f" * 64)
    d = move(tmp_path, A)
    (tmp_path / B).unlink()
    (tmp_path / C).unlink()
    tend.run(tmp_path, stages=["hub"])
    tend.run(tmp_path, stages=["hub"])
    saved = journal.load(tmp_path, d.decision_id).hub
    assert (saved["status"], saved["reason"]) == ("skipped", "card-id-not-owned")
    assert not [a for a, _ in fake.calls if a != "status"]


def retired_in_fleet(tmp_path, monkeypatch):
    setup_card(tmp_path, b"---\ncard_id: old\n---\nA text\n")
    fake = configure_hub(tmp_path, monkeypatch)
    d = move(tmp_path, A)
    assert d.hub["status"] == "done"
    fake.calls.clear()
    return fake, d


def test_a_retire_in_another_scope_does_not_authorize_a_revive(tmp_path, monkeypatch):
    fake, d = retired_in_fleet(tmp_path, monkeypatch)
    monkeypatch.setattr(hubc, "load_settings", lambda _: hubc.HubSettings(mode="sync", scope="other"))
    with store.locked(tmp_path):
        state = store.published(tmp_path)
        state[fake.identity]["other"] = {"old": {"revision": 1, "log_epoch": "epoch", "path": "memory/cards/x.md"}}
        store.save_published(tmp_path, state)
    undo = archive.restore(tmp_path, d)
    assert not sent(fake, "put")
    assert undo.hub["reason"] == "card-id-collision"


def test_a_retire_on_another_hub_does_not_authorize_a_revive(tmp_path, monkeypatch):
    fake, d = retired_in_fleet(tmp_path, monkeypatch)

    class OtherHub(type(fake)):
        identity = "other-hub"

    other = OtherHub()
    monkeypatch.setattr(hubc, "client_for", lambda _: other)
    with store.locked(tmp_path):
        state = store.published(tmp_path)
        state["other-hub"] = {"fleet": {"old": {"revision": 1, "log_epoch": "epoch", "path": "memory/cards/x.md"}}}
        store.save_published(tmp_path, state)
    undo = archive.restore(tmp_path, d)
    assert not sent(other, "put")
    assert undo.hub["reason"] == "card-id-collision"


def test_a_later_write_to_the_record_voids_the_retire_evidence(tmp_path, monkeypatch):
    fake, d = retired_in_fleet(tmp_path, monkeypatch)
    # Someone else wrote the id after this workspace retired it: the base moved past the retire's revision.
    set_record(tmp_path, fake, path="memory/cards/x.md", revision=7)
    undo = archive.restore(tmp_path, d)
    assert not sent(fake, "put")
    assert undo.hub["reason"] == "card-id-collision"


def test_an_older_retire_result_without_a_revision_still_authorizes(tmp_path, monkeypatch):
    fake, d = retired_in_fleet(tmp_path, monkeypatch)
    saved = {k: v for k, v in journal.load(tmp_path, d.decision_id).hub.items() if k not in ("revision", "log_epoch")}
    journal.update_hub(tmp_path, d.decision_id, saved)
    undo = archive.restore(tmp_path, d)
    assert undo.hub["status"] == "done"
    assert len(sent(fake, "put")) == 1


def test_repair_persists_its_event_id_before_sending(tmp_path, monkeypatch):
    fake = two_cards(tmp_path, monkeypatch)
    card(tmp_path, C, "C text")
    d = move(tmp_path, A)
    assert d.hub["reason"] == "card-id-collision"
    (tmp_path / B).unlink()
    (tmp_path / C).unlink()
    real = store.save_published
    crashed = []

    def crash_after_the_hub_accepts(target, state):
        if sent(fake, "retire") and not crashed:
            crashed.append(True)
            raise RuntimeError("process died")
        return real(target, state)

    monkeypatch.setattr(store, "save_published", crash_after_the_hub_accepts)
    with pytest.raises(RuntimeError):
        hub.retry_pending(tmp_path)
    saved = journal.load(tmp_path, d.decision_id).hub
    assert saved["status"] == "pending"
    event_id = saved["request"]["client_event_id"]
    assert [f["client_event_id"] for f in sent(fake, "retire")] == [event_id]
    result = hub.retry_pending(tmp_path)
    assert result["done"] == 1
    assert [f["client_event_id"] for f in sent(fake, "retire")] == [event_id, event_id]
    assert journal.load(tmp_path, d.decision_id).hub["status"] == "done"


def three_cards(tmp_path, monkeypatch):
    fake = two_cards(tmp_path, monkeypatch)
    card(tmp_path, C, "C text")
    return fake


def shared_text(fake):
    """What the fake hub shares for the id: the last put's text, None after a retire, "initial" before any write."""
    shown = "initial"
    for action, fields in fake.calls:
        if action == "put":
            shown = fields["text"]
        elif action == "retire":
            shown = None
    return shown


def quiet(fake):
    return [a for a, _ in fake.calls if a != "status"]


def final(tmp_path, d):
    return journal.load(tmp_path, d.decision_id).hub


@pytest.mark.parametrize("tend_between", [False, True])
def test_three_files_the_owner_is_replaced_when_one_holder_is_left(tmp_path, monkeypatch, tend_between):
    fake = three_cards(tmp_path, monkeypatch)
    first = move(tmp_path, A)
    assert first.hub["reason"] == "card-id-collision"
    if tend_between:
        tend.run(tmp_path, stages=["hub"])
        assert not quiet(fake)
    second = move(tmp_path, B)
    # The second archive carries the handover: the record names A, which is archived, and C is the only holder.
    assert (second.hub["status"], second.hub["action"], second.hub["holder"]) == ("done", "handover", C)
    tend.run(tmp_path, stages=["hub"])
    assert shared_text(fake) == text_of(tmp_path, C)
    assert record(tmp_path, fake)["path"] == C
    # The first decision is closed, not re-sent.
    assert final(tmp_path, first)["status"] == "superseded"
    assert final(tmp_path, first)["reason"] == "later-archive"
    assert final(tmp_path, second)["status"] == "done"
    fake.calls.clear()
    tend.run(tmp_path, stages=["hub"])
    assert not quiet(fake)
    assert "A text" not in repr(fake.calls) and "B text" not in repr(fake.calls)


def test_three_files_offline_second_archive_hands_over_on_tend(tmp_path, monkeypatch):
    fake = three_cards(tmp_path, monkeypatch)
    first = move(tmp_path, A)
    fake.unavailable = True
    second = move(tmp_path, B)
    assert second.hub["status"] == "pending"
    fake.unavailable = False
    fake.calls.clear()
    tend.run(tmp_path, stages=["hub"])
    # Rule: the queued later archive was decided with fewer holders, so it carries the handover
    # with its stored event id, and the earlier skipped decision is settled by it.
    assert shared_text(fake) == text_of(tmp_path, C)
    assert record(tmp_path, fake)["path"] == C
    assert [f["client_event_id"] for f in sent(fake, "put")] == [second.hub["request"]["client_event_id"]]
    assert not sent(fake, "retire")
    fake.calls.clear()
    tend.run(tmp_path, stages=["hub"])
    assert not quiet(fake)
    assert (final(tmp_path, second)["status"], final(tmp_path, second)["action"]) == ("done", "handover")
    assert (final(tmp_path, first)["status"], final(tmp_path, first)["reason"]) == ("superseded", "later-archive")


def skip_as_an_older_release_did(tmp_path, d, rel):
    journal.update_hub(
        tmp_path,
        d.decision_id,
        {"status": "skipped", "action": "retire", "card_id": "old", "reason": "card-id-collision", "path": rel},
    )


def test_old_data_undone_later_archive_does_not_close_the_owner_decision(tmp_path, monkeypatch):
    fake = two_cards(tmp_path, monkeypatch)
    # An older release archived both files and skipped both retires; nothing reached the hub.
    monkeypatch.setattr(hubc, "load_settings", lambda _: hubc.HubSettings(mode="off"))
    first = move(tmp_path, A)
    second = move(tmp_path, B)
    skip_as_an_older_release_did(tmp_path, first, A)
    skip_as_an_older_release_did(tmp_path, second, B)
    monkeypatch.setattr(hubc, "load_settings", lambda _: hubc.HubSettings(mode="sync", scope="fleet"))
    undo = archive.restore(tmp_path, journal.load(tmp_path, second.decision_id))
    assert undo.hub["reason"] == "card-id-collision"
    assert not quiet(fake)
    tend.run(tmp_path, stages=["hub"])
    assert (final(tmp_path, first)["status"], final(tmp_path, first)["action"]) == ("done", "handover")
    assert final(tmp_path, first)["holder"] == B
    assert shared_text(fake) == text_of(tmp_path, B)
    assert record(tmp_path, fake)["path"] == B
    fake.calls.clear()
    tend.run(tmp_path, stages=["hub"])
    assert not quiet(fake)


@pytest.mark.parametrize("tend_between", [False, True])
def test_four_files_end_with_the_last_holder_sharing(tmp_path, monkeypatch, tend_between):
    fake = three_cards(tmp_path, monkeypatch)
    card(tmp_path, "memory/cards/fourth.md", "D text")
    d_rel = "memory/cards/fourth.md"
    decisions = []
    for rel in (A, B, C):
        decisions.append(move(tmp_path, rel))
        if tend_between:
            tend.run(tmp_path, stages=["hub"])
    tend.run(tmp_path, stages=["hub"])
    # R1 and R2: only D is in use and the hub shares D's text, never an archived card's.
    assert shared_text(fake) == text_of(tmp_path, d_rel)
    assert record(tmp_path, fake)["path"] == d_rel
    assert not sent(fake, "retire")
    assert "A text" not in repr(fake.calls) and "B text" not in repr(fake.calls) and "C text" not in repr(fake.calls)
    assert [final(tmp_path, d)["status"] for d in decisions][2] == "done"
    assert {final(tmp_path, d)["status"] for d in decisions[:2]} == {"superseded"}
    fake.calls.clear()
    tend.run(tmp_path, stages=["hub"])
    assert not quiet(fake)
    # The last holder going away retires the id once, and A and B are not re-sent after that.
    archived_d = move(tmp_path, d_rel)
    assert archived_d.hub["status"] == "done"
    assert shared_text(fake) is None
    tend.run(tmp_path, stages=["hub"])
    tend.run(tmp_path, stages=["hub"])
    assert len(sent(fake, "retire")) == 1


def test_orphan_handover_uses_the_archived_owner_bytes_for_authority(tmp_path, monkeypatch):
    fake = three_cards(tmp_path, monkeypatch)
    set_record(tmp_path, fake, path=A, content_hash=hash_of(tmp_path, A))  # adopted: not written here
    first = move(tmp_path, A)
    assert first.hub["reason"] == "card-id-collision"
    second = move(tmp_path, B)
    # B's text differs from the hub text, but the journal holds A's archived bytes and they match the record.
    assert (second.hub["status"], second.hub["action"], second.hub["holder"]) == ("done", "handover", C)
    assert shared_text(fake) == text_of(tmp_path, C)


def test_orphan_handover_without_provable_text_is_not_owned(tmp_path, monkeypatch):
    fake = three_cards(tmp_path, monkeypatch)
    set_record(tmp_path, fake, path=A, content_hash="f" * 64)
    move(tmp_path, A)
    second = move(tmp_path, B)
    assert second.hub["reason"] == "card-id-not-owned"
    assert not quiet(fake)
    tend.run(tmp_path, stages=["hub"])
    assert not quiet(fake)
    assert final(tmp_path, second)["reason"] == "card-id-not-owned"


def test_orphan_evidence_ignores_an_archive_that_was_undone(tmp_path, monkeypatch):
    fake = three_cards(tmp_path, monkeypatch)
    set_record(tmp_path, fake, path=A, content_hash=hash_of(tmp_path, A))
    first = move(tmp_path, A)
    archive.restore(tmp_path, first)
    (tmp_path / A).unlink()  # the record's file is gone and the only archive of it was undone
    second = move(tmp_path, B)
    assert second.hub["reason"] == "card-id-not-owned"
    assert not quiet(fake)


@pytest.mark.parametrize("visibility", ["private", "secret"])
@pytest.mark.parametrize("scenario", ["orphan", "pathless", "old-data", "four"])
def test_withheld_remaining_holder_gets_no_put_on_the_new_paths(tmp_path, monkeypatch, visibility, scenario):
    fake = three_cards(tmp_path, monkeypatch)

    def hide(rel):
        card(tmp_path, rel, "secret body", visibility=visibility)

    if scenario == "orphan":
        move(tmp_path, A)
        hide(C)
        move(tmp_path, B)
    elif scenario == "pathless":
        set_record(tmp_path, fake, content_hash=hash_of(tmp_path, A))
        move(tmp_path, A)
        hide(C)
        move(tmp_path, B)
    elif scenario == "four":
        card(tmp_path, "memory/cards/fourth.md", "D text")
        for rel in (A, B):
            move(tmp_path, rel)
        hide("memory/cards/fourth.md")
        move(tmp_path, C)
    else:
        (tmp_path / C).unlink()
        monkeypatch.setattr(hubc, "load_settings", lambda _: hubc.HubSettings(mode="off"))
        first = move(tmp_path, A)
        second = move(tmp_path, B)
        skip_as_an_older_release_did(tmp_path, first, A)
        skip_as_an_older_release_did(tmp_path, second, B)
        monkeypatch.setattr(hubc, "load_settings", lambda _: hubc.HubSettings(mode="sync", scope="fleet"))
        archive.restore(tmp_path, journal.load(tmp_path, second.decision_id))
        hide(B)
    tend.run(tmp_path, stages=["hub"])
    # Nothing publishable holds the id, so it is retired and no card text is ever sent.
    assert not sent(fake, "put")
    assert len(sent(fake, "retire")) == 1
    assert shared_text(fake) is None
    assert "secret body" not in repr(fake.calls)
    fake.calls.clear()
    tend.run(tmp_path, stages=["hub"])
    assert not quiet(fake)


def test_retry_reads_the_cards_and_journal_a_constant_number_of_times(tmp_path, monkeypatch):
    from brigade import memory_hub_sync as sync

    setup_card(tmp_path, b"---\ncard_id: old\n---\nA text\n")
    names = [f"memory/cards/dup{n}.md" for n in range(6)]
    for rel in names:
        card(tmp_path, rel, rel)
    configure_hub(tmp_path, monkeypatch)
    skipped = [move(tmp_path, rel) for rel in [A, *names[:4]]]
    assert {d.hub["reason"] for d in skipped} == {"card-id-collision"}
    reads = {"cards": 0, "entries": 0}
    real_cards, real_entries = sync._local_cards, journal.entries

    def count_cards(target):
        reads["cards"] += 1
        return real_cards(target)

    def count_entries(target, **kwargs):
        reads["entries"] += 1
        return real_entries(target, **kwargs)

    evaluated = []
    real_send = hub._send

    def count_send(*args, **kwargs):
        evaluated.append(kwargs["decision"].decision_id)
        return real_send(*args, **kwargs)

    monkeypatch.setattr(sync, "_local_cards", count_cards)
    monkeypatch.setattr(journal, "entries", count_entries)
    monkeypatch.setattr(hub, "_send", count_send)
    result = hub.retry_pending(tmp_path)
    assert result["done"] == 0
    # Five skipped decisions are re-evaluated with no hub write: one read of the cards and one of the journal.
    assert sorted(evaluated) == sorted(d.decision_id for d in skipped)
    assert reads == {"cards": 1, "entries": 1}


def test_retry_reads_again_after_a_hub_write(tmp_path, monkeypatch):
    from brigade import memory_hub_sync as sync

    fake = three_cards(tmp_path, monkeypatch)
    first = move(tmp_path, A)
    fake.unavailable = True
    move(tmp_path, B)
    fake.unavailable = False
    reads = {"cards": 0}
    real_cards = sync._local_cards

    def count_cards(target):
        reads["cards"] += 1
        return real_cards(target)

    monkeypatch.setattr(sync, "_local_cards", count_cards)
    hub.retry_pending(tmp_path)
    # The first decision is settled by the queued second one, whose handover re-reads the cards before it sends.
    assert reads["cards"] >= 2
    assert shared_text(fake) == text_of(tmp_path, C)
    assert len(sent(fake, "put")) == 1
    assert final(tmp_path, first)["status"] == "superseded"


def test_queued_handover_sends_the_holder_text_as_it_is_now(tmp_path, monkeypatch):
    fake = two_cards(tmp_path, monkeypatch)
    fake.unavailable = True
    d = move(tmp_path, A)
    stored = d.hub["request"]
    assert stored["text"] == text_of(tmp_path, B)
    # The holder is edited while the handover waits for the hub.
    card(tmp_path, B, "B text, with the withdrawn line gone")
    fake.unavailable = False
    fake.calls.clear()
    tend.run(tmp_path, stages=["hub"])
    [put] = sent(fake, "put")
    assert put["text"] == text_of(tmp_path, B)
    assert put["client_event_id"] != stored["client_event_id"]
    assert "B text\n" not in repr(fake.calls)
    assert final(tmp_path, d)["status"] == "done"
    assert record(tmp_path, fake)["content_hash"] == hash_of(tmp_path, B)


def test_two_archives_queued_offline_send_one_retire_and_no_conflict(tmp_path, monkeypatch):
    fake = two_cards(tmp_path, monkeypatch)
    fake.unavailable = True
    first = move(tmp_path, A)
    second = move(tmp_path, B)
    assert (first.hub["action"], second.hub["action"]) == ("handover", "retire")
    fake.unavailable = False
    fake.calls.clear()
    result = tend.run(tmp_path, stages=["hub"])
    # Rule: the later queued archive carries the write; sending both would refuse the second as stale.
    assert [f["client_event_id"] for f in sent(fake, "retire")] == [second.hub["request"]["client_event_id"]]
    assert not sent(fake, "put")
    assert result["stages"]["hub"]["conflict"] == 0
    assert final(tmp_path, second)["status"] == "done"
    assert (final(tmp_path, first)["status"], final(tmp_path, first)["reason"]) == ("superseded", "later-archive")


@pytest.mark.parametrize("front", ["card_id: elsewhere\n", "card_id: old\nvisibility: private\n"])
def test_named_owner_still_on_disk_keeps_the_record(tmp_path, monkeypatch, front):
    fake = three_cards(tmp_path, monkeypatch)
    # The record's file stays live but stops holding the id: it changed id, or went private.
    (tmp_path / A).write_bytes(f"---\n{front}---\nA text\n".encode())
    d = move(tmp_path, B)
    # Rule: only a named file that is gone or archived gives up the record.
    assert (d.hub["status"], d.hub["reason"]) == ("skipped", "card-id-collision")
    assert not quiet(fake)
    assert record(tmp_path, fake)["path"] == A


def test_handover_judges_the_holder_as_it_is_under_the_lock(tmp_path, monkeypatch):
    fake = two_cards(tmp_path, monkeypatch)
    snap = hub._Snapshot(tmp_path)
    snap.cards()
    # The holder goes private after the run read its cards.
    card(tmp_path, B, "B text", visibility="private")
    result = hub._send(
        tmp_path, action="retire", card_id="old", reason="ended", path=A, content_hash=hash_of(tmp_path, A), snap=snap
    )
    assert (result["status"], result["action"]) == ("done", "retire")
    assert not sent(fake, "put")
    assert "B text" not in repr(fake.calls)
