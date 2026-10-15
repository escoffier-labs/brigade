from __future__ import annotations

import json

import pytest

from brigade import acpx_adapter
from brigade import proc


def _stream(*messages: dict) -> str:
    if any(
        isinstance(message.get("result"), dict)
        and "protocolVersion" in message["result"]
        and message.get("id") == "init-1"
        for message in messages
    ) and not any(message.get("method") == "initialize" and message.get("id") == "init-1" for message in messages):
        messages = ({"jsonrpc": "2.0", "method": "initialize", "id": "init-1", "params": {}}, *messages)
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


def test_cursor_auth_status_redacts_credential_shapes_and_private_paths(monkeypatch):
    sensitive_values = (
        "ghp_" + "A" * 24,
        "github_pat_" + "A" * 24,
        "xoxb-" + "A" * 16,
        "sk-" + "A" * 24,
        "AKIA" + "A" * 16,
        "ASIA" + "A" * 16,
        "AIza" + "a" * 30,
        "glpat-" + "a" * 20,
        "eyJ" + "A" * 8 + "." + "B" * 8 + "." + "C" * 8,
        r"\root\.ssh\id_rsa",
    )
    monkeypatch.setattr(acpx_adapter.proc, "which", lambda cmd: f"/bin/{cmd}")
    for sensitive in sensitive_values:
        monkeypatch.setattr(
            acpx_adapter.proc,
            "run",
            lambda argv, sensitive=sensitive, **kwargs: proc.Result(
                1, f"bad status {sensitive}", f"failure {sensitive}"
            ),
        )
        status = acpx_adapter.cursor_auth_status()
        assert sensitive not in status.detail
        assert sensitive not in status.stdout
        assert sensitive not in status.stderr


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
    assert result.requested_model == "composer-2.5"
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


def test_run_cursor_reuses_registry_for_all_preflight_and_execution_calls(monkeypatch, tmp_path):
    registry = proc.ProcessRegistry()
    registries = []

    def fake_run(argv, **kwargs):
        registries.append(kwargs.get("process_registry"))
        if argv == ["acpx", "--version"]:
            return proc.Result(0, "acpx 0.12.0\n", "")
        if argv == ["cursor-agent", "status", "--format", "json"]:
            return proc.Result(0, '{"isAuthenticated":true,"status":"authenticated"}\n', "")
        return proc.Result(0, _success_stream(), "")

    monkeypatch.setattr(acpx_adapter.proc, "which", lambda cmd: f"/bin/{cmd}")
    monkeypatch.setattr(acpx_adapter.proc, "run", fake_run)

    result = acpx_adapter.run_cursor(
        "inspect",
        cwd=tmp_path,
        timeout=120,
        model="composer-2.5",
        version="0.12.0",
        read_only=True,
        process_registry=registry,
    )

    assert result.ok is True
    assert registries == [registry, registry, registry]


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


def test_parse_stream_ignores_model_like_values_outside_config_updates():
    parsed, error = acpx_adapter.parse_stream(
        _stream(
            {"jsonrpc": "2.0", "id": "init-1", "result": {"protocolVersion": 1}},
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "update": {
                        "sessionUpdate": "config_option_update",
                        "modelId": "grok-4.5[effort=high,fast=true]",
                    }
                },
            },
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "hello", "model": "PRIVATE_MARKER"},
                    }
                },
            },
            {"jsonrpc": "2.0", "id": "req-1", "result": {"stopReason": "end_turn"}},
        )
    )

    assert error == ""
    assert parsed is not None and parsed["effective_model"] == "grok-4.5[effort=high,fast=true]"


def test_parse_stream_accepts_default_model_with_empty_options():
    parsed, error = acpx_adapter.parse_stream(
        _stream(
            {"jsonrpc": "2.0", "id": "init-1", "result": {"protocolVersion": 1}},
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {"update": {"sessionUpdate": "config_option_update", "modelId": "default[]"}},
            },
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "answer"},
                    }
                },
            },
            {"jsonrpc": "2.0", "id": "req-1", "result": {"stopReason": "end_turn"}},
        )
    )

    assert error == ""
    assert parsed is not None and parsed["effective_model"] == "default[]"


