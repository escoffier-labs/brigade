"""Failure signals retain identifiers and reserve verification capacity."""

import pytest

from brigade import memory_lesson_rows as lessons
from brigade import memory_evidence_log as log
from tests.test_memory_verify_select import NOW, TODAY, entry, head, key
from brigade import memory_verify_select as pick

REF = {"scope": "repo:verify", "card_id": "card-example", "hash": "a" * 12}


def test_failed_run_is_deduplicated_and_body_free(tmp_path):
    receipt = {
        "run_id": "run-example",
        "status": "failed",
        "evidence_brief": {"memory_refs": [REF]},
        "task": "PRIVATE CARD BODY",
    }
    lessons.log_run_receipt(tmp_path, receipt)
    lessons.log_run_receipt(tmp_path, receipt)
    rows, warnings, _ = log.read_evidence(tmp_path)
    assert warnings == 0
    assert len(rows) == 1
    assert rows[0]["kind"] == "negative-outcome"
    assert "PRIVATE CARD BODY" not in (tmp_path / log.LOG_PATH).read_text()
    assert lessons.pending_keys(tmp_path, [head(REF["card_id"])]) == {key(REF["card_id"])}


def test_success_and_dry_run_do_not_write(tmp_path):
    for extra in ({"status": "ok"}, {"status": "failed", "dry_run": True}):
        lessons.log_run_receipt(tmp_path, {"run_id": "run-example", "evidence_brief": {"memory_refs": [REF]}, **extra})
    assert log.read_evidence(tmp_path)[0] == []


def test_review_finding_logs_one_row_per_receipt_card(tmp_path):
    finding = {"memory_refs": [REF], "rationale": "PRIVATE CARD BODY"}
    lessons.log_review_findings(tmp_path, {"run_id": "review-example"}, [finding, finding])
    lessons.log_review_findings(tmp_path, {"run_id": "review-example"}, [finding])
    assert len(log.read_evidence(tmp_path)[0]) == 1
    assert "PRIVATE CARD BODY" not in (tmp_path / log.LOG_PATH).read_text()


def test_negative_priority_reserves_cadence_slots():
    heads = [head(f"card-{i}") for i in range(8)]
    negatives = {key(f"card-{i}") for i in range(5)}
    selected = pick.select(
        heads, entries={}, reads={}, slots=4, now_ms=NOW, min_interval_days=7, negative_keys=negatives
    ).selected
    assert [lane for _, lane in selected[:2]] == ["negative_outcome"] * 2
    assert len({card.key for card, _ in selected}) == 4
    assert any(card.key not in negatives for card, _ in selected[2:]) or all(
        lane != "negative_outcome" for _, lane in selected[2:]
    )


def test_noisy_card_keeps_cadence_cooldown():
    selected = pick.select(
        [head("card-noisy"), head("card-other")],
        entries={key("card-noisy"): entry(verified=TODAY)},
        reads={},
        slots=1,
        now_ms=NOW,
        min_interval_days=7,
        negative_keys={key("card-noisy")},
    ).selected
    assert [card.card_id for card, _ in selected] == ["card-other"]


def test_run_writer_emits_after_durable_receipt(tmp_path):
    from brigade.aboyeur import run_io

    run_dir = tmp_path / ".brigade/runs/run-example"
    run_dir.mkdir(parents=True)
    payload = {
        "status": "failed",
        "cwd": str(tmp_path),
        "task": "PRIVATE CARD BODY",
        "evidence_brief": {"memory_refs": [REF]},
    }
    run_io._write_json(run_dir / "run.json", payload)
    run_io._write_json(run_dir / "run.json", payload)
    assert len(log.read_evidence(tmp_path)[0]) == 1


