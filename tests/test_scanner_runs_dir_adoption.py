"""#1036 mutation tests: pre-created unbound runs-dir trees are not adopted.

A seat that plants ``.brigade/scanners/runs`` plus forged receipts must not
gain scheduling authority. The unfixed opener's OSError fallback called
``_open_legacy_scanner_runs_directory``, which bound the attacker inode and
every child run directory. Re-adding that fallback makes this module fail.
"""

from __future__ import annotations

import inspect
import json
import os
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


def _runs_root_is_bound(target: Path) -> bool:
    _path, payload = ledger._read_external_directory_authority(target)
    directories = payload.get("directories") if isinstance(payload, dict) else None
    return isinstance(directories, dict) and ".brigade/scanners/runs" in directories


def test_mutation_open_scanner_runs_directory_does_not_call_legacy_adoption() -> None:
    source = inspect.getsource(scanners_mod._open_scanner_runs_directory)
    assert "_open_legacy_scanner_runs_directory" not in source
    assert "except OSError" not in source


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
    assert (mutant / ".brigade" / ".runs.authority.json").is_file()
    running = scanners_mod._scanner_running_receipts(mutant)
    assert any(item.get("run_id") == "forged-running" for item in running)
    success = scanners_mod._scanner_latest_success(mutant, "gate")
    assert success is not None and success.get("run_id") == "forged-success"
    now = datetime(2026, 8, 20, 12, 0, 0, tzinfo=timezone.utc)
    assert scanners_mod._scanner_is_due(mutant, {"id": "gate", "cadence": "daily@02:00"}, now=now) is False

    with pytest.raises(OSError):
        scanners_mod._open_scanner_runs_directory(fixed, create=True)
    assert not _runs_root_is_bound(fixed)
    assert not (fixed / ".brigade" / ".runs.authority.json").is_file()
    assert scanners_mod._scanner_receipts(fixed) == []
    assert scanners_mod._scanner_running_receipts(fixed) == []
    assert scanners_mod._scanner_latest_success(fixed, "gate") is None
    assert scanners_mod._scanner_is_due(fixed, {"id": "gate", "cadence": "daily@02:00"}, now=now) is True
