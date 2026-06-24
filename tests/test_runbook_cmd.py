from __future__ import annotations

import json

from brigade import cli, runbook_cmd


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
