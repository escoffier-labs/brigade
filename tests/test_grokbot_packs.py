"""Tests for the first-party Grok Bot connector-pack registry and lifecycle."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from brigade import cli, grokbot_mcp, grokbot_ops, grokbot_packs
from tests.test_grokbot_ops import assert_listener_recovery_policy

SECRET = "not-a-real-token"
QUEUE_PACK_IDS = ("implementation-worker", "operator", "repository-scout")
PACK_IDS = (
    "backup-steward",
    "cerebro-memory",
    "fleet-steward",
    "implementation-worker",
    "n8n-operator",
    "obsidian-operator",
    "operator",
    "repository-scout",
    "wazuh-triage",
)
CEREBRO_TOOLS = (
    "cerebro_health",
    "cerebro_proposal_status",
    "cerebro_propose",
    "cerebro_search",
    "cerebro_show",
)
FLEET_TOOLS = (
    "execute_remediation",
    "fleet_overview",
    "host_status",
    "incident_bundle",
    "propose_remediation",
    "service_health",
)
BACKUP_TOOLS = (
    "backup_execute_action",
    "backup_operation_status",
    "backup_overview",
    "backup_propose_action",
    "backup_restore_readiness",
    "backup_target_status",
)
OBSIDIAN_TOOLS = (
    "obsidian_action_status",
    "obsidian_capabilities",
    "obsidian_execute_action",
    "obsidian_propose_action",
    "obsidian_read",
    "obsidian_search",
)
WAZUH_TOOLS = (
    "wazuh_action_status",
    "wazuh_alert_status",
    "wazuh_classify",
    "wazuh_incident_bundle",
    "wazuh_ingest",
    "wazuh_propose_remediation",
)
N8N_TOOLS = (
    "n8n_action_status",
    "n8n_execute_action",
    "n8n_execution_bundle",
    "n8n_overview",
    "n8n_propose_action",
    "n8n_workflow_status",
)


def _reject(reason: str):
    return pytest.raises(grokbot_packs.PackError, match=reason)


def _queue_tools(instance: str) -> list[str]:
    return sorted(grokbot_mcp.tools_for_instance(instance))


def _manifest(**overrides: object) -> dict[str, object]:
    instance = str(overrides.get("instance", "operator"))
    payload: dict[str, object] = {
        "schema": grokbot_packs.PACK_SCHEMA,
        "id": instance,
        "version": "1.0.0",
        "kind": "queue-role",
        "instance": instance,
        "default_bind": grokbot_packs.DEFAULT_BINDS[instance],
        "public_route": "",
        "tools": _queue_tools(instance),
    }
    payload.update(overrides)
    return payload


def _pack_argv(target: Path, command: str, *extra: str) -> list[str]:
    return ["run", "cloud", "grokbot", "pack", command, "--target", str(target), *extra]


def _write_secret_file(path: Path) -> Path:
    path.write_text(SECRET + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def test_registry_is_closed_deterministic_and_exact_key_validated():
    packs = grokbot_packs.list_packs()
    assert [pack["id"] for pack in packs] == list(PACK_IDS)
    assert [pack["id"] for pack in grokbot_packs.list_packs()] == [pack["id"] for pack in packs]
    assert not hasattr(grokbot_packs, "register_pack")
    for pack in packs:
        assert set(pack) == grokbot_packs.PACK_KEYS
        assert pack["schema"] == grokbot_packs.PACK_SCHEMA
        shown = grokbot_packs.show_pack(pack["id"])
        assert shown == pack
        if pack["id"] in QUEUE_PACK_IDS:
            assert pack["kind"] == "queue-role"
            assert set(shown["tools"]) == grokbot_mcp.tools_for_instance(pack["instance"])
        elif pack["id"] == "backup-steward":
            assert pack["kind"] == "connector"
            assert shown["tools"] == list(BACKUP_TOOLS)
            assert shown["default_bind"] == "127.0.0.1:8772"
            assert shown["public_route"] == ""
        elif pack["id"] == "cerebro-memory":
            assert pack["kind"] == "connector"
            assert shown["tools"] == list(CEREBRO_TOOLS)
            assert shown["default_bind"] == "127.0.0.1:8770"
            assert shown["public_route"] == ""
        elif pack["id"] == "obsidian-operator":
            assert pack["kind"] == "connector"
            assert shown["tools"] == list(OBSIDIAN_TOOLS)
            assert shown["default_bind"] == "127.0.0.1:8773"
            assert shown["public_route"] == "/mcp"
        elif pack["id"] == "wazuh-triage":
            assert pack["kind"] == "connector"
            assert shown["tools"] == list(WAZUH_TOOLS)
            assert shown["default_bind"] == "127.0.0.1:8774"
            assert shown["public_route"] == ""
        elif pack["id"] == "n8n-operator":
            assert pack["kind"] == "connector"
            assert shown["tools"] == list(N8N_TOOLS)
            assert shown["default_bind"] == "127.0.0.1:8775"
            assert shown["public_route"] == ""
        else:
            assert pack["kind"] == "connector"
            assert pack["id"] == "fleet-steward"
            assert shown["tools"] == list(FLEET_TOOLS)
            assert shown["default_bind"] == "127.0.0.1:8771"
            assert shown["public_route"] == ""


def test_registry_ignores_user_supplied_pack_files(tmp_path: Path):
    rogue = tmp_path / grokbot_packs.INSTANCE_DIR / "cerebro.json"
    rogue.parent.mkdir(parents=True)
    rogue.write_text(json.dumps(_manifest(id="cerebro", instance="operator")), encoding="utf-8")
    assert [pack["id"] for pack in grokbot_packs.list_packs()] == list(PACK_IDS)
    with _reject("unknown-pack"):
        grokbot_packs.show_pack("cerebro")


def test_registry_rejects_duplicate_pack_ids():
    with _reject("duplicate-pack-id"):
        grokbot_packs.validate_registry([_manifest(), _manifest(id="operator", default_bind="127.0.0.1:8799")])


def test_registry_rejects_duplicate_ports():
    with _reject("duplicate-port"):
        grokbot_packs.validate_registry(
            [
                _manifest(),
                _manifest(id="repository-scout", instance="repository-scout", default_bind="127.0.0.1:8766"),
            ]
        )


def test_registry_rejects_overlapping_nonempty_public_routes():
    left = _manifest(public_route="/steward")
    right = _manifest(
        id="repository-scout",
        instance="repository-scout",
        default_bind="127.0.0.1:8767",
        public_route="/steward/status",
    )
    with _reject("overlapping-public-route"):
        grokbot_packs.validate_registry([left, right])
    grokbot_packs.validate_registry(
        [
            _manifest(public_route=""),
            _manifest(id="repository-scout", instance="repository-scout", default_bind="127.0.0.1:8767"),
        ]
    )


def test_registry_rejects_unsafe_or_non_loopback_default_binds():
    for bind in ("0.0.0.0:8766", "198.51.100.10:8766", "example.com:8766", "127.0.0.1:0"):
        with _reject("unsafe-bind"):
            grokbot_packs.validate_registry([_manifest(default_bind=bind)])


def test_registry_rejects_invalid_versions():
    for version in ("1", "1.0", "v1.0.0", "01.0.0", "1.0.0-beta", "latest", ""):
        with _reject("invalid-version"):
            grokbot_packs.validate_registry([_manifest(version=version)])


def test_registry_rejects_declared_tool_inventory_drift():
    with _reject("inventory-mismatch"):
        grokbot_packs.validate_registry([_manifest(tools=["grokbot_queue_list"])])
    with _reject("inventory-mismatch"):
        grokbot_packs.validate_registry([_manifest(tools=_queue_tools("operator") + ["hidden_tool"])])


def test_registry_rejects_unexpected_pack_keys():
    with _reject("unexpected-key"):
        grokbot_packs.validate_registry([_manifest(note="no")])


def test_first_party_queue_packs_keep_isolated_ports_tools_and_credentials():
    packs = {pack["id"]: pack for pack in grokbot_packs.list_packs()}
    queue_ports = {grokbot_mcp.parse_bind(packs[pack_id]["default_bind"])[1] for pack_id in QUEUE_PACK_IDS}
    assert queue_ports == {8766, 8767, 8768}
    assert grokbot_mcp.parse_bind(packs["cerebro-memory"]["default_bind"])[1] == 8770
    assert grokbot_mcp.parse_bind(packs["fleet-steward"]["default_bind"])[1] == 8771
    assert grokbot_mcp.parse_bind(packs["backup-steward"]["default_bind"])[1] == 8772
    assert grokbot_mcp.parse_bind(packs["obsidian-operator"]["default_bind"])[1] == 8773
    assert grokbot_mcp.parse_bind(packs["wazuh-triage"]["default_bind"])[1] == 8774
    assert grokbot_mcp.parse_bind(packs["n8n-operator"]["default_bind"])[1] == 8775
    assert packs["operator"]["tools"] == _queue_tools("operator")
    assert packs["repository-scout"]["tools"] == _queue_tools("repository-scout")
    assert packs["implementation-worker"]["tools"] == _queue_tools("implementation-worker")
    assert packs["operator"]["tools"] != packs["repository-scout"]["tools"]
    assert grokbot_packs.instance_config_path(Path("/tmp/target"), "operator") != grokbot_packs.instance_config_path(
        Path("/tmp/target"), "repository-scout"
    )
    assert grokbot_ops.config_path(Path("/tmp/target"), "operator") != grokbot_ops.config_path(
        Path("/tmp/target"), "repository-scout"
    )


def test_setup_preview_writes_nothing_and_apply_uses_mode_0600(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_GROKBOT_BEARER", SECRET)
    preview = grokbot_packs.preview_setup(tmp_path, "operator", bearer_env="TEST_GROKBOT_BEARER")
    assert preview["apply"] is False
    assert not grokbot_packs.instance_config_path(tmp_path, "operator").exists()
    assert not grokbot_ops.config_path(tmp_path, "operator").exists()

    applied = grokbot_packs.apply_setup(tmp_path, "operator", bearer_env="TEST_GROKBOT_BEARER")
    pack_path = grokbot_packs.instance_config_path(tmp_path, "operator")
    legacy_path = grokbot_ops.config_path(tmp_path, "operator")
    assert applied["apply"] is True
    assert pack_path.is_file()
    assert pack_path.stat().st_mode & 0o777 == 0o600
    assert legacy_path.is_file()
    assert legacy_path.stat().st_mode & 0o777 == 0o600
    pack_config = json.loads(pack_path.read_text(encoding="utf-8"))
    assert set(pack_config) == grokbot_packs.INSTANCE_KEYS
    assert SECRET not in json.dumps(pack_config)
    assert pack_config["bearer"] == {"kind": "env", "name": "TEST_GROKBOT_BEARER"}
    assert pack_config["bind"] == "127.0.0.1:8766"
    assert grokbot_ops.load_config(tmp_path, "operator")["instance"] == "operator"


def test_setup_rejects_missing_and_weak_secret_references(tmp_path: Path):
    with _reject("missing-secret-reference"):
        grokbot_packs.apply_setup(tmp_path, "operator")
    with _reject("missing-secret-reference"):
        grokbot_packs.apply_setup(tmp_path, "operator", bearer_env="TEST_GROKBOT_BEARER", bearer_file=tmp_path / "t")
    with _reject("weak-secret-reference"):
        grokbot_packs.apply_setup(tmp_path, "operator", bearer_env="not valid")
    with _reject("weak-secret-reference"):
        grokbot_packs.apply_setup(tmp_path, "operator", bearer={"kind": "env", "name": "TEST", "value": SECRET})
    assert not grokbot_packs.instance_config_path(tmp_path, "operator").exists()


def test_setup_is_idempotent_and_keeps_role_default_ports(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_GROKBOT_BEARER", SECRET)
    first = grokbot_packs.apply_setup(tmp_path, "operator", bearer_env="TEST_GROKBOT_BEARER")
    second = grokbot_packs.apply_setup(tmp_path, "operator", bearer_env="TEST_GROKBOT_BEARER")
    assert first["bind"] == second["bind"] == "127.0.0.1:8766"
    scout = grokbot_packs.apply_setup(tmp_path, "repository-scout", bearer_env="TEST_GROKBOT_BEARER")
    worker = grokbot_packs.apply_setup(tmp_path, "implementation-worker", bearer_env="TEST_GROKBOT_BEARER")
    assert scout["bind"] == "127.0.0.1:8767"
    assert worker["bind"] == "127.0.0.1:8768"


def test_setup_refuses_bind_owned_by_another_packaged_default(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_GROKBOT_BEARER", SECRET)
    with _reject("duplicate-port"):
        grokbot_packs.preview_setup(tmp_path, "operator", bind="127.0.0.1:8767", bearer_env="TEST_GROKBOT_BEARER")
    with _reject("duplicate-port"):
        grokbot_packs.apply_setup(tmp_path, "operator", bind="127.0.0.1:8767", bearer_env="TEST_GROKBOT_BEARER")
    assert not grokbot_packs.instance_config_path(tmp_path, "operator").exists()
    assert not grokbot_ops.config_path(tmp_path, "operator").exists()


def test_setup_refuses_bind_owned_by_another_installed_custom_bind(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_GROKBOT_BEARER", SECRET)
    grokbot_packs.apply_setup(tmp_path, "repository-scout", bind="127.0.0.1:8799", bearer_env="TEST_GROKBOT_BEARER")
    before = grokbot_packs.instance_config_path(tmp_path, "repository-scout").read_bytes()
    with _reject("duplicate-port"):
        grokbot_packs.apply_setup(tmp_path, "operator", bind="127.0.0.1:8799", bearer_env="TEST_GROKBOT_BEARER")
    assert not grokbot_packs.instance_config_path(tmp_path, "operator").exists()
    assert not grokbot_ops.config_path(tmp_path, "operator").exists()
    assert grokbot_packs.instance_config_path(tmp_path, "repository-scout").read_bytes() == before


def test_setup_same_instance_custom_bind_is_idempotent(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_GROKBOT_BEARER", SECRET)
    first = grokbot_packs.apply_setup(tmp_path, "operator", bind="127.0.0.1:8799", bearer_env="TEST_GROKBOT_BEARER")
    second = grokbot_packs.apply_setup(tmp_path, "operator", bind="127.0.0.1:8799", bearer_env="TEST_GROKBOT_BEARER")
    assert first["bind"] == second["bind"] == "127.0.0.1:8799"
    scout = grokbot_packs.apply_setup(tmp_path, "repository-scout", bearer_env="TEST_GROKBOT_BEARER")
    assert scout["bind"] == "127.0.0.1:8767"


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX no-follow parent walk")
def test_setup_refuses_unsafe_existing_config_paths_without_exposing_contents(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_GROKBOT_BEARER", SECRET)
    decoy = tmp_path / "decoy.json"
    decoy.write_text(
        json.dumps({"bind": "127.0.0.1:8799", "bearer": {"value": SECRET}}, indent=2) + "\n",
        encoding="utf-8",
    )
    scout_pack = grokbot_packs.instance_config_path(tmp_path, "repository-scout")
    scout_pack.parent.mkdir(parents=True)
    scout_pack.symlink_to(decoy)
    with _reject("unsafe-path") as caught:
        grokbot_packs.apply_setup(tmp_path, "operator", bind="127.0.0.1:8790", bearer_env="TEST_GROKBOT_BEARER")
    assert SECRET not in str(caught.value)
    assert SECRET not in caught.value.reason
    assert decoy.read_text(encoding="utf-8").endswith("\n")
    assert SECRET in decoy.read_text(encoding="utf-8")
    assert not grokbot_packs.instance_config_path(tmp_path, "operator").exists()

    scout_pack.unlink()
    scout_legacy = grokbot_ops.config_path(tmp_path, "repository-scout")
    scout_legacy.symlink_to(decoy)
    with _reject("unsafe-path") as caught:
        grokbot_packs.apply_setup(tmp_path, "operator", bind="127.0.0.1:8790", bearer_env="TEST_GROKBOT_BEARER")
    assert SECRET not in str(caught.value)
    assert SECRET not in caught.value.reason
    assert decoy.read_text(encoding="utf-8").count(SECRET) == 1
    assert not grokbot_packs.instance_config_path(tmp_path, "operator").exists()


def _fail_legacy_config_write(monkeypatch: pytest.MonkeyPatch, target: Path, instance: str) -> None:
    real_write = grokbot_ops._write_text_nofollow_atomic
    legacy = grokbot_ops.config_path(target, instance)

    def write_or_fail(path: Path, data: str, **kwargs: object) -> None:
        if Path(path) == legacy:
            raise OSError("disk full")
        real_write(path, data, **kwargs)

    monkeypatch.setattr(grokbot_ops, "_write_text_nofollow_atomic", write_or_fail)


def _grokbot_config_files(target: Path) -> list[Path]:
    root = target / ".brigade" / "grokbot"
    if not root.exists():
        return []
    return sorted(path for path in root.rglob("*") if path.is_file())


def test_setup_first_install_failure_leaves_neither_config_store(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_GROKBOT_BEARER", SECRET)
    _fail_legacy_config_write(monkeypatch, tmp_path, "operator")
    with _reject("unsafe-path"):
        grokbot_packs.apply_setup(tmp_path, "operator", bearer_env="TEST_GROKBOT_BEARER")
    assert not grokbot_packs.instance_config_path(tmp_path, "operator").exists()
    assert not grokbot_ops.config_path(tmp_path, "operator").exists()
    assert _grokbot_config_files(tmp_path) == []


def test_setup_update_failure_preserves_existing_bytes_and_modes(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_GROKBOT_BEARER", SECRET)
    grokbot_packs.apply_setup(tmp_path, "operator", bearer_env="TEST_GROKBOT_BEARER")
    pack_path = grokbot_packs.instance_config_path(tmp_path, "operator")
    legacy_path = grokbot_ops.config_path(tmp_path, "operator")
    prior_pack = pack_path.read_bytes()
    prior_legacy = legacy_path.read_bytes()
    prior_pack_mode = pack_path.stat().st_mode & 0o777
    prior_legacy_mode = legacy_path.stat().st_mode & 0o777
    assert prior_pack != b""
    assert prior_legacy != b""

    _fail_legacy_config_write(monkeypatch, tmp_path, "operator")
    with _reject("unsafe-path"):
        grokbot_packs.apply_setup(tmp_path, "operator", bind="127.0.0.1:8799", bearer_env="TEST_GROKBOT_BEARER")
    assert pack_path.read_bytes() == prior_pack
    assert legacy_path.read_bytes() == prior_legacy
    assert pack_path.stat().st_mode & 0o777 == prior_pack_mode
    assert legacy_path.stat().st_mode & 0o777 == prior_legacy_mode
    assert _grokbot_config_files(tmp_path) == [legacy_path, pack_path]


def _assert_error_hides_secrets_and_paths(
    caught: pytest.ExceptionInfo[grokbot_packs.PackError],
    *paths: Path,
    hidden: str = SECRET,
) -> None:
    rendered = str(caught.value)
    assert hidden not in rendered
    assert hidden not in caught.value.reason
    for path in paths:
        assert str(path) not in rendered
        assert str(path) not in caught.value.reason
        if path.is_file():
            assert path.read_text(encoding="utf-8") not in rendered


def test_setup_rollback_restore_failure_raises_rollback_failed(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_GROKBOT_BEARER", SECRET)
    grokbot_packs.apply_setup(tmp_path, "operator", bearer_env="TEST_GROKBOT_BEARER")
    pack_path = grokbot_packs.instance_config_path(tmp_path, "operator")
    legacy_path = grokbot_ops.config_path(tmp_path, "operator")
    _fail_legacy_config_write(monkeypatch, tmp_path, "operator")

    real_restore = grokbot_packs._restore_regular_file

    def restore_or_fail(path: Path, snapshot: tuple[str, int] | None) -> None:
        if Path(path) == pack_path:
            raise grokbot_packs.PackError("unsafe-path")
        real_restore(path, snapshot)

    monkeypatch.setattr(grokbot_packs, "_restore_regular_file", restore_or_fail)
    with _reject("rollback-failed") as caught:
        grokbot_packs.apply_setup(tmp_path, "operator", bind="127.0.0.1:8799", bearer_env="TEST_GROKBOT_BEARER")
    _assert_error_hides_secrets_and_paths(caught, pack_path, legacy_path)
    assert caught.value.reason == "rollback-failed"


def test_setup_rollback_verification_mismatch_raises_rollback_failed(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_GROKBOT_BEARER", SECRET)
    grokbot_packs.apply_setup(tmp_path, "operator", bearer_env="TEST_GROKBOT_BEARER")
    pack_path = grokbot_packs.instance_config_path(tmp_path, "operator")
    legacy_path = grokbot_ops.config_path(tmp_path, "operator")
    prior_pack = pack_path.read_bytes()
    prior_legacy = legacy_path.read_bytes()
    prior_pack_mode = pack_path.stat().st_mode & 0o777
    _fail_legacy_config_write(monkeypatch, tmp_path, "operator")

    real_restore = grokbot_packs._restore_regular_file

    def restore_then_mismatch(path: Path, snapshot: tuple[str, int] | None) -> None:
        real_restore(path, snapshot)
        if Path(path) == pack_path:
            path.write_bytes(b'{"tampered":true}\n')
            path.chmod(prior_pack_mode)

    monkeypatch.setattr(grokbot_packs, "_restore_regular_file", restore_then_mismatch)
    with _reject("rollback-failed") as caught:
        grokbot_packs.apply_setup(tmp_path, "operator", bind="127.0.0.1:8799", bearer_env="TEST_GROKBOT_BEARER")
    _assert_error_hides_secrets_and_paths(caught, pack_path, legacy_path)
    assert caught.value.reason == "rollback-failed"
    assert '{"tampered":true}' not in str(caught.value)
    assert pack_path.read_bytes() != prior_pack
    assert legacy_path.read_bytes() == prior_legacy


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX no-follow parent walk")
def test_setup_and_removal_refuse_descriptor_unsafe_paths(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_GROKBOT_BEARER", SECRET)
    outside = tmp_path / "outside"
    outside.mkdir()
    config_parent = tmp_path / ".brigade"
    config_parent.symlink_to(outside, target_is_directory=True)
    with _reject("unsafe-path"):
        grokbot_packs.apply_setup(tmp_path, "operator", bearer_env="TEST_GROKBOT_BEARER")
    assert list(outside.iterdir()) == []

    config_parent.unlink()
    grokbot_packs.apply_setup(tmp_path, "operator", bearer_env="TEST_GROKBOT_BEARER")
    pack_path = grokbot_packs.instance_config_path(tmp_path, "operator")
    decoy = tmp_path / "decoy.json"
    decoy.write_text("keep me\n", encoding="utf-8")
    pack_path.unlink()
    pack_path.symlink_to(decoy)
    with _reject("symlink"):
        grokbot_packs.apply_remove(tmp_path, "operator")
    assert decoy.read_text(encoding="utf-8") == "keep me\n"


def test_update_preview_writes_nothing_and_apply_stays_compatible(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_GROKBOT_BEARER", SECRET)
    grokbot_packs.apply_setup(tmp_path, "operator", bearer_env="TEST_GROKBOT_BEARER")
    preview = grokbot_packs.preview_update(tmp_path, "operator")
    assert preview["apply"] is False
    assert (
        json.loads(grokbot_packs.instance_config_path(tmp_path, "operator").read_text(encoding="utf-8"))["pack_version"]
        == "1.0.0"
    )

    monkeypatch.setattr(
        grokbot_packs,
        "_PACKAGED",
        tuple(
            pack if pack["id"] != "operator" else {**pack, "version": "1.2.0"}
            for pack in grokbot_packs.packaged_manifests()
        ),
    )
    applied = grokbot_packs.apply_update(tmp_path, "operator")
    assert applied["apply"] is True
    config = json.loads(grokbot_packs.instance_config_path(tmp_path, "operator").read_text(encoding="utf-8"))
    assert config["pack_version"] == "1.2.0"
    assert config["bind"] == "127.0.0.1:8766"
    assert config["bearer"] == {"kind": "env", "name": "TEST_GROKBOT_BEARER"}

    monkeypatch.setattr(
        grokbot_packs,
        "_PACKAGED",
        tuple(
            pack if pack["id"] != "operator" else {**pack, "version": "2.0.0"}
            for pack in grokbot_packs.packaged_manifests()
        ),
    )
    with _reject("incompatible-version"):
        grokbot_packs.apply_update(tmp_path, "operator")
    assert (
        json.loads(grokbot_packs.instance_config_path(tmp_path, "operator").read_text(encoding="utf-8"))["pack_version"]
        == "1.2.0"
    )


def test_removal_preview_writes_nothing_and_apply_stays_in_owned_scope(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_GROKBOT_BEARER", SECRET)
    grokbot_packs.apply_setup(tmp_path, "operator", bearer_env="TEST_GROKBOT_BEARER")
    unit_dir = tmp_path / "units"
    grokbot_packs.apply_install_service(tmp_path, "operator", out_dir=unit_dir)
    neighbor = tmp_path / grokbot_ops.CONFIG_DIR / "keep-me.json"
    neighbor.write_text("{}\n", encoding="utf-8")
    unit_neighbor = unit_dir / "unrelated.service"
    unit_neighbor.write_text("keep\n", encoding="utf-8")

    preview = grokbot_packs.preview_remove(tmp_path, "operator", unit_dir=unit_dir)
    assert preview["apply"] is False
    assert grokbot_packs.instance_config_path(tmp_path, "operator").is_file()
    assert grokbot_ops.config_path(tmp_path, "operator").is_file()
    assert (unit_dir / grokbot_ops.unit_name("operator")).is_file()

    applied = grokbot_packs.apply_remove(tmp_path, "operator", unit_dir=unit_dir)
    assert applied["apply"] is True
    assert not grokbot_packs.instance_config_path(tmp_path, "operator").exists()
    assert not grokbot_ops.config_path(tmp_path, "operator").exists()
    assert not (unit_dir / grokbot_ops.unit_name("operator")).exists()
    assert neighbor.is_file()
    assert unit_neighbor.read_text(encoding="utf-8") == "keep\n"


def test_removal_refuses_unsafe_permissions_and_outside_paths(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_GROKBOT_BEARER", SECRET)
    grokbot_packs.apply_setup(tmp_path, "operator", bearer_env="TEST_GROKBOT_BEARER")
    pack_path = grokbot_packs.instance_config_path(tmp_path, "operator")
    pack_path.chmod(0o644)
    with _reject("unsafe-permissions"):
        grokbot_packs.apply_remove(tmp_path, "operator")
    assert pack_path.is_file()

    pack_path.chmod(0o600)
    outside = tmp_path / "outside-units"
    with _reject("outside-owned-path"):
        grokbot_packs.apply_remove(tmp_path, "operator", unit_dir=outside / "nested")
    assert pack_path.is_file()


def test_doctor_and_canary_are_sanitized_and_non_mutating(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_GROKBOT_BEARER", SECRET)
    grokbot_packs.apply_setup(tmp_path, "operator", bearer_env="TEST_GROKBOT_BEARER")
    token_file = _write_secret_file(tmp_path / "token.txt")
    grokbot_packs.apply_setup(
        tmp_path,
        "repository-scout",
        bearer_file=token_file,
    )
    before = grokbot_ops.config_path(tmp_path, "operator").read_text(encoding="utf-8")
    checks = grokbot_packs.doctor(tmp_path, "operator")
    canary = grokbot_packs.canary(tmp_path, "operator")
    rendered = json.dumps({"checks": checks, "canary": canary})
    assert SECRET not in rendered
    assert str(token_file) not in rendered
    assert all(set(check) == {"check", "status"} for check in checks)
    assert canary["ok"] is False
    assert grokbot_ops.config_path(tmp_path, "operator").read_text(encoding="utf-8") == before


def test_cli_preview_is_default_and_redacts_secrets(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("TEST_GROKBOT_BEARER", SECRET)
    assert cli.main(_pack_argv(tmp_path, "list", "--json")) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [pack["id"] for pack in listed["packs"]] == list(PACK_IDS)

    assert cli.main(_pack_argv(tmp_path, "show", "--id", "operator", "--json")) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["id"] == "operator"
    assert SECRET not in capsys.readouterr().out

    assert (
        cli.main(_pack_argv(tmp_path, "setup", "--id", "operator", "--bearer-env", "TEST_GROKBOT_BEARER", "--json"))
        == 0
    )
    preview = json.loads(capsys.readouterr().out)
    assert preview["apply"] is False
    assert SECRET not in json.dumps(preview)
    assert not grokbot_packs.instance_config_path(tmp_path, "operator").exists()

    assert (
        cli.main(
            _pack_argv(
                tmp_path, "setup", "--id", "operator", "--bearer-env", "TEST_GROKBOT_BEARER", "--apply", "--json"
            )
        )
        == 0
    )
    applied = json.loads(capsys.readouterr().out)
    assert applied["apply"] is True
    assert SECRET not in json.dumps(applied)
    assert grokbot_packs.instance_config_path(tmp_path, "operator").is_file()

    assert cli.main(_pack_argv(tmp_path, "doctor", "--id", "operator")) == 1
    doctor_out = capsys.readouterr()
    assert SECRET not in doctor_out.out + doctor_out.err
    assert "config" in doctor_out.out

    assert cli.main(_pack_argv(tmp_path, "canary", "--id", "operator")) == 1
    canary_out = capsys.readouterr()
    assert SECRET not in canary_out.out + canary_out.err

    assert cli.main(_pack_argv(tmp_path, "install-service", "--id", "operator")) == 0
    unit_text = capsys.readouterr().out
    assert "[Unit]" in unit_text
    assert SECRET not in unit_text

    assert cli.main(_pack_argv(tmp_path, "update", "--id", "operator", "--json")) == 0
    assert json.loads(capsys.readouterr().out)["apply"] is False

    assert cli.main(_pack_argv(tmp_path, "remove", "--id", "operator", "--json")) == 0
    assert json.loads(capsys.readouterr().out)["apply"] is False
    assert grokbot_packs.instance_config_path(tmp_path, "operator").is_file()

    assert cli.main(_pack_argv(tmp_path, "remove", "--id", "operator", "--apply")) == 0
    assert SECRET not in capsys.readouterr().out
    assert not grokbot_packs.instance_config_path(tmp_path, "operator").exists()


def test_parser_inventory_includes_pack_lifecycle_and_legacy_commands():
    from brigade.roadmap_cmd import _cli_command_paths

    paths = _cli_command_paths()
    for command in (
        "pack list",
        "pack show",
        "pack setup",
        "pack doctor",
        "pack canary",
        "pack install-service",
        "pack update",
        "pack remove",
        "pack relay-setup",
        "pack relay-doctor",
        "pack relay",
        "pack install-relay-service",
        "setup",
        "doctor",
        "canary",
        "install-service",
    ):
        assert f"brigade run-cloud grokbot {command}" in paths


def test_legacy_lifecycle_stays_compatible_with_pack_config(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_GROKBOT_BEARER", SECRET)
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
            ]
        )
        == 0
    )
    assert grokbot_ops.load_config(tmp_path, "operator")["bind"] == "127.0.0.1:8766"
    doctor = grokbot_packs.doctor(tmp_path, "operator")
    assert any(check["check"] == "config" and check["status"] == "ok" for check in doctor)

    assert grokbot_packs.apply_setup(tmp_path, "repository-scout", bearer_env="TEST_GROKBOT_BEARER")["apply"] is True
    assert grokbot_ops.load_config(tmp_path, "repository-scout")["bind"] == "127.0.0.1:8767"
    assert (
        cli.main(["run", "cloud", "grokbot", "doctor", "--target", str(tmp_path), "--instance", "repository-scout"])
        == 1
    )


def _cerebro_paths(tmp_path: Path) -> tuple[Path, Path]:
    workdir = tmp_path / "cerebro-work"
    workdir.mkdir()
    executable = tmp_path / "cerebro-agents"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    return executable, workdir


def test_cerebro_pack_is_closed_connector_with_exact_inventory():
    pack = grokbot_packs.show_pack("cerebro-memory")
    assert pack["kind"] == "connector"
    assert pack["instance"] == "cerebro-memory"
    assert pack["default_bind"] == "127.0.0.1:8770"
    assert pack["public_route"] == ""
    assert pack["tools"] == list(CEREBRO_TOOLS)
    with _reject("unknown-pack"):
        grokbot_packs.show_pack("cerebro")


def test_fleet_steward_pack_is_closed_connector_with_exact_inventory():
    pack = grokbot_packs.show_pack("fleet-steward")
    assert pack["kind"] == "connector"
    assert pack["instance"] == "fleet-steward"
    assert pack["default_bind"] == "127.0.0.1:8771"
    assert pack["public_route"] == ""
    assert pack["tools"] == list(FLEET_TOOLS)
    with _reject("unknown-pack"):
        grokbot_packs.show_pack("fleet")


def test_backup_steward_pack_is_closed_connector_with_exact_inventory():
    pack = grokbot_packs.show_pack("backup-steward")
    assert pack["kind"] == "connector"
    assert pack["instance"] == "backup-steward"
    assert pack["default_bind"] == "127.0.0.1:8772"
    assert pack["public_route"] == ""
    assert pack["tools"] == list(BACKUP_TOOLS)
    with _reject("unknown-pack"):
        grokbot_packs.show_pack("backup")


def test_n8n_operator_pack_is_closed_connector_with_exact_inventory():
    pack = grokbot_packs.show_pack("n8n-operator")
    assert pack["kind"] == "connector"
    assert pack["instance"] == "n8n-operator"
    assert pack["default_bind"] == "127.0.0.1:8775"
    assert pack["public_route"] == ""
    assert pack["tools"] == list(N8N_TOOLS)
    assert "n8n-operator" not in grokbot_packs.STEWARD_PACK_IDS
    with _reject("unknown-pack"):
        grokbot_packs.show_pack("n8n")


def test_cerebro_setup_preview_apply_and_queue_role_regression(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_GROKBOT_BEARER", SECRET)
    executable, workdir = _cerebro_paths(tmp_path)
    preview = grokbot_packs.preview_setup(
        tmp_path,
        "cerebro-memory",
        bearer_env="TEST_GROKBOT_BEARER",
        cli_executable=executable,
        workdir=workdir,
    )
    assert preview["apply"] is False
    assert preview["bind"] == "127.0.0.1:8770"
    assert str(executable) not in json.dumps(preview)
    assert str(workdir) not in json.dumps(preview)
    assert not grokbot_packs.instance_config_path(tmp_path, "cerebro-memory").exists()
    assert not grokbot_ops.config_path(tmp_path, "cerebro-memory").exists()

    applied = grokbot_packs.apply_setup(
        tmp_path,
        "cerebro-memory",
        bearer_env="TEST_GROKBOT_BEARER",
        cli_executable=executable,
        workdir=workdir,
    )
    pack_path = grokbot_packs.instance_config_path(tmp_path, "cerebro-memory")
    assert applied["apply"] is True
    assert pack_path.is_file()
    assert pack_path.stat().st_mode & 0o777 == 0o600
    assert not grokbot_ops.config_path(tmp_path, "cerebro-memory").exists()
    config = json.loads(pack_path.read_text(encoding="utf-8"))
    assert set(config) == grokbot_packs.CONNECTOR_INSTANCE_KEYS
    assert SECRET not in json.dumps(config)
    assert config["cli_executable"] == str(executable)
    assert config["workdir"] == str(workdir)
    assert config["bearer"] == {"kind": "env", "name": "TEST_GROKBOT_BEARER"}

    operator = grokbot_packs.apply_setup(tmp_path, "operator", bearer_env="TEST_GROKBOT_BEARER")
    assert operator["bind"] == "127.0.0.1:8766"
    assert grokbot_ops.config_path(tmp_path, "operator").is_file()
    assert grokbot_packs.instance_config_path(tmp_path, "operator").is_file()


def test_cerebro_setup_requires_path_refs_and_rejects_them_on_queue_roles(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_GROKBOT_BEARER", SECRET)
    executable, workdir = _cerebro_paths(tmp_path)
    with _reject("missing-path-reference"):
        grokbot_packs.apply_setup(tmp_path, "cerebro-memory", bearer_env="TEST_GROKBOT_BEARER")
    with _reject("unexpected-key"):
        grokbot_packs.apply_setup(
            tmp_path,
            "operator",
            bearer_env="TEST_GROKBOT_BEARER",
            cli_executable=executable,
            workdir=workdir,
        )
    with _reject("unsafe-path") as caught:
        grokbot_packs.apply_setup(
            tmp_path,
            "cerebro-memory",
            bearer_env="TEST_GROKBOT_BEARER",
            cli_executable="cerebro-agents",
            workdir=workdir,
        )
    assert "cerebro-agents" not in str(caught.value)
    assert not grokbot_packs.instance_config_path(tmp_path, "cerebro-memory").exists()
    assert not grokbot_ops.config_path(tmp_path, "operator").exists()


def test_cerebro_setup_refuses_queue_default_port_and_keeps_rollback(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_GROKBOT_BEARER", SECRET)
    executable, workdir = _cerebro_paths(tmp_path)
    with _reject("duplicate-port"):
        grokbot_packs.apply_setup(
            tmp_path,
            "cerebro-memory",
            bind="127.0.0.1:8766",
            bearer_env="TEST_GROKBOT_BEARER",
            cli_executable=executable,
            workdir=workdir,
        )
    grokbot_packs.apply_setup(tmp_path, "operator", bearer_env="TEST_GROKBOT_BEARER")
    with _reject("duplicate-port"):
        grokbot_packs.apply_setup(
            tmp_path,
            "operator",
            bind="127.0.0.1:8770",
            bearer_env="TEST_GROKBOT_BEARER",
        )
    assert grokbot_ops.load_config(tmp_path, "operator")["bind"] == "127.0.0.1:8766"

    grokbot_packs.apply_setup(
        tmp_path,
        "cerebro-memory",
        bearer_env="TEST_GROKBOT_BEARER",
        cli_executable=executable,
        workdir=workdir,
    )
    pack_path = grokbot_packs.instance_config_path(tmp_path, "cerebro-memory")
    prior = pack_path.read_bytes()
    real_write = grokbot_ops._write_text_nofollow_atomic

    def fail_updated_bind(path: Path, data: str, **kwargs: object) -> None:
        if Path(path) == pack_path and "8791" in data:
            raise OSError("disk full")
        real_write(path, data, **kwargs)

    monkeypatch.setattr(grokbot_ops, "_write_text_nofollow_atomic", fail_updated_bind)
    with _reject("unsafe-path"):
        grokbot_packs.apply_setup(
            tmp_path,
            "cerebro-memory",
            bind="127.0.0.1:8791",
            bearer_env="TEST_GROKBOT_BEARER",
            cli_executable=executable,
            workdir=workdir,
        )
    assert pack_path.read_bytes() == prior


def test_cerebro_doctor_canary_and_unit_hide_paths(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_GROKBOT_BEARER", SECRET)
    executable, workdir = _cerebro_paths(tmp_path)
    grokbot_packs.apply_setup(
        tmp_path,
        "cerebro-memory",
        bearer_env="TEST_GROKBOT_BEARER",
        cli_executable=executable,
        workdir=workdir,
    )
    checks = grokbot_packs.doctor(tmp_path, "cerebro-memory")
    canary = grokbot_packs.canary(tmp_path, "cerebro-memory")
    unit = grokbot_packs.render_install_service(tmp_path, "cerebro-memory")
    sanitized = json.dumps({"checks": checks, "canary": canary})
    assert SECRET not in sanitized
    assert str(executable) not in sanitized
    assert str(workdir) not in sanitized
    assert SECRET not in unit
    assert str(executable) not in unit
    assert all(set(check) == {"check", "status"} for check in checks)
    assert canary["ok"] is False
    assert "--pack" in unit
    assert "cerebro-memory" in unit
    assert "127.0.0.1:8770" in unit
    assert "NoNewPrivileges=yes" in unit
    assert "PrivateTmp=yes" in unit
    assert "ProtectSystem=strict" in unit
    assert "ProtectHome=read-only" in unit
    unit_dir = tmp_path / "units"
    installed = grokbot_packs.apply_install_service(tmp_path, "cerebro-memory", out_dir=unit_dir)
    assert installed["unit"] == grokbot_ops.unit_name("cerebro-memory")
    preview = grokbot_packs.preview_remove(tmp_path, "cerebro-memory", unit_dir=unit_dir)
    assert preview["apply"] is False
    grokbot_packs.apply_remove(tmp_path, "cerebro-memory", unit_dir=unit_dir)
    assert not grokbot_packs.instance_config_path(tmp_path, "cerebro-memory").exists()
    assert not (unit_dir / grokbot_ops.unit_name("cerebro-memory")).exists()
    assert not grokbot_ops.config_path(tmp_path, "cerebro-memory").exists()


def _fleet_paths(tmp_path: Path) -> dict[str, Path]:
    runtime = tmp_path / "runtime.json"
    ledger = tmp_path / "ledger" / "ledger.json"
    actions = tmp_path / "actions"
    approvals = tmp_path / "approvals"
    runtime.write_text("{}", encoding="utf-8")
    ledger.parent.mkdir(mode=0o700)
    actions.mkdir(mode=0o700)
    approvals.mkdir(mode=0o700)
    os.chmod(ledger.parent, 0o700)
    os.chmod(actions, 0o700)
    os.chmod(approvals, 0o700)
    return {
        "runtime_path": runtime,
        "ledger_path": ledger,
        "action_state_path": actions,
        "approval_dir": approvals,
    }


def test_fleet_setup_preview_apply_and_rejects_foreign_keys(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_GROKBOT_BEARER", SECRET)
    paths = _fleet_paths(tmp_path)
    preview = grokbot_packs.preview_setup(
        tmp_path,
        "fleet-steward",
        bearer_env="TEST_GROKBOT_BEARER",
        **paths,
    )
    assert preview["apply"] is False
    assert preview["bind"] == "127.0.0.1:8771"
    dumped = json.dumps(preview)
    assert str(paths["runtime_path"]) not in dumped
    assert str(paths["ledger_path"]) not in dumped
    assert not grokbot_packs.instance_config_path(tmp_path, "fleet-steward").exists()
    assert not grokbot_ops.config_path(tmp_path, "fleet-steward").exists()

    applied = grokbot_packs.apply_setup(
        tmp_path,
        "fleet-steward",
        bearer_env="TEST_GROKBOT_BEARER",
        **paths,
    )
    pack_path = grokbot_packs.instance_config_path(tmp_path, "fleet-steward")
    assert applied["apply"] is True
    assert pack_path.is_file()
    assert pack_path.stat().st_mode & 0o777 == 0o600
    assert not grokbot_ops.config_path(tmp_path, "fleet-steward").exists()
    config = json.loads(pack_path.read_text(encoding="utf-8"))
    assert set(config) == grokbot_packs.FLEET_INSTANCE_KEYS
    assert SECRET not in json.dumps(config)
    assert "cli_executable" not in config
    assert "workdir" not in config
    assert config["runtime_path"] == str(paths["runtime_path"])
    assert config["bearer"] == {"kind": "env", "name": "TEST_GROKBOT_BEARER"}

    executable, workdir = _cerebro_paths(tmp_path)
    with _reject("unexpected-key"):
        grokbot_packs.apply_setup(
            tmp_path,
            "fleet-steward",
            bearer_env="TEST_GROKBOT_BEARER",
            cli_executable=executable,
            workdir=workdir,
            **paths,
        )
    with _reject("unexpected-key"):
        grokbot_packs.apply_setup(
            tmp_path,
            "cerebro-memory",
            bearer_env="TEST_GROKBOT_BEARER",
            cli_executable=executable,
            workdir=workdir,
            runtime_path=paths["runtime_path"],
        )
    with _reject("unexpected-key"):
        grokbot_packs.apply_setup(
            tmp_path,
            "operator",
            bearer_env="TEST_GROKBOT_BEARER",
            runtime_path=paths["runtime_path"],
        )
    with _reject("missing-path-reference"):
        grokbot_packs.preview_setup(tmp_path, "fleet-steward", bearer_env="TEST_GROKBOT_BEARER")


def test_fleet_setup_refuses_queue_and_cerebro_default_ports(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_GROKBOT_BEARER", SECRET)
    paths = _fleet_paths(tmp_path)
    executable, workdir = _cerebro_paths(tmp_path)
    with _reject("duplicate-port"):
        grokbot_packs.apply_setup(
            tmp_path,
            "fleet-steward",
            bind="127.0.0.1:8766",
            bearer_env="TEST_GROKBOT_BEARER",
            **paths,
        )
    with _reject("duplicate-port"):
        grokbot_packs.apply_setup(
            tmp_path,
            "fleet-steward",
            bind="127.0.0.1:8770",
            bearer_env="TEST_GROKBOT_BEARER",
            **paths,
        )
    grokbot_packs.apply_setup(tmp_path, "operator", bearer_env="TEST_GROKBOT_BEARER")
    with _reject("duplicate-port"):
        grokbot_packs.apply_setup(
            tmp_path,
            "operator",
            bind="127.0.0.1:8771",
            bearer_env="TEST_GROKBOT_BEARER",
        )
    grokbot_packs.apply_setup(
        tmp_path,
        "cerebro-memory",
        bearer_env="TEST_GROKBOT_BEARER",
        cli_executable=executable,
        workdir=workdir,
    )
    with _reject("duplicate-port"):
        grokbot_packs.apply_setup(
            tmp_path,
            "cerebro-memory",
            bind="127.0.0.1:8771",
            bearer_env="TEST_GROKBOT_BEARER",
            cli_executable=executable,
            workdir=workdir,
        )
    with _reject("duplicate-port"):
        grokbot_packs.apply_setup(
            tmp_path,
            "fleet-steward",
            bind="127.0.0.1:8772",
            bearer_env="TEST_GROKBOT_BEARER",
            **paths,
        )
    with _reject("duplicate-port"):
        grokbot_packs.apply_setup(
            tmp_path,
            "fleet-steward",
            bind="127.0.0.1:8773",
            bearer_env="TEST_GROKBOT_BEARER",
            **paths,
        )
    with _reject("duplicate-port"):
        grokbot_packs.apply_setup(
            tmp_path,
            "fleet-steward",
            bind="127.0.0.1:8774",
            bearer_env="TEST_GROKBOT_BEARER",
            **paths,
        )
    with _reject("duplicate-port"):
        grokbot_packs.apply_setup(
            tmp_path,
            "fleet-steward",
            bind="127.0.0.1:8775",
            bearer_env="TEST_GROKBOT_BEARER",
            **paths,
        )
    assert grokbot_ops.load_config(tmp_path, "operator")["bind"] == "127.0.0.1:8766"


def test_backup_setup_preview_apply_and_rejects_foreign_keys(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_GROKBOT_BEARER", SECRET)
    paths = _fleet_paths(tmp_path)
    preview = grokbot_packs.preview_setup(
        tmp_path,
        "backup-steward",
        bearer_env="TEST_GROKBOT_BEARER",
        **paths,
    )
    assert preview["apply"] is False
    assert preview["bind"] == "127.0.0.1:8772"
    dumped = json.dumps(preview)
    assert str(paths["runtime_path"]) not in dumped
    assert str(paths["ledger_path"]) not in dumped
    assert not grokbot_packs.instance_config_path(tmp_path, "backup-steward").exists()
    assert not grokbot_ops.config_path(tmp_path, "backup-steward").exists()

    applied = grokbot_packs.apply_setup(
        tmp_path,
        "backup-steward",
        bearer_env="TEST_GROKBOT_BEARER",
        **paths,
    )
    pack_path = grokbot_packs.instance_config_path(tmp_path, "backup-steward")
    assert applied["apply"] is True
    assert pack_path.is_file()
    assert pack_path.stat().st_mode & 0o777 == 0o600
    assert not grokbot_ops.config_path(tmp_path, "backup-steward").exists()
    config = json.loads(pack_path.read_text(encoding="utf-8"))
    assert set(config) == grokbot_packs.FLEET_INSTANCE_KEYS
    assert SECRET not in json.dumps(config)
    assert "cli_executable" not in config
    assert config["runtime_path"] == str(paths["runtime_path"])
    assert config["bearer"] == {"kind": "env", "name": "TEST_GROKBOT_BEARER"}

    executable, workdir = _cerebro_paths(tmp_path)
    with _reject("unexpected-key"):
        grokbot_packs.apply_setup(
            tmp_path,
            "backup-steward",
            bearer_env="TEST_GROKBOT_BEARER",
            cli_executable=executable,
            workdir=workdir,
            **paths,
        )
    with _reject("missing-path-reference"):
        grokbot_packs.preview_setup(tmp_path, "backup-steward", bearer_env="TEST_GROKBOT_BEARER")


def test_backup_setup_refuses_existing_default_ports(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_GROKBOT_BEARER", SECRET)
    paths = _fleet_paths(tmp_path)
    for bind in (
        "127.0.0.1:8766",
        "127.0.0.1:8770",
        "127.0.0.1:8771",
        "127.0.0.1:8773",
        "127.0.0.1:8774",
        "127.0.0.1:8775",
    ):
        with _reject("duplicate-port"):
            grokbot_packs.apply_setup(
                tmp_path,
                "backup-steward",
                bind=bind,
                bearer_env="TEST_GROKBOT_BEARER",
                **paths,
            )
    grokbot_packs.apply_setup(tmp_path, "operator", bearer_env="TEST_GROKBOT_BEARER")
    with _reject("duplicate-port"):
        grokbot_packs.apply_setup(
            tmp_path,
            "operator",
            bind="127.0.0.1:8772",
            bearer_env="TEST_GROKBOT_BEARER",
        )
    assert grokbot_ops.load_config(tmp_path, "operator")["bind"] == "127.0.0.1:8766"


def test_backup_setup_keeps_prior_config_on_failed_rewrite(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_GROKBOT_BEARER", SECRET)
    paths = _fleet_paths(tmp_path)
    grokbot_packs.apply_setup(
        tmp_path,
        "backup-steward",
        bearer_env="TEST_GROKBOT_BEARER",
        **paths,
    )
    pack_path = grokbot_packs.instance_config_path(tmp_path, "backup-steward")
    prior = pack_path.read_bytes()
    real_write = grokbot_ops._write_text_nofollow_atomic

    def fail_updated_bind(path: Path, data: str, **kwargs: object) -> None:
        if Path(path) == pack_path and "8791" in data:
            raise OSError("disk full")
        real_write(path, data, **kwargs)

    monkeypatch.setattr(grokbot_ops, "_write_text_nofollow_atomic", fail_updated_bind)
    with _reject("unsafe-path"):
        grokbot_packs.apply_setup(
            tmp_path,
            "backup-steward",
            bind="127.0.0.1:8791",
            bearer_env="TEST_GROKBOT_BEARER",
            **paths,
        )
    assert pack_path.read_bytes() == prior


def _obsidian_paths(tmp_path: Path) -> dict[str, Path]:
    runtime = tmp_path / "obsidian-runtime.json"
    actions = tmp_path / "obsidian-actions"
    approvals = tmp_path / "obsidian-approvals"
    staging = tmp_path / "obsidian-staging"
    helper = tmp_path / "excalidraw-helper"
    runtime.write_text("{}\n", encoding="utf-8")
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
    monkeypatch.setenv("TEST_GROKBOT_UPSTREAM", "u" * 16)
    return {
        **_obsidian_paths(tmp_path),
        "upstream_url": "https://127.0.0.1:27124/",
        "upstream_key_env": "TEST_GROKBOT_UPSTREAM",
    }


def test_obsidian_is_closed_connector_not_a_steward():
    pack = grokbot_packs.show_pack("obsidian-operator")
    assert pack["kind"] == "connector"
    assert pack["instance"] == "obsidian-operator"
    assert pack["default_bind"] == "127.0.0.1:8773"
    assert pack["public_route"] == "/mcp"
    assert pack["tools"] == list(OBSIDIAN_TOOLS)
    assert "obsidian-operator" not in grokbot_packs.STEWARD_PACK_IDS
    with _reject("unknown-pack"):
        grokbot_packs.show_pack("obsidian")


def test_obsidian_setup_preview_apply_and_rejects_foreign_keys(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_GROKBOT_BEARER", SECRET)
    kwargs = _obsidian_setup_kwargs(tmp_path, monkeypatch)
    preview = grokbot_packs.preview_setup(
        tmp_path,
        "obsidian-operator",
        bearer_env="TEST_GROKBOT_BEARER",
        **kwargs,
    )
    assert preview["apply"] is False
    assert preview["bind"] == "127.0.0.1:8773"
    dumped = json.dumps(preview)
    assert str(kwargs["runtime_path"]) not in dumped
    assert str(kwargs["excalidraw_bin"]) not in dumped
    assert SECRET not in dumped
    assert "u" * 16 not in dumped
    assert not grokbot_packs.instance_config_path(tmp_path, "obsidian-operator").exists()
    assert not grokbot_ops.config_path(tmp_path, "obsidian-operator").exists()

    applied = grokbot_packs.apply_setup(
        tmp_path,
        "obsidian-operator",
        bearer_env="TEST_GROKBOT_BEARER",
        **kwargs,
    )
    pack_path = grokbot_packs.instance_config_path(tmp_path, "obsidian-operator")
    assert applied["apply"] is True
    assert pack_path.is_file()
    assert pack_path.stat().st_mode & 0o777 == 0o600
    assert not grokbot_ops.config_path(tmp_path, "obsidian-operator").exists()
    config = json.loads(pack_path.read_text(encoding="utf-8"))
    assert set(config) == grokbot_packs.OBSIDIAN_INSTANCE_KEYS
    assert SECRET not in json.dumps(config)
    assert "cli_executable" not in config
    assert "ledger_path" not in config
    assert config["public_route"] == "/mcp"
    assert config["runtime_path"] == str(kwargs["runtime_path"])
    assert config["bearer"] == {"kind": "env", "name": "TEST_GROKBOT_BEARER"}
    assert config["upstream_url"] == "https://127.0.0.1:27124/"
    assert config["upstream_key"] == {"kind": "env", "name": "TEST_GROKBOT_UPSTREAM"}
    assert "u" * 16 not in json.dumps(config)

    executable, workdir = _cerebro_paths(tmp_path)
    with _reject("unexpected-key"):
        grokbot_packs.apply_setup(
            tmp_path,
            "obsidian-operator",
            bearer_env="TEST_GROKBOT_BEARER",
            cli_executable=executable,
            workdir=workdir,
            **kwargs,
        )
    with _reject("unexpected-key"):
        grokbot_packs.apply_setup(
            tmp_path,
            "obsidian-operator",
            bearer_env="TEST_GROKBOT_BEARER",
            ledger_path=tmp_path / "ledger.json",
            **kwargs,
        )
    with _reject("missing-path-reference"):
        grokbot_packs.preview_setup(tmp_path, "obsidian-operator", bearer_env="TEST_GROKBOT_BEARER")
    with _reject("missing-upstream-reference"):
        grokbot_packs.preview_setup(
            tmp_path,
            "obsidian-operator",
            bearer_env="TEST_GROKBOT_BEARER",
            runtime_path=kwargs["runtime_path"],
            action_state_path=kwargs["action_state_path"],
            approval_dir=kwargs["approval_dir"],
            staging_dir=kwargs["staging_dir"],
            excalidraw_bin=kwargs["excalidraw_bin"],
        )
    with _reject("unexpected-key"):
        grokbot_packs.preview_setup(
            tmp_path,
            "operator",
            bearer_env="TEST_GROKBOT_BEARER",
            upstream_url="https://127.0.0.1:27124/",
            upstream_key_env="TEST_GROKBOT_UPSTREAM",
        )


def test_obsidian_setup_refuses_existing_default_ports(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_GROKBOT_BEARER", SECRET)
    kwargs = _obsidian_setup_kwargs(tmp_path, monkeypatch)
    for bind in ("127.0.0.1:8766", "127.0.0.1:8770", "127.0.0.1:8771", "127.0.0.1:8772"):
        with _reject("duplicate-port"):
            grokbot_packs.apply_setup(
                tmp_path,
                "obsidian-operator",
                bind=bind,
                bearer_env="TEST_GROKBOT_BEARER",
                **kwargs,
            )
    grokbot_packs.apply_setup(tmp_path, "operator", bearer_env="TEST_GROKBOT_BEARER")
    with _reject("duplicate-port"):
        grokbot_packs.apply_setup(
            tmp_path,
            "operator",
            bind="127.0.0.1:8773",
            bearer_env="TEST_GROKBOT_BEARER",
        )
    assert grokbot_ops.load_config(tmp_path, "operator")["bind"] == "127.0.0.1:8766"


def _record_pack_systemctl(monkeypatch, *, stdout="success\n", returncode=0, error: BaseException | None = None):
    recorded: list[list[str]] = []

    def fake_run(argv, *args, **kwargs):
        recorded.append(list(argv))
        if error is not None:
            raise error
        return subprocess.CompletedProcess(list(argv), returncode, stdout=stdout, stderr="secret-stderr")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return recorded


def test_pack_doctor_does_not_call_systemctl_without_service_result(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_GROKBOT_BEARER", SECRET)
    grokbot_packs.apply_setup(tmp_path, "operator", bearer_env="TEST_GROKBOT_BEARER")
    recorded = _record_pack_systemctl(monkeypatch)
    checks = grokbot_packs.doctor(tmp_path, "operator")
    assert recorded == []
    assert all(check["check"] != "service-result" for check in checks)
    explicit = grokbot_packs.doctor(tmp_path, "operator", service_result=False)
    assert recorded == []
    assert all(check["check"] != "service-result" for check in explicit)


@pytest.mark.parametrize("pack_id", PACK_IDS)
def test_pack_doctor_service_result_uses_exact_service_argv(tmp_path: Path, monkeypatch, pack_id: str):
    recorded = _record_pack_systemctl(monkeypatch)
    checks = grokbot_packs.doctor(tmp_path, pack_id, service_result=True)
    service = grokbot_ops.unit_name(pack_id)
    assert recorded == [
        ["systemctl", "--user", "show", service, "--property=Result", "--value"],
    ]
    assert service.endswith(".service")
    assert not service.endswith(".timer")
    assert checks[-1] == {"check": "service-result", "status": "ok"}
    assert all(set(check) == {"check", "status"} for check in checks)


def test_pack_doctor_appends_service_result_after_early_config_failure(tmp_path: Path, monkeypatch):
    recorded = _record_pack_systemctl(monkeypatch, stdout="failed\n")
    checks = grokbot_packs.doctor(tmp_path, "operator", service_result=True)
    assert any(check["check"] == "config" and check["status"] == "fail" for check in checks)
    assert checks[-1] == {"check": "service-result", "status": "fail"}
    assert recorded[0][3] == "brigade-grokbot-operator.service"


def test_pack_doctor_cli_json_and_plain_service_result_shape(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("TEST_GROKBOT_BEARER", SECRET)
    grokbot_packs.apply_setup(tmp_path, "operator", bearer_env="TEST_GROKBOT_BEARER")
    _record_pack_systemctl(monkeypatch, stdout="success\n")
    assert cli.main(_pack_argv(tmp_path, "doctor", "--id", "operator", "--service-result")) == 1
    plain = capsys.readouterr()
    assert "service-result: ok" in plain.out
    assert "secret-stderr" not in plain.out + plain.err
    assert SECRET not in plain.out + plain.err

    _record_pack_systemctl(monkeypatch, stdout="timeout\n")
    assert cli.main(_pack_argv(tmp_path, "doctor", "--id", "operator", "--service-result", "--json")) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["checks"][-1] == {"check": "service-result", "status": "fail"}
    assert all(set(check) == {"check", "status"} for check in payload["checks"])
    assert "timeout" not in json.dumps(payload)


def test_pack_doctor_cli_returns_zero_when_every_check_is_ok(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("TEST_GROKBOT_BEARER", SECRET)
    grokbot_packs.apply_setup(tmp_path, "operator", bearer_env="TEST_GROKBOT_BEARER")
    monkeypatch.setattr(
        grokbot_ops,
        "doctor",
        lambda *_args, **_kwargs: [
            {"check": "dependency", "status": "ok"},
            {"check": "config", "status": "ok"},
            {"check": "permissions", "status": "ok"},
            {"check": "queue", "status": "ok"},
            {"check": "endpoint", "status": "ok"},
        ],
    )
    _record_pack_systemctl(monkeypatch, stdout="success\n")
    assert cli.main(_pack_argv(tmp_path, "doctor", "--id", "operator", "--service-result", "--json")) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["checks"][-1] == {"check": "service-result", "status": "ok"}


def test_service_result_flag_is_pack_doctor_only(tmp_path: Path):
    with pytest.raises(SystemExit) as caught:
        cli.main(_pack_argv(tmp_path, "canary", "--id", "operator", "--service-result"))
    assert caught.value.code == 2
    with pytest.raises(SystemExit) as caught:
        cli.main(
            [
                "run",
                "cloud",
                "grokbot",
                "doctor",
                "--target",
                str(tmp_path),
                "--instance",
                "operator",
                "--service-result",
            ]
        )
    assert caught.value.code == 2


def test_first_party_pack_units_use_shared_listener_recovery_policy(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_GROKBOT_BEARER", SECRET)
    grokbot_packs.apply_setup(tmp_path, "operator", bearer_env="TEST_GROKBOT_BEARER")
    unit = grokbot_packs.render_install_service(tmp_path, "operator")
    assert_listener_recovery_policy(unit)
