from __future__ import annotations

import json
import hashlib
import os
import shutil
from pathlib import Path

from brigade import cli, runbook_cmd


def _sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_tool(path, body=None):
    path.write_text(
        body
        or "\n".join(
            [
                "#!/bin/sh",
                'if [ "$1" = "--version" ]; then',
                "  printf 'tool 1.2.3\\n'",
                "  exit 0",
                "fi",
                'printf ran > "$1"',
                "",
            ]
        )
    )
    path.chmod(0o755)
    return path


def _write_runbook(path):
    path.write_text(
        json.dumps(
            {
                "id": "smoke",
                "description": "tiny runbook",
                "allowed_commands": ["printf"],
                "steps": [
                    {"id": "hello", "run": "printf hello"},
                    {"id": "again", "run": "printf again"},
                ],
            }
        )
    )
    return path


def test_runbook_plan_run_resume_and_closeout(tmp_path, capsys):
    runbook = _write_runbook(tmp_path / "runbook.json")

    assert runbook_cmd.plan(target=tmp_path, runbook=runbook, json_output=True) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["runbook_id"] == "smoke"
    assert plan["step_count"] == 2

    assert runbook_cmd.run(target=tmp_path, runbook=runbook, approved=True, json_output=True) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["status"] == "completed"
    assert len(receipt["steps"]) == 2
    assert (tmp_path / ".brigade" / "runbooks" / "runs" / receipt["run_id"] / "receipt.json").is_file()

    assert runbook_cmd.resume(target=tmp_path, json_output=True) == 0
    resume = json.loads(capsys.readouterr().out)
    assert resume["next"] is None

    assert runbook_cmd.closeout(target=tmp_path, run_id=receipt["run_id"], reason="checked", json_output=True) == 0
    closeout = json.loads(capsys.readouterr().out)
    assert closeout["status"] == "reviewed"
    assert closeout["reason"] == "checked"