def test_parse_stream_clears_stale_model_after_rejected_explicit_update():
    parsed, error = acpx_adapter.parse_stream(
        _stream(
            {"jsonrpc": "2.0", "id": "init-1", "result": {"protocolVersion": 1}},
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {"update": {"sessionUpdate": "config_option_update", "modelId": "model-a"}},
            },
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "update": {
                        "sessionUpdate": "config_option_update",
                        "modelId": "ghp_FAKE0123456789abcdefghijklmnopqrstuvwxyz",
                    }
                },
            },
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "hello"},
                    }
                },
            },
        )
    )

    assert error == ""
    assert parsed is not None and parsed["effective_model"] is None


@pytest.mark.parametrize(
    "message",
    [
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "update": {
                    "sessionUpdate": "config_option_update",
                    "configOptions": [
                        {
                            "id": "model",
                            "category": "model",
                            "type": "select",
                            "currentValue": "composer-2.5",
                            "options": [],
                        },
                        {
                            "id": "fast-mode",
                            "category": "model",
                            "type": "select",
                            "currentValue": "fast",
                            "options": [],
                        },
                    ],
                }
            },
        },
        {
            "jsonrpc": "2.0",
            "id": "new-session",
            "result": {
                "configOptions": [
                    {
                        "id": "model",
                        "category": "model",
                        "type": "select",
                        "currentValue": "composer-2.5",
                        "options": [],
                    },
                    {
                        "id": "fast-mode",
                        "category": "model",
                        "type": "select",
                        "currentValue": "fast",
                        "options": [],
                    },
                ]
            },
        },
    ],
    ids=["config-update", "session-response"],
)
def test_parse_stream_reads_standard_model_config_options(message):
    parsed, error = acpx_adapter.parse_stream(
        _stream(
            {"jsonrpc": "2.0", "id": "init-1", "result": {"protocolVersion": 1}},
            {"jsonrpc": "2.0", "method": "session/new", "id": "new-session", "params": {}},
            message,
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "hello"},
                    }
                },
            },
        )
    )

    assert error == ""
    assert parsed is not None and parsed["effective_model"] == "composer-2.5"
    assert parsed["effective_model_locator"] == (
        "acp-config-option-update" if message.get("method") == "session/update" else "acp-session-new-response"
    )


def test_parse_stream_ignores_config_options_from_unrelated_response():
    parsed, error = acpx_adapter.parse_stream(
        _stream(
            {"jsonrpc": "2.0", "method": "session/new", "id": "new-1", "params": {}},
            {
                "jsonrpc": "2.0",
                "id": "new-1",
                "result": {"configOptions": [{"id": "model", "category": "model", "currentValue": "composer-2.5"}]},
            },
            {"jsonrpc": "2.0", "method": "session/list", "id": "other-1", "params": {}},
            {
                "jsonrpc": "2.0",
                "id": "other-1",
                "result": {"configOptions": [{"id": "model", "category": "model", "currentValue": "forged-model"}]},
            },
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "hello"},
                    }
                },
            },
        )
    )

    assert error == ""
    assert parsed is not None and parsed["effective_model"] == "composer-2.5"


def test_parse_stream_keeps_jsonrpc_id_types_distinct():
    parsed, error = acpx_adapter.parse_stream(
        _stream(
            {"jsonrpc": "2.0", "method": "session/new", "id": 1, "params": {}},
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"configOptions": [{"id": "model", "category": "model", "currentValue": "right-model"}]},
            },
            {
                "jsonrpc": "2.0",
                "id": "1",
                "result": {"configOptions": [{"id": "model", "category": "model", "currentValue": "wrong-model"}]},
            },
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "hello"},
                    }
                },
            },
        )
    )

    assert error == ""
    assert parsed is not None and parsed["effective_model"] == "right-model"


def test_parse_stream_only_completes_matching_active_prompt_ids():
    streams = (
        _stream(
            {"jsonrpc": "2.0", "method": "session/prompt", "id": 1, "params": {}},
            {"jsonrpc": "2.0", "id": "1", "result": {"stopReason": "end_turn"}},
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "partial"}}
                },
            },
        ),
        _stream(
            {"jsonrpc": "2.0", "id": "req-1", "result": {"stopReason": "end_turn"}},
            {"jsonrpc": "2.0", "method": "session/prompt", "id": "req-1", "params": {}},
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "partial"}}
                },
            },
        ),
    )
    for stream in streams:
        parsed, error = acpx_adapter.parse_stream(stream)
        assert error == ""
        assert parsed is not None
        assert parsed["prompt_completed"] is False
        assert parsed["stop_reason"] is None


