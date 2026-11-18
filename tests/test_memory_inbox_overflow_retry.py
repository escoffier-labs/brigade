"""Review drafts held by an earlier release for a full instruction file drain into overflow cards."""

from __future__ import annotations

import re
import sys
import threading
from datetime import date

import pytest

from tests._handoff_origin_fixtures import requires_automatic_filing, windows_draft_review
from brigade import handoff_origin, ingest, memory_care_tend, memory_inbox_retry
from brigade import memory_care_journal as journal
from brigade import memory_inbox_drain as drain
from tests._home import set_home
from tests._instruction_fixtures import CARD_HEAD, INJECTION, field_workspace, handoff_text, sized, write_handoff

TODAY = date(2026, 11, 13)
NOTE = "Prefer one-line status updates."
CARD = "memory/cards/instructions/user-md--overflow-preference.md"


def _held(ws, content=NOTE, *, flags=False, number=0):
    """An authorized review draft, optionally without legacy retry flags in its header."""
    if sys.platform == "win32":
        source = ws / ".claude/memory-handoffs" / f"2026-10-01-{number}.md"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(handoff_text("USER.md", content, type_="preference"), encoding="utf-8")
    else:
        source = write_handoff(ws, f"2026-10-01-{number}.md", handoff_text("USER.md", content, type_="preference"))
    ingest._execute(
        ingest.Outcome("inboxed", code="bootstrap-budget-exceeded"),
        source,
        ws,
        {},
        ws / drain.INBOX_REL,
        source.parent / "processed",
        False,
        snapshot=source.read_bytes(),
        promote_cards=True if flags else None,
        route_documents=True if flags else None,
    )
    draft = sorted((ws / drain.INBOX_REL).glob("*.md"))[-1]
    stamped = handoff_origin.stamp_review_draft(
        ws, draft, draft.read_bytes(), handoff_origin.ReviewDraft("bootstrap-budget-exceeded", flags, True)
    )
    assert stamped == (sys.platform != "win32")
    return draft


def test_retry_flags_route_old_budget_holds():
    text = "<!-- routed from a.md\n     reason: bootstrap-budget-exceeded\n     routed-at: 20261101T000000Z -->\n"
    assert drain.retry_flags("bootstrap-budget-exceeded", text) == (False, True)
    assert drain.classify("bootstrap-budget-exceeded", text) == "retry"


def test_the_new_success_code_is_a_hold_and_needs_no_evidence_proof():
    assert "document-routed-to-card" in drain.HOLD_CODES
    assert drain.NEEDS_EVIDENCE_PROOF["document-routed-to-card"] is False
    assert "bootstrap-budget-exceeded" in drain.RETRY_CODES and "bootstrap-budget-exceeded" not in drain.HOLD_CODES


@requires_automatic_filing
@pytest.mark.parametrize("flags", [False, True])
def test_old_budget_hold_lands_in_the_overflow_card_on_the_next_drain(tmp_path, monkeypatch, flags):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)  # USER.md is 8241 bytes
    draft = _held(ws, flags=flags)
    result = drain.drain(ws, today=TODAY, deadline_days=14)
    assert result["retried"] == 1, result
    assert (ws / CARD).read_text().endswith("\nPrefer one-line status updates.\n")
    assert not draft.exists() and (ws / "USER.md").stat().st_size == 8241


@requires_automatic_filing
def test_second_retry_appends_to_the_existing_card(tmp_path, monkeypatch):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    _held(ws, NOTE, number=0)
    _held(ws, "Prefer UTC timestamps.", number=1)
    result = drain.drain(ws, today=TODAY, deadline_days=14)
    assert result["retried"] == 2, result  # both retries counted
    card = (ws / CARD).read_text()
    assert "archive: never" in card and NOTE in card and card.endswith("\n\nPrefer UTC timestamps.\n")


