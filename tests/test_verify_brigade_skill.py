"""Keep the verify-brigade lever honest.

The `registry/skills/verify-brigade/` skill exists so a change to the Brigade
CLI can be proved by driving the CLI. Its helper is only useful if it still
runs, so CI drives the same three commands a proof run starts with:
new-target, doctor-target, cleanup.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_DIR = REPO_ROOT / "registry" / "skills" / "verify-brigade"
CONTROL = SKILL_DIR / "control-brigade.py"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is required to create a target")


def control(*args: str, root: Path) -> tuple[int, dict]:
    """Run one control-brigade subcommand and return (exit code, parsed JSON)."""
    brigade = shlex.join([sys.executable, "-m", "brigade"])
    argv = [sys.executable, str(CONTROL), "--brigade", brigade, "--root", str(root), *args]
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(REPO_ROOT / "src"), env.get("PYTHONPATH", "")]))
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=600, env=env, check=False)
    assert proc.stdout, f"no JSON on stdout: {proc.stderr}"
    return proc.returncode, json.loads(proc.stdout)


def test_control_brigade_is_an_executable_helper():
    assert CONTROL.is_file(), "the verify-brigade skill must ship its helper"
    assert os.access(CONTROL, os.X_OK), "control-brigade.py must be executable"
    assert (SKILL_DIR / "SKILL.md").is_file()
    assert (SKILL_DIR / "features" / "README.md").is_file()


def test_new_target_doctor_target_and_cleanup(tmp_path):
    root = tmp_path / "state"

    code, created = control("new-target", "--depth", "repo", "--harnesses", "claude", root=root)
    assert code == 0, created
    assert created["ok"] is True
    assert created["action"] == "new-target"
    target = Path(created["target"])
    assert target.is_dir()
    assert target.is_relative_to(root / "targets")
    assert (target / ".brigade").is_dir()

    code, health = control("doctor-target", "--target", str(target), root=root)
    assert code == 0, health
    assert health["ok"] is True
    assert health["ready"] is True
    assert health["summary"]["failed"] == 0
    assert health["summary"]["total"] > 0

    code, preview = control("cleanup", "--dry-run", root=root)
    assert code == 0, preview
    assert preview["dry_run"] is True
    assert preview["would_remove"] == [str(target)]
    assert target.is_dir(), "a dry run must not remove anything"

    code, removed = control("cleanup", root=root)
    assert code == 0, removed
    assert removed["ok"] is True
    assert removed["removed"] == [str(target)]
    assert not target.exists()


def test_cleanup_refuses_a_target_it_did_not_create(tmp_path):
    root = tmp_path / "state"
    stranger = tmp_path / "not-ours"
    stranger.mkdir()

    code, payload = control("cleanup", "--target", str(stranger), root=root)
    assert code == 1
    assert payload["ok"] is False
    assert "refusing to remove" in payload["error"]
    assert stranger.is_dir()
