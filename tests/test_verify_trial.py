"""Scoreable verify receipt binding and project_trial eligibility (#571)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from brigade import localio, verify_manifest, verify_trial, work_cmd
from brigade.verify_manifest import _WORKSPACE_MANIFESTS_REL
from brigade.work_cmd import verification as verify_mod

from tests.work_cmd_test_helpers import _init_git_repo


def _init_verify_git_repo(path: Path) -> None:
    _init_git_repo(path)
    (path / ".gitignore").write_text(".brigade/\n")
    subprocess.run(["git", "add", ".gitignore"], cwd=path, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(
        ["git", "-c", "user.name=Test User", "-c", "user.email=test@example.invalid", "commit", "-m", "init"],
        cwd=path,
        check=True,
        stdout=subprocess.DEVNULL,
    )


def _track_workspace_manifest(path: Path, manifest_id: str) -> None:
    rel = _WORKSPACE_MANIFESTS_REL / f"{manifest_id}.json"
    subprocess.run(["git", "add", str(rel)], cwd=path, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test User",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-m",
            f"add verify manifest {manifest_id}",
        ],
        cwd=path,
        check=True,
        stdout=subprocess.DEVNULL,
    )


def _write_skill_subject(
    path: Path, *, rel: str = "skills/brigade-work/SKILL.md", content: str = "# brigade-work\n"
) -> Path:
    skill_path = path / rel
    skill_path.parent.mkdir(parents=True, exist_ok=True)
    skill_path.write_text(content)
    return skill_path


def _stamp_receipt_digest(receipt: dict) -> None:
    receipt["digests"] = {
        "algorithm": "sha256",
        "logs": {},
        "receipt_sha256": localio.canonical_json_digest(receipt, exclude_keys={"digests"}),
    }


def _minimal_manifest_binding(
    *,
    manifest_id: str = "structural-fixture",
    payload_sha256: str = "sha256:" + "b" * 64,
) -> dict:
    return {
        "manifest_id": manifest_id,
        "payload_sha256": payload_sha256,
        "source_path": f"verify/manifests/{manifest_id}.json",
    }


def _structural_fixture_binding(**overrides) -> dict:
    binding = {
        "binding_mode": "fixture_eval",
        "artifact_kind": "skill",
        "artifact_id": "brigade-work",
        "content_fingerprint": "sha256:" + "a" * 64,
        "fixture_binding": {
            "manifest_id": "adversarial-failed-verification",
            "case_id": "report-failing-tests",
            "check_id": "fixture.echo-ok",
        },
        "manifest_binding": _minimal_manifest_binding(),
        "verifier_identity": {"verifier_id": "test", "session_id": "verifier-1"},
    }
    binding.update(overrides)
    return binding


def _write_patch_manifest(path: Path, *, manifest_id: str = "test-skill-patch") -> dict:
    payload = {
        "schema": "brigade.verify_manifest.v1",
        "schema_version": 1,
        "manifest_id": manifest_id,
        "binding_mode": "patch_backed",
        "verifier_id": "brigade.verify.test",
        "patch_source": "worktree",
        "subject": {
            "artifact_kind": "skill",
            "artifact_id": "brigade-work",
            "subject_path": "skills/brigade-work/SKILL.md",
        },
        "checks": [
            {
                "check_id": "verify.echo-ok",
                "check_role": "effectiveness",
                "command": f"{sys.executable} -c \"print('ok')\"",
            }
        ],
    }
    verify_manifest.write_workspace_manifest(path, payload)
    _track_workspace_manifest(path, manifest_id)
    return payload


def _write_fixture_manifest(path: Path, *, manifest_id: str = "test-skill-fixture") -> dict:
    payload = {
        "schema": "brigade.verify_manifest.v1",
        "schema_version": 1,
        "manifest_id": manifest_id,
        "binding_mode": "fixture_eval",
        "verifier_id": "brigade.verify.fixture",
        "subject": {
            "artifact_kind": "skill",
            "artifact_id": "brigade-work",
            "subject_path": "skills/brigade-work/SKILL.md",
        },
        "fixture": {
            "manifest_id": "adversarial-failed-verification",
            "case_id": "report-failing-tests",
            "check_id": "fixture.echo-ok",
        },
        "checks": [
            {
                "check_id": "fixture.echo-ok",
                "check_role": "effectiveness",
                "command": f"{sys.executable} -c \"print('ok')\"",
            }
        ],
    }
    verify_manifest.write_workspace_manifest(path, payload)
    _track_workspace_manifest(path, manifest_id)
    return payload


def _run_manifest_verify(
    tmp_path: Path,
    manifest_id: str,
    *,
    monkeypatch: pytest.MonkeyPatch,
    start_session: bool = True,
) -> dict:
    monkeypatch.setattr(verify_mod.graphtrail_delta, "capture_before", lambda *_a, **_k: None)
    monkeypatch.setattr(
        verify_mod.graphtrail_delta,
        "capture_after_and_diff",
        lambda *_a, **_k: verify_mod.graphtrail_delta._status("disabled", "graph disabled in test"),
    )
    if start_session:
        assert work_cmd.start(target=tmp_path, title="verify-trial") == 0
    import io
    from contextlib import redirect_stdout

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        rc = verify_mod.verify_run(
            target=tmp_path,
            manifest_id=manifest_id,
            reuse=False,
            json_output=True,
        )
    assert rc == 0, buffer.getvalue()
    return json.loads(buffer.getvalue())


@pytest.fixture
def scoreable_target(tmp_path):
    target = tmp_path / "ws"
    target.mkdir()
    _init_verify_git_repo(target)
    _write_skill_subject(target)
    subprocess.run(["git", "add", "skills"], cwd=target, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(
        ["git", "-c", "user.name=Test User", "-c", "user.email=test@example.invalid", "commit", "-m", "add skill"],
        cwd=target,
        check=True,
        stdout=subprocess.DEVNULL,
    )
    return target


def test_registered_manifest_is_discoverable(scoreable_target):
    _write_patch_manifest(scoreable_target, manifest_id="local-patch")
    assert "local-patch" in verify_manifest.registered_manifest_ids(scoreable_target)


def test_resolve_manifest_rejects_untracked_workspace_manifest(scoreable_target):
    payload = {
        "schema": "brigade.verify_manifest.v1",
        "schema_version": 1,
        "manifest_id": "untracked-patch",
        "binding_mode": "patch_backed",
        "verifier_id": "brigade.verify.test",
        "patch_source": "worktree",
        "subject": {
            "artifact_kind": "skill",
            "artifact_id": "brigade-work",
            "subject_path": "skills/brigade-work/SKILL.md",
        },
        "checks": [
            {
                "check_id": "verify.echo-ok",
                "check_role": "effectiveness",
                "command": f"{sys.executable} -c \"print('ok')\"",
            }
        ],
    }
    verify_manifest.write_workspace_manifest(scoreable_target, payload)
    manifest_path = scoreable_target / _WORKSPACE_MANIFESTS_REL / "untracked-patch.json"
    assert manifest_path.is_file()

    manifest, error = verify_manifest.resolve_manifest(scoreable_target, "untracked-patch")
    assert manifest is None
    assert error == "verify manifest not tracked: untracked-patch"
    assert "untracked-patch" not in verify_manifest.registered_manifest_ids(scoreable_target)


def test_adhoc_verify_receipt_is_unattributed(scoreable_target, monkeypatch):
    monkeypatch.setattr(verify_mod.graphtrail_delta, "capture_before", lambda *_a, **_k: None)
    monkeypatch.setattr(
        verify_mod.graphtrail_delta,
        "capture_after_and_diff",
        lambda *_a, **_k: verify_mod.graphtrail_delta._status("disabled", "graph disabled in test"),
    )
    import io
    from contextlib import redirect_stdout

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        rc = verify_mod.verify_run(
            target=scoreable_target,
            commands=[f"{sys.executable} -c \"print('ok')\""],
            reuse=False,
            json_output=True,
        )
    assert rc == 0
    receipt = json.loads(buffer.getvalue())
    assert "subject_binding" not in receipt
    assert all("check_role" not in command for command in receipt["commands"])
    projection = verify_trial.project_trial(receipt, target=scoreable_target)
    assert projection.eligible is False
    assert projection.reason == "unattributed"
    assert projection.attributed is False


def test_manifest_verify_writes_subject_binding_and_check_roles(scoreable_target, monkeypatch):
    _write_patch_manifest(scoreable_target)
    receipt = _run_manifest_verify(scoreable_target, "test-skill-patch", monkeypatch=monkeypatch)
    assert receipt["verify_manifest_id"] == "test-skill-patch"
    binding = receipt["subject_binding"]
    assert binding["binding_mode"] == "patch_backed"
    assert binding["manifest_binding"]["manifest_id"] == "test-skill-patch"
    assert binding["artifact_id"] == "brigade-work"
    assert binding["patch_binding"]["subject_path"] == "skills/brigade-work/SKILL.md"
    assert receipt["commands"][0]["check_role"] == "effectiveness"
    assert receipt["commands"][0]["check_id"] == "verify.echo-ok"
    projection = verify_trial.project_trial(receipt, target=scoreable_target)
    assert projection.eligible is False
    assert projection.reason == "empty_patch"


def test_manifest_verify_rejects_cli_command_override(scoreable_target):
    from brigade.cli.work import dispatching

    class Parser:
        @staticmethod
        def error(msg: str) -> None:
            raise SystemExit(msg)

    class Args:
        work_command = "verify"
        verify_command = "run"
        target = scoreable_target
        verify_commands = ["true"]
        verify_argv_json = None
        verify_manifest_id = "test-skill-patch"
        timeout = 900
        graphtrail_timeout = None
        json = False
        capture = None
        capture_kind = "skill"
        no_reuse = True
        _brigade_parser = Parser()

    _write_patch_manifest(scoreable_target)
    with pytest.raises(SystemExit, match="mutually exclusive"):
        dispatching.dispatch(Args())


def test_project_trial_empty_patch_is_ineligible(scoreable_target, monkeypatch):
    _write_patch_manifest(scoreable_target)
    receipt = _run_manifest_verify(scoreable_target, "test-skill-patch", monkeypatch=monkeypatch)
    projection = verify_trial.project_trial(receipt, target=scoreable_target)
    assert projection.eligible is False
    assert projection.reason == "empty_patch"


def test_project_trial_patch_digest_mismatch_is_ineligible(scoreable_target, monkeypatch):
    _write_patch_manifest(scoreable_target)
    receipt = _run_manifest_verify(scoreable_target, "test-skill-patch", monkeypatch=monkeypatch)
    run_dir = Path(receipt["path"])
    patch_path = run_dir / "changes.patch"
    patch_path.write_bytes(b"diff --git a/x b/x\n")
    projection = verify_trial.project_trial(receipt, target=scoreable_target, patch_path=patch_path)
    assert projection.eligible is False
    assert projection.reason == "patch_digest_mismatch"


def test_project_trial_no_owned_work_without_subject_delta(scoreable_target, monkeypatch):
    _write_patch_manifest(scoreable_target)
    assert work_cmd.start(target=scoreable_target, title="verify-trial") == 0
    readme = scoreable_target / "README.md"
    readme.write_text("changed\n")
    subprocess.run(["git", "add", "README.md"], cwd=scoreable_target, check=True)
    receipt = _run_manifest_verify(scoreable_target, "test-skill-patch", monkeypatch=monkeypatch, start_session=False)
    projection = verify_trial.project_trial(receipt, target=scoreable_target)
    assert projection.eligible is False
    assert projection.reason == "no_owned_work"


def test_project_trial_no_session_dirty_work_is_ineligible(scoreable_target, monkeypatch):
    _write_patch_manifest(scoreable_target)
    skill = _write_skill_subject(scoreable_target, content="# brigade-work\npatched\n")
    subprocess.run(["git", "add", str(skill.relative_to(scoreable_target))], cwd=scoreable_target, check=True)
    receipt = _run_manifest_verify(scoreable_target, "test-skill-patch", monkeypatch=monkeypatch, start_session=False)
    projection = verify_trial.project_trial(receipt, target=scoreable_target)
    assert projection.eligible is False
    assert projection.reason == "producer_session_missing"


def test_project_trial_pre_existing_dirty_subject_is_ineligible(scoreable_target, monkeypatch):
    _write_patch_manifest(scoreable_target)
    skill = _write_skill_subject(scoreable_target, content="# brigade-work\npre-existing\n")
    subprocess.run(["git", "add", str(skill.relative_to(scoreable_target))], cwd=scoreable_target, check=True)
    assert work_cmd.start(target=scoreable_target, title="verify-trial") == 0
    receipt = _run_manifest_verify(scoreable_target, "test-skill-patch", monkeypatch=monkeypatch, start_session=False)
    projection = verify_trial.project_trial(receipt, target=scoreable_target)
    assert projection.eligible is False
    assert projection.reason == "subject_pre_existing_dirty"


def test_project_trial_concurrent_session_ownership_is_ineligible(scoreable_target, monkeypatch):
    _write_patch_manifest(scoreable_target)
    skill = _write_skill_subject(scoreable_target, content="# brigade-work\npre-existing\n")
    subprocess.run(["git", "add", str(skill.relative_to(scoreable_target))], cwd=scoreable_target, check=True)
    assert work_cmd.start(target=scoreable_target, title="producer") == 0
    receipt = _run_manifest_verify(scoreable_target, "test-skill-patch", monkeypatch=monkeypatch, start_session=False)
    producer = receipt["subject_binding"]["producer_binding"]
    producer["subject_clean_at_start"] = True
    projection = verify_trial.project_trial(receipt, target=scoreable_target)
    assert projection.eligible is False
    assert projection.reason == "concurrent_session_ownership"


def test_project_trial_owned_patch_is_eligible(scoreable_target, monkeypatch):
    _write_patch_manifest(scoreable_target)
    assert work_cmd.start(target=scoreable_target, title="verify-trial") == 0
    skill = _write_skill_subject(scoreable_target, content="# brigade-work\npatched\n")
    subprocess.run(["git", "add", str(skill.relative_to(scoreable_target))], cwd=scoreable_target, check=True)
    receipt = _run_manifest_verify(scoreable_target, "test-skill-patch", monkeypatch=monkeypatch, start_session=False)
    projection = verify_trial.project_trial(receipt, target=scoreable_target)
    assert projection.eligible is True


def test_project_trial_fixture_eval_without_patch(scoreable_target, monkeypatch):
    _write_fixture_manifest(scoreable_target)
    receipt = _run_manifest_verify(scoreable_target, "test-skill-fixture", monkeypatch=monkeypatch, start_session=False)
    binding = receipt["subject_binding"]
    assert binding["binding_mode"] == "fixture_eval"
    assert binding["fixture_binding"]["case_id"] == "report-failing-tests"
    projection = verify_trial.project_trial(receipt, target=scoreable_target)
    assert projection.eligible is True


def test_project_trial_generated_patch_requires_independent_verifier(scoreable_target, monkeypatch):
    payload = _write_patch_manifest(scoreable_target, manifest_id="generated-patch")
    payload["patch_source"] = "generated"
    verify_manifest.write_workspace_manifest(scoreable_target, payload)
    _track_workspace_manifest(scoreable_target, "generated-patch")
    assert work_cmd.start(target=scoreable_target, title="producer") == 0
    skill = _write_skill_subject(scoreable_target, content="# brigade-work\ngenerated\n")
    subprocess.run(["git", "add", str(skill.relative_to(scoreable_target))], cwd=scoreable_target, check=True)
    monkeypatch.setenv("BRIGADE_CLAUDE_SESSION", "aaaaaaaaaaaaaaaa")
    receipt = _run_manifest_verify(
        scoreable_target,
        "generated-patch",
        monkeypatch=monkeypatch,
        start_session=False,
    )
    producer_session = receipt["subject_binding"]["producer_binding"]["work_session_id"]
    receipt["subject_binding"]["verifier_identity"]["session_id"] = producer_session
    projection = verify_trial.project_trial(receipt, target=scoreable_target)
    assert projection.eligible is False
    assert projection.reason == "verifier_not_independent"
    receipt["subject_binding"]["verifier_identity"]["session_id"] = f"{producer_session}-verifier"
    _stamp_receipt_digest(receipt)
    projection = verify_trial.project_trial(receipt, target=scoreable_target)
    assert projection.eligible is True


def test_project_trial_infrastructure_failure_is_ineligible(scoreable_target):
    receipt = {
        "status": "failed",
        "failure_class": "infrastructure",
        "failure_kind": "timeout",
        "commands": [
            {
                "status": "timed_out",
                "exit_code": None,
                "check_role": "effectiveness",
                "check_id": "verify.echo-ok",
                "failure_class": "infrastructure",
                "failure_kind": "timeout",
            }
        ],
        "subject_binding": _structural_fixture_binding(),
    }
    projection = verify_trial.project_trial(receipt)
    assert projection.eligible is False
    assert projection.reason == "failure_taxonomy_infrastructure"
    assert projection.infrastructure_excluded is True


def test_project_trial_missing_taxonomy_on_failure_is_ineligible(scoreable_target):
    receipt = {
        "status": "failed",
        "commands": [
            {
                "status": "failed",
                "exit_code": 1,
                "check_role": "effectiveness",
                "check_id": "verify.echo-ok",
            }
        ],
        "subject_binding": _structural_fixture_binding(),
    }
    projection = verify_trial.project_trial(receipt)
    assert projection.eligible is False
    assert projection.reason == "failure_taxonomy_missing"


def test_project_trial_skill_failure_with_taxonomy_is_eligible(scoreable_target):
    receipt = {
        "status": "failed",
        "failure_class": "verification",
        "failure_kind": "receipt_failed",
        "commands": [
            {
                "status": "failed",
                "exit_code": 1,
                "check_role": "effectiveness",
                "check_id": "verify.echo-ok",
                "failure_class": "verification",
                "failure_kind": "nonzero_exit",
            }
        ],
        "subject_binding": _structural_fixture_binding(),
    }
    _stamp_receipt_digest(receipt)
    projection = verify_trial.project_trial(receipt)
    assert projection.eligible is True
    assert projection.reason == "eligible"


def test_project_trial_missing_receipt_digest_is_ineligible(scoreable_target):
    receipt = {
        "status": "completed",
        "commands": [
            {
                "status": "completed",
                "exit_code": 0,
                "check_role": "effectiveness",
                "check_id": "verify.echo-ok",
            }
        ],
        "subject_binding": _structural_fixture_binding(),
    }
    projection = verify_trial.project_trial(receipt)
    assert projection.eligible is False
    assert projection.reason == "receipt_digest_missing"
    assert projection.attributed is True


def test_project_trial_receipt_digest_mismatch_is_ineligible(scoreable_target):
    receipt = {
        "status": "completed",
        "commands": [
            {
                "status": "completed",
                "exit_code": 0,
                "check_role": "effectiveness",
                "check_id": "verify.echo-ok",
            }
        ],
        "subject_binding": _structural_fixture_binding(),
        "digests": {
            "algorithm": "sha256",
            "logs": {},
            "receipt_sha256": "b" * 64,
        },
    }
    projection = verify_trial.project_trial(receipt)
    assert projection.eligible is False
    assert projection.reason == "receipt_digest_mismatch"
    assert projection.attributed is True


def test_project_trial_missing_check_role_is_ineligible(scoreable_target):
    receipt = {
        "status": "completed",
        "commands": [{"status": "completed", "exit_code": 0, "check_id": "verify.echo-ok"}],
        "subject_binding": _structural_fixture_binding(),
    }
    projection = verify_trial.project_trial(receipt)
    assert projection.eligible is False
    assert projection.reason == "missing_check_role"


def test_verify_finalize_stamps_failure_taxonomy(tmp_path):
    receipt = {
        "schema_version": 2,
        "run_id": "test",
        "target": str(tmp_path),
        "status": "failed",
        "started_at": "2026-01-01T00:00:00+00:00",
        "path": str(tmp_path),
        "commands": [{"command": "false", "status": "failed", "exit_code": 1}],
    }
    verify_trial.stamp_verify_receipt_failure_taxonomy(receipt)
    assert receipt["commands"][0]["failure_class"] == "verification"
    assert receipt["failure_class"] == "verification"


def test_manifest_required_utility_check_ids_must_reference_guardrail_checks(scoreable_target):
    with pytest.raises(ValueError, match="utility_guardrail"):
        verify_manifest.write_workspace_manifest(
            scoreable_target,
            {
                "schema": "brigade.verify_manifest.v1",
                "schema_version": 1,
                "manifest_id": "bad-utility",
                "binding_mode": "fixture_eval",
                "verifier_id": "brigade.verify.test",
                "subject": {
                    "artifact_kind": "skill",
                    "artifact_id": "brigade-work",
                    "subject_path": "skills/brigade-work/SKILL.md",
                },
                "fixture": {
                    "manifest_id": "adversarial-failed-verification",
                    "case_id": "report-failing-tests",
                    "check_id": "fixture.echo-ok",
                },
                "required_utility_check_ids": ["guardrail.missing"],
                "checks": [
                    {
                        "check_id": "fixture.echo-ok",
                        "check_role": "effectiveness",
                        "command": f"{sys.executable} -c \"print('ok')\"",
                    }
                ],
            },
        )


def test_project_trial_missing_required_utility_check_is_ineligible(scoreable_target):
    receipt = {
        "status": "completed",
        "required_utility_check_ids": ["guardrail.tests-green"],
        "commands": [
            {
                "status": "completed",
                "exit_code": 0,
                "check_role": "effectiveness",
                "check_id": "verify.echo-ok",
            }
        ],
        "subject_binding": _structural_fixture_binding(),
    }
    projection = verify_trial.project_trial(receipt)
    assert projection.eligible is False
    assert projection.reason == "missing_required_utility_check"


def test_manifest_verify_stamps_required_utility_check_ids(scoreable_target, monkeypatch):
    payload = _write_patch_manifest(scoreable_target, manifest_id="utility-manifest")
    payload["checks"].append(
        {
            "check_id": "guardrail.tests-green",
            "check_role": "utility_guardrail",
            "command": f"{sys.executable} -c \"print('ok')\"",
        }
    )
    payload["required_utility_check_ids"] = ["guardrail.tests-green"]
    verify_manifest.write_workspace_manifest(scoreable_target, payload)
    _track_workspace_manifest(scoreable_target, "utility-manifest")
    assert work_cmd.start(target=scoreable_target, title="verify-trial") == 0
    skill = _write_skill_subject(scoreable_target, content="# brigade-work\nutility\n")
    subprocess.run(["git", "add", str(skill.relative_to(scoreable_target))], cwd=scoreable_target, check=True)
    receipt = _run_manifest_verify(scoreable_target, "utility-manifest", monkeypatch=monkeypatch, start_session=False)
    assert receipt["required_utility_check_ids"] == ["guardrail.tests-green"]
    assert receipt["commands"][1]["check_role"] == "utility_guardrail"
    projection = verify_trial.project_trial(receipt, target=scoreable_target)
    assert projection.eligible is True


def test_project_trial_missing_manifest_binding_is_ineligible(scoreable_target):
    receipt = {
        "status": "completed",
        "commands": [
            {
                "status": "completed",
                "exit_code": 0,
                "check_role": "effectiveness",
                "check_id": "verify.echo-ok",
            }
        ],
        "subject_binding": {
            "binding_mode": "fixture_eval",
            "artifact_kind": "skill",
            "artifact_id": "brigade-work",
            "content_fingerprint": "sha256:" + "a" * 64,
            "fixture_binding": {
                "manifest_id": "adversarial-failed-verification",
                "case_id": "report-failing-tests",
                "check_id": "fixture.echo-ok",
            },
            "verifier_identity": {"verifier_id": "test", "session_id": "verifier-1"},
        },
    }
    _stamp_receipt_digest(receipt)
    projection = verify_trial.project_trial(receipt)
    assert projection.eligible is False
    assert projection.reason == "verifier_manifest_missing"


def test_project_trial_missing_verifier_identity_is_ineligible(scoreable_target):
    binding = _structural_fixture_binding()
    binding.pop("verifier_identity")
    receipt = {
        "status": "completed",
        "commands": [
            {
                "status": "completed",
                "exit_code": 0,
                "check_role": "effectiveness",
                "check_id": "verify.echo-ok",
            }
        ],
        "subject_binding": binding,
    }
    _stamp_receipt_digest(receipt)
    projection = verify_trial.project_trial(receipt)
    assert projection.eligible is False
    assert projection.reason == "verifier_identity_missing"


def test_project_trial_forged_manifest_binding_mismatch_is_ineligible(scoreable_target, monkeypatch):
    _write_fixture_manifest(scoreable_target)
    receipt = _run_manifest_verify(scoreable_target, "test-skill-fixture", monkeypatch=monkeypatch, start_session=False)
    receipt["subject_binding"]["manifest_binding"]["payload_sha256"] = "sha256:" + "f" * 64
    _stamp_receipt_digest(receipt)
    projection = verify_trial.project_trial(receipt, target=scoreable_target)
    assert projection.eligible is False
    assert projection.reason == "verifier_manifest_mismatch"


def test_project_trial_forged_subject_artifact_id_is_ineligible(scoreable_target, monkeypatch):
    _write_fixture_manifest(scoreable_target)
    receipt = _run_manifest_verify(scoreable_target, "test-skill-fixture", monkeypatch=monkeypatch, start_session=False)
    receipt["subject_binding"]["artifact_id"] = "forged-skill"
    _stamp_receipt_digest(receipt)
    projection = verify_trial.project_trial(receipt, target=scoreable_target)
    assert projection.eligible is False
    assert projection.reason == "verifier_manifest_mismatch"


def test_project_trial_forged_check_plan_is_ineligible(scoreable_target, monkeypatch):
    _write_fixture_manifest(scoreable_target)
    receipt = _run_manifest_verify(scoreable_target, "test-skill-fixture", monkeypatch=monkeypatch, start_session=False)
    receipt["commands"][0]["check_id"] = "forged.check"
    _stamp_receipt_digest(receipt)
    projection = verify_trial.project_trial(receipt, target=scoreable_target)
    assert projection.eligible is False
    assert projection.reason == "verifier_manifest_mismatch"


def test_project_trial_copied_binding_without_tracked_manifest_is_ineligible(scoreable_target, monkeypatch):
    _write_fixture_manifest(scoreable_target)
    receipt = _run_manifest_verify(scoreable_target, "test-skill-fixture", monkeypatch=monkeypatch, start_session=False)
    forged = dict(receipt)
    forged.pop("verify_manifest_id", None)
    forged["subject_binding"] = dict(receipt["subject_binding"])
    _stamp_receipt_digest(forged)
    projection = verify_trial.project_trial(forged, target=scoreable_target)
    assert projection.eligible is False
    assert projection.reason == "verifier_manifest_missing"
