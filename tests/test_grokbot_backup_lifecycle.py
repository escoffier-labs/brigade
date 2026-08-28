"""Backup Steward lifecycle, canary, and path-safety tests."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from brigade import grokbot_ops, grokbot_packs
from brigade.grokbot_backup.contracts import TOOLS, BackupError
from brigade.grokbot_backup.ledger import BackupLedger
from brigade.grokbot_backup.lifecycle import (
    BackupListenerConfig,
    _invalid_backup_tool_request,
    build_tools_from_config,
    render_unit,
    validate_disjoint_state_paths,
    validate_runtime_file,
    validate_state_directory,
)
from tests.test_grokbot_backup_runtime import _runtime

SECRET = "not-a-real-token-value-32chars!!"


def _backup_paths(tmp_path: Path) -> dict[str, Path]:
    runtime = tmp_path / "runtime.json"
    ledger = tmp_path / "ledger" / "ledger.jsonl"
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
    with pytest.raises(BackupError) as caught:
        validate_disjoint_state_paths("/var/lib/a", "/var/lib/a/ledger.json", "/var/lib/b", "/var/lib/c")
    assert caught.value.code == "invalid_request"
    assert "/var/lib/a" not in str(caught.value)
    with pytest.raises(BackupError):
        validate_disjoint_state_paths("/var/lib/../a", "/var/lib/b", "/var/lib/c", "/var/lib/d")
    with pytest.raises(BackupError):
        validate_disjoint_state_paths("var/lib/a", "/var/lib/b", "/var/lib/c", "/var/lib/d")


def test_pack_setup_doctor_canary_and_unit_hide_secrets(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_GROKBOT_BEARER", SECRET)
    paths = _backup_paths(tmp_path)
    preview = grokbot_packs.preview_setup(
        tmp_path,
        "backup-steward",
        bearer_env="TEST_GROKBOT_BEARER",
        **paths,
    )
    assert preview["apply"] is False
    assert not grokbot_packs.instance_config_path(tmp_path, "backup-steward").exists()
    assert str(paths["runtime_path"]) not in json.dumps(preview)
    grokbot_packs.apply_setup(
        tmp_path,
        "backup-steward",
        bearer_env="TEST_GROKBOT_BEARER",
        **paths,
    )
    checks = grokbot_packs.doctor(tmp_path, "backup-steward")
    canary = grokbot_packs.canary(tmp_path, "backup-steward")
    unit = grokbot_packs.render_install_service(tmp_path, "backup-steward")
    sanitized = json.dumps({"checks": checks, "canary": canary})
    assert SECRET not in sanitized
    assert str(paths["runtime_path"]) not in sanitized
    assert SECRET not in unit
    assert str(paths["runtime_path"]) not in unit
    assert "--pack" in unit
    assert "backup-steward" in unit
    assert "127.0.0.1:8772" in unit
    assert "NoNewPrivileges=yes" in unit
    assert "KillMode=mixed" in unit
    assert "StandardOutput=journal" in unit
    assert "ProtectSystem=strict" in unit
    assert canary["ok"] is False
    assert all(set(check) == {"check", "status"} for check in checks)
    unit_dir = tmp_path / "units"
    installed = grokbot_packs.apply_install_service(tmp_path, "backup-steward", out_dir=unit_dir)
    assert installed["unit"] == grokbot_ops.unit_name("backup-steward")
    grokbot_packs.apply_remove(tmp_path, "backup-steward", unit_dir=unit_dir)
    assert not grokbot_packs.instance_config_path(tmp_path, "backup-steward").exists()


def test_unit_rendering_does_not_resolve_secret_values(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_GROKBOT_BEARER", SECRET)
    paths = _backup_paths(tmp_path)
    grokbot_packs.apply_setup(
        tmp_path,
        "backup-steward",
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
    from brigade.grokbot_backup import lifecycle as lifecycle_mod

    monkeypatch.setenv("TEST_GROKBOT_BEARER", SECRET)
    paths = _backup_paths(tmp_path)
    grokbot_packs.apply_setup(
        tmp_path,
        "backup-steward",
        bearer_env="TEST_GROKBOT_BEARER",
        **paths,
    )
    ledger = BackupLedger(str(paths["ledger_path"]))
    finding = {
        "finding_id": "media-archive:stale-lock",
        "target_alias": "media-archive",
        "kind": "stale-lock",
        "severity_class": "warning",
        "summary": "A stale backup lock is present",
        "observed_at": "2026-08-28T00:00:00Z",
        "receipt_ref": "receipt-1",
        "proposed_action_id": "run-backup",
        "blast_radius": "one registered restic target",
        "verification_statement": "compare the next snapshot receipt",
        "recovery_statement": "operator reruns the approved backup",
    }
    ledger.replace_findings("media-archive", [finding])
    seeded = Path(paths["action_state_path"]) / "proposals" / "seed.json"
    seeded.parent.mkdir(mode=0o700)
    os.chmod(seeded.parent, 0o700)
    seeded.write_text('{"seed":true}', encoding="utf-8")
    os.chmod(seeded, 0o600)

    def snapshot() -> tuple[bytes | None, dict[str, bytes | str]]:
        ledger_bytes = paths["ledger_path"].read_bytes() if paths["ledger_path"].exists() else None
        tree: dict[str, bytes | str] = {}
        root = Path(paths["action_state_path"])
        for item in sorted(root.rglob("*")):
            relative = str(item.relative_to(root))
            tree[relative] = item.read_bytes() if item.is_file() else "dir"
        return ledger_bytes, tree

    before = snapshot()
    monkeypatch.setattr(
        grokbot_ops,
        "_request_json",
        lambda *_args, **_kwargs: {"ok": True, "service": "grokbot-backup-steward"},
    )
    monkeypatch.setattr(grokbot_ops, "_anonymous_health_status", lambda *_args, **_kwargs: 401)
    monkeypatch.setattr(grokbot_ops, "_tools_list", lambda *_args, **_kwargs: [{"name": name} for name in TOOLS])
    result = lifecycle_mod.canary(tmp_path)
    assert result["ok"] is True
    assert result["auth_rejected_without_bearer"] is True
    assert set(result["tools"]) == set(TOOLS)
    assert snapshot() == before


def test_jsonrpc_batches_and_unknown_tools_are_rejected():
    assert _invalid_backup_tool_request(b"not-json") is False
    assert _invalid_backup_tool_request(b"[]") is True
    assert (
        _invalid_backup_tool_request(
            json.dumps({"method": "tools/call", "params": {"name": "hidden_tool", "arguments": {}}}).encode()
        )
        is True
    )
    assert (
        _invalid_backup_tool_request(
            json.dumps(
                {"method": "tools/call", "params": {"name": "backup_overview", "arguments": {"extra": 1}}}
            ).encode()
        )
        is True
    )
    assert (
        _invalid_backup_tool_request(
            json.dumps({"method": "tools/call", "params": {"name": "backup_overview", "arguments": {}}}).encode()
        )
        is False
    )


def test_runtime_and_state_paths_reject_symlinks(tmp_path: Path):
    real = tmp_path / "runtime.json"
    real.write_text("{}", encoding="utf-8")
    os.chmod(real, 0o600)
    link = tmp_path / "runtime.link"
    link.symlink_to(real)
    with pytest.raises(BackupError) as caught:
        validate_runtime_file(str(link))
    assert caught.value.code == "invalid_request"
    assert str(link) not in str(caught.value)
    directory = tmp_path / "actions"
    directory.mkdir(mode=0o700)
    os.chmod(directory, 0o700)
    linked_dir = tmp_path / "actions.link"
    linked_dir.symlink_to(directory)
    with pytest.raises(BackupError):
        validate_state_directory(str(linked_dir), must_exist=True)


def _tools_config(tmp_path: Path, payload: dict[str, object]) -> BackupListenerConfig:
    runtime = tmp_path / "runtime.json"
    runtime.write_text(json.dumps(payload), encoding="utf-8")
    os.chmod(runtime, 0o600)
    ledger = tmp_path / "ledger" / "ledger.jsonl"
    actions = tmp_path / "actions"
    approvals = tmp_path / "approvals"
    ledger.parent.mkdir(mode=0o700)
    actions.mkdir(mode=0o700)
    approvals.mkdir(mode=0o700)
    os.chmod(ledger.parent, 0o700)
    os.chmod(actions, 0o700)
    os.chmod(approvals, 0o700)
    return BackupListenerConfig(
        target=tmp_path,
        bind_host="127.0.0.1",
        bind_port=8772,
        allowed_hosts=(),
        allowed_origins=(),
        bearer="B" * 32,
        runtime_path=str(runtime),
        ledger_path=str(ledger),
        action_state_path=str(actions),
        approval_dir=str(approvals),
    )


def test_listener_uses_shared_configured_process_limiter(tmp_path: Path):
    payload = _runtime(tmp_path)
    payload["max_concurrent_processes"] = 1
    tools = build_tools_from_config(_tools_config(tmp_path, payload), env={})
    assert tools.observers.limiter is tools.executor.limiter
    assert tools.observers.limiter._max == 1
    held = threading.Event()
    release = threading.Event()
    finished = {"second": False}

    def occupy() -> None:
        tools.observers.limiter.run(lambda: (held.set(), release.wait(timeout=2)))

    first = threading.Thread(target=occupy)
    first.start()
    assert held.wait(timeout=2)

    def second() -> None:
        tools.executor.limiter.run(lambda: None)
        finished["second"] = True

    other = threading.Thread(target=second)
    other.start()
    other.join(timeout=0.2)
    assert finished["second"] is False
    release.set()
    other.join(timeout=2)
    first.join(timeout=2)
    assert finished["second"] is True


def test_listener_rejects_invalid_process_cap_at_startup(tmp_path: Path):
    payload = _runtime(tmp_path)
    payload["max_concurrent_processes"] = 0
    with pytest.raises(BackupError) as caught:
        build_tools_from_config(_tools_config(tmp_path, payload), env={})
    assert caught.value.code == "invalid_request"