@pytest.mark.parametrize("agent_response_first", (True, False))
def test_parse_stream_keeps_prompt_active_when_agent_request_reuses_its_id(agent_response_first):
    messages = [
        {"jsonrpc": "2.0", "method": "session/prompt", "id": "shared", "params": {}},
        {"jsonrpc": "2.0", "method": "session/request_permission", "id": "shared", "params": {}},
    ]
    agent_response = {"jsonrpc": "2.0", "id": "shared", "result": {}}
    if agent_response_first:
        messages.append(agent_response)
    messages.extend(
        (
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "answer"},
                    }
                },
            },
            {"jsonrpc": "2.0", "id": "shared", "result": {"stopReason": "end_turn"}},
        )
    )
    if not agent_response_first:
        messages.append(agent_response)

    parsed, error = acpx_adapter.parse_stream(_stream(*messages))

    assert error == ""
    assert parsed is not None
    assert parsed["request_id"] == "shared"
    assert parsed["text"] == "answer"
    assert parsed["prompt_completed"] is True
    assert parsed["stop_reason"] == "end_turn"


def test_parse_stream_rejects_overlapping_prompts_before_uncorrelated_chunks():
    parsed, error = acpx_adapter.parse_stream(
        _stream(
            {"jsonrpc": "2.0", "method": "session/prompt", "id": "old", "params": {}},
            {"jsonrpc": "2.0", "method": "session/prompt", "id": "current", "params": {}},
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "partial"},
                    }
                },
            },
            {"jsonrpc": "2.0", "id": "old", "result": {"stopReason": "end_turn"}},
        )
    )

    assert parsed is None
    assert "overlapping session/prompt requests" in error


def test_parse_stream_new_prompt_resets_previous_completion_and_text():
    parsed, error = acpx_adapter.parse_stream(
        _stream(
            {"jsonrpc": "2.0", "method": "session/prompt", "id": "first", "params": {}},
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "old answer"},
                    }
                },
            },
            {"jsonrpc": "2.0", "id": "first", "result": {"stopReason": "end_turn"}},
            {"jsonrpc": "2.0", "method": "session/prompt", "id": "latest", "params": {}},
        ),
        allow_empty_text=True,
    )

    assert error == "ACP stream contained no final assistant text"
    assert parsed is not None
    assert parsed["request_id"] == "latest"
    assert parsed["text"] == ""
    assert parsed["prompt_completed"] is False
    assert parsed["stop_reason"] is None


def test_parse_stream_ignores_colliding_agent_permission_error_for_prompt_state():
    parsed, error = acpx_adapter.parse_stream(
        _stream(
            {"jsonrpc": "2.0", "method": "session/prompt", "id": "shared", "params": {}},
            {
                "jsonrpc": "2.0",
                "method": "session/request_permission",
                "id": "shared",
                "params": {},
            },
            {
                "jsonrpc": "2.0",
                "id": "shared",
                "error": {"code": -32072, "message": "PERMISSION_PROMPT_UNAVAILABLE"},
            },
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "answer"},
                    }
                },
            },
            {"jsonrpc": "2.0", "id": "shared", "result": {"stopReason": "end_turn"}},
        ),
        allow_empty_text=True,
    )

    assert error == ""
    assert parsed is not None
    assert parsed["text"] == "answer"
    assert parsed["stop_reason"] == "end_turn"
    assert parsed["has_structured_jsonrpc_error"] is False


def test_parse_stream_preserves_ambiguous_prompt_error_until_prompt_completion():
    parsed, error = acpx_adapter.parse_stream(
        _stream(
            {"jsonrpc": "2.0", "method": "session/prompt", "id": "shared", "params": {}},
            {
                "jsonrpc": "2.0",
                "method": "session/request_permission",
                "id": "shared",
                "params": {},
            },
            {"jsonrpc": "2.0", "id": "shared", "error": {"code": -32000, "message": "Model unavailable"}},
            {"jsonrpc": "2.0", "id": "shared", "result": {"outcome": "selected"}},
        ),
        allow_empty_text=True,
    )

    assert parsed is not None
    assert parsed["prompt_completed"] is False
    assert parsed["structured_error_detail"] == "Model unavailable"
    assert error == "Model unavailable"


