"""Local route support against the Orca 1.4.269 CLI contract."""

import json
import os
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
        declared_version=ORCA_VERSION,
        worktree="new-child",
        target_fields="",
        check_env=None,
        extra_args=(),
    ):
        roster.write_bytes(
            f"""\
orchestrator = "lead"

[agents.lead]
cli = "codex"
role = "lead"

[agents.coder]
cli = "codex"
role = "coder"

[orca_targets.coder]
agent = "{agent}"
worktree = "{worktree}"
orca_version = "{declared_version}"
{target_fields}
""".encode()
        )
        calls = []
        resolutions = []

        def resolve(name, path=None):
            if name == "codex":
                # The shared operation scope fingerprints the selected seat.
                return proc.ExecutableIdentity(name, None, "missing", False, "")
            resolutions.append(name)
            assert name == "orca-ide"
            return proc.ExecutableIdentity(name, executable if installed else None, "native", installed, "")

        def run(argv, *, timeout, env=None, **kwargs):
            assert argv[0] == executable
            assert timeout == 12
            if check_env is not None:
                check_env(env)
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
        assert resolutions == ["orca-ide"]
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
            "orca-target-driver-unsupported",
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
        "ready": {},
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


@pytest.mark.parametrize(
    ("target", "reason"),
    [
        ({"declared_version": "1.0.0"}, "orca-target-version-unsupported"),
        ({"worktree": "current"}, "orca-target-worktree-unsupported"),
        ({"target_fields": 'effort = "high"'}, "orca-target-model-required"),
        ({"target_fields": 'unknown_field = "example"'}, "orca-target-invalid"),
    ],
)
def test_orca_1_4_269_refuses_invalid_target(orca_cli, target, reason):
    invoke, _ = orca_cli
    code, out, calls = invoke(**target, extra_args=("--json",))
    report = json.loads(out)
    evidence = {item["category"]: item for item in report["evidence"]}
    assert code == report["exit_code"] == 2
    assert report["limiting_reason"] == evidence["orca-agent"]["reason"] == reason
    assert evidence["orca-version"]["status"] == "passed"
    assert evidence["orca-agent"]["status"] == "unsupported"
    assert calls == [VERSION_ARGS, HELP_ARGS]


@pytest.mark.parametrize("section", ["Notes", "Examples", "Additional information"])
def test_orca_1_4_269_retry_request_in_a_later_section_does_not_count(orca_cli, section):
    invoke, fixture = orca_cli
    before_notes, heading, notes = fixture["worker_start_help"]["stdout"].partition("\nNotes:\n")
    assert heading and notes
    before_notes = before_notes.replace(" [--retry-request <id>]", "")
    before_notes = "\n".join(
        line for line in before_notes.split("\n") if not line.lstrip().startswith("--retry-request ")
    )
    assert "--retry-request" not in before_notes
    help_text = before_notes + f"\n{section}:\n  --retry-request <id>  Mentioned here only\n"
    help_text += notes if section == "Notes" else heading + notes
    code, out, calls = invoke(help_text=help_text, extra_args=("--json",))
    assert code == 2
    assert json.loads(out)["limiting_reason"] == "orca-retry-request-unsupported"
    assert calls == [VERSION_ARGS, HELP_ARGS]


def test_orca_1_4_269_foreign_worker_start_usage_is_unverified(orca_cli):
    invoke, fixture = orca_cli
    help_text = "\n".join(
        "Usage: unrelated worker-start" if line.startswith("Usage:") else line
        for line in fixture["worker_start_help"]["stdout"].split("\n")
    )
    code, out, calls = invoke(help_text=help_text, extra_args=("--json",))
    assert code == 3
    assert json.loads(out)["limiting_reason"] == "orca-worker-start-help-unrecognized"
    assert calls == [VERSION_ARGS, HELP_ARGS]


def test_orca_1_4_269_probes_filter_credentials_and_keep_startup_env(orca_cli, monkeypatch, tmp_path):
    invoke, _ = orca_cli
    credentials = (
        "GH_TOKEN",
        "OP_SERVICE_ACCOUNT_TOKEN",
        "Aws_Profile",
        "GITHUB_ACTOR",
        "ANTHROPIC_BASE_URL",
        "OPENAI_ORG_ID",
        "GOOGLE_APPLICATION_PROJECT",
        "example_token",
        "example_secret",
        "example_password",
        "example_passwd",
        "example_credential",
        "example_api_key",
        "example_apikey",
        "example_private_key",
        "example_auth",
    )
    startup = {
        "PATH": os.environ.get("PATH", str(tmp_path)),
        "HOME": str(tmp_path),
        "XDG_RUNTIME_DIR": str(tmp_path / "runtime"),
        "APPDATA": str(tmp_path / "appdata"),
        "APPIMAGE_EXTRACT_AND_RUN": "1",
    }
    for name in credentials:
        monkeypatch.setenv(name, "example")
    for name, value in startup.items():
        monkeypatch.setenv(name, value)

    def check_env(env):
        assert env is not None
        assert not {name.upper() for name in credentials} & {name.upper() for name in env}
        for name, value in startup.items():
            assert env[name] == value

    code, _, calls = invoke(check_env=check_env)
    assert code == 0
    assert calls == [VERSION_ARGS, HELP_ARGS]
    assert all(os.environ[name] == "example" for name in credentials)
