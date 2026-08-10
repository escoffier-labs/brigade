"""Claude compaction restore marker (issue #736)."""

from __future__ import annotations

import json
import stat
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from brigade.claude_hooks import compaction_marker, envelope, runtime
from brigade.claude_hooks.package import MANAGED_EVENTS, PACKAGE_REF
from brigade.install import install_selection
from brigade.selection import Selection


def _wired_claude(tmp_path: Path) -> Path:
    target = tmp_path / "repo"
    selection = Selection(depth="repo", harnesses=["claude"], owner="claude", includes=[])
    assert install_selection(target, selection) == 0
    return target


def _cache_env(tmp_path: Path) -> dict[str, str]:
    cache = tmp_path / "xdg-cache"
    cache.mkdir(parents=True, exist_ok=True)
    return {"XDG_CACHE_HOME": str(cache), "HOME": str(tmp_path / "home")}


def _payload(target: Path, event: str, *, session_id: str = "session-1", **extra):
    return {
        "session_id": session_id,
        "cwd": str(target),
        "hook_event_name": event,
        **extra,
    }


@pytest.fixture
def marker_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    env = _cache_env(tmp_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    (tmp_path / "home").mkdir(parents=True, exist_ok=True)
    return env


def test_managed_events_include_compaction_hooks():
    assert "PreCompact" in MANAGED_EVENTS
    assert "UserPromptSubmit" in MANAGED_EVENTS


def test_marker_lives_in_cache_not_repo(tmp_path: Path, marker_env: dict[str, str]):
    target = _wired_claude(tmp_path)
    path = compaction_marker.write_pending("sess-a", target, env=marker_env)
    assert path.is_file()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert ".brigade" not in path.parts or path.parts[path.parts.index(".brigade") - 1] != "repo"
    assert str(path).startswith(str(Path(marker_env["XDG_CACHE_HOME"])))
    assert not any(target.rglob("pending.json"))


def test_marker_keys_separate_workspaces_sharing_session_id(tmp_path: Path, marker_env: dict[str, str]):
    left = _wired_claude(tmp_path / "left")
    right_root = tmp_path / "right"
    right = _wired_claude(right_root)
    session_id = "shared-session"
    compaction_marker.write_pending(session_id, left, env=marker_env)
    compaction_marker.write_pending(session_id, right, env=marker_env)
    assert compaction_marker.marker_key(session_id, left) != compaction_marker.marker_key(session_id, right)
    assert compaction_marker.marker_present(compaction_marker.marker_key(session_id, left), env=marker_env)
    assert compaction_marker.marker_present(compaction_marker.marker_key(session_id, right), env=marker_env)


def test_concurrent_claims_only_one_winner(tmp_path: Path, marker_env: dict[str, str]):
    target = _wired_claude(tmp_path)
    compaction_marker.write_pending("race", target, env=marker_env)

    def claim(_index: int):
        return compaction_marker.try_claim("race", target, env=marker_env)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(claim, range(8)))
    winners = [item for item in results if item is not None]
    assert len(winners) == 1
    pending = compaction_marker.pending_path(compaction_marker.marker_key("race", target), env=marker_env)
    assert not pending.exists()
    assert winners[0].path.is_file()


def test_stale_claim_returns_to_pending(tmp_path: Path, marker_env: dict[str, str], monkeypatch):
    target = _wired_claude(tmp_path)
    compaction_marker.write_pending("stale", target, env=marker_env)
    claim = compaction_marker.try_claim("stale", target, env=marker_env)
    assert claim is not None
    # Backdate claimed_at beyond the stale window.
    record = dict(claim.record)
    record["claimed_at"] = "2000-01-01T00:00:00+00:00"
    claim.path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    monkeypatch.setattr(compaction_marker, "STALE_CLAIM_SECONDS", 1)
    time.sleep(0.01)
    recovered = compaction_marker.try_claim("stale", target, env=marker_env)
    assert recovered is not None
    assert recovered.path != claim.path
    assert not claim.path.exists()


def test_cache_directory_symlink_is_rejected(tmp_path: Path, marker_env: dict[str, str]):
    target = _wired_claude(tmp_path)
    root = compaction_marker.markers_root(env=marker_env)
    root.parent.mkdir(parents=True, exist_ok=True)
    real = tmp_path / "elsewhere"
    real.mkdir()
    if root.exists():
        root.rmdir()
    root.symlink_to(real)
    with pytest.raises(OSError, match="symlink"):
        compaction_marker.write_pending("sym", target, env=marker_env)


def test_precompact_writes_marker_and_empty_envelope(tmp_path: Path, marker_env: dict[str, str], capsys):
    target = _wired_claude(tmp_path)
    payload = _payload(target, "PreCompact", trigger="auto")
    assert runtime.handle_payload("PreCompact", payload) is None
    key = compaction_marker.marker_key("session-1", target)
    assert compaction_marker.marker_present(key)
    capsys.readouterr()
    assert runtime.hook_run(event="PreCompact", package=PACKAGE_REF, stdin_text=json.dumps(payload)) == 0
    assert json.loads(capsys.readouterr().out) == envelope.empty_envelope("PreCompact")


def test_user_prompt_hot_path_skips_brief_when_marker_absent(tmp_path: Path, marker_env: dict[str, str], monkeypatch):
    target = _wired_claude(tmp_path)
    calls: list[Path] = []

    def boom(repo: Path) -> str:
        calls.append(repo)
        raise AssertionError("brief must not run on hot path")

    monkeypatch.setattr(runtime, "_run_brief", boom)
    assert runtime.handle_payload("UserPromptSubmit", _payload(target, "UserPromptSubmit")) is None
    assert calls == []


