"""Independent connector lifecycle contract."""

from tests.dot_support import requires_local_state

import json
from pathlib import Path
from brigade.dot.contracts import DotError
import pytest
from brigade.dot import connector_registry as registry
from brigade import grokbot_packs
from tests.test_cloud_agent_profile import cloud_document


def setup_runtime(tmp_path):
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps(cloud_document(tmp_path)))
    path.chmod(0o600)
    return path


def test_closed_generic_registry():
    assert {p["id"] for p in registry.packaged_manifests()} == {"dot", "duet", "cloud-agent"}
    with pytest.raises(registry.PackError):
        registry.show_pack("unknown")


def test_existing_dot_duet_config_paths_unchanged(tmp_path):
    for name in ("dot", "duet"):
        assert registry.instance_config_path(tmp_path, name) == grokbot_packs.instance_config_path(tmp_path, name)
    assert (
        registry.instance_config_path(tmp_path, "cloud-agent")
        == tmp_path / ".brigade/connectors/cloud-agent/instance.json"
    )


@requires_local_state
def test_canonical_unit_argv(dot_private_dir):
    tmp_path = dot_private_dir.parent
    runtime = setup_runtime(tmp_path)
    preview = registry.preview_setup(tmp_path, "cloud-agent", runtime_path=runtime)
    assert not registry.instance_config_path(tmp_path, "cloud-agent").exists()
    assert preview["bind"] == "127.0.0.1:8783"
    registry.apply_setup(tmp_path, "cloud-agent", runtime_path=runtime)
    unit = registry.render_install_service(tmp_path, "cloud-agent")
    assert "hub connector serve --id cloud-agent" in unit
    assert "example-listener-credential" not in unit


@requires_local_state
def test_nonloopback_refused_even_with_explicit_hosts(dot_private_dir):
    tmp_path = dot_private_dir.parent
    runtime = setup_runtime(tmp_path)
    with pytest.raises(registry.PackError):
        registry.preview_setup(tmp_path, "cloud-agent", runtime_path=runtime, bind="0.0.0.0:8783")
    with pytest.raises(registry.PackError, match="unsafe-bind"):
        registry.preview_setup(
            tmp_path,
            "cloud-agent",
            runtime_path=runtime,
            bind="0.0.0.0:8783",
            allowed_hosts=["connector.example.test:8783"],
        )


@pytest.mark.parametrize("shared", ["token", "hub-token", "state", "principal", "audience"])
@requires_local_state
def test_cloud_agent_rejects_shared_identity(dot_private_dir, shared):
    tmp_path = dot_private_dir.parent
    from tests.cloud_agent_support import _memory_document
    from brigade.dot.runtime_config import DUET_RUNTIME_SCHEMA

    other = tmp_path / "other"
    other.mkdir(mode=0o700)
    data = cloud_document(other)
    data["schema"] = DUET_RUNTIME_SCHEMA
    data["messaging_enabled"] = False
    data["hub"]["principal"] = "agent:duet"
    data["auth"] = _memory_document()["auth"]
    Path(data["hub"]["message_token_file"]).write_text("other-example-hub-credential-00000000000000000")
    other_runtime = other / "runtime.json"
    other_runtime.write_text(json.dumps(data))
    other_runtime.chmod(0o600)
    instance = tmp_path / ".brigade/grokbot/packs/duet.json"
    instance.parent.mkdir(parents=True)
    instance.write_text(
        json.dumps(
            dict(
                schema=registry.INSTANCE_SCHEMA,
                pack_id="duet",
                pack_version="1.0.0",
                bind="127.0.0.1:8781",
                allowed_hosts=[],
                allowed_origins=[],
                public_route="",
                runtime_path=str(other_runtime),
            )
        )
    )
    instance.chmod(0o600)
    payload = cloud_document(tmp_path)
    if shared == "token":
        payload["auth"]["token_file"] = data["hub"]["message_token_file"]
    elif shared == "hub-token":
        payload["hub"]["message_token_file"] = data["hub"]["message_token_file"]
    elif shared == "state":
        payload["paths"]["state_dir"] = data["paths"]["state_dir"]
    elif shared == "principal":
        payload["hub"]["principal"] = data["hub"]["principal"]
    else:
        payload["auth"] = {**data["auth"], "mode": "access"}
    runtime = tmp_path / "runtime.json"
    runtime.write_text(json.dumps(payload))
    runtime.chmod(0o600)
    with pytest.raises((registry.PackError, DotError)):
        registry.preview_setup(tmp_path, "cloud-agent", runtime_path=runtime)


