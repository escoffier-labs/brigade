"""Failed registered verification is a review lead, never supporting proof."""

from __future__ import annotations

import io
import json
import os
import sys
from contextlib import redirect_stdout
from datetime import datetime, timezone

import pytest

from brigade import memory_reflect, verify_manifest, verify_trial
from brigade import memory_evidence_log as log
from brigade.work_cmd import verification as verify_mod
from tests.test_memory_outcome_evidence import capture, verified_card as verified_card
from tests.test_memory_reflect import card, command, snapshot
from tests.test_verify_trial import _init_verify_git_repo, _track_workspace_manifest


pytestmark = pytest.mark.skipif(
    os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"),
    reason="safe directory descriptors unavailable",
)


@pytest.fixture
def failed_card(tmp_path, monkeypatch):
    _init_verify_git_repo(tmp_path)
    path = card(tmp_path, "cache", body="Keep the cache warm. [[linked]]")
    card(tmp_path, "linked", body="Keep linked state.")
    monkeypatch.setattr(verify_mod.helpers, "_now", lambda: datetime(2026, 10, 20, 12, tzinfo=timezone.utc))
    monkeypatch.setattr(verify_mod.graphtrail_delta, "capture_before", lambda *_a, **_k: None)
    monkeypatch.setattr(
        verify_mod.graphtrail_delta,
        "capture_after_and_diff",
        lambda *_a, **_k: verify_mod.graphtrail_delta._status("disabled", "graph disabled in test"),
    )
    verify_manifest.write_workspace_manifest(
        tmp_path,
        {
            "schema": "brigade.verify_manifest.v1",
            "schema_version": 1,
            "manifest_id": "negative-card",
            "binding_mode": "fixture_eval",
            "verifier_id": "brigade.verify.fixture",
            "subject": {"artifact_kind": "card", "artifact_id": "cache", "subject_path": "memory/cards/cache.md"},
            "fixture": {
                "manifest_id": "adversarial-failed-verification",
                "case_id": "report-failing-tests",
                "check_id": "fixture.fail",
            },
            "checks": [
                {
                    "check_id": "fixture.fail",
                    "check_role": "effectiveness",
                    "command": f'{sys.executable} -c "raise SystemExit(1)"',
                }
            ],
        },
    )
    _track_workspace_manifest(tmp_path, "negative-card")

    def run():
        output = io.StringIO()
        with redirect_stdout(output):
            assert (
                verify_mod.verify_run(target=tmp_path, manifest_id="negative-card", reuse=False, json_output=True) == 1
            )
        receipt = json.loads(output.getvalue())
        assert receipt["status"] == "failed"
        assert verify_trial.project_trial(receipt, target=tmp_path).eligible
        return receipt

    return path, run(), run


