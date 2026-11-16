"""Lesson recording needs authority and survives clock and buffer resets."""

import json

import pytest

from brigade import lesson_activity as activity, lesson_auth, lesson_review as review, receipt_signing
from brigade import lesson_admission as admission

from tests.test_lesson_review import event, seat
from tests.test_lesson_review_r4 import scope as scope

DEFAULT_AUTOMATIC_LESSON_ADMISSION = admission.AUTOMATIC_LESSON_ADMISSION


@pytest.mark.parametrize("entry", ["record", "review", "drain"])
def test_missing_key_stops_and_clears_spool(scope, monkeypatch, entry):
    event(scope)
    activity.record_event(scope, "UserPromptSubmit", {"prompt": "Keep publication atomic."}, now=100)
    before = activity.load(scope)
    monkeypatch.setattr(receipt_signing, "load_workspace_key", lambda _target: None)
    calls = seat(monkeypatch)
    if entry == "record":
        result = activity.record_event(scope, "PostToolUse", {"tool_output": "Read source."}, now=101)
        assert result == "lesson_review_needs_key"
    elif entry == "review":
        for now in (111, 411, 711):
            assert review.run(scope, now=now)["status"] == "lesson_review_needs_key"
    else:
        activity.drain_prompts(scope, before)
    assert not calls
    assert not activity._read_prompts(activity.prompt_path(scope))
    assert activity.load(scope) == before


def test_append_survives_history_above_clock(scope):
    event(scope)
    state = activity.load(scope)
    state["events"][0]["append_sequence"] = 10**30
    lesson_auth.stamp(scope, state["events"][0])
    activity.save(scope, state)
    event(scope)
    assert activity.load(scope)["events"][-1]["append_sequence"] == 10**30 + 1


def test_identical_clock_readings_still_order_appends(scope, monkeypatch):
    monkeypatch.setattr(activity.time, "monotonic_ns", lambda: 1)
    event(scope)
    event(scope)
    assert [e["append_sequence"] for e in activity.load(scope)["events"]] == [1, 2]


def test_evicted_prompt_waits_for_final_persisted_event(scope):
    for _ in range(activity.MAX_EVENTS):
        event(scope)
    activity.record_event(scope, "UserPromptSubmit", {"prompt": "Keep publication atomic."}, now=101)
    state = activity.load(scope)
    activity.drain_prompts(scope, state)
    assert not any(e["kind"] == "UserPromptSubmit" for e in state["events"])
    assert activity._read_prompts(activity.prompt_path(scope))
    sequence = state["sequence"]
    activity.drain_prompts(scope, state)
    assert state["sequence"] == sequence
    state["reviewed"] = state["sequence"]
    activity.save(scope, state)
    activity.drain_prompts(scope, state)
    assert not activity._read_prompts(activity.prompt_path(scope))


@pytest.mark.parametrize("keyless", [True, False])
def test_forged_pending_never_publishes(scope, monkeypatch, keyless):
    from tests.test_lesson_review import proposal

    from brigade import lesson_admission as admission

    monkeypatch.setattr(admission, "AUTOMATIC_LESSON_ADMISSION", False)
    candidate = proposal(scope)
    state = activity.load(scope)
    state.update(
        calls=2,
        pending={
            "run_id": "lesson-" + "a" * 32,
            "sequence": 0,
            "calls": 2,
            "digest": "invented",
            "lesson": candidate,
            "inbox": review.OUTBOX,
        },
    )
    activity.save(scope, state)
    if keyless:
        monkeypatch.setattr(receipt_signing, "load_workspace_key", lambda _target: None)
    calls = seat(monkeypatch)
    result = review.run(scope, now=111)
    assert result["status"] != "committed"
    assert not calls
    assert "pending" not in activity.load(scope)
    assert not list((scope / "memory/handoff-inbox").glob("*.md"))
    assert not list((scope / activity.ROOT / "runs").glob("*/review.json"))


