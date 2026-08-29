"""Pack lifecycle, diagnostics, canary, and unit-hardening tests."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from brigade.grokbot_fleet.contracts import FleetError
from brigade.grokbot_fleet.lifecycle import (
    render_unit,
    validate_disjoint_state_paths,
)
from brigade import grokbot_ops, grokbot_packs

SECRET = "not-a-real-token-value-32chars!!"


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


def test_disjoint_paths_reject_overlap_dots_and_relative():
    with pytest.raises(FleetError) as caught:
        validate_disjoint_state_paths("/var/lib/a", "/var/lib/a/ledger.json", "/var/lib/b", "/var/lib/c")
    assert caught.value.code == "invalid_request"
    assert "/var/lib/a" not in str(caught.value)
    with pytest.raises(FleetError):
        validate_disjoint_state_paths("/var/lib/../a", "/var/lib/b", "/var/lib/c", "/var/lib/d")
    with pytest.raises(FleetError):
        validate_disjoint_state_paths("var/lib/a", "/var/lib/b", "/var/lib/c", "/var/lib/d")


def test_pack_setup_doctor_canary_and_unit_hide_secrets(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_GROKBOT_BEARER", SECRET)
    paths = _fleet_paths(tmp_path)
    preview = grokbot_packs.preview_setup(
        tmp_path,
        "fleet-steward",
        bearer_env="TEST_GROKBOT_BEARER",
        **paths,
    )
    assert preview["apply"] is False
    assert not grokbot_packs.instance_config_path(tmp_path, "fleet-steward").exists()
    assert str(paths["runtime_path"]) not in json.dumps(preview)
    grokbot_packs.apply_setup(
        tmp_path,
        "fleet-steward",
        bearer_env="TEST_GROKBOT_BEARER",
        **paths,
    )
    checks = grokbot_packs.doctor(tmp_path, "fleet-steward")
    canary = grokbot_packs.canary(tmp_path, "fleet-steward")
    unit = grokbot_packs.render_install_service(tmp_path, "fleet-steward")
    sanitized = json.dumps({"checks": checks, "canary": canary})
    assert SECRET not in sanitized
    assert str(paths["runtime_path"]) not in sanitized
    assert SECRET not in unit
    assert str(paths["runtime_path"]) not in unit
    assert "--pack" in unit
    assert "fleet-steward" in unit
    assert "127.0.0.1:8771" in unit
    assert "NoNewPrivileges=yes" in unit
    assert "KillMode=mixed" in unit
    assert "StandardOutput=journal" in unit
    assert "ProtectSystem=strict" in unit
    assert canary["ok"] is False
    assert all(set(check) == {"check", "status"} for check in checks)
    unit_dir = tmp_path / "units"
    installed = grokbot_packs.apply_install_service(tmp_path, "fleet-steward", out_dir=unit_dir)
    assert installed["unit"] == grokbot_ops.unit_name("fleet-steward")
    grokbot_packs.apply_remove(tmp_path, "fleet-steward", unit_dir=unit_dir)
    assert not grokbot_packs.instance_config_path(tmp_path, "fleet-steward").exists()


def test_unit_rendering_does_not_resolve_secret_values(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_GROKBOT_BEARER", SECRET)
    paths = _fleet_paths(tmp_path)
    grokbot_packs.apply_setup(
        tmp_path,
        "fleet-steward",
        bearer_env="TEST_GROKBOT_BEARER",
        **paths,
    )
    unit = render_unit(tmp_path)
    assert SECRET not in unit
    assert "--bearer-env TEST_GROKBOT_BEARER" in unit or "--bearer-env" in unit
    read_write = unit.split("ReadWritePaths=", 1)[-1]
    assert str(paths["approval_dir"]) not in read_write
    assert str(paths["runtime_path"]) not in unit


def test_successful_canary_does_not_mutate_ledger_findings_or_action_state(tmp_path: Path, monkeypatch):
    from brigade.grokbot_fleet import lifecycle as lifecycle_mod
    from brigade.grokbot_fleet.contracts import TOOLS
    from brigade.grokbot_fleet.ledger import FleetLedger

    monkeypatch.setenv("TEST_GROKBOT_BEARER", SECRET)
    paths = _fleet_paths(tmp_path)
    grokbot_packs.apply_setup(
        tmp_path,
        "fleet-steward",
        bearer_env="TEST_GROKBOT_BEARER",
        **paths,
    )
    ledger = FleetLedger(str(paths["ledger_path"]))
    finding = {
        "finding_id": "control-plane:unreachable",
        "target_alias": "control-plane",
        "proposed_action_id": "inspect-host",
        "reason": "Host is unreachable",
        "blast_radius": "one registered host",
        "verification_id": "verify-host-reachability",
        "rollback_id": "no-rollback",
    }
    ledger.replace_findings("control-plane", [finding])
    seeded = Path(paths["action_state_path"]) / "proposals" / "seed.json"
    seeded.parent.mkdir(mode=0o700)
    os.chmod(seeded.parent, 0o700)
    seeded.write_text('{"seed":true}', encoding="utf-8")
    os.chmod(seeded, 0o600)

    def snapshot() -> tuple[bytes, object, dict[str, bytes | str]]:
        ledger_bytes = paths["ledger_path"].read_bytes()
        findings = json.loads(ledger_bytes)["findings"]
        tree: dict[str, bytes | str] = {}
        root = Path(paths["action_state_path"])
        for item in sorted(root.rglob("*")):
            relative = str(item.relative_to(root))
            tree[relative] = item.read_bytes() if item.is_file() else "dir"
        return ledger_bytes, findings, tree

    before = snapshot()

    monkeypatch.setattr(
        grokbot_ops,
        "_request_json",
        lambda *_args, **_kwargs: {"ok": True, "service": "grokbot-fleet-steward"},
    )
    monkeypatch.setattr(grokbot_ops, "_anonymous_health_status", lambda *_args, **_kwargs: 401)
    monkeypatch.setattr(grokbot_ops, "_tools_list", lambda *_args, **_kwargs: [{"name": name} for name in TOOLS])

    result = lifecycle_mod.canary(tmp_path)
    assert result["ok"] is True
    assert result["auth_rejected_without_bearer"] is True
    assert set(result["tools"]) == set(TOOLS)
    assert snapshot() == before
    assert "execute_remediation" not in result
    assert ledger.finding("control-plane:unreachable")["proposed_action_id"] == "inspect-host"


def test_runtime_and_state_paths_reject_symlinks(tmp_path: Path):
    from brigade.grokbot_fleet.lifecycle import validate_runtime_file, validate_state_directory

    real = tmp_path / "runtime.json"
    real.write_text("{}", encoding="utf-8")
    link = tmp_path / "runtime.link"
    link.symlink_to(real)
    with pytest.raises(FleetError) as caught:
        validate_runtime_file(str(link))
    assert caught.value.code == "invalid_request"
    assert str(link) not in str(caught.value)
    directory = tmp_path / "actions"
    directory.mkdir(mode=0o700)
    os.chmod(directory, 0o700)
    linked_dir = tmp_path / "actions.link"
    linked_dir.symlink_to(directory)
    with pytest.raises(FleetError):
        validate_state_directory(str(linked_dir), must_exist=True)