def test_failed_verification_capture_is_review_only(tmp_path, failed_card, capsys):
    from brigade import memory_proposals as api
    from brigade.memory_proposal_v2_reflect import propose_payload

    path, receipt, _ = failed_card
    before = snapshot(tmp_path)
    assert capture(tmp_path, receipt) == 0
    rows = log.read_evidence(tmp_path)[0]
    assert len(rows) == 1
    assert rows[0]["kind"] == "negative-outcome"
    assert rows[0]["ts"] == receipt["completed_at"]
    assert set(rows[0]) == {"ts", "card_id", "card_path", "evidence_id", "kind", "similarity"}
    capsys.readouterr()
    code, result = command(tmp_path, capsys, "run")
    assert code == 0
    (candidate,) = result["candidates"]
    assert candidate["kind"] == "negative-outcome-review"
    assert candidate["semantic_contradiction"] == "unadjudicated"
    assert candidate["tier"] == 2
    assert candidate["proof_count"] == candidate["proof_sources"] == 0
    assert candidate["diff"] == ""
    assert candidate["review_evidence_ids"] == [rows[0]["evidence_id"]]
    assert candidate["outcome_bindings"][rows[0]["evidence_id"]]["subject_binding"] == receipt["subject_binding"]
    with pytest.raises(api.ProposalError):
        propose_payload(target=tmp_path, candidate_id=candidate["candidate_id"], reason="Inspect failed verification.")
    assert not log.validate_outcome_support(
        tmp_path, card_path="memory/cards/cache.md", card_text=path.read_text(), ref=rows[0]["evidence_id"]
    )
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize(
    "drift", ["card", "linked", "receipt", "missing", "identity", "digest", "manifest", "timestamp"]
)
def test_negative_review_revalidates_current_bindings(tmp_path, failed_card, capsys, drift):
    from tests.test_memory_outcome_evidence import drift_support

    path, receipt, _ = failed_card
    assert capture(tmp_path, receipt) == 0
    if drift == "manifest":
        manifest = tmp_path / "verify/manifests/negative-card.json"
        manifest.write_text(manifest.read_text().replace("report-failing-tests", "changed-case"))
    elif drift == "timestamp":
        ledger = tmp_path / log.LOG_PATH
        row = json.loads(ledger.read_text())
        row["ts"] = "2026-10-21T12:00:00Z"
        ledger.write_text(json.dumps(row) + "\n")
    else:
        drift_support(tmp_path, path, receipt, drift)
    before = snapshot(tmp_path)
    capsys.readouterr()
    code, result = command(tmp_path, capsys, "run", "--dry-run")
    assert code == 0
    assert result["candidates"] == []
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize(
    "case", ["missing-taxonomy", "unknown-taxonomy", "infrastructure", "reused", "utility", "wrong-card"]
)
def test_negative_capture_requires_eligible_attributed_failure(tmp_path, failed_card, capsys, case):
    from tests.test_memory_outcome_evidence import rewrite

    _, receipt, _ = failed_card
    if case == "missing-taxonomy":
        receipt.pop("failure_class", None)
        receipt["commands"][0].pop("failure_class", None)
    elif case in {"unknown-taxonomy", "infrastructure"}:
        receipt["commands"][0]["failure_class"] = "unknown" if case == "unknown-taxonomy" else "infrastructure"
    elif case == "reused":
        receipt["reused_from"] = receipt["run_id"]
    elif case == "utility":
        receipt["commands"][0]["check_role"] = "utility_guardrail"
    else:
        card(tmp_path, "other")
    rewrite(receipt)
    before = snapshot(tmp_path)
    assert capture(tmp_path, receipt, ident="other" if case == "wrong-card" else "cache") == 0
    assert log.read_evidence(tmp_path)[0] == []
    assert memory_reflect.run_payload(target=tmp_path, dry_run=True)["candidates"] == []
    assert snapshot(tmp_path) == before


def test_repeated_and_independent_failures_preserve_distinct_identity(tmp_path, failed_card):
    _, receipt, run = failed_card
    before = snapshot(tmp_path)
    for _ in range(3):
        assert capture(tmp_path, receipt) == 0
    (first,) = memory_reflect.run_payload(target=tmp_path, dry_run=True)["candidates"]
    assert len(first["evidence_ids"]) == 1
    assert capture(tmp_path, receipt) == 0
    assert memory_reflect.run_payload(target=tmp_path, dry_run=True)["candidates"] == [first]
    second = run()
    assert second["run_id"] != receipt["run_id"]
    assert capture(tmp_path, second) == 0
    (candidate,) = memory_reflect.run_payload(target=tmp_path, dry_run=True)["candidates"]
    assert len(candidate["evidence_ids"]) == 2
    assert candidate["candidate_id"] != first["candidate_id"]
    assert candidate["proof_count"] == candidate["proof_sources"] == 0
    assert snapshot(tmp_path) == before


def test_negative_blocks_same_card_without_contaminating_healthy_candidates(tmp_path, failed_card):
    from tests.test_memory_reflect import evidence, log as write_log

    path, receipt, _ = failed_card
    healthy = card(tmp_path, "healthy", body="Keep healthy state.")
    assert capture(tmp_path, receipt) == 0
    negative = log.read_evidence(tmp_path)[0][0]["evidence_id"]
    support = evidence(tmp_path, ".brigade/receipts/independent.json")
    for p in (path, healthy):
        write_log(tmp_path, p, (support, "outcome", "2026-10-21T12:00:00Z"))
    before = snapshot(tmp_path)
    candidates = memory_reflect.run_payload(target=tmp_path, dry_run=True)["candidates"]
    for c in candidates:
        if c["kind"] == "negative-outcome-review":
            continue
        assert negative not in c["evidence_ids"]
        assert c["proof_count"] == 1
        if "cache" in c["card_ids"]:
            assert negative in c["review_evidence_ids"]
            assert c["tier"] == 2 and c["diff"] == ""
        else:
            assert c["card_ids"] == ["healthy"]
            assert c["tier"] == 0 and c["diff"]
    assert {c["kind"] for c in candidates} == {"negative-outcome-review", "reinforce", "stale-newer-evidence"}
    assert snapshot(tmp_path) == before


