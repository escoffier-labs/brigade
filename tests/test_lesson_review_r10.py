"""Workspace edits cannot authorize receipt admission or prompt acknowledgement."""

import json
from datetime import datetime

import pytest

from brigade import lesson_activity as activity, lesson_admission as admission, lesson_review as review
from tests.test_lesson_review import event, proposal
from tests.test_lesson_review_r4 import scope as scope


def test_forged_first_observation_cannot_revive_authentic_failure(scope):
    candidate = proposal(scope)
    failure = json.loads((scope / candidate["failure_evidence"]).read_text())
    completed = datetime.fromisoformat(failure["completed_at"].replace("Z", "+00:00")).timestamp()
    # The actual window started an hour after this authentic failure completed.
    event(scope, now=completed + 3600)
    event(scope, now=completed + 3601)
    state = activity.load(scope)
    forged = dict(state["events"][0])
    forged.update(id=0, at=completed + 0.2, earliest_unreviewed_at=completed + 0.2)
    state["events"].insert(0, forged)
    state["earliest_unreviewed_at"] = completed + 0.2
    activity.save(scope, state)
    with pytest.raises(ValueError):
        admission.admit(scope, candidate, activity.load(scope)["events"], {})


@pytest.mark.parametrize("damage", ["deleted", "text", "timestamp", "event-id", "prompt-id"])
def test_prompt_retry_requires_matching_authenticated_saved_event(scope, monkeypatch, damage):
    text = "Correction: keep publication atomic to prevent partial reads."
    activity.record_event(scope, "UserPromptSubmit", {"prompt": text}, now=99)
    spool = activity.prompt_path(scope)
    prompt_id = activity._read_prompts(spool)[0]["prompt_id"]
    write = activity.io.write

    def fail_ack(path, data, **kwargs):
        if path == spool and data == b"null\n":
            raise OSError("injected acknowledgement failure")
        return write(path, data, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(activity.io, "write", fail_ack)
        event(scope)
        state = activity.load(scope)
        assert prompt_id in state["drained_prompt_ids"]
        assert [e["kind"] for e in state["events"]] == ["UserPromptSubmit", "PostToolUse"]
        if damage == "deleted":
            state["events"].clear()
        else:
            field, value = {
                "text": ("text", "An unrelated replacement observation."),
                "timestamp": ("at", 1),
                "event-id": ("id", 0),
                "prompt-id": ("prompt_id", "invented-prompt"),
            }[damage]
            state["events"][0][field] = value
        activity.save(scope, state)
        event(scope)
        # A second failed acknowledgement still leaves the spool and a newly
        # recorded correction.
        assert activity._read_prompts(spool)[0]["prompt_id"] == prompt_id
        prompts = [e for e in activity.load(scope)["events"] if e["kind"] == "UserPromptSubmit"]
        assert prompts
        # Each failed acknowledgement follows the hook's final tool save.
        assert prompts[-1]["id"] == 3
        assert prompts[-1]["text"] == text
        assert prompts[-1]["at"] == 99
    event(scope)
    assert not activity._read_prompts(spool)


def test_failed_acknowledgement_keeps_hook_event_and_prompt(scope, monkeypatch):
    text = "Correction: keep publication atomic to prevent partial reads."
    activity.record_event(scope, "UserPromptSubmit", {"prompt": text}, now=99)
    spool = activity.prompt_path(scope)

    def fail_ack(_target):
        raise OSError("injected acknowledgement failure")

    with monkeypatch.context() as patch:
        patch.setattr(activity, "acknowledge_prompts", fail_ack)
        event(scope, now=100)
    state = activity.load(scope)
    assert [e["kind"] for e in state["events"]] == ["UserPromptSubmit", "PostToolUse"]
    assert state["events"][0]["text"] == text
    assert activity._read_prompts(spool)[0]["prompt_id"] == state["events"][0]["prompt_id"]
    event(scope, now=101)
    assert not activity._read_prompts(spool)
    assert [e["kind"] for e in activity.load(scope)["events"]].count("UserPromptSubmit") == 1


def test_failed_acknowledgement_does_not_force_review_retry(scope, monkeypatch):
    activity.record_event(scope, "UserPromptSubmit", {"prompt": "Keep publication atomic."}, now=99)

    def fail_ack(_target):
        raise OSError("injected acknowledgement failure")

    monkeypatch.setattr(activity, "acknowledge_prompts", fail_ack)
    assert review.run(scope, now=100) == {"status": "waiting"}
    assert activity._read_prompts(activity.prompt_path(scope))
    assert activity.load(scope)["drained_prompt_ids"]


def test_late_prompt_drain_does_not_move_last_activity_backwards(scope):
    event(scope, now=200)
    activity.record_event(scope, "UserPromptSubmit", {"prompt": "Keep the original evidence for review."}, now=99)
    state = activity.load(scope)
    activity.drain_prompts(scope, state)
    assert activity.load(scope)["last_activity"] == 200


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", 0),
        ("append_sequence", 0),
        ("at", 1),
        ("earliest_unreviewed_at", 1),
        ("verify_run_id", "copied-old-run"),
        ("work_session_id", "copied-work-session"),
        ("harness_session_fingerprint", "copied-fingerprint"),
        ("session_id", "copied-session"),
        ("text", "Forged resolution quote with copied receipt identifiers."),
        ("authentication", {}),
    ],
)
def test_observation_envelope_binds_admission_fields(scope, field, value):
    candidate = proposal(scope)
    event(scope)
    event(scope)
    state = activity.load(scope)
    state["events"][0][field] = value
    with pytest.raises(ValueError, match="authenticated session observations"):
        admission.admit(scope, candidate, state["events"], {})


