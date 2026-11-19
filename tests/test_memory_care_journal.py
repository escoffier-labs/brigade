from datetime import datetime, timezone
import os

import pytest

from brigade import memory_care_journal as journal
from tests._handoff_origin_fixtures import requires_automatic_filing


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


@pytest.mark.parametrize("sink", ["card", "document"])
@pytest.mark.parametrize("claim", ["missing", "invalid", pytest.param("consumed", marks=requires_automatic_filing)])
@pytest.mark.parametrize("draft_missing", [False, True])
def test_legacy_prepared_review_without_origin_is_abandoned(tmp_path, monkeypatch, sink, claim, draft_missing):
    from brigade import handoff_origin, receipt_signing
    from brigade.memory_inbox_append import plan_append

    draft = "memory/handoff-inbox/legacy.md"
    resolved = "memory/handoff-inbox/resolved/legacy.md"
    data = b"Unsigned review draft for a person.\n"
    path = tmp_path / draft
    path.parent.mkdir(parents=True)
    path.write_bytes(data)
    changes = [journal.Change(resolved, None, data), journal.Change(draft, data, None)]
    metadata = {}
    destination = "memory/cards/legacy.md" if sink == "card" else "TOOLS.md"
    if sink == "card":
        changes.insert(0, journal.Change(destination, None, b"Unapproved card content.\n"))
    else:
        metadata["appends"] = [plan_append(destination, b"Unapproved document content.\n", "document", draft=draft)]
    if claim == "invalid":
        metadata["origin_stamp_mac"] = "invalid"
    elif claim == "consumed":
        receipt_signing.generate_key(tmp_path)
        assert handoff_origin.stamp_review_draft(
            tmp_path, path, data, handoff_origin.ReviewDraft("document-destination-unwritable", False, True)
        )
        review = handoff_origin.verify_review_draft(tmp_path, path, data)
        assert review is not None
        metadata["origin_stamp_mac"] = review.stamp.consumed.name
        assert handoff_origin.claim_review_stamp(tmp_path, review.stamp.consumed.name, "another-decision")

    prepared = []

    def crash(target, decision_id):
        prepared.append(decision_id)
        raise RuntimeError("old release stopped after prepare")

    with monkeypatch.context() as patch:
        patch.setattr(journal, "finish_prepared", crash)
        with pytest.raises(RuntimeError, match="old release stopped"):
            journal.commit_phased(
                tmp_path,
                kind="inbox",
                reason="retry:document-destination-unwritable",
                card_id=None,
                changes=changes,
                prepared_paths=[resolved],
                hub={},
                metadata=metadata,
            )
    (decision_id,) = prepared
    decision = journal.load(tmp_path, decision_id)
    assert not (tmp_path / destination).exists()
    assert journal._payload(tmp_path, decision.decision_id)["phase"] == "prepared"
    if draft_missing:
        path.unlink()
    recovered = journal.resolve_prepared(tmp_path)
    assert not (tmp_path / destination).exists()
    assert path.read_bytes() == data
    payload = journal._payload(tmp_path, decision.decision_id)
    assert payload["phase"] == payload["status"] == "abandoned"
    assert payload["abandoned_reason"] == "review origin stamp missing or unavailable"
    assert recovered == [
        {
            "decision_id": decision.decision_id,
            "phase": "abandoned",
            "reason": "review origin stamp missing or unavailable",
        }
    ]
    assert journal.resolve_prepared(tmp_path) == []


@pytest.mark.parametrize("review_derived", [False, True])
def test_prepared_append_draft_marker_requires_origin(tmp_path, monkeypatch, review_derived):
    from brigade.memory_inbox_append import plan_append

    draft = tmp_path / "memory/handoff-inbox/review.md"
    draft.parent.mkdir(parents=True)
    draft.write_bytes(b"Review this draft.\n")
    block = b"Document content.\n"
    append = plan_append("TOOLS.md", block, "document")
    if review_derived:
        append["draft"] = "memory/handoff-inbox/review.md"
    prepared = []

    def crash(target, decision_id):
        prepared.append(decision_id)
        raise RuntimeError("stopped after prepare")

    with monkeypatch.context() as patch:
        patch.setattr(journal, "finish_prepared", crash)
        with pytest.raises(RuntimeError, match="stopped after prepare"):
            journal.commit_phased(
                tmp_path,
                kind="inbox",
                reason="append",
                card_id=None,
                changes=[],
                prepared_paths=[],
                hub={},
                metadata={"appends": [append]},
            )
    journal.resolve_prepared(tmp_path)
    (decision_id,) = prepared
    payload = journal._payload(tmp_path, decision_id)
    if review_derived:
        assert not (tmp_path / "TOOLS.md").exists()
        assert payload["phase"] == "abandoned"
        assert payload["abandoned_reason"] == "review origin stamp missing or unavailable"
    else:
        assert (tmp_path / "TOOLS.md").read_bytes() == block
        assert payload["phase"] == "committed"
    assert draft.read_bytes() == b"Review this draft.\n"


