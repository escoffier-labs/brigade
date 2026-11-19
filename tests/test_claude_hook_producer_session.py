"""A hook-enabled Claude session can produce a manifest trial without ``work start`` (#2589)."""

from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from brigade import git_status_snapshot, localio, scorecard, verify_manifest, verify_trial, work_cmd
from brigade.agent_identity import claude_session_fingerprint, stamp_verify_identity
from brigade.claude_hooks import producer_session, runtime
from brigade.install import install_selection
from brigade.selection import Selection
from tests.test_verify_trial import (
    _init_verify_git_repo,
    _run_manifest_verify,
    _track_workspace_manifest,
    _write_patch_manifest,
    _write_skill_subject,
)

SESSION_ID = "11111111-2222-3333-4444-555555555555"
OTHER_SESSION_ID = "99999999-8888-7777-6666-555555555555"
MANIFEST_ID = "test-skill-patch"


# A manifest trial on Windows fails ``verifier_manifest_mismatch`` the same way the allowlisted
# tests/test_verify_trial.py does (#1482 baseline), so the trial-scoring cases here run on POSIX only.
posix_manifest_digest = pytest.mark.skipif(
    os.name == "nt", reason="manifest trial digest mismatches on Windows (same as test_verify_trial.py)"
)
windows_invalid_filename = pytest.mark.skipif(os.name == "nt", reason="Windows filenames cannot contain '>'")
posix_permissions = pytest.mark.skipif(os.name == "nt", reason="needs POSIX file permissions")
_SESSION_ENV = (
    "BRIGADE_CLAUDE_SESSION",
    "BRIGADE_AGENT_SESSION",
    "BRIGADE_RUN_ID",
    "BRIGADE_VERIFY_SESSION_ID",
    "CLAUDE_CODE_SESSION_ID",
)


def _fingerprint(session_id: str = SESSION_ID) -> str:
    return localio.stable_hash({"claude_session_id": session_id})