def test_parse_stream_rejects_duplicate_outstanding_prompt_id():
    parsed, error = acpx_adapter.parse_stream(
        _stream(
            {"jsonrpc": "2.0", "method": "session/prompt", "id": "same", "params": {}},
            {"jsonrpc": "2.0", "method": "session/prompt", "id": "same", "params": {}},
        ),
        allow_empty_text=True,
    )

    assert parsed is None
    assert "overlapping session/prompt requests" in error


def test_parse_stream_rejects_overlapping_prompt_before_superseded_permission_error():
    parsed, error = acpx_adapter.parse_stream(
        _stream(
            {"jsonrpc": "2.0", "method": "session/prompt", "id": "old", "params": {}},
            {"jsonrpc": "2.0", "method": "session/prompt", "id": "new", "params": {}},
            {"jsonrpc": "2.0", "id": "old", "error": {"code": -32072, "message": "PERMISSION_PROMPT_UNAVAILABLE"}},
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "new answer"},
                    }
                },
            },
            {"jsonrpc": "2.0", "id": "new", "result": {"stopReason": "end_turn"}},
        )
    )

    assert parsed is None
    assert "overlapping session/prompt requests" in error


@pytest.mark.parametrize("protocol_version", (True, "2"))
def test_parse_stream_rejects_malformed_protocol_version(protocol_version):
    parsed, error = acpx_adapter.parse_stream(
        _stream(
            {"jsonrpc": "2.0", "method": "initialize", "id": "init", "params": {}},
            {"jsonrpc": "2.0", "id": "init", "result": {"protocolVersion": protocol_version}},
            {"jsonrpc": "2.0", "method": "session/prompt", "id": "prompt", "params": {}},
            {"jsonrpc": "2.0", "id": "prompt", "result": {"stopReason": "end_turn"}},
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "answer"},
                    }
                },
            },
        )
    )

    assert parsed is None
    assert "malformed ACP protocol version" in error


def test_parse_stream_rejects_protocol_version_outside_initialize_response():
    parsed, error = acpx_adapter.parse_stream(
        _stream(
            {"jsonrpc": "2.0", "method": "initialize", "id": "init", "params": {}},
            {"jsonrpc": "2.0", "id": "init", "result": {}},
            {"jsonrpc": "2.0", "id": "other", "result": {"protocolVersion": 1}},
            {"jsonrpc": "2.0", "method": "session/prompt", "id": "prompt", "params": {}},
            {"jsonrpc": "2.0", "id": "prompt", "result": {"stopReason": "end_turn"}},
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "answer"},
                    }
                },
            },
        )
    )

    assert parsed is None
    assert "protocolVersion outside initialize response" in error


def test_parse_stream_rejects_same_direction_request_id_collision():
    parsed, error = acpx_adapter.parse_stream(
        _stream(
            {"jsonrpc": "2.0", "method": "session/new", "id": "same", "params": {}},
            {"jsonrpc": "2.0", "method": "session/prompt", "id": "same", "params": {}},
            {
                "jsonrpc": "2.0",
                "id": "same",
                "result": {"configOptions": [{"id": "model", "value": "forged-model"}], "stopReason": "end_turn"},
            },
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "answer"},
                    }
                },
            },
        )
    )

    assert parsed is None
    assert "duplicate outstanding JSON-RPC request id" in error


@pytest.mark.parametrize(
    "response",
    (
        {"id": "extra", "error": "not-an-error-object"},
        {"id": "extra", "error": {"code": "bad", "message": "failure"}},
        {"id": "extra", "error": {"code": -32000, "message": 4}},
        {"id": True, "error": {"code": -32000, "message": "failure"}},
    ),
)
def test_parse_stream_rejects_invalid_jsonrpc_error_responses(response):
    parsed, error = acpx_adapter.parse_stream(
        _stream(
            {"jsonrpc": "2.0", "method": "session/prompt", "id": "req", "params": {}},
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "answer"}}
                },
            },
            {"jsonrpc": "2.0", "id": "req", "result": {"stopReason": "end_turn"}},
            {"jsonrpc": "2.0", **response},
        )
    )

    assert parsed is None
    assert "malformed JSON-RPC response" in error