@pytest.mark.parametrize("sink", ["document", "card-append"])
@pytest.mark.parametrize("claim", ["missing", "invalid", "unowned", "foreign", "owned"])
@pytest.mark.parametrize("draft_missing", [False, True])
def test_legacy_committed_review_pending_append_requires_owned_origin(
    tmp_path, monkeypatch, sink, claim, draft_missing
):
    from brigade import handoff_origin
    from brigade.memory_inbox_append import plan_append

    if os.name != "posix" and claim in {"foreign", "owned"}:
        pytest.skip(requires_automatic_filing.kwargs["reason"])
    draft = "memory/handoff-inbox/legacy-committed.md"
    resolved = "memory/handoff-inbox/resolved/legacy-committed.md"
    data = b"Unsigned review draft for a person.\n"
    path = tmp_path / draft
    path.parent.mkdir(parents=True)
    path.write_bytes(data)
    destination = "TOOLS.md" if sink == "document" else "memory/cards/tools-overflow.md"
    (tmp_path / destination).parent.mkdir(parents=True, exist_ok=True)
    from tests._instruction_fixtures import CARD_HEAD

    before = (CARD_HEAD.encode() if sink == "card-append" else b"") + b"Existing content.\n"
    (tmp_path / destination).write_bytes(before)
    block = b"Unapproved appended content.\n"

    def legacy_finish(target, decision_id):
        journal._execute_phase(target, decision_id, "legacyfinish", [journal.Change(draft, data, None)])
        journal.annotate(target, decision_id, phase="committed", status="committed")
        return journal.load(target, decision_id)

    with monkeypatch.context() as patch:
        patch.setattr(journal, "finish_prepared", legacy_finish)
        decision = journal.commit_phased(
            tmp_path,
            kind="inbox",
            reason="retry:document-destination-unwritable",
            card_id=None,
            changes=[journal.Change(resolved, None, data), journal.Change(draft, data, None)],
            prepared_paths=[resolved],
            hub={},
            metadata={"appends": [plan_append(destination, block, sink, draft=draft, created=False)]},
        )
    if not draft_missing:
        path.write_bytes(data)
    mac = "a" * 64
    if claim != "missing":
        journal.annotate(tmp_path, decision.decision_id, origin_stamp_mac="invalid" if claim == "invalid" else mac)
    if claim in {"foreign", "owned"}:
        (tmp_path / ".brigade/handoffs").mkdir(parents=True, exist_ok=True)
        assert handoff_origin.claim_review_stamp(
            tmp_path, mac, decision.decision_id if claim == "owned" else "other-decision"
        )
    result = journal.resolve_prepared(tmp_path)
    payload = journal._payload(tmp_path, decision.decision_id)
    if claim == "owned":
        assert (tmp_path / destination).read_bytes() == before + block
        assert payload["appends_complete"]
    else:
        assert (tmp_path / destination).read_bytes() == before
        assert path.read_bytes() == data
        assert payload["append_refused"] == "origin-review-required: review origin stamp missing or unavailable"
        assert result == [
            {
                "decision_id": decision.decision_id,
                "phase": "append-refused",
                "reason": "review origin stamp missing or unavailable",
            }
        ]
    assert journal.resolve_prepared(tmp_path) == []


