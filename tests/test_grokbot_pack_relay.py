# content-guard: allow bearer-token file
"""First-party steward ledger relay through generic findings."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from brigade import fleet_client, grokbot_findings, grokbot_packs
from brigade.grokbot_fleet.ledger import FleetLedger

NOW = datetime(2026, 8, 30, 9, 0, tzinfo=timezone.utc)
PRIVATE_FLEET = "PRIVATE_FLEET_REASON_TOKEN"
RESULT_KEYS = {
    "apply",
    "eligible",
    "known",
    "created",
    "skipped",
    "pending",
    "reported",
    "limit",
    "relays",
}
HEX_64 = __import__("re").compile(r"^[0-9a-f]{64}$")


def _owner(path: Path) -> Path:
    path.mkdir(mode=0o700)
    os.chmod(path, 0o700)
    return path


def _steward_paths(root: Path, ledger_name: str) -> dict[str, Path]:
    runtime = root / "runtime.json"
    ledger = root / "ledger" / ledger_name
    actions = root / "actions"
    approvals = root / "approvals"
    root.mkdir()
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


def _setup_stewards(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    monkeypatch.setenv("TEST_GROKBOT_BEARER", "not-a-real-token-value-32chars!!")
    target = tmp_path / "queue"
    target.mkdir()
    owner = _owner(tmp_path / "owner")
    fleet_paths = _steward_paths(tmp_path / "fleet-state", "ledger.json")
    instance = grokbot_packs.instance_config_path(target, "fleet-steward")
    instance.parent.mkdir(parents=True)
    instance.write_text(
        json.dumps(
            {
                "schema": grokbot_packs.INSTANCE_SCHEMA,
                "pack_id": "fleet-steward",
                "pack_version": "1.0.0",
                "bind": "127.0.0.1:8771",
                "allowed_hosts": [],
                "allowed_origins": [],
                "public_route": "",
                "bearer": {"kind": "env", "name": "TEST_GROKBOT_BEARER"},
                **{key: str(value) for key, value in fleet_paths.items()},
            }
        )
    )
    instance.chmod(0o600)
    FleetLedger(str(fleet_paths["ledger_path"])).ready()
    return target, owner, fleet_paths["ledger_path"]


def _fleet_finding(finding_id: str = "control-plane:unreachable") -> dict[str, object]:
    return {
        "finding_id": finding_id,
        "target_alias": finding_id.split(":", 1)[0],
        "proposed_action_id": "inspect-host",
        "reason": PRIVATE_FLEET,
        "blast_radius": "one registered host",
        "verification_id": "verify-host-reachability",
        "rollback_id": "no-rollback",
        "observed_at": "2026-08-30T09:00:00Z",
    }


def _assert_public(result: object) -> None:
    assert isinstance(result, dict)
    assert set(result) == RESULT_KEYS
    dumped = json.dumps(result)
    assert PRIVATE_FLEET not in dumped
    assert "schema" not in dumped
    assert "/fleet-state" not in dumped
    assert "TEST_GROKBOT_BEARER" not in dumped
    for relay_id in result["relays"]:
        assert isinstance(relay_id, str)
        assert HEX_64.fullmatch(relay_id)


def test_collect_manifests_adapts_steward_ledgers_in_memory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from brigade import grokbot_pack_relay

    target, _owner, fleet_path = _setup_stewards(tmp_path, monkeypatch)
    FleetLedger(str(fleet_path)).replace_findings("control-plane", [_fleet_finding()])

    manifests = grokbot_pack_relay.collect_manifests(target)

    assert [item["schema"] for item in manifests] == [
        grokbot_findings.FINDINGS_SCHEMA,
    ]
    producers = [[entry["producer"] for entry in item["entries"]] for item in manifests]
    assert producers == [["fleet"]]
    for manifest in manifests:
        for entry in manifest["entries"]:
            assert set(entry) == grokbot_findings.REQUIRED_ENTRY_KEYS
    assert not list(target.rglob("*findings*.json"))


def test_preview_relay_counts_without_writing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from brigade import grokbot_pack_relay

    target, owner, fleet_path = _setup_stewards(tmp_path, monkeypatch)
    FleetLedger(str(fleet_path)).replace_findings("control-plane", [_fleet_finding()])
    before = {path: path.stat().st_mtime_ns for path in target.rglob("*") if path.is_file()}

    result = grokbot_pack_relay.preview_relay(target, owner, limit=50)

    assert result["apply"] is False
    assert result["eligible"] == 1
    assert result["known"] == 0
    assert result["created"] == 0
    assert result["limit"] == 50
    _assert_public(result)
    assert not (owner / "memory" / "handoff-inbox").exists()
    after = {path: path.stat().st_mtime_ns for path in target.rglob("*") if path.is_file()}
    assert after == before


def test_known_finding_does_not_starve_new_fleet_creation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from brigade import grokbot_pack_relay

    monkeypatch.setattr(fleet_client, "report_event", lambda *_args, **_kwargs: True)
    target, owner, fleet_path = _setup_stewards(tmp_path, monkeypatch)
    FleetLedger(str(fleet_path)).replace_findings("control-plane", [_fleet_finding()])
    first = grokbot_pack_relay.apply_relay(target, owner, limit=1, now=NOW)
    assert first["created"] == 1
    FleetLedger(str(fleet_path)).replace_findings(
        "control-plane", [_fleet_finding(), _fleet_finding("control-plane:degraded")]
    )

    result = grokbot_pack_relay.apply_relay(target, owner, limit=1, now=NOW)

    assert result["apply"] is True
    assert result["known"] == 1
    assert result["created"] == 1
    assert result["eligible"] == 1
    _assert_public(result)
    drafts = list((owner / "memory" / "handoff-inbox").glob("*.md"))
    assert len(drafts) == 2
    bodies = "\n".join(path.read_text(encoding="utf-8") for path in drafts)
    assert PRIVATE_FLEET in bodies


def test_invalid_limit_is_rejected_before_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from brigade import grokbot_pack_relay

    target, owner, _fleet_path = _setup_stewards(tmp_path, monkeypatch)
    owner_before = list(owner.rglob("*"))

    with pytest.raises(grokbot_pack_relay.PackRelayError) as caught:
        grokbot_pack_relay.apply_relay(target, owner, limit=0, now=NOW)

    assert caught.value.reason == "invalid-limit"
    assert list(owner.rglob("*")) == owner_before
    assert not (target / ".brigade" / "cloud" / "grokbot" / "outbox").exists()


def test_relay_setup_writes_owner_only_config_and_hides_paths(tmp_path: Path):
    from brigade import grokbot_pack_relay

    target = tmp_path / "queue"
    target.mkdir()
    owner = _owner(tmp_path / "owner")
    preview = grokbot_pack_relay.preview_relay_setup(target, owner)
    assert preview["apply"] is False
    assert str(owner) not in json.dumps(preview)
    assert not (target / ".brigade" / "grokbot" / "relay.json").exists()
    applied = grokbot_pack_relay.apply_relay_setup(target, owner)
    assert applied["apply"] is True
    config_path = target / ".brigade" / "grokbot" / "relay.json"
    assert config_path.is_file()
    assert config_path.stat().st_mode & 0o777 == 0o600
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    assert set(payload) == {"schema", "owner_workspace"}
    assert payload["schema"] == "brigade.grokbot.pack-relay.v1"
    loaded = grokbot_pack_relay.load_relay_config(target)
    assert loaded["owner_workspace"] == str(owner.resolve())
    checks = grokbot_pack_relay.relay_doctor(target)
    assert all(check["status"] == "ok" for check in checks)
    assert str(owner) not in json.dumps(checks)


def test_relay_setup_rejects_insecure_owner_and_config_symlink(tmp_path: Path):
    from brigade import grokbot_pack_relay

    target = tmp_path / "queue"
    target.mkdir()
    owner = tmp_path / "owner"
    owner.mkdir(mode=0o755)
    os.chmod(owner, 0o755)
    with pytest.raises(grokbot_pack_relay.PackRelayError) as caught:
        grokbot_pack_relay.apply_relay_setup(target, owner)
    assert caught.value.reason == "invalid-owner"
    assert not (target / ".brigade" / "grokbot" / "relay.json").exists()
    safe_owner = _owner(tmp_path / "safe-owner")
    grokbot_pack_relay.apply_relay_setup(target, safe_owner)
    config_path = target / ".brigade" / "grokbot" / "relay.json"
    real = tmp_path / "outside.json"
    real.write_text(config_path.read_text(encoding="utf-8"), encoding="utf-8")
    os.chmod(real, 0o600)
    config_path.unlink()
    config_path.symlink_to(real)
    with pytest.raises(grokbot_pack_relay.PackRelayError) as linked:
        grokbot_pack_relay.load_relay_config(target)
    assert linked.value.reason == "unsafe-config"


def test_render_relay_units_are_oneshot_timer_and_hide_owner_text(tmp_path: Path):
    from brigade import grokbot_pack_relay

    target = tmp_path / "queue"
    target.mkdir()
    owner = _owner(tmp_path / "owner")
    grokbot_pack_relay.apply_relay_setup(target, owner)
    units = grokbot_pack_relay.render_relay_units(target)
    service = units["brigade-grokbot-findings-relay.service"]
    timer = units["brigade-grokbot-findings-relay.timer"]
    assert "Type=oneshot" in service
    assert "--apply --limit 50" in service or ("--apply" in service and "--limit" in service and "50" in service)
    assert "UMask=0077" in service
    assert "NoNewPrivileges=yes" in service
    assert "PrivateTmp=yes" in service
    assert "ProtectSystem=strict" in service
    assert "ProtectHome=read-only" in service
    assert "TimeoutStartSec=120" in service
    assert "ReadWritePaths=" in service
    assert "OnBootSec=2min" in timer
    assert "OnUnitActiveSec=5min" in timer
    assert "RandomizedDelaySec=30s" in timer
    assert "AccuracySec=30s" in timer
    assert "Persistent=false" in timer
    assert PRIVATE_FLEET not in service + timer
    out = tmp_path / "units"
    out.mkdir()
    written = grokbot_pack_relay.write_relay_units(target, out)
    assert {path.name for path in written} == {
        "brigade-grokbot-findings-relay.service",
        "brigade-grokbot-findings-relay.timer",
    }


def test_render_relay_units_preview_omits_private_python_executable(tmp_path: Path):
    from brigade import grokbot_pack_relay

    target = tmp_path / "queue"
    target.mkdir()
    owner = _owner(tmp_path / "owner")
    grokbot_pack_relay.apply_relay_setup(target, owner)
    python = tmp_path / "private-venv" / "bin" / "python3"
    units = grokbot_pack_relay.render_relay_units(target, python=str(python))
    preview = "".join(units.values())
    assert "brigade-grokbot-findings-relay.service" in units
    assert str(python) not in preview
    assert str(python.parent) not in preview


def test_relay_preview_hides_package_path_but_written_unit_keeps_it(tmp_path, monkeypatch):
    from brigade import grokbot_pack_relay, isolated_exec

    target = tmp_path / "queue"
    target.mkdir()
    owner = _owner(tmp_path / "owner")
    grokbot_pack_relay.apply_relay_setup(target, owner)
    installation = tmp_path / "private-install/lib/python3.12/site-packages/brigade"
    monkeypatch.setattr(isolated_exec, "trusted_cwd", lambda: installation)
    monkeypatch.setattr(grokbot_pack_relay, "trusted_cwd", lambda: installation)
    python = str(tmp_path / "private-install/bin/python")
    preview = "".join(grokbot_pack_relay.render_relay_units(target, python=python).values())
    assert str(installation) not in preview, "unit preview exposed the trusted installation directory"
    output = tmp_path / "units"
    output.mkdir()
    paths = grokbot_pack_relay.write_relay_units(target, output, python=python)
    service = next(path.read_text() for path in paths if path.suffix == ".service")
    assert str(installation / "__init__.py") in service
    assert f"WorkingDirectory={installation}" in service


def test_cli_pack_relay_commands_are_preview_first_and_omit_id(tmp_path: Path, capsys, monkeypatch):
    from brigade import cli

    target = tmp_path / "queue"
    target.mkdir()
    owner = _owner(tmp_path / "owner")
    argv = [
        "run",
        "cloud",
        "grokbot",
        "pack",
        "relay-setup",
        "--target",
        str(target),
        "--owner",
        str(owner),
        "--json",
    ]
    assert cli.main(argv) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["apply"] is False
    assert str(owner) not in json.dumps(preview)
    assert cli.main([*argv, "--apply"]) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["apply"] is True
    assert cli.main(["run", "cloud", "grokbot", "pack", "relay-doctor", "--target", str(target)]) == 0
    doctor_out = capsys.readouterr()
    assert str(owner) not in doctor_out.out + doctor_out.err
    assert cli.main(["run", "cloud", "grokbot", "pack", "relay", "--target", str(target), "--json"]) == 0
    relay_preview = json.loads(capsys.readouterr().out)
    _assert_public(relay_preview)
    assert relay_preview["apply"] is False
    assert (
        cli.main(["run", "cloud", "grokbot", "pack", "relay", "--target", str(target), "--apply", "--limit", "0"]) == 2
    )
    err = capsys.readouterr().err
    assert "invalid-limit" in err
    assert cli.main(["run", "cloud", "grokbot", "pack", "install-relay-service", "--target", str(target)]) == 0
    unit_text = capsys.readouterr().out
    assert "brigade-grokbot-findings-relay.service" in unit_text
    assert "OnBootSec=2min" in unit_text
    wrappers = grokbot_packs.preview_relay_setup(target, owner)
    assert wrappers["apply"] is False


def test_cli_install_relay_service_preview_omits_absolute_paths(tmp_path: Path, capsys, monkeypatch):
    from brigade import cli, grokbot_pack_relay

    monkeypatch.setenv("TEST_GROKBOT_BEARER", "not-a-real-token-value-32chars!!")
    target = tmp_path / "queue"
    target.mkdir()
    owner = _owner(tmp_path / "owner")
    grokbot_pack_relay.apply_relay_setup(target, owner)
    forbidden = (
        str(target.resolve()),
        str(owner.resolve()),
    )

    assert cli.main(["run", "cloud", "grokbot", "pack", "install-relay-service", "--target", str(target)]) == 0
    preview = capsys.readouterr()
    leaked = preview.out + preview.err
    assert "brigade-grokbot-findings-relay.service" in preview.out
    assert "brigade-grokbot-findings-relay.timer" in preview.out
    for path in forbidden:
        assert path not in leaked

    out = tmp_path / "units"
    out.mkdir()
    assert (
        cli.main(
            [
                "run",
                "cloud",
                "grokbot",
                "pack",
                "install-relay-service",
                "--target",
                str(target),
                "--out",
                str(out),
            ]
        )
        == 0
    )
    apply_out = capsys.readouterr()
    apply_leaked = apply_out.out + apply_out.err
    for path in forbidden:
        assert path not in apply_leaked
    written = (out / "brigade-grokbot-findings-relay.service").read_text(encoding="utf-8")
    assert str(target.resolve()) in written
    assert str(owner.resolve()) in written


def test_write_relay_units_keeps_both_units_unchanged_when_second_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from brigade import grokbot_ops, grokbot_pack_relay

    target = tmp_path / "queue"
    target.mkdir()
    owner = _owner(tmp_path / "owner")
    grokbot_pack_relay.apply_relay_setup(target, owner)
    out = tmp_path / "units"
    out.mkdir()
    timer = out / grokbot_pack_relay.RELAY_TIMER_UNIT
    original_timer = "# pre-existing timer\n"
    timer.write_text(original_timer, encoding="utf-8")
    service = out / grokbot_pack_relay.RELAY_SERVICE_UNIT
    writes = {"count": 0}
    real_write = grokbot_ops._write_text_nofollow_atomic

    def fail_second(path: Path, data: str, **kwargs: object) -> None:
        writes["count"] += 1
        if writes["count"] >= 2:
            raise OSError("simulated second-write failure")
        real_write(path, data, **kwargs)

    monkeypatch.setattr(grokbot_ops, "_write_text_nofollow_atomic", fail_second)

    with pytest.raises(grokbot_pack_relay.PackRelayError) as caught:
        grokbot_pack_relay.write_relay_units(target, out, force=True)

    assert caught.value.reason == "unsafe-path"
    assert not service.exists()
    assert timer.read_text(encoding="utf-8") == original_timer


def test_write_relay_units_rejects_service_symlink_with_force_before_either_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from brigade import grokbot_ops, grokbot_pack_relay

    target = tmp_path / "queue"
    target.mkdir()
    owner = _owner(tmp_path / "owner")
    grokbot_pack_relay.apply_relay_setup(target, owner)
    out = tmp_path / "units"
    out.mkdir()
    victim = tmp_path / "outside.service"
    original_target = "# keep the symlink target\n"
    victim.write_text(original_target, encoding="utf-8")
    service = out / grokbot_pack_relay.RELAY_SERVICE_UNIT
    service.symlink_to(victim)
    timer = out / grokbot_pack_relay.RELAY_TIMER_UNIT
    original_timer = "# pre-existing timer\n"
    timer.write_text(original_timer, encoding="utf-8")
    writes = {"count": 0}
    real_write = grokbot_ops._write_text_nofollow_atomic

    def fail_second(path: Path, data: str, **kwargs: object) -> None:
        writes["count"] += 1
        if writes["count"] >= 2:
            raise OSError("simulated second-write failure")
        real_write(path, data, **kwargs)

    monkeypatch.setattr(grokbot_ops, "_write_text_nofollow_atomic", fail_second)

    with pytest.raises(grokbot_pack_relay.PackRelayError) as caught:
        grokbot_pack_relay.write_relay_units(target, out, force=True)

    assert caught.value.reason == "unsafe-path"
    assert writes["count"] == 0
    assert service.is_symlink()
    assert service.readlink() == victim
    assert victim.read_text(encoding="utf-8") == original_target
    assert timer.read_text(encoding="utf-8") == original_timer


def test_relay_refuses_retained_fleet_overlapping_paths(tmp_path, monkeypatch):
    from brigade import grokbot_pack_relay

    target, owner, _ledger = _setup_stewards(tmp_path, monkeypatch)
    instance = grokbot_packs.instance_config_path(target, "fleet-steward")
    payload = json.loads(instance.read_text())
    payload["ledger_path"] = payload["runtime_path"]
    instance.write_text(json.dumps(payload))
    with pytest.raises(grokbot_pack_relay.PackRelayError) as caught:
        grokbot_pack_relay.preview_relay(target, owner)
    assert caught.value.reason == "unsafe-path"
