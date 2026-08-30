"""Obsidian Operator lifecycle, canary, and unit rendering."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from brigade import grokbot_ops, grokbot_packs
from tests.test_grokbot_ops import assert_listener_recovery_policy
from brigade.grokbot_obsidian.adapters import NATIVE_MCP_TOOLS
from brigade.grokbot_obsidian.contracts import TOOLS, ObsidianError
from brigade.grokbot_obsidian.lifecycle import (
    ObsidianListenerConfig,
    _invalid_obsidian_tool_request,
    build_tools_from_config,
    canary,
    close_native_client,
    render_unit,
    validate_disjoint_state_paths,
)
from brigade.grokbot_obsidian.native_client import StreamableNativeMcpClient

SECRET = "not-a-real-token-value-32chars!!"
UPSTREAM_KEY = "u" * 16
UPSTREAM_URL = "https://127.0.0.1:27124/"


def _obsidian_paths(tmp_path: Path) -> dict[str, Path]:
    runtime = tmp_path / "runtime.json"
    actions = tmp_path / "actions"
    approvals = tmp_path / "approvals"
    staging = tmp_path / "staging"
    helper = tmp_path / "excalidraw"
    runtime.write_text("{}\n", encoding="utf-8")
    os.chmod(runtime, 0o600)
    actions.mkdir(mode=0o700)
    approvals.mkdir(mode=0o700)
    staging.mkdir(mode=0o700)
    helper.write_text("#!/bin/sh\n", encoding="utf-8")
    os.chmod(actions, 0o700)
    os.chmod(approvals, 0o700)
    os.chmod(staging, 0o700)
    os.chmod(helper, 0o755)
    return {
        "runtime_path": runtime,
        "action_state_path": actions,
        "approval_dir": approvals,
        "staging_dir": staging,
        "excalidraw_bin": helper,
    }


def _obsidian_setup_kwargs(tmp_path: Path, monkeypatch) -> dict[str, object]:
    monkeypatch.setenv("TEST_GROKBOT_BEARER", SECRET)
    monkeypatch.setenv("TEST_GROKBOT_UPSTREAM", UPSTREAM_KEY)
    return {
        **_obsidian_paths(tmp_path),
        "upstream_url": UPSTREAM_URL,
        "upstream_key_env": "TEST_GROKBOT_UPSTREAM",
    }


def _write_runtime(path: Path, *, sensitive_tags: list[str] | None = None) -> Path:
    payload = {
        "version": 1,
        "upstream_tls": {"ca_path": str(path.parent / "ca.pem"), "spki_sha256": ["sha256:" + ("A" * 43) + "="]},
        "templates": {"catalog": []},
        "home_note": "Home.md",
        "flashcard_note": "03 - Resources/Cards.md",
        "flashcard_heading": "Inbox",
        "dashboard_root": "01 - Projects/Dashboard.base",
        "daily_notes_folder": "",
        "sensitive_tags": list(sensitive_tags or []),
        "sensitive_path_prefixes": [],
        "command_fingerprint": "sha256:" + ("0" * 64),
        "plugin_inventory": [],
        "excalidraw": {
            "enabled": False,
            "verified_suffix": ".excalidraw.md",
            "probe_receipt_sha256": "sha256:" + ("0" * 64),
        },
    }
    (path.parent / "ca.pem").write_text(
        "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n", encoding="utf-8"
    )
    os.chmod(path.parent / "ca.pem", 0o600)
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def test_disjoint_paths_reject_overlap_and_relative():
    with pytest.raises(ObsidianError) as caught:
        validate_disjoint_state_paths("/var/lib/a", "/var/lib/a/actions", "/var/lib/b", "/var/lib/c", "/usr/bin/true")
    assert caught.value.code == "invalid_request"
    assert "/var/lib/a" not in str(caught.value)
    with pytest.raises(ObsidianError):
        validate_disjoint_state_paths("var/lib/a", "/var/lib/b", "/var/lib/c", "/var/lib/d", "/usr/bin/true")


def test_pack_setup_doctor_canary_and_unit_hide_secrets(tmp_path: Path, monkeypatch):
    kwargs = _obsidian_setup_kwargs(tmp_path, monkeypatch)
    preview = grokbot_packs.preview_setup(
        tmp_path,
        "obsidian-operator",
        bearer_env="TEST_GROKBOT_BEARER",
        **kwargs,
    )
    assert preview["apply"] is False
    assert not grokbot_packs.instance_config_path(tmp_path, "obsidian-operator").exists()
    dumped = json.dumps(preview)
    assert str(kwargs["runtime_path"]) not in dumped
    assert UPSTREAM_KEY not in dumped
    grokbot_packs.apply_setup(
        tmp_path,
        "obsidian-operator",
        bearer_env="TEST_GROKBOT_BEARER",
        **kwargs,
    )
    config = json.loads(grokbot_packs.instance_config_path(tmp_path, "obsidian-operator").read_text(encoding="utf-8"))
    assert config["upstream_url"] == UPSTREAM_URL
    assert config["upstream_key"] == {"kind": "env", "name": "TEST_GROKBOT_UPSTREAM"}
    assert UPSTREAM_KEY not in json.dumps(config)
    checks = grokbot_packs.doctor(tmp_path, "obsidian-operator")
    canary = grokbot_packs.canary(tmp_path, "obsidian-operator")
    unit = grokbot_packs.render_install_service(tmp_path, "obsidian-operator")
    sanitized = json.dumps({"checks": checks, "canary": canary})
    assert SECRET not in sanitized
    assert UPSTREAM_KEY not in sanitized
    assert str(kwargs["runtime_path"]) not in sanitized
    assert str(kwargs["excalidraw_bin"]) not in sanitized
    assert SECRET not in unit
    assert UPSTREAM_KEY not in unit
    assert str(kwargs["runtime_path"]) not in unit
    assert str(kwargs["approval_dir"]) not in unit
    assert str(kwargs["excalidraw_bin"]) not in unit
    assert "--pack" in unit
    assert "obsidian-operator" in unit
    assert "127.0.0.1:8773" in unit
    assert "--upstream-url" in unit
    assert "--upstream-key-env" in unit
    assert "TEST_GROKBOT_UPSTREAM" in unit
    assert "NoNewPrivileges=yes" in unit
    assert "ProtectSystem=strict" in unit
    assert canary["ok"] is False
    assert all(set(check) == {"check", "status"} for check in checks)
    unit_dir = tmp_path / "units"
    installed = grokbot_packs.apply_install_service(tmp_path, "obsidian-operator", out_dir=unit_dir)
    assert installed["unit"] == grokbot_ops.unit_name("obsidian-operator")
    grokbot_packs.apply_remove(tmp_path, "obsidian-operator", unit_dir=unit_dir)
    assert not grokbot_packs.instance_config_path(tmp_path, "obsidian-operator").exists()


def test_pack_setup_rejects_raw_upstream_key_and_non_loopback_url(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_GROKBOT_BEARER", SECRET)
    paths = _obsidian_paths(tmp_path)
    with pytest.raises(grokbot_packs.PackError):
        grokbot_packs.apply_setup(
            tmp_path,
            "obsidian-operator",
            bearer_env="TEST_GROKBOT_BEARER",
            upstream_url="https://example.test/",
            upstream_key_env="TEST_GROKBOT_UPSTREAM",
            **paths,
        )
    with pytest.raises(grokbot_packs.PackError):
        grokbot_packs.apply_setup(
            tmp_path,
            "obsidian-operator",
            bearer_env="TEST_GROKBOT_BEARER",
            upstream_url=UPSTREAM_URL,
            upstream_key={"kind": "env", "name": "TEST_GROKBOT_UPSTREAM", "value": UPSTREAM_KEY},
            **paths,
        )


def test_unit_readwrite_paths_exclude_approval_and_secrets(tmp_path: Path, monkeypatch):
    kwargs = _obsidian_setup_kwargs(tmp_path, monkeypatch)
    grokbot_packs.apply_setup(
        tmp_path,
        "obsidian-operator",
        bearer_env="TEST_GROKBOT_BEARER",
        **kwargs,
    )
    unit = render_unit(tmp_path)
    read_write = unit.split("ReadWritePaths=", 1)[-1]
    assert str(kwargs["action_state_path"]) in read_write
    assert str(kwargs["staging_dir"]) in read_write
    assert str(kwargs["approval_dir"]) not in read_write
    assert str(kwargs["runtime_path"]) not in unit
    assert SECRET not in unit
    assert UPSTREAM_KEY not in unit


def test_configured_service_reaches_live_client_factory_and_allowlist(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_GROKBOT_UPSTREAM", UPSTREAM_KEY)
    paths = _obsidian_paths(tmp_path)
    _write_runtime(paths["runtime_path"])
    created: list[object] = []

    class RecordingClient:
        def __init__(self) -> None:
            self.closed = False
            self.names: list[str] = []

        def call_tool(self, name: str, arguments=None):
            self.names.append(name)
            if name == "command_list":
                return {"commands": []}
            raise AssertionError(name)

        def close(self) -> None:
            self.closed = True

    def factory(**kwargs):
        assert kwargs["url"] == UPSTREAM_URL
        assert kwargs["api_key"] == UPSTREAM_KEY
        assert kwargs["api_key"] != SECRET
        client = RecordingClient()
        created.append(client)
        return client

    config = ObsidianListenerConfig(
        target=tmp_path,
        bind_host="127.0.0.1",
        bind_port=8773,
        allowed_hosts=(),
        allowed_origins=(),
        bearer=SECRET,
        runtime_path=str(paths["runtime_path"]),
        action_state_path=str(paths["action_state_path"]),
        approval_dir=str(paths["approval_dir"]),
        staging_dir=str(paths["staging_dir"]),
        excalidraw_bin=str(paths["excalidraw_bin"]),
        upstream_url=UPSTREAM_URL,
        upstream_key={"kind": "env", "name": "TEST_GROKBOT_UPSTREAM"},
    )
    tools = build_tools_from_config(config, native_client_factory=factory)
    assert created
    assert tools.native.policy["sensitiveTags"] == ()
    assert isinstance(tools.native.client, RecordingClient)
    assert not isinstance(tools.native.client, StreamableNativeMcpClient)
    tools.native.command_list()
    assert created[0].names == ["command_list"]
    with pytest.raises(ObsidianError) as caught:
        tools.native._call("command_execute", {})
    assert caught.value.code == "protocol_error"
    assert set(NATIVE_MCP_TOOLS) == {
        "search_simple",
        "vault_read",
        "vault_get_document_map",
        "vault_write",
        "vault_patch",
        "vault_copy",
        "vault_move",
        "vault_delete",
        "command_list",
    }
    close_native_client(tools)
    assert created[0].closed is True


def test_build_tools_passes_runtime_sensitive_tags_into_native_port(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_GROKBOT_UPSTREAM", UPSTREAM_KEY)
    paths = _obsidian_paths(tmp_path)
    _write_runtime(paths["runtime_path"], sensitive_tags=["private"])

    class RecordingClient:
        def call_tool(self, name: str, arguments=None):
            raise AssertionError(name)

        def close(self) -> None:
            return None

    config = ObsidianListenerConfig(
        target=tmp_path,
        bind_host="127.0.0.1",
        bind_port=8773,
        allowed_hosts=(),
        allowed_origins=(),
        bearer=SECRET,
        runtime_path=str(paths["runtime_path"]),
        action_state_path=str(paths["action_state_path"]),
        approval_dir=str(paths["approval_dir"]),
        staging_dir=str(paths["staging_dir"]),
        excalidraw_bin=str(paths["excalidraw_bin"]),
        upstream_url=UPSTREAM_URL,
        upstream_key={"kind": "env", "name": "TEST_GROKBOT_UPSTREAM"},
    )
    tools = build_tools_from_config(config, native_client_factory=lambda **_kwargs: RecordingClient())
    assert tools.native.policy["sensitiveTags"] == ("private",)
    close_native_client(tools)


def test_canary_requires_live_capabilities_and_fails_when_native_unavailable(tmp_path: Path, monkeypatch):
    kwargs = _obsidian_setup_kwargs(tmp_path, monkeypatch)
    grokbot_packs.apply_setup(
        tmp_path,
        "obsidian-operator",
        bearer_env="TEST_GROKBOT_BEARER",
        **kwargs,
    )
    monkeypatch.setattr(
        grokbot_ops,
        "_request_json",
        lambda *_args, **_kwargs: {"ok": True, "service": "grokbot-obsidian-operator"},
    )
    monkeypatch.setattr(grokbot_ops, "_anonymous_health_status", lambda *_args, **_kwargs: 401)
    monkeypatch.setattr(grokbot_ops, "_tools_list", lambda *_args, **_kwargs: [{"name": name} for name in TOOLS])

    def boom(*_args, **_kwargs):
        raise ObsidianError("unavailable", "Obsidian observation is unavailable")

    monkeypatch.setattr(
        "brigade.grokbot_obsidian.lifecycle._call_capabilities_tool",
        boom,
    )
    result = canary(tmp_path)
    assert result["ok"] is False
    assert result.get("reason") == "capabilities"


def test_configured_service_reaches_excalidraw_stdio_factory(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_GROKBOT_UPSTREAM", UPSTREAM_KEY)
    paths = _obsidian_paths(tmp_path)
    _write_runtime(paths["runtime_path"])
    created: list[dict[str, object]] = []

    class RecordingLifecycle:
        def call_tool(self, name: str, arguments=None):
            raise AssertionError(name)

        def close(self) -> None:
            return None

    def factory(*, executable, staging_dir, env):
        created.append({"executable": executable, "staging_dir": staging_dir, "env": dict(env), "args": ()})
        return RecordingLifecycle()

    config = ObsidianListenerConfig(
        target=tmp_path,
        bind_host="127.0.0.1",
        bind_port=8773,
        allowed_hosts=(),
        allowed_origins=(),
        bearer=SECRET,
        runtime_path=str(paths["runtime_path"]),
        action_state_path=str(paths["action_state_path"]),
        approval_dir=str(paths["approval_dir"]),
        staging_dir=str(paths["staging_dir"]),
        excalidraw_bin=str(paths["excalidraw_bin"]),
        upstream_url=UPSTREAM_URL,
        upstream_key={"kind": "env", "name": "TEST_GROKBOT_UPSTREAM"},
    )
    tools = build_tools_from_config(
        config,
        native_client_factory=lambda **_kwargs: object(),
        excalidraw_client_factory=factory,
    )
    started = tools.executor.excalidraw.start_client(
        {"command": "/bin/evil", "args": ["-c", "rm -rf /"], "cwd": "/tmp", "shell": True}
    )
    assert created[0]["executable"] == str(paths["excalidraw_bin"])
    assert created[0]["staging_dir"] == str(paths["staging_dir"])
    assert created[0]["args"] == ()
    assert "/bin/evil" not in json.dumps(created)
    assert started.__class__.__name__ == "RecordingLifecycle"
    launched: list[object] = []

    def boom(*args, **kwargs):
        launched.append((args, kwargs))
        raise OSError("stop")

    monkeypatch.setattr("brigade.grokbot_obsidian.excalidraw_client.subprocess.Popen", boom)
    default_tools = build_tools_from_config(config, native_client_factory=lambda **_kwargs: object())
    with pytest.raises(ObsidianError) as caught:
        default_tools.executor.excalidraw.start_client({"command": "/bin/evil", "args": ["-c", "rm"]})
    assert caught.value.code == "unavailable"
    argv = launched[0][0][0]
    assert isinstance(argv, list) and len(argv) == 1
    assert argv[0].startswith("/proc/self/fd/")
    assert launched[0][1].get("pass_fds")
    assert launched[0][1]["shell"] is False
    assert launched[0][1]["cwd"] == str(paths["staging_dir"])
    from brigade.grokbot_obsidian.excalidraw_client import create_excalidraw_stdio_client

    assert callable(create_excalidraw_stdio_client)


def test_default_factory_builds_streamable_client(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_GROKBOT_UPSTREAM", UPSTREAM_KEY)
    paths = _obsidian_paths(tmp_path)
    _write_runtime(paths["runtime_path"])
    config = ObsidianListenerConfig(
        target=tmp_path,
        bind_host="127.0.0.1",
        bind_port=8773,
        allowed_hosts=(),
        allowed_origins=(),
        bearer=SECRET,
        runtime_path=str(paths["runtime_path"]),
        action_state_path=str(paths["action_state_path"]),
        approval_dir=str(paths["approval_dir"]),
        staging_dir=str(paths["staging_dir"]),
        excalidraw_bin=str(paths["excalidraw_bin"]),
        upstream_url=UPSTREAM_URL,
        upstream_key={"kind": "env", "name": "TEST_GROKBOT_UPSTREAM"},
    )
    tools = build_tools_from_config(config)
    assert isinstance(tools.native.client, StreamableNativeMcpClient)
    close_native_client(tools)


def test_upstream_key_must_differ_from_bearer_and_peer_tokens(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_GROKBOT_UPSTREAM", SECRET)
    monkeypatch.setenv("GROKBOT_DISPATCH_TOKEN", UPSTREAM_KEY)
    paths = _obsidian_paths(tmp_path)
    _write_runtime(paths["runtime_path"])
    config = ObsidianListenerConfig(
        target=tmp_path,
        bind_host="127.0.0.1",
        bind_port=8773,
        allowed_hosts=(),
        allowed_origins=(),
        bearer=SECRET,
        runtime_path=str(paths["runtime_path"]),
        action_state_path=str(paths["action_state_path"]),
        approval_dir=str(paths["approval_dir"]),
        staging_dir=str(paths["staging_dir"]),
        excalidraw_bin=str(paths["excalidraw_bin"]),
        upstream_url=UPSTREAM_URL,
        upstream_key={"kind": "env", "name": "TEST_GROKBOT_UPSTREAM"},
    )
    with pytest.raises(ObsidianError) as caught:
        build_tools_from_config(config)
    assert caught.value.code == "invalid_request"
    assert SECRET not in str(caught.value)
    assert UPSTREAM_KEY not in str(caught.value)


def test_jsonrpc_batches_and_unknown_tools_are_rejected():
    assert _invalid_obsidian_tool_request(b"not-json") is False
    assert _invalid_obsidian_tool_request(b"[]") is True
    assert (
        _invalid_obsidian_tool_request(
            b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"vault_write","arguments":{}}}'
        )
        is True
    )
    assert (
        _invalid_obsidian_tool_request(
            b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"obsidian_capabilities","arguments":{}}}'
        )
        is False
    )
    assert set(TOOLS) == {
        "obsidian_capabilities",
        "obsidian_search",
        "obsidian_read",
        "obsidian_action_status",
        "obsidian_propose_action",
        "obsidian_execute_action",
    }


def test_obsidian_unit_uses_shared_listener_recovery_policy(tmp_path: Path, monkeypatch):
    grokbot_packs.apply_setup(
        tmp_path,
        "obsidian-operator",
        bearer_env="TEST_GROKBOT_BEARER",
        **_obsidian_setup_kwargs(tmp_path, monkeypatch),
    )
    assert_listener_recovery_policy(render_unit(tmp_path))