def test_negative_citations_and_forged_support_rows_cannot_supply_proof(tmp_path, failed_card):
    from tests.test_memory_reflect import log as write_log

    path, receipt, _ = failed_card
    assert capture(tmp_path, receipt) == 0
    ref = log.read_evidence(tmp_path)[0][0]["evidence_id"]
    path.write_text(path.read_text().replace("evidence: []", f'evidence: ["receipt:{ref}#proof"]'))
    write_log(tmp_path, path, (ref, "outcome", receipt["completed_at"]), (ref, "reinforced", "2026-10-22T12:00:00Z"))
    before = snapshot(tmp_path)
    assert memory_reflect.run_payload(target=tmp_path, dry_run=True)["candidates"] == []
    assert snapshot(tmp_path) == before


def test_negative_append_failure_keeps_durable_outcome_and_card_bytes(tmp_path, failed_card, monkeypatch, capsys):
    from brigade import outcome_cmd

    _, receipt, _ = failed_card
    before = snapshot(tmp_path)
    capsys.readouterr()

    def fail(*args, **kwargs):
        raise OSError("private failure " + "x" * 2000)

    monkeypatch.setattr(log, "append_evidence", fail)
    assert capture(tmp_path, receipt) == 0
    output = capsys.readouterr()
    assert "warning: evidence log append failed" in output.err
    assert len(output.err) < 400
    assert len(outcome_cmd.load_records(tmp_path)) == 1
    assert log.read_evidence(tmp_path)[0] == []
    assert snapshot(tmp_path) == before


def test_negative_receipt_refused_as_explicit_proposal_support(tmp_path, failed_card):
    from brigade import memory_proposals as api, memory_proposal_v2_ops as delta

    path, receipt, _ = failed_card
    assert capture(tmp_path, receipt) == 0
    ref = log.read_evidence(tmp_path)[0][0]["evidence_id"]
    block = delta.blocks(delta.anchor_text(path.read_text(), "memory/cards/cache.md"))[0]
    before = snapshot(tmp_path)
    with pytest.raises(api.ProposalError, match="outcome evidence drift"):
        api.create_payload(
            target=tmp_path,
            relation="consolidate",
            source_paths=["memory/cards/cache.md"],
            anchor_blocks=True,
            reason="Inspect receipt evidence.",
            ops=[
                dict(
                    type="append_evidence",
                    card_path="memory/cards/cache.md",
                    block_id=block.block_id,
                    expected_block_hash=block.sha256,
                    evidence_ids=[ref],
                )
            ],
        )
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("cutoff", ["2026-10-20", "2026-10-21T00:00:00Z"])
def test_negative_uses_original_pending_window(tmp_path, failed_card, cutoff):
    path, _, run = failed_card
    path.write_text(path.read_text().replace("last_reviewed: 2026-10-01", f"last_consolidated: {cutoff}"))
    receipt = run()
    before = snapshot(tmp_path)
    assert capture(tmp_path, receipt) == 0
    assert log.read_evidence(tmp_path)[0]
    assert memory_reflect.run_payload(target=tmp_path, dry_run=True)["candidates"] == []
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("phase", ["propose", "review", "apply"])
@pytest.mark.parametrize("drift", [None, "receipt", "linked"])
def test_pending_negative_blocks_retained_positive_candidate(tmp_path, failed_card, phase, drift):
    from brigade import memory_proposals as api
    from brigade.memory_proposal_v2_reflect import propose_payload
    from tests.test_memory_reflect import evidence, log as write_log

    path, receipt, _ = failed_card
    support = evidence(tmp_path, ".brigade/receipts/healthy-support.json")
    write_log(tmp_path, path, (support, "outcome", "2026-10-19T12:00:00Z"))
    candidate = next(c for c in memory_reflect.run_payload(target=tmp_path)["candidates"] if c["kind"] == "reinforce")
    values = {}
    if phase != "propose":
        proposal = propose_payload(
            target=tmp_path, candidate_id=candidate["candidate_id"], reason="Inspect original evidence."
        )
        values = dict(target=tmp_path, proposal_id=proposal["id"], digest=proposal["digest"])
        if phase == "apply":
            api.review_payload(**values, reason="Inspected the original preview.")
    (tmp_path / log.LOG_PATH).chmod(0o600)
    assert capture(tmp_path, receipt) == 0
    assert any(row["kind"] == "negative-outcome" for row in log.read_evidence(tmp_path)[0])

    def attempt():
        if phase == "propose":
            return propose_payload(
                target=tmp_path, candidate_id=candidate["candidate_id"], reason="Inspect current evidence."
            )
        if phase == "review":
            return api.review_payload(**values, reason="Inspect current evidence.")
        return api.apply_payload(**values)

    if drift is not None:
        from tests.test_memory_outcome_evidence import drift_support

        with pytest.raises(api.ProposalError):
            attempt()
        drift_support(tmp_path, path, receipt, drift)
    before = snapshot(tmp_path)
    with pytest.raises(api.ProposalError):
        attempt()
    assert snapshot(tmp_path) == before
    assert not list(tmp_path.rglob("apply-receipt.json"))