def _commit_all(path: Path, message: str) -> None:
    subprocess.run(["git", "add", "-A"], cwd=path, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(
        ["git", "-c", "user.name=Test User", "-c", "user.email=test@example.invalid", "commit", "-m", message],
        cwd=path,
        check=True,
        stdout=subprocess.DEVNULL,
    )


@pytest.fixture(autouse=True)
def _clean_session_env(monkeypatch):
    for name in _SESSION_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(runtime, "_run_brief", lambda _repo: "work brief: test")


@pytest.fixture
def hook_target(tmp_path: Path) -> Path:
    """A git workspace wired for Claude hooks with a tracked worktree-patch manifest and a clean subject."""
    target = tmp_path / "ws"
    target.mkdir()
    selection = Selection(depth="repo", harnesses=["claude"], owner="claude", includes=[])
    assert install_selection(target, selection) == 0
    _init_verify_git_repo(target)
    _write_skill_subject(target)
    _commit_all(target, "wire and add skill")
    _write_patch_manifest(target)
    return target


def _fake_git(monkeypatch, run) -> None:
    """Replace only this module's git call, so the hook's own git use is untouched."""
    monkeypatch.setattr(
        git_status_snapshot,
        "subprocess",
        SimpleNamespace(run=run, SubprocessError=subprocess.SubprocessError, DEVNULL=subprocess.DEVNULL),
    )


def _session_start(target: Path, session_id: str = SESSION_ID, source: str | None = None) -> dict:
    payload = {"session_id": session_id, "cwd": str(target), "hook_event_name": "SessionStart"}
    if source is not None:
        payload["source"] = source
    runtime.handle_payload("SessionStart", payload)
    state = runtime.read_session_state(target.resolve(), session_id)
    assert state is not None
    return state


def _edit_subject(target: Path, text: str = "patched") -> None:
    skill = _write_skill_subject(target, content=f"# brigade-work\n{text}\n")
    subprocess.run(["git", "add", str(skill.relative_to(target))], cwd=target, check=True)


def _hook_write(target: Path, session_id: str = SESSION_ID, text: str = "patched") -> None:
    """One Write on the subject as the hooks see it: PreToolUse records the check, then the edit, then PostToolUse."""
    subject = target / "skills" / "brigade-work" / "SKILL.md"
    event = {
        "session_id": session_id,
        "cwd": str(target),
        "tool_name": "Write",
        "tool_input": {"file_path": str(subject), "content": f"# brigade-work\n{text}\n"},
    }
    runtime.handle_payload("PreToolUse", {**event, "hook_event_name": "PreToolUse"})
    _edit_subject(target, text)
    runtime.handle_payload("PostToolUse", {**event, "hook_event_name": "PostToolUse"})


def _verify(target: Path, monkeypatch) -> dict:
    return _run_manifest_verify(target, MANIFEST_ID, monkeypatch=monkeypatch, start_session=False)


def test_hook_records_start_dirty_files_in_work_session_shape(hook_target: Path) -> None:
    (hook_target / "scratch.txt").write_text("pending\n")
    state = _session_start(hook_target)
    assert state["start"]["git"] == {"dirty_files": ["?? scratch.txt"], "status_format": "z1", "prefix": ""}


def test_start_snapshot_keeps_the_leading_status_column(hook_target: Path) -> None:
    skill = hook_target / "skills" / "brigade-work" / "SKILL.md"
    skill.write_text("# brigade-work\nunstaged\n")
    state = _session_start(hook_target)
    assert " M skills/brigade-work/SKILL.md" in state["start"]["git"]["dirty_files"]


def test_start_snapshot_survives_later_hook_events(hook_target: Path) -> None:
    first = _session_start(hook_target)
    write = {
        "session_id": SESSION_ID,
        "cwd": str(hook_target),
        "hook_event_name": "PostToolUse",
        "tool_name": "Write",
        "tool_input": {"file_path": str(hook_target / "skills" / "brigade-work" / "SKILL.md"), "content": "x\n"},
    }
    runtime.handle_payload("PostToolUse", write)
    runtime.handle_payload("SessionStart", {**write, "hook_event_name": "SessionStart"})
    later = runtime.read_session_state(hook_target.resolve(), SESSION_ID)
    assert later is not None
    assert later["write_observed"] is True
    assert later["start"] == first["start"]


def test_start_snapshot_is_taken_once_at_state_creation(hook_target: Path) -> None:
    first = _session_start(hook_target)
    (hook_target / "later.txt").write_text("after start\n")
    again = _session_start(hook_target)
    assert again["start"] == first["start"]
    assert "later.txt" not in json.dumps(again["start"])


def test_git_failure_records_nothing(hook_target: Path, monkeypatch) -> None:
    def boom(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("git", 2)

    _fake_git(monkeypatch, boom)
    state = _session_start(hook_target)
    assert "start" not in state


def test_oversized_snapshot_is_dropped_not_truncated(monkeypatch, tmp_path: Path) -> None:
    entries = b"".join(f"?? f{index}".encode() + b"\0" for index in range(producer_session.MAX_DIRTY_LINES + 1))
    _fake_git(monkeypatch, lambda *_a, **_k: subprocess.CompletedProcess([], 0, stdout=entries, stderr=b""))
    assert producer_session.snapshot_start(tmp_path) is None


def test_nonzero_git_exit_records_nothing(monkeypatch, tmp_path: Path) -> None:
    _fake_git(monkeypatch, lambda *_a, **_k: subprocess.CompletedProcess([], 128, stdout=b"", stderr=b"not a repo"))
    assert producer_session.snapshot_start(tmp_path) is None


def test_producer_id_cannot_collide_with_a_work_session_id() -> None:
    fingerprint = _fingerprint()
    pid = producer_session.producer_id(fingerprint)
    assert pid == f"claude-hook:{fingerprint}"
    assert producer_session.fingerprint_from_producer_id(pid) == fingerprint
    assert producer_session.fingerprint_from_producer_id("20260818-101010-some-task") is None
    assert producer_session.fingerprint_from_producer_id("claude-hook:../escape") is None
    assert producer_session.fingerprint_from_producer_id(None) is None


def test_same_session_matches_the_bare_fingerprint_and_nothing_else() -> None:
    fingerprint = _fingerprint()
    pid = producer_session.producer_id(fingerprint)
    assert producer_session.same_session(pid, fingerprint)
    assert producer_session.same_session(pid, pid)
    assert not producer_session.same_session(pid, _fingerprint(OTHER_SESSION_ID))
    assert not producer_session.same_session("20260818-101010-task", fingerprint)
    assert not producer_session.same_session(pid, "")
    assert not producer_session.same_session(pid, None)


def test_load_payload_resolves_state_without_the_environment(hook_target: Path) -> None:
    _session_start(hook_target)
    payload = verify_trial.load_work_session_payload(hook_target, producer_session.producer_id(_fingerprint()))
    state = runtime.read_session_state(hook_target.resolve(), SESSION_ID)
    assert payload == {"id": f"claude-hook:{_fingerprint()}", "start": state["start"]}
    assert payload["start"]["git"]["dirty_files"] == []


def test_load_payload_needs_exactly_one_matching_state(hook_target: Path) -> None:
    _session_start(hook_target)
    sessions = runtime._sessions_root(hook_target.resolve())
    original = next(sessions.glob("*.json"))
    (sessions / "duplicate.json").write_text(original.read_text())
    assert producer_session.load_payload(hook_target, producer_session.producer_id(_fingerprint())) is None


def test_load_payload_requires_a_recorded_start(hook_target: Path) -> None:
    _session_start(hook_target)
    sessions = runtime._sessions_root(hook_target.resolve())
    path = next(sessions.glob("*.json"))
    state = json.loads(path.read_text())
    state.pop("start")
    path.write_text(json.dumps(state))
    assert producer_session.load_payload(hook_target, producer_session.producer_id(_fingerprint())) is None


def test_load_payload_rejects_a_state_copied_from_another_target(hook_target: Path) -> None:
    _session_start(hook_target)
    sessions = runtime._sessions_root(hook_target.resolve())
    path = next(sessions.glob("*.json"))
    state = json.loads(path.read_text())
    state["target"] = str(hook_target.parent / "elsewhere")
    path.write_text(json.dumps(state))
    assert producer_session.load_payload(hook_target, producer_session.producer_id(_fingerprint())) is None


@posix_manifest_digest
def test_hook_session_trial_is_eligible_and_scores(hook_target: Path, monkeypatch) -> None:
    """The headline case: no work session, a hook session that edits the subject and verifies it."""
    _session_start(hook_target)
    _hook_write(hook_target)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION_ID)

    receipt = _verify(hook_target, monkeypatch)

    producer = receipt["subject_binding"]["producer_binding"]
    assert producer["work_session_id"] == f"claude-hook:{_fingerprint()}"
    assert producer["subject_clean_at_start"] is True
    projection = verify_trial.project_trial(receipt, target=hook_target)
    assert projection.reason == "eligible"
    assert projection.eligible is True
    assert [audit.eligible for audit in scorecard.audit_all_verify_receipts(hook_target)] == [True]


@posix_manifest_digest
def test_hook_printed_session_value_also_resolves_the_producer(hook_target: Path, monkeypatch) -> None:
    _session_start(hook_target)
    _hook_write(hook_target)
    monkeypatch.setenv("BRIGADE_CLAUDE_SESSION", _fingerprint())
    receipt = _verify(hook_target, monkeypatch)
    assert verify_trial.project_trial(receipt, target=hook_target).eligible is True


def test_subject_already_dirty_at_hook_start_is_ineligible(hook_target: Path, monkeypatch) -> None:
    _edit_subject(hook_target, "edited before the session began")
    _session_start(hook_target)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION_ID)
    receipt = _verify(hook_target, monkeypatch)
    projection = verify_trial.project_trial(receipt, target=hook_target)
    assert projection.eligible is False
    assert projection.reason == "subject_pre_existing_dirty"


def test_hook_session_without_a_start_snapshot_is_ineligible(hook_target: Path, monkeypatch) -> None:
    with monkeypatch.context() as patched:
        _fake_git(patched, lambda *_a, **_k: (_ for _ in ()).throw(OSError("no git")))
        assert "start" not in _session_start(hook_target)
    _edit_subject(hook_target)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION_ID)
    receipt = _verify(hook_target, monkeypatch)
    projection = verify_trial.project_trial(receipt, target=hook_target)
    assert projection.eligible is False
    assert projection.reason == "producer_session_missing"


