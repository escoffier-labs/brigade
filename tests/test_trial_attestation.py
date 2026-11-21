"""Local trial attestation labeling and the forged-session bound (#2779)."""

from __future__ import annotations

import io
import json
import subprocess
from contextlib import redirect_stdout
from pathlib import Path

from brigade import localio, outcome, outcome_cmd, verify_manifest, verify_trial, work_cmd

from tests.test_verify_trial import (
    _run_manifest_verify,
    _stamp_receipt_digest,
    _track_workspace_manifest,
    _write_skill_subject,
)

pytest_plugins = ["tests.test_verify_trial"]


def _write_portable_patch_manifest(target: Path) -> None:
    """Use a command with no path separators so both shlex modes normalise it the same way.

    The shared test helper embeds ``sys.executable`` in the check command. On Windows that
    path holds backslashes, which the verify runtime splits with ``posix=False`` while the
    eligibility projection splits with ``posix=True``. The two command plans then differ
    and the receipt reads as ``verifier_manifest_mismatch`` before attestation is reached.
    """
    payload = {
        "schema": "brigade.verify_manifest.v1",
        "schema_version": 1,
        "manifest_id": "test-skill-patch",
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
                "command": "git --version",
            }
        ],
    }
    verify_manifest.write_workspace_manifest(target, payload)
    _track_workspace_manifest(target, "test-skill-patch")


def _rank(target: Path, *, json_output: bool) -> tuple[dict, str]:
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        assert outcome_cmd.rank(target=target, json_output=json_output) == 0
    text = buffer.getvalue()
    payload = json.loads(text) if json_output else {}
    return payload, text


def test_forged_local_session_state_still_yields_a_local_trial(scoreable_target, monkeypatch):
    _write_portable_patch_manifest(scoreable_target)
    assert work_cmd.start(target=scoreable_target, title="producer") == 0
    skill = _write_skill_subject(scoreable_target, content="# brigade-work\npatched\n")
    subprocess.run(
        ["git", "add", str(skill.relative_to(scoreable_target))],
        cwd=scoreable_target,
        check=True,
        stdout=subprocess.DEVNULL,
    )
    receipt = _run_manifest_verify(scoreable_target, "test-skill-patch", monkeypatch=monkeypatch, start_session=False)
    producer = receipt["subject_binding"]["producer_binding"]
    session_id = producer["work_session_id"]

    # The scored agent can write this file on the same host. Forge every plausible
    # attestation claim while keeping the binding fields the producer check reads.
    session_path = scoreable_target / ".brigade" / "work" / session_id / "session.json"
    session = json.loads(session_path.read_text())
    session["attestation"] = "attested"
    session["attested"] = True
    session["ownership_proof"] = "hub-held"
    session["independent_verifier"] = "verifier-seat"
    session["start"]["attestation"] = "attested"
    session_path.write_bytes(json.dumps(session).encode("utf-8"))

    projection = verify_trial.project_trial(receipt, target=scoreable_target)
    assert projection.eligible is True, projection.reason
    assert projection.attestation == "local"

    # Persist the receipt so the scorecard and rank read the same forged trial.
    run_dir = scoreable_target / ".brigade" / "work" / "verify-runs" / "forged-session"
    run_dir.mkdir(parents=True, exist_ok=True)
    patch_bytes = (Path(receipt["path"]) / "changes.patch").read_bytes()
    (run_dir / "changes.patch").write_bytes(patch_bytes)
    receipt["run_id"] = "forged-session"
    receipt["path"] = str(run_dir)
    _stamp_receipt_digest(receipt)
    localio.write_json(run_dir / "receipt.json", receipt)

    # A legacy record makes the artifact a ranked entry, so the text path prints it.
    outcome_cmd.append_records(
        scoreable_target,
        [outcome.OutcomeRecord("brigade-work", "skill", "t-legacy", "verify", 1, "ref", "2026-06-20T00:00:00+00:00")],
    )

    payload, text = _rank(scoreable_target, json_output=True)
    entry = next(item for item in payload["ranking"] if item["artifact_id"] == "brigade-work")
    assert entry["local_trials"] == 1
    assert entry["attested_trials"] == 0

    _, text = _rank(scoreable_target, json_output=False)
    assert "local=1 attested=0" in text
