"""Owning boundary: actual receipts and recorded tool protocol, never model booleans."""

import json
from pathlib import Path

import pytest

from brigade import model_trials

FIXTURE = Path(__file__).parents[1] / "src/brigade/fixtures/paired-brigade-work/manifest.json"


def test_paired_identity_and_snapshot(tmp_path):
    plan = model_trials.paired_plan(FIXTURE)
    assert len(plan["cells"]) == 10
    assert [c["arm"] for c in plan["cells"][:4]] == ["baseline", "candidate", "candidate", "baseline"]
    manifest = json.loads(FIXTURE.read_text())
    manifest["settings"]["reasoning"] = "changed"
    changed = tmp_path / "manifest.json"
    for arm in ("baseline", "candidate"):
        manifest[arm] = str(FIXTURE.parent / arm)
    changed.write_text(json.dumps(manifest))
    assert model_trials.paired_plan(changed)["cells"][0]["cell_id"] != plan["cells"][0]["cell_id"]


def trusted(callback):
    callback.paired_adapter_identity = {"id": "test.host-protocol.v1"}
    return callback


@trusted
def protocol(prompt, call):
    call("brief", {})
    call("verify", {})
    receipt = call("receipt", {})["receipt"]
    call("scoreability", {})
    call("capture", {})
    return {"run_id": receipt["run_id"], "status": receipt["status"], "eligible": False}


def test_grader_requires_successful_tools_and_exact_command_outcomes(tmp_path):
    import copy
    from brigade.model_trial_paired import grade

    result = model_trials.paired_execute(FIXTURE, tmp_path, protocol, cases=["failing-verify", "command-timeout"])
    attempts = {a["cell"]["case"]: a for a in result["attempts"]}
    for case, change in [
        ("failing-verify", "brief-exit"),
        ("failing-verify", "verify-exit"),
        ("failing-verify", "wrong-failure"),
        ("command-timeout", "not-timeout"),
    ]:
        attempt = copy.deepcopy(attempts[case])
        if change in {"brief-exit", "verify-exit"}:
            tool = "brief" if change == "brief-exit" else "verify"
            row = next(r for r in attempt["trace"] if r["tool"] == tool)
            row["response"]["exit_code"] = 2 if tool == "brief" else 0
        else:
            command = attempt["receipt"]["commands"][0]
            command.update(status="failed", exit_code=9)
            next(r for r in attempt["trace"] if r["tool"] == "receipt")["response"]["receipt"] = attempt["receipt"]
        assert grade(attempt)["passed"] is False, change


def test_real_pair_receipts_tamper_and_forged_success(tmp_path):
    result = model_trials.paired_execute(FIXTURE, tmp_path, protocol, cases=["failing-verify"])
    assert result["pairs"][0]["comparison"] == "both_pass"
    assert result["live"] is False
    assert result["promotion_eligible"] is False
    assert result["consistency_validated"] is True
    evidence = tmp_path / "evidence.json"
    payload = json.loads(evidence.read_text())
    cell = payload["attempts"][0]
    assert cell["receipt"]["status"] == "failed"
    assert cell["grade"]["passed"] is True
    cell["final"]["status"] = "completed"
    evidence.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="digest"):
        model_trials.paired_validate(tmp_path, result["evidence_sha256"])
    forged = model_trials.paired_execute(FIXTURE, tmp_path / "forged", trusted(lambda p, c: {"status": "completed"}))
    assert all(not a["grade"]["passed"] for a in forged["attempts"])


