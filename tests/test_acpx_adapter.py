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


def _late_permission_stream(*, final_text: str = "Complete review findings.") -> str:
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
                "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": final_text}},
            },
        },
        {"jsonrpc": "2.0", "id": "req-1", "result": {"stopReason": "end_turn"}},
        {
            "jsonrpc": "2.0",
            "id": "perm-1",
            "error": {"code": -32072, "message": "PERMISSION_PROMPT_UNAVAILABLE"},
        },
    )


def _malformed_late_permission_chronology_stream() -> str:
    return _stream(
        {"jsonrpc": "2.0", "id": "init-1", "result": {"protocolVersion": 1}},
        {
            "jsonrpc": "2.0",
            "id": "req-1",
            "method": "session/prompt",
            "params": {"sessionId": "session-1", "prompt": "hidden"},
        },
        {"jsonrpc": "2.0", "id": "req-1", "result": {"stopReason": "end_turn"}},
        {
            "jsonrpc": "2.0",
            "id": "perm-1",
            "error": {"code": -32072, "message": "PERMISSION_PROMPT_UNAVAILABLE"},
        },
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": "session-1",
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "Complete review findings."},
                },
            },
        },
    )


def _prefinal_permission_stream() -> str:
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
            "id": "perm-1",
            "error": {"code": -32072, "message": "PERMISSION_PROMPT_UNAVAILABLE"},
        },
    )


def _authenticated_status() -> acpx_adapter.CursorAuthStatus:
    return acpx_adapter.CursorAuthStatus(
        "authenticated",
        "cursor-agent CLI is authenticated",
        "Logged in as <redacted>",
        "",
        0,
    )