def test_parse_stream_accepts_null_id_jsonrpc_error():
    parsed, error = acpx_adapter.parse_stream(
        _stream(
            {"jsonrpc": "2.0", "id": "init-1", "result": {"protocolVersion": 1}},
            {"jsonrpc": "2.0", "method": "session/prompt", "id": "req", "params": {}},
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "answer"}}
                },
            },
            {"jsonrpc": "2.0", "id": "req", "result": {"stopReason": "end_turn"}},
            {"jsonrpc": "2.0", "id": None, "error": {"code": -32000, "message": "provider failed"}},
        )
    )

    assert error == ""
    assert parsed is not None and parsed["has_structured_jsonrpc_error"] is True


@pytest.mark.parametrize(
    "response",
    (
        {"id": "req", "result": {"stopReason": "end_turn"}, "error": {"code": -32072}},
        {"id": "req", "method": "session/prompt", "result": {"stopReason": "end_turn"}},
        {"id": None, "result": {"stopReason": "end_turn"}},
    ),
)
def test_parse_stream_rejects_malformed_jsonrpc_responses(response):
    parsed, error = acpx_adapter.parse_stream(
        _stream(
            {"jsonrpc": "2.0", "method": "session/prompt", "id": "req", "params": {}},
            {"jsonrpc": "2.0", **response},
        )
    )

    assert parsed is None
    assert "malformed JSON-RPC response" in error


def test_parse_stream_drops_unrecognized_event_labels():
    credential = "ghp_FAKE0123456789abcdefghijklmnopqrstuvwxyz"
    parsed, error = acpx_adapter.parse_stream(
        _stream(
            {"jsonrpc": "2.0", "method": "session/prompt", "id": "req", "params": {}},
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {"update": {"sessionUpdate": credential, "status": credential}},
            },
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {"update": {"sessionUpdate": "agent_message_chunk", "status": credential}},
            },
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "safe answer"},
                    }
                },
            },
            {"jsonrpc": "2.0", "id": "req", "result": {"stopReason": "end_turn"}},
        )
    )

    assert error == ""
    assert parsed is not None
    assert parsed["events"] == [{"type": "agent_message_chunk"}, {"type": "agent_message_chunk"}]
    assert all("status" not in event for event in parsed["events"])


@pytest.mark.parametrize("event_type", ("tool_call", "tool_call_update"))
@pytest.mark.parametrize("status", ("pending", "in_progress", "completed", "failed"))
def test_parse_stream_preserves_tool_call_lifecycle_status(event_type, status):
    parsed, error = acpx_adapter.parse_stream(
        _stream(
            {"jsonrpc": "2.0", "method": "session/prompt", "id": "req-1", "params": {}},
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {"update": {"sessionUpdate": event_type, "status": status}},
            },
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "done"},
                    }
                },
            },
            {"jsonrpc": "2.0", "id": "req-1", "result": {"stopReason": "end_turn"}},
        )
    )

    assert error == ""
    assert parsed is not None
    assert parsed["events"][0] == {"type": event_type, "status": status}


def test_parse_stream_retires_config_request_id_without_config_options():
    parsed, error = acpx_adapter.parse_stream(
        _stream(
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {"update": {"sessionUpdate": "config_option_update", "modelId": "right-model"}},
            },
            {"jsonrpc": "2.0", "method": "session/new", "id": "new-1", "params": {}},
            {"jsonrpc": "2.0", "id": "new-1", "result": {}},
            {
                "jsonrpc": "2.0",
                "id": "new-1",
                "result": {"configOptions": [{"id": "model", "category": "model", "currentValue": "wrong-model"}]},
            },
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "hello"},
                    }
                },
            },
        )
    )

    assert error == ""
    assert parsed is not None and parsed["effective_model"] == "right-model"


