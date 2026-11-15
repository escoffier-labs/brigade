"""Review drafts held by an earlier release for a full instruction file drain into overflow cards."""

from __future__ import annotations

import threading
from datetime import date

import pytest

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
    assert handoff_origin.stamp_review_draft(
        ws, draft, draft.read_bytes(), handoff_origin.ReviewDraft("bootstrap-budget-exceeded", flags, True)
    )
    return draft


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


@pytest.mark.parametrize("flags", [False, True])
def test_unsigned_budget_hold_stays_for_a_person(tmp_path, monkeypatch, flags):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    draft = _held(ws, flags=flags)
    handoff_origin.stamp_path(ws, draft).unlink()
    result = drain.drain(ws, today=TODAY, deadline_days=14)
    assert result["kept_for_person"] == 1 and result["retried"] == 0
    assert draft.exists() and not (ws / CARD).exists()


@pytest.mark.parametrize("move", [False, True])
def test_manually_removed_review_draft_loses_retry_authority(tmp_path, monkeypatch, move):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    draft = _held(ws)
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
