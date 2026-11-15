"""Instruction move journal boundaries (matrix J)."""

from contextlib import contextmanager
from datetime import date, datetime, timezone
import os

import pytest

from brigade import ingest, memory_care_journal as journal, memory_inbox_drain

ID = "20261114T120000Z-instructions-01234567"
CARDS = ("memory/cards/instructions/user--one.md", "memory/cards/instructions/user--two.md")
BEFORE = b"# Preferences\n\n## One\nFirst\n\n## Two\nSecond\n"
AFTER = b"# Preferences\n\nPointers\n"


def move(target, *, decision_id=ID):
    return journal.commit_phased(
        target,
        kind="instructions",
        reason="test",
        card_id=None,
        decision_id=decision_id,
        changes=[journal.Change("USER.md", BEFORE, AFTER), *(journal.Change(p, None, p.encode()) for p in CARDS)],
        prepared_paths=CARDS,
        hub={},
        metadata={"document_undo": True},
    )


def source(target):
    (target / "USER.md").write_bytes(BEFORE)


def interrupt(monkeypatch, phase, crash_after=None):
    execute = journal._execute_phase
    kernel_execute = journal.kernel.execute

    def interrupted(target, decision_id, name, changes):
        if name == phase:
            if crash_after is None:
                raise RuntimeError("crash")
            with monkeypatch.context() as patch:
                patch.setattr(
                    journal.kernel,
                    "execute",
                    lambda plan, **kw: kernel_execute(
                        plan, **kw, inject=journal.kernel.FailureInjector(crash_after=crash_after)
                    ),
                )
                return execute(target, decision_id, name, changes)
        return execute(target, decision_id, name, changes)

    monkeypatch.setattr(journal, "_execute_phase", interrupted)
    return execute


def lock_spy(monkeypatch):
    entered = []
    original = ingest._document_lock

    @contextmanager
    def locked(target):
        with original(target):
            entered.append(True)
            yield

    monkeypatch.setattr(ingest, "_document_lock", locked)
    return entered


def crash(target, monkeypatch, phase="finish", crash_after=None):
    source(target)
    execute = interrupt(monkeypatch, phase, crash_after)
    with pytest.raises((RuntimeError, journal.JournalConflict), match="crash|recovery-required"):
        move(target)
    monkeypatch.setattr(journal, "_execute_phase", execute)


def assert_abandoned(target):
    assert journal._payload(target, ID)["phase"] == "abandoned"
    assert all(not (target / p).exists() for p in CARDS)


def link_source(target):
    copy = target / "copy.md"
    copy.write_bytes(BEFORE)
    (target / "USER.md").unlink()
    (target / "USER.md").symlink_to(copy)
    return copy


def test_preallocated_id_is_used_and_checked(tmp_path):
    source(tmp_path)
    now = datetime(2026, 11, 14, 12, tzinfo=timezone.utc)
    allocated = journal.new_decision_id("instructions", now=now)
    assert allocated.startswith("20261114T120000Z-instructions-")
    d = move(tmp_path, decision_id=allocated)
    assert d.decision_id == allocated
    assert d.created_at == now.isoformat()
    (tmp_path / "USER.md").write_bytes(BEFORE)
    snapshots = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    for invalid in (allocated, "../escape", ID.replace("instructions", "undo"), ID.replace("20261114", "20269999")):
        with pytest.raises(journal.JournalConflict, match="decision id taken or invalid"):
            move(tmp_path, decision_id=invalid)
        assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == snapshots


def test_move_crash_before_prepare_leaves_nothing(tmp_path, monkeypatch):
    crash(tmp_path, monkeypatch, "prepare")
    assert (tmp_path / "USER.md").read_bytes() == BEFORE
    assert all(not (tmp_path / p).exists() for p in CARDS)
    assert journal.resolve_prepared(tmp_path) == []
    assert not (tmp_path / journal.JOURNAL_REL / ID / "decision.json").exists()
    d = move(tmp_path, decision_id=None)
    assert d.decision_id != ID
    assert (tmp_path / "USER.md").read_bytes() == AFTER


@pytest.mark.parametrize("crash_after", [1, 2])
def test_move_crash_inside_prepare_restores_clean(tmp_path, monkeypatch, crash_after):
    crash(tmp_path, monkeypatch, "prepare", crash_after)
    journal.resolve_prepared(tmp_path)
    assert (tmp_path / "USER.md").read_bytes() == BEFORE
    assert all(not (tmp_path / p).exists() for p in CARDS)
    assert not (tmp_path / journal.JOURNAL_REL / ID / "decision.json").exists()
    assert journal.kernel.unfinished_operations(tmp_path) == []


def test_move_crash_between_phases_finishes_under_the_document_lock(tmp_path, monkeypatch):
    crash(tmp_path, monkeypatch)
    entered = lock_spy(monkeypatch)
    assert journal.resolve_prepared(tmp_path)[0]["phase"] == "committed"
    assert entered
    assert (tmp_path / "USER.md").read_bytes() == AFTER
    assert all((tmp_path / p).exists() for p in CARDS)