def test_parse_stream_reads_model_config_from_set_option_response():
    parsed, error = acpx_adapter.parse_stream(
        _stream(
            {"jsonrpc": "2.0", "method": "session/set_config_option", "id": "set-1", "params": {}},
            {
                "jsonrpc": "2.0",
                "id": "set-1",
                "result": {"configOptions": [{"id": "model", "category": "model", "currentValue": "composer-2.5"}]},
            },
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "hello"},
                    }
                },
            },
        )
    )

    assert error == ""
    assert parsed is not None and parsed["effective_model"] == "composer-2.5"
    assert parsed["effective_model_locator"] == "acp-set-config-option-response"


def test_failed_run_preserves_observed_model_and_session_metadata(monkeypatch, tmp_path):
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
                "update": {"sessionUpdate": "config_option_update", "modelId": "composer-2.5"},
            },
        },
        {"jsonrpc": "2.0", "id": "req-1", "error": {"code": -32000, "message": "provider failed"}},
    )
    monkeypatch.setattr(acpx_adapter.proc, "which", lambda cmd: f"/bin/{cmd}")
    monkeypatch.setattr(acpx_adapter, "cursor_auth_status", _authenticated_status)
    outputs = iter([proc.Result(0, "acpx 0.12.0\n", ""), proc.Result(1, stream, "provider failed")])
    monkeypatch.setattr(acpx_adapter.proc, "run", lambda argv, **kwargs: next(outputs))

    result = acpx_adapter.run_cursor(
        "inspect", cwd=tmp_path, timeout=120, model="composer-2.5", version="0.12.0", read_only=True
    )

    assert result.ok is False
    assert result.effective_model == "composer-2.5"
    assert result.protocol_version == 1
    assert result.session_id == "session-1"
    assert result.request_id == "req-1"
    assert result.safe_events


def test_output_limit_failure_preserves_model_from_complete_retained_frames(monkeypatch, tmp_path):
    retained = _stream(
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
    )
    overflow = proc.Result(
        -15,
        retained + proc.TRUNCATION_MARKER + '{"jsonrpc":"2.0"',
        "combined output exceeded capture limit",
        output_limit_exceeded=True,
        stdout_bytes=proc.MAX_CAPTURE_BYTES + 1,
    )
    monkeypatch.setattr(acpx_adapter.proc, "which", lambda cmd: f"/bin/{cmd}")
    monkeypatch.setattr(acpx_adapter, "cursor_auth_status", _authenticated_status)
    outputs = iter([proc.Result(0, "acpx 0.12.0\n", ""), overflow])
    monkeypatch.setattr(acpx_adapter.proc, "run", lambda argv, **kwargs: next(outputs))

    result = acpx_adapter.run_cursor(
        "inspect", cwd=tmp_path, timeout=120, model="composer-2.5", version="0.12.0", read_only=True
    )

    assert result.ok is False
    assert result.failure_kind == "output-limit"
    assert result.effective_model == "composer-2.5"
    assert result.effective_model_locator == "acp-config-option-update"
    assert result.session_id == "session-1"
    assert result.request_id == "req-1"


@pytest.mark.parametrize(
    "message",
    (
        "model unavailable",
        "provider token" + "=" + "ghp_" + "FAKE0123456789abcdefghijklmnopqrstuvwxyz",
        "x" * 300,
    ),
)
def test_run_cursor_uses_sanitized_bounded_structured_error_without_stderr(monkeypatch, tmp_path, message):
    stream = _stream({"jsonrpc": "2.0", "id": None, "error": {"code": -32602, "message": message}})
    monkeypatch.setattr(acpx_adapter.proc, "which", lambda cmd: f"/bin/{cmd}")
    monkeypatch.setattr(acpx_adapter, "cursor_auth_status", _authenticated_status)
    outputs = iter([proc.Result(0, "acpx 0.12.0\n", ""), proc.Result(1, stream, "")])
    monkeypatch.setattr(acpx_adapter.proc, "run", lambda argv, **kwargs: next(outputs))

    result = acpx_adapter.run_cursor(
        "inspect", cwd=tmp_path, timeout=120, model="composer-2.5", version="0.12.0", read_only=True
    )

    assert result.ok is False
    assert result.detail
    assert len(result.detail) <= 200
    if message == "model unavailable":
        assert result.detail == message
    elif "ghp_" in message:
        assert "ghp_" not in result.detail


