from __future__ import annotations

import pytest

from brigade import ingest, memory_care_tend
from brigade import memory_care_journal as journal
from brigade.memory_inbox_append import commit_card_append

CARD = "memory/cards/instructions/user-md--overflow-preference.md"
HEAD = (
    b"---\ntitle: USER.md overflow: preference\ncategory: instructions\ntags: [moved-instructions]\n"
    b"visibility: private\nsource_file: USER.md\narchive: never\n---\n"
)


def test_first_append_creates_the_card_and_undo_removes_it(tmp_path):
    d = commit_card_append(
        tmp_path,
        CARD,
        HEAD + b"\nPrefer short replies.\n",
        header_bytes=len(HEAD),
        reason="ingest:document-routed-to-card",
    )
    assert (tmp_path / CARD).read_bytes() == HEAD + b"\nPrefer short replies.\n"
    assert journal._payload(tmp_path, d.decision_id)["appends"][0]["writer"] == "card-append"
    memory_care_tend.undo(tmp_path, d.decision_id)
    assert not (tmp_path / CARD).exists()


def test_undo_of_the_creating_append_keeps_frontmatter_for_later_appends(tmp_path):
    first = commit_card_append(tmp_path, CARD, HEAD + b"\none\n", header_bytes=len(HEAD), reason="r")
    commit_card_append(tmp_path, CARD, b"\n\ntwo\n", header_bytes=0, reason="r")
    memory_care_tend.undo(tmp_path, first.decision_id)
    assert (tmp_path / CARD).read_bytes() == HEAD + b"\n\ntwo\n"


def test_undo_refuses_an_edited_block(tmp_path):
    d = commit_card_append(tmp_path, CARD, HEAD + b"\none\n", header_bytes=len(HEAD), reason="r")
    (tmp_path / CARD).write_bytes(HEAD + b"\nONE\n")
    with pytest.raises(journal.JournalConflict):
        memory_care_tend.undo(tmp_path, d.decision_id)


def test_read_only_cards_abandon_the_append(tmp_path):
    (tmp_path / ".brigade").mkdir()
    (tmp_path / ".brigade" / "memory-care.toml").write_text('read_only_paths = ["memory/cards/instructions/**"]\n')
    with pytest.raises(journal.JournalConflict):
        commit_card_append(tmp_path, CARD, HEAD + b"\nx\n", header_bytes=len(HEAD), reason="r")
    assert not (tmp_path / CARD).exists()


def test_interrupted_publish_is_recovered_once(tmp_path, monkeypatch):
    real = ingest._append_document_nofollow
    calls = []

    def fail_once(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("interrupted")
        return real(*args, **kwargs)

    monkeypatch.setattr(ingest, "_append_document_nofollow", fail_once)
    with pytest.raises(OSError):
        commit_card_append(tmp_path, CARD, HEAD + b"\nx\n", header_bytes=len(HEAD), reason="r")
    journal.resolve_prepared(tmp_path)
    journal.resolve_prepared(tmp_path)
    assert (tmp_path / CARD).read_bytes() == HEAD + b"\nx\n"


def test_a_block_built_for_a_missing_card_is_refused_once_the_card_exists(tmp_path):
    # A non-Brigade writer created the card after the block was built: a second frontmatter would corrupt it.
    (tmp_path / CARD).parent.mkdir(parents=True)
    (tmp_path / CARD).write_bytes(HEAD + b"\nfirst\n")
    with pytest.raises(journal.JournalConflict):
        commit_card_append(tmp_path, CARD, HEAD + b"\nsecond\n", header_bytes=len(HEAD), reason="r")
    assert (tmp_path / CARD).read_bytes() == HEAD + b"\nfirst\n"


def test_a_plain_block_is_refused_when_the_card_is_missing(tmp_path):
    with pytest.raises(journal.JournalConflict):
        commit_card_append(tmp_path, CARD, b"\n\nsecond\n", header_bytes=0, reason="r")
    assert not (tmp_path / CARD).exists()


def test_an_append_is_refused_when_the_card_is_not_one_this_feature_created(tmp_path):
    # The last check before the write reads the same bytes the append guard pins.
    (tmp_path / CARD).parent.mkdir(parents=True)
    planted = HEAD.replace(b"visibility: private", b"visibility: public") + b"\nplanted\n"
    (tmp_path / CARD).write_bytes(planted)
    with pytest.raises(journal.JournalConflict, match="not one this feature created"):
        commit_card_append(tmp_path, CARD, b"\n\nsecond\n", header_bytes=0, reason="r")
    assert (tmp_path / CARD).read_bytes() == planted
