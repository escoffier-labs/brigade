"""Real child regressions for workspace-influenced hook imports."""

import json
import pytest

from brigade.claude_hooks import runtime
from brigade.config import Config, write_config
from brigade.selection import Selection
from tests._home import set_home


@pytest.mark.parametrize("shadow", ["brigade.py", "brigade/__init__.py"])
@pytest.mark.parametrize("worker", [False, True], ids=["session-start", "subprocess-worker"])
def test_session_start_keeps_workspace_code_out_of_children(tmp_path, monkeypatch, shadow, worker):
    target = tmp_path / "workspace"
    target.mkdir()
    write_config(target, Config(version=1, selection=Selection(depth="repo", harnesses=["claude"], owner="claude")))
    sentinel = tmp_path / "executed"
    planted = target / shadow
    planted.parent.mkdir(parents=True, exist_ok=True)
    planted.write_text(f"from pathlib import Path\nPath({str(sentinel)!r}).touch()\n", encoding="utf-8")
    monkeypatch.chdir(target)
    monkeypatch.setenv("PYTHONPATH", str(target))
    monkeypatch.delenv("BRIGADE_FLEET_HUB_URL", raising=False)
    payload = {"cwd": str(target), "session_id": "isolation-session", "hook_event_name": "SessionStart"}
    try:
        if worker:
            outcome = runtime._subprocess_timed_hook_run(
                "SessionStart", json.dumps(payload), timeout=20, pin_target=target
            )
            result = outcome["result"]
        else:
            result = runtime.handle_payload("SessionStart", payload)
    finally:
        assert not sentinel.exists(), "SessionStart child executed workspace Brigade code"
    context = result["hookSpecificOutput"]["additionalContext"]
    assert "work brief:" in context
    assert str(target) in context


def test_subprocess_worker_preserves_tilde_target_pin(tmp_path, monkeypatch):
    from pathlib import Path

    target = tmp_path / "workspace"
    target.mkdir()
    write_config(target, Config(version=1, selection=Selection(depth="repo", harnesses=["claude"], owner="claude")))
    set_home(monkeypatch, tmp_path)
    monkeypatch.delenv("BRIGADE_FLEET_HUB_URL", raising=False)
    payload = {"cwd": str(target), "session_id": "tilde-session", "hook_event_name": "SessionStart"}
    outcome = runtime._subprocess_timed_hook_run(
        "SessionStart", json.dumps(payload), timeout=20, pin_target=Path("~/workspace")
    )
    assert "work brief:" in outcome["result"]["hookSpecificOutput"]["additionalContext"]
