"""Opt-in real adapter write probes.

These tests intentionally run third-party CLIs against a temporary directory.
They are skipped by default because they depend on local authentication and may
consume model quota.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from brigade import agents


pytestmark = pytest.mark.skipif(
    os.environ.get("BRIGADE_AGENT_WRITE_PROBES") != "1",
    reason="set BRIGADE_AGENT_WRITE_PROBES=1 to run real adapter write probes",
)


def _run_probe(cli_ref: str, prompt: str, tmp_path: Path) -> None:
    if not agents.detect(cli_ref):
        pytest.skip(f"{agents.command_for(cli_ref)} is not installed")
    result = subprocess.run(
        agents.build_argv(cli_ref, prompt, cwd=tmp_path),
        cwd=tmp_path,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr[-1000:]


def test_antigravity_writable_probe_creates_file_in_temp_dir(tmp_path: Path):
    target = tmp_path / "antigravity-write-probe.txt"
    _run_probe(
        "antigravity",
        f"Create a file named {target.name} containing exactly: antigravity write probe",
        tmp_path,
    )
    assert target.read_text().strip() == "antigravity write probe"


def test_kimi_writable_probe_creates_file_in_temp_dir(tmp_path: Path):
    target = tmp_path / "kimi-write-probe.txt"
    _run_probe(
        "kimi",
        f"Create a file named {target.name} containing exactly: kimi write probe",
        tmp_path,
    )
    assert target.read_text().strip() == "kimi write probe"
