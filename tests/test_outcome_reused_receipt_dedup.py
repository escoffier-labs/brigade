"""Regression coverage for reused verify receipt outcome dedup (#634)."""

from __future__ import annotations

import json
from pathlib import Path

from brigade import outcome, outcome_cmd
from brigade.work_cmd import verification


def _write_receipt(target: Path, run_id: str, *, reused_from: str | None = None, status: str = "completed") -> Path:
    run_dir = target / ".brigade" / "work" / "verify-runs" / run_id
    run_dir.mkdir(parents=True)
    receipt = {
        "run_id": run_id,
        "target": str(target),
        "status": status,
        "started_at": "2026-07-25T02:00:00+00:00",
        "completed_at": "2026-07-25T02:00:05+00:00",
        "path": str(run_dir),
        "commands": [{"command": "true", "status": status, "exit_code": 0 if status == "completed" else 1}],
        "planned_commands": ["true"],
    }
    if reused_from is not None:
        receipt["reused_from"] = reused_from
    (run_dir / "receipt.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    return run_dir


def test_capture_follows_reused_from_to_original_evidence_ref(tmp_path):
    original = _write_receipt(tmp_path, "20260725-020358-work-verify-bc82bb")
    reused = _write_receipt(
        tmp_path,
        "20260725-020602-work-verify-3622db",
        reused_from="20260725-020358-work-verify-bc82bb",
    )
    skill = tmp_path / ".claude" / "skills" / "taste"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# taste\n")

    assert outcome_cmd.capture(target=tmp_path, artifact_id="taste", run_id=reused.name) == 0
    rows = [json.loads(line) for line in (tmp_path / "memory" / "outcome" / "records.jsonl").read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["evidence_ref"] == str(original / "receipt.json")
    assert rows[0]["evidence_ref"] != str(reused / "receipt.json")


def test_score_counts_original_and_reused_receipt_as_one_signal(tmp_path):
    original = _write_receipt(tmp_path, "orig-run")
    reused = _write_receipt(tmp_path, "reuse-run", reused_from="orig-run")
    skill = tmp_path / ".claude" / "skills" / "taste"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# taste\n")

    assert outcome_cmd.capture(target=tmp_path, artifact_id="taste", run_id="orig-run") == 0
    # Second capture still appends a row (append-only), but both point at the
    # original identity so scoring collapses them to one helped signal.
    assert outcome_cmd.capture(target=tmp_path, artifact_id="taste", run_id="reuse-run") == 0
    raw = outcome_cmd.load_records(tmp_path)
    assert len(raw) == 2
    assert {record.evidence_ref for record in raw} == {str(original / "receipt.json")}
    assert str(reused / "receipt.json") not in {record.evidence_ref for record in raw}

    scored = outcome_cmd.load_scoring_records(tmp_path)
    score = outcome.score_records("taste", [r for r in scored if r.artifact_id == "taste"])
    assert score.helped == 1
    assert score.hurt == 0

    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert outcome_cmd.rank(target=tmp_path, json_output=True) == 0
    payload = json.loads(buf.getvalue())
    entry = next(item for item in payload["ranking"] if item["artifact_id"] == "taste")
    assert entry["helped"] == 1


def test_distinct_receipts_without_reused_from_remain_separate_signals(tmp_path):
    _write_receipt(tmp_path, "run-a")
    _write_receipt(tmp_path, "run-b")
    skill = tmp_path / ".claude" / "skills" / "taste"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# taste\n")

    assert outcome_cmd.capture(target=tmp_path, artifact_id="taste", run_id="run-a") == 0
    assert outcome_cmd.capture(target=tmp_path, artifact_id="taste", run_id="run-b") == 0
    scored = outcome_cmd.load_scoring_records(tmp_path)
    score = outcome.score_records("taste", [r for r in scored if r.artifact_id == "taste"])
    assert score.helped == 2


def test_canonical_verify_evidence_ref_follows_chain(tmp_path):
    _write_receipt(tmp_path, "root")
    mid = _write_receipt(tmp_path, "mid", reused_from="root")
    leaf = _write_receipt(tmp_path, "leaf", reused_from="mid")
    leaf_receipt = json.loads((leaf / "receipt.json").read_text())
    assert verification.canonical_verify_evidence_ref(tmp_path, leaf_receipt) == str(
        tmp_path / ".brigade" / "work" / "verify-runs" / "root" / "receipt.json"
    )
    mid_receipt = json.loads((mid / "receipt.json").read_text())
    assert verification.canonical_verify_evidence_ref(tmp_path, mid_receipt).endswith("/root/receipt.json")


def test_scoring_canonicalizes_legacy_rows_that_stored_reused_path(tmp_path):
    """Already-written rows keep their bytes; scoring still collapses via reused_from."""
    original = _write_receipt(tmp_path, "orig-run")
    reused = _write_receipt(tmp_path, "reuse-run", reused_from="orig-run")
    path = tmp_path / "memory" / "outcome" / "records.jsonl"
    path.parent.mkdir(parents=True)
    first = outcome.OutcomeRecord(
        "taste", "skill", "", "verify", 1, str(original / "receipt.json"), "2026-07-25T02:00:00+00:00"
    )
    second = outcome.OutcomeRecord(
        "taste", "skill", "", "verify", 1, str(reused / "receipt.json"), "2026-07-25T02:06:00+00:00"
    )
    outcome_cmd.append_records(tmp_path, [first, second])
    before = path.read_bytes()
    scored = outcome_cmd.load_scoring_records(tmp_path)
    score = outcome.score_records("taste", scored)
    assert score.helped == 1
    assert path.read_bytes() == before


def _append_legacy_reused_rows(tmp_path: Path) -> tuple[Path, Path]:
    original = _write_receipt(tmp_path, "orig-run")
    reused = _write_receipt(tmp_path, "reuse-run", reused_from="orig-run")
    (tmp_path / "memory" / "outcome").mkdir(parents=True)
    first = outcome.OutcomeRecord(
        "taste", "skill", "", "verify", 1, str(original / "receipt.json"), "2026-07-25T02:00:00+00:00"
    )
    second = outcome.OutcomeRecord(
        "taste", "skill", "", "verify", 1, str(reused / "receipt.json"), "2026-07-25T02:06:00+00:00"
    )
    outcome_cmd.append_records(tmp_path, [first, second])
    return original, reused


def test_legacy_row_canonicalization_survives_receipt_pruning(tmp_path):
    """#650: once resolved, a reused legacy row keeps collapsing after its receipt is gone."""
    import shutil

    original, _reused = _append_legacy_reused_rows(tmp_path)
    scored = outcome_cmd.load_scoring_records(tmp_path)
    assert {r.evidence_ref for r in scored} == {str(original / "receipt.json")}
    canonical_map = tmp_path / "memory" / "outcome" / "evidence-canonical.json"
    assert canonical_map.is_file()
    payload = json.loads(canonical_map.read_text())
    assert payload["schema_version"] == outcome_cmd.CANONICAL_EVIDENCE_SCHEMA_VERSION
    assert payload["refs"][str(_reused / "receipt.json")] == str(original / "receipt.json")

    shutil.rmtree(tmp_path / ".brigade" / "work" / "verify-runs")
    scored_after = outcome_cmd.load_scoring_records(tmp_path)
    score = outcome.score_records("taste", scored_after)
    assert score.helped == 1
    assert {r.evidence_ref for r in scored_after} == {str(original / "receipt.json")}


def test_unresolved_pruned_receipt_is_not_persisted_as_canonical(tmp_path):
    """A receipt that was never readable leaves no map entry, so a later restore can still resolve it."""
    import shutil

    original, reused = _append_legacy_reused_rows(tmp_path)
    shutil.rmtree(tmp_path / ".brigade" / "work" / "verify-runs")
    scored = outcome_cmd.load_scoring_records(tmp_path)
    # Silent double count is the documented pre-#650 gap when nothing was ever resolved.
    assert {r.evidence_ref for r in scored} == {str(original / "receipt.json"), str(reused / "receipt.json")}
    assert not (tmp_path / "memory" / "outcome" / "evidence-canonical.json").exists()


def test_scoring_opens_each_distinct_receipt_at_most_once(tmp_path, monkeypatch):
    """#650: rank/score/reconcile must not re-open O(rows) receipt files on every read."""
    original, reused = _append_legacy_reused_rows(tmp_path)
    # Duplicate rows for the same two receipts.
    for idx in range(3):
        outcome_cmd.append_records(
            tmp_path,
            [
                outcome.OutcomeRecord(
                    "taste", "skill", "", "verify", 1, str(reused / "receipt.json"), f"2026-07-25T03:0{idx}:00+00:00"
                )
            ],
        )
    opened: list[Path] = []
    real_read = outcome_cmd.localio.read_json_dict

    def counting_read(path: Path):
        if path.name == "receipt.json":
            opened.append(path)
        return real_read(path)

    real_verify_read = verification._verify_read_receipt

    def counting_verify_read(path: Path):
        opened.append(path)
        return real_verify_read(path)

    monkeypatch.setattr(outcome_cmd.localio, "read_json_dict", counting_read)
    monkeypatch.setattr(verification, "_verify_read_receipt", counting_verify_read)
    outcome_cmd.load_scoring_records(tmp_path)
    first_pass = len(opened)
    # Two distinct refs resolved once each (the reused_from hop enumerates the
    # receipts dir); never once per ledger row on every read.
    assert 1 <= first_pass <= 4

    opened.clear()
    outcome_cmd.load_scoring_records(tmp_path)
    assert opened == []  # second read is served entirely from the persisted map


def test_capture_records_reused_receipt_provenance_on_the_row(tmp_path):
    """#650: the ledger row keeps the reused receipt path, not just the canonical one."""
    original = _write_receipt(tmp_path, "orig-run")
    reused = _write_receipt(tmp_path, "reuse-run", reused_from="orig-run")
    skill = tmp_path / ".claude" / "skills" / "taste"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# taste\n")

    assert outcome_cmd.capture(target=tmp_path, artifact_id="taste", run_id="orig-run") == 0
    assert outcome_cmd.capture(target=tmp_path, artifact_id="taste", run_id="reuse-run") == 0
    rows = [json.loads(line) for line in (tmp_path / "memory" / "outcome" / "records.jsonl").read_text().splitlines()]
    assert "reused_evidence_ref" not in rows[0]
    assert rows[1]["evidence_ref"] == str(original / "receipt.json")
    assert rows[1]["reused_evidence_ref"] == str(reused / "receipt.json")
    records = outcome_cmd.load_records(tmp_path)
    assert records[0].reused_evidence_ref is None
    assert records[1].reused_evidence_ref == str(reused / "receipt.json")
