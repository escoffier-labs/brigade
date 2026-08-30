"""n8n-operator lifecycle, doctor, canary, unit rendering, and CLI dispatch."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from brigade import cli, grokbot_ops, grokbot_packs
from brigade.grokbot_n8n.contracts import N8nError, PACK_ID, TOOLS
from brigade.grokbot_n8n.lifecycle import (
    N8nListenerConfig,
    build_tools_from_config,
    render_unit,
    validate_disjoint_state_paths,
)

SECRET = "not-a-real-token-value-32chars!!"


def _n8n_paths(tmp_path: Path) -> dict[str, Path]:
    runtime = tmp_path / "n8n-runtime.json"
    key = tmp_path / "n8n.key"
    actions = tmp_path / "n8n-actions"
    approvals = tmp_path / "n8n-approvals"
    key.write_text("n8n-placeholder-key-not-real\n", encoding="utf-8")
    os.chmod(key, 0o600)
    runtime.write_text(
        json.dumps(
            {
                "version": 1,
                "base_url": "https://n8n.example.invalid",
                "api_key_file": str(key),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    os.chmod(runtime, 0o600)
    actions.mkdir(mode=0o700)
    approvals.mkdir(mode=0o700)
    os.chmod(actions, 0o700)
    os.chmod(approvals, 0o700)
    return {
        "runtime_path": runtime,
        "action_state_path": actions,
        "approval_dir": approvals,
    }


def _listener_config(tmp_path: Path, paths: dict[str, Path]) -> N8nListenerConfig:
    return N8nListenerConfig(
        target=tmp_path,
        bind_host="127.0.0.1",
        bind_port=8775,
        allowed_hosts=("127.0.0.1",),
        allowed_origins=("http://127.0.0.1",),
        bearer=SECRET,
        runtime_path=str(paths["runtime_path"]),
        action_state_path=str(paths["action_state_path"]),
        approval_dir=str(paths["approval_dir"]),
    )


def _rewrite_runtime(paths: dict[str, Path], api_key_file: Path) -> None:
    payload = {
        "version": 1,
        "base_url": "https://n8n.example.invalid",
        "api_key_file": str(api_key_file),
    }
    paths["runtime_path"].write_text(json.dumps(payload) + "\n", encoding="utf-8")
    os.chmod(paths["runtime_path"], 0o600)


def test_disjoint_paths_reject_overlap_and_keep_approval_separate():
    with pytest.raises(N8nError) as caught:
        validate_disjoint_state_paths(
            "/var/lib/n8n/runtime.json", "/var/lib/n8n/runtime.json", "/var/lib/n8n/approvals"
        )
    assert caught.value.code == "invalid_request"
    assert "/var/lib" not in str(caught.value)
    with pytest.raises(N8nError):
        validate_disjoint_state_paths(
            "/var/lib/n8n/runtime.json", "/var/lib/n8n/actions", "/var/lib/n8n/actions/nested"
        )


def test_api_key_inside_writable_trees_is_rejected_before_key_read(tmp_path: Path, monkeypatch):
    paths = _n8n_paths(tmp_path)
    reads: list[str] = []

    def capture_key_read(path_text: str) -> str:
        reads.append(path_text)
        raise AssertionError(f"read overlapping key {path_text}")

    monkeypatch.setattr("brigade.grokbot_n8n.lifecycle.read_secure_api_key", capture_key_read)
    for tree_key in ("action_state_path", "approval_dir"):
        nested = paths[tree_key] / "n8n.key"
        nested.write_text("n8n-placeholder-key-not-real\n", encoding="utf-8")
        os.chmod(nested, 0o600)
        _rewrite_runtime(paths, nested)
        reads.clear()
        with pytest.raises(N8nError) as caught:
            build_tools_from_config(_listener_config(tmp_path, paths), client=object())
        assert caught.value.code == "invalid_request"
        assert reads == []
        assert str(nested) not in str(caught.value)
        assert "n8n-placeholder-key-not-real" not in str(caught.value)
        nested.unlink()

    sibling_key = tmp_path / "n8n.key"
    _rewrite_runtime(paths, sibling_key)
    monkeypatch.setattr(
        "brigade.grokbot_n8n.lifecycle.read_secure_api_key",
        lambda path_text: "n8n-placeholder-key-not-real",
    )
    tools = build_tools_from_config(_listener_config(tmp_path, paths), client=object())
    assert str(sibling_key) in tools.secrets
    assert tools.secrets.count("n8n-placeholder-key-not-real") == 1


def test_setup_preview_apply_rollback_and_foreign_keys(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_GROKBOT_BEARER", SECRET)
    paths = _n8n_paths(tmp_path)
    preview = grokbot_packs.preview_setup(
        tmp_path,
        PACK_ID,
        bearer_env="TEST_GROKBOT_BEARER",
        **paths,
    )
    assert preview["apply"] is False
    assert preview["bind"] == "127.0.0.1:8775"
    dumped = json.dumps(preview)
    assert SECRET not in dumped
    assert str(paths["runtime_path"]) not in dumped
    assert str(tmp_path / "n8n.key") not in dumped
    assert "n8n-placeholder-key-not-real" not in dumped
    assert not grokbot_packs.instance_config_path(tmp_path, PACK_ID).exists()

    applied = grokbot_packs.apply_setup(
        tmp_path,
        PACK_ID,
        bearer_env="TEST_GROKBOT_BEARER",
        **paths,
    )
    pack_path = grokbot_packs.instance_config_path(tmp_path, PACK_ID)
    assert applied["apply"] is True
    assert pack_path.is_file()
    assert pack_path.stat().st_mode & 0o777 == 0o600
    config = json.loads(pack_path.read_text(encoding="utf-8"))
    assert set(config) == grokbot_packs.N8N_INSTANCE_KEYS
    assert "ledger_path" not in config
    assert "cli_executable" not in config
    assert config["runtime_path"] == str(paths["runtime_path"])
    assert SECRET not in json.dumps(config)
    assert "n8n-placeholder-key-not-real" not in json.dumps(config)

    with pytest.raises(grokbot_packs.PackError, match="unexpected-key"):
        grokbot_packs.preview_setup(
            tmp_path,
            PACK_ID,
            bearer_env="TEST_GROKBOT_BEARER",
            ledger_path=tmp_path / "ledger.json",
            **paths,
        )
    with pytest.raises(grokbot_packs.PackError, match="missing-path-reference"):
        grokbot_packs.preview_setup(tmp_path, PACK_ID, bearer_env="TEST_GROKBOT_BEARER")

    prior = pack_path.read_bytes()
    real_write = grokbot_ops._write_text_nofollow_atomic

    def fail_updated_bind(path: Path, data: str, **kwargs: object) -> None:
        if Path(path) == pack_path and "8795" in data:
            raise OSError("disk full")
        real_write(path, data, **kwargs)

    monkeypatch.setattr(grokbot_ops, "_write_text_nofollow_atomic", fail_updated_bind)
    with pytest.raises(grokbot_packs.PackError, match="unsafe-path"):
        grokbot_packs.apply_setup(
            tmp_path,
            PACK_ID,
            bind="127.0.0.1:8795",
            bearer_env="TEST_GROKBOT_BEARER",
            **paths,
        )
    assert pack_path.read_bytes() == prior


def test_setup_refuses_packaged_default_ports(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_GROKBOT_BEARER", SECRET)
    paths = _n8n_paths(tmp_path)
    for bind in (
        "127.0.0.1:8766",
        "127.0.0.1:8770",
        "127.0.0.1:8771",
        "127.0.0.1:8772",
        "127.0.0.1:8773",
        "127.0.0.1:8774",
    ):
        with pytest.raises(grokbot_packs.PackError, match="duplicate-port"):
            grokbot_packs.apply_setup(
                tmp_path,
                PACK_ID,
                bind=bind,
                bearer_env="TEST_GROKBOT_BEARER",
                **paths,
            )
    grokbot_packs.apply_setup(tmp_path, "operator", bearer_env="TEST_GROKBOT_BEARER")
    with pytest.raises(grokbot_packs.PackError, match="duplicate-port"):
        grokbot_packs.apply_setup(
            tmp_path,
            "operator",
            bind="127.0.0.1:8775",
            bearer_env="TEST_GROKBOT_BEARER",
        )


def test_doctor_canary_and_unit_hide_secrets_and_restrict_writable_paths(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_GROKBOT_BEARER", SECRET)
    paths = _n8n_paths(tmp_path)
    grokbot_packs.apply_setup(tmp_path, PACK_ID, bearer_env="TEST_GROKBOT_BEARER", **paths)
    checks = grokbot_packs.doctor(tmp_path, PACK_ID)
    canary = grokbot_packs.canary(tmp_path, PACK_ID)
    unit = grokbot_packs.render_install_service(tmp_path, PACK_ID)
    sanitized = json.dumps({"checks": checks, "canary": canary})
    assert SECRET not in sanitized
    assert "n8n-placeholder-key-not-real" not in sanitized
    assert str(paths["runtime_path"]) not in sanitized
    assert str(tmp_path / "n8n.key") not in sanitized
    assert str(paths["approval_dir"]) not in sanitized
    assert SECRET not in unit
    assert "n8n-placeholder-key-not-real" not in unit
    assert str(paths["runtime_path"]) not in unit
    assert str(tmp_path / "n8n.key") not in unit
    assert str(paths["approval_dir"]) not in unit.split("ReadWritePaths=", 1)[-1]
    assert str(paths["action_state_path"]) in unit
    assert "--pack" in unit
    assert PACK_ID in unit
    assert "127.0.0.1:8775" in unit
    assert "ProtectSystem=strict" in unit
    assert "NoNewPrivileges=yes" in unit
    assert canary["ok"] is False
    assert all(set(check) == {"check", "status"} for check in checks)
    rendered = render_unit(tmp_path)
    assert "--bearer-env TEST_GROKBOT_BEARER" in rendered or "--bearer-env" in rendered
    unit_dir = tmp_path / "units"
    installed = grokbot_packs.apply_install_service(tmp_path, PACK_ID, out_dir=unit_dir)
    assert installed["unit"] == grokbot_ops.unit_name(PACK_ID)
    grokbot_packs.apply_remove(tmp_path, PACK_ID, unit_dir=unit_dir)
    assert not grokbot_packs.instance_config_path(tmp_path, PACK_ID).exists()


def test_hardlinked_runtime_key_is_rejected_after_path_containment(tmp_path: Path, monkeypatch):
    paths = _n8n_paths(tmp_path)
    key = tmp_path / "n8n.key"
    os.link(paths["runtime_path"], tmp_path / "runtime-alias.json")
    key.unlink()
    os.link(paths["runtime_path"], key)
    reads: list[str] = []

    def capture_key_read(path_text: str) -> str:
        reads.append(path_text)
        raise AssertionError(f"read aliased key {path_text}")

    monkeypatch.setattr("brigade.grokbot_n8n.lifecycle.read_secure_api_key", capture_key_read)
    with pytest.raises(N8nError) as caught:
        build_tools_from_config(_listener_config(tmp_path, paths), client=object())
    assert caught.value.code == "invalid_request"
    assert reads == []
    assert str(key) not in str(caught.value)
    assert str(paths["runtime_path"]) not in str(caught.value)
    assert "n8n-placeholder-key-not-real" not in str(caught.value)


def test_windows_lifecycle_fails_closed(tmp_path: Path, monkeypatch):
    paths = _n8n_paths(tmp_path)
    monkeypatch.setattr("brigade.grokbot_n8n.runtime_config.permission_policy", lambda: "unsupported")
    with pytest.raises(N8nError) as caught:
        build_tools_from_config(_listener_config(tmp_path, paths), client=object())
    assert caught.value.code == "invalid_request"
    assert "n8n environment is invalid" in str(caught.value)
    assert str(paths["runtime_path"]) not in str(caught.value)


def test_cli_pack_and_serve_choices_include_n8n_operator():
    parser = cli._build_parser()
    shown = parser.parse_args(["run", "cloud", "grokbot", "pack", "show", "--id", "n8n-operator", "--json"])
    assert shown.pack_id == "n8n-operator"
    served = parser.parse_args(
        [
            "run",
            "cloud",
            "grokbot",
            "serve",
            "--pack",
            "n8n-operator",
            "--bearer-env",
            "TEST_GROKBOT_BEARER",
        ]
    )
    assert served.pack == "n8n-operator"
    assert not hasattr(served, "api_key")
    assert not hasattr(served, "api_key_file")
    assert PACK_ID not in grokbot_packs.STEWARD_PACK_IDS
    assert set(TOOLS) == {
        "n8n_overview",
        "n8n_workflow_status",
        "n8n_execution_bundle",
        "n8n_propose_action",
        "n8n_action_status",
        "n8n_execute_action",
    }
