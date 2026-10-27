"""Bridge package contracts, independent of the development CLI version."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from brigade import toml_compat

BRIDGE = Path(__file__).resolve().parents[1] / "distribution/brigadestack"


def test_python_bridge_adds_only_an_alias_for_the_exact_stable_dependency():
    manifest = toml_compat.loads((BRIDGE / "pypi/pyproject.toml").read_text())
    project = manifest["project"]
    assert (project["name"], project["version"]) == ("brigadestack", "0.30.0")
    assert project["requires-python"] == ">=3.10"
    assert project["dependencies"] == ["brigade-cli==0.30.0"]
    assert project["scripts"] == {"brigadestack": "brigade.cli:process_main"}
    # No discovery of a second brigade package or alternate implementation.
    assert manifest["tool"]["setuptools"]["packages"] == []
    assert manifest["tool"]["setuptools"]["py-modules"] == []


def test_npm_bridge_keeps_existing_global_brigade_bin_and_pins_stable():
    manifest = json.loads((BRIDGE / "npm/package.json").read_text())
    assert (manifest["name"], manifest["version"]) == ("brigadestack", "0.30.0")
    assert manifest["dependencies"] == {"@escoffier-labs/brigade": "0.30.0"}
    assert manifest["bin"] == {"brigadestack": "launcher.cjs"}
    assert manifest["engines"]["node"] == ">=22"


def test_npm_launcher_contracts():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for bridge launcher contract tests")
    result = subprocess.run(
        [node, "--test", str(BRIDGE / "npm/launcher.test.cjs")],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
