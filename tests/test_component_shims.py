"""Compatibility executables are verified members of the setup transaction."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from brigade import component_install as install
from brigade import component_manifest as manifest
from brigade import component_paths, component_shims as shims, component_state as state


@pytest.fixture
def installed(tmp_path):
    canonical = next(iter(manifest.COMPONENT_FAMILIES.values()))
    legacy = next(iter(manifest.COMPONENT_FAMILIES))
    root = str(tmp_path)
    source = Path(component_paths.managed_executable_path(root, canonical))
    source.parent.mkdir(parents=True)
    source.write_bytes(b"verified executable")
    source.chmod(0o755)
    record = state.InstalledComponentRecord(
        "a" * 40,
        canonical + "-linux-amd64",
        source.stat().st_size,
        hashlib.sha256(source.read_bytes()).hexdigest(),
        "https://example.invalid/asset",
        str(source),
    )
    current = state.InstalledState(1, "0.31.0", "fixture", "linux-amd64", "2026-01-01T00:00:00Z", {canonical: record})
    return current, root, source, Path(component_paths.managed_executable_path(root, legacy))


def create(current, root):
    shims.create_shims(current, root, install.verify_cached_asset, install.materialize_executable)


def test_hardlink_and_cleanup(installed):
    current, root, source, shim = installed
    create(current, root)
    assert shim.read_bytes() == source.read_bytes()
    if os.name != "nt":
        assert os.path.samefile(source, shim)
    shims.remove_shims(current, root, install.verify_cached_asset)
    assert not shim.exists()
    assert source.exists()


def test_cross_volume_copy_is_verified(installed, monkeypatch):
    current, root, source, shim = installed

    def cross_volume(*args):
        raise OSError("cross-device link")

    monkeypatch.setattr(os, "link", cross_volume)
    create(current, root)
    assert shim.read_bytes() == source.read_bytes()
    assert not os.path.samefile(source, shim)


def test_copy_corruption_is_refused_without_publishing(installed, monkeypatch):
    current, root, source, shim = installed

    def fail_link(*args):
        raise OSError("no links")

    def corrupt_copy(*, cache_path, managed_path):
        managed_path.write_bytes(b"corrupt")

    monkeypatch.setattr(os, "link", fail_link)
    with pytest.raises(install.ComponentInstallError, match="mismatch"):
        shims.create_shims(current, root, install.verify_cached_asset, corrupt_copy)
    assert not shim.exists()
    assert list(source.parent.iterdir()) == [source]


def test_foreign_shim_is_preserved(installed):
    current, root, source, shim = installed
    shim.write_bytes(b"operator executable")
    shims.remove_shims(current, root, install.verify_cached_asset)
    with pytest.raises(ValueError, match="unmanaged"):
        create(current, root)
    assert shim.read_bytes() == b"operator executable"


def test_external_record_cannot_authorize_shim_removal(installed, tmp_path):
    from dataclasses import replace

    current, root, source, shim = installed
    create(current, root)
    key, record = next(iter(current.components.items()))
    external = replace(current, components={key: replace(record, executable=str(tmp_path / "external"))})
    shims.remove_shims(external, root, install.verify_cached_asset)
    assert shim.exists()
    with pytest.raises(ValueError, match="outside managed bin"):
        create(external, root)


@pytest.mark.skipif(os.name == "nt", reason="Windows shims are verified copies, not hardlinks")
def test_corrupted_hardlink_is_still_removed_for_repair(installed):
    current, root, source, shim = installed
    create(current, root)
    # An in-place write through either name corrupts both, since they share an inode.
    with source.open("r+b") as handle:
        handle.write(b"CORRUPT")
    assert shim.read_bytes() == source.read_bytes()
    shims.remove_shims(current, root, install.verify_cached_asset)
    assert not shim.exists()
    assert source.exists()


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privileges on Windows")
def test_snapshot_restore_keeps_an_operator_symlink(tmp_path):
    legacy = next(iter(manifest.COMPONENT_FAMILIES))
    target = tmp_path / "operator-tool"
    target.write_bytes(b"operator tool")
    link = tmp_path / "bin" / legacy
    link.parent.mkdir()
    link.symlink_to(target)
    snapshots = install._snapshot_managed_executables([link])
    # A failed transaction may have replaced the link with a regular file.
    link.unlink()
    link.write_bytes(b"replaced")
    install._restore_managed_executables(snapshots)
    assert link.is_symlink()
    assert os.readlink(link) == str(target)
    assert target.read_bytes() == b"operator tool"