def test_review_import_retains_citations_and_logs(tmp_path):
    import json
    from brigade.work_cmd import reviews

    run_dir = tmp_path / ".brigade/reviews/runs/review-example"
    run_dir.mkdir(parents=True)
    findings_path = run_dir / "findings.json"
    findings_path.write_text(
        json.dumps([{"path": "src/example.py", "rationale": "Wrong result", "memory_refs": [REF]}])
    )
    receipt = {
        "run_id": "review-example",
        "reviewer_id": "example",
        "path": str(run_dir),
        "findings_path": str(findings_path),
        "status": "completed",
    }
    (run_dir / "receipt.json").write_text(json.dumps(receipt))
    assert reviews.review_import_findings(target=tmp_path, run_id="review-example", dry_run=True) == 0
    assert log.read_evidence(tmp_path)[0] == []
    assert reviews.review_import_findings(target=tmp_path, run_id="review-example") == 0
    assert reviews.review_import_findings(target=tmp_path, run_id="review-example") == 0
    assert len(log.read_evidence(tmp_path)[0]) == 1


def test_negative_scopes_do_not_bleed(tmp_path):
    lessons.log_run_receipt(
        tmp_path, {"run_id": "run-example", "status": "failed", "evidence_brief": {"memory_refs": [REF]}}
    )
    assert lessons.pending_keys(tmp_path, [head(REF["card_id"], scope="repo:other")]) == set()


@pytest.mark.parametrize("field", ["last_reviewed", "last_reviewed_at", "reviewed_at"])
def test_negative_review_cutoff(tmp_path, field):
    lessons.log_run_receipt(
        tmp_path,
        {
            "run_id": "run-example",
            "status": "failed",
            "finished_at": "2026-01-01T00:00:00Z",
            "evidence_brief": {"memory_refs": [REF]},
        },
    )
    reviewed = head(REF["card_id"], body=f"---\n{field}: 2026-01-02\n---\nReviewed. ^c-abcdef\n")
    assert lessons.pending_keys(tmp_path, [reviewed]) == set()


def test_review_completion_captures_without_import(tmp_path):
    import sys
    from brigade.work_cmd import reviews

    script = tmp_path / "review.py"
    script.write_text(
        "import json\nfrom pathlib import Path\n"
        + "Path('findings.json').write_text(json.dumps(["
        + repr({"path": "src/example.py", "rationale": "Wrong result", "memory_refs": [REF]})
        + "]))\n"
    )
    receipt = reviews._review_run_one(
        tmp_path,
        {
            "id": "example",
            "command": f"{sys.executable} {script}",
            "cwd": ".",
            "findings_path": "findings.json",
            "timeout": 30,
        },
    )
    assert receipt["status"] == "completed"
    assert len(log.read_evidence(tmp_path)[0]) == 1


def test_dispatch_citation_capture_has_no_card_text(tmp_path, monkeypatch):
    from brigade import dispatch_memory
    from brigade.aboyeur import briefs

    packet = dispatch_memory.Packet("connector", "PRIVATE CARD BODY", {"items": [REF]})
    monkeypatch.setattr(dispatch_memory, "build", lambda *args, **kwargs: packet)
    brief = briefs.with_memory_context(briefs.EvidenceBrief(False), tmp_path, "task")
    assert brief.memory_refs == (REF,)
    assert "PRIVATE CARD BODY" not in str(brief.memory_refs)


def test_single_slot_does_not_starve_older_cards_after_cooldown():
    selected = pick.select(
        [head("card-noisy"), head("card-other")],
        entries={key("card-noisy"): entry(verified=TODAY - 2)},
        reads={},
        slots=1,
        now_ms=NOW,
        min_interval_days=1,
        negative_keys={key("card-noisy")},
    ).selected
    assert [card.card_id for card, _ in selected] == ["card-other"]


def test_file_card_run_capture_is_deduplicated(tmp_path):
    from brigade.outcome import OutcomeRecord

    card = tmp_path / "memory/cards/example.md"
    card.parent.mkdir(parents=True)
    card.write_text("---\ntopic: example\n---\nPRIVATE CARD BODY\n")
    record = OutcomeRecord(
        artifact_id="example",
        artifact_kind="card",
        task_id="",
        source="run",
        signal_value=-1,
        evidence_ref=str(tmp_path / ".brigade/runs/run-example/run.json"),
        ts="2026-01-01T00:00:00Z",
    )
    for _ in range(2):
        log.log_outcome(tmp_path, record, {"status": "failed"})
    rows = log.read_evidence(tmp_path)[0]
    assert len(rows) == 1
    assert rows[0]["card_path"] == "memory/cards/example.md"
    assert "PRIVATE CARD BODY" not in (tmp_path / log.LOG_PATH).read_text()
