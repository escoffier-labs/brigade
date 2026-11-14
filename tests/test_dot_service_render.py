"""Shared service/file safety assertions ported from the retired listener tests."""

import os
import json
import subprocess
from pathlib import Path
import pytest
from brigade.dot import service_render as grokbot_ops, transport as grokbot_mcp
from tests._posix import requires_opath


@requires_opath
def test_posix_path_walker_uses_opath_root_and_keeps_read_write_nofollow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    directory = tmp_path / "nested" / "config"
    path = directory / "config.json"
    real_open = os.open
    root_flags: list[int] = []

    def protected_root_open(
        name: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if name == "/" and dir_fd is None:
            root_flags.append(flags)
            if not flags & os.O_PATH:
                raise PermissionError("root is unreadable")
        return real_open(name, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(grokbot_ops.os, "open", protected_root_open)

    grokbot_ops._write_text_nofollow_atomic(path, "safe\n", mode=0o600)
    assert grokbot_ops._read_regular_text(path) == "safe\n"
    assert root_flags and root_flags[0] & os.O_PATH

    outside = tmp_path / "outside.txt"
    outside.write_text("keep me\n", encoding="utf-8")
    path.unlink()
    path.symlink_to(outside)

    with pytest.raises(OSError):
        grokbot_ops._read_regular_text(path)
    with pytest.raises(OSError):
        grokbot_ops._write_text_nofollow_atomic(path, "changed\n", mode=0o600)
    assert outside.read_text(encoding="utf-8") == "keep me\n"


def test_inspect_service_result_uses_fixed_argv_and_succeeds_on_exact_success(monkeypatch):
    recorded = _record_systemctl(monkeypatch, stdout="  success \n")
    check = grokbot_ops.inspect_service_result("operator")
    assert check == {"check": "service-result", "status": "ok"}
    assert recorded[0]["argv"] == [
        "systemctl",
        "--user",
        "show",
        "brigade-grokbot-operator.service",
        "--property=Result",
        "--value",
    ]
    kwargs = recorded[0]["kwargs"]
    assert kwargs.get("shell") is not True
    assert kwargs.get("check") is not True
    assert kwargs.get("timeout") == grokbot_ops.DEFAULT_TIMEOUT_SECONDS
    assert not any(verb in recorded[0]["argv"] for verb in _SYSTEMD_MUTATION_VERBS)
    assert not any(str(part).endswith(".timer") for part in recorded[0]["argv"])


@pytest.mark.parametrize(
    "stdout,returncode,error",
    (
        ("failed\n", 0, None),
        ("Result=success\n", 0, None),
        ("SUCCESS\n", 0, None),
        ("success extra\n", 0, None),
        ("", 0, None),
        ("success\n", 1, None),
        ("success\n", 0, FileNotFoundError("systemctl")),
        ("success\n", 0, OSError("unavailable")),
        (
            "success\n",
            0,
            subprocess.TimeoutExpired(
                ["systemctl", "--user", "show", "brigade-grokbot-operator.service"],
                5,
            ),
        ),
    ),
)
def test_inspect_service_result_fails_closed_without_raw_output(
    monkeypatch, stdout: str, returncode: int, error: BaseException | None
):
    recorded = _record_systemctl(monkeypatch, stdout=stdout, returncode=returncode, error=error)
    check = grokbot_ops.inspect_service_result("operator")
    assert check == {"check": "service-result", "status": "fail"}
    assert set(check) == {"check", "status"}
    if error is None:
        assert recorded[0]["argv"][3] == "brigade-grokbot-operator.service"


def test_inspect_service_result_sanitizes_raw_stdout_and_stderr(monkeypatch):
    leak = "timeout /srv/example/secret/.config/systemd/user/brigade-grokbot-operator.service token=not-a-real-token"

    def fake_run(argv, *args, **kwargs):
        return subprocess.CompletedProcess(list(argv), 0, stdout=leak, stderr=leak)

    monkeypatch.setattr(subprocess, "run", fake_run)
    check = grokbot_ops.inspect_service_result("operator")
    rendered = json.dumps(check)
    assert check == {"check": "service-result", "status": "fail"}
    assert leak not in rendered
    assert "not-a-real-token" not in rendered
    assert "/srv/example/secret" not in rendered


_SYSTEMD_MUTATION_VERBS = frozenset(
    {"start", "stop", "restart", "enable", "disable", "reload", "daemon-reload", "isolate", "kill"}
)


def _record_systemctl(monkeypatch, *, stdout="success\n", returncode=0, error: BaseException | None = None):
    recorded: list[dict[str, object]] = []

    def fake_run(argv, *args, **kwargs):
        recorded.append({"argv": list(argv), "kwargs": dict(kwargs)})
        if error is not None:
            raise error
        return subprocess.CompletedProcess(list(argv), returncode, stdout=stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return recorded


CONNECTOR_PACK_IDS = ("fleet-steward", "obsidian-operator", "operations-relay")


@pytest.mark.parametrize("pack_id", CONNECTOR_PACK_IDS)
def test_legacy_connector_pack_names_remain_exact(tmp_path, pack_id):
    service = f"brigade-grokbot-{pack_id}.service"
    assert grokbot_ops.unit_name(pack_id) == service
    assert grokbot_ops.unit_name(pack_id, None) == service
    assert grokbot_ops.config_path(tmp_path, pack_id) == tmp_path / ".brigade" / "grokbot" / f"{pack_id}.json"
    assert grokbot_ops.config_path(tmp_path, pack_id, None) == tmp_path / ".brigade" / "grokbot" / f"{pack_id}.json"
    assert grokbot_ops.service_result_argv(pack_id) == [
        "systemctl",
        "--user",
        "show",
        service,
        "--property=Result",
        "--value",
    ]


@pytest.mark.parametrize("pack_id", CONNECTOR_PACK_IDS)
def test_named_connector_pack_names_are_refused(tmp_path, pack_id):
    with pytest.raises(grokbot_mcp.ConfigurationError):
        grokbot_ops.unit_name(pack_id, "alpha")
    with pytest.raises(grokbot_mcp.ConfigurationError):
        grokbot_ops.config_path(tmp_path, pack_id, "alpha")
    with pytest.raises(grokbot_mcp.ConfigurationError):
        grokbot_ops.service_result_argv(pack_id, "alpha")


@pytest.mark.parametrize("role", ["operator", "implementation-worker", "repository-scout"])
def test_named_queue_role_service_result_uses_client_unit(role):
    assert grokbot_ops.service_result_argv(role, "alpha") == [
        "systemctl",
        "--user",
        "show",
        f"brigade-grokbot-{role}-alpha.service",
        "--property=Result",
        "--value",
    ]


@pytest.mark.parametrize(
    "client_id", ["", "../alpha", "alpha/beta", ".", "Alpha", "alpha beta", "alpha\n", "a" * 65, 1]
)
def test_invalid_client_names_never_reach_paths(tmp_path, client_id):
    with pytest.raises(grokbot_mcp.ConfigurationError):
        grokbot_ops.config_path(tmp_path, "implementation-worker", client_id)
    with pytest.raises(grokbot_mcp.ConfigurationError):
        grokbot_ops.unit_name("implementation-worker", client_id)
    assert not (tmp_path / grokbot_ops.CONFIG_DIR).exists()
