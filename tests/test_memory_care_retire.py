from datetime import date

from brigade import memory_card_reads as reads, memory_care_retire as retire, memory_care_tend as tend, memory_cmd


def test_retire_requires_old_ledger_expiry_and_no_recent_read(tmp_path):
    path = tmp_path / "memory/cards/old.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\ncard_id: old\nexpires_at: 2026-01-01\n---\nEnded\n")
    today = date(2026, 11, 12)
    cfg, tc = memory_cmd.MemoryCareConfig(), tend.TendConfig()
    assert retire.plan(tmp_path, cfg, tc, today=today)["candidates"] == []
    reads.record(tmp_path, [], today=date(2026, 1, 1))
    assert len(retire.plan(tmp_path, cfg, tc, today=today)["candidates"]) == 1
    reads.record(tmp_path, ["old"], today=today)
    assert retire.plan(tmp_path, cfg, tc, today=today)["candidates"] == []


def test_fresh_until_does_not_mean_ended(tmp_path):
    path = tmp_path / "memory/cards/old.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\nfresh_until: 2026-01-01\n---\nOngoing\n")
    reads.record(tmp_path, [], today=date(2026, 1, 1))
    assert (
        retire.apply(tmp_path, memory_cmd.MemoryCareConfig(), tend.TendConfig(), today=date(2026, 11, 12))["archived"]
        == 0
    )


def test_blast_cap_and_deterministic_expiry(tmp_path):
    cards = tmp_path / "memory/cards"
    cards.mkdir(parents=True)
    for n in range(25):
        (cards / f"{n}.md").write_text(f"---\ncard_id: card-{n}\nexpires: 2026-01-01\n---\nEnded\n")
    reads.record(tmp_path, [], today=date(2026, 1, 1))
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
