"""Exact Windows legacy ownership, safe paths, and portable Compose selection."""

import os
import stat
import subprocess
import sys

import pytest

from tests import test_fleet_hub_dogfood_recipe as recipe
from tests.test_fleet_hub_dogfood_recipe import no_gpu_host
from tests.test_fleet_hub_setup_overrides import (  # noqa: F401
    _env,
    _fail_at,
    _init,
    _tree,
    gpu_host,
    setup,
    switch,
)


@pytest.fixture
def project(tmp_path):
    return recipe.project.__wrapped__(tmp_path)


SELECTION = "compose.yaml:compose.override.yaml:compose.hybrid.yaml"


@pytest.mark.parametrize("backend,token", [("local", "embed-local-token"), ("remote", "embed-token")])
def test_exact_crlf_legacy_migrates(project, tmp_path, backend, token):
    assert _init(project, tmp_path) == 0
    legacy = setup._compose_helper("document", backend, token).replace(b"\n", b"\r\n")
    override = project / "compose.override.yaml"
    override.write_bytes(legacy)
    assert switch(project, "hybrid") == 0
    assert override.read_bytes().endswith(b"services: {}\n")
    assert (project / "compose.hybrid.yaml").read_bytes() == setup._compose_helper(
        "document", "local", "embed-local-token"
    )


@pytest.mark.parametrize("mode", ["hybrid", "lexical"])
def test_crlf_legacy_extra_line_refused_unchanged(project, tmp_path, mode):
    assert _init(project, tmp_path) == 0
    legacy = setup._compose_helper("document", "local", "embed-local-token").replace(b"\n", b"\r\n")
    (project / "compose.override.yaml").write_bytes(legacy + b"# extra line\r\n")
    before = _tree(project)
    assert switch(project, mode) == 1
    assert _tree(project) == before


def snapshot(project):
    result = {}
    for path in project.rglob("*"):
        mode = path.lstat().st_mode
        result[str(path.relative_to(project))] = (
            mode,
            os.readlink(path) if stat.S_ISLNK(mode) else path.read_bytes() if stat.S_ISREG(mode) else None,
        )
    return result


@pytest.mark.parametrize("name", ["compose.override.yaml", "compose.hybrid.yaml", ".env"])
@pytest.mark.parametrize("kind", ["fifo", "symlink", "symlink-fifo"])
@pytest.mark.parametrize("action", ["init", "switch"])
def test_unsafe_compose_path_refused_before_writes(project, tmp_path, name, kind, action):
    if "fifo" in kind and not hasattr(os, "mkfifo"):
        pytest.skip("FIFO requires POSIX")
    assert _init(project, tmp_path) == 0
    path = project / name
    path.unlink(missing_ok=True)
    if kind == "fifo":
        os.mkfifo(path)
    else:
        target = tmp_path / "operator-file"
        if kind == "symlink-fifo":
            os.mkfifo(target)
        else:
            target.write_bytes(b"services: {}\n")
        try:
            path.symlink_to(target)
        except OSError:
            pytest.skip("symlinks unavailable")
    before = snapshot(project)
    code = """
import sys
from pathlib import Path
from tests import test_fleet_hub_dogfood_recipe as recipe
project = Path(sys.argv[1])
def no_write(*args, **kwargs):
    raise AssertionError('unexpected install write')
recipe.setup.ensure_secrets = no_write
recipe.setup.apply = no_write
if sys.argv[2] == 'init':
    rc = recipe._init(project, project.parent, run=recipe.gpu_host())
else:
    rc = recipe.setup.main(['--project-dir', str(project), 'switch', '--to', 'hybrid'], run=recipe.gpu_host())
raise SystemExit(rc)
"""
    done = subprocess.run([sys.executable, "-c", code, str(project), action], capture_output=True, text=True, timeout=5)
    assert done.returncode == 1
    assert "symlink" in done.stderr if "symlink" in kind else "not a regular file" in done.stderr
    assert "unexpected install write" not in done.stderr
    assert snapshot(project) == before


@pytest.mark.parametrize("separator", [None, ":"])
def test_hybrid_persists_separator_and_lexical_removes_pair(project, tmp_path, separator):
    assert _init(project, tmp_path) == 0
    env = _env(project)
    env["COMPOSE_FILE"] = SELECTION
    if separator is not None:
        env["COMPOSE_PATH_SEPARATOR"] = separator
    setup.write_env(project / ".env", env)
    assert switch(project, "hybrid") == 0
    assert _env(project)["COMPOSE_PATH_SEPARATOR"] == ":"
    assert _env(project)["COMPOSE_FILE"] == SELECTION
    assert switch(project, "lexical") == 0
    assert "COMPOSE_FILE" not in _env(project)
    assert "COMPOSE_PATH_SEPARATOR" not in _env(project)


@pytest.mark.parametrize("selection,separator", [(SELECTION, ";"), (None, ":"), (None, ";")])
def test_hybrid_refuses_operator_separator(project, tmp_path, selection, separator):
    assert _init(project, tmp_path) == 0
    env = _env(project)
    if selection is not None:
        env["COMPOSE_FILE"] = selection
    env["COMPOSE_PATH_SEPARATOR"] = separator
    setup.write_env(project / ".env", env)
    before = _tree(project)
    assert switch(project, "hybrid") == 1
    assert _tree(project) == before


@pytest.mark.parametrize("selection,separator", [(SELECTION, ";"), (None, ":"), ("custom.yaml", ":")])
def test_lexical_preserves_operator_separator_combination(project, tmp_path, capsys, selection, separator):
    assert _init(project, tmp_path) == 0
    env = _env(project)
    if selection is not None:
        env["COMPOSE_FILE"] = selection
    env["COMPOSE_PATH_SEPARATOR"] = separator
    setup.write_env(project / ".env", env)
    assert switch(project, "lexical") == 0
    assert _env(project).get("COMPOSE_FILE") == selection
    assert _env(project)["COMPOSE_PATH_SEPARATOR"] == separator
    assert "warning" in capsys.readouterr().err


@pytest.mark.parametrize("mode", ["hybrid", "lexical"])
@pytest.mark.parametrize("failure", ["write_env", "install_probed_token"])
def test_separator_pair_rollback_is_byte_identical(project, tmp_path, monkeypatch, mode, failure):
    assert _init(project, tmp_path, run=gpu_host() if mode == "lexical" else no_gpu_host) == 0
    before = _tree(project)
    _fail_at(monkeypatch, failure, "after")
    assert switch(project, mode) == 1
    assert _tree(project) == before
