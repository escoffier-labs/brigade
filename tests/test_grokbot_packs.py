"""Tests for the first-party Grok Bot connector-pack registry and lifecycle."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from brigade import cli, grokbot_mcp, grokbot_ops, grokbot_packs

SECRET = "not-a-real-token"
PACK_IDS = ("implementation-worker", "operator", "repository-scout")


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
        assert pack["kind"] == "queue-role"
        shown = grokbot_packs.show_pack(pack["id"])
        assert shown == pack
        assert set(shown["tools"]) == grokbot_mcp.tools_for_instance(pack["instance"])


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
    for bind in ("0.0.0.0:8766", "192.168.1.10:8766", "example.com:8766", "127.0.0.1:0"):
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
    ports = {grokbot_mcp.parse_bind(pack["default_bind"])[1] for pack in packs.values()}
    assert ports == {8766, 8767, 8768}
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
