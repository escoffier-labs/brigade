"""Local route support against the Orca 1.4.269 CLI contract."""

import json
from pathlib import Path

import pytest

from brigade import cli, proc

ORCA_VERSION = "1.4.269"
VERSION_ARGS = ["--version"]
HELP_ARGS = ["orchestration", "worker-start", "--help"]


@pytest.fixture
def orca_cli(tmp_path, monkeypatch, capsys):
    fixture_path = Path(__file__).parent / "fixtures" / "native_orca" / f"readiness-{ORCA_VERSION}.json"
    fixture = json.loads(fixture_path.read_bytes())
    assert fixture["orca_version"] == ORCA_VERSION
    executable = str(tmp_path / "example orca install" / "orca-test.exe")
    roster = tmp_path / "roster.toml"

    def invoke(
        *,
        installed=True,
        version=None,
        help_text=None,
        agent="codex",
        help_code=0,
        command="orca-test",
        extra_args=(),
    ):
        command_setting = f'orca_command = "{command}"\n' if command else ""
        roster.write_bytes(
            f"""\
orchestrator = "lead"
{command_setting}
[agents.lead]
cli = "codex"
role = "lead"

[agents.coder]
cli = "codex"
role = "coder"

[orca_targets.coder]
agent = "{agent}"
worktree = "new-child"
orca_version = "{ORCA_VERSION}"
""".encode()
        )
        selected_command = command or "orca-ide"
        calls = []
        resolutions = []

        def resolve(name, path=None):
            if name == "codex":
                # The shared operation scope fingerprints the selected seat.
                return proc.ExecutableIdentity(name, None, "missing", False, "")
            resolutions.append(name)
            assert name == selected_command
            return proc.ExecutableIdentity(name, executable if installed else None, "native", installed, "")

        def run(argv, *, timeout, **kwargs):
            assert argv[0] == executable
            assert 0 < timeout <= 10
            args = argv[1:]
            calls.append(args)
            if args == VERSION_ARGS:
                stdout = fixture["version"]["stdout"] if version is None else version
                return proc.Result(0, stdout, "")
            assert args == HELP_ARGS
            stdout = fixture["worker_start_help"]["stdout"] if help_text is None else help_text
            return proc.Result(help_code, stdout, "example diagnostic" if help_code else "")

        monkeypatch.setattr(proc, "resolve_executable", resolve)
        monkeypatch.setattr(proc, "run", run)
        code = cli.main(
            [
                "roster",
                "ready",
                "--target",
                str(tmp_path),
                "--roster",
                str(roster),
                "--route",
                "orca",
                "--worker",
                "coder",
                *extra_args,
            ]
        )
        out, err = capsys.readouterr()
        assert not err
        assert resolutions == [selected_command]
        return code, out, calls

    return invoke, fixture


@pytest.mark.parametrize(
    ("scenario", "exit_code", "status", "states", "reason", "probe_args"),
    [
        ("missing-binary", 2, "blocked", ("failed", "skipped", "skipped", "skipped"), "orca-binary-missing", []),
        (
            "unsupported-version",
            2,
            "blocked",
            ("passed", "unsupported", "skipped", "skipped"),
            "orca-version-unsupported",
            [VERSION_ARGS],
        ),
        (
            "missing-retry-request",
            2,
            "blocked",
            ("passed", "passed", "unsupported", "skipped"),
            "orca-retry-request-unsupported",
            [VERSION_ARGS, HELP_ARGS],
        ),
        (
            "disabled-agent",
            2,
            "blocked",
            ("passed", "passed", "passed", "unsupported"),
            "orca-agent-disabled",
            [VERSION_ARGS, HELP_ARGS],
        ),
        (
            "probe-timeout",
            3,
            "unverified",
            ("passed", "passed", "unknown", "skipped"),
            "orca-help-probe-timeout",
            [VERSION_ARGS, HELP_ARGS],
        ),
        (
            "probe-failure",
            3,
            "unverified",
            ("passed", "passed", "unknown", "skipped"),
            "orca-help-probe-failed",
            [VERSION_ARGS, HELP_ARGS],
        ),
        (
            "unrecognized-version-output",
            3,
            "unverified",
            ("passed", "unknown", "skipped", "skipped"),
            "orca-version-unrecognized",
            [VERSION_ARGS],
        ),
        ("ready", 0, "ready", ("passed", "passed", "passed", "passed"), None, [VERSION_ARGS, HELP_ARGS]),
    ],
)
def test_orca_1_4_269_readiness(orca_cli, scenario, exit_code, status, states, reason, probe_args):
    invoke, fixture = orca_cli
    options = {
        "missing-binary": {"installed": False},
        "unsupported-version": {"version": "1.4.267\r\n"},
        "missing-retry-request": {
            "help_text": fixture["worker_start_help"]["stdout"].replace("--retry-request", "--retry-request-extra"),
        },
        "disabled-agent": {"agent": "unsupported-agent"},
        "probe-timeout": {"help_code": 124},
        "probe-failure": {"help_code": 1},
        "unrecognized-version-output": {"version": "unrecognized"},
        "ready": {"command": None},
    }
    code, out, calls = invoke(**options[scenario], extra_args=("--json",))
    report = json.loads(out)
    assert code == report["exit_code"] == exit_code
    assert report["schema"] == "brigade.roster_readiness.v1"
    assert report["request"]["route"] == "orca"
    assert report["status"] == status
    assert report["limiting_reason"] == reason
    assert report["assurance"] == "local-probe"
    assert report["admitted_capacity"] is None
    assert report["capacity_reserved"] is False
    assert report["launch_guaranteed"] is False
    assert report["fallback"]["automatic"] is False
    assert report["usable_worker_count"] == int(status == "ready")
    evidence = {item["category"]: item for item in report["evidence"]}
    assert (
        tuple(evidence[key]["status"] for key in ("orca-binary", "orca-version", "orca-retry-request", "orca-agent"))
        == states
    )
    assert evidence["orca-agent"]["seat"] == "coder"
    assert "example diagnostic" not in out and "example orca install" not in out
    assert calls == probe_args


def test_orca_1_4_269_text_reports_skipped_checks(orca_cli):
    invoke, _ = orca_cli
    code, out, calls = invoke(installed=False)
    assert code == 2
    assert "orca-binary: failed" in out
    assert "orca-version: skipped" in out
    assert "orca-retry-request: skipped" in out
    assert "orca-agent seat=coder: skipped" in out
    assert not calls
