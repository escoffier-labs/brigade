from tests.memory_read_fixtures import automatic_archive_test_mode as automatic_archive_test_mode
from tests.memory_read_fixtures import windows_scheduler as windows_scheduler
from datetime import date

from tests.memory_read_fixtures import coverage

from tests.memory_read_fixtures import signed_memory_authority as signed_memory_authority

from brigade import memory_card_reads as reads, memory_care_retire as retire, memory_care_tend as tend, memory_cmd


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


def test_round14_retire_overlap_skips_only_card(tmp_path, monkeypatch):
    from brigade import memory_lifecycle

    monkeypatch.setattr(memory_lifecycle, "_references", lambda *args: {})
    from brigade import memory_care_journal as journal

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
    assert "projection_blocked" in result["skipped"], result
    assert result["skipped"]["projection_blocked"] == [
        {
            "path": "memory/cards/a.md",
            "reason": (f"unfinished projection {operation_id} overlaps the planned destination set"),
        }
    ]
    assert path.exists()
