"""#1036 mutation tests: pre-created unbound runs-dir trees are not adopted.

A seat that plants ``.brigade/scanners/runs`` plus forged receipts must not
gain scheduling authority. The unfixed opener's OSError fallback called
``_open_legacy_scanner_runs_directory``, which bound the attacker inode and
every child run directory. Re-adding that fallback makes this module fail.

A released pre-0.27 workspace has the same unbound root without an authority
record. That clean operator-owned tree must bind and keep sweeping. A
foreign-uid, world-writable, or symlink tree must still fail closed.
"""

from __future__ import annotations

import inspect
import json
import os
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from brigade.work_cmd import helpers, ledger, scanners as scanners_mod


def _unfixed_open_scanner_runs_directory(target: Path, *, create: bool) -> int:
    """Replica of the pre-#1036 opener: adopt on any create-path OSError."""
    try:
        return ledger._open_verifier_owned_directory(
            target,
            components=(".brigade", "scanners", "runs"),
            anchor_name=".runs.authority.json",
            create=create,
        )
    except OSError:
        if not create:
            raise
        return ledger._open_legacy_scanner_runs_directory(target)


def _plant_unbound_forged_runs_tree(target: Path) -> None:
    """Create the #1036 attack tree: unbound root plus forged receipts."""
    runs = helpers._scanner_runs_root(target)
    running = runs / "forged-running"
    success = runs / "forged-success"
    running.mkdir(parents=True)
    success.mkdir(parents=True)
    (running / "receipt.json").write_text(
        json.dumps(
            {
                "run_id": "forged-running",
                "scanner_id": "gate",
                "status": "running",
                "started_at": "2099-01-01T00:00:00+00:00",
            }
        )
    )
    (success / "receipt.json").write_text(
        json.dumps(
            {
                "run_id": "forged-success",
                "scanner_id": "gate",
                "status": "completed",
                "exit_code": 0,
                "started_at": "2099-01-01T00:00:00+00:00",
                "completed_at": "2099-01-01T00:00:01+00:00",
            }
        )
    )


def _plant_pre_027_unbound_clean_runs_tree(target: Path) -> None:
    """Released-Brigade layout: plain mkdir, no authority record, operator-owned."""
    helpers._scanner_runs_root(target).mkdir(parents=True)


def _write_due_scanner_config(target: Path, *, scanner_id: str = "gate") -> None:
    config = target / ".brigade" / "scanners.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        f"""