@requires_automatic_filing
def test_held_copy_of_published_content_resolves_without_a_second_card(tmp_path, monkeypatch):  # Review Focus 6
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    (ws / ".brigade").mkdir()
    (ws / ".brigade" / "memory-care.toml").write_text("max_card_bytes = 600\n")
    card = sized(ws / CARD, 580, head=CARD_HEAD)
    with card.open("a", encoding="utf-8") as handle:
        handle.write("\n\n" + NOTE + "\n")  # 614 bytes: card 1 is full
    _held(ws)
    result = drain.drain(ws, today=TODAY, deadline_days=14)
    assert result["resolved"] == 1, result
    assert [p.name for p in (ws / "memory/cards/instructions").iterdir()] == ["user-md--overflow-preference.md"]


@requires_automatic_filing
def test_old_budget_hold_with_an_injection_marker_stays_for_a_person(tmp_path, monkeypatch):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    draft = _held(ws, INJECTION)
    assert drain.classify("bootstrap-budget-exceeded", draft.read_text()) == "human"
    drain.drain(ws, today=TODAY, deadline_days=14)
    assert draft.exists() and not (ws / "memory/cards/instructions").exists()


@requires_automatic_filing
def test_retry_waits_when_the_document_lock_is_busy(tmp_path, monkeypatch):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    draft = _held(ws)
    held, release = threading.Event(), threading.Event()

    def hold():
        with ingest._document_lock(ws):
            held.set()
            release.wait(5)

    worker = threading.Thread(target=hold)
    worker.start()
    held.wait(5)
    try:
        assert memory_inbox_retry.reprocess(ws, draft).summary == "document-publication-busy"
    finally:
        release.set()
        worker.join()
    assert not (ws / "memory/cards/instructions").exists()


@requires_automatic_filing
def test_retry_decision_undoes_by_exact_block(tmp_path, monkeypatch):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    action = memory_inbox_retry.reprocess(ws, _held(ws))
    assert action.kind == "routed"
    memory_care_tend.undo(ws, action.summary)  # retry puts the decision id in summary, memory_inbox_retry.py:152
    assert not (ws / CARD).exists()


@requires_automatic_filing
def test_retry_claim_failure_abandons_without_writing_card(tmp_path, monkeypatch):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    draft = _held(ws)
    data = draft.read_bytes()
    finish = journal.finish_prepared
    prepared = []

    def block_claim(target, decision_id):
        prepared.append(decision_id)
        # Verification succeeds, but the durable claim cannot be written.
        (ws / ".brigade/handoffs/consumed-stamps").write_text("not a directory")
        return finish(target, decision_id)

    monkeypatch.setattr(journal, "finish_prepared", block_claim)
    with pytest.raises(journal.JournalConflict, match="retry abandoned"):
        memory_inbox_retry.reprocess(ws, draft)
    assert not (ws / CARD).exists()
    assert draft.read_bytes() == data
    (decision_id,) = prepared
    payload = journal._payload(ws, decision_id)
    assert payload["phase"] == payload["status"] == "abandoned"
    assert payload["abandoned_reason"] == "review origin stamp missing or unavailable"
    assert journal.resolve_prepared(ws) == []


@requires_automatic_filing
def test_retry_journals_a_card_append_and_dry_run_writes_nothing(tmp_path, monkeypatch):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    draft = _held(ws)
    dry = memory_inbox_retry.reprocess(ws, draft, dry_run=True)
    assert dry.kind == "routed" and not (ws / "memory/cards/instructions").exists() and draft.exists()
    action = memory_inbox_retry.reprocess(ws, draft)
    payload = journal._payload(ws, action.summary)
    assert [item["writer"] for item in payload["appends"]] == ["card-append"]
    assert payload["appends"][0]["created"] is True and payload["appends"][0]["header_bytes"] > 0


@requires_automatic_filing
def test_retry_of_a_card_append_keeps_the_card_private(tmp_path, monkeypatch):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    memory_inbox_retry.reprocess(ws, _held(ws, "Secret-ish preference."))
    head = (ws / CARD).read_text().split("\n---\n", 1)[0]
    assert "visibility: private\n" in head + "\n" and "archive: never" in head