@pytest.mark.parametrize("excluded", [False, True])
def test_round14_automatic_holds_conflict_and_guard(tmp_path, monkeypatch, excluded):
    (tmp_path / "a.md").write_bytes(b"old a")
    (tmp_path / "b.md").write_bytes(b"old b")
    saved = []

    def interrupted(target, decision_id):
        saved.append(decision_id)
        raise RuntimeError("interrupted")

    with monkeypatch.context() as patch:
        patch.setattr(journal, "finish_prepared", interrupted)
        with pytest.raises(RuntimeError, match="interrupted"):
            journal.commit_phased(
                tmp_path,
                kind="fix",
                reason="test",
                card_id=None,
                changes=[journal.Change("a.md", b"old a", b"new a"), journal.Change("b.md", b"old b", b"new b")],
                prepared_paths=["a.md"],
                hub={},
                metadata={"append_guards": {"b.md": journal.kernel.content_digest(b"old b")}} if excluded else {},
            )
    if not excluded:
        (tmp_path / "b.md").write_bytes(b"edited")
    result = journal.resolve_prepared(tmp_path, automatic=True)
    assert result[0]["phase"] == "operator_required"
    assert journal._payload(tmp_path, saved[0])["phase"] == "prepared"
    assert (tmp_path / "a.md").read_bytes() == b"new a"
    if not excluded:
        # The hold sticks after the conflicting bytes return to their captured state.
        (tmp_path / "b.md").write_bytes(b"old b")
        again = journal.resolve_prepared(tmp_path, automatic=True)
        assert again[0]["phase"] == "operator_required"
        assert journal._payload(tmp_path, saved[0])["phase"] == "prepared"
        (tmp_path / "b.md").write_bytes(b"edited")
        journal.resolve_prepared(tmp_path, decision_id=saved[0])
        assert (tmp_path / "a.md").read_bytes() == b"old a"
        assert (tmp_path / "b.md").read_bytes() == b"edited"
        final = journal._payload(tmp_path, saved[0])
        assert final["phase"] == "abandoned"
        assert final["abandoned_reason"]
        assert "restorable" in final


def test_automatic_recovery_holds_a_projection_blocked_decision(tmp_path, monkeypatch):
    (tmp_path / "a.md").write_bytes(b"old a")
    saved = []

    def interrupted(target, decision_id):
        saved.append(decision_id)
        raise RuntimeError("interrupted")

    with monkeypatch.context() as patch:
        patch.setattr(journal, "finish_prepared", interrupted)
        with pytest.raises(RuntimeError, match="interrupted"):
            journal.commit_phased(
                tmp_path,
                kind="fix",
                reason="test",
                card_id=None,
                changes=[journal.Change("a.md", b"old a", b"new a")],
                prepared_paths=[],
                hub={},
                metadata={},
            )

    def blocked(*args, **kwargs):
        raise journal.kernel.OverlapBlockedError("unfinished projection other-op overlaps a.md")

    monkeypatch.setattr(journal, "_execute_phase", blocked)
    result = journal.resolve_prepared(tmp_path, automatic=True)
    assert result[0]["decision_id"] == saved[0]
    assert result[0]["phase"] == "operator_required"
    assert "other-op" in result[0]["reason"]
    assert journal._payload(tmp_path, saved[0])["phase"] == "prepared"
    assert (tmp_path / "a.md").read_bytes() == b"old a"
    # Clearing the blocking projection does not let a later automatic run finish it.
    monkeypatch.undo()
    again = journal.resolve_prepared(tmp_path, automatic=True)
    assert again[0]["phase"] == "operator_required"
    assert journal._payload(tmp_path, saved[0])["phase"] == "prepared"
    assert (tmp_path / "a.md").read_bytes() == b"old a"
    journal.resolve_prepared(tmp_path, decision_id=saved[0])
    assert journal._payload(tmp_path, saved[0])["phase"] != "prepared"


def _leave_prepared(target, monkeypatch, changes, *, prepared_paths=()):
    """Leave a decision prepared where a crash interrupted its finish phase."""
    saved = []

    def interrupted(target, decision_id):
        saved.append(decision_id)
        raise RuntimeError("interrupted")

    with monkeypatch.context() as patch:
        patch.setattr(journal, "finish_prepared", interrupted)
        with pytest.raises(RuntimeError, match="interrupted"):
            journal.commit_phased(
                target,
                kind="fix",
                reason="test",
                card_id=None,
                changes=changes,
                prepared_paths=list(prepared_paths),
                hub={},
            )
    return saved[0]


def _conflict_hold(target, monkeypatch):
    """Hold a prepared decision whose unprepared path changed after capture."""
    (target / "a.md").write_bytes(b"old a")
    (target / "b.md").write_bytes(b"old b")
    decision_id = _leave_prepared(
        target,
        monkeypatch,
        [journal.Change("a.md", b"old a", b"new a"), journal.Change("b.md", b"old b", b"new b")],
        prepared_paths=["a.md"],
    )
    (target / "b.md").write_bytes(b"edited")
    assert journal.resolve_prepared(target, automatic=True)[0]["phase"] == "operator_required"
    return decision_id


