from __future__ import annotations

import shlex
import sys

import pytest

from brigade.repos_cmd import fleet
from brigade.work_cmd import config, constants, verification


@pytest.fixture
def policy_name(monkeypatch):
    name = "policy-tool"
    monkeypatch.setattr(constants, "SCANNER_HIGH_RISK_COMMANDS", {name})
    return name


@pytest.fixture(params=["scanner", "review", "verify-command", "verify-argv", "fleet-command", "fleet-argv"])
def policy_entry(request, tmp_path):
    entry = request.param

    def parse(argv):
        command = shlex.join(argv)
        if entry == "scanner":
            return config._scanner_argv(command)
        if entry == "review":
            return config._review_argv(argv)
        if entry == "verify-command":
            result, _, reason = verification._verify_parse_command(command, tmp_path)
            return result, reason
        if entry == "verify-argv":
            result, _, reason = verification._verify_parse_argv(argv, tmp_path)
            return result, reason
        raw = {"command": command} if entry == "fleet-command" else {"argv": argv}
        return fleet._health_command_from_raw(raw, "demo", 1)

    kind = "verification" if entry.startswith("verify-") else "scanner"
    return parse, kind


def test_policy_checks_arguments(policy_entry, monkeypatch):
    argv = ["brigade", "--format", "sample", "report"]
    observed = []

    def check_policy(parts):
        observed.append(parts)
        return "policy-tool"

    monkeypatch.setattr(constants, "policy_blocked_name", check_policy, raising=False)
    parse, kind = policy_entry
    result, reason = parse(argv)

    assert observed == [argv]
    assert result is None
    assert reason is not None
    assert f"high-risk {kind} command: policy-tool" in reason


@pytest.mark.parametrize("spelling", ["policy-tool", "/tools/POLICY-TOOL", r"C:\tools\Policy-Tool.EXE"])
def test_policy_matches_argument_names(spelling, policy_name):
    assert constants.policy_blocked_name(["report-tool", "--format", "sample", spelling]) == policy_name


def test_policy_accepts_unlisted_arguments(policy_entry, policy_name, monkeypatch):
    monkeypatch.setattr(config.shutil, "which", lambda executable, *, path=None: executable)
    parse, _ = policy_entry
    result, reason = parse(["brigade", "report", "sample", f"--name=/tools/{policy_name}"])

    assert result is not None
    assert reason is None


def test_fleet_requires_a_health_command():
    result, reason = fleet._health_command_from_raw({"argv": []}, "demo", 1)

    assert result is None
    assert reason == "repo demo: health command health-1: command or argv is required"


@pytest.mark.parametrize(
    "spelling",
    [
        "policy-tool",
        "Policy-Tool",
        "POLICY-TOOL",
        "/tools/policy-tool",
        r"\tools\policy-tool",
        r"C:\tools\Policy-Tool.EXE",
        "policy-tool.exe",
        "policy-tool.com",
        "policy-tool.bat",
        "policy-tool.cmd",
        "policy-tool.vbs",
        "policy-tool.vbe",
        "policy-tool.js",
        "policy-tool.jse",
        "policy-tool.wsf",
        "policy-tool.wsh",
        "policy-tool.msc",
        "policy-tool.",
        "Policy-Tool.EXE. ",
        r"C:\tools\policy-tool.exe:part",
        "policy-tool.exe::$DATA",
    ],
)
def test_policy_matches_normalized_names(spelling, policy_entry, policy_name):
    parse, kind = policy_entry
    result, reason = parse([spelling])
    assert result is None
    assert reason is not None
    assert f"high-risk {kind} command: {policy_name}" in reason


@pytest.mark.parametrize(
    ("spelling", "name"),
    [
        ("", ""),
        ("Policy-Tool.Config", "policy-tool.config"),
        ("Policy-Tool.EXE.CMD", "policy-tool.exe"),
        (r"C:Policy-Tool.EXE", "policy-tool"),
        (r"C:\Program Files/tools\Policy-Tool.EXE", "policy-tool"),
        ("Policy-Tool. . ", "policy-tool"),
        ("Policy-Tool.Exe:Part.Exe", "policy-tool"),
        ("...", ""),
    ],
)
def test_normalize_policy_executable(spelling, name):
    assert constants.normalize_policy_executable(spelling) == name


@pytest.mark.parametrize("name", ["cmd", "dash", "ksh", "busybox"])
def test_policy_matches_shell_names(name, tmp_path):
    argv, _, reason = verification._verify_parse_argv([name], tmp_path)
    assert argv is None
    assert reason is not None
    assert reason.startswith(f"high-risk verification command: {name} ")


@pytest.mark.skipif(sys.platform != "win32", reason="requires a native Windows path")
def test_policy_matches_native_absolute_name(policy_name, tmp_path):
    executable = tmp_path / f"{policy_name}.EXE"
    assert executable.is_absolute()

    argv, review_error = config._review_argv([str(executable)])
    assert argv is None
    assert review_error == f"high-risk scanner command: {policy_name}"

    argv, _, verify_error = verification._verify_parse_argv([str(executable)], tmp_path)
    assert argv is None
    assert verify_error is not None
    assert verify_error.startswith(f"high-risk verification command: {policy_name} ")