def test_no_session_identity_stays_producer_session_missing(hook_target: Path, monkeypatch) -> None:
    _session_start(hook_target)
    _edit_subject(hook_target)
    receipt = _verify(hook_target, monkeypatch)
    projection = verify_trial.project_trial(receipt, target=hook_target)
    assert projection.eligible is False
    assert projection.reason == "producer_session_missing"


def test_ambiguous_ambient_identity_picks_no_session(hook_target: Path, monkeypatch) -> None:
    _session_start(hook_target)
    _edit_subject(hook_target)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION_ID)
    monkeypatch.setenv("BRIGADE_AGENT_SESSION", _fingerprint(OTHER_SESSION_ID))
    receipt = _verify(hook_target, monkeypatch)
    assert verify_trial.project_trial(receipt, target=hook_target).reason == "producer_session_missing"


def test_worker_inherited_ambient_identity_picks_no_session(hook_target: Path, monkeypatch) -> None:
    _session_start(hook_target)
    _edit_subject(hook_target)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION_ID)
    monkeypatch.setenv("BRIGADE_RUN_ID", "run-1")
    receipt = _verify(hook_target, monkeypatch)
    assert verify_trial.project_trial(receipt, target=hook_target).reason == "producer_session_missing"


def test_session_that_never_ran_a_hook_here_picks_no_session(hook_target: Path, monkeypatch) -> None:
    _session_start(hook_target, OTHER_SESSION_ID)
    _edit_subject(hook_target)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION_ID)
    receipt = _verify(hook_target, monkeypatch)
    assert verify_trial.project_trial(receipt, target=hook_target).reason == "producer_session_missing"


@posix_manifest_digest
def test_active_work_session_wins_over_the_hook_session(hook_target: Path, monkeypatch) -> None:
    _session_start(hook_target)
    assert work_cmd.start(target=hook_target, title="explicit") == 0
    _edit_subject(hook_target)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION_ID)
    receipt = _verify(hook_target, monkeypatch)
    session_id = receipt["subject_binding"]["producer_binding"]["work_session_id"]
    assert not session_id.startswith("claude-hook:")
    assert verify_trial.project_trial(receipt, target=hook_target).eligible is True


def test_typed_verify_is_stamped_as_its_own_sessions_receipt(hook_target: Path, monkeypatch) -> None:
    """F1 from #2759: the receipt, the verifier id and the Stop gate must agree on the session."""
    state = _session_start(hook_target)
    _edit_subject(hook_target)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION_ID)
    receipt = _verify(hook_target, monkeypatch)

    fingerprint = _fingerprint()
    assert claude_session_fingerprint() == fingerprint
    assert receipt["harness_session"] == {"harness": "claude", "fingerprint": fingerprint}
    assert receipt["subject_binding"]["verifier_identity"]["session_id"] == fingerprint
    assert verify_trial.verifier_session_id() == fingerprint
    assert runtime._session_has_completed_receipt(
        hook_target.resolve(), session_fingerprint=fingerprint, since=state["started_at"]
    )


def test_stamp_uses_the_hook_printed_value_alone(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BRIGADE_CLAUDE_SESSION", "aaaaaaaaaaaaaaaa")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION_ID)
    receipt: dict = {}
    stamp_verify_identity(receipt, tmp_path)
    assert receipt["harness_session"]["fingerprint"] == "aaaaaaaaaaaaaaaa"


def test_stamp_adds_no_session_when_ambient_values_disagree(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION_ID)
    monkeypatch.setenv("BRIGADE_AGENT_SESSION", _fingerprint(OTHER_SESSION_ID))
    receipt: dict = {}
    stamp_verify_identity(receipt, tmp_path)
    assert "harness_session" not in receipt
    assert verify_trial.verifier_session_id() is None


def test_explicit_verify_session_override_still_names_the_verifier(monkeypatch) -> None:
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION_ID)
    monkeypatch.setenv("BRIGADE_VERIFY_SESSION_ID", "separate-verifier")
    assert verify_trial.verifier_session_id() == "separate-verifier"


def test_generated_patch_verified_by_its_own_hook_session_is_not_independent(hook_target: Path, monkeypatch) -> None:
    """A same-session verify must never read as an independent verifier of a generated patch."""
    payload = _write_patch_manifest(hook_target, manifest_id="generated-patch")
    payload["patch_source"] = "generated"
    verify_manifest.write_workspace_manifest(hook_target, payload)
    _track_workspace_manifest(hook_target, "generated-patch")
    _session_start(hook_target)
    _hook_write(hook_target, text="generated")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION_ID)
    monkeypatch.setenv("BRIGADE_GENERATED_PATCH_CANDIDATE_COUNT", "3")
    monkeypatch.setenv("BRIGADE_GENERATED_PATCH_MODEL", "gpt-test")
    monkeypatch.setenv("BRIGADE_GENERATED_PATCH_MODEL_VERSION", "2024-01")

    receipt = _run_manifest_verify(hook_target, "generated-patch", monkeypatch=monkeypatch, start_session=False)

    producer = receipt["subject_binding"]["producer_binding"]["work_session_id"]
    assert producer == f"claude-hook:{_fingerprint()}"
    assert receipt["subject_binding"]["verifier_identity"]["session_id"] == _fingerprint()
    projection = verify_trial.project_trial(receipt, target=hook_target)
    assert projection.eligible is False
    assert projection.reason == "verifier_not_independent"


def test_bare_ambient_agent_session_names_no_producer(hook_target: Path, monkeypatch) -> None:
    """Only a Claude session id proves a Claude session, so one ambient value alone picks nothing."""
    _session_start(hook_target)
    _edit_subject(hook_target)
    monkeypatch.setenv("BRIGADE_AGENT_SESSION", _fingerprint())
    receipt = _verify(hook_target, monkeypatch)
    assert "harness_session" not in receipt
    assert verify_trial.project_trial(receipt, target=hook_target).reason == "producer_session_missing"


def _project(target: Path, receipt: dict) -> verify_trial.TrialProjection:
    return verify_trial.project_trial(receipt, target=target)


