"""Authenticated verify evidence and in-flight correction regressions."""

import json
import subprocess
import sys

import pytest

from brigade import lesson_activity as activity, lesson_admission as admission, lesson_review as review
from brigade import localio, receipt_signing, verify_manifest, work_cmd
from brigade.handoff_ingest_rules import sha256_hex
from tests.test_lesson_review import event, proposal, seat, scope as lesson_scope


@pytest.fixture
def scope(tmp_path, monkeypatch):
    return lesson_scope.__wrapped__(tmp_path, monkeypatch)


@pytest.mark.parametrize("damage", ["unsigned", "self-digest", "other-session", "missing-session", "other-key"])
def test_forged_manifest_receipt_pair_is_refused(scope, monkeypatch, damage):
    event(scope)
    event(scope)
    candidate = proposal(scope)
    for reference in (candidate["failure_evidence"], candidate["evidence"]):
        path = scope / reference
        receipt = json.loads(path.read_text())
        if damage == "unsigned":
            receipt["digests"].pop("workspace_signature", None)
        elif damage == "self-digest":
            receipt["subject_binding"]["verifier_identity"]["session_id"] = "invented-verifier"
            receipt["digests"]["receipt_sha256"] = localio.canonical_json_digest(receipt, exclude_keys={"digests"})
        elif damage == "other-key":
            receipt["digests"]["workspace_signature"] = receipt_signing.sign(
                receipt["digests"]["receipt_sha256"], b"x" * 32
            )
        else:
            receipt["producer_work_session_id"] = "other-session" if damage == "other-session" else None
            # Even authentic evidence from another session must not qualify.
            digest = localio.canonical_json_digest(receipt, exclude_keys={"digests"})
            receipt["digests"]["receipt_sha256"] = digest
            receipt_signing.stamp_workspace_signature(scope, receipt)
        path.write_text(json.dumps(receipt))
    seat(monkeypatch, candidate)
    assert review.run(scope, now=111)["status"] == "rejected"
    assert not list((scope / "memory/handoff-inbox").glob("*.md"))
    assert activity.load(scope)["reviewed"] == 0


@pytest.mark.parametrize("phase", ["lesson-select", "lesson-propose"])
def test_prompt_during_either_worker_phase_interrupts(scope, monkeypatch, phase):
    event(scope)
    event(scope)
    candidate = proposal(scope)
    original = review._invoke
    seat(monkeypatch, candidate)

    def invoke(*args, **kwargs):
        response = original(*args, **kwargs)
        if args[-1] == phase:
            activity.record_event(
                scope, "UserPromptSubmit", {"prompt": "Correction: the prior claim is false."}, now=111
            )
        return response

    monkeypatch.setattr(review, "_invoke", invoke)
    assert review.run(scope, now=111)["status"] == "interrupted"
    state = activity.load(scope)
    assert state["reviewed"] == 0
    assert state["last_activity"] == 111
    assert state["events"][-1]["text"] == "Correction: the prior claim is false."
    assert not list((scope / "memory/handoff-inbox").glob("*.md"))


def test_prompt_spool_keeps_multiple_corrections(scope):
    prompts = ["Correction: use atomic writes for publication.", "Also keep the original evidence for review."]
    for prompt in prompts:
        activity.record_event(scope, "UserPromptSubmit", {"prompt": prompt}, now=99)
    event(scope)
    assert [e["text"] for e in activity.load(scope)["events"] if e["kind"] == "UserPromptSubmit"] == prompts


@pytest.mark.parametrize("hook_kind", ["PostToolUse", "PostToolUseFailure"])
def test_failed_verify_observation_survives_routine_activity(scope, monkeypatch, hook_kind):
    candidate = proposal(scope)
    activity.record_event(
        scope,
        hook_kind,
        {
            "session_id": "example-session",
            "tool_input": {"command": "brigade work verify run --manifest lesson"},
            "tool_output": "run: failed\nreceipt: " + candidate["failure_evidence"],
        },
        now=99,
    )
    for _ in range(activity.MAX_EVENTS + 1):
        activity.record_event(scope, "PostToolUse", {"tool_output": "Read source."}, now=100)
    event(scope)
    state = activity.load(scope)
    assert any(e["id"] == 1 and e.get("verify_command") for e in state["events"])
    candidate["event"] = state["sequence"]
    # The final output cites only its own passing receipt.
    state["events"][-1]["text"] = candidate["quote"] + "\n" + candidate["evidence"]
    activity.save(scope, state)
    seat(monkeypatch, candidate)
    assert review.run(scope, now=111)["status"] == "committed"


