"""Failure signals retain identifiers and reserve verification capacity."""

import json
import os

import pytest

from brigade import memory_lesson_rows as lessons
from brigade import memory_evidence_log as log
from tests.test_memory_verify_select import NOW, TODAY, entry, head, key
from brigade import memory_verify_select as pick

# Callers catch append failures and warn, so platforms without safe directory
# descriptors run without failure signals. Match the other memory test modules.
pytestmark = pytest.mark.skipif(
    os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"),
    reason="safe directory descriptors unavailable",
)

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
    rows, warnings, _ = log.read_evidence(tmp_path, log_path=lessons.LOG_PATH)
    assert warnings == 0
    assert len(rows) == 1
    assert rows[0]["kind"] == "negative-outcome"
    assert "PRIVATE CARD BODY" not in (tmp_path / lessons.LOG_PATH).read_text()
    assert lessons.pending_keys(tmp_path, [head(REF["card_id"])]) == {key(REF["card_id"])}


def test_success_and_dry_run_do_not_write(tmp_path):
    for extra in ({"status": "ok"}, {"status": "failed", "dry_run": True}):
        lessons.log_run_receipt(tmp_path, {"run_id": "run-example", "evidence_brief": {"memory_refs": [REF]}, **extra})
    assert log.read_evidence(tmp_path, log_path=lessons.LOG_PATH)[0] == []


def test_review_finding_logs_one_row_per_receipt_card(tmp_path):
    finding = {"memory_refs": [REF], "rationale": "PRIVATE CARD BODY"}
    lessons.log_review_findings(tmp_path, {"run_id": "review-example"}, [finding, finding])
    lessons.log_review_findings(tmp_path, {"run_id": "review-example"}, [finding])
    assert len(log.read_evidence(tmp_path, log_path=lessons.LOG_PATH)[0]) == 1
    assert "PRIVATE CARD BODY" not in (tmp_path / lessons.LOG_PATH).read_text()


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
    assert len(log.read_evidence(tmp_path, log_path=lessons.LOG_PATH)[0]) == 1


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
    assert log.read_evidence(tmp_path, log_path=lessons.LOG_PATH)[0] == []
    assert reviews.review_import_findings(target=tmp_path, run_id="review-example") == 0
    assert reviews.review_import_findings(target=tmp_path, run_id="review-example") == 0
    assert len(log.read_evidence(tmp_path, log_path=lessons.LOG_PATH)[0]) == 1


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
    assert len(log.read_evidence(tmp_path, log_path=lessons.LOG_PATH)[0]) == 1


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


@pytest.mark.parametrize("field", ["last_reviewed", "last_reviewed_at", "reviewed_at"])
def test_reinforcement_does_not_consume_failures(tmp_path, field):
    lessons.log_run_receipt(
        tmp_path,
        {
            "run_id": "run-example",
            "status": "failed",
            "finished_at": "2026-01-01T00:00:00Z",
            "evidence_brief": {"memory_refs": [REF]},
        },
    )
    row = {
        "ts": "2026-01-02T12:00:00Z",
        "card_id": REF["card_id"],
        "card_path": "memory/cards/example.md",
        "evidence_id": "handoff:duplicate",
        "kind": "reinforced",
        "similarity": 1.0,
    }
    log.append_log_row(tmp_path, json.dumps(row).encode() + b"\n")
    reinforced = head(REF["card_id"], body=f"---\n{field}: 2026-01-02\n---\nReinforced. ^c-abcdef\n")
    assert lessons.pending_keys(tmp_path, [reinforced]) == {key(REF["card_id"])}


def test_file_card_run_capture_uses_completion_time(tmp_path):
    from brigade.outcome import OutcomeRecord

    card = tmp_path / "memory/cards/example.md"
    card.parent.mkdir(parents=True)
    card.write_text("---\ntopic: example\n---\nBody\n")
    record = OutcomeRecord(
        artifact_id="example",
        artifact_kind="card",
        task_id="",
        source="run",
        signal_value=-1,
        evidence_ref=str(tmp_path / ".brigade/runs/run-late/run.json"),
        ts="2026-01-01T00:00:00Z",
    )
    log.log_outcome(tmp_path, record, {"status": "failed", "finished_at": "2026-01-03T00:00:00Z"})
    rows = log.read_evidence(tmp_path, log_path=lessons.LOG_PATH)[0]
    assert [row["ts"] for row in rows] == ["2026-01-03T00:00:00Z"]


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
    rows = log.read_evidence(tmp_path, log_path=lessons.LOG_PATH)[0]
    assert len(rows) == 1
    assert rows[0]["card_path"] == "memory/cards/example.md"
    assert "PRIVATE CARD BODY" not in (tmp_path / lessons.LOG_PATH).read_text()