def test_cursor_auth_status_is_prompt_free_bounded_and_redacted(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen.update(argv=argv, kwargs=kwargs)
        return proc.Result(
            0,
            json.dumps(
                {
                    "isAuthenticated": True,
                    "hasAccessToken": True,
                    "hasRefreshToken": True,
                    "status": "authenticated",
                    "userInfo": {
                        "email": "person@example.test",
                        "firstName": "Private",
                        "lastName": "Person",
                        "userId": "user-secret-id",
                    },
                }
            ),
            "",
        )

    monkeypatch.setattr(acpx_adapter.proc, "which", lambda cmd: f"/bin/{cmd}")
    monkeypatch.setattr(acpx_adapter.proc, "run", fake_run)

    status = acpx_adapter.cursor_auth_status()

    assert status.state == "authenticated"
    assert status.detail == "cursor-agent CLI is authenticated"
    assert status.stdout == (
        '{"hasAccessToken":true,"hasRefreshToken":true,"isAuthenticated":true,"status":"authenticated"}'
    )
    assert "person@example.test" not in repr(status)
    assert "Private" not in repr(status)
    assert "user-secret-id" not in repr(status)
    assert seen == {
        "argv": ["cursor-agent", "status", "--format", "json"],
        "kwargs": {"timeout": 10.0},
    }


def test_cursor_auth_status_reports_unauthenticated_with_recovery(monkeypatch):
    monkeypatch.setattr(acpx_adapter.proc, "which", lambda cmd: f"/bin/{cmd}")
    monkeypatch.setattr(
        acpx_adapter.proc,
        "run",
        lambda argv, **kwargs: proc.Result(
            1,
            json.dumps(
                {
                    "isAuthenticated": False,
                    "status": "unauthenticated",
                    "userInfo": {"email": "person@example.test"},
                }
            ),
            "",
        ),
    )

    status = acpx_adapter.cursor_auth_status()

    assert status.state == "unauthenticated"
    assert status.detail == (
        "cursor-agent CLI is not logged in; run `cursor-agent login` once, then verify with `cursor-agent status`"
    )
    assert status.stdout == '{"isAuthenticated":false,"status":"unauthenticated"}'
    assert status.exit_code == 1


def test_cursor_auth_status_uses_boolean_json_state_not_incidental_words(monkeypatch):
    monkeypatch.setattr(acpx_adapter.proc, "which", lambda cmd: f"/bin/{cmd}")
    outputs = iter(
        [
            proc.Result(0, '{"isAuthenticated":false,"status":"authenticated"}', ""),
            proc.Result(
                0,
                '{"isAuthenticated":true,"status":"authenticated"}',
                "warning: optional telemetry authentication required",
            ),
            proc.Result(0, "authenticated: false", ""),
        ]
    )
    monkeypatch.setattr(acpx_adapter.proc, "run", lambda argv, **kwargs: next(outputs))

    false_state = acpx_adapter.cursor_auth_status()
    true_state = acpx_adapter.cursor_auth_status()
    prose = acpx_adapter.cursor_auth_status()

    assert false_state.state == "unauthenticated"
    assert true_state.state == "authenticated"
    assert prose.state == "unrecognized"


def test_cursor_auth_status_redacts_ansi_and_common_credential_shapes(monkeypatch):
    monkeypatch.setattr(acpx_adapter.proc, "which", lambda cmd: f"/bin/{cmd}")
    monkeypatch.setattr(
        acpx_adapter.proc,
        "run",
        lambda argv, **kwargs: proc.Result(
            5,
            "",
            "\x1b[31mToken raw-token Bearer raw-bearer password=raw-password\x1b[0m",
        ),
    )

    status = acpx_adapter.cursor_auth_status()

    assert status.state == "unavailable"
    assert "\x1b" not in status.stderr
    assert "raw-token" not in repr(status)
    assert "raw-bearer" not in repr(status)
    assert "raw-password" not in repr(status)
    assert status.stderr == "Token=<redacted> Bearer <redacted> password=<redacted>"


def test_cursor_auth_status_distinguishes_failure_and_unrecognized_output(monkeypatch):
    monkeypatch.setattr(acpx_adapter.proc, "which", lambda cmd: f"/bin/{cmd}")
    outputs = iter(
        [
            proc.Result(124, "", "timeout after 10.0s"),
            proc.Result(0, "status format changed", ""),
        ]
    )
    monkeypatch.setattr(acpx_adapter.proc, "run", lambda argv, **kwargs: next(outputs))

    failed = acpx_adapter.cursor_auth_status()
    unknown = acpx_adapter.cursor_auth_status()

    assert failed.state == "unavailable"
    assert failed.detail == "cursor-agent status failed (exit 124): timeout after 10.0s"
    assert failed.exit_code == 124
    assert unknown.state == "unrecognized"
    assert unknown.detail == "cursor-agent status returned an unrecognized response: status format changed"


def test_run_refuses_unauthenticated_cursor_before_acpx(monkeypatch, tmp_path):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv == ["acpx", "--version"]:
            return proc.Result(0, "acpx 0.12.0\n", "")
        if argv == ["cursor-agent", "status", "--format", "json"]:
            return proc.Result(0, '{"isAuthenticated":false,"status":"unauthenticated"}\n', "")
        raise AssertionError(f"ACPX must not start when Cursor is unauthenticated: {argv}")

    monkeypatch.setattr(acpx_adapter.proc, "which", lambda cmd: f"/bin/{cmd}")
    monkeypatch.setattr(acpx_adapter.proc, "run", fake_run)

    result = acpx_adapter.run_cursor(
        "inspect", cwd=tmp_path, timeout=120, model="composer-2.5", version="0.12.0", read_only=True
    )

    assert result.ok is False
    assert result.failure_phase == "preflight"
    assert result.failure_kind == "provider-auth"
    assert result.detail == (
        "cursor-agent CLI is not logged in; run `cursor-agent login` once, then verify with `cursor-agent status`"
    )
    assert result.stdout == '{"isAuthenticated":false,"status":"unauthenticated"}'
    assert result.exit_code == 0
    assert calls == [
        ["acpx", "--version"],
        ["cursor-agent", "status", "--format", "json"],
    ]


def test_run_refuses_unavailable_or_unrecognized_cursor_status_before_acpx(monkeypatch, tmp_path):
    monkeypatch.setattr(acpx_adapter.proc, "which", lambda cmd: f"/bin/{cmd}")
    outputs = iter(
        [
            proc.Result(0, "acpx 0.12.0\n", ""),
            proc.Result(5, "", "status service unavailable"),
            proc.Result(0, "acpx 0.12.0\n", ""),
            proc.Result(0, "new status format", ""),
        ]
    )
    monkeypatch.setattr(acpx_adapter.proc, "run", lambda argv, **kwargs: next(outputs))

    unavailable = acpx_adapter.run_cursor(
        "inspect", cwd=tmp_path, timeout=120, model="composer-2.5", version="0.12.0", read_only=True
    )
    unrecognized = acpx_adapter.run_cursor(
        "inspect", cwd=tmp_path, timeout=120, model="composer-2.5", version="0.12.0", read_only=True
    )

    assert unavailable.ok is False
    assert unavailable.failure_phase == "preflight"
    assert unavailable.failure_kind == "auth-status-unavailable"
    assert unavailable.exit_code == 5
    assert unrecognized.ok is False
    assert unrecognized.failure_phase == "preflight"
    assert unrecognized.failure_kind == "auth-status-unrecognized"
    assert unrecognized.exit_code == 0


def test_run_authenticated_cursor_reaches_acpx_and_preserves_short_final(monkeypatch, tmp_path):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv == ["acpx", "--version"]:
            return proc.Result(0, "acpx 0.12.0\n", "")
        if argv == ["cursor-agent", "status", "--format", "json"]:
            return proc.Result(0, '{"isAuthenticated":true,"status":"authenticated"}\n', "")
        return proc.Result(0, _success_stream().replace('"text": "hello"', '"text": "No findings."'), "")

    monkeypatch.setattr(acpx_adapter.proc, "which", lambda cmd: f"/bin/{cmd}")
    monkeypatch.setattr(acpx_adapter.proc, "run", fake_run)

    result = acpx_adapter.run_cursor(
        "inspect", cwd=tmp_path, timeout=120, model="composer-2.5", version="0.12.0", read_only=True
    )

    assert result.ok is True
    assert result.text == "No findings."
    assert calls[1] == ["cursor-agent", "status", "--format", "json"]
    assert calls[2][0] == "acpx"


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
    monkeypatch.setattr(acpx_adapter, "cursor_auth_status", _authenticated_status)
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
    monkeypatch.setattr(acpx_adapter, "cursor_auth_status", _authenticated_status)
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
    monkeypatch.setattr(acpx_adapter, "cursor_auth_status", _authenticated_status)
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
    monkeypatch.setattr(acpx_adapter, "cursor_auth_status", _authenticated_status)
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


def test_run_correlates_stop_reason_to_prompt_request(monkeypatch, tmp_path):
    stream = _stream(
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
                "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "partial"}},
            },
        },
        {"jsonrpc": "2.0", "id": "req-1", "result": {"stopReason": "cancelled"}},
        {"jsonrpc": "2.0", "id": "unrelated", "result": {"stopReason": "end_turn"}},
    )
    monkeypatch.setattr(acpx_adapter.proc, "which", lambda cmd: f"/bin/{cmd}")
    monkeypatch.setattr(acpx_adapter, "cursor_auth_status", _authenticated_status)
    outputs = iter([proc.Result(0, "acpx 0.12.0\n", ""), proc.Result(0, stream, "")])
    monkeypatch.setattr(acpx_adapter.proc, "run", lambda argv, **kwargs: next(outputs))

    result = acpx_adapter.run_cursor(
        "inspect", cwd=tmp_path, timeout=120, model="composer-2.5", version="0.12.0", read_only=True
    )

    assert result.ok is False
    assert result.request_id == "req-1"
    assert result.stop_reason == "cancelled"
    assert result.failure_kind == "non-final-stop"


