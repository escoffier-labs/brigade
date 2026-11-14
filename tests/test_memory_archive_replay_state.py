"""Queued decisions are reconciled against current bytes before network writes."""

import pytest

from brigade import memory_archive as archive, memory_care_hub as bridge, memory_care_journal as journal
from tests.test_memory_archive import setup_card
from tests.test_memory_archive_review import configure_hub, move


@pytest.mark.parametrize("change", ["recreated", "archive-edited", "undo-edited", "undo-deleted"])
def test_changed_local_state_supersedes_queued_write(tmp_path, monkeypatch, change):
    card = setup_card(tmp_path)
    original = card.read_bytes()
    fake = configure_hub(tmp_path, monkeypatch)
    fake.unavailable = True
    decision = move(tmp_path)
    if change.startswith("undo"):
        decision = archive.restore(tmp_path, decision)
        if change == "undo-edited":
            card.write_bytes(original + b"Edited after undo\n")
        else:
            card.unlink()
    elif change == "recreated":
        card.write_bytes(original)
    else:
        (tmp_path / archive.archive_rel("memory/cards/old.md")).write_bytes(b"changed archive\n")
    fake.unavailable = False
    fake.calls.clear()
    bridge.retry_pending(tmp_path)
    assert journal.load(tmp_path, decision.decision_id).hub["status"] == "superseded"
    assert not any(action == ("put" if change.startswith("undo") else "retire") for action, _ in fake.calls)


@pytest.mark.parametrize(
    "state, expected",
    [
        ("archived-unchanged", "done"),
        ("source-recreated", "superseded"),
        ("archive-changed", "superseded"),
        ("undone-unchanged", "done"),
        ("undone-edited", "superseded"),
        ("undone-deleted", "superseded"),
        ("undo-pair", "done"),
        ("private-recorded", "skipped"),
        ("private-live", "skipped"),
        ("shared-id", "done"),
        ("hub-changed", "needs_review"),
        ("scope-changed", "needs_review"),
    ],
)
def test_replay_current_state_table(tmp_path, monkeypatch, state, expected):
    from brigade import memory_hub_client as hubc

    card = setup_card(tmp_path)
    if state == "private-recorded":
        card.write_text("---\ncard_id: old\nvisibility: private\n---\nprivate\n")
    original = card.read_bytes()
    fake = configure_hub(tmp_path, monkeypatch)
    fake.unavailable = state != "undone-unchanged"
    decision = move(tmp_path)
    undo = state.startswith("undone") or state in ("undo-pair", "private-recorded", "private-live")
    if undo:
        fake.unavailable = True
        decision = archive.restore(tmp_path, decision)
    if state == "source-recreated":
        card.write_bytes(original)
    elif state == "archive-changed":
        (tmp_path / archive.archive_rel("memory/cards/old.md")).write_bytes(b"changed\n")
    elif state == "undone-edited":
        card.write_bytes(original + b"edited\n")
    elif state == "undone-deleted":
        card.unlink()
    elif state == "private-live":
        card.write_text("---\ncard_id: old\nvisibility: private\n---\nprivate\n")
    elif state == "shared-id":
        card.with_name("other.md").write_bytes(original)
    elif state == "hub-changed":
        fake.identity = "another-hub"
    elif state == "scope-changed":
        monkeypatch.setattr(hubc, "load_settings", lambda _: hubc.HubSettings(mode="sync", scope="repo:other"))
    fake.unavailable = False
    fake.calls.clear()
    bridge.retry_pending(tmp_path)
    assert journal.load(tmp_path, decision.decision_id).hub["status"] == expected
    actions = [action for action, _ in fake.calls if action != "status"]
    if expected != "done":
        assert ("put" if undo else "retire") not in actions
    if state == "undo-pair":
        assert actions == ["retire", "put"]
    if state == "shared-id":
        # The owner was archived and one other holder remains: the hub is given that card's text, not retired.
        assert actions == ["put"]