def test_failed_run_redacts_credential_shaped_provider_diagnostic(monkeypatch, tmp_path):
    credential = "ghp_FAKE0123456789abcdefghijklmnopqrstuvwxyz"
    stream = _stream(
        {"jsonrpc": "2.0", "id": "init-1", "result": {"protocolVersion": 1}},
        {"jsonrpc": "2.0", "method": "session/prompt", "id": "req-1", "params": {}},
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "partial"},
                }
            },
        },
        {"jsonrpc": "2.0", "id": "req-1", "error": {"code": -32000, "message": "provider failed"}},
    )
    monkeypatch.setattr(acpx_adapter.proc, "which", lambda cmd: f"/bin/{cmd}")
    monkeypatch.setattr(acpx_adapter, "cursor_auth_status", _authenticated_status)
    outputs = iter([proc.Result(0, "acpx 0.12.0\n", ""), proc.Result(1, stream, f"provider error {credential}")])
    monkeypatch.setattr(acpx_adapter.proc, "run", lambda argv, **kwargs: next(outputs))

    result = acpx_adapter.run_cursor(
        "inspect", cwd=tmp_path, timeout=120, model="composer-2.5", version="0.12.0", read_only=True
    )

    assert result.ok is False
    assert result.detail == "<redacted diagnostic>"
    assert credential not in result.detail


def test_structured_permission_failure_preserves_prior_model_evidence(monkeypatch, tmp_path):
    stream = _stream(
        {"jsonrpc": "2.0", "id": "init-1", "result": {"protocolVersion": 1}},
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": "session-1",
                "update": {"sessionUpdate": "config_option_update", "modelId": "composer-2.5"},
            },
        },
        {"jsonrpc": "2.0", "id": "perm-1", "error": {"code": -32072, "message": "PERMISSION_PROMPT_UNAVAILABLE"}},
    )
    monkeypatch.setattr(acpx_adapter.proc, "which", lambda cmd: f"/bin/{cmd}")
    monkeypatch.setattr(acpx_adapter, "cursor_auth_status", _authenticated_status)
    outputs = iter([proc.Result(0, "acpx 0.12.0\n", ""), proc.Result(5, stream, "")])
    monkeypatch.setattr(acpx_adapter.proc, "run", lambda argv, **kwargs: next(outputs))

    result = acpx_adapter.run_cursor(
        "inspect", cwd=tmp_path, timeout=120, model="composer-2.5", version="0.12.0", read_only=True
    )

    assert result.ok is False
    assert result.failure_kind == "provider-startup"
    assert result.effective_model == "composer-2.5"
    assert result.effective_model_locator == "acp-config-option-update"


def test_parse_stream_rejects_known_credential_families_as_model_ids():
    credential_models = (
        "ghp_" + "A" * 24,
        "github_pat_" + "A" * 24,
        "xoxb-" + "A" * 16,
        "sk-" + "A" * 24,
        "AKIA" + "A" * 16,
        "ASIA" + "A" * 16,
        "AIza" + "a" * 30,
        "glpat-" + "a" * 20,
        "eyJ" + "A" * 8 + "." + "B" * 8 + "." + "C" * 8,
    )
    for model_id in credential_models:
        parsed, error = acpx_adapter.parse_stream(
            _stream(
                {"jsonrpc": "2.0", "id": "init-1", "result": {"protocolVersion": 1}},
                {
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {"update": {"sessionUpdate": "config_option_update", "modelId": model_id}},
                },
                {
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": "done"},
                        }
                    },
                },
                {"jsonrpc": "2.0", "id": "req-1", "result": {"stopReason": "end_turn"}},
            )
        )
        assert error == ""
        assert parsed is not None and parsed["effective_model"] is None


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
    assert result.transport == "acpx"
    assert result.requested_model == "composer-2.5"


