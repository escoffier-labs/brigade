"""CLI for brigade projection recover (issue #910)."""

from __future__ import annotations

import json
from pathlib import Path

from brigade import cli
from brigade.projection import kernel as projection
from brigade.projection.fixture import mixed_workspace


def test_projection_recover_cli_restores_and_prints_json(tmp_path: Path, capsys) -> None:
    workspace, plan = mixed_workspace(tmp_path)
    projection.execute(
        plan,
        target=workspace,
        inject=projection.FailureInjector(boundary="commit:1:after", restore_boundary="restore:0:before"),
    )
    code = cli.main(["projection", "recover", plan.operation_id, "--target", str(workspace), "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["terminal_state"] == "restored"
    assert payload["operation_id"] == plan.operation_id
    assert not (workspace / "a-create.txt").exists()
    assert (workspace / "c-remove.txt").is_file()


def test_projection_recover_cli_force_after_drift(tmp_path: Path, capsys) -> None:
    workspace, plan = mixed_workspace(tmp_path)
    projection.execute(
        plan,
        target=workspace,
        inject=projection.FailureInjector(boundary="commit:1:after", restore_boundary="restore:0:before"),
    )
    (workspace / "a-create.txt").write_bytes(b"user-edited\n")
    code = cli.main(["projection", "recover", plan.operation_id, "--target", str(workspace), "--json"])
    assert code == 2
    err = capsys.readouterr().err
    assert "changed" in err.lower() or "drift" in err.lower()
    code = cli.main(
        [
            "projection",
            "recover",
            plan.operation_id,
            "--target",
            str(workspace),
            "--force",
            "--json",
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["terminal_state"] == "restored"


def test_projection_command_is_grouped() -> None:
    names = [name for _, names in cli.COMMAND_GROUPS for name in names]
    assert "projection" in names
