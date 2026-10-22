"""Isolation contract for Brigade-owned Python subprocesses."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("shadow", ["brigade.py", "brigade/__init__.py"])
def test_module_argv_runs_exact_package_without_ambient_imports(tmp_path, monkeypatch, shadow):
    from brigade import isolated_exec

    installed = tmp_path / "installed"
    package = installed / "brigade"
    shutil.copytree(
        Path(isolated_exec.__file__).resolve().parent, package, ignore=shutil.ignore_patterns("__pycache__")
    )
    (package / "isolation_probe.py").write_text(
        "import json, platform, site, sys, brigade, optional_dependency\n"
        "print(json.dumps({'file': brigade.__file__, 'argv': sys.argv[1:], "
        "'isolated': sys.flags.isolated, 'user_site': site.ENABLE_USER_SITE, 'path': sys.path, "
        "'dependency': optional_dependency.__file__, 'platform': platform.__file__}))\n",
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sentinel = tmp_path / "executed"
    (installed / "optional_dependency.py").write_text("VALUE = 'installed dependency'\n", encoding="utf-8")
    hostile_source = f"from pathlib import Path\nPath({str(sentinel)!r}).touch()\n"
    (installed / "platform.py").write_text(hostile_source, encoding="utf-8")
    (installed / "hostile.pth").write_text(
        f"import pathlib; pathlib.Path({str(sentinel)!r}).touch()\n{workspace}\n", encoding="utf-8"
    )
    (workspace / "optional_dependency.py").write_text(hostile_source, encoding="utf-8")
    planted = workspace / shadow
    planted.parent.mkdir(parents=True, exist_ok=True)
    planted.write_text(f"from pathlib import Path\nPath({str(sentinel)!r}).touch()\n", encoding="utf-8")
    launcher = tmp_path / "launch.py"
    launcher.write_text(
        "import os, subprocess, sys\nfrom brigade.isolated_exec import module_argv, trusted_cwd\n"
        f"os.environ['PYTHONPATH'] = {str(workspace)!r}\n"
        "argv = module_argv('brigade.isolation_probe', 'space argument', '--flag', python=None)\n"
        "assert '\\n' not in argv[3]\n"
        "result = subprocess.run(argv, cwd=trusted_cwd(), capture_output=True, text=True)\n"
        "print(result.stdout, end='')\nprint(result.stderr, end='', file=sys.stderr)\n"
        "raise SystemExit(result.returncode)\n",
        encoding="utf-8",
    )
    env = dict(os.environ, PYTHONPATH=str(installed))
    result = subprocess.run([sys.executable, str(launcher)], cwd=workspace, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert not sentinel.exists()
    payload = json.loads(result.stdout)
    assert Path(payload["file"]) == package / "__init__.py"
    assert payload["argv"] == ["space argument", "--flag"]
    assert payload["isolated"] == 1
    assert payload["user_site"] is False
    assert str(workspace) not in payload["path"]
    assert payload["path"][-1] == str(installed)
    assert Path(payload["dependency"]) == installed / "optional_dependency.py"
    assert not Path(payload["platform"]).is_relative_to(installed)
    assert isolated_exec.module_argv("brigade", python="renderer-python")[0] == "renderer-python"
    assert isolated_exec.trusted_cwd() == Path(isolated_exec.__file__).resolve().parent


@pytest.mark.parametrize("missing", ["file", "directory"])
def test_bootstrap_reports_unloadable_initializer_with_exit_78(tmp_path, missing):
    from brigade import isolated_exec

    initializer = tmp_path / "brigade" / "__init__.py"
    if missing == "directory":
        initializer.mkdir(parents=True)
    argv = isolated_exec.package_argv(initializer, "brigade.nothing")
    result = subprocess.run(argv, capture_output=True, text=True, cwd=tmp_path, check=False)
    assert result.returncode == 78
    assert "brigade:cannot-load:" in result.stderr
    assert "Traceback" not in result.stderr
