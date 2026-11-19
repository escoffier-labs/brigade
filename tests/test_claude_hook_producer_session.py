"""A hook-enabled Claude session can produce a manifest trial without ``work start`` (#2589)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from brigade import localio, scorecard, verify_manifest, verify_trial, work_cmd
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
        producer_session, "subprocess", SimpleNamespace(run=run, SubprocessError=subprocess.SubprocessError)
    )


def _session_start(target: Path, session_id: str = SESSION_ID) -> dict:
    payload = {"session_id": session_id, "cwd": str(target), "hook_event_name": "SessionStart"}
    runtime.handle_payload("SessionStart", payload)
    state = runtime.read_session_state(target.resolve(), session_id)
    assert state is not None
    return state


def _edit_subject(target: Path, text: str = "patched") -> None:
    skill = _write_skill_subject(target, content=f"# brigade-work\n{text}\n")
    subprocess.run(["git", "add", str(skill.relative_to(target))], cwd=target, check=True)


def _verify(target: Path, monkeypatch) -> dict:
    return _run_manifest_verify(target, MANIFEST_ID, monkeypatch=monkeypatch, start_session=False)


def test_hook_records_start_dirty_files_in_work_session_shape(hook_target: Path) -> None:
    (hook_target / "scratch.txt").write_text("pending\n")
    state = _session_start(hook_target)
    assert state["start"] == {"git": {"dirty_files": ["?? scratch.txt"]}}


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
    lines = "\n".join(f"?? f{index}" for index in range(producer_session.MAX_DIRTY_LINES + 1))
    _fake_git(monkeypatch, lambda *_a, **_k: subprocess.CompletedProcess([], 0, stdout=lines, stderr=""))
    assert producer_session.snapshot_dirty_files(tmp_path) is None


def test_nonzero_git_exit_records_nothing(monkeypatch, tmp_path: Path) -> None:
    _fake_git(monkeypatch, lambda *_a, **_k: subprocess.CompletedProcess([], 128, stdout="", stderr="not a repo"))
    assert producer_session.snapshot_dirty_files(tmp_path) is None


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
    assert payload == {
        "id": f"claude-hook:{_fingerprint()}",
        "start": {"git": {"dirty_files": []}},
    }


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


def test_hook_session_trial_is_eligible_and_scores(hook_target: Path, monkeypatch) -> None:
    """The headline case: no work session, a hook session that edits the subject and verifies it."""
    _session_start(hook_target)
    _edit_subject(hook_target)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION_ID)

    receipt = _verify(hook_target, monkeypatch)

    producer = receipt["subject_binding"]["producer_binding"]
    assert producer["work_session_id"] == f"claude-hook:{_fingerprint()}"
    assert producer["subject_clean_at_start"] is True
    projection = verify_trial.project_trial(receipt, target=hook_target)
    assert projection.reason == "eligible"
    assert projection.eligible is True
    assert [audit.eligible for audit in scorecard.audit_all_verify_receipts(hook_target)] == [True]


def test_hook_printed_session_value_also_resolves_the_producer(hook_target: Path, monkeypatch) -> None:
    _session_start(hook_target)
    _edit_subject(hook_target)
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
    _edit_subject(hook_target, "generated")
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
