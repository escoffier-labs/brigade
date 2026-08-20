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
    assert 'os.name != "posix"' not in helper
    assert "ledger_mod._dirfd_available" in helper
    assert "ledger_mod._open_directory_nofollow" in helper
    assert "ledger_mod._dirfd_open_dir" in helper
    open_run = inspect.getsource(scanners_mod._open_scanner_run_directory)
    assert "ledger_mod._dirfd_mkdir" in open_run
    assert "ledger_mod._dirfd_open_dir" in open_run


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


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="POSIX uid/gid red-flags are not evaluated on Windows")
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


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="POSIX uid/gid red-flags are not evaluated on Windows")
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


@pytest.mark.skipif(os.name != "posix" or not hasattr(os, "geteuid"), reason="world-writable mode bits are POSIX-only")
def test_world_writable_parent_is_a_red_flag(tmp_path: Path) -> None:
    scanners = helpers._scanner_runs_root(tmp_path).parent
    scanners.mkdir(parents=True)
    scanners.chmod(scanners.stat().st_mode | stat.S_IWOTH)
    helpers._scanner_runs_root(tmp_path).mkdir()
    with pytest.raises(OSError, match="world-writable"):
        scanners_mod._bind_released_unbound_scanner_runs_root(tmp_path)
    assert not _runs_root_is_bound(tmp_path)


def test_bind_returns_missing_when_dirfd_unavailable_and_runs_tree_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fresh operator quickstart must not raise when the platform has no dirfd."""
    monkeypatch.setattr(ledger, "_dirfd_available", lambda: False)
    monkeypatch.setattr(scanners_mod.ledger_mod, "_dirfd_available", lambda: False)
    assert scanners_mod._bind_released_unbound_scanner_runs_root(tmp_path) == "missing"
    assert not _runs_root_is_bound(tmp_path)
    assert scanners_mod.scanners_init(target=tmp_path, update_gitignore=False) == 0


def test_bind_does_not_adopt_existing_tree_when_dirfd_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1036 stays closed: no descriptor walk means no adoption, even off POSIX."""
    _plant_unbound_forged_runs_tree(tmp_path)
    monkeypatch.setattr(ledger, "_dirfd_available", lambda: False)
    monkeypatch.setattr(scanners_mod.ledger_mod, "_dirfd_available", lambda: False)
    with pytest.raises(OSError, match="descriptor-relative directory authority operations are unavailable"):
        scanners_mod._bind_released_unbound_scanner_runs_root(tmp_path)
    assert not _runs_root_is_bound(tmp_path)
    assert not _child_run_is_bound(tmp_path, "forged-running")
    assert not _child_run_is_bound(tmp_path, "forged-success")


def _install_posix_standin_for_nt_dirfd(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route ledger dirfd helpers through POSIX opens while claiming NT is available."""
    from brigade.work_cmd import nt_dirfd

    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)

    def open_root(path: Path, *, writable: bool = True) -> int:
        del writable
        return os.open(path, directory_flags)

    def open_child(parent: int, name: str, *, writable: bool = True) -> int:
        del writable
        return os.open(name, directory_flags, dir_fd=parent)

    def mkdir_child(parent: int, name: str) -> None:
        os.mkdir(name, 0o700, dir_fd=parent)

    def open_file(parent: int, name: str, flags: int, mode: int = 0o600) -> int:
        if flags & os.O_CREAT:
            return os.open(name, flags, mode, dir_fd=parent)
        return os.open(name, flags, dir_fd=parent)

    def replace_children(parent: int, source: str, destination: str) -> None:
        os.replace(source, destination, src_dir_fd=parent, dst_dir_fd=parent)

    def unlink_child(parent: int, name: str) -> None:
        os.unlink(name, dir_fd=parent)

    def stat_child(parent: int, name: str) -> os.stat_result:
        return os.stat(name, dir_fd=parent, follow_symlinks=False)

    monkeypatch.setattr(ledger, "_posix_dirfd_available", lambda: False)
    monkeypatch.setattr(scanners_mod.ledger_mod, "_posix_dirfd_available", lambda: False)
    monkeypatch.setattr(ledger, "_nt_dirfd_available", lambda: True)
    monkeypatch.setattr(scanners_mod.ledger_mod, "_nt_dirfd_available", lambda: True)
    monkeypatch.setattr(nt_dirfd, "open_root_directory", open_root)
    monkeypatch.setattr(nt_dirfd, "open_child_directory", open_child)
    monkeypatch.setattr(nt_dirfd, "mkdir_child", mkdir_child)
    monkeypatch.setattr(nt_dirfd, "open_file", open_file)
    monkeypatch.setattr(nt_dirfd, "replace_children", replace_children)
    monkeypatch.setattr(nt_dirfd, "unlink_child", unlink_child)
    monkeypatch.setattr(nt_dirfd, "stat_child", stat_child)


@pytest.mark.skipif(os.open not in os.supports_dir_fd, reason="stand-in NT helpers need POSIX dir_fd to execute here")
def test_bind_and_sweep_use_nt_dirfd_helpers_when_posix_dirfd_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows quickstart/sweep must bind through nt_dirfd, not a POSIX-only raise."""
    _install_posix_standin_for_nt_dirfd(monkeypatch)
    assert scanners_mod._bind_released_unbound_scanner_runs_root(tmp_path) == "missing"
    assert scanners_mod.scanners_init(target=tmp_path, update_gitignore=False) == 0

    released = tmp_path / "released"
    released.mkdir()
    _plant_pre_027_unbound_clean_runs_tree(released)
    _write_due_scanner_config(released)
    assert scanners_mod._bind_released_unbound_scanner_runs_root(released) == "repaired"
    assert _runs_root_is_bound(released)

    payload, rc = scanners_mod._scanners_run_payload(target=released, scanner_id="gate", force=True)
    assert rc == 0, payload
    assert payload["completed"] == 1
    assert payload["failed"] == 0
    run = payload["runs"][0]
    assert run.get("status") == "completed"
    assert run.get("runs_directory_error") is not True
    assert _child_run_is_bound(released, str(run["run_id"]))
    assert not _child_run_is_bound(released, "forged-running")


def test_scanner_path_red_flag_skips_posix_ownership_without_geteuid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helpers._scanner_runs_root(tmp_path).mkdir(parents=True)
    metadata = helpers._scanner_runs_root(tmp_path).stat()
    monkeypatch.delattr(scanners_mod.os, "geteuid", raising=False)
    assert scanners_mod._scanner_path_red_flag(metadata) is None
    file_path = tmp_path / "not-a-dir"
    file_path.write_text("x")
    assert scanners_mod._scanner_path_red_flag(file_path.stat()) == "not a directory"
