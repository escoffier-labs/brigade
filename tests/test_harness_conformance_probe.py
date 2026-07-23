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


def _fake_version_process(exit_code: int = 0) -> mock.Mock:
    process = mock.Mock()
    process.poll.return_value = exit_code
    process.pid = 6161
    return process


def test_hermes_version_probe_records_version_and_platform_receipt(probe, schema, fixtures) -> None:
    fixture = next(item for item in fixtures if item["harness"]["id"] == "hermes")
    with (
        mock.patch.object(probe.shutil, "which", return_value="/fake/bin/hermes"),
        mock.patch.object(probe, "_popen_probe_process", return_value=_fake_version_process()),
        mock.patch.object(
            probe,
            "_collect_bounded_output",
            return_value=(b"hermes 0.3.1\n", False, None),
        ),
    ):
        result = probe.probe_fixture(fixture, schema, run_version=True, timeout_seconds=1.0)
    version_probe = result["version_probe"]
    assert version_probe["state"] == "observed"
    assert version_probe["command"] == "hermes"
    assert version_probe["version"] == "hermes 0.3.1"
    assert version_probe["platform"] == sys.platform
    assert "hermes 0.3.1" in version_probe["output"]


def test_hermes_missing_binary_stays_externally_blocked_binary_not_found(probe, schema, fixtures) -> None:
    fixture = next(item for item in fixtures if item["harness"]["id"] == "hermes")
    with mock.patch.object(probe.shutil, "which", return_value=None):
        result = probe.probe_fixture(fixture, schema, run_version=True, timeout_seconds=1.0)
    assert result["availability"]["state"] == "externally_blocked"
    assert result["availability"]["reason"] == "binary_not_found"
    assert result["version_probe"]["state"] == "externally_blocked"
    assert result["version_probe"]["reason"] == "binary_not_found"


def test_hermes_home_directory_only_never_counts_as_runtime_conformance(
    probe, schema, fixtures, monkeypatch, tmp_path: Path
) -> None:
    fixture = next(item for item in fixtures if item["harness"]["id"] == "hermes")
    home = tmp_path / "home"
    (home / ".hermes").mkdir(parents=True)
    (home / ".config" / "hermes").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    with mock.patch.object(probe.shutil, "which", return_value=None):
        result = probe.probe_fixture(fixture, schema, run_version=True, timeout_seconds=1.0)
    for section in (result["availability"], result["version_probe"]):
        assert section["state"] == "externally_blocked"
        assert section["reason"] == "binary_not_found"


def test_antigravity_version_probe_records_version_and_platform_receipt(probe, schema, fixtures) -> None:
    fixture = next(item for item in fixtures if item["harness"]["id"] == "antigravity")
    assert fixture["binary"] == {"command": "agy", "version_args": ["--version"]}
    with (
        mock.patch.object(
            probe.shutil,
            "which",
            side_effect=lambda name: "/fake/bin/agy" if name == "agy" else None,
        ),
        mock.patch.object(probe, "_popen_probe_process", return_value=_fake_version_process()),
        mock.patch.object(
            probe,
            "_collect_bounded_output",
            return_value=(b"agy 1.4.0\n", False, None),
        ),
    ):
        result = probe.probe_fixture(fixture, schema, run_version=True, timeout_seconds=1.0)
    version_probe = result["version_probe"]
    assert version_probe["state"] == "observed"
    assert version_probe["command"] == "agy"
    assert version_probe["version"] == "agy 1.4.0"
    assert version_probe["platform"] == sys.platform
    assert "agy 1.4.0" in version_probe["output"]