@pytest.mark.parametrize("field", ["last_consolidated", "last_reviewed", "last_reviewed_at", "reviewed_at"])
@pytest.mark.parametrize(
    "review,pending",
    [("2026-01-02", False), ("2026-01-02T15:00:00Z", False), ("2026-01-02T09:00:00Z", True), ("2026-01-01", True)],
)
def test_negative_cutoff_same_day(tmp_path, field, review, pending):
    lessons.log_run_receipt(
        tmp_path,
        {
            "run_id": "run-example",
            "status": "failed",
            "finished_at": "2026-01-02T12:00:00Z",
            "evidence_brief": {"memory_refs": [REF]},
        },
    )
    reviewed = head(REF["card_id"], body=f"---\n{field}: {review}\n---\nReviewed. ^c-abcdef\n")
    assert lessons.pending_keys(tmp_path, [reviewed]) == ({key(REF["card_id"])} if pending else set())


@pytest.mark.parametrize("field", ["last_reviewed", "last_reviewed_at", "reviewed_at"])
def test_negative_cutoff_uses_latest_review_and_consolidation(tmp_path, field):
    lessons.log_run_receipt(
        tmp_path,
        {
            "run_id": "run-example",
            "status": "failed",
            "finished_at": "2026-01-02T12:00:00Z",
            "evidence_brief": {"memory_refs": [REF]},
        },
    )
    reviewed = head(
        REF["card_id"], body=f"---\nlast_consolidated: 2026-01-01\n{field}: 2026-01-03\n---\nReviewed. ^c-abcdef\n"
    )
    assert lessons.pending_keys(tmp_path, [reviewed]) == set()


@pytest.fixture(params=["run", "review", "capture"])
def append_failure(tmp_path, request):
    """Exercise all three producers with stable receipt identifiers."""
    if request.param == "run":
        return lambda run_id="run-example": lessons.log_run_receipt(
            tmp_path,
            {
                "run_id": run_id,
                "status": "failed",
                "evidence_brief": {"memory_refs": [REF]},
            },
        )
    if request.param == "review":
        return lambda run_id="run-example": lessons.log_review_findings(
            tmp_path, {"run_id": run_id}, [{"memory_refs": [REF]}]
        )
    from brigade.outcome import OutcomeRecord

    card = tmp_path / "memory/cards/example.md"
    card.parent.mkdir(parents=True)
    card.write_text("---\ntopic: example\n---\nExample card.\n")
    return lambda run_id="run-example": log.log_outcome(
        tmp_path,
        OutcomeRecord(
            "example", "card", "", "run", -1, str(tmp_path / f".brigade/runs/{run_id}/run.json"), "2026-01-01T00:00:00Z"
        ),
        {"status": "failed"},
    )