def test_unsupported_calls_incomplete_retry_and_timeout(tmp_path):
    @trusted
    def interrupted(prompt, call):
        assert call("shell", {"command": "touch escaped"})["error"] == "unsupported tool or arguments"
        raise TimeoutError("model deadline")

    first = model_trials.paired_execute(FIXTURE, tmp_path, interrupted, cases=["passing-verify"])
    assert first["pairs"][0]["comparison"] == "incomplete"
    assert first["attempts"][0]["failure"] == "model_timeout"
    second = model_trials.paired_execute(FIXTURE, tmp_path, protocol, cases=["passing-verify"])
    assert len(second["attempts"]) == 4
    assert second["attempts"][0] == first["attempts"][0]
    assert second["attempts"][-1]["grade"]["passed"]
    timeout = model_trials.paired_execute(FIXTURE, tmp_path / "timeout", protocol, cases=["command-timeout"])
    assert timeout["attempts"][0]["failure"] is None
    assert timeout["attempts"][0]["receipt"]["commands"][0]["status"] == "timed_out"
    assert timeout["attempts"][0]["grade"]["passed"]


def test_rpc_five_cases_and_independent_claim_checks(tmp_path):
    from brigade.model_trial_paired import Boundary, digest

    target = tmp_path / "rpc"
    target.mkdir()
    boundary = Boundary(target, "passing-verify", {("SKILL.md",): b"# fake skill"})
    rejected = boundary.rpc({"id": 1, "method": "item/tool/call", "params": {"tool": "shell", "arguments": {}}})
    assert rejected["result"]["success"] is False
    assert boundary.trace[-1]["tool"] == "shell"
    boundary.rpc({"id": 2, "method": "item/tool/call", "params": {"tool": "brief", "arguments": {}}})
    assert boundary.trace[-1]["response"]["exit_code"] == 0
    result = model_trials.paired_execute(FIXTURE, tmp_path / "all", protocol)
    assert len(result["pairs"]) == 5
    assert all(a["grade"]["passed"] for a in result["attempts"])
    assert len({a["receipt"]["path"] for a in result["attempts"]}) == 10
    payload = json.loads((tmp_path / "all/evidence.json").read_text())
    payload["attempts"][0]["final"]["eligible"] = True
    (tmp_path / "all/evidence.json").write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="trace grade"):
        model_trials.paired_validate(tmp_path / "all", digest(payload))


def test_resume_rejects_package_mutation_and_input_tampering(tmp_path):
    import shutil
    from brigade.model_trial_paired import digest

    fixture = tmp_path / "fixture"
    shutil.copytree(FIXTURE.parent, fixture)
    manifest = fixture / "manifest.json"
    output = tmp_path / "output"
    model_trials.paired_execute(manifest, output, protocol, cases=["audit-only"])
    candidate = fixture / "candidate/SKILL.md"
    candidate.write_text(candidate.read_text() + "\nChanged revision.\n")
    with pytest.raises(ValueError, match="identity changed"):
        model_trials.paired_execute(manifest, output, protocol, cases=["audit-only"])
    payload = json.loads((output / "evidence.json").read_text())
    payload["attempts"][0]["input"]["skill"] = "forged instructions"
    (output / "evidence.json").write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="input"):
        model_trials.paired_validate(output, digest(payload))


def test_candidate_regression_is_visible_and_receipt_snapshots_are_checked(tmp_path):
    from brigade.model_trial_paired import digest

    @trusted
    def regressed(prompt, call):
        final = protocol(prompt, call)
        if "Receipt-grounded reporting revision" in prompt["skill"]:
            final["eligible"] = True
        return final

    result = model_trials.paired_execute(FIXTURE, tmp_path, regressed, cases=["audit-only"])
    assert result["pairs"][0]["comparison"] == "baseline_only_pass"
    candidate = next(a for a in result["attempts"] if a["cell"]["arm"] == "candidate")
    assert candidate["grade"]["passed"] is False
    receipt = tmp_path / "attempts" / candidate["attempt_id"] / "receipt.json"
    receipt.write_text("{}")
    with pytest.raises(ValueError, match="receipt snapshot"):
        model_trials.paired_validate(tmp_path, digest(json.loads((tmp_path / "evidence.json").read_text())))