def test_run_rejects_version_mismatch(monkeypatch, tmp_path):
    monkeypatch.setattr(acpx_adapter.proc, "which", lambda cmd: f"/bin/{cmd}")
    monkeypatch.setattr(acpx_adapter.proc, "run", lambda argv, **kwargs: proc.Result(0, "acpx 0.13.0\n", ""))
    result = acpx_adapter.run_cursor(
        "inspect", cwd=tmp_path, timeout=120, model="composer-2.5", version="0.12.0", read_only=True
    )
    assert result.ok is False
    assert "requires acpx 0.12.0" in result.detail
    assert result.failure_phase == "preflight"
    assert result.failure_kind == "version-mismatch"


def test_run_rejects_invalid_or_empty_protocol_output(monkeypatch, tmp_path):
    monkeypatch.setattr(acpx_adapter.proc, "which", lambda cmd: f"/bin/{cmd}")
    monkeypatch.setattr(acpx_adapter, "cursor_auth_status", _authenticated_status)
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
    assert invalid.failure_phase == "output-validation"
    assert invalid.failure_kind == "malformed-transport"
    assert empty.failure_phase == "output-validation"
    assert empty.failure_kind == "empty-output"


def test_run_distinguishes_timeout_permission_and_provider_startup(monkeypatch, tmp_path):
    monkeypatch.setattr(acpx_adapter.proc, "which", lambda cmd: f"/bin/{cmd}")
    monkeypatch.setattr(acpx_adapter, "cursor_auth_status", _authenticated_status)
    outputs = iter(
        [
            proc.Result(0, "acpx 0.12.0\n", ""),
            proc.Result(3, "", "acpx timed out"),
            proc.Result(0, "acpx 0.12.0\n", ""),
            proc.Result(5, "", "permission denied"),
            proc.Result(0, "acpx 0.12.0\n", ""),
            proc.Result(7, "", "provider failed before ACP startup"),
        ]
    )
    monkeypatch.setattr(acpx_adapter.proc, "run", lambda argv, **kwargs: next(outputs))
    timeout = acpx_adapter.run_cursor(
        "inspect", cwd=tmp_path, timeout=120, model="composer-2.5", version="0.12.0", read_only=True
    )
    denied = acpx_adapter.run_cursor(
        "inspect", cwd=tmp_path, timeout=120, model="composer-2.5", version="0.12.0", read_only=True
    )
    startup = acpx_adapter.run_cursor(
        "inspect", cwd=tmp_path, timeout=120, model="composer-2.5", version="0.12.0", read_only=True
    )
    assert timeout.exit_code == 3 and timeout.timed_out is True
    assert denied.exit_code == 5 and denied.detail == "permission denied"
    assert timeout.failure_phase == "inference"
    assert timeout.failure_kind == "timeout"
    assert denied.failure_phase == "dispatch"
    assert denied.failure_kind == "permission-denied"
    assert startup.exit_code == 7
    assert startup.failure_phase == "dispatch"
    assert startup.failure_kind == "provider-startup"