def test_other_sessions_edit_is_not_this_sessions_trial(hook_target: Path, monkeypatch) -> None:
    """F1: B starts clean, A edits the subject, B verifies. B's clean start snapshot must not make it B's trial."""
    _session_start(hook_target, SESSION_ID)
    _session_start(hook_target, OTHER_SESSION_ID)
    _hook_write(hook_target, OTHER_SESSION_ID, "edited by the other session")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION_ID)

    receipt = _verify(hook_target, monkeypatch)

    projection = _project(hook_target, receipt)
    assert projection.eligible is False
    assert projection.reason == "concurrent_session_ownership"


def test_both_sessions_writing_the_subject_is_concurrent(hook_target: Path, monkeypatch) -> None:
    _session_start(hook_target, SESSION_ID)
    _session_start(hook_target, OTHER_SESSION_ID)
    _hook_write(hook_target, OTHER_SESSION_ID, "first")
    _hook_write(hook_target, SESSION_ID, "second")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION_ID)
    projection = _project(hook_target, _verify(hook_target, monkeypatch))
    assert projection.eligible is False
    assert projection.reason == "concurrent_session_ownership"


def test_a_session_that_never_wrote_the_subject_has_no_owned_work(hook_target: Path, monkeypatch) -> None:
    """A change made only through Bash leaves no write record, so it fails closed."""
    _session_start(hook_target)
    _edit_subject(hook_target)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION_ID)
    projection = _project(hook_target, _verify(hook_target, monkeypatch))
    assert projection.eligible is False
    assert projection.reason == "no_owned_work"


@posix_manifest_digest
def test_a_foreign_write_before_this_session_started_does_not_veto(hook_target: Path, monkeypatch) -> None:
    _session_start(hook_target, OTHER_SESSION_ID)
    _hook_write(hook_target, OTHER_SESSION_ID, "landed earlier")
    _commit_all(hook_target, "the other session's change landed")
    _session_start(hook_target, SESSION_ID)
    _hook_write(hook_target, SESSION_ID, "this session's change")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION_ID)
    projection = _project(hook_target, _verify(hook_target, monkeypatch))
    assert projection.reason == "eligible"


@posix_manifest_digest
def test_a_foreign_write_to_another_file_does_not_veto(hook_target: Path, monkeypatch) -> None:
    _session_start(hook_target, SESSION_ID)
    _session_start(hook_target, OTHER_SESSION_ID)
    other = {
        "session_id": OTHER_SESSION_ID,
        "cwd": str(hook_target),
        "tool_name": "Write",
        "tool_input": {"file_path": str(hook_target / "notes.txt"), "content": "x\n"},
    }
    runtime.handle_payload("PreToolUse", {**other, "hook_event_name": "PreToolUse"})
    (hook_target / "notes.txt").write_text("x\n")
    runtime.handle_payload("PostToolUse", {**other, "hook_event_name": "PostToolUse"})
    _hook_write(hook_target, SESSION_ID)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION_ID)
    assert _project(hook_target, _verify(hook_target, monkeypatch)).reason == "eligible"


def test_a_foreign_write_with_no_recorded_path_is_treated_as_concurrent(hook_target: Path, monkeypatch) -> None:
    """Another session's Bash write names no path, so it cannot be ruled out as the subject."""
    _session_start(hook_target, SESSION_ID)
    _session_start(hook_target, OTHER_SESSION_ID)
    _hook_write(hook_target, SESSION_ID)
    sessions = runtime._sessions_root(hook_target.resolve())
    other_path = next(
        path for path in sessions.glob("*.json") if json.loads(path.read_text()).get("session_id") == OTHER_SESSION_ID
    )
    other = json.loads(other_path.read_text())
    other["write_observed"] = True
    other["last_write_at"] = localio.utc_now_iso()
    other_path.write_text(json.dumps(other))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION_ID)
    projection = _project(hook_target, _verify(hook_target, monkeypatch))
    assert projection.reason == "concurrent_session_ownership"


@posix_manifest_digest
def test_a_foreign_write_after_the_receipt_is_caught_at_projection_time(hook_target: Path, monkeypatch) -> None:
    _session_start(hook_target, SESSION_ID)
    _session_start(hook_target, OTHER_SESSION_ID)
    _hook_write(hook_target, SESSION_ID)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION_ID)
    receipt = _verify(hook_target, monkeypatch)
    assert _project(hook_target, receipt).reason == "eligible"
    _hook_write(hook_target, OTHER_SESSION_ID, "later foreign edit")
    assert _project(hook_target, receipt).reason == "concurrent_session_ownership"


def test_the_binding_carries_the_ownership_verdict_for_a_target_less_projection(hook_target: Path, monkeypatch) -> None:
    _session_start(hook_target, SESSION_ID)
    _session_start(hook_target, OTHER_SESSION_ID)
    _hook_write(hook_target, OTHER_SESSION_ID)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION_ID)
    receipt = _verify(hook_target, monkeypatch)
    producer = receipt["subject_binding"]["producer_binding"]
    assert producer["hook_ownership"] == "concurrent_session_ownership"
    assert verify_trial.project_trial(receipt, target=None).reason != "eligible"


def test_a_clean_hook_trial_records_ownership_in_the_binding(hook_target: Path, monkeypatch) -> None:
    _session_start(hook_target)
    _hook_write(hook_target)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION_ID)
    receipt = _verify(hook_target, monkeypatch)
    assert receipt["subject_binding"]["producer_binding"]["hook_ownership"] == "owned"


def test_override_naming_the_producers_own_session_is_not_independent() -> None:
    """F4: BRIGADE_VERIFY_SESSION_ID must not defeat same-session detection."""
    fingerprint = _fingerprint()
    pid = producer_session.producer_id(fingerprint)
    assert producer_session.same_session(pid, producer_session.producer_id(fingerprint))
    assert producer_session.same_session(fingerprint, pid)
    assert not producer_session.same_session(pid, _fingerprint(OTHER_SESSION_ID))
    assert not producer_session.same_session(pid, producer_session.producer_id(_fingerprint(OTHER_SESSION_ID)))