def test_forged_negative_pointer_does_not_read_unrelated_file(tmp_path, failed_card, monkeypatch):
    path, receipt, _ = failed_card
    unrelated = tmp_path / "unrelated.json"
    unrelated.write_text("{}\n")
    log.append_evidence(
        tmp_path,
        card_path=path,
        evidence_id="unrelated.json",
        kind="negative-outcome",
        similarity=1.0,
        ts=receipt["completed_at"],
    )
    read_bytes = log.io.read_bytes

    def guarded_read(candidate, *args, **kwargs):
        assert candidate != unrelated, "negative row expanded receipt read authority"
        return read_bytes(candidate, *args, **kwargs)

    monkeypatch.setattr(log.io, "read_bytes", guarded_read)
    before = snapshot(tmp_path)
    assert memory_reflect.run_payload(target=tmp_path, dry_run=True)["candidates"] == []
    assert snapshot(tmp_path) == before


@pytest.fixture(params=["uncaptured", "append-failed", "malformed-text", "malformed-json"])
def unlogged_failure(tmp_path, failed_card, monkeypatch, request):
    path, receipt, _ = failed_card
    if request.param == "append-failed":
        with monkeypatch.context() as patch:

            def fail_append(*args, **kwargs):
                raise OSError("optional log unavailable")

            patch.setattr(log, "append_evidence", fail_append)
            assert capture(tmp_path, receipt) == 0
    elif request.param.startswith("malformed-"):
        receipt_path = tmp_path / ".brigade/work/verify-runs" / receipt["run_id"] / "receipt.json"
        receipt_path.write_text("not receipt JSON" if request.param == "malformed-text" else "{}")
    assert log.read_evidence(tmp_path)[0] == []
    ref = f".brigade/work/verify-runs/{receipt['run_id']}/receipt.json"
    return path, receipt, ref


def test_unlogged_failed_receipt_citation_is_not_positive_proof(tmp_path, unlogged_failure):
    from tests.test_memory_reflect import evidence, log as write_log

    path, _, ref = unlogged_failure
    path.write_text(path.read_text().replace("evidence: []", f'evidence: ["{ref}"]'))
    healthy = evidence(tmp_path, ".brigade/receipts/healthy-support.json")
    write_log(tmp_path, path, (healthy, "outcome", "2026-10-21T12:00:00Z"))
    before = snapshot(tmp_path)
    candidates = memory_reflect.run_payload(target=tmp_path, dry_run=True)["candidates"]
    assert {c["kind"] for c in candidates} == {"reinforce", "stale-newer-evidence"}
    for candidate in candidates:
        assert ref not in candidate["evidence_ids"]
        assert candidate["proof_count"] == 1
    assert snapshot(tmp_path) == before