def test_antigravity_version_probe_falls_back_to_declared_availability_candidates(probe, schema, fixtures) -> None:
    # Greptile P1 on PR 450: an install with only the `antigravity` launcher
    # must not report availability "available" alongside a blocked version
    # probe; the probe resolves whichever declared candidate is present.
    fixture = next(item for item in fixtures if item["harness"]["id"] == "antigravity")
    with (
        mock.patch.object(
            probe.shutil,
            "which",
            side_effect=lambda name: "/fake/bin/antigravity" if name == "antigravity" else None,
        ),
        mock.patch.object(probe, "_popen_probe_process", return_value=_fake_version_process()),
        mock.patch.object(
            probe,
            "_collect_bounded_output",
            return_value=(b"antigravity 2.0.1\n", False, None),
        ),
    ):
        result = probe.probe_fixture(fixture, schema, run_version=True, timeout_seconds=1.0)
    assert result["availability"]["state"] == "available"
    version_probe = result["version_probe"]
    assert version_probe["state"] == "observed"
    assert version_probe["command"] == "antigravity"
    assert version_probe["version"] == "antigravity 2.0.1"
    assert version_probe["platform"] == sys.platform


def test_version_receipt_skips_banner_lines_without_version_numbers(probe, schema, fixtures) -> None:
    # Greptile P2 on PR 450: a warning or banner printed before the version
    # must not be reported as the version evidence.
    fixture = next(item for item in fixtures if item["harness"]["id"] == "hermes")
    bannered_output = b"WARNING: deprecated config key detected\nBuild channel: stable\nhermes 0.3.1\n"
    with (
        mock.patch.object(probe.shutil, "which", return_value="/fake/bin/hermes"),
        mock.patch.object(probe, "_popen_probe_process", return_value=_fake_version_process()),
        mock.patch.object(
            probe,
            "_collect_bounded_output",
            return_value=(bannered_output, False, None),
        ),
    ):
        result = probe.probe_fixture(fixture, schema, run_version=True, timeout_seconds=1.0)
    version_probe = result["version_probe"]
    assert version_probe["state"] == "observed"
    assert version_probe["version"] == "hermes 0.3.1"


def test_version_receipt_reports_none_when_no_version_line_present(probe, schema, fixtures) -> None:
    fixture = next(item for item in fixtures if item["harness"]["id"] == "hermes")
    with (
        mock.patch.object(probe.shutil, "which", return_value="/fake/bin/hermes"),
        mock.patch.object(probe, "_popen_probe_process", return_value=_fake_version_process()),
        mock.patch.object(
            probe,
            "_collect_bounded_output",
            return_value=(b"build channel: stable\n", False, None),
        ),
    ):
        result = probe.probe_fixture(fixture, schema, run_version=True, timeout_seconds=1.0)
    version_probe = result["version_probe"]
    assert version_probe["state"] == "observed"
    assert version_probe["version"] is None


def test_antigravity_missing_binary_stays_externally_blocked_binary_not_found(probe, schema, fixtures) -> None:
    fixture = next(item for item in fixtures if item["harness"]["id"] == "antigravity")
    with mock.patch.object(probe.shutil, "which", return_value=None):
        result = probe.probe_fixture(fixture, schema, run_version=True, timeout_seconds=1.0)
    assert result["availability"]["state"] == "externally_blocked"
    assert result["availability"]["reason"] == "binary_not_found"
    assert result["version_probe"]["state"] == "externally_blocked"
    assert result["version_probe"]["reason"] == "binary_not_found"


def test_antigravity_home_directory_only_never_counts_as_runtime_conformance(
    probe, schema, fixtures, monkeypatch, tmp_path: Path
) -> None:
    fixture = next(item for item in fixtures if item["harness"]["id"] == "antigravity")
    home = tmp_path / "home"
    (home / ".antigravity").mkdir(parents=True)
    (home / ".config" / "antigravity").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    with mock.patch.object(probe.shutil, "which", return_value=None):
        result = probe.probe_fixture(fixture, schema, run_version=True, timeout_seconds=1.0)
    for section in (result["availability"], result["version_probe"]):
        assert section["state"] == "externally_blocked"
        assert section["reason"] == "binary_not_found"