@requires_automatic_filing
def test_retry_with_an_invalid_instructions_table_still_routes_the_handoff(tmp_path, monkeypatch):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    sized(ws / "USER.md", 100)
    (ws / ".brigade").mkdir()
    (ws / ".brigade" / "memory-care.toml").write_text('[instructions]\nact_at_percent = "x"\n')
    action = memory_inbox_retry.reprocess(ws, _held(ws))
    assert action.kind == "routed" and (ws / CARD).is_file()
    assert (ws / "USER.md").stat().st_size == 100


@requires_automatic_filing
def test_a_committed_retry_is_not_reported_skipped_when_the_card_id_index_cannot_read_the_card(tmp_path, monkeypatch):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    draft = _held(ws)

    def unreadable(self, rel, text):
        raise OSError("card unreadable after the commit")

    monkeypatch.setattr(ingest.CardIdIndex, "register", unreadable)
    result = drain.drain(ws, today=TODAY, deadline_days=14)
    assert result["retried"] == 1 and not result.get("skipped"), result
    assert (ws / CARD).read_text().endswith("\n" + NOTE + "\n") and not draft.exists()


@pytest.mark.parametrize("flags", [False, True])
def test_unsigned_budget_hold_stays_for_a_person(tmp_path, monkeypatch, flags):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    draft = _held(ws, flags=flags)
    if windows_draft_review(ws, draft):
        return
    handoff_origin.stamp_path(ws, draft).unlink()
    result = drain.drain(ws, today=TODAY, deadline_days=14)
    assert result["kept_for_person"] == 1 and result["retried"] == 0
    assert draft.exists() and not (ws / CARD).exists()


@pytest.mark.parametrize("move", [False, True])
def test_manually_removed_review_draft_loses_retry_authority(tmp_path, monkeypatch, move):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    draft = _held(ws)
    if windows_draft_review(ws, draft):
        return
    data = draft.read_bytes()
    sidecar = handoff_origin.stamp_path(ws, draft)
    if move:
        draft.rename(draft.with_suffix(".removed"))
    else:
        draft.unlink()
    preview = drain.drain(ws, today=TODAY, deadline_days=14, dry_run=True)
    assert preview["retried"] == 0 and sidecar.exists()
    drain.drain(ws, today=TODAY, deadline_days=14)
    assert not sidecar.exists()
    draft.write_bytes(data)
    result = drain.drain(ws, today=TODAY, deadline_days=14)
    assert result["kept_for_person"] == 1 and result["retried"] == 0
    assert draft.exists() and not (ws / CARD).exists()


def _refuse_retry_at_write(ws, monkeypatch, before_write=None):
    original = ingest._append_document_nofollow

    def refuse(owner, path, text, **kwargs):
        if before_write is not None:
            before_write()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("untrusted card\n")
        return original(owner, path, text, **kwargs)

    monkeypatch.setattr(ingest, "_append_document_nofollow", refuse)


@requires_automatic_filing
def test_refused_retry_removes_resolved_copy_and_restores_inbox(tmp_path, monkeypatch):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    draft = _held(ws)
    original = draft.read_bytes()
    _refuse_retry_at_write(ws, monkeypatch)

    result = drain.drain(ws, today=TODAY, deadline_days=14)

    assert result["held"] == 1
    assert draft.read_bytes() == original
    assert not list((ws / drain.RESOLVED_REL).glob("*.md"))


@requires_automatic_filing
def test_refused_retry_keeps_changed_resolved_copy_and_reports_reason(tmp_path, monkeypatch):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    draft = _held(ws)

    def change_resolved():
        resolved = next((ws / drain.RESOLVED_REL).glob("*.md"))
        resolved.write_bytes(resolved.read_bytes() + b"\nchanged\n")

    _refuse_retry_at_write(ws, monkeypatch, change_resolved)
    result = drain.drain(ws, today=TODAY, deadline_days=14)

    assert draft.exists() and list((ws / drain.RESOLVED_REL).glob("*.md"))
    assert result["decisions"][0]["reason"] == "hold:resolved-copy-digest-mismatch"
    retry = next(item for item in journal.entries(ws) if item.reason.startswith("retry:"))
    refused = journal._payload(ws, retry.decision_id)["append_refused"]
    assert "resolved-copy-digest-mismatch" in refused
    assert re.search(r"\(cause: \S.*\)$", refused), refused


