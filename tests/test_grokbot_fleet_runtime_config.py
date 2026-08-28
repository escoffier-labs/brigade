"""Runtime configuration and environment tests for Fleet Steward."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from brigade.grokbot_fleet.contracts import FleetError
from brigade.grokbot_fleet.runtime_config import (
    FLEET_PROBE_ENVIRONMENT_KEYS,
    fleet_probe_environment,
    load_fleet_runtime_env,
    parse_fleet_private_runtime,
    project_fleet_public_registry,
)

PRIVATE_SSH_HYPERVISOR = "lab-hypervisor"
PRIVATE_SSH_WORKER = "lab-worker.01"
PRIVATE_UNIT_BRIDGE = "research-bridge.service"
PRIVATE_RUNTIME_PATH = "/var/lib/fleet-steward/runtime.json"
PRIVATE_LEDGER_PATH = "/var/lib/fleet-steward/ledger.json"
PRIVATE_ACTION_STATE_PATH = "/var/lib/fleet-steward/actions"
PRIVATE_APPROVAL_DIR = "/etc/fleet-steward/approvals"
FLEET_TOKEN = "F" * 32
DISPATCH_TOKEN = "D" * 32

VALID_RUNTIME = {
    "version": 1,
    "roles": {
        "control-plane": {"adapter": "local"},
        "hypervisor": {"adapter": "linux", "ssh_alias": PRIVATE_SSH_HYPERVISOR},
        "worker": {"adapter": "linux", "ssh_alias": PRIVATE_SSH_WORKER},
    },
    "services": {
        "research-bridge": {"unit": PRIVATE_UNIT_BRIDGE, "manager": "user"},
        "virtualization-api": {"unit": "virtualization-api.service", "manager": "system"},
        "overlay-network": {"unit": "overlay-network.service", "manager": "system"},
    },
}


def _env(**overrides: str) -> dict[str, str]:
    payload = {
        "GROKBOT_FLEET_TOKEN": FLEET_TOKEN,
        "GROKBOT_FLEET_HOST": "127.0.0.1",
        "GROKBOT_FLEET_PORT": "8771",
        "GROKBOT_FLEET_RUNTIME_PATH": PRIVATE_RUNTIME_PATH,
        "GROKBOT_FLEET_LEDGER_PATH": PRIVATE_LEDGER_PATH,
        "GROKBOT_FLEET_ACTION_STATE_PATH": PRIVATE_ACTION_STATE_PATH,
        "GROKBOT_FLEET_APPROVAL_DIR": PRIVATE_APPROVAL_DIR,
    }
    payload.update(overrides)
    return payload


def _expect_neutral(action, secrets: list[str]) -> None:
    with pytest.raises(FleetError) as caught:
        action()
    assert caught.value.code == "invalid_request"
    assert caught.value.message in {
        "Fleet runtime configuration is invalid",
        "Fleet environment is invalid",
    }
    text = f"{caught.value}\n{caught.value.message}"
    for secret in secrets:
        assert secret not in text


def test_private_runtime_parses_and_projects_public_registry():
    parsed = parse_fleet_private_runtime(VALID_RUNTIME)
    assert parsed["roles"]["hypervisor"]["ssh_alias"] == PRIVATE_SSH_HYPERVISOR
    registry = project_fleet_public_registry(parsed)
    assert [target["alias"] for target in registry.targets] == ["control-plane", "hypervisor", "worker"]
    assert [service["service_id"] for service in registry.services] == [
        "research-bridge",
        "virtualization-api",
        "overlay-network",
    ]


def test_private_runtime_rejects_unknown_fields_and_duplicate_aliases():
    _expect_neutral(lambda: parse_fleet_private_runtime({**VALID_RUNTIME, "extra": True}), ["extra"])
    _expect_neutral(
        lambda: parse_fleet_private_runtime(
            {
                **VALID_RUNTIME,
                "roles": {
                    **VALID_RUNTIME["roles"],
                    "worker": {"adapter": "linux", "ssh_alias": PRIVATE_SSH_HYPERVISOR},
                },
            }
        ),
        [PRIVATE_SSH_HYPERVISOR],
    )


def test_runtime_env_requires_loopback_token_and_disjoint_paths():
    loaded = load_fleet_runtime_env(_env())
    assert loaded["host"] == "127.0.0.1"
    assert loaded["port"] == 8771
    assert loaded["token"] == FLEET_TOKEN
    _expect_neutral(lambda: load_fleet_runtime_env(_env(GROKBOT_FLEET_TOKEN="short")), [])
    _expect_neutral(
        lambda: load_fleet_runtime_env(_env(), dispatch_token=FLEET_TOKEN),
        [FLEET_TOKEN],
    )
    _expect_neutral(lambda: load_fleet_runtime_env(_env(GROKBOT_FLEET_HOST="0.0.0.0")), ["0.0.0.0"])
    _expect_neutral(
        lambda: load_fleet_runtime_env(_env(GROKBOT_FLEET_LEDGER_PATH="/var/lib/fleet-steward/runtime.json/nested")),
        [PRIVATE_RUNTIME_PATH],
    )
    _expect_neutral(
        lambda: load_fleet_runtime_env(_env(GROKBOT_FLEET_RUNTIME_PATH="/var/lib/fleet-steward/../runtime.json")),
        [".."],
    )


def _write_runtime(path: Path, payload: dict, mode: int = 0o600) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    os.chmod(path, mode)
    return path


def _expect_runtime_rejected(action) -> None:
    with pytest.raises(FleetError) as caught:
        action()
    assert caught.value.code == "invalid_request"
    assert caught.value.message == "Fleet environment is invalid"


def test_secure_runtime_read_requires_owner_only_regular_file(tmp_path: Path):
    from brigade.grokbot_fleet.runtime_config import read_secure_runtime_text

    path = _write_runtime(tmp_path / "runtime.json", VALID_RUNTIME)
    assert json.loads(read_secure_runtime_text(str(path)))["version"] == 1
    world_readable = _write_runtime(tmp_path / "world.json", VALID_RUNTIME, 0o644)
    _expect_runtime_rejected(lambda: read_secure_runtime_text(str(world_readable)))
    link = tmp_path / "runtime.link"
    link.symlink_to(path)
    _expect_runtime_rejected(lambda: read_secure_runtime_text(str(link)))


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="uid checks are POSIX")
def test_secure_runtime_read_rejects_foreign_owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from brigade.grokbot_fleet.runtime_config import read_secure_runtime_text

    path = _write_runtime(tmp_path / "runtime.json", VALID_RUNTIME)
    owner = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: owner + 1)
    _expect_runtime_rejected(lambda: read_secure_runtime_text(str(path)))


def test_secure_runtime_read_rejects_path_swap_to_alternate_unit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from brigade.grokbot_fleet import runtime_config as runtime_mod
    from brigade.grokbot_fleet.runtime_config import read_secure_runtime_text

    original = _write_runtime(tmp_path / "runtime.json", VALID_RUNTIME)
    swapped = {
        **VALID_RUNTIME,
        "services": {
            **VALID_RUNTIME["services"],
            "research-bridge": {"unit": "attacker.service", "manager": "user"},
        },
    }
    alternate = _write_runtime(tmp_path / "alternate.json", swapped)
    real_lstat = runtime_mod.os.lstat

    def lstat_then_swap(path, *args, **kwargs):
        info = real_lstat(path, *args, **kwargs)
        candidate = Path(os.fspath(path))
        if candidate.name == "runtime.json" and alternate.exists():
            os.replace(alternate, candidate)
        return info

    monkeypatch.setattr(runtime_mod.os, "lstat", lstat_then_swap)
    _expect_runtime_rejected(lambda: read_secure_runtime_text(str(original)))
    assert "attacker.service" in original.read_text(encoding="utf-8")


def test_probe_environment_is_allowlisted():
    source = {
        **{key: f"value-{key}" for key in FLEET_PROBE_ENVIRONMENT_KEYS},
        "GROKBOT_FLEET_TOKEN": FLEET_TOKEN,
        "SSH_AUTH_SOCK": "/tmp/ssh.sock",
        "UNRELATED": "no",
    }
    env = fleet_probe_environment(source)
    assert set(env) == set(FLEET_PROBE_ENVIRONMENT_KEYS)
    assert "GROKBOT_FLEET_TOKEN" not in env
    assert "UNRELATED" not in env
