# tests/test_research_claim_audit.py
"""Claim-level support audits (#938): validation, derivation, persistence, resume, show."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from brigade.research import claim_audit as claim_audit_mod
from brigade.research import registry
from brigade.research.engine import ResearchEngine
from brigade.research.types import Caps, ClaimAudit, Finding, ResearchRunError, ResumeState

from tests.test_research_engine import FIXTURE_SOURCE_ID, ScriptedBackend, fake_lanes, fixed_source_provider

CITE = f"[source:{FIXTURE_SOURCE_ID}]"
REPORT = f"Answer {CITE}"
OTHER_SOURCE = "src-2222222222222222"


def _finding(source_id: str = FIXTURE_SOURCE_ID, evidence: str = "Verified fact") -> Finding:
    from brigade.research.provenance import stamp_finding

    return stamp_finding(_raw_finding(source_id, evidence), run_id="claim-audit-test")


def _raw_finding(source_id: str, evidence: str) -> Finding:
    return Finding(
        source_ids=(source_id,),
        title="Fact",
        summary="Verified fact",
        evidence=evidence,
        trust="web",
        extraction_lane="luna",
        extracted_at="2026-08-13T12:00:00+00:00",
    )


def _review(
    claims: list[dict[str, object]] | object,
    *,
    accepted: bool = True,
    detail: str = "ok",
) -> str:
    payload: dict[str, object] = {"accepted": accepted, "detail": detail, "rejected_claims": []}
    if claims is not None:
        payload["claims"] = claims
    return json.dumps(payload)


def _lanes(calls, review_payloads: list[str], *, repair_report: str | None = None):
    lanes = fake_lanes(calls, gemini_report=REPORT, repair_report=repair_report, review="accepted")
    lanes.reviewer.responses = list(review_payloads)
    return lanes


def _run(lanes, **kwargs):
    engine = ResearchEngine(lanes=lanes, sources=[fixed_source_provider()], caps=Caps(max_rounds=1), **kwargs)
    return engine, engine.run("q")


# --- parse / derive -----------------------------------------------------------


def test_backed_claim_binds_span_digest_and_finding_fingerprint() -> None:
    finding = _finding()
    records = claim_audit_mod.parse_claim_records(
        [{"span": [0, 6], "source_ids": [FIXTURE_SOURCE_ID], "result": "backed", "explanation": "yes"}],
        report=REPORT,
        findings=(finding,),
    )
    assert len(records) == 1
    record = records[0]
    assert record.claim_id == "c1"
    assert record.text_digest == claim_audit_mod.text_digest("Answer")
    assert record.finding_fingerprints == (claim_audit_mod.finding_fingerprint(finding),)
    audit = claim_audit_mod.derive_claim_audit(
        report=REPORT,
        synthesis_attempt_id="s1",
        reviewer_seat="luna",
        review_attempt_id="r1",
        reviewer_accepted=True,
        claims=records,
    )
    assert audit.accepted is True
    assert audit.report_digest == claim_audit_mod.text_digest(REPORT)
    assert audit.counts["backed"] == 1 and audit.counts["total"] == 1


@pytest.mark.parametrize(
    ("claims", "kind"),
    [
        ("not-a-list", "malformed"),
        (["string"], "malformed"),
        ([{"span": [0, 6], "source_ids": [OTHER_SOURCE], "result": "backed"}], "unknown-id"),
        ([{"span": [0, 999], "source_ids": [FIXTURE_SOURCE_ID], "result": "backed"}], "bad-span"),
        ([{"span": [6, 6], "source_ids": [FIXTURE_SOURCE_ID], "result": "backed"}], "bad-span"),
        ([{"span": ["0", 6], "source_ids": [FIXTURE_SOURCE_ID], "result": "backed"}], "bad-span"),
        ([{"span": [0, 6], "source_ids": [FIXTURE_SOURCE_ID], "result": "maybe"}], "bad-result"),
        ([{"span": [0, 6], "source_ids": [], "result": "backed"}], "malformed"),
        ([{"span": [0, 6], "source_ids": [FIXTURE_SOURCE_ID], "result": "disputed"}], "malformed"),
        (
            [{"span": [0, 6], "source_ids": [FIXTURE_SOURCE_ID], "result": "backed", "explanation": "x" * 501}],
            "oversized",
        ),
        ([{"span": [0, 6], "source_ids": [FIXTURE_SOURCE_ID] * 21, "result": "backed"}], "oversized"),
        ([{"span": [0, 6], "source_ids": [FIXTURE_SOURCE_ID], "result": "backed"}] * 201, "oversized"),
        (
            [
                {"claim_id": "dup", "span": [0, 6], "source_ids": [FIXTURE_SOURCE_ID], "result": "backed"},
                {"claim_id": "dup", "span": [0, 6], "source_ids": [FIXTURE_SOURCE_ID], "result": "backed"},
            ],
            "malformed",
        ),
    ],
)
def test_parse_rejects_invalid_records(claims: object, kind: str) -> None:
    with pytest.raises(claim_audit_mod.ClaimAuditError) as caught:
        claim_audit_mod.parse_claim_records(claims, report=REPORT, findings=(_finding(),))
    assert caught.value.kind == kind


def test_acceptance_is_derived_not_copied_from_reviewer() -> None:
    records = claim_audit_mod.parse_claim_records(
        [
            {
                "span": [0, 6],
                "source_ids": [FIXTURE_SOURCE_ID],
                "result": "disputed",
                "conflicting_source_ids": [FIXTURE_SOURCE_ID],
            }
        ],
        report=REPORT,
        findings=(_finding(),),
    )
    audit = claim_audit_mod.derive_claim_audit(
        report=REPORT,
        synthesis_attempt_id="s1",
        reviewer_seat="luna",
        review_attempt_id="r1",
        reviewer_accepted=True,
        claims=records,
    )
    assert audit.reviewer_accepted is True
    assert audit.accepted is False
    assert audit.counts["disputed"] == 1
    assert "c1:disputed" in audit.detail


def test_insufficient_requires_stated_limitation_adjacent_to_claim() -> None:
    report = f"Answer {CITE}. Evidence for this is limited to one source."
    limit_start = report.index("Evidence")
    base = {"span": [0, 6], "source_ids": [FIXTURE_SOURCE_ID], "result": "insufficient"}
    findings = (_finding(),)
    stated = claim_audit_mod.parse_claim_records(
        [{**base, "limitation_span": [limit_start, len(report)]}], report=report, findings=findings
    )
    unstated = claim_audit_mod.parse_claim_records([base], report=report, findings=findings)
    kwargs = dict(
        report=report, synthesis_attempt_id="s1", reviewer_seat="luna", review_attempt_id="r1", reviewer_accepted=True
    )
    ok = claim_audit_mod.derive_claim_audit(claims=stated, **kwargs)
    bad = claim_audit_mod.derive_claim_audit(claims=unstated, **kwargs)
    assert ok.accepted is True
    assert ok.exceptions[0]["reason"] == "insufficient-with-stated-limit"
    assert ok.exceptions[0]["claim_id"] == "c1"
    assert bad.accepted is False
    assert bad.counts["unstated_insufficient"] == 1


def test_limitation_far_from_claim_does_not_count() -> None:
    filler = "x" * (claim_audit_mod.LIMITATION_PROXIMITY_CHARS + 50)
    report = f"Answer {CITE}. {filler} Evidence is limited."
    limit_start = report.index("Evidence")
    records = claim_audit_mod.parse_claim_records(
        [
            {
                "span": [0, 6],
                "source_ids": [FIXTURE_SOURCE_ID],
                "result": "insufficient",
                "limitation_span": [limit_start, len(report)],
            }
        ],
        report=report,
        findings=(_finding(),),
    )
    assert records[0].limitation_stated is False


def test_payload_round_trip_and_schema() -> None:
    records = claim_audit_mod.parse_claim_records(
        [
            {
                "span": [0, 6],
                "source_ids": [FIXTURE_SOURCE_ID],
                "result": "disputed",
                "conflicting_source_ids": [FIXTURE_SOURCE_ID],
                "explanation": "both sides",
            }
        ],
        report=REPORT,
        findings=(_finding(),),
    )
    audit = claim_audit_mod.derive_claim_audit(
        report=REPORT,
        synthesis_attempt_id="s1",
        reviewer_seat="luna",
        review_attempt_id="r1",
        reviewer_accepted=False,
        claims=records,
    )
    payload = claim_audit_mod.to_payload(audit)
    assert payload["schema"] == "brigade.research.claim-audit.v1"
    assert payload["schema_version"] == 1
    serialized = json.loads(json.dumps(payload))
    assert serialized["claims"][0]["conflicting_finding_fingerprints"] == list(
        records[0].conflicting_finding_fingerprints
    )
    assert serialized["claims"][0]["span"] == [0, 6]
    assert claim_audit_mod.from_payload(serialized) == audit
    with pytest.raises(ValueError):
        claim_audit_mod.from_payload({**payload, "schema": "brigade.research.claim-audit.v0"})
    with pytest.raises(ValueError):
        claim_audit_mod.from_payload({**payload, "claims": [{"span": [0, 1], "result": "nope"}]})


def test_summary_line_handles_unavailable() -> None:
    assert claim_audit_mod.summary_line(None) == "claim_audit: unavailable"
    assert claim_audit_mod.summary_counts(None) is None


# --- engine -------------------------------------------------------------------


def test_engine_persists_claim_audit_bound_to_report_and_synthesis_attempt() -> None:
    persisted: list[ClaimAudit] = []
    completed: list[tuple[str, dict]] = []
    calls: list[tuple[str, str]] = []
    lanes = _lanes(
        calls,
        [_review([{"span": [0, 6], "source_ids": [FIXTURE_SOURCE_ID], "result": "backed", "explanation": "ok"}])],
    )
    _engine, result = _run(
        lanes,
        persist_claim_audit=lambda audit: persisted.append(audit) or {"path": "claim-audit.json", "digest": "d" * 64},
        on_phase_completed=lambda phase, detail: completed.append((phase, detail)),
    )
    assert result.claim_audit is not None
    assert result.claim_audit.accepted is True
    assert persisted == [result.claim_audit]
    assert result.claim_audit.report_digest == claim_audit_mod.text_digest(REPORT)
    assert result.claim_audit.synthesis_attempt_id == result.synthesis_attempt_id
    assert result.claim_audit.review_attempt_id == result.review.attempt_id
    review_detail = next(detail for phase, detail in completed if phase == "review")
    assert review_detail["claim_audit"]["artifact"]["path"] == "claim-audit.json"
    assert review_detail["claim_audit"]["counts"]["backed"] == 1
    assert "claims" not in review_detail["claim_audit"]


def test_engine_reviewer_prompt_asks_for_claims_and_forbids_fetching() -> None:
    prompts: list[str] = []
    lanes = fake_lanes([], gemini_report=REPORT, review="accepted", review_prompts=prompts)
    ResearchEngine(lanes=lanes, sources=[fixed_source_provider()], caps=Caps(max_rounds=1)).run("q")
    assert '"claims"' in prompts[0]
    assert "Do not fetch new evidence" in prompts[0]


def test_known_citation_on_unrelated_claim_fails_semantic_check_after_repair() -> None:
    """Citation tokens resolve, but the reviewer marks the claim unsupported twice."""
    calls: list[tuple[str, str]] = []
    unrelated = [
        {"span": [0, 6], "source_ids": [FIXTURE_SOURCE_ID], "result": "insufficient", "explanation": "off-topic"}
    ]
    lanes = _lanes(calls, [_review(unrelated), _review(unrelated)], repair_report=f"Still {CITE}")
    with pytest.raises(ResearchRunError) as caught:
        _run(lanes)
    assert caught.value.failure_kind == "review-rejected"
    assert "insufficient-unstated" in caught.value.detail
    assert [phase for phase, _ in calls].count("review") == 2
    assert "synthesis" in [phase for phase, _ in calls]


def test_disputed_claim_triggers_single_repair_and_reruns_both_checks() -> None:
    calls: list[tuple[str, str]] = []
    persisted: list[ClaimAudit] = []
    repaired = f"Repaired {CITE}"
    disputed = [
        {
            "span": [0, 6],
            "source_ids": [FIXTURE_SOURCE_ID],
            "result": "disputed",
            "conflicting_source_ids": [FIXTURE_SOURCE_ID],
            "explanation": "conflict",
        }
    ]
    backed = [{"span": [0, 8], "source_ids": [FIXTURE_SOURCE_ID], "result": "backed"}]
    lanes = _lanes(calls, [_review(disputed), _review(backed)], repair_report=repaired)
    _engine, result = _run(lanes, persist_claim_audit=lambda audit: persisted.append(audit) or "claim-audit.json")
    assert result.report == repaired
    assert len(persisted) == 2
    assert persisted[0].accepted is False
    assert persisted[0].claims[0].conflicting_finding_fingerprints
    assert persisted[0].report_digest == claim_audit_mod.text_digest(REPORT)
    # The repair invalidated the prior audit: the final one binds to the new digest.
    assert result.claim_audit is persisted[1]
    assert persisted[1].report_digest == claim_audit_mod.text_digest(repaired)
    assert persisted[1].synthesis_attempt_id != persisted[0].synthesis_attempt_id
    assert persisted[1].accepted is True
    phases = [phase for phase, _ in calls]
    assert phases.count("review") == 2
    assert phases.count("synthesis") == 2  # original + repair on the synthesis seat


def test_skipped_claim_is_repaired_and_surfaced_in_repair_prompt() -> None:
    prompts: list[str] = []
    calls: list[tuple[str, str]] = []
    skipped = [{"span": [0, 6], "source_ids": [], "result": "skipped", "explanation": "not checked"}]
    backed = [{"span": [0, 8], "source_ids": [FIXTURE_SOURCE_ID], "result": "backed"}]
    lanes = _lanes(calls, [_review(skipped), _review(backed)], repair_report=f"Repaired {CITE}")
    lanes.synthesizers[0].prompts = prompts
    _engine, result = _run(lanes)
    assert result.claim_audit is not None and result.claim_audit.accepted
    assert any("skipped: Answer" in prompt for prompt in prompts)


@pytest.mark.parametrize(
    ("claims", "kind"),
    [
        ("garbage", "claim-audit-malformed"),
        ([{"span": [0, 6], "source_ids": [OTHER_SOURCE], "result": "backed"}], "claim-audit-unknown-id"),
        ([{"span": [0, 99], "source_ids": [FIXTURE_SOURCE_ID], "result": "backed"}], "claim-audit-bad-span"),
        ([{"span": [0, 6], "source_ids": [FIXTURE_SOURCE_ID], "result": "backed"}] * 201, "claim-audit-oversized"),
    ],
)
def test_engine_fails_review_on_invalid_claim_records(claims: object, kind: str) -> None:
    persisted: list[ClaimAudit] = []
    lanes = _lanes([], [_review(claims)])
    with pytest.raises(ResearchRunError) as caught:
        _run(lanes, persist_claim_audit=lambda audit: persisted.append(audit) or "claim-audit.json")
    assert caught.value.failure_phase == "review"
    assert caught.value.failure_kind == kind
    assert persisted == []


def test_engine_rejects_malformed_reviewer_json_before_claims() -> None:
    lanes = _lanes([], ["not json at all"])
    with pytest.raises(ResearchRunError) as caught:
        _run(lanes)
    assert caught.value.failure_kind == "invalid-json"


def test_reviewer_without_claims_keeps_boolean_veto_behavior() -> None:
    lanes = _lanes([], [_review(None, accepted=False, detail="nope"), _review(None, accepted=False, detail="nope")])
    lanes.synthesizers[0].responses.append(f"Again {CITE}")
    with pytest.raises(ResearchRunError) as caught:
        _run(lanes)
    assert caught.value.failure_kind == "review-rejected"
    assert caught.value.detail == "nope"


def test_resume_skips_review_only_when_claim_audit_matches_report() -> None:
    findings = (_finding(),)
    from brigade.research.types import CitationAudit, ReviewResult, SynthesisRecord

    review = ReviewResult(accepted=True, detail="ok", rejected_claims=(), seat="luna", attempt_id="r1")
    synthesis = SynthesisRecord(seat="luna", attempt_id="s1", requested_model=None, observed_model="x")
    good = claim_audit_mod.derive_claim_audit(
        report=REPORT,
        synthesis_attempt_id="s1",
        reviewer_seat="luna",
        review_attempt_id="r1",
        reviewer_accepted=True,
        claims=(),
    )
    stale = claim_audit_mod.derive_claim_audit(
        report="different report",
        synthesis_attempt_id="s1",
        reviewer_seat="luna",
        review_attempt_id="r1",
        reviewer_accepted=True,
        claims=(),
    )
    base = dict(
        plan="plan",
        sources=(),
        findings=findings,
        report=REPORT,
        audit=CitationAudit(accepted=True, citations=(), unresolved=()),
        review=review,
        synthesis=synthesis,
    )
    for audit, expect_review_calls in ((good, 0), (stale, 1), (None, 1)):
        calls: list[tuple[str, str]] = []
        lanes = _lanes(calls, [_review([{"span": [0, 6], "source_ids": [FIXTURE_SOURCE_ID], "result": "backed"}])])
        engine = ResearchEngine(lanes=lanes, sources=[fixed_source_provider()], caps=Caps(max_rounds=1))
        result = engine.run("q", resume=ResumeState(claim_audit=audit, **base))
        assert [phase for phase, _ in calls].count("review") == expect_review_calls, audit
        assert result.claim_audit is not None
        assert result.claim_audit.report_digest == claim_audit_mod.text_digest(REPORT)


# --- registry / resume_state / show ------------------------------------------


def _seed_completed(tmp_path: Path, *, with_claim_audit: bool, stale: bool = False) -> str:
    from tests.test_research_cmd import seed_cancelled_run_after_accepted_review

    run_id = seed_cancelled_run_after_accepted_review(tmp_path, claim_audit=with_claim_audit)
    if stale:
        # Rewrite the synthesis attempt so the persisted records no longer bind.
        sidecar = registry.read_research(tmp_path, run_id)
        phases = dict(sidecar["phases"])
        phases["synthesis"] = {**phases["synthesis"], "attempt_id": "luna:synthesis-2"}
        registry.update_research(tmp_path, run_id, phases=phases)
    return run_id


def test_registry_round_trips_claim_audit_with_digest_verification(tmp_path: Path) -> None:
    run_id = registry.create_run(tmp_path, question="q", run_id="claim-rt", caps={})
    audit = claim_audit_mod.derive_claim_audit(
        report=REPORT,
        synthesis_attempt_id="s1",
        reviewer_seat="luna",
        review_attempt_id="r1",
        reviewer_accepted=True,
        claims=(),
    )
    ref = registry.write_claim_audit(tmp_path, run_id, audit)
    assert ref["path"] == registry.CLAIM_AUDIT_ARTIFACT
    loaded = registry.read_verified_artifact(tmp_path, run_id, ref)
    assert loaded == audit
    path = registry.standard_run_dir(tmp_path, run_id) / registry.CLAIM_AUDIT_ARTIFACT
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema"] == "brigade.research.claim-audit.v1"
    path.write_text(json.dumps({**payload, "accepted": False}), encoding="utf-8")
    assert registry.read_verified_artifact(tmp_path, run_id, ref) is None


def test_resume_state_drops_review_without_matching_claim_audit(tmp_path: Path) -> None:
    from brigade import research_cmd

    state = research_cmd.resume_state(tmp_path, _seed_completed(tmp_path, with_claim_audit=True))
    assert state.claim_audit is not None and state.review is not None and state.report is not None

    state = research_cmd.resume_state(tmp_path, _seed_completed(tmp_path, with_claim_audit=False))
    assert state.report is not None and state.audit is not None
    assert state.claim_audit is None and state.review is None

    state = research_cmd.resume_state(tmp_path, _seed_completed(tmp_path, with_claim_audit=True, stale=True))
    assert state.report is not None
    assert state.claim_audit is None and state.review is None


def test_show_exposes_counts_without_records_and_reports_unavailable(tmp_path: Path, capsys, monkeypatch) -> None:
    from brigade import research_cmd
    from tests.test_research_cmd import patch_stub_lanes
    from brigade.research_cmd import cli_resume, cli_show

    patch_stub_lanes(monkeypatch)
    run_id = _seed_completed(tmp_path, with_claim_audit=True)
    assert cli_resume(target=tmp_path, run_id=run_id, overrides={}, json_output=True) == 0
    capsys.readouterr()
    assert cli_show(target=tmp_path, run_id=run_id, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    claim = payload["claim_audit"]
    assert claim["artifact"] == registry.CLAIM_AUDIT_ARTIFACT
    assert claim["verification"] == "verified"
    assert claim["accepted"] is True
    assert claim["counts"]["total"] == 0
    assert "claims" not in claim
    assert payload["run"]["artifacts"]["claim_audit"] == registry.CLAIM_AUDIT_ARTIFACT
    assert payload["artifact_verification"]["claim_audit"] == "verified"
    assert cli_show(target=tmp_path, run_id=run_id, json_output=False) == 0
    text = capsys.readouterr().out
    assert "claim_audit: accepted (0 claims: backed=0, disputed=0, insufficient=0, skipped=0)" in text

    # A legacy-shaped run has no claim audit and is not upgraded.
    legacy_id = research_cmd.registry.create_run(tmp_path, question="q", run_id="legacy-claims", caps={})
    assert cli_show(target=tmp_path, run_id=legacy_id, json_output=True) == 0
    assert json.loads(capsys.readouterr().out)["claim_audit"] is None
    assert cli_show(target=tmp_path, run_id=legacy_id, json_output=False) == 0
    assert "claim_audit: unavailable" in capsys.readouterr().out


def test_scripted_backend_fixture_still_accepts_plain_review() -> None:
    backend = ScriptedBackend(seat="luna", phase="review", calls=[], responses=[_review(None)])
    assert json.loads(backend.complete([{"role": "user", "content": ""}]))["accepted"] is True
