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


@pytest.mark.parametrize("payload", ['{"phase":"prepared"}', "{}"])
@pytest.mark.parametrize("route", ["pending", "kernel-refused"])
def test_partial_inflight_record_is_quarantined(tmp_path, monkeypatch, payload, route):
    crash(tmp_path, monkeypatch, "finish", 1)
    record = tmp_path / journal.JOURNAL_REL / ID / "decision.json"
    record.write_text(payload)
    if route == "pending":
        # Hide operations only from the kernel-recovery loop, leaving the real
        # unfinished operation available when the pending row is classified.
        unfinished = journal.kernel.unfinished_operations
        calls = 0

        def pending_only(target):
            nonlocal calls
            calls += 1
            return [] if calls == 2 else unfinished(target)

        monkeypatch.setattr(journal.kernel, "unfinished_operations", pending_only)
    else:
        (tmp_path / "USER.md").write_bytes(b"drift")
    result = journal.resolve_prepared(tmp_path)
    assert result[0]["phase"] == "quarantined"
    assert result[0]["reason"] == "unreadable decision record quarantined"
    assert not record.exists()
    assert journal.resolve_prepared(tmp_path) == []


def test_damaged_history_is_reported_without_quarantine(tmp_path):
    source(tmp_path)
    move(tmp_path)
    record = tmp_path / journal.JOURNAL_REL / ID / "decision.json"
    record.write_bytes(b"broken history")
    result = journal.resolve_prepared(tmp_path)
    assert result == [{"decision_id": ID, "phase": "unreadable", "reason": "unreadable decision record retained"}]
    assert record.read_bytes() == b"broken history"
    assert not list(record.parent.glob("decision.quarantined-*.json"))


def test_pending_skips_non_decision_directories(tmp_path):
    directory = tmp_path / journal.JOURNAL_REL / "not-a-decision"
    directory.mkdir(parents=True)
    (directory / "decision.json").write_text("{}")
    assert journal.resolve_prepared(tmp_path) == []


def test_undo_prints_quarantine_reason(tmp_path, monkeypatch, capsys):
    from brigade import memory_care_tend

    valid = journal.commit(
        tmp_path,
        kind="fix",
        reason="test",
        card_id=None,
        changes=[journal.Change("other.md", None, b"other")],
        hub={},
    )
    crash(tmp_path, monkeypatch, "finish", 1)
    (tmp_path / journal.JOURNAL_REL / ID / "decision.json").write_text("{}")
    memory_care_tend.undo(tmp_path, valid.decision_id)
    assert "unreadable decision record quarantined" in capsys.readouterr().err


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
@pytest.mark.parametrize("linked", ["journal", "decision.json"])
@pytest.mark.parametrize("caller", ["tend", "undo", "drain"])
def test_journal_symlink_named_error(tmp_path, monkeypatch, capsys, linked, caller):
    from argparse import Namespace
    from brigade import memory_care_tend

    crash(tmp_path, monkeypatch)
    path = tmp_path / journal.JOURNAL_REL
    if linked == "decision.json":
        path = path / ID / linked
    saved = tmp_path / "saved"
    path.rename(saved)
    path.symlink_to(saved, target_is_directory=linked == "journal")
    if caller == "drain":
        error = memory_inbox_drain.drain(tmp_path, today=date(2026, 11, 14), deadline_days=30)["error"]
    else:
        args = Namespace(
            memory_care_command=caller, target=tmp_path, decision_id=ID, stage=[], dry_run=False, json=True
        )
        assert memory_care_tend.dispatch(args) == 1
        error = capsys.readouterr().err
    assert "symlink path:" in error
    assert journal.JOURNAL_REL in error
    assert "the memory-care journal contains a symlink; inspect and remove it" in error
    assert path.is_symlink()


def test_refused_restore_reports_all_unrestored_destinations(tmp_path, monkeypatch):
    crash(tmp_path, monkeypatch, "prepare", 2)
    operation = journal.kernel.unfinished_operations(tmp_path)[0]
    op = journal.store.read_json(journal.kernel.operation_dir(tmp_path, operation.operation_id) / "journal.json")
    committed = [op["mutations"][n]["display_path"] for n in op["committed_indexes"]]
    (tmp_path / committed[-1]).write_bytes(b"drift")
    result = journal.resolve_prepared(tmp_path)[0]
    assert result["committed_destinations"] == committed
    assert result["unrestored_destinations"] == committed


def test_sweep_reports_quarantined_retention(tmp_path, monkeypatch):
    from brigade.memory_inbox_sweep import sweep

    crash(tmp_path, monkeypatch, "finish", 1)
    record = tmp_path / journal.JOURNAL_REL / ID / "decision.json"
    record.write_text("{}")
    journal.resolve_prepared(tmp_path)
    assert sweep(tmp_path)["retentions"] == [{"decision_id": ID, "reason": "quarantined decision record"}]


@pytest.mark.parametrize("field", list(journal.Decision.__dataclass_fields__))
@pytest.mark.parametrize("damage", ["missing", "wrong-type"])
def test_finish_validates_decision_fields_before_writes(tmp_path, monkeypatch, field, damage):
    crash(tmp_path, monkeypatch)
    p = journal._payload(tmp_path, ID)
    if damage == "missing":
        del p[field]
    else:
        p[field] = 42
    journal.store.write_json(tmp_path / journal.JOURNAL_REL / ID / "decision.json", p)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    with pytest.raises(journal.JournalConflict, match=f"invalid decision record: {field}$"):
        journal.finish_prepared(tmp_path, ID)
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


