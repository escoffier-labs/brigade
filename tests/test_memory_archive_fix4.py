"""Fourth review ownership and abandoned-copy regressions."""

import pytest

from brigade import memory_archive as archive, memory_care_journal as journal, memory_care_tend as tend
from tests.test_memory_archive import setup_card
from tests.test_memory_archive_review import configure_hub, move
from tests.test_memory_archive_fix3 import crash_finish
from brigade import memory_hub_store as store


@pytest.mark.parametrize("owner", [False, True])
@pytest.mark.parametrize("replay", [False, True])
def test_shared_publication_never_retires(tmp_path, monkeypatch, owner, replay):
    path = setup_card(tmp_path)
    fake = configure_hub(tmp_path, monkeypatch)
    other = path.with_name("other.md")
    rel = "memory/cards/old.md" if owner else "memory/cards/other.md"
    with store.locked(tmp_path):
        state = store.published(tmp_path)
        state[fake.identity]["fleet"]["old"]["path"] = rel if replay else "memory/cards/old.md"
        store.save_published(tmp_path, state)
    if not replay:
        other.write_bytes(path.read_bytes())
    elif not owner:
        other.write_bytes(path.read_bytes())
        path.unlink()
    fake.unavailable = replay
    d = move(tmp_path, rel)
    if replay:
        assert d.hub["status"] == "pending"
        holder = other if owner else path
        holder.write_bytes(b"---\ncard_id: old\n---\nOther\n")
        fake.unavailable = False
        fake.calls.clear()
        result = tend.run(tmp_path, stages=["hub"])
        assert result["stages"]["hub"]["skipped_items"][0]["reason"] == "card-id-collision"
        d = journal.load(tmp_path, d.decision_id)
    assert not any(action == "retire" for action, _ in fake.calls)
    assert d.hub["status"] == "skipped"
    assert d.hub["reason"] == "card-id-collision"
    assert not (tmp_path / rel).exists()


def test_partial_recovery_does_not_block_next_archive(tmp_path, monkeypatch):
    path = setup_card(tmp_path)
    original = path.read_bytes()
    execute = crash_finish(monkeypatch, count=2)
    with pytest.raises(archive.ArchiveRefused):
        move(tmp_path)
    monkeypatch.setattr(journal.kernel, "execute", execute)
    path.write_bytes(b"changed card")
    result = tend.run(tmp_path, stages=[])
    decision_id = result["recovery"][0]["decision_id"]
    assert not journal._payload(tmp_path, decision_id)["restorable"]
    with pytest.raises(archive.ArchiveRefused, match="abandoned after card changed"):
        archive.restore(tmp_path, journal.load(tmp_path, decision_id))
    d = journal.load(tmp_path, decision_id)
    assert journal.before_bytes(tmp_path, d, "memory/cards/old.md") == original
    assert not (tmp_path / "memory/archive/cards/old.md").exists()
    assert move(tmp_path).decision_id != decision_id


def test_publication_other_path_skips_without_live_duplicate(tmp_path, monkeypatch):
    setup_card(tmp_path)
    configure_hub(tmp_path, monkeypatch, path="memory/cards/another.md")
    d = move(tmp_path)
    assert d.hub["status"] == "skipped"
    assert d.hub["reason"] == "card-id-collision"


def test_undo_never_revives_over_the_owning_card(tmp_path, monkeypatch):
    path = setup_card(tmp_path)
    fake = configure_hub(tmp_path, monkeypatch)
    owner = path.with_name("active.md")
    owner.write_bytes(path.read_bytes())
    with store.locked(tmp_path):
        state = store.published(tmp_path)
        state[fake.identity]["fleet"]["old"]["path"] = "memory/cards/active.md"
        store.save_published(tmp_path, state)
    original = path.read_bytes()
    d = move(tmp_path, "memory/cards/old.md")
    assert d.hub["reason"] == "card-id-collision"
    fake.calls.clear()
    archive.restore(tmp_path, d)
    # The local undo still happens, and the hub card owned by the other file is left alone.
    assert path.read_bytes() == original
    assert not any(action == "put" for action, _ in fake.calls)
    with store.locked(tmp_path):
        assert store.published(tmp_path)[fake.identity]["fleet"]["old"]["path"] == "memory/cards/active.md"