def test_run_preserves_end_turn_final_after_late_permission_prompt(monkeypatch, tmp_path):
    stream = _late_permission_stream()
    monkeypatch.setattr(acpx_adapter.proc, "which", lambda cmd: f"/bin/{cmd}")
    monkeypatch.setattr(acpx_adapter, "cursor_auth_status", _authenticated_status)
    outputs = iter(
        [
            proc.Result(0, "acpx 0.12.0\n", ""),
            proc.Result(5, stream, "PERMISSION_PROMPT_UNAVAILABLE"),
        ]
    )
    monkeypatch.setattr(acpx_adapter.proc, "run", lambda argv, **kwargs: next(outputs))

    result = acpx_adapter.run_cursor(
        "inspect", cwd=tmp_path, timeout=120, model="composer-2.5", version="0.12.0", read_only=True
    )

    assert result.ok is True
    assert result.text == "Complete review findings."
    assert result.exit_code == 5
    assert result.stop_reason == "end_turn"
    assert result.session_id == "session-1"
    assert result.request_id == "req-1"
    assert result.stdout == stream
    assert result.stderr == "PERMISSION_PROMPT_UNAVAILABLE"
    assert result.transport_warning == {
        "phase": "post-final",
        "kind": "permission-prompt-unavailable",
        "code": -32072,
        "detail": "PERMISSION_PROMPT_UNAVAILABLE",
    }
    assert "late permission prompt unavailable" in result.detail


