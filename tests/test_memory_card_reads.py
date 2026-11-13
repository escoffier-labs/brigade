from datetime import date

from brigade import memory_card_reads as reads


def test_record_and_corrupt_reset(tmp_path):
    reads.record(tmp_path, ["a"], today=date(2026, 1, 1))
    reads.record(tmp_path, ["b"], today=date(2026, 2, 1))
    ledger = reads.load(tmp_path)
    assert ledger.tracking_since == date(2026, 1, 1)
    assert reads.last_read(ledger, ["a", "b"]) == date(2026, 2, 1)
    (tmp_path / reads.READS_REL).write_text("broken")
    reads.record(tmp_path, ["c"], today=date(2026, 3, 1))
    assert reads.load(tmp_path).tracking_since == date(2026, 3, 1)
