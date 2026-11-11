"""Operator Compose content survives setup and its transactional failures."""

import os
import shutil
import subprocess

import pytest
from tests import test_fleet_hub_dogfood_recipe as recipe

from tests.test_fleet_hub_dogfood_recipe import (  # noqa: F401
    _env,
    _fail_at,
    _init,
    _tree,
    gpu_host,
    setup,
)


@pytest.fixture
def project(tmp_path):
    return recipe.project.__wrapped__(tmp_path)


OPERATOR = b"""# Operator settings\nservices:
  hub:
    image: sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
    command: ["--trust-tailscale-identity"]
volumes:
  brigade-state:
    driver_opts:
      type: none
      o: bind
      device: /srv/example-hub-state
"""


def switch(project, mode):
    return setup.main(["--project-dir", str(project), "switch", "--to", mode], run=gpu_host())


@pytest.mark.parametrize("action", ["init", "hybrid", "lexical"])
def test_operator_override_survives_each_setup_command(project, tmp_path, action):
    if action != "init":
        assert _init(project, tmp_path) == 0
    override = project / "compose.override.yaml"
    override.write_bytes(OPERATOR)
    rc = _init(project, tmp_path, run=gpu_host()) if action == "init" else switch(project, action)
    assert rc == 0
    assert override.read_bytes() == OPERATOR


def test_compose_file_list_includes_operator_and_mount_and_cleans_up(project, tmp_path):
    (project / "compose.override.yaml").write_bytes(OPERATOR)
    assert _init(project, tmp_path, run=gpu_host()) == 0
    assert _env(project)["COMPOSE_FILE"].split(":") == ["compose.yaml", "compose.override.yaml", "compose.hybrid.yaml"]
    assert "file: ./secrets/embed-local-token" in (project / "compose.hybrid.yaml").read_text()
    if shutil.which("docker") and os.name != "nt":
        done = subprocess.run(
            ["docker", "compose", "--project-directory", str(project), "config", "--format", "json"],
            cwd=project,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert done.returncode == 0, done.stderr
        import json

        config = json.loads(done.stdout)
        assert config["services"]["hub"]["image"] == "sha256:" + "a" * 64
        assert config["services"]["hub"]["command"] == ["--trust-tailscale-identity"]
        assert config["volumes"]["brigade-state"]["driver_opts"]["device"] == "/srv/example-hub-state"
        assert "embed_token" in {item["source"] for item in config["services"]["hub"]["secrets"]}
    assert switch(project, "lexical") == 0
    assert not (project / "compose.hybrid.yaml").exists()
    assert "COMPOSE_FILE" not in _env(project)
    assert (project / "compose.override.yaml").read_bytes() == OPERATOR


def test_legacy_setup_override_migrates(project, tmp_path):
    assert _init(project, tmp_path, run=gpu_host()) == 0
    managed = project / "compose.hybrid.yaml"
    old = project / "compose.override.yaml"
    # Build the old installation without calling any new helper API.
    legacy = (
        "# Written by bin/hub-setup.py for the local embedder backend.\n"
        "services:\n  hub:\n    secrets:\n      - admin_token\n      - findings_key\n      - embed_token\n"
        "secrets:\n  embed_token:\n    file: ./secrets/embed-local-token\n"
    ).encode()
    if managed.exists():
        managed.unlink()
    old.write_bytes(legacy)
    assert switch(project, "hybrid") == 0
    assert not old.exists()
    assert managed.read_bytes() == legacy
    assert _env(project)["COMPOSE_FILE"] == "compose.yaml:compose.hybrid.yaml"


@pytest.mark.parametrize("mode", ["hybrid", "lexical"])
@pytest.mark.parametrize("failure", ["managed-write", "legacy-delete", "env-write", "token-install"])
def test_new_compose_write_steps_roll_back_bytes(project, tmp_path, monkeypatch, mode, failure):
    assert _init(project, tmp_path, run=gpu_host()) == 0
    if failure == "legacy-delete":
        (project / "compose.hybrid.yaml").rename(project / "compose.override.yaml")
    else:
        (project / "compose.override.yaml").write_bytes(OPERATOR)
    before = _tree(project, tmp_path / "backups")
    if failure in ("env-write", "token-install"):
        _fail_at(monkeypatch, "write_env" if failure == "env-write" else "install_probed_token", "after")
    else:
        from pathlib import Path

        method = "write_bytes" if mode == "hybrid" and failure == "managed-write" else "unlink"
        name = "compose.override.yaml" if failure == "legacy-delete" else "compose.hybrid.yaml"
        real = getattr(Path, method)
        injected = []

        def fail(path, *args, **kwargs):
            result = real(path, *args, **kwargs)
            if path.name == name and not injected:
                injected.append(True)
                raise OSError("injected compose failure")
            return result

        monkeypatch.setattr(Path, method, fail)
    assert switch(project, mode) == 1
    assert _tree(project, tmp_path / "backups") == before


def test_lexical_keeps_explicit_operator_file_list(project, tmp_path):
    assert _init(project, tmp_path) == 0
    env = _env(project)
    env["COMPOSE_FILE"] = "compose.yaml:custom.yaml"
    setup.write_env(project / ".env", env)
    (project / "custom.yaml").write_bytes(OPERATOR)
    assert switch(project, "hybrid") == 0
    assert _env(project)["COMPOSE_FILE"] == "compose.yaml:custom.yaml:compose.hybrid.yaml"
    assert switch(project, "lexical") == 0
    assert _env(project)["COMPOSE_FILE"] == "compose.yaml:custom.yaml"
    assert (project / "custom.yaml").read_bytes() == OPERATOR
