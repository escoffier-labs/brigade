"""Artifact attribution survives interleaved Read/Bash hook snapshots."""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

import pytest

from brigade import cli, inbox_lock
from brigade.claude_hooks import runtime
from brigade.install import install_selection
from brigade.selection import Selection


SESSION = "artifact-session"


def event(target: Path, name: str, **fields):
    return runtime.handle_payload(name, {"session_id": SESSION, "cwd": str(target), **fields})


@pytest.fixture
def target(tmp_path: Path, monkeypatch) -> Path:
    target = tmp_path / "repo"
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    assert install_selection(target, Selection(depth="repo", harnesses=["claude"], owner="claude", includes=[])) == 0
    monkeypatch.setattr(runtime.heartbeat, "on_event", lambda *args: None)
    monkeypatch.setattr(runtime.presence, "emit_presence", lambda *args: None)
    monkeypatch.setattr(runtime, "_run_recall", lambda *args: "")
    monkeypatch.setattr(runtime, "_run_brief", lambda *args: "work brief")
    event(target, "SessionStart", source="startup")
    state = runtime.read_session_state(target, SESSION)
    assert state is not None
    state["repo_fingerprint"] = "recorded-baseline"
    runtime.write_session_state(target, SESSION, state)
    skill = target / ".claude/skills/taste/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("# taste\n")
    return target


def read_skill(target: Path) -> None:
    event(
        target, "PostToolUse", tool_name="Read", tool_input={"file_path": str(target / ".claude/skills/taste/SKILL.md")}
    )
    state = runtime.read_session_state(target, SESSION)
    assert state is not None
    assert (state["exercised_artifact_id"], state["exercised_artifact_kind"]) == ("taste", "skill")


def seed_artifact(target: Path, artifact_id: str | None, *, kind: str = "runbook") -> dict:
    state = runtime.read_session_state(target, SESSION)
    assert state is not None
    if artifact_id is not None:
        state["exercised_artifact_id"] = artifact_id
        state["exercised_artifact_kind"] = kind
    runtime.write_session_state(target, SESSION, state)
    return state


@pytest.mark.parametrize("stale_artifact", [None, "brigade-work", "prior-skill"])
def test_read_during_bash_preserves_guidance_and_receipt_attribution(target, monkeypatch, stale_artifact):
    seed_artifact(target, stale_artifact)
    interleaved = False

    def fingerprint(_target):
        nonlocal interleaved
        assert not interleaved
        interleaved = True
        read_skill(target)
        return "after-read"

    with monkeypatch.context() as boundary:
        boundary.setattr(runtime, "repo_worktree_fingerprint", fingerprint)
        result = event(target, "PreToolUse", tool_name="Bash", tool_input={"command": "python -m pytest -q"})
    assert interleaved
    state = runtime.read_session_state(target, SESSION)
    assert state is not None
    assert (state.get("exercised_artifact_id"), state.get("exercised_artifact_kind")) == ("taste", "skill")
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    tokens = shlex.split(reason.split("Use: ", 1)[1])
    capture = tokens[tokens.index("--capture") + 1]
    assert capture == "taste"
    assert runtime.exercised_artifact_for_fingerprint(target, state["session_fingerprint"]) == "taste"

    # Exercise the real receipt/capture path using the hook's selected artifact,
    # with a tiny verifier instead of starting another pytest suite.
    monkeypatch.setenv(runtime.CLAUDE_SESSION_ENV, state["session_fingerprint"])
    assert (
        cli.main(
            [
                "work",
                "verify",
                "run",
                "--target",
                str(target),
                "--argv-json",
                json.dumps([sys.executable, "-c", "pass"]),
                "--capture",
                capture,
            ]
        )
        == 0
    )
    receipts = list((target / ".brigade/work/verify-runs").glob("*/receipt.json"))
    assert len(receipts) == 1
    receipt = json.loads(receipts[0].read_text())
    assert receipt["outcome_capture"] == {"artifact_id": "taste", "artifact_kind": "skill"}
    assert receipt["harness_session"]["fingerprint"] == state["session_fingerprint"]