@pytest.mark.parametrize("binding_mode", ["fixture_eval", "patch_backed"])
def test_real_signed_failure_and_pass_are_admitted(scope, monkeypatch, capsys, binding_mode):
    from tests.test_verify_trial import _track_workspace_manifest
    from brigade.work_cmd import verification

    candidate = proposal(scope)
    # Replace the hand-built evidence with actual verify-run receipts.
    manifest_path = scope / "verify/manifests/lesson.json"
    payload = json.loads(manifest_path.read_text())
    payload["checks"][0]["command"] = [sys.executable, "check.py"]
    card = scope / "memory/cards" / candidate["target_card"]
    card.parent.mkdir(parents=True)
    if binding_mode == "fixture_eval":
        payload["subject"]["content_fingerprint"] = "sha256:" + sha256_hex(candidate["content"].encode())
    else:
        payload.update(binding_mode="patch_backed", patch_source="worktree")
        payload.pop("fixture")
        card.write_bytes(b"Initial card.\n")
        subprocess.run(["git", "add", "memory"], cwd=scope, check=True)
        subprocess.run(
            ["git", "-c", "user.name=Test User", "-c", "user.email=test@example.invalid", "commit", "-m", "add card"],
            cwd=scope,
            check=True,
        )
        candidate["action"] = "update-card"
    verify_manifest.write_workspace_manifest(scope, payload)
    _track_workspace_manifest(scope, "lesson")
    (scope / "check.py").write_text("raise SystemExit(1)\n")
    (scope / ".brigade/work/current").unlink()
    assert work_cmd.start(target=scope, title="lesson fix") == 0
    monkeypatch.setenv("BRIGADE_CLAUDE_SESSION", localio.stable_hash({"claude_session_id": "example-session"}))
    monkeypatch.setenv("BRIGADE_VERIFY_SESSION_ID", "independent-verifier")
    monkeypatch.setattr(verification.code_graph_delta, "capture_before", lambda *_a, **_k: None)
    monkeypatch.setattr(
        verification.code_graph_delta, "capture_after_and_diff", lambda *_a, **_k: {"status": "disabled"}
    )
    capsys.readouterr()
    for expected in (1, 0):
        if binding_mode == "patch_backed":
            card.write_bytes(("Failing card.\n" if expected else candidate["content"]).encode())
        rc = work_cmd.verify_run(target=scope, manifest_id="lesson", reuse=False, json_output=True)
        assert rc == expected
        receipt = json.loads(capsys.readouterr().out)
        reference = str(
            (scope / ".brigade/work/verify-runs" / receipt["run_id"] / "receipt.json").relative_to(scope).as_posix()
        )
        candidate["failure_evidence" if expected else "evidence"] = reference
        activity.record_event(
            scope,
            "PostToolUseFailure" if expected else "PostToolUse",
            {
                "session_id": "example-session",
                "tool_input": {"command": "brigade work verify run --manifest lesson"},
                "tool_output": json.dumps(receipt),
            },
            now=100,
        )
        (scope / "check.py").write_text("print('ok')\n")
    activity.record_event(
        scope, "PostToolUse", {"session_id": "example-session", "tool_output": candidate["quote"]}, now=100
    )
    candidate["event"] = activity.load(scope)["sequence"]
    reads = {card.name: sha256_hex(card.read_bytes())} if binding_mode == "patch_backed" else {}
    admission.admit(scope, candidate, activity.load(scope)["events"], reads)
    seat(monkeypatch, candidate, reads=list(reads))
    result = review.run(scope, now=111)
    assert result["status"] == "committed", result
    assert len(list((scope / "memory/handoff-inbox").glob("lesson-*.md"))) == 1
    if binding_mode == "fixture_eval":
        assert not card.exists()
    else:
        assert card.read_text() == candidate["content"]
        # Historical failure validation must not relax passing live-content checks.
        card.write_text("Different current card.\n")
        with pytest.raises(ValueError, match="subject_hash_mismatch"):
            admission.verified_receipt(
                scope,
                candidate["evidence"],
                content=candidate["content"],
                card=card.name,
                events=activity.load(scope)["events"],
                earliest=100,
            )
        admission.verified_receipt(
            scope,
            candidate["failure_evidence"],
            content=None,
            card=card.name,
            events=activity.load(scope)["events"],
            earliest=100,
        )

        # An authentic historical receipt still has to bind its own post-image.
        path = scope / candidate["failure_evidence"]
        receipt = json.loads(path.read_text())
        receipt["subject_binding"]["patch_binding"]["subject_hash"] = "sha256:" + "0" * 64
        receipt["digests"]["receipt_sha256"] = localio.canonical_json_digest(receipt, exclude_keys={"digests"})
        receipt_signing.stamp_workspace_signature(scope, receipt)
        path.write_text(json.dumps(receipt))
        assert receipt_signing.authentic_workspace_receipt(scope, receipt)
        with pytest.raises(ValueError, match="subject_hash_mismatch"):
            admission.verified_receipt(
                scope,
                candidate["failure_evidence"],
                content=None,
                card=card.name,
                events=activity.load(scope)["events"],
                earliest=100,
            )


@pytest.mark.parametrize("output", ["Exit code 1\nrun: failed", '{"run_id": "failed"}'])
def test_failure_error_records_verify_run(scope, output):
    activity.record_event(
        scope,
        "PostToolUseFailure",
        {"tool_input": {"command": "brigade work verify run"}, "error": output},
        now=100,
    )
    assert activity.load(scope)["events"][-1]["verify_run_id"] == "failed"


def test_failure_error_refuses_ambiguous_runs(scope):
    activity.record_event(
        scope,
        "PostToolUseFailure",
        {"tool_input": {"command": "brigade work verify run"}, "error": "run: first\nrun: second"},
        now=100,
    )
    assert "verify_run_id" not in activity.load(scope)["events"][-1]