def test_parse_stream_rejects_post_permission_text_without_prefinal_answer():
    parsed, error = acpx_adapter.parse_stream(_malformed_late_permission_chronology_stream())

    assert parsed is None
    assert error == "ACP stream contained no final assistant text"


def test_run_rejects_post_permission_text_without_prefinal_answer(monkeypatch, tmp_path):
    stream = _malformed_late_permission_chronology_stream()
    monkeypatch.setattr(acpx_adapter.proc, "which", lambda cmd: f"/bin/{cmd}")
    monkeypatch.setattr(acpx_adapter, "cursor_auth_status", _authenticated_status)
    outputs = iter(
        [
            proc.Result(0, "acpx 0.12.0\n", ""),
            proc.Result(5, stream, "PERMISSION_PROMPT_UNAVAILABLE"),
        ]
    )
    monkeypatch.setattr(acpx_adapter.proc, "run", lambda argv, **kwargs: next(outputs))

    result = acpx_adapter.run_cursor(
        "inspect", cwd=tmp_path, timeout=120, model="composer-2.5", version="0.12.0", read_only=True
    )

    assert result.ok is False
    assert result.text == ""
    assert result.exit_code == 5
    assert result.transport_warning is None
    assert result.failure_kind == "provider-startup"


def test_run_keeps_prefinal_permission_failure_failed(monkeypatch, tmp_path):
    stream = _prefinal_permission_stream()
    monkeypatch.setattr(acpx_adapter.proc, "which", lambda cmd: f"/bin/{cmd}")
    monkeypatch.setattr(acpx_adapter, "cursor_auth_status", _authenticated_status)
    outputs = iter(
        [
            proc.Result(0, "acpx 0.12.0\n", ""),
            proc.Result(5, stream, "PERMISSION_PROMPT_UNAVAILABLE"),
        ]
    )
    monkeypatch.setattr(acpx_adapter.proc, "run", lambda argv, **kwargs: next(outputs))

    result = acpx_adapter.run_cursor(
        "inspect", cwd=tmp_path, timeout=120, model="composer-2.5", version="0.12.0", read_only=True
    )

    assert result.ok is False
    assert result.text == ""
    assert result.exit_code == 5
    assert result.failure_kind == "provider-startup"
    assert result.transport_warning is None


def test_permission_prompt_diagnostic_requires_exact_code():
    assert acpx_adapter._permission_prompt_diagnostic({"code": -32072, "message": "PERMISSION_PROMPT_UNAVAILABLE"}) == {
        "phase": "post-final",
        "kind": "permission-prompt-unavailable",
        "code": -32072,
        "detail": "PERMISSION_PROMPT_UNAVAILABLE",
    }
    assert (
        acpx_adapter._permission_prompt_diagnostic({"code": -32000, "message": "PERMISSION_PROMPT_UNAVAILABLE"}) is None
    )
    assert (
        acpx_adapter._permission_prompt_diagnostic({"code": "-32072", "message": "PERMISSION_PROMPT_UNAVAILABLE"})
        is None
    )


def test_permission_prompt_diagnostic_canonicalizes_structured_message():
    message = "PERMISSION_PROMPT_UNAVAILABLE token=fake-secret-token /tmp/fake-secret-path"
    warning = acpx_adapter._permission_prompt_diagnostic({"code": -32072, "message": message})

    assert warning == {
        "phase": "post-final",
        "kind": "permission-prompt-unavailable",
        "code": -32072,
        "detail": "PERMISSION_PROMPT_UNAVAILABLE",
    }
    assert "fake-secret-token" not in repr(warning)
    assert "/tmp/fake-secret-path" not in repr(warning)


