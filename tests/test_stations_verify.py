"""Tests for explicit station-manifest conformance verification."""

from __future__ import annotations

import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest

from brigade import cli, managed, stations_cmd


def _write_executable(directory: Path, name: str, body: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(f"#!{sys.executable}\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _surface(**overrides):
    surface = {
        "kind": "verify-exit",
        "command": ["station-helper", "--version"],
        "read_only": True,
        "timeout_seconds": 2,
    }
    surface.update(overrides)
    return surface


def _write_manifest(
    directory: Path,
    *,
    lifecycle: str = "active",
    owner: str = "maintained-owner",
    tool: dict | None = None,
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    if tool is None:
        tool = {
            "name": "station-helper",
            "command": "station-helper",
            "summary": "test helper",
            "install": ["python", "-c", "raise SystemExit('install must not run')"],
            "surfaces": [_surface()],
        }
    payload = {
        "schema": "brigade.station.v1",
        "name": "test-station",
        "station": "tokens",
        "summary": "test contract",
        "lifecycle": lifecycle,
        "owner": owner,
        "tools": [tool] if lifecycle == "active" else [],
    }
    path = directory / "station.json"
    path.write_text(json.dumps(payload))
    return path


@pytest.fixture
def helper_path(tmp_path, monkeypatch):
    binary = _write_executable(tmp_path / "bin", "station-helper", "print('station-helper 1.0')")
    monkeypatch.setenv("PATH", f"{binary.parent}{os.pathsep}{os.environ.get('PATH', '')}")
    return binary


def test_cli_verify_directory_and_manifest_json_payload(tmp_path, helper_path, capsys):
    manifest = _write_manifest(tmp_path / "sidecar")

    assert cli.main(["stations", "verify", str(manifest.parent), "--json"]) == 0
    directory_payload = json.loads(capsys.readouterr().out)
    assert directory_payload["schema"] == "brigade.stations.verify.v1"
    assert directory_payload["status"] == "passed"
    assert directory_payload["ok"] is True
    assert directory_payload["manifest"]["path"] == str(manifest)

    assert cli.main(["stations", "verify", str(manifest), "--json", "--check-managed"]) == 0
    file_payload = json.loads(capsys.readouterr().out)
    assert file_payload["check_managed"] is True


def test_cli_verify_missing_or_malformed_manifest_is_usage_error(tmp_path, capsys):
    assert cli.main(["stations", "verify", str(tmp_path / "missing"), "--json"]) == 2
    missing = json.loads(capsys.readouterr().out)
    assert missing["status"] == "error"
    assert missing["exit_code"] == 2

    bad = tmp_path / "station.json"
    bad.write_text("{")
    assert cli.main(["stations", "verify", str(bad), "--json"]) == 2
    malformed = json.loads(capsys.readouterr().out)
    assert malformed["status"] == "error"
    assert "not valid JSON" in malformed["detail"]


def test_cli_verify_manifest_read_race_is_structured_usage_error(tmp_path, monkeypatch, capsys):
    manifest = _write_manifest(tmp_path / "sidecar")
    real_read_text = Path.read_text

    def raced_read_text(path, *args, **kwargs):
        if path == manifest:
            raise FileNotFoundError("manifest disappeared")
        return real_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", raced_read_text)

    assert cli.main(["stations", "verify", str(manifest), "--json"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "error"
    assert payload["exit_code"] == 2
    assert "could not be read" in payload["detail"]


def test_cli_verify_oversized_timeout_is_structured_usage_error(tmp_path, capsys):
    manifest = _write_manifest(tmp_path / "sidecar")
    raw = json.loads(manifest.read_text())
    raw["tools"][0]["surfaces"][0]["timeout_seconds"] = 10**400
    manifest.write_text(json.dumps(raw))

    assert cli.main(["stations", "verify", str(manifest), "--json"]) == 2
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["status"] == "error"
    assert payload["exit_code"] == 2
    assert "numeric range" in payload["detail"]
    assert "Traceback" not in captured.err


def test_discovery_and_verification_never_execute_install_argv(tmp_path, helper_path):
    marker = tmp_path / "install-ran"
    manifest = _write_manifest(tmp_path / "sidecar")
    raw = json.loads(manifest.read_text())
    raw["tools"][0]["install"] = [sys.executable, "-c", f"open({str(marker)!r}, 'w').write('bad')"]
    manifest.write_text(json.dumps(raw))

    discovered = stations_cmd.discover_payload(roots=[tmp_path], max_depth=2)
    payload = stations_cmd.verify_payload(str(manifest))

    assert discovered["count"] == 1
    assert payload["ok"] is True
    assert not marker.exists()


def test_executable_runs_absolute_without_shell_from_manifest_parent_in_isolated_home(tmp_path, monkeypatch):
    sidecar = tmp_path / "sidecar"
    binary = _write_executable(
        tmp_path / "bin",
        "station-helper",
        """
import json, os
print(json.dumps({
    "argv0_absolute": os.path.isabs(__import__('sys').argv[0]),
    "cwd": os.getcwd(),
    "home": os.environ["HOME"],
    "xdg": [os.environ[name] for name in (
        "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME"
    )],
}))
""",
    )
    monkeypatch.setenv("PATH", f"{binary.parent}{os.pathsep}{os.environ.get('PATH', '')}")
    real_popen = stations_cmd.subprocess.Popen
    calls = []

    def recording_popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return real_popen(argv, **kwargs)

    monkeypatch.setattr(stations_cmd.subprocess, "Popen", recording_popen)
    _write_manifest(
        sidecar,
        tool={
            "name": "station-helper",
            "command": "station-helper",
            "surfaces": [_surface(kind="summary-json", command=["station-helper"], max_chars=1000)],
        },
    )

    payload = stations_cmd.verify_payload(str(sidecar))

    assert payload["ok"] is True
    argv, kwargs = calls[0]
    assert argv[0] == str(binary.resolve())
    assert kwargs["cwd"] == sidecar
    assert kwargs["shell"] is False
    env = kwargs["env"]
    isolated = {env["HOME"], env["XDG_CONFIG_HOME"], env["XDG_CACHE_HOME"], env["XDG_DATA_HOME"]}
    assert len(isolated) == 4
    assert all(Path(value).is_relative_to(Path(env["HOME"]).parent) for value in isolated)
    assert not Path(env["HOME"]).parent.exists()


@pytest.mark.parametrize(
    ("kind", "body", "expected"),
    [
        ("summary-json", "print('{not json}')", "invalid-json"),
        ("summary-json", "print('{\"ok\": true}')", "passed"),
        ("brief-markdown", "print('')", "empty-markdown"),
        ("brief-markdown", "print('# useful brief')", "passed"),
    ],
)
def test_safe_operational_surface_validates_json_or_markdown_shape(tmp_path, monkeypatch, kind, body, expected):
    binary = _write_executable(tmp_path / "bin", "station-helper", body)
    monkeypatch.setenv("PATH", f"{binary.parent}{os.pathsep}{os.environ.get('PATH', '')}")
    _write_manifest(
        tmp_path / "sidecar",
        tool={
            "name": "station-helper",
            "command": "station-helper",
            "surfaces": [_surface(kind=kind, command=["station-helper"], max_chars=100)],
        },
    )

    payload = stations_cmd.verify_payload(str(tmp_path / "sidecar"))

    result = payload["tools"][0]["surfaces"][0]
    assert result["status"] == expected
    assert payload["ok"] is (expected == "passed")


@pytest.mark.parametrize(
    "body",
    [
        "print('NaN')",
        "print('Infinity')",
        "print('-Infinity')",
        "print('{\"value\": NaN}')",
    ],
)
def test_json_surface_rejects_non_standard_constants(tmp_path, monkeypatch, body):
    binary = _write_executable(tmp_path / "bin", "station-helper", body)
    monkeypatch.setenv("PATH", f"{binary.parent}{os.pathsep}{os.environ.get('PATH', '')}")
    _write_manifest(
        tmp_path / "sidecar",
        tool={
            "name": "station-helper",
            "command": "station-helper",
            "surfaces": [_surface(kind="summary-json", command=["station-helper"], max_chars=100)],
        },
    )

    payload = stations_cmd.verify_payload(str(tmp_path / "sidecar"))

    assert payload["tools"][0]["surfaces"][0]["status"] == "invalid-json"
    assert payload["ok"] is False


def test_help_probe_checks_flags_without_running_stateful_template(tmp_path, monkeypatch):
    marker = tmp_path / "operational-ran"
    binary = _write_executable(
        tmp_path / "bin",
        "station-helper",
        f"""
import pathlib, sys
if '--help' in sys.argv:
    print('usage: station-helper evidence --markdown --limit')
else:
    pathlib.Path({str(marker)!r}).write_text('bad')
""",
    )
    monkeypatch.setenv("PATH", f"{binary.parent}{os.pathsep}{os.environ.get('PATH', '')}")
    _write_manifest(
        tmp_path / "sidecar",
        tool={
            "name": "station-helper",
            "command": "station-helper",
            "surfaces": [
                _surface(
                    kind="brief-markdown",
                    command=["station-helper", "evidence", "<task>", "--markdown", "--limit", "5"],
                    read_only=False,
                    max_chars=1000,
                    probe=["station-helper", "evidence", "--help"],
                    probe_contains=["--markdown", "--limit"],
                )
            ],
        },
    )

    payload = stations_cmd.verify_payload(str(tmp_path / "sidecar"))

    assert payload["ok"] is True
    assert payload["tools"][0]["surfaces"][0]["execution"] == "probe"
    assert not marker.exists()


@pytest.mark.parametrize(
    "probe",
    [
        ["station-helper", "--help"],
        ["station-helper", "-h"],
        ["station-helper", "--version"],
        ["station-helper", "version"],
        ["station-helper", "doctor", "--help"],
        ["station-helper", "doctor", "hooks", "-h"],
    ],
)
def test_safe_probe_grammar_accepts_only_documented_help_and_version_forms(tmp_path, monkeypatch, probe):
    binary = _write_executable(tmp_path / "bin", "station-helper", "print('--required-flag')")
    monkeypatch.setenv("PATH", f"{binary.parent}{os.pathsep}{os.environ.get('PATH', '')}")
    _write_manifest(
        tmp_path / "sidecar",
        tool={
            "name": "station-helper",
            "command": "station-helper",
            "surfaces": [_surface(probe=probe, probe_contains=["--required-flag"])],
        },
    )

    payload = stations_cmd.verify_payload(str(tmp_path / "sidecar"))

    assert payload["ok"] is True


@pytest.mark.parametrize(
    "probe",
    [
        ["station-helper", "--help", "write"],
        ["station-helper", "version", "write"],
        ["station-helper", "doctor", "--version"],
        ["station-helper", "--json", "--help"],
        ["station-helper", "../outside", "--help"],
        ["station-helper", "doctor;write", "--help"],
        ["station-helper", "doctör", "--help"],
    ],
)
def test_safe_probe_grammar_rejects_ambiguous_forms_before_spawn(tmp_path, helper_path, monkeypatch, probe):
    _write_manifest(
        tmp_path / "sidecar",
        tool={
            "name": "station-helper",
            "command": "station-helper",
            "surfaces": [_surface(probe=probe, probe_contains=["--required-flag"])],
        },
    )
    monkeypatch.setattr(
        stations_cmd.subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("unsafe probe spawned"),
    )

    payload = stations_cmd.verify_payload(str(tmp_path / "sidecar"))

    result = payload["tools"][0]["surfaces"][0]
    assert result["status"] == "failed"
    assert result["executed"] is False
    assert "safe support grammar" in result["detail"]


def test_unverified_surface_makes_active_conformance_fail(tmp_path, helper_path):
    _write_manifest(
        tmp_path / "sidecar",
        tool={
            "name": "station-helper",
            "command": "station-helper",
            "surfaces": [_surface(command=["station-helper", "mutate"], read_only=False)],
        },
    )

    payload = stations_cmd.verify_payload(str(tmp_path / "sidecar"))

    surface = payload["tools"][0]["surfaces"][0]
    assert surface["status"] == "unverified"
    assert payload["ok"] is False


@pytest.mark.parametrize(
    ("surface", "detail"),
    [
        (_surface(probe=["station-helper", "write"]), "probe is not a safe support command"),
        (_surface(command=["other-tool", "--version"]), "does not match declared executable"),
        (_surface(timeout_seconds=0), "positive timeout_seconds"),
        (_surface(kind="summary-json", max_chars=None), "positive max_chars"),
    ],
)
def test_unsafe_or_unbounded_contract_fails_before_execution(tmp_path, helper_path, surface, detail):
    _write_manifest(
        tmp_path / "sidecar",
        tool={
            "name": "station-helper",
            "command": "station-helper",
            "surfaces": [surface],
        },
    )

    payload = stations_cmd.verify_payload(str(tmp_path / "sidecar"))

    result = payload["tools"][0]["surfaces"][0]
    assert result["status"] == "failed"
    assert detail in result["detail"]
    assert result["executed"] is False


def test_legacy_manifest_loads_and_discovers_but_strict_verify_reports_bounds(tmp_path, helper_path):
    manifest = _write_manifest(tmp_path / "sidecar")
    raw = json.loads(manifest.read_text())
    raw["tools"][0]["surfaces"][0].pop("timeout_seconds")
    manifest.write_text(json.dumps(raw))

    discovered = stations_cmd.discover_payload(roots=[tmp_path], max_depth=2)
    payload = stations_cmd.verify_payload(str(manifest))

    assert discovered["count"] == 1
    assert payload["ok"] is False
    assert "positive timeout_seconds" in payload["tools"][0]["surfaces"][0]["detail"]


def test_nonzero_exit_and_missing_executable_are_active_failures(tmp_path, monkeypatch):
    binary = _write_executable(tmp_path / "bin", "station-helper", "raise SystemExit(7)")
    monkeypatch.setenv("PATH", f"{binary.parent}{os.pathsep}{os.environ.get('PATH', '')}")
    _write_manifest(tmp_path / "sidecar")
    nonzero = stations_cmd.verify_payload(str(tmp_path / "sidecar"))
    assert nonzero["tools"][0]["surfaces"][0]["exit_code"] == 7
    assert nonzero["ok"] is False

    monkeypatch.setenv("PATH", "")
    missing = stations_cmd.verify_payload(str(tmp_path / "sidecar"))
    assert missing["tools"][0]["status"] == "unavailable"
    assert missing["ok"] is False


def test_timeout_kills_descendant_holding_pipe_and_sanitizes_payload(tmp_path, monkeypatch):
    pid_file = tmp_path / "child.pid"
    binary = _write_executable(
        tmp_path / "bin",
        "station-helper",
        f"""
import pathlib, subprocess, sys
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
pathlib.Path({str(pid_file)!r}).write_text(str(child.pid))
print('sensitive child output', flush=True)
raise SystemExit(0)
""",
    )
    monkeypatch.setenv("PATH", f"{binary.parent}{os.pathsep}{os.environ.get('PATH', '')}")
    _write_manifest(
        tmp_path / "sidecar",
        tool={
            "name": "station-helper",
            "command": "station-helper",
            "surfaces": [_surface(command=["station-helper"], timeout_seconds=0.2)],
        },
    )

    started = time.monotonic()
    payload = stations_cmd.verify_payload(str(tmp_path / "sidecar"))
    elapsed = time.monotonic() - started

    result = payload["tools"][0]["surfaces"][0]
    assert result["timed_out"] is True
    assert result["overflowed"] is False
    assert result["status"] == "timeout"
    assert elapsed < 3
    serialized = json.dumps(payload)
    assert "sensitive child output" not in serialized
    assert set(["exit_code", "duration_ms", "stdout_bytes", "stderr_bytes", "total_bytes"]) <= set(result)
    child_pid = int(pid_file.read_text())
    status_path = Path(f"/proc/{child_pid}/status")
    if status_path.exists():
        assert "State:\tZ" in status_path.read_text()
    else:
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.01)
        else:
            pytest.fail("descendant remained alive after process-group timeout cleanup")


@pytest.mark.skipif(not hasattr(os, "fork"), reason="fork-based process-group fixture requires POSIX")
def test_combined_output_limit_kills_group_and_caps_byte_counts(tmp_path, monkeypatch):
    binary = _write_executable(
        tmp_path / "bin",
        "station-helper",
        """
import os, sys, time
if os.fork() == 0:
    while True:
        os.write(2, b'e' * 4096)
else:
    while True:
        os.write(1, b'o' * 4096)
""",
    )
    monkeypatch.setenv("PATH", f"{binary.parent}{os.pathsep}{os.environ.get('PATH', '')}")
    _write_manifest(
        tmp_path / "sidecar",
        tool={
            "name": "station-helper",
            "command": "station-helper",
            "surfaces": [_surface(command=["station-helper"], timeout_seconds=2)],
        },
    )

    payload = stations_cmd.verify_payload(str(tmp_path / "sidecar"))

    result = payload["tools"][0]["surfaces"][0]
    assert result["status"] == "overflow"
    assert result["overflowed"] is True
    assert result["total_bytes"] == 64 * 1024
    assert result["stdout_bytes"] + result["stderr_bytes"] == result["total_bytes"]
    assert "oooo" not in json.dumps(payload)


def test_unsupported_platform_fails_closed_before_spawn(tmp_path, helper_path, monkeypatch, capsys):
    _write_manifest(tmp_path / "sidecar")
    monkeypatch.setattr(stations_cmd, "_supports_process_containment", lambda: False, raising=False)
    monkeypatch.setattr(
        stations_cmd.subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("verifier process spawned on unsupported platform"),
    )

    assert cli.main(["stations", "verify", str(tmp_path / "sidecar"), "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "unsupported-platform"
    assert payload["ok"] is False
    assert payload["platform"] == {
        "supported": False,
        "detail": "station probe execution requires POSIX process-group containment",
    }
    assert payload["tools"][0]["status"] == "unsupported-platform"
    assert payload["tools"][0]["surfaces"][0]["status"] == "unsupported-platform"


def test_details_strip_controls_and_human_output_quotes_manifest_fields(tmp_path, helper_path, capsys):
    manifest = _write_manifest(
        tmp_path / "sidecar",
        tool={
            "name": "tool\x07name",
            "command": "station-helper",
            "surfaces": [
                _surface(
                    kind="surface\u009bkind",
                    probe=["station-helper", "--help"],
                    probe_contains=["\x1b[31m\u202e--missing"],
                )
            ],
        },
    )
    raw = json.loads(manifest.read_text())
    raw["name"] = "station\x1b[31m\u202ename"
    manifest.write_text(json.dumps(raw))

    assert stations_cmd.verify(str(manifest)) == 1
    output = capsys.readouterr().out
    result = stations_cmd.verify_payload(str(manifest))["tools"][0]["surfaces"][0]

    forbidden = {"\x1b", "\x07", "\u009b", "\u202e"}
    assert not any(character in result["detail"] for character in forbidden)
    assert not any(character in output for character in forbidden)
    assert 'name="station[31mname"' in output
    assert '"toolname"' in output
    assert '"surfacekind"' in output


@pytest.mark.parametrize("lifecycle", ["embedded", "deprecated", "historical"])
def test_non_active_lifecycle_skips_external_execution(tmp_path, lifecycle):
    _write_manifest(tmp_path / lifecycle, lifecycle=lifecycle)

    payload = stations_cmd.verify_payload(str(tmp_path / lifecycle))

    assert payload["status"] == f"{lifecycle}-skip"
    assert payload["ok"] is True
    assert payload["tools"] == []
    assert payload["lifecycle_counts"][lifecycle] == 1


def test_skill_roster_probe_runs_from_manifest_without_install_detection(tmp_path, monkeypatch):
    sidecar = tmp_path / "skillet"
    sidecar.mkdir()
    (sidecar / "probe.py").write_text("import os; print('ok' if os.getcwd() else 'bad')\n")
    _write_manifest(
        sidecar,
        tool={
            "name": "skillet",
            "kind": "skill-roster",
            "install": ["npx", "skills", "add", "example.invalid/skillet"],
            "surfaces": [
                {
                    "kind": "verify-exit",
                    "probe": [sys.executable, "probe.py"],
                    "probe_contains": ["ok"],
                    "timeout_seconds": 2,
                }
            ],
        },
    )
    monkeypatch.setattr(stations_cmd.shutil, "which", lambda command: pytest.fail("executable lookup ran"))

    payload = stations_cmd.verify_payload(str(sidecar))

    assert payload["ok"] is True
    assert payload["tools"][0]["kind"] == "skill-roster"
    assert payload["managed_parity"]["exemptions"] == [{"tool": "skillet", "reason": "skill-roster"}]


def test_skill_roster_rejects_probe_without_manifest_local_target(tmp_path):
    sidecar = tmp_path / "skillet"
    _write_manifest(
        sidecar,
        tool={
            "name": "skillet",
            "kind": "skill-roster",
            "surfaces": [
                {
                    "kind": "verify-exit",
                    "probe": [sys.executable, "-c", "print('ok')"],
                    "probe_contains": ["ok"],
                    "timeout_seconds": 2,
                }
            ],
        },
    )

    payload = stations_cmd.verify_payload(str(sidecar))

    result = payload["tools"][0]["surfaces"][0]
    assert result["status"] == "failed"
    assert result["executed"] is False
    assert "manifest-local" in result["detail"]


def test_managed_parity_drift_is_advisory_unless_requested(tmp_path, helper_path, monkeypatch):
    _write_manifest(tmp_path / "sidecar")
    catalog_tool = managed.ManagedTool(
        name="station-helper",
        station="tokens",
        command="station-helper",
        summary="managed",
        install_args=["pipx", "install", "different"],
        wire=lambda ctx: [],
        doctor=lambda ctx: [],
        surfaces=(managed.MachineSurface("verify-exit", ("station-helper", "--help"), timeout_seconds=2),),
    )
    monkeypatch.setattr(managed, "resolve", lambda name: catalog_tool if name == "station-helper" else None)

    advisory = stations_cmd.verify_payload(str(tmp_path / "sidecar"))
    gated = stations_cmd.verify_payload(str(tmp_path / "sidecar"), check_managed=True)

    assert advisory["managed_parity"]["status"] == "drift"
    assert advisory["managed_parity"]["advisory"] is True
    assert advisory["ok"] is True
    assert gated["managed_parity"]["status"] == "drift"
    assert gated["managed_parity"]["advisory"] is False
    assert gated["ok"] is False


def test_unmanaged_executable_is_named_as_parity_exemption(tmp_path, helper_path):
    _write_manifest(tmp_path / "sidecar")

    payload = stations_cmd.verify_payload(str(tmp_path / "sidecar"), check_managed=True)

    assert payload["managed_parity"]["exemptions"] == [{"tool": "station-helper", "reason": "not-in-managed-catalog"}]
    assert payload["ok"] is True