def _fix(target):
    return journal.commit(
        target,
        kind="fix",
        reason="test",
        card_id=None,
        changes=[journal.Change("other.md", None, b"other")],
        hub={},
    )


def test_unphased_commit_records_leave_nothing_pending(tmp_path, monkeypatch):
    from brigade import memory_care_pending

    _fix(tmp_path)
    entered = lock_spy(monkeypatch)
    assert memory_care_pending.scan(tmp_path) == {}
    assert journal.resolve_prepared(tmp_path) == []
    assert entered == []


def test_undo_without_appends_leaves_nothing_pending(tmp_path, monkeypatch):
    from brigade import memory_care_pending

    decision = _fix(tmp_path)
    journal.revert(tmp_path, decision.decision_id, reason="undo")
    entered = lock_spy(monkeypatch)
    assert memory_care_pending.scan(tmp_path) == {}
    assert journal.resolve_prepared(tmp_path) == []
    assert journal.resolve_prepared(tmp_path) == []
    assert entered == []


def test_unreadable_kernel_journal_still_quarantines(tmp_path, monkeypatch):
    crash(tmp_path, monkeypatch, "finish", 1)
    (tmp_path / journal.JOURNAL_REL / ID / "decision.json").write_text("{}")
    operation = journal.kernel.unfinished_operations(tmp_path)[0]

    def refuse(operation_id, **kw):
        (journal.kernel.operation_dir(tmp_path, operation_id) / "journal.json").write_text("[]")
        raise journal.kernel.ProjectionError("unreadable kernel journal")

    monkeypatch.setattr(journal.kernel, "recover", refuse)
    monkeypatch.setattr(journal.kernel, "supersede", lambda *a, **kw: None)
    result = journal.resolve_prepared(tmp_path)
    assert result[0]["decision_id"] == ID and result[0]["phase"] == "quarantined"
    assert result[0]["committed_destinations"] == []
    assert operation.operation_id.startswith(f"care-{ID}-")
    assert not (tmp_path / journal.JOURNAL_REL / ID / "decision.json").exists()


@pytest.mark.parametrize("damage", [None, [True], ["yes", False, False], "missing"])
def test_finish_validates_before_absent(tmp_path, monkeypatch, damage):
    crash(tmp_path, monkeypatch)
    p = journal._payload(tmp_path, ID)
    if damage == "missing":
        del p["before_absent"]
    else:
        p["before_absent"] = damage
    journal.store.write_json(tmp_path / journal.JOURNAL_REL / ID / "decision.json", p)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    with pytest.raises(journal.JournalConflict, match="invalid decision record: before_absent$"):
        journal.finish_prepared(tmp_path, ID)
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


def test_drain_reports_kernel_projection_error(tmp_path, monkeypatch):
    def refuse(target, *, automatic=False):
        raise journal.kernel.ProjectionError("kernel journal unreadable")

    monkeypatch.setattr(journal, "resolve_prepared", refuse)
    result = memory_inbox_drain.drain(tmp_path, today=date(2026, 11, 14), deadline_days=30)
    assert result["error"] == "kernel journal unreadable"


def test_rows_cached_before_a_classifier_change_are_rebuilt(tmp_path):
    import hashlib
    import json

    from brigade import memory_care_pending

    source(tmp_path)
    move(tmp_path)
    record = tmp_path / journal.JOURNAL_REL / ID / "decision.json"
    p = journal._payload(tmp_path, ID)
    del p["before_absent"]
    journal.store.write_json(record, p)
    stat = record.stat()
    records = {
        ID: {
            "stamp": [stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino],
            "row": dict.fromkeys(("prepared", "document", "repair", "append"), False),
        }
    }
    digest = hashlib.sha256(json.dumps(records, sort_keys=True).encode("utf-8")).hexdigest()
    journal.store.write_json(tmp_path / memory_care_pending.INDEX_REL, {"records": records, "sha256": digest})
    assert memory_care_pending.load(tmp_path)[ID]["document"] is True
    index = journal.store.read_json(tmp_path / memory_care_pending.INDEX_REL)
    assert index["version"] == memory_care_pending.INDEX_VERSION


def test_a_phased_record_that_lost_its_phase_is_not_exempt(tmp_path):
    from brigade import memory_care_pending

    source(tmp_path)
    move(tmp_path)
    p = journal._payload(tmp_path, ID)
    del p["phase"]
    p["status"] = "committed"
    journal.store.write_json(tmp_path / journal.JOURNAL_REL / ID / "decision.json", p)
    assert memory_care_pending.scan(tmp_path)[ID]["document"] is True


def test_commit_interrupted_at_pending_leaves_nothing_for_care_recovery(tmp_path, monkeypatch):
    from brigade import memory_care_pending

    class Killed(BaseException):
        pass

    def killed(plan, **kw):
        raise Killed()

    monkeypatch.setattr(journal.kernel, "execute", killed)
    with pytest.raises(Killed):
        _fix(tmp_path)
    monkeypatch.undo()
    (record,) = (tmp_path / journal.JOURNAL_REL).glob("*/decision.json")
    assert journal.store.read_json(record)["status"] == "pending"
    entered = lock_spy(monkeypatch)
    assert memory_care_pending.scan(tmp_path) == {}
    assert journal.resolve_prepared(tmp_path) == []
    assert entered == []
