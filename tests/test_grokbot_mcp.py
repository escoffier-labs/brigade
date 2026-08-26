"""Contract tests for the role-scoped Grok Bot Streamable HTTP adapter."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from brigade import cli, fleet_client, grokbot_jobs, grokbot_mcp


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


def test_workers_only_list_claim_and_read_their_configured_role(tmp_path: Path, monkeypatch):
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
    assert grokbot_jobs.get_job(tmp_path, scout)["state"] == "queued"

    monkeypatch.setattr(
        fleet_client,
        "admit_cloud",
        lambda *args, **kwargs: fleet_client.CloudDecision(True, "ok", lease={"lease_id": implementation}),
    )
    monkeypatch.setattr(fleet_client, "bind_cloud", lambda *args, **kwargs: fleet_client.CloudDecision(True, "ok"))
    claimed = worker.call_tool("grokbot_queue_claim", {"job_id": implementation, "lease_id": "lease-a"})
    assert claimed["state"] == "claimed"
    assert claimed["execution_context"] == _spec("implementation-worker")
    assert "PRIVATE_INSTRUCTIONS_MUST_NOT_LEAK" not in json.dumps(listed)
    assert "execution_context" not in worker.call_tool("grokbot_queue_status", {"job_id": implementation})
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


def test_claim_is_admitted_before_queue_mutation_and_binds_the_deterministic_hub_lease(tmp_path: Path, monkeypatch):
    job_id = grokbot_jobs.enqueue(tmp_path, _spec("implementation-worker"), "implementation-job")["job_id"]
    adapter = _adapter(tmp_path)
    calls: list[tuple[str, dict[str, object]]] = []

    def admit(provider: str, **kwargs: object) -> fleet_client.CloudDecision:
        calls.append(("admit", {"provider": provider, **kwargs}))
        assert grokbot_jobs.get_job(tmp_path, job_id)["state"] == "queued"
        return fleet_client.CloudDecision(True, "ok", lease={"lease_id": job_id})

    def bind(lease_id: str, provider_task_id: str, **kwargs: object) -> fleet_client.CloudDecision:
        calls.append(("bind", {"lease_id": lease_id, "provider_task_id": provider_task_id, **kwargs}))
        return fleet_client.CloudDecision(True, "ok")

    monkeypatch.setattr(fleet_client, "admit_cloud", admit)
    monkeypatch.setattr(fleet_client, "bind_cloud", bind)

    claimed = adapter.call_tool("grokbot_queue_claim", {"job_id": job_id, "lease_id": "lease-a"})

    assert claimed["state"] == "claimed"
    assert [call[0] for call in calls] == ["admit", "bind"]
    admitted = calls[0][1]
    assert admitted == {
        "provider": "grokbot-cloud",
        "repo": "example/brigade",
        "label": f"grokbot-cloud:example/brigade@{claimed['task_hash'].removeprefix('sha256:')[:12]}",
        "prompt_hash": claimed["task_hash"].removeprefix("sha256:"),
        "conductor": "implementation-worker",
        "ttl_seconds": grokbot_mcp.LEASE_SECONDS,
        "lease_id": job_id,
        "holder": "lease-a",
    }
    assert calls[1][1] == {"lease_id": job_id, "provider_task_id": job_id, "holder": "lease-a"}


def test_claim_denial_leaves_the_local_job_queued(tmp_path: Path, monkeypatch):
    job_id = grokbot_jobs.enqueue(tmp_path, _spec("implementation-worker"), "implementation-job")["job_id"]
    monkeypatch.setattr(
        fleet_client,
        "admit_cloud",
        lambda *args, **kwargs: fleet_client.CloudDecision(False, "refused"),
    )
    monkeypatch.setattr(fleet_client, "bind_cloud", lambda *args, **kwargs: pytest.fail("bind must not run"))

    with pytest.raises(grokbot_mcp.AdapterError):
        _adapter(tmp_path).call_tool("grokbot_queue_claim", {"job_id": job_id, "lease_id": "lease-a"})

    assert grokbot_jobs.get_job(tmp_path, job_id)["state"] == "queued"


def test_claim_releases_an_admitted_hub_lease_when_the_local_claim_fails(tmp_path: Path, monkeypatch):
    job_id = grokbot_jobs.enqueue(tmp_path, _spec("implementation-worker"), "implementation-job")["job_id"]
    releases: list[dict[str, object]] = []
    monkeypatch.setattr(
        fleet_client,
        "admit_cloud",
        lambda *args, **kwargs: fleet_client.CloudDecision(True, "ok", lease={"lease_id": job_id}),
    )
    monkeypatch.setattr(
        grokbot_jobs,
        "claim_execution_context",
        lambda *args, **kwargs: (_ for _ in ()).throw(grokbot_jobs.GrokbotJobError("lease-conflict")),
    )
    monkeypatch.setattr(
        fleet_client,
        "release_cloud",
        lambda lease_id, **kwargs: (
            releases.append({"lease_id": lease_id, **kwargs}) or fleet_client.CloudDecision(True, "ok")
        ),
    )

    with pytest.raises(grokbot_mcp.AdapterError):
        _adapter(tmp_path).call_tool("grokbot_queue_claim", {"job_id": job_id, "lease_id": "lease-a"})

    assert grokbot_jobs.get_job(tmp_path, job_id)["state"] == "queued"
    assert releases == [{"lease_id": job_id, "state": "released", "holder": "lease-a"}]


def test_renew_reconciles_a_legacy_running_job_and_normal_renewals_refresh_the_hub(tmp_path: Path, monkeypatch):
    job_id = grokbot_jobs.enqueue(tmp_path, _spec("implementation-worker"), "implementation-job")["job_id"]
    adapter = _adapter(tmp_path)
    grokbot_jobs.claim(tmp_path, job_id, adapter.config.bot_id, "lease-a", grokbot_mcp.LEASE_SECONDS)
    grokbot_jobs.transition(tmp_path, job_id, adapter.config.bot_id, "lease-a", "running")
    calls: list[tuple[str, dict[str, object]]] = []

    def renew(lease_id: str, **kwargs: object) -> fleet_client.CloudDecision:
        calls.append(("renew", {"lease_id": lease_id, **kwargs}))
        return fleet_client.CloudDecision(False, "refused")

    def admit(provider: str, **kwargs: object) -> fleet_client.CloudDecision:
        calls.append(("admit", {"provider": provider, **kwargs}))
        return fleet_client.CloudDecision(True, "ok", lease={"lease_id": job_id})

    def bind(lease_id: str, provider_task_id: str, **kwargs: object) -> fleet_client.CloudDecision:
        calls.append(("bind", {"lease_id": lease_id, "provider_task_id": provider_task_id, **kwargs}))
        return fleet_client.CloudDecision(True, "ok")

    monkeypatch.setattr(fleet_client, "renew_cloud", renew)
    monkeypatch.setattr(fleet_client, "admit_cloud", admit)
    monkeypatch.setattr(fleet_client, "bind_cloud", bind)

    renewed = adapter.call_tool("grokbot_queue_renew", {"job_id": job_id, "lease_id": "lease-a"})

    assert renewed["state"] == "running"
    assert [name for name, _ in calls] == ["renew", "admit", "bind"]
    assert calls[0][1] == {"lease_id": job_id, "ttl_seconds": grokbot_mcp.LEASE_SECONDS, "holder": "lease-a"}
    assert calls[-1][1] == {"lease_id": job_id, "provider_task_id": job_id, "holder": "lease-a"}

    calls.clear()
    monkeypatch.setattr(
        fleet_client,
        "renew_cloud",
        lambda lease_id, **kwargs: (
            calls.append(("renew", {"lease_id": lease_id, **kwargs})) or fleet_client.CloudDecision(True, "ok")
        ),
    )
    adapter.call_tool("grokbot_queue_renew", {"job_id": job_id, "lease_id": "lease-a"})
    assert [name for name, _ in calls] == ["renew"]


def test_start_reconciles_a_missing_hub_lease_without_rolling_back_the_local_transition(tmp_path: Path, monkeypatch):
    job_id = grokbot_jobs.enqueue(tmp_path, _spec("implementation-worker"), "implementation-job")["job_id"]
    adapter = _adapter(tmp_path)
    grokbot_jobs.claim(tmp_path, job_id, adapter.config.bot_id, "lease-a", grokbot_mcp.LEASE_SECONDS)
    calls: list[str] = []
    monkeypatch.setattr(
        fleet_client,
        "renew_cloud",
        lambda *args, **kwargs: calls.append("renew") or fleet_client.CloudDecision(False, "refused"),
    )
    monkeypatch.setattr(
        fleet_client,
        "admit_cloud",
        lambda *args, **kwargs: (
            calls.append("admit") or fleet_client.CloudDecision(True, "ok", lease={"lease_id": job_id})
        ),
    )
    monkeypatch.setattr(
        fleet_client,
        "bind_cloud",
        lambda *args, **kwargs: calls.append("bind") or fleet_client.CloudDecision(True, "ok"),
    )

    started = adapter.call_tool("grokbot_queue_start", {"job_id": job_id, "lease_id": "lease-a"})

    assert started["state"] == "running"
    assert calls == ["renew", "admit", "bind"]


@pytest.mark.parametrize(
    ("tool", "arguments", "prepare", "expected_state"),
    [
        ("grokbot_queue_fail", {}, None, "failed"),
        (
            "grokbot_queue_complete",
            {
                "artifact": {
                    "kind": "draft-pr",
                    "url": "https://github.com/example/brigade/pull/123",
                    "branch": "grokbot/job-1",
                }
            },
            "running",
            "completed",
        ),
        ("grokbot_queue_ack_cancel", {}, "cancel", "canceled"),
    ],
)
def test_terminal_worker_transitions_release_hub_leases(
    tmp_path: Path, monkeypatch, tool: str, arguments: dict[str, object], prepare: str | None, expected_state: str
):
    job_id = grokbot_jobs.enqueue(tmp_path, _spec("implementation-worker"), "implementation-job")["job_id"]
    adapter = _adapter(tmp_path)
    grokbot_jobs.claim(tmp_path, job_id, adapter.config.bot_id, "lease-a", grokbot_mcp.LEASE_SECONDS)
    if prepare == "running":
        grokbot_jobs.transition(tmp_path, job_id, adapter.config.bot_id, "lease-a", "running")
    elif prepare == "cancel":
        grokbot_jobs.cancel(tmp_path, job_id)
    releases: list[dict[str, object]] = []
    monkeypatch.setattr(
        fleet_client,
        "release_cloud",
        lambda lease_id, **kwargs: (
            releases.append({"lease_id": lease_id, **kwargs}) or fleet_client.CloudDecision(True, "ok")
        ),
    )

    result = adapter.call_tool(tool, {"job_id": job_id, "lease_id": "lease-a", **arguments})

    assert result["state"] == expected_state
    assert releases == [{"lease_id": job_id, "state": expected_state, "holder": "lease-a"}]


def test_hub_mirror_failures_do_not_undo_a_local_terminal_transition_or_leak_private_values(
    tmp_path: Path, monkeypatch
):
    job_id = grokbot_jobs.enqueue(tmp_path, _spec("implementation-worker"), "implementation-job")["job_id"]
    adapter = _adapter(tmp_path)
    grokbot_jobs.claim(tmp_path, job_id, adapter.config.bot_id, "lease-a", grokbot_mcp.LEASE_SECONDS)
    monkeypatch.setattr(
        fleet_client,
        "release_cloud",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("lease-a PRIVATE_INSTRUCTIONS_MUST_NOT_LEAK")),
    )

    failed = adapter.call_tool("grokbot_queue_fail", {"job_id": job_id, "lease_id": "lease-a"})

    assert failed["state"] == "failed"
    projection = _adapter(tmp_path).call_tool("grokbot_queue_status", {"job_id": job_id})
    rendered = json.dumps(projection)
    assert "lease-a" not in rendered
    assert "PRIVATE_INSTRUCTIONS_MUST_NOT_LEAK" not in rendered


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
