from __future__ import annotations

import json

from brigade import acpx_adapter
from brigade import proc


def _stream(*messages: dict) -> str:
    return "\n".join(json.dumps(message) for message in messages) + "\n"


def _success_stream() -> str:
    return _stream(
        {"jsonrpc": "2.0", "id": "init-1", "result": {"protocolVersion": 1}},
        {
            "jsonrpc": "2.0",
            "id": "req-1",
            "method": "session/prompt",
            "params": {"sessionId": "session-1", "prompt": "hidden"},
        },
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": "session-1",
                "update": {"sessionUpdate": "config_option_update", "modelId": "composer-2.5"},
            },
        },
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": "session-1",
                "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "hello"}},
            },
        },
        {"jsonrpc": "2.0", "id": "req-1", "result": {"stopReason": "end_turn"}},
    )


def test_build_argv_is_bounded_and_permission_specific(tmp_path):
    read = acpx_adapter.build_argv(
        prompt="inspect",
        cwd=tmp_path,
        timeout=120.0,
        model="composer-2.5",
        read_only=True,
        writable_worktree=False,
    )
    assert read == [
        "acpx",
        "--cwd",
        str(tmp_path.resolve()),
        "--format",
        "json",
        "--json-strict",
        "--no-terminal",
        "--timeout",
        "120",
        "--model",
        "composer-2.5",
        "--approve-reads",
        "--non-interactive-permissions",
        "fail",
        "--agent",
        "cursor-agent acp",
        "exec",
        "inspect",
    ]
    write = acpx_adapter.build_argv(
        prompt="edit",
        cwd=tmp_path,
        timeout=12.5,
        model="composer-2.5",
        read_only=False,
        writable_worktree=True,
    )
    assert "--approve-all" in write


def test_build_argv_refuses_writable_non_worktree(tmp_path):
    try:
        acpx_adapter.build_argv(
            prompt="edit",
            cwd=tmp_path,
            timeout=10,
            model="composer-2.5",
            read_only=False,
            writable_worktree=False,
        )
    except ValueError as exc:
        assert "writable worktree" in str(exc)
    else:
        raise AssertionError("expected writable ACP refusal")


def test_run_parses_protocol_and_final_text(monkeypatch, tmp_path):
    monkeypatch.setattr(acpx_adapter.proc, "which", lambda cmd: f"/bin/{cmd}")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv == ["acpx", "--version"]:
            return proc.Result(0, "acpx 0.12.0\n", "")
        return proc.Result(0, _success_stream(), "")

    monkeypatch.setattr(acpx_adapter.proc, "run", fake_run)
    result = acpx_adapter.run_cursor(
        "inspect", cwd=tmp_path, timeout=120, model="composer-2.5", version="0.12.0", read_only=True
    )
    assert result.ok is True
    assert result.text == "hello"
    assert result.transport == "acpx"
    assert result.protocol_version == 1
    assert result.session_id == "session-1"
    assert result.request_id == "req-1"
    assert result.stop_reason == "end_turn"
    assert result.effective_model == "composer-2.5"


def test_run_rejects_in_band_provider_error_with_protocol_evidence(monkeypatch, tmp_path):
    diagnostic = (
        "I will check the provider first.\n"
        "Error: NonRetriableError: Provider Error We're having trouble connecting "
        "to the model provider. This might be temporary - please try again in a moment."
    )
    stream = _success_stream().replace('"text": "hello"', f'"text": {json.dumps(diagnostic)}')
    monkeypatch.setattr(acpx_adapter.proc, "which", lambda cmd: f"/bin/{cmd}")
    outputs = iter([proc.Result(0, "acpx 0.12.0\n", ""), proc.Result(0, stream, "")])
    monkeypatch.setattr(acpx_adapter.proc, "run", lambda argv, **kwargs: next(outputs))

    result = acpx_adapter.run_cursor(
        "inspect", cwd=tmp_path, timeout=120, model="grok-4.5", version="0.12.0", read_only=True
    )

    assert result.ok is False
    assert result.text == diagnostic
    assert result.exit_code == 0
    assert result.stop_reason == "end_turn"
    assert result.session_id == "session-1"
    assert result.request_id == "req-1"
    assert result.failure_phase == "output-validation"
    assert result.failure_kind == "provider-error"
    assert result.detail.startswith("provider returned an error instead of a final result:")