def test_patch_bytes_survive_windows_default_newlines(scope, monkeypatch):
    from pathlib import Path
    from brigade import runguard, verify_trial
    from tests.test_lesson_review import proposal

    proposal(scope)
    card = scope / "memory/cards/atomic-publication.md"
    card.parent.mkdir(parents=True)
    card.write_bytes(b"Publication uses atomic replacement.\n")
    patch = scope / ".brigade/changes.patch"
    write = Path.write_text

    def windows_write(path, text, **kwargs):
        kwargs.setdefault("newline", "\r\n")
        return write(path, text, **kwargs)

    monkeypatch.setattr(Path, "write_text", windows_write)
    runguard.collect_changes_patch(scope, patch)
    assert b"\r\n" not in patch.read_bytes()
    import subprocess

    baseline = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=scope, text=True).strip()
    assert verify_trial._historical_subject_hash(
        scope, baseline, patch.read_bytes(), "memory/cards/atomic-publication.md"
    ) == verify_trial.subject_hash_for_path(scope, "memory/cards/atomic-publication.md")


@pytest.mark.parametrize("field", ["content", "candidate", "observations", "receipt", "sequence"])
def test_recovery_refuses_modified_prepared_publication(scope, monkeypatch, field):
    from tests.test_lesson_review import proposal

    event(scope)
    event(scope)
    candidate = proposal(scope)
    seat(monkeypatch, candidate)
    publish = review._publish
    monkeypatch.setattr(review, "_publish", lambda *_: (_ for _ in ()).throw(OSError("injected crash")))
    assert review.run(scope, now=111)["status"] == "retry"
    state = activity.load(scope)
    pending = state["pending"]
    if field == "content":
        pending["lesson"]["content"] = "Invented replacement lesson."
    elif field == "candidate":
        pending["lesson"]["admission_inputs"]["candidate"]["quote"] = "Invented quote."
    elif field == "observations":
        pending["lesson"]["admission_inputs"]["events"] = []
    elif field == "receipt":
        pending["lesson"]["admission_inputs"]["candidate"]["failure_evidence"] = (
            ".brigade/work/verify-runs/other/receipt.json"
        )
    else:
        pending["sequence"] = 1
    activity.save(scope, state)
    monkeypatch.setattr(review, "_publish", publish)
    calls = seat(monkeypatch)
    assert review.run(scope, now=500)["status"] == "rejected"
    assert not calls
    assert "pending" not in activity.load(scope)
    assert not list((scope / "memory/handoff-inbox").glob("*.md"))


def test_default_anchor_gate_refuses_authentic_pair(scope, monkeypatch):
    from brigade import lesson_admission as admission
    from tests.test_lesson_review import proposal

    candidate = proposal(scope)
    event(scope)
    event(scope)
    assert DEFAULT_AUTOMATIC_LESSON_ADMISSION is False
    monkeypatch.setattr(admission, "AUTOMATIC_LESSON_ADMISSION", False)
    seat(monkeypatch, candidate)
    result = review.run(scope, now=111)
    assert result["status"] == "lesson_admission_awaits_hub_anchor (#2620)"
    assert not list((scope / "memory/handoff-inbox").glob("*.md"))
    assert not list((scope / activity.ROOT / "runs").glob("*/review.json"))


def test_final_hook_save_must_retain_correction_before_ack(scope):
    for _ in range(activity.MAX_EVENTS - 1):
        event(scope)
    activity.record_event(scope, "PostToolUse", {"tool_output": "Read source."}, now=100)
    activity.record_event(scope, "UserPromptSubmit", {"prompt": "Keep publication atomic."}, now=101)
    event(scope, now=102)
    saved = activity.load(scope)
    assert not any(e["kind"] == "UserPromptSubmit" for e in saved["events"])
    assert activity._read_prompts(activity.prompt_path(scope))


def test_hooks_record_nothing_while_admission_is_off(tmp_path):
    (tmp_path / ".brigade").mkdir()
    (tmp_path / ".brigade/lesson-review.json").write_text(
        json.dumps({"threshold": 2, "idle_seconds": 10, "seat": "fake"})
    )
    assert activity.record_event(tmp_path, "UserPromptSubmit", {"prompt": "Keep publication atomic."}, now=1) == (
        "lesson_admission_awaits_hub_anchor"
    )
    assert (
        activity.record_event(tmp_path, "PostToolUse", {"tool_output": "ok"}, now=2)
        == "lesson_admission_awaits_hub_anchor"
    )
    assert not activity._read_prompts(activity.prompt_path(tmp_path))
    assert not (tmp_path / activity.ROOT / "state.json").exists()