def test_unlogged_failed_receipt_is_not_direct_proposal_support(tmp_path, unlogged_failure):
    from brigade import memory_proposals as api, memory_proposal_v2_ops as delta

    path, _, ref = unlogged_failure
    block = delta.blocks(delta.anchor_text(path.read_text(), "memory/cards/cache.md"))[0]
    before = snapshot(tmp_path)
    with pytest.raises(api.ProposalError, match="outcome evidence drift"):
        api.create_payload(
            target=tmp_path,
            relation="consolidate",
            source_paths=["memory/cards/cache.md"],
            anchor_blocks=True,
            reason="Inspect uncaptured receipt.",
            ops=[
                dict(
                    type="append_evidence",
                    card_path="memory/cards/cache.md",
                    block_id=block.block_id,
                    expected_block_hash=block.sha256,
                    evidence_ids=[ref],
                )
            ],
        )
    assert snapshot(tmp_path) == before


def test_unlogged_completed_receipt_keeps_valid_direct_support(tmp_path, verified_card):
    from brigade import memory_proposals as api, memory_proposal_v2_ops as delta

    path, receipt = verified_card
    assert log.read_evidence(tmp_path)[0] == []
    ref = f".brigade/work/verify-runs/{receipt['run_id']}/receipt.json"
    block = delta.blocks(delta.anchor_text(path.read_text(), "memory/cards/cache.md"))[0]
    before = snapshot(tmp_path)
    proposal = api.create_payload(
        target=tmp_path,
        relation="consolidate",
        source_paths=["memory/cards/cache.md"],
        anchor_blocks=True,
        reason="Inspect eligible completed receipt.",
        ops=[
            dict(
                type="append_evidence",
                card_path="memory/cards/cache.md",
                block_id=block.block_id,
                expected_block_hash=block.sha256,
                evidence_ids=[ref],
            )
        ],
    )
    assert proposal["ops"][0]["evidence_ids"] == [ref]
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("subject", ["unbound", "skill"])
def test_ordinary_completed_receipt_keeps_generic_support(tmp_path, failed_card, subject):
    from brigade import memory_proposals as api, memory_proposal_v2_ops as delta
    from tests.test_memory_reflect import evidence, log as write_log

    path, _, _ = failed_card
    kwargs = {"commands": [[sys.executable, "-c", "pass"]]}
    if subject == "skill":
        from tests.test_verify_trial import _write_skill_subject

        skill = _write_skill_subject(tmp_path)
        manifest = json.loads((tmp_path / "verify/manifests/negative-card.json").read_text())
        manifest["manifest_id"] = "positive-skill"
        manifest["subject"] = {
            "artifact_kind": "skill",
            "artifact_id": "brigade-work",
            "subject_path": skill.relative_to(tmp_path).as_posix(),
        }
        manifest["checks"][0]["command"] = f'{sys.executable} -c "pass"'
        verify_manifest.write_workspace_manifest(tmp_path, manifest)
        _track_workspace_manifest(tmp_path, "positive-skill")
        kwargs = {"manifest_id": "positive-skill"}
    output = io.StringIO()
    with redirect_stdout(output):
        assert verify_mod.verify_run(target=tmp_path, reuse=False, json_output=True, **kwargs) == 0
    receipt = json.loads(output.getvalue())
    assert receipt["status"] == "completed"
    if subject == "skill":
        assert receipt["subject_binding"]["artifact_kind"] == "skill"
        assert verify_trial.project_trial(receipt, target=tmp_path).eligible
    else:
        assert "subject_binding" not in receipt
    ref = f".brigade/work/verify-runs/{receipt['run_id']}/receipt.json"
    path.write_text(path.read_text().replace("evidence: []", f'evidence: ["{ref}"]'))
    healthy = evidence(tmp_path, ".brigade/receipts/healthy-support.json")
    write_log(tmp_path, path, (healthy, "outcome", "2026-10-21T12:00:00Z"))
    before = snapshot(tmp_path)
    candidates = memory_reflect.run_payload(target=tmp_path, dry_run=True)["candidates"]
    assert {c["kind"] for c in candidates} == {"reinforce", "stale-newer-evidence"}
    for candidate in candidates:
        assert candidate["proof_count"] == 2
        assert ref in candidate["evidence_ids"]
    block = delta.blocks(delta.anchor_text(path.read_text(), "memory/cards/cache.md"))[0]
    proposal = api.create_payload(
        target=tmp_path,
        relation="consolidate",
        source_paths=["memory/cards/cache.md"],
        anchor_blocks=True,
        reason="Inspect ordinary completed verification.",
        ops=[
            dict(
                type="append_evidence",
                card_path="memory/cards/cache.md",
                block_id=block.block_id,
                expected_block_hash=block.sha256,
                evidence_ids=[ref],
            )
        ],
    )
    assert proposal["ops"][0]["evidence_ids"] == [ref]
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("drift", ["receipt", "linked"])
def test_unresolved_negative_keeps_candidates_review_only(tmp_path, failed_card, drift):
    from tests.test_memory_outcome_evidence import drift_support
    from tests.test_memory_reflect import evidence, log as write_log

    path, receipt, _ = failed_card
    assert capture(tmp_path, receipt) == 0
    ref = log.read_evidence(tmp_path)[0][0]["evidence_id"]
    healthy = evidence(tmp_path, ".brigade/receipts/healthy-support.json")
    write_log(tmp_path, path, (healthy, "outcome", "2026-10-19T12:00:00Z"))
    drift_support(tmp_path, path, receipt, drift)
    before = snapshot(tmp_path)
    candidates = memory_reflect.run_payload(target=tmp_path, dry_run=True)["candidates"]
    assert {c["kind"] for c in candidates} == {"reinforce", "stale-newer-evidence"}
    for candidate in candidates:
        assert candidate["tier"] == 2 and candidate["diff"] == ""
        assert ref in candidate["review_evidence_ids"]
        assert ref in candidate["unresolved_evidence_ids"]
        assert ref not in candidate["evidence_ids"]
        assert candidate["proof_count"] == 1
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("corrupt", [False, True])
def test_manual_cutoff_cannot_hide_pending_negative(tmp_path, failed_card, corrupt):
    from brigade import memory_proposals as api, memory_proposal_v2_ops as delta
    from tests.test_memory_outcome_evidence import drift_support
    from tests.test_memory_reflect import evidence, log as write_log

    path, receipt, _ = failed_card
    assert capture(tmp_path, receipt) == 0
    if corrupt:
        drift_support(tmp_path, path, receipt, "receipt")
    later = evidence(tmp_path, ".brigade/receipts/later.json")
    write_log(tmp_path, path, (later, "near-match", "2026-10-21T12:00:00Z"))
    block = delta.blocks(delta.anchor_text(path.read_text(), "memory/cards/cache.md"))[0]
    before = snapshot(tmp_path)
    with pytest.raises(api.ProposalError, match="no valid delta ops remain"):
        api.create_payload(
            target=tmp_path,
            relation="consolidate",
            source_paths=["memory/cards/cache.md"],
            anchor_blocks=True,
            reason="Inspect later consolidation evidence.",
            ops=[
                dict(
                    type="append_evidence",
                    card_path="memory/cards/cache.md",
                    block_id=block.block_id,
                    expected_block_hash=block.sha256,
                    evidence_ids=[later],
                    last_consolidated="2026-10-21T12:00:00Z",
                )
            ],
        )
    assert snapshot(tmp_path) == before