def test_generated_patch_verified_through_an_override_naming_its_own_session_is_not_independent(
    hook_target: Path, monkeypatch
) -> None:
    payload = _write_patch_manifest(hook_target, manifest_id="generated-patch")
    payload["patch_source"] = "generated"
    verify_manifest.write_workspace_manifest(hook_target, payload)
    _track_workspace_manifest(hook_target, "generated-patch")
    _session_start(hook_target)
    _hook_write(hook_target, text="generated")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION_ID)
    monkeypatch.setenv("BRIGADE_VERIFY_SESSION_ID", producer_session.producer_id(_fingerprint()))
    monkeypatch.setenv("BRIGADE_GENERATED_PATCH_CANDIDATE_COUNT", "3")
    monkeypatch.setenv("BRIGADE_GENERATED_PATCH_MODEL", "gpt-test")
    monkeypatch.setenv("BRIGADE_GENERATED_PATCH_MODEL_VERSION", "2024-01")
    receipt = _run_manifest_verify(hook_target, "generated-patch", monkeypatch=monkeypatch, start_session=False)
    assert receipt["subject_binding"]["verifier_identity"]["session_id"] == producer_session.producer_id(_fingerprint())
    assert _project(hook_target, receipt).reason == "verifier_not_independent"


def test_hook_snapshot_lists_untracked_files_individually(hook_target: Path) -> None:
    """F3: a collapsed ``?? newdir/`` line would hide every file under it."""
    (hook_target / "newdir").mkdir()
    (hook_target / "newdir" / "a.txt").write_text("a\n")
    state = _session_start(hook_target)
    assert "?? newdir/a.txt" in state["start"]["git"]["dirty_files"]
    assert "?? newdir/" not in state["start"]["git"]["dirty_files"]


def test_unreadable_session_directory_fails_closed(hook_target: Path, monkeypatch) -> None:
    _session_start(hook_target)
    _hook_write(hook_target)

    def refuse(*_args, **_kwargs):
        raise producer_session.anchored_io.SessionIntegrityError("refused")

    monkeypatch.setattr(producer_session.anchored_io, "read_states_strict", refuse)
    reason = producer_session.ownership_reason(
        hook_target, producer_session.producer_id(_fingerprint()), "skills/brigade-work/SKILL.md"
    )
    assert reason == "concurrent_session_ownership"


# --- second fix round (#2589): foreign writes, status parsing, /clear, strict state reads ---


def _state_path(target: Path, session_id: str) -> Path:
    sessions = runtime._sessions_root(target.resolve())
    return next(
        path for path in sessions.glob("*.json") if json.loads(path.read_text()).get("session_id") == session_id
    )


def _write_event(target: Path, session_id: str, path: Path) -> dict:
    return {
        "session_id": session_id,
        "cwd": str(target),
        "tool_name": "Write",
        "tool_input": {"file_path": str(path), "content": "x\n"},
    }


def _pre(event: dict) -> None:
    runtime.handle_payload("PreToolUse", {**event, "hook_event_name": "PreToolUse"})


def _post(event: dict) -> None:
    runtime.handle_payload("PostToolUse", {**event, "hook_event_name": "PostToolUse"})


def _foreign_file_write(target: Path, session_id: str, name: str = "notes.txt") -> None:
    path = target / name
    event = _write_event(target, session_id, path)
    _pre(event)
    path.write_text("x\n")
    _post(event)


def _as_session(target: Path, monkeypatch, session_id: str = SESSION_ID) -> str:
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", session_id)
    return _project(target, _verify(target, monkeypatch)).reason


def test_a_foreign_write_that_began_before_this_session_and_finished_after_is_concurrent(
    hook_target: Path, monkeypatch
) -> None:
    """D1: the other session announced its subject write before B started and finished it after, then wrote elsewhere."""
    subject = hook_target / "skills" / "brigade-work" / "SKILL.md"
    _session_start(hook_target, OTHER_SESSION_ID)
    straddling = _write_event(hook_target, OTHER_SESSION_ID, subject)
    _pre(straddling)
    _session_start(hook_target, SESSION_ID)
    _hook_write(hook_target, SESSION_ID, "this session's own edit")
    _post(straddling)
    _foreign_file_write(hook_target, OTHER_SESSION_ID)
    assert _as_session(hook_target, monkeypatch) == "concurrent_session_ownership"


def test_a_foreign_file_write_elsewhere_plus_a_bash_write_is_concurrent(hook_target: Path, monkeypatch) -> None:
    """D1: the other session's file-tool record for another file must not account for its Bash write."""
    subject = hook_target / "skills" / "brigade-work" / "SKILL.md"
    _session_start(hook_target, SESSION_ID)
    _session_start(hook_target, OTHER_SESSION_ID)
    _foreign_file_write(hook_target, OTHER_SESSION_ID)
    bash = {
        "session_id": OTHER_SESSION_ID,
        "cwd": str(hook_target),
        "tool_name": "Bash",
        "tool_input": {"command": "printf 'by shell\\n' >> skills/brigade-work/SKILL.md"},
    }
    _pre(bash)
    with subject.open("a") as handle:
        handle.write("by shell\n")
    _post(bash)
    events = json.loads(_state_path(hook_target, OTHER_SESSION_ID).read_text())["write_events"]
    assert [(event["tool"], event["path"]) for event in events] == [("Write", "notes.txt"), ("Bash", None)]
    _hook_write(hook_target, SESSION_ID, "this session's own edit")
    assert _as_session(hook_target, monkeypatch) == "concurrent_session_ownership"


@posix_manifest_digest
def test_a_foreign_write_outside_the_target_does_not_veto(hook_target: Path, monkeypatch, tmp_path: Path) -> None:
    _session_start(hook_target, SESSION_ID)
    _session_start(hook_target, OTHER_SESSION_ID)
    outside = tmp_path / "elsewhere" / "plan.md"
    outside.parent.mkdir()
    event = _write_event(hook_target, OTHER_SESSION_ID, outside)
    _pre(event)
    outside.write_text("x\n")
    _post(event)
    events = json.loads(_state_path(hook_target, OTHER_SESSION_ID).read_text())["write_events"]
    assert events[-1]["outside"] is True
    _hook_write(hook_target, SESSION_ID)
    assert _as_session(hook_target, monkeypatch) == "eligible"