def test_automatic_hold_write_failure_keeps_the_reason_and_writes_nothing(tmp_path, monkeypatch):
    (tmp_path / "a.md").write_bytes(b"old a")
    (tmp_path / "b.md").write_bytes(b"old b")
    decision_id = _leave_prepared(
        tmp_path,
        monkeypatch,
        [journal.Change("a.md", b"old a", b"new a"), journal.Change("b.md", b"old b", b"new b")],
        prepared_paths=["a.md"],
    )
    (tmp_path / "b.md").write_bytes(b"edited")

    def unwritable(path, data):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(journal.store, "write_private", unwritable)
    result = journal.resolve_prepared(tmp_path, automatic=True)
    reason = result[0]["reason"]
    assert result[0]["phase"] == "operator_required"
    assert "changed or read-only paths require operator recovery" in reason
    assert "automatic hold not recorded" in reason
    assert "No space left on device" in reason
    assert "unreadable recovery operations" not in reason
    assert journal._payload(tmp_path, decision_id)["phase"] == "prepared"
    assert (tmp_path / "b.md").read_bytes() == b"edited"
    monkeypatch.undo()
    assert not journal._automatic_hold(tmp_path, decision_id).exists()


def test_automatic_hold_marker_names_the_blocking_projection_and_echoes_it(tmp_path, monkeypatch):
    (tmp_path / "a.md").write_bytes(b"old a")
    decision_id = _leave_prepared(tmp_path, monkeypatch, [journal.Change("a.md", b"old a", b"new a")])
    blocker = journal.kernel.OperationSummary("projection-blocker-0001", "committing", ())
    monkeypatch.setattr(journal.kernel, "unfinished_operations", lambda target: [blocker])

    def blocked(*args, **kwargs):
        raise journal.kernel.OverlapBlockedError(
            "unfinished projection projection-blocker-0001 overlaps the planned destination set"
        )

    monkeypatch.setattr(journal, "_execute_phase", blocked)
    result = journal.resolve_prepared(tmp_path, automatic=True)
    assert result[0]["phase"] == "operator_required"
    assert "projection-blocker-0001" in result[0]["reason"]
    marker = journal._automatic_hold(tmp_path, decision_id).read_text(encoding="utf-8")
    assert "blocking_operation: projection-blocker-0001" in marker
    assert "blocking_status: committing" in marker
    assert journal._payload(tmp_path, decision_id)["phase"] == "prepared"
    assert (tmp_path / "a.md").read_bytes() == b"old a"
    # A later run echoes the recorded blocker instead of a constant reason.
    again = journal.resolve_prepared(tmp_path, automatic=True)
    assert again[0]["phase"] == "operator_required"
    assert "projection-blocker-0001" in again[0]["reason"]
    assert "committing" in again[0]["reason"]
    assert journal._payload(tmp_path, decision_id)["phase"] == "prepared"
    assert (tmp_path / "a.md").read_bytes() == b"old a"


def test_dangling_symlink_at_the_hold_marker_counts_as_held(tmp_path, monkeypatch):
    (tmp_path / "a.md").write_bytes(b"old a")
    decision_id = _leave_prepared(tmp_path, monkeypatch, [journal.Change("a.md", b"old a", b"new a")])
    marker = journal._directory(tmp_path, decision_id) / "automatic-hold"
    try:
        marker.symlink_to(tmp_path / "missing-target")
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    result = journal.resolve_prepared(tmp_path, automatic=True)
    assert result[0]["phase"] == "operator_required"
    assert "symlink" in result[0]["reason"]
    assert journal._payload(tmp_path, decision_id)["phase"] == "prepared"
    assert (tmp_path / "a.md").read_bytes() == b"old a"
    assert marker.is_symlink()


def test_named_recovery_commit_clears_the_hold_marker(tmp_path, monkeypatch):
    decision_id = _conflict_hold(tmp_path, monkeypatch)
    assert journal._automatic_hold(tmp_path, decision_id).exists()
    # Automatic recovery never clears the marker, even when the bytes return.
    (tmp_path / "b.md").write_bytes(b"old b")
    assert journal.resolve_prepared(tmp_path, automatic=True)[0]["phase"] == "operator_required"
    assert journal._automatic_hold(tmp_path, decision_id).exists()
    result = journal.resolve_prepared(tmp_path, decision_id=decision_id)
    assert result[0]["phase"] == "committed"
    assert journal._payload(tmp_path, decision_id)["phase"] == "committed"
    assert (tmp_path / "a.md").read_bytes() == b"new a"
    assert (tmp_path / "b.md").read_bytes() == b"new b"
    assert not journal._automatic_hold(tmp_path, decision_id).exists()


def test_named_recovery_abandon_clears_the_hold_marker(tmp_path, monkeypatch):
    decision_id = _conflict_hold(tmp_path, monkeypatch)
    assert journal._automatic_hold(tmp_path, decision_id).exists()
    result = journal.resolve_prepared(tmp_path, decision_id=decision_id)
    assert result[0]["phase"] == "abandoned"
    assert journal._payload(tmp_path, decision_id)["phase"] == "abandoned"
    assert not journal._automatic_hold(tmp_path, decision_id).exists()
