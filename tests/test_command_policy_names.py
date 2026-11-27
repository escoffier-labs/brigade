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


@pytest.mark.parametrize(
    "arguments",
    [
        ["--format", "sample", "policy-tool"],
        ["--name=Policy-Tool.EXE"],
        ["-X/tools/POLICY-TOOL"],
        ['"C:\\Program Files\\Policy-Tool.EXE" sample'],
        ["fixture-wrapper", "--format", "sample", "policy-tool.exe"],
        ["fixture-runner", "run", "policy-tool", "sample"],
        ["fixture-runner run policy-tool sample"],
    ],
    ids=["plain", "named", "short", "quoted", "multiple", "flat-runner", "embedded-runner"],
)
def test_policy_matches_launcher_arguments(arguments, policy_entry, policy_name, monkeypatch):
    monkeypatch.setattr(constants, "_POLICY_LAUNCHERS", {"fixture-wrapper", "fixture-runner"})
    monkeypatch.setattr(constants, "_POLICY_LAUNCHER_SUBCOMMANDS", {"fixture-runner": {"run"}})
    parse, kind = policy_entry
    result, reason = parse(["Fixture-Wrapper.EXE", *arguments])

    assert result is None
    assert reason is not None
    assert f"high-risk {kind} command: {policy_name}" in reason


@pytest.mark.parametrize(("subcommand", "blocked"), [("run", True), ("inspect", False)])
def test_policy_uses_launcher_subcommands(subcommand, blocked, policy_entry, policy_name, monkeypatch):
    monkeypatch.setattr(constants, "_POLICY_LAUNCHERS", {"fixture-runner"})
    monkeypatch.setattr(constants, "_POLICY_LAUNCHER_SUBCOMMANDS", {"fixture-runner": {"run"}})
    monkeypatch.setattr(config.shutil, "which", lambda executable, *, path=None: executable)
    parse, kind = policy_entry
    result, reason = parse(["fixture-runner", subcommand, policy_name])

    if blocked:
        assert result is None
        assert reason is not None
        assert f"high-risk {kind} command: {policy_name}" in reason
    else:
        assert result is not None
        assert reason is None


@pytest.mark.parametrize(
    ("argv", "listed_name"),
    [
        (["pytest", "-k", "policy-tool"], "policy-tool"),
        (["rg", "policy-tool", "sample"], "policy-tool"),
        (["rg", "--type", "policy-tool", "sample"], "policy-tool"),
        (["go", "test", "./cmd"], "cmd"),
        (["brigade", "--name=/tools/policy-tool"], "policy-tool"),
    ],
    ids=["test-filter", "search-pattern", "file-type", "package-path", "named-value"],
)
def test_policy_accepts_arguments_for_other_commands(argv, listed_name, policy_entry, monkeypatch):
    monkeypatch.setattr(constants, "SCANNER_HIGH_RISK_COMMANDS", {listed_name})
    monkeypatch.setattr(config.shutil, "which", lambda executable, *, path=None: executable)
    parse, _ = policy_entry
    result, reason = parse(argv)

    assert result is not None
    assert reason is None


@pytest.mark.parametrize("depth", [1, 2, 3, 4, 5])
def test_policy_checks_nested_arguments(depth, policy_name, monkeypatch):
    launcher = "fixture-wrapper"
    monkeypatch.setattr(constants, "_POLICY_LAUNCHERS", {launcher})
    target = policy_name if depth <= 3 else "fixture-report"
    argv = [launcher, target, "sample"]
    for _ in range(depth):
        argv = [launcher, shlex.join(argv)]

    expected = policy_name if depth <= 3 else None if depth == 4 else launcher
    assert constants.policy_blocked_name(argv) == expected


def test_fleet_refuses_empty_health_argv():
    with pytest.raises(IndexError):
        fleet._health_command_from_raw({"argv": []}, "demo", 1)


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
