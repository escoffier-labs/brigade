"""The fixed workspace authority key never trusts receipt overrides or links."""

import os
import sys

import pytest

from brigade import receipt_signing
from tests.memory_read_fixtures import signing_key


def test_missing_fixed_key_never_uses_override_or_creates_key(tmp_path, monkeypatch):
    override = tmp_path / "override"
    override.write_text("34" * 32)
    monkeypatch.setenv(receipt_signing.KEY_ENV, str(override))
    assert receipt_signing.workspace_key(tmp_path) is None
    assert not (tmp_path / ".brigade").exists()


def test_fixed_key_platform_confinement(tmp_path, monkeypatch):
    signing_key(tmp_path)
    override = tmp_path / "override"
    override.write_text("34" * 32)
    monkeypatch.setenv(receipt_signing.KEY_ENV, str(override))
    expected = None if sys.platform == "win32" else bytes.fromhex("12" * 32)
    assert receipt_signing.workspace_key(tmp_path) == expected


@pytest.mark.skipif(os.name == "nt", reason="POSIX key confinement probes")
@pytest.mark.parametrize(
    "unsafe", ["mode", "owner", "symlink", "hardlink", "fifo", "parent-link", "short", "oversized"]
)
def test_unsafe_fixed_key_is_rejected(tmp_path, monkeypatch, unsafe):
    signing_key(tmp_path)
    key = tmp_path / ".brigade" / receipt_signing.KEY_NAME
    if unsafe == "mode":
        key.chmod(0o644)
    elif unsafe == "owner":
        monkeypatch.setattr(os, "getuid", lambda: key.stat().st_uid + 1)
    elif unsafe in {"symlink", "hardlink", "fifo"}:
        data = key.read_bytes()
        key.unlink()
        outside = tmp_path / "outside"
        outside.write_bytes(data)
        outside.chmod(0o600)
        if unsafe == "symlink":
            key.symlink_to(outside)
        elif unsafe == "hardlink":
            os.link(outside, key)
        else:
            os.mkfifo(key)
    elif unsafe == "parent-link":
        (tmp_path / ".brigade").rename(tmp_path / "outside")
        (tmp_path / ".brigade").symlink_to(tmp_path / "outside", target_is_directory=True)
    else:
        key.write_text("12" * (1 if unsafe == "short" else 300))
    assert receipt_signing.workspace_key(tmp_path) is None