def test_run_late_permission_structured_error_sanitizes_receipt_detail(monkeypatch, tmp_path):
    secret_message = "PERMISSION_PROMPT_UNAVAILABLE token=fake-secret-token /tmp/fake-secret-path"
    stream = _stream(
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
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "Complete review findings."},
                },
            },
        },
        {"jsonrpc": "2.0", "id": "req-1", "result": {"stopReason": "end_turn"}},
        {
            "jsonrpc": "2.0",
            "id": "perm-1",
            "error": {"code": -32072, "message": secret_message},
        },
    )
    monkeypatch.setattr(acpx_adapter.proc, "which", lambda cmd: f"/bin/{cmd}")
    monkeypatch.setattr(acpx_adapter, "cursor_auth_status", _authenticated_status)
    outputs = iter(
        [
            proc.Result(0, "acpx 0.12.0\n", ""),
            proc.Result(5, stream, "transport stderr preserved"),
        ]
    )
    monkeypatch.setattr(acpx_adapter.proc, "run", lambda argv, **kwargs: next(outputs))

    result = acpx_adapter.run_cursor(
        "inspect", cwd=tmp_path, timeout=120, model="composer-2.5", version="0.12.0", read_only=True
    )

    assert result.ok is True
    assert result.transport_warning == {
        "phase": "post-final",
        "kind": "permission-prompt-unavailable",
        "code": -32072,
        "detail": "PERMISSION_PROMPT_UNAVAILABLE",
    }
    assert "fake-secret-token" not in repr(result.transport_warning)
    assert "/tmp/fake-secret-path" not in repr(result.transport_warning)
    assert "fake-secret-token" not in result.detail
    assert "/tmp/fake-secret-path" not in result.detail
    assert result.stdout == stream
    assert result.stderr == "transport stderr preserved"


def test_late_permission_from_stderr_uses_canonical_detail():
    stderr = "PERMISSION_PROMPT_UNAVAILABLE token=fake-secret-token /tmp/fake-secret-path"
    warning = acpx_adapter._late_permission_from_stderr(stderr, prompt_completed=True)

    assert warning == {
        "phase": "post-final",
        "kind": "permission-prompt-unavailable",
        "code": -32072,
        "detail": "PERMISSION_PROMPT_UNAVAILABLE",
    }
    assert "fake-secret-token" not in repr(warning)
    assert "/tmp/fake-secret-path" not in repr(warning)


def test_run_late_permission_stderr_fallback_sanitizes_structured_output(monkeypatch, tmp_path):
    stream = _stream(
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
                "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "Done."}},
            },
        },
        {"jsonrpc": "2.0", "id": "req-1", "result": {"stopReason": "end_turn"}},
    )
    stderr = "PERMISSION_PROMPT_UNAVAILABLE token=fake-secret-token /tmp/fake-secret-path"
    monkeypatch.setattr(acpx_adapter.proc, "which", lambda cmd: f"/bin/{cmd}")
    monkeypatch.setattr(acpx_adapter, "cursor_auth_status", _authenticated_status)
    outputs = iter(
        [
            proc.Result(0, "acpx 0.12.0\n", ""),
            proc.Result(5, stream, stderr),
        ]
    )
    monkeypatch.setattr(acpx_adapter.proc, "run", lambda argv, **kwargs: next(outputs))

    result = acpx_adapter.run_cursor(
        "inspect", cwd=tmp_path, timeout=120, model="composer-2.5", version="0.12.0", read_only=True
    )

    assert result.ok is True
    assert result.transport_warning == {
        "phase": "post-final",
        "kind": "permission-prompt-unavailable",
        "code": -32072,
        "detail": "PERMISSION_PROMPT_UNAVAILABLE",
    }
    assert "fake-secret-token" not in repr(result.transport_warning)
    assert "/tmp/fake-secret-path" not in repr(result.transport_warning)
    assert "fake-secret-token" not in result.detail
    assert "/tmp/fake-secret-path" not in result.detail