[[scanner]]
id = "{scanner_id}"
source = "{scanner_id}"
command = "{sys.executable} -c print(0)"
cadence = "daily@02:00"
enabled = true
timeout = 30
output_path = ".brigade/{scanner_id}.json"
conflict_window = "02:00-02:10"
"""
    )


def _runs_root_is_bound(target: Path) -> bool:
    _path, payload = ledger._read_external_directory_authority(target)
    directories = payload.get("directories") if isinstance(payload, dict) else None
    return isinstance(directories, dict) and ".brigade/scanners/runs" in directories


def _child_run_is_bound(target: Path, run_id: str) -> bool:
    _path, payload = ledger._read_external_directory_authority(target)
    directories = payload.get("directories") if isinstance(payload, dict) else None
    return isinstance(directories, dict) and f".brigade/scanners/runs/{run_id}" in directories


def test_mutation_open_scanner_runs_directory_does_not_call_legacy_adoption() -> None:
    source = inspect.getsource(scanners_mod._open_scanner_runs_directory)
    assert "_open_legacy_scanner_runs_directory" not in source
    assert "except OSError" not in source
    helper = inspect.getsource(scanners_mod._bind_released_unbound_scanner_runs_root)
    assert "_open_legacy_scanner_runs_directory" not in helper
    assert "_adopt_preexisting_scanner_run_directories" not in helper
    assert "descriptor-relative directory authority operations are unavailable" not in helper
    assert "os.name != " not in helper


def test_mutation_precreated_unbound_tree_is_adopted_by_unfixed_fallback_and_refused_by_fix(
    tmp_path: Path,
) -> None:
    mutant = tmp_path / "mutant"
    fixed = tmp_path / "fixed"
    mutant.mkdir()
    fixed.mkdir()
    _plant_unbound_forged_runs_tree(mutant)
    _plant_unbound_forged_runs_tree(fixed)

    descriptor = _unfixed_open_scanner_runs_directory(mutant, create=True)
    os.close(descriptor)
    assert _runs_root_is_bound(mutant)
    assert _child_run_is_bound(mutant, "forged-running")
    assert _child_run_is_bound(mutant, "forged-success")
    assert (mutant / ".brigade" / ".runs.authority.json").is_file()
    now = datetime(2026, 8, 20, 12, 0, 0, tzinfo=timezone.utc)

    descriptor = scanners_mod._open_scanner_runs_directory(fixed, create=True)
    os.close(descriptor)
    assert _runs_root_is_bound(fixed)
    assert not _child_run_is_bound(fixed, "forged-running")
    assert not _child_run_is_bound(fixed, "forged-success")
    assert scanners_mod._scanner_receipts(fixed) == []
    assert scanners_mod._scanner_running_receipts(fixed) == []
    assert scanners_mod._scanner_latest_success(fixed, "gate") is None
    assert scanners_mod._scanner_is_due(fixed, {"id": "gate", "cadence": "daily@02:00"}, now=now) is True


def test_pre_027_unbound_clean_workspace_upgrades_and_sweeps(tmp_path: Path) -> None:
    """(a) Released unbound-but-clean root binds and a due sweep succeeds.

    Red if the opener still refuses every pre-existing unbound root.
    """
    _plant_pre_027_unbound_clean_runs_tree(tmp_path)
    _write_due_scanner_config(tmp_path)
    assert not _runs_root_is_bound(tmp_path)

    assert scanners_mod._bind_released_unbound_scanner_runs_root(tmp_path) == "repaired"
    assert _runs_root_is_bound(tmp_path)

    payload, rc = scanners_mod._scanners_run_payload(target=tmp_path, scanner_id="gate", force=True)
    assert rc == 0, payload
    assert payload.get("errors") in (None, [])
    assert payload["completed"] == 1
    assert payload["failed"] == 0
    run = payload["runs"][0]
    assert run.get("status") == "completed"
    assert run.get("runs_directory_error") is not True
    assert _child_run_is_bound(tmp_path, str(run["run_id"]))


def test_foreign_or_forged_unbound_tree_is_still_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """(b) Foreign-uid and symlink trees stay fail-closed; forged receipts stay inert.

    Red if the unfixed fallback is restored and those receipts become authority.
    """
    foreign = tmp_path / "foreign"
    linked = tmp_path / "linked"
    writable = tmp_path / "writable"
    forged = tmp_path / "forged"
    for workspace in (foreign, linked, writable, forged):
        workspace.mkdir()

    _plant_pre_027_unbound_clean_runs_tree(foreign)
    real_geteuid = os.geteuid
    monkeypatch.setattr(scanners_mod.os, "geteuid", lambda: real_geteuid() + 1)
    with pytest.raises(OSError, match="foreign uid"):
        scanners_mod._open_scanner_runs_directory(foreign, create=True)
    monkeypatch.undo()
    assert not _runs_root_is_bound(foreign)

    attacker = linked / "attacker-runs"
    attacker.mkdir()
    parent = helpers._scanner_runs_root(linked).parent
    parent.mkdir(parents=True)
    (parent / "runs").symlink_to(attacker)
    with pytest.raises(OSError, match="symlink"):
        scanners_mod._open_scanner_runs_directory(linked, create=True)
    assert not _runs_root_is_bound(linked)

    _plant_pre_027_unbound_clean_runs_tree(writable)
    helpers._scanner_runs_root(writable).chmod(0o777)
    with pytest.raises(OSError, match="world-writable"):
        scanners_mod._open_scanner_runs_directory(writable, create=True)
    assert not _runs_root_is_bound(writable)
    helpers._scanner_runs_root(writable).chmod(0o700)

    _plant_unbound_forged_runs_tree(forged)
    receipt = scanners_mod._scanner_run_one(
        forged, {"id": "gate", "source": "gate", "command": f"{sys.executable} -c print(0)", "timeout": 5}
    )
    assert receipt.get("status") == "completed"
    assert receipt.get("runs_directory_error") is not True
    assert scanners_mod._scanner_running_receipts(forged) == []
    assert (
        scanners_mod._scanner_latest_success(forged, "gate") is None
        or scanners_mod._scanner_latest_success(forged, "gate").get("run_id") != "forged-success"
    )
    assert not _child_run_is_bound(forged, "forged-running")
    assert not _child_run_is_bound(forged, "forged-success")


def test_scanner_run_one_surfaces_runs_dir_failure_without_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _plant_pre_027_unbound_clean_runs_tree(tmp_path)
    real_geteuid = os.geteuid
    monkeypatch.setattr(scanners_mod.os, "geteuid", lambda: real_geteuid() + 1)
    receipt = scanners_mod._scanner_run_one(tmp_path, {"id": "gate", "source": "gate", "command": "", "timeout": 1})
    assert receipt["status"] == "failed"
    assert receipt["runs_directory_error"] is True
    assert "foreign uid" in str(receipt["error"])
    assert "Traceback" not in str(receipt["error"])

    _write_due_scanner_config(tmp_path)
    payload, rc = scanners_mod._scanners_run_payload(target=tmp_path, scanner_id="gate", force=True)
    assert rc == 1
    assert payload["errors"]
    assert "foreign uid" in payload["errors"][0]


def test_bind_and_init_are_noop_when_runs_root_is_absent(tmp_path: Path) -> None:
    """Fresh operator quickstart has no runs dir; init must stay exit 0."""
    assert scanners_mod._bind_released_unbound_scanner_runs_root(tmp_path) == "missing"
    assert scanners_mod.scanners_init(target=tmp_path) == 0
    assert not helpers._scanner_runs_root(tmp_path).exists()
    assert not _runs_root_is_bound(tmp_path)


def test_bind_is_noop_when_dirfd_is_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows / no-dirfd hosts must not fail scanners_init on a fresh tree.

    Red if bind still raises ``descriptor-relative ... unavailable`` (the
    pre-fix ``os.name != "posix"`` path) before noticing the runs root is
    absent. That exit 1 broke windows-native-acceptance ``operator quickstart``.
    """
    monkeypatch.setattr(ledger, "_dirfd_available", lambda: False)
    assert scanners_mod._bind_released_unbound_scanner_runs_root(tmp_path) == "missing"
    assert scanners_mod.scanners_init(target=tmp_path) == 0
    assert not helpers._scanner_runs_root(tmp_path).exists()
    assert not _runs_root_is_bound(tmp_path)


