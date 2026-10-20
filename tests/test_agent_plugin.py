"""Agent plugin package: manifests, pins, skill drift, and the SessionStart hook."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from brigade import toml_compat

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "brigade"
HOOK = PLUGIN / "hooks" / "session-start.sh"


def _version() -> str:
    return toml_compat.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]


def _json(rel: str) -> dict:
    return json.loads((PLUGIN / rel).read_text())


def test_manifests_name_and_version_match_pyproject():
    for rel in (".claude-plugin/plugin.json", ".codex-plugin/plugin.json"):
        manifest = _json(rel)
        assert manifest["name"] == "brigade"
        assert manifest["version"] == _version()
    codex = _json(".codex-plugin/plugin.json")
    assert (PLUGIN / codex["skills"]).is_dir()
    assert (PLUGIN / codex["mcpServers"]).is_file()


def test_mcp_configs_pin_the_npm_package_and_inbox():
    for rel, inbox in ((".mcp.json", "claude"), ("codex.mcp.json", "codex")):
        server = _json(rel)["mcpServers"]["brigade-memory"]
        assert server["command"] == "npx"
        assert server["args"] == [
            "-y",
            f"@escoffier-labs/brigade@{_version()}",
            "memory",
            "serve-mcp",
            "--stdio",
            "--inbox",
            inbox,
        ]


def test_plugin_skills_match_canonical_sources():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/sync_plugin_skills.py")], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert (PLUGIN / "skills" / "enforce" / "SKILL.md").read_text().startswith("---\nname: enforce\n")


def _run_hook(tmp_path: Path, project: Path, path_dirs: list[Path]) -> subprocess.CompletedProcess:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {
        "HOME": str(home),
        "PATH": os.pathsep.join([*map(str, path_dirs), "/usr/bin", "/bin"]),
        "CLAUDE_PROJECT_DIR": str(project),
    }
    return subprocess.run(["bash", str(HOOK)], env=env, capture_output=True, text=True, timeout=30)


@pytest.mark.skipif(os.name != "posix", reason="the SessionStart hook is a POSIX shell script")
def test_hook_exits_zero_without_brigade_on_path(tmp_path: Path):
    project = tmp_path / "proj"
    project.mkdir()
    result = _run_hook(tmp_path, project, [])
    assert result.returncode == 0
    assert "brigade-memory MCP tools" in result.stdout


@pytest.mark.skipif(os.name != "posix", reason="the SessionStart hook is a POSIX shell script")
def test_hook_prints_brief_in_wired_repo_and_skips_when_enforced(tmp_path: Path):
    project = tmp_path / "proj"
    (project / ".brigade").mkdir(parents=True)
    (project / ".brigade" / "config.json").write_text("{}")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "brigade"
    fake.write_text('#!/bin/sh\necho FAKE-BRIEF "$@"\n')
    fake.chmod(0o755)
    result = _run_hook(tmp_path, project, [bin_dir])
    assert result.returncode == 0
    assert "FAKE-BRIEF work brief --target" in result.stdout
    (project / ".brigade" / "claude-hooks.json").write_text("{}")
    assert _run_hook(tmp_path, project, [bin_dir]).stdout == ""


@pytest.mark.skipif(os.name != "posix", reason="the SessionStart hook is a POSIX shell script")
def test_hook_parses_and_exits_zero_with_home_unset(tmp_path: Path):
    assert subprocess.run(["bash", "-n", str(HOOK)], capture_output=True, timeout=30).returncode == 0
    project = tmp_path / "proj"
    project.mkdir()
    env = {"PATH": "/usr/bin:/bin", "CLAUDE_PROJECT_DIR": str(project)}
    result = subprocess.run(["bash", str(HOOK)], env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert "brigade-memory MCP tools" in result.stdout


@pytest.mark.skipif(os.name != "posix", reason="the SessionStart hook is a POSIX shell script")
def test_hook_skips_the_brief_when_no_timeout_runner_exists(tmp_path: Path):
    project = tmp_path / "proj"
    (project / ".brigade").mkdir(parents=True)
    (project / ".brigade" / "config.json").write_text("{}")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("dirname", "head"):
        found = shutil.which(tool)
        assert found is not None
        (bin_dir / tool).symlink_to(found)
    stalled = bin_dir / "brigade"
    stalled.write_text("#!/bin/sh\nsleep 30\n")
    stalled.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    env = {"HOME": str(home), "PATH": str(bin_dir), "CLAUDE_PROJECT_DIR": str(project)}
    started = time.monotonic()
    result = subprocess.run(
        [shutil.which("bash") or "/bin/bash", str(HOOK)], env=env, capture_output=True, text=True, timeout=30
    )
    assert time.monotonic() - started < 5
    assert result.returncode == 0, result.stderr
    assert "brigade-memory MCP tools" in result.stdout