def test_run_preserves_transport_warning_when_late_final_fails_output_validation(monkeypatch, tmp_path):
    stream = _late_permission_stream(final_text="Reviewing repository files.")
    monkeypatch.setattr(acpx_adapter.proc, "which", lambda cmd: f"/bin/{cmd}")
    monkeypatch.setattr(acpx_adapter, "cursor_auth_status", _authenticated_status)
    outputs = iter(
        [
            proc.Result(0, "acpx 0.12.0\n", ""),
            proc.Result(5, stream, "PERMISSION_PROMPT_UNAVAILABLE"),
        ]
    )
    monkeypatch.setattr(acpx_adapter.proc, "run", lambda argv, **kwargs: next(outputs))

    result = acpx_adapter.run_cursor(
        "inspect", cwd=tmp_path, timeout=120, model="composer-2.5", version="0.12.0", read_only=True
    )

    assert result.ok is False
    assert result.failure_phase == "output-validation"
    assert result.failure_kind == "non-final-output"
    assert result.transport_warning == {
        "phase": "post-final",
        "kind": "permission-prompt-unavailable",
        "code": -32072,
        "detail": "PERMISSION_PROMPT_UNAVAILABLE",
    }


def test_run_rejects_mixed_channel_wrong_code_with_stderr_marker(monkeypatch, tmp_path):
    stream = _stream(
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
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "Complete review findings."},
                },
            },
        },
        {"jsonrpc": "2.0", "id": "req-1", "result": {"stopReason": "end_turn"}},
        {
            "jsonrpc": "2.0",
            "id": "perm-1",
            "error": {"code": -32000, "message": "PERMISSION_PROMPT_UNAVAILABLE"},
        },
    )
    stderr = "PERMISSION_PROMPT_UNAVAILABLE token=fake-secret-token /tmp/fake-secret-path"
    monkeypatch.setattr(acpx_adapter.proc, "which", lambda cmd: f"/bin/{cmd}")
    monkeypatch.setattr(acpx_adapter, "cursor_auth_status", _authenticated_status)
    outputs = iter(
        [
            proc.Result(0, "acpx 0.12.0\n", ""),
            proc.Result(5, stream, stderr),
        ]
    )
    monkeypatch.setattr(acpx_adapter.proc, "run", lambda argv, **kwargs: next(outputs))

    result = acpx_adapter.run_cursor(
        "inspect", cwd=tmp_path, timeout=120, model="composer-2.5", version="0.12.0", read_only=True
    )

    assert result.ok is False
    assert result.text == "Complete review findings."
    assert result.exit_code == 5
    assert result.transport_warning is None
    assert result.failure_kind == "transport-error"


def test_run_rejects_wrong_permission_code_even_with_marker_message(monkeypatch, tmp_path):
    stream = _stream(
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
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "Complete review findings."},
                },
            },
        },
        {"jsonrpc": "2.0", "id": "req-1", "result": {"stopReason": "end_turn"}},
        {
            "jsonrpc": "2.0",
            "id": "perm-1",
            "error": {"code": -32000, "message": "PERMISSION_PROMPT_UNAVAILABLE"},
        },
    )
    monkeypatch.setattr(acpx_adapter.proc, "which", lambda cmd: f"/bin/{cmd}")
    monkeypatch.setattr(acpx_adapter, "cursor_auth_status", _authenticated_status)
    outputs = iter(
        [
            proc.Result(0, "acpx 0.12.0\n", ""),
            proc.Result(5, stream, "unrelated transport failure"),
        ]
    )
    monkeypatch.setattr(acpx_adapter.proc, "run", lambda argv, **kwargs: next(outputs))

    result = acpx_adapter.run_cursor(
        "inspect", cwd=tmp_path, timeout=120, model="composer-2.5", version="0.12.0", read_only=True
    )

    assert result.ok is False
    assert result.text == "Complete review findings."
    assert result.transport_warning is None
    assert result.failure_kind == "transport-error"