@pytest.mark.parametrize("damage", ["missing", "symlink", "public", "malformed"])
def test_unavailable_confined_key_never_authorizes_admission(scope, monkeypatch, damage):
    import sys
    from brigade import receipt_signing

    if sys.platform == "win32":
        pytest.skip("confined workspace authority is POSIX-only")
    candidate = proposal(scope)
    event(scope)
    event(scope)
    key = scope / ".brigade/receipt-signing-key"
    audit = scope / "audit-key"
    audit.write_bytes(key.read_bytes())
    audit.chmod(0o600)
    monkeypatch.setenv(receipt_signing.KEY_ENV, str(audit))
    if damage in {"missing", "symlink"}:
        key.unlink()
        if damage == "symlink":
            key.symlink_to(audit)
    elif damage == "public":
        key.chmod(0o644)
    else:
        key.write_text("invalid key")
    with pytest.raises(ValueError, match="authenticated session observations"):
        admission.admit(scope, candidate, activity.load(scope)["events"], {})
    activity.record_event(scope, "UserPromptSubmit", {"prompt": "Keep the original evidence for review."}, now=99)
    state = activity.load(scope)
    activity.drain_prompts(scope, state)
    assert not activity._read_prompts(activity.prompt_path(scope))
    assert not any(e["kind"] == "UserPromptSubmit" for e in activity.load(scope)["events"])


def test_unsigned_state_boundary_cannot_revive_old_failure(scope):
    candidate = proposal(scope)
    failed = json.loads((scope / candidate["failure_evidence"]).read_text())
    completed = datetime.fromisoformat(failed["completed_at"].replace("Z", "+00:00")).timestamp()
    event(scope, now=completed + 3600)
    event(scope, now=completed + 3601)
    state = activity.load(scope)
    state["earliest_unreviewed_at"] = completed - 1
    activity.save(scope, state)
    from brigade import lesson_review as review

    # Admission must use signed event boundaries, even through review._review.
    from unittest.mock import patch

    with patch.object(review, "_invoke") as invoke:
        invoke.side_effect = [{"read_cards": []}, {"lessons": [candidate]}]
        with pytest.raises(ValueError, match="stale lesson receipt"):
            review._review(scope, activity.config(scope), activity.load(scope), "lesson-example")


@pytest.mark.parametrize("latency", [0.2, 5])
def test_signed_first_observation_keeps_codex_dispatch_allowance(scope, latency):
    candidate = proposal(scope)
    failed = json.loads((scope / candidate["failure_evidence"]).read_text())
    completed = datetime.fromisoformat(failed["completed_at"].replace("Z", "+00:00")).timestamp()
    event(scope, now=completed + latency)
    event(scope, now=completed + latency + 1)
    assert admission.admit(scope, candidate, activity.load(scope)["events"], {})["kind"] == "verified-fix"


def test_sequence_rollback_cannot_reorder_signed_observations(scope):
    event(scope)
    state = activity.load(scope)
    state["sequence"] = 0
    activity.save(scope, state)
    with pytest.raises(ValueError, match="sequence precedes authenticated history"):
        event(scope)


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_real_hook_process_reads_confined_workspace_key(scope, harness):
    import subprocess
    import sys

    from brigade import lesson_auth
    from tests.test_work_mode import _wired

    if sys.platform == "win32":
        pytest.skip("confined workspace authority is POSIX-only")
    target = _wired(scope, harnesses=(harness,))
    key = target / ".brigade/receipt-signing-key"
    key.write_bytes((scope / ".brigade/receipt-signing-key").read_bytes())
    key.chmod(0o600)
    (target / ".brigade/work").mkdir(exist_ok=True)
    (target / ".brigade/work/current").write_text("example-work-session")
    payload = {
        "hook_event_name": "PostToolUse",
        "session_id": "example-session",
        "cwd": str(target),
        "tool_name": "Bash",
        "tool_input": {"command": "brigade work verify run --manifest lesson"},
        "tool_output": "run: example",
    }
    if harness == "codex":
        argv = [
            sys.executable,
            "-m",
            "brigade",
            "work",
            "presence-hook",
            "--harness",
            "codex",
            "--stdin",
            "--context",
            "codex-observe",
        ]
    else:
        from brigade.claude_hooks.package import PACKAGE_REF

        argv = [
            sys.executable,
            "-m",
            "brigade.claude_hooks.entry",
            "--event",
            "PostToolUse",
            "--package",
            PACKAGE_REF,
            "--target",
            str(target),
        ]
    # Recording is off in code until #2620, so the child process turns the gate on first.
    gate_on = (
        "import runpy, sys\n"
        "from brigade import lesson_admission\n"
        "lesson_admission.AUTOMATIC_LESSON_ADMISSION = True\n"
        "module = sys.argv[1]\n"
        "sys.argv = [module] + sys.argv[2:]\n"
        "runpy.run_module(module, run_name='__main__', alter_sys=True)\n"
    )
    argv = [argv[0], "-c", gate_on, *argv[2:]]
    result = subprocess.run(argv, input=json.dumps(payload), capture_output=True, text=True, cwd=target, timeout=30)
    assert result.returncode == 0, result.stderr
    events = activity.load(target)["events"]
    assert len(events) == 1
    assert lesson_auth.authentic(target, events[0])
    assert events[0]["verify_run_id"] == "example"
    assert events[0]["work_session_id"] == "example-work-session"
    assert events[0]["harness_session_fingerprint"]


def test_append_sequence_follows_authenticated_history(scope):
    event(scope)
    event(scope)
    sequences = [e["append_sequence"] for e in activity.load(scope)["events"]]
    assert sequences[1] == sequences[0] + 1
