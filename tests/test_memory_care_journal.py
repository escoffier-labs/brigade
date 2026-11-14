from datetime import datetime, timezone
import os

import pytest

from brigade import memory_care_journal as journal


def test_commit_and_revert(tmp_path):
    path = tmp_path / "card.md"
    path.write_bytes(b"old\r\n")
    d = journal.commit(
        tmp_path, kind="fix", reason="test", card_id=None, changes=[journal.Change("card.md", b"old\r\n", b"new")]
    )
    assert journal.before_bytes(tmp_path, d, "card.md") == b"old\r\n"
    assert path.read_bytes() == b"new"
    undo = journal.revert(tmp_path, d.decision_id, reason="undo")
    assert path.read_bytes() == b"old\r\n"
    assert undo.undoes == d.decision_id
    assert journal.load(tmp_path, d.decision_id).undone_by == undo.decision_id
    assert journal.entries(tmp_path, since=datetime.now(timezone.utc)) == []
    if os.name == "posix":
        directory = tmp_path / journal.JOURNAL_REL / d.decision_id
        assert directory.stat().st_mode & 0o777 == 0o700
        assert (directory / "decision.json").stat().st_mode & 0o777 == 0o600


def test_drift_never_overwritten(tmp_path):
    path = tmp_path / "card.md"
    path.write_bytes(b"edited")
    with pytest.raises(journal.JournalConflict):
        journal.commit(
            tmp_path, kind="fix", reason="test", card_id=None, changes=[journal.Change("card.md", b"old", b"new")]
        )
    assert path.read_bytes() == b"edited"
    assert journal.entries(tmp_path) == []


def test_applied_and_undo_conflict(tmp_path):
    path = tmp_path / "card.md"
    path.write_bytes(b"new")
    d = journal.commit(
        tmp_path,
        kind="fix",
        reason="test",
        card_id=None,
        changes=[journal.Change("card.md", b"old", b"new")],
        applied=True,
    )
    path.write_bytes(b"newer")
    with pytest.raises(journal.JournalConflict):
        journal.revert(tmp_path, d.decision_id, reason="undo")
    assert path.read_bytes() == b"newer"


def test_read_only(tmp_path):
    (tmp_path / ".brigade").mkdir()
    (tmp_path / ".brigade/memory-care.toml").write_text('read_only_paths = ["policy/"]\n')
    with pytest.raises(journal.ReadOnlyPath):
        journal.commit(
            tmp_path, kind="fix", reason="test", card_id=None, changes=[journal.Change("policy/a.md", None, b"new")]
        )
    assert not (tmp_path / "policy/a.md").exists()
