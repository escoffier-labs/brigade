"""t3-dispatch owner-only runtime JSON."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from brigade.grokbot_t3 import contracts as c
from brigade.grokbot_t3 import runtime_config as rc


def runtime_payload(tmp_path: Path, **overrides):
    payload = {
        "schema": rc.RUNTIME_SCHEMA,
        "enabled": True,
        "t3_fleet_bin": str(tmp_path / "t3-fleet"),
        "git_bin": str(tmp_path / "git"),
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


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX owner-only runtime files: docs/superpowers/plans/2026-10-16-grokbot-t3-dispatch.md:652",
)
def test_loads_valid_runtime(tmp_path):
    runtime = rc.load_runtime(write_runtime(tmp_path))
    assert runtime.enabled is True
    assert runtime.default_seat == "dsflash"
    assert runtime.fallback_seat == "glmflash"
    assert runtime.hosts == ("host-a", "host-b", "host-c")
    assert runtime.repositories["brigade"].default_branch == "main"
    assert runtime.ledger_path == tmp_path / "state" / "ledger.sqlite3"


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX owner-only runtime files: docs/superpowers/plans/2026-10-16-grokbot-t3-dispatch.md:652",
)
def test_rejects_group_readable_file(tmp_path):
    with pytest.raises(c.T3DispatchError):
        rc.load_runtime(write_runtime(tmp_path, mode=0o640))


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX owner-only runtime files: docs/superpowers/plans/2026-10-16-grokbot-t3-dispatch.md:652",
)
def test_rejects_symlink(tmp_path):
    real = write_runtime(tmp_path)
    link = tmp_path / "link.json"
    link.symlink_to(real)
    with pytest.raises(c.T3DispatchError):
        rc.load_runtime(link)


@pytest.mark.parametrize(
    "overrides",
    [
        {"hub_recipients": []},
        {"hub_recipients": {"host-a": "bad-node-id"}},
        {"hub_recipients": {"bad alias": "00000000-0000-4000-8000-000000000001"}},
        {"hub_recipients": {str(i): "00000000-0000-4000-8000-000000000001" for i in range(17)}},
        {"hub_token_file": "relative.token"},
        {"hub_token_file": None},
        {"hub_wake_target": "bad\nname"},
        {"hub_wake_target": ""},
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
        if sys.platform == "win32":
            rc.parse_runtime(runtime_payload(tmp_path, **overrides))
        else:
            rc.load_runtime(write_runtime(tmp_path, **overrides))


def test_fallback_seat_may_be_null(tmp_path):
    runtime = (
        rc.parse_runtime(runtime_payload(tmp_path, fallback_seat=None))
        if sys.platform == "win32"
        else rc.load_runtime(write_runtime(tmp_path, fallback_seat=None))
    )
    assert runtime.fallback_seat is None


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX owner-only runtime files: docs/superpowers/plans/2026-10-16-grokbot-t3-dispatch.md:652",
)
def test_validate_runtime_reference_returns_path(tmp_path):
    path = write_runtime(tmp_path)
    assert rc.validate_runtime_reference(str(path)) == str(path)
    with pytest.raises(c.T3DispatchError):
        rc.validate_runtime_reference("relative.json")


def intake_payload(**overrides):
    policy = {
        "label": "grokbot-build-approved",
        "queued_label": "grokbot-build-queued",
        "repositories": ["brigade"],
        "caller": "Builder",
        "gh_bin": str(Path("/usr/bin/gh").absolute()),
        "max_per_pass": 1,
        "headroom": 2,
    }
    policy.update(overrides)
    return policy


def test_intake_is_optional(tmp_path):
    assert rc.parse_runtime(runtime_payload(tmp_path)).intake is None


def test_intake_policy_preserves_repository_order(tmp_path):
    payload = runtime_payload(tmp_path, intake=intake_payload(repositories=["other", "brigade"]))
    payload["repositories"]["other"] = {"remote": "https://github.com/example/other.git", "default_branch": "main"}
    policy = rc.parse_runtime(payload).intake
    assert policy.repositories == (("other", "example/other"), ("brigade", "escoffier-labs/brigade"))
    assert (policy.label, policy.queued_label, policy.caller, policy.gh_bin, policy.max_per_pass, policy.headroom) == (
        "grokbot-build-approved",
        "grokbot-build-queued",
        "Builder",
        str(Path("/usr/bin/gh").absolute()),
        1,
        2,
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"extra": 1},
        {"label": "grokbot-build-queued"},
        {"label": "bad label"},
        {"queued_label": ""},
        {"repositories": ["missing"]},
        {"repositories": []},
        {"repositories": ["brigade", "brigade"]},
        {"headroom": 6},
        {"headroom": -1},
        {"headroom": 0},
        {"max_per_pass": 0},
        {"max_per_pass": 6},
        {"max_per_pass": True},
        {"gh_bin": "relative/gh"},
        {"caller": "bad:caller"},
    ],
)
def test_rejects_invalid_intake(tmp_path, overrides):
    with pytest.raises(c.T3DispatchError) as error:
        rc.parse_runtime(runtime_payload(tmp_path, intake=intake_payload(**overrides)))
    assert error.value.code == "invalid_request"


def test_intake_rejects_non_github_remote(tmp_path):
    payload = runtime_payload(tmp_path, intake=intake_payload())
    payload["repositories"]["brigade"]["remote"] = "https://example.com/team/brigade.git"
    with pytest.raises(c.T3DispatchError):
        rc.parse_runtime(payload)


def test_hub_aliases_default_empty_and_preserve_node_id(tmp_path):
    runtime = rc.parse_runtime(runtime_payload(tmp_path))
    assert runtime.hub_recipients == {}
    assert runtime.hub_wake_target == "default"
    assert runtime.hub_token_file is None
    aliases = {"host-a": "00000000-0000-4000-8000-000000000001"}
    configured = rc.parse_runtime(runtime_payload(tmp_path, hub_recipients=aliases))
    assert configured.hub_recipients == aliases
    aliases.clear()
    assert configured.hub_recipients == {"host-a": "00000000-0000-4000-8000-000000000001"}
