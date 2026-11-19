from tests.memory_read_fixtures import automatic_archive_test_mode as automatic_archive_test_mode
from tests.memory_read_fixtures import windows_scheduler as windows_scheduler
from datetime import date

from tests.memory_read_fixtures import coverage

from tests.memory_read_fixtures import signed_memory_authority as signed_memory_authority

from brigade import (
    memory_card_reads as reads,
    memory_care_journal as journal,
    memory_care_retire as retire,
    memory_care_tend as tend,
    memory_cmd,
    memory_lifecycle,
)


def test_retire_requires_old_ledger_expiry_and_no_recent_read(tmp_path):
    path = tmp_path / "memory/cards/old.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\ncard_id: old\nexpires_at: 2026-01-01\n---\nEnded\n")
    today = date(2026, 11, 12)
    cfg, tc = memory_cmd.MemoryCareConfig(), tend.TendConfig()
    assert retire.plan(tmp_path, cfg, tc, today=today)["candidates"] == []
    coverage(tmp_path, date(2026, 1, 1), date(2026, 11, 12))
    assert len(retire.plan(tmp_path, cfg, tc, today=today)["candidates"]) == 1
    reads.record(tmp_path, ["old"], today=today)
    assert retire.plan(tmp_path, cfg, tc, today=today)["candidates"] == []


def test_fresh_until_does_not_mean_ended(tmp_path):
    path = tmp_path / "memory/cards/old.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\nfresh_until: 2026-01-01\n---\nOngoing\n")
    coverage(tmp_path, date(2026, 1, 1), date(2026, 11, 12))
    assert (
        retire.apply(tmp_path, memory_cmd.MemoryCareConfig(), tend.TendConfig(), today=date(2026, 11, 12))["archived"]
        == 0
    )


def test_blast_cap_and_deterministic_expiry(tmp_path):
    cards = tmp_path / "memory/cards"
    cards.mkdir(parents=True)
    for n in range(25):
        (cards / f"{n}.md").write_text(f"---\ncard_id: card-{n}\nexpires: 2026-01-01\n---\nEnded\n")
    coverage(tmp_path, date(2026, 1, 1), date(2026, 11, 12))
    payload = retire.apply(tmp_path, memory_cmd.MemoryCareConfig(), tend.TendConfig(), today=date(2026, 11, 12))
    assert payload["archived"] == 20
    assert (
        len(
            retire.plan(tmp_path, memory_cmd.MemoryCareConfig(), tend.TendConfig(), today=date(2026, 11, 12))[
                "candidates"
            ]
        )
        == 5
    )


def _unfinished_operation(target, path, operation_id):
    """Leave a real unfinished kernel operation that owns one destination."""
    plan = journal.kernel.build_plan(
        operation_id=operation_id,
        projector="fixture",
        source_fingerprint="test",
        mutations=[
            journal.kernel.mutation(
                destination=path,
                mutation="create",
                expected_before=journal.kernel.content_digest(None),
                desired_after=journal.kernel.content_digest(b"held"),
                staged_bytes=b"held",
            )
        ],
        target=target,
    )
    journal.kernel.write_crash_fixture(plan, target=target, committed_count=0)


def test_round14_retire_overlap_skips_only_card(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_lifecycle, "_references", lambda *args: {})

    cards = tmp_path / "memory/cards"
    cards.mkdir(parents=True)
    for name in ("a", "b"):
        (cards / f"{name}.md").write_text(f"---\ncard_id: {name}\nexpires: 2026-01-01\n---\nEnded\n")
    today = date(2026, 11, 12)
    coverage(tmp_path, date(2026, 1, 1), today)
    operation_id = "care-20261112T000000Z-archive-12345678-prepare"
    path = cards / "a.md"
    plan = journal.kernel.build_plan(
        operation_id=operation_id,
        projector="fixture",
        source_fingerprint="test",
        mutations=[
            journal.kernel.mutation(
                destination=path,
                mutation="replace",
                expected_before=journal.kernel.content_digest(path.read_bytes()),
                desired_after=journal.kernel.content_digest(b"held"),
                staged_bytes=b"held",
            )
        ],
        target=tmp_path,
    )
    journal.kernel.write_crash_fixture(plan, target=tmp_path, committed_count=0)
    result = retire.apply(tmp_path, memory_cmd.MemoryCareConfig(), tend.TendConfig(), today=today)
    assert result["archived"] == 1, result
    # One report per card: a projection block is not also a refusal, and skipped
    # keeps integer counters only.
    assert result["projection_blocked"] == [
        {
            "path": "memory/cards/a.md",
            "reason": (f"unfinished projection {operation_id} overlaps the planned destination set"),
        }
    ]
    assert result["refused"] == []
    assert all(isinstance(value, int) for value in result["skipped"].values()), result
    assert "projection_blocked" not in result["skipped"]
    assert path.exists()


def test_blocked_card_pass_leaves_no_empty_decision_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_lifecycle, "_references", lambda *args: {})
    card = tmp_path / "memory/cards/a.md"
    card.parent.mkdir(parents=True)
    card.write_text("---\ncard_id: a\nexpires: 2026-01-01\n---\nEnded\n")
    today = date(2026, 11, 12)
    coverage(tmp_path, date(2026, 1, 1), today)
    destination = tmp_path / "memory/archive/cards/a.md"
    undo_id = "20261112T000000Z-undo-deadbeef"
    _unfinished_operation(tmp_path, destination, undo_id)
    journal_root = tmp_path / journal.JOURNAL_REL
    for _ in range(2):
        result = retire.apply(tmp_path, memory_cmd.MemoryCareConfig(), tend.TendConfig(), today=today)
        assert result["archived"] == 0, result
        assert [row["path"] for row in result["projection_blocked"]] == ["memory/cards/a.md"]
        # The prepare phase was refused, so no decision record exists and the
        # journal must not collect one orphan directory per pass.
        assert list(journal_root.iterdir()) == []
        assert card.exists()
        assert not destination.exists()
    # The blocking undo is addressable by its own operation id, and the next
    # pass archives the card once that operation is recovered.
    recovered = journal.resolve_prepared(tmp_path, decision_id=undo_id)
    assert [item["decision_id"] for item in recovered] == [undo_id]
    assert journal.kernel.unfinished_operations(tmp_path) == []
    result = retire.apply(tmp_path, memory_cmd.MemoryCareConfig(), tend.TendConfig(), today=today)
    assert result["archived"] == 1, result
    assert not card.exists()
    assert destination.exists()