def test_write_events_are_bounded_and_eviction_is_recorded(hook_target: Path) -> None:
    _session_start(hook_target, OTHER_SESSION_ID)
    for index in range(producer_session.MAX_WRITE_EVENTS + 3):
        _foreign_file_write(hook_target, OTHER_SESSION_ID, f"n{index}.txt")
    state = json.loads(_state_path(hook_target, OTHER_SESSION_ID).read_text())
    assert len(state["write_events"]) == producer_session.MAX_WRITE_EVENTS
    assert state["write_events"][-1]["path"] == f"n{producer_session.MAX_WRITE_EVENTS + 2}.txt"
    assert localio.parse_iso_datetime(state["write_events_evicted_at"]) is not None


def test_a_foreign_event_list_that_lost_events_inside_the_window_fails_closed(hook_target: Path, monkeypatch) -> None:
    _session_start(hook_target, SESSION_ID)
    _session_start(hook_target, OTHER_SESSION_ID)
    _foreign_file_write(hook_target, OTHER_SESSION_ID)
    path = _state_path(hook_target, OTHER_SESSION_ID)
    state = json.loads(path.read_text())
    state["write_events_evicted_at"] = localio.utc_now_iso()
    path.write_text(json.dumps(state))
    _hook_write(hook_target, SESSION_ID)
    assert _as_session(hook_target, monkeypatch) == "concurrent_session_ownership"


def test_a_foreign_write_that_has_no_event_fails_closed(hook_target: Path, monkeypatch) -> None:
    """A reported write whose event was lost reads as an unattributed write, not as an accounted one."""
    _session_start(hook_target, SESSION_ID)
    _session_start(hook_target, OTHER_SESSION_ID)
    _foreign_file_write(hook_target, OTHER_SESSION_ID)
    path = _state_path(hook_target, OTHER_SESSION_ID)
    state = json.loads(path.read_text())
    state["last_write_at"] = localio.utc_now_iso()
    path.write_text(json.dumps(state))
    _hook_write(hook_target, SESSION_ID)
    assert _as_session(hook_target, monkeypatch) == "concurrent_session_ownership"


def _bash_event(session_id: str, target: Path, command: str) -> dict:
    return {"session_id": session_id, "cwd": str(target), "tool_name": "Bash", "tool_input": {"command": command}}


def test_a_foreign_bash_still_running_vetoes_before_its_post_tool_use(hook_target: Path, monkeypatch) -> None:
    """The other session's Bash changed the subject but has not completed, so no event records it yet."""
    subject = hook_target / "skills" / "brigade-work" / "SKILL.md"
    _session_start(hook_target, SESSION_ID)
    _session_start(hook_target, OTHER_SESSION_ID)
    _hook_write(hook_target, SESSION_ID, "this session's own edit")
    bash = _bash_event(OTHER_SESSION_ID, hook_target, "printf 'by shell\\n' >> skills/brigade-work/SKILL.md")
    _pre(bash)
    with subject.open("a") as handle:
        handle.write("by shell\n")
    state = json.loads(_state_path(hook_target, OTHER_SESSION_ID).read_text())
    assert state["pending_bash_started_at"]
    assert not state.get("write_events")
    assert _as_session(hook_target, monkeypatch) == "concurrent_session_ownership"


def _own_reason(target: Path) -> str | None:
    return producer_session.ownership_reason(
        target, producer_session.producer_id(_fingerprint()), "skills/brigade-work/SKILL.md"
    )


def _age_marker(path: Path, seconds: float) -> None:
    """Move a session's Bash marker and its state file's mtime ``seconds`` into the past."""
    state = json.loads(path.read_text())
    started = localio.utc_now() - timedelta(seconds=seconds)
    state["pending_bash_started_at"] = started.isoformat()
    path.write_text(json.dumps(state))
    old = time.time() - seconds
    os.utime(path, (old, old))


def test_a_foreign_bash_that_started_before_the_scan_window_still_vetoes(hook_target: Path, monkeypatch) -> None:
    """A long command leaves its state file untouched, so an mtime older than the start must not hide the marker."""
    _session_start(hook_target, SESSION_ID)
    _session_start(hook_target, OTHER_SESSION_ID)
    _hook_write(hook_target, SESSION_ID, "this session's own edit")
    _pre(_bash_event(OTHER_SESSION_ID, hook_target, "sleep 600"))
    _age_marker(_state_path(hook_target, OTHER_SESSION_ID), 1800)
    assert _as_session(hook_target, monkeypatch) == "concurrent_session_ownership"


def test_an_abandoned_foreign_bash_marker_expires(hook_target: Path, monkeypatch) -> None:
    """A command that failed or was killed never reaches PostToolUse, so its marker must not veto forever."""
    _session_start(hook_target, SESSION_ID)
    _session_start(hook_target, OTHER_SESSION_ID)
    _hook_write(hook_target, SESSION_ID, "this session's own edit")
    _pre(_bash_event(OTHER_SESSION_ID, hook_target, "false"))
    other_path = _state_path(hook_target, OTHER_SESSION_ID)
    lifetime = producer_session.BASH_MARKER_LIFETIME.total_seconds()
    _age_marker(other_path, lifetime + 600)
    assert _own_reason(hook_target) is None
    # A fresh state file does not revive an expired marker either.
    os.utime(other_path, None)
    assert _own_reason(hook_target) is None


def test_an_unreadable_foreign_state_inside_the_marker_lifetime_fails_closed(hook_target: Path, monkeypatch) -> None:
    """It could hold a running Bash marker, so it cannot be skipped as old."""
    _session_start(hook_target, SESSION_ID)
    _hook_write(hook_target, SESSION_ID, "this session's own edit")
    junk = runtime._sessions_root(hook_target.resolve()) / "stale-junk.json"
    junk.write_text("{not json")
    old = time.time() - 1800
    os.utime(junk, (old, old))
    assert _as_session(hook_target, monkeypatch) == "concurrent_session_ownership"