def test_runbook_cli_and_failed_step(tmp_path, capsys):
    runbook = tmp_path / "fail.json"
    runbook.write_text(json.dumps({"id": "fail", "steps": [{"id": "bad", "run": "exit 7"}]}))

    assert cli.main(["runbook", "plan", str(runbook), "--target", str(tmp_path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["runbook_id"] == "fail"

    assert cli.main(["runbook", "run", str(runbook), "--target", str(tmp_path), "--approved", "--json"]) == 1
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["status"] == "failed"
    assert receipt["steps"][0]["exit_code"] == 7

    assert cli.main(["runbook", "resume", receipt["run_id"], "--target", str(tmp_path), "--json"]) == 0
    resume = json.loads(capsys.readouterr().out)
    assert resume["next"]["id"] == "bad"


def test_runbook_requires_approval_and_supports_dry_run(tmp_path, capsys):
    runbook = tmp_path / "runbook.json"
    runbook.write_text(json.dumps({"id": "needs-approval", "steps": [{"id": "hello", "run": "printf hello"}]}))

    assert runbook_cmd.run(target=tmp_path, runbook=runbook, json_output=True) == 1
    blocked = json.loads(capsys.readouterr().out)
    assert blocked["status"] == "approval-required"

    assert runbook_cmd.run(target=tmp_path, runbook=runbook, approved=True, dry_run=True, json_output=True) == 0
    dry = json.loads(capsys.readouterr().out)
    assert dry["dry_run"] is True
    assert not (tmp_path / ".brigade" / "runbooks" / "runs").exists()


def test_runbook_blocks_destructive_commands(tmp_path, capsys):
    runbook = tmp_path / "bad.json"
    runbook.write_text(json.dumps({"id": "bad", "steps": [{"id": "bad", "run": "rm -rf /tmp/nope"}]}))

    assert runbook_cmd.run(target=tmp_path, runbook=runbook, approved=True, json_output=True) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "blocked"
    assert payload["policy_failures"]


def test_runbook_retry_runs_from_failed_step(tmp_path, capsys):
    marker = tmp_path / "marker"
    runbook = tmp_path / "retry.json"
    runbook.write_text(
        json.dumps(
            {
                "id": "retry",
                "allowed_commands": ["test", "touch"],
                "steps": [
                    {"id": "wait", "run": f"test -f {marker}"},
                    {"id": "touch", "run": f"touch {tmp_path / 'done'}"},
                ],
            }
        )
    )

    assert runbook_cmd.run(target=tmp_path, runbook=runbook, approved=True, json_output=True) == 1
    failed = json.loads(capsys.readouterr().out)
    marker.write_text("ready")
    assert runbook_cmd.retry(target=tmp_path, run_id=failed["run_id"], approved=True, json_output=True) == 0
    retried = json.loads(capsys.readouterr().out)
    assert retried["source_run_id"] == failed["run_id"]
    assert retried["start_index"] == 1
    assert (tmp_path / "done").exists()


def test_file_embedded_approved_does_not_execute_without_cli_approval(tmp_path, capsys):
    """A runbook author baking approved=true must NOT bypass the operator's --approved gate."""
    marker = tmp_path / "pwned"
    runbook = tmp_path / "evil.json"
    runbook.write_text(
        json.dumps(
            {
                "id": "evil",
                "approved": True,
                "allowed_commands": ["touch"],
                "steps": [{"id": "drop", "run": f"touch {marker}"}],
            }
        )
    )

    # No operator-supplied approval: file-embedded approved=true is not enough.
    assert runbook_cmd.run(target=tmp_path, runbook=runbook, json_output=True) == 1
    blocked = json.loads(capsys.readouterr().out)
    assert blocked["status"] == "approval-required"
    assert not marker.exists()
    assert not (tmp_path / ".brigade" / "runbooks" / "runs").exists()

    # Operator-supplied --approved still executes the legitimate flow.
    assert runbook_cmd.run(target=tmp_path, runbook=runbook, approved=True, json_output=True) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["status"] == "completed"
    assert marker.exists()


def test_runbook_cli_run_ignores_file_embedded_approval(tmp_path, capsys):
    marker = tmp_path / "cli-pwned"
    runbook = tmp_path / "evil.json"
    runbook.write_text(
        json.dumps(
            {
                "id": "evil",
                "approved": True,
                "allowed_commands": ["touch"],
                "steps": [{"id": "drop", "run": f"touch {marker}"}],
            }
        )
    )

    # Without --approved on the CLI, execution is refused even though the file says approved.
    assert cli.main(["runbook", "run", str(runbook), "--target", str(tmp_path), "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "approval-required"
    assert not marker.exists()

    # With --approved it runs.
    assert cli.main(["runbook", "run", str(runbook), "--target", str(tmp_path), "--approved", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "completed"
    assert marker.exists()


def test_runbook_validates_whole_command_not_just_first_token(tmp_path, capsys):
    """allowed_commands:['bash'] + 'bash -c "rm -rf /"' must be blocked, not allowed by first-token match."""
    marker = tmp_path / "wrapped"
    runbook = tmp_path / "bypass.json"
    # Inner command is benign so the advisory deny-list does NOT catch it; only
    # whole-command allowlist validation should block the shell-wrapper bypass.
    runbook.write_text(
        json.dumps(
            {
                "id": "bypass",
                "allowed_commands": ["bash"],
                "steps": [{"id": "wrap", "run": f"bash -c 'touch {marker}'"}],
            }
        )
    )

    assert runbook_cmd.run(target=tmp_path, runbook=runbook, approved=True, json_output=True) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "blocked"
    assert payload["policy_failures"]
    assert not marker.exists()


def test_runbook_plan_warns_when_allowlist_negated_by_shell_wrapper(tmp_path, capsys):
    runbook = tmp_path / "shellwrap.json"
    runbook.write_text(
        json.dumps(
            {
                "id": "shellwrap",
                "allowed_commands": ["bash"],
                "steps": [{"id": "wrap", "run": "bash -c 'echo hi'"}],
            }
        )
    )

    assert runbook_cmd.plan(target=tmp_path, runbook=runbook, json_output=True) == 0
    plan = json.loads(capsys.readouterr().out)
    warnings = plan["steps"][0]["policy"]["warnings"]
    assert any("allowlist" in warning.lower() for warning in warnings)


def test_runbook_clean_pin_plan_run_and_receipt(tmp_path, capsys):
    tool = _write_tool(tmp_path / "tool")
    marker = tmp_path / "marker"
    runbook = tmp_path / "pinned.json"
    runbook.write_text(
        json.dumps(
            {
                "id": "pinned",
                "allowed_commands": ["tool"],
                "pins": [
                    {
                        "command": "tool",
                        "path": str(tool),
                        "sha256": _sha256(tool),
                        "version_cmd": "--version",
                        "version": "tool 1.2.3",
                    }
                ],
                "steps": [{"id": "run", "run": f"{tool} {marker}"}],
            }
        )
    )

    assert runbook_cmd.plan(target=tmp_path, runbook=runbook, json_output=True) == 0
    plan = json.loads(capsys.readouterr().out)
    pin = plan["steps"][0]["pin"]
    assert pin["command"] == "tool"
    assert pin["status"] == "ok"
    assert pin["resolved_path"] == str(tool)
    assert pin["expected_sha256"] == _sha256(tool)
    assert pin["observed_sha256"] == _sha256(tool)
    assert "version_output" not in pin

    assert runbook_cmd.run(target=tmp_path, runbook=runbook, approved=True, json_output=True) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert marker.read_text() == "ran"
    assert receipt["pin_checks"][0]["status"] == "ok"
    assert receipt["pin_checks"][0]["override"] is False
    assert receipt["pin_checks"][0]["version_output"] == "tool 1.2.3"


def test_runbook_pin_mismatch_refuses_without_executing(tmp_path, capsys):
    tool = _write_tool(tmp_path / "tool")
    marker = tmp_path / "marker"
    runbook = tmp_path / "pinned.json"
    runbook.write_text(
        json.dumps(
            {
                "id": "pinned",
                "allowed_commands": ["tool"],
                "pins": [{"command": "tool", "path": str(tool), "sha256": "0" * 64}],
                "steps": [{"id": "run", "run": f"{tool} {marker}"}],
            }
        )
    )

    assert runbook_cmd.run(target=tmp_path, runbook=runbook, approved=True, json_output=True) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "pin-check-failed"
    assert payload["pin_checks"][0]["status"] == "mismatch"
    assert not marker.exists()
    assert not (tmp_path / ".brigade" / "runbooks" / "runs").exists()


def test_runbook_pin_mismatch_override_proceeds_and_records_override(tmp_path, capsys):
    tool = _write_tool(tmp_path / "tool")
    marker = tmp_path / "marker"
    runbook = tmp_path / "pinned.json"
    runbook.write_text(
        json.dumps(
            {
                "id": "pinned",
                "allowed_commands": ["tool"],
                "pins": [{"command": "tool", "path": str(tool), "sha256": "0" * 64}],
                "steps": [{"id": "run", "run": f"{tool} {marker}"}],
            }
        )
    )

    assert (
        cli.main(
            [
                "runbook",
                "run",
                str(runbook),
                "--target",
                str(tmp_path),
                "--approved",
                "--allow-pin-mismatch",
                "--json",
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    receipt = json.loads(captured.out)
    assert marker.read_text() == "ran"
    assert "pin mismatch override" in captured.err
    assert receipt["pin_checks"][0]["status"] == "mismatch"
    assert receipt["pin_checks"][0]["override"] is True


def test_runbook_missing_pinned_binary_refuses_without_executing(tmp_path, capsys):
    marker = tmp_path / "marker"
    runbook = tmp_path / "missing.json"
    runbook.write_text(
        json.dumps(
            {
                "id": "missing",
                "pins": [{"command": "definitely-missing-brigade-test-bin", "sha256": "0" * 64}],
                "steps": [{"id": "run", "run": f"touch {marker}"}],
            }
        )
    )

    assert runbook_cmd.run(target=tmp_path, runbook=runbook, approved=True, json_output=True) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "pin-check-failed"
    assert payload["pin_checks"][0]["status"] == "missing"
    assert payload["pin_checks"][0]["resolved_path"] is None
    assert payload["pin_checks"][0]["observed_sha256"] is None
    assert not marker.exists()


def test_runbook_unpinned_receipt_keys_unchanged_and_no_pin_checks(tmp_path, capsys):
    runbook = _write_runbook(tmp_path / "runbook.json")

    assert runbook_cmd.run(target=tmp_path, runbook=runbook, approved=True, json_output=True) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert set(receipt) == {
        "run_id",
        "runbook_id",
        "target",
        "runbook_path",
        "started_at",
        "completed_at",
        "status",
        "approved",
        "source_run_id",
        "start_index",
        "steps",
        "receipt_path",
    }
    assert "pin_checks" not in receipt


def test_runbook_bash_script_step_plan_pins_only_argv0_interpreter(tmp_path, capsys):
    bash = shutil.which("bash")
    if bash is None:
        return
    script = tmp_path / "script.sh"
    script.write_text("#!/bin/sh\nprintf hi\n")
    runbook = tmp_path / "bash.json"
    runbook.write_text(
        json.dumps(
            {
                "id": "bash-pin",
                "allowed_commands": ["bash"],
                "pins": [{"command": "bash", "sha256": _sha256(Path(bash))}],
                "steps": [{"id": "run", "run": f"bash {script}"}],
            }
        )
    )

    assert runbook_cmd.plan(target=tmp_path, runbook=runbook, json_output=True) == 0
    plan = json.loads(capsys.readouterr().out)
    pin = plan["steps"][0]["pin"]
    assert pin["command"] == "bash"
    assert os.path.basename(pin["resolved_path"]) == "bash"
    assert pin["resolved_path"] != str(script)
    assert pin["observed_sha256"] == _sha256(Path(bash))


def test_runbook_pin_writes_pins_that_plan_verifies_clean(tmp_path, monkeypatch, capsys):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    tool = _write_tool(bindir / "tool")
    marker = tmp_path / "marker"
    runbook = tmp_path / "pin-me.json"
    runbook.write_text(
        json.dumps(
            {
                "id": "pin-me",
                "allowed_commands": ["tool"],
                "pins": [{"command": "tool", "version_cmd": "--version", "version": "old"}],
                "steps": [{"id": "run", "run": f"tool {marker}"}],
            }
        )
    )
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")

    assert cli.main(["runbook", "pin", str(runbook), "--target", str(tmp_path), "--json"]) == 0
    pinned = json.loads(capsys.readouterr().out)
    assert pinned["written"] is True
    assert pinned["runbook_path"] == str(runbook.resolve())
    assert pinned["pins"] == [
        {
            "command": "tool",
            "path": str(tool),
            "sha256": _sha256(tool),
            "version_cmd": "--version",
            "version": "tool 1.2.3",
        }
    ]
    assert not marker.exists()

    assert runbook_cmd.plan(target=tmp_path, runbook=runbook, json_output=True) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["steps"][0]["pin"]["status"] == "ok"
    assert plan["steps"][0]["pin"]["resolved_path"] == str(tool)


def test_runbook_repin_updates_hash_after_binary_replacement(tmp_path, monkeypatch, capsys):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    tool = _write_tool(bindir / "tool")
    runbook = tmp_path / "pin-me.json"
    runbook.write_text(
        json.dumps(
            {
                "id": "pin-me",
                "allowed_commands": ["tool"],
                "steps": [{"id": "run", "run": "tool marker"}],
            }
        )
    )
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")

    assert runbook_cmd.pin(target=tmp_path, runbook=runbook, json_output=True) == 0
    first = json.loads(capsys.readouterr().out)
    first_hash = first["pins"][0]["sha256"]

    _write_tool(
        tool,
        "\n".join(
            [
                "#!/bin/sh",
                'if [ "$1" = "--version" ]; then',
                "  printf 'tool 2.0.0\\n'",
                "  exit 0",
                "fi",
                'printf replaced > "$1"',
                "",
            ]
        ),
    )

    assert runbook_cmd.pin(target=tmp_path, runbook=runbook, json_output=True) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["pins"][0]["path"] == str(tool)
    assert second["pins"][0]["sha256"] == _sha256(tool)
    assert second["pins"][0]["sha256"] != first_hash

    assert runbook_cmd.plan(target=tmp_path, runbook=runbook, json_output=True) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["steps"][0]["pin"]["status"] == "ok"


def test_runbook_run_version_output_comes_from_resolved_pinned_binary(tmp_path, capsys):
    tool = _write_tool(tmp_path / "tool")
    marker = tmp_path / "marker"
    runbook = tmp_path / "pinned.json"
    runbook.write_text(
        json.dumps(
            {
                "id": "pinned-version",
                "allowed_commands": ["tool"],
                "pins": [
                    {
                        "command": "tool",
                        "path": str(tool),
                        "sha256": _sha256(tool),
                        "version_cmd": "--version",
                        "version": "tool 1.2.3",
                    }
                ],
                "steps": [{"id": "run", "run": f"{tool} {marker}"}],
            }
        )
    )

    assert runbook_cmd.run(target=tmp_path, runbook=runbook, approved=True, json_output=True) == 0
    receipt = json.loads(capsys.readouterr().out)
    check = receipt["pin_checks"][0]
    assert check["version_output"] == "tool 1.2.3"
    assert check["version_exit_code"] == 0


def test_runbook_run_dry_run_does_not_execute_version_cmd(tmp_path, capsys):
    version_marker = tmp_path / "version-ran"
    tool = _write_tool(
        tmp_path / "tool",
        body="\n".join(
            [
                "#!/bin/sh",
                'if [ "$1" = "--version" ]; then',
                f'  printf ran > "{version_marker}"',
                "  printf 'tool 1.2.3\\n'",
                "  exit 0",
                "fi",
                'printf ran > "$1"',
                "",
            ]
        ),
    )
    marker = tmp_path / "marker"
    runbook = tmp_path / "pinned.json"
    runbook.write_text(
        json.dumps(
            {
                "id": "pinned-dry",
                "allowed_commands": ["tool"],
                "pins": [
                    {
                        "command": "tool",
                        "path": str(tool),
                        "sha256": _sha256(tool),
                        "version_cmd": "--version",
                        "version": "tool 1.2.3",
                    }
                ],
                "steps": [{"id": "run", "run": f"{tool} {marker}"}],
            }
        )
    )

    assert runbook_cmd.run(target=tmp_path, runbook=runbook, approved=True, dry_run=True, json_output=True) == 0
    capsys.readouterr()
    assert not version_marker.exists()
    assert not marker.exists()

    assert runbook_cmd.run(target=tmp_path, runbook=runbook, approved=True, json_output=True) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert version_marker.read_text() == "ran"
    assert receipt["pin_checks"][0]["version_output"] == "tool 1.2.3"
