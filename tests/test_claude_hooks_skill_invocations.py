"""Skill-tool invocations and ``--capture auto`` suggestions in the Claude hooks (#2589)."""

from __future__ import annotations

from pathlib import Path

from brigade import outcome_attribution
from brigade.claude_hooks import package, runtime, skill_invocations
from tests.test_claude_hooks_runtime import _payload, _wired_claude


def _install_skill(target: Path, skill_id: str) -> None:
    skill = target / ".claude" / "skills" / skill_id
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text(f"# {skill_id}\n")


def _env() -> dict[str, str]:
    return {"BRIGADE_CLAUDE_SESSION": runtime._session_fingerprint("session-1")}


def _invoke(target: Path, skill: object, **extra) -> None:
    runtime.handle_payload(
        "PostToolUse",
        _payload(target, "PostToolUse", tool_name="Skill", tool_input={"skill": skill}, **extra),
    )


def test_post_tool_use_matcher_covers_the_skill_tool():
    project = package.managed_groups()["PostToolUse"]
    user = package.managed_user_groups(Path("/opt/brigade-hook.sh"))["PostToolUse"]
    for groups in (project, user):
        assert "Skill" in groups[0]["matcher"].split("|")


def test_skill_tool_invocation_reaches_auto_capture_resolution(tmp_path: Path):
    target = _wired_claude(tmp_path)
    _install_skill(target, "taste")
    _invoke(target, "taste")
    assert outcome_attribution.resolve_observed_skill_ids(target, _env()) == ["taste"]
    state = runtime.read_session_state(target, "session-1")
    assert state is not None
    assert state[skill_invocations.STATE_KEY] == ["taste"]
    assert "exercised_artifact_id" not in state  # veto-only: never a caller-typed artifact


def test_plugin_prefixed_skill_name_resolves_to_the_installed_id(tmp_path: Path):
    target = _wired_claude(tmp_path)
    _install_skill(target, "taste")
    _invoke(target, "superpowers:taste")
    assert outcome_attribution.resolve_observed_skill_ids(target, _env()) == ["taste"]


def test_invocations_survive_later_hook_writes_and_dedupe(tmp_path: Path):
    target = _wired_claude(tmp_path)
    _install_skill(target, "taste")
    _install_skill(target, "scout")
    _invoke(target, "taste")
    runtime.handle_payload(
        "PostToolUse",
        _payload(
            target,
            "PostToolUse",
            tool_name="Read",
            tool_input={"file_path": str(target / ".claude" / "skills" / "scout" / "SKILL.md")},
        ),
    )
    _invoke(target, "taste")
    state = runtime.read_session_state(target, "session-1")
    assert state is not None
    assert state[skill_invocations.STATE_KEY] == ["taste"]
    assert outcome_attribution.resolve_observed_skill_ids(target, _env()) == ["scout", "taste"]


def test_malformed_or_failed_skill_calls_record_nothing(tmp_path: Path):
    target = _wired_claude(tmp_path)
    for bad in (None, 7, "", "  ", "a/b", "../x", "x" * 300):
        _invoke(target, bad)
    _invoke(target, "taste", tool_response={"is_error": True})
    state = runtime.read_session_state(target, "session-1")
    assert state is None or not state.get(skill_invocations.STATE_KEY)


def test_unsupported_structure_hint_uses_auto_capture(tmp_path: Path):
    target = _wired_claude(tmp_path)
    result = runtime.handle_payload(
        "PreToolUse",
        _payload(target, "PreToolUse", tool_name="Bash", tool_input={"command": "cd src && pytest -q"}),
    )
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "Split shell grouping" in reason
    assert "--capture auto" in reason
    assert "--capture brigade-work" not in reason


def test_unsupported_structure_hint_keeps_an_exercised_artifact(tmp_path: Path):
    target = _wired_claude(tmp_path)
    _install_skill(target, "taste")
    runtime.handle_payload(
        "PostToolUse",
        _payload(
            target,
            "PostToolUse",
            tool_name="Read",
            tool_input={"file_path": str(target / ".claude" / "skills" / "taste" / "SKILL.md")},
        ),
    )
    result = runtime.handle_payload(
        "PreToolUse",
        _payload(target, "PreToolUse", tool_name="Bash", tool_input={"command": "cd src && pytest -q"}),
    )
    assert "--capture taste" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_failure_followup_uses_auto_capture_without_an_explicit_or_exercised_id(tmp_path: Path):
    target = _wired_claude(tmp_path)
    result = runtime.handle_payload(
        "PostToolUseFailure",
        _payload(
            target,
            "PostToolUseFailure",
            tool_name="Bash",
            tool_input={"command": 'brigade work verify run --target . --command "pytest"'},
            error="exit 1",
        ),
    )
    context = result["hookSpecificOutput"]["additionalContext"]
    assert "--capture auto" in context
    assert "brigade-work" not in context
