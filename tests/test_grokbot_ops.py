"""Tests for Grok Bot setup, doctor, canary, and systemd service helpers."""

from __future__ import annotations

import json
import sys
import urllib.error
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from brigade import cli, grokbot_jobs, grokbot_mcp, grokbot_ops

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
    for command in ("setup", "doctor", "canary", "install-service"):
        assert f"brigade run-cloud grokbot {command}" in paths