@requires_local_state
def test_doctor_reports_token_mode_without_access_dependency(dot_private_dir, monkeypatch):
    from brigade.dot import lifecycle
    from brigade.dot import access_auth

    root = dot_private_dir.parent
    runtime = setup_runtime(root)
    registry.apply_setup(root, "cloud-agent", runtime_path=runtime)
    monkeypatch.setattr(access_auth, "_jwt", lambda: pytest.fail("token mode must not require JWT"))
    monkeypatch.setattr(lifecycle, "_probe", lambda *args, **kwargs: (401, None))
    monkeypatch.setattr(lifecycle, "authenticated_authority", lambda *args, **kwargs: None)
    checks = registry.doctor(root, "cloud-agent")
    assert {"check": "auth-mode", "status": "ok", "mode": "token"} in checks
    assert {"check": "token-config", "status": "ok"} in checks
    assert {"check": "dependency", "status": "ok"} in checks


@requires_local_state
def test_collision_and_remove_preserve_runtime(dot_private_dir):
    root = dot_private_dir.parent
    runtime = setup_runtime(root)
    with pytest.raises(registry.PackError, match="duplicate-port"):
        registry.preview_setup(root, "cloud-agent", runtime_path=runtime, bind="127.0.0.1:8779")
    registry.apply_setup(root, "cloud-agent", runtime_path=runtime)
    before = runtime.read_bytes()
    preview = registry.preview_remove(root, "cloud-agent")
    assert preview["paths"] and registry.instance_config_path(root, "cloud-agent").exists()
    registry.apply_remove(root, "cloud-agent")
    assert not registry.instance_config_path(root, "cloud-agent").exists()
    assert runtime.read_bytes() == before


@requires_local_state
def test_failed_write_restores_instance(dot_private_dir, monkeypatch):
    from brigade import grokbot_ops

    root = dot_private_dir.parent
    runtime = setup_runtime(root)
    registry.apply_setup(root, "cloud-agent", runtime_path=runtime)
    instance = registry.instance_config_path(root, "cloud-agent")
    before = instance.read_bytes()

    def fail(*args, **kwargs):
        raise OSError("fake write failure")

    monkeypatch.setattr(grokbot_ops, "_write_text_nofollow_atomic", fail)
    with pytest.raises(registry.PackError, match="unsafe-path"):
        registry.apply_setup(root, "cloud-agent", runtime_path=runtime)
    assert instance.read_bytes() == before


@requires_local_state
def test_collision_with_legacy_listener_config(dot_private_dir):
    from brigade import grokbot_ops

    root = dot_private_dir.parent
    runtime = setup_runtime(root)
    legacy = grokbot_ops.config_path(root, "operator")
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(json.dumps({"bind": "127.0.0.1:8888"}))
    with pytest.raises(registry.PackError, match="duplicate-port"):
        registry.preview_setup(root, "cloud-agent", runtime_path=runtime, bind="127.0.0.1:8888")


@requires_local_state
def test_legacy_registry_respects_generic_installed_port(dot_private_dir):
    root = dot_private_dir.parent
    runtime = setup_runtime(root)
    registry.apply_setup(root, "cloud-agent", runtime_path=runtime, bind="127.0.0.1:8888")
    with pytest.raises(registry.PackError, match="duplicate-port"):
        grokbot_packs._assert_setup_bind_available(root, "operator", "127.0.0.1:8888")
