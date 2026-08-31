"""Tests for Grok Bot setup, doctor, canary, and systemd service helpers."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from brigade import cli, grokbot_jobs, grokbot_mcp, grokbot_ops

LISTENER_RECOVERY_UNIT_LINES = ("StartLimitIntervalSec=15min", "StartLimitBurst=3")
LISTENER_RECOVERY_SERVICE_LINES = (
    "Restart=on-failure",
    "RestartSec=60s",
    "RestartPreventExitStatus=2",
)
_SYSTEMD_MUTATION_VERBS = frozenset(
    {"start", "stop", "restart", "enable", "disable", "reload", "daemon-reload", "isolate", "kill"}
)


def systemd_sections(unit: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in unit.splitlines():
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            sections[current] = []
            continue
        if current is not None:
            sections[current].append(line)
    return {name: "\n".join(lines) for name, lines in sections.items()}


def assert_listener_recovery_policy(unit: str) -> None:
    sections = systemd_sections(unit)
    unit_lines = [line for line in sections["Unit"].splitlines() if line]
    service_lines = [line for line in sections["Service"].splitlines() if line]
    for line in LISTENER_RECOVERY_UNIT_LINES:
        assert line in unit_lines
        assert line not in service_lines
    for line in LISTENER_RECOVERY_SERVICE_LINES:
        assert line in service_lines
        assert line not in unit_lines


SECRET = "not-a-real-token"


def _spec(role: str) -> dict[str, object]:
    return {
        "label": f"{role} job",
        "role": role,
        "repository": "example/brigade",
        "base_ref": "main",
        "ownership_paths": ["src/brigade/grokbot_mcp.py"],
        "instructions": "PRIVATE_INSTRUCTIONS_MUST_NOT_LEAK",
        "verification_commands": ["true"],
        "artifact": {"kind": "draft-pr"},
        "timeout_seconds": 900,
    }


def _setup(tmp_path: Path, instance: str = "operator", extra: list[str] | None = None) -> int:
    argv = [
        "run",
        "cloud",
        "grokbot",
        "setup",
        "--target",
        str(tmp_path),
        "--instance",
        instance,
        "--bearer-env",
        "TEST_GROKBOT_BEARER",
    ] + (extra or [])
    return cli.main(argv)


def test_setup_writes_role_scoped_config_without_secret_values(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_GROKBOT_BEARER", SECRET)

    assert _setup(tmp_path) == 0

    path = grokbot_ops.config_path(tmp_path, "operator")
    assert path.is_file()
    assert path.stat().st_mode & 0o777 == 0o600
    rendered = json.dumps(json.loads(path.read_text(encoding="utf-8")))
    assert SECRET not in rendered
    assert "TEST_GROKBOT_BEARER" in rendered
    config = grokbot_ops.load_config(tmp_path, "operator")
    assert config["instance"] == "operator"
    assert config["bind"] == "127.0.0.1:8766"
    assert config["bearer"] == {"kind": "env", "name": "TEST_GROKBOT_BEARER"}
    assert (tmp_path / grokbot_ops.QUEUE_STATE_DIR).is_dir()


def test_setup_supports_all_roles_and_secret_file_references(tmp_path: Path):
    token_file = tmp_path / "token.txt"
    token_file.write_text(SECRET + "\n", encoding="utf-8")
    token_file.chmod(0o600)

    for role in ("operator", "repository-scout", "implementation-worker"):
        assert (
            cli.main(
                [
                    "run",
                    "cloud",
                    "grokbot",
                    "setup",
                    "--target",
                    str(tmp_path),
                    "--instance",
                    role,
                    "--bearer-file",
                    str(token_file),
                ]
            )
            == 0
        )
        config = grokbot_ops.load_config(tmp_path, role)
        assert config["bearer"]["kind"] == "file"


def test_setup_stores_hub_token_file_reference_and_renders_role_environment(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("TEST_GROKBOT_BEARER", SECRET)
    hub_token = tmp_path / "listener.hub-token"
    hub_token.write_text("hub-node-token-value\n", encoding="utf-8")
    hub_token.chmod(0o600)

    assert (
        cli.main(
            [
                "run",
                "cloud",
                "grokbot",
                "setup",
                "--target",
                str(tmp_path),
                "--instance",
                "implementation-worker",
                "--bearer-env",
                "TEST_GROKBOT_BEARER",
                "--hub-token-file",
                str(hub_token),
            ]
        )
        == 0
    )
    config = grokbot_ops.load_config(tmp_path, "implementation-worker")
    assert config["hub_token_file"] == str(hub_token)
    rendered_config = json.dumps(config, sort_keys=True)
    assert "hub-node-token-value" not in rendered_config
    assert SECRET not in rendered_config

    assert (
        cli.main(
            [
                "run",
                "cloud",
                "grokbot",
                "install-service",
                "--target",
                str(tmp_path),
                "--instance",
                "implementation-worker",
            ]
        )
        == 0
    )
    unit = capsys.readouterr().out
    assert f"BRIGADE_GROKBOT_IMPLEMENTATION_WORKER_HUB_TOKEN_FILE={hub_token}" in unit
    assert "hub-node-token-value" not in unit
    assert "BRIGADE_FLEET_NODE_TOKEN" not in unit

    hub_token.chmod(0o644)
    with pytest.raises(grokbot_mcp.ConfigurationError):
        grokbot_ops.save_config(
            tmp_path,
            instance="repository-scout",
            bind="127.0.0.1:8767",
            allowed_hosts=[],
            allowed_origins=[],
            bearer_env="TEST_GROKBOT_BEARER",
            bearer_file=None,
            hub_token_file=hub_token,
        )
    with pytest.raises(grokbot_mcp.ConfigurationError):
        grokbot_ops.save_config(
            tmp_path,
            instance="repository-scout",
            bind="127.0.0.1:8767",
            allowed_hosts=[],
            allowed_origins=[],
            bearer_env="TEST_GROKBOT_BEARER",
            bearer_file=None,
            hub_token_file=Path("relative.hub-token"),
        )


def test_setup_with_hub_authority_does_not_require_queue_status(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_GROKBOT_BEARER", SECRET)
    hub_token = tmp_path / "listener.hub-token"
    hub_token.write_text("hub-node-token-value\n", encoding="utf-8")
    hub_token.chmod(0o600)
    grokbot_jobs.admit_hub_authority(tmp_path)

    def fail_status(_target: Path) -> None:
        raise grokbot_jobs.GrokbotJobError("hub-authentication-failed")

    monkeypatch.setattr(grokbot_jobs, "status", fail_status)

    assert _setup(tmp_path, "implementation-worker", ["--hub-token-file", str(hub_token)]) == 0
    config = grokbot_ops.load_config(tmp_path, "implementation-worker")
    assert config["hub_token_file"] == str(hub_token)


def test_operator_setup_renders_its_own_hub_actor_token_reference(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("TEST_GROKBOT_BEARER", SECRET)
    hub_token = tmp_path / "operator.hub-token"
    hub_token.write_text("operator-node-token\n", encoding="utf-8")
    hub_token.chmod(0o600)

    assert (
        cli.main(
            [
                "run",
                "cloud",
                "grokbot",
                "setup",
                "--target",
                str(tmp_path),
                "--instance",
                "operator",
                "--bearer-env",
                "TEST_GROKBOT_BEARER",
                "--hub-token-file",
                str(hub_token),
            ]
        )
        == 0
    )
    assert (
        cli.main(
            [
                "run",
                "cloud",
                "grokbot",
                "install-service",
                "--target",
                str(tmp_path),
                "--instance",
                "operator",
            ]
        )
        == 0
    )
    unit = capsys.readouterr().out
    assert f"BRIGADE_GROKBOT_OPERATOR_HUB_TOKEN_FILE={hub_token}" in unit
    assert "operator-node-token" not in unit


def test_setup_rejects_bad_instance_and_bad_secret_reference_shape(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_GROKBOT_BEARER", SECRET)
    with pytest.raises(SystemExit):
        _setup(tmp_path, instance="not-a-role")
    assert (
        cli.main(
            [
                "run",
                "cloud",
                "grokbot",
                "setup",
                "--target",
                str(tmp_path),
                "--instance",
                "operator",
                "--bearer-env",
                "not a valid name",
            ]
        )
        == 2
    )
    assert not grokbot_ops.config_path(tmp_path, "operator").exists()


def test_setup_stores_secret_reference_even_when_bearer_is_not_yet_present(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("TEST_GROKBOT_BEARER", raising=False)
    assert _setup(tmp_path) == 0
    assert grokbot_ops.load_config(tmp_path, "operator")["bearer"]["name"] == "TEST_GROKBOT_BEARER"


def test_doctor_reports_sanitized_checks_without_private_content(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("TEST_GROKBOT_BEARER", SECRET)
    assert _setup(tmp_path) == 0
    job_id = grokbot_jobs.enqueue(tmp_path, _spec("implementation-worker"), "worker-job")["job_id"]

    assert cli.main(["run", "cloud", "grokbot", "doctor", "--target", str(tmp_path), "--instance", "operator"]) == 1

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "config" in combined and "queue" in combined
    assert "endpoint" in combined
    assert SECRET not in combined and job_id not in combined
    assert "PRIVATE_INSTRUCTIONS_MUST_NOT_LEAK" not in combined


@pytest.mark.parametrize("instance", ("operator", "repository-scout", "implementation-worker"))
def test_doctor_uses_configured_listener_actor_for_hub_queue_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, instance: str
):
    from brigade import fleet_client_grokbot

    monkeypatch.setenv("TEST_GROKBOT_BEARER", SECRET)
    token = f"{instance}-listener-token"
    token_file = tmp_path / f"{instance}.hub-token"
    token_file.write_text(token + "\n", encoding="utf-8")
    token_file.chmod(0o600)
    assert _setup(tmp_path, instance, ["--hub-token-file", str(token_file)]) == 0

    seen: list[str | None] = []
    monkeypatch.setattr(
        grokbot_jobs,
        "status",
        lambda _target: seen.append(fleet_client_grokbot.current_listener_token()) or {"jobs": []},
    )

    checks = grokbot_ops.doctor(tmp_path, instance)

    assert {check["check"]: check["status"] for check in checks}["queue"] == "ok"
    assert seen == [token]


def test_doctor_keeps_host_identity_without_configured_listener_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from brigade import fleet_client_grokbot

    monkeypatch.setenv("TEST_GROKBOT_BEARER", SECRET)
    assert _setup(tmp_path, "operator") == 0
    seen: list[str | None] = []
    monkeypatch.setattr(
        grokbot_jobs,
        "status",
        lambda _target: seen.append(fleet_client_grokbot.current_listener_token()) or {"jobs": []},
    )

    checks = grokbot_ops.doctor(tmp_path, "operator")

    assert {check["check"]: check["status"] for check in checks}["queue"] == "ok"
    assert seen == [None]


def test_doctor_refuses_changed_unsafe_listener_token_file_without_secret_or_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("TEST_GROKBOT_BEARER", SECRET)
    token = SECRET
    token_file = tmp_path / "listener.hub-token"
    token_file.write_text(token + "\n", encoding="utf-8")
    token_file.chmod(0o600)
    assert _setup(tmp_path, "operator", ["--hub-token-file", str(token_file)]) == 0
    token_file.chmod(0o640)
    status_calls: list[object] = []
    monkeypatch.setattr(grokbot_jobs, "status", lambda _target: status_calls.append(object()) or {"jobs": []})

    checks = grokbot_ops.doctor(tmp_path, "operator")

    statuses = {check["check"]: check["status"] for check in checks}
    assert statuses["config"] == "fail"
    assert "queue" not in statuses
    assert status_calls == []
    assert token not in json.dumps(checks)
    assert str(token_file) not in json.dumps(checks)


def _feed_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    token = "feed-listener-token"
    token_file = tmp_path / "feed.hub-token"
    token_file.write_text(token + "\n", encoding="utf-8")
    token_file.chmod(0o600)
    monkeypatch.setenv("BRIGADE_GROKBOT_FEED_HUB_TOKEN_FILE", str(token_file))
    return token


def _stub_hub(monkeypatch: pytest.MonkeyPatch, *, actor_kind: str = "feed", list_granted: bool = True) -> list[str]:
    from brigade import fleet_client_grokbot

    actions: list[str] = []

    def whoami(**_fields: object):
        actions.append("whoami")
        return fleet_client_grokbot.GrokbotHubDecision(True, "ok", job={"actor_kind": actor_kind, "role": None})

    def list_jobs(**_fields: object):
        actions.append("list")
        if not list_granted:
            return fleet_client_grokbot.GrokbotHubDecision(False, "auth-failed")
        return fleet_client_grokbot.GrokbotHubDecision(True, "ok", jobs=[])

    monkeypatch.setattr(fleet_client_grokbot, "whoami", whoami)
    monkeypatch.setattr(fleet_client_grokbot, "list_jobs", list_jobs)
    monkeypatch.setattr(grokbot_jobs, "hub_authority", lambda _target: True)
    monkeypatch.setattr(grokbot_jobs, "status", lambda _target: {"jobs": []})
    return actions


def test_doctor_reports_feed_authority_ok_when_feed_can_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TEST_GROKBOT_BEARER", SECRET)
    assert _setup(tmp_path, "operator") == 0
    token = _feed_token(tmp_path, monkeypatch)
    _stub_hub(monkeypatch)

    checks = grokbot_ops.doctor(tmp_path, "operator")

    assert {check["check"]: check["status"] for check in checks}["feed-authority"] == "ok"
    assert token not in json.dumps(checks)


def test_doctor_reports_feed_authority_fail_when_feed_list_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TEST_GROKBOT_BEARER", SECRET)
    assert _setup(tmp_path, "operator") == 0
    _feed_token(tmp_path, monkeypatch)
    _stub_hub(monkeypatch, list_granted=False)

    checks = grokbot_ops.doctor(tmp_path, "operator")

    assert {check["check"]: check["status"] for check in checks}["feed-authority"] == "fail"


def test_doctor_skips_feed_authority_without_a_configured_feed_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TEST_GROKBOT_BEARER", SECRET)
    monkeypatch.delenv("BRIGADE_GROKBOT_FEED_HUB_TOKEN_FILE", raising=False)
    assert _setup(tmp_path, "operator") == 0
    actions = _stub_hub(monkeypatch)

    checks = grokbot_ops.doctor(tmp_path, "operator")

    assert {check["check"]: check["status"] for check in checks}["feed-authority"] == "skipped"
    assert actions == []


def test_doctor_omits_feed_authority_without_hub_authority(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TEST_GROKBOT_BEARER", SECRET)
    assert _setup(tmp_path, "operator") == 0
    _feed_token(tmp_path, monkeypatch)
    actions = _stub_hub(monkeypatch)
    monkeypatch.setattr(grokbot_jobs, "hub_authority", lambda _target: False)

    checks = grokbot_ops.doctor(tmp_path, "operator")

    assert "feed-authority" not in {check["check"] for check in checks}
    assert actions == []


def test_doctor_feed_authority_probe_issues_no_mutating_hub_action(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from brigade import fleet_hub_grokbot

    monkeypatch.setenv("TEST_GROKBOT_BEARER", SECRET)
    assert _setup(tmp_path, "operator") == 0
    _feed_token(tmp_path, monkeypatch)
    actions = _stub_hub(monkeypatch)

    grokbot_ops.doctor(tmp_path, "operator")

    assert set(actions) == {"whoami", "list"}
    assert not set(actions) & fleet_hub_grokbot.MUTATING_ACTIONS


@pytest.mark.parametrize(
    ("feed_status", "exit_code"),
    (("skipped", 0), ("ok", 0), ("fail", 1)),
)
def test_doctor_command_treats_a_skipped_feed_authority_probe_as_non_failing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, feed_status: str, exit_code: int
):
    checks = [{"check": "queue", "status": "ok"}, {"check": "feed-authority", "status": feed_status}]
    monkeypatch.setattr(grokbot_ops, "doctor", lambda *_args, **_kwargs: checks)

    argv = ["run", "cloud", "grokbot", "doctor", "--target", str(tmp_path), "--instance", "operator"]

    assert cli.main(argv) == exit_code
    assert f"feed-authority: {feed_status}" in capsys.readouterr().out


def test_doctor_fails_cleanly_on_missing_config(tmp_path: Path, capsys):
    assert cli.main(["run", "cloud", "grokbot", "doctor", "--target", str(tmp_path), "--instance", "operator"]) == 1
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert SECRET not in combined
    assert "config" in combined


def test_canary_verifies_auth_rejection_and_exact_inventory(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_GROKBOT_BEARER", SECRET)
    assert _setup(tmp_path) == 0

    config = grokbot_mcp.build_listener_config(
        target=tmp_path,
        instance="operator",
        bind="127.0.0.1:8766",
        allowed_hosts=[],
        allowed_origins=[],
        bearer_file=None,
        bearer_env="TEST_GROKBOT_BEARER",
    )
    adapter = grokbot_mcp.GrokbotAdapter(config)

    class _Response:
        def __init__(self, status: int, body: bytes):
            self.status = status
            self._body = body

        def read(self, amount: int = -1) -> bytes:
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_open(request, timeout):
        url = request.full_url
        authorized = adapter.authorized(request.get_header("Authorization"))
        if url.endswith("/health"):
            if not authorized:
                return _Response(401, b"{}")
            return _Response(200, json.dumps(adapter.health_payload()).encode("utf-8"))
        if url.endswith("/mcp"):
            if not authorized:
                return _Response(401, b"{}")
            request_json = json.loads(request.data.decode("utf-8"))
            return _Response(
                200,
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": request_json.get("id"),
                        "result": {"tools": [{"name": name} for name in sorted(adapter._tools())]},
                    }
                ).encode("utf-8"),
            )
        raise AssertionError(url)

    monkeypatch.setattr(grokbot_ops, "_open_http", fake_open)
    monkeypatch.setattr(grokbot_ops, "_tools_list", lambda *_args: adapter.tool_inventory())
    result = grokbot_ops.canary(tmp_path, "operator", timeout=5)

    assert result["ok"] is True
    assert set(result["tools"]) == set(grokbot_mcp.OPERATOR_TOOLS)
    assert result["health"] == {"ok": True, "service": "grokbot-mcp", "role": "operator"}
    assert result["auth_rejected_without_bearer"] is True


def test_canary_accepts_anonymous_http_auth_challenge(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_GROKBOT_BEARER", SECRET)
    assert _setup(tmp_path) == 0

    monkeypatch.setattr(
        grokbot_ops,
        "_request_json",
        lambda *_args, **_kwargs: {"ok": True, "service": "grokbot-mcp", "role": "operator"},
    )
    monkeypatch.setattr(grokbot_ops, "_anonymous_health_status", lambda *_args: 401, raising=False)
    monkeypatch.setattr(
        grokbot_ops,
        "_tools_list",
        lambda *_args: [{"name": name} for name in grokbot_mcp.OPERATOR_TOOLS],
    )

    assert grokbot_ops.canary(tmp_path, "operator")["ok"] is True


def test_anonymous_health_status_preserves_http_auth_rejection(monkeypatch):
    def rejected(*_args, **_kwargs):
        raise urllib.error.HTTPError("http://example.test/health", 403, "forbidden", {}, None)

    monkeypatch.setattr(grokbot_ops, "_open_http", rejected)
    assert grokbot_ops._anonymous_health_status("http://example.test/health", 1) == 403


def test_authenticated_health_requests_ignore_proxy_environment_and_refuse_redirects(monkeypatch):
    opened: list[tuple[str, str | None, int]] = []

    class _Opener:
        def open(self, request, timeout):
            opened.append((request.full_url, request.get_header("Authorization"), timeout))
            raise urllib.error.HTTPError(request.full_url, 302, "found", {}, None)

    def build_opener(*handlers):
        proxy_handler = next(handler for handler in handlers if isinstance(handler, urllib.request.ProxyHandler))
        redirect_handler = next(handler for handler in handlers if isinstance(handler, grokbot_ops._NoRedirect))
        assert proxy_handler.proxies == {}
        assert redirect_handler.redirect_request(None, None, 302, "found", {}, "http://outside.test") is None
        return _Opener()

    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example.test:8080")
    monkeypatch.setattr(urllib.request, "build_opener", build_opener)
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_args, **_kwargs: pytest.fail("urlopen must not be used"))

    assert grokbot_ops._request_json("http://listener.test/health", SECRET, 7, method="GET") is None
    assert opened == [("http://listener.test/health", f"Bearer {SECRET}", 7)]


def test_authenticated_health_redirect_never_reaches_redirect_target():
    target_requests: list[str | None] = []

    class _TargetHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            target_requests.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()

        def log_message(self, _format: str, *_args: object) -> None:
            return None

    target = ThreadingHTTPServer(("127.0.0.1", 0), _TargetHandler)
    target_thread = threading.Thread(target=target.serve_forever, daemon=True)
    target_thread.start()

    class _RedirectHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{target.server_port}/redirect-target")
            self.end_headers()

        def log_message(self, _format: str, *_args: object) -> None:
            return None

    redirect = ThreadingHTTPServer(("127.0.0.1", 0), _RedirectHandler)
    redirect_thread = threading.Thread(target=redirect.serve_forever, daemon=True)
    redirect_thread.start()
    try:
        assert (
            grokbot_ops._request_json(f"http://127.0.0.1:{redirect.server_port}/health", SECRET, 1, method="GET")
            is None
        )
        assert target_requests == []
    finally:
        redirect.shutdown()
        redirect.server_close()
        redirect_thread.join()
        target.shutdown()
        target.server_close()
        target_thread.join()


def test_tools_list_uses_an_initialized_official_mcp_session(monkeypatch):
    events: list[str] = []

    class _HttpClient:
        def __init__(self, **kwargs):
            assert kwargs["headers"] == {"Authorization": f"Bearer {SECRET}"}
            assert kwargs["trust_env"] is False
            events.append("http-client")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

    class _Transport:
        async def __aenter__(self):
            events.append("transport")
            return object(), object()

        async def __aexit__(self, *_exc):
            return False

    class _Session:
        def __init__(self, *_streams):
            self.initialized = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def initialize(self):
            self.initialized = True
            events.append("initialize")

        async def list_tools(self):
            assert self.initialized
            events.append("list-tools")
            return SimpleNamespace(tools=[SimpleNamespace(model_dump=lambda: {"name": "grokbot_queue_list"})])

    monkeypatch.setattr(
        grokbot_ops,
        "_mcp_client_components",
        lambda: (_Session, lambda *_args, **_kwargs: _Transport(), _HttpClient),
        raising=False,
    )

    assert grokbot_ops._tools_list("http://example.test", SECRET, 1) == [{"name": "grokbot_queue_list"}]
    assert events == ["http-client", "transport", "initialize", "list-tools"]


def test_mcp_client_components_use_the_sdk_http_transport(monkeypatch):
    class _ClientSession:
        pass

    class _AsyncClient:
        pass

    mcp = ModuleType("mcp")
    mcp.__path__ = []  # type: ignore[attr-defined]
    mcp.ClientSession = _ClientSession  # type: ignore[attr-defined]
    client = ModuleType("mcp.client")
    client.__path__ = []  # type: ignore[attr-defined]
    streamable = ModuleType("mcp.client.streamable_http")
    streamable.streamable_http_client = object()  # type: ignore[attr-defined]
    httpx2 = ModuleType("httpx2")
    httpx2.AsyncClient = _AsyncClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mcp", mcp)
    monkeypatch.setitem(sys.modules, "mcp.client", client)
    monkeypatch.setitem(sys.modules, "mcp.client.streamable_http", streamable)
    monkeypatch.setitem(sys.modules, "httpx2", httpx2)

    _, _, async_client = grokbot_ops._mcp_client_components()
    assert async_client is _AsyncClient


def test_canary_fails_on_wrong_inventory(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_GROKBOT_BEARER", SECRET)
    assert _setup(tmp_path) == 0

    def fake_open(request, timeout):
        class _Response:
            status = 200

            def read(self, amount: int = -1) -> bytes:
                if not request.get_header("Authorization"):
                    return b"{}"
                if request.full_url.endswith("/health"):
                    return json.dumps({"ok": True, "service": "grokbot-mcp", "role": "operator"}).encode("utf-8")
                return json.dumps({"result": {"tools": [{"name": "not-the-right-tool"}]}}).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        return _Response()

    monkeypatch.setattr(grokbot_ops, "_open_http", fake_open)
    monkeypatch.setattr(grokbot_ops, "_anonymous_health_status", lambda *_args: 401)
    monkeypatch.setattr(grokbot_ops, "_tools_list", lambda *_args: [{"name": "not-the-right-tool"}])
    result = grokbot_ops.canary(tmp_path, "operator", timeout=5)
    assert result["ok"] is False
    assert "inventory" in result["reason"]


def test_canary_is_non_mutating_and_reports_missing_listener(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("TEST_GROKBOT_BEARER", SECRET)
    assert _setup(tmp_path) == 0
    before = json.dumps(grokbot_jobs.status(tmp_path), sort_keys=True)

    assert cli.main(["run", "cloud", "grokbot", "canary", "--target", str(tmp_path), "--instance", "operator"]) == 1
    after = json.dumps(grokbot_jobs.status(tmp_path), sort_keys=True)
    assert before == after
    combined = capsys.readouterr().out + capsys.readouterr().err
    assert SECRET not in combined


def test_render_unit_is_role_scoped_with_protected_token_reference_only(tmp_path: Path):
    token_file = tmp_path / "token.txt"
    unit = grokbot_ops.render_unit(
        {
            "schema": grokbot_ops.CONFIG_SCHEMA,
            "instance": "repository-scout",
            "bind": "127.0.0.1:8766",
            "allowed_hosts": ["mcp.example.test"],
            "allowed_origins": ["https://console.example.test"],
            "bearer": {"kind": "file", "path": str(token_file)},
        },
        python=str(Path("/usr/bin/python3")),
        exec_root=tmp_path,
    )
    rendered = unit
    assert "brigade-grokbot-repository-scout.service" in rendered or "repository-scout" in rendered
    assert "EnvironmentFile=" not in rendered
    assert "--bearer-file" in rendered and str(token_file) in rendered
    assert SECRET not in rendered
    assert "sudo" not in rendered.lower()
    assert "NoNewPrivileges=yes" in rendered
    assert f"ReadWritePaths={tmp_path / '.brigade' / 'cloud' / 'grokbot'}" in rendered

    env_unit = grokbot_ops.render_unit(
        {
            "schema": grokbot_ops.CONFIG_SCHEMA,
            "instance": "operator",
            "bind": "127.0.0.1:8766",
            "allowed_hosts": [],
            "allowed_origins": [],
            "bearer": {"kind": "env", "name": "TEST_GROKBOT_BEARER"},
        },
        python=str(Path("/usr/bin/python3")),
        exec_root=tmp_path,
    )
    assert "EnvironmentFile" not in env_unit
    assert "--bearer-env TEST_GROKBOT_BEARER" in env_unit
    assert "BRIGADE_FLEET_NODE_TOKEN" not in unit
    assert "BRIGADE_FLEET_TOKEN" not in unit
    assert "node_token" not in unit
    assert "BRIGADE_FLEET_NODE_TOKEN" not in env_unit


def test_render_unit_quotes_space_and_quote_arguments_and_rejects_controls(tmp_path: Path):
    token_file = tmp_path / "token with space.txt"
    config = {
        "schema": grokbot_ops.CONFIG_SCHEMA,
        "instance": "operator",
        "bind": "127.0.0.1:8766",
        "allowed_hosts": ["mcp.example.test"],
        "allowed_origins": ["https://console.example.test"],
        "bearer": {"kind": "file", "path": str(token_file)},
    }

    rendered = grokbot_ops.render_unit(config, python="/opt/python with space", exec_root=tmp_path)
    assert '"/opt/python with space"' in rendered
    assert f'"{token_file}"' in rendered

    config["bearer"] = {"kind": "file", "path": f"{token_file}\nExecStart=/bin/false"}
    with pytest.raises(grokbot_mcp.ConfigurationError):
        grokbot_ops.render_unit(config, python="/usr/bin/python3", exec_root=tmp_path)

    for expanded in ("/tmp/token%N", "/tmp/$TOKEN"):
        config["bearer"] = {"kind": "file", "path": expanded}
        with pytest.raises(grokbot_mcp.ConfigurationError):
            grokbot_ops.render_unit(config, python="/usr/bin/python3", exec_root=tmp_path)


def test_write_unit_refuses_unrelated_overwrites_and_honors_force(tmp_path: Path):
    out_dir = tmp_path / "units"
    out_dir.mkdir()
    unrelated = out_dir / "unrelated.service"
    unrelated.write_text("keep me\n", encoding="utf-8")
    config = {
        "schema": grokbot_ops.CONFIG_SCHEMA,
        "instance": "operator",
        "bind": "127.0.0.1:8766",
        "allowed_hosts": [],
        "allowed_origins": [],
        "bearer": {"kind": "env", "name": "TEST_GROKBOT_BEARER"},
    }

    path = grokbot_ops.write_unit(config, out_dir, exec_root=tmp_path, force=False)
    assert grokbot_ops.unit_name("operator") == path.name

    path.write_text("changed\n", encoding="utf-8")
    with pytest.raises(grokbot_ops.ServiceRenderError):
        grokbot_ops.write_unit(config, out_dir, exec_root=tmp_path, force=False)
    assert (
        grokbot_ops.write_unit(config, out_dir, exec_root=tmp_path, force=True).read_text(encoding="utf-8")
        != "changed\n"
    )
    assert unrelated.read_text(encoding="utf-8") == "keep me\n"


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX dirfd publication")
def test_write_unit_no_force_refuses_competing_file_created_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    config = {
        "schema": grokbot_ops.CONFIG_SCHEMA,
        "instance": "operator",
        "bind": "127.0.0.1:8766",
        "allowed_hosts": [],
        "allowed_origins": [],
        "bearer": {"kind": "env", "name": "TEST_GROKBOT_BEARER"},
    }
    out_dir = tmp_path / "units"
    path = out_dir / grokbot_ops.unit_name("operator")
    real_link = os.link

    def competing_link(source: str, destination: str, **kwargs: object) -> None:
        path.write_text("competing unit\n", encoding="utf-8")
        real_link(source, destination, **kwargs)

    monkeypatch.setattr(grokbot_ops.os, "link", competing_link)

    with pytest.raises(grokbot_ops.ServiceRenderError):
        grokbot_ops.write_unit(config, out_dir, exec_root=tmp_path, force=False)
    assert path.read_text(encoding="utf-8") == "competing unit\n"


def test_config_operations_refuse_symlinked_config_file_and_directory(tmp_path: Path):
    config = {
        "schema": grokbot_ops.CONFIG_SCHEMA,
        "instance": "operator",
        "bind": "127.0.0.1:8766",
        "allowed_hosts": [],
        "allowed_origins": [],
        "bearer": {"kind": "env", "name": "TEST_GROKBOT_BEARER"},
    }
    config_dir = tmp_path / grokbot_ops.CONFIG_DIR
    config_dir.mkdir(parents=True)
    outside = tmp_path / "outside.json"
    outside.write_text("keep me\n", encoding="utf-8")
    path = grokbot_ops.config_path(tmp_path, "operator")
    path.symlink_to(outside)

    with pytest.raises(grokbot_mcp.ConfigurationError):
        grokbot_ops.save_config(
            tmp_path,
            instance="operator",
            bind="127.0.0.1:8766",
            allowed_hosts=[],
            allowed_origins=[],
            bearer_env="TEST_GROKBOT_BEARER",
            bearer_file=None,
        )
    with pytest.raises(grokbot_mcp.ConfigurationError):
        grokbot_ops.load_config(tmp_path, "operator")
    assert outside.read_text(encoding="utf-8") == "keep me\n"

    path.unlink()
    config_dir.rmdir()
    outside_dir = tmp_path / "outside-dir"
    outside_dir.mkdir()
    config_dir.symlink_to(outside_dir, target_is_directory=True)

    with pytest.raises(grokbot_mcp.ConfigurationError):
        grokbot_ops.save_config(
            tmp_path,
            instance="operator",
            bind="127.0.0.1:8766",
            allowed_hosts=[],
            allowed_origins=[],
            bearer_env="TEST_GROKBOT_BEARER",
            bearer_file=None,
        )
    with pytest.raises(grokbot_mcp.ConfigurationError):
        grokbot_ops.load_config(tmp_path, "operator")
    assert list(outside_dir.iterdir()) == []
    assert config["instance"] == "operator"


@pytest.mark.skipif(
    os.name != "posix" or not getattr(os, "O_PATH", 0), reason="requires POSIX O_PATH directory descriptors"
)
def test_posix_path_walker_uses_opath_root_and_keeps_read_write_nofollow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    directory = tmp_path / "nested" / "config"
    path = directory / "config.json"
    real_open = os.open
    root_flags: list[int] = []

    def protected_root_open(
        name: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if name == "/" and dir_fd is None:
            root_flags.append(flags)
            if not flags & os.O_PATH:
                raise PermissionError("root is unreadable")
        return real_open(name, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(grokbot_ops.os, "open", protected_root_open)

    grokbot_ops._write_text_nofollow_atomic(path, "safe\n", mode=0o600)
    assert grokbot_ops._read_regular_text(path) == "safe\n"
    assert root_flags and root_flags[0] & os.O_PATH

    outside = tmp_path / "outside.txt"
    outside.write_text("keep me\n", encoding="utf-8")
    path.unlink()
    path.symlink_to(outside)

    with pytest.raises(OSError):
        grokbot_ops._read_regular_text(path)
    with pytest.raises(OSError):
        grokbot_ops._write_text_nofollow_atomic(path, "changed\n", mode=0o600)
    assert outside.read_text(encoding="utf-8") == "keep me\n"


def test_write_unit_refuses_symlinked_paths_and_force_replaces_only_the_link(tmp_path: Path):
    config = {
        "schema": grokbot_ops.CONFIG_SCHEMA,
        "instance": "operator",
        "bind": "127.0.0.1:8766",
        "allowed_hosts": [],
        "allowed_origins": [],
        "bearer": {"kind": "env", "name": "TEST_GROKBOT_BEARER"},
    }
    out_dir = tmp_path / "units"
    out_dir.mkdir()
    target = tmp_path / "unrelated.service"
    target.write_text("keep me\n", encoding="utf-8")
    unit = out_dir / grokbot_ops.unit_name("operator")
    unit.symlink_to(target)

    with pytest.raises(grokbot_ops.ServiceRenderError):
        grokbot_ops.write_unit(config, out_dir, exec_root=tmp_path)
    assert unit.is_symlink()
    assert target.read_text(encoding="utf-8") == "keep me\n"

    written = grokbot_ops.write_unit(config, out_dir, exec_root=tmp_path, force=True)
    assert written == unit and not unit.is_symlink()
    assert target.read_text(encoding="utf-8") == "keep me\n"
    assert unit.read_text(encoding="utf-8").startswith("# Generated by brigade")

    unit.unlink()
    out_dir.rmdir()
    outside_dir = tmp_path / "outside-units"
    outside_dir.mkdir()
    out_dir.symlink_to(outside_dir, target_is_directory=True)
    with pytest.raises(grokbot_ops.ServiceRenderError):
        grokbot_ops.write_unit(config, out_dir, exec_root=tmp_path, force=True)
    assert list(outside_dir.iterdir()) == []


def test_write_unit_keeps_identical_content_and_mode_without_rewriting(tmp_path: Path):
    config = {
        "schema": grokbot_ops.CONFIG_SCHEMA,
        "instance": "operator",
        "bind": "127.0.0.1:8766",
        "allowed_hosts": [],
        "allowed_origins": [],
        "bearer": {"kind": "env", "name": "TEST_GROKBOT_BEARER"},
    }
    path = grokbot_ops.write_unit(config, tmp_path / "units", exec_root=tmp_path)
    before = path.stat()

    assert grokbot_ops.write_unit(config, tmp_path / "units", exec_root=tmp_path) == path
    after = path.stat()
    assert after.st_mode & 0o777 == 0o644
    assert after.st_mtime_ns == before.st_mtime_ns


def test_install_service_cli_prints_dry_run_by_default(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("TEST_GROKBOT_BEARER", SECRET)
    assert _setup(tmp_path) == 0
    monkeypatch.delenv("TEST_GROKBOT_BEARER")

    assert (
        cli.main(["run", "cloud", "grokbot", "install-service", "--target", str(tmp_path), "--instance", "operator"])
        == 0
    )
    out = capsys.readouterr().out
    assert "[Unit]" in out and "brigade-grokbot-operator.service" in out
    assert SECRET not in out

    out_dir = tmp_path / "rendered"
    assert (
        cli.main(
            [
                "run",
                "cloud",
                "grokbot",
                "install-service",
                "--target",
                str(tmp_path),
                "--instance",
                "operator",
                "--out",
                str(out_dir),
            ]
        )
        == 0
    )
    assert (out_dir / grokbot_ops.unit_name("operator")).is_file()


def test_new_grokbot_commands_appear_in_parser_derived_inventory():
    from brigade.roadmap_cmd import _cli_command_paths

    paths = _cli_command_paths()
    for command in ("setup", "doctor", "canary", "install-service", "reconcile-reports"):
        assert f"brigade run-cloud grokbot {command}" in paths


@pytest.mark.parametrize("instance", ("operator", "repository-scout", "implementation-worker"))
def test_queue_role_units_use_shared_listener_recovery_policy(tmp_path: Path, instance: str):
    unit = grokbot_ops.render_unit(
        {
            "schema": grokbot_ops.CONFIG_SCHEMA,
            "instance": instance,
            "bind": "127.0.0.1:8766",
            "allowed_hosts": [],
            "allowed_origins": [],
            "bearer": {"kind": "env", "name": "TEST_GROKBOT_BEARER"},
        },
        python="/usr/bin/python3",
        exec_root=tmp_path,
    )
    assert_listener_recovery_policy(unit)
    assert grokbot_ops.LISTENER_RECOVERY_UNIT_FRAGMENT in unit
    assert grokbot_ops.LISTENER_RECOVERY_SERVICE_FRAGMENT in unit


def _record_systemctl(monkeypatch, *, stdout="success\n", returncode=0, error: BaseException | None = None):
    recorded: list[dict[str, object]] = []

    def fake_run(argv, *args, **kwargs):
        recorded.append({"argv": list(argv), "kwargs": dict(kwargs)})
        if error is not None:
            raise error
        return subprocess.CompletedProcess(list(argv), returncode, stdout=stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return recorded


def test_inspect_service_result_uses_fixed_argv_and_succeeds_on_exact_success(monkeypatch):
    recorded = _record_systemctl(monkeypatch, stdout="  success \n")
    check = grokbot_ops.inspect_service_result("operator")
    assert check == {"check": "service-result", "status": "ok"}
    assert recorded[0]["argv"] == [
        "systemctl",
        "--user",
        "show",
        "brigade-grokbot-operator.service",
        "--property=Result",
        "--value",
    ]
    kwargs = recorded[0]["kwargs"]
    assert kwargs.get("shell") is not True
    assert kwargs.get("check") is not True
    assert kwargs.get("timeout") == grokbot_ops.DEFAULT_TIMEOUT_SECONDS
    assert not any(verb in recorded[0]["argv"] for verb in _SYSTEMD_MUTATION_VERBS)
    assert not any(str(part).endswith(".timer") for part in recorded[0]["argv"])


@pytest.mark.parametrize(
    "stdout,returncode,error",
    (
        ("failed\n", 0, None),
        ("Result=success\n", 0, None),
        ("SUCCESS\n", 0, None),
        ("success extra\n", 0, None),
        ("", 0, None),
        ("success\n", 1, None),
        ("success\n", 0, FileNotFoundError("systemctl")),
        ("success\n", 0, OSError("unavailable")),
        (
            "success\n",
            0,
            subprocess.TimeoutExpired(
                ["systemctl", "--user", "show", "brigade-grokbot-operator.service"],
                5,
            ),
        ),
    ),
)
def test_inspect_service_result_fails_closed_without_raw_output(
    monkeypatch, stdout: str, returncode: int, error: BaseException | None
):
    recorded = _record_systemctl(monkeypatch, stdout=stdout, returncode=returncode, error=error)
    check = grokbot_ops.inspect_service_result("operator")
    assert check == {"check": "service-result", "status": "fail"}
    assert set(check) == {"check", "status"}
    if error is None:
        assert recorded[0]["argv"][3] == "brigade-grokbot-operator.service"


def test_inspect_service_result_sanitizes_raw_stdout_and_stderr(monkeypatch):
    leak = "timeout /home/secret/.config/systemd/user/brigade-grokbot-operator.service token=not-a-real-token"

    def fake_run(argv, *args, **kwargs):
        return subprocess.CompletedProcess(list(argv), 0, stdout=leak, stderr=leak)

    monkeypatch.setattr(subprocess, "run", fake_run)
    check = grokbot_ops.inspect_service_result("operator")
    rendered = json.dumps(check)
    assert check == {"check": "service-result", "status": "fail"}
    assert leak not in rendered
    assert "not-a-real-token" not in rendered
    assert "/home/secret" not in rendered
