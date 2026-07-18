"""Tests for the issue 258 portable harness conformance probe."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
FIXTURES_DIR = ROOT / "docs" / "research" / "fixtures" / "harness-contract.v1"
SCHEMA_PATH = ROOT / "docs" / "proposals" / "harness-contract.v1.schema.json"
PROBE_PATH = TOOLS / "harness_conformance_probe.py"


def _probe_module():
    if str(TOOLS) not in sys.path:
        sys.path.insert(0, str(TOOLS))
    spec = importlib.util.spec_from_file_location("harness_conformance_probe", PROBE_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def probe():
    return _probe_module()


@pytest.fixture
def schema(probe):
    return probe.load_schema(SCHEMA_PATH)


@pytest.fixture
def fixtures(probe):
    return probe.load_fixtures(FIXTURES_DIR)


def test_all_checked_in_fixtures_validate_against_shipped_schema(probe, schema, fixtures) -> None:
    assert {fixture["harness"]["id"] for fixture in fixtures} == {
        "antigravity",
        "claude-code",
        "codex-cli",
        "codex-desktop",
        "cursor-cli",
        "cursor-gui",
        "grok-cli",
        "hermes",
        "opencode",
        "openclaw",
        "pi",
    }
    for fixture in fixtures:
        assert probe.validate_fixture(fixture, schema) == []


def test_schema_requires_implementation_layers_and_exact_capability_count(schema) -> None:
    assert schema["$schema"] == "http://json-schema.org/draft-07/schema#"
    capabilities = schema["properties"]["capabilities"]
    assert capabilities["minItems"] == 11
    assert capabilities["maxItems"] == 11
    cell = capabilities["items"]
    assert "implementation_layers" in cell["required"]
    assert "implementation" not in cell["properties"]
    assert schema["properties"]["deep_probes"]["additionalProperties"] is False


def test_duplicate_capability_ids_fail_schema_validation(probe, schema, fixtures) -> None:
    fixture = json.loads(json.dumps(fixtures[0]))
    fixture["capabilities"][1]["id"] = fixture["capabilities"][0]["id"]
    errors = probe.validate_fixture(fixture, schema)
    assert any("duplicate capability ids" in error for error in errors)


def test_extra_capability_cell_fails_schema_validation(probe, schema, fixtures) -> None:
    fixture = json.loads(json.dumps(fixtures[0]))
    fixture["capabilities"].append(
        {
            "id": "instructions",
            "claim": "duplicate row",
            "provenance": "unknown",
            "support_state": "unsupported",
            "implementation_layers": ["unknown"],
            "evidence": [{"kind": "probe_receipt", "reference": "extra"}],
            "tested_version": None,
            "platform": "linux",
            "scope": "extra",
        }
    )
    errors = probe.validate_fixture(fixture, schema)
    assert errors


def test_additional_top_level_property_fails_schema_validation(probe, schema, fixtures) -> None:
    fixture = json.loads(json.dumps(fixtures[0]))
    fixture["unexpected"] = True
    errors = probe.validate_fixture(fixture, schema)
    assert any("additional property" in error for error in errors)


def test_redact_text_removes_assignment_secrets_authorization_and_bearer(probe) -> None:
    sample = "\n".join(
        [
            "token=abc123",
            "Authorization: Bearer abc123",
            "Bearer abc123",
            "/home/example/.config",
            '{"token": "super-secret"}',
        ]
    )
    result = probe.redact_text(sample, "/home/example")
    assert "abc123" not in result
    assert "super-secret" not in result
    assert "/home/example" not in result
    assert "[REDACTED]" in result
    assert "Bearer [REDACTED]" in result
    assert '"token": "[REDACTED]"' in result


def test_redact_text_removes_real_and_temporary_home_segments(probe, monkeypatch, tmp_path: Path) -> None:
    real_home = str(tmp_path / "real-home")
    temp_home = str(tmp_path / "temp-home")
    monkeypatch.setenv("HOME", real_home)
    sample = f"config={real_home}/.config\nsandbox={temp_home}/probe"
    result = probe.redact_text(sample, temp_home)
    assert real_home not in result
    assert temp_home not in result
    assert result.count("[HOME]") == 2


def test_redact_text_removes_inherited_path_home_segments(probe, monkeypatch, tmp_path: Path) -> None:
    home_segment = str(tmp_path / "operator" / "bin")
    monkeypatch.setenv("HOME", str(tmp_path / "operator"))
    monkeypatch.setenv("PATH", f"{home_segment}{os.pathsep}/usr/bin")
    result = probe.redact_text(f"PATH includes {home_segment}", None)
    assert home_segment not in result
    assert "[PATH]" in result


def test_availability_json_never_emits_resolved_absolute_paths(probe, schema) -> None:
    fixture = {
        "schema": "harness-contract.v1",
        "harness": {"id": "path-cli", "surface": "cli"},
        "binary": {"command": "python3", "version_args": ["--version"]},
        "capabilities": _minimal_capabilities(),
        "deep_probes": _minimal_deep_probes(),
    }
    with mock.patch.object(probe.shutil, "which", return_value="/usr/bin/python3"):
        result = probe.probe_fixture(fixture, schema, run_version=False, timeout_seconds=1.0)
    availability = result["availability"]
    assert "resolved" not in availability
    assert availability["command_available"]["python3"] is True
    serialized = json.dumps(availability)
    assert "/usr/bin/python3" not in serialized


def test_minimal_environment_uses_executable_parent_and_platform_defaults(probe, tmp_path: Path) -> None:
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    environment = probe._minimal_environment(home_dir, "/opt/tools/example-cli")
    path_parts = environment["PATH"].split(os.pathsep)
    assert path_parts[0] == "/opt/tools"
    assert "/usr/bin" in path_parts
    assert "/bin" in path_parts
    assert str(tmp_path / "operator") not in environment["PATH"]


def test_windows_process_spawn_uses_new_process_group(probe) -> None:
    with mock.patch.object(probe.os, "name", "nt"):
        with mock.patch.object(probe.subprocess, "Popen") as popen:
            popen.return_value.stdout = mock.Mock()
            popen.return_value.stderr = mock.Mock()
            probe._popen_probe_process(
                ["example-cli", "--version"],
                cwd="/tmp",
                env={"PATH": "/bin"},
            )
    _, kwargs = popen.call_args
    assert kwargs["creationflags"] == 0x00000200
    assert "start_new_session" not in kwargs


def test_windows_terminate_process_group_uses_taskkill_before_kill(probe) -> None:
    process = mock.Mock()
    process.poll.side_effect = [None, None, 1]
    process.pid = 4242
    with mock.patch.object(probe.os, "name", "nt"):
        with mock.patch.object(probe.subprocess, "run") as run:
            with mock.patch.object(process, "kill") as kill:
                probe._terminate_process_group(process)
    run.assert_called_once_with(
        ["taskkill", "/PID", "4242", "/T", "/F"],
        stdin=probe.subprocess.DEVNULL,
        stdout=probe.subprocess.DEVNULL,
        stderr=probe.subprocess.DEVNULL,
        check=False,
    )
    kill.assert_called_once_with()


def test_run_version_probe_cleans_up_process_tree_on_post_spawn_exception(probe, schema) -> None:
    fixture = {
        "schema": "harness-contract.v1",
        "harness": {"id": "boom-cli", "surface": "cli"},
        "binary": {"command": "python3", "version_args": ["--version"]},
        "capabilities": _minimal_capabilities(),
        "deep_probes": _minimal_deep_probes(),
    }
    process = mock.Mock()
    process.poll.return_value = None
    process.pid = 5150
    with (
        mock.patch.object(probe.shutil, "which", return_value="/usr/bin/python3"),
        mock.patch.object(probe, "_popen_probe_process", return_value=process),
        mock.patch.object(probe, "_collect_bounded_output", side_effect=RuntimeError("boom")),
        mock.patch.object(probe, "_terminate_process_group") as terminate,
    ):
        with pytest.raises(RuntimeError, match="boom"):
            probe.run_version_probe(fixture, timeout_seconds=1.0)
    terminate.assert_called_once_with(process)
    process.wait.assert_called_once_with(timeout=1)


def test_positive_finite_timeout_is_required(probe, schema) -> None:
    fixture = {
        "schema": "harness-contract.v1",
        "harness": {"id": "timeout-cli", "surface": "cli"},
        "binary": {"command": "python3", "version_args": ["--version"]},
        "capabilities": _minimal_capabilities(),
        "deep_probes": _minimal_deep_probes(),
    }
    with pytest.raises(ValueError, match="finite positive"):
        probe.probe_fixture(fixture, schema, run_version=False, timeout_seconds=0)


def test_invalid_fixture_is_not_executable(probe, schema) -> None:
    fixture = {
        "schema": "harness-contract.v1",
        "harness": {"id": "unsafe-cli", "surface": "cli"},
        "binary": {"command": "/bin/sh", "version_args": ["--version"]},
        "capabilities": [],
        "deep_probes": {},
    }
    result = probe.probe_fixture(fixture, schema, run_version=True, timeout_seconds=1.0)
    assert result["validation_errors"]
    assert result["availability"]["state"] == "not_executable"
    assert result["version_probe"]["state"] == "not_executable"


def test_missing_binary_is_reported_as_external_blocker(probe, schema) -> None:
    fixture = {
        "schema": "harness-contract.v1",
        "harness": {"id": "missing-cli", "surface": "cli"},
        "binary": {"command": "not-a-real-harness-binary", "version_args": ["--version"]},
        "capabilities": _minimal_capabilities(),
        "deep_probes": _minimal_deep_probes(),
    }
    result = probe.probe_fixture(fixture, schema, run_version=True, timeout_seconds=1.0)
    assert result["availability"]["state"] == "externally_blocked"
    assert result["availability"]["reason"] == "binary_not_found"
    assert result["version_probe"]["state"] == "externally_blocked"
    assert result["version_probe"]["reason"] == "binary_not_found"


def test_unsafe_version_args_are_blocked_without_execution(probe, schema) -> None:
    fixture = {
        "schema": "harness-contract.v1",
        "harness": {"id": "unsafe-args", "surface": "cli"},
        "binary": {"command": "python3", "version_args": ["-c", "print('x')"]},
        "capabilities": _minimal_capabilities(),
        "deep_probes": _minimal_deep_probes(),
    }
    errors = probe.validate_fixture(fixture, schema)
    assert errors
    result = probe.probe_fixture(fixture, schema, run_version=True, timeout_seconds=1.0)
    assert result["version_probe"]["state"] == "not_executable"


def test_nonzero_exit_is_distinct_from_externally_blocked(probe, schema) -> None:
    fixture = {
        "schema": "harness-contract.v1",
        "harness": {"id": "nonzero-cli", "surface": "cli"},
        "binary": {"command": "false", "version_args": ["--version"]},
        "capabilities": _minimal_capabilities(),
        "deep_probes": _minimal_deep_probes(),
    }
    with mock.patch.object(probe.shutil, "which", return_value="/bin/false"):
        result = probe.probe_fixture(fixture, schema, run_version=True, timeout_seconds=1.0)
    assert result["version_probe"]["state"] == "nonzero_exit"
    assert result["version_probe"]["exit_code"] != 0


def test_timeout_is_reported_as_external_blocker(probe, schema) -> None:
    fixture = {
        "schema": "harness-contract.v1",
        "harness": {"id": "slow-cli", "surface": "cli"},
        "binary": {"command": "sleep", "version_args": ["--version"]},
        "capabilities": _minimal_capabilities(),
        "deep_probes": _minimal_deep_probes(),
    }

    with (
        mock.patch.object(probe.shutil, "which", return_value="/bin/sleep"),
        mock.patch.object(
            probe,
            "_collect_bounded_output",
            return_value=(b"", False, "TimeoutExpired"),
        ),
        mock.patch.object(probe.subprocess, "Popen") as popen,
    ):
        popen.return_value.poll.return_value = None
        popen.return_value.wait.return_value = None
        popen.return_value.pid = 4242
        result = probe.probe_fixture(fixture, schema, run_version=True, timeout_seconds=0.01)
    assert result["version_probe"]["state"] == "externally_blocked"
    assert result["version_probe"]["reason"] == "TimeoutExpired"


def test_output_overflow_terminates_process_group(probe, schema) -> None:
    fixture = {
        "schema": "harness-contract.v1",
        "harness": {"id": "chatty-cli", "surface": "cli"},
        "binary": {"command": "chatty", "version_args": ["--version"]},
        "capabilities": _minimal_capabilities(),
        "deep_probes": _minimal_deep_probes(),
    }

    with (
        mock.patch.object(probe.shutil, "which", return_value="/bin/chatty"),
        mock.patch.object(
            probe,
            "_collect_bounded_output",
            return_value=(b"x" * probe.OUTPUT_CAP_BYTES, True, "output_overflow"),
        ),
        mock.patch.object(probe.subprocess, "Popen") as popen,
    ):
        popen.return_value.poll.return_value = 0
        popen.return_value.wait.return_value = 0
        popen.return_value.pid = 9001
        result = probe.probe_fixture(fixture, schema, run_version=True, timeout_seconds=1.0)
    assert result["version_probe"]["state"] == "externally_blocked"
    assert result["version_probe"]["reason"] == "output_overflow"


def test_collect_bounded_output_kills_process_group_on_overflow(probe, tmp_path: Path) -> None:
    helper = tmp_path / "chatty-version"
    helper.write_text("#!/usr/bin/env python3\nimport sys\nsys.stdout.write('x' * 100000)\n")
    helper.chmod(0o755)

    process = subprocess.Popen(
        [str(helper), "--version"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    with mock.patch.object(probe, "_terminate_process_group") as terminate:
        output, overflow, reason = probe._collect_bounded_output(
            process,
            cap_bytes=probe.OUTPUT_CAP_BYTES,
            timeout_seconds=2.0,
        )
    assert overflow is True
    assert reason == "output_overflow"
    assert len(output) == probe.OUTPUT_CAP_BYTES
    terminate.assert_called()


def test_default_mode_skips_version_execution(probe, schema, fixtures) -> None:
    result = probe.probe_fixture(fixtures[0], schema, run_version=False, timeout_seconds=1.0)
    assert result["version_probe"]["state"] == "skipped"
    assert result["availability"]["state"] in {"available", "externally_blocked", "external_only"}


def test_desktop_fixture_is_external_only(probe, schema, fixtures) -> None:
    for harness_id in ("codex-desktop", "cursor-gui"):
        fixture = next(item for item in fixtures if item["harness"]["id"] == harness_id)
        result = probe.probe_fixture(fixture, schema, run_version=True, timeout_seconds=1.0)
        assert result["availability"]["state"] == "external_only"
        assert result["version_probe"]["state"] == "external_only"


def test_antigravity_is_gui_surface_with_availability_only_candidates(probe, schema, fixtures) -> None:
    fixture = next(item for item in fixtures if item["harness"]["id"] == "antigravity")
    assert fixture["harness"]["surface"] == "gui"
    with mock.patch.object(probe.shutil, "which", side_effect=lambda name: None):
        result = probe.probe_fixture(fixture, schema, run_version=False, timeout_seconds=1.0)
    assert result["availability"]["commands"] == ["agy", "antigravity"]
    assert result["availability"]["state"] == "externally_blocked"
    assert result["version_probe"]["state"] == "skipped"


def test_deep_probes_are_returned_not_executed(probe, schema, fixtures) -> None:
    result = probe.probe_fixture(fixtures[0], schema, run_version=False, timeout_seconds=1.0)
    assert result["deep_probes"] == fixtures[0]["deep_probes"]
    assert set(result["deep_probes"]) == {
        "instruction",
        "skill",
        "hook",
        "mcp",
        "workspace",
        "session",
        "verification",
        "handoff",
        "reload",
        "telemetry",
        "platform",
    }


def test_probe_script_is_tracked_under_tools() -> None:
    assert PROBE_PATH.is_file()
    completed = subprocess.run(
        [
            sys.executable,
            str(PROBE_PATH),
            "--fixtures-dir",
            str(FIXTURES_DIR),
            "--schema",
            str(SCHEMA_PATH),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0
    payload = json.loads(completed.stdout)
    assert payload["deep_probe_policy"] == "declared_only_not_executed"
    assert len(payload["results"]) == 11


def _minimal_capabilities() -> list[dict[str, object]]:
    return [
        {
            "id": capability_id,
            "claim": "test",
            "provenance": "unknown",
            "support_state": "unsupported",
            "implementation_layers": ["unknown"],
            "evidence": [{"kind": "probe_receipt", "reference": "test"}],
            "tested_version": None,
            "platform": "linux",
            "scope": "test",
        }
        for capability_id in [
            "instructions",
            "skills",
            "hooks",
            "mcp",
            "workspace",
            "session",
            "verification",
            "handoff",
            "reload",
            "telemetry",
            "platform",
        ]
    ]


def _minimal_deep_probes() -> dict[str, str]:
    return {
        "instruction": "declared",
        "skill": "declared",
        "hook": "declared",
        "mcp": "declared",
        "workspace": "declared",
        "session": "declared",
        "verification": "declared",
        "handoff": "declared",
        "reload": "declared",
        "telemetry": "declared",
        "platform": "declared",
    }
