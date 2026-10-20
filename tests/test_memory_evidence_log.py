"""Storage contracts for the body-free memory evidence ledger."""

import json
import os
from pathlib import Path

import pytest

from brigade import memory_proposal_io as io

POSIX_IO = os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW")
CARD_ID = "card-12345678-1234-4123-8123-123456789abc"
LOG = ".brigade/memory/reflect/evidence-log.jsonl"


def _card(target: Path) -> Path:
    card = target / "memory/cards/example.md"
    card.parent.mkdir(parents=True)
    card.write_text(f"---\nid: {CARD_ID}\n---\nPrivate card body.\n")
    return card


@pytest.mark.skipif(not POSIX_IO, reason="proposal IO requires safe directory descriptors")
def test_append_is_body_free_append_only_and_does_not_write_card(tmp_path):
    from brigade.memory_evidence_log import append_evidence, read_evidence

    card = _card(tmp_path)
    before = card.stat(), card.read_bytes()
    for kind in ("near-match", "opposite-polarity", "reinforced", "outcome"):
        append_evidence(tmp_path, card_path=card, evidence_id=f"evidence-{kind}", kind=kind, similarity=0.95)
    rows, warnings, unsupported = read_evidence(tmp_path)
    assert unsupported is False
    assert warnings == 0
    assert len(rows) == 4
    assert {row["kind"] for row in rows} == {"near-match", "opposite-polarity", "reinforced", "outcome"}
    assert rows[0]["card_id"] == CARD_ID
    assert rows[0]["card_path"] == "memory/cards/example.md"
    assert set(rows[0]) == {"ts", "card_id", "card_path", "evidence_id", "kind", "similarity"}
    assert b"Private card body" not in (tmp_path / LOG).read_bytes()
    assert card.read_bytes() == before[1]
    assert card.stat().st_mtime_ns == before[0].st_mtime_ns


@pytest.mark.skipif(not POSIX_IO, reason="proposal IO requires safe directory descriptors")
@pytest.mark.parametrize("unsafe", ["symlink", "hardlink", "fifo", "directory", "parent-symlink"])
def test_log_refuses_nonregular_or_linked_storage(tmp_path, unsafe):
    from brigade.memory_evidence_log import append_evidence, read_evidence

    card = _card(tmp_path)
    log = tmp_path / LOG
    log.parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.write_bytes(b"untouched")
    if unsafe == "symlink":
        log.symlink_to(outside)
    elif unsafe == "hardlink":
        os.link(outside, log)
    elif unsafe == "fifo":
        os.mkfifo(log)
    elif unsafe == "directory":
        log.mkdir()
    else:
        log.parent.rmdir()
        log.parent.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises((OSError, ValueError)):
        append_evidence(tmp_path, card_path=card, evidence_id="e-1", kind="near-match", similarity=0.95)
    assert read_evidence(tmp_path) == ([], 1, False)
    assert outside.read_bytes() == b"untouched"


def test_log_fails_closed_without_directory_descriptors(tmp_path, monkeypatch, capsys):
    from brigade import memory_cmd
    from brigade.memory_evidence_log import append_evidence

    card = _card(tmp_path)
    monkeypatch.setattr(io.os, "supports_dir_fd", set())
    with pytest.raises(OSError, match="safe directory descriptors unavailable"):
        append_evidence(tmp_path, card_path=card, evidence_id="e-1", kind="near-match", similarity=0.95)
    assert memory_cmd.scan(target=tmp_path, json_output=True) == 0
    captured = capsys.readouterr()
    summary = json.loads(captured.out)["evidence_log"]
    assert summary["unsupported"] is True
    assert summary["row_count"] == 0
    assert summary["warning_count"] == 0
    assert "invalid line(s)" not in captured.err
    assert not (tmp_path / LOG).exists()


@pytest.mark.skipif(not POSIX_IO, reason="proposal IO requires safe directory descriptors")
def test_reader_counts_invalid_and_oversized_lines_and_keeps_later_rows(tmp_path):
    from brigade.memory_evidence_log import MAX_LINE_BYTES, append_evidence, read_evidence

    card = _card(tmp_path)
    append_evidence(tmp_path, card_path=card, evidence_id="e-1", kind="near-match", similarity=0.95)
    log = tmp_path / LOG
    valid = log.read_bytes()
    row = json.loads(valid)
    row["similarity"] = 10**1000
    log.write_bytes(
        valid + b"{broken\n" + b"x" * (MAX_LINE_BYTES * 3) + b"\n" + json.dumps(row).encode() + b"\n" + valid
    )
    rows, warnings, unsupported = read_evidence(tmp_path)
    assert unsupported is False
    assert len(rows) == 2
    assert warnings == 3
    before = log.read_bytes()
    with pytest.raises(ValueError):
        append_evidence(tmp_path, card_path=card, evidence_id="x" * MAX_LINE_BYTES, kind="near-match", similarity=0.95)
    assert log.read_bytes() == before


@pytest.mark.skipif(not POSIX_IO, reason="proposal IO requires safe directory descriptors")
@pytest.mark.parametrize("cutoff", ["boundary", "partial-row", "oversized-row"])
def test_reader_bounds_scan_to_newest_bytes(tmp_path, monkeypatch, cutoff):
    from brigade import memory_evidence_log as ledger

    card = _card(tmp_path)
    ledger.append_evidence(tmp_path, card_path=card, evidence_id="old", kind="near-match", similarity=0.95)
    log = tmp_path / LOG
    old = log.read_bytes()
    ledger.append_evidence(tmp_path, card_path=card, evidence_id="new", kind="near-match", similarity=0.95)
    new = log.read_bytes()[len(old) :]
    if cutoff == "oversized-row":
        prefix = b"x" * (ledger.MAX_LINE_BYTES * 3) + b"\n"
    else:
        prefix = old * 20
    data = prefix + new * 2
    log.write_bytes(data)
    budget = len(new) * 2 + (0 if cutoff == "boundary" else 17)
    monkeypatch.setattr(ledger, "MAX_SCAN_BYTES", budget, raising=False)
    before = log.stat().st_mtime_ns

    rows, warnings, unsupported = ledger.read_evidence(tmp_path)

    assert [row["evidence_id"] for row in rows] == ["new", "new"]
    assert warnings == 1
    assert unsupported is False
    assert log.read_bytes() == data
    assert log.stat().st_mtime_ns == before
