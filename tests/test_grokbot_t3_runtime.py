"""t3-dispatch owner-only runtime JSON."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from brigade.grokbot_t3 import contracts as c
from brigade.grokbot_t3 import runtime_config as rc


def runtime_payload(tmp_path: Path, **overrides):
    payload = {
        "schema": rc.RUNTIME_SCHEMA,
        "enabled": True,
        "t3_fleet_bin": "/usr/bin/t3-fleet",
        "git_bin": "/usr/bin/git",
        "repositories": {
            "brigade": {"remote": "https://github.com/escoffier-labs/brigade.git", "default_branch": "main"},
        },
        "hosts": ["host-a", "host-b", "host-c"],
        "seats": ["dsflash", "glmflash", "sol61", "luna6", "opus55", "sonnet55"],
        "default_seat": "dsflash",
        "fallback_seat": "glmflash",
        "fleet_enrolled": False,
        "max_open": 6,
        "daily_limit": 30,
        "prompt_max_bytes": 16384,
        "ledger_path": str(tmp_path / "state" / "ledger.sqlite3"),
        "wake_path": str(tmp_path / "state" / "wake.json"),
        "writable_paths": [],
    }
    payload.update(overrides)
    return payload


def write_runtime(tmp_path: Path, mode: int = 0o600, **overrides) -> Path:
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps(runtime_payload(tmp_path, **overrides)), encoding="utf-8")
    os.chmod(path, mode)
    return path


def test_loads_valid_runtime(tmp_path):
    runtime = rc.load_runtime(write_runtime(tmp_path))
    assert runtime.enabled is True
    assert runtime.default_seat == "dsflash"
    assert runtime.fallback_seat == "glmflash"
    assert runtime.hosts == ("host-a", "host-b", "host-c")
    assert runtime.repositories["brigade"].default_branch == "main"
    assert runtime.ledger_path == tmp_path / "state" / "ledger.sqlite3"


def test_rejects_group_readable_file(tmp_path):
    with pytest.raises(c.T3DispatchError):
        rc.load_runtime(write_runtime(tmp_path, mode=0o640))


def test_rejects_symlink(tmp_path):
    real = write_runtime(tmp_path)
    link = tmp_path / "link.json"
    link.symlink_to(real)
    with pytest.raises(c.T3DispatchError):
        rc.load_runtime(link)


@pytest.mark.parametrize(
    "overrides",
    [
        {"default_seat": "nope"},
        {"fallback_seat": "dsflash"},
        {"fallback_seat": "missing"},
        {"max_open": 0},
        {"daily_limit": 201},
        {"prompt_max_bytes": 65536},
        {"hosts": []},
        {"seats": ["dsflash", "dsflash"]},
        {"t3_fleet_bin": "relative/t3-fleet"},
        {"repositories": {"brigade": {"remote": "git@github.com:x/y.git", "default_branch": "main"}}},
        {"repositories": {"brigade": {"remote": "https://github.com/x/y.git", "default_branch": "a/../b"}}},
        {"writable_paths": ["relative"]},
        {"extra": 1},
    ],
)
def test_rejects_invalid_runtime(tmp_path, overrides):
    with pytest.raises(c.T3DispatchError):
        rc.load_runtime(write_runtime(tmp_path, **overrides))


def test_fallback_seat_may_be_null(tmp_path):
    assert rc.load_runtime(write_runtime(tmp_path, fallback_seat=None)).fallback_seat is None


def test_validate_runtime_reference_returns_path(tmp_path):
    path = write_runtime(tmp_path)
    assert rc.validate_runtime_reference(str(path)) == str(path)
    with pytest.raises(c.T3DispatchError):
        rc.validate_runtime_reference("relative.json")
