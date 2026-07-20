import importlib.util
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "published-artifact-acceptance.py"


@pytest.fixture()
def acceptance_module():
    spec = importlib.util.spec_from_file_location("published_artifact_acceptance", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _component(component_id, executable, *, status="healthy", detail="ok"):
    return {
        "component_id": component_id,
        "status": status,
        "detail": detail,
        "recorded_executable": str(executable),
        "managed_executable_path": str(executable),
    }


def _healthy_report(managed_bin):
    return {
        "components": [
            _component(component_id, managed_bin / component_id)
            for component_id in ("graphtrail", "graphtrail-mcp", "miseledger", "sessionfind")
        ]
    }


def _write_managed_binaries(managed_bin):
    managed_bin.mkdir(parents=True)
    for component_id in ("graphtrail", "graphtrail-mcp", "miseledger", "sessionfind"):
        executable = managed_bin / component_id
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o755)


def test_component_report_rejects_relative_executable_before_resolving(acceptance_module, tmp_path):
    report = _healthy_report(tmp_path / "xdg-data" / "brigade" / "bin")
    report["components"][0]["recorded_executable"] = "graphtrail"

    with pytest.raises(acceptance_module.AcceptanceError, match="absolute"):
        acceptance_module.validate_component_report(report, tmp_path / "xdg-data" / "brigade" / "bin")


def test_managed_bin_path_uses_xdg_data_home_on_linux(acceptance_module, tmp_path):
    assert acceptance_module.managed_bin_path(tmp_path / "xdg-data", tmp_path / "profile", platform="linux") == (
        tmp_path / "xdg-data" / "brigade" / "bin"
    )


def test_managed_bin_path_uses_application_support_on_macos(acceptance_module, tmp_path):
    assert acceptance_module.managed_bin_path(tmp_path / "xdg-data", tmp_path / "profile", platform="darwin") == (
        tmp_path / "profile" / "Library" / "Application Support" / "brigade" / "bin"
    )


def test_component_report_rejects_outside_managed_root(acceptance_module, tmp_path):
    managed_bin = tmp_path / "xdg-data" / "brigade" / "bin"
    _write_managed_binaries(managed_bin)
    outside = tmp_path / "outside"
    outside.write_text("not managed")
    report = _healthy_report(managed_bin)
    report["components"][0]["recorded_executable"] = str(outside)

    with pytest.raises(acceptance_module.AcceptanceError, match="outside"):
        acceptance_module.validate_component_report(report, managed_bin)


def test_component_report_rejects_symlink_outside_managed_root(acceptance_module, tmp_path):
    managed_bin = tmp_path / "xdg-data" / "brigade" / "bin"
    _write_managed_binaries(managed_bin)
    outside = tmp_path / "outside"
    outside.write_text("not managed")
    linked = managed_bin / "graphtrail"
    linked.unlink()
    linked.symlink_to(outside)
    report = _healthy_report(managed_bin)

    with pytest.raises(acceptance_module.AcceptanceError, match="outside"):
        acceptance_module.validate_component_report(report, managed_bin)


def test_component_report_rejects_missing_or_unhealthy_component(acceptance_module, tmp_path):
    managed_bin = tmp_path / "xdg-data" / "brigade" / "bin"
    report = _healthy_report(managed_bin)

    with pytest.raises(acceptance_module.AcceptanceError, match="missing"):
        acceptance_module.validate_component_report(report, managed_bin)

    _write_managed_binaries(managed_bin)
    report["components"][0]["status"] = "corrupt"
    report["components"][0]["detail"] = "digest mismatch"
    with pytest.raises(acceptance_module.AcceptanceError, match="digest mismatch"):
        acceptance_module.validate_component_report(report, managed_bin)


def test_component_report_requires_exactly_four_components(acceptance_module, tmp_path):
    managed_bin = tmp_path / "xdg-data" / "brigade" / "bin"
    _write_managed_binaries(managed_bin)
    report = _healthy_report(managed_bin)
    report["components"].pop()

    with pytest.raises(acceptance_module.AcceptanceError, match="exactly 4"):
        acceptance_module.validate_component_report(report, managed_bin)


def test_command_failure_preserves_installer_or_asset_error(acceptance_module):
    def failing_runner(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 1, "", "missing asset: digest mismatch")

    with pytest.raises(acceptance_module.AcceptanceError, match="missing asset: digest mismatch"):
        acceptance_module.run_checked(["pipx", "install", "brigade-cli==1.2.3"], runner=failing_runner)


def test_wait_for_pypi_version_returns_when_exact_version_is_immediately_available(acceptance_module):
    calls = []

    def fetch_json(url):
        calls.append(url)
        return {"releases": {"1.2.3": [{}]}}

    acceptance_module.wait_for_pypi_version(
        "1.2.3",
        fetch_json=fetch_json,
        sleep=lambda _: pytest.fail("available version should not sleep"),
    )

    assert calls == [acceptance_module.PYPI_PROJECT_URL]


def test_wait_for_pypi_version_retries_until_exact_version_is_available(acceptance_module):
    clock = [0.0]
    sleeps = []
    responses = iter(({"releases": {}}, {"releases": {}}, {"releases": {"1.2.3": [{}]}}))

    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    acceptance_module.wait_for_pypi_version(
        "1.2.3",
        fetch_json=lambda _: next(responses),
        sleep=sleep,
        monotonic=lambda: clock[0],
        timeout_seconds=10,
        poll_interval_seconds=2,
    )

    assert sleeps == [2, 2]


def test_wait_for_pypi_version_times_out_after_unavailable_or_malformed_responses(acceptance_module):
    clock = [0.0]
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    with pytest.raises(acceptance_module.AcceptanceError, match="not available"):
        acceptance_module.wait_for_pypi_version(
            "1.2.3",
            fetch_json=lambda _: {"releases": []},
            sleep=sleep,
            monotonic=lambda: clock[0],
            timeout_seconds=6,
            poll_interval_seconds=2,
        )

    assert sleeps == [2, 2, 2]


def test_smoke_uses_only_absolute_managed_executables(acceptance_module, tmp_path):
    managed_bin = tmp_path / "xdg-data" / "brigade" / "bin"
    _write_managed_binaries(managed_bin)
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        assert Path(argv[0]).is_absolute()
        if Path(argv[0]).name == "graphtrail-mcp":
            return subprocess.CompletedProcess(argv, 0, '{"jsonrpc":"2.0","id":1,"result":{}}', "")
        if Path(argv[0]).name == "sessionfind":
            return subprocess.CompletedProcess(argv, 0, "usage: sessionfind", "")
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    acceptance_module.smoke_managed_components(
        {component_id: managed_bin / component_id for component_id in acceptance_module.COMPONENT_IDS},
        runner=runner,
    )

    assert {Path(argv[0]).name for argv in calls} == set(acceptance_module.COMPONENT_IDS)


def test_poison_binary_invocation_is_a_failure(acceptance_module, tmp_path):
    marker = tmp_path / "poison-invoked"
    marker.write_text("graphtrail\n")

    with pytest.raises(acceptance_module.AcceptanceError, match="poison"):
        acceptance_module.assert_no_poison_invocation(marker)