@pytest.mark.parametrize("planted", ["evidence.sha256", "evidence.json", "packages", "output", "ancestor"])
def test_paired_refuses_planted_output_symlinks(tmp_path, planted):
    victim = tmp_path / "victim"
    victim.mkdir()
    secret = victim / "secret"
    secret.write_bytes(b"preserve victim")
    output = tmp_path / "output"
    output.mkdir()
    if planted == "output":
        output.rmdir()
        output.symlink_to(victim, target_is_directory=True)
    elif planted == "ancestor":
        (tmp_path / "alias").symlink_to(victim, target_is_directory=True)
        output = tmp_path / "alias" / "child"
    else:
        (output / planted).symlink_to(
            victim if planted == "packages" else secret, target_is_directory=planted == "packages"
        )
    with pytest.raises((ValueError, OSError)):
        model_trials.paired_execute(FIXTURE, output, trusted(lambda p, c: {}), cases=["audit-only"])
    assert secret.read_bytes() == b"preserve victim"
    assert sorted(p.name for p in victim.iterdir()) == ["secret"]


def test_replay_identity_and_envelopes_are_bound(tmp_path):
    from brigade.model_trial_paired import replay_session

    script = json.loads((FIXTURE.parent / "replay.json").read_text())
    result = model_trials.paired_execute(FIXTURE, tmp_path, replay_session(script), cases=["audit-only"])
    assert result["model_executed"] is False
    assert result["consistency_validated"] is True
    row = result["attempts"][0]["trace"][0]
    assert row["request_envelope"] == script["audit-only"]["requests"][0]
    assert row["response_envelope"]["id"] == row["request_envelope"]["id"]
    script["audit-only"]["requests"][0]["params"]["threadId"] = "changed-thread"
    with pytest.raises(ValueError, match="identity changed"):
        model_trials.paired_execute(FIXTURE, tmp_path, replay_session(script), cases=["audit-only"])


def test_host_callback_requires_explicit_adapter_identity(tmp_path):
    with pytest.raises(ValueError, match="adapter identity"):
        model_trials.paired_execute(FIXTURE, tmp_path, lambda p, c: {})


def test_resume_refuses_planted_artifact_and_changed_host_adapter(tmp_path):
    from brigade.model_trial_paired import digest

    result = model_trials.paired_execute(FIXTURE, tmp_path, protocol, cases=["audit-only"])
    victim = tmp_path.parent / (tmp_path.name + "-victim")
    victim.write_bytes(b"preserve on resume")
    artifact = tmp_path / "attempts" / result["attempts"][0]["attempt_id"] / "receipt.json"
    original = artifact.read_bytes()
    artifact.unlink()
    artifact.symlink_to(victim)
    with pytest.raises((ValueError, OSError)):
        model_trials.paired_execute(FIXTURE, tmp_path, protocol, cases=["audit-only"])
    assert victim.read_bytes() == b"preserve on resume"
    artifact.unlink()
    artifact.write_bytes(original)

    def other(prompt, call):
        return protocol(prompt, call)

    other.paired_adapter_identity = {"id": "test.changed-host.v1"}
    with pytest.raises(ValueError, match="identity changed"):
        model_trials.paired_execute(FIXTURE, tmp_path, other, cases=["audit-only"])
    payload = json.loads((tmp_path / "evidence.json").read_text())
    payload["model_executed"] = True
    (tmp_path / "evidence.json").write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="offline"):
        model_trials.paired_validate(tmp_path, digest(payload))


def test_replay_response_envelope_cannot_disagree_with_trace(tmp_path):
    from brigade.model_trial_paired import replay_session, digest

    script = json.loads((FIXTURE.parent / "replay.json").read_text())
    model_trials.paired_execute(FIXTURE, tmp_path, replay_session(script), cases=["audit-only"])
    evidence = tmp_path / "evidence.json"
    payload = json.loads(evidence.read_text())
    payload["attempts"][0]["trace"][0]["response_envelope"]["id"] = "forged"
    evidence.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="response envelope"):
        model_trials.paired_validate(tmp_path, digest(payload))