def test_run_missing_executable_and_unsafe_worktree_keep_configured_model(monkeypatch, tmp_path):
    monkeypatch.setattr(acpx_adapter.proc, "which", lambda cmd: None)
    missing = acpx_adapter.run_cursor(
        "inspect", cwd=tmp_path, timeout=120, model="composer-2.5", version="0.12.0", read_only=True
    )
    assert missing.failure_kind == "missing-executable"
    assert missing.transport == "acpx"
    assert missing.requested_model == "composer-2.5"

    monkeypatch.setattr(acpx_adapter.proc, "which", lambda cmd: f"/bin/{cmd}")
    monkeypatch.setattr(acpx_adapter, "cursor_auth_status", _authenticated_status)
    monkeypatch.setattr(acpx_adapter, "installed_version", lambda **kwargs: ("0.12.0", ""))

    def reject_argv(**kwargs):
        raise ValueError("unsafe")

    monkeypatch.setattr(acpx_adapter, "build_argv", reject_argv)
    unsafe = acpx_adapter.run_cursor(
        "inspect", cwd=tmp_path, timeout=120, model="composer-2.5", version="0.12.0", read_only=True
    )
    assert unsafe.failure_kind == "unsafe-worktree"
    assert unsafe.transport == "acpx"
    assert unsafe.requested_model == "composer-2.5"


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


def test_run_parses_large_ndjson_with_non_ascii_final(monkeypatch, tmp_path):
    padding = "x" * 400_000
    final_text = f"{padding} résumé validé — aucun problème."
    stream = _success_stream().replace('"text": "hello"', f'"text": {json.dumps(final_text)}')
    monkeypatch.setattr(acpx_adapter.proc, "which", lambda cmd: f"/bin/{cmd}")
    monkeypatch.setattr(acpx_adapter, "cursor_auth_status", _authenticated_status)
    outputs = iter([proc.Result(0, "acpx 0.12.0\n", ""), proc.Result(0, stream, "")])
    monkeypatch.setattr(acpx_adapter.proc, "run", lambda argv, **kwargs: next(outputs))

    result = acpx_adapter.run_cursor(
        "inspect", cwd=tmp_path, timeout=120, model="composer-2.5", version="0.12.0", read_only=True
    )

    assert result.ok is True
    assert result.text == final_text
    assert result.exit_code == 0


def test_run_handles_absent_decoded_output_without_counting_verdict(monkeypatch, tmp_path):
    monkeypatch.setattr(acpx_adapter.proc, "which", lambda cmd: f"/bin/{cmd}")
    monkeypatch.setattr(acpx_adapter, "cursor_auth_status", _authenticated_status)
    decode_error = "child stdout is not valid UTF-8 (utf-8): 'utf-8' codec can't decode byte 0x9d in position 0"
    outputs = iter(
        [
            proc.Result(0, "acpx 0.12.0\n", ""),
            proc.Result(
                0,
                "",
                decode_error,
                stdout_decode_error=decode_error,
            ),
        ]
    )
    monkeypatch.setattr(acpx_adapter.proc, "run", lambda argv, **kwargs: next(outputs))

    result = acpx_adapter.run_cursor(
        "inspect", cwd=tmp_path, timeout=120, model="composer-2.5", version="0.12.0", read_only=True
    )

    assert result.ok is False
    assert result.text == ""
    assert result.stdout == ""
    assert result.exit_code == 0
    assert result.failure_phase == "harness"
    assert result.failure_kind == "decode-failure"
    assert decode_error in result.detail


def test_run_rejects_invalid_utf8_stderr_with_valid_acp_stdout(monkeypatch, tmp_path):
    monkeypatch.setattr(acpx_adapter.proc, "which", lambda cmd: f"/bin/{cmd}")
    monkeypatch.setattr(acpx_adapter, "cursor_auth_status", _authenticated_status)
    decode_error = "child stderr is not valid UTF-8 (utf-8): 'utf-8' codec can't decode byte 0x9d in position 0"
    outputs = iter(
        [
            proc.Result(0, "acpx 0.12.0\n", ""),
            proc.Result(
                0,
                _success_stream(),
                decode_error,
                stderr_decode_error=decode_error,
            ),
        ]
    )
    monkeypatch.setattr(acpx_adapter.proc, "run", lambda argv, **kwargs: next(outputs))

    result = acpx_adapter.run_cursor(
        "inspect", cwd=tmp_path, timeout=120, model="composer-2.5", version="0.12.0", read_only=True
    )

    assert result.ok is False
    assert result.text == ""
    assert result.exit_code == 0
    assert result.failure_phase == "harness"
    assert result.failure_kind == "decode-failure"
    assert decode_error in result.detail
    assert result.effective_model == "composer-2.5"
    assert result.effective_model_locator == "acp-config-option-update"
    assert result.protocol_version == 1
    assert result.session_id == "session-1"
    assert "hello" not in result.text


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
