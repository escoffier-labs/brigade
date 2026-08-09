"""Regression coverage for completed-ledger diagnosis and sanctioned repair (#639)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from brigade import cli, localio, outcome, outcome_cmd, outcome_repair

from tests.work_cmd_test_helpers import _init_git_repo


def _signed_row(record: outcome.OutcomeRecord, prev_digest: str | None) -> dict:
    row = outcome_cmd._record_payload(record)
    row["prev_digest"] = prev_digest
    row["digest"] = localio.canonical_json_digest(row, exclude_keys={"digest"})
    return row


def _write_rows(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows))


def _seed_two_valid(tmp_path: Path) -> tuple[Path, list[dict]]:
    first = outcome.OutcomeRecord("skill-x", "skill", "t0", "verify", 1, "ref-0", "2026-06-20T00:00:00+00:00")
    second = outcome.OutcomeRecord("skill-x", "skill", "t1", "verify", 1, "ref-1", "2026-06-20T01:00:00+00:00")
    outcome_cmd.append_records(tmp_path, [first, second])
    path = tmp_path / "memory" / "outcome" / "records.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return path, rows


def test_diagnose_duplicate_record_chain_break(tmp_path):
    path, rows = _seed_two_valid(tmp_path)
    # Competing writers: append a near-identical payload that still points at the
    # first digest instead of the second (classic duplicate-writer break).
    duplicate = dict(rows[1])
    duplicate["prev_digest"] = rows[0]["digest"]
    duplicate["evidence_ref"] = rows[1]["evidence_ref"]
    duplicate["digest"] = localio.canonical_json_digest(duplicate, exclude_keys={"digest"})
    _write_rows(path, [rows[0], rows[1], duplicate])

    break_info = outcome_repair.diagnose_completed_ledger(path)
    assert break_info is not None
    assert break_info.kind == "digest_chain_break"
    assert break_info.line_no == 3
    assert break_info.expected_prev == rows[1]["digest"]
    assert break_info.actual_prev == rows[0]["digest"]
    assert break_info.suspected_cause == "duplicate-writer records"


def test_diagnose_truncated_line_break(tmp_path):
    path, rows = _seed_two_valid(tmp_path)
    path.write_bytes(path.read_bytes() + b'{"artifact_id":"skill-x","digest":"pending"')

    break_info = outcome_repair.diagnose_completed_ledger(path)
    assert break_info is not None
    assert break_info.kind == "incomplete_trailing"
    assert break_info.line_no == 3
    assert (
        break_info.valid_prefix_bytes
        == (json.dumps(rows[0], sort_keys=True) + "\n").encode() + (json.dumps(rows[1], sort_keys=True) + "\n").encode()
    )
    assert break_info.suspected_cause == "incomplete trailing record"


def test_doctor_reports_completed_ledger_chain_break(tmp_path, capsys):
    path, rows = _seed_two_valid(tmp_path)
    broken = _signed_row(
        outcome.OutcomeRecord("skill-x", "skill", "t2", "verify", 1, "ref-2", "2026-06-20T02:00:00+00:00"),
        prev_digest="not-the-previous-digest",
    )
    _write_rows(path, [rows[0], rows[1], broken])

    assert cli.main(["outcome", "doctor", "--target", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["completed_ledger"]["status"] == "corrupt"
    assert payload["completed_ledger"]["line_no"] == 3
    assert payload["completed_ledger"]["kind"] == "digest_chain_break"
    assert "outcome repair" in payload["completed_ledger"]["repair_command"]
    assert "--operator-confirm" not in payload["completed_ledger"]["repair_command"]
    assert payload["completed_ledger"]["repair_requires_operator_confirm"] is True

    assert cli.main(["outcome", "doctor", "--target", str(tmp_path)]) == 0
    text = capsys.readouterr().out
    assert "completed_ledger: CORRUPT line=3" in text
    assert "repair: brigade outcome repair (requires explicit operator confirmation)" in text
    assert "--operator-confirm" not in text


def test_repair_requires_operator_confirmation(tmp_path, capsys):
    path, rows = _seed_two_valid(tmp_path)
    broken = _signed_row(
        outcome.OutcomeRecord("skill-x", "skill", "t2", "verify", 1, "ref-2", "2026-06-20T02:00:00+00:00"),
        prev_digest="wrong",
    )
    _write_rows(path, [rows[0], rows[1], broken])
    before = path.read_bytes()

    assert cli.main(["outcome", "repair", "--target", str(tmp_path)]) == 2
    err = capsys.readouterr().err
    assert "operator confirmation is required" in err
    assert path.read_bytes() == before


def test_repair_quarantines_rechains_tail_and_reverify(tmp_path):
    path, rows = _seed_two_valid(tmp_path)
    original_prefix = path.read_bytes()
    broken = _signed_row(
        outcome.OutcomeRecord("skill-x", "skill", "t2", "verify", 1, "ref-2", "2026-06-20T02:00:00+00:00"),
        prev_digest=rows[0]["digest"],
    )
    # Make the broken payload match row 1 so diagnosis flags duplicate-writer.
    for key in ("artifact_id", "artifact_kind", "task_id", "source", "signal_value", "evidence_ref", "ts"):
        broken[key] = rows[1][key]
    broken["digest"] = localio.canonical_json_digest(broken, exclude_keys={"digest"})
    _write_rows(path, [rows[0], rows[1], broken])
    original = path.read_bytes()

    assert outcome_repair.repair(target=tmp_path, operator_confirmed=True, json_output=False) == 0

    repaired_rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    assert len(repaired_rows) == 4
    assert repaired_rows[0] == rows[0]
    assert repaired_rows[1] == rows[1]
    assert repaired_rows[2]["task_id"] == broken["task_id"]
    assert repaired_rows[2]["prev_digest"] == rows[1]["digest"]
    assert repaired_rows[2]["digest"] == localio.canonical_json_digest(repaired_rows[2], exclude_keys={"digest"})
    assert repaired_rows[3]["source"] == "ledger-repair"
    assert repaired_rows[3]["signal_value"] == 0
    assert repaired_rows[3]["prev_digest"] == repaired_rows[2]["digest"]
    assert repaired_rows[3]["digest"] == localio.canonical_json_digest(repaired_rows[3], exclude_keys={"digest"})
    assert path.read_bytes().startswith(original_prefix)

    quarantine_dirs = list((tmp_path / ".brigade" / "outcome" / "repairs").iterdir())
    assert len(quarantine_dirs) == 1
    quarantine = quarantine_dirs[0] / "original.jsonl"
    invalid = quarantine_dirs[0] / "invalid-segment.jsonl"
    record = quarantine_dirs[0] / "record.json"
    assert quarantine.read_bytes() == original
    assert invalid.read_bytes() == (json.dumps(broken, sort_keys=True) + "\n").encode()
    audit = json.loads(record.read_text())
    assert audit["kind"] == "digest_chain_break"
    assert audit["suspected_cause"] == "duplicate-writer records"
    assert audit["break_line"] == 3
    assert audit["re_chained_record_count"] == 1
    assert audit["re_chained_line_ranges"] == [[3, 3]]

    assert outcome_cmd._validate_completed_ledger(path) == repaired_rows[3]["digest"]
    assert outcome_repair.diagnose_completed_ledger(path) is None


def test_repair_rechains_valid_records_after_break(tmp_path):
    path, rows = _seed_two_valid(tmp_path)
    broken = _signed_row(
        outcome.OutcomeRecord("skill-x", "skill", "t2", "verify", 1, "ref-2", "2026-06-20T02:00:00+00:00"),
        prev_digest="wrong",
    )
    tail = _signed_row(
        outcome.OutcomeRecord("skill-x", "skill", "t3", "verify", 1, "ref-3", "2026-06-20T03:00:00+00:00"),
        prev_digest=broken["digest"],
    )
    _write_rows(path, [rows[0], rows[1], broken, tail])

    assert outcome_repair.repair(target=tmp_path, operator_confirmed=True) == 0

    repaired_rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    assert [row["task_id"] for row in repaired_rows] == ["t0", "t1", "t2", "t3", repaired_rows[-1]["task_id"]]
    assert repaired_rows[-1]["source"] == "ledger-repair"
    assert repaired_rows[2]["prev_digest"] == rows[1]["digest"]
    assert repaired_rows[3]["prev_digest"] == repaired_rows[2]["digest"]
    audit_path = next((tmp_path / ".brigade" / "outcome" / "repairs").glob("*/record.json"))
    audit = json.loads(audit_path.read_text())
    assert audit["re_chained_record_count"] == 2
    assert audit["re_chained_line_ranges"] == [[3, 4]]
    assert outcome_cmd._validate_completed_ledger(path) == repaired_rows[-1]["digest"]


def test_repair_rechains_from_a_first_line_break(tmp_path):
    path, rows = _seed_two_valid(tmp_path)
    first = dict(rows[0])
    first["prev_digest"] = "wrong"
    first["digest"] = localio.canonical_json_digest(first, exclude_keys={"digest"})
    third = _signed_row(
        outcome.OutcomeRecord("skill-x", "skill", "t2", "verify", 1, "ref-2", "2026-06-20T02:00:00+00:00"),
        prev_digest=rows[1]["digest"],
    )
    _write_rows(path, [first, rows[1], third])

    assert outcome_repair.repair(target=tmp_path, operator_confirmed=True) == 0

    repaired_rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    assert [row["task_id"] for row in repaired_rows[:-1]] == ["t0", "t1", "t2"]
    assert repaired_rows[0]["prev_digest"] is None
    assert repaired_rows[1]["prev_digest"] == repaired_rows[0]["digest"]
    assert repaired_rows[2]["prev_digest"] == repaired_rows[1]["digest"]
    assert outcome_cmd._validate_completed_ledger(path) == repaired_rows[-1]["digest"]


def test_repair_rechains_across_multiple_chain_breaks(tmp_path):
    path, rows = _seed_two_valid(tmp_path)
    first_break = _signed_row(
        outcome.OutcomeRecord("skill-x", "skill", "t2", "verify", 1, "ref-2", "2026-06-20T02:00:00+00:00"),
        prev_digest="wrong-a",
    )
    second_break = _signed_row(
        outcome.OutcomeRecord("skill-x", "skill", "t3", "verify", 1, "ref-3", "2026-06-20T03:00:00+00:00"),
        prev_digest="wrong-b",
    )
    tail = _signed_row(
        outcome.OutcomeRecord("skill-x", "skill", "t4", "verify", 1, "ref-4", "2026-06-20T04:00:00+00:00"),
        prev_digest=second_break["digest"],
    )
    _write_rows(path, [rows[0], rows[1], first_break, second_break, tail])

    assert outcome_repair.repair(target=tmp_path, operator_confirmed=True) == 0

    repaired_rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    assert [row["task_id"] for row in repaired_rows[:-1]] == ["t0", "t1", "t2", "t3", "t4"]
    assert all(
        repaired_rows[index]["prev_digest"] == repaired_rows[index - 1]["digest"]
        for index in range(1, len(repaired_rows))
    )
    audit_path = next((tmp_path / ".brigade" / "outcome" / "repairs").glob("*/record.json"))
    audit = json.loads(audit_path.read_text())
    assert audit["re_chained_record_count"] == 3
    assert audit["re_chained_line_ranges"] == [[3, 5]]
    assert outcome_cmd._validate_completed_ledger(path) == repaired_rows[-1]["digest"]


def test_repair_skips_binary_bytes_and_rechains_later_valid_records(tmp_path):
    path, rows = _seed_two_valid(tmp_path)
    broken = _signed_row(
        outcome.OutcomeRecord("skill-x", "skill", "t2", "verify", 1, "ref-2", "2026-06-20T02:00:00+00:00"),
        prev_digest="wrong",
    )
    tail = _signed_row(
        outcome.OutcomeRecord("skill-x", "skill", "t3", "verify", 1, "ref-3", "2026-06-20T03:00:00+00:00"),
        prev_digest=broken["digest"],
    )
    path.write_bytes(
        b"".join(
            [
                *(json.dumps(row, sort_keys=True).encode() + b"\n" for row in rows),
                json.dumps(broken, sort_keys=True).encode() + b"\n",
                b"\xff\n",
                json.dumps(tail, sort_keys=True).encode() + b"\n",
            ]
        )
    )

    assert outcome_repair.repair(target=tmp_path, operator_confirmed=True) == 0

    repaired_rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    assert [row["task_id"] for row in repaired_rows[:-1]] == ["t0", "t1", "t2", "t3"]
    record_path = next((tmp_path / ".brigade" / "outcome" / "repairs").glob("*/record.json"))
    audit = json.loads(record_path.read_text())
    assert audit["re_chained_line_ranges"] == [[3, 3], [5, 5]]
    assert outcome_cmd._validate_completed_ledger(path) == repaired_rows[-1]["digest"]


def test_repair_keeps_the_original_tail_range_when_chain_break_precedes_partial_bytes(tmp_path):
    path, rows = _seed_two_valid(tmp_path)
    broken = _signed_row(
        outcome.OutcomeRecord("skill-x", "skill", "t2", "verify", 1, "ref-2", "2026-06-20T02:00:00+00:00"),
        prev_digest="wrong",
    )
    partial = b'{"artifact_id":"partial"'
    _write_rows(path, [rows[0], rows[1], broken])
    path.write_bytes(path.read_bytes() + partial)

    assert outcome_repair.repair(target=tmp_path, operator_confirmed=True) == 0

    record_path = next((tmp_path / ".brigade" / "outcome" / "repairs").glob("*/record.json"))
    audit = json.loads(record_path.read_text())
    quarantine = record_path.parent / "original.jsonl"
    assert audit["invalid_segment_end"] == 4
    assert quarantine.read_bytes().endswith(partial)


def test_capture_degrades_with_bounded_error_and_writes_nothing(tmp_path, capsys):
    _init_git_repo(tmp_path)
    path, rows = _seed_two_valid(tmp_path)
    broken = _signed_row(
        outcome.OutcomeRecord("skill-x", "skill", "t2", "verify", 1, "ref-2", "2026-06-20T02:00:00+00:00"),
        prev_digest="wrong",
    )
    _write_rows(path, [rows[0], rows[1], broken])
    before = path.read_bytes()
    _write_verify_receipt = tmp_path / ".brigade" / "work" / "verify-runs" / "v1"
    _write_verify_receipt.mkdir(parents=True)
    (_write_verify_receipt / "receipt.json").write_text(
        json.dumps(
            {
                "run_id": "v1",
                "target": str(tmp_path),
                "status": "completed",
                "started_at": "2026-06-20T03:00:00+00:00",
                "completed_at": "2026-06-20T03:00:00+00:00",
                "commands": [],
                "path": str(_write_verify_receipt),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )

    rc = outcome_cmd.capture(target=tmp_path, artifact_id="skill-x", run_id="v1")
    captured = capsys.readouterr()
    assert rc == 1
    assert "ledger corrupt at line 3" in captured.err
    assert "brigade outcome doctor" in captured.err
    assert "--operator-confirm" not in captured.err
    assert path.read_bytes() == before


def test_post_repair_capture_succeeds(tmp_path, capsys):
    _init_git_repo(tmp_path)
    path, rows = _seed_two_valid(tmp_path)
    broken = _signed_row(
        outcome.OutcomeRecord("skill-x", "skill", "t2", "verify", 1, "ref-2", "2026-06-20T02:00:00+00:00"),
        prev_digest="wrong",
    )
    _write_rows(path, [rows[0], rows[1], broken])
    assert outcome_repair.repair(target=tmp_path, operator_confirmed=True) == 0

    run_dir = tmp_path / ".brigade" / "work" / "verify-runs" / "v2"
    run_dir.mkdir(parents=True)
    (run_dir / "receipt.json").write_text(
        json.dumps(
            {
                "run_id": "v2",
                "target": str(tmp_path),
                "status": "completed",
                "started_at": "2026-06-20T04:00:00+00:00",
                "completed_at": "2026-06-20T04:00:00+00:00",
                "commands": [],
                "path": str(run_dir),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )

    assert outcome_cmd.capture(target=tmp_path, artifact_id="skill-x", run_id="v2") == 0
    out = capsys.readouterr().out
    assert "outcome capture: skill-x" in out
    assert outcome_repair.diagnose_completed_ledger(path) is None
    final_rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    assert final_rows[-1]["evidence_ref"].endswith("receipt.json")
    assert final_rows[-1]["prev_digest"] == final_rows[-2]["digest"]


def test_repair_quarantines_incomplete_trailing_bytes_before_recovery(tmp_path, capsys):
    path, rows = _seed_two_valid(tmp_path)
    prefix = path.read_bytes()
    path.write_bytes(prefix + b'{"artifact_id":"truncated"')

    assert outcome_repair.repair(target=tmp_path, operator_confirmed=True) == 0
    out = capsys.readouterr().out
    assert "ledger healthy" in out
    assert path.read_bytes() == prefix
    assert outcome_repair.diagnose_completed_ledger(path) is None
    rows_after = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    assert rows_after == rows
    quarantine = next((tmp_path / ".brigade" / "outcome" / "repairs").glob("*/original.jsonl"))
    assert quarantine.read_bytes() == prefix + b'{"artifact_id":"truncated"'


def test_publish_exclusive_bytes_preserves_non_utf8_bytes(tmp_path):
    path = tmp_path / "quarantine" / "original.jsonl"
    original = b'\xff\r\n{"partial":"tail"}'

    outcome_repair._publish_exclusive_bytes(path, original)

    assert path.read_bytes() == original
    with pytest.raises(FileExistsError):
        outcome_repair._publish_exclusive_bytes(path, b"replacement")
    assert path.read_bytes() == original