def test_move_crash_inside_finish_recovers_under_the_document_lock(tmp_path, monkeypatch):
    crash(tmp_path, monkeypatch, "finish", 1)
    assert journal._payload(tmp_path, ID)["phase"] == "committed"
    entered = lock_spy(monkeypatch)
    assert journal.resolve_prepared(tmp_path)[0]["phase"] == "committed"
    assert entered
    assert (tmp_path / "USER.md").read_bytes() == AFTER


def test_move_crash_then_edit_abandons_and_removes_new_cards(tmp_path, monkeypatch):
    crash(tmp_path, monkeypatch)
    (tmp_path / "USER.md").write_bytes(b"Edited")
    assert journal.resolve_prepared(tmp_path)[0]["phase"] == "abandoned"
    assert_abandoned(tmp_path)
    assert (tmp_path / "USER.md").read_bytes() == b"Edited"


@pytest.mark.parametrize("drift", ["none", "edited", "linked"])
def test_unreadable_unfinished_instructions_operation_takes_the_document_lock(tmp_path, monkeypatch, drift):
    from brigade import memory_care_pending as pending

    if drift == "linked" and os.name == "nt":
        pytest.skip("POSIX symlinks")
    crash(tmp_path, monkeypatch, "finish", 1)
    record = tmp_path / journal.JOURNAL_REL / ID / "decision.json"
    record.write_bytes(b"not json")
    if drift == "edited":
        (tmp_path / "USER.md").write_bytes(b"Edited")
        (tmp_path / CARDS[0]).write_bytes(b"Unrelated card edit")
    elif drift == "linked":
        link_source(tmp_path)
    assert pending.load(tmp_path)[ID]["document"] is True
    entered = lock_spy(monkeypatch)
    journal.resolve_prepared(tmp_path)
    assert entered
    assert journal.kernel.unfinished_operations(tmp_path) == []
    assert journal.resolve_prepared(tmp_path) == []
    assert ID not in pending.load(tmp_path)
    assert any(p.read_bytes() == b"not json" for p in record.parent.glob("decision.quarantined-*.json"))
    # Without a readable decision, preparation alone cannot prove card ownership.
    assert (tmp_path / CARDS[0]).read_bytes() == (b"Unrelated card edit" if drift == "edited" else CARDS[0].encode())
    assert (tmp_path / CARDS[1]).read_bytes() == CARDS[1].encode()
    if drift == "edited":
        assert (tmp_path / "USER.md").read_bytes() == b"Edited"
    elif drift == "linked":
        assert (tmp_path / "USER.md").is_symlink()
        assert (tmp_path / "copy.md").read_bytes() == BEFORE


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
def test_finish_treats_a_linked_change_path_as_drift(tmp_path, monkeypatch):
    source(tmp_path)
    finish = journal.finish_prepared

    def linked(target, decision_id):
        link_source(target)
        return finish(target, decision_id)

    monkeypatch.setattr(journal, "finish_prepared", linked)
    move(tmp_path)
    assert_abandoned(tmp_path)
    assert (tmp_path / "USER.md").is_symlink()
    assert (tmp_path / "copy.md").read_bytes() == BEFORE


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
def test_linked_file_after_prepare_does_not_block_recovery(tmp_path, monkeypatch):
    crash(tmp_path, monkeypatch)
    copy = link_source(tmp_path)
    assert journal.resolve_prepared(tmp_path)[0]["phase"] == "abandoned"
    assert_abandoned(tmp_path)
    assert journal.resolve_prepared(tmp_path) == []
    memory_inbox_drain.drain(tmp_path, today=date(2026, 11, 14), deadline_days=30)
    assert (tmp_path / "USER.md").is_symlink()
    assert copy.read_bytes() == BEFORE


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
def test_linked_guard_path_abandons_after_a_crash(tmp_path, monkeypatch):
    source(tmp_path)
    for p in CARDS:
        path = tmp_path / p
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(p.encode())
    execute = interrupt(monkeypatch, "finish")
    with pytest.raises(RuntimeError, match="crash"):
        journal.commit_phased(
            tmp_path,
            kind="instructions",
            reason="reconcile",
            card_id=None,
            decision_id=ID,
            changes=[journal.Change(p, p.encode(), None) for p in CARDS],
            prepared_paths=[],
            hub={},
            metadata={"document_undo": True, "append_guards": {"USER.md": journal.kernel.content_digest(BEFORE)}},
        )
    monkeypatch.setattr(journal, "_execute_phase", execute)
    copy = link_source(tmp_path)
    assert journal.resolve_prepared(tmp_path)[0]["phase"] == "abandoned"
    assert all((tmp_path / p).read_bytes() == p.encode() for p in CARDS)
    assert (tmp_path / "USER.md").is_symlink()
    assert copy.read_bytes() == BEFORE
    assert journal.resolve_prepared(tmp_path) == []


def test_generated_id_preserves_creation_timestamp_precision(tmp_path, monkeypatch):
    stamp = datetime(2026, 11, 14, 12, 0, 0, 123456, tzinfo=timezone.utc)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return stamp

    monkeypatch.setattr(journal, "datetime", Clock)
    decision = journal.commit_phased(
        tmp_path, kind="instructions", reason="test", card_id=None, changes=[], prepared_paths=[], hub={}
    )
    assert decision.created_at == stamp.isoformat()
