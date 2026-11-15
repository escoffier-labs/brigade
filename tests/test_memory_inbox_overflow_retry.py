"""Review drafts held by an earlier release for a full instruction file drain into overflow cards."""

from __future__ import annotations

import threading
from datetime import date

import pytest

from brigade import ingest, memory_care_tend, memory_inbox_retry
from brigade import memory_care_journal as journal
from brigade import memory_inbox_drain as drain
from tests._home import set_home
from tests._instruction_fixtures import CARD_HEAD, INJECTION, field_workspace, handoff_text, sized, write_handoff

TODAY = date(2026, 11, 13)
NOTE = "Prefer one-line status updates."
CARD = "memory/cards/instructions/user-md--overflow-preference.md"


def _held(ws, content=NOTE, *, flags=False, number=0):
    """A draft held by an earlier release, built the way tests/test_memory_inbox_drain.py builds one."""
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
    return sorted((ws / drain.INBOX_REL).glob("*.md"))[-1]


def test_retry_flags_route_old_budget_holds():
    text = "<!-- routed from a.md\n     reason: bootstrap-budget-exceeded\n     routed-at: 20261101T000000Z -->\n"
    assert drain.retry_flags("bootstrap-budget-exceeded", text) == (False, True)
    assert drain.classify("bootstrap-budget-exceeded", text) == "retry"


def test_the_new_success_code_is_a_hold_and_needs_no_evidence_proof():
    assert "document-routed-to-card" in drain.HOLD_CODES
    assert drain.NEEDS_EVIDENCE_PROOF["document-routed-to-card"] is False
    assert "bootstrap-budget-exceeded" in drain.RETRY_CODES and "bootstrap-budget-exceeded" not in drain.HOLD_CODES


@pytest.mark.parametrize("flags", [False, True])
def test_old_budget_hold_lands_in_the_overflow_card_on_the_next_drain(tmp_path, monkeypatch, flags):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)  # USER.md is 8241 bytes
    draft = _held(ws, flags=flags)
    result = drain.drain(ws, today=TODAY, deadline_days=14)
    assert result["retried"] == 1, result
    assert (ws / CARD).read_text().endswith("\nPrefer one-line status updates.\n")
    assert not draft.exists() and (ws / "USER.md").stat().st_size == 8241


def test_second_retry_appends_to_the_existing_card(tmp_path, monkeypatch):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    _held(ws, NOTE, number=0)
    _held(ws, "Prefer UTC timestamps.", number=1)
    result = drain.drain(ws, today=TODAY, deadline_days=14)
    assert result["retried"] == 2, result  # both retries counted
    card = (ws / CARD).read_text()
    assert "archive: never" in card and NOTE in card and card.endswith("\n\nPrefer UTC timestamps.\n")


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


def test_old_budget_hold_with_an_injection_marker_stays_for_a_person(tmp_path, monkeypatch):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    draft = _held(ws, INJECTION)
    assert drain.classify("bootstrap-budget-exceeded", draft.read_text()) == "human"
    drain.drain(ws, today=TODAY, deadline_days=14)
    assert draft.exists() and not (ws / "memory/cards/instructions").exists()


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


def test_retry_decision_undoes_by_exact_block(tmp_path, monkeypatch):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    action = memory_inbox_retry.reprocess(ws, _held(ws))
    assert action.kind == "routed"
    memory_care_tend.undo(ws, action.summary)  # retry puts the decision id in summary, memory_inbox_retry.py:152
    assert not (ws / CARD).exists()


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


def test_retry_of_a_card_append_keeps_the_card_private(tmp_path, monkeypatch):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    memory_inbox_retry.reprocess(ws, _held(ws, "Secret-ish preference."))
    head = (ws / CARD).read_text().split("\n---\n", 1)[0]
    assert "visibility: private\n" in head + "\n" and "archive: never" in head


def test_retry_with_an_invalid_instructions_table_still_routes_the_handoff(tmp_path, monkeypatch):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    sized(ws / "USER.md", 100)
    (ws / ".brigade").mkdir()
    (ws / ".brigade" / "memory-care.toml").write_text('[instructions]\nact_at_percent = "x"\n')
    action = memory_inbox_retry.reprocess(ws, _held(ws))
    assert action.kind == "routed" and (ws / CARD).is_file()
    assert (ws / "USER.md").stat().st_size == 100


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


def _refuse_retry_at_write(ws, monkeypatch, before_write=None):
    original = ingest._append_document_nofollow

    def refuse(owner, path, text, **kwargs):
        if before_write is not None:
            before_write()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("untrusted card\n")
        return original(owner, path, text, **kwargs)

    monkeypatch.setattr(ingest, "_append_document_nofollow", refuse)


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
    assert "resolved-copy-digest-mismatch" in journal._payload(ws, retry.decision_id)["append_refused"]


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

    for _ in range(2):
        journal.resolve_prepared(ws)
    assert draft.read_bytes() == original
    assert drain.pending(ws) == [draft]
    assert not list((ws / drain.RESOLVED_REL).glob("*.md"))
    cleanups = [item for item in journal.entries(ws) if item.reason.startswith("append-refused:")]
    assert len(cleanups) == 1
    memory_care_tend.undo(ws, cleanups[0].decision_id)
    assert not draft.exists()
    assert [path.read_bytes() for path in (ws / drain.RESOLVED_REL).glob("*.md")] == [original]


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