def test_failure_append_survives_large_log(tmp_path, append_failure):
    append_failure()
    path = tmp_path / lessons.LOG_PATH
    first = path.read_bytes()
    # Push the original signal outside the reader's tail with unrelated evidence.
    row = {**json.loads(first), "kind": "near-match", "evidence_id": "handoff-example.md"}
    padding = (json.dumps(row) + "\n").encode()
    with path.open("ab") as handle:
        handle.write(padding * (log.MAX_SCAN_BYTES // len(padding) + 1))
    append_failure()
    assert path.read_bytes().count(first) == 1
    # A distinct receipt still appends beyond the old scan limit.
    append_failure("another-run")
    assert any(row["kind"] == "negative-outcome" for row in log.read_evidence(tmp_path, log_path=lessons.LOG_PATH)[0])


@pytest.mark.parametrize(
    "malformed",
    [b"invalid\n", b'{"torn":', b"x" * (log.MAX_LINE_BYTES + 1) + b"\n"],
    ids=["invalid", "torn", "oversized"],
)
def test_failure_append_survives_malformed_log(tmp_path, append_failure, malformed):
    append_failure()
    path = tmp_path / lessons.LOG_PATH
    first = path.read_bytes()
    with path.open("ab") as handle:
        handle.write(malformed)
    append_failure()
    append_failure("another-run")
    rows, warnings, _ = log.read_evidence(tmp_path, log_path=lessons.LOG_PATH)
    assert warnings >= 1
    assert len([row for row in rows if row["kind"] == "negative-outcome"]) == 2
    assert path.read_bytes().count(first) == 1


def test_failure_retraction_sticks_on_retry(tmp_path, append_failure):
    from brigade.memory_log_retractions import withdrawn

    append_failure()
    path = tmp_path / lessons.LOG_PATH
    log.append_log_row(tmp_path, withdrawn(path.read_bytes()), log_path=lessons.LOG_PATH)
    assert log.read_evidence(tmp_path, log_path=lessons.LOG_PATH)[0] == []
    before = path.read_bytes()
    append_failure()
    assert log.read_evidence(tmp_path, log_path=lessons.LOG_PATH)[0] == []
    assert path.read_bytes() == before


def test_legacy_file_rows_scope(tmp_path):
    from brigade import memory_proposal_io as io

    log.append_log_row(
        tmp_path,
        io.json_bytes(
            {
                "ts": "2026-01-01T12:00:00Z",
                "card_id": REF["card_id"],
                "card_path": "memory/cards/example.md",
                "evidence_id": "run-example",
                "kind": "negative-outcome",
                "similarity": 1.0,
            }
        ),
    )
    assert lessons.pending_keys(
        tmp_path,
        [head(REF["card_id"]), head(REF["card_id"], scope="repo:other")],
        scope=REF["scope"],
    ) == {key(REF["card_id"])}


def test_complete_unterminated_row_is_not_duplicated(tmp_path, append_failure):
    append_failure()
    path = tmp_path / lessons.LOG_PATH
    original = path.read_bytes()
    path.write_bytes(original.rstrip(b"\n"))
    append_failure()
    assert path.read_bytes() == original.rstrip(b"\n")
    append_failure("another-run")
    assert len(log.read_evidence(tmp_path, log_path=lessons.LOG_PATH)[0]) == 2
    assert path.read_bytes().count(original) == 1


def test_dedup_refuses_concurrent_log_truncation(tmp_path, monkeypatch, append_failure):
    from brigade import memory_proposal_io as io

    append_failure()
    path = tmp_path / lessons.LOG_PATH
    regular = io._regular

    def truncate(fd, **kwargs):
        metadata = regular(fd, **kwargs)
        # The lock and captured-card reads also validate regular files. Only
        # truncate once the dedup scanner has sized the ledger itself.
        if metadata.st_ino == path.stat().st_ino:
            path.write_bytes(b"")
        return metadata

    monkeypatch.setattr(io, "_regular", truncate)
    with pytest.raises(ValueError, match="evidence ledger truncated during deduplication"):
        append_failure("another-run")
    assert path.read_bytes() == b""


@pytest.mark.parametrize("failure_day,pending", [(TODAY - 9, False), (TODAY - 8, False), (TODAY - 7, True)])
def test_verified_cutoff_keeps_only_new_failures(tmp_path, failure_day, pending):
    from datetime import datetime, timezone

    lessons.log_run_receipt(
        tmp_path,
        {
            "run_id": "run-example",
            "status": "failed",
            "finished_at": datetime.fromtimestamp(failure_day * 86400 + 43200, timezone.utc).isoformat(),
            "evidence_brief": {"memory_refs": [REF]},
        },
    )
    assert lessons.pending_keys(tmp_path, [head(REF["card_id"])], {key(REF["card_id"]): entry(verified=TODAY - 8)}) == (
        {key(REF["card_id"])} if pending else set()
    )


def test_outcome_capture_survives_failure_logging_error(tmp_path, monkeypatch, capsys):
    from brigade import outcome_cmd
    from tests.test_outcome_cmd import _write_run_receipt

    card = tmp_path / "memory/cards/example.md"
    card.parent.mkdir(parents=True)
    card.write_text("---\ntopic: example\n---\nExample card.\n")
    _write_run_receipt(tmp_path, status="failed")

    def fail(*args, **kwargs):
        raise ValueError("private failure details")

    monkeypatch.setattr(lessons, "log_captured_run", fail)
    assert (
        outcome_cmd.capture(
            target=tmp_path, artifact_id="example", artifact_kind="card", task_id="", run_receipt="brigade-run"
        )
        == 0
    )
    records = outcome_cmd.load_records(tmp_path)
    assert len(records) == 1 and records[0].signal_value == -1
    assert capsys.readouterr().err == "warning: evidence log append failed; retry outcome capture\n"


@pytest.mark.parametrize("producer", ["run", "review", "capture"])
@pytest.mark.parametrize("scope", ["repo:verify", "repo:other"])
def test_scheduling_failure_does_not_block_local_proposal(tmp_path, producer, scope):
    from brigade.card_identity import card_identity
    from brigade.memory_cmd import _parse_frontmatter
    from brigade.outcome import OutcomeRecord
    from tests.test_memory_proposal_v2 import setup, create, op, apply

    card = setup(tmp_path)
    meta, _ = _parse_frontmatter(card.read_text())
    card_id = card_identity(meta, card.relative_to(tmp_path).as_posix()).card_id
    ref = {**REF, "card_id": card_id, "scope": scope}
    proposal = create(tmp_path, [op("replace_block", text="Keep the cache cold.")])
    if producer == "run":
        lessons.log_run_receipt(
            tmp_path,
            {
                "run_id": "run-example",
                "status": "failed",
                "evidence_brief": {"memory_refs": [ref]},
            },
        )
    elif producer == "review":
        lessons.log_review_findings(tmp_path, {"run_id": "review-example"}, [{"memory_refs": [ref]}])
    else:
        lessons.log_captured_run(
            tmp_path,
            OutcomeRecord(
                "cache",
                "card",
                "",
                "run",
                -1,
                str(tmp_path / ".brigade/runs/run-example/run.json"),
                "2026-11-15T12:00:00Z",
            ),
            {"status": "failed"},
        )
    assert log.read_evidence(tmp_path) == ([], 0, False)
    assert log.EvidenceLog(tmp_path).by_identity == {}
    from brigade.memory_proposal_v2 import _refuse_live_blockers

    # Exercise the live preflight used by reflection proposals, not just storage.
    _refuse_live_blockers(
        tmp_path,
        [
            {
                "path": card.relative_to(tmp_path).as_posix(),
                "text": card.read_text(),
                "metadata": meta,
                "id": card_id,
            }
        ],
    )
    assert len(log.read_evidence(tmp_path, log_path=lessons.LOG_PATH)[0]) == 1
    if producer != "capture":
        assert lessons.pending_keys(tmp_path, [head(card_id), head(card_id, scope="repo:other")]) == {
            key(card_id, scope=scope)
        }
    assert apply(tmp_path, proposal)["status"] == "committed"


def test_scheduling_reads_real_verification_failure_rows(tmp_path):
    log.append_log_row(
        tmp_path,
        json.dumps(
            {
                "ts": "2026-11-15T12:00:00Z",
                "card_id": REF["card_id"],
                "card_path": "memory/cards/example.md",
                "kind": "negative-outcome",
                "similarity": 1.0,
                "evidence_id": ".brigade/work/verify-runs/example/receipt.json",
            }
        ).encode()
        + b"\n",
    )
    assert lessons.pending_keys(tmp_path, [head(REF["card_id"])], scope=REF["scope"]) == {key(REF["card_id"])}


def test_shared_ledger_volume_cannot_evict_scheduling_signal(tmp_path):
    lessons.log_run_receipt(
        tmp_path,
        {
            "run_id": "run-example",
            "status": "failed",
            "evidence_brief": {"memory_refs": [REF]},
        },
    )
    path = tmp_path / log.LOG_PATH
    path.write_bytes(b"invalid\n" * (log.MAX_SCAN_BYTES // 8 + 1))
    assert lessons.pending_keys(tmp_path, [head(REF["card_id"])]) == {key(REF["card_id"])}


def test_run_writer_uses_receipt_root_for_nested_cwd(tmp_path):
    from brigade.aboyeur import run_io

    run_dir = tmp_path / ".brigade/runs/run-example"
    run_dir.mkdir(parents=True)
    cwd = tmp_path / "src/nested"
    cwd.mkdir(parents=True)
    run_io._write_json(
        run_dir / "run.json",
        {
            "status": "failed",
            "cwd": str(cwd),
            "evidence_brief": {"memory_refs": [REF]},
        },
    )
    assert lessons.pending_keys(tmp_path, [head(REF["card_id"])]) == {key(REF["card_id"])}
    assert not (cwd / ".brigade").exists()


def test_dispatch_caps_citations_independently_of_packet_size(tmp_path, monkeypatch):
    from brigade import dispatch_memory
    from brigade.aboyeur import briefs

    packet = dispatch_memory.Packet("connector", "Context", {"items": [REF] * (dispatch_memory.MAX_REFS + 1)})
    monkeypatch.setattr(dispatch_memory, "build", lambda *args, **kwargs: packet)
    brief = briefs.with_memory_context(briefs.EvidenceBrief(False), tmp_path, "task")
    assert len(brief.memory_refs) == dispatch_memory.MAX_REFS
    assert dispatch_memory.validate_refs(list(brief.memory_refs))


def test_evidence_rows_reject_unused_scope_field():
    row = {
        "ts": "2026-01-01T12:00:00Z",
        "card_id": REF["card_id"],
        "card_path": "memory/cards/example.md",
        "evidence_id": "run-example",
        "kind": "negative-outcome",
        "similarity": 1.0,
    }
    assert log._valid(row)
    assert not log._valid({**row, "scope": REF["scope"]})