def test_doctor_and_init_repair_released_unbound_runs_root(tmp_path: Path) -> None:
    _plant_pre_027_unbound_clean_runs_tree(tmp_path)
    _write_due_scanner_config(tmp_path)
    health = scanners_mod._scanner_health(tmp_path)
    check = next(item for item in health["checks"] if item.get("name") == "scanner_runs_root")
    assert check["status"] == "ok"
    assert "bound" in str(check["detail"])
    assert _runs_root_is_bound(tmp_path)

    other = tmp_path / "other"
    other.mkdir()
    _plant_pre_027_unbound_clean_runs_tree(other)
    assert scanners_mod.scanners_init(target=other) == 0
    assert _runs_root_is_bound(other)


def test_forged_receipt_in_verifier_created_run_dir_is_rejected(tmp_path: Path) -> None:
    root = scanners_mod._open_scanner_runs_directory(tmp_path, create=True)
    run_id = "verifier-run"
    os.mkdir(run_id, 0o700, dir_fd=root)
    run = os.open(run_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
    try:
        ledger._record_verifier_owned_directory(
            tmp_path,
            components=(".brigade", "scanners", "runs", run_id),
            directory=run,
        )
    finally:
        os.close(run)
        os.close(root)
    (helpers._scanner_runs_root(tmp_path) / run_id / "receipt.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "scanner_id": "gate",
                "status": "running",
                "started_at": "2099-01-01T00:00:00+00:00",
            }
        )
    )
    assert scanners_mod._scanner_receipts(tmp_path) == []
    assert scanners_mod._scanner_running_receipts(tmp_path) == []


def test_world_writable_parent_is_a_red_flag(tmp_path: Path) -> None:
    scanners = helpers._scanner_runs_root(tmp_path).parent
    scanners.mkdir(parents=True)
    scanners.chmod(scanners.stat().st_mode | stat.S_IWOTH)
    helpers._scanner_runs_root(tmp_path).mkdir()
    with pytest.raises(OSError, match="world-writable"):
        scanners_mod._bind_released_unbound_scanner_runs_root(tmp_path)
    assert not _runs_root_is_bound(tmp_path)