def test_a_corrupt_foreign_state_older_than_the_marker_lifetime_stays_unread(hook_target: Path, monkeypatch) -> None:
    """Nothing in it could still be running, so one old junk file does not veto every trial."""
    _session_start(hook_target, SESSION_ID)
    _hook_write(hook_target, SESSION_ID, "this session's own edit")
    junk = runtime._sessions_root(hook_target.resolve()) / "stale-junk.json"
    junk.write_text("{not json")
    old = time.time() - producer_session.BASH_MARKER_LIFETIME.total_seconds() - 600
    os.utime(junk, (old, old))
    assert _own_reason(hook_target) is None


def test_this_sessions_own_pending_bash_does_not_veto_it(hook_target: Path, monkeypatch) -> None:
    _session_start(hook_target, SESSION_ID)
    _hook_write(hook_target, SESSION_ID, "this session's own edit")
    _pre(_bash_event(SESSION_ID, hook_target, "sleep 600"))
    assert json.loads(_state_path(hook_target, SESSION_ID).read_text())["pending_bash_started_at"]
    reason = producer_session.ownership_reason(
        hook_target, producer_session.producer_id(_fingerprint()), "skills/brigade-work/SKILL.md"
    )
    assert reason is None


def test_a_finished_foreign_bash_that_left_the_subject_alone_does_not_veto(hook_target: Path, monkeypatch) -> None:
    """PostToolUse clears the marker; a Bash that wrote nothing detectable leaves no event, so it behaves as before."""
    _session_start(hook_target, SESSION_ID)
    _session_start(hook_target, OTHER_SESSION_ID)
    _hook_write(hook_target, SESSION_ID, "this session's own edit")
    bash = _bash_event(OTHER_SESSION_ID, hook_target, "true")
    _pre(bash)
    _post(bash)
    state = json.loads(_state_path(hook_target, OTHER_SESSION_ID).read_text())
    assert "pending_bash_started_at" not in state
    assert "pending_bash_fingerprint" not in state
    reason = producer_session.ownership_reason(
        hook_target, producer_session.producer_id(_fingerprint()), "skills/brigade-work/SKILL.md"
    )
    assert reason is None


def test_write_events_survive_a_task_epoch_reset(hook_target: Path) -> None:
    _session_start(hook_target, SESSION_ID, source="startup")
    _foreign_file_write(hook_target, SESSION_ID)
    _session_start(hook_target, SESSION_ID, source="clear")
    state = runtime.read_session_state(hook_target.resolve(), SESSION_ID)
    assert [event["path"] for event in state["write_events"]] == ["notes.txt"]


def _work_session_snapshot(target: Path) -> dict:
    return {"id": "20261119-000000-work-test", "start": {"git": work_cmd._git_snapshot(target)}}


def _clean_at_start(target: Path, subject_path: str) -> bool:
    binding = verify_trial.build_producer_binding(
        session_id="20261119-000000-work-test",
        session_payload=_work_session_snapshot(target),
        subject_path=subject_path,
        owned_delta="0" * 64,
    )
    return binding["subject_clean_at_start"]


@windows_invalid_filename
def test_a_quoted_rename_whose_names_hold_an_arrow_reads_dirty(tmp_path: Path) -> None:
    """D2: the rename split at the arrow inside the quotes and never named the subject."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_verify_git_repo(repo)
    (repo / "old -> subject.txt").write_text("a\n")
    _commit_all(repo, "add")
    subprocess.run(["git", "mv", "old -> subject.txt", "new -> subject.txt"], cwd=repo, check=True)
    assert _clean_at_start(repo, "new -> subject.txt") is False
    assert _clean_at_start(repo, "old -> subject.txt") is False
    assert _clean_at_start(repo, "other.txt") is True


def test_a_wholly_untracked_target_under_a_repository_subdirectory_reads_dirty(tmp_path: Path) -> None:
    """D2: ``git status --short`` printed ``?? ./`` for it, which normalized to nothing."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_verify_git_repo(repo)
    (repo / "tracked.txt").write_text("a\n")
    _commit_all(repo, "add")
    target = repo / "pkg"
    (target / "skills").mkdir(parents=True)
    (target / "skills" / "SKILL.md").write_text("new\n")
    assert _clean_at_start(target, "skills/SKILL.md") is False
    assert _clean_at_start(target, "skills/OTHER.md") is False


@windows_invalid_filename
def test_hook_snapshot_reads_a_renamed_subject_with_an_arrow_as_dirty(hook_target: Path) -> None:
    (hook_target / "a -> b.txt").write_text("a\n")
    _commit_all(hook_target, "add")
    subprocess.run(["git", "mv", "a -> b.txt", "c -> d.txt"], cwd=hook_target, check=True)
    start = _session_start(hook_target)["start"]
    assert git_status_snapshot.subject_dirty("c -> d.txt", start["git"]) is True
    assert git_status_snapshot.subject_dirty("a -> b.txt", start["git"]) is True


def test_an_older_status_format_never_reads_clean() -> None:
    legacy = {"dirty_files": [" M unrelated.txt"]}
    assert git_status_snapshot.subject_dirty("skills/brigade-work/SKILL.md", legacy) is True
    binding = verify_trial.build_producer_binding(
        session_id="20261119-000000-work-test",
        session_payload={"id": "20261119-000000-work-test", "start": {"git": legacy}},
        subject_path="skills/brigade-work/SKILL.md",
        owned_delta="0" * 64,
    )
    assert binding["subject_clean_at_start"] is False
    assert "start_git" not in binding


@posix_manifest_digest
def test_work_session_receipt_against_an_older_session_payload_fails_closed(hook_target: Path, monkeypatch) -> None:
    target = hook_target
    assert work_cmd.start(target=target, title="legacy") == 0
    _edit_subject(target)
    receipt = _verify(target, monkeypatch)
    producer = receipt["subject_binding"]["producer_binding"]
    assert producer["subject_clean_at_start"] is True
    assert _project(target, receipt).reason == "eligible"
    session_json = next((target / ".brigade" / "work").glob("*/session.json"))
    session = json.loads(session_json.read_text())
    git = session["start"]["git"]
    git.pop("status_format")
    git.pop("prefix")
    git["dirty_files"] = []
    session_json.write_text(json.dumps(session))
    assert _project(target, receipt).eligible is False