@requires_automatic_filing
def test_refused_retry_does_not_overwrite_occupied_inbox_and_cleanup_undoes(tmp_path, monkeypatch):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    draft = _held(ws)
    original = draft.read_bytes()
    occupant = b"occupying file\n"

    def occupy_inbox():
        draft.write_bytes(occupant)

    _refuse_retry_at_write(ws, monkeypatch, occupy_inbox)
    drain.drain(ws, today=TODAY, deadline_days=14)

    recovered = list(draft.parent.glob(f"{draft.stem}-append-refused-*.md"))
    assert draft.read_bytes() == occupant
    assert len(recovered) == 1 and recovered[0].read_bytes() == original
    cleanup = next(item for item in journal.entries(ws) if item.reason.startswith("append-refused:"))
    memory_care_tend.undo(ws, cleanup.decision_id)
    assert draft.read_bytes() == occupant and not recovered[0].exists()
    assert len(list((ws / drain.RESOLVED_REL).glob("*.md"))) == 1


@requires_automatic_filing
def test_refused_cited_retry_keeps_one_pending_draft(tmp_path, monkeypatch):
    from brigade import memory_evidence_log

    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    draft = _held(ws)
    original = draft.read_bytes()
    monkeypatch.setattr(
        memory_evidence_log,
        "read_evidence",
        lambda _: ([{"evidence_id": draft.relative_to(ws).as_posix()}], 0, False),
    )
    _refuse_retry_at_write(ws, monkeypatch)
    result = drain.drain(ws, today=TODAY, deadline_days=14)

    assert result["held"] == 1 and not result["skipped"], result
    assert drain.pending(ws) == [draft]
    assert draft.read_bytes() == original
    assert not list(draft.parent.glob(f"{draft.stem}-append-refused-*.md"))
    assert not list((ws / drain.RESOLVED_REL).glob("*.md"))


@pytest.mark.parametrize("step", ["finish", "annotation"])
@requires_automatic_filing
def test_refused_retry_cleanup_recovers_once_after_interruption(tmp_path, monkeypatch, step):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    draft = _held(ws)
    original = draft.read_bytes()
    _refuse_retry_at_write(ws, monkeypatch)
    execute = journal._execute_phase
    annotate = journal.annotate

    class Interrupted(BaseException):
        pass

    def interrupt_finish(target, decision_id, phase, changes):
        if phase == "finish" and journal._payload(target, decision_id).get("refusal_of"):
            raise Interrupted
        return execute(target, decision_id, phase, changes)

    def interrupt_annotation(target, decision_id, **fields):
        if "append_refused" in fields:
            raise Interrupted
        return annotate(target, decision_id, **fields)

    with monkeypatch.context() as patch:
        patch.setattr(journal, "_execute_phase", interrupt_finish if step == "finish" else execute)
        patch.setattr(journal, "annotate", interrupt_annotation if step == "annotation" else annotate)
        with pytest.raises(Interrupted):
            memory_inbox_retry.reprocess(ws, draft)

    cleanup = next(
        journal._payload(ws, path.parent.name)
        for path in (ws / journal.JOURNAL_REL).glob("*/decision.json")
        if journal._payload(ws, path.parent.name).get("refusal_of")
    )
    journal.resolve_prepared(ws, decision_id=cleanup["decision_id"])
    for _ in range(2):
        journal.resolve_prepared(ws, decision_id=cleanup["refusal_of"])
    assert draft.read_bytes() == original
    assert drain.pending(ws) == [draft]
    assert not list((ws / drain.RESOLVED_REL).glob("*.md"))
    cleanups = [item for item in journal.entries(ws) if item.reason.startswith("append-refused:")]
    assert len(cleanups) == 1
    memory_care_tend.undo(ws, cleanups[0].decision_id)
    assert not draft.exists()
    assert [path.read_bytes() for path in (ws / drain.RESOLVED_REL).glob("*.md")] == [original]