def test_antigravity_version_args_gate_still_blocks_unsafe_args(probe, fixtures) -> None:
    fixture = json.loads(json.dumps(next(item for item in fixtures if item["harness"]["id"] == "antigravity")))
    fixture["binary"]["version_args"] = ["--full"]
    with mock.patch.object(probe, "_popen_probe_process") as popen:
        result = probe.run_version_probe(fixture, timeout_seconds=1.0)
    assert result["state"] == "externally_blocked"
    assert result["reason"] == "unsafe_version_arguments"
    popen.assert_not_called()


def test_gui_surface_without_binary_declaration_remains_version_limited(probe, schema) -> None:
    fixture = {
        "schema": "harness-contract.v1",
        "harness": {"id": "gui-without-binary", "surface": "gui"},
        "availability": {"command_candidates": ["example-gui"]},
        "capabilities": _minimal_capabilities(),
        "deep_probes": _minimal_deep_probes(),
    }
    with mock.patch.object(probe, "_popen_probe_process") as popen:
        result = probe.probe_fixture(fixture, schema, run_version=True, timeout_seconds=1.0)
    assert result["version_probe"]["state"] == "externally_blocked"
    assert result["version_probe"]["reason"] == "version_execution_limited_to_cli_surface"
    popen.assert_not_called()


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
    assert "deep_probe_receipts" not in result


def test_schema_accepts_inline_discovery_spec(probe, schema, fixtures) -> None:
    fixture = next(item for item in fixtures if item["harness"]["id"] == "codex-cli")
    assert probe.validate_fixture(fixture, schema) == []
    assert fixture["deep_probes"]["instruction"]["discovery"] == {
        "command": "codex",
        "args": ["--help"],
    }


def test_run_deep_probes_default_policy_stays_declared_only_not_executed(probe, schema, fixtures) -> None:
    result = probe.probe_fixture(fixtures[0], schema, run_version=False, run_deep_probes=False, timeout_seconds=1.0)
    assert "deep_probe_receipts" not in result


def test_run_deep_probes_emits_one_receipt_per_declared_probe(probe, schema) -> None:
    fixture = _fixture_with_deep_probes(
        harness_id="receipt-count-cli",
        deep_probes=_deep_probes_with_instruction_discovery(),
    )
    with (
        mock.patch.object(probe.shutil, "which", return_value="/fake/bin/codex"),
        mock.patch.object(probe, "_popen_probe_process", return_value=_fake_version_process()),
        mock.patch.object(
            probe,
            "_collect_bounded_output",
            return_value=(b"codex help output\n", False, None),
        ),
    ):
        result = probe.probe_fixture(fixture, schema, run_version=False, run_deep_probes=True, timeout_seconds=1.0)
    receipts = result["deep_probe_receipts"]
    assert set(receipts) == set(_minimal_deep_probes())
    assert len(receipts) == 11


def test_run_deep_probes_declared_only_without_discovery_spec(probe, schema) -> None:
    fixture = _fixture_with_deep_probes(harness_id="declared-only-cli", deep_probes=_minimal_deep_probes())
    result = probe.probe_fixture(fixture, schema, run_version=False, run_deep_probes=True, timeout_seconds=1.0)
    receipt = result["deep_probe_receipts"]["skill"]
    assert receipt["probe_id"] == "skill"
    assert receipt["state"] == "declared_only"
    assert receipt["reason"] == "no_discovery_spec"
    assert receipt["platform"] == sys.platform


