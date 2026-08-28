"""Operator-only installer safety for the Obsidian adapter plugin."""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
INSTALLER = REPO / "ops" / "install-grokbot-operator-adapter.sh"
PLUGIN = REPO / "obsidian-plugin" / "grokbot-operator-adapter"
ADAPTER_DIR = ".obsidian/plugins/grokbot-operator-adapter"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_sha256sums_match_checked_in_plugin_bytes():
    listed = {}
    for line in (PLUGIN / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        digest, name = line.split()
        listed[name] = digest
    assert listed == {
        "main.js": _sha256(PLUGIN / "main.js"),
        "manifest.json": _sha256(PLUGIN / "manifest.json"),
    }


def _run(vault: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(INSTALLER), *[str(arg) for arg in args], str(vault)],
        check=False,
        capture_output=True,
        text=True,
        cwd=str(REPO),
    )


def test_installer_copies_only_adapter_files_mode_0644(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir(mode=0o700)
    os.chmod(vault, 0o700)
    result = _run(vault)
    assert result.returncode == 0, result.stderr
    dest = vault / ADAPTER_DIR
    assert dest.is_dir()
    assert not dest.is_symlink()
    names = sorted(path.name for path in dest.iterdir())
    assert names == ["main.js", "manifest.json"]
    for name in names:
        info = (dest / name).stat()
        assert stat.S_IMODE(info.st_mode) == 0o644
        assert _sha256(dest / name) == _sha256(PLUGIN / name)
    assert "enable" not in result.stdout.lower()
    assert "reload" not in result.stdout.lower() or "operator" in result.stdout.lower()


def test_installer_rejects_symlink_and_non_owner_vault(tmp_path: Path):
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    linked = tmp_path / "linked"
    linked.symlink_to(real)
    assert _run(linked).returncode != 0
    other = tmp_path / "other"
    other.mkdir(mode=0o700)
    os.chmod(other, 0o700)
    os.chown(other, 65534, 65534) if os.geteuid() == 0 else None
    if other.stat().st_uid != os.getuid():
        assert _run(other).returncode != 0


def test_installer_rejects_relative_and_nested_destination(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir(mode=0o700)
    relative = subprocess.run(
        ["bash", str(INSTALLER), "vault"],
        check=False,
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
    )
    assert relative.returncode != 0
    nested = subprocess.run(
        ["bash", str(INSTALLER), str(vault), str(vault / "extra")],
        check=False,
        capture_output=True,
        text=True,
        cwd=str(REPO),
    )
    assert nested.returncode != 0


def test_rollback_removes_only_adapter_directory(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir(mode=0o700)
    os.chmod(vault, 0o700)
    marker = vault / "keep.md"
    marker.write_text("keep\n", encoding="utf-8")
    assert _run(vault).returncode == 0
    dest = vault / ADAPTER_DIR
    assert dest.is_dir()
    result = _run(vault, "--rollback")
    assert result.returncode == 0, result.stderr
    assert not dest.exists()
    assert marker.is_file()
    assert (vault / ".obsidian" / "plugins").is_dir()