def test_run_accepts_short_substantive_final(monkeypatch, tmp_path):
    stream = _success_stream().replace('"text": "hello"', '"text": "No findings."')
    monkeypatch.setattr(acpx_adapter.proc, "which", lambda cmd: f"/bin/{cmd}")
    outputs = iter([proc.Result(0, "acpx 0.12.0\n", ""), proc.Result(0, stream, "")])
    monkeypatch.setattr(acpx_adapter.proc, "run", lambda argv, **kwargs: next(outputs))

    result = acpx_adapter.run_cursor(
        "inspect", cwd=tmp_path, timeout=120, model="composer-2.5", version="0.12.0", read_only=True
    )

    assert result.ok is True
    assert result.text == "No findings."


def test_run_rejects_non_final_stop_reason_with_partial_text(monkeypatch, tmp_path):
    stream = _success_stream().replace('"stopReason": "end_turn"', '"stopReason": "cancelled"')
    monkeypatch.setattr(acpx_adapter.proc, "which", lambda cmd: f"/bin/{cmd}")
    outputs = iter([proc.Result(0, "acpx 0.12.0\n", ""), proc.Result(0, stream, "")])
    monkeypatch.setattr(acpx_adapter.proc, "run", lambda argv, **kwargs: next(outputs))

    result = acpx_adapter.run_cursor(
        "inspect", cwd=tmp_path, timeout=120, model="composer-2.5", version="0.12.0", read_only=True
    )

    assert result.ok is False
    assert result.text == "hello"
    assert result.exit_code == 0
    assert result.stop_reason == "cancelled"
    assert result.failure_phase == "output-validation"
    assert result.failure_kind == "non-final-stop"
    assert result.detail == "ACP stream ended without a final completion (stopReason=cancelled)"


def test_run_rejects_version_mismatch(monkeypatch, tmp_path):
    monkeypatch.setattr(acpx_adapter.proc, "which", lambda cmd: f"/bin/{cmd}")
    monkeypatch.setattr(acpx_adapter.proc, "run", lambda argv, **kwargs: proc.Result(0, "acpx 0.13.0\n", ""))
    result = acpx_adapter.run_cursor(
        "inspect", cwd=tmp_path, timeout=120, model="composer-2.5", version="0.12.0", read_only=True
    )
    assert result.ok is False
    assert "requires acpx 0.12.0" in result.detail


def test_run_rejects_invalid_or_empty_protocol_output(monkeypatch, tmp_path):
    monkeypatch.setattr(acpx_adapter.proc, "which", lambda cmd: f"/bin/{cmd}")
    outputs = iter(
        [
            proc.Result(0, "acpx 0.12.0\n", ""),
            proc.Result(0, "not-json\n", ""),
            proc.Result(0, "acpx 0.12.0\n", ""),
            proc.Result(0, _stream({"jsonrpc": "2.0", "id": "x", "result": {"stopReason": "end_turn"}}), ""),
        ]
    )
    monkeypatch.setattr(acpx_adapter.proc, "run", lambda argv, **kwargs: next(outputs))
    invalid = acpx_adapter.run_cursor(
        "inspect", cwd=tmp_path, timeout=120, model="composer-2.5", version="0.12.0", read_only=True
    )
    empty = acpx_adapter.run_cursor(
        "inspect", cwd=tmp_path, timeout=120, model="composer-2.5", version="0.12.0", read_only=True
    )
    assert invalid.ok is False and "invalid ACP NDJSON" in invalid.detail
    assert empty.ok is False and "no final assistant text" in empty.detail


def test_run_preserves_timeout_and_permission_exit(monkeypatch, tmp_path):
    monkeypatch.setattr(acpx_adapter.proc, "which", lambda cmd: f"/bin/{cmd}")
    outputs = iter(
        [
            proc.Result(0, "acpx 0.12.0\n", ""),
            proc.Result(3, "", "acpx timed out"),
            proc.Result(0, "acpx 0.12.0\n", ""),
            proc.Result(5, "", "permission denied"),
        ]
    )
    monkeypatch.setattr(acpx_adapter.proc, "run", lambda argv, **kwargs: next(outputs))
    timeout = acpx_adapter.run_cursor(
        "inspect", cwd=tmp_path, timeout=120, model="composer-2.5", version="0.12.0", read_only=True
    )
    denied = acpx_adapter.run_cursor(
        "inspect", cwd=tmp_path, timeout=120, model="composer-2.5", version="0.12.0", read_only=True
    )
    assert timeout.exit_code == 3 and timeout.timed_out is True
    assert denied.exit_code == 5 and denied.detail == "permission denied"
