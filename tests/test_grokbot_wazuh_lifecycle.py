"""Wazuh triage lifecycle: preview, bind collision, canary, and inventory."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from brigade import cli, grokbot_ops, grokbot_packs
from brigade.grokbot_wazuh.contracts import TOOLS, WazuhError
from brigade.grokbot_wazuh.lifecycle import (
    render_unit,
    validate_disjoint_state_paths,
)

SECRET = "not-a-real-token-value-32chars!!"


def _wazuh_paths(tmp_path: Path) -> dict[str, Path]:
    runtime = tmp_path / "runtime.json"
    ledger = tmp_path / "state" / "wazuh.json"
    actions = tmp_path / "actions"
    approvals = tmp_path / "approvals"
    runtime.write_text("{}", encoding="utf-8")
    os.chmod(runtime, 0o600)
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


def test_disjoint_paths_reject_overlap_dots_and_relative():
    with pytest.raises(WazuhError) as caught:
        validate_disjoint_state_paths("/var/lib/a", "/var/lib/a/state.json", "/var/lib/b", "/var/lib/c")
    assert caught.value.code == "invalid_request"
    assert "/var/lib/a" not in str(caught.value)
    with pytest.raises(WazuhError):
        validate_disjoint_state_paths("/var/lib/../a", "/var/lib/b", "/var/lib/c", "/var/lib/d")
    with pytest.raises(WazuhError):
        validate_disjoint_state_paths("var/lib/a", "/var/lib/b", "/var/lib/c", "/var/lib/d")


def test_pack_setup_doctor_canary_and_unit_hide_secrets(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_GROKBOT_BEARER", SECRET)
    paths = _wazuh_paths(tmp_path)
    preview = grokbot_packs.preview_setup(
        tmp_path,
        "wazuh-triage",
        bearer_env="TEST_GROKBOT_BEARER",
        **paths,
    )
    assert preview["apply"] is False
    assert preview["bind"] == "127.0.0.1:8774"
    assert not grokbot_packs.instance_config_path(tmp_path, "wazuh-triage").exists()
    assert str(paths["runtime_path"]) not in json.dumps(preview)
    grokbot_packs.apply_setup(
        tmp_path,
        "wazuh-triage",
        bearer_env="TEST_GROKBOT_BEARER",
        **paths,
    )
    checks = grokbot_packs.doctor(tmp_path, "wazuh-triage")
    canary = grokbot_packs.canary(tmp_path, "wazuh-triage")
    unit = grokbot_packs.render_install_service(tmp_path, "wazuh-triage")
    sanitized = json.dumps({"checks": checks, "canary": canary})
    assert SECRET not in sanitized
    assert str(paths["runtime_path"]) not in sanitized
    assert SECRET not in unit
    assert str(paths["runtime_path"]) not in unit
    assert "--pack" in unit
    assert "wazuh-triage" in unit
    assert "127.0.0.1:8774" in unit
    assert "NoNewPrivileges=yes" in unit
    assert "KillMode=mixed" in unit
    assert "StandardOutput=journal" in unit
    assert "ProtectSystem=strict" in unit
    assert canary["ok"] is False
    assert all(set(check) == {"check", "status"} for check in checks)
    assert set(TOOLS) == {
        "wazuh_action_status",
        "wazuh_alert_status",
        "wazuh_classify",
        "wazuh_incident_bundle",
        "wazuh_ingest",
        "wazuh_propose_remediation",
    }
    unit_dir = tmp_path / "units"
    installed = grokbot_packs.apply_install_service(tmp_path, "wazuh-triage", out_dir=unit_dir)
    assert installed["unit"] == grokbot_ops.unit_name("wazuh-triage")
    grokbot_packs.apply_remove(tmp_path, "wazuh-triage", unit_dir=unit_dir)
    assert not grokbot_packs.instance_config_path(tmp_path, "wazuh-triage").exists()


def test_default_bind_collision_and_failed_rewrite_rollback(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_GROKBOT_BEARER", SECRET)
    paths = _wazuh_paths(tmp_path)
    for bind in ("127.0.0.1:8766", "127.0.0.1:8770", "127.0.0.1:8771", "127.0.0.1:8772", "127.0.0.1:8773"):
        with pytest.raises(grokbot_packs.PackError) as caught:
            grokbot_packs.apply_setup(
                tmp_path,
                "wazuh-triage",
                bind=bind,
                bearer_env="TEST_GROKBOT_BEARER",
                **paths,
            )
        assert caught.value.reason == "duplicate-port"
        assert not grokbot_packs.instance_config_path(tmp_path, "wazuh-triage").exists()
    grokbot_packs.apply_setup(
        tmp_path,
        "wazuh-triage",
        bearer_env="TEST_GROKBOT_BEARER",
        **paths,
    )
    pack_path = grokbot_packs.instance_config_path(tmp_path, "wazuh-triage")
    prior = pack_path.read_bytes()
    real_write = grokbot_ops._write_text_nofollow_atomic

    def fail_updated_bind(path: Path, data: str, **kwargs: object) -> None:
        if Path(path) == pack_path and "8794" in data:
            raise OSError("disk full")
        real_write(path, data, **kwargs)

    monkeypatch.setattr(grokbot_ops, "_write_text_nofollow_atomic", fail_updated_bind)
    with pytest.raises(grokbot_packs.PackError) as caught:
        grokbot_packs.apply_setup(
            tmp_path,
            "wazuh-triage",
            bind="127.0.0.1:8794",
            bearer_env="TEST_GROKBOT_BEARER",
            **paths,
        )
    assert caught.value.reason == "unsafe-path"
    assert pack_path.read_bytes() == prior


def test_unit_rendering_does_not_resolve_secret_values(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_GROKBOT_BEARER", SECRET)
    paths = _wazuh_paths(tmp_path)
    grokbot_packs.apply_setup(
        tmp_path,
        "wazuh-triage",
        bearer_env="TEST_GROKBOT_BEARER",
        **paths,
    )
    unit = render_unit(tmp_path)
    assert SECRET not in unit
    assert "--bearer-env" in unit
    assert str(paths["runtime_path"]) not in unit


def test_successful_canary_is_read_only_and_does_not_mutate_state(tmp_path: Path, monkeypatch):
    from brigade.grokbot_wazuh import lifecycle as lifecycle_mod
    from brigade.grokbot_wazuh.contracts import parse_ingest_input
    from brigade.grokbot_wazuh.normalize import normalize_alert
    from brigade.grokbot_wazuh.policy import classify
    from brigade.grokbot_wazuh.store import WazuhStore

    monkeypatch.setenv("TEST_GROKBOT_BEARER", SECRET)
    paths = _wazuh_paths(tmp_path)
    grokbot_packs.apply_setup(
        tmp_path,
        "wazuh-triage",
        bearer_env="TEST_GROKBOT_BEARER",
        **paths,
    )
    store = WazuhStore(str(paths["ledger_path"]))
    store.ready()
    alert = parse_ingest_input(
        {
            "alerts": [
                {
                    "rule_id": "504",
                    "rule_level": 12,
                    "rule_description": "Agent disconnected",
                    "rule_groups": ["agent_disconnected"],
                    "agent_id": "001",
                    "decoder": "agent-buffer",
                    "timestamp": "2026-08-28T16:00:00Z",
                    "detail": "keepalive stopped",
                }
            ]
        }
    )["alerts"][0]
    record = normalize_alert(alert)
    store.upsert_alert(record, classify(record), now=datetime(2026, 8, 28, 16, tzinfo=timezone.utc))
    before = paths["ledger_path"].read_bytes() if paths["ledger_path"].exists() else b""
    monkeypatch.setattr(
        grokbot_ops,
        "_request_json",
        lambda *_args, **_kwargs: {"ok": True, "service": "grokbot-wazuh-triage"},
    )
    monkeypatch.setattr(grokbot_ops, "_anonymous_health_status", lambda *_args, **_kwargs: 401)
    monkeypatch.setattr(grokbot_ops, "_tools_list", lambda *_args, **_kwargs: [{"name": name} for name in TOOLS])
    result = lifecycle_mod.canary(tmp_path)
    assert result["ok"] is True
    assert result["auth_rejected_without_bearer"] is True
    assert set(result["tools"]) == set(TOOLS)
    after = paths["ledger_path"].read_bytes() if paths["ledger_path"].exists() else b""
    assert after == before


def test_runtime_owner_read_uses_nofollow_descriptor_not_path_read_text(tmp_path: Path, monkeypatch):
    from brigade.grokbot_wazuh.lifecycle import RUNTIME_SCHEMA, _owner_from_runtime

    runtime = tmp_path / "runtime.json"
    owner = tmp_path / "owner-dir"
    owner.mkdir()
    runtime.write_text(
        json.dumps({"schema": RUNTIME_SCHEMA, "owner_path": str(owner.resolve())}),
        encoding="utf-8",
    )
    os.chmod(runtime, 0o600)
    original = Path.read_text

    def boom(self: Path, *args: object, **kwargs: object) -> str:
        if self.name == "runtime.json":
            raise AssertionError("TOCTOU Path.read_text")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", boom)
    resolved = _owner_from_runtime(str(runtime.resolve()), tmp_path)
    assert resolved == owner.resolve()
    real = tmp_path / "real-runtime.json"
    real.write_text("{}", encoding="utf-8")
    os.chmod(real, 0o600)
    linked = tmp_path / "linked-runtime.json"
    linked.symlink_to(real)
    with pytest.raises(WazuhError):
        _owner_from_runtime(str(linked), tmp_path)


def test_cli_pack_and_serve_choices_include_wazuh_triage():
    parser = cli._build_parser()
    shown = parser.parse_args(["run", "cloud", "grokbot", "pack", "show", "--id", "wazuh-triage", "--json"])
    assert shown.pack_id == "wazuh-triage"
    served = parser.parse_args(
        [
            "run",
            "cloud",
            "grokbot",
            "serve",
            "--pack",
            "wazuh-triage",
            "--bearer-env",
            "TEST_GROKBOT_BEARER",
        ]
    )
    assert served.pack == "wazuh-triage"
    assert not hasattr(served, "wazuh_token")
    assert "wazuh-triage" in grokbot_packs.STEWARD_PACK_IDS