def test_run_deep_probes_executes_safe_discovery_spec(probe, schema, monkeypatch, tmp_path: Path) -> None:
    fixture = _fixture_with_deep_probes(
        harness_id="discovery-cli",
        deep_probes=_deep_probes_with_instruction_discovery(),
    )
    real_home = tmp_path / "real-home"
    real_home.mkdir()
    monkeypatch.setenv("HOME", str(real_home))
    monkeypatch.setenv("USERPROFILE", str(real_home))
    process = _fake_version_process()
    with (
        mock.patch.object(probe.shutil, "which", return_value="/fake/bin/codex"),
        mock.patch.object(probe, "_popen_probe_process", return_value=process) as popen,
        mock.patch.object(
            probe,
            "_collect_bounded_output",
            return_value=(
                (
                    f"codex help output\nhome={real_home}\n"
                    "credential=fake-credential\n"
                    "AWS_SECRET_ACCESS_KEY=fake-access-key\n"
                    "private_key=fake-private-key\n"
                    '{"AWS_SECRET_ACCESS_KEY":"fake-json-access-key",'
                    '"apiKey":"fake-json-api-key",'
                    '"private_key":"fake-escaped-\\"private-key"}\n'
                ).encode(),
                False,
                None,
            ),
        ),
    ):
        result = probe.probe_fixture(fixture, schema, run_version=False, run_deep_probes=True, timeout_seconds=1.0)
    popen.assert_called_once()
    argv = popen.call_args[0][0]
    kwargs = popen.call_args[1]
    assert argv == ["/fake/bin/codex", "--help"]
    sandbox_root = Path(kwargs["cwd"])
    environment = kwargs["env"]
    for variable in ("HOME", "USERPROFILE", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME"):
        assert Path(environment[variable]).is_relative_to(sandbox_root)
        assert str(real_home) not in environment[variable]
    receipt = result["deep_probe_receipts"]["instruction"]
    assert receipt["state"] == "observed"
    assert receipt["probe_id"] == "instruction"
    assert receipt["command"] == "codex"
    assert receipt["exit_code"] == 0
    assert receipt["platform"] == sys.platform
    assert "codex help output" in receipt["output"]
    assert str(real_home) not in receipt["output"]
    assert "fake-credential" not in receipt["output"]
    assert "fake-access-key" not in receipt["output"]
    assert "fake-private-key" not in receipt["output"]
    assert "fake-json-access-key" not in receipt["output"]
    assert "fake-json-api-key" not in receipt["output"]
    assert "fake-escaped" not in receipt["output"]
    assert receipt["output"].count("[REDACTED]") == 6
    assert ('{"AWS_SECRET_ACCESS_KEY":"[REDACTED]","apiKey":"[REDACTED]","private_key":"[REDACTED]"}\n') in receipt[
        "output"
    ]


def test_run_deep_probes_blocked_fixture_stays_declared_only(probe, schema) -> None:
    fixture = _fixture_with_deep_probes(
        harness_id="blocked-cli",
        deep_probes=_deep_probes_with_instruction_discovery(),
        command="missing-binary",
    )
    with (
        mock.patch.object(probe.shutil, "which", return_value=None),
        mock.patch.object(probe, "_popen_probe_process") as popen,
    ):
        result = probe.probe_fixture(fixture, schema, run_version=False, run_deep_probes=True, timeout_seconds=1.0)
    popen.assert_not_called()
    assert result["availability"]["state"] == "externally_blocked"
    receipt = result["deep_probe_receipts"]["instruction"]
    assert receipt["state"] == "declared_only"
    assert receipt["reason"] == "externally_blocked_fixture"


def test_run_deep_probes_invalid_fixture_emits_declared_only_receipts_without_launch(probe, schema) -> None:
    fixture = _fixture_with_deep_probes(
        harness_id="invalid-cli",
        deep_probes=_deep_probes_with_instruction_discovery(),
    )
    fixture["unexpected"] = True
    with mock.patch.object(probe, "_popen_probe_process") as popen:
        result = probe.probe_fixture(fixture, schema, run_version=False, run_deep_probes=True, timeout_seconds=1.0)
    popen.assert_not_called()
    assert result["availability"]["state"] == "not_executable"
    receipts = result["deep_probe_receipts"]
    assert len(receipts) == len(_minimal_deep_probes())
    assert {receipt["reason"] for receipt in receipts.values()} == {"invalid_fixture"}
    assert {receipt["state"] for receipt in receipts.values()} == {"declared_only"}


def test_run_deep_probes_invalid_nonobject_declarations_emit_receipts_without_launch(probe, schema) -> None:
    fixture = _fixture_with_deep_probes(
        harness_id="invalid-deep-probes-cli",
        deep_probes=_minimal_deep_probes(),
    )
    fixture["deep_probes"] = "invalid"
    with mock.patch.object(probe, "_popen_probe_process") as popen:
        result = probe.probe_fixture(fixture, schema, run_version=False, run_deep_probes=True, timeout_seconds=1.0)
    popen.assert_not_called()
    assert result["availability"]["state"] == "not_executable"
    receipts = result["deep_probe_receipts"]
    assert len(receipts) == len(_minimal_deep_probes())
    assert {receipt["reason"] for receipt in receipts.values()} == {"invalid_fixture"}
    assert {receipt["state"] for receipt in receipts.values()} == {"declared_only"}


def test_run_deep_probes_refuses_unsafe_command_before_launch(probe, schema) -> None:
    fixture = _fixture_with_deep_probes(
        harness_id="unsafe-command-cli",
        deep_probes={
            **_minimal_deep_probes(),
            "instruction": {
                "state": "declared",
                "discovery": {"command": "/bin/sh", "args": ["--help"]},
            },
        },
    )
    with mock.patch.object(probe, "_popen_probe_process") as popen:
        result = probe.probe_fixture(fixture, schema, run_version=False, run_deep_probes=True, timeout_seconds=1.0)
    popen.assert_not_called()
    receipt = result["deep_probe_receipts"]["instruction"]
    assert receipt["state"] == "refused"
    assert receipt["reason"] == "unsafe_command_name"


def test_run_deep_probes_refuses_unsafe_args_before_launch(probe, schema) -> None:
    fixture = _fixture_with_deep_probes(
        harness_id="unsafe-args-cli",
        deep_probes={
            **_minimal_deep_probes(),
            "instruction": {
                "state": "declared",
                "discovery": {"command": "codex", "args": ["-c", "print('x')"]},
            },
        },
    )
    with mock.patch.object(probe, "_popen_probe_process") as popen:
        result = probe.probe_fixture(fixture, schema, run_version=False, run_deep_probes=True, timeout_seconds=1.0)
    popen.assert_not_called()
    receipt = result["deep_probe_receipts"]["instruction"]
    assert receipt["state"] == "refused"
    assert receipt["reason"] == "unsafe_discovery_arguments"


def test_run_deep_probes_refuses_args_outside_exact_allowlist(probe, schema) -> None:
    fixture = _fixture_with_deep_probes(
        harness_id="unallowlisted-args-cli",
        deep_probes={
            **_minimal_deep_probes(),
            "instruction": {
                "state": "declared",
                "discovery": {"command": "codex", "args": ["config", "--help"]},
            },
        },
    )
    with mock.patch.object(probe, "_popen_probe_process") as popen:
        result = probe.probe_fixture(fixture, schema, run_version=False, run_deep_probes=True, timeout_seconds=1.0)
    popen.assert_not_called()
    receipt = result["deep_probe_receipts"]["instruction"]
    assert receipt["state"] == "refused"
    assert receipt["reason"] == "unsafe_discovery_arguments"


def test_run_deep_probes_nonzero_exit_preserves_bounded_output(probe, schema, monkeypatch, tmp_path: Path) -> None:
    fixture = _fixture_with_deep_probes(
        harness_id="nonzero-discovery-cli",
        deep_probes=_deep_probes_with_instruction_discovery(),
    )
    real_home = tmp_path / "real-home"
    real_home.mkdir()
    monkeypatch.setenv("HOME", str(real_home))
    process = _fake_version_process(exit_code=2)
    with (
        mock.patch.object(probe.shutil, "which", return_value="/fake/bin/codex"),
        mock.patch.object(probe, "_popen_probe_process", return_value=process),
        mock.patch.object(
            probe,
            "_collect_bounded_output",
            return_value=(
                (f'token=abc123\nconfig={real_home}/.config/codex\n{{"apiKey":"fake-nonzero-json-key"}}\n').encode(),
                False,
                None,
            ),
        ),
    ):
        result = probe.probe_fixture(fixture, schema, run_version=False, run_deep_probes=True, timeout_seconds=1.0)
    receipt = result["deep_probe_receipts"]["instruction"]
    assert receipt["state"] == "nonzero_exit"
    assert receipt["reason"] == "nonzero_exit"
    assert receipt["exit_code"] == 2
    assert "abc123" not in receipt["output"]
    assert "fake-nonzero-json-key" not in receipt["output"]
    assert str(real_home) not in receipt["output"]
    assert "[REDACTED]" in receipt["output"]
    assert "[HOME]/.config/codex" in receipt["output"]


def test_run_deep_probes_timeout_preserves_bounded_output(probe, schema) -> None:
    fixture = _fixture_with_deep_probes(
        harness_id="timeout-discovery-cli",
        deep_probes=_deep_probes_with_instruction_discovery(),
    )
    process = mock.Mock()
    process.poll.return_value = None
    process.pid = 5151
    with (
        mock.patch.object(probe.shutil, "which", return_value="/fake/bin/codex"),
        mock.patch.object(probe, "_popen_probe_process", return_value=process),
        mock.patch.object(
            probe,
            "_collect_bounded_output",
            return_value=(
                (
                    "partial output\n"
                    "credential=fake-timeout-credential\n"
                    '{"AWS_SECRET_ACCESS_KEY":"fake-timeout-json-key"}\n'
                ).encode(),
                False,
                "TimeoutExpired",
            ),
        ),
    ):
        result = probe.probe_fixture(fixture, schema, run_version=False, run_deep_probes=True, timeout_seconds=0.01)
    receipt = result["deep_probe_receipts"]["instruction"]
    assert receipt["state"] == "externally_blocked"
    assert receipt["reason"] == "TimeoutExpired"
    assert receipt["output"] == ('partial output\ncredential=[REDACTED]\n{"AWS_SECRET_ACCESS_KEY":"[REDACTED]"}\n')
    assert "fake-timeout-credential" not in receipt["output"]
    assert "fake-timeout-json-key" not in receipt["output"]


def test_run_deep_probes_output_overflow_preserves_bounded_output(probe, schema) -> None:
    fixture = _fixture_with_deep_probes(
        harness_id="overflow-discovery-cli",
        deep_probes=_deep_probes_with_instruction_discovery(),
    )
    process = mock.Mock()
    process.poll.return_value = 0
    process.pid = 5152
    json_secret_line = '{"token":"x"}\n'
    overflow_output = json_secret_line * (probe.OUTPUT_CAP_BYTES // len(json_secret_line))
    overflow_output += "x" * (probe.OUTPUT_CAP_BYTES % len(json_secret_line))
    assert len(overflow_output.encode()) == probe.OUTPUT_CAP_BYTES
    with (
        mock.patch.object(probe.shutil, "which", return_value="/fake/bin/codex"),
        mock.patch.object(probe, "_popen_probe_process", return_value=process),
        mock.patch.object(
            probe,
            "_collect_bounded_output",
            return_value=(overflow_output.encode(), True, "output_overflow"),
        ),
    ):
        result = probe.probe_fixture(fixture, schema, run_version=False, run_deep_probes=True, timeout_seconds=1.0)
    receipt = result["deep_probe_receipts"]["instruction"]
    assert receipt["state"] == "externally_blocked"
    assert receipt["reason"] == "output_overflow"
    assert json_secret_line not in receipt["output"]
    assert receipt["output"].startswith('{"token":"[REDACTED]"}\n')
    assert len(receipt["output"].encode()) <= probe.OUTPUT_CAP_BYTES


def test_run_deep_probes_output_overflow_redacts_unterminated_json_at_boundary(probe, schema) -> None:
    fixture = _fixture_with_deep_probes(
        harness_id="overflow-partial-json-cli",
        deep_probes=_deep_probes_with_instruction_discovery(),
    )
    process = mock.Mock()
    process.poll.return_value = 0
    process.pid = 5153
    partial_secret = '{"apiKey":"partial-secret'
    overflow_output = ("x" * (probe.OUTPUT_CAP_BYTES - len(partial_secret))) + partial_secret
    with (
        mock.patch.object(probe.shutil, "which", return_value="/fake/bin/codex"),
        mock.patch.object(probe, "_popen_probe_process", return_value=process),
        mock.patch.object(
            probe,
            "_collect_bounded_output",
            return_value=(overflow_output.encode(), True, "output_overflow"),
        ),
    ):
        result = probe.probe_fixture(fixture, schema, run_version=False, run_deep_probes=True, timeout_seconds=1.0)
    receipt = result["deep_probe_receipts"]["instruction"]
    assert receipt["state"] == "externally_blocked"
    assert receipt["reason"] == "output_overflow"
    assert "partial-secret" not in receipt["output"]
    assert len(receipt["output"].encode()) <= probe.OUTPUT_CAP_BYTES


def test_run_deep_probes_sandbox_routes_writes_away_from_real_home(probe, schema, monkeypatch, tmp_path: Path) -> None:
    real_home = tmp_path / "real-home"
    real_home.mkdir()
    monkeypatch.setenv("HOME", str(real_home))
    monkeypatch.setenv("USERPROFILE", str(real_home))
    helper = tmp_path / "probe-home-check"
    helper.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os\n"
        "from pathlib import Path\n"
        'target = Path(os.environ["HOME"]) / ".probe-config" / "receipt"\n'
        "target.parent.mkdir(parents=True)\n"
        'target.write_text("sandboxed")\n'
        "print(json.dumps({\n"
        '    "target": str(target),\n'
        '    "cwd": os.getcwd(),\n'
        '    "exists": target.is_file(),\n'
        "}))\n"
    )
    helper.chmod(0o755)
    fixture = _fixture_with_deep_probes(
        harness_id="sandbox-home-cli",
        deep_probes={
            **_minimal_deep_probes(),
            "instruction": {
                "state": "declared",
                "discovery": {"command": "probe-home-check", "args": ["--help"]},
            },
        },
    )
    with mock.patch.object(probe.shutil, "which", return_value=str(helper)):
        result = probe.probe_fixture(fixture, schema, run_version=False, run_deep_probes=True, timeout_seconds=2.0)
    receipt = result["deep_probe_receipts"]["instruction"]
    assert receipt["state"] == "observed"
    assert '"exists": true' in receipt["output"]
    assert "[HOME]/.probe-config/receipt" in receipt["output"]
    assert '"cwd": "[HOME]"' in receipt["output"]
    assert str(tmp_path) not in receipt["output"]
    assert list(real_home.iterdir()) == []
    assert not (real_home / ".probe-session").exists()


def test_probe_script_reports_deep_probe_policy_and_opt_in_flag(probe) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(PROBE_PATH),
            "--fixtures-dir",
            str(FIXTURES_DIR),
            "--schema",
            str(SCHEMA_PATH),
            "--run-deep-probes",
        ],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": ""},
    )
    assert completed.returncode == 0
    payload = json.loads(completed.stdout)
    assert payload["deep_probe_policy"] == "executed_when_specified"
    assert payload["run_deep_probes"] is True
    codex = next(item for item in payload["results"] if item["harness_id"] == "codex-cli")
    assert len(codex["deep_probe_receipts"]) == 11


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
    assert payload["run_deep_probes"] is False
    assert len(payload["results"]) == 11


def _fixture_with_deep_probes(
    *,
    harness_id: str,
    deep_probes: dict[str, object],
    command: str = "python3",
) -> dict[str, object]:
    return {
        "schema": "harness-contract.v1",
        "harness": {"id": harness_id, "surface": "cli"},
        "binary": {"command": command, "version_args": ["--version"]},
        "capabilities": _minimal_capabilities(),
        "deep_probes": deep_probes,
    }


def _deep_probes_with_instruction_discovery() -> dict[str, object]:
    return {
        **_minimal_deep_probes(),
        "instruction": {
            "state": "declared",
            "discovery": {"command": "codex", "args": ["--help"]},
        },
    }


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
