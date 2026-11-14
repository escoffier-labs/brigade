"""Recovery index and abandoned undo regressions using real inbox producers."""

import pytest

from brigade import memory_care_journal as journal, memory_care_tend as tend
from brigade import memory_inbox_drain as drain
from tests.test_memory_inbox_drain import draft, TODAY, DOCUMENT_BODY


def test_abandoned_document_undo_can_retry(tmp_path, monkeypatch):
    draft(tmp_path, "document-destination-unwritable", DOCUMENT_BODY)
    drain.drain(tmp_path, today=TODAY, deadline_days=14)
    original = journal.entries(tmp_path)[0]
    finish = journal.finish_prepared
    config = tmp_path / ".brigade/memory-care.toml"

    def protect(target, decision_id):
        config.write_text('read_only_paths = ["USER.md"]\n', encoding="utf-8")
        return finish(target, decision_id)

    monkeypatch.setattr(journal, "finish_prepared", protect)
    undo = tend.undo(tmp_path, original.decision_id)
    assert journal._payload(tmp_path, undo.decision_id)["phase"] == "abandoned"
    assert journal.load(tmp_path, original.decision_id).undone_by is None
    config.write_text("", encoding="utf-8")
    monkeypatch.setattr(journal, "finish_prepared", finish)
    retry = tend.undo(tmp_path, original.decision_id)
    assert journal.load(tmp_path, original.decision_id).undone_by == retry.decision_id
    assert not (tmp_path / "USER.md").exists()


@pytest.mark.parametrize("boundary", ["prepare-before", "prepare-after", "finish-before", "finish-after", "append"])
def test_pending_index_matches_scan_after_crash(tmp_path, monkeypatch, boundary):
    from brigade import memory_care_pending as pending, ingest

    draft(tmp_path, "document-destination-unwritable", DOCUMENT_BODY)
    execute = journal._execute_phase
    append = ingest._append_document_nofollow

    class Crash(BaseException):
        pass

    def interrupt(target, decision_id, phase, changes):
        if boundary == phase + "-before":
            raise Crash()
        execute(target, decision_id, phase, changes)
        if boundary == phase + "-after":
            raise Crash()

    def interrupted_append(*args, **kwargs):
        raise Crash()

    monkeypatch.setattr(journal, "_execute_phase", interrupt)
    if boundary == "append":
        monkeypatch.setattr(ingest, "_append_document_nofollow", interrupted_append)
    with pytest.raises(Crash):
        drain.drain(tmp_path, today=TODAY, deadline_days=14)
    monkeypatch.setattr(journal, "_execute_phase", execute)
    monkeypatch.setattr(ingest, "_append_document_nofollow", append)
    assert pending.load(tmp_path) == pending.scan(tmp_path)
    journal.resolve_prepared(tmp_path)
    assert pending.load(tmp_path) == pending.scan(tmp_path)
    for content in (None, b"broken"):
        path = tmp_path / pending.INDEX_REL
        if content is None:
            path.unlink()
        else:
            path.write_bytes(content)
        assert pending.load(tmp_path) == pending.scan(tmp_path)


def test_completed_decisions_not_reread_by_recovery(tmp_path, monkeypatch):
    from brigade import memory_care_pending as pending

    draft(tmp_path, "document-destination-unwritable", DOCUMENT_BODY)
    drain.drain(tmp_path, today=TODAY, deadline_days=14)
    pending.load(tmp_path)
    payload = journal._payload

    def refuse(target, decision_id):
        raise AssertionError("completed record reread")

    monkeypatch.setattr(journal, "_payload", refuse)
    journal.resolve_prepared(tmp_path)
    monkeypatch.setattr(journal, "_payload", payload)