@requires_automatic_filing
def test_recovery_reports_resolved_copy_digest_mismatch(tmp_path, monkeypatch):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    draft = _held(ws)
    original = draft.read_bytes()

    def interrupt(*_args, **_kwargs):
        raise OSError("interrupted publication")

    with monkeypatch.context() as patch:
        patch.setattr(ingest, "_append_document_nofollow", interrupt)
        with pytest.raises(OSError, match="interrupted publication"):
            memory_inbox_retry.reprocess(ws, draft)
    (resolved,) = (ws / drain.RESOLVED_REL).glob("*.md")
    changed = resolved.read_bytes() + b"\nchanged\n"
    resolved.write_bytes(changed)
    _refuse_retry_at_write(ws, monkeypatch)

    recovery = journal.resolve_prepared(ws)

    assert len(recovery) == 1
    assert recovery[0]["phase"] == "append-refused"
    assert recovery[0]["reason"] == "resolved-copy-digest-mismatch"
    assert resolved.read_bytes() == changed and draft.read_bytes() == original


@pytest.mark.parametrize("record_id", ["20000101T000000Z-inbox-00000000", "not-a-decision"])
@requires_automatic_filing
def test_refused_retry_recovers_with_unrelated_unreadable_record(tmp_path, monkeypatch, record_id):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    draft = _held(ws)
    original = draft.read_bytes()

    def interrupt(*_args, **_kwargs):
        raise OSError("interrupted publication")

    with monkeypatch.context() as patch:
        patch.setattr(ingest, "_append_document_nofollow", interrupt)
        with pytest.raises(OSError, match="interrupted publication"):
            memory_inbox_retry.reprocess(ws, draft)
    assert not draft.exists()
    record = ws / journal.JOURNAL_REL / record_id / "decision.json"
    record.parent.mkdir()
    record.write_text("{malformed JSON\n")
    _refuse_retry_at_write(ws, monkeypatch)

    recovery = journal.resolve_prepared(ws)

    assert any(item["phase"] == "append-refused" for item in recovery)
    assert draft.read_bytes() == original
    assert record.read_text() == "{malformed JSON\n"
    assert not list((ws / drain.RESOLVED_REL).glob("*.md"))
    journal.resolve_prepared(ws)
    assert drain.pending(ws) == [draft]


@requires_automatic_filing
def test_refused_retry_recovers_with_read_only_resolved_copy_and_undo(tmp_path, monkeypatch):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    draft = _held(ws)
    original = draft.read_bytes()

    def interrupt(*_args, **_kwargs):
        raise OSError("interrupted publication")

    with monkeypatch.context() as patch:
        patch.setattr(ingest, "_append_document_nofollow", interrupt)
        with pytest.raises(OSError, match="interrupted publication"):
            memory_inbox_retry.reprocess(ws, draft)
    assert not draft.exists()
    (resolved,) = (ws / drain.RESOLVED_REL).glob("*.md")
    before = resolved.stat()
    (ws / ".brigade/memory-care.toml").write_text('read_only_paths = ["memory/handoff-inbox/resolved/**"]\n')
    _refuse_retry_at_write(ws, monkeypatch)

    recovery = journal.resolve_prepared(ws)

    assert recovery == [
        {"decision_id": recovery[0]["decision_id"], "phase": "append-refused", "reason": "resolved-copy-read-only"}
    ]
    assert draft.read_bytes() == original and resolved.read_bytes() == original
    assert (resolved.stat().st_ino, resolved.stat().st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    retry = journal._payload(ws, recovery[0]["decision_id"])
    assert "resolved-copy-read-only" in retry["append_refused"]
    cleanup = next(item for item in journal.entries(ws) if item.reason.startswith("append-refused:"))
    assert cleanup.paths == (draft.relative_to(ws).as_posix(),)
    assert journal._payload(ws, cleanup.decision_id)["refusal_reason"] == "resolved-copy-read-only"
    assert journal.resolve_prepared(ws) == []
    result = drain.drain(ws, today=TODAY, deadline_days=14)
    assert not result.get("error") and not result["skipped"], result
    assert draft.read_bytes() == original
    memory_care_tend.undo(ws, cleanup.decision_id)
    assert not draft.exists() and resolved.read_bytes() == original
    assert (resolved.stat().st_ino, resolved.stat().st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