def test_negative_receipt_disappears_without_blocking_healthy_cards(tmp_path, failed_card, monkeypatch):
    from brigade import memory_reflect_candidates as candidates
    from tests.test_memory_reflect import evidence, log as write_log

    _, receipt, _ = failed_card
    assert capture(tmp_path, receipt) == 0
    ref = log.read_evidence(tmp_path)[0][0]["evidence_id"]
    healthy_card = card(tmp_path, "healthy", body="Keep healthy state.")
    healthy = evidence(tmp_path, ".brigade/receipts/healthy-support.json")
    write_log(tmp_path, healthy_card, (healthy, "outcome", "2026-10-21T12:00:00Z"))
    original = candidates._resolved
    removed = False

    def disappear(target, refs, roots):
        nonlocal removed
        if not removed and ref in refs:
            (target / ref).unlink()
            removed = True
        return original(target, refs, roots)

    monkeypatch.setattr(candidates, "_resolved", disappear)
    before = snapshot(tmp_path)
    result = memory_reflect.run_payload(target=tmp_path, dry_run=True)["candidates"]
    assert removed and result
    assert all(c["card_ids"] == ["healthy"] and c["tier"] == 0 for c in result)
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize(
    "corruption",
    [
        "empty-binding",
        "null-binding",
        "text-binding",
        "nested-skill-binding",
        "removed-binding",
        "status-only",
        "identity",
        "commands",
        "failed-command",
        "missing-digest",
        "missing-schema",
        "missing-manifest-identity",
        "mismatched-manifest-identity",
    ],
)
def test_malformed_completed_receipt_cannot_become_generic_support(tmp_path, verified_card, corruption, monkeypatch):
    from brigade import memory_proposals as api, memory_proposal_v2_ops as delta
    from tests.test_memory_reflect import evidence, log as write_log

    path, receipt = verified_card
    if corruption in {"missing-manifest-identity", "mismatched-manifest-identity"}:
        from tests.test_verify_trial import _run_manifest_verify, _write_fixture_manifest, _write_skill_subject

        _write_skill_subject(tmp_path)
        _write_fixture_manifest(tmp_path)
        receipt = _run_manifest_verify(tmp_path, "test-skill-fixture", monkeypatch=monkeypatch, start_session=False)
        assert receipt["subject_binding"]["artifact_kind"] == "skill"
        assert verify_trial.project_trial(receipt, target=tmp_path).eligible
    ref = f".brigade/work/verify-runs/{receipt['run_id']}/receipt.json"
    if corruption == "missing-manifest-identity":
        receipt.pop("verify_manifest_id")
    elif corruption == "mismatched-manifest-identity":
        receipt["verify_manifest_id"] = "other-manifest"
    elif corruption == "empty-binding":
        receipt["subject_binding"] = {}
    elif corruption == "null-binding":
        receipt["subject_binding"] = None
    elif corruption == "text-binding":
        receipt["subject_binding"] = ""
    elif corruption == "nested-skill-binding":
        receipt["subject_binding"]["artifact_kind"] = "skill"
        receipt["subject_binding"]["manifest_binding"] = "malformed"
    elif corruption == "removed-binding":
        receipt.pop("subject_binding")
    elif corruption == "status-only":
        receipt = {"status": "completed"}
    else:
        receipt.pop("subject_binding")
        if corruption == "identity":
            receipt["run_id"] = "different-run"
        elif corruption == "commands":
            receipt["commands"] = {}
        elif corruption == "failed-command":
            receipt["commands"][0]["exit_code"] = 1
        elif corruption == "missing-digest":
            receipt.pop("digests")
        elif corruption == "missing-schema":
            receipt.pop("schema_version")
    if corruption not in {"removed-binding", "missing-digest", "missing-schema"}:
        from tests.test_verify_trial import _stamp_receipt_digest

        # These rows test envelope validation independently of stale checksums.
        _stamp_receipt_digest(receipt)
    (tmp_path / ref).write_text(json.dumps(receipt))
    path.write_text(path.read_text().replace("evidence: []", f'evidence: ["{ref}"]'))
    healthy = evidence(tmp_path, ".brigade/receipts/healthy-support.json")
    write_log(tmp_path, path, (healthy, "outcome", "2026-10-21T12:00:00Z"))
    before = snapshot(tmp_path)
    candidates = memory_reflect.run_payload(target=tmp_path, dry_run=True)["candidates"]
    assert {c["kind"] for c in candidates} == {"reinforce", "stale-newer-evidence"}
    for candidate in candidates:
        assert ref not in candidate["evidence_ids"]
        assert candidate["proof_count"] == 1
    block = delta.blocks(delta.anchor_text(path.read_text(), "memory/cards/cache.md"))[0]
    with pytest.raises(api.ProposalError, match="outcome evidence drift"):
        api.create_payload(
            target=tmp_path,
            relation="consolidate",
            source_paths=["memory/cards/cache.md"],
            anchor_blocks=True,
            reason="Inspect malformed completed receipt.",
            ops=[
                dict(
                    type="append_evidence",
                    card_path="memory/cards/cache.md",
                    block_id=block.block_id,
                    expected_block_hash=block.sha256,
                    evidence_ids=[ref],
                )
            ],
        )
    assert snapshot(tmp_path) == before
