"""Local readiness against versioned Orca CLI registry evidence."""

import json
import os
from pathlib import Path

import pytest

from brigade import cli, proc

ORCA_VERSION = "1.4.269"
VERSION_ARGS = ["--version"]
REGISTRY_ARGS = ["agent-context", "--json"]


def registry_fixture(version):
    path = Path(__file__).parent / "fixtures" / "native_orca" / f"registry-{version}.json"
    fixture = json.loads(path.read_bytes())
    assert fixture["orca_version"] == version
    return fixture["registry"]


@pytest.fixture
def orca_cli(tmp_path, monkeypatch, capsys):
    fixture = registry_fixture(ORCA_VERSION)
    executable = str(tmp_path / "example orca install" / "orca-test.exe")
    roster = tmp_path / "roster.toml"

    def invoke(
        *,
        installed=True,
        version=None,
        registry_text=None,
        agent="codex",
        registry_code=0,
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
                stdout = ORCA_VERSION + "\n" if version is None else version
                return proc.Result(0, stdout, "")
            assert args == REGISTRY_ARGS
            stdout = json.dumps(fixture) if registry_text is None else registry_text
            return proc.Result(registry_code, stdout, "example diagnostic" if registry_code else "")

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
            "version-mismatch",
            2,
            "blocked",
            ("passed", "passed", "passed", "unsupported"),
            "orca-target-version-mismatch",
            [VERSION_ARGS, REGISTRY_ARGS],
        ),
        (
            "missing-retry-request",
            2,
            "blocked",
            ("passed", "passed", "unsupported", "skipped"),
            "orca-command-capabilities-missing",
            [VERSION_ARGS, REGISTRY_ARGS],
        ),
        (
            "disabled-agent",
            2,
            "blocked",
            ("passed", "passed", "passed", "unsupported"),
            "orca-target-driver-unsupported",
            [VERSION_ARGS, REGISTRY_ARGS],
        ),
        (
            "probe-timeout",
            3,
            "unverified",
            ("passed", "passed", "unknown", "skipped"),
            "orca-registry-probe-timeout",
            [VERSION_ARGS, REGISTRY_ARGS],
        ),
        (
            "probe-failure",
            3,
            "unverified",
            ("passed", "passed", "unknown", "skipped"),
            "orca-registry-probe-failed",
            [VERSION_ARGS, REGISTRY_ARGS],
        ),
        (
            "unrecognized-version-output",
            3,
            "unverified",
            ("passed", "unknown", "skipped", "skipped"),
            "orca-version-unrecognized",
            [VERSION_ARGS],
        ),
        ("ready", 0, "ready", ("passed", "passed", "passed", "passed"), None, [VERSION_ARGS, REGISTRY_ARGS]),
    ],
)
def test_orca_1_4_269_readiness(orca_cli, scenario, exit_code, status, states, reason, probe_args):
    invoke, fixture = orca_cli
    options = {
        "missing-binary": {"installed": False},
        "version-mismatch": {"version": "1.4.267\r\n"},
        "missing-retry-request": {
            "registry_text": json.dumps(fixture).replace('"retry-request"', '"retry-request-extra"'),
        },
        "disabled-agent": {"agent": "unsupported-agent"},
        "probe-timeout": {"registry_code": 124},
        "probe-failure": {"registry_code": 1},
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
        ({"declared_version": "1.4.269 extra"}, "orca-target-version-unsupported"),
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
    assert calls == [VERSION_ARGS, REGISTRY_ARGS]


@pytest.mark.parametrize("version", ["1.4.268", "1.4.269", "1.4.271"])
def test_ready_with_exact_version_and_registry_capabilities(orca_cli, version):
    invoke, _ = orca_cli
    registry = registry_fixture(version)
    registry["futureField"] = {"enabled": True}
    registry["commands"][0]["futureField"] = "example"
    code, out, calls = invoke(
        version=version + "\n", declared_version=version, registry_text=json.dumps(registry), extra_args=("--json",)
    )
    assert code == 0
    assert json.loads(out)["status"] == "ready"
    assert calls == [VERSION_ARGS, REGISTRY_ARGS]


@pytest.mark.parametrize(
    ("command", "flag"),
    [
        ("worker-start", "retry-request"),
        ("worker-stop", "retry-request"),
        ("worker-show", "full"),
        ("worker-read", "source"),
        ("inbox", "run"),
    ],
)
def test_missing_capability_blocks_readiness(orca_cli, command, flag):
    invoke, registry = orca_cli
    entry = next(item for item in registry["commands"] if item["path"] == ["orchestration", command])
    entry["flags"].remove(flag)
    code, out, calls = invoke(registry_text=json.dumps(registry), extra_args=("--json",))
    assert code == 2
    assert json.loads(out)["limiting_reason"] == "orca-command-capabilities-missing"
    assert calls == [VERSION_ARGS, REGISTRY_ARGS]


@pytest.mark.parametrize(
    "change",
    ["path", "missing-command", "duplicate", "flags", "unknown-schema", "bool-schema", "duplicate-key", "json"],
)
def test_registry_requires_unambiguous_structured_evidence(orca_cli, change):
    invoke, registry = orca_cli
    commands = registry["commands"]
    entry = commands[0]
    if change == "path":
        entry["path"] = "orchestration inbox"
    elif change == "missing-command":
        entry["path"].insert(0, "foreign")
    elif change == "duplicate":
        commands.append(entry.copy())
    elif change == "flags":
        entry["flags"].append(False)
    elif change == "unknown-schema":
        registry["schemaVersion"] = 2
    elif change == "bool-schema":
        registry["schemaVersion"] = True
    text = json.dumps(registry)
    if change == "json":
        text = "Usage: orca-ide"
    elif change == "duplicate-key":
        text = '{"schemaVersion": 2, ' + text[1:]
    code, out, calls = invoke(registry_text=text, extra_args=("--json",))
    assert code == (2 if change == "missing-command" else 3)
    reason = "orca-command-capabilities-missing" if change == "missing-command" else "orca-registry-unrecognized"
    assert json.loads(out)["limiting_reason"] == reason
    assert calls == [VERSION_ARGS, REGISTRY_ARGS]


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
    assert calls == [VERSION_ARGS, REGISTRY_ARGS]
    assert all(os.environ[name] == "example" for name in credentials)