def test_user_prompt_restores_brief_once_then_clears_marker(
    tmp_path: Path, marker_env: dict[str, str], monkeypatch, capsys
):
    target = _wired_claude(tmp_path)
    monkeypatch.setattr(runtime, "_run_brief", lambda repo: "rule-one\nrule-two")
    assert runtime.handle_payload("PreCompact", _payload(target, "PreCompact", trigger="manual")) is None
    payload = _payload(target, "UserPromptSubmit")
    capsys.readouterr()
    assert runtime.hook_run(event="UserPromptSubmit", package=PACKAGE_REF, stdin_text=json.dumps(payload)) == 0
    out = json.loads(capsys.readouterr().out)
    context = out["hookSpecificOutput"]["additionalContext"]
    assert context.startswith("[Brigade] If this context appears truncated")
    assert compaction_marker.RESTORE_PREFIX in context
    assert "rule-one" in context and "rule-two" in context
    assert compaction_marker.CLAIM_KEY not in out
    key = compaction_marker.marker_key("session-1", target)
    assert not compaction_marker.marker_present(key)
    # Second submit is a no-op hot path.
    assert runtime.handle_payload("UserPromptSubmit", payload) is None


def test_injection_failure_leaves_marker_for_retry(tmp_path: Path, marker_env: dict[str, str], monkeypatch, capsys):
    target = _wired_claude(tmp_path)
    compaction_marker.write_pending("session-1", target)

    def boom(_repo: Path) -> str:
        raise runtime.HookDegraded("store unreadable")

    monkeypatch.setattr(runtime, "_run_brief", boom)
    payload = _payload(target, "UserPromptSubmit")
    capsys.readouterr()
    assert runtime.hook_run(event="UserPromptSubmit", package=PACKAGE_REF, stdin_text=json.dumps(payload)) == 0
    assert json.loads(capsys.readouterr().out) == envelope.degraded_envelope("UserPromptSubmit")
    key = compaction_marker.marker_key("session-1", target)
    assert compaction_marker.marker_present(key)
    pending = compaction_marker.pending_path(key)
    assert pending.is_file()


def test_emit_failure_returns_claim_to_pending(tmp_path: Path, marker_env: dict[str, str], monkeypatch, capsys):
    target = _wired_claude(tmp_path)
    monkeypatch.setattr(runtime, "_run_brief", lambda repo: "brief-body")
    compaction_marker.write_pending("session-1", target)

    def boom(_payload: dict) -> None:
        raise OSError("stdout broken")

    monkeypatch.setattr(envelope, "emit_stdout", boom)
    payload = _payload(target, "UserPromptSubmit")
    capsys.readouterr()
    assert runtime.hook_run(event="UserPromptSubmit", package=PACKAGE_REF, stdin_text=json.dumps(payload)) == 0
    key = compaction_marker.marker_key("session-1", target)
    assert compaction_marker.pending_path(key).is_file()


def test_session_start_clears_stale_markers(tmp_path: Path, marker_env: dict[str, str], monkeypatch):
    target = _wired_claude(tmp_path)
    monkeypatch.setattr(runtime, "_run_brief", lambda repo: "brief")
    compaction_marker.write_pending("session-1", target)
    # Already briefed: silent, but still clears markers.
    runtime.handle_payload("SessionStart", _payload(target, "SessionStart", source="startup"))
    runtime.handle_payload("SessionStart", _payload(target, "SessionStart", source="startup"))
    key = compaction_marker.marker_key("session-1", target)
    assert not compaction_marker.marker_present(key)


def test_session_start_compact_reinjects_without_marker(tmp_path: Path, marker_env: dict[str, str], monkeypatch):
    target = _wired_claude(tmp_path)
    monkeypatch.setattr(runtime, "_run_brief", lambda repo: "compact-brief")
    first = runtime.handle_payload("SessionStart", _payload(target, "SessionStart", source="startup"))
    assert "compact-brief" in first["hookSpecificOutput"]["additionalContext"]
    # Leave a marker as if PreCompact raced ahead; compact source clears it.
    compaction_marker.write_pending("session-1", target)
    restored = runtime.handle_payload(
        "SessionStart",
        _payload(target, "SessionStart", source="compact"),
    )
    context = restored["hookSpecificOutput"]["additionalContext"]
    assert compaction_marker.RESTORE_PREFIX in context
    assert "compact-brief" in context
    assert not compaction_marker.marker_present(compaction_marker.marker_key("session-1", target))
    # Marker path stays silent after Claude's preferred compact reinjection.
    assert runtime.handle_payload("UserPromptSubmit", _payload(target, "UserPromptSubmit")) is None


def test_two_prompt_events_do_not_double_inject(tmp_path: Path, marker_env: dict[str, str], monkeypatch):
    target = _wired_claude(tmp_path)
    calls: list[str] = []

    def brief(repo: Path) -> str:
        calls.append(str(repo))
        return "once-only"

    monkeypatch.setattr(runtime, "_run_brief", brief)
    compaction_marker.write_pending("session-1", target)
    payload = _payload(target, "UserPromptSubmit")

    def run_once(_index: int) -> dict:
        # Use handle_payload + manual claim completion to race at claim time.
        return runtime.handle_payload("UserPromptSubmit", payload)  # type: ignore[return-value]

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(run_once, range(4)))
    injected = [item for item in results if item is not None]
    assert len(injected) == 1
    assert len(calls) == 1
    # Complete the winning claim the way hook_run would.
    cleaned, claim_path = runtime._strip_claim(injected[0])
    assert claim_path is not None
    assert compaction_marker.CLAIM_KEY not in cleaned
    compaction_marker.complete_claim_path(claim_path)
    assert runtime.handle_payload("UserPromptSubmit", payload) is None
