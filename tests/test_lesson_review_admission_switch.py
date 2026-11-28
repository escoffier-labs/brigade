"""Lesson hooks stay inert while automatic admission is unavailable."""

import pytest

from tests._home import set_home

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
    assert writes == [((activity.prompt_path(tmp_path), b"null\n"), {})]
    assert (tmp_path / activity.ROOT / "state.json").read_bytes() == before


def test_hook_still_spools_prompt_when_admission_is_on(tmp_path, monkeypatch):
    monkeypatch.setattr(admission, "AUTOMATIC_LESSON_ADMISSION", True)
    monkeypatch.setattr(activity.receipt_signing, "load_workspace_key", lambda _target: b"key")

    assert activity.record_event(tmp_path, "UserPromptSubmit", {"prompt": "Remember this correction."}) is None
    assert [item["prompt"] for item in activity._read_prompts(activity.prompt_path(tmp_path))] == [
        "Remember this correction."
    ]


def test_review_erases_leftover_prompt_when_admission_is_off(tmp_path, monkeypatch):
    set_home(monkeypatch, tmp_path / "isolated")
    monkeypatch.setattr(admission, "AUTOMATIC_LESSON_ADMISSION", False)
    spool = activity.prompt_path(tmp_path)
    activity.io.write(spool, b'[{"prompt":"Synthetic correction for a test.","at":1}]')
    monkeypatch.setattr(activity, "_read_prompts", lambda _path: pytest.fail("discard must not read prompt text"))

    assert review.run(tmp_path) == {"status": admission.ANCHOR_STATUS}

    assert spool.read_bytes() == b"null\n"
    assert activity.load(tmp_path)["events"] == []


@pytest.mark.parametrize("operation", ["discard_prompts", "held_file_lock", "load", "save"])
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (review.InboxLockTimeout("synthetic lock timeout"), {"status": "coalesced"}),
        (ValueError("synthetic bad state"), {"status": "rejected", "reason": "synthetic bad state"}),
        (OSError("synthetic IO failure"), {"status": "retry"}),
        (RuntimeError("synthetic runtime failure"), {"status": "retry"}),
    ],
)
def test_admission_off_preserves_failure_mapping(tmp_path, monkeypatch, operation, error, expected):
    set_home(monkeypatch, tmp_path / "isolated")
    monkeypatch.setattr(admission, "AUTOMATIC_LESSON_ADMISSION", False)
    activity.save(tmp_path, {**activity.load(tmp_path), "pending": {"prepared": True}})

    def fail(*args, **kwargs):
        raise error

    owner = review if operation == "held_file_lock" else activity
    monkeypatch.setattr(owner, operation, fail)

    assert review.run(tmp_path) == expected