@posix_manifest_digest
def test_work_after_a_clear_produces_a_trial(hook_target: Path, monkeypatch) -> None:
    """D3: a task-epoch reset left the state without a start, so the new task could never score."""
    _session_start(hook_target, SESSION_ID, source="startup")
    _session_start(hook_target, SESSION_ID, source="clear")
    state = runtime.read_session_state(hook_target.resolve(), SESSION_ID)
    assert state["start"]["id"] and len(state["start_history"]) == 1
    _hook_write(hook_target)
    assert _as_session(hook_target, monkeypatch) == "eligible"


@posix_manifest_digest
def test_an_eligible_receipt_stays_eligible_after_a_later_clear(hook_target: Path, monkeypatch) -> None:
    """D3: the receipt is bound to the start it was made under, and that start outlives the reset."""
    _session_start(hook_target, SESSION_ID, source="startup")
    _hook_write(hook_target)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION_ID)
    receipt = _verify(hook_target, monkeypatch)
    bound = receipt["subject_binding"]["producer_binding"]["hook_start_id"]
    assert _project(hook_target, receipt).reason == "eligible"
    _session_start(hook_target, SESSION_ID, source="clear")
    state = runtime.read_session_state(hook_target.resolve(), SESSION_ID)
    assert state["start"]["id"] != bound
    assert _project(hook_target, receipt).reason == "eligible"


def test_a_receipt_bound_to_a_start_that_aged_out_of_the_history_fails_closed(hook_target: Path, monkeypatch) -> None:
    _session_start(hook_target, SESSION_ID, source="startup")
    _hook_write(hook_target)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION_ID)
    receipt = _verify(hook_target, monkeypatch)
    for _ in range(producer_session.MAX_START_HISTORY + 1):
        _session_start(hook_target, SESSION_ID, source="clear")
    state = runtime.read_session_state(hook_target.resolve(), SESSION_ID)
    assert len(state["start_history"]) == producer_session.MAX_START_HISTORY
    assert _project(hook_target, receipt).reason == "producer_session_missing"


def test_a_receipt_without_a_recorded_start_id_fails_closed(hook_target: Path, monkeypatch) -> None:
    _session_start(hook_target, SESSION_ID, source="startup")
    _hook_write(hook_target)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION_ID)
    receipt = _verify(hook_target, monkeypatch)
    del receipt["subject_binding"]["producer_binding"]["hook_start_id"]
    assert _project(hook_target, receipt).eligible is False


def _own_a_subject_with_a_foreign_state_file(hook_target: Path, monkeypatch, damage) -> str:
    _session_start(hook_target, SESSION_ID)
    _hook_write(hook_target)
    sessions = runtime._sessions_root(hook_target.resolve())
    damage(sessions)
    return _as_session(hook_target, monkeypatch)


def test_a_malformed_foreign_state_file_fails_closed(hook_target: Path, monkeypatch) -> None:
    """D4: ``iter_states`` skipped it, so the ownership check never saw that session."""
    reason = _own_a_subject_with_a_foreign_state_file(
        hook_target, monkeypatch, lambda sessions: (sessions / "foreign.json").write_text("{not json")
    )
    assert reason == "concurrent_session_ownership"


def test_a_non_object_foreign_state_file_fails_closed(hook_target: Path, monkeypatch) -> None:
    reason = _own_a_subject_with_a_foreign_state_file(
        hook_target, monkeypatch, lambda sessions: (sessions / "foreign.json").write_text("[]")
    )
    assert reason == "concurrent_session_ownership"


def test_an_oversized_foreign_state_file_fails_closed(hook_target: Path, monkeypatch) -> None:
    reason = _own_a_subject_with_a_foreign_state_file(
        hook_target,
        monkeypatch,
        lambda sessions: (sessions / "foreign.json").write_text(
            json.dumps({"pad": "x" * producer_session.outcome_attribution.MAX_STATE_BYTES})
        ),
    )
    assert reason == "concurrent_session_ownership"


def test_a_non_regular_foreign_state_entry_fails_closed(hook_target: Path, monkeypatch) -> None:
    reason = _own_a_subject_with_a_foreign_state_file(
        hook_target, monkeypatch, lambda sessions: (sessions / "foreign.json").mkdir()
    )
    assert reason == "concurrent_session_ownership"


@posix_permissions
def test_an_unreadable_foreign_state_file_fails_closed(hook_target: Path, monkeypatch) -> None:
    def damage(sessions: Path) -> None:
        foreign = sessions / "foreign.json"
        foreign.write_text(json.dumps({"session_id": "x"}))
        foreign.chmod(0)

    try:
        reason = _own_a_subject_with_a_foreign_state_file(hook_target, monkeypatch, damage)
    finally:
        runtime._sessions_root(hook_target.resolve()).joinpath("foreign.json").chmod(0o600)
    if os.access(runtime._sessions_root(hook_target.resolve()) / "foreign.json", os.R_OK) and os.geteuid() == 0:
        pytest.skip("root reads a mode 0 file")
    assert reason == "concurrent_session_ownership"


def test_a_foreign_state_file_that_cannot_be_statted_fails_closed(hook_target: Path, monkeypatch) -> None:
    real_stat = producer_session.anchored_io.dirfd.stat_child

    def flaky(fd, name, **kwargs):
        if name == "foreign.json":
            raise PermissionError("stat refused")
        return real_stat(fd, name, **kwargs)

    monkeypatch.setattr(producer_session.anchored_io.dirfd, "stat_child", flaky)
    reason = _own_a_subject_with_a_foreign_state_file(
        hook_target, monkeypatch, lambda sessions: (sessions / "foreign.json").write_text(json.dumps({"a": 1}))
    )
    assert reason == "concurrent_session_ownership"


@posix_manifest_digest
def test_an_old_malformed_state_file_outside_the_window_does_not_veto(hook_target: Path, monkeypatch) -> None:
    def damage(sessions: Path) -> None:
        old = sessions / "foreign.json"
        old.write_text("{not json")
        os.utime(old, (1_000_000, 1_000_000))

    assert _own_a_subject_with_a_foreign_state_file(hook_target, monkeypatch, damage) == "eligible"
