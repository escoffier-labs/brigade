"""Lesson hooks stay inert while automatic admission is unavailable."""

from brigade import lesson_activity as activity
from brigade import lesson_admission as admission
from brigade import lesson_review as review


def test_hook_does_not_write_prompt_spool_when_admission_is_off(tmp_path, monkeypatch):
    monkeypatch.setattr(admission, "AUTOMATIC_LESSON_ADMISSION", False)
    spool = activity.prompt_path(tmp_path)
    writes = []
    monkeypatch.setattr(activity.io, "write", lambda *args, **kwargs: writes.append((args, kwargs)))

    status = activity.record_event(tmp_path, "UserPromptSubmit", {"prompt": "Remember this correction."})

    assert status == "lesson_admission_awaits_hub_anchor"
    assert writes == []
    assert not spool.exists()


def test_review_ignores_stale_state_while_admission_is_off(tmp_path, monkeypatch):
    monkeypatch.setattr(admission, "AUTOMATIC_LESSON_ADMISSION", True)
    activity.save(
        tmp_path,
        {
            "sequence": 1,
            "reviewed": 0,
            "last_activity": 1,
            "events": [{"id": 1}],
            "calls": 1,
            "pending": {"prepared": True},
            "attempt": {"count": 1},
        },
    )
    monkeypatch.setattr(admission, "AUTOMATIC_LESSON_ADMISSION", False)

    assert review.run(tmp_path, now=1000) == {"status": admission.ANCHOR_STATUS}
    state = activity.load(tmp_path)
    assert "pending" not in state
    assert "attempt" not in state
    before = (tmp_path / activity.ROOT / "state.json").read_bytes()
    writes = []
    monkeypatch.setattr(activity.io, "write", lambda *args, **kwargs: writes.append((args, kwargs)))
    assert review.run(tmp_path, now=1001) == {"status": admission.ANCHOR_STATUS}
    assert writes == []
    assert (tmp_path / activity.ROOT / "state.json").read_bytes() == before


def test_hook_still_spools_prompt_when_admission_is_on(tmp_path, monkeypatch):
    monkeypatch.setattr(admission, "AUTOMATIC_LESSON_ADMISSION", True)
    monkeypatch.setattr(activity.receipt_signing, "load_workspace_key", lambda _target: b"key")

    assert activity.record_event(tmp_path, "UserPromptSubmit", {"prompt": "Remember this correction."}) is None
    assert [item["prompt"] for item in activity._read_prompts(activity.prompt_path(tmp_path))] == [
        "Remember this correction."
    ]
