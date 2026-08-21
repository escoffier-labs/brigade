"""#1066: legacy directory-authority records migrate; forged records still fail.

The pre-workspace store body is ``{schema_version, target, directories}``.
Pass-2 validation required ``workspace`` and treated a missing field as a
directory mismatch, stranding long-lived workspaces. Auto-upgrade is allowed
only when every identity field that IS present still matches. A swapped
directory, or a legacy-looking record whose extant inodes disagree, must
hard-fail. Re-collapsing that distinction into "rewrite live identities"
makes the forged case pass — this module fails if that happens.
"""

from __future__ import annotations

import inspect
import json
import os
import shutil
from pathlib import Path

import pytest

from brigade import doctor as doctor_mod
from brigade.work_cmd import ledger


_PROOF_COMPONENTS = (".brigade", "work", "imports", "proofs")
_PROOF_SCOPE = ".brigade/work/imports/proofs"


def _dir_identity(path: Path) -> dict[str, int]:
    info = path.stat()
    return {"device": info.st_dev, "inode": info.st_ino}


def _plant_legacy_unsigned_record(target: Path, directories: dict[str, dict[str, int]]) -> Path:
    """Write the pre-workspace body the first external-authority commits used."""
    record = {
        "schema_version": 1,
        "target": str(target.expanduser().resolve()),
        "directories": directories,
    }
    path = ledger._directory_authority_store_path(target)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(json.dumps(record, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    return path


def _ensure_proofs_dir(target: Path) -> Path:
    proofs = target / ".brigade" / "work" / "imports" / "proofs"
    proofs.mkdir(parents=True)
    return proofs


def _swap_proofs_directory(target: Path) -> Path:
    """Replace the proofs directory with a different inode.

    ``rmtree`` + ``mkdir`` often reuses the same inode on tmpfs; create the
    replacement first so the two directories coexist, then rename into place.
    """
    proofs = target / ".brigade" / "work" / "imports" / "proofs"
    replacement = proofs.with_name("proofs-swapped")
    replacement.mkdir()
    assert _dir_identity(replacement) != _dir_identity(proofs)
    retired = proofs.with_name("proofs-retired")
    proofs.rename(retired)
    replacement.rename(proofs)
    shutil.rmtree(retired)
    return proofs


def _unfixed_validate_collapses_legacy_and_mismatch(
    target: Path, components: tuple[str, ...], directory: int, *, workspace: dict[str, int]
) -> None:
    """Replica of a distinction-free validator: any body is rewritten to live ids.

    This is the mutation #1066 must remain sensitive to. Treating a genuine
    identity mismatch as "legacy, just upgrade" lets a swapped directory pass.
    """
    path, payload = ledger._read_external_directory_authority(target)
    if payload is None:
        payload = {
            "schema_version": 1,
            "target": str(target.expanduser().resolve()),
            "directories": {},
        }
    payload["workspace"] = workspace
    directories = payload.setdefault("directories", {})
    directories[ledger._directory_authority_scope(components)] = ledger._directory_identity(directory)
    ledger._write_external_directory_authority(path, payload)


def test_legacy_authority_record_migrates_and_open_succeeds(tmp_path: Path) -> None:
    proofs = _ensure_proofs_dir(tmp_path)
    store = _plant_legacy_unsigned_record(tmp_path, {_PROOF_SCOPE: _dir_identity(proofs)})
    assert "workspace" not in json.loads(store.read_text(encoding="utf-8"))

    descriptor = ledger._open_import_proof_directory(tmp_path, create=True)
    os.close(descriptor)

    path, payload = ledger._read_external_directory_authority(tmp_path)
    assert path == store
    assert payload is not None
    assert payload["workspace"] == ledger._workspace_directory_identity(tmp_path)
    assert payload["directories"][_PROOF_SCOPE] == _dir_identity(proofs)
    assert not ledger._is_legacy_external_directory_authority(payload)


def test_legacy_authority_record_allows_import_proof_publication(tmp_path: Path) -> None:
    proofs = _ensure_proofs_dir(tmp_path)
    _plant_legacy_unsigned_record(tmp_path, {_PROOF_SCOPE: _dir_identity(proofs)})
    item = ledger._make_import("legacy migrate import", kind="task", source="memory-care")

    imported, skipped, dismissed, rejected = ledger._append_import_records(tmp_path, [item])

    assert imported and not skipped and not dismissed and not rejected
    assert ledger._has_persisted_import_proof(imported[0], target=tmp_path) is True
    _path, payload = ledger._read_external_directory_authority(tmp_path)
    assert payload is not None
    assert payload.get("workspace") == ledger._workspace_directory_identity(tmp_path)


def test_swapped_directory_still_hard_fails_with_record_path_and_remediation(tmp_path: Path) -> None:
    descriptor = ledger._open_import_proof_directory(tmp_path, create=True)
    os.close(descriptor)
    path, payload = ledger._read_external_directory_authority(tmp_path)
    assert payload is not None
    bound = dict(payload["directories"][_PROOF_SCOPE])

    proofs = _swap_proofs_directory(tmp_path)
    assert _dir_identity(proofs) != bound

    with pytest.raises(OSError, match="does not match directory") as excinfo:
        ledger._open_import_proof_directory(tmp_path, create=True)
    err = str(excinfo.value)
    assert str(path) in err
    assert f"directories[{_PROOF_SCOPE}]" in err
    assert "brigade work rebind-authority --target" in err
    assert str(tmp_path.resolve()) in err

    _path, after = ledger._read_external_directory_authority(tmp_path)
    assert after is not None
    assert after["directories"][_PROOF_SCOPE] == bound


def test_legacy_looking_forged_record_is_not_migrated(tmp_path: Path) -> None:
    proofs = _ensure_proofs_dir(tmp_path)
    store = _plant_legacy_unsigned_record(tmp_path, {_PROOF_SCOPE: {"device": 1, "inode": 2}})
    live = _dir_identity(proofs)
    assert live != {"device": 1, "inode": 2}

    with pytest.raises(OSError, match="does not match directory") as excinfo:
        ledger._open_import_proof_directory(tmp_path, create=True)
    err = str(excinfo.value)
    assert str(store) in err
    assert f"directories[{_PROOF_SCOPE}]" in err
    assert "brigade work rebind-authority --target" in err

    _path, payload = ledger._read_external_directory_authority(tmp_path)
    assert payload is not None
    assert payload.get("workspace") is None
    assert payload["directories"][_PROOF_SCOPE] == {"device": 1, "inode": 2}


def test_unfixed_collapse_would_accept_swapped_directory_but_fix_refuses(tmp_path: Path) -> None:
    """Mutation guard: collapsing legacy-upgrade into live rewrite accepts forgery."""
    mutant = tmp_path / "mutant"
    fixed = tmp_path / "fixed"
    mutant.mkdir()
    fixed.mkdir()
    for workspace in (mutant, fixed):
        descriptor = ledger._open_import_proof_directory(workspace, create=True)
        os.close(descriptor)
        _swap_proofs_directory(workspace)

    mutant_root = ledger._open_directory_nofollow(mutant)
    mutant_proofs = ledger._open_directory_nofollow(mutant / ".brigade" / "work" / "imports" / "proofs")
    try:
        _unfixed_validate_collapses_legacy_and_mismatch(
            mutant,
            _PROOF_COMPONENTS,
            mutant_proofs,
            workspace=ledger._directory_identity(mutant_root),
        )
    finally:
        os.close(mutant_proofs)
        os.close(mutant_root)
    _path, mutant_payload = ledger._read_external_directory_authority(mutant)
    assert mutant_payload is not None
    assert mutant_payload["directories"][_PROOF_SCOPE] == _dir_identity(
        mutant / ".brigade" / "work" / "imports" / "proofs"
    )

    with pytest.raises(OSError, match="does not match directory"):
        ledger._open_import_proof_directory(fixed, create=True)
    _path, fixed_payload = ledger._read_external_directory_authority(fixed)
    assert fixed_payload is not None
    proofs_after_swap = fixed / ".brigade" / "work" / "imports" / "proofs"
    assert fixed_payload["directories"][_PROOF_SCOPE] != _dir_identity(proofs_after_swap)


def test_mutation_validate_requires_present_field_match_before_upgrade() -> None:
    source = inspect.getsource(ledger._validate_external_directory_authority)
    assert "_adopt_legacy_directory_authority_if_safe" in source
    assert "_is_legacy_external_directory_authority" in inspect.getsource(
        ledger._adopt_legacy_directory_authority_if_safe
    )
    adopt = inspect.getsource(ledger._adopt_legacy_directory_authority_if_safe)
    assert "_present_directory_authority_mismatches" in adopt
    assert "_upgrade_legacy_directory_authority" in adopt
    upgrade = inspect.getsource(ledger._upgrade_legacy_directory_authority)
    assert "workspace" in upgrade
    assert "_write_external_directory_authority" in upgrade


def test_rebind_authority_upgrades_legacy_record(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    proofs = _ensure_proofs_dir(tmp_path)
    store = _plant_legacy_unsigned_record(tmp_path, {_PROOF_SCOPE: _dir_identity(proofs)})

    assert ledger.rebind_directory_authority(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "upgraded legacy record" in out
    assert str(store) in out

    _path, payload = ledger._read_external_directory_authority(tmp_path)
    assert payload is not None
    assert payload["workspace"] == ledger._workspace_directory_identity(tmp_path)
    assert not ledger._is_legacy_external_directory_authority(payload)


def test_rebind_authority_refuses_mismatched_legacy_record(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _ensure_proofs_dir(tmp_path)
    store = _plant_legacy_unsigned_record(tmp_path, {_PROOF_SCOPE: {"device": 1, "inode": 2}})

    assert ledger.rebind_directory_authority(target=tmp_path) == 1
    err = capsys.readouterr().err
    assert str(store) in err
    assert "brigade work rebind-authority --target" in err
    _path, payload = ledger._read_external_directory_authority(tmp_path)
    assert payload is not None
    assert payload.get("workspace") is None


def test_doctor_warns_on_legacy_directory_authority(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    proofs = _ensure_proofs_dir(tmp_path)
    store = _plant_legacy_unsigned_record(tmp_path, {_PROOF_SCOPE: _dir_identity(proofs)})

    checks = doctor_mod._check_directory_authority(tmp_path)
    assert checks
    status, name, detail = checks[0]
    assert status == doctor_mod.WARN
    assert name == "directory-authority: legacy record"
    assert str(store) in detail
    assert "brigade work rebind-authority --target" in detail

    ctx = doctor_mod.build_context(tmp_path)
    core = doctor_mod.core_station_checks(ctx)
    legacy_checks = [check for check in core if check[1] == "directory-authority: legacy record"]
    assert legacy_checks
    assert legacy_checks[0][0] == doctor_mod.WARN

    rc = doctor_mod.run(target=tmp_path)
    out = capsys.readouterr().out
    assert rc in {0, 1}
    assert "directory-authority: legacy record" in out
    assert "brigade work rebind-authority --target" in out
    assert "[warn]" in out
