"""Tests for scripts/version_sync.py component manifest coverage."""

from __future__ import annotations

import importlib.util
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_version_sync_module(repo_root: Path):
    spec = importlib.util.spec_from_file_location("version_sync_test", ROOT / "scripts/version_sync.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.ROOT = repo_root
    module.RECORDINGS = []
    return module


def test_version_sync_checks_component_manifest_brigade_version(tmp_path):
    repo = tmp_path / "repo"
    init_dir = repo / "src/brigade"
    manifest_dir = repo / "src/brigade/templates/components"
    init_dir.mkdir(parents=True)
    manifest_dir.mkdir(parents=True)
    (repo / "pyproject.toml").write_text('[project]\nversion = "1.0.0"\n')
    (init_dir / "__init__.py").write_text('__version__ = "1.0.0"\n')
    manifest_rel = "src/brigade/templates/components/manifest-v1.json"
    manifest_path = repo / manifest_rel
    manifest_path.write_text('{"brigade_version": "0.9.0"}\n')

    module = _load_version_sync_module(repo)
    stderr = io.StringIO()
    original_stderr = sys.stderr
    sys.stderr = stderr
    try:
        exit_code = module.check("1.0.0")
    finally:
        sys.stderr = original_stderr

    output = stderr.getvalue()
    assert exit_code == 1
    assert manifest_rel in output
    assert "0.9.0" in output


def test_version_sync_checks_and_rewrites_mcp_server_manifests(tmp_path, capsys):
    repo = tmp_path / "repo"
    (repo / "src/brigade/templates/components").mkdir(parents=True)
    (repo / "mcp-registry/memory").mkdir(parents=True)
    (repo / "pyproject.toml").write_text('[project]\nversion = "1.0.0"\n')
    (repo / "src/brigade/__init__.py").write_text('__version__ = "1.0.0"\n')
    (repo / "src/brigade/templates/components/manifest-v1.json").write_text('{"brigade_version": "1.0.0"}\n')
    server = repo / "mcp-registry/memory/server.json"
    server.write_text('{"version": "0.9.0", "packages": [{"identifier": "brigade-cli", "version": "1.0.0"}]}\n')

    module = _load_version_sync_module(repo)
    assert module.check("1.0.0") == 1
    assert "mcp-registry/memory/server.json declares 0.9.0" in capsys.readouterr().err

    assert module.write("1.0.0") == 0
    assert module.check("1.0.0") == 0
    assert server.read_text().count('"version": "1.0.0"') == 2
