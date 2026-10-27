"""Verified card capture supplies reflection support without editing cards."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from brigade import cli, localio, outcome_cmd, verify_manifest, verify_trial
from brigade import memory_evidence_log as log
from brigade.work_cmd import verification as verify_mod
from tests.test_outcome_cmd import _write_run_receipt
from tests.test_memory_reflect import card, command, snapshot
from tests.test_verify_trial import _init_verify_git_repo, _run_manifest_verify, _track_workspace_manifest

pytestmark = pytest.mark.skipif(
    os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"),
    reason="safe directory descriptors unavailable",
)


@pytest.fixture
def verified_card(tmp_path, monkeypatch, request):
    monkeypatch.setattr(verify_mod.helpers, "_now", lambda: datetime(2026, 10, 20, 12, tzinfo=timezone.utc))
    _init_verify_git_repo(tmp_path)
    path = card(tmp_path, "cache")
    role = getattr(request, "param", "effectiveness")
    if role in ("linked", "anchored-linked"):
        card(tmp_path, "linked")
        path.write_text(path.read_text() + "[[linked]]\n")
        if role == "anchored-linked":
            from brigade import memory_proposal_v2_ops as delta

            path.write_text(delta.anchor_text(path.read_text(), "memory/cards/cache.md"))
        role = "effectiveness"
    verify_manifest.write_workspace_manifest(
        tmp_path,
        {
            "schema": "brigade.verify_manifest.v1",
            "schema_version": 1,
            "manifest_id": "card-fixture",
            "binding_mode": "fixture_eval",
            "verifier_id": "brigade.verify.fixture",
            "subject": {
                "artifact_kind": "card",
                "artifact_id": "cache",
                "subject_path": "memory/cards/cache.md",
            },
            "fixture": {
                "manifest_id": "adversarial-failed-verification",
                "case_id": "report-failing-tests",
                "check_id": "fixture.echo-ok",
            },
            "checks": [
                {
                    "check_id": "fixture.echo-ok",
                    "check_role": role,
                    "command": f"{sys.executable} -c \"print('ok')\"",
                }
            ],
        },
    )
    _track_workspace_manifest(tmp_path, "card-fixture")
    receipt = _run_manifest_verify(tmp_path, "card-fixture", monkeypatch=monkeypatch, start_session=False)
    assert verify_trial.project_trial(receipt, target=tmp_path).eligible
    return path, receipt


def capture(target, receipt, *, ident="cache", kind="card"):
    return cli.main(
        ["outcome", "capture", ident, "--kind", kind, "--target", str(target), "--run-id", receipt["run_id"]]
    )


def rewrite(receipt):
    receipt["digests"] = {
        "algorithm": "sha256",
        "logs": {},
        "receipt_sha256": localio.canonical_json_digest(receipt, exclude_keys={"digests"}),
    }
    (Path(receipt["path"]) / "receipt.json").write_text(json.dumps(receipt))


def test_verified_capture_reflects_one_proof_without_refreshing_or_writing_cards(tmp_path, verified_card, capsys):
    path, receipt = verified_card
    before = snapshot(tmp_path)
    for _ in range(2):
        assert capture(tmp_path, receipt) == 0
    rows, warnings, unsupported = log.read_evidence(tmp_path)
    assert not warnings and not unsupported
    ref = f".brigade/work/verify-runs/{receipt['run_id']}/receipt.json"
    assert rows
    assert {(r["evidence_id"], r["ts"], r["kind"]) for r in rows} == {(ref, receipt["completed_at"], "outcome")}
    capsys.readouterr()
    code, result = command(tmp_path, capsys, "run", "--dry-run")
    assert code == 0
    assert {c["kind"] for c in result["candidates"]} == {"reinforce", "stale-newer-evidence"}
    for candidate in result["candidates"]:
        assert candidate["proof_count"] == 1
        assert candidate["evidence_ids"] == [ref]
        assert candidate["evidence_sources"] == {ref: ".brigade/work/verify-runs"}
    assert f"+last_consolidated: {receipt['completed_at']}" in result["candidates"][0]["diff"]
    assert snapshot(tmp_path) == before
    assert path.read_bytes() == before["memory/cards/cache.md"][0]


@pytest.mark.parametrize(
    "case",
    [
        "caller",
        "skill",
        "changed",
        "missing",
        "symlink",
        "reused",
        "audit",
        "failed",
        "malformed_role",
        "fingerprint",
        "kind",
        "id",
    ],
)
def test_capture_rejects_non_supporting_receipts(tmp_path, verified_card, case, capsys):
    path, receipt = verified_card
    ident, kind = "cache", "card"
    if case == "caller":
        card(tmp_path, "other")
        ident = "other"
    elif case == "skill":
        kind = "skill"
    elif case == "changed":
        path.write_text(path.read_text() + "Changed claim.\n")
    elif case == "missing":
        path.unlink()
    elif case == "symlink":
        other = tmp_path / "other.md"
        other.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(other)
    elif case == "reused":
        receipt["reused_from"] = receipt["run_id"]
    elif case == "audit":
        receipt.pop("subject_binding")
    elif case == "failed":
        receipt["status"] = "failed"
        receipt["commands"][0].update(status="failed", exit_code=1)
    elif case == "malformed_role":
        receipt["commands"][0]["check_role"] = []
    else:
        receipt["subject_binding"][
            {"fingerprint": "content_fingerprint", "kind": "artifact_kind", "id": "artifact_id"}[case]
        ] = "other"
    rewrite(receipt)
    before = snapshot(tmp_path)
    capsys.readouterr()
    assert capture(tmp_path, receipt, ident=ident, kind=kind) == 0
    assert len(outcome_cmd.load_records(tmp_path)) == 1
    assert "evidence log append failed" not in capsys.readouterr().err
    assert log.read_evidence(tmp_path)[0] == []
    assert snapshot(tmp_path) == before


def test_optional_append_failure_warns_after_capture_and_retry_supplies_evidence(
    tmp_path, verified_card, monkeypatch, capsys
):
    _, receipt = verified_card
    before = snapshot(tmp_path)
    capsys.readouterr()
    with monkeypatch.context() as patch:

        def fail(*args, **kwargs):
            raise OSError("private failure " + "x" * 2000)

        patch.setattr(log, "append_evidence", fail)
        assert capture(tmp_path, receipt) == 0
    output = capsys.readouterr()
    assert "warning: evidence log append failed" in output.err
    assert len(output.err) < 400
    assert len(outcome_cmd.load_records(tmp_path)) == 1
    assert log.read_evidence(tmp_path)[0] == []
    assert capture(tmp_path, receipt) == 0
    assert len(log.read_evidence(tmp_path)[0]) == 1
    assert snapshot(tmp_path) == before


def test_failed_outcome_append_never_supplies_evidence(tmp_path, verified_card, monkeypatch):
    _, receipt = verified_card

    def fail(*args, **kwargs):
        raise outcome_cmd.OutcomeLedgerError("ledger unavailable")

    monkeypatch.setattr(outcome_cmd, "append_records", fail)
    assert capture(tmp_path, receipt) == 1
    assert log.read_evidence(tmp_path)[0] == []


@pytest.mark.parametrize("verified_card", ["utility_guardrail"], indirect=True)
def test_eligible_verification_without_effectiveness_is_not_support(tmp_path, verified_card):
    _, receipt = verified_card
    assert verify_trial.project_trial(receipt, target=tmp_path).eligible
    assert capture(tmp_path, receipt) == 0
    assert log.read_evidence(tmp_path)[0] == []


@pytest.mark.parametrize("source", ["run", "manual"])
def test_non_verify_card_signals_do_not_supply_support(tmp_path, verified_card, source):
    _, receipt = verified_card
    before = snapshot(tmp_path)
    if source == "run":
        _write_run_receipt(tmp_path, run_id="card-run", status="ok")
        assert (
            outcome_cmd.capture(target=tmp_path, artifact_id="cache", artifact_kind="card", run_receipt="card-run") == 0
        )
    else:
        assert (
            outcome_cmd.record(
                target=tmp_path,
                artifact_id="cache",
                artifact_kind="card",
                source="friction",
                status="cleared",
                evidence_ref=str(Path(receipt["path"]) / "receipt.json"),
            )
            == 0
        )
    assert log.read_evidence(tmp_path)[0] == []
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("verified_card", ["linked"], indirect=True)
@pytest.mark.parametrize("drift", ["card", "linked", "receipt"])
def test_capture_admission_interleaving_preserves_outcome_without_support(
    tmp_path, verified_card, monkeypatch, capsys, drift
):
    path, receipt = verified_card
    original = verify_trial.project_trial

    def interleave(value, **kwargs):
        projection = original(value, **kwargs)
        assert projection.eligible
        if drift == "receipt":
            (Path(receipt["path"]) / "receipt.json").write_text("{}")
        else:
            changed = path if drift == "card" else tmp_path / "memory/cards/linked.md"
            changed.write_text(changed.read_text().replace("Keep", "Never keep"))
        return projection

    capsys.readouterr()
    monkeypatch.setattr(verify_trial, "project_trial", interleave)
    assert capture(tmp_path, receipt) == 0
    output = capsys.readouterr()
    assert len(outcome_cmd.load_records(tmp_path)) == 1
    assert log.read_evidence(tmp_path)[0] == []
    assert "warning: evidence log append failed" in output.err
    assert len(output.err) < 400


def drift_support(target, path, receipt, drift):
    retained = Path(receipt["path"]) / "receipt.json"
    if drift == "card":
        path.write_text(path.read_text().replace("Keep", "Never keep"))
    elif drift == "linked":
        linked = target / "memory/cards/linked.md"
        linked.write_text(linked.read_text().replace("Keep", "Never keep"))
    elif drift == "missing":
        retained.unlink()
    elif drift == "identity":
        changed = json.loads(retained.read_text())
        changed["run_id"] = "other-receipt"
        rewrite(changed)
    elif drift == "digest":
        changed = json.loads(retained.read_text())
        changed["completed_at"] = "2026-10-21T12:00:00Z"
        retained.write_text(json.dumps(changed))
    else:
        retained.write_text("{}")


@pytest.mark.parametrize("verified_card", ["linked"], indirect=True)
@pytest.mark.parametrize("drift", ["card", "linked", "receipt", "missing", "identity", "digest"])
def test_reflection_excludes_drifted_outcome_support(tmp_path, verified_card, capsys, drift):
    path, receipt = verified_card
    assert capture(tmp_path, receipt) == 0
    rows = log.read_evidence(tmp_path)[0]
    assert rows
    drift_support(tmp_path, path, receipt, drift)
    before = snapshot(tmp_path)
    capsys.readouterr()
    code, result = command(tmp_path, capsys, "run", "--dry-run")
    assert code == 0
    assert result["candidates"] == []
    assert log.read_evidence(tmp_path)[0] == rows
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("verified_card", ["linked"], indirect=True)
@pytest.mark.parametrize("drift", ["linked", "receipt"])
@pytest.mark.parametrize("stage", ["candidate", "proposal"])
def test_retained_outcome_preview_refuses_evidence_drift(tmp_path, verified_card, drift, stage):
    from brigade import memory_proposals as api, memory_reflect
    from brigade.memory_proposal_v2_reflect import propose_payload

    path, receipt = verified_card
    assert capture(tmp_path, receipt) == 0
    candidate = next(c for c in memory_reflect.run_payload(target=tmp_path)["candidates"] if c["kind"] == "reinforce")
    if stage == "proposal":
        proposal = propose_payload(target=tmp_path, candidate_id=candidate["candidate_id"], reason="Inspect support.")
        values = dict(target=tmp_path, proposal_id=proposal["id"], digest=proposal["digest"])
        api.review_payload(**values, reason="Inspect the exact preview.")
    drift_support(tmp_path, path, receipt, drift)
    before = snapshot(tmp_path)
    with pytest.raises(api.ProposalError):
        if stage == "candidate":
            propose_payload(target=tmp_path, candidate_id=candidate["candidate_id"], reason="Inspect support.")
        else:
            api.apply_payload(**values)
    assert snapshot(tmp_path) == before


def test_changed_card_cannot_re_admit_logged_outcome_via_metadata_citation(tmp_path, verified_card, capsys):
    path, receipt = verified_card
    assert capture(tmp_path, receipt) == 0
    ref = f".brigade/work/verify-runs/{receipt['run_id']}/receipt.json"
    path.write_text(path.read_text().replace("evidence: []", f'evidence: ["receipt:{ref}#proof"]'))
    before = snapshot(tmp_path)
    capsys.readouterr()
    code, result = command(tmp_path, capsys, "run", "--dry-run")
    assert code == 0
    assert result["candidates"] == []
    assert log.read_evidence(tmp_path)[0]
    assert snapshot(tmp_path) == before


def test_repeated_outcome_validation_is_distinct_and_newest_window_bounded(tmp_path, verified_card, monkeypatch):
    from brigade import memory_reflect
    from tests.test_memory_reflect import evidence, log as write_log

    path, receipt = verified_card
    assert capture(tmp_path, receipt) == 0
    ref = f".brigade/work/verify-runs/{receipt['run_id']}/receipt.json"
    write_log(tmp_path, path, *[(f"receipt:{ref}#proof", "outcome", receipt["completed_at"])] * 1000)
    original = log.validate_outcome_support
    checked = []

    def observe(*args, **kwargs):
        checked.append(kwargs["ref"])
        return original(*args, **kwargs)

    monkeypatch.setattr(log, "validate_outcome_support", observe)
    result = memory_reflect.run_payload(target=tmp_path, dry_run=True)
    assert checked == [ref]
    assert all(c["proof_count"] == 1 for c in result["candidates"])
    checked.clear()
    newest = [f".brigade/work/verify-runs/new-{i:03}/receipt.json" for i in range(70)]
    write_log(tmp_path, path, *[(evidence(tmp_path, item), "outcome", "2026-10-21T12:00:00Z") for item in newest])
    result = memory_reflect.run_payload(target=tmp_path, dry_run=True)
    assert len(checked) == 64
    assert set(checked) == set(newest[-64:])
    assert result["candidates"] == []


@pytest.mark.parametrize("run_id", ["..", r"bad\run", "x" * 1050])
def test_malformed_verify_pointer_preserves_generic_cli_candidates(tmp_path, capsys, run_id):
    from tests.test_memory_reflect import evidence, log as write_log

    path = card(tmp_path, "cache")
    ref = evidence(tmp_path, ".brigade/receipts/generic.json")
    write_log(tmp_path, path, (ref, "outcome", "2026-10-20T12:00:00Z"))
    code, baseline = command(tmp_path, capsys, "run", "--dry-run")
    assert code == 0 and len(baseline["candidates"]) == 2
    write_log(
        tmp_path,
        path,
        (f".brigade/work/verify-runs/{run_id}/receipt.json", "outcome", "2026-10-21T12:00:00Z"),
    )
    assert log.read_evidence(tmp_path)[1:] == (0, False)
    code, result = command(tmp_path, capsys, "run", "--dry-run")
    assert code == 0
    assert result["candidates"] == baseline["candidates"]


@pytest.mark.parametrize("verified_card", ["linked"], indirect=True)
@pytest.mark.parametrize("stage", ["reflection", "proposal"])
def test_oversized_linked_support_never_uses_unrestricted_reads(tmp_path, verified_card, monkeypatch, stage):
    from brigade import memory_proposals as api, memory_reflect
    from brigade.memory_proposal_v2_reflect import propose_payload

    _, receipt = verified_card
    assert capture(tmp_path, receipt) == 0
    candidate = next(c for c in memory_reflect.run_payload(target=tmp_path)["candidates"] if c["kind"] == "reinforce")
    linked = tmp_path / "memory/cards/linked.md"
    linked.write_bytes(b"x" * (log.io.MAX_CARD_BYTES + 1))
    unsafe_reads = []
    for name in ("read_text", "read_bytes"):
        original = getattr(Path, name)

        def observe(path, *args, _original=original, **kwargs):
            if path == linked:
                unsafe_reads.append(path)
            return _original(path, *args, **kwargs)

        monkeypatch.setattr(Path, name, observe)
    if stage == "reflection":
        result = memory_reflect.run_payload(target=tmp_path, dry_run=True)
        assert result["status"] == "completed" and result["candidates"] == []
    else:
        with pytest.raises(api.ProposalError):
            propose_payload(target=tmp_path, candidate_id=candidate["candidate_id"], reason="Inspect support.")
    assert unsafe_reads == []


@pytest.mark.parametrize("verified_card", ["linked"], indirect=True)
@pytest.mark.parametrize("bound", ["discovery", "bytes"])
def test_support_bounds_precede_closure_reads(tmp_path, verified_card, monkeypatch, bound):
    path, receipt = verified_card
    assert capture(tmp_path, receipt) == 0
    text = path.read_text()
    if bound == "discovery":
        for index in range(log.io.MAX_FILES):
            (path.parent / f"unused-{index}.md").touch()
    else:
        monkeypatch.setattr(log.io, "MAX_CORPUS_BYTES", len(text.encode()))
    linked = path.parent / "linked.md"
    reads = []
    original = log.io.read_bytes

    def observe(value, **kwargs):
        reads.append((value, kwargs.get("limit")))
        return original(value, **kwargs)

    monkeypatch.setattr(log.io, "read_bytes", observe)
    assert not log.validate_outcome_support(
        tmp_path,
        card_path="memory/cards/cache.md",
        card_text=text,
        ref=f".brigade/work/verify-runs/{receipt['run_id']}/receipt.json",
    )
    if bound == "discovery":
        assert not any(value.parent == path.parent for value, _ in reads)
    else:
        assert all(limit == 0 for value, limit in reads if value == linked)


@pytest.mark.parametrize("verified_card", ["linked"], indirect=True)
def test_bounded_support_preserves_legacy_link_fingerprint(tmp_path, verified_card, monkeypatch):
    path, receipt = verified_card
    linked = path.parent / "linked.md"
    linked.rename(path.parent / "Linked.md")
    path.write_text(path.read_text().replace("[[linked]]", "[[cards/LINKED.md#section|alias]] [[absent]]"))
    linked = path.parent / "Linked.md"
    linked.write_text(linked.read_text() + "[[CACHE]]\n")
    # Obtain a fresh binding from the real verifier after editing the closure.
    receipt = _run_manifest_verify(tmp_path, "card-fixture", monkeypatch=monkeypatch, start_session=False)
    assert receipt["subject_binding"]["content_fingerprint"] == outcome_cmd.artifact_fingerprint(
        tmp_path, "cache", "card"
    )
    assert verify_trial.project_trial(receipt, target=tmp_path).eligible
    text = path.read_text()
    unsafe_reads = []
    for name in ("read_text", "read_bytes"):
        original = getattr(Path, name)

        def observe(value, *args, _original=original, **kwargs):
            if value.parent == path.parent:
                unsafe_reads.append(value)
            return _original(value, *args, **kwargs)

        monkeypatch.setattr(Path, name, observe)
    assert log.validate_outcome_support(
        tmp_path,
        card_path="memory/cards/cache.md",
        card_text=text,
        ref=f".brigade/work/verify-runs/{receipt['run_id']}/receipt.json",
    )
    assert unsafe_reads == []


@pytest.mark.parametrize("first_kind", ["reinforce", "stale-newer-evidence"])
@pytest.mark.parametrize("second_support", ["generic", "verified"])
def test_successive_approved_consolidations_preserve_historical_receipts(
    tmp_path, verified_card, monkeypatch, capsys, first_kind, second_support
):
    from brigade import memory_proposals as api
    from tests.test_memory_reflect import evidence, log as write_log

    path, receipt = verified_card
    first_ref = f".brigade/work/verify-runs/{receipt['run_id']}/receipt.json"
    refs = [first_ref]
    for iteration in range(2):
        if iteration == 0:
            assert capture(tmp_path, receipt) == 0
        else:
            monkeypatch.setattr(verify_mod.helpers, "_now", lambda: datetime(2026, 10, 21, 12, tzinfo=timezone.utc))
            if second_support == "verified":
                receipt = _run_manifest_verify(tmp_path, "card-fixture", monkeypatch=monkeypatch, start_session=False)
                assert capture(tmp_path, receipt) == 0
                refs.append(f".brigade/work/verify-runs/{receipt['run_id']}/receipt.json")
            else:
                refs.append(evidence(tmp_path, ".brigade/receipts/new.json"))
                write_log(tmp_path, path, (refs[-1], "outcome", "2026-10-21T12:00:00Z"))
        capsys.readouterr()
        code, result = command(tmp_path, capsys, "run")
        assert code == 0
        kind = first_kind if iteration == 0 else "stale-newer-evidence"
        candidate = next(c for c in result["candidates"] if c["kind"] == kind)
        # A historical receipt is preserved as a citation, never re-admitted as current support.
        assert candidate["evidence_ids"] == [refs[-1]]
        code, proposal = command(tmp_path, capsys, "propose", candidate["candidate_id"], "--reason", "Review support.")
        assert code == 0, proposal
        assert set(proposal["op_evidence"]) == set(refs)
        if iteration:
            assert candidate["diff"] == proposal["diff"]
        for action in ("review", "apply"):
            args = [
                "memory",
                "proposal",
                action,
                proposal["id"],
                "--digest",
                proposal["digest"],
                "--target",
                str(tmp_path),
                "--json",
            ]
            if action == "review":
                args.extend(["--reason", "Inspected the exact evidence delta."])
            assert cli.main(args) == 0, capsys.readouterr()
            output = json.loads(capsys.readouterr().out)
            assert output["status"] == ("accepted" if action == "review" else "committed")
        assert api._meta(path.read_text())["evidence"] == sorted(refs)
        assert int(api._meta(path.read_text())["proof_count"]) == candidate["proof_count"]
    assert api._meta(path.read_text())["last_consolidated"] == (
        receipt["completed_at"] if second_support == "verified" else "2026-10-21T12:00:00Z"
    )


@pytest.mark.parametrize("verified_card", ["anchored-linked"], indirect=True)
@pytest.mark.parametrize("drift", ["card", "linked", "receipt"])
def test_uncited_obsolete_outcome_does_not_block_new_consolidation(tmp_path, verified_card, capsys, drift):
    from brigade import memory_proposals as api
    from tests.test_memory_reflect import evidence, log as write_log

    path, receipt = verified_card
    assert capture(tmp_path, receipt) == 0
    old_rows = log.read_evidence(tmp_path)[0]
    drift_support(tmp_path, path, receipt, drift)
    assert api._meta(path.read_text())["evidence"] == []
    ref = evidence(tmp_path, ".brigade/receipts/new.json")
    stamp = "2026-10-21T12:00:00Z"
    write_log(tmp_path, path, (ref, "outcome", stamp))
    capsys.readouterr()
    code, result = command(tmp_path, capsys, "run")
    assert code == 0
    candidate = next(c for c in result["candidates"] if c["kind"] == "stale-newer-evidence")
    assert candidate["evidence_ids"] == [ref]
    code, proposal = command(tmp_path, capsys, "propose", candidate["candidate_id"], "--reason", "Review new support.")
    assert code == 0, proposal
    assert candidate["diff"] == proposal["diff"]
    assert set(proposal["op_evidence"]) == {ref}
    for action in ("review", "apply"):
        args = [
            "memory",
            "proposal",
            action,
            proposal["id"],
            "--digest",
            proposal["digest"],
            "--target",
            str(tmp_path),
            "--json",
        ]
        if action == "review":
            args.extend(["--reason", "Inspected the current support."])
        assert cli.main(args) == 0, capsys.readouterr()
        assert json.loads(capsys.readouterr().out)["status"] == ("accepted" if action == "review" else "committed")
    meta = api._meta(path.read_text())
    assert meta["evidence"] == [ref]
    assert meta["last_consolidated"] == stamp
    assert int(meta["proof_count"]) == 1
    assert log.read_evidence(tmp_path)[0][: len(old_rows)] == old_rows


def test_uncited_eligible_outcome_still_blocks_consolidation(tmp_path, verified_card):
    from brigade import memory_proposals as api
    from brigade import memory_proposal_v2_ops as delta
    from tests.test_memory_reflect import evidence, log as write_log

    path, receipt = verified_card
    assert capture(tmp_path, receipt) == 0
    ref = evidence(tmp_path, ".brigade/receipts/new.json")
    stamp = "2026-10-21T12:00:00Z"
    write_log(tmp_path, path, (ref, "outcome", stamp))
    block = delta.blocks(delta.anchor_text(path.read_text(), "memory/cards/cache.md"))[0]
    before = snapshot(tmp_path)
    with pytest.raises(api.ProposalError, match="no valid"):
        api.create_payload(
            target=tmp_path,
            relation="consolidate",
            source_paths=["memory/cards/cache.md"],
            ops=[
                dict(
                    type="append_evidence",
                    card_path="memory/cards/cache.md",
                    block_id=block.block_id,
                    expected_block_hash=block.sha256,
                    evidence_ids=[ref],
                    last_consolidated=stamp,
                )
            ],
            anchor_blocks=True,
            reason="Try to skip eligible uncited support.",
        )
    assert snapshot(tmp_path) == before


def test_cited_outcome_explicitly_reused_as_support_still_requires_live_binding(tmp_path, verified_card):
    from brigade import memory_proposals as api
    from brigade import memory_proposal_v2_ops as delta

    path, receipt = verified_card
    assert capture(tmp_path, receipt) == 0
    ref = f".brigade/work/verify-runs/{receipt['run_id']}/receipt.json"
    path.write_text(path.read_text().replace("evidence: []", f'evidence: ["{ref}"]'))
    anchored = delta.anchor_text(path.read_text(), "memory/cards/cache.md")
    block = delta.blocks(anchored)[0]
    before = snapshot(tmp_path)
    with pytest.raises(api.ProposalError, match="outcome evidence drift"):
        api.create_payload(
            target=tmp_path,
            relation="consolidate",
            source_paths=["memory/cards/cache.md"],
            ops=[
                dict(
                    type="append_evidence",
                    card_path="memory/cards/cache.md",
                    block_id=block.block_id,
                    expected_block_hash=block.sha256,
                    evidence_ids=[ref],
                )
            ],
            anchor_blocks=True,
            reason="Try to reuse a cited receipt as live support.",
        )
    assert snapshot(tmp_path) == before


def test_historical_receipt_bytes_remain_bound_through_review_and_apply(tmp_path, verified_card):
    from brigade import memory_proposals as api, memory_reflect
    from brigade.memory_proposal_v2_reflect import propose_payload
    from tests.test_memory_proposal_v2 import apply
    from tests.test_memory_reflect import evidence, log as write_log

    path, receipt = verified_card
    assert capture(tmp_path, receipt) == 0
    first = next(c for c in memory_reflect.run_payload(target=tmp_path)["candidates"] if c["kind"] == "reinforce")
    proposal = propose_payload(target=tmp_path, candidate_id=first["candidate_id"], reason="Inspect first support.")
    assert apply(tmp_path, proposal)["status"] == "committed"
    ref = evidence(tmp_path, ".brigade/receipts/new.json")
    write_log(tmp_path, path, (ref, "outcome", "2026-10-21T12:00:00Z"))
    second = next(c for c in memory_reflect.run_payload(target=tmp_path)["candidates"] if c["kind"] == "reinforce")
    proposal = propose_payload(target=tmp_path, candidate_id=second["candidate_id"], reason="Inspect new support.")
    assert second["diff"] == proposal["diff"]
    values = dict(target=tmp_path, proposal_id=proposal["id"], digest=proposal["digest"])
    api.review_payload(**values, reason="Inspect current and retained evidence bytes.")
    retained = Path(receipt["path"]) / "receipt.json"
    historical_ref = retained.relative_to(tmp_path).as_posix()
    assert historical_ref in proposal["op_evidence"]
    assert historical_ref not in proposal["ops"][0]["evidence_ids"]
    retained.write_text(retained.read_text() + "\n")
    before = snapshot(tmp_path)
    with pytest.raises(api.ProposalError, match="evidence or reference drift"):
        api.apply_payload(**values)
    assert snapshot(tmp_path) == before
