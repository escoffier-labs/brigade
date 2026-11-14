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


def test_every_returned_card_is_recorded_as_read(tmp_path):
    from brigade import memory_cmd

    matches = [{"card_id": f"card-{n}", "card_aliases": [f"alias-{n}"]} for n in range(8)]
    memory_cmd._record_search_log(tmp_path, query="something specific", matches=matches)
    ledger = reads.load(tmp_path)
    for n in range(8):
        assert reads.last_read(ledger, [f"card-{n}"]) is not None
        assert reads.last_read(ledger, [f"alias-{n}"]) is not None


def test_reads_are_recorded_even_when_the_search_is_not_logged(tmp_path):
    from brigade import memory_cmd

    assert not memory_cmd._should_log_search("")
    memory_cmd._record_search_log(tmp_path, query="", matches=[{"card_id": "seen"}])
    assert reads.last_read(reads.load(tmp_path), ["seen"]) is not None
