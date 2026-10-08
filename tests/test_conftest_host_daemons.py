"""The suite must never start a host process manager daemon.

``pm2 jlist`` spawns a PM2 God daemon under ``$PM2_HOME`` when none is running.
Under the suite ``HOME`` is a per-session ``operator_home`` temp dir, so every
real ``pm2`` call started a fresh daemon that outlived pytest and pinned its
temp dir open.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from brigade.operator_cmd import surfaces


def _fake_pm2(tmp_path: Path) -> tuple[Path, Path]:
    marker = tmp_path / "pm2-ran"
    fake = tmp_path / "bin" / "pm2"
    fake.parent.mkdir()
    fake.write_text(f"#!/bin/sh\ntouch '{marker}'\necho '[]'\n")
    fake.chmod(0o755)
    return fake, marker


def test_suite_treats_pm2_as_absent(tmp_path):
    fake, marker = _fake_pm2(tmp_path)

    with pytest.raises(FileNotFoundError):
        subprocess.run([str(fake), "jlist"], check=False)

    assert not marker.exists()


def test_read_only_pm2_probe_reports_command_not_found(tmp_path):
    fake, marker = _fake_pm2(tmp_path)

    result = surfaces._run_read_only_command([str(fake), "jlist"])

    assert result == {"ok": False, "stdout": "", "error": "command not found"}
    assert not marker.exists()
