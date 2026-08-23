"""Claude hook output contract (issue #735)."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from brigade.claude_hooks import envelope, runtime
from brigade.claude_hooks.package import MANAGED_EVENTS, PACKAGE_REF
from brigade.install import install_selection
from brigade.selection import Selection


def _wired_claude(tmp_path: Path) -> Path:
    target = tmp_path / "repo"
    selection = Selection(depth="repo", harnesses=["claude"], owner="claude", includes=[])
    assert install_selection(target, selection) == 0
    return target


def _payload(target: Path, event: str, *, session_id: str = "session-1", **extra):
    return {
        "session_id": session_id,
        "cwd": str(target),
        "hook_event_name": event,
        **extra,
    }


@pytest.mark.parametrize("event", MANAGED_EVENTS)
def test_empty_envelope_is_pinned_per_event(event: str):
    assert envelope.empty_envelope(event) == {}


@pytest.mark.parametrize("event", MANAGED_EVENTS)
def test_hook_run_emits_empty_envelope_outside_wired_repo(tmp_path: Path, event: str, capsys):
    payload = _payload(tmp_path, event)
    capsys.readouterr()
    assert runtime.hook_run(event=event, package=PACKAGE_REF, stdin_text=json.dumps(payload)) == 0
    assert json.loads(capsys.readouterr().out) == envelope.empty_envelope(event)


def test_hook_run_malformed_stdin_emits_doctor_pointer(capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    capsys.readouterr()
    assert runtime.hook_run(event="SessionStart", package=PACKAGE_REF, stdin_text="{not-json") == 0
    out = json.loads(capsys.readouterr().out)
    assert out == envelope.degraded_envelope("SessionStart")
    assert envelope.DOCTOR_POINTER in out["hookSpecificOutput"]["additionalContext"]
    log = (tmp_path / ".brigade" / "work" / "claude-hooks" / "hook.log").read_text(encoding="utf-8")
    assert "malformed hook stdin" in log


def test_induced_store_failure_emits_doctor_pointer_and_log(tmp_path: Path, monkeypatch, capsys):
    target = _wired_claude(tmp_path)

    def boom(_target: Path) -> str:
        raise runtime.HookDegraded("store unreadable")

    monkeypatch.setattr(runtime, "_run_brief", boom)
    payload = _payload(target, "SessionStart")
    capsys.readouterr()
    assert runtime.hook_run(event="SessionStart", package=PACKAGE_REF, stdin_text=json.dumps(payload)) == 0
    captured = capsys.readouterr()
    out = json.loads(captured.out)
    assert out == envelope.degraded_envelope("SessionStart")
    assert captured.err == ""
    log = envelope.log_path(target).read_text(encoding="utf-8")
    assert "store unreadable" in log


def test_injection_begins_with_anti_truncation_and_persists_full_copy(tmp_path: Path, monkeypatch):
    target = _wired_claude(tmp_path)
    monkeypatch.setattr(runtime, "_run_brief", lambda _repo: "alpha\nbeta\ngamma")
    result = runtime.handle_payload("SessionStart", _payload(target, "SessionStart"))
    context = result["hookSpecificOutput"]["additionalContext"]
    assert context.startswith("[Brigade] If this context appears truncated")
    assert "cat .brigade/work/claude-hooks/injections/" in context
    assert "alpha" in context and "beta" in context and "gamma" in context
    persisted = list(envelope.injections_root(target).glob("*.txt"))
    assert len(persisted) == 1
    assert persisted[0].read_text(encoding="utf-8") == "alpha\nbeta\ngamma\n"
    assert stat.S_IMODE(persisted[0].stat().st_mode) == 0o600


def test_item_cap_emits_elision_banner_naming_items_cap(tmp_path: Path):
    target = _wired_claude(tmp_path)
    records = [f"record-{index}" for index in range(6)]
    rendered = envelope.render_records(
        records,
        target=target,
        session_id="cap-items",
        event="SessionStart",
        max_chars=4000,
        max_items=2,
    )
    assert "Elided 4 of 6 records (items cap)" in rendered.text
    assert rendered.cap_fired == "items"
    assert rendered.selected_count == 2
    assert "record-0" in rendered.text and "record-1" in rendered.text
    assert "record-5" not in rendered.text
    assert "record-5" in rendered.persisted_path.read_text(encoding="utf-8")


def test_char_cap_emits_elision_banner_naming_chars_cap(tmp_path: Path):
    target = _wired_claude(tmp_path)
    records = [
        "one-short-record-" + ("a" * 200),
        "two-short-record-" + ("b" * 200),
        "three-short-record-" + ("c" * 200),
    ]
    rendered = envelope.render_records(
        records,
        target=target,
        session_id="cap-chars",
        event="SessionStart",
        max_chars=600,
        max_items=32,
    )
    assert rendered.cap_fired == "chars"
    assert "chars cap" in rendered.text
    assert rendered.selected_count >= 1
    assert rendered.dropped_count >= 1
    assert rendered.text.startswith("[Brigade] If this context appears truncated")
    assert len(rendered.text) <= 600


def test_single_oversized_record_emits_metadata_stub(tmp_path: Path):
    target = _wired_claude(tmp_path)
    huge = "文" * 5000  # multibyte code points at the boundary
    rendered = envelope.render_records(
        [huge],
        target=target,
        session_id="cap-oversize",
        event="SessionStart",
        max_chars=400,
        max_items=8,
    )
    assert rendered.cap_fired == "oversized"
    assert "Record exceeds injection budget (5000 code points)" in rendered.text
    assert "full copy: cat " in rendered.text
    assert huge not in rendered.text
    assert huge in rendered.persisted_path.read_text(encoding="utf-8")
    assert len(rendered.text) <= 400


def test_record_larger_than_entire_budget_stays_bounded(tmp_path: Path):
    target = _wired_claude(tmp_path)
    rendered = envelope.render_records(
        ["x" * 10_000],
        target=target,
        session_id="cap-huge",
        event="PostToolUseFailure",
        max_chars=80,
        max_items=1,
    )
    assert len(rendered.text) <= 80
    assert rendered.cap_fired == "oversized"
    assert "full copy" in rendered.text


def test_multibyte_boundary_counts_code_points_not_bytes(tmp_path: Path):
    target = _wired_claude(tmp_path)
    records = ["你", "好", "吗", "啊"]
    rendered = envelope.render_records(
        records,
        target=target,
        session_id="cap-unicode",
        event="SessionStart",
        max_chars=4000,
        max_items=2,
    )
    assert rendered.selected_count == 2
    assert rendered.cap_fired == "items"
    assert "Elided 2 of 4 records (items cap)" in rendered.text
    assert len("你好".encode("utf-8")) > len("你好")


def test_persisted_copy_is_atomic_private_and_cleaned(tmp_path: Path):
    target = _wired_claude(tmp_path)
    for index in range(envelope.MAX_INJECTION_FILES + 5):
        envelope.render_records(
            [f"payload-{index}"],
            target=target,
            session_id=f"session-{index}",
            event="SessionStart",
            max_chars=2000,
            max_items=8,
        )
    files = list(envelope.injections_root(target).glob("*.txt"))
    assert len(files) <= envelope.MAX_INJECTION_FILES
    for path in files:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_hook_timeout_covers_whole_operation(tmp_path: Path, monkeypatch, capsys):
    target = _wired_claude(tmp_path)
    monkeypatch.setattr(envelope, "HOOK_TIMEOUT_SECONDS", 0.01)

    def hang(_event: str, _payload: dict, **_kwargs) -> dict | None:
        import time

        time.sleep(1)
        return None

    monkeypatch.setattr(runtime, "handle_payload", hang)
    capsys.readouterr()
    assert (
        runtime.hook_run(
            event="PreToolUse",
            package=PACKAGE_REF,
            stdin_text=json.dumps(_payload(target, "PreToolUse")),
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == envelope.degraded_envelope("PreToolUse")
    assert "timed out" in envelope.log_path(target).read_text(encoding="utf-8")


def test_preamble_and_banner_count_against_budget(tmp_path: Path):
    target = _wired_claude(tmp_path)
    records = [f"r{i}" for i in range(10)]
    rendered = envelope.render_records(
        records,
        target=target,
        session_id="budget",
        event="SessionStart",
        max_chars=260,
        max_items=10,
    )
    assert len(rendered.text) <= 260
    assert rendered.text.startswith("[Brigade] If this context appears truncated")
    if rendered.dropped_count:
        assert "Elided" in rendered.text


def test_env_caps_are_honored(monkeypatch):
    monkeypatch.setenv("BRIGADE_HOOK_MAX_CHARS", "500")
    monkeypatch.setenv("BRIGADE_HOOK_MAX_ITEMS", "3")
    assert envelope.resolve_caps() == (500, 3)


def test_path_redaction_strips_home_prefix():
    home = str(Path.home().expanduser().resolve())
    assert envelope.redact_paths(f"{home}/secret/log") == "~/secret/log"
