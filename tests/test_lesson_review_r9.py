"""Receipt observation timing and crash-safe correction draining."""

import json
from datetime import datetime

import pytest

from brigade import lesson_activity as activity, lesson_admission as admission, localio, receipt_signing
from tests.test_lesson_review import event, proposal, scope as scope


def test_first_post_tool_observation_accepts_its_just_completed_receipt(scope):
    candidate = proposal(scope)
    failed = json.loads((scope / candidate["failure_evidence"]).read_text())
    completed = datetime.fromisoformat(failed["completed_at"].replace("Z", "+00:00")).timestamp()
    event(scope, now=completed + 0.2)
    event(scope, now=completed + 2)
    admission.admit(scope, candidate, activity.load(scope)["events"], {})


@pytest.mark.parametrize("gap", [6, 3600])
def test_first_observation_cannot_revive_old_receipt(scope, gap):
    candidate = proposal(scope)
    failed = json.loads((scope / candidate["failure_evidence"]).read_text())
    completed = datetime.fromisoformat(failed["completed_at"].replace("Z", "+00:00")).timestamp()
    event(scope, now=completed + gap)
    event(scope, now=completed + gap + 1)
    with pytest.raises(ValueError, match="stale lesson receipt"):
        admission.admit(scope, candidate, activity.load(scope)["events"], {})


def test_later_observation_cannot_use_first_receipt_exception(scope):
    candidate = proposal(scope)
    failed = json.loads((scope / candidate["failure_evidence"]).read_text())
    completed = datetime.fromisoformat(failed["completed_at"].replace("Z", "+00:00")).timestamp()
    activity.record_event(scope, "PreToolUse", {"tool_input": "read source"}, now=completed + 0.1)
    event(scope, now=completed + 0.2)
    event(scope, now=completed + 2)
    candidate["event"] = 3
    with pytest.raises(ValueError, match="stale lesson receipt"):
        admission.admit(scope, candidate, activity.load(scope)["events"], {})


@pytest.mark.parametrize("crash", ["before-save", "before-ack"])
@pytest.mark.parametrize("legacy", [False, True])
def test_prompt_drain_survives_write_failure_without_duplicates(scope, monkeypatch, crash, legacy):
    text = "Correction: keep publication atomic to prevent partial reads."
    activity.record_event(scope, "UserPromptSubmit", {"prompt": text}, now=99)
    spool = activity.prompt_path(scope)
    if legacy:
        activity.io.write(spool, activity.io.json_bytes([{"prompt": text, "at": 99}]))
    write = activity.io.write

    def fail(path, data, **kwargs):
        should_fail = path.name == "state.json" if crash == "before-save" else path == spool and data == b"null\n"
        if should_fail:
            raise OSError("injected persistence failure")
        return write(path, data, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(activity.io, "write", fail)
        if crash == "before-save":
            with pytest.raises(OSError, match="injected persistence failure"):
                event(scope)
        else:
            event(scope)
    assert activity._read_prompts(spool)
    event(scope)
    state = activity.load(scope)
    assert [e["text"] for e in state["events"] if e["kind"] == "UserPromptSubmit"] == [text]
    # An acknowledgement failure now occurs after the tool event is saved.
    assert state["sequence"] == (3 if crash == "before-ack" else 2)
    assert not activity._read_prompts(spool)


@pytest.mark.parametrize("damage", ["unsigned", "other-session", "other-observation"])
def test_first_receipt_exception_requires_authenticated_session_binding(scope, damage):
    candidate = proposal(scope)
    path = scope / candidate["failure_evidence"]
    failed = json.loads(path.read_text())
    completed = datetime.fromisoformat(failed["completed_at"].replace("Z", "+00:00")).timestamp()
    event(scope, now=completed + 0.2)
    event(scope, now=completed + 2)
    if damage == "unsigned":
        failed["digests"].pop("workspace_signature")
    else:
        failed["producer_work_session_id" if damage == "other-session" else "run_id"] = "other"
        failed["digests"]["receipt_sha256"] = localio.canonical_json_digest(failed, exclude_keys={"digests"})
        receipt_signing.stamp_workspace_signature(scope, failed)
    path.write_text(json.dumps(failed))
    with pytest.raises(ValueError):
        admission.admit(scope, candidate, activity.load(scope)["events"], {})


def test_retry_keeps_new_and_identical_prompts(scope, monkeypatch):
    text = "Correction: keep publication atomic to prevent partial reads."
    activity.record_event(scope, "UserPromptSubmit", {"prompt": text}, now=99)
    write = activity.io.write
    spool = activity.prompt_path(scope)

    def fail_ack(path, data, **kwargs):
        if path == spool and data == b"null\n":
            raise OSError("injected ack failure")
        return write(path, data, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(activity.io, "write", fail_ack)
        event(scope)
    assert activity.load(scope)["events"][-1]["kind"] == "PostToolUse"
    assert activity._read_prompts(spool)
    activity.record_event(scope, "UserPromptSubmit", {"prompt": text}, now=100)
    event(scope)
    prompts = [e for e in activity.load(scope)["events"] if e["kind"] == "UserPromptSubmit"]
    assert len(prompts) == 2
    assert [e["at"] for e in prompts] == [99, 100]