@pytest.mark.parametrize(
    "capture,expected", [("prior-skill", "prior-skill"), ("new-skill", "new-skill"), ("brigade-work", "taste")]
)
def test_explicit_capture_intent_survives_concurrent_read(target, monkeypatch, capture, expected):
    seed_artifact(target, "prior-skill", kind="skill")
    original = runtime._write_session_state_preserving_latch
    interleaved = False

    def write_after_read(repo, session_id, state, **kwargs):
        nonlocal interleaved
        if kwargs.get("patch_fields") is None and not interleaved:
            interleaved = True
            read_skill(target)
        return original(repo, session_id, state, **kwargs)

    monkeypatch.setattr(runtime, "_write_session_state_preserving_latch", write_after_read)
    event(
        target,
        "PostToolUse",
        tool_name="Bash",
        tool_input={"command": f"brigade work verify run --command true --capture {capture}"},
    )
    assert interleaved
    state = runtime.read_session_state(target, SESSION)
    assert state is not None
    assert (state.get("exercised_artifact_id"), state.get("exercised_artifact_kind")) == (expected, "skill")


@pytest.mark.parametrize("kind", ["skill", "runbook"])
@pytest.mark.parametrize("capture", [None, "taste", " taste "])
def test_full_save_preserves_kind_pair_and_updates_caller(target, kind, capture):
    stale = seed_artifact(target, "prior-skill")
    current = dict(stale, exercised_artifact_id="taste", exercised_artifact_kind=kind)
    runtime.write_session_state(target, SESSION, current)
    assert runtime._write_session_state_preserving_latch(target, SESSION, stale, capture_artifact_id=capture)
    persisted = runtime.read_session_state(target, SESSION)
    assert persisted is not None
    for state in (persisted, stale):
        assert (state["exercised_artifact_id"], state["exercised_artifact_kind"]) == ("taste", kind)


@pytest.mark.parametrize("capture,expected", [("taste", ("taste", "runbook")), ("new-skill", ("new-skill", "skill"))])
def test_routed_capture_preserves_kind_only_for_same_artifact(target, capture, expected):
    seed_artifact(target, "taste", kind="runbook")
    event(
        target,
        "PostToolUse",
        tool_name="Bash",
        tool_input={"command": f"brigade work verify run --command true --capture {capture}"},
    )
    state = runtime.read_session_state(target, SESSION)
    assert state is not None
    assert (state["exercised_artifact_id"], state["exercised_artifact_kind"]) == expected


def test_task_reset_does_not_inherit_artifact_or_accept_old_snapshot(target):
    read_skill(target)
    old = runtime.read_session_state(target, SESSION)
    assert old is not None
    event(target, "SessionStart", source="clear")
    current = runtime.read_session_state(target, SESSION)
    assert current is not None
    assert current["task_epoch"] != old["task_epoch"]
    assert "exercised_artifact_id" not in current
    assert "exercised_artifact_kind" not in current
    before = dict(old)
    assert not runtime._write_session_state_preserving_latch(target, SESSION, old)
    assert old == before
    assert runtime.read_session_state(target, SESSION) == current
    result = event(target, "PreToolUse", tool_name="Bash", tool_input={"command": "python -m pytest -q"})
    assert "--capture brigade-work" in result["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.mark.parametrize("failure", ["lock", "write"])
def test_failed_save_does_not_sync_caller_artifact(target, monkeypatch, failure):
    stale = seed_artifact(target, "prior-skill")
    read_skill(target)
    before = dict(stale)

    def fail(*args, **kwargs):
        if failure == "lock":
            raise inbox_lock.InboxLockTimeout("fixture lock busy")
        raise OSError("fixture write failed")

    if failure == "lock":
        monkeypatch.setattr(runtime, "_session_state_lock", fail)
        assert not runtime._write_session_state_preserving_latch(target, SESSION, stale)
    else:
        monkeypatch.setattr(runtime, "write_session_state", fail)
        with pytest.raises(OSError, match="fixture write failed"):
            runtime._write_session_state_preserving_latch(target, SESSION, stale)
    assert stale == before
