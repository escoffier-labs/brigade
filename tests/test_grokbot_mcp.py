"""Contract tests for the role-scoped Grok Bot Streamable HTTP adapter."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from brigade import cli, grokbot_jobs, grokbot_mcp


def _spec(role: str) -> dict[str, object]:
    return {
        "label": f"{role} job",
        "role": role,
        "repository": "example/brigade",
        "base_ref": "main",
        "ownership_paths": ["src/brigade/grokbot_mcp.py"],
        "instructions": "PRIVATE_INSTRUCTIONS_MUST_NOT_LEAK",
        "verification_commands": ["pytest -q tests/test_grokbot_mcp.py"],
        "artifact": {"kind": "report" if role == "repository-scout" else "draft-pr"},
        "timeout_seconds": 900,
    }


def _adapter(tmp_path: Path, role: str = "implementation-worker") -> grokbot_mcp.GrokbotAdapter:
    return grokbot_mcp.GrokbotAdapter(
        grokbot_mcp.ListenerConfig(
            target=tmp_path,
            instance=role,
            bind_host="127.0.0.1",
            bind_port=8766,
            allowed_hosts=(),
            allowed_origins=(),
            bearer="not-a-real-token",
        )
    )


def test_exact_role_tool_inventories_and_hidden_direct_calls_are_indistinguishable(tmp_path: Path):
    expected = {
        "operator": {
            "grokbot_queue_list",
            "grokbot_queue_status",
            "grokbot_queue_cancel",
            "grokbot_queue_expire",
        },
        "repository-scout": {
            "grokbot_queue_list",
            "grokbot_queue_status",
            "grokbot_queue_claim",
            "grokbot_queue_renew",
            "grokbot_queue_start",
            "grokbot_queue_complete",
            "grokbot_queue_fail",
            "grokbot_queue_ack_cancel",
        },
        "implementation-worker": {
            "grokbot_queue_list",
            "grokbot_queue_status",
            "grokbot_queue_claim",
            "grokbot_queue_renew",
            "grokbot_queue_start",
            "grokbot_queue_complete",
            "grokbot_queue_fail",
            "grokbot_queue_ack_cancel",
        },
    }

    for role, tools in expected.items():
        adapter = _adapter(tmp_path, role)
        assert {item["name"] for item in adapter.tool_inventory()} == tools

    operator = _adapter(tmp_path, "operator")
    with pytest.raises(grokbot_mcp.AdapterError) as hidden:
        operator.call_tool("grokbot_queue_claim", {"job_id": "grokbot-" + "a" * 24, "lease_id": "lease-a"})
    with pytest.raises(grokbot_mcp.AdapterError) as unknown:
        operator.call_tool("not-a-real-tool", {})
    assert hidden.value.public_error() == unknown.value.public_error()


def test_workers_only_list_claim_and_read_their_configured_role(tmp_path: Path):
    implementation = grokbot_jobs.enqueue(tmp_path, _spec("implementation-worker"), "implementation-job")["job_id"]
    scout = grokbot_jobs.enqueue(tmp_path, _spec("repository-scout"), "scout-job")["job_id"]
    worker = _adapter(tmp_path)

    listed = worker.call_tool("grokbot_queue_list", {})
    assert [job["job_id"] for job in listed["jobs"]] == [implementation]
    assert "PRIVATE_INSTRUCTIONS_MUST_NOT_LEAK" not in json.dumps(listed)

    with pytest.raises(grokbot_mcp.AdapterError) as unauthorized:
        worker.call_tool("grokbot_queue_claim", {"job_id": scout, "lease_id": "lease-a"})
    assert unauthorized.value.public_error() == {
        "error": {"code": "invalid_request", "message": "Tool input failed validation"}
    }

    claimed = worker.call_tool("grokbot_queue_claim", {"job_id": implementation, "lease_id": "lease-a"})
    assert claimed["state"] == "claimed"
    assert grokbot_jobs.get_job(tmp_path, implementation)["role"] == "implementation-worker"


def test_worker_inputs_cannot_select_target_role_bot_identity_or_lease_duration(tmp_path: Path):
    job_id = grokbot_jobs.enqueue(tmp_path, _spec("implementation-worker"), "implementation-job")["job_id"]
    worker = _adapter(tmp_path)

    for forbidden in ("target", "role", "bot_id", "lease_seconds"):
        with pytest.raises(grokbot_mcp.AdapterError) as error:
            worker.call_tool(
                "grokbot_queue_claim",
                {"job_id": job_id, "lease_id": "lease-a", forbidden: "attacker"},
            )
        assert error.value.public_error()["error"]["code"] == "invalid_request"


def test_safe_error_projection_never_exposes_queue_error_or_private_content(tmp_path: Path):
    adapter = _adapter(tmp_path)
    with pytest.raises(grokbot_mcp.AdapterError) as error:
        adapter.call_tool("grokbot_queue_status", {"job_id": "grokbot-" + "a" * 24})

    rendered = json.dumps(error.value.public_error())
    assert rendered == '{"error": {"code": "invalid_request", "message": "Tool input failed validation"}}'
    assert "PRIVATE" not in rendered
    assert "storage" not in rendered


def test_bearer_comparison_is_constant_time_and_header_shape_is_strict(tmp_path: Path):
    adapter = _adapter(tmp_path)
    assert adapter.authorized("Bearer not-a-real-token") is True
    assert adapter.authorized("Bearer not-a-real-tokeN") is False
    assert adapter.authorized("not-a-real-token") is False
    assert adapter.authorized("Basic not-a-real-token") is False
    assert adapter.authorized("Bearer not-a-real-tok\N{SNOWMAN}") is False


def test_non_ascii_configured_bearer_is_rejected(tmp_path: Path):
    with pytest.raises(grokbot_mcp.ConfigurationError):
        grokbot_mcp.ListenerConfig(
            instance="operator",
            target=tmp_path,
            bind_host="127.0.0.1",
            bind_port=8766,
            allowed_hosts=(),
            allowed_origins=(),
            bearer="not-a-real-tok\N{SNOWMAN}",
        ).validate()


def test_listener_configuration_defaults_to_loopback_and_requires_allowlists_for_real_hostnames(tmp_path: Path):
    assert grokbot_mcp.parse_bind("127.0.0.1:8766") == ("127.0.0.1", 8766)
    with pytest.raises(grokbot_mcp.ConfigurationError):
        grokbot_mcp.ListenerConfig(
            target=tmp_path,
            instance="operator",
            bind_host="mcp.example.test",
            bind_port=8766,
            allowed_hosts=(),
            allowed_origins=(),
            bearer="not-a-real-token",
        ).validate()
    config = grokbot_mcp.ListenerConfig(
        target=tmp_path,
        instance="operator",
        bind_host="mcp.example.test",
        bind_port=8766,
        allowed_hosts=("mcp.example.test",),
        allowed_origins=("https://console.example.test",),
        bearer="not-a-real-token",
    )
    config.validate()


def test_security_gate_checks_host_origin_auth_and_bounded_request_size(tmp_path: Path):
    gate = grokbot_mcp.RequestGate(_adapter(tmp_path).config, max_request_bytes=32)
    assert gate.reject_reason({"host": "127.0.0.1:8766", "authorization": "Bearer not-a-real-token"}, 31) is None
    assert gate.reject_reason({"host": "attacker.test", "authorization": "Bearer not-a-real-token"}, 1) == "forbidden"
    assert (
        gate.reject_reason(
            {"host": "127.0.0.1", "origin": "https://attacker.test", "authorization": "Bearer not-a-real-token"}, 1
        )
        == "forbidden"
    )
    assert gate.reject_reason({"host": "127.0.0.1", "authorization": "Bearer wrong"}, 1) == "unauthorized"
    assert gate.reject_reason({"host": "127.0.0.1", "authorization": "Bearer not-a-real-token"}, 33) == "too-large"


def test_health_output_is_minimal_and_never_includes_config_or_queue_data(tmp_path: Path):
    job_id = grokbot_jobs.enqueue(tmp_path, _spec("implementation-worker"), "implementation-job")["job_id"]
    payload = _adapter(tmp_path).health_payload()

    assert payload == {"ok": True, "service": "grokbot-mcp", "role": "implementation-worker"}
    rendered = json.dumps(payload)
    for forbidden in (job_id, "target", "bearer", "host", "error", "PRIVATE"):
        assert forbidden not in rendered


def test_streamable_http_app_enforces_bearer_origin_and_body_limit(tmp_path: Path):
    pytest.importorskip("mcp.server", reason="requires the grokbot extra")
    TestClient = pytest.importorskip("starlette.testclient", reason="requires the grokbot extra").TestClient

    with TestClient(grokbot_mcp.build_app(_adapter(tmp_path).config), base_url="http://127.0.0.1:8766") as client:
        assert client.get("/health").status_code == 401
        response = client.get("/health", headers={"authorization": "Bearer not-a-real-token"})
        assert response.status_code == 200
        assert response.json() == {"ok": True, "service": "grokbot-mcp", "role": "implementation-worker"}
        assert (
            client.post(
                "/mcp",
                content=b"x" * (grokbot_mcp.MAX_REQUEST_BYTES + 1),
                headers={"authorization": "Bearer not-a-real-token", "content-type": "application/json"},
            ).status_code
            == 413
        )
        assert (
            client.post(
                "/mcp",
                json={},
                headers={"authorization": "Bearer not-a-real-token", "origin": "https://attacker.test"},
            ).status_code
            == 403
        )


def test_asgi_rejects_non_ascii_authorization_without_reaching_the_app(tmp_path: Path):
    adapter = _adapter(tmp_path)

    async def downstream(scope, receive, send):
        raise AssertionError("unauthorized request reached downstream")

    async def request() -> list[dict[str, object]]:
        sent: list[dict[str, object]] = []

        async def receive() -> dict[str, object]:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: dict[str, object]) -> None:
            sent.append(message)

        await grokbot_mcp._GateASGI(downstream, grokbot_mcp.RequestGate(adapter.config), adapter)(
            {
                "type": "http",
                "method": "GET",
                "path": "/health",
                "headers": [(b"host", b"127.0.0.1:8766"), (b"authorization", b"Bearer not-a-real-tok\xff")],
            },
            receive,
            send,
        )
        return sent

    sent = asyncio.run(request())
    assert sent[0]["status"] in {401, 403}


def test_gate_bounded_reads_every_http_method_and_preserves_bodyless_requests(tmp_path: Path):
    adapter = _adapter(tmp_path)
    delivered: list[tuple[str, list[dict[str, object]]]] = []

    async def downstream(scope, receive, send):
        messages = []
        while True:
            message = await receive()
            messages.append(message)
            if message.get("type") == "http.disconnect":
                break
        delivered.append((scope["method"], messages))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def request(method: str, messages: list[dict[str, object]]) -> list[dict[str, object]]:
        pending = iter(messages)
        sent: list[dict[str, object]] = []

        async def receive():
            return next(pending, {"type": "http.disconnect"})

        async def send(message):
            sent.append(message)

        await grokbot_mcp._GateASGI(downstream, grokbot_mcp.RequestGate(adapter.config), adapter)(
            {
                "type": "http",
                "method": method,
                "path": "/health",
                "headers": [
                    (b"host", b"127.0.0.1:8766"),
                    (b"authorization", b"Bearer not-a-real-token"),
                ],
            },
            receive,
            send,
        )
        return sent

    for method in ("GET", "DELETE"):
        sent = asyncio.run(
            request(
                method,
                [
                    {"type": "http.request", "body": b"x" * grokbot_mcp.MAX_REQUEST_BYTES, "more_body": True},
                    {"type": "http.request", "body": b"x", "more_body": False},
                ],
            )
        )
        assert sent[0]["status"] == 413

    for method in ("GET", "DELETE"):
        sent = asyncio.run(request(method, [{"type": "http.request", "body": b"", "more_body": False}]))
        assert sent[0]["status"] == 200
    assert [method for method, _ in delivered] == ["GET", "DELETE"]


def test_loopback_canary_path_keeps_sdk_loopback_hosts_with_a_public_allowlist(tmp_path: Path):
    pytest.importorskip("mcp.server", reason="requires the grokbot extra")
    TestClient = pytest.importorskip("starlette.testclient", reason="requires the grokbot extra").TestClient

    config = grokbot_mcp.ListenerConfig(
        target=tmp_path,
        instance="implementation-worker",
        bind_host="127.0.0.1",
        bind_port=8766,
        allowed_hosts=("mcp.example.test",),
        allowed_origins=(),
        bearer="not-a-real-token",
    )
    non_loopback = grokbot_mcp.ListenerConfig(
        target=tmp_path,
        instance="implementation-worker",
        bind_host="mcp.example.test",
        bind_port=8766,
        allowed_hosts=("mcp.example.test",),
        allowed_origins=("https://console.example.test",),
        bearer="not-a-real-token",
    )
    assert set(grokbot_mcp._transport_allowed_hosts(config)) >= {
        "localhost",
        "localhost:*",
        "127.0.0.1",
        "127.0.0.1:*",
        "::1",
        "[::1]:*",
    }
    assert grokbot_mcp._transport_allowed_hosts(non_loopback) == ["mcp.example.test"]

    with TestClient(grokbot_mcp.build_app(config), base_url="http://127.0.0.1:8766") as client:
        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "grokbot_queue_list", "arguments": {}},
            },
            headers={"authorization": "Bearer not-a-real-token"},
        )
    assert response.status_code == 200
    assert response.json()["result"]["isError"] is False


def test_mcp_asgi_rejects_extra_and_malformed_raw_tool_arguments(tmp_path: Path):
    pytest.importorskip("mcp.server", reason="requires the grokbot extra")
    TestClient = pytest.importorskip("starlette.testclient", reason="requires the grokbot extra").TestClient

    headers = {"authorization": "Bearer not-a-real-token", "content-type": "application/json"}
    invalid_calls = (
        ("grokbot_queue_list", {"bot_id": "caller-selected"}),
        ("grokbot_queue_status", {"job_id": []}),
        ("grokbot_queue_complete", {"job_id": "grokbot-" + "a" * 24, "lease_id": "lease-a", "artifact": []}),
    )
    with TestClient(grokbot_mcp.build_app(_adapter(tmp_path).config), base_url="http://127.0.0.1:8766") as client:
        for name, arguments in invalid_calls:
            response = client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                },
                headers=headers,
            )
            assert response.status_code == 400
            assert response.json() == {
                "jsonrpc": "2.0",
                "id": 1,
                "error": {"code": -32602, "message": "Invalid request"},
            }

        valid = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "grokbot_queue_list", "arguments": {}},
            },
            headers=headers,
        )
        omitted_arguments = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "grokbot_queue_list"},
            },
            headers=headers,
        )
    assert valid.status_code == 200
    assert valid.json()["result"]["isError"] is False
    assert omitted_arguments.status_code == 200
    assert omitted_arguments.json()["result"]["isError"] is False


def test_cli_serve_reports_missing_optional_extra_without_printing_secret(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("TEST_GROKBOT_BEARER", "not-a-real-token")
    monkeypatch.setattr(grokbot_mcp, "_load_mcp", lambda: (_ for _ in ()).throw(grokbot_mcp.OptionalDependencyError()))

    assert (
        cli.main(
            [
                "run",
                "cloud",
                "grokbot",
                "serve",
                "--instance",
                "operator",
                "--target",
                str(tmp_path),
                "--bearer-env",
                "TEST_GROKBOT_BEARER",
            ]
        )
        == 2
    )
    captured = capsys.readouterr()
    assert "pip install brigade-cli[grokbot]" in captured.err
    assert "not-a-real-token" not in captured.err + captured.out
